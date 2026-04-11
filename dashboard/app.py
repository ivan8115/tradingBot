"""
Trading Bot Dashboard — FastAPI backend.
Run: python -m dashboard.app
Open: http://localhost:8000
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from broker.client import BrokerClient
from core.config import settings
from database.migrations import get_session_factory
from database.models import Signal, Trade

DB_PATH = settings.system.db_path
STATE_PATH = str(Path(DB_PATH).parent / "strategy_state.json")
MODE = "PAPER" if settings.alpaca_paper else "LIVE"

app = FastAPI(title="TradingBot Dashboard", version="1.0")

_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


def _get_broker() -> BrokerClient:
    return BrokerClient()


@app.get("/")
async def index():
    return FileResponse(str(_static_dir / "index.html"))


@app.get("/api/account")
async def get_account():
    try:
        acct = _get_broker().get_account()
        return {
            "cash": float(acct.cash),
            "equity": float(acct.equity),
            "buying_power": float(acct.buying_power),
            "portfolio_value": float(acct.portfolio_value),
            "mode": MODE,
        }
    except Exception as e:
        logger.warning(f"[Dashboard] /api/account broker error: {e}")
        raise HTTPException(status_code=503, detail="Broker unavailable")


@app.get("/api/positions")
async def get_positions():
    try:
        return [
            {
                "symbol": p.symbol,
                "side": p.side,
                "quantity": p.quantity,
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "market_value": float(p.market_value),
                "unrealized_pnl": float(p.unrealized_pnl),
                "unrealized_pnl_pct": float(p.unrealized_pnl_pct),
            }
            for p in _get_broker().get_positions()
        ]
    except Exception as e:
        logger.warning(f"[Dashboard] /api/positions broker error: {e}")
        raise HTTPException(status_code=503, detail="Broker unavailable")


@app.get("/api/strategy-state")
async def get_strategy_state():
    try:
        path = Path(STATE_PATH)
        if path.exists():
            return json.loads(path.read_text())
    except Exception as e:
        logger.warning(f"Could not read strategy_state.json: {e}")
    return {}


@app.get("/api/trades")
async def get_trades():
    with get_session_factory(DB_PATH)() as session:
        trades = (
            session.query(Trade)
            .order_by(Trade.filled_at.desc())
            .limit(100)
            .all()
        )
        return [
            {
                "order_id": t.order_id,
                "symbol": t.symbol,
                "strategy_id": t.strategy_id,
                "side": t.side,
                "quantity": t.quantity,
                "fill_price": float(t.fill_price),
                "commission": float(t.commission) if t.commission else 0.0,
                "is_options": t.is_options,
                "option_contract_id": t.option_contract_id,
                "filled_at": str(t.filled_at),
            }
            for t in trades
        ]


@app.get("/api/performance")
async def get_performance():
    with get_session_factory(DB_PATH)() as session:
        trades = session.query(Trade).order_by(Trade.filled_at.asc()).all()

    if not trades:
        return {
            "win_rate": 0.0,
            "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "trade_count": 0,
            "equity_curve": [],
        }

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    equity_curve = []

    for t in trades:
        notional = float(t.fill_price) * int(t.quantity) * (100 if t.is_options else 1)
        if t.side == "sell":
            equity += notional
        else:
            equity -= notional
        equity -= float(t.commission) if t.commission else 0.0

        if equity > peak:
            peak = equity
        drawdown = (peak - equity) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, drawdown)
        equity_curve.append({"ts": str(t.filled_at), "equity": round(equity, 2)})

    # Win rate: fraction of equity-curve ticks where cumulative P&L was positive.
    positive_ticks = sum(1 for ec in equity_curve if ec["equity"] > 0)
    win_rate = positive_ticks / len(equity_curve) if equity_curve else 0.0

    try:
        starting_equity = float(settings.system.starting_equity)
    except AttributeError:
        starting_equity = 20000.0

    return {
        "win_rate": round(win_rate, 4),
        "total_return_pct": round(equity / starting_equity, 4) if starting_equity > 0 else 0.0,
        "max_drawdown_pct": round(max_dd, 4),
        "trade_count": len(trades),
        "equity_curve": equity_curve[-200:],
    }


@app.get("/api/alerts")
async def get_alerts():
    with get_session_factory(DB_PATH)() as session:
        rejected = (
            session.query(Signal)
            .filter(Signal.approved == False)  # noqa: E712
            .order_by(Signal.generated_at.desc())
            .limit(50)
            .all()
        )
        return [
            {
                "strategy_id": s.strategy_id,
                "symbol": s.symbol,
                "signal_type": s.signal_type,
                "rejection_reason": s.rejection_reason,
                "generated_at": str(s.generated_at),
            }
            for s in rejected
        ]


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("Dashboard WebSocket client connected")
    try:
        while True:
            try:
                broker = _get_broker()
                acct = broker.get_account()
                positions = broker.get_positions()

                strategy_state: dict = {}
                state_path = Path(STATE_PATH)
                if state_path.exists():
                    try:
                        strategy_state = json.loads(state_path.read_text())
                    except Exception:
                        pass

                await websocket.send_json({
                    "type": "update",
                    "account": {
                        "cash": float(acct.cash),
                        "equity": float(acct.equity),
                        "buying_power": float(acct.buying_power),
                        "mode": MODE,
                    },
                    "positions": [
                        {
                            "symbol": p.symbol,
                            "side": p.side,
                            "quantity": p.quantity,
                            "avg_entry_price": float(p.avg_entry_price),
                            "unrealized_pnl": float(p.unrealized_pnl),
                        }
                        for p in positions
                    ],
                    "strategy_state": strategy_state,
                })
            except Exception as e:
                logger.warning(f"WebSocket update error: {e}")
                await websocket.send_json({"type": "error", "message": str(e)})

            await asyncio.sleep(5)

    except WebSocketDisconnect:
        logger.info("Dashboard WebSocket client disconnected")


if __name__ == "__main__":
    uvicorn.run("dashboard.app:app", host="0.0.0.0", port=8000, reload=False)
