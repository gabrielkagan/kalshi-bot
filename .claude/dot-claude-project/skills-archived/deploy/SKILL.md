# Deploy and Verify

Deploy the latest changes to the VPS and verify everything is healthy.

## Steps

1. **Pre-flight checks**
   - Run `python3 -c "import ast; ast.parse(open('bot.py').read())"` — abort if syntax fails
   - Verify critical constants unchanged (grep bot.py for OBSERVATION_MODE, MIN_ENTRY_PRICE, MAX_SECONDS_BEFORE_CLOSE) — show values to user
   - If bot.py was modified: grep for all `_shadow_diag` keys and confirm they exist in DB insert signatures

2. **Commit and push**
   - `git status` to review changes
   - Stage relevant files (NEVER `.env` or `*.jsonl`)
   - Commit with descriptive message
   - `git push origin main`

3. **Verify on VPS**
   - SSH: `ssh botuser@45.55.181.30`
   - Check commit: `cd ~/kalshi-bot-repo && git log --oneline -1`
   - If stale, pull manually: `git pull origin main`
   - Verify commit hash matches what we just pushed

4. **Verify bot is running**
   - `systemctl status kalshi-bot 2>&1 | head -15` — confirm Active: active (running)
   - Confirm PID is fresh (uptime < 2 min = restarted)
   - If uptime is old and bot.py changed, the deploy didn't restart — flag to user

5. **Log health check** (wait ~20s after restart)
   - `journalctl -u kalshi-bot --since '1 min ago' --no-pager 2>/dev/null | grep -c ERROR` — should be 0
   - `journalctl -u kalshi-bot --since '1 min ago' --no-pager 2>/dev/null | tail -10` — show recent activity

6. **Report**
   - Commit hash deployed
   - Bot status (running/crashed/not restarted)
   - Error count
   - Memory usage
