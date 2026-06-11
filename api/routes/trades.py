"""Trade history endpoints — powers the Past Trades page.

GET /api/trades/{account_id}          paginated trade list with filters
GET /api/trades/{account_id}/summary  aggregate stats (win rate, total P&L…)
GET /api/trades/{account_id}/equity   equity curve time-series
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from db.database import Database
from db import queries as q
from api.deps import get_database

router = APIRouter(prefix="/api/trades", tags=["trades"])


class TradeOut(BaseModel):
    id: int
    account_id: int
    symbol: str
    direction: str
    contracts: int
    entry_price: float
    exit_price: Optional[float]
    strategy_name: Optional[str]
    regime_at_entry: Optional[str]
    point_value: float
    pnl: Optional[float]
    pnl_pct: Optional[float]
    opened_at: str
    closed_at: Optional[str]
    duration_mins: Optional[int]
    is_open: bool
    is_winner: Optional[bool]


class TradeSummaryOut(BaseModel):
    total_trades: int
    winners: int
    losers: int
    win_rate: float
    total_pnl: float
    avg_winner: float
    avg_loser: float
    max_loss: float
    max_win: float


class EquityPoint(BaseModel):
    recorded_at: str
    equity: float
    pnl: Optional[float]
    regime: Optional[str]


@router.get("/{account_id}", response_model=list[TradeOut])
def list_trades(
    account_id: int,
    symbol: Optional[str] = Query(None),
    open_only: bool = Query(False),
    closed_only: bool = Query(False),
    limit: int = Query(200, le=1000),
    offset: int = Query(0),
    db: Database = Depends(get_database),
):
    trades = q.list_trades(
        db, account_id=account_id, symbol=symbol,
        open_only=open_only, closed_only=closed_only,
        limit=limit, offset=offset,
    )
    return [_to_out(t) for t in trades]


@router.get("/{account_id}/summary", response_model=TradeSummaryOut)
def get_summary(account_id: int, db: Database = Depends(get_database)):
    s = q.trade_summary(db, account_id=account_id)
    return TradeSummaryOut(
        total_trades=s.get("total_trades", 0),
        winners=s.get("winners", 0),
        losers=s.get("losers", 0),
        win_rate=s.get("win_rate", 0.0),
        total_pnl=s.get("total_pnl", 0.0),
        avg_winner=s.get("avg_winner", 0.0),
        avg_loser=s.get("avg_loser", 0.0),
        max_loss=s.get("max_loss", 0.0),
        max_win=s.get("max_win", 0.0),
    )


@router.get("/{account_id}/equity", response_model=list[EquityPoint])
def get_equity_curve(
    account_id: int,
    limit: int = Query(500, le=2000),
    db: Database = Depends(get_database),
):
    snaps = q.list_equity_snapshots(db, account_id=account_id, limit=limit)
    return [
        EquityPoint(recorded_at=s.recorded_at, equity=s.equity, pnl=s.pnl, regime=s.regime)
        for s in snaps
    ]


def _to_out(t) -> TradeOut:
    return TradeOut(
        id=t.id, account_id=t.account_id,
        symbol=t.symbol, direction=t.direction,
        contracts=t.contracts, entry_price=t.entry_price,
        exit_price=t.exit_price, strategy_name=t.strategy_name,
        regime_at_entry=t.regime_at_entry, point_value=t.point_value,
        pnl=round(t.pnl, 2) if t.pnl is not None else None,
        pnl_pct=round(t.pnl_pct, 4) if t.pnl_pct is not None else None,
        opened_at=t.opened_at, closed_at=t.closed_at,
        duration_mins=t.duration_mins,
        is_open=t.is_open, is_winner=t.is_winner,
    )
