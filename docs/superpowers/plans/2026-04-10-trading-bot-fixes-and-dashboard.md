# Trading Bot Core Fixes + Web Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 6 critical gaps preventing the Wheel Strategy from trading options on paper, then add a FastAPI web dashboard for live monitoring.

**Architecture:** The bot's event-driven pipeline (bar → strategy → risk → executor → broker) already exists; we're completing missing pieces — options chain fetching, options order routing, assignment detection, roll logic, and Wheel wiring. The dashboard is a separate process reading the same SQLite DB and Alpaca API, communicating with the browser via WebSocket.

**Tech Stack:** Python 3.11+, alpaca-py, FastAPI, uvicorn, Chart.js (CDN), SQLAlchemy, APScheduler, asyncio, loguru, pytest

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `broker/client.py` | Modify | Add `get_options_chain()` and `submit_options_order()` |
| `broker/market_data.py` | Modify | Detect assignment events from `trade_updates` stream |
| `core/config.py` | Modify | Add `roll_when_dte` to `CSPConfig` |
| `execution/executor.py` | Modify | Route options signals to options-specific order submission |
| `scheduler/scheduler.py` | Modify | Add chain refresh job; write `data/strategy_state.json` |
| `strategies/wheel/csp_leg.py` | Modify | Use configurable `roll_when_dte` instead of hardcoded 7 |
| `main.py` | Modify | Instantiate WheelStrategy in `cmd_trade` |
| `config.yaml` | Modify | Update Wheel symbols to AMD + MARA; add CSP `roll_when_dte` |
| `dashboard/__init__.py` | Create | Package marker |
| `dashboard/app.py` | Create | FastAPI app: 6 REST endpoints + WebSocket |
| `dashboard/static/index.html` | Create | Single-page UI (Chart.js, vanilla JS) |
| `tests/unit/test_options_chain.py` | Create | Tests for chain normalization |
| `tests/unit/test_options_executor.py` | Create | Tests for options order routing |
| `tests/unit/test_assignment.py` | Create | Tests for assignment FillEvent creation |
| `tests/unit/test_roll_logic.py` | Create | Tests for configurable roll_when_dte |
| `tests/unit/test_dashboard_api.py` | Create | Tests for FastAPI endpoints |

---

## Task 1: Options Chain Fetching in BrokerClient

**Files:**
- Modify: `broker/client.py`
- Create: `tests/unit/test_options_chain.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_options_chain.py`:

```python
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
    bid="2.00",
    ask="2.20",
    delta="-0.28",
    iv="0.45",
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
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /home/ivan8115/git/tradingBot
python -m pytest tests/unit/test_options_chain.py -v
```

Expected: `FAILED` — `get_options_chain` doesn't exist yet.

- [ ] **Step 3: Add options chain imports and `get_options_chain()` to `broker/client.py`**

Add these imports at the top of `broker/client.py` after existing imports:

```python
from datetime import date, timedelta

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionSnapshotRequest
from alpaca.trading.requests import GetOptionContractsRequest
from alpaca.trading.enums import ContractType

from strategies.wheel.csp_leg import OptionContract
```

Update `BrokerClient.__init__` to also instantiate the data client:

```python
def __init__(self) -> None:
    self._client = TradingClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        paper=settings.alpaca_paper,
    )
    self._option_data = OptionHistoricalDataClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
    )
    mode = "PAPER" if settings.alpaca_paper else "LIVE"
    logger.info(f"BrokerClient initialized [{mode}]")
```

Add this method to `BrokerClient` after `get_position()`:

