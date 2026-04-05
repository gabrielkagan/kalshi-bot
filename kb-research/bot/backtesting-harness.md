---
status: active
updated: 2026-04-04
tags: [research, backtesting, methodology, tool]
---
# Backtesting Harness — Build and Validation

Source: Claude Code session, 2026-04-04

## Summary

Built `scripts/backtest.py` — a parameter optimization tool that replays historical signals. Two modes: filter (actual trades with alternate caps/gates) and expansion (include rejected signals, approximate). The tool went through two broken iterations before producing validated results.

## The Tool

**Filter mode** (reliable): Replays actual settled_trades from the main pipeline. Applies gates (exclude assets), entry floors, and contract caps. Uses actual PnL — no balance-dependent re-sizing. Baseline matches reality within $1.

**Expansion mode** (approximate): Replays evaluated_opportunities including rejections. Models alternate edge thresholds. ~33% fill rate not modeled — use for relative comparison only, not absolute PnL.

## Two Rounds of Wrong Results

### Round 1: Phantom Signal Trading
The first version loaded ALL evaluated_opportunities (9,442 insufficient_edge + 467 candidate + shadows) and replayed every signal that passed the edge check. Result: 279 "trades" vs 156 actual. SOL showed -$678 (reality: +$71). Root cause: insufficient_edge signals that technically pass the edge check were rejected for other reasons (SOL_MIN_EDGE, STC timing, asset locks, ticker cooldowns) — the backtester couldn't model these.

### Round 2: Balance-Dependent Re-Sizing
Fixed to use settled_trades only (filter mode). But the filter function applied balance-dependent per-asset risk caps using a simulated $900 balance. The real bot had $900-$1,500 throughout the period. At $900, the SOL 12% cap = 121ct max; at $1,500 = 202ct max. Winning SOL trades at 170ct+ got silently reduced to 121ct, cutting $37 of real profit. Path-dependency made it worse — early losses reduced simulated balance further.

### Round 3: Validated
Removed balance-dependent re-sizing from filter mode. Use actual trade counts and PnL. Only apply explicit contract caps (balance-independent). Baseline: $85.47 vs reality $86.19 (99.2% accurate).

## Key Findings (Validated)

| Config | Trades | WR | PnL | MaxDD |
|---|---|---|---|---|
| Baseline | 155 | 92.3% | +$85 | 17.3% |
| Gate SOL | 78 | 93.6% | +$15 | 3.5% |
| SOL cap 30ct | 155 | 92.3% | +$31 | 8.7% |
| Gate BTC | 122 | 92.6% | +$90 | 17.2% |
| BTC floor 93c | 145 | 93.1% | +$87 | 17.2% |

**SOL is profitable** (+$71 on 77 trades). No intervention beats the baseline PnL. SOL cap 30ct is the only defensible change (halves drawdown at -$55 cost).

## Methodological Lessons

1. **Validate baseline against reality FIRST** — if the backtester can't reproduce actual PnL, nothing it says is trustworthy
2. **Filter mode should use actual data** — don't re-simulate what already happened. Use real counts, real PnL. Only modify what you're testing.
3. **Balance-dependent sizing creates path-dependency** — a fixed starting balance diverges from reality within days. Every trade after the first is sized wrong.
4. **`ONE_ASSET_PER_WINDOW = False`** — verified in code (bot.py:66). The bot trades multiple assets per window. This was incorrectly assumed to be True in the first round.
5. **Candidate → fill rate is ~33%** — 467 candidates evaluated, 156 filled. Expansion mode overstates trade count by 3x.

## Related (KB operational articles)
- [[kb/concepts/per-asset-rules.md]]
- [[kb/concepts/edge-thresholds.md]]
- [[kb/concepts/sol-dynamics.md]]
