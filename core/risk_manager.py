"""Position sizing, leverage control, and drawdown circuit-breakers.

RiskManager is the last gate before any order reaches the broker.  It enforces
all hard limits defined in settings.yaml[risk] and maintains intraday /
intraweek drawdown state.

DESIGN PRINCIPLES
-----------------
- Operates INDEPENDENTLY of the HMM.  Even if the HMM fails completely,
  circuit breakers catch drawdowns based on actual P&L.
- Has ABSOLUTE VETO POWER over any signal.
- Defense in depth: portfolio limits -> circuit breakers -> per-position ->
  leverage -> correlation -> order validation.  Each layer is independent.

LEVERAGE NOTE (Alpaca)
-----------------------
Alpaca supports 2x overnight (Reg T, >= $2k equity) and 4x intraday (PDT,
>= $25k equity).  Our 1.25x ceiling is deliberately conservative so the
strategy can scale from paper to live without hitting margin calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from core.regime_strategies import Signal

logger = logging.getLogger(__name__)

_LOCK_FILENAME = "trading_halted.lock"


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────


class CircuitBreakerStatus(str, Enum):
    NONE = "none"
    DAILY_REDUCE = "daily_reduce"     # 50% sizes, rest of day
    DAILY_HALT = "daily_halt"         # no new positions, rest of day
    WEEKLY_REDUCE = "weekly_reduce"   # 50% sizes, rest of week
    WEEKLY_HALT = "weekly_halt"       # no new positions, rest of week
    PEAK_HALT = "peak_halt"           # all trading halted; lock file must be deleted manually


# Backward-compat alias (matches original skeleton name)
TradingHalt = CircuitBreakerStatus


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class RiskConfig:
    """Parameters consumed by RiskManager, sourced from settings.yaml[risk]."""

    # ── Portfolio limits ─────────────────────────────────────────────────────
    max_risk_per_trade: float = 0.01        # max fraction of equity risked per trade
    max_exposure: float = 0.80              # max total gross exposure (20% cash min)
    max_leverage: float = 1.25             # hard leverage ceiling
    max_single_position: float = 0.15      # max single-symbol weight
    max_correlated_exposure: float = 0.30  # max same-sector notional / equity
    max_concurrent: int = 5                # max open positions at once
    max_daily_trades: int = 20             # max fills per calendar day

    # ── Circuit breakers ─────────────────────────────────────────────────────
    daily_dd_reduce: float = 0.02          # daily DD threshold for 50% size reduction
    daily_dd_halt: float = 0.03            # daily DD threshold for full day halt
    weekly_dd_reduce: float = 0.05         # weekly DD threshold for 50% size reduction
    weekly_dd_halt: float = 0.07           # weekly DD threshold for full week halt
    max_dd_from_peak: float = 0.10         # rolling peak DD threshold; locks trading

    # ── Order validation ─────────────────────────────────────────────────────
    max_bid_ask_spread_pct: float = 0.005  # 0.5% max bid-ask spread
    duplicate_block_seconds: int = 60      # block same symbol+direction for N seconds
    min_position_value: float = 100.0      # reject positions smaller than $100

    # ── Overnight gap risk ───────────────────────────────────────────────────
    overnight_gap_multiplier: float = 3.0  # assume 3x stop distance for gap-through
    overnight_portfolio_risk: float = 0.02 # max 2% of equity from an overnight gap

    # ── Correlation limits ───────────────────────────────────────────────────
    correlation_window: int = 60           # rolling window (days) for correlation
    correlation_reduce_threshold: float = 0.70   # reduce 50% if corr > this
    correlation_reject_threshold: float = 0.85   # reject if corr > this

    # ── Leverage policy ──────────────────────────────────────────────────────
    min_confidence_for_leverage: float = 0.55    # require regime confidence >= this
    max_open_for_leverage: int = 3               # force 1.0x if >= this many open
    max_flicker_rate_for_leverage: float = 0.20  # force 1.0x if flicker rate > this


# ─────────────────────────────────────────────────────────────────────────────
# Data transfer objects
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Position:
    """An open position with all metadata needed for risk calculations."""

    symbol: str
    shares: float
    entry_price: float
    stop_price: float
    current_price: float
    sector: str = "UNKNOWN"
    opened_at: Optional[datetime] = None

    @property
    def notional_value(self) -> float:
        return self.shares * self.current_price

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry_price - self.stop_price)

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.entry_price) * self.shares


@dataclass
class PortfolioState:
    """Full portfolio snapshot passed to validate_signal.

    Callers are responsible for keeping this current (update after every fill
    and every mark-to-market bar).
    """

    equity: float                          # current NAV = cash + sum(shares * price)
    cash: float                            # cash balance (may be negative with margin)
    buying_power: float                    # available buying power (from broker)
    positions: dict[str, Position] = field(default_factory=dict)
    daily_pnl: float = 0.0                 # unrealised + realised since day open
    weekly_pnl: float = 0.0
    peak_equity: float = 0.0              # rolling historical high-water mark
    day_open_equity: float = 0.0          # equity at today's session open
    week_open_equity: float = 0.0         # equity at Monday open
    circuit_breaker_status: CircuitBreakerStatus = CircuitBreakerStatus.NONE
    flicker_rate: float = 0.0             # from HMMEngine.get_regime_flicker_rate()
    trades_today: int = 0
    recent_orders: list[dict] = field(default_factory=list)
    # Close-price history keyed by symbol; used for correlation checks
    price_history: dict[str, pd.Series] = field(default_factory=dict)
    current_regime: str = "UNKNOWN"       # active HMM regime label (for logging)

    # ── Derived properties ────────────────────────────────────────────────────

    @property
    def daily_drawdown(self) -> float:
        ref = self.day_open_equity
        return (self.equity - ref) / ref if ref > 0 else 0.0

    @property
    def weekly_drawdown(self) -> float:
        ref = self.week_open_equity
        return (self.equity - ref) / ref if ref > 0 else 0.0

    @property
    def drawdown_from_peak(self) -> float:
        ref = self.peak_equity if self.peak_equity > 0 else self.day_open_equity
        return (self.equity - ref) / ref if ref > 0 else 0.0

    @property
    def gross_exposure(self) -> float:
        if self.equity <= 0:
            return 0.0
        return sum(abs(p.notional_value) for p in self.positions.values()) / self.equity

    @property
    def n_positions(self) -> int:
        return len(self.positions)


@dataclass
class RiskSnapshot:
    """Current risk state returned by the simplified evaluate() API."""

    halt_status: CircuitBreakerStatus
    size_scalar: float              # multiply target sizes by this (1.0 = full, 0.5 = half)
    daily_drawdown: float
    weekly_drawdown: float
    drawdown_from_peak: float
    trades_today: int
    open_positions: int
    gross_exposure: float
    effective_leverage: float


@dataclass
class CircuitBreakerEvent:
    """Immutable audit record written every time a breaker fires."""

    timestamp: datetime
    breaker_type: str
    actual_dd: float
    equity: float
    positions_closed: int
    hmm_regime: str
    size_scalar: float


@dataclass
class RiskDecision:
    """Result of RiskManager.validate_signal()."""

    approved: bool
    modified_signal: Any = None     # Optional[Signal]; Any avoids TYPE_CHECKING import
    rejection_reason: Optional[str] = None
    modifications: list[str] = field(default_factory=list)
    final_shares: Optional[float] = None


# ─────────────────────────────────────────────────────────────────────────────
# CircuitBreaker
# ─────────────────────────────────────────────────────────────────────────────


class CircuitBreaker:
    """Tracks drawdown thresholds and fires when limits are breached.

    Priority (highest to lowest):
        PEAK_HALT > WEEKLY_HALT > WEEKLY_REDUCE > DAILY_HALT > DAILY_REDUCE

    Once PEAK_HALT fires it writes a lock file.  The status persists across
    reset_daily() / reset_weekly() calls until the lock file is manually deleted.
    DAILY and WEEKLY states are cleared by their respective reset methods.
    """

    def __init__(self, config: RiskConfig, lock_dir: Path = Path(".")) -> None:
        self.config = config
        self._lock_path = lock_dir / _LOCK_FILENAME
        self._status: CircuitBreakerStatus = CircuitBreakerStatus.NONE
        self._size_scalar: float = 1.0
        self._history: list[CircuitBreakerEvent] = []

    # ── Public interface ──────────────────────────────────────────────────────

    def check(
        self,
        daily_dd: float,
        weekly_dd: float,
        dd_from_peak: float,
        equity: float,
        n_positions: int = 0,
        hmm_regime: str = "UNKNOWN",
    ) -> tuple[CircuitBreakerStatus, float]:
        """Evaluate all thresholds; return (status, size_scalar).

        size_scalar = 0.0 when halted, 0.5 when reduced, 1.0 when clear.
        Logs every new trigger with: breaker type, actual DD, equity,
        positions that would be closed, and the active HMM regime.
        """
        cfg = self.config

        # 1. Peak drawdown — most severe, write lock file
        if dd_from_peak <= -cfg.max_dd_from_peak:
            return self._fire(
                CircuitBreakerStatus.PEAK_HALT,
                actual_dd=dd_from_peak,
                equity=equity,
                n_positions=n_positions,
                hmm_regime=hmm_regime,
                scalar=0.0,
                write_lock=True,
            )

        # 2. Weekly halt
        if weekly_dd <= -cfg.weekly_dd_halt:
            return self._fire(
                CircuitBreakerStatus.WEEKLY_HALT,
                actual_dd=weekly_dd,
                equity=equity,
                n_positions=n_positions,
                hmm_regime=hmm_regime,
                scalar=0.0,
            )

        # 3. Weekly reduce
        if weekly_dd <= -cfg.weekly_dd_reduce:
            return self._fire(
                CircuitBreakerStatus.WEEKLY_REDUCE,
                actual_dd=weekly_dd,
                equity=equity,
                n_positions=n_positions,
                hmm_regime=hmm_regime,
                scalar=0.5,
            )

        # 4. Daily halt
        if daily_dd <= -cfg.daily_dd_halt:
            return self._fire(
                CircuitBreakerStatus.DAILY_HALT,
                actual_dd=daily_dd,
                equity=equity,
                n_positions=n_positions,
                hmm_regime=hmm_regime,
                scalar=0.0,
            )

        # 5. Daily reduce
        if daily_dd <= -cfg.daily_dd_reduce:
            return self._fire(
                CircuitBreakerStatus.DAILY_REDUCE,
                actual_dd=daily_dd,
                equity=equity,
                n_positions=n_positions,
                hmm_regime=hmm_regime,
                scalar=0.5,
            )

        # All clear
        if self._status not in (
            CircuitBreakerStatus.PEAK_HALT,
            CircuitBreakerStatus.WEEKLY_HALT,
            CircuitBreakerStatus.WEEKLY_REDUCE,
        ):
            self._status = CircuitBreakerStatus.NONE
            self._size_scalar = 1.0

        return self._status, self._size_scalar

    def update(self, current_nav: float, day_open_nav: float, week_open_nav: float, peak_nav: float) -> None:
        """Convenience method: derive drawdowns from NAV values and call check()."""
        daily_dd = (current_nav - day_open_nav) / day_open_nav if day_open_nav > 0 else 0.0
        weekly_dd = (current_nav - week_open_nav) / week_open_nav if week_open_nav > 0 else 0.0
        dd_peak = (current_nav - peak_nav) / peak_nav if peak_nav > 0 else 0.0
        self.check(daily_dd=daily_dd, weekly_dd=weekly_dd, dd_from_peak=dd_peak, equity=current_nav)

    def reset_daily(self) -> None:
        """Clear daily-scoped breaker states at session open."""
        if self._status in (CircuitBreakerStatus.DAILY_REDUCE, CircuitBreakerStatus.DAILY_HALT):
            logger.info("Circuit breaker reset: %s -> NONE (new day)", self._status.value)
            self._status = CircuitBreakerStatus.NONE
            self._size_scalar = 1.0

    def reset_weekly(self) -> None:
        """Clear weekly-scoped breaker states at week open."""
        if self._status in (CircuitBreakerStatus.WEEKLY_REDUCE, CircuitBreakerStatus.WEEKLY_HALT):
            logger.info("Circuit breaker reset: %s -> NONE (new week)", self._status.value)
            self._status = CircuitBreakerStatus.NONE
            self._size_scalar = 1.0

    def is_halted(self) -> bool:
        """Return True if the PEAK_HALT lock file exists (requires manual deletion)."""
        return self._lock_path.exists()

    def get_history(self) -> list[CircuitBreakerEvent]:
        """Return a copy of all recorded trigger events."""
        return list(self._history)

    @property
    def status(self) -> CircuitBreakerStatus:
        return self._status

    @property
    def size_scalar(self) -> float:
        return self._size_scalar

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _fire(
        self,
        new_status: CircuitBreakerStatus,
        actual_dd: float,
        equity: float,
        n_positions: int,
        hmm_regime: str,
        scalar: float,
        write_lock: bool = False,
    ) -> tuple[CircuitBreakerStatus, float]:
        """Record the event, log it, and update internal state."""
        is_new = new_status != self._status

        if is_new:
            event = CircuitBreakerEvent(
                timestamp=datetime.now(),
                breaker_type=new_status.value,
                actual_dd=actual_dd,
                equity=equity,
                positions_closed=n_positions if scalar == 0.0 else 0,
                hmm_regime=hmm_regime,
                size_scalar=scalar,
            )
            self._history.append(event)

            log_level = logging.CRITICAL if scalar == 0.0 else logging.WARNING
            logger.log(
                log_level,
                "CIRCUIT BREAKER [%s] | DD=%.2f%% | equity=$%.0f | "
                "positions=%d | regime=%s | size_scalar=%.1f",
                new_status.value.upper(),
                actual_dd * 100,
                equity,
                n_positions,
                hmm_regime,
                scalar,
            )

        self._status = new_status
        self._size_scalar = scalar

        if write_lock:
            self._write_lock_file(actual_dd, equity)

        return new_status, scalar

    def _write_lock_file(self, actual_dd: float, equity: float) -> None:
        try:
            self._lock_path.write_text(
                f"TRADING HALTED\n"
                f"Peak drawdown threshold exceeded.\n"
                f"Timestamp: {datetime.now().isoformat()}\n"
                f"Peak DD: {actual_dd*100:.2f}%\n"
                f"Equity at halt: ${equity:,.2f}\n"
                f"\nDelete this file to resume trading.\n",
                encoding="utf-8",
            )
            logger.critical("HALT LOCK FILE written: %s", self._lock_path.resolve())
        except OSError as exc:
            logger.error("Failed to write halt lock file %s: %s", self._lock_path, exc)


# ─────────────────────────────────────────────────────────────────────────────
# RiskManager
# ─────────────────────────────────────────────────────────────────────────────


class RiskManager:
    """Enforces position limits, leverage caps, and drawdown circuit-breakers.

    The primary entry point is ``validate_signal(signal, portfolio_state)``
    which applies every risk rule and returns a ``RiskDecision``.  The
    simplified ``evaluate()`` / ``size_position()`` / ``check_order()`` API
    remains for backward compatibility with the backtester.

    Parameters
    ----------
    config:
        Risk parameters (all thresholds from settings.yaml[risk]).
    initial_nav:
        Starting NAV used as the initial drawdown baseline.
    lock_dir:
        Directory where ``trading_halted.lock`` is written.  Defaults to the
        current working directory so operators find it easily.
    """

    def __init__(
        self,
        config: RiskConfig,
        initial_nav: float,
        lock_dir: Optional[Path] = None,
    ) -> None:
        self.config = config
        self._initial_nav = initial_nav
        self._peak_nav: float = initial_nav
        self._day_open_nav: float = initial_nav
        self._week_open_nav: float = initial_nav
        self._trades_today: int = 0
        self._open_positions: dict[str, float] = {}  # symbol → notional value

        self.circuit_breaker = CircuitBreaker(
            config, lock_dir=lock_dir or Path(".")
        )

    # ── Primary validation API ───────────────────────────────────────────────

    def validate_signal(
        self,
        signal: "Signal",
        portfolio_state: PortfolioState,
    ) -> RiskDecision:
        """Apply every risk rule and decide whether to approve the signal.

        Checks applied in order (defense in depth):
            1. PEAK_HALT lock file (independent of circuit breaker state)
            2. Circuit breaker status (halt / reduce)
            3. Concurrent positions limit
            4. Daily trades limit
            5. Stop loss present and meaningful
            6. Leverage rules (may downgrade leverage)
            7. Correlation with existing positions
            8. Duplicate order within 60 seconds
            9. Risk-based position sizing (1% rule, overnight cap, weight cap)
           10. Total exposure cap (80%)
           11. Buying power check

        Returns
        -------
        RiskDecision
            .approved     = True if order may proceed
            .modified_signal = potentially leverage-modified copy of the input
            .final_shares    = approved share count (only if approved=True)
            .modifications   = human-readable list of adjustments made
            .rejection_reason = why it was blocked (only if approved=False)
        """
        modifications: list[str] = []

        # 1. PEAK_HALT lock file
        if self.circuit_breaker.is_halted():
            return RiskDecision(
                approved=False,
                rejection_reason=(
                    f"PEAK_HALT: trading halted — delete "
                    f"'{_LOCK_FILENAME}' to resume"
                ),
            )

        # Resolve Direction lazily to avoid circular import at module level
        try:
            from core.regime_strategies import Direction
            flat = Direction.FLAT
        except ImportError:
            flat = None  # type: ignore[assignment]

        # 2. FLAT signals (position closes) always pass risk checks
        if flat is not None and signal.direction == flat:
            return RiskDecision(
                approved=True,
                modified_signal=signal,
                final_shares=0.0,
                modifications=["FLAT: close signal bypasses sizing rules"],
            )

        # 3. Evaluate circuit breakers from portfolio state drawdowns
        cb_status, size_scalar = self.circuit_breaker.check(
            daily_dd=portfolio_state.daily_drawdown,
            weekly_dd=portfolio_state.weekly_drawdown,
            dd_from_peak=portfolio_state.drawdown_from_peak,
            equity=portfolio_state.equity,
            n_positions=portfolio_state.n_positions,
            hmm_regime=portfolio_state.current_regime,
        )

        _HALT_STATUSES = (
            CircuitBreakerStatus.DAILY_HALT,
            CircuitBreakerStatus.WEEKLY_HALT,
            CircuitBreakerStatus.PEAK_HALT,
        )
        if cb_status in _HALT_STATUSES:
            return RiskDecision(
                approved=False,
                rejection_reason=f"Circuit breaker {cb_status.value} active: no new positions",
            )

        if cb_status == CircuitBreakerStatus.DAILY_REDUCE:
            modifications.append("DAILY_REDUCE: sizes halved for remainder of session")
        elif cb_status == CircuitBreakerStatus.WEEKLY_REDUCE:
            modifications.append("WEEKLY_REDUCE: sizes halved for remainder of week")

        # 4. Concurrent positions
        if portfolio_state.n_positions >= self.config.max_concurrent:
            return RiskDecision(
                approved=False,
                rejection_reason=(
                    f"max_concurrent={self.config.max_concurrent} positions already open "
                    f"({portfolio_state.n_positions} held)"
                ),
            )

        # 5. Daily trades
        if portfolio_state.trades_today >= self.config.max_daily_trades:
            return RiskDecision(
                approved=False,
                rejection_reason=(
                    f"max_daily_trades={self.config.max_daily_trades} reached "
                    f"({portfolio_state.trades_today} today)"
                ),
            )

        # 6. Stop loss required
        risk_per_share = abs(signal.entry_price - signal.stop_loss)
        if signal.stop_loss is None or risk_per_share < 1e-6:
            logger.error(
                "REJECTED %s: no valid stop loss (entry=%.2f stop=%.2f)",
                signal.symbol, signal.entry_price, signal.stop_loss,
            )
            return RiskDecision(
                approved=False,
                rejection_reason="Stop loss required and must differ from entry price",
            )

        # 7. Leverage rules
        signal, lev_mods = self._apply_leverage_rules(signal, portfolio_state)
        modifications.extend(lev_mods)

        # 8. Correlation check
        reduce_factor, reject_reason = self._check_correlation(signal, portfolio_state)
        if reject_reason:
            logger.info("REJECTED %s: %s", signal.symbol, reject_reason)
            return RiskDecision(approved=False, rejection_reason=reject_reason)
        if reduce_factor < 1.0:
            modifications.append(
                f"Correlation: size reduced to {reduce_factor*100:.0f}% of target"
            )

        # 9. Duplicate order check
        dup_reason = self._check_duplicate(signal, portfolio_state)
        if dup_reason:
            logger.info("REJECTED %s: %s", signal.symbol, dup_reason)
            return RiskDecision(approved=False, rejection_reason=dup_reason)

        # 10. Position sizing
        effective_scalar = size_scalar * reduce_factor
        shares = self._compute_shares(
            entry_price=signal.entry_price,
            stop_price=signal.stop_loss,
            equity=portfolio_state.equity,
            proposed_weight=signal.position_size_pct,
            size_scalar=effective_scalar,
        )

        if shares <= 0:
            return RiskDecision(
                approved=False,
                rejection_reason=(
                    f"Position too small after sizing "
                    f"(< ${self.config.min_position_value:.0f} minimum)"
                ),
            )

        # 11. Total exposure cap
        existing_notional = sum(
            abs(p.notional_value) for p in portfolio_state.positions.values()
        )
        new_notional = shares * signal.entry_price
        projected_exposure = (existing_notional + new_notional) / max(portfolio_state.equity, 1.0)

        if projected_exposure > self.config.max_exposure:
            available = max(
                self.config.max_exposure * portfolio_state.equity - existing_notional, 0.0
            )
            shares = int(available / signal.entry_price)
            modifications.append(
                f"Exposure cap {self.config.max_exposure*100:.0f}%: "
                f"reduced to {shares} shares"
            )
            if shares <= 0:
                return RiskDecision(
                    approved=False,
                    rejection_reason=(
                        f"Total exposure limit {self.config.max_exposure*100:.0f}% "
                        "would be breached"
                    ),
                )
            new_notional = shares * signal.entry_price

        # 12. Buying power
        if new_notional > portfolio_state.buying_power:
            return RiskDecision(
                approved=False,
                rejection_reason=(
                    f"Insufficient buying power "
                    f"(need ${new_notional:,.0f}, have ${portfolio_state.buying_power:,.0f})"
                ),
            )

        # 13. Sector exposure (skip if sector data not populated)
        sig_sector = signal.metadata.get("sector", "UNKNOWN")
        if sig_sector != "UNKNOWN":
            sector_notional = sum(
                p.notional_value
                for p in portfolio_state.positions.values()
                if p.sector == sig_sector
            )
            if (sector_notional + new_notional) / max(portfolio_state.equity, 1.0) > self.config.max_correlated_exposure:
                max_sector = max(
                    self.config.max_correlated_exposure * portfolio_state.equity - sector_notional, 0.0
                )
                shares = int(max_sector / signal.entry_price)
                modifications.append(
                    f"Sector cap {self.config.max_correlated_exposure*100:.0f}% [{sig_sector}]: "
                    f"reduced to {shares} shares"
                )
                if shares <= 0:
                    return RiskDecision(
                        approved=False,
                        rejection_reason=(
                            f"Sector exposure limit {self.config.max_correlated_exposure*100:.0f}% "
                            f"for '{sig_sector}' would be breached"
                        ),
                    )

        logger.info(
            "APPROVED %s %s: %d shares @ $%.2f | stop=$%.2f | notional=$%.0f | %s",
            signal.direction,
            signal.symbol,
            shares,
            signal.entry_price,
            signal.stop_loss,
            shares * signal.entry_price,
            (", ".join(modifications) if modifications else "no adjustments"),
        )

        return RiskDecision(
            approved=True,
            modified_signal=signal,
            modifications=modifications,
            final_shares=float(shares),
        )

    # ── Simplified / backward-compatible API ─────────────────────────────────

    def evaluate(self, current_nav: float) -> RiskSnapshot:
        """Compute the current risk snapshot using internal NAV tracking.

        Lighter than validate_signal; useful for monitoring without a full
        PortfolioState.
        """
        self._peak_nav = max(self._peak_nav, current_nav)
        daily_dd, weekly_dd, dd_from_peak = self._compute_drawdowns(current_nav)
        halt_status, size_scalar = self.circuit_breaker.check(
            daily_dd=daily_dd,
            weekly_dd=weekly_dd,
            dd_from_peak=dd_from_peak,
            equity=current_nav,
            n_positions=len(self._open_positions),
        )
        return RiskSnapshot(
            halt_status=halt_status,
            size_scalar=size_scalar,
            daily_drawdown=daily_dd,
            weekly_drawdown=weekly_dd,
            drawdown_from_peak=dd_from_peak,
            trades_today=self._trades_today,
            open_positions=len(self._open_positions),
            gross_exposure=self._gross_exposure(current_nav),
            effective_leverage=self._gross_exposure(current_nav),
        )

    def size_position(
        self,
        symbol: str,
        entry_price: float,
        stop_price: float,
        current_nav: float,
        proposed_weight: float,
    ) -> float:
        """Return the risk-adjusted position size in shares (simplified API)."""
        snapshot = self.evaluate(current_nav)
        return float(self._compute_shares(
            entry_price=entry_price,
            stop_price=stop_price,
            equity=current_nav,
            proposed_weight=proposed_weight,
            size_scalar=snapshot.size_scalar,
        ))

    def check_order(
        self,
        symbol: str,
        shares: float,
        price: float,
        current_nav: float,
        snapshot: RiskSnapshot,
    ) -> tuple[bool, str]:
        """Validate a pre-sized order against hard portfolio limits.

        Returns (approved, reason).
        """
        if snapshot.halt_status in (
            CircuitBreakerStatus.DAILY_HALT,
            CircuitBreakerStatus.WEEKLY_HALT,
            CircuitBreakerStatus.PEAK_HALT,
        ):
            return False, f"Circuit breaker {snapshot.halt_status.value}: order blocked"

        notional = shares * price

        if notional < self.config.min_position_value:
            return False, f"Notional ${notional:.0f} < minimum ${self.config.min_position_value:.0f}"

        position_weight = notional / current_nav
        if position_weight > self.config.max_single_position:
            return False, (
                f"Single position weight {position_weight*100:.1f}% "
                f"> max {self.config.max_single_position*100:.0f}%"
            )

        new_exposure = snapshot.gross_exposure + position_weight
        if new_exposure > self.config.max_exposure:
            return False, (
                f"Gross exposure {new_exposure*100:.1f}% "
                f"> max {self.config.max_exposure*100:.0f}%"
            )

        if snapshot.open_positions >= self.config.max_concurrent:
            return False, f"max_concurrent={self.config.max_concurrent} already open"

        if snapshot.trades_today >= self.config.max_daily_trades:
            return False, f"max_daily_trades={self.config.max_daily_trades} reached"

        return True, "approved"

    def record_trade(self, symbol: str, notional: float) -> None:
        """Register a filled trade so intraday counters stay accurate."""
        self._trades_today += 1
        existing = self._open_positions.get(symbol, 0.0)
        self._open_positions[symbol] = existing + notional
        logger.debug("Trade recorded: %s notional=$%.0f (total today: %d)", symbol, notional, self._trades_today)

    def update_position(self, symbol: str, notional_value: float) -> None:
        """Update the tracked notional value of an open position."""
        self._open_positions[symbol] = notional_value

    def close_position(self, symbol: str) -> None:
        """Remove a position from the tracker after it is closed."""
        self._open_positions.pop(symbol, None)

    def reset_daily(self, current_nav: float) -> None:
        """Call at session open to reset intraday counters and NAV baseline."""
        self._day_open_nav = current_nav
        self._trades_today = 0
        self.circuit_breaker.reset_daily()
        logger.info("Daily reset: day_open_nav=$%.0f trades_today=0", current_nav)

    def reset_weekly(self, current_nav: float) -> None:
        """Call at the start of each trading week to reset weekly counters."""
        self._week_open_nav = current_nav
        self.circuit_breaker.reset_weekly()
        logger.info("Weekly reset: week_open_nav=$%.0f", current_nav)

    # ── Internal risk helpers ─────────────────────────────────────────────────

    def _compute_shares(
        self,
        entry_price: float,
        stop_price: float,
        equity: float,
        proposed_weight: float,
        size_scalar: float,
    ) -> int:
        """Core position-sizing formula:

            size = min(
                risk-budget    / risk_per_share,      # 1% rule
                overnight_cap  / (3 * risk_per_share),# overnight gap cap
                equity * weight / entry_price,        # regime weight cap
                equity * max_single / entry_price,    # position cap
            ) * size_scalar
        """
        if entry_price <= 0:
            return 0
        risk_per_share = abs(entry_price - stop_price)
        if risk_per_share < 1e-6:
            return 0

        # 1% portfolio risk per trade
        risk_budget = equity * self.config.max_risk_per_trade
        shares_by_risk = risk_budget / risk_per_share

        # Overnight gap: assume 3x stop gap-through, total loss <= 2% of equity
        overnight_budget = equity * self.config.overnight_portfolio_risk
        shares_overnight = overnight_budget / (
            self.config.overnight_gap_multiplier * risk_per_share
        )

        # Regime weight requested by strategy
        shares_by_weight = (equity * proposed_weight) / entry_price

        # Hard position cap
        shares_by_cap = (equity * self.config.max_single_position) / entry_price

        raw_shares = min(shares_by_risk, shares_overnight, shares_by_weight, shares_by_cap)
        scaled = raw_shares * max(size_scalar, 0.0)

        if scaled * entry_price < self.config.min_position_value:
            return 0

        return int(scaled)  # floor to whole shares

    def _apply_leverage_rules(
        self,
        signal: "Signal",
        portfolio_state: PortfolioState,
    ) -> tuple["Signal", list[str]]:
        """Enforce leverage policy; return (possibly modified signal, modifications)."""
        cfg = self.config
        mods: list[str] = []
        target_leverage = min(signal.leverage, cfg.max_leverage)
        force_reasons: list[str] = []

        # Any circuit breaker active
        if portfolio_state.circuit_breaker_status != CircuitBreakerStatus.NONE:
            force_reasons.append(
                f"circuit_breaker={portfolio_state.circuit_breaker_status.value}"
            )

        # Too many positions open
        if portfolio_state.n_positions >= cfg.max_open_for_leverage:
            force_reasons.append(
                f"{portfolio_state.n_positions} open positions >= {cfg.max_open_for_leverage}"
            )

        # Low regime confidence
        if signal.confidence < cfg.min_confidence_for_leverage:
            force_reasons.append(
                f"confidence={signal.confidence:.2f} < {cfg.min_confidence_for_leverage}"
            )

        # High HMM flicker rate
        if portfolio_state.flicker_rate > cfg.max_flicker_rate_for_leverage:
            force_reasons.append(
                f"flicker_rate={portfolio_state.flicker_rate:.2f} > {cfg.max_flicker_rate_for_leverage}"
            )

        # HMM uncertainty flag in signal metadata
        if signal.metadata.get("is_uncertain", False):
            force_reasons.append("HMM uncertainty mode active")

        if force_reasons:
            if target_leverage > 1.0:
                mods.append(
                    f"Leverage forced to 1.0x (was {target_leverage:.2f}x): "
                    + "; ".join(force_reasons)
                )
            target_leverage = 1.0
        elif target_leverage > 1.0:
            mods.append(f"Leverage {target_leverage:.2f}x approved (low-vol regime)")

        if target_leverage != signal.leverage:
            signal = replace(signal, leverage=target_leverage)

        return signal, mods

    def _check_correlation(
        self,
        signal: "Signal",
        portfolio_state: PortfolioState,
    ) -> tuple[float, Optional[str]]:
        """Check rolling correlation of signal's symbol with all open positions.

        Returns (reduce_factor, rejection_reason).
        reduce_factor = 0.5 if max correlation > reduce_threshold, else 1.0.
        rejection_reason is set if max correlation > reject_threshold.
        """
        history = portfolio_state.price_history
        sym = signal.symbol
        win = self.config.correlation_window

        if sym not in history or len(history[sym]) < win:
            return 1.0, None  # skip correlation check when data is insufficient

        sig_series = history[sym].iloc[-win:]
        max_corr = 0.0

        for pos_sym in portfolio_state.positions:
            if pos_sym == sym or pos_sym not in history:
                continue
            pos_series = history[pos_sym].iloc[-win:]
            aligned = pd.concat([sig_series, pos_series], axis=1, join="inner")
            if len(aligned) < 20:
                continue
            corr = float(aligned.iloc[:, 0].corr(aligned.iloc[:, 1]))
            if np.isnan(corr):
                continue

            if corr > self.config.correlation_reject_threshold:
                return 1.0, (
                    f"{sym} / {pos_sym} correlation {corr:.2f} "
                    f"> reject threshold {self.config.correlation_reject_threshold}"
                )
            max_corr = max(max_corr, corr)

        if max_corr > self.config.correlation_reduce_threshold:
            logger.info(
                "%s max correlation %.2f > %.2f: size reduced 50%%",
                sym, max_corr, self.config.correlation_reduce_threshold,
            )
            return 0.5, None

        return 1.0, None

    def _check_duplicate(
        self,
        signal: "Signal",
        portfolio_state: PortfolioState,
    ) -> Optional[str]:
        """Block same symbol + direction orders within duplicate_block_seconds."""
        cutoff = datetime.now() - timedelta(seconds=self.config.duplicate_block_seconds)
        for order in portfolio_state.recent_orders:
            ts = order.get("timestamp")
            if (
                order.get("symbol") == signal.symbol
                and order.get("direction") == str(signal.direction)
                and isinstance(ts, datetime)
                and ts > cutoff
            ):
                return (
                    f"Duplicate order: {signal.symbol} {signal.direction} "
                    f"submitted within {self.config.duplicate_block_seconds}s"
                )
        return None

    # ── Internal helpers (used by evaluate / simplified API) ─────────────────

    def _compute_drawdowns(
        self, current_nav: float
    ) -> tuple[float, float, float]:
        """Return (daily_dd, weekly_dd, dd_from_peak) as signed fractions."""
        def _dd(nav: float, ref: float) -> float:
            return (nav - ref) / ref if ref > 0 else 0.0

        return (
            _dd(current_nav, self._day_open_nav),
            _dd(current_nav, self._week_open_nav),
            _dd(current_nav, self._peak_nav),
        )

    def _determine_halt(
        self, daily_dd: float, weekly_dd: float, dd_from_peak: float
    ) -> tuple[CircuitBreakerStatus, float]:
        """Determine halt status and size scalar from drawdown levels."""
        cfg = self.config
        if dd_from_peak <= -cfg.max_dd_from_peak:
            return CircuitBreakerStatus.PEAK_HALT, 0.0
        if weekly_dd <= -cfg.weekly_dd_halt:
            return CircuitBreakerStatus.WEEKLY_HALT, 0.0
        if weekly_dd <= -cfg.weekly_dd_reduce:
            return CircuitBreakerStatus.WEEKLY_REDUCE, 0.5
        if daily_dd <= -cfg.daily_dd_halt:
            return CircuitBreakerStatus.DAILY_HALT, 0.0
        if daily_dd <= -cfg.daily_dd_reduce:
            return CircuitBreakerStatus.DAILY_REDUCE, 0.5
        return CircuitBreakerStatus.NONE, 1.0

    def _gross_exposure(self, current_nav: float) -> float:
        """Sum |position notional| / NAV across all tracked positions."""
        if current_nav <= 0:
            return 0.0
        return sum(abs(v) for v in self._open_positions.values()) / current_nav
