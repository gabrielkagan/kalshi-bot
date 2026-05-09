# bot/ — implementation rules

These rules apply to `bot/_impl.py` and the engine modules
(`spx_engine.py`, `weather_engine.py`, `sports_engine.py`,
`fifteenm_shadow.py`, `analyst.py`). The first seven sections
(Threading, cal_mlp four-site, Cell-block, SQLite, `_shadow_diag`,
Engine→CalEngine, `discover_active_windows()`) are
**implementation-specific** — they only matter when editing the
runtime, not when running audits or working in `tests/` /
`scripts/`. The Cell-block section additionally has cross-cutting
reach beyond `bot/` and is duplicated as a one-liner in
`scripts/CLAUDE.md`. The trailing **Workflows** section includes
prose long-forms of cross-cutting skills (`/investigate`, `/audit`,
`/deploy`) that route through bot/_impl.py state; the canonical surface
for those is the matching skill, and this section is a backup
readable here for agents working inside `bot/`.

## Threading + numerical libraries (sacred ordering)

- **Don't import torch directly in `bot/_impl.py`.** `cal_mlp` is the
  single torch entry point via `scripts/cal_mlp/integration.py`,
  which constrains threads at module-import time.
- **`import bot._thread_env` must remain the FIRST non-stdlib import in `bot/_impl.py`.**
  numpy/scipy C extensions cache OpenBLAS thread count at load time,
  so `OMP_NUM_THREADS=1` has to be in `os.environ` before they
  import. Direct `import torch` or any reorder defeats the
  contention fix. Postmortem: production incident 2026-04-29 (scan
  loop ballooned to 7.75s, 0 candidates in 5 min) →
  `kb/failures/cal-mlp-torch-thread-contention-apr29.md`. AST
  regression:
  `tests/test_cal_mlp_invariants.py::test_thread_env_imported_before_numerical_libs_in_bot_impl`.

## cal_mlp feature transforms (four-site lock-step)

Any change to a feature transform — winsorize cap
`SIGMA_WINSOR_ABS_CAP=25.0`, `hour_sin`/`hour_cos` derivation,
`prob_breakeven_gap` formula, sigma derivation — must update **all
four sites in ONE commit**:

1. `scripts/cal_mlp/extract_data.py`
2. `scripts/cal_mlp/post_hoc_processor.py`
3. `scripts/cal_mlp/integration.py`
4. `scripts/cal_mlp/features.py::compute_cfg_fp`

Splitting → train/serve skew (model trained on one distribution,
served from another). See `agent_docs/calibration_pipeline.md` "cal_mlp
feature transforms (four-site lock-step)" for the rationale.
Regression tests: `tests/test_calmlp_sigma_winsorize.py` +
`tests/test_calmlp_tm96_gate.py`.

## Cell-block activations deflate `filter_stage='candidate'` rollups

Audit + dashboard scripts that query `WHERE filter_stage = 'candidate'`
for "all 15M trades" totals **under-count post-activation**. The
actual `filter_stage` VALUES (string literals stored in DB — NOT
Python constant names) are:

- `'96C_SOL_XRP_STC_DANGER_BAND'` (HPSB)
- `'TM98_97_98C_2_5MIN_BLEED'`
- `'SOL_TAKER_85_89C_2_5MIN_BLEED'`

To re-aggregate true total candidate volume, UNION these stage
values. **Pattern:** any script that filters
`WHERE filter_stage = 'candidate'` (or
`IN ('candidate', 'observation_trade')`) on 15M-scoped queries.
Identify via:
`grep -rn "filter_stage[ =]*[='IN ]*candidate" scripts/ *.py .claude/`.

Confirmed-affected (Apr 30): `scripts/15m_live_audit.py`,
`scripts/15m_alpha_research.py` (`/15m-alpha`),
`scripts/alpha_audit.py` (`/alpha-audit`),
`scripts/data_health_monitor.py`, `scripts/generate_whitepaper_stats.py`,
`scripts/maker_opportunity_cost.py`, `scripts/quiet_market_monitor.py`,
`dashboard_snapshot.py` (root), `analyst.py`, `auditor.py`,
`researcher.py`, `.claude/skills/status/SKILL.md` (`/status` skill).
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

## `_shadow_diag` schema chain

Adding keys to `_shadow_diag`: also update
`insert_rejection()` + `insert_evaluated_opportunity()` signatures + SQL.
All four sites ship in one commit, otherwise the new key gets dropped
silently at write time.

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

## Workflows (bot/_impl.py changes)

### Add a shadow strategy
1. Shadow flag constant (e.g. `NEW_FEATURE_SHADOW = True`).
2. Wire into `scan()`; log to `evaluated_opportunities` with the right
   `filter_stage`.
3. New DB columns: update INSERT + signature + SQL in same commit.
4. Add a metric to `dashboard_snapshot.py`.
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
2. Syntax-check `bot/_impl.py` + `bot/constants.py` (`make ast-check`); for changes to `bot/helpers/*.py`, `bot/logger.py`, `bot/notifier.py`, `bot/kalshi_client.py`, `bot/fetchers/*.py`, `bot/feeds/*.py`, or `bot/engines/*.py`, the full pytest suite covers transitively (no per-file ast-check target as of Sprint 6).
3. Grep call sites if signatures changed. Constants live in `bot/constants.py` (Bit 3.1, re-exported via `from bot.constants import *`); helpers live in `bot/helpers/*.py` (Bit 3.2, re-exported via `from bot.helpers import *` plus explicit underscore re-exports for `validators` and `breakers`); `Logger` lives in `bot/logger.py` (Bit 4.1, re-imported via `from bot.logger import Logger`); `TelegramNotifier` lives in `bot/notifier.py` (Bit 4.2, re-imported via `from bot.notifier import TelegramNotifier`); `KalshiClient` lives in `bot/kalshi_client.py` (Bit 4.3, re-imported via `from bot.kalshi_client import KalshiClient`); `DeribitDVOLFetcher` and `CoinGlassFetcher` live in `bot/fetchers/` (Bit 4.4, re-imported via `from bot.fetchers import DeribitDVOLFetcher, CoinGlassFetcher`); `CoinbaseFeed`, `CrossExchangeFeed`, `KalshiFeed`, and `OrderbookSchemaError` live in `bot/feeds/` (Bit 4.5a + 4.5b, re-imported via `from bot.feeds import CoinbaseFeed, CrossExchangeFeed, KalshiFeed, OrderbookSchemaError`; `KalshiFeed` (`bot/feeds/kalshi.py`) imports `OrderbookSchemaError` directly from sibling `bot/feeds/orderbook_schema.py`); `VolatilityEngine` lives in `bot/engines/volatility.py` (Bit 6.1) and `ProbabilityEngine` lives in `bot/engines/probability.py` (Bit 6.2), both re-imported via `from bot.engines import VolatilityEngine, ProbabilityEngine`. ProbabilityEngine deviates from byte-for-byte: `compute()` and `counterfactual_prob()` use `from bot import _impl as _bot_impl` late-binding to access the mutable `_CALIBRATION_ENGINE` singleton and `_resolve_cal_engine` function (both defined in `bot/_impl.py` BELOW the line-109 engines re-export — top-level import would ImportError or capture stale `None`). The new `bot/engines/` subpackage will host `CalibrationEngine` (Bit 6.3) next.
4. Present a change summary — wait for approval.
5. `git add` + `commit` + `push` (triggers auto-deploy).
6. Verify VPS pulled the commit hash.
7. Verify expected DB rows are appearing.
