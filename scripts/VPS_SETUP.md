# VPS Cron & MCP Server Setup

## Cron Jobs

Add to botuser's crontab (`crontab -e`):

```cron
# Data health monitor — every 30 min, sends Telegram for critical issues
*/30 * * * * cd /home/botuser/kalshi-bot-repo && /home/botuser/kalshi-bot-repo/venv/bin/python scripts/audit/data_health_monitor.py --db state.db --telegram >> /tmp/data_health.log 2>&1

# Quiet market monitor — every 15 min, alerts when 15M goes unusually silent
*/15 * * * * cd /home/botuser/kalshi-bot-repo && /home/botuser/kalshi-bot-repo/venv/bin/python scripts/audit/quiet_market_monitor.py --db state.db >> /tmp/quiet_market.log 2>&1
```

## MCP Server Setup

1. Install mcp package on VPS:
   ```
   source venv/bin/activate
   pip install mcp
   ```

2. Add to Claude Code's MCP config (`~/.claude/mcp_servers.json` on your Mac):
   ```json
   {
     "kalshi-vps": {
       "type": "stdio",
       "command": "ssh",
       "args": [
         "botuser@45.55.181.30",
         "cd /home/botuser/kalshi-bot-repo && source venv/bin/activate && python scripts/vps_mcp_server.py"
       ]
     }
   }
   ```

3. Restart Claude Code to pick up the new MCP server.

4. Available tools:
   - `query_db(sql)` — Read-only SQL against state.db
   - `get_recent_logs(lines, since_minutes, grep_pattern)` — journalctl output
   - `get_bot_status()` — systemd state, commit, uptime, balance
   - `get_data_health()` — eval counts, NULL rates, latest timestamps, errors
   - `checkpoint_wal()` — WAL checkpoint before queries
