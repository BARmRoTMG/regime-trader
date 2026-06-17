# tests/ — Test Suite

Unit and integration tests for the HMM engine, strategy logic, risk management, and order execution. Run with `pytest`.

## Running tests

```bash
pytest                              # all tests
pytest tests/test_hmm.py -v         # HMM engine only
pytest tests/test_look_ahead.py -v  # look-ahead bias checks (critical)
pytest tests/test_risk.py -v        # risk manager + circuit breakers
pytest tests/test_strategies.py -v  # regime strategies
pytest tests/test_orders.py -v      # order executor
pytest -k "TestDrawdown" -v         # run by class/method name pattern
pytest --tb=short                   # shorter tracebacks
```

## Test files

### test_hmm.py
**Scope:** `HMMEngine` — construction, fitting, prediction, stability/flicker guards.

**Fixtures:**
- `default_config` — `HMMConfig(n_candidates=[3,4], min_train_bars=50, stability_bars=2, ...)`
- `synthetic_features` — 300 bars of synthetic feature data with 3 distinct volatility regimes
- `rng` — `np.random.default_rng(seed=42)` for reproducibility
- `fitted_engine` — pre-fitted engine on `synthetic_features`

**Test classes:**
| Class | What it tests |
|-------|---------------|
| `TestHMMEngineInit` | Unfitted state, config stored |
| `TestHMMEngineFit` | `min_train_bars` validation, `_state_to_regime` populated, all regime labels valid |
| `TestHMMEnginePredict` | Output length, `RegimeState` type, confidence [0,1], posteriors sum to 1, raises if not fitted |
| `TestStabilityAndFlicker` | State list length matches input, flicker_count ≥ 0 |

### test_look_ahead.py
**Scope:** Verifies zero look-ahead bias in the forward algorithm and feature engineering. **This suite must always pass before merging any changes to `core/hmm_engine.py` or `data/feature_engineering.py`.**

**Key tests:**
- `test_no_look_ahead_bias` — `P(state_T | obs_0:T)` must equal `P(state_T | obs_0:T+100)`. The forward-filtered distribution at T must not change when future data is added.
- `test_no_look_ahead_multiple_horizons` — repeat at T=100, 200, 300, 400, 500
- `TestFeatureEngineeringNoLookahead` — each feature value at bar T must not change when rows T+1…T+N are appended
- `TestWalkForwardNoLookahead` — fold IS and OOS date ranges are non-overlapping

**Fixtures:**
- `ohlcv_600` — 700 synthetic OHLCV bars (600 features remain after NaN-drop)
- `features_600` — pre-built feature DataFrame
- `fitted_engine` — `HMMConfig(n_candidates=[3,4], min_train_bars=252)`

### test_orders.py
**Scope:** `OrderExecutor` — placement, validation, cancellation, dry-run mode.

**Fixtures:**
- `mock_client` — `MagicMock(spec=AlpacaClient)`
- `executor` — live `OrderExecutor(mock_client, risk_manager)`
- `dry_run_executor` — same but with `dry_run=True`

**Test classes:**
| Class | What it tests |
|-------|---------------|
| `TestPlaceMarketOrder` | Order record created, appended to log |
| `TestDryRun` | Dry-run does NOT call `mock_client.submit_order`, still records order |
| `TestSignalOrdering` | Sells submitted before buys in `execute_signals()` |
| `TestCancellation` | `cancel_order()` and `cancel_all_open_orders()` |
| `TestPreFlightCheck` | Rejects zero/negative share counts, respects halt lock file |

### test_risk.py
**Scope:** `RiskManager` — position sizing, validation, drawdown circuit-breakers, session reset.

**Fixtures:**
- `risk_config` — `RiskConfig(max_risk_per_trade=0.01, max_single_position=0.15, ...)`
- `risk_manager` — `RiskManager(risk_config)`
- Various `PortfolioState` instances for different drawdown levels

**Test classes:**
| Class | What it tests |
|-------|---------------|
| `TestRiskManagerInit` | No halt at startup, size_scalar=1.0, 0 open positions |
| `TestPositionSizing` | Respects max_risk_per_trade, max_single_position, returns whole shares |
| `TestOrderValidation` | Valid orders approved; rejected when halted or max_daily_trades exceeded |
| `TestDrawdownCircuitBreakers` | DAILY_REDUCE at −2%, DAILY_HALT at −3%, WEEKLY_HALT at −7%, PEAK_HALT at −10% |
| `TestSessionReset` | `reset_daily()` clears trade count; weekly tracking updates `week_open_nav` |

### test_strategies.py
**Scope:** `LowVolBullStrategy`, `MidVolCautiousStrategy`, `HighVolDefensiveStrategy`, `StrategyOrchestrator`.

**Fixtures:**
- `config` — `StrategyConfig(low_vol_allocation=0.95, high_vol_allocation=0.60, leverage=1.25)`
- `make_bars(trend)` — synthetic OHLCV with configurable up/down trend
- `make_regime_state(label, probability, is_confirmed)` — `RegimeState` factory
- `make_regime_infos()` — dict of `RegimeInfo` with monotonically separated volatilities

**Test classes:**
| Class | What it tests |
|-------|---------------|
| `TestLowVolBullStrategy` | LONG direction, leverage=1.25, stop below entry, TP respects R:R ratio |
| `TestMidVolCautiousStrategy` | Trend-intact → 95% allocation; broken trend → 60%; leverage=1.0 |
| `TestHighVolDefensiveStrategy` | 60% allocation, leverage=1.0, LONG direction (never short) |
| `TestUncertaintyMode` | Low confidence → halved position size; "UNCERTAINTY" in rationale |
| `TestStrategyOrchestratorMapping` | Lowest-vol regime → LowVolBull; highest-vol → HighVolDefensive |
| `TestRebalancing` | Drift > 10% triggers rebalance; report = target − current weights |

## Conventions

- All tests use `pytest` fixtures — no `setUp`/`tearDown`.
- Synthetic data uses `np.random.default_rng(seed=42)` for reproducibility.
- In-memory databases (`sqlite3.connect(":memory:")`) for any DB-touching tests.
- `MagicMock(spec=AlpacaClient)` for broker tests — real Alpaca calls are never made.

## Sync rules

- **Add a new public method to a tested class** → add at least one test case in the relevant file.
- **Implement a stub** (`signal_generator.py`, `market_data.py`) → create `tests/test_signal_generator.py` or `tests/test_market_data.py`.
- **Change circuit-breaker thresholds** → update the assertion values in `TestDrawdownCircuitBreakers`.
- **Change `HMMConfig` defaults** → update `default_config` fixture or add a new fixture.
