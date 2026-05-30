"""
RiskManager weekly trade counters must use (year, week) tuples so they
reset correctly at year boundaries (week 1 of 2026 != week 1 of 2027).
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

from risk.risk_manager import RiskManager


def test_global_week_counter_resets_on_new_iso_year():
    """
    Week 1 of year N+1 must reset the counter even though the ISO week number
    (1) is the same as the previous week 1.
    """
    rm = RiskManager()
    # Manually seed the counter as if 3 trades happened in week 1 of last year
    rm._total_new_trades_this_week = 3
    rm._global_week_iso = (2025, 1)  # old year

    # Now the system is in week 1 of 2026 — counter MUST reset
    with patch("risk.risk_manager.date") as mock_date:
        mock_date.today.return_value = date(2026, 1, 1)  # Jan 1 2026 = ISO week 1 of 2026
        rm._reset_global_week_counter_if_needed()

    assert rm._total_new_trades_this_week == 0, (
        "Counter must reset when moving to the same week number in a new year."
    )


def test_global_week_counter_does_not_reset_within_same_week():
    """Counter must NOT reset mid-week (same year + same week number)."""
    rm = RiskManager()
    rm._total_new_trades_this_week = 2
    rm._global_week_iso = date.today().isocalendar()[:2]

    rm._reset_global_week_counter_if_needed()

    assert rm._total_new_trades_this_week == 2, "Must not reset within same (year, week)"


def test_momentum_week_counter_resets_on_new_iso_year():
    """Same year-boundary fix must apply to the momentum-specific week counter."""
    rm = RiskManager()
    rm._momentum_trades_this_week = 5
    rm._week_iso = (2025, 52)

    with patch("risk.risk_manager.date") as mock_date:
        mock_date.today.return_value = date(2026, 1, 1)  # Jan 1 2026 = ISO week 1 of 2026
        rm._reset_week_counter_if_needed()

    assert rm._momentum_trades_this_week == 0
