# Wheel Metrics Dashboard Section

**Date:** 2026-05-27
**Status:** Approved

## Goal

Add a "Wheel Metrics" section to the existing dashboard (`localhost:8000`) that surfaces the five key numbers needed to evaluate whether the Wheel strategy is working. These map directly to the success/failure criteria agreed on before starting paper trading.

## Success Criteria (context)

- Premium ≥1% of collateral per month per position
- CSP win rate >60% (puts expire worthless more often than not)
- No runaway losses — single position should not consume >20% of account
- After 30 days and 3 full cycles: assess whether to continue or adjust entry criteria

---

## Backend

### New endpoint: `GET /api/metrics`

File: `dashboard/app.py`

Queries two existing tables:

**WheelCycle queries:**
- `cycles_completed` — count of rows where `completed = True`
- `cycles_active` — count of rows where `completed = False`
- `premium_this_month` — sum of `total_premium_collected` for rows where `started_at` falls in the current calendar month (both complete and active)
- `csp_win_rate` — among completed cycles only: fraction where `stock_cost_basis IS NULL`. A null cost basis means the put expired worthless and shares were never assigned. Returns 0.0 if no completed cycles.

**PortfolioSnapshot queries:**
- `current_drawdown_pct` — `(peak_total_value - latest_total_value) / peak_total_value`. Uses `max(total_value)` as peak and the most recent row (by `recorded_at`) as current. Returns 0.0 if no snapshots exist.

**Response shape:**
```json
{
  "premium_this_month": 142.50,
  "csp_win_rate": 0.67,
  "cycles_completed": 6,
  "cycles_active": 2,
  "current_drawdown_pct": 0.012
}
```

All values are floats. `premium_this_month` is cast from Decimal. `csp_win_rate` and `current_drawdown_pct` are ratios (multiply by 100 for display).

---

## Frontend

### New section in `dashboard/static/index.html`

Inserted between the "Wheel Strategy" section and the "Performance" section.

**HTML:** A `<section>` with `id="wheel-metrics-stats"` using the existing `.stat-card` / `.stat-label` / `.stat-value` grid layout (same as `#perf-stats`). Five cards:

| Card | ID | Value |
|------|----|-------|
| Premium This Month | `stat-premium-month` | `fmt$()` |
| CSP Win Rate | `stat-csp-winrate` | `fmtPct()` + color |
| Cycles Completed | `stat-cycles-done` | integer |
| Active Cycles | `stat-cycles-active` | integer |
| Current Drawdown | `stat-wm-drawdown` | `fmtPct()` |

**Color coding on CSP Win Rate:**
- ≥ 60% → `var(--positive)` (green)
- 40–59% → `var(--amber)` (amber)
- < 40% → `var(--negative)` (red)

**JS:** New `loadWheelMetrics()` function. Fetches `/api/metrics` once on page load. Called alongside `loadPerformance()` and `loadTrades()` in the init block. No WebSocket changes.

---

## What is NOT changing

- No changes to `WheelCycle` or `PortfolioSnapshot` DB models
- No changes to the WebSocket push logic
- No changes to existing `/api/performance` endpoint
- No new CSS classes — reuses existing `.stat-card`, `.stat-label`, `.stat-value`
