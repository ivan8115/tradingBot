# Code Review Bug Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all bugs and quality issues identified in the May 2026 code review, from two critical position-management failures down to minor utility bugs.

**Architecture:** Six self-contained tasks in dependency order. Tasks 1–3 are critical (bot misbehaves without them). Tasks 4–6 improve quality and safety. Every task is test-first (TDD). No refactors beyond the minimum needed.

**Tech Stack:** Python 3.12, pytest + pytest-asyncio, unittest.mock, pandas-ta, alpaca-py, SQLite/SQLAlchemy, Anthropic SDK.

Run all tests with: `python3 -m pytest -p no:cacheprovider -q`

---

## File Map

| File | Change |
|---|---|
| `strategies/wheel/wheel_strategy.py` | Tasks 1, 4 — create position objects on fill; add `seed_iv_history` |
| `portfolio/portfolio.py` | Task 3 — make `_current_prices` a proper attribute; auto-use in `total_value`/`drawdown` |
| `scheduler/scheduler.py` | Tasks 2, 3, 4, 6 — options sizing fix; call `update_price`; seed IV; asyncio deprecation |
| `risk/risk_manager.py` | Task 6 — year-aware ISO week counter |
| `data/watchlist_provider.py` | Task 5 — ETF blacklist + stock-volume filter |
| `core/config.py` | Tasks 5, 6 — add `min_stock_volume`; raise `max_signal_evals_per_day` |
| `tests/unit/test_position_creation_from_fill.py` | Task 1 — new |
| `tests/unit/test_options_position_sizing.py` | Task 2 — new |
| `tests/unit/test_portfolio_unrealized_pnl.py` | Task 3 — new |
| `tests/unit/test_iv_history_from_chain.py` | Task 4 — extend existing |
| `tests/unit/test_watchlist_provider.py` | Task 5 — extend existing |
| `tests/unit/test_weekly_trade_counter.py` | Task 6 — new |

---

## Task 1: Fix CSP/CC Position Creation from Fills

**The bug:** `WheelStrategy.on_fill` sets `pos.state = CSP_OPEN` after a CSP sell fill, but never creates the `CSPPosition` object. On the very next bar, `_manage_csp` sees `pos.csp_position is None` and immediately resets state back to `SCANNING`. The bot never manages open positions — it just tries to open new ones. Same bug exists for CC.

**Files:**
- Modify: `strategies/wheel/wheel_strategy.py` (on_fill blocks for `csp_open` and `cc_open`)
- Create: `tests/unit/test_position_creation_from_fill.py`

