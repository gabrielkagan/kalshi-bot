---
name: audit
description: "Run a specific system's audit script (15m/hourly/spx/weather/sports) with regime filtering and Wilson CIs. Use when: \"run the 15m audit script\", \"get the Wilson CIs\", \"audit numbers for hourly\", \"get me the stats\", \"run audit all\""
---

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

## Preflight

Before running this skill, verify:
- `/tmp/state.db` exists (operator must have synced via `.claude/skills/references/db-sync.md`).
- The Makefile target (if one exists, see dispatch table) parses: `make -n <target>` exits 0.
- The fallback `scripts/<script>.py` file exists if no Makefile target.

If any check fails, surface the missing path to the operator with a clear remedy ("Run `.claude/skills/references/db-sync.md` first" or "Sprint 11 Bit 11.3 wrapper missing — fall back to `python3 scripts/X.py`").

## Steps

1. **Parse the argument** to determine which system. The dispatch table below names the Makefile wrapper where one exists (Bit 11.3 / 11.1c); rows without a `Make wrapper` entry must use the direct `python3 scripts/X.py` form:

   | Argument | Make wrapper | Script | Default args |
   |----------|--------------|--------|-------------|
   | `15m` | `make 15m-audit` | `scripts/15m_live_audit.py` | `--regime auto` |
   | `hourly` | `make hourly-audit` | `scripts/hourly_shadow_audit.py` | `--regime auto` |
   | `spx` | — | `scripts/spx_shadow_audit.py` | `--regime auto` |
   | `weather` | — | `scripts/weather_shadow_audit.py` | `--regime auto` |
   | `sports` | — | `scripts/sports_shadow_audit.py` | `--regime auto` |
   | `no_side` | `make no-side` | `scripts/no_side_status.py` | `--db /tmp/state.db` |
   | `all` | partial (15m + hourly + no_side via `make`) | Run all 6 scripts sequentially | see above for each |

   If no argument provided, ask the user which system.
   If an additional date argument is provided (e.g., `/audit hourly 2026-03-01`), use `--since <date>` instead of `--regime auto` — note that this requires the direct `python3 scripts/X.py` form (Makefile wrappers don't accept custom args).

2. **Sync the database.** Follow `.claude/skills/references/db-sync.md` to sync the database.

3. **Run the audit**. Prefer the `make` wrapper if the dispatch table lists one (Bit 11.3 / 11.1c) — it pre-bakes `--db /tmp/state.db --regime auto`. Otherwise use the direct script invocation:
   ```bash
   # When a `make` wrapper exists (15m / hourly / no_side):
   make <wrapper> 2>&1

   # Otherwise (spx / weather / sports), or when passing custom args:
   python3 scripts/<script> --db /tmp/state.db --regime auto 2>&1
   ```
   If user provided a date, use `--since "<date>"` instead of `--regime auto` (always via direct invocation — wrappers don't pass through).

   **WHY `--regime auto`?** This flag makes the script detect the last git commit that changed relevant trading constants (via `git log --diff-filter`), then filters data to only include rows after that commit. This prevents mixing data from old configs (different sizing, edge thresholds, calibration) with current config — which would produce misleading WR and PnL numbers.

4. **Present the output** — show the full script output, then add:
   - **Top 3 findings**: most actionable insights from the audit
   - **Data gaps**: any sections that show insufficient data or missing columns
   - **Recommendations**: only if supported by statistical significance (p < 0.10)

5. **When running `/audit all`**, present a combined summary table at the end:
   ```
   ## Audit Summary

   | System  | Settled | WR (Wilson 95% CI) | Sim PnL | Status     |
   |---------|---------|--------------------:|--------:|------------|
   | 15M     | 237     | 92.0% (87.8-95.0)  |  +$142  | Live       |
   | Hourly  | 391     | 67.5% (62.6-72.1)  | -$1318  | Obs only   |
   | SPX     | 48      | 72.9% (58.2-84.7)  |   -$22  | Obs only   |
   | Weather | 156     | 61.5% (53.4-69.1)  |  -$203  | Obs only   |
   | Sports  | 89      | 58.4% (47.5-68.8)  |   -$94  | Obs only   |
   ```

## WHY Wilson CIs instead of raw percentages

Raw WR is misleading at small sample sizes. A system with 8W/2L shows 80% WR, but the Wilson 95% CI is (49.0%-94.3%) — it could easily be a losing strategy. Wilson score intervals:
- Are accurate even at small n (unlike Wald/normal approximation which breaks below n~30)
- Never produce impossible intervals (unlike Wald which can go below 0% or above 100%)
- Are the standard in clinical trials and A/B testing for exactly this reason

**Decision rule:** If the Wilson CI lower bound is below breakeven WR, the result is NOT significant — say so explicitly. Don't recommend config changes based on it.

## Error Handling

| Situation | Action |
|-----------|--------|
| Script not found (`No such file`) | Check if the script path is correct. Run `ls scripts/*audit*` to find available scripts. |
| Script errors with `no such table` | The DB copy may be from before that table was created. Tell user the system hasn't generated enough data yet. |
| Script outputs 0 rows / "No data found" | Not an error — the system may be new or the regime filter may be too narrow. Report: "0 settled trades in current regime (since YYYY-MM-DD). Either the regime is very new or the system isn't generating data." Offer to widen with `--since`. |
| Script runs but numbers look wrong | Check: is `--regime auto` picking the right date? Run `python3 scripts/<script> --db /tmp/state.db --regime auto --verbose 2>&1 | head -5` to see what regime date it detected. |
| `/audit all` and one script fails | Continue running the remaining scripts. Report the failure inline but don't abort the whole audit. |

## IMPORTANT
- Always use `/tmp/state.db` — never query VPS state.db directly (avoids busy_timeout contention with live bot)
- Present numbers with sample sizes. Small samples (n < 20) get a **"LOW SAMPLE — NOT SIGNIFICANT"** warning.
- Performance analysis must filter to current config regime — don't mix data from old configs with current
- All scripts use `--regime auto` which detects the last git commit that changed relevant trading constants. No hardcoded dates.
- If an audit reveals something alarming (WR below breakeven, unexpected losses), proactively offer `/investigate` or the relevant `-alpha` skill for deeper analysis.
