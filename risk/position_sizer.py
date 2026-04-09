"""
Position sizing methods: Kelly Criterion, fixed-fraction, percent-equity.
All methods return the number of whole shares to trade.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Literal

from core.config import settings
from core.events import SignalEvent
from portfolio.portfolio import Portfolio


class PositionSizer:
    """
    Computes position sizes based on configured method.
    Risk manager approves the signal first; sizer determines how many shares.
    """

    def __init__(
        self,
        method: Literal["kelly", "fixed_fraction", "percent_equity"] | None = None,
        kelly_fraction: float | None = None,
        fixed_risk_pct: float | None = None,
    ) -> None:
        self._method = method or settings.risk.position_sizing_method
        self._kelly_fraction = kelly_fraction or settings.risk.kelly_fraction
        self._fixed_risk_pct = fixed_risk_pct or settings.risk.max_portfolio_risk_pct

    def size_position(
        self,
        signal: SignalEvent,
        portfolio: Portfolio,
        current_price: Decimal,
        win_rate: float = 0.55,
        avg_win_loss_ratio: float = 1.5,
        atr: float | None = None,
    ) -> int:
        """
        Calculate position size in shares.

        Args:
            signal: The approved signal to size
            portfolio: Current portfolio state (for cash/equity)
            current_price: Current market price of the symbol
            win_rate: Historical win rate of this strategy (for Kelly)
            avg_win_loss_ratio: Avg win / Avg loss (for Kelly)
            atr: ATR value (used for ATR-based stop loss calculation)

        Returns:
            Number of whole shares (0 if insufficient capital)
        """
        if current_price <= 0:
            return 0

        portfolio_value = float(portfolio.total_value())
        if portfolio_value <= 0:
            return 0

        method = self._method

        if method == "kelly":
            shares = self._kelly_size(
                portfolio_value, float(current_price),
                win_rate, avg_win_loss_ratio, signal.strength
            )
        elif method == "fixed_fraction":
            shares = self._fixed_fraction_size(
                portfolio_value, float(current_price),
                self._fixed_risk_pct, atr
            )
        else:  # percent_equity
            shares = self._percent_equity_size(
                portfolio_value, float(current_price),
                self._fixed_risk_pct
            )

        # Never exceed max single position limit
        max_position_value = portfolio_value * settings.risk.max_single_position_pct
        max_shares = int(max_position_value / float(current_price))
        shares = min(shares, max_shares)

        # Must have cash available
        required_cash = float(current_price) * shares
        available_cash = float(portfolio.cash)
        if required_cash > available_cash:
            shares = int(available_cash / float(current_price))

        return max(0, shares)

    def _kelly_size(
        self,
        portfolio_value: float,
        price: float,
        win_rate: float,
        win_loss_ratio: float,
        signal_strength: float,
    ) -> int:
        """
        Fractional Kelly Criterion.
        Kelly % = (bp - q) / b
          b = odds received (win/loss ratio)
          p = probability of winning
          q = probability of losing (1 - p)

        We then apply the kelly_fraction multiplier (e.g. 0.25 for quarter-Kelly)
        and signal_strength to scale the position.
        """
        b = win_loss_ratio
        p = win_rate
        q = 1.0 - p

        kelly_pct = (b * p - q) / b
        kelly_pct = max(0.0, kelly_pct)  # never go negative (no short signals here)

        # Scale by fractional Kelly and signal strength
        risk_pct = kelly_pct * self._kelly_fraction * signal_strength
        risk_pct = min(risk_pct, settings.risk.max_portfolio_risk_pct * 5)  # cap at 5× base risk

        risk_amount = portfolio_value * risk_pct
        return int(risk_amount / price)

    def _fixed_fraction_size(
        self,
        portfolio_value: float,
        price: float,
        risk_pct: float,
        atr: float | None,
    ) -> int:
        """
        Risk a fixed % of portfolio per trade.
        If ATR is provided, use ATR-based stop to determine share count
        so that a 2×ATR stop only loses risk_pct of portfolio.
        """
        risk_amount = portfolio_value * risk_pct
        if atr and atr > 0:
            # Shares = risk_amount / (2 × ATR stop distance)
            stop_distance = atr * 2.0
            shares = int(risk_amount / stop_distance)
        else:
            shares = int(risk_amount / price)
        return shares

    def _percent_equity_size(
        self,
        portfolio_value: float,
        price: float,
        pct: float,
    ) -> int:
        """Allocate a fixed % of total equity to this position."""
        allocation = portfolio_value * pct
        return int(allocation / price)

    def kelly_criterion_raw(
        self,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
    ) -> float:
        """
        Raw Kelly % (0.0–1.0).
        Useful for analysis and reporting.
        """
        if avg_loss == 0:
            return 0.0
        b = avg_win / abs(avg_loss)
        p = win_rate
        q = 1.0 - p
        kelly = (b * p - q) / b
        return max(0.0, kelly)
