"""Tests for Minervini Stage Analysis classification."""

import pandas as pd

from analysis.stage_analysis import Stage, classify_stage


def _make_df(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "open": [c - 0.3 for c in closes],
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [1_000_000] * len(closes),
    })


class TestStageAnalysis:
    def test_stage_2_when_price_above_rising_smas_in_order(self):
        # Steadily rising series: price > SMA150 > SMA200, SMA200 rising
        closes = [100 + i * 0.5 for i in range(220)]
        df = _make_df(closes)
        assert classify_stage(df) == Stage.STAGE_2

    def test_stage_4_when_price_below_declining_sma200(self):
        # Steadily declining: price below declining SMA200
        closes = [200 - i * 0.5 for i in range(220)]
        df = _make_df(closes)
        assert classify_stage(df) == Stage.STAGE_4

    def test_unknown_when_insufficient_data(self):
        closes = [100.0] * 150
        df = _make_df(closes)
        assert classify_stage(df) == Stage.UNKNOWN

    def test_unknown_on_empty_df(self):
        assert classify_stage(pd.DataFrame()) == Stage.UNKNOWN
