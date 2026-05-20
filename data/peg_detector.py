"""
Power Earnings Gap (PEG) detector.

Identifies stocks that gapped up ≥10% from prior close on ≥2× average volume
within the last 2 trading days. Uses price/volume history only — no news API.

The WatchlistProvider will call this to auto-prioritize PEG candidates.
"""

from __future__ import annotations

from loguru import logger

import pandas as pd

from data.historical import HistoricalDataFetcher


def is_power_earnings_gap(
    current_open: float,
    prior_close: float,
    current_volume: int | float,
    avg_volume: float,
    min_gap_pct: float = 0.10,
    min_volume_ratio: float = 2.0,
) -> bool:
    """
    Return True if a single bar's open/volume qualifies as a Power Earnings Gap.

    A PEG is a gap-up ≥ min_gap_pct from prior close with volume ≥ min_volume_ratio
    times the recent average. Only bullish gaps (current_open > prior_close) qualify.
    """
    import math
    if prior_close <= 0 or avg_volume <= 0 or not math.isfinite(avg_volume):
        return False

    gap_pct = (current_open - prior_close) / prior_close
    vol_ratio = current_volume / avg_volume

    return gap_pct >= min_gap_pct and vol_ratio >= min_volume_ratio


class PEGDetector:
    """Scans a list of symbols and returns those with a recent Power Earnings Gap."""

    def __init__(self) -> None:
        self._fetcher = HistoricalDataFetcher()

    def scan_recent_gaps(
        self,
        symbols: list[str],
        lookback_days: int = 30,
        gap_lookback_bars: int = 2,
    ) -> list[str]:
        """
        Return symbols that had a PEG within the last `gap_lookback_bars` trading days.

        Args:
            symbols: Tickers to scan.
            lookback_days: Calendar days of history to fetch (for avg volume baseline).
            gap_lookback_bars: How many recent bars to check for a gap (default 2).
        """
        peg_symbols: list[str] = []

        for symbol in symbols:
            try:
                df = self._fetcher.fetch_recent_bars(symbol, days=lookback_days, timeframe="1Day")

                min_bars = 22 + gap_lookback_bars
                if df.empty or len(df) < min_bars:
                    continue

                # 20-day avg volume from the baseline period (excluding gap candidates)
                baseline_end = len(df) - gap_lookback_bars
                avg_volume = float(df["volume"].iloc[baseline_end - 20:baseline_end].mean())

                # Check each candidate bar (most recent gap_lookback_bars)
                for offset in range(1, gap_lookback_bars + 1):
                    idx = -offset
                    prior_idx = idx - 1

                    current_open = float(df["open"].iloc[idx])
                    prior_close = float(df["close"].iloc[prior_idx])
                    current_volume = float(df["volume"].iloc[idx])

                    if is_power_earnings_gap(current_open, prior_close, current_volume, avg_volume):
                        logger.info(
                            f"[PEG] {symbol}: gap={100*(current_open/prior_close - 1):.1f}% "
                            f"vol={current_volume/avg_volume:.1f}x avg"
                        )
                        peg_symbols.append(symbol)
                        break

            except Exception as e:
                logger.warning(f"[PEG] {symbol} scan failed: {e}")
                continue

        return peg_symbols
