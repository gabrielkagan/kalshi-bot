"""Tests for WS drift auto-flagging (fix #1b from
kb/failures/ws-cache-drift-silent-scan-2026-04-24.md).

Fix #1a (d9858a6) added `flag_ticker_drifted(ticker)` and the
corresponding bypass in `_get_orderbook_cached`, but only wired it from
the `no_orderbook`/`no_best_ask` silent-bail paths — i.e. *empty* book
drift. The 2026-04-24 22:12 UTC incident had a *populated* book whose
WS qty was 5–10× the REST qty; `_best_yes_ask_cents()` returned a
valid-looking ask, so the silent-bail paths never fired, and fix #1a
alone wouldn't have caught it.

Fix #1b wires the pre-existing `_drift_probe_tick` (once-per-minute
WS-vs-REST diff, diagnostic-only in d9858a6) into `flag_ticker_drifted`
when the drift crosses a threshold calibrated against actual incident
values.

Threshold design: both conditions must hold to avoid false positives
during normal book churn —
- |ws_qty_total − rest_qty_total| ≥ 1000 (absolute miss must be meaningful)
- max(ws, rest) / min(ws, rest) ≥ 2.0 (ratio must be off by 2×+)
OR one side is zero and the other is ≥ 1000 (total disagreement).

Calibrated against incident values (all must trigger):
- XRP NO:  ws=10745 rest=1074   ratio=10.0×   abs=9671
- BTC NO:  ws=27137 rest=5403   ratio=5.0×    abs=21734
- BTC NO:  ws=28492 rest=19478  ratio=1.46×   abs=9014
- ETH NO:  ws=16533 rest=40366  ratio=2.44×   abs=23833

The BTC ws=28492/rest=19478 case has ratio only 1.46× — a pure ratio
filter at 2.0× misses it. So the gate is "abs diff AND (ratio OR one
side is zero)". Combined with the 1000-qty absolute-minimum floor,
normal churn (diff of hundreds) passes through cleanly.
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot
from bot import OpportunityScanner


BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot/_impl.py")


class TestIsSevereDrift(unittest.TestCase):
    """Pure threshold logic — no mocks, no fixtures."""

    def test_normal_book_not_severe(self):
        """Balanced books during normal trading must not flag."""
        self.assertFalse(OpportunityScanner._is_severe_drift(500, 500))
        self.assertFalse(OpportunityScanner._is_severe_drift(10000, 10000))
        self.assertFalse(OpportunityScanner._is_severe_drift(0, 0))

    def test_small_absolute_diff_never_severe(self):
        """10 vs 100 is a 10× ratio, but abs diff is 90 < 1000 — normal
        book churn, not drift."""
        self.assertFalse(OpportunityScanner._is_severe_drift(10, 100))
        self.assertFalse(OpportunityScanner._is_severe_drift(100, 500))

    def test_moderate_abs_diff_below_threshold_not_severe(self):
        """Diff of 500 at large magnitude is normal churn, not corruption."""
        self.assertFalse(OpportunityScanner._is_severe_drift(1500, 1000))
        self.assertFalse(OpportunityScanner._is_severe_drift(5500, 5000))

    def test_incident_xrp_10x_triggers(self):
        """Real 2026-04-24 incident: XRP NO side WS=10745 vs REST=1074."""
        self.assertTrue(OpportunityScanner._is_severe_drift(10745, 1074))

    def test_incident_btc_5x_triggers(self):
        """Real incident: BTC NO side WS=27137 vs REST=5403."""
        self.assertTrue(OpportunityScanner._is_severe_drift(27137, 5403))

    def test_incident_btc_early_1_46x_triggers(self):
        """Earlier BTC reading with ratio only 1.46× but abs diff 9014.
        Should still flag because WS is bloated relative to REST by a
        qty that dwarfs normal churn."""
        self.assertTrue(OpportunityScanner._is_severe_drift(28492, 19478))

    def test_incident_eth_inverse_2_4x_triggers(self):
        """Real incident: ETH NO side WS=16533 vs REST=40366. Here WS
        UNDER-represents the book by 23k qty — same corruption class,
        opposite direction. Must trigger."""
        self.assertTrue(OpportunityScanner._is_severe_drift(16533, 40366))

    def test_empty_rest_with_ws_phantom_triggers(self):
        """REST says 0 levels, WS claims 5000 qty → WS has phantoms."""
        self.assertTrue(OpportunityScanner._is_severe_drift(5000, 0))

    def test_empty_ws_with_rest_data_triggers(self):
        """WS empty, REST says 5000 → WS missed the book entirely."""
        self.assertTrue(OpportunityScanner._is_severe_drift(0, 5000))

    def test_empty_rest_small_ws_not_severe(self):
        """REST=0, WS=50 is not a meaningful phantom — just stale single
        level or tiny mismatch."""
        self.assertFalse(OpportunityScanner._is_severe_drift(50, 0))

    def test_symmetric_in_sides(self):
        """Function must treat (ws, rest) and (rest, ws) symmetrically —
        we care about the magnitude of the mismatch, not the direction."""
        self.assertEqual(
            OpportunityScanner._is_severe_drift(10745, 1074),
            OpportunityScanner._is_severe_drift(1074, 10745))


class TestDriftProbeWiring(unittest.TestCase):
    """AST regression: `_drift_probe_tick` must call
    `_is_severe_drift` and conditionally `flag_ticker_drifted`.
    Without this wiring, fix #1b is dead code."""

    @staticmethod
    def _find_method(cls_name: str, method_name: str) -> ast.FunctionDef:
        with open(BOT_PY) as f:
            tree = ast.parse(f.read())
        for cls in ast.walk(tree):
            if isinstance(cls, ast.ClassDef) and cls.name == cls_name:
                for node in cls.body:
                    if (isinstance(node, ast.FunctionDef)
                            and node.name == method_name):
                        return node
        raise AssertionError(
            f"{cls_name}.{method_name} not found in bot/_impl.py")

    def test_drift_probe_calls_is_severe_drift(self):
        probe = self._find_method("OpportunityScanner", "_drift_probe_tick")
        calls = [
            n for n in ast.walk(probe)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_is_severe_drift"
        ]
        self.assertGreaterEqual(
            len(calls), 1,
            "_drift_probe_tick must call _is_severe_drift to detect "
            "phantom-qty WS corruption. See "
            "kb/failures/ws-cache-drift-silent-scan-2026-04-24.md fix #1b.")

    def test_drift_probe_calls_flag_ticker_drifted(self):
        probe = self._find_method("OpportunityScanner", "_drift_probe_tick")
        calls = [
            n for n in ast.walk(probe)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "flag_ticker_drifted"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "self"
        ]
        self.assertGreaterEqual(
            len(calls), 1,
            "_drift_probe_tick must call self.flag_ticker_drifted when "
            "drift is severe, so the WS-bypass activates automatically.")


if __name__ == "__main__":
    unittest.main()
