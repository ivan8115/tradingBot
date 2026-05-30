# Fill Metadata Enrichment — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enrich live Alpaca fill events with signal metadata (leg, contract_id, strategy_id, underlying_price) so the Wheel state machine advances correctly on real paper/live fills.

**Architecture:** The `Executor` stores a `{order_id → signal.metadata}` dict when it submits each options order. `TradingScheduler._on_fill` looks up this dict by `fill.order_id` and merges the metadata (plus corrects `strategy_id`) before routing to strategies. A companion change sets `client_order_id` on the Alpaca order request as a belt-and-suspenders fallback. One signal metadata field (`underlying_price`) is also added at signal generation time so it flows through cleanly.

**Tech Stack:** Python 3.12, alpaca-py (LimitOrderRequest `client_order_id` field), Pydantic v2 (`model_copy`), pytest.

Run tests with: `python3 -m pytest -p no:cacheprovider -q`

---

## File Map

| File | Change |
|---|---|
| `strategies/wheel/wheel_strategy.py` | Add `"underlying_price"` to CSP signal metadata |
| `broker/client.py` | Add `client_order_id` param to `submit_options_order` |
| `execution/executor.py` | Add `_pending_order_metadata` dict + `pop_pending_metadata()` method; populate on options submit; pass `client_order_id` to broker |
| `scheduler/scheduler.py` | Enrich fill metadata from pending registry in `_on_fill` before routing to strategies |
| `TODO.md` | Remove item #2 (now fixed) |
| `tests/unit/test_fill_metadata_enrichment.py` | New — 6 tests |

---

## Task 1: Fill Metadata Enrichment (single coupled task)

All four source changes must land together — the enrichment chain only works when every link is present.

**Files:**
- Modify: `strategies/wheel/wheel_strategy.py:333-345` (signal metadata)
- Modify: `broker/client.py:231-276` (`submit_options_order` signature + requests)
- Modify: `execution/executor.py:27-110` (pending registry)
- Modify: `scheduler/scheduler.py:587-614` (`_on_fill` enrichment)
- Modify: `TODO.md` (remove fixed item)
- Create: `tests/unit/test_fill_metadata_enrichment.py`

---

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_fill_metadata_enrichment.py` with exactly this content:

```python
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
```

- [ ] **Step 2: Run to confirm tests fail**

```bash
cd /home/ivan8115/git/tradingBot && python3 -m pytest -p no:cacheprovider -q tests/unit/test_fill_metadata_enrichment.py
```

Expected: `AttributeError: 'Executor' object has no attribute '_pending_order_metadata'` for most tests; the scheduler enrichment and end-to-end tests may already pass (they don't depend on executor yet).

- [ ] **Step 3: Add `underlying_price` to CSP signal metadata in `strategies/wheel/wheel_strategy.py`**

Find the `SignalEvent(...)` return at the end of `_evaluate_entry` (around line 327). The `metadata` dict currently ends with `"session_id": session_id`. Add `"underlying_price"` as the last entry before `"session_id"`:

```python
        return [SignalEvent(
            strategy_id=self.strategy_id,
            symbol=bar.symbol,
            signal_type="SELL_PUT",
            strength=min(1.0, iv_rank_val / 100.0),
            timestamp=bar.timestamp,
            metadata={
                "leg": "csp_open",
                "contract_id": contract.contract_id,
                "strike": float(contract.strike),
                "expiry": str(contract.expiry),
                "dte": contract.dte,
                "delta": greeks_delta,
                "premium": float(contract.mid),
                "iv_rank": iv_rank_val,
                "ai_strike_reasoning": ai_strike_reasoning,
                "collateral": float(contract.strike * 100),
                "underlying_price": float(bar.close),
                "session_id": session_id,
            },
        )]
```

- [ ] **Step 4: Add `client_order_id` to `submit_options_order` in `broker/client.py`**

Find the `submit_options_order` method. Add `client_order_id: str | None = None` to its signature and pass it to both request constructors:

```python
    def submit_options_order(
        self,
        contract_symbol: str,
        qty: int,
        side: str,
        order_type: str = "limit",
        limit_price: Decimal | None = None,
        client_order_id: str | None = None,
    ) -> str:
        """Submit an options order. Returns Alpaca order ID."""
        if order_type == "limit" and limit_price is None:
            raise ValueError("limit_price is required for limit orders")

        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

        try:
            if order_type == "limit":
                request = LimitOrderRequest(
                    symbol=contract_symbol,
                    qty=qty,
                    side=order_side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=float(limit_price),
                    client_order_id=client_order_id,
                )
            else:
                request = MarketOrderRequest(
                    symbol=contract_symbol,
                    qty=qty,
                    side=order_side,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=client_order_id,
                )

            logger.info(
                f"[Options] Submitting {order_type} {side} {qty}x {contract_symbol} "
                f"@ {limit_price if limit_price is not None else 'market'}"
            )
            order = self._client.submit_order(request)
            logger.info(f"[Options] Order submitted: {order.id}")
            return str(order.id)

        except Exception as e:
            msg = str(e).lower()
            if "insufficient" in msg or "buying power" in msg:
                raise InsufficientFundsError(
                    f"Insufficient funds for options order {contract_symbol}"
                ) from e
            raise OrderError(f"Options order failed for {contract_symbol}: {e}") from e
```

- [ ] **Step 5: Add pending registry to `Executor` in `execution/executor.py`**

**Change 1** — in `__init__`, add the pending dict as the last line:

```python
    def __init__(
        self,
        broker: BrokerClient,
        order_builder: OrderBuilder,
        db_path: str = "data/trading.db",
    ) -> None:
        self._broker = broker
        self._builder = order_builder
        self._session_factory = get_session_factory(db_path)
        self._pending_order_metadata: dict[str, dict] = {}
