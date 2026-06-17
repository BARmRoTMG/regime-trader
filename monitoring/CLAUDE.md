# monitoring/ — Alerts, Terminal TUI, Structured Logging

Observability layer for the live HMM trading engine. Used by `main.py` during live trading — not used by the TradingView webhook dashboard in `api/`.

## Files

### alerts.py
Multi-channel alert dispatcher with per-type rate-limiting.

**`AlertSeverity` enum:** `INFO`, `WARNING`, `CRITICAL`

**`AlertConfig`:** SMTP credentials, Slack webhook URL, Discord webhook URL, `rate_limit_minutes` per alert type.

**`Alert` dataclass:** `alert_type`, `severity`, `subject`, `body`, `metadata: dict`, `timestamp`

**`AlertManager` key methods:**

| Method | Purpose |
|--------|---------|
| `send(alert)` | Route to all configured channels, respecting rate limit |
| `regime_change(old, new, confidence)` | Factory for regime-transition alert |
| `drawdown_halt(level, pnl_pct, equity)` | Factory for circuit-breaker trigger |
| `order_error(symbol, side, error)` | Factory for order submission failure |
| `system_error(component, error)` | Factory for infrastructure errors |
| `large_fill(symbol, shares, fill_price, notional)` | Factory for unexpectedly large fills |
| `_send_email()` | SMTP delivery |
| `_send_slack()` | Slack webhook POST |
| `_send_discord()` | Discord webhook POST |
| `_is_rate_limited(alert_type)` | Check per-type rate limit |
| `_record_sent(alert_type)` | Update last-sent timestamp |

Rate limiting prevents alert storms during volatile periods. Default: 15 minutes per alert type (set via `AlertConfig.rate_limit_minutes`).

### dashboard.py
Rich-based terminal TUI for live monitoring during algo trading sessions.

**`Dashboard`** renders a live layout with:
- **Regime panel** — current regime label with colour (green = low_vol, yellow = mid_vol, red = high_vol)
- **Portfolio panel** — NAV, cash, gross exposure, daily P&L
- **Positions table** — open positions with live P&L
- **Log tail** — last N log lines

**Key methods:**
- `start()` — launch background refresh thread
- `stop()` — cancel refresh loop, restore terminal
- `update(regime_state, portfolio_snapshot, signal_packet)` — push new data to layout
- `push_log(message)` — append to log tail

Refresh interval: 5 seconds (set in `config/settings.yaml` → `monitoring.dashboard_refresh_seconds`).

### logger.py
Structured JSON logging for log aggregators (Datadog, CloudWatch, Loki, etc.).

**`JsonFormatter`** — serialises each `LogRecord` to a single-line JSON object with standard fields (`timestamp`, `level`, `logger`, `message`) plus any `extra` fields passed at log-call sites.

**`StructuredLogger`** — subclass of Python's `Logger` with domain-specific convenience methods:
- `.event(event_type, **kwargs)` — generic structured event
- `.trade(symbol, side, shares, price, **kwargs)` — trade execution log
- `.regime_change(old_regime, new_regime, confidence)` — regime transition
- `.risk_alert(level, pnl_pct, equity)` — circuit-breaker event

**`setup_logging(log_file, level)`** — configures root logger with:
- Rich console handler (human-readable, coloured)
- JSON file handler (machine-readable, one JSON object per line)

**`get_logger(name)`** → `StructuredLogger` — use instead of `logging.getLogger()` in all modules.

## Cross-module connections

| Direction | Module | What |
|-----------|--------|------|
| Imports from | `core/hmm_engine.py` | `RegimeState` (for Dashboard and AlertManager) |
| Imports from | `broker/position_tracker.py` | `PortfolioSnapshot` (for Dashboard) |
| Imports from | `core/signal_generator.py` | `SignalPacket` (for Dashboard) |
| Called by | `main.py` | `setup_logging()`, `AlertManager`, `Dashboard` |

## How to test

No dedicated test file. Test manually by running:
```bash
python main.py --mode live --log-level DEBUG
```
Or inject a test alert:
```python
from monitoring.alerts import AlertManager, AlertConfig, Alert, AlertSeverity
mgr = AlertManager(AlertConfig())
mgr.send(Alert("test", AlertSeverity.INFO, "Test", "Hello"))
```

## Sync rules

- **Add a new alert type** → add a factory method to `AlertManager` and update the key methods table above.
- **Add a new delivery channel** → add a `_send_*` method and update the channel list.
- **Add a new Dashboard panel** → update the renders section above.
- **Change `dashboard_refresh_seconds`** → note is configured in `config/settings.yaml`, not hardcoded.
