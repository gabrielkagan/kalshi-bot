"""D1.3-fu4-oom-closure — staggered reconnect contract (ticket 86b9zk4hz, 2026-05-19).

The B-orphan-sweep ship (PR #105 `86ba0jmz9`) closed the disk-side
in_flight orphan recovery gap. The underlying mechanism that
GENERATED the orphans — systemd-initiated OOM-kill on the kalshi-
collector cgroup — is closed by this Bit on the memory side.

What this file pins:

  1. Public surface — `_RECONNECT_STAGGER_SECONDS` exists as a
     module-level constant in `collector/main_loop.py` with a finite
     positive default. A future edit that drops the constant or sets
     it to 0 / a negative value defeats the OOM-closure invariant.

  2. Behavioral contract — `_replan_for_archivers` spaces per-archiver
     `request_reconnect()` calls in time by at least
     `_RECONNECT_STAGGER_SECONDS`. The pre-Bit code called all
     archivers' reconnect within ~1ms; the staggered version separates
     them so the concurrent subscribe-ack flood (peak ~7 conns × ~5 MB
     ack payloads simultaneously parsed on the asyncio threads) is
     replaced by sequential single-conn ack windows.

  3. No trailing stagger — the loop sleeps BETWEEN iterations, not
     AFTER the last archiver. Avoids spurious wall-clock latency on
     replan completion.

  4. Cancellability via shutdown_event — `_replan_for_archivers`
     accepts a `shutdown_event` parameter; if the event fires during
     the stagger sleep, the loop terminates early instead of pinning
     the refresher thread for ~140s during graceful shutdown.

  5. Existing dispatch contract preserved — every archiver still
     receives BOTH `update_subscriptions(new_frames, new_map)` AND
     `request_reconnect()`. The stagger MUST NOT skip any archiver.

See `kb/decisions/d1-3-fu4-oom-closure-plan.md` §RCA for the OOM
mechanism trace and the derivation of the 20s default stagger value.
"""
from __future__ import annotations

import threading
import time
from typing import Any, List
from unittest.mock import MagicMock

import pytest


# Import from the canonical home in collector/main_loop.py. This import
# itself is part of the contract — the constant + function MUST be
# importable at this module path post-Bit.
from collector.main_loop import (  # noqa: F401
    _RECONNECT_STAGGER_SECONDS,
    _replan_for_archivers,
)


# ─── 1. Public surface ─────────────────────────────────────────────────────


def test_reconnect_stagger_constant_is_positive_finite():
    """`_RECONNECT_STAGGER_SECONDS` must be a positive float < 600s.

    A 0 / negative value defeats the OOM closure (collapses back to the
    pre-Bit tight-loop reconnect). Values ≥ 600s would push the 7-conn
    total replan window over 70 min — at hourly REST refresh that risks
    overlap with the next refresh tick.
    """
    assert isinstance(_RECONNECT_STAGGER_SECONDS, (int, float)), (
        f"_RECONNECT_STAGGER_SECONDS must be a numeric scalar "
        f"(got type {type(_RECONNECT_STAGGER_SECONDS).__name__})."
    )
    assert _RECONNECT_STAGGER_SECONDS > 0, (
        f"_RECONNECT_STAGGER_SECONDS must be > 0 (got "
        f"{_RECONNECT_STAGGER_SECONDS}); zero stagger collapses back to "
        f"the pre-Bit tight-loop reconnect that OOMs the cgroup."
    )
    assert _RECONNECT_STAGGER_SECONDS < 600, (
        f"_RECONNECT_STAGGER_SECONDS must be < 600s (got "
        f"{_RECONNECT_STAGGER_SECONDS}); per-conn × 7 conns must stay "
        f"well under DEFAULT_REFRESH_INTERVAL_SECONDS=3600 to avoid "
        f"overlapping with the next REST refresh tick."
    )


# ─── 2. Behavioral contract — dispatch preservation ────────────────────────


