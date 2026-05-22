# Scripts

Audit, research, backfill, and ops scripts. Read-only against `state.db` unless explicitly noted.

## Layout (post-Bit-11.2, 2026-05-12)

The flat `scripts/` root was reorganized into 3 tier subdirs:

- `scripts/audit/` — read-only analysis + Wilson CI + alpha-research + verification (35 files).
- `scripts/backfill/` — historical data backfills (9 files).
- `scripts/ops/` — operator-facing one-shots + setup + migrations + lock sentinels (34 files).
- `scripts/cal_mlp/` — cal_mlp pipeline (already a subdir; untouched by 11.2).
- `scripts/git_hooks/` — git hook templates (already a subdir; untouched by 11.2).
- `scripts/research/` — falsification spikes + cross-system research (F-series CT-MDP falsifications: `f0_1_stale_quote_falsification.py`, `f0_4_cross_asset_lead_lag.py`, `f0_5_settlement_window_gamma.py`; tests at `tests/research/`). Read-only against `state.db`; verdict docs land at `kb/findings/`.

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
- **Cell-block activations deflate `filter_stage='candidate'` rollups.** Audit/dashboard scripts that filter `WHERE filter_stage = 'candidate'` for "all 15M trades" totals under-count post-activation. Bleed-cell stage VALUES to UNION: `'96C_SOL_XRP_STC_DANGER_BAND'`, `'TM98_97_98C_2_5MIN_BLEED'`, `'SOL_TAKER_85_89C_2_5MIN_BLEED'`, `'SOL_BLEED_V2_88_93C_2_5MIN'`. (Canonical 5-set including baseline `'candidate'` lives in `bot.helpers.cohort_attribution.COHORT_PARTITION_STAGES` — Money Printer Roadmap P1.1, ticket `86b9x3kgd`.) Confirmed-affected list + cross-cutting detail: `bot/CLAUDE.md`. Decision: `kb/decisions/bleed-cell-blocks-2026-04-30.md`.

## Audit scripts (`scripts/audit/`)
- `audit_runner.sh` — aggregate runner; called by hourly cron
- `15m_live_audit.py`, `hourly_alpha_research.py`, `spx_shadow_audit.py`, etc. — per-system
- `doc_drift_check.py` — runs before commits that change config values (invoked by `make doc-drift`)
- `cohort_attribution_nightly.py` — nightly 13:07 UTC materialization of `cohort_attribution_daily` (Money Printer Roadmap P1.1, ticket `86b9x3kgd`)
- `weekly_bleed_report.py` — Mondays 13:13 UTC markdown report → `kb/findings/weekly-bleed-{YYYY-MM-DD}.md` + Telegram one-liner via canonical `_TELEGRAM` singleton (Money Printer Roadmap P1.4, ticket `86b9x3kn2`)

