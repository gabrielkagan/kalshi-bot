# Data Health Check

Check instrumentation quality, data gaps, NULL rates, and shadow coverage across all systems.

## When to use
- "Fix data gaps"
- "Make instrumentation more robust"
- "Is data flowing correctly"
- "Check shadow column coverage"
- Any data quality concern

## Usage
```
/data-health
/data-health 15m
/data-health weather
```

## Steps

1. **Checkpoint WAL + copy fresh state.db from VPS**:
   ```
   ssh botuser@45.55.181.30 "cd ~/kalshi-bot-repo && python3 -c \"import sqlite3; c=sqlite3.connect('state.db'); c.execute('PRAGMA wal_checkpoint(PASSIVE)'); c.close()\""
   scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
   ```

2. **Run the data health monitor script**:
   ```
   python3 scripts/data_health_monitor.py --db /tmp/state.db --verbose 2>&1
   ```

3. **If script not available, run these queries manually**:

   ```sql
   -- NULL rate check per product_type (last 24h)
   SELECT
     COALESCE(product_type, '15m') as system,
     COUNT(*) as total,
     SUM(CASE WHEN raw_prob IS NULL THEN 1 ELSE 0 END) as null_raw_prob,
     SUM(CASE WHEN entry_price IS NULL THEN 1 ELSE 0 END) as null_price,
     SUM(CASE WHEN position_size IS NULL THEN 1 ELSE 0 END) as null_size,
     SUM(CASE WHEN egarch_sigma IS NULL THEN 1 ELSE 0 END) as null_egarch,
     SUM(CASE WHEN rk_sigma IS NULL THEN 1 ELSE 0 END) as null_rk,
     ROUND(100.0 * SUM(CASE WHEN raw_prob IS NULL THEN 1 ELSE 0 END) / COUNT(*), 1) as pct_null_raw_prob
   FROM evaluated_opportunities
   WHERE evaluation_time > datetime('now', '-24 hours')
   GROUP BY COALESCE(product_type, '15m');

   -- Shadow variant data accumulation
   SELECT approach, COUNT(*) as n,
     SUM(CASE WHEN settled=1 THEN 1 ELSE 0 END) as settled,
     MIN(created_at) as first_signal,
     MAX(created_at) as latest_signal
   FROM fifteenm_shadow_signals
   GROUP BY approach;

   -- Data freshness per system
   SELECT
     COALESCE(product_type, '15m') as system,
     MAX(evaluation_time) as latest,
     COUNT(*) as last_hour_count
   FROM evaluated_opportunities
   WHERE evaluation_time > datetime('now', '-1 hour')
   GROUP BY COALESCE(product_type, '15m');

   -- NO-side data flow
   SELECT
     side,
     COUNT(*) as total,
     SUM(CASE WHEN evaluation_time > datetime('now', '-6 hours') THEN 1 ELSE 0 END) as last_6h,
     MAX(evaluation_time) as latest
   FROM evaluated_opportunities
   WHERE side IS NOT NULL
   GROUP BY side;

   -- Settled trades with missing fields
   SELECT COUNT(*) as total_settled,
     SUM(CASE WHEN escalation_type IS NULL THEN 1 ELSE 0 END) as null_escalation,
     SUM(CASE WHEN fill_latency IS NULL THEN 1 ELSE 0 END) as null_fill_latency
   FROM settled_trades;
   ```

4. **Present a health report**:

   ```
   ## Data Health Report

   ### NULL Rates (last 24h)
   | System  | Evals | raw_prob | egarch | rk    | size  | Status |
   |---------|-------|----------|--------|-------|-------|--------|
   | 15M     | 342   | 0%       | 2%     | 0%    | 0%    | OK     |
   | Hourly  | 128   | 45%      | 0%     | 0%    | 12%   | WARN   |

   ### Shadow Variant Progress
   | Variant | Total | Settled | Target | Progress |
   |---------|-------|---------|--------|----------|
   | A1      | 142   | 89      | 200    | 71%      |
   | A2      | 89    | 45      | 200    | 45%      |
   | A3      | 67    | 34      | 100    | 67%      |

   ### Data Freshness
   | System  | Latest Eval      | Last Hour | Status   |
   |---------|------------------|-----------|----------|
   | 15M     | 2 min ago        | 42        | Active   |
   | Hourly  | 8 min ago        | 18        | Active   |
   | Weather | 45 min ago       | 2         | Slow     |

   ### Issues Found
   1. [WARN] Hourly raw_prob 45% NULL — check CalEngine INSERT path
   2. [OK] All other systems healthy
   ```

5. **For each issue found**, trace the code path:
   - Which INSERT statement writes to this column?
   - Is the value being computed upstream?
   - Is there a try/except swallowing an error?

## IMPORTANT
- Always checkpoint WAL before SCP
- Use `/tmp/state.db` — never query VPS directly
- NULL rates >20% are WARNINGS, >50% are CRITICAL
- When investigating a NULL gap, always check if it's "never been set" vs "recently broke"
- If a fix is needed, present it with syntax check before deploying
