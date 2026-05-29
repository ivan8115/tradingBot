"""Tests for assignment detection from Alpaca trade_updates stream."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from broker.market_data import MarketDataStream
from core.events import FillEvent


def _make_assignment_update(symbol="AMD240119P00120000", qty=100, strike=120.0):
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

    update = _make_assignment_update(symbol="AMD240119P00120000", qty=100, strike=120.0)
    await stream._on_trade_update(update)

    assert len(received) == 1
    fill = received[0]
    assert fill.symbol == "AMD240119P00120000"
    assert fill.filled_qty == 100
    assert fill.metadata.get("leg") == "assignment"
    assert fill.is_options is True
    assert fill.strategy_id == "wheel"
    assert fill.option_contract_id == "AMD240119P00120000"


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


@pytest.mark.asyncio
async def test_assignment_with_missing_price_is_skipped():
    """Assignment update without a price should be skipped (no FillEvent emitted)."""
    received: list[FillEvent] = []

    async def capture_fill(fill: FillEvent) -> None:
        received.append(fill)

    stream = MarketDataStream()
    stream._fill_handler = capture_fill

    update = _make_assignment_update(symbol="AMD240119P00120000", qty=100, strike=120.0)
    update.price = None  # simulate missing price

    await stream._on_trade_update(update)

    assert received == []


def test_csp_close_assignment_uses_correct_cost_basis_when_metadata_missing():
    """
    When a CSP closes via csp_close+assigned=True with NO cost_basis in metadata,
    the fallback must use strike - premium_received (not the option buyback price).
    """
    from strategies.wheel.wheel_strategy import WheelStrategy, WheelState, WheelPosition
    from strategies.wheel.csp_leg import CSPPosition, OptionContract, CashSecuredPutLeg
    from core.config import CSPConfig
    from unittest.mock import MagicMock, patch

    with patch("strategies.wheel.wheel_strategy.settings") as ms:
        ms.indicators.bar_window_size = 200
        ms.strategies.wheel.csp.pain_threshold_default = 0.85
        ms.strategies.wheel.csp.min_iv_rank = 40
        w = WheelStrategy.__new__(WheelStrategy)
        w.symbols = ["AMD"]
        w.strategy_id = "wheel"
        w._positions = {"AMD": WheelPosition(symbol="AMD")}
        w._advisor = None

    cfg_mock = MagicMock(spec=CSPConfig)
    cfg_mock.pain_threshold_default = 0.85
    w._csp_leg = CashSecuredPutLeg(cfg_mock)
    # cc_leg not needed for this test
    w._cc_leg = MagicMock()

    contract = MagicMock(spec=OptionContract)
    contract.strike = Decimal("28.00")
    contract.dte = 0
    pos = w._positions["AMD"]
    pos.csp_position = CSPPosition(
        symbol="AMD",
        contract=contract,
        premium_received=Decimal("1.20"),
        opened_at=datetime.now(timezone.utc),
    )
    pos.state = WheelState.CSP_OPEN

    fill = MagicMock(spec=FillEvent)
    fill.strategy_id = "wheel"
    fill.symbol = "AMD"
    fill.side = "buy"
    fill.fill_price = Decimal("5.50")   # option buyback price — WRONG as cost basis
    fill.filled_qty = 100
    fill.metadata = {"leg": "csp_close", "assigned": True}  # no cost_basis key

    w.on_fill(fill)

    expected = Decimal("28.00") - Decimal("1.20")  # strike - premium = $26.80
    assert pos.stock_cost_basis == expected, (
        f"Expected cost_basis={expected}, got {pos.stock_cost_basis}. "
        "Fallback must use strike - premium, not option buyback price."
    )
    assert pos.state == WheelState.ASSIGNED
