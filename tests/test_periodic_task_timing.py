"""Regression — Apr 25 00:30 UTC SLOW_SCAN_TICK still firing after PM
Fix 5 thread fix. The 2s WS drift sleep was only one of several
synchronous blockers in `_tick()`. Forensics named the cluster:
`_refresh_active_windows` (9+ REST calls), `tracker.tick`
(settlement scan over 400+ pending rows), Supabase HTTP, weather
refresh. We don't yet know which is dominant.

This test asserts that each suspected periodic task in `_tick()` is
wrapped with timing instrumentation that emits a `PERIODIC_TASK_SLOW`
warning when its wall time exceeds a per-task threshold. The next
round of journalctl logs will then name the dominant blocker by
task, and we can thread the worst offender.

Test-first: this fails RED until the instrumentation is in place.
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot/main_loop.py")


def _find_tick_method() -> ast.FunctionDef:
    with open(BOT_PY) as f:
        tree = ast.parse(f.read())
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef):
            for node in cls.body:
                if (isinstance(node, ast.FunctionDef)
                        and node.name == "_tick"):
                    return node
    raise AssertionError("_tick() method not found in bot/_impl.py")


class TestTickHasTimingInstrumentation(unittest.TestCase):
    """`_tick()` must contain a `PERIODIC_TASK_SLOW` log emission so
    that when stalls happen, journalctl tells us *which* periodic
    task is to blame. Without per-task timing the SLOW_SCAN_TICK
    warning is symptom-only (we know there's a stall but not its
    cause)."""

    def test_tick_contains_periodic_task_slow_log(self):
        with open(BOT_PY) as f:
            src = f.read()
        self.assertIn("PERIODIC_TASK_SLOW", src,
                      "_tick() must emit `PERIODIC_TASK_SLOW` warnings "
                      "for individual periodic tasks (refresh windows, "
                      "tracker.tick, etc.) to identify the dominant "
                      "main-thread blocker. See Apr 25 00:30 UTC "
                      "SLOW_SCAN_TICK incident — threading the WS drift "
                      "probe alone didn't drop stall rate.")

    def test_tick_uses_perf_counter_for_timing(self):
        """Per-task timing must use `time.perf_counter()` (monotonic,
        high-resolution) — not `time.time()` (subject to NTP jumps)."""
        tick = _find_tick_method()
        perf_counter_calls = []
        for sub in ast.walk(tick):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            if (isinstance(f, ast.Attribute)
                    and f.attr == "perf_counter"
                    and isinstance(f.value, ast.Name)
                    and f.value.id == "time"):
                perf_counter_calls.append(sub)
        self.assertGreaterEqual(
            len(perf_counter_calls), 2,
            "_tick() should call `time.perf_counter()` at least twice "
            "(start + end) per timed periodic task. Found "
            f"{len(perf_counter_calls)} occurrences.")

    def test_each_major_periodic_task_has_a_timed_wrapper(self):
        """The cluster of synchronous blockers fired in scan() every
        ~30s must each have a timing wrapper. Verifying by source
        match: we need PERIODIC_TASK_SLOW log lines that name the
        suspect tasks so the next deploy's logs tell us which is
        dominating."""
        with open(BOT_PY) as f:
            src = f.read()
        # Each of these names must appear in a logging call within
        # _tick(). We grep the file for the name strings; the
        # PERIODIC_TASK_SLOW log_message will contain the task name.
        for task_name in (
            "refresh_active_windows",
            "subscribe_discovery_orderbooks",
            "tracker_tick",
        ):
            self.assertIn(
                task_name, src,
                f"Expected `{task_name}` to appear as a timing label "
                f"in _tick(). The PERIODIC_TASK_SLOW emission needs a "
                f"task-name argument so journalctl shows which one "
                f"dominates.")


if __name__ == "__main__":
    unittest.main()
