"""Alpaca API wrapper.

Wraps both the REST API (alpaca-py) and the streaming data feed
(websocket-client) behind a clean interface so the rest of the system
never imports alpaca-py directly.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import pandas as pd
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Account, Order, Position

logger = logging.getLogger(__name__)


@dataclass
class AlpacaConfig:
    """Alpaca connection parameters."""

    api_key: str
    secret_key: str
    paper: bool = True


class AlpacaClient:
    """Thin wrapper around Alpaca REST + streaming APIs.

    Parameters
    ----------
    config:
        API credentials and paper/live flag.

    Responsibilities
    ----------------
    - Authenticate and expose TradingClient and StockHistoricalDataClient.
    - Fetch historical OHLCV bars as a tidy DataFrame.
    - Stream real-time quotes/trades and invoke registered callbacks.
    - Expose account, position, and order query helpers.
    - Centralise retry / rate-limit logic so callers don't duplicate it.
    """

    def __init__(self, config: AlpacaConfig) -> None:
        self.config = config
        self._trading: Optional[TradingClient] = None
        self._data: Optional[StockHistoricalDataClient] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Initialise TradingClient and StockHistoricalDataClient."""
        ...

    def disconnect(self) -> None:
        """Close any open streaming connections gracefully."""
        ...

    # ------------------------------------------------------------------
    # Account & portfolio queries
    # ------------------------------------------------------------------

    def get_account(self) -> Account:
        """Return Alpaca Account object with current NAV, buying power, etc."""
        ...

    def get_positions(self) -> list[Position]:
        """Return all open positions from the Alpaca portfolio."""
        ...

    def get_position(self, symbol: str) -> Optional[Position]:
        """Return the open position for *symbol*, or None if flat."""
        ...

    def get_orders(self, status: str = "open") -> list[Order]:
        """Return orders filtered by status ('open' | 'closed' | 'all')."""
        ...

    # ------------------------------------------------------------------
    # Historical data
    # ------------------------------------------------------------------

    def get_bars(
        self,
        symbols: list[str],
        timeframe: str,
        start: datetime,
        end: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """Fetch historical OHLCV bars and return a multi-index DataFrame.

        Parameters
        ----------
        symbols:
            List of tickers to fetch.
        timeframe:
            Alpaca timeframe string e.g. "1Day", "1Hour".
        start:
            Start of the query window (UTC-aware datetime).
        end:
            End of the query window; defaults to now.
        limit:
            Maximum bars per symbol.

        Returns
        -------
        pd.DataFrame
            MultiIndex (symbol, timestamp) with columns: open, high, low, close, volume.
        """
        ...

    def get_latest_bars(
        self, symbols: list[str], timeframe: str = "1Day"
    ) -> pd.DataFrame:
        """Convenience: fetch the single most recent bar for each symbol."""
        ...

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def subscribe_bars(
        self,
        symbols: list[str],
        callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Subscribe to real-time bar updates and invoke *callback* on each."""
        ...

    def subscribe_quotes(
        self,
        symbols: list[str],
        callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Subscribe to real-time quote updates."""
        ...

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_market_open(self) -> bool:
        """Return True if the US equity market is currently in regular session."""
        ...

    def _retry(self, fn: Callable, retries: int = 3, backoff: float = 1.0) -> Any:
        """Call *fn* with exponential backoff on transient API errors."""
        ...
