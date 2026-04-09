"""
Mean Reversion Strategy: Bollinger Bands + RSI.

Entry (long):
  - Price touches or crosses below lower Bollinger Band
  - RSI oversold (< 30)
  - Price recovers back above lower band (confirmation)

Exit:
  - Price reaches BB midline (SMA 20)
  - OR RSI reaches 50 (mean)
  - OR Price touches upper band (full reversion)
"""

from __future__ import annotations

from loguru import logger

from analysis.indicators import is_rsi_oversold
from core.config import MeanReversionStrategyConfig, settings
from core.events import BarEvent, FillEvent, SignalEvent
from strategies.base import Strategy


class MeanReversionStrategy(Strategy):
    """
    Counter-trend strategy: buy dips that return to the mean.
    Best in ranging / sideways markets.
    """

    strategy_id = "mean_reversion"

    def __init__(
        self,
        symbols: list[str],
        config: MeanReversionStrategyConfig | None = None,
    ) -> None:
        super().__init__(symbols)
        cfg = config or settings.strategies.mean_reversion
        self._indicators.rsi_period = cfg.rsi_period
        self._rsi_period = cfg.rsi_period
        self._bb_period = cfg.bb_period
        self._bb_std = cfg.bb_std

        self._in_position: dict[str, bool] = {sym: False for sym in symbols}
        self._touched_lower_band: dict[str, bool] = {sym: False for sym in symbols}
        self._min_bars = cfg.bb_period + 10

    def on_bar(self, bar: BarEvent) -> list[SignalEvent]:
        if bar.symbol not in self.symbols:
            return []

        snap = self._update_indicators(bar)
        if not self._bars_available(bar.symbol, self._min_bars):
            return []

        sym = bar.symbol
        signals: list[SignalEvent] = []
        close = float(bar.close)

        if not self._in_position[sym]:
            # Track if price touched lower band (pre-condition)
            if not snap.bb_lower != snap.bb_lower:  # not NaN
                if close <= snap.bb_lower:
                    self._touched_lower_band[sym] = True

            # Entry: was below lower band, now RSI oversold and recovering
            if (
                self._touched_lower_band[sym]
                and is_rsi_oversold(snap, threshold=35.0)
                and close > snap.bb_lower  # recovering
            ):
                strength = max(0.3, 1.0 - snap.rsi / 30.0)
                logger.info(
                    f"[MeanRev] ENTRY LONG {sym} | "
                    f"RSI={snap.rsi:.1f} close={close:.2f} bb_lower={snap.bb_lower:.2f}"
                )
                signals.append(SignalEvent(
                    strategy_id=self.strategy_id,
                    symbol=sym,
                    signal_type="ENTRY_LONG",
                    strength=min(1.0, strength),
                    timestamp=bar.timestamp,
                    metadata={
                        "rsi": snap.rsi,
                        "bb_lower": snap.bb_lower,
                        "bb_mid": snap.bb_mid,
                        "close": close,
                        "atr": snap.atr,
                    },
                ))
                self._touched_lower_band[sym] = False
        else:
            # Exit: price reached midband or upper band
            if not snap.bb_mid != snap.bb_mid:  # not NaN
                if close >= snap.bb_mid or snap.rsi >= 50.0:
                    reason = "BB midline reached" if close >= snap.bb_mid else "RSI at mean (50)"
                    logger.info(f"[MeanRev] EXIT LONG {sym} | {reason}")
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

    def get_state(self) -> dict:
        return {
            "in_position": self._in_position.copy(),
            "touched_lower": self._touched_lower_band.copy(),
        }

    def load_state(self, state: dict) -> None:
        self._in_position.update(state.get("in_position", {}))
        self._touched_lower_band.update(state.get("touched_lower", {}))
