"""
Covered Call leg of the Wheel Strategy.
Handles strike selection, entry, and management of open CC positions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from core.config import CCConfig
from strategies.wheel.csp_leg import OptionContract


@dataclass
class CCPosition:
    """Tracks an open Covered Call position."""
    symbol: str
    contract: OptionContract
    premium_received: Decimal       # credit per share
    opened_at: datetime
    stock_cost_basis: Decimal       # cost basis of underlying shares
    contracts: int = 1              # each contract = 100 shares

    @property
    def max_profit(self) -> Decimal:
        return self.premium_received * 100 * self.contracts

    @property
    def breakeven(self) -> Decimal:
        """Stock price at which CC covers the cost basis."""
        return self.stock_cost_basis - self.premium_received

    def current_value(self, current_contract_price: Decimal) -> Decimal:
        return current_contract_price * 100 * self.contracts

    def unrealized_pnl(self, current_contract_price: Decimal) -> Decimal:
        """Short call P&L = premium received - current value."""
        return self.max_profit - self.current_value(current_contract_price)

    def profit_pct(self, current_contract_price: Decimal) -> float:
        if self.max_profit == 0:
            return 0.0
        return float(self.unrealized_pnl(current_contract_price) / self.max_profit)

    def stock_pnl_if_called(self, fill_price: Decimal) -> Decimal:
        """P&L from the stock position if shares are called away."""
        return (self.contract.strike - self.stock_cost_basis) * 100 * self.contracts

    def total_pnl_if_called(self) -> Decimal:
        """Total Wheel cycle P&L if called away at strike."""
        return self.stock_pnl_if_called(self.contract.strike) + self.max_profit


class CoveredCallLeg:
    """
    Manages the Covered Call leg of the Wheel Strategy.
    """

    def __init__(self, config: CCConfig) -> None:
        self._cfg = config

    def select_strike(
        self,
        chain: list[OptionContract],
        stock_cost_basis: Decimal,
        underlying_price: float,
    ) -> Optional[OptionContract]:
        """
        Find the call contract that:
        1. Has strike ABOVE the cost basis (selling at or above breakeven)
        2. DTE in [min_dte, max_dte]
        3. Delta closest to target (e.g. 0.30)

        We never sell a CC below our cost basis — that locks in a loss if called.
        """
        calls = [c for c in chain if c.option_type == "call"]

        # Filter DTE
        in_window = [
            c for c in calls
            if self._cfg.min_dte <= c.dte <= self._cfg.max_dte
        ]

        # Strike must be above cost basis
        above_cost = [
            c for c in in_window
            if c.strike > stock_cost_basis
        ]
        if not above_cost:
            # Relax: allow at cost basis if no strikes above are available
            above_cost = [c for c in in_window if c.strike >= stock_cost_basis]

        if not above_cost:
            return None

        # Closest to target delta
        target = self._cfg.target_delta  # positive (e.g. 0.30)
        return min(above_cost, key=lambda c: abs(c.delta - target))

    def should_close_early(
        self,
        position: CCPosition,
        current_contract_price: Decimal,
    ) -> tuple[bool, str]:
        """
        Close the CC early when:
        1. Profit target reached (50% of max)
        2. DTE ≤ roll_when_dte (roll to next expiry)
        """
        profit_pct = position.profit_pct(current_contract_price)

        if profit_pct >= self._cfg.profit_target_pct:
            return True, f"Profit target: {profit_pct*100:.0f}% of max"

        if position.contract.dte <= self._cfg.roll_when_dte:
            return True, f"DTE={position.contract.dte} ≤ {self._cfg.roll_when_dte} — roll"

        return False, ""

    def is_deep_itm(self, position: CCPosition, underlying_price: Decimal) -> bool:
        """
        True if the call is deep in-the-money (stock price well above strike).
        In this case we may want to roll up to avoid losing the stock at a loss.
        """
        threshold = float(position.contract.strike) * 1.05  # 5% above strike = deep ITM
        return float(underlying_price) > threshold

    def total_premium_collected(
        self,
        csp_premium: Decimal,
        cc_premium: Decimal,
        contracts: int = 1,
    ) -> Decimal:
        """Total premium collected across the full Wheel cycle."""
        return (csp_premium + cc_premium) * 100 * contracts
