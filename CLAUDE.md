# Kalshi Crypto Trading Bot

Cryptocurrency prediction market bot for Kalshi. Trades 15-minute above/below windows on BTC, ETH, SOL, XRP. Hourly markets and weather/sports/SPX scan in observation mode.

## Reference docs (read on demand)

Listed here so Claude knows when to pull them. They cost zero tokens until read.

- `agent_docs/current_state.md` — what's live, what's shadow, what's disabled. Refresh-target.
- `agent_docs/config_reference.md` — every constant in `bot.py` with data justifications.
- `agent_docs/db_schema.md` — `state.db` schema for all tables.
- `agent_docs/bot_layout.md` — bot.py line ranges + project file map.
- `agent_docs/calibration_pipeline.md` — calibration, hourly three-layer, three-commit rule.
- `kb/_index.md` — design decisions, postmortems, strategy specs (read for any deep "why" question).
- `kb-research/_index.md` — compiled research findings.

## Interaction rules

- **Answer first, plan later.** For investigations (loss, alert, anomaly), give numbers first. No plan mode, no code exploration before answering.
- **Don't re-plan finalized plans.** Continuing from a prior session = start implementing.
- **Don't deploy without explicit confirmation.** Always present the change summary; wait for approval before `git push`.

## Critical rules

Each links to the postmortem in `kb/failures/` for full context. The rule itself fires here.

- **bot.py is sacred.** systemd → `start.sh` → `bot.py`. Don't rename or split.
- Never commit `.env` or `*.jsonl` (gitignored).
- Syntax-check before commit: `python3 -c "import ast; ast.parse(open('bot.py').read())"`
- Pushing to main auto-deploys. Always verify the VPS pulled the new commit hash.
- Data-driven changes only. No config tuning without backing data.
- After signature changes: grep all call sites. `ast.parse` won't catch unbound names.
- After constant changes in `bot.py`: grep across the repo, especially `market_config.py` (asserts at startup → crash loop on mismatch).
- After changes to `discover_active_windows()` or `product_type` assignments: grep every `window.get("product_type")` in `scan()`.
- Adding keys to `_shadow_diag`: also update `insert_rejection()` + `insert_evaluated_opportunity()` signatures + SQL.
- New `sqlite3.connect()`: set `PRAGMA journal_mode=WAL` + `PRAGMA busy_timeout=10000`. Multi-thread access shares `state.db`.
- WAL checkpoints: use `PASSIVE`, never `TRUNCATE`. TRUNCATE creates deadlocks with concurrent readers.
- DB write batches: ≤50 rows per commit. Larger holds the write lock long enough to deadlock readers + checkpoints.
- Don't commit inside loops — accumulate writes, commit once at the end.
- Engine → CalEngine wiring ships in ONE commit: engine `INSERT` adds `raw_prob`, settlement routes to the right CalEngine, audit script checks for observations. (`agent_docs/calibration_pipeline.md`)
- Performance analysis filters to current config regime. Pre-regime data is misleading.
- After deploy: verify expected DB rows are being created (e.g., stc_shadow when STC 300-600s, weather_observation when weather is on). "Service running, no errors" is not enough.
- Investigate before explaining. Look at actual data, not assumptions about it.
- Verify schema before querying: `PRAGMA table_info()` and `SELECT DISTINCT`.
- After bug fixes: root-cause it, write a regression test, draft a postmortem (`kb/failures/`). Never just fix and move on.
- Sim PnL and counterfactuals use actual Kelly sizing. Never flat 1-contract.
- Dashboard changes: `dashboard_snapshot.py` and `dashboard/index.html` (gh-pages) ship in the same commit per `kb/decisions/dashboard-overhaul-plan.md`.
- Doc drift: when changing config values, update README.md / whitepaper.md / whitepaper_investor.md / CLAUDE.md / `agent_docs/config_reference.md` in the same commit. Run `python3 scripts/doc_drift_check.py`.
- **Don't import torch directly in bot.py.** Cal_mlp is the single torch entry point via `scripts/cal_mlp/integration.py`, which constrains threads at module-import time. AND: `import _thread_env` must remain the FIRST import in bot.py — numpy/scipy C extensions cache OpenBLAS thread count at load time, so OMP_NUM_THREADS=1 has to be in os.environ before they import. Direct `import torch` or any reorder defeats the contention fix (postmortem: production incident 2026-04-29, scan loop ballooned to 7.75s, 0 candidates in 5 min). AST regression: `tests/test_cal_mlp_invariants.py::test_thread_env_imported_before_numerical_libs_in_bot_py`.

## Anti-patterns

- Don't refactor `bot.py` into multiple files. Engines (spx/weather/sports/analyst) run as separate threads/processes — that's the only acceptable split.
- Don't add async. Synchronous + threading for WS feeds is the design.
- Don't switch from SQLite. Single-writer + local-to-VPS latency is the right choice.
- Don't switch from JSONL journals. Append-only, zero-overhead, daily cron rotation.
- Don't refactor for readability during a bug fix. Fix the bug.
- Don't change Kelly fraction, blend weights, or edge thresholds without data.
- Don't write tests unsolicited. Regression tests after bug fixes only.

## Workflows

### Investigate a loss or anomaly
1. Query `state.db` for the trade(s): entry, settlement, PnL, fees, STC, asset, product_type.
2. Pull `raw_prob`, `calibrated_prob`, `blended_prob` from `evaluated_opportunities`.
3. Verify settlement against actual price data.
4. Decide: config issue, model issue, or variance.
5. Numbers first, then offer next steps.

### Performance analysis
1. Identify current config regime (`git log` major config changes).
2. Filter `settled_trades` to current regime only.
3. Use actual Kelly sizing.
4. Report n / W-L / WR / total PnL / PnL per trade / Brier (if applicable).
5. Break down by asset / STC zone / price bucket.

### Add a shadow strategy
1. Shadow flag constant (e.g. `NEW_FEATURE_SHADOW = True`).
2. Wire into `scan()`; log to `evaluated_opportunities` with the right `filter_stage`.
3. New DB columns: update INSERT + signature + SQL in same commit.
4. Add a metric to `dashboard_snapshot.py`.
5. Shadow only — don't promote without explicit instruction.

### Deploy a change
1. Make the edit.
2. Syntax-check `bot.py`.
3. Grep call sites if signatures changed; grep constants across files.
4. Present a change summary — wait for approval.
5. `git add` + `commit` + `push` (triggers auto-deploy).
6. Verify VPS pulled the commit hash.
7. Verify expected DB rows are appearing.

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
