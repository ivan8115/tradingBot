"""
Momentum Strategy: RSI + MACD + EMA crossover.

Entry (long):
  - EMA short crosses above EMA long (bullish trend)
  - MACD histogram turns positive (momentum confirmed)
  - RSI not overbought (< 70)

Exit:
  - EMA short crosses below EMA long (trend reversal)
  - OR RSI crosses into overbought territory (> 75)
  - OR MACD histogram turns negative
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from loguru import logger

from analysis.indicators import (
    is_ema_bearish_cross,
    is_ema_bullish_cross,
    is_macd_bearish_cross,
    is_macd_bullish_cross,
    is_rsi_overbought,
)
from core.config import MomentumStrategyConfig, settings
from core.events import BarEvent, FillEvent, SignalEvent
from strategies.base import Strategy


class MomentumStrategy(Strategy):
    """
    Trend-following strategy using RSI, MACD, and EMA crossovers.
    Trades equities only (no options).
    """

    strategy_id = "momentum"

    def __init__(
        self,
        symbols: list[str],
        config: MomentumStrategyConfig | None = None,
    ) -> None:
        super().__init__(symbols)
        cfg = config or settings.strategies.momentum

        # Override indicator periods from config
        self._indicators.rsi_period = cfg.rsi_period
        self._indicators.macd_fast = cfg.macd_fast
        self._indicators.macd_slow = cfg.macd_slow
        self._indicators.macd_signal_period = cfg.macd_signal
        self._indicators.ema_short_period = cfg.ema_short
        self._indicators.ema_long_period = cfg.ema_long

        self._rsi_overbought = cfg.rsi_overbought
        self._rsi_oversold = cfg.rsi_oversold

        # Track open positions per symbol (True = long)
        self._in_position: dict[str, bool] = {sym: False for sym in symbols}

        # Minimum bars before we start generating signals (needs enough for all indicators)
        self._min_bars = max(cfg.macd_slow + cfg.macd_signal + 5, cfg.ema_long + 5)

    def on_bar(self, bar: BarEvent) -> list[SignalEvent]:
        if bar.symbol not in self.symbols:
            return []

        snap = self._update_indicators(bar)
        prev = self._get_prev_snapshot(bar.symbol)

        if not self._bars_available(bar.symbol, self._min_bars) or prev is None:
            return []

        sym = bar.symbol
        signals: list[SignalEvent] = []
        in_pos = self._in_position[sym]

        if not in_pos:
            # --- Entry conditions ---
            ema_cross_up = is_ema_bullish_cross(snap, prev)
            macd_cross_up = is_macd_bullish_cross(snap, prev)
            rsi_ok = not is_rsi_overbought(snap, threshold=self._rsi_overbought)

            # Primary signal: EMA crossover confirmed by MACD
            if ema_cross_up and macd_cross_up and rsi_ok:
                strength = self._compute_entry_strength(snap)
                logger.info(
                    f"[Momentum] ENTRY LONG {sym} | "
                    f"RSI={snap.rsi:.1f} MACD_hist={snap.macd_hist:.4f} "
                    f"strength={strength:.2f}"
                )
                signals.append(SignalEvent(
                    strategy_id=self.strategy_id,
                    symbol=sym,
                    signal_type="ENTRY_LONG",
                    strength=strength,
                    timestamp=bar.timestamp,
                    metadata={
                        "rsi": snap.rsi,
                        "macd_hist": snap.macd_hist,
                        "ema_short": snap.ema_short,
                        "ema_long": snap.ema_long,
                        "close": float(bar.close),
                        "atr": snap.atr,
                        "stop_loss": float(bar.close) - snap.atr * 2.0,
                        "take_profit": float(bar.close) + snap.atr * 4.0,
                    },
                ))
        else:
            # --- Exit conditions ---
            ema_cross_down = is_ema_bearish_cross(snap, prev)
            macd_cross_down = is_macd_bearish_cross(snap, prev)
            rsi_extreme = is_rsi_overbought(snap, threshold=75.0)

            if ema_cross_down or macd_cross_down or rsi_extreme:
                reason = (
                    "EMA bearish cross" if ema_cross_down else
                    "MACD bearish cross" if macd_cross_down else
                    "RSI overbought"
                )
                logger.info(
                    f"[Momentum] EXIT LONG {sym} | reason={reason} | "
                    f"RSI={snap.rsi:.1f}"
                )
                signals.append(SignalEvent(
                    strategy_id=self.strategy_id,
                    symbol=sym,
                    signal_type="EXIT_LONG",
                    strength=1.0,
                    timestamp=bar.timestamp,
                    metadata={
                        "reason": reason,
                        "rsi": snap.rsi,
                        "close": float(bar.close),
                    },
                ))

        return signals

    def on_fill(self, fill: FillEvent) -> None:
        if fill.strategy_id != self.strategy_id:
            return
        sym = fill.symbol
        if fill.side == "buy":
            self._in_position[sym] = True
            logger.info(f"[Momentum] Position opened: LONG {sym} @ ${fill.fill_price}")
        elif fill.side == "sell":
            self._in_position[sym] = False
            logger.info(f"[Momentum] Position closed: {sym} @ ${fill.fill_price}")

    def get_state(self) -> dict:
        return {"in_position": self._in_position.copy()}

    def load_state(self, state: dict) -> None:
        self._in_position.update(state.get("in_position", {}))

    def _compute_entry_strength(self, snap) -> float:
        """
        Signal strength 0–1 based on confluence of indicators.
        Higher strength → larger position size via Kelly sizing.
        """
        score = 0.0
        # RSI in ideal zone (40–60 for trend continuation)
        if 40 <= snap.rsi <= 60:
            score += 0.3
        elif snap.rsi < 70:
            score += 0.15
        # MACD histogram magnitude (normalized to 0–0.3)
        if snap.macd_hist > 0:
            score += min(0.3, snap.macd_hist * 10)
        # EMA spread (short well above long = strong trend)
        if snap.ema_short > snap.ema_long:
            spread = (snap.ema_short - snap.ema_long) / snap.ema_long
            score += min(0.2, spread * 20)
        # Volume confirmation
        if snap.volume_ratio >= 1.5:
            score += 0.2

        return min(1.0, score)
