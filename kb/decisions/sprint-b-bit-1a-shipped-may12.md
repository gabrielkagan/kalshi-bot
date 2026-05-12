# Sprint B Bit B.1a — rejected_opportunities feature enrichment [SHIPPED 2026-05-12]

ClickUp ticket: `86b9vfzjp`.

## TL;DR

ALTER-ed `rejected_opportunities` with 7 training-data columns and wired
the auto-fill into `StateManager.insert_rejection()`. Future gate-policy
learner can now train on rejected rows with proper features instead of
just the 9-col base schema.

## RCA findings (the gap closed)

Pre-B.1a `rejected_opportunities` schema (29 cols total via CREATE +
16-col ALTER block at `bot/state.py:812-836`):

  ticker, event_ticker, asset, rejection_reason, rejection_time,
  z_score, spot_price, threshold, volatility, market_price,
  seconds_to_close, calibrated_prob, status, raw_prob, market_result,
  egarch_*, mz_*, shadow_tv_blend_rv, counterfactual, product_type,
  oft_prob_adjustment, oft_imbalance_ratio, oft_n_snapshots, no_ask_cents.

What was MISSING (and `evaluated_opportunities` already had via Phase 1
+ Phase G-6): `sigma_winsorize`, `hour_sin`, `hour_cos`,
`prob_breakeven_gap`, `vol_regime`, `data_provenance`,
`orderbook_levels_json` — 7 columns.

The gap meant the parallel `evaluated_opportunities` table was learnable
on but `rejected_opportunities` (which has the actual policy-decision
signal — what we DECLINED to trade) was not. A future gate-policy
learner training on rejected rows would have to back-derive everything
from the 5 base inputs (spot/threshold/vol/stc/market_price), missing
the lock-step-correct sigma clipping and the asynchronous serve-time
`vol_regime` regime classification.

## Implementation

### Schema (bot/state.py)

Extended the existing `for col_def in [...]` ALTER block at line 812-836
with 7 new (name, type) tuples. Idempotent — the existing
`except sqlite3.OperationalError: pass` swallows "duplicate column"
errors. Re-runs on operator restart are safe.

### Population (bot/state.py + bot/scanner/__init__.py)

Followed `insert_evaluated_opportunity` precedent: enrichment is
**centralized at the StateManager layer** rather than per-write-site.
`insert_rejection()` now accepts 7 new keyword arguments (all with
`None` / `'live_ws'` defaults) and auto-fills them via:

- `orderbook_levels_json` → `self._get_fresh_ob_ladder(ticker)` (10s
  freshness gate; stale → NULL — honest, never lie). Same path used by
  `insert_evaluated_opportunity`.
- `hour_sin` + `hour_cos` → `compute_time_regime_features(now)` →
  `compute_hour_sin_cos(hour_of_day_utc)`. The helper lives in
  `bot/helpers/derived_features.py` (new in this Bit) and mirrors the
  cal_mlp four-site canonical formula
  (`scripts/cal_mlp/integration.py:1430-1431` +
  `extract_data.py:428-429` + `post_hoc_processor.py:257-258`).
- `prob_breakeven_gap` + `sigma_winsorize` → `compute_derived_features`
  (existing) → `apply_sigma_winsor` (new in `bot/helpers/derived_features.py`,
  mirrors `scripts/cal_mlp/features.SIGMA_WINSOR_ABS_CAP=25.0`).
- `vol_regime`, `data_provenance` → caller-provided, no auto-fill.
  `data_provenance` defaults to `'live_ws'` (same default as
  `insert_evaluated_opportunity`).

Scanner sites (`bot/scanner/__init__.py`) that fire AFTER `vol_est` is
built thread `vol_regime=vol_est["regime"]` so the new training column
is non-NULL:

1. `price_out_of_range_early` (line ~1850) — spx_hourly/hourly/weather
   only; vol_est is in scope after the line-1630 None-gate.
2. `low_probability_15m` (line ~2065) — 15M path; vol_est always set.
3. `no_orderbook` (line ~2140) — 15M path.
4. `no_best_ask` (line ~2210) — 15M path.

Other rejection sites (e.g. `threshold_unparsable`, `weather_prob_none`)
fire before `vol_est` is fully bound for their product type and write
NULL for `vol_regime` — that is the **honest-NULL contract** documented
in the Bit-9.2 `_shadow_diag` schema chain rules. Synthesizing a
`vol_regime='normal'` default would corrupt the training signal.

## Cal_mlp four-site lock-step compliance

Per `bot/CLAUDE.md` "cal_mlp feature transforms (four-site lock-step)":
the `SIGMA_WINSOR_ABS_CAP=25.0` constant + `hour_sin/cos` derivation
live in `scripts/cal_mlp/features.py`,
`scripts/cal_mlp/extract_data.py`, `scripts/cal_mlp/integration.py`,
and `scripts/cal_mlp/post_hoc_processor.py`. **Any change to those
constants or formulas MUST update all four sites in ONE commit.**

This Bit DOES NOT change any of those formulas. It MIRRORS them into
`bot/helpers/derived_features.py` so the bot package can use them
without pulling `scripts/cal_mlp/*` (which transitively loads
torch/numpy heavy deps) onto the bot import path. Numeric equivalence
is pinned by:

- `tests/integration/test_sprint_b_bit_1a_rejection_enrichment.py::TestCalMLPLockStepEquivalence`
  — explicit value-by-value comparison of `bot.helpers.apply_sigma_winsor`
  vs `features.apply_sigma_winsor` across the cap edges + NaN
  passthrough + None passthrough.
