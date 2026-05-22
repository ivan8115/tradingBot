"""Tests for WheelStrategy.sync_symbols() dynamic watchlist support."""
from __future__ import annotations

import pytest


def _make_strategy(symbols):
    """Create a WheelStrategy without needing env vars."""
    from strategies.wheel.wheel_strategy import WheelStrategy
    return WheelStrategy(symbols=symbols)


def test_sync_symbols_adds_new_symbols():
    strategy = _make_strategy(["AMD"])
    strategy.sync_symbols(["AMD", "MARA", "TSLA"])
    assert "MARA" in strategy.symbols
    assert "TSLA" in strategy.symbols
    assert "AMD" in strategy.symbols


def test_sync_symbols_keeps_open_position_not_in_new_list():
    """Symbol with open CSP is kept even if not in new watchlist."""
    from strategies.wheel.wheel_strategy import WheelState
    strategy = _make_strategy(["AMD"])
    strategy._positions["AMD"].state = WheelState.CSP_OPEN

    strategy.sync_symbols(["MARA"])  # AMD not in new list but is open

    assert "AMD" in strategy.symbols   # kept — open position
    assert "MARA" in strategy.symbols  # added


def test_sync_symbols_removes_scanning_symbol_not_in_new_list():
    """SCANNING symbols not in new watchlist are removed."""
    from strategies.wheel.wheel_strategy import WheelState
    strategy = _make_strategy(["AMD", "MARA"])
    # Both in SCANNING (default)
    strategy.sync_symbols(["TSLA"])

    assert "TSLA" in strategy.symbols
    assert "AMD" not in strategy.symbols
    assert "MARA" not in strategy.symbols


def test_sync_symbols_updates_bar_windows():
    """New symbols get a bar_windows entry; removed symbols are cleaned up."""
    from collections import deque
    strategy = _make_strategy(["AMD"])
    strategy.sync_symbols(["TSLA"])

    assert "TSLA" in strategy._bar_windows
    assert "AMD" not in strategy._bar_windows  # AMD was SCANNING, removed


def test_sync_symbols_symbols_list_in_sync_with_positions():
    """self.symbols always matches self._positions.keys()."""
    strategy = _make_strategy(["AMD", "MARA"])
    strategy.sync_symbols(["TSLA", "NVDA"])
    assert set(strategy.symbols) == set(strategy._positions.keys())
