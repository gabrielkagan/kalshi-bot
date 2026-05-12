# Sprint A.7-fu1 SHIPPED May 10 — FINDING-ONLY: shared helper NOT viable for Phase 0a

**Ticket:** 86b9vgga7 (A.7-fu1: Refactor Phase 0a to import shared helper)
**Outcome:** FINDING-ONLY. No production-code change. Tests + KB closeout only.
**Branch:** `86b9vgga7-state-db-helper-import` (from origin/main 2d10053)
**Effort:** XS (RCA + KB; no LOC delta in `scripts/`)

## TL;DR

The ticket asked us to remove inlined `take_snapshot/compress/compute_sha256/integrity_check` from `scripts/state_db_s3_backup.py` by importing from the canonical helper `scripts/_state_db_snapshot.py` (A.7, c84c94a). On reading both files end-to-end, **the premise is false**:

1. Phase 0a does NOT inline `take_snapshot`, `compute_sha256`, or `integrity_check`. It has `snapshot_sqlite`, `compress`, `decompress`, `compute_object_key`, `LocalDirStore`, `S3RcloneStore`, `_single_runner_lock`, `run_backup` — none of which exist in the shared helper.
2. The two `sqlite3.Connection.backup()` wrappers diverge on **load-bearing production-safety** parameters that cannot be reconciled without regressing both consumers.
3. Phase 0a `compress`/`decompress` use **subprocess + zstd CLI binary** by design (VPS has no `zstandard` Python package — see Phase 0a docstring §"WHY NOT rsync" tail). Shared helper uses **Python-native `zstandard`/`gzip`** modules. These are not equivalent on the deployment target.

The honest call is to file the finding, leave both consumers intact, and update the helper's coordination contract to record why Phase 0a stays on its own primitives.

## RCA — Why the duplication exists and why importing closes nothing

### A.7 (c84c94a) and Phase 0a (ec7eefa) shipped independently

- A.7 (`scripts/cal_mlp/snapshot_state_db.py` + `scripts/_state_db_snapshot.py`) shipped first, May 9, 21:14Z. Designed for **dev-Mac extract-time** snapshots:
  - Content-addressed by SHA-256 (return-value contract).
  - WAL→DELETE journal-mode flip so the snapshot opens cleanly read-only without -wal/-shm sidecars (required for `extract_data.py` repeat-opens).
  - Python-native compression (`zstandard` if importable, else stdlib `gzip`). `zstandard` is not in pyproject (neither main nor `[dev]`); dev-Mac sessions pip-install it ad-hoc when needed.
  - Single-shot `src.backup(dst)` — pages=-1 default. Acceptable because the source state.db on dev-Mac during extract is QUIESCENT (no live bot writes).

- Phase 0a (`scripts/state_db_s3_backup.py`) shipped same day, 21:23Z, ~8 minutes later (8 minutes 20 seconds). Designed for **VPS nightly cron** against the LIVE state.db:
  - No SHA-256 in the bot-facing surface (S3 ETag is the integrity contract; SHA-256 verification lives in `state_db_restore.py` weekly verify path).
  - NO journal-mode flip — the snapshot is uploaded immediately, never reopened locally.
  - **Subprocess `zstd -19`** because Phase 0a chose subprocess `zstd` to avoid adding a Python-module runtime dep (`zstandard` is not in pyproject at all — neither main deps nor `[dev]`; pip-installed ad-hoc on dev-Mac sessions that need it). See Phase 0a docstring §"WHY NOT rsync" for the design rationale; the VPS cron script intentionally stays on the subprocess CLI binary.
  - **Yielding-pages batched `src.backup(dst_conn, pages=100, sleep=0.050)`** — A-C1 finding pin. `pages=-1` (the stdlib default the helper uses) holds the SQLite write lock for the full 30-60s of the 447 MB live DB copy, breaching the bot's 10s busy_timeout and locking writers out. The yielding constants are TESTED by `test_backup_uses_yielding_pages_constants` and `test_writer_makes_progress_during_snapshot`.

### What the helper would have to absorb to be drop-in

