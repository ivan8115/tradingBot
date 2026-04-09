"""
Backtesting engine.
Wires BacktestDataFeed + SimulatedBroker + real Strategy classes.
The same Strategy class used in live trading runs here unchanged.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from loguru import logger

from backtest.broker_sim import SimulatedBroker
from backtest.report import BacktestReport
from core.events import BarEvent, FillEvent, OrderEvent, SignalEvent
from data.feed import BacktestDataFeed
from portfolio.portfolio import Portfolio
from portfolio.performance import calculate_performance
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager
from strategies.base import Strategy


class BacktestEngine:
    """
    Event-driven backtesting engine.

    Usage:
        engine = BacktestEngine(strategy, feed, broker_sim, ...)
        report = asyncio.run(engine.run())
        print(report)
    """

    def __init__(
        self,
        strategy: Strategy,
        feed: BacktestDataFeed,
        broker_sim: SimulatedBroker,
        risk_manager: RiskManager,
        position_sizer: PositionSizer,
        start_capital: Decimal = Decimal("100000"),
        # Strategy performance estimates for Kelly sizing
        assumed_win_rate: float = 0.55,
        assumed_win_loss_ratio: float = 1.5,
    ) -> None:
        self._strategy = strategy
        self._feed = feed
        self._broker = broker_sim
        self._risk = risk_manager
        self._sizer = position_sizer
        self._portfolio = Portfolio(cash=start_capital)
        self._win_rate = assumed_win_rate
        self._win_loss_ratio = assumed_win_loss_ratio

        # Track equity curve (one point per bar per symbol, simplified to daily)
        self._equity_snapshots: list[float] = []
        self._last_snapshot_date: str = ""
        self._current_prices: dict[str, Decimal] = {}

    async def run(self) -> BacktestReport:
        """Run the full backtest. Returns a BacktestReport."""
        logger.info(f"Backtest starting: {self._strategy}")

        self._strategy.on_start()
        self._risk.set_daily_start_value(self._portfolio)

        await self._feed.subscribe(
            symbols=self._strategy.symbols,
            handler=self._on_bar,
        )

        self._strategy.on_stop()
        logger.info("Backtest complete")

        return self._build_report()

    async def _on_bar(self, bar: BarEvent) -> None:
        """Process one bar: update prices, check fills, run strategy, risk-check signals."""
        self._current_prices[bar.symbol] = bar.close

        # 1. Fill any pending orders using this bar's OHLCV
        fills = self._broker.process_bar(bar)
        for fill in fills:
            self._portfolio.apply_fill(fill)
            self._strategy.on_fill(fill)
            logger.debug(
                f"  Fill: {fill.side} {fill.filled_qty}x {fill.symbol} "
                f"@ ${fill.fill_price} | pnl_running=${self._portfolio.realized_pnl:.2f}"
            )

        # 2. Run strategy logic
        signals = self._strategy.on_bar(bar)

        # 3. Risk-check and size each signal
        for signal in signals:
            price = self._current_prices.get(signal.symbol, bar.close)
            result = self._risk.validate_signal(signal, self._portfolio, price)

            if result.approved:
                qty = self._sizer.size_position(
                    signal=signal,
                    portfolio=self._portfolio,
                    current_price=price,
                    win_rate=self._win_rate,
                    avg_win_loss_ratio=self._win_loss_ratio,
                    atr=signal.metadata.get("atr"),
                )
                if qty > 0:
                    order = self._build_order(signal, qty)
                    self._broker.submit_order(order)

        # 4. Daily equity snapshot
        date_str = bar.timestamp.strftime("%Y-%m-%d")
        if date_str != self._last_snapshot_date:
            self._equity_snapshots.append(
                float(self._portfolio.total_value(self._current_prices))
            )
            self._last_snapshot_date = date_str

    def _build_order(self, signal: SignalEvent, qty: int) -> OrderEvent:
        return OrderEvent(
            order_id=str(uuid.uuid4()),
            signal_event=signal,
            order_type="market",
            quantity=qty,
            is_options=signal.signal_type in ("SELL_PUT", "BUY_TO_CLOSE_PUT", "SELL_CALL", "BUY_TO_CLOSE_CALL"),
            submitted_at=datetime.now(tz=timezone.utc),
        )

    def _build_report(self) -> BacktestReport:
        history = self._portfolio.trade_history

        # Pair buys and sells to compute per-trade P&L
        trade_pnls: list[float] = []
        holding_days: list[float] = []
        open_trades: dict[str, FillEvent] = {}

        for fill in history:
            sym = fill.symbol
            if fill.side == "buy":
                open_trades[sym] = fill
            elif fill.side == "sell" and sym in open_trades:
                entry = open_trades.pop(sym)
                pnl = float(
                    (fill.fill_price - entry.fill_price) * fill.filled_qty
                    - fill.commission - entry.commission
                )
                trade_pnls.append(pnl)
                if entry.filled_at and fill.filled_at:
                    days = (fill.filled_at - entry.filled_at).total_seconds() / 86400
                    holding_days.append(days)

        initial = float(self._portfolio._initial_cash)
        final = float(self._portfolio.total_value(self._current_prices))

        perf = calculate_performance(
            equity_curve=self._equity_snapshots,
            trade_pnls=trade_pnls,
            holding_days=holding_days,
            initial_capital=initial,
        )

        return BacktestReport(
            strategy_id=self._strategy.strategy_id,
            symbols=self._strategy.symbols,
            initial_capital=initial,
            final_value=final,
            performance=perf,
            equity_curve=self._equity_snapshots,
            trade_history=history,
        )
