"""Per-source circuit breakers for external API calls.

Architectural foundation introduced after the Apr 25 09:34 UTC
sports-400 cascade: ~14 dead Kalshi soccer series each costing
~1s on a 400 response × 2 calls per discovery cycle compounded
into 15M scan throughput collapse. The cascade was preventable —
each dead series should have auto-disabled after a few failures
instead of being retried forever.

Design:

  CLOSED  → calls pass through; failures count toward threshold.
  OPEN    → calls short-circuit; auto-recovers after recovery_seconds.
  (no explicit half-open state — a single failure after auto-recovery
  re-opens immediately, which is functionally equivalent.)

Usage:

    from circuit_breaker import REGISTRY

    breaker = REGISTRY.get("kalshi_events_KXNBAGAME",
                           failures_to_open=3, recovery_seconds=300)
    if breaker.is_open():
        return None  # short-circuit; don't call the failing endpoint
    try:
        result = client.get_events(series_ticker="KXNBAGAME", ...)
        breaker.record_success()
        return result
    except Exception:
        breaker.record_failure()
        raise

Thread-safe — all state mutations are guarded by a per-breaker lock.
The registry's get() is also locked so concurrent first-time access
for the same key returns a single instance.

See kb/failures/scan-tick-stall-cluster-2026-04-25.md for the
incident this prevents.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional


class CircuitBreaker:
    """Per-source circuit breaker.

    Args:
        failures_to_open: Number of consecutive failures that triggers
            the OPEN state. Default 3.
        recovery_seconds: How long to stay OPEN before auto-recovering.
            Default 300 (5 minutes).
    """

    __slots__ = ("failures_to_open", "recovery_seconds",
                 "_failures", "_opened_at",
                 "_just_auto_recovered", "_lock")

    def __init__(self, failures_to_open: int = 3,
                 recovery_seconds: float = 300.0) -> None:
        self.failures_to_open: int = failures_to_open
        self.recovery_seconds: float = recovery_seconds
        self._failures: int = 0
        self._opened_at: Optional[float] = None
        # True after auto-recovery, until the next record_success or
        # record_failure resolves whether to fully close or reopen.
        # Implements half-open semantics without an explicit state.
        self._just_auto_recovered: bool = False
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        """Returns True if calls should short-circuit. Side-effect:
        if the recovery period has elapsed, transitions OPEN → CLOSED
        and marks half-open so a single subsequent failure re-opens
        immediately (rather than waiting for N more failures)."""
        with self._lock:
            if self._opened_at is None:
                return False
            if time.time() - self._opened_at >= self.recovery_seconds:
                # Auto-recover into half-open. record_failure will
                # treat the next failure as "still failing" and
                # reopen on the first hit.
                self._opened_at = None
                self._failures = 0
                self._just_auto_recovered = True
                return False
            return True

    def record_success(self) -> None:
        """Reset the failure counter and clear OPEN/half-open state."""
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._just_auto_recovered = False

    def record_failure(self) -> None:
        """Increment failure counter. If we're in half-open (just
        auto-recovered), a single failure immediately reopens.
        Otherwise, transition to OPEN once `failures_to_open` is hit."""
        with self._lock:
            self._failures += 1
            if self._just_auto_recovered:
                # Half-open: trip immediately.
                self._opened_at = time.time()
                self._just_auto_recovered = False
                return
            if self._failures >= self.failures_to_open:
                self._opened_at = time.time()

    @property
    def state(self) -> str:
        """'closed' or 'open' — for dashboard rendering."""
        return "open" if self.is_open() else "closed"

    @property
    def failure_count(self) -> int:
        """Current consecutive-failure count for diagnostics."""
        with self._lock:
            return self._failures

    def __repr__(self) -> str:
        return (f"CircuitBreaker(state={self.state}, "
                f"failures={self.failure_count}/"
                f"{self.failures_to_open}, "
                f"recovery={self.recovery_seconds}s)")


class CircuitBreakerRegistry:
    """Per-key registry so each external endpoint gets its own breaker.

    Used as a process-global singleton (see module-level REGISTRY).
    Same key always returns the same breaker instance so state
    persists across calls.
    """

    def __init__(self) -> None:
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def get(self, key: str, **kwargs) -> CircuitBreaker:
        """Return the breaker for `key`, creating it on first access.

        kwargs are passed to the CircuitBreaker constructor on first
        creation only. Subsequent get() calls for the same key
        ignore kwargs and return the existing instance.
        """
        # Fast path — most calls hit existing breakers.
        existing = self._breakers.get(key)
        if existing is not None:
            return existing
        # Slow path — first-time creation, lock-and-check pattern.
        with self._lock:
            existing = self._breakers.get(key)
            if existing is not None:
                return existing
            breaker = CircuitBreaker(**kwargs)
            self._breakers[key] = breaker
            return breaker

    def all_breakers(self) -> Dict[str, CircuitBreaker]:
        """Snapshot of all registered breakers, for the per-source
        health dashboard (planned commit #6)."""
        with self._lock:
            return dict(self._breakers)


# Module-level singleton — import as `from circuit_breaker import REGISTRY`.
REGISTRY: CircuitBreakerRegistry = CircuitBreakerRegistry()
