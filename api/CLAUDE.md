# api/ — FastAPI Server

HTTP and WebSocket server for the TradingView → Tradovate monitoring dashboard. Receives Pine Script webhook alerts, persists them, and streams live updates to the React frontend.

## Files

### server.py
FastAPI app factory. Registers all routers, CORS middleware, WebSocket keepalive, and (in production) serves the React SPA from `frontend/dist/`.

- **CORS:** allows `localhost:5173` (Vite dev) and `localhost:8000` (production)
- **Lifespan:** `get_db()` called on startup (applies migrations); `_keepalive_loop` task started
- **Static serving:** `frontend/dist/` mounted at `/` only if the directory exists (so dev mode without a build still works)
- **Docs:** Swagger at `/api/docs`, ReDoc at `/api/redoc`

### ws.py
In-process WebSocket manager. Holds a global `_connections: list[WebSocket]`.

- `broadcast(data: dict)` — serialises to JSON and sends to every connected client; removes dead connections silently
- `_keepalive_loop()` — pings all clients every 30 s with `{"type": "ping"}`
- Client messages are received but ignored (connection kept alive via `receive_text()`)

**Message types sent by the server:**
| Type | Trigger | Payload |
|------|---------|---------|
| `signal` | New webhook alert received | `account_id`, `signal_id`, `payload` (secret stripped) |
| `equity` | (reserved, not yet used) | — |
| `ping` | Every 30 s | — |

### deps.py
Single FastAPI dependency: `get_database()` returns `get_db()` singleton. All route functions declare `db: Database = Depends(get_database)`.

### routes/webhook.py
**The only inbound path from TradingView.** `POST /webhook/alert`

Processing order:
1. Parse JSON body
2. Validate `payload["secret"]` against `WEBHOOK_SECRET` env var (401 if mismatch)
3. Resolve account by name; auto-create if not found
4. Insert signal row (approved or blocked)
5. Insert equity snapshot if `strategy_equity` present
6. Reconstruct trade open/close:
   - `buy` + no open trade → open long
   - `sell` + open trade → close it (compute P&L)
   - `sell` + no open trade → open short
7. Background tasks: broadcast to WebSocket + check circuit breakers → Slack

**Field aliases:** Accepts both `strategy_equity`/`strategy_pnl` (Pine Script) and `equity`/`netprofit` (manual test payloads).

**Circuit-breaker thresholds** (`pnl / equity` ratio):
| Threshold | Label |
|-----------|-------|
| ≤ −10% | PEAK_HALT |
| ≤ −7% | WEEKLY_HALT |
| ≤ −5% | WEEKLY_REDUCE |
| ≤ −3% | DAILY_HALT |
| ≤ −2% | DAILY_REDUCE |

These must match `routes/portfolio.py` and `core/CLAUDE.md`.

**Response schema:** `WebhookResponse { status, signal_id, trade_action, message }`  
`trade_action` is one of `"opened"`, `"closed"`, `"ignored"`.

### routes/portfolio.py
`GET /api/portfolio/{account_id}` — powers the Dashboard top bar.

Returns: latest equity, session P&L, current regime, open positions list with estimated unrealised P&L, and circuit-breaker status string (computed from `strategy_pnl / strategy_equity` of the latest signal).

### routes/trades.py
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/trades/{id}` | Paginated closed-trade list (filters: symbol, open_only, closed_only) |
| `GET` | `/api/trades/{id}/summary` | Aggregate stats (win_rate, total_pnl, avg_winner, avg_loser, max_win, max_loss) |
| `GET` | `/api/trades/{id}/equity` | Time-series equity curve (max 500 points) |

### routes/signals.py
`GET /api/signals/{account_id}` — live signal feed, newest first. Query params: `symbol`, `limit` (default 50, max 500), `offset`.

### routes/accounts.py
Full CRUD for account profiles.
| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/api/accounts` | Active only by default |
| `POST` | `/api/accounts` | Body: name, broker, environment |
| `GET` | `/api/accounts/{id}` | Single account |
| `PATCH` | `/api/accounts/{id}` | name, environment, notes, is_active |
| `DELETE` | `/api/accounts/{id}` | Soft-delete (`is_active = 0`) |

### routes/strategies.py
| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/api/strategies` | All registered strategies + enabled state |
| `PATCH` | `/api/strategies/{name}` | Toggle `is_enabled` or update `description` |

## Environment variables

| Var | Default | Purpose |
|-----|---------|---------|
| `WEBHOOK_SECRET` | `""` | Validates TradingView alerts; empty = no validation |
| `SLACK_WEBHOOK_URL` | `""` | Circuit-breaker Slack notifications; empty = disabled |
| `DB_PATH` | `data/regime_trader.db` | SQLite path |
| `WEBHOOK_PORT` | `8000` | Port for `__main__` entry point |

## Cross-module connections

- **Imports:** `db/database.py` (get_db), `db/queries.py` (all CRUD)
- **Serves:** React frontend at `http://localhost:8000` (production) or proxied via Vite at `localhost:5173` (dev)
- **No dependency** on `core/`, `broker/`, `backtest/`, or `data/` — the API layer is purely a logging/monitoring layer

## Sync rules

- **Add a new endpoint** → add a row to the relevant endpoint table above.
- **Change circuit-breaker thresholds** → update the thresholds table here AND in `core/CLAUDE.md`.
- **Change webhook payload fields** → update the field description in `routes/webhook.py` section and in `pinescript/CLAUDE.md`.
- **Add a WebSocket message type** → update the message types table in `ws.py` section.
- **Change CORS origins** → note the change in the `server.py` section.
