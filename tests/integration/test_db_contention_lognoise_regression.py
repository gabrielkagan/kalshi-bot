"""db-contention log-noise regression (2026-06-13).

Three StateManager hot-path writers (insert_rejection /
insert_evaluated_opportunity / insert_bot_order) log a RICH structured
WARNING that already captures begin_immediate timing + retry count +
active/recent-writer envelope when SQLite contention trips their BEGIN
IMMEDIATE retry loop. (The fourth guarded site, mark_rejection_settled,
swallows the commit-race silently with no envelope of its own — out of
scope.) Pre-fix the three ALSO passed ``exc_info=True``, so every
handled-and-accounted contention event printed a full ~12-line
traceback to stderr. At current universe scale
that runs ~100/hr (chronic single-writer contention on ``state.db`` —
NOT a bug, the row loss is by-design telemetry-class swallow), and the
traceback flood buries genuine ERROR-level lines in journalctl.

The traceback for the KNOWN contention class is pure noise: it points
only at the ``conn.execute`` line the structured envelope already names
and carries no information the envelope lacks. For an UNEXPECTED
exception (schema bug, TypeError, …) the stack IS diagnostic and must
be preserved.

Fix: classify via ``bot.state._is_known_db_contention(exc)`` and pass
``exc_info=not _is_known_db_contention(e)`` at the contention-swallow
WARNING sites. Behavior of the retry/swallow control flow is UNCHANGED
— this is an observability-only change.

Pre-fix RED expectations:
- ``test_swallowed_contention_logs_without_traceback``: pre-fix the
  WARNING record carries a truthy exc_info (traceback) → the
  ``not record.exc_info`` assertion fails (post-fix exc_info is False,
  which logging treats identically to None — no stack emitted).
- ``test_known_db_contention_classifier``: the helper does not exist
  pre-fix → ImportError.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from bot.state import StateManager


# ─── Error-injection wrapper (mirrors the sister defensive-guard tests) ──


class _ConnWrapper:
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
    db = tmp_path / "state.db"
    state = StateManager(str(db))
    state.conn = _ConnWrapper(state.conn)
    return state


_REJ_ARGS = dict(
    ticker="KXBTC15M-26JUN130400-00",
    event_ticker="KXBTC15M-26JUN130400",
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
    orderbook_levels_json="[]",
    hour_sin=0.0,
    hour_cos=1.0,
    sigma_winsorize=0.0,
    prob_breakeven_gap=0.0,
)


def _insert_hook_raising(exc_factory):
    """Return an execute-hook that raises on the rejection INSERT only."""
    def _hook(sql, *args, **kwargs):
        _s = str(sql)
        if "INSERT" in _s and "rejected_opportunities" in _s:
            raise exc_factory()
        return None
    return _hook


# ─── 1. The classifier (load-bearing decision logic) ─────────────────────


def test_known_db_contention_classifier():
    from bot.state import _is_known_db_contention

    # The chronic single-writer + cursor-race signatures = noise.
    assert _is_known_db_contention(sqlite3.OperationalError("database is locked"))
    assert _is_known_db_contention(sqlite3.OperationalError("database table is locked"))
    assert _is_known_db_contention(sqlite3.DatabaseError("another row available"))
    assert _is_known_db_contention(sqlite3.DatabaseError("no more rows available"))
    assert _is_known_db_contention(
        sqlite3.OperationalError("cannot commit - no transaction is active")
    )

    # Genuine bugs must NOT be classified as benign contention —
    # their tracebacks stay.
    assert not _is_known_db_contention(ValueError("table schema drifted"))
    assert not _is_known_db_contention(TypeError("NoneType is not subscriptable"))
    assert not _is_known_db_contention(
        sqlite3.OperationalError("no such column: cal_mlp_p_mean")
    )


# ─── 2. Contention swallow → structured WARNING, NO traceback ────────────


def test_swallowed_contention_logs_without_traceback(tmp_path: Path, caplog):
    """A persistent `database is locked` on the INSERT exhausts the
    guard and swallows with the structured envelope — but post-fix the
    WARNING must NOT carry a traceback (exc_info), because the envelope
    already names the failing statement + retry/writer context.

    Pre-fix RED: the swallow passed exc_info=True → record.exc_info is
    a 3-tuple → this assertion fails.
    """
    state = _make_state(tmp_path)
    state.conn.__dict__["_execute_hook"] = _insert_hook_raising(
        lambda: sqlite3.OperationalError("database is locked")
    )

    with caplog.at_level(logging.WARNING):
        state.insert_rejection(**_REJ_ARGS)  # must NOT raise (telemetry swallow)

    recs = [r for r in caplog.records if "insert_rejection failed" in r.getMessage()]
    assert recs, "expected the structured insert_rejection swallow WARNING"
    assert all(not r.exc_info for r in recs), (  # None or False — both suppress the stack
        "contention swallow must not emit a traceback — the structured "
        "envelope (begin_immediate/retries/recent_writes) already carries "
        "the diagnostic context; the stack is pure journalctl noise"
    )


# ─── 3. Unexpected exception → traceback PRESERVED ───────────────────────


def test_swallowed_unexpected_error_keeps_traceback(tmp_path: Path, caplog):
    """A non-contention exception hitting the same broad swallow MUST
    still log its traceback — for a genuine bug the stack is the whole
    point. Guards against an over-broad exc_info suppression."""
    state = _make_state(tmp_path)
    state.conn.__dict__["_execute_hook"] = _insert_hook_raising(
        lambda: ValueError("unexpected schema drift")
    )

    with caplog.at_level(logging.WARNING):
        state.insert_rejection(**_REJ_ARGS)

    recs = [r for r in caplog.records if "insert_rejection failed" in r.getMessage()]
    assert recs, "expected the structured insert_rejection swallow WARNING"
    assert all(r.exc_info for r in recs), (  # a (type, value, tb) tuple
        "an unexpected (non-contention) exception must keep its "
        "traceback — only the known contention class is suppressed"
    )
