# Bug Fixes & Strategy Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 12 bugs found in the code review — 5 critical (silent failures, broken guardrails, missing stop losses), 4 trading strategy flaws, and 3 code reliability issues — bringing the bot to a state safe for live paper trading.

**Architecture:** All fixes are isolated changes within existing files. No new modules needed. Each task is independent — they can be done in any order. Tests use the existing pytest + unittest.mock pattern found in `tests/unit/`.

**Tech Stack:** Python 3.12, pytest, unittest.mock, Decimal arithmetic, pydantic, loguru

---

## Files Modified (summary)

| File | Tasks |
|------|-------|
| `scheduler/scheduler.py` | T1 (alerter calls), T5 (daily loss restart), T11 (drawdown units), T12 (private method) |
| `strategies/wheel/wheel_strategy.py` | T2 (premium 100×), T6 (real IV), T10 (cost basis) |
| `strategies/swing/swing_strategy.py` | T3 (hard stop), T9 (earnings gate) |
| `risk/risk_manager.py` | T4 (collateral cap) |
| `strategies/wheel/csp_leg.py` | T8 (Tier 1 stop) |
| `strategies/wheel/covered_call_leg.py` | T7 (CC downside exit) |
| `execution/executor.py` | T12 (public method) |
| `core/config.py` | T7 (cc_stop_loss_pct field), T8 (mark_stop_multiplier field) |
| `config.yaml` | T7, T8 (new config values) |
| `tests/unit/test_stop_loss_semantics.py` | T8 |
| `tests/unit/test_collateral_cap.py` | T4 |
| `tests/unit/test_happy_sad_panda.py` | T3 (new stop test) |
| `tests/unit/test_assignment.py` | T10 |

---

## Task 1: Fix alerter.alert() Wrong Argument Count

**Files:**
- Modify: `scheduler/scheduler.py:420`, `scheduler/scheduler.py:673-676`, `scheduler/scheduler.py:698`

**Context:** `AlertManager.alert(event_type, message, level, data)` requires two positional args. Three call sites in the scheduler pass only one string (which lands in `event_type`, leaving `message` missing). Each raises `TypeError` at runtime, silently swallowed, so daily reviews, weekly reviews, and thesis warnings are never delivered.

- [ ] **Step 1: Run the existing test suite to establish baseline**

```bash
cd /home/ivan8115/git/tradingBot && python -m pytest tests/ -q 2>&1 | tail -20
```

- [ ] **Step 2: Write a failing test that confirms the wrong-arg call pattern**

Add to `tests/unit/test_dashboard_api.py` (or create `tests/unit/test_alerter_calls.py`):

```python
# tests/unit/test_alerter_calls.py
"""Regression: alerter.alert() requires (event_type, message). These calls must not raise."""
from unittest.mock import patch, MagicMock


def test_daily_review_alert_call_does_not_raise():
    from monitoring.alerting import AlertManager
    am = AlertManager.__new__(AlertManager)
    am._slack_enabled = False
    am._email_enabled = False
    am._alert_on = {"all"}
    # This must not raise TypeError
    am.alert("daily_review", "Daily Review [A]: Things look good.")


def test_weekly_review_alert_call_does_not_raise():
    from monitoring.alerting import AlertManager
    am = AlertManager.__new__(AlertManager)
    am._slack_enabled = False
    am._email_enabled = False
    am._alert_on = {"all"}
    am.alert("weekly_review", "Weekly Review [B]: premium=$500.00, win_rate=75%")


def test_thesis_warning_alert_call_does_not_raise():
    from monitoring.alerting import AlertManager
    am = AlertManager.__new__(AlertManager)
    am._slack_enabled = False
    am._email_enabled = False
    am._alert_on = {"all"}
    am.alert("thesis_warning", "[Thesis Warning] AAPL: negative guidance cut")
```

- [ ] **Step 3: Run to confirm they currently pass (they test the library, not the broken caller)**

```bash
python -m pytest tests/unit/test_alerter_calls.py -v
```

Expected: PASS (the AlertManager itself is fine — the bug is in how scheduler.py calls it).

- [ ] **Step 4: Fix the three call sites in scheduler.py**

**Line 420** (inside `_on_market_close`, daily review):
```python
# BEFORE:
alerter.alert(f"Daily Review [{review.grade}]: {review.summary}")

# AFTER:
alerter.alert("daily_review", f"Daily Review [{review.grade}]: {review.summary}")
```

**Lines 673–676** (inside `_weekly_review`):
```python
# BEFORE:
alerter.alert(
    f"Weekly Review [{review.week_grade}]: "
    f"premium=${review.total_premium:.2f}, win_rate={review.win_rate:.0%}"
)

# AFTER:
alerter.alert(
    "weekly_review",
    f"Weekly Review [{review.week_grade}]: "
    f"premium=${review.total_premium:.2f}, win_rate={review.win_rate:.0%}",
)
```

**Line 698** (inside `_midday_thesis_check`):
```python
# BEFORE:
alerter.alert(f"[Thesis Warning] {sym}: {thesis[:200]}")

# AFTER:
alerter.alert("thesis_warning", f"[Thesis Warning] {sym}: {thesis[:200]}", level=AlertLevel.WARNING)
```

Note: `AlertLevel` is already imported at the top of the scheduler via the gap-down check. Confirm with `grep "AlertLevel" scheduler/scheduler.py` — if not at the top, add `from monitoring.alerting import AlertLevel` where the other monitoring imports are (around line 36).

- [ ] **Step 5: Run tests**

```bash
python -m pytest tests/ -q 2>&1 | tail -20
```

Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add scheduler/scheduler.py tests/unit/test_alerter_calls.py
git commit -m "fix: pass event_type and message to alerter.alert() — was dropping all AI review and thesis alerts"
```

---

## Task 2: Fix total_premium_collected 100× Underreporting

**Files:**
- Modify: `strategies/wheel/wheel_strategy.py:127` and `strategies/wheel/wheel_strategy.py:160`

**Context:** `pos.total_premium_collected += fill.fill_price` accumulates a per-share price (e.g. `$1.50`) instead of the total dollar credit (`$1.50 × 100 = $150`). All premium metrics, AI review inputs, and the EOD summary are 100× understated.

- [ ] **Step 1: Write a failing test**

Create `tests/unit/test_premium_accounting.py`:

```python
"""Premium collected must be total dollars, not per-share price."""
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest


def _make_fill(leg: str, side: str, fill_price: float, symbol: str = "AMD"):
    from core.events import FillEvent
    f = MagicMock(spec=FillEvent)
    f.strategy_id = "wheel"
    f.symbol = symbol
    f.side = side
    f.fill_price = Decimal(str(fill_price))
    f.filled_qty = 100
    f.metadata = {"leg": leg}
    return f


def _make_wheel():
    from strategies.wheel.wheel_strategy import WheelStrategy, WheelState
    from unittest.mock import patch
    with patch("strategies.wheel.wheel_strategy.settings") as mock_settings:
        mock_settings.strategies.wheel.csp.min_iv_rank = 40
        mock_settings.strategies.wheel.csp.pain_threshold_default = 0.85
        mock_settings.indicators.bar_window_size = 200
        w = WheelStrategy.__new__(WheelStrategy)
        w.symbols = ["AMD"]
        w.strategy_id = "wheel"
        from strategies.wheel.wheel_strategy import WheelPosition
        w._positions = {"AMD": WheelPosition(symbol="AMD")}
        w._advisor = None
        return w


