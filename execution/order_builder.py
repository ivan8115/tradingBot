"""
OrderBuilder — constructs OrderEvent objects from SignalEvents.
Handles both equity and options orders.
Selects order type (limit preferred) and calculates limit prices.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from core.config import settings
from core.events import OrderEvent, SignalEvent

# Signal types that map to a buy-side order
BUY_SIGNALS = {"ENTRY_LONG", "BUY_TO_CLOSE_PUT", "BUY_TO_CLOSE_CALL"}
# Signal types that map to a sell-side order
_SELL_SIGNALS = {"EXIT_LONG", "ENTRY_SHORT", "SELL_PUT", "SELL_CALL", "EXIT_SHORT"}
# Options signals
OPTIONS_SIGNALS = {"SELL_PUT", "BUY_TO_CLOSE_PUT", "SELL_CALL", "BUY_TO_CLOSE_CALL"}


class OrderBuilder:
    """
    Translates SignalEvents into OrderEvents ready for submission.

    Limit order pricing:
    - For equities: bid + offset (buys) or ask - offset (sells)
    - For options: mid-price of bid/ask spread (standard practice)
    """

    def __init__(
        self,
        default_order_type: Literal["market", "limit"] | None = None,
        limit_offset_pct: float | None = None,
    ) -> None:
        self._order_type = default_order_type or settings.execution.default_order_type
        self._offset_pct = limit_offset_pct or settings.execution.limit_price_offset_pct

    def build(
        self,
        signal: SignalEvent,
        quantity: int,
        current_price: Decimal | None = None,
        bid: Decimal | None = None,
        ask: Decimal | None = None,
        option_contract_id: str | None = None,
    ) -> OrderEvent:
        """
        Build an OrderEvent from a signal.

        Args:
            signal: The approved signal to execute
            quantity: Number of shares/contracts (from PositionSizer)
            current_price: Last trade price (used if bid/ask not available)
            bid: Current best bid
            ask: Current best ask
            option_contract_id: Alpaca contract symbol for options orders
        """
        is_options = signal.signal_type in OPTIONS_SIGNALS
        side = "buy" if signal.signal_type in BUY_SIGNALS else "sell"

        # Determine order type (exit signals always use market for certainty)
        if signal.signal_type in ("EXIT_LONG", "EXIT_SHORT", "BUY_TO_CLOSE_PUT", "BUY_TO_CLOSE_CALL"):
            order_type = "market"
        else:
            order_type = self._order_type

        limit_price = None
        if order_type == "limit":
            limit_price = self._calc_limit_price(side, current_price, bid, ask, is_options)

        return OrderEvent(
            order_id=str(uuid.uuid4()),
            signal_event=signal,
            order_type=order_type,
            quantity=quantity,
            limit_price=limit_price,
            is_options=is_options,
            option_contract_id=option_contract_id or signal.metadata.get("contract_id"),
            submitted_at=datetime.now(tz=timezone.utc),
        )

    def build_stop_loss(
        self,
        signal: SignalEvent,
        quantity: int,
        stop_price: Decimal,
    ) -> OrderEvent:
        """Build a stop-loss order (protective stop for an open position)."""
        return OrderEvent(
            order_id=str(uuid.uuid4()),
            signal_event=signal,
            order_type="stop",
            quantity=quantity,
            stop_price=stop_price,
            is_options=False,
            submitted_at=datetime.now(tz=timezone.utc),
        )

    def _calc_limit_price(
        self,
        side: str,
        current_price: Decimal | None,
        bid: Decimal | None,
        ask: Decimal | None,
        is_options: bool,
    ) -> Decimal | None:
        if is_options:
            # Options: use mid of bid/ask (standard)
            if bid and ask:
                mid = (bid + ask) / Decimal("2")
                return mid.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            return current_price

        if bid and ask:
            if side == "buy":
                # Aggressive limit: slightly above bid to get filled
                offset = ask * Decimal(str(self._offset_pct))
                price = bid + offset
            else:
                offset = bid * Decimal(str(self._offset_pct))
                price = ask - offset
            return price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        if current_price:
            offset = current_price * Decimal(str(self._offset_pct))
            if side == "buy":
                return (current_price + offset).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            return (current_price - offset).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        return None