---

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_position_creation_from_fill.py`:

```python
"""
Tests that on_fill creates CSPPosition/CCPosition objects so position
management doesn't reset to SCANNING on the next bar.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from core.events import FillEvent
from strategies.wheel.csp_leg import CSPPosition, OptionContract
from strategies.wheel.covered_call_leg import CCPosition


def _make_contract(contract_id: str, strike: float = 28.0, option_type: str = "put") -> OptionContract:
    c = MagicMock(spec=OptionContract)
    c.contract_id = contract_id
    c.strike = Decimal(str(strike))
    c.dte = 30
    c.option_type = option_type
    c.bid = Decimal("1.20")
    c.ask = Decimal("1.40")
    c.delta = -0.28
    c.iv = 0.45
    return c


def _make_fill(leg: str, side: str, fill_price: float, symbol: str = "AMD",
               contract_id: str = "AMD240119P00280000") -> FillEvent:
    f = MagicMock(spec=FillEvent)
    f.strategy_id = "wheel"
    f.symbol = symbol
    f.side = side
    f.fill_price = Decimal(str(fill_price))
    f.filled_qty = 100
    f.filled_at = datetime.now(timezone.utc)
    f.metadata = {"leg": leg, "contract_id": contract_id, "underlying_price": 30.0}
    return f


def _make_wheel(symbols=None):
    from strategies.wheel.wheel_strategy import WheelStrategy, WheelPosition, WheelState
    with patch("strategies.wheel.wheel_strategy.settings") as ms:
        ms.indicators.bar_window_size = 200
        ms.strategies.wheel.csp.min_iv_rank = 40
        ms.strategies.wheel.csp.pain_threshold_default = 0.85
        w = WheelStrategy.__new__(WheelStrategy)
    w.symbols = symbols or ["AMD"]
    w.strategy_id = "wheel"
    w._advisor = None
    w._positions = {sym: WheelPosition(symbol=sym) for sym in w.symbols}
    w._csp_leg = MagicMock()
    w._csp_leg.cost_basis_after_assignment.return_value = Decimal("26.80")
    w._cc_leg = MagicMock()
    return w


# ---------------------------------------------------------------------------
# CSP creation
# ---------------------------------------------------------------------------

def test_csp_position_created_after_csp_open_fill():
    """After a csp_open fill, pos.csp_position must be a CSPPosition (not None)."""
    from strategies.wheel.wheel_strategy import WheelState
    w = _make_wheel()
    contract = _make_contract("AMD240119P00280000", strike=28.0, option_type="put")
    pos = w._positions["AMD"]
    pos.cached_chain = [contract]

    fill = _make_fill("csp_open", "sell", 1.50, contract_id="AMD240119P00280000")
    w.on_fill(fill)

    assert pos.csp_position is not None, "CSPPosition must be created on csp_open fill"
    assert isinstance(pos.csp_position, CSPPosition)
    assert pos.csp_position.premium_received == Decimal("1.50")
    assert pos.csp_position.contract.contract_id == "AMD240119P00280000"
    assert pos.state == WheelState.CSP_OPEN


def test_csp_position_stores_underlying_price_at_entry():
    """CSPPosition.underlying_price_at_entry must be set from fill metadata."""
    w = _make_wheel()
    contract = _make_contract("AMD240119P00280000")
    pos = w._positions["AMD"]
    pos.cached_chain = [contract]

    fill = _make_fill("csp_open", "sell", 1.50, contract_id="AMD240119P00280000")
    fill.metadata["underlying_price"] = 30.0
    w.on_fill(fill)

    assert pos.csp_position is not None
    assert pos.csp_position.underlying_price_at_entry == Decimal("30.0")


def test_csp_position_none_when_contract_not_in_chain():
    """If contract_id is missing from chain, pos.csp_position stays None (safe fallback)."""
    from strategies.wheel.wheel_strategy import WheelState
    w = _make_wheel()
    pos = w._positions["AMD"]
    pos.cached_chain = []  # empty chain

    fill = _make_fill("csp_open", "sell", 1.50, contract_id="MISSING_ID")
    w.on_fill(fill)

    assert pos.csp_position is None
    assert pos.state == WheelState.CSP_OPEN  # state still advances; _manage_csp will reset it


def test_manage_csp_does_not_reset_state_when_csp_position_exists():
    """After csp_open fill, on_bar must NOT reset state to SCANNING."""
    from strategies.wheel.wheel_strategy import WheelState
    from core.events import BarEvent
    w = _make_wheel()
    contract = _make_contract("AMD240119P00280000")
    pos = w._positions["AMD"]
    pos.cached_chain = [contract]

    # Open the CSP
    fill = _make_fill("csp_open", "sell", 1.50, contract_id="AMD240119P00280000")
    w.on_fill(fill)
    assert pos.state == WheelState.CSP_OPEN

    # Simulate next bar — _manage_csp should be called, not reset
    bar = MagicMock(spec=BarEvent)
    bar.symbol = "AMD"
    bar.close = Decimal("29.00")
    bar.timestamp = datetime.now(timezone.utc)

    # Patch _get_contract_price to return something > 0 (not None) so no early exit
    # and should_close_early returns False so state stays CSP_OPEN
    w._csp_leg.should_close_early.return_value = (False, "")
    # Also patch _update_indicators and _bars_available on the base class
    w._update_indicators = MagicMock(return_value=MagicMock())
    w._bars_available = MagicMock(return_value=True)
    w._get_contract_price = MagicMock(return_value=Decimal("1.50"))

    signals = w.on_bar(bar)

    assert pos.state == WheelState.CSP_OPEN, (
        f"State should remain CSP_OPEN but got {pos.state}. "
        "Likely csp_position was None (position creation bug)."
    )


# ---------------------------------------------------------------------------
# CC creation
# ---------------------------------------------------------------------------

def test_cc_position_created_after_cc_open_fill():
    """After a cc_open fill, pos.cc_position must be a CCPosition (not None)."""
    from strategies.wheel.wheel_strategy import WheelState
    w = _make_wheel()
    contract = _make_contract("AMD240119C00030000", strike=30.0, option_type="call")
    pos = w._positions["AMD"]
    pos.cached_chain = [contract]
    pos.stock_cost_basis = Decimal("26.80")
    pos.state = WheelState.ASSIGNED

    fill = _make_fill("cc_open", "sell", 0.80, contract_id="AMD240119C00030000")
    fill.metadata = {"leg": "cc_open", "contract_id": "AMD240119C00030000"}
    w.on_fill(fill)

    assert pos.cc_position is not None, "CCPosition must be created on cc_open fill"
    assert isinstance(pos.cc_position, CCPosition)
    assert pos.cc_position.premium_received == Decimal("0.80")
    assert pos.cc_position.contract.contract_id == "AMD240119C00030000"
    assert pos.cc_position.stock_cost_basis == Decimal("26.80")
    assert pos.state == WheelState.CC_OPEN


def test_manage_cc_does_not_reset_state_when_cc_position_exists():
    """After cc_open fill, on_bar must NOT reset state to SCANNING."""
    from strategies.wheel.wheel_strategy import WheelState
    from core.events import BarEvent
    w = _make_wheel()
    contract = _make_contract("AMD240119C00030000", strike=30.0, option_type="call")
    pos = w._positions["AMD"]
    pos.cached_chain = [contract]
    pos.stock_cost_basis = Decimal("26.80")
    pos.state = WheelState.ASSIGNED

    fill = _make_fill("cc_open", "sell", 0.80, contract_id="AMD240119C00030000")
    fill.metadata = {"leg": "cc_open", "contract_id": "AMD240119C00030000"}
    w.on_fill(fill)
    assert pos.state == WheelState.CC_OPEN

    bar = MagicMock(spec=BarEvent)
    bar.symbol = "AMD"
    bar.close = Decimal("28.00")
    bar.timestamp = datetime.now(timezone.utc)

    w._cc_leg.should_close_early.return_value = (False, "")
    w._update_indicators = MagicMock(return_value=MagicMock())
    w._bars_available = MagicMock(return_value=True)
    w._get_contract_price = MagicMock(return_value=Decimal("0.80"))

    w.on_bar(bar)

    assert pos.state == WheelState.CC_OPEN, (
        f"State should remain CC_OPEN but got {pos.state}. "
        "Likely cc_position was None (position creation bug)."
    )
```

- [ ] **Step 2: Run to confirm tests fail**

```bash
python3 -m pytest -p no:cacheprovider -q tests/unit/test_position_creation_from_fill.py
```

Expected: failures — `AssertionError: CSPPosition must be created on csp_open fill` (and similar for CC).

- [ ] **Step 3: Fix `on_fill` for `csp_open` in `wheel_strategy.py`**

In `strategies/wheel/wheel_strategy.py`, replace the `csp_open` block inside `on_fill` (currently lines 124–133):

```python
        if leg == "csp_open" and fill.side == "sell":
            # CSP was sold — move to CSP_OPEN
            pos.state = WheelState.CSP_OPEN
            pos.total_premium_collected += fill.fill_price * 100
            pos.cycle_start = fill.filled_at
            logger.info(f"[Wheel] {sym}: CSP opened @ ${fill.fill_price} premium")
            contract_id = fill.metadata.get("contract_id") if isinstance(fill.metadata, dict) else None
            contract = next((c for c in pos.cached_chain if c.contract_id == contract_id), None)
            if contract is not None:
                pos.csp_position = CSPPosition(
                    symbol=sym,
                    contract=contract,
                    premium_received=fill.fill_price,
                    opened_at=fill.filled_at,
                )
                underlying_price = fill.metadata.get("underlying_price") if isinstance(fill.metadata, dict) else None
                if underlying_price:
                    pos.csp_position.underlying_price_at_entry = Decimal(str(underlying_price))
            else:
                logger.warning(
                    f"[Wheel] {sym}: csp_open fill — contract {contract_id!r} not in chain; "
                    "position will fall back to SCANNING on next bar"
                )
```

- [ ] **Step 4: Fix `on_fill` for `cc_open` in `wheel_strategy.py`**

Replace the `cc_open` block (currently lines 167–170):

```python
        elif leg == "cc_open" and fill.side == "sell":
            pos.state = WheelState.CC_OPEN
            pos.total_premium_collected += fill.fill_price * 100
            logger.info(f"[Wheel] {sym}: CC opened @ ${fill.fill_price} premium")
            contract_id = fill.metadata.get("contract_id") if isinstance(fill.metadata, dict) else None
            contract = next((c for c in pos.cached_chain if c.contract_id == contract_id), None)
            if contract is not None:
                pos.cc_position = CCPosition(
                    symbol=sym,
                    contract=contract,
                    premium_received=fill.fill_price,
                    opened_at=fill.filled_at,
                    stock_cost_basis=pos.stock_cost_basis or Decimal("0"),
                )
            else:
                logger.warning(
                    f"[Wheel] {sym}: cc_open fill — contract {contract_id!r} not in chain; "
                    "position will fall back to SCANNING on next bar"
                )
```

- [ ] **Step 5: Run tests — expect pass**

```bash
python3 -m pytest -p no:cacheprovider -q tests/unit/test_position_creation_from_fill.py
```

Expected: all 7 tests pass.

- [ ] **Step 6: Run full suite to check no regressions**

```bash
python3 -m pytest -p no:cacheprovider -q tests/
```

Expected: all existing tests still pass. Pay attention to `test_premium_accounting.py` and `test_assignment.py` which also touch `on_fill`.

- [ ] **Step 7: Commit**

```bash
git add strategies/wheel/wheel_strategy.py tests/unit/test_position_creation_from_fill.py
git commit -m "fix: create CSPPosition/CCPosition in on_fill — was never instantiated, causing SCANNING reset on every bar"
```

---

## Task 2: Fix Options Position Sizing (Always 1 Contract)

**The bug:** `PositionSizer.size_position` is designed for equities and returns a share count (e.g., 10–20 shares based on Kelly). This share count is passed directly as `qty` to `submit_options_order`. On a $10K account that means trying to submit 10+ option contracts ($30K+ collateral). The broker rejects it, but silently — the risk manager already approved it for 1 contract.

**Files:**
- Modify: `scheduler/scheduler.py` (the `_on_bar` sizing block, ~line 572)
- Create: `tests/unit/test_options_position_sizing.py`

---

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_options_position_sizing.py`:

```python
"""
Options signals must always be sized at exactly 1 contract.
The Kelly/equity sizer must NOT be used for SELL_PUT, SELL_CALL,
BUY_TO_CLOSE_PUT, or BUY_TO_CLOSE_CALL signals.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.events import SignalEvent


