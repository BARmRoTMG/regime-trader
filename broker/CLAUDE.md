# broker/ — Alpaca Integration & Order Execution

Wraps the Alpaca API for data fetching, order submission, and position reconciliation. Also contains a **separate** TradingView webhook server that gates orders through the HMM risk pipeline before sending to Alpaca.

> **Two webhook servers exist in this project:**
> - `api/routes/webhook.py` — monitoring-only; logs TV alerts to SQLite, no orders sent to Alpaca
> - `broker/tv_webhook.py` — execution webhook; validates → risk-checks → places Alpaca orders

## Files

### alpaca_client.py
Centralised Alpaca API gateway. All other broker files import from here; no other file imports `alpaca-py` directly.

**`AlpacaConfig`:** `api_key`, `secret_key`, `paper: bool = True`

**`AlpacaClient` key methods:**

| Method | Notes |
|--------|-------|
| `connect()` | Initialises `TradingClient` + `StockHistoricalDataClient` |
| `disconnect()` | Closes WebSocket stream gracefully |
| `get_account()` | Account cash, buying power, equity |
| `get_positions()` / `get_position(symbol)` | Live positions |
| `get_bars(symbols, timeframe, start, end, limit)` | Returns MultiIndex DataFrame (symbol, timestamp) |
| `get_latest_bars()` / `get_latest_price()` | Current prices |
| `subscribe_bars()` / `subscribe_quotes()` | Real-time WebSocket callbacks |
| `_retry(fn, retries=3, backoff=1.0)` | Exponential backoff on transient errors |

Timeframe strings: `"1Min"`, `"5Min"`, `"15Min"`, `"1Hour"`, `"4Hour"`, `"1Day"`, `"1Week"`, `"1Month"`.

### order_executor.py
Translates `TradeSignal` objects into Alpaca orders and tracks their lifecycle.

**Batch execution rule: sells before buys** (`_sort_signals()`) — frees cash before spending it.

**`OrderStatus` enum:** `pending`, `submitted`, `filled`, `partially_filled`, `cancelled`, `rejected`, `failed`

**`OrderRecord`:** Tracks `id`, `symbol`, `side`, `shares`, `order_type`, `submitted_at`, `status`, `filled_qty`, `filled_avg_price`, `alpaca_order` object.

**Key methods:**
- `execute_signals(signals)` — batch: sort → pre-flight check → submit each
- `place_market_order(symbol, side, shares, tif, stop_loss, take_profit)` — bracket order with stops
- `place_limit_order(symbol, side, shares, limit_price, tif)`
- `place_tv_order()` — entry point from `tv_webhook.py`
- `wait_for_fill(order_id, timeout_seconds)` — blocking poll
- `cancel_all_open_orders()`
- `_pre_flight_check()` — validates vs halt lock file + share count > 0

**Dry-run mode:** Set `DRY_RUN=true` env var. Orders are logged but not submitted to Alpaca. Use for integration testing.

### position_tracker.py
Maintains in-process view of open positions; reconciles with Alpaca REST on demand.

**`PositionRecord`:** `symbol`, `shares`, `avg_entry_price`, `current_price`, `market_value`, `unrealised_pnl`, `unrealised_pnl_pct`, `side`, `opened_at`, `last_updated`

**`PortfolioSnapshot`:** `nav`, `cash`, `gross_exposure`, `net_exposure`, `unrealised_pnl`, `daily_pnl`, positions list, weights dict

**Key methods:**
- `snapshot()` → `PortfolioSnapshot` — current portfolio state
- `on_fill(symbol, side, shares, fill_price, filled_at)` — update on fill event (call after each order fill)
- `update_prices(prices: dict)` — mark-to-market
- `reconcile()` — pull live positions from Alpaca REST, sync internal `_positions` dict
- `reset_daily(current_nav)` — set `day_open_nav` for intraday P&L tracking
- `_update_position_on_buy()` — open new or average-up existing
- `_update_position_on_sell()` — reduce or close; computes realised P&L

### tv_adapter.py
Normalises TradingView webhook payload and converts to project `Signal` dataclass.

**`parse_payload(payload)`** — validates required fields (`symbol`, `action`, `price`); returns `TVPayload`.

**`_normalise_symbol(raw)`** — strips exchange prefix: `"NASDAQ:AAPL"` → `"AAPL"`, `"NYSE:IBM"` → `"IBM"`, `"BINANCE:BTCUSDT"` → `"BTCUSDT"`.

**`to_signal(tv_payload, regime_id, regime_name, ...)`** — builds `Signal` with defaults:
- Stop = 0 → `entry_price × 0.97`
- Take-profit = 0 → `entry + 2 × (entry − stop)` (2R target)
- Direction: `LONG` if action=`"buy"`, `FLAT` if `"sell"` / `"close"`

### tv_webhook.py
FastAPI app for Alpaca-execution webhook. Separate from `api/server.py`.

**Endpoints:**
| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/alert` | Main TV receiver: validate secret → parse → risk-check → execute |
| `GET` | `/health` | `status`, `timestamp`, `alpaca_connected` |
| `GET` | `/status` | Full `PortfolioSnapshot` as JSON |
| `POST` | `/manual` | Dev-only test signal, no secret required |

**Processing flow for `POST /alert`:**
1. Parse body via `tv_adapter.parse_payload()`
2. Convert to `Signal` via `tv_adapter.to_signal()` (with current regime from HMM)
3. Gate through `RiskManager.validate_signal(signal, portfolio_snapshot)`
4. If approved → `OrderExecutor.place_tv_order()`
5. Background: `PositionTracker.reconcile()` after fill

**Auth:** Shared secret from `X-TV-Secret` header or `"secret"` field in JSON body.

**Global singletons** (initialised in lifespan): `_alpaca`, `_tracker`, `_risk_mgr`, `_executor`

**Env vars:** `DRY_RUN` (skip Alpaca submission), `WEBHOOK_SECRET`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER`

## Cross-module connections

| Direction | Module | What |
|-----------|--------|------|
| Imports from | `core/risk_manager.py` | `RiskManager.validate_signal()` |
| Imports from | `core/signal_generator.py` | `TradeSignal`, `SignalPacket` |
| Imports from | `data/market_data.py` | `MarketDataFeed` (for live price feed) |
| Exports fills to | `broker/position_tracker.py` | `on_fill()` after each Alpaca order |

## How to test

```bash
pytest tests/test_orders.py -v   # OrderExecutor logic (dry-run mode)
```

Manual dry-run:
```bash
DRY_RUN=true uvicorn broker.tv_webhook:app --port 8001
```

## Sync rules

- **Add an endpoint to `tv_webhook.py`** → add a row to the endpoints table above.
- **Change secret header name** → update the Auth note above.
- **Change `_normalise_symbol` logic** → update the symbol normalisation note in `tv_adapter.py` section.
- **Change order batching logic** → update the "sells before buys" rule above.
- **Implement `market_data.py`** → `position_tracker.history()` and streaming flows will change; update this file.
