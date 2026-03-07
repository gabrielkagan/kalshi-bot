# Run Audit Script

Run a specific audit script for a single system. Takes a system argument.

## Usage
```
/audit hourly
/audit spx
/audit weather
/audit sports
/audit 15m
/audit all
```

## Steps

1. **Parse the argument** to determine which system:
   | Argument | Script | Default args |
   |----------|--------|-------------|
   | `15m` | `scripts/15m_live_audit.py` | `--regime auto` |
   | `hourly` | `scripts/hourly_shadow_audit.py` | `--regime auto` |
   | `spx` | `scripts/spx_shadow_audit.py` | `--regime auto` |
   | `weather` | `scripts/weather_shadow_audit.py` | `--regime auto` |
   | `sports` | `scripts/sports_shadow_audit.py` | `--regime auto` |
   | `no_side` | `scripts/no_side_status.py` | `--db /tmp/state.db` |
   | `all` | Run all 6 scripts sequentially | `--regime auto` for all |

   If no argument provided, ask the user which system.
   If an additional date argument is provided (e.g., `/audit hourly 2026-03-01`), use `--since <date>` instead of `--regime auto`.

2. **Checkpoint WAL + copy fresh state.db from VPS**:
   SQLite WAL mode means recent writes live in the WAL file, not the main DB.
   Always checkpoint before copying to avoid missing data.
   ```
   ssh botuser@45.55.181.30 "cd ~/kalshi-bot-repo && python3 -c \"import sqlite3; c=sqlite3.connect('state.db'); c.execute('PRAGMA wal_checkpoint(PASSIVE)'); c.close()\""
   scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
   ```

3. **Run the audit script**:
   ```
   python3 scripts/<script> --db /tmp/state.db --regime auto 2>&1
   ```
   If user provided a date, use `--since "<date>"` instead of `--regime auto`.

4. **Present the output** — show the full script output, then add:
   - **Top 3 findings**: most actionable insights from the audit
   - **Data gaps**: any sections that show insufficient data or missing columns
   - **Recommendations**: only if supported by statistical significance (p < 0.10)

## IMPORTANT
- Always checkpoint WAL before SCP — without this, recent writes are invisible
- Always use `/tmp/state.db` — never query VPS state.db directly (avoid busy_timeout contention with live bot)
- If the script errors, show the error and check if the DB copy is stale or the script has a bug
- Present numbers with sample sizes. Small samples (n < 20) get a "NOT SIGNIFICANT" warning.
- When running `/audit all`, present a combined summary table at the end with each system's health status
- Performance analysis must filter to current config regime — don't mix data from old configs with current
- All scripts use `--regime auto` which detects the last git commit that changed relevant trading constants via git diff. No more hardcoded dates.