def _make_mock_archiver(conn_id: str) -> MagicMock:
    """Build a MagicMock archiver with the methods _replan_for_archivers calls.

    Tracks call timestamps via a side_effect that records monotonic time
    into `_call_times` per call.
    """
    archiver = MagicMock()
    archiver._conn_id = conn_id
    archiver._update_subscriptions_times: List[float] = []
    archiver._request_reconnect_times: List[float] = []

    def _update_side_effect(*args: Any, **kwargs: Any) -> None:
        archiver._update_subscriptions_times.append(time.monotonic())

    def _reconnect_side_effect(*args: Any, **kwargs: Any) -> None:
        archiver._request_reconnect_times.append(time.monotonic())

    archiver.update_subscriptions = MagicMock(side_effect=_update_side_effect)
    archiver.request_reconnect = MagicMock(side_effect=_reconnect_side_effect)
    return archiver


def _replan_kwargs(archivers: List[MagicMock], **overrides: Any) -> dict:
    """Default kwargs to drive `_replan_for_archivers`. Tests may override
    `shutdown_event` or supply a tickers_by_tier shape suited to the
    SubscriptionManager planner."""
    base = {
        # Tier shape: single tier with N×2 tickers so the planner round-
        # robins ≥ 2 tickers to each conn. The exact ticker count is
        # not load-bearing for these tests; only the dispatch + timing
        # behavior matters.
        "new_tickers_by_tier": {"1": [f"TICKER-{i}" for i in range(len(archivers) * 4)]},
        "archivers": archivers,
        "conn_count": len(archivers),
        "batch_size": 1000,
    }
    base.update(overrides)
    return base


def test_replan_dispatches_to_all_archivers():
    """Existing-behavior pin: each archiver receives BOTH
    `update_subscriptions` AND `request_reconnect`. The stagger MUST NOT
    skip any archiver under normal (no-shutdown) conditions.
    """
    archivers = [_make_mock_archiver(f"conn-{i}") for i in range(3)]
    # Use a tiny stagger so the test runs fast; the dispatch contract
    # is independent of stagger duration.
    _replan_for_archivers(
        **_replan_kwargs(
            archivers,
            shutdown_event=None,
            stagger_seconds=0.01,
        )
    )
    for a in archivers:
        assert a.update_subscriptions.call_count == 1, (
            f"archiver {a._conn_id}: update_subscriptions called "
            f"{a.update_subscriptions.call_count}× (expected exactly 1)."
        )
        assert a.request_reconnect.call_count == 1, (
            f"archiver {a._conn_id}: request_reconnect called "
            f"{a.request_reconnect.call_count}× (expected exactly 1)."
        )


# ─── 3. Behavioral contract — stagger timing ───────────────────────────────


def test_replan_staggers_reconnect_calls_by_wall_clock():
    """Per-archiver `request_reconnect` timestamps must be spaced apart
    by ≥ stagger_seconds (with scheduling jitter tolerance).

    Pre-Bit code calls all reconnects in a tight loop with <1ms between
    timestamps. Post-Bit, conn-N+1's reconnect waits until conn-N has
    had its ack-burst window to drain.

    Test uses stagger=0.05s × 3 archivers → total wall ~0.1s. Contract
    tier budget (<5s) is well preserved.
    """
    archivers = [_make_mock_archiver(f"conn-{i}") for i in range(3)]
    stagger = 0.05
    t0 = time.monotonic()
    _replan_for_archivers(
        **_replan_kwargs(
            archivers,
            shutdown_event=None,
            stagger_seconds=stagger,
        )
    )
    elapsed = time.monotonic() - t0

    # Collect the per-archiver request_reconnect timestamps in plan order
    # (zip with archivers preserves the order _replan_for_archivers iterated).
    reconnect_times = [a._request_reconnect_times[0] for a in archivers]
    assert len(reconnect_times) == len(archivers)

    # Pairwise gaps between consecutive reconnects must be >= stagger,
    # modulo a small jitter floor for scheduling. Use 0.8× stagger as
    # the gate so CI runners with coarser sleep precision don't flake.
    for i in range(1, len(reconnect_times)):
        gap = reconnect_times[i] - reconnect_times[i - 1]
        assert gap >= stagger * 0.8, (
            f"Gap between archiver[{i - 1}] and archiver[{i}] "
            f"request_reconnect was {gap:.3f}s; expected ≥ "
            f"{stagger * 0.8:.3f}s (stagger={stagger}). Concurrent "
            f"reconnect on REST refresh = OOM mechanism this Bit closes."
        )

    # Total elapsed must reflect the staggers: N-1 inter-iteration gaps
    # of `stagger`, but NO trailing stagger after the last iteration.
    # With 3 archivers + 0.05s stagger, expect ~0.1s total (NOT 0.15s).
    assert elapsed < stagger * len(archivers) * 1.5, (
        f"Total replan wall time {elapsed:.3f}s exceeded "
        f"{stagger * len(archivers) * 1.5:.3f}s; possible trailing "
        f"stagger after last archiver (should sleep BETWEEN, not AFTER)."
    )


