"""Strategy management endpoints — powers the Strategies page.

GET   /api/strategies              list all registered strategies
PATCH /api/strategies/{name}       enable or disable a strategy
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db.database import Database
from db import queries as q
from api.deps import get_database

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


class StrategyOut(BaseModel):
    id: int
    name: str
    description: Optional[str]
    is_enabled: bool
    created_at: str
    last_signal: Optional[str]


class StrategyUpdate(BaseModel):
    is_enabled: Optional[bool] = None
    description: Optional[str] = None


@router.get("", response_model=list[StrategyOut])
def list_strategies(db: Database = Depends(get_database)):
    return [_to_out(s) for s in q.list_strategies(db)]


@router.patch("/{name}", response_model=StrategyOut)
def update_strategy(name: str, body: StrategyUpdate, db: Database = Depends(get_database)):
    strat = q.get_strategy(db, name)
    if not strat:
        raise HTTPException(404, f"Strategy '{name}' not found")
    if body.is_enabled is not None:
        q.update_strategy_enabled(db, name, body.is_enabled)
    if body.description is not None:
        db.execute(
            "UPDATE strategies SET description = ? WHERE name = ?",
            (body.description, name),
        )
        db.commit()
    return _to_out(q.get_strategy(db, name))


def _to_out(s) -> StrategyOut:
    return StrategyOut(
        id=s.id, name=s.name, description=s.description,
        is_enabled=s.is_enabled, created_at=s.created_at,
        last_signal=s.last_signal,
    )
