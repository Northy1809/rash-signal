# rash-signal paper-bot

Paper-trading bot for [Polymarket](https://polymarket.com) CS2 markets, mirroring
signals from a specific public wallet (Rash) with half-Kelly sizing and hard-limit
guardrails. Paper-mode only — no real orders are placed.

## What it does

**Strategy A — mirror Rash's large BUYs:**
- Subscribes to Polymarket's public WSS activity feed
- Filters trades from Rash's proxyWallet with size ≥ $1,615 (p90+)
- Only entries in price buckets 0.55-0.65 or 0.65-0.75
- Live orderbook check: fills only if best_ask + slippage ≤ hard limit
- Half-Kelly sizing on compound bankroll, cap $2000/trade
- Holds to market resolution

**Strategy D — crash-buy:**
- Scans Rash's recently-traded assets every 15s
- Buys on >20% price crash in last 30 min if current price ∈ [0.10, 0.60]
- Exit after +60 min via best_bid (TP +5%, SL -8%)

## Run locally

```
python paper_bot.py           # main event-loop (WSS + periodic exits)
python paper_bot.py --status  # print state summary
python paper_bot.py --tick N S  # REST-only, N ticks S seconds apart (CI mode)
```

## Cloud drift

GitHub Actions runs `--tick 55 60` every hour (see `.github/workflows/tick.yml`),
giving ~55 min continuous coverage per hour with automatic state commits.

## Files

- `paper_bot.py` — main loop
- `paper_state.json` — persistent state (positions, cash, PnL, decision log)
- `BRIEF.md` — strategy detail + hard-limits derivation

## Data sources (all free, public)

- `data-api.polymarket.com/trades` — trade history
- `clob.polymarket.com/book` — live orderbook
- `gamma-api.polymarket.com/markets` — resolution outcomes
- `ws-live-data.polymarket.com` — real-time activity WSS

No API keys, no wallets, no real capital.
