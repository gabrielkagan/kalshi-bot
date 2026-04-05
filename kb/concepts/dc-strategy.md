---
status: active
updated: 2026-04-01
tags: [strategy, dc, z-score, decided-contract]
---
# Decided Contract (DC) Strategy

## Summary
DC is the highest-performing strategy, near-100% WR across clean tiers. Trades contracts very likely to settle in a known direction based on price convergence at expiry.

## Tiers
| Tier | Status | Notes |
|------|--------|-------|
| T1 | Live | Highest confidence |
| T1B | Live | Slightly lower confidence |
| T2 | Live | Standard tier |
| T2_Z25 | Live | 2.5% zone |
| T2_Z2 | **Shadowed** | Two losses, ~$310 impact |

## SOL DC Tiered Risk
- ≤94c entry: 20% of standard size
- 95-96c entry: 10%
- ≥97c entry: 5%

## NO-Side DC
Evaluated and killed. NO-side settles ~50/50, zero edge.

## Fill Rate

## Related
- [[failures/t2-z2-losses.md]]
- [[concepts/sol-dynamics.md]]
