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
- "what's the status of all my shadow strategies" (include this in rollup)

## Steps

1. **Checkpoint WAL + copy fresh state.db from VPS** (unless recently copied or user didn't say "fresh"):
   ```
   ssh botuser@45.55.181.30 "cd ~/kalshi-bot-repo && python3 -c \"import sqlite3; c=sqlite3.connect('state.db'); c.execute('PRAGMA wal_checkpoint(PASSIVE)'); c.close()\""
   scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
   ```

2. **Run the audit script**:
   ```
   # Both sections (default)
   python3 scripts/weekend_discount_audit.py --db /tmp/state.db 2>&1

   # Weekend only
   python3 scripts/weekend_discount_audit.py --db /tmp/state.db --weekend-only 2>&1

   # Overnight only
   python3 scripts/weekend_discount_audit.py --db /tmp/state.db --overnight-only 2>&1
   ```

3. **Present findings conversationally**, focusing on:
   - **Graduation status** for each: YES / NO / NEED MORE DATA
   - **Key metrics**: overall WR with CI, sim PnL, per-asset health
   - **Any asset dragging below 75%**: flag it specifically
   - **Comparison**: are weekend and overnight tracking similarly?
   - **Expected timeline**: overnight collects ~1-2 signals/weeknight, expect 6-8 weeks to 60

4. **If user asks follow-ups**:
   - "what about just SOL?" → re-run with `--asset SOL`
   - "what if we used 0.7x instead?" → re-run with `--discount 0.7`
   - "should we promote?" → check all 4 graduation criteria explicitly for each

5. **If VERDICT is YES (ready to promote)**, draft the code change:
   - Apply the relevant discount multiplier to the live edge check in bot.py
   - Show the exact diff before/after
   - Wait for user confirmation before deploying

## Graduation Criteria (same for both)
- 60+ settled signals
- Overall WR >= 85%
- No single asset below 75% WR (with n>=5)
- No edge inversion (worst tier >= 70% WR)

## Config Reference

### Weekend
- `WEEKEND_EDGE_DISCOUNT = 0.60` in bot.py
- filter_stage: `weekend_discount_shadow`
- Active: Saturday/Sunday (UTC weekday >= 5)

### Overnight
- `OVERNIGHT_EDGE_DISCOUNT = 0.60` in bot.py
- `OVERNIGHT_QUIET_START = 4`, `OVERNIGHT_QUIET_END = 11` (UTC hours)
- filter_stage: `overnight_discount_shadow`
- Active: weekdays only, 04:00-11:00 UTC (23:00-06:00 ET)
- Skipped when weekend discount already applies (no overlap/stacking)

### Shared
- Only applies to 15M markets (`product_type in (None, '15m')`)
- Only re-evaluates `insufficient_edge` rejections at prices >= MIN_ENTRY_PRICE (86c)
- Settlement: automatic via `_poll_evaluated_opportunities()` — fills `counterfactual_pnl`
- Dashboard: `snap["weekend_discount_shadow"]` and `snap["overnight_discount_shadow"]`
