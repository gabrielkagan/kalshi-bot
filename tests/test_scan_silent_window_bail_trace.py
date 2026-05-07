"""Regression guard — ws-cache-drift-silent-scan-2026-04-24 PM Prevention #3.

After Fix 2 (commit 9dd396b) covered the no_orderbook / no_best_ask
silent-bail paths, three OTHER silent-continue paths in scan() remained
unprotected (window-level bails before per-market evaluation):

  - line ~8147: LOSS_COOLDOWN — `continue` when asset is in cooldown
  - line ~8166: spot None — `continue` when CoinbaseFeed returns None
  - line ~8175: vol None — `continue` when vol_est is None / blended_rv <= 0

Each kills ALL markets in the window with zero DB trace. Apr 24 second
incident confirmed this surface remained: post-restart we saw the
scan-productive watchdog firing 30+ consecutive ticks with 0 rows in
both `evaluated_opportunities` and `rejected_opportunities` for
`product_type='15m'`.

This test asserts each silent path now writes a trace row via
`self._state.insert_evaluated_opportunity(...)` with a distinguishing
filter_stage string before `continue`. Next occurrence leaves a DB row
within ~1 tick instead of producing a 30-min black hole.

See `kb/failures/ws-cache-drift-silent-scan-2026-04-24.md`.
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot/_impl.py")


def _find_scan_method() -> ast.FunctionDef:
    with open(BOT_PY) as f:
        tree = ast.parse(f.read())
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef) and cls.name == "OpportunityScanner":
            for node in cls.body:
                if isinstance(node, ast.FunctionDef) and node.name == "scan":
                    return node
    raise AssertionError("OpportunityScanner.scan not found in bot/_impl.py")


def _insert_evaluated_calls_in(node: ast.AST) -> list:
    """All `self._state.insert_evaluated_opportunity(...)` call nodes."""
    calls = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        f = sub.func
        if (isinstance(f, ast.Attribute)
                and f.attr == "insert_evaluated_opportunity"
                and isinstance(f.value, ast.Attribute)
                and f.value.attr == "_state"
                and isinstance(f.value.value, ast.Name)
                and f.value.value.id == "self"):
            calls.append(sub)
    return calls


def _call_has_filter_stage(call: ast.Call, stage: str) -> bool:
    """True if the call has filter_stage=<stage> as a keyword arg."""
    for kw in call.keywords:
        if (kw.arg == "filter_stage"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value == stage):
            return True
    return False


class TestScanWindowLevelSilentBailLeavesTrace(unittest.TestCase):
    """scan() must insert_evaluated_opportunity before each window-level
    `continue` on cooldown / spot-None / vol-None paths. Otherwise the
    same failure class as Apr 24 22:12 UTC reproduces with zero DB
    evidence."""

    def test_silent_loss_cooldown_writes_trace(self):
        scan = _find_scan_method()
        calls = _insert_evaluated_calls_in(scan)
        matches = [c for c in calls
                   if _call_has_filter_stage(c, "silent_loss_cooldown")]
        self.assertGreaterEqual(
            len(matches), 1,
            "scan() is missing insert_evaluated_opportunity("
            "filter_stage='silent_loss_cooldown') before the LOSS_COOLDOWN "
            "continue. The bail produces zero DB trace — diagnostic black "
            "hole. See ws-cache-drift-silent-scan-2026-04-24 PM Prevention #3.")

    def test_silent_spot_none_writes_trace(self):
        scan = _find_scan_method()
        calls = _insert_evaluated_calls_in(scan)
        matches = [c for c in calls
                   if _call_has_filter_stage(c, "silent_spot_none")]
        self.assertGreaterEqual(
            len(matches), 1,
            "scan() is missing insert_evaluated_opportunity("
            "filter_stage='silent_spot_none') before the CoinbaseFeed-None "
            "continue. The bail produces zero DB trace — diagnostic black "
            "hole. See ws-cache-drift-silent-scan-2026-04-24 PM Prevention #3.")

    def test_silent_vol_none_writes_trace(self):
        scan = _find_scan_method()
        calls = _insert_evaluated_calls_in(scan)
        matches = [c for c in calls
                   if _call_has_filter_stage(c, "silent_vol_none")]
        self.assertGreaterEqual(
            len(matches), 1,
            "scan() is missing insert_evaluated_opportunity("
            "filter_stage='silent_vol_none') before the vol_est-None "
            "continue. The bail produces zero DB trace — diagnostic black "
            "hole. See ws-cache-drift-silent-scan-2026-04-24 PM Prevention #3.")

    def test_all_three_filter_stages_distinct(self):
        """Sanity — the three stages should be three different strings,
        not a copy-paste oversight that points all three at the same one."""
        scan = _find_scan_method()
        calls = _insert_evaluated_calls_in(scan)
        stages = set()
        for stage in ("silent_loss_cooldown", "silent_spot_none",
                      "silent_vol_none"):
            for c in calls:
                if _call_has_filter_stage(c, stage):
                    stages.add(stage)
        self.assertEqual(stages,
                         {"silent_loss_cooldown",
                          "silent_spot_none",
                          "silent_vol_none"},
                         "Expected 3 distinct silent-bail filter_stages.")


if __name__ == "__main__":
    unittest.main()
