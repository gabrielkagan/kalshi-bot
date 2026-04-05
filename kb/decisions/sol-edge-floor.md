---
status: decided
updated: 2026-03-28
tags: [decision, sol, edge, floor]
date: 2026-03-28
---
# Decision: SOL Minimum Edge Floor at 1.0%

Date: 2026-03-01 (approximate, refined through March)
Status: Decided

## Context
SOL generates the most trade volume and PnL but also the most risk. Analysis of SOL trade outcomes by edge bucket revealed a sharp discontinuity: trades with edge below 1.0% had dramatically worse performance than those at or above 1.0%.

## Options Considered
1. **No SOL-specific edge floor** — Use universal `MIN_EDGE_BY_PRICE` schedule
   - Risk: SOL's noisier vol estimates mean low-edge SOL trades are riskier than low-edge BTC trades
2. **SOL_MIN_EDGE = 1.0%** — Reject SOL trades below 1.0% fee-adjusted edge
   - Data: < 1.0% edge = 82% WR on SOL; >= 1.0% edge = 94.2% WR on 258 trades
   - Sharp cutoff, not gradual degradation
3. **SOL_MIN_EDGE = 1.8%** — More conservative floor
   - Initially set at 1.8% based on pre-BLR data, but that data was invalid under passthrough calibration
   - Reverted to 1.0% after BLR removal

## Decision
`SOL_MIN_EDGE = 0.010` (1.0%). Applied in addition to the universal price-dependent edge schedule.

## Data
| Edge Bucket | SOL WR | n |
|-------------|--------|---|
| < 1.0% | 82% | ~50+ |
| >= 1.0% | 94.2% | 258 |

The 12 percentage point gap in win rate at the 1.0% boundary is large and persistent. At 82% WR, after fees, SOL trades below 1.0% edge are approximately breakeven or slightly negative.

## Consequences
- Rejects the lowest-quality SOL signals
- Reduces SOL trade volume by ~15-20% (the low-edge tail)
- Increases average SOL WR and average PnL per trade
- Combined with taker-first execution, makes SOL the most profitable asset per trade

## Related
- [[concepts/sol-dynamics.md]]
- [[concepts/per-asset-rules.md]]
- [[failures/sol-maker-adverse-selection.md]]
