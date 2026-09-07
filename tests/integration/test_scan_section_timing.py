"""Diagnostic instrumentation — Apr 25 01:09 UTC SCAN_BODY_SLOW 5.64s.

After threading all 4 periodic-task blockers (drift probe 7dac681,
tracker.tick 8114ddc, market refresh f216a8d, EGARCH refit
84dc223), SCAN_BODY_SLOW continues to fire occasionally inside
scan() body itself (5.64s, ~once per 2-3 min). This is no longer
post-restart cold start (we're past warmup).

To pinpoint which section of scan() is slow, this commit adds two
per-section timers:
  - SCAN_LOOP_SLOW: the main `for window in eligible_windows:` loop
    (the bulk of scan body, ~4200 lines)
  - SCAN_POSTLOOP_SLOW: the post-loop processing — price_shadow,
    no_side, overnight, low_price processors + candidate selection

The next deploy's logs will tell us which section dominates.

Same TDD rhythm as eb5960e (per-task timing → identified
tracker_tick) and b554a9b (SCAN_BODY_SLOW → confirmed scan-body).
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "bot", "scanner", "__init__.py")


class TestScanSectionTiming(unittest.TestCase):

    def test_scan_loop_slow_log_exists(self):
        """scan() must emit `SCAN_LOOP_SLOW` when the per-window
        for-loop body exceeds 1.5s. Without per-section data we
        can't tell whether scan body slowness is in the loop or in
        post-loop processing."""
        src = ""
        if os.path.exists(BOT_PY):
            with open(BOT_PY) as f:
                src = f.read()
        self.assertIn(
            "SCAN_LOOP_SLOW", src,
            "scan() must emit SCAN_LOOP_SLOW warning when the "
            "main for-loop over eligible_windows exceeds 1.5s. "
            "See Apr 25 01:09 SCAN_BODY_SLOW 5.64s investigation.")

    def test_scan_postloop_slow_log_exists(self):
        """scan() must emit `SCAN_POSTLOOP_SLOW` when post-loop
        processing (shadow processors + candidate selection) exceeds
        1.5s. This catches the alternative blocker location."""
        src = ""
        if os.path.exists(BOT_PY):
            with open(BOT_PY) as f:
                src = f.read()
        self.assertIn(
            "SCAN_POSTLOOP_SLOW", src,
            "scan() must emit SCAN_POSTLOOP_SLOW warning when "
            "post-loop processing (price_shadow, no_side, overnight, "
            "low_price processors + candidate selection) exceeds "
            "1.5s. Without this we can't distinguish loop slowness "
            "from post-loop slowness.")

    def test_scan_preloop_slow_logs_section_breakdown(self):
        """Post-#178 leftover cost is scan() setup (2.1–6.6s live).
        SCAN_PRELOOP_SLOW must name the subsections or the next
        deploy cannot tell kill-switch SQL from KalshiFeed._lock.
        """
        src = ""
        if os.path.exists(BOT_PY):
            with open(BOT_PY) as f:
                src = f.read()
        self.assertIn("SCAN_PRELOOP_SLOW", src)
        for key in (
                "drift_probe=", "silence=", "productive=",
                "kill_sql=", "cooldown=", "cleanup=",
                "subscribe=", "filter=", "occupied="):
            self.assertIn(
                key, src,
                "SCAN_PRELOOP_SLOW must include section timing %s "
                "(2026-09-07 live: watchdogs=2.2–2.7s of 2.5s preloop)"
                % key)
        # Format-string keys stay green if _pre_mark is deleted and
        # .get(..., 0.0) logs zeros. Pin the mark calls in order.
        marks = [
            '_pre_mark("drift_probe")',
            '_pre_mark("silence")',
            '_pre_mark("productive")',
            '_pre_mark("kill_sql")',
            '_pre_mark("cooldown")',
            '_pre_mark("cleanup")',
            '_pre_mark("subscribe")',
            '_pre_mark("filter")',
            '_pre_mark("occupied")',
        ]
        last = -1
        for mark in marks:
            idx = src.find(mark)
            self.assertGreaterEqual(
                idx, 0, "scan() must call %s (not just log the key)" % mark)
            self.assertGreater(
                idx, last, "%s must run after the previous section mark" % mark)
            last = idx

    def test_scan_productive_skips_sql_when_heartbeat_recent(self):
        """VPS 2026-09-07: watchdogs=2.2–2.7s was COUNT(*) every 1 Hz.

        Skip SQL on the healthy heartbeat path. EXISTS only when the
        previous tick did not iterate a 15M window. Kill switches stay
        1 Hz (measured kill_sql=0.01s — do not delay a money kill).
        """
        src = ""
        if os.path.exists(BOT_PY):
            with open(BOT_PY) as f:
                src = f.read()
        start = src.find("def _check_scan_productive_15m")
        end = src.find("\n    def _drift_probe_tick")
        body = src[start:end]
        self.assertIn("if not heartbeat_recent:", body)
        idx_gate = body.find("if not heartbeat_recent:")
        idx_sql = body.find("evaluated_opportunities")
        self.assertGreaterEqual(idx_gate, 0)
        self.assertGreater(idx_sql, idx_gate)
        prod = src.find('_pre_mark("productive")')
        kill_mark = src.find('_pre_mark("kill_sql")')
        self.assertGreater(kill_mark, prod)
        kill_block = src[prod:kill_mark]
        self.assertIn("if bot.constants.WEATHER_NO_SIDE_LIVE:", kill_block)
        self.assertIn("if bot.constants.HOURLY_NO_SIDE_LIVE:", kill_block)
        self.assertIn("if bot.constants.BRACKET_NO_ENABLED:", kill_block)
        self.assertNotIn("slow_scan_due", kill_block)
        self.assertNotIn("_last_kill_sql_ts", kill_block)
        state_src = ""
        state_py = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), "bot", "state.py")
        if os.path.exists(state_py):
            with open(state_py) as f:
                state_src = f.read()
        self.assertIn("idx_eval_opp_pt_time", state_src)
        self.assertIn("idx_rejected_opp_pt_time", state_src)


if __name__ == "__main__":
    unittest.main()
