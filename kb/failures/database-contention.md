---
status: resolved
updated: 2026-03-16
tags: [failure, sqlite, contention, wal]
severity: major
---
# SQLite Database Contention Saga

## Summary
Five separate database contention incidents across March 2-16, 2026. Multiple threads sharing state.db without proper concurrency controls caused cascading "database is locked" errors, CPU spikes, and a deadlock triangle. Produced four permanent rules for all future SQLite usage.

## Incident 1: sports_engine.py Missing busy_timeout (Mar 2)
**Symptom:** ~2000 "database is locked" errors in 8 hours from sports_engine.py.
**Root cause:** `sqlite3.connect()` call had no `PRAGMA busy_timeout`. Sports engine thread competed with bot main thread and supabase_sync for write access. Default timeout is 0ms -- instant failure on any contention.
**Fix:** Added `PRAGMA busy_timeout=10000` (10s) to sports_engine.py connection.

## Incident 2: analyst.py Missing busy_timeout (Mar 9)
**Symptom:** Intermittent locked errors from analyst thread, contributing to overall contention.
**Root cause:** Same as Incident 1 -- analyst.py (now `bot/ai/analyst.py` post-Sprint-10.3) opened its own connection without busy_timeout.
**Fix:** Added `PRAGMA busy_timeout=10000` to analyst.py (now `bot/ai/analyst.py` post-Sprint-10.3). Established rule: every new `sqlite3.connect()` must include WAL + busy_timeout.

## Incident 3: Per-Row Commits in Loop (Mar 9, PM-001)
**Symptom:** "database is locked" burst + CPU spike during `_poll_evaluated_opportunities()`.
**Root cause:** 91 individual `conn.commit()` calls per cycle, one per row. Each commit acquires and releases the write lock, multiplying the contention window. supabase_sync runs 165 queries every 10s -- the collision rate was enormous.
**Fix:** Accumulated all writes, single `conn.commit()` at end of batch. Rule: never commit inside a loop -- always batch.

**Carveout (2026-05-21, ticket 86ba1xdwp — settlement weather writer-storm).**
The "never commit inside a loop" rule applies when the loop body is FAST
(CPU-only / local DB work). When the loop body contains slow synchronous
I/O — specifically the Phase 3 weather sub-block of
`SettlementTracker._poll_evaluated_opportunities`, which interleaves
Open-Meteo `fetch_observed_high()` HTTP calls (1-5s each) with
`UPDATE evaluated_opportunities SET wx_actual_high_temp=...` writes — a
single end-of-loop commit holds the writer lock continuously across all
HTTP latency and busy-times-out every separate-conn writer in the bot
(`weather_engine._save_bias`, `market_obs_snapshotter`,
`phantom_reconcile_monitor`, `CALMLP_POSTHOC`). The Bit 86ba1xdwp fix
splits the weather phase into Phase 3a (HTTP-only, collect) and
Phase 3b (DB-only, per-row UPDATE + commit + bias update). The per-row
commit is REQUIRED in that sub-block to release the lock between rows
and is the OPPOSITE of Incident 3's batch-commit rule. The
Phase 2 fast-DB-writes block in the same method still follows the
batch rule (≤50-row chunks). Pinned by
`tests/contracts/test_settlement_weather_writer_lock_phase3.py` +
`tests/integration/test_settlement_weather_writer_storm_regression.py`.

## Incident 4: WAL Checkpoint TRUNCATE Deadlock (Mar 16)
**Symptom:** 11,258 "database is locked" errors in 12 hours.
**Root cause:** `PRAGMA wal_checkpoint(TRUNCATE)` requires an exclusive lock that blocks ALL readers and writers. Deadlock triangle: checkpoint waits for supabase_sync reader to finish, reader holds shared lock, settlement writer waits for checkpoint's exclusive lock. supabase_sync runs 192 SELECTs every 30s -- TRUNCATE could never acquire exclusive access cleanly.
**Fix:** Switched to `PRAGMA wal_checkpoint(PASSIVE)` which checkpoints whatever pages it can without blocking. TRUNCATE is now banned.

## Incident 5: Large Settlement Batch (Mar 16)
**Symptom:** Contributed to Incident 4 -- weather expansion produced 228-row settlement batch.
**Root cause:** Large batch held write lock long enough to overlap with concurrent readers and the TRUNCATE checkpoint attempt.
**Fix:** Settlement Phase 2 now chunks into batches of <=50 rows per commit. Rule: keep DB write batches small.

