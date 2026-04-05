---
status: active
updated: 2026-04-05
tags: [research, overnight, calibration, golden-hour, vol-model]
---
# Overnight Model Miscalibration Analysis

Date: April 5, 2026
Scope: Weekday 04-11 UTC, 15M product type, insufficient_edge signals at 91-94c

## Summary

The volatility model systematically underestimates probability during overnight hours (04-11 UTC), producing insufficient_edge rejections on contracts that settle YES at 95%+ WR. The root cause is the BLR calibrator being disabled (Mar 28) — the one mechanism designed to correct this bias. The verified opportunity is real but modest (+$259 over 19 weekdays = $13.60/day at 50ct) and **not yet statistically significant** (p=0.15).

## Core Finding: Model Underconfidence

| Price Tier | Model CalProb | Actual WR | Gap | Breakeven WR | Profitable? |
|-----------|---------------|-----------|-----|-------------|-------------|
| 91-92c | 89.0% | 96.2% | +7.2pp | 92.0% | CI doesn't clear BE |
| 93-94c | 92.3% | 95.3% | +3.0pp | 93.5% | CI doesn't clear BE |
| 95-96c | 91.0% | 94.5% | +3.5pp | 95.3% | No — negative PnL |
| 97-99c | 88.5% | 93.3% | +4.8pp | 98.1% | No — deeply negative |

This is overnight-specific. Daytime control (12-23 UTC) at 91-94c: 92.6% WR — every tier net negative PnL.

## Verified PnL (50 contracts, taker fees)

| Tier | n | WR | PnL | Wilson CI Lower | Breakeven |
|------|---|-----|-----|-----------------|-----------|
| 91-92c | 118 | 95.8% | +$212 | 90.5% | 92.0% |
| 93-94c | 165 | 94.5% | +$47 | 90.0% | 93.5% |
| 95-96c | 111 | 94.6% | -$64 | 88.7% | 95.3% |
| 97-99c | 128 | 93.0% | -$329 | 87.2% | 98.1% |

**CI lower bounds are below breakeven at every tier.** Cannot statistically confirm profitability.

## Statistical Significance

- Overnight vs daytime at 91-94c: z=1.43, **p=0.152** — not significant
- Rolling WR is stable (93-95% across 283 trades over 19 weekdays) — no degradation
- Losses spread across 10 of 19 days, max 2 per day — no clustering
- STC <= 400 refinement: 97.3% WR (145W/4L) on 149 trades — strongest sub-filter

## Root Cause: BLR Disabled

`FIFTEEN_M_CALIBRATION_ENABLED = False` since commit d9ce294 (Mar 28). The BLR calibrator (active_method="blr", n=500 observations, Brier=0.040) maps raw probs correctly:

- Without BLR (current): raw 83% → passthrough 83% → blend 86.6% → edge -5.4% → **rejected**
- With BLR: raw 83% → BLR 95.5% → blend 93.8% → edge +1.8% → **passes at 92c**

BLR was disabled because global Brier was marginally worse (0.0648 vs 0.0634 raw). The overnight-specific improvement was washed out by daytime data.

## Vol Engine Confirms

Overnight vol is genuinely ~6-11% lower than daytime:

| Asset | Daytime Vol | Overnight Vol | Reduction |
|-------|------------|---------------|-----------|
| BTC | 0.000195 | 0.000183 | -6.2% |
| ETH | 0.000271 | 0.000259 | -4.4% |
| SOL | 0.000167 | 0.000161 | -3.6% |
| XRP | 0.000138 | 0.000124 | -10.1% |

The vol model correctly measures lower overnight vol, but the probability output is still ~7pp below reality at 91-92c. The gap is in the probability model → calibration layer, not the vol estimate.

## "Golden Hour" Framing Was Wrong

The original golden_hour_shadow (UTC 3, 5, 6, 11) captured a symptom of this broader overnight miscalibration. The actual pattern:
- It's NOT 4 specific hours — it's the entire 04-11 UTC window
- Hours 7, 9, 10, 20, 21 are equally strong but weren't labeled "golden"
- The dead hours (UTC 4, 15, 17, 18) are real but also not statistically significant yet
- The overnight discount at 0.6x doesn't reach deep enough — only captures 21 of 557 eligible signals

## Per-Asset (91-94c overnight)

| Asset | n | WR | PnL |
|-------|---|-----|-----|
| BTC | 94 | 94.7% | +$2.03 |
| ETH | 56 | 96.4% | +$2.20 |
| SOL | 43 | 95.3% | +$1.09 |
| XRP | 90 | 94.4% | +$1.20 |

All 4 assets individually above 94% WR. No single toxic asset at this tier. (Note: XRP at 91c not tradeable due to 92c floor.)

## Existing Strategy Overlap

During overnight hours, live strategies already capture:
- Regular candidates: 149 trades, 98.0% WR (signals with sufficient edge)
- DC overlays: 136 trades, 100% WR (z-score based, ignores edge)
- Terminal momentum: 45 trades, 97.8% WR (95-99c near expiry)
- Overnight discount: 36 live fires — but ALL 36 also had candidate entries (currently zero incremental)

## Recommendations

1. **Don't build a golden hour strategy** — the concept is wrong. This is a calibration gap, not a strategy gap.
2. **Keep shadowing** — need 600+ trades for CI to clear breakeven. Current rate: ~15/day → ~2 more months.
3. **Investigate overnight-specific BLR** — train on overnight data, evaluate on overnight Brier. If it improves overnight without hurting daytime, re-enable with time-gating.
4. **STC <= 400 is the strongest refinement** — if shadowing, gate on this. Removes 71% of losses, keeps 53% of volume.
5. **Track dead hours separately** — UTC 4/15/17/18 losses are directionally real (-$722 on 256 trades) but same sample size limitation.

## Related

- [[decisions/blr-removal.md]] — Why BLR was disabled
- [[strategies/overnight-discount.md]] — Current overnight discount implementation
- [[concepts/edge-thresholds.md]] — Price-dependent edge schedule
- [[concepts/cal-engine-registry.md]] — CalEngine architecture
