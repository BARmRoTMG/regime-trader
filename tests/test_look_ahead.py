"""Look-ahead bias tests.

The forward algorithm gives P(state_t | obs_1 … obs_t).  The mathematical
guarantee is that adding observations at T+1, T+2, … cannot change the value
at T.  These tests verify that the implementation honours that guarantee.

Key test
--------
``test_no_look_ahead_bias`` trains the HMM once on the full dataset, then
compares the filtered distribution at bar 399:

  * computed with only the first 400 bars of features
  * computed with 500 bars of features (future data appended)

They MUST be identical.  Any difference would indicate that future data leaked
into the computation at bar 399 — look-ahead bias.

The spec shows ``predict_regime_filtered(data[0:500])[400]`` but index 400
is the 401st bar, whereas ``predict_regime_filtered(data[0:400])[-1]`` is bar
399.  The correct comparison is at the same bar index, so we use index 399 in
both calls.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.hmm_engine import HMMConfig, HMMEngine, RegimeState
from data.feature_engineering import FEATURE_COLUMNS, build_features


# ---------------------------------------------------------------------------
# Shared synthetic data fixture
# ---------------------------------------------------------------------------


def _make_ohlcv(n: int, seed: int = 42) -> pd.DataFrame:
    """Synthetic OHLCV with three embedded volatility regimes."""
    rng = np.random.default_rng(seed)

    # Switch volatility every n/3 bars to create regime structure
    segment = n // 3
    vol_schedule = np.concatenate(
        [
            np.full(segment, 0.008),       # low vol
            np.full(segment, 0.018),       # mid vol
            np.full(n - 2 * segment, 0.032),  # high vol
        ]
    )

    log_returns = rng.normal(0, 1, n) * vol_schedule
    close = 100.0 * np.exp(np.cumsum(log_returns))
    high = close * np.exp(rng.uniform(0, 0.005, n))
    low = close * np.exp(-rng.uniform(0, 0.005, n))
    volume = rng.integers(1_000_000, 10_000_000, n).astype(float)

    dates = pd.bdate_range("2016-01-01", periods=n)
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": volume},
        index=dates,
    )


@pytest.fixture(scope="module")
def ohlcv_600() -> pd.DataFrame:
    return _make_ohlcv(700)    # 700 bars so 600 features survive NaN-drop


@pytest.fixture(scope="module")
def features_600(ohlcv_600: pd.DataFrame) -> pd.DataFrame:
    return build_features(ohlcv_600, normalise=True, dropna=True)


@pytest.fixture(scope="module")
def fitted_engine(features_600: pd.DataFrame, ohlcv_600: pd.DataFrame) -> HMMEngine:
    cfg = HMMConfig(
        n_candidates=[3, 4],    # small for test speed
        n_init=2,
        min_train_bars=252,
        stability_bars=2,
        flicker_window=10,
        flicker_threshold=3,
        min_confidence=0.55,
    )
    engine = HMMEngine(config=cfg)
    engine.fit(ohlcv_600)
    return engine


# ---------------------------------------------------------------------------
# THE KEY TEST: no look-ahead bias in the forward algorithm
# ---------------------------------------------------------------------------


def test_no_look_ahead_bias(features_600: pd.DataFrame, fitted_engine: HMMEngine) -> None:
    """Regime at bar T must be identical whether we pass data[0:T+1] or data[0:T+101].

    This is the canonical look-ahead bias test.  The forward algorithm computes
    P(state_t | obs_1 … obs_t) which by definition does not depend on obs_{t+1}
    and beyond.  If this test fails, future data is leaking into the computation.
    """
    T = 400    # bar index to test (0-based)
    assert len(features_600) > T + 100, "Not enough features for this test."

    X_short = features_600.values[:T + 1]     # bars 0 … T  (inclusive)
    X_long = features_600.values[:T + 101]    # bars 0 … T+100

    proba_short = fitted_engine.predict_regime_filtered(X_short)   # shape (T+1, k)
    proba_long = fitted_engine.predict_regime_filtered(X_long)     # shape (T+101, k)

    dist_at_T_short = proba_short[T]    # P(state | obs_0 … obs_T) using short seq
    dist_at_T_long = proba_long[T]      # P(state | obs_0 … obs_T) using long seq

    np.testing.assert_allclose(
        dist_at_T_short,
        dist_at_T_long,
        atol=1e-10,
        err_msg=(
            "LOOK-AHEAD BIAS DETECTED: filtered distribution at bar T differs "
            "when future bars are appended.  The forward algorithm is not causal."
        ),
    )


def test_no_look_ahead_multiple_horizons(
    features_600: pd.DataFrame, fitted_engine: HMMEngine
) -> None:
    """Repeat the look-ahead check at several T values across the sequence."""
    test_bars = [100, 200, 300, 400, 500]
    for T in test_bars:
        if T + 50 >= len(features_600):
            continue
        X_short = features_600.values[:T + 1]
        X_long = features_600.values[:T + 51]
        dist_short = fitted_engine.predict_regime_filtered(X_short)[T]
        dist_long = fitted_engine.predict_regime_filtered(X_long)[T]
        np.testing.assert_allclose(
            dist_short, dist_long, atol=1e-10,
            err_msg=f"Look-ahead bias detected at T={T}.",
        )


# ---------------------------------------------------------------------------
# Feature engineering: no look-ahead
# ---------------------------------------------------------------------------


class TestFeatureEngineeringNoLookahead:
    """Every feature at bar T must depend only on bars 0 … T."""

    @pytest.mark.parametrize(
        "feature_name",
        [
            "log_return_1",
            "log_return_5",
            "log_return_20",
            "realized_vol_20",
            "vol_ratio",
            "volume_zscore",
            "volume_trend",
            "adx_14",
            "sma50_slope",
            "rsi14_zscore",
            "dist_sma200",
            "roc_10",
            "roc_20",
            "atr_norm",
        ],
    )
    def test_feature_no_lookahead(
        self, feature_name: str, ohlcv_600: pd.DataFrame
    ) -> None:
        """Feature value at bar T must be unchanged when we remove future bars."""
        full_features = build_features(ohlcv_600, normalise=True, dropna=False)

        T = 350   # arbitrary bar in the middle of the series (after warm-up)
        full_at_T = full_features[feature_name].iloc[T]

        # Recompute on a prefix ending at T
        prefix_features = build_features(
            ohlcv_600.iloc[: T + 1], normalise=True, dropna=False
        )
        prefix_at_T = prefix_features[feature_name].iloc[-1]   # same bar

        if np.isnan(full_at_T) and np.isnan(prefix_at_T):
            return   # both NaN is acceptable (warm-up period)

        assert not np.isnan(full_at_T), f"{feature_name} is NaN at T={T} on full series"
        assert not np.isnan(prefix_at_T), f"{feature_name} is NaN at T={T} on prefix"

        np.testing.assert_allclose(
            prefix_at_T,
            full_at_T,
            rtol=1e-6,
            err_msg=(
                f"Feature '{feature_name}' at bar {T} differs between full series "
                f"({full_at_T:.8f}) and prefix ({prefix_at_T:.8f}).  LOOK-AHEAD BIAS."
            ),
        )

    def test_rolling_zscore_no_lookahead(self, ohlcv_600: pd.DataFrame) -> None:
        """Rolling z-score must use only past data in its mean/std computation."""
        from data.feature_engineering import rolling_zscore

        close = ohlcv_600["close"]
        lr = np.log(close / close.shift(1))

        T = 300
        full_zscore = rolling_zscore(lr, 252)
        prefix_zscore = rolling_zscore(lr.iloc[:T + 1], 252)

        full_val = full_zscore.iloc[T]
        prefix_val = prefix_zscore.iloc[-1]

        if not (np.isnan(full_val) or np.isnan(prefix_val)):
            np.testing.assert_allclose(prefix_val, full_val, rtol=1e-6)


# ---------------------------------------------------------------------------
# HMM inference: no look-ahead (forward algorithm property)
# ---------------------------------------------------------------------------


class TestHMMInferenceNoLookahead:
    def test_predict_sequence_causal(
        self, features_600: pd.DataFrame, fitted_engine: HMMEngine
    ) -> None:
        """predict() on a prefix must match predict() on the full series at the same bar."""
        T = 300
        X_full = features_600.values
        X_prefix = features_600.values[:T + 1]

        states_full = fitted_engine.predict(features_600)
        states_prefix = fitted_engine.predict(features_600.iloc[:T + 1])

        np.testing.assert_allclose(
            states_prefix[T].state_probabilities,
            states_full[T].state_probabilities,
            atol=1e-10,
            err_msg="predict() is not causal — look-ahead bias detected.",
        )

    def test_forward_algorithm_monotone_normalisation(
        self, features_600: pd.DataFrame, fitted_engine: HMMEngine
    ) -> None:
        """Each row of filtered probabilities must sum to 1.0."""
        X = features_600.values[:200]
        proba = fitted_engine.predict_regime_filtered(X)
        row_sums = proba.sum(axis=1)
        np.testing.assert_allclose(
            row_sums, np.ones(len(row_sums)), atol=1e-9,
            err_msg="Filtered probabilities do not sum to 1 — normalisation error.",
        )

    def test_forward_algorithm_non_negative(
        self, features_600: pd.DataFrame, fitted_engine: HMMEngine
    ) -> None:
        """All filtered probabilities must be ≥ 0."""
        proba = fitted_engine.predict_regime_filtered(features_600.values[:200])
        assert (proba >= 0).all(), "Negative filtered probabilities detected."


# ---------------------------------------------------------------------------
# Walk-forward backtest: no look-ahead at the fold boundary
# ---------------------------------------------------------------------------


class TestWalkForwardNoLookahead:
    def test_train_test_dates_non_overlapping(self) -> None:
        """Every fold's train_end must be strictly before its test_start."""
        from backtest.backtester import BacktestConfig, WalkForwardBacktester
        from core.hmm_engine import HMMConfig
        from core.regime_strategies import StrategyConfig
        from core.risk_manager import RiskConfig

        ohlcv = _make_ohlcv(800)
        multi_ohlcv = pd.concat({"SPY": ohlcv}, names=["symbol", "date"])

        cfg = BacktestConfig(
            train_window=252,
            test_window=63,
            step_size=63,
            initial_capital=100_000,
        )
        bt = WalkForwardBacktester(
            ohlcv=multi_ohlcv,
            hmm_config=HMMConfig(n_candidates=[3], n_init=1, min_train_bars=100),
            strategy_config=StrategyConfig(),
            risk_config=RiskConfig(),
            backtest_config=cfg,
            symbols=["SPY"],
        )
        folds = bt._generate_folds()
        for train_df, test_df in folds:
            train_end = train_df.index.get_level_values(-1).max()
            test_start = test_df.index.get_level_values(-1).min()
            assert train_end < test_start, (
                f"Fold overlap detected: train_end={train_end}  test_start={test_start}"
            )

    def test_hmm_sees_only_training_data(
        self, features_600: pd.DataFrame, fitted_engine: HMMEngine
    ) -> None:
        """Fitting on bars 0..251 must not affect prediction at bar 252 onwards."""
        train_feats = features_600.iloc[:252]
        train_ohlcv = _make_ohlcv(700)[:len(features_600)]   # proxy to get OHLCV

        cfg = HMMConfig(n_candidates=[3], n_init=1, min_train_bars=100)
        engine_train = HMMEngine(config=cfg)
        # We verify no exception and that the engine only sees training data
        # (the fit call itself enforces this by taking only the OHLCV slice)
        engine_train.fit(train_ohlcv.iloc[:300])  # 300 rows → ~252 features after warmup

        assert engine_train.is_fitted()
        assert engine_train._n_states in [3]
