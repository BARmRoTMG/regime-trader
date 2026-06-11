"""Dataclass models — one per database table.

Each model maps 1-to-1 with a table row and is used as the return type
from query helpers.  They are plain dataclasses (no ORM magic) so they
serialise cleanly to JSON via dataclasses.asdict().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Account:
    """A TradingView-connected Tradovate account profile."""

    id: int
    name: str
    broker: str                  # 'tradovate'
    environment: str             # 'demo' | 'live'
    notes: Optional[str]
    is_active: bool
    created_at: str              # ISO-8601 string from SQLite


@dataclass
class Signal:
    """A single TradingView webhook alert received by the server."""

    id: int
    account_id: int
    symbol: str
    action: str                  # 'buy' | 'sell' | 'flat'
    contracts: Optional[int]
    price: Optional[float]
    stop_price: Optional[float]
    take_profit: Optional[float]
    strategy_name: Optional[str]
    regime: Optional[str]        # 'LOW_VOL' | 'MID_VOL' | 'HIGH_VOL'
    strategy_equity: Optional[float]
    strategy_pnl: Optional[float]
    position_size: Optional[float]
    approved: bool
    rejection_reason: Optional[str]
    raw_payload: Optional[str]   # full JSON string
    received_at: str             # ISO-8601


@dataclass
class Trade:
    """A completed (or still-open) trade reconstructed from signal pairs."""

    id: int
    account_id: int
    symbol: str
    direction: str               # 'long' | 'short'
    contracts: int
    entry_price: float
    exit_price: Optional[float]
    entry_signal_id: Optional[int]
    exit_signal_id: Optional[int]
    strategy_name: Optional[str]
    regime_at_entry: Optional[str]
    point_value: float           # $/point — used for P&L calculation
    pnl: Optional[float]         # (exit - entry) * contracts * point_value
    pnl_pct: Optional[float]
    opened_at: str               # ISO-8601
    closed_at: Optional[str]
    duration_mins: Optional[int]

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def is_winner(self) -> Optional[bool]:
        if self.pnl is None:
            return None
        return self.pnl > 0


@dataclass
class EquitySnapshot:
    """NAV recorded at each TradingView alert, powering the equity curve."""

    id: int
    account_id: int
    equity: float
    cash: Optional[float]
    pnl: Optional[float]
    regime: Optional[str]
    recorded_at: str             # ISO-8601


@dataclass
class Strategy:
    """A registered Pine Script strategy name with its enable/disable state."""

    id: int
    name: str
    description: Optional[str]
    is_enabled: bool
    created_at: str              # ISO-8601
    last_signal: Optional[str]   # ISO-8601 of most recent alert
