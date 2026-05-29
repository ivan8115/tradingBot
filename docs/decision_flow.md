# Decision Flow — 5-Layer Pipeline

Every CSP entry flows through five layers. Each layer can reject independently.
All decisions are logged to `logs/decisions/YYYY-MM-DD.jsonl` with the stage name below.

## Layer 1 — Mechanical Filter
**File:** `strategies/wheel/wheel_strategy.py` → `_evaluate_entry()`
**Log stage:** `wheel/mechanical_filter`, `wheel/entry_signal`
**Checks:** IV rank ≥ 40, trend not "downtrend", options chain available
**Rejection:** returns `[]` (no signal generated); logged with `decision: "reject"`

## Layer 2 — Sonnet Approval
**File:** `ai/trading_advisor.py` → `evaluate_signal()`
**Log stage:** `scheduler/sonnet_eval`
**Checks:** Contextual review — regime, drawdown, current portfolio state, market themes
**Shadow baseline:** `shadow_decision.approved = True` (mechanical filter already vetted the signal)
**Rejection:** `eval_result.approved = False`; scheduler skips order

## Layer 3 — Opus Strike Selection
**File:** `ai/trading_advisor.py` → `select_csp_strike()`
**Log stage:** `llm/select_csp_strike`
**Action:** Picks best contract from top 30 options chain candidates
**Shadow baseline:** `shadow_decision` shows contract closest to -0.28 delta (mechanical choice)
**Fallback:** If Opus fails, mechanical fallback picks closest to -0.28 delta

## Layer 4 — Risk Manager
**File:** `risk/risk_manager.py` → `validate_signal()`
**Log stage:** `risk_manager`
**Checks (in order):**
1. Max drawdown (15% halt)
2. Daily loss limit (3%)
3. Total collateral cap (80% of account)
4. Position concentration (20% per symbol)
5. Net delta exposure (±500)
6. Market regime (no entries in BEARISH)
7. Max open positions (6)
8. Max weekly trades (3)
**Rejection:** `ValidationResult.approved = False`; reason in `rejection_reason`

## Layer 5 — Execution
**File:** `execution/executor.py`
**Checks:** qty > 0, contract_id valid, Alpaca accepts the order
**On fill:** `on_fill()` transitions wheel state machine

## Exit Monitoring (ongoing, not an entry gate)
**File:** `strategies/wheel/wheel_strategy.py` → `_manage_csp()`
**Triggers:**
- Profit target: mark ≤ 50% of premium received
- Soft stop: mark ≥ 2.5× credit AND underlying < strike (directional move confirmed)
- Pain threshold: underlying < strike × 0.85 (AMD/MARA: 0.80)
- DTE roll: contract ≤ 7 DTE
**Log stage:** `wheel/csp_exit` (on trigger)
