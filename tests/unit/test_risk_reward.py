"""Tests for 2:1 minimum R:R gate in RiskManager."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from core.events import SignalEvent
from portfolio.portfolio import Portfolio
from risk.risk_manager import RiskManager


def _make_signal(stop_loss: float | None, take_profit: float | None, close: float = 150.0) -> SignalEvent:
    meta: dict = {"atr": 2.0, "close": close}
    if stop_loss is not None:
        meta["stop_loss"] = stop_loss
    if take_profit is not None:
        meta["take_profit"] = take_profit
    return SignalEvent(
        strategy_id="momentum",
        symbol="AAPL",
        signal_type="ENTRY_LONG",
        strength=0.8,
        timestamp=datetime.now(timezone.utc),
        metadata=meta,
    )


class TestRiskRewardGate:
    def setup_method(self):
        self.rm = RiskManager()
        self.portfolio = Portfolio(cash=Decimal("100000"))

    def test_rejects_when_rr_below_2(self):
        # stop_loss = 148 (risk=2), take_profit = 152 (reward=2) → R:R = 1.0
        signal = _make_signal(stop_loss=148.0, take_profit=152.0)
        result = self.rm.validate_signal(signal, self.portfolio, Decimal("150.00"))
        assert not result.approved
        assert any("R:R" in c.reason for c in result.checks if not c.passed)

    def test_approves_when_rr_exactly_2(self):
        # stop_loss = 148 (risk=2), take_profit = 154 (reward=4) → R:R = 2.0
        signal = _make_signal(stop_loss=148.0, take_profit=154.0)
        result = self.rm.validate_signal(signal, self.portfolio, Decimal("150.00"))
        assert result.approved

    def test_approves_when_rr_above_2(self):
        # stop_loss = 146 (risk=4), take_profit = 160 (reward=10) → R:R = 2.5
        signal = _make_signal(stop_loss=146.0, take_profit=160.0)
        result = self.rm.validate_signal(signal, self.portfolio, Decimal("150.00"))
        assert result.approved

    def test_skips_check_when_no_stop_loss_key(self):
        signal = _make_signal(stop_loss=None, take_profit=154.0)
        result = self.rm.validate_signal(signal, self.portfolio, Decimal("150.00"))
        assert result.approved

    def test_skips_check_when_no_take_profit_key(self):
        signal = _make_signal(stop_loss=148.0, take_profit=None)
        result = self.rm.validate_signal(signal, self.portfolio, Decimal("150.00"))
        assert result.approved

    def test_skips_check_for_options_signal(self):
        signal = SignalEvent(
            strategy_id="wheel",
            symbol="AMD",
            signal_type="SELL_PUT",
            strength=1.0,
            timestamp=datetime.now(timezone.utc),
            metadata={"delta": -0.28, "stop_loss": 100.0, "take_profit": 101.0},
        )
        result = self.rm.validate_signal(signal, self.portfolio)
        assert result.approved
