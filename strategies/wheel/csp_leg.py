"""
Cash-Secured Put leg of the Wheel Strategy.
Handles strike selection, entry conditions, and early exit rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from analysis.greeks import calculate_greeks, calculate_iv, dte_to_years, expected_move
from core.config import CSPConfig


@dataclass
class OptionContract:
    """Represents a single options contract from a chain."""
    symbol: str             # underlying
    contract_id: str        # Alpaca contract symbol (e.g. AAPL240119P00180000)
    option_type: str        # "put" | "call"
    strike: Decimal
    expiry: date
    dte: int
    bid: Decimal
    ask: Decimal
    delta: float
    iv: float
    open_interest: int = 0
    volume: int = 0

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread_pct(self) -> float:
        if self.mid == 0:
            return 1.0
        return float((self.ask - self.bid) / self.mid)


@dataclass
class CSPPosition:
    """Tracks an open Cash-Secured Put position."""
    symbol: str
    contract: OptionContract
    premium_received: Decimal       # credit per share (× 100 for total)
    opened_at: datetime
    contracts: int = 1              # number of contracts (each = 100 shares)
    cost_basis: Optional[Decimal] = None  # set if assigned
    underlying_price_at_entry: Optional[Decimal] = None  # underlying price when CSP was opened

    @property
    def max_profit(self) -> Decimal:
        return self.premium_received * 100 * self.contracts

    @property
    def collateral_required(self) -> Decimal:
        """Cash needed to secure the put (strike × 100 × contracts)."""
        return self.contract.strike * 100 * self.contracts

    def current_value(self, current_contract_price: Decimal) -> Decimal:
        """Current value of the SHORT put position (liability)."""
        return current_contract_price * 100 * self.contracts

    def unrealized_pnl(self, current_contract_price: Decimal) -> Decimal:
        """
        For a short put: P&L = premium received - current value.
        Positive = profit (option lost value).
        """
        return self.max_profit - self.current_value(current_contract_price)

    def profit_pct(self, current_contract_price: Decimal) -> float:
        if self.max_profit == 0:
            return 0.0
        return float(self.unrealized_pnl(current_contract_price) / self.max_profit)


class CashSecuredPutLeg:
    """
    Manages the CSP leg of the Wheel Strategy.
    Selects strikes, evaluates entry conditions, and signals exits.
    """

    def __init__(self, config: CSPConfig) -> None:
        self._cfg = config
        self._symbol_pain_thresholds: dict[str, float] = {}

    def select_strike(
        self,
        chain: list[OptionContract],
        underlying_price: float,
        risk_free_rate: float = 0.05,
    ) -> Optional[OptionContract]:
        """
        Find the put contract that best matches our target delta,
        within the target DTE window and meeting minimum premium.

        Selection criteria (in order):
        1. DTE in [min_dte, max_dte]
        2. Bid >= min_premium
        3. Spread < 5% of mid (liquidity check)
        4. Delta closest to target_delta
        """
        puts = [c for c in chain if c.option_type == "put"]

        # Filter by DTE
        in_window = [
            c for c in puts
            if self._cfg.min_dte <= c.dte <= self._cfg.max_dte
        ]
        if not in_window:
            return None

        # Filter by minimum premium
        min_prem = Decimal(str(self._cfg.min_premium))
        liquid = [c for c in in_window if c.bid >= min_prem and c.spread_pct < 0.05]
        if not liquid:
            # Relax spread filter
            liquid = [c for c in in_window if c.bid >= min_prem]
        if not liquid:
            return None

        # Select closest to target delta
        target = self._cfg.target_delta  # negative (e.g. -0.28)
        return min(liquid, key=lambda c: abs(c.delta - target))

    def check_entry_conditions(
        self,
        symbol: str,
        underlying_price: float,
        iv_rank: float,
        trend_direction: str,
        available_cash: Decimal,
        contract: OptionContract,
    ) -> tuple[bool, str]:
        """
        Validate that all entry conditions are met before opening a CSP.

        Returns (approved, reason_if_rejected)
        """
        # IV Rank threshold
        if iv_rank < self._cfg.min_iv_rank:
            return False, f"IV Rank {iv_rank:.0f} < minimum {self._cfg.min_iv_rank}"

        # Underlying trend should be neutral-to-bullish (not bearish)
        if trend_direction == "downtrend":
            return False, "Underlying in downtrend — not ideal for CSP"

        # Cash requirement
        collateral = contract.strike * 100
        if available_cash < collateral:
            return False, f"Insufficient cash: need ${collateral:,.0f}, have ${available_cash:,.0f}"

        # Strike should be below current price (OTM put)
        if contract.strike >= Decimal(str(underlying_price)):
            return False, f"Strike {contract.strike} >= underlying {underlying_price:.2f} (not OTM)"

        return True, ""

    def should_close_early(
        self,
        position: CSPPosition,
        current_contract_price: Decimal,
        current_underlying: Decimal | None = None,
        dte: int | None = None,
    ) -> tuple[bool, str]:
        """
        Two-tier exit for short puts.
        Returns (should_close, rule_that_triggered).

        Exit rules (checked in order):
        1. Profit target reached (e.g. 50% of max profit)
        2. Tier 1 soft stop: mark >= 2.5× credit AND underlying < strike
           (avoids spurious exits from IV expansion when position is directionally fine)
        3. Tier 2 pain threshold: underlying < strike × pain_threshold
           (directional problem regardless of option price)
        4. DTE roll: DTE <= roll_when_dte
        """
        profit_pct = position.profit_pct(current_contract_price)

        # Profit target
        if profit_pct >= self._cfg.profit_target_pct:
            return True, f"profit_target: {profit_pct*100:.0f}% of max"

        # Tier 1: soft stop — mark >= 2.5× credit AND underlying is below strike
        soft_stop_threshold = position.premium_received * Decimal("2.5")
        if (
            current_underlying is not None
            and current_contract_price >= soft_stop_threshold
            and current_underlying < position.contract.strike
        ):
            return True, (
                f"soft_stop_2.5x: mark=${current_contract_price:.2f} >= "
                f"2.5x credit=${soft_stop_threshold:.2f}, "
                f"underlying=${current_underlying:.2f} < strike=${position.contract.strike:.2f}"
            )

        # Standalone mark stop: option price reached N× credit regardless of underlying direction
        # Catches pure IV-spike scenarios where Tier 1 (requires underlying < strike) won't fire
        mark_stop_mult = getattr(self._cfg, "mark_stop_multiplier", 3.0)
        if mark_stop_mult > 0:
            mark_stop_threshold = position.premium_received * Decimal(str(mark_stop_mult))
            if current_contract_price >= mark_stop_threshold:
                return True, (
                    f"mark_stop_{mark_stop_mult:.0f}x: mark=${current_contract_price:.2f} >= "
                    f"{mark_stop_mult:.0f}× credit=${mark_stop_threshold:.2f}"
                )

        # Tier 2: pain threshold — underlying dropped too far regardless of option price
        if current_underlying is not None:
            pain_pct = self._symbol_pain_thresholds.get(
                position.symbol, self._cfg.pain_threshold_default
            )
            pain_price = position.contract.strike * Decimal(str(pain_pct))
            if current_underlying < pain_price:
                return True, (
                    f"pain_threshold_{pain_pct:.0%}: "
                    f"underlying=${current_underlying:.2f} < "
                    f"strike*{pain_pct:.0%}=${pain_price:.2f}"
                )

        # DTE roll
        effective_dte = dte if dte is not None else position.contract.dte
        if effective_dte <= self._cfg.roll_when_dte:
            return True, f"dte_roll: {effective_dte} <= {self._cfg.roll_when_dte}"

        return False, ""

    def is_assigned(self, position: CSPPosition, underlying_price: Decimal) -> bool:
        """
        Returns True if the put is in-the-money at expiry (assignment likely).
        In practice, Alpaca auto-assigns ITM options at expiry.
        """
        return underlying_price < position.contract.strike and position.contract.dte == 0

    def cost_basis_after_assignment(self, position: CSPPosition) -> Decimal:
        """
        Effective cost basis of shares received on assignment.
        cost_basis = strike - premium_received_per_share
        """
        return position.contract.strike - position.premium_received
