# Sprint B Bit B.2b — order_decision_snapshots SHIPPED 2026-05-12

**ClickUp**: `86b9vfzr2` — Order-decision orderbook-trajectory capture.

**Status**: SHIPPED (commit `<HASH>` — to be filled by parent agent at merge).

## What

New `order_decision_snapshots` table + 3 StateManager helpers + 13 emit
call sites in `bot/executor.py` + daily-retention hook in
`MainLoop._log_daily_summary`. ONE row per maker-vs-taker route
DECISION (NOT per event), with the full top-of-book ladder + spot
context + an opportunistic 30s post-decision tick stream as a
JSON-blob column. Sister table to `order_lifecycle_snapshots`:

- `order_lifecycle_snapshots` = per-EVENT (submit/fill/cancel) — already shipped Phase 4.
- `order_decision_snapshots`  = per-DECISION (route choice itself) — Bit B.2b.

The two join on `(ticker, time-proximity)` for forensic replay, or via
`decision_id ↔ order_id` once a fill closes the loop.

## Why

Future execution-policy learner needs the microstructure data at the
decision moment to learn "maker vs taker at this state — which was
right?" Without it, post-hoc audit can only see the outcome, not the
state that drove the route choice.

## RCA — enumeration of decision points instrumented

`bot/executor.py::execute()` is the canonical "decide what to do" gate.
Routes:

| Decision path | decision_type | Where (executor.py) |
|---|---|---|
| `_execute_hourly_taker` | taker_first | ~L291 |
| `_execute_weather_no_taker` | taker_first | ~L349 |
| `_execute_hourly_no_taker` | taker_first | ~L389 |
| `_execute_dc_taker` (DC strategies + hourly_dc) | taker_first | ~L2391 |
| `_execute_tm_taker` | taker_first | ~L2541 |
| `_execute_lpne_taker` | taker_first | ~L2724 |
| `_execute_bracket_no_taker` | taker_first | ~L2814 |
| SOL taker-first override (in `execute()`) | taker_first | ~L903 |
| Direct-taker `<180s` (in `execute()`) | taker_first | ~L1076 |
| Maker tier-1 (in `execute()`) | maker_first | ~L1250 |
| Maker tier-2 degraded (in `execute()`) | maker_first | ~L1235 |
| Post-only-taker tier-3 escalation (in `execute()`) | escalate | ~L1162 |
| `_escalate_to_taker_inner` (maker→taker on TTL) | escalate | ~L1989 |

ALL 13 emit a row via the new `OrderExecutor._emit_decision_snapshot(candidate, decision_type)`
helper. Locked by the AST guard in
`tests/integration/test_sprint_b_bit_2b_decision_snapshots.py::TestExecutorAllDecisionPointsInstrumented`.

### decision_id threading

`execute()` seeds `candidate["decision_id"] = uuid.uuid4().hex` at the
TOP of the function. Each downstream branch's `_emit_decision_snapshot`
call reuses this id. The seeded id rides on the candidate dict through
`_submit_maker(candidate)` → `_active_orders[asset] = {..., "candidate": candidate}`
→ later `_escalate_to_taker_inner` does `candidate = dict(order["candidate"])`
which carries `decision_id` forward. The escalation emit uses the SAME
decision_id, so a learner joining `WHERE decision_id=X ORDER BY id`
reconstructs the full "maker_first@t0 → escalate@t15s" route sequence.

Belt-and-braces: helper auto-seeds if missing; `_escalate_to_taker_inner`
also re-seeds if missing (defensive against future code paths that
forge an order dict outside `execute()`).

## 30s tick-stream approach — option (c) JSON blob

Picked option (c) — JSON blob field `followup_ticks_json` on the
snapshot row — over (a) join to position_price_observations or (b)
separate `order_decision_followup_ticks` table.

Reasoning:
- Most decisions are taker IOC paths that resolve immediately (no
  follow-up needed). For maker-first → escalation paths, ~6 ticks at
  5s cadence is the practical maximum; storing as a JSON list on the
  single decision row keeps queries to "WHERE decision_id=X" without
  a join.
- Option (a) doesn't cover unfilled non-positions (most maker-first
  decisions never fill the parent IOC).
- Option (b) introduces a second table for the rare cohort; not worth
  the schema cost for ~6-row max blobs.
- Volume bound: 500-1000 rows/day × ~3KB worst case = ~270 MB / 90d.
  Well within SQLite single-file comfort.

Implementation: `StateManager.append_decision_followup_tick(decision_id,
t_offset_s, best_yes_ask, best_yes_bid, ask_depth, bid_depth)`.
Defensive guards:
- Silently drops ticks beyond `DECISION_FOLLOWUP_WINDOW_S = 30.0`.
- Silently drops ticks beyond `DECISION_FOLLOWUP_MAX_TICKS = 8`.
- Single-writer (bot main_loop is serial) — the read-modify-write
  pattern (SELECT → json.loads → append → UPDATE) is safe under that
  constraint; concurrent multi-thread writers could lose appends but
  no such writers exist in production today.
- No-ops if `decision_id` isn't found (e.g., snapshot insert failed).

Wiring of the actual 30s tick capture into the executor tick loop is
DEFERRED to a follow-up bit — this Bit ships the schema + helper +
decision-point emits, which is the load-bearing piece. The followup
helper is regression-tested but not yet called from production code.

**Follow-up ticket** (to file): "Wire append_decision_followup_tick
into OrderExecutor.tick() for the maker-first → escalate cohort." —
add a per-decision_id tracker that runs for 30s post-decision and
appends one tick per main_loop iteration.

