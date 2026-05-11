"""Per-source circuit breakers for external API calls.

Architectural foundation v3 — three rounds of adversarial review.

Semantics fixed in v3:
  failure_count is STRICTLY "consecutive failures in the current
  CLOSED epoch." It is reset on every state transition (CLOSED→OPEN,
  HALF_OPEN→CLOSED, HALF_OPEN→OPEN). Late acks from prior epochs
  are silently dropped (generation mismatch) — this is intentional
  and matches the documented semantics.

  For lifetime "how many failures has this source seen?" metrics,
  add a separate monotonic counter in a follow-up commit (out of
  scope for the foundation).

History:

  P0-1: Half-open allowed N parallel probes under concurrency.
  P0-2: Stale `record_success` from in-flight calls predating the
        OPEN transition could force CLOSED → defeats the breaker.
  P0-3: `state` property mutated state on read (auto-recovered an
        OPEN breaker just by being observed by a dashboard).

Design (v2):

  Three explicit states: CLOSED, OPEN, HALF_OPEN.

  acquire() -> Optional[int]
    The ONLY state-mutating "before-call" method. Returns a
    generation token if the call is allowed, or None if the
    breaker is OPEN (or HALF_OPEN with a probe already in flight).
    Caller must pass the token to record_result.

  record_result(generation: int, success: bool)
    Records the outcome. The generation token disambiguates stale
    completions (an in-flight call that started before a state
    transition will have a stale token; its outcome is ignored).

  is_open() / state / failure_count / __repr__
    Pure peeks — never mutate state. Safe to call from a dashboard
    thread.

  call() — context manager
    Misuse-resistant high-level API. acquire-call-record_result
    in one block; ensures success/failure are always recorded.
    Raises CircuitBreakerOpen if the circuit is OPEN.

Half-open semantics:
  Exactly ONE trial probe is allowed at a time. A probe_timeout
  (default 30s) prevents a forgotten record_result from
  permanently blocking recovery — after the timeout, a new probe
  may be acquired.

Time:
  Uses `time.monotonic()` so NTP step adjustments don't skew
  recovery / probe timing.

Thread-safety:
  All state under a single per-breaker `threading.Lock` (non-
  reentrant; no callsite reenters). Registry uses an unconditional
  lock — the lock-free fast path was removed to avoid relying on
  CPython implementation details for dict-read atomicity.

See:
  - kb/failures/scan-tick-stall-cluster-2026-04-25.md
    (the architectural rebuild's parent incident)
  - kb/failures/morning-incident-2026-04-25.md
    (Kalshi /events 400 cascade that proved the design need)
"""

from __future__ import annotations

import enum
import logging
import math
import threading
import time
from contextlib import contextmanager
from typing import Dict, Iterator, Optional


_log = logging.getLogger(__name__)


