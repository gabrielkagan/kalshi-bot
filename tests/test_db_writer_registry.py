"""Tests for bot/db_writer_registry.py — process-global tracker for active
sqlite3 writers.

Background: post-Bit 4.4 + db-locked instrumentation captured 2 diag hits
showing the SLOW-PATH busy_timeout pattern (begin_immediate=
'OperationalError: database is locked', in_tx=False, MainThread). This
confirms ANOTHER connection holds the writer lock ≥30s but doesn't
identify which one.

This module + its consumers (StateManager + 6 other writer modules)
log per-write activity AND let the StateManager failure-path dump a
snapshot of currently-active writers, so the operator can grep the
journal for the long-holding writer.

L33 (Bit 4.4): annotation-consumer pinning. The same drift-class applies
here — the wrapping of write-site call sites must stay in sync with the
registry API. AST tests verify each instrumented module references the
registry's tracked_write context manager.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ─── 1. Module exists + API surface ────────────────────────────────────────


def test_module_imports():
    import bot.db_writer_registry  # noqa: F401


def test_module_exposes_expected_api():
    """Public surface: tracked_write, register_write, unregister_write,
    snapshot_active. Plus the WRITER_ACTIVE log marker so operators can
    grep the journal."""
    import bot.db_writer_registry as r
    assert hasattr(r, "tracked_write")
    assert hasattr(r, "register_write")
    assert hasattr(r, "unregister_write")
    assert hasattr(r, "snapshot_active")


# ─── 2. register/unregister round-trip ─────────────────────────────────────


def test_register_returns_token_string():
    import bot.db_writer_registry as r
    token = r.register_write("test_module", "test_kind")
    assert isinstance(token, str)
    assert len(token) > 0
    # Cleanup so other tests aren't polluted
    r.unregister_write(token)


def test_register_then_snapshot_shows_writer():
    import bot.db_writer_registry as r
    token = r.register_write("snap_test", "kind_x")
    try:
        active = r.snapshot_active()
        names = [name for (_tok, _started, _kind, _th, name) in
                 ((t, s, k, th, t.split("#")[0]) for (t, s, k, th) in active)]
        assert "snap_test" in names
    finally:
        r.unregister_write(token)


def test_unregister_clears_writer_from_snapshot():
    import bot.db_writer_registry as r
    token = r.register_write("clear_test", "kind_y")
    r.unregister_write(token)
    active = r.snapshot_active()
    names = [t.split("#")[0] for (t, _s, _k, _th) in active]
    assert "clear_test" not in names


def test_unregister_unknown_token_is_silent():
    """Unregistering a non-existent token must not raise. Safe-by-default
    for partial-failure paths in the consumer modules."""
    import bot.db_writer_registry as r
    # Should not raise
    r.unregister_write("does_not_exist#0#thread")


# ─── 3. tracked_write context manager ──────────────────────────────────────


def test_tracked_write_registers_during_block_and_unregisters_after():
    import bot.db_writer_registry as r

    with r.tracked_write("ctx_test", "kind_z"):
        active = r.snapshot_active()
        names = [t.split("#")[0] for (t, _s, _k, _th) in active]
        assert "ctx_test" in names

    active_after = r.snapshot_active()
    names_after = [t.split("#")[0] for (t, _s, _k, _th) in active_after]
    assert "ctx_test" not in names_after


def test_tracked_write_unregisters_on_exception():
    """If the wrapped block raises, the registry must still clean up.
    Otherwise stale entries pollute snapshot_active() forever."""
    import bot.db_writer_registry as r

    with pytest.raises(ValueError):
        with r.tracked_write("exc_test", "kind"):
            raise ValueError("simulated write failure")

    active = r.snapshot_active()
    names = [t.split("#")[0] for (t, _s, _k, _th) in active]
    assert "exc_test" not in names


# ─── 4. Per-write log line emission ────────────────────────────────────────


def test_per_write_log_emitted_with_marker(caplog):
    """Every completed write must produce a single log line with a
    grep-able WRITER_ACTIVE marker so operators can search the journal
    even without the snapshot."""
    import bot.db_writer_registry as r

    caplog.set_level(logging.INFO)
    with r.tracked_write("log_test", "log_kind"):
        time.sleep(0.001)

    msgs = [rec.getMessage() for rec in caplog.records]
    matches = [m for m in msgs if "WRITER_ACTIVE" in m and "log_test" in m]
    assert matches, (
        f"WRITER_ACTIVE log line not found. caplog records: {msgs}"
    )
    line = matches[0]
    assert "name='log_test'" in line or 'name="log_test"' in line, (
        f"log line missing name= field: {line!r}"
    )
    assert "kind='log_kind'" in line or 'kind="log_kind"' in line
    assert "duration_ms=" in line
    assert "status=ok" in line


def test_per_write_log_marks_failure_on_exception(caplog):
    """When the wrapped block raises, the log line must show status=fail
    so operators distinguish failed writes from healthy ones."""
    import bot.db_writer_registry as r

    caplog.set_level(logging.INFO)
    with pytest.raises(RuntimeError):
        with r.tracked_write("fail_test", "fail_kind"):
            raise RuntimeError("boom")

    msgs = [rec.getMessage() for rec in caplog.records]
    matches = [m for m in msgs if "WRITER_ACTIVE" in m and "fail_test" in m]
    assert matches
    assert "status=fail" in matches[0]


# ─── 5. Thread-safety ──────────────────────────────────────────────────────


def test_concurrent_registers_are_thread_safe():
    """Multiple threads registering simultaneously must not corrupt the
    registry. The bot has 8 sqlite3 connections + multiple threads, all
    potentially calling into the registry concurrently."""
    import bot.db_writer_registry as r

    n_threads = 16
    n_iters = 50
    errors: list = []

    def worker():
        try:
            for _ in range(n_iters):
                with r.tracked_write("thread_test", "kind"):
                    time.sleep(0.0001)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    # All threads completed; registry should be empty (all unregistered)
    active = r.snapshot_active()
    names = [t.split("#")[0] for (t, _s, _k, _th) in active]
    assert "thread_test" not in names


# ─── 6. snapshot_active return shape ───────────────────────────────────────


def test_snapshot_active_returns_list_of_tuples():
    """snapshot_active() returns a list of (token, started_ts, kind,
    thread_name) tuples. This shape is consumed by
    StateManager.insert_evaluated_opportunity's failure log."""
    import bot.db_writer_registry as r

    token = r.register_write("shape_test", "kind")
    try:
        active = r.snapshot_active()
        assert isinstance(active, list)
        for entry in active:
            assert isinstance(entry, tuple)
            assert len(entry) == 4
            tok, started, kind, thread = entry
            assert isinstance(tok, str)
            assert isinstance(started, float)
            assert isinstance(kind, str)
            assert isinstance(thread, str)
    finally:
        r.unregister_write(token)


