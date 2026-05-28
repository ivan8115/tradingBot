"""
Tests for the 80% total collateral cap in RiskManager.

Scenario: $10K account, 5 open CSP positions at $1,600 collateral each = $8,000 deployed (80%).
A new $2,000 trade must be rejected. A $1,500 trade (→ 79%) must be allowed.
"""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest


def _make_signal(collateral: float, delta: float = -0.28):
    from core.events import SignalEvent

    sig = MagicMock(spec=SignalEvent)
    sig.signal_type = "SELL_PUT"
    sig.symbol = "TEST"
    sig.strategy_id = "wheel"
    sig.metadata = {"collateral": collateral, "delta": delta, "leg": "csp_open"}
    return sig


def _make_portfolio(total_value: float, cash: float):
    from portfolio.portfolio import Portfolio

    p = MagicMock(spec=Portfolio)
    p.total_value.return_value = Decimal(str(total_value))
    p.cash = Decimal(str(cash))
    p.drawdown.return_value = 0.0
    p.positions = {}
    return p


def test_rejects_when_collateral_would_exceed_80_pct():
    """5 positions at $1,600 each = $8,000 deployed on $10,000 account.
    Attempting a new $2,000 trade must be rejected (would hit 100%)."""
    from risk.risk_manager import RiskManager

    rm = RiskManager(max_total_deployed_pct=0.80)
    # total=$10k, cash=$2k → currently $8k deployed (80%)
    portfolio = _make_portfolio(total_value=10_000, cash=2_000)
    signal = _make_signal(collateral=2_000)

    result = rm.validate_signal(signal, portfolio)

    assert result.approved is False
    assert result.rejection_reason is not None
    reason_lower = result.rejection_reason.lower()
    assert "collateral" in reason_lower or "deployed" in reason_lower


def test_allows_trade_within_80_pct_cap():
    """4 positions at $1,600 each = $6,400 deployed. New $1,500 trade → 79.0% → allowed."""
    from risk.risk_manager import RiskManager

    rm = RiskManager(max_total_deployed_pct=0.80)
    # total=$10k, cash=$3.6k → currently $6.4k deployed (64%)
    portfolio = _make_portfolio(total_value=10_000, cash=3_600)
    signal = _make_signal(collateral=1_500)

    result = rm.validate_signal(signal, portfolio)

    collateral_check = next(
        (c for c in result.checks if c.name == "collateral_cap"), None
    )
    assert collateral_check is not None
    assert collateral_check.passed is True


def test_non_sell_put_skips_collateral_check():
    """BUY_TO_CLOSE_PUT should not trigger the collateral cap check at all."""
    from risk.risk_manager import RiskManager

    rm = RiskManager(max_total_deployed_pct=0.80)
    portfolio = _make_portfolio(total_value=10_000, cash=100)  # almost no cash

    sig = MagicMock()
    sig.signal_type = "BUY_TO_CLOSE_PUT"
    sig.symbol = "TEST"
    sig.strategy_id = "wheel"
    sig.metadata = {}

    result = rm.validate_signal(sig, portfolio)

    collateral_check = next(
        (c for c in result.checks if c.name == "collateral_cap"), None
    )
    assert collateral_check is None  # check was skipped entirely


def test_exactly_at_cap_is_rejected():
    """Exactly 80% deployed + a trade that would push to 100% should be rejected."""
    from risk.risk_manager import RiskManager

    rm = RiskManager(max_total_deployed_pct=0.80)
    # $8,000 deployed out of $10,000 = exactly 80% — at the limit
    portfolio = _make_portfolio(total_value=10_000, cash=2_000)
    # Any positive collateral now would exceed 80%
    signal = _make_signal(collateral=1)  # even $1 more should reject

    result = rm.validate_signal(signal, portfolio)

    collateral_check = next(
        (c for c in result.checks if c.name == "collateral_cap"), None
    )
    assert collateral_check is not None
    assert collateral_check.passed is False


def test_no_collateral_in_metadata_skips_check():
    """If the signal has no collateral key, the check is skipped (safe fallback)."""
    from risk.risk_manager import RiskManager

    rm = RiskManager(max_total_deployed_pct=0.80)
    portfolio = _make_portfolio(total_value=10_000, cash=100)

    sig = MagicMock()
    sig.signal_type = "SELL_PUT"
    sig.symbol = "TEST"
    sig.strategy_id = "wheel"
    sig.metadata = {"delta": -0.28}  # no collateral key

    result = rm.validate_signal(sig, portfolio)

    collateral_check = next(
        (c for c in result.checks if c.name == "collateral_cap"), None
    )
    assert collateral_check is None