def test_csp_premium_collected_is_total_dollars_not_per_share():
    """Opening a 1-contract CSP at $1.50/share = $150 total credit."""
    from strategies.wheel.wheel_strategy import WheelStrategy, WheelState
    from strategies.wheel.csp_leg import CSPPosition, OptionContract

    w = _make_wheel()
    pos = w._positions["AMD"]
    pos.state = WheelState.SCANNING

    contract = MagicMock(spec=OptionContract)
    contract.strike = Decimal("50")
    contract.dte = 30
    pos.csp_position = MagicMock()
    pos.csp_position.premium_received = Decimal("1.50")

    fill = _make_fill("csp_open", "sell", 1.50)
    fill.metadata = {"leg": "csp_open", "underlying_price": 52.0}
    w.on_fill(fill)

    assert pos.total_premium_collected == Decimal("150.00"), (
        f"Expected $150.00 total credit, got {pos.total_premium_collected}"
    )


def test_cc_premium_collected_is_total_dollars_not_per_share():
    """Opening a 1-contract CC at $0.80/share = $80 total credit."""
    from strategies.wheel.wheel_strategy import WheelStrategy, WheelState

    w = _make_wheel()
    pos = w._positions["AMD"]
    pos.state = WheelState.ASSIGNED
    pos.total_premium_collected = Decimal("150.00")

    fill = _make_fill("cc_open", "sell", 0.80)
    fill.metadata = {"leg": "cc_open"}
    w.on_fill(fill)

    assert pos.total_premium_collected == Decimal("230.00"), (
        f"Expected $150 + $80 = $230 total, got {pos.total_premium_collected}"
    )
```

- [ ] **Step 2: Run to confirm the test fails**

```bash
python -m pytest tests/unit/test_premium_accounting.py -v
```

Expected: FAIL — `AssertionError: Expected $150.00 total credit, got 1.50`

- [ ] **Step 3: Fix both accumulation lines in wheel_strategy.py**

**Line 127** (csp_open fill handler):
```python
# BEFORE:
pos.total_premium_collected += fill.fill_price

# AFTER:
pos.total_premium_collected += fill.fill_price * 100
```

**Line 160** (cc_open fill handler):
```python
# BEFORE:
pos.total_premium_collected += fill.fill_price

# AFTER:
pos.total_premium_collected += fill.fill_price * 100
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/unit/test_premium_accounting.py tests/unit/test_assignment.py -v
```

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add strategies/wheel/wheel_strategy.py tests/unit/test_premium_accounting.py
git commit -m "fix: accumulate total_premium_collected in dollars (×100), not per-share price"
```

---

## Task 3: Add Hard Stop Loss to Swing Strategy

**Files:**
- Modify: `strategies/swing/swing_strategy.py`
- Modify: `core/config.py` — `SwingStrategyConfig` (no new field needed, `atr_stop_mult` already exists)

**Context:** `_check_exits()` never reads the `stop_loss` value placed in signal metadata. The only exits are EMA crossback, Stage 3/4, and `max_hold_bars` — all lagging. A price-based stop must be tracked per-symbol and checked on every bar.

- [ ] **Step 1: Write a failing test**

Add to `tests/unit/test_happy_sad_panda.py` (or create `tests/unit/test_swing_stop_loss.py`):

```python
# tests/unit/test_swing_stop_loss.py
"""Swing strategy must close when price drops below the recorded stop level."""
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest


def _make_bar(symbol: str, close: float, ts=None):
    from core.events import BarEvent
    b = MagicMock(spec=BarEvent)
    b.symbol = symbol
    b.close = Decimal(str(close))
    b.open = b.close
    b.high = b.close
    b.low = b.close
    b.volume = 100_000
    b.timestamp = ts or datetime.now(timezone.utc)
    return b


def _make_swing():
    from strategies.swing.swing_strategy import SwingStrategy
    from core.config import SwingStrategyConfig
    cfg = SwingStrategyConfig(
        enabled=True,
        atr_stop_mult=2.0,
        atr_target_mult=4.0,
        max_hold_bars=30,
        min_bars_for_entry=50,
    )
    with patch("strategies.swing.swing_strategy.settings") as ms:
        ms.indicators.bar_window_size = 200
        ms.strategies.swing = cfg
        s = SwingStrategy(symbols=["NVDA"], config=cfg)
    return s


def test_stop_loss_triggers_when_price_drops_below_recorded_level():
    """After entry, if close drops below stop_loss, EXIT_LONG is emitted."""
    s = _make_swing()
    sym = "NVDA"

    # Simulate position already open with a known stop level
    s._in_position[sym] = True
    s._bars_held[sym] = 5
    s._entry_stop_levels[sym] = 850.0   # stop level set at entry

    bar = _make_bar(sym, close=840.0)   # below stop

    # Patch _update_indicators to return a minimal snapshot
    snap = MagicMock()
    snap.ema_trend_up = True  # not sad panda
    snap.macd_hist = 0.01
    snap.rsi = 55.0
    snap.atr = 10.0
    snap.volume_ratio = 1.0
    prev = MagicMock()
    prev.ema_trend_up = True  # no crossback

    with patch.object(s, "_update_indicators", return_value=snap), \
         patch.object(s, "_get_prev_snapshot", return_value=prev), \
         patch.object(s, "_bars_available", return_value=True), \
         patch("strategies.swing.swing_strategy.classify_stage") as mock_stage, \
         patch("strategies.swing.swing_strategy.is_sad_panda", return_value=False):
        from analysis.stage_analysis import Stage
        mock_stage.return_value = Stage.STAGE_2  # still stage 2
        signals = s.on_bar(bar)

    assert len(signals) == 1
    assert signals[0].signal_type == "EXIT_LONG"
    assert "stop_loss" in signals[0].metadata["reason"].lower()


def test_stop_loss_does_not_trigger_above_stop_level():
    """Price above stop level must not generate an exit signal."""
    s = _make_swing()
    sym = "NVDA"

    s._in_position[sym] = True
    s._bars_held[sym] = 5
    s._entry_stop_levels[sym] = 850.0

    bar = _make_bar(sym, close=870.0)  # above stop

    snap = MagicMock()
    snap.ema_trend_up = True
    snap.macd_hist = 0.01
    snap.rsi = 55.0
    snap.atr = 10.0
    snap.volume_ratio = 1.0
    prev = MagicMock()
    prev.ema_trend_up = True

    with patch.object(s, "_update_indicators", return_value=snap), \
         patch.object(s, "_get_prev_snapshot", return_value=prev), \
         patch.object(s, "_bars_available", return_value=True), \
         patch("strategies.swing.swing_strategy.classify_stage") as mock_stage, \
         patch("strategies.swing.swing_strategy.is_sad_panda", return_value=False):
        from analysis.stage_analysis import Stage
        mock_stage.return_value = Stage.STAGE_2
        signals = s.on_bar(bar)

    assert len(signals) == 0


def test_entry_records_stop_level():
    """On entry signal, stop_loss from metadata is stored in _entry_stop_levels."""
    from core.events import FillEvent
    s = _make_swing()
    sym = "NVDA"

    fill = MagicMock(spec=FillEvent)
    fill.strategy_id = "swing"
    fill.symbol = sym
    fill.side = "buy"
    fill.fill_price = Decimal("900.00")
    fill.metadata = {"stop_loss": 864.0}  # 2 ATR below

    s.on_fill(fill)

    assert s._entry_stop_levels[sym] == 864.0
    assert s._in_position[sym] is True
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/unit/test_swing_stop_loss.py -v
```

Expected: FAIL — `AttributeError: 'SwingStrategy' object has no attribute '_entry_stop_levels'`

- [ ] **Step 3: Implement the fix in swing_strategy.py**

In `__init__`, add the stop level tracker alongside `_bars_held`:

```python
# After:  self._bars_held: dict[str, int] = {sym: 0 for sym in symbols}
self._entry_stop_levels: dict[str, float] = {}
```

In `on_fill`, record the stop level when a buy fill arrives:

