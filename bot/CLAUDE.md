# bot/ — implementation rules

These rules apply to the bot runtime — primarily `bot/main_loop.py`,
`bot/scanner/__init__.py`, `bot/executor.py`, `bot/settlement.py`,
`bot/state.py`, `bot/order_flow.py`, `bot/boot.py`, `bot/runtime_config.py`,
and the engine modules (`bot/engines/spx_engine.py`,
`bot/engines/weather_engine.py`, `bot/engines/sports_engine.py`,
`bot/shadows/fifteenm_shadow.py`, `bot/ai/analyst.py`). **Bit 9.3-iii.c (2026-05-11)
DELETED `bot/_impl.py`** — the rules historically attributed to that file
now apply cross-cutting to the bot/ package. The first seven sections
(Threading, cal_mlp feature-transform lock-step, Cell-block, SQLite, `_shadow_diag`,
Engine→CalEngine, `discover_active_windows()`) are
**implementation-specific** — they only matter when editing the
runtime, not when running audits or working in `tests/` /
`scripts/`. The Cell-block section additionally has cross-cutting
reach beyond `bot/` and is duplicated as a one-liner in
`scripts/CLAUDE.md`. The trailing **Workflows** section includes
prose long-forms of cross-cutting skills (`/investigate`, `/audit`,
`/deploy`) that route through the bot package runtime state; the canonical
surface for those is the matching skill, and this section is a backup
readable here for agents working inside `bot/`.

## Threading + numerical libraries (sacred ordering)

- **Don't import torch directly anywhere under `bot/`.** `cal_mlp` is the
  single torch entry point via `scripts/cal_mlp/integration.py`,
  which constrains threads at module-import time. Enforced by the
  `bot-no-torch` import-linter contract (Bit 12.3).
- **`import bot._thread_env` must run BEFORE any numerical-library load
  in the bot package import chain.** The canonical entry is
  `bot/__main__.py`, which top-imports `bot._thread_env` as the FIRST
  non-stdlib import before `from bot.main_loop import MainLoop`. The
  `bot/boot.py` clean-leaf module also defensively `__import__("bot._thread_env")`s
  before scripts/cal_mlp/integration loads numpy/scipy/torch transitively.
  numpy/scipy C extensions cache OpenBLAS thread count at load time,
  so `OMP_NUM_THREADS=1` has to be in `os.environ` before they
  import. Direct `import torch` or any reorder defeats the
  contention fix. Postmortem: production incident 2026-04-29 (scan
  loop ballooned to 7.75s, 0 candidates in 5 min) →
  `kb/failures/cal-mlp-torch-thread-contention-apr29.md`. AST
  regression:
  `tests/integration/test_cal_mlp_invariants.py::test_thread_env_imported_before_numerical_libs_in_bot_boot` (the sister `..._in_bot_impl` test self-skips post-Bit-9.3-iii.c since bot/_impl.py was deleted).

## cal_mlp feature transforms (lock-step)

Any change to a feature transform — winsorize cap
`SIGMA_WINSOR_ABS_CAP=25.0`, `hour_sin`/`hour_cos` derivation,
`prob_breakeven_gap` formula, sigma derivation — must keep all
surfaces in lock-step in ONE commit. RCA refresh 2026-05-12 (Sprint
A.1a RCA closeout; ticket `86b9vejnq`) revised the surface from the
historical "four-site" framing to its actual shape, and **Sprint A.1b
(ticket `86b9veppa`, 2026-05-12) closed the inline-drift surface** by
routing all tracked cal_mlp sites through canonical helpers.

**Drift surface — 4 tracked drift sites + 1 helper home (post-A.1b: all 4 call canonical helpers):**

Drift sites (pinned by `HOUR_SINCOS_DRIFT_SITES` in `tests/contracts/test_calmlp_lockstep.py`):
1. `scripts/cal_mlp/extract_data.py` — `hour_sin/cos` via `features.compute_hour_features`; reads sigma + breakeven from DB
2. `scripts/cal_mlp/post_hoc_processor.py` — `hour_sin/cos` via `features.compute_hour_features`; reads sigma + breakeven from DB
3. `scripts/cal_mlp/integration.py` — `hour_sin/cos` via `features.compute_hour_features`; sigma + breakeven via `bot.helpers.derived_features.compute_derived_features` + `features.apply_sigma_winsor` (mirrors the train-time DB-write path at `bot/state.py:1713`/`2010`)
4. `scripts/cal_mlp/sim_pnl.py` — `hour_sin/cos` via `features.compute_hour_features` (sister tracked site, added during A.1a R2 adv-review 2026-05-12; train-side sim-PnL backbone)

