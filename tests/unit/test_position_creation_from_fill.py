"""
Tests that on_fill creates CSPPosition/CCPosition objects so position
management doesn't reset to SCANNING on the next bar.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from core.events import FillEvent
from strategies.wheel.csp_leg import CSPPosition, OptionContract
from strategies.wheel.covered_call_leg import CCPosition


def _make_contract(contract_id: str, strike: float = 28.0, option_type: str = "put") -> OptionContract:
    c = MagicMock(spec=OptionContract)
    c.contract_id = contract_id
    c.strike = Decimal(str(strike))
    c.dte = 30
    c.option_type = option_type
    c.bid = Decimal("1.20")
    c.ask = Decimal("1.40")
    c.delta = -0.28
    c.iv = 0.45
    return c


def _make_fill(leg: str, side: str, fill_price: float, symbol: str = "AMD",
               contract_id: str = "AMD240119P00280000") -> FillEvent:
    f = MagicMock(spec=FillEvent)
    f.strategy_id = "wheel"
    f.symbol = symbol
    f.side = side
    f.fill_price = Decimal(str(fill_price))
    f.filled_qty = 100
    f.filled_at = datetime.now(timezone.utc)
    f.metadata = {"leg": leg, "contract_id": contract_id, "underlying_price": 30.0}
    return f


def _make_wheel(symbols=None):
    from strategies.wheel.wheel_strategy import WheelStrategy, WheelPosition, WheelState
    with patch("strategies.wheel.wheel_strategy.settings") as ms:
        ms.indicators.bar_window_size = 200
        ms.strategies.wheel.csp.min_iv_rank = 40
        ms.strategies.wheel.csp.pain_threshold_default = 0.85
        w = WheelStrategy.__new__(WheelStrategy)
    w.symbols = symbols or ["AMD"]
    w.strategy_id = "wheel"
    w._advisor = None
    w._positions = {sym: WheelPosition(symbol=sym) for sym in w.symbols}
    w._csp_leg = MagicMock()
    w._csp_leg.cost_basis_after_assignment.return_value = Decimal("26.80")
    w._cc_leg = MagicMock()
    return w


# ---------------------------------------------------------------------------
# CSP creation
# ---------------------------------------------------------------------------

def test_csp_position_created_after_csp_open_fill():
    """After a csp_open fill, pos.csp_position must be a CSPPosition (not None)."""
    from strategies.wheel.wheel_strategy import WheelState
    w = _make_wheel()
    contract = _make_contract("AMD240119P00280000", strike=28.0, option_type="put")
    pos = w._positions["AMD"]
    pos.cached_chain = [contract]

    fill = _make_fill("csp_open", "sell", 1.50, contract_id="AMD240119P00280000")
    w.on_fill(fill)

    assert pos.csp_position is not None, "CSPPosition must be created on csp_open fill"
    assert isinstance(pos.csp_position, CSPPosition)
    assert pos.csp_position.premium_received == Decimal("1.50")
    assert pos.csp_position.contract.contract_id == "AMD240119P00280000"
    assert pos.state == WheelState.CSP_OPEN


def test_csp_position_stores_underlying_price_at_entry():
    """CSPPosition.underlying_price_at_entry must be set from fill metadata."""
    w = _make_wheel()
    contract = _make_contract("AMD240119P00280000")
    pos = w._positions["AMD"]
    pos.cached_chain = [contract]

    fill = _make_fill("csp_open", "sell", 1.50, contract_id="AMD240119P00280000")
    fill.metadata["underlying_price"] = 30.0
    w.on_fill(fill)

    assert pos.csp_position is not None
    assert pos.csp_position.underlying_price_at_entry == Decimal("30.0")


def test_csp_position_none_when_contract_not_in_chain():
    """If contract_id is missing from chain, pos.csp_position stays None (safe fallback)."""
    from strategies.wheel.wheel_strategy import WheelState
    w = _make_wheel()
    pos = w._positions["AMD"]
    pos.cached_chain = []  # empty chain

    fill = _make_fill("csp_open", "sell", 1.50, contract_id="MISSING_ID")
    w.on_fill(fill)

    assert pos.csp_position is None
    assert pos.state == WheelState.CSP_OPEN  # state still advances; _manage_csp will reset it


def test_manage_csp_does_not_reset_state_when_csp_position_exists():
    """After csp_open fill, on_bar must NOT reset state to SCANNING."""
    from strategies.wheel.wheel_strategy import WheelState
    from core.events import BarEvent
    w = _make_wheel()
    contract = _make_contract("AMD240119P00280000")
    pos = w._positions["AMD"]
    pos.cached_chain = [contract]

    # Open the CSP
    fill = _make_fill("csp_open", "sell", 1.50, contract_id="AMD240119P00280000")
    w.on_fill(fill)
    assert pos.state == WheelState.CSP_OPEN

    # Simulate next bar — _manage_csp should be called, not reset
    bar = MagicMock(spec=BarEvent)
    bar.symbol = "AMD"
    bar.close = Decimal("29.00")
    bar.timestamp = datetime.now(timezone.utc)

    w._csp_leg.should_close_early.return_value = (False, "")
    w._update_indicators = MagicMock(return_value=MagicMock())
    w._bars_available = MagicMock(return_value=True)
    w._get_contract_price = MagicMock(return_value=Decimal("1.50"))

    signals = w.on_bar(bar)

    assert pos.state == WheelState.CSP_OPEN, (
        f"State should remain CSP_OPEN but got {pos.state}. "
        "Likely csp_position was None (position creation bug)."
    )


# ---------------------------------------------------------------------------
# CC creation
# ---------------------------------------------------------------------------

def test_cc_position_created_after_cc_open_fill():
    """After a cc_open fill, pos.cc_position must be a CCPosition (not None)."""
    from strategies.wheel.wheel_strategy import WheelState
    w = _make_wheel()
    contract = _make_contract("AMD240119C00030000", strike=30.0, option_type="call")
    pos = w._positions["AMD"]
    pos.cached_chain = [contract]
    pos.stock_cost_basis = Decimal("26.80")
    pos.state = WheelState.ASSIGNED

    fill = _make_fill("cc_open", "sell", 0.80, contract_id="AMD240119C00030000")
    fill.metadata = {"leg": "cc_open", "contract_id": "AMD240119C00030000"}
    w.on_fill(fill)

    assert pos.cc_position is not None, "CCPosition must be created on cc_open fill"
    assert isinstance(pos.cc_position, CCPosition)
    assert pos.cc_position.premium_received == Decimal("0.80")
    assert pos.cc_position.contract.contract_id == "AMD240119C00030000"
    assert pos.cc_position.stock_cost_basis == Decimal("26.80")
    assert pos.state == WheelState.CC_OPEN


def test_manage_cc_does_not_reset_state_when_cc_position_exists():
    """After cc_open fill, on_bar must NOT reset state to SCANNING."""
    from strategies.wheel.wheel_strategy import WheelState
    from core.events import BarEvent
    w = _make_wheel()
    contract = _make_contract("AMD240119C00030000", strike=30.0, option_type="call")
    pos = w._positions["AMD"]
    pos.cached_chain = [contract]
    pos.stock_cost_basis = Decimal("26.80")
    pos.state = WheelState.ASSIGNED

    fill = _make_fill("cc_open", "sell", 0.80, contract_id="AMD240119C00030000")
    fill.metadata = {"leg": "cc_open", "contract_id": "AMD240119C00030000"}
    w.on_fill(fill)
    assert pos.state == WheelState.CC_OPEN

    bar = MagicMock(spec=BarEvent)
    bar.symbol = "AMD"
    bar.close = Decimal("28.00")
    bar.timestamp = datetime.now(timezone.utc)

    w._cc_leg.should_close_early.return_value = (False, "")
    w._update_indicators = MagicMock(return_value=MagicMock())
    w._bars_available = MagicMock(return_value=True)
    w._get_contract_price = MagicMock(return_value=Decimal("0.80"))

    w.on_bar(bar)

    assert pos.state == WheelState.CC_OPEN, (
        f"State should remain CC_OPEN but got {pos.state}. "
        "Likely cc_position was None (position creation bug)."
    )
