"""
Wheel Strategy — state machine per symbol.

States:
  SCANNING  → looking for entry (high IV, neutral-bullish trend)
  CSP_OPEN  → short put is open, managing to profit target or stop
  ASSIGNED  → we own the stock (put was assigned), initiating CC
  CC_OPEN   → short call is open, managing to profit target or expiry

State transitions are driven by FillEvents (not BarEvents) for exactness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from loguru import logger

from analysis.greeks import iv_rank
from analysis.indicators import TechnicalIndicators
from core.config import WheelStrategyConfig, settings
from core.events import BarEvent, FillEvent, SignalEvent
from strategies.base import Strategy
from strategies.wheel.covered_call_leg import CCPosition, CoveredCallLeg
from strategies.wheel.csp_leg import CSPPosition, CashSecuredPutLeg, OptionContract


class WheelState(str, Enum):
    SCANNING = "scanning"
    CSP_OPEN = "csp_open"
    ASSIGNED = "assigned"
    CC_OPEN = "cc_open"


@dataclass
class WheelPosition:
    """Full state of one Wheel cycle for a single symbol."""
    symbol: str
    state: WheelState = WheelState.SCANNING
    csp_position: Optional[CSPPosition] = None
    cc_position: Optional[CCPosition] = None
    stock_quantity: int = 0
    stock_cost_basis: Optional[Decimal] = None
    total_premium_collected: Decimal = Decimal("0")
    cycle_start: Optional[datetime] = None
    # Cached options chain (refreshed periodically)
    cached_chain: list[OptionContract] = field(default_factory=list)
    iv_history: list[float] = field(default_factory=list)
    # AI pre-selected strike — set by scheduler before on_bar(), consumed once
    ai_preferred_contract_id: Optional[str] = None


class WheelStrategy(Strategy):
    """
    The Wheel Strategy: sell CSP → get assigned → sell CC → repeat.
    Runs an independent state machine per symbol.
    """

    strategy_id = "wheel"

    def __init__(
        self,
        symbols: list[str],
        config: WheelStrategyConfig | None = None,
        advisor=None,
    ) -> None:
        super().__init__(symbols)
        cfg = config or settings.strategies.wheel
        self._cfg = cfg
        self._csp_leg = CashSecuredPutLeg(cfg.csp)
        self._cc_leg = CoveredCallLeg(cfg.cc)
        self._advisor = advisor

        # One WheelPosition per symbol
        self._positions: dict[str, WheelPosition] = {
            sym: WheelPosition(symbol=sym) for sym in symbols
        }

    def on_bar(self, bar: BarEvent) -> list[SignalEvent]:
        if bar.symbol not in self.symbols:
            return []

        snap = self._update_indicators(bar)
        pos = self._positions[bar.symbol]

        match pos.state:
            case WheelState.SCANNING:
                return self._evaluate_entry(bar, pos, snap)
            case WheelState.CSP_OPEN:
                return self._manage_csp(bar, pos)
            case WheelState.ASSIGNED:
                return self._initiate_cc(bar, pos)
            case WheelState.CC_OPEN:
                return self._manage_cc(bar, pos)

        return []

    def on_fill(self, fill: FillEvent) -> None:
        """State transitions triggered by fills — not by bar events."""
        if fill.strategy_id != self.strategy_id:
            return

        sym = fill.symbol
        pos = self._positions.get(sym)
        if not pos:
            return

        leg = fill.metadata.get("leg")

        if leg == "csp_open" and fill.side == "sell":
            # CSP was sold — move to CSP_OPEN
            pos.state = WheelState.CSP_OPEN
            pos.total_premium_collected += fill.fill_price
            pos.cycle_start = fill.filled_at
            logger.info(f"[Wheel] {sym}: CSP opened @ ${fill.fill_price} premium")

        elif leg == "csp_close" and fill.side == "buy":
            # CSP bought back — profit taken or stop hit
            pnl = float(pos.csp_position.premium_received - fill.fill_price) * 100 if pos.csp_position else 0
            logger.info(f"[Wheel] {sym}: CSP closed | P&L ≈ ${pnl:+.2f}")
            pos.csp_position = None
            if fill.metadata.get("assigned"):
                pos.state = WheelState.ASSIGNED
                pos.stock_cost_basis = Decimal(str(fill.metadata.get("cost_basis", fill.fill_price)))
                pos.stock_quantity = int(fill.metadata.get("quantity", 100))
            else:
                pos.state = WheelState.SCANNING

        elif leg == "assignment":
            pos.state = WheelState.ASSIGNED
            pos.stock_quantity = fill.filled_qty
            pos.stock_cost_basis = self._csp_leg.cost_basis_after_assignment(pos.csp_position) \
                if pos.csp_position else fill.fill_price
            logger.info(
                f"[Wheel] {sym}: ASSIGNED {fill.filled_qty} shares @ "
                f"cost_basis=${pos.stock_cost_basis}"
            )
            pos.csp_position = None

        elif leg == "cc_open" and fill.side == "sell":
            pos.state = WheelState.CC_OPEN
            pos.total_premium_collected += fill.fill_price
            logger.info(f"[Wheel] {sym}: CC opened @ ${fill.fill_price} premium")

        elif leg == "cc_close":
            called_away = fill.metadata.get("called_away", False)
            if called_away:
                stock_pnl = (pos.cc_position.contract.strike - pos.stock_cost_basis) \
                    * pos.stock_quantity if pos.cc_position and pos.stock_cost_basis else Decimal("0")
                logger.info(
                    f"[Wheel] {sym}: Shares CALLED AWAY | "
                    f"Stock P&L=${stock_pnl:.2f} | "
                    f"Total premium=${pos.total_premium_collected:.2f}"
                )
                pos.stock_quantity = 0
                pos.stock_cost_basis = None
            pos.cc_position = None
            pos.state = WheelState.SCANNING
            logger.info(f"[Wheel] {sym}: Wheel cycle complete → SCANNING")

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    def _evaluate_entry(
        self,
        bar: BarEvent,
        pos: WheelPosition,
        snap,
    ) -> list[SignalEvent]:
        """
        SCANNING state: look for a valid CSP entry.
        We need the options chain from pos.cached_chain (refreshed externally).
        """
        if not pos.cached_chain:
            # No chain available yet — wait for scheduler to populate
            return []

        if not self._bars_available(bar.symbol, 50):
            return []

        # Update IV history (using ATR as rough IV proxy if no real IV available)
        current_iv = self._estimate_iv(snap, bar)
        if current_iv > 0:
            pos.iv_history.append(current_iv)
            if len(pos.iv_history) > 252:
                pos.iv_history = pos.iv_history[-252:]

        iv_rank_val = iv_rank(current_iv, pos.iv_history) if len(pos.iv_history) > 10 else 0.0

        # Check IV Rank threshold
        if iv_rank_val < self._cfg.csp.min_iv_rank:
            return []

        # Trend must be neutral or bullish
        trend = self._get_trend(bar.symbol)
        if trend == "downtrend":
            return []

        # Select strike — use AI pre-selection if available, else fall back to mechanical
        ai_strike_reasoning = ""
        contract = None
        if pos.ai_preferred_contract_id:
            contract = next(
                (c for c in pos.cached_chain if c.contract_id == pos.ai_preferred_contract_id),
                None,
            )
            if contract:
                ai_strike_reasoning = "AI-selected strike"
            pos.ai_preferred_contract_id = None  # consume — one-shot

        if contract is None:
            contract = self._csp_leg.select_strike(
                chain=pos.cached_chain,
                underlying_price=float(bar.close),
            )

        if not contract:
            return []

        # Log opportunity
        logger.info(
            f"[Wheel] {bar.symbol}: CSP opportunity | "
            f"IV_Rank={iv_rank_val:.0f} | "
            f"Strike={contract.strike} | "
            f"DTE={contract.dte} | "
            f"Delta={contract.delta:.2f} | "
            f"Premium=${contract.mid:.2f}"
            + (f" | {ai_strike_reasoning}" if ai_strike_reasoning else "")
        )

        greeks_delta = contract.delta  # already computed from chain

        return [SignalEvent(
            strategy_id=self.strategy_id,
            symbol=bar.symbol,
            signal_type="SELL_PUT",
            strength=min(1.0, iv_rank_val / 100.0),
            timestamp=bar.timestamp,
            metadata={
                "leg": "csp_open",
                "contract_id": contract.contract_id,
                "strike": float(contract.strike),
                "expiry": str(contract.expiry),
                "dte": contract.dte,
                "delta": greeks_delta,
                "premium": float(contract.mid),
                "iv_rank": iv_rank_val,
                "ai_strike_reasoning": ai_strike_reasoning,
                "collateral": float(contract.strike * 100),  # cash locked to secure this put
            },
        )]

    def _manage_csp(self, bar: BarEvent, pos: WheelPosition) -> list[SignalEvent]:
        """CSP_OPEN state: check if we should close early."""
        if not pos.csp_position:
            # Position data missing — resync
            pos.state = WheelState.SCANNING
            return []

        # We need the current contract price from the chain
        current_price = self._get_contract_price(pos.csp_position.contract.contract_id, pos)
        if current_price is None:
            return []

        should_close, reason = self._csp_leg.should_close_early(pos.csp_position, current_price)
        if should_close:
            logger.info(f"[Wheel] {bar.symbol}: Closing CSP — {reason}")
            return [SignalEvent(
                strategy_id=self.strategy_id,
                symbol=bar.symbol,
                signal_type="BUY_TO_CLOSE_PUT",
                strength=1.0,
                timestamp=bar.timestamp,
                metadata={
                    "leg": "csp_close",
                    "contract_id": pos.csp_position.contract.contract_id,
                    "reason": reason,
                },
            )]
        return []

    def _initiate_cc(self, bar: BarEvent, pos: WheelPosition) -> list[SignalEvent]:
        """ASSIGNED state: immediately sell a Covered Call."""
        if not pos.cached_chain or not pos.stock_cost_basis:
            return []

        contract = self._cc_leg.select_strike(
            chain=pos.cached_chain,
            stock_cost_basis=pos.stock_cost_basis,
            underlying_price=float(bar.close),
        )
        if not contract:
            return []

        logger.info(
            f"[Wheel] {bar.symbol}: Opening CC | "
            f"Strike={contract.strike} | DTE={contract.dte} | "
            f"Delta={contract.delta:.2f} | Premium=${contract.mid:.2f} | "
            f"CostBasis=${pos.stock_cost_basis}"
        )

        return [SignalEvent(
            strategy_id=self.strategy_id,
            symbol=bar.symbol,
            signal_type="SELL_CALL",
            strength=0.9,
            timestamp=bar.timestamp,
            metadata={
                "leg": "cc_open",
                "contract_id": contract.contract_id,
                "strike": float(contract.strike),
                "expiry": str(contract.expiry),
                "dte": contract.dte,
                "delta": contract.delta,
                "premium": float(contract.mid),
                "cost_basis": float(pos.stock_cost_basis),
            },
        )]

    def _manage_cc(self, bar: BarEvent, pos: WheelPosition) -> list[SignalEvent]:
        """CC_OPEN state: check for early close."""
        if not pos.cc_position:
            pos.state = WheelState.SCANNING
            return []

        current_price = self._get_contract_price(pos.cc_position.contract.contract_id, pos)
        if current_price is None:
            return []

        should_close, reason = self._cc_leg.should_close_early(pos.cc_position, current_price)
        if should_close:
            logger.info(f"[Wheel] {bar.symbol}: Closing CC — {reason}")
            return [SignalEvent(
                strategy_id=self.strategy_id,
                symbol=bar.symbol,
                signal_type="BUY_TO_CLOSE_CALL",
                strength=1.0,
                timestamp=bar.timestamp,
                metadata={
                    "leg": "cc_close",
                    "contract_id": pos.cc_position.contract.contract_id,
                    "reason": reason,
                },
            )]
        return []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def update_options_chain(self, symbol: str, chain: list[OptionContract]) -> None:
        """Called by scheduler to refresh the options chain for a symbol."""
        if symbol in self._positions:
            self._positions[symbol].cached_chain = chain

    def sync_symbols(self, new_symbols: list[str]) -> None:
        """
        Update the active symbol list from a fresh watchlist scan.

        Rules:
        - New symbols are added in SCANNING state.
        - SCANNING symbols not in new_symbols are removed.
        - Symbols with open positions (CSP_OPEN, ASSIGNED, CC_OPEN) are never removed.
        """
        from collections import deque
        window_size = next(
            (w.maxlen for w in self._bar_windows.values() if w.maxlen),
            settings.indicators.bar_window_size,
        )

        for sym in new_symbols:
            if sym not in self._positions:
                self._positions[sym] = WheelPosition(symbol=sym)
                self._bar_windows[sym] = deque(maxlen=window_size)
                logger.info(f"[Wheel] Added new symbol from watchlist: {sym}")

        to_remove = [
            sym
            for sym, pos in self._positions.items()
            if sym not in new_symbols and pos.state == WheelState.SCANNING
        ]
        for sym in to_remove:
            del self._positions[sym]
            self._bar_windows.pop(sym, None)
            logger.info(f"[Wheel] Removed idle symbol from watchlist: {sym}")

        self.symbols = list(self._positions.keys())

    def get_state(self) -> dict:
        return {
            sym: {
                "state": pos.state.value,
                "stock_quantity": pos.stock_quantity,
                "stock_cost_basis": str(pos.stock_cost_basis) if pos.stock_cost_basis else None,
                "total_premium": str(pos.total_premium_collected),
            }
            for sym, pos in self._positions.items()
        }

    def load_state(self, state: dict) -> None:
        for sym, data in state.items():
            if sym in self._positions:
                self._positions[sym].state = WheelState(data["state"])
                self._positions[sym].stock_quantity = data.get("stock_quantity", 0)
                cb = data.get("stock_cost_basis")
                self._positions[sym].stock_cost_basis = Decimal(cb) if cb else None
                tp = data.get("total_premium", "0")
                self._positions[sym].total_premium_collected = Decimal(tp)

    def _estimate_iv(self, snap, bar: BarEvent) -> float:
        """
        Rough IV estimate from ATR when real IV isn't available.
        IV ≈ (ATR / Price) × sqrt(252)
        """
        import math
        if not snap or snap.atr != snap.atr:  # NaN check
            return 0.0
        return (snap.atr / float(bar.close)) * math.sqrt(252)

    def _get_trend(self, symbol: str) -> str:
        snap = self._get_snapshot(symbol)
        if snap is None:
            return "unknown"
        if snap.ema_trend_up is True:
            return "uptrend"
        if snap.ema_trend_up is False:
            return "downtrend"
        return "sideways"

    def _get_contract_price(
        self, contract_id: str, pos: WheelPosition
    ) -> Decimal | None:
        """Look up current mid price from the cached chain."""
        for contract in pos.cached_chain:
            if contract.contract_id == contract_id:
                return contract.mid
        return None
