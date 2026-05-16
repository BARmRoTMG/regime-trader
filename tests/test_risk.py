"""Unit tests for RiskManager: position sizing, limits, and drawdown controls."""

from __future__ import annotations

import pytest

from core.risk_manager import RiskConfig, RiskManager, TradingHalt


INITIAL_NAV = 100_000.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def default_config() -> RiskConfig:
    return RiskConfig(
        max_risk_per_trade=0.01,
        max_exposure=0.80,
        max_leverage=1.25,
        max_single_position=0.15,
        max_concurrent=5,
        max_daily_trades=20,
        daily_dd_reduce=0.02,
        daily_dd_halt=0.03,
        weekly_dd_reduce=0.05,
        weekly_dd_halt=0.07,
        max_dd_from_peak=0.10,
    )


@pytest.fixture()
def risk_manager(default_config: RiskConfig) -> RiskManager:
    return RiskManager(config=default_config, initial_nav=INITIAL_NAV)


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


class TestRiskManagerInit:
    def test_no_halt_at_init(self, risk_manager: RiskManager) -> None:
        snapshot = risk_manager.evaluate(INITIAL_NAV)
        assert snapshot.halt_status == TradingHalt.NONE

    def test_size_scalar_one_at_init(self, risk_manager: RiskManager) -> None:
        snapshot = risk_manager.evaluate(INITIAL_NAV)
        assert snapshot.size_scalar == pytest.approx(1.0)

    def test_zero_open_positions_at_init(self, risk_manager: RiskManager) -> None:
        snapshot = risk_manager.evaluate(INITIAL_NAV)
        assert snapshot.open_positions == 0


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------


class TestPositionSizing:
    def test_size_respects_max_risk_per_trade(
        self, risk_manager: RiskManager
    ) -> None:
        # Entry $100, stop $95 → 5 % risk per share; max_risk_per_trade=1 % of $100k → $1000
        # max shares = 1000 / 5 = 200
        shares = risk_manager.size_position(
            symbol="SPY",
            entry_price=100.0,
            stop_price=95.0,
            current_nav=INITIAL_NAV,
            proposed_weight=0.30,
        )
        max_risk_dollars = INITIAL_NAV * 0.01
        risk_per_share = abs(100.0 - 95.0)
        expected_max = max_risk_dollars / risk_per_share
        assert shares <= expected_max

    def test_size_respects_max_single_position(
        self, risk_manager: RiskManager
    ) -> None:
        shares = risk_manager.size_position(
            symbol="SPY",
            entry_price=100.0,
            stop_price=95.0,
            current_nav=INITIAL_NAV,
            proposed_weight=0.50,  # exceeds max_single_position=0.15
        )
        max_notional = INITIAL_NAV * 0.15
        assert shares * 100.0 <= max_notional + 1e-6  # allow float rounding

    def test_size_is_whole_shares(self, risk_manager: RiskManager) -> None:
        shares = risk_manager.size_position(
            symbol="SPY",
            entry_price=100.0,
            stop_price=95.0,
            current_nav=INITIAL_NAV,
            proposed_weight=0.10,
        )
        assert shares == int(shares)


# ---------------------------------------------------------------------------
# Order validation
# ---------------------------------------------------------------------------


class TestOrderValidation:
    def test_valid_order_approved(self, risk_manager: RiskManager) -> None:
        snapshot = risk_manager.evaluate(INITIAL_NAV)
        approved, reason = risk_manager.check_order(
            symbol="SPY",
            shares=10,
            price=100.0,
            current_nav=INITIAL_NAV,
            snapshot=snapshot,
        )
        assert approved, reason

    def test_order_rejected_when_halted(self, risk_manager: RiskManager) -> None:
        # Force a halt by simulating a large drawdown
        nav_after_dd = INITIAL_NAV * (1 - 0.04)   # 4 % daily DD > halt threshold
        risk_manager.reset_daily(INITIAL_NAV)
        snapshot = risk_manager.evaluate(nav_after_dd)
        assert snapshot.halt_status == TradingHalt.HALT
        approved, _ = risk_manager.check_order(
            symbol="SPY", shares=10, price=100.0,
            current_nav=nav_after_dd, snapshot=snapshot,
        )
        assert not approved

    def test_order_rejected_beyond_max_daily_trades(
        self, risk_manager: RiskManager
    ) -> None:
        for i in range(20):
            risk_manager.record_trade(symbol=f"SYM{i}", notional=1000.0)
        snapshot = risk_manager.evaluate(INITIAL_NAV)
        approved, reason = risk_manager.check_order(
            symbol="SPY", shares=5, price=100.0,
            current_nav=INITIAL_NAV, snapshot=snapshot,
        )
        assert not approved
        assert "daily" in reason.lower()


# ---------------------------------------------------------------------------
# Drawdown circuit-breakers
# ---------------------------------------------------------------------------


class TestDrawdownCircuitBreakers:
    def test_reduce_triggered_at_daily_dd_reduce(
        self, risk_manager: RiskManager
    ) -> None:
        risk_manager.reset_daily(INITIAL_NAV)
        nav = INITIAL_NAV * (1 - 0.025)   # 2.5 % DD > reduce threshold (2 %)
        snapshot = risk_manager.evaluate(nav)
        assert snapshot.halt_status == TradingHalt.REDUCE_SIZE
        assert snapshot.size_scalar < 1.0

    def test_halt_triggered_at_daily_dd_halt(
        self, risk_manager: RiskManager
    ) -> None:
        risk_manager.reset_daily(INITIAL_NAV)
        nav = INITIAL_NAV * (1 - 0.035)   # 3.5 % DD > halt threshold (3 %)
        snapshot = risk_manager.evaluate(nav)
        assert snapshot.halt_status == TradingHalt.HALT

    def test_weekly_halt_independent_of_daily(
        self, risk_manager: RiskManager
    ) -> None:
        risk_manager.reset_weekly(INITIAL_NAV)
        nav = INITIAL_NAV * (1 - 0.08)    # 8 % weekly DD > halt threshold (7 %)
        snapshot = risk_manager.evaluate(nav)
        assert snapshot.halt_status == TradingHalt.HALT

    def test_peak_drawdown_halt(self, risk_manager: RiskManager) -> None:
        # Peak was at 120k, now at 107k → 10.8 % from peak > max_dd_from_peak (10 %)
        risk_manager._peak_nav = 120_000.0
        snapshot = risk_manager.evaluate(107_000.0)
        assert snapshot.halt_status == TradingHalt.HALT


# ---------------------------------------------------------------------------
# Session reset
# ---------------------------------------------------------------------------


class TestSessionReset:
    def test_daily_reset_clears_trade_count(self, risk_manager: RiskManager) -> None:
        risk_manager.record_trade("SPY", 5000.0)
        risk_manager.reset_daily(INITIAL_NAV)
        assert risk_manager._trades_today == 0

    def test_weekly_reset_updates_week_open_nav(
        self, risk_manager: RiskManager
    ) -> None:
        new_nav = 110_000.0
        risk_manager.reset_weekly(new_nav)
        assert risk_manager._week_open_nav == new_nav
