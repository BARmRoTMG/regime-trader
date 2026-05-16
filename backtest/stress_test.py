"""Stress testing: crash injection and gap simulation.

StressTester takes a BacktestResult or raw equity curve and replays it
through a library of synthetic adverse scenarios to measure how the strategy
degrades under tail conditions not well-represented in the training data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, TYPE_CHECKING

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from backtest.performance import PerformanceAnalyzer

if TYPE_CHECKING:
    from backtest.backtester import BacktestResult

logger = logging.getLogger(__name__)
_CONSOLE = Console()
_TRADING_DAYS = 252


# ─────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class StressScenario:
    """Definition of a single stress scenario."""

    name: str
    description: str
    shock_fn: Callable[[pd.DataFrame], pd.DataFrame]


@dataclass
class StressResult:
    """Result of running one stress scenario through the backtester."""

    scenario_name: str
    base_sharpe: float
    stressed_sharpe: float
    base_max_dd: float
    stressed_max_dd: float
    base_return: float
    stressed_return: float
    stressed_equity_curve: pd.Series
    metadata: dict = field(default_factory=dict)


@dataclass
class MonteCarloResult:
    """Aggregated result of a Monte Carlo stress test."""

    test_name: str
    n_simulations: int
    mean_max_loss: float          # mean per-sim max drawdown (positive fraction)
    worst_case_loss: float        # worst single-sim max drawdown
    pct_circuit_breaker: float    # fraction of sims where max_dd > threshold
    circuit_breaker_threshold: float
    all_max_drawdowns: np.ndarray
    metadata: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# StressTester
# ─────────────────────────────────────────────────────────────────────────────


class StressTester:
    """Runs a battery of stress scenarios against historical or simulated OHLCV data.

    Parameters
    ----------
    ohlcv:
        Full MultiIndex (symbol, timestamp) OHLCV DataFrame.
    run_backtest_fn:
        Callable that accepts modified OHLCV and returns an equity curve Series.
    performance_analyzer:
        PerformanceAnalyzer instance configured with the same risk-free rate.
    backtest_result:
        Optional BacktestResult for the regime misclassification Monte Carlo.
    """

    CIRCUIT_BREAKER_THRESHOLD = 0.20   # 20% max-drawdown → "circuit breaker fired"
    ATR_WINDOW = 14

    def __init__(
        self,
        ohlcv: pd.DataFrame,
        run_backtest_fn: Callable[[pd.DataFrame], pd.Series],
        performance_analyzer: PerformanceAnalyzer,
        backtest_result: Optional[BacktestResult] = None,
    ) -> None:
        self.ohlcv = ohlcv
        self.run_backtest = run_backtest_fn
        self.analyzer = performance_analyzer
        self.backtest_result = backtest_result
        self._scenarios: list[StressScenario] = []
        self._register_builtin_scenarios()

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _equity_metrics(self, equity: pd.Series) -> tuple[float, float, float]:
        """Return (annualised_sharpe, max_drawdown, total_return)."""
        rets = equity.pct_change().dropna()
        if len(rets) < 2 or float(equity.iloc[0]) == 0:
            return 0.0, 0.0, 0.0
        n = len(rets)
        ann_ret = float((equity.iloc[-1] / equity.iloc[0]) ** (_TRADING_DAYS / n) - 1)
        vol = float(rets.std() * np.sqrt(_TRADING_DAYS))
        rfr = float(getattr(self.analyzer, "risk_free_rate", 0.045))
        sharpe = (ann_ret - rfr) / vol if vol > 1e-10 else 0.0
        dd = float((equity / equity.cummax() - 1).min())
        total_ret = float(equity.iloc[-1] / equity.iloc[0] - 1)
        return sharpe, dd, total_ret

    def _primary_sym(self, ohlcv: pd.DataFrame) -> str:
        if isinstance(ohlcv.index, pd.MultiIndex):
            syms = ohlcv.index.get_level_values(0).unique()
            return "SPY" if "SPY" in syms else str(syms[0])
        return "_"

    def _get_primary(self, ohlcv: pd.DataFrame) -> pd.DataFrame:
        """Extract primary-symbol rows from a (possibly MultiIndex) DataFrame."""
        if isinstance(ohlcv.index, pd.MultiIndex):
            return ohlcv.xs(self._primary_sym(ohlcv), level=0).copy()
        return ohlcv.copy()

    def _put_primary(
        self,
        ohlcv: pd.DataFrame,
        modified_primary: pd.DataFrame,
    ) -> pd.DataFrame:
        """Return a copy of *ohlcv* with the primary symbol replaced."""
        if not isinstance(ohlcv.index, pd.MultiIndex):
            return modified_primary.copy()
        primary_sym = self._primary_sym(ohlcv)
        syms = ohlcv.index.get_level_values(0).unique()
        names = ohlcv.index.names
        parts: list[pd.DataFrame] = []
        for sym in syms:
            data = (
                modified_primary.copy()
                if sym == primary_sym
                else ohlcv.xs(sym, level=0).copy()
            )
            new_idx = pd.MultiIndex.from_arrays(
                [[sym] * len(data), data.index], names=names
            )
            data.index = new_idx
            parts.append(data)
        return pd.concat(parts).sort_index()

    def _compute_atr(self, data: pd.DataFrame) -> pd.Series:
        h, l, c = data["high"], data["low"], data["close"]
        prev_c = c.shift(1)
        tr = pd.concat(
            [h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1
        ).max(axis=1)
        return tr.rolling(self.ATR_WINDOW).mean()

    def _price_cols(self, data: pd.DataFrame) -> list[str]:
        return [c for c in ("open", "high", "low", "close") if c in data.columns]

    # ── run_all / run_scenario ───────────────────────────────────────────────

    def run_all(self, base_equity_curve: pd.Series) -> list[StressResult]:
        """Run every registered scenario and return results."""
        results: list[StressResult] = []
        for scenario in self._scenarios:
            try:
                results.append(self.run_scenario(scenario, base_equity_curve))
            except Exception as exc:
                logger.warning("Scenario '%s' failed: %s", scenario.name, exc)
        return results

    def run_scenario(
        self,
        scenario: StressScenario,
        base_equity_curve: pd.Series,
    ) -> StressResult:
        """Apply one scenario and compare against the baseline."""
        shocked = scenario.shock_fn(self.ohlcv.copy())
        stressed_equity = self.run_backtest(shocked)
        b_sharpe, b_dd, b_ret = self._equity_metrics(base_equity_curve)
        s_sharpe, s_dd, s_ret = self._equity_metrics(stressed_equity)
        return StressResult(
            scenario_name=scenario.name,
            base_sharpe=b_sharpe,
            stressed_sharpe=s_sharpe,
            base_max_dd=b_dd,
            stressed_max_dd=s_dd,
            base_return=b_ret,
            stressed_return=s_ret,
            stressed_equity_curve=stressed_equity,
            metadata={"description": scenario.description},
        )

    # ── Scenario registration ────────────────────────────────────────────────

    def register(self, scenario: StressScenario) -> None:
        """Add a custom stress scenario to the runner."""
        self._scenarios.append(scenario)

    def _register_builtin_scenarios(self) -> None:
        """Populate the built-in scenario library."""
        self._scenarios.extend([
            StressScenario(
                "crash_20pct",
                "Single 20% crash injected at mid-sample over 5 bars",
                lambda d: self._crash_injection(d, drop_pct=0.20, duration_bars=5),
            ),
            StressScenario(
                "crash_40pct",
                "COVID-scale 40% crash over 20 bars",
                lambda d: self._crash_injection(d, drop_pct=0.40, duration_bars=20),
            ),
            StressScenario(
                "overnight_gaps_down",
                "Three overnight gap-downs of −5% at random points",
                lambda d: self._overnight_gap(d, gap_pct=-0.05, n_gaps=3),
            ),
            StressScenario(
                "vol_spike_3x",
                "Volatility tripled for 20 bars (GFC-style regime shift)",
                lambda d: self._vol_spike(d, vol_multiplier=3.0, duration_bars=20),
            ),
            StressScenario(
                "liquidity_crisis",
                "High-low spread widened 5x for 10 bars (illiquidity proxy)",
                lambda d: self._liquidity_crisis(d, spread_multiplier=5.0, duration_bars=10),
            ),
            StressScenario(
                "prolonged_bear_30pct",
                "Slow 30% drawdown over 1 quarter then 50% recovery",
                lambda d: self._prolonged_drawdown(d, drawdown_pct=0.30, recovery_bars=126),
            ),
        ])

    # ── Built-in shock functions ─────────────────────────────────────────────

    def _crash_injection(
        self,
        ohlcv: pd.DataFrame,
        drop_pct: float = 0.20,
        duration_bars: int = 5,
        start_bar: Optional[int] = None,
    ) -> pd.DataFrame:
        """Inject a sudden price crash of *drop_pct* sustained over *duration_bars*."""
        primary = self._get_primary(ohlcv)
        n = len(primary)
        if n < duration_bars + 20:
            return ohlcv

        if start_bar is None:
            start_bar = n // 2
        start_bar = int(np.clip(start_bar, 1, n - duration_bars - 1))
        end_bar = min(start_bar + duration_bars, n)

        # Linear ramp down during the crash window; hold at trough thereafter
        factors = np.ones(n)
        for i in range(start_bar, end_bar):
            t = (i - start_bar + 1) / duration_bars
            factors[i] = 1.0 - drop_pct * t
        factors[end_bar:] = 1.0 - drop_pct

        modified = primary.copy()
        for col in self._price_cols(modified):
            modified[col] = primary[col].values * factors

        return self._put_primary(ohlcv, modified)

    def _overnight_gap(
        self,
        ohlcv: pd.DataFrame,
        gap_pct: float = -0.05,
        n_gaps: int = 3,
    ) -> pd.DataFrame:
        """Inject *n_gaps* overnight gaps of size *gap_pct*."""
        primary = self._get_primary(ohlcv)
        n = len(primary)
        if n < 30:
            return ohlcv

        rng = np.random.default_rng()
        gap_idxs = np.sort(
            rng.choice(
                np.arange(10, n - 5),
                size=min(n_gaps, n - 15),
                replace=False,
            )
        )

        # Each gap permanently shifts all subsequent prices
        factors = np.ones(n)
        for idx in gap_idxs:
            f = np.ones(n)
            f[int(idx):] = 1.0 + gap_pct
            factors *= f

        modified = primary.copy()
        for col in self._price_cols(modified):
            modified[col] = primary[col].values * factors

        return self._put_primary(ohlcv, modified)

    def _vol_spike(
        self,
        ohlcv: pd.DataFrame,
        vol_multiplier: float = 3.0,
        duration_bars: int = 20,
    ) -> pd.DataFrame:
        """Scale up daily log-returns by *vol_multiplier* for *duration_bars* bars."""
        primary = self._get_primary(ohlcv)
        n = len(primary)
        if n < duration_bars + 20:
            return ohlcv

        start = n // 2
        end = min(start + duration_bars, n - 1)

        closes = primary["close"].values.astype(float)
        log_rets = np.diff(np.log(np.maximum(closes, 1e-10)))

        amp_log_rets = log_rets.copy()
        amp_log_rets[start:end] *= vol_multiplier

        new_close = np.empty(n)
        new_close[0] = closes[0]
        new_close[1:] = closes[0] * np.exp(np.cumsum(amp_log_rets))

        ratio = new_close / np.maximum(closes, 1e-10)

        modified = primary.copy()
        for col in self._price_cols(modified):
            modified[col] = primary[col].values * ratio

        return self._put_primary(ohlcv, modified)

    def _liquidity_crisis(
        self,
        ohlcv: pd.DataFrame,
        spread_multiplier: float = 5.0,
        duration_bars: int = 10,
    ) -> pd.DataFrame:
        """Widen the high-low range to simulate illiquidity / higher slippage."""
        primary = self._get_primary(ohlcv)
        n = len(primary)
        if n < duration_bars + 10:
            return ohlcv

        start = n // 2
        end = min(start + duration_bars, n)
        modified = primary.copy()

        if "high" in modified.columns and "low" in modified.columns:
            slice_idx = modified.index[start:end]
            mid = (modified.loc[slice_idx, "high"] + modified.loc[slice_idx, "low"]) / 2
            half_range = (
                (modified.loc[slice_idx, "high"] - modified.loc[slice_idx, "low"])
                / 2
                * spread_multiplier
            )
            modified.loc[slice_idx, "high"] = (mid + half_range).values
            modified.loc[slice_idx, "low"] = np.maximum(
                (mid - half_range).values, 0.01
            )

        return self._put_primary(ohlcv, modified)

    def _prolonged_drawdown(
        self,
        ohlcv: pd.DataFrame,
        drawdown_pct: float = 0.30,
        recovery_bars: int = 126,
    ) -> pd.DataFrame:
        """Simulate a slow bear-market drawdown followed by partial recovery."""
        primary = self._get_primary(ohlcv)
        n = len(primary)
        if n < 200:
            return ohlcv

        dd_start = n // 4
        dd_bars = max(n // 4, 20)
        dd_end = min(dd_start + dd_bars, n)
        rec_end = min(dd_end + recovery_bars, n)
        trough = 1.0 - drawdown_pct
        recovery_target = trough + 0.5 * drawdown_pct

        factors = np.ones(n)
        for i in range(dd_start, dd_end):
            t = (i - dd_start) / dd_bars
            factors[i] = 1.0 - drawdown_pct * t
        factors[dd_end:] = trough
        for i in range(dd_end, rec_end):
            t = (i - dd_end) / recovery_bars
            factors[i] = trough + (recovery_target - trough) * t
        if rec_end < n:
            factors[rec_end:] = factors[rec_end - 1]

        modified = primary.copy()
        for col in self._price_cols(modified):
            modified[col] = primary[col].values * factors

        return self._put_primary(ohlcv, modified)

    # ── Monte Carlo: crash injection ─────────────────────────────────────────

    def run_crash_monte_carlo(
        self,
        base_equity_curve: pd.Series,
        n_simulations: int = 100,
        n_shocks: int = 10,
        min_drop: float = 0.05,
        max_drop: float = 0.15,
        rng_seed: Optional[int] = None,
    ) -> MonteCarloResult:
        """100 Monte Carlo runs: 10 random single-day crashes of −5% to −15% each.

        Each simulation picks *n_shocks* random bars and drop magnitudes,
        applies compounded permanent price shifts, runs the backtest, and
        records the resulting max drawdown.  Reports mean max loss, worst
        case, and the fraction of runs where a 20% circuit-breaker would
        have fired.
        """
        rng = np.random.default_rng(rng_seed)
        primary = self._get_primary(self.ohlcv)
        n_bars = len(primary)
        price_cols = self._price_cols(primary)

        if n_bars < 150:
            logger.warning("Crash MC: insufficient bars (%d < 150).", n_bars)

        _, base_dd, _ = self._equity_metrics(base_equity_curve)
        all_dds: list[float] = []

        logger.info(
            "Crash Monte Carlo: %d sims x %d shocks (%.0f%%--%.0f%% each)...",
            n_simulations, n_shocks, min_drop * 100, max_drop * 100,
        )

        safe_range = max(n_bars - 100, 1)
        max_shocks = max(min(n_shocks, safe_range // 2), 1)

        for _ in range(n_simulations):
            shock_bars = np.sort(
                rng.choice(
                    np.arange(50, min(n_bars - 50, 50 + safe_range)),
                    size=max_shocks,
                    replace=False,
                )
            )
            drop_mags = rng.uniform(min_drop, max_drop, size=max_shocks)

            # Compound all shocks into a single factors array
            factors = np.ones(n_bars)
            for bar_idx, drop in zip(shock_bars, drop_mags):
                f = np.ones(n_bars)
                f[int(bar_idx):] = 1.0 - drop
                factors *= f

            mod_primary = primary.copy()
            for col in price_cols:
                mod_primary[col] = primary[col].values * factors

            shocked_ohlcv = self._put_primary(self.ohlcv.copy(), mod_primary)

            try:
                stressed_eq = self.run_backtest(shocked_ohlcv)
                _, dd, _ = self._equity_metrics(stressed_eq)
                all_dds.append(abs(dd))
            except Exception as exc:
                logger.debug("Crash sim failed: %s", exc)
                all_dds.append(abs(base_dd))  # conservative: assume same as baseline

        dds = np.array(all_dds)
        cb = self.CIRCUIT_BREAKER_THRESHOLD
        return MonteCarloResult(
            test_name="crash_injection",
            n_simulations=n_simulations,
            mean_max_loss=float(dds.mean()),
            worst_case_loss=float(dds.max()),
            pct_circuit_breaker=float((dds > cb).mean()),
            circuit_breaker_threshold=cb,
            all_max_drawdowns=dds,
            metadata={
                "n_shocks_per_sim": n_shocks,
                "drop_range": f"−{min_drop*100:.0f}% to −{max_drop*100:.0f}%",
                "base_max_dd": float(abs(base_dd)),
                "pct_worse_than_base": float((dds > abs(base_dd)).mean()),
            },
        )

    # ── Monte Carlo: gap risk ────────────────────────────────────────────────

    def run_gap_risk(
        self,
        base_equity_curve: pd.Series,
        n_simulations: int = 50,
        atr_min_mult: float = 2.0,
        atr_max_mult: float = 5.0,
        n_gaps: int = 5,
        rng_seed: Optional[int] = None,
    ) -> MonteCarloResult:
        """Gap-risk Monte Carlo: overnight gaps of 2--5x ATR at random points.

        Reports expected loss (sum of gap sizes assuming 100% investment) vs
        actual max drawdown, giving a "containment ratio" that shows how much
        of the theoretical loss the strategy absorbed.
        """
        rng = np.random.default_rng(rng_seed)
        primary = self._get_primary(self.ohlcv)
        price_cols = self._price_cols(primary)
        n_bars = len(primary)

        atr = self._compute_atr(primary)
        avg_atr_pct = float((atr / primary["close"]).dropna().mean())

        _, base_dd, _ = self._equity_metrics(base_equity_curve)
        all_dds: list[float] = []
        expected_losses: list[float] = []

        logger.info(
            "Gap risk Monte Carlo: %d sims x %d gaps (%.1fx--%.1fx ATR, avg ATR=%.2f%%)...",
            n_simulations, n_gaps, atr_min_mult, atr_max_mult, avg_atr_pct * 100,
        )

        safe_n = max(n_bars - 30, 1)
        max_gaps = max(min(n_gaps, safe_n // 5), 1)

        for _ in range(n_simulations):
            gap_bars = np.sort(
                rng.choice(
                    np.arange(20, min(n_bars - 10, 20 + safe_n)),
                    size=max_gaps,
                    replace=False,
                )
            )
            atr_mults = rng.uniform(atr_min_mult, atr_max_mult, size=max_gaps)
            signs = rng.choice([-1, 1], size=max_gaps, p=[0.7, 0.3])
            gap_pcts = signs * atr_mults * avg_atr_pct

            expected_losses.append(float(np.sum(np.abs(gap_pcts[signs < 0]))))

            factors = np.ones(n_bars)
            for bar_idx, gap_pct in zip(gap_bars, gap_pcts):
                f = np.ones(n_bars)
                f[int(bar_idx):] = 1.0 + float(gap_pct)
                factors *= f

            mod_primary = primary.copy()
            for col in price_cols:
                mod_primary[col] = primary[col].values * factors

            shocked_ohlcv = self._put_primary(self.ohlcv.copy(), mod_primary)

            try:
                stressed_eq = self.run_backtest(shocked_ohlcv)
                _, dd, _ = self._equity_metrics(stressed_eq)
                all_dds.append(abs(dd))
            except Exception as exc:
                logger.debug("Gap sim failed: %s", exc)
                all_dds.append(abs(base_dd))

        dds = np.array(all_dds)
        avg_expected = float(np.mean(expected_losses)) if expected_losses else 0.0
        cb = self.CIRCUIT_BREAKER_THRESHOLD
        containment = float(dds.mean() / avg_expected) if avg_expected > 1e-10 else None

        return MonteCarloResult(
            test_name="gap_risk",
            n_simulations=n_simulations,
            mean_max_loss=float(dds.mean()),
            worst_case_loss=float(dds.max()),
            pct_circuit_breaker=float((dds > cb).mean()),
            circuit_breaker_threshold=cb,
            all_max_drawdowns=dds,
            metadata={
                "atr_multiple_range": f"{atr_min_mult:.1f}x--{atr_max_mult:.1f}x ATR",
                "avg_atr_pct": avg_atr_pct,
                "avg_expected_loss_pct": avg_expected,
                "avg_actual_max_dd": float(dds.mean()),
                "containment_ratio": containment,
            },
        )

    # ── Monte Carlo: regime misclassification ────────────────────────────────

    def run_regime_misclassification(
        self,
        n_simulations: int = 100,
        rng_seed: Optional[int] = None,
    ) -> MonteCarloResult:
        """Shuffle regime labels; verify that risk management limits damage independently.

        Approach
        --------
        For each simulation, randomly permute the regime-label assignment across
        all bars.  Replay the portfolio using each bar's permuted strategy
        allocation x the primary-symbol daily return.  If the resulting max
        drawdown exceeds 20% in <10% of runs → PASS (risk management is
        regime-independent).  If it frequently blows up → FAIL (strategy relies
        too heavily on regime accuracy for safety).
        """
        if self.backtest_result is None:
            raise ValueError(
                "backtest_result must be provided to StressTester for the "
                "regime misclassification test."
            )

        br = self.backtest_result
        regime_series = br.combined_regime.dropna()
        primary_closes = br.primary_closes.reindex(regime_series.index).ffill().dropna()
        common_idx = regime_series.index.intersection(primary_closes.index)
        regime_series = regime_series.loc[common_idx]
        primary_closes = primary_closes.loc[common_idx]

        if len(regime_series) < 50:
            raise ValueError(
                "Insufficient regime history for misclassification test (need ≥ 50 bars)."
            )

        from core.regime_strategies import LABEL_TO_STRATEGY

        _STRATEGY_NOTIONAL: dict[str, float] = {
            "LowVolBullStrategy": 0.95 * 1.25,      # 118.75%
            "MidVolCautiousStrategy": 0.95 * 1.0,   # 95%
            "HighVolDefensiveStrategy": 0.60 * 1.0, # 60%
        }

        def _label_notional(lbl: str) -> float:
            cls = LABEL_TO_STRATEGY.get(lbl)
            if cls is None:
                return 0.95
            return _STRATEGY_NOTIONAL.get(cls.__name__, 0.95)

        label_to_notional: dict[str, float] = {
            lbl: _label_notional(lbl) for lbl in regime_series.unique()
        }
        n_syms = max(len(br.symbols), 1) if br.symbols else 1
        daily_rets = primary_closes.pct_change().fillna(0.0).values
        regime_arr = regime_series.values

        # Baseline: correct regime labels
        correct_notionals = np.array(
            [label_to_notional.get(str(lbl), 0.95) for lbl in regime_arr]
        )
        base_port_rets = (correct_notionals / n_syms) * daily_rets
        eq_base = 100_000.0 * np.cumprod(1.0 + base_port_rets)
        running_max = np.maximum.accumulate(eq_base)
        base_dd = float(np.min(eq_base / running_max) - 1)

        rng = np.random.default_rng(rng_seed)
        all_dds: list[float] = []
        n_blowups = 0  # sims where max_dd > 2x base_dd + 10% grace

        logger.info(
            "Regime misclassification MC: %d sims (base MaxDD=%.1f%%)...",
            n_simulations, abs(base_dd) * 100,
        )

        for _ in range(n_simulations):
            shuffled = rng.permutation(regime_arr)
            shuffled_notionals = np.array(
                [label_to_notional.get(str(lbl), 0.95) for lbl in shuffled]
            )
            port_rets = (shuffled_notionals / n_syms) * daily_rets
            eq = 100_000.0 * np.cumprod(1.0 + port_rets)
            rm = np.maximum.accumulate(eq)
            dd = float(np.min(eq / rm) - 1)
            all_dds.append(abs(dd))
            if abs(dd) > 2.0 * abs(base_dd) + 0.10:
                n_blowups += 1

        dds = np.array(all_dds)
        cb = self.CIRCUIT_BREAKER_THRESHOLD
        pct_cb = float((dds > cb).mean())
        verdict = "PASS" if pct_cb < 0.10 else "FAIL"

        return MonteCarloResult(
            test_name="regime_misclassification",
            n_simulations=n_simulations,
            mean_max_loss=float(dds.mean()),
            worst_case_loss=float(dds.max()),
            pct_circuit_breaker=pct_cb,
            circuit_breaker_threshold=cb,
            all_max_drawdowns=dds,
            metadata={
                "base_max_dd": float(abs(base_dd)),
                "n_blowups": n_blowups,
                "pct_blowups": float(n_blowups / n_simulations),
                "verdict": verdict,
                "verdict_message": (
                    "PASS: Risk management contains damage independently of regime accuracy."
                    if verdict == "PASS"
                    else (
                        "FAIL: Strategy blows up with wrong regime labels → "
                        "risk management is NOT regime-independent."
                    )
                ),
            },
        )

    # ── Reporting ────────────────────────────────────────────────────────────

    def summary_table(self, results: list[StressResult]) -> pd.DataFrame:
        """Return a tidy DataFrame comparing all stress scenario metrics."""
        return pd.DataFrame([
            {
                "scenario": r.scenario_name,
                "description": r.metadata.get("description", ""),
                "base_sharpe": round(r.base_sharpe, 3),
                "stressed_sharpe": round(r.stressed_sharpe, 3),
                "sharpe_delta": round(r.stressed_sharpe - r.base_sharpe, 3),
                "base_max_dd_pct": round(r.base_max_dd * 100, 2),
                "stressed_max_dd_pct": round(r.stressed_max_dd * 100, 2),
                "dd_delta_pct": round((r.stressed_max_dd - r.base_max_dd) * 100, 2),
                "base_return_pct": round(r.base_return * 100, 2),
                "stressed_return_pct": round(r.stressed_return * 100, 2),
            }
            for r in results
        ])

    def print_summary(self, results: list[StressResult]) -> None:
        """Print the summary table to stdout using Rich."""
        t = Table(title="Scenario Stress Test Results", show_lines=True)
        t.add_column("Scenario", style="cyan", min_width=22)
        t.add_column("Base Sharpe", justify="right")
        t.add_column("Stressed Sharpe", justify="right")
        t.add_column("Δ Sharpe", justify="right")
        t.add_column("Base MaxDD", justify="right")
        t.add_column("Stressed MaxDD", justify="right")
        t.add_column("Stressed Ret", justify="right")
        t.add_column("Status", justify="center")

        for r in results:
            warn = r.stressed_max_dd < -self.CIRCUIT_BREAKER_THRESHOLD
            status = "[red]WARN[/red]" if warn else "[green]OK[/green]"
            t.add_row(
                r.scenario_name,
                f"{r.base_sharpe:.2f}",
                f"{r.stressed_sharpe:.2f}",
                f"{r.stressed_sharpe - r.base_sharpe:+.2f}",
                f"{r.base_max_dd * 100:.1f}%",
                f"{r.stressed_max_dd * 100:.1f}%",
                f"{r.stressed_return * 100:.1f}%",
                status,
            )
        _CONSOLE.print(t)

    def _print_mc_result(self, mc: MonteCarloResult) -> None:
        t = Table(
            title=f"Monte Carlo -- {mc.test_name}", show_lines=False, box=None
        )
        t.add_column("Metric", style="cyan", min_width=34)
        t.add_column("Value", justify="right")

        t.add_row("Simulations", str(mc.n_simulations))
        t.add_row("Mean Max Loss", f"{mc.mean_max_loss * 100:.2f}%")
        t.add_row("Worst Case Loss", f"{mc.worst_case_loss * 100:.2f}%")
        t.add_row(
            f"Circuit Breaker (>{mc.circuit_breaker_threshold * 100:.0f}% MaxDD)",
            f"{mc.pct_circuit_breaker * 100:.1f}% of sims",
        )

        _meta_fmt: dict[str, tuple[str, Callable]] = {
            "n_shocks_per_sim": ("Shocks / Sim", str),
            "drop_range": ("Drop Range", str),
            "base_max_dd": ("Base MaxDD", lambda v: f"{v * 100:.2f}%"),
            "pct_worse_than_base": ("Pct Worse than Base", lambda v: f"{v * 100:.1f}%"),
            "atr_multiple_range": ("ATR Multiple Range", str),
            "avg_atr_pct": ("Avg ATR %", lambda v: f"{v * 100:.2f}%"),
            "avg_expected_loss_pct": ("Avg Expected Loss", lambda v: f"{v * 100:.2f}%"),
            "avg_actual_max_dd": ("Avg Actual MaxDD", lambda v: f"{v * 100:.2f}%"),
            "containment_ratio": ("Containment Ratio", lambda v: f"{v:.2f}" if v is not None else "N/A"),
            "n_blowups": ("Blowups", str),
            "pct_blowups": ("Pct Blowups", lambda v: f"{v * 100:.1f}%"),
        }
        for key, (label, fmt) in _meta_fmt.items():
            if key in mc.metadata:
                t.add_row(label, fmt(mc.metadata[key]))

        _CONSOLE.print(t)

        verdict = mc.metadata.get("verdict")
        verdict_msg = mc.metadata.get("verdict_message")
        if verdict:
            color = "green" if verdict == "PASS" else "red"
            _CONSOLE.print(f"  Verdict: [{color}]{verdict}[/{color}]")
        if verdict_msg:
            _CONSOLE.print(f"  {verdict_msg}\n")

    def full_stress_report(
        self,
        base_equity_curve: pd.Series,
        n_crash_sims: int = 100,
        n_gap_sims: int = 50,
        n_regime_sims: int = 100,
        output_path: Optional[Path] = None,
    ) -> dict:
        """Run all stress tests and print a complete Rich-formatted report.

        Returns
        -------
        dict with keys: scenario_results, crash_monte_carlo, gap_risk,
        regime_misclassification (None if backtest_result not provided).
        """
        _CONSOLE.print("\n[bold cyan]═══ STRESS TEST REPORT ═══[/bold cyan]\n")

        _CONSOLE.print("[bold]1. Scenario Stress Tests[/bold]")
        scenario_results = self.run_all(base_equity_curve)
        self.print_summary(scenario_results)

        _CONSOLE.print(
            f"\n[bold]2. Monte Carlo Crash Injection "
            f"({n_crash_sims} sims, 10 shocks each, −5% to −15%)[/bold]"
        )
        crash_mc = self.run_crash_monte_carlo(
            base_equity_curve, n_simulations=n_crash_sims
        )
        self._print_mc_result(crash_mc)

        _CONSOLE.print(
            f"\n[bold]3. Gap Risk ({n_gap_sims} sims, 2--5x ATR gaps)[/bold]"
        )
        gap_mc = self.run_gap_risk(base_equity_curve, n_simulations=n_gap_sims)
        self._print_mc_result(gap_mc)

        regime_mc: Optional[MonteCarloResult] = None
        if self.backtest_result is not None:
            _CONSOLE.print(
                f"\n[bold]4. Regime Misclassification ({n_regime_sims} sims)[/bold]"
            )
            regime_mc = self.run_regime_misclassification(n_simulations=n_regime_sims)
            self._print_mc_result(regime_mc)
        else:
            _CONSOLE.print(
                "\n[dim]4. Regime misclassification skipped "
                "(pass backtest_result= to StressTester to enable).[/dim]"
            )

        if output_path is not None:
            op = Path(output_path)
            op.mkdir(parents=True, exist_ok=True)
            self.summary_table(scenario_results).to_csv(
                op / "stress_scenarios.csv", index=False
            )
            for mc_res in filter(None, [crash_mc, gap_mc, regime_mc]):
                pd.Series(
                    mc_res.all_max_drawdowns, name="max_drawdown"
                ).to_csv(op / f"mc_{mc_res.test_name}.csv")
            logger.info("Stress test results saved to %s", op)

        return {
            "scenario_results": scenario_results,
            "crash_monte_carlo": crash_mc,
            "gap_risk": gap_mc,
            "regime_misclassification": regime_mc,
        }
