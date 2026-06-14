# Regime Trader

A full-stack trading dashboard that receives TradingView Pine Script webhook alerts, logs them to SQLite, and displays live equity, signals, open positions, and trade history in a dark React dashboard.

**Architecture:**
```
TradingView Pine Script strategy
    → webhook alert (JSON)
    → POST /webhook/alert  (FastAPI)
    → SQLite (signals, trades, equity snapshots)
    → WebSocket push
    → React dashboard (live updates)
```

No Tradovate API is called directly — TradingView handles order execution via its native Tradovate broker integration. This server is purely a monitoring and logging layer.

---

## Project structure

```
regime-trader/
├── api/                  # FastAPI server
│   ├── server.py         # App factory, CORS, static file serving
│   ├── ws.py             # WebSocket manager (broadcast to dashboard)
│   ├── deps.py           # FastAPI dependencies
│   └── routes/
│       ├── accounts.py   # GET/POST/PATCH/DELETE /api/accounts
│       ├── portfolio.py  # GET /api/portfolio/{id}  (live snapshot)
│       ├── trades.py     # GET /api/trades/{id}     (history + equity curve)
│       ├── signals.py    # GET /api/signals/{id}    (signal log)
│       ├── strategies.py # GET/PATCH /api/strategies
│       └── webhook.py    # POST /webhook/alert      (TradingView receiver)
├── db/
│   ├── database.py       # SQLite connection + auto-migrations
│   ├── models.py         # Dataclass models
│   └── queries.py        # All CRUD helpers + P&L calculation
├── frontend/             # Vite + React + TypeScript dashboard
│   └── src/
│       ├── pages/
│       │   ├── Dashboard.tsx   # Equity curve, positions, signal feed
│       │   ├── Trades.tsx      # Paginated trade history + stats
│       │   ├── Strategies.tsx  # Enable/disable Pine strategies
│       │   └── Settings.tsx    # Account CRUD, webhook URL
│       ├── lib/
│       │   ├── api.ts          # Typed axios wrappers for all endpoints
│       │   └── context.ts      # Account selector context
│       └── hooks/
│           └── useWebSocket.ts # Auto-reconnecting WebSocket hook
├── pinescript/
│   └── regime_trader.pine      # Pine Script v5 strategy (paste into TradingView)
├── .env.example
└── requirements.txt
```

---

## Quick start

### 1 — Clone and create the Python virtual environment

```bash
git clone https://github.com/BARmRoTMG/regime-trader.git
cd regime-trader
python -m venv .venv
```

### 2 — Install Python dependencies

```bash
# Windows
.venv\Scripts\activate
pip install -r requirements.txt
```

### 3 — Create your `.env` file

```bash
copy .env.example .env   # Windows
# cp .env.example .env   # Mac/Linux
```

Open `.env` and set:

