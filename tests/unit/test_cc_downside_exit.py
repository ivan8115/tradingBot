"""Covered call leg must signal exit when stock falls below cost basis stop."""
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest


def _make_cc_position(symbol="AMD", strike=55.0, cost_basis=50.0, premium=0.80):
    from strategies.wheel.covered_call_leg import CCPosition
    from strategies.wheel.csp_leg import OptionContract
    contract = MagicMock(spec=OptionContract)
    contract.strike = Decimal(str(strike))
    contract.dte = 20
    pos = CCPosition(
        symbol=symbol,
        contract=contract,
        premium_received=Decimal(str(premium)),
        opened_at=datetime.now(timezone.utc),
        stock_cost_basis=Decimal(str(cost_basis)),
    )
    return pos


def _make_cc_leg(stop_pct=0.90):
    from strategies.wheel.covered_call_leg import CoveredCallLeg
    from core.config import CCConfig
    cfg = MagicMock(spec=CCConfig)
    cfg.profit_target_pct = 0.50
    cfg.roll_when_dte = 7
    cfg.stock_stop_loss_pct = stop_pct
    return CoveredCallLeg(cfg)


def test_cc_closes_when_stock_falls_below_stop():
    """Stock at $44 < cost_basis($50) × 0.90($45) → should close.

    Use contract_price=0.60 (25% profit) so profit target does not fire first.
    """
    leg = _make_cc_leg(stop_pct=0.90)
    pos = _make_cc_position(cost_basis=50.0)
    should_close, reason = leg.should_close_early(
        pos,
        current_contract_price=Decimal("0.60"),
        underlying_price=Decimal("44.00"),
    )
    assert should_close is True
    assert "stock_stop" in reason.lower() or "cost_basis" in reason.lower()


def test_cc_does_not_close_above_stop():
    """Stock at $48 > cost_basis($50) × 0.90($45) → no stop exit.

    Use contract_price=0.60 (25% profit) so neither profit target nor stop fires.
    """
    leg = _make_cc_leg(stop_pct=0.90)
    pos = _make_cc_position(cost_basis=50.0)
    should_close, _ = leg.should_close_early(
        pos,
        current_contract_price=Decimal("0.60"),
        underlying_price=Decimal("48.00"),
    )
    assert should_close is False


def test_cc_profit_target_fires_before_stop():
    """50% profit target should trigger even when stock is above stop."""
    leg = _make_cc_leg(stop_pct=0.90)
    pos = _make_cc_position(cost_basis=50.0, premium=0.80)
    should_close, reason = leg.should_close_early(
        pos,
        current_contract_price=Decimal("0.40"),  # 50% of $0.80 = profit target
        underlying_price=Decimal("52.00"),
    )
    assert should_close is True
    assert "profit" in reason.lower()


def test_cc_no_stop_without_underlying_price():
    """If underlying_price not provided, stock stop must not fire.

    Use contract_price=0.60 (25% profit) so profit target does not fire either.
    """
    leg = _make_cc_leg(stop_pct=0.90)
    pos = _make_cc_position(cost_basis=50.0)
    should_close, _ = leg.should_close_early(
        pos,
        current_contract_price=Decimal("0.60"),
        underlying_price=None,
    )
    assert should_close is False
