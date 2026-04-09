"""
Performance metrics: Sharpe, Sortino, max drawdown, CAGR, win rate.
Computed from a list of FillEvents and equity curve snapshots.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

import numpy as np
import pandas as pd


@dataclass
class PerformanceReport:
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float             # 0.0–1.0
    avg_win: float              # average winning trade P&L
    avg_loss: float             # average losing trade P&L (negative)
    profit_factor: float        # gross profit / gross loss
    total_pnl: float
    total_pnl_pct: float
    cagr: float                 # Compound Annual Growth Rate
    sharpe: float               # Annualized Sharpe ratio (Rf=0)
    sortino: float              # Annualized Sortino ratio
    max_drawdown: float         # Maximum drawdown as fraction (e.g. 0.15 = 15%)
    max_drawdown_pct: float     # same as percentage
    calmar: float               # CAGR / Max Drawdown
    avg_holding_days: float

    def __str__(self) -> str:
        return (
            f"Performance Report\n"
            f"{'='*45}\n"
            f"  Trades:        {self.total_trades}  "
            f"({self.winning_trades}W / {self.losing_trades}L)\n"
            f"  Win Rate:      {self.win_rate*100:.1f}%\n"
            f"  Avg Win:       ${self.avg_win:.2f}\n"
            f"  Avg Loss:      ${self.avg_loss:.2f}\n"
            f"  Profit Factor: {self.profit_factor:.2f}\n"
            f"  Total P&L:     ${self.total_pnl:.2f} ({self.total_pnl_pct:.1f}%)\n"
            f"  CAGR:          {self.cagr*100:.1f}%\n"
            f"  Sharpe:        {self.sharpe:.2f}\n"
            f"  Sortino:       {self.sortino:.2f}\n"
            f"  Max Drawdown:  {self.max_drawdown_pct:.1f}%\n"
            f"  Calmar:        {self.calmar:.2f}\n"
            f"  Avg Holding:   {self.avg_holding_days:.1f} days\n"
        )


def calculate_performance(
    equity_curve: list[float],
    trade_pnls: list[float],
    holding_days: list[float] | None = None,
    initial_capital: float = 100_000.0,
    trading_days_per_year: int = 252,
) -> PerformanceReport:
    """
    Calculate comprehensive performance metrics.

    Args:
        equity_curve: List of portfolio total values over time (daily snapshots)
        trade_pnls: List of realized P&L per closed trade
        holding_days: Optional list of holding period per trade in days
        initial_capital: Starting capital
        trading_days_per_year: 252 for stocks

    Returns:
        PerformanceReport with all metrics
    """
    eq = np.array(equity_curve, dtype=float)
    pnls = np.array(trade_pnls, dtype=float)

    # --- Trade stats ---
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    total_trades = len(pnls)
    winning = len(wins)
    losing = len(losses)
    win_rate = winning / total_trades if total_trades > 0 else 0.0
    avg_win = float(wins.mean()) if len(wins) > 0 else 0.0
    avg_loss = float(losses.mean()) if len(losses) > 0 else 0.0
    gross_profit = float(wins.sum()) if len(wins) > 0 else 0.0
    gross_loss = abs(float(losses.sum())) if len(losses) > 0 else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # --- P&L ---
    total_pnl = float(pnls.sum())
    total_pnl_pct = total_pnl / initial_capital * 100 if initial_capital > 0 else 0.0

    # --- CAGR ---
    n_years = len(eq) / trading_days_per_year if len(eq) > 0 else 1.0
    final_value = eq[-1] if len(eq) > 0 else initial_capital
    cagr = (final_value / initial_capital) ** (1.0 / n_years) - 1.0 if n_years > 0 else 0.0

    # --- Daily returns ---
    daily_returns = np.diff(eq) / eq[:-1] if len(eq) > 1 else np.array([0.0])

    # --- Sharpe (annualized, Rf=0) ---
    if len(daily_returns) > 1 and daily_returns.std() > 0:
        sharpe = (daily_returns.mean() / daily_returns.std()) * math.sqrt(trading_days_per_year)
    else:
        sharpe = 0.0

    # --- Sortino (downside deviation only) ---
    downside = daily_returns[daily_returns < 0]
    if len(downside) > 1 and downside.std() > 0:
        sortino = (daily_returns.mean() / downside.std()) * math.sqrt(trading_days_per_year)
    else:
        sortino = 0.0

    # --- Max Drawdown ---
    if len(eq) > 0:
        peak = np.maximum.accumulate(eq)
        drawdowns = (peak - eq) / peak
        max_dd = float(drawdowns.max())
    else:
        max_dd = 0.0

    # --- Calmar ---
    calmar = cagr / max_dd if max_dd > 0 else float("inf")

    # --- Avg holding period ---
    avg_holding = float(np.mean(holding_days)) if holding_days else 0.0

    return PerformanceReport(
        total_trades=total_trades,
        winning_trades=winning,
        losing_trades=losing,
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        profit_factor=profit_factor,
        total_pnl=total_pnl,
        total_pnl_pct=total_pnl_pct,
        cagr=cagr,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=max_dd,
        max_drawdown_pct=max_dd * 100,
        calmar=calmar,
        avg_holding_days=avg_holding,
    )