```env
WEBHOOK_SECRET=any-random-secret-string
# Optional — Slack DM on circuit-breaker trigger:
# SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

> The `WEBHOOK_SECRET` must match the **Webhook Secret** input in the TradingView Pine Script settings.

### 4 — Start the backend

```bash
# With venv activated, from the regime-trader folder:
uvicorn api.server:app --host 0.0.0.0 --port 8000 --reload
```

The first run auto-creates `data/regime_trader.db` and applies all schema migrations. You should see:

```
INFO  Database ready at ...data/regime_trader.db
INFO  regime-trader API server started
```

API docs are available at **http://localhost:8000/api/docs**

### 5 — Start the frontend (development)

Open a second terminal:

```bash
cd frontend
npm install       # first time only
npm run dev
```

Open **http://localhost:5173** — the Vite dev server proxies all `/api/*` and `/ws` calls to the backend on `:8000`.

---

## Testing without TradingView

Send a fake webhook alert using PowerShell to verify the full pipeline:

```powershell
# BUY signal
$body = @{
    account       = "Demo NQ"
    symbol        = "NQM5"
    action        = "buy"
    contracts     = 2
    price         = 19420.0
    stop          = 19250.0
    take_profit   = 19700.0
    strategy      = "regime_trader_v1"
    regime        = "LOW_VOL"
    equity        = 102000.0
    netprofit     = 2000.0
    position_size = 2
    secret        = "any-random-secret-string"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://localhost:8000/webhook/alert" `
    -Method POST -Body $body -ContentType "application/json"
```

The server auto-creates the `Demo NQ` account on the first alert. Send a second request with `"action": "sell"` to close the trade — it will then appear in Past Trades with a calculated P&L.

---

## Connecting TradingView

TradingView only delivers webhooks to **publicly reachable HTTPS URLs**. For local development, use ngrok.

### Expose port 8000 with ngrok

```bash
ngrok http 8000
```

Copy the `https://xxxx.ngrok-free.app` URL.

### Add the Pine Script strategy to TradingView

1. Open TradingView → chart for the symbol you trade (e.g. `NQ1!`)
2. Open the Pine Script Editor → paste the full contents of `pinescript/regime_trader.pine`
3. Click **Add to chart** — the strategy tester panel will appear
4. In the **Settings → Inputs** tab, fill in:

| Input | Value |
|-------|-------|
| Webhook Secret | same value as `WEBHOOK_SECRET` in `.env` |
| Strategy Name | `regime_trader_v1` (or any name you want) |
| Account Name | `Demo NQ` (must match the account name in the dashboard) |

### Create the TradingView alert

1. Click the **Alerts** (clock) icon → **Create Alert**
2. **Condition**: `Regime Trader` → `Any alert() call`
3. **Webhook URL**: `https://xxxx.ngrok-free.app/webhook/alert`
4. **Message**: leave blank (the `alert()` calls in the script build the JSON)
5. **Expiry**: set as long as possible
6. **Trigger**: `Once Per Bar Close`

> TradingView **Pro+** or higher is required for webhook delivery. Free plans cannot send webhooks.

### Create the account in the dashboard

Go to **Settings** in the sidebar → **Add Account** → set the name to exactly `Demo NQ` (matching the Pine Script input). Or skip this — the server auto-creates the account on the first webhook it receives.

---

## What each page shows

| Page | Content |
|------|---------|
| **Dashboard** | Live equity, session P&L, regime badge, circuit-breaker status, equity curve chart, signal feed, open positions table |
| **Past Trades** | Paginated closed-trade history with win/loss filter, win rate, avg winner/loser |
| **Strategies** | List of registered Pine strategies with enable/disable toggles |
| **Settings** | Account CRUD, webhook URL copy button, risk threshold reference |

---

## Circuit breakers

The server computes a circuit-breaker level from each alert's `equity` / `netprofit` fields:

| Level | Trigger |
|-------|---------|
| Daily Reduce | Session P&L ≤ −2% |
| Daily Halt | Session P&L ≤ −3% |
| Weekly Reduce | Session P&L ≤ −5% |
| Weekly Halt | Session P&L ≤ −7% |
| Peak Halt | Session P&L ≤ −10% |

These are displayed on the Dashboard and (if `SLACK_WEBHOOK_URL` is set) sent as a Slack notification.

---

## Production build

To serve the React app from FastAPI itself (single binary, no Vite needed):

```bash
cd frontend
npm run build          # outputs to frontend/dist/
cd ..
uvicorn api.server:app --host 0.0.0.0 --port 8000
```

FastAPI serves the built `frontend/dist/` at `/` automatically when the folder exists.

---

## API reference

Full interactive docs at `http://localhost:8000/api/docs` (Swagger UI).

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/webhook/alert` | Receive TradingView alert |
| `GET` | `/api/accounts` | List accounts |
| `POST` | `/api/accounts` | Create account |
| `PATCH` | `/api/accounts/{id}` | Update account |
| `DELETE` | `/api/accounts/{id}` | Delete account |
| `GET` | `/api/portfolio/{id}` | Live equity snapshot + open positions |
| `GET` | `/api/trades/{id}` | Trade history (paginated) |
| `GET` | `/api/trades/{id}/summary` | Win rate, avg P&L stats |
| `GET` | `/api/trades/{id}/equity` | Equity curve time-series |
| `GET` | `/api/signals/{id}` | Signal log |
| `GET` | `/api/strategies` | Strategy list |
| `PATCH` | `/api/strategies/{name}` | Enable / disable strategy |
| `WS` | `/ws` | WebSocket live push |

---

## Security notes

- Never commit `.env` or `config/credentials.yaml` — both are git-ignored.
- Set a strong random `WEBHOOK_SECRET` and keep it only in `.env` and TradingView Inputs.
- The server validates the secret on every incoming webhook and returns `401` if it doesn't match.
