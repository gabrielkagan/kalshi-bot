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

    def test_slash_marker_literal_in_input_rejected(self):
        # R1 M1 — injectivity. flatten("a__SLASH__b/c") and flatten("a/b/c")
        # would otherwise both produce 'a__SLASH__b__SLASH__c'.
        with pytest.raises(ValueError):
            flatten_target_path("a__SLASH__b/c")
        with pytest.raises(ValueError):
            flatten_target_path("a__SLASH__b")
        # Sanity: the legitimate path containing real slashes still works
        # and is the only encoding for that basename.
        assert flatten_target_path("a/b/c") == "a__SLASH__b__SLASH__c"

    @pytest.mark.parametrize(
        "bad_input",
        [
            # R4 M1 — the canonical collision pair the reviewer demonstrated:
            # both inputs flatten to 'a__SLASH__SLASH__b' under the R1-only
            # guard. After the R4 fix both must raise.
            "a__SLASH/b",
            "a/SLASH__b",
            # Component ENDING in __SLASH (the trailing piece reproduces
            # _SLASH_MARKER once joined with the next `__SLASH__`).
            "foo__SLASH/bar.py",
            "bot__SLASH/_impl.py",
            # Component STARTING with SLASH__ (mirror image).
            "foo/SLASH__bar.py",
            "bot/SLASH__scanner",
            # Multi-component path with the boundary mid-way.
            "a/b__SLASH/c",
            "a/b/SLASH__c",
        ],
    )
    def test_partial_slash_marker_components_rejected(self, bad_input):
        # R4 M1 — boundary-straddling injectivity. Without this guard the
        # join would reconstruct `_SLASH_MARKER` across a `/` boundary and
        # collide with a legitimate path containing real slashes.
        with pytest.raises(ValueError, match="straddles slash-marker"):
            flatten_target_path(bad_input)

    def test_partial_slash_marker_demonstrated_collision_is_blocked(self):
        # R4 M1 — explicit collision pair. The R3-era code accepted BOTH
        # inputs and produced identical flat names; here we assert BOTH
        # raise (so the collision cannot be silently constructed).
        with pytest.raises(ValueError):
            flatten_target_path("a__SLASH/b")
        with pytest.raises(ValueError):
            flatten_target_path("a/SLASH__b")
        # Sanity: a benign sibling that does NOT touch the marker boundary
        # still flattens normally. (`SLASH__` mid-component is fine — only
        # leading `SLASH__` or trailing `__SLASH` are dangerous.)
        assert flatten_target_path("a/bSLASH__c") == "a__SLASH__bSLASH__c"
        assert flatten_target_path("a/b__SLASHc") == "a__SLASH__b__SLASHc"

    def test_flatten_unflatten_property_random(self):
        # R4 — property-style defense-in-depth. Generate a small space of
        # candidate components mixing benign chars, the SLASH__ / __SLASH
        # adversaries, and the literal marker. For every multi-component
        # path built from these, `flatten` must either (a) raise ValueError
        # or (b) round-trip identity through `unflatten`. Any silent
        # non-bijective output is a regression.
        import itertools
        import random

        rng = random.Random(20260510)  # deterministic
        components = [
            "a",
            "bot",
            "_impl.py",
            "scanner",
            "SLASH__x",       # adversary: starts with SLASH__
            "y__SLASH",       # adversary: ends with __SLASH
            "z__SLASH__w",    # adversary: contains the contiguous marker
            "SLASH__only",    # full leading-marker
            "only__SLASH",    # full trailing-marker
            "harmlessSLASH__inside",   # SLASH__ mid-component (benign)
            "harmless__SLASHinside",   # __SLASH mid-component (benign)
        ]
        # Build ~20 random multi-component paths of length 2-4.
        trials = 0
        bijection_holds = 0
        raised = 0
        for _ in range(20):
            n = rng.randint(2, 4)
            picked = [rng.choice(components) for _ in range(n)]
            target = "/".join(picked)
            trials += 1
            try:
                flat = flatten_target_path(target)
            except ValueError:
                raised += 1
                continue
            unflat = unflatten_target_path(flat)
            # If flatten accepted it, the round-trip MUST be identity.
            assert unflat == target, (
                f"Non-bijective: input={target!r} flat={flat!r} "
                f"unflat={unflat!r}"
            )
            bijection_holds += 1
        # Sanity-check the probe space did something — at least one of each
        # outcome should occur, otherwise the property test is vacuous.
        assert raised >= 1, "property test never exercised the rejection path"
        assert bijection_holds >= 1, "property test never exercised the accept path"
        # And we ran the full sweep — no early return.
        assert raised + bijection_holds == trials

        # Also exhaustively check the 4 minimal adversarial pairs caught
        # by the R4 finding (no randomness — these are the witnesses).
        adversarial_pairs = list(itertools.product(
            ["a", "bot__SLASH", "SLASH__bot"],
            ["b", "SLASH__b", "b__SLASH"],
        ))
        for left, right in adversarial_pairs:
            target = f"{left}/{right}"
            try:
                flat = flatten_target_path(target)
            except ValueError:
                continue
            # Accepted → must round-trip.
            assert unflatten_target_path(flat) == target, (
                f"Adversarial-pair non-bijective: target={target!r} flat={flat!r}"
            )

    def test_nul_byte_in_path_rejected(self):
        # R1 M2 — NUL byte would crash inside os.open with
        # ValueError: embedded null byte. Caller would see uncaught crash.
        with pytest.raises(ValueError):
            flatten_target_path("foo/\x00bar.py")

    def test_control_char_in_path_rejected(self):
        # R1 M2 — broader: any ASCII control char is rejected (un-greppable
        # lockfile names + potential os-layer surprises).
        for ch in ("\x01", "\x07", "\n", "\t", "\x1f", "\x7f"):
            with pytest.raises(ValueError):
                flatten_target_path(f"foo/{ch}bar.py")


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


