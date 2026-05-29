"""Premium collected must be total dollars, not per-share price."""
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest


def _make_fill(leg: str, side: str, fill_price: float, symbol: str = "AMD"):
    from core.events import FillEvent
    f = MagicMock(spec=FillEvent)
    f.strategy_id = "wheel"
    f.symbol = symbol
    f.side = side
    f.fill_price = Decimal(str(fill_price))
    f.filled_qty = 100
    f.filled_at = datetime.now(timezone.utc)
    f.metadata = {"leg": leg}
    return f


def _make_wheel_with_position():
    """Returns a WheelStrategy instance with a single AMD WheelPosition pre-wired."""
    from strategies.wheel.wheel_strategy import WheelStrategy, WheelPosition, WheelState
    from unittest.mock import patch
    with patch("strategies.wheel.wheel_strategy.settings") as ms:
        ms.indicators.bar_window_size = 200
        ms.strategies.wheel.csp.min_iv_rank = 40
        ms.strategies.wheel.csp.pain_threshold_default = 0.85
        ms.strategies.wheel.cc.profit_target_pct = 0.50
        ms.strategies.wheel.cc.roll_when_dte = 7
        w = WheelStrategy.__new__(WheelStrategy)
    w.symbols = ["AMD"]
    w.strategy_id = "wheel"
    pos = WheelPosition(symbol="AMD")
    pos.state = WheelState.SCANNING
    w._positions = {"AMD": pos}
    w._advisor = None
    # Attach a mock csp_leg with cost_basis_after_assignment
    from unittest.mock import MagicMock
    w._csp_leg = MagicMock()
    w._csp_leg.cost_basis_after_assignment.return_value = Decimal("28.00")
    return w, pos


def test_csp_premium_collected_is_total_dollars():
    """Opening a 1-contract CSP at $1.50/share must add $150 (not $1.50) to total_premium."""
    w, pos = _make_wheel_with_position()
    fill = _make_fill("csp_open", "sell", 1.50)
    fill.metadata = {"leg": "csp_open", "underlying_price": 52.0}

    w.on_fill(fill)

    assert pos.total_premium_collected == Decimal("150.00"), (
        f"Expected $150.00, got {pos.total_premium_collected}"
    )


def test_cc_premium_collected_is_total_dollars():
    """Opening a 1-contract CC at $0.80/share must add $80 (not $0.80) to total_premium."""
    from strategies.wheel.wheel_strategy import WheelState
    w, pos = _make_wheel_with_position()
    pos.total_premium_collected = Decimal("150.00")
    pos.state = WheelState.ASSIGNED

    fill = _make_fill("cc_open", "sell", 0.80)
    fill.metadata = {"leg": "cc_open"}
    w.on_fill(fill)

    assert pos.total_premium_collected == Decimal("230.00"), (
        f"Expected $150 + $80 = $230.00, got {pos.total_premium_collected}"
    )