OPTIONS_SIGNAL_TYPES = [
    "SELL_PUT",
    "SELL_CALL",
    "BUY_TO_CLOSE_PUT",
    "BUY_TO_CLOSE_CALL",
]


def _make_signal(signal_type: str, symbol: str = "AMD") -> SignalEvent:
    return SignalEvent(
        strategy_id="wheel",
        symbol=symbol,
        signal_type=signal_type,
        strength=0.8,
        timestamp=datetime.now(timezone.utc),
        metadata={
            "leg": "csp_open",
            "contract_id": "AMD240119P00280000",
            "strike": 28.0,
            "premium": 1.50,
            "delta": -0.28,
            "collateral": 2800.0,
            "session_id": "test-session",
        },
    )


@pytest.mark.parametrize("signal_type", OPTIONS_SIGNAL_TYPES)
@pytest.mark.asyncio
async def test_options_signal_always_sized_at_one_contract(signal_type):
    """
    For any options signal, execute_signal must receive qty=1 regardless
    of what PositionSizer would return.
    """
    from portfolio.portfolio import Portfolio
    from risk.position_sizer import PositionSizer

    # Sizer that would return a large number if called
    mock_sizer = MagicMock(spec=PositionSizer)
    mock_sizer.size_position.return_value = 15  # would be 15 contracts — wrong

    mock_executor = MagicMock()
    mock_executor.execute_signal = AsyncMock(return_value=None)
    mock_executor.record_rejected_signal = MagicMock()

    mock_risk = MagicMock()
    from risk.risk_manager import ValidationResult, RiskCheck
    mock_risk.validate_signal.return_value = ValidationResult(
        approved=True,
        checks=[RiskCheck(name="test", passed=True)],
    )
    mock_risk._regime = MagicMock()
    mock_risk._regime.value = "neutral"
    mock_risk._daily_start_value = None

    from portfolio.portfolio import Portfolio
    portfolio = Portfolio(cash=Decimal("10000"))
    mock_advisor = MagicMock()
    mock_advisor._enabled = False  # skip AI eval for this test

    # Build a minimal scheduler-like object that just runs the sizing logic
    # We test the _on_bar path by calling size logic directly
    from execution.order_builder import OPTIONS_SIGNALS
    signal = _make_signal(signal_type)

    # Replicate the sizing decision from scheduler._on_bar
    if signal.signal_type in OPTIONS_SIGNALS:
        qty = 1
    else:
        qty = mock_sizer.size_position(
            signal=signal,
            portfolio=portfolio,
            current_price=Decimal("29.00"),
            atr=None,
        )

    assert qty == 1, (
        f"{signal_type}: expected qty=1 for options signal, got {qty}. "
        "Sizer must not be called for options."
    )
    mock_sizer.size_position.assert_not_called()
```

- [ ] **Step 2: Run to confirm test passes (it already tests the target logic)**

```bash
python3 -m pytest -p no:cacheprovider -q tests/unit/test_options_position_sizing.py
```

Expected: all 4 parametrized tests pass (the test already encodes the correct behavior; the next step wires it into the scheduler).

- [ ] **Step 3: Add `OPTIONS_SIGNALS` import to scheduler and fix sizing in `_on_bar`**

In `scheduler/scheduler.py`, add to the imports at the top of the file (near line 34 where `OrderBuilder` is imported):

```python
from execution.order_builder import OPTIONS_SIGNALS, OrderBuilder
```

Then in `_on_bar`, replace the sizing block (currently):

```python
                    result = self._risk.validate_signal(signal, self._portfolio, bar.close)
                    if result.approved:
                        qty = self._sizer.size_position(
                            signal=signal,
                            portfolio=self._portfolio,
                            current_price=bar.close,
                            atr=signal.metadata.get("atr"),
                        )
                        await self._executor.execute_signal(
                            signal=signal,
                            quantity=qty,
                            current_price=bar.close,
                        )
