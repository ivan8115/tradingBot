"""
TradingAdvisor — Claude API integration layer.

All public methods return None on failure so callers can always fall back
to the existing mechanical code path. Never raises.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime
from typing import Any, Optional

from loguru import logger
from pydantic import BaseModel

from core.config import settings
from core.decision_log import log_decision
from core.events import SignalEvent


# ---------------------------------------------------------------------------
# Structured response models (also used as tool schemas)
# ---------------------------------------------------------------------------

class SignalEvalResult(BaseModel):
    approved: bool
    adjusted_strength: float    # 0.0–1.0
    confidence: float           # 0.0–1.0
    reasoning: str
    key_factors: list[str]      # top 3 factors


class StrikeSelectionResult(BaseModel):
    contract_id: str
    strike: float
    dte: int
    premium: float
    delta: float
    reasoning: str
    risk_notes: str


class PreMarketBriefing(BaseModel):
    suggested_regime: str       # "bullish" | "neutral" | "bearish"
    focus_symbols: list[str]
    risk_posture: str           # "aggressive" | "normal" | "defensive"
    key_themes: list[str]
    notes: str


class DailyReview(BaseModel):
    grade: str                  # A–F
    summary: str
    wins: list[str]
    losses: list[str]
    lessons: list[str]
    tomorrow_focus: str


class WeeklyReview(BaseModel):
    week_grade: str
    total_premium: float
    win_rate: float
    key_insights: list[str]
    strategy_adjustments: list[str]
    next_week_focus: str


# ---------------------------------------------------------------------------
# Helper: convert a Pydantic model to an Anthropic tool definition
# ---------------------------------------------------------------------------

def _model_to_tool(name: str, description: str, model: type[BaseModel]) -> dict:
    schema = model.model_json_schema()
    schema.pop("title", None)
    return {
        "name": name,
        "description": description,
        "input_schema": schema,
    }


# ---------------------------------------------------------------------------
# TradingAdvisor
# ---------------------------------------------------------------------------

class TradingAdvisor:
    """
    Wraps Claude API calls for each trading decision point.
    Each method is async and returns None if the API is unavailable or times out.
    """

    def __init__(self) -> None:
        self._client = None
        self._enabled = bool(settings.claude_api_key)
        self._cfg = settings.claude
        self._signal_evals_today: int = 0
        self._eval_date: date = date.today()

    # ------------------------------------------------------------------
    # Public async methods
    # ------------------------------------------------------------------

    async def pre_market_briefing(
        self,
        account: dict,
        regime: str,
        active_symbols: list[str],
        open_positions: list[dict],
        research_context: dict | None = None,
        earnings_context: dict | None = None,
        market_themes: list[str] | None = None,
    ) -> Optional[PreMarketBriefing]:
        if not self._enabled:
            return None
        try:
            research_section = ""
            if research_context:
                lines = [f"  {sym}: {summary}" for sym, summary in research_context.items()]
                research_section = "\nPerplexity Research:\n" + "\n".join(lines) + "\n"

            earnings_section = ""
            if earnings_context:
                near = {sym: dt for sym, dt in earnings_context.items() if dt and dt != "unknown"}
                if near:
                    earnings_section = f"\nUpcoming Earnings: {json.dumps(near)}\n"

            themes_section = ""
            if market_themes:
                themes_section = f"\nMacro Themes Today: {', '.join(market_themes)}\n"

            prompt = (
                f"Today is {date.today().isoformat()}. "
                f"Market regime (from EMA filter): {regime}.\n"
                f"Account: equity=${account.get('equity', 0):,.0f}, "
                f"cash=${account.get('cash', 0):,.0f}, "
                f"buying_power=${account.get('buying_power', 0):,.0f}.\n"
                f"Active symbols under watch: {', '.join(active_symbols)}.\n"
                f"Open positions: {json.dumps(open_positions)}."
                f"{themes_section}{earnings_section}{research_section}\n"
                "Assess today's trading environment for the Wheel options and Swing stock strategies. "
                "Identify key themes, suggest which symbols to focus on, and recommend "
                "a risk posture (aggressive/normal/defensive). "
                "Consider current regime, account size, open exposure, upcoming earnings risks, "
                "and any material research findings."
            )
            result = await asyncio.wait_for(
                self._call_structured(
                    model=self._cfg.haiku_model,
                    tool_name="pre_market_briefing",
                    tool_description="Deliver a structured pre-market briefing for the Wheel options trading day",
                    response_model=PreMarketBriefing,
                    user_prompt=prompt,
                    max_tokens=self._cfg.max_tokens_briefing,
                    stage="llm/pre_market_briefing",
                ),
                timeout=self._cfg.briefing_timeout_seconds,
            )
            if result:
                logger.info(
                    f"[AI] Pre-market briefing: regime={result.suggested_regime}, "
                    f"posture={result.risk_posture}, themes={result.key_themes}"
                )
            return result
        except asyncio.TimeoutError:
            logger.warning("[AI] pre_market_briefing timed out")
            return None
        except Exception as e:
            logger.warning(f"[AI] pre_market_briefing failed: {e}")
            return None

    async def evaluate_signal(
        self,
        signal: SignalEvent,
        market_context: dict,
    ) -> Optional[SignalEvalResult]:
        if not self._enabled:
            return None
        self._reset_eval_counter_if_new_day()
        if self._signal_evals_today >= self._cfg.max_signal_evals_per_day:
            logger.debug("[AI] Daily signal eval cap reached — skipping AI check")
            return None
        try:
            prompt = (
                f"Signal to evaluate:\n"
                f"  Strategy: {signal.strategy_id}\n"
                f"  Type: {signal.signal_type}\n"
                f"  Symbol: {signal.symbol}\n"
                f"  Strength: {signal.strength:.2f}\n"
                f"  Metadata: {json.dumps(signal.metadata)}\n\n"
                f"Market context:\n"
                f"  Regime: {market_context.get('regime', 'unknown')}\n"
                f"  Portfolio drawdown: {market_context.get('drawdown_pct', 0)*100:.1f}%\n"
                f"  Daily P&L: {market_context.get('daily_pnl_pct', 0)*100:.1f}%\n"
                f"  Risk posture: {market_context.get('risk_posture', 'normal')}\n"
                f"  Open positions: {json.dumps(market_context.get('open_positions', []))}\n\n"
                "Evaluate whether this trading signal should be approved, and if approved, "
                "adjust the strength score based on conviction. "
                "Consider current risk posture, drawdown, regime, and signal quality."
            )
            result = await asyncio.wait_for(
                self._call_structured(
                    model=self._cfg.sonnet_model,
                    tool_name="evaluate_signal",
                    tool_description="Evaluate a trading signal and decide whether to approve it",
                    response_model=SignalEvalResult,
                    user_prompt=prompt,
                    max_tokens=self._cfg.max_tokens_signal,
                    session_id=signal.metadata.get("session_id") if hasattr(signal, "metadata") else None,
                    symbol=signal.symbol,
                    stage="llm/evaluate_signal",
                ),
                timeout=self._cfg.signal_eval_timeout_seconds,
            )
            if result is not None:
                self._signal_evals_today += 1
                logger.debug(
                    f"[AI] Signal eval: {signal.symbol} {signal.signal_type} "
                    f"→ {'APPROVED' if result.approved else 'REJECTED'} "
                    f"(confidence={result.confidence:.2f})"
                )
            return result
        except asyncio.TimeoutError:
            logger.warning(f"[AI] evaluate_signal timed out for {signal.symbol}")
            return None
        except Exception as e:
            logger.warning(f"[AI] evaluate_signal failed: {e}")
            return None

    async def select_csp_strike(
        self,
        symbol: str,
        underlying_price: float,
        iv_rank: float,
        chain: list[dict],
        account: dict,
    ) -> Optional[StrikeSelectionResult]:
        if not self._enabled:
            return None
        try:
            chain_summary = [
                {
                    "contract_id": c.get("contract_id"),
                    "strike": c.get("strike"),
                    "dte": c.get("dte"),
                    "delta": c.get("delta"),
                    "premium": c.get("mid"),
                    "bid": c.get("bid"),
                    "ask": c.get("ask"),
                    "volume": c.get("volume"),
                    "open_interest": c.get("open_interest"),
                }
                for c in chain[:30]  # cap context size
            ]
            prompt = (
                f"Select the best Cash-Secured Put strike for {symbol}.\n"
                f"Underlying price: ${underlying_price:.2f}\n"
                f"IV Rank: {iv_rank:.0f}\n"
                f"Account equity: ${account.get('equity', 0):,.0f}\n\n"
                f"Available put contracts (sorted by delta proximity to -0.28):\n"
                f"{json.dumps(chain_summary, indent=2)}\n\n"
                "Choose the contract that best balances premium income, risk, and liquidity. "
                "Target delta around -0.28, DTE 21-45 days, adequate bid/ask spread. "
                "The contract_id MUST exactly match one in the list above."
            )
            result = await asyncio.wait_for(
                self._call_structured(
                    model=self._cfg.opus_model,
                    tool_name="select_csp_strike",
                    tool_description="Select the optimal Cash-Secured Put strike from the available chain",
                    response_model=StrikeSelectionResult,
                    user_prompt=prompt,
                    max_tokens=self._cfg.max_tokens_signal,
                    symbol=symbol,
                    stage="llm/select_csp_strike",
                ),
                timeout=self._cfg.signal_eval_timeout_seconds,
            )
            if result is None:
                return None
            # Hallucination guard: verify contract_id exists in chain
            valid_ids = {str(c.get("contract_id")) for c in chain}
            if str(result.contract_id) not in valid_ids:
                logger.warning(
                    f"[AI] select_csp_strike returned unknown contract_id "
                    f"'{result.contract_id}' for {symbol} — falling back to mechanical"
                )
                return None
            logger.info(
                f"[AI] CSP strike selected: {symbol} strike={result.strike} "
                f"DTE={result.dte} delta={result.delta:.2f} premium={result.premium:.2f}"
            )
            return result
        except asyncio.TimeoutError:
            logger.warning(f"[AI] select_csp_strike timed out for {symbol}")
            return None
        except Exception as e:
            logger.warning(f"[AI] select_csp_strike failed: {e}")
            return None

    async def daily_review(
        self,
        trades_today: list[dict],
        signals_today: list[dict],
        eod_summary: dict,
    ) -> Optional[DailyReview]:
        if not self._enabled:
            return None
        try:
            prompt = (
                f"Date: {date.today().isoformat()}\n"
                f"EOD Summary: {json.dumps(eod_summary)}\n"
                f"Trades executed today ({len(trades_today)}): {json.dumps(trades_today)}\n"
                f"Signals generated today ({len(signals_today)}): {json.dumps(signals_today)}\n\n"
                "Review today's trading activity for the Wheel options strategy. "
                "Grade the day A-F, identify wins, losses, and key lessons. "
                "What should we focus on tomorrow?"
            )
            result = await asyncio.wait_for(
                self._call_structured(
                    model=self._cfg.haiku_model,
                    tool_name="daily_review",
                    tool_description="Generate an end-of-day trading review",
                    response_model=DailyReview,
                    user_prompt=prompt,
                    max_tokens=self._cfg.max_tokens_review,
                    stage="llm/daily_review",
                ),
                timeout=self._cfg.briefing_timeout_seconds,
            )
            if result:
                logger.info(f"[AI] Daily review: grade={result.grade}, summary={result.summary[:80]}")
            return result
        except asyncio.TimeoutError:
            logger.warning("[AI] daily_review timed out")
            return None
        except Exception as e:
            logger.warning(f"[AI] daily_review failed: {e}")
            return None

    async def weekly_review(
        self,
        metrics: dict,
        trades_this_week: list[dict],
    ) -> Optional[WeeklyReview]:
        if not self._enabled:
            return None
        try:
            prompt = (
                f"Week ending: {date.today().isoformat()}\n"
                f"Wheel metrics: {json.dumps(metrics)}\n"
                f"Trades this week ({len(trades_this_week)}): {json.dumps(trades_this_week)}\n\n"
                "Review this week's Wheel strategy performance. "
                "Grade A-F, summarize what worked, what didn't, and recommend any "
                "strategy adjustments for next week. "
                "Consider CSP win rate, premium collected, and drawdown."
            )
            result = await asyncio.wait_for(
                self._call_structured(
                    model=self._cfg.haiku_model,
                    tool_name="weekly_review",
                    tool_description="Generate a weekly trading performance review",
                    response_model=WeeklyReview,
                    user_prompt=prompt,
                    max_tokens=self._cfg.max_tokens_review,
                    stage="llm/weekly_review",
                ),
                timeout=self._cfg.briefing_timeout_seconds,
            )
            if result:
                logger.info(
                    f"[AI] Weekly review: grade={result.week_grade}, "
                    f"premium={result.total_premium:.2f}, win_rate={result.win_rate:.2f}"
                )
            return result
        except asyncio.TimeoutError:
            logger.warning("[AI] weekly_review timed out")
            return None
        except Exception as e:
            logger.warning(f"[AI] weekly_review failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_client(self):
        if self._client is None:
            from anthropic import AsyncAnthropic
            self._client = AsyncAnthropic(api_key=settings.claude_api_key)
        return self._client

    def _get_cached_system_prompt(self) -> list[dict]:
        """
        System prompt with prompt caching on the last block.
        Cached after first call → ~90% token savings on repeated intraday invocations.
        """
        cfg = settings.strategies.wheel
        risk = settings.risk
        rules = (
            "You are the AI brain of an autonomous Wheel options trading bot. "
            "Your role is to evaluate signals, select option strikes, and review performance.\n\n"
            "## Wheel Strategy Rules\n"
            "- Trade only Cash-Secured Puts (CSP) and Covered Calls (CC) — no naked options\n"
            f"- CSP target delta: {cfg.csp.target_delta}, DTE {cfg.csp.min_dte}–{cfg.csp.max_dte}\n"
            f"- CC target delta: {cfg.cc.target_delta}, DTE {cfg.cc.min_dte}–{cfg.cc.max_dte}\n"
            f"- Min IV Rank to enter: {cfg.csp.min_iv_rank}\n"
            f"- Profit target: {cfg.csp.profit_target_pct*100:.0f}% of premium\n"
            f"- Stop loss: {cfg.csp.stop_loss_multiplier}x premium paid\n\n"
            "## Risk Rules\n"
            f"- Max single position: {risk.max_single_position_pct*100:.0f}% of portfolio\n"
            f"- Max drawdown halt: {risk.max_drawdown_pct*100:.0f}%\n"
            f"- Daily loss limit: {risk.daily_loss_limit_pct*100:.0f}%\n"
            f"- No new entries when market regime is BEARISH\n\n"
            "## Decision Principles\n"
            "- Prefer high IV rank (>50) with neutral/bullish trend\n"
            "- Prioritize liquid options (tight bid/ask, high OI)\n"
            "- In defensive posture: raise bar for approval, select lower delta\n"
            "- When in doubt, pass (reject) — patience beats forced trades\n"
            "- Always provide specific, actionable reasoning"
        )
        return [
            {
                "type": "text",
                "text": rules,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    async def _call_structured(
        self,
        model: str,
        tool_name: str,
        tool_description: str,
        response_model: type[BaseModel],
        user_prompt: str,
        max_tokens: int,
        *,
        session_id: str | None = None,
        symbol: str | None = None,
        stage: str | None = None,
    ) -> Optional[Any]:
        client = self._build_client()
        tool_def = _model_to_tool(tool_name, tool_description, response_model)

        start = time.monotonic()
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=self._get_cached_system_prompt(),
            tools=[tool_def],
            tool_choice={"type": "any"},
            messages=[{"role": "user", "content": user_prompt}],
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        result = None
        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                result = response_model.model_validate(block.input)
                break

        try:
            log_decision({
                "session_id": session_id,
                "stage": stage or f"llm/{tool_name}",
                "symbol": symbol,
                "model": model,
                "latency_ms": latency_ms,
                "input_tokens": getattr(getattr(response, "usage", None), "input_tokens", None),
                "output_tokens": getattr(getattr(response, "usage", None), "output_tokens", None),
                "prompt_preview": user_prompt[:500],
                "result_preview": str(result)[:500] if result else None,
            })
        except Exception as _log_exc:
            logger.debug(f"[AI] decision log write failed: {_log_exc}")

        if result is None:
            logger.warning(f"[AI] No tool_use block in response for {tool_name}")
        return result

    def _reset_eval_counter_if_new_day(self) -> None:
        today = date.today()
        if today != self._eval_date:
            self._signal_evals_today = 0
            self._eval_date = today


# Module-level singleton
advisor = TradingAdvisor()
