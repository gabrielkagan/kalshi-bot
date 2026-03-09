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

1. **Checkpoint WAL + copy fresh state.db from VPS**:
   ```
   ssh botuser@45.55.181.30 "cd ~/kalshi-bot-repo && python3 -c \"import sqlite3; c=sqlite3.connect('state.db'); c.execute('PRAGMA wal_checkpoint(PASSIVE)'); c.close()\""
   scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
   ```

2. **Query 15M shadow variants** (fifteenm_shadow_signals table):
   ```sql
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
   | Approach | Asset | Settled | W/L    | WR%   | Sim PnL | Progress | Status |
   |----------|-------|---------|--------|-------|---------|----------|--------|
   | A1       | BTC   | 45      | 40/5   | 88.9% | +$12.34 | 45/200   | Collecting |
   | A1       | ALL   | 142     | 120/22 | 84.5% | +$8.21  | 142/200  | 71%    |
   | A2       | BTC   | 23      | 19/4   | 82.6% | -$1.20  | 23/200   | Collecting |
   | A3       | ALL   | 67      | 58/9   | 86.6% | +$5.50  | 67/100   | 67%    |

   ### Promotion Readiness
   - A1: Need 58 more settled rows (est. ~3 days at current rate)
   - A2: Need 177 more settled rows (est. ~8 days)
   - A3: Need 33 more settled rows (est. ~2 days)
   ```

5. **Compute promotion timeline** estimate:
   - settled_per_day = settled_count / days_since_first_signal
   - days_to_target = (target - settled) / settled_per_day

## IMPORTANT
- **All sim PnL MUST use actual position sizing** from the sim_pnl_cents column, NOT 1-contract flat
- If sim_pnl_cents is NULL, compute it: `(100 - price) * size` for wins, `-price * size` for losses, minus fees
- Present with-XRP and without-XRP breakdowns when relevant
- Highlight any variant that's within 20 rows of promotion threshold
- Always show sample size — never make promotion recommendations on n < 50 settled