```

**Change 2** — in `execute_signal`, add `client_order_id` and metadata stash inside the `if is_options:` block. Replace the `alpaca_id = self._broker.submit_options_order(...)` call with:

```python
                alpaca_id = self._broker.submit_options_order(
                    contract_symbol=contract_id,
                    qty=quantity,
                    side=side,
                    order_type="limit" if limit_price else "market",
                    limit_price=limit_price,
                    client_order_id=signal.strategy_id,
                )
                # Store metadata so _on_fill can enrich bare live fills
                self._pending_order_metadata[alpaca_id] = {
                    "strategy_id": signal.strategy_id,
                    **signal.metadata,
                }
```

**Change 3** — add `pop_pending_metadata` method after `record_rejected_signal`:

```python
    def pop_pending_metadata(self, order_id: str) -> dict | None:
        """
        Retrieve and remove pending signal metadata for an order.
        Returns None if the order is not in the registry (e.g. assignment fills,
        equity fills, or orders that were cancelled before filling).
        """
        return self._pending_order_metadata.pop(order_id, None)
```

- [ ] **Step 6: Inject pending metadata in `scheduler/scheduler.py` `_on_fill`**

Find `_on_fill` (around line 587). Add the enrichment block at the very top of the method, before the `logger.info(...)` call:

```python
    async def _on_fill(self, fill: FillEvent) -> None:
        # Live Alpaca fills arrive with empty metadata (no leg/contract_id/strategy_id).
        # Enrich from the executor's pending order registry, keyed by Alpaca order_id.
        pending = self._executor.pop_pending_metadata(fill.order_id)
        if pending:
            strategy_id = pending.pop("strategy_id", None)
            fill.metadata.update(pending)
            if strategy_id and strategy_id != fill.strategy_id:
                fill = fill.model_copy(update={"strategy_id": strategy_id})

        logger.info(
            f"Fill received: {fill.side.upper()} {fill.filled_qty}x "
            f"{fill.symbol} @ ${fill.fill_price}"
        )
        self._portfolio.apply_fill(fill)
        self._executor.record_fill(fill)
        alerter.fill_alert(
            symbol=fill.symbol,
            side=fill.side,
            qty=fill.filled_qty,
            price=float(fill.fill_price),
            strategy=fill.strategy_id,
        )

        # Check drawdown after every fill
        dd = float(self._portfolio.drawdown())
        if dd >= settings.risk.max_drawdown_pct:
            alerter.drawdown_alert(dd * 100, settings.risk.max_drawdown_pct * 100)
            logger.critical(f"MAX DRAWDOWN BREACHED: {dd*100:.1f}% — halting new orders")

        for strategy in self._strategies:
            try:
                strategy.on_fill(fill)
            except Exception as e:
                logger.error(f"Strategy {strategy.strategy_id} fill error: {e}")

        self._save_strategy_state()
```

- [ ] **Step 7: Run new tests — expect all pass**

```bash
cd /home/ivan8115/git/tradingBot && python3 -m pytest -p no:cacheprovider -q tests/unit/test_fill_metadata_enrichment.py
```

Expected: 6/6 pass.

- [ ] **Step 8: Run full suite — no regressions**

```bash
cd /home/ivan8115/git/tradingBot && python3 -m pytest -p no:cacheprovider -q tests/
```

Expected: all tests pass. Pay attention to `test_options_executor.py` (it tests `submit_options_order` and `Executor` directly) and `test_assignment.py` (assignment fills must still work — they have `leg="assignment"` already so the enrichment block is a no-op for them).

- [ ] **Step 9: Remove the fixed TODO item**

In `TODO.md`, remove item #2 entirely ("Live Alpaca fills missing `leg` / `contract_id` metadata — Wheel state machine stalls") and renumber the remaining items so #3 becomes #2 and #4 becomes #3.

- [ ] **Step 10: Commit**

```bash
cd /home/ivan8115/git/tradingBot && git add \
  strategies/wheel/wheel_strategy.py \
  broker/client.py \
  execution/executor.py \
  scheduler/scheduler.py \
  TODO.md \
  tests/unit/test_fill_metadata_enrichment.py \
  && git commit -m "fix: enrich live fills with signal metadata — Wheel state machine now advances on real Alpaca fills"
```

---

## Self-Review

**Spec coverage:**
- [x] `underlying_price` added to CSP signal metadata → Step 3
- [x] `client_order_id` set on Alpaca order → Step 4
- [x] Executor `_pending_order_metadata` dict populated on options submit → Step 5
- [x] Executor `pop_pending_metadata()` method → Step 5
- [x] `scheduler._on_fill` enriches fill before routing → Step 6
- [x] TODO.md item removed → Step 9
- [x] Tests for each layer of the fix → Step 1

**Placeholder scan:** All code blocks are complete. No TBDs.

**Type consistency:**
- `pop_pending_metadata(order_id: str) -> dict | None` — called as `self._executor.pop_pending_metadata(fill.order_id)` in scheduler; `fill.order_id` is `str` ✓
- `pending.pop("strategy_id", None)` modifies the local copy, not the stored dict (since `pop` already removed the entry from `_pending_order_metadata`) ✓
- `fill.model_copy(update={"strategy_id": strategy_id})` — Pydantic v2 `model_copy` returns a new instance of the same model; reassigned to `fill` local variable ✓
- `fill.metadata.update(pending)` — `metadata` is `dict[str, Any]`, mutable in Pydantic v2 ✓
