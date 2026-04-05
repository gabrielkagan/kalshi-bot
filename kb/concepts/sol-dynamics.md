---
status: active
updated: 2026-04-05
tags: [sol, edge-floor, sizing, taker-first, stc-gate]
---
# SOL Trading Dynamics

## Summary
SOL drives the majority of bot PnL and trade volume. This concentration creates both opportunity and risk.

## Edge Floor
SOL minimum edge: 1.0% under passthrough calibration. Verified correct.

## PnL Dominance
SOL consistently generates the most trades and profit due to favorable microstructure and volatility creating more opportunities.

## Risk Concentration
A string of SOL losses creates outsized drawdown. Mitigations:
- DC tiered risk caps (see [[concepts/dc-strategy.md]])
- SOL sub-86c time gate (see below)

## Sub-86c Time Gate (Apr 5, 2026)
`SOL_LOW_ENTRY_STC_GATE = True` — blocks SOL ≤85c at STC≥300s.

**Data (from stc-sizing-research):**
- SOL sub-86c near-expiry (<300s): 17 trades, 100% WR, +$228
- SOL sub-86c far-from-expiry (≥300s): 23 trades, 78.3% WR, **-$289**
- 85c is the #1 PnL-destroying price tier: 14.7pp model miscalibration gap
- All 4 catastrophic losses had STC > 350s with near-zero EGARCH sigma → oversized

**Mechanism:** At ≤85c, the market prices in downside risk. Near expiry, there isn't enough time for the move → free money. With 5+ min, SOL has enough runway to breach the threshold.

**Pipeline position:** After XRP shadow gate, before candidates.append(). Follows same pattern (dedup → insert_evaluated_opportunity → continue). Does NOT block DC, TM, or discount strategies (all have price floors ≥89c).

## Time-of-Day Patterns (Under Investigation)
Shadow tags deployed March 31:
- **sol_usmorn_sub88:** SOL morning session, sub-88c entries. Evaluate after 30+ obs.
- **usaft_short_stc:** US afternoon short STC. Evaluate after 30+ obs.
- Most TOD patterns were artifacts of pre-DC era, BLR, or t2_z2 losses — not genuine edge patterns.

## Related
- [[concepts/dc-strategy.md]]
- [[../kb-research/bot/stc-sizing-research.md]] — Source data for sub-86c gate and STC scaler
