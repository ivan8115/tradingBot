"""Swing strategy must close when price drops below the recorded stop level."""
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest


def _make_bar(symbol: str, close: float):
    from core.events import BarEvent
    b = MagicMock(spec=BarEvent)
    b.symbol = symbol
    b.close = Decimal(str(close))
    b.open = b.close
    b.high = b.close
    b.low = b.close
    b.volume = 100_000
    b.timestamp = datetime.now(timezone.utc)
    return b


def _make_swing():
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
        s = SwingStrategy(symbols=["NVDA"], config=cfg)
    return s


def test_stop_loss_triggers_when_price_drops_below_level():
    s = _make_swing()
    sym = "NVDA"
    s._in_position[sym] = True
    s._bars_held[sym] = 5
    s._entry_stop_levels[sym] = 850.0

    bar = _make_bar(sym, close=840.0)
    snap = MagicMock()
    snap.ema_trend_up = True
    snap.atr = 10.0
    snap.volume_ratio = 1.0
    prev = MagicMock()
    prev.ema_trend_up = True

    with patch.object(s, "_update_indicators", return_value=snap), \
         patch.object(s, "_get_prev_snapshot", return_value=prev), \
         patch.object(s, "_bars_available", return_value=True), \
         patch("strategies.swing.swing_strategy.classify_stage") as mock_stage, \
         patch("strategies.swing.swing_strategy.is_sad_panda", return_value=False):
        from analysis.stage_analysis import Stage
        mock_stage.return_value = Stage.STAGE_2
        signals = s.on_bar(bar)

    assert len(signals) == 1
    assert signals[0].signal_type == "EXIT_LONG"
    assert "stop_loss" in signals[0].metadata["reason"].lower()


def test_stop_loss_does_not_trigger_above_stop_level():
    s = _make_swing()
    sym = "NVDA"
    s._in_position[sym] = True
    s._bars_held[sym] = 5
    s._entry_stop_levels[sym] = 850.0

    bar = _make_bar(sym, close=870.0)
    snap = MagicMock()
    snap.ema_trend_up = True
    snap.atr = 10.0
    snap.volume_ratio = 1.0
    prev = MagicMock()
    prev.ema_trend_up = True

    with patch.object(s, "_update_indicators", return_value=snap), \
         patch.object(s, "_get_prev_snapshot", return_value=prev), \
         patch.object(s, "_bars_available", return_value=True), \
         patch("strategies.swing.swing_strategy.classify_stage") as mock_stage, \
         patch("strategies.swing.swing_strategy.is_sad_panda", return_value=False):
        from analysis.stage_analysis import Stage
        mock_stage.return_value = Stage.STAGE_2
        signals = s.on_bar(bar)

    assert len(signals) == 0


def test_on_fill_buy_records_stop_level():
    from core.events import FillEvent
    s = _make_swing()
    sym = "NVDA"

    fill = MagicMock(spec=FillEvent)
    fill.strategy_id = "swing"
    fill.symbol = sym
    fill.side = "buy"
    fill.fill_price = Decimal("900.00")
    fill.metadata = {"stop_loss": 864.0}

    s.on_fill(fill)

    assert s._entry_stop_levels[sym] == 864.0
    assert s._in_position[sym] is True


def test_on_fill_sell_clears_stop_level():
    from core.events import FillEvent
    s = _make_swing()
    sym = "NVDA"
    s._entry_stop_levels[sym] = 864.0
    s._in_position[sym] = True

    fill = MagicMock(spec=FillEvent)
    fill.strategy_id = "swing"
    fill.symbol = sym
    fill.side = "sell"
    fill.fill_price = Decimal("920.00")
    fill.metadata = {}

    s.on_fill(fill)

    assert sym not in s._entry_stop_levels
    assert s._in_position[sym] is False
