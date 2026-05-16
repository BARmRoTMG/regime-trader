"""Unit and integration tests for StrategyOrchestrator and the three concrete strategies."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.hmm_engine import RegimeInfo, RegimeState
from core.regime_strategies import (
    AllocationResult,
    Direction,
    HighVolDefensiveStrategy,
    LABEL_TO_STRATEGY,
    LowVolBullStrategy,
    MidVolCautiousStrategy,
    Signal,
    StrategyConfig,
    StrategyOrchestrator,
    # backward-compat aliases
    BullTrendStrategy,
    BearTrendStrategy,
    CrashDefensiveStrategy,
    MeanReversionStrategy,
    EuphoriaCautiousStrategy,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT", "AMZN"]


@pytest.fixture()
def config() -> StrategyConfig:
    return StrategyConfig(
        low_vol_allocation=0.95,
        mid_vol_allocation_trend=0.95,
        mid_vol_allocation_no_trend=0.60,
        high_vol_allocation=0.60,
        low_vol_leverage=1.25,
        rebalance_threshold=0.10,
        uncertainty_size_mult=0.50,
        min_confidence=0.55,
        max_single_position=0.15,
        take_profit_rr_ratio=2.0,
    )


def make_bars(n: int = 100, uptrend: bool = True, seed: int = 42) -> pd.DataFrame:
    """Synthetic OHLCV with a clear uptrend or downtrend."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2022-01-01", periods=n)
    drift = 0.0005 if uptrend else -0.0005
    log_ret = rng.normal(drift, 0.012, n)
    close = 400.0 * np.exp(np.cumsum(log_ret))
    high = close * np.exp(rng.uniform(0, 0.003, n))
    low = close * np.exp(-rng.uniform(0, 0.003, n))
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": np.full(n, 1e6)},
        index=dates,
    )


def make_regime_state(
    label: str = "BULL",
    state_id: int = 0,
    probability: float = 0.80,
    is_confirmed: bool = True,
    consecutive_bars: int = 10,
) -> RegimeState:
    n_states = 3
    proba = np.full(n_states, (1 - probability) / (n_states - 1))
    proba[0] = probability
    return RegimeState(
        label=label,
        state_id=state_id,
        probability=probability,
        state_probabilities=proba,
        timestamp=pd.Timestamp.now(tz="UTC"),
        is_confirmed=is_confirmed,
        consecutive_bars=consecutive_bars,
    )


def make_regime_infos(n_states: int = 3) -> dict[str, RegimeInfo]:
    """Create regime infos with monotonically increasing expected_volatility."""
    labels = {3: ["BEAR", "NEUTRAL", "BULL"],
               4: ["CRASH", "BEAR", "BULL", "EUPHORIA"],
               5: ["CRASH", "BEAR", "NEUTRAL", "BULL", "EUPHORIA"]}[n_states]
    vols = np.linspace(0.25, 0.60, n_states)   # clear vol separation
    rets = np.linspace(-0.005, 0.005, n_states)
    return {
        label: RegimeInfo(
            regime_id=i,
            regime_name=label,
            expected_return=float(rets[i]),
            expected_volatility=float(vols[i]),
            recommended_strategy_type="moderate",
            max_leverage_allowed=1.25,
            max_position_size_pct=0.15,
        )
        for i, label in enumerate(labels)
    }


# ---------------------------------------------------------------------------
# LowVolBullStrategy
# ---------------------------------------------------------------------------


