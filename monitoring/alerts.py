"""Email and webhook alerts for critical trading events.

AlertManager dispatches notifications when risk thresholds are breached,
regime changes occur, large trades execute, or the system encounters errors.
A configurable rate-limit prevents alert storms.
"""

from __future__ import annotations

import logging
import smtplib
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class AlertConfig:
    """Alert delivery configuration."""

    smtp_host: str = ""
    smtp_port: int = 587
    sender_email: str = ""
    sender_password: str = ""
    recipient_email: str = ""
    slack_webhook_url: str = ""
    discord_webhook_url: str = ""
    rate_limit_minutes: int = 15    # minimum gap between duplicate alert types


@dataclass
class Alert:
    """An alert event to be dispatched."""

    alert_type: str          # e.g. "daily_dd_halt", "regime_change", "fill_error"
    severity: AlertSeverity
    subject: str
    body: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metadata: dict = field(default_factory=dict)


class AlertManager:
    """Dispatches email and webhook alerts with rate-limiting.

    Parameters
    ----------
    config:
        SMTP and webhook credentials plus rate-limit settings.

    Responsibilities
    ----------------
    - Accept Alert objects and route them to all configured channels.
    - Enforce per-alert-type rate limiting so repeated events don't flood
      inboxes / Slack channels.
    - Support async-safe dispatch so alerts don't block the trading loop.
    - Provide convenience factory methods for common alert types.
    """

    def __init__(self, config: AlertConfig) -> None:
        self.config = config
        self._last_sent: dict[str, datetime] = {}    # alert_type → last send time

    # ------------------------------------------------------------------
    # Main dispatch entry point
    # ------------------------------------------------------------------

    def send(self, alert: Alert) -> bool:
        """Dispatch *alert* to all configured channels, subject to rate-limiting.

        Parameters
        ----------
        alert:
            The alert to send.

        Returns
        -------
        bool
            True if the alert was dispatched (not rate-limited).
        """
        ...

    # ------------------------------------------------------------------
    # Convenience factory methods
    # ------------------------------------------------------------------

    def regime_change(
        self, old_regime: str, new_regime: str, confidence: float
    ) -> None:
        """Send an INFO alert for a regime transition."""
        ...

    def drawdown_halt(self, level: str, drawdown_pct: float) -> None:
        """Send a CRITICAL alert when a drawdown halt is triggered.

        Parameters
        ----------
        level:
            "daily" or "weekly".
        drawdown_pct:
            The drawdown percentage that triggered the halt.
        """
        ...

    def order_error(self, symbol: str, error_message: str) -> None:
        """Send a WARNING alert for a failed order submission."""
        ...

    def system_error(self, component: str, error_message: str) -> None:
        """Send a CRITICAL alert for an unexpected exception in a core component."""
        ...

    def large_fill(
        self, symbol: str, side: str, shares: float, value: float
    ) -> None:
        """Send an INFO alert when a fill exceeds a notional size threshold."""
        ...

    # ------------------------------------------------------------------
    # Delivery channels
    # ------------------------------------------------------------------

    def _send_email(self, alert: Alert) -> bool:
        """Dispatch *alert* via SMTP.  Returns True on success."""
        ...

    def _send_slack(self, alert: Alert) -> bool:
        """POST *alert* to the Slack incoming webhook URL."""
        ...

    def _send_discord(self, alert: Alert) -> bool:
        """POST *alert* to the Discord webhook URL."""
        ...

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _is_rate_limited(self, alert_type: str) -> bool:
        """Return True if this alert type was sent within rate_limit_minutes."""
        ...

    def _record_sent(self, alert_type: str) -> None:
        """Record that *alert_type* was just dispatched."""
        ...

    def _format_body(self, alert: Alert) -> str:
        """Format a plain-text body with metadata appended."""
        ...
