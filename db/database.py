"""SQLite connection management and schema migrations.

Usage
-----
    from db.database import get_db

    db = get_db()                 # returns singleton Database
    db.execute("SELECT ...")      # raw query
    db.close()                    # explicit close (optional — auto-closed on exit)

The database file is created at DB_PATH (default: ./data/regime_trader.db).
Set DB_PATH in .env to override.

Migrations
----------
Each migration is a list of SQL statements tagged with a version number.
Running get_db() applies any outstanding migrations automatically, so the
schema is always up-to-date without a separate migration runner.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "regime_trader.db"
_instance: Optional["Database"] = None


# ---------------------------------------------------------------------------
# Schema migrations — append new versions; never edit existing ones
# ---------------------------------------------------------------------------

_MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, [
        # ── Accounts ─────────────────────────────────────────────────────────
        # Each row = one TradingView-connected Tradovate account profile.
        # Credentials are NOT stored here; they live in .env.
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL UNIQUE,
            broker      TEXT    NOT NULL DEFAULT 'tradovate',
            environment TEXT    NOT NULL DEFAULT 'demo',   -- 'demo' | 'live'
            notes       TEXT,
            is_active   INTEGER NOT NULL DEFAULT 1,        -- 0 = archived
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """,

        # ── Signals ──────────────────────────────────────────────────────────
        # Every TradingView webhook received is logged here, whether approved
        # (executed by TV on Tradovate) or rejected by our circuit breakers.
        """
        CREATE TABLE IF NOT EXISTS signals (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id          INTEGER NOT NULL REFERENCES accounts(id),
            symbol              TEXT    NOT NULL,
            action              TEXT    NOT NULL,  -- 'buy' | 'sell' | 'flat'
            contracts           INTEGER,           -- from Pine Script alert
            price               REAL,
            stop_price          REAL,
            take_profit         REAL,
            strategy_name       TEXT,
            regime              TEXT,              -- 'LOW_VOL' | 'MID_VOL' | 'HIGH_VOL'
            strategy_equity     REAL,              -- {{strategy.equity}} from TV
            strategy_pnl        REAL,              -- {{strategy.netprofit}} from TV
            position_size       REAL,              -- {{strategy.position_size}} from TV
            approved            INTEGER NOT NULL DEFAULT 1,  -- 1 = executed, 0 = blocked
            rejection_reason    TEXT,
            raw_payload         TEXT,              -- full JSON for audit
            received_at         TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """,

        # ── Trades ───────────────────────────────────────────────────────────
        # Closed positions reconstructed from paired buy/sell signals.
        # A trade row is written when a closing signal arrives.
        """
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id      INTEGER NOT NULL REFERENCES accounts(id),
            symbol          TEXT    NOT NULL,
            direction       TEXT    NOT NULL,  -- 'long' | 'short'
            contracts       INTEGER NOT NULL,
            entry_price     REAL    NOT NULL,
            exit_price      REAL,              -- NULL while position is open
            entry_signal_id INTEGER REFERENCES signals(id),
            exit_signal_id  INTEGER REFERENCES signals(id),
            strategy_name   TEXT,
            regime_at_entry TEXT,
            point_value     REAL    NOT NULL DEFAULT 20.0,  -- $/point for P&L calc
            pnl             REAL,              -- (exit-entry) * contracts * point_value
            pnl_pct         REAL,              -- pnl / (entry * contracts * point_value)
            opened_at       TEXT    NOT NULL,
            closed_at       TEXT,
            duration_mins   INTEGER            -- closed_at - opened_at in minutes
        )
        """,

        # ── Equity Snapshots ─────────────────────────────────────────────────
        # NAV recorded from {{strategy.equity}} in each TV alert.
        # Powers the equity curve chart.
        """
        CREATE TABLE IF NOT EXISTS equity_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id  INTEGER NOT NULL REFERENCES accounts(id),
            equity      REAL    NOT NULL,
            cash        REAL,
            pnl         REAL,
            regime      TEXT,
            recorded_at TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """,

        # ── Strategies ───────────────────────────────────────────────────────
        # Registered Pine Script strategy names and their enabled/disabled state.
        """
        CREATE TABLE IF NOT EXISTS strategies (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL UNIQUE,
            description TEXT,
            is_enabled  INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            last_signal TEXT
        )
        """,

        # ── Indexes ──────────────────────────────────────────────────────────
        "CREATE INDEX IF NOT EXISTS idx_signals_account    ON signals(account_id, received_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_signals_symbol     ON signals(symbol, received_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_trades_account     ON trades(account_id, opened_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_equity_account     ON equity_snapshots(account_id, recorded_at DESC)",

        # ── Schema version tracking ───────────────────────────────────────────
        "CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER PRIMARY KEY)",
        "INSERT OR IGNORE INTO _schema_version VALUES (1)",
    ]),
]


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------


class Database:
    """Thin wrapper around a sqlite3 connection with migration support.

    Thread safety: sqlite3 connections are NOT thread-safe by default.
    FastAPI runs handlers in a threadpool, so we use
    check_same_thread=False and rely on the GIL for serialisation.
    For a production system, swap to a connection pool (e.g. aiosqlite).
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._conn = sqlite3.connect(
            str(path),
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")   # better concurrent reads
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        logger.info("Database ready at %s", path)

    # ── Query helpers ─────────────────────────────────────────────────────────

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def executemany(self, sql: str, params_seq: list) -> sqlite3.Cursor:
        return self._conn.executemany(sql, params_seq)

    def fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    def fetchone(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchone()

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── Migrations ────────────────────────────────────────────────────────────

    def _migrate(self) -> None:
        """Apply any pending schema migrations in version order."""
        # Check what version we're at (0 if table doesn't exist yet)
        try:
            row = self._conn.execute(
                "SELECT MAX(version) FROM _schema_version"
            ).fetchone()
            current = row[0] if row and row[0] else 0
        except sqlite3.OperationalError:
            current = 0

        for version, statements in _MIGRATIONS:
            if version <= current:
                continue
            logger.info("Applying DB migration v%d", version)
            for stmt in statements:
                self._conn.execute(stmt)
            self._conn.commit()
            logger.info("Migration v%d applied", version)


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


def get_db() -> Database:
    """Return the process-wide Database singleton, creating it if needed."""
    global _instance
    if _instance is None:
        path_str = os.getenv("DB_PATH", str(_DEFAULT_DB_PATH))
        _instance = Database(Path(path_str))
    return _instance