Helper home (not a drift site — owning the formula IS the canonical change vehicle):
- `scripts/cal_mlp/features.py` — `SIGMA_WINSOR_ABS_CAP` + `apply_sigma_winsor` + `compute_hour_features` home; `compute_cfg_fp` captures sigma_winsor_abs_cap

Untracked dev artifacts (NOT in HOUR_SINCOS_DRIFT_SITES; ticket `86b9wjd3e` pending to track-or-delete):
- `scripts/cal_mlp/backfill_offline.py`
- `scripts/cal_mlp/mac_diagnostics/v2_live_audit/score_live_ws.py`

**Canonical helper home:**

- `bot/helpers/derived_features.py::compute_derived_features` — owns
  `spot_distance_to_strike_sigma` + `prob_breakeven_gap`. Extracted
  in Bit 3.2 (2026-05-08); allowed by `.importlinter` Contract 4
  (helpers-leaf). A.1b (2026-05-12) routed `scripts/cal_mlp/integration.py`
  inline formulas through this helper.
- `bot/helpers/derived_features.py::compute_hour_sin_cos` — scalar
  hour-of-day cyclic encoding (Bit B.1a, 2026-05-12). Mirrored by
  `scripts/cal_mlp/features.compute_hour_features` (A.1b) which accepts
  scalar OR Series for DataFrame-side extract paths.

**Helper-call sites (preserve when editing):**

- `bot/state.py:1713` + `:2010` — pre-DB-write `compute_derived_features` calls (+ `:1723` `apply_sigma_winsor` on the returned sigma); replaces the pre-Bit-9.3-iii.c `bot/_impl.py:2192` site
- `bot/engines/sports_engine.py` — 2 call sites for sports-engine evals
- `scripts/backfill/backfill_extended_features.py` — backfill script (pre-B.1a evaluated_opportunities Tier 4/5)
- `scripts/backfill/wave1_derived_cols.py` — backfill script (B.1a-fu2, 2026-05-12; replays B.1a auto-fill on `rejected_opportunities` Wave 1 derivable cols + `evaluated_opportunities.prob_breakeven_gap`)
- `scripts/backfill/hype_doge_replay_backfill.py` — HYPE/DOGE Phase 2 replay backfill (86b9wy7v3, 2026-05-12); per-market `replay_market()` calls `compute_hour_sin_cos` + `compute_derived_features` + `apply_sigma_winsor` for the lock-step features written to `historical_replay_calmlp`
- `scripts/cal_mlp/integration.py` — serve-path `should_block_tm96` (post-A.1b)

Splitting → train/serve skew (model trained on one distribution,
served from another). See `agent_docs/calibration_pipeline.md` "cal_mlp
feature transforms (lock-step)" for the rationale.
Regression tests:
`tests/integration/test_calmlp_sigma_winsorize.py` (single-anchor behavior) +
`tests/integration/test_calmlp_tm96_gate.py` (integration) +
`tests/contracts/test_calmlp_lockstep.py` (cross-site AST + runtime parity guard,
Sprint A.1a 2026-05-12).

## Cell-block activations deflate `filter_stage='candidate'` rollups

Audit + dashboard scripts that query `WHERE filter_stage = 'candidate'`
for "all 15M trades" totals **under-count post-activation**. The
actual `filter_stage` VALUES (string literals stored in DB — NOT
Python constant names) are:

- `'96C_SOL_XRP_STC_DANGER_BAND'` (HPSB)
- `'TM98_97_98C_2_5MIN_BLEED'`
- `'SOL_TAKER_85_89C_2_5MIN_BLEED'`
- `'SOL_BLEED_V2_88_93C_2_5MIN'`

Canonical 5-set (`'candidate'` baseline + these 4 bleed stages) lives
in `bot.helpers.cohort_attribution.COHORT_PARTITION_STAGES` (Money
Printer Roadmap P1.1, ticket `86b9x3kgd`, 2026-05-12) — extend that
constant when adding a new cell-block stage and the cohort
aggregator + dashboard + alerts + weekly report pick it up
automatically.

To re-aggregate true total candidate volume, UNION these stage
values. **Pattern:** any script that filters
`WHERE filter_stage = 'candidate'` (or
`IN ('candidate', 'observation_trade')`) on 15M-scoped queries.
Identify via:
`grep -rn "filter_stage[ =]*[='IN ]*candidate" scripts/ *.py .claude/`.