def _child_acquire_barriered(
    root_path: str,
    reclaim_log_path: str,
    target: str,
    barrier,
    queue,
):
    """R1 M3 — barriered race child.

    Both children rendezvous on the multiprocessing.Barrier so that the
    two `acquire_raw()` calls happen within microseconds of each other,
    putting actual contention pressure on the lockfile create.
    """
    os.environ["KALSHI_SESSION_LOCK_ROOT"] = root_path
    os.environ["KALSHI_SESSION_LOCK_RECLAIM_LOG"] = reclaim_log_path
    from scripts import _session_lock as child_mod
    from scripts._session_lock import LockHeldError as ChildLockHeldError
    from scripts._session_lock import SessionLock as ChildSessionLock

    child_mod._LOCK_ROOT_OVERRIDE = None
    child_mod._RECLAIM_LOG_OVERRIDE = None

    # Rendezvous BEFORE acquire so both call sites fire near-simultaneously.
    try:
        barrier.wait(timeout=10.0)
    except Exception as e:  # pragma: no cover
        queue.put(("error", f"barrier: {e!r}"))
        return

    try:
        lock = ChildSessionLock(target)
        lock.acquire_raw()
        # Hold briefly so the loser definitely observes us as live.
        queue.put(("acquired", lock.metadata))
        time.sleep(0.3)
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
        # R1 M3 — REAL race. Both children rendezvous on a Barrier and call
        # acquire_raw() within microseconds of each other; over N trials,
        # we must see exactly one "acquired" + one "held" (or in rare
        # contention "held"+"held" if the canonical lockfile vanishes
        # between attempts — but NEVER two "acquired"). Multiple trials
        # to apply pressure on the TOCTOU window the previous "500ms head
        # start" version slept right through.
        target = "bot/_impl.py"
        ctx = multiprocessing.get_context("spawn")

        N_TRIALS = 20
        for trial in range(N_TRIALS):
            q: multiprocessing.Queue = ctx.Queue()
            barrier = ctx.Barrier(2)

            p1 = ctx.Process(
                target=_child_acquire_barriered,
                args=(
                    str(lock_root),
                    str(lock_root.parent / "reclaim.log"),
                    target,
                    barrier,
                    q,
                ),
            )
            p2 = ctx.Process(
                target=_child_acquire_barriered,
                args=(
                    str(lock_root),
                    str(lock_root.parent / "reclaim.log"),
                    target,
                    barrier,
                    q,
                ),
            )
            p1.start()
            p2.start()

            results = []
            for _ in range(2):
                results.append(q.get(timeout=10.0))

            p1.join(timeout=10.0)
            p2.join(timeout=10.0)

            kinds = [r[0] for r in results]
            n_acquired = kinds.count("acquired")
            # CRITICAL invariant: NEVER two acquired. That would mean both
            # children believed they held the lock — the C1 TOCTOU bug.
            assert n_acquired <= 1, (
                f"trial {trial}: both children acquired the same lock "
                f"(C1 TOCTOU regression): results={results}"
            )
            # And at least one must succeed each trial — otherwise the
            # primitive is just rejecting both, which is also broken.
            assert n_acquired == 1, (
                f"trial {trial}: neither child acquired; results={results}"
            )

            # Clean up any leftover lockfile between trials.
            flat = flatten_target_path(target)
            leftover = lock_root / f"{flat}.lock"
            if leftover.exists():
                leftover.unlink()


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
    """R1 M5 — honest kill -9 simulation.

    Previous version backdated the heartbeat timestamp before os._exit(0),
    which cheated the 180s stale-window contract. The fix uses
    monkeypatched STALE_THRESHOLD_S in the parent + a real sleep so the
    full reclaim path is exercised at scaled-down clock.

    The child here just acquires + dies without releasing, with the
    heartbeat thread stopped so it can't tick after we exit.
    """
    os.environ["KALSHI_SESSION_LOCK_ROOT"] = root_path
    os.environ["KALSHI_SESSION_LOCK_RECLAIM_LOG"] = reclaim_log_path
    from scripts import _session_lock as child_mod

    child_mod._LOCK_ROOT_OVERRIDE = None
    child_mod._RECLAIM_LOG_OVERRIDE = None
    from scripts._session_lock import SessionLock as ChildSessionLock

    lock = ChildSessionLock(target)
    lock.acquire_raw()
    # Stop the heartbeat thread so it can't tick after we exit (the
    # daemon thread WOULD die with the process; we belt-and-braces it).
    lock._heartbeat_stop.set()
    # Die without releasing — simulate kill -9. Lockfile has REAL fresh
    # last_heartbeat, just like a kill -9 victim. No timestamp cheating.
    os._exit(0)


