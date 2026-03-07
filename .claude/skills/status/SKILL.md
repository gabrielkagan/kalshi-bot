# Quick Status Check

Lightweight pulse check across all systems. Not a full audit — just "is data flowing, any trades, anything notable."

## When to use
- "How's it going?"
- "15 min markets been quiet"
- "How is the no side coming along"
- "How are a1 and a2 looking"
- "What's happening"
- Any quick status question that doesn't need a full audit

## Usage
```
/status
/status 15m
/status hourly
/status no-side
/status variants
```

## Steps

1. **Checkpoint WAL + copy fresh state.db from VPS**:
   ```
   ssh botuser@45.55.181.30 "cd ~/kalshi-bot-repo && python3 -c \"import sqlite3; c=sqlite3.connect('state.db'); c.execute('PRAGMA wal_checkpoint(PASSIVE)'); c.close()\""
   scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
   ```

2. **Run quick queries** (single sqlite3 session, NOT full audit scripts):

   ```sql
   -- Last 4 hours activity summary
   SELECT
     COALESCE(product_type, '15m') as system,
     COUNT(*) as total_evals,
     SUM(CASE WHEN filter_stage='candidate' THEN 1 ELSE 0 END) as candidates,
     SUM(CASE WHEN filter_stage='observation_trade' THEN 1 ELSE 0 END) as observations,
     MAX(evaluation_time) as latest_eval
   FROM evaluated_opportunities
   WHERE evaluation_time > datetime('now', '-4 hours')
   GROUP BY COALESCE(product_type, '15m');

   -- Recent trades
   SELECT ticker, result, entry_price, pnl_cents, settled_time
   FROM settled_trades
   WHERE settled_time > datetime('now', '-4 hours')
   ORDER BY settled_time DESC;

   -- Shadow variant counts (last 24h)
   SELECT approach, asset, COUNT(*) as n,
     SUM(CASE WHEN settled=1 AND result='win' THEN 1 ELSE 0 END) as wins,
     SUM(CASE WHEN settled=1 AND result='loss' THEN 1 ELSE 0 END) as losses,
     SUM(CASE WHEN settled=0 THEN 1 ELSE 0 END) as pending
   FROM fifteenm_shadow_signals
   WHERE created_at > datetime('now', '-24 hours')
   GROUP BY approach, asset;

   -- NO-side recent signals
   SELECT COUNT(*) as no_signals,
     SUM(CASE WHEN settled=1 THEN 1 ELSE 0 END) as settled,
     MAX(evaluation_time) as latest
   FROM evaluated_opportunities
   WHERE side='no' AND evaluation_time > datetime('now', '-6 hours');

   -- Bot health: is it scanning?
   SELECT COUNT(*) as recent_evals
   FROM evaluated_opportunities
   WHERE evaluation_time > datetime('now', '-30 minutes');
   ```

3. **Present a compact summary**:

   ```
   ## Status (as of HH:MM UTC)

   | System | Last 4h | Candidates | Latest | Status |
   |--------|---------|------------|--------|--------|
   | 15M    | 42      | 3          | 5m ago | Active |
   | Hourly | 18      | 0          | 12m ago| Quiet  |
   | SPX    | 8       | 0          | 1h ago | Quiet  |

   Recent trades: 2 (1W/1L, +$4.32)
   Shadow variants: A1=142/200, A2=89/200, A3=67/100
   NO-side: 12 signals (6h), 0 settled
   Bot health: OK (scanning)
   ```

4. **Flag anything notable**:
   - No evals in 30+ min → "Bot may not be scanning"
   - No candidates in 4h+ during market hours → "Markets quiet, no edge"
   - Any system with 0 evals → note it
   - Shadow variants near promotion threshold (n>180) → highlight

## If argument is specific system
- `15m`: Focus on 15M trades, candidates, shadow variants, NO-side
- `hourly`: Focus on hourly observations, alt strategies A/B, CalEngine
- `no-side`: Run `python3 scripts/no_side_status.py --db /tmp/state.db` for full NO report
- `variants`: Focus on fifteenm_shadow_signals progress per approach

## IMPORTANT
- This is a QUICK check — don't run full audit scripts unless the user asks for `/audit`
- Always checkpoint WAL before SCP
- Use `/tmp/state.db` — never query VPS directly
- If something looks broken, say so and offer to investigate with `/investigate`
