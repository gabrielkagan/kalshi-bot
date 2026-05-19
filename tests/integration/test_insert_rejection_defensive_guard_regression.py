"""86ba0jvgw — insert_rejection defensive-guard regression (sister to 86ba0jb1g).

Incident (2026-05-19): live VPS log surfaced
`Tick error at state.py:2049:insert_rejection: another row available`
during the disk-full cascade. `insert_rejection` was the LAST hot-path
StateManager write site without retry-on-busy + commit-race swallow
protection — the 4-site coverage chain stops short:

  Site                                   Pattern                     Ship
  ─────────────────────────────────────  ──────────────────────────  ────────
  insert_evaluated_opportunity           BEGIN IMMEDIATE + retry +   B3-fu1
                                         SWALLOW                     86b9zxawt
  mark_rejection_settled                 UPDATE + B3-fu1 swallow     B3-fu1
  insert_bot_order                       BEGIN IMMEDIATE + retry +   86ba0jb1g
                                         RAISE (crash safety)
  ▶ insert_rejection                     ← THIS BIT (86ba0jvgw)

Fix scope (this Bit):

1. **Retry-on-busy** — 3-attempt BEGIN IMMEDIATE retry loop on
   transient "database is locked" / "busy" OperationalErrors.
   Mirrors the sister `StateManager.insert_evaluated_opportunity`'s
   BEGIN IMMEDIATE retry loop (symbolic anchor; cross-references use
   function names instead of line numbers to survive future file
   shifts — see R1-M1 ratchet in this Bit's adv-review history).

2. **SWALLOW-on-exhaustion (TELEMETRY divergence)** — if all 3
   retries fail, log a structured WARNING with retry-count +
   recent_writes() envelope and RETURN normally. **Different from
   insert_bot_order which RE-RAISES.** Rejection rows are
   telemetry; losing one is preferable to crashing the scan tick.
   Same divergence justification as insert_evaluated_opportunity.

3. **B3-fu1 commit-race swallow** — `settlement_tracker`
   (`bot/settlement.py`) writes via `mark_rejection_settled` on the
   shared `state.conn`. The cross-thread commit-race signature
   `"cannot commit - no transaction is active"` must be swallowed
   (data already captured by racer's commit); other commit errors
   propagate via the broader telemetry swallow.

4. **Broad telemetry swallow on the implicit-tx INSERT path** — the
   pre-Bit form does bare `self.conn.execute(INSERT)` then
   `self.conn.commit()`. Tonight's `another row available`
   sqlite3.OperationalError fires when the connection has an
   unfinalized cursor mid-iteration. The defensive guard must
   swallow ALL sqlite3.OperationalError from the INSERT/COMMIT
   step with the diagnostic-WARNING envelope (telemetry-class
   divergence: drop the row, keep the tick alive).

Pre-fix RED expectations (with current bot/state.py:1960):
- Test 1 (retry-on-busy): bare execute(INSERT) at line 2049 raises →
  propagates → row does NOT exist → assertion fails.
- Test 2 (swallow-on-exhaustion + diagnostic envelope): function
  raises pre-fix (no swallow), expected to return cleanly post-fix.
- Test 3 (B3-fu1 commit-race swallow): commit() raises → propagates →
  test expects no-raise → fails.
- Test 4 ("another row available" telemetry swallow): tonight's exact
  incident signature; function raises pre-fix → propagates → test
  expects no-raise → fails.
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
    test_insert_bot_order_defensive_guard_regression.py::_ConnWrapper.
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
    db = tmp_path / "state.db"
    state = StateManager(str(db))
    state.conn = _ConnWrapper(state.conn)
    return state


# Pass orderbook_levels_json + hour_sin/cos + sigma_winsorize +
# prob_breakeven_gap explicitly so the auto-fill helpers inside
# insert_rejection don't hit conn.execute during error injection.
_REJ_ARGS = dict(
    ticker="KXBTC15M-26MAY190400-00",
    event_ticker="KXBTC15M-26MAY190400",
    asset="BTC",
    rejection_reason="z_score_below_threshold",
    z_score=0.5,
    spot_price=100000.0,
    threshold=100050.0,
    volatility=0.01,
    market_price=50,
    seconds_to_close=600.0,
    calibrated_prob=0.4,
    raw_prob=0.45,
    product_type="15m",
    orderbook_levels_json="[]",  # explicit — skip auto-fill path
    hour_sin=0.0,
    hour_cos=1.0,
    sigma_winsorize=0.0,
    prob_breakeven_gap=0.0,
)


def _read_back_one(state: StateManager, ticker: str):
    state.conn.__dict__["_execute_hook"] = None
    state.conn.__dict__["_commit_hook"] = None
    rows = state.conn.execute(
        "SELECT ticker, asset, rejection_reason "
        "FROM rejected_opportunities WHERE ticker=?",
        (ticker,)
    ).fetchall()
    return rows


# ─── 1. RETRY-ON-BUSY ────────────────────────────────────────────────────


def test_insert_rejection_retries_on_database_locked(tmp_path: Path):
    """Post-fix: insert_rejection MUST retry the BEGIN IMMEDIATE up
    to 3 times when SQLite raises OperationalError("database is
    locked") on transient intra-process contention. The third
    attempt succeeds and the row lands.

    Pre-fix RED: the bare execute(INSERT) at state.py:2049 has no
    retry loop. The first injected OperationalError propagates →
    rejected_opportunities row never inserted → read-back returns empty.
    """
    state = _make_state(tmp_path)

    fail_remaining = [2]

    def _execute_hook(sql, *args, **kwargs):
        if fail_remaining[0] > 0:
            fail_remaining[0] -= 1
            raise sqlite3.OperationalError("database is locked")
        return None

    state.conn.__dict__["_execute_hook"] = _execute_hook

    state.insert_rejection(**_REJ_ARGS)

    rows = _read_back_one(state, _REJ_ARGS["ticker"])
    assert len(rows) == 1, (
        "Post-fix: retry-on-busy must land the INSERT after 2 "
        f"transient locks. Got {len(rows)} rows.")
    assert rows[0][2] == _REJ_ARGS["rejection_reason"]


# ─── 2. SWALLOW-ON-EXHAUSTION (TELEMETRY CONTRACT) ───────────────────────


def test_insert_rejection_swallows_after_retry_exhaustion(
        tmp_path: Path, caplog):
    """Post-fix: after all 3 BEGIN IMMEDIATE retries fail, the
    OperationalError MUST be SWALLOWED with a structured WARNING.
    Rejection rows are telemetry; losing one is preferable to
    crashing the scan tick.

    CONTRAST: insert_bot_order RAISES on exhaustion (crash-safety
    contract — orders must not silently lose records). insert_rejection
    is the OPPOSITE — drop the row, keep the tick alive.

    Pre-fix RED on the retry-count pin: bare execute(INSERT) raises
    on call 1 → propagates → test expects clean return → fails AND
    execute_call_count == 1 (not 3).
    """
    state = _make_state(tmp_path)

    def _execute_hook(sql, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    state.conn.__dict__["_execute_hook"] = _execute_hook

    with caplog.at_level(logging.WARNING, logger="root"):
        # MUST return cleanly (no raise).
        state.insert_rejection(**_REJ_ARGS)

    assert state.conn.execute_call_count >= 3, (
        "Post-fix: retry-on-busy must attempt BEGIN IMMEDIATE 3 "
        f"times before swallowing. Got "
        f"{state.conn.execute_call_count} execute() call(s).")

    failure_warnings = [
        r for r in caplog.records
        if "insert_rejection failed" in r.getMessage()
        and "begin_immediate_retries" in r.getMessage()
        and "recent_writes" in r.getMessage()
    ]
    assert failure_warnings, (
        "Post-fix: retry-exhaustion swallow must emit a structured "
        "WARNING with begin_immediate_retries + recent_writes "
        f"envelope. Got: {[r.getMessage()[:200] for r in caplog.records]}")


# ─── 3. B3-fu1 COMMIT-RACE TOLERANCE ─────────────────────────────────────


def test_insert_rejection_swallows_no_transaction_active_commit_race(
        tmp_path: Path):
    """Post-fix: if the COMMIT step raises OperationalError
    "cannot commit - no transaction is active" — the B3-fu1
    cross-thread commit-race signature — insert_rejection MUST
    swallow and return normally. The racer's commit (here,
    settlement_tracker's commit) already captured our INSERT
    before our COMMIT fired, so data is preserved.

    Pre-fix RED: bare commit() has no try/except → OperationalError
    propagates → test expects clean return → fails.
    """
    state = _make_state(tmp_path)

    def _commit_hook():
        raise sqlite3.OperationalError(
            "cannot commit - no transaction is active")

    state.conn.__dict__["_commit_hook"] = _commit_hook

    # MUST return cleanly (no raise).
    state.insert_rejection(**_REJ_ARGS)


# ─── 4. "another row available" telemetry swallow (tonight's signature) ──


def test_insert_rejection_swallows_another_row_available(
        tmp_path: Path, caplog):
    """Post-fix: tonight's incident signature
    `sqlite3.OperationalError: another row available` (caused by an
    unfinalized cursor on the shared connection, surfaced during the
    disk-full cascade) MUST be swallowed by the broad telemetry-class
    catch on the INSERT/COMMIT step.

    Pre-fix RED: the bare execute(INSERT) at state.py:2049 propagates
    the OperationalError → tick crashes (the 2026-05-19 Tick error
    pattern). Post-fix: swallow + diagnostic-WARNING envelope.

    This pins the exact signature that surfaced in production on
    2026-05-19 at the insert_rejection site — it's a non-locked,
    non-busy OperationalError that the retry loop should NOT retry
    (no point — retrying won't finalize the cursor) but should
    swallow at the telemetry-class boundary.
    """
    state = _make_state(tmp_path)

    raised_once = [False]

    def _execute_hook(sql, *args, **kwargs):
        # Let BEGIN IMMEDIATE succeed; fail on the INSERT.
        if "INSERT" in sql.upper() and not raised_once[0]:
            raised_once[0] = True
            raise sqlite3.OperationalError("another row available")
        return None

    state.conn.__dict__["_execute_hook"] = _execute_hook

    with caplog.at_level(logging.WARNING, logger="root"):
        # MUST return cleanly (no raise).
        state.insert_rejection(**_REJ_ARGS)

    failure_warnings = [
        r for r in caplog.records
        if "insert_rejection failed" in r.getMessage()
    ]
    assert failure_warnings, (
        "Post-fix: the 'another row available' telemetry swallow "
        "must emit a structured WARNING so operators can correlate "
        "to the broader contention storm class. Got: "
        f"{[r.getMessage()[:200] for r in caplog.records]}")
