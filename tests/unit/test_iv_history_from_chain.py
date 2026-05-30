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


# ---------------------------------------------------------------------------
# seed_iv_history — populate from historical daily bars
# ---------------------------------------------------------------------------

def _make_daily_bars_df(n: int = 252) -> "pd.DataFrame":
    """Synthetic daily bars with a gentle uptrend (ATR ≈ 1.0)."""
    import pandas as pd
    import numpy as np
    rng = np.random.default_rng(42)
    closes = 50.0 + np.cumsum(rng.normal(0, 0.5, n))
    closes = np.maximum(closes, 10.0)
    highs = closes + rng.uniform(0.3, 1.5, n)
    lows = closes - rng.uniform(0.3, 1.5, n)
    return pd.DataFrame({
        "open": closes - 0.2,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [500_000] * n,
    })


def test_seed_iv_history_populates_from_bars():
    """seed_iv_history must add at least 200 IV observations from 252 daily bars."""
    w = _make_wheel()
    df = _make_daily_bars_df(252)
    w.seed_iv_history("TEST", df)
    pos = w._positions["TEST"]
    assert len(pos.iv_history) >= 200, (
        f"Expected >=200 observations after seeding 252 bars, got {len(pos.iv_history)}"
    )


def test_seed_iv_history_values_are_positive():
    """All IV estimates from ATR proxy must be > 0."""
    w = _make_wheel()
    df = _make_daily_bars_df(252)
    w.seed_iv_history("TEST", df)
    pos = w._positions["TEST"]
    assert all(v > 0 for v in pos.iv_history), "IV estimates must all be positive"


def test_seed_iv_history_capped_at_252():
    """iv_history must be capped at 252 entries even with more bars."""
    w = _make_wheel()
    df = _make_daily_bars_df(500)
    w.seed_iv_history("TEST", df)
    pos = w._positions["TEST"]
    assert len(pos.iv_history) <= 252


def test_seed_iv_history_skips_if_already_seeded():
    """If iv_history already has >=30 entries, seed must not overwrite."""
    w = _make_wheel()
    pos = w._positions["TEST"]
    existing = [0.3] * 30
    pos.iv_history = existing[:]
    df = _make_daily_bars_df(252)
    w.seed_iv_history("TEST", df)
    assert pos.iv_history == existing, "Must not overwrite when already seeded"


def test_seed_iv_history_noop_for_unknown_symbol():
    """seed_iv_history on a symbol not in _positions must not raise."""
    w = _make_wheel()
    df = _make_daily_bars_df(10)
    w.seed_iv_history("UNKNOWN", df)  # must not raise


def test_seed_iv_history_noop_on_empty_df():
    """Empty DataFrame must not raise."""
    import pandas as pd
    w = _make_wheel()
    w.seed_iv_history("TEST", pd.DataFrame())
    pos = w._positions["TEST"]
    assert pos.iv_history == []
