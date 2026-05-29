# tests/unit/test_decision_log.py
import json
from pathlib import Path
from unittest.mock import patch

def test_writes_jsonl_record(tmp_path):
    log_dir = tmp_path / "decisions"
    with patch("core.decision_log.LOG_DIR", log_dir):
        import importlib
        import core.decision_log as dl
        importlib.reload(dl)
        dl.log_decision({"session_id": "abc123", "stage": "risk_manager", "symbol": "AMD"})
    files = list(log_dir.glob("*.jsonl"))
    assert len(files) == 1
    record = json.loads(files[0].read_text().strip())
    assert record["session_id"] == "abc123"
    assert record["stage"] == "risk_manager"
    assert "timestamp" in record

def test_appends_multiple_records(tmp_path):
    log_dir = tmp_path / "decisions"
    with patch("core.decision_log.LOG_DIR", log_dir):
        import importlib
        import core.decision_log as dl
        importlib.reload(dl)
        dl.log_decision({"session_id": "x1", "stage": "a"})
        dl.log_decision({"session_id": "x1", "stage": "b"})
    files = list(log_dir.glob("*.jsonl"))
    lines = [l for l in files[0].read_text().strip().split("\n") if l.strip()]
    assert len(lines) == 2