class TestProcessDeathCleanup:
    def test_lockfile_persists_after_kill_then_reclaim_recovers(
        self, lock_root, tmp_path, monkeypatch
    ):
        # R1 M5 — exercise the real "stale threshold elapsed" path without
        # cheating the timestamp. Scale STALE_THRESHOLD_S down to 0.5s and
        # sleep 1.0s after the child exits.
        monkeypatch.setattr(_session_lock, "STALE_THRESHOLD_S", 0.5)
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

        # BEFORE the scaled stale-window elapses, the lock blocks acquire.
        # (The child's REAL fresh heartbeat is still within threshold.)
        with pytest.raises(LockHeldError):
            SessionLock(target).acquire_raw()

        # Sleep past the scaled-down stale window.
        time.sleep(1.0)

        # Now stale-reclaim engages and a new acquire succeeds.
        new_lock = SessionLock(target)
        with new_lock.acquire():
            data = json.loads(new_lock.lockfile_path.read_text())
            assert data["pid"] == os.getpid()

        # Reclaim audit log records a RECLAIM_STALE entry.
        reclaim_log = Path(os.environ["KALSHI_SESSION_LOCK_RECLAIM_LOG"])
        assert reclaim_log.exists()
        assert "RECLAIM_STALE" in reclaim_log.read_text()


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
# R1 C1 — TOCTOU between O_CREAT|O_EXCL and payload write
# --------------------------------------------------------------------------


