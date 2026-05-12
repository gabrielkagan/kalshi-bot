---
name: status
description: "Quick pulse check — is data flowing, any trades, anything notable. Not a full audit. Use when: \"how's it going?\", \"anything happening?\", \"what's happening?\", \"quick check\", \"is the bot running?\""
---

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

## Preflight

Follow `.claude/skills/references/preflight.md` substituting `<wrapper>` = `no-side` (the only Bit 11.3 wrapper /status references; other no_side path stays as direct query). Verify `/tmp/state.db` exists, `make -n no-side` parses, and the script files referenced inline (e.g., `scripts/audit/no_side_status.py`) exist (Bit 11.1d).

## Steps

1. **Sync the database.** Follow `.claude/skills/references/db-sync.md` to sync the database.

2. **Run quick queries** (single sqlite3 session, NOT full audit scripts):

   ```sql
   PRAGMA busy_timeout=10000;

   -- Last 4 hours activity summary
   -- WHY 4 hours: covers ~16 fifteen-minute windows. Long enough to see patterns,
   -- short enough that "0 candidates" is meaningful (not just overnight).
   SELECT
     COALESCE(product_type, '15m') as system,
     COUNT(*) as total_evals,
     SUM(CASE WHEN filter_stage='candidate' THEN 1 ELSE 0 END) as candidates,
     SUM(CASE WHEN filter_stage='observation_trade' THEN 1 ELSE 0 END) as observations,
     MAX(evaluation_time) as latest_eval
   FROM evaluated_opportunities
   WHERE evaluation_time > datetime('now', '-4 hours')
   GROUP BY COALESCE(product_type, '15m');

   -- Recent settled trades (last 4h)
   SELECT ticker, asset, market_result, entry_price_cents, pnl_cents, settled_at
   FROM settled_trades
   WHERE settled_at > datetime('now', '-4 hours')
   ORDER BY settled_at DESC;

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
     SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled,
     MAX(evaluation_time) as latest
   FROM evaluated_opportunities
   WHERE side='no' AND evaluation_time > datetime('now', '-6 hours');

   -- Bot health: is it scanning?
   -- WHY 30 minutes: the bot scans every ~10s, so even during quiet markets there
   -- should be rejected_opportunities or evaluated_opportunities within 30 min.
   -- Zero evals in 30 min means the bot is likely crashed or stuck.
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

   Recent trades: 2W/1L (+$4.32 net)
   Shadow variants: A1 142 settled (84.5% WR), A2 89 settled (81.0% WR)
   NO-side: 12 signals (6h), 4 settled
   Bot health: OK (scanning, last eval 2m ago)
   ```

4. **Flag anything notable**:
   - No evals in 30+ min → "Bot may not be scanning — investigate?"
   - No candidates in 4h+ during market hours (Mon-Fri 9am-4pm ET) → "Markets quiet, no edge found"
   - Any system with 0 evals when it should be active → note it
   - Shadow variants near promotion threshold (n > 180 of 200 needed) → highlight with countdown

## If argument is specific system
- `15m`: Focus on 15M trades, candidates, shadow variants, NO-side
- `hourly`: Focus on hourly observations, alt strategies, CalEngine status
- `no-side`: Run `make no-side` (Bit 11.3 wraps `python3 scripts/audit/no_side_status.py --db /tmp/state.db`) for full NO report
- `variants`: Focus on fifteenm_shadow_signals progress per approach — show settled count vs promotion threshold

## Error Handling

| Situation | Action |
|-----------|--------|
| VPS unreachable (SSH fails) | Try MCP fallback: `mcp__kalshi-vps__get_bot_status`. If that also fails, tell user "VPS is unreachable — can't get status." |
| DB copy is stale (latest eval > 1h old during market hours) | Flag: "DB looks stale — latest eval is Xh ago. Bot may be down." Offer `/investigate`. |
| Query returns 0 rows for all systems | Don't say "everything is fine." Say: "No evaluations found in the last 4 hours. This is unusual during market hours." Check if it's a weekend/holiday or if the bot is down. |
| Shadow query fails (table doesn't exist) | Skip that section, note "fifteenm_shadow_signals table not found — shadow engine may not be initialized yet." |

## IMPORTANT
- This is a QUICK check — don't run full audit scripts unless the user asks for `/audit`
- Use `/tmp/state.db` — never query VPS directly (avoids busy_timeout contention with live bot)
- If something looks broken, say so and offer to investigate with `/investigate`
- **Don't reassure when data is missing.** Zero rows is not "quiet" — it might be a crash. Always check the bot health query before concluding "nothing happened."
