# backtest/ — Walk-Forward Backtesting & Analytics

Simulates the HMM regime-allocation strategy on historical data using a rolling walk-forward methodology, computes risk-adjusted performance metrics, and stress-tests the strategy under adverse scenarios.

## Files

### backtester.py
Rolling walk-forward backtester. Trains HMM on in-sample (IS) data, then simulates bar-by-bar out-of-sample (OOS) rebalancing with realistic fills.

**`BacktestConfig`:**
```
train_window  = 252 bars   # IS period (1 year of daily data)
test_window   = 126 bars   # OOS period (half year)
step_size     = 126 bars   # Fold overlap = train+test, step = test_window
slippage_pct  = 0.0005     # 0.05% one-way
initial_capital = 100_000
```

**Key classes:**

| Class | Purpose |
|-------|---------|
| `BacktestConfig` | All hyperparameters |
| `FoldResult` | One fold: equity_curve, regime_series, confidence_series, trades, metrics |
| `BacktestResult` | Aggregated: combined equity, all fold results, trade log, primary_closes |
| `WalkForwardBacktester` | Orchestrator: accepts MultiIndex (symbol, timestamp) OHLCV |

**Walk-forward loop (`run()`):**
1. `_generate_folds()` — produces `(feature_dates, is_dates, oos_dates)` triples with no overlap
2. Per fold: build features → `HMMEngine.fit(is_data)` → `predict_regime_filtered()` on full window → `_simulate_oos()`
3. `_simulate_oos()` — bar-by-bar: compute signal → risk-check → execute rebalance if drift > threshold → mark-to-market

**Critical design decisions:**
- **Fill delay = 1 bar**: signal at bar N close → execution at bar N+1 open (mirrors live trading latency)
- **Forward algorithm** re-applied bar-by-bar on OOS data; stability filter resets per fold (models real-time uncertainty)
- **Sells before buys** during rebalance (`_execute_rebalance`): frees cash first
- **Rebalance threshold**: only rebalance when portfolio drift > `StrategyConfig.rebalance_threshold` (reduces churn)
- **Slippage**: buys +0.05%, sells −0.05% (applied to fill price, not signal price)
- **Primary symbol** for HMM training: SPY if in universe, else first alphabetical symbol

**`_fold_metrics()`** — lightweight per-fold stats: Sharpe, max drawdown, total return, number of rebalances.

### performance.py
Comprehensive risk-adjusted analytics and report export.

**`PerformanceAnalyzer`** takes a `BacktestResult` and computes a `PerformanceReport`:

| Category | Metrics |
|----------|---------|
| Returns | total return, CAGR, annualised volatility |
| Risk-adjusted | Sharpe, Sortino, Calmar ratios |
| Drawdowns | max drawdown, avg drawdown, max DD duration, recovery days |
| Worst periods | worst 1-day, 1-week, 1-month returns |
| Trades | count, win rate, avg win, avg loss, profit factor |
| Regime breakdown | per-regime % time, return contribution, Sharpe, win rate |
| Confidence buckets | performance at <50%, 50–60%, 60–70%, 70%+ regime confidence |
| Benchmarks | buy-and-hold, SMA-200 filter, random allocation (100 MC runs) |
| Alpha/Beta | vs primary benchmark daily returns |
| Walk-forward folds | per-fold return, Sharpe, max-DD |

**Output:**
- `print_report()` — Rich-formatted terminal tables
- `save_results(results_dir)` — exports `equity_curve.csv`, `trade_log.csv`, `regime_history.csv`, `benchmark_comparison.csv`

### stress_test.py
Injects synthetic adverse scenarios and runs Monte Carlo tail-risk analysis.

**Built-in scenarios:**
| Scenario | Shock |
|----------|-------|
| `_crash_injection()` | Linear ramp down 20%/40% over 5/20 bars |
| `_overnight_gap()` | Permanent −5% price gaps at random bars |
| `_vol_spike()` | Amplify log-returns 3× for 20 bars (GFC-style) |
| `_liquidity_crisis()` | Widen high-low spread 5× for 10 bars |
| `_prolonged_drawdown()` | Slow −30% over 63 bars, 50% recovery over 126 bars |

**Monte Carlo runs:**
- `run_crash_monte_carlo()` — 100 sims × 10 random single-day crashes (−5% to −15%); reports mean/worst loss, % blowups (max-DD > 20%)
- `run_gap_risk()` — 50 sims × 5 overnight gaps (2–5× ATR, 70% down / 30% up); reports containment ratio
- `run_regime_misclassification()` — 100 sims shuffling regime labels; verifies circuit breakers contain damage independently of HMM

**`full_stress_report()`** — runs all scenarios + all MC tests; prints Rich tables + exports CSVs.

## Cross-module connections

| Direction | Module | What |
|-----------|--------|------|
| Imports from | `core/hmm_engine.py` | `HMMEngine`, `HMMConfig` |
| Imports from | `core/regime_strategies.py` | `StrategyOrchestrator`, `StrategyConfig` |
| Imports from | `core/risk_manager.py` | `RiskManager`, `RiskConfig` |
| Imports from | `data/feature_engineering.py` | `build_features()` |
| Exports to | `main.py` | `WalkForwardBacktester`, `PerformanceAnalyzer`, `StressTester` |

## How to run

```bash
# Full walk-forward backtest
python main.py --mode backtest

# Stress test suite
python main.py --mode stress

# Single-symbol debug
python main.py --mode backtest --symbols SPY --log-level DEBUG
```

## Sync rules

- **Change slippage or fill delay** → update both the BacktestConfig defaults table and the "Critical design decisions" section.
- **Add a new stress scenario** → add a row to the built-in scenarios table.
- **Change MC simulation counts** → update the Monte Carlo runs section.
- **Add a new performance metric** → add to the metrics table in `performance.py` section.
- **Change the primary-symbol selection rule** → update the note above.
