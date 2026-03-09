---
name: data-health
description: "Check instrumentation quality — NULL rates, data gaps, shadow coverage, data freshness across all systems. Use when: \"fix data gaps\", \"is data flowing?\", \"check shadow column coverage\", \"data quality check\", \"are we missing rows?\""
---

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

1. **Sync the database.** Follow `.claude/skills/references/db-sync.md` to sync the database.

2. **Run the data health monitor script** (if available):
   ```bash
   python3 scripts/data_health_monitor.py --db /tmp/state.db --verbose 2>&1
   ```

3. **If script not available, run these queries manually**:

   ```sql
   PRAGMA busy_timeout=10000;

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
   ## Data Health Report (as of HH:MM UTC)

   ### NULL Rates (last 24h)
   | System  | Evals | raw_prob | egarch | rk    | size  | Status   |
   |---------|-------|----------|--------|-------|-------|----------|
   | 15M     | 342   | 0%       | 2%     | 0%    | 0%    | OK       |
   | Hourly  | 128   | 45%      | 0%     | 0%    | 12%   | WARN     |
   | SPX     | 48    | 0%       | 5%     | 0%    | 0%    | OK       |
   | Weather | 89    | 100%     | N/A    | N/A   | 0%    | EXPECTED |
   | Sports  | 34    | 15%      | N/A    | N/A   | 8%    | WARN     |

   ### Shadow Variant Progress
   | Variant | Total | Settled | Target | Progress |
   |---------|-------|---------|--------|----------|
   | A1      | 142   | 89      | 200    | 44.5%    |
   | A2      | 89    | 45      | 200    | 22.5%    |
   | A3      | 67    | 34      | 100    | 34.0%    |

   ### Data Freshness
   | System  | Latest Eval      | Last Hour | Status   |
   |---------|------------------|-----------|----------|
   | 15M     | 2 min ago        | 42        | Active   |
   | Hourly  | 8 min ago        | 18        | Active   |
   | Weather | 45 min ago       | 2         | Slow     |
   | Sports  | 3h ago           | 0         | Inactive |

   ### Issues Found
   1. [WARN] Hourly raw_prob 45% NULL — CalEngine disabled, expected for passthrough path
   2. [WARN] Sports raw_prob 15% NULL — check sports_engine INSERT
   3. [OK] All other systems healthy
   ```

5. **For each issue found**, trace the code path:
   - Which INSERT statement writes to this column?
   - Is the value being computed upstream?
   - Is there a try/except swallowing an error?

## WHY these NULL rate thresholds

| Threshold | Level | WHY |
|-----------|-------|-----|
| 0-5% NULL | OK | Minor gaps are normal — race conditions, edge cases where a value isn't computed (e.g., EGARCH hasn't converged yet). Not actionable. |
| 5-20% NULL | MONITOR | Something is intermittently failing. Common causes: a try/except swallowing errors, a conditional branch that skips the assignment, or a timing issue where the value isn't ready. Worth investigating but not urgent. |
| 20-50% NULL | WARN | A significant fraction of data is missing. This affects audit quality — any analysis using this column is biased toward the rows that DO have it. Root-cause and fix. |
| >50% NULL | CRITICAL | More data is missing than present. The column is effectively broken. Possible causes: INSERT doesn't set it, the computation always fails, or the column was added after most existing rows. Check if it's "never been set" (old rows) vs "recently broke" (regression). |
| 100% NULL on weather/sports-specific columns | EXPECTED | Weather uses `wx_*` columns, sports uses sport-specific columns. Crypto systems won't have these. Only flag 100% NULL for columns that SHOULD be populated for that product_type. |

**"Never been set" vs "recently broke":** Query the first non-NULL row: `SELECT MIN(evaluation_time) FROM evaluated_opportunities WHERE <column> IS NOT NULL AND product_type='<type>'`. If it's recent, the column was just added. If it's old, the column broke recently.

## Error Handling

| Situation | Action |
|-----------|--------|
| `data_health_monitor.py` not found | Fall back to manual SQL queries (step 3). This is fine — the queries are the same ones the script runs. |
| Query errors with `no such column` | The column may not exist in this DB version. Run `PRAGMA table_info(evaluated_opportunities)` to see actual columns. Skip missing columns in the report. |
| A system shows 0 evals in last 24h | Not necessarily broken — check if it's a weekend (no SPX), no games (sports), or a system that was recently deployed. Report: "0 evals — system may be inactive or newly deployed." |
| fifteenm_shadow_signals table missing | Shadow engine hasn't initialized. Report: "Shadow table not created yet — engine may not have run." |
| NULL rate suddenly jumps from 0% to 40%+ | Regression — something broke. Check recent commits: `git log --oneline -5`. Compare the column's NULL rate for `evaluation_time > datetime('now', '-2 hours')` vs `evaluation_time BETWEEN datetime('now', '-48 hours') AND datetime('now', '-2 hours')` to pinpoint when it broke. |
| All systems show "Active" but user reports data issues | The freshness check only verifies evals exist. Check SPECIFIC columns the user is asking about — a system can be "active" (writing rows) but writing NULLs to critical columns. |

## IMPORTANT
- Use `/tmp/state.db` — never query VPS directly
- When investigating a NULL gap, always check if it's "never been set" vs "recently broke" (see WHY section)
- If a fix is needed, present it with syntax check before deploying
- Don't alarm on expected NULLs (weather columns on crypto rows, sport columns on crypto rows)
- **Cross-reference with product_type** — a NULL that's normal for weather is a bug for 15M