```python
def on_fill(self, fill: FillEvent) -> None:
    if fill.strategy_id != self.strategy_id:
        return
    sym = fill.symbol
    if fill.side == "buy":
        self._in_position[sym] = True
        self._bars_held[sym] = 0
        stop = fill.metadata.get("stop_loss") if isinstance(fill.metadata, dict) else None
        if stop is not None:
            self._entry_stop_levels[sym] = float(stop)
        logger.info(f"[Swing] Position opened: LONG {sym} @ ${fill.fill_price}")
    elif fill.side == "sell":
        self._in_position[sym] = False
        self._bars_held[sym] = 0
        self._entry_stop_levels.pop(sym, None)
        logger.info(f"[Swing] Position closed: {sym} @ ${fill.fill_price}")
```

In `_check_exits`, add stop loss as the first check (before EMA crossback):

```python
def _check_exits(self, sym: str, snap, prev, bar: BarEvent) -> list[SignalEvent]:
    close = float(bar.close)

    # Exit 0: Hard stop loss — price dropped below recorded stop level
    stop_level = self._entry_stop_levels.get(sym)
    if stop_level is not None and close <= stop_level:
        return self._exit_signal(sym, bar, f"stop_loss: close={close:.2f} <= stop={stop_level:.2f}")

    # Exit 1: Sad Panda — EMA crossback
    if is_sad_panda(snap, prev):
        return self._exit_signal(sym, bar, "EMA crossback (sad panda)")

    # Exit 2: Stage deterioration (Stage 3 or 4)
    df = pd.DataFrame(list(self._bar_windows[sym]))
    stage = classify_stage(df)
    if stage in (Stage.STAGE_3, Stage.STAGE_4):
        return self._exit_signal(sym, bar, f"Stage deteriorated to {stage.value}")

    # Exit 3: Max hold bars exceeded
    if self._bars_held.get(sym, 0) >= self._cfg.max_hold_bars:
        return self._exit_signal(sym, bar, f"Max hold bars ({self._cfg.max_hold_bars}) reached")

    return []
```

In `get_state` and `load_state`, persist the stop levels:

```python
def get_state(self) -> dict[str, Any]:
    return {
        "in_position": self._in_position.copy(),
        "bars_held": self._bars_held.copy(),
        "entry_stop_levels": self._entry_stop_levels.copy(),
    }

def load_state(self, state: dict[str, Any]) -> None:
    self._in_position.update(state.get("in_position", {}))
    self._bars_held.update(state.get("bars_held", {}))
    self._entry_stop_levels.update(state.get("entry_stop_levels", {}))
```

In `sync_symbols`, initialize the new dict for new symbols:

```python
def sync_symbols(self, new_symbols: list[str]) -> None:
    for sym in new_symbols:
        if sym not in self.symbols:
            self.symbols.append(sym)
            self._bar_windows[sym] = __import__('collections').deque(
                maxlen=settings.indicators.bar_window_size
            )
            self._prev_snapshots[sym] = None
            self._curr_snapshots[sym] = None
            self._in_position[sym] = False
            self._bars_held[sym] = 0
            # entry_stop_levels is populated only on fill — no init needed
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/unit/test_swing_stop_loss.py -v
```

Expected: All 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add strategies/swing/swing_strategy.py tests/unit/test_swing_stop_loss.py
git commit -m "fix: track entry stop levels in SwingStrategy and check them on every bar"
```

---

## Task 4: Fix Collateral Cap Check for Cash-Secured Puts

**Files:**
- Modify: `risk/risk_manager.py`
- Modify: `tests/unit/test_collateral_cap.py`

**Context:** `_check_total_collateral` uses `currently_deployed = total_value - portfolio.cash`. For the Wheel strategy, CSP collateral stays **in cash** (it's a promise to buy shares, not an actual outflow until assignment). So `portfolio.cash` always equals `total_value`, making `currently_deployed` always ≈ 0 — the 80% cap guardrail is silently bypassed.

Fix: Track committed CSP collateral in-memory in `RiskManager` and use that sum in the cap calculation.

- [ ] **Step 1: Write a failing test that exposes the real bug**

Add to `tests/unit/test_collateral_cap.py`:

```python
def test_collateral_cap_blocks_when_csp_cash_is_still_in_portfolio():
    """
    Real wheel account: cash-secured puts don't debit cash.
    After opening 3 CSPs worth $8,000 total, portfolio.cash is still $10,000.
    The cap check must still block a new $2,500 trade.
    """
    from risk.risk_manager import RiskManager

    rm = RiskManager(max_total_deployed_pct=0.80)

    # Simulate 3 already-approved SELL_PUT signals so rm tracks committed collateral
    for amount in [2_000, 3_000, 3_000]:
        sig = _make_signal(collateral=amount)
        portfolio_full = _make_portfolio(total_value=10_000, cash=10_000)
        rm.validate_signal(sig, portfolio_full)  # these are approved; rm commits them

    # Now portfolio.cash is still $10,000 (CSP doesn't debit cash)
    portfolio_full_cash = _make_portfolio(total_value=10_000, cash=10_000)
    new_signal = _make_signal(collateral=2_500)  # would bring total to $10,500 = 105%

    result = rm.validate_signal(new_signal, portfolio_full_cash)

    assert result.approved is False, "Should be blocked by collateral cap"
    collateral_check = next((c for c in result.checks if c.name == "collateral_cap"), None)
    assert collateral_check is not None
    assert collateral_check.passed is False
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/unit/test_collateral_cap.py::test_collateral_cap_blocks_when_csp_cash_is_still_in_portfolio -v
```

Expected: FAIL — the trade is approved when it should be rejected.

- [ ] **Step 3: Add committed collateral tracking to RiskManager**

In `__init__`, add the tracker after `self._net_portfolio_delta`:

```python
self._committed_csp_collateral: float = 0.0
```

Replace `_check_total_collateral` entirely:

```python
def _check_total_collateral(
    self, signal: SignalEvent, portfolio: Portfolio
) -> RiskCheck | None:
    """For SELL_PUT only: reject if committed CSP collateral would exceed the cap.

    Tracks committed collateral internally (CSP cash stays in portfolio until
    assignment, so portfolio.cash is not a reliable measure of deployment).
    """
    if signal.signal_type != "SELL_PUT":
        return None
    proposed = float(signal.metadata.get("collateral", 0))
    if not proposed:
        return None
    total_value = float(portfolio.total_value())
    if total_value == 0:
        return None
    projected_pct = (self._committed_csp_collateral + proposed) / total_value
    if projected_pct > self._max_total_deployed_pct:
        return RiskCheck(
            name="collateral_cap",
            passed=False,
            reason=(
                f"Collateral cap: deploying ${proposed:,.0f} would bring committed "
                f"CSP collateral to {projected_pct:.1%} (limit {self._max_total_deployed_pct:.0%}). "
                f"Currently committed: ${self._committed_csp_collateral:,.0f} / ${total_value:,.0f}."
            ),
        )
    return RiskCheck(name="collateral_cap", passed=True)
```

In `validate_signal`, after the approval decision, commit or release collateral:

```python
# After:  approved = all(c.passed for c in checks)

if approved and signal.signal_type == "SELL_PUT":
    collateral = float(signal.metadata.get("collateral", 0))
    self._committed_csp_collateral += collateral

if approved and signal.signal_type in ("BUY_TO_CLOSE_PUT",):
    # On close/buy-back, release the committed collateral for this symbol.
    # Use the collateral value from signal metadata if present.
    collateral = float(signal.metadata.get("collateral", 0))
    self._committed_csp_collateral = max(0.0, self._committed_csp_collateral - collateral)
