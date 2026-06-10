"""FastAPI webhook server for TradingView alerts.

TradingView fires a POST request to /alert whenever a Pine Script alert
condition triggers.  This server:
  1. Validates the shared secret.
  2. Parses the JSON payload via tv_adapter.
  3. Gates the signal through the RiskManager (circuit breakers, sizing).
  4. Executes via OrderExecutor → AlpacaClient.

Setup
-----
1. Copy config/credentials.yaml.example to config/credentials.yaml and fill in.
2. Set WEBHOOK_SECRET in .env (must match the `secret` field in Pine Script).
3. Run: uvicorn broker.tv_webhook:app --host 0.0.0.0 --port 8000
4. Expose port 8000 publicly:
   - Dev:  ngrok http 8000   → copy the https://xxxx.ngrok.io URL
   - Prod: deploy to a VPS or cloud function
5. Paste the public URL into the TradingView alert webhook field.

Endpoints
---------
POST /alert     — TradingView webhook receiver
GET  /health    — liveness check
GET  /status    — portfolio snapshot + circuit breaker status
POST /manual    — fire a manual test signal (dev only)
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from broker.alpaca_client import AlpacaClient, AlpacaConfig
from broker.order_executor import OrderExecutor, OrderStatus
from broker.position_tracker import PositionTracker
from broker import tv_adapter
from core.regime_strategies import Direction
from core.risk_manager import (
    CircuitBreakerStatus,
    PortfolioState,
    Position,
    RiskConfig,
    RiskManager,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App-level singletons (initialised in lifespan)
# ---------------------------------------------------------------------------

_alpaca: Optional[AlpacaClient] = None
_tracker: Optional[PositionTracker] = None
_risk_mgr: Optional[RiskManager] = None
_executor: Optional[OrderExecutor] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Connect to Alpaca on startup, disconnect on shutdown."""
    global _alpaca, _tracker, _risk_mgr, _executor

    api_key    = os.getenv("ALPACA_API_KEY", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY", "")
    paper      = os.getenv("ALPACA_PAPER", "true").lower() != "false"
    dry_run    = os.getenv("DRY_RUN", "false").lower() == "true"

    if not api_key or not secret_key:
        logger.warning(
            "ALPACA_API_KEY / ALPACA_SECRET_KEY not set — running in DRY_RUN mode"
        )
        dry_run = True

    _alpaca = AlpacaClient(AlpacaConfig(api_key=api_key, secret_key=secret_key, paper=paper))
    if not dry_run:
        _alpaca.connect()
        logger.info("AlpacaClient connected (paper=%s)", paper)

    _tracker  = PositionTracker(_alpaca)

    initial_nav = 100_000.0
    if not dry_run:
        try:
            acct = _alpaca.get_account()
            initial_nav = float(acct.portfolio_value or acct.equity or initial_nav)
        except Exception:
            pass

    _risk_mgr = RiskManager(RiskConfig(), initial_nav=initial_nav)
    _executor = OrderExecutor(_alpaca, _risk_mgr, dry_run=dry_run)

    if not dry_run:
        _tracker.reconcile()
        account = _alpaca.get_account()
        nav = float(account.portfolio_value or account.equity or 0)
        _tracker.reset_daily(nav)

    logger.info("WebhookServer ready (dry_run=%s)", dry_run)
    yield

    if _alpaca is not None:
        _alpaca.disconnect()
    logger.info("WebhookServer shut down")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="regime-trader webhook",
    description="TradingView → Alpaca execution bridge",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # restrict in prod
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")


async def _require_secret(request: Request) -> None:
    """Validate the shared secret from the JSON body or X-TV-Secret header."""
    if not WEBHOOK_SECRET:
        return  # secret not configured — open for dev

    # Header check (preferred)
    header_secret = request.headers.get("x-tv-secret", "")
    if header_secret and header_secret == WEBHOOK_SECRET:
        return

    # Body check — secret embedded in JSON payload
    try:
        body = await request.body()
        payload = json.loads(body)
        if payload.get("secret") == WEBHOOK_SECRET:
            request.state.cached_body = body  # cache so body can be re-read below
            return
    except Exception:
        pass

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing webhook secret",
    )


# ---------------------------------------------------------------------------
# Helper: build PortfolioState from live tracker
# ---------------------------------------------------------------------------

def _build_portfolio_state() -> PortfolioState:
    """Construct a PortfolioState from the live position tracker + Alpaca account."""
    if _alpaca is None or _tracker is None:
        return PortfolioState(equity=100_000, cash=100_000, buying_power=100_000)

    try:
        account = _alpaca.get_account()
        equity  = float(account.portfolio_value or account.equity or 0)
        cash    = float(account.cash or 0)
        bp      = float(account.buying_power or cash)
    except Exception:
        snap = _tracker.snapshot()
        equity = snap.nav
        cash   = snap.cash
        bp     = cash

    positions: dict[str, Position] = {}
    for sym, rec in (_tracker._positions or {}).items():
        positions[sym] = Position(
            symbol=sym,
            shares=rec.shares,
            entry_price=rec.avg_entry_price,
            stop_price=rec.avg_entry_price * 0.97,  # default 3% stop estimate
            current_price=rec.current_price,
        )

    return PortfolioState(
        equity=equity,
        cash=cash,
        buying_power=bp,
        positions=positions,
        daily_pnl=_tracker.daily_pnl(),
        peak_equity=equity,
        day_open_equity=_tracker._day_open_nav or equity,
        trades_today=0,
    )


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class AlertResponse(BaseModel):
    status: str
    symbol: Optional[str] = None
    action: Optional[str] = None
    shares: Optional[int] = None
    order_id: Optional[str] = None
    reason: Optional[str] = None
    timestamp: str = ""


