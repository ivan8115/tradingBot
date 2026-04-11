"""
TradingScheduler — market-hours-aware job scheduler.
Uses APScheduler for time-based jobs and asyncio for the real-time event loop.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from broker.client import BrokerClient
from broker.market_data import MarketDataStream
from broker.portfolio import PortfolioTracker
from core.config import settings
from core.events import BarEvent, FillEvent
from data.historical import HistoricalDataFetcher
from execution.executor import Executor
from execution.order_builder import OrderBuilder
from monitoring.alerting import alerter
from portfolio.portfolio import Portfolio
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager
from strategies.base import Strategy
from strategies.wheel.wheel_strategy import WheelStrategy


class TradingScheduler:
    """
    Orchestrates all timed and event-driven trading activity.

    Architecture:
    - APScheduler fires pre-market, open, check, and close jobs
    - The Alpaca WebSocket pushes BarEvents and FillEvents into handlers
    - Strategy signals flow: bar → strategy → risk → executor → broker
    """

    def __init__(
        self,
        strategies: list[Strategy],
        broker: BrokerClient,
    ) -> None:
        self._strategies = strategies
        self._broker = broker

        # Build supporting components
        self._stream = MarketDataStream()
        self._tracker = PortfolioTracker(broker)
        self._portfolio = Portfolio(cash=broker.get_account().cash)
        self._risk = RiskManager()
        self._sizer = PositionSizer()
        self._order_builder = OrderBuilder()
        self._executor = Executor(broker, self._order_builder, settings.system.db_path)
        self._fetcher = HistoricalDataFetcher()

        self._scheduler = AsyncIOScheduler(timezone=settings.system.timezone)
        self._tz = pytz.timezone(settings.system.timezone)
        self._active_symbols: list[str] = list(
            {sym for s in strategies for sym in s.symbols}
        )
        self._wheel_strategies: list[WheelStrategy] = [
            s for s in strategies if isinstance(s, WheelStrategy)
        ]

    def setup(self) -> None:
        """Register all scheduled jobs."""
        cfg = settings.scheduler
        tz = settings.system.timezone

        # Pre-market
        self._scheduler.add_job(
            self._pre_market,
            CronTrigger(hour=cfg.pre_market_hour, minute=cfg.pre_market_minute,
                        day_of_week="mon-fri", timezone=tz),
            id="pre_market",
        )

        # Market open
        self._scheduler.add_job(
            self._on_market_open,
            CronTrigger(hour=9, minute=30, day_of_week="mon-fri", timezone=tz),
            id="market_open",
        )

        # Options position check every N minutes
        self._scheduler.add_job(
            self._check_options_positions,
            IntervalTrigger(minutes=cfg.options_check_interval_minutes),
            id="options_check",
        )

        # Options chain refresh every 15 minutes
        self._scheduler.add_job(
            self._refresh_options_chains,
            IntervalTrigger(minutes=15),
            id="options_chain_refresh",
        )

        # DTE warning check at 10 AM
        self._scheduler.add_job(
            self._check_dte_warnings,
            CronTrigger(hour=10, minute=0, day_of_week="mon-fri", timezone=tz),
            id="dte_check",
        )

        # Pre-close buffer
        pre_close_hour, pre_close_minute = self._calc_pre_close(cfg.pre_close_buffer_minutes)
        self._scheduler.add_job(
            self._pre_close,
            CronTrigger(hour=pre_close_hour, minute=pre_close_minute,
                        day_of_week="mon-fri", timezone=tz),
            id="pre_close",
        )

        # Market close
        self._scheduler.add_job(
            self._on_market_close,
            CronTrigger(hour=16, minute=0, day_of_week="mon-fri", timezone=tz),
            id="market_close",
        )

        logger.info("TradingScheduler configured")

    async def run(self) -> None:
        """Start the scheduler and block until interrupted."""
        self.setup()
        self._scheduler.start()
        logger.info("TradingScheduler started — waiting for market events")
        try:
            # Keep event loop alive
            while True:
                await asyncio.sleep(60)
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutdown signal received")
            await self.shutdown()

    async def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)
        await self._stream.stop()
        for strategy in self._strategies:
            strategy.on_stop()
        logger.info("TradingScheduler shut down cleanly")

    # ------------------------------------------------------------------
    # Scheduled jobs
    # ------------------------------------------------------------------

    async def _pre_market(self) -> None:
        logger.info("=== PRE-MARKET ROUTINE ===")
        if not self._is_trading_day():
            logger.info("Market holiday — skipping")
            return

        self._tracker.sync()
        # Sync cash from broker into internal portfolio
        self._portfolio = Portfolio(cash=self._tracker.cash)
        self._risk.set_daily_start_value(self._portfolio)

        logger.info(
            f"Account ready: cash=${self._tracker.cash:,.2f} "
            f"equity=${self._tracker.equity:,.2f}"
        )

    async def _on_market_open(self) -> None:
        if not self._is_market_open():
            logger.info("Market not open (holiday?)")
            return

        logger.info("=== MARKET OPEN ===")
        for strategy in self._strategies:
            strategy.on_start()

        # Start WebSocket streams
        asyncio.create_task(
            self._stream.start(
                symbols=self._active_symbols,
                bar_handler=self._on_bar,
                fill_handler=self._on_fill,
            )
        )

    async def _check_options_positions(self) -> None:
        if not self._is_market_open():
            return
        logger.debug("Options position check...")
        # Wheel strategy manages its own positions via on_bar —
        # this job is a safety net to trigger check signals manually if needed.

    async def _check_dte_warnings(self) -> None:
        """Log structured warnings for positions approaching roll threshold."""
        if not self._is_market_open():
            return
        for wheel in self._wheel_strategies:
            state = wheel.get_state()
            for symbol, data in state.items():
                wheel_state = data.get("state")
                if wheel_state not in ("csp_open", "cc_open"):
                    continue
                pos = wheel._positions.get(symbol)
                if not pos:
                    continue
                contract = None
                if pos.csp_position:
                    contract = pos.csp_position.contract
                elif pos.cc_position:
                    contract = pos.cc_position.contract
                if contract:
                    threshold = 7
                    if contract.dte <= threshold + 3:
                        logger.warning(
                            f"[DTE] {symbol} {wheel_state}: DTE={contract.dte} "
                            f"approaching roll threshold={threshold}"
                        )

    async def _pre_close(self) -> None:
        if not self._is_market_open():
            return
        logger.info("=== PRE-CLOSE ===")
        # Cancel all open limit orders to avoid post-market fills
        try:
            self._broker.cancel_all_orders()
        except Exception as e:
            logger.warning(f"Could not cancel orders at pre-close: {e}")

    async def _on_market_close(self) -> None:
        logger.info("=== MARKET CLOSE ===")
        await self._stream.stop()
        for strategy in self._strategies:
            strategy.on_stop()

        # Reconcile
        self._tracker.sync()
        self._tracker.reconcile_with_internal(self._portfolio.positions)

        summary = self._portfolio.summary()
        logger.info(
            f"EOD Summary | "
            f"P&L: ${summary['realized_pnl']:+,.2f} | "
            f"Total: ${summary['total_value']:,.2f} | "
            f"Drawdown: {summary['drawdown_pct']:.1f}%"
        )
        alerter.daily_summary_alert(summary)

    # ------------------------------------------------------------------
    # Event handlers (called by WebSocket stream)
    # ------------------------------------------------------------------

    async def _on_bar(self, bar: BarEvent) -> None:
        from decimal import Decimal
        self._portfolio._current_prices = getattr(self._portfolio, '_current_prices', {})
        self._portfolio._current_prices[bar.symbol] = bar.close  # type: ignore[attr-defined]

        for strategy in self._strategies:
            if bar.symbol not in strategy.symbols:
                continue
            try:
                signals = strategy.on_bar(bar)
            except Exception as e:
                logger.error(f"Strategy {strategy.strategy_id} error on bar: {e}")
                continue

            for signal in signals:
                result = self._risk.validate_signal(signal, self._portfolio, bar.close)
                if result.approved:
                    qty = self._sizer.size_position(
                        signal=signal,
                        portfolio=self._portfolio,
                        current_price=bar.close,
                        atr=signal.metadata.get("atr"),
                    )
                    await self._executor.execute_signal(
                        signal=signal,
                        quantity=qty,
                        current_price=bar.close,
                    )

    async def _on_fill(self, fill: FillEvent) -> None:
        logger.info(
            f"Fill received: {fill.side.upper()} {fill.filled_qty}x "
            f"{fill.symbol} @ ${fill.fill_price}"
        )
        self._portfolio.apply_fill(fill)
        self._executor.record_fill(fill)
        alerter.fill_alert(
            symbol=fill.symbol,
            side=fill.side,
            qty=fill.filled_qty,
            price=float(fill.fill_price),
            strategy=fill.strategy_id,
        )

        # Check drawdown after every fill
        dd = float(self._portfolio.drawdown())
        if dd >= settings.risk.max_drawdown_pct:
            alerter.drawdown_alert(dd * 100, settings.risk.max_drawdown_pct * 100)
            logger.critical(f"MAX DRAWDOWN BREACHED: {dd*100:.1f}% — halting new orders")

        for strategy in self._strategies:
            try:
                strategy.on_fill(fill)
            except Exception as e:
                logger.error(f"Strategy {strategy.strategy_id} fill error: {e}")

        self._save_strategy_state()

    async def _refresh_options_chains(self) -> None:
        """Fetch live options chain for each Wheel symbol and push to strategy."""
        if not self._is_market_open():
            return
        for wheel in self._wheel_strategies:
            for symbol in wheel.symbols:
                try:
                    chain = self._broker.get_options_chain(
                        symbol=symbol,
                        dte_min=21,
                        dte_max=45,
                        option_type="both",
                    )
                    wheel.update_options_chain(symbol, chain)
                    logger.debug(f"[Scheduler] Chain refreshed: {symbol} ({len(chain)} contracts)")
                except Exception as e:
                    logger.warning(f"[Scheduler] Chain refresh failed for {symbol}: {e}")

    def _save_strategy_state(self) -> None:
        """Persist all strategy states to data/strategy_state.json for the dashboard."""
        state: dict = {}
        for strategy in self._strategies:
            try:
                state[strategy.strategy_id] = strategy.get_state()
            except Exception as e:
                logger.warning(f"Could not save state for {strategy.strategy_id}: {e}")
        path = Path(settings.system.db_path).parent / "strategy_state.json"
        try:
            path.write_text(json.dumps(state, default=str))
        except Exception as e:
            logger.warning(f"Could not write strategy_state.json: {e}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_market_open(self) -> bool:
        try:
            return self._broker.is_market_open()
        except Exception:
            return False

    def _is_trading_day(self) -> bool:
        """True if today is a weekday (not accounting for holidays)."""
        return datetime.now(self._tz).weekday() < 5

    @staticmethod
    def _calc_pre_close(buffer_minutes: int) -> tuple[int, int]:
        """Return (hour, minute) for pre-close job."""
        close_minutes = 16 * 60  # 4:00 PM = 960 minutes
        pre_close = close_minutes - buffer_minutes
        return pre_close // 60, pre_close % 60
