# Shadow Feature Status Report

Comprehensive report across ALL shadow/observation systems — 15M live, hourly, SPX, weather, and sports.

## Steps

1. **Copy state.db from VPS**
   ```
   scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
   ```

2. **Check for pre-computed audit snapshots** (fast path — runs in <1s):
   ```sql
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

   Each row's `metrics_json` is a JSON blob with these common fields:
   - `total_evals`, `signals`, `settled`, `wins`, `losses`, `pending`
   - `win_rate`, `win_rate_ci` (Wilson 95% CI), `sim_pnl_cents`, `avg_entry_price`

   System-specific fields:
   - **15m**: `total_trades`, `total_pnl_cents` (real $), `total_fees_cents`, `avg_fill_latency`, `daily_pnl_today`, `by_asset`, `maker_fills`, `taker_fills`
   - **hourly**: `brier`, `overconfidence_pp`, `temp_coverage_pct`, `worst_asset`, `worst_asset_pnl`, `by_asset`
   - **spx**: `brier`, `trading_days`, `blend_adapting`, `data_checks_pass`, `data_checks_total`
   - **weather**: `ensemble_coverage_pct`, `no_side_edge_populated`, `by_city`
   - **sports**: `sprt_llr`, `sprt_decision`, `sprt_n`, `games_covered`, `leagues`, `pregame_capture_pct`

3. **Full audit fallback** — Run all 5 audit scripts with appropriate `--since` flags:

   **Regime timestamps** (grep bot.py and CLAUDE.md for latest, or use these defaults):
   - 15M: `--regime auto` (auto-detect from gaps)
   - Hourly: `--since 2026-02-28T18:30:00` (three-layer optimization)
   - SPX: `--since 2026-03-02` (per-window limits deploy)
   - Weather: `--since 2026-03-02T16:54:00` (ensemble fix + instrumentation)
   - Sports: `--since 2026-03-01` (price ceiling + LR scale changes)

   Run each, capture output:
   ```
   python3 scripts/15m_live_audit.py --db /tmp/state.db --regime auto 2>&1
   python3 scripts/hourly_shadow_audit.py --db /tmp/state.db --since "2026-02-28T18:30:00" 2>&1
   python3 scripts/spx_shadow_audit.py --db /tmp/state.db --since "2026-03-02" 2>&1
   python3 scripts/weather_shadow_audit.py --db /tmp/state.db --since "2026-03-02T16:54:00" 2>&1
   python3 scripts/sports_shadow_audit.py --db /tmp/state.db --since "2026-03-01" 2>&1
   ```

4. **Read current shadow constants** from bot.py:
   - Grep for `_SHADOW_MODE`, `_OBSERVATION_ONLY`, and `SHADOW_` constants
   - For each, report: True (shadow/collecting) or False (promoted/live)

5. **Produce unified summary table**:

   | System | Mode | Signals | Settled | WR | Sim PnL | Brier | Key Issue | Promotion Ready? |
   |--------|------|---------|---------|----|---------| ------|-----------|-----------------|

   For each system, extract from audit snapshots or script output:
   - Signal count and settled count
   - Win rate and simulated PnL
   - Brier score (if available)
   - Top issue or blocker
   - Promotion readiness: READY / NEEDS MORE DATA / NOT READY (with reason)

   When using pre-computed snapshots, note the `computed_at` timestamp and label: *"(pre-computed N min ago)"*

6. **Shadow column instrumentation check**:
   - For each system, check if shadow columns are populating (non-null counts)
   - Flag any columns with 0% coverage as DATA GAP

7. **Report format** — Present the unified table FIRST, then per-system highlights:
   - 15M: live trading performance, any losses to investigate
   - Hourly: overconfidence metric, worst asset, STC sensitivity
   - SPX: EGARCH quality, per-window position limits working
   - Weather: ensemble coverage, NO-side edge, observed temp accumulation
   - Sports: comeback detection accuracy, signal volume, game coverage
