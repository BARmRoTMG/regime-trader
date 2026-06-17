# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**Regime Trader** is a dual-purpose system:

1. **TradingView → Tradovate monitoring layer** — receives Pine Script webhook alerts, persists them to SQLite, and streams live updates to a React dashboard. TradingView itself executes orders via its native Tradovate integration; this server only logs and displays.

2. **HMM-based algo trading engine** — a Hidden Markov Model volatility classifier (`core/`) that drives an Alpaca-connected live/paper trading system (`broker/`). This runs separately from the webhook dashboard via `main.py`.

## Development commands

### Backend (FastAPI)
```bash
# Activate venv first (Windows)
.venv\Scripts\activate

# Run dev server (auto-reloads, creates DB on first run)
uvicorn api.server:app --host 0.0.0.0 --port 8000 --reload

# Run with explicit port
python main.py --mode live
python main.py --mode backtest
python main.py --mode stress
python main.py --mode live --symbols SPY --log-level DEBUG
```

### Frontend (Vite + React)
```bash
cd frontend
npm install          # first time only
npm run dev          # dev server at :5173, proxies /api/* /webhook/* /ws → :8000
npm run build        # outputs frontend/dist/ — FastAPI auto-serves this in production
npm run lint         # ESLint
```

### Tests
```bash
pytest                        # all tests
pytest tests/test_hmm.py      # single file
pytest tests/test_risk.py -v  # verbose
```

### Manual webhook test (PowerShell)
```powershell
$body = @{
    account="Demo NQ"; symbol="NQM5"; action="buy"; contracts=2
    price=19420.0; stop=19250.0; take_profit=19700.0
    strategy="regime_trader_v1"; regime="LOW_VOL"
    strategy_equity=102000.0; strategy_pnl=2000.0; position_size=2
    secret="any-random-secret-string"
} | ConvertTo-Json
Invoke-RestMethod -Uri "http://localhost:8000/webhook/alert" -Method POST -Body $body -ContentType "application/json"
```

## Environment setup

Copy `.env.example` to `.env`. Required variables:
- `WEBHOOK_SECRET` — must match the Pine Script "Webhook Secret" input in TradingView
- `SLACK_WEBHOOK_URL` — optional, Slack DM on circuit-breaker trigger
- `DB_PATH` — optional, defaults to `data/regime_trader.db`
- `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER` — for the HMM live-trading path only

## Architecture

### Webhook → dashboard flow
```
TradingView Pine Script
  → POST /webhook/alert
  → webhook.py: validate secret, resolve/auto-create account, insert signal,
                reconstruct trade open/close, insert equity snapshot
  → SQLite (signals, trades, equity_snapshots)
  → broadcast() via WebSocket to all dashboard tabs
```

### HMM trading engine flow (main.py)
```
MarketDataFeed (Alpaca) → FeatureEngineer → HMMEngine.predict_latest()
  → RegimeStrategy (Low/Mid/High vol) → SignalGenerator
  → RiskManager (circuit breakers, position sizing) → OrderExecutor (Alpaca)
  → PositionTracker
```

### Database layer (`db/`)
- `database.py`: Singleton `Database` wrapper around `sqlite3`. Migrations are versioned tuples in `_MIGRATIONS` — append new versions, never edit existing ones. Applied automatically on startup.
- `queries.py`: All CRUD. Convention: `list_*` returns `list[Model]`, `get_*` returns `Optional[Model]`, `insert_*` returns new row id, `update_*`/`delete_*` return rows affected.
- `models.py`: Plain dataclasses; no ORM.
- Point values for P&L calculation live in `queries.POINT_VALUES` (keyed by futures root symbol, e.g. `"NQ"` → `20.0`).

### API layer (`api/`)
- `server.py`: FastAPI app factory. In production, mounts `frontend/dist/` at `/`. In dev, Vite proxies to `:8000`.
- `ws.py`: In-process WebSocket manager. `broadcast(dict)` sends JSON to all connected clients. A 30-second keepalive ping runs as a background task.
- `deps.py`: FastAPI `Depends` helpers.
- `routes/webhook.py`: The only inbound path from TradingView. Accepts both `strategy_equity`/`strategy_pnl` (Pine Script) and `equity`/`netprofit` (manual test payloads). Auto-creates accounts on first alert. Circuit-breaker thresholds (−2%/−3%/−5%/−7%/−10%) trigger Slack notifications.

### Frontend (`frontend/`)
- React 19 + TypeScript + Vite + Tailwind CSS v4
- State: TanStack Query for REST, `useWebSocket` hook for live WS updates
- `lib/api.ts`: Typed axios wrappers for every endpoint
- `lib/context.ts`: Account selector context shared across pages
- Pages: Dashboard (equity curve via Recharts, positions, signal feed), Trades (paginated history + stats), Strategies (enable/disable toggles), Settings (account CRUD)

