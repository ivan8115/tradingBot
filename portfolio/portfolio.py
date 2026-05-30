"""
Internal portfolio tracker.
Tracks cash, positions, and P&L without requiring API calls.
Separate from broker/portfolio.py (which syncs from Alpaca).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from core.events import FillEvent


@dataclass
class PortfolioPosition:
    symbol: str
    quantity: int           # positive = long, negative = short
    avg_cost: Decimal       # average cost per share/contract
    is_options: bool = False

    @property
    def market_value(self) -> Decimal:
        """Must call update_price() to get a meaningful value."""
        return self._current_price * abs(self.quantity)

    def update_price(self, price: Decimal) -> None:
        self._current_price = price

    def unrealized_pnl(self) -> Decimal:
        if not hasattr(self, "_current_price"):
            return Decimal("0")
        if self.quantity > 0:
            return (self._current_price - self.avg_cost) * self.quantity
        return (self.avg_cost - self._current_price) * abs(self.quantity)

    _current_price: Decimal = field(default=Decimal("0"), repr=False)


class Portfolio:
    """
    Pure in-memory portfolio state.
    Used by both live trading and backtesting.
    """

    def __init__(self, cash: Decimal) -> None:
        self._cash = cash
        self._initial_cash = cash
        self._positions: dict[str, PortfolioPosition] = {}
        self._trade_history: list[FillEvent] = []
        self._realized_pnl: Decimal = Decimal("0")
        self._peak_value: Decimal = cash
        self._created_at: datetime = datetime.utcnow()
        self._current_prices: dict[str, Decimal] = {}

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def positions(self) -> dict[str, PortfolioPosition]:
        return self._positions

    @property
    def realized_pnl(self) -> Decimal:
        return self._realized_pnl

    @property
    def trade_history(self) -> list[FillEvent]:
        return self._trade_history

    def update_price(self, symbol: str, price: Decimal) -> None:
        """Cache the current market price for a symbol. Called on every bar."""
        self._current_prices[symbol] = price

    def equity(self, current_prices: dict[str, Decimal] | None = None) -> Decimal:
        """Total value of all positions at current market prices."""
        total = Decimal("0")
        for sym, pos in self._positions.items():
            price = (current_prices or {}).get(sym, Decimal("0"))
            if price:
                multiplier = Decimal("100") if pos.is_options else Decimal("1")
                total += price * abs(pos.quantity) * multiplier
        return total

    def total_value(self, current_prices: dict[str, Decimal] | None = None) -> Decimal:
        prices = current_prices if current_prices is not None else self._current_prices
        return self._cash + self.equity(prices)

    def drawdown(self, current_prices: dict[str, Decimal] | None = None) -> Decimal:
        """Current drawdown from peak as a fraction (0.0 to 1.0)."""
        prices = current_prices if current_prices is not None else self._current_prices
        total = self.total_value(prices)
        if total > self._peak_value:
            self._peak_value = total
        if self._peak_value == Decimal("0"):
            return Decimal("0")
        return (self._peak_value - total) / self._peak_value

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def apply_fill(self, fill: FillEvent) -> None:
        """Update cash and positions when a fill arrives."""
        self._trade_history.append(fill)

        if fill.side == "buy":
            self._cash -= fill.total_cost
            self._open_or_add(fill)
        else:
            self._cash += abs(fill.total_cost)
            realized = self._close_or_reduce(fill)
            self._realized_pnl += realized

    def _open_or_add(self, fill: FillEvent) -> None:
        sym = fill.symbol
        qty = fill.filled_qty
        price = fill.fill_price

        if sym not in self._positions:
            self._positions[sym] = PortfolioPosition(
                symbol=sym,
                quantity=qty,
                avg_cost=price,
                is_options=fill.is_options,
            )
        else:
            pos = self._positions[sym]
            total_qty = pos.quantity + qty
            if total_qty == 0:
                del self._positions[sym]
            else:
                pos.avg_cost = (pos.avg_cost * pos.quantity + price * qty) / total_qty
                pos.quantity = total_qty

    def _close_or_reduce(self, fill: FillEvent) -> Decimal:
        """Returns realized P&L from this fill."""
        sym = fill.symbol
        qty = fill.filled_qty
        price = fill.fill_price

        if sym not in self._positions:
            return Decimal("0")

        pos = self._positions[sym]
        multiplier = Decimal("100") if pos.is_options else Decimal("1")
        realized = (price - pos.avg_cost) * min(qty, pos.quantity) * multiplier

        pos.quantity -= qty
        if pos.quantity <= 0:
            del self._positions[sym]

        return realized

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def summary(self, current_prices: dict[str, Decimal] | None = None) -> dict:
        total = self.total_value(current_prices)
        pnl_total = total - self._initial_cash
        return {
            "cash": float(self._cash),
            "equity": float(self.equity(current_prices)),
            "total_value": float(total),
            "realized_pnl": float(self._realized_pnl),
            "total_pnl": float(pnl_total),
            "total_pnl_pct": float(pnl_total / self._initial_cash * 100),
            "drawdown_pct": float(self.drawdown(current_prices) * 100),
            "open_positions": len(self._positions),
            "total_trades": len(self._trade_history),
        }
