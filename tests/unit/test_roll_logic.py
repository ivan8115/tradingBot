"""Tests for configurable roll_when_dte in CSP leg."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from strategies.wheel.csp_leg import CashSecuredPutLeg, CSPPosition, OptionContract
from core.config import CSPConfig


def _make_csp_config(roll_when_dte: int = 7) -> CSPConfig:
    return CSPConfig(
        target_delta=-0.28,
        min_dte=21,
        max_dte=45,
        profit_target_pct=0.50,
        stop_loss_multiplier=2.0,
        min_premium=1.00,
        min_iv_rank=50,
        roll_when_dte=roll_when_dte,
    )


def _make_position(dte: int, premium: float = 2.0) -> CSPPosition:
    contract = OptionContract(
        symbol="AMD",
        contract_id="AMD240119P00120000",
        option_type="put",
        strike=Decimal("120"),
        expiry=date.today(),
        dte=dte,
        bid=Decimal("2.00"),
        ask=Decimal("2.20"),
        delta=-0.28,
        iv=0.45,
    )
    return CSPPosition(
        symbol="AMD",
        contract=contract,
        premium_received=Decimal(str(premium)),
        opened_at=datetime.now(timezone.utc),
    )


def test_csp_closes_when_dte_at_threshold():
    """should_close_early returns True when DTE equals roll_when_dte from config."""
    leg = CashSecuredPutLeg(_make_csp_config(roll_when_dte=7))
    pos = _make_position(dte=7)
    should_close, reason = leg.should_close_early(pos, Decimal("2.00"))
    assert should_close is True
    assert "7" in reason


def test_csp_uses_config_roll_when_dte_not_hardcoded():
    """roll_when_dte=10 closes at DTE=10, not just DTE=7."""
    leg = CashSecuredPutLeg(_make_csp_config(roll_when_dte=10))
    pos = _make_position(dte=10)
    should_close, reason = leg.should_close_early(pos, Decimal("2.00"))
    assert should_close is True
    assert "10" in reason


def test_csp_does_not_close_when_dte_above_threshold():
    """should_close_early returns False when DTE > roll_when_dte and no P&L trigger."""
    leg = CashSecuredPutLeg(_make_csp_config(roll_when_dte=7))
    pos = _make_position(dte=15)
    should_close, _ = leg.should_close_early(pos, Decimal("2.00"))
    assert should_close is False
