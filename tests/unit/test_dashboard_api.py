"""Tests for FastAPI dashboard endpoints."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient with mocked broker and temp SQLite DB."""
    db_path = str(tmp_path / "test.db")
    state_path = str(tmp_path / "strategy_state.json")

    from database.migrations import init_db
    init_db(db_path)

    import importlib
    import dashboard.app
    monkeypatch.setattr(dashboard.app, "DB_PATH", db_path)
    monkeypatch.setattr(dashboard.app, "STATE_PATH", state_path)
    importlib.reload(dashboard.app)
    monkeypatch.setattr(dashboard.app, "DB_PATH", db_path)
    monkeypatch.setattr(dashboard.app, "STATE_PATH", state_path)

    mock_broker = MagicMock()
    mock_broker.get_account.return_value = MagicMock(
        cash=Decimal("20000"),
        equity=Decimal("20500"),
        buying_power=Decimal("20000"),
        portfolio_value=Decimal("20500"),
    )
    mock_broker.get_positions.return_value = []
    monkeypatch.setattr(dashboard.app, "_get_broker", lambda: mock_broker)

    from dashboard.app import app
    yield TestClient(app)


def test_account_endpoint_returns_cash(client):
    resp = client.get("/api/account")
    assert resp.status_code == 200
    data = resp.json()
    assert "cash" in data
    assert float(data["cash"]) == pytest.approx(20000.0)
    assert "equity" in data
    assert "mode" in data


def test_positions_endpoint_returns_list(client):
    resp = client.get("/api/positions")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_trades_endpoint_returns_list(client):
    resp = client.get("/api/trades")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_performance_endpoint_returns_stats(client):
    resp = client.get("/api/performance")
    assert resp.status_code == 200
    data = resp.json()
    assert "win_rate" in data
    assert "total_return_pct" in data
    assert "max_drawdown_pct" in data
    assert "trade_count" in data


def test_strategy_state_endpoint_returns_dict(client):
    resp = client.get("/api/strategy-state")
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)


def test_alerts_endpoint_returns_list(client):
    resp = client.get("/api/alerts")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
