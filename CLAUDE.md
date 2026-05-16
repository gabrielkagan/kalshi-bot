# Kalshi Crypto Trading Bot

Cryptocurrency prediction market bot for Kalshi. Trades 15-minute above/below windows on BTC, ETH, SOL, XRP, HYPE, DOGE live (HYPE/DOGE T4 promoted 2026-05-14 via P2.3 raw_prob + per-asset MARKET_BLEND_W; cal_mlp training arc retired). Hourly markets and weather/sports/SPX scan in observation mode.

## Reference docs (read on demand)

- `agent_docs/current_state.md` — what's live, what's shadow, what's disabled. Refresh-target.
- `agent_docs/config_reference.md` — every constant in `bot/constants.py` (canonical home post-Bit-3.1; Bit 9.3-iii.c (2026-05-11) deleted the bot/_impl.py shim that previously re-exported them via `from bot.constants import *`) with data justifications.
- `agent_docs/db_schema.md` — `state.db` schema for all tables.
- `agent_docs/bot_layout.md` — project file map (bot/_impl.py was DELETED in Bit 9.3-iii.c; class table reflects canonical submodule homes).
- `agent_docs/calibration_pipeline.md` — calibration, hourly three-layer, three-commit rule.
- `kb/_index.md` — design decisions, postmortems, strategy specs (read for any deep "why" question).
- `kb-research/_index.md` — compiled research findings.
- Package-level guides auto-load when working in-dir: `bot/CLAUDE.md`, `tests/CLAUDE.md`, `scripts/CLAUDE.md`, `ops/CLAUDE.md`, `kb/CLAUDE.md`.

## Interaction rules

- **Answer first, plan later.** For investigations (loss, alert, anomaly), give numbers first. No plan mode, no code exploration before answering.
- **Don't re-plan finalized plans.** Continuing from a prior session = start implementing.
- **Don't deploy without explicit confirmation.** Always present the change summary; wait for approval before `git push`.
- **Followups → ClickUp, always.** Any emergent issue outside the current task scope (bug, cleanup, idea, deferred item) files a ticket via `/ticket`. Never bury followups in KB bullets, end-of-turn prose, "deferred items" sections, or `# TODO` comments without a ticket ID. Default to over-filing — triage later.
- **Extraction-bit discipline (Sprint 4-9 + any HIGH-risk Bit):** state atop every plan doc — (1) RCA every CRITICAL/MAJOR finding before patching; (2) TDD-first via the Pillar 4 hook (`/test-writer` scaffolds the failing regression test); (3) adversarial review to 2 consecutive zero-CRITICAL/MAJOR rounds (some Bits need 8). Lessons L32-L80 + 12-step pre-flight: `kb/concepts/extraction-pre-flight-checklist.md`.

## Critical rules

Each one-liner fires here; rationale + history live in `kb/failures/` postmortems.

