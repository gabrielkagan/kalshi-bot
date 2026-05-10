# Sprint PSC Bit P5.3 SHIPPED — git pre-commit hook (lock + main-fetch divergence)

**ClickUp:** 86b9vgx9w
**Branch:** `86b9vgx9w-psc-pre-commit-hook` (rooted on `origin/main` at `dc3d465`)
**Ship status:** committed in worktree, NOT pushed (reviewer-agent run pending)

## RCA / problem statement

Two (or more) Claude Code sessions touch this repo concurrently — parent
orchestrator on the main checkout, per-Bit workers in
`.claude/worktrees/<name>/`. Without a coordination layer at `git commit`
time, P5.1's lockfile primitive is advisory-only: a worker session that
ignores (or never queries) the lockfile can still:

1. Stage and commit a file that another session is actively editing
   (silent overwrite on rebase).
2. Push to `origin/main` while local is behind origin (race the parallel
   session's `[skip ci]` auto-generated whitepaper commit or another
   worker's Bit ship).

P5.3 closes both gaps at the pre-commit boundary:

- **Part A** (lock enforcement): for each `git diff --cached --name-only`
  entry, check `.claude/locks/active-work/<flat>.lock`. Refuse if held
  by a non-self, non-stale peer.
- **Part B** (origin-divergence): on the protected branches (currently
  just `main`), `git fetch origin --quiet` then
  `git merge-base --is-ancestor origin/<branch> HEAD`. Refuse if NOT an
  ancestor — local has diverged.

## Why fail-open is the correct default for a load-bearing hook

A buggy pre-commit hook that exits 1 spuriously blocks **every commit
in the repo** until either:

- the bug is fixed AND `git commit --no-verify` is used to land that fix,
  or
- the symlink is manually removed from `.git/hooks/`.

In a parallel-session topology this is catastrophic: a worker on
`sprint-9-bit-9.3` would be unable to ship its Bit without manual
intervention from the operator. The cost asymmetry of the failure modes
is severe:

| Failure mode | Cost |
|---|---|
| Hook spuriously refuses | Halt all repo development; recovery requires `--no-verify` round-trip. |
| Hook spuriously allows | Worst case: silent overwrite that rebase or post-commit review catches. |

The hook is one layer in a defense-in-depth stack — P5.1 advisory lock,
P5.2 PreToolUse warn, P5.3 commit-time enforce, P5.5 `/pickup` CAS check,
P5.6 memory-write coordination. A single layer being lenient is OK; a
single layer being brittle is not.

Concretely the design contract is:

> **Any unexpected condition — missing lock dir, `_session_lock` import
> failure, lock-check exception, git-fetch failure, ANY top-level
> exception in the hook body — MUST allow the commit with a one-line
> stderr warning.**

The only cases where the hook exits nonzero on the commit-time path are:

1. Part A: a peer (live, non-stale) holds a lockfile for a staged file.
2. Part B: on a protected branch + local has diverged from origin.

And the only nonzero exit on the `--self-test` path is when the operator
runs the self-test against a broken install — which is exactly what they
want to see.

## Test discipline (TDD-first)

Per Pillar 4 + CLAUDE.md extraction-bit discipline, all 15 hermetic test
scenarios were written in `tests/hooks/test_pre_commit.py` BEFORE the
hook script existed, confirmed RED, then turned GREEN by implementing
the hook. The test count maps to the spec:

| # | Scenario | Result |
|---|---|---|
| 1 | Happy path — no lock + non-protected branch | PASS |
| 2 | Part A — staged file held by ANOTHER session → REFUSE | PASS |
| 3 | Part A — staged file held by SELF → allow | PASS |
| 4 | Part A — stale lock → allow | PASS |
| 5 | Part A — lock dir missing → fail-open + warn | PASS |
| 6 | Part A — `_session_lock` import fails → fail-open + warn | PASS |
| 7 | Part B — on `main`, behind origin → REFUSE | PASS |
| 8 | Part B — on `main`, ahead of origin → allow | PASS |
| 9 | Part B — on `main`, equal to origin → allow | PASS |
| 10 | Part B — feature branch (non-protected) → skip | PASS |
| 11 | Part B — fetch fails → fail-open + warn | PASS |
| 12 | Bypass — `git commit --no-verify` succeeds even when Part A would refuse | PASS |
| 13 | Exception in hook body → fail-open + warn | PASS |
| 14 | `--self-test` exits 0 on healthy install with "OK" stdout | PASS |
| 14b | `--self-test` exits 1 when `_session_lock` unimportable | PASS |

All 15 PASS in 2.32s.

### Hermeticity

- No test modifies the worktree's `.git/hooks/` directory. Scenario 12
  (the bypass test) installs a symlink in the *tmp test repo's*
  `.git/hooks/pre-commit`, which is automatically discarded when
  `tmp_path` is cleaned up.
- Each test constructs a fresh `git init` repo in `tmp_path`. Lock root
  is redirected via `KALSHI_SESSION_LOCK_ROOT` env override (honored by
  P5.1's `_lock_root()`).
- `GIT_CONFIG_GLOBAL=/dev/null` + `GIT_CONFIG_SYSTEM=/dev/null` prevents
  inheritance of the operator's machine config.
- Two test-only env hooks added to the production hook to exercise
  otherwise-unreachable fail-open paths:
  - `KALSHI_PSC_HOOK_FORCE_IMPORT_FAIL=1` → makes `_import_session_lock`
    return None (scenario 6).
  - `KALSHI_PSC_HOOK_FORCE_EXCEPTION=1` → raises RuntimeError mid-body
    so the top-level guard's catch path is covered (scenario 13).
  Both are pure-passive (off in production) and clearly documented in
  the hook docstring.

## Fail-open verification — every code path where exceptions could escape

The hook's outer `main()` wraps `_main_inner()` in
`try / except BaseException`. Every internal helper is also bounded:

| Helper | Exception path |
|---|---|
| `_warn`, `_refuse` | swallow `Exception` on stderr write |
| `_import_session_lock` | `try/except Exception` → return None + warn |
| `_git` | `try/except (FileNotFoundError, OSError)` → rc=127 |
| `_staged_files` | reads `_git` return code, warns on nonzero |
| `_current_branch` | reads `_git` return code, returns None on error |
| `_check_part_a` | every loop iteration's `flatten`, `read_meta`, `is_stale_fn` calls are individually `try/except Exception` |
| `_check_part_b` | `git remote get-url`, `git fetch`, `git rev-parse`, `git merge-base` all fail-open on rc != 0/1 |
| `_self_test` | top-level `try/except Exception` in `main()` wraps it |
| `main` (outer) | `try / except SystemExit: raise` + `except BaseException: warn + return 0` |

The `SystemExit` re-raise is intentional — `sys.exit()` is currently
unused inside the body, but the carve-out lets future contributors
short-circuit cleanly without the BaseException catch swallowing it.
`KeyboardInterrupt` falls into the BaseException branch by design —
Ctrl-C during commit should fail-open, not permanently block.

## Files touched

- `scripts/git_hooks/pre-commit` (NEW, executable, ~400 LOC including docstring)
- `tests/hooks/__init__.py` (NEW, empty package marker)
- `tests/hooks/test_pre_commit.py` (NEW, ~420 LOC, 15 tests)
- `Makefile` (added `install-hooks` target + help line; +.PHONY entry)
- `README.md` (added Setup `### Install git hooks` subsection after Run)
- `kb/decisions/sprint-psc-bit-p5.3-shipped-may10.md` (this file)

## Worktree caveat for `make install-hooks`

`git rev-parse --git-path hooks` resolves to the *common* git-dir's
`hooks/` (the main checkout's `.git/hooks/`), not the worktree's
per-worktree subdir. This is correct: installing once from any worktree
covers every worktree on the same checkout, matching how `core.hooksPath`
behaves. The Makefile target uses `git rev-parse --git-path hooks`
specifically so worktree-aware installation Just Works.

## Out-of-scope findings

None this Bit. Out-of-scope items already tracked:

- `86b9vgx8n` — P5.2 PreToolUse hook (warn-only, complementary surface
  to P5.3's refuse-at-commit-time).
- `86b9vgxa9` — P5.5 `/pickup` CAS + worktree-default (independent of P5.3).
- `86b9vgxcc` — P5.6 memory-write coordination.

## R1 review findings + fixes (single atomic commit on top of `12486c1`)

R1 came back with 1 CRITICAL + 3 MAJOR + 4 minor. All addressed; minor
m2 deferred as a separate P5.1 ticket (out of P5.3 scope).

### C1 — Hook would refuse a merge-resolution commit on `main`

The pre-R1 hook's Part B blindly fired `git merge-base --is-ancestor
origin/main HEAD` whenever the branch was protected. But the natural
recovery path from a "local diverged" refusal is exactly `git fetch
&& git merge origin/main` — and a non-trivial merge produces a merge
in progress (`.git/MERGE_HEAD` exists, HEAD is still pre-merge). The
hook's own suggestion would then deterministically fail on the
follow-up `git commit --no-edit`. Self-contradicting refusal.

**Fix.** New helper `_is_mid_operation()` checks for any of:
`MERGE_HEAD`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`, `rebase-merge`,
`rebase-apply`. Resolved via `git rev-parse --git-path <name>` so
worktrees (where `.git` is a FILE, not directory) work correctly.
`_check_part_b` short-circuits with `True, ""` after the protected-
branch check whenever a mid-operation sentinel exists. Test scenario
`s15_mid_merge_commit_does_not_refuse` sets up a real
`.git/MERGE_HEAD` via `git merge --no-commit --no-ff` and asserts
rc=0.

### M1 — `git fetch` had no timeout

`_git(["fetch", "origin", "--quiet"])` called `subprocess.run`
without `timeout=`. A slow VPN, flaky Wi-Fi, or origin temporarily
unreachable but not failing fast would hang the hook for minutes,
blocking every commit-to-main.

**Fix.** `_git()` grew a `timeout` kwarg; `_check_part_b` passes
`timeout=FETCH_TIMEOUT_S` (15s). On `subprocess.TimeoutExpired` the
wrapper returns `rc=124` (GNU-timeout convention) and the caller
fails open with a clear warning recommending a manual fetch. New
test-only env hook `KALSHI_PSC_HOOK_FORCE_FETCH_TIMEOUT=1`
synthesizes the timeout deterministically without a real network
simulation. Test scenario `s16_fetch_timeout_fails_open` asserts
rc=0 + "timed out" / "timeout" in stderr.

### M2 — Existing pre-commit on main checkout blocks install

The main checkout had a 488-byte hand-written `pre-commit` doing
`python3 -c "import ast; ast.parse(...)"` syntax-checking. CLAUDE.md
explicitly says "Syntax-check before commit: `make ast-check`".
Pre-R1 `install-hooks` refused to clobber non-symlinks → operator
must move the file aside manually, losing the syntax-check
functionality silently.

**Fix.** Two parts:
1. `Makefile install-hooks` now detects an existing non-symlink hook
   and renames it to `.git/hooks/pre-commit.local` before installing
   the P5.3 symlink. Refuses only if BOTH `pre-commit` and
   `pre-commit.local` already exist as non-symlinks (operator must
   resolve the collision).
2. P5.3 hook gained `_run_chained_local_hook(argv)` which, before
   Part A/B, looks up `.git/hooks/pre-commit.local` via `git
   rev-parse --git-path hooks` and execs it with the same argv. A
   nonzero exit from `.local` propagates directly (NOT collapsed to
   1) so distinctive return codes (e.g. ast-check shell hook's exit
   1) reach the user untouched. Exec failure (missing executable
   bit, etc.) fails open with a warning — a broken `.local` must
   never permanently block all commits.

Three new test scenarios:
- `s17_chained_local_hook_runs_first_and_can_refuse` — installs a
  `.local` that exits 7, asserts the hook propagates rc=7 + stderr.
- `s18_chained_local_hook_passes_proceeds_to_part_a_b` — installs a
  passing `.local`, asserts Part A/B runs after and the .local
  stderr surfaces.
- `s19_no_chained_hook_skips_gracefully` — affirms absence of
  `.local` → identical behavior to pre-R1.

README §"Install git hooks" updated with the chaining contract.

### M3 — Dead `holder_session_id == my_session_id` branch

P5.1's `SessionLock._session_id` is `uuid.uuid4().hex` — a per-
instance UUID with NO env-var path. The hook's self-check OR'd two
clauses:

    holder_marker == my_session_id or holder_session_id == my_session_id

The second clause could never match in production (UUID vs. a
hypothetical env var). And the env var the hook read,
`KALSHI_SESSION_ID`, is not consulted anywhere in P5.1 — so the
ENTIRE self-check would silently miss in production unless the user
manually set `KALSHI_SESSION_ID` to match the claude marker, which
no one does.

**Fix.** Self-check now goes through `claude_session_marker` ==
`CLAUDE_SESSION_ID` exclusively. The env var P5.1's `SessionLock`
initializes `claude_session_marker` from is the same one we read
here (`CLAUDE_SESSION_ID`), so self-detection is reliable end-to-end.
Hook docstring + helper docstrings updated. Test s03 updated to
exercise the new contract.

### m1 — `STALE_THRESHOLD_S=180` hardcoded in refusal message

The refusal message read "must be >180s old" as a magic number.
P5.1's `STALE_THRESHOLD_S` could be retuned and the docs would silently
drift.

**Fix.** Pull `STALE_THRESHOLD_S` from `session_lock_mod` at message-
construction time. Falls back to "past the stale threshold" prose if
the constant is missing.

### m3 — s12 `--no-verify` test didn't positively assert commit landed

Pre-R1 s12 asserted the bypass returncode was 0 but never verified
HEAD actually advanced. Could mask a silent no-op.

**Fix.** Capture HEAD via `git rev-parse HEAD` before each commit
attempt; assert HEAD is unchanged after the refused plain commit, and
asssert HEAD moved + subject matches "bypass" after the `--no-verify`
attempt.

### m4 — s12 chmodded the real worktree hook

Pre-R1 s12 called `HOOK_SCRIPT.chmod(0o755)` on the real worktree
file. Mode is already 755 (committed that way; verified by s14's
self-test on the real install), so the chmod was redundant — and
moreover an asymmetric side-effect that survives the test (chmod is
not reverted in teardown).

**Fix.** Drop the `chmod` line.

### Deferred — m2 (P5.1 public-export rename)

P5.1's `_read_lockfile_metadata` has a leading underscore but is
read by the P5.3 hook (the hook uses `getattr(mod, '_read_lockfile_
metadata')` to access it). Renaming to `read_lockfile_metadata`
(plus an alias for back-compat) is a separate P5.1 cleanup, not in
scope for P5.3 R1. **Filed as `86b9vgxXX` (TBD by orchestrator).**

## Lessons

(Filed for index when this Bit fully ships through reviewer-agent +
push. Tentative numbering pending the parallel Sprint 9 session's
recent L-additions.)

- **L8x — fail-open hook is BaseException-bounded, not Exception-bounded.**
  A KeyboardInterrupt during `git commit` should still let the commit
  complete; the load-bearing-block guard catches `BaseException` and
  exits 0, with `SystemExit` re-raised as the explicit carve-out.
- **L8x — test-only env hooks for fail-open paths.** Some fail-open
  branches (forced import failure, forced exception, forced fetch
  timeout) cannot be reached by external manipulation of fixtures
  alone. Adding clearly-named `KALSHI_PSC_HOOK_FORCE_*` env hooks
  (off in production, asserted-only by hermetic tests) is the
  lowest-coupling way to cover them. R1 added a third
  (`KALSHI_PSC_HOOK_FORCE_FETCH_TIMEOUT`) on the same pattern.
- **L8x — `git rev-parse --git-path hooks` is worktree-aware.** Don't
  hand-roll `.git/hooks/` resolution. The plumbing command yields the
  right path whether invoked from the main checkout or any worktree.
  The same idiom applies for `.git/MERGE_HEAD` etc — `git rev-parse
  --git-path MERGE_HEAD` resolves correctly in worktrees where `.git`
  is a FILE.
- **L8x — refuse-then-suggest-same-flow is a self-contradicting
  refusal class (R1 C1).** A load-bearing hook that refuses with
  message "do X" must verify the resulting state of X doesn't itself
  trigger refusal. Mid-merge / mid-rebase / mid-cherry-pick / mid-
  revert are the canonical "the user is mid-resolution" states; skip
  the divergence check entirely in those states.
- **L8x — subprocess.run without `timeout=` is a load-bearing-block
  bug (R1 M1).** Any subprocess invocation on the commit-time path
  needs an explicit timeout. Local-only invocations (rev-parse,
  diff --cached, etc.) are fine; only the network-touching ones
  (`fetch`, `push`, etc.) need bounds. 15s is a defensible default
  for git fetch — long enough to tolerate one TCP retry, short
  enough that the user blocks ≤15s.
- **L8x — hook chaining preserves operator's prior workflow (R1
  M2).** If a Bit adds a new pre-commit hook, the install script
  must detect and preserve a pre-existing user-installed hook —
  silently regressing the user's ast-check is a worse failure mode
  than ANY new functionality the hook adds. Rename to `.local` and
  exec it first; propagate its exit code untouched. Exec failure of
  the `.local` itself fails open (so a broken `.local` doesn't
  permanently block commits, mirroring the fail-open contract of
  the parent hook).
- **L8x — dead self-check branches are silent functionality
  regressions (R1 M3).** P5.3 pre-R1 OR'd two self-check clauses;
  one (`holder_marker == my_session_id`) worked, the other
  (`holder_session_id == my_session_id`) was dead because P5.1's
  `session_id` is a UUID with no env-var path. Code-review intuition:
  "does ANY production env-var actually populate this side of the
  equality?" Tag dead-but-defensible code as such with a comment OR
  delete it.
