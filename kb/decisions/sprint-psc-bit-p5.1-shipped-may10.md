# Sprint PSC Bit P5.1 SHIPPED — session lock infrastructure

**Ticket:** ClickUp `86b9vgx5d` (P5.1: Lock infrastructure — O_CREAT|O_EXCL lockfile + heartbeat + stale-reclaim)
**Date:** 2026-05-10
**Branch:** `86b9vgx5d-psc-lock-infra` (NOT pushed; reviewer agent gate next)
**Files:**
- `scripts/_session_lock.py` (new, 290 LOC, stdlib-only)
- `tests/test_session_lock.py` (new, 415 LOC, 28 tests)
- `kb/decisions/sprint-psc-bit-p5.1-shipped-may10.md` (this file)

## RCA — why this primitive

The kalshi-bot repo runs **multiple concurrent Claude Code sessions**:
- Parent orchestrator on the main checkout.
- Per-Bit worktree workers under `.claude/worktrees/`.
- Background routines (Pillar 5, /loop, schedule).

Without coordination they collide on shared write surfaces:
- **`MEMORY.md`** — concurrent appends produce corrupt index entries (the user's auto-memory file). Witnessed `feedback_parallel_sessions.md` 2026-05-09 (HEAD moved 277e8ab→247c738 mid-session during Bit-A.1).
- **origin/main** — both sessions push the same Bit; second push fails or, worse, ships divergent code. Witnessed during Phase 0a (`ec7eefa` rebase needed: origin moved 3× during a single session — `f26a611→90fdf9b→c84c94a→ad5b781`).
- **`agent_docs/*` + `kb/*`** — the same finding gets written twice from parallel sessions.
- **`bot/_impl.py`** — two extraction Bits each rebase on top of stale main, each shipping different "monolith → module" line ranges.

`kb/decisions/parallel-session-coordination-may09.md` (the spike that filed this Bit family) defined the lockfile + heartbeat + stale-reclaim approach mirroring [mclaude](https://github.com/AnastasiyaW/mclaude). P5.1 ships the leaf primitive that subsequent hooks consume:
- **P5.2 PreToolUse** — `Edit`/`Write` calls block on a `SessionLock` for the touched path.
- **P5.3 git-pre-commit** — load-bearing — every commit acquires a per-target lock + does `git fetch && merge-base --is-ancestor` to ensure we're not racing main.
- **P5.6 memory-write** — every MEMORY.md mutation goes through a single global `SessionLock`.

P5.4 (SessionStart heartbeat probe) and P5.5 (`/pickup` CAS + worktree-default) also consume this primitive but on different code paths.

## TDD discipline

1. **R0 RED:** wrote `tests/test_session_lock.py` first; `pytest tests/test_session_lock.py` failed at import (`scripts._session_lock` did not exist). 1 collection error, 0 passes.
2. Implemented `scripts/_session_lock.py` (~250 LOC pre-fix).
3. **First green:** 27/28 PASS, 1 FAIL (`test_lockfile_persists_after_kill_then_reclaim_recovers`).
4. **Root cause:** the heartbeat thread fired immediately on start, racing the test's backdate write. The `_heartbeat_loop` had `try-tick → wait` ordering. Inverted to `wait → try-tick`: lockfile is fresh from `acquire_raw` so an immediate first tick is wasted I/O AND introduces a race window.
5. **R1 GREEN:** 28/28 PASS.

## R0 self-review (top-to-bottom diff read)

Issues caught + fixed in-Bit (no follow-up needed):

1. **MAJOR — quarantine sidecar collision:** two sessions racing to quarantine the same malformed lockfile produced identical millisecond stamps. Fix: stamp is now `unix-millis.pid`.
2. **MAJOR — heartbeat tmp-file collision:** tmp-name was `<lock>.tmp.<pid>`; if the same process held the same target-path lock twice (impossible by acquire_raw invariant, but defense-in-depth), tmps would collide. Fix: tmp-name is `<lock>.tmp.<pid>.<session_id[:8]>`.
3. **MAJOR — `is_stale(path, now=0.0)` falsy-coalesce bug:** `(now or _now())` would treat `0.0` as falsy and silently substitute the wall clock. Fix: explicit `None` check.
4. **MINOR — heartbeat-vs-reclaim race window:** documented in-code. The window is microseconds. Fix deferred (would require fcntl advisory lock); recorded in module docstring as known acceptable degradation.

Issues NOT requiring change (analyzed + dismissed):
- Concurrent reclaim: A and B both judge stale → both unlink (one wins, other gets FileNotFoundError, swallowed) → both retry `_try_create()` → only one wins via O_EXCL. **Correct by construction.**
- `_LockContext.__exit__` doesn't suppress body exceptions: **correct behavior** — exceptions should propagate; we just guarantee release.
- 3-pass retry exhaustion in `acquire_raw`: bounded so we don't infinite-loop on pathological state. **Correct policy.**

## Test inventory (28 tests)

| Suite | Test | Purpose |
|---|---|---|
| TestPathFlatten | 6 tests | Round-trip; rejection of absolute / traversal / empty-component |
| TestAcquireRelease | 6 tests | Happy path, idempotent release, lockfile location, naming |
| TestHeartbeat | 3 tests | mtime updates over time, daemon=True invariant, stops on release |
| TestConcurrentAcquire | 3 tests | Same-target-second-instance, different-targets-OK, **subprocess race** |
| TestStaleReclaim | 4 tests | Within-threshold not stale, past-threshold stale, reclaim succeeds + audits, fresh-but-dead-pid is honored |
| TestProcessDeathCleanup | 1 test | **kill -9 simulation** via `os._exit`; lockfile persists; reclaim recovers |
| TestMalformedLockfile | 2 tests | Corrupt JSON quarantined; missing required keys quarantined |
| TestICloudSuffix | 1 test | `* 2.lock` sibling does NOT block canonical acquire |
| TestStress | 2 tests | 20-cycle no-leak; 5-thread serialization |

## iCloud mitigation

`kb/decisions/parallel-session-coordination-may09.md` flagged that this repo lives under iCloud Drive, which can spawn `* 2.lock` collision siblings. Mitigation:

- **Acquire is name-direct, not glob-walked.** O_CREAT|O_EXCL is on the canonical `<flat>.lock` path. iCloud never inserts characters into our chosen filename — only adds siblings.
- **Stale-reclaim only inspects the canonical path.** A `* 2.lock` sibling is ignored, not honored.
- **The `__SLASH__` token** is upper+double-underscore — chosen because (a) it cannot appear in a real POSIX path, (b) survives iCloud's filename quirks (no spaces, no special chars).
- **`active-work/` directory** is under `.claude/`, which is configured by `.claude/settings.json`; iCloud sync exclusion can be added later via Finder if churn becomes a problem.

Test `TestICloudSuffix::test_icloud_collision_sibling_is_ignored` pins this contract.

## Acceptance criteria checklist

- [x] `scripts/_session_lock.py` shipped, stdlib-only (verified: imports = `errno`, `json`, `os`, `threading`, `time`, `uuid`, `pathlib`, `typing`)
- [x] `SessionLock(target_path).acquire()` returns context manager; `__exit__` releases cleanly
- [x] Heartbeat thread updates JSON `last_heartbeat` every 30 s while held; `daemon=True` invariant pinned by test
- [x] Concurrent acquire from 2nd PID raises `LockHeldError` with held-by metadata (subprocess test)
- [x] Stale lock (`last_heartbeat > 180 s`) reclaimable with audit log entry (`RECLAIM_STALE` event in `.claude/locks/reclaim.log`)
- [x] All call sites grep-checked: `git ls-files | grep -i lock` → zero collisions with existing infra (only test files reference `lock` semantically)
- [x] Tests pass: `pytest tests/test_session_lock.py` → 28 passed in ~2.7 s
- [x] R0 self-review complete; 4 issues caught (3 fixed, 1 documented)
- [x] No `bot/` or `bot.constants` import — pure stdlib helper
- [x] KB closeout commit (this file)
- [x] Pre-flight grep `git ls-files | grep -i lock` → zero collision

## Out-of-scope findings (for orchestrator to file)

None. Self-contained leaf module.

## Next in PSC chain

P5.2 (`86b9vgx8n`, S/caution) — PreToolUse hook consumes `SessionLock` for `Edit`/`Write` operations. Schema unblocked.
P5.3 (`86b9vgx9w`, M/caution, **load-bearing**) — git-pre-commit hook. Adds `git fetch && merge-base --is-ancestor` post-acquire.
P5.4 (`86b9vgx9z`, XS/safe/low-pri) — SessionStart probe.
P5.5 (`86b9vgxa9`, S/caution) — `/pickup` CAS + worktree-default — **independent of P5.1**, can ship in parallel.
P5.6 (`86b9vgxcc`, S/safe) — memory-write hook.

## Lessons (lockstep with master lesson list)

The session lessons list is currently at L86. New lessons from this Bit:

- **L87 — heartbeat loops should `wait → tick`, not `tick → wait`.** The lockfile is fresh on acquire; an immediate tick wastes I/O AND opens a race window with callers about to manipulate the file before the first interval. Caught by `test_lockfile_persists_after_kill_then_reclaim_recovers` failing on R0; flipped the loop order; R1 GREEN.
- **L88 — falsy-coalesce on numeric defaults silently corrupts at zero.** `(now or _now())` looks innocuous until a caller passes `now=0.0` for testing-clock purposes and gets the wall-clock back. Always use `now if now is not None else _now()` for numeric optionals. Caught by R0 self-review reading the diff.
