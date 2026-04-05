---
status: decided
updated: 2026-03-31
tags: [decision, dc, t2-z2, shadow]
date: 2026-03-31
---
# Decision: Shadow T2-Z2 Decided Contract Tier

Date: 2026-03-31
Status: Decided

## Context

The T2-Z2 decided contract tier (z <= -1.75, 93-96c) was live with 20% fixed
sizing (DECIDED_CONTRACT_T2_Z2_RISK = 0.20). This tier represented the
shallowest z-score threshold in the DC family, sitting below T2-Z25 (z <= -2.5)
and T2 (z <= -3.0).

Two catastrophic losses occurred in quick succession:

| Trade | Asset | Contracts | Price | PnL |
|-------|-------|-----------|-------|-----|
| Loss 1 | SOL | 296 | 96c | -$284.10 |
| Loss 2 | XRP | 279 | 96c | -$267.84 |

Both losses hit at 96c -- the top of the 93-96c range where downside per
contract is highest. The 20% fixed sizing produced outsized position sizes
that amplified the damage.

Net result across 47 T2-Z2 trades: **-$313 total PnL**. The win rate was
not catastrophic in isolation, but the sizing-to-WR ratio was unsustainable.

## Options Considered

1. **Kill entirely** -- remove T2-Z2 from code. Tradeoff: loses data collection
   on a z-score range that may have exploitable edge with better sizing.
2. **Shadow for more data** -- set DECIDED_T2_Z2_ENABLED="0" in env, keep
   logging observations. Tradeoff: gives up potential live PnL if the tier
   does have edge, but eliminates blowup risk while data accumulates.
3. **Reduce sizing** -- drop from 20% to 10% or lower. Tradeoff: still
   exposed to losses in a tier where the WR may not justify any live sizing.

## Decision

Shadow it. Set `DECIDED_T2_Z2_ENABLED="0"` in the VPS env var. The code
continues to evaluate and log T2-Z2 opportunities to evaluated_opportunities
with appropriate filter_stage, but no orders are placed.

Rationale: the z <= -1.75 range *might* have edge -- it is close to the
proven z <= -2.0 and z <= -2.5 tiers. But the 47-trade sample showed that
20% sizing at this shallow z-score is reckless. Shadowing preserves the
data pipeline while eliminating risk. Re-promotion requires:

- At least 100 shadow observations with settlement data
- Demonstrated WR above breakeven for the 93-96c price range
- Sizing no higher than 10% (half the original)

## Consequences

- T2-Z2 no longer places live orders
- Shadow observations continue flowing to evaluated_opportunities
- The deeper DC tiers (T1, T1B, T2, T2-Z25, T2-Z2 at z <= -2.0) remain live
- See [[failures/t2-z2-losses.md]] for detailed loss analysis
- See [[concepts/dc-strategy.md]] for the full DC tier architecture

## Related

- [[failures/t2-z2-losses.md]] - Root cause analysis of the two catastrophic losses
- [[concepts/dc-strategy.md]] - Decided contract strategy overview
- [[failures/loss-clustering.md]] - Mar 31 triple-loss window that included one T2-Z2 loss