## Backfill scripts (`scripts/backfill/`)
- `gdelt_backfill.py`, `glassnode_backfill.py`, `cryptocompare_news_backfill.py` — invoked by `.github/workflows/h4_backfill.yml` (wrapped via `scripts/ops/h4_run_with_alert.py` for Telegram failure alerts)
- `shadow_coverage_backfill.py`, `shadow_coverage_calmlp_backfill.py` — Phase G coverage backfills
- `external_market_poller.py` — OKX funding/OI + Deribit DVOL; CRON-NEVER-INSTALLED (see `agent_docs/calibration_pipeline.md`)
- `stamp_data_provenance.py` — one-time post-migration backfill (referenced from `bot/state.py`)
- `backfill_extended_features.py` — one-time Tier 4 (time/regime) + Tier 5 (derived) backfill on `evaluated_opportunities` (pre-B.1a).
- `wave1_derived_cols.py` — B.1a-fu2 (2026-05-12) one-shot backfill of Wave 1 derivable cols (`hour_sin`/`hour_cos`/`sigma_winsorize`/`prob_breakeven_gap`) on `rejected_opportunities` + `prob_breakeven_gap` on `evaluated_opportunities`. Replays B.1a auto-fill via canonical helpers in `bot/helpers/{derived_features,time_features}.py`. Idempotent + honest-NULL.
- `crypto_replay_backfill.py` — Phase 2 replay backfill (originally 86b9wy7v3 as `hype_doge_replay_backfill.py`, 2026-05-12; renamed + BNB-widened in Bit F (86ba1wpck, 2026-05-21)). Pulls historical Kalshi `KX{HYPE,DOGE,BNB}15M` settled markets (public REST) + Coinbase 1-min `{HYPE,DOGE,BNB}-USD` candles, drives `bot.engines.probability.ProbabilityEngine.compute()` against each market's open_time, pairs with realized YES/NO settlement, writes to `historical_replay_calmlp` table (PK `(ticker, evaluation_time)`, CHECK on asset+result enums including BNB). Bit F added `spot_staleness_seconds REAL` audit column populated from `eval_ts - warmup[-1][0]` (NULL when warmup empty). Lock-step via `bot.helpers.derived_features` for `hour_sin`/`hour_cos`/`sigma_winsorize`/`prob_breakeven_gap`. Phase 2 v1: `blended_prob` honest-NULL (no HYPE/DOGE/BNB trained cal_mlp predictor exists at either deployment site — Mac OR VPS; production `_calmlp_predictors` covers only BTC/ETH/SOL/XRP), `prob_breakeven_gap` honest-NULL (no historical Kalshi orderbook). Mac-side fetch — Bybit (CloudFront 403 from US) + Binance.com (HTTP 451) geo-blocked, mirroring `BINANCE_FEED_ENABLED=0` runtime default; uses Coinbase REST (also bot's primary live feed for these assets). Operator-supplied `--db PATH`; do NOT point at production `state.db`. Sister `86b9wy15n` (calibration health check) consumes the corpus. Bit F migration script: `scripts/ops/migrate_replay_table_bit_f.py` (one-shot CHECK widening + spot_staleness column add; idempotent).
- `cross_asset_transfer_blended_prob.py` — Bit A of HYPE/DOGE cal_mlp v1.1 retrain umbrella (umbrella `86ba0jmyq`, bit ticket `86ba0jmzu`, 2026-05-19). Reads `historical_replay_calmlp` HYPE/DOGE rows WHERE `blended_prob IS NULL`, feeds each through `CalMLPPredictor("BTC")` (cross-asset transfer mechanism: unseen ticker → vocab ID 0 per `scripts/cal_mlp/integration.py:1037`), UPDATEs `blended_prob` in place. Idempotent + per-row try/except + Mac-only defensive guard (refuses paths under `/home/botuser/`). Lock-step via `bot.helpers.derived_features.compute_derived_features` + `compute_hour_sin_cos` (no inline math.sin/cos; AST pinned by `tests/integration/test_cross_asset_transfer_blended_prob.py::test_no_inline_hour_sin_cos_math`). Honest-NULL: every row in the current 9,794-row replay corpus (4,897 HYPE + 4,897 DOGE) has `prob_breakeven_gap IS NULL`; the script's skip counters partition the candidates into `null_raw_prob=76` (raw_prob pre-filter, predict never invoked) + `predict_raised=9,718` (CalMLPError missing_features on the bp_gap CONT_FEATURE_COLS check). `blended_prob` stays NULL on every row; no fabricated feature values. CLI: `--db PATH [--asset HYPE|DOGE] [--limit N] [--dry-run]`. Sister Bit E (`86ba0jn6a` — Kalshi trade-history scrape to backfill `prob_breakeven_gap`) is the prerequisite for non-trivial Bit A outcome; once that lands, this script's idempotent re-run produces the real backfill.

## cal_mlp pipeline (`scripts/cal_mlp/`)
- `extract_data.py` — Phase 2 training-corpus extractor for the
  PRODUCTION recipe. Reads `evaluated_opportunities` from `state.db`,
  runs `_classify_drop` (12 sequential predicates) per row, writes
  kept rows to per-asset Parquet under `data/cal_mlp/<asset>/`. Bit B
  (86ba0jn0w, 2026-05-19) widened `--asset` choices from
  `['BTC','ETH','SOL','XRP']` to add HYPE/DOGE — letting HYPE/DOGE
  LIVE rows (in `evaluated_opportunities`) flow through the production
  recipe. No UNION with the replay corpus (architectural regression
  per Bit B R1 finding M1; see `kb/decisions/v1-1-B-extract-union-plan.md`
  L99 STALE patterns). `cfg_fp` UNCHANGED by Bit B.
