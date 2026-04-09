"""
Options Greeks calculator using Black-Scholes-Merton model.
Computes Delta, Gamma, Theta, Vega, and Implied Volatility (IV).

All inputs/outputs use float for speed (options math benefits from numpy).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
from scipy.stats import norm


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class Greeks:
    delta: float        # Rate of change of option price w.r.t. underlying
    gamma: float        # Rate of change of delta w.r.t. underlying
    theta: float        # Daily time decay (negative for long options)
    vega: float         # Sensitivity to 1% change in IV
    rho: float          # Sensitivity to 1% change in risk-free rate
    iv: float           # Implied volatility (annualized, e.g. 0.30 = 30%)
    intrinsic: float    # Max(0, S-K) for calls, Max(0, K-S) for puts
    time_value: float   # Option price - intrinsic value
    option_price: float # Theoretical price from BSM


@dataclass
class IVResult:
    iv: float           # Annualized implied volatility
    converged: bool     # Whether Newton-Raphson converged
    iterations: int


# ---------------------------------------------------------------------------
# Black-Scholes-Merton
# ---------------------------------------------------------------------------


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    """Compute d1 and d2 for BSM formula."""
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return d1, d2


def bsm_price(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: Literal["call", "put"],
) -> float:
    """
    Black-Scholes-Merton option price.

    Args:
        S: Current underlying price
        K: Strike price
        T: Time to expiration in years (e.g. 30 days = 30/365)
        r: Risk-free rate (annualized, e.g. 0.05 = 5%)
        sigma: Implied volatility (annualized, e.g. 0.30 = 30%)
        option_type: "call" or "put"

    Returns:
        Theoretical option price
    """
    if T <= 0:
        # Expired — return intrinsic value
        if option_type == "call":
            return max(0.0, S - K)
        return max(0.0, K - S)

    d1, d2 = _d1_d2(S, K, T, r, sigma)

    if option_type == "call":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def calculate_greeks(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: Literal["call", "put"],
    market_price: float | None = None,
) -> Greeks:
    """
    Compute all Greeks for an option.

    Args:
        S: Underlying price
        K: Strike price
        T: Time to expiration in years
        r: Risk-free rate
        sigma: Implied volatility (annualized). Use calculate_iv() if unknown.
        option_type: "call" or "put"
        market_price: If provided, used to compute time_value

    Returns:
        Greeks dataclass with all computed values
    """
    if T <= 0:
        delta = 1.0 if option_type == "call" and S > K else (
            -1.0 if option_type == "put" and S < K else 0.0
        )
        return Greeks(
            delta=delta, gamma=0.0, theta=0.0, vega=0.0, rho=0.0,
            iv=sigma, intrinsic=max(0.0, S - K if option_type == "call" else K - S),
            time_value=0.0, option_price=max(0.0, S - K if option_type == "call" else K - S),
        )

    d1, d2 = _d1_d2(S, K, T, r, sigma)
    sqrt_T = math.sqrt(T)
    nd1 = norm.pdf(d1)
    e_rT = math.exp(-r * T)

    # Delta
    if option_type == "call":
        delta = norm.cdf(d1)
    else:
        delta = norm.cdf(d1) - 1.0    # negative for puts

    # Gamma (same for calls and puts)
    gamma = nd1 / (S * sigma * sqrt_T)

    # Theta (per calendar day)
    common_theta = -(S * nd1 * sigma) / (2 * sqrt_T)
    if option_type == "call":
        theta = (common_theta - r * K * e_rT * norm.cdf(d2)) / 365.0
    else:
        theta = (common_theta + r * K * e_rT * norm.cdf(-d2)) / 365.0

    # Vega (per 1% change in IV — divide by 100 for standard vega per 1%)
    vega = S * nd1 * sqrt_T / 100.0

    # Rho (per 1% change in rate)
    if option_type == "call":
        rho = K * T * e_rT * norm.cdf(d2) / 100.0
    else:
        rho = -K * T * e_rT * norm.cdf(-d2) / 100.0

    theo_price = bsm_price(S, K, T, r, sigma, option_type)
    if option_type == "call":
        intrinsic = max(0.0, S - K)
    else:
        intrinsic = max(0.0, K - S)

    actual_price = market_price if market_price is not None else theo_price
    time_value = max(0.0, actual_price - intrinsic)

    return Greeks(
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        rho=rho,
        iv=sigma,
        intrinsic=intrinsic,
        time_value=time_value,
        option_price=theo_price,
    )


def calculate_iv(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    option_type: Literal["call", "put"],
    initial_guess: float = 0.30,
    max_iterations: int = 100,
    tolerance: float = 1e-6,
) -> IVResult:
    """
    Calculate implied volatility using Newton-Raphson iteration.

    Args:
        market_price: Observed market price of the option
        S: Underlying price
        K: Strike price
        T: Time to expiration in years
        r: Risk-free rate
        option_type: "call" or "put"
        initial_guess: Starting IV estimate (0.30 = 30%)
        max_iterations: Newton-Raphson iteration limit
        tolerance: Convergence threshold (price difference)

    Returns:
        IVResult with iv, converged flag, and iteration count
    """
    if T <= 0:
        return IVResult(iv=float("nan"), converged=False, iterations=0)

    # Intrinsic value bounds check
    intrinsic = max(0.0, S - K if option_type == "call" else K - S)
    if market_price <= intrinsic:
        # Price at or below intrinsic — IV effectively 0 (deep ITM)
        return IVResult(iv=0.0, converged=True, iterations=0)

    sigma = initial_guess
    for i in range(max_iterations):
        price = bsm_price(S, K, T, r, sigma, option_type)
        d1, _ = _d1_d2(S, K, T, r, sigma)
        vega_raw = S * norm.pdf(d1) * math.sqrt(T)  # raw vega (not divided by 100)

        if abs(vega_raw) < 1e-10:
            break

        diff = price - market_price
        if abs(diff) < tolerance:
            return IVResult(iv=sigma, converged=True, iterations=i + 1)

        sigma -= diff / vega_raw
        sigma = max(0.001, min(sigma, 10.0))  # clamp to [0.1%, 1000%]

    return IVResult(iv=sigma, converged=False, iterations=max_iterations)


def iv_rank(current_iv: float, iv_history: list[float]) -> float:
    """
    IV Rank: where current IV sits in its 52-week range (0–100).
    IV Rank = (current - low) / (high - low) * 100
    """
    if not iv_history or len(iv_history) < 2:
        return float("nan")
    iv_low = min(iv_history)
    iv_high = max(iv_history)
    if iv_high == iv_low:
        return 0.0
    return (current_iv - iv_low) / (iv_high - iv_low) * 100.0


def iv_percentile(current_iv: float, iv_history: list[float]) -> float:
    """
    IV Percentile: % of days in the past year where IV was lower than current.
    More robust than IV Rank for skewed distributions.
    """
    if not iv_history:
        return float("nan")
    below = sum(1 for iv in iv_history if iv < current_iv)
    return below / len(iv_history) * 100.0


def dte_to_years(dte: int) -> float:
    """Convert days-to-expiration to fraction of year."""
    return dte / 365.0


def expected_move(S: float, iv: float, dte: int) -> float:
    """
    Expected 1-sigma move by expiration.
    Formula: S * IV * sqrt(DTE/365)
    """
    return S * iv * math.sqrt(dte / 365.0)