```python
# ------------------------------------------------------------------
# Options
# ------------------------------------------------------------------

def get_options_chain(
    self,
    symbol: str,
    dte_min: int = 21,
    dte_max: int = 45,
    option_type: str = "put",     # "put" | "call" | "both"
) -> list[OptionContract]:
    """
    Fetch options chain from Alpaca and return normalized OptionContract list.
    Filters by DTE window. Returns [] on any error (non-fatal).
    """
    today = date.today()
    exp_gte = today + timedelta(days=dte_min)
    exp_lte = today + timedelta(days=dte_max)

    try:
        contract_type = None
        if option_type == "put":
            contract_type = ContractType.PUT
        elif option_type == "call":
            contract_type = ContractType.CALL

        req = GetOptionContractsRequest(
            underlying_symbols=[symbol],
            expiration_date_gte=exp_gte,
            expiration_date_lte=exp_lte,
            type=contract_type,
        )
        resp = self._client.get_option_contracts(req)
        raw_contracts = resp.option_contracts if resp else []
    except Exception as e:
        logger.warning(f"[Options] Failed to fetch chain for {symbol}: {e}")
        return []

    if not raw_contracts:
        return []

    # Fetch snapshots for greeks + quotes
    contract_symbols = [c.symbol for c in raw_contracts]
    snapshots: dict = {}
    try:
        snap_req = OptionSnapshotRequest(symbol_or_symbols=contract_symbols)
        snapshots = self._option_data.get_option_snapshot(snap_req)
    except Exception as e:
        logger.warning(f"[Options] Snapshot fetch failed for {symbol}: {e}")
        # Continue — use close_price fallback

    result: list[OptionContract] = []
    for raw in raw_contracts:
        expiry = raw.expiration_date
        if isinstance(expiry, str):
            expiry = date.fromisoformat(expiry)
        dte = (expiry - today).days
        if not (dte_min <= dte <= dte_max):
            continue

        snap = snapshots.get(raw.symbol)
        if snap and snap.latest_quote:
            bid = Decimal(str(snap.latest_quote.bid_price or 0))
            ask = Decimal(str(snap.latest_quote.ask_price or 0))
        else:
            mid_fallback = Decimal(str(raw.close_price or 0))
            bid = mid_fallback * Decimal("0.95")
            ask = mid_fallback * Decimal("1.05")

        if snap and snap.greeks:
            delta = float(snap.greeks.delta or 0)
            iv = float(snap.greeks.implied_volatility or 0)
        else:
            delta = 0.0
            iv = 0.0

        result.append(OptionContract(
            symbol=symbol,
            contract_id=raw.symbol,
            option_type=raw.type.value if hasattr(raw.type, "value") else str(raw.type),
            strike=Decimal(str(raw.strike_price)),
            expiry=expiry,
            dte=dte,
            bid=bid,
            ask=ask,
            delta=delta,
            iv=iv,
            open_interest=int(raw.open_interest or 0),
            volume=int(raw.volume or 0),
        ))

    logger.info(f"[Options] {symbol}: {len(result)} contracts fetched (DTE {dte_min}-{dte_max})")
    return result
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python -m pytest tests/unit/test_options_chain.py -v
```

Expected: All 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add broker/client.py tests/unit/test_options_chain.py
git commit -m "feat: add BrokerClient.get_options_chain() with DTE filtering and Greeks"
```

---

## Task 2: Options Order Submission in BrokerClient

**Files:**
- Modify: `broker/client.py`
- Create: `tests/unit/test_options_executor.py` (broker tests now; executor tests added in Task 4)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_options_executor.py`:

```python
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
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/unit/test_options_executor.py -v
```

Expected: FAIL — `submit_options_order` not yet defined.

- [ ] **Step 3: Add `submit_options_order()` to `broker/client.py`**

Add these imports alongside the existing options imports from Task 1:

```python
from alpaca.trading.requests import OptionLimitOrderRequest, OptionMarketOrderRequest
```

Add the method to `BrokerClient` after `get_options_chain()`:

```python
def submit_options_order(
    self,
    contract_symbol: str,     # OCC format: "AMD240119P00120000"
    qty: int,
    side: str,                # "buy" | "sell"
    order_type: str = "limit",
    limit_price: Decimal | None = None,
    time_in_force: str = "day",
) -> str:
    """Submit an options order. Returns Alpaca order ID."""
    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

    try:
        if order_type == "limit" and limit_price is not None:
            request = OptionLimitOrderRequest(
                symbol=contract_symbol,
                qty=qty,
                side=order_side,
                time_in_force=TimeInForce.DAY,
                limit_price=float(limit_price),
            )
        else:
            request = OptionMarketOrderRequest(
                symbol=contract_symbol,
                qty=qty,
                side=order_side,
                time_in_force=TimeInForce.DAY,
            )

        logger.info(
            f"[Options] Submitting {order_type} {side} {qty}x {contract_symbol} "
            f"@ {limit_price or 'market'}"
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

**Note:** If `OptionLimitOrderRequest`/`OptionMarketOrderRequest` are absent in the installed alpaca-py version, fall back to `LimitOrderRequest(symbol=contract_symbol, ...)` — Alpaca accepts the OCC symbol directly as the `symbol` field for options orders.

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python -m pytest tests/unit/test_options_executor.py -v
```

