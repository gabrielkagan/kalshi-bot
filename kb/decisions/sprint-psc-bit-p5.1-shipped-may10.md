# Sprint PSC Bit P5.1 SHIPPED — session lock infrastructure

**Ticket:** ClickUp `86b9vgx5d` (P5.1: Lock infrastructure — O_CREAT|O_EXCL lockfile + heartbeat + stale-reclaim)
**Date:** 2026-05-10
**Branch:** `86b9vgx5d-psc-lock-infra` (NOT pushed; reviewer agent gate next)
**Files (post-R4 HEAD):**
- `scripts/_session_lock.py` (new, 729 LOC, stdlib-only)
- `tests/integration/test_session_lock.py` (new, 1136 LOC, 47 tests)
- `tests/contracts/test_session_lock_gitignore.py` (new, 123 LOC, 8 tests)
- `.gitignore` (4 patterns added under Sprint PSC Bit P5.1 block)
- `kb/decisions/sprint-psc-bit-p5.1-shipped-may10.md` (this file)

Round-by-round size growth: R0 ≈ 290 LOC / 28 tests → R1 +TOCTOU + worktree
fixes + harder concurrency tests → R2 +gitignore contract file + relative-
gitdir resolution → R4 +partial-marker injectivity guard + property test.

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

1. **R0 RED:** wrote `tests/integration/test_session_lock.py` first; `pytest tests/integration/test_session_lock.py` failed at import (`scripts._session_lock` did not exist). 1 collection error, 0 passes.
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
- [x] Tests pass: `pytest tests/integration/test_session_lock.py` → 28 passed in ~2.7 s
- [x] R0 self-review complete; 4 issues caught (3 fixed, 1 documented)
- [x] No `bot/` or `bot.constants` import — pure stdlib helper
- [x] KB closeout commit (this file)
- [x] Pre-flight grep `git ls-files | grep -i lock` → zero collision

## R1 adversarial review findings + fixes

R1 (fresh-eyes reviewer agent) found additional issues that R0 (self-review) missed. R0 reviewed the diff top-to-bottom; R1 looked specifically at the **interaction surface** (concurrent processes, worktree topology, attacker-controlled inputs). All R1 findings were fixed in one atomic commit on top of R0's `e8e9af4`.

### CRITICAL findings

- **C1 — TOCTOU between `O_CREAT|O_EXCL` and payload write.** The previous `_try_create` opened the canonical lockfile path with O_CREAT|O_EXCL, then wrote the JSON payload in a SECOND syscall. Between those two syscalls, the canonical lockfile was observable as a 0-byte empty file. A peer doing `acquire_raw` against the same target would see EEXIST on its O_EXCL attempt, then `_read_lockfile_metadata` would parse the empty file as malformed (JSONDecodeError → None), conclude "malformed", call `_quarantine_malformed`, which `os.replace`s the (still-being-written) file out from under the writer. Both sessions then succeed O_EXCL on the empty canonical path. **Fix:** write+fsync to a per-pid+per-session tmp file (`<flat>.lock.tmp.<pid>.<sid8>`), then `os.link(tmp, canonical)` for atomic publish. link() raises FileExistsError if canonical already exists (preserves O_EXCL semantics) WITHOUT exposing a 0-byte canonical state. Tmp is unlinked after publish; canonical retains the inode via the hardlink. Regression test: `TestTOCTOU::test_no_partial_lockfile_observable_at_canonical_path` (50 trials × 200 snapshots).

- **C2 — Worktree workers never see each other's locks.** `_repo_root()` walked up looking for any `.git` entry, then halted at the first match. In a git worktree, `.git` is a FILE (not a directory) pointing at the parent repo's `.git/worktrees/<name>/`. `Path.exists()` returned true for both, so a worker inside `.claude/worktrees/<name>/` resolved its lock root to `<worktree>/.claude/locks/active-work/`, while a session in the main checkout resolved to `<main>/.claude/locks/active-work/`. Concurrent edits to `bot/_impl.py` from main + worktree NEVER collided — defeating the entire purpose of the primitive for our actual topology (Phase B uses 5 worktrees + main session). **Fix:** branch on `.git`-is-file vs. `.git`-is-dir. If file, parse the `gitdir: <path>` pointer; the common repo root is `gitdir.parents[1].parent` (`<repo>/.git/worktrees/<name>` → `<repo>`). Regression tests: `TestWorktreeRepoRoot::*` (constructs synthetic main+worktree layout, asserts both resolve to same path AND to same `SessionLock.lockfile_path`).

