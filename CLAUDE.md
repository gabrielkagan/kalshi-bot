# Kalshi Crypto Trading Bot

Cryptocurrency prediction market bot for Kalshi. Trades 15-minute above/below windows on BTC, ETH, SOL, XRP. Hourly markets and weather/sports/SPX scan in observation mode.

## Reference docs (read on demand)

- `agent_docs/current_state.md` — what's live, what's shadow, what's disabled. Refresh-target.
- `agent_docs/config_reference.md` — every constant in `bot/_impl.py` with data justifications.
- `agent_docs/db_schema.md` — `state.db` schema for all tables.
- `agent_docs/bot_layout.md` — bot/_impl.py line ranges + project file map.
- `agent_docs/calibration_pipeline.md` — calibration, hourly three-layer, three-commit rule.
- `kb/_index.md` — design decisions, postmortems, strategy specs (read for any deep "why" question).
- `kb-research/_index.md` — compiled research findings.
- Package-level guides auto-load when working in-dir: `tests/CLAUDE.md`, `scripts/CLAUDE.md`, `ops/CLAUDE.md` (and `bot/CLAUDE.md` once Sprint 2 Bit 2.2 ships — Bit 2.1a renamed `bot.py` → `bot/_impl.py`; entrypoint is `bot/__main__.py`).

## Interaction rules

- **Answer first, plan later.** For investigations (loss, alert, anomaly), give numbers first. No plan mode, no code exploration before answering.
- **Don't re-plan finalized plans.** Continuing from a prior session = start implementing.
- **Don't deploy without explicit confirmation.** Always present the change summary; wait for approval before `git push`.

## Critical rules

Each one-liner fires here; rationale + history live in `kb/failures/` postmortems.

- **`bot/__main__.py` is the runtime entrypoint; `bot/_impl.py` is the body.** systemd → `ops/kalshi-bot.service` → `start.sh` → `python -m bot` → `bot/__main__.py` → `bot/_impl.py`. Source of truth = `ops/`. Logic moves out per the modularization track (Sprints 3-9); the entrypoint file stays at `bot/__main__.py` for the remainder of the modularization track.
- Never commit `.env` or `*.jsonl` (gitignored). KB files (`kb/`, `kb-research/`) are local-only by convention — don't `git add` new files there (existing tracked entries are pre-rule legacy).
- Syntax-check before commit: `make ast-check` (alias for `python3 -c "import ast; ast.parse(open('bot/_impl.py').read())"`).
- Pushing to main auto-deploys. Always verify the VPS pulled the new commit hash.
- Data-driven changes only. No config tuning without backing data.
- After signature changes: grep all call sites. `ast.parse` won't catch unbound names.
- After constant changes in `bot/_impl.py`: grep across the repo, especially `market_config.py` (asserts at startup → crash loop on mismatch).
- Performance analysis filters to current config regime. Pre-regime data is misleading.
- After deploy: verify expected DB rows are being created (e.g., `stc_shadow` when STC 300-600s, `weather_observation` when weather is on). "Service running, no errors" is not enough.
- Investigate before explaining. Look at actual data, not assumptions about it.
- Verify schema before querying: `PRAGMA table_info()` and `SELECT DISTINCT`.
- After bug fixes: root-cause it, write a regression test, draft a postmortem in `kb/failures/`. Never just fix and move on.
- Sim PnL and counterfactuals use actual Kelly sizing. Never flat 1-contract.
- Dashboard changes: `dashboard_snapshot.py` and `dashboard/index.html` (gh-pages) ship in the same commit per `kb/decisions/dashboard-overhaul-plan.md`.
- Doc drift: when changing config values, update `README.md` / `whitepaper.md` / `whitepaper_investor.md` / `CLAUDE.md` / `agent_docs/config_reference.md` in the same commit. Run `make doc-drift` (alias for `python3 scripts/doc_drift_check.py`).
- **`bot/_impl.py` implementation rules** (torch threading + `_thread_env` import ordering, `cal_mlp` four-site lock-step, cell-block `filter_stage` string literals, SQLite WAL pragmas + ≤50-row commit batches, `_shadow_diag` schema chain, engine→CalEngine one-commit wiring, `discover_active_windows()`/`product_type` cross-checks, shadow-strategy add workflow): see `agent_docs/bot-claude-md-draft.md`. Sprint 2 Bit 2.2 promotes this draft to `bot/CLAUDE.md`, after which it auto-loads when working inside `bot/`.

## Anti-patterns

- Don't refactor `bot/_impl.py` into multiple files outside the planned modularization track. Engines (spx/weather/sports/analyst) run as separate threads/processes — that's the only acceptable split.
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
| KB health check | `/kb-lint` |
| KB capture session findings | `/kb-ingest` |
| KB structural maintenance | `/kb-evolve` |

**Disambiguation:** Status = quick. Audit = rigorous. Alpha-audit = cross-system funnel. *-alpha = single-system grid search.

**Two-file-mode flag (Bit 1.3, forward-looking):** `AGENTS.md` is a symlink to this file. If Claude-Code-specific content here (skill routing, hook references) grows past what makes sense in a portable file, see `kb/decisions/bit-1.3-agents-md-shipped-may06.md` commitment 2 for the GO/NO-GO trigger to flip to two-file mode.
