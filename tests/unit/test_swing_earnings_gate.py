"""Swing strategy must block entry entirely within 7 days of earnings."""
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest


def _make_earnings_calendar(days_to_earnings: int):
    ec = MagicMock()
    # is_near_earnings(sym, min_days=N) returns True if earnings are within N days
    ec.is_near_earnings = lambda sym, min_days: days_to_earnings <= min_days
    return ec


def _make_swing(earnings_calendar=None):
    from strategies.swing.swing_strategy import SwingStrategy
    from core.config import SwingStrategyConfig
    cfg = SwingStrategyConfig(
        enabled=True,
        atr_stop_mult=2.0,
        atr_target_mult=4.0,
        max_hold_bars=30,
        min_bars_for_entry=50,
    )
    with patch("strategies.swing.swing_strategy.settings") as ms:
        ms.indicators.bar_window_size = 200
        ms.strategies.swing = cfg
        s = SwingStrategy(symbols=["MSFT"], config=cfg, earnings_calendar=earnings_calendar)
    return s


def _minimal_snap():
    snap = MagicMock()
    snap.ema_trend_up = True
    snap.macd_hist = 0.05
    snap.rsi = 55.0
    snap.atr = 5.0
    snap.volume_ratio = 1.8
    return snap


def _make_bar(sym="MSFT"):
    from core.events import BarEvent
    b = MagicMock(spec=BarEvent)
    b.symbol = sym
    b.close = Decimal("400.00")
    b.timestamp = datetime.now(timezone.utc)
    return b


def _run_entry(s, sym="MSFT"):
    from analysis.stage_analysis import Stage
    snap = _minimal_snap()
    prev = MagicMock()
    prev.ema_trend_up = True
    bar = _make_bar(sym)
    with patch.object(s, "_update_indicators", return_value=snap), \
         patch.object(s, "_get_prev_snapshot", return_value=prev), \
         patch.object(s, "_bars_available", return_value=True), \
         patch("strategies.swing.swing_strategy.classify_stage") as mock_stage, \
         patch("strategies.swing.swing_strategy.is_sad_panda", return_value=False):
        mock_stage.return_value = Stage.STAGE_2
        return s.on_bar(bar)


def test_entry_blocked_within_7_days_of_earnings():
    """No entry signal within 7 days of earnings (hard block)."""
    ec = _make_earnings_calendar(days_to_earnings=5)
    s = _make_swing(earnings_calendar=ec)
    signals = _run_entry(s)
    assert len(signals) == 0, "Must block entry within 7 days of earnings"


def test_entry_allowed_but_halved_between_7_and_30_days():
    """Between 7–30 days, entry allowed but strength halved."""
    ec = _make_earnings_calendar(days_to_earnings=15)
    s = _make_swing(earnings_calendar=ec)
    signals = _run_entry(s)
    assert len(signals) == 1, "Should allow entry at 15 days"
    assert signals[0].strength < 0.75, "Strength must be reduced near earnings"


def test_entry_full_strength_beyond_30_days():
    """Beyond 30 days, no earnings restriction."""
    ec = _make_earnings_calendar(days_to_earnings=45)
    s = _make_swing(earnings_calendar=ec)
    signals = _run_entry(s)
    assert len(signals) == 1
    # strength should NOT be halved (> 30 days away)
    assert signals[0].strength >= 0.3, "No earnings gate at 45 days"
