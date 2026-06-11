"""Signal log endpoints — the live feed on the dashboard.

GET /api/signals/{account_id}   recent webhook alerts (newest first)
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from db.database import Database
from db import queries as q
from api.deps import get_database

router = APIRouter(prefix="/api/signals", tags=["signals"])


class SignalOut(BaseModel):
    id: int
    account_id: int
    symbol: str
    action: str
    contracts: Optional[int]
    price: Optional[float]
    stop_price: Optional[float]
    take_profit: Optional[float]
    strategy_name: Optional[str]
    regime: Optional[str]
    strategy_equity: Optional[float]
    strategy_pnl: Optional[float]
    position_size: Optional[float]
    approved: bool
    rejection_reason: Optional[str]
    received_at: str


@router.get("/{account_id}", response_model=list[SignalOut])
def list_signals(
    account_id: int,
    symbol: Optional[str] = Query(None),
    limit: int = Query(50, le=500),
    offset: int = Query(0),
    db: Database = Depends(get_database),
):
    sigs = q.list_signals(db, account_id=account_id, symbol=symbol, limit=limit, offset=offset)
    return [_to_out(s) for s in sigs]


def _to_out(s) -> SignalOut:
    return SignalOut(
        id=s.id, account_id=s.account_id,
        symbol=s.symbol, action=s.action,
        contracts=s.contracts, price=s.price,
        stop_price=s.stop_price, take_profit=s.take_profit,
        strategy_name=s.strategy_name, regime=s.regime,
        strategy_equity=s.strategy_equity, strategy_pnl=s.strategy_pnl,
        position_size=s.position_size,
        approved=s.approved, rejection_reason=s.rejection_reason,
        received_at=s.received_at,
    )
