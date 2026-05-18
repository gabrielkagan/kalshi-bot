"""B3-fu1 regression — cross-thread commit race on shared state.conn.

Incident (2026-05-18): live VPS logs show recurring ~5-10min cadence of
`cannot commit - no transaction is active` at two surfaces:

1. `bot/state.py::StateManager.insert_evaluated_opportunity`
   — `self.conn.execute("COMMIT")` after a successful explicit
   `BEGIN IMMEDIATE` and `INSERT`.
2. `bot/state.py::StateManager.mark_rejection_settled`
   — `self.conn.commit()` after a successful `UPDATE`.

Root cause: `bot/state.py:147` opens a single `sqlite3.Connection`
with `check_same_thread=False` and default `isolation_level=""`
(deferred). Python's sqlite3 module serializes individual C-level
calls via an internal mutex, but **not** multi-statement Python
sequences. The settlement_tracker daemon thread (spawned at
`bot/settlement.py:212`) shares `state.conn` with MainThread; a
racing `conn.commit()` from settlement_tracker between MainThread's
BEGIN IMMEDIATE/INSERT and COMMIT commits MainThread's still-open
transaction. The subsequent COMMIT on MainThread finds SQLite no
longer in tx and raises `OperationalError: cannot commit - no
transaction is active`. The symmetric case applies for
`mark_rejection_settled` on settlement_tracker thread when MainThread
commits first.

Data integrity note: the racer's commit captures our pending
INSERT/UPDATE — data IS persisted before our COMMIT fires. The
error is therefore spurious; defensive swallow is safe.

Fix (B3-fu1, 2026-05-18): guard each affected commit with a narrow
`except sqlite3.OperationalError` that swallows ONLY the
"no transaction is active" substring. All other OperationalErrors
(disk-full, corruption, etc.) propagate unchanged.

Tests assert via `caplog` that:

- RED before fix: WARNING log "insert_evaluated_opportunity failed:
  cannot commit - no transaction is active" / "mark_rejection_settled
  failed: cannot commit - no transaction is active" fires.
- GREEN after fix: NO such WARNING fires for the race case.

Other OperationalErrors (disk-full) still log WARNING (state.py
insert) or propagate (state.py mark_rejection_settled — currently
unguarded → propagates).

Tests use a real sqlite3 file via `tmp_path` (per
`tests/CLAUDE.md` integration-tier convention).
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from bot.state import StateManager


# ─── Wrapper conn for error injection ────────────────────────────────────


class _ConnWrapper:
    """Wraps a real sqlite3.Connection so tests can inject errors at
    `execute()` / `commit()` call boundaries.

    sqlite3.Connection has C-level read-only attributes, so
    `mock.patch.object(conn, "execute", ...)` raises AttributeError.
    This wrapper delegates by default and lets a per-test hook
    intercept specific statements.
    """

    def __init__(self, inner: sqlite3.Connection):
        self.__dict__["_inner"] = inner
        self.__dict__["_execute_hook"] = None
        self.__dict__["_commit_hook"] = None

    def execute(self, sql, *args, **kwargs):
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
        if name in ("_inner", "_execute_hook", "_commit_hook"):
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


def _seed_rejection_row(state: StateManager, ticker: str) -> None:
    """Insert a minimal `rejected_opportunities` row so
    mark_rejection_settled has a target to UPDATE."""
    state.conn.execute(
        "INSERT INTO rejected_opportunities "
        "(ticker, event_ticker, asset, rejection_reason, rejection_time) "
        "VALUES (?,?,?,?,?)",
        (ticker, "EVT", "BTC", "below_threshold", "2026-05-18T00:00:00Z"),
    )
    state.conn.commit()


# ─── 1. insert_evaluated_opportunity — race-tolerant COMMIT ──────────────


def test_insert_evaluated_opportunity_swallows_no_transaction_active(
        tmp_path: Path, caplog):
    """Pre-fix: the outer try/except catches the COMMIT OperationalError
    and logs WARNING "insert_evaluated_opportunity failed: cannot commit
    - no transaction is active". Post-fix: the swallow path returns
    cleanly with NO such WARNING — AND the INSERT row persists
    (captured by the racer's pre-COMMIT).
    """
    state = _make_state(tmp_path)

    def _execute_hook(sql, *args, **kwargs):
        if sql.strip().upper() == "COMMIT":
            # Simulate: racer's commit landed between INSERT and COMMIT.
            # The racer's commit committed our INSERT, so commit inner
            # to reflect that state, then raise the error our COMMIT
            # attempt would now produce.
            state.conn.__dict__["_inner"].commit()
            raise sqlite3.OperationalError(
                "cannot commit - no transaction is active")
        return None

    state.conn.__dict__["_execute_hook"] = _execute_hook

    with caplog.at_level(logging.WARNING, logger="root"):
        state.insert_evaluated_opportunity(
            ticker="KXTEST-1",
            event_ticker="EVT",
            asset="BTC",
            filter_stage="candidate",
            product_type="15m",
        )

    failure_warnings = [
        r for r in caplog.records
        if "insert_evaluated_opportunity failed" in r.getMessage()
        and "no transaction is active" in r.getMessage()
    ]
    assert not failure_warnings, (
        f"Post-fix: the no-tx-active COMMIT race must NOT log WARNING. "
        f"Got: {[r.getMessage()[:200] for r in failure_warnings]}")

    # Persistence pin: the racer's commit captured our INSERT before
    # our COMMIT fired, so the row MUST be visible in the DB. Disable
    # hook for the read-back. This locks the docstring's data-integrity
    # claim into the test contract.
    state.conn.__dict__["_execute_hook"] = None
    rows = state.conn.execute(
        "SELECT ticker FROM evaluated_opportunities "
        "WHERE ticker=?", ("KXTEST-1",)).fetchall()
    assert len(rows) == 1, (
        "Race-swallow must preserve the INSERT — racer's commit "
        f"captured it before our COMMIT fired. Got {len(rows)} rows.")


def test_insert_evaluated_opportunity_still_warns_on_other_operational_errors(
        tmp_path: Path, caplog):
    """Other OperationalErrors (disk-full etc.) still propagate to the
    outer try/except → WARNING log fires (so operators see real
    failures)."""
    state = _make_state(tmp_path)

    def _execute_hook(sql, *args, **kwargs):
        if sql.strip().upper() == "COMMIT":
            raise sqlite3.OperationalError("disk I/O error")
        return None

    state.conn.__dict__["_execute_hook"] = _execute_hook

    with caplog.at_level(logging.WARNING, logger="root"):
        state.insert_evaluated_opportunity(
            ticker="KXTEST-2",
            event_ticker="EVT",
            asset="BTC",
            filter_stage="candidate",
            product_type="15m",
        )

    failure_warnings = [
        r for r in caplog.records
        if "insert_evaluated_opportunity failed" in r.getMessage()
        and "disk I/O" in r.getMessage()
    ]
    assert failure_warnings, (
        "Disk-full OperationalError must still log WARNING (must NOT "
        "be swallowed by the no-tx race guard)")


# ─── 2. mark_rejection_settled — race-tolerant commit ────────────────────


def test_mark_rejection_settled_swallows_no_transaction_active(
        tmp_path: Path):
    """Pre-fix: `self.conn.commit()` raises OperationalError "no
    transaction is active" — verified via live VPS traceback at
    `journalctl -u kalshi-bot 2026-05-18 12:03:56 UTC`:

        File "bot/state.py", line 1936, in mark_rejection_settled
            self.conn.commit()
        sqlite3.OperationalError: cannot commit - no transaction is active

    The race window is narrow (between Python's autocommit check
    inside conn.commit() and SQLite's actual COMMIT step) so
    synthetic single-threaded repros do NOT trigger it. Post-fix:
    swallow returns cleanly AND the UPDATE persists (racer's commit
    captured it).
    """
    state = _make_state(tmp_path)
    _seed_rejection_row(state, "KXREJ-1")

    def _commit_hook():
        # Racer-simulated: their commit captured our UPDATE.
        state.conn.__dict__["_inner"].commit()
        raise sqlite3.OperationalError(
            "cannot commit - no transaction is active")

    state.conn.__dict__["_commit_hook"] = _commit_hook

    # Post-fix: returns silently. Pre-fix: OperationalError propagates.
    state.mark_rejection_settled(
        "KXREJ-1",
        market_result="yes",
        counterfactual='{"k":"v"}',
    )

    # Persistence pin: the racer's commit captured our UPDATE.
    state.conn.__dict__["_commit_hook"] = None
    row = state.conn.execute(
        "SELECT status, market_result, counterfactual FROM rejected_opportunities "
        "WHERE ticker=?", ("KXREJ-1",)).fetchone()
    assert row is not None, "UPDATE row must exist post-swallow"
    assert row[0] == "settled", (
        f"UPDATE must have set status='settled'; got {row[0]!r}")
    assert row[1] == "yes"
    assert row[2] == '{"k":"v"}'


def test_mark_rejection_settled_reraises_other_operational_errors(
        tmp_path: Path):
    """Only swallow the no-tx-active text; other errors propagate."""
    state = _make_state(tmp_path)
    _seed_rejection_row(state, "KXREJ-2")

    def _commit_hook():
        raise sqlite3.OperationalError("disk I/O error")

    state.conn.__dict__["_commit_hook"] = _commit_hook

    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        state.mark_rejection_settled(
            "KXREJ-2",
            market_result="yes",
            counterfactual=None,
        )


# NOTE: A real-DB cross-thread integration test was attempted but
# segfaults on macOS Python 3.9 even before the fix (sqlite3 internal
# mutex fragility under concurrent BEGIN/INSERT/COMMIT + commit() on
# `check_same_thread=False` conn). Production (Linux Python 3.12) is
# the canonical environment; the mock-based tests above pin the
# defensive-swallow contract independent of platform fragility.
