"""Regression — Apr 25 00:43 UTC silent_vol_none investigation.

The trace shipped in commit a34f8c7 surfaced 6 vol_est=None events
all within 18 seconds of bot restart — they are post-restart vol
warmup, not a steady-state bug. Vol engines (RK + EGARCH) need a
few seconds of WS price feeds before they can compute returns.

But silent_vol_none should NOT keep firing 60s+ after restart in
steady state. If it does, that's a real vol engine stall — same
class of bug as the original ws-cache-drift-silent-scan-2026-04-24
incident, just at a different code path.

Fix: when scan() takes the silent_vol_none branch AND uptime is
past warmup (60s), emit a `VOL_NONE_POST_WARMUP` warning at WARNING
level. During warmup the trace row remains the only signal (benign).
After warmup the warning escalates so the next outage class
self-announces.

This test asserts:
  1. The vol_none branch contains a `VOL_NONE_POST_WARMUP` log message.
  2. The log is gated by an uptime check (compares to a process-start
     attribute, not unconditional — so warmup ticks don't spam).
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.py")


def _find_scan_method() -> ast.FunctionDef:
    with open(BOT_PY) as f:
        tree = ast.parse(f.read())
    for cls in ast.walk(tree):
        if (isinstance(cls, ast.ClassDef)
                and cls.name == "OpportunityScanner"):
            for node in cls.body:
                if (isinstance(node, ast.FunctionDef)
                        and node.name == "scan"):
                    return node
    raise AssertionError("OpportunityScanner.scan not found")


class TestVolNonePostWarmupEscalation(unittest.TestCase):

    def test_scan_contains_vol_none_post_warmup_log(self):
        """`scan()` must emit a `VOL_NONE_POST_WARMUP` log message
        somewhere in its body, so steady-state vol failures escalate
        beyond the silent trace row."""
        with open(BOT_PY) as f:
            src = f.read()
        self.assertIn(
            "VOL_NONE_POST_WARMUP", src,
            "scan() must emit a VOL_NONE_POST_WARMUP warning when "
            "vol_est=None occurs after the warmup window. See Apr 25 "
            "00:43 silent_vol_none investigation — first 18s post-"
            "restart is benign warmup, but later occurrences are real "
            "vol engine stalls.")

    def test_post_warmup_log_is_uptime_gated(self):
        """The warning must be gated by an uptime check — otherwise
        warmup ticks would spam the warning. The gate compares against
        `_scan_15m_process_start_ts` (already used by the watchdog
        grace) for consistency."""
        with open(BOT_PY) as f:
            src = f.read()
        # Look for the gating pattern: the WARNING string and an
        # uptime comparison must appear within ~10 lines of each other.
        # Cheap structural check: both substrings must exist together
        # in the file, and the warning string follows the gating attr.
        self.assertIn("_scan_15m_process_start_ts", src)
        idx_attr = src.find("VOL_NONE_POST_WARMUP")
        # Look up to 600 chars before for the uptime gate keyword.
        window = src[max(0, idx_attr - 600):idx_attr]
        self.assertIn(
            "_scan_15m_process_start_ts", window,
            "VOL_NONE_POST_WARMUP must be gated by a check against "
            "`self._scan_15m_process_start_ts` so warmup ticks do "
            "not spam the warning.")


if __name__ == "__main__":
    unittest.main()
