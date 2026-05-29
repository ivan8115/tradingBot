# TODO — Issues Found But Not Fixed in This Pass

These issues were identified during the May 2026 code review. They are not blocking for
the current $10K paper trading phase but should be addressed before increasing capital.

## 1. Earnings date data is stale (researcher.py uses Claude, no real-time access)

`researcher.check_earnings_dates()` asks Claude Haiku for earnings dates. Claude's training
cutoff means these can be wrong. The earnings filter is advisory only.

**Fix:** Add a dedicated earnings API (Polygon.io calendar or Alpaca corporate actions
endpoint). Until then, manually cross-check earnings before any weekly options trade.

## 2. Signal evaluation rate limit (20/day) may be too low

With 6 positions × multiple symbols on the watchlist, the 20 Sonnet eval cap
(`claude.max_signal_evals_per_day`) could be exhausted before EOD on active days.

**Fix:** Raise to 50 or make configurable in `config.yaml` under `claude.max_signal_evals_per_day`.

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
