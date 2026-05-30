# WheelPosition Persistence + Pre-Market Gap Detection

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix two paper-trading safety gaps — (1) a bot restart no longer loses open option positions, and (2) the 8:15 AM gap-down scan uses real-time quotes instead of yesterday's close.

**Architecture:** Task 1 extends `WheelStrategy.get_state/load_state` to serialize/deserialize full `CSPPosition`/`CCPosition` objects (contract data + premium) into the existing `strategy_state.json`. Task 2 adds `BrokerClient.get_latest_quote` backed by Alpaca's `StockHistoricalDataClient` and wires it into `scheduler._get_current_price` with a daily-bar fallback. Tasks are independent — implement in order, or in parallel if preferred.

**Tech Stack:** Python 3.12, alpaca-py (`StockHistoricalDataClient`, `StockLatestQuoteRequest`), pytest.

Run tests with: `python3 -m pytest -p no:cacheprovider -q`

---

## File Map

| File | Task | Change |
|---|---|---|
| `strategies/wheel/wheel_strategy.py` | 1 | Extend `get_state` + `load_state`; add `date` import |
| `broker/client.py` | 2 | Add `_stock_data` client + `get_latest_quote` method |
| `scheduler/scheduler.py` | 2 | Update `_get_current_price` to use quote with fallback |
| `TODO.md` | 1+2 | Remove items #2 and #3 after each is done |
| `tests/unit/test_wheel_state_persistence.py` | 1 | New — 7 tests |
| `tests/unit/test_gap_down_quote.py` | 2 | New — 4 tests |

---

## Task 1: WheelPosition State Persistence

**The problem:** `strategy_state.json` saves `WheelState`, `stock_quantity`, `stock_cost_basis`, and `total_premium` but NOT the `CSPPosition`/`CCPosition` objects. After a restart in `CSP_OPEN` or `CC_OPEN` state, those objects are `None` and the state machine immediately resets to `SCANNING` — opening duplicate positions.

**Fix:** Serialize the full contract data + position metadata into `get_state()`. Reconstruct on `load_state()` from the saved JSON, recalculating DTE from the stored expiry date.

