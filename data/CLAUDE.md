# data/ — Feature Engineering & Market Data

Builds the causal feature matrix fed into the HMM engine, and manages the historical/streaming OHLCV price feed from Alpaca.

## Files

### feature_engineering.py
Produces a 14-column feature DataFrame from raw OHLCV. All features are causal — every rolling window looks backward only, with no future data leakage.

**Main entry point:** `build_features(ohlcv, normalise=True, dropna=True)` → `pd.DataFrame`

**Pipeline steps (in order):**
1. Log returns over 1, 5, 20 bars → `log_return_1`, `log_return_5`, `log_return_20`
2. Realised vol (20-bar annualised) + vol ratio (5/20) → `realized_vol_20`, `vol_ratio`
3. Volume z-score (vs 50-bar mean) + volume trend slope → `volume_zscore`, `volume_trend`
4. ADX(14), SMA50 slope % → `adx_14`, `sma50_slope`
5. RSI(14) z-score, % distance from SMA200 → `rsi14_zscore`, `dist_sma200`
6. Rate of change over 10/20 bars → `roc_10`, `roc_20`
7. ATR(14) normalised by close → `atr_norm`

**`FEATURE_COLUMNS`** — the ordered list of all 14 column names above. HMM training uses this constant to select columns. `F_LOG_RETURN_1 = "log_return_1"` is imported by hmm_engine for state sorting.

**Normalisation:** `rolling_zscore(series, window=252)` — 1-year rolling z-score with `min_periods = window // 4`. All features are normalised to zero-mean, unit-variance after the warm-up period converges.

**`_rolling_linear_slope(series, window)`** — OLS slope normalised by `|mean|` for scale-free output (used for `sma50_slope`, `volume_trend`).

**`FeatureEngineer` class** — stateless wrapper for ergonomic use; thread-safe; delegates to the standalone `build_features()` function.

**Warm-up:** After `dropna=True`, approximately 252 bars are consumed by the rolling z-score windows. The HMM's `min_train_bars` accounts for this.

### market_data.py
Single source of truth for price data. Caches historical bars, appends real-time stream updates, detects gaps, and exposes a unified read interface.

**Status: STUB.** All methods are `...` (not implemented). The class interface is defined:
- `load_history(symbols, n_bars=504)` — fetch from Alpaca REST
- `start_stream()` / `stop_stream()` — WebSocket bar/quote streaming
- `get_bars(symbol, n=None, start=None, end=None)` → `pd.DataFrame`
- `get_latest_price(symbol)` / `get_latest_prices(symbols)` → price dict
- `on_new_bar(callback)` — register listener for new-bar events
- `_handle_bar(event)` — parse stream event, append to cache, invoke callbacks
- `_detect_and_fill_gaps()` — back-fill from REST if stream drops
- `_normalize_bars()` — ensure consistent column names, UTC DatetimeIndex

## Cross-module connections

| Direction | Module | What |
|-----------|--------|------|
| Imports from | `broker/alpaca_client.py` | REST + streaming OHLCV |
| Exports to | `core/hmm_engine.py` | `FEATURE_COLUMNS`, feature DataFrame |
| Exports to | `core/signal_generator.py` | `build_features()`, `MarketDataFeed` |
| Exports to | `backtest/backtester.py` | `build_features()` called per fold |

## Design constraints

- **No look-ahead bias:** every rolling op uses only data up to bar T. `rolling(..., min_periods=N)` is safe; `shift(-N)` is not.
- Feature matrix must use `FEATURE_COLUMNS` constant — do not hard-code column names in the HMM engine.
- `build_features()` output must be deterministic given the same OHLCV input (stateless).

## How to test

```bash
pytest tests/test_look_ahead.py -v   # verifies feature engineering is causal
```

Manual check:
```python
from data.feature_engineering import build_features
import pandas as pd
# Pass 300 bars of OHLCV; verify output shape = (300 - ~252, 14) after dropna
```

## Sync rules

- **Add a new feature column** → add the compute function, add the column name to `FEATURE_COLUMNS`, and update the feature pipeline table in this file.
- **Change rolling window sizes** → update the normalisation and warm-up description.
- **Implement `market_data.py` stubs** → remove "Status: STUB" note from the market_data.py section.
- **Change `F_LOG_RETURN_1`** or any other exported constant → update `core/CLAUDE.md` (HMMEngine uses it for state sorting).
