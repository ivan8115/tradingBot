# tradingBot

Automated options income bot running the Wheel strategy on Alpaca paper (and eventually live) accounts.

For a plain-English explanation of how it works, see [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md).

---

## Quick Start

```bash
# Install dependencies
pip install -e .

# Copy env template and fill in your keys (never commit .env)
cp .env.example .env

# Run paper trading
python main.py
```

---

## Position Sizing

- Max position size: 20% of account equity per symbol
- Max total deployed collateral: 80% of account (20% buffer for assignment slippage)
- Max open positions: 6 (but the 80% collateral cap is the binding constraint on small accounts)

---

## Exit Rules (CSP)

- Profit target: close at 50% of max premium received
- Soft stop: close if mark ≥ 2.5× credit received AND underlying is below the strike (directional move confirmed — not a pure IV spike)
- Pain threshold: close if underlying drops below strike × 0.85 (default; configurable per symbol via `wheel.symbol_overrides` in config.yaml)
- DTE roll: close when ≤ 7 DTE

---

## Decision Logging

Every trade decision is logged to `logs/decisions/YYYY-MM-DD.jsonl` (one JSON record per event).
Use `python scripts/replay_trade.py <session_id>` to reconstruct any decision chain.

See [docs/decision_flow.md](docs/decision_flow.md) and [docs/operations.md](docs/operations.md)
for the full pipeline description and operational guide.

---

## Docs

| File | Contents |
|---|---|
| [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) | Plain-English strategy overview |
| [docs/decision_flow.md](docs/decision_flow.md) | 5-layer decision pipeline with code pointers |
| [docs/operations.md](docs/operations.md) | Log reading, replay, gap-down handling, notifications |
| [TODO.md](TODO.md) | Deferred issues from the May 2026 code review |

---

## Active Strategies

| Strategy | Status | Reason |
|---|---|---|
| Wheel (options) | Enabled | Primary strategy |
| Momentum (stocks) | Disabled | PDT risk on $10K account |
| Swing (stocks) | Disabled | PDT risk on $10K account |
