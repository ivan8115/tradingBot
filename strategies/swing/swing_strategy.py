"""
SwingStrategy — Minervini Stage 2 stock swing trades.

Entry gates (all must pass):
  1. Sufficient bar history (min_bars_for_entry)
  2. Minervini Stage 2 classification
  3. EMA bullish (short > long) + MACD histogram positive + RSI 40–70
  4. Not already in position

Exits (any one triggers):
  1. Sad Panda (EMA crossback below)
  2. Stage 3 or Stage 4 detected
  3. Held > max_hold_bars (stale trade)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger

from analysis.indicators import is_sad_panda
from analysis.stage_analysis import Stage, classify_stage
from core.config import SwingStrategyConfig, settings
from core.events import BarEvent, FillEvent, SignalEvent
from strategies.base import Strategy

import pandas as pd


class SwingStrategy(Strategy):
    """
    Swing trades individual stocks using Minervini Stage 2 + momentum indicators.
    Trades equities only (no options).
    """

    strategy_id = "swing"

    def __init__(
        self,
        symbols: list[str],
        config: SwingStrategyConfig | None = None,
        advisor=None,
        earnings_calendar=None,
    ) -> None:
        super().__init__(symbols)
        self._cfg = config or settings.strategies.swing
        self._advisor = advisor
        self._earnings_calendar = earnings_calendar

        self._in_position: dict[str, bool] = {sym: False for sym in symbols}
        self._bars_held: dict[str, int] = {sym: 0 for sym in symbols}
        self._entry_stop_levels: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    def on_bar(self, bar: BarEvent) -> list[SignalEvent]:
        if bar.symbol not in self.symbols:
            return []

        snap = self._update_indicators(bar)
        prev = self._get_prev_snapshot(bar.symbol)
        sym = bar.symbol

        if not self._bars_available(sym, self._cfg.min_bars_for_entry) or prev is None:
            return []

        signals: list[SignalEvent] = []
        in_pos = self._in_position[sym]

        if in_pos:
            self._bars_held[sym] = self._bars_held.get(sym, 0) + 1
            signals.extend(self._check_exits(sym, snap, prev, bar))
        else:
            signals.extend(self._check_entry(sym, snap, prev, bar))

        return signals

    def on_fill(self, fill: FillEvent) -> None:
        if fill.strategy_id != self.strategy_id:
            return
        sym = fill.symbol
        if fill.side == "buy":
            self._in_position[sym] = True
            self._bars_held[sym] = 0
            stop = fill.metadata.get("stop_loss") if isinstance(fill.metadata, dict) else None
            if stop is not None:
                self._entry_stop_levels[sym] = float(stop)
            logger.info(f"[Swing] Position opened: LONG {sym} @ ${fill.fill_price}")
        elif fill.side == "sell":
            self._in_position[sym] = False
            self._bars_held[sym] = 0
            self._entry_stop_levels.pop(sym, None)
            logger.info(f"[Swing] Position closed: {sym} @ ${fill.fill_price}")

    # ------------------------------------------------------------------
    # Entry / exit logic
    # ------------------------------------------------------------------

    def _check_entry(self, sym: str, snap, prev, bar: BarEvent) -> list[SignalEvent]:
        # Gate 1: Minervini Stage 2
        df = pd.DataFrame(list(self._bar_windows[sym]))
        stage = classify_stage(df)
        if stage != Stage.STAGE_2:
            return []

        # Gate 2: EMA bullish (short > long)
        if snap.ema_trend_up is not True:
            return []

        # Gate 3: MACD histogram positive
        if snap.macd_hist <= 0:
            return []

        # Gate 4: RSI in valid entry zone (40–70)
        if not (40 <= snap.rsi <= 70):
            return []

        close = float(bar.close)
        atr = snap.atr if snap.atr and snap.atr > 0 else close * 0.02

        strength = self._compute_entry_strength(snap, stage)

        # Soft earnings gate: halve strength if earnings are near (< 30 days)
        if (
            self._earnings_calendar is not None
            and self._earnings_calendar.is_near_earnings(sym, min_days=30)
        ):
            logger.debug(f"[Swing] {sym} near earnings — reducing strength by 50%")
            strength *= 0.5

        logger.info(
            f"[Swing] ENTRY LONG {sym} | Stage={stage.value} "
            f"RSI={snap.rsi:.1f} MACD_hist={snap.macd_hist:.4f} "
            f"strength={strength:.2f}"
        )
        return [
            SignalEvent(
                strategy_id=self.strategy_id,
                symbol=sym,
                signal_type="ENTRY_LONG",
                strength=strength,
                timestamp=bar.timestamp,
                metadata={
                    "stage": stage.value,
                    "rsi": snap.rsi,
                    "macd_hist": snap.macd_hist,
                    "ema_short": snap.ema_short,
                    "ema_long": snap.ema_long,
                    "close": close,
                    "atr": atr,
                    "stop_loss": close - atr * self._cfg.atr_stop_mult,
                    "take_profit": close + atr * self._cfg.atr_target_mult,
                },
            )
        ]

    def _check_exits(self, sym: str, snap, prev, bar: BarEvent) -> list[SignalEvent]:
        close = float(bar.close)

        # Exit 0: Hard stop loss
        stop_level = self._entry_stop_levels.get(sym)
        if stop_level is not None and close <= stop_level:
            return self._exit_signal(sym, bar, f"stop_loss: close={close:.2f} <= stop={stop_level:.2f}")

        # Exit 1: Sad Panda — EMA crossback
        if is_sad_panda(snap, prev):
            return self._exit_signal(sym, bar, "EMA crossback (sad panda)")

        # Exit 2: Stage deterioration (Stage 3 or 4)
        df = pd.DataFrame(list(self._bar_windows[sym]))
        stage = classify_stage(df)
        if stage in (Stage.STAGE_3, Stage.STAGE_4):
            return self._exit_signal(sym, bar, f"Stage deteriorated to {stage.value}")

        # Exit 3: Max hold bars exceeded
        if self._bars_held.get(sym, 0) >= self._cfg.max_hold_bars:
            return self._exit_signal(sym, bar, f"Max hold bars ({self._cfg.max_hold_bars}) reached")

        return []

    def _exit_signal(self, sym: str, bar: BarEvent, reason: str) -> list[SignalEvent]:
        logger.info(f"[Swing] EXIT LONG {sym} | reason={reason}")
        return [
            SignalEvent(
                strategy_id=self.strategy_id,
                symbol=sym,
                signal_type="EXIT_LONG",
                strength=1.0,
                timestamp=bar.timestamp,
                metadata={"reason": reason, "close": float(bar.close)},
            )
        ]

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def get_state(self) -> dict[str, Any]:
        return {
            "in_position": self._in_position.copy(),
            "bars_held": self._bars_held.copy(),
            "entry_stop_levels": self._entry_stop_levels.copy(),
        }

    def load_state(self, state: dict[str, Any]) -> None:
        self._in_position.update(state.get("in_position", {}))
        self._bars_held.update(state.get("bars_held", {}))
        self._entry_stop_levels.update(state.get("entry_stop_levels", {}))

    # ------------------------------------------------------------------
    # Dynamic symbol list (called by scheduler on watchlist refresh)
    # ------------------------------------------------------------------

    def sync_symbols(self, new_symbols: list[str]) -> None:
        """Add new symbols; never removes symbols that have open positions."""
        for sym in new_symbols:
            if sym not in self.symbols:
                self.symbols.append(sym)
                self._bar_windows[sym] = __import__('collections').deque(
                    maxlen=settings.indicators.bar_window_size
                )
                self._prev_snapshots[sym] = None
                self._curr_snapshots[sym] = None
                self._in_position[sym] = False
                self._bars_held[sym] = 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _compute_entry_strength(self, snap, stage: Stage) -> float:
        score = 0.0
        # Stage 2 confirmed
        if stage == Stage.STAGE_2:
            score += 0.3
        # RSI in ideal zone (50–65 for momentum)
        if 50 <= snap.rsi <= 65:
            score += 0.3
        elif 40 <= snap.rsi < 50:
            score += 0.15
        # MACD histogram magnitude
        if snap.macd_hist > 0:
            score += min(0.2, snap.macd_hist * 10)
        # Volume confirmation
        if snap.volume_ratio >= 1.5:
            score += 0.2
        return min(1.0, score)
