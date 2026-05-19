"""86ba0jb1g — insert_bot_order defensive-guard regression.

Incident (2026-05-19): live VPS log at 07:42:37 UTC traced a
`sqlite3.OperationalError: database is locked` to
`bot/state.py:3304:insert_bot_order` — the LIVE-order ledger path,
not just telemetry. Tonight's MarketObsSnapshotter writer-thread
lock storm leaked from the previously-protected
`insert_evaluated_opportunity` / `insert_rejection` sites into the
unprotected order-ledger site.

Root cause: `insert_bot_order` (bot/state.py:3299-3312) was the
last hot-path writer with ZERO contention protection — bare
`self.conn.execute(INSERT)` + `self.conn.commit()`, no retry-on-busy
loop, no commit-race swallow, no diagnostic logging. Peer sites
have had at least one of those since May 9 (retry-on-busy in
insert_evaluated_opportunity / insert_rejection) and May 18 (B3-fu1
commit-race swallow in insert_evaluated_opportunity /
mark_rejection_settled).

Fix scope (this Bit):

1. **Retry-on-busy** — 3-attempt BEGIN IMMEDIATE retry loop on
   transient "database is locked" / "busy" OperationalErrors.
   Mirrors `bot/state.py:2563-2594` (insert_evaluated_opportunity).

2. **Raise-on-exhaustion (CRASH SAFETY)** — if all 3 retries fail,
   RE-RAISE the OperationalError. **Different from
   insert_evaluated_opportunity, which SWALLOWS.** The
   crash-safety contract pinned by
   `tests/integration/test_execution.py::test_maker_persists_to_db_before_api`
   requires that if the DB persist fails, place_order MUST NOT
   fire. Swallowing here would let the executor proceed and
   submit an order with no local record — strictly worse than
   the tick failing.

3. **B3-fu1 commit-race swallow** — `settlement_tracker`
   (`bot/settlement.py:201`) writes to `pending_orders` via
   `cleanup_expired_resting_orders` on the shared `state.conn`.
   That can fire the B3-fu1 race: settlement_tracker commits
   between MainThread's BEGIN IMMEDIATE/INSERT and COMMIT.
   Swallow ONLY the "no transaction is active" OperationalError
   (data already captured by racer's commit); propagate everything
   else.

Tests use a real sqlite3 file via `tmp_path` (per
`tests/CLAUDE.md` integration-tier convention) plus the
`_ConnWrapper` injection pattern adapted from
`test_cannot_commit_no_transaction_regression.py`.

Pre-fix RED expectations (with current bot/state.py):
- Test 1 (retry-on-busy): first execute(INSERT) raises → propagates →
  row does NOT exist → assertion fails.
- Test 2 (raise-on-exhaustion + retry-count pin): function raises,
  but `execute_call_count == 1` — pre-fix retry contract not met.
- Test 3 (commit-race swallow): commit() raises → propagates → test
  expects no-raise → fails.
- Test 4 (disk-full propagate): passes pre-fix AND post-fix
  (contract pin against future regressions).
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from bot.state import StateManager


# ─── Wrapper conn for error injection (parallel to sister regression) ────


class _ConnWrapper:
    """Wraps a real sqlite3.Connection so tests can inject errors at
    `execute()` / `commit()` call boundaries.

    sqlite3.Connection has C-level read-only attributes, so
    `mock.patch.object(conn, "execute", ...)` raises AttributeError.
    This wrapper delegates by default and lets a per-test hook
    intercept specific statements. Adapted from
    test_cannot_commit_no_transaction_regression.py::_ConnWrapper.
    """

    def __init__(self, inner: sqlite3.Connection):
        self.__dict__["_inner"] = inner
        self.__dict__["_execute_hook"] = None
        self.__dict__["_commit_hook"] = None
        self.__dict__["execute_call_count"] = 0

    def execute(self, sql, *args, **kwargs):
        self.__dict__["execute_call_count"] += 1
        hook = self.__dict__["_execute_hook"]
        if hook is not None:
            result = hook(sql, *args, **kwargs)
            if result is not None:
                return result
        return self.__dict__["_inner"].execute(sql, *args, **kwargs)

    def commit(self):
        hook = self.__dict__["_commit_hook"]
        if hook is not None:
            hook()
            return
        return self.__dict__["_inner"].commit()

    def __getattr__(self, name):
        return getattr(self.__dict__["_inner"], name)

    def __setattr__(self, name, value):
        if name in ("_inner", "_execute_hook", "_commit_hook",
                    "execute_call_count"):
            self.__dict__[name] = value
        else:
            setattr(self.__dict__["_inner"], name, value)


def _make_state(tmp_path: Path) -> StateManager:
    """Construct StateManager on a tmp sqlite3 file, then swap its
    `.conn` for a _ConnWrapper so tests can inject errors."""
    db = tmp_path / "state.db"
    state = StateManager(str(db))
    state.conn = _ConnWrapper(state.conn)
    return state


_ORDER_ARGS = dict(
    client_order_id="cli-test-1",
    ticker="KXBTC15M-26MAY190400-00",
    event_ticker="KXBTC15M-26MAY190400",
    asset="BTC",
    side="yes",
    count=10,
    price_cents=99,
    is_taker=True,
)


def _read_back_one(state: StateManager, client_order_id: str):
    """Read pending_orders by client_order_id with hooks disabled,
    so reads aren't affected by the injection."""
    state.conn.__dict__["_execute_hook"] = None
    state.conn.__dict__["_commit_hook"] = None
    rows = state.conn.execute(
        "SELECT client_order_id, ticker, count, price_cents, status "
        "FROM pending_orders WHERE client_order_id=?",
        (client_order_id,)
    ).fetchall()
    return rows


