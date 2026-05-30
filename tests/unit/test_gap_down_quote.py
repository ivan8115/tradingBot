"""
BrokerClient.get_latest_quote must return a real-time price from Alpaca's
StockHistoricalDataClient. Used by the gap-down scanner at 8:15 AM to detect
overnight moves before the first intraday bar prints.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest


@patch("broker.client.TradingClient")
@patch("broker.client.OptionHistoricalDataClient")
@patch("broker.client.StockHistoricalDataClient")
def test_get_latest_quote_returns_ask_price(MockStock, MockOption, MockTrading):
    """Returns ask_price when available and positive."""
    mock_quote = MagicMock()
    mock_quote.ask_price = 29.50
    mock_quote.bid_price = 29.40
    MockStock.return_value.get_stock_latest_quote.return_value = {"AMD": mock_quote}

    from broker.client import BrokerClient
    broker = BrokerClient()
    result = broker.get_latest_quote("AMD")

    assert result == 29.50


@patch("broker.client.TradingClient")
@patch("broker.client.OptionHistoricalDataClient")
@patch("broker.client.StockHistoricalDataClient")
def test_get_latest_quote_returns_bid_when_ask_zero(MockStock, MockOption, MockTrading):
    """Falls back to bid_price when ask_price is 0 or None."""
    mock_quote = MagicMock()
    mock_quote.ask_price = 0.0
    mock_quote.bid_price = 29.40
    MockStock.return_value.get_stock_latest_quote.return_value = {"AMD": mock_quote}

    from broker.client import BrokerClient
    broker = BrokerClient()
    result = broker.get_latest_quote("AMD")

    assert result == 29.40


@patch("broker.client.TradingClient")
@patch("broker.client.OptionHistoricalDataClient")
@patch("broker.client.StockHistoricalDataClient")
def test_get_latest_quote_returns_none_on_exception(MockStock, MockOption, MockTrading):
    """Returns None gracefully when the API call raises."""
    MockStock.return_value.get_stock_latest_quote.side_effect = Exception("timeout")

    from broker.client import BrokerClient
    broker = BrokerClient()
    result = broker.get_latest_quote("AMD")

    assert result is None


@patch("broker.client.TradingClient")
@patch("broker.client.OptionHistoricalDataClient")
@patch("broker.client.StockHistoricalDataClient")
def test_get_latest_quote_returns_none_when_symbol_missing(MockStock, MockOption, MockTrading):
    """Returns None when symbol is not in the API response dict."""
    MockStock.return_value.get_stock_latest_quote.return_value = {}

    from broker.client import BrokerClient
    broker = BrokerClient()
    result = broker.get_latest_quote("AMD")

    assert result is None
