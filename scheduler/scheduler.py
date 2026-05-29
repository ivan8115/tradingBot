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

from ai.trading_advisor import PreMarketBriefing, advisor as _advisor
from ai.researcher import researcher as _researcher
from broker.client import BrokerClient
from broker.market_data import MarketDataStream
from broker.portfolio import PortfolioTracker
from core.config import settings
from core.decision_log import log_decision
from core.events import BarEvent, FillEvent
from data.earnings_calendar import earnings_calendar as _earnings_calendar
from database.migrations import get_session_factory
from database.models import Signal, Trade
from data.historical import HistoricalDataFetcher
from data.market_regime import MarketRegimeFilter
from data.watchlist_provider import WatchlistProvider
from execution.executor import Executor
from execution.order_builder import OrderBuilder
from monitoring.alerting import alerter
from portfolio.portfolio import Portfolio
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager
from strategies.base import Strategy
from strategies.swing.swing_strategy import SwingStrategy
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
        self._watchlist = WatchlistProvider()
        self._regime_filter = MarketRegimeFilter()

        self._scheduler = AsyncIOScheduler(timezone=settings.system.timezone)
        self._tz = pytz.timezone(settings.system.timezone)
        self._active_symbols: list[str] = list(
            {sym for s in strategies for sym in s.symbols}
        )
        self._wheel_strategies: list[WheelStrategy] = [
            s for s in strategies if isinstance(s, WheelStrategy)
        ]

        self._advisor = _advisor
        self._researcher = _researcher
        self._earnings_calendar = _earnings_calendar
        self._briefing: PreMarketBriefing | None = None
        self._briefing_posture: str = "normal"
        self._session_factory = get_session_factory(settings.system.db_path)
        self._swing_strategies: list[SwingStrategy] = [
            s for s in strategies if isinstance(s, SwingStrategy)
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

        # Watchlist refresh (pre-market, before trading starts)
        self._scheduler.add_job(
            self._refresh_watchlist,
            CronTrigger(
                hour=settings.watchlist.refresh_hour,
                minute=settings.watchlist.refresh_minute,
                day_of_week="mon-fri",
                timezone=tz,
            ),
            id="watchlist_refresh",
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

        # Friday weekly AI review (fires 5 min after market close)
        self._scheduler.add_job(
            self._weekly_review,
            CronTrigger(hour=16, minute=5, day_of_week="fri", timezone=tz),
            id="weekly_review",
        )

        # Midday thesis check at 12:00 PM ET
        self._scheduler.add_job(
            self._midday_thesis_check,
            CronTrigger(hour=12, minute=0, day_of_week="mon-fri", timezone=tz),
            id="midday_thesis",
        )

        # Pre-market gap-down risk scan at 8:15 AM
        self._scheduler.add_job(
            self._check_gap_downs,
            CronTrigger(hour=8, minute=15, day_of_week="mon-fri", timezone=tz),
            id="gap_down_check",
            replace_existing=True,
        )

        logger.info("TradingScheduler configured")

    async def run(self) -> None:
        """Start the scheduler and block until interrupted."""
        self.setup()
        self._load_strategy_state()
        self._scheduler.start()
        logger.info("TradingScheduler started — waiting for market events")

        if self._is_market_open():
            logger.info("Market already open on startup — resuming mid-session")
            await self._on_market_open()

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

        # Assess market regime from SPY/QQQ daily EMAs
        try:
            spy_df = self._fetcher.fetch_recent_bars("SPY", days=60, timeframe="1Day")
            qqq_df = self._fetcher.fetch_recent_bars("QQQ", days=60, timeframe="1Day")
            if not spy_df.empty and not qqq_df.empty:
                regime = self._regime_filter.get_regime(spy_df, qqq_df)
                self._risk.set_regime(regime)
                logger.info(f"Market regime: {regime.value.upper()}")
        except Exception as e:
            logger.warning(f"Regime check failed, defaulting to NEUTRAL: {e}")

        # Earnings calendar prefetch for all active symbols
        try:
            await self._earnings_calendar.prefetch(self._active_symbols)
        except Exception as e:
            logger.warning(f"[EarningsCalendar] prefetch failed: {e}")

        # Perplexity research
        research_context: dict = {}
        market_themes: list[str] = []
        if self._researcher._enabled:
            try:
                research_context = await self._researcher.research_symbols(self._active_symbols)
                market_themes = await self._researcher.get_market_themes()
                self._write_research_log(research_context, market_themes)
            except Exception as e:
                logger.warning(f"[Researcher] pre-market research failed: {e}")

        # AI pre-market briefing
        try:
            acct = self._tracker
            briefing = await self._advisor.pre_market_briefing(
                account={
                    "equity": float(acct.equity),
                    "cash": float(acct.cash),
                    "buying_power": float(getattr(acct, "buying_power", acct.cash)),
                },
                regime=self._risk._regime.value,
                active_symbols=self._active_symbols,
                open_positions=[
                    {"symbol": s, "state": p.state.value}
                    for wheel in self._wheel_strategies
                    for s, p in wheel._positions.items()
                ],
                research_context=research_context,
                earnings_context=dict(self._earnings_calendar._cache),
                market_themes=market_themes,
            )
            if briefing is not None:
                self._briefing = briefing
                self._briefing_posture = briefing.risk_posture
                from data.market_regime import Regime
                regime_map = {"bullish": Regime.BULLISH, "bearish": Regime.BEARISH}
                override = regime_map.get(briefing.suggested_regime.lower())
                if override and override != self._risk._regime:
                    logger.info(
                        f"[AI] Overriding regime: {self._risk._regime.value} → {override.value}"
                    )
                    self._risk.set_regime(override)
        except Exception as e:
            logger.warning(f"[AI] Pre-market briefing error: {e}")

        # Trigger watchlist refresh
        await self._refresh_watchlist()

    async def _refresh_watchlist(self) -> None:
        """Scan Finviz + QuiverQuant for today's Wheel candidates and sync strategies."""
        if not self._is_trading_day():
            return
        logger.info("=== WATCHLIST REFRESH ===")
        try:
            loop = asyncio.get_event_loop()
            symbols = await loop.run_in_executor(None, self._watchlist.refresh)
            if not symbols:
                logger.warning("[Watchlist] Refresh returned empty list — no symbol change")
                return

            for wheel in self._wheel_strategies:
                wheel.sync_symbols(symbols)

            for swing in self._swing_strategies:
                swing.sync_symbols(symbols)

            self._active_symbols = list(
                {sym for s in self._strategies for sym in s.symbols}
            )
            logger.info(f"[Watchlist] Active symbols updated: {self._active_symbols}")
        except Exception as exc:
            logger.error(f"[Watchlist] Refresh failed: {exc}")

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
                try:
                    wheel_state = data.get("state")
                    if wheel_state not in ("csp_open", "cc_open"):
                        continue
                    pos = wheel._positions.get(symbol)
                    if not pos:
                        continue
                    contract = None
                    if pos.csp_position:
                        contract = pos.csp_position.contract
                        threshold = wheel._cfg.csp.roll_when_dte
                    elif pos.cc_position:
                        contract = pos.cc_position.contract
                        threshold = wheel._cfg.cc.roll_when_dte
                    else:
                        threshold = wheel._cfg.csp.roll_when_dte
                    if contract:
                        if contract.dte <= threshold + 3:
                            logger.warning(
                                f"[DTE] {symbol} {wheel_state}: DTE={contract.dte} "
                                f"approaching roll threshold={threshold}"
                            )
                except Exception as e:
                    logger.warning(f"[DTE] Error checking DTE warning for {symbol}: {e}")

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

        # AI daily review
        try:
            trades_today = self._get_todays_trades()
            signals_today = self._get_todays_signals()
            review = await self._advisor.daily_review(trades_today, signals_today, summary)
            if review is not None:
                import json as _json
                from pathlib import Path as _Path
                reviews_dir = _Path(settings.system.db_path).parent / "reviews"
                reviews_dir.mkdir(exist_ok=True)
                review_path = reviews_dir / f"daily_{datetime.utcnow().date().isoformat()}.json"
                review_path.write_text(_json.dumps(review.model_dump(), indent=2))
                alerter.alert("daily_review", f"Daily Review [{review.grade}]: {review.summary}")
                logger.info(f"[AI] Daily review saved: {review_path}")
        except Exception as e:
            logger.warning(f"[AI] Daily review error: {e}")

    # ------------------------------------------------------------------
    # Event handlers (called by WebSocket stream)
    # ------------------------------------------------------------------

    async def _on_bar(self, bar: BarEvent) -> None:
        from decimal import Decimal
        self._portfolio._current_prices = getattr(self._portfolio, '_current_prices', {})
        self._portfolio._current_prices[bar.symbol] = bar.close  # type: ignore[attr-defined]

        # AI strike pre-selection for Wheel strategies in SCANNING state
        if self._advisor._enabled:
            from strategies.wheel.wheel_strategy import WheelState
            for wheel in self._wheel_strategies:
                if bar.symbol not in wheel._positions:
                    continue
                pos = wheel._positions[bar.symbol]
                if pos.state != WheelState.SCANNING or not pos.cached_chain:
                    continue
                if pos.ai_preferred_contract_id:
                    continue  # already pre-selected this bar cycle
                try:
                    from analysis.greeks import iv_rank as iv_rank_fn
                    current_iv = wheel._estimate_iv(wheel._get_snapshot(bar.symbol), bar)
                    iv_rank_val = iv_rank_fn(current_iv, pos.iv_history) if len(pos.iv_history) > 10 else 0.0
                    if iv_rank_val >= wheel._cfg.csp.min_iv_rank:
                        chain_dicts = [
                            {
                                "contract_id": c.contract_id,
                                "strike": float(c.strike),
                                "dte": c.dte,
                                "delta": c.delta,
                                "mid": float(c.mid),
                                "bid": float(c.bid) if hasattr(c, "bid") else None,
                                "ask": float(c.ask) if hasattr(c, "ask") else None,
                                "volume": getattr(c, "volume", None),
                                "open_interest": getattr(c, "open_interest", None),
                            }
                            for c in pos.cached_chain
                        ]
                        ai_strike = await self._advisor.select_csp_strike(
                            symbol=bar.symbol,
                            underlying_price=float(bar.close),
                            iv_rank=iv_rank_val,
                            chain=chain_dicts,
                            account={
                                "equity": float(self._tracker.equity),
                                "cash": float(self._tracker.cash),
                            },
                        )
                        if ai_strike is not None:
                            pos.ai_preferred_contract_id = ai_strike.contract_id
                except Exception as e:
                    logger.warning(f"[AI] Strike pre-selection failed for {bar.symbol}: {e}")

        market_context = {
            "regime": self._risk._regime.value,
            "drawdown_pct": float(self._portfolio.drawdown()),
            "daily_pnl_pct": self._get_daily_pnl_pct(),
            "risk_posture": self._briefing_posture,
            "open_positions": [
                {"symbol": sym, "side": pos.side, "qty": pos.quantity}
                for sym, pos in self._portfolio.positions.items()
            ],
        }

        for strategy in self._strategies:
            if bar.symbol not in strategy.symbols:
                continue
            try:
                signals = strategy.on_bar(bar)
            except Exception as e:
                logger.error(f"Strategy {strategy.strategy_id} error on bar: {e}")
                continue

            for signal in signals:
                # AI signal evaluation (fails open — passes to RiskManager if AI unavailable)
                ai_approved = True
                if self._advisor._enabled:
                    try:
                        eval_result = await self._advisor.evaluate_signal(signal, market_context)
                        if eval_result is not None:
                            # Find the wheel position for the chain snapshot (best effort)
                            wheel_pos = None
                            for _wheel in self._wheel_strategies:
                                if signal.symbol in _wheel._positions:
                                    wheel_pos = _wheel._positions[signal.symbol]
                                    break
                            try:
                                log_decision({
                                    "session_id": signal.metadata.get("session_id"),
                                    "stage": "scheduler/sonnet_eval",
                                    "symbol": signal.symbol,
                                    "signal_type": signal.signal_type,
                                    "ai_approved": eval_result.approved,
                                    "ai_confidence": getattr(eval_result, "confidence", None),
                                    "ai_reason": getattr(eval_result, "reasoning", None),
                                    "shadow_decision": {
                                        "approved": True,
                                        "reason": "mechanical_baseline_always_approves_generated_signals",
                                    },
                                    "options_chain_snapshot": [
                                        {
                                            "contract_id": c.contract_id,
                                            "strike": float(c.strike),
                                            "dte": c.dte,
                                            "bid": float(c.bid) if hasattr(c, "bid") else None,
                                            "ask": float(c.ask) if hasattr(c, "ask") else None,
                                            "delta": c.delta,
                                            "iv": getattr(c, "iv", None),
                                        }
                                        for c in (wheel_pos.cached_chain[:5] if wheel_pos else [])
                                    ],
                                })
                            except Exception as _log_exc:
                                logger.debug(f"[Scheduler] decision log write failed: {_log_exc}")

                            if not eval_result.approved:
                                signal.metadata["ai_rejected"] = True
                                signal.metadata["ai_reasoning"] = eval_result.reasoning
                                self._executor._save_signal(
                                    signal,
                                    approved=False,
                                    rejection_reason=f"AI: {eval_result.reasoning[:255]}",
                                )
                                ai_approved = False
                            else:
                                signal.strength = eval_result.adjusted_strength
                                signal.metadata["ai_approved"] = True
                                signal.metadata["ai_reasoning"] = eval_result.reasoning
                                signal.metadata["ai_confidence"] = eval_result.confidence
                    except Exception as e:
                        logger.warning(f"[AI] Signal eval error for {signal.symbol}: {e}")

                if not ai_approved:
                    continue

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

    async def _weekly_review(self) -> None:
        """Friday EOD: run AI weekly review and save to disk."""
        logger.info("=== WEEKLY AI REVIEW ===")
        try:
            from datetime import timedelta
            import json as _json
            from pathlib import Path as _Path
            from database.models import PortfolioSnapshot, WheelCycle

            today = datetime.utcnow().date()
            week_start = today - timedelta(days=7)
            with self._session_factory() as session:
                completed = session.query(WheelCycle).filter(WheelCycle.completed == True).all()  # noqa: E712
                active = session.query(WheelCycle).filter(WheelCycle.completed == False).all()  # noqa: E712
                month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                premium_this_month = float(sum(
                    (c.total_premium_collected or 0)
                    for c in (completed + active)
                    if c.started_at and c.started_at >= month_start
                ))
                csp_wins = sum(1 for c in completed if c.stock_cost_basis is None)
                csp_win_rate = csp_wins / len(completed) if completed else 0.0
                snapshots = session.query(PortfolioSnapshot).all()
                if snapshots:
                    peak = max(float(s.total_value) for s in snapshots)
                    latest = max(snapshots, key=lambda s: s.recorded_at)
                    current_drawdown = (peak - float(latest.total_value)) / peak if peak > 0 else 0.0
                else:
                    current_drawdown = 0.0

                trades_this_week = [
                    {"symbol": t.symbol, "side": t.side, "fill_price": float(t.fill_price),
                     "quantity": t.quantity, "filled_at": str(t.filled_at)}
                    for t in session.query(Trade).all()
                    if t.filled_at and t.filled_at.date() >= week_start
                ]

            metrics = {
                "premium_this_month": round(premium_this_month, 2),
                "csp_win_rate": round(csp_win_rate, 4),
                "cycles_completed": len(completed),
                "cycles_active": len(active),
                "current_drawdown_pct": round(current_drawdown, 4),
            }

            review = await self._advisor.weekly_review(metrics, trades_this_week)
            if review is not None:
                reviews_dir = _Path(settings.system.db_path).parent / "reviews"
                reviews_dir.mkdir(exist_ok=True)
                review_path = reviews_dir / f"weekly_{today.isoformat()}.json"
                review_path.write_text(_json.dumps(review.model_dump(), indent=2))
                alerter.alert(
                    "weekly_review",
                    f"Weekly Review [{review.week_grade}]: "
                    f"premium=${review.total_premium:.2f}, win_rate={review.win_rate:.0%}",
                )
                logger.info(f"[AI] Weekly review saved: {review_path}")
        except Exception as e:
            logger.warning(f"[AI] Weekly review error: {e}")

    async def _midday_thesis_check(self) -> None:
        """12 PM ET: ask Perplexity whether each open position's thesis still holds."""
        if not self._is_market_open() or not self._researcher._enabled:
            return
        logger.info("=== MIDDAY THESIS CHECK ===")
        for sym, pos in self._portfolio.positions.items():
            try:
                context = {
                    "state": pos.side,
                    "entry_price": float(pos.avg_entry_price) if hasattr(pos, "avg_entry_price") else "unknown",
                }
                thesis = await self._researcher.check_thesis(sym, context)
                if thesis:
                    logger.info(f"[Thesis] {sym}: {thesis}")
                    if any(kw in thesis.lower() for kw in (
                        "negative", "missed", "cut guidance", "regulatory", "insider selling", "broken"
                    )):
                        from monitoring.alerting import AlertLevel
                        alerter.alert("thesis_warning", f"[Thesis Warning] {sym}: {thesis[:200]}", level=AlertLevel.WARNING)
            except Exception as e:
                logger.warning(f"[Thesis] {sym} check failed: {e}")

    async def _check_gap_downs(self) -> None:
        """8:15 AM: scan open CSP positions for overnight gap-down risk."""
        logger.info("[Scheduler] Gap-down check starting")

        for strategy in self._strategies:
            if not hasattr(strategy, "get_open_csp_positions"):
                continue
            for symbol, wheel_pos in strategy.get_open_csp_positions().items():
                try:
                    csp = wheel_pos.csp_position
                    if csp is None or csp.underlying_price_at_entry is None:
                        continue

                    entry_price = float(csp.underlying_price_at_entry)
                    if entry_price <= 0:
                        continue

                    current_price = await self._get_current_price(symbol)
                    if current_price is None:
                        continue

                    pct_change = (current_price - entry_price) / entry_price

                    if pct_change <= -0.10:
                        msg = (
                            f"GAP-DOWN ALERT — {symbol}: down {pct_change:.1%} from "
                            f"entry ${entry_price:.2f} (now ${current_price:.2f}). "
                            f"Manual review required before trading."
                        )
                        logger.warning(f"[GapDown] {msg}")
                        try:
                            log_decision({
                                "stage": "gap_down_check",
                                "symbol": symbol,
                                "current_price": current_price,
                                "entry_price": entry_price,
                                "pct_change": round(pct_change, 4),
                                "flags": [f"down {pct_change:.1%} from entry"],
                                "action": "manual_review_flagged",
                            })
                        except Exception as log_exc:
                            logger.warning(f"[GapDown] Decision log write failed for {symbol}: {log_exc}")
                        try:
                            from monitoring.alerting import AlertLevel
                            alerter.alert(
                                "gap_down",
                                msg,
                                level=AlertLevel.WARNING,
                            )
                        except Exception as alert_exc:
                            logger.warning(f"[GapDown] Alert send failed for {symbol}: {alert_exc}")
                    else:
                        logger.debug(f"[GapDown] {symbol}: {pct_change:.1%} from entry — no flag")

                except Exception as exc:
                    logger.warning(f"[GapDown] Check failed for {symbol}: {exc}")

    async def _get_current_price(self, symbol: str) -> float | None:
        """Fetch the most recent closing price for a symbol via HistoricalDataFetcher."""
        # Uses daily close — won't catch intraday pre-market moves.
        # True pre-market detection requires Alpaca extended-hours quotes (see TODO.md).
        try:
            loop = asyncio.get_event_loop()
            df = await loop.run_in_executor(
                None,
                lambda: self._fetcher.fetch_recent_bars(symbol, days=2, timeframe="1Day"),
            )
            if df is None or df.empty:
                logger.warning(f"[GapDown] No price data returned for {symbol}")
                return None
            return float(df["close"].iloc[-1])
        except Exception as exc:
            logger.warning(f"[GapDown] Price fetch failed for {symbol}: {exc}")
            return None

    def _write_research_log(self, research: dict, themes: list[str]) -> None:
        """Append today's research summary to data/research_log.md (keep last 5 days)."""
        from datetime import date as _date
        log_path = Path(settings.system.db_path).parent / "research_log.md"
        today = _date.today().isoformat()

        lines = [f"\n## {today}"]
        if themes:
            lines.append(f"**Themes:** {', '.join(themes)}")
        for sym, summary in research.items():
            lines.append(f"**{sym}:** {summary}")

        try:
            existing = log_path.read_text() if log_path.exists() else ""
            # Prune: keep only last 4 dated sections so new one makes 5
            sections = existing.split("\n## ")
            if sections and sections[0] == "":
                sections = sections[1:]
            recent = sections[-4:] if len(sections) >= 4 else sections
            pruned = ("## " + "\n## ".join(recent)) if recent else ""
            log_path.write_text(pruned + "\n".join(lines) + "\n")
        except Exception as e:
            logger.warning(f"[Researcher] Could not write research log: {e}")

    def _get_daily_pnl_pct(self) -> float:
        if self._risk._daily_start_value is None:
            return 0.0
        start = float(self._risk._daily_start_value)
        if start == 0:
            return 0.0
        current = float(self._portfolio.total_value())
        return (current - start) / start

    def _get_todays_trades(self) -> list[dict]:
        today = datetime.utcnow().date()
        try:
            with self._session_factory() as session:
                return [
                    {"symbol": t.symbol, "side": t.side, "fill_price": float(t.fill_price),
                     "quantity": t.quantity, "strategy_id": t.strategy_id, "filled_at": str(t.filled_at)}
                    for t in session.query(Trade).all()
                    if t.filled_at and t.filled_at.date() == today
                ]
        except Exception:
            return []

    def _get_todays_signals(self) -> list[dict]:
        today = datetime.utcnow().date()
        try:
            with self._session_factory() as session:
                return [
                    {"symbol": s.symbol, "signal_type": s.signal_type,
                     "approved": s.approved, "rejection_reason": s.rejection_reason,
                     "generated_at": str(s.generated_at)}
                    for s in session.query(Signal).all()
                    if s.generated_at and s.generated_at.date() == today
                ]
        except Exception:
            return []

    def _load_strategy_state(self) -> None:
        """Restore Wheel strategy states from disk after a restart."""
        path = Path(settings.system.db_path).parent / "strategy_state.json"
        if not path.exists():
            logger.info("No strategy_state.json found — starting fresh")
            return
        try:
            saved = json.loads(path.read_text())
        except Exception as e:
            logger.warning(f"Could not read strategy_state.json: {e}")
            return
        for strategy in self._strategies:
            strategy_data = saved.get(strategy.strategy_id, {})
            if strategy_data:
                strategy.load_state(strategy_data)
                logger.info(f"Restored state for {strategy.strategy_id}")

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