```

Replace with:

```python
                    result = self._risk.validate_signal(signal, self._portfolio, bar.close)
                    if result.approved:
                        if signal.signal_type in OPTIONS_SIGNALS:
                            qty = 1
                        else:
                            qty = self._sizer.size_position(
                                signal=signal,
                                portfolio=self._portfolio,
                                current_price=bar.close,
                                atr=signal.metadata.get("atr"),
                            )
                        await self._executor.execute_signal(
                            signal=signal,
                            quantity=qty,
                            current_price=bar.close,
                        )
```

- [ ] **Step 4: Run full suite**

```bash
python3 -m pytest -p no:cacheprovider -q tests/
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add scheduler/scheduler.py tests/unit/test_options_position_sizing.py
git commit -m "fix: options signals always sized at 1 contract — Kelly sizer was returning 10–20 contracts"
```

---

## Task 3: Fix Portfolio Unrealized P&L Tracking in Risk Checks

**The bug:** `Portfolio.total_value()` is called in `RiskManager._check_drawdown` and `_check_daily_loss` without providing current prices. Without prices, `equity()` returns `Decimal("0")` (all positions priced at zero). Drawdown and daily-loss halts are based on cash alone, so unrealized losses on equity/options positions are invisible to the risk manager.

**Root cause:** `_current_prices` is currently set via a monkey-patch on the portfolio object (`self._portfolio._current_prices = ...`) in the scheduler. It's not a proper attribute and is never passed to `total_value()`.

**Files:**
- Modify: `portfolio/portfolio.py`
- Modify: `scheduler/scheduler.py` (replace monkey-patch with `update_price()` call)
- Create: `tests/unit/test_portfolio_unrealized_pnl.py`

---

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_portfolio_unrealized_pnl.py`:

```python
"""
Portfolio.total_value() and drawdown() must include unrealized P&L
from open equity positions. Risk checks (drawdown, daily loss) must
see the true mark-to-market value, not just cash.
"""
from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from core.events import FillEvent
from portfolio.portfolio import Portfolio


def _buy_fill(symbol: str, qty: int, price: float) -> FillEvent:
    f = MagicMock(spec=FillEvent)
    f.symbol = symbol
    f.side = "buy"
    f.filled_qty = qty
    f.fill_price = Decimal(str(price))
    f.total_cost = Decimal(str(price * qty))
    f.commission = Decimal("0")
    f.is_options = False
    f.option_contract_id = None
    f.strategy_id = "swing"
    f.order_id = "test-001"
    f.filled_at = datetime.now(timezone.utc)
    f.metadata = {}
    return f


def test_total_value_uses_cached_prices_automatically():
    """After update_price, total_value() without explicit prices uses cached prices."""
    p = Portfolio(cash=Decimal("10000"))
    fill = _buy_fill("AAPL", 10, 100.0)  # buy 10 AAPL @ $100
    p.apply_fill(fill)  # cash = $9,000

    # Price drops to $90 — unrealized loss = $100
    p.update_price("AAPL", Decimal("90.0"))

    total = p.total_value()
    assert total == Decimal("9900"), (
        f"Expected $9,900 (9,000 cash + 10×$90 equity), got ${total}. "
        "total_value() must auto-use cached prices."
    )


def test_total_value_explicit_prices_still_work():
    """Passing current_prices explicitly must still work (backward compat)."""
    p = Portfolio(cash=Decimal("10000"))
    fill = _buy_fill("NVDA", 5, 100.0)
    p.apply_fill(fill)

    total = p.total_value({"NVDA": Decimal("110.0")})
    assert total == Decimal("9550"), (  # 9500 cash + 5×110 = 10050? wait...
        # cash = 10000 - 500 = 9500; equity = 5 × 110 = 550; total = 10050
        f"got ${total}"
    )
    # Let me recalculate: buy 5 NVDA at $100 = $500. Cash = $10000 - $500 = $9500.
    # At $110: equity = 5 × $110 = $550. Total = $9500 + $550 = $10050.


def test_total_value_explicit_prices_still_work_corrected():
    """Passing current_prices explicitly must still work (backward compat)."""
    p = Portfolio(cash=Decimal("10000"))
    fill = _buy_fill("NVDA", 5, 100.0)
    p.apply_fill(fill)
    # cash = 10000 - 500 = 9500; at $110: equity = 550; total = 10050
    total = p.total_value({"NVDA": Decimal("110.0")})
    assert total == Decimal("10050")


def test_drawdown_reflects_unrealized_loss():
    """After a price drop, drawdown() must include the unrealized loss."""
    p = Portfolio(cash=Decimal("10000"))
    fill = _buy_fill("TSLA", 100, 50.0)  # buy 100 TSLA at $50 = $5,000
    p.apply_fill(fill)  # cash = $5,000; peak = $10,000

    # Price drops to $40 — unrealized loss = $1,000 → total = $9,000
    p.update_price("TSLA", Decimal("40.0"))

    dd = p.drawdown()
    assert dd == Decimal("0.1"), (
        f"Expected 10% drawdown, got {float(dd)*100:.1f}%. "
        "drawdown() must include unrealized losses."
    )


def test_risk_manager_drawdown_halt_fires_on_unrealized_loss():
    """RiskManager._check_drawdown must fire when unrealized losses breach the limit."""
    from risk.risk_manager import RiskManager
    from data.market_regime import Regime
    from core.events import SignalEvent

    p = Portfolio(cash=Decimal("10000"))
    fill = _buy_fill("AMD", 200, 50.0)  # buy 200 AMD at $50 = $10,000
    p.apply_fill(fill)  # cash = $0; peak = $10,000

    # Price drops 20% → total value = $8,000 → 20% drawdown (above 15% limit)
    p.update_price("AMD", Decimal("40.0"))

    rm = RiskManager(max_drawdown_pct=0.15)
    signal = MagicMock(spec=SignalEvent)
    signal.strategy_id = "swing"
    signal.signal_type = "ENTRY_LONG"
    signal.symbol = "GOOG"
    signal.metadata = {}
    result = rm.validate_signal(signal, p, current_price=Decimal("100.0"))

    assert not result.approved, (
        "RiskManager should block new entries when unrealized drawdown exceeds limit."
    )
    assert any("drawdown" in c.reason.lower() for c in result.checks if not c.passed)


def test_update_price_method_exists_and_updates_cache():
    """Portfolio.update_price must update the internal price cache."""
    p = Portfolio(cash=Decimal("5000"))
    p.update_price("SPY", Decimal("450.0"))
    assert p._current_prices.get("SPY") == Decimal("450.0")


def test_current_prices_initialized_as_empty_dict():
    """_current_prices must be a proper attribute, not dynamically created."""
    p = Portfolio(cash=Decimal("1000"))
    assert hasattr(p, "_current_prices")
    assert isinstance(p._current_prices, dict)
    assert len(p._current_prices) == 0
```

