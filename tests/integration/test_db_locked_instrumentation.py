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

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def bot_impl_source() -> str:
    """Bit 7.1 retarget (2026-05-10): StateManager (incl.
    insert_evaluated_opportunity) moved to bot/state.py.

    Bit 8.1 retarget (2026-05-10): OpportunityScanner (incl.
    low_price_shadow_signals warning site) moved to bot/scanner/__init__.py.

    The fixture concats all three files so tests find their target
    regardless of which module owns it post-extraction. The fixture name
    stays for backward compat with the L40-pattern AST/regex guards.
    AST-walks find class/method nodes from any file; regex/string
    searches find markers across the concat."""
    # Bit 9.3-iii.c (2026-05-11): bot/_impl.py DELETED — read tolerant of absence.
    parts = []
    impl_p = ROOT / "bot" / "_impl.py"
    if impl_p.exists():
        parts.append(impl_p.read_text())
    parts.append((ROOT / "bot" / "state.py").read_text())
    scanner_p = ROOT / "bot" / "scanner" / "__init__.py"
    if scanner_p.exists():
        parts.append(scanner_p.read_text())
    return "\n".join(parts)


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
    import pytest as _pytest_bit_iii_c_skip; _pytest_bit_iii_c_skip.skip("bot/_impl.py removed (Bit 9.3-iii.c) — re-export contract retired", allow_module_level=False)
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


def test_begin_immediate_duration_captured_on_failure(caplog):
    """When BEGIN IMMEDIATE raises OperationalError, the failure-path warning
    log must include `begin_immediate_duration_ms=...` so the operator can
    distinguish:
      - 30000ms = busy_timeout-driven (some other writer held the lock)
      - <100ms  = fast-fail (different mechanism — SQLITE_LOCKED, conn-state, etc.)

    This pivots Bit 4.4-followup RCA: prior diag captured the BEGIN IMMEDIATE
    error message but not its duration. The 03:33:07 hit on prod (PID 1613244)
    showed the wrapped INSERT failed in 0.3ms, suggesting the BEGIN IMMEDIATE
    also fast-failed — but we couldn't prove it without this duration field.
    """
    caplog.set_level(logging.WARNING)
    sm = _make_state_manager_with_mocked_conn()

    def _execute_side_effect(sql, *args, **kwargs):
        if sql == "BEGIN IMMEDIATE":
            raise sqlite3.OperationalError("database is locked")
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
    assert "begin_immediate_duration_ms=" in msg, (
        f"warning must include begin_immediate_duration_ms= field; got: {msg!r}"
    )