- `extract_data_replay.py` — parallel HYPE/DOGE replay-corpus
  extractor shipped P2.1.a-3 (ticket `86b9wuhhr`, commit `3a9d690a`).
  Reads `historical_replay_calmlp` from `data/replay/state.db`
  (Mac-only). Strict-subset 4-feature recipe with its own
  `compute_cfg_fp_replay` namespace pinned at `ea9c30477f844afa`
  (pre-Bit-F: `9347942aaba71146`; rotated Bit F 86ba1wpck 2026-05-21 via the BNB-key addition to `ASSET_FLOORS_REPLAY`)
  (dropped from recipe: `market_price`, `prob_breakeven_gap`). Boundary
  with `extract_data.py`: live rows → production recipe; replay rows →
  replay recipe. `train.py` consumes both bundle types via
  `CONT_FEATURE_COLS_REPLAY` routing at `train.py:690`.

## Ops scripts (`scripts/ops/`)
### Backup (Phase 0a — state.db S3 backup)
- `state_db_s3_backup.py` — `sqlite3.Connection.backup()` → zstd → `rclone copyto s3prod:bucket/daily/...`. Invoked every 4h by `kalshi-state-db-backup.timer` (00/04/08/12/16/20:00 UTC; cadence revised from daily 06:00 UTC by ticket `86b9zkp89` 2026-05-17 — Bronze durability; sub-daily ticks share the same `daily/<UTC-date>.db.zst` S3 key, so S3 retains the latest-of-day per day). NEVER call directly with rsync semantics — see the docstring + `kb/decisions/auto-research-phase-0a-plan-may09.md` RCA.
- `state_db_restore.py` — verify-only mode (weekly automated check, integrity + row-count parity ±5%) OR `--to PATH` for manual incident recovery. Refuses to overwrite live `state.db` without `--allow-overwrite-live`.
- `state_db_backup_heartbeat.py` — heartbeat helper for the launchd plist template at scripts/ root.
- `setup_state_db_backup_timer.sh` — installer for both backup timers (mirrors `setup_h4_cron.sh` pattern). Re-runnable.
- `export_market_obs_to_s3.py` — nightly archive of `market_observations_continuous` (the only retention-pruned table) to S3 via Parquet+zstd. Read-only SQLite connection; target_date = today - 13d (rows still exist for ≥1 more day). Invoked nightly by `kalshi-market-obs-archive.timer` (05:30 UTC). Ticket `86b9xcdwg`.
- `setup_market_obs_archive_timer.sh` — installer for the market_obs archive timer. Companion to `setup_state_db_backup_timer.sh` (expects that one to have run first; shares `s3prod` rclone remote + bucket creds). Re-runnable.
- `journal_archives_s3_sync.py` — incremental S3 sync of `~/kalshi-bot-repo/journal_archives/` via `rclone copy --checksum --immutable` (NOT `sync` — `sync` would mirror-delete S3 objects when local rotation prunes at 90d; `copy` is one-way). Excludes live `*.jsonl` (current-day, uncompressed) AND `rotation.log` (which `rotate_journals.sh` appends to daily — would trip `--immutable` otherwise). Idempotent: re-runs are no-ops via S3 ETag short-circuit. Invoked every 4h by `kalshi-journal-archives-sync.timer` (00/04/08/12/16/20:30 UTC, 30 min after each paired `rotate_journals.sh` tick which itself runs every 4h on the hour post-2026-05-17; cadence revised from daily 04:30 UTC by ticket `86b9zkp89`). Ticket `86b9xgp7k`.
- `setup_journal_archives_sync_timer.sh` — installer for the journal archives sync timer. Same companion-to-state.db posture (shares `s3prod` rclone remote + bucket creds). Re-runnable.
- `fetch_training_data_from_s3.py` — operator-run one-shot CLI that pulls the four training-data sources (state.db daily backup, market_obs Parquet, journal archives, bronze Kalshi WS chunks) from S3 into a local target dir + emits `MANIFEST.txt` (SHA256 + S3 path + size + mtime) for drift detection on re-fetch. Reuses `state_db_restore.restore_to_path` for the state.db source. `rclone copy --checksum --immutable` for the flat-prefix + bronze-partition sources (mirrors `journal_archives_s3_sync` idempotency). Exit 0 clean / 1 partial / 2 full failure / 64 usage. Ticket `86b9zkn60`.