Confirmed-affected (Apr 30): `scripts/audit/15m_live_audit.py`,
`scripts/audit/15m_alpha_research.py` (`/15m-alpha`),
`scripts/audit/alpha_audit.py` (`/alpha-audit`),
`scripts/audit/data_health_monitor.py`, `scripts/ops/generate_whitepaper_stats.py`,
`scripts/audit/maker_opportunity_cost.py`, `scripts/audit/quiet_market_monitor.py`,
`bot/snapshots/dashboard_snapshot.py` (Sprint 10.4, 2026-05-12), `bot/ai/analyst.py`, `bot/ai/auditor.py`,
`bot/ai/researcher.py` (Sprint 10.3, 2026-05-12), `.claude/skills/status/SKILL.md` (`/status` skill).
Decision doc: `kb/decisions/bleed-cell-blocks-2026-04-30.md`.

## SQLite (WAL, pragmas, batch sizes)

Multi-thread access shares `state.db`. Single-writer is the design.

- New `sqlite3.connect()`: set `PRAGMA journal_mode=WAL` +
  `PRAGMA busy_timeout=10000`. Catch the contention bugs early.
- WAL checkpoints: use `PASSIVE`, never `TRUNCATE`. TRUNCATE creates
  deadlocks with concurrent readers. Postmortem:
  `kb/failures/database-contention.md`.
- DB write batches: ≤50 rows per commit. Larger holds the write lock
  long enough to deadlock readers + checkpoints.
- Don't commit inside loops — accumulate writes, commit once at the end.

## Band-calibrated sizing (P4.1)

**15M `_sizer.compute()` receives `calibrated_prob_for_sizing(...)`,
NOT bare `final_prob`.** Helper home: `bot/helpers/band_calibration.py`
(42-cell empirical lookup with hierarchical shrinkage k=30 toward
band-aggregate prior; 30d/60d hybrid window per band; baseline frozen
2026-05-17 — `agent_docs/p4_1_calibration_baseline.md`). Trade-selection
gates upstream still use `final_prob` unchanged — **only Kelly magnitude
changes**. Out of scope: V2 path (`_v2_prob`), NO-side (`no_prob`),
hourly / SPX / weather (helper short-circuits to `raw_prob`). Pinned
by `tests/contracts/test_p4_1_band_calibrated_sizing.py` (AST guard +
42-cell value pin + V2/NO-still-unwrapped negative tests).

## `_shadow_diag` schema chain

Adding keys to `_shadow_diag`: also update
`insert_rejection()` + `insert_evaluated_opportunity()` signatures + SQL.
All schema-chain sites ship in one commit, otherwise the new key gets dropped
silently at write time.

## `config_snapshot_id` schema chain (ticket 86b9zkp8p, 2026-05-17)

Per-decision config snapshot — every `evaluated_opportunities` +
`rejected_opportunities` row carries a FK to `config_snapshots(id)` that
captures EXACTLY which config produced the decision (sha256 over
`bot/constants.py` + `bot/config.py` + `market_config.py` + sorted-key JSON of
tracked env-var flags + git HEAD). Replay = look up the snapshot →
restore the exact config → re-run.

Schema-chain sites that ship in ONE commit (same discipline as `_shadow_diag`):

1. `bot/helpers/config_snapshot.py` — `compute_config_snapshot()` returns the
   6-field bundle; `persist_config_snapshot(conn)` inserts or looks up the
   row, returns `id`. Helper-leaf module (stdlib only — reads the three
   config files as FILE CONTENTS via sha256, not as Python imports).
2. `bot/state.py::_create_tables` — `CREATE TABLE config_snapshots` + index +
   `ALTER TABLE evaluated_opportunities ADD COLUMN config_snapshot_id INTEGER` +
   matching `rejected_opportunities` ALTER + FK indexes on both tables.
3. `bot/state.py::insert_evaluated_opportunity` + `insert_rejection` —
   `config_snapshot_id: Optional[int] = None` kwarg + INSERT column + VALUES
   placeholder + (eval-only) `COALESCE(...)` in the ON CONFLICT DO UPDATE so
   the FIRST snapshot stamp on a row survives subsequent UPSERTs.
4. `bot/main_loop.py::MainLoop.__init__` — `self.config_snapshot_id =
   persist_config_snapshot(self.state.conn)` after StateManager init. Phase-1
   captures ONCE per process boot; mid-day mutation re-capture is Phase-2.
5. `bot/scanner/__init__.py` — EVERY `insert_evaluated_opportunity(...)` and
   `insert_rejection(...)` call passes
   `config_snapshot_id=self._ml.config_snapshot_id` (mirrors the existing
   `self._ml.X` constructor-injected attribute pattern documented in
   `bot/scanner/CLAUDE.md`). 69 call sites today.
6. `tests/contracts/test_config_snapshot.py` — full chain pin: table exists,
   columns exist, signatures accept kwarg, helper produces stable hashes,
   hash rotates on file/env drift, persist returns existing id on dup hash,
   round-trip via insert + JOIN reproduces the env bundle, AST guard verifies
   every scanner call site passes the kwarg, AST guard verifies helper-leaf
   rule, AST guard verifies `MainLoop.__init__` calls the helper.
