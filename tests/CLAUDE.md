# Tests

3,744 tests collected (post-Bit-6.1). Pytest. `conftest.py` at repo root.
Verify exact count with `python3 -m pytest tests/ --collect-only -q | tail -1`;
this header drifts as bits ship and is only refreshed when an extraction touches
`tests/CLAUDE.md` directly.

## Run

### Tiered (Pillar 5 — preferred for the agent loop)

`make test-<tier>` is single-source-of-truth in the Makefile; CI mirrors
the same targets in `.github/workflows/test.yml` + `deploy.yml`.

| Tier | Budget | Contents | When to run |
|---|---|---|---|
| `make test-unit` | <10s (sub-sec actual) | Pure-Python invariants: pyproject parsing, Makefile parsing, repo hygiene. No DB, no network. | Every save (or every edit, via `test-affected`). |
| `make test-contract` | <5s ticket / ~12s actual on Mac | Pillar 1 public_api snapshot + Pillar 2 import-linter + AST guards (extraction tests, call_sites, db_signatures, config_consistency, order_outcome_vocab). | After any change to `bot/`, `pyproject.toml`, or `.importlinter`. |
| `make test-equivalence` | <30s (~3s actual) | Pillar 3 numeric snapshots + property tests for `bot/engines/{volatility,probability}.py`. Frozen calibration via `tests/equivalence/conftest.py`. | After any change to `bot/engines/`, `bot/constants.py`, or anything that flows into engine inputs. |
| `make test-integration` | <2min (~50s actual) | Everything else under `tests/` (the broad behavioral suite). | Before opening a PR. |
| `make test` | ~3min (sum of above) | All four tiers in order, fail-fast on the cheapest. | Before pushing to main. |

### Incremental (testmon)

| Target | Behavior | When to run |
|---|---|---|
| `make test-affected` | testmon-driven: re-runs only tests whose code dependencies changed since the last run. ~5s typical after seeding. | Tight inner loop — every edit. |
| `make test-changed` | Alias for `test-affected` (Pillar 5 remote-control name preference). | Same as above. |

`.testmondata` (the per-test fingerprint cache) is per-machine,
gitignored. The first invocation seeds it via a full pass and is slow;
subsequent invocations are fast.

**Don't** run testmon against `tests/equivalence/` — testmon's "skip
unchanged" semantics conflict with snapshot regen-detection. Equivalence
runs in its own tier.

### Individual files (debugging)

- All: `python3 -m pytest tests/ -x`
- One file: `python3 -m pytest tests/test_<name>.py -x`
- One test: `python3 -m pytest tests/test_<name>.py::test_func -x`

### Mutation testing (Pillar 5 — one-time baseline)

`make test-mutmut` runs mutmut against `bot/engines/{volatility,probability}.py`
to grade the equivalence harness. Long-running (~1-2h on Mac); usually
launched in the background. Output goes to `mutants/` (gitignored). The
baseline tally lives in `kb/findings/mutmut-baseline-<date>.md`. Per
ticket 86b9ve11y AC: re-run is human-driven, not part of the agent loop.

## Conventions
- One test file per concern. Mirror the bot/_impl.py class/function being tested.
- Real DB, not mocks — integration tests must hit a real sqlite3 file (use `tmp_path`).
- For sizing/Kelly assertions, use the actual `OrderExecutor` paths, never reimplement Kelly inline.
- Regression tests after bug fixes: name `test_<bug_keyword>_regression` and reference the commit/incident in a one-line docstring.

## When adding a test
- Match existing file naming and fixture patterns — read 2-3 sibling tests first.
- AST-style guards (`test_call_sites.py`, `test_db_signatures.py`, `test_config_consistency.py`) catch signature drift; extend these rather than writing parallel checks when the failure mode fits.

## Equivalence harness (`tests/equivalence/`, Pillar 3)

- Snapshot files (`tests/equivalence/test_*/`) pin engine outputs.
  **Do not** run `pytest --force-regen` autonomously — regen is a
  human-with-diff-review operation. If a snapshot fails, investigate
  the divergence first; the snapshot is the contract.
