# VPS Cron & MCP Server Setup

## Cron Jobs

Add to botuser's crontab (`crontab -e`).

**IMPORTANT:** Telegram-alerting scripts must source `~/.env` to pick up
`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`. Without the env load the script
runs without crashing but silently skips all alerts (see
`data_health_monitor.send_telegram`: "No Telegram config, skipping alert.").
This is a separate latent failure class from the path drift that caused
the 2026-05-17 data_health_monitor incident (ticket 86ba0xmmq, where the
live VPS cron line referenced the pre-Bit-11.2 path
`scripts/data_health_monitor.py` after the script was relocated to
`scripts/audit/`). The env-loading pattern below guards against both classes.

Pinned by `tests/contracts/test_vps_setup_cron_paths.py` — every Python
script referenced below must exist at the documented path in the repo.

```cron
# Data health monitor — every 30 min, sends Telegram for critical issues
*/30 * * * * cd ~/kalshi-bot-repo && source venv/bin/activate && set -a && source ~/.env && set +a && python3 scripts/audit/data_health_monitor.py --db state.db --telegram >> /tmp/data_health.log 2>&1

# Quiet market monitor — every 15 min, alerts when 15M goes unusually silent
*/15 * * * * cd ~/kalshi-bot-repo && source venv/bin/activate && set -a && source ~/.env && set +a && python3 scripts/audit/quiet_market_monitor.py --db state.db >> /tmp/quiet_market.log 2>&1

# Monitor-the-monitor (E.1, ticket 86ba0xq51) — every 10 min, alerts on stale cron monitor logs
*/10 * * * * cd ~/kalshi-bot-repo && source venv/bin/activate && set -a && source ~/.env && set +a && python3 scripts/ops/monitor_watchdog.py >> ~/monitor_watchdog.log 2>&1

# Autoalpha Phase 1 (ticket TBD, 2026-05-19) — daily at 13:30 UTC, 23 min after cohort_attribution_nightly.py (13:07 UTC). Reads cohort_attribution_daily, emits promote/demote recommendations to kb/findings/, Telegram-alerts top 5 each.
30 13 * * * cd ~/kalshi-bot-repo && source venv/bin/activate && set -a && source ~/.env && set +a && python3 scripts/audit/autoalpha_edge_scorer.py >> ~/autoalpha.log 2>&1
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