### MAJOR findings

- **M1 — Path-flatten not injective.** `flatten_target_path("a__SLASH__b/c")` and `flatten_target_path("a/b/c")` both produced `'a__SLASH__b__SLASH__c'`. A caller targeting one would block on a lock held for the other. `unflatten_target_path` lied about provenance. **Fix:** reject any target path containing the literal `__SLASH__` token. One-line guard + regression test.

- **M2 — NUL / control-byte in target_path crashed inside `os.open`.** `flatten_target_path("foo/\x00bar.py")` returned a valid-looking flat name, but `acquire_raw` then crashed inside `os.open` with `ValueError: embedded null byte`. Callers catching only `LockHeldError`/`OSError` saw an uncaught crash. **Fix:** reject NUL + all ASCII control characters (0x00-0x1f + 0x7f) in `flatten_target_path`. Regression test covers `\x00`, `\x01`, `\x07`, `\n`, `\t`, `\x1f`, `\x7f`.

- **M3 — `test_concurrent_subprocess_acquire_blocks` was not actually a race test.** The 500 ms `time.sleep` between `p1.start()` and `p2.start()` was ~5 orders of magnitude larger than the realistic TOCTOU window (~38 µs). The test exercised SERIAL acquire — p1 was already locked when p2 tried — and would have passed even with the C1 TOCTOU bug. **Fix:** drop the head start; both children rendezvous on a `multiprocessing.Barrier` so their `acquire_raw()` calls fire within microseconds of each other. 20 trials; assert exactly one "acquired" + one "held" per trial (and CRITICAL: never two "acquired", which would be the C1 regression signature).

- **M4 — `test_thread_concurrent_acquire_serializes` didn't verify exclusivity.** The assertion `len(successes) >= 1 and len(successes) + len(held_errors) == 5` would pass even if all 5 threads acquired sequentially (5 successes, 0 held_errors) — i.e., no actual race occurred and threads each got their own turn. **Fix:** gate threads on a `threading.Barrier` so all 5 attempt acquire within microseconds; winner sleeps 150 ms (>> contention window); assert EXACTLY ONE success per trial. 10 trials.

- **M5 — `test_lockfile_persists_after_kill_then_reclaim_recovers` shortcut the contract.** The child set `_heartbeat_stop` then backdated `last_heartbeat = time.time() - 500` before `os._exit(0)`. A real kill -9 leaves a FRESH heartbeat (within 30 s); the 180 s stale window would not engage. The test verified "lockfile persists" but **not** "reclaim engages after 180 s elapses". **Fix:** drop the backdate; monkeypatch `STALE_THRESHOLD_S = 0.5`, sleep 1.0 s after the child exits, assert that BEFORE the sleep the lock blocks (`LockHeldError`) and AFTER it is reclaimable. Exercises the real 180 s contract at scaled-down clock.

### Lesson

- **L89 — adversarial review must target the interaction surface, not the diff surface.** R0 caught 4 issues by reading the diff top-to-bottom — all of them were "this line is sketchy". R1 caught 2 CRITICALs + 5 MAJORs by asking "what does a peer process see at each microsecond of this operation, in our actual deployment topology?". The C1 bug was invisible from diff-reading because each line was individually correct; the bug lived in the *gap between syscalls*. The C2 bug was invisible because `_repo_root()` looked perfectly reasonable in isolation; it failed only when two callers ran in different filesystem layouts (worktree + main). Lesson: when reviewing concurrency primitives, mentally place two adversarial peers running in lockstep at each line and ask what each observes. When reviewing path-resolution code, place callers in every supported filesystem topology.

## R2 adversarial review findings + fixes

