# Tests

~5,000 tests collected. Pytest. `conftest.py` at repo root.
Verify exact count with `python3 -m pytest tests/ --collect-only -q | tail -1`.

## Layout (post-Bit-12.2)

Bit 12.2 (Sprint 12, 2026-05-11) reorganized `tests/` into tier-named
subdirectories. The tier is now visible from the tree — `make test-<tier>`
runs `pytest tests/<tier>/`. Pattern adapted from Hypothesis's
`cover/`/`nocover/` split (the only OSS Python project with a comparable
explicit-tier discipline; surveyed pytest, Django, Flask, FastAPI,
scikit-learn, pandas, Sentry, requests, httpx, Pydantic, Aider before
choosing this layout).

```
tests/
  unit/          # Tier 1 — pure invariants, no DB/network (<10s, ~sub-second)
  contracts/     # Tier 2 — Pillar 1 public_api + Pillar 2 import-linter + AST guards
  equivalence/   # Tier 3 — Pillar 3 engine snapshots (volatility, probability)
  integration/   # Tier 4 — everything else, real DB, broad behavioral suite
  regression/    # Sprint-1 legacy bucket (8 files pinned by test_no_root_test_files.py)
  hooks/         # Test infrastructure (pre-commit hook tests)
  fixtures/      # Shared fixture data (non-test files)
```

**File-classification rule for new tests:**
- Pure invariants on repo state / packaging / Makefile / docs → `tests/unit/`
- AST guards, public_api snapshots, import-linter contracts → `tests/contracts/`
- Engine equivalence snapshots → `tests/equivalence/` (Pillar 3 isolation; regen is human-only)
- Bug-fix regression tests → mirror the feature being tested in `tests/integration/` and name `test_<bug_keyword>_regression`; reserve `tests/regression/` for the 8 Sprint-1 files
- Everything else (behavioral, multi-module, real-DB) → `tests/integration/`

## Run

### Tiered (Pillar 5 — preferred for the agent loop)

`make test-<tier>` is single-source-of-truth in the Makefile; CI mirrors
the same targets in `.github/workflows/test.yml` + `deploy.yml`.

| Tier | Budget | Contents | When to run |
|---|---|---|---|
| `make test-unit` | <10s (sub-sec actual) | Pure-Python invariants: pyproject parsing, Makefile parsing, repo hygiene. No DB, no network. | Every save (or every edit, via `test-affected`). |
| `make test-contract` | <5s budget / ~12s actual on Mac | Pillar 1 public_api snapshot + Pillar 2 import-linter + AST guards (extraction tests, call_sites, db_signatures, config_consistency, order_outcome_vocab). The Mac overshoot is fundamental — AST-walking the large canonical bot modules (`bot/scanner/__init__.py` ~9.4K LOC, `bot/executor.py` ~5.4K LOC, `bot/main_loop.py` ~2.2K LOC) is bounded by file size; CI Linux clears the budget. Pre-Bit-9.3-iii.c the dominant scan target was bot/_impl.py (now deleted). | After any change to `bot/`, `pyproject.toml`, or `.importlinter`. |
| `make test-equivalence` | <30s (~3s actual) | Pillar 3 numeric snapshots + property tests for `bot/engines/{volatility,probability}.py`. Frozen calibration via `tests/equivalence/conftest.py`. | After any change to `bot/engines/`, `bot/constants.py`, or anything that flows into engine inputs. |
| `make test-integration` | <30s wall (~22s actual locally; alias for both shards) | Bit-9 (2026-05-17) alias that runs both shard targets sequentially. | Before opening a PR. |
| `make test-integration-shard-0` | <30s (~11s actual; ~half the corpus) | Bit-9: pytest-shard `--shard-id=0 --num-shards=2` + xdist. Hash-balanced split of the broad behavioral suite. | Auto-runs as part of `make test`; in CI runs as its own GH job concurrent with shard-1. |
| `make test-integration-shard-1` | <30s (~11s actual; ~half the corpus) | Bit-9: pytest-shard `--shard-id=1 --num-shards=2` + xdist. | Same — own GH job concurrent with shard-0. |
| `make test-integration-serial` | <20s (~16s actual) | 11 @serial-marked timing-sensitive tests (subprocess/threading-Barrier/SIGALRM/daemon-thread-log-race) run in a single worker. Bit-5 (CI perf umbrella 86b9zjtzk); count grew via Bit-7 fix-forwards. | Auto-runs as part of `make test`. |
| `make test` | ~2min (sum of above) | All tiers + serial-marked tests in order, fail-fast on the cheapest. | Before pushing to main. |

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
- One file: `python3 -m pytest tests/<tier>/test_<name>.py -x`
- One test: `python3 -m pytest tests/<tier>/test_<name>.py::test_func -x`

