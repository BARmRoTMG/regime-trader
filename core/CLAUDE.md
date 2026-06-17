# core/ — HMM Engine & Trading Strategy

The intelligence layer of the algo-trading system: volatility regime detection (HMM), strategy allocation, risk management, and signal orchestration. This module is used by the `backtest/` pipeline and (when implemented) the live trading loop in `main.py`. It is **not** used by the TradingView webhook dashboard in `api/`.

## Files

### hmm_engine.py
Gaussian HMM volatility classifier. Answers "Is the market calm, moderate, or turbulent?" — never "Will it go up?"

**Key classes:**

| Class | Purpose |
|-------|---------|
| `HMMConfig` | Hyperparameters (n_candidates, n_init, min_train_bars, stability_bars, flicker_window, flicker_threshold, min_confidence) |
| `HMMEngine` | Main detector: `fit()`, `predict()`, `predict_latest()` |
| `RegimeInfo` | Static metadata per HMM state: expected return, expected volatility, strategy type, max leverage |
| `RegimeState` | One-bar output: regime, confidence, posteriors, is_confirmed, flicker_count, timestamp |

**Key methods:**
- `fit(features_df)` — Trains HMM via BIC model selection across `n_candidates` state counts (3–7). Requires ≥ `min_train_bars` rows.
- `predict(features_df)` → `list[RegimeState]` — Forward algorithm on a batch.
- `predict_latest(features_df)` → `RegimeState` — Single current-bar prediction with stability + flicker filters applied.
- `is_fitted()` — Guard before any inference call.

**Regime label maps** (`REGIME_LABELS`): states sorted by mean log-return ascending. 3 states → `[BEAR, NEUTRAL, BULL]`; 4 → `[CRASH, BEAR, BULL, EUPHORIA]`; etc.

### CRITICAL INVARIANT — Forward Algorithm Only
**Never call `model.predict()` (Viterbi) in any live or backtest path.** Viterbi revises past assignments using future observations = look-ahead bias. Only the forward algorithm (`predict_latest` / `predict`) is valid.

### regime_strategies.py
Maps HMM volatility rank to trading allocation. **Uses volatility rank, not regime labels** — a "BULL" label does not guarantee low volatility.

**Volatility rank mapping** (after sorting all states by `expected_volatility` ascending):
```
position ≤ 0.33  →  LowVolBullStrategy      (95% allocation, 1.25x leverage)
position ≥ 0.67  →  HighVolDefensiveStrategy (60% allocation, 1.0x leverage)
else             →  MidVolCautiousStrategy   (95% if trend intact, 60% if not)
```

**Key classes:**

| Class | Purpose |
|-------|---------|
| `Signal` | One actionable signal: symbol, direction (LONG/FLAT), entry/stop/tp prices, position_size_pct, leverage, regime metadata |
| `StrategyConfig` | Portfolio parameters: low_vol_allocation, high_vol_allocation, low_vol_leverage, rebalance_threshold |
| `AllocationResult` | Per-bar aggregate: target equity fraction, symbol weights |
| `LowVolBullStrategy` | 95% allocation, 1.25x leverage, stop = max(P−3ATR, EMA50−0.5ATR) |
| `MidVolCautiousStrategy` | 95% if price > EMA50 (trend intact), 60% otherwise; leverage 1.0x |
| `HighVolDefensiveStrategy` | 60% allocation, 1.0x leverage, wider stop at EMA50−1.0ATR |
| `StrategyOrchestrator` | Routes each regime to the correct strategy by volatility rank |

**No short positions.** All strategies emit `Direction.LONG` or `Direction.FLAT`. High-vol defensive stays 60% long to catch V-shaped rebounds.

**Uncertainty mode:** If `regime_state.confidence < min_confidence` or `flicker_count > threshold`, halve position size and tag signal as "UNCERTAINTY".

### risk_manager.py
13-layer risk gauntlet — last gate before orders reach the broker. Operates independently of the HMM (circuit breakers fire on actual drawdowns, not forecasts).

**Key classes:**

