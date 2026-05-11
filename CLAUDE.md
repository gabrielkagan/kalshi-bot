# Kalshi Crypto Trading Bot

Cryptocurrency prediction market bot for Kalshi. Trades 15-minute above/below windows on BTC, ETH, SOL, XRP live; HYPE and DOGE in shadow observation (T1 2026-05-10). Hourly markets and weather/sports/SPX scan in observation mode.

## Reference docs (read on demand)

- `agent_docs/current_state.md` — what's live, what's shadow, what's disabled. Refresh-target.
- `agent_docs/config_reference.md` — every constant in `bot/constants.py` (extracted from `bot/_impl.py` per Bit 3.1; re-exported via `from bot.constants import *`) with data justifications.
- `agent_docs/db_schema.md` — `state.db` schema for all tables.
- `agent_docs/bot_layout.md` — bot/_impl.py line ranges + project file map.
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

- **`bot/__main__.py` is the entrypoint shim — sacred boundary, no logic.** Logic lives in `bot/<subpackage>/<module>.py` (e.g., `bot/main_loop.py`, `bot/scanner/__init__.py`, `bot/executor.py`, `bot/settlement.py`, `bot/order_flow.py`, `bot/orphan_db_watchdog.py`, `bot/boot.py`, `bot/engines/{volatility,probability,calibration}.py`, `bot/feeds/`, `bot/fetchers/`, `bot/helpers/`, `bot/notifier.py`, `bot/logger.py`, `bot/state.py`, `bot/kalshi_client.py`). Runtime chain: systemd → `ops/kalshi-bot.service` → `start.sh` → `python -m bot` → `bot/__main__.py` → `bot.main_loop.MainLoop` (with `import bot._thread_env` firing FIRST so OMP_NUM_THREADS=1 is set before numpy loads transitively). Source of truth for runtime config = `ops/`. The residual `bot/_impl.py` shim (591 LOC post-Bit-9.3-iii.b — re-exports + breadcrumbs + 23-line residual-shim docstring; boot-time bindings + cal_mlp warmup relocated to `bot/boot.py` in 9.3-iii.a) is scheduled for full deletion in Bit 9.3-iii.c; **post-Bit-9.3-iii.b (2026-05-11) the `_BotProxy` is RETIRED — `bot/__init__.py` is now docstring-only and `bot.X` reads must use canonical submodules directly (e.g., `bot.constants.X`, `bot.main_loop.MainLoop`, `bot.state.StateManager`).** bot/_impl.py is NOT the body and is no longer in the production import chain.
- Never commit `.env` or `*.jsonl` (gitignored). KB files (`kb/`, `kb-research/`) are local-only by convention — don't `git add` new files there (existing tracked entries are pre-rule legacy).
- Syntax-check before commit: `make ast-check` (alias for `python3 -c "import ast; ast.parse(open('bot/_impl.py').read())"`).
- Pushing to main auto-deploys. Always verify the VPS pulled the new commit hash.
- Data-driven changes only. No config tuning without backing data.
- After signature changes: grep all call sites. `ast.parse` won't catch unbound names.
- After constant changes in `bot/constants.py` (or any remaining underscore-prefixed constants in `bot/_impl.py`): grep across the repo, especially `market_config.py` (asserts at startup → crash loop on mismatch).
- Performance analysis filters to current config regime. Pre-regime data is misleading.
- After deploy: verify expected DB rows are being created (e.g., `stc_shadow` when STC 300-600s, `weather_observation` when weather is on). "Service running, no errors" is not enough.
- Investigate before explaining. Look at actual data, not assumptions about it.
- Verify schema before querying: `PRAGMA table_info()` and `SELECT DISTINCT`.
- After bug fixes: root-cause it, write a regression test, draft a postmortem in `kb/failures/`. Never just fix and move on.
- **Equivalence snapshots are never auto-regenerated.** `tests/equivalence/` (Pillar 3) pins engine outputs against a 1000-row corpus. If a snapshot fails, **investigate the divergence** — never run `pytest --force-regen` autonomously. Regen is a human-with-diff-review operation; see `tests/equivalence/REGEN.md`.
- Sim PnL and counterfactuals use actual Kelly sizing. Never flat 1-contract.
- Dashboard changes: `dashboard_snapshot.py` and `dashboard/index.html` (gh-pages) ship in the same commit per `kb/decisions/dashboard-overhaul-plan.md`.
- Doc drift: when changing config values, update `README.md` / `whitepaper.md` / `whitepaper_investor.md` / `CLAUDE.md` / `agent_docs/config_reference.md` in the same commit. Run `make doc-drift` (alias for `python3 scripts/doc_drift_check.py`).
- **`bot/_impl.py` implementation rules** (torch threading + `_thread_env` import ordering, `cal_mlp` four-site lock-step, cell-block `filter_stage` string literals, SQLite WAL pragmas + ≤50-row commit batches, `_shadow_diag` schema chain, engine→CalEngine one-commit wiring, `discover_active_windows()`/`product_type` cross-checks, shadow-strategy add workflow): see `bot/CLAUDE.md`. Auto-loads when working inside `bot/`.

## Anti-patterns

- Don't carve new `bot/<subpackage>/` layers or relocate code across the existing modularization tree outside the planned modularization track (Sprint 4-9 done; Sprint 10 sibling-reorg + Bit 9.3-iii cleanup still pending). Engines (spx/weather/sports/analyst) run as separate threads/processes — that's the only acceptable runtime split. The residual `bot/_impl.py` shim is scheduled for deletion in Bit 9.3-iii; do not add new code to it.
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