# ─── 1. RETRY-ON-BUSY ────────────────────────────────────────────────────


def test_insert_bot_order_retries_on_database_locked(tmp_path: Path):
    """Post-fix: insert_bot_order MUST retry the BEGIN IMMEDIATE up
    to 3 times when SQLite raises OperationalError("database is
    locked") on transient intra-process contention. The third
    attempt succeeds and the row lands.

    Pre-fix RED: the bare execute(INSERT) at state.py:3304 has no
    retry loop. The first injected OperationalError propagates →
    pending_orders row never inserted → read-back returns empty.

    Hook design: fail the first two execute() calls with
    "database is locked" regardless of statement. Pre-fix: call 1 =
    INSERT (raises, no retry) → fail. Post-fix: call 1 + 2 =
    BEGIN IMMEDIATE (raise + retry; raise + retry) → call 3 =
    BEGIN IMMEDIATE succeeds → INSERT + COMMIT land → row exists.
    """
    state = _make_state(tmp_path)

    fail_remaining = [2]  # mutable counter

    def _execute_hook(sql, *args, **kwargs):
        if fail_remaining[0] > 0:
            fail_remaining[0] -= 1
            raise sqlite3.OperationalError("database is locked")
        return None

    state.conn.__dict__["_execute_hook"] = _execute_hook

    state.insert_bot_order(**_ORDER_ARGS)

    rows = _read_back_one(state, _ORDER_ARGS["client_order_id"])
    assert len(rows) == 1, (
        "Post-fix: retry-on-busy must land the INSERT after 2 "
        f"transient locks. Got {len(rows)} rows.")
    assert rows[0][1] == _ORDER_ARGS["ticker"]
    assert rows[0][2] == _ORDER_ARGS["count"]
    assert rows[0][3] == _ORDER_ARGS["price_cents"]


# ─── 2. RAISE-ON-EXHAUSTION (CRASH SAFETY CONTRACT) ──────────────────────


