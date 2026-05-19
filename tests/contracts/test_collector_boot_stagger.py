"""D1.3-fu4-boot-stagger — boot-time `archiver.start()` stagger contract
(post-PR-#110 follow-up, 2026-05-19).

PR #110 (D1.3-fu4-oom-closure) staggered the REST-refresh-driven
`request_reconnect()` dispatch. Post-deploy verification showed boot
peak at 510.7 MiB / 512 MiB cap (99.7%) — the boot subscribe burst
ALSO hits the OOM-precursor margin. This Bit extends the same
stagger pattern to the `for archiver in archivers: archiver.start()`
boot loop in `collector/main_loop.run`.

What this file pins:

  1. Public surface — `_start_archivers_staggered` exists as a
     module-level helper in `collector/main_loop.py`. Structural
     test for the canonical home of the boot stagger logic.

  2. Dispatch contract — every archiver receives `.start()` under
     normal (no-shutdown) conditions; the stagger MUST NOT skip
     any archiver.

  3. Stagger timing — per-archiver `.start()` calls are spaced apart
     by ≥ `stagger_seconds` (with scheduling-jitter tolerance).

  4. No leading stagger — the FIRST archiver starts immediately
     (no sleep before it).

  5. Cancellability — `shutdown_event` interrupts the stagger and
     returns early; caller can compute partial-boot count from the
     return value.

See `kb/decisions/d1-3-fu4-boot-stagger-plan.md` for the full RCA +
boot-peak mechanism trace.
"""
from __future__ import annotations

import threading
import time
from typing import Any, List
from unittest.mock import MagicMock

import pytest


from collector.main_loop import (  # noqa: F401
    _RECONNECT_STAGGER_SECONDS,
    _start_archivers_staggered,
)


def test_start_archivers_staggered_helper_exists():
    """`_start_archivers_staggered` is importable from `collector.main_loop`.

    Structural pin — the canonical home of the boot-stagger logic.
    A future edit that drops the helper or relocates it without
    updating this pin is a regression on the L99 lockstep contract.
    """
    assert callable(_start_archivers_staggered), (
        "_start_archivers_staggered must be a callable module-level "
        "function in collector.main_loop."
    )


def _make_mock_archiver(conn_id: str) -> MagicMock:
    """Build a MagicMock archiver with `.start()` instrumented to record
    monotonic timestamps."""
    archiver = MagicMock()
    archiver._conn_id = conn_id
    archiver._start_times: List[float] = []

    def _start_side_effect(*args: Any, **kwargs: Any) -> None:
        archiver._start_times.append(time.monotonic())

    archiver.start = MagicMock(side_effect=_start_side_effect)
    return archiver


def test_boot_dispatches_start_to_all_archivers():
    """Existing-behavior pin: under normal (no shutdown) conditions,
    every archiver receives `.start()`. The stagger MUST NOT skip any
    archiver."""
    archivers = [_make_mock_archiver(f"conn-{i}") for i in range(3)]
    shutdown_event = threading.Event()
    started = _start_archivers_staggered(
        archivers,
        shutdown_event=shutdown_event,
        stagger_seconds=0.01,
    )
    assert started == len(archivers), (
        f"_start_archivers_staggered should return the count of "
        f"archivers actually started ({len(archivers)}); got {started}."
    )
    for a in archivers:
        assert a.start.call_count == 1, (
            f"archiver {a._conn_id} should have received .start() "
            f"exactly once; got {a.start.call_count} call(s)."
        )


def test_boot_staggers_start_calls_by_wall_clock():
    """Per-archiver `.start()` timestamps must be spaced apart by
    ≥ `stagger_seconds` (with scheduling jitter tolerance).

    Pre-Bit boot fires all `start()` calls within ~100ms total. Post-Bit
    conn N+1's `start()` waits until conn N's initial subscribe-ack
    burst has had its window to drain.
    """
    archivers = [_make_mock_archiver(f"conn-{i}") for i in range(3)]
    shutdown_event = threading.Event()
    stagger = 0.05
    _start_archivers_staggered(
        archivers,
        shutdown_event=shutdown_event,
        stagger_seconds=stagger,
    )
    start_times = [a._start_times[0] for a in archivers]
    for i in range(1, len(start_times)):
        gap = start_times[i] - start_times[i - 1]
        assert gap >= stagger * 0.8, (
            f"Gap between archiver[{i - 1}] and archiver[{i}] start() "
            f"was {gap:.3f}s; expected ≥ {stagger * 0.8:.3f}s. "
            f"Boot-time concurrent subscribe-ack flood = OOM-precursor "
            f"mechanism this Bit closes."
        )