7. `tests/fixtures/state_db_schema_baseline.txt` — append `config_snapshots`
   table section + bump `evaluated_opportunities` (142 → 143 cols, FK at
   cid=135 just before the cal_mlp_* cols added later by
   `_calmlp_migrate_schema`) + `rejected_opportunities` (36 → 37) + add FK
   index entries. Bit 7.1 baseline-as-snapshot discipline.

Splitting → new column gets silently dropped at write time (the prior
`_shadow_diag` failure class). Phase-2 followup (filed at ship time): when
a tracked env var or constant changes mid-process, the current snapshot
becomes stale; Phase-2 adds a periodic re-hash + INSERT OR IGNORE per scan
tick if the hash changed.

## Engine → CalEngine wiring (one-commit rule)

Engine → CalEngine wiring ships in ONE commit:

1. Engine `INSERT` adds `raw_prob`.
2. Settlement routes to the right `CalEngine`.
3. Audit script checks for observations.

Splitting → engine writes start producing rows the calibrator never
sees, or the calibrator runs against rows the engine never wrote.
See `agent_docs/calibration_pipeline.md`.

## `discover_active_windows()` / `product_type` cross-checks

After changes to `discover_active_windows()` or `product_type`
assignments: grep every `window.get("product_type")` in `scan()`. The
two sides must stay in sync — a new product_type that scan() doesn't
know about silently drops the window.

## Workflows (bot/ runtime changes — historical bot/_impl.py rules now cross-cutting)

### Add a shadow strategy
1. Shadow flag constant (e.g. `NEW_FEATURE_SHADOW = True`).
2. Wire into `scan()`; log to `evaluated_opportunities` with the right
   `filter_stage`.
3. New DB columns: update INSERT + signature + SQL in same commit.
4. Add a metric to `bot/snapshots/dashboard_snapshot.py`.
5. Shadow only — don't promote without explicit instruction.

### Investigate a loss or anomaly (the long form behind /investigate)
1. Query `state.db` for the trade(s): entry, settlement, PnL, fees,
   STC, asset, product_type.
2. Pull `raw_prob`, `calibrated_prob`, `blended_prob` from
   `evaluated_opportunities`.
3. Verify settlement against actual price data.
4. Decide: config issue, model issue, or variance.
5. Numbers first, then offer next steps.

### Performance analysis (the long form behind /audit)
1. Identify current config regime (`git log` major config changes).
2. Filter `settled_trades` to current regime only.
3. Use actual Kelly sizing.
4. Report n / W-L / WR / total PnL / PnL per trade / Brier.
5. Break down by asset / STC zone / price bucket.

