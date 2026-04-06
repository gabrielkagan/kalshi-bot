---
status: active
updated: 2026-04-03
tags: [fees, maker, taker, fill-rate]
---
# Fee Optimization

## Summary
Kalshi charges $0 for maker fills and `ceil(0.07 * C * P * (1-P))` for taker fills. This asymmetry drives the bot's maker-first execution strategy, saving roughly $50/day in fees at current volume. However, maker fills are not guaranteed -- fill rates vary dramatically by asset (BTC ~55%, SOL ~28% pre-taker-first), creating a tradeoff between fee savings and missed opportunities.

## Fee Formula

From `models.py`:

```python
def calculate_fee(count, price_cents, is_taker,
                  fee_mult_taker=0.07, fee_mult_maker=0.0):
    if not is_taker:
        return 0
    return math.ceil(fee_mult_taker * count * price_cents
                     * (100 - price_cents) / 100)
```

- **Taker:** `ceil(0.07 * C * P * (1-P))` -- quadratic in price, peaks at 50c
- **Maker:** $0 (Kalshi charges no fee on maker fills)
- **SPX exception:** Finance category gets 50% discount (`fee_mult_taker=0.035`)

## Fee Impact by Price Level

The `P * (1-P)` term makes fees price-dependent. At high prices where the bot trades most, fees are small:

| Price | Fee per Contract (taker) | % of Risk | Impact |
|-------|-------------------------|-----------|--------|
| 95c | ceil(0.07 * 95 * 5 / 100) = 1c | 0.33% of 3c risk | Negligible |
| 90c | ceil(0.07 * 90 * 10 / 100) = 1c | 0.63% of 10c risk | Low |
| 85c | ceil(0.07 * 85 * 15 / 100) = 1c | 0.89% of 15c risk | Low |
| 80c | ceil(0.07 * 80 * 20 / 100) = 2c | 1.12% of 20c risk | Moderate |
| 70c | ceil(0.07 * 70 * 30 / 100) = 2c | 1.47% of 30c risk | Moderate |
| 60c | ceil(0.07 * 60 * 40 / 100) = 2c | 1.68% of 40c risk | Significant |
| 50c | ceil(0.07 * 50 * 50 / 100) = 2c | 1.75% of 50c risk | Significant |

**Key insight:** At the bot's typical entry prices (89-97c), taker fees are 1c per contract. The fee advantage of maker over taker is real but small per contract -- it matters in aggregate across hundreds of daily trades.

For multi-contract positions, the fee applies to the total: `ceil(0.07 * 100 * 95 * 5 / 100) = 33c` for 100 contracts at 95c. This is where maker savings become substantial.

## Execution Strategy: Maker-First

The $0 maker fee drives the default execution path:

1. **Post maker order** (post_only=True) at desired price
2. **Poll for fill** during available time
3. **Escalate to taker IOC** if unfilled before STC deadline

This saves the full taker fee on every maker fill. At current volume (~200-400 trades/day), maker fills save an estimated $50/day in aggregate fees.

## The Fill Rate Problem

Maker orders are not guaranteed to fill. Pre-taker-first fill rates by asset:

| Asset | Maker Fill Rate | Issue |
|-------|----------------|-------|
| BTC | ~55% | Thick book, moderate fill rate |
| ETH | ~50% | Similar to BTC |
| SOL | ~28% | Thin book, adverse selection |
| XRP | ~45% | Moderate |

**SOL was the worst case:** Only 28% of maker orders filled, meaning 72% had to escalate to taker or miss entirely. The low fill rate combined with adverse selection (see [[failures/sol-maker-adverse-selection.md]]) led to `SOL_TAKER_FIRST=True` -- SOL now bypasses maker entirely and submits IOC at all STC levels.

## The Maker/Taker Tradeoff

The core tension:

- **Maker saves fees** but risks missing the trade entirely if the market moves away
- **Taker guarantees execution** but pays 1-2c per contract in fees
- **Unfilled maker orders have opportunity cost** -- the edge that existed when the signal fired may no longer exist by the time the bot escalates

For a 100-contract position at 95c:
- Maker fill: $0 fee, but ~45% chance of missing the trade
- Taker fill: $0.33 fee, guaranteed execution
- Expected fee cost of maker-first: 0.55 * $0 + 0.45 * $0.33 = $0.15 (if escalation works)
- But if the opportunity vanishes during maker wait: $0 fee, $0 profit

The `/maker-cost` skill tracks this tradeoff with fill rate data, unfilled opportunity cost, and per-asset capture rate analysis.

## Decided Contracts and Fee Impact

Decided contracts (T1/T1B/T2) route directly to taker because:
1. Settlement is near-certain (z <= -2 to -5)
2. Speed matters more than fee savings
3. Fee at 93-97c is only 1c/contract
4. Missing a 97c decided contract costs 3c/contract -- 3x the fee

## Fee Tracking: Per-Fill Accumulation (Apr 6, 2026)

`record_settlement()` previously recomputed the fee on the full position at settlement time. For escalated orders (maker→taker), `is_taker=MAX(all_fills)=True` caused the ENTIRE position to be charged taker fee, even when some contracts filled as maker ($0 fee). This overcounted fees by ~$120 across 1329 trades.

**Fix:** Added `accumulated_fee_cents` column to positions table. Each fill in `record_position_from_fill()` computes its own fee using the correct per-fill `is_taker` and accumulates. At settlement, the accumulated fee is used instead of recomputing. Legacy positions without accumulated data fall back to the old computation.

See [[failures/pnl-reporting-bugs.md]] for the full incident.

## Related
- [[concepts/execution-layer.md]]
- [[failures/sol-maker-adverse-selection.md]]
- [[failures/pnl-reporting-bugs.md]]
- [[concepts/dc-strategy.md]]
