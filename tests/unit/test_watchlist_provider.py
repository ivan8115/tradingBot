"""Tests for WatchlistProvider — all external HTTP calls are mocked."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from data.watchlist_provider import WatchlistEntry, WatchlistProvider, _days_ago


# ---------------------------------------------------------------------------
# _days_ago helper
# ---------------------------------------------------------------------------

def test_days_ago_today():
    from datetime import date
    today_str = date.today().isoformat()
    assert _days_ago(today_str) == 0


def test_days_ago_old_date():
    assert _days_ago("2000-01-01") > 1000


def test_days_ago_bad_string():
    assert _days_ago("not-a-date") == 999


# ---------------------------------------------------------------------------
# WatchlistEntry scoring
# ---------------------------------------------------------------------------

def test_entry_defaults():
    e = WatchlistEntry(symbol="AAPL", price=150.0, iv_proxy=30.0, options_volume=1000)
    assert e.quiverquant_score == 0.0
    assert e.final_score == 0.0


# ---------------------------------------------------------------------------
# WatchlistProvider._scan_finviz
# ---------------------------------------------------------------------------

@patch("data.watchlist_provider.Screener")
def test_scan_finviz_filters_price(mock_screener_cls, tmp_path, monkeypatch):
    """Stocks outside price range are excluded."""
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")

    mock_screener = MagicMock()
    mock_screener.__iter__ = MagicMock(return_value=iter([
        {"Ticker": "AMD", "Price": "40.00", "Volatility": "40%", "Volume": "5000000"},
        {"Ticker": "CHEAP", "Price": "2.00", "Volatility": "80%", "Volume": "999999"},
    ]))
    mock_screener_cls.return_value = mock_screener

    from core.config import settings
    monkeypatch.setattr(settings.watchlist, "min_price", 10.0)
    monkeypatch.setattr(settings.watchlist, "max_price", 50.0)
    monkeypatch.setattr(settings.watchlist, "min_options_volume", 0)
    monkeypatch.setattr(settings.watchlist, "min_stock_volume", 0)

    provider = WatchlistProvider.__new__(WatchlistProvider)
    provider._cfg = settings.watchlist
    provider._api_key = ""

    entries = provider._scan_finviz()
    assert all(e.symbol != "CHEAP" for e in entries)
    assert any(e.symbol == "AMD" for e in entries)


@patch("data.watchlist_provider.Screener")
def test_scan_finviz_exception_returns_empty(mock_screener_cls, monkeypatch):
    """If Finviz throws, we return [] gracefully."""
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    mock_screener_cls.side_effect = Exception("network error")

    from data.watchlist_provider import WatchlistProvider
    provider = WatchlistProvider.__new__(WatchlistProvider)
    from core.config import settings
    provider._cfg = settings.watchlist
    provider._api_key = ""
    result = provider._scan_finviz()
    assert result == []


# ---------------------------------------------------------------------------
# QuiverQuant enrichment
# ---------------------------------------------------------------------------

@patch("data.watchlist_provider.httpx.get")
def test_enrich_quiverquant_adds_score(mock_get, monkeypatch):
    """Recent congressional buys bump the quiverquant_score."""
    from datetime import date, timedelta
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")

    recent = (date.today() - timedelta(days=10)).isoformat()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"Transaction": "Purchase", "TransactionDate": recent},
        {"Transaction": "Purchase", "TransactionDate": recent},
    ]
    mock_get.return_value = mock_resp

    from data.watchlist_provider import WatchlistProvider, WatchlistEntry
    provider = WatchlistProvider.__new__(WatchlistProvider)
    from core.config import settings
    provider._cfg = settings.watchlist
    provider._api_key = "fake_key"

    entries = [WatchlistEntry(symbol="AMD", price=40.0, iv_proxy=40.0, options_volume=1000)]
    result = provider._enrich_quiverquant(entries)
    assert result[0].quiverquant_score == 20.0  # 2 buys × 10


@patch("data.watchlist_provider.httpx.get")
def test_enrich_quiverquant_failure_is_nonfatal(mock_get, monkeypatch):
    """QQ HTTP failure doesn't crash — score stays 0."""
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    mock_get.side_effect = Exception("timeout")

    from data.watchlist_provider import WatchlistProvider, WatchlistEntry
    provider = WatchlistProvider.__new__(WatchlistProvider)
    from core.config import settings
    provider._cfg = settings.watchlist
    provider._api_key = "fake_key"

    entries = [WatchlistEntry(symbol="AMD", price=40.0, iv_proxy=40.0, options_volume=1000)]
    result = provider._enrich_quiverquant(entries)
    assert result[0].quiverquant_score == 0.0