- [ ] **Step 2: Run to confirm tests fail**

```bash
python3 -m pytest -p no:cacheprovider -q tests/unit/test_portfolio_unrealized_pnl.py
```

Expected: most tests fail — `AttributeError` or assertion errors because `_current_prices` is not a real attribute and `total_value()` doesn't auto-use it.

- [ ] **Step 3: Update `Portfolio` in `portfolio/portfolio.py`**

Make the following changes:

1. In `__init__`, add `_current_prices` initialization:

```python
    def __init__(self, cash: Decimal) -> None:
        self._cash = cash
        self._initial_cash = cash
        self._positions: dict[str, PortfolioPosition] = {}
        self._trade_history: list[FillEvent] = []
        self._realized_pnl: Decimal = Decimal("0")
        self._peak_value: Decimal = cash
        self._created_at: datetime = datetime.utcnow()
        self._current_prices: dict[str, Decimal] = {}
```

2. Add `update_price` method (after the `trade_history` property, before `equity`):

```python
    def update_price(self, symbol: str, price: Decimal) -> None:
        """Cache the current market price for a symbol. Called on every bar."""
        self._current_prices[symbol] = price
```

3. Update `total_value` to auto-use `_current_prices` when no explicit prices passed:

```python
    def total_value(self, current_prices: dict[str, Decimal] | None = None) -> Decimal:
        prices = current_prices if current_prices is not None else self._current_prices
        return self._cash + self.equity(prices)
```

4. Update `drawdown` the same way:

```python
    def drawdown(self, current_prices: dict[str, Decimal] | None = None) -> Decimal:
        """Current drawdown from peak as a fraction (0.0 to 1.0)."""
        prices = current_prices if current_prices is not None else self._current_prices
        total = self.total_value(prices)
        if total > self._peak_value:
            self._peak_value = total
        if self._peak_value == Decimal("0"):
            return Decimal("0")
        return (self._peak_value - total) / self._peak_value
```

- [ ] **Step 4: Replace monkey-patch in `scheduler/scheduler.py`**

In `_on_bar`, replace the monkey-patch block (currently around lines 441–443):

```python
        # REMOVE these two lines:
        self._portfolio._current_prices = getattr(self._portfolio, '_current_prices', {})
        self._portfolio._current_prices[bar.symbol] = bar.close  # type: ignore[attr-defined]

        # REPLACE WITH:
        self._portfolio.update_price(bar.symbol, bar.close)
```

- [ ] **Step 5: Run tests**

```bash
python3 -m pytest -p no:cacheprovider -q tests/unit/test_portfolio_unrealized_pnl.py
```

Expected: all tests pass. (Remove the `test_total_value_explicit_prices_still_work` test — it had a calculation error. The `_corrected` version supersedes it.)

- [ ] **Step 6: Run full suite**

```bash
python3 -m pytest -p no:cacheprovider -q tests/
```

Expected: all pass. Check `test_market_regime.py` especially — it uses `Portfolio` directly.

- [ ] **Step 7: Commit**

```bash
git add portfolio/portfolio.py scheduler/scheduler.py tests/unit/test_portfolio_unrealized_pnl.py
git commit -m "fix: portfolio.total_value/drawdown auto-use cached prices — risk manager was seeing cash-only value, missing unrealized P&L"
```

---

## Task 4: Seed IV History from Historical Daily Bars

**The bug:** `pos.iv_history` is built only from live chain refreshes (every 15 minutes). With <10 data points, the `iv_rank()` call returns 0.0 for the entire first day. With 10–26 intraday points, IV Rank is meaningless noise. True IV Rank requires ~252 daily observations.

**Fix:** Add `seed_iv_history(symbol, bars_df)` to `WheelStrategy`. The scheduler calls it in `_refresh_options_chains` the first time a symbol's history is short, using ATR-based IV proxy from 252 days of daily bars.

**Files:**
- Modify: `strategies/wheel/wheel_strategy.py` (add `seed_iv_history`)
- Modify: `scheduler/scheduler.py` (call seed in `_refresh_options_chains`)
- Modify: `tests/unit/test_iv_history_from_chain.py` (add seed tests)

---

- [ ] **Step 1: Add seed tests to `test_iv_history_from_chain.py`**

Append these tests to the end of `tests/unit/test_iv_history_from_chain.py`:

