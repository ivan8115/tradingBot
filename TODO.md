# TODO — Issues Found But Not Fixed in This Pass

These issues were identified during the May 2026 code review. They are not blocking for
the current $10K paper trading phase but should be addressed before increasing capital.

## 1. Earnings date data is stale (researcher.py uses Claude, no real-time access)

`researcher.check_earnings_dates()` asks Claude Haiku for earnings dates. Claude's training
cutoff means these can be wrong. The earnings filter is advisory only.

**Fix:** Add a dedicated earnings API (Polygon.io calendar or Alpaca corporate actions
endpoint). Until then, manually cross-check earnings before any weekly options trade.

## 2. Gap-down check uses most-recent daily close, not true pre-market quote

`_get_current_price()` fetches the most recent daily bar close. True pre-market gap detection
requires extended-hours quotes from Alpaca (available via their quotes API).

**Impact:** A gap-down that occurs after yesterday's close but before 8:15 AM won't be detected
until the stock prints a new daily bar. The >10% threshold still catches multi-day moves.

**Fix:** Switch to Alpaca's `get_latest_quote()` with `feed="iex"` or similar to get a real-time
pre-market price.

## 3. Equity fills not enriched — Swing/Momentum on_fill never fires on live fills

`submit_limit_order` and `submit_market_order` in `execution/executor.py` do not set
`client_order_id` and do not populate `_pending_order_metadata`. Live equity fills
arrive with `strategy_id` derived from Alpaca's auto-generated order UUID, which never
matches the strategy's `strategy_id`. Every `on_fill` in `SwingStrategy` and
`MomentumStrategy` filters on `fill.strategy_id != self.strategy_id` and returns early.

**Impact:** Position state (open/closed flags, hold bars, stop levels) never updates
from live fills — exits driven by `on_fill` won't fire.

**Fix:** Apply the same pending registry pattern to the equity submit paths in
`executor.execute_signal`, passing `client_order_id=f"{signal.strategy_id}-{uuid.uuid4().hex[:12]}"`.

**Priority:** Low — Swing and Momentum are currently disabled (PDT risk on $10K account).
Fix before re-enabling either strategy.
