#!/usr/bin/env python3
# scripts/replay_trade.py
"""Print the full decision chain for a given session_id.

Usage:
    python3 scripts/replay_trade.py <session_id>
    python3 scripts/replay_trade.py <session_id> --days 30
"""
import argparse
import json
import sys
from pathlib import Path
from datetime import date, datetime, timedelta, timezone


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a trade decision chain")
    parser.add_argument("session_id", help="Session ID (from signal metadata or decision log)")
    parser.add_argument("--days", type=int, default=30, help="Days of logs to search (default 30)")
    args = parser.parse_args()

    log_dir = Path(__file__).parent.parent / "logs" / "decisions"
    if not log_dir.exists():
        print("No decision logs found at logs/decisions/", file=sys.stderr)
        sys.exit(1)

    # Compute cutoff date string e.g. "2026-05-01" for --days 7 from 2026-05-08
    cutoff_date = (date.today() - timedelta(days=args.days)).isoformat()

    records = []
    for path in sorted(log_dir.glob("*.jsonl")):
        # filename is YYYY-MM-DD.jsonl
        file_date = path.stem  # e.g. "2026-05-28"
        if file_date < cutoff_date:
            continue
        for line in path.open():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("session_id") == args.session_id:
                records.append(rec)

    if not records:
        print(f"No records found for session_id={args.session_id!r} (searched {args.days} days).")
        sys.exit(1)

    records.sort(key=lambda r: r.get("timestamp", ""))
    print(f"\n=== Decision chain for {args.session_id} ({len(records)} events) ===\n")
    for rec in records:
        ts = rec.get("timestamp", "unknown")
        stage = rec.get("stage", "unknown")
        print(f"[{ts}] {stage}")
        for k, v in rec.items():
            if k in ("timestamp", "stage", "session_id"):
                continue
            print(f"  {k}: {v}")
        print()


if __name__ == "__main__":
    main()