## Incident 6: Bare-DatabaseError stale-cursor escapes narrow OperationalError catch (May 21-22)
**Symptom:** Telegram-alertable Tick errors firing at `state.py:2779:insert_evaluated_opportunity` (2026-05-21 12:59:29 UTC + 14:03:46 UTC) and `state.py:2155:insert_rejection` (multiple 2026-05-22 hits). Past-48h VPS journal histogram: 21× `sqlite3.DatabaseError: another row available` + 2× `sqlite3.DatabaseError: no more rows available` escaping the BEGIN IMMEDIATE retry blocks across the StateManager hot-path writers. Each escape lost a telemetry row on the swallow-class siblings (`insert_evaluated_opportunity` + `insert_rejection`); the crash-safety site (`insert_bot_order`) would have propagated the exception up the call stack, potentially killing a scan tick during order placement.
**Root cause:** Python's sqlite3 module surfaces stale-cursor-class raises at the bare `sqlite3.DatabaseError` parent class — NOT the `OperationalError` subclass. The retry-on-busy except clauses at the three BEGIN IMMEDIATE retry sites caught only `sqlite3.OperationalError`, so bare `DatabaseError` raises bypassed the catch + the transient/non-transient string-match dispatch. Cross-thread tx race (`MainThread` insert collisions with `settlement_tracker` UPDATE/commit cycles on the shared conn) creates the underlying open-tx-or-bad-cursor condition; the catch-class mismatch turns a recoverable race into a tick-killing escape.
**Fix (Bit `bit-state-py-database-error-catch`, 2026-05-22):** broadened the BEGIN IMMEDIATE retry-loop except clauses at `insert_evaluated_opportunity`, `insert_rejection`, and `insert_bot_order` from `sqlite3.OperationalError` to its parent `sqlite3.DatabaseError`. Strict superset — `OperationalError` cases still match. Transient/non-transient string-match dispatch ("locked"/"busy" → retry; "cannot start..."/"no more rows..."/"another row..." → fall-through) is unchanged. The fourth StateManager hot-path site `mark_rejection_settled` has only a commit-race catch (no BEGIN retry); its "no transaction is active" signature has been observed only as `OperationalError` (1 production event in 48h) and stays narrow until the cross-thread tx-race refactor (deferred per B3-fu1 OUT OF SCOPE). Rule: catch sqlite3 exceptions at the BROADEST class compatible with the dispatch logic — string-match on the exception message inside the handler if you need finer routing.
**Pinned by:** `tests/integration/test_db_locked_instrumentation.py::test_begin_immediate_retry_loop_catches_database_error` (parametrized over the three host functions; extend `_BEGIN_RETRY_LOOP_HOSTS` for any future BEGIN-retry sister site).

## Permanent Rules (from these incidents)
1. Every `sqlite3.connect()` MUST include `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=10000`
2. Never `conn.commit()` inside a loop -- accumulate writes, commit once (carveout: see Incident 3 note for the slow-I/O-in-loop class introduced by Bit 86ba1xdwp 2026-05-21)
3. Never use `wal_checkpoint(TRUNCATE)` -- use `PASSIVE` only
4. Keep write batches <=50 rows per commit
5. Cross-thread connections MUST use `check_same_thread=False`

## Why SQLite (Not Postgres)
Single-writer is fine for bot throughput. Latency matters -- local SQLite is sub-millisecond. The contention issues are solved by discipline (WAL, timeouts, batching), not by switching databases. See CLAUDE.md anti-patterns: "Don't suggest switching from SQLite."

## Thread Model
state.db is shared by 5+ concurrent threads/processes:
- bot.py main thread (scan loop, settlement, position tracking)
- supabase_sync.py (192 SELECTs every 30s, plus UPSERTs to Supabase)
- sports_engine.py (shadow evaluations, game state)
- bot/ai/analyst.py (news sentiment, loss analysis)
- fifteenm_shadow.py (shadow signal writes)

All five must have WAL + busy_timeout. Any new sqlite3.connect() call added anywhere in the codebase must follow the same pattern.

## Detection
Grep syslog for "database is locked". Dashboard rate_limits panel shows contention spikes. `bot/ai/auditor.py` checks for locked error rates.

## Current Timeouts
| Component | busy_timeout |
|-----------|-------------|
| bot.py | 10s |
| supabase_sync.py | 5s |
| sports_engine.py | 10s |
| bot/ai/analyst.py | 10s |
| fifteenm_shadow.py | 10s |

## Related
- [[failures/evaluations-sync.md]] (Supabase sync -- another multi-thread data issue)
- [[concepts/weather-system.md]] (weather expansion triggered Incident 5)