Expected: Both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add broker/client.py tests/unit/test_options_executor.py
git commit -m "feat: add BrokerClient.submit_options_order() for options execution"
```

---

## Task 3: Assignment Detection in MarketDataStream

**Files:**
- Modify: `broker/market_data.py`
- Create: `tests/unit/test_assignment.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_assignment.py`:

```python
"""Tests for assignment detection from Alpaca trade_updates stream."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from broker.market_data import MarketDataStream
from core.events import FillEvent


def _make_assignment_update(symbol="AMD", qty=100, strike=120.0):
    update = MagicMock()
    update.event = "assigned"
    update.timestamp = datetime.now(timezone.utc)

    order = MagicMock()
    order.id = "assign-order-001"
    order.symbol = symbol
    order.client_order_id = "wheel"
    order.side = MagicMock()
    order.side.value = "sell"

    update.order = order
    update.qty = str(qty)
    update.price = str(strike)
    return update


@pytest.mark.asyncio
async def test_assignment_creates_fill_event_with_leg_metadata():
    """Assignment event from Alpaca must become a FillEvent with leg='assignment'."""
    received: list[FillEvent] = []

    async def capture_fill(fill: FillEvent) -> None:
        received.append(fill)

    stream = MarketDataStream()
    stream._fill_handler = capture_fill

    update = _make_assignment_update(symbol="AMD", qty=100, strike=120.0)
    await stream._on_trade_update(update)

    assert len(received) == 1
    fill = received[0]
    assert fill.symbol == "AMD"
    assert fill.filled_qty == 100
    assert fill.metadata.get("leg") == "assignment"
    assert fill.is_options is True
    assert fill.strategy_id == "wheel"


@pytest.mark.asyncio
async def test_non_assignment_fill_event_is_not_flagged():
    """Regular 'new' order acknowledgements are ignored."""
    received: list[FillEvent] = []

    async def capture_fill(fill: FillEvent) -> None:
        received.append(fill)

    stream = MarketDataStream()
    stream._fill_handler = capture_fill

    update = MagicMock()
    update.event = "new"
    await stream._on_trade_update(update)

    assert received == []
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/unit/test_assignment.py -v
```

Expected: FAIL — `_on_trade_update` does not handle `"assigned"` events.

- [ ] **Step 3: Update `_on_trade_update` in `broker/market_data.py`**

Replace the existing `_on_trade_update` method with:

```python
async def _on_trade_update(self, update) -> None:
    """Convert Alpaca trade update to FillEvent for fills and assignments."""
    try:
        event_type = getattr(update, "event", None)

        # --- Assignment handling ---
        if event_type == "assigned":
            order = update.order
            qty = int(float(getattr(update, "qty", 0) or 0))
            price = getattr(update, "price", None)

            fill = FillEvent(
                order_id=str(order.id),
                symbol=order.symbol,
                strategy_id=order.client_order_id or "wheel",
                side="sell",
                filled_qty=qty,
                fill_price=Decimal(str(price)) if price else Decimal("0"),
                commission=Decimal("0"),
                is_options=True,
                filled_at=update.timestamp,
                metadata={"leg": "assignment", "quantity": qty},
            )
            if self._fill_handler:
                await self._fill_handler(fill)
            return

        # --- Normal fill handling ---
        if event_type not in ("fill", "partial_fill"):
            return

        order = update.order
        fill_qty = int(getattr(update, "qty", 0) or 0)
        fill_price_raw = getattr(update, "price", None)
        if not fill_price_raw or fill_qty == 0:
            return

        is_options = getattr(order, "asset_class", None) == "us_option"

        fill = FillEvent(
            order_id=str(order.id),
            symbol=order.symbol,
            strategy_id=order.client_order_id or "unknown",
            side="buy" if order.side.value == "buy" else "sell",
            filled_qty=fill_qty,
            fill_price=Decimal(str(fill_price_raw)),
            commission=Decimal("0"),
            is_options=is_options,
            option_contract_id=order.symbol if is_options else None,
            filled_at=update.timestamp,
        )

        if self._fill_handler:
            await self._fill_handler(fill)

    except Exception as e:
        logger.error(f"Error processing trade update: {e}")
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python -m pytest tests/unit/test_assignment.py -v
```

Expected: Both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add broker/market_data.py tests/unit/test_assignment.py
git commit -m "feat: detect assignment events from Alpaca and emit FillEvent with leg=assignment"
```

---

## Task 4: Options Order Routing in Executor

**Files:**
- Modify: `execution/executor.py`
- Modify: `tests/unit/test_options_executor.py` (append executor tests)

- [ ] **Step 1: Append executor tests to `tests/unit/test_options_executor.py`**

```python
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
```

- [ ] **Step 2: Run to confirm the new tests fail**

```bash
python -m pytest tests/unit/test_options_executor.py -v -k "test_executor"
```

Expected: FAIL — executor uses equity path for options signals.

- [ ] **Step 3: Update the broker submission block in `execution/executor.py`**

Replace the broker submission block (the `if order.order_type == "market":` ... `else:` block) with:

```python
        # Submit to broker
        try:
            OPTIONS_SIGNAL_TYPES = {
                "SELL_PUT", "SELL_CALL", "BUY_TO_CLOSE_PUT", "BUY_TO_CLOSE_CALL"
            }
            is_options = signal.signal_type in OPTIONS_SIGNAL_TYPES
            buy_side = signal.signal_type in (
                "ENTRY_LONG", "BUY_TO_CLOSE_PUT", "BUY_TO_CLOSE_CALL"
            )
            side = "buy" if buy_side else "sell"

            if is_options:
                contract_id = signal.metadata.get("contract_id")
                if not contract_id:
                    logger.error(f"Options signal missing contract_id: {signal.symbol}")
                    return None
                premium = signal.metadata.get("premium")
                limit_price = Decimal(str(premium)) if premium else None
                alpaca_id = self._broker.submit_options_order(
                    contract_symbol=contract_id,
                    qty=quantity,
                    side=side,
                    order_type="limit" if limit_price else "market",
                    limit_price=limit_price,
                )
            elif order.order_type == "market":
                alpaca_id = self._broker.submit_market_order(
                    symbol=signal.symbol,
                    qty=quantity,
                    side=side,
                )
            else:
                if not order.limit_price:
                    logger.error(f"Limit order has no price for {signal.symbol}")
                    return None
                alpaca_id = self._broker.submit_limit_order(
                    symbol=signal.symbol,
                    qty=quantity,
                    side=side,
                    limit_price=order.limit_price,
                )
```

- [ ] **Step 4: Run all executor tests**

```bash
python -m pytest tests/unit/test_options_executor.py -v
```

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add execution/executor.py tests/unit/test_options_executor.py
git commit -m "feat: route options signals to submit_options_order in Executor"
```

---

## Task 5: Configurable Roll Logic for CSP Leg

**Files:**
- Modify: `core/config.py`
- Modify: `strategies/wheel/csp_leg.py`
- Modify: `config.yaml`
- Create: `tests/unit/test_roll_logic.py`

- [ ] **Step 1: Find `CSPConfig` in `core/config.py`**

```bash
grep -n "CSPConfig\|roll_when_dte" core/config.py
```

Note the class definition and existing fields.

- [ ] **Step 2: Write the failing tests**

Create `tests/unit/test_roll_logic.py`:

```python
"""Tests for configurable roll_when_dte in CSP leg."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from strategies.wheel.csp_leg import CashSecuredPutLeg, CSPPosition, OptionContract
from core.config import CSPConfig


