"""Regression guard for ws-cache-drift-silent-scan-2026-04-24.

Second 15M outage of Apr 24, 2026: scan() silently `continue`d on every
15M market because `_best_yes_ask_cents()` returned None under WS cache
drift. The `no_orderbook` and `no_best_ask` branches each logged to
`opportunity_journal.jsonl` but never called `insert_rejection()`, so
there was zero DB trace — the silence watchdog fired, but diagnosis
had no breadcrumbs.

This test asserts that both silent-bail paths in OpportunityScanner.scan
contain a `self._state.insert_rejection(...)` call with the reason
string matching the branch name. Next occurrence of this failure class
leaves a DB row at `rejection_reason='no_orderbook'` or `'no_best_ask'`,
making the failure immediately visible in state.db.

See `kb/failures/ws-cache-drift-silent-scan-2026-04-24.md`.
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot/scanner/__init__.py")


def _find_scan_method() -> ast.FunctionDef:
    """Return the AST node for OpportunityScanner.scan."""
    if os.path.exists(BOT_PY):
        with open(BOT_PY) as f:
            tree = ast.parse(f.read())
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef) and cls.name == "OpportunityScanner":
            for node in cls.body:
                if isinstance(node, ast.FunctionDef) and node.name == "scan":
                    return node
    raise AssertionError("OpportunityScanner.scan not found in bot/_impl.py")


def _insert_rejection_calls_in(node: ast.AST) -> list:
    """All `self._state.insert_rejection(...)` call nodes under `node`."""
    calls = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        f = sub.func
        if (isinstance(f, ast.Attribute)
                and f.attr == "insert_rejection"
                and isinstance(f.value, ast.Attribute)
                and f.value.attr == "_state"
                and isinstance(f.value.value, ast.Name)
                and f.value.value.id == "self"):
            calls.append(sub)
    return calls


def _call_has_first_reason_arg(call: ast.Call, reason: str) -> bool:
    """True if the 4th positional arg (rejection_reason) is the exact string."""
    # Signature: insert_rejection(ticker, event_ticker, asset, rejection_reason, ...)
    # So args[3] is the reason string.
    if len(call.args) < 4:
        return False
    a = call.args[3]
    return isinstance(a, ast.Constant) and a.value == reason


class TestScanSilentBailLeavesDbTrace(unittest.TestCase):
    """bot/_impl.py scan() must insert_rejection before `continue` on both
    silent-bail paths (no_orderbook, no_best_ask). Otherwise the next
    WS-cache-drift outage produces zero DB evidence — same failure
    shape as 2026-04-24 22:12 UTC."""

    def test_scan_contains_insert_rejection_no_orderbook(self):
        scan = _find_scan_method()
        calls = _insert_rejection_calls_in(scan)
        matches = [c for c in calls
                   if _call_has_first_reason_arg(c, "no_orderbook")]
        self.assertGreaterEqual(
            len(matches), 1,
            "scan() is missing `self._state.insert_rejection(... 'no_orderbook' ...)` "
            "— silent-bail path would produce zero DB trace under WS drift. "
            "See kb/failures/ws-cache-drift-silent-scan-2026-04-24.md.")

    def test_scan_contains_insert_rejection_no_best_ask(self):
        scan = _find_scan_method()
        calls = _insert_rejection_calls_in(scan)
        matches = [c for c in calls
                   if _call_has_first_reason_arg(c, "no_best_ask")]
        self.assertGreaterEqual(
            len(matches), 1,
            "scan() is missing `self._state.insert_rejection(... 'no_best_ask' ...)` "
            "— silent-bail path would produce zero DB trace when NBBO is None. "
            "See kb/failures/ws-cache-drift-silent-scan-2026-04-24.md.")

    def test_both_insert_rejection_calls_pass_product_type(self):
        """Both new calls must thread product_type so 15m/hourly/weather
        rejections are queryable per-product in state.db."""
        scan = _find_scan_method()
        calls = _insert_rejection_calls_in(scan)
        targets = [c for c in calls
                   if _call_has_first_reason_arg(c, "no_orderbook")
                   or _call_has_first_reason_arg(c, "no_best_ask")]
        for call in targets:
            keywords = {k.arg: k.value for k in call.keywords}
            self.assertIn(
                "product_type", keywords,
                "insert_rejection call on a silent-bail path must pass "
                "product_type= so downstream queries can filter by product.")


if __name__ == "__main__":
    unittest.main()
