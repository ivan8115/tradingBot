"""
MarketDataStream — owns the Alpaca WebSocket connection.
Emits BarEvents and FillEvents into registered async handlers.
Includes auto-reconnect on disconnect.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Callable, Coroutine

from alpaca.data.enums import DataFeed
from alpaca.data.live import StockDataStream
from alpaca.trading.stream import TradingStream
from loguru import logger

from core.config import settings
from core.events import BarEvent, FillEvent
from data.normalizer import normalize_bar


BarHandler = Callable[[BarEvent], Coroutine]
FillHandler = Callable[[FillEvent], Coroutine]


def _strategy_id_from_order(order, default: str = "unknown") -> str:
    """Extract strategy prefix from client_order_id (format: '{strategy}-{uuid12}')."""
    raw = getattr(order, "client_order_id", None) or ""
    return raw.split("-")[0] if raw else default


class MarketDataStream:
    """
    Wraps Alpaca's WebSocket data stream.

    - Subscribes to 1-minute bars for given symbols
    - Emits BarEvents to registered bar_handler
    - Separately connects to trading stream for order updates / fills
    - Auto-reconnects on disconnect (exponential backoff, max 60s)
    """

    def __init__(self) -> None:
        self._data_stream: StockDataStream | None = None
        self._trade_stream: TradingStream | None = None
        self._bar_handler: BarHandler | None = None
        self._fill_handler: FillHandler | None = None
        self._symbols: list[str] = []
        self._running = False
        self._reconnect_delay = 1.0     # seconds, doubles on each failure

    async def start(
        self,
        symbols: list[str],
        bar_handler: BarHandler,
        fill_handler: FillHandler | None = None,
    ) -> None:
        """Connect and start streaming. Runs until stop() is called."""
        self._symbols = symbols
        self._bar_handler = bar_handler
        self._fill_handler = fill_handler
        self._running = True

        # Start both streams concurrently
        await asyncio.gather(
            self._run_data_stream(),
            self._run_trade_stream(),
        )

    async def stop(self) -> None:
        self._running = False
        if self._data_stream:
            try:
                await self._data_stream.stop_ws()
            except Exception:
                pass
        if self._trade_stream:
            try:
                await self._trade_stream.stop_ws()
            except Exception:
                pass
        logger.info("MarketDataStream stopped")

    # ------------------------------------------------------------------
    # Market data stream (bars, quotes)
    # ------------------------------------------------------------------

    async def _run_data_stream(self) -> None:
        while self._running:
            try:
                self._data_stream = StockDataStream(
                    api_key=settings.alpaca_api_key,
                    secret_key=settings.alpaca_secret_key,
                    feed=DataFeed.IEX,   # free feed; use DataFeed.SIP with paid plan
                )

                self._data_stream.subscribe_bars(self._on_bar, *self._symbols)
                logger.info(f"Data stream connected: {self._symbols}")
                self._reconnect_delay = 1.0

                await self._data_stream._run_forever()

            except Exception as e:
                if not self._running:
                    break
                logger.warning(
                    f"Data stream disconnected: {e}. "
                    f"Reconnecting in {self._reconnect_delay:.0f}s..."
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60.0)

    async def _on_bar(self, raw_bar) -> None:
        try:
            data = normalize_bar(raw_bar, source="live")
            bar = BarEvent(
                symbol=data["symbol"],
                timestamp=data["timestamp"],
                open=data["open"],
                high=data["high"],
                low=data["low"],
                close=data["close"],
                volume=data["volume"],
                vwap=data["vwap"],
                trade_count=data["trade_count"],
                source="live",
            )
            if self._bar_handler:
                await self._bar_handler(bar)
        except Exception as e:
            logger.error(f"Error processing bar: {e}")

    # ------------------------------------------------------------------
    # Trading stream (order updates / fills)
    # ------------------------------------------------------------------

    async def _run_trade_stream(self) -> None:
        if not self._fill_handler:
            return

        while self._running:
            try:
                self._trade_stream = TradingStream(
                    api_key=settings.alpaca_api_key,
                    secret_key=settings.alpaca_secret_key,
                    paper=settings.alpaca_paper,
                )
                self._trade_stream.subscribe_trade_updates(self._on_trade_update)
                logger.info("Trading stream connected")
                await self._trade_stream._run_forever()

            except Exception as e:
                if not self._running:
                    break
                logger.warning(f"Trading stream disconnected: {e}. Reconnecting...")
                await asyncio.sleep(self._reconnect_delay)

    async def _on_trade_update(self, update) -> None:
        """Convert Alpaca trade update to FillEvent for fills and assignments."""
        try:
            event_type = getattr(update, "event", None)

            # --- Assignment handling ---
            if event_type == "assigned":
                order = update.order
                raw_qty = getattr(update, "qty", 0) or 0
                qty = int(Decimal(str(raw_qty)))
                price = getattr(update, "price", None)

                if not price:
                    logger.warning("Assignment update missing price for %s; skipping", order.symbol)
                    return

                fill = FillEvent(
                    order_id=str(order.id),
                    symbol=order.symbol,
                    strategy_id=_strategy_id_from_order(order, default="wheel"),
                    side="sell",
                    filled_qty=qty,
                    fill_price=Decimal(str(price)),
                    commission=Decimal("0"),
                    is_options=True,
                    option_contract_id=order.symbol,
                    filled_at=update.timestamp,
                    metadata={"leg": "assignment", "contract_id": order.symbol},
                )
                if self._fill_handler:
                    await self._fill_handler(fill)
                return

            # --- Normal fill handling ---
            if event_type not in ("fill", "partial_fill"):
                return

            order = update.order
            fill_qty = int(getattr(update, "qty", 0) or 0)
            fill_price_raw = getattr(update, "price", None)
            if not fill_price_raw or fill_qty == 0:
                return

            is_options = getattr(order, "asset_class", None) == "us_option"

            fill = FillEvent(
                order_id=str(order.id),
                symbol=order.symbol,
                strategy_id=_strategy_id_from_order(order, default="unknown"),
                side="buy" if order.side.value == "buy" else "sell",
                filled_qty=fill_qty,
                fill_price=Decimal(str(fill_price_raw)),
                commission=Decimal("0"),    # Alpaca provides commission separately
                is_options=is_options,
                option_contract_id=order.symbol if is_options else None,
                filled_at=update.timestamp,
            )

            if self._fill_handler:
                await self._fill_handler(fill)

        except Exception as e:
            logger.error(f"Error processing trade update: {e}")
