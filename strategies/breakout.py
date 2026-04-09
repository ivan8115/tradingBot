"""
Breakout Strategy: volume-confirmed price breakouts above recent highs.

Entry:
  - Price closes above the N-bar high (default 20 bars)
  - Volume is ≥ 1.5× the average volume (confirmation)
  - Not already in a position

Exit:
  - ATR-based trailing stop (2× ATR below the highest close since entry)
  - OR price closes back below the breakout level
"""

from __future__ import annotations

from loguru import logger

from analysis.indicators import is_volume_spike
from core.config import BreakoutStrategyConfig, settings
from core.events import BarEvent, FillEvent, SignalEvent
from strategies.base import Strategy


class BreakoutStrategy(Strategy):
    """
    Momentum breakout strategy for strong directional moves.
    Best in trending markets with clear price expansion.
    """

    strategy_id = "breakout"

    def __init__(
        self,
        symbols: list[str],
        config: BreakoutStrategyConfig | None = None,
    ) -> None:
        super().__init__(symbols)
        cfg = config or settings.strategies.breakout
        self._lookback = cfg.lookback_period
        self._vol_multiplier = cfg.volume_confirmation_multiplier

        self._in_position: dict[str, bool] = {sym: False for sym in symbols}
        self._entry_price: dict[str, float] = {sym: 0.0 for sym in symbols}
        self._highest_close: dict[str, float] = {sym: 0.0 for sym in symbols}
        self._breakout_level: dict[str, float] = {sym: 0.0 for sym in symbols}
        self._min_bars = self._lookback + 5

    def on_bar(self, bar: BarEvent) -> list[SignalEvent]:
        if bar.symbol not in self.symbols:
            return []

        snap = self._update_indicators(bar)
        if not self._bars_available(bar.symbol, self._min_bars):
            return []

        sym = bar.symbol
        close = float(bar.close)
        signals: list[SignalEvent] = []

        if not self._in_position[sym]:
            # Breakout level = highest high in the lookback window (excluding last bar)
            breakout_level = snap.pivot_high
            vol_spike = is_volume_spike(snap, multiplier=self._vol_multiplier)

            if close > breakout_level and vol_spike:
                logger.info(
                    f"[Breakout] ENTRY LONG {sym} | "
                    f"close={close:.2f} > level={breakout_level:.2f} | "
                    f"vol_ratio={snap.volume_ratio:.1f}×"
                )
                self._entry_price[sym] = close
                self._highest_close[sym] = close
                self._breakout_level[sym] = breakout_level

                signals.append(SignalEvent(
                    strategy_id=self.strategy_id,
                    symbol=sym,
                    signal_type="ENTRY_LONG",
                    strength=min(1.0, (snap.volume_ratio - 1.0) / 2.0),
                    timestamp=bar.timestamp,
                    metadata={
                        "breakout_level": breakout_level,
                        "volume_ratio": snap.volume_ratio,
                        "close": close,
                        "atr": snap.atr,
                    },
                ))
        else:
            # Update trailing stop high
            if close > self._highest_close[sym]:
                self._highest_close[sym] = close

            # Trailing stop: 2× ATR below the highest close since entry
            if snap.atr and snap.atr == snap.atr:  # not NaN
                trailing_stop = self._highest_close[sym] - (2.0 * snap.atr)
            else:
                trailing_stop = self._entry_price[sym] * 0.97  # 3% fallback

            # Exit: trailing stop hit or price closes back below breakout level
            failed_breakout = close < self._breakout_level[sym]
            stop_hit = close < trailing_stop

            if stop_hit or failed_breakout:
                reason = "Trailing stop" if stop_hit else "Failed breakout (closed below level)"
                logger.info(f"[Breakout] EXIT LONG {sym} | {reason}")
                signals.append(SignalEvent(
                    strategy_id=self.strategy_id,
                    symbol=sym,
                    signal_type="EXIT_LONG",
                    strength=1.0,
                    timestamp=bar.timestamp,
                    metadata={"reason": reason, "close": close},
                ))

        return signals

    def on_fill(self, fill: FillEvent) -> None:
        if fill.strategy_id != self.strategy_id:
            return
        if fill.side == "buy":
            self._in_position[fill.symbol] = True
        elif fill.side == "sell":
            self._in_position[fill.symbol] = False
            self._entry_price[fill.symbol] = 0.0
            self._highest_close[fill.symbol] = 0.0

    def get_state(self) -> dict:
        return {
            "in_position": self._in_position.copy(),
            "entry_price": self._entry_price.copy(),
            "highest_close": self._highest_close.copy(),
            "breakout_level": self._breakout_level.copy(),
        }

    def load_state(self, state: dict) -> None:
        self._in_position.update(state.get("in_position", {}))
        self._entry_price.update(state.get("entry_price", {}))
        self._highest_close.update(state.get("highest_close", {}))
        self._breakout_level.update(state.get("breakout_level", {}))
