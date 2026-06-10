"""Track open positions, real-time P&L, and portfolio-level metrics.

PositionTracker maintains an in-process view of the portfolio that is
continuously reconciled against Alpaca's REST API so signal generation
and risk management always see current state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
        self._cash: float = 0.0

    # ------------------------------------------------------------------
    # Snapshot / query
    # ------------------------------------------------------------------

    def snapshot(self) -> PortfolioSnapshot:
        """Return the current PortfolioSnapshot (uses cached in-memory state)."""
        now = datetime.now(timezone.utc)
        nav = self._compute_nav(self._cash)
        gross = sum(abs(p.market_value) for p in self._positions.values())
        net = sum(
            p.market_value if p.side == "long" else -p.market_value
            for p in self._positions.values()
        )
        unrealised = sum(p.unrealised_pnl for p in self._positions.values())
        return PortfolioSnapshot(
            timestamp=now,
            nav=nav,
            cash=self._cash,
            gross_exposure=gross,
            net_exposure=net,
            unrealised_pnl=unrealised,
            daily_pnl=self.daily_pnl(),
            positions=list(self._positions.values()),
            weights=self.get_weights(nav),
        )

    def get_position(self, symbol: str) -> Optional[PositionRecord]:
        """Return the PositionRecord for *symbol*, or None if not held."""
        return self._positions.get(symbol)

    def get_weights(self, nav: float) -> dict[str, float]:
        """Return current portfolio weights as fractions of *nav*."""
        if nav <= 0:
            return {}
        return {sym: p.market_value / nav for sym, p in self._positions.items()}

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
        """Update in-memory position state when an order is filled."""
        side = side.lower()
        if side == "buy":
            self._update_position_on_buy(symbol, shares, fill_price, filled_at)
        elif side in ("sell", "close"):
            realised = self._update_position_on_sell(symbol, shares, fill_price)
            self._realised_pnl += realised
        else:
            logger.warning("Unknown fill side '%s' for %s", side, symbol)

    def update_prices(self, prices: dict[str, float]) -> None:
        """Update current_price and recompute unrealised P&L for all positions."""
        for symbol, price in prices.items():
            rec = self._positions.get(symbol)
            if rec is None:
                continue
            rec.current_price = price
            rec.market_value = rec.shares * price
            rec.unrealised_pnl = (price - rec.avg_entry_price) * rec.shares
            if rec.avg_entry_price > 0:
                rec.unrealised_pnl_pct = (price / rec.avg_entry_price) - 1.0
            rec.last_updated = datetime.now(timezone.utc)

    def reconcile(self) -> None:
        """Pull live positions from Alpaca REST and sync _positions."""
        try:
            alpaca_positions = self.client.get_positions()
            account = self.client.get_account()
            self._cash = float(account.cash)

            live_symbols: set[str] = set()
            for pos in alpaca_positions:
                sym = str(pos.symbol)
                live_symbols.add(sym)
                shares = float(pos.qty)
                avg_price = float(pos.avg_entry_price)
                current = float(pos.current_price or avg_price)
                mv = float(pos.market_value or shares * current)
                upnl = float(pos.unrealized_pl or 0)
                upnl_pct = float(pos.unrealized_plpc or 0)
                side = "long" if shares >= 0 else "short"
                now = datetime.now(timezone.utc)

                existing = self._positions.get(sym)
                opened_at = existing.opened_at if existing else now

                self._positions[sym] = PositionRecord(
                    symbol=sym,
                    shares=abs(shares),
                    avg_entry_price=avg_price,
                    current_price=current,
                    market_value=abs(mv),
                    unrealised_pnl=upnl,
                    unrealised_pnl_pct=upnl_pct,
                    side=side,
                    opened_at=opened_at,
                    last_updated=now,
                )

            # Remove positions closed externally
            for sym in list(self._positions.keys()):
                if sym not in live_symbols:
                    logger.info("Reconcile: %s closed externally, removing", sym)
                    del self._positions[sym]

            logger.debug(
                "Reconciled: %d positions, cash=%.2f",
                len(self._positions), self._cash,
            )
        except Exception as exc:
            logger.error("reconcile() failed: %s", exc)

    def reset_daily(self, current_nav: float) -> None:
        """Record today's opening NAV to compute intraday P&L."""
        self._day_open_nav = current_nav
        self._realised_pnl = 0.0
        logger.info("Daily reset: opening NAV=%.2f", current_nav)

    # ------------------------------------------------------------------
    # P&L helpers
    # ------------------------------------------------------------------

    def daily_pnl(self) -> float:
        """Unrealised + realised P&L since _day_open_nav was set."""
        unrealised = sum(p.unrealised_pnl for p in self._positions.values())
        return unrealised + self._realised_pnl

    def daily_pnl_pct(self) -> float:
        """daily_pnl() as a fraction of _day_open_nav."""
        if self._day_open_nav <= 0:
            return 0.0
        return self.daily_pnl() / self._day_open_nav

    def history(self, n_days: int = 30) -> pd.DataFrame:
        """Return a DataFrame of daily NAV and P&L snapshots (if persisted)."""
        # In-memory only — returns empty frame until persistence is added.
        return pd.DataFrame(columns=["date", "nav", "daily_pnl", "daily_pnl_pct"])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_nav(self, cash: float) -> float:
        """Sum cash + all position market values."""
        return cash + sum(p.market_value for p in self._positions.values())

    def _update_position_on_buy(
        self, symbol: str, shares: float, price: float, ts: datetime
    ) -> None:
        """Open a new position or average-up an existing long."""
        existing = self._positions.get(symbol)
        if existing is None:
            self._positions[symbol] = PositionRecord(
                symbol=symbol,
                shares=shares,
                avg_entry_price=price,
                current_price=price,
                market_value=shares * price,
                unrealised_pnl=0.0,
                unrealised_pnl_pct=0.0,
                side="long",
                opened_at=ts,
                last_updated=ts,
            )
        else:
            total_shares = existing.shares + shares
            avg_price = (
                (existing.avg_entry_price * existing.shares) + (price * shares)
            ) / total_shares
            existing.shares = total_shares
            existing.avg_entry_price = avg_price
            existing.current_price = price
            existing.market_value = total_shares * price
            existing.unrealised_pnl = (price - avg_price) * total_shares
            existing.last_updated = ts

    def _update_position_on_sell(
        self, symbol: str, shares: float, price: float
    ) -> float:
        """Reduce or close a position; return the realised P&L."""
        existing = self._positions.get(symbol)
        if existing is None:
            logger.warning("Sell fill for %s but no open position tracked", symbol)
            return 0.0

        realised = (price - existing.avg_entry_price) * min(shares, existing.shares)
        remaining = existing.shares - shares
        if remaining <= 0:
            del self._positions[symbol]
            logger.info("Position closed: %s | realised P&L: %.2f", symbol, realised)
        else:
            existing.shares = remaining
            existing.market_value = remaining * existing.current_price
            existing.unrealised_pnl = (existing.current_price - existing.avg_entry_price) * remaining
            existing.last_updated = datetime.now(timezone.utc)
        return realised