class State(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpen(Exception):
    """Raised by CircuitBreaker.call() when the breaker is open."""


class CircuitBreaker:
    """Per-source circuit breaker.

    Args:
        failures_to_open: Consecutive failures that trip CLOSED → OPEN.
            Must be >= 1.
        recovery_seconds: How long to stay OPEN before allowing a
            half-open probe. Must be >= 0.
        probe_timeout_seconds: Max time a HALF_OPEN probe may be
            in flight before a forgotten record_result is treated
            as failed (allowing a new probe). Default 30s.
    """

    def __init__(self, failures_to_open: int = 3,
                 recovery_seconds: float = 300.0,
                 probe_timeout_seconds: float = 30.0,
                 name: str = "<unnamed>") -> None:
        # Type-check first — Python's `bool` is a subclass of `int`,
        # so `failures_to_open=True` would silently become 1. Reject.
        if not isinstance(failures_to_open, int) or isinstance(
                failures_to_open, bool):
            raise ValueError(
                "failures_to_open must be an int (not bool, not "
                "float). Got %r" % (failures_to_open,))
        if failures_to_open < 1:
            raise ValueError("failures_to_open must be >= 1")
        if not math.isfinite(recovery_seconds) or recovery_seconds < 0:
            raise ValueError(
                "recovery_seconds must be a finite number >= 0 "
                "(NaN/inf would leave the breaker stuck OPEN).")
        if (not math.isfinite(probe_timeout_seconds)
                or probe_timeout_seconds <= 0):
            raise ValueError(
                "probe_timeout_seconds must be a finite number > 0 "
                "— a zero or non-finite timeout would treat every "
                "probe as immediately abandoned, re-introducing "
                "the v1 P0-1 'unlimited parallel probes' bug.")
        self._failures_to_open = failures_to_open
        self._recovery_seconds = recovery_seconds
        self._probe_timeout_seconds = probe_timeout_seconds
        self.name: str = name
        self._state: State = State.CLOSED
        self._failures: int = 0
        self._opened_at: Optional[float] = None
        # Generation counter: incremented on every state transition
        # so stale `record_result` from old generations are ignored.
        self._generation: int = 0
        # When a HALF_OPEN probe is in flight, this is its
        # acquisition time (monotonic). None when no probe.
        self._probe_started_at: Optional[float] = None
        # Lifetime monotonic counters (round-4 A1 — observability for
        # partial-broken APIs). These count EVERY ack regardless of
        # generation, so dashboard sees true call volume even when
        # the trip-counter `_failures` resets.
        self._total_successes: int = 0
        self._total_failures: int = 0
        self._total_calls_refused: int = 0
        self._lock = threading.Lock()

    # ── Mutating "before call" ────────────────────────────────────

    def acquire(self) -> Optional[int]:
        """Try to acquire permission for a call. Returns a generation
        token if allowed; None if the circuit is OPEN or HALF_OPEN
        with a probe already in flight.

        Caller MUST pass the returned token to record_result.
        """
        now = time.monotonic()
        with self._lock:
            if self._state is State.CLOSED:
                return self._generation

            if self._state is State.OPEN:
                # Has the recovery period elapsed?
                assert self._opened_at is not None
                if (now - self._opened_at) >= self._recovery_seconds:
                    # Transition OPEN → HALF_OPEN, allow one probe.
                    self._state = State.HALF_OPEN
                    self._generation += 1
                    self._probe_started_at = now
                    return self._generation
                self._total_calls_refused += 1
                return None

            # HALF_OPEN: only one probe allowed at a time.
            assert self._state is State.HALF_OPEN
            if self._probe_started_at is None:
                # Defensive — no probe in flight, allow one.
                self._generation += 1
                self._probe_started_at = now
                return self._generation
            # Probe in flight; check timeout.
            if (now - self._probe_started_at) >= self._probe_timeout_seconds:
                # Forgotten probe — treat it as abandoned and let
                # this caller take the next probe immediately. The
                # stale generation guarantees the abandoned probe's
                # record_result (if it ever arrives) is ignored.
                self._generation += 1
                self._probe_started_at = now
                return self._generation
            self._total_calls_refused += 1
            return None

    # ── Mutating "after call" ─────────────────────────────────────

    def record_result(self, generation: int, success: bool) -> None:
        """Record the outcome of a call. Stale generations (from
        before a state transition) are silently ignored for STATE
        purposes — this is what prevents a stale ack from defeating
        the breaker. Lifetime monotonic counters are incremented
        regardless of generation (round-4 A1: observability for
        partial-broken APIs)."""
        with self._lock:
            # Always increment lifetime counters, even for stale
            # generations. This way the dashboard sees true call
            # volume (e.g., a 30%-broken upstream that sawtooths
            # trip→recover→trip will accumulate visible failures).
            if success:
                self._total_successes += 1
            else:
                self._total_failures += 1

            if generation != self._generation:
                # Stale ack — call was issued in a different
                # generation. Don't update state.
                return

            if self._state is State.HALF_OPEN:
                self._probe_started_at = None
                if success:
                    # Probe succeeded — fully close.
                    self._state = State.CLOSED
                    self._failures = 0
                    self._opened_at = None
                    self._generation += 1
                    _log.info(
                        "CIRCUIT_BREAKER_RECOVERED: %s "
                        "(probe success → CLOSED)", self.name)
                else:
                    # Probe failed — back to OPEN. Reset failure
                    # counter so the next CLOSED epoch starts fresh
                    # (round-3 P0-1 fix: failure_count is strictly
                    # "consecutive failures in current CLOSED epoch").
                    self._state = State.OPEN
                    self._opened_at = time.monotonic()
                    self._failures = 0
                    self._generation += 1
                    _log.warning(
                        "CIRCUIT_BREAKER_REOPENED: %s "
                        "(probe failed → OPEN)", self.name)
                return

            if self._state is State.CLOSED:
                if success:
                    self._failures = 0
                else:
                    self._failures += 1
                    if self._failures >= self._failures_to_open:
                        self._state = State.OPEN
                        self._opened_at = time.monotonic()
                        # Bump generation so in-flight CLOSED-epoch
                        # acks (which carry the old gen) are dropped
                        # by the gen-mismatch check above. Round-3
                        # decision: failure_count strictly tracks
                        # consecutive-in-CLOSED, so resetting to 0
                        # here is correct. Lifetime metrics belong
                        # in a separate counter (future work).
                        self._failures = 0
                        self._generation += 1
                        _log.warning(
                            "CIRCUIT_BREAKER_TRIPPED: %s "
                            "(threshold=%d, recovery=%.0fs)",
                            self.name, self._failures_to_open,
                            self._recovery_seconds)
                return

            # State.OPEN: shouldn't be reachable with a matching
            # generation (acquire returned None for callers that
            # tried to acquire while OPEN; old CLOSED-epoch acks
            # have stale gens and were dropped above). If we somehow
            # land here, ignore — the breaker is already tripped.

    # ── Context manager (the recommended API) ─────────────────────

    @contextmanager
    def call(self) -> Iterator[None]:
        """Misuse-resistant API. Use `with breaker.call(): ...`.

        Raises CircuitBreakerOpen if the circuit is OPEN. Records
        success on normal exit, failure if an exception propagates.
        """
        gen = self.acquire()
        if gen is None:
            # Snapshot state for the error message. May be slightly
            # stale by the time the exception lands, but at least
            # reflects what acquire() saw rather than racing with
            # a concurrent transition.
            snapshot = self.state
            raise CircuitBreakerOpen(
                f"Circuit is {snapshot}; call refused.")
        recorded = False
        try:
            yield
            # Set `recorded` BEFORE the record_result call so that
            # if record_result itself ever raises in the future
            # (it doesn't today), the `except Exception` branch
            # below won't double-record (round-5 P0-1 defensive).
            recorded = True
            self.record_result(gen, success=True)
        except Exception:
            if not recorded:
                self.record_result(gen, success=False)
                recorded = True
            raise
        finally:
            if not recorded:
                # BaseException path (KeyboardInterrupt, SystemExit,
                # asyncio.CancelledError pre-3.8, etc.). The call was
                # interrupted, not failed. Releasing the probe slot
                # without recording prevents Ctrl-C during a
                # HALF_OPEN probe from blocking recovery for
                # probe_timeout_seconds (round-4 A2).
                self._release_probe_unrecorded(gen)

    def _release_probe_unrecorded(self, generation: int) -> None:
        """Release a HALF_OPEN probe slot WITHOUT recording success/
        failure. Used for BaseException paths (KeyboardInterrupt,
        SystemExit) where the call was abandoned, not failed.

        State is unchanged — the next acquire() can take a fresh
        probe immediately."""
        with self._lock:
            if generation != self._generation:
                return  # stale; nothing to release
            if (self._state is State.HALF_OPEN
                    and self._probe_started_at is not None):
                # Bump generation so any future record_result from
                # the abandoned probe (matching the OLD gen) is
                # ignored as stale.
                self._probe_started_at = None
                self._generation += 1

    # ── Pure peeks (never mutate) ─────────────────────────────────

    def is_open(self) -> bool:
        """True iff state is OPEN. Pure read — does NOT auto-recover.
        Auto-recovery happens only inside acquire()."""
        with self._lock:
            return self._state is State.OPEN

    @property
    def state(self) -> str:
        """One of 'closed', 'open', 'half_open'. Pure read."""
        with self._lock:
            return self._state.value

    @property
    def failure_count(self) -> int:
        """Consecutive failures in the CURRENT CLOSED epoch. Resets
        on every state transition. For lifetime metrics, use
        `metrics`."""
        with self._lock:
            return self._failures

    @property
    def metrics(self) -> Dict[str, int]:
        """Lifetime monotonic counters for the per-source health
        dashboard. `total_failures` and `total_successes` count
        every ack regardless of state — so a partial-broken upstream
        that sawtooths trip↔recover is visible in the metrics
        even when `failure_count` resets to 0 on each transition."""
        with self._lock:
            return {
                "total_successes": self._total_successes,
                "total_failures": self._total_failures,
                "total_calls_refused": self._total_calls_refused,
            }

    def __repr__(self) -> str:
        with self._lock:
            return (f"CircuitBreaker(state={self._state.value}, "
                    f"failures={self._failures}/"
                    f"{self._failures_to_open}, "
                    f"recovery={self._recovery_seconds}s)")


class CircuitBreakerRegistry:
    """Per-key registry so each external endpoint gets its own breaker."""

    def __init__(self) -> None:
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def get(self, key: str, **kwargs) -> CircuitBreaker:
        # Always lock — fast-path-without-lock is undefined behavior
        # under the Python language spec even if CPython dict reads
        # happen to be atomic today (P1-2 from review).
        with self._lock:
            existing = self._breakers.get(key)
            if existing is not None:
                return existing
            # Auto-name from registry key so logs identify which
            # endpoint tripped (round-1 A3 of step #2 review).
            kwargs.setdefault("name", key)
            breaker = CircuitBreaker(**kwargs)
            self._breakers[key] = breaker
            return breaker

    def all_breakers(self) -> Dict[str, CircuitBreaker]:
        """Snapshot of all registered breakers, for the per-source
        health dashboard (planned commit #6)."""
        with self._lock:
            return dict(self._breakers)


# Module-level singleton — `from bot.infra.circuit_breaker import REGISTRY` (Sprint 10.5a, 2026-05-11).
REGISTRY: CircuitBreakerRegistry = CircuitBreakerRegistry()