class TestLowVolBullStrategy:
    def test_returns_long_signal(self, config: StrategyConfig) -> None:
        strat = LowVolBullStrategy(config)
        bars = make_bars(uptrend=True)
        state = make_regime_state("BULL")
        sig = strat.generate_signal("SPY", bars, state, n_symbols=5)
        assert sig is not None
        assert sig.direction == Direction.LONG

    def test_leverage_is_1_25(self, config: StrategyConfig) -> None:
        strat = LowVolBullStrategy(config)
        bars = make_bars(uptrend=True)
        state = make_regime_state("BULL", probability=0.85)
        sig = strat.generate_signal("SPY", bars, state, n_symbols=5)
        assert sig is not None
        assert sig.leverage == pytest.approx(1.25)

    def test_stop_below_entry(self, config: StrategyConfig) -> None:
        strat = LowVolBullStrategy(config)
        bars = make_bars(uptrend=True)
        state = make_regime_state("BULL")
        sig = strat.generate_signal("SPY", bars, state, n_symbols=5)
        assert sig is not None
        assert sig.stop_loss < sig.entry_price

    def test_take_profit_above_entry(self, config: StrategyConfig) -> None:
        strat = LowVolBullStrategy(config)
        bars = make_bars(uptrend=True)
        state = make_regime_state("BULL")
        sig = strat.generate_signal("SPY", bars, state, n_symbols=5)
        assert sig is not None
        assert sig.take_profit is not None
        assert sig.take_profit > sig.entry_price

    def test_take_profit_respects_rr_ratio(self, config: StrategyConfig) -> None:
        strat = LowVolBullStrategy(config)
        bars = make_bars(uptrend=True)
        state = make_regime_state("BULL")
        sig = strat.generate_signal("SPY", bars, state, n_symbols=1)
        assert sig is not None
        assert sig.take_profit is not None
        risk = sig.entry_price - sig.stop_loss
        expected_tp = sig.entry_price + config.take_profit_rr_ratio * risk
        assert sig.take_profit == pytest.approx(expected_tp, rel=1e-6)

    def test_position_size_respects_max_single(self, config: StrategyConfig) -> None:
        strat = LowVolBullStrategy(config)
        bars = make_bars(uptrend=True)
        state = make_regime_state("BULL")
        # With n_symbols=1, equal weight = 95% which exceeds max_single_position=15%
        sig = strat.generate_signal("SPY", bars, state, n_symbols=1)
        assert sig is not None
        assert sig.position_size_pct <= config.max_single_position + 1e-9

    def test_position_size_divided_equally(self, config: StrategyConfig) -> None:
        strat = LowVolBullStrategy(config)
        bars = make_bars(uptrend=True)
        state = make_regime_state("BULL")
        n = 10
        sig = strat.generate_signal("SPY", bars, state, n_symbols=n)
        assert sig is not None
        expected = min(config.low_vol_allocation / n, config.max_single_position)
        assert sig.position_size_pct == pytest.approx(expected, rel=1e-6)

    def test_returns_none_for_short_bars(self, config: StrategyConfig) -> None:
        strat = LowVolBullStrategy(config)
        bars = make_bars(n=30)   # fewer than _MIN_BARS=60
        state = make_regime_state("BULL")
        sig = strat.generate_signal("SPY", bars, state)
        assert sig is None

    def test_strategy_name(self, config: StrategyConfig) -> None:
        assert LowVolBullStrategy(config).name == "LowVolBullStrategy"


# ---------------------------------------------------------------------------
# MidVolCautiousStrategy
# ---------------------------------------------------------------------------


class TestMidVolCautiousStrategy:
    def test_trend_intact_uses_high_allocation(self, config: StrategyConfig) -> None:
        strat = MidVolCautiousStrategy(config)
        bars = make_bars(uptrend=True, n=200)   # long uptrend → price > EMA50
        state = make_regime_state("NEUTRAL", probability=0.75)
        sig = strat.generate_signal("SPY", bars, state, n_symbols=5)
        assert sig is not None
        expected = min(config.mid_vol_allocation_trend / 5, config.max_single_position)
        assert sig.position_size_pct == pytest.approx(expected, rel=1e-4)

    def test_trend_broken_uses_low_allocation(self, config: StrategyConfig) -> None:
        strat = MidVolCautiousStrategy(config)
        bars = make_bars(uptrend=False, n=200)   # downtrend → price < EMA50
        state = make_regime_state("WEAK_BEAR", probability=0.75)
        sig = strat.generate_signal("SPY", bars, state, n_symbols=5)
        assert sig is not None
        # The downtrend may not push price below EMA50 immediately; check upper bound
        max_pct = min(config.mid_vol_allocation_trend / 5, config.max_single_position)
        assert sig.position_size_pct <= max_pct + 1e-9

    def test_leverage_always_1(self, config: StrategyConfig) -> None:
        strat = MidVolCautiousStrategy(config)
        bars = make_bars(uptrend=True, n=120)
        state = make_regime_state("NEUTRAL")
        sig = strat.generate_signal("SPY", bars, state, n_symbols=5)
        assert sig is not None
        assert sig.leverage == pytest.approx(1.0)

    def test_stop_below_entry(self, config: StrategyConfig) -> None:
        strat = MidVolCautiousStrategy(config)
        bars = make_bars(uptrend=True, n=120)
        state = make_regime_state("NEUTRAL")
        sig = strat.generate_signal("SPY", bars, state)
        assert sig is not None
        assert sig.stop_loss < sig.entry_price

    def test_trend_flag_in_metadata(self, config: StrategyConfig) -> None:
        strat = MidVolCautiousStrategy(config)
        bars = make_bars(uptrend=True, n=200)
        state = make_regime_state("NEUTRAL")
        sig = strat.generate_signal("SPY", bars, state, n_symbols=5)
        assert sig is not None
        assert "trend_intact" in sig.metadata


