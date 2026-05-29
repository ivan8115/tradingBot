"""
MarketResearcher — Claude-backed market research.

Uses Claude Haiku for fast, cheap pre-market research calls.
All methods return empty results on failure — never raises, always fails gracefully.

Note: Claude has no real-time web access. Earnings dates and news are based on
training knowledge and may be a quarter stale for less-covered stocks. The
EarningsCalendar fails open (returns False when uncertain), so this is safe.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from loguru import logger

from core.config import settings


class MarketResearcher:
    """
    Wraps Claude Haiku calls for pre-market research and midday thesis checks.
    Same interface as the previous Perplexity-backed version.
    """

    def __init__(self) -> None:
        self._enabled = bool(
            getattr(settings, "claude_api_key", "") and
            getattr(settings, "perplexity", None) and
            settings.perplexity.enabled
        )
        self._cfg = getattr(settings, "perplexity", None)
        self._client = None

    # ------------------------------------------------------------------
    # Public async methods
    # ------------------------------------------------------------------

    async def research_symbols(self, symbols: list[str]) -> dict[str, str]:
        """
        Returns {symbol: research_summary} for each symbol.
        Summaries cover recent news, catalysts, and near-term risks.
        Batches large symbol lists to stay within context limits.
        """
        if not self._enabled or not symbols:
            return {}
        results: dict[str, str] = {}
        batch_size = self._cfg.max_symbols_per_call if self._cfg else 10
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            batch_results = await self._research_batch(batch)
            results.update(batch_results)
        return results

    async def check_earnings_dates(self, symbols: list[str]) -> dict[str, str]:
        """
        Returns {symbol: "YYYY-MM-DD" | "unknown"} for each symbol.
        Uses Claude's training knowledge for next scheduled earnings date.
        May be a quarter stale for smaller stocks — EarningsCalendar fails open.
        """
        if not self._enabled or not symbols:
            return {}
        sym_list = ", ".join(symbols)
        prompt = (
            f"For each of these stock ticker symbols, provide the EXACT date of the next "
            f"scheduled quarterly earnings announcement: {sym_list}. "
            "Return ONLY a JSON object with ticker symbols as keys and dates in YYYY-MM-DD format "
            "as values. If a date is unknown or not yet announced, use the string \"unknown\". "
            "Example: {\"AAPL\": \"2026-07-29\", \"MSFT\": \"unknown\"}"
        )
        try:
            raw = await asyncio.wait_for(
                self._call(prompt),
                timeout=self._cfg.timeout_seconds if self._cfg else 15,
            )
            return self._parse_json_dict(raw, symbols, default="unknown")
        except asyncio.TimeoutError:
            logger.warning("[Researcher] check_earnings_dates timed out")
            return {}
        except Exception as e:
            logger.warning(f"[Researcher] check_earnings_dates failed: {e}")
            return {}

    async def check_thesis(self, symbol: str, position_context: dict) -> str:
        """
        Midday thesis check: is the position's original thesis still valid?
        Returns a 2–3 sentence assessment.
        """
        if not self._enabled:
            return ""
        state = position_context.get("state", "unknown")
        entry_price = position_context.get("entry_price", "unknown")
        prompt = (
            f"I hold a position in {symbol} (state: {state}, avg entry: {entry_price}). "
            "Based on your knowledge of this company and typical market dynamics, "
            "is the original bullish thesis likely still intact? "
            "Reply in 2-3 sentences. Flag immediately if there is any well-known material "
            "risk such as structural earnings decline, regulatory headwinds, or sector rotation."
        )
        try:
            return await asyncio.wait_for(
                self._call(prompt),
                timeout=self._cfg.timeout_seconds if self._cfg else 15,
            )
        except asyncio.TimeoutError:
            logger.warning(f"[Researcher] check_thesis timed out for {symbol}")
            return ""
        except Exception as e:
            logger.warning(f"[Researcher] check_thesis failed for {symbol}: {e}")
            return ""

    async def get_market_themes(self) -> list[str]:
        """
        Returns 3–5 macro themes likely driving the market based on current context.
        Note: themes are based on Claude's training knowledge, not live news.
        """
        if not self._enabled:
            return []
        prompt = (
            "What are 3-5 important macro themes currently driving the US stock market? "
            "Return ONLY a JSON array of short theme descriptions (under 15 words each). "
            'Example: ["Fed rate trajectory uncertain", "AI infrastructure spending surge"]'
        )
        try:
            raw = await asyncio.wait_for(
                self._call(prompt),
                timeout=self._cfg.timeout_seconds if self._cfg else 15,
            )
            return self._parse_json_list(raw)
        except asyncio.TimeoutError:
            logger.warning("[Researcher] get_market_themes timed out")
            return []
        except Exception as e:
            logger.warning(f"[Researcher] get_market_themes failed: {e}")
            return []

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _research_batch(self, symbols: list[str]) -> dict[str, str]:
        sym_list = ", ".join(symbols)
        prompt = (
            f"For each of these stock ticker symbols: {sym_list}. "
            "Provide a brief research summary (2-3 sentences each) covering: "
            "1) Most relevant recent catalysts or business developments, "
            "2) Key near-term risk factors. "
            "Return as a JSON object with ticker symbols as keys and summaries as string values. "
            'Example: {"AAPL": "Apple beat earnings last quarter...", "MSFT": "..."}'
        )
        try:
            raw = await asyncio.wait_for(
                self._call(prompt),
                timeout=self._cfg.timeout_seconds if self._cfg else 15,
            )
            result = self._parse_json_dict(raw, symbols, default="No data available.")
            for sym in symbols:
                if sym in result:
                    logger.debug(f"[Researcher] {sym}: {result[sym][:80]}...")
            return result
        except asyncio.TimeoutError:
            logger.warning(f"[Researcher] _research_batch timed out for {symbols}")
            return {}
        except Exception as e:
            logger.warning(f"[Researcher] _research_batch failed: {e}")
            return {}

    def _build_client(self):
        if self._client is None:
            from anthropic import AsyncAnthropic
            self._client = AsyncAnthropic(api_key=settings.claude_api_key)
        return self._client

    async def _call(self, prompt: str) -> str:
        """Call Claude Haiku and return the text response."""
        client = self._build_client()
        model = settings.claude.haiku_model if settings.claude else "claude-haiku-4-5-20251001"
        response = await client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text

    @staticmethod
    def _parse_json_dict(
        raw: str,
        expected_keys: list[str],
        default: Any = "",
    ) -> dict[str, Any]:
        """Extract JSON object from raw text, filling missing keys with default."""
        try:
            clean = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
            match = re.search(r"\{.*\}", clean, re.DOTALL)
            if match:
                parsed = json.loads(match.group())
                result = {}
                for key in expected_keys:
                    result[key] = parsed.get(key, default)
                return result
        except Exception:
            pass
        return {k: default for k in expected_keys}

    @staticmethod
    def _parse_json_list(raw: str) -> list[str]:
        """Extract JSON array from raw text."""
        try:
            clean = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
            match = re.search(r"\[.*\]", clean, re.DOTALL)
            if match:
                parsed = json.loads(match.group())
                return [str(item) for item in parsed if isinstance(item, (str, int, float))]
        except Exception:
            pass
        return []


# Module-level singleton
researcher = MarketResearcher()
