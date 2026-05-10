"""Diagnostic instrumentation — Apr 25 01:35 UTC.

After ruling out orderbook fetch (zero SCAN_OB_FETCH_SLOW events,
correlated with 4 SCAN_WINDOW_SLOW events including one 10.43s
BTC stall), the cost is somewhere ELSE in the per-window iteration
body. No DB lock errors either.

Remaining suspects in the iteration body:
  1. `vol.update(asset)` — RV/EGARCH compute (could be slow if
     buffer is being recomputed or RK bandwidth is searching)
  2. DB inserts via `insert_evaluated_opportunity` (multiple per
     iteration; with WAL + busy_timeout, contention with threaded
     workers could add latency)
  3. `sizer.compute()` + sizing scalers
  4. `evaluate_execution_strategy()`

This commit adds three more per-section timers inside the
iteration body:
  - SCAN_VOL_SLOW: around vol.update call
  - SCAN_DBWRITE_SLOW: around the heaviest insert_evaluated_opportunity
    block (zero_sizing site, which writes the richest row with ~50
    fields)
  - SCAN_SIZING_SLOW: around the sizer.compute call

Test-first per usual TDD rhythm. Next deploy's logs name which
sub-section dominates.
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot", "scanner", "__init__.py")


class TestScanSectionInsideIteration(unittest.TestCase):

    def test_scan_vol_slow_log_exists(self):
        with open(BOT_PY) as f:
            src = f.read()
        self.assertIn(
            "SCAN_VOL_SLOW", src,
            "scan() must emit SCAN_VOL_SLOW timer around vol.update "
            "in the per-window loop. See Apr 25 01:35 SCAN_WINDOW_SLOW "
            "10.43s investigation.")

    def test_scan_dbwrite_slow_log_exists(self):
        with open(BOT_PY) as f:
            src = f.read()
        self.assertIn(
            "SCAN_DBWRITE_SLOW", src,
            "scan() must emit SCAN_DBWRITE_SLOW timer around "
            "insert_evaluated_opportunity calls (at least one site) "
            "to localize DB-write cost vs other in-iteration work.")

    def test_scan_sizing_slow_log_exists(self):
        with open(BOT_PY) as f:
            src = f.read()
        self.assertIn(
            "SCAN_SIZING_SLOW", src,
            "scan() must emit SCAN_SIZING_SLOW timer around "
            "sizer.compute call to localize sizing-math cost.")


if __name__ == "__main__":
    unittest.main()
