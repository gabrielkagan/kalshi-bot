"""Diagnostic instrumentation — Apr 25 00:55 UTC.

After threading the WS drift probe (7dac681), SettlementTracker
(8114ddc), and market refresh (f216a8d), SLOW_SCAN_TICK was
expected to drop below 2s. It didn't — a 6.13s gap fired at
00:55:16 with ZERO PERIODIC_TASK_SLOW events.

That means the dominant blocker is NOT in the periodic-task
cluster but somewhere else:
  (a) inside scan() body itself (iterating 230+ active_windows
      × per-market evaluation × DB writes), or
  (b) between scan() and the next iteration (the per-window
      vol.update + scan_journal.jsonl writes in _tick body, or
      the POSITION_PRICE_MONITOR loop, etc.)

This commit instruments scan() body itself with a duration timer
that emits `SCAN_BODY_SLOW` when scan() execution exceeds 1.5s.
With this, the next deploy's logs will tell us:
  - If SCAN_BODY_SLOW fires: scan() body is the blocker (fix
    iteration / DB write / sizing math)
  - If SCAN_BODY_SLOW silent but SLOW_SCAN_TICK still fires: the
    blocker is in _tick() outside scan() (fix that next)

Same TDD rhythm as commit eb5960e (per-task instrumentation)
which immediately named SettlementTracker as the dominant
blocker on the next deploy.
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot/_impl.py")


def _find_tick_method() -> ast.FunctionDef:
    """Find MainLoop._tick — the orchestrator that calls scanner.scan.
    The call site is the cleanest place to time scan() body since the
    method itself is 4700 lines with 4 return points; wrapping it
    externally is non-invasive."""
    with open(BOT_PY) as f:
        tree = ast.parse(f.read())
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef) and cls.name == "MainLoop":
            for node in cls.body:
                if (isinstance(node, ast.FunctionDef)
                        and node.name == "_tick"):
                    return node
    raise AssertionError("MainLoop._tick not found")


class TestScanBodyDurationLog(unittest.TestCase):

    def test_scan_body_slow_log_string_exists(self):
        """SCAN_BODY_SLOW must be emitted when scanner.scan() takes
        more than the threshold. Without this we can't distinguish
        scan-body slowness from gap-between-calls slowness."""
        with open(BOT_PY) as f:
            src = f.read()
        self.assertIn(
            "SCAN_BODY_SLOW", src,
            "_tick() must emit SCAN_BODY_SLOW warning when "
            "scanner.scan() body exceeds 1.5s — needed to distinguish "
            "scan-body slowness from gap-between-calls slowness. See "
            "Apr 25 00:55 SLOW_SCAN_TICK 6.13s investigation.")

    def test_tick_wraps_scan_call_in_try_finally(self):
        """The scan() call in _tick must be wrapped in try/finally so
        the duration log fires even when scan() raises or returns
        early — guarantees we always measure."""
        tick = _find_tick_method()

        # Find any try/finally that contains a call to scanner.scan
        wrapping_try_finally = []
        for sub in ast.walk(tick):
            if not (isinstance(sub, ast.Try) and sub.finalbody):
                continue
            for inner in ast.walk(sub):
                if (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "scan"):
                    # confirm it's `<x>.scanner.scan(...)`-shaped
                    if (isinstance(inner.func.value, ast.Attribute)
                            and inner.func.value.attr == "scanner"):
                        wrapping_try_finally.append(sub)
                        break
        self.assertGreaterEqual(
            len(wrapping_try_finally), 1,
            "_tick() must wrap `self.scanner.scan(...)` in a "
            "try/finally so SCAN_BODY_SLOW fires even when scan() "
            "raises. Found no such wrapper.")

    def test_tick_uses_perf_counter_around_scan_call(self):
        tick = _find_tick_method()
        perf_counter_calls = 0
        for sub in ast.walk(tick):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            if (isinstance(f, ast.Attribute)
                    and f.attr == "perf_counter"
                    and isinstance(f.value, ast.Name)
                    and f.value.id == "time"):
                perf_counter_calls += 1
        # _tick already has perf_counter calls for periodic-task
        # timing (commit eb5960e). Adding scan-body timing adds one
        # more for the scan() start. Total expected >= many.
        self.assertGreaterEqual(
            perf_counter_calls, 4,
            "_tick should call `time.perf_counter()` for periodic-"
            "task timing (existing) AND scan-body timing (new). "
            f"Found {perf_counter_calls} occurrences.")


if __name__ == "__main__":
    unittest.main()
