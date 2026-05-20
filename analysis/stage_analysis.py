"""
Minervini Stage Analysis based on 150-day and 200-day SMAs.

Stage 2 (uptrend) is the primary target for Wheel strategy candidates.
Requires at least 200 bars of daily history to classify reliably.
"""

from __future__ import annotations

import math
from enum import Enum

import pandas as pd
import pandas_ta as ta


class Stage(Enum):
    STAGE_1 = "stage_1"   # Base / accumulation above SMA200
    STAGE_2 = "stage_2"   # Uptrend: price > SMA150 > SMA200, SMA200 rising
    STAGE_3 = "stage_3"   # Topping: SMA200 no longer rising
    STAGE_4 = "stage_4"   # Downtrend: price < SMA200, SMA200 declining
    UNKNOWN = "unknown"    # Insufficient data


def classify_stage(df: pd.DataFrame) -> Stage:
    """
    Classify a stock's Minervini stage from its daily bar DataFrame.

    Args:
        df: Daily bar DataFrame with a 'close' column and 200+ rows.

    Returns:
        Stage enum value.
    """
    if df.empty or len(df) < 200:
        return Stage.UNKNOWN

    close = df["close"].astype(float)
    sma150 = ta.sma(close, length=150)
    sma200 = ta.sma(close, length=200)

    if sma150 is None or sma200 is None:
        return Stage.UNKNOWN

    last_close = float(close.iloc[-1])
    last_sma150 = float(sma150.iloc[-1])
    last_sma200 = float(sma200.iloc[-1])
    prev_sma200 = float(sma200.iloc[-2])

    if any(math.isnan(v) for v in [last_close, last_sma150, last_sma200, prev_sma200]):
        return Stage.UNKNOWN

    sma200_rising = last_sma200 > prev_sma200

    # Stage 2: price > SMA150 > SMA200 and SMA200 is rising
    if last_close > last_sma150 > last_sma200 and sma200_rising:
        return Stage.STAGE_2

    # Stage 4: price below SMA200 and SMA200 declining
    if last_close < last_sma200 and not sma200_rising:
        return Stage.STAGE_4

    # Stage 3: price above SMAs in order but SMA200 stalling/declining (topping)
    if last_close > last_sma150 and last_sma150 > last_sma200:
        return Stage.STAGE_3

    # Stage 1: price above SMA200 but below SMA150 (basing)
    if last_close > last_sma200:
        return Stage.STAGE_1

    # price < SMA200 but SMA200 still rising — ambiguous transition, not yet Stage 4
    return Stage.UNKNOWN
