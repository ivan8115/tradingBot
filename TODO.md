# TODO — Issues Found But Not Fixed in This Pass

These issues were identified during the May 2026 code review. They are not blocking for
the current $10K paper trading phase but should be addressed before increasing capital.

## 1. Earnings date data is stale (researcher.py uses Claude, no real-time access)

`researcher.check_earnings_dates()` asks Claude Haiku for earnings dates. Claude's training
cutoff means these can be wrong. The earnings filter is advisory only.

**Fix:** Add a dedicated earnings API (Polygon.io calendar or Alpaca corporate actions
endpoint). Until then, manually cross-check earnings before any weekly options trade.

## 2. Live Alpaca fills missing `leg` / `contract_id` metadata — Wheel state machine stalls

`WheelStrategy.on_fill` dispatches entirely on `fill.metadata.get("leg")`. Regular buy/sell
fills from Alpaca's trade-update stream arrive with empty metadata — no `leg`, no `contract_id`.
This means the state machine never advances from `CSP_OPEN` on a real fill (it only works in
tests where metadata is hand-built). Assignment fills already carry `leg="assignment"` and work.

**Impact:** After a CSP sell is filled live, the bot stays in `SCANNING` and may attempt to sell
another CSP. No other part of the Wheel cycle will function until this is fixed.

**Fix:** In the executor or scheduler, after submitting an options order, stash the signal
metadata keyed by `order_id`. When the fill arrives via trade-update stream, look up the
originating signal by `order.id` and inject `leg`, `contract_id`, and `underlying_price`
into `FillEvent.metadata` before routing to `on_fill`.

## 3. Gap-down check uses most-recent daily close, not true pre-market quote

`_get_current_price()` fetches the most recent daily bar close. True pre-market gap detection
requires extended-hours quotes from Alpaca (available via their quotes API).

**Impact:** A gap-down that occurs after yesterday's close but before 8:15 AM won't be detected
until the stock prints a new daily bar. The >10% threshold still catches multi-day moves.

**Fix:** Switch to Alpaca's `get_latest_quote()` with `feed="iex"` or similar to get a real-time
pre-market price.

## 4. WheelPosition has no on-disk persistence

State machine state (`WheelState`, `csp_position`, etc.) lives in memory. A bot restart
loses all state and the strategy treats all symbols as SCANNING.

**Impact:** After a crash or restart, the bot may attempt to re-enter CSP positions that are
already open, potentially doubling up collateral.

**Fix:** Add DB-backed state load/save. Write state to SQLite on every fill. Load on startup.
