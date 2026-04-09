"""
BacktestReport — collects results and renders a summary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from core.events import FillEvent
from portfolio.performance import PerformanceReport


@dataclass
class BacktestReport:
    strategy_id: str
    symbols: list[str]
    initial_capital: float
    final_value: float
    performance: PerformanceReport
    equity_curve: list[float] = field(default_factory=list)
    trade_history: list[FillEvent] = field(default_factory=list)

    def print_summary(self) -> None:
        print(f"\n{'='*55}")
        print(f"  BACKTEST RESULTS — {self.strategy_id.upper()}")
        print(f"  Symbols: {', '.join(self.symbols)}")
        print(f"{'='*55}")
        print(f"  Capital:    ${self.initial_capital:>12,.2f}  →  ${self.final_value:>12,.2f}")
        net = self.final_value - self.initial_capital
        pct = net / self.initial_capital * 100
        print(f"  Net P&L:    ${net:>+12,.2f}  ({pct:+.1f}%)")
        print(f"{'='*55}")
        print(self.performance)

    def print_trades(self, limit: int = 20) -> None:
        """Print the last N fills."""
        print(f"\nLast {limit} fills:")
        print(f"{'Time':<22} {'Symbol':<8} {'Side':<5} {'Qty':>6} {'Price':>10} {'Commission':>12}")
        print("-" * 70)
        for fill in self.trade_history[-limit:]:
            ts = fill.filled_at.strftime("%Y-%m-%d %H:%M") if fill.filled_at else "N/A"
            print(
                f"{ts:<22} {fill.symbol:<8} {fill.side:<5} "
                f"{fill.filled_qty:>6} ${float(fill.fill_price):>9.2f} "
                f"${float(fill.commission):>11.4f}"
            )

    def export_equity_csv(self, path: str) -> None:
        """Export equity curve to CSV for external charting."""
        import csv
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["day", "equity"])
            for i, val in enumerate(self.equity_curve):
                writer.writerow([i, f"{val:.2f}"])
        print(f"Equity curve exported to {path}")