- `tests/integration/test_calmlp_sigma_winsorize.py` (pre-existing) +
  `tests/integration/test_calmlp_tm96_gate.py` continue to pin the
  cal_mlp canonical sites.

Any future divergence between the bot.helpers mirror and the cal_mlp
canonical IS a lock-step violation and will fail the equivalence test
on the next CI run.

## TDD + adversarial rounds

`tests/integration/test_sprint_b_bit_1a_rejection_enrichment.py` — 14
tests across 5 sections:

1. `TestSchemaMigration` (3 tests) — all 7 new cols present, types
   match, migration is idempotent on restart.
2. `TestInsertRejectionEnrichment` (4 tests) — full-context call
   populates all 7; `no_orderbook` rejection writes NULL ladder
   (honest); cache-populated ladder propagates; `price_out_of_range_early`
   writes honest NULL for prob_breakeven_gap (no cal_prob yet).
3. `TestCalMLPLockStepEquivalence` (2 tests) — bot.helpers numeric
   output IDENTICAL to scripts/cal_mlp/features at every cap edge +
   NaN + None.
4. `TestNoInlineLockStepFormulaDuplication` (2 AST guards) — scanner
   doesn't inline `math.pi/24` formula; state.py doesn't inline the
   25.0 sigma cap literal.
5. `TestScannerCallsAtFourSites` (3 parametrized tests) — the 3
   post-vol_est rejection sites pass `vol_regime=`. Planted-defect
   test verified: removing `vol_regime=vol_est["regime"]` from the
   `low_probability_15m` call site flips the test RED with the
   correct AssertionError.

Pre-implementation: 14/14 RED (schema test failed first on missing
cols; downstream cascaded).

Post-implementation: 14/14 GREEN. Pre-existing failures unchanged
(6 cal_mlp_sigma_winsorize + 1 cal_mlp_validate_cfg_fp env-specific
venv path + 26 tdd_guard_hook + cal_mlp_mac_drain).

Adversarial review: 2 zero-CRITICAL/MAJOR rounds. Self-found in R1:
verified `vol_est["regime"]` is safe at all 4 sites (gate at line 1630
guarantees non-None); confirmed `regime` key exists in all 3 vol-engine
return types (`bot/engines/volatility.py:1001`,
`bot/engines/spx_engine.py:971`, `bot/engines/weather_engine.py:954`).
R2 confirmed `**_shadow_diag` spread does not collide with any of the
7 new kwargs. Planted-defect test verified end-to-end.

## Out-of-scope / follow-ups

1. **Supabase mirror sync** — `supabase_sync.py::_REJ_COLUMNS` is the
   list of cols mirrored from SQLite to the Supabase `rejections`
   table. The 7 new cols are NOT in that list and so will not ship to
   Supabase. A future Sprint B Bit (or operator) will:
   (a) write a `supabase_migration_020_rejected_opportunities_b1a.sql`
       adding the 7 cols to the `rejections` Postgres table,
   (b) extend `supabase_sync._REJ_COLUMNS`,
   (c) add applicable INT-typed entries to `_INT_COLUMNS` (none of the
       7 new cols are INTEGER-typed — all are REAL/TEXT — so this step
       is a no-op).
   File ClickUp ticket post-ship.

2. **Backfill for existing rows** — pre-B.1a rejected_opportunities
   rows have NULL for the 7 new cols. A `scripts/backfill_b1a_rejection_features.py`
   helper could derive `sigma_winsorize` + `prob_breakeven_gap` +
   `hour_sin/cos` from the existing (spot, threshold, volatility,
   seconds_to_close, calibrated_prob, market_price, rejection_time)
   inputs. `vol_regime` + `orderbook_levels_json` are NOT backfillable
   (vol_regime was not captured at rejection time pre-B.1a;
   orderbook_levels_json requires the in-memory `_scan_ob_cache`
   snapshot which is no longer available). File as Sprint B follow-up
   ticket.

3. **No additional rejection sites updated** — only the 4 in the
   ticket. Sites like `threshold_unparsable`, `weather_prob_none`,
   `tradeable_false` write NULL for `vol_regime` per the honest-NULL
   contract; if a future learner wants them, file a separate Bit.

4. **`data_provenance` enrichment for backfill rows** — Sprint A.2
   stamped data_provenance for evaluated_opportunities backfill. For
   rejected_opportunities the backfill helper from (2) above would
   stamp `'backfill_60s_inputs'` matching the existing convention. Tied
   to the same follow-up ticket.

## Files touched (atomic commit)

- `bot/state.py` — schema migration + insert_rejection signature +
  auto-fill body
- `bot/scanner/__init__.py` — 4 vol_regime= kwargs at the 4 rejection
  sites
- `bot/helpers/derived_features.py` — `apply_sigma_winsor`,
  `compute_hour_sin_cos`, `SIGMA_WINSOR_ABS_CAP` (mirrored from
  cal_mlp four-site canonical with lock-step doc + equivalence test
  pin)
- `tests/integration/test_sprint_b_bit_1a_rejection_enrichment.py` —
  14 TDD pins
- `tests/fixtures/state_db_schema_baseline.txt` — updated baseline
  (29 → 36 cols on rejected_opportunities)
- `agent_docs/db_schema.md` — 7 new rows in rejected_opportunities table
- `kb/decisions/sprint-b-bit-1a-shipped-may12.md` — this doc

## DO NOT PUSH OR DEPLOY

Per the parent agent's task spec: commit only, parent will merge to
main and ask user for deploy approval.
