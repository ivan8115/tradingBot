"""Unit tests for technical indicators — deterministic on known data."""

import math

import numpy as np
import pandas as pd
import pytest

from analysis.indicators import (
    IndicatorSnapshot,
    TechnicalIndicators,
    is_macd_bullish_cross,
    is_rsi_overbought,
    is_rsi_oversold,
    stop_loss_price,
)
from analysis.greeks import (
    IVResult,
    bsm_price,
    calculate_greeks,
    calculate_iv,
    iv_rank,
)
from analysis.fibonacci import auto_fibonacci, calculate_retracements


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_trending_up_df(n: int = 100, base: float = 100.0, step: float = 0.5) -> pd.DataFrame:
    """Steadily rising price series."""
    closes = [base + i * step for i in range(n)]
    return pd.DataFrame({
        "open": [c - 0.2 for c in closes],
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [1_000_000] * n,
    })


def make_flat_df(n: int = 100, price: float = 150.0) -> pd.DataFrame:
    return pd.DataFrame({
        "open": [price] * n,
        "high": [price + 1.0] * n,
        "low": [price - 1.0] * n,
        "close": [price] * n,
        "volume": [500_000] * n,
    })


# ---------------------------------------------------------------------------
# TechnicalIndicators tests
# ---------------------------------------------------------------------------


class TestTechnicalIndicators:
    def setup_method(self):
        self.ti = TechnicalIndicators()

    def test_returns_snapshot_insufficient_data(self):
        df = make_flat_df(n=1)
        snap = self.ti.compute(df)
        assert isinstance(snap, IndicatorSnapshot)
        assert math.isnan(snap.rsi)

    def test_rsi_in_range(self):
        df = make_trending_up_df(n=60)
        snap = self.ti.compute(df)
        assert not math.isnan(snap.rsi)
        assert 0.0 <= snap.rsi <= 100.0

    def test_rsi_overbought_on_strong_uptrend(self):
        df = make_trending_up_df(n=60, step=1.0)
        snap = self.ti.compute(df)
        # Strong uptrend should push RSI toward overbought
        assert snap.rsi > 50.0

    def test_macd_values_present(self):
        df = make_trending_up_df(n=60)
        snap = self.ti.compute(df)
        assert not math.isnan(snap.macd)
        assert not math.isnan(snap.macd_signal)
        assert not math.isnan(snap.macd_hist)

    def test_bb_values_present(self):
        df = make_flat_df(n=60)
        snap = self.ti.compute(df)
        assert not math.isnan(snap.bb_upper)
        assert not math.isnan(snap.bb_lower)
        assert not math.isnan(snap.bb_mid)
        assert snap.bb_upper > snap.bb_mid > snap.bb_lower

    def test_bb_pct_at_midpoint_for_flat_price(self):
        df = make_flat_df(n=60)
        snap = self.ti.compute(df)
        # Flat price sits exactly at the midband → bb_pct ≈ 0.5
        assert not math.isnan(snap.bb_pct)
        assert abs(snap.bb_pct - 0.5) < 0.1

    def test_ema_trend_up_on_uptrend(self):
        df = make_trending_up_df(n=60)
        snap = self.ti.compute(df)
        assert snap.ema_trend_up is True

    def test_atr_positive(self):
        df = make_trending_up_df(n=30)
        snap = self.ti.compute(df)
        assert not math.isnan(snap.atr)
        assert snap.atr > 0

    def test_pivot_high_low(self):
        df = make_trending_up_df(n=30)
        snap = self.ti.compute(df)
        assert snap.pivot_high >= snap.close
        assert snap.pivot_low <= snap.close


class TestSignalHelpers:
    def test_rsi_oversold(self):
        snap = IndicatorSnapshot(rsi=25.0)
        assert is_rsi_oversold(snap, threshold=30.0)
        assert not is_rsi_oversold(snap, threshold=20.0)

    def test_rsi_overbought(self):
        snap = IndicatorSnapshot(rsi=75.0)
        assert is_rsi_overbought(snap, threshold=70.0)
        assert not is_rsi_overbought(snap, threshold=80.0)

    def test_macd_bullish_cross(self):
        prev = IndicatorSnapshot(macd_hist=-0.1)
        curr = IndicatorSnapshot(macd_hist=0.1)
        assert is_macd_bullish_cross(curr, prev)
        assert not is_macd_bullish_cross(prev, curr)

    def test_stop_loss_long(self):
        stop = stop_loss_price(entry=100.0, atr=2.0, multiplier=2.0, side="long")
        assert stop == 96.0

    def test_stop_loss_short(self):
        stop = stop_loss_price(entry=100.0, atr=2.0, multiplier=2.0, side="short")
        assert stop == 104.0


# ---------------------------------------------------------------------------
# Greeks tests
# ---------------------------------------------------------------------------


