# How the Trading Bot Works

## The Strategy: Wheel Options

The bot runs one strategy called the **Wheel**. The idea is simple:
**collect premium income by selling options on stocks you're okay owning.**

You are on the **selling side** — not the buying side. That means you collect cash upfront and hope the option expires worthless.

---

## The Two Phases

### Phase 1 — Sell a Cash-Secured Put (CSP)
- The bot finds a stock (e.g. SOFI at $12) and sells someone the right to force you to buy 100 shares at a set price (e.g. $11 strike)
- You collect premium upfront (~$50–150)
- Alpaca locks up $1,100 as collateral (100 shares × $11) — you don't own shares yet
- **If stock stays above $11:** option expires worthless, you keep the cash, repeat
- **If stock drops below $11:** you get "assigned" — you now own 100 shares at $11

### Phase 2 — Sell a Covered Call (CC)
- Only happens if you got assigned and now own 100 shares
- The bot sells someone the right to buy your shares at a higher price (e.g. $12)
- You collect more premium while you wait
- **If stock rises above $12:** your shares get called away, you pocket the gain + premium
- **If stock stays below $12:** option expires, you sell another covered call and repeat

The cycle: **CSP → (maybe assigned) → CC → back to CSP**

---

## What Determines Each Trade

Five layers of filtering before any order is placed:

1. **Watchlist scan** — Finviz finds stocks $10–$50 with active options markets
2. **Mechanical rules** — checks market trend, volatility rank, upcoming earnings (blocked within earnings window), and account risk limits
3. **Claude Sonnet** — second opinion on the signal, approve/reject with confidence score
4. **Claude Opus 4.7** — picks the specific contract (strike price + expiration) from the options chain
5. **Risk manager** — final position sizing before order hits Alpaca

Nothing trades unless all five layers agree.

---

## Key Numbers

| Parameter | Value |
|---|---|
| Account size | $10,000 (paper) |
| Stock price range | $10–$50 |
| Collateral per trade | $1,000–$5,000 |
| Max open positions | 6 |
| Max new trades/week | 3 |
| Target holding period | 21–45 days |
| Target delta | ~0.28 (moderately out of the money) |
| Profit target | 50% of premium collected |
| Stop loss | 2× premium paid |

---

## The Risk

The main risk is getting assigned on a stock that then **keeps falling and doesn't recover**. You're stuck holding 100 shares worth less than you paid. The bot manages this by:
- Only targeting stocks it's willing to own (quality filter)
- Avoiding earnings windows
- Halting all trading if drawdown hits 15%
- Limiting position size to 20% of account per trade

---

## A Typical Day

| Time | What Happens |
|---|---|
| 8:00 AM | Portfolio sync, market regime check, AI pre-market briefing |
| 8:30 AM | Watchlist refreshed from Finviz |
| 9:30 AM | Market opens, live data stream starts |
| Every 15 min | Options chains refreshed, new trades evaluated |
| 4:00 PM | Market closes, AI writes daily review (graded A–F) |
| Friday 4:05 PM | Weekly performance review generated |

---

## Active Strategies

| Strategy | Status | Reason |
|---|---|---|
| Wheel (options) | ✅ Enabled | Primary strategy |
| Momentum (stocks) | ❌ Disabled | PDT risk on $10K account |
| Swing (stocks) | ❌ Disabled | PDT risk on $10K account |

Momentum and Swing trade large stocks directly (SPY ~$580, NVDA ~$130+). Three round-trip trades in 5 days on a sub-$25K account triggers the Pattern Day Trader rule and locks the account. Re-enable after graduating to a live account above $25K.

---

## The 30-Day Goal

After one month of paper trading:
- 3+ full Wheel cycles completed
- Trending toward $200/month in premium collected
- No PDT violations, drawdown stayed under 15%

If those are met → consider moving to a live account and adding paid data feeds.
