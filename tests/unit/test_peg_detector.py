"""Tests for Power Earnings Gap detection."""

from unittest.mock import MagicMock

import pandas as pd

from data.peg_detector import PEGDetector, is_power_earnings_gap


def _make_bars(
    closes: list[float],
    opens: list[float] | None = None,
    volumes: list[int] | None = None,
) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame({
        "open": opens or closes,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": volumes or [1_000_000] * n,
    })


class TestIsPowerEarningsGap:
    def test_detects_gap_up_with_volume_spike(self):
        # gap = 15%, volume = 3x
        assert is_power_earnings_gap(
            current_open=115.0,
            prior_close=100.0,
            current_volume=3_000_000,
            avg_volume=1_000_000,
        ) is True

    def test_rejects_gap_below_10_pct(self):
        assert is_power_earnings_gap(
            current_open=108.0,
            prior_close=100.0,
            current_volume=3_000_000,
            avg_volume=1_000_000,
        ) is False

    def test_rejects_volume_below_2x(self):
        assert is_power_earnings_gap(
            current_open=115.0,
            prior_close=100.0,
            current_volume=1_500_000,
            avg_volume=1_000_000,
        ) is False

    def test_rejects_gap_down(self):
        assert is_power_earnings_gap(
            current_open=85.0,
            prior_close=100.0,
            current_volume=3_000_000,
            avg_volume=1_000_000,
        ) is False

    def test_rejects_zero_avg_volume(self):
        assert is_power_earnings_gap(
            current_open=115.0,
            prior_close=100.0,
            current_volume=3_000_000,
            avg_volume=0,
        ) is False


class TestPEGDetector:
    def _make_history_with_gap(self, last_open: float, prev_close: float, gap_vol: int = 3_000_000) -> pd.DataFrame:
        """30-bar history where the last bar has a PEG."""
        closes = [100.0] * 28 + [prev_close, prev_close]
        opens = [100.0] * 28 + [prev_close, last_open]
        volumes = [1_000_000] * 29 + [gap_vol]
        return _make_bars(closes, opens, volumes)

    def test_scan_returns_symbols_with_peg(self):
        detector = PEGDetector()
        detector._fetcher = MagicMock()

        peg_df = self._make_history_with_gap(last_open=115.0, prev_close=100.0)
        no_gap_df = self._make_history_with_gap(last_open=101.0, prev_close=100.0)

        detector._fetcher.fetch_recent_bars.side_effect = lambda sym, **kw: (
            peg_df if sym == "AAPL" else no_gap_df
        )

        result = detector.scan_recent_gaps(["AAPL", "MSFT"])
        assert "AAPL" in result
        assert "MSFT" not in result

    def test_scan_skips_symbol_on_exception(self):
        detector = PEGDetector()
        detector._fetcher = MagicMock()
        detector._fetcher.fetch_recent_bars.side_effect = Exception("network error")

        result = detector.scan_recent_gaps(["AAPL"])
        assert result == []

    def test_scan_returns_empty_on_insufficient_data(self):
        detector = PEGDetector()
        detector._fetcher = MagicMock()
        detector._fetcher.fetch_recent_bars.return_value = pd.DataFrame()

        result = detector.scan_recent_gaps(["AAPL"])
        assert result == []
