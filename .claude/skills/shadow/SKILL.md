---
name: shadow
description: "Bird's-eye summary across ALL 5 shadow/observation systems (15M, hourly, SPX, weather, sports). Use when: \"shadow overview\", \"how are all the shadows doing?\", \"shadow status report\", \"all shadows at a glance\", \"check all shadows\""
---

# Shadow Feature Status Report

Comprehensive report across ALL shadow/observation systems — 15M live, hourly, SPX, weather, and sports.

## Preflight

Follow `.claude/skills/references/preflight.md` substituting `<wrapper>` for the 2 mappable rows (`<wrapper>` = `15m-audit` and `hourly-audit`; spx/weather/sports require direct `python3 scripts/audit/<name>.py` invocation since no wrapper exists). Verify `/tmp/state.db` exists, `make -n 15m-audit && make -n hourly-audit` both parse, and the 5 fallback scripts (`scripts/audit/15m_live_audit.py`, `scripts/audit/hourly_shadow_audit.py`, `scripts/audit/spx_shadow_audit.py`, `scripts/audit/weather_shadow_audit.py`, `scripts/audit/sports_shadow_audit.py`) all exist (Bit 11.1d + Bit 11.2 2026-05-12 subdir reorg).

## Steps

1. **Sync the database.** Follow `.claude/skills/references/db-sync.md` to sync the database.

2. **Check for pre-computed audit snapshots** (fast path — runs in <1s):
   ```sql
   PRAGMA busy_timeout=10000;

   SELECT audit_type, computed_at, regime_since, metrics_json
   FROM audit_snapshots
   WHERE id IN (
       SELECT MAX(id) FROM audit_snapshots GROUP BY audit_type
   )
   ORDER BY audit_type;
   ```

   Run via: `sqlite3 /tmp/state.db "<query above>"`

   **If ALL 5 systems have rows AND all `computed_at` < 1 hour old → use fast path (skip to step 5).**
   **If the table doesn't exist or rows are stale (>1h) → fall back to full audit (step 3).**

   **WHY 1 hour staleness threshold?** Audit snapshots are computed by cron every hour. Data older than 1 hour may miss recent trades or settlements. During active trading hours, fresh data matters; during quiet hours, 1h-old data is fine. The threshold balances speed (fast path) vs accuracy (full audit).

   Each row's `metrics_json` is a JSON blob with these common fields:
   - `total_evals`, `signals`, `settled`, `wins`, `losses`, `pending`
   - `win_rate`, `win_rate_ci` (Wilson 95% CI), `sim_pnl_cents`, `avg_entry_price`

   System-specific fields:
   - **15m**: `total_trades`, `total_pnl_cents` (real $), `total_fees_cents`, `avg_fill_latency`, `daily_pnl_today`, `by_asset`, `maker_fills`, `taker_fills`
   - **hourly**: `brier`, `overconfidence_pp`, `temp_coverage_pct`, `worst_asset`, `worst_asset_pnl`, `by_asset`
   - **spx**: `brier`, `trading_days`, `blend_adapting`, `data_checks_pass`, `data_checks_total`
   - **weather**: `ensemble_coverage_pct`, `no_side_edge_populated`, `by_city`
   - **sports**: `sprt_llr`, `sprt_decision`, `sprt_n`, `games_covered`, `leagues`, `pregame_capture_pct`

3. **Full audit fallback** — Run all 5 audit scripts with `--regime auto`:

   ```bash
   make 15m-audit 2>&1          # wraps `python3 scripts/audit/15m_live_audit.py --db /tmp/state.db --regime auto` (Bit 11.3)
   make hourly-audit 2>&1       # wraps `python3 scripts/audit/hourly_shadow_audit.py --db /tmp/state.db --regime auto` (Bit 11.3)
   python3 scripts/audit/spx_shadow_audit.py --db /tmp/state.db --regime auto 2>&1
   python3 scripts/audit/weather_shadow_audit.py --db /tmp/state.db --regime auto 2>&1
   python3 scripts/audit/sports_shadow_audit.py --db /tmp/state.db --regime auto 2>&1
   ```

   **Always use `--regime auto`** — it auto-detects the last relevant config change from git history. Never hardcode regime dates (they go stale).

4. **Read current shadow constants** from `bot/constants.py` (Bit 3.1: module-level UPPER_SNAKE constants live there):
   - Grep for `_SHADOW_MODE`, `_OBSERVATION_ONLY`, and `SHADOW_` constants
   - For each, report: True (shadow/collecting) or False (promoted/live)

5. **Produce unified summary table**:

   ```
   ## Shadow Status (as of HH:MM UTC)

   | System  | Mode       | Settled | WR (95% CI)       | Sim PnL | Key Issue            | Ready?          |
   |---------|------------|--------:|------------------:|--------:|:---------------------|:----------------|
   | 15M     | LIVE       | 237     | 92.0% (87.8-95.0) | +$142   | —                    | LIVE            |
   | Hourly  | Obs only   | 391     | 67.5% (62.6-72.1) | -$1318  | Overconfident +19pp  | NOT READY       |
   | SPX     | Obs only   | 48      | 72.9% (58.2-84.7) | -$22    | Low n, need 100+     | NEEDS MORE DATA |
   | Weather | Obs only   | 156     | 61.5% (53.4-69.1) | -$203   | Ensemble gaps        | NOT READY       |
   | Sports  | Obs only   | 89      | 58.4% (47.5-68.8) | -$94    | SPRT: CONTINUE       | NEEDS MORE DATA |

   (pre-computed 23 min ago)
   ```

   When using pre-computed snapshots, note the `computed_at` timestamp.

6. **Shadow column instrumentation check**:
   - For each system, check if shadow columns are populating (non-null counts)
   - Flag any columns with 0% coverage as DATA GAP

7. **Report format** — Present the unified table FIRST, then per-system highlights:
   - **15M**: live trading performance, any losses to investigate, shadow variant progress (A1/A2/A3)
   - **Hourly**: overconfidence metric, worst asset, CalEngine status
   - **SPX**: EGARCH quality, observation count, trading days covered
   - **Weather**: ensemble coverage, per-city breakdown, data freshness
   - **Sports**: SPRT decision status, game coverage, per-sport WR

## Error Handling

| Situation | Action |
|-----------|--------|
| `audit_snapshots` table doesn't exist | Fall back to full audit (step 3). This is normal on first run or if auditor cron hasn't run yet. |
| Some systems have snapshots but not all | Use snapshots for available systems, run individual audit scripts for missing ones. Note which are pre-computed vs fresh. |
| Snapshots exist but are very stale (>6h) | Flag: "Snapshots are N hours old — auditor cron may not be running. Running full audit." Fall back to step 3. Consider investigating the cron job. |
| One audit script fails | Continue running the others. Show the error inline for the failed system, report the rest normally. Don't abort the whole report. |
| A system shows 0 settled but has evals | The system is collecting data but nothing has settled yet. Report the eval count and when the first eval was created. Say "collecting — no settlements yet." |
| Script output is empty or just warnings | The system may not have enough data in the current regime. Report: "No data in current regime for [system]." |

## IMPORTANT
- Present the unified summary table FIRST — the user wants the bird's-eye view before details
- Always use `--regime auto` instead of hardcoded dates (dates go stale)
- When using pre-computed snapshots, always show the timestamp so the user knows data freshness
- If any system looks alarming (WR dropping, unexpected losses), flag it and offer the system-specific `-alpha` skill for deeper analysis
- This is a SUMMARY skill — don't do deep analysis here. If the user asks follow-up questions about one system, route to the appropriate `-alpha` skill