def test_replan_no_stagger_after_last_archiver():
    """Stagger sleeps BETWEEN iterations, not AFTER the last archiver.

    With N archivers and stagger=S, total wall time should be
    approximately (N-1)*S + per-archiver work, NOT N*S. A trailing
    stagger would add S to graceful shutdown latency for every replan
    without buying anything.
    """
    archivers = [_make_mock_archiver(f"conn-{i}") for i in range(3)]
    stagger = 0.1
    t0 = time.monotonic()
    _replan_for_archivers(
        **_replan_kwargs(
            archivers,
            shutdown_event=None,
            stagger_seconds=stagger,
        )
    )
    elapsed = time.monotonic() - t0

    # Expected: (3-1) × 0.1 = 0.2s of stagger total + per-arch work.
    # With a trailing stagger: 3 × 0.1 = 0.3s. Gate at 0.27s to
    # distinguish the two cases reliably under jitter.
    assert elapsed < (len(archivers) - 0.5) * stagger, (
        f"Total replan wall time {elapsed:.3f}s ≈ N×stagger "
        f"({len(archivers) * stagger:.3f}s); expected ≈ (N-1)×stagger "
        f"({(len(archivers) - 1) * stagger:.3f}s). Possible trailing "
        f"sleep after final archiver."
    )


# ─── 4. Cancellability ─────────────────────────────────────────────────────


def test_replan_observes_shutdown_event_mid_stagger():
    """If `shutdown_event` fires during the stagger sleep, the loop
    breaks early instead of pinning the refresher thread for ~140s.

    Test setup: 5 archivers, stagger=0.5s. Total no-shutdown wall would
    be ~(5-1)×0.5 = 2.0s. We set the shutdown event after ~0.6s, which
    should land mid-stagger between archivers 2 and 3 — the loop should
    NOT continue past archiver 2's reconnect.
    """
    archivers = [_make_mock_archiver(f"conn-{i}") for i in range(5)]
    stagger = 0.5
    shutdown_event = threading.Event()

    def _fire_shutdown() -> None:
        time.sleep(0.6)
        shutdown_event.set()

    timer = threading.Thread(target=_fire_shutdown, daemon=True)
    timer.start()

    t0 = time.monotonic()
    _replan_for_archivers(
        **_replan_kwargs(
            archivers,
            shutdown_event=shutdown_event,
            stagger_seconds=stagger,
        )
    )
    elapsed = time.monotonic() - t0
    timer.join(timeout=1.0)

    # Expected behavior: archivers 0 + 1 receive both calls (firing at
    # t≈0 and t≈0.5); archivers 2-4 either receive 0 or only
    # update_subscriptions before the loop exits via the cancelled wait.
    # Gate the reconnect counts: at most 2 of the 5 archivers should
    # have actually reconnected.
    n_reconnected = sum(a.request_reconnect.call_count for a in archivers)
    assert n_reconnected <= 2, (
        f"Shutdown event fired mid-replan; expected ≤ 2 archivers to "
        f"have completed request_reconnect (the loop should have "
        f"observed the event during stagger and exited). Got "
        f"{n_reconnected} reconnects across {len(archivers)} archivers."
    )

    # Elapsed wall should be roughly bounded by the time we set the
    # event (~0.6s) + one stagger interval grace. Definitely NOT the
    # full no-shutdown duration ((N-1)*stagger ≈ 2.0s).
    assert elapsed < (len(archivers) - 1) * stagger - 0.1, (
        f"Replan wall {elapsed:.3f}s ≈ full no-shutdown duration; "
        f"shutdown event should have cancelled the stagger early."
    )
