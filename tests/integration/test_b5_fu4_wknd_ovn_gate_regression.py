"""B5-fu4 regression + behavioral pin (ticket 86ba05k5q, 2026-05-18).

L107 sister of B5 (terminal_momentum intercept). B5 closed the
`terminal_momentum_98 stacks on same-window decided_t1` class by adding
two gates to the TM intercept block:

- `_tm_dc_retry_overlap`: scans `self._ml.executor._dc_retry_queue` for
  any entry whose `candidate["ticker"]` matches AND whose `strategy`
  starts with `"decided_"`.
- `_tm_non_tm_position`: scans `self._state.get_open_positions()` for
  any row whose `ticker` matches AND whose `side == "yes"` AND whose
  `strategy` does NOT start with the protected prefix.

`weekend_discount` and `overnight_discount` (both LIVE strategies) have
the SAME unguarded surface. Their existing `_*_dc_overlap` checks only
catch same-tick decided_* candidates via the z-score predicate — they
miss (a) the cross-tick `_dc_retry_queue` overlap and (b) the
non-WKND/non-OVN open-position stack on the same (ticker, side).

This file pins the fix:

1. Structural: AST-extract assignments to four new predicates
   - `_wknd_dc_retry_overlap`
   - `_wknd_non_wknd_position`
   - `_ovn_dc_retry_overlap`
   - `_ovn_non_ovn_position`
   Assert each exists with an `any(...)` RHS.
2. Behavioral: AST-extract each RHS and eval it against synthetic
   state mimicking the L107 incident class — gate must fire (True)
   for the production case and NOT fire (False) for carve-outs.

TDD-RED against pre-fix code: the four predicate assignments do not
exist in `bot/scanner/__init__.py` until B5-fu4 lands.
"""
from __future__ import annotations

import ast
import os
import sys
import unittest
from types import SimpleNamespace

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, REPO_ROOT)

SCANNER_PATH = os.path.join(REPO_ROOT, "bot", "scanner", "__init__.py")


def _scanner_source() -> str:
    with open(SCANNER_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _extract_assignment_rhs(target_name: str) -> str:
    """Return the source text of the RHS of `target_name = <rhs>` in
    `OpportunityScanner.scan()`. Mirrors the B5-fu1 helper — prefers
    the last-occurring `ast.Call` (meaningful predicate) over a bare
    `Constant` False (the init line that precedes the `if` guard).
    """
    src = _scanner_source()
    tree = ast.parse(src, filename=SCANNER_PATH)
    last_any_call: str = ""
    last_other: str = ""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id == target_name):
            continue
        if isinstance(node.value, ast.Call):
            last_any_call = ast.unparse(node.value)
        else:
            last_other = ast.unparse(node.value)
    return last_any_call or last_other


# ─────────────────────────────────────────────────────────────────────────────
# Structural anchors
# ─────────────────────────────────────────────────────────────────────────────