```

Add a public reset method (used on assignment when collateral converts to stock):

```python
def release_collateral(self, amount: float) -> None:
    """Release committed CSP collateral (e.g. on assignment or manual close)."""
    self._committed_csp_collateral = max(0.0, self._committed_csp_collateral - amount)
```

- [ ] **Step 4: Run all collateral cap tests**

```bash
python -m pytest tests/unit/test_collateral_cap.py -v
```

Expected: All 5 tests PASS (4 existing + 1 new).

Note: The existing tests mock `portfolio.cash` as less than `total_value`. They will continue to pass because the new logic uses `_committed_csp_collateral` (which starts at 0) and the mock signals are submitted fresh each test with a new `RiskManager` instance.

- [ ] **Step 5: Commit**

```bash
git add risk/risk_manager.py tests/unit/test_collateral_cap.py
git commit -m "fix: track committed CSP collateral in RiskManager — cash-secured puts don't debit portfolio cash"
```

---

## Task 5: Fix Daily Loss Circuit-Breaker on Mid-Session Restart

**Files:**
- Modify: `scheduler/scheduler.py:322–338` (`_on_market_open`)

**Context:** When the bot restarts while the market is already open, `run()` calls `_on_market_open()` directly and skips `_pre_market()`. `set_daily_start_value()` is only called inside `_pre_market()`, so `_daily_start_value` stays `None`, and `_check_daily_loss()` always returns `passed=True`. The daily 3% loss circuit-breaker is silently disabled for the entire session.

- [ ] **Step 1: Write a failing test**

Create `tests/unit/test_daily_loss_restart.py`:

```python
"""Daily loss circuit-breaker must be seeded even when bot starts mid-session."""
from decimal import Decimal
from unittest.mock import MagicMock, AsyncMock, patch
import pytest


@pytest.mark.asyncio
async def test_on_market_open_sets_daily_start_value_when_not_already_set():
    """If _on_market_open is called without a prior _pre_market run,
    the daily start value must still be seeded from the portfolio."""
    from risk.risk_manager import RiskManager
    from portfolio.portfolio import Portfolio

    rm = RiskManager()
    assert rm._daily_start_value is None

    portfolio = Portfolio(cash=Decimal("50000"))
    rm.set_daily_start_value(portfolio)

    assert rm._daily_start_value == Decimal("50000")
    # Confirm check now works
    result = rm._check_daily_loss(portfolio)
    assert result.passed is True
```

- [ ] **Step 2: Run to confirm test passes (it tests RiskManager in isolation — always worked)**

```bash
python -m pytest tests/unit/test_daily_loss_restart.py -v
```

Expected: PASS — the RiskManager logic is fine; the bug is in scheduler wiring.

- [ ] **Step 3: Fix _on_market_open in scheduler.py**

Find the `_on_market_open` method (around line 322). Add the daily start value seed at the top of the method body, guarded so it only sets once per session:

```python
async def _on_market_open(self) -> None:
    if not self._is_market_open():
        logger.info("Market not open (holiday?)")
        return

    logger.info("=== MARKET OPEN ===")

    # Seed daily loss baseline if not already set by _pre_market (mid-session restart case)
    if self._risk._daily_start_value is None:
        self._tracker.sync()
        self._portfolio = Portfolio(cash=self._tracker.cash)
        self._risk.set_daily_start_value(self._portfolio)
        logger.info(
            f"[RiskManager] Daily baseline seeded at market open "
            f"(mid-session start): ${float(self._risk._daily_start_value):,.2f}"
        )

    for strategy in self._strategies:
        strategy.on_start()

    # Start WebSocket streams
    asyncio.create_task(
        self._stream.start(
            symbols=self._active_symbols,
            bar_handler=self._on_bar,
            fill_handler=self._on_fill,
        )
    )
```

- [ ] **Step 4: Run full test suite**

```bash
python -m pytest tests/ -q 2>&1 | tail -20
```

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add scheduler/scheduler.py tests/unit/test_daily_loss_restart.py
git commit -m "fix: seed daily loss baseline in _on_market_open for mid-session restart cases"
```

---

## Task 6: Use Real Options IV for IV Rank (Not ATR Proxy)

**Files:**
- Modify: `strategies/wheel/wheel_strategy.py` — `_evaluate_entry`, `WheelPosition`, and `update_options_chain`

**Context:** `pos.iv_history` is built from `(ATR / price) × √252` — a realized volatility proxy, not implied volatility. The `min_iv_rank: 40` filter is supposed to ensure elevated options premium before selling CSPs, but it's comparing apples and oranges. The options chain already provides `contract.iv` on each contract. Use the ATM IV from the chain instead.

- [ ] **Step 1: Write a failing test**

Create `tests/unit/test_iv_history_from_chain.py`:

```python
"""IV history must be built from options chain IV, not ATR proxy."""
from decimal import Decimal
from unittest.mock import MagicMock
import pytest


def _make_contract(strike=50.0, iv=0.45, option_type="put", dte=30):
    from strategies.wheel.csp_leg import OptionContract
    c = MagicMock(spec=OptionContract)
    c.option_type = option_type
    c.strike = Decimal(str(strike))
    c.dte = dte
    c.iv = iv
    c.delta = -0.28
    c.bid = Decimal("1.20")
    c.ask = Decimal("1.40")
    c.volume = 500
    c.open_interest = 2000
    c.contract_id = f"TEST{strike}P"
    return c


def test_update_options_chain_extracts_atm_iv():
    """update_options_chain should store median put IV in pos.iv_history."""
    from strategies.wheel.wheel_strategy import WheelStrategy, WheelPosition
    from unittest.mock import patch

    with patch("strategies.wheel.wheel_strategy.settings") as ms:
        ms.indicators.bar_window_size = 200
        ms.strategies.wheel.csp.min_iv_rank = 40
        ms.strategies.wheel.csp.pain_threshold_default = 0.85
        ms.strategies.wheel.csp.min_dte = 21
        ms.strategies.wheel.csp.max_dte = 45
        w = WheelStrategy.__new__(WheelStrategy)
        w.symbols = ["TEST"]
        w._positions = {"TEST": WheelPosition(symbol="TEST")}
        w._advisor = None

    chain = [
        _make_contract(strike=48.0, iv=0.40, dte=30),
        _make_contract(strike=50.0, iv=0.45, dte=30),  # ATM
        _make_contract(strike=52.0, iv=0.42, dte=30),
    ]

    underlying_price = 50.5
    w.update_options_chain("TEST", chain, underlying_price=underlying_price)

    pos = w._positions["TEST"]
    assert len(pos.iv_history) == 1, "Should have added one IV reading"
    # ATM IV (the put closest to ATM at $50) = 0.45
    assert 0.40 <= pos.iv_history[0] <= 0.50, f"Expected ATM IV ~0.45, got {pos.iv_history[0]}"
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/unit/test_iv_history_from_chain.py -v
```

Expected: FAIL — `update_options_chain` doesn't accept `underlying_price` yet, and doesn't update `iv_history`.

- [ ] **Step 3: Update `update_options_chain` in wheel_strategy.py**

```python
def update_options_chain(
    self, symbol: str, chain: list[OptionContract], underlying_price: float | None = None
) -> None:
    """Called by scheduler to refresh the options chain for a symbol.
    Also extracts ATM IV to build the iv_history for IV Rank calculation.
    """
    if symbol not in self._positions:
        return
    pos = self._positions[symbol]
    pos.cached_chain = chain

    # Extract ATM IV from the puts in the 21–45 DTE window
    if underlying_price and underlying_price > 0:
        puts_in_window = [
            c for c in chain
            if c.option_type == "put"
            and self._cfg.csp.min_dte <= c.dte <= self._cfg.csp.max_dte
            and getattr(c, "iv", None)
        ]
        if puts_in_window:
            # Pick the put strike closest to ATM
            atm_put = min(puts_in_window, key=lambda c: abs(float(c.strike) - underlying_price))
            atm_iv = atm_put.iv
            pos.iv_history.append(atm_iv)
            if len(pos.iv_history) > 252:
                pos.iv_history = pos.iv_history[-252:]
```

