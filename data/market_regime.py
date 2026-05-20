"""
Market regime classification based on SPY and QQQ EMA alignment.
Classifies overall market condition as BULLISH, NEUTRAL, or BEARISH.
"""

from __future__ import annotations

from enum import Enum

import pandas as pd
import pandas_ta as ta


class Regime(Enum):
    BULLISH = "bullish"
    NEUTRAL = "neutral"
    BEARISH = "bearish"


class MarketRegimeFilter:
    """
    Scores SPY and QQQ on 9 EMA and 20 EMA alignment.

    Scoring per symbol (max 2 per symbol, 4 total):
      +1 if close > 9 EMA AND 9 EMA is rising (last bar > bar before)
      +1 if close > 20 EMA AND 20 EMA is rising

    Total score 4 → BULLISH, 0–1 → BEARISH, 2–3 → NEUTRAL.
    """

    def get_regime(self, spy_df: pd.DataFrame, qqq_df: pd.DataFrame) -> Regime:
        if spy_df.empty or qqq_df.empty:
            return Regime.NEUTRAL

        score = self._score_df(spy_df) + self._score_df(qqq_df)

        if score >= 4:
            return Regime.BULLISH
        if score <= 1:
            return Regime.BEARISH
        return Regime.NEUTRAL

    def _score_df(self, df: pd.DataFrame) -> int:
        """Return 0, 1, or 2 based on how aligned price is above rising EMAs."""
        if len(df) < 25:
            return 0

        close = df["close"].astype(float)
        last_close = float(close.iloc[-1])

        ema9 = ta.ema(close, length=9)
        ema20 = ta.ema(close, length=20)

        if ema9 is None or ema20 is None or len(ema9) < 2:
            return 0

        if pd.isna(ema9.iloc[-1]) or pd.isna(ema20.iloc[-1]):
            return 0

        last_ema9 = float(ema9.iloc[-1])
        prev_ema9 = float(ema9.iloc[-2])
        last_ema20 = float(ema20.iloc[-1])
        prev_ema20 = float(ema20.iloc[-2])

        score = 0
        if last_close > last_ema9 and last_ema9 > prev_ema9:
            score += 1
        if last_close > last_ema20 and last_ema20 > prev_ema20:
            score += 1
        return score
