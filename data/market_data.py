"""Real-time and historical market data ingestion.

MarketDataFeed acts as the single source of truth for price data throughout
the system.  It caches historical bars in memory and appends real-time
updates as they arrive so downstream consumers always see a consistent,
up-to-date time series.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import pandas as pd

from broker.alpaca_client import AlpacaClient

logger = logging.getLogger(__name__)


class MarketDataFeed:
    """Manages historical and streaming OHLCV data for the symbol universe.

    Parameters
    ----------
    client:
        Connected AlpacaClient for both REST and streaming.
    symbols:
        List of tickers to track.
    timeframe:
        Bar timeframe string (e.g. "1Day").

    Responsibilities
    ----------------
    - Load a configurable window of historical bars on startup.
    - Subscribe to real-time bar / quote streams and append incoming data.
    - Provide thread-safe read access via get_bars() / get_latest_price().
    - Detect data gaps and back-fill from REST if the stream drops.
    - Expose a callback registration mechanism for subscribers (e.g. the
      SignalGenerator) that want to be notified on each new bar.
    """

    def __init__(
        self,
        client: AlpacaClient,
        symbols: list[str],
        timeframe: str = "1Day",
    ) -> None:
        self.client = client
        self.symbols = symbols
        self.timeframe = timeframe
        self._bars: dict[str, pd.DataFrame] = {}    # symbol → OHLCV DataFrame
        self._callbacks: list[Callable[[str, pd.Series], None]] = []

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def load_history(self, lookback_bars: int = 504) -> None:
        """Fetch historical bars from Alpaca REST and populate the internal cache.

        Parameters
        ----------
        lookback_bars:
            Number of bars to load per symbol (calendar-adjusted).
        """
        ...

    def start_stream(self) -> None:
        """Open a websocket stream and begin appending real-time bars."""
        ...

    def stop_stream(self) -> None:
        """Close the websocket connection gracefully."""
        ...

    # ------------------------------------------------------------------
    # Data access
    # ------------------------------------------------------------------

    def get_bars(
        self,
        symbol: str,
        n: Optional[int] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> pd.DataFrame:
        """Return cached bars for *symbol*, optionally sliced.

        Parameters
        ----------
        symbol:
            Ticker to query.
        n:
            Return only the most recent *n* bars.
        start / end:
            Date range filter (mutually exclusive with *n*).

        Returns
        -------
        pd.DataFrame
            DatetimeIndex, columns: open, high, low, close, volume.
        """
        ...

    def get_latest_price(self, symbol: str) -> float:
        """Return the most recent close price for *symbol*."""
        ...

    def get_latest_prices(self) -> dict[str, float]:
        """Return the most recent close price for every tracked symbol."""
        ...

    def get_multi_symbol_bars(
        self, n: Optional[int] = None
    ) -> pd.DataFrame:
        """Return a MultiIndex DataFrame (symbol, timestamp) for all symbols."""
        ...

    def available_symbols(self) -> list[str]:
        """Return symbols for which at least one bar has been loaded."""
        ...

    # ------------------------------------------------------------------
    # Callback registration
    # ------------------------------------------------------------------

    def on_new_bar(self, callback: Callable[[str, pd.Series], None]) -> None:
        """Register a callable invoked whenever a new bar arrives.

        Parameters
        ----------
        callback:
            Function accepting (symbol, bar_series) as arguments.
        """
        ...

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _handle_bar(self, bar: dict) -> None:
        """Parse a streaming bar event, append to cache, and invoke callbacks."""
        ...

    def _detect_and_fill_gaps(self, symbol: str) -> None:
        """Identify missing bars and back-fill from REST."""
        ...

    def _normalize_bars(self, df: pd.DataFrame) -> pd.DataFrame:
        """Ensure consistent column names and a UTC DatetimeIndex."""
        ...