def _make_csp_config(roll_when_dte: int = 7) -> CSPConfig:
    return CSPConfig(
        target_delta=-0.28,
        min_dte=21,
        max_dte=45,
        profit_target_pct=0.50,
        stop_loss_multiplier=2.0,
        min_premium=1.00,
        min_iv_rank=50,
        roll_when_dte=roll_when_dte,
    )


def _make_position(dte: int, premium: float = 2.0) -> CSPPosition:
    contract = OptionContract(
        symbol="AMD",
        contract_id="AMD240119P00120000",
        option_type="put",
        strike=Decimal("120"),
        expiry=date.today(),
        dte=dte,
        bid=Decimal("2.00"),
        ask=Decimal("2.20"),
        delta=-0.28,
        iv=0.45,
    )
    return CSPPosition(
        symbol="AMD",
        contract=contract,
        premium_received=Decimal(str(premium)),
        opened_at=datetime.now(timezone.utc),
    )


def test_csp_closes_when_dte_at_threshold():
    """should_close_early returns True when DTE equals roll_when_dte from config."""
    leg = CashSecuredPutLeg(_make_csp_config(roll_when_dte=7))
    pos = _make_position(dte=7)
    should_close, reason = leg.should_close_early(pos, Decimal("2.00"))
    assert should_close is True
    assert "7" in reason


def test_csp_uses_config_roll_when_dte_not_hardcoded():
    """roll_when_dte=10 closes at DTE=10, not just DTE=7."""
    leg = CashSecuredPutLeg(_make_csp_config(roll_when_dte=10))
    pos = _make_position(dte=10)
    should_close, reason = leg.should_close_early(pos, Decimal("2.00"))
    assert should_close is True
    assert "10" in reason


def test_csp_does_not_close_when_dte_above_threshold():
    """should_close_early returns False when DTE > roll_when_dte and no P&L trigger."""
    leg = CashSecuredPutLeg(_make_csp_config(roll_when_dte=7))
    pos = _make_position(dte=15)
    should_close, _ = leg.should_close_early(pos, Decimal("2.00"))
    assert should_close is False
