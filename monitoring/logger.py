"""Structured logging configuration.

Sets up a JSON-structured log pipeline so that every component emits
machine-readable records that can be ingested by log aggregators (Datadog,
CloudWatch, Loki, etc.) while also rendering human-friendly output in the
terminal via Rich.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.logging import RichHandler


LOG_FORMAT_JSON = "%(asctime)s %(levelname)s %(name)s %(message)s"
DEFAULT_LOG_DIR = Path("logs")


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object.

    Includes standard fields (timestamp, level, logger, message) plus any
    extra key-value pairs attached via the *extra=* argument to log calls.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Serialise *record* to a JSON string."""
        ...


class StructuredLogger(logging.Logger):
    """Logger subclass with convenience methods for structured key-value logging.

    Usage
    -----
    logger = get_logger(__name__)
    logger.event("order_filled", symbol="SPY", shares=10, price=450.0)
    """

    def event(self, event_name: str, **kwargs) -> None:
        """Log a named event with arbitrary key-value metadata at INFO level."""
        ...

    def trade(self, symbol: str, side: str, shares: float, price: float, **kwargs) -> None:
        """Convenience: log a trade execution event."""
        ...

    def regime_change(self, old_regime: str, new_regime: str, confidence: float) -> None:
        """Convenience: log a regime transition."""
        ...

    def risk_alert(self, alert_type: str, value: float, threshold: float) -> None:
        """Convenience: log a risk threshold breach."""
        ...


def setup_logging(
    level: str = "INFO",
    log_dir: Optional[Path] = None,
    json_file: bool = True,
    rich_console: bool = True,
) -> None:
    """Configure the root logger with Rich (console) and JSON (file) handlers.

    Parameters
    ----------
    level:
        Minimum log level as a string (e.g. "DEBUG", "INFO").
    log_dir:
        Directory for JSON log files.  Defaults to DEFAULT_LOG_DIR.
    json_file:
        Whether to emit JSON logs to a rotating file.
    rich_console:
        Whether to render pretty logs in the terminal via Rich.
    """
    ...


def get_logger(name: str) -> StructuredLogger:
    """Return a StructuredLogger for the given module name.

    Parameters
    ----------
    name:
        Typically ``__name__`` of the calling module.

    Returns
    -------
    StructuredLogger
    """
    ...