class TestTOCTOU:
    """R1 C1 — empirical regression. Previously the lockfile was created
    via O_CREAT|O_EXCL on the canonical name, then payload was written
    in a second syscall. Peers observing the empty file between the two
    syscalls parsed it as "malformed" and quarantined it out from under
    the writer, then both sides won O_EXCL on their retry. With the
    write-tmp + os.link(tmp, canonical) fix, a peer NEVER sees a partial
    file at the canonical path.
    """

    def test_no_partial_lockfile_observable_at_canonical_path(self, lock_root):
        # Critical C1 invariant: when the canonical lockfile EXISTS, it
        # must be fully written + parseable. The pre-fix bug was a
        # window between `os.open(O_CREAT|O_EXCL)` and `os.write(payload)`
        # during which the canonical file existed as a 0-byte empty file.
        # A peer in that window would parse it as malformed and quarantine
        # it out from under the writer, then both sessions acquire O_EXCL
        # on retry.
        #
        # Test strategy: a single writer holds the lock for HOLD_S
        # seconds; in parallel, a reader thread continuously snapshots
        # the canonical lockfile bytes. EVERY snapshot read while the
        # writer is in the "holding" phase must be a fully-formed
        # parseable JSON blob with our expected `session_id`. Empty
        # files or stale parses indicate the C1 partial-write window.
        target = "bot/_impl.py"
        flat = flatten_target_path(target)
        canonical = lock_root / f"{flat}.lock"

        from scripts._session_lock import _read_lockfile_metadata

        N_TRIALS = 50
        bad: list[str] = []

        for trial in range(N_TRIALS):
            holder = SessionLock(target)
            holder_session_id = holder._session_id
            holder.acquire_raw()
            # During the "holding" phase the canonical file must always
            # parse cleanly. We snapshot it many times in a tight loop.
            try:
                for _ in range(200):
                    # The reader sees the file in a stable state since
                    # acquire_raw has returned (post-atomic-publish).
                    try:
                        raw = canonical.read_bytes()
                    except FileNotFoundError:
                        bad.append(f"trial {trial}: canonical vanished while held")
                        break
                    if not raw:
                        bad.append(
                            f"trial {trial}: canonical observed empty while held "
                            "(C1 partial-write regression)"
                        )
                        break
                    meta = _read_lockfile_metadata(canonical)
                    if meta is None:
                        bad.append(
                            f"trial {trial}: canonical malformed while held"
                        )
                        break
                    if meta.get("session_id") != holder_session_id:
                        bad.append(
                            f"trial {trial}: canonical owned by "
                            f"{meta.get('session_id')!r}, not {holder_session_id!r}"
                        )
                        break
            finally:
                holder.release()

        assert bad == [], (
            f"R1 C1 invariant violated {len(bad)} times: {bad[:3]}"
        )


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
        # R1 M4 — REAL serialization test. Previous version's assertion
        # `successes >= 1 and successes + held_errors == 5` would pass
        # if all 5 threads acquired sequentially (5 successes, 0 errors)
        # — i.e. NO actual race happened, threads each waited their turn.
        # That's not what this test is supposed to prove.
        #
        # Fix: gate threads on a Barrier so they all attempt acquire_raw
        # within microseconds of each other; the winner sleeps long enough
        # that the losers' attempts overlap; assert EXACTLY ONE success
        # per trial. Run N trials.
        target = "bot/_impl.py"
        N_THREADS = 5
        N_TRIALS = 10

        for trial in range(N_TRIALS):
            successes: list[int] = []
            held_errors: list[int] = []
            errors: list[str] = []
            barrier = threading.Barrier(N_THREADS)

            def worker():
                try:
                    barrier.wait(timeout=5.0)
                except threading.BrokenBarrierError:
                    errors.append("barrier-broken")
                    return
                try:
                    lock = SessionLock(target)
                    lock.acquire_raw()
                    successes.append(threading.get_ident())
                    # Hold for >> the contention window so other threads'
                    # acquire_raw attempts overlap our held window.
                    time.sleep(0.15)
                    lock.release()
                except LockHeldError:
                    held_errors.append(threading.get_ident())
                except Exception as e:  # pragma: no cover - defensive
                    errors.append(repr(e))

            threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)

            assert errors == [], f"trial {trial}: errors={errors}"
            # EXACTLY one success — the winner holds for 150ms while the
            # losers' fast-path retries all bail to LockHeldError.
            assert len(successes) == 1, (
                f"trial {trial}: expected 1 success (mutual exclusion), "
                f"got {len(successes)}; held={len(held_errors)}; "
                f"successes={successes}"
            )
            assert len(successes) + len(held_errors) == N_THREADS


