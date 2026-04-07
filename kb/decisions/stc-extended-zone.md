# STC Extended Zone: 300-600s with Per-Asset Higher Floors

**Date:** 2026-04-07
**Status:** Live (commit 3818e89)

## Decision

Re-enabled the 300-600s STC zone for live trading with per-asset higher price floors. Previously, STC_SHADOW_THRESHOLD=300 blocked all trades above 300s. Now STC_SHADOW_THRESHOLD=600, with a new `stc_extended_floor_shadow` gate enforcing higher floors in the 300-600s zone.

## Per-Asset Extended Floors

| Asset | Normal Floor | Extended Floor (300-600s) | Data |
|-------|-------------|--------------------------|------|
| BTC | 88c | **93c** | 93c+ = 98.1% WR, n=52, +$28 |
| ETH | 90c | **90c** (same) | 90c+ = 100% WR, n=31, +$63 |
| SOL | 80c | **95c** | 95c+ = 100% WR, n=14, +$36 |
| XRP | 92c | **92c** (same) | 92c+ = 100% WR, n=15, +$49 |

Combined: 83 trades, 82W/1L, 98.8% WR, +$160 PnL.

## Why It Works

The vol model is well-calibrated at <300s (gap = -0.6pp) but 9pp overconfident at 300-600s. At high prices, the base rate is so high (~97%+) that 9pp overconfidence doesn't matter — the trades still settle YES. At low prices (sub-93c for BTC, sub-95c for SOL), the overconfidence turns marginal trades into losers.

The fix isn't better calibration — it's simply raising the price floor in this zone so only the safe high-price segment trades.

## Risk Controls

1. **STC sizing scaler** — already active, `contracts *= 300/STC`. At STC=450s, position size is 0.67x normal.
2. **Per-asset floors** — only the profitable high-price segment passes through.
3. **All other filters unchanged** — edge check, fee-adjusted edge, Kelly sizing all still apply.

## Implementation

- `STC_SHADOW_THRESHOLD`: 300 → 600 (outer boundary)
- `STC_EXTENDED_LIVE_FLOOR = 300` (inner boundary)
- New gate after STC shadow gate: if `best_ask < extended_floor`, log as `stc_extended_floor_shadow` and block
- Trades that pass the extended floor proceed to candidate as normal

## Monitoring

- Filter stage `stc_extended_floor_shadow` — blocked trades
- Filter stage `candidate` with STC 300-600s — trades that passed
- Compare WR and PnL at 300-600s vs <300s to confirm edge holds

## Related

- [[../concepts/edge-thresholds.md]] — price-dependent edge schedule
- [[../strategies/overnight-discount.md]] — also uses STC gates
- `STC_SIZING_SCALER_KNEE = 300` — sizing reduction in this zone
