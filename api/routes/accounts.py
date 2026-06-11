"""Account CRUD endpoints.

GET    /api/accounts              list all active accounts
POST   /api/accounts              create a new account
GET    /api/accounts/{id}         get one account
PATCH  /api/accounts/{id}         update name / environment / notes
DELETE /api/accounts/{id}         soft-delete (sets is_active=0)
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db.database import Database
from db import queries as q
from api.deps import get_database

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class AccountCreate(BaseModel):
    name: str
    broker: str = "tradovate"
    environment: str = "demo"   # "demo" | "live"
    notes: Optional[str] = None


class AccountUpdate(BaseModel):
    name: Optional[str] = None
    environment: Optional[str] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None


class AccountOut(BaseModel):
    id: int
    name: str
    broker: str
    environment: str
    notes: Optional[str]
    is_active: bool
    created_at: str


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[AccountOut])
def list_accounts(db: Database = Depends(get_database)):
    return [_to_out(a) for a in q.list_accounts(db)]


@router.post("", response_model=AccountOut, status_code=201)
def create_account(body: AccountCreate, db: Database = Depends(get_database)):
    if q.get_account_by_name(db, body.name):
        raise HTTPException(400, f"Account '{body.name}' already exists")
    acc_id = q.insert_account(db, body.name, body.broker, body.environment, body.notes)
    return _to_out(q.get_account(db, acc_id))


@router.get("/{account_id}", response_model=AccountOut)
def get_account(account_id: int, db: Database = Depends(get_database)):
    acc = q.get_account(db, account_id)
    if not acc:
        raise HTTPException(404, "Account not found")
    return _to_out(acc)


@router.patch("/{account_id}", response_model=AccountOut)
def update_account(account_id: int, body: AccountUpdate, db: Database = Depends(get_database)):
    if not q.get_account(db, account_id):
        raise HTTPException(404, "Account not found")
    q.update_account(db, account_id, body.name, body.environment, body.notes, body.is_active)
    return _to_out(q.get_account(db, account_id))


@router.delete("/{account_id}", status_code=204)
def delete_account(account_id: int, db: Database = Depends(get_database)):
    if not q.get_account(db, account_id):
        raise HTTPException(404, "Account not found")
    q.delete_account(db, account_id)


def _to_out(a) -> AccountOut:
    return AccountOut(
        id=a.id, name=a.name, broker=a.broker,
        environment=a.environment, notes=a.notes,
        is_active=a.is_active, created_at=a.created_at,
    )