# ─── 6.5. Recent-writes ring buffer (intra-process culprit RCA) ────────────


def test_recent_writes_api_exists():
    """`recent_writes(window_s)` returns writes that finished within the
    last `window_s` seconds. Used by StateManager's failure-path log to
    identify the lock-holder that JUST released. The lock-holder typically
    finishes in milliseconds before our BEGIN IMMEDIATE returns SQLITE_BUSY,
    so `snapshot_active()` shows [] but `recent_writes(5.0)` reveals it."""
    import bot.db_writer_registry as r
    assert hasattr(r, "recent_writes")


def test_recent_writes_includes_just_finished_writer():
    """A write that just unregistered should appear in recent_writes()
    with elapsed_since_finished close to zero."""
    import bot.db_writer_registry as r
    import time as t

    with r.tracked_write("recent_test", "kind"):
        t.sleep(0.001)

    recent = r.recent_writes(window_s=1.0)
    names = [name for (name, _kind, _dur, _finished_ts, _th) in recent]
    assert "recent_test" in names, (
        f"recent_writes() missing the just-finished entry. recent={recent!r}"
    )


def test_recent_writes_excludes_old_entries():
    """Writes that finished outside the window must be excluded so the
    failure log isn't flooded with stale entries."""
    import bot.db_writer_registry as r
    import time as t

    with r.tracked_write("old_test", "kind"):
        pass

    # Wait past the window
    t.sleep(0.05)

    recent = r.recent_writes(window_s=0.01)  # 10ms window
    names = [name for (name, _kind, _dur, _finished_ts, _th) in recent]
    assert "old_test" not in names, (
        f"recent_writes(window_s=0.01) should exclude write finished 50ms ago. recent={recent!r}"
    )


