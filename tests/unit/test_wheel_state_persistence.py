"""
WheelStrategy get_state/load_state must persist and restore the full
CSPPosition and CCPosition objects so a bot restart doesn't lose open positions.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from strategies.wheel.csp_leg import CSPPosition, OptionContract
from strategies.wheel.covered_call_leg import CCPosition


def _make_wheel():
    from strategies.wheel.wheel_strategy import WheelStrategy, WheelPosition
    with patch("strategies.wheel.wheel_strategy.settings") as ms:
        ms.indicators.bar_window_size = 200
        ms.strategies.wheel.csp.min_iv_rank = 40
        ms.strategies.wheel.csp.pain_threshold_default = 0.85
        w = WheelStrategy.__new__(WheelStrategy)
    w.symbols = ["AMD"]
    w.strategy_id = "wheel"
    w._advisor = None
    w._positions = {"AMD": WheelPosition(symbol="AMD")}
    w._csp_leg = MagicMock()
    w._cc_leg = MagicMock()
    return w


def _make_option_contract(
    contract_id="AMD240119P00280000",
    option_type="put",
    strike="28.00",
    expiry_str="2026-06-20",
) -> OptionContract:
    expiry = date.fromisoformat(expiry_str)
    return OptionContract(
        symbol="AMD",
        contract_id=contract_id,
        option_type=option_type,
        strike=Decimal(strike),
        expiry=expiry,
        dte=(expiry - date.today()).days,
        bid=Decimal("1.20"),
        ask=Decimal("1.40"),
        delta=-0.28 if option_type == "put" else 0.30,
        iv=0.45,
        open_interest=2000,
        volume=500,
    )


def _set_csp_open(w, contract_id="AMD240119P00280000"):
    from strategies.wheel.wheel_strategy import WheelState
    contract = _make_option_contract(contract_id=contract_id)
    csp = CSPPosition(
        symbol="AMD",
        contract=contract,
        premium_received=Decimal("1.50"),
        opened_at=datetime(2026, 5, 30, 14, 0, 0, tzinfo=timezone.utc),
        contracts=1,
        underlying_price_at_entry=Decimal("30.00"),
    )
    pos = w._positions["AMD"]
    pos.state = WheelState.CSP_OPEN
    pos.csp_position = csp
    pos.total_premium_collected = Decimal("150.00")
    return pos


def _set_cc_open(w, contract_id="AMD240119C00030000"):
    from strategies.wheel.wheel_strategy import WheelState
    contract = _make_option_contract(
        contract_id=contract_id, option_type="call", strike="30.00"
    )
    cc = CCPosition(
        symbol="AMD",
        contract=contract,
        premium_received=Decimal("0.80"),
        opened_at=datetime(2026, 5, 30, 15, 0, 0, tzinfo=timezone.utc),
        stock_cost_basis=Decimal("26.80"),
        contracts=1,
    )
    pos = w._positions["AMD"]
    pos.state = WheelState.CC_OPEN
    pos.cc_position = cc
    pos.stock_quantity = 100
    pos.stock_cost_basis = Decimal("26.80")
    pos.total_premium_collected = Decimal("230.00")
    return pos


# ---------------------------------------------------------------------------
# get_state serialization
# ---------------------------------------------------------------------------

def test_get_state_includes_csp_position_data():
    """get_state must serialize full CSP contract + position data."""
    w = _make_wheel()
    _set_csp_open(w)

    state = w.get_state()
    amd = state["AMD"]

    assert amd["csp_position"] is not None
    csp = amd["csp_position"]
    assert csp["contract_id"] == "AMD240119P00280000"
    assert csp["strike"] == "28.00"
    assert csp["option_type"] == "put"
    assert csp["premium_received"] == "1.50"
    assert csp["underlying_price_at_entry"] == "30.00"
    assert "expiry" in csp
    assert "opened_at" in csp


def test_get_state_null_csp_position_when_scanning():
    """SCANNING state must produce csp_position: null in serialized state."""
    w = _make_wheel()
    state = w.get_state()
    assert state["AMD"]["csp_position"] is None


def test_get_state_includes_cc_position_data():
    """get_state must serialize full CC contract + position data."""
    w = _make_wheel()
    _set_cc_open(w)

    state = w.get_state()
    amd = state["AMD"]

    assert amd["cc_position"] is not None
    cc = amd["cc_position"]
    assert cc["contract_id"] == "AMD240119C00030000"
    assert cc["option_type"] == "call"
    assert cc["stock_cost_basis"] == "26.80"
    assert cc["premium_received"] == "0.80"


# ---------------------------------------------------------------------------
# load_state reconstruction
# ---------------------------------------------------------------------------

def test_load_state_reconstructs_csp_position():
    """load_state must reconstruct a valid CSPPosition from serialized data."""
    w = _make_wheel()
    _set_csp_open(w)
    state = w.get_state()

    w2 = _make_wheel()
    w2.load_state(state)

    pos = w2._positions["AMD"]
    assert pos.csp_position is not None
    assert isinstance(pos.csp_position, CSPPosition)
    assert pos.csp_position.premium_received == Decimal("1.50")
    assert pos.csp_position.contract.contract_id == "AMD240119P00280000"
    assert pos.csp_position.underlying_price_at_entry == Decimal("30.00")


def test_load_state_recalculates_dte_from_expiry():
    """DTE must be recalculated from expiry date on load, not taken from stale stored value."""
    w = _make_wheel()
    _set_csp_open(w)
    state = w.get_state()

    w2 = _make_wheel()
    w2.load_state(state)

    pos = w2._positions["AMD"]
    assert pos.csp_position is not None
    expiry = pos.csp_position.contract.expiry
    expected_dte = max(0, (expiry - date.today()).days)
    assert pos.csp_position.contract.dte == expected_dte


def test_load_state_reconstructs_cc_position():
    """load_state must reconstruct a valid CCPosition from serialized data."""
    w = _make_wheel()
    _set_cc_open(w)
    state = w.get_state()

    w2 = _make_wheel()
    w2.load_state(state)

    pos = w2._positions["AMD"]
    assert pos.cc_position is not None
    assert isinstance(pos.cc_position, CCPosition)
    assert pos.cc_position.premium_received == Decimal("0.80")
    assert pos.cc_position.stock_cost_basis == Decimal("26.80")
    assert pos.cc_position.contract.contract_id == "AMD240119C00030000"


def test_round_trip_preserves_full_state():
    """get_state → load_state round-trip: all position fields match."""
    w = _make_wheel()
    pos = _set_csp_open(w)
    state = w.get_state()

    w2 = _make_wheel()
    w2.load_state(state)
    pos2 = w2._positions["AMD"]

    from strategies.wheel.wheel_strategy import WheelState
    assert pos2.state == WheelState.CSP_OPEN
    assert pos2.total_premium_collected == Decimal("150.00")
    assert pos2.csp_position.premium_received == pos.csp_position.premium_received
    assert pos2.csp_position.contract.strike == pos.csp_position.contract.strike
