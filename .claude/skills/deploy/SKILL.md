---
name: deploy
description: "Push to main and verify VPS auto-deploy — syntax check, constant validation, service restart, DB entry verification. Use when: \"deploy\", \"push it\", \"deploy this change\""
---

# Deploy to VPS

Push to main and verify the bot is running correctly on VPS after auto-deploy.

## Usage
```
/deploy
```

## Pre-Deploy Checklist (ALL must pass before pushing)

1. **Syntax check**:
   ```bash
   python3 -c "import ast; ast.parse(open('bot.py').read())"
   ```

2. **Verify critical trading constants unchanged** (unless the change IS a constant change):
   ```bash
   grep -n "^OBSERVATION_MODE\|^MAX_SECONDS_BEFORE_CLOSE\|^MIN_ENTRY_PRICE\|^MAX_ENTRY_PRICE\|^HOURLY_OBSERVATION_ONLY\|^SPX_HOURLY_OBSERVATION_ONLY\|^WEATHER_OBSERVATION_ONLY" bot.py
   ```
   Expected: OBSERVATION_MODE=False, MIN_ENTRY_PRICE=80, MAX_ENTRY_PRICE=99, MAX_SECONDS_BEFORE_CLOSE=900, all observation modes True.

3. **If any constant changed in bot.py**: grep the constant name across ALL files, especially `market_config.py`. Mismatch = crash loop on VPS.
   ```bash
   grep -rn "CONSTANT_NAME" bot.py market_config.py dashboard_snapshot.py
   ```

4. **If any function signature changed**: grep all call sites and verify callers pass new params.

5. **Run regression tests** (the ones that don't need `requests`):
   ```bash
   python3 -m pytest tests/test_regression.py::TestInstrumentationIntegrity tests/test_regression.py::TestSyntaxCheck tests/test_regression.py::TestCalibrationPipeline -v 2>&1
   ```

6. **Show the user a change summary** with:
   - Files changed
   - What the change does (1-2 sentences)
   - Any config/constant changes
   - Risk level (low/medium/high)

7. **Wait for explicit user confirmation** before pushing. NEVER auto-push.

## Deploy Steps

1. **Push to main**:
   ```bash
   git push origin main
   ```

2. **Wait for GitHub Actions deploy** (~30-60 seconds):
   ```bash
   sleep 30
   ```

3. **Verify VPS pulled the latest commit**:
   ```bash
   ssh botuser@45.55.181.30 "cd ~/kalshi-bot-repo && git log --oneline -1"
   ```
   Must match the local commit hash. If it doesn't match after 60s, the deploy may have failed — check GitHub Actions.

4. **Verify bot service is running**:
   ```bash
   ssh botuser@45.55.181.30 "systemctl is-active kalshi-bot && journalctl -u kalshi-bot --no-pager -n 20 --since '1 min ago'"
   ```

5. **Check for startup errors** (crash loops, import errors, assertion failures):
   ```bash
   ssh botuser@45.55.181.30 "journalctl -u kalshi-bot --no-pager -n 50 --since '2 min ago' | grep -iE 'error|exception|traceback|assert|crash|restart'"
   ```

6. **Verify bot is scanning** (look for recent scan activity):
   ```bash
   ssh botuser@45.55.181.30 "journalctl -u kalshi-bot --no-pager -n 10 --since '1 min ago' | tail -5"
   ```

7. **Post-deploy data validation** — if the change affects DB writes, verify expected entries are being created:
   ```bash
   ssh botuser@45.55.181.30 "cd ~/kalshi-bot-repo && python3 -c \"
   import sqlite3
   c = sqlite3.connect('state.db')
   c.execute('PRAGMA busy_timeout=5000')
   # Adjust query based on what changed
   r = c.execute('SELECT COUNT(*) FROM evaluated_opportunities WHERE evaluation_time > datetime(\\\"now\\\", \\\"-2 minutes\\\")').fetchone()
   print(f'Evals in last 2 min: {r[0]}')
   c.close()
   \""
   ```

## Post-Deploy Report

```
## Deploy Report

- **Commit:** abc1234 — "feat: add XYZ"
- **VPS status:** Running (active since HH:MM UTC)
- **Startup errors:** None
- **Scanning:** Yes (N evals in last 2 min)
- **Data validation:** OK (expected rows being created)
- **Result:** DEPLOY SUCCESS
```

## Rollback Procedure

If the deploy goes wrong, follow this procedure based on severity:

### Severity 1: Bot is crash-looping (restarts every few seconds)
This is the most urgent — the bot can't trade and may spam APIs.
```bash
# 1. Stop the bot immediately to prevent API spam
ssh botuser@45.55.181.30 "sudo systemctl stop kalshi-bot"
# 2. Revert the commit locally
git revert HEAD
# 3. Push the revert (triggers new deploy)
git push origin main
# 4. Wait for deploy, then start the bot
sleep 30
ssh botuser@45.55.181.30 "sudo systemctl start kalshi-bot"
# 5. Verify it's stable
ssh botuser@45.55.181.30 "journalctl -u kalshi-bot --no-pager -n 20 --since '1 min ago'"
```

### Severity 2: Bot is running but behaving wrong (bad trades, wrong config)
The bot is stable but doing the wrong thing — still urgent but not crashing.
```bash
# 1. Stop the bot to prevent further bad trades
ssh botuser@45.55.181.30 "sudo systemctl stop kalshi-bot"
# 2. Investigate what went wrong (check logs, DB)
# 3. Either fix forward (new commit) or revert
git revert HEAD
git push origin main
sleep 30
ssh botuser@45.55.181.30 "sudo systemctl start kalshi-bot"
```

### Severity 3: Non-critical issue (wrong logging, dashboard broken, shadow not recording)
Bot is trading correctly but something ancillary is broken. No need to stop the bot.
```bash
# Fix forward — make a new commit with the fix
# No need to revert or stop the bot
```

**Always get user confirmation before stopping the bot or reverting.**

## Error Handling

| Situation | Action |
|-----------|--------|
| `git push` rejected (not fast-forward) | Someone else pushed. Run `git pull --rebase origin main`, resolve any conflicts, then push again. |
| GitHub Actions deploy fails | Check: `gh run list --limit 1`. Show the failure reason. The VPS still has the old code — no damage done. Fix and re-push. |
| VPS unreachable after push | Code was pushed but can't verify. Wait 2 minutes, retry SSH. If still unreachable, check DigitalOcean console. The deploy script on VPS will auto-pull on next boot. |
| Commit hash doesn't match after 60s | Deploy script may have failed. SSH in and check: `cd ~/kalshi-bot-repo && git status && git log --oneline -1`. May need manual `git pull`. |
| Bot starts but shows `validate_market_configs` assertion error | A constant was changed in bot.py but not in market_config.py. This is a Severity 1 crash loop — follow rollback procedure immediately. |
| Tests pass locally but bot crashes on VPS | Likely a missing dependency or environment difference. Check the error in logs. Common: missing pip package in venv, different Python version, missing .env variable. |

## IMPORTANT
- NEVER push without explicit user confirmation
- Pushing to main auto-deploys — there is no staging environment
- The bot is trading real money — every deploy is high stakes
- If unsure about severity, default to stopping the bot first (Severity 1 procedure) — lost trading time is better than lost money