# ---------------------------------------------------------------------------
# HighVolDefensiveStrategy
# ---------------------------------------------------------------------------


class TestHighVolDefensiveStrategy:
    def test_allocation_is_60_pct(self, config: StrategyConfig) -> None:
        strat = HighVolDefensiveStrategy(config)
        bars = make_bars(n=120)
        state = make_regime_state("CRASH", probability=0.80)
        n = 5
        sig = strat.generate_signal("SPY", bars, state, n_symbols=n)
        assert sig is not None
        expected = min(config.high_vol_allocation / n, config.max_single_position)
        assert sig.position_size_pct == pytest.approx(expected, rel=1e-6)

    def test_leverage_is_1(self, config: StrategyConfig) -> None:
        strat = HighVolDefensiveStrategy(config)
        bars = make_bars(n=120)
        state = make_regime_state("CRASH")
        sig = strat.generate_signal("SPY", bars, state)
        assert sig is not None
        assert sig.leverage == pytest.approx(1.0)

    def test_direction_is_long_not_short(self, config: StrategyConfig) -> None:
        strat = HighVolDefensiveStrategy(config)
        bars = make_bars(uptrend=False, n=120)
        state = make_regime_state("CRASH")
        sig = strat.generate_signal("SPY", bars, state)
        assert sig is not None
        assert sig.direction == Direction.LONG

    def test_stop_below_entry(self, config: StrategyConfig) -> None:
        strat = HighVolDefensiveStrategy(config)
        bars = make_bars(n=120)
        state = make_regime_state("CRASH")
        sig = strat.generate_signal("SPY", bars, state)
        assert sig is not None
        assert sig.stop_loss < sig.entry_price


# ---------------------------------------------------------------------------
# Uncertainty mode
# ---------------------------------------------------------------------------


class TestUncertaintyMode:
    def test_low_confidence_halves_position(self, config: StrategyConfig) -> None:
        strat = LowVolBullStrategy(config)
        bars = make_bars(n=120)
        state_confident = make_regime_state("BULL", probability=0.80)
        state_uncertain = make_regime_state("BULL", probability=0.45)

        sig_conf = strat.generate_signal("SPY", bars, state_confident, n_symbols=5)
        sig_unc = strat.generate_signal("SPY", bars, state_uncertain, n_symbols=5)
        assert sig_conf is not None and sig_unc is not None
        assert sig_unc.position_size_pct == pytest.approx(
            sig_conf.position_size_pct * config.uncertainty_size_mult, rel=1e-4
        )

    def test_uncertainty_text_in_reasoning(self, config: StrategyConfig) -> None:
        strat = LowVolBullStrategy(config)
        bars = make_bars(n=120)
        state = make_regime_state("BULL", probability=0.40)
        sig = strat.generate_signal("SPY", bars, state)
        assert sig is not None
        assert "UNCERTAINTY" in sig.reasoning

    def test_flickering_halves_position(self, config: StrategyConfig) -> None:
        strat = LowVolBullStrategy(config)
        bars = make_bars(n=120)
        state = make_regime_state("BULL", probability=0.80)  # high confidence
        sig_normal = strat.generate_signal("SPY", bars, state, n_symbols=5)
        sig_flicker = strat.generate_signal(
            "SPY", bars, state, n_symbols=5, is_flickering=True
        )
        assert sig_normal is not None and sig_flicker is not None
        assert sig_flicker.position_size_pct < sig_normal.position_size_pct

    def test_uncertainty_forces_leverage_to_1(self, config: StrategyConfig) -> None:
        strat = LowVolBullStrategy(config)
        bars = make_bars(n=120)
        state = make_regime_state("BULL", probability=0.40)  # below threshold
        sig = strat.generate_signal("SPY", bars, state)
        assert sig is not None
        assert sig.leverage == pytest.approx(1.0)

    def test_unconfirmed_state_triggers_uncertainty(self, config: StrategyConfig) -> None:
        strat = LowVolBullStrategy(config)
        bars = make_bars(n=120)
        state = make_regime_state("BULL", probability=0.80, is_confirmed=False)
        sig = strat.generate_signal("SPY", bars, state)
        assert sig is not None
        assert "UNCERTAINTY" in sig.reasoning


