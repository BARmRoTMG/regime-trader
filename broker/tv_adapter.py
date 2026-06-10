"""TradingView webhook payload → internal Signal converter.

TradingView sends a JSON payload when an alert fires.  This module:
  1. Defines the expected payload schema (TVPayload).
  2. Normalises TradingView ticker format ("NASDAQ:AAPL" → "AAPL").
  3. Converts the payload into the project's Signal dataclass so the
     RiskManager can validate it before the order reaches Alpaca.

Pine Script alert message format (set in TradingView alert dialog):
{
  "symbol":     "{{ticker}}",
  "action":     "buy",
  "price":      {{close}},
  "stop":       0.0,
  "take_profit": 0.0,
  "timeframe":  "{{interval}}",
  "strategy":   "regime_trader_v1",
  "secret":     "YOUR_WEBHOOK_SECRET"
}

The `secret` field is validated by tv_webhook.py before calling this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

from core.regime_strategies import Direction, Signal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Payload schema
# ---------------------------------------------------------------------------


@dataclass
class TVPayload:
    """Parsed and validated TradingView webhook payload."""

    symbol: str              # normalised ticker, e.g. "AAPL"
    action: str              # "buy" | "sell" | "flat"
    price: float             # alert price (close of triggering bar)
    stop: float              # stop-loss price; 0.0 if not provided
    take_profit: float       # take-profit price; 0.0 if not provided
    timeframe: str           # e.g. "D", "60", "15"
    strategy: str            # free-form strategy name from Pine Script
    raw: dict = field(default_factory=dict)  # original payload for audit log


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_payload(payload: dict[str, Any]) -> TVPayload:
    """Parse and validate a raw TradingView webhook dict.

    Parameters
    ----------
    payload:
        Decoded JSON body from the POST /alert endpoint.

    Returns
    -------
    TVPayload

    Raises
    ------
    ValueError
        If required fields (symbol, action, price) are missing or invalid.
    """
    _require(payload, "symbol", "action", "price")

    symbol = _normalise_symbol(str(payload["symbol"]))
    action = str(payload.get("action", "flat")).lower().strip()
    if action not in ("buy", "sell", "flat", "close"):
        raise ValueError(f"Invalid action '{action}'. Must be buy | sell | flat | close.")

    price = float(payload["price"])
    if price <= 0:
        raise ValueError(f"Price must be positive; got {price}")

    stop = float(payload.get("stop", 0.0))
    take_profit = float(payload.get("take_profit", 0.0))

    return TVPayload(
        symbol=symbol,
        action=action if action != "close" else "sell",
        price=price,
        stop=stop,
        take_profit=take_profit,
        timeframe=str(payload.get("timeframe", "D")),
        strategy=str(payload.get("strategy", "tradingview")),
        raw=dict(payload),
    )


def to_signal(
    tv: TVPayload,
    regime_id: int = -1,
    regime_name: str = "UNKNOWN",
    regime_probability: float = 1.0,
    confidence: float = 1.0,
    position_size_pct: float = 0.10,
    leverage: float = 1.0,
) -> Signal:
    """Convert a TVPayload into the project's Signal dataclass.

    Parameters
    ----------
    tv:
        Parsed TradingView payload.
    regime_id:
        Active HMM regime index (-1 when HMM is unavailable).
    regime_name:
        Human-readable regime label (e.g. "LOW_VOL", "HIGH_VOL").
    regime_probability:
        Posterior probability of the active regime.
    confidence:
        Confidence passed to RiskManager's leverage check.
    position_size_pct:
        Fraction of NAV to allocate (before leverage).  The RiskManager
        will apply its own caps; this is the starting proposal.
    leverage:
        Proposed leverage multiplier (1.0 or 1.25).

    Returns
    -------
    Signal
    """
    direction = Direction.LONG if tv.action == "buy" else Direction.FLAT
    stop = tv.stop if tv.stop > 0 else tv.price * 0.97  # default 3% stop
    tp = tv.take_profit if tv.take_profit > 0 else None

    return Signal(
        symbol=tv.symbol,
        direction=direction,
        confidence=confidence,
        entry_price=tv.price,
        stop_loss=stop,
        take_profit=tp,
        position_size_pct=position_size_pct,
        leverage=leverage,
        regime_id=regime_id,
        regime_name=regime_name,
        regime_probability=regime_probability,
        timestamp=pd.Timestamp.now(tz="UTC"),
        reasoning=f"TradingView alert | strategy={tv.strategy} | tf={tv.timeframe}",
        strategy_name=tv.strategy,
        metadata={
            "source": "tradingview",
            "timeframe": tv.timeframe,
            "raw_symbol": tv.raw.get("symbol", tv.symbol),
            "is_uncertain": regime_id == -1,
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_symbol(raw: str) -> str:
    """Strip exchange prefix from TradingView ticker format.

    Examples
    --------
    "NASDAQ:AAPL"  → "AAPL"
    "NYSE:IBM"     → "IBM"
    "AAPL"         → "AAPL"
    "BINANCE:BTCUSDT" → "BTCUSDT"  (crypto tickers also supported)
    """
    return raw.split(":")[-1].upper().strip()


def _require(payload: dict, *keys: str) -> None:
    missing = [k for k in keys if k not in payload or payload[k] is None]
    if missing:
        raise ValueError(f"TradingView payload missing required fields: {missing}")
