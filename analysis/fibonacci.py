"""
Fibonacci retracement and extension levels.
Used to identify potential support/resistance and price targets.
"""

from __future__ import annotations

from dataclasses import dataclass

RETRACEMENT_LEVELS = [0.0, 0.236, 0.382, 0.500, 0.618, 0.786, 1.0]
EXTENSION_LEVELS = [1.0, 1.272, 1.414, 1.618, 2.0, 2.618]


@dataclass
class FibLevel:
    ratio: float
    price: float
    label: str


@dataclass
class FibResult:
    swing_high: float
    swing_low: float
    direction: str                  # "up" (retracing from high) | "down" (retracing from low)
    retracements: list[FibLevel]
    extensions: list[FibLevel]

    def nearest_support(self, current_price: float) -> FibLevel | None:
        """Nearest fib level below current price (potential support)."""
        candidates = [l for l in self.retracements if l.price < current_price]
        return max(candidates, key=lambda l: l.price) if candidates else None

    def nearest_resistance(self, current_price: float) -> FibLevel | None:
        """Nearest fib level above current price (potential resistance)."""
        candidates = [l for l in self.retracements if l.price > current_price]
        return min(candidates, key=lambda l: l.price) if candidates else None

    def nearest_extension_target(self, current_price: float) -> FibLevel | None:
        """Nearest extension level above current price (price target)."""
        candidates = [l for l in self.extensions if l.price > current_price]
        return min(candidates, key=lambda l: l.price) if candidates else None


def calculate_retracements(
    swing_high: float,
    swing_low: float,
    direction: str = "up",
) -> FibResult:
    """
    Calculate Fibonacci retracement levels between a swing high and low.

    Args:
        swing_high: The peak price
        swing_low: The trough price
        direction: "up" = price moved up (retracements are pullbacks)
                   "down" = price moved down (retracements are bounces)

    Returns:
        FibResult with retracement and extension levels
    """
    diff = swing_high - swing_low

    if direction == "up":
        # Retracements: price pulling back from high toward low
        retracements = [
            FibLevel(
                ratio=r,
                price=round(swing_high - diff * r, 4),
                label=f"{r*100:.1f}%",
            )
            for r in RETRACEMENT_LEVELS
        ]
        # Extensions: price extending above the swing high
        extensions = [
            FibLevel(
                ratio=r,
                price=round(swing_low + diff * r, 4),
                label=f"{r*100:.1f}%",
            )
            for r in EXTENSION_LEVELS
        ]
    else:
        # direction == "down"
        # Retracements: price bouncing back up from low toward high
        retracements = [
            FibLevel(
                ratio=r,
                price=round(swing_low + diff * r, 4),
                label=f"{r*100:.1f}%",
            )
            for r in RETRACEMENT_LEVELS
        ]
        # Extensions: price extending below the swing low
        extensions = [
            FibLevel(
                ratio=r,
                price=round(swing_high - diff * r, 4),
                label=f"{r*100:.1f}%",
            )
            for r in EXTENSION_LEVELS
        ]

    return FibResult(
        swing_high=swing_high,
        swing_low=swing_low,
        direction=direction,
        retracements=retracements,
        extensions=extensions,
    )


def find_swing_points(
    high: list[float],
    low: list[float],
    lookback: int = 20,
) -> tuple[float, float]:
    """
    Find the swing high and swing low over the most recent `lookback` bars.

    Returns:
        (swing_high, swing_low)
    """
    recent_high = high[-lookback:]
    recent_low = low[-lookback:]
    return max(recent_high), min(recent_low)


def auto_fibonacci(
    high: list[float],
    low: list[float],
    close: list[float],
    lookback: int = 50,
) -> FibResult:
    """
    Automatically determine swing direction and compute Fibonacci levels.

    Direction is determined by whether price is currently closer to the
    swing high (trending up, retracing down) or swing low (trending down,
    bouncing up).
    """
    swing_high, swing_low = find_swing_points(high, low, lookback)
    current = close[-1]
    mid = (swing_high + swing_low) / 2.0

    # If price is in upper half of range, treat as uptrend (retracing from high)
    direction = "up" if current >= mid else "down"
    return calculate_retracements(swing_high, swing_low, direction)