# ---------------------------------------------------------------------------
# Blacklist
# ---------------------------------------------------------------------------

@patch("data.watchlist_provider.Screener")
def test_blacklisted_symbols_excluded(mock_screener_cls, monkeypatch):
    """Symbols on the ETF blacklist must never appear in scan results."""
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")

    mock_screener = MagicMock()
    mock_screener.__iter__ = MagicMock(return_value=iter([
        {"Ticker": "AMD", "Price": "35.00", "Volatility": "40%", "Volume": "2000000"},
        {"Ticker": "TSLL", "Price": "15.00", "Volatility": "120%", "Volume": "5000000"},
        {"Ticker": "BITO", "Price": "25.00", "Volatility": "80%", "Volume": "3000000"},
        {"Ticker": "SOXL", "Price": "30.00", "Volatility": "90%", "Volume": "4000000"},
    ]))
    mock_screener_cls.return_value = mock_screener

    from core.config import settings
    monkeypatch.setattr(settings.watchlist, "min_price", 10.0)
    monkeypatch.setattr(settings.watchlist, "max_price", 50.0)
    monkeypatch.setattr(settings.watchlist, "min_options_volume", 0)
    monkeypatch.setattr(settings.watchlist, "min_stock_volume", 0)

    provider = WatchlistProvider.__new__(WatchlistProvider)
    provider._cfg = settings.watchlist
    provider._api_key = ""

    entries = provider._scan_finviz()
    symbols = [e.symbol for e in entries]
    assert "AMD" in symbols
    assert "TSLL" not in symbols, "TSLL is a leveraged ETF — must be blacklisted"
    assert "BITO" not in symbols, "BITO is a crypto ETF — must be blacklisted"
    assert "SOXL" not in symbols, "SOXL is a leveraged ETF — must be blacklisted"


# ---------------------------------------------------------------------------
# Stock volume filter
# ---------------------------------------------------------------------------

@patch("data.watchlist_provider.Screener")
def test_low_volume_stock_excluded(mock_screener_cls, monkeypatch):
    """Stocks below min_stock_volume must be excluded."""
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")

    mock_screener = MagicMock()
    mock_screener.__iter__ = MagicMock(return_value=iter([
        {"Ticker": "LIQUID", "Price": "20.00", "Volatility": "30%", "Volume": "1000000"},
        {"Ticker": "ILLIQUID", "Price": "20.00", "Volatility": "35%", "Volume": "100000"},
    ]))
    mock_screener_cls.return_value = mock_screener

    from core.config import settings
    monkeypatch.setattr(settings.watchlist, "min_price", 10.0)
    monkeypatch.setattr(settings.watchlist, "max_price", 50.0)
    monkeypatch.setattr(settings.watchlist, "min_options_volume", 0)
    monkeypatch.setattr(settings.watchlist, "min_stock_volume", 500_000)

    provider = WatchlistProvider.__new__(WatchlistProvider)
    provider._cfg = settings.watchlist
    provider._api_key = ""

    entries = provider._scan_finviz()
    symbols = [e.symbol for e in entries]
    assert "LIQUID" in symbols
    assert "ILLIQUID" not in symbols, "ILLIQUID has volume 100K < 500K minimum"
