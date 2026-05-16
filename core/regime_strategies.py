"""Volatility-regime-based allocation strategies.

DESIGN PHILOSOPHY
-----------------
The HMM excels at detecting VOLATILITY ENVIRONMENTS, not market direction.
Stocks trend upward roughly 70 % of the time in low-volatility periods.
The worst drawdowns cluster in high-volatility spikes.  So the strategy is:

  Low vol  → be fully invested (calm markets trend up)
  Mid vol  → stay invested if trend intact, reduce if not
  High vol → reduce but stay partially invested (catch V-shaped rebounds)

The edge comes from AVOIDING BIG DRAWDOWNS through volatility-based sizing.

Volatility rank mapping (independent of regime labels)
------------------------------------------------------
After the HMM fits, we sort all states by expected_volatility (ascending):

    position = rank / (n_regimes − 1)      # 0.0 = calmest, 1.0 = most volatile
    position ≤ 0.33  →  LowVolBullStrategy
    position ≥ 0.67  →  HighVolDefensiveStrategy
    else             →  MidVolCautiousStrategy

A "BULL" label does NOT guarantee low volatility.  The orchestrator ignores
labels and uses only the volatility sort.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

from core.hmm_engine import RegimeInfo, RegimeState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Direction enum
# ---------------------------------------------------------------------------


class Direction(str, Enum):
    LONG = "LONG"
    FLAT = "FLAT"


# ---------------------------------------------------------------------------
# Signal dataclass
# ---------------------------------------------------------------------------


@dataclass
class Signal:
    """One actionable signal for a single symbol at a single bar."""

    symbol: str
    direction: Direction
    confidence: float               # regime posterior probability (0–1)
    entry_price: float
    stop_loss: float
    take_profit: Optional[float]    # None when not computed
    position_size_pct: float        # fraction of NAV to allocate to this symbol
    leverage: float                 # 1.0 or 1.25 depending on regime/strategy
    regime_id: int
    regime_name: str
    regime_probability: float
    timestamp: pd.Timestamp
    reasoning: str
    strategy_name: str
    metadata: dict = field(default_factory=dict)

    def effective_notional_pct(self) -> float:
        """position_size_pct × leverage — the gross NAV exposure for this symbol."""
        return self.position_size_pct * self.leverage


# ---------------------------------------------------------------------------
# StrategyConfig
# ---------------------------------------------------------------------------


@dataclass
class StrategyConfig:
    """Parameters consumed by all strategy classes and the orchestrator."""

    low_vol_allocation: float = 0.95
    mid_vol_allocation_trend: float = 0.95
    mid_vol_allocation_no_trend: float = 0.60
    high_vol_allocation: float = 0.60
    low_vol_leverage: float = 1.25
    rebalance_threshold: float = 0.10
    uncertainty_size_mult: float = 0.50
    min_confidence: float = 0.55
    max_single_position: float = 0.15  # hard cap regardless of equal-weight calc
    take_profit_rr_ratio: float = 2.0  # risk-reward multiple for take-profit
    ema_span: int = 50                 # EMA period for trend / stop calculations
    atr_span: int = 14                 # ATR smoothing period


# ---------------------------------------------------------------------------
# AllocationResult – kept for backward-compat with signal_generator.py
# ---------------------------------------------------------------------------


@dataclass
class AllocationResult:
    """Aggregated allocation for one bar across the full symbol universe."""

    regime: str                         # human-readable label e.g. "BULL"
    target_equity_fraction: float       # total equity fraction (0.60–0.95)
    leverage: float
    symbol_weights: dict[str, float]    # symbol → fraction of NAV
    is_uncertain: bool
    rationale: str
    signals: list[Signal] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal indicator helpers
# ---------------------------------------------------------------------------


def _compute_ema(close: pd.Series, span: int) -> float:
    """Last value of an exponential moving average."""
    return float(close.ewm(span=span, adjust=False).mean().iloc[-1])


def _compute_atr(
    high: pd.Series, low: pd.Series, close: pd.Series, span: int = 14
) -> float:
    """Wilder's ATR via EWM smoothing of the true range."""
    tr = pd.concat(
        [
            (high - low),
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return float(tr.ewm(span=span, adjust=False).mean().iloc[-1])


def _normalise_bars(bars: pd.DataFrame) -> pd.DataFrame:
    """Lower-case column names so strategies don't care about capitalisation."""
    bars = bars.copy()
    bars.columns = [c.lower() for c in bars.columns]
    return bars


_MIN_BARS = 60   # minimum history required before generating a signal


# ---------------------------------------------------------------------------
# Base strategy (ABC)
# ---------------------------------------------------------------------------


class BaseStrategy(abc.ABC):
    """Abstract base for all regime strategies.

    Every concrete strategy receives per-bar OHLCV data for one symbol and
    the current HMM RegimeState, then returns an Optional[Signal].

    Parameters
    ----------
    config:
        StrategyConfig shared across all strategy instances.
    """

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Human-readable class name surfaced in Signal.strategy_name."""
        ...

    @abc.abstractmethod
    def generate_signal(
        self,
        symbol: str,
        bars: pd.DataFrame,
        regime_state: RegimeState,
        n_symbols: int = 1,
        is_flickering: bool = False,
    ) -> Optional[Signal]:
        """Generate a trading signal for one symbol.

        Parameters
        ----------
        symbol:
            Ticker being evaluated.
        bars:
            OHLCV DataFrame (at least _MIN_BARS rows).  Column names are
            normalised to lowercase by _normalise_bars().
        regime_state:
            Current HMM output including label, probability, and stability.
        n_symbols:
            Number of symbols with sufficient data — used to divide the total
            allocation equally across the universe.
        is_flickering:
            True when the HMM flicker guard is triggered.  Causes the
            uncertainty mode to activate regardless of probability.

        Returns
        -------
        Optional[Signal]
            None when there is insufficient history or the stop math fails.
        """
        ...

    # ------------------------------------------------------------------
    # Shared helpers used by all concrete strategies
    # ------------------------------------------------------------------

    def _indicators(
        self, bars: pd.DataFrame
    ) -> tuple[float, float, float]:
        """Return (last_close, ema50, atr14) from a normalised bars DataFrame."""
        close = bars["close"]
        high = bars["high"]
        low = bars["low"]
        price = float(close.iloc[-1])
        ema = _compute_ema(close, self.config.ema_span)
        atr = _compute_atr(high, low, close, self.config.atr_span)
        return price, ema, atr

    def _per_symbol_pct(self, total_allocation: float, n_symbols: int) -> float:
        """Equal-weight per-symbol allocation, capped at max_single_position."""
        if n_symbols <= 0:
            return min(total_allocation, self.config.max_single_position)
        return min(
            total_allocation / n_symbols,
            self.config.max_single_position,
        )

    def _take_profit(self, entry: float, stop: float) -> Optional[float]:
        """Compute take-profit using the configured risk-reward ratio.

        Returns None if stop ≥ entry (degenerate case handled upstream).
        """
        risk = entry - stop
        if risk <= 0:
            return None
        return entry + self.config.take_profit_rr_ratio * risk

    def _safe_stop(self, computed_stop: float, price: float, atr: float) -> float:
        """Ensure stop is always below entry; fall back to price − 2 × ATR."""
        if computed_stop >= price:
            return price - 2.0 * atr
        return computed_stop

    def _apply_uncertainty(
        self,
        signal: Signal,
        regime_state: RegimeState,
        is_flickering: bool,
    ) -> Signal:
        """Halve position size and force leverage=1.0 when confidence is low."""
        uncertain = (
            regime_state.probability < self.config.min_confidence
            or not regime_state.is_confirmed
            or is_flickering
        )
        if uncertain:
            signal.position_size_pct *= self.config.uncertainty_size_mult
            signal.leverage = 1.0
            signal.confidence *= self.config.uncertainty_size_mult
            signal.reasoning += "  [UNCERTAINTY -- size halved]"
        return signal


# ---------------------------------------------------------------------------
# Three concrete strategies
# ---------------------------------------------------------------------------


class LowVolBullStrategy(BaseStrategy):
    """Calm-market regime: fully invested with modest leverage.

    Allocation: 95 % of portfolio
    Leverage:   1.25 ×
    Stop:       max(price − 3 × ATR,  50 EMA − 0.5 × ATR)

    This is where the bulk of returns are generated.  Calm markets trend
    upward; modest leverage accelerates compounding without significant
    additional drawdown risk.
    """

    @property
    def name(self) -> str:
        return "LowVolBullStrategy"

    def generate_signal(
        self,
        symbol: str,
        bars: pd.DataFrame,
        regime_state: RegimeState,
        n_symbols: int = 1,
        is_flickering: bool = False,
    ) -> Optional[Signal]:
        bars = _normalise_bars(bars)
        if len(bars) < _MIN_BARS:
            logger.debug("%s: insufficient bars (%d < %d).", symbol, len(bars), _MIN_BARS)
            return None

        price, ema50, atr14 = self._indicators(bars)

        # Stop: highest of the two floors (tighter of the two wins on the upside)
        raw_stop = max(price - 3.0 * atr14, ema50 - 0.5 * atr14)
        stop = self._safe_stop(raw_stop, price, atr14)
        tp = self._take_profit(price, stop)

        pos_pct = self._per_symbol_pct(self.config.low_vol_allocation, n_symbols)

        reasoning = (
            f"{self.name} | {regime_state.label} (p={regime_state.probability:.3f}) | "
            f"allocation={self.config.low_vol_allocation:.0%}  "
            f"leverage={self.config.low_vol_leverage:.2f}x | "
            f"stop={stop:.4f} [max(P-3ATR, EMA50-0.5ATR)] | "
            f"atr={atr14:.4f}  ema50={ema50:.4f}"
        )

        sig = Signal(
            symbol=symbol,
            direction=Direction.LONG,
            confidence=regime_state.probability,
            entry_price=price,
            stop_loss=stop,
            take_profit=tp,
            position_size_pct=pos_pct,
            leverage=self.config.low_vol_leverage,
            regime_id=regime_state.state_id,
            regime_name=regime_state.label,
            regime_probability=regime_state.probability,
            timestamp=regime_state.timestamp,
            reasoning=reasoning,
            strategy_name=self.name,
            metadata={
                "ema50": ema50,
                "atr14": atr14,
                "stop_floor_atr": price - 3.0 * atr14,
                "stop_floor_ema": ema50 - 0.5 * atr14,
            },
        )
        return self._apply_uncertainty(sig, regime_state, is_flickering)


class MidVolCautiousStrategy(BaseStrategy):
    """Moderate-volatility regime: stay invested while trend is intact.

    Price > 50 EMA → allocation 95 %, leverage 1.0 × (trend intact, stay in)
    Price < 50 EMA → allocation 60 %, leverage 1.0 × (trend broken, reduce)
    Stop: 50 EMA − 0.5 × ATR

    The trend filter prevents riding a deteriorating market at full size
    while still participating when momentum is positive.
    """

    @property
    def name(self) -> str:
        return "MidVolCautiousStrategy"

    def generate_signal(
        self,
        symbol: str,
        bars: pd.DataFrame,
        regime_state: RegimeState,
        n_symbols: int = 1,
        is_flickering: bool = False,
    ) -> Optional[Signal]:
        bars = _normalise_bars(bars)
        if len(bars) < _MIN_BARS:
            logger.debug("%s: insufficient bars (%d < %d).", symbol, len(bars), _MIN_BARS)
            return None

        price, ema50, atr14 = self._indicators(bars)

        trend_intact = price > ema50
        allocation = (
            self.config.mid_vol_allocation_trend
            if trend_intact
            else self.config.mid_vol_allocation_no_trend
        )
        trend_tag = "trend-intact" if trend_intact else "trend-broken"

        stop = self._safe_stop(ema50 - 0.5 * atr14, price, atr14)
        tp = self._take_profit(price, stop)
        pos_pct = self._per_symbol_pct(allocation, n_symbols)

        reasoning = (
            f"{self.name} | {regime_state.label} (p={regime_state.probability:.3f}) | "
            f"{trend_tag} → allocation={allocation:.0%}  leverage=1.0x | "
            f"stop={stop:.4f} [EMA50-0.5ATR] | "
            f"price={price:.4f}  ema50={ema50:.4f}  atr={atr14:.4f}"
        )

        sig = Signal(
            symbol=symbol,
            direction=Direction.LONG,
            confidence=regime_state.probability,
            entry_price=price,
            stop_loss=stop,
            take_profit=tp,
            position_size_pct=pos_pct,
            leverage=1.0,
            regime_id=regime_state.state_id,
            regime_name=regime_state.label,
            regime_probability=regime_state.probability,
            timestamp=regime_state.timestamp,
            reasoning=reasoning,
            strategy_name=self.name,
            metadata={
                "ema50": ema50,
                "atr14": atr14,
                "trend_intact": trend_intact,
                "allocation_used": allocation,
            },
        )
        return self._apply_uncertainty(sig, regime_state, is_flickering)


class HighVolDefensiveStrategy(BaseStrategy):
    """Turbulent-market regime: stay 60 % long to capture V-shaped rebounds.

    Allocation: 60 % of portfolio
    Leverage:   1.0 ×  (no leverage in volatile conditions)
    Stop:       50 EMA − 1.0 × ATR  (wider stop for volatile price action)
    Direction:  LONG only (not short — rebounds are swift and punishing to shorts)

    Remaining 40 % in cash provides a cushion and dry powder for adding
    exposure when the next low-vol regime is confirmed.
    """

    @property
    def name(self) -> str:
        return "HighVolDefensiveStrategy"

    def generate_signal(
        self,
        symbol: str,
        bars: pd.DataFrame,
        regime_state: RegimeState,
        n_symbols: int = 1,
        is_flickering: bool = False,
    ) -> Optional[Signal]:
        bars = _normalise_bars(bars)
        if len(bars) < _MIN_BARS:
            logger.debug("%s: insufficient bars (%d < %d).", symbol, len(bars), _MIN_BARS)
            return None

        price, ema50, atr14 = self._indicators(bars)

        # Wider stop — volatile conditions require more room to breathe
        stop = self._safe_stop(ema50 - 1.0 * atr14, price, atr14)
        tp = self._take_profit(price, stop)
        pos_pct = self._per_symbol_pct(self.config.high_vol_allocation, n_symbols)

        reasoning = (
            f"{self.name} | {regime_state.label} (p={regime_state.probability:.3f}) | "
            f"defensive allocation={self.config.high_vol_allocation:.0%}  leverage=1.0x | "
            f"stop={stop:.4f} [EMA50-1.0ATR] staying LONG for rebounds | "
            f"price={price:.4f}  ema50={ema50:.4f}  atr={atr14:.4f}"
        )

        sig = Signal(
            symbol=symbol,
            direction=Direction.LONG,
            confidence=regime_state.probability,
            entry_price=price,
            stop_loss=stop,
            take_profit=tp,
            position_size_pct=pos_pct,
            leverage=1.0,
            regime_id=regime_state.state_id,
            regime_name=regime_state.label,
            regime_probability=regime_state.probability,
            timestamp=regime_state.timestamp,
            reasoning=reasoning,
            strategy_name=self.name,
            metadata={"ema50": ema50, "atr14": atr14},
        )
        return self._apply_uncertainty(sig, regime_state, is_flickering)


# ---------------------------------------------------------------------------
# Backward-compatible aliases
# ---------------------------------------------------------------------------

CrashDefensiveStrategy = HighVolDefensiveStrategy
BearTrendStrategy = HighVolDefensiveStrategy
MeanReversionStrategy = MidVolCautiousStrategy
BullTrendStrategy = LowVolBullStrategy
EuphoriaCautiousStrategy = LowVolBullStrategy   # trim in euphoria = same sizes as bull

# Label → strategy class (informational; orchestrator uses volatility rank, not this)
LABEL_TO_STRATEGY: dict[str, type[BaseStrategy]] = {
    "CRASH":        HighVolDefensiveStrategy,
    "STRONG_BEAR":  HighVolDefensiveStrategy,
    "BEAR":         HighVolDefensiveStrategy,
    "WEAK_BEAR":    MidVolCautiousStrategy,
    "NEUTRAL":      MidVolCautiousStrategy,
    "WEAK_BULL":    MidVolCautiousStrategy,
    "BULL":         LowVolBullStrategy,
    "STRONG_BULL":  LowVolBullStrategy,
    "EUPHORIA":     LowVolBullStrategy,
}


# ---------------------------------------------------------------------------
# StrategyOrchestrator
# ---------------------------------------------------------------------------


class StrategyOrchestrator:
    """Maps each HMM regime to the appropriate strategy by VOLATILITY RANK.

    Volatility rank mapping
    -----------------------
    After the HMM fits, ``update_regime_infos()`` receives the RegimeInfo dict
    from ``HMMEngine.get_all_regime_infos()``.  It sorts all regimes by
    ``expected_volatility`` (ascending, independent of label), computes a
    normalised position in [0, 1], and assigns a strategy class:

        position = rank / (n_regimes − 1)
        ≤ 0.33  →  LowVolBullStrategy
        ≥ 0.67  →  HighVolDefensiveStrategy
        else    →  MidVolCautiousStrategy

    Parameters
    ----------
    config:
        StrategyConfig with all allocation parameters.
    regime_infos:
        Dict returned by ``HMMEngine.get_all_regime_infos()`` (label → RegimeInfo).
        Can be empty at construction; call ``update_regime_infos`` after fitting.
    """

    def __init__(
        self,
        config: StrategyConfig,
        regime_infos: Optional[dict[str, RegimeInfo]] = None,
    ) -> None:
        self.config = config
        self._id_to_strategy: dict[int, BaseStrategy] = {}
        self._id_to_vol_rank: dict[int, float] = {}
        self._id_to_label: dict[int, str] = {}

        if regime_infos:
            self.update_regime_infos(regime_infos)

    # ------------------------------------------------------------------
    # Regime → strategy mapping
    # ------------------------------------------------------------------

    def update_regime_infos(
        self, regime_infos: dict[str, RegimeInfo]
    ) -> None:
        """Rebuild the regime → strategy mapping after an HMM retrain.

        Parameters
        ----------
        regime_infos:
            ``label → RegimeInfo`` dict from ``HMMEngine.get_all_regime_infos()``.
        """
        if not regime_infos:
            logger.warning("update_regime_infos: received empty dict, nothing mapped.")
            return

        # Sort by VOLATILITY, not return — labels are intentionally ignored here
        sorted_by_vol = sorted(
            regime_infos.values(), key=lambda ri: ri.expected_volatility
        )
        n = len(sorted_by_vol)

        self._id_to_strategy = {}
        self._id_to_vol_rank = {}
        self._id_to_label = {}

        for rank, info in enumerate(sorted_by_vol):
            # Normalised vol position in [0.0, 1.0]
            vol_pos = rank / (n - 1) if n > 1 else 0.0

            if vol_pos <= 0.33:
                cls: type[BaseStrategy] = LowVolBullStrategy
            elif vol_pos >= 0.67:
                cls = HighVolDefensiveStrategy
            else:
                cls = MidVolCautiousStrategy

            self._id_to_strategy[info.regime_id] = cls(self.config)
            self._id_to_vol_rank[info.regime_id] = vol_pos
            self._id_to_label[info.regime_id] = info.regime_name

            logger.debug(
                "  state_id=%d  label=%s  vol=%.4f  vol_rank=%.2f  → %s",
                info.regime_id, info.regime_name, info.expected_volatility,
                vol_pos, cls.__name__,
            )

        logger.info(
            "StrategyOrchestrator: %d regimes mapped by volatility rank.", n
        )

    def get_strategy_for_regime(self, regime_state: RegimeState) -> BaseStrategy:
        """Return the strategy instance assigned to the current regime.

        Falls back to HighVolDefensiveStrategy (safest choice) if the state_id
        is not found — e.g., after an HMM retrain that changed the state count.
        """
        strat = self._id_to_strategy.get(regime_state.state_id)
        if strat is None:
            logger.warning(
                "No strategy mapped for state_id=%d (%s). "
                "Defaulting to HighVolDefensiveStrategy.",
                regime_state.state_id, regime_state.label,
            )
            return HighVolDefensiveStrategy(self.config)
        return strat

    def vol_rank_for_regime(self, state_id: int) -> float:
        """Return the normalised volatility rank [0, 1] for a given state id."""
        return self._id_to_vol_rank.get(state_id, 1.0)   # unknown → worst-case

    # ------------------------------------------------------------------
    # Signal generation
    # ------------------------------------------------------------------

    def generate_signals(
        self,
        symbols: list[str],
        bars: dict[str, pd.DataFrame],
        regime_state: RegimeState,
        is_flickering: bool = False,
    ) -> list[Signal]:
        """Generate per-symbol signals for the current regime.

        Parameters
        ----------
        symbols:
            Full ticker universe.
        bars:
            ``symbol → OHLCV DataFrame`` with at least _MIN_BARS rows each.
        regime_state:
            Current HMM output (label, probability, stability).
        is_flickering:
            True when ``HMMEngine.is_flickering()`` returns True.

        Returns
        -------
        list[Signal]
            One Signal per symbol with sufficient bar history.  Symbols
            with fewer than _MIN_BARS rows are skipped silently.
        """
        strategy = self.get_strategy_for_regime(regime_state)

        # Count symbols that have enough data to receive a signal
        eligible = [s for s in symbols if s in bars and len(bars[s]) >= _MIN_BARS]
        n_eligible = len(eligible) or 1   # avoid division by zero

        signals: list[Signal] = []
        for symbol in symbols:
            sym_bars = bars.get(symbol)
            if sym_bars is None or len(sym_bars) < _MIN_BARS:
                continue
            sig = strategy.generate_signal(
                symbol=symbol,
                bars=sym_bars,
                regime_state=regime_state,
                n_symbols=n_eligible,
                is_flickering=is_flickering,
            )
            if sig is not None:
                signals.append(sig)

        vol_rank = self._id_to_vol_rank.get(regime_state.state_id, -1.0)
        logger.info(
            "generate_signals: %d/%d symbols → %d signals | "
            "regime=%s  vol_rank=%.2f  strategy=%s%s",
            n_eligible, len(symbols), len(signals),
            regime_state.label, vol_rank, strategy.name,
            "  [flickering]" if is_flickering else "",
        )
        return signals

    # ------------------------------------------------------------------
    # Rebalance gating
    # ------------------------------------------------------------------

    def needs_rebalance(
        self,
        target_weights: dict[str, float],
        current_weights: dict[str, float],
    ) -> bool:
        """Return True if any symbol deviates beyond ``rebalance_threshold``.

        Prevents churn from minor probability fluctuations.  Fewer trades
        means less slippage in real-world execution.
        """
        all_syms = set(target_weights) | set(current_weights)
        for sym in all_syms:
            target = target_weights.get(sym, 0.0)
            current = current_weights.get(sym, 0.0)
            if abs(target - current) > self.config.rebalance_threshold:
                return True
        return False

    def drift_report(
        self,
        target_weights: dict[str, float],
        current_weights: dict[str, float],
    ) -> dict[str, float]:
        """Return per-symbol drift as (target − current).  Positive = underweight."""
        all_syms = set(target_weights) | set(current_weights)
        return {
            sym: target_weights.get(sym, 0.0) - current_weights.get(sym, 0.0)
            for sym in all_syms
        }

    # ------------------------------------------------------------------
    # Backward-compat wrapper (used by signal_generator.py skeleton)
    # ------------------------------------------------------------------

    def compute_allocation(
        self,
        regime_state: RegimeState,
        bars: dict[str, pd.DataFrame],
        symbols: list[str],
        current_weights: dict[str, float],
        is_flickering: bool = False,
    ) -> AllocationResult:
        """Legacy entry point that wraps ``generate_signals`` into AllocationResult.

        Kept for backward compatibility with the signal_generator skeleton.
        New code should call ``generate_signals`` directly.
        """
        signals = self.generate_signals(
            symbols=symbols,
            bars=bars,
            regime_state=regime_state,
            is_flickering=is_flickering,
        )

        strategy = self.get_strategy_for_regime(regime_state)
        is_uncertain = (
            regime_state.probability < self.config.min_confidence
            or not regime_state.is_confirmed
            or is_flickering
        )

        # Derive totals from strategy type
        if isinstance(strategy, LowVolBullStrategy):
            total_eq = self.config.low_vol_allocation
            leverage = self.config.low_vol_leverage
        elif isinstance(strategy, MidVolCautiousStrategy):
            # Use the first available symbol as a representative trend check
            trend_intact = True
            for sym in symbols:
                sym_bars = bars.get(sym)
                if sym_bars is not None and len(sym_bars) >= _MIN_BARS:
                    sym_bars = _normalise_bars(sym_bars)
                    price = float(sym_bars["close"].iloc[-1])
                    ema50 = _compute_ema(sym_bars["close"], self.config.ema_span)
                    trend_intact = price > ema50
                    break
            total_eq = (
                self.config.mid_vol_allocation_trend
                if trend_intact
                else self.config.mid_vol_allocation_no_trend
            )
            leverage = 1.0
        else:
            total_eq = self.config.high_vol_allocation
            leverage = 1.0

        if is_uncertain:
            total_eq *= self.config.uncertainty_size_mult
            leverage = 1.0

        symbol_weights = {sig.symbol: sig.position_size_pct for sig in signals}
        vol_rank = self._id_to_vol_rank.get(regime_state.state_id, -1.0)

        rationale = (
            f"regime={regime_state.label}  vol_rank={vol_rank:.2f}  "
            f"strategy={strategy.name}  equity={total_eq:.0%}  leverage={leverage:.2f}x"
        )
        if is_uncertain:
            rationale += "  [UNCERTAINTY -- size halved]"

        return AllocationResult(
            regime=regime_state.label,
            target_equity_fraction=total_eq,
            leverage=leverage,
            symbol_weights=symbol_weights,
            is_uncertain=is_uncertain,
            rationale=rationale,
            signals=signals,
        )


# ---------------------------------------------------------------------------
# Backward-compatible class alias (signal_generator.py imports RegimeStrategy)
# ---------------------------------------------------------------------------

RegimeStrategy = StrategyOrchestrator
