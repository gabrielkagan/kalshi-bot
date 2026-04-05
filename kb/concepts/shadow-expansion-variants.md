---
status: active
updated: 2026-04-03
tags: [shadow, dc-expansion, low-price, promotion]
---
# Shadow Expansion Variants

## Summary
Beyond the main live strategies, six DC shadow variants, a low-price shadow (70-79c), and an overnight LP shadow run in parallel to evaluate expansion opportunities. All are shadow-only (no orders placed), log to evaluated_opportunities for settlement linking, and appear in dashboard counterfactual analysis.

## DC Shadow Variants
Defined in `DC_SHADOW_STAGES` (bot.py ~584). Each evaluates a relaxed DC condition that is not yet promoted:

| Variant | Condition | Data (W/L) | Notes |
|---------|-----------|------------|-------|
| dc_shadow_t1b_93c | T1B at 93-94c floor | 13/14 | 1 loss at small n |
| dc_shadow_t2_z25 | T2 relaxed to z <= -2.5 | 47/48 | Near promotion threshold |
| dc_shadow_t2_90c | T2 floor 90c (BTC/ETH/SOL) | 29/29 | Perfect record |
| dc_shadow_t2_90c_xrp | T2 floor 90c (XRP only) | 10/11 | Isolated for XRP risk |
| dc_shadow_t2_z2 | T2 relaxed to z <= -2 | 105/111 | Widest z relaxation |
| dc_shadow_no_side | NO-side decided (z >= 5) | 166/166 | Perfect but NO-side has structural concerns |

These are inserted via `_dc_shadow_insert_pre()` and `_dc_shadow_insert()` helper functions during the scan loop. Each uses the standard evaluated_opportunities schema with the variant name as `filter_stage`.

## Low-Price Shadow (70-79c)
`LOW_PRICE_SHADOW_ENABLED = True` (bot.py ~709). Evaluates 15M signals in the 70-79c range that the live bot rejects due to per-asset price floors.

**Dual sizing simulation** (line 10819):
- **Full Kelly**: current sizer output -- what would happen if the price floor was simply lowered
- **Capped Kelly**: `LP_KELLY_FRACTION=0.25` (quarter-Kelly) with `LP_MAX_RISK_PER_TRADE=0.10` (10% cap) -- conservative alternative

**Correlation tracking**: per-window and per-hour signal counts to measure simultaneous low-price exposure. This data answers: "if we lower the floor, how many correlated low-price positions would we hold at once?"

Data stored in both `evaluated_opportunities` (filter_stage="low_price_shadow") and a dedicated `low_price_shadow_signals` table with full/capped sizing columns. Settlement backfills both tables.

## Overnight LP Shadow
`_process_overnight_lp_shadow()` (bot.py ~10577). Evaluates 50-85c YES contracts during overnight hours (00-12 UTC).

**Thesis**: overnight market makers are slow/absent, creating stale pricing on cheap YES contracts where the model predicts 90%+ probability.

**Dual execution simulation**:
- Taker path: IOC at best_ask
- Maker path: post-only at best_bid + 1 cent

**Vol-spike circuit breaker**: if current `blended_rv > 2x` overnight median (from 100+ prior observations), skip the asset. Prevents false signals during overnight volatility spikes.

Sizing uses `OVERNIGHT_LP_KELLY_FRACTION=0.125` (eighth-Kelly) and `OVERNIGHT_LP_MAX_RISK_PER_TRADE=0.10` -- extra conservative because calibration was trained on 86-99c data, not this price range.

Logged as filter_stage="overnight_lp_shadow" in evaluated_opportunities.

## Shadow Promotion Criteria

All shadows use the same promotion framework:
1. **Wilson CI lower bound** above breakeven WR for the price tier
2. **Minimum 30+ settled observations** (forward-tested, not backtested)
3. **Data from current config regime only** (post-parameter-change data)

**Promoted**: Overnight discount (75 settled at 89c+, 93.8% WR), DC T2_Z25 (clean WR), DC T2_Z2 (promoted then re-shadowed after two losses)

**Killed**: NO-side DC (50/50 settlement, zero edge), A2 LightGBM as calibrator (price proxy only, no independent signal), DIP_ADDON (55.2% WR), hourly shadow configs h/j/k (55% WR)

## Related
- [[concepts/dc-strategy.md]]
- [[concepts/dc-execution-mechanics.md]]
- [[strategies/overnight-discount.md]]
- [[concepts/edge-thresholds.md]]
