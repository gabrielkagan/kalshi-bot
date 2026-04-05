---
status: active
updated: 2026-04-03
tags: [failure, sol, maker, adverse-selection]
severity: major
---
# SOL Maker Adverse Selection

## Summary
SOL's MAKER_PATIENT execution path had 88.1% WR — below the 88.7% taker breakeven — meaning maker fills on SOL were net-negative. The escalation_wait path was toxic, and the empty-book fallback mechanism masked the problem. Led to SOL being switched to taker-first execution.

## Symptom
- SOL maker fill rate: only 44.7%
- SOL maker WR: 88.1% (below 88.7% breakeven for maker execution)
- $101/week in missed opportunity cost from unfilled maker orders
- Escalation_wait path (maker posts, waits, then escalates to taker) accumulated slip averaging 3.4 cents

## Root Cause: Adverse Selection
Maker orders on SOL suffer adverse selection:
1. Bot posts a maker order below the current ask
2. The order fills **only when price moves against the bot** (someone hitting our bid means they think price is going lower)
3. Orders that would have been profitable don't fill — the market moves up and away from our resting order
4. The 44.7% fill rate means more than half of good opportunities are missed entirely

The escalation path compounds this: by the time the bot cancels the unfilled maker and submits an IOC taker, the market has moved and entry price is worse by ~3.4 cents.

## Data
- 44.7% maker fill rate for SOL
- $101/week missed opportunity cost (unfilled orders that would have been winners)
- 95% WR on unfilled orders (confirming adverse selection — the good ones don't fill)
- Taker fee delta: ~$2/week extra cost vs $101 missed

## Fix
`SOL_TAKER_FIRST = True` — SOL bypasses maker entirely, submits direct IOC at all STC values.

**SOL empty-book maker fallback:** When SOL orderbook is completely empty (depth=0), still uses maker at 87c+ with STC >= 60s. Data: 400 unfilled at depth=0, 95% WR. This is safe because on an empty book there's no one to adversely select against us.

## Lessons
1. **Maker-first is not always optimal** — SOL's microstructure favors takers
2. **Track unfilled opportunity cost** — the cost of not getting filled can exceed the maker fee savings
3. **Adverse selection is measurable** — compare WR of filled vs unfilled maker orders
4. **Per-asset execution rules matter** — different assets have different microstructure

## Related
- [[concepts/execution-layer.md]]
- [[concepts/sol-dynamics.md]]
- [[concepts/per-asset-rules.md]]
- [[decisions/sol-edge-floor.md]]
