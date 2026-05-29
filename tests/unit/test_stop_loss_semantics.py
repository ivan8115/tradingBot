"""
Tests for two-tier CSP stop loss semantics.
"""
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest


def _make_position(symbol="AMD", strike=50.0, premium=1.50, contracts=1):
    from strategies.wheel.csp_leg import CSPPosition, OptionContract
    contract = MagicMock(spec=OptionContract)
    contract.strike = Decimal(str(strike))
    contract.dte = 30
    pos = CSPPosition(
        symbol=symbol,
        contract=contract,
        premium_received=Decimal(str(premium)),
        opened_at=datetime.now(timezone.utc),
        contracts=contracts,
        underlying_price_at_entry=Decimal(str(strike * 1.05)),
    )
    return pos


def _leg(pain_threshold_default=0.85, symbol_overrides=None):
    from strategies.wheel.csp_leg import CashSecuredPutLeg
    from core.config import CSPConfig
    cfg = MagicMock(spec=CSPConfig)
    cfg.profit_target_pct = 0.50
    cfg.stop_loss_multiplier = 2.0
    cfg.roll_when_dte = 7
    cfg.pain_threshold_default = pain_threshold_default
    leg = CashSecuredPutLeg(cfg)
    leg._symbol_pain_thresholds = symbol_overrides or {}
    return leg


def test_soft_exit_triggers_when_mark_2_5x_and_below_strike():
    """Mark at 2.5× credit AND underlying below strike → close."""
    leg = _leg()
    pos = _make_position(strike=50.0, premium=1.50)
    closed, reason = leg.should_close_early(
        pos,
        current_contract_price=Decimal("3.75"),  # 1.50 × 2.5
        current_underlying=Decimal("48.00"),       # below strike of 50
        dte=30,
    )
    assert closed is True
    assert reason != ""


def test_soft_exit_does_not_trigger_on_iv_expansion_only():
    """Mark at 2.5× credit but underlying ABOVE strike (IV spike) → don't close."""
    leg = _leg()
    pos = _make_position(strike=50.0, premium=1.50)
    closed, _ = leg.should_close_early(
        pos,
        current_contract_price=Decimal("3.75"),
        current_underlying=Decimal("52.00"),  # above strike — not directional
        dte=30,
    )
    assert closed is False


def test_pain_threshold_triggers_when_underlying_drops_below_85pct_of_strike():
    """Underlying below strike × 0.85 → close regardless of option math."""
    leg = _leg(pain_threshold_default=0.85)
    pos = _make_position(strike=50.0, premium=1.50)
    # 50 × 0.85 = 42.50; underlying at 42.00 < 42.50
    closed, reason = leg.should_close_early(
        pos,
        current_contract_price=Decimal("1.00"),
        current_underlying=Decimal("42.00"),
        dte=30,
    )
    assert closed is True
    assert reason != ""


def test_pain_threshold_does_not_trigger_above_threshold():
    """Underlying at 44.00 > 42.50 (strike × 0.85) → don't trigger."""
    leg = _leg(pain_threshold_default=0.85)
    pos = _make_position(strike=50.0, premium=1.50)
    closed, _ = leg.should_close_early(
        pos,
        current_contract_price=Decimal("1.00"),
        current_underlying=Decimal("44.00"),
        dte=30,
    )
    assert closed is False


def test_profit_target_still_works():
    """50% profit target still fires first."""
    leg = _leg()
    pos = _make_position(strike=50.0, premium=1.50)
    # mark = 0.75 → profit_pct = (1.50 - 0.75) / 1.50 = 50% → close
    closed, reason = leg.should_close_early(
        pos,
        current_contract_price=Decimal("0.75"),
        current_underlying=Decimal("52.00"),
        dte=30,
    )
    assert closed is True
    assert "profit" in reason.lower()


def test_per_symbol_pain_threshold_override():
    """Symbol-specific override takes precedence over default."""
    leg = _leg(pain_threshold_default=0.85, symbol_overrides={"AMD": 0.90})
    pos = _make_position(symbol="AMD", strike=50.0)
    # 50 × 0.90 = 45.00; underlying at 44.00 < 45.00 → close (stricter than 0.85)
    closed, _ = leg.should_close_early(
        pos,
        current_contract_price=Decimal("1.00"),
        current_underlying=Decimal("44.00"),
        dte=30,
    )
    assert closed is True


def test_dte_roll_still_works():
    """DTE at or below roll_when_dte (7) still triggers close."""
    leg = _leg()
    pos = _make_position(strike=50.0, premium=1.50)
    closed, reason = leg.should_close_early(
        pos,
        current_contract_price=Decimal("1.00"),
        current_underlying=Decimal("55.00"),  # well above strike
        dte=7,
    )
    assert closed is True
    assert "dte" in reason.lower()