- `conftest.py::isolate_calibration_singletons` patches
  `_CALIBRATION_ENGINE` to None so `ProbabilityEngine.compute()` takes
  the deterministic passthrough/fixed-beta cascade. Bit 6.3 (CalibrationEngine
  extraction) owns extending this to inject a frozen learned-method oracle.
- Full runbook: `tests/equivalence/REGEN.md`.

## TDD-with-hook (Pillar 4)

`.claude/hooks/tdd_guard.py` is a `PreToolUse` hook on
`Edit|Write|MultiEdit` (wired in `.claude/settings.json`). It blocks
edits to `bot/**/*.py` unless the session transcript shows a prior
`Edit|Write|MultiEdit` of any file under `tests/`.

The intent is structural enforcement of test-first discipline on new
extractions and new behavior — Cherny's TDD-with-agents pattern.
Existing untested `bot/` code is grandfathered: the hook only blocks
*new edits without a paired test edit in the same session*.

### Bypass markers

Use sparingly, with rationale:

| Marker | Scope | Use case |
|---|---|---|
| `KALSHI_TDD_BYPASS=1` env var | per-session | refactor sessions covered by Pillar-3 equivalence; emergency hotfix |
| `[no-tdd]` in HEAD commit subject | per-Bit | doc-only Bits (e.g., 4.2.5.x README sweeps); `git mv` Bits (e.g., 2.3 module renames); refactors with property-based equivalence already proving behavior unchanged |

The hook checks `git log -1 --format=%s` and exits 0 if `[no-tdd]`
appears anywhere in the subject.

### Scaffolding a failing test

Use `/test-writer <target>` to scaffold a RED test mirroring the
target's path under `tests/`. The skill applies sibling conftest
patterns (heavy-dep mocking, calibration-singleton isolation) and
runs pytest to confirm the test fails before handing off to the
implementation pass.

### What counts as a "test edit"

Any `Edit|Write|MultiEdit` of a `.py` file under `tests/` (recursive),
including `tests/conftest.py` and `tests/equivalence/conftest.py`.

**Non-`.py` files under `tests/` do NOT count** — snapshot YAML/CSV
(under `tests/equivalence/test_*/`), `tests/REGEN.md`, and any
`.DS_Store`/dotfile are not "tests" in the TDD sense. This pairs
with the Pillar-3 rule that snapshot regeneration is human-review-
only; allowing snapshot edits to bypass the hook would let a
careless agent re-baseline the harness mid-flow.

The relevance of the test to the `bot/` change is honor-system —
the hook is a workflow nudge, not a correctness verifier.

**Note:** the repo-root `conftest.py` (sibling of `bot/`, `tests/`,
`scripts/`) does NOT count — only files under `tests/`. If you edit
the root conftest to add a fixture and then edit `bot/`, the hook
will still block; either move the fixture into `tests/conftest.py`
or use a bypass marker.

### What does NOT trigger the hook

- Edits to `bot/CLAUDE.md` or any non-`*.py` file
- `git mv` / `rm` via `Bash` (those go through the `Bash` tool, not `Edit`/`Write`)
- Anything outside `bot/`

### Failure modes

The hook distinguishes infrastructure errors from contract violations:

| Condition | Behavior | Rationale |
|---|---|---|
| Malformed stdin JSON | **fail open** (exit 0) | Workflow nudge, not a correctness gate. Don't penalize harness bugs. |
| `transcript_path` field present but file missing on disk | **fail open** | Legitimate during the very first tool_use of a fresh session before the harness flushes. |
| `transcript_path` field empty string | **block** | Per Claude Code hook spec the field is mandatory; empty indicates contract regression or spoofed input. Conservative default. |
| `git` unavailable / no commits / not a repo | **fail open** on the marker check | The transcript-scan path still runs; only the `[no-tdd]` bypass is forfeit. |
| Sub-agent (Task) edit | scoped to the sub-agent's own transcript | A parent's test edit does NOT satisfy a sub-agent's hook (each session has its own `transcript_path`). The sub-agent must write its own test, or the parent uses a bypass marker. |

The hook is a workflow nudge, not a load-bearing gate; the equivalence
harness (Pillar 3) is the actual correctness ratchet for engine
extractions.
