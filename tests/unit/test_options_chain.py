"""Tests for BrokerClient.get_options_chain() normalization."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from broker.client import BrokerClient
from strategies.wheel.csp_leg import OptionContract


def _make_raw_contract(
    symbol="AMD240119P00120000",
    underlying="AMD",
    contract_type="put",
    strike="120.00",
    expiry_date=None,
    oi=500,
    volume=200,
):
    c = MagicMock()
    c.symbol = symbol
    c.underlying_symbol = underlying
    c.type = MagicMock()
    c.type.value = contract_type
    c.strike_price = Decimal(strike)
    c.expiration_date = expiry_date or (date.today() + timedelta(days=30))
    c.close_price = Decimal("2.10")
    c.open_interest = oi
    c.volume = volume
    return c


def _make_snapshot(bid="2.00", ask="2.20", delta="-0.28", iv="0.45"):
    snap = MagicMock()
    snap.latest_quote = MagicMock()
    snap.latest_quote.bid_price = Decimal(bid)
    snap.latest_quote.ask_price = Decimal(ask)
    snap.greeks = MagicMock()
    snap.greeks.delta = float(delta)
    snap.greeks.implied_volatility = float(iv)
    return snap


@patch("broker.client.TradingClient")
@patch("broker.client.OptionHistoricalDataClient")
def test_get_options_chain_returns_put_contracts(MockDataClient, MockTradingClient):
    """get_options_chain returns normalized OptionContract list for puts."""
    raw = _make_raw_contract()
    MockTradingClient.return_value.get_option_contracts.return_value = MagicMock(
        option_contracts=[raw]
    )
    MockDataClient.return_value.get_option_snapshot.return_value = {
        "AMD240119P00120000": _make_snapshot()
    }

    client = BrokerClient()
    chain = client.get_options_chain("AMD", dte_min=21, dte_max=45, option_type="put")

    assert len(chain) == 1
    c = chain[0]
    assert isinstance(c, OptionContract)
    assert c.symbol == "AMD"
    assert c.contract_id == "AMD240119P00120000"
    assert c.option_type == "put"
    assert c.strike == Decimal("120.00")
    assert c.bid == Decimal("2.00")
    assert c.ask == Decimal("2.20")
    assert abs(c.delta - (-0.28)) < 0.001
    assert c.iv == pytest.approx(0.45, rel=0.01)


@patch("broker.client.TradingClient")
@patch("broker.client.OptionHistoricalDataClient")
def test_get_options_chain_filters_by_dte(MockDataClient, MockTradingClient):
    """Contracts outside dte_min/dte_max window are excluded."""
    inside = _make_raw_contract(
        symbol="AMD240119P00120000",
        expiry_date=date.today() + timedelta(days=30),
    )
    outside = _make_raw_contract(
        symbol="AMD240215P00120000",
        expiry_date=date.today() + timedelta(days=60),
    )
    MockTradingClient.return_value.get_option_contracts.return_value = MagicMock(
        option_contracts=[inside, outside]
    )
    MockDataClient.return_value.get_option_snapshot.return_value = {
        "AMD240119P00120000": _make_snapshot(),
        "AMD240215P00120000": _make_snapshot(),
    }

    client = BrokerClient()
    chain = client.get_options_chain("AMD", dte_min=21, dte_max=45, option_type="put")

    assert len(chain) == 1
    assert chain[0].contract_id == "AMD240119P00120000"


@patch("broker.client.TradingClient")
@patch("broker.client.OptionHistoricalDataClient")
def test_get_options_chain_returns_empty_on_api_error(MockDataClient, MockTradingClient):
    """Returns empty list (does not raise) when API call fails."""
    MockTradingClient.return_value.get_option_contracts.side_effect = Exception("API error")

    client = BrokerClient()
    chain = client.get_options_chain("AMD", dte_min=21, dte_max=45, option_type="put")

    assert chain == []