### Mutation testing (Pillar 5 — one-time baseline)

`make test-mutmut` runs mutmut against `bot/engines/{volatility,probability}.py`
to grade the equivalence harness. Long-running (~1-2h on Mac); usually
launched in the background. Output goes to `mutants/` (gitignored). The
baseline tally lives in `kb/findings/mutmut-baseline-<date>.md`. Per
ticket 86b9ve11y AC: re-run is human-driven, not part of the agent loop.

**Concurrency guard (ticket 86b9vgh1a).** `make test-mutmut`,
`make test-equivalence`, and `make test-integration` all acquire an
exclusive `fcntl.flock(LOCK_EX | LOCK_NB)` on `.mutmut.lock` (repo
root, gitignored) via `scripts/ops/_mutmut_lock.py` before they run. If
one is already active, the contender exits non-zero with a clear
"another mutmut/tier-test invocation holds the lock" error to
stderr. This replaces the prior honor-system "don't run in parallel"
warning — mutmut's in-place mutation of `bot/engines/` would
otherwise corrupt a parallel reader's source view. The wrapper uses
Python's stdlib `fcntl` (not `flock(1)`, which is Linux-only) so the
guard works identically on darwin and Linux. To debug a stuck lock:
`lsof .mutmut.lock` shows the holder PID; the lockfile also contains
the holder PID as a body (advisory).

### Deploy failed at integration tier (operator runbook)

The CI integration tier is asymmetric:
* `test.yml` (PR gate) runs integration as `continue-on-error: true` —
  a red integration step doesn't block PR merge.
* `deploy.yml` (push-to-main gate) runs integration as **blocking** —
  a red integration step DOES block deploy to the VPS.

This means: a PR can merge with green checks even if integration is
red, then the post-merge `deploy.yml` blocks at the integration step
and the bot stays on the prior commit. Symptom = `deploy` job red on
the merge commit; bot still healthy on the prior version.

Recovery:
1. **Confirm the bot is still running on the prior commit**:
   `ssh -t botuser@<vps> "systemctl is-active kalshi-bot"` — should
   say `active`. The deploy aborted before `git reset --hard`.
2. **Triage locally**: `git checkout main && git pull && make
   test-integration`. Read the failure(s).
3. **Fix forward** if the regression is small: open a follow-up PR
   that fixes the integration failure. Once that lands and deploy.yml
   gates green, the VPS will pull the merged remediation.
4. **Or revert** if the merge commit is sizable / time-sensitive:
   `git revert <merge-sha>` and push. The revert triggers a fresh
   `deploy.yml` against the reverted state, which will redeploy the
   prior healthy version.

Do NOT push directly to main with a `[skip ci]` flag to bypass the
gate — that's how stale-deploy incidents start.

## Conventions
- One test file per concern. Mirror the canonical bot submodule class/function being tested (e.g., `bot/scanner/__init__.py::OpportunityScanner` → `tests/integration/test_scanner_extraction.py` + `tests/integration/test_scan_*`; bot/_impl.py was DELETED in Bit 9.3-iii.c).
- Real DB, not mocks — integration tests must hit a real sqlite3 file (use `tmp_path`).
- For sizing/Kelly assertions, use the actual `OrderExecutor` paths, never reimplement Kelly inline.
- Regression tests after bug fixes: name `test_<bug_keyword>_regression` and reference the commit/incident in a one-line docstring.

## When adding a test
- Match existing file naming and fixture patterns — read 2-3 sibling tests first.
- AST-style guards (`tests/contracts/test_call_sites.py`, `tests/contracts/test_db_signatures.py`, `tests/contracts/test_config_consistency.py`) catch signature drift; extend these rather than writing parallel checks when the failure mode fits.

## Equivalence harness (`tests/equivalence/`, Pillar 3)

- Snapshot files (`tests/equivalence/test_*/`) pin engine outputs.
  **Do not** run `pytest --force-regen` autonomously — regen is a
  human-with-diff-review operation. If a snapshot fails, investigate
  the divergence first; the snapshot is the contract.
- `conftest.py::isolate_calibration_singletons` (autouse) patches
  `bot.engines.calibration._CALIBRATION_ENGINE` to None so
  `ProbabilityEngine.compute()` takes the deterministic
  passthrough/fixed-beta cascade. Bit 6.3 path-B extended this with
  the opt-in `install_frozen_cal_engine` fixture (vendored-snapshot
  flavor — hand-crafted Platt state) for tests that need to exercise
  the learned-method branches of the cascade. See
  `tests/equivalence/REGEN.md` § "Calibration-engine isolation
  (post-Bit-6.3 path-B)" for the full pattern.
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
