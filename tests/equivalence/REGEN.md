# Equivalence harness — snapshot regeneration runbook

Pillar 3 of the testing-foundation-sprint
(`kb/decisions/testing-foundation-sprint-may09.md`). This file documents
how to regenerate the snapshot fixtures consumed by
`tests/equivalence/test_probability_engine.py` and
`tests/equivalence/test_volatility_engine.py`.

> **AGENT-FACING RULE — DO NOT VIOLATE.**
>
> Never run `pytest --force-regen` or
> `pytest --regen-all` autonomously. Snapshot regeneration always
> requires a human to diff and commit the new files. An agent that
> regenerates without review effectively erases the verification gap
> the harness exists to close.
>
> If a test fails because the snapshot diverged from current
> behavior, the answer is **investigate the divergence**, not
> regenerate. Engine math should not change without a corresponding
> KB decision doc.

## What lives where

```
tests/fixtures/
  engine_inputs.parquet            # 1000-row ProbabilityEngine corpus
  engine_inputs.meta.json          # sidecar — sample seed, days, strata

tests/equivalence/
  conftest.py                      # corpus loader, dep mocks, calibration patch
  test_probability_engine.py       # 9 tests: 3 corpus + 1 counterfactual_prob corpus + 4 property + 1 spot-check
  test_volatility_engine.py        # 15 tests: 6 static-method snapshots + 1 row-order regression + 6 property + 2 spot

  test_probability_engine/
    test_probability_engine_compute_corpus_numeric.csv     # 1000-row × 3-col (z_score, raw_prob, calibrated_prob)
    test_probability_engine_compute_corpus_categorical.yml
    test_counterfactual_prob_corpus_numeric.csv            # 1000-row × 1-col (counterfactual_prob @ 1.5×volatility)

  test_volatility_engine/
    test_parzen_kernel_grid_snapshot.csv                # num_regression — continuous x (rtol=1e-12)
    test_estimate_noise_variance_snapshot.csv           # num_regression — floor-magnitude (atol=1e-22)
    estimate_noise_variance_keys.yml                    #   sidecar: row order
    test_realized_quarticity_snapshot.csv               # num_regression — floor-magnitude (atol=1e-22)
    realized_quarticity_keys.yml                        #   sidecar: row order
    test_optimal_rk_bandwidth_snapshot.yml              # data_regression — int values, keyed by scenario|window
    test_realized_kernel_snapshot.yml                   # data_regression — sigma-scale, keyed by scenario|window
    test_bipower_variation_snapshot.yml                 # data_regression — sigma-scale, keyed by scenario|window
```

Why the snapshot format split: floor-magnitude outputs (~1e-15 to
1e-20) need ``num_regression`` with ``atol=1e-22`` to preserve drift
signal — ``data_regression`` with 12-decimal rounding silently floors
those values to 0.0. Sigma-scale outputs (~1e-5 to 1e-3) and integer
outputs are well above that floor; ``data_regression`` keyed by
scenario gives readable per-scenario YAML diffs (immune to row-shift
on scenario insertion). The two ``num_regression`` tests have a
parallel ``*_keys.yml`` sidecar; row order is pinned by
``test_volatility_engine_snapshot_row_order``.

## When to regenerate

| Trigger | Regen needed? | Why |
|---|---|---|
| Engine math change with KB decision doc | **Yes** — by hand, with diff review | The whole point. New behavior → new snapshot. |
| New static helper added to an engine | **Yes** — extend `_SCENARIOS` and add a snapshot test | Coverage gap |
| Numerical floating-point reordering ("equivalent" math) | **No** — fix the test | rtol=1e-12 is below quantization; if it breaks, the math is not actually equivalent |
| New `bot.constants` value that affects the engine | **Yes** — but verify the constant change has its own KB doc first | Constant changes are the silent-drift class |
| New row added to `evaluated_opportunities` schema (DB column) | **No** | The script only pulls a fixed column subset |
| Engines extracted to new modules (Bit 6.3+) | **Yes** | Snapshots pin the *outputs*, not the *paths* — extraction itself shouldn't move them, but verify with a regen+diff round |
| `dist_config.json` regenerated (per-asset distribution refit) | **Yes** | `ProbabilityEngine._cdf_complement` reads `DIST_CONFIG` (loaded at `config.py` import). New `student_t_df` or NIG params → every corpus row's `raw_prob` shifts. |
| `MARKET_CONFIGS` made dynamic (e.g., Supabase fetch) | **Yes** + freeze in conftest | `compute()` calls `get_market_config(product_type)` before the cascade; today static, but if it becomes env-dependent the snapshot is machine-dependent. See conftest's "Implicit input" docstring. |
| scipy minor version bump in CI | **Maybe** | `student_t.cdf` / `norminvgauss.cdf` can shift ~1e-13 to 1e-11 across minor releases. The `rtol=1e-9` on the corpus snapshot tolerates this; if a scipy bump pushes drift above that floor, regen ONLY after confirming the bump is documented and intentional. |

