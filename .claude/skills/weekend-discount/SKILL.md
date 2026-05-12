---
name: weekend-discount
description: "Evaluate weekend/overnight edge discount shadow strategies — graduation criteria, per-asset WR, edge inversion checks. Use when: \"how's the weekend discount doing?\", \"overnight discount status\", \"should we promote any discounts?\", \"quiet-market shadow check\""
---

# Quiet-Market Edge Discount Shadow Audit

Evaluate weekend and overnight edge discount shadow strategies — performance, graduation readiness, and comparison.

## Usage
```
/weekend-discount
/weekend-discount fresh    # Force fresh DB copy from VPS
/weekend-discount weekend  # Weekend section only
/weekend-discount overnight # Overnight section only
```

## When to use
- "how's the weekend discount doing"
- "how's the overnight discount doing"
- "shadow discount status"
- "should we promote any discounts"
- "check quiet-market shadow performance"

## Steps

1. **Sync the database.** Follow `.claude/skills/references/db-sync.md` to sync the database. If user says "fresh", always re-sync regardless of cache age.

2. **Run the audit script**:
   ```bash
   # Both sections (default)
   python3 scripts/audit/weekend_discount_audit.py --db /tmp/state.db 2>&1

   # Weekend only
   python3 scripts/audit/weekend_discount_audit.py --db /tmp/state.db --weekend-only 2>&1

   # Overnight only
   python3 scripts/audit/weekend_discount_audit.py --db /tmp/state.db --overnight-only 2>&1
   ```

3. **Present findings**, focusing on:
   - **Graduation status** for each: YES / NO / NEED MORE DATA
   - **Key metrics**: overall WR with CI, sim PnL, per-asset health
   - **Any asset dragging below 75%**: flag it specifically
   - **Comparison**: are weekend and overnight tracking similarly?
   - **Expected timeline**: based on signal accumulation rate

4. **Present summary**:
   ```
   ## Weekend/Overnight Discount Shadow Status

   ### Weekend (WEEKEND_EDGE_DISCOUNT = 0.60)
   - 28 settled: 24W/4L (85.7%, Wilson 95% CI: 67.3-96.0%)
   - Sim PnL: +$18.40
   - Graduation: NEED MORE DATA (28/60 settled, est. 4 more weekends)

   | Asset | Settled | WR%   | Status |
   |-------|---------|-------|--------|
   | BTC   | 12      | 91.7% | OK     |
   | ETH   | 8       | 87.5% | OK     |
   | SOL   | 6       | 66.7% | WATCH  |
   | XRP   | 2       | 100%  | LOW N  |

   ### Overnight (OVERNIGHT_EDGE_DISCOUNT = 0.60)
   - 18 settled: 15W/3L (83.3%, Wilson 95% CI: 58.6-96.4%)
   - Sim PnL: +$9.20
   - Graduation: NEED MORE DATA (18/60 settled, est. 6 more weeks)

   ### Verdict
   Both on track but too early to promote. Weekend SOL at 66.7% needs monitoring.
   ```

5. **If user asks follow-ups**:
   - "what about just SOL?" → re-run with `--asset SOL`
   - "what if we used 0.7x instead?" → re-run with `--discount 0.7`
   - "should we promote?" → check all 4 graduation criteria explicitly

6. **If VERDICT is YES (ready to promote)**, draft the code change:
   - Apply the relevant discount multiplier to the live edge check (constant definitions in `bot/constants.py` post-Bit-3.1; the live edge gate logic stays in `bot/_impl.py`)
   - Show the exact diff before/after
   - Wait for user confirmation before deploying

## Graduation Criteria — with WHY for each