| Class | Purpose |
|-------|---------|
| `RiskConfig` | Thresholds: max_risk_per_trade=0.01, max_exposure=0.80, max_single_position=0.15, max_leverage=1.25, max_concurrent=5, max_daily_trades=20 |
| `CircuitBreaker` | Drawdown-triggered halts/reductions; writes lock file on PEAK_HALT |
| `RiskManager` | `validate_signal(signal, portfolio_state)` → `RiskDecision` |
| `PortfolioState` | Full portfolio snapshot: equity, cash, open positions, daily/weekly/peak drawdowns |
| `RiskDecision` | Output: approved bool, modified_signal, final_shares, modifications list, rejection_reason |

**Circuit-breaker levels** (size_scalar → 0.0 = halted, 0.5 = reduced, 1.0 = clear):
| Trigger | Level | Action |
|---------|-------|--------|
| Daily P&L ≤ −2% | DAILY_REDUCE | size × 0.5 |
| Daily P&L ≤ −3% | DAILY_HALT | size × 0.0 |
| Weekly P&L ≤ −5% | WEEKLY_REDUCE | size × 0.5 |
| Weekly P&L ≤ −7% | WEEKLY_HALT | size × 0.0 |
| Peak drawdown ≤ −10% | PEAK_HALT | writes lock file; manual deletion required to resume |

**Position sizing formula** (`_compute_shares`):
```
shares = min(
    risk_budget / risk_per_share,          # 1% portfolio / (entry - stop)
    equity * overnight_cap / (3 * risk),   # overnight gap: 3x stop = max 2% loss
    equity * allocation_weight / price,    # strategy target weight
    equity * max_single_position / price   # 15% cap
) * size_scalar (circuit breaker scalar)
```
Result floored to whole shares; minimum $100 notional.

**Leverage rules:** Force 1.0x if: circuit breaker active, too many open positions, low confidence, high flicker rate.

**Correlation check:** If correlation > 0.70 with existing position → reduce 50%; if > 0.85 → reject.

### signal_generator.py
Orchestration skeleton — combines HMMEngine + StrategyOrchestrator + RiskManager into a `SignalPacket` ready for execution.

**Status: STUB.** The following methods are `...` (not yet implemented):
- `SignalGenerator.generate()` — main entry point
- `_compute_trend_signal()` — MA crossover / momentum
- `_build_trade_signals()` — converts AllocationResult into per-symbol TradeSignals

**Key output types:**
- `TradeSignal` — per-symbol: action (buy/sell/hold/close), target_weight, shares, rationale
- `SignalPacket` — one-bar bundle: regime_state, allocation, risk_snapshot, per-symbol TradeSignals, is_actionable flag

## Cross-module connections

| Direction | Module | What |
|-----------|--------|------|
| Imports from | `data/feature_engineering.py` | `FEATURE_COLUMNS`, `F_LOG_RETURN_1`, `build_features()` |
| Exports to | `backtest/backtester.py` | HMMEngine, StrategyOrchestrator, RiskManager, SignalPacket |
| Exports to | `broker/order_executor.py` | TradeSignal, SignalPacket |
| Exports to | `broker/tv_webhook.py` | RiskManager.validate_signal() |

## How to test

```bash
pytest tests/test_hmm.py -v
pytest tests/test_strategies.py -v
pytest tests/test_risk.py -v
pytest tests/test_look_ahead.py -v   # critical — verifies no look-ahead bias
```

## Sync rules

- **Implement a stub method** → remove "Status: STUB" note from `signal_generator.py` section above.
- **Change circuit-breaker thresholds** → update the CB table here AND in `api/CLAUDE.md` (webhook.py and portfolio.py both implement the same thresholds — they must stay in sync).
- **Add a new regime state count** → add the label list to `REGIME_LABELS` in hmm_engine.py and update the label maps section above.
- **Change `RiskConfig` defaults** → update the Key classes table above and `config/settings.yaml`.
- **Change `StrategyConfig` allocations** → update the volatility rank mapping section above.