def test_recent_writes_returns_tuple_shape():
    """Each entry: (name, kind, duration_ms, finished_ts, thread_name).
    Consumed by StateManager's failure-path formatter."""
    import bot.db_writer_registry as r

    with r.tracked_write("shape2_test", "kind_a"):
        pass

    recent = r.recent_writes(window_s=1.0)
    assert recent
    for entry in recent:
        assert isinstance(entry, tuple)
        assert len(entry) == 5
        name, kind, dur, finished_ts, thread = entry
        assert isinstance(name, str)
        assert isinstance(kind, str)
        assert isinstance(dur, float)
        assert isinstance(finished_ts, float)
        assert isinstance(thread, str)


def test_recent_writes_ring_buffer_bounded():
    """Ring buffer must be bounded (deque maxlen) so a long-running
    process doesn't accumulate unbounded entries. Test by adding many
    writes and asserting len <= maxlen."""
    import bot.db_writer_registry as r

    for i in range(100):
        with r.tracked_write(f"buf_test_{i}", "kind"):
            pass

    recent = r.recent_writes(window_s=999999.0)  # very wide window
    assert len(recent) <= 50, (
        f"recent_writes ring buffer not bounded: {len(recent)} entries"
    )


# ─── 7. AST: instrumented modules reference the registry ───────────────────


import ast


# Modules that ACTUALLY write to state.db (verified by grep for INSERT/UPDATE/
# DELETE/commit on the module's connection). supabase_sync.py is a state.db
# READER (writes go to Supabase HTTP), so it's not tracked here.
WRITER_MODULES = [
    "bot/_impl.py",
    "fifteenm_shadow.py",
    "market_observations_snapshotter.py",
    "bot/engines/weather_engine.py",  # Sprint 10.1c sibling-reorg (2026-05-11)
    "sports_engine.py",
    "spx_harrv_shadow.py",
]


@pytest.mark.parametrize("module_path", WRITER_MODULES)
def test_writer_module_imports_db_writer_registry(module_path):
    """Each writer module must import from bot.db_writer_registry so the
    registry can track its writes. This is the L33 contract."""
    src = (ROOT / module_path).read_text()
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "bot.db_writer_registry":
                found = True
                break
    assert found, (
        f"{module_path} does not `from bot.db_writer_registry import ...`. "
        f"This is the writer-tracking contract for db-locked RCA."
    )


@pytest.mark.parametrize("module_path", WRITER_MODULES)
def test_writer_module_uses_tracked_write(module_path):
    """Each writer module must actually CALL tracked_write. AST sweep
    looking for `tracked_write(...)` references."""
    src = (ROOT / module_path).read_text()
    assert "tracked_write" in src, (
        f"{module_path} imports the registry but does not call tracked_write. "
        f"The wrapping is the actual instrumentation."
    )


# ─── 8. StateManager failure log includes snapshot ─────────────────────────


def test_state_manager_failure_log_includes_writer_snapshot():
    """When insert_evaluated_opportunity fails with database-is-locked,
    the warning log must include `active_writers=...` from
    snapshot_active(). This is the symptom-to-cause bridge.

    Bit 7.1 retarget (2026-05-10): StateManager moved to bot/state.py."""
    state_src = (ROOT / "bot" / "state.py").read_text()
    tree = ast.parse(state_src)
    func = next(
        (
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
            and n.name == "insert_evaluated_opportunity"
        ),
        None,
    )
    assert func is not None, "insert_evaluated_opportunity definition missing"
    body_src = ast.get_source_segment(state_src, func) or ""
    assert "snapshot_active" in body_src or "active_writers=" in body_src, (
        "insert_evaluated_opportunity failure log must include "
        "snapshot_active() output via 'active_writers=' field."
    )


def test_state_manager_failure_log_includes_recent_writes():
    """When insert_evaluated_opportunity hits the FAST-fail path
    (begin_immediate_duration_ms ~0.1ms), the lock-holder typically
    released JUST before our BEGIN IMMEDIATE returned SQLITE_BUSY —
    so snapshot_active() returns []. recent_writes() captures writes
    that finished within the last few seconds, which (per RCA) contains
    the intra-process lock-holder."""
    bot_impl = (ROOT / "bot" / "_impl.py").read_text()
    assert "recent_writes" in bot_impl, (
        "insert_evaluated_opportunity failure log must include "
        "recent_writes(window_s=...) output via 'recent_writes=' field."
    )