def test_boot_no_stagger_before_first_archiver():
    """The first archiver starts immediately (no leading stagger).
    Total wall time ≈ (N-1) × stagger, NOT N × stagger.
    """
    archivers = [_make_mock_archiver(f"conn-{i}") for i in range(3)]
    shutdown_event = threading.Event()
    stagger = 0.1
    t0 = time.monotonic()
    _start_archivers_staggered(
        archivers,
        shutdown_event=shutdown_event,
        stagger_seconds=stagger,
    )
    elapsed = time.monotonic() - t0
    # 3 archivers + 0.1s stagger:
    # Expected: 2 inter-iteration gaps = 0.2s + per-archiver work
    # Trailing-stagger bug would add 0.1s → 0.3s.
    assert elapsed < (len(archivers) - 0.5) * stagger, (
        f"Total boot wall {elapsed:.3f}s ≈ N × stagger "
        f"({len(archivers) * stagger:.3f}s); expected ≈ (N-1) × stagger "
        f"({(len(archivers) - 1) * stagger:.3f}s). Possible leading or "
        f"trailing sleep."
    )


def test_boot_observes_shutdown_event_mid_stagger():
    """If `shutdown_event` fires during the stagger sleep, the loop
    breaks early and returns the partial-boot count.

    Test setup: 5 archivers, stagger=0.5s. Set shutdown at t=0.6s.
    Expected: archiver 0 starts at t=0; archiver 1 at t=0.5; event
    fires at t=0.6; loop breaks. Exactly 2 archivers started.
    Returned count = 2.
    """
    archivers = [_make_mock_archiver(f"conn-{i}") for i in range(5)]
    stagger = 0.5
    shutdown_event = threading.Event()

    def _fire_shutdown() -> None:
        time.sleep(0.6)
        shutdown_event.set()

    timer = threading.Thread(target=_fire_shutdown, daemon=True)
    timer.start()

    started = _start_archivers_staggered(
        archivers,
        shutdown_event=shutdown_event,
        stagger_seconds=stagger,
    )
    timer.join(timeout=1.0)

    assert started == 2, (
        f"Expected EXACTLY 2 archivers to have started (arch0 at t=0, "
        f"arch1 at t=0.5, then break on cancelled event.wait at t=0.6); "
        f"got {started}."
    )
    n_called = sum(a.start.call_count for a in archivers)
    assert n_called == 2, (
        f"Expected exactly 2 archivers to receive .start() before "
        f"shutdown; got {n_called}."
    )


def test_boot_returns_started_count():
    """The return value of `_start_archivers_staggered` is the count of
    archivers that actually received .start() before any cancellation.
    Lets `main_loop.run` log a partial-boot warning + decide whether
    to proceed to refresher.start()."""
    archivers = [_make_mock_archiver(f"conn-{i}") for i in range(4)]
    shutdown_event = threading.Event()
    started = _start_archivers_staggered(
        archivers,
        shutdown_event=shutdown_event,
        stagger_seconds=0.01,
    )
    assert isinstance(started, int), (
        f"_start_archivers_staggered must return an int "
        f"(got {type(started).__name__})."
    )
    assert started == len(archivers), (
        f"Under normal (no shutdown) conditions, started count "
        f"({started}) must equal len(archivers) ({len(archivers)})."
    )


def test_boot_stagger_default_matches_reconnect_constant():
    """Boot stagger reuses `_RECONNECT_STAGGER_SECONDS` as the default.

    The mechanism is identical (per-conn subscribe → cumulative ack
    burst), so a single source-of-truth constant is the design. A
    future split into separate boot vs refresh constants would need
    an explicit Bit + this test would need a corresponding update.
    """
    import inspect
    sig = inspect.signature(_start_archivers_staggered)
    default = sig.parameters["stagger_seconds"].default
    assert default == _RECONNECT_STAGGER_SECONDS, (
        f"_start_archivers_staggered.stagger_seconds default "
        f"({default}) must equal _RECONNECT_STAGGER_SECONDS "
        f"({_RECONNECT_STAGGER_SECONDS}); single-source-of-truth "
        f"design. Splitting the constants requires an explicit Bit."
    )