```

- [ ] **Step 3: Run tests to confirm failure**

```bash
python -m pytest tests/unit/test_roll_logic.py -v
```

Expected: FAIL — `CSPConfig` has no `roll_when_dte` field.

- [ ] **Step 4: Add `roll_when_dte` to `CSPConfig` in `core/config.py`**

Find the `CSPConfig` class and add `roll_when_dte: int = 7` as a field:

```python
class CSPConfig(BaseModel):
    target_delta: float = -0.28
    min_dte: int = 21
    max_dte: int = 45
    profit_target_pct: float = 0.50
    stop_loss_multiplier: float = 2.0
    min_premium: float = 1.00
    min_iv_rank: float = 50.0
    roll_when_dte: int = 7       # close/roll when DTE reaches this threshold
```

- [ ] **Step 5: Update `csp_leg.py` to use `self._cfg.roll_when_dte`**

In `CashSecuredPutLeg.should_close_early()`, replace the hardcoded DTE check:

```python
        # Before (hardcoded):
        # if position.contract.dte <= 7:
        #     return True, f"DTE={position.contract.dte} <= 7 — closing to avoid gamma risk"

        # After (from config):
        if position.contract.dte <= self._cfg.roll_when_dte:
            return True, (
                f"DTE={position.contract.dte} <= {self._cfg.roll_when_dte} — closing for roll"
            )
```

- [ ] **Step 6: Update `config.yaml` — add CSP roll_when_dte and update symbols**

Replace the `strategies.wheel` block in `config.yaml`:

```yaml
strategies:
  wheel:
    enabled: true
    symbols:
      - AMD
      - MARA
    csp:
      target_delta: -0.28
      min_dte: 21
      max_dte: 45
      profit_target_pct: 0.50
      stop_loss_multiplier: 2.0
      min_premium: 0.50
      min_iv_rank: 40
      roll_when_dte: 7
    cc:
      target_delta: 0.30
      min_dte: 21
      max_dte: 45
      profit_target_pct: 0.50
      roll_when_dte: 7
```

- [ ] **Step 7: Run roll logic tests**

```bash
python -m pytest tests/unit/test_roll_logic.py -v
```

Expected: All 3 tests PASS.

- [ ] **Step 8: Commit**

```bash
git add core/config.py strategies/wheel/csp_leg.py config.yaml tests/unit/test_roll_logic.py
git commit -m "feat: make CSP roll_when_dte configurable; update Wheel symbols for <$25K account"
```

---

## Task 6: Options Chain Refresh Job + Strategy State File in Scheduler

**Files:**
- Modify: `scheduler/scheduler.py`

- [ ] **Step 1: Add imports at the top of `scheduler/scheduler.py`**

```python
import json
from pathlib import Path
from strategies.wheel.wheel_strategy import WheelStrategy
```

- [ ] **Step 2: Detect WheelStrategy instances in `__init__`**

After the existing assignments in `__init__`, add:

```python
        self._wheel_strategies: list[WheelStrategy] = [
            s for s in strategies if isinstance(s, WheelStrategy)
        ]
```

- [ ] **Step 3: Register the chain refresh job in `setup()`**

After the existing scheduler jobs in `setup()`, add:

```python
        # Options chain refresh every 15 minutes during market hours
        self._scheduler.add_job(
            self._refresh_options_chains,
            IntervalTrigger(minutes=cfg.options_check_interval_minutes),
            id="options_chain_refresh",
        )
```

- [ ] **Step 4: Add `_refresh_options_chains()`, update `_check_dte_warnings()`, and add `_save_strategy_state()`**

Add these three methods to `TradingScheduler`:

```python
    async def _refresh_options_chains(self) -> None:
        """Fetch live options chain for each Wheel symbol and push to strategy."""
        if not self._is_market_open():
            return
        for wheel in self._wheel_strategies:
            for symbol in wheel.symbols:
                try:
                    chain = self._broker.get_options_chain(
                        symbol=symbol,
                        dte_min=settings.strategies.wheel.csp.min_dte,
                        dte_max=settings.strategies.wheel.csp.max_dte,
                        option_type="both",
                    )
                    wheel.update_options_chain(symbol, chain)
                    logger.debug(
                        f"[Scheduler] Chain refreshed: {symbol} ({len(chain)} contracts)"
                    )
                except Exception as e:
                    logger.warning(f"[Scheduler] Chain refresh failed for {symbol}: {e}")

    async def _check_dte_warnings(self) -> None:
        """Log structured warnings for positions approaching roll threshold."""
        if not self._is_market_open():
            return
        for wheel in self._wheel_strategies:
            state = wheel.get_state()
            for symbol, data in state.items():
                wheel_state = data.get("state")
                if wheel_state not in ("csp_open", "cc_open"):
                    continue
                pos = wheel._positions.get(symbol)  # type: ignore[attr-defined]
                if not pos:
                    continue
                contract = None
                if pos.csp_position:
                    contract = pos.csp_position.contract
                elif pos.cc_position:
                    contract = pos.cc_position.contract
                if contract:
                    threshold = settings.strategies.wheel.cc.roll_when_dte
                    if contract.dte <= threshold + 3:
                        logger.warning(
                            f"[DTE] {symbol} {wheel_state}: DTE={contract.dte} "
                            f"approaching roll threshold={threshold}"
                        )

    def _save_strategy_state(self) -> None:
        """Persist all strategy states to data/strategy_state.json for the dashboard."""
        state: dict = {}
        for strategy in self._strategies:
            try:
                state[strategy.strategy_id] = strategy.get_state()
            except Exception as e:
                logger.warning(f"Could not save state for {strategy.strategy_id}: {e}")
        path = Path(settings.system.db_path).parent / "strategy_state.json"
        try:
            path.write_text(json.dumps(state, default=str))
        except Exception as e:
            logger.warning(f"Could not write strategy_state.json: {e}")
