# Deploy to VPS

Push to main and verify the bot is running correctly on VPS after auto-deploy.

## Usage
```
/deploy
```

## Pre-Deploy Checklist (ALL must pass before pushing)

1. **Syntax check**:
   ```
   python3 -c "import ast; ast.parse(open('bot.py').read())"
   ```

2. **Verify critical trading constants unchanged** (unless the change IS a constant change):
   ```
   grep -n "^OBSERVATION_MODE\|^MAX_SECONDS_BEFORE_CLOSE\|^MIN_ENTRY_PRICE\|^MAX_ENTRY_PRICE\|^HOURLY_OBSERVATION_ONLY\|^SPX_HOURLY_OBSERVATION_ONLY\|^WEATHER_OBSERVATION_ONLY" bot.py
   ```
   Expected: OBSERVATION_MODE=False, MIN_ENTRY_PRICE=86, MAX_ENTRY_PRICE=99, MAX_SECONDS_BEFORE_CLOSE=900, all observation modes True.

3. **If any constant changed in bot.py**: grep the constant name across ALL files, especially `market_config.py`. Mismatch = crash loop on VPS.
   ```
   grep -rn "CONSTANT_NAME" bot.py market_config.py firebase_push.py
   ```

4. **If any function signature changed**: grep all call sites and verify callers pass new params.

5. **Run regression tests** (the ones that don't need `requests`):
   ```
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
   ```
   git push origin main
   ```

2. **Wait for GitHub Actions deploy** (~30-60 seconds):
   ```
   sleep 30
   ```

3. **Verify VPS pulled the latest commit**:
   ```
   ssh botuser@45.55.181.30 "cd ~/kalshi-bot-repo && git log --oneline -1"
   ```
   Must match the local commit hash.

4. **Verify bot service is running**:
   ```
   ssh botuser@45.55.181.30 "systemctl is-active kalshi-bot && journalctl -u kalshi-bot --no-pager -n 20 --since '1 min ago'"
   ```

5. **Check for startup errors** (crash loops, import errors, assertion failures):
   ```
   ssh botuser@45.55.181.30 "journalctl -u kalshi-bot --no-pager -n 50 --since '2 min ago' | grep -iE 'error|exception|traceback|assert|crash|restart'"
   ```

6. **Verify bot is scanning** (look for recent scan activity):
   ```
   ssh botuser@45.55.181.30 "journalctl -u kalshi-bot --no-pager -n 10 --since '1 min ago' | tail -5"
   ```

7. **Post-deploy data validation** — if the change affects DB writes, verify expected entries are being created:
   ```
   ssh botuser@45.55.181.30 "cd ~/kalshi-bot-repo && python3 -c \"
   import sqlite3
   c = sqlite3.connect('state.db')
   c.execute('PRAGMA busy_timeout=5000')
   # Adjust query based on what changed
   r = c.execute('SELECT COUNT(*) FROM evaluated_opportunities WHERE evaluation_time > datetime(\"now\", \"-2 minutes\")').fetchone()
   print(f'Evals in last 2 min: {r[0]}')
   c.close()
   \""
   ```

## Post-Deploy Report

Present a summary:
- Commit hash deployed
- VPS status (running/error)
- Any errors found in logs
- Data validation result (if applicable)
- Overall: DEPLOY SUCCESS or DEPLOY FAILED (with what to do)

## IMPORTANT
- NEVER push without explicit user confirmation
- If VPS shows errors after deploy, immediately show the error and suggest rollback: `git revert HEAD && git push`
- If bot enters crash loop (multiple restarts in logs), that's CRITICAL — propose immediate revert
- Pushing to main auto-deploys — there is no staging environment
- The bot is trading real money — every deploy is high stakes