### HMM engine (`core/`)
- `hmm_engine.py`: Uses the **forward algorithm only** (not Viterbi) to avoid look-ahead bias. States are sorted by mean log-return; strategy layer sorts by volatility separately.
- `regime_strategies.py`: Volatility rank mapping — bottom third → `LowVolBullStrategy`, top third → `HighVolDefensiveStrategy`, middle → `MidVolCautiousStrategy`.
- `risk_manager.py`: Position sizing, daily/weekly drawdown circuit breakers.
- `signal_generator.py`: Translates HMM regime + strategy output into broker orders.

### Pine Script (`pinescript/regime_trader.pine`)
The strategy is pasted into TradingView. It sends JSON alerts to `/webhook/alert` with fields `account`, `symbol`, `action`, `contracts`, `price`, `stop`, `take_profit`, `strategy`, `regime`, `strategy_equity`, `strategy_pnl`, `position_size`, `secret`. TradingView **Pro+** required for webhook delivery.

## Key invariants

- **No look-ahead**: `HMMEngine` uses only the forward algorithm (`predict_latest` reads `obs_1…obs_t`).
- **Account auto-creation**: The first webhook for an unknown account name auto-creates it — no manual setup required.
- **Soft deletes**: `delete_account` sets `is_active = 0`; rows are never hard-deleted.
- **Trade reconstruction**: Trades are inferred from paired buy/sell signals server-side. A `buy` with no open trade opens a long; a `sell` with an open trade closes it; a `sell` with no open trade opens a short.
- **WebSocket secret stripping**: The `secret` field is removed from the WS broadcast payload before sending to clients.

---

## Sub-module documentation

Each major directory has its own `CLAUDE.md` with file-by-file detail, key invariants, cross-module connections, and per-directory sync rules. **When working in a subdirectory, read its `CLAUDE.md` first.**

| Directory | CLAUDE.md | What it covers |
|-----------|-----------|---------------|
| `api/` | [api/CLAUDE.md](api/CLAUDE.md) | FastAPI routes, WebSocket manager, endpoint table, env vars |
| `backtest/` | [backtest/CLAUDE.md](backtest/CLAUDE.md) | Walk-forward backtester, performance analytics, stress testing |
| `broker/` | [broker/CLAUDE.md](broker/CLAUDE.md) | Alpaca client, order executor, position tracker, TV adapter |
| `core/` | [core/CLAUDE.md](core/CLAUDE.md) | HMM engine, regime strategies, risk manager, signal generator stubs |
| `data/` | [data/CLAUDE.md](data/CLAUDE.md) | Feature engineering pipeline (14 columns), market data feed stub |
| `db/` | [db/CLAUDE.md](db/CLAUDE.md) | SQLite schema, migration convention, query helpers, POINT_VALUES |
| `frontend/` | [frontend/CLAUDE.md](frontend/CLAUDE.md) | Vite/React toolchain, build output, dev proxy config |
| `frontend/src/` | [frontend/src/CLAUDE.md](frontend/src/CLAUDE.md) | Component architecture, state management, API contracts, WS flow |
| `monitoring/` | [monitoring/CLAUDE.md](monitoring/CLAUDE.md) | Alert dispatching, terminal TUI, structured JSON logging |
| `tests/` | [tests/CLAUDE.md](tests/CLAUDE.md) | Test suite structure, fixtures, per-file coverage, run commands |
| `pinescript/` | [pinescript/CLAUDE.md](pinescript/CLAUDE.md) | Pine Script logic, regime detection, full webhook payload schema |

---

## Sync policy

After making any change, update the relevant documentation to keep the hierarchy accurate. These are the specific rules:

| Change | Files to update |
|--------|----------------|
| Add/remove/change an API endpoint | `api/CLAUDE.md` endpoint table |
| Add/change the DB schema | `db/CLAUDE.md` schema section + append new migration version |
| Add a new feature column to the HMM | `data/CLAUDE.md` feature list + `FEATURE_COLUMNS` constant |
| Change circuit-breaker thresholds | `api/CLAUDE.md` CB table + `core/CLAUDE.md` CB table (must match) |
| Change Pine Script webhook payload fields | `pinescript/CLAUDE.md` payload table + `api/CLAUDE.md` webhook section |
| Implement a stub method (`signal_generator.py` or `market_data.py`) | Remove "Status: STUB" note in the relevant sub-CLAUDE.md; add test file note to `tests/CLAUDE.md` |
| Add a new page to the frontend | `frontend/src/CLAUDE.md` pages section + `frontend/CLAUDE.md` source layout |
| Add a new npm dependency | `frontend/CLAUDE.md` toolchain table |
| Add a new stress scenario | `backtest/CLAUDE.md` scenarios table |
| Add a new alert delivery channel | `monitoring/CLAUDE.md` channels section |
| Add futures symbol to POINT_VALUES | `db/CLAUDE.md` POINT_VALUES table |
| Add/change a WebSocket message type | `api/CLAUDE.md` message types table + `frontend/src/CLAUDE.md` WS section |
| Any of the above that also affects high-level arch | Update the relevant section in this root file too |
