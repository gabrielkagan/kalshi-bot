"""Regression for B5: terminal_momentum stacks on same-window decided_t1.

2026-05-18 incident (HYPE KXHYPE15M-26MAY180530-30, ticket 86b9zudg2):
the `terminal_momentum_98` intercept in `OpportunityScanner.scan`
(`bot/scanner/__init__.py`) fired 22s after a `decided_contract_t1`
Kelly-sized entry on the same (ticker, side="yes"). Both entries
cleared independently and landed live; the TM stack added ~58
contracts of NEGATIVE-edge exposure on top of decided_t1's 62-contract
Kelly-sized entry, doubling the real settlement loss.

The existing TM gates leak this stack:

1. `_tm_dc_overlap` — `any(c["ticker"] == ticker and
   c.get("strategy", "").startswith("decided_") for c in candidates)`
   only sees decided_* candidates emitted in the SAME scan tick. The
   production decided_t1 candidate fired 22 seconds earlier in a prior
   tick — `candidates` is per-tick and was empty for it.
2. `_tm_has_position` — only blocks when the open position's strategy
   group matches `f"terminal_momentum_{best_ask}"`. A `decided_t1`
   open position (or one in `_dc_retry_queue` not yet filled) does
   NOT match.

Fix shape: extend the TM intercept gate with two new checks before
the existing `_tm_has_position` branch:

- `_tm_dc_retry_overlap`: scan `self._ml.executor._dc_retry_queue`
  for any entry whose `candidate["ticker"]` matches AND whose
  `strategy` starts with `"decided_"` — catches the cross-tick
  in-flight decided_* IOC retry that the candidate-list overlap
  check misses.
- `_tm_non_tm_position`: scan `self._state.get_open_positions()`
  for any row whose `ticker` matches AND whose `side == "yes"` AND
  whose `strategy` does NOT start with `"terminal_momentum"` —
  catches an already-filled Kelly-sized entry on the same
  (ticker, side) regardless of strategy. Side-filtering is required
  so that NO-side strategies (e.g., `bracket_no`) on the same
  ticker do NOT block the YES-side TM intercept.

This is TDD-RED against unfixed code: the AST guard scans the TM
intercept block source for the two new tokens. The existing block
references neither.
"""
import ast
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, REPO_ROOT)

SCANNER_PATH = os.path.join(REPO_ROOT, "bot", "scanner", "__init__.py")


