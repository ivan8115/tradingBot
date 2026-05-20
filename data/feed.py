"""
DataFeed abstraction.
Live trading uses LiveDataFeed (WebSocket-backed).
Backtesting uses BacktestDataFeed (replays historical bars).
Strategies are unaware of which they're connected to.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import AsyncIterator, Callable, Coroutine

import pandas as pd

from core.events import BarEvent


DataHandler = Callable[[BarEvent], Coroutine]


class DataFeed(ABC):
    @abstractmethod
    async def subscribe(
        self,
        symbols: list[str],
        handler: DataHandler,
    ) -> None:
        """Subscribe to bar updates. handler is called for each new bar."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the feed."""
        ...


class BacktestDataFeed(DataFeed):
    """
    Replays historical bars as BarEvents, identical in shape to live bars.
    Runs at max speed (speed_multiplier=0) or throttled for debugging.
    """

    def __init__(
        self,
        bars: dict[str, pd.DataFrame],    # {symbol: DataFrame}
        speed_multiplier: float = 0.0,    # 0 = instant, 1 = real-time, 2 = 2x, etc.
    ) -> None:
        self._bars = bars
        self._speed = speed_multiplier
        self._running = False

    async def subscribe(self, symbols: list[str], handler: DataHandler) -> None:
        """
        Iterate through all bars in chronological order across all symbols,
        emitting a BarEvent for each one.
        """
        self._running = True

        # Merge all symbols into a single time-sorted stream
        events: list[BarEvent] = []
        for sym, df in self._bars.items():
            if sym not in symbols or df.empty:
                continue
            for _, row in df.iterrows():
                ts = row.get("timestamp", row.name)
                if hasattr(ts, "to_pydatetime"):
                    ts = ts.to_pydatetime()
                bar = BarEvent(
                    symbol=sym,
                    timestamp=ts,
                    open=Decimal(str(row.get("open", 0))),
                    high=Decimal(str(row.get("high", 0))),
                    low=Decimal(str(row.get("low", 0))),
                    close=Decimal(str(row.get("close", 0))),
                    volume=int(row.get("volume", 0)),
                    vwap=Decimal(str(row["vwap"])) if "vwap" in row and pd.notna(row["vwap"]) else None,
                    source="backtest",
                )
                events.append(bar)

        # Sort all events chronologically
        events.sort(key=lambda e: e.timestamp)

        for bar in events:
            if not self._running:
                break
            await handler(bar)
            if self._speed > 0:
                await asyncio.sleep(1.0 / self._speed)

    async def stop(self) -> None:
        self._running = False


class LiveDataFeed(DataFeed):
    """
    Wraps the Alpaca WebSocket stream.
    Emits BarEvents to registered handlers on each bar update.
    Implemented in Phase 4 (broker/market_data.py).
    """

    def __init__(self, market_data_stream) -> None:
        self._stream = market_data_stream
        self._handler: DataHandler | None = None

    async def subscribe(self, symbols: list[str], handler: DataHandler) -> None:
        self._handler = handler
        await self._stream.start(symbols, self._on_bar)

    async def _on_bar(self, bar: BarEvent) -> None:
        if self._handler:
            await self._handler(bar)

    async def stop(self) -> None:
        await self._stream.stop()
