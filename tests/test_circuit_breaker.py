"""Foundation tests for circuit_breaker.py — Apr 25 09:34 UTC
sports-400 cascade incident response.

Architectural fix (per kb/failures/scan-tick-stall-cluster-2026-04-25.md
"What I'd push back on" section + this morning's reaffirmation):
every external call should be wrapped in a circuit breaker that
auto-disables a failing source after N failures and reopens after
a recovery period. Prevents cascading failures like today's
~14 dead soccer series × ~1s each = 14s of synchronous REST
blocking that compounded into 15M scan throughput collapse.

These tests define the public interface BEFORE the implementation
exists. Each test asserts a specific invariant of the state
machine: CLOSED → OPEN after N failures, OPEN → CLOSED after
recovery elapsed, success in HALF_OPEN resets counter, etc.

The module must be thread-safe (called from multiple worker
threads — sports, weather, settlement, market refresh, EGARCH
refit, scan).
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestCircuitBreakerStateMachine(unittest.TestCase):
    """The core CLOSED → OPEN → CLOSED state machine."""

    def test_starts_closed(self):
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=3, recovery_seconds=10)
        self.assertFalse(b.is_open())

    def test_opens_after_n_failures(self):
        """N consecutive failures must transition CLOSED → OPEN."""
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=3, recovery_seconds=10)
        for _ in range(2):
            b.record_failure()
        self.assertFalse(b.is_open(),
            "Should still be closed after 2 failures (threshold is 3)")
        b.record_failure()
        self.assertTrue(b.is_open(),
            "Should be open after 3 consecutive failures")

    def test_success_resets_failure_counter(self):
        """A success in CLOSED state resets the consecutive-failure
        counter so the next failure starts from zero."""
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=3, recovery_seconds=10)
        b.record_failure()
        b.record_failure()
        b.record_success()
        b.record_failure()
        b.record_failure()
        self.assertFalse(b.is_open(),
            "Counter should have reset after success; 2 fresh "
            "failures should not open the breaker.")

    def test_recovers_after_timeout(self):
        """OPEN → CLOSED transition after recovery_seconds elapsed."""
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=2, recovery_seconds=0.1)
        b.record_failure()
        b.record_failure()
        self.assertTrue(b.is_open())
        time.sleep(0.15)
        self.assertFalse(b.is_open(),
            "Should auto-recover after recovery_seconds elapsed.")

    def test_failure_in_half_open_reopens(self):
        """If we recover and the next call fails again, the breaker
        should re-open immediately (not wait for N more failures)."""
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=2, recovery_seconds=0.1)
        b.record_failure()
        b.record_failure()
        self.assertTrue(b.is_open())
        time.sleep(0.15)
        # Now is_open() should return False (half-open)
        self.assertFalse(b.is_open())
        # If next call fails, should reopen on first failure.
        b.record_failure()
        self.assertTrue(b.is_open(),
            "A single failure after recovery should reopen the "
            "breaker (half-open semantics).")

    def test_thread_safe(self):
        """Concurrent record_failure / record_success / is_open
        from multiple threads must not corrupt state."""
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=1000, recovery_seconds=60)
        N = 50
        ITER = 100

        def worker():
            for _ in range(ITER):
                b.record_failure()
                b.is_open()
                b.record_success()

        threads = [threading.Thread(target=worker) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Final state: counter should be 0 (every fail followed by a
        # success). Even if interleaving was racy, counter must be a
        # valid int (no exceptions raised).
        self.assertFalse(b.is_open())


class TestCircuitBreakerRegistry(unittest.TestCase):
    """Per-key registry so each external endpoint gets its own breaker
    (e.g., one per Kalshi series_ticker, one per ESPN league, etc.)."""

    def test_returns_same_breaker_for_same_key(self):
        from circuit_breaker import CircuitBreakerRegistry
        r = CircuitBreakerRegistry()
        b1 = r.get("kalshi_events_KXNBAGAME")
        b2 = r.get("kalshi_events_KXNBAGAME")
        self.assertIs(b1, b2,
            "Same key must return the same breaker instance "
            "(state must persist across calls).")

    def test_returns_different_breaker_for_different_keys(self):
        from circuit_breaker import CircuitBreakerRegistry
        r = CircuitBreakerRegistry()
        b1 = r.get("kalshi_events_KXNBAGAME")
        b2 = r.get("kalshi_events_KXMLBGAME")
        self.assertIsNot(b1, b2,
            "Different keys must return distinct breaker instances "
            "so a failing source doesn't trip a healthy one.")

    def test_first_call_uses_provided_kwargs(self):
        """First-time creation respects failures_to_open / recovery
        kwargs."""
        from circuit_breaker import CircuitBreakerRegistry
        r = CircuitBreakerRegistry()
        b = r.get("test_endpoint",
                  failures_to_open=5, recovery_seconds=600)
        for _ in range(4):
            b.record_failure()
        self.assertFalse(b.is_open(),
            "Should respect failures_to_open=5; 4 failures should "
            "not open the breaker.")
        b.record_failure()
        self.assertTrue(b.is_open(),
            "5th failure should open it (matches kwarg).")

    def test_registry_thread_safe(self):
        """Concurrent get() calls for the same key must not create
        duplicate breakers (race in dict.setdefault would lose state)."""
        from circuit_breaker import CircuitBreakerRegistry
        r = CircuitBreakerRegistry()
        N = 50
        results = []
        results_lock = threading.Lock()

        def worker():
            b = r.get("contended_key")
            with results_lock:
                results.append(id(b))

        threads = [threading.Thread(target=worker) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(set(results)), 1,
            "All threads must receive the same breaker instance.")


class TestCircuitBreakerObservability(unittest.TestCase):
    """Operators need to see breaker state for the per-source health
    dashboard (commit #6 in the plan)."""

    def test_state_property_returns_string_label(self):
        """`state` should return one of {'closed', 'open'}
        for dashboard rendering."""
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=1, recovery_seconds=10)
        self.assertEqual(b.state, "closed")
        b.record_failure()
        self.assertEqual(b.state, "open")

    def test_failure_count_observable(self):
        """`failure_count` should expose the running counter for
        diagnostics."""
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=10, recovery_seconds=10)
        self.assertEqual(b.failure_count, 0)
        b.record_failure()
        b.record_failure()
        self.assertEqual(b.failure_count, 2)


if __name__ == "__main__":
    unittest.main()
