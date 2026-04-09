"""
Typed event models — the shared contract between every module.
These are the only objects that cross module boundaries.
All prices use Decimal to avoid float rounding errors.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class BarEvent(BaseModel):
    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    vwap: Optional[Decimal] = None
    trade_count: int = 0
    source: Literal["live", "backtest"] = "live"

    model_config = {"arbitrary_types_allowed": True}


class SignalEvent(BaseModel):
    strategy_id: str
    symbol: str
    signal_type: Literal[
        "ENTRY_LONG",
        "EXIT_LONG",
        "ENTRY_SHORT",
        "EXIT_SHORT",
        "SELL_PUT",            # Open CSP
        "BUY_TO_CLOSE_PUT",   # Close CSP
        "SELL_CALL",           # Open CC
        "BUY_TO_CLOSE_CALL",  # Close CC
    ]
    strength: float = Field(ge=0.0, le=1.0, default=1.0)
    timestamp: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    # metadata for options: strike, expiry, contract_id, delta, dte, etc.


class OrderEvent(BaseModel):
    order_id: str
    signal_event: SignalEvent
    order_type: Literal["market", "limit", "stop", "stop_limit"]
    quantity: int
    limit_price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    is_options: bool = False
    option_contract_id: Optional[str] = None
    submitted_at: datetime

    model_config = {"arbitrary_types_allowed": True}


class FillEvent(BaseModel):
    order_id: str
    symbol: str
    strategy_id: str
    side: Literal["buy", "sell"]
    filled_qty: int
    fill_price: Decimal
    commission: Decimal = Decimal("0")
    is_options: bool = False
    option_contract_id: Optional[str] = None
    filled_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}

    @property
    def notional_value(self) -> Decimal:
        multiplier = Decimal("100") if self.is_options else Decimal("1")
        return self.fill_price * self.filled_qty * multiplier

    @property
    def total_cost(self) -> Decimal:
        """Positive = cash outflow (buy), negative = cash inflow (sell)."""
        if self.side == "buy":
            return self.notional_value + self.commission
        return -(self.notional_value - self.commission)
