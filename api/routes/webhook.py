"""TradingView webhook receiver — the only inbound path from TradingView.

POST /webhook/alert
    Receives a JSON alert from TradingView Pine Script.
    - Validates the shared secret.
    - Logs the signal to SQLite.
    - Reconstructs open/close trades from paired buy/sell signals.
    - Saves an equity snapshot for the equity curve.
    - Broadcasts the new signal over WebSocket to the dashboard.
    - Sends a Slack notification if a circuit-breaker threshold is hit.

Expected JSON payload from Pine Script alert message field:
{
    "account":         "Demo NQ",        ← must match an account.name in DB
    "symbol":          "NQM5",
    "action":          "buy",            ← "buy" | "sell" | "flat"
    "contracts":       2,
    "price":           19420.0,
    "stop":            19250.0,
    "take_profit":     19700.0,
    "strategy":        "regime_trader_v1",
    "regime":          "LOW_VOL",
    "strategy_equity": 52000.0,          ← {{strategy.equity}}
    "strategy_pnl":    2000.0,           ← {{strategy.netprofit}}
    "position_size":   2,                ← {{strategy.position_size}}
    "secret":          "YOUR_SECRET"
}
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from pydantic import BaseModel

from db.database import Database
from db import queries as q
from api.deps import get_database
from api.ws import broadcast

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
SLACK_WEBHOOK  = os.getenv("SLACK_WEBHOOK_URL", "")

# Circuit-breaker thresholds (must match Pine Script and RiskConfig)
_CB_THRESHOLDS = [
    (-0.10, "PEAK_HALT   — trading paused, delete trading_halted.lock to resume"),
    (-0.07, "WEEKLY_HALT — no new positions for the rest of the week"),
    (-0.05, "WEEKLY_REDUCE — position sizes reduced 50% for the rest of the week"),
    (-0.03, "DAILY_HALT  — no new positions for today"),
    (-0.02, "DAILY_REDUCE — position sizes reduced 50% for today"),
]


class WebhookResponse(BaseModel):
    status: str
    signal_id: int
    trade_action: str   # "opened" | "closed" | "ignored"
    message: str


@router.post("/alert", response_model=WebhookResponse)
async def tradingview_alert(
    request: Request,
    background_tasks: BackgroundTasks,
) -> WebhookResponse:
    # ── Parse body ────────────────────────────────────────────────────────────
    try:
        raw = await request.body()
        payload: dict[str, Any] = json.loads(raw)
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid JSON: {exc}")

    # ── Validate secret ───────────────────────────────────────────────────────
    if WEBHOOK_SECRET and payload.get("secret") != WEBHOOK_SECRET:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook secret")

    # ── Resolve account ───────────────────────────────────────────────────────
    db: Database = get_database()
    account_name = str(payload.get("account", "")).strip()
    if not account_name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Missing 'account' field")

    acc = q.get_account_by_name(db, account_name)
    if acc is None:
        # Auto-create account on first alert so setup is frictionless
        acc_id = q.insert_account(db, account_name)
        acc = q.get_account(db, acc_id)
        logger.info("Auto-created account '%s' from first webhook", account_name)

    # ── Extract fields ────────────────────────────────────────────────────────
    symbol         = str(payload.get("symbol", "")).upper().strip()
    action         = str(payload.get("action", "flat")).lower().strip()
    contracts      = _int(payload.get("contracts"))
    price          = _float(payload.get("price"))
    stop_price     = _float(payload.get("stop"))
    take_profit    = _float(payload.get("take_profit"))
    strategy_name  = str(payload.get("strategy", "unknown"))
    regime         = str(payload.get("regime", "")).upper() or None
    strat_equity   = _float(payload.get("strategy_equity"))
    strat_pnl      = _float(payload.get("strategy_pnl"))
    position_size  = _float(payload.get("position_size"))

    if not symbol:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Missing 'symbol'")

    logger.info("Webhook: %s %s %s x%s @ %s [account=%s]",
                action.upper(), symbol, strategy_name, contracts, price, account_name)

    # ── Check if strategy is disabled ─────────────────────────────────────────
    strat = q.get_strategy(db, strategy_name)
    approved = True
    rejection_reason = None
    if strat and not strat.is_enabled:
        approved = False
        rejection_reason = f"Strategy '{strategy_name}' is disabled"
        logger.info("Signal blocked: %s", rejection_reason)

    # ── Log signal ────────────────────────────────────────────────────────────
    signal_id = q.insert_signal(
        db,
        account_id=acc.id,
        symbol=symbol,
        action=action,
        contracts=contracts,
        price=price,
        stop_price=stop_price,
        take_profit=take_profit,
        strategy_name=strategy_name,
        regime=regime,
        strategy_equity=strat_equity,
        strategy_pnl=strat_pnl,
        position_size=position_size,
        approved=approved,
        rejection_reason=rejection_reason,
        raw_payload=payload,
    )

    # ── Equity snapshot ───────────────────────────────────────────────────────
    if strat_equity is not None:
        q.insert_equity_snapshot(db, acc.id, strat_equity, pnl=strat_pnl, regime=regime)

    # ── Trade open / close reconstruction ────────────────────────────────────
    trade_action = "ignored"
    if approved and action in ("buy", "sell") and price and contracts:
        open_trade = q.get_open_trade(db, acc.id, symbol)

        if action == "buy" and open_trade is None:
            q.open_trade(
                db, acc.id, symbol, "long", contracts, price,
                entry_signal_id=signal_id,
                strategy_name=strategy_name,
                regime_at_entry=regime,
            )
            trade_action = "opened"

        elif action == "sell" and open_trade is not None:
            q.close_trade(db, open_trade.id, price, exit_signal_id=signal_id)
            trade_action = "closed"

        elif action == "sell" and open_trade is None:
            # Short entry
            q.open_trade(
                db, acc.id, symbol, "short", contracts, price,
                entry_signal_id=signal_id,
                strategy_name=strategy_name,
                regime_at_entry=regime,
            )
            trade_action = "opened"

    # ── Background: broadcast + circuit breaker check ────────────────────────
    background_tasks.add_task(
        _post_alert_tasks, acc.id, signal_id, strat_equity, strat_pnl, payload
    )

    return WebhookResponse(
        status="ok" if approved else "blocked",
        signal_id=signal_id,
        trade_action=trade_action,
        message=rejection_reason or f"{action.upper()} {symbol} logged",
    )


# ── Background tasks ──────────────────────────────────────────────────────────

async def _post_alert_tasks(
    account_id: int,
    signal_id: int,
    equity: float | None,
    pnl: float | None,
    payload: dict,
) -> None:
    """Broadcast update + check circuit breakers after each alert."""
    # Push to WebSocket clients
    await broadcast({
        "type": "signal",
        "account_id": account_id,
        "signal_id": signal_id,
        "payload": {k: v for k, v in payload.items() if k != "secret"},
    })

    # Circuit-breaker notification
    if equity and pnl:
        pnl_pct = pnl / equity
        for threshold, label in _CB_THRESHOLDS:
            if pnl_pct <= threshold:
                msg = (
                    f":rotating_light: *Circuit Breaker Triggered* — account `{account_id}`\n"
                    f"*{label}*\n"
                    f"Session P&L: `{pnl_pct:.1%}` (${pnl:,.0f})\n"
                    f"Equity: `${equity:,.0f}`\n"
                    f"Action: Pause the TradingView strategy manually."
                )
                await _send_slack(msg)
                break


async def _send_slack(text: str) -> None:
    if not SLACK_WEBHOOK:
        return
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            await client.post(SLACK_WEBHOOK, json={"text": text}, timeout=5)
    except Exception as exc:
        logger.warning("Slack notification failed: %s", exc)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _float(val: Any) -> float | None:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _int(val: Any) -> int | None:
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None