If a regen is appropriate, the diff goes in the same PR as the
underlying behavior change. PR description must include the engine-math
KB link.

## How to regenerate the parquet corpus

The parquet feeds ProbabilityEngine snapshots only. VolatilityEngine
synthetic scenarios are inline in `test_volatility_engine.py`.

**On the VPS** (cleanest — direct read from the live `state.db`):

```bash
ssh botuser@<VPS>
cd ~/kalshi-bot-repo
python3 scripts/sample_engine_inputs.py --db ~/kalshi-bot-repo/state.db
# Outputs:
#   tests/fixtures/engine_inputs.parquet
#   tests/fixtures/engine_inputs.meta.json
git diff -- tests/fixtures/engine_inputs.parquet  # binary, mostly meaningless
git diff -- tests/fixtures/engine_inputs.meta.json  # human-readable strata diff
```

**On a Mac dev box** (with a scp'd state.db copy):

```bash
scp botuser@<VPS>:~/kalshi-bot-repo/state.db /tmp/state.db
python3 scripts/sample_engine_inputs.py --db /tmp/state.db
```

**Bootstrap note (2026-05-09):** the initial parquet shipped with
Pillar 3 was generated server-side via the kalshi-vps MCP `query_db`
tool (`tests/fixtures/engine_inputs.meta.json` documents the SQL).
Running the script will produce a **non-byte-identical** parquet
because the script's stratified sampler is python-side
(deficit-cascade allocator) while the bootstrap was SQL-side
(LCG hash + `ROW_NUMBER` partitioning). The strata distribution
should still match within ±5 rows per stratum. Diff the
`.meta.json` strata block, not the parquet bytes.

## How to regenerate the snapshot files

After a legitimate engine-math change has been merged into the engine
modules:

```bash
# 1. Confirm tests fail in expected places
python3 -m pytest tests/equivalence/ -v --tb=short

# 2. Regenerate (HUMAN ONLY — never run via agent)
python3 -m pytest tests/equivalence/ --force-regen

# 3. Diff every changed file by hand
git diff -- tests/equivalence/

# 4. For categorical/yaml diffs: read line-by-line.
#    For numeric csv diffs: spot-check at least 5 rows; if the magnitudes
#    don't match the documented engine-math change, STOP — the regen is
#    capturing an unintended drift.

# 5. Commit only after the diff is fully understood
git add tests/equivalence/
git commit
```

If the diff cannot be explained from the engine-math change, the regen
is unsafe — **do not commit**. Investigate first.

## Float tolerance

Tolerance is split by output magnitude:

| Snapshot | Tolerance | Why |
|---|---|---|
| ProbabilityEngine corpus (`*compute_corpus_numeric.csv`) | `rtol=1e-9, atol=0` | scipy `student_t.cdf` / `norminvgauss.cdf` can shift ~1e-13 to 1e-11 across minor releases; 1e-9 is 3 orders of magnitude above that drift floor and 3 below the engine's `round(*, 6)` quantization. |
| Volatility floor-magnitude (`*estimate_noise_variance*.csv`, `*realized_quarticity*.csv`) | `rtol=1e-9, atol=1e-22` | Outputs reach `RK_NOISE_VAR_FLOOR=1e-20`. `atol` floor is 100× below the floor; 12-decimal data_regression rounding silently destroyed this signal in the first cut (per R2 M1). |
| Volatility sigma-scale (`*realized_kernel*.yml`, `*bipower_variation*.yml`, `*optimal_rk_bandwidth*.yml`) | `data_regression` with 12-decimal pre-serialization rounding (effectively atol=5e-13) | Outputs at 1e-5 to 1e-3 are well above the rounding floor; YAML key→value mapping makes diffs naturally per-scenario (immune to row-shift on scenario insertion). |
| Parzen kernel grid (`test_parzen_kernel_grid_snapshot.csv`) | `rtol=1e-12, atol=0` | Pure stdlib math, ULP-stable across platforms; tighter tolerance is safe and catches the smallest possible drift. |

Engine outputs are rounded to 4–6 decimals internally for ProbabilityEngine,
so the snapshot tolerance is well below the engine's own quantization
floor. A test failure means the engine produced a *meaningfully*
different value, not a floating-point reordering artifact.

If a future refactor introduces a true reordering that crosses the
tolerance (e.g., switching from `sum()` to `numpy.sum()` for a
many-term reduction), the right fix is *not* to widen tolerance — it
is to either:
1. Pin the reduction order in the engine itself (if the order matters
   for downstream callers), or
2. Tighten the engine's internal rounding (the snapshot will then sit
   above the new quantization floor).