## Schema migration diff

```sql
CREATE TABLE IF NOT EXISTS order_decision_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    asset TEXT NOT NULL,
    decision_time TEXT NOT NULL,
    decision_type TEXT NOT NULL
        CHECK (decision_type IN ('maker_first','taker_first','escalate','shadow')),
    orderbook_levels_json TEXT,
    spot_price REAL,
    seconds_to_close REAL,
    vol_regime TEXT,
    source TEXT,
    followup_ticks_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_ods_decision_id
    ON order_decision_snapshots(decision_id);
CREATE INDEX IF NOT EXISTS idx_ods_ticker_time
    ON order_decision_snapshots(ticker, decision_time);
```

Lives inside `StateManager._create_tables()` so it shares the parent
connection (WAL + busy_timeout=30000). Migration is idempotent (CREATE
TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS); regression test
`TestDecisionSnapshotsMigrationIdempotent` re-instantiates StateManager
twice on the same DB and verifies no error + existing rows survive.

## Retention implementation site

`StateManager.prune_old_decision_snapshots(days=90)` deletes rows where
`decision_time < now - 90d`. Mirrors `scripts/audit_cron.prune_old()`
pattern (single DELETE, parameterized cutoff, returns rowcount).

Invoked daily from `MainLoop._log_daily_summary()` — the same housekeeping
hook used by the daily Telegram digest. Defensive: wrapped in its own
try/except so a prune failure can't poison the daily-summary path.

## Test results

- RED before implementation: 18/19 tests failed (1 incidentally passed
  — re-init idempotency test trivially holds when the table doesn't
  exist yet).
- GREEN after implementation: 22/22 tests pass (added 3 additional
  runtime tests in adversarial Round 1: `_emit_decision_snapshot`
  seeds id, reuses id, swallows downstream failures).

Adversarial review tally:
- Round 1: 1 MAJOR (missing runtime tests for `_emit_decision_snapshot`)
  + 1 MINOR (decision_id mutates caller candidate dict — same pattern
  as existing `entry_path` mutation, accepted). Fix: added 3 runtime
  tests.
- Round 2: 0 CRITICAL / 0 MAJOR. Reviewed concurrency on
  `append_decision_followup_tick` (single-writer OK), retention
  rowcount reliability (single execute, OK), schema doc drift
  (db_schema.md updated in same commit). Identified 1 follow-up
  (wire `append_decision_followup_tick` into tick loop) — NOT in
  scope for this Bit per ticket "Pick during RCA; document choice".
- Round 3: 0 CRITICAL / 0 MAJOR.
- Round 4: 0 CRITICAL / 0 MAJOR.

2 consecutive zero-CRITICAL/MAJOR rounds cleared at R3+R4 (ship gate).

Broader test suite: 4,912 passed; 10 pre-existing failures
(calmlp_lockstep — Sprint A.1a in-flight; repo_hygiene iCloud —
environment-specific; cancel_404 test-isolation pollution). None
caused by this Bit.

## Files touched

- `bot/state.py` — new CREATE TABLE + 3 helper methods
  (`insert_decision_snapshot`, `append_decision_followup_tick`,
  `prune_old_decision_snapshots`).
- `bot/executor.py` — new `_emit_decision_snapshot` instance method +
  13 emit call sites + `decision_id` seeding at top of `execute()` +
  defensive re-seed in `_escalate_to_taker_inner`.
- `bot/main_loop.py` — `_log_daily_summary` invokes
  `prune_old_decision_snapshots(days=90)` (wrapped in try/except).
- `tests/integration/test_sprint_b_bit_2b_decision_snapshots.py` — 22
  new tests across 7 clusters (schema, helper insert, followup
  append, AST decision-point coverage, decision_id threading,
  retention, migration idempotency, runtime smoke).
- `tests/integration/test_executor_extraction.py` — add
  `_emit_decision_snapshot` to `EXECUTOR_INSTANCE_METHODS` tuple.
- `tests/integration/test_state_extraction.py` — add 3 new methods to
  `STATE_METHODS` tuple.
- `agent_docs/db_schema.md` — new table documented.
- `kb/decisions/sprint-b-bit-2b-shipped-may12.md` — this doc.

## Anti-patterns avoided (per ticket + CLAUDE.md)

- Did NOT bundle with B.2a (separate ticket, ships independently).
- Did NOT subsume scan_journal (regime classifier territory, out of
  scope).
- Did NOT write snapshots for non-decision events (periodic OB polls).
  ONE row per `_route_decision` (and per escalation).
- Did NOT add async.
- Did NOT switch from SQLite.
- Did NOT carve new `bot/<subpackage>/` layers.

## Follow-ups (to file as separate tickets)

1. **Wire `append_decision_followup_tick` into `OrderExecutor.tick()`
   for the maker-first → escalate cohort.** This Bit ships the
   helper + schema but the tick-loop caller is deferred — current
   `followup_ticks_json` will be NULL until that wiring lands.
2. **Dashboard panel for `order_decision_snapshots` rollup** — once
   data accumulates, surface decision_type × asset volume in the
   ops dashboard.
3. **Strengthen post_only_taker tier-3 emit** — currently labeled
   `escalate` even though the upstream maker tier-1 emit might be
   from the previous `execute()` call (separate decision sequence
   in the post-only-rejection retry loop). Verify a learner can
   correctly reconstruct the sequence; may need a separate
   `decision_type='post_only_taker_escalate'` literal.
