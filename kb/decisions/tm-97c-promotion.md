---
status: decided
updated: 2026-04-02
tags: [decision, tm, 97c, promotion]
date: 2026-04-02
---
# Decision: Add 97c to Terminal Momentum Price Set

Date: 2026-04-02
Status: Decided

## Context

Terminal Momentum (TM) trades high-probability contracts in the final seconds
before settlement. The TM_PRICE_SET controlled which prices were eligible:
`{95, 96, 98, 99}`. The value 97 was excluded because early data showed it
as a "dead zone" with 95.7% WR -- below the ~97% breakeven threshold at that
price point.

New data accumulated since the original exclusion decision. As of April 2,
2026, 97c showed **98.2% WR on 55 observations**, comfortably above the 97%
breakeven. The sample size (n=55) is modest but sufficient given the binary
nature of TM outcomes and the tight confidence interval at 98%+ WR.

## Options Considered

1. **Keep 97c excluded** -- wait for more data. Tradeoff: leaving money on the
   table at a price point that now appears profitable.
2. **Add 97c to TM_PRICE_SET** -- simple one-line change. Tradeoff: 55
   observations is not huge, but the WR is well above breakeven.
3. **Shadow 97c first** -- collect more observations before live. Tradeoff:
   TM already has extensive infrastructure; adding a shadow path for one
   price point is overengineering.

## Decision

Add 97 to TM_PRICE_SET, making it `{95, 96, 97, 98, 99}`. This is a
one-line change in bot.py plus a test update.

Rationale:
- 98.2% WR on 55 obs exceeds 97% breakeven by 1.2pp
- TM at 95c and 96c (lower WR prices) are already live
- The original exclusion was based on early sparse data that has been superseded
- TM uses fixed 50-contract sizing so the risk per trade is bounded

## Consequences

- 97c contracts now eligible for TM execution in the 61-300s STC window
- Expected to add a small number of incremental trades per day
- Monitor first 50 live fills at 97c to confirm WR holds
- If WR drops below 96% on 100+ live trades, re-evaluate

## Related

- [[strategies/terminal-momentum.md]] - Full TM strategy documentation
- [[concepts/dc-strategy.md]] - DC strategy also operates in the 93-96c range
- [[concepts/fee-optimization.md]] - Fee impact at 97c (taker fee = ~2c)
