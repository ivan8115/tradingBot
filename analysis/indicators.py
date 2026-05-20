"""
Technical indicators computed on a rolling OHLCV DataFrame.
All functions accept a pandas DataFrame with columns:
  open, high, low, close, volume (float/Decimal-compatible)
and return a new column or Series.

Uses pandas-ta for most calculations — validated against known values in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

import numpy as np
import pandas as pd
import pandas_ta as ta


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass
class IndicatorSnapshot:
    """
    Point-in-time snapshot of all indicators for a single bar.
    All values are float (NaN if insufficient data).
    """

    # Trend
    ema_short: float = float("nan")
    ema_long: float = float("nan")
    sma_20: float = float("nan")
    sma_50: float = float("nan")
    sma_200: float = float("nan")
    ema_trend_up: Optional[bool] = None   # True if short EMA > long EMA

    # Momentum
    rsi: float = float("nan")
    macd: float = float("nan")
    macd_signal: float = float("nan")
    macd_hist: float = float("nan")
    macd_bullish: Optional[bool] = None   # True if histogram > 0

    # Volatility
    bb_upper: float = float("nan")
    bb_mid: float = float("nan")
    bb_lower: float = float("nan")
    bb_width: float = float("nan")        # (upper - lower) / mid
    bb_pct: float = float("nan")          # where price sits in band 0–1
    atr: float = float("nan")

    # Volume
    vwap: float = float("nan")
    obv: float = float("nan")
    obv_sma: float = float("nan")         # 20-bar SMA of OBV
    volume_sma: float = float("nan")      # 20-bar SMA of volume
    volume_ratio: float = float("nan")    # current volume / volume_sma

    # Price context
    close: float = float("nan")
    pivot_high: float = float("nan")      # highest high in lookback
    pivot_low: float = float("nan")       # lowest low in lookback


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------


class TechnicalIndicators:
    """
    Stateless indicator calculator.
    Pass a DataFrame of bars; get back an IndicatorSnapshot for the last bar.
    """

    def __init__(
        self,
        rsi_period: int = 14,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        bb_period: int = 20,
        bb_std: float = 2.0,
        atr_period: int = 14,
        ema_short: int = 9,
        ema_long: int = 21,
        obv_sma_period: int = 20,
        volume_sma_period: int = 20,
        pivot_lookback: int = 20,
    ) -> None:
        self.rsi_period = rsi_period
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal_period = macd_signal
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.atr_period = atr_period
        self.ema_short_period = ema_short
        self.ema_long_period = ema_long
        self.obv_sma_period = obv_sma_period
        self.volume_sma_period = volume_sma_period
        self.pivot_lookback = pivot_lookback

    def compute(self, df: pd.DataFrame) -> IndicatorSnapshot:
        """
        Compute all indicators on the provided DataFrame.
        Returns an IndicatorSnapshot for the last (most recent) bar.

        Args:
            df: DataFrame with at least: open, high, low, close, volume columns.
                Must have enough rows for the longest-period indicator (200 bars recommended).
        """
        if len(df) < 2:
            return IndicatorSnapshot()

        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)

        snap = IndicatorSnapshot(close=float(close.iloc[-1]))

        # --- EMA / SMA ---
        snap.ema_short = self._last(ta.ema(close, length=self.ema_short_period))
        snap.ema_long = self._last(ta.ema(close, length=self.ema_long_period))
        snap.sma_20 = self._last(ta.sma(close, length=20))
        snap.sma_50 = self._last(ta.sma(close, length=50))
        snap.sma_200 = self._last(ta.sma(close, length=200))
        if not np.isnan(snap.ema_short) and not np.isnan(snap.ema_long):
            snap.ema_trend_up = snap.ema_short > snap.ema_long

        # --- RSI ---
        snap.rsi = self._last(ta.rsi(close, length=self.rsi_period))

        # --- MACD ---
        macd_df = ta.macd(
            close,
            fast=self.macd_fast,
            slow=self.macd_slow,
            signal=self.macd_signal_period,
        )
        if macd_df is not None and not macd_df.empty:
            cols = macd_df.columns.tolist()
            snap.macd = self._last(macd_df[cols[0]])        # MACD line
            snap.macd_hist = self._last(macd_df[cols[1]])   # Histogram
            snap.macd_signal = self._last(macd_df[cols[2]]) # Signal line
            if not np.isnan(snap.macd_hist):
                snap.macd_bullish = snap.macd_hist > 0

        # --- Bollinger Bands ---
        bb = ta.bbands(close, length=self.bb_period, std=self.bb_std)
        if bb is not None and not bb.empty:
            cols = bb.columns.tolist()
            snap.bb_lower = self._last(bb[cols[0]])
            snap.bb_mid = self._last(bb[cols[1]])
            snap.bb_upper = self._last(bb[cols[2]])
            if not any(np.isnan([snap.bb_upper, snap.bb_lower, snap.bb_mid])):
                band_range = snap.bb_upper - snap.bb_lower
                snap.bb_width = band_range / snap.bb_mid if snap.bb_mid else float("nan")
                snap.bb_pct = (snap.close - snap.bb_lower) / band_range if band_range else float("nan")

        # --- ATR ---
        snap.atr = self._last(ta.atr(high, low, close, length=self.atr_period))

        # --- VWAP (intraday — resets on first bar if no cumulative state) ---
        if "vwap" in df.columns:
            snap.vwap = float(df["vwap"].iloc[-1]) if pd.notna(df["vwap"].iloc[-1]) else float("nan")
        else:
            snap.vwap = self._compute_vwap(high, low, close, volume)

        # --- OBV ---
        obv_series = ta.obv(close, volume)
        snap.obv = self._last(obv_series)
        if obv_series is not None:
            snap.obv_sma = self._last(ta.sma(obv_series, length=self.obv_sma_period))

        # --- Volume ---
        vol_sma = ta.sma(volume, length=self.volume_sma_period)
        snap.volume_sma = self._last(vol_sma)
        if not np.isnan(snap.volume_sma) and snap.volume_sma > 0:
            snap.volume_ratio = float(volume.iloc[-1]) / snap.volume_sma

        # --- Pivot high/low ---
        lookback = min(self.pivot_lookback, len(df))
        snap.pivot_high = float(high.iloc[-lookback:].max())
        snap.pivot_low = float(low.iloc[-lookback:].min())

        return snap

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _last(series: pd.Series | None) -> float:
        if series is None or series.empty:
            return float("nan")
        val = series.iloc[-1]
        return float(val) if pd.notna(val) else float("nan")

    @staticmethod
    def _compute_vwap(
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        volume: pd.Series,
    ) -> float:
        """Session VWAP = cumulative(typical_price * volume) / cumulative(volume)."""
        typical = (high + low + close) / 3.0
        cum_vol = volume.cumsum()
        cum_tp_vol = (typical * volume).cumsum()
        vwap_series = cum_tp_vol / cum_vol
        return float(vwap_series.iloc[-1]) if not vwap_series.empty else float("nan")


# ---------------------------------------------------------------------------
# Signal helpers (used by strategies)
# ---------------------------------------------------------------------------


def is_rsi_oversold(snap: IndicatorSnapshot, threshold: float = 30.0) -> bool:
    return not np.isnan(snap.rsi) and snap.rsi < threshold


def is_rsi_overbought(snap: IndicatorSnapshot, threshold: float = 70.0) -> bool:
    return not np.isnan(snap.rsi) and snap.rsi > threshold


def is_macd_bullish_cross(snap: IndicatorSnapshot, prev_snap: IndicatorSnapshot) -> bool:
    """MACD histogram crossed from negative to positive."""
    if np.isnan(snap.macd_hist) or np.isnan(prev_snap.macd_hist):
        return False
    return prev_snap.macd_hist < 0 and snap.macd_hist > 0


def is_macd_bearish_cross(snap: IndicatorSnapshot, prev_snap: IndicatorSnapshot) -> bool:
    if np.isnan(snap.macd_hist) or np.isnan(prev_snap.macd_hist):
        return False
    return prev_snap.macd_hist > 0 and snap.macd_hist < 0


def is_ema_bullish_cross(snap: IndicatorSnapshot, prev_snap: IndicatorSnapshot) -> bool:
    """Short EMA crossed above long EMA."""
    if snap.ema_trend_up is None or prev_snap.ema_trend_up is None:
        return False
    return not prev_snap.ema_trend_up and snap.ema_trend_up


def is_ema_bearish_cross(snap: IndicatorSnapshot, prev_snap: IndicatorSnapshot) -> bool:
    if snap.ema_trend_up is None or prev_snap.ema_trend_up is None:
        return False
    return prev_snap.ema_trend_up and not snap.ema_trend_up


def is_bb_squeeze(snap: IndicatorSnapshot, threshold: float = 0.02) -> bool:
    """Bollinger Band width below threshold signals a volatility squeeze."""
    return not np.isnan(snap.bb_width) and snap.bb_width < threshold


def is_volume_spike(snap: IndicatorSnapshot, multiplier: float = 1.5) -> bool:
    return not np.isnan(snap.volume_ratio) and snap.volume_ratio >= multiplier


def stop_loss_price(entry: float, atr: float, multiplier: float = 2.0, side: str = "long") -> float:
    """ATR-based stop loss distance."""
    if side == "long":
        return entry - (atr * multiplier)
    return entry + (atr * multiplier)


def is_happy_panda(snap: IndicatorSnapshot, prev_snap: IndicatorSnapshot | None) -> bool:
    """
    Bullish EMA crossback: 9 EMA was below 20 EMA and has now crossed back above.
    This is a bounce/continuation entry — 9 EMA recovers from a pullback.

    Cannot alias is_ema_bullish_cross: this function accepts prev_snap=None
    (callers pass None before enough bars exist), while is_ema_bullish_cross
    requires a non-None IndicatorSnapshot and would raise AttributeError.
    """
    if prev_snap is None or snap.ema_trend_up is None or prev_snap.ema_trend_up is None:
        return False
    return not prev_snap.ema_trend_up and snap.ema_trend_up


def is_sad_panda(snap: IndicatorSnapshot, prev_snap: IndicatorSnapshot | None) -> bool:
    """
    Bearish EMA crossback: 9 EMA was above 20 EMA and has now crossed back below.
    Use as an early exit signal — the rally failed.

    Cannot alias is_ema_bearish_cross: same reason as is_happy_panda — this
    function accepts prev_snap=None; is_ema_bearish_cross does not.
    """
    if prev_snap is None or snap.ema_trend_up is None or prev_snap.ema_trend_up is None:
        return False
    return prev_snap.ema_trend_up and not snap.ema_trend_up
