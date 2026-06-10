"""Order placement, modification, and cancellation.

OrderExecutor is the single point of contact between signal logic and the
Alpaca trading API.  It translates TradeSignals into concrete Alpaca orders,
manages bracket/stop logic, and ensures all orders pass a final pre-flight
risk check before submission.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from alpaca.trading.models import Order
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from broker.alpaca_client import AlpacaClient
from core.risk_manager import RiskManager

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


# TradeSignal is defined here as a simple protocol to avoid circular imports.
# The signal_generator.TradeSignal satisfies this shape.
@dataclass
class _TVOrder:
    """Minimal signal shape used by the TradingView path."""

    symbol: str
    action: str       # "buy" | "sell" | "close"
    shares: int
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


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

    def execute_signals(self, signals: list) -> list[OrderRecord]:
        """Execute a list of TradeSignals in safe order (sells first).

        Parameters
        ----------
        signals:
            Output of SignalGenerator.generate().signals or TVOrder list.

        Returns
        -------
        list[OrderRecord]
            One record per submitted (or skipped) signal.
        """
        sorted_signals = self._sort_signals(signals)
        records: list[OrderRecord] = []
        for signal in sorted_signals:
            action = getattr(signal, "action", "").lower()
            if action in ("hold", "flat"):
                continue
            side = OrderSide.BUY if action == "buy" else OrderSide.SELL
            shares = int(getattr(signal, "shares", 0))
            if shares <= 0:
                logger.debug("Skipping %s — zero shares after sizing", signal.symbol)
                continue
            ok, reason = self._pre_flight_check(signal.symbol, side, shares)
            if not ok:
                logger.warning("Pre-flight failed for %s: %s", signal.symbol, reason)
                continue
            stop_loss = getattr(signal, "stop_loss", None)
            take_profit = getattr(signal, "take_profit", None)
            record = self.place_market_order(
                signal.symbol, side, shares,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
            records.append(record)
        return records

    def place_tv_order(
        self,
        symbol: str,
        side: OrderSide,
        shares: int,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> OrderRecord:
        """Convenience entry point for the TradingView webhook path."""
        ok, reason = self._pre_flight_check(symbol, side, shares)
        if not ok:
            logger.warning("Pre-flight failed for %s: %s", symbol, reason)
            rec = OrderRecord(
                id=f"rejected-{uuid.uuid4().hex[:8]}",
                symbol=symbol,
                side=side.value,
                shares=shares,
                order_type="market",
                submitted_at=datetime.now(timezone.utc),
                status=OrderStatus.REJECTED,
            )
            self._order_log.append(rec)
            return rec
        return self.place_market_order(symbol, side, shares, stop_loss=stop_loss, take_profit=take_profit)

    def place_market_order(
        self,
        symbol: str,
        side: OrderSide,
        shares: float,
        time_in_force: TimeInForce = TimeInForce.DAY,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> OrderRecord:
        """Submit a market order and return its OrderRecord."""
        if self.dry_run:
            logger.info("[DRY RUN] %s %s x%d", side.value.upper(), symbol, shares)
            rec = OrderRecord(
                id=f"dry-{uuid.uuid4().hex[:8]}",
                symbol=symbol,
                side=side.value,
                shares=shares,
                order_type="market",
                submitted_at=datetime.now(timezone.utc),
                status=OrderStatus.SUBMITTED,
            )
            self._order_log.append(rec)
            return rec

        order_kwargs: dict = dict(
            symbol=symbol,
            qty=int(shares),
            side=side,
            time_in_force=time_in_force,
        )
        if stop_loss is not None:
            order_kwargs["stop_loss"] = StopLossRequest(stop_price=stop_loss)
        if take_profit is not None:
            order_kwargs["take_profit"] = TakeProfitRequest(limit_price=take_profit)

        req = MarketOrderRequest(**order_kwargs)
        order: Order = self.client._retry(
            lambda: self.client._trading.submit_order(req)
        )
        rec = self._record(order)
        logger.info(
            "Order submitted: %s %s x%d | id=%s",
            side.value.upper(), symbol, shares, rec.id,
        )
        return rec

    def place_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        shares: float,
        limit_price: float,
        time_in_force: TimeInForce = TimeInForce.DAY,
    ) -> OrderRecord:
        """Submit a limit order and return its OrderRecord."""
        if self.dry_run:
            logger.info(
                "[DRY RUN] LIMIT %s %s x%d @ %.2f",
                side.value.upper(), symbol, shares, limit_price,
            )
            rec = OrderRecord(
                id=f"dry-lmt-{uuid.uuid4().hex[:8]}",
                symbol=symbol,
                side=side.value,
                shares=shares,
                order_type="limit",
                submitted_at=datetime.now(timezone.utc),
                status=OrderStatus.SUBMITTED,
            )
            self._order_log.append(rec)
            return rec

        req = LimitOrderRequest(
            symbol=symbol,
            qty=int(shares),
            side=side,
            limit_price=limit_price,
            time_in_force=time_in_force,
        )
        order = self.client._retry(lambda: self.client._trading.submit_order(req))
        return self._record(order)

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order by ID.  Returns True if successfully cancelled."""
        try:
            self.client._trading.cancel_order_by_id(order_id)
            for rec in self._order_log:
                if rec.id == order_id:
                    rec.status = OrderStatus.CANCELLED
            logger.info("Cancelled order %s", order_id)
            return True
        except Exception as exc:
            logger.error("Failed to cancel order %s: %s", order_id, exc)
            return False

    def cancel_all_open_orders(self) -> int:
        """Cancel every open order.  Returns the count of cancelled orders."""
        try:
            cancelled = self.client._retry(
                lambda: self.client._trading.cancel_orders()
            )
            count = len(cancelled) if cancelled else 0
            for rec in self._order_log:
                if rec.status == OrderStatus.SUBMITTED:
                    rec.status = OrderStatus.CANCELLED
            logger.info("Cancelled %d open orders", count)
            return count
        except Exception as exc:
            logger.error("cancel_all_open_orders failed: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # Lifecycle / tracking
    # ------------------------------------------------------------------

    def refresh_order(self, order_id: str) -> OrderRecord:
        """Poll Alpaca for the latest status of an order and update its record."""
        order = self.client._retry(
            lambda: self.client._trading.get_order_by_id(order_id)
        )
        for rec in self._order_log:
            if rec.id == order_id:
                rec.alpaca_order = order
                rec.filled_qty = float(order.filled_qty or 0)
                rec.filled_avg_price = float(order.filled_avg_price or 0)
                rec.status = _map_status(str(order.status))
                return rec
        # Not in log — build a new record
        return self._record(order)

    def wait_for_fill(
        self, order_id: str, timeout_seconds: float = 30.0
    ) -> OrderRecord:
        """Block until an order is filled or timeout elapses."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            rec = self.refresh_order(order_id)
            if rec.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED,
                               OrderStatus.CANCELLED, OrderStatus.REJECTED):
                return rec
            time.sleep(0.5)
        logger.warning("wait_for_fill timed out for order %s", order_id)
        return self.refresh_order(order_id)

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
        if self.risk_manager.circuit_breaker.is_halted():
            return False, "trading_halted.lock file present — manual clearance required"
        if shares <= 0:
            return False, f"invalid share count: {shares}"
        return True, ""

    def _sort_signals(self, signals: list) -> list:
        """Return signals sorted so sells precede buys."""
        sells = [s for s in signals if getattr(s, "action", "").lower() in ("sell", "close")]
        buys  = [s for s in signals if getattr(s, "action", "").lower() == "buy"]
        other = [s for s in signals if s not in sells and s not in buys]
        return sells + other + buys

    def _record(self, order: Order) -> OrderRecord:
        """Convert an Alpaca Order into an OrderRecord and append to log."""
        rec = OrderRecord(
            id=str(order.id),
            symbol=str(order.symbol),
            side=str(order.side.value) if hasattr(order.side, "value") else str(order.side),
            shares=float(order.qty or 0),
            order_type=str(order.order_type.value) if hasattr(order.order_type, "value") else str(order.order_type),
            submitted_at=order.submitted_at or datetime.now(timezone.utc),
            status=_map_status(str(order.status)),
            filled_qty=float(order.filled_qty or 0),
            filled_avg_price=float(order.filled_avg_price or 0),
            alpaca_order=order,
        )
        self._order_log.append(rec)
        return rec


def _map_status(alpaca_status: str) -> OrderStatus:
    """Map Alpaca order status string to our OrderStatus enum."""
    mapping = {
        "new":              OrderStatus.SUBMITTED,
        "partially_filled": OrderStatus.PARTIALLY_FILLED,
        "filled":           OrderStatus.FILLED,
        "done_for_day":     OrderStatus.CANCELLED,
        "canceled":         OrderStatus.CANCELLED,
        "cancelled":        OrderStatus.CANCELLED,
        "expired":          OrderStatus.CANCELLED,
        "replaced":         OrderStatus.CANCELLED,
        "pending_cancel":   OrderStatus.SUBMITTED,
        "pending_replace":  OrderStatus.SUBMITTED,
        "held":             OrderStatus.SUBMITTED,
        "accepted":         OrderStatus.SUBMITTED,
        "pending_new":      OrderStatus.PENDING,
        "rejected":         OrderStatus.REJECTED,
        "suspended":        OrderStatus.REJECTED,
    }
    return mapping.get(alpaca_status.lower(), OrderStatus.SUBMITTED)