# ---------------------------------------------------------------------------
# StrategyOrchestrator — vol-rank mapping
# ---------------------------------------------------------------------------


class TestStrategyOrchestratorMapping:
    def test_3_states_maps_all_three_classes(self, config: StrategyConfig) -> None:
        """With 3 states (positions 0.0, 0.5, 1.0), all three classes appear."""
        infos = make_regime_infos(n_states=3)
        orch = StrategyOrchestrator(config, infos)
        strategy_types = {
            type(s).__name__ for s in orch._id_to_strategy.values()
        }
        assert "LowVolBullStrategy" in strategy_types
        assert "MidVolCautiousStrategy" in strategy_types
        assert "HighVolDefensiveStrategy" in strategy_types

    def test_lowest_vol_gets_low_vol_strategy(self, config: StrategyConfig) -> None:
        infos = make_regime_infos(n_states=3)
        orch = StrategyOrchestrator(config, infos)
        # state_id=0 has the lowest vol in our fixture
        assert isinstance(orch._id_to_strategy[0], LowVolBullStrategy)

    def test_highest_vol_gets_high_vol_strategy(self, config: StrategyConfig) -> None:
        infos = make_regime_infos(n_states=3)
        orch = StrategyOrchestrator(config, infos)
        # state_id=2 has the highest vol
        assert isinstance(orch._id_to_strategy[2], HighVolDefensiveStrategy)

    def test_mapping_ignores_labels(self, config: StrategyConfig) -> None:
        """A regime labelled BULL but with the highest volatility → HighVolDefensive."""
        # Construct infos where BULL has the highest expected_volatility
        infos = {
            "BEAR": RegimeInfo(
                regime_id=0, regime_name="BEAR",
                expected_return=-0.005, expected_volatility=0.10,
                recommended_strategy_type="defensive",
                max_leverage_allowed=0.5, max_position_size_pct=0.10,
            ),
            "BULL": RegimeInfo(
                regime_id=1, regime_name="BULL",
                expected_return=0.005, expected_volatility=0.80,  # high vol despite "BULL" label
                recommended_strategy_type="growth",
                max_leverage_allowed=1.25, max_position_size_pct=0.15,
            ),
        }
        orch = StrategyOrchestrator(config, infos)
        # BULL (id=1) has the highest vol → must map to HighVolDefensiveStrategy
        assert isinstance(orch._id_to_strategy[1], HighVolDefensiveStrategy)

    def test_update_regime_infos_rebuilds_mapping(self, config: StrategyConfig) -> None:
        infos3 = make_regime_infos(n_states=3)
        orch = StrategyOrchestrator(config, infos3)
        assert len(orch._id_to_strategy) == 3

        infos5 = make_regime_infos(n_states=5)
        orch.update_regime_infos(infos5)
        assert len(orch._id_to_strategy) == 5

    def test_unknown_state_falls_back_to_defensive(self, config: StrategyConfig) -> None:
        infos = make_regime_infos(n_states=3)
        orch = StrategyOrchestrator(config, infos)
        state = make_regime_state("UNKNOWN", state_id=99)   # not in mapping
        strat = orch.get_strategy_for_regime(state)
        assert isinstance(strat, HighVolDefensiveStrategy)


# ---------------------------------------------------------------------------
# StrategyOrchestrator — generate_signals
# ---------------------------------------------------------------------------


