"""Fetches historical bars and options chain data from Alpaca."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from loguru import logger

from core.config import settings
from core.exceptions import MarketDataError
from data.normalizer import normalize_bar


class HistoricalDataFetcher:
    """Fetches OHLCV bars and options chains from Alpaca REST API."""

    def __init__(self) -> None:
        self._client = StockHistoricalDataClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
        )

    def fetch_bars(
        self,
        symbol: str,
        start: date | datetime | str,
        end: date | datetime | str | None = None,
        timeframe: str = "1Day",
        limit: int | None = None,
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV bars for a symbol.

        Args:
            symbol: Ticker symbol (e.g. "AAPL")
            start: Start date/datetime or ISO string
            end: End date/datetime or ISO string (defaults to yesterday)
            timeframe: "1Min", "5Min", "15Min", "1Hour", "1Day"
            limit: Max number of bars to return

        Returns:
            DataFrame with columns: symbol, timestamp, open, high, low, close,
            volume, vwap, trade_count — all prices as Decimal.
        """
        tf = self._parse_timeframe(timeframe)
        start_dt = self._to_datetime(start)
        end_dt = self._to_datetime(end) if end else datetime.now(tz=timezone.utc) - timedelta(days=1)

        logger.info(f"Fetching {timeframe} bars for {symbol}: {start_dt.date()} → {end_dt.date()}")

        try:
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=tf,
                start=start_dt,
                end=end_dt,
                limit=limit,
                adjustment="all",   # corporate action adjusted
            )
            bars = self._client.get_stock_bars(request)
        except Exception as e:
            raise MarketDataError(f"Failed to fetch bars for {symbol}: {e}") from e

        if not bars or symbol not in bars.df.index.get_level_values(0):
            logger.warning(f"No bars returned for {symbol}")
            return pd.DataFrame()

        df = bars.df.loc[symbol].copy()
        df = df.reset_index()
        df.rename(columns={"timestamp": "timestamp"}, inplace=True)

        # Ensure timezone-aware timestamps
        if df["timestamp"].dt.tz is None:
            df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")

        logger.info(f"Fetched {len(df)} bars for {symbol}")
        return df

    def fetch_recent_bars(
        self,
        symbol: str,
        days: int = 365,
        timeframe: str = "1Day",
    ) -> pd.DataFrame:
        """Convenience method: fetch the last N days of bars."""
        start = datetime.now(tz=timezone.utc) - timedelta(days=days)
        return self.fetch_bars(symbol, start=start, timeframe=timeframe)

    def fetch_multiple(
        self,
        symbols: list[str],
        start: date | datetime | str,
        end: date | datetime | str | None = None,
        timeframe: str = "1Day",
    ) -> dict[str, pd.DataFrame]:
        """Fetch bars for multiple symbols, returns {symbol: DataFrame}."""
        tf = self._parse_timeframe(timeframe)
        start_dt = self._to_datetime(start)
        end_dt = self._to_datetime(end) if end else datetime.now(tz=timezone.utc) - timedelta(days=1)

        logger.info(f"Fetching {timeframe} bars for {symbols}")

        try:
            request = StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=tf,
                start=start_dt,
                end=end_dt,
                adjustment="all",
            )
            bars = self._client.get_stock_bars(request)
        except Exception as e:
            raise MarketDataError(f"Failed to fetch bars for {symbols}: {e}") from e

        result: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            try:
                df = bars.df.loc[sym].copy().reset_index()
                if df["timestamp"].dt.tz is None:
                    df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")
                result[sym] = df
                logger.debug(f"  {sym}: {len(df)} bars")
            except KeyError:
                logger.warning(f"No data returned for {sym}")
                result[sym] = pd.DataFrame()

        return result

    @staticmethod
    def _parse_timeframe(tf_str: str) -> TimeFrame:
        mapping = {
            "1Min": TimeFrame(1, TimeFrameUnit.Minute),
            "5Min": TimeFrame(5, TimeFrameUnit.Minute),
            "15Min": TimeFrame(15, TimeFrameUnit.Minute),
            "30Min": TimeFrame(30, TimeFrameUnit.Minute),
            "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
            "4Hour": TimeFrame(4, TimeFrameUnit.Hour),
            "1Day": TimeFrame(1, TimeFrameUnit.Day),
            "1Week": TimeFrame(1, TimeFrameUnit.Week),
            "1Month": TimeFrame(1, TimeFrameUnit.Month),
        }
        if tf_str not in mapping:
            raise ValueError(f"Unknown timeframe '{tf_str}'. Options: {list(mapping)}")
        return mapping[tf_str]

    @staticmethod
    def _to_datetime(value: date | datetime | str) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
        if isinstance(value, str):
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        raise TypeError(f"Cannot convert {type(value)} to datetime")
