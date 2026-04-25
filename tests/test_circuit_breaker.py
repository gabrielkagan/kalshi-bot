"""Foundation tests for circuit_breaker.py — Apr 25 09:34 UTC
sports-400 cascade incident response, post-adversarial-review v2.

The original (commit 0bcd66c) had 3 P0 bugs the adversarial review
surfaced before any integration:
  P0-1: Half-open allowed N parallel "trial" probes under concurrent
        worker threads — the whole point of half-open (one probe)
        was broken.
  P0-2: Stale `record_success` from in-flight calls predating the
        OPEN transition could force the breaker CLOSED.
  P0-3: `state` property and `__repr__` called `is_open()` which
        mutated state — a dashboard read could silently auto-recover
        a real OPEN breaker.

This v2 test suite locks down the post-fix design:
  - Explicit State enum (CLOSED / OPEN / HALF_OPEN)
  - `acquire()` is the only state-mutating "before-call" method;
    returns a generation token or None.
  - `record_result(gen, success)` ignores stale-generation acks.
  - `is_open()`, `state`, `__repr__` are pure peeks — never mutate.
  - Context manager `call()` makes the API misuse-resistant.
  - HALF_OPEN gates exactly ONE trial probe at a time (with timeout
    so a forgotten record_result doesn't permanently lock the
    breaker).
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestCircuitBreakerStateMachine(unittest.TestCase):

    def test_starts_closed(self):
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=3, recovery_seconds=10)
        self.assertFalse(b.is_open())
        self.assertEqual(b.state, "closed")

    def test_opens_after_n_failures(self):
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=3, recovery_seconds=10)
        for _ in range(2):
            gen = b.acquire()
            self.assertIsNotNone(gen)
            b.record_result(gen, success=False)
        self.assertFalse(b.is_open(),
            "Should still be closed after 2 failures (threshold 3)")
        gen = b.acquire()
        b.record_result(gen, success=False)
        self.assertTrue(b.is_open(),
            "Should be open after 3 consecutive failures")

    def test_success_in_closed_resets_failure_counter(self):
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=3, recovery_seconds=10)
        for _ in range(2):
            gen = b.acquire()
            b.record_result(gen, success=False)
        gen = b.acquire()
        b.record_result(gen, success=True)
        for _ in range(2):
            gen = b.acquire()
            b.record_result(gen, success=False)
        self.assertFalse(b.is_open(),
            "Counter should have reset after success.")

    def test_acquire_returns_none_when_open(self):
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=2, recovery_seconds=60)
        gen1 = b.acquire(); b.record_result(gen1, success=False)
        gen2 = b.acquire(); b.record_result(gen2, success=False)
        self.assertTrue(b.is_open())
        self.assertIsNone(b.acquire(),
            "acquire() must return None when circuit is OPEN.")

    def test_recovery_transitions_to_half_open(self):
        """After recovery_seconds, the next acquire() must return a
        token (transitioning to HALF_OPEN). is_open() should reflect
        the half-open state honestly (returns False — calls allowed)."""
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=2, recovery_seconds=0.05)
        gen = b.acquire(); b.record_result(gen, success=False)
        gen = b.acquire(); b.record_result(gen, success=False)
        self.assertTrue(b.is_open())
        time.sleep(0.07)
        gen = b.acquire()
        self.assertIsNotNone(gen,
            "After recovery, acquire() must return a token "
            "(transitioning to half-open).")
        self.assertEqual(b.state, "half_open")

    def test_half_open_failure_reopens_immediately(self):
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=2, recovery_seconds=0.05)
        for _ in range(2):
            gen = b.acquire(); b.record_result(gen, success=False)
        self.assertTrue(b.is_open())
        time.sleep(0.07)
        gen = b.acquire()  # → HALF_OPEN
        b.record_result(gen, success=False)
        self.assertTrue(b.is_open(),
            "Single failure in HALF_OPEN must reopen.")

    def test_half_open_success_closes(self):
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=2, recovery_seconds=0.05)
        for _ in range(2):
            gen = b.acquire(); b.record_result(gen, success=False)
        time.sleep(0.07)
        gen = b.acquire()  # → HALF_OPEN
        b.record_result(gen, success=True)
        self.assertEqual(b.state, "closed")
        self.assertEqual(b.failure_count, 0)


class TestCircuitBreakerConcurrency(unittest.TestCase):
    """The bugs the adversarial review caught — all multi-threaded."""

    def test_half_open_allows_only_one_probe(self):
        """P0-1 regression. After auto-recovery, two threads racing
        to acquire() must NOT both pass through. Exactly one gets a
        token; the other gets None until the probe resolves."""
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=2, recovery_seconds=0.05)
        for _ in range(2):
            gen = b.acquire(); b.record_result(gen, success=False)
        time.sleep(0.07)

        # Race two threads into acquire(). Exactly one should get a
        # token; the other must get None because the probe is in
        # flight.
        results = []
        results_lock = threading.Lock()

        def race():
            gen = b.acquire()
            with results_lock:
                results.append(gen)

        threads = [threading.Thread(target=race) for _ in range(20)]
        for t in threads: t.start()
        for t in threads: t.join()

        non_none = [r for r in results if r is not None]
        self.assertEqual(len(non_none), 1,
            f"Exactly one thread should have gotten a HALF_OPEN "
            f"probe token; got {len(non_none)}. P0-1 regression.")

    def test_stale_record_result_ignored(self):
        """P0-2 regression. A successful record_result from an in-
        flight call that started BEFORE a state transition must NOT
        force the breaker closed."""
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=3, recovery_seconds=10)

        # T1 acquires (gen=g1), then before T1 records, the breaker
        # trips OPEN via 3 separate failures from T2.
        g1 = b.acquire()
        for _ in range(3):
            g = b.acquire()
            b.record_result(g, success=False)
        self.assertTrue(b.is_open())

        # Now T1 finally records SUCCESS with the stale generation.
        # Breaker MUST stay open.
        b.record_result(g1, success=True)
        self.assertTrue(b.is_open(),
            "Stale-generation success must not force OPEN→CLOSED. "
            "P0-2 regression.")

    def test_state_property_does_not_mutate(self):
        """P0-3 regression. Reading `state` must not auto-recover an
        OPEN breaker, even when recovery_seconds has elapsed."""
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=1, recovery_seconds=0.01)
        gen = b.acquire(); b.record_result(gen, success=False)
        time.sleep(0.05)  # recovery period elapsed
        # Read state many times — must not mutate.
        for _ in range(100):
            _ = b.state
            _ = repr(b)
            _ = b.failure_count
        self.assertEqual(b.state, "open",
            "state property must be a pure read; cannot transition "
            "OPEN→HALF_OPEN as a side effect of being read. "
            "P0-3 regression.")

    def test_concurrent_acquire_does_not_corrupt_state(self):
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=10000, recovery_seconds=60)
        N = 50
        ITER = 100

        def worker():
            for _ in range(ITER):
                gen = b.acquire()
                if gen is not None:
                    b.record_result(gen, success=True)

        threads = [threading.Thread(target=worker) for _ in range(N)]
        for t in threads: t.start()
        for t in threads: t.join()
        # All successes: counter must be 0 and state CLOSED.
        self.assertEqual(b.failure_count, 0)
        self.assertEqual(b.state, "closed")

    def test_concurrent_closed_burst_trips_and_late_acks_dropped(self):
        """Round-3 semantic: failure_count is STRICTLY 'consecutive
        failures in current CLOSED epoch.' When N>>threshold concurrent
        CLOSED-epoch failures arrive:
          a) Breaker trips OPEN (state correctness).
          b) After the trip, late CLOSED-epoch acks are dropped
             (generation mismatch) — failure_count resets to 0.
          c) For lifetime 'how many failures total?' metrics, a
             separate monotonic counter would be needed (future work).

        This is the deliberate trade-off after round-3 review found
        that 'count all failures' semantics led to unbounded growth
        and a counter that contradicted documented semantics."""
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=3, recovery_seconds=60)

        # 10 threads each acquire in CLOSED (gen=0).
        gens = [b.acquire() for _ in range(10)]
        for g in gens:
            self.assertIsNotNone(g)

        # All record failure. After the 3rd, state→OPEN, gen bumps,
        # _failures resets to 0. The remaining 7 acks have stale gen
        # → silently dropped.
        for g in gens:
            b.record_result(g, success=False)

        self.assertEqual(b.state, "open")
        self.assertEqual(b.failure_count, 0,
            "After trip, failure_count resets to 0 — late CLOSED-"
            "epoch acks are dropped (stale generation). "
            "Round-3 semantic.")

    def test_failure_count_resets_on_probe_failure(self):
        """Round-3 P0-1 regression. After CLOSED→OPEN→HALF_OPEN→OPEN
        (probe failed), failure_count must be 0, not stuck at the
        prior trip value. Otherwise the next CLOSED epoch starts
        with leftover noise."""
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=3, recovery_seconds=0.05)
        # Trip OPEN.
        for _ in range(3):
            g = b.acquire(); b.record_result(g, success=False)
        self.assertEqual(b.state, "open")
        self.assertEqual(b.failure_count, 0,
            "failure_count resets on CLOSED→OPEN transition.")
        # Recover, probe fails.
        time.sleep(0.07)
        g = b.acquire()
        self.assertEqual(b.state, "half_open")
        b.record_result(g, success=False)
        self.assertEqual(b.state, "open")
        self.assertEqual(b.failure_count, 0,
            "failure_count resets on HALF_OPEN→OPEN (probe failed) "
            "transition. Round-3 P0-1.")

    def test_late_closed_epoch_success_does_not_unstick_open(self):
        """Round-1 P0-2 regression check, restated for v2: a stale
        success from a CLOSED-epoch call must NOT force an OPEN
        breaker back to CLOSED."""
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=3, recovery_seconds=60)
        # All 5 acquire in CLOSED gen=0.
        gens = [b.acquire() for _ in range(5)]
        # Trip OPEN by failing 3 of them.
        for g in gens[:3]:
            b.record_result(g, success=False)
        self.assertEqual(b.state, "open")
        # Now the remaining 2 stale-epoch successes arrive.
        for g in gens[3:]:
            b.record_result(g, success=True)
        # Breaker MUST stay open.
        self.assertEqual(b.state, "open",
            "Stale CLOSED-epoch success must not force OPEN→CLOSED.")

    def test_full_state_transition_loop_under_real_contention(self):
        """Round-3 P1-3 regression. Real concurrent workers cycling
        the breaker through CLOSED → OPEN → HALF_OPEN → CLOSED.
        Uses a shared phase flag (not per-worker counters) so the
        switch from 'all failing' to 'all succeeding' is deterministic.

        Asserts: no exceptions/corruption + valid state at each
        phase end + at least one OPEN refusal observed."""
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=3, recovery_seconds=0.05,
                           probe_timeout_seconds=1.0)

        # Shared state: while True, workers fail; while False, succeed.
        failing_phase = threading.Event()
        failing_phase.set()
        stop = threading.Event()
        counters = {"refused": 0, "errors": 0}
        counters_lock = threading.Lock()

        def worker():
            while not stop.is_set():
                try:
                    gen = b.acquire()
                    if gen is None:
                        with counters_lock:
                            counters["refused"] += 1
                        time.sleep(0.001)
                        continue
                    b.record_result(gen, success=not failing_phase.is_set())
                except Exception:
                    with counters_lock:
                        counters["errors"] += 1

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads: t.start()

        # Phase 1: ~150ms of failures — should trip OPEN and stay OPEN.
        time.sleep(0.15)
        self.assertEqual(b.state, "open",
            "After failure burst, breaker should be OPEN.")
        self.assertGreater(counters["refused"], 0,
            "Refusals must have occurred during OPEN window.")

        # Phase 2: switch to success mode. Recovery_seconds=0.05;
        # within ~150ms, some HALF_OPEN probe should succeed and
        # close the breaker.
        failing_phase.clear()
        time.sleep(0.15)

        stop.set()
        for t in threads: t.join()

        self.assertEqual(counters["errors"], 0,
            "No exceptions or state corruption allowed.")
        # Round-4 A5: state may be CLOSED or HALF_OPEN at check
        # time — a probe could be in flight. Both are healthy.
        self.assertIn(b.state, ("closed", "half_open"),
            f"Expected CLOSED or HALF_OPEN after success phase; "
            f"got {b.state}.")


class TestCircuitBreakerLifetimeMetrics(unittest.TestCase):
    """Round-4 A1: dashboard observability for partial-broken APIs.
    `failure_count` resets on transitions; `metrics` is monotonic."""

    def test_metrics_count_every_ack_regardless_of_state(self):
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=3, recovery_seconds=60)
        # 5 concurrent CLOSED-epoch acquires.
        gens = [b.acquire() for _ in range(5)]
        for g in gens:
            b.record_result(g, success=False)
        # State trips on 3rd; remaining 2 are stale-gen → don't
        # affect _failures, but DO count toward total_failures.
        m = b.metrics
        self.assertEqual(m["total_failures"], 5,
            f"All 5 failures must count toward lifetime metric. "
            f"Got {m['total_failures']}.")
        self.assertEqual(b.failure_count, 0,
            "failure_count is the trip counter; resets on trip.")

    def test_metrics_count_refused_calls(self):
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=1, recovery_seconds=60)
        g = b.acquire(); b.record_result(g, success=False)
        # Now OPEN — every acquire returns None and counts as refused.
        for _ in range(7):
            self.assertIsNone(b.acquire())
        m = b.metrics
        self.assertEqual(m["total_calls_refused"], 7)


class TestCircuitBreakerProbeSlotLeak(unittest.TestCase):
    """Round-4 A2: BaseException paths must release the HALF_OPEN
    probe slot, not block recovery for probe_timeout_seconds."""

    def test_keyboard_interrupt_in_half_open_releases_probe(self):
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=1, recovery_seconds=0.05,
                           probe_timeout_seconds=30.0)
        g = b.acquire(); b.record_result(g, success=False)
        time.sleep(0.07)
        # Simulate Ctrl-C during the HALF_OPEN probe call.
        with self.assertRaises(KeyboardInterrupt):
            with b.call():
                raise KeyboardInterrupt()
        # The probe slot must have been released; another caller
        # should be able to acquire IMMEDIATELY (not after 30s).
        new_g = b.acquire()
        self.assertIsNotNone(new_g,
            "HALF_OPEN probe slot must be released on KeyboardInterrupt; "
            "otherwise a single Ctrl-C blocks recovery for 30s. "
            "Round-4 A2 regression.")

    def test_system_exit_in_half_open_releases_probe(self):
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=1, recovery_seconds=0.05,
                           probe_timeout_seconds=30.0)
        g = b.acquire(); b.record_result(g, success=False)
        time.sleep(0.07)
        with self.assertRaises(SystemExit):
            with b.call():
                raise SystemExit(1)
        new_g = b.acquire()
        self.assertIsNotNone(new_g)


class TestCircuitBreakerTypeContract(unittest.TestCase):
    """Round-4 A3: `failures_to_open=True` (bool, subclass of int)
    silently became 1. Reject explicitly."""

    def test_failures_to_open_rejects_bool(self):
        from circuit_breaker import CircuitBreaker
        with self.assertRaises(ValueError):
            CircuitBreaker(failures_to_open=True, recovery_seconds=10)
        with self.assertRaises(ValueError):
            CircuitBreaker(failures_to_open=False, recovery_seconds=10)

    def test_failures_to_open_rejects_float(self):
        from circuit_breaker import CircuitBreaker
        with self.assertRaises(ValueError):
            CircuitBreaker(failures_to_open=3.0, recovery_seconds=10)


class TestCircuitBreakerContextManager(unittest.TestCase):

    def test_call_records_success_on_normal_exit(self):
        from circuit_breaker import CircuitBreaker, CircuitBreakerOpen
        b = CircuitBreaker(failures_to_open=3, recovery_seconds=10)
        gen = b.acquire(); b.record_result(gen, success=False)
        gen = b.acquire(); b.record_result(gen, success=False)
        # 2 failures so far; one success via context manager should reset.
        with b.call():
            pass  # success
        # Now 2 more failures should NOT open (counter reset).
        gen = b.acquire(); b.record_result(gen, success=False)
        gen = b.acquire(); b.record_result(gen, success=False)
        self.assertFalse(b.is_open())

    def test_call_records_failure_on_exception(self):
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=2, recovery_seconds=10)
        with self.assertRaises(RuntimeError):
            with b.call():
                raise RuntimeError("simulated")
        self.assertEqual(b.failure_count, 1)

    def test_call_raises_when_open(self):
        from circuit_breaker import CircuitBreaker, CircuitBreakerOpen
        b = CircuitBreaker(failures_to_open=1, recovery_seconds=60)
        gen = b.acquire(); b.record_result(gen, success=False)
        with self.assertRaises(CircuitBreakerOpen):
            with b.call():
                pass


class TestCircuitBreakerArgValidation(unittest.TestCase):
    """Reject footgun configurations at construction."""

    def test_failures_to_open_must_be_positive(self):
        from circuit_breaker import CircuitBreaker
        with self.assertRaises(ValueError):
            CircuitBreaker(failures_to_open=0, recovery_seconds=10)
        with self.assertRaises(ValueError):
            CircuitBreaker(failures_to_open=-1, recovery_seconds=10)

    def test_recovery_seconds_must_be_non_negative(self):
        from circuit_breaker import CircuitBreaker
        with self.assertRaises(ValueError):
            CircuitBreaker(failures_to_open=3, recovery_seconds=-1)

    def test_probe_timeout_must_be_positive(self):
        """Round-2 P1-2 regression — zero probe_timeout reintroduces
        unlimited parallel probes."""
        from circuit_breaker import CircuitBreaker
        with self.assertRaises(ValueError):
            CircuitBreaker(failures_to_open=3, recovery_seconds=10,
                           probe_timeout_seconds=0)
        with self.assertRaises(ValueError):
            CircuitBreaker(failures_to_open=3, recovery_seconds=10,
                           probe_timeout_seconds=-1)

    def test_recovery_seconds_rejects_nan_and_inf(self):
        """Round-3 P1-1 regression — NaN/inf would leave the breaker
        stuck OPEN forever because comparisons silently fail."""
        from circuit_breaker import CircuitBreaker
        with self.assertRaises(ValueError):
            CircuitBreaker(failures_to_open=3,
                           recovery_seconds=float("nan"))
        with self.assertRaises(ValueError):
            CircuitBreaker(failures_to_open=3,
                           recovery_seconds=float("inf"))

    def test_probe_timeout_rejects_nan_and_inf(self):
        from circuit_breaker import CircuitBreaker
        with self.assertRaises(ValueError):
            CircuitBreaker(failures_to_open=3, recovery_seconds=10,
                           probe_timeout_seconds=float("nan"))
        with self.assertRaises(ValueError):
            CircuitBreaker(failures_to_open=3, recovery_seconds=10,
                           probe_timeout_seconds=float("inf"))


class TestCircuitBreakerInterruptHandling(unittest.TestCase):
    """Round-3 P1-2: BaseException catches were too broad — Ctrl-C
    or SystemExit during a healthy call would record as a failure."""

    def test_keyboard_interrupt_does_not_record_failure(self):
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=3, recovery_seconds=10)
        with self.assertRaises(KeyboardInterrupt):
            with b.call():
                raise KeyboardInterrupt()
        self.assertEqual(b.failure_count, 0,
            "KeyboardInterrupt is a control-flow signal, not a "
            "call failure. failure_count must remain 0.")

    def test_system_exit_does_not_record_failure(self):
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=3, recovery_seconds=10)
        with self.assertRaises(SystemExit):
            with b.call():
                raise SystemExit(1)
        self.assertEqual(b.failure_count, 0)

    def test_regular_exception_still_records_failure(self):
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=3, recovery_seconds=10)
        with self.assertRaises(ValueError):
            with b.call():
                raise ValueError("simulated API error")
        self.assertEqual(b.failure_count, 1)


class TestCircuitBreakerHalfOpenTimeout(unittest.TestCase):
    """If a caller forgets record_result in HALF_OPEN, the breaker
    must not be permanently blocked from probing again."""

    def test_half_open_probe_times_out(self):
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=1, recovery_seconds=0.05,
                           probe_timeout_seconds=0.05)
        gen = b.acquire(); b.record_result(gen, success=False)
        time.sleep(0.07)
        # Acquire HALF_OPEN probe but never record_result.
        probe_gen = b.acquire()
        self.assertIsNotNone(probe_gen)
        # While probe is in-flight, second acquire returns None.
        self.assertIsNone(b.acquire())
        # After probe_timeout elapses, a new probe is allowed.
        time.sleep(0.07)
        new_gen = b.acquire()
        self.assertIsNotNone(new_gen,
            "Half-open probe must time out so a forgotten "
            "record_result doesn't permanently block recovery.")


class TestCircuitBreakerRegistry(unittest.TestCase):

    def test_returns_same_breaker_for_same_key(self):
        from circuit_breaker import CircuitBreakerRegistry
        r = CircuitBreakerRegistry()
        b1 = r.get("kalshi_events_KXNBAGAME")
        b2 = r.get("kalshi_events_KXNBAGAME")
        self.assertIs(b1, b2)

    def test_returns_different_breaker_for_different_keys(self):
        from circuit_breaker import CircuitBreakerRegistry
        r = CircuitBreakerRegistry()
        b1 = r.get("kalshi_events_KXNBAGAME")
        b2 = r.get("kalshi_events_KXMLBGAME")
        self.assertIsNot(b1, b2)

    def test_first_call_uses_provided_kwargs(self):
        from circuit_breaker import CircuitBreakerRegistry
        r = CircuitBreakerRegistry()
        b = r.get("test_endpoint",
                  failures_to_open=5, recovery_seconds=600)
        for _ in range(4):
            gen = b.acquire(); b.record_result(gen, success=False)
        self.assertFalse(b.is_open())
        gen = b.acquire(); b.record_result(gen, success=False)
        self.assertTrue(b.is_open())

    def test_registry_thread_safe(self):
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
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(set(results)), 1)


class TestCircuitBreakerObservability(unittest.TestCase):

    def test_state_returns_string_label(self):
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=1, recovery_seconds=10)
        self.assertEqual(b.state, "closed")
        gen = b.acquire(); b.record_result(gen, success=False)
        self.assertEqual(b.state, "open")

    def test_failure_count_observable(self):
        from circuit_breaker import CircuitBreaker
        b = CircuitBreaker(failures_to_open=10, recovery_seconds=10)
        self.assertEqual(b.failure_count, 0)
        gen = b.acquire(); b.record_result(gen, success=False)
        gen = b.acquire(); b.record_result(gen, success=False)
        self.assertEqual(b.failure_count, 2)


if __name__ == "__main__":
    unittest.main()