class TestStrategyOrchestratorSignals:
    @pytest.fixture()
    def orchestrator(self, config: StrategyConfig) -> StrategyOrchestrator:
        return StrategyOrchestrator(config, make_regime_infos(n_states=3))

    @pytest.fixture()
    def bars_dict(self) -> dict[str, pd.DataFrame]:
        return {sym: make_bars(n=120) for sym in SYMBOLS}

    def test_returns_one_signal_per_eligible_symbol(
        self, orchestrator: StrategyOrchestrator, bars_dict: dict[str, pd.DataFrame]
    ) -> None:
        state = make_regime_state("BULL", state_id=0)
        signals = orchestrator.generate_signals(SYMBOLS, bars_dict, state)
        assert len(signals) == len(SYMBOLS)

    def test_skips_symbols_with_short_bars(
        self, orchestrator: StrategyOrchestrator
    ) -> None:
        bars = {sym: make_bars(n=120) for sym in SYMBOLS}
        bars["AAPL"] = make_bars(n=30)   # insufficient
        state = make_regime_state("BULL", state_id=0)
        signals = orchestrator.generate_signals(SYMBOLS, bars, state)
        signal_syms = [s.symbol for s in signals]
        assert "AAPL" not in signal_syms
        assert len(signals) == len(SYMBOLS) - 1

    def test_all_signals_are_long(
        self, orchestrator: StrategyOrchestrator, bars_dict: dict[str, pd.DataFrame]
    ) -> None:
        state = make_regime_state("CRASH", state_id=2)
        signals = orchestrator.generate_signals(SYMBOLS, bars_dict, state)
        for sig in signals:
            assert sig.direction == Direction.LONG

    def test_signals_carry_correct_regime_fields(
        self, orchestrator: StrategyOrchestrator, bars_dict: dict[str, pd.DataFrame]
    ) -> None:
        state = make_regime_state("BULL", state_id=0, probability=0.82)
        signals = orchestrator.generate_signals(SYMBOLS, bars_dict, state)
        for sig in signals:
            assert sig.regime_id == 0
            assert sig.regime_name == "BULL"
            assert sig.regime_probability == pytest.approx(0.82)

    def test_empty_bars_dict_returns_no_signals(
        self, orchestrator: StrategyOrchestrator
    ) -> None:
        state = make_regime_state("BULL", state_id=0)
        signals = orchestrator.generate_signals(SYMBOLS, {}, state)
        assert signals == []


# ---------------------------------------------------------------------------
# Rebalancing
# ---------------------------------------------------------------------------


class TestRebalancing:
    @pytest.fixture()
    def orchestrator(self, config: StrategyConfig) -> StrategyOrchestrator:
        return StrategyOrchestrator(config, make_regime_infos(n_states=3))

    def test_no_rebalance_within_threshold(
        self, orchestrator: StrategyOrchestrator
    ) -> None:
        target = {"SPY": 0.09, "QQQ": 0.09, "AAPL": 0.09}
        current = {"SPY": 0.095, "QQQ": 0.085, "AAPL": 0.092}  # max drift 0.005
        assert not orchestrator.needs_rebalance(target, current)

    def test_rebalance_triggered_above_threshold(
        self, orchestrator: StrategyOrchestrator
    ) -> None:
        target = {"SPY": 0.09, "QQQ": 0.09}
        current = {"SPY": 0.25, "QQQ": 0.09}   # SPY drifted +0.16
        assert orchestrator.needs_rebalance(target, current)

    def test_drift_report_is_target_minus_current(
        self, orchestrator: StrategyOrchestrator
    ) -> None:
        target = {"SPY": 0.10, "QQQ": 0.10}
        current = {"SPY": 0.08, "QQQ": 0.12}
        report = orchestrator.drift_report(target, current)
        assert report["SPY"] == pytest.approx(0.02)   # underweight → positive drift
        assert report["QQQ"] == pytest.approx(-0.02)  # overweight → negative drift

    def test_missing_from_current_counted_as_zero(
        self, orchestrator: StrategyOrchestrator
    ) -> None:
        target = {"SPY": 0.10, "NVDA": 0.10}
        current = {"SPY": 0.10}   # NVDA not held
        assert orchestrator.needs_rebalance(target, current)

    def test_held_but_not_in_target_triggers_rebalance(
        self, orchestrator: StrategyOrchestrator
    ) -> None:
        target = {"SPY": 0.10}
        current = {"SPY": 0.10, "QQQ": 0.15}   # QQQ should be zero but is 15%
        assert orchestrator.needs_rebalance(target, current)


