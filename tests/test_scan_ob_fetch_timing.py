"""Diagnostic instrumentation — Apr 25 01:28 UTC SCAN_WINDOW_SLOW
localized to a single XRP window taking 5.73s.

After Phase 1 (per-window timer in commit 98d5249) confirmed Pattern
B (one window dominates rather than distributed cost across 22+
windows), the next surgical fix needs to know WHERE within that
single window iteration the 5+ seconds is spent. Top suspect:
synchronous REST orderbook fetch via `_get_orderbook_cached(ticker)`
on a Kalshi REST timeout/retry path.

This commit adds `SCAN_OB_FETCH_SLOW` timing around the main
orderbook fetch site (bot/_impl.py ~8547) inside the per-window loop.
Threshold 500ms — well above normal cache hit (<1ms) or healthy
REST (~200ms), but flags any retry or hung-call behavior.

Pattern interpretation after deploy:
  - SCAN_OB_FETCH_SLOW fires + SCAN_WINDOW_SLOW fires same window:
    orderbook fetch IS the dominant per-window cost. Surgical fix
    is to reduce REST timeout / add explicit deadline / parallelize.
  - SCAN_OB_FETCH_SLOW silent + SCAN_WINDOW_SLOW fires: the cost is
    elsewhere in the iteration (vol cold path, DB lock, etc.).
    Different fix needed.

Test-first per usual TDD rhythm. See
kb/failures/scan-tick-stall-cluster-2026-04-25.md item #3 in the
Tomorrow's checklist (now being addressed tonight).
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot", "scanner", "__init__.py")


class TestScanOBFetchTiming(unittest.TestCase):

    def test_scan_ob_fetch_slow_log_exists(self):
        with open(BOT_PY) as f:
            src = f.read()
        self.assertIn(
            "SCAN_OB_FETCH_SLOW", src,
            "scan() must emit SCAN_OB_FETCH_SLOW timer warning around "
            "the orderbook fetch in the per-window loop. Apr 25 01:28 "
            "investigation: SCAN_WINDOW_SLOW localized to ONE window "
            "(XRP, 5.73s); need to confirm orderbook fetch is the "
            "dominant per-window cost.")

    def test_ob_fetch_log_includes_ticker(self):
        """Log must name the ticker so we can correlate slow fetches
        with WS_DRIFT_AUTO_FLAG events on specific tickers."""
        src = ""
        if os.path.exists(BOT_PY):
            with open(BOT_PY) as f:
                src = f.read()
        idx = 0
        found = False
        while True:
            idx = src.find("SCAN_OB_FETCH_SLOW", idx)
            if idx < 0:
                break
            window = src[idx:idx + 400]
            if "ticker=" in window or "%s" in window:
                found = True
                break
            idx += 1
        self.assertTrue(
            found,
            "SCAN_OB_FETCH_SLOW log must name the ticker for "
            "correlation with WS_DRIFT_AUTO_FLAG / REST fallback "
            "state.")


if __name__ == "__main__":
    unittest.main()