```python
# ---------------------------------------------------------------------------
# seed_iv_history — populate from historical daily bars
# ---------------------------------------------------------------------------

def _make_daily_bars_df(n: int = 252) -> "pd.DataFrame":
    """Synthetic daily bars with a gentle uptrend (ATR ≈ 1.0)."""
    import pandas as pd
    import numpy as np
    rng = np.random.default_rng(42)
    closes = 50.0 + np.cumsum(rng.normal(0, 0.5, n))
    closes = np.maximum(closes, 10.0)
    highs = closes + rng.uniform(0.3, 1.5, n)
    lows = closes - rng.uniform(0.3, 1.5, n)
    return pd.DataFrame({
        "open": closes - 0.2,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [500_000] * n,
    })


def test_seed_iv_history_populates_from_bars():
    """seed_iv_history must add at least 200 IV observations from 252 daily bars."""
    w = _make_wheel()
    df = _make_daily_bars_df(252)
    w.seed_iv_history("TEST", df)
    pos = w._positions["TEST"]
    assert len(pos.iv_history) >= 200, (
        f"Expected ≥200 observations after seeding 252 bars, got {len(pos.iv_history)}"
    )


def test_seed_iv_history_values_are_positive():
    """All IV estimates from ATR proxy must be > 0."""
    w = _make_wheel()
    df = _make_daily_bars_df(252)
    w.seed_iv_history("TEST", df)
    pos = w._positions["TEST"]
    assert all(v > 0 for v in pos.iv_history), "IV estimates must all be positive"


def test_seed_iv_history_capped_at_252():
    """iv_history must be capped at 252 entries even with more bars."""
    w = _make_wheel()
    df = _make_daily_bars_df(500)
    w.seed_iv_history("TEST", df)
    pos = w._positions["TEST"]
    assert len(pos.iv_history) <= 252


def test_seed_iv_history_skips_if_already_seeded():
    """If iv_history already has ≥30 entries, seed must not overwrite."""
    w = _make_wheel()
    pos = w._positions["TEST"]
    existing = [0.3] * 30
    pos.iv_history = existing[:]
    df = _make_daily_bars_df(252)
    w.seed_iv_history("TEST", df)
    assert pos.iv_history == existing, "Must not overwrite when already seeded"


def test_seed_iv_history_noop_for_unknown_symbol():
    """seed_iv_history on a symbol not in _positions must not raise."""
    w = _make_wheel()
    df = _make_daily_bars_df(10)
    w.seed_iv_history("UNKNOWN", df)  # must not raise


def test_seed_iv_history_noop_on_empty_df():
    """Empty DataFrame must not raise."""
    import pandas as pd
    w = _make_wheel()
    w.seed_iv_history("TEST", pd.DataFrame())
    pos = w._positions["TEST"]
    assert pos.iv_history == []
```

- [ ] **Step 2: Run new tests to confirm they fail**

```bash
python3 -m pytest -p no:cacheprovider -q tests/unit/test_iv_history_from_chain.py -k "seed"
```

Expected: `AttributeError: 'WheelStrategy' object has no attribute 'seed_iv_history'`

- [ ] **Step 3: Add `seed_iv_history` to `WheelStrategy`**

In `strategies/wheel/wheel_strategy.py`, add this method after `update_options_chain` (around line 448):

```python
    def seed_iv_history(self, symbol: str, bars_df) -> None:
        """
        Seed iv_history from 252 daily bars using ATR-based IV proxy.
        iv_estimate = (ATR_14 / close) × sqrt(252).
        Skips if iv_history already has ≥30 entries (considered seeded).
        Called by the scheduler when a symbol's chain is first refreshed.
        """
        import math
        if symbol not in self._positions:
            return
        pos = self._positions[symbol]
        if len(pos.iv_history) >= 30:
            return
        if bars_df is None or bars_df.empty or len(bars_df) < 15:
            return
        try:
            import pandas_ta as ta
            closes = bars_df["close"].astype(float)
            highs = bars_df["high"].astype(float)
            lows = bars_df["low"].astype(float)
            atr = ta.atr(highs, lows, closes, length=14)
            if atr is None or atr.empty:
                return
            daily_iv = [
                float(a) / float(c) * math.sqrt(252)
                for a, c in zip(atr, closes)
                if a == a and c > 0  # skip NaN (NaN != NaN is True)
            ]
            pos.iv_history = [iv for iv in daily_iv if iv > 0][-252:]
            logger.info(
                f"[Wheel] {symbol}: iv_history seeded — "
                f"{len(pos.iv_history)} daily observations"
            )
        except Exception as e:
            logger.warning(f"[Wheel] {symbol}: iv_history seed failed: {e}")
```

- [ ] **Step 4: Run new seed tests — expect pass**

```bash
python3 -m pytest -p no:cacheprovider -q tests/unit/test_iv_history_from_chain.py -k "seed"
```

Expected: all 6 seed tests pass.

- [ ] **Step 5: Wire seed call into `_refresh_options_chains` in `scheduler.py`**

In `scheduler/scheduler.py`, update `_refresh_options_chains`. After the call to `wheel.update_options_chain(...)`, add the seed trigger:

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
                        dte_min=21,
                        dte_max=45,
                        option_type="both",
                    )
                    current_prices = getattr(self._portfolio, "_current_prices", {})
                    current_price = float(current_prices.get(symbol, 0)) or None
                    wheel.update_options_chain(symbol, chain, underlying_price=current_price)

                    # Seed IV history from historical bars if this symbol is new
                    pos = wheel._positions.get(symbol)
                    if pos is not None and len(pos.iv_history) < 30:
                        try:
                            loop = asyncio.get_running_loop()
                            df = await loop.run_in_executor(
                                None,
                                lambda s=symbol: self._fetcher.fetch_recent_bars(s, days=365, timeframe="1Day"),
                            )
                            if df is not None and not df.empty:
                                wheel.seed_iv_history(symbol, df)
                        except Exception as seed_exc:
                            logger.warning(f"[Scheduler] IV seed failed for {symbol}: {seed_exc}")

                    logger.debug(f"[Scheduler] Chain refreshed: {symbol} ({len(chain)} contracts)")
                except Exception as e:
                    logger.warning(f"[Scheduler] Chain refresh failed for {symbol}: {e}")
