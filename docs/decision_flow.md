# Decision Flow — 5-Layer Pipeline

Every CSP entry flows through five layers. Each layer can reject independently.
All decisions are logged to `logs/decisions/YYYY-MM-DD.jsonl` with the stage name below.

## Layer 1 — Mechanical Filter
**File:** `strategies/wheel/wheel_strategy.py` → `_evaluate_entry()`
**Log stage:** `wheel/mechanical_filter`, `wheel/entry_signal`
**Checks:** IV rank ≥ 40, trend not "downtrend", options chain available
**IV Rank source:** seeded from 252 daily ATR bars on first chain refresh per symbol (one attempt); updated from live ATM chain IV every 15 min thereafter
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
1. Max drawdown (15% halt) — computed from mark-to-market portfolio value including unrealized equity P&L
2. Daily loss limit (3%) — same mark-to-market basis
3. Total collateral cap (80% of account) — tracked as committed CSP collateral internally; not derived from portfolio cash (which stays flat for cash-secured puts until assignment)
4. Position concentration (20% per symbol)
5. Net delta exposure (±500)
6. Market regime (no entries in BEARISH)
7. Max open positions (6)
8. Max weekly trades (3)
**Rejection:** `ValidationResult.approved = False`; reason in `rejection_reason`

## Layer 5 — Execution
**File:** `execution/executor.py`
**Checks:** qty > 0, contract_id valid, Alpaca accepts the order
**Position sizing:** Options signals always use qty=1 contract; equity signals use Kelly (0.25 fractional)
**On fill:** `on_fill()` transitions wheel state machine AND creates `CSPPosition`/`CCPosition` objects from the cached chain — these objects are required for exit monitoring in Layers 5a/5b below
**Fill enrichment:** Live Alpaca fills arrive with empty metadata. `Executor` stores `{order_id → signal.metadata}` at submit time; `scheduler._on_fill` injects `leg`, `contract_id`, `underlying_price`, and correct `strategy_id` before routing. All Wheel fill paths (CSP open/close, CC open/close, assignment) are enriched. Options orders submit with `client_order_id=f"{strategy_id}-{uuid12}"` as a secondary mechanism. Equity fills (Swing/Momentum) are not yet enriched — low priority while both strategies are disabled.
**Restart safety:** `CSPPosition`/`CCPosition` are fully serialized to `data/strategy_state.json` on every fill (contract data + premium + opened_at). On restart, positions are reconstructed with DTE recalculated from stored expiry date.

## CSP Exit Monitoring (ongoing, not an entry gate)
**File:** `strategies/wheel/wheel_strategy.py` → `_manage_csp()` → `csp_leg.should_close_early()`
**Triggers (checked in order):**
1. **Profit target:** mark ≤ 50% of premium received
2. **Tier 1 soft stop:** mark ≥ 2.5× credit AND underlying < strike (directional move confirmed)
3. **Mark stop (Tier 1.5):** mark ≥ 3× credit regardless of stock direction (catches pure IV-spike events where Tier 1's AND-logic won't fire)
4. **Pain threshold (Tier 2):** underlying < strike × 0.85 (AMD/MARA: 0.80)
5. **DTE roll:** contract ≤ 7 DTE
**Log stage:** `wheel/csp_exit` (on trigger)

## Covered Call Exit Monitoring (ongoing, not an entry gate)
**File:** `strategies/wheel/wheel_strategy.py` → `_manage_cc()` → `covered_call_leg.should_close_early()`
**Triggers (checked in order):**
1. **Profit target:** mark ≤ 50% of premium received
2. **DTE roll:** contract ≤ 7 DTE
3. **Stock stop loss:** underlying < stock cost basis × 0.90 (exits stock position if it falls more than 10% below purchase price)
**Log stage:** `wheel/cc_exit` (on trigger)
