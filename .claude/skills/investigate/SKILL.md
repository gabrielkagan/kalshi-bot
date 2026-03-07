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

1. **Checkpoint WAL + copy fresh state.db from VPS** (always do this first):
   ```
   ssh botuser@45.55.181.30 "cd ~/kalshi-bot-repo && python3 -c \"import sqlite3; c=sqlite3.connect('state.db'); c.execute('PRAGMA wal_checkpoint(PASSIVE)'); c.close()\""
   scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
   ```

2. **Gather evidence** — run ALL of these in parallel:

   a. **Check recent VPS logs for errors**:
   ```
   ssh botuser@45.55.181.30 "journalctl -u kalshi-bot --no-pager -n 100 --since '30 min ago' | grep -iE 'error|exception|traceback|warning|CRITICAL'"
   ```

   b. **Check dashboard health alerts**:
   ```
   ssh botuser@45.55.181.30 "journalctl -u kalshi-bot --no-pager -n 200 --since '30 min ago' | grep -iE 'health|alert|failing|broken'"
   ```

   c. **Query the specific anomaly** in state.db:
   - If it's a trade: `SELECT * FROM settled_trades WHERE ticker LIKE '%<ticker>%' ORDER BY settled_time DESC LIMIT 5`
   - If it's a missed opportunity: `SELECT * FROM evaluated_opportunities WHERE ticker LIKE '%<ticker>%' ORDER BY evaluation_time DESC LIMIT 10`
   - If it's a rejection: `SELECT * FROM rejected_opportunities WHERE ticker LIKE '%<ticker>%' ORDER BY rejection_time DESC LIMIT 10`
   - If it's a data gap: check NULL rates on relevant columns

   d. **Check bot config is correct**:
   ```
   ssh botuser@45.55.181.30 "cd ~/kalshi-bot-repo && grep -n 'OBSERVATION_MODE\|MIN_ENTRY_PRICE\|MAX_ENTRY_PRICE\|STC_SHADOW_THRESHOLD' bot.py | head -10"
   ```

3. **Present findings immediately** — data first, not speculation:

   ```
   ## Investigation: <anomaly description>

   ### Evidence
   - [what the data shows]
   - [relevant DB rows]
   - [log entries]

   ### Root Cause
   [what actually happened and why]

   ### Impact
   [was money lost? how much? is it ongoing?]

   ### Fix Options
   1. [option A — description, risk level]
   2. [option B — description, risk level]

   ### Prevention
   [what test/check would catch this in the future]
   ```

4. **If it's an active emergency** (bot doing something wrong RIGHT NOW):
   - Check if bot needs to be stopped: `ssh botuser@45.55.181.30 "systemctl status kalshi-bot"`
   - If actively losing money, suggest stopping: `sudo systemctl stop kalshi-bot`
   - Never stop the bot without user confirmation

## IMPORTANT
- **Answer first, investigate second** — give the user an immediate read on what you see, then dig deeper
- **No assumptions** — query actual data before explaining. Never guess column values or schema.
- **Check the actual config on VPS**, not just local — configs may have drifted
- **Every investigation must end with a prevention recommendation** — "why did this happen and how do we prevent it"
- If the fix involves code changes, always syntax-check and present for review before deploying
- If root cause reveals a bug class, add a regression test
