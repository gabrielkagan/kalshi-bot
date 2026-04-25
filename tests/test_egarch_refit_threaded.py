"""Regression — Apr 25 01:02 UTC SLOW_SCAN_TICK 54.37s.

After threading three other periodic-task blockers (WS drift probe
in 7dac681, SettlementTracker.tick in 8114ddc, market refresh in
f216a8d), one stall remained but only fired periodically every
~15-20 minutes:

    01:02:24 PERIODIC_TASK_SLOW: egarch_refit took 53.30s
    01:02:24 SLOW_SCAN_TICK: 54.37s since previous tick start

The EGARCH MLE refit (Maximum Likelihood Estimation across 4 assets,
scipy L-BFGS-B optimization) blocks the main thread for ~50s every
time it runs (per-asset interval 1-2 hours, but with 4 assets
staggered the cluster fires more often).

Fix: same pattern as the other 3 threading fixes. _tick() spawns a
daemon worker thread for `egarch_estimator.maybe_refit()`. A
`_egarch_refit_running` flag prevents thread pile-up if a refit
takes longer than _tick's call interval.

Thread safety: maybe_refit writes `self._params[asset] = new_params`
which is atomic (Python dict-item assignment under GIL). Readers in
EGARCHEstimator.update() read `self._params[asset]` and copy via
`dict(...)` — see either old or new params, never partial. Disk
saves via _save_state() are pre-existing, also called from this
worker; no new race introduced.

This test asserts:
  1. MainLoop has a `_egarch_refit_running` flag (re-entry guard).
  2. _tick() spawns a `threading.Thread` for the egarch refit branch.
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.py")


def _find_tick_method() -> ast.FunctionDef:
    with open(BOT_PY) as f:
        tree = ast.parse(f.read())
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef) and cls.name == "MainLoop":
            for node in cls.body:
                if (isinstance(node, ast.FunctionDef)
                        and node.name == "_tick"):
                    return node
    raise AssertionError("MainLoop._tick not found")


class TestEGARCHRefitIsThreaded(unittest.TestCase):

    def test_main_loop_has_egarch_refit_running_guard(self):
        with open(BOT_PY) as f:
            src = f.read()
        self.assertIn(
            "_egarch_refit_running", src,
            "MainLoop must declare a `_egarch_refit_running` flag to "
            "prevent multiple EGARCH MLE refit threads from running "
            "simultaneously. Same pattern as _market_refresh_running "
            "(f216a8d) and SettlementTracker._worker_running "
            "(8114ddc). See Apr 25 01:02 SLOW_SCAN_TICK 54.37s "
            "incident.")

    def test_tick_spawns_thread_in_egarch_refit_branch(self):
        """The egarch-refit branch in _tick() must spawn a daemon
        thread for maybe_refit()."""
        tick = _find_tick_method()

        # Find an If or expression that calls
        # `self.egarch_estimator.maybe_refit()`. The threading.Thread
        # ctor must be in the same logical group.
        refit_branch = None
        for node in ast.walk(tick):
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "maybe_refit"):
                    # Walk up to the enclosing If statement
                    if isinstance(node, ast.If):
                        refit_branch = node
                        break
            if refit_branch is not None:
                break

        self.assertIsNotNone(
            refit_branch,
            "_tick() must contain an `if self.egarch_estimator: ...` "
            "branch with maybe_refit. None found.")

        # Inside that branch, look for threading.Thread(...) ctor.
        thread_ctors = []
        for sub in ast.walk(refit_branch):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            if (isinstance(f, ast.Attribute) and f.attr == "Thread"
                    and isinstance(f.value, ast.Name)
                    and f.value.id == "threading"):
                thread_ctors.append(sub)
        self.assertGreaterEqual(
            len(thread_ctors), 1,
            "EGARCH refit branch in _tick must launch a "
            "`threading.Thread(target=..., daemon=True).start()` for "
            "maybe_refit. Apr 25 01:02 incident: synchronous refit "
            "blocked main thread for 53.30s.")


if __name__ == "__main__":
    unittest.main()
