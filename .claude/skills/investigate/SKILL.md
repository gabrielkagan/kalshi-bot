---
name: investigate
description: "Emergency investigation — anomaly, unexpected trade, dashboard alert, or suspected bug. Use when: \"something looks wrong\", \"investigate this trade\", \"why did we take a trade at X cents?\", \"I saw this error\", \"treat this as an emergency\""
---

# Emergency Investigation

Investigate an anomaly, unexpected trade, dashboard alert, or suspected bug. Answer first with data, then root cause, then fix options.

## When to use
- "Something looks wrong"
- "Investigate this trade"
- "I saw this error on the dashboard"
- "Treat this as an emergency"
- "Why did we take a trade at X cents"
- Any anomaly or suspected bug

## Usage
```
/investigate <description of anomaly>
```

## Steps

1. **Sync the database.** Follow `.claude/skills/references/db-sync.md` to sync the database. For investigations, ALWAYS sync fresh — never reuse a cached copy. Pass "fresh" mentally.

2. **Gather evidence** — run ALL of these in parallel:

   a. **Check recent VPS logs for errors**:
   ```bash
   ssh botuser@45.55.181.30 "journalctl -u kalshi-bot --no-pager -n 100 --since '30 min ago' | grep -iE 'error|exception|traceback|warning|CRITICAL'"
   ```

   b. **Check dashboard health alerts**:
   ```bash
   ssh botuser@45.55.181.30 "journalctl -u kalshi-bot --no-pager -n 200 --since '30 min ago' | grep -iE 'health|alert|failing|broken'"
   ```

   c. **Query the specific anomaly** in `/tmp/state.db`:
   - If it's a trade: `SELECT * FROM settled_trades WHERE ticker LIKE '%<ticker>%' ORDER BY settled_at DESC LIMIT 5`
   - If it's a missed opportunity: `SELECT * FROM evaluated_opportunities WHERE ticker LIKE '%<ticker>%' ORDER BY evaluation_time DESC LIMIT 10`
   - If it's a rejection: `SELECT * FROM rejected_opportunities WHERE ticker LIKE '%<ticker>%' ORDER BY rejection_time DESC LIMIT 10`
   - If it's a data gap: check NULL rates on relevant columns
   - If the anomaly description is vague, start broad: query last 10 settled trades, last 10 evaluated opportunities, look for anything unusual

   d. **Check bot config is correct on VPS** (not local — configs may have drifted):
   ```bash
   ssh botuser@45.55.181.30 "cd ~/kalshi-bot-repo && grep -n 'OBSERVATION_MODE\|MIN_ENTRY_PRICE\|MAX_ENTRY_PRICE\|STC_SHADOW_THRESHOLD' bot.py | head -10"
   ```

3. **Present findings immediately** — data first, not speculation:

   ```
   ## Investigation: <anomaly description>

   ### Evidence
   - Trade KXBTC15M-26MAR091430-B97500 entered at 93c, settled YES → WIN +$7.00
   - Bot logs show normal maker→fill sequence, no errors
   - entry_price_cents=93, market_result=yes, pnl_cents=700

   ### Root Cause
   This trade was correct — the bot entered at 93c on a YES above $97,500,
   BTC was at $97,823 at settlement. No anomaly found.

   ### Impact
   No impact — trade was profitable and correctly executed.

   ### Conclusion
   FALSE ALARM — no action needed. The trade followed normal pipeline logic.
   ```

4. **If the investigation finds a real problem**, present fix options:
   ```
   ### Fix Options
   1. [Hotfix — description, risk: low/medium/high, deploys immediately]
   2. [Config change — description, risk level, requires restart]
   3. [Monitor — watch for recurrence, no code change needed]

   ### Prevention
   [Regression test or check that would catch this in the future]
   ```

5. **If it's an active emergency** (bot doing something wrong RIGHT NOW):
   - Check if bot needs to be stopped: `ssh botuser@45.55.181.30 "systemctl status kalshi-bot"`
   - If actively losing money or entering bad trades, suggest stopping: `sudo systemctl stop kalshi-bot`
   - **Never stop the bot without user confirmation**
   - After stopping, investigate at leisure — the bot won't trade while stopped

## When the emergency turns out to be nothing

This is common and expected. Don't pad the response with unnecessary analysis. The correct response is:

1. Show the evidence that disproves the concern
2. Explain WHY it looked suspicious but isn't (e.g., "93c looks high but the model had 96.2% calibrated probability, edge was 3.2%")
3. Say "FALSE ALARM — no action needed" clearly
4. Don't offer fix options for non-problems — that creates anxiety

**Common false alarms:**
- Trade at a price that "looks high" → check if edge and probability justified it
- Gap in trading activity → check if it's overnight, weekend, or no markets were eligible
- Dashboard showing stale data → check if supabase_sync is running, not the bot itself
- PnL dip → check if it's a single loss within normal variance (Kelly sizing means individual losses happen)

## Error Handling

| Situation | Action |
|-----------|--------|
| VPS unreachable | Tell user immediately: "Can't reach VPS — unable to check live logs or config." Use cached `/tmp/state.db` if available for DB queries, but warn that data may be stale. Suggest checking DigitalOcean console. |
| Logs show no errors but anomaly is real | The bug may be a silent failure (swallowed exception). Check for `except Exception` patterns in the relevant code path. Query DB for missing rows that should exist. |
| Can't find the ticker in any table | The market may not have been scanned yet, or the ticker format may be wrong. Check `evaluated_opportunities` and `rejected_opportunities` with `LIKE '%partial_ticker%'`. Also check if the market's product_type is one the bot tracks. |
| VPS config doesn't match local | This means the deploy didn't land or there's a git conflict. Show the diff and treat as HIGH priority — the bot may be running old code. |

## IMPORTANT
- **Answer first, investigate second** — give the user an immediate read on what you see, then dig deeper
- **No assumptions** — query actual data before explaining. Never guess column values or schema.
- **Check the actual config on VPS**, not just local — configs may have drifted
- **Every real bug must end with a prevention recommendation** — "why did this happen and how do we prevent it"
- **False alarms get a clear "FALSE ALARM" label** — don't hedge or suggest unnecessary follow-ups
- If the fix involves code changes, always syntax-check and present for review before deploying
- If root cause reveals a bug class, add a regression test
