# db/ — Database Layer

SQLite persistence for all webhook-dashboard data: accounts, signals, trades, equity snapshots, and strategy registry.

## Files

### database.py
Thin `Database` wrapper around `sqlite3.Connection` with version-controlled auto-migrations.

- `Database.__init__`: Opens connection with WAL journal mode and `PRAGMA foreign_keys=ON`, then calls `_migrate()`.
- `Database._migrate()`: Iterates `_MIGRATIONS` list; applies any version > current `_schema_version`. **Never edit an existing migration tuple — only append new ones.**
- `get_db()`: Returns the process-wide singleton. FastAPI routes call this via `api/deps.py`.

**Thread safety:** Uses `check_same_thread=False` and relies on the GIL. Sufficient for the single-process FastAPI server but not production-grade. Swap to `aiosqlite` + connection pool if concurrency grows.

### models.py
Plain Python dataclasses, one per table. No ORM.

| Dataclass | Table | Notable fields |
|-----------|-------|----------------|
| `Account` | `accounts` | `is_active` (soft-delete flag) |
| `Signal` | `signals` | `approved`, `rejection_reason`, `raw_payload` (full JSON) |
| `Trade` | `trades` | `exit_price` is NULL while open; `point_value` set at open time |
| `EquitySnapshot` | `equity_snapshots` | Powers equity curve chart |
| `Strategy` | `strategies` | `is_enabled` toggle, `last_signal` timestamp |

`Trade` has two computed properties: `is_open` (`exit_price is None`) and `is_winner` (`pnl > 0`).

### queries.py
All CRUD helpers. Every function takes a `Database` argument for testability (pass an in-memory DB in tests).

**Naming convention:** `list_*` → `list[Model]`, `get_*` → `Optional[Model]`, `insert_*` → new row id, `update_*`/`delete_*` → rows affected.

Key functions worth knowing:

| Function | Notes |
|----------|-------|
| `delete_account()` | Soft-delete only — sets `is_active = 0`, never hard-deletes |
| `insert_signal()` | Auto-calls `_ensure_strategy()` to register new strategy names |
| `open_trade()` | Looks up `point_value_for(symbol)` from `POINT_VALUES` dict |
| `close_trade()` | Computes `pnl`, `pnl_pct`, `duration_mins` at close time |
| `get_open_trade()` | Returns most-recent unclosed trade for account+symbol |
| `trade_summary()` | Aggregate stats (win_rate, avg_winner, max_loss) for Trades page |
| `_ensure_strategy()` | `INSERT OR IGNORE` + update `last_signal` timestamp |

**`POINT_VALUES` dict** — keyed by futures root symbol (strip month/year suffix first):
```
ES=50.0, NQ=20.0, RTY=50.0, YM=5.0, CL=1000.0, GC=100.0,
SI=5000.0, ZB=1000.0, ZN=1000.0, 6E=1250.0, 6B=6250.0
```
Unknown symbols default to 20.0 (NQ-like) with a warning log.

## Schema (Migration v1)

```
accounts         id, name UNIQUE, broker, environment, notes, is_active, created_at
signals          id, account_id→accounts, symbol, action, contracts, price, stop_price,
                 take_profit, strategy_name, regime, strategy_equity, strategy_pnl,
                 position_size, approved, rejection_reason, raw_payload, received_at
trades           id, account_id→accounts, symbol, direction, contracts, entry_price,
                 exit_price, entry_signal_id→signals, exit_signal_id→signals,
                 strategy_name, regime_at_entry, point_value, pnl, pnl_pct,
                 opened_at, closed_at, duration_mins
equity_snapshots id, account_id→accounts, equity, cash, pnl, regime, recorded_at
strategies       id, name UNIQUE, description, is_enabled, created_at, last_signal
_schema_version  version (PK)
```

Indexes: `(account_id, received_at DESC)` on signals; `(account_id, opened_at DESC)` on trades; `(account_id, recorded_at DESC)` on equity_snapshots; `(symbol, received_at DESC)` on signals.

## Cross-module connections

- **Consumed by:** `api/deps.py` (dependency injection), `api/routes/*.py` (all CRUD), `api/routes/webhook.py` (insert signal/trade/equity)
- **DB path:** `data/regime_trader.db` by default; override with `DB_PATH` env var

## Sync rules

- **New table or column** → append a new migration tuple `(version, [...sql...])` to `_MIGRATIONS` in `database.py`; add the field to the relevant dataclass in `models.py`; add a `_row_to_*` converter entry in `queries.py`; document the schema change in this file.
- **New query function** → add to the naming-convention table above.
- **New symbol** → add to `POINT_VALUES` dict and update the table in this file.
- **Schema version** changes → update the schema section above.
