"""Converts raw Alpaca SDK objects into internal typed data models."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


def _to_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return default


def normalize_bar(raw: Any, source: str = "live") -> dict:
    """
    Convert an alpaca-py Bar object (or dict) into a normalized dict
    with Decimal prices. This dict maps 1:1 to core.events.BarEvent fields.
    """
    if hasattr(raw, "__dict__"):
        data = raw.__dict__
    elif isinstance(raw, dict):
        data = raw
    else:
        data = {}

    # alpaca-py uses lowercase attribute names on Bar
    symbol = data.get("symbol") or data.get("S", "")
    timestamp = data.get("timestamp") or data.get("t")
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if timestamp is None:
        timestamp = datetime.now(tz=timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    return {
        "symbol": symbol,
        "timestamp": timestamp,
        "open": _to_decimal(data.get("open") or data.get("o")),
        "high": _to_decimal(data.get("high") or data.get("h")),
        "low": _to_decimal(data.get("low") or data.get("l")),
        "close": _to_decimal(data.get("close") or data.get("c")),
        "volume": int(data.get("volume") or data.get("v") or 0),
        "vwap": _to_decimal(data.get("vwap") or data.get("vw")) or None,
        "trade_count": int(data.get("trade_count") or data.get("n") or 0),
        "source": source,
    }


def normalize_quote(raw: Any) -> dict:
    """Normalize a quote (bid/ask) from Alpaca."""
    if hasattr(raw, "__dict__"):
        data = raw.__dict__
    elif isinstance(raw, dict):
        data = raw
    else:
        data = {}

    return {
        "symbol": data.get("symbol") or data.get("S", ""),
        "bid": _to_decimal(data.get("bid_price") or data.get("bp")),
        "ask": _to_decimal(data.get("ask_price") or data.get("ap")),
        "bid_size": int(data.get("bid_size") or data.get("bs") or 0),
        "ask_size": int(data.get("ask_size") or data.get("as_") or 0),
    }
