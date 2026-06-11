"""Portfolio snapshot endpoint — powers the Dashboard top bar.

GET /api/portfolio/{account_id}
    Returns latest equity, open positions, session P&L,
    current regime, and circuit-breaker status.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db.database import Database
from db import queries as q
from api.deps import get_database

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


class OpenPositionOut(BaseModel):
    trade_id: int
    symbol: str
    direction: str
    contracts: int
    entry_price: float
    strategy_name: Optional[str]
    regime_at_entry: Optional[str]
    opened_at: str
    point_value: float
    unrealised_pnl: Optional[float]   # requires current_price from latest signal


class PortfolioOut(BaseModel):
    account_id: int
    account_name: str
    strategy_equity: Optional[float]    # from latest equity snapshot
    strategy_pnl: Optional[float]       # running P&L from TV strategy
    regime: Optional[str]               # latest regime label
    open_positions: list[OpenPositionOut]
    circuit_breaker: str                # "NONE" | "REDUCE" | "HALT"
    last_updated: Optional[str]


@router.get("/{account_id}", response_model=PortfolioOut)
def get_portfolio(account_id: int, db: Database = Depends(get_database)):
    acc = q.get_account(db, account_id)
    if not acc:
        raise HTTPException(404, "Account not found")

    snap = q.latest_equity(db, account_id)
    open_trades = q.list_trades(db, account_id=account_id, open_only=True)

    # Derive circuit-breaker state from session P&L in snapshots
    cb_status = _compute_cb_status(db, account_id, snap)

    positions = []
    for t in open_trades:
        # Estimate unrealised P&L using the last known price from signals
        last_sig = _last_price_for_symbol(db, account_id, t.symbol)
        unrealised = None
        if last_sig and t.entry_price:
            mult = 1 if t.direction == "long" else -1
            unrealised = (last_sig - t.entry_price) * t.contracts * t.point_value * mult

        positions.append(OpenPositionOut(
            trade_id=t.id,
            symbol=t.symbol,
            direction=t.direction,
            contracts=t.contracts,
            entry_price=t.entry_price,
            strategy_name=t.strategy_name,
            regime_at_entry=t.regime_at_entry,
            opened_at=t.opened_at,
            point_value=t.point_value,
            unrealised_pnl=round(unrealised, 2) if unrealised is not None else None,
        ))

    return PortfolioOut(
        account_id=account_id,
        account_name=acc.name,
        strategy_equity=snap.equity if snap else None,
        strategy_pnl=snap.pnl if snap else None,
        regime=snap.regime if snap else None,
        open_positions=positions,
        circuit_breaker=cb_status,
        last_updated=snap.recorded_at if snap else None,
    )


def _last_price_for_symbol(db: Database, account_id: int, symbol: str) -> Optional[float]:
    """Return the most recent price for this symbol from the signal log."""
    row = db.fetchone(
        """
        SELECT price FROM signals
        WHERE account_id = ? AND symbol = ? AND price IS NOT NULL
        ORDER BY received_at DESC LIMIT 1
        """,
        (account_id, symbol),
    )
    return float(row["price"]) if row else None


def _compute_cb_status(db: Database, account_id: int, snap) -> str:
    """Derive a simple circuit-breaker label from today's P&L snapshots."""
    if snap is None:
        return "NONE"

    # Pull session P&L from strategy_pnl field of the latest signal
    row = db.fetchone(
        """
        SELECT strategy_equity, strategy_pnl FROM signals
        WHERE account_id = ? AND strategy_equity IS NOT NULL
        ORDER BY received_at DESC LIMIT 1
        """,
        (account_id,),
    )
    if not row or not row["strategy_equity"]:
        return "NONE"

    pnl = row["strategy_pnl"] or 0
    equity = row["strategy_equity"]
    pnl_pct = pnl / equity if equity else 0

    # Thresholds (match RiskConfig defaults)
    if pnl_pct <= -0.10:
        return "PEAK_HALT"
    if pnl_pct <= -0.07:
        return "WEEKLY_HALT"
    if pnl_pct <= -0.05:
        return "WEEKLY_REDUCE"
    if pnl_pct <= -0.03:
        return "DAILY_HALT"
    if pnl_pct <= -0.02:
        return "DAILY_REDUCE"
    return "NONE"