For Phase 0a to legitimately import `_snap.take_snapshot()`, the helper would need:

| Phase 0a requirement | Helper today | Drop-in cost |
|---|---|---|
| `pages_per_step=100`, `sleep_between_steps_s=0.050` | hard-coded `src.backup(dst)` (pages=-1) | new signature param + threading through; breaks A.7's existing call site |
| `force=False` overwrite-guard | unconditional overwrite | new signature param; breaks A.7 (relies on `tempfile.TemporaryDirectory` for uniqueness) |
| NO WAL→DELETE flip (snapshot is uploaded-then-discarded) | mandatory flip | optional flag; orthogonal — adds branch the helper doesn't need |
| Return type `None` (Phase 0a discards) | returns `str` (SHA-256) | accept and ignore — minor |
| `tests/integration/test_state_db_s3_backup.py::test_backup_uses_yielding_pages_constants` pins `_BACKUP_PAGES_PER_STEP` | constants live on Phase 0a module | helper-side constants OR keep Phase 0a wrapper that adds them |

The drop-in is NOT XS. It mutates the A.7 helper's signature for needs A.7 doesn't have, then adds a Phase 0a wrapper to translate back. Net LOC delta is POSITIVE, not negative — and it forces the helper to grow toward Phase 0a's VPS-safety constraints that A.7 (extract-time) does not share.