class TestBSMGreeks:
    """Validate Black-Scholes against known analytical values."""

    def test_call_price_positive(self):
        price = bsm_price(S=100, K=100, T=30/365, r=0.05, sigma=0.20, option_type="call")
        assert price > 0

    def test_put_call_parity(self):
        """C - P = S - K*e^(-rT)"""
        S, K, T, r, sigma = 100.0, 100.0, 0.25, 0.05, 0.20
        call = bsm_price(S, K, T, r, sigma, "call")
        put = bsm_price(S, K, T, r, sigma, "put")
        expected = S - K * math.exp(-r * T)
        assert abs((call - put) - expected) < 0.001

    def test_atm_call_delta_near_half(self):
        g = calculate_greeks(S=100, K=100, T=0.25, r=0.05, sigma=0.20, option_type="call")
        assert 0.45 <= g.delta <= 0.60  # ATM call delta ≈ 0.50–0.55

    def test_put_delta_negative(self):
        g = calculate_greeks(S=100, K=100, T=0.25, r=0.05, sigma=0.20, option_type="put")
        assert g.delta < 0

    def test_theta_negative_for_long_options(self):
        g = calculate_greeks(S=100, K=100, T=0.25, r=0.05, sigma=0.20, option_type="call")
        assert g.theta < 0  # time decay costs money

    def test_gamma_positive(self):
        g = calculate_greeks(S=100, K=100, T=0.25, r=0.05, sigma=0.20, option_type="call")
        assert g.gamma > 0

    def test_vega_positive(self):
        g = calculate_greeks(S=100, K=100, T=0.25, r=0.05, sigma=0.20, option_type="call")
        assert g.vega > 0

    def test_deep_itm_call_delta_near_one(self):
        g = calculate_greeks(S=150, K=100, T=0.25, r=0.05, sigma=0.20, option_type="call")
        assert g.delta > 0.90

    def test_deep_otm_call_delta_near_zero(self):
        g = calculate_greeks(S=50, K=100, T=0.25, r=0.05, sigma=0.20, option_type="call")
        assert g.delta < 0.05

    def test_expired_option_returns_intrinsic(self):
        g = calculate_greeks(S=110, K=100, T=0.0, r=0.05, sigma=0.20, option_type="call")
        assert g.option_price == pytest.approx(10.0, abs=0.001)

    def test_iv_round_trip(self):
        """Calculate price from IV, then recover IV from price — should match."""
        S, K, T, r, sigma = 100.0, 105.0, 0.25, 0.05, 0.25
        market_price = bsm_price(S, K, T, r, sigma, "call")
        result = calculate_iv(market_price, S, K, T, r, "call")
        assert result.converged
        assert abs(result.iv - sigma) < 0.001

    def test_iv_rank_calculation(self):
        history = [0.20, 0.25, 0.30, 0.35, 0.40]
        rank = iv_rank(0.30, history)
        assert rank == pytest.approx(50.0, abs=1.0)

    def test_iv_rank_at_high(self):
        history = [0.20, 0.25, 0.30]
        rank = iv_rank(0.30, history)
        assert rank == pytest.approx(100.0)

    def test_iv_rank_at_low(self):
        history = [0.20, 0.25, 0.30]
        rank = iv_rank(0.20, history)
        assert rank == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Fibonacci tests
# ---------------------------------------------------------------------------


class TestFibonacci:
    def test_retracement_levels_count(self):
        result = calculate_retracements(swing_high=110.0, swing_low=100.0, direction="up")
        assert len(result.retracements) == 7  # 0, 23.6, 38.2, 50, 61.8, 78.6, 100

    def test_retracement_prices_descending_for_uptrend(self):
        result = calculate_retracements(swing_high=110.0, swing_low=100.0, direction="up")
        prices = [l.price for l in result.retracements]
        # 0% retracement = high, 100% = low (descending)
        assert prices[0] == pytest.approx(110.0)
        assert prices[-1] == pytest.approx(100.0)
        assert prices == sorted(prices, reverse=True)

    def test_50pct_retracement_is_midpoint(self):
        result = calculate_retracements(swing_high=120.0, swing_low=100.0, direction="up")
        level_50 = next(l for l in result.retracements if abs(l.ratio - 0.5) < 0.01)
        assert level_50.price == pytest.approx(110.0)

    def test_nearest_support(self):
        result = calculate_retracements(swing_high=110.0, swing_low=100.0, direction="up")
        support = result.nearest_support(current_price=106.0)
        assert support is not None
        assert support.price < 106.0

    def test_nearest_resistance(self):
        result = calculate_retracements(swing_high=110.0, swing_low=100.0, direction="up")
        resistance = result.nearest_resistance(current_price=106.0)
        assert resistance is not None
        assert resistance.price > 106.0

    def test_auto_fibonacci_returns_result(self):
        highs = [100 + i for i in range(50)]
        lows = [98 + i for i in range(50)]
        closes = [99 + i for i in range(50)]
        result = auto_fibonacci(highs, lows, closes)
        assert result.swing_high >= result.swing_low
