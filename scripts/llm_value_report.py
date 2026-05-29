#!/usr/bin/env python3
"""Weekly LLM value report: did AI layers add value vs. mechanical baseline?

Usage:
    python scripts/llm_value_report.py          # last 7 days
    python scripts/llm_value_report.py --days 30
"""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    log_dir = Path(__file__).parent.parent / "logs" / "decisions"
    if not log_dir.exists():
        print("No decision logs found at logs/decisions/")
        return

    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
    records = []
    for path in sorted(log_dir.glob("*.jsonl")):
        if path.stem < cutoff:
            continue
        for line in path.open():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    eval_records = [r for r in records if r.get("stage") == "scheduler/sonnet_eval"]
    strike_records = [r for r in records if "select_csp_strike" in r.get("stage", "")]

    total_evals = len(eval_records)
    ai_vetoes = sum(
        1 for r in eval_records
        if not r.get("ai_approved")
        and r.get("shadow_decision", {}).get("approved")
    )
    ai_agrees = sum(
        1 for r in eval_records
        if r.get("ai_approved")
        and r.get("shadow_decision", {}).get("approved")
    )

    total_strikes = len(strike_records)
    same_strike = sum(
        1 for r in strike_records
        if r.get("contract_id") == r.get("shadow_decision", {}).get("contract_id")
        and r.get("contract_id") is not None
    )
    diff_strike = total_strikes - same_strike

    def pct(n, d):
        return f"{n/d:.0%}" if d else "n/a"

    lines = [
        f"# LLM Value Report — Last {args.days} Days",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Signal Evaluation (Sonnet layer)",
        f"- Total signals evaluated: {total_evals}",
        f"- AI agreed with mechanical: {ai_agrees} ({pct(ai_agrees, total_evals)})",
        f"- AI vetoed a mechanical-approved trade: {ai_vetoes} ({pct(ai_vetoes, total_evals)})",
        "",
        "> **TODO:** Cross-reference vetoed symbols against underlying price action",
        "> in the 30 days after the veto to determine if vetoes added value.",
        "",
        "## Strike Selection (Opus vs Mechanical)",
        f"- Total strike selections: {total_strikes}",
        f"- Same contract as mechanical: {same_strike} ({pct(same_strike, total_strikes)})",
        f"- Different from mechanical: {diff_strike} ({pct(diff_strike, total_strikes)})",
        "",
        "> **TODO:** Add realized P&L tracking to compare AI vs mechanical strikes",
        "> after positions close. Available after 30 days of live data.",
    ]

    output = "\n".join(lines)
    print(output)

    out_path = log_dir.parent / f"llm_value_report_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    out_path.write_text(output)
    print(f"\nReport saved to {out_path}")


if __name__ == "__main__":
    main()
