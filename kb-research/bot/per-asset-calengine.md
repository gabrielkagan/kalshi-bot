---
status: deployed-shadow
updated: 2026-04-06
tags: [calibration, per-asset, sizing, overconfidence]
---
# Per-Asset 15M CalEngine

## Problem Statement

The shared 15M CalibrationEngine trains on aggregated data across all assets. This masks asset-specific calibration errors:

- **SOL at 300-600s STC**: Model predicts 91.3%, actual WR is 89.0% (+2.3pp overconfident). Kelly sizes for the predicted prob, creating catastrophically large positions on near-zero edge trades.
- **BTC at 300-600s STC**: Model predicts 92.3%, actual 90.5% (+1.9pp overconfident, all-time).
- **XRP at all STC**: Model is 1-2pp UNDERCONFIDENT. Per-asset CalEngine would size UP for XRP.

The aggregate 15M calibration looks fine (gap < 1pp) because asset-specific errors cancel out.

## Root Cause: "Positions Scale With Balance, But Edge Doesn't"

As the balance grew from $800 to $1400, Kelly sizing doubled position sizes. But the model's accuracy didn't improve. A 2pp overconfidence at $800 balance means $20 too much risk; at $1400 it means $40 too much. The overconfidence is amplified by balance growth.

Per-asset CalEngines fix this by correcting the probability INPUT to Kelly, so Kelly sizes appropriately for the actual edge.

## Key Data (Apr 6, 2026)

Per-asset, per-STC calibration (all-time):

| Asset | 0-120s gap | 120-300s gap | 300-600s gap | 300-600s Net PnL |
|-------|-----------|-------------|-------------|-----------------|
| BTC | +2.0% | -2.3% | +1.9% | -$42 |
| ETH | +11.4% | -3.0% | +0.9% | -$30 |
| SOL | +1.2% | -1.0% | +2.3% | **-$287** |
| XRP | -1.2% | -1.3% | -1.3% | +$48 |

SOL is 85% of the high-STC loss problem. The overconfidence varies over time (rolling 50-trade gap ranges from -1.6% to +5.7%), so a fixed temperature would be wrong half the time. An adaptive CalEngine that learns from settlement data is the right fix.

## Architecture

Uses the existing per-subtype CalEngine pattern (weather cities, sports leagues):

1. `market_config.py`: Add `cal_subtypes = {"BTC": "cal_15m_BTC.json", "ETH": "cal_15m_ETH.json", "SOL": "cal_15m_SOL.json", "XRP": "cal_15m_XRP.json"}` to 15M config
2. `_resolve_cal_engine()`: Handle 15M subtypes (currently returns None for 15M)
3. `_derive_subtype("15m", "BTC")`: Returns "BTC" directly (no extraction needed like weather)
4. MainLoop init: Creates 4 engines via existing cal_subtypes loop
5. Settlement routing: Already passes asset to `_resolve_cal_engine()` — works as-is
6. Keep `_CALIBRATION_ENGINE` as fallback for unknown assets or pre-training

## Data Readiness

All-time evaluated_opportunities with raw_prob:
- BTC: ~250 samples (Platt threshold: 200)
- ETH: ~288 samples (Platt threshold: 200)
- SOL: ~435 samples (approaching Beta Cal threshold: 500)
- XRP: ~358 samples (Platt threshold: 200)

## Alternatives Rejected

1. **SOL-specific temperature**: Fixed T=1.15 for SOL >300s. Rejected because overconfidence varies from -1.6% to +5.7% over time — a fixed correction is wrong half the time.
2. **Hard STC gate at 300s**: Blocks $730 in wins alongside $1,280 in losses. Profitable trades killed.
3. **Position cap**: Crude, doesn't scale intelligently with edge.
4. **STC-dependent temperature for all assets**: XRP is underconfident — temperature would hurt it.

## Implementation (Deployed Apr 6, 2026)

Deployed as shadow (cal_engine_enabled=False). All changes follow existing weather/sports subtype pattern.

**Changes made:**
- market_config.py: Added `cal_subtypes` to 15M with 4 per-asset state files
- bot.py `_derive_subtype()`: Added `if product_type == "15m": return asset`
- bot.py `_derive_asset_filter()`: Added `if product_type == "15m": return subtype_code`
- bot.py `_resolve_cal_engine()`: Changed `if product_type in (None, "15m")` to `if product_type is None` — lets "15m" fall through to subtype lookup
- bot.py startup loop: Removed `if _pt == "15m": continue` skip
- bot.py settlement: Dual-feed observations to both per-asset AND global engine
- bot.py migration: Backfills product_type on evaluated_opportunities (1,744 rows)
- test_config_consistency.py: Updated assertion to expect cal_subtypes

**Initial training results (500 obs each):**
- 15m_BTC: beta_cal, Brier 0.109 (backtest: 0.201→0.109, +46%)
- 15m_ETH: beta_cal, Brier 0.110 (backtest: 0.219→0.110, +50%)
- 15m_SOL: platt, Brier 0.170 (backtest: 0.222→0.170, +23%). Beta Cal had degenerate params, correctly rejected.
- 15m_XRP: platt, Brier 0.116

**Promotion criteria:** Enable per-asset (cal_engine_enabled=True for 15M) when:
1. Per-asset Brier consistently beats raw passthrough over 2+ weeks
2. SOL overconfidence gap narrows (current +3.5pp → target <1pp)
3. Walk-forward PnL validation shows improvement on 3+ out of 5 folds

## Related
- [[concepts/cal-engine-registry.md]]
- [[failures/pnl-reporting-bugs.md]]
- [[concepts/fee-optimization.md]]