### Deploy a change (the long form behind /deploy)
1. Make the edit.
2. Syntax-check the runtime hotspots `bot/constants.py` + `bot/main_loop.py` + `bot/scanner/__init__.py` via `make ast-check` (Bit 9.3-iii.c: bot/_impl.py was DELETED; the ast-check target was retargeted to the canonical submodules — see Makefile `ast-check`). For changes to `bot/helpers/*.py`, `bot/logger.py`, `bot/notifier.py`, `bot/kalshi_client.py`, `bot/fetchers/*.py`, `bot/feeds/*.py`, or `bot/engines/*.py`, the full pytest suite covers transitively (no per-file ast-check target as of Sprint 6).
3. Grep call sites if signatures changed. Constants live in `bot/constants.py` (Bit 3.1, re-exported via `from bot.constants import *`); helpers live in `bot/helpers/*.py` (Bit 3.2, re-exported via `from bot.helpers import *` plus explicit underscore re-exports for `validators` and `breakers`); `Logger` lives in `bot/logger.py` (Bit 4.1, re-imported via `from bot.logger import Logger`); `TelegramNotifier` lives in `bot/notifier.py` (Bit 4.2, re-imported via `from bot.notifier import TelegramNotifier`); `KalshiClient` lives in `bot/kalshi_client.py` (Bit 4.3, re-imported via `from bot.kalshi_client import KalshiClient`); `DeribitDVOLFetcher` and `CoinGlassFetcher` live in `bot/fetchers/` (Bit 4.4, re-imported via `from bot.fetchers import DeribitDVOLFetcher, CoinGlassFetcher`); `CoinbaseFeed`, `CrossExchangeFeed`, `KalshiFeed`, and `OrderbookSchemaError` live in `bot/feeds/` (Bit 4.5a + 4.5b, re-imported via `from bot.feeds import CoinbaseFeed, CrossExchangeFeed, KalshiFeed, OrderbookSchemaError`; `KalshiFeed` (`bot/feeds/kalshi.py`) imports `OrderbookSchemaError` directly from sibling `bot/feeds/orderbook_schema.py`); `VolatilityEngine` lives in `bot/engines/volatility.py` (Bit 6.1), `ProbabilityEngine` lives in `bot/engines/probability.py` (Bit 6.2), and `CalibrationEngine` lives in `bot/engines/calibration.py` (Bit 6.3, 2026-05-10) — all three re-imported via `from bot.engines import VolatilityEngine, ProbabilityEngine, CalibrationEngine`. **Bit 6.3 path-B refactor**: in addition to moving the class, Bit 6.3 relocated the calibration runtime state — `_CALIBRATION_ENGINE` singleton + `_CAL_REGISTRY` dict + `_derive_subtype` / `_derive_asset_filter` / `_resolve_cal_engine` helpers — out of `bot/_impl.py` into `bot/engines/calibration.py` alongside the class. Both `bot/_impl.py` and `bot/engines/probability.py` now reach those names via top-level `from bot.engines import calibration as _cal_state` + `_cal_state.X` attribute access — module-attribute access pattern preserves singleton-mutation freshness without late-binding. This lifted the Bit 6.2 late-binding `from bot import _impl as _bot_impl` workaround AND removed the matching `.importlinter` `bot.engines.probability -> bot._impl` ignore_imports carve-out in the same commit. `_TELEGRAM` stays in `bot/_impl.py` (unrelated singleton). `StateManager` lives in `bot/state.py` (Bit 7.1, 2026-05-10) — re-imported via `from bot.state import StateManager`; consumer-class type annotations on `OpportunityScanner.__init__`, `OrderExecutor.__init__`, and `SettlementTracker.__init__` (`state: StateManager`) resolve through the line-109 re-export. **Bit 7.1 path-A++ refactor**: in-Bit refactor of `scripts/cal_mlp/integration.py::parity_assert(conn) -> tuple[str, int]` and `sizing_parity_assert(conn, *, rowid, compute_for_15m_main_path)` dropped their `bot_globals` parameter — the laundered-namespace coupling smell is fixed in-Bit per the modularization strategic goal of reducing code smells. The `_get_compute_for_15m_main_path()` single-name late-binding helper inside `bot/state.py` returns `bot._impl.compute_for_15m_main_path` (the closure created via `make_compute_for_15m_main_path()` — search anchor: `compute_for_15m_main_path = make_compute_for_15m_main_path`). **Bit 7.1 fu (Smell 4, ticket 86b9vhccw, 2026-05-10)**: `make_compute_for_15m_main_path` itself was subsequently refactored to drop its `bot_globals: dict` parameter — the closure now imports its 11 dependent names directly from `bot.constants` + `bot.config` (Bit 12.1, 2026-05-12 retargeted from repo-root `config`) inside the function body (mirrors path-A++ pattern), plus a literal `DRAWDOWN_HALT_FLOOR = 0.10` fallback for the one name not in either source. **Bit 7.1 fu (Smell 3, ticket 86b9vhcat, 2026-05-10)**: the `_calmlp_predictors` cache + `.warmup()` orchestration moved from `bot/_impl.py` module-level (search anchor: removed `_calmlp_predictors = {a: CalMLPPredictor(a) for a in ...}` block) to `scripts/cal_mlp/integration.py` alongside the `CalMLPPredictor` class — locality of reference, parallel to the `_POSTHOC_PROCESSOR` precedent in that module. `bot/_impl.py` imports `_calmlp_predictors` and `warmup_predictor_cache` via the existing `from integration import (...)` block at line 59, then emits the `[CALMLP] enabled=…` boot log in its own logger namespace using the `(enabled, warmed_count)` tuple the helper returns (M3 — preserves the operator-runbook grep contract). The 3 consumer sites in bot/_impl.py (2 in OpportunityScanner, 1 in MainLoop) keep their bare-name `_calmlp_predictors.get(asset)` / `predictors=_calmlp_predictors` references untouched. Kill-switch contract (R-p7-cleanroom#H2 + R-p7-coldboot#C-S2) preserved: predictor INSTANCES always constructed at integration.py module-import time; `.warmup()` gated on `CALMLP_ENABLED`; the per-call env check inside `annotate_evaluation_kwargs` and `annotate_evaluation_async_enqueue` ensures `predict()` never runs when env=0 even if the cache IS warmed. Sister Bit 7.2 (`agent_docs/db_schema.md` refresh) shipped in the same atomic commit as Bit 7.1. **Bit 9.1 (path-A++, 2026-05-10)**: `OrderExecutor` lives in `bot/executor.py` — re-imported via `from bot.executor import OrderExecutor` (line ~116 of bot/_impl.py). The previous `_get_order_executor()` late-binding helper in bot/scanner/__init__.py retired atomically; the `scanner-no-impl-toplevel` `.importlinter` contract dropped (net contracts: 6 → 5). The bot.executor ↔ bot.scanner cycle is broken from the executor side via a `_get_opportunity_scanner()` method-body helper (the 7 OpportunityScanner staticmethod call sites in OrderExecutor body — `_convert_orderbook_fp` × 2, `_best_yes_ask_cents` × 5 — go through it; a future Sprint 10 sibling-reorg Bit may relocate those staticmethods to `bot/helpers/orderbook.py` to eliminate the helper). Path-A++ relocation of `_append_raw_api_journal` to `bot/helpers/raw_api_journal.py` — eliminates late-binding need that would have required new `.importlinter` carve-outs in both Bit 9.1 and Bit 9.2. 4 latent `OpportunityScanner._best_ask_depth(...)` AttributeError sites in OrderExecutor body fixed (closes ticket 86b9vn9r5; `_best_ask_depth` is a staticmethod on OrderExecutor itself).

**Bit 9.2 (path-A++, 2026-05-10)**: `SettlementTracker` lives in `bot/settlement.py` — re-imported via `from bot.settlement import SettlementTracker, discover_active_windows` (line ~117 of bot/_impl.py). The module-level `discover_active_windows()` function ships with SettlementTracker per master plan Phase Z+AA bundle decision. The Bit 9.1 L81 alias-import RETIRED atomically. No new `.importlinter` carve-out needed — SettlementTracker has zero references to names defined below the line-117 re-export point in bot/_impl.py; clean leaf extraction. Bundled bug fix (ticket 86b9vppn3): pre-existing UnboundLocalError 'best_ask' in OpportunityScanner.scan() low_probability_15m insert_rejection branch — initialize best_ask=None at iteration start.

**Bit 9.3 (path-A method-body late-binding, 2026-05-10)**: `MainLoop` lives in `bot/main_loop.py` — re-imported via `from bot.main_loop import MainLoop` (line ~119 of bot/_impl.py). Two-step atomic per master plan L2195-2216: 9.3-i (initial ship) shipped the extraction with bot/__main__.py UNCHANGED (still `from bot._impl import MainLoop` via the proxy chain); 9.3-ii (deferred) swaps bot/__main__.py to direct `from bot.main_loop import MainLoop`. **Path-A**: bot/main_loop.py uses METHOD-BODY late-binding for the residual bot._impl names bound BELOW the line-119 re-export point. Post-Bit-9.3.5, the late-binding block inside `MainLoop.__init__` is 2 names (`_HPSB_MISSING_BLEEDERS`, `_HPSB_VALIDATOR_UNAVAILABLE_REASON`); `MainLoop.startup` covers 1 (`detect_orphan_db_holders`). NO new `.importlinter` carve-out — bot/main_loop.py has zero top-level bot._impl edge in the import graph; net contracts stays at 5. The `_telegram_state._TELEGRAM` consumer enumeration grows from 4 to 5 modules: **bot/_impl.py STAYS** (for the orphan-DB Layer-3 watchdog helpers — `_alert_orphan_db_holder` and the `detect_orphan_db_holders` lsof-not-found Telegram alert branch — the only remaining `_telegram_state._TELEGRAM` consumer block in bot/_impl.py post-MainLoop-extraction) + **bot/main_loop.py ADDS** (MainLoop reads + the singleton WRITE in `__init__`) + bot/scanner/__init__.py + bot/executor.py + bot/settlement.py. Sister Bit 9.3.5 (USER Option-B decision via AskUserQuestion) extracted the remaining `OrderFlowEngine` (122 LOC) + `KalshiOrderFlowTracker` (240 LOC) classes from bot/_impl.py to bot/order_flow.py — the two `# REMOVE BIT 9.3.5` markers in `MainLoop.__init__`'s late-binding block collapsed to a top-level `from bot.order_flow import OrderFlowEngine, KalshiOrderFlowTracker` in bot/main_loop.py.

**Bit 9.3.5 (clean leaf, 2026-05-10)**: `OrderFlowEngine` + `KalshiOrderFlowTracker` live in `bot/order_flow.py` — re-imported via `from bot.order_flow import OrderFlowEngine, KalshiOrderFlowTracker` (line ~120 of bot/_impl.py, immediately after the Bit 9.3 MainLoop re-export). Smallest extraction in Sprint 9 (~277 LOC raw class bodies; 378 LOC including header). Mirrors Bit 9.2 SettlementTracker clean-leaf shape but smaller surface: bot/order_flow.py imports only stdlib (`logging`, `time`, `collections.deque`, `typing.{Dict,List,Optional,Set,Tuple}`) and 21 explicit names from `bot.constants` — zero `_telegram_state` consumers, zero `_cal_state` consumers, zero `bot._impl`-below-line-119 references. The two `# REMOVE BIT 9.3.5` markers in `MainLoop.__init__`'s late-binding block (Bit 9.3 form) collapsed to a top-level `from bot.order_flow import OrderFlowEngine, KalshiOrderFlowTracker` in bot/main_loop.py. **Sister cleanup atomic in same commit**: bot/scanner/__init__.py `Optional["OrderFlowEngine"]` and `Optional["KalshiOrderFlowTracker"]` forward-refs UNQUOTED — bot/order_flow.py has zero bot.scanner edges so the new top-level `from bot.order_flow import` in bot/scanner/__init__.py resolves cleanly at scanner load time. .importlinter `helpers-leaf` `forbidden_modules` extended with `bot.order_flow`; net contracts stays at 5. The 5-consumer `_telegram_state._TELEGRAM` enumeration is UNCHANGED — OFE+KOFT do not emit Telegram alerts. bot/_impl.py was class-free post-9.3.5 (~767 LOC residual).

**Bit 9.3-ii (clean leaf + bot/__main__.py swap, 2026-05-10)**: orphan-DB Layer-3 watchdog block (5 functions + `_ORPHAN_DB_WATCHDOG_PATTERNS` list, ~200 LOC, was bot/_impl.py:375-578) relocated to NEW `bot/orphan_db_watchdog.py` — re-imported via `from bot.orphan_db_watchdog import (detect_orphan_db_holders, _run_lsof_for_db, _get_pid_cmdline, _alert_orphan_db_holder, _ORPHAN_DB_WATCHDOG_PATTERNS)` (line ~121 of bot/_impl.py, immediately after the Bit 9.3.5 OFE+KOFT re-export). Clean leaf: stdlib + `import bot.notifier as _telegram_state` only. 5-consumer `_telegram_state._TELEGRAM` enumeration: bot/orphan_db_watchdog.py REPLACES bot/_impl.py as the 5th consumer slot — net stays at 5. **MainLoop.startup() late-binding retargeted** from `from bot._impl import detect_orphan_db_holders` to `from bot.orphan_db_watchdog import detect_orphan_db_holders` atomically in same commit. **bot/__main__.py swap (master plan L2197)**: from `from bot._impl import MainLoop` to direct `from bot.main_loop import MainLoop`, with `import bot._thread_env` as the FIRST import (preserves the OMP_NUM_THREADS=1-before-numpy guarantee since the post-swap chain `bot/__main__.py → bot.main_loop → models → numpy` no longer routes through bot._impl's first non-stdlib import). `logging.basicConfig(force=True)` block preserved (R7 #1 — load-bearing for production journalctl structured INFO logging; subsequently hoisted to module top-level in Bit 9.3-iii.b CALMLP-boot-log fu so the `bot.boot` module-load log reaches stderr). .importlinter `helpers-leaf` `forbidden_modules` extended with `bot.orphan_db_watchdog`; net contracts stays at 5. bot/_impl.py: 767 → ~582 LOC at Bit-9.3-ii closeout (residual: 16 class/function re-exports + 2 module aliases + 4 boot-time bindings + cal_mlp boot log + comments/breadcrumbs). At Bit-9.3-ii ship time the `_BotProxy` shim in bot/__init__.py was still operational for proxy-resolved reads/writes (used by tests/integration/test_execution.py `@patch("bot.X")` sites + production kill-switch self-mutation paths); the proxy was subsequently RETIRED in Bit 9.3-iii.b (2026-05-11 — see paragraph below). **Sprint 9 main-class chunk closes here.**

**Bit 9.3-iii.a (clean-leaf boot relocation, 2026-05-11)**: the 4 boot-time bindings (`_HPSB_VALIDATOR_UNAVAILABLE_REASON`, `_HPSB_MISSING_BLEEDERS = _validate_high_price_stc_block_bleeder_strings()`, `_BLEED_BLOCK_MISSING_BLEEDERS = _validate_bleed_block_bleeder_strings()`, `compute_for_15m_main_path = make_compute_for_15m_main_path()`) + cal_mlp warmup boot log relocated to NEW `bot/boot.py` clean leaf (stdlib + bot.helpers.validators + scripts/cal_mlp/integration only; `import bot._thread_env` as defense-in-depth first non-stdlib import). bot/_impl.py preserves the surface via `from bot.boot import (...)` re-export (keeps `tests/contracts/public_api.json` byte-stable until proxy retirement in 9.3-iii.b/c). bot/main_loop.py + bot/state.py top-import from bot.boot directly — the Bit 9.3 method-body late-binding block in `MainLoop.__init__` + the Bit 7.1 `_get_compute_for_15m_main_path()` helper in bot/state.py RETIRED atomically. `.importlinter` `helpers-leaf` `forbidden_modules` extended with `bot.boot` + `state-no-impl-toplevel` contract RETIRED (3-layer Bit-7.1 anti-regression seal → stronger 1-layer "zero bot._impl edges at any scope" peer-pin); net contracts 7 → 6. bot/_impl.py: 582 → ~565 LOC. Sister sub-bit 9.3-iii.c (DELETE bot/_impl.py) remains pending.

**Bit 9.3-iii.b (proxy retirement + CALMLP-boot-log fu, 2026-05-11)**: the `_BotProxy` ModuleType subclass in `bot/__init__.py` was retired entirely. `bot/__init__.py` is now docstring-only; `bot.X` reads no longer route through `bot._impl.X` — callers must use canonical submodules directly (`bot.constants.X`, `bot.main_loop.MainLoop`, `bot.state.StateManager`, `bot.helpers.<sub>.X`, etc.). Constants that live in `bot/config.py` (MAX_RISK_PER_TRADE, MARKET_BLEND_W, HOURLY_KELLY_FRACTION, ...) reach via `bot.config.X` directly (Bit 12.1, 2026-05-12: relocated from repo root `config.py`). 91+ test files and `market_config.py` retargeted atomically. Bundled CALMLP-boot-log fu: `logging.basicConfig(force=True)` hoisted from inside `if __name__ == "__main__":` block in `bot/__main__.py` to module top-level (after `import bot._thread_env`, before `from bot.main_loop import MainLoop`) — the `[CALMLP] enabled=N at boot` log emitted during `bot.boot` module-load now reaches stderr at production runtime instead of being silent-dropped against the unconfigured root logger. Net `.importlinter` contracts: 6 → 6 (no change — proxy removal is the opposite of a carve-out). Sister sub-bit 9.3-iii.c (DELETE bot/_impl.py — MILESTONE) remains pending.

**Bit 8.1 (path-A++, 2026-05-10) — post-Bit-9.3-ii state**: `OpportunityScanner` lives in `bot/scanner/__init__.py` — re-imported via `from bot.scanner import OpportunityScanner`. The `_get_order_executor()` helper retired in Sprint 9 Bit 9.1; scanner now uses top-level `from bot.executor import OrderExecutor` directly. The `scanner-no-impl-toplevel` `.importlinter` contract dropped in Bit 9.1 (net contracts: 6 → 5). The Bit 8.1 `_TELEGRAM` singleton relocation to `bot/notifier.py` stands; post-Bit-9.3-ii the singleton is reached from FIVE consumer modules — `bot/orphan_db_watchdog.py` (for the `_alert_orphan_db_holder` orphan-DB Layer-3 helper + the `detect_orphan_db_holders` lsof-not-found alert branch — clean leaf relocated from bot/_impl.py at Bit 9.3-ii REPLACING that slot), `bot/main_loop.py` (MainLoop reads + WRITE in `__init__`), `bot/scanner/__init__.py`, `bot/executor.py`, `bot/settlement.py` — via `import bot.notifier as _telegram_state` plus `_telegram_state._TELEGRAM` module-attribute access (mutation freshness preserved across all five). The 13 consumer call sites (12 OrderExecutor static-method calls + 1 MainLoop static-method call) + ~30 test-suite instantiations all resolve via the line-115 re-export. Sister Bit 8.2 (`bot/scanner/CLAUDE.md`) shipped 2026-05-10; Bit 8.3 internal scanner split deferred 7d post-Bit-8.1. Sprint 8 closed at Bit 8.2.

**P1.3 (Money Printer Roadmap, 2026-05-12) — lazy-only Telegram consumer**: `bot/helpers/cohort_alerts.py::emit_alert` reaches `_telegram_state._TELEGRAM` via a FUNCTION-SCOPED lazy import (search anchor: `def emit_alert`). This is NOT a 6th canonical consumer slot — the import is exception-swallowed and never participates in (a) the boot-ordering chain (no module-load edge) or (b) the singleton-mutation contract (`MainLoop.__init__` still owns the only WRITE site; `emit_alert` is a fire-and-forget READ). The `.importlinter` `helpers-leaf` carve-out `bot.helpers.cohort_alerts -> bot.notifier` documents the exception. The 5-canonical-consumer enumeration above is UNCHANGED.
4. Present a change summary — wait for approval.
5. `git add` + `commit` + `push` (triggers auto-deploy).
6. Verify VPS pulled the commit hash.
7. Verify expected DB rows are appearing.
