"""Regression — Apr 25 00:39 UTC. After threading the WS drift probe
(commit 7dac681) and instrumenting periodic-task timing (commit
eb5960e), the per-task logs named the dominant main-thread blocker:
SettlementTracker.tick() takes 4.84-5.54s every cycle, accounting
for ~70% of the SLOW_SCAN_TICK gap.

Mechanism: tracker.tick() runs `_poll()` + `_poll_rejections()` +
`_poll_evaluated_opportunities()` synchronously. With 440 pending
evaluated_opportunities rows accumulated, the third call alone is
multi-second.

Fix: tracker.tick() spawns a daemon worker thread that runs the
body. A `_worker_running` flag prevents thread pile-up (next tick
skips if previous is still running). Main thread call returns in
microseconds.

This test asserts:
  1. SettlementTracker has a `_worker_running` attribute (re-entry guard).
  2. SettlementTracker.tick() body contains a `threading.Thread`
     constructor + `.start()`.
  3. The poll calls (`_poll`, `_poll_rejections`,
     `_poll_evaluated_opportunities`) appear inside a worker function
     definition (nested) — not directly in the top-level body of
     tick().

If a future refactor reverts to synchronous, this fails. See
ws-cache-drift-silent-scan-2026-04-24 PM Fix 5 / Apr 25 00:39
SLOW_SCAN_TICK incident.
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.py")


def _find_tracker_tick() -> ast.FunctionDef:
    """Return the AST node for SettlementTracker.tick (the one with
    the SETTLEMENT_CHECK_SECONDS throttle, NOT OrderExecutor.tick)."""
    with open(BOT_PY) as f:
        tree = ast.parse(f.read())
    for cls in ast.walk(tree):
        if (isinstance(cls, ast.ClassDef)
                and cls.name == "SettlementTracker"):
            for node in cls.body:
                if (isinstance(node, ast.FunctionDef)
                        and node.name == "tick"):
                    return node
    raise AssertionError(
        "SettlementTracker.tick not found in bot.py")


class TestTrackerTickIsThreaded(unittest.TestCase):

    def test_tick_spawns_a_background_thread(self):
        """SettlementTracker.tick() must launch a daemon thread for
        its work. Apr 25 00:39 incident: synchronous tick was 4.84-
        5.54s per cycle, dominating the main-thread stall."""
        tick = _find_tracker_tick()
        thread_starts = []
        for sub in ast.walk(tick):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            if isinstance(f, ast.Attribute) and f.attr == "start":
                thread_starts.append(sub)
        self.assertGreaterEqual(
            len(thread_starts), 1,
            "SettlementTracker.tick() must launch a `threading."
            "Thread(target=..., daemon=True).start()` for its body. "
            "See Apr 25 00:39 SLOW_SCAN_TICK incident.")

    def test_tick_uses_threading_thread_constructor(self):
        tick = _find_tracker_tick()
        thread_ctors = []
        for sub in ast.walk(tick):
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
            "SettlementTracker.tick body to defer settlement "
            "processing off the main thread.")

    def test_poll_calls_are_inside_nested_function_not_top_level(self):
        """The expensive _poll/_poll_rejections/
        _poll_evaluated_opportunities calls must NOT appear in the
        top-level body of tick() (would mean still synchronous).
        They should be inside a nested worker function."""
        tick = _find_tracker_tick()

        def _walk_main_body(node):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef,
                                      ast.AsyncFunctionDef,
                                      ast.Lambda)):
                    continue  # nested — runs in thread
                yield child
                yield from _walk_main_body(child)

        offending = []
        for sub in _walk_main_body(tick):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            if (isinstance(f, ast.Attribute)
                    and f.attr in ("_poll", "_poll_rejections",
                                   "_poll_evaluated_opportunities")
                    and isinstance(f.value, ast.Name)
                    and f.value.id == "self"):
                offending.append(f.attr)
        self.assertEqual(
            offending, [],
            f"These poll calls are still synchronous in tick(): "
            f"{offending}. They must be inside the nested worker "
            f"function (which runs in a daemon thread).")

    def test_tick_has_worker_running_reentry_guard(self):
        """A `_worker_running` flag (or equivalent) must prevent
        multiple settlement worker threads from running
        simultaneously. Without this, a slow worker that takes
        longer than SETTLEMENT_CHECK_SECONDS would spawn duplicate
        threads each cycle."""
        with open(BOT_PY) as f:
            src = f.read()
        self.assertIn(
            "_worker_running", src,
            "SettlementTracker must use a `_worker_running` flag "
            "(or equivalent) to prevent thread pile-up if tick body "
            "takes longer than SETTLEMENT_CHECK_SECONDS.")


if __name__ == "__main__":
    unittest.main()