For `compress`/`decompress`, the helper is fundamentally the wrong tool: it uses Python `zstandard` (not installed on VPS) and auto-picks suffix from a base path (incompatible with Phase 0a's explicit-dest CLI contract `compress(src, dst, algorithm)`). Reusing it would either:
- Add a `zstandard` runtime dep to the VPS (declined by Phase 0a design), OR
- Add an algorithm-routing branch that picks subprocess vs Python — at which point the helper IS the duplication, not the deduplication.

### Why this didn't surface at ticket-file time

The ticket was filed during the A.7 ship pass (see MEMORY.md "NEW shared helper ... A.7-fu1=86b9vgga7 filed"), presumably as a hygiene followup before reading Phase 0a end-to-end. Phase 0a's design rationale (subprocess CLI on VPS, yielding-pages safety, A-C1 + A-M6 + A-M7 + B-C1 adversarial findings) is documented in 696 lines of inline docstrings + the operator runbook `scripts/STATE_DB_BACKUP_SETUP.md`. The ticket assumed function-name overlap implied behavior overlap. It does not.

## Acceptance criteria — disposition

| AC | Status | Note |
|---|---|---|
| Inline `take_snapshot/compress/compute_sha256/integrity_check` removed from `scripts/state_db_s3_backup.py` | N/A — none are inlined there. Phase 0a has `snapshot_sqlite/compress/decompress` (different signatures, different runtime constraints). | See RCA above. |
| All 73 Phase 0a tests + 21 A.7 tests pass post-refactor | GREEN baseline confirmed: 61 passed + 2 skipped on `tests/integration/test_state_db_s3_backup.py` + `tests/integration/test_state_db_snapshot.py` (41 + 22 collected; counts diverge from ticket's 73+21 due to consolidation since ship). No refactor → no regression. | Pre-ship pytest run logged below. |
| KB closeout same commit | This file. | — |
| Effort XS | XS (KB only). | — |

## Pre-ship pytest baseline (GREEN, both consumers)

```
$ python3 -m pytest tests/integration/test_state_db_s3_backup.py tests/integration/test_state_db_snapshot.py -x -q
collected 63 items
tests/integration/test_state_db_s3_backup.py ....................................... [ 61%]
.s                                                                       [ 65%]
tests/integration/test_state_db_snapshot.py ....s.................                   [100%]
======================== 61 passed, 2 skipped in 0.71s =========================
```

Of the 2 skips, 1 is zstd-conditional (`test_compression_round_trip_zstd` — no `zstandard` Python module on the dev-Mac running this session, which is itself the Phase 0a constraint in microcosm). The other (`test_lock_path_default_not_under_tmp`) is the `/var/lock` writability skip on Mac dev hosts.

## Signature mismatches surfaced (per ticket instructions: "surface, don't fix")

Three cross-consumer surfaces would need helper-side widening to unify:

1. **`take_snapshot` → `snapshot_sqlite`:** helper needs `pages_per_step`/`sleep_between_steps_s` for VPS-live-DB use. A.7's extract-time use does not need them. Adding them as optional kwargs is technically additive, but it mutates the helper's invariant ("single-shot online backup with mandatory journal-mode flip") into a parameterized state machine.
2. **`compress` → `compress`:** subprocess-vs-Python algorithm dispatch is a runtime-environment branch, not a signature branch. The helper would need a `use_subprocess: bool` or env-detection codepath. Either is a meaningful surface expansion.
3. **`decompress` → `decompress`:** same runtime-environment problem. `state_db_restore.py` (NOT in scope for this ticket) consumes `state_db_s3_backup.decompress` for the weekly verify path on the VPS; switching it to `_snap.decompress` would import `zstandard` on a host where it isn't installed.

### Bonus divergence surfaced post-R1: a THIRD `integrity_check` impl

R2 review caught that the worker's R1 analysis missed a third divergent implementation. `scripts/state_db_restore.py:117` defines its own `integrity_check(db_path: Path) -> List[str]` that:

- Uses `PRAGMA integrity_check(0)` (the `(0)` arg = **unlimited** error count, vs. the stdlib default `PRAGMA integrity_check` which truncates at 100 — round-1 finding A-M2 from the Phase 0a ship pass)
- Adds `PRAGMA foreign_key_check` on top (the helper's `bool`-returning impl does NOT do FK checking)
- Returns `List[str]` of issue strings (vs. the helper's `bool`, vs. Phase 0a's pragma-driven branch)

So there are now **three** divergent `integrity_check` callsites across the snapshot/backup/restore surface:
| Site | Signature | Behavior |
|---|---|---|
| `scripts/_state_db_snapshot.py` (helper) | `() -> bool` | default `PRAGMA integrity_check`, 100-error cap, no FK check |
| `scripts/state_db_s3_backup.py` (Phase 0a) | inline pragma | default `PRAGMA integrity_check`, 100-error cap, no FK check |
| `scripts/state_db_restore.py:117` (verify path) | `(Path) -> List[str]` | `PRAGMA integrity_check(0)` unlimited + `foreign_key_check` |

This **strengthens** the FINDING-ONLY conclusion: an honest unification has to reconcile three different intent levels (bool sanity-probe vs. cap-truncated pragma vs. unlimited+FK detailed audit), not two. The naive "swap to helper" patch would silently weaken the weekly verify path's error-detection from unlimited+FK to cap-100+no-FK.

If a future refactor wants to address this honestly, the right move is probably:
- Promote `_state_db_snapshot.py` from a stdlib-only helper to a **two-layer module**: a low-level core (`_backup_pages_yielding(src, dst, *, pages_per_step, sleep_s, force, flip_journal_mode)`) plus thin wrappers `take_snapshot()` (A.7 defaults — single-shot + flip) and `snapshot_sqlite()` (Phase 0a defaults — yielding + no flip). Both consumers import their wrapper.
- Extract compression into a separate `_compression.py` with a runtime-environment-aware dispatch (subprocess on VPS, Python module on dev-Mac), tested under both regimes.

That is a Sprint A spike-worthy effort (4-8 hours, both consumers + both test suites + new VPS-vs-Mac runtime-environment tests). Not the "XS ~30 LOC" the ticket scoped. File as a fresh ticket if the duplication has been observed to actually drift between consumers (so far it has not — both have been touched zero times since their initial ship).

## Coordination-contract update

The shared helper's module docstring (`scripts/_state_db_snapshot.py:9-15`) says:

> Coordination contract pinned in:
>   kb/decisions/sprint-a-bit-7-plan-may09.md (this session, 86b9vejrj)
>   .claude/worktrees/86b9vd9e3-state-db-s3-backup/kb/decisions/auto-research-phase-0a-plan-may09.md
>
> Do not change a function signature without updating both consumers atomically.

The above statement is still correct for the A.7 consumer (`scripts/cal_mlp/snapshot_state_db.py`). It is **misleading** for Phase 0a — Phase 0a is NOT a consumer of the helper at all. Future helper edits do not need to consider Phase 0a. The coordination contract should be amended to: "Single consumer: scripts/cal_mlp/snapshot_state_db.py. Phase 0a (scripts/state_db_s3_backup.py) intentionally maintains its own primitives, and `scripts/state_db_restore.py` is a sibling-not-consumer that diverges further (its own `integrity_check(0)` + `foreign_key_check` impl at line 117) — see kb/decisions/sprint-a-7-fu1-shipped-may10.md."

This amendment is OUT OF SCOPE for the current ticket (would touch `_state_db_snapshot.py`, which is on the untouchable list for this worker). Filing a follow-up: **A.7-fu1.1** to amend the helper's coordination-contract docstring.

## Lessons (additive to L74-L78 from session-resume-may09-from-pillar-5-merged; collision-checked against PSC P5.1 which took L87-L92, incl. R5-added L91/L92)

- **L93 — Name overlap is not behavior overlap.** Two functions can have identical names + similar signatures and still solve materially different problems if their runtime environments differ (dev-Mac extract-time vs VPS-live-cron). Read both call sites end-to-end before assuming deduplication is XS.
- **L94 — "Inlined duplicate" claims need a pre-refactor read.** The ticket assumed Phase 0a inlined what the helper extracted. It did not; the two were authored independently against different constraints. The pre-refactor read takes 10 minutes and prevents committing a regression.
- **L95 — Renumber MUST grep at renumber-time, not recall-based.** When two adversarial rounds in a row find a lesson-number collision (R2 caught L87/L88 vs PSC P5.1's L87-L90 documented range; R3 caught L91/L92 vs PSC P5.1's R5-added L91/L92 not in the original range), the failure mode is the same: trusting a remembered/cited MAX without re-running `grep -rE "^- \*\*L[0-9]+" kb/ | grep -oE "L[0-9]+" | sort -u | sort -V | tail` at the moment of renumber. Parallel sessions on the same lesson space mean MAX changes between read and write. **Protocol:** run the grep immediately before picking new numbers, pick MAX+1..MAX+k, re-run grep after edit to confirm zero duplicates. Same class of bug R2 found, recurring because the protocol wasn't followed in R2's fix — fix shipped here.

## Files touched

- `kb/decisions/sprint-a-7-fu1-shipped-may10.md` (this file; NEW)

Zero LOC delta in `scripts/`. Zero LOC delta in `tests/`.

## Follow-ups filed

(To be created via `/ticket` after this commit:)

- **A.7-fu1.1** — Amend `scripts/_state_db_snapshot.py` module docstring lines 9-15 to drop the Phase 0a coordination-contract claim. XS, doc-only. Agent-eligible.
- **A.7-fu1.2 (SPIKE)** — Evaluate the two-layer split (`_backup_pages_yielding` core + thin wrappers) to genuinely deduplicate the snapshot codepaths. **Scope must also cover** (a) the three divergent `integrity_check` callsites — helper `bool`, Phase 0a inline, and `state_db_restore.py:117` `List[str]` with unlimited+FK — and decide a canonical signature without weakening the weekly verify path; (b) extracting compression into a separate `_compression.py` with runtime-environment-aware dispatch (subprocess on VPS, Python module on dev-Mac), tested under both regimes; (c) auditing whether the `_ro_uri` read-only-open helper (currently duplicated across consumers) can be shared. M effort, requires VPS-runtime-environment test infrastructure. Not agent-eligible (needs human-in-loop because it touches Phase 0a's load-bearing production-safety constants).

## Verification chain

- `git status` clean on branch entry → baseline pinned at 2d10053
- `python3 -m pytest tests/integration/test_state_db_s3_backup.py tests/integration/test_state_db_snapshot.py -x -q` → 61 passed + 2 skipped (zstd-conditional) BEFORE ANY EDIT
- This KB doc is the only file added/changed
- Atomic commit message ends with `[86b9vgga7]` per ticket convention
- DO NOT push (reviewer agent runs next)