- [ ] **Step 4: Remove ATR-based IV estimation from `_evaluate_entry`**

In `_evaluate_entry` (around line 202), remove the ATR IV estimation block and use `iv_history` directly:

```python
# REMOVE these lines (they build fake ATR-based IV history):
#   current_iv = self._estimate_iv(snap, bar)
#   if current_iv > 0:
#       pos.iv_history.append(current_iv)
#       if len(pos.iv_history) > 252:
#           pos.iv_history = pos.iv_history[-252:]

# REPLACE the IV rank calculation with:
iv_rank_val = iv_rank(pos.iv_history[-1], pos.iv_history[:-1]) if len(pos.iv_history) > 10 else 0.0
```

The IV history is now exclusively populated by `update_options_chain`. The `_estimate_iv` method can remain (used nowhere else now) but is no longer called in the entry path.

- [ ] **Step 5: Update scheduler.py to pass underlying_price to update_options_chain**

In `_refresh_options_chains` (around line 604), pass the current bar price:

```python
async def _refresh_options_chains(self) -> None:
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
                # Pass current price so update_options_chain can extract ATM IV
                current_price = float(self._portfolio._current_prices.get(symbol, 0))
                wheel.update_options_chain(symbol, chain, underlying_price=current_price or None)
                logger.debug(f"[Scheduler] Chain refreshed: {symbol} ({len(chain)} contracts)")
            except Exception as e:
                logger.warning(f"[Scheduler] Chain refresh failed for {symbol}: {e}")
```

- [ ] **Step 6: Run tests**

```bash
python -m pytest tests/unit/test_iv_history_from_chain.py tests/unit/test_wheel_sync_symbols.py -v
```

Expected: All pass.

- [ ] **Step 7: Commit**

```bash
git add strategies/wheel/wheel_strategy.py scheduler/scheduler.py tests/unit/test_iv_history_from_chain.py
git commit -m "fix: build IV rank history from real options chain ATM IV, not ATR proxy"
```

---

## Task 7: Add Covered Call Downside Exit

**Files:**
- Modify: `core/config.py` — add `cc_stop_loss_pct` to `CCConfig`
- Modify: `config.yaml` — add `cc_stop_loss_pct`
- Modify: `strategies/wheel/covered_call_leg.py` — update `should_close_early`
- Modify: `strategies/wheel/wheel_strategy.py` — `_manage_cc` passes `underlying_price`

**Context:** After assignment, `is_deep_itm()` is defined but never called, and there is no exit when the underlying falls below the cost basis. The bot can hold a deteriorating stock indefinitely while selling CCs at lower and lower strikes, compounding the loss.

- [ ] **Step 1: Add config field to CCConfig in core/config.py**

```python
class CCConfig(BaseModel):
    target_delta: float = 0.30
    min_dte: int = 21
    max_dte: int = 45
    profit_target_pct: float = 0.50
    roll_when_dte: int = 7
    stock_stop_loss_pct: float = 0.90  # Close stock if price < cost_basis × this
```

- [ ] **Step 2: Add to config.yaml under strategies.wheel.cc**

```yaml
    cc:
      target_delta: 0.30
      min_dte: 21
      max_dte: 45
      profit_target_pct: 0.50
      roll_when_dte: 7
      stock_stop_loss_pct: 0.90   # Exit stock if underlying < cost_basis × 0.90
```

- [ ] **Step 3: Write a failing test**

Create `tests/unit/test_cc_downside_exit.py`:

```python
"""Covered call leg must signal exit when stock falls below cost basis stop."""
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest


def _make_cc_position(symbol="AMD", strike=55.0, cost_basis=50.0, premium=0.80):
    from strategies.wheel.covered_call_leg import CCPosition
    from strategies.wheel.csp_leg import OptionContract
    contract = MagicMock(spec=OptionContract)
    contract.strike = Decimal(str(strike))
    contract.dte = 20
    pos = CCPosition(
        symbol=symbol,
        contract=contract,
        premium_received=Decimal(str(premium)),
        opened_at=datetime.now(timezone.utc),
        stock_cost_basis=Decimal(str(cost_basis)),
    )
    return pos


def _make_cc_leg(stop_pct=0.90):
    from strategies.wheel.covered_call_leg import CoveredCallLeg
    from core.config import CCConfig
    cfg = MagicMock(spec=CCConfig)
    cfg.profit_target_pct = 0.50
    cfg.roll_when_dte = 7
    cfg.stock_stop_loss_pct = stop_pct
    return CoveredCallLeg(cfg)


def test_cc_closes_when_stock_falls_below_cost_basis_stop():
    """Stock at $44 < cost_basis($50) × 0.90($45) → should close."""
    leg = _make_cc_leg(stop_pct=0.90)
    pos = _make_cc_position(cost_basis=50.0)
    should_close, reason = leg.should_close_early(
        pos,
        current_contract_price=Decimal("0.40"),
        underlying_price=Decimal("44.00"),  # below 50 × 0.90 = 45
    )
    assert should_close is True
    assert "stock_stop" in reason.lower() or "cost_basis" in reason.lower()


def test_cc_does_not_close_above_cost_basis_stop():
    """Stock at $48 > cost_basis($50) × 0.90($45) → no stop exit."""
    leg = _make_cc_leg(stop_pct=0.90)
    pos = _make_cc_position(cost_basis=50.0)
    should_close, _ = leg.should_close_early(
        pos,
        current_contract_price=Decimal("0.40"),
        underlying_price=Decimal("48.00"),  # above 45 threshold
    )
    assert should_close is False


def test_cc_profit_target_still_fires_first():
    """50% profit target should still trigger regardless of stock price."""
    leg = _make_cc_leg(stop_pct=0.90)
    pos = _make_cc_position(cost_basis=50.0, premium=0.80)
    # 50% of $0.80 = mark must be <= $0.40
    should_close, reason = leg.should_close_early(
        pos,
        current_contract_price=Decimal("0.40"),
        underlying_price=Decimal("52.00"),
    )
    assert should_close is True
    assert "profit" in reason.lower()
```

- [ ] **Step 4: Run to confirm failure**

```bash
python -m pytest tests/unit/test_cc_downside_exit.py -v
```

Expected: FAIL — `should_close_early` doesn't accept `underlying_price` yet.

- [ ] **Step 5: Update CoveredCallLeg.should_close_early**

```python
def should_close_early(
    self,
    position: CCPosition,
    current_contract_price: Decimal,
    underlying_price: Decimal | None = None,
) -> tuple[bool, str]:
    """
    Close the CC early when:
    1. Profit target reached (50% of max)
    2. DTE ≤ roll_when_dte (roll to next expiry)
    3. Stock stop: underlying < cost_basis × stock_stop_loss_pct
    """
    profit_pct = position.profit_pct(current_contract_price)

    if profit_pct >= self._cfg.profit_target_pct:
        return True, f"Profit target: {profit_pct*100:.0f}% of max"

    if position.contract.dte <= self._cfg.roll_when_dte:
        return True, f"DTE={position.contract.dte} ≤ {self._cfg.roll_when_dte} — roll"

    # Stock stop loss: exit if underlying fell too far below cost basis
    stop_pct = getattr(self._cfg, "stock_stop_loss_pct", 0.90)
    if underlying_price is not None and stop_pct > 0:
        stop_price = position.stock_cost_basis * Decimal(str(stop_pct))
        if underlying_price < stop_price:
            return True, (
                f"stock_stop: underlying=${underlying_price:.2f} < "
                f"cost_basis × {stop_pct:.0%} = ${stop_price:.2f}"
            )

    return False, ""
```

