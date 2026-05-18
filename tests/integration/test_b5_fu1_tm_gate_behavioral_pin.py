"""B5-fu1 behavioral pin (ticket 86b9zyh4v, 2026-05-18).

Sister of `test_tm_stack_decided_regression.py` (B5 structural guards).
B5-fu1 closes the BEHAVIORAL gap: the existing tests pin the gate
PREDICATE STRUCTURE (presence of `_dc_retry_queue` reference + side
filter + non-TM strategy filter), but do NOT pin the predicate's
boolean OUTPUT under specific synthetic state.

Without a behavioral pin, a future refactor could preserve the
structural tokens (`_dc_retry_queue`, `side`, `not...startswith(
"terminal_momentum")`) while inverting the boolean logic or breaking
the AND/OR composition. Result: gate looks correct to AST scanners
but stack-prevents the wrong scenarios.

This file:
1. AST-extracts the RHS of the `_tm_dc_retry_overlap` and
   `_tm_non_tm_position` assignments from `bot/scanner/__init__.py`.
2. Compiles each RHS as an expression.
3. `eval`s it against synthetic locals mimicking the production
   2026-05-18 incident state (HYPE KXHYPE15M-26MAY180530-30):
   - `ticker` = the production ticker
   - `self` = a stub exposing `_ml.executor._dc_retry_queue`
   - `_tm_open_positions` = synthetic open-position list
4. Asserts the boolean output matches the EXPECTED gate behavior.

Both predicates must return True under the production trace (gate
fires); each carve-out scenario verifies the predicate returns False
when it should NOT fire (e.g., NO-side position, different-strategy
queue entry).

The B5 gate predicate (`bot/scanner/__init__.py` ~line 3397-3442):

```python
_tm_dc_retry_overlap = any(
    (entry.get("candidate") or {}).get("ticker") == ticker
    and (entry.get("strategy") or "").startswith("decided_")
    for entry in self._ml.executor._dc_retry_queue
)
_tm_non_tm_position = any(
    p["ticker"] == ticker
    and (p.get("side") or "yes") == "yes"
    and not (p.get("strategy") or "").startswith("terminal_momentum")
    for p in _tm_open_positions
)
```

The behavioral pin extracts these two `any(...)` generator
expressions and exec-tests them against synthetic state.
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
    `OpportunityScanner.scan()`. The B5 gate predicates are inline
    in scan() (a 9400-line method).

    Some predicates have a False/None init followed by the meaningful
    `any(...)` assignment inside an `if` guard. Prefer the
    last-occurring `ast.Call` RHS (the meaningful predicate) over a
    bare `Constant` False (the init). If no Call RHS exists, fall
    back to the last assignment found.
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


class TestB5GatePredicatesExist(unittest.TestCase):
    """Structural anchor: assert both B5 gate predicate assignments
    exist in scanner with an `any(...)` RHS.

    RED against pre-B5 code (commits earlier than `76f1d54c`); GREEN
    post-B5. A missing predicate here means the behavioral classes
    below would SKIP via setUpClass — make it a HARD FAIL instead so
    maintainers see the regression immediately.
    """

    def test_tm_dc_retry_overlap_assignment_exists(self):
        rhs = _extract_assignment_rhs("_tm_dc_retry_overlap")
        self.assertTrue(
            rhs.startswith("any("),
            f"`_tm_dc_retry_overlap = any(...)` assignment missing from "
            f"`bot/scanner/__init__.py` — required by B5. Got RHS: {rhs!r}",
        )

    def test_tm_non_tm_position_assignment_exists(self):
        rhs = _extract_assignment_rhs("_tm_non_tm_position")
        self.assertTrue(
            rhs.startswith("any("),
            f"`_tm_non_tm_position = any(...)` assignment missing from "
            f"`bot/scanner/__init__.py` — required by B5. Got RHS: {rhs!r}",
        )


class TestTmDcRetryOverlapPredicate(unittest.TestCase):
    """Behavioral pin for `_tm_dc_retry_overlap` — must fire (True)
    when a decided_* candidate is queued for retry on the same ticker;
    must NOT fire (False) for non-decided strategies or different
    tickers.

    Production case: KXHYPE15M-26MAY180530-30 on 2026-05-18 09:25 UTC.
    decided_t1 was at attempt=2/11 in _dc_retry_queue when TM_98
    fired 22s later in a separate scan tick.
    """

    @classmethod
    def setUpClass(cls):
        cls.rhs = _extract_assignment_rhs("_tm_dc_retry_overlap")
        if not cls.rhs:
            raise unittest.SkipTest(
                "_tm_dc_retry_overlap assignment not found in scanner — "
                "refresh the regression if it was renamed or moved."
            )

    def _eval(self, *, ticker: str, queue: list) -> bool:
        """Evaluate the RHS expression against synthetic state.

        Python genexpr quirk: inner generator scope doesn't see names
        from the `locals` arg to `eval` — only `globals`. Pack the
        synthetic state into the globals dict.
        """
        self_ns = SimpleNamespace(
            _ml=SimpleNamespace(
                executor=SimpleNamespace(_dc_retry_queue=queue),
            ),
        )
        return eval(self.rhs, {"self": self_ns, "ticker": ticker, "any": any}, {})

    def test_fires_for_decided_t1_in_queue_same_ticker(self):
        """The 2026-05-18 production trace: decided_t1 in retry queue
        for the same ticker → gate must REFUSE TM (predicate True)."""
        ticker = "KXHYPE15M-26MAY180530-30"
        queue = [
            {
                "candidate": {"ticker": ticker},
                "strategy": "decided_t1",
                "attempt": 2,
            }
        ]
        self.assertTrue(
            self._eval(ticker=ticker, queue=queue),
            "_tm_dc_retry_overlap must return True for decided_t1 in "
            "_dc_retry_queue on the same ticker — the canonical B5 case.",
        )

    def test_does_not_fire_for_different_ticker(self):
        """A queued decided_* for a different ticker must NOT block TM."""
        queue = [
            {
                "candidate": {"ticker": "KXBTC15M-OTHER-50"},
                "strategy": "decided_t1",
            }
        ]
        self.assertFalse(
            self._eval(ticker="KXHYPE15M-26MAY180530-30", queue=queue),
            "_tm_dc_retry_overlap must return False when the queued "
            "decided_* is on a different ticker.",
        )

    def test_does_not_fire_for_non_decided_strategy(self):
        """A queued candidate for a non-decided_* strategy must NOT
        block TM (only decided_* belongs to the gate's scope)."""
        ticker = "KXHYPE15M-26MAY180530-30"
        queue = [
            {
                "candidate": {"ticker": ticker},
                "strategy": "weekend_discount",
            }
        ]
        self.assertFalse(
            self._eval(ticker=ticker, queue=queue),
            "_tm_dc_retry_overlap must return False for non-decided_* "
            "strategies — those have their own gate paths.",
        )

    def test_does_not_fire_for_empty_queue(self):
        """Empty queue → predicate False (no retry to overlap with)."""
        ticker = "KXHYPE15M-26MAY180530-30"
        self.assertFalse(
            self._eval(ticker=ticker, queue=[]),
            "_tm_dc_retry_overlap must return False when the retry "
            "queue is empty.",
        )

    def test_handles_missing_candidate_key_gracefully(self):
        """Defensive: `(entry.get("candidate") or {}).get("ticker")`
        must handle missing/None candidate dict without raising."""
        ticker = "KXHYPE15M-26MAY180530-30"
        queue = [
            {"candidate": None, "strategy": "decided_t1"},
            {"strategy": "decided_t1"},  # missing candidate key entirely
        ]
        # Neither entry has a matching ticker, so result is False —
        # but the eval must NOT raise AttributeError on the .get chain.
        result = self._eval(ticker=ticker, queue=queue)
        self.assertFalse(
            result,
            "_tm_dc_retry_overlap must defensively handle missing/None "
            "candidate keys without raising.",
        )


class TestTmNonTmPositionPredicate(unittest.TestCase):
    """Behavioral pin for `_tm_non_tm_position` — must fire (True)
    when a non-TM YES-side position is already open on the same
    ticker; must NOT fire (False) for NO-side positions (so
    bracket_no doesn't block YES-side TM) or for TM-on-TM stacking
    (different-price stacks remain allowed per the 40/40 ticker data).
    """

    @classmethod
    def setUpClass(cls):
        cls.rhs = _extract_assignment_rhs("_tm_non_tm_position")
        if not cls.rhs:
            raise unittest.SkipTest(
                "_tm_non_tm_position assignment not found in scanner — "
                "refresh the regression if it was renamed or moved."
            )

    def _eval(self, *, ticker: str, positions: list) -> bool:
        """See `TestTmDcRetryOverlapPredicate._eval` for the genexpr
        scope quirk — names go in globals, not locals."""
        return eval(
            self.rhs,
            {"ticker": ticker, "_tm_open_positions": positions, "any": any},
            {},
        )

    def test_fires_for_decided_t1_yes_position_same_ticker(self):
        """The 2026-05-18 production trace: decided_t1 YES position
        filled on the same ticker → gate must REFUSE TM."""
        ticker = "KXHYPE15M-26MAY180530-30"
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
            "_tm_non_tm_position must return True for decided_t1 YES "
            "position on the same ticker — the canonical B5 case.",
        )

    def test_does_not_fire_for_no_side_position(self):
        """L107 carve-out: NO-side positions must NOT block YES-side
        TM (e.g., bracket_no on the same ticker)."""
        ticker = "KXHYPE15M-26MAY180530-30"
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
            "_tm_non_tm_position must return False for NO-side "
            "positions — YES-side TM should not be blocked by them.",
        )

    def test_does_not_fire_for_terminal_momentum_position(self):
        """TM-on-TM stacking carve-out: different-price TM stacks
        remain allowed (per the 40/40 ticker data). The
        `_tm_has_position` check (different from this one) handles
        same-price TM blocking; this predicate must NOT block any TM."""
        ticker = "KXHYPE15M-26MAY180530-30"
        positions = [
            {
                "ticker": ticker,
                "side": "yes",
                "strategy": "terminal_momentum_97",
                "count": 50,
            }
        ]
        self.assertFalse(
            self._eval(ticker=ticker, positions=positions),
            "_tm_non_tm_position must return False for ANY "
            "terminal_momentum_* position — TM-on-TM same-side "
            "stacking is governed by _tm_has_position (different "
            "predicate), not this one.",
        )

    def test_handles_missing_side_as_yes(self):
        """Legacy schema (pre-NO-side) had no `side` column;
        `(p.get("side") or "yes") == "yes"` defaults missing to yes."""
        ticker = "KXHYPE15M-26MAY180530-30"
        positions = [
            # No `side` key — legacy row.
            {
                "ticker": ticker,
                "strategy": "decided_t1",
                "count": 62,
            }
        ]
        self.assertTrue(
            self._eval(ticker=ticker, positions=positions),
            "_tm_non_tm_position must default missing `side` to 'yes' "
            "so legacy pre-NO-side schema rows still block TM.",
        )

    def test_does_not_fire_for_different_ticker(self):
        """A non-TM YES position on a different ticker must NOT block."""
        positions = [
            {
                "ticker": "KXBTC15M-OTHER-50",
                "side": "yes",
                "strategy": "decided_t1",
                "count": 62,
            }
        ]
        self.assertFalse(
            self._eval(ticker="KXHYPE15M-26MAY180530-30", positions=positions),
            "_tm_non_tm_position must return False when the non-TM "
            "position is on a different ticker.",
        )

    def test_does_not_fire_for_empty_positions(self):
        """No open positions → predicate False."""
        self.assertFalse(
            self._eval(ticker="KXHYPE15M-26MAY180530-30", positions=[]),
            "_tm_non_tm_position must return False on empty positions.",
        )


if __name__ == "__main__":
    unittest.main()