**Files:**
- Modify: `strategies/wheel/wheel_strategy.py:17` (imports), `:566-586` (`get_state`/`load_state`)
- Create: `tests/unit/test_wheel_state_persistence.py`
- Modify: `TODO.md` (remove item #3)

---

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_wheel_state_persistence.py`:

```python
"""
WheelStrategy get_state/load_state must persist and restore the full
CSPPosition and CCPosition objects so a bot restart doesn't lose open positions.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from strategies.wheel.csp_leg import CSPPosition, OptionContract
from strategies.wheel.covered_call_leg import CCPosition


def _make_wheel():
    from strategies.wheel.wheel_strategy import WheelStrategy, WheelPosition
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
    w._cc_leg = MagicMock()
    return w


def _make_option_contract(
    contract_id="AMD240119P00280000",
    option_type="put",
    strike="28.00",
    expiry_str="2026-06-20",
) -> OptionContract:
    expiry = date.fromisoformat(expiry_str)
    return OptionContract(
        symbol="AMD",
        contract_id=contract_id,
        option_type=option_type,
        strike=Decimal(strike),
        expiry=expiry,
        dte=(expiry - date.today()).days,
        bid=Decimal("1.20"),
        ask=Decimal("1.40"),
        delta=-0.28 if option_type == "put" else 0.30,
        iv=0.45,
        open_interest=2000,
        volume=500,
    )


def _set_csp_open(w, contract_id="AMD240119P00280000"):
    from strategies.wheel.wheel_strategy import WheelState
    contract = _make_option_contract(contract_id=contract_id)
    csp = CSPPosition(
        symbol="AMD",
        contract=contract,
        premium_received=Decimal("1.50"),
        opened_at=datetime(2026, 5, 30, 14, 0, 0, tzinfo=timezone.utc),
        contracts=1,
        underlying_price_at_entry=Decimal("30.00"),
    )
    pos = w._positions["AMD"]
    pos.state = WheelState.CSP_OPEN
    pos.csp_position = csp
    pos.total_premium_collected = Decimal("150.00")
    return pos


def _set_cc_open(w, contract_id="AMD240119C00030000"):
    from strategies.wheel.wheel_strategy import WheelState
    contract = _make_option_contract(
        contract_id=contract_id, option_type="call", strike="30.00"
    )
    cc = CCPosition(
        symbol="AMD",
        contract=contract,
        premium_received=Decimal("0.80"),
        opened_at=datetime(2026, 5, 30, 15, 0, 0, tzinfo=timezone.utc),
        stock_cost_basis=Decimal("26.80"),
        contracts=1,
    )
    pos = w._positions["AMD"]
    pos.state = WheelState.CC_OPEN
    pos.cc_position = cc
    pos.stock_quantity = 100
    pos.stock_cost_basis = Decimal("26.80")
    pos.total_premium_collected = Decimal("230.00")
    return pos


# ---------------------------------------------------------------------------
# get_state serialization
# ---------------------------------------------------------------------------

def test_get_state_includes_csp_position_data():
    """get_state must serialize full CSP contract + position data."""
    w = _make_wheel()
    _set_csp_open(w)

    state = w.get_state()
    amd = state["AMD"]

    assert amd["csp_position"] is not None
    csp = amd["csp_position"]
    assert csp["contract_id"] == "AMD240119P00280000"
    assert csp["strike"] == "28.00"
    assert csp["option_type"] == "put"
    assert csp["premium_received"] == "1.50"
    assert csp["underlying_price_at_entry"] == "30.00"
    assert "expiry" in csp
    assert "opened_at" in csp


def test_get_state_null_csp_position_when_scanning():
    """SCANNING state must produce csp_position: null in serialized state."""
    from strategies.wheel.wheel_strategy import WheelState
    w = _make_wheel()
    state = w.get_state()
    assert state["AMD"]["csp_position"] is None


def test_get_state_includes_cc_position_data():
    """get_state must serialize full CC contract + position data."""
    w = _make_wheel()
    _set_cc_open(w)

    state = w.get_state()
    amd = state["AMD"]

    assert amd["cc_position"] is not None
    cc = amd["cc_position"]
    assert cc["contract_id"] == "AMD240119C00030000"
    assert cc["option_type"] == "call"
    assert cc["stock_cost_basis"] == "26.80"
    assert cc["premium_received"] == "0.80"


# ---------------------------------------------------------------------------
# load_state reconstruction
# ---------------------------------------------------------------------------

def test_load_state_reconstructs_csp_position():
    """load_state must reconstruct a valid CSPPosition from serialized data."""
    w = _make_wheel()
    _set_csp_open(w)
    state = w.get_state()

    # Fresh strategy instance — no positions set
    w2 = _make_wheel()
    w2.load_state(state)

    pos = w2._positions["AMD"]
    assert pos.csp_position is not None
    assert isinstance(pos.csp_position, CSPPosition)
    assert pos.csp_position.premium_received == Decimal("1.50")
    assert pos.csp_position.contract.contract_id == "AMD240119P00280000"
    assert pos.csp_position.underlying_price_at_entry == Decimal("30.00")


def test_load_state_recalculates_dte_from_expiry():
    """DTE must be recalculated from expiry date on load, not taken from stale stored value."""
    w = _make_wheel()
    _set_csp_open(w)
    state = w.get_state()

    w2 = _make_wheel()
    w2.load_state(state)

    pos = w2._positions["AMD"]
    assert pos.csp_position is not None
    expiry = pos.csp_position.contract.expiry
    expected_dte = max(0, (expiry - date.today()).days)
    assert pos.csp_position.contract.dte == expected_dte


def test_load_state_reconstructs_cc_position():
    """load_state must reconstruct a valid CCPosition from serialized data."""
    w = _make_wheel()
    _set_cc_open(w)
    state = w.get_state()

    w2 = _make_wheel()
    w2.load_state(state)

    pos = w2._positions["AMD"]
    assert pos.cc_position is not None
    assert isinstance(pos.cc_position, CCPosition)
    assert pos.cc_position.premium_received == Decimal("0.80")
    assert pos.cc_position.stock_cost_basis == Decimal("26.80")
    assert pos.cc_position.contract.contract_id == "AMD240119C00030000"


def test_round_trip_preserves_full_state():
    """get_state → load_state round-trip: all position fields match."""
    w = _make_wheel()
    pos = _set_csp_open(w)
    state = w.get_state()

    w2 = _make_wheel()
    w2.load_state(state)
    pos2 = w2._positions["AMD"]

    from strategies.wheel.wheel_strategy import WheelState
    assert pos2.state == WheelState.CSP_OPEN
    assert pos2.total_premium_collected == Decimal("150.00")
    assert pos2.csp_position.premium_received == pos.csp_position.premium_received
    assert pos2.csp_position.contract.strike == pos.csp_position.contract.strike
```

- [ ] **Step 2: Run to confirm failures**

```bash
cd /home/ivan8115/git/tradingBot && python3 -m pytest -p no:cacheprovider -q tests/unit/test_wheel_state_persistence.py
```

Expected: failures — `AssertionError: state["AMD"]["csp_position"] is not None` (get_state doesn't serialize position objects yet).

- [ ] **Step 3: Add `date` to imports in `wheel_strategy.py`**

Find line 17:
```python
from datetime import datetime, timezone
```
Replace with:
```python
from datetime import date, datetime, timezone
```

- [ ] **Step 4: Replace `get_state` in `wheel_strategy.py`**

Find and replace the entire `get_state` method:

```python
    def get_state(self) -> dict:
        result = {}
        for sym, pos in self._positions.items():
            entry: dict = {
                "state": pos.state.value,
                "stock_quantity": pos.stock_quantity,
                "stock_cost_basis": str(pos.stock_cost_basis) if pos.stock_cost_basis else None,
                "total_premium": str(pos.total_premium_collected),
                "csp_position": None,
                "cc_position": None,
            }
            if pos.csp_position:
                c = pos.csp_position.contract
                entry["csp_position"] = {
                    "contract_id": c.contract_id,
                    "symbol": c.symbol,
                    "option_type": c.option_type,
                    "strike": str(c.strike),
                    "expiry": c.expiry.isoformat(),
                    "bid": str(c.bid),
                    "ask": str(c.ask),
                    "delta": c.delta,
                    "iv": c.iv,
                    "open_interest": c.open_interest,
                    "volume": c.volume,
                    "premium_received": str(pos.csp_position.premium_received),
                    "opened_at": pos.csp_position.opened_at.isoformat(),
                    "contracts": pos.csp_position.contracts,
                    "underlying_price_at_entry": str(pos.csp_position.underlying_price_at_entry)
                        if pos.csp_position.underlying_price_at_entry else None,
                }
            if pos.cc_position:
                c = pos.cc_position.contract
                entry["cc_position"] = {
                    "contract_id": c.contract_id,
                    "symbol": c.symbol,
                    "option_type": c.option_type,
                    "strike": str(c.strike),
                    "expiry": c.expiry.isoformat(),
                    "bid": str(c.bid),
                    "ask": str(c.ask),
                    "delta": c.delta,
                    "iv": c.iv,
                    "open_interest": c.open_interest,
                    "volume": c.volume,
                    "premium_received": str(pos.cc_position.premium_received),
                    "opened_at": pos.cc_position.opened_at.isoformat(),
                    "contracts": pos.cc_position.contracts,
                    "stock_cost_basis": str(pos.cc_position.stock_cost_basis),
                }
            result[sym] = entry
        return result
```

- [ ] **Step 5: Replace `load_state` in `wheel_strategy.py`**

Find and replace the entire `load_state` method:

```python
    def load_state(self, state: dict) -> None:
        for sym, data in state.items():
            if sym not in self._positions:
                continue
            pos = self._positions[sym]
            pos.state = WheelState(data["state"])
            pos.stock_quantity = data.get("stock_quantity", 0)
            cb = data.get("stock_cost_basis")
            pos.stock_cost_basis = Decimal(cb) if cb else None
            tp = data.get("total_premium", "0")
            pos.total_premium_collected = Decimal(tp)

            # Restore CSPPosition (only when state is CSP_OPEN)
            csp_data = data.get("csp_position")
            if csp_data and pos.state == WheelState.CSP_OPEN:
                try:
                    expiry = date.fromisoformat(csp_data["expiry"])
                    contract = OptionContract(
                        symbol=csp_data["symbol"],
                        contract_id=csp_data["contract_id"],
                        option_type=csp_data["option_type"],
                        strike=Decimal(csp_data["strike"]),
                        expiry=expiry,
                        dte=max(0, (expiry - date.today()).days),
                        bid=Decimal(csp_data["bid"]),
                        ask=Decimal(csp_data["ask"]),
                        delta=csp_data["delta"],
                        iv=csp_data["iv"],
                        open_interest=csp_data.get("open_interest", 0),
                        volume=csp_data.get("volume", 0),
                    )
                    uep = csp_data.get("underlying_price_at_entry")
                    pos.csp_position = CSPPosition(
                        symbol=pos.symbol,
                        contract=contract,
                        premium_received=Decimal(csp_data["premium_received"]),
                        opened_at=datetime.fromisoformat(csp_data["opened_at"]),
                        contracts=csp_data.get("contracts", 1),
                        underlying_price_at_entry=Decimal(str(uep)) if uep else None,
                    )
                except Exception as e:
                    logger.warning(f"[Wheel] {sym}: failed to restore CSPPosition from state: {e}")

            # Restore CCPosition (only when state is CC_OPEN)
            cc_data = data.get("cc_position")
            if cc_data and pos.state == WheelState.CC_OPEN:
                try:
                    expiry = date.fromisoformat(cc_data["expiry"])
                    contract = OptionContract(
                        symbol=cc_data["symbol"],
                        contract_id=cc_data["contract_id"],
                        option_type=cc_data["option_type"],
                        strike=Decimal(cc_data["strike"]),
                        expiry=expiry,
                        dte=max(0, (expiry - date.today()).days),
                        bid=Decimal(cc_data["bid"]),
                        ask=Decimal(cc_data["ask"]),
                        delta=cc_data["delta"],
                        iv=cc_data["iv"],
                        open_interest=cc_data.get("open_interest", 0),
                        volume=cc_data.get("volume", 0),
                    )
                    pos.cc_position = CCPosition(
                        symbol=pos.symbol,
                        contract=contract,
                        premium_received=Decimal(cc_data["premium_received"]),
                        opened_at=datetime.fromisoformat(cc_data["opened_at"]),
                        stock_cost_basis=Decimal(cc_data["stock_cost_basis"]),
                        contracts=cc_data.get("contracts", 1),
                    )
                except Exception as e:
                    logger.warning(f"[Wheel] {sym}: failed to restore CCPosition from state: {e}")
```

- [ ] **Step 6: Run persistence tests — expect all 7 pass**

```bash
cd /home/ivan8115/git/tradingBot && python3 -m pytest -p no:cacheprovider -q tests/unit/test_wheel_state_persistence.py
```

Expected: 7/7 pass.

- [ ] **Step 7: Run full suite — no regressions**

```bash
cd /home/ivan8115/git/tradingBot && python3 -m pytest -p no:cacheprovider -q tests/
```

Expected: all pass. Pay attention to `test_wheel_sync_symbols.py` and `test_assignment.py` which also exercise WheelStrategy.

- [ ] **Step 8: Remove item #3 from `TODO.md`**

Delete the entire `## 3. WheelPosition has no on-disk persistence` section and renumber: old #4 becomes #3.

The updated TODO.md items after this change:
1. Earnings date data is stale
2. Gap-down check uses most-recent daily close ← will be removed in Task 2
3. Equity fills not enriched (was #4)

- [ ] **Step 9: Commit**

```bash
cd /home/ivan8115/git/tradingBot && git add \
  strategies/wheel/wheel_strategy.py \
  TODO.md \
  tests/unit/test_wheel_state_persistence.py \
  && git commit -m "fix: persist CSPPosition/CCPosition in strategy_state.json — restart no longer loses open option positions"
```

---

## Task 2: Pre-Market Gap Detection Using Live Quotes

**The problem:** `scheduler._get_current_price` calls `fetch_recent_bars(days=2, timeframe="1Day")` which returns yesterday's close. A 15% overnight gap-down (earnings miss, black swan) won't be detected at the 8:15 AM scan.

**Fix:** Add `BrokerClient.get_latest_quote(symbol)` using Alpaca's `StockHistoricalDataClient` (already used for options snapshots). The IEX feed provides real-time and pre/post-market quotes. `_get_current_price` calls it first, falls back to daily bar close if it returns `None`.

**Files:**
- Modify: `broker/client.py` (imports + `__init__` + new method)
- Modify: `scheduler/scheduler.py` (`_get_current_price`)
- Create: `tests/unit/test_gap_down_quote.py`
- Modify: `TODO.md` (remove item #2)

---

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_gap_down_quote.py`:

```python
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
    MockStock.return_value.get_stock_latest_quote.return_value = {}  # symbol absent

    from broker.client import BrokerClient
    broker = BrokerClient()
    result = broker.get_latest_quote("AMD")

    assert result is None
```

- [ ] **Step 2: Run to confirm failures**

```bash
cd /home/ivan8115/git/tradingBot && python3 -m pytest -p no:cacheprovider -q tests/unit/test_gap_down_quote.py
```

Expected: `ImportError` or `AttributeError` — `StockHistoricalDataClient` not imported and `get_latest_quote` doesn't exist yet.

- [ ] **Step 3: Add imports and `_stock_data` client to `broker/client.py`**

Add to the import block at the top of the file (after the existing alpaca data imports):

```python
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
```

In `BrokerClient.__init__`, add after `self._option_data = ...`:

```python
        self._stock_data = StockHistoricalDataClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
        )
```

- [ ] **Step 4: Add `get_latest_quote` method to `BrokerClient`**

Add this method in `broker/client.py`, placed in the `# Options` section after `get_options_chain` (or wherever is most logical — near the other data-fetching methods):

```python
    def get_latest_quote(self, symbol: str) -> float | None:
        """
        Fetch the latest available quote price for a symbol.
        Uses IEX feed — provides real-time data including pre/post-market activity.
        Returns ask_price (bid if ask is unavailable), or None on failure.
        Used by the gap-down scanner to detect overnight moves before the first bar.
        """
        try:
            req = StockLatestQuoteRequest(
                symbol_or_symbols=[symbol],
                feed=DataFeed.IEX,
            )
            data = self._stock_data.get_stock_latest_quote(req)
            quote = data.get(symbol)
            if quote is None:
                return None
            ask = float(quote.ask_price or 0)
            bid = float(quote.bid_price or 0)
            if ask > 0:
                return ask
            if bid > 0:
                return bid
            return None
        except Exception as e:
            logger.warning(f"[BrokerClient] get_latest_quote failed for {symbol}: {e}")
            return None
```

- [ ] **Step 5: Run quote tests — expect all 4 pass**

```bash
cd /home/ivan8115/git/tradingBot && python3 -m pytest -p no:cacheprovider -q tests/unit/test_gap_down_quote.py
```

- [ ] **Step 6: Update `_get_current_price` in `scheduler/scheduler.py`**

Find the `_get_current_price` method (currently around line 798). Replace the entire method:

```python
    async def _get_current_price(self, symbol: str) -> float | None:
        """
        Fetch the latest available quote price for a symbol (pre-market aware).
        Tries Alpaca's real-time IEX quote first; falls back to most-recent daily bar close.
        """
        try:
            price = self._broker.get_latest_quote(symbol)
            if price is not None:
                return price
        except Exception as exc:
            logger.warning(f"[GapDown] Live quote failed for {symbol}: {exc}")

        # Fallback: last daily bar close
        try:
            loop = asyncio.get_running_loop()
            df = await loop.run_in_executor(
                None,
                lambda: self._fetcher.fetch_recent_bars(symbol, days=2, timeframe="1Day"),
            )
            if df is None or df.empty:
                logger.warning(f"[GapDown] No price data returned for {symbol}")
                return None
            return float(df["close"].iloc[-1])
        except Exception as exc:
            logger.warning(f"[GapDown] Price fetch failed for {symbol}: {exc}")
            return None
```

- [ ] **Step 7: Run full suite — no regressions**

```bash
cd /home/ivan8115/git/tradingBot && python3 -m pytest -p no:cacheprovider -q tests/
```

Expected: all pass.

- [ ] **Step 8: Remove item #2 from `TODO.md`**

Delete the entire `## 2. Gap-down check uses most-recent daily close` section. After this, only 2 items remain:
1. Earnings date data is stale
2. Equity fills not enriched (Swing/Momentum)

- [ ] **Step 9: Commit**

```bash
cd /home/ivan8115/git/tradingBot && git add \
  broker/client.py \
  scheduler/scheduler.py \
  TODO.md \
  tests/unit/test_gap_down_quote.py \
  && git commit -m "fix: use real-time IEX quote for gap-down detection — was using prior-day close"
```

---

## Self-Review

**Spec coverage:**
- [x] CSPPosition serialized in `get_state` → Task 1 Steps 4
- [x] CCPosition serialized in `get_state` → Task 1 Step 4
- [x] CSPPosition reconstructed in `load_state` with DTE recalculated → Task 1 Step 5
- [x] CCPosition reconstructed in `load_state` → Task 1 Step 5
- [x] Backward compatible with old state files (missing keys handled by `.get()`) → Task 1 Step 5
- [x] `get_latest_quote` returns ask/bid/None → Task 2 Step 4
- [x] `_get_current_price` uses quote first, daily bar fallback → Task 2 Step 6
- [x] TODO.md items removed after each task → Steps 8

**Placeholder scan:** All code is complete. No TBDs.

**Type consistency:**
- `date.fromisoformat(...)` — `date` is now imported at line 17 of `wheel_strategy.py` ✓
- `OptionContract(...)` — already imported at module level ✓
- `CSPPosition(...)` — already imported at module level ✓
- `CCPosition(...)` — already imported at module level ✓
- `DataFeed.IEX` — `DataFeed` imported in `broker/client.py` ✓
- `StockLatestQuoteRequest` — imported in `broker/client.py` ✓