- [ ] **Step 6: Update `_manage_cc` in wheel_strategy.py to pass underlying price**

```python
def _manage_cc(self, bar: BarEvent, pos: WheelPosition) -> list[SignalEvent]:
    """CC_OPEN state: check for early close."""
    if not pos.cc_position:
        pos.state = WheelState.SCANNING
        return []

    current_price = self._get_contract_price(pos.cc_position.contract.contract_id, pos)
    if current_price is None:
        return []

    should_close, reason = self._cc_leg.should_close_early(
        pos.cc_position,
        current_price,
        underlying_price=bar.close,   # pass current stock price for stop check
    )
    if should_close:
        logger.info(f"[Wheel] {bar.symbol}: Closing CC — {reason}")
        return [SignalEvent(
            strategy_id=self.strategy_id,
            symbol=bar.symbol,
            signal_type="BUY_TO_CLOSE_CALL",
            strength=1.0,
            timestamp=bar.timestamp,
            metadata={
                "leg": "cc_close",
                "contract_id": pos.cc_position.contract.contract_id,
                "reason": reason,
            },
        )]
    return []
```

- [ ] **Step 7: Run all tests**

```bash
python -m pytest tests/unit/test_cc_downside_exit.py tests/unit/test_roll_logic.py -v
```

Expected: All pass.

- [ ] **Step 8: Commit**

```bash
git add core/config.py config.yaml strategies/wheel/covered_call_leg.py strategies/wheel/wheel_strategy.py tests/unit/test_cc_downside_exit.py
git commit -m "feat: add stock stop loss to covered call leg — exit when underlying falls below cost basis threshold"
```

---

## Task 8: Fix Tier 1 Soft Stop for Pure IV Spikes

**Files:**
- Modify: `strategies/wheel/csp_leg.py:188–199`
- Modify: `core/config.py` — add `mark_stop_multiplier` to `CSPConfig`
- Modify: `config.yaml`
- Modify: `tests/unit/test_stop_loss_semantics.py`

**Context:** The Tier 1 soft stop requires BOTH `mark >= 2.5× credit` AND `underlying < strike`. An IV spike alone (stock still above strike) doesn't trigger it, allowing the liability to 3× or 4× without any exit. Adding a standalone mark-multiplier stop fixes this: if the option's mark reaches N× the credit regardless of stock direction, close to limit the loss.

- [ ] **Step 1: Add config field to CSPConfig in core/config.py**

```python
class CSPConfig(BaseModel):
    target_delta: float = -0.28
    min_dte: int = 21
    max_dte: int = 45
    profit_target_pct: float = 0.50
    stop_loss_multiplier: float = 2.0
    min_premium: float = 1.00
    min_iv_rank: float = 50.0
    roll_when_dte: int = 7
    pain_threshold_default: float = 0.85
    mark_stop_multiplier: float = 3.0  # Close if mark reaches 3× credit (IV spike stop)
```

- [ ] **Step 2: Add to config.yaml under strategies.wheel.csp**

```yaml
    csp:
      target_delta: -0.28
      min_dte: 21
      max_dte: 45
      profit_target_pct: 0.50
      stop_loss_multiplier: 2.0
      min_premium: 0.50
      min_iv_rank: 40
      roll_when_dte: 7
      pain_threshold_default: 0.85
      mark_stop_multiplier: 3.0   # Exit if option mark reaches 3× original credit
```

- [ ] **Step 3: Write a failing test for the new mark stop**

Add to `tests/unit/test_stop_loss_semantics.py`:

```python
def test_mark_stop_triggers_on_pure_iv_spike_above_strike():
    """Mark at 3× credit with underlying ABOVE strike (pure IV event) → close.
    Tier 1 (AND logic) would not catch this — the standalone mark stop must."""
    from core.config import CSPConfig
    cfg = MagicMock(spec=CSPConfig)
    cfg.profit_target_pct = 0.50
    cfg.stop_loss_multiplier = 2.0
    cfg.roll_when_dte = 7
    cfg.pain_threshold_default = 0.85
    cfg.mark_stop_multiplier = 3.0

    from strategies.wheel.csp_leg import CashSecuredPutLeg
    leg = CashSecuredPutLeg(cfg)

    pos = _make_position(strike=50.0, premium=1.50)
    # Mark jumped to 4.50 (3× credit) but stock is at $52 (above strike — OTM)
    closed, reason = leg.should_close_early(
        pos,
        current_contract_price=Decimal("4.50"),  # 3× premium
        current_underlying=Decimal("52.00"),      # above strike — Tier 1 won't fire
        dte=30,
    )
    assert closed is True
    assert "mark_stop" in reason.lower() or "3x" in reason.lower() or "3.0x" in reason.lower()


def test_mark_stop_does_not_trigger_below_multiplier():
    """Mark at 2× credit (below 3× threshold) with stock above strike → no exit."""
    from core.config import CSPConfig
    cfg = MagicMock(spec=CSPConfig)
    cfg.profit_target_pct = 0.50
    cfg.stop_loss_multiplier = 2.0
    cfg.roll_when_dte = 7
    cfg.pain_threshold_default = 0.85
    cfg.mark_stop_multiplier = 3.0

    from strategies.wheel.csp_leg import CashSecuredPutLeg
    leg = CashSecuredPutLeg(cfg)

    pos = _make_position(strike=50.0, premium=1.50)
    closed, _ = leg.should_close_early(
        pos,
        current_contract_price=Decimal("2.80"),  # < 3× premium ($4.50)
        current_underlying=Decimal("52.00"),
        dte=30,
    )
    assert closed is False
```

- [ ] **Step 4: Run to confirm failure**

```bash
python -m pytest tests/unit/test_stop_loss_semantics.py::test_mark_stop_triggers_on_pure_iv_spike_above_strike -v
```

Expected: FAIL

- [ ] **Step 5: Add the mark stop to `should_close_early` in csp_leg.py**

Insert after the existing Tier 1 block (around line 199), before Tier 2:

```python
# Standalone mark stop: option price reached N× credit regardless of underlying direction
# Catches pure IV-spike scenarios where Tier 1 (which requires underlying < strike) won't fire
mark_stop_mult = getattr(self._cfg, "mark_stop_multiplier", 3.0)
if mark_stop_mult > 0:
    mark_stop_threshold = position.premium_received * Decimal(str(mark_stop_mult))
    if current_contract_price >= mark_stop_threshold:
        return True, (
            f"mark_stop_{mark_stop_mult:.0f}x: mark=${current_contract_price:.2f} >= "
            f"{mark_stop_mult:.0f}× credit=${mark_stop_threshold:.2f}"
        )
```

Place this block **between** the Tier 1 block (ends line ~199) and Tier 2 (starts line ~201). The order is:
1. Profit target
2. Tier 1 soft stop (AND: mark ≥ 2.5× AND underlying < strike)
3. **NEW: Standalone mark stop (mark ≥ 3×, regardless of underlying)**
4. Tier 2 pain threshold (underlying < strike × 0.85)
5. DTE roll

- [ ] **Step 6: Run all stop loss tests**

```bash
python -m pytest tests/unit/test_stop_loss_semantics.py -v
```

Expected: All tests pass (existing + 2 new).

- [ ] **Step 7: Commit**

```bash
git add strategies/wheel/csp_leg.py core/config.py config.yaml tests/unit/test_stop_loss_semantics.py
git commit -m "feat: add standalone mark stop (3× credit) to CSP to catch IV-spike scenarios"
```

---

## Task 9: Harden Earnings Gate to Block Entry Within 7 Days

