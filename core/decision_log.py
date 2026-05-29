# core/decision_log.py
import json
from datetime import datetime, timezone
from pathlib import Path

# Use a guard so that importlib.reload() does not overwrite a patched LOG_DIR.
# Tests patch this attribute before reloading; the guard preserves the patched value.
if "LOG_DIR" not in dir():
    LOG_DIR = Path(__file__).parent.parent / "logs" / "decisions"

def log_decision(record: dict) -> None:
    """Append one structured record to today's decision log (logs/decisions/YYYY-MM-DD.jsonl)."""
    import core.decision_log as _self
    log_dir: Path = _self.LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = log_dir / f"{today}.jsonl"
    if "timestamp" not in record:
        record = {**record, "timestamp": datetime.now(timezone.utc).isoformat()}
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")
