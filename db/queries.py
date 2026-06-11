"""Typed read/write helpers for every table.

All functions accept a Database instance so they can be used in tests
with an in-memory database.  The API layer calls these instead of writing
raw SQL inline.

Convention
----------
- list_*   → returns list[Model]
- get_*    → returns Optional[Model]
- insert_* → returns the new row id (int)
- update_* → returns rows affected (int)
- delete_* → returns rows affected (int)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from db.database import Database
from db.models import Account, EquitySnapshot, Signal, Strategy, Trade

logger = logging.getLogger(__name__)

_NOW = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731

# ---------------------------------------------------------------------------
# Point values by root symbol (for P&L calculation)
# ---------------------------------------------------------------------------

POINT_VALUES: dict[str, float] = {
    "ES":  50.0,   # E-mini S&P 500
    "NQ":  20.0,   # E-mini Nasdaq-100
    "RTY": 50.0,   # E-mini Russell 2000
    "YM":   5.0,   # E-mini Dow Jones
    "CL": 1000.0,  # Crude Oil
    "GC":  100.0,  # Gold
    "SI":  5000.0, # Silver
    "ZB":  1000.0, # 30-Year T-Bond
    "ZN":  1000.0, # 10-Year T-Note
    "6E":  1250.0, # Euro FX
    "6B":  6250.0, # British Pound
}


def point_value_for(symbol: str) -> float:
    """Return $/point for a futures symbol, defaulting to 20.0 (NQ-like)."""
    root = "".join(c for c in symbol if c.isalpha()).upper()
    # Strip trailing month/year codes e.g. "NQM5" → "NQ"
    for known in sorted(POINT_VALUES, key=len, reverse=True):
        if root.startswith(known):
            return POINT_VALUES[known]
    logger.warning("Unknown futures root for '%s', defaulting point_value=20.0", symbol)
    return 20.0


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def list_accounts(db: Database, active_only: bool = True) -> list[Account]:
    where = "WHERE is_active = 1" if active_only else ""
    rows = db.fetchall(f"SELECT * FROM accounts {where} ORDER BY name")
    return [_row_to_account(r) for r in rows]


def get_account(db: Database, account_id: int) -> Optional[Account]:
    row = db.fetchone("SELECT * FROM accounts WHERE id = ?", (account_id,))
    return _row_to_account(row) if row else None


def get_account_by_name(db: Database, name: str) -> Optional[Account]:
    row = db.fetchone("SELECT * FROM accounts WHERE name = ?", (name,))
    return _row_to_account(row) if row else None


def insert_account(
    db: Database,
    name: str,
    broker: str = "tradovate",
    environment: str = "demo",
    notes: Optional[str] = None,
) -> int:
    cur = db.execute(
        "INSERT INTO accounts (name, broker, environment, notes) VALUES (?, ?, ?, ?)",
        (name, broker, environment, notes),
    )
    db.commit()
    return cur.lastrowid


def update_account(
    db: Database,
    account_id: int,
    name: Optional[str] = None,
    environment: Optional[str] = None,
    notes: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> int:
    fields, values = [], []
    if name is not None:
        fields.append("name = ?"); values.append(name)
    if environment is not None:
        fields.append("environment = ?"); values.append(environment)
    if notes is not None:
        fields.append("notes = ?"); values.append(notes)
    if is_active is not None:
        fields.append("is_active = ?"); values.append(int(is_active))
    if not fields:
        return 0
    values.append(account_id)
    cur = db.execute(f"UPDATE accounts SET {', '.join(fields)} WHERE id = ?", tuple(values))
    db.commit()
    return cur.rowcount


def delete_account(db: Database, account_id: int) -> int:
    """Soft-delete by setting is_active = 0."""
    cur = db.execute("UPDATE accounts SET is_active = 0 WHERE id = ?", (account_id,))
    db.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


def insert_signal(
    db: Database,
    account_id: int,
    symbol: str,
    action: str,
    contracts: Optional[int] = None,
    price: Optional[float] = None,
    stop_price: Optional[float] = None,
    take_profit: Optional[float] = None,
    strategy_name: Optional[str] = None,
    regime: Optional[str] = None,
    strategy_equity: Optional[float] = None,
    strategy_pnl: Optional[float] = None,
    position_size: Optional[float] = None,
    approved: bool = True,
    rejection_reason: Optional[str] = None,
    raw_payload: Optional[dict] = None,
) -> int:
    raw_json = json.dumps(raw_payload) if raw_payload else None
    cur = db.execute(
        """
        INSERT INTO signals (
            account_id, symbol, action, contracts, price, stop_price, take_profit,
            strategy_name, regime, strategy_equity, strategy_pnl, position_size,
            approved, rejection_reason, raw_payload, received_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            account_id, symbol, action, contracts, price, stop_price, take_profit,
            strategy_name, regime, strategy_equity, strategy_pnl, position_size,
            int(approved), rejection_reason, raw_json, _NOW(),
        ),
    )
    db.commit()

    # Auto-register strategy name if new
    if strategy_name:
        _ensure_strategy(db, strategy_name)

    return cur.lastrowid