```

- [ ] **Step 5: Call `_save_strategy_state()` at end of `_on_fill()`**

At the very end of the `_on_fill` method, after the `for strategy in self._strategies` loop, add:

```python
        # Persist updated strategy state for dashboard
        self._save_strategy_state()
```

- [ ] **Step 6: Run existing tests to verify nothing broke**

```bash
python -m pytest tests/unit/ -v
```

Expected: All tests PASS.

- [ ] **Step 7: Commit**

```bash
git add scheduler/scheduler.py
git commit -m "feat: add options chain refresh job and strategy state persistence to scheduler"
```

---

## Task 7: Wire Wheel Strategy into Live Trading

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Update the strategy-loading block in `cmd_trade()` in `main.py`**

Replace lines 239–251 (the `# Build active strategies` block):

```python
    # Build active strategies
    strategies = []
    if settings.strategies.momentum.enabled:
        syms = settings.strategies.momentum.symbols or settings.universe.watchlist
        strategies.append(MomentumStrategy(syms))

    if settings.strategies.wheel.enabled:
        from strategies.wheel.wheel_strategy import WheelStrategy
        wheel_syms = settings.strategies.wheel.symbols
        strategies.append(WheelStrategy(wheel_syms))

    if not strategies:
        print("No strategies enabled. Check config.yaml.")
        return

    for s in strategies:
        print(f"  Strategy: {s.strategy_id} on {s.symbols}")
```

- [ ] **Step 2: Smoke-test bot startup**

```bash
python main.py account
```

Expected: Account info prints without ImportError.

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: wire WheelStrategy into live trading alongside Momentum"
```

---

## Task 8: Dashboard Backend (FastAPI)

**Files:**
- Create: `dashboard/__init__.py`
- Create: `dashboard/app.py`
- Create: `tests/unit/test_dashboard_api.py`

- [ ] **Step 1: Install dashboard dependencies**

```bash
pip install fastapi uvicorn
python -c "import fastapi, uvicorn; print('OK')"
```

Expected: `OK`

- [ ] **Step 2: Create `dashboard/__init__.py` and static directory**

```bash
touch dashboard/__init__.py
mkdir -p dashboard/static
```

- [ ] **Step 3: Write failing API tests**

Create `tests/unit/test_dashboard_api.py`:

```python
"""Tests for FastAPI dashboard endpoints."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient with mocked broker and temp SQLite DB."""
    monkeypatch.setenv("DASHBOARD_DB_PATH", str(tmp_path / "test.db"))

    with (
        patch("dashboard.app._get_broker") as mock_broker_factory,
        patch("dashboard.app.DB_PATH", str(tmp_path / "test.db")),
        patch("dashboard.app.STATE_PATH", str(tmp_path / "strategy_state.json")),
    ):
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = MagicMock(
            cash=Decimal("20000"),
            equity=Decimal("20500"),
            buying_power=Decimal("20000"),
            portfolio_value=Decimal("20500"),
        )
        mock_broker.get_positions.return_value = []
        mock_broker_factory.return_value = mock_broker

        from database.migrations import init_db
        init_db(str(tmp_path / "test.db"))

        from dashboard.app import app
        yield TestClient(app)


def test_account_endpoint_returns_cash(client):
    resp = client.get("/api/account")
    assert resp.status_code == 200
    data = resp.json()
    assert "cash" in data
    assert float(data["cash"]) == pytest.approx(20000.0)
    assert "equity" in data
    assert "mode" in data


def test_positions_endpoint_returns_list(client):
    resp = client.get("/api/positions")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_trades_endpoint_returns_list(client):
    resp = client.get("/api/trades")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_performance_endpoint_returns_stats(client):
    resp = client.get("/api/performance")
    assert resp.status_code == 200
    data = resp.json()
    assert "win_rate" in data
    assert "total_return_pct" in data
    assert "max_drawdown_pct" in data
    assert "trade_count" in data


