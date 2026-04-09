"""
PortfolioTracker — syncs live position and account state from Alpaca REST API.
Used as ground truth on startup and for daily reconciliation.
Separate from portfolio/portfolio.py (which tracks running P&L in memory).
"""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime

from loguru import logger

from broker.client import AccountInfo, BrokerClient, PositionInfo
from core.exceptions import BrokerError


class PortfolioTracker:
    """
    Fetches and caches current account + position state from Alpaca.
    Call sync() on startup and after each reconciliation cycle.
    """

    def __init__(self, broker: BrokerClient) -> None:
        self._broker = broker
        self._account: AccountInfo | None = None
        self._positions: dict[str, PositionInfo] = {}
        self._last_sync: datetime | None = None

    def sync(self) -> None:
        """Pull fresh state from Alpaca. Call at market open and periodically."""
        try:
            self._account = self._broker.get_account()
            positions = self._broker.get_positions()
            self._positions = {p.symbol: p for p in positions}
            self._last_sync = datetime.utcnow()

            logger.info(
                f"Portfolio synced: cash=${self._account.cash:,.2f} "
                f"equity=${self._account.equity:,.2f} "
                f"positions={len(self._positions)}"
            )
        except BrokerError as e:
            logger.error(f"Portfolio sync failed: {e}")

    @property
    def cash(self) -> Decimal:
        return self._account.cash if self._account else Decimal("0")

    @property
    def equity(self) -> Decimal:
        return self._account.equity if self._account else Decimal("0")

    @property
    def buying_power(self) -> Decimal:
        return self._account.buying_power if self._account else Decimal("0")

    @property
    def portfolio_value(self) -> Decimal:
        return self._account.portfolio_value if self._account else Decimal("0")

    @property
    def is_trading_blocked(self) -> bool:
        return bool(self._account and (self._account.trading_blocked or self._account.account_blocked))

    def get_position(self, symbol: str) -> PositionInfo | None:
        return self._positions.get(symbol)

    def get_all_positions(self) -> list[PositionInfo]:
        return list(self._positions.values())

    def reconcile_with_internal(self, internal_positions: dict) -> list[str]:
        """
        Compare Alpaca positions with internal portfolio state.
        Returns list of discrepancy warnings.
        """
        warnings = []
        for sym, broker_pos in self._positions.items():
            internal = internal_positions.get(sym)
            if internal is None:
                warnings.append(
                    f"DISCREPANCY: {sym} exists in Alpaca ({broker_pos.quantity} shares) "
                    f"but not in internal portfolio"
                )
            elif abs(internal.quantity - broker_pos.quantity) > 0:
                warnings.append(
                    f"DISCREPANCY: {sym} qty mismatch — "
                    f"Alpaca={broker_pos.quantity}, internal={internal.quantity}"
                )

        for sym in internal_positions:
            if sym not in self._positions:
                warnings.append(
                    f"DISCREPANCY: {sym} in internal portfolio but not in Alpaca"
                )

        for w in warnings:
            logger.warning(f"[Reconciliation] {w}")

        if not warnings:
            logger.info("Reconciliation: positions match ✓")

        return warnings