R2 (fresh-eyes reviewer agent) verified ALL R1 fixes (C1, C2, M1-M5) as REAL_FIX (not surface-only) by reading each patched code path top-to-bottom. R2 then ran a fresh adversarial pass against the post-R1 module and found 2 new MAJOR issues that R0 and R1 both missed — both about the boundary between the primitive and its environment (repo gitignore, git's worktree-pointer format), rather than the primitive's internal logic.

### MAJOR findings

- **M6 — Lockfiles + reclaim.log not gitignored.** After any test run or production session, `git status` reports `?? .claude/locks/reclaim.log`. Any agent doing `git add -A`, `git add .claude/`, or an over-eager glob commits machine-specific PIDs/session_ids into the repo. Verified pre-fix: `git check-ignore .claude/locks/active-work/test.lock .claude/locks/reclaim.log` returns exit 1 (not ignored). Self-defeating gap — a coordination primitive that pollutes the repo it coordinates. **Fix:** add 4 patterns to `.gitignore` under a `# Sprint PSC Bit P5.1` comment block: `/.claude/locks/active-work/*.lock`, `*.tmp.*`, `*.quarantine`, `/.claude/locks/reclaim.log`. Leading `/` anchors to repo root (mirrors `/data/`, `/state/` rationale). `.gitkeep` remains tracked (pattern is narrower than the directory). Regression test: new `tests/contracts/test_session_lock_gitignore.py` invokes `git check-ignore` via subprocess on 5 representative lockfile/tmp/quarantine/log paths (parametrized) + asserts `.gitkeep` is NOT ignored + verifies the helper's polarity via a negative-smoke test against `README.md`. 8 tests.

- **M7 — Relative `gitdir:` paths in worktree pointer silently produce wrong lock-root.** git 2.48+ supports `git config --global worktree.useRelativePaths true`, which makes `git worktree add` write `gitdir: ../../.git/worktrees/<name>` (relative) instead of absolute. Pre-fix `_repo_root` did `gitdir = Path(gitdir_str)`, then `common_root = gitdir.parents[1].parent`, then `if common_root.exists(): return common_root`. For a relative path, `Path('../../..')` is interpreted relative to CWD (NOT relative to the .git-pointer file's directory), and `.exists()` happens to return True if CWD is "deep enough". Silently locks onto a wrong-but-existent path. git-worktree(1) documents: "if gitdir is a relative path, it is relative to the location of the worktree's `.git` file" — pre-fix code violates the documented git contract. Main session and worktree session resolve to DIFFERENT lock roots → never collide → exactly the C2 bug rebadged. **Fix:** resolve relative gitdir against `git_entry.parent` (the worktree's .git-pointer-file's directory) before computing `parents[1].parent`. 3-line fix in `scripts/_session_lock.py:136-152`. Regression tests: two new `TestWorktreeRepoRoot::test_relative_gitdir_*` tests that build a synthetic main+worktree layout with a relative-form pointer file and assert both `_repo_root()` and `SessionLock.lockfile_path` collide on the same canonical path. Pre-fix RED verified by `git stash`-ing the module fix: `worktree_root == '../../..'` (which `.exists()` returns True against the run-from CWD, an unrelated path). Post-fix GREEN: both resolve to `main_repo`.

### Tests added in R2

| File | Tests | Purpose |
|---|---|---|
| `tests/contracts/test_session_lock_gitignore.py` (new) | 8 | M6 contract: 5 lockfile-artifact patterns ignored, `.gitkeep` NOT ignored + still tracked, `_check_ignore` polarity smoke |
| `tests/integration/test_session_lock.py::TestWorktreeRepoRoot::test_relative_gitdir_*` (2 new) | 2 | M7 regression: relative gitdir resolves correctly + collides with main checkout |

Total post-R2: 47 tests (37 session_lock + 8 gitignore contract + 2 new worktree). Triple-rerun stability on the concurrency-heavy suite (TestConcurrentAcquire + TestTOCTOU + TestStress + TestProcessDeathCleanup): 3-of-3 PASS at 7 tests each. Contract tier: 32 pass (up from 24, +8 from new contract file). Unit tier: 654 pass.

### Lesson

- **L90 — adversarial review of a coordination primitive must also audit its environment, not just its code.** R0 looked at the diff. R1 looked at concurrent-process interactions. R2 looked at two environmental boundaries: (a) "what does git's filesystem state look like in deployments newer than this codebase?" caught M7 (git 2.48 useRelativePaths); (b) "what happens to the artifacts this primitive writes if the surrounding repo doesn't expect them?" caught M6 (no gitignore). Both bugs lived entirely outside the source file under review — M6 in `.gitignore`, M7 in the git-pointer contract — but rendered the primitive incorrect for its actual deployment. Lesson: when reviewing infrastructure that writes files into a repo, also review the repo's hygiene (gitignore, hooks, CI artifact policy) AND the upstream contracts the infrastructure consumes (git-worktree format, filesystem semantics).

## R3 adversarial review findings + fixes

R3 (fresh-eyes reviewer agent) verified all R1+R2 fixes as REAL_FIX and ran
a fresh adversarial pass against the post-R2 module. R3 returned **0
CRITICAL / 0 MAJOR** with 3 informational minors. No code changes required;
all minors deferred as low-priority follow-ups (see Out-of-scope section):

- **min1 — KB doc LOC drift.** Earlier "Files modified" header listed
  R0-era sizes (`_session_lock.py` 290 LOC / `test_session_lock.py` 415
  LOC / 28 tests) — long stale after R1+R2 churn. Folded into R4 along
  with the rest of the doc refresh.
- **min2 — long target_path > 255 char flat name → uncaught `OSError`.**
  Theoretical (no real path triggers this; kalshi-bot tops out at ~40
  chars). Defer to follow-up ticket.
- **min3 — contrived lock-leak edge case.** Requires a sequence of
  process-level kills + race conditions that is not realistically
  reproducible in production. Defer.

## R4 adversarial review findings + fixes

R4 (fresh-eyes reviewer agent) verified the R1 M1 (`_SLASH_MARKER` literal-
in-input rejection) fix as REAL_FIX, but found that the injectivity claim
in its docstring is **half-true**: the contiguous-substring check closes
direct collision but leaves boundary-straddling collision open.

### MAJOR findings

- **M1 — Path-flatten injectivity is FALSE for boundary-straddling inputs.**
  ``flatten_target_path("a__SLASH/b")`` and
  ``flatten_target_path("a/SLASH__b")`` both produce
  ``'a__SLASH__SLASH__b'``. Neither input contains the contiguous
  literal `__SLASH__`, so the R1 M1 guard accepts both — but
  ``_SLASH_MARKER.join(parts)`` reproduces the marker across the `/`
  boundary, yielding identical flat names. ``unflatten`` is non-bijective:
  ``unflatten(flatten("a/b__SLASH/c"))`` returns ``"a/b/SLASH__c"``, not
  the input. No real kalshi-bot path triggers this today (the bot uses
  conventional `bot/<asset>/_impl.py`-style names), but the docstring +
  KB doc both promised injectivity. **Fix:** in addition to rejecting
  the contiguous marker, also reject any component that
  ``startswith("SLASH__")`` or ``endswith("__SLASH")``. Those two
  predicates cover every non-empty proper suffix/prefix of the marker
  because any longer overlap subsumes one of them (e.g. ``__SLASH``
  ending matches ``__SLASH`` ending; ``SLASH__`` starting matches
  ``SLASH__`` starting). One additional check loop in
  `flatten_target_path` (~5 LOC). Updated the module docstring to
  describe the full bijection contract.

### Tests added in R4

| File | Tests | Purpose |
|---|---|---|
| `tests/integration/test_session_lock.py::TestPathFlatten::test_partial_slash_marker_components_rejected` | 8 (parametrized) | R4 M1 — reject the 8 canonical boundary-straddle inputs: the original collision-pair witness (`a__SLASH/b` + `a/SLASH__b`), plus 6 mirror variants covering both `endswith("__SLASH")` and `startswith("SLASH__")` at different positions. |
| `tests/integration/test_session_lock.py::TestPathFlatten::test_partial_slash_marker_demonstrated_collision_is_blocked` | 1 | R4 M1 — pin the explicit collision pair from the reviewer's demo + assert benign mid-component `SLASH__` / `__SLASH` substrings still flatten. |
| `tests/integration/test_session_lock.py::TestPathFlatten::test_flatten_unflatten_property_random` | 1 | R4 defense-in-depth — random property test over 20 multi-component paths drawn from a probe space with mixed benign + adversarial components, asserting every `flatten` output either raises `ValueError` or `unflatten` is identity. Vacuity guards: at least one of each outcome must occur. Also exhaustively covers 9 adversarial pairs from the 3×3 cartesian product of `{benign, ends-with-__SLASH, starts-with-SLASH__}`. |

Total post-R4: 55 tests (47 session_lock + 8 gitignore contract). Triple-rerun
stability on the concurrency-heavy suite (TestConcurrentAcquire +
TestTOCTOU + TestStress + TestProcessDeathCleanup): 3-of-3 PASS at 7 tests
each. Full file: 47/47 PASS in ~11.5s wall.

### Property-test design notes

The property test (`test_flatten_unflatten_property_random`) is deliberately
NOT hypothesis-based; we don't want to add a hypothesis dependency for one
adversarial check. Instead it:

1. Deterministic seed (`random.Random(20260510)`) — reproducible across CI.
2. Probe space mixes 4 benign components, 5 adversarial components, and 2
   "looks-dangerous-but-benign" components (e.g. `harmlessSLASH__inside`
   has `SLASH__` MID-component, which is fine — only leading/trailing
   counts). The benign-but-suspicious cases are the most informative
   coverage — they prove the rejection is precise, not overbroad.
3. 20 random multi-component (length 2-4) paths per seed. With this probe
   space the run yields ~5 accept + ~15 reject — enough exercise of each
   branch to catch over- or under-rejection regressions.
4. Vacuity guards: `raised >= 1` and `bijection_holds >= 1`. If a future
   patch makes flatten too permissive (no rejects) or too strict (no
   accepts), the test fails LOUDLY rather than silently passing on a
   degenerate probe space.
5. Adversarial-pair exhaustion: the 3×3 cartesian product of
   `{benign, ends-with-__SLASH, starts-with-SLASH__}` is checked
   separately (no randomness). Each cell is a `(left, right)` pair joined
   by `/`; for any cell that `flatten` accepts, the round-trip must be
   identity. This is the minimal generator set for the R4 bug class.

### Lesson

- **L91 — "injectivity" claims on string-encoding functions must verify the
  boundary between components, not just within components.** R1 M1 closed
  direct collision (`flatten` of an input containing the literal contiguous
  marker). R4 M1 caught what R1 missed: even after rejecting the contiguous
  marker, the `join` operation can RECONSTRUCT the marker across a `/`
  boundary if one component ends with a proper prefix of the marker and
  the adjacent component starts with the matching proper suffix. The
  fix is straightforward (reject components touching the marker boundary),
  but the bug was invisible from reading the R1 fix in isolation — every
  individual line of `flatten_target_path` looked correct. The right
  review lens is: "for every adversarial pair `(left, right)` of
  components, does `_SLASH_MARKER.join([left, right])` reconstruct the
  marker?". Pair this with a property test that randomly mixes benign
  and adversarial components and asserts the bijection invariant.

## R5 adversarial review findings + fixes

R5 (fresh-eyes reviewer agent) verified R4 M1's literal+single-component
guards as REAL_FIX for the demonstrated R4 witnesses (`a__SLASH/b` +
`a/SLASH__b`), but found that R4's docstring claim — *"covers every
non-empty proper suffix/prefix of the marker because any longer overlap
subsumes one of these"* — is mathematically false. The marker
`__SLASH__` has 8 proper-prefix/suffix overlap classes (k=1..8); R4
covered only k=2 (`SLASH__` startswith) and k=7 (`__SLASH` endswith).
R5 demonstrated a surviving collision at k=1 + k=8:

```
p1 = 'X__SLASH_/_Y'  parts=['X__SLASH_', '_Y']  → 'X__SLASH___SLASH___Y'  (left ends '_'  = M[:1])
p2 = 'X/_SLASH___Y'  parts=['X', '_SLASH___Y']  → 'X__SLASH___SLASH___Y'  (right starts '_SLASH__' = M[1:])
                                                  ^^ COLLISION ^^
```

`unflatten` returns `p2` for both inputs — non-bijective.

### CRITICAL findings

- **C5-1 — Path-flatten injectivity still false at k=8 + k=1 overlap class.**
  R4's per-component rule (`startswith('SLASH__') or endswith('__SLASH')`)
  rejected the **k=2 startswith** and **k=7 endswith** classes only, on
  the false claim that "longer overlap subsumes one of these". The
  surviving collision uses k=1 left-straddle (`left.endswith('_')`)
  paired with k=8 right-straddle (`right.startswith('_SLASH__')`).
  **Fix (corrected algebra):** the correct invariant has TWO independent
  checks, not one symmetric one:
  - **Left-straddle (k=1..8):** any non-last component ending with
    `_SLASH_MARKER[:k]` reconstructs the marker at position
    `len(comp)-k` of the joined output. There are exactly 8 such
    classes — enumerate over k.
  - **Right-straddle (k where marker has self-overlap):** any non-first
    component starting with `_SLASH_MARKER[len-k:]` reconstructs the
    marker at position `len(preceding)+k` of the joined output, but
    ONLY when `M[k:] == M[:len(M)-k]` (the marker overlaps itself). For
    `__SLASH__` this is exactly `{7, 8}` (computed at module load into
    `_SLASH_MARKER_SELF_OVERLAP_KS`).
  This is bijective by construction: any flat string with a
  non-canonical marker occurrence requires either a left-straddle or a
  right-straddle at the boundary; both are rejected; therefore on the
  accepted set, `unflatten` recovers the exact input. Real Kalshi paths
  (`bot/_impl.py`, `bot/scanner/__init__.py`, `agent_docs/*`, `kb/*`)
  all pass because their leading-underscore basenames hit M[8:]=`_`
  (one char) but NOT M[1:]=`_SLASH__` (eight chars) — the right-straddle
  rule cleanly distinguishes them.

### MAJOR findings

- **M5-1 — Property test pool blind to the surviving collision class.**
  R4's `test_flatten_unflatten_property_random` pool included 5
  adversaries that hit k=2 + k=7 only (`SLASH__x`, `y__SLASH`,
  `z__SLASH__w`, `SLASH__only`, `only__SLASH`) and 2 benign
  "looks-dangerous-but-fine" cases. It deliberately EXCLUDED components
  that would hit k=1..6 or k=8, so 20 random trials × 2-4 components
  had near-zero chance of constructing the k=1+k=8 pair. **Fix:** new
  pool covers EVERY overlap class — for each k in 1..len(M)-1 the pool
  includes both a `comp{M[:k]}` (left-straddle witness) and a
  `{M[k:]}comp` (right-straddle witness) — plus the bare `_`, `__`,
  `___` adversaries, plus the full leading/trailing marker forms. Trial
  count raised from 20 to 200 to give the larger pool a chance to
  exercise every class. Also added **EXHAUSTIVE parametrized enumeration**
  outside the random sweep (no randomness, no dependence on trial
  count): `test_left_straddle_class_k_rejected` parametrized over k=1..8
  + `test_right_straddle_class_k_rejected` parametrized over k=7,8
  (the self-overlap set) + `test_r5_witness_pair_both_rejected` for the
  R5 reviewer's explicit witness pair `('X__SLASH_/_Y', 'X/_SLASH___Y')`
  AND the R4 originals `('a__SLASH/b', 'a/SLASH__b')`. Every overlap
  class now has at least one parametrized test that fails LOUDLY if
  someone weakens the rule.

- **m5-1 (minor) — `str.replace` overlap semantics unpinned.** R5 noted
  that `unflatten_target_path` relies on `str.replace`'s left-to-right
  non-overlapping scan, which is correct but worth pinning with an
  explicit unit test that documents the semantics (the existing property
  test exercises this implicitly via round-trip; the new test makes the
  invariant primary). **Fix:** new
  `test_str_replace_overlap_semantics_for_marker` test that asserts
  `'__SLASH____SLASH__'.replace('__SLASH__', '/') == '//'` (two adjacent
  markers → two slashes, non-overlapping scan) plus a small round-trip
  matrix covering the six canonical accepted shapes.

### Tests added in R5

| File | Tests | Purpose |
|---|---|---|
| `tests/integration/test_session_lock.py::TestPathFlatten::test_left_straddle_class_k_rejected` | 8 (parametrized k=1..8) | C5-1 left-straddle enumeration — for each k, `A{M[:k]}/{M[k:]}B` must raise. |
| `tests/integration/test_session_lock.py::TestPathFlatten::test_right_straddle_class_k_rejected` | 2 (parametrized k=7,8) | C5-1 right-straddle enumeration — for each self-overlap k, `safe/{M[len-k:]}rest` must raise. |
| `tests/integration/test_session_lock.py::TestPathFlatten::test_r5_witness_pair_both_rejected` | 2 (parametrized pairs) | C5-1 explicit witness — both halves of the R5 reviewer's pair AND the R4 originals must raise. Pre-R5: one half of each was accepted. |
| `tests/integration/test_session_lock.py::TestPathFlatten::test_self_overlap_constant_is_correct_for_current_marker` | 1 | Constant sanity-pin — `_SLASH_MARKER_SELF_OVERLAP_KS` matches a fresh recomputation. Forces regen + manual review if the marker ever changes. |
| `tests/integration/test_session_lock.py::TestPathFlatten::test_str_replace_overlap_semantics_for_marker` | 1 | m5-1 — pin `str.replace` left-to-right non-overlapping scan + 6 round-trip cases. |

Total post-R5: **69 tests** (61 session_lock + 8 gitignore contract). Triple-rerun
stability on the concurrency-heavy suite (TestConcurrentAcquire +
TestTOCTOU + TestStress + TestProcessDeathCleanup + TestStaleReclaim):
3-of-3 PASS at 11 tests each. Full file: 69/69 PASS in ~11.6s wall.

### Code changes in R5

- `scripts/_session_lock.py`:
  - Added `_SLASH_MARKER_SELF_OVERLAP_KS` constant (~8 LOC + comment),
    computed eagerly at module load from `_SLASH_MARKER` (= `(7, 8)`
    for the current marker).
  - Replaced the R4 per-component check loop (~10 LOC) with the
    enumerative left-straddle (k=1..8) + right-straddle (k in
    self-overlap set) loop (~25 LOC). Error messages now disambiguate
    which class fired ("marker prefix" vs "marker suffix") for easier
    forensics.
  - Module docstring + function docstring updated with the full
    "Algebra" derivation showing the two straddle cases and why only
    `{7, 8}` self-overlap at k for `__SLASH__`. Explicitly demonstrates
    the C5-1 collision pair and explains why pairwise (per-adjacent-pair)
    checks would NOT suffice — a path like `X/_SLASH___Y` has no
    `parts[i].endswith(marker[:k])` match for the LEFT side `'X'`, so a
    naive pairwise-AND check (the first attempt) misses it. The correct
    invariant is per-component-positional (non-last → left-check;
    non-first → right-check) over the full overlap set.

- `tests/integration/test_session_lock.py`: pool rewrite + 14 new parametrized
  tests (8 + 2 + 2 + 1 + 1). R4-era message-regex `"straddles
  slash-marker"` updated to `"marker (prefix|suffix)"` to match the
  more-specific R5 error messages while still passing for both flavors.

### Lesson

- **L92 — combinatorial overlap rules require k=1..len(marker)-1
  enumeration, not "the two extremes are enough" reasoning.** R4 argued
  by analogy: "`__SLASH` endswith subsumes shorter endings; `SLASH__`
  startswith subsumes shorter startings; therefore those two checks
  cover all overlap classes". The analogy was WRONG: subsumption goes
  the OTHER way — `endswith('__SLASH')` rejects components ending with
  `__SLASH` but NOT components ending with `_` (the shorter k=1
  prefix); the k=1 ending is strictly MORE permissive (more matches)
  than the k=7 ending, not less. The algebra of marker self-overlap
  also matters: for a marker M to be reconstructible across a join,
  EITHER `left.endswith(M[:k])` (no constraint on M) OR
  `right.startswith(M[len-k:])` AND `M[k:] == M[:len-k]` (self-overlap
  required). The self-overlap set is marker-specific; for `__SLASH__`
  it's `{7, 8}`, but for an arbitrary marker it could be empty (no
  right-straddle possible) or larger. **Review lens:** for any
  string-encoding bijection over a multi-component domain, enumerate
  k=1..len(separator)-1 and ask FOR EACH k: (a) does there exist a
  left-side input whose tail is `sep[:k]`? (yes, by construction); (b)
  does there exist a right-side input whose head reconstructs `sep`
  when concatenated with `sep[k:]`? (yes iff `sep[k:] == sep[:len-k]`,
  i.e. self-overlap). Pair with a property test whose pool covers ALL
  k, plus parametrized enumeration as a vacuity-resistant backstop.

## Out-of-scope findings (for orchestrator to file)

- **/ticket — iCloud `* 2.lock` sibling cleanup.** iCloud Drive occasionally spawns `<name> 2.lock` siblings next to `<name>.lock` on the user's repo. The lock primitive ignores them (correct), but they accumulate forever with no cleanup. R1 minor #4. File a janitorial-script ticket: nightly cron to `find .claude/locks/active-work -name '* [0-9].lock'` and unlink if no canonical sibling holds a fresh heartbeat. Low priority.
- **/ticket — `_audit_log` should log to stderr on OSError.** Currently swallows silently if the reclaim log is unwritable. R1 sister recommendation. File: emit a one-line stderr warning when audit-log write fails, so operators notice if `.claude/locks/` is unwritable (e.g., readonly FS, full disk). Best-effort still — must NEVER block lock acquire.
- **/ticket — reclaim.log unbounded growth.** R2 minor m1. The reclaim audit log appends one JSON line per QUARANTINE / RECLAIM_STALE / RELEASE_MISSING event. In steady state this is ~kilobytes/day, but a pathological loop (e.g. a poison-pill malformed lockfile keeps getting recreated by a buggy peer) could fill the disk. File a janitorial ticket: weekly rotation (logrotate-style) or in-process size cap with truncate-on-overflow. Low priority — current usage is <1KB; not load-bearing for correctness.

## Next in PSC chain

P5.2 (`86b9vgx8n`, S/caution) — PreToolUse hook consumes `SessionLock` for `Edit`/`Write` operations. Schema unblocked.
P5.3 (`86b9vgx9w`, M/caution, **load-bearing**) — git-pre-commit hook. Adds `git fetch && merge-base --is-ancestor` post-acquire.
P5.4 (`86b9vgx9z`, XS/safe/low-pri) — SessionStart probe.
P5.5 (`86b9vgxa9`, S/caution) — `/pickup` CAS + worktree-default — **independent of P5.1**, can ship in parallel.
P5.6 (`86b9vgxcc`, S/safe) — memory-write hook.

## Lessons (lockstep with master lesson list)

The session lessons list is currently at L86 / L90 post-R2. New lessons from this Bit (post-R4):

- **L87 — heartbeat loops should `wait → tick`, not `tick → wait`.** The lockfile is fresh on acquire; an immediate tick wastes I/O AND opens a race window with callers about to manipulate the file before the first interval. Caught by `test_lockfile_persists_after_kill_then_reclaim_recovers` failing on R0; flipped the loop order; R1 GREEN.
- **L88 — falsy-coalesce on numeric defaults silently corrupts at zero.** `(now or _now())` looks innocuous until a caller passes `now=0.0` for testing-clock purposes and gets the wall-clock back. Always use `now if now is not None else _now()` for numeric optionals. Caught by R0 self-review reading the diff.
- **L89 — adversarial review must target the interaction surface, not the diff surface.** R0 caught 4 issues reading the diff line-by-line — all "this line is sketchy". R1 caught 2 CRITICALs + 5 MAJORs by asking "what does a peer process see at each microsecond, in our actual deployment topology?". The C1 TOCTOU bug was invisible from diff-reading — every line was correct in isolation; the bug lived in the *gap between syscalls*. The C2 worktree bug was invisible because `_repo_root()` looked reasonable; it failed only when two callers ran in different filesystem layouts.
- **L90 — adversarial review of a coordination primitive must also audit its environment.** R2 found two issues (M6 gitignore, M7 relative-gitdir) that lived entirely OUTSIDE the source file: M6 in `.gitignore`, M7 in the git-worktree pointer contract. Both rendered the primitive incorrect for real deployments. Lesson: when reviewing infrastructure that writes files into a repo, audit (a) the repo's hygiene (gitignore/hooks/CI artifact policy) and (b) the upstream contracts the infrastructure consumes (git-worktree format, filesystem semantics, package conventions).
- **L91 — "injectivity" claims on string-encoding functions must verify the boundary between components, not just within components.** R1 M1 rejected the contiguous literal marker but left boundary-straddling collisions: `flatten('a__SLASH/b')` and `flatten('a/SLASH__b')` both yield `'a__SLASH__SLASH__b'` because `join` reconstructs the marker across the `/` boundary. R4 M1 closed the gap by also rejecting components that `startswith("SLASH__")` or `endswith("__SLASH")`. The review lens: "for every adversarial pair `(left, right)` of components, does `_SEPARATOR.join([left, right])` reconstruct the encoding marker?" Pair with a property test that mixes benign + adversarial components and asserts round-trip identity for every accepted input.
- **L92 — combinatorial overlap rules require k=1..len(marker)-1 enumeration, not "the two extremes are enough" reasoning.** R4 argued by analogy that `__SLASH` endswith + `SLASH__` startswith "cover" all marker-prefix/suffix overlap classes. The analogy was false: subsumption goes the OTHER way (shorter prefixes are more permissive, not less). R5 demonstrated a k=1+k=8 surviving collision: `'X__SLASH_/_Y'` (left ends with `M[:1]='_'`) collides with `'X/_SLASH___Y'` (right starts with `M[1:]='_SLASH__'` AND the marker self-overlaps at k=8 since `M[8:]='_'==M[:1]='_'`). Correct fix: enumerate left-straddle k=1..8 AND right-straddle k where the marker has self-overlap (set is marker-specific; for `__SLASH__` it's `{7, 8}`). Review lens: "for each k in 1..len(sep)-1, (a) does there exist a left-side input whose tail is `sep[:k]`? — yes by construction; (b) does there exist a right-side input whose head + `sep[k:]` reconstructs `sep`? — yes iff `sep[k:] == sep[:len(sep)-k]` (self-overlap)." Pair with property test pool that covers EVERY k AND parametrized enumeration as a vacuity-resistant backstop.