**Files:**
- Modify: `strategies/swing/swing_strategy.py:122–128`

**Context:** The earnings gate currently halves signal strength within 30 days of earnings instead of blocking. Binary earnings gaps are the biggest source of overnight losses in swing trades. Strength reduction doesn't prevent entry — it just reduces size by a fraction.

- [ ] **Step 1: Write a failing test**

Create `tests/unit/test_swing_earnings_gate.py`:

```python
"""Swing strategy must block entry entirely within 7 days of earnings."""
from unittest.mock import MagicMock, patch
import pytest


def _make_earnings_calendar(days_to_earnings: int):
    ec = MagicMock()
    ec.is_near_earnings = lambda sym, min_days: days_to_earnings <= min_days
    return ec


def _minimal_entry_snap():
    snap = MagicMock()
    snap.ema_trend_up = True
    snap.macd_hist = 0.05
    snap.rsi = 55.0
    snap.atr = 5.0
    snap.volume_ratio = 1.8
    return snap


def test_entry_blocked_within_7_days_of_earnings():
    """No entry signal within 7 days of earnings (hard block)."""
    from strategies.swing.swing_strategy import SwingStrategy
    from core.config import SwingStrategyConfig
    from decimal import Decimal
    from datetime import datetime, timezone
    from core.events import BarEvent

    cfg = SwingStrategyConfig(min_bars_for_entry=50, atr_stop_mult=2.0, atr_target_mult=4.0)
    ec = _make_earnings_calendar(days_to_earnings=5)  # 5 days away — must block

    with patch("strategies.swing.swing_strategy.settings") as ms:
        ms.indicators.bar_window_size = 200
        ms.strategies.swing = cfg
        s = SwingStrategy(symbols=["MSFT"], config=cfg, earnings_calendar=ec)

    snap = _minimal_entry_snap()
    prev = MagicMock()
    prev.ema_trend_up = True

    bar = MagicMock(spec=BarEvent)
    bar.symbol = "MSFT"
    bar.close = Decimal("400.00")
    bar.timestamp = datetime.now(timezone.utc)

    with patch.object(s, "_update_indicators", return_value=snap), \
         patch.object(s, "_get_prev_snapshot", return_value=prev), \
         patch.object(s, "_bars_available", return_value=True), \
         patch("strategies.swing.swing_strategy.classify_stage") as mock_stage, \
         patch("strategies.swing.swing_strategy.is_sad_panda", return_value=False):
        from analysis.stage_analysis import Stage
        mock_stage.return_value = Stage.STAGE_2
        signals = s.on_bar(bar)

    assert len(signals) == 0, "Must block entry within 7 days of earnings"


def test_entry_halved_strength_between_7_and_30_days():
    """Between 7–30 days of earnings, entry allowed but strength halved."""
    from strategies.swing.swing_strategy import SwingStrategy
    from core.config import SwingStrategyConfig
    from decimal import Decimal
    from datetime import datetime, timezone
    from core.events import BarEvent

    cfg = SwingStrategyConfig(min_bars_for_entry=50, atr_stop_mult=2.0, atr_target_mult=4.0)
    ec = _make_earnings_calendar(days_to_earnings=15)  # 15 days — soft zone

    with patch("strategies.swing.swing_strategy.settings") as ms:
        ms.indicators.bar_window_size = 200
        ms.strategies.swing = cfg
        s = SwingStrategy(symbols=["MSFT"], config=cfg, earnings_calendar=ec)

    snap = _minimal_entry_snap()
    prev = MagicMock()
    prev.ema_trend_up = True

    bar = MagicMock(spec=BarEvent)
    bar.symbol = "MSFT"
    bar.close = Decimal("400.00")
    bar.timestamp = datetime.now(timezone.utc)

    with patch.object(s, "_update_indicators", return_value=snap), \
         patch.object(s, "_get_prev_snapshot", return_value=prev), \
         patch.object(s, "_bars_available", return_value=True), \
         patch("strategies.swing.swing_strategy.classify_stage") as mock_stage, \
         patch("strategies.swing.swing_strategy.is_sad_panda", return_value=False):
        from analysis.stage_analysis import Stage
        mock_stage.return_value = Stage.STAGE_2
        signals = s.on_bar(bar)

    # Signal allowed but strength reduced
    assert len(signals) == 1
    assert signals[0].strength < 0.75, "Strength must be halved near earnings"
```

- [ ] **Step 2: Run to confirm the within-7-days test fails**

```bash
python -m pytest tests/unit/test_swing_earnings_gate.py::test_entry_blocked_within_7_days_of_earnings -v
```

Expected: FAIL — currently returns a signal instead of blocking.

- [ ] **Step 3: Fix `_check_entry` in swing_strategy.py**

Replace the existing earnings gate block (lines 122–128):

```python
# BEFORE:
# Soft earnings gate: halve strength if earnings are near (< 30 days)
if (
    self._earnings_calendar is not None
    and self._earnings_calendar.is_near_earnings(sym, min_days=30)
):
    logger.debug(f"[Swing] {sym} near earnings — reducing strength by 50%")
    strength *= 0.5

# AFTER:
# Hard earnings gate: block entirely within 7 days, halve strength within 30 days
if self._earnings_calendar is not None:
    if self._earnings_calendar.is_near_earnings(sym, min_days=7):
        logger.info(f"[Swing] {sym}: blocked — earnings within 7 days")
        return []
    if self._earnings_calendar.is_near_earnings(sym, min_days=30):
        logger.debug(f"[Swing] {sym}: near earnings (7–30 days) — reducing strength by 50%")
        strength *= 0.5
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/unit/test_swing_earnings_gate.py -v
```