# --------------------------------------------------------------------------
# R1 C2 — worktree-aware repo-root resolution
# --------------------------------------------------------------------------


class TestWorktreeRepoRoot:
    """R1 C2 — without a worktree-aware _repo_root(), a Claude session
    inside `.claude/worktrees/<name>/` resolves a DIFFERENT lock root
    than a session in the main checkout. They never collide on the
    canonical lockfile, defeating the entire purpose of the primitive
    (Phase B uses 5 worktrees + main session topology).
    """

    def test_worktree_pointer_resolves_to_common_repo_root(
        self, tmp_path, monkeypatch
    ):
        # Construct a synthetic main-repo + worktree layout. The "main"
        # has `.git/` as a directory; the "worktree" has `.git` as a
        # POINTER FILE containing `gitdir: <main>/.git/worktrees/<name>/`.
        # Both `_repo_root()` invocations (one from each layout) must
        # resolve to `main_repo`.
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()
        (main_repo / ".git").mkdir()
        (main_repo / "scripts").mkdir()
        (main_repo / ".git" / "worktrees").mkdir()
        worktree_gitdir = main_repo / ".git" / "worktrees" / "wt1"
        worktree_gitdir.mkdir()

        worktree = main_repo / ".claude" / "worktrees" / "wt1"
        worktree.mkdir(parents=True)
        (worktree / "scripts").mkdir()
        # The worktree pointer file (this is the EXACT format git writes).
        (worktree / ".git").write_text(
            f"gitdir: {worktree_gitdir}\n", encoding="utf-8"
        )

        # Stub `Path(__file__).resolve().parent` for each call by directly
        # exercising _repo_root via monkeypatched module-attr.
        import scripts._session_lock as mod

        # Save the original module __file__ to restore.
        orig_file = mod.__file__

        try:
            # Pretend we're running from main_repo/scripts/_session_lock.py.
            mod.__file__ = str(main_repo / "scripts" / "_session_lock.py")
            main_root = mod._repo_root()

            # Pretend we're running from worktree/scripts/_session_lock.py.
            mod.__file__ = str(worktree / "scripts" / "_session_lock.py")
            worktree_root = mod._repo_root()
        finally:
            mod.__file__ = orig_file

        # Both must resolve to the SAME path: main_repo.
        assert main_root == main_repo, f"main: {main_root}"
        assert worktree_root == main_repo, (
            f"worktree resolved to {worktree_root}, expected {main_repo}"
        )

    def test_worktree_lockfile_path_collides_with_main_lockfile_path(
        self, tmp_path, monkeypatch
    ):
        """End-to-end: SessionLock("bot/_impl.py") from a worktree must
        produce the SAME canonical lockfile path as from the main repo.
        This is the contract that makes the primitive useful for our
        actual topology.
        """
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()
        (main_repo / ".git").mkdir()
        (main_repo / "scripts").mkdir()
        (main_repo / ".git" / "worktrees").mkdir()
        worktree_gitdir = main_repo / ".git" / "worktrees" / "wt1"
        worktree_gitdir.mkdir()

        worktree = main_repo / ".claude" / "worktrees" / "wt1"
        worktree.mkdir(parents=True)
        (worktree / "scripts").mkdir()
        (worktree / ".git").write_text(
            f"gitdir: {worktree_gitdir}\n", encoding="utf-8"
        )

        import scripts._session_lock as mod

        # Clear env + module overrides so _lock_root() falls back to
        # _repo_root() / .claude / locks / active-work.
        monkeypatch.delenv("KALSHI_SESSION_LOCK_ROOT", raising=False)
        monkeypatch.delenv("KALSHI_SESSION_LOCK_RECLAIM_LOG", raising=False)
        monkeypatch.setattr(mod, "_LOCK_ROOT_OVERRIDE", None)
        monkeypatch.setattr(mod, "_RECLAIM_LOG_OVERRIDE", None)

        orig_file = mod.__file__
        try:
            mod.__file__ = str(main_repo / "scripts" / "_session_lock.py")
            main_lock_path = mod.SessionLock("bot/_impl.py").lockfile_path

            mod.__file__ = str(worktree / "scripts" / "_session_lock.py")
            wt_lock_path = mod.SessionLock("bot/_impl.py").lockfile_path
        finally:
            mod.__file__ = orig_file

        assert main_lock_path == wt_lock_path, (
            f"worktree + main must collide on the same lockfile path; "
            f"main={main_lock_path}, wt={wt_lock_path}"
        )
        # And specifically, the lock root is rooted under main_repo.
        assert str(main_repo) in str(main_lock_path)

    def test_main_checkout_dotgit_dir_returns_containing_root(
        self, tmp_path, monkeypatch
    ):
        # When `.git` is a directory (main checkout), `_repo_root()`
        # returns the directory containing it. Sanity check that the
        # new is_dir() / is_file() branching doesn't regress this.
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()
        (main_repo / ".git").mkdir()
        (main_repo / "scripts").mkdir()

        import scripts._session_lock as mod

        orig_file = mod.__file__
        try:
            mod.__file__ = str(main_repo / "scripts" / "_session_lock.py")
            root = mod._repo_root()
        finally:
            mod.__file__ = orig_file

        assert root == main_repo

    def test_relative_gitdir_pointer_resolves_to_common_repo_root(
        self, tmp_path, monkeypatch
    ):
        """R2 M7 — git 2.48+ supports
        `git config --global worktree.useRelativePaths true`, which makes
        `git worktree add` write `gitdir: ../../.git/worktrees/<name>`
        (relative, not absolute). Per git-worktree(1): "if gitdir is a
        relative path, it is relative to the location of the worktree's
        .git file."

        Pre-fix bug: `Path('../../.git/worktrees/wt1').parents[1].parent`
        = `Path('../..')`, then `.exists()` happens to succeed against
        CWD (returning whatever lives two levels above CWD), silently
        producing a WRONG-but-existent lock-root. Main session and
        worktree session resolve to DIFFERENT lock roots → never collide
        → exactly the C2 bug rebadged.

        Fix: resolve relative gitdir against `git_entry.parent` (the
        worktree's .git file's directory) — per the documented git
        contract. Both layouts then resolve to the same canonical
        `main_repo`.
        """
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()
        (main_repo / ".git").mkdir()
        (main_repo / "scripts").mkdir()
        (main_repo / ".git" / "worktrees").mkdir()
        worktree_gitdir = main_repo / ".git" / "worktrees" / "wt1"
        worktree_gitdir.mkdir()

        worktree = main_repo / ".claude" / "worktrees" / "wt1"
        worktree.mkdir(parents=True)
        (worktree / "scripts").mkdir()

        # Compute the RELATIVE pointer the way git 2.48+ writes it:
        # relative-from the worktree's .git file's parent directory
        # (which is `worktree/`) to the main repo's gitdir-worktree
        # subdir.
        # Worktree lives at `<main>/.claude/worktrees/wt1`; its `.git`
        # file's parent dir is `worktree/` (a sibling of `scripts/`).
        # Climbing back to `<main>` takes 3 `..` (out of wt1, out of
        # worktrees, out of .claude), then descending into the gitdir.
        rel = Path("../../../.git/worktrees/wt1")
        # Sanity: confirm `worktree/<rel>` actually points at
        # worktree_gitdir on disk.
        assert (worktree / rel).resolve() == worktree_gitdir.resolve()

        # Write the pointer file with the RELATIVE form.
        (worktree / ".git").write_text(
            f"gitdir: {rel}\n", encoding="utf-8"
        )

        import scripts._session_lock as mod

        orig_file = mod.__file__
        try:
            mod.__file__ = str(main_repo / "scripts" / "_session_lock.py")
            main_root = mod._repo_root()

            mod.__file__ = str(worktree / "scripts" / "_session_lock.py")
            worktree_root = mod._repo_root()
        finally:
            mod.__file__ = orig_file

        # Both must resolve to the SAME canonical path. The .resolve()
        # is necessary because _repo_root() now calls it for the
        # relative branch; the absolute branch (main) returns un-resolved
        # but in this synthetic layout main_repo == main_repo.resolve()
        # (tmp_path is already absolute & resolved on macOS/Linux).
        assert main_root.resolve() == main_repo.resolve()
        assert worktree_root.resolve() == main_repo.resolve(), (
            f"relative-gitdir worktree resolved to {worktree_root}, "
            f"expected {main_repo} — M7 regression."
        )

    def test_relative_gitdir_lockfile_path_collides_with_main(
        self, tmp_path, monkeypatch
    ):
        """R2 M7 end-to-end — with a relative gitdir pointer (git 2.48+
        `worktree.useRelativePaths=true`), the worktree's
        `SessionLock("bot/_impl.py").lockfile_path` must match the
        main-checkout one. This is the contract that makes the primitive
        useful in deployments that opt into relative worktree paths.
        """
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()
        (main_repo / ".git").mkdir()
        (main_repo / "scripts").mkdir()
        (main_repo / ".git" / "worktrees").mkdir()
        worktree_gitdir = main_repo / ".git" / "worktrees" / "wt1"
        worktree_gitdir.mkdir()

        worktree = main_repo / ".claude" / "worktrees" / "wt1"
        worktree.mkdir(parents=True)
        (worktree / "scripts").mkdir()
        # See sister test's rel-path comment — 3 `..` to climb out of
        # `.claude/worktrees/wt1` back to main_repo.
        (worktree / ".git").write_text(
            "gitdir: ../../../.git/worktrees/wt1\n", encoding="utf-8"
        )

        import scripts._session_lock as mod

        # Clear env + module overrides so _lock_root() falls back to
        # _repo_root() / .claude / locks / active-work.
        monkeypatch.delenv("KALSHI_SESSION_LOCK_ROOT", raising=False)
        monkeypatch.delenv("KALSHI_SESSION_LOCK_RECLAIM_LOG", raising=False)
        monkeypatch.setattr(mod, "_LOCK_ROOT_OVERRIDE", None)
        monkeypatch.setattr(mod, "_RECLAIM_LOG_OVERRIDE", None)

        orig_file = mod.__file__
        try:
            mod.__file__ = str(main_repo / "scripts" / "_session_lock.py")
            main_lock_path = mod.SessionLock("bot/_impl.py").lockfile_path

            mod.__file__ = str(worktree / "scripts" / "_session_lock.py")
            wt_lock_path = mod.SessionLock("bot/_impl.py").lockfile_path
        finally:
            mod.__file__ = orig_file

        # Both lock paths must resolve to the same canonical path on
        # disk. Different absolute representations of the same path
        # (one via main, one via worktree-relative + resolve) would
        # still create the same lockfile inode, but we want exact
        # equality so the in-memory `.lockfile_path == .lockfile_path`
        # check used by sister hooks (P5.2/P5.3) never spuriously
        # reports a divergence.
        assert main_lock_path.resolve() == wt_lock_path.resolve(), (
            f"relative-gitdir worktree + main must collide on the same "
            f"lockfile path; main={main_lock_path}, wt={wt_lock_path}"
        )
        # And the lock root is rooted under main_repo, not under the
        # worktree's filesystem subtree.
        assert str(main_repo.resolve()) in str(main_lock_path.resolve())
