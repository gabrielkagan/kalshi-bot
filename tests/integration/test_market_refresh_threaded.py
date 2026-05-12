"""Regression — Apr 25 00:45 UTC residual SLOW_SCAN_TICK 2.2s gaps.

After threading SettlementTracker.tick (commit 8114ddc) the dominant
5s blocker is gone, but SLOW_SCAN_TICK still fires every 30s with
2.2-2.4s gaps. The next dominant task per `eb5960e` instrumentation
is `refresh_active_windows` (1.51s observed), which makes 9+ REST
calls to Kalshi (4× 15M series + 4× hourly + weather + SPX windows).

Fix: same pattern as 7dac681 (WS drift probe) and 8114ddc
(SettlementTracker.tick). MainLoop._tick() spawns a daemon worker
thread that calls _refresh_active_windows() + the subscribe step.
A `_market_refresh_running` flag prevents thread pile-up.

Thread safety: _active_windows is reassigned atomically. Readers
(scan() main thread, dashboard) see either the old list or the new
one — never a partial state. _last_market_refresh is a float, also
atomic.

This test asserts:
  1. MainLoop has a `_market_refresh_running` flag (re-entry guard).
  2. _tick() spawns a `threading.Thread` for the market refresh
     branch (not synchronous like before).
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "bot/main_loop.py")


def _find_tick_method() -> ast.FunctionDef:
    """Find MainLoop._tick (NOT SettlementTracker.tick or
    OrderExecutor.tick — the one that orchestrates per-cycle work)."""
    if os.path.exists(BOT_PY):
        with open(BOT_PY) as f:
            tree = ast.parse(f.read())
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef) and cls.name == "MainLoop":
            for node in cls.body:
                if (isinstance(node, ast.FunctionDef)
                        and node.name == "_tick"):
                    return node
    raise AssertionError("MainLoop._tick not found")


class TestMarketRefreshIsThreaded(unittest.TestCase):

    def test_main_loop_has_market_refresh_running_guard(self):
        src = ""
        if os.path.exists(BOT_PY):
            with open(BOT_PY) as f:
                src = f.read()
        self.assertIn(
            "_market_refresh_running", src,
            "MainLoop must declare a `_market_refresh_running` flag "
            "(or equivalent) to prevent multiple market-refresh "
            "worker threads from running simultaneously.")

    def test_tick_spawns_thread_in_market_refresh_branch(self):
        """The market-refresh branch in _tick() (gated by
        `MARKET_REFRESH_SECONDS`) must spawn a daemon thread for
        _refresh_active_windows + _subscribe_discovery_orderbooks."""
        tick = _find_tick_method()

        # Find the if-branch that gates the market refresh interval.
        # Pattern: any If statement whose body contains a Call to
        # `_refresh_active_windows`. Inside that branch body, look for
        # a `threading.Thread(...).start()` call.
        refresh_if_node = None
        for node in ast.walk(tick):
            if not isinstance(node, ast.If):
                continue
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "_refresh_active_windows"):
                    refresh_if_node = node
                    break
            if refresh_if_node is not None:
                break

        self.assertIsNotNone(
            refresh_if_node,
            "_tick() must contain an `if` branch that calls "
            "_refresh_active_windows. None found.")

        # Inside that branch (whole subtree), there must be a
        # threading.Thread(...) ctor.
        thread_ctors = []
        for sub in ast.walk(refresh_if_node):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            if (isinstance(f, ast.Attribute) and f.attr == "Thread"
                    and isinstance(f.value, ast.Name)
                    and f.value.id == "threading"):
                thread_ctors.append(sub)
        self.assertGreaterEqual(
            len(thread_ctors), 1,
            "Market-refresh branch in _tick must launch a "
            "`threading.Thread(target=..., daemon=True).start()` for "
            "_refresh_active_windows + subscribe. See Apr 25 00:45 "
            "SLOW_SCAN_TICK 2.2s residual incident.")


if __name__ == "__main__":
    unittest.main()
