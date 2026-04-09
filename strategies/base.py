"""
Abstract base class for all trading strategies.
The same class runs in both backtesting and live trading.
Only the DataFeed differs between modes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from typing import Any

import pandas as pd

from analysis.indicators import IndicatorSnapshot, TechnicalIndicators
from core.config import settings
from core.events import BarEvent, FillEvent, SignalEvent


class Strategy(ABC):
    """
    All strategies implement this interface.

    Lifecycle:
        on_start()               Called once before market open
        on_bar(bar) → signals    Called on every new bar
        on_fill(fill)            Called when an order fills
        on_stop()                Called once after market close
    """

    strategy_id: str = "base"
    symbols: list[str] = []

    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols
        window_size = settings.indicators.bar_window_size
        # Rolling window of bars per symbol for indicator computation
        self._bar_windows: dict[str, deque[dict]] = {
            sym: deque(maxlen=window_size) for sym in symbols
        }
        self._indicators = TechnicalIndicators(
            rsi_period=14,
            macd_fast=12,
            macd_slow=26,
            macd_signal=9,
        )
        # Store last two snapshots per symbol for crossover detection
        self._prev_snapshots: dict[str, IndicatorSnapshot | None] = {
            sym: None for sym in symbols
        }
        self._curr_snapshots: dict[str, IndicatorSnapshot | None] = {
            sym: None for sym in symbols
        }

    # ------------------------------------------------------------------
    # Abstract interface — strategies must implement these
    # ------------------------------------------------------------------

    @abstractmethod
    def on_bar(self, bar: BarEvent) -> list[SignalEvent]:
        """
        Called on every new bar for subscribed symbols.
        Return an empty list if no signal is generated.
        """
        ...

    @abstractmethod
    def on_fill(self, fill: FillEvent) -> None:
        """Update internal state when an order fills."""
        ...

    # ------------------------------------------------------------------
    # Optional hooks
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        """Called once before market open. Override to initialize state."""

    def on_stop(self) -> None:
        """Called once after market close. Override for cleanup."""

    # ------------------------------------------------------------------
    # State persistence (for restarts)
    # ------------------------------------------------------------------

    def get_state(self) -> dict[str, Any]:
        """Return serializable strategy state for persistence."""
        return {}

    def load_state(self, state: dict[str, Any]) -> None:
        """Restore strategy state from a persisted dict."""

    # ------------------------------------------------------------------
    # Helpers available to all strategies
    # ------------------------------------------------------------------

    def _update_indicators(self, bar: BarEvent) -> IndicatorSnapshot:
        """
        Push a new bar into the rolling window and recompute indicators.
        Returns the current IndicatorSnapshot for bar.symbol.
        """
        sym = bar.symbol
        self._bar_windows[sym].append({
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume),
            "vwap": float(bar.vwap) if bar.vwap else None,
            "timestamp": bar.timestamp,
        })

        df = pd.DataFrame(list(self._bar_windows[sym]))
        snap = self._indicators.compute(df)

        self._prev_snapshots[sym] = self._curr_snapshots[sym]
        self._curr_snapshots[sym] = snap
        return snap

    def _get_snapshot(self, symbol: str) -> IndicatorSnapshot | None:
        return self._curr_snapshots.get(symbol)

    def _get_prev_snapshot(self, symbol: str) -> IndicatorSnapshot | None:
        return self._prev_snapshots.get(symbol)

    def _bars_available(self, symbol: str, minimum: int) -> bool:
        return len(self._bar_windows.get(symbol, [])) >= minimum

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(symbols={self.symbols})"
