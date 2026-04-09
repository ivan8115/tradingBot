"""
Pattern recognition: support/resistance levels, trend structures,
and classical chart patterns (flags, breakouts).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd


class PatternType(str, Enum):
    BULL_FLAG = "bull_flag"
    BEAR_FLAG = "bear_flag"
    BREAKOUT_UP = "breakout_up"
    BREAKOUT_DOWN = "breakout_down"
    SUPPORT_BOUNCE = "support_bounce"
    RESISTANCE_REJECT = "resistance_reject"
    DOUBLE_BOTTOM = "double_bottom"
    DOUBLE_TOP = "double_top"


@dataclass
class SupportResistanceLevel:
    price: float
    strength: int           # number of times price touched/respected this level
    level_type: str         # "support" | "resistance" | "both"
    first_seen: int         # bar index
    last_seen: int          # bar index


@dataclass
class PatternResult:
    pattern: PatternType
    confidence: float       # 0.0–1.0
    description: str
    bar_index: int          # bar where pattern was detected


@dataclass
class PatternSnapshot:
    support_levels: list[SupportResistanceLevel] = field(default_factory=list)
    resistance_levels: list[SupportResistanceLevel] = field(default_factory=list)
    nearest_support: Optional[float] = None
    nearest_resistance: Optional[float] = None
    patterns: list[PatternResult] = field(default_factory=list)
    trend: Optional[str] = None   # "uptrend" | "downtrend" | "sideways"
    trend_strength: float = 0.0   # 0.0–1.0


class PatternRecognizer:
    """
    Identifies support/resistance levels, trends, and chart patterns
    from a DataFrame of OHLCV bars.
    """

    def __init__(
        self,
        sr_lookback: int = 50,
        sr_tolerance_pct: float = 0.005,    # 0.5% tolerance for level clustering
        sr_min_touches: int = 2,
        trend_lookback: int = 20,
        flag_min_bars: int = 5,
        flag_max_bars: int = 20,
        breakout_lookback: int = 20,
        volume_confirm_multiplier: float = 1.5,
    ) -> None:
        self.sr_lookback = sr_lookback
        self.sr_tolerance_pct = sr_tolerance_pct
        self.sr_min_touches = sr_min_touches
        self.trend_lookback = trend_lookback
        self.flag_min_bars = flag_min_bars
        self.flag_max_bars = flag_max_bars
        self.breakout_lookback = breakout_lookback
        self.volume_confirm_multiplier = volume_confirm_multiplier

    def analyze(self, df: pd.DataFrame) -> PatternSnapshot:
        """
        Full pattern analysis on the provided OHLCV DataFrame.
        Returns PatternSnapshot for the most recent bar.
        """
        if len(df) < max(self.sr_lookback, self.trend_lookback) // 2:
            return PatternSnapshot()

        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)
        current_price = float(close.iloc[-1])

        snap = PatternSnapshot()

        # Support / Resistance
        sr_levels = self._find_sr_levels(high, low, close)
        snap.support_levels = [l for l in sr_levels if l.level_type in ("support", "both")]
        snap.resistance_levels = [l for l in sr_levels if l.level_type in ("resistance", "both")]

        supports_below = [l.price for l in snap.support_levels if l.price < current_price]
        resistances_above = [l.price for l in snap.resistance_levels if l.price > current_price]
        snap.nearest_support = max(supports_below) if supports_below else None
        snap.nearest_resistance = min(resistances_above) if resistances_above else None

        # Trend
        snap.trend, snap.trend_strength = self._detect_trend(close, high, low)

        # Patterns
        patterns: list[PatternResult] = []
        patterns.extend(self._detect_breakout(close, high, low, volume))
        patterns.extend(self._detect_flags(close, high, low, volume, snap.trend))
        patterns.extend(self._detect_double_bottom(close, low))
        patterns.extend(self._detect_double_top(close, high))
        patterns.extend(self._detect_sr_reaction(close, sr_levels))
        snap.patterns = patterns

        return snap

    # ------------------------------------------------------------------
    # Support / Resistance
    # ------------------------------------------------------------------

    def _find_sr_levels(
        self,
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
    ) -> list[SupportResistanceLevel]:
        """
        Find S/R levels by clustering pivot highs and lows.
        A pivot high is a bar whose high is the highest in its neighborhood.
        """
        lookback = min(self.sr_lookback, len(close))
        h = high.iloc[-lookback:].values
        l = low.iloc[-lookback:].values
        n = len(h)

        # Find pivot highs and lows (simple: local max/min with window=3)
        pivot_highs = []
        pivot_lows = []
        window = 3
        for i in range(window, n - window):
            if h[i] == max(h[i - window:i + window + 1]):
                pivot_highs.append((i, h[i]))
            if l[i] == min(l[i - window:i + window + 1]):
                pivot_lows.append((i, l[i]))

        levels: list[SupportResistanceLevel] = []

        # Cluster pivot highs → resistance
        levels.extend(self._cluster_pivots(pivot_highs, "resistance", lookback, n))
        # Cluster pivot lows → support
        levels.extend(self._cluster_pivots(pivot_lows, "support", lookback, n))

        # Filter by min touches
        return [l for l in levels if l.strength >= self.sr_min_touches]

    def _cluster_pivots(
        self,
        pivots: list[tuple[int, float]],
        level_type: str,
        lookback: int,
        n: int,
    ) -> list[SupportResistanceLevel]:
        if not pivots:
            return []

        clusters: list[SupportResistanceLevel] = []
        for idx, price in pivots:
            # Try to merge into existing cluster
            merged = False
            for cluster in clusters:
                if abs(cluster.price - price) / cluster.price <= self.sr_tolerance_pct:
                    # Merge: update price to weighted average
                    total = cluster.strength + 1
                    cluster.price = (cluster.price * cluster.strength + price) / total
                    cluster.strength = total
                    cluster.last_seen = max(cluster.last_seen, idx)
                    merged = True
                    break
            if not merged:
                clusters.append(SupportResistanceLevel(
                    price=price,
                    strength=1,
                    level_type=level_type,
                    first_seen=idx,
                    last_seen=idx,
                ))

        return clusters

    # ------------------------------------------------------------------
    # Trend detection
    # ------------------------------------------------------------------

    def _detect_trend(
        self,
        close: pd.Series,
        high: pd.Series,
        low: pd.Series,
    ) -> tuple[str, float]:
        """
        Detect trend using higher-highs/higher-lows (uptrend)
        and lower-highs/lower-lows (downtrend).
        Returns (trend_type, strength 0–1).
        """
        lookback = min(self.trend_lookback, len(close))
        h = high.iloc[-lookback:].values
        l = low.iloc[-lookback:].values

        # Slope of close via linear regression
        x = np.arange(lookback)
        slope, _ = np.polyfit(x, close.iloc[-lookback:].values, 1)
        price_range = close.iloc[-lookback:].max() - close.iloc[-lookback:].min()
        if price_range == 0:
            return "sideways", 0.0

        normalized_slope = slope * lookback / price_range
        strength = min(abs(normalized_slope), 1.0)

        if normalized_slope > 0.1:
            return "uptrend", strength
        elif normalized_slope < -0.1:
            return "downtrend", strength
        return "sideways", strength

    # ------------------------------------------------------------------
    # Pattern detection
    # ------------------------------------------------------------------

    def _detect_breakout(
        self,
        close: pd.Series,
        high: pd.Series,
        low: pd.Series,
        volume: pd.Series,
    ) -> list[PatternResult]:
        """Volume-confirmed breakout above recent high or below recent low."""
        lookback = min(self.breakout_lookback, len(close) - 1)
        if lookback < 5:
            return []

        prev_high = float(high.iloc[-lookback - 1:-1].max())
        prev_low = float(low.iloc[-lookback - 1:-1].min())
        curr_close = float(close.iloc[-1])
        curr_vol = float(volume.iloc[-1])
        avg_vol = float(volume.iloc[-lookback - 1:-1].mean())

        results = []
        vol_confirmed = avg_vol > 0 and curr_vol >= avg_vol * self.volume_confirm_multiplier

        if curr_close > prev_high:
            confidence = 0.7 + (0.3 if vol_confirmed else 0.0)
            results.append(PatternResult(
                pattern=PatternType.BREAKOUT_UP,
                confidence=confidence,
                description=f"Breakout above {prev_high:.2f}" + (" with volume" if vol_confirmed else ""),
                bar_index=len(close) - 1,
            ))
        elif curr_close < prev_low:
            confidence = 0.7 + (0.3 if vol_confirmed else 0.0)
            results.append(PatternResult(
                pattern=PatternType.BREAKOUT_DOWN,
                confidence=confidence,
                description=f"Breakdown below {prev_low:.2f}" + (" with volume" if vol_confirmed else ""),
                bar_index=len(close) - 1,
            ))

        return results

    def _detect_flags(
        self,
        close: pd.Series,
        high: pd.Series,
        low: pd.Series,
        volume: pd.Series,
        trend: Optional[str],
    ) -> list[PatternResult]:
        """
        Bull/Bear flag: sharp move (pole) followed by tight consolidation.
        """
        if len(close) < self.flag_max_bars + 5:
            return []

        results = []
        # Look for consolidation in the last flag_min_bars–flag_max_bars bars
        for flag_len in range(self.flag_min_bars, min(self.flag_max_bars, len(close) - 5) + 1):
            flag_close = close.iloc[-(flag_len):].values
            flag_high = high.iloc[-(flag_len):].values
            flag_low = low.iloc[-(flag_len):].values

            flag_range = (max(flag_high) - min(flag_low))
            flag_mid = (max(flag_high) + min(flag_low)) / 2
            if flag_mid == 0:
                continue

            flag_range_pct = flag_range / flag_mid

            # Consolidation: tight range (< 5% range)
            if flag_range_pct > 0.05:
                continue

            # Pole: strong prior move
            pole_bars = max(5, flag_len // 2)
            if len(close) < flag_len + pole_bars:
                continue

            pole_close = close.iloc[-(flag_len + pole_bars):-flag_len].values
            pole_move = (pole_close[-1] - pole_close[0]) / pole_close[0] if pole_close[0] != 0 else 0

            if pole_move > 0.05 and trend != "downtrend":
                results.append(PatternResult(
                    pattern=PatternType.BULL_FLAG,
                    confidence=min(0.9, 0.6 + abs(pole_move) * 2),
                    description=f"Bull flag: {pole_move*100:.1f}% pole, {flag_range_pct*100:.1f}% consolidation",
                    bar_index=len(close) - 1,
                ))
                break
            elif pole_move < -0.05 and trend != "uptrend":
                results.append(PatternResult(
                    pattern=PatternType.BEAR_FLAG,
                    confidence=min(0.9, 0.6 + abs(pole_move) * 2),
                    description=f"Bear flag: {pole_move*100:.1f}% pole, {flag_range_pct*100:.1f}% consolidation",
                    bar_index=len(close) - 1,
                ))
                break

        return results

    def _detect_double_bottom(
        self, close: pd.Series, low: pd.Series
    ) -> list[PatternResult]:
        """Two similar lows with a recovery in between."""
        if len(low) < 20:
            return []

        lookback = min(40, len(low))
        l = low.iloc[-lookback:].values

        # Find two lowest points
        sorted_idx = np.argsort(l)
        if len(sorted_idx) < 2:
            return []

        idx1, idx2 = sorted(sorted_idx[:2])
        if idx2 - idx1 < 5:  # must be separated
            return []

        price1, price2 = l[idx1], l[idx2]
        if abs(price1 - price2) / max(price1, price2) > 0.03:  # within 3%
            return []

        # Check there's a rally between them
        peak_between = max(l[idx1:idx2 + 1])
        recovery_pct = (peak_between - min(price1, price2)) / min(price1, price2)
        if recovery_pct < 0.03:
            return []

        confidence = min(0.85, 0.5 + recovery_pct * 5)
        return [PatternResult(
            pattern=PatternType.DOUBLE_BOTTOM,
            confidence=confidence,
            description=f"Double bottom near {min(price1, price2):.2f}",
            bar_index=len(close) - 1,
        )]

    def _detect_double_top(
        self, close: pd.Series, high: pd.Series
    ) -> list[PatternResult]:
        """Two similar highs with a pullback in between."""
        if len(high) < 20:
            return []

        lookback = min(40, len(high))
        h = high.iloc[-lookback:].values

        sorted_idx = np.argsort(h)[::-1]
        if len(sorted_idx) < 2:
            return []

        idx1, idx2 = sorted(sorted_idx[:2])
        if idx2 - idx1 < 5:
            return []

        price1, price2 = h[idx1], h[idx2]
        if abs(price1 - price2) / max(price1, price2) > 0.03:
            return []

        trough_between = min(h[idx1:idx2 + 1])
        pullback_pct = (max(price1, price2) - trough_between) / max(price1, price2)
        if pullback_pct < 0.03:
            return []

        confidence = min(0.85, 0.5 + pullback_pct * 5)
        return [PatternResult(
            pattern=PatternType.DOUBLE_TOP,
            confidence=confidence,
            description=f"Double top near {max(price1, price2):.2f}",
            bar_index=len(close) - 1,
        )]

    def _detect_sr_reaction(
        self,
        close: pd.Series,
        levels: list[SupportResistanceLevel],
    ) -> list[PatternResult]:
        """Price bouncing off a known support or rejecting from resistance."""
        if len(close) < 3:
            return []

        curr = float(close.iloc[-1])
        prev = float(close.iloc[-2])
        results = []

        for level in levels:
            proximity = abs(curr - level.price) / level.price
            if proximity > 0.01:  # within 1%
                continue

            if level.level_type in ("support", "both") and curr > prev:
                results.append(PatternResult(
                    pattern=PatternType.SUPPORT_BOUNCE,
                    confidence=min(0.8, 0.4 + level.strength * 0.1),
                    description=f"Support bounce at {level.price:.2f} (touched {level.strength}x)",
                    bar_index=len(close) - 1,
                ))
            elif level.level_type in ("resistance", "both") and curr < prev:
                results.append(PatternResult(
                    pattern=PatternType.RESISTANCE_REJECT,
                    confidence=min(0.8, 0.4 + level.strength * 0.1),
                    description=f"Resistance rejection at {level.price:.2f} (touched {level.strength}x)",
                    bar_index=len(close) - 1,
                ))

        return results
