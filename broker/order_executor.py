"""Order placement, modification, and cancellation.

OrderExecutor is the single point of contact between signal logic and the
Alpaca trading API.  It translates TradeSignals into concrete Alpaca orders,
manages bracket/stop logic, and ensures all orders pass a final pre-flight
risk check before submission.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from alpaca.trading.models import Order
from alpaca.trading.requests import MarketOrderRequest

from broker.alpaca_client import AlpacaClient
from core.risk_manager import RiskManager
from core.signal_generator import TradeSignal

logger = logging.getLogger(__name__)


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass
class OrderRecord:
    """Internal representation of an order throughout its lifecycle."""

    id: str
    symbol: str
    side: str
    shares: float
    order_type: str
    submitted_at: datetime
    status: OrderStatus
    filled_qty: float = 0.0
    filled_avg_price: float = 0.0
    alpaca_order: Optional[Order] = None


class OrderExecutor:
    """Translates TradeSignals into Alpaca orders and tracks their lifecycle.

    Parameters
    ----------
    client:
        Connected AlpacaClient instance.
    risk_manager:
        RiskManager for pre-submission validation.
    dry_run:
        If True, log orders but never actually submit them.

    Responsibilities
    ----------------
    - Accept a list of TradeSignals and determine the optimal order sequence
      (e.g. sell before buy to free up buying power).
    - Submit market / limit orders and store OrderRecords.
    - Poll or stream for fill events and update records accordingly.
    - Cancel stale unfilled orders at end-of-session.
    - Enforce max_daily_trades limit before each submission.
    """

    def __init__(
        self,
        client: AlpacaClient,
        risk_manager: RiskManager,
        dry_run: bool = False,
    ) -> None:
        self.client = client
        self.risk_manager = risk_manager
        self.dry_run = dry_run
        self._order_log: list[OrderRecord] = []

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute_signals(self, signals: list[TradeSignal]) -> list[OrderRecord]:
        """Execute a list of TradeSignals in safe order (sells first).

        Parameters
        ----------
        signals:
            Output of SignalGenerator.generate().signals.

        Returns
        -------
        list[OrderRecord]
            One record per submitted (or skipped) signal.
        """
        ...

    def place_market_order(
        self,
        symbol: str,
        side: OrderSide,
        shares: float,
        time_in_force: TimeInForce = TimeInForce.DAY,
    ) -> OrderRecord:
        """Submit a market order and return its OrderRecord.

        Parameters
        ----------
        symbol:
            Ticker to trade.
        side:
            BUY or SELL.
        shares:
            Number of whole shares.
        time_in_force:
            Alpaca time-in-force enum; defaults to DAY.

        Returns
        -------
        OrderRecord
        """
        ...

    def place_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        shares: float,
        limit_price: float,
        time_in_force: TimeInForce = TimeInForce.DAY,
    ) -> OrderRecord:
        """Submit a limit order and return its OrderRecord."""
        ...

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order by ID.  Returns True if successfully cancelled."""
        ...

    def cancel_all_open_orders(self) -> int:
        """Cancel every open order.  Returns the count of cancelled orders."""
        ...

    # ------------------------------------------------------------------
    # Lifecycle / tracking
    # ------------------------------------------------------------------

    def refresh_order(self, order_id: str) -> OrderRecord:
        """Poll Alpaca for the latest status of an order and update its record."""
        ...

    def wait_for_fill(
        self, order_id: str, timeout_seconds: float = 30.0
    ) -> OrderRecord:
        """Block until an order is filled or timeout elapses."""
        ...

    def get_order_log(self) -> list[OrderRecord]:
        """Return all OrderRecords created in this session."""
        return list(self._order_log)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _pre_flight_check(
        self, symbol: str, side: OrderSide, shares: float
    ) -> tuple[bool, str]:
        """Validate the order against daily trade limits and halt status."""
        ...

    def _sort_signals(self, signals: list[TradeSignal]) -> list[TradeSignal]:
        """Return signals sorted so sells precede buys."""
        ...

    def _record(self, order: Order) -> OrderRecord:
        """Convert an Alpaca Order into an OrderRecord and append to log."""
        ...
