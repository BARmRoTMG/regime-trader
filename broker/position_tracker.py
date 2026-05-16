"""Track open positions, real-time P&L, and portfolio-level metrics.

PositionTracker maintains an in-process view of the portfolio that is
continuously reconciled against Alpaca's REST API so signal generation
and risk management always see current state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

from broker.alpaca_client import AlpacaClient

logger = logging.getLogger(__name__)


@dataclass
class PositionRecord:
    """In-process representation of one open position."""

    symbol: str
    shares: float
    avg_entry_price: float
    current_price: float
    market_value: float
    unrealised_pnl: float
    unrealised_pnl_pct: float
    side: str                        # "long" | "short"
    opened_at: datetime
    last_updated: datetime


@dataclass
class PortfolioSnapshot:
    """Aggregate portfolio view at a single point in time."""

    timestamp: datetime
    nav: float                       # net asset value
    cash: float
    gross_exposure: float
    net_exposure: float
    unrealised_pnl: float
    daily_pnl: float
    positions: list[PositionRecord] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=dict)  # symbol → fraction of NAV


class PositionTracker:
    """Maintains live position state and portfolio metrics.

    Parameters
    ----------
    client:
        Connected AlpacaClient for periodic reconciliation.

    Responsibilities
    ----------------
    - Keep an in-memory map of open positions, updated on every fill event.
    - Periodically reconcile with Alpaca REST to catch manual trades or errors.
    - Compute real-time weights, gross/net exposure, and unrealised P&L.
    - Provide a PortfolioSnapshot for the risk manager and dashboard.
    - Track session-level (daily) realised P&L by accumulating closed-trade gains.
    """

    def __init__(self, client: AlpacaClient) -> None:
        self.client = client
        self._positions: dict[str, PositionRecord] = {}
        self._day_open_nav: float = 0.0
        self._realised_pnl: float = 0.0

    # ------------------------------------------------------------------
    # Snapshot / query
    # ------------------------------------------------------------------

    def snapshot(self) -> PortfolioSnapshot:
        """Return the current PortfolioSnapshot (uses cached in-memory state)."""
        ...

    def get_position(self, symbol: str) -> Optional[PositionRecord]:
        """Return the PositionRecord for *symbol*, or None if not held."""
        return self._positions.get(symbol)

    def get_weights(self, nav: float) -> dict[str, float]:
        """Return current portfolio weights as fractions of *nav*."""
        ...

    def is_flat(self, symbol: str) -> bool:
        """Return True if no open position exists for *symbol*."""
        return symbol not in self._positions

    # ------------------------------------------------------------------
    # State updates
    # ------------------------------------------------------------------

    def on_fill(
        self,
        symbol: str,
        side: str,
        shares: float,
        fill_price: float,
        filled_at: datetime,
    ) -> None:
        """Update in-memory position state when an order is filled.

        Parameters
        ----------
        symbol:
            Ticker that was traded.
        side:
            "buy" or "sell".
        shares:
            Number of shares filled.
        fill_price:
            Average fill price.
        filled_at:
            Fill timestamp (UTC).
        """
        ...

    def update_prices(self, prices: dict[str, float]) -> None:
        """Update current_price and recompute unrealised P&L for all positions."""
        ...

    def reconcile(self) -> None:
        """Pull live positions from Alpaca REST and sync _positions."""
        ...

    def reset_daily(self, current_nav: float) -> None:
        """Record today's opening NAV to compute intraday P&L."""
        ...

    # ------------------------------------------------------------------
    # P&L helpers
    # ------------------------------------------------------------------

    def daily_pnl(self) -> float:
        """Unrealised + realised P&L since _day_open_nav was set."""
        ...

    def daily_pnl_pct(self) -> float:
        """daily_pnl() as a fraction of _day_open_nav."""
        ...

    def history(self, n_days: int = 30) -> pd.DataFrame:
        """Return a DataFrame of daily NAV and P&L snapshots (if persisted)."""
        ...

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_nav(self, cash: float) -> float:
        """Sum cash + all position market values."""
        ...

    def _update_position_on_buy(
        self, symbol: str, shares: float, price: float, ts: datetime
    ) -> None:
        """Open a new position or average-up an existing long."""
        ...

    def _update_position_on_sell(
        self, symbol: str, shares: float, price: float
    ) -> float:
        """Reduce or close a position; return the realised P&L."""
        ...
