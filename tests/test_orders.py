"""Unit tests for OrderExecutor: placement, validation, cancellation, and lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce

from broker.alpaca_client import AlpacaClient
from broker.order_executor import OrderExecutor, OrderRecord, OrderStatus
from core.risk_manager import RiskConfig, RiskManager, RiskSnapshot, TradingHalt
from core.signal_generator import TradeSignal


INITIAL_NAV = 100_000.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_client() -> MagicMock:
    client = MagicMock(spec=AlpacaClient)
    return client


@pytest.fixture()
def risk_manager() -> RiskManager:
    config = RiskConfig(
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
    return RiskManager(config=config, initial_nav=INITIAL_NAV)


@pytest.fixture()
def executor(mock_client: MagicMock, risk_manager: RiskManager) -> OrderExecutor:
    return OrderExecutor(client=mock_client, risk_manager=risk_manager, dry_run=False)


@pytest.fixture()
def dry_run_executor(
    mock_client: MagicMock, risk_manager: RiskManager
) -> OrderExecutor:
    return OrderExecutor(client=mock_client, risk_manager=risk_manager, dry_run=True)


def make_trade_signal(
    symbol: str = "SPY",
    action: str = "buy",
    shares: float = 10.0,
    target_weight: float = 0.10,
    current_weight: float = 0.0,
) -> TradeSignal:
    return TradeSignal(
        symbol=symbol,
        action=action,
        target_weight=target_weight,
        current_weight=current_weight,
        delta_weight=target_weight - current_weight,
        shares=shares,
        rationale="test signal",
    )


def make_order_record(
    symbol: str = "SPY",
    side: str = "buy",
    shares: float = 10.0,
    status: OrderStatus = OrderStatus.SUBMITTED,
) -> OrderRecord:
    return OrderRecord(
        id="test-order-id",
        symbol=symbol,
        side=side,
        shares=shares,
        order_type="market",
        submitted_at=datetime.now(tz=timezone.utc),
        status=status,
    )


# ---------------------------------------------------------------------------
# Market order placement
# ---------------------------------------------------------------------------


class TestPlaceMarketOrder:
    def test_returns_order_record(
        self, executor: OrderExecutor, mock_client: MagicMock
    ) -> None:
        mock_client.get_account.return_value = MagicMock(buying_power=50_000)
        mock_client._trading = MagicMock()
        mock_client._trading.submit_order.return_value = MagicMock(
            id="abc-123",
            status="submitted",
            filled_qty=0,
            filled_avg_price=None,
        )
        record = executor.place_market_order(
            symbol="SPY", side=OrderSide.BUY, shares=10.0
        )
        assert isinstance(record, OrderRecord)

    def test_order_appended_to_log(
        self, executor: OrderExecutor, mock_client: MagicMock
    ) -> None:
        mock_client._trading = MagicMock()
        mock_client._trading.submit_order.return_value = MagicMock(
            id="abc-456", status="submitted", filled_qty=0, filled_avg_price=None
        )
        executor.place_market_order("SPY", OrderSide.BUY, 5.0)
        assert len(executor.get_order_log()) == 1


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_does_not_call_submit(
        self, dry_run_executor: OrderExecutor, mock_client: MagicMock
    ) -> None:
        dry_run_executor.place_market_order("SPY", OrderSide.BUY, 10.0)
        mock_client._trading.submit_order.assert_not_called()

    def test_dry_run_still_records_order(
        self, dry_run_executor: OrderExecutor
    ) -> None:
        dry_run_executor.place_market_order("SPY", OrderSide.BUY, 10.0)
        assert len(dry_run_executor.get_order_log()) == 1


# ---------------------------------------------------------------------------
# Signal execution ordering (sells before buys)
# ---------------------------------------------------------------------------


class TestSignalOrdering:
    def test_sells_submitted_before_buys(
        self, executor: OrderExecutor
    ) -> None:
        signals = [
            make_trade_signal("SPY", action="buy", shares=10),
            make_trade_signal("QQQ", action="sell", shares=5),
            make_trade_signal("AAPL", action="buy", shares=8),
        ]
        sorted_signals = executor._sort_signals(signals)
        actions = [s.action for s in sorted_signals]
        sell_indices = [i for i, a in enumerate(actions) if a == "sell"]
        buy_indices = [i for i, a in enumerate(actions) if a == "buy"]
        assert all(s < b for s in sell_indices for b in buy_indices)


# ---------------------------------------------------------------------------
# Order cancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    def test_cancel_calls_client(
        self, executor: OrderExecutor, mock_client: MagicMock
    ) -> None:
        mock_client._trading = MagicMock()
        mock_client._trading.cancel_order_by_id.return_value = None
        result = executor.cancel_order("order-id-xyz")
        mock_client._trading.cancel_order_by_id.assert_called_once_with("order-id-xyz")

    def test_cancel_all_returns_count(
        self, executor: OrderExecutor, mock_client: MagicMock
    ) -> None:
        mock_client.get_orders.return_value = [MagicMock(id="a"), MagicMock(id="b")]
        mock_client._trading = MagicMock()
        count = executor.cancel_all_open_orders()
        assert isinstance(count, int)


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------


class TestPreFlightCheck:
    def test_valid_order_passes_preflight(self, executor: OrderExecutor) -> None:
        approved, reason = executor._pre_flight_check("SPY", OrderSide.BUY, 10.0)
        assert approved, reason

    def test_zero_shares_rejected(self, executor: OrderExecutor) -> None:
        approved, _ = executor._pre_flight_check("SPY", OrderSide.BUY, 0.0)
        assert not approved

    def test_negative_shares_rejected(self, executor: OrderExecutor) -> None:
        approved, _ = executor._pre_flight_check("SPY", OrderSide.BUY, -5.0)
        assert not approved