- **`bot/__main__.py` is the entrypoint shim — sacred boundary, no logic.** Logic lives in `bot/<subpackage>/<module>.py` (e.g., `bot/main_loop.py`, `bot/scanner/__init__.py`, `bot/executor.py`, `bot/settlement.py`, `bot/order_flow.py`, `bot/orphan_db_watchdog.py`, `bot/boot.py`, `bot/engines/{volatility,probability,calibration}.py`, `bot/feeds/`, `bot/fetchers/`, `bot/helpers/`, `bot/notifier.py`, `bot/logger.py`, `bot/state.py`, `bot/kalshi_client.py`, `bot/runtime_config.py`, `bot/snapshots/`). Runtime chain: systemd → `ops/kalshi-bot.service` → `start.sh` → `python -m bot` → `bot/__main__.py` → `bot.main_loop.MainLoop` (with `import bot._thread_env` firing FIRST so OMP_NUM_THREADS=1 is set before numpy loads transitively). Source of truth for runtime config = `ops/`. **Post-Bit-9.3-iii.c (2026-05-11) bot/_impl.py is DELETED** — the residual re-export shim no longer exists. Callers must use canonical submodules directly (`bot.constants.X`, `bot.main_loop.MainLoop`, `bot.state.StateManager`, etc.). bot/snapshots/dashboard_snapshot.py + bot/snapshots/supabase_sync.py (post Bit 10.4 2026-05-12 sibling-reorg) read runtime config via `import bot.runtime_config as _bot_mod` (PEP 562 dual-probe of bot.constants → bot.config; Bit 12.1 (2026-05-12) retargeted from repo-root `config.py` → `bot/config.py`) — that helper replaces the bot._impl namespace they previously used. **The `_BotProxy` (retired in 9.3-iii.b) and bot/_impl.py (deleted in 9.3-iii.c) are GONE.** Sprint 9 main modularization is CLOSED. **D1.1 + D1.1.5 + D1.2 + D1.3 + D1.4 + D1.5 (2026-05-16, tickets `86b9ypn49`+`86b9zdhz2`+`86b9ypn66`+`86b9ypn72`+`86b9ypn8r`+`86b9ypna4`)**: new top-level SIBLINGS `collector/` + `kalshi_wire/` (Data Corpus). Both have ZERO `bot.*` imports (`[importlinter:contract:collector-no-bot]` + `[importlinter:contract:kalshi_wire-no-{bot,collector}]`). `kalshi_wire/` is the shared transport (RSA-PSS auth + WS connect/reconnect/silence-watchdog + frame parse) consumed by both `bot/feeds/kalshi.py` (KalshiFeed, via 4 sync callbacks) AND `collector/ws_connection.py` (BronzeArchiver); "two sides of the same coin" symmetry per the 2026-05-16 §5 AMENDMENT. D1.2 shipped the bronze data plumbing: `collector/{writer,uploader,main_loop,ws_connection}.py` bodies (WS → JSONL.zst → S3 via `rclone copyto`, KEEP-local on failure, D0.3 §7 contract). **D1.3 SHIPPED** wires `collector/subscription_manager.py` body (tier-aware per-conn ticker assignment + subscribe-frame batching) + `BronzeArchiver.on_session_start` callback (dispatches subscribe frames; binds sid→channel from `type=subscribed`/`type=ok` acks) + `main_loop` multi-conn fan-out (one BronzeArchiver per conn, channel-aware `writers_by_channel` dispatch, single drain thread fans out across all writers). First-bronze-flow landed at D1.3 (R1-C3 acceptance criterion deferred from D1.2 closed here). **D1.4 SHIPPED** wires `collector/rest_snapshot.py` body — Kalshi REST `/markets?status=open` paginated fetch via `kalshi_wire.auth.make_rest_headers` (no inline RSA-PSS) returning `{TIER_ALL: [tickers]}` (single-tier classification until measurement-driven split justified; D0.2 scope-map authoritative) + `RestSnapshotRefresher` (hourly poll, first tick immediate on start, callback fires only on ticker-set change to avoid reconnect storms, callback exceptions swallowed) + `BronzeArchiver.update_subscriptions` / `request_reconnect` (atomic-replace of subscribe frames under `_lock` on the write side — readers in `_on_session_start` + `_handle_subscribe_ack` use single-bytecode-op attribute capture under the GIL, see the `update_subscriptions` docstring + WSClient force-cycle so on_session_end clears sid map then on_session_start dispatches new subscribes) + `main_loop._replan_for_archivers` (REST-refresh callback that re-plans via SubscriptionManager + propagates to every archiver). Hourly REST is the new default ticker source; `COLLECTOR_TICKERS_FILE` retained as the offline/test boot seam. Net `.importlinter` contracts 6 → 8 (unchanged through D1.5). **D1.5 SHIPPED** ships `ops/kalshi-collector.service` (CPUAffinity=1 / Nice=10 / MemoryMax=512M / MemorySwapMax=0 / LimitNOFILE=4096 / `Restart=on-failure`+`RestartSec=10s` / `EnvironmentFile=/home/botuser/.env.collector` / `ExecStart=/home/botuser/kalshi-bot-repo/collector-start.sh`) + extends `ops/install.sh` to a multi-unit parallel-array installer (validates+enables BOTH kalshi-bot AND kalshi-collector atomically) + refreshes `collector-start.sh` body to source `/home/botuser/.env.collector` exclusively (dedicated home-rooted env file, NOT the bot's repo-rooted `.env` — strengthens D0.3 §6 isolation beyond the D1.1 stub). Bronze day-zero = first-chunk-in-S3 timestamp after operator runs `systemctl start kalshi-collector` on the VPS (REQUIRES-APPROVAL discipline; never auto-deploy). 3 D0.3 §12 operator decisions resolved at kickoff: lifecycle Standard → DEEP_ARCHIVE @ 30d (skip IA, matches journals/ precedent), Nice=10 (I/O-bound), KALSHI_COLLECTOR_KEY_ID via operator-provisioned `.env.collector`. See `agent_docs/bot_layout.md` "Data Corpus collector" + `kalshi_wire/` sections + `kb/decisions/data-corpus-architecture.md` (D0.3) + `ops/CLAUDE.md` for full architecture.
- Never commit `.env` or `*.jsonl` (gitignored). KB files (`kb/`, `kb-research/`) are local-only by convention — don't `git add` new files there (existing tracked entries are pre-rule legacy).
- Syntax-check before commit: `make ast-check` (alias scans `bot/constants.py` + `bot/main_loop.py` + `bot/scanner/__init__.py` post-Bit-9.3-iii.c; bot/_impl.py is deleted).
- Pushing to main auto-deploys. Always verify the VPS pulled the new commit hash.
- Data-driven changes only. No config tuning without backing data.
- After signature changes: grep all call sites. `ast.parse` won't catch unbound names.
- After constant changes in `bot/constants.py`: grep across the repo, especially `market_config.py` (asserts at startup → crash loop on mismatch). Constants no longer live in bot/_impl.py (deleted in Bit 9.3-iii.c).
- Performance analysis filters to current config regime. Pre-regime data is misleading.
- After deploy: verify expected DB rows are being created (e.g., `stc_shadow` when STC 300-600s, `weather_observation` when weather is on). "Service running, no errors" is not enough.
- Investigate before explaining. Look at actual data, not assumptions about it.
- Verify schema before querying: `PRAGMA table_info()` and `SELECT DISTINCT`.
- After bug fixes: root-cause it, write a regression test, draft a postmortem in `kb/failures/`. Never just fix and move on.
- **Equivalence snapshots are never auto-regenerated.** `tests/equivalence/` (Pillar 3) pins engine outputs against a 1000-row corpus. If a snapshot fails, **investigate the divergence** — never run `pytest --force-regen` autonomously. Regen is a human-with-diff-review operation; see `tests/equivalence/REGEN.md`.
- Sim PnL and counterfactuals use actual Kelly sizing. Never flat 1-contract.
- Dashboard changes: `bot/snapshots/dashboard_snapshot.py` (Bit 10.4 2026-05-12 sibling-reorg) and `dashboard/index.html` (gh-pages) ship in the same commit per `kb/decisions/dashboard-overhaul-plan.md`.
- Doc drift: when changing config values, update `README.md` / `whitepaper.md` / `whitepaper_investor.md` / `CLAUDE.md` / `agent_docs/config_reference.md` in the same commit. Run `make doc-drift` (alias for `python3 scripts/audit/doc_drift_check.py`).
- **`bot/` implementation rules** (torch threading + `_thread_env` import ordering, `cal_mlp` feature-transform lock-step (drift surface in `scripts/cal_mlp/` + canonical helper `bot/helpers/derived_features.py`; Sprint A.1a 2026-05-12 RCA-refreshed), cell-block `filter_stage` string literals, SQLite WAL pragmas + ≤50-row commit batches, `_shadow_diag` schema chain, engine→CalEngine one-commit wiring, `discover_active_windows()`/`product_type` cross-checks, shadow-strategy add workflow): see `bot/CLAUDE.md`. Auto-loads when working inside `bot/`.

## Anti-patterns

- Don't carve new `bot/<subpackage>/` layers or relocate code across the existing modularization tree outside the planned modularization track (Sprint 4-9 CLOSED at Bit 9.3-iii.c milestone — bot/_impl.py DELETED 2026-05-11; Sprint 10 sibling-reorg also done). Engines (spx/weather/sports/analyst) run as separate threads/processes — that's the only acceptable runtime split.
- Don't add async. Synchronous + threading for WS feeds is the design.
- Don't switch from SQLite. Single-writer + local-to-VPS latency is the right choice.
- Don't switch from JSONL journals. Append-only, zero-overhead, daily cron rotation.
- Don't refactor for readability during a bug fix. Fix the bug.
- Don't change Kelly fraction, blend weights, or edge thresholds without data.
- Don't write tests unsolicited. Regression tests after bug fixes only.

## Skill routing

When a user request fits a skill, prefer the skill over ad-hoc work.

| Intent | Skill |
|---|---|
| "How's it going?" / 30s pulse | `/status` |
| Statistically rigorous numbers w/ CIs | `/audit` |
| Anomaly, alert, suspected bug | `/investigate` |
| Push to main + verify | `/deploy` |
| NULLs / data gaps / coverage | `/data-health` |
| Cross-system funnel, "where's the alpha?" | `/alpha-audit` |
| All 5 shadow systems at a glance | `/shadow` |
| Specific variants (A1/A2, hourly alts) w/ Kelly PnL | `/variant-status` |
| 15M / hourly / SPX / weather / sports deep dive | `/15m-alpha`, `/hourly-alpha`, `/spx-alpha`, `/weather-alpha`, `/sports-alpha` |
| Maker vs taker opportunity cost | `/maker-cost` |
| NO-side data | `/no-side` |
| Weekend/overnight discount status | `/weekend-discount` |
| Compile data for external researcher | `/research-package` |
| Scaffold a failing TDD test before bot/ extraction | `/test-writer` |
| KB health check | `/kb-lint` |
| KB capture session findings | `/kb-ingest` |
| KB structural maintenance | `/kb-evolve` |

**Disambiguation:** Status = quick. Audit = rigorous. Alpha-audit = cross-system funnel. *-alpha = single-system grid search. **Two-file-mode flag** (`AGENTS.md` is a symlink to this file): trigger + GO/NO-GO in `kb/decisions/bit-1.3-agents-md-shipped-may06.md`.
