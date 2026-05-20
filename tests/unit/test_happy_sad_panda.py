"""Tests for Happy/Sad Panda (EMA crossback) pattern detection."""

from datetime import datetime, timezone
from decimal import Decimal

from analysis.indicators import IndicatorSnapshot, is_happy_panda, is_sad_panda
from core.events import BarEvent


def _snap(ema_trend_up: bool | None) -> IndicatorSnapshot:
    snap = IndicatorSnapshot()
    snap.ema_trend_up = ema_trend_up
    if ema_trend_up is True:
        snap.ema_short = 10.0
        snap.ema_long = 9.0
    else:
        snap.ema_short = 9.0
        snap.ema_long = 10.0
    return snap


class TestHappyPanda:
    def test_happy_panda_detects_bullish_crossback(self):
        prev = _snap(ema_trend_up=False)
        curr = _snap(ema_trend_up=True)
        assert is_happy_panda(curr, prev) is True

    def test_happy_panda_false_when_already_bullish(self):
        prev = _snap(ema_trend_up=True)
        curr = _snap(ema_trend_up=True)
        assert is_happy_panda(curr, prev) is False

    def test_happy_panda_false_when_no_prev_snap(self):
        curr = _snap(ema_trend_up=True)
        assert is_happy_panda(curr, None) is False

    def test_sad_panda_detects_bearish_crossback(self):
        prev = _snap(ema_trend_up=True)
        curr = _snap(ema_trend_up=False)
        assert is_sad_panda(curr, prev) is True

    def test_sad_panda_false_when_already_bearish(self):
        prev = _snap(ema_trend_up=False)
        curr = _snap(ema_trend_up=False)
        assert is_sad_panda(curr, prev) is False

    def test_sad_panda_false_when_no_prev_snap(self):
        curr = _snap(ema_trend_up=False)
        assert is_sad_panda(curr, None) is False


class TestMomentumCrossbackIntegration:
    def test_strategy_has_ema_bearish_bars_tracker(self):
        from strategies.momentum import MomentumStrategy
        strat = MomentumStrategy(["SPY"])
        assert hasattr(strat, "_ema_bearish_bars")
        assert "SPY" in strat._ema_bearish_bars
        assert strat._ema_bearish_bars["SPY"] == 0

    def test_bearish_bars_increments_when_ema_bearish(self):
        from strategies.momentum import MomentumStrategy
        strat = MomentumStrategy(["AAPL"])
        # Feed a steadily declining price series — 9 EMA will stay below 20 EMA
        for i in range(50):
            bar = BarEvent(
                symbol="AAPL",
                timestamp=datetime.now(timezone.utc),
                open=Decimal(str(200 - i * 0.4)),
                high=Decimal(str(200 - i * 0.4 + 0.5)),
                low=Decimal(str(200 - i * 0.4 - 0.5)),
                close=Decimal(str(200 - i * 0.4)),
                volume=1_000_000,
            )
            strat.on_bar(bar)
        # After a declining series, _ema_bearish_bars should be > 0
        assert strat._ema_bearish_bars["AAPL"] > 0

    def test_bearish_bars_resets_when_ema_turns_bullish(self):
        from strategies.momentum import MomentumStrategy
        strat = MomentumStrategy(["AAPL"])
        # Feed a rising series after a few declining bars — counter should reset to 0
        for i in range(60):
            close = 200 + i * 0.5  # steadily rising
            bar = BarEvent(
                symbol="AAPL",
                timestamp=datetime.now(timezone.utc),
                open=Decimal(str(close - 0.3)),
                high=Decimal(str(close + 0.5)),
                low=Decimal(str(close - 0.5)),
                close=Decimal(str(close)),
                volume=1_000_000,
            )
            strat.on_bar(bar)
        # After a long rising series, counter should be 0
        assert strat._ema_bearish_bars["AAPL"] == 0