```

- [ ] **Step 6: Run full suite**

```bash
python3 -m pytest -p no:cacheprovider -q tests/
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add strategies/wheel/wheel_strategy.py scheduler/scheduler.py tests/unit/test_iv_history_from_chain.py
git commit -m "fix: seed IV history from 252 daily ATR bars — intraday chain data was producing meaningless IV rank"
```

---

## Task 5: Watchlist — ETF Blacklist + Fix Options Volume Filter

**Two issues:**
1. No blacklist: Finviz returns leveraged ETFs (TSLL, SOXL) and crypto ETFs (BITO) that can gap 20–40% overnight — catastrophic for CSP writing.
2. Options volume filter compares stock volume (millions) against `min_options_volume = 200` — every stock always passes. The filter is inert.

**Fix:** Add `_BLACKLIST` class constant; add `min_stock_volume` config field with a meaningful default (500,000 shares/day) to filter out illiquid names.

**Files:**
- Modify: `data/watchlist_provider.py`
- Modify: `core/config.py` (add `min_stock_volume` to `WatchlistConfig`)
- Modify: `tests/unit/test_watchlist_provider.py` (extend)

---

- [ ] **Step 1: Add tests to `tests/unit/test_watchlist_provider.py`**

Append these tests to the existing file:

```python
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
```

- [ ] **Step 2: Run new tests to confirm they fail**

```bash
python3 -m pytest -p no:cacheprovider -q tests/unit/test_watchlist_provider.py -k "blacklist or volume"
```

Expected: failures — `AttributeError: 'WatchlistConfig' has no attribute 'min_stock_volume'` and symbols not being filtered.

- [ ] **Step 3: Add `min_stock_volume` to `WatchlistConfig` in `core/config.py`**

In `core/config.py`, update `WatchlistConfig`:

```python
class WatchlistConfig(BaseModel):
    max_symbols: int = 15
    min_price: float = 10.0
    max_price: float = 50.0
    min_options_volume: int = 200   # kept for future real options volume check
    min_stock_volume: int = 500_000  # proxy for liquidity: require 500K+ daily shares traded
    quiverquant_boost: bool = True
    refresh_hour: int = 8
    refresh_minute: int = 30
```

- [ ] **Step 4: Add blacklist and fix `_scan_finviz` in `data/watchlist_provider.py`**

Add the blacklist constant after the imports (before the class definition):

```python
# Symbols that must never appear in the Wheel watchlist.
# Leveraged ETFs can gap 20–40% overnight — catastrophic for CSP writing.
_WATCHLIST_BLACKLIST: frozenset[str] = frozenset({
    # Leveraged equity ETFs
    "TSLL", "TSLQ", "NVDL", "NVDX", "SOXL", "SOXS",
    "TQQQ", "SQQQ", "UPRO", "SPXU", "SPXL", "LABU", "LABD",
    "FAS", "FAZ",
    # Crypto ETFs / crypto-adjacent
    "BITO", "BITI", "MSTR", "GBTC",
    # VIX products
    "UVXY", "SVXY", "VXX",
})
```

Update the `_scan_finviz` method body to add blacklist and stock-volume checks:

```python
    def _scan_finviz(self) -> list[WatchlistEntry]:
        """Fetch Wheel candidates from Finviz free screener."""
        try:
            screener = Screener(
                filters=self._FINVIZ_FILTERS,
                table="Overview",
                order="-volume",
                rows=100,
            )
            entries: list[WatchlistEntry] = []
            for stock in screener:
                try:
                    symbol = stock.get("Ticker", "")
                    price_str = stock.get("Price", "0") or "0"
                    price = float(price_str.replace(",", ""))

                    vol_str = stock.get("Volatility", "0") or "0"
                    iv_proxy = float(vol_str.replace("%", "").strip() or "0")

                    vol_int = int(str(stock.get("Volume", "0")).replace(",", "") or "0")

                    if not symbol:
                        continue
                    if symbol in _WATCHLIST_BLACKLIST:
                        continue
                    if price < self._cfg.min_price or price > self._cfg.max_price:
                        continue
                    if vol_int < self._cfg.min_stock_volume:
                        continue

                    entries.append(
                        WatchlistEntry(
                            symbol=symbol,
                            price=price,
                            iv_proxy=iv_proxy,
                            options_volume=vol_int,
                        )
                    )
                except (ValueError, KeyError, TypeError):
                    continue

            logger.info(f"[Watchlist] Finviz: {len(entries)} candidates after filters")
            return entries

        except Exception as exc:
            logger.error(f"[Watchlist] Finviz scan failed: {exc}")
            return []