def _scanner_source():
    with open(SCANNER_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _tm_intercept_slice(src: str) -> tuple[int, int, str]:
    """Locate the Terminal Momentum intercept block.

    Boundaries:
    - start: the `# ── Terminal Momentum intercept` section header
    - end: the `if _tm_intercepted:` line whose body is `continue`
      (the block-exit point that skips insufficient_edge rejection)
    """
    start_marker = "# ── Terminal Momentum intercept"
    end_marker = (
        "continue  # Skip insufficient_edge rejection — this is now a TM candidate"
    )
    start = src.find(start_marker)
    end = src.find(end_marker, start)
    return start, end, src[start:end] if start > -1 and end > start else ""


class TestTmInterceptBlockBoundary(unittest.TestCase):
    """Guard the AST/source scan itself: the block boundaries are found.

    If the block moves or its anchors are renamed, this test fails
    first so the maintainer refreshes the regression instead of
    letting the gate checks silently pass on an empty slice.
    """

    def test_tm_intercept_block_exists(self):
        src = _scanner_source()
        start, end, block_src = _tm_intercept_slice(src)
        self.assertGreater(
            start,
            -1,
            "TM intercept start marker not found — refresh the regression "
            "if the section header moved.",
        )
        self.assertGreater(
            end,
            start,
            "TM intercept end marker not found after start — refresh the "
            "regression if the post-block `continue` comment moved.",
        )
        # Sanity: the slice must contain the canonical TM hooks.
        self.assertIn("_tm_intercepted", block_src)
        self.assertIn("terminal_momentum_", block_src)


class TestTmInterceptChecksDcRetryQueue(unittest.TestCase):
    """B5 fix #1: TM intercept must check `_dc_retry_queue` to catch
    decided_* IOCs in flight from prior scan ticks.

    Without this, a decided_t1 candidate emitted in a prior tick that
    queued for retry (no immediate fill) leaves `candidates` empty in
    the next tick — the existing `_tm_dc_overlap` candidate-list
    check sees nothing, and TM_98 fires on top of the in-flight
    Kelly-sized entry. Production case: KXHYPE15M-26MAY180530-30 on
    2026-05-18 09:25 UTC.
    """

    def test_tm_intercept_references_dc_retry_queue(self):
        src = _scanner_source()
        _, _, block_src = _tm_intercept_slice(src)
        self.assertTrue(
            block_src,
            "TM intercept block source slice empty — block-boundary test "
            "should have caught this first.",
        )
        # Strip line comments + triple-quoted strings so an explanatory
        # comment mentioning the token doesn't trip the guard.
        no_triple = re.sub(r'""".*?"""', "", block_src, flags=re.DOTALL)
        no_triple = re.sub(r"'''.*?'''", "", no_triple, flags=re.DOTALL)
        no_comments = "\n".join(
            ln.split("#", 1)[0] for ln in no_triple.splitlines()
        )
        self.assertRegex(
            no_comments,
            r"\b_dc_retry_queue\b",
            "TM intercept block does not reference `_dc_retry_queue` "
            "(executor's in-memory pending decided_* IOC retries). "
            "Required by B5 fix to catch cross-tick decided_* stacks "
            "that the per-tick `candidates` overlap check misses.",
        )


class TestTmInterceptBlocksNonTmOpenPositionSameSide(unittest.TestCase):
    """B5 fix #2: TM intercept must refuse when a non-TM open position
    exists on the same (ticker, side='yes').

    The existing `_tm_has_position` check only blocks same-price-group
    stacks (`strategy_group == f"terminal_momentum_{best_ask}"`).
    Different-price TM stacks are deliberately allowed (per
    `kb/strategies/terminal-momentum.md` — 40/40 stackable tickers
    settled YES). Non-TM strategies (`decided_*`, `weekend_discount`,
    `overnight_discount`, etc.) on the same (ticker, side) are NOT
    allowed to be stacked over by TM — that's the per-(ticker, side)
    entry-lock the B5 ticket calls for.

    Side filtering is required so NO-side positions (e.g.,
    `bracket_no` filled on this ticker) do NOT block YES-side TM.
    """

    def test_tm_intercept_filters_open_positions_by_side(self):
        src = _scanner_source()
        _, _, block_src = _tm_intercept_slice(src)
        self.assertTrue(block_src, "TM intercept block slice empty.")
        no_triple = re.sub(r'""".*?"""', "", block_src, flags=re.DOTALL)
        no_triple = re.sub(r"'''.*?'''", "", no_triple, flags=re.DOTALL)
        no_comments = "\n".join(
            ln.split("#", 1)[0] for ln in no_triple.splitlines()
        )
        # The block must filter open positions by side="yes" so non-TM
        # YES-side positions block TM but NO-side positions don't.
        # Accept either `p["side"] == "yes"` or `p.get("side") == "yes"`
        # patterns. Pattern uses non-capturing character class for the
        # quote chars to avoid breaking the outer raw-string delimiter.
        side_pattern = r"\bp\b[\s\S]{0,40}\bside\b[\s\S]{0,20}" + chr(0x22) + r"yes" + chr(0x22)
        side_pattern_sq = r"\bp\b[\s\S]{0,40}\bside\b[\s\S]{0,20}" + chr(0x27) + r"yes" + chr(0x27)
        self.assertTrue(
            re.search(side_pattern, no_comments) is not None
            or re.search(side_pattern_sq, no_comments) is not None,
            "TM intercept block does not filter open positions by "
            "`side == 'yes'`. Required by B5 fix so NO-side positions "
            "(e.g., bracket_no) don't block YES-side TM.",
        )

    def test_tm_intercept_filters_open_positions_by_non_tm_strategy(self):
        src = _scanner_source()
        _, _, block_src = _tm_intercept_slice(src)
        self.assertTrue(block_src, "TM intercept block slice empty.")
        no_triple = re.sub(r'""".*?"""', "", block_src, flags=re.DOTALL)
        no_triple = re.sub(r"'''.*?'''", "", no_triple, flags=re.DOTALL)
        no_comments = "\n".join(
            ln.split("#", 1)[0] for ln in no_triple.splitlines()
        )
        # Look for a `not ...startswith("terminal_momentum")` check on
        # an open-position strategy — the canonical shape of the
        # non-TM same-side block. Match either single- or double-
        # quoted argument by avoiding the literal quote char in the
        # outer raw-string delimiter.
        pattern = r"\bnot\b[\s\S]{0,120}\bstartswith\(\s*."  + r"terminal_momentum"
        self.assertRegex(
            no_comments,
            pattern,
            "TM intercept block does not check "
            "`not strategy.startswith('terminal_momentum')` on open "
            "positions. Required by B5 fix to block TM stacking on "
            "decided_*, weekend_discount, overnight_discount, etc. "
            "same-(ticker, side) Kelly-sized entries.",
        )


if __name__ == "__main__":
    unittest.main()
