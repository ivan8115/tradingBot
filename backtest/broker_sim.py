"""
Simulated broker for backtesting.
Fills market orders at next-bar's open price (avoids look-ahead bias).
Applies configurable slippage and commission.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from core.events import BarEvent, FillEvent, OrderEvent


class SimulatedBroker:
    """
    Realistic fill simulation for backtesting.

    Fill rules:
    - Market orders: filled at next bar's open + slippage
    - Limit orders: filled if next bar trades through the limit price
    - Options: priced via IV (simplified: use last known price)
    """

    def __init__(
        self,
        slippage_bps: float = 5.0,          # basis points of slippage per trade
        commission_per_share: float = 0.005, # $0.005/share (IBKR-style)
        commission_per_contract: float = 0.65, # per options contract
        min_commission: float = 1.00,        # minimum per trade
    ) -> None:
        self._slippage_bps = slippage_bps / 10_000.0  # convert to fraction
        self._commission_per_share = commission_per_share
        self._commission_per_contract = commission_per_contract
        self._min_commission = min_commission

        # Pending orders waiting for the next bar
        self._pending: list[OrderEvent] = []
        self._order_counter: int = 0

    def submit_order(self, order: OrderEvent) -> None:
        """Queue an order for fill on the next bar."""
        self._pending.append(order)

    def process_bar(self, bar: BarEvent) -> list[FillEvent]:
        """
        Called on every new bar. Fills any pending orders using this bar's OHLCV.
        Returns list of fills generated.
        """
        fills: list[FillEvent] = []
        still_pending: list[OrderEvent] = []

        for order in self._pending:
            if order.signal_event.symbol != bar.symbol:
                still_pending.append(order)
                continue

            fill = self._try_fill(order, bar)
            if fill:
                fills.append(fill)
            else:
                still_pending.append(order)

        self._pending = still_pending
        return fills

    def _try_fill(self, order: OrderEvent, bar: BarEvent) -> FillEvent | None:
        signal = order.signal_event
        side = "buy" if signal.signal_type in ("ENTRY_LONG", "ENTRY_SHORT") else "sell"
        # For options: buy_to_close = buy, sell_to_open = sell
        if signal.signal_type in ("BUY_TO_CLOSE_PUT", "BUY_TO_CLOSE_CALL"):
            side = "buy"
        elif signal.signal_type in ("SELL_PUT", "SELL_CALL"):
            side = "sell"

        if order.order_type == "market":
            fill_price = self._apply_slippage(bar.open, side)
        elif order.order_type == "limit":
            if not order.limit_price:
                return None
            fill_price = self._check_limit_fill(order.limit_price, side, bar)
            if fill_price is None:
                return None
        elif order.order_type == "stop":
            if not order.stop_price:
                return None
            fill_price = self._check_stop_fill(order.stop_price, side, bar)
            if fill_price is None:
                return None
        else:
            return None

        commission = self._calculate_commission(order.quantity, order.is_options)

        return FillEvent(
            order_id=order.order_id,
            symbol=signal.symbol,
            strategy_id=signal.strategy_id,
            side=side,
            filled_qty=order.quantity,
            fill_price=fill_price,
            commission=commission,
            is_options=order.is_options,
            option_contract_id=order.option_contract_id,
            filled_at=bar.timestamp,
            metadata=signal.metadata,
        )

    def _apply_slippage(self, price: Decimal, side: str) -> Decimal:
        """Buy at slightly above open, sell at slightly below."""
        slip = price * Decimal(str(self._slippage_bps))
        if side == "buy":
            return price + slip
        return price - slip

    def _check_limit_fill(
        self, limit_price: Decimal, side: str, bar: BarEvent
    ) -> Decimal | None:
        """
        Limit buy: fills if bar's low trades through limit (bar low <= limit).
        Limit sell: fills if bar's high trades through limit (bar high >= limit).
        Fill at limit price (best case — realistic for liquid stocks).
        """
        if side == "buy" and bar.low <= limit_price:
            return limit_price
        if side == "sell" and bar.high >= limit_price:
            return limit_price
        return None

    def _check_stop_fill(
        self, stop_price: Decimal, side: str, bar: BarEvent
    ) -> Decimal | None:
        """
        Stop buy (buy-stop): triggers if bar high >= stop. Fill at open or stop (worse).
        Stop sell (stop-loss): triggers if bar low <= stop. Fill at stop with slippage.
        """
        if side == "buy" and bar.high >= stop_price:
            fill = max(bar.open, stop_price)
            return self._apply_slippage(fill, side)
        if side == "sell" and bar.low <= stop_price:
            fill = min(bar.open, stop_price)
            return self._apply_slippage(fill, side)
        return None

    def _calculate_commission(self, qty: int, is_options: bool) -> Decimal:
        if is_options:
            comm = qty * self._commission_per_contract
        else:
            comm = qty * self._commission_per_share
        return Decimal(str(max(comm, self._min_commission)))