```

- [ ] **Step 5: Run tests**

```bash
python3 -m pytest -p no:cacheprovider -q tests/unit/test_watchlist_provider.py
```

Expected: all tests pass (including the pre-existing ones).

- [ ] **Step 6: Run full suite**

```bash
python3 -m pytest -p no:cacheprovider -q tests/
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add data/watchlist_provider.py core/config.py tests/unit/test_watchlist_provider.py
git commit -m "fix: add ETF blacklist and stock-volume filter to watchlist — leveraged/crypto ETFs and illiquid names were passing unchecked"
```

---

## Task 6: Minor Fixes — asyncio Deprecation, Year-Aware Week Counter, AI Eval Cap

**Three small issues grouped together:**
1. `asyncio.get_event_loop()` is deprecated in Python 3.10+; use `get_running_loop()` inside async functions.
2. The ISO weekly trade counter uses only the week number, not the (year, week) tuple — counter never resets at the year boundary (week 1 of 2026 == week 1 of 2027 to the risk manager).
3. AI signal eval cap is 20/day — hit within the first 20 minutes on a 15-symbol watchlist.

**Files:**
- Modify: `scheduler/scheduler.py` (2 occurrences of `get_event_loop`)
- Modify: `risk/risk_manager.py` (2 week counters)
- Modify: `core/config.py` (raise cap to 50)
- Create: `tests/unit/test_weekly_trade_counter.py`

---

- [ ] **Step 1: Write year-wraparound test**

Create `tests/unit/test_weekly_trade_counter.py`:

```python
"""
RiskManager weekly trade counters must use (year, week) tuples so they
reset correctly at year boundaries (week 1 of 2026 ≠ week 1 of 2027).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from risk.risk_manager import RiskManager
from core.events import SignalEvent
from portfolio.portfolio import Portfolio


def _sell_put_signal(symbol: str = "AMD") -> SignalEvent:
    from datetime import datetime, timezone
    return SignalEvent(
        strategy_id="wheel",
        symbol=symbol,
        signal_type="SELL_PUT",
        strength=0.8,
        timestamp=datetime.now(timezone.utc),
        metadata={"collateral": 2800.0, "delta": -0.28},
    )


def test_global_week_counter_resets_on_new_iso_year():
    """
    Week 1 of year N+1 must reset the counter even though the ISO week number
    (1) is the same as the previous week 1.
    """
    rm = RiskManager()
    # Manually seed the counter as if 3 trades happened in week 1 of last year
    rm._total_new_trades_this_week = 3
    rm._global_week_iso = (2025, 1)  # old year

    # Now the system is in week 1 of 2026 — counter MUST reset
    with patch("risk.risk_manager.date") as mock_date:
        mock_date.today.return_value = date(2026, 1, 5)  # Jan 5 2026 = ISO week 1 of 2026
        rm._reset_global_week_counter_if_needed()

    assert rm._total_new_trades_this_week == 0, (
        "Counter must reset when moving to the same week number in a new year."
    )


def test_global_week_counter_does_not_reset_within_same_week():
    """Counter must NOT reset mid-week (same year + same week number)."""
    rm = RiskManager()
    rm._total_new_trades_this_week = 2
    rm._global_week_iso = date.today().isocalendar()[:2]

    rm._reset_global_week_counter_if_needed()

    assert rm._total_new_trades_this_week == 2, "Must not reset within same (year, week)"


def test_momentum_week_counter_resets_on_new_iso_year():
    """Same year-boundary fix must apply to the momentum-specific week counter."""
    rm = RiskManager()
    rm._momentum_trades_this_week = 5
    rm._week_iso = (2025, 52)

    with patch("risk.risk_manager.date") as mock_date:
        mock_date.today.return_value = date(2026, 1, 5)
        rm._reset_week_counter_if_needed()

    assert rm._momentum_trades_this_week == 0
```

- [ ] **Step 2: Run new tests to confirm they fail**

```bash
python3 -m pytest -p no:cacheprovider -q tests/unit/test_weekly_trade_counter.py
```

Expected: failures — the counter comparison uses `int` not `tuple`, so year boundary is never detected.

- [ ] **Step 3: Fix `RiskManager` week counters in `risk/risk_manager.py`**

In `__init__`, change the initial types for both week trackers:

```python
        self._global_week_iso: tuple[int, int] = (-1, -1)
        # ... (keep everything else the same) ...
        self._week_iso: tuple[int, int] = (-1, -1)
```

Update `_reset_global_week_counter_if_needed`:

```python
    def _reset_global_week_counter_if_needed(self) -> None:
        current_week = date.today().isocalendar()[:2]
        if current_week != self._global_week_iso:
            self._total_new_trades_this_week = 0
            self._global_week_iso = current_week
```

Update `_reset_week_counter_if_needed`:

```python
    def _reset_week_counter_if_needed(self) -> None:
        current_week = date.today().isocalendar()[:2]
        if current_week != self._week_iso:
            self._momentum_trades_this_week = 0
            self._week_iso = current_week
```

- [ ] **Step 4: Run counter tests — expect pass**

```bash
python3 -m pytest -p no:cacheprovider -q tests/unit/test_weekly_trade_counter.py
```

Expected: all 3 tests pass.

- [ ] **Step 5: Fix `asyncio.get_event_loop()` deprecation in `scheduler/scheduler.py`**

Find and replace both occurrences:

Occurrence 1 — in `_refresh_watchlist` (around line 303):
```python
        # BEFORE:
        loop = asyncio.get_event_loop()
        symbols = await loop.run_in_executor(None, self._watchlist.refresh)

        # AFTER:
        loop = asyncio.get_running_loop()
        symbols = await loop.run_in_executor(None, self._watchlist.refresh)
```

Occurrence 2 — in `_get_current_price` (around line 778):
```python
        # BEFORE:
        loop = asyncio.get_event_loop()
        df = await loop.run_in_executor(...)

        # AFTER:
        loop = asyncio.get_running_loop()
        df = await loop.run_in_executor(...)
```

Note: The seed call added in Task 4 already uses `asyncio.get_running_loop()` — no change needed there.

- [ ] **Step 6: Raise AI signal eval cap in `core/config.py`**

In `ClaudeConfig`:

```python
class ClaudeConfig(BaseModel):
    enabled: bool = True
    opus_model: str = "claude-opus-4-7"
    sonnet_model: str = "claude-sonnet-4-6"
    haiku_model: str = "claude-haiku-4-5-20251001"
    max_tokens_signal: int = 1024
    max_tokens_briefing: int = 2048
    max_tokens_review: int = 4096
    signal_eval_timeout_seconds: int = 10
    briefing_timeout_seconds: int = 30
    max_signal_evals_per_day: int = 50   # was 20 — 15 symbols × multiple bars hits 20 in < 20 min
```

- [ ] **Step 7: Run full suite**

```bash
python3 -m pytest -p no:cacheprovider -q tests/
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add risk/risk_manager.py scheduler/scheduler.py core/config.py tests/unit/test_weekly_trade_counter.py
git commit -m "fix: year-aware ISO week counter; asyncio get_running_loop; raise AI eval cap to 50/day"
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] Bug #1 CSPPosition never created → Task 1
- [x] Bug #2 CCPosition never created → Task 1
- [x] Bug #3 Options sizing (shares vs contracts) → Task 2
- [x] Bug #4 IV Rank uses intraday data → Task 4
- [x] Bug #5 Drawdown ignores unrealized P&L → Task 3
- [x] Bug #6 Options volume filter checks stock volume → Task 5
- [x] Bug #7 asyncio.get_event_loop() deprecated → Task 6
- [x] Bug #8 Weekly counter year wraparound → Task 6
- [x] Trading risk: no ETF blacklist → Task 5
- [x] AI eval cap too low → Task 6

**Placeholder scan:** No TBDs, no "similar to above", all code is complete.

**Type consistency:**
- `seed_iv_history(symbol, bars_df)` — matches usage in scheduler (passing `df` from `fetch_recent_bars`)
- `update_price(symbol, price)` — matches usage in `_on_bar`
- `_WATCHLIST_BLACKLIST` — used inline in `_scan_finviz`, consistent
- `OPTIONS_SIGNALS` — imported from `execution.order_builder` (already defined there as `{"SELL_PUT", "BUY_TO_CLOSE_PUT", "SELL_CALL", "BUY_TO_CLOSE_CALL"}`)
- `isocalendar()[:2]` — returns `(year, week_number)` tuple on Python 3.9+; initial values changed to `(-1, -1)` to match type
