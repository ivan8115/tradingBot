"""
Executor — receives risk-approved signals, builds orders, submits to broker,
and persists everything to the database.
"""

from __future__ import annotations

from decimal import Decimal

from loguru import logger

from broker.client import BrokerClient
from core.events import FillEvent, OrderEvent, SignalEvent
from database.migrations import get_session_factory
from database.models import Signal, Trade
from datetime import datetime
from execution.order_builder import BUY_SIGNALS, OPTIONS_SIGNALS, OrderBuilder


class Executor:
    """
    The final step before capital leaves the account.
    Receives an approved (signal, quantity) pair, builds the order,
    submits it to Alpaca, and records it in the database.
    """

    def __init__(
        self,
        broker: BrokerClient,
        order_builder: OrderBuilder,
        db_path: str = "data/trading.db",
    ) -> None:
        self._broker = broker
        self._builder = order_builder
        self._session_factory = get_session_factory(db_path)
        self._pending_order_metadata: dict[str, dict] = {}

    async def execute_signal(
        self,
        signal: SignalEvent,
        quantity: int,
        current_price: Decimal | None = None,
        bid: Decimal | None = None,
        ask: Decimal | None = None,
    ) -> OrderEvent | None:
        """
        Build and submit an order for an approved signal.
        Returns the OrderEvent on success, None on failure.
        """
        if quantity <= 0:
            logger.debug(f"Skipping signal {signal.signal_type} {signal.symbol}: qty=0")
            return None

        order = self._builder.build(
            signal=signal,
            quantity=quantity,
            current_price=current_price,
            bid=bid,
            ask=ask,
        )

        # Submit to broker
        try:
            is_options = signal.signal_type in OPTIONS_SIGNALS
            side = "buy" if signal.signal_type in BUY_SIGNALS else "sell"

            if is_options:
                contract_id = signal.metadata.get("contract_id")
                if not contract_id:
                    logger.error(f"Options signal missing contract_id: {signal.symbol}")
                    return None
                premium = signal.metadata.get("premium")
                limit_price = Decimal(str(premium)) if premium else None
                alpaca_id = self._broker.submit_options_order(
                    contract_symbol=contract_id,
                    qty=quantity,
                    side=side,
                    order_type="limit" if limit_price else "market",
                    limit_price=limit_price,
                    client_order_id=signal.strategy_id,
                )
                # Store metadata so _on_fill can enrich bare live fills
                self._pending_order_metadata[alpaca_id] = {
                    "strategy_id": signal.strategy_id,
                    **signal.metadata,
                }
            elif order.order_type == "market":
                alpaca_id = self._broker.submit_market_order(
                    symbol=signal.symbol,
                    qty=quantity,
                    side=side,
                )
            else:
                if not order.limit_price:
                    logger.error(f"Limit order has no price for {signal.symbol}")
                    return None
                alpaca_id = self._broker.submit_limit_order(
                    symbol=signal.symbol,
                    qty=quantity,
                    side=side,
                    limit_price=order.limit_price,
                )

            logger.info(
                f"[Executor] Submitted: {signal.signal_type} {quantity}x {signal.symbol} "
                f"| order_id={alpaca_id} type={order.order_type}"
            )

        except Exception as e:
            logger.error(f"[Executor] Order failed: {signal.symbol} — {e}")
            self._save_signal(signal, approved=True, rejection_reason=f"Submission failed: {e}")
            return None

        # Persist signal to DB
        self._save_signal(signal, approved=True)

        return order

    def record_fill(self, fill: FillEvent) -> None:
        """Persist a fill to the database. Called when a fill event arrives."""
        with self._session_factory() as session:
            trade = Trade(
                order_id=fill.order_id,
                symbol=fill.symbol,
                strategy_id=fill.strategy_id,
                side=fill.side,
                quantity=fill.filled_qty,
                fill_price=fill.fill_price,
                commission=fill.commission,
                is_options=fill.is_options,
                option_contract_id=fill.option_contract_id,
                filled_at=fill.filled_at,
            )
            session.add(trade)
            session.commit()
            logger.bind(trade_log=True).info(
                f"FILL | {fill.side.upper()} {fill.filled_qty}x {fill.symbol} "
                f"@ ${fill.fill_price} | commission=${fill.commission} "
                f"| order_id={fill.order_id}"
            )

    def record_rejected_signal(
        self,
        signal: SignalEvent,
        rejection_reason: str,
    ) -> None:
        """Public API for recording AI-rejected or pre-execution rejections."""
        self._save_signal(signal, approved=False, rejection_reason=rejection_reason)

    def pop_pending_metadata(self, order_id: str) -> dict | None:
        """
        Retrieve and remove pending signal metadata for an order.
        Returns None if the order is not in the registry (e.g. assignment fills,
        equity fills, or orders cancelled before filling).
        """
        return self._pending_order_metadata.pop(order_id, None)

    def _save_signal(
        self,
        signal: SignalEvent,
        approved: bool,
        rejection_reason: str | None = None,
    ) -> None:
        import json
        with self._session_factory() as session:
            record = Signal(
                strategy_id=signal.strategy_id,
                symbol=signal.symbol,
                signal_type=signal.signal_type,
                strength=signal.strength,
                approved=approved,
                rejection_reason=rejection_reason,
                generated_at=signal.timestamp,
                metadata_json=json.dumps(signal.metadata) if signal.metadata else None,
            )
            session.add(record)
            session.commit()
