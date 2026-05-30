"""
Tests for fill metadata enrichment.

Live Alpaca fills arrive with empty metadata (no leg, contract_id, strategy_id).
The Executor stores signal metadata keyed by order_id at submit time.
TradingScheduler._on_fill looks it up and injects it before routing to strategies.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from core.events import FillEvent, SignalEvent
from execution.executor import Executor
from execution.order_builder import OrderBuilder


def _make_options_signal(
    strategy_id: str = "wheel",
    contract_id: str = "AMD240119P00280000",
) -> SignalEvent:
    return SignalEvent(
        strategy_id=strategy_id,
        symbol="AMD",
        signal_type="SELL_PUT",
        strength=0.8,
        timestamp=datetime.now(timezone.utc),
        metadata={
            "leg": "csp_open",
            "contract_id": contract_id,
            "strike": 28.0,
            "premium": 1.50,
            "delta": -0.28,
            "collateral": 2800.0,
            "underlying_price": 30.0,
        },
    )


def _bare_fill(order_id: str) -> FillEvent:
    """Simulates a live Alpaca fill — empty metadata, strategy_id='unknown'."""
    return FillEvent(
        order_id=order_id,
        symbol="AMD240119P00280000",
        strategy_id="unknown",
        side="sell",
        filled_qty=1,
        fill_price=Decimal("1.50"),
        commission=Decimal("0"),
        is_options=True,
        option_contract_id="AMD240119P00280000",
        filled_at=datetime.now(timezone.utc),
        metadata={},
    )


# ---------------------------------------------------------------------------
# Executor: pending registry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("broker.client.TradingClient")
@patch("broker.client.OptionHistoricalDataClient")
async def test_executor_stores_metadata_on_options_submit(MockData, MockTrading, tmp_path):
    """After submitting a SELL_PUT, _pending_order_metadata must contain leg/contract_id/strategy_id."""
    mock_order = MagicMock()
    mock_order.id = "alpaca-abc123"
    MockTrading.return_value.submit_order.return_value = mock_order

    from broker.client import BrokerClient
    from database.migrations import init_db
    broker = BrokerClient()
    init_db(str(tmp_path / "test.db"))
    executor = Executor(broker, OrderBuilder(), db_path=str(tmp_path / "test.db"))

    signal = _make_options_signal()
    await executor.execute_signal(signal=signal, quantity=1, current_price=Decimal("30.0"))

    assert "alpaca-abc123" in executor._pending_order_metadata
    stored = executor._pending_order_metadata["alpaca-abc123"]
    assert stored["leg"] == "csp_open"
    assert stored["contract_id"] == "AMD240119P00280000"
    assert stored["strategy_id"] == "wheel"
    assert stored["underlying_price"] == 30.0


def test_pop_pending_metadata_returns_and_removes():
    """pop_pending_metadata must return the entry and delete it in one call."""
    executor = Executor.__new__(Executor)
    executor._pending_order_metadata = {
        "order-123": {"leg": "csp_open", "strategy_id": "wheel"},
    }

    result = executor.pop_pending_metadata("order-123")
    assert result == {"leg": "csp_open", "strategy_id": "wheel"}
    assert "order-123" not in executor._pending_order_metadata


def test_pop_pending_metadata_returns_none_for_unknown_order():
    """pop_pending_metadata must return None for orders not in the registry."""
    executor = Executor.__new__(Executor)
    executor._pending_order_metadata = {}
    assert executor.pop_pending_metadata("not-here") is None


@pytest.mark.asyncio
@patch("broker.client.TradingClient")
@patch("broker.client.OptionHistoricalDataClient")
async def test_executor_skips_pending_registry_for_equity_signals(MockData, MockTrading, tmp_path):
    """ENTRY_LONG signals (equity) must NOT add entries to _pending_order_metadata."""
    mock_order = MagicMock()
    mock_order.id = "equity-xyz"
    MockTrading.return_value.submit_order.return_value = mock_order

    from broker.client import BrokerClient
    from database.migrations import init_db
    broker = BrokerClient()
    init_db(str(tmp_path / "test.db"))
    executor = Executor(broker, OrderBuilder(), db_path=str(tmp_path / "test.db"))

    equity_signal = SignalEvent(
        strategy_id="swing",
        symbol="AMD",
        signal_type="ENTRY_LONG",
        strength=0.8,
        timestamp=datetime.now(timezone.utc),
        metadata={"close": 30.0, "atr": 1.0, "stop_loss": 28.0, "take_profit": 34.0},
    )
    await executor.execute_signal(
        signal=equity_signal, quantity=10, current_price=Decimal("30.0")
    )
    assert "equity-xyz" not in executor._pending_order_metadata


# ---------------------------------------------------------------------------
# Scheduler enrichment logic
# ---------------------------------------------------------------------------

def test_scheduler_enrichment_injects_metadata_and_strategy_id():
    """
    Simulate the enrichment block in scheduler._on_fill:
    a bare fill gets metadata + corrected strategy_id from the pending registry.
    """
    fill = _bare_fill("order-enrich-test")
    pending = {
        "leg": "csp_open",
        "contract_id": "AMD240119P00280000",
        "underlying_price": 30.0,
        "strategy_id": "wheel",
    }

    # Replicate scheduler enrichment logic
    strategy_id = pending.pop("strategy_id", None)
    fill.metadata.update(pending)
    if strategy_id and strategy_id != fill.strategy_id:
        fill = fill.model_copy(update={"strategy_id": strategy_id})

    assert fill.metadata["leg"] == "csp_open"
    assert fill.metadata["contract_id"] == "AMD240119P00280000"
    assert fill.metadata["underlying_price"] == 30.0
    assert fill.strategy_id == "wheel"


def test_enriched_fill_creates_csp_position_in_wheel():
    """
    End-to-end smoke test: a fill with the correct metadata (as produced by
    the enrichment path) must result in CSPPosition being created by on_fill.
    """
    from strategies.wheel.wheel_strategy import WheelStrategy, WheelPosition, WheelState
    from strategies.wheel.csp_leg import CSPPosition, OptionContract

    with patch("strategies.wheel.wheel_strategy.settings") as ms:
        ms.indicators.bar_window_size = 200
        ms.strategies.wheel.csp.min_iv_rank = 40
        ms.strategies.wheel.csp.pain_threshold_default = 0.85
        w = WheelStrategy.__new__(WheelStrategy)
    w.symbols = ["AMD"]
    w.strategy_id = "wheel"
    w._advisor = None
    w._positions = {"AMD": WheelPosition(symbol="AMD")}
    w._csp_leg = MagicMock()
    w._csp_leg.cost_basis_after_assignment.return_value = Decimal("26.80")
    w._cc_leg = MagicMock()

    contract = MagicMock(spec=OptionContract)
    contract.contract_id = "AMD240119P00280000"
    w._positions["AMD"].cached_chain = [contract]

    # Fill that arrives already enriched (as if scheduler injected the metadata)
    fill = FillEvent(
        order_id="live-order-001",
        symbol="AMD240119P00280000",
        strategy_id="wheel",
        side="sell",
        filled_qty=1,
        fill_price=Decimal("1.50"),
        commission=Decimal("0"),
        is_options=True,
        option_contract_id="AMD240119P00280000",
        filled_at=datetime.now(timezone.utc),
        metadata={
            "leg": "csp_open",
            "contract_id": "AMD240119P00280000",
            "underlying_price": 30.0,
        },
    )

    w.on_fill(fill)

    pos = w._positions["AMD"]
    assert pos.csp_position is not None, "CSPPosition must be created after enriched fill"
    assert isinstance(pos.csp_position, CSPPosition)
    assert pos.state == WheelState.CSP_OPEN
    assert pos.csp_position.underlying_price_at_entry == Decimal("30.0")