def list_signals(
    db: Database,
    account_id: Optional[int] = None,
    symbol: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Signal]:
    where, params = _build_where(account_id=account_id, symbol=symbol)
    rows = db.fetchall(
        f"SELECT * FROM signals {where} ORDER BY received_at DESC LIMIT ? OFFSET ?",
        params + (limit, offset),
    )
    return [_row_to_signal(r) for r in rows]


def get_signal(db: Database, signal_id: int) -> Optional[Signal]:
    row = db.fetchone("SELECT * FROM signals WHERE id = ?", (signal_id,))
    return _row_to_signal(row) if row else None


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------


def open_trade(
    db: Database,
    account_id: int,
    symbol: str,
    direction: str,
    contracts: int,
    entry_price: float,
    entry_signal_id: Optional[int] = None,
    strategy_name: Optional[str] = None,
    regime_at_entry: Optional[str] = None,
) -> int:
    pv = point_value_for(symbol)
    cur = db.execute(
        """
        INSERT INTO trades (
            account_id, symbol, direction, contracts, entry_price,
            entry_signal_id, strategy_name, regime_at_entry,
            point_value, opened_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            account_id, symbol, direction, contracts, entry_price,
            entry_signal_id, strategy_name, regime_at_entry,
            pv, _NOW(),
        ),
    )
    db.commit()
    return cur.lastrowid


def close_trade(
    db: Database,
    trade_id: int,
    exit_price: float,
    exit_signal_id: Optional[int] = None,
) -> int:
    trade = get_trade(db, trade_id)
    if trade is None:
        return 0
    pv = trade.point_value
    mult = 1 if trade.direction == "long" else -1
    pnl = (exit_price - trade.entry_price) * trade.contracts * pv * mult
    notional = trade.entry_price * trade.contracts * pv
    pnl_pct = pnl / notional if notional else 0.0

    opened = datetime.fromisoformat(trade.opened_at.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    duration_mins = int((now - opened).total_seconds() / 60)

    cur = db.execute(
        """
        UPDATE trades SET
            exit_price = ?, exit_signal_id = ?,
            pnl = ?, pnl_pct = ?,
            closed_at = ?, duration_mins = ?
        WHERE id = ?
        """,
        (exit_price, exit_signal_id, pnl, pnl_pct, _NOW(), duration_mins, trade_id),
    )
    db.commit()
    return cur.rowcount


def get_trade(db: Database, trade_id: int) -> Optional[Trade]:
    row = db.fetchone("SELECT * FROM trades WHERE id = ?", (trade_id,))
    return _row_to_trade(row) if row else None


def get_open_trade(db: Database, account_id: int, symbol: str) -> Optional[Trade]:
    """Return the most recent still-open trade for this account+symbol."""
    row = db.fetchone(
        """
        SELECT * FROM trades
        WHERE account_id = ? AND symbol = ? AND closed_at IS NULL
        ORDER BY opened_at DESC LIMIT 1
        """,
        (account_id, symbol),
    )
    return _row_to_trade(row) if row else None


def list_trades(
    db: Database,
    account_id: Optional[int] = None,
    symbol: Optional[str] = None,
    open_only: bool = False,
    closed_only: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> list[Trade]:
    where, params = _build_where(account_id=account_id, symbol=symbol)
    extra_clauses = []
    if open_only:
        extra_clauses.append("closed_at IS NULL")
    if closed_only:
        extra_clauses.append("closed_at IS NOT NULL")
    if extra_clauses:
        connector = "WHERE" if not where else "AND"
        where += f" {connector} " + " AND ".join(extra_clauses)

    rows = db.fetchall(
        f"SELECT * FROM trades {where} ORDER BY opened_at DESC LIMIT ? OFFSET ?",
        params + (limit, offset),
    )
    return [_row_to_trade(r) for r in rows]


def trade_summary(db: Database, account_id: Optional[int] = None) -> dict:
    """Aggregate stats for the Trades page summary row."""
    where, params = _build_where(account_id=account_id)
    closed_where = where + (" AND " if where else "WHERE ") + "closed_at IS NOT NULL"

    row = db.fetchone(
        f"""
        SELECT
            COUNT(*)                         AS total_trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS winners,
            SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS losers,
            SUM(pnl)                         AS total_pnl,
            AVG(CASE WHEN pnl > 0 THEN pnl END) AS avg_winner,
            AVG(CASE WHEN pnl < 0 THEN pnl END) AS avg_loser,
            MIN(pnl)                         AS max_loss,
            MAX(pnl)                         AS max_win
        FROM trades {closed_where}
        """,
        params,
    )
    if not row:
        return {}
    total = row["total_trades"] or 0
    winners = row["winners"] or 0
    return {
        "total_trades": total,
        "winners": winners,
        "losers": row["losers"] or 0,
        "win_rate": round(winners / total, 4) if total else 0.0,
        "total_pnl": round(row["total_pnl"] or 0, 2),
        "avg_winner": round(row["avg_winner"] or 0, 2),
        "avg_loser": round(row["avg_loser"] or 0, 2),
        "max_loss": round(row["max_loss"] or 0, 2),
        "max_win": round(row["max_win"] or 0, 2),
    }


# ---------------------------------------------------------------------------
# Equity Snapshots
# ---------------------------------------------------------------------------


def insert_equity_snapshot(
    db: Database,
    account_id: int,
    equity: float,
    cash: Optional[float] = None,
    pnl: Optional[float] = None,
    regime: Optional[str] = None,
) -> int:
    cur = db.execute(
        """
        INSERT INTO equity_snapshots (account_id, equity, cash, pnl, regime, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (account_id, equity, cash, pnl, regime, _NOW()),
    )
    db.commit()
    return cur.lastrowid


def list_equity_snapshots(
    db: Database,
    account_id: int,
    limit: int = 500,
) -> list[EquitySnapshot]:
    rows = db.fetchall(
        "SELECT * FROM equity_snapshots WHERE account_id = ? ORDER BY recorded_at ASC LIMIT ?",
        (account_id, limit),
    )
    return [_row_to_equity(r) for r in rows]


def latest_equity(db: Database, account_id: int) -> Optional[EquitySnapshot]:
    row = db.fetchone(
        "SELECT * FROM equity_snapshots WHERE account_id = ? ORDER BY recorded_at DESC LIMIT 1",
        (account_id,),
    )
    return _row_to_equity(row) if row else None


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def list_strategies(db: Database) -> list[Strategy]:
    rows = db.fetchall("SELECT * FROM strategies ORDER BY name")
    return [_row_to_strategy(r) for r in rows]


def get_strategy(db: Database, name: str) -> Optional[Strategy]:
    row = db.fetchone("SELECT * FROM strategies WHERE name = ?", (name,))
    return _row_to_strategy(row) if row else None


def update_strategy_enabled(db: Database, name: str, enabled: bool) -> int:
    cur = db.execute(
        "UPDATE strategies SET is_enabled = ? WHERE name = ?",
        (int(enabled), name),
    )
    db.commit()
    return cur.rowcount


def update_strategy_last_signal(db: Database, name: str) -> None:
    db.execute(
        "UPDATE strategies SET last_signal = ? WHERE name = ?",
        (_NOW(), name),
    )
    db.commit()


def _ensure_strategy(db: Database, name: str) -> None:
    """Insert strategy if it doesn't exist; update last_signal timestamp."""
    db.execute(
        "INSERT OR IGNORE INTO strategies (name) VALUES (?)", (name,)
    )
    db.execute(
        "UPDATE strategies SET last_signal = ? WHERE name = ?", (_NOW(), name)
    )
    db.commit()


# ---------------------------------------------------------------------------
# Internal row → model converters
# ---------------------------------------------------------------------------


def _row_to_account(r: object) -> Account:
    return Account(
        id=r["id"], name=r["name"], broker=r["broker"],
        environment=r["environment"], notes=r["notes"],
        is_active=bool(r["is_active"]), created_at=r["created_at"],
    )


def _row_to_signal(r: object) -> Signal:
    return Signal(
        id=r["id"], account_id=r["account_id"],
        symbol=r["symbol"], action=r["action"],
        contracts=r["contracts"], price=r["price"],
        stop_price=r["stop_price"], take_profit=r["take_profit"],
        strategy_name=r["strategy_name"], regime=r["regime"],
        strategy_equity=r["strategy_equity"], strategy_pnl=r["strategy_pnl"],
        position_size=r["position_size"],
        approved=bool(r["approved"]), rejection_reason=r["rejection_reason"],
        raw_payload=r["raw_payload"], received_at=r["received_at"],
    )


def _row_to_trade(r: object) -> Trade:
    return Trade(
        id=r["id"], account_id=r["account_id"],
        symbol=r["symbol"], direction=r["direction"],
        contracts=r["contracts"], entry_price=r["entry_price"],
        exit_price=r["exit_price"],
        entry_signal_id=r["entry_signal_id"], exit_signal_id=r["exit_signal_id"],
        strategy_name=r["strategy_name"], regime_at_entry=r["regime_at_entry"],
        point_value=r["point_value"], pnl=r["pnl"], pnl_pct=r["pnl_pct"],
        opened_at=r["opened_at"], closed_at=r["closed_at"],
        duration_mins=r["duration_mins"],
    )


def _row_to_equity(r: object) -> EquitySnapshot:
    return EquitySnapshot(
        id=r["id"], account_id=r["account_id"],
        equity=r["equity"], cash=r["cash"], pnl=r["pnl"],
        regime=r["regime"], recorded_at=r["recorded_at"],
    )


def _row_to_strategy(r: object) -> Strategy:
    return Strategy(
        id=r["id"], name=r["name"], description=r["description"],
        is_enabled=bool(r["is_enabled"]), created_at=r["created_at"],
        last_signal=r["last_signal"],
    )


# ---------------------------------------------------------------------------
# Query builder helper
# ---------------------------------------------------------------------------


def _build_where(
    account_id: Optional[int] = None,
    symbol: Optional[str] = None,
) -> tuple[str, tuple]:
    clauses, params = [], []
    if account_id is not None:
        clauses.append("account_id = ?"); params.append(account_id)
    if symbol is not None:
        clauses.append("symbol = ?"); params.append(symbol)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, tuple(params)
