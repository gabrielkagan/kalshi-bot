# Scripts

Audit, research, backfill, and ops scripts. Read-only against `state.db` unless explicitly noted.

## Layout (post-Bit-11.2, 2026-05-12)

The flat `scripts/` root was reorganized into 3 tier subdirs:

- `scripts/audit/` — read-only analysis + Wilson CI + alpha-research + verification (33 files).
- `scripts/backfill/` — historical data backfills (9 files).
- `scripts/ops/` — operator-facing one-shots + setup + migrations + lock sentinels (32 files).
- `scripts/cal_mlp/` — cal_mlp pipeline (already a subdir; untouched by 11.2).
- `scripts/git_hooks/` — git hook templates (already a subdir; untouched by 11.2).

Files remaining directly at `scripts/`:

- `vps_mcp_server.py` (Bit 13.5 owns)
- `CLAUDE.md`, `STATE_DB_BACKUP_SETUP.md`, `VPS_SETUP.md` (docs)
- `io.kalshi.state-db-backup-heartbeat.plist.template` (template artifact)

The invariant "no `.py`/`.sh`/`.sql` directly under `scripts/` outside the allow-list" is pinned in `tests/contracts/test_bit_11_2_scripts_subdirs.py`.

## Conventions
- **Regime-filter every analysis.** Identify when the relevant config changed (`git log market_config.py bot/constants.py bot/main_loop.py bot/scanner/__init__.py bot/executor.py`) and filter `settled_trades` to that regime only. Pre-regime data is misleading. Bit 3.1 (May 8 2026) moved most module-level UPPER_SNAKE constants to `bot/constants.py`; Bits 7.1-9.3 extracted classes to canonical submodules; Bit 9.3-iii.c (2026-05-11) deleted the residual `bot/_impl.py` shim entirely — git history for that file is sealed at the deletion commit and won't show new regime changes.
- **Kelly-sized PnL only.** Sim PnL and counterfactuals must use the bot's actual Kelly + risk parameters. Never flat 1-contract.
- **Wilson CI on win rates** when n<200. Use `scripts/audit/wilson_ci.py` if it exists, else compute inline.
- **Verify schema before querying.** `PRAGMA table_info(<table>)` and `SELECT DISTINCT <col>` before assuming column values.
- **`settled_trades.pnl_cents` is GROSS, not net.** It excludes Kalshi fees. Any aggregate labeled "Net PnL" / "total_pnl" / "pnl" must use `SUM(pnl_cents - COALESCE(fee_cents, 0))`. The COALESCE protects legacy NULL-fee rows from being silently dropped. The exception is `sports_shadow_log` which has no `fee_cents` column (simulated PnL pre-fees by design); tag those sites with `# noqa: sports_shadow_log has no fee_cents`. Regression test: `tests/integration/test_audit_scripts_net_pnl.py`. Postmortem: `kb/failures/audit-pnl-fee-omission-apr29.md`.
- **Cell-block activations deflate `filter_stage='candidate'` rollups.** Audit/dashboard scripts that filter `WHERE filter_stage = 'candidate'` for "all 15M trades" totals under-count post-activation. Bleed-cell stage VALUES to UNION: `'96C_SOL_XRP_STC_DANGER_BAND'`, `'TM98_97_98C_2_5MIN_BLEED'`, `'SOL_TAKER_85_89C_2_5MIN_BLEED'`. Confirmed-affected list + cross-cutting detail: `bot/CLAUDE.md`. Decision: `kb/decisions/bleed-cell-blocks-2026-04-30.md`.

## Audit scripts (`scripts/audit/`)
- `audit_runner.sh` — aggregate runner; called by hourly cron
- `15m_live_audit.py`, `hourly_alpha_research.py`, `spx_shadow_audit.py`, etc. — per-system
- `doc_drift_check.py` — runs before commits that change config values (invoked by `make doc-drift`)

## Backfill scripts (`scripts/backfill/`)
- `gdelt_backfill.py`, `glassnode_backfill.py`, `cryptocompare_news_backfill.py` — invoked by `.github/workflows/h4_backfill.yml` (wrapped via `scripts/ops/h4_run_with_alert.py` for Telegram failure alerts)
- `shadow_coverage_backfill.py`, `shadow_coverage_calmlp_backfill.py` — Phase G coverage backfills
- `external_market_poller.py` — OKX funding/OI + Deribit DVOL; CRON-NEVER-INSTALLED (see `agent_docs/calibration_pipeline.md`)
- `stamp_data_provenance.py` — one-time post-migration backfill (referenced from `bot/state.py`)

## Ops scripts (`scripts/ops/`)
### Backup (Phase 0a — state.db S3 backup)
- `state_db_s3_backup.py` — `sqlite3.Connection.backup()` → zstd → `rclone copyto s3prod:bucket/daily/...`. Invoked nightly by `kalshi-state-db-backup.timer` (06:00 UTC). NEVER call directly with rsync semantics — see the docstring + `kb/decisions/auto-research-phase-0a-plan-may09.md` RCA.
- `state_db_restore.py` — verify-only mode (weekly automated check, integrity + row-count parity ±5%) OR `--to PATH` for manual incident recovery. Refuses to overwrite live `state.db` without `--allow-overwrite-live`.
- `state_db_backup_heartbeat.py` — heartbeat helper for the launchd plist template at scripts/ root.
- `setup_state_db_backup_timer.sh` — installer for both backup timers (mirrors `setup_h4_cron.sh` pattern). Re-runnable.

### Setup helpers
- `setup_audit_cron.sh`, `setup_doc_drift_timer.sh`, `setup_full_audit_timer.sh`, `setup_h4_cron.sh` — operator-facing cron + timer installers.
- `pre_deploy_check.sh` — manual pre-deploy aggregator (the canonical pre-deploy gate is `scripts/cal_mlp/deploy_check.sh`).

### Whitepaper + config artifacts
- `extract_config.py` → `config.json` (data artifact, repo root)
- `calibrate_dist.py` → `dist_config.json` (data artifact, repo root)
- `build_whitepaper.py`, `generate_whitepaper_stats.py` — invoked by `.github/workflows/whitepaper.yml`
- `build_lr_tables.py`, `sample_engine_inputs.py` — model + harness inputs

### Lock sentinels + h4 runtime
- `_mutmut_lock.py` — fcntl.flock(LOCK_EX|LOCK_NB) sentinel for concurrent mutmut/equivalence/integration races. Pinned in `Makefile` MUTMUT_GUARD.
- `_session_lock.py`, `_state_db_snapshot.py`, `_h4_runtime_safety.py` — operator-side lock + snapshot + h4 runtime helpers.
- `h4_run_with_alert.py` — Telegram-alerting wrapper for h4 backfill jobs (called from `.github/workflows/h4_backfill.yml`).

### Migration helpers + map
- `refresh_repo_map.py` — invoked by `make refresh-map`; regenerates `agent_docs/repository_map.md`.
- `supabase_migration_*.sql` — 11 numbered migration SQL files (007–019).

## STATE_DB_BACKUP_SETUP.md
- Lives at `scripts/STATE_DB_BACKUP_SETUP.md` (docs-tier; doesn't fit `audit/`/`backfill/`/`ops/`).
- Operator runbook for one-time bucket + IAM + lifecycle (steps 1–6). Run once per VPS lifecycle.

## DB connections
Any new `sqlite3.connect()` here must include both `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=10000`. The bot, `bot/snapshots/supabase_sync.py`, sports, `bot/ai/analyst.py` all share `state.db`.