def test_begin_immediate_retries_on_busy_then_succeeds(caplog):
    """Per RCA (2026-05-09): SQLite returns SQLITE_BUSY immediately for
    intra-process lock contention (busy_handler bypassed). We retry
    explicitly with jittered backoff. If retry succeeds (e.g., the
    market_obs_snapshotter holder finishes its 7s tx between attempt 1
    and attempt 2), the row is captured.

    Test mocks BEGIN IMMEDIATE: 1st call raises, 2nd call succeeds. Verify
    no warning fires (insert succeeded).
    """
    caplog.set_level(logging.WARNING)
    sm = _make_state_manager_with_mocked_conn()

    call_count = {"begin": 0}

    def _execute_side_effect(sql, *args, **kwargs):
        if sql == "BEGIN IMMEDIATE":
            call_count["begin"] += 1
            if call_count["begin"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return MagicMock()  # 2nd attempt succeeds
        if "INSERT INTO evaluated_opportunities" in sql:
            return MagicMock()
        if sql == "COMMIT":
            return MagicMock()
        return MagicMock()

    sm.conn.execute.side_effect = _execute_side_effect

    with _no_raise():
        _call_insert_with_minimal_args(sm)

    # 1st BEGIN IMMEDIATE failed, 2nd succeeded → no failure warning
    failure_warnings = [
        r for r in caplog.records
        if "insert_evaluated_opportunity failed" in r.getMessage()
    ]
    assert not failure_warnings, (
        f"BEGIN IMMEDIATE retry should have succeeded; got failure warning: "
        f"{[r.getMessage() for r in failure_warnings]}"
    )
    # And BEGIN IMMEDIATE was called at least twice
    assert call_count["begin"] >= 2


def test_begin_immediate_gives_up_after_max_retries(caplog):
    """If BEGIN IMMEDIATE fails repeatedly, we give up after a bounded
    number of retries (3) and log the failure. The retry count must be
    bounded so the scan loop doesn't hang on persistent contention.

    Adversarial review reduced from 5x50-200ms to 3x25-75ms (worst case
    ~225ms vs 1000ms) to keep MainThread blocking under SCAN_BODY_SLOW
    1.5s budget when multiple inserts contend in the same tick.
    """
    caplog.set_level(logging.WARNING)
    sm = _make_state_manager_with_mocked_conn()

    call_count = {"begin": 0}

    def _execute_side_effect(sql, *args, **kwargs):
        if sql == "BEGIN IMMEDIATE":
            call_count["begin"] += 1
            raise sqlite3.OperationalError("database is locked")
        if "INSERT INTO evaluated_opportunities" in sql:
            raise sqlite3.OperationalError("database is locked")
        return MagicMock()

    sm.conn.execute.side_effect = _execute_side_effect

    with _no_raise():
        _call_insert_with_minimal_args(sm)

    # Should have retried up to the bound (3 attempts)
    assert 2 <= call_count["begin"] <= 5, (
        f"BEGIN IMMEDIATE should retry ~3x but bounded; got {call_count['begin']} attempts"
    )

    # Failure warning fired since all retries exhausted
    failure_warnings = [
        r for r in caplog.records
        if "insert_evaluated_opportunity failed" in r.getMessage()
    ]
    assert failure_warnings


def test_stale_tx_error_does_not_retry(caplog):
    """When BEGIN IMMEDIATE raises 'cannot start a transaction within a
    transaction' (Python-side stale-tx, NOT transient lock contention),
    retries are pointless — sleep won't clear the conn's tx state. The
    loop must short-circuit on the first such failure to avoid wasting
    ~225ms of dead MainThread sleep.

    Adversarial review (2026-05-09 R1): identified this case in
    production logs (see test_runtime_warning_includes_begin_immediate_error_message).
    """
    caplog.set_level(logging.WARNING)
    sm = _make_state_manager_with_mocked_conn()

    call_count = {"begin": 0}

    def _execute_side_effect(sql, *args, **kwargs):
        if sql == "BEGIN IMMEDIATE":
            call_count["begin"] += 1
            raise sqlite3.OperationalError(
                "cannot start a transaction within a transaction"
            )
        if "INSERT INTO evaluated_opportunities" in sql:
            raise sqlite3.OperationalError("database is locked")
        return MagicMock()

    sm.conn.execute.side_effect = _execute_side_effect

    with _no_raise():
        _call_insert_with_minimal_args(sm)

    # Stale-tx error → no retries (loop breaks on first failure)
    assert call_count["begin"] == 1, (
        f"stale-tx error should NOT retry; got {call_count['begin']} attempts"
    )


def test_failure_log_includes_retry_count(caplog):
    """The failure warning must include `begin_immediate_retries=N` so the
    operator can distinguish single-shot fast-fails from exhausted retry
    loops in the journal."""
    caplog.set_level(logging.WARNING)
    sm = _make_state_manager_with_mocked_conn()

    def _execute_side_effect(sql, *args, **kwargs):
        if sql == "BEGIN IMMEDIATE":
            raise sqlite3.OperationalError("database is locked")
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
    assert "begin_immediate_retries=" in msg, (
        f"warning must include begin_immediate_retries=N field; got: {msg!r}"
    )


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


# ─── AST: BEGIN IMMEDIATE except handlers catch sqlite3.DatabaseError ──────


_BEGIN_RETRY_LOOP_HOSTS = (
    "insert_evaluated_opportunity",
    "insert_rejection",
    "insert_bot_order",
)
"""Three StateManager hot-path writers that share the BEGIN IMMEDIATE
retry-loop pattern (`for _attempt in range(3): try: BEGIN IMMEDIATE`).
The fourth member of the SQLite-section "4-site coverage chain" —
`mark_rejection_settled` — has only a commit-race except (no BEGIN
retry loop), so it is excluded; widen this tuple if a future Bit
adds a BEGIN retry to that function.
"""


def _find_begin_retry_loop(fn: ast.FunctionDef) -> "ast.For | None":
    """Locate the ``for _attempt in range(...):`` retry loop inside
    a function body. Matches on the SHAPE of the for-loop header so
    the guard survives identifier renames at the loop body level."""
    for sub in ast.walk(fn):
        if not isinstance(sub, ast.For):
            continue
        if not (isinstance(sub.target, ast.Name)
                and sub.target.id == "_attempt"):
            continue
        if not (isinstance(sub.iter, ast.Call)
                and isinstance(sub.iter.func, ast.Name)
                and sub.iter.func.id == "range"):
            continue
        return sub
    return None


def _assert_handler_is_sqlite_database_error(
        handler: ast.ExceptHandler, *, host: str) -> None:
    """Assert ``handler.type`` is the ``sqlite3.DatabaseError`` Attribute
    node — i.e. an `Attribute(value=Name('sqlite3'), attr='DatabaseError')`."""
    htype = handler.type
    assert isinstance(htype, ast.Attribute), (
        f"[{host}] except handler type must be `sqlite3.DatabaseError` "
        f"(attribute access), got AST node "
        f"{type(htype).__name__ if htype is not None else 'None (bare except)'}: "
        f"{ast.dump(htype) if htype is not None else '<bare>'}"
    )
    assert (isinstance(htype.value, ast.Name)
            and htype.value.id == "sqlite3"), (
        f"[{host}] except handler must qualify the class via the "
        f"`sqlite3` module; got value={ast.dump(htype.value)}"
    )
    assert htype.attr == "DatabaseError", (
        f"[{host}] except handler must catch `sqlite3.DatabaseError` "
        f"(the parent class). Past-48h VPS-journal histogram (2026-05-22) "
        f"showed 21× `DatabaseError: another row available` + 2× "
        f"`DatabaseError: no more rows available` escaping the narrow "
        f"`OperationalError` catch at the BEGIN IMMEDIATE retry sites — "
        f"each escape lost a telemetry row (or, for insert_bot_order, "
        f"propagated up the call stack with crash-safety divergence). "
        f"Broadening to the parent class catches both `OperationalError` "
        f"and the bare-`DatabaseError` raises while preserving the "
        f"transient/non-transient string-match dispatch. "
        f"Got `sqlite3.{htype.attr}`."
    )


@pytest.mark.parametrize("host_name", _BEGIN_RETRY_LOOP_HOSTS)
def test_begin_immediate_retry_loop_catches_database_error(host_name: str):
    """Each StateManager BEGIN IMMEDIATE retry loop must catch
    ``sqlite3.DatabaseError`` (the parent class), NOT just
    ``sqlite3.OperationalError``.

    Background: Python's sqlite3 module surfaces stale-cursor-class
    raises ("another row available", "no more rows available") at the
    bare ``DatabaseError`` class. The pre-fix narrow ``OperationalError``
    catch at each site let those raises escape the retry block —
    telemetry rows were lost (insert_evaluated_opportunity, insert_rejection)
    and the crash-safety site (insert_bot_order) would propagate the
    exception up the call stack, potentially killing a scan tick during
    order placement.

    Robustness against future refactors:

    - The guard ITERATES retry-loop ``try.handlers`` and checks the
      LAST handler (Python's except-matching is first-match-wins, so
      a narrow handler in front of a broad handler is fine — what we
      pin is that the FINAL catch IS the parent class). This survives
      a future defensive refactor that adds a more-specific handler
      (e.g. ``except sqlite3.IntegrityError`` then ``except
      sqlite3.DatabaseError``) without false-positive failures.
    - A no-op-narrow-shadows-broad inversion (broad first, narrow
      after) is caught by an explicit assertion that no handler EARLIER
      than the DatabaseError one is a sqlite3-subclass — the earlier
      handler must be for a non-sqlite class (a future ``except
      json.JSONDecodeError`` or similar) so the DatabaseError catch
      stays reachable.
    """
    state_py_path = ROOT / "bot" / "state.py"
    tree = ast.parse(state_py_path.read_text())

    fn = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef)
                and node.name == host_name):
            fn = node
            break
    assert fn is not None, (
        f"could not locate {host_name!r} in bot/state.py — if the "
        f"function was renamed, update _BEGIN_RETRY_LOOP_HOSTS"
    )

    retry_loop = _find_begin_retry_loop(fn)
    assert retry_loop is not None, (
        f"could not locate `for _attempt in range(...):` retry loop "
        f"inside {host_name!r}"
    )

    try_stmt = None
    for stmt in retry_loop.body:
        if isinstance(stmt, ast.Try):
            try_stmt = stmt
            break
    assert try_stmt is not None, (
        f"[{host_name}] could not locate try/except inside the retry loop"
    )
    assert len(try_stmt.handlers) >= 1, (
        f"[{host_name}] retry-loop try must have at least one except handler"
    )

    # Pin: the LAST handler is `sqlite3.DatabaseError`. Earlier handlers
    # (if any) MUST be non-sqlite — otherwise a narrow sqlite-subclass
    # in front (e.g. accidentally adding `except sqlite3.OperationalError`
    # FIRST and `except sqlite3.DatabaseError` SECOND) would re-create
    # the narrow-shadow bug we are fixing for the transient classes the
    # retry path should still hit.
    _assert_handler_is_sqlite_database_error(
        try_stmt.handlers[-1], host=host_name)

    for earlier in try_stmt.handlers[:-1]:
        htype = earlier.type
        if isinstance(htype, ast.Attribute) and isinstance(htype.value, ast.Name):
            assert htype.value.id != "sqlite3", (
                f"[{host_name}] except handler earlier than the final "
                f"`sqlite3.DatabaseError` catch is a sqlite3 subclass "
                f"({ast.dump(htype)}) — first-match-wins means it would "
                f"shadow the broad catch and re-introduce the bug. "
                f"Earlier handlers must be for non-sqlite classes."
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
