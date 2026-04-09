"""
BrokerClient — single facade over alpaca-py.
Paper vs. live is a config flag; all callers are unaware of the difference.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopOrderRequest,
)
from loguru import logger

from core.config import settings
from core.exceptions import BrokerError, InsufficientFundsError, OrderError


@dataclass
class AccountInfo:
    cash: Decimal
    portfolio_value: Decimal
    buying_power: Decimal
    equity: Decimal
    currency: str
    pattern_day_trader: bool
    trading_blocked: bool
    account_blocked: bool


@dataclass
class PositionInfo:
    symbol: str
    quantity: int           # negative for short
    avg_entry_price: Decimal
    current_price: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal
    unrealized_pnl_pct: Decimal
    side: str               # "long" | "short"


class BrokerClient:
    """
    Thin wrapper around alpaca-py TradingClient.
    Paper vs. live controlled by settings.alpaca_paper.
    """

    def __init__(self) -> None:
        self._client = TradingClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
            paper=settings.alpaca_paper,
        )
        mode = "PAPER" if settings.alpaca_paper else "LIVE"
        logger.info(f"BrokerClient initialized [{mode}]")

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_account(self) -> AccountInfo:
        try:
            acct = self._client.get_account()
        except Exception as e:
            raise BrokerError(f"Failed to fetch account: {e}") from e

        return AccountInfo(
            cash=Decimal(str(acct.cash)),
            portfolio_value=Decimal(str(acct.portfolio_value)),
            buying_power=Decimal(str(acct.buying_power)),
            equity=Decimal(str(acct.equity)),
            currency=acct.currency,
            pattern_day_trader=bool(acct.pattern_day_trader),
            trading_blocked=bool(acct.trading_blocked),
            account_blocked=bool(acct.account_blocked),
        )

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def get_positions(self) -> list[PositionInfo]:
        try:
            positions = self._client.get_all_positions()
        except Exception as e:
            raise BrokerError(f"Failed to fetch positions: {e}") from e

        result = []
        for p in positions:
            qty = int(float(p.qty))
            result.append(PositionInfo(
                symbol=p.symbol,
                quantity=qty if p.side.value == "long" else -qty,
                avg_entry_price=Decimal(str(p.avg_entry_price)),
                current_price=Decimal(str(p.current_price)),
                market_value=Decimal(str(p.market_value)),
                unrealized_pnl=Decimal(str(p.unrealized_pl)),
                unrealized_pnl_pct=Decimal(str(p.unrealized_plpc)),
                side=p.side.value,
            ))
        return result

    def get_position(self, symbol: str) -> PositionInfo | None:
        try:
            p = self._client.get_open_position(symbol)
        except Exception:
            return None

        qty = int(float(p.qty))
        return PositionInfo(
            symbol=p.symbol,
            quantity=qty if p.side.value == "long" else -qty,
            avg_entry_price=Decimal(str(p.avg_entry_price)),
            current_price=Decimal(str(p.current_price)),
            market_value=Decimal(str(p.market_value)),
            unrealized_pnl=Decimal(str(p.unrealized_pl)),
            unrealized_pnl_pct=Decimal(str(p.unrealized_plpc)),
            side=p.side.value,
        )

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def submit_market_order(
        self,
        symbol: str,
        qty: int,
        side: str,                   # "buy" | "sell"
        time_in_force: str = "day",
    ) -> str:
        """Submit a market order. Returns the Alpaca order ID."""
        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
        tif = TimeInForce(time_in_force.upper()) if time_in_force != "day" else TimeInForce.DAY

        request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=order_side,
            time_in_force=tif,
        )
        return self._submit(request, symbol, side, qty)

    def submit_limit_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        limit_price: Decimal,
        time_in_force: str = "day",
    ) -> str:
        """Submit a limit order. Returns the Alpaca order ID."""
        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
        tif = TimeInForce.DAY

        request = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=order_side,
            time_in_force=tif,
            limit_price=float(limit_price),
        )
        return self._submit(request, symbol, side, qty, limit_price=limit_price)

    def submit_stop_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        stop_price: Decimal,
        time_in_force: str = "day",
    ) -> str:
        """Submit a stop order. Returns the Alpaca order ID."""
        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

        request = StopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=order_side,
            time_in_force=TimeInForce.DAY,
            stop_price=float(stop_price),
        )
        return self._submit(request, symbol, side, qty, stop_price=stop_price)

    def cancel_order(self, order_id: str) -> None:
        try:
            self._client.cancel_order_by_id(order_id)
            logger.info(f"Cancelled order {order_id}")
        except Exception as e:
            raise OrderError(f"Failed to cancel order {order_id}: {e}") from e

    def cancel_all_orders(self) -> None:
        try:
            self._client.cancel_orders()
            logger.warning("Cancelled ALL open orders")
        except Exception as e:
            raise OrderError(f"Failed to cancel all orders: {e}") from e

    def get_open_orders(self) -> list:
        try:
            return self._client.get_orders(filter=GetOrdersRequest(status="open"))
        except Exception as e:
            raise BrokerError(f"Failed to fetch open orders: {e}") from e

    def is_market_open(self) -> bool:
        try:
            clock = self._client.get_clock()
            return clock.is_open
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _submit(self, request, symbol: str, side: str, qty: int, **kwargs) -> str:
        price_info = " ".join(f"{k}={v}" for k, v in kwargs.items())
        logger.info(f"Submitting {type(request).__name__}: {side} {qty}x {symbol} {price_info}")
        try:
            order = self._client.submit_order(request)
            logger.info(f"Order submitted: {order.id} [{order.status}]")
            return str(order.id)
        except Exception as e:
            msg = str(e).lower()
            if "insufficient" in msg or "buying power" in msg:
                raise InsufficientFundsError(f"Insufficient funds for {side} {qty}x {symbol}") from e
            raise OrderError(f"Order submission failed for {symbol}: {e}") from e
