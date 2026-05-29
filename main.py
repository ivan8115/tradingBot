#!/usr/bin/env python3
"""
TradingBot entry point.

Usage:
    python main.py fetch   --symbol AAPL [--days 365] [--timeframe 1Day]
    python main.py analyze --symbol AAPL [--days 365]
    python main.py account
    python main.py backtest --strategy momentum [--start 2023-01-01] [--end 2024-01-01]
    python main.py trade   --mode paper
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure project root is on path when running as script
sys.path.insert(0, str(Path(__file__).parent))

from core.logging import setup_logging
from core.config import settings


def cmd_fetch(args) -> None:
    """Fetch and display historical bars for a symbol."""
    from data.historical import HistoricalDataFetcher

    fetcher = HistoricalDataFetcher()
    df = fetcher.fetch_recent_bars(args.symbol, days=args.days, timeframe=args.timeframe)

    if df.empty:
        print(f"No data returned for {args.symbol}")
        return

    print(f"\n{args.symbol} — {args.timeframe} bars ({len(df)} rows)")
    print("-" * 70)
    display_cols = ["timestamp", "open", "high", "low", "close", "volume"]
    available = [c for c in display_cols if c in df.columns]
    print(df[available].tail(20).to_string(index=False))
    print(f"\nDate range: {df['timestamp'].min()} → {df['timestamp'].max()}")


def cmd_account(args) -> None:
    """Display current account info."""
    from broker.client import BrokerClient

    client = BrokerClient()
    acct = client.get_account()
    mode = "PAPER" if settings.alpaca_paper else "LIVE"

    print(f"\nAccount [{mode}]")
    print("-" * 40)
    print(f"  Cash:            ${acct.cash:,.2f}")
    print(f"  Equity:          ${acct.equity:,.2f}")
    print(f"  Portfolio Value: ${acct.portfolio_value:,.2f}")
    print(f"  Buying Power:    ${acct.buying_power:,.2f}")
    print(f"  PDT:             {acct.pattern_day_trader}")
    print(f"  Trading Blocked: {acct.trading_blocked}")

    client2 = BrokerClient()
    positions = client2.get_positions()
    if positions:
        print(f"\nOpen Positions ({len(positions)})")
        print("-" * 40)
        for p in positions:
            print(
                f"  {p.symbol:8s} {p.side:5s} {p.quantity:6d} shares  "
                f"avg=${p.avg_entry_price:.2f}  "
                f"pnl=${p.unrealized_pnl:+.2f}"
            )
    else:
        print("\nNo open positions.")


def cmd_analyze(args) -> None:
    """Fetch bars and run technical indicator analysis."""
    import math
    from data.historical import HistoricalDataFetcher
    from analysis.indicators import TechnicalIndicators
    from analysis.fibonacci import auto_fibonacci

    fetcher = HistoricalDataFetcher()
    df = fetcher.fetch_recent_bars(args.symbol, days=args.days, timeframe="1Day")

    if df.empty:
        print(f"No data returned for {args.symbol}")
        return

    ti = TechnicalIndicators()
    snap = ti.compute(df)
    fib = auto_fibonacci(
        df["high"].astype(float).tolist(),
        df["low"].astype(float).tolist(),
        df["close"].astype(float).tolist(),
        lookback=min(50, len(df)),
    )

    def fmt(v: float, decimals: int = 2) -> str:
        return f"{v:.{decimals}f}" if not math.isnan(v) else "N/A"

    print(f"\n{args.symbol} — Technical Analysis ({len(df)} bars)")
    print("=" * 55)
    print(f"  Close:         ${snap.close:.2f}")
    print()
    print("TREND")
    print(f"  EMA {ti.ema_short_period}/{ti.ema_long_period}:     {fmt(snap.ema_short)} / {fmt(snap.ema_long)}  ({'↑ bullish' if snap.ema_trend_up else '↓ bearish' if snap.ema_trend_up is False else 'N/A'})")
    print(f"  SMA 20/50/200: {fmt(snap.sma_20)} / {fmt(snap.sma_50)} / {fmt(snap.sma_200)}")
    print()
    print("MOMENTUM")
    print(f"  RSI (14):      {fmt(snap.rsi, 1)}  ({'overbought' if snap.rsi > 70 else 'oversold' if snap.rsi < 30 else 'neutral'})")
    print(f"  MACD:          {fmt(snap.macd, 3)}  signal={fmt(snap.macd_signal, 3)}  hist={fmt(snap.macd_hist, 3)}")
    print()
    print("VOLATILITY")
    print(f"  BB Upper:      ${fmt(snap.bb_upper)}")
    print(f"  BB Mid:        ${fmt(snap.bb_mid)}")
    print(f"  BB Lower:      ${fmt(snap.bb_lower)}")
    print(f"  BB Width:      {fmt(snap.bb_width * 100, 2)}%   BB%: {fmt(snap.bb_pct * 100, 1)}%")
    print(f"  ATR (14):      {fmt(snap.atr)}")
    print()
    print("VOLUME")
    print(f"  VWAP:          ${fmt(snap.vwap)}")
    print(f"  OBV:           {snap.obv:,.0f}" if not math.isnan(snap.obv) else "  OBV:           N/A")
    print(f"  Volume Ratio:  {fmt(snap.volume_ratio, 2)}x avg")
    print()
    print("FIBONACCI")
    print(f"  Swing High:    ${fib.swing_high:.2f}  Low: ${fib.swing_low:.2f}")
    support = fib.nearest_support(snap.close)
    resistance = fib.nearest_resistance(snap.close)
    if support:
        print(f"  Nearest Sup:   ${support.price:.2f} ({support.label})")
    if resistance:
        print(f"  Nearest Res:   ${resistance.price:.2f} ({resistance.label})")
    print()
    print("S/R LEVELS")
    print(f"  Pivot High:    ${snap.pivot_high:.2f}")
    print(f"  Pivot Low:     ${snap.pivot_low:.2f}")


def cmd_backtest(args) -> None:
    """Run a strategy backtest."""
    import asyncio
    from decimal import Decimal
    from data.historical import HistoricalDataFetcher
    from data.feed import BacktestDataFeed
    from backtest.broker_sim import SimulatedBroker
    from backtest.engine import BacktestEngine
    from risk.risk_manager import RiskManager
    from risk.position_sizer import PositionSizer

    cfg = settings.universe.backtest
    start_capital = Decimal(str(cfg.start_capital))

    # Resolve symbols for chosen strategy
    if args.strategy == "momentum":
        from strategies.momentum import MomentumStrategy
        symbols = settings.strategies.momentum.symbols or settings.universe.watchlist
        strategy = MomentumStrategy(symbols)
    elif args.strategy == "mean_reversion":
        print("mean_reversion strategy coming in Phase 6.")
        return
    elif args.strategy == "breakout":
        print("breakout strategy coming in Phase 6.")
        return
    elif args.strategy == "wheel":
        print("wheel strategy coming in Phase 5.")
        return
    else:
        print(f"Unknown strategy: {args.strategy}")
        return

    print(f"\nFetching historical data for {symbols}...")
    fetcher = HistoricalDataFetcher()
    bars = {}
    for sym in symbols:
        df = fetcher.fetch_bars(sym, start=args.start, end=args.end, timeframe="1Day")
        if not df.empty:
            bars[sym] = df
            print(f"  {sym}: {len(df)} bars")

    if not bars:
        print("No data fetched. Check your symbols and date range.")
        return

    feed = BacktestDataFeed(bars)
    broker_sim = SimulatedBroker(slippage_bps=5, commission_per_share=0.005)
    risk_mgr = RiskManager()
    sizer = PositionSizer()

    engine = BacktestEngine(
        strategy=strategy,
        feed=feed,
        broker_sim=broker_sim,
        risk_manager=risk_mgr,
        position_sizer=sizer,
        start_capital=start_capital,
    )

    print(f"\nRunning backtest: {args.strategy} | {args.start} → {args.end}")
    report = asyncio.run(engine.run())
    report.print_summary()

    if args.trades:
        report.print_trades()

    if args.export_csv:
        report.export_equity_csv(args.export_csv)


def cmd_trade(args) -> None:
    """Start live/paper trading."""
    import asyncio
    from broker.client import BrokerClient
    from database.migrations import init_db
    from scheduler.scheduler import TradingScheduler
    from strategies.momentum import MomentumStrategy

    # Override paper/live from CLI if specified
    if args.mode == "live" and settings.alpaca_paper:
        print("WARNING: config has ALPACA_PAPER=true but --mode live was passed.")
        print("Set ALPACA_PAPER=false in .env to trade live. Exiting.")
        return

    # Initialize database
    init_db(settings.system.db_path)

    broker = BrokerClient()
    acct = broker.get_account()
    if acct.trading_blocked or acct.account_blocked:
        print("ERROR: Account is blocked. Check your Alpaca account.")
        return

    mode = "PAPER" if settings.alpaca_paper else "LIVE"
    print(f"\nStarting trading bot [{mode}]")
    print(f"  Cash:     ${acct.cash:,.2f}")
    print(f"  Equity:   ${acct.equity:,.2f}")

    # Build active strategies
    strategies = []
    if settings.strategies.momentum.enabled:
        syms = settings.strategies.momentum.symbols or settings.universe.watchlist
        strategies.append(MomentumStrategy(syms))

    if settings.strategies.wheel.enabled:
        from strategies.wheel.wheel_strategy import WheelStrategy
        from ai.trading_advisor import advisor as ai_advisor
        wheel_syms = settings.strategies.wheel.symbols
        strategies.append(WheelStrategy(wheel_syms, advisor=ai_advisor))

    if settings.strategies.swing.enabled:
        from strategies.swing.swing_strategy import SwingStrategy
        from ai.trading_advisor import advisor as ai_advisor
        from data.earnings_calendar import earnings_calendar
        swing_syms = settings.strategies.swing.symbols
        strategies.append(SwingStrategy(swing_syms, advisor=ai_advisor,
                                        earnings_calendar=earnings_calendar))

    if not strategies:
        print("No strategies enabled. Check config.yaml.")
        return

    for s in strategies:
        print(f"  Strategy: {s.strategy_id} on {s.symbols}")

    scheduler = TradingScheduler(strategies=strategies, broker=broker)
    print("\nBot running. Press Ctrl+C to stop.\n")

    try:
        asyncio.run(scheduler.run())
    except KeyboardInterrupt:
        print("\nStopped.")


def main() -> None:
    parser = argparse.ArgumentParser(description="TradingBot CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # fetch
    p_fetch = subparsers.add_parser("fetch", help="Fetch historical bars")
    p_fetch.add_argument("--symbol", required=True)
    p_fetch.add_argument("--days", type=int, default=365)
    p_fetch.add_argument("--timeframe", default="1Day",
                         choices=["1Min", "5Min", "15Min", "30Min", "1Hour", "4Hour", "1Day"])
    p_fetch.set_defaults(func=cmd_fetch)

    # account
    p_acct = subparsers.add_parser("account", help="Show account info and positions")
    p_acct.set_defaults(func=cmd_account)

    # analyze
    p_analyze = subparsers.add_parser("analyze", help="Run technical analysis on a symbol")
    p_analyze.add_argument("--symbol", required=True)
    p_analyze.add_argument("--days", type=int, default=365)
    p_analyze.set_defaults(func=cmd_analyze)

    # backtest
    p_bt = subparsers.add_parser("backtest", help="Run a strategy backtest")
    p_bt.add_argument("--strategy", required=True, choices=["momentum", "mean_reversion", "breakout", "wheel"])
    p_bt.add_argument("--start", default=settings.universe.backtest.start_date)
    p_bt.add_argument("--end", default=settings.universe.backtest.end_date)
    p_bt.add_argument("--trades", action="store_true", help="Print trade log")
    p_bt.add_argument("--export-csv", metavar="PATH", help="Export equity curve to CSV")
    p_bt.set_defaults(func=cmd_backtest)

    # trade
    p_trade = subparsers.add_parser("trade", help="Start live or paper trading")
    p_trade.add_argument("--mode", choices=["paper", "live"], default="paper")
    p_trade.set_defaults(func=cmd_trade)

    args = parser.parse_args()
    setup_logging(log_level=settings.system.log_level)
    args.func(args)


if __name__ == "__main__":
    main()
