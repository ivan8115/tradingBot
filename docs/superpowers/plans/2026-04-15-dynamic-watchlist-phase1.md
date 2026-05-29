# Dynamic Watchlist — Phase 1 (Free Sources) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace hardcoded Wheel symbols (AMD, MARA) with a daily-scanned dynamic watchlist built from Finviz screener + optional QuiverQuant congressional trade enrichment — all free.

**Architecture:** A new `WatchlistProvider` class runs pre-market via the scheduler, scans Finviz for Wheel-eligible candidates (price range, has options, options volume), optionally boosts scores using QuiverQuant congressional trade data, persists results to a new `watchlist_candidates` DB table, and feeds the dynamic symbol list into `WheelStrategy`. A new dashboard endpoint exposes today's watchlist for visibility.

**Tech Stack:** Python `finviz` library (pip install finviz), `httpx` (already used), SQLAlchemy (existing), APScheduler (existing), QuiverQuant REST API (free tier, 500 calls/day).

---

## What You Need to Provide Before Starting

1. **QuiverQuant API key** (optional but recommended):
   - Register free at `quiverquant.com` → Settings → API Access → copy your token
   - Add `QUIVERQUANT_API_KEY=<your_token>` to your `.env` file
   - If you skip this, the bot still works — it just won't use congressional trade data
2. **Nothing else** — Finviz screener requires no API key

---

## Phase Roadmap

| Phase | Sources | Cost | Gate |
|-------|---------|------|------|
| **Phase 1A (this plan)** | Finviz + QuiverQuant free | $0/mo | Build now |
| **Phase 1B** | Add Barchart UOA (free page) | $0/mo | After 30 days paper |
| **Phase 2** | Unusual Whales API | ~$50/mo | After consistent profit |
| **Phase 3** | Finviz Elite | ~$25/mo | After Phase 2 validates |

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `core/config.py` | Add `WatchlistConfig` model + `quiverquant_api_key` to Settings |
| Modify | `database/models.py` | Add `WatchlistCandidate` ORM model |
| Modify | `database/migrations.py` | Create `watchlist_candidates` table |
| **Create** | `data/watchlist_provider.py` | Finviz scan + QQ enrichment + scoring + DB persistence |
| **Create** | `tests/unit/test_watchlist_provider.py` | Unit tests (mocked HTTP) |
| Modify | `strategies/wheel/wheel_strategy.py` | Add `sync_symbols()` for dynamic symbol list |
| Modify | `scheduler/scheduler.py` | Add pre-market watchlist refresh job |
| Modify | `dashboard/app.py` | Add `GET /api/watchlist` endpoint |

---

## Task 1: WatchlistConfig + quiverquant_api_key in config.py

**Files:**
- Modify: `core/config.py`

- [ ] **Step 1: Add `WatchlistConfig` model to `core/config.py`**

Insert after `SchedulerConfig` (around line 119), before `MonitoringConfig`:

```python
class WatchlistConfig(BaseModel):
    max_symbols: int = 15           # max Wheel candidates per day
    min_price: float = 10.0        # stock price floor
    max_price: float = 150.0       # stock price ceiling (100 shares = $15K max collateral)
    min_options_volume: int = 200  # minimum daily options volume
    quiverquant_boost: bool = True  # weight candidates with recent congressional buys
    refresh_hour: int = 8          # pre-market scan time (ET)
    refresh_minute: int = 30
```

- [ ] **Step 2: Add `quiverquant_api_key` and `watchlist` to `Settings`**

In `Settings` class, add after `alert_email_to`:

```python
quiverquant_api_key: str = ""
```

In `Settings` class, add to the `# From config.yaml` section after `monitoring`:

```python
watchlist: WatchlistConfig = Field(default_factory=WatchlistConfig)
```

- [ ] **Step 3: Verify config loads cleanly**

```bash
cd /home/ivan8115/git/tradingBot && \
.venv/bin/python -c "from core.config import settings; print(settings.watchlist)"
```

Expected: `WatchlistConfig(max_symbols=15, min_price=10.0, ...)` with no errors.

- [ ] **Step 4: Commit**

```bash
git add core/config.py
git commit -m "feat: add WatchlistConfig + quiverquant_api_key to settings"
```

---

## Task 2: WatchlistCandidate DB Model + Migration

**Files:**
- Modify: `database/models.py`
- Modify: `database/migrations.py`

