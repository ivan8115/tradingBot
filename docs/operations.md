# Operations Guide

## Starting and stopping the bot

```bash
# Start in background (logs to logs/bot.log)
nohup .venv/bin/python main.py trade --mode paper > logs/bot.log 2>&1 &
echo "PID: $!"

# Stop
kill $(pgrep -f "main.py trade")
```

## Reading logs

### Main log (human-readable)
```bash
tail -f logs/bot.log
```

### Decision log (structured JSONL)
```bash
# View all today's decisions (pretty-printed)
while IFS= read -r line; do echo "$line" | python -m json.tool; echo "---"; done \
  < logs/decisions/$(date +%Y-%m-%d).jsonl
```

## Replaying a trade decision

Find the `session_id` from the decision log, then:
```bash
python scripts/replay_trade.py <session_id>
python scripts/replay_trade.py <session_id> --days 7
```

The session_id is generated per entry evaluation and flows through all 5 pipeline stages.

## Running the weekly LLM value report

```bash
python scripts/llm_value_report.py           # last 7 days
python scripts/llm_value_report.py --days 30 # last 30 days
```

The report shows:
- **Veto rate**: % of mechanical-approved trades that Sonnet rejected
- **Strike agreement rate**: % of times Opus and mechanical picked the same contract

After 30 days of live data, check the report to decide if the LLM layers are earning their keep.

## Handling a gap-down alert

You will receive a Slack/email message like:
> GAP-DOWN ALERT — AMD: down 11.2% from entry $142.50 (now $126.50). Manual review required.

Steps:
1. Log in to Alpaca paper account and check the current AMD price and the open put position
2. Look up the decision log: `grep '"symbol": "AMD"' logs/decisions/$(date +%Y-%m-%d).jsonl`
3. Decide: close the put now (buy-to-close via Alpaca UI), roll to a lower strike, or hold
4. The bot does NOT auto-close on a gap-down alert — this is a manual review gate

## Configuring notifications

In `.env`:
```
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

In `config.yaml`:
```yaml
monitoring:
  slack_alerts: true
  alert_on:
    - gap_down
    - large_loss
    - drawdown_breach
    - fill
    - daily_summary
    - daily_review       # AI daily review grade + summary
    - weekly_review      # AI weekly performance review
    - thesis_warning     # Midday research flagged a negative catalyst
```

## Per-symbol pain thresholds

AMD and MARA use a tighter pain threshold (0.80 instead of default 0.85):

```yaml
strategies:
  wheel:
    symbol_overrides:
      AMD:
        pain_threshold: 0.80
      MARA:
        pain_threshold: 0.80
```

Add any new high-volatility symbols here as needed.
