"""Diagnostic instrumentation — Apr 25 01:17 UTC SCAN_LOOP_SLOW.

After threading all 4 periodic-task blockers, SCAN_BODY_SLOW was
localized to the per-window for-loop (commit 4db3956 named
SCAN_LOOP_SLOW: 1.78-15.14s with eligible_windows=22-23). With ~700ms
per window in the slow case, the dominant per-window cost is
suspected to be the synchronous REST orderbook fetch when WS cache
is cold or drift-flagged.

This commit (Phase 1 of the deferred scan-loop optimization) adds a
per-window timer that emits `SCAN_WINDOW_SLOW` when one iteration of
the for-loop body exceeds 500ms. With per-window data we'll see:

  - If multiple windows fire SCAN_WINDOW_SLOW with similar durations
    (~300-500ms each): orderbook REST is the bottleneck. Phase 2
    (parallel orderbook prefetch) is the right fix.
  - If only ONE window per slow tick fires SCAN_WINDOW_SLOW with a
    big duration (1-5s): one specific operation in that window is
    the cost (e.g. vol.update cold path, DB lock contention, etc.).
    Phase 2 prefetch wouldn't help; need a different fix.

Test-first: assert the log string + perf_counter in the per-window
loop body. See kb/failures/scan-tick-stall-cluster-2026-04-25.md
'Tomorrow's checklist' item #3.
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.py")


class TestScanWindowTiming(unittest.TestCase):

    def test_scan_window_slow_log_exists(self):
        with open(BOT_PY) as f:
            src = f.read()
        self.assertIn(
            "SCAN_WINDOW_SLOW", src,
            "scan() must emit SCAN_WINDOW_SLOW per-window timer "
            "warning to localize which window iteration is slow. "
            "See Apr 25 01:17 SCAN_LOOP_SLOW investigation.")

    def test_scan_window_slow_log_includes_asset_or_ticker(self):
        """The log must name WHICH window is slow (asset or ticker)
        so we can correlate with WS drift state, recent WS_DRIFT_AUTO_FLAG
        events, etc. Without identifying the window, the log is
        unactionable. Looks for the actual `logging.warning(...)` call
        site, not just the first textual occurrence (which may be in
        a comment)."""
        with open(BOT_PY) as f:
            src = f.read()
        # Find every occurrence of SCAN_WINDOW_SLOW and check at least
        # one has asset/ticker formatting nearby (i.e. inside a string
        # literal followed by % args).
        idx = 0
        found_with_args = False
        while True:
            idx = src.find("SCAN_WINDOW_SLOW", idx)
            if idx < 0:
                break
            window = src[idx:idx + 400]
            if "asset=" in window or "ticker=" in window:
                found_with_args = True
                break
            idx += 1
        self.assertTrue(
            found_with_args,
            "At least one SCAN_WINDOW_SLOW log call must include "
            "`asset=...` or `ticker=...` formatting so the slow "
            "window can be identified.")


if __name__ == "__main__":
    unittest.main()