class TestB5Fu4PredicateAssignmentsExist(unittest.TestCase):
    """Each of the four new predicates must be assigned an `any(...)`
    expression in `bot/scanner/__init__.py`. RED before B5-fu4 lands;
    GREEN after the fix wires the gates into the WKND + OVN paths.
    """

    def test_wknd_dc_retry_overlap_assignment_exists(self):
        rhs = _extract_assignment_rhs("_wknd_dc_retry_overlap")
        self.assertTrue(
            rhs.startswith("any("),
            f"`_wknd_dc_retry_overlap = any(...)` assignment missing "
            f"from `bot/scanner/__init__.py` — required by B5-fu4. "
            f"Got RHS: {rhs!r}",
        )

    def test_wknd_non_wknd_position_assignment_exists(self):
        rhs = _extract_assignment_rhs("_wknd_non_wknd_position")
        self.assertTrue(
            rhs.startswith("any("),
            f"`_wknd_non_wknd_position = any(...)` assignment missing "
            f"from `bot/scanner/__init__.py` — required by B5-fu4. "
            f"Got RHS: {rhs!r}",
        )

    def test_ovn_dc_retry_overlap_assignment_exists(self):
        rhs = _extract_assignment_rhs("_ovn_dc_retry_overlap")
        self.assertTrue(
            rhs.startswith("any("),
            f"`_ovn_dc_retry_overlap = any(...)` assignment missing "
            f"from `bot/scanner/__init__.py` — required by B5-fu4. "
            f"Got RHS: {rhs!r}",
        )

    def test_ovn_non_ovn_position_assignment_exists(self):
        rhs = _extract_assignment_rhs("_ovn_non_ovn_position")
        self.assertTrue(
            rhs.startswith("any("),
            f"`_ovn_non_ovn_position = any(...)` assignment missing "
            f"from `bot/scanner/__init__.py` — required by B5-fu4. "
            f"Got RHS: {rhs!r}",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Behavioral pins — weekend_discount
# ─────────────────────────────────────────────────────────────────────────────


class TestWkndDcRetryOverlapPredicate(unittest.TestCase):
    """`_wknd_dc_retry_overlap` must fire (True) when a decided_*
    candidate is queued for retry on the same ticker; must NOT fire
    (False) for non-decided strategies or different tickers.
    """

    @classmethod
    def setUpClass(cls):
        cls.rhs = _extract_assignment_rhs("_wknd_dc_retry_overlap")
        if not cls.rhs:
            raise unittest.SkipTest(
                "_wknd_dc_retry_overlap assignment not found in scanner — "
                "refresh the regression if it was renamed or moved."
            )

    def _eval(self, *, ticker: str, queue: list) -> bool:
        """genexpr scope quirk: inner generator doesn't see locals
        passed to eval — pack synthetic state into globals."""
        self_ns = SimpleNamespace(
            _ml=SimpleNamespace(
                executor=SimpleNamespace(_dc_retry_queue=queue),
            ),
        )
        return eval(self.rhs, {"self": self_ns, "ticker": ticker, "any": any}, {})

    def test_fires_for_decided_t1_in_queue_same_ticker(self):
        ticker = "KXBTC15M-26MAY171800-99"
        queue = [
            {
                "candidate": {"ticker": ticker},
                "strategy": "decided_t1",
                "attempt": 2,
            }
        ]
        self.assertTrue(
            self._eval(ticker=ticker, queue=queue),
            "_wknd_dc_retry_overlap must return True for decided_t1 in "
            "_dc_retry_queue on the same ticker — the L107 case.",
        )

    def test_does_not_fire_for_different_ticker(self):
        queue = [
            {
                "candidate": {"ticker": "KXBTC15M-OTHER-50"},
                "strategy": "decided_t1",
            }
        ]
        self.assertFalse(
            self._eval(ticker="KXBTC15M-26MAY171800-99", queue=queue),
            "_wknd_dc_retry_overlap must return False when the queued "
            "decided_* is on a different ticker.",
        )

    def test_does_not_fire_for_non_decided_strategy(self):
        ticker = "KXBTC15M-26MAY171800-99"
        queue = [
            {
                "candidate": {"ticker": ticker},
                "strategy": "terminal_momentum_97",
            }
        ]
        self.assertFalse(
            self._eval(ticker=ticker, queue=queue),
            "_wknd_dc_retry_overlap must return False for non-decided_* "
            "strategies — those have their own gate paths.",
        )

    def test_does_not_fire_for_empty_queue(self):
        self.assertFalse(
            self._eval(ticker="KXBTC15M-26MAY171800-99", queue=[]),
            "_wknd_dc_retry_overlap must return False when the retry "
            "queue is empty.",
        )

    def test_handles_missing_candidate_key_gracefully(self):
        ticker = "KXBTC15M-26MAY171800-99"
        queue = [
            {"candidate": None, "strategy": "decided_t1"},
            {"strategy": "decided_t1"},
        ]
        result = self._eval(ticker=ticker, queue=queue)
        self.assertFalse(
            result,
            "_wknd_dc_retry_overlap must defensively handle missing/None "
            "candidate keys without raising.",
        )


class TestWkndNonWkndPositionPredicate(unittest.TestCase):
    """`_wknd_non_wknd_position` must fire (True) when a non-WKND
    YES-side position is already open on the same ticker; must NOT
    fire for NO-side positions or for weekend_discount-on-weekend_discount
    stacking (though same-strategy stacking is gated by other checks).
    """

    @classmethod
    def setUpClass(cls):
        cls.rhs = _extract_assignment_rhs("_wknd_non_wknd_position")
        if not cls.rhs:
            raise unittest.SkipTest(
                "_wknd_non_wknd_position assignment not found in scanner — "
                "refresh the regression if it was renamed or moved."
            )

    def _eval(self, *, ticker: str, positions: list) -> bool:
        return eval(
            self.rhs,
            {"ticker": ticker, "_wknd_open_positions": positions, "any": any},
            {},
        )

    def test_fires_for_decided_t1_yes_position_same_ticker(self):
        ticker = "KXBTC15M-26MAY171800-99"
        positions = [
            {
                "ticker": ticker,
                "side": "yes",
                "strategy": "decided_t1",
                "count": 62,
            }
        ]
        self.assertTrue(
            self._eval(ticker=ticker, positions=positions),
            "_wknd_non_wknd_position must return True for decided_t1 YES "
            "position on the same ticker — the L107 case.",
        )

    def test_fires_for_terminal_momentum_yes_position_same_ticker(self):
        """TM-on-WKND stacking class: a same-side TM fill must also
        block WKND from re-entering on top of it."""
        ticker = "KXBTC15M-26MAY171800-99"
        positions = [
            {
                "ticker": ticker,
                "side": "yes",
                "strategy": "terminal_momentum_98",
                "count": 30,
            }
        ]
        self.assertTrue(
            self._eval(ticker=ticker, positions=positions),
            "_wknd_non_wknd_position must return True for "
            "terminal_momentum_* YES position — WKND should not "
            "stack on top of a same-side TM fill.",
        )

    def test_does_not_fire_for_no_side_position(self):
        """L107 carve-out: NO-side positions must NOT block YES-side
        WKND (e.g., bracket_no on the same ticker)."""
        ticker = "KXBTC15M-26MAY171800-99"
        positions = [
            {
                "ticker": ticker,
                "side": "no",
                "strategy": "bracket_no",
                "count": 50,
            }
        ]
        self.assertFalse(
            self._eval(ticker=ticker, positions=positions),
            "_wknd_non_wknd_position must return False for NO-side "
            "positions — YES-side WKND should not be blocked by them.",
        )

    def test_does_not_fire_for_weekend_discount_position(self):
        """WKND-on-WKND stacking is governed by other checks (or
        deliberately disallowed); this predicate must NOT fire for
        same-strategy positions so its semantics stay scoped to the
        cross-strategy case."""
        ticker = "KXBTC15M-26MAY171800-99"
        positions = [
            {
                "ticker": ticker,
                "side": "yes",
                "strategy": "weekend_discount",
                "count": 40,
            }
        ]
        self.assertFalse(
            self._eval(ticker=ticker, positions=positions),
            "_wknd_non_wknd_position must return False for "
            "weekend_discount positions — same-strategy stacking is "
            "outside this predicate's scope.",
        )

    def test_handles_missing_side_as_yes(self):
        ticker = "KXBTC15M-26MAY171800-99"
        positions = [
            {
                "ticker": ticker,
                "strategy": "decided_t1",
                "count": 62,
            }
        ]
        self.assertTrue(
            self._eval(ticker=ticker, positions=positions),
            "_wknd_non_wknd_position must default missing `side` to "
            "'yes' so legacy pre-NO-side schema rows still block WKND.",
        )

    def test_does_not_fire_for_different_ticker(self):
        positions = [
            {
                "ticker": "KXBTC15M-OTHER-50",
                "side": "yes",
                "strategy": "decided_t1",
                "count": 62,
            }
        ]
        self.assertFalse(
            self._eval(ticker="KXBTC15M-26MAY171800-99", positions=positions),
            "_wknd_non_wknd_position must return False when the "
            "non-WKND position is on a different ticker.",
        )

    def test_does_not_fire_for_empty_positions(self):
        self.assertFalse(
            self._eval(ticker="KXBTC15M-26MAY171800-99", positions=[]),
            "_wknd_non_wknd_position must return False on empty positions.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Behavioral pins — overnight_discount
# ─────────────────────────────────────────────────────────────────────────────


class TestOvnDcRetryOverlapPredicate(unittest.TestCase):
    """Parallel of `TestWkndDcRetryOverlapPredicate` for the overnight
    discount path. Same predicate shape, different name."""

    @classmethod
    def setUpClass(cls):
        cls.rhs = _extract_assignment_rhs("_ovn_dc_retry_overlap")
        if not cls.rhs:
            raise unittest.SkipTest(
                "_ovn_dc_retry_overlap assignment not found in scanner — "
                "refresh the regression if it was renamed or moved."
            )

    def _eval(self, *, ticker: str, queue: list) -> bool:
        self_ns = SimpleNamespace(
            _ml=SimpleNamespace(
                executor=SimpleNamespace(_dc_retry_queue=queue),
            ),
        )
        return eval(self.rhs, {"self": self_ns, "ticker": ticker, "any": any}, {})

    def test_fires_for_decided_t1_in_queue_same_ticker(self):
        ticker = "KXSOL15M-26MAY180445-45"
        queue = [
            {
                "candidate": {"ticker": ticker},
                "strategy": "decided_t1",
                "attempt": 2,
            }
        ]
        self.assertTrue(
            self._eval(ticker=ticker, queue=queue),
            "_ovn_dc_retry_overlap must return True for decided_t1 in "
            "_dc_retry_queue on the same ticker.",
        )

    def test_does_not_fire_for_different_ticker(self):
        queue = [
            {
                "candidate": {"ticker": "KXSOL15M-OTHER-50"},
                "strategy": "decided_t1",
            }
        ]
        self.assertFalse(
            self._eval(ticker="KXSOL15M-26MAY180445-45", queue=queue),
            "_ovn_dc_retry_overlap must return False on different ticker.",
        )

    def test_does_not_fire_for_non_decided_strategy(self):
        ticker = "KXSOL15M-26MAY180445-45"
        queue = [
            {
                "candidate": {"ticker": ticker},
                "strategy": "weekend_discount",
            }
        ]
        self.assertFalse(
            self._eval(ticker=ticker, queue=queue),
            "_ovn_dc_retry_overlap must return False for non-decided_*.",
        )

    def test_does_not_fire_for_empty_queue(self):
        self.assertFalse(
            self._eval(ticker="KXSOL15M-26MAY180445-45", queue=[]),
            "_ovn_dc_retry_overlap must return False on empty queue.",
        )

    def test_handles_missing_candidate_key_gracefully(self):
        ticker = "KXSOL15M-26MAY180445-45"
        queue = [
            {"candidate": None, "strategy": "decided_t1"},
            {"strategy": "decided_t1"},
        ]
        result = self._eval(ticker=ticker, queue=queue)
        self.assertFalse(
            result,
            "_ovn_dc_retry_overlap must defensively handle missing/None "
            "candidate keys without raising.",
        )


class TestOvnNonOvnPositionPredicate(unittest.TestCase):
    """Parallel of `TestWkndNonWkndPositionPredicate` for the overnight
    discount path."""

    @classmethod
    def setUpClass(cls):
        cls.rhs = _extract_assignment_rhs("_ovn_non_ovn_position")
        if not cls.rhs:
            raise unittest.SkipTest(
                "_ovn_non_ovn_position assignment not found in scanner — "
                "refresh the regression if it was renamed or moved."
            )

    def _eval(self, *, ticker: str, positions: list) -> bool:
        return eval(
            self.rhs,
            {"ticker": ticker, "_ovn_open_positions": positions, "any": any},
            {},
        )

    def test_fires_for_decided_t1_yes_position_same_ticker(self):
        ticker = "KXSOL15M-26MAY180445-45"
        positions = [
            {
                "ticker": ticker,
                "side": "yes",
                "strategy": "decided_t1",
                "count": 62,
            }
        ]
        self.assertTrue(
            self._eval(ticker=ticker, positions=positions),
            "_ovn_non_ovn_position must return True for decided_t1 YES "
            "position on the same ticker.",
        )

    def test_fires_for_terminal_momentum_yes_position_same_ticker(self):
        ticker = "KXSOL15M-26MAY180445-45"
        positions = [
            {
                "ticker": ticker,
                "side": "yes",
                "strategy": "terminal_momentum_98",
                "count": 30,
            }
        ]
        self.assertTrue(
            self._eval(ticker=ticker, positions=positions),
            "_ovn_non_ovn_position must return True for "
            "terminal_momentum_* YES position on the same ticker.",
        )

    def test_does_not_fire_for_no_side_position(self):
        ticker = "KXSOL15M-26MAY180445-45"
        positions = [
            {
                "ticker": ticker,
                "side": "no",
                "strategy": "bracket_no",
                "count": 50,
            }
        ]
        self.assertFalse(
            self._eval(ticker=ticker, positions=positions),
            "_ovn_non_ovn_position must return False for NO-side positions.",
        )

    def test_does_not_fire_for_overnight_discount_position(self):
        ticker = "KXSOL15M-26MAY180445-45"
        positions = [
            {
                "ticker": ticker,
                "side": "yes",
                "strategy": "overnight_discount",
                "count": 40,
            }
        ]
        self.assertFalse(
            self._eval(ticker=ticker, positions=positions),
            "_ovn_non_ovn_position must return False for "
            "overnight_discount positions — same-strategy stacking "
            "is outside this predicate's scope.",
        )

    def test_handles_missing_side_as_yes(self):
        ticker = "KXSOL15M-26MAY180445-45"
        positions = [
            {
                "ticker": ticker,
                "strategy": "decided_t1",
                "count": 62,
            }
        ]
        self.assertTrue(
            self._eval(ticker=ticker, positions=positions),
            "_ovn_non_ovn_position must default missing `side` to 'yes'.",
        )

    def test_does_not_fire_for_different_ticker(self):
        positions = [
            {
                "ticker": "KXSOL15M-OTHER-50",
                "side": "yes",
                "strategy": "decided_t1",
                "count": 62,
            }
        ]
        self.assertFalse(
            self._eval(ticker="KXSOL15M-26MAY180445-45", positions=positions),
            "_ovn_non_ovn_position must return False on different ticker.",
        )

    def test_does_not_fire_for_empty_positions(self):
        self.assertFalse(
            self._eval(ticker="KXSOL15M-26MAY180445-45", positions=[]),
            "_ovn_non_ovn_position must return False on empty positions.",
        )


if __name__ == "__main__":
    unittest.main()