- [ ] **Step 1: Add `WatchlistCandidate` to `database/models.py`**

Append after the `PortfolioSnapshot` class at the end of the file:

```python
class WatchlistCandidate(Base):
    """Daily Wheel candidate from automated screener scan."""

    __tablename__ = "watchlist_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    scan_date: Mapped[str] = mapped_column(String(16), index=True)   # YYYY-MM-DD
    price: Mapped[float] = mapped_column(Float)
    iv_proxy: Mapped[float] = mapped_column(Float)                    # Finviz volatility %
    options_volume: Mapped[int] = mapped_column(Integer)
    quiverquant_score: Mapped[float] = mapped_column(Float, default=0.0)
    final_score: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32), default="finviz")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    added_at: Mapped[datetime] = mapped_column(DateTime)
```

- [ ] **Step 2: Read `database/migrations.py` to understand pattern, then add table creation**

Open [database/migrations.py](database/migrations.py) and find where `Base.metadata.create_all` is called or where individual tables are created. Add `WatchlistCandidate` to the same import and ensure the table is created on `init_db()`.

The import line at the top of `migrations.py` likely references model classes — add `WatchlistCandidate` to it:

```python
from database.models import (
    Base,
    PortfolioSnapshot,
    Position,
    Signal,
    Trade,
    WatchlistCandidate,
    WheelCycle,
)
```

(Adjust if the existing import already uses `Base.metadata.create_all(engine)` which auto-creates all tables registered with `Base` — in that case no extra line needed, just confirm `WatchlistCandidate` imports alongside the other models so SQLAlchemy registers it.)

- [ ] **Step 3: Verify table creates**

```bash
cd /home/ivan8115/git/tradingBot && \
.venv/bin/python -c "
from database.migrations import init_db
init_db('data/test_watchlist.db')
import sqlite3, os
conn = sqlite3.connect('data/test_watchlist.db')
tables = conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()
print([t[0] for t in tables])
conn.close()
os.remove('data/test_watchlist.db')
"
```

Expected output includes `'watchlist_candidates'`.

- [ ] **Step 4: Commit**

```bash
git add database/models.py database/migrations.py
git commit -m "feat: add WatchlistCandidate DB model and migration"
```

---

## Task 3: Install finviz + httpx

**Files:** None (dependency install)

- [ ] **Step 1: Install finviz**

```bash
cd /home/ivan8115/git/tradingBot && \
.venv/bin/pip install finviz
```

Expected: `Successfully installed finviz-...`

- [ ] **Step 2: Verify httpx is already installed**

```bash
.venv/bin/python -c "import httpx; print(httpx.__version__)"
```

If not installed: `.venv/bin/pip install httpx`

- [ ] **Step 3: Spot-check finviz works (no API key needed)**

```bash
.venv/bin/python -c "
from finviz.screener import Screener
s = Screener(filters=['sh_opt_option', 'sh_price_o10', 'sh_price_u150'], table='Overview', order='-volume', rows=5)
for stock in s:
    print(dict(stock))
    break
"
```

Expected: prints a dict with `Ticker`, `Price`, `Volume`, etc. **Note which keys are present** — you'll need the exact column names for Task 4.

- [ ] **Step 4: Commit requirements if needed**

If you have a `requirements.txt`:
```bash
cd /home/ivan8115/git/tradingBot && \
.venv/bin/pip freeze | grep -i finviz >> requirements.txt
git add requirements.txt
git commit -m "chore: add finviz dependency"
```

---

## Task 4: WatchlistProvider (Finviz scan + QuiverQuant)

**Files:**
- Create: `data/watchlist_provider.py`
- Create: `tests/unit/test_watchlist_provider.py`

- [ ] **Step 1: Write failing tests first**

Create `tests/unit/test_watchlist_provider.py`:

