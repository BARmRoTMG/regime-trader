"""Core regime-detection and strategy logic."""

from core.hmm_engine import HMMConfig, HMMEngine, RegimeInfo, RegimeState
from core.regime_strategies import (
    AllocationResult,
    Direction,
    HighVolDefensiveStrategy,
    LowVolBullStrategy,
    MidVolCautiousStrategy,
    Signal,
    StrategyConfig,
    StrategyOrchestrator,
    RegimeStrategy,        # backward-compat alias
    LABEL_TO_STRATEGY,
)
from core.risk_manager import (
    RiskConfig, RiskManager,
    CircuitBreaker, CircuitBreakerStatus, CircuitBreakerEvent,
    Position, PortfolioState, RiskSnapshot, RiskDecision,
    TradingHalt,           # backward-compat alias
)
from core.signal_generator import SignalGenerator

__all__ = [
    # HMM
    "HMMConfig", "HMMEngine", "RegimeInfo", "RegimeState",
    # Strategies
    "AllocationResult", "Direction", "Signal", "StrategyConfig",
    "LowVolBullStrategy", "MidVolCautiousStrategy", "HighVolDefensiveStrategy",
    "StrategyOrchestrator", "RegimeStrategy", "LABEL_TO_STRATEGY",
    # Risk
    "RiskConfig", "RiskManager",
    "CircuitBreaker", "CircuitBreakerStatus", "CircuitBreakerEvent",
    "Position", "PortfolioState", "RiskSnapshot", "RiskDecision",
    "TradingHalt",
    # Signals
    "SignalGenerator",
]
