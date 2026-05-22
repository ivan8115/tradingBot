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

    # Finviz filter codes: has options, price $10-$50 (fits $5K account)
    _FINVIZ_FILTERS = ["sh_opt_option", "sh_price_o10", "sh_price_u50"]

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

                    vol_str = stock.get("Volatility", "0") or "0"
                    iv_proxy = float(vol_str.replace("%", "").strip() or "0")

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
                entry.quiverquant_score = min(recent_buys * 10.0, 50.0)
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