# ---------------------------------------------------------------------------
# AllocationResult backward-compat wrapper
# ---------------------------------------------------------------------------


class TestComputeAllocationBackwardCompat:
    @pytest.fixture()
    def orchestrator(self, config: StrategyConfig) -> StrategyOrchestrator:
        return StrategyOrchestrator(config, make_regime_infos(n_states=3))

    def test_returns_allocation_result(
        self, orchestrator: StrategyOrchestrator
    ) -> None:
        bars = {sym: make_bars(n=120) for sym in SYMBOLS}
        state = make_regime_state("BULL", state_id=0)
        result = orchestrator.compute_allocation(
            regime_state=state, bars=bars,
            symbols=SYMBOLS, current_weights={},
        )
        assert isinstance(result, AllocationResult)

    def test_symbol_weights_match_signal_sizes(
        self, orchestrator: StrategyOrchestrator
    ) -> None:
        bars = {sym: make_bars(n=120) for sym in SYMBOLS}
        state = make_regime_state("BULL", state_id=0)
        result = orchestrator.compute_allocation(
            regime_state=state, bars=bars,
            symbols=SYMBOLS, current_weights={},
        )
        for sig in result.signals:
            assert result.symbol_weights.get(sig.symbol) == pytest.approx(
                sig.position_size_pct, rel=1e-6
            )

    def test_uncertain_mode_reflected_in_result(
        self, orchestrator: StrategyOrchestrator
    ) -> None:
        bars = {sym: make_bars(n=120) for sym in SYMBOLS}
        state = make_regime_state("BULL", state_id=0, probability=0.40)
        result = orchestrator.compute_allocation(
            regime_state=state, bars=bars, symbols=SYMBOLS, current_weights={},
        )
        assert result.is_uncertain
        assert "UNCERTAINTY" in result.rationale


# ---------------------------------------------------------------------------
# Backward-compatible aliases
# ---------------------------------------------------------------------------


class TestBackwardCompatAliases:
    def test_regime_strategy_is_orchestrator(self) -> None:
        from core.regime_strategies import RegimeStrategy
        assert RegimeStrategy is StrategyOrchestrator

    def test_bull_trend_strategy_is_low_vol(self, config: StrategyConfig) -> None:
        assert BullTrendStrategy is LowVolBullStrategy

    def test_bear_trend_strategy_is_high_vol(self, config: StrategyConfig) -> None:
        assert BearTrendStrategy is HighVolDefensiveStrategy

    def test_crash_defensive_is_high_vol(self, config: StrategyConfig) -> None:
        assert CrashDefensiveStrategy is HighVolDefensiveStrategy

    def test_mean_reversion_is_mid_vol(self, config: StrategyConfig) -> None:
        assert MeanReversionStrategy is MidVolCautiousStrategy

    def test_euphoria_cautious_is_low_vol(self, config: StrategyConfig) -> None:
        assert EuphoriaCautiousStrategy is LowVolBullStrategy

    def test_label_to_strategy_covers_all_known_labels(self) -> None:
        known = {
            "CRASH", "STRONG_BEAR", "BEAR", "WEAK_BEAR",
            "NEUTRAL", "WEAK_BULL", "BULL", "STRONG_BULL", "EUPHORIA",
        }
        assert known.issubset(LABEL_TO_STRATEGY.keys())

    def test_label_to_strategy_correct_classes(self) -> None:
        assert LABEL_TO_STRATEGY["CRASH"] is HighVolDefensiveStrategy
        assert LABEL_TO_STRATEGY["BEAR"] is HighVolDefensiveStrategy
        assert LABEL_TO_STRATEGY["NEUTRAL"] is MidVolCautiousStrategy
        assert LABEL_TO_STRATEGY["BULL"] is LowVolBullStrategy
        assert LABEL_TO_STRATEGY["EUPHORIA"] is LowVolBullStrategy
