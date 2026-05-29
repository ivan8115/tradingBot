"""IV history must be built from real options chain ATM IV, not ATR proxy."""
from decimal import Decimal
from unittest.mock import MagicMock, patch
import pytest


def _make_contract(strike=50.0, iv=0.45, option_type="put", dte=30):
    from strategies.wheel.csp_leg import OptionContract
    c = MagicMock(spec=OptionContract)
    c.option_type = option_type
    c.strike = Decimal(str(strike))
    c.dte = dte
    c.iv = iv
    c.delta = -0.28
    c.bid = Decimal("1.20")
    c.ask = Decimal("1.40")
    c.volume = 500
    c.open_interest = 2000
    c.contract_id = f"TEST{int(strike)}P"
    return c


def _make_wheel():
    from strategies.wheel.wheel_strategy import WheelStrategy, WheelPosition
    from unittest.mock import patch
    with patch("strategies.wheel.wheel_strategy.settings") as ms:
        ms.indicators.bar_window_size = 200
        ms.strategies.wheel.csp.min_iv_rank = 40
        ms.strategies.wheel.csp.pain_threshold_default = 0.85
        ms.strategies.wheel.csp.min_dte = 21
        ms.strategies.wheel.csp.max_dte = 45
        w = WheelStrategy.__new__(WheelStrategy)
    w.symbols = ["TEST"]
    w._positions = {"TEST": WheelPosition(symbol="TEST")}
    w._advisor = None
    w._cfg = MagicMock()
    w._cfg.csp.min_dte = 21
    w._cfg.csp.max_dte = 45
    return w


def test_update_options_chain_extracts_atm_iv():
    """update_options_chain with underlying_price must store ATM put IV in pos.iv_history."""
    w = _make_wheel()
    chain = [
        _make_contract(strike=48.0, iv=0.40, dte=30),
        _make_contract(strike=50.0, iv=0.45, dte=30),  # closest to underlying 50.5
        _make_contract(strike=52.0, iv=0.42, dte=30),
    ]
    w.update_options_chain("TEST", chain, underlying_price=50.5)
    pos = w._positions["TEST"]
    assert len(pos.iv_history) == 1
    assert 0.40 <= pos.iv_history[0] <= 0.50, f"Expected ~0.45, got {pos.iv_history[0]}"


def test_update_options_chain_without_underlying_price_skips_iv():
    """Without underlying_price, iv_history must not be updated."""
    w = _make_wheel()
    chain = [_make_contract(strike=50.0, iv=0.45, dte=30)]
    w.update_options_chain("TEST", chain)  # no underlying_price
    pos = w._positions["TEST"]
    assert len(pos.iv_history) == 0


def test_iv_history_capped_at_252():
    """iv_history must not grow beyond 252 entries."""
    w = _make_wheel()
    pos = w._positions["TEST"]
    pos.iv_history = [0.3] * 252  # already full
    chain = [_make_contract(strike=50.0, iv=0.50, dte=30)]
    w.update_options_chain("TEST", chain, underlying_price=50.0)
    assert len(pos.iv_history) == 252
    assert pos.iv_history[-1] == 0.50  # new entry added, oldest dropped
