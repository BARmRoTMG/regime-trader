"""SQLite persistence layer for regime-trader.

Modules
-------
database  — connection management and schema migrations
models    — dataclass representations of every table row
queries   — typed read/write helpers used by the API layer
"""

from db.database import Database, get_db
from db.models import Account, Trade, Signal, EquitySnapshot, Strategy

__all__ = [
    "Database", "get_db",
    "Account", "Trade", "Signal", "EquitySnapshot", "Strategy",
]
