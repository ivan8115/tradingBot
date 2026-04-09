"""Repository pattern — CRUD operations and query helpers for all models."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from database.models import (
    PortfolioSnapshot,
    Position,
    Signal,
    Trade,
    WheelCycle,
)


class TradeRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, trade: Trade) -> Trade:
        self._session.add(trade)
        self._session.commit()
        self._session.refresh(trade)
        return trade

    def get_by_order_id(self, order_id: str) -> Trade | None:
        return self._session.query(Trade).filter_by(order_id=order_id).first()

    def get_by_symbol(self, symbol: str, limit: int = 100) -> list[Trade]:
        return (
            self._session.query(Trade)
            .filter_by(symbol=symbol)
            .order_by(Trade.filled_at.desc())
            .limit(limit)
            .all()
        )

    def get_recent(self, limit: int = 50) -> list[Trade]:
        return (
            self._session.query(Trade)
            .order_by(Trade.filled_at.desc())
            .limit(limit)
            .all()
        )


class SignalRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save(
        self,
        strategy_id: str,
        symbol: str,
        signal_type: str,
        strength: float,
        approved: bool,
        rejection_reason: str | None = None,
        metadata: dict | None = None,
    ) -> Signal:
        signal = Signal(
            strategy_id=strategy_id,
            symbol=symbol,
            signal_type=signal_type,
            strength=strength,
            approved=approved,
            rejection_reason=rejection_reason,
            generated_at=datetime.utcnow(),
            metadata_json=json.dumps(metadata) if metadata else None,
        )
        self._session.add(signal)
        self._session.commit()
        return signal


class WheelCycleRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_active(self, symbol: str) -> WheelCycle | None:
        """Get the most recent incomplete cycle for a symbol."""
        return (
            self._session.query(WheelCycle)
            .filter_by(symbol=symbol, completed=False)
            .order_by(WheelCycle.started_at.desc())
            .first()
        )

    def create(self, symbol: str) -> WheelCycle:
        cycle = WheelCycle(
            symbol=symbol,
            state="scanning",
            started_at=datetime.utcnow(),
            total_premium_collected=Decimal("0"),
        )
        self._session.add(cycle)
        self._session.commit()
        self._session.refresh(cycle)
        return cycle

    def update(self, cycle: WheelCycle) -> WheelCycle:
        self._session.add(cycle)
        self._session.commit()
        return cycle


class PortfolioSnapshotRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, snapshot: PortfolioSnapshot) -> PortfolioSnapshot:
        self._session.add(snapshot)
        self._session.commit()
        return snapshot

    def get_latest(self) -> PortfolioSnapshot | None:
        return (
            self._session.query(PortfolioSnapshot)
            .order_by(PortfolioSnapshot.snapshot_date.desc())
            .first()
        )

    def get_all(self) -> list[PortfolioSnapshot]:
        return (
            self._session.query(PortfolioSnapshot)
            .order_by(PortfolioSnapshot.snapshot_date.asc())
            .all()
        )