```python
"""Tests for WatchlistProvider — all external HTTP calls are mocked."""
from __future__ import annotations

import os
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from data.watchlist_provider import WatchlistEntry, WatchlistProvider, _days_ago


# ---------------------------------------------------------------------------
# _days_ago helper
# ---------------------------------------------------------------------------

def test_days_ago_today():
    from datetime import date
    today_str = date.today().isoformat()
    assert _days_ago(today_str) == 0


def test_days_ago_old_date():
    assert _days_ago("2000-01-01") > 1000


def test_days_ago_bad_string():
    assert _days_ago("not-a-date") == 999


# ---------------------------------------------------------------------------
# WatchlistEntry scoring
# ---------------------------------------------------------------------------

def test_entry_defaults():
    e = WatchlistEntry(symbol="AAPL", price=150.0, iv_proxy=30.0, options_volume=1000)
    assert e.quiverquant_score == 0.0
    assert e.final_score == 0.0


# ---------------------------------------------------------------------------
# WatchlistProvider._scan_finviz
# ---------------------------------------------------------------------------

@patch("data.watchlist_provider.Screener")
def test_scan_finviz_filters_price(mock_screener_cls, tmp_path, monkeypatch):
    """Stocks outside price range are excluded."""
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")

    # Simulate two Finviz results: one in range, one too cheap
    mock_screener = MagicMock()
    mock_screener.__iter__ = MagicMock(return_value=iter([
        {"Ticker": "AMD", "Price": "120.00", "Volatility": "40%", "Volume": "5000000"},
        {"Ticker": "CHEAP", "Price": "2.00", "Volatility": "80%", "Volume": "999999"},
    ]))
    mock_screener_cls.return_value = mock_screener

    from core.config import settings
    monkeypatch.setattr(settings.watchlist, "min_price", 10.0)
    monkeypatch.setattr(settings.watchlist, "max_price", 150.0)
    monkeypatch.setattr(settings.watchlist, "min_options_volume", 0)
    monkeypatch.setattr(settings, "system", MagicMock(db_path=str(tmp_path / "t.db")))

    provider = WatchlistProvider.__new__(WatchlistProvider)
    provider._cfg = settings.watchlist
    provider._api_key = ""

    # Patch _scan_finviz to avoid DB dependency in this unit test
    raw = provider._scan_finviz.__func__  # we'll call with mock
    # Just verify filtering logic via direct call
    entries = provider._scan_finviz()
    assert all(e.symbol != "CHEAP" for e in entries)


@patch("data.watchlist_provider.Screener")
def test_scan_finviz_exception_returns_empty(mock_screener_cls, monkeypatch):
    """If Finviz throws, we return [] gracefully."""
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    mock_screener_cls.side_effect = Exception("network error")

    from data.watchlist_provider import WatchlistProvider
    provider = WatchlistProvider.__new__(WatchlistProvider)
    from core.config import settings
    provider._cfg = settings.watchlist
    provider._api_key = ""
    result = provider._scan_finviz()
    assert result == []


# ---------------------------------------------------------------------------
# QuiverQuant enrichment
# ---------------------------------------------------------------------------

@patch("data.watchlist_provider.httpx.get")
def test_enrich_quiverquant_adds_score(mock_get, monkeypatch):
    """Recent congressional buys bump the quiverquant_score."""
    from datetime import date, timedelta
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")

    recent = (date.today() - timedelta(days=10)).isoformat()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"Transaction": "Purchase", "TransactionDate": recent},
        {"Transaction": "Purchase", "TransactionDate": recent},
    ]
    mock_get.return_value = mock_resp

    from data.watchlist_provider import WatchlistProvider, WatchlistEntry
    provider = WatchlistProvider.__new__(WatchlistProvider)
    from core.config import settings
    provider._cfg = settings.watchlist
    provider._api_key = "fake_key"

    entries = [WatchlistEntry(symbol="AMD", price=120.0, iv_proxy=40.0, options_volume=1000)]
    result = provider._enrich_quiverquant(entries)
    assert result[0].quiverquant_score == 20.0  # 2 buys × 10


@patch("data.watchlist_provider.httpx.get")
def test_enrich_quiverquant_failure_is_nonfatal(mock_get, monkeypatch):
    """QQ HTTP failure doesn't crash — score stays 0."""
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    mock_get.side_effect = Exception("timeout")

    from data.watchlist_provider import WatchlistProvider, WatchlistEntry
    provider = WatchlistProvider.__new__(WatchlistProvider)
    from core.config import settings
    provider._cfg = settings.watchlist
    provider._api_key = "fake_key"

    entries = [WatchlistEntry(symbol="AMD", price=120.0, iv_proxy=40.0, options_volume=1000)]
    result = provider._enrich_quiverquant(entries)
    assert result[0].quiverquant_score == 0.0
```

