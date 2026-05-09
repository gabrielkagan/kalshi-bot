---
clickup: 86b9ve0zu
parent_ticket: 86b9ve0wa
sprint: testing-foundation-sprint
shipped: 2026-05-09
---

# Pillar 3 SHIPPED — hypothesis + pytest-regressions equivalence harness

ClickUp: [86b9ve0zu](https://app.clickup.com/t/86b9ve0zu) under parent [86b9ve0wa](https://app.clickup.com/t/86b9ve0wa).

Branch: `86b9ve0zu-pillar-3-equivalence-harness` off `origin/main` at `247c738`. **REQUIRES-APPROVAL risk tier** — open PR + WAIT for explicit user merge approval (no autonomous merge).

## What shipped

A snapshot-and-property test harness under `tests/equivalence/` that pins the numerical outputs of the two already-extracted engines (`VolatilityEngine` from Bit 6.1, `ProbabilityEngine` from Bit 6.2) against a real-corpus snapshot fixture. The pillar exists to close the verification gap that Bit 6.2 exposed — a non-byte-for-byte extraction that we only knew was correct because the diff was reviewed by eye. With ~25 more bits coming in Sprints 6-13, eye-review doesn't scale.

| Surface | What it pins | How |
|---|---|---|
| ProbabilityEngine corpus | Numerical outputs (`z_score`, `raw_prob`, `calibrated_prob`) of `compute()` across 1000 stratified inputs from production `state.db` | `pytest-regressions.num_regression`, rtol=1e-9 |
| ProbabilityEngine corpus categorical | `calibration_method`, `tradeable`, `reason` distribution buckets | `data_regression` YAML, exact text equality |
| ProbabilityEngine property | NaN/inf-free outputs, probability ∈ [0, 1], dynamic_cap ∈ (0.5, 1], `_calibrate(0.5) == 0.5` | `hypothesis` with hand-bounded strategies |
| VolatilityEngine static methods | 6 pure `@staticmethod` math kernels across 9 deterministic synthetic scenarios × 3 windows | mix of `num_regression` (floor-magnitude `omega_sq` / `rq`) and `data_regression` (sigma-scale `realized_kernel` / `bipower_variation`) |
| VolatilityEngine property | RV ≥ 0, kernel ∈ [0, 1], BV ≈ σ asymptotically | `hypothesis` |

24 tests total (9 ProbabilityEngine + 15 VolatilityEngine including the row-order regression; ProbabilityEngine bumped from 8 to 9 in R3 — added a `counterfactual_prob` corpus pin to close the late-binding-shared-with-`compute()` drift gap). Suite runs in ~2.7s — well under the AC's <5s budget.

## What did NOT ship vs the original AC

The original AC (`kb/decisions/testing-foundation-sprint-may09.md`) called for `polyfactory-from-dataclass` + `syrupy`. The shipped harness uses **hand-written hypothesis strategies + pytest-regressions** — surfaced and approved during plan-mode interaction:

| AC original | Shipped | Why |
|---|---|---|
| `polyfactory-from-dataclass` for hypothesis input synthesis | Hand-written hypothesis strategies | Engine inputs are tightly constrained (price ∈ [0.01, 0.99] on Kalshi-cents, vol > 0 but < 0.15, sec_remaining ≤ 86400). Polyfactory would generate impossible inputs unless heavily configured; hand-written bounds are simpler and faster. |
| `syrupy` for snapshots | `pytest-regressions` | num_regression is purpose-built for numeric snapshots with built-in atol/rtol + NaN handling. Syrupy is heavier and overkill for primarily-numeric engine outputs. The AC explicitly offered `syrupy or pytest-regressions`, so this is partial-blessed. |

The bootstrap parquet at `tests/fixtures/engine_inputs.parquet` was generated server-side via the kalshi-vps MCP `query_db` tool with a deterministic LCG hash + `ROW_NUMBER` partitioning (LIMIT 1000 across 41 strata) rather than the script's python-side deficit-cascade allocator. Regenerating via `python3 scripts/sample_engine_inputs.py` will produce a non-byte-identical parquet — strata distributions match within ±5 rows per stratum. Documented in `tests/equivalence/REGEN.md` and the parquet's sidecar `tests/fixtures/engine_inputs.meta.json`.

## Calibration-engine isolation (load-bearing scope decision)

`tests/equivalence/conftest.py::isolate_calibration_singletons` is autouse and patches:

* `bot._impl._CALIBRATION_ENGINE → None`
* `bot._impl._resolve_cal_engine → lambda *a, **kw: None`

This forces `ProbabilityEngine.compute()` into outcomes 4 + 5 of the 5-way calibration cascade (`probability.py:204-238`) — passthrough and fixed-β fallback. Outcomes 1-3 (registry learned-method, legacy `_CALIBRATION_ENGINE` learned, legacy non-learned + BLR_BYPASS log) are dark.

This is intentional: the snapshot needs to be deterministic and the calibrator's mutable state changes across retrains. Testing the learned-method branches is **explicitly Bit 6.3's responsibility** — when CalibrationEngine extracts to its own module, the conftest fixture extends to inject a frozen oracle. The conftest docstring includes a sketch (`load_frozen` is hypothetical — Bit 6.3 must define this surface).

The corpus categorical snapshot confirms the partition: `{fixed_beta: 303, passthrough: 697}`. The BLR clamp (probability.py:244-260) and market-discrepancy gate (264-272) ride along incidentally.

## Bit 6.3 unblocking

Pillar 3 was the last blocker for Bit 6.3 (CalibrationEngine extraction, **High** risk, 72h soak, four-site lock-step). With this merged:

* The harness exists to detect drift if the extracted CalibrationEngine differs from the inline one.
* The conftest fixture has a documented extension point for the oracle.
* The Bit 6.3 author needs to pick subprocess-git-checkout vs vendored-snapshot oracle (deferred decision — both paths supported by the harness shape).
* WIP commit `cbd4cd2` on branch `86b9vda4w-bit-6-3-calibration-engine` resumes from a clean main once Pillar 3 ships.

## Adversarial cadence

Standard project discipline: two consecutive zero CRITICAL+MAJOR rounds. Hit at R3 + R4. Full counts: R1 = 2C/7M/8m → R2 = 0C/4M/7m → R3 = 0C/0M/7m → R4 = 0C/0M/4m. Ship gate satisfied.

### R1 — 2C / 7M / 8m

R1 CRITICALs:
1. **Uncommitted state** — process issue, deferred to commit step (not a code defect).
2. **Calibration-engine isolation pinned to passthrough/fixed-β only; `shadow_cal_prob` and `shadow_cal_temperature` were guaranteed-NaN columns masquerading as pinned signal; Bit 6.3 handoff lacked code stub.** Fixed by dropping NaN-only fields from `_NUMERIC_FIELDS`, expanding the conftest docstring with explicit branch coverage + Bit 6.3 sketch + MARKET_CONFIGS implicit-input note.

R1 MAJORs (all fixed):
3. **iCloud `* 2.*` duplicates doubled pytest collection (44 tests instead of 22).** rm'd duplicates + extended `.gitignore` patterns + added `.hypothesis/`.
4. **scipy unpinned + rtol=1e-12 corpus snapshot would flake on scipy upgrades.** Relaxed corpus rtol to 1e-9 (3 OOM above scipy float drift, 3 OOM below engine quantization).
5. **Hypothesis property bounds undershot real input range** (`secs<3600`, `vol<0.01`). Widened to 86400 / 0.15 to match observed corpus extremes.
6. **VolatilityEngine static-method snapshot row order coupled to `sorted(_SCENARIOS.keys())`** — refactored 5 of 6 to data_regression keyed by scenario.
7. **MARKET_CONFIGS not isolated.** Documented as implicit input in conftest + REGEN.md regen-table row.
8. **`scripts/sample_engine_inputs.py` violated scripts/CLAUDE.md two-pragma rule.** Added `PRAGMA journal_mode=WAL` (no-op on `?mode=ro` but the rule is uniform).
9. **CI ran equivalence twice (dedicated step + broad pytest discovery).** Added `--ignore=tests/equivalence` to the broad pytest invocations in both workflows.

### R2 — 0C / 4M / 7m

R2 MAJORs (all fixed except M2 which IS this doc):
- **M1: 12-decimal data_regression rounding silently floored noise_variance + quarticity outputs to 0.0** for 8 of 9 scenarios (RK_NOISE_VAR_FLOOR=1e-20 < 12-decimal precision). Fixed by switching those two tests back to num_regression with atol=1e-22; sigma-scale tests stay on data_regression. Added `test_volatility_engine_snapshot_row_order` regression test to pin row order load-bearing.
- **M2: closeout KB doc absent.** Fixed — this is that doc.
- **M3: `.gitignore` iCloud patterns missed `.parquet`, `.sql`, `.toml`, `.lock`, `.sh`.** Added.
- **M4: `_PTYPE_STRAT` missing `"sports"`** (5th product type with cal_eligible=False). Added.

R2 MINORs (handled where actionable, deferred where not):
- m1 (5 outcomes vs 4 branches): conftest docstring corrected.
- m2 (REGEN.md still claimed rtol=1e-12 default): replaced with per-snapshot tolerance table.
- m3 (`_to_nan` dead code under current corpus): kept as defense-in-depth — engine could return None on invalid inputs that future corpus curation might include.
- m4 (`load_frozen` hypothetical): explicit "NOT YET IMPLEMENTED" comment added.
- m5 (`dist_config.json` regen path): noted but the fitter pipeline is out-of-repo; future contributor will need to chase it.
- m6 (scipy unpinned): defer-acknowledged. Tolerance fix mitigates; full pin is belt-and-suspenders for a future bit.
- m7 (hypothesis nondeterminism): defer-acknowledged. `derandomize=True` could be added when the suite hits a flake; not preemptively.

### R3 — 0C / 0M / 7m

First zero round on the CRITICAL+MAJOR axes. Minors handled in-round:
- m1 (REGEN.md "What lives where" still showed pre-R2 YAML format for noise_variance/quarticity + 14 tests): rewritten to match disk — 23 → 24 tests after R3 added the counterfactual_prob test, file listing now shows CSV+keys sidecars and the snapshot format split.
- m2 (`PRAGMA journal_mode=WAL` raises on read-only conn for non-WAL DBs): wrapped in try/except `OperationalError` so legacy/snapshot DBs are tolerated. busy_timeout pragma stays outside the try block.
- m3 (`counterfactual_prob` was an untested public surface with the same late-binding pattern as `compute()`): added `test_counterfactual_prob_corpus_numeric` pinning the calibrated probability for `alt_blended_rv = 1.5 × volatility` across the corpus. 24th and final test.
- m5 (sports product_type in `_PTYPE_STRAT` but absent from corpus): comment added explaining sports is exercised by the property test only, not the corpus snapshot.

Deferred (rationale):
- m1 (5 cascade outcomes vs 4 — already fixed in R2; new R3 m1 was REGEN drift).
- m4 (sub-2x drift detection at floor): documented as a known limitation in REGEN.md "Known limitations" section. KB-doc rule for floor changes is the actual gate.
- m6 (counterfactual cascade-coverage gap): same calibration-engine isolation applies; deferred to Bit 6.3 oracle.
- m7 (`* 2/` bare-dir gitignore semantics): documented as known limitation; `git add -A` does not hit the edge case.

### R4 — 0C / 0M / 4m

Second consecutive zero round. Closes ship gate. Minors handled in-round:
- M1 (REGEN.md still missed `test_counterfactual_prob_corpus_numeric.csv` from the file listing — literal recurrence of R3 m1 within the same review cycle): added.
- M2 (closeout test count drift: line 26 said "9 ProbabilityEngine + 15 = 24" but Files-Changed listing still said "8 tests"): unified at 9.
- M3 (closeout's "Adversarial cadence" section ended at "R3 → ship gate" without describing R3+R4 actions): added this R3 and R4 subsection.
- M4 (conftest "Implicit input" docstring lists MARKET_CONFIGS but not DIST_CONFIG, both equally implicit inputs): documented in REGEN.md regen-trigger table for now; conftest docstring kept tight.

## Lessons

Numbered continuing the modularization-track sequence (last was L48 in Pillar 2 closeout).

**L49** — Snapshot tolerance must match the magnitude regime of the output, not be a one-size-fits-all default. This pillar's first cut used data_regression with 12-decimal pre-serialize rounding for all 5 volatility static-method snapshots. R2 caught that 8 of 9 noise_variance rows snapshotted as 0.0 because `RK_NOISE_VAR_FLOOR=1e-20 < 5e-13` (the 12-decimal precision floor). Fix: split snapshots by output magnitude — floor-magnitude (ω², RQ) → num_regression with atol=1e-22; sigma-scale (RK, BV) → data_regression at 12 decimals. Pre-flight: when snapshotting numerical engine outputs, sample one value from each method first and check it survives the planned rounding. A snapshot of all-zeros pretends to verify behavior but verifies nothing.

**L50** — `pytest-regressions.data_regression` does textual file-equality on YAML, not numerical comparison. `-0.0` and `0.0` diff-fail despite numerical equality; floats with higher-than-rounded-decimal noise will flap on the lowest bits between platforms. For pure stdlib math at sigma-scale magnitudes the trade-off is fine, but for floor-magnitude or scipy-touched outputs, use num_regression's numpy-isclose-based comparison instead.

**L51** — The "row order is load-bearing" hazard recurs whenever a parallel-files snapshot pattern (e.g., values.csv + keys.yml) is used. The fix isn't always to switch to a key→value format — sometimes the dual format is correct (when value tolerance matters and keys must be reviewed inline). When it is, ship a regression test that pins the ordering explicitly: a one-line `assert sorted(_SCENARIOS.keys()) == [<expected>]` fails BEFORE the snapshot diff and tells the reviewer "scenario list changed; expect a row shift" rather than "math drifted." Cheap, prevents a bad-diff-read class.

**L52** — When mocking a mutable module-level singleton via late-binding (Bit 6.2's `_CALIBRATION_ENGINE` pattern), `monkeypatch.setattr(target, name, value, raising=True)` is the right primitive. The autouse fixture is load-bearing — individual tests cannot opt out of the patch — and `raising=True` asserts the target attribute exists at patch time, which catches a future rename of the singleton in a fail-loud way rather than silently snapshotting against an uncovered branch.

**L53** — iCloud Drive sync conflict copies are a recurring drift class. The pattern is `<filename> 2.<ext>`, `<dirname> 2/`. The first cut of `.gitignore` covered `.py`, `.md`, `.csv`, `.yml`, `.yaml`, `.json`, `.txt`, but R2 caught that the equivalence suite owns `.parquet` and the project also has `.sql`, `.toml`, `.lock`, `.sh` files in scope. Lesson: when adding iCloud-conflict patterns to `.gitignore`, walk the file types this Pillar actually touches and audit the existing project for any other extensions in active use. `git check-ignore` is the per-pattern verification.

## Files changed

```
NEW
  scripts/sample_engine_inputs.py                                            (~190 LOC)
  tests/fixtures/engine_inputs.parquet                                       (~62 KB binary)
  tests/fixtures/engine_inputs.meta.json                                     (regen sidecar)
  tests/equivalence/__init__.py                                              (empty)
  tests/equivalence/conftest.py                                              (corpus loader + autouse calibration patch)
  tests/equivalence/test_probability_engine.py                               (9 tests)
  tests/equivalence/test_volatility_engine.py                                (15 tests inc. row-order regression)
  tests/equivalence/REGEN.md                                                 (regen runbook + agent-never-update warning)
  tests/equivalence/test_probability_engine/*.csv|.yml                       (committed snapshots)
  tests/equivalence/test_volatility_engine/*.csv|.yml                        (committed snapshots, mixed format)
  kb/decisions/testing-foundation-pillar-3-shipped-may09.md                  (this doc)

MODIFIED
  pyproject.toml                                                             (hypothesis + pytest-regressions + pyarrow + pandas to dev; pinned [ml] pandas/pyarrow)
  .github/workflows/test.yml                                                 (Engine equivalence step + --ignore=tests/equivalence on broad pytest)
  .github/workflows/deploy.yml                                               (mirror of test.yml)
  .gitignore                                                                 (.hypothesis/ + iCloud * 2.* patterns)
  CLAUDE.md                                                                  (Critical rules: no auto-regen)
  tests/CLAUDE.md                                                            (equivalence harness section)
```

## What this does NOT cover (explicit followups)

* **CalibrationEngine equivalence** — Bit 6.3's job. Conftest extension point + sketch are committed.
* **Mutation testing on the equivalence corpus** — Pillar 5's job (testmon + tiered suite + mutmut baseline, ticket [86b9ve11y](https://app.clickup.com/t/86b9ve11y)).
* **Backfill snapshots for non-extracted engines** — only `bot/engines/volatility.py` and `bot/engines/probability.py` are extracted today. Sprint 7+ engines extend the harness in their respective bits.
* **scipy version pinning** — tolerance fix mitigates the immediate flake risk; full pin is a separate bit if it ever becomes load-bearing.
