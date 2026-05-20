"""Tests for MarketRegimeFilter and regime-gated risk checks."""

from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd
import pytest

from core.events import SignalEvent
from data.market_regime import MarketRegimeFilter, Regime
from portfolio.portfolio import Portfolio
from risk.risk_manager import RiskManager


def _make_df(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "open": [c - 0.5 for c in closes],
        "high": [c + 1.0 for c in closes],
        "low": [c - 1.0 for c in closes],
        "close": closes,
        "volume": [1_000_000] * len(closes),
    })


class TestMarketRegimeFilter:
    def setup_method(self):
        self.f = MarketRegimeFilter()

    def test_bullish_when_both_above_rising_emas(self):
        closes = [100 + i * 0.5 for i in range(60)]
        spy_df = _make_df(closes)
        qqq_df = _make_df([c * 2 for c in closes])
        assert self.f.get_regime(spy_df, qqq_df) == Regime.BULLISH

    def test_bearish_when_both_below_declining_emas(self):
        closes = [100 - i * 0.5 for i in range(60)]
        spy_df = _make_df(closes)
        qqq_df = _make_df([c * 2 for c in closes])
        assert self.f.get_regime(spy_df, qqq_df) == Regime.BEARISH

    def test_neutral_when_mixed_signals(self):
        up = [100 + i * 0.5 for i in range(60)]
        down = [200 - i * 0.5 for i in range(60)]
        spy_df = _make_df(up)
        qqq_df = _make_df(down)
        assert self.f.get_regime(spy_df, qqq_df) == Regime.NEUTRAL

    def test_returns_neutral_on_empty_df(self):
        assert self.f.get_regime(pd.DataFrame(), pd.DataFrame()) == Regime.NEUTRAL

    def test_score_df_max_two_for_rising_above_both_emas(self):
        closes = [100 + i * 0.5 for i in range(60)]
        df = _make_df(closes)
        assert self.f._score_df(df) == 2

    def test_score_df_zero_for_declining_below_both_emas(self):
        closes = [100 - i * 0.5 for i in range(60)]
        df = _make_df(closes)
        assert self.f._score_df(df) == 0


class TestRiskManagerRegime:
    def test_regime_blocks_entry_long_in_bearish(self):
        rm = RiskManager()
        rm.set_regime(Regime.BEARISH)
        portfolio = Portfolio(cash=Decimal("100000"))
        signal = SignalEvent(
            strategy_id="momentum",
            symbol="AAPL",
            signal_type="ENTRY_LONG",
            strength=0.8,
            timestamp=datetime.now(timezone.utc),
            metadata={"atr": 2.0, "close": 150.0},
        )
        result = rm.validate_signal(signal, portfolio, current_price=Decimal("150.00"))
        assert not result.approved
        assert any("BEARISH" in c.reason for c in result.checks if not c.passed)

    def test_regime_blocks_sell_put_in_bearish(self):
        rm = RiskManager()
        rm.set_regime(Regime.BEARISH)
        portfolio = Portfolio(cash=Decimal("100000"))
        signal = SignalEvent(
            strategy_id="wheel",
            symbol="AMD",
            signal_type="SELL_PUT",
            strength=1.0,
            timestamp=datetime.now(timezone.utc),
            metadata={"delta": -0.28},
        )
        result = rm.validate_signal(signal, portfolio)
        assert not result.approved

    def test_regime_allows_exit_in_bearish(self):
        rm = RiskManager()
        rm.set_regime(Regime.BEARISH)
        portfolio = Portfolio(cash=Decimal("100000"))
        signal = SignalEvent(
            strategy_id="momentum",
            symbol="AAPL",
            signal_type="EXIT_LONG",
            strength=1.0,
            timestamp=datetime.now(timezone.utc),
            metadata={},
        )
        result = rm.validate_signal(signal, portfolio, current_price=Decimal("150.00"))
        assert result.approved

    def test_regime_allows_entry_in_bullish(self):
        rm = RiskManager()
        rm.set_regime(Regime.BULLISH)
        portfolio = Portfolio(cash=Decimal("100000"))
        signal = SignalEvent(
            strategy_id="momentum",
            symbol="AAPL",
            signal_type="ENTRY_LONG",
            strength=0.8,
            timestamp=datetime.now(timezone.utc),
            metadata={"atr": 2.0, "close": 150.0},
        )
        result = rm.validate_signal(signal, portfolio, current_price=Decimal("150.00"))
        assert result.approved

    def test_regime_blocks_entry_short_in_bearish(self):
        rm = RiskManager()
        rm.set_regime(Regime.BEARISH)
        portfolio = Portfolio(cash=Decimal("100000"))
        signal = SignalEvent(
            strategy_id="momentum",
            symbol="AAPL",
            signal_type="ENTRY_SHORT",
            strength=0.8,
            timestamp=datetime.now(timezone.utc),
            metadata={"atr": 2.0, "close": 150.0},
        )
        result = rm.validate_signal(signal, portfolio, current_price=Decimal("150.00"))
        assert not result.approved
        assert any("BEARISH" in c.reason for c in result.checks if not c.passed)

    def test_regime_blocks_sell_call_in_bearish(self):
        rm = RiskManager()
        rm.set_regime(Regime.BEARISH)
        portfolio = Portfolio(cash=Decimal("100000"))
        signal = SignalEvent(
            strategy_id="wheel",
            symbol="AMD",
            signal_type="SELL_CALL",
            strength=1.0,
            timestamp=datetime.now(timezone.utc),
            metadata={"delta": 0.28},
        )
        result = rm.validate_signal(signal, portfolio)
        assert not result.approved
