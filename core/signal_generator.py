"""Signal generator – combines HMM regime + strategy into actionable signals.

Acts as the orchestration layer between the HMMEngine (what regime are we in?)
and RegimeStrategy (what should we hold?), producing a SignalPacket that the
order executor can act on directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from core.hmm_engine import HMMEngine, RegimeState
from core.regime_strategies import AllocationResult, RegimeStrategy
from core.risk_manager import RiskManager, RiskSnapshot

logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    """A single actionable signal for one symbol."""

    symbol: str
    action: str                    # "buy" | "sell" | "hold" | "close"
    target_weight: float           # fraction of NAV
    current_weight: float
    delta_weight: float            # target − current
    shares: float                  # risk-adjusted share count (0 = no change)
    rationale: str


@dataclass
class SignalPacket:
    """Bundle of signals produced for a single bar across the full universe."""

    timestamp: pd.Timestamp
    regime_state: RegimeState
    allocation: AllocationResult
    risk_snapshot: RiskSnapshot
    signals: list[TradeSignal] = field(default_factory=list)
    is_actionable: bool = False    # False if halted or confidence too low


class SignalGenerator:
    """Orchestrates HMM inference, strategy allocation, and risk checks.

    Parameters
    ----------
    hmm_engine:
        A fitted HMMEngine instance.
    strategy:
        RegimeStrategy configured with the same symbol universe.
    risk_manager:
        RiskManager with current session state.

    Responsibilities
    ----------------
    - Pull latest features and pass them through the HMM to get a RegimeState.
    - Pass RegimeState and trend signal to RegimeStrategy to get AllocationResult.
    - Pass AllocationResult through RiskManager to size each position.
    - Assemble per-symbol TradeSignals and determine whether rebalancing is needed.
    - Return a fully populated SignalPacket, ready for OrderExecutor.
    """

    def __init__(
        self,
        hmm_engine: HMMEngine,
        strategy: RegimeStrategy,
        risk_manager: RiskManager,
    ) -> None:
        self.hmm_engine = hmm_engine
        self.strategy = strategy
        self.risk_manager = risk_manager

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def generate(
        self,
        features: pd.DataFrame,
        current_weights: dict[str, float],
        current_nav: float,
        prices: dict[str, float],
    ) -> SignalPacket:
        """Generate a full SignalPacket for the latest bar.

        Parameters
        ----------
        features:
            Feature matrix up to and including the current bar.
        current_weights:
            Live symbol weights as fractions of NAV.
        current_nav:
            Current portfolio net asset value.
        prices:
            Latest mid-prices keyed by symbol.

        Returns
        -------
        SignalPacket
        """
        ...

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_trend_signal(self, features: pd.DataFrame) -> Optional[bool]:
        """Derive a boolean trend flag from moving average crossover or momentum."""
        ...

    def _build_trade_signals(
        self,
        allocation: AllocationResult,
        current_weights: dict[str, float],
        risk_snapshot: RiskSnapshot,
        current_nav: float,
        prices: dict[str, float],
    ) -> list[TradeSignal]:
        """Convert an AllocationResult into per-symbol TradeSignals."""
        ...

    def _classify_action(
        self, delta_weight: float, current_weight: float
    ) -> str:
        """Return 'buy', 'sell', 'hold', or 'close' given weight delta."""
        ...
