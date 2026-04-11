"""Tests for assignment detection from Alpaca trade_updates stream."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from broker.market_data import MarketDataStream
from core.events import FillEvent


def _make_assignment_update(symbol="AMD", qty=100, strike=120.0):
    update = MagicMock()
    update.event = "assigned"
    update.timestamp = datetime.now(timezone.utc)

    order = MagicMock()
    order.id = "assign-order-001"
    order.symbol = symbol
    order.client_order_id = "wheel"
    order.side = MagicMock()
    order.side.value = "sell"

    update.order = order
    update.qty = str(qty)
    update.price = str(strike)
    return update


@pytest.mark.asyncio
async def test_assignment_creates_fill_event_with_leg_metadata():
    """Assignment event from Alpaca must become a FillEvent with leg='assignment'."""
    received: list[FillEvent] = []

    async def capture_fill(fill: FillEvent) -> None:
        received.append(fill)

    stream = MarketDataStream()
    stream._fill_handler = capture_fill

    update = _make_assignment_update(symbol="AMD", qty=100, strike=120.0)
    await stream._on_trade_update(update)

    assert len(received) == 1
    fill = received[0]
    assert fill.symbol == "AMD"
    assert fill.filled_qty == 100
    assert fill.metadata.get("leg") == "assignment"
    assert fill.is_options is True
    assert fill.strategy_id == "wheel"


@pytest.mark.asyncio
async def test_non_assignment_fill_event_is_not_flagged():
    """Regular 'new' order acknowledgements are ignored."""
    received: list[FillEvent] = []

    async def capture_fill(fill: FillEvent) -> None:
        received.append(fill)

    stream = MarketDataStream()
    stream._fill_handler = capture_fill

    update = MagicMock()
    update.event = "new"
    await stream._on_trade_update(update)

    assert received == []
