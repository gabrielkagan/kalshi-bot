# Scripts

Audit, research, and one-off analysis scripts. Read-only against `state.db` unless explicitly noted.

## Conventions
- **Regime-filter every analysis.** Identify when the relevant config changed (`git log market_config.py bot/constants.py bot/main_loop.py bot/scanner/__init__.py bot/executor.py`) and filter `settled_trades` to that regime only. Pre-regime data is misleading. Bit 3.1 (May 8 2026) moved most module-level UPPER_SNAKE constants to `bot/constants.py`; Bits 7.1-9.3 extracted classes to canonical submodules; Bit 9.3-iii.c (2026-05-11) deleted the residual `bot/_impl.py` shim entirely — git history for that file is sealed at the deletion commit and won't show new regime changes.
- **Kelly-sized PnL only.** Sim PnL and counterfactuals must use the bot's actual Kelly + risk parameters. Never flat 1-contract.
- **Wilson CI on win rates** when n<200. Use `scripts/wilson_ci.py` if it exists, else compute inline.
- **Verify schema before querying.** `PRAGMA table_info(<table>)` and `SELECT DISTINCT <col>` before assuming column values.
- **`settled_trades.pnl_cents` is GROSS, not net.** It excludes Kalshi fees. Any aggregate labeled "Net PnL" / "total_pnl" / "pnl" must use `SUM(pnl_cents - COALESCE(fee_cents, 0))`. The COALESCE protects legacy NULL-fee rows from being silently dropped. The exception is `sports_shadow_log` which has no `fee_cents` column (simulated PnL pre-fees by design); tag those sites with `# noqa: sports_shadow_log has no fee_cents`. Regression test: `tests/integration/test_audit_scripts_net_pnl.py`. Postmortem: `kb/failures/audit-pnl-fee-omission-apr29.md`.
- **Cell-block activations deflate `filter_stage='candidate'` rollups.** Audit/dashboard scripts that filter `WHERE filter_stage = 'candidate'` for "all 15M trades" totals under-count post-activation. Bleed-cell stage VALUES to UNION: `'96C_SOL_XRP_STC_DANGER_BAND'`, `'TM98_97_98C_2_5MIN_BLEED'`, `'SOL_TAKER_85_89C_2_5MIN_BLEED'`. Confirmed-affected list + cross-cutting detail: `bot/CLAUDE.md`. Decision: `kb/decisions/bleed-cell-blocks-2026-04-30.md`.

## Audit scripts
- `audit_runner.sh` — aggregate runner; called by hourly cron
- `15m_live_audit.py`, `hourly_alpha_research.py`, etc. — per-system
- `doc_drift_check.py` — runs before commits that change config values

## Backup scripts (Phase 0a — state.db S3 backup)
- `state_db_s3_backup.py` — `sqlite3.Connection.backup()` → zstd → `rclone copyto s3prod:bucket/daily/...`. Invoked nightly by `kalshi-state-db-backup.timer` (06:00 UTC). NEVER call directly with rsync semantics — see the docstring + `kb/decisions/auto-research-phase-0a-plan-may09.md` RCA.
- `state_db_restore.py` — verify-only mode (weekly automated check, integrity + row-count parity ±5%) OR `--to PATH` for manual incident recovery. Refuses to overwrite live `state.db` without `--allow-overwrite-live`.
- `setup_state_db_backup_timer.sh` — installer for both timers (mirrors `setup_h4_cron.sh` pattern). Re-runnable.
- `STATE_DB_BACKUP_SETUP.md` — operator runbook for one-time bucket + IAM + lifecycle (steps 1–6). Run once per VPS lifecycle.

## DB connections
Any new `sqlite3.connect()` here must include both `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=10000`. The bot, `supabase_sync`, sports, analyst all share `state.db`.