Expected: Both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add strategies/swing/swing_strategy.py tests/unit/test_swing_earnings_gate.py
git commit -m "fix: block swing entry within 7 days of earnings (was only halving strength)"
```

---

## Task 10: Fix Assignment Cost Basis When csp_position Cleared Early

**Files:**
- Modify: `strategies/wheel/wheel_strategy.py:135–145`
- Modify: `tests/unit/test_assignment.py`

**Context:** In the `csp_close + assigned=True` path, `pos.csp_position = None` is set before `stock_cost_basis` is computed. The fallback `fill.metadata.get("cost_basis", fill.fill_price)` uses `fill.fill_price` (the option buyback price, e.g. `$5.00`) if metadata is missing — not the correct strike minus premium (e.g. `$28.00`). The CC leg would then sell calls above $5 instead of above $28.

- [ ] **Step 1: Write a failing test**

Add to `tests/unit/test_assignment.py`:

```python
def test_assignment_via_csp_close_uses_correct_cost_basis_when_metadata_missing():
    """
    When a CSP is closed with assigned=True and no cost_basis in metadata,
    fallback must use strike - premium_received, NOT the option's fill price.
    """
    from strategies.wheel.wheel_strategy import WheelStrategy, WheelState, WheelPosition
    from strategies.wheel.csp_leg import CSPPosition, OptionContract
    from core.events import FillEvent
    from decimal import Decimal
    from datetime import datetime, timezone
    from unittest.mock import MagicMock, patch

    with patch("strategies.wheel.wheel_strategy.settings") as ms:
        ms.indicators.bar_window_size = 200
        ms.strategies.wheel.csp.pain_threshold_default = 0.85
        ms.strategies.wheel.csp.min_iv_rank = 40
        w = WheelStrategy.__new__(WheelStrategy)
        w.symbols = ["AMD"]
        w.strategy_id = "wheel"
        w._positions = {"AMD": WheelPosition(symbol="AMD")}
        w._advisor = None

    # Set up an open CSP position
    from strategies.wheel.csp_leg import CashSecuredPutLeg
    from core.config import CSPConfig
    cfg_mock = MagicMock(spec=CSPConfig)
    cfg_mock.pain_threshold_default = 0.85
    w._csp_leg = CashSecuredPutLeg(cfg_mock)

    contract = MagicMock(spec=OptionContract)
    contract.strike = Decimal("28.00")
    pos = w._positions["AMD"]
    pos.csp_position = CSPPosition(
        symbol="AMD",
        contract=contract,
        premium_received=Decimal("1.20"),
        opened_at=datetime.now(timezone.utc),
    )
    pos.state = WheelState.CSP_OPEN

    # Fill for csp_close with assigned=True but NO cost_basis in metadata
    fill = MagicMock(spec=FillEvent)
    fill.strategy_id = "wheel"
    fill.symbol = "AMD"
    fill.side = "buy"
    fill.fill_price = Decimal("5.50")   # option buyback price (WRONG as cost basis)
    fill.filled_qty = 100
    fill.metadata = {"leg": "csp_close", "assigned": True}  # no cost_basis key

    w.on_fill(fill)

    expected_cost_basis = Decimal("28.00") - Decimal("1.20")  # strike - premium = $26.80
    assert pos.stock_cost_basis == expected_cost_basis, (
        f"Expected cost_basis={expected_cost_basis}, got {pos.stock_cost_basis}. "
        "Fallback must use strike - premium, not the option buyback price."
    )
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/unit/test_assignment.py::test_assignment_via_csp_close_uses_correct_cost_basis_when_metadata_missing -v
```

Expected: FAIL — cost_basis is `$5.50` instead of `$26.80`.

- [ ] **Step 3: Fix the on_fill csp_close handler in wheel_strategy.py**

```python
elif leg == "csp_close" and fill.side == "buy":
    pnl = float(pos.csp_position.premium_received - fill.fill_price) * 100 if pos.csp_position else 0
    logger.info(f"[Wheel] {sym}: CSP closed | P&L ≈ ${pnl:+.2f}")

    if fill.metadata.get("assigned"):
        # Compute cost basis BEFORE clearing csp_position
        if fill.metadata.get("cost_basis"):
            cost_basis = Decimal(str(fill.metadata["cost_basis"]))
        elif pos.csp_position:
            cost_basis = self._csp_leg.cost_basis_after_assignment(pos.csp_position)
        else:
            cost_basis = fill.fill_price  # last-resort fallback
        pos.csp_position = None
        pos.state = WheelState.ASSIGNED
        pos.stock_cost_basis = cost_basis
        pos.stock_quantity = int(fill.metadata.get("quantity", 100))
    else:
        pos.csp_position = None
        pos.state = WheelState.SCANNING
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/unit/test_assignment.py -v
```

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add strategies/wheel/wheel_strategy.py tests/unit/test_assignment.py
git commit -m "fix: compute assignment cost_basis before clearing csp_position — was using option buyback price as fallback"
```

---

## Task 11: Normalize drawdown_pct Units (Fraction vs Percent)

**Files:**
- Modify: `scheduler/scheduler.py:481`

**Context:** `market_context['drawdown_pct'] = float(self._portfolio.drawdown())` sends a fraction (0.05 = 5%). `portfolio.summary()['drawdown_pct']` returns a percent (5.0 = 5%). The `TradingAdvisor` multiplies by 100, which is correct today, but the two paths use different semantic contracts for the same key name. Standardize to percent everywhere to match `summary()`.

- [ ] **Step 1: Fix the line in scheduler.py**

Find `_on_bar` where `market_context` is built (around line 479–488):

```python
# BEFORE:
market_context = {
    "regime": self._risk._regime.value,
    "drawdown_pct": float(self._portfolio.drawdown()),
    ...
}

# AFTER:
market_context = {
    "regime": self._risk._regime.value,
    "drawdown_pct": float(self._portfolio.drawdown()) * 100,  # percent, matching summary()
    ...
}
```

- [ ] **Step 2: Check trading_advisor.py — confirm it no longer needs to multiply**

```bash
grep -n "drawdown_pct" /home/ivan8115/git/tradingBot/ai/trading_advisor.py
```

Find the line that does `market_context.get('drawdown_pct', 0) * 100` and remove the `* 100`:

```python
# BEFORE (in trading_advisor.py):
drawdown_pct = market_context.get('drawdown_pct', 0) * 100

# AFTER:
drawdown_pct = market_context.get('drawdown_pct', 0)   # already in percent
```

- [ ] **Step 3: Run tests**

```bash
python -m pytest tests/ -q 2>&1 | tail -20
```

Expected: All pass.

- [ ] **Step 4: Commit**

```bash
git add scheduler/scheduler.py ai/trading_advisor.py
git commit -m "fix: normalize drawdown_pct to percent (×100) in market_context to match portfolio.summary() convention"
```

---

## Task 12: Add Public record_rejected_signal() to Executor

**Files:**
- Modify: `execution/executor.py`
- Modify: `scheduler/scheduler.py:544`

**Context:** The scheduler directly calls `self._executor._save_signal()` — a private method — when the AI rejects a signal. This couples the scheduler to the Executor's internals and will silently break on any internal refactoring.

- [ ] **Step 1: Add a public method to Executor**

In `execution/executor.py`, add after `record_fill`:

```python
def record_rejected_signal(
    self,
    signal: SignalEvent,
    rejection_reason: str,
) -> None:
    """Public API for recording AI-rejected or pre-execution rejections."""
    self._save_signal(signal, approved=False, rejection_reason=rejection_reason)
```

- [ ] **Step 2: Update the scheduler call site**

In `scheduler/scheduler.py` around line 544:

```python
# BEFORE:
self._executor._save_signal(
    signal,
    approved=False,
    rejection_reason=f"AI: {eval_result.reasoning[:255]}",
)

# AFTER:
self._executor.record_rejected_signal(
    signal,
    rejection_reason=f"AI: {eval_result.reasoning[:255]}",
)
```

- [ ] **Step 3: Run tests**

```bash
python -m pytest tests/ -q 2>&1 | tail -20
```

Expected: All pass.

- [ ] **Step 4: Commit**

```bash
git add execution/executor.py scheduler/scheduler.py
git commit -m "refactor: add public Executor.record_rejected_signal() — remove direct _save_signal call from scheduler"
```

---

## Final Verification

- [ ] **Run the full test suite**

```bash
cd /home/ivan8115/git/tradingBot && python -m pytest tests/ -v 2>&1 | tail -40
```

Expected: All tests pass, no regressions.

- [ ] **Check for any import errors**

```bash
python -c "from strategies.wheel.wheel_strategy import WheelStrategy; from strategies.swing.swing_strategy import SwingStrategy; from risk.risk_manager import RiskManager; from execution.executor import Executor; print('All imports OK')"
```

Expected: `All imports OK`

- [ ] **Verify config loads cleanly**

```bash
python -c "from core.config import settings; print('Config OK — mode:', settings.system.mode)"
```

Expected: `Config OK — mode: paper`

---

## Self-Review Checklist

- [x] **Spec coverage:** All 12 findings from the code review are addressed (T1=P1-A, T2=P1-B, T3=P1-C, T4=P1-D, T5=P1-E, T6=P2-A, T7=P2-B, T8=P2-C, T9=P2-D, T10=P3-A, T11=P3-B, T12=P3-C)
- [x] **Placeholder scan:** No TBDs. All code blocks show actual implementation.
- [x] **Type consistency:** `Decimal` used throughout options math. `underlying_price` passed as `Decimal | None` in T7 matching existing patterns. `float` used in config values matching existing `CCConfig` fields.
- [x] **Test isolation:** All tests create fresh instances. No shared mutable state between tests.
- [x] **Tasks are independent:** Each task can be executed in any order. No task depends on another task's code changes.
