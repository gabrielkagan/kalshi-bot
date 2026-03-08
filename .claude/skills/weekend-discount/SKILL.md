# Weekend Edge Discount Shadow Audit

Evaluate the weekend edge discount shadow strategy performance and graduation readiness.

## Usage
```
/weekend-discount
/weekend-discount fresh    # Force fresh DB copy from VPS
```

## When to use
- "how's the weekend discount doing"
- "weekend discount audit"
- "should we promote the weekend discount"
- "check weekend shadow performance"
- "what's the status of all my shadow strategies" (include this in rollup)

## Steps

1. **Checkpoint WAL + copy fresh state.db from VPS** (unless recently copied or user didn't say "fresh"):
   ```
   ssh botuser@45.55.181.30 "cd ~/kalshi-bot-repo && python3 -c \"import sqlite3; c=sqlite3.connect('state.db'); c.execute('PRAGMA wal_checkpoint(PASSIVE)'); c.close()\""
   scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
   ```

2. **Run the audit script**:
   ```
   python3 scripts/weekend_discount_audit.py --db /tmp/state.db 2>&1
   ```

3. **Present findings conversationally**, focusing on:
   - **Graduation status**: YES (ready to promote) / NO (what's failing) / NEED MORE DATA (how many weekends left)
   - **Key metrics**: overall WR with CI, sim PnL, per-asset health
   - **Any asset dragging below 75%**: flag it specifically
   - **Per-weekend consistency**: is one good weekend carrying the numbers or is it stable?

4. **If user asks follow-ups**:
   - "what about just SOL?" → re-run with `--asset SOL`
   - "what if we used 0.7x instead?" → re-run with `--discount 0.7`
   - "should we promote?" → check all 4 graduation criteria explicitly

5. **If VERDICT is YES (ready to promote)**, draft the code change:
   - Apply `WEEKEND_EDGE_DISCOUNT` multiplier to the live edge check in bot.py scan loop
   - Show the exact diff before/after
   - Wait for user confirmation before deploying

## Graduation Criteria
- 60+ settled signals
- Overall WR >= 85%
- No single asset below 75% WR (with n>=5)
- No edge inversion (worst tier >= 70% WR)

## Config Reference
- `WEEKEND_EDGE_DISCOUNT = 0.60` in bot.py
- Shadow filter_stage: `weekend_discount_shadow` in evaluated_opportunities
- Active only on Saturday/Sunday (UTC weekday >= 5)
- Only applies to 15M markets (`product_type in (None, '15m')`)
- Only re-evaluates `insufficient_edge` rejections at prices >= MIN_ENTRY_PRICE (86c)

## Data Sources
- Primary: `evaluated_opportunities` table (filter_stage='weekend_discount_shadow')
- Settlement: automatic via `_poll_evaluated_opportunities()` — fills `counterfactual_pnl`
- Dashboard: `snap["weekend_discount_shadow"]` in dashboard_snapshot.py
