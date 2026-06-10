"""Alpaca API wrapper.

Wraps both the REST API (alpaca-py) and the streaming data feed
(websocket-client) behind a clean interface so the rest of the system
never imports alpaca-py directly.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import pandas as pd
from alpaca.data import StockHistoricalDataClient
from alpaca.data.live import StockDataStream
from alpaca.data.requests import StockBarsRequest, StockLatestBarRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.models import TradeAccount, Order, Position
from alpaca.trading.requests import GetOrdersRequest

logger = logging.getLogger(__name__)

_TIMEFRAME_MAP: dict[str, TimeFrame] = {
    "1Min":  TimeFrame.Minute,
    "5Min":  TimeFrame(5, TimeFrameUnit.Minute),
    "15Min": TimeFrame(15, TimeFrameUnit.Minute),
    "1Hour": TimeFrame.Hour,
    "4Hour": TimeFrame(4, TimeFrameUnit.Hour),
    "1Day":  TimeFrame.Day,
    "1Week": TimeFrame.Week,
    "1Month": TimeFrame.Month,
}


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
        self._stream: Optional[StockDataStream] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Initialise TradingClient and StockHistoricalDataClient."""
        self._trading = TradingClient(
            api_key=self.config.api_key,
            secret_key=self.config.secret_key,
            paper=self.config.paper,
        )
        self._data = StockHistoricalDataClient(
            api_key=self.config.api_key,
            secret_key=self.config.secret_key,
        )
        logger.info("AlpacaClient connected (paper=%s)", self.config.paper)

    def disconnect(self) -> None:
        """Close any open streaming connections gracefully."""
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception as exc:
                logger.warning("Error stopping stream: %s", exc)
            self._stream = None
        logger.info("AlpacaClient disconnected")

    # ------------------------------------------------------------------
    # TradeAccount & portfolio queries
    # ------------------------------------------------------------------

    def get_account(self) -> TradeAccount:
        """Return Alpaca TradeAccount object with current NAV, buying power, etc."""
        return self._retry(lambda: self._trading.get_account())

    def get_positions(self) -> list[Position]:
        """Return all open positions from the Alpaca portfolio."""
        return self._retry(lambda: self._trading.get_all_positions())

    def get_position(self, symbol: str) -> Optional[Position]:
        """Return the open position for *symbol*, or None if flat."""
        try:
            return self._retry(lambda: self._trading.get_open_position(symbol))
        except Exception:
            return None

    def get_orders(self, status: str = "open") -> list[Order]:
        """Return orders filtered by status ('open' | 'closed' | 'all')."""
        req = GetOrdersRequest(status=QueryOrderStatus(status))
        return self._retry(lambda: self._trading.get_orders(req))

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
        tf = _TIMEFRAME_MAP.get(timeframe, TimeFrame.Day)
        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=tf,
            start=start,
            end=end or datetime.now(timezone.utc),
            limit=limit,
        )
        bars = self._retry(lambda: self._data.get_stock_bars(req))
        df = bars.df.copy()
        df.index.names = ["symbol", "timestamp"]
        df.columns = [c.lower() for c in df.columns]
        return df[["open", "high", "low", "close", "volume"]]

    def get_latest_bars(
        self, symbols: list[str], timeframe: str = "1Day"
    ) -> pd.DataFrame:
        """Convenience: fetch the single most recent bar for each symbol."""
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=7)
        return self.get_bars(symbols, timeframe, start=start, end=end, limit=1)

    def get_latest_price(self, symbol: str) -> float:
        """Return the latest trade price for *symbol*."""
        req = StockLatestBarRequest(symbol_or_symbols=[symbol])
        resp = self._retry(lambda: self._data.get_stock_latest_bar(req))
        bar = resp[symbol]
        return float(bar.close)

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def subscribe_bars(
        self,
        symbols: list[str],
        callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Subscribe to real-time bar updates and invoke *callback* on each."""
        self._stream = StockDataStream(
            api_key=self.config.api_key,
            secret_key=self.config.secret_key,
        )

        async def _handler(bar: Any) -> None:
            callback(bar)

        self._stream.subscribe_bars(_handler, *symbols)
        self._stream.run()

    def subscribe_quotes(
        self,
        symbols: list[str],
        callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Subscribe to real-time quote updates."""
        self._stream = StockDataStream(
            api_key=self.config.api_key,
            secret_key=self.config.secret_key,
        )

        async def _handler(quote: Any) -> None:
            callback(quote)

        self._stream.subscribe_quotes(_handler, *symbols)
        self._stream.run()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_market_open(self) -> bool:
        """Return True if the US equity market is currently in regular session."""
        clock = self._retry(lambda: self._trading.get_clock())
        return bool(clock.is_open)

    def _retry(self, fn: Callable, retries: int = 3, backoff: float = 1.0) -> Any:
        """Call *fn* with exponential backoff on transient API errors."""
        last_exc: Optional[Exception] = None
        for attempt in range(retries):
            try:
                return fn()
            except Exception as exc:
                last_exc = exc
                if attempt < retries - 1:
                    wait = backoff * (2 ** attempt)
                    logger.warning(
                        "Alpaca API error (attempt %d/%d): %s — retrying in %.1fs",
                        attempt + 1, retries, exc, wait,
                    )
                    time.sleep(wait)
        raise last_exc  # type: ignore[misc]