Tolerance widening hides drift. Don't.

## Known limitations (what the harness CANNOT detect)

* **Sub-2x drift in `RK_NOISE_VAR_FLOOR`.** Eight of nine
  noise-variance scenarios snapshot at the floor (1e-20). With
  `atol=1e-22`, a constant change that shifts the floor by less
  than 2e-22 (i.e., <2% of the floor itself) passes silently. In
  practice, RK floor changes always require a separate KB doc
  (per `scripts/CLAUDE.md` regime-filter rule) — but the harness
  is not the gate.
* **Bare-directory iCloud-conflict matches** (`.gitignore`
  `**/*\ 2/`). A literal `git add "tests/equivalence/foo 2"`
  without a trailing slash is not pre-warned by `git
  check-ignore`. Files inside are still ignored when git scans
  contents (the practical case). Strictly bare-dir staging is
  rare; `git add -A` does not hit it.
* **Non-corpus product_types.** The corpus has 4 product_types
  (15m, hourly, spx_hourly, weather). `sports` is exercised only
  via hypothesis property tests, not the corpus snapshot.
  ProbabilityEngine `compute()` outputs for sports are not
  pinned numerically — only the no-crash invariant is.

## Calibration-engine isolation (post-Bit-6.3 path-B)

`tests/equivalence/conftest.py::isolate_calibration_singletons` (autouse)
patches `bot.engines.calibration._CALIBRATION_ENGINE` to `None` and
`bot.engines.calibration._resolve_cal_engine` to a stub that returns
`None`. This forces `ProbabilityEngine.compute()` into the
deterministic passthrough/fixed-beta cascade by default — the
calibration engine's mutable state would otherwise couple the
snapshot to whatever calibrator shipped at snapshot time.

**Patch targets** track Bit 6.3 path-B (2026-05-10): the singleton +
resolver were relocated from `bot/_impl.py` to
`bot/engines/calibration.py` alongside the class, and the
`.importlinter` `bot.engines.probability -> bot._impl` carve-out was
removed. Both `bot/_impl.py` and `bot/engines/probability.py` now
read the singleton via the same module alias
(`from bot.engines import calibration as _cal_state`), so a single
patch on `bot.engines.calibration.X` propagates to every consumer.

**Opt-in oracle for learned-method branches** —
`tests/equivalence/conftest.py::install_frozen_cal_engine` stacks on
top of the autouse fixture with a `CalibrationEngine` instance loaded
from a hand-crafted Platt state dict (`_FROZEN_CAL_STATE`,
deterministic `A=BETA_SLOPE, B=0, _platt_trained=True`). Tests that
request this fixture exercise the learned-method outcomes 1+2 of the
cascade rather than the autouse outcome-4/5 default. **Vendored-
snapshot flavor was chosen over subprocess-git-checkout** for
determinism + speed: the state dict is constructed from primitives
in conftest, written to `tmp_path` per test, and loaded via
`CalibrationEngine(state_path=...)` — no SHA pin, no trained-model
file in `tests/fixtures/`. The Bit 6.3 closeout doc records the rationale
and the KB note on regenerating the state dict if the persistence schema
evolves (filename pattern `kb/decisions/bit-6.3-shipped-may<DD>.md`,
authored at ship time — local-only per `kb/CLAUDE.md`).
