# pinescript/ — TradingView Pine Script Strategy

Pine Script v5 strategy pasted into TradingView. Detects volatility regimes in-chart, manages positions, and fires JSON webhook alerts to the monitoring server. TradingView executes orders natively via its Tradovate broker integration — this script does not call Alpaca.

## File

### regime_trader.pine

**Inputs (grouped):**

| Group | Input | Default | Purpose |
|-------|-------|---------|---------|
| Volatility Regime | ATR Length | 14 | ATR period |
| Volatility Regime | Low-Vol ATR/Close % | 0.8% | LOW_VOL threshold |
| Volatility Regime | High-Vol ATR/Close % | 2.0% | HIGH_VOL threshold |
| Volatility Regime | Vol Smooth | 5 | SMA smoothing on atr_ratio |
| Trend Filter | EMA Fast | 20 | Crossover signal |
| Trend Filter | EMA Slow | 50 | Crossover signal |
| Trend Filter | SMA Trend | 200 | Long-term trend filter |
| Trend Filter | RSI Length | 14 | RSI period |
| Trend Filter | RSI Midline | 50 | MID_VOL entry filter |
| Risk | Default Stop % | 3% | Fallback stop distance |
| Risk | Take-Profit R Multiple | 2.0 | TP = entry + 2R |
| Risk | Max Daily Loss % | −3% | Daily halt threshold |
| Webhook | Secret | — | Must match `WEBHOOK_SECRET` in `.env` |
| Webhook | Strategy Name | — | Must match account name in dashboard |
| Webhook | Account Name | — | Must match account in dashboard |

**Regime detection:**
```pine
atr_ratio = ta.sma(ta.atr(atr_len) / close * 100, vol_smooth)
LOW_VOL  = atr_ratio <= low_vol_pct   // default: ≤ 0.8%
HIGH_VOL = atr_ratio >= high_vol_pct  // default: ≥ 2.0%
MID_VOL  = not LOW_VOL and not HIGH_VOL
```

**Entry conditions:**
```
buy_low = LOW_VOL + ta.crossover(ema_fast, ema_slow) + not daily_halted
buy_mid = MID_VOL + ta.crossover(ema_fast, ema_slow) + rsi > 50 + close > sma200 + not daily_halted
buy_sig = buy_low OR buy_mid
```

**Exit conditions:**
```
sell_high = HIGH_VOL + close < ema_slow
sell_cross = ta.crossunder(ema_fast, ema_slow)
sell_sig = sell_high OR sell_cross
```

**Daily loss gate:**
```pine
// On new day: session_open_equity := strategy.equity
daily_pnl_pct = (strategy.equity - session_open_equity) / session_open_equity
daily_halted  = daily_pnl_pct <= max_daily_loss_pct  // default: -3%
```
When `daily_halted` is true, no new entries are taken for the rest of the session. An `X` shape is plotted on the chart.

**Webhook payload fields** sent on each alert:
```json
{
  "account":          "<Account Name input>",
  "symbol":           "{{ticker}}",
  "action":           "buy" | "sell" | "flat",
  "contracts":        {{strategy.position_size}},
  "price":            {{close}},
  "stop":             <stop_price>,
  "take_profit":      <take_profit_price>,
  "timeframe":        "{{interval}}",
  "strategy":         "<Strategy Name input>",
  "regime":           "LOW_VOL" | "MID_VOL" | "HIGH_VOL",
  "secret":           "<Webhook Secret input>",
  "strategy_equity":  {{strategy.equity}},
  "strategy_pnl":     {{strategy.netprofit}},
  "position_size":    {{strategy.position_size}},
  "daily_pnl_pct":    <daily_pnl_pct>,
  "daily_halted":     true | false
}
```

**Alert triggers:**
- `buy_sig` → `buy_msg` (action: "buy")
- `sell_sig` → `sell_msg` (action: "sell")
- Regime transitions to HIGH_VOL with open position → `hv_msg` (action: "flat")

**Visualisation:**
- Background colour: green (LOW_VOL), orange (MID_VOL), red (HIGH_VOL)
- Triangle-up on BUY, triangle-down on SELL, X on daily halt
- Plotted lines: EMA 20 (blue), EMA 50 (orange), SMA 200 (white)
- Stats table (bottom-right): Regime, ATR/Close, RSI, Daily P&L, Daily Halted status

## TradingView requirements

- **Pro+ plan or higher** — webhook delivery requires a paid plan; free accounts cannot send webhooks
- Alert must be set to **"Once Per Bar Close"** trigger
- Webhook URL: the ngrok HTTPS URL (or production URL) pointing to `/webhook/alert`
- Alert **Message** field: leave blank — the `alert()` calls in the script build the JSON body

## Connection to backend

The server side (`api/routes/webhook.py`) accepts both `strategy_equity`/`strategy_pnl` (Pine Script field names) and `equity`/`netprofit` (manual test payload aliases). Any field added to the Pine Script alert payload must also be handled in `api/routes/webhook.py`.

## Sync rules

- **Change the webhook payload schema** → update the payload fields table above AND the field descriptions in `api/CLAUDE.md` → `routes/webhook.py` section.
- **Change regime thresholds** → update the regex detection block above and note the input defaults.
- **Change entry/exit conditions** → update the entry/exit condition sections.
- **Add a new alert type** → add the trigger + payload description above.
- **Change the daily halt threshold default** → update the daily loss gate section AND check that `api/routes/webhook.py` circuit-breaker thresholds still match (`-3%` daily halt must agree).
