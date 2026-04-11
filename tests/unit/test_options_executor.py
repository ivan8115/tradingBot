"""Tests for options order routing in BrokerClient and Executor."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from broker.client import BrokerClient
from core.exceptions import InsufficientFundsError, OrderError


def _make_mock_order(order_id="order-abc-123"):
    o = MagicMock()
    o.id = order_id
    o.status = MagicMock()
    o.status.value = "accepted"
    return o


@patch("broker.client.TradingClient")
@patch("broker.client.OptionHistoricalDataClient")
def test_submit_options_order_sell(MockDataClient, MockTradingClient):
    """submit_options_order sends a limit sell with the OCC contract symbol."""
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
    """submit_options_order sends a limit buy for BTC orders."""
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


@patch("broker.client.TradingClient")
@patch("broker.client.OptionHistoricalDataClient")
def test_submit_options_order_raises_insufficient_funds_error(MockDataClient, MockTradingClient):
    """Raises InsufficientFundsError when Alpaca returns a buying power error."""
    MockTradingClient.return_value.submit_order.side_effect = Exception("insufficient buying power")

    client = BrokerClient()
    with pytest.raises(InsufficientFundsError):
        client.submit_options_order(
            contract_symbol="AMD240119P00120000",
            qty=1,
            side="sell",
            order_type="limit",
            limit_price=Decimal("2.10"),
        )


@patch("broker.client.TradingClient")
@patch("broker.client.OptionHistoricalDataClient")
def test_submit_options_order_raises_order_error_on_failure(MockDataClient, MockTradingClient):
    """Raises OrderError for generic Alpaca API failures."""
    MockTradingClient.return_value.submit_order.side_effect = Exception("api rate limit exceeded")

    client = BrokerClient()
    with pytest.raises(OrderError):
        client.submit_options_order(
            contract_symbol="AMD240119P00120000",
            qty=1,
            side="sell",
            order_type="limit",
            limit_price=Decimal("2.10"),
        )


def test_submit_options_order_raises_value_error_for_limit_without_price():
    """Raises ValueError when order_type="limit" but no limit_price given."""
    with patch("broker.client.TradingClient"), patch("broker.client.OptionHistoricalDataClient"):
        client = BrokerClient()
        with pytest.raises(ValueError, match="limit_price is required"):
            client.submit_options_order(
                contract_symbol="AMD240119P00120000",
                qty=1,
                side="sell",
                order_type="limit",
                limit_price=None,
            )


from datetime import datetime, timezone
from execution.executor import Executor
from execution.order_builder import OrderBuilder
from core.events import SignalEvent


def _make_signal(signal_type="SELL_PUT", symbol="AMD",
                 contract_id="AMD240119P00120000", premium=2.10):
    return SignalEvent(
        strategy_id="wheel",
        symbol=symbol,
        signal_type=signal_type,
        strength=0.8,
        timestamp=datetime.now(timezone.utc),
        metadata={
            "leg": "csp_open",
            "contract_id": contract_id,
            "strike": 120.0,
            "premium": premium,
            "delta": -0.28,
        },
    )


@pytest.mark.asyncio
@patch("broker.client.TradingClient")
@patch("broker.client.OptionHistoricalDataClient")
async def test_executor_routes_sell_put_to_options_order(MockDataClient, MockTradingClient, tmp_path):
    """SELL_PUT signal must call submit_order (options path), not equity market order."""
    MockTradingClient.return_value.submit_order.return_value = _make_mock_order("opt-order-001")

    from broker.client import BrokerClient
    from database.migrations import init_db
    broker = BrokerClient()
    builder = OrderBuilder()
    init_db(str(tmp_path / "test.db"))
    executor = Executor(broker, builder, db_path=str(tmp_path / "test.db"))

    signal = _make_signal("SELL_PUT")
    await executor.execute_signal(signal=signal, quantity=1, current_price=Decimal("122.00"))

    assert MockTradingClient.return_value.submit_order.called
    call_args = MockTradingClient.return_value.submit_order.call_args[0][0]
    # Options orders use the contract symbol, not the underlying
    assert call_args.symbol == "AMD240119P00120000"


@pytest.mark.asyncio
@patch("broker.client.TradingClient")
@patch("broker.client.OptionHistoricalDataClient")
async def test_executor_skips_zero_quantity(MockDataClient, MockTradingClient, tmp_path):
    """Zero quantity signals are skipped without submitting."""
    from broker.client import BrokerClient
    from database.migrations import init_db
    broker = BrokerClient()
    builder = OrderBuilder()
    init_db(str(tmp_path / "test.db"))
    executor = Executor(broker, builder, db_path=str(tmp_path / "test.db"))

    signal = _make_signal("SELL_PUT")
    result = await executor.execute_signal(signal=signal, quantity=0)

    assert result is None
    assert not MockTradingClient.return_value.submit_order.called