class ManualSignalRequest(BaseModel):
    symbol: str
    action: str          # "buy" | "sell" | "flat"
    price: float
    stop: float = 0.0
    take_profit: float = 0.0
    shares_override: Optional[int] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness check."""
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "alpaca_connected": _alpaca is not None,
    }


@app.get("/status")
async def portfolio_status() -> dict[str, Any]:
    """Return portfolio snapshot and circuit breaker state."""
    if _tracker is None:
        raise HTTPException(status_code=503, detail="Tracker not initialised")
    snap = _tracker.snapshot()
    cb_status = (
        _risk_mgr.circuit_breaker._status.value
        if _risk_mgr else CircuitBreakerStatus.NONE.value
    )
    return {
        "nav": snap.nav,
        "cash": snap.cash,
        "gross_exposure": snap.gross_exposure,
        "daily_pnl": snap.daily_pnl,
        "positions": {p.symbol: {"shares": p.shares, "pnl": p.unrealised_pnl}
                      for p in snap.positions},
        "circuit_breaker": cb_status,
        "timestamp": snap.timestamp.isoformat(),
    }


@app.post("/alert", response_model=AlertResponse)
async def tradingview_alert(
    request: Request,
    background_tasks: BackgroundTasks,
    _: None = Depends(_require_secret),
) -> AlertResponse:
    """Receive and execute a TradingView webhook alert."""
    # Read body (may have been cached by _require_secret)
    cached = getattr(request.state, "cached_body", None)
    raw_body = cached if cached is not None else await request.body()

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")

    # Parse TradingView payload
    try:
        tv = tv_adapter.parse_payload(payload)
    except ValueError as exc:
        logger.warning("Payload parse error: %s | body=%s", exc, raw_body[:200])
        raise HTTPException(status_code=422, detail=str(exc))

    logger.info("TV alert: %s %s @ %.2f", tv.action.upper(), tv.symbol, tv.price)

    # Convert to Signal for RiskManager
    signal = tv_adapter.to_signal(tv)

    # Gate: flat signals just log and return
    if signal.direction == Direction.FLAT:
        background_tasks.add_task(_sync_tracker)
        return AlertResponse(
            status="ignored",
            symbol=tv.symbol,
            action="flat",
            reason="FLAT direction — no order placed",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    # Validate through RiskManager
    portfolio_state = _build_portfolio_state()
    try:
        decision = _risk_mgr.validate_signal(signal, portfolio_state)
    except Exception as exc:
        logger.error("RiskManager error: %s", exc)
        raise HTTPException(status_code=500, detail=f"Risk validation error: {exc}")

    if not decision.approved:
        logger.info(
            "Signal rejected by RiskManager: %s | reason=%s",
            tv.symbol, decision.rejection_reason,
        )
        return AlertResponse(
            status="rejected",
            symbol=tv.symbol,
            action=tv.action,
            reason=decision.rejection_reason,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    approved_shares = int(decision.final_shares or 0)
    if approved_shares <= 0:
        return AlertResponse(
            status="rejected",
            symbol=tv.symbol,
            action=tv.action,
            shares=0,
            reason="Zero shares after risk sizing",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    # Execute order
    from alpaca.trading.enums import OrderSide
    order_side = OrderSide.BUY if tv.action == "buy" else OrderSide.SELL
    stop_price = tv.stop if tv.stop > 0 else None
    tp_price   = tv.take_profit if tv.take_profit > 0 else None

    try:
        record = _executor.place_tv_order(
            symbol=tv.symbol,
            side=order_side,
            shares=approved_shares,
            stop_loss=stop_price,
            take_profit=tp_price,
        )
    except Exception as exc:
        logger.error("Order execution error: %s", exc)
        raise HTTPException(status_code=500, detail=f"Order failed: {exc}")

    # Schedule background reconciliation
    background_tasks.add_task(_sync_tracker)

    final_status = "executed" if record.status == OrderStatus.SUBMITTED else record.status.value
    return AlertResponse(
        status=final_status,
        symbol=tv.symbol,
        action=tv.action,
        shares=approved_shares,
        order_id=record.id,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@app.post("/manual", response_model=AlertResponse)
async def manual_signal(body: ManualSignalRequest) -> AlertResponse:
    """Fire a manual test signal (development use only).

    Does NOT require webhook secret — add auth before exposing publicly.
    """
    fake_payload = {
        "symbol": body.symbol,
        "action": body.action,
        "price":  body.price,
        "stop":   body.stop,
        "take_profit": body.take_profit,
        "strategy": "manual_test",
    }
    request_obj = Request(  # type: ignore[call-arg]
        scope={"type": "http", "method": "POST", "headers": []},
    )
    request_obj.state.cached_body = json.dumps(fake_payload).encode()
    return await tradingview_alert(request_obj, BackgroundTasks(), _=None)


# ---------------------------------------------------------------------------
# Background helpers
# ---------------------------------------------------------------------------

async def _sync_tracker() -> None:
    """Reconcile position tracker with Alpaca (runs after each fill)."""
    if _tracker is not None:
        try:
            _tracker.reconcile()
        except Exception as exc:
            logger.error("Background reconcile failed: %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "broker.tv_webhook:app",
        host="0.0.0.0",
        port=int(os.getenv("WEBHOOK_PORT", "8000")),
        reload=False,
        log_level="info",
    )