### Setup helpers
- `setup_audit_cron.sh`, `setup_doc_drift_timer.sh`, `setup_full_audit_timer.sh`, `setup_h4_cron.sh`, `setup_cohort_attribution_cron.sh` (P1.1, ticket `86b9x3kgd`), `setup_weekly_bleed_report_cron.sh` (P1.4, ticket `86b9x3kn2`) — operator-facing cron + timer installers.
- `pre_deploy_check.sh` — manual pre-deploy aggregator (the canonical pre-deploy gate is `scripts/cal_mlp/deploy_check.sh`).

### Whitepaper + config artifacts
- `extract_config.py` → `config.json` (data artifact, repo root)
- `calibrate_dist.py` → `ops/runtime/dist_config.json` (data artifact; relocated from repo root in Sprint 14-A Bit 3, 2026-05-17, ticket 86b9zfbt8 — lock-step with `bot/config.py:DIST_CONFIG_PATH` + `bot/snapshots/dashboard_snapshot.py` reader)
- `build_whitepaper.py`, `generate_whitepaper_stats.py` — invoked by `.github/workflows/whitepaper.yml`
- `build_lr_tables.py`, `sample_engine_inputs.py` — model + harness inputs

### Health monitoring (cron-driven)
- `collector_health_monitor.py` (D1.6 + D2.5 + B3-fu3 + D1.8) — multi-tier cron-driven Telegram alert dispatcher: 4 collector checks × 2 WS-collector tiers (kalshi-collector, kalshi-coinbase-collector) + 3 weather checks (kalshi-weather-collector, no WS) + 1 bot check (insert_evaluated_opportunity failures). Per-tier dedup-key prefixes `d1_6_*` / `d2_5_*` / `d1_8_*` / `b3_fu3_*`. Lazy import of `bot.notifier.TelegramNotifier`; always exits 0 (cron convention). Operator-installed cron entry per the module docstring.
- `phantom_reconcile_monitor.py` (TBD, 2026-05-19) — hourly cron-driven wrapper around `scripts/audit/phantom_pnl_audit.py`. Invokes `run_audit(apply=True, audit_run_id="auto-YYYY-MM-DD")` over the last 24h and Telegram-alerts on 3 classes with day-stable cross-process dedup (JSON sidecar at `./phantom_reconcile_dedup.json`): (1) SUMMARY — aggregated material drift `|delta_pnl_cents| >= $5`, top 5 by |Δpnl|, dedup prefix `phantom_reconcile_summary`; (2) UNVERIFIED — Kalshi REST left ≥50% (ticker, side) pairs unverified, dedup prefix `phantom_reconcile_unverified`; (3) CRASH — auditor itself raised `Exception`, dedup prefix `phantom_reconcile_crash`. `audit_run_id` is day-granular (not hour) so INSERT OR REPLACE on UNIQUE(audit_run_id, ticker, side) keeps `phantom_corrections` to at most one row per (day, ticker, side) — downstream LEFT JOIN consumers see no row multiplication. `KeyboardInterrupt` / `SystemExit` propagate (don't get caught + alerted). Synchronous `send_sync` (added 2026-05-19 to `bot/notifier.py`) closes the SIGTERM/daemon-thread race — sidecar records only on 2xx delivery. `fcntl.flock(LOCK_EX)` on a sibling `.lock` file serializes concurrent `_record_sent` writers. Operator-installed cron entry per the module docstring; cron env must source whichever env files export the 4 required keys (`KALSHI_API_KEY{,_ID}`, `KALSHI_PRIVATE_KEY_PATH`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) — on the production VPS these are split between `~/.env` (Telegram) and `~/kalshi-bot-repo/.env` (Kalshi, mirrored from `kalshi-bot.service` `EnvironmentFile=`); see `ops/CLAUDE.md` "Phantom reconcile cron" for the canonical install line.

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
