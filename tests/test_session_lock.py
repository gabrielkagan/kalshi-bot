"""Sprint PSC Bit P5.1: SessionLock primitive tests.

RCA: parallel Claude Code sessions on the same repo collide on shared
files (MEMORY.md merge conflicts, commit races on origin/main, double-
shipping). This test pins the lock primitive that subsequent hooks
(P5.2 PreToolUse, P5.3 git-pre-commit, P5.6 memory-write) consume to
serialize per-target writes.

Spec source: ClickUp 86b9vgx5d acceptance criteria.

Stdlib-only (mirrors mclaude pattern). No `bot.*` imports.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import sys
import threading
import time
from pathlib import Path

import pytest

# scripts/ is not a package; test runs with the repo root on sys.path.
# Add the repo root explicitly so `from scripts._session_lock import ...`
# works regardless of how pytest is invoked.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import _session_lock  # noqa: E402
from scripts._session_lock import (  # noqa: E402
    LockHeldError,
    SessionLock,
    flatten_target_path,
    unflatten_target_path,
)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def lock_root(tmp_path, monkeypatch):
    """Redirect lock root to a tmp path so tests don't touch repo state.

    The module reads LOCK_ROOT lazily via a getter, so monkeypatching the
    module attribute is sufficient; we also set the env var as a belt-and-
    braces fallback in case any subprocess is spawned.
    """
    root = tmp_path / "active-work"
    root.mkdir()
    reclaim_log = tmp_path / "reclaim.log"
    monkeypatch.setattr(_session_lock, "_LOCK_ROOT_OVERRIDE", root)
    monkeypatch.setattr(_session_lock, "_RECLAIM_LOG_OVERRIDE", reclaim_log)
    monkeypatch.setenv("KALSHI_SESSION_LOCK_ROOT", str(root))
    monkeypatch.setenv("KALSHI_SESSION_LOCK_RECLAIM_LOG", str(reclaim_log))
    return root


# --------------------------------------------------------------------------
# Path-flatten round-trip
# --------------------------------------------------------------------------


class TestPathFlatten:
    def test_simple_path_round_trip(self):
        flat = flatten_target_path("bot/_impl.py")
        assert "/" not in flat
        assert flatten_target_path(unflatten_target_path(flat)) == flat

    def test_nested_path_round_trip(self):
        original = "bot/scanner/__init__.py"
        flat = flatten_target_path(original)
        assert "/" not in flat
        assert unflatten_target_path(flat) == original

    def test_slash_marker_is_preserved(self):
        # Per spec: slash → __SLASH__
        assert flatten_target_path("a/b") == "a__SLASH__b"
        assert unflatten_target_path("a__SLASH__b") == "a/b"

    def test_empty_components_rejected(self):
        # Defense against `//` or trailing-slash inputs creating ambiguous
        # round-trips.
        with pytest.raises(ValueError):
            flatten_target_path("a//b")

    def test_absolute_paths_rejected(self):
        with pytest.raises(ValueError):
            flatten_target_path("/abs/path")

    def test_path_traversal_rejected(self):
        with pytest.raises(ValueError):
            flatten_target_path("../escape")


# --------------------------------------------------------------------------
# Happy path: acquire / release
# --------------------------------------------------------------------------


class TestAcquireRelease:
    def test_acquire_returns_context_manager(self, lock_root):
        lock = SessionLock("bot/_impl.py")
        with lock.acquire() as held:
            assert held is lock
            assert lock.lockfile_path.exists()
            data = json.loads(lock.lockfile_path.read_text())
            assert data["pid"] == os.getpid()
            assert data["target_path"] == "bot/_impl.py"
            assert "started_at" in data
            assert "last_heartbeat" in data
            assert "session_id" in data
        # Released on __exit__.
        assert not lock.lockfile_path.exists()

    def test_acquire_release_pair_explicit(self, lock_root):
        lock = SessionLock("bot/_impl.py")
        lock.acquire_raw()
        try:
            assert lock.lockfile_path.exists()
        finally:
            lock.release()
        assert not lock.lockfile_path.exists()

    def test_lockfile_lives_under_active_work(self, lock_root):
        lock = SessionLock("bot/_impl.py")
        with lock.acquire():
            assert lock.lockfile_path.parent == lock_root

    def test_lockfile_name_is_flattened_target(self, lock_root):
        lock = SessionLock("bot/scanner/__init__.py")
        with lock.acquire():
            assert lock.lockfile_path.name.startswith("bot__SLASH__scanner__SLASH__")
            assert lock.lockfile_path.name.endswith(".lock")

    def test_release_is_idempotent(self, lock_root):
        lock = SessionLock("bot/_impl.py")
        lock.acquire_raw()
        lock.release()
        # Second release must not raise.
        lock.release()

    def test_release_without_acquire_is_noop(self, lock_root):
        lock = SessionLock("bot/_impl.py")
        # No raise; just a defensive no-op.
        lock.release()


# --------------------------------------------------------------------------
# Heartbeat
# --------------------------------------------------------------------------


class TestHeartbeat:
    def test_heartbeat_thread_updates_lockfile(self, lock_root, monkeypatch):
        # Force a tiny heartbeat interval for the test.
        monkeypatch.setattr(_session_lock, "HEARTBEAT_INTERVAL_S", 0.05)
        lock = SessionLock("bot/_impl.py")
        with lock.acquire():
            initial = json.loads(lock.lockfile_path.read_text())["last_heartbeat"]
            time.sleep(0.25)  # ~5 heartbeats
            updated = json.loads(lock.lockfile_path.read_text())["last_heartbeat"]
            assert updated > initial

    def test_heartbeat_thread_is_daemon(self, lock_root):
        lock = SessionLock("bot/_impl.py")
        with lock.acquire():
            # Must be daemon so a dying main thread doesn't keep the
            # process alive holding the lock.
            assert lock._heartbeat_thread is not None
            assert lock._heartbeat_thread.daemon is True

    def test_heartbeat_thread_stops_after_release(self, lock_root, monkeypatch):
        monkeypatch.setattr(_session_lock, "HEARTBEAT_INTERVAL_S", 0.05)
        lock = SessionLock("bot/_impl.py")
        with lock.acquire():
            t = lock._heartbeat_thread
        # Thread should join soon after release.
        t.join(timeout=1.0)
        assert not t.is_alive()


# --------------------------------------------------------------------------
# Concurrent acquire
# --------------------------------------------------------------------------


def _child_acquire_target(root_path: str, reclaim_log_path: str, target: str, queue):
    """Run in a subprocess; try to acquire and report what happened."""
    os.environ["KALSHI_SESSION_LOCK_ROOT"] = root_path
    os.environ["KALSHI_SESSION_LOCK_RECLAIM_LOG"] = reclaim_log_path
    # Re-import in the child so the env vars take effect.
    from scripts import _session_lock as child_mod
    from scripts._session_lock import LockHeldError as ChildLockHeldError
    from scripts._session_lock import SessionLock as ChildSessionLock

    # Force module to re-resolve overrides from env (test env uses
    # module-attribute overrides, but the subprocess reads from env vars).
    child_mod._LOCK_ROOT_OVERRIDE = None
    child_mod._RECLAIM_LOG_OVERRIDE = None

    try:
        lock = ChildSessionLock(target)
        lock.acquire_raw()
        queue.put(("acquired", lock.metadata))
        time.sleep(2.0)
        lock.release()
    except ChildLockHeldError as e:
        queue.put(("held", e.held_by))
    except Exception as e:  # pragma: no cover - defensive
        queue.put(("error", repr(e)))


class TestConcurrentAcquire:
    def test_second_acquire_same_target_raises_lock_held_error(self, lock_root):
        a = SessionLock("bot/_impl.py")
        b = SessionLock("bot/_impl.py")
        with a.acquire():
            with pytest.raises(LockHeldError) as ei:
                b.acquire_raw()
            assert ei.value.held_by["pid"] == os.getpid()
            assert ei.value.held_by["target_path"] == "bot/_impl.py"
            assert "session_id" in ei.value.held_by

    def test_different_targets_dont_collide(self, lock_root):
        a = SessionLock("bot/_impl.py")
        b = SessionLock("bot/state.py")
        with a.acquire(), b.acquire():
            assert a.lockfile_path.exists()
            assert b.lockfile_path.exists()
            assert a.lockfile_path != b.lockfile_path

    def test_concurrent_subprocess_acquire_blocks(self, lock_root, tmp_path):
        # Two child processes race for the same target. Second must see
        # LockHeldError with metadata.
        target = "bot/_impl.py"
        ctx = multiprocessing.get_context("spawn")
        q: multiprocessing.Queue = ctx.Queue()

        p1 = ctx.Process(
            target=_child_acquire_target,
            args=(str(lock_root), str(lock_root.parent / "reclaim.log"), target, q),
        )
        p1.start()
        # Give p1 a head start so it wins the race deterministically.
        time.sleep(0.5)

        p2 = ctx.Process(
            target=_child_acquire_target,
            args=(str(lock_root), str(lock_root.parent / "reclaim.log"), target, q),
        )
        p2.start()

        results = []
        for _ in range(2):
            results.append(q.get(timeout=10.0))

        p1.join(timeout=10.0)
        p2.join(timeout=10.0)

        kinds = sorted(r[0] for r in results)
        assert kinds == ["acquired", "held"]
        held_meta = next(r[1] for r in results if r[0] == "held")
        assert held_meta["target_path"] == target
        assert "pid" in held_meta


# --------------------------------------------------------------------------
# Stale-reclaim
# --------------------------------------------------------------------------


class TestStaleReclaim:
    def test_lock_within_threshold_is_not_stale(self, lock_root):
        a = SessionLock("bot/_impl.py")
        a.acquire_raw()
        try:
            assert _session_lock.is_stale(a.lockfile_path) is False
        finally:
            a.release()

    def test_lock_past_threshold_is_stale(self, lock_root, monkeypatch):
        a = SessionLock("bot/_impl.py")
        a.acquire_raw()
        try:
            # Backdate last_heartbeat past the 180s threshold.
            data = json.loads(a.lockfile_path.read_text())
            data["last_heartbeat"] = time.time() - 200.0
            a.lockfile_path.write_text(json.dumps(data))
            assert _session_lock.is_stale(a.lockfile_path) is True
        finally:
            # Stop the heartbeat thread to avoid races on file removal.
            a._stop_heartbeat()
            try:
                a.lockfile_path.unlink()
            except FileNotFoundError:
                pass

    def test_stale_lock_can_be_reclaimed(self, lock_root, monkeypatch):
        # Simulate an orphaned lockfile (no heartbeat thread).
        target = "bot/_impl.py"
        flat = flatten_target_path(target)
        path = lock_root / f"{flat}.lock"
        stale_meta = {
            "pid": 999999,
            "session_id": "dead-session",
            "target_path": target,
            "started_at": time.time() - 500,
            "last_heartbeat": time.time() - 300,
            "claude_session_marker": "stale-orphan",
        }
        path.write_text(json.dumps(stale_meta))

        # Now a fresh acquire should reclaim and succeed.
        b = SessionLock(target)
        with b.acquire():
            data = json.loads(b.lockfile_path.read_text())
            assert data["pid"] == os.getpid()
            assert data["session_id"] != "dead-session"

        # Reclaim audit log should mention the dead session.
        reclaim_log = Path(os.environ["KALSHI_SESSION_LOCK_RECLAIM_LOG"])
        assert reclaim_log.exists()
        content = reclaim_log.read_text()
        assert "dead-session" in content

    def test_fresh_lock_blocks_acquire_even_with_dead_pid(self, lock_root):
        # Even if the holding PID does not exist, the lock is honored
        # while heartbeat is fresh (≤180s). PID liveness is not the
        # gate — heartbeat age is.
        target = "bot/_impl.py"
        flat = flatten_target_path(target)
        path = lock_root / f"{flat}.lock"
        fresh_meta = {
            "pid": 999998,
            "session_id": "fresh-orphan",
            "target_path": target,
            "started_at": time.time() - 5,
            "last_heartbeat": time.time() - 1,
            "claude_session_marker": "fresh",
        }
        path.write_text(json.dumps(fresh_meta))

        with pytest.raises(LockHeldError):
            SessionLock(target).acquire_raw()


# --------------------------------------------------------------------------
# Process death cleanup
# --------------------------------------------------------------------------


def _child_acquire_then_die(root_path: str, reclaim_log_path: str, target: str):
    os.environ["KALSHI_SESSION_LOCK_ROOT"] = root_path
    os.environ["KALSHI_SESSION_LOCK_RECLAIM_LOG"] = reclaim_log_path
    from scripts import _session_lock as child_mod

    child_mod._LOCK_ROOT_OVERRIDE = None
    child_mod._RECLAIM_LOG_OVERRIDE = None
    from scripts._session_lock import SessionLock as ChildSessionLock

    lock = ChildSessionLock(target)
    lock.acquire_raw()
    # Stop the heartbeat thread first so it can't race our backdate.
    lock._heartbeat_stop.set()
    # Backdate heartbeat so parent sees a stale lock immediately.
    data = json.loads(lock.lockfile_path.read_text())
    data["last_heartbeat"] = time.time() - 500
    lock.lockfile_path.write_text(json.dumps(data))
    # Die without releasing — simulate kill -9.
    os._exit(0)


class TestProcessDeathCleanup:
    def test_lockfile_persists_after_kill_then_reclaim_recovers(
        self, lock_root, tmp_path
    ):
        target = "bot/_impl.py"
        ctx = multiprocessing.get_context("spawn")
        p = ctx.Process(
            target=_child_acquire_then_die,
            args=(str(lock_root), str(lock_root.parent / "reclaim.log"), target),
        )
        p.start()
        p.join(timeout=10.0)
        assert p.exitcode == 0

        # Lockfile remains on disk even though child is dead.
        flat = flatten_target_path(target)
        leftover = lock_root / f"{flat}.lock"
        assert leftover.exists()

        # Stale-reclaim recovers it.
        new_lock = SessionLock(target)
        with new_lock.acquire():
            data = json.loads(new_lock.lockfile_path.read_text())
            assert data["pid"] == os.getpid()


# --------------------------------------------------------------------------
# Malformed lockfile quarantine
# --------------------------------------------------------------------------


class TestMalformedLockfile:
    def test_corrupt_json_is_quarantined_and_acquire_succeeds(self, lock_root):
        target = "bot/_impl.py"
        flat = flatten_target_path(target)
        bad = lock_root / f"{flat}.lock"
        bad.write_text("not-json{{{")

        # Acquire should treat malformed lockfile as quarantine candidate
        # and succeed (mclaude semantics: a malformed lockfile is no
        # different from a stale one — its provenance is unknowable).
        lock = SessionLock(target)
        with lock.acquire():
            assert lock.lockfile_path.exists()
            data = json.loads(lock.lockfile_path.read_text())
            assert data["pid"] == os.getpid()

        # Quarantine sidecar must be written.
        quarantines = list(lock_root.glob("*.quarantine"))
        assert len(quarantines) == 1
        # Reclaim log records the quarantine.
        reclaim_log = Path(os.environ["KALSHI_SESSION_LOCK_RECLAIM_LOG"])
        assert reclaim_log.exists()
        assert "QUARANTINE" in reclaim_log.read_text()

    def test_missing_required_keys_treated_as_malformed(self, lock_root):
        target = "bot/_impl.py"
        flat = flatten_target_path(target)
        bad = lock_root / f"{flat}.lock"
        # Valid JSON but missing required keys.
        bad.write_text(json.dumps({"pid": 1}))

        lock = SessionLock(target)
        with lock.acquire():
            assert lock.lockfile_path.exists()
        quarantines = list(lock_root.glob("*.quarantine"))
        assert len(quarantines) == 1


# --------------------------------------------------------------------------
# iCloud-suffix robustness
# --------------------------------------------------------------------------


class TestICloudSuffix:
    def test_icloud_collision_sibling_is_ignored(self, lock_root):
        # iCloud sometimes creates "foo 2.lock" siblings of "foo.lock".
        # We must not honor a "* 2.lock" as if it were the canonical
        # lock for the target path.
        target = "bot/_impl.py"
        flat = flatten_target_path(target)
        canonical = lock_root / f"{flat}.lock"
        sibling = lock_root / f"{flat} 2.lock"
        # Drop a *fresh* sibling to test the worst case (would otherwise
        # block acquires forever).
        sibling.write_text(
            json.dumps(
                {
                    "pid": 999997,
                    "session_id": "icloud-ghost",
                    "target_path": target,
                    "started_at": time.time(),
                    "last_heartbeat": time.time(),
                    "claude_session_marker": "icloud",
                }
            )
        )
        # Canonical path is free; acquire must succeed without consulting
        # the sibling.
        lock = SessionLock(target)
        with lock.acquire():
            assert canonical.exists()
            # Sibling is left untouched; we don't try to clean iCloud's
            # mess, just refuse to be confused by it.
            assert sibling.exists()


# --------------------------------------------------------------------------
# Stress: many acquire/release cycles
# --------------------------------------------------------------------------


class TestStress:
    def test_repeated_acquire_release_does_not_leak(self, lock_root):
        target = "bot/_impl.py"
        for _ in range(20):
            with SessionLock(target).acquire():
                pass
        # After all releases, no lockfile remains.
        leftovers = list(lock_root.glob("*.lock"))
        assert leftovers == []

    def test_thread_concurrent_acquire_serializes(self, lock_root):
        # Threads in the same process MUST also be serialized — the
        # primitive's job is target-level mutual exclusion regardless
        # of caller topology.
        target = "bot/_impl.py"
        successes = []
        held_errors = []

        def worker():
            try:
                lock = SessionLock(target)
                lock.acquire_raw()
                successes.append(threading.get_ident())
                time.sleep(0.1)
                lock.release()
            except LockHeldError:
                held_errors.append(threading.get_ident())

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        # At least one thread succeeded; the rest hit LockHeldError.
        assert len(successes) >= 1
        assert len(successes) + len(held_errors) == 5
