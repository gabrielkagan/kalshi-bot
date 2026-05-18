"""Regression for B3: LPNE branch references unbound `final_prob` at insert site.

2026-05-18 incident (HYPE dc_retry crash window, ticket 86b9zud6t):
The LPNE intercept block in `OpportunityScanner.scan` (bot/scanner/__init__.py)
fires when `best_ask < _asset_floor` — BEFORE the main probability cascade
that assigns `final_prob = prob_with_market["calibrated_prob"]`. The
`insert_evaluated_opportunity(...)` call inside the LPNE block uses
`calibrated_prob=final_prob`, raising `UnboundLocalError` for every LPNE
candidate (caught by the surrounding `except Exception:` and logged as a
WARNING — so the LPNE row is silently dropped from `evaluated_opportunities`,
defeating the row-writing contract that downstream training/audit relies on).

The candidate dict assembled immediately above the insert call (line ~2740)
already uses `cal_prob` ("calibrated_prob": round(cal_prob, 6)) — that is the
correct bound name at this point in scan().  The DB insert must use the same
name.

Fix shape: replace `calibrated_prob=final_prob` with `calibrated_prob=cal_prob`
in the LPNE `insert_evaluated_opportunity` call.

This is TDD-RED against unfixed code: the AST guard finds an
`insert_evaluated_opportunity` call whose 4th positional arg is the string
literal `"low_price_near_expiry"` and whose `calibrated_prob` kwarg is
`Name(id="final_prob")` — exactly the latent bug.
"""
import ast
import os
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


def _find_lpne_insert_calls(tree: ast.AST) -> list[ast.Call]:
    """Return Call nodes matching `*.insert_evaluated_opportunity(...,
    "low_price_near_expiry", ...)` — the LPNE row write.

    The match is by 4th positional arg being the string literal
    "low_price_near_expiry" (matches the signature shape
    `insert_evaluated_opportunity(ticker, event_ticker, asset, strategy, ...)`).
    """
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "insert_evaluated_opportunity":
            continue
        # 4th positional arg (index 3) should be the strategy string literal
        if len(node.args) < 4:
            continue
        strat_arg = node.args[3]
        if (
            isinstance(strat_arg, ast.Constant)
            and strat_arg.value == "low_price_near_expiry"
        ):
            out.append(node)
    return out


class TestLpneCalibratedProbBoundName(unittest.TestCase):
    """LPNE `insert_evaluated_opportunity` must use `cal_prob` for
    `calibrated_prob` — `final_prob` is not yet bound at that point in
    `scan()`."""

    def test_lpne_insert_call_exists(self):
        """Guard the AST search itself: the LPNE insert call is present.

        If the LPNE block is removed or the strategy literal is renamed,
        this test fails first and tells the maintainer to refresh the
        regression instead of letting the kwarg check silently pass on
        zero matches.
        """
        tree = ast.parse(_scanner_source(), filename=SCANNER_PATH)
        calls = _find_lpne_insert_calls(tree)
        self.assertGreaterEqual(
            len(calls),
            1,
            "LPNE insert_evaluated_opportunity call (4th positional arg "
            '"low_price_near_expiry") not found — refresh the regression '
            "if the LPNE block moved/renamed.",
        )

    def test_lpne_calibrated_prob_uses_cal_prob_not_final_prob(self):
        """Each LPNE insert_evaluated_opportunity must pass `cal_prob`
        (not `final_prob`) for the `calibrated_prob` kwarg.

        `final_prob` is assigned only later in scan() (after the
        per-asset price-floor branch); referencing it in the LPNE block
        raises UnboundLocalError and silently kills the DB row write.
        `cal_prob` is the canonical bound name at this point and is
        already used by the LPNE candidate dict above the insert call.
        """
        tree = ast.parse(_scanner_source(), filename=SCANNER_PATH)
        calls = _find_lpne_insert_calls(tree)
        offenders: list[tuple[int, str]] = []
        missing: list[int] = []
        for call in calls:
            cp_kw = next(
                (kw for kw in call.keywords if kw.arg == "calibrated_prob"),
                None,
            )
            if cp_kw is None:
                missing.append(call.lineno)
                continue
            val = cp_kw.value
            # We require the kwarg value to be a Name node bound at this
            # point in scan(). `final_prob` is the known-broken case; we
            # additionally pin it to `cal_prob` (the canonical name used
            # by the LPNE candidate dict directly above) for tightness.
            if isinstance(val, ast.Name) and val.id == "final_prob":
                offenders.append((call.lineno, "final_prob"))
            elif not (isinstance(val, ast.Name) and val.id == "cal_prob"):
                # Some other unexpected form — fail loud rather than
                # silently passing.
                offenders.append(
                    (call.lineno, ast.dump(val, annotate_fields=False))
                )
        self.assertEqual(
            offenders,
            [],
            "LPNE insert_evaluated_opportunity uses unbound/unexpected "
            f"name for `calibrated_prob`: {offenders}. Expected "
            "`cal_prob` (the LPNE candidate dict on the line above "
            "already uses `cal_prob`).",
        )
        self.assertEqual(
            missing,
            [],
            f"LPNE insert call(s) missing `calibrated_prob` kwarg at "
            f"lines: {missing}",
        )


