"""RCA instrumentation for `database is locked` errors at deploy time.

Background: post-deploy verify failed on Bit 4.4 (commit 1d7fb6f) with 10
non-benign error lines in 2 min, 7 of which were `insert_evaluated_opportunity
failed: database is locked` at 01:46:22 (all in the same second). Per RCA
in this session:

- Bit 4.3 deploy at 01:14 had 11 errors spread over 9 seconds (slow contention
  consistent with busy_timeout-driven waits).
- Bit 4.4 deploy at 01:46 had 7+2 errors in single seconds (fast contention
  inconsistent with busy_timeout).
- The fast pattern points at a stale-tx scenario: BEGIN IMMEDIATE raises
  "cannot start a transaction within a transaction" (caught as OperationalError),
  the fall-through INSERT then runs inside the broken stale-tx and raises
  "database is locked" without waiting busy_timeout.
- 8 separate sqlite3 connections to state.db all in the bot main process
  compete for the writer lock.

This bit adds DIAGNOSTIC instrumentation only — no behavior change. The
warning log at the failure site is enriched with:

- `begin_immediate=...` — the BEGIN IMMEDIATE error message (or `'OK'` if
  it succeeded). Distinguishes "cannot start a tx within a tx" from
  "database is locked" so future deploys narrow the RCA.
- `thread=...` — the current thread name. Identifies which writer owns
  the failure path (MainThread = scan loop, others = engines).
- `in_tx=...` — `self.conn.in_transaction` at the failure moment.
  Distinguishes broken-stale-tx (in_tx=True) from clean lock-contention
  (in_tx=False).

After 1-2 deploys with this instrumentation in place, the contention's
true mechanism becomes visible in journalctl, and a targeted fix (retry
loop, conn-pool consolidation, etc.) can land with confidence.

Tests are AST-style on the patched call sites + a runtime test that
mocks the OperationalError path.
"""
from __future__ import annotations

import ast
import logging
import re
import sqlite3
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def bot_impl_source() -> str:
    return (ROOT / "bot" / "_impl.py").read_text()


# ─── 1. AST: BEGIN IMMEDIATE except captures the error message ──────────────


