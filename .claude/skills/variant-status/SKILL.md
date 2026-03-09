---
name: variant-status
description: "Focused shadow variant comparison with Kelly-sized sim PnL and promotion timeline estimates. Use when: \"how are A1 and A2 looking?\", \"variant comparison\", \"which variant is closest to promotion?\", \"shadow variant performance\""
---

# Shadow Variant Comparison

Focused comparison of shadow variant performance with actual Kelly sizing. Shows which variants are ready for promotion decisions.

## When to use
- "How are a1 and a2 looking"
- "Price shadow no xrp looking promising?"
- "How is BTC P>=70 wl2"
- "STC shadow variants"
- Any question about specific shadow variant performance

## Usage
```
/variant-status
/variant-status 15m
/variant-status hourly
```

## Steps

1. **Sync the database.** Follow `.claude/skills/references/db-sync.md` to sync the database.

2. **Query 15M shadow variants** (fifteenm_shadow_signals table):
   ```sql
   PRAGMA busy_timeout=10000;

   SELECT
     approach,
     asset,
     COUNT(*) as total,
     SUM(CASE WHEN settled=1 THEN 1 ELSE 0 END) as settled,
     SUM(CASE WHEN settled=1 AND result='win' THEN 1 ELSE 0 END) as wins,
     SUM(CASE WHEN settled=1 AND result='loss' THEN 1 ELSE 0 END) as losses,
     ROUND(100.0 * SUM(CASE WHEN settled=1 AND result='win' THEN 1 ELSE 0 END) /
       NULLIF(SUM(CASE WHEN settled=1 THEN 1 ELSE 0 END), 0), 1) as wr_pct,
     ROUND(SUM(CASE WHEN settled=1 THEN sim_pnl_cents ELSE 0 END) / 100.0, 2) as sim_pnl,
     MIN(created_at) as first,
     MAX(created_at) as latest
   FROM fifteenm_shadow_signals
   GROUP BY approach, asset
   ORDER BY approach, asset;
   ```

3. **Query hourly alt strategies** (if applicable):
   ```sql
   SELECT
     filter_stage,
     COALESCE(asset, ticker) as asset,
     COUNT(*) as total,
     SUM(CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END) as settled,
     SUM(CASE WHEN result='yes' THEN 1 ELSE 0 END) as wins,
     ROUND(SUM(CASE WHEN result IS NOT NULL THEN
       CASE WHEN result='yes' THEN (100 - entry_price) * COALESCE(position_size, 1)
            ELSE -entry_price * COALESCE(position_size, 1) END
       ELSE 0 END) / 100.0, 2) as sim_pnl
   FROM evaluated_opportunities
   WHERE product_type IN ('hourly', 'hourly_alt_a', 'hourly_alt_b')
     AND evaluation_time > datetime('now', '-7 days')
   GROUP BY filter_stage, COALESCE(asset, ticker);
   ```

4. **Present comparison table**:

   ```
   ## Shadow Variant Status

   ### 15M Approaches
   | Approach | Asset | Settled | W/L    | WR%   | Sim PnL  | Progress | Status     |
   |----------|-------|---------|--------|-------|----------|----------|------------|
   | A1       | BTC   | 45      | 40/5   | 88.9% | +$12.34  | 45/200   | Collecting |
   | A1       | ALL   | 142     | 120/22 | 84.5% | +$8.21   | 142/200  | 71%        |
   | A2       | BTC   | 23      | 19/4   | 82.6% | -$1.20   | 23/200   | Collecting |
   | A3       | ALL   | 67      | 58/9   | 86.6% | +$5.50   | 67/100   | 67%        |

   ### Promotion Readiness
   - A1: Need 58 more settled rows (est. ~3 days at current rate)
   - A2: Need 177 more settled rows (est. ~8 days)
   - A3: Need 33 more settled rows (est. ~2 days) ← closest to promotion
   ```

5. **Compute promotion timeline** estimate:
   - settled_per_day = settled_count / days_since_first_signal
   - days_to_target = (target - settled) / settled_per_day
   - If settled_per_day is 0 or days_since_first_signal < 1, say "insufficient data for timeline estimate"

## WHY Kelly sizing, not flat 1-contract

Flat 1-contract PnL treats a 93c trade and an 86c trade as equal-size bets. In reality, the bot uses Kelly sizing which scales position size by edge and probability:
- A 93c trade with 2% edge gets ~5 contracts
- An 86c trade with 4% edge gets ~12 contracts
- A loss at 93c costs $93×5 = $465; a win at 86c pays $14×12 = $168

Flat PnL can show a variant as profitable when Kelly-sized PnL shows it losing (or vice versa), because it ignores the correlation between position size and price tier. The `sim_pnl_cents` column already uses Kelly sizing from the shadow engine — always use it.

**WHY 200 settled for A1/A2, 100 for A3?** A1 and A2 are full replacement strategies (they'd replace the live model's probability estimate), so they need more evidence. A3 is a gating model (it only vetoes, never overrides), so it has less downside and needs a lower bar. 100 is the minimum where Wilson 95% CI narrows enough to be actionable (~±8pp at 85% WR).

## Error Handling

| Situation | Action |
|-----------|--------|
| `fifteenm_shadow_signals` table doesn't exist | Shadow engine hasn't initialized yet. Report: "Shadow engine not yet active — no variant data available." |
| All `settled` counts are 0 | Signals exist but none have settled yet. Report the pending counts and when the first signal was created. Say: "N signals pending settlement — check back after they expire." |
| `sim_pnl_cents` is NULL for some rows | The shadow engine may have been deployed before the sim_pnl column was added. Compute manually: `(100 - price) * size` for wins, `-price * size` for losses. Flag: "N rows missing sim_pnl — using manual computation." |
| One approach has dramatically different results per-asset | This is important signal, not an error. Break it out explicitly: "A1 is 92% on BTC but 71% on XRP — asset-specific risk." |
| Query returns rows for approaches you don't recognize | New shadow approaches may have been added. Show them all — don't filter to a hardcoded list. |

## IMPORTANT
- **All sim PnL MUST use actual position sizing** from the sim_pnl_cents column, NOT 1-contract flat
- If sim_pnl_cents is NULL, compute it: `(100 - price) * size` for wins, `-price * size` for losses, minus fees
- Present with-XRP and without-XRP breakdowns when relevant (XRP has known vol underestimation)
- Highlight any variant that's within 20 rows of promotion threshold — these are the ones the user cares most about
- Always show sample size — never make promotion recommendations on n < 50 settled
- If a variant has crossed its threshold, say so clearly: "A3 has 104/100 settled — ready for promotion evaluation with `/alpha-audit shadows`"
