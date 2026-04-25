"""Regression — Apr 25 00:21 UTC SLOW_SCAN_TICK incident.

`OpportunityScanner._drift_probe_tick()` had `time.sleep(2.0)` in
the main scan() path to wait between REST_1 and REST_2 of the
stability probe (bot.py line 14267). When WS_DRIFT_AUTO_FLAG fires
(every minute), this 2s sleep blocks the main loop. Combined with
the rest of the periodic-task sweep (market discovery, ESPN fetch,
weather refresh, settlement scan) it cascades into 6-10s
SLOW_SCAN_TICK warnings and clock_drift_detected events — the same
event-loop-stall pattern the original PM Fix 5 deferred.

Fix: the second REST fetch + comparison is moved off the main
thread (background daemon thread). Probe phase A (REST_1, drift log,
auto-flag) stays on the main thread; phase B (REST_2, stability log)
runs in the background.

This test asserts:
  1. `_drift_probe_tick` does NOT call `time.sleep` synchronously
     anywhere in its body.
  2. The body contains a `threading.Thread(...).start()` call,
     proving the deferred work is moved off-thread.

If a future refactor reintroduces a synchronous sleep, this test
fails. See ws-cache-drift-silent-scan-2026-04-24 PM Fix 5 (deferred
clock-drift investigation, completed Apr 25 incident-driven).
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.py")


def _find_drift_probe_tick() -> ast.FunctionDef:
    with open(BOT_PY) as f:
        tree = ast.parse(f.read())
    for cls in ast.walk(tree):
        if (isinstance(cls, ast.ClassDef)
                and cls.name == "OpportunityScanner"):
            for node in cls.body:
                if (isinstance(node, ast.FunctionDef)
                        and node.name == "_drift_probe_tick"):
                    return node
    raise AssertionError(
        "OpportunityScanner._drift_probe_tick not found in bot.py")


class TestDriftProbeIsNonBlocking(unittest.TestCase):

    def test_no_synchronous_sleep_in_drift_probe_body(self):
        """`time.sleep(...)` MUST NOT appear in the top-level (main-
        thread) body of `_drift_probe_tick`. Sleeps inside nested
        `def _worker(...)` functions are fine — those run in a
        spawned thread. Apr 25 00:21 SLOW_SCAN_TICK incident showed
        `time.sleep(2.0)` on the main path cascaded into 6-10s
        main-thread stalls."""
        probe = _find_drift_probe_tick()

        # Walk only top-level — skip nested FunctionDef/AsyncFunctionDef
        # bodies so worker-function sleeps don't false-positive.
        def _walk_main_thread(node):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.Lambda)):
                    continue  # nested — runs elsewhere
                yield child
                yield from _walk_main_thread(child)

        sleep_calls = []
        for sub in _walk_main_thread(probe):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            if (isinstance(f, ast.Attribute) and f.attr == "sleep"
                    and isinstance(f.value, ast.Name)
                    and f.value.id == "time"):
                sleep_calls.append(sub)
        self.assertEqual(
            sleep_calls, [],
            "`_drift_probe_tick` must not call `time.sleep` "
            "synchronously on the main thread — defer the stability "
            "re-probe to a worker thread. See "
            "ws-cache-drift-silent-scan-2026-04-24 PM Fix 5 / Apr 25 "
            "00:21 SLOW_SCAN_TICK incident.")

    def test_drift_probe_starts_a_background_thread(self):
        """Stability re-probe must be moved to a background thread.
        Asserts the body contains a `Thread(...)` constructor call
        followed by `.start()`."""
        probe = _find_drift_probe_tick()
        thread_starts = []
        for sub in ast.walk(probe):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            # Match `<Thread instance>.start()` calls
            if isinstance(f, ast.Attribute) and f.attr == "start":
                thread_starts.append(sub)
        self.assertGreaterEqual(
            len(thread_starts), 1,
            "`_drift_probe_tick` must launch a background thread for "
            "the 2s stability re-probe instead of blocking. Look for "
            "`threading.Thread(target=..., daemon=True).start()`.")
        # Also assert that `threading` is referenced inside the body
        thread_ctors = []
        for sub in ast.walk(probe):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            if (isinstance(f, ast.Attribute) and f.attr == "Thread"
                    and isinstance(f.value, ast.Name)
                    and f.value.id == "threading"):
                thread_ctors.append(sub)
        self.assertGreaterEqual(
            len(thread_ctors), 1,
            "Expected `threading.Thread(...)` constructor inside "
            "_drift_probe_tick body to spawn the deferred re-probe.")


if __name__ == "__main__":
    unittest.main()
