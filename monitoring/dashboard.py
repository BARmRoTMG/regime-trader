"""Terminal-based live dashboard powered by Rich.

Renders a continuously refreshed TUI showing regime state, open positions,
P&L, risk metrics, and recent orders in a clean, colour-coded layout.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from broker.position_tracker import PortfolioSnapshot
from core.hmm_engine import RegimeState
from core.signal_generator import SignalPacket


# Colour mapping for regime labels
REGIME_COLOURS: dict[str, str] = {
    "low_vol": "green",
    "mid_vol": "yellow",
    "high_vol": "red",
    "unknown": "dim",
}


class Dashboard:
    """Live terminal dashboard for regime-trader.

    Parameters
    ----------
    refresh_seconds:
        How often to redraw the screen.
    console:
        Optional Rich Console; a new one is created if omitted.

    Responsibilities
    ----------------
    - Maintain the latest snapshot of regime state, portfolio, and signals.
    - Render a multi-panel layout (regime, positions, P&L, orders, log tail).
    - Run the refresh loop in a background thread so it doesn't block trading.
    - Expose an update() method called by the main loop each bar.
    - Cleanly stop the refresh loop on shutdown.
    """

    def __init__(
        self,
        refresh_seconds: int = 5,
        console: Optional[Console] = None,
    ) -> None:
        self.refresh_seconds = refresh_seconds
        self.console = console or Console()
        self._regime_state: Optional[RegimeState] = None
        self._portfolio: Optional[PortfolioSnapshot] = None
        self._last_packet: Optional[SignalPacket] = None
        self._log_lines: list[str] = []
        self._live: Optional[Live] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background refresh thread and open the Live display."""
        ...

    def stop(self) -> None:
        """Signal the refresh thread to stop and close the Live display."""
        ...

    # ------------------------------------------------------------------
    # Data updates (called from the main trading loop)
    # ------------------------------------------------------------------

    def update(
        self,
        regime_state: Optional[RegimeState] = None,
        portfolio: Optional[PortfolioSnapshot] = None,
        packet: Optional[SignalPacket] = None,
    ) -> None:
        """Push fresh data into the dashboard for the next render cycle."""
        ...

    def push_log(self, message: str) -> None:
        """Append a log line to the scrolling log panel (max 20 lines)."""
        ...

    # ------------------------------------------------------------------
    # Layout construction
    # ------------------------------------------------------------------

    def _build_layout(self) -> Layout:
        """Assemble the full Rich Layout tree."""
        ...

    def _render_regime_panel(self) -> Panel:
        """Render the current regime, confidence, and stability status."""
        ...

    def _render_positions_table(self) -> Table:
        """Render a table of open positions with P&L columns."""
        ...

    def _render_portfolio_panel(self) -> Panel:
        """Render NAV, cash, exposure, daily P&L, and drawdown metrics."""
        ...

    def _render_signals_table(self) -> Table:
        """Render the most recent signal packet's trade signals."""
        ...

    def _render_log_panel(self) -> Panel:
        """Render the last N log lines in a scrolling panel."""
        ...

    def _regime_colour(self, regime_label: str) -> str:
        """Return the Rich colour string for a regime label."""
        return REGIME_COLOURS.get(regime_label, "white")

    # ------------------------------------------------------------------
    # Internal refresh loop
    # ------------------------------------------------------------------

    def _refresh_loop(self) -> None:
        """Background thread target: redraw at refresh_seconds interval."""
        ...