def test_strategy_state_endpoint_returns_dict(client):
    resp = client.get("/api/strategy-state")
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)


def test_alerts_endpoint_returns_list(client):
    resp = client.get("/api/alerts")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
```

- [ ] **Step 4: Run tests to confirm failure**

```bash
python -m pytest tests/unit/test_dashboard_api.py -v
```

Expected: FAIL — `dashboard.app` doesn't exist yet.

- [ ] **Step 5: Create `dashboard/app.py`**

```python
"""
Trading Bot Dashboard — FastAPI backend.

Run: python -m dashboard.app
Open: http://localhost:8000
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from broker.client import BrokerClient
from core.config import settings
from database.migrations import get_session_factory
from database.models import Signal, Trade

DB_PATH = settings.system.db_path
STATE_PATH = str(Path(DB_PATH).parent / "strategy_state.json")
MODE = "PAPER" if settings.alpaca_paper else "LIVE"

app = FastAPI(title="TradingBot Dashboard", version="1.0")

_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


def _get_broker() -> BrokerClient:
    return BrokerClient()


@app.get("/")
async def index():
    return FileResponse(str(_static_dir / "index.html"))


@app.get("/api/account")
async def get_account():
    acct = _get_broker().get_account()
    return {
        "cash": float(acct.cash),
        "equity": float(acct.equity),
        "buying_power": float(acct.buying_power),
        "portfolio_value": float(acct.portfolio_value),
        "mode": MODE,
    }


@app.get("/api/positions")
async def get_positions():
    return [
        {
            "symbol": p.symbol,
            "side": p.side,
            "quantity": p.quantity,
            "avg_entry_price": float(p.avg_entry_price),
            "current_price": float(p.current_price),
            "market_value": float(p.market_value),
            "unrealized_pnl": float(p.unrealized_pnl),
            "unrealized_pnl_pct": float(p.unrealized_pnl_pct),
        }
        for p in _get_broker().get_positions()
    ]


@app.get("/api/strategy-state")
async def get_strategy_state():
    try:
        path = Path(STATE_PATH)
        if path.exists():
            return json.loads(path.read_text())
    except Exception as e:
        logger.warning(f"Could not read strategy_state.json: {e}")
    return {}


@app.get("/api/trades")
async def get_trades():
    with get_session_factory(DB_PATH)() as session:
        trades = (
            session.query(Trade)
            .order_by(Trade.filled_at.desc())
            .limit(100)
            .all()
        )
        return [
            {
                "order_id": t.order_id,
                "symbol": t.symbol,
                "strategy_id": t.strategy_id,
                "side": t.side,
                "quantity": t.quantity,
                "fill_price": float(t.fill_price),
                "commission": float(t.commission) if t.commission else 0.0,
                "is_options": t.is_options,
                "option_contract_id": t.option_contract_id,
                "filled_at": str(t.filled_at),
            }
            for t in trades
        ]


@app.get("/api/performance")
async def get_performance():
    with get_session_factory(DB_PATH)() as session:
        trades = session.query(Trade).order_by(Trade.filled_at.asc()).all()

    if not trades:
        return {
            "win_rate": 0.0,
            "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "trade_count": 0,
            "equity_curve": [],
        }

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    equity_curve = []
    sells = 0
    buys = 0

    for t in trades:
        notional = float(t.fill_price) * int(t.quantity) * (100 if t.is_options else 1)
        if t.side == "sell":
            equity += notional
            sells += 1
        else:
            equity -= notional
            buys += 1
        equity -= float(t.commission) if t.commission else 0.0

        if equity > peak:
            peak = equity
        drawdown = (peak - equity) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, drawdown)
        equity_curve.append({"ts": str(t.filled_at), "equity": round(equity, 2)})

    total_trades = sells + buys
    win_rate = sells / total_trades if total_trades > 0 else 0.0

    return {
        "win_rate": round(win_rate, 4),
        "total_return_pct": round(equity / 20000.0, 4),
        "max_drawdown_pct": round(max_dd, 4),
        "trade_count": total_trades,
        "equity_curve": equity_curve[-200:],
    }


@app.get("/api/alerts")
async def get_alerts():
    with get_session_factory(DB_PATH)() as session:
        rejected = (
            session.query(Signal)
            .filter(Signal.approved == False)  # noqa: E712
            .order_by(Signal.generated_at.desc())
            .limit(50)
            .all()
        )
        return [
            {
                "strategy_id": s.strategy_id,
                "symbol": s.symbol,
                "signal_type": s.signal_type,
                "rejection_reason": s.rejection_reason,
                "generated_at": str(s.generated_at),
            }
            for s in rejected
        ]


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("Dashboard WebSocket client connected")
    try:
        while True:
            try:
                broker = _get_broker()
                acct = broker.get_account()
                positions = broker.get_positions()

                strategy_state: dict = {}
                state_path = Path(STATE_PATH)
                if state_path.exists():
                    try:
                        strategy_state = json.loads(state_path.read_text())
                    except Exception:
                        pass

                await websocket.send_json({
                    "type": "update",
                    "account": {
                        "cash": float(acct.cash),
                        "equity": float(acct.equity),
                        "buying_power": float(acct.buying_power),
                        "mode": MODE,
                    },
                    "positions": [
                        {
                            "symbol": p.symbol,
                            "side": p.side,
                            "quantity": p.quantity,
                            "avg_entry_price": float(p.avg_entry_price),
                            "unrealized_pnl": float(p.unrealized_pnl),
                        }
                        for p in positions
                    ],
                    "strategy_state": strategy_state,
                })
            except Exception as e:
                logger.warning(f"WebSocket update error: {e}")
                await websocket.send_json({"type": "error", "message": str(e)})

            await asyncio.sleep(5)

    except WebSocketDisconnect:
        logger.info("Dashboard WebSocket client disconnected")


if __name__ == "__main__":
    uvicorn.run("dashboard.app:app", host="0.0.0.0", port=8000, reload=False)
```

- [ ] **Step 6: Run dashboard tests**

```bash
python -m pytest tests/unit/test_dashboard_api.py -v
```

Expected: All 6 tests PASS.

- [ ] **Step 7: Commit**

```bash
git add dashboard/__init__.py dashboard/app.py tests/unit/test_dashboard_api.py
git commit -m "feat: add FastAPI dashboard backend with REST endpoints and WebSocket"
```

---

## Task 9: Dashboard Frontend (Single-Page UI)

**Files:**
- Create: `dashboard/static/index.html`

The frontend uses vanilla JS with a `escapeHTML()` helper to sanitize all server data before DOM insertion, preventing XSS from any unexpected characters in symbol names or contract IDs.

- [ ] **Step 1: Create `dashboard/static/index.html`**

Write the file using the Write tool with the following complete content. Key design decisions:
- All server-provided strings pass through `escapeHTML()` before being set via `textContent` or safe template literals
- Chart.js loaded from CDN (no build step)
- WebSocket auto-reconnects on disconnect
- Four sections: account bar, strategy cards, positions table, performance + trade log

The HTML structure and JavaScript must:

1. Define `escapeHTML(str)` at the top of the script that escapes `&`, `<`, `>`, `"`, `'` characters
2. Use `element.textContent = value` for plain text insertion
3. Only use template literals with `escapeHTML()` applied to any server-sourced string

Reference implementation structure:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>TradingBot Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    /* dark theme, account bar, card grid, table styles, status indicator */
  </style>
</head>
<body>
  <!-- account bar: cash, equity, buying power, mode badge -->
  <!-- strategy cards: one per symbol, wheel state chip, premium -->
  <!-- positions table: symbol, side, qty, entry, P&L -->
  <!-- performance: stats + Chart.js equity curve + trade log table -->
  <script>
    function escapeHTML(str) {
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
    }
    // Chart setup, WebSocket connect(), loadPerformance(), loadTrades(),
    // renderPositions(), renderStrategyCards() — all using escapeHTML()
  </script>
</body>
</html>
```

Implement the full HTML/CSS/JS in one file. The CSS should use `#0f1117` background, `#1a1f2e` cards, and colored state chips matching Wheel states (SCANNING=gray, CSP_OPEN=blue, ASSIGNED=amber, CC_OPEN=green).

- [ ] **Step 2: Smoke-test the dashboard**

```bash
python -m dashboard.app &
sleep 2
curl -s http://localhost:8000/api/account | python -m json.tool
kill %1
```

Expected: JSON with `cash`, `equity`, `buying_power`, `mode` fields.

- [ ] **Step 3: Run full test suite**

```bash
python -m pytest tests/unit/ -v --tb=short
```

Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add dashboard/static/index.html
git commit -m "feat: add single-page web dashboard with XSS-safe rendering and live WebSocket"
```

---

## Final Verification

- [ ] **Run full test suite**

```bash
python -m pytest tests/unit/ -v --tb=short
```

Expected: All tests PASS.

- [ ] **Smoke-test bot startup**

```bash
python main.py account
```

Expected: Account info prints cleanly.

- [ ] **Verify all imports resolve**

```bash
python -c "
from broker.client import BrokerClient
from strategies.wheel.wheel_strategy import WheelStrategy
from dashboard.app import app
print('All imports OK')
"
```

Expected: `All imports OK`

- [ ] **Final commit**

```bash
git status   # confirm only expected files changed
git log --oneline -10
```
