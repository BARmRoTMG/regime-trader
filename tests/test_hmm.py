"""Unit tests for HMMEngine: fitting, prediction, and regime labelling."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.hmm_engine import HMMConfig, HMMEngine, Regime, RegimeState


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def default_config() -> HMMConfig:
    return HMMConfig(
        n_candidates=[3, 4],
        n_init=3,
        min_train_bars=50,
        stability_bars=2,
        flicker_window=10,
        flicker_threshold=3,
        min_confidence=0.55,
    )


@pytest.fixture()
def synthetic_features(rng: np.random.Generator) -> pd.DataFrame:
    """Generate 300 bars of synthetic feature data with 3 distinct volatility regimes."""
    ...


@pytest.fixture()
def rng() -> np.random.Generator:
    return np.random.default_rng(seed=42)


@pytest.fixture()
def fitted_engine(default_config: HMMConfig, synthetic_features: pd.DataFrame) -> HMMEngine:
    engine = HMMEngine(config=default_config)
    engine.fit(synthetic_features)
    return engine


# ---------------------------------------------------------------------------
# Construction / configuration
# ---------------------------------------------------------------------------


class TestHMMEngineInit:
    def test_not_fitted_before_fit(self, default_config: HMMConfig) -> None:
        engine = HMMEngine(config=default_config)
        assert not engine.is_fitted()

    def test_config_stored(self, default_config: HMMConfig) -> None:
        engine = HMMEngine(config=default_config)
        assert engine.config is default_config


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------


class TestHMMEngineFit:
    def test_fit_marks_engine_as_fitted(
        self, default_config: HMMConfig, synthetic_features: pd.DataFrame
    ) -> None:
        engine = HMMEngine(config=default_config)
        engine.fit(synthetic_features)
        assert engine.is_fitted()

    def test_fit_raises_on_too_few_bars(
        self, default_config: HMMConfig, synthetic_features: pd.DataFrame
    ) -> None:
        engine = HMMEngine(config=default_config)
        with pytest.raises(ValueError, match="min_train_bars"):
            engine.fit(synthetic_features.iloc[:10])

    def test_state_to_regime_populated_after_fit(self, fitted_engine: HMMEngine) -> None:
        assert len(fitted_engine._state_to_regime) > 0

    def test_all_regime_labels_valid(self, fitted_engine: HMMEngine) -> None:
        for regime in fitted_engine._state_to_regime.values():
            assert isinstance(regime, Regime)


# ---------------------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------------------


class TestHMMEnginePredict:
    def test_predict_returns_one_state_per_row(
        self, fitted_engine: HMMEngine, synthetic_features: pd.DataFrame
    ) -> None:
        states = fitted_engine.predict(synthetic_features.iloc[-20:])
        assert len(states) == 20

    def test_predict_latest_returns_single_state(
        self, fitted_engine: HMMEngine, synthetic_features: pd.DataFrame
    ) -> None:
        state = fitted_engine.predict_latest(synthetic_features)
        assert isinstance(state, RegimeState)

    def test_confidence_between_zero_and_one(
        self, fitted_engine: HMMEngine, synthetic_features: pd.DataFrame
    ) -> None:
        state = fitted_engine.predict_latest(synthetic_features)
        assert 0.0 <= state.confidence <= 1.0

    def test_posteriors_sum_to_one(
        self, fitted_engine: HMMEngine, synthetic_features: pd.DataFrame
    ) -> None:
        state = fitted_engine.predict_latest(synthetic_features)
        assert abs(state.posteriors.sum() - 1.0) < 1e-6

    def test_predict_raises_if_not_fitted(
        self, default_config: HMMConfig, synthetic_features: pd.DataFrame
    ) -> None:
        engine = HMMEngine(config=default_config)
        with pytest.raises(RuntimeError, match="not fitted"):
            engine.predict(synthetic_features)


# ---------------------------------------------------------------------------
# Stability and flicker guards
# ---------------------------------------------------------------------------


class TestStabilityAndFlicker:
    def test_stability_mask_length_matches_input(
        self, fitted_engine: HMMEngine, synthetic_features: pd.DataFrame
    ) -> None:
        states = fitted_engine.predict(synthetic_features)
        assert len(states) == len(synthetic_features)

    def test_flicker_count_non_negative(
        self, fitted_engine: HMMEngine, synthetic_features: pd.DataFrame
    ) -> None:
        states = fitted_engine.predict(synthetic_features)
        for s in states:
            assert s.flicker_count >= 0
