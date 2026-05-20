"""
Risk Manager — validates every signal before it becomes an order.
All signals must pass through here. If any check fails, the signal is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from loguru import logger

from core.config import settings
from core.events import SignalEvent
from data.market_regime import Regime
from portfolio.portfolio import Portfolio


@dataclass
class RiskCheck:
    name: str
    passed: bool
    reason: str = ""


@dataclass
class ValidationResult:
    approved: bool
    checks: list[RiskCheck] = field(default_factory=list)

    @property
    def rejection_reason(self) -> str | None:
        failed = [c for c in self.checks if not c.passed]
        return "; ".join(c.reason for c in failed) if failed else None


class RiskManager:
    """
    Gate between signal generation and order execution.
    Performs portfolio-level risk checks on every signal.
    """

    def __init__(
        self,
        max_drawdown_pct: float | None = None,
        max_single_position_pct: float | None = None,
        daily_loss_limit_pct: float | None = None,
        max_delta_exposure: int | None = None,
    ) -> None:
        cfg = settings.risk
        self._max_drawdown = max_drawdown_pct or cfg.max_drawdown_pct
        self._max_position_pct = max_single_position_pct or cfg.max_single_position_pct
        self._daily_loss_pct = daily_loss_limit_pct or cfg.daily_loss_limit_pct
        self._max_delta = max_delta_exposure or cfg.max_delta_exposure

        self._daily_start_value: Decimal | None = None
        self._net_portfolio_delta: float = 0.0
        self._regime: Regime = Regime.NEUTRAL

    def validate_signal(
        self,
        signal: SignalEvent,
        portfolio: Portfolio,
        current_price: Decimal | None = None,
    ) -> ValidationResult:
        """
        Run all risk checks. Returns ValidationResult with approved flag.
        Logs rejections for audit trail.
        """
        checks: list[RiskCheck] = []

        checks.append(self._check_drawdown(portfolio))
        checks.append(self._check_daily_loss(portfolio))
        checks.append(self._check_position_concentration(signal, portfolio, current_price))

        # Only check delta for options signals
        if signal.signal_type in ("SELL_PUT", "SELL_CALL", "BUY_TO_CLOSE_PUT", "BUY_TO_CLOSE_CALL"):
            checks.append(self._check_delta_exposure(signal))

        checks.append(self._check_regime(signal))

        if signal.signal_type in ("ENTRY_LONG", "ENTRY_SHORT"):
            checks.append(self._check_risk_reward(signal, current_price))

        approved = all(c.passed for c in checks)

        if not approved:
            failed = [c for c in checks if not c.passed]
            logger.warning(
                f"[RiskManager] Signal REJECTED: {signal.strategy_id} "
                f"{signal.signal_type} {signal.symbol} | "
                f"Reasons: {'; '.join(c.reason for c in failed)}"
            )
        else:
            logger.debug(
                f"[RiskManager] Signal APPROVED: {signal.strategy_id} "
                f"{signal.signal_type} {signal.symbol}"
            )

        return ValidationResult(approved=approved, checks=checks)

    def set_daily_start_value(self, portfolio: Portfolio) -> None:
        """Call at market open to record the starting value for daily loss tracking."""
        self._daily_start_value = portfolio.total_value()

    def update_delta_exposure(self, delta_change: float) -> None:
        """Called by options positions to update net portfolio delta."""
        self._net_portfolio_delta += delta_change

    def set_regime(self, regime: Regime) -> None:
        """Update current market regime. Called by scheduler pre-market."""
        self._regime = regime
        logger.info(f"[RiskManager] Market regime set to: {regime.value}")

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_drawdown(self, portfolio: Portfolio) -> RiskCheck:
        dd = float(portfolio.drawdown())
        if dd >= self._max_drawdown:
            return RiskCheck(
                name="max_drawdown",
                passed=False,
                reason=f"Max drawdown breached: {dd*100:.1f}% >= {self._max_drawdown*100:.1f}%",
            )
        return RiskCheck(name="max_drawdown", passed=True)

    def _check_daily_loss(self, portfolio: Portfolio) -> RiskCheck:
        if self._daily_start_value is None:
            return RiskCheck(name="daily_loss", passed=True)

        current = portfolio.total_value()
        daily_loss = float((self._daily_start_value - current) / self._daily_start_value)

        if daily_loss >= self._daily_loss_pct:
            return RiskCheck(
                name="daily_loss",
                passed=False,
                reason=f"Daily loss limit: {daily_loss*100:.1f}% >= {self._daily_loss_pct*100:.1f}%",
            )
        return RiskCheck(name="daily_loss", passed=True)

    def _check_position_concentration(
        self,
        signal: SignalEvent,
        portfolio: Portfolio,
        current_price: Decimal | None,
    ) -> RiskCheck:
        # Only check for entry signals
        if "EXIT" in signal.signal_type or "CLOSE" in signal.signal_type:
            return RiskCheck(name="concentration", passed=True)

        total = float(portfolio.total_value())
        if total == 0:
            return RiskCheck(name="concentration", passed=True)

        # Check if symbol already has a position at max size
        existing = portfolio.positions.get(signal.symbol)
        if existing and current_price:
            position_value = float(current_price) * abs(existing.quantity)
            concentration = position_value / total
            if concentration >= self._max_position_pct:
                return RiskCheck(
                    name="concentration",
                    passed=False,
                    reason=(
                        f"{signal.symbol} position {concentration*100:.1f}% >= "
                        f"max {self._max_position_pct*100:.1f}%"
                    ),
                )
        return RiskCheck(name="concentration", passed=True)

    def _check_delta_exposure(self, signal: SignalEvent) -> RiskCheck:
        new_delta = signal.metadata.get("delta", 0.0)
        projected = self._net_portfolio_delta + new_delta

        if abs(projected) > self._max_delta:
            return RiskCheck(
                name="delta_exposure",
                passed=False,
                reason=(
                    f"Net delta {projected:.0f} would exceed limit ±{self._max_delta}"
                ),
            )
        return RiskCheck(name="delta_exposure", passed=True)

    def _check_risk_reward(
        self, signal: SignalEvent, current_price: Decimal | None
    ) -> RiskCheck:
        """Require minimum 2:1 R:R for equity entry signals."""
        stop_loss = signal.metadata.get("stop_loss")
        take_profit = signal.metadata.get("take_profit")

        if stop_loss is None or take_profit is None:
            return RiskCheck(name="risk_reward", passed=True)

        entry = float(current_price) if current_price else signal.metadata.get("close", 0.0)
        if not entry:
            return RiskCheck(name="risk_reward", passed=True)

        if signal.signal_type == "ENTRY_SHORT":
            risk = float(stop_loss) - entry
            reward = entry - float(take_profit)
        else:
            risk = entry - float(stop_loss)
            reward = float(take_profit) - entry

        if risk <= 0:
            return RiskCheck(name="risk_reward", passed=True)

        rr = reward / risk
        if rr < 2.0:
            return RiskCheck(
                name="risk_reward",
                passed=False,
                reason=f"R:R {rr:.2f}x below minimum 2.0x (reward={reward:.2f}, risk={risk:.2f})",
            )
        return RiskCheck(name="risk_reward", passed=True)

    def _check_regime(self, signal: SignalEvent) -> RiskCheck:
        """Reject new entries when market regime is BEARISH."""
        entry_types = ("ENTRY_LONG", "ENTRY_SHORT", "SELL_PUT", "SELL_CALL")
        if signal.signal_type not in entry_types:
            return RiskCheck(name="regime", passed=True)
        if self._regime == Regime.BEARISH:
            return RiskCheck(
                name="regime",
                passed=False,
                reason=f"Market regime BEARISH: no new entries allowed",
            )
        return RiskCheck(name="regime", passed=True)