def _slice_function_body(src: str, name: str) -> str:
    """Return the source of a top-level method named `name` from `src`.
    Uses AST to find exact start/end (not a fixed-size window — function
    bodies can exceed 30KB)."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"function {name!r} not found")


def test_begin_immediate_except_captures_error_message(bot_impl_source):
    """The except clause for BEGIN IMMEDIATE in insert_evaluated_opportunity
    must bind the OperationalError to a variable AND assign its message
    to `_be_err_repr` (or equivalent). Without this, the warning log can
    only say "insert failed" without distinguishing the two failure
    modes (cannot-start-tx vs database-is-locked at BEGIN).
    """
    body = _slice_function_body(bot_impl_source, "insert_evaluated_opportunity")
    assert "BEGIN IMMEDIATE" in body
    # The except clause must bind to a name (e.g., `_be_err`)
    assert re.search(
        r"except\s+sqlite3\.OperationalError\s+as\s+\w+\s*:", body
    ), (
        "BEGIN IMMEDIATE except must bind the OperationalError to a "
        "variable so its message can be captured for diagnostics. Pattern: "
        "`except sqlite3.OperationalError as _be_err:`"
    )
    # The captured message must be assigned to a diagnostic variable
    assert "_be_err_repr" in body, (
        "BEGIN IMMEDIATE except must assign the error message to "
        "`_be_err_repr` so the failure-path warning log can include it."
    )


# ─── 2. AST: failure-path warning log includes diag fields ──────────────────


def test_insert_evaluated_opportunity_warning_includes_diag(bot_impl_source):
    """The `insert_evaluated_opportunity failed` warning log must include
    three diagnostic fields: begin_immediate, thread, in_tx. Without these,
    the next post-deploy alert leaves the same RCA gap.
    """
    body = _slice_function_body(bot_impl_source, "insert_evaluated_opportunity")
    # The warning string must contain all three diag prefixes.
    assert "insert_evaluated_opportunity failed:" in body, (
        "failure warning log message marker missing"
    )
    # begin_immediate=...
    assert re.search(r"begin_immediate=", body), (
        "warning log must include 'begin_immediate=' field showing the "
        "BEGIN IMMEDIATE error message (or 'OK' if it succeeded)"
    )
    # thread=...
    assert re.search(r"\bthread=", body), (
        "warning log must include 'thread=' field showing "
        "threading.current_thread().name"
    )
    # in_tx=...
    assert re.search(r"\bin_tx=", body), (
        "warning log must include 'in_tx=' field showing "
        "self.conn.in_transaction at failure time"
    )


# ─── 3. AST: low_price_shadow_signals warning also enriched ─────────────────


def test_low_price_shadow_signals_warning_includes_diag(bot_impl_source):
    """The `low_price_shadow_signals insert failed` warning log must also
    include thread + in_tx fields. This site shares the same StateManager
    connection as insert_evaluated_opportunity and was caught in the same
    Bit 4.4 deploy cluster.
    """
    marker = "low_price_shadow_signals insert failed"
    m = bot_impl_source.find(marker)
    assert m >= 0, f"{marker!r} marker missing"
    # Look at a small window around the warning call (must be on same line
    # or one nearby).
    window = bot_impl_source[max(0, m - 50):m + 400]
    assert re.search(r"\bthread=", window), (
        f"low_price_shadow_signals warning log must include 'thread=' "
        f"field. Window: {window!r}"
    )
    assert re.search(r"\bin_tx=", window), (
        f"low_price_shadow_signals warning log must include 'in_tx=' "
        f"field. Window: {window!r}"
    )


# ─── 4. Runtime: mock OperationalError captures correct fields ──────────────


def _make_state_manager_with_mocked_conn():
    """Construct a StateManager with a mocked sqlite3 connection so we
    can simulate the failure paths without a real DB. Mirrors the helper
    style in test_kalshi_client_breakers.py.
    """
    import bot._impl as bi
    sm = bi.StateManager.__new__(bi.StateManager)
    sm.conn = MagicMock()
    sm.conn.in_transaction = True   # default — we'll override per test
    sm._scan_bid_cache = {}
    sm._scan_ms_cache = {}
    sm._scan_cx_gap_cache = {}
    sm._scan_ob_cache = {}
    sm._lifecycle_snapshot_failures = 0
    sm._extended_feature_provider = None
    sm._bot_state_provider = None
    sm._last_balance_cents = None
    return sm


def _call_insert_with_minimal_args(sm):
    """Call insert_evaluated_opportunity with the absolute minimum
    positional args. All optional kwargs default. Reaches the BEGIN
    IMMEDIATE / INSERT path so the failure-log instrumentation runs.
    """
    sm.insert_evaluated_opportunity(
        ticker="TEST-TICKER",
        event_ticker="TEST-EVT",
        asset="BTC",
        filter_stage="candidate",
    )


def test_runtime_warning_includes_begin_immediate_error_message(caplog):
    """When BEGIN IMMEDIATE raises OperationalError and INSERT also fails,
    the warning log must include the BEGIN error message string so the
    operator can distinguish the failure mode."""
    caplog.set_level(logging.WARNING)
    sm = _make_state_manager_with_mocked_conn()

    def _execute_side_effect(sql, *args, **kwargs):
        if sql == "BEGIN IMMEDIATE":
            raise sqlite3.OperationalError(
                "cannot start a transaction within a transaction"
            )
        if "INSERT INTO evaluated_opportunities" in sql:
            raise sqlite3.OperationalError("database is locked")
        return MagicMock()

    sm.conn.execute.side_effect = _execute_side_effect

    with _no_raise():
        _call_insert_with_minimal_args(sm)

    warnings = [
        r for r in caplog.records
        if "insert_evaluated_opportunity failed" in r.getMessage()
    ]
    assert warnings, (
        f"no insert_evaluated_opportunity-failed warning captured. "
        f"records: {[r.getMessage() for r in caplog.records]}"
    )
    msg = warnings[0].getMessage()
    assert "cannot start a transaction within a transaction" in msg, (
        f"warning must include the BEGIN IMMEDIATE error string; got: {msg!r}"
    )
    assert "thread=" in msg, f"warning must include thread=; got: {msg!r}"
    assert "in_tx=" in msg, f"warning must include in_tx=; got: {msg!r}"


def test_runtime_warning_marks_begin_immediate_ok_when_succeeds(caplog):
    """When BEGIN IMMEDIATE succeeds and INSERT fails, the warning log
    must show begin_immediate=OK (not a stale error message)."""
    caplog.set_level(logging.WARNING)
    sm = _make_state_manager_with_mocked_conn()

    def _execute_side_effect(sql, *args, **kwargs):
        if sql == "BEGIN IMMEDIATE":
            return MagicMock()  # succeeds
        if "INSERT INTO evaluated_opportunities" in sql:
            raise sqlite3.OperationalError("database is locked")
        return MagicMock()

    sm.conn.execute.side_effect = _execute_side_effect

    with _no_raise():
        _call_insert_with_minimal_args(sm)

    warnings = [
        r for r in caplog.records
        if "insert_evaluated_opportunity failed" in r.getMessage()
    ]
    assert warnings
    msg = warnings[0].getMessage()
    assert "begin_immediate='OK'" in msg, (
        f"warning must mark begin_immediate='OK' when BEGIN IMMEDIATE "
        f"succeeded; got: {msg!r}"
    )


# ─── helper: pytest "does not raise" context manager ───────────────────────

from contextlib import contextmanager


@contextmanager
def _no_raise():
    """Inverse of pytest.raises: assert no exception escaped."""
    try:
        yield
    except Exception as e:
        raise AssertionError(f"unexpected exception: {e!r}") from e
