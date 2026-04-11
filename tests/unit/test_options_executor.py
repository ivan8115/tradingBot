"""Tests for options order routing in BrokerClient and Executor."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from broker.client import BrokerClient


def _make_mock_order(order_id="order-abc-123"):
    o = MagicMock()
    o.id = order_id
    o.status = MagicMock()
    o.status.value = "accepted"
    return o


@patch("broker.client.TradingClient")
@patch("broker.client.OptionHistoricalDataClient")
def test_submit_options_order_sell(MockDataClient, MockTradingClient):
    MockTradingClient.return_value.submit_order.return_value = _make_mock_order("order-abc-123")
    client = BrokerClient()
    order_id = client.submit_options_order(
        contract_symbol="AMD240119P00120000",
        qty=1,
        side="sell",
        order_type="limit",
        limit_price=Decimal("2.10"),
    )
    assert order_id == "order-abc-123"
    call_args = MockTradingClient.return_value.submit_order.call_args[0][0]
    assert call_args.symbol == "AMD240119P00120000"
    assert call_args.qty == 1


@patch("broker.client.TradingClient")
@patch("broker.client.OptionHistoricalDataClient")
def test_submit_options_order_buy_to_close(MockDataClient, MockTradingClient):
    MockTradingClient.return_value.submit_order.return_value = _make_mock_order("order-xyz-456")
    client = BrokerClient()
    order_id = client.submit_options_order(
        contract_symbol="AMD240119P00120000",
        qty=1,
        side="buy",
        order_type="limit",
        limit_price=Decimal("1.05"),
    )
    assert order_id == "order-xyz-456"
