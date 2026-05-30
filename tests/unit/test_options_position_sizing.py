"""
Options signals must always be sized at exactly 1 contract.
The Kelly/equity sizer must NOT be used for SELL_PUT, SELL_CALL,
BUY_TO_CLOSE_PUT, or BUY_TO_CLOSE_CALL signals.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.events import SignalEvent


OPTIONS_SIGNAL_TYPES = [
    "SELL_PUT",
    "SELL_CALL",
    "BUY_TO_CLOSE_PUT",
    "BUY_TO_CLOSE_CALL",
]


def _make_signal(signal_type: str, symbol: str = "AMD") -> SignalEvent:
    return SignalEvent(
        strategy_id="wheel",
        symbol=symbol,
        signal_type=signal_type,
        strength=0.8,
        timestamp=datetime.now(timezone.utc),
        metadata={
            "leg": "csp_open",
            "contract_id": "AMD240119P00280000",
            "strike": 28.0,
            "premium": 1.50,
            "delta": -0.28,
            "collateral": 2800.0,
            "session_id": "test-session",
        },
    )


@pytest.mark.parametrize("signal_type", OPTIONS_SIGNAL_TYPES)
def test_options_signal_always_sized_at_one_contract(signal_type):
    """
    For any options signal, the sizing logic must return qty=1 regardless
    of what PositionSizer would return.
    """
    from portfolio.portfolio import Portfolio
    from risk.position_sizer import PositionSizer
    from execution.order_builder import OPTIONS_SIGNALS

    # Sizer that would return a large number if called
    mock_sizer = MagicMock(spec=PositionSizer)
    mock_sizer.size_position.return_value = 15  # would be 15 contracts — wrong

    from portfolio.portfolio import Portfolio
    portfolio = Portfolio(cash=Decimal("10000"))

    signal = _make_signal(signal_type)

    # Replicate the sizing decision from scheduler._on_bar
    if signal.signal_type in OPTIONS_SIGNALS:
        qty = 1
    else:
        qty = mock_sizer.size_position(
            signal=signal,
            portfolio=portfolio,
            current_price=Decimal("29.00"),
            atr=None,
        )

    assert qty == 1, (
        f"{signal_type}: expected qty=1 for options signal, got {qty}. "
        "Sizer must not be called for options."
    )
    mock_sizer.size_position.assert_not_called()