def test_insert_bot_order_reraises_after_retry_exhaustion(
        tmp_path: Path, caplog):
    """Post-fix: after all 3 BEGIN IMMEDIATE retries fail, the
    OperationalError MUST be RE-RAISED — not swallowed.

    This is the load-bearing crash-safety contract pinned by
    tests/integration/test_execution.py::test_maker_persists_to_db_before_api
    ("DB persist must happen before API call"). If insert_bot_order
    silently swallowed the OperationalError after retry exhaustion,
    the executor's `self._state.insert_bot_order(...)` call would
    return cleanly, the executor would proceed to
    `self._client.place_order(...)`, and Kalshi would receive an
    order with NO local record. The settled_trades reconciliation
    would then fail to find the originating bot_order, producing
    a ghost-fill-class anomaly.

    CONTRAST: insert_evaluated_opportunity (bot/state.py:2937)
    SWALLOWS its OperationalError because losing a telemetry row
    is preferable to crashing the scan tick. insert_bot_order is
    the OPPOSITE — crashing the tick is preferable to losing an
    order record.

    Pre-fix RED on the retry-count pin: the bare execute(INSERT)
    raises on call 1, so execute_call_count == 1. Post-fix:
    BEGIN IMMEDIATE retried 3x → execute_call_count == 3.

    Also pins the diagnostic-WARNING envelope (R1-M2 fix,
    2026-05-19): the re-raise path MUST emit a structured
    `insert_bot_order failed` WARNING including the
    begin_immediate_retries count and recent_writes() ring
    buffer — mirrors sister envelope at state.py:2974-2984 so
    operators can correlate order-ledger failures to the
    broader contention storm class. A future regression that
    drops this logging would fail the caplog assertion.
    """
    state = _make_state(tmp_path)

    def _execute_hook(sql, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    state.conn.__dict__["_execute_hook"] = _execute_hook

    with caplog.at_level(logging.WARNING, logger="root"):
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            state.insert_bot_order(**_ORDER_ARGS)

    # Retry-count pin: distinguishes pre/post-fix behavior.
    # Pre-fix: 1 call. Post-fix: 3 calls (the retry loop).
    assert state.conn.execute_call_count >= 3, (
        "Post-fix: retry-on-busy must attempt BEGIN IMMEDIATE 3 "
        f"times before re-raising. Got "
        f"{state.conn.execute_call_count} execute() call(s).")

    # Diagnostic-envelope pin (R1-M2): structured WARNING fires
    # on the re-raise path with retry-count + recent_writes.
    failure_warnings = [
        r for r in caplog.records
        if "insert_bot_order failed" in r.getMessage()
        and "begin_immediate_retries" in r.getMessage()
        and "recent_writes" in r.getMessage()
    ]
    assert failure_warnings, (
        "Post-fix R1-M2: retry-exhaustion re-raise must emit "
        "structured WARNING with begin_immediate_retries + "
        "recent_writes envelope. Got: "
        f"{[r.getMessage()[:200] for r in caplog.records]}")


# ─── 3. B3-fu1 COMMIT-RACE TOLERANCE ─────────────────────────────────────


def test_insert_bot_order_swallows_no_transaction_active_commit_race(
        tmp_path: Path):
    """Post-fix: if the COMMIT step raises OperationalError
    "cannot commit - no transaction is active" — the B3-fu1
    cross-thread commit-race signature — insert_bot_order MUST
    swallow and return normally. The racer's commit (here,
    settlement_tracker's commit inside cleanup_expired_resting_orders)
    already captured our INSERT before our COMMIT fired, so data
    is preserved.

    Pre-fix RED: bare commit() has no try/except → OperationalError
    propagates → test expects clean return → fails.

    Hooks both `conn.commit()` AND `execute("COMMIT")` as
    defense-in-depth: the current implementation uses
    `execute("COMMIT")` exclusively, so `_commit_hook` is dead
    in this test. We install both so a future refactor that
    switches the impl to `self.conn.commit()` (parallel to
    B3-fu1's `if _began_explicitly: execute("COMMIT") else
    commit()` shape) still triggers this test without silent
    contract erosion.
    """
    state = _make_state(tmp_path)

    def _execute_hook(sql, *args, **kwargs):
        if sql.strip().upper() == "COMMIT":
            # Racer-simulated: their commit captured our INSERT.
            state.conn.__dict__["_inner"].commit()
            raise sqlite3.OperationalError(
                "cannot commit - no transaction is active")
        return None

    def _commit_hook():
        # Dead path under current impl (defense-in-depth — see docstring).
        state.conn.__dict__["_inner"].commit()
        raise sqlite3.OperationalError(
            "cannot commit - no transaction is active")

    state.conn.__dict__["_execute_hook"] = _execute_hook
    state.conn.__dict__["_commit_hook"] = _commit_hook

    # Post-fix: returns silently. Pre-fix: OperationalError propagates.
    state.insert_bot_order(**_ORDER_ARGS)

    rows = _read_back_one(state, _ORDER_ARGS["client_order_id"])
    assert len(rows) == 1, (
        "Commit-race swallow must preserve the INSERT — racer's "
        f"commit captured it before our COMMIT fired. Got "
        f"{len(rows)} rows.")
    assert rows[0][1] == _ORDER_ARGS["ticker"]


# ─── 4. NON-RACE OperationalErrors STILL PROPAGATE ───────────────────────


def test_insert_bot_order_reraises_other_commit_operational_errors(
        tmp_path: Path):
    """The B3-fu1 swallow MUST be narrow — only "no transaction is
    active". Other OperationalErrors at commit time (disk I/O,
    corruption, etc.) MUST propagate so the executor crash-safety
    contract holds for non-race failures too.

    Both pre-fix and post-fix should pass this — pin against
    future regressions that overbroaden the swallow."""
    state = _make_state(tmp_path)

    def _execute_hook(sql, *args, **kwargs):
        if sql.strip().upper() == "COMMIT":
            raise sqlite3.OperationalError("disk I/O error")
        return None

    def _commit_hook():
        raise sqlite3.OperationalError("disk I/O error")

    state.conn.__dict__["_execute_hook"] = _execute_hook
    state.conn.__dict__["_commit_hook"] = _commit_hook

    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        state.insert_bot_order(**_ORDER_ARGS)
