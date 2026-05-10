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

## Lessons

(Filed for index when this Bit fully ships through reviewer-agent +
push. Tentative numbering pending the parallel Sprint 9 session's
recent L-additions.)

- **L8x — fail-open hook is BaseException-bounded, not Exception-bounded.**
  A KeyboardInterrupt during `git commit` should still let the commit
  complete; the load-bearing-block guard catches `BaseException` and
  exits 0, with `SystemExit` re-raised as the explicit carve-out.
- **L8x — test-only env hooks for fail-open paths.** Some fail-open
  branches (forced import failure, forced exception) cannot be reached
  by external manipulation of fixtures alone. Adding two clearly-named
  `KALSHI_PSC_HOOK_FORCE_*` env hooks (off in production, asserted-only
  by hermetic tests) is the lowest-coupling way to cover them.
- **L8x — `git rev-parse --git-path hooks` is worktree-aware.** Don't
  hand-roll `.git/hooks/` resolution. The plumbing command yields the
  right path whether invoked from the main checkout or any worktree.
