"""
Portfolio.total_value() and drawdown() must include unrealized P&L
from open equity positions. Risk checks (drawdown, daily loss) must
see the true mark-to-market value, not just cash.
"""
from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from core.events import FillEvent
from portfolio.portfolio import Portfolio


def _buy_fill(symbol: str, qty: int, price: float) -> FillEvent:
    f = MagicMock(spec=FillEvent)
    f.symbol = symbol
    f.side = "buy"
    f.filled_qty = qty
    f.fill_price = Decimal(str(price))
    f.total_cost = Decimal(str(price * qty))
    f.commission = Decimal("0")
    f.is_options = False
    f.option_contract_id = None
    f.strategy_id = "swing"
    f.order_id = "test-001"
    f.filled_at = datetime.now(timezone.utc)
    f.metadata = {}
    return f


def test_total_value_uses_cached_prices_automatically():
    """After update_price, total_value() without explicit prices uses cached prices."""
    p = Portfolio(cash=Decimal("10000"))
    fill = _buy_fill("AAPL", 10, 100.0)  # buy 10 AAPL @ $100
    p.apply_fill(fill)  # cash = $9,000

    # Price drops to $90 — unrealized loss = $100
    p.update_price("AAPL", Decimal("90.0"))

    total = p.total_value()
    assert total == Decimal("9900"), (
        f"Expected $9,900 (9,000 cash + 10×$90 equity), got ${total}. "
        "total_value() must auto-use cached prices."
    )


def test_total_value_explicit_prices_still_work_corrected():
    """Passing current_prices explicitly must still work (backward compat)."""
    p = Portfolio(cash=Decimal("10000"))
    fill = _buy_fill("NVDA", 5, 100.0)
    p.apply_fill(fill)
    # cash = 10000 - 500 = 9500; at $110: equity = 5 × $110 = 550; total = 10050
    total = p.total_value({"NVDA": Decimal("110.0")})
    assert total == Decimal("10050")


def test_drawdown_reflects_unrealized_loss():
    """After a price drop, drawdown() must include the unrealized loss."""
    p = Portfolio(cash=Decimal("10000"))
    fill = _buy_fill("TSLA", 100, 50.0)  # buy 100 TSLA at $50 = $5,000
    p.apply_fill(fill)  # cash = $5,000; peak = $10,000

    # Price drops to $40 — unrealized loss = $1,000 → total = $9,000
    p.update_price("TSLA", Decimal("40.0"))

    dd = p.drawdown()
    assert dd == Decimal("0.1"), (
        f"Expected 10% drawdown, got {float(dd)*100:.1f}%. "
        "drawdown() must include unrealized losses."
    )


def test_risk_manager_drawdown_halt_fires_on_unrealized_loss():
    """RiskManager._check_drawdown must fire when unrealized losses breach the limit."""
    from risk.risk_manager import RiskManager
    from data.market_regime import Regime
    from core.events import SignalEvent

    p = Portfolio(cash=Decimal("10000"))
    fill = _buy_fill("AMD", 200, 50.0)  # buy 200 AMD at $50 = $10,000
    p.apply_fill(fill)  # cash = $0; peak = $10,000

    # Price drops 20% → total value = $8,000 → 20% drawdown (above 15% limit)
    p.update_price("AMD", Decimal("40.0"))

    rm = RiskManager(max_drawdown_pct=0.15)
    signal = MagicMock(spec=SignalEvent)
    signal.strategy_id = "swing"
    signal.signal_type = "ENTRY_LONG"
    signal.symbol = "GOOG"
    signal.metadata = {}
    result = rm.validate_signal(signal, p, current_price=Decimal("100.0"))

    assert not result.approved, (
        "RiskManager should block new entries when unrealized drawdown exceeds limit."
    )
    assert any("drawdown" in c.reason.lower() for c in result.checks if not c.passed)


def test_update_price_method_exists_and_updates_cache():
    """Portfolio.update_price must update the internal price cache."""
    p = Portfolio(cash=Decimal("5000"))
    p.update_price("SPY", Decimal("450.0"))
    assert p._current_prices.get("SPY") == Decimal("450.0")


def test_current_prices_initialized_as_empty_dict():
    """_current_prices must be a proper attribute, not dynamically created."""
    p = Portfolio(cash=Decimal("1000"))
    assert hasattr(p, "_current_prices")
    assert isinstance(p._current_prices, dict)
    assert len(p._current_prices) == 0
