"""
BrokerClient — single facade over alpaca-py.
Paper vs. live is a config flag; all callers are unaware of the difference.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionSnapshotRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import ContractType, OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopOrderRequest,
)
from loguru import logger

from core.config import settings
from core.exceptions import BrokerError, InsufficientFundsError, OrderError
from strategies.wheel.csp_leg import OptionContract


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
        self._option_data = OptionHistoricalDataClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
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
    # Options
    # ------------------------------------------------------------------

    def get_options_chain(
        self,
        symbol: str,
        dte_min: int = 21,
        dte_max: int = 45,
        option_type: str = "put",     # "put" | "call" | "both"
    ) -> list[OptionContract]:
        """
        Fetch options chain from Alpaca and return normalized OptionContract list.
        Filters by DTE window. Returns [] on any error (non-fatal).
        """
        today = date.today()
        exp_gte = today + timedelta(days=dte_min)
        exp_lte = today + timedelta(days=dte_max)

        try:
            contract_type = None
            if option_type == "put":
                contract_type = ContractType.PUT
            elif option_type == "call":
                contract_type = ContractType.CALL

            req = GetOptionContractsRequest(
                underlying_symbols=[symbol],
                expiration_date_gte=exp_gte,
                expiration_date_lte=exp_lte,
                type=contract_type,
            )
            resp = self._client.get_option_contracts(req)
            raw_contracts = resp.option_contracts if resp else []
        except Exception as e:
            logger.warning(f"[Options] Failed to fetch chain for {symbol}: {e}")
            return []

        if not raw_contracts:
            return []

        # Fetch snapshots for greeks + quotes
        contract_symbols = [c.symbol for c in raw_contracts]
        snapshots: dict = {}
        try:
            snap_req = OptionSnapshotRequest(symbol_or_symbols=contract_symbols)
            snapshots = self._option_data.get_option_snapshot(snap_req)
        except Exception as e:
            logger.warning(f"[Options] Snapshot fetch failed for {symbol}: {e}")
            # Continue — use close_price fallback

        result: list[OptionContract] = []
        for raw in raw_contracts:
            expiry = raw.expiration_date
            if isinstance(expiry, str):
                expiry = date.fromisoformat(expiry)
            dte = (expiry - today).days
            if not (dte_min <= dte <= dte_max):
                continue

            snap = snapshots.get(raw.symbol)
            if snap and snap.latest_quote:
                bid = Decimal(str(snap.latest_quote.bid_price or 0))
                ask = Decimal(str(snap.latest_quote.ask_price or 0))
            else:
                mid_fallback = Decimal(str(raw.close_price or 0))
                bid = mid_fallback * Decimal("0.95")
                ask = mid_fallback * Decimal("1.05")

            if snap and snap.greeks:
                delta = float(snap.greeks.delta or 0)
                iv = float(snap.greeks.implied_volatility or 0)
            else:
                delta = 0.0
                iv = 0.0

            result.append(OptionContract(
                symbol=symbol,
                contract_id=raw.symbol,
                option_type=raw.type.value if hasattr(raw.type, "value") else str(raw.type),
                strike=Decimal(str(raw.strike_price)),
                expiry=expiry,
                dte=dte,
                bid=bid,
                ask=ask,
                delta=delta,
                iv=iv,
                open_interest=int(raw.open_interest or 0),
                volume=int(raw.volume or 0),
            ))

        logger.info(f"[Options] {symbol}: {len(result)} contracts fetched (DTE {dte_min}-{dte_max})")
        return result

    def submit_options_order(
        self,
        contract_symbol: str,
        qty: int,
        side: str,
        order_type: str = "limit",
        limit_price: Decimal | None = None,
        time_in_force: str = "day",
    ) -> str:
        """Submit an options order. Returns Alpaca order ID."""
        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

        try:
            if order_type == "limit" and limit_price is not None:
                request = LimitOrderRequest(
                    symbol=contract_symbol,
                    qty=qty,
                    side=order_side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=float(limit_price),
                )
            else:
                request = MarketOrderRequest(
                    symbol=contract_symbol,
                    qty=qty,
                    side=order_side,
                    time_in_force=TimeInForce.DAY,
                )

            logger.info(
                f"[Options] Submitting {order_type} {side} {qty}x {contract_symbol} "
                f"@ {limit_price or 'market'}"
            )
            order = self._client.submit_order(request)
            logger.info(f"[Options] Order submitted: {order.id}")
            return str(order.id)

        except Exception as e:
            msg = str(e).lower()
            if "insufficient" in msg or "buying power" in msg:
                raise InsufficientFundsError(
                    f"Insufficient funds for options order {contract_symbol}"
                ) from e
            raise OrderError(f"Options order failed for {contract_symbol}: {e}") from e

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