class TestLpneBlockHasNoFinalProbReference(unittest.TestCase):
    """Defense in depth: scan the LPNE block as a whole and assert no
    `final_prob` Name token appears in it.  Catches the same bug
    reappearing elsewhere inside the LPNE intercept (e.g., a future
    edit that adds another field reading `final_prob`).

    The block boundary is located by two distinctive source markers
    so a future renumbering doesn't silently invalidate the test.  We
    do a word-boundary regex pass on the slice — simpler and more
    robust than trying to dedent + ast-parse an indented function
    fragment.
    """

    def test_no_final_prob_name_in_lpne_block(self):
        import re

        src = _scanner_source()
        start_marker = "# ── LPNE intercept: BTC 80-87c near-expiry"
        end_marker = (
            "continue  # Skip floor rejection — this is now an LPNE candidate"
        )
        start = src.find(start_marker)
        end = src.find(end_marker, start)
        self.assertGreater(
            start,
            -1,
            f"LPNE intercept start marker not found: {start_marker!r}",
        )
        self.assertGreater(
            end,
            start,
            f"LPNE intercept end marker not found after start: {end_marker!r}",
        )
        block_src = src[start:end]
        # Strip line comments and triple-quoted strings before matching
        # so that an explanatory comment mentioning `final_prob` doesn't
        # trip the guard.  (Triple-quoted strings are unlikely inside
        # this control-flow block but we strip defensively.)
        no_triple = re.sub(r'""".*?"""', "", block_src, flags=re.DOTALL)
        no_triple = re.sub(r"'''.*?'''", "", no_triple, flags=re.DOTALL)
        # Strip everything after `#` on each line (line comments).
        no_comments = "\n".join(
            ln.split("#", 1)[0] for ln in no_triple.splitlines()
        )
        # Word-boundary match on the bare identifier.
        offender_lines: list[tuple[int, str]] = []
        for i, ln in enumerate(no_comments.splitlines(), start=1):
            if re.search(r"\bfinal_prob\b", ln):
                # Translate the in-block line number back to a file
                # line for the failure message.
                file_line = src[:start].count("\n") + i
                offender_lines.append((file_line, ln.strip()))
        self.assertEqual(
            offender_lines,
            [],
            "LPNE intercept block references `final_prob` (unbound at "
            f"this point in scan()) at file line(s): {offender_lines}. "
            "Use `cal_prob` instead — `final_prob` is assigned only "
            "after the per-asset price floor branch.",
        )


if __name__ == "__main__":
    unittest.main()