| # | Criterion | Threshold | WHY |
|---|-----------|-----------|-----|
| 1 | Settled signals | ≥ 60 | Weekend/overnight signals accumulate slowly (~2-4 per weekend, ~1-2 per weeknight). At 60, Wilson CI at 85% WR is (73.4%-93.5%) — narrow enough to confirm edge exceeds breakeven. Below 60, CI is too wide for a promotion decision. |
| 2 | Overall WR | ≥ 85% | The discount LOWERS the edge threshold (0.60× = 40% reduction). At lower edge, breakeven WR is higher because the margin for error is smaller. 85% provides the ~2pp buffer over typical breakeven (82-84% at 86-90c) needed to survive out-of-sample degradation. |
| 3 | No single asset < 75% WR (n≥5) | Floor | If one asset consistently loses at discounted edge, the discount is too aggressive for that asset's volatility profile. 75% is below overall 85% target because individual assets have higher variance at small n — but below 75% is a structural problem, not noise. Requiring n≥5 prevents a single loss from flagging an asset. |
| 4 | No edge inversion (worst tier ≥ 70%) | Monotonicity | Edge inversion means higher-edge trades have LOWER WR — the signal is noise. If the worst price tier is below 70%, the discount is creating false positives at that tier. 70% is a generous floor (below breakeven) because we're looking for catastrophic inversion, not marginal underperformance. |

**WHY 0.60 discount?** The edge threshold multiplier of 0.60 means that during quiet market periods (weekend/overnight), the bot accepts trades with 60% of the normal minimum edge. The hypothesis is that volatility is lower during these periods, so the model's probability estimates are more accurate (less noise), and the reduced edge requirement captures trades that the normal threshold incorrectly filters out. If WR stays high at the lower bar, the normal threshold is too conservative for quiet periods.

## Config Reference

### Weekend
- `WEEKEND_EDGE_DISCOUNT = 0.60` in `bot/constants.py`
- filter_stage: `weekend_discount_shadow`
- Active: Saturday/Sunday (UTC weekday >= 5)

### Overnight
- `OVERNIGHT_EDGE_DISCOUNT = 0.60` in `bot/constants.py`
- `OVERNIGHT_QUIET_START = 4`, `OVERNIGHT_QUIET_END = 11` (UTC hours)
- filter_stage: `overnight_discount_shadow`
- Active: weekdays only, 04:00-11:00 UTC (23:00-06:00 ET)
- Skipped when weekend discount already applies (no overlap/stacking)

### Shared
- Only applies to 15M markets (`product_type in (None, '15m')`)
- Only re-evaluates `insufficient_edge` rejections at prices >= MIN_ENTRY_PRICE (86c)
- Settlement: automatic via `_poll_evaluated_opportunities()` — fills `counterfactual_pnl`
- Dashboard: `snap["weekend_discount_shadow"]` and `snap["overnight_discount_shadow"]`

## Error Handling

| Situation | Action |
|-----------|--------|
| Script not found | Check: `ls scripts/audit/weekend*`. |
| 0 signals for weekend or overnight | The shadow may not be wired into the scan loop yet, or no `insufficient_edge` rejections occurred during quiet periods. Check: `SELECT COUNT(*) FROM evaluated_opportunities WHERE filter_stage LIKE '%discount_shadow%'`. If 0, the code path may not be executing. |
| Very few settled (< 10) | Report what exists but say: "n=N is too small for any conclusions. Weekend signals accumulate at ~2-4/weekend. Need N more weekends." |
| One asset shows 100% WR at n=2 | Don't celebrate. Say: "n=2 is meaningless. Need ≥5 before asset-level WR is informative." |
| SOL or XRP below 75% at n≥5 | Flag it prominently but check if the bot's live XRP_15M_SHADOW exclusion also applies here. If XRP is shadow-only already, the discount shadow shouldn't include it either. |
| Weekend and overnight show dramatically different WR | This is meaningful signal. Weekend has truly lower volatility (no institutional trading); overnight still has Asian markets. Different discount factors may be needed. Report the difference and suggest: "Consider separate discount tuning." |
| Script errors with `no such column` | The `filter_stage` values may not match what the script expects. Check actual values: `SELECT DISTINCT filter_stage FROM evaluated_opportunities WHERE filter_stage LIKE '%discount%'`. |
