# Trading Bot: Core Fixes + Web Dashboard Design

**Date:** 2026-04-10  
**Status:** Approved  
**Scope:** Fix 6 critical gaps preventing live trading, add web dashboard

---

## Context

The existing codebase has a solid architecture (event-driven, strategy/risk/execution separated, backtesting isolated from live) but contains 6 critical implementation gaps that prevent the bot from actually trading options. This spec covers fixing those gaps and adding a web dashboard.

**Constraints:**
- Account size: under $25K (paper trading only for now)
- PDT-aware: avoid frequent same-day equity round-trips
- Paper trading validates before any live deployment

---

## Section 1: Core Bot Fixes

### 1. Wire Wheel Strategy into Live Trading

**File:** `main.py` — `cmd_trade()`

Currently only `MomentumStrategy` is instantiated. Add `WheelStrategy` instantiation when `strategies.wheel.enabled: true` in config. Both strategies pass through the same scheduler → risk → executor pipeline.

### 2. Live Options Chain Fetching

**Files:** `broker/client.py`, `scheduler/scheduler.py`

Add `BrokerClient.get_options_chain(symbol, dte_min, dte_max, option_type)` that calls Alpaca's options data API and normalizes the response into the existing `OptionContract` dataclass.

Add `_refresh_options_chains()` scheduler job firing every 15 minutes during market hours. For each Wheel symbol, fetches the chain and calls `wheel.update_options_chain(symbol, chain)`. Without this, `cached_chain` is always empty and the Wheel never generates signals.

### 3. Assignment Detection

**File:** `broker/market_data.py`

Subscribe to Alpaca's `trade_updates` WebSocket stream alongside the existing bar stream. When an event with `event_type="assignment"` arrives, convert it into a `FillEvent` with `metadata={"leg": "assignment"}` and route it to `strategy.on_fill()`. This triggers the existing `ASSIGNED → CC_OPEN` state transition in `WheelStrategy`.

### 4. Options Order Execution

**Files:** `execution/executor.py`, `broker/client.py`

Add `BrokerClient.submit_options_order(contract_symbol, qty, side, order_type, limit_price)` using Alpaca's options-specific endpoint. The OCC contract symbol format is used (e.g., `AAPL230120P00150000`).

In the executor, add a branch for options signal types (`SELL_PUT`, `SELL_CALL`, `BUY_TO_CLOSE_PUT`, `BUY_TO_CLOSE_CALL`) that extracts `contract_id` from `signal.metadata` and calls `submit_options_order`. Equity orders remain unchanged.

### 5. Roll Logic

**File:** `strategies/wheel/wheel_strategy.py`

In `_manage_csp()` and `_manage_cc()`: if `contract.dte < cfg.cc.roll_when_dte` (default: 7), emit a roll signal — close the current contract and re-open at the nearest expiry that is 21–45 DTE from today (matching the existing `min_dte`/`max_dte` config). Prevents expiration-week gamma risk.

The `_check_dte_warnings()` scheduler stub is updated to log a structured warning when any position is within roll threshold, serving as a fallback audit log.

### 6. Symbol Selection for Under-$25K Account

**File:** `config.yaml`

Replace Wheel symbols (currently AAPL, TSLA) with under-$25K-friendly underlyings:
- **AMD** (~$120 strike = ~$12K to secure one CSP contract)
- **MARA** (~$15 strike = ~$1.5K per contract, lower premium but scalable)

Momentum symbols (SPY, QQQ) are fine as-is — they're equity-only, no capital-securing requirement.

One Wheel position runs at a time given account size. The position sizer enforces this via the existing `max_single_position_pct: 0.10` config (prevents over-allocating).

---

## Section 2: Web Dashboard

### Architecture

Two independent processes communicating through the shared SQLite database and live Alpaca API calls. No shared in-process state.

```
Trading Bot (main.py trade)     Dashboard (dashboard/app.py)
        |                               |
        v                               v
   SQLite DB  <---- reads/writes ---->  FastAPI
   Alpaca API  <---- live calls ----->  FastAPI
                                        |
                                        v
                                   Browser (localhost:8000)
```

### Backend: FastAPI (`dashboard/app.py`)

| Endpoint | Source | Returns |
|----------|--------|---------|
| `GET /api/account` | Alpaca live | Cash, equity, buying power, mode |
| `GET /api/positions` | Alpaca live | Open positions with unrealized P&L |
| `GET /api/strategy-state` | SQLite + in-memory state file | Wheel state per symbol, Momentum in/out |
| `GET /api/trades` | SQLite `trades` table | Last 100 trades |
| `GET /api/performance` | SQLite `trades` table | Equity curve, win rate, avg P&L, max drawdown |
| `GET /api/alerts` | SQLite `signals` table (rejected) | Recent rejections and warnings |
| `WS /ws` | Poll above APIs | Pushes updates every 5 seconds |

Strategy state is persisted to a JSON sidecar file (`data/strategy_state.json`) by the bot on each state transition. The dashboard reads this file — no direct coupling to strategy objects.

### Frontend: Single HTML Page (`dashboard/static/index.html`)

Four sections, auto-refreshing via WebSocket:

**Account bar (top):** Cash | Equity | Buying Power | PAPER/LIVE badge

**Strategy cards:** One card per active symbol showing:
- Wheel: state chip (SCANNING / CSP_OPEN / ASSIGNED / CC_OPEN), open contract strike/expiry/DTE, total premium collected this cycle
- Momentum: IN / OUT chip, last signal direction

**Live positions table:** Symbol | Side | Qty | Entry Price | Current Price | Unrealized P&L (green/red)

**Performance panel:**
- Equity curve chart (Chart.js line chart)
- Summary stats: Total Return % | Win Rate | Max Drawdown | Total Trades
- Scrollable trade history table below

### Dependencies Added

```
fastapi
uvicorn
```

Chart.js loaded from CDN in the HTML page. No build step, no npm.

### Running

```bash
# Terminal 1
python main.py trade --mode paper

# Terminal 2  
python -m dashboard.app

# Browser
open http://localhost:8000
```

---

## Files Changed / Created

| File | Action |
|------|--------|
| `main.py` | Update `cmd_trade` to load WheelStrategy |
| `config.yaml` | Update Wheel symbols to AMD + MARA |
| `broker/client.py` | Add `get_options_chain()`, `submit_options_order()` |
| `broker/market_data.py` | Subscribe to `trade_updates` for assignments |
| `scheduler/scheduler.py` | Add `_refresh_options_chains()` job |
| `strategies/wheel/wheel_strategy.py` | Add roll logic in `_manage_csp` / `_manage_cc` |
| `execution/executor.py` | Add options order branch |
| `dashboard/__init__.py` | New |
| `dashboard/app.py` | New — FastAPI app |
| `dashboard/static/index.html` | New — single-page UI |

---

## Out of Scope

- Mean reversion and breakout strategies (Phase 6 — validate Wheel + Momentum first)
- Live trading (paper validation required first)
- Mobile/responsive dashboard design
- Multi-user auth on dashboard (local use only)
