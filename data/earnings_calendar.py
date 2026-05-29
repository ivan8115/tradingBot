"""
EarningsCalendar — day-level cache of upcoming earnings dates.

Backed by Perplexity on first call, then cached until EOD.
Fails OPEN (returns False when unknown) — never blocks a trade on uncertainty.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from loguru import logger


class EarningsCalendar:
    """
    Thin wrapper around researcher.check_earnings_dates() with day-level caching.
    Cache is keyed by (symbol, today's date) and auto-expires the next day.
    """

    def __init__(self) -> None:
        self._cache: dict[str, str | None] = {}  # symbol → "YYYY-MM-DD" | None
        self._cache_date: date | None = None

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def is_near_earnings(self, symbol: str, min_days: int = 21) -> bool:
        """
        Returns True if the symbol has earnings within min_days.
        Fails OPEN (returns False) if date unknown or symbol not in cache.
        """
        self._evict_if_stale()
        raw = self._cache.get(symbol)
        if not raw or raw == "unknown":
            return False
        try:
            earnings_date = datetime.strptime(raw, "%Y-%m-%d").date()
            days_until = (earnings_date - date.today()).days
            return 0 <= days_until <= min_days
        except ValueError:
            return False

    async def prefetch(self, symbols: list[str]) -> None:
        """
        Populate cache for all symbols. Call once in pre-market.
        Skips symbols already cached for today.
        """
        self._evict_if_stale()
        missing = [s for s in symbols if s not in self._cache]
        if not missing:
            return

        from ai.researcher import researcher

        if not researcher._enabled:
            logger.debug("[EarningsCalendar] Researcher disabled — skipping prefetch")
            for sym in missing:
                self._cache[sym] = None
            self._cache_date = date.today()
            return

        logger.info(f"[EarningsCalendar] Fetching earnings dates for {missing}")
        try:
            results = await researcher.check_earnings_dates(missing)
            for sym in missing:
                self._cache[sym] = results.get(sym)
            self._cache_date = date.today()
            logger.info(f"[EarningsCalendar] Cached {len(results)} earnings dates")
        except Exception as e:
            logger.warning(f"[EarningsCalendar] prefetch failed: {e}")
            for sym in missing:
                self._cache[sym] = None
            self._cache_date = date.today()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _evict_if_stale(self) -> None:
        if self._cache_date is not None and self._cache_date < date.today():
            self._cache.clear()
            self._cache_date = None


# Module-level singleton
earnings_calendar = EarningsCalendar()