- [ ] **Step 2: Run tests — verify they all FAIL (class doesn't exist yet)**

```bash
cd /home/ivan8115/git/tradingBot && \
.venv/bin/pytest tests/unit/test_watchlist_provider.py -v -p no:cacheprovider 2>&1 | head -30
```

Expected: `ImportError: cannot import name 'WatchlistProvider'`

- [ ] **Step 3: Create `data/watchlist_provider.py`**

```python
"""
WatchlistProvider: builds a daily Wheel candidate watchlist from free sources.

Phase 1 sources:
  1. Finviz screener  — price range, has options, options volume filter
  2. QuiverQuant      — optional boost from recent congressional purchases
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import httpx
from finviz.screener import Screener
from loguru import logger
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.config import settings
from database.models import WatchlistCandidate


@dataclass
class WatchlistEntry:
    symbol: str
    price: float
    iv_proxy: float          # Finviz volatility % — rough IV stand-in
    options_volume: int
    quiverquant_score: float = 0.0
    final_score: float = 0.0


class WatchlistProvider:
    """Scans free data sources and returns a scored Wheel candidate list."""

    # Finviz filter codes: has options, price $10-$150
    _FINVIZ_FILTERS = ["sh_opt_option", "sh_price_o10", "sh_price_u150"]

    def __init__(self) -> None:
        self._cfg = settings.watchlist
        self._api_key = settings.quiverquant_api_key
        self._engine = create_engine(f"sqlite:///{settings.system.db_path}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> list[str]:
        """
        Run full scan cycle. Persists results to DB.
        Returns the top symbols (up to max_symbols) sorted by score.
        """
        candidates = self._scan_finviz()
        if not candidates:
            logger.warning("[Watchlist] Finviz scan returned 0 candidates — keeping yesterday's list")
            return self.get_active_symbols()

        if self._api_key and self._cfg.quiverquant_boost:
            candidates = self._enrich_quiverquant(candidates)

        self._score_and_save(candidates)

        top = sorted(candidates, key=lambda x: x.final_score, reverse=True)[: self._cfg.max_symbols]
        symbols = [e.symbol for e in top]
        logger.info(f"[Watchlist] Refreshed — {len(symbols)} candidates: {symbols}")
        return symbols

    def get_active_symbols(self) -> list[str]:
        """Return today's active watchlist from DB (used if refresh was skipped)."""
        today = date.today().isoformat()
        with Session(self._engine) as session:
            rows = (
                session.query(WatchlistCandidate)
                .filter_by(scan_date=today, active=True)
                .order_by(WatchlistCandidate.final_score.desc())
                .limit(self._cfg.max_symbols)
                .all()
            )
        return [r.symbol for r in rows]

    # ------------------------------------------------------------------
    # Internal scan steps
    # ------------------------------------------------------------------

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

                    # Volatility column format: "3.45%" (weekly/monthly)
                    vol_str = stock.get("Volatility", "0") or "0"
                    iv_proxy = float(vol_str.replace("%", "").strip() or "0")

                    # Volume as options activity proxy (Finviz Overview shows stock volume)
                    vol_int = int(str(stock.get("Volume", "0")).replace(",", "") or "0")

                    if not symbol:
                        continue
                    if price < self._cfg.min_price or price > self._cfg.max_price:
                        continue
                    if vol_int < self._cfg.min_options_volume:
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

    def _enrich_quiverquant(self, entries: list[WatchlistEntry]) -> list[WatchlistEntry]:
        """
        Boost score for symbols with recent congressional purchases.
        Uses QuiverQuant free tier (500 calls/day).
        Non-fatal: if QQ fails, entries are returned unchanged.
        """
        for entry in entries:
            try:
                resp = httpx.get(
                    f"https://api.quiverquant.com/beta/historical/congresstrading/{entry.symbol}",
                    headers={"Authorization": f"Token {self._api_key}"},
                    timeout=5.0,
                )
                if resp.status_code != 200:
                    continue
                trades = resp.json()
                recent_buys = sum(
                    1
                    for t in trades
                    if t.get("Transaction") == "Purchase"
                    and _days_ago(t.get("TransactionDate", "")) <= 90
                )
                entry.quiverquant_score = min(recent_buys * 10.0, 50.0)  # cap at 50
            except Exception:
                pass  # QQ failure is never fatal

        return entries

    def _score_and_save(self, entries: list[WatchlistEntry]) -> None:
        """Compute final composite score and persist to DB."""
        max_vol = max((e.options_volume for e in entries), default=1)

        for entry in entries:
            vol_norm = (entry.options_volume / max_vol) * 30.0
            # 60% IV proxy + 30% volume + 10% congressional signal
            entry.final_score = (
                (entry.iv_proxy * 0.6)
                + vol_norm
                + (entry.quiverquant_score * 0.1)
            )

        today = date.today().isoformat()
        with Session(self._engine) as session:
            # Deactivate all previous candidates
            session.query(WatchlistCandidate).filter(
                WatchlistCandidate.scan_date != today
            ).update({"active": False})

            for entry in entries:
                candidate = WatchlistCandidate(
                    symbol=entry.symbol,
                    scan_date=today,
                    price=entry.price,
                    iv_proxy=entry.iv_proxy,
                    options_volume=entry.options_volume,
                    quiverquant_score=entry.quiverquant_score,
                    final_score=entry.final_score,
                    source="finviz",
                    active=True,
                    added_at=datetime.utcnow(),
                )
                session.add(candidate)

            session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _days_ago(date_str: str) -> int:
    """Return how many calendar days ago a YYYY-MM-DD string was. Returns 999 on error."""
    try:
        d = date.fromisoformat(date_str[:10])
        return (date.today() - d).days
    except Exception:
        return 999
```

- [ ] **Step 4: Run tests — verify they all PASS**

```bash
cd /home/ivan8115/git/tradingBot && \
.venv/bin/pytest tests/unit/test_watchlist_provider.py -v -p no:cacheprovider
```

Expected: all tests green.

- [ ] **Step 5: Commit**

```bash
git add data/watchlist_provider.py tests/unit/test_watchlist_provider.py
git commit -m "feat: add WatchlistProvider with Finviz scan and QuiverQuant enrichment"
```

---

## Task 5: WheelStrategy — dynamic symbol sync

**Files:**
- Modify: `strategies/wheel/wheel_strategy.py`

The WheelStrategy currently has a fixed `symbols` list set at init. We need it to accept new symbols mid-run without disrupting open positions.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_wheel_strategy.py` (or create if missing):

```python
def test_sync_symbols_adds_new_and_keeps_open():
    """sync_symbols adds new candidates; symbols with open positions are never removed."""
    from strategies.wheel.wheel_strategy import WheelStrategy, WheelState
    strategy = WheelStrategy(symbols=["AMD"])
    # Simulate AMD in open CSP state
    strategy._positions["AMD"].state = WheelState.CSP_OPEN

    strategy.sync_symbols(["MARA", "TSLA"])  # AMD not in new list but is open

    assert "MARA" in strategy.symbols
    assert "TSLA" in strategy.symbols
    assert "AMD" in strategy.symbols          # kept because position is open


def test_sync_symbols_removes_scanning_symbols():
    """Symbols in SCANNING state that are no longer in the watchlist are removed."""
    from strategies.wheel.wheel_strategy import WheelStrategy, WheelState
    strategy = WheelStrategy(symbols=["AMD", "MARA"])
    # AMD is in SCANNING (default), MARA is in SCANNING
    strategy.sync_symbols(["TSLA"])  # only TSLA in new list

    assert "TSLA" in strategy.symbols
    assert "AMD" not in strategy.symbols
    assert "MARA" not in strategy.symbols
```

- [ ] **Step 2: Run the test — verify FAIL**

```bash
.venv/bin/pytest tests/unit/test_wheel_strategy.py -k "test_sync_symbols" -v -p no:cacheprovider
```

Expected: `AttributeError: 'WheelStrategy' object has no attribute 'sync_symbols'`

- [ ] **Step 3: Add `sync_symbols()` to `WheelStrategy`**

In `strategies/wheel/wheel_strategy.py`, add after `update_options_chain()` (around line 336):

```python
def sync_symbols(self, new_symbols: list[str]) -> None:
    """
    Update the active symbol list from a fresh watchlist scan.

    Rules:
    - New symbols are added (SCANNING state, no position).
    - Symbols in SCANNING state that are not in new_symbols are removed.
    - Symbols with open positions (CSP_OPEN, ASSIGNED, CC_OPEN) are NEVER removed.
    """
    # Add new symbols not yet tracked
    for sym in new_symbols:
        if sym not in self._positions:
            self._positions[sym] = WheelPosition(symbol=sym)
            logger.info(f"[Wheel] Added new symbol from watchlist: {sym}")

    # Remove SCANNING symbols no longer in the watchlist
    to_remove = [
        sym
        for sym, pos in self._positions.items()
        if sym not in new_symbols and pos.state == WheelState.SCANNING
    ]
    for sym in to_remove:
        del self._positions[sym]
        logger.info(f"[Wheel] Removed inactive symbol from watchlist: {sym}")

    # Keep symbols property in sync
    self._symbols = list(self._positions.keys())
```

Also update the `symbols` property — check `strategies/base.py` to see how `self._symbols` is set and make sure `sync_symbols` correctly updates it. The base class likely has:

```python
@property
def symbols(self) -> list[str]:
    return self._symbols
```

If `__init__` sets `self._symbols = symbols`, the `sync_symbols` method above handles keeping it current.

- [ ] **Step 4: Run tests — verify PASS**

```bash
.venv/bin/pytest tests/unit/test_wheel_strategy.py -k "test_sync_symbols" -v -p no:cacheprovider
```

- [ ] **Step 5: Run full test suite — verify no regressions**

```bash
.venv/bin/pytest tests/ -v -p no:cacheprovider 2>&1 | tail -20
```

Expected: all 56 existing tests + 2 new ones pass.

- [ ] **Step 6: Commit**

```bash
git add strategies/wheel/wheel_strategy.py tests/unit/test_wheel_strategy.py
git commit -m "feat: add WheelStrategy.sync_symbols() for dynamic watchlist support"
```

---

## Task 6: Scheduler — pre-market watchlist refresh

**Files:**
- Modify: `scheduler/scheduler.py`

- [ ] **Step 1: Add WatchlistProvider to scheduler**

In `scheduler/scheduler.py`, add import near the top with other imports:

```python
from data.watchlist_provider import WatchlistProvider
```

In `TradingScheduler.__init__()`, after `self._fetcher = HistoricalDataFetcher()`:

```python
self._watchlist = WatchlistProvider()
```

- [ ] **Step 2: Add watchlist refresh job in `setup()`**

In `TradingScheduler.setup()`, after the pre_market job registration:

```python
# Watchlist refresh (pre-market, before trading starts)
self._scheduler.add_job(
    self._refresh_watchlist,
    CronTrigger(
        hour=settings.watchlist.refresh_hour,
        minute=settings.watchlist.refresh_minute,
        day_of_week="mon-fri",
        timezone=tz,
    ),
    id="watchlist_refresh",
)
```

- [ ] **Step 3: Add `_refresh_watchlist` job method**

Add after `_pre_market()` in the scheduled jobs section:

```python
async def _refresh_watchlist(self) -> None:
    """Scan Finviz + QuiverQuant for today's Wheel candidates and sync strategies."""
    if not self._is_trading_day():
        return
    logger.info("=== WATCHLIST REFRESH ===")
    try:
        symbols = await asyncio.get_event_loop().run_in_executor(
            None, self._watchlist.refresh
        )
        if not symbols:
            logger.warning("[Watchlist] Refresh returned empty list — no symbol change")
            return

        for wheel in self._wheel_strategies:
            wheel.sync_symbols(symbols)

        # Rebuild active symbol set for WebSocket subscriptions
        self._active_symbols = list(
            {sym for s in self._strategies for sym in s.symbols}
        )
        logger.info(f"[Watchlist] Active symbols updated: {self._active_symbols}")
    except Exception as exc:
        logger.error(f"[Watchlist] Refresh failed: {exc}")
```

Note: `run_in_executor` is used because Finviz and httpx calls are synchronous — this keeps the async event loop unblocked.

- [ ] **Step 4: Also call refresh at startup in `_pre_market`**

In `_pre_market()`, after `self._risk.set_daily_start_value(self._portfolio)`:

```python
# Trigger watchlist refresh if it hasn't run yet today
await self._refresh_watchlist()
```

This ensures the watchlist is populated even if the scheduler just started mid-morning.

- [ ] **Step 5: Verify no import errors**

```bash
cd /home/ivan8115/git/tradingBot && \
.venv/bin/python -c "from scheduler.scheduler import TradingScheduler; print('OK')"
```

Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add scheduler/scheduler.py
git commit -m "feat: add pre-market watchlist refresh to scheduler"
```

---

## Task 7: Dashboard — watchlist endpoint

**Files:**
- Modify: `dashboard/app.py`

- [ ] **Step 1: Add import for WatchlistCandidate**

In `dashboard/app.py`, add to the existing database imports:

```python
from database.models import Signal, Trade, WatchlistCandidate
```

- [ ] **Step 2: Add `GET /api/watchlist` endpoint**

Add after the existing `/api/strategy-state` endpoint:

```python
@app.get("/api/watchlist")
async def get_watchlist():
    """Return today's Wheel candidates from the automated screener scan."""
    from datetime import date
    today = date.today().isoformat()
    Session = get_session_factory(DB_PATH)
    with Session() as session:
        rows = (
            session.query(WatchlistCandidate)
            .filter_by(scan_date=today, active=True)
            .order_by(WatchlistCandidate.final_score.desc())
            .all()
        )
    return [
        {
            "symbol": r.symbol,
            "price": r.price,
            "iv_proxy": r.iv_proxy,
            "options_volume": r.options_volume,
            "quiverquant_score": r.quiverquant_score,
            "final_score": round(r.final_score, 2),
            "scan_date": r.scan_date,
        }
        for r in rows
    ]
```

- [ ] **Step 3: Verify endpoint appears in OpenAPI docs**

```bash
cd /home/ivan8115/git/tradingBot && \
.venv/bin/python -c "from dashboard.app import app; routes = [r.path for r in app.routes]; print(routes)"
```

Expected: list includes `'/api/watchlist'`

- [ ] **Step 4: Commit**

```bash
git add dashboard/app.py
git commit -m "feat: add /api/watchlist endpoint to dashboard"
```

---

## Task 8: End-to-end smoke test

**Goal:** Confirm the full chain works — provider scans → saves to DB → scheduler would call sync → endpoint returns data.

- [ ] **Step 1: Run a manual watchlist refresh (paper mode, no money at risk)**

```bash
cd /home/ivan8115/git/tradingBot && \
.venv/bin/python -c "
from database.migrations import init_db
from core.config import settings
init_db(settings.system.db_path)

from data.watchlist_provider import WatchlistProvider
p = WatchlistProvider()
symbols = p.refresh()
print('Top candidates:', symbols)
"
```

Expected: prints a list of 10-15 stock tickers. If Finviz is rate-limited, wait 30 seconds and retry.

- [ ] **Step 2: Confirm DB was written**

```bash
.venv/bin/python -c "
import sqlite3
from core.config import settings
conn = sqlite3.connect(settings.system.db_path)
rows = conn.execute('SELECT symbol, price, final_score FROM watchlist_candidates ORDER BY final_score DESC LIMIT 10').fetchall()
for r in rows: print(r)
conn.close()
"
```

Expected: rows printed showing symbols, prices, scores.

- [ ] **Step 3: Start the dashboard and hit the endpoint**

```bash
# Terminal 1:
cd /home/ivan8115/git/tradingBot && \
.venv/bin/python -m dashboard.app &
sleep 3
curl -s http://localhost:8000/api/watchlist | python3 -m json.tool | head -40
kill %1
```

Expected: JSON array of watchlist candidates.

- [ ] **Step 4: Run full test suite one final time**

```bash
.venv/bin/pytest tests/ -v -p no:cacheprovider 2>&1 | tail -10
```

Expected: all tests pass.

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "feat: dynamic watchlist Phase 1 complete — Finviz scan + QuiverQuant enrichment"
```

---

## Phase 2 Gate Criteria

Do not start Phase 2 subscriptions until all of these are true:

- [ ] Paper trading has run for **≥30 calendar days**
- [ ] Wheel strategy has completed **≥3 full cycles** (CSP → assigned → CC → called away)
- [ ] Monthly premium collected is **≥$200** (covers ~4 months of Unusual Whales sub)
- [ ] Watchlist candidates are showing up in fills (i.e., the screener is surfacing real trades, not just AMD/MARA)

When criteria are met, next services to evaluate (in order):
1. **Unusual Whales** (~$50/mo) — adds real-time options flow as a signal layer
2. **QuiverQuant Pro** (~$25/mo) — removes 500 call/day limit, adds sector/ETF flow data
3. **Finviz Elite** (~$25/mo) — real IV rank filter instead of volatility proxy, intraday scans
