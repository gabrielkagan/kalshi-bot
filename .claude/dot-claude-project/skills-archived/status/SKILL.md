# Bot Health Status

Quick check that the bot is running healthy on VPS.

## Steps

1. **Service status**
   - `ssh botuser@45.55.181.30 "systemctl status kalshi-bot 2>&1 | head -15"`
   - Report: running/stopped, PID, uptime, memory

2. **Error check**
   - `ssh botuser@45.55.181.30 "journalctl -u kalshi-bot --since '5 min ago' --no-pager 2>/dev/null | grep -c ERROR"`
   - If errors > 0, show the error lines

3. **Trading stats** — SCP and run a script on VPS:
   ```python
   import sqlite3
   conn = sqlite3.connect('state.db')
   c = conn.cursor()
   # All-time 15M
   r = c.execute("SELECT COUNT(*), SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END), SUM(pnl_cents) FROM settled_trades WHERE event_ticker LIKE '%15M%'").fetchone()
   print(f'15M all-time: {r[0]} trades, {r[1]}W/{r[0]-r[1]}L, PnL=${r[2]/100:.2f}')
   # Today
   t = c.execute("SELECT COUNT(*), SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END), SUM(pnl_cents) FROM settled_trades WHERE event_ticker LIKE '%15M%' AND settled_at >= date('now')").fetchone()
   print(f'Today 15M: {t[0]} trades, {(t[1] or 0)}W/{t[0]-(t[1] or 0)}L, PnL=${(t[2] or 0)/100:.2f}')
   # Pipeline last hour
   p = c.execute("SELECT filter_stage, COUNT(*) FROM evaluated_opportunities WHERE (product_type IS NULL OR product_type != 'hourly') AND evaluation_time > datetime('now', '-1 hour') GROUP BY filter_stage ORDER BY COUNT(*) DESC").fetchall()
   print('Pipeline (last 1h):')
   for row in p: print(f'  {row[0]}: {row[1]}')
   conn.close()
   ```
   Write the script to /tmp, SCP to VPS, run, delete.

4. **Report** — concise 5-line summary:
   - Service: running/stopped (uptime)
   - Errors: count in last 5 min
   - 15M today: W/L, PnL
   - 15M all-time: W/L, PnL
   - Pipeline: candidate count last 1h
