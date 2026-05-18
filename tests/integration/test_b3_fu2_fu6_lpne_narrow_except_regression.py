"""Regression for B3-fu2 + B3-fu6 (tickets 86b9zxb02 + 86b9zxbz0, 2026-05-18).

B3 (`86b9zud6t`) was a 42-day silent LPNE row drop driven by an
UnboundLocalError at `bot/scanner/__init__.py` LPNE
`insert_evaluated_opportunity` site — hidden behind a bare
`except Exception:` swallow that downgraded the crash to a WARNING.

The L106 lesson generalizes: any `except Exception:` around a DB-write
site silently masks the NameError / UnboundLocalError class. B3-fu2
narrows the LPNE swallow to `sqlite3.OperationalError` so the DB-class
errors (disk-full / corruption / busy / database-locked) still surface
as WARNINGs while NameError / UnboundLocalError / AttributeError
propagate to the WS-thread top level.

B3-fu6 is the sister audit at the `dc_shadow_no_side` POR path
(`insert_evaluated_opportunity(..., "dc_shadow_no_side", ...)`):
the call uses `ofa_adjustment=ofa_adjustment` even though
`ofa_adjustment = 0.0` is initialized ~300 lines below in `scan()`
(well after the dc_shadow_no_side block). That site is reachable on
`_por_z >= 5.0 + best_ask <= 20 + _no_ask_dc in (1, 93]` — when it
fires, `ofa_adjustment` is unbound -> UnboundLocalError -> silently
swallowed by the surrounding `except Exception:` (sister of the LPNE
swallow). Same class as B3.

Fix shape:
- Top of `bot/scanner/__init__.py`: add `import sqlite3`.
- LPNE `insert_evaluated_opportunity` `try/except`:
  `except Exception:` -> `except sqlite3.OperationalError as e:`.
- dc_shadow_no_side POR `insert_evaluated_opportunity` `try/except`:
  same narrow.
- dc_shadow_no_side POR insert kwarg:
  `ofa_adjustment=ofa_adjustment` -> `ofa_adjustment=0.0` (literal —
  the dc_shadow_no_side POR path runs BEFORE the OFA signal
  computation in `scan()`, mirrors the LPNE candidate-dict pattern
  `"ofa_adjustment": 0.0` immediately above the LPNE insert).

(Line numbers omitted — the AST guards locate sites by strategy-literal
match, not by line, so drift in the source doesn't invalidate the test.)

This is TDD-RED against unfixed code: each AST guard finds the
canonical bare-Exception / unbound-name shape and fails.
"""
from __future__ import annotations

import ast
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, REPO_ROOT)

SCANNER_PATH = os.path.join(REPO_ROOT, "bot", "scanner", "__init__.py")


def _scanner_source() -> str:
    with open(SCANNER_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _scanner_tree() -> ast.AST:
    return ast.parse(_scanner_source(), filename=SCANNER_PATH)


def _find_insert_calls_by_strategy(tree: ast.AST, strategy: str) -> list[ast.Call]:
    """Find `*.insert_evaluated_opportunity(..., <strategy>, ...)` calls.

    The match is by 4th positional arg being the string literal
    matching `strategy`.
    """
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "insert_evaluated_opportunity":
            continue
        if len(node.args) < 4:
            continue
        strat_arg = node.args[3]
        if isinstance(strat_arg, ast.Constant) and strat_arg.value == strategy:
            out.append(node)
    return out


def _is_sqlite3_operationalerror(handler_type: ast.expr | None) -> bool:
    """True iff the handler's exception type is `sqlite3.OperationalError`
    OR a Tuple containing it. Bare `Exception` or `BaseException` is False.
    """
    if handler_type is None:
        return False  # bare `except:` — not narrow

    def _matches(node: ast.expr) -> bool:
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "sqlite3"
            and node.attr == "OperationalError"
        ):
            return True
        return False

    if _matches(handler_type):
        return True
    if isinstance(handler_type, ast.Tuple):
        return any(_matches(e) for e in handler_type.elts)
    return False


def _find_try_wrapping_call(tree: ast.AST, target_call: ast.Call) -> ast.Try | None:
    """Walk back from the target Call to find the smallest enclosing Try."""
    # ast doesn't track parents; we do an explicit walk capturing the
    # enclosing Try by checking whether target_call is inside any Try
    # in the tree. Multiple LPNE / dc_shadow_no_side call sites exist
    # at different code paths; we return the first Try whose body
    # contains target_call (Python guarantees one direct enclosing Try
    # per Call since Try blocks don't overlap at the same nesting).
    best: ast.Try | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for body_node in node.body:
            for sub in ast.walk(body_node):
                if sub is target_call:
                    # tighter (more nested) Try wins
                    if best is None or node.lineno > best.lineno:
                        best = node
                    break
    return best


class TestScannerImportsSqlite3(unittest.TestCase):
    """`bot/scanner/__init__.py` must top-import `sqlite3` so the narrow
    `except sqlite3.OperationalError` form resolves at module load.
    """

    def test_scanner_top_imports_sqlite3(self):
        tree = _scanner_tree()
        found = False
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "sqlite3":
                        found = True
                        break
            elif isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
                # `from sqlite3 import OperationalError` would also work
                # but we standardize on `import sqlite3` for the dotted form.
                found = True
            if found:
                break
        self.assertTrue(
            found,
            "bot/scanner/__init__.py must top-import sqlite3 so the "
            "narrow `except sqlite3.OperationalError` clauses resolve.",
        )


class TestLpneInsertExceptIsSqliteNarrow(unittest.TestCase):
    """B3-fu2: the LPNE `insert_evaluated_opportunity` `except` clause
    must be narrowed to `sqlite3.OperationalError` so NameError /
    UnboundLocalError / AttributeError propagate (the B3 RCA class).
    """

    def test_lpne_insert_except_is_sqlite3_operationalerror(self):
        tree = _scanner_tree()
        calls = _find_insert_calls_by_strategy(tree, "low_price_near_expiry")
        self.assertGreaterEqual(
            len(calls), 1, "LPNE insert call not found — refresh guard."
        )
        offenders: list[tuple[int, str]] = []
        for call in calls:
            try_node = _find_try_wrapping_call(tree, call)
            if try_node is None:
                offenders.append(
                    (call.lineno, "no enclosing Try (insert not wrapped)")
                )
                continue
            # The first handler is the one that fires for this insert.
            if not try_node.handlers:
                offenders.append((try_node.lineno, "Try with no handlers"))
                continue
            # ALL handlers covering this site must be narrowed (the
            # idiomatic shape is one handler — if someone adds a
            # second, both should be specific).
            for handler in try_node.handlers:
                if not _is_sqlite3_operationalerror(handler.type):
                    dump = (
                        ast.dump(handler.type, annotate_fields=False)
                        if handler.type is not None
                        else "bare except:"
                    )
                    offenders.append((handler.lineno, dump))
        self.assertEqual(
            offenders,
            [],
            "LPNE insert_evaluated_opportunity is wrapped in a broad "
            f"`except Exception:` (or similar) — found: {offenders}. "
            "Narrow to `except sqlite3.OperationalError as e:` so the "
            "L106 NameError/UnboundLocalError class propagates.",
        )


class TestDcShadowNoSideInsertExceptIsSqliteNarrow(unittest.TestCase):
    """B3-fu6: the dc_shadow_no_side `insert_evaluated_opportunity`
    `except` clause must be narrowed (sister of LPNE).
    """

    def test_dc_shadow_no_side_insert_except_is_sqlite3_operationalerror(self):
        tree = _scanner_tree()
        calls = _find_insert_calls_by_strategy(tree, "dc_shadow_no_side")
        self.assertGreaterEqual(
            len(calls), 1, "dc_shadow_no_side insert call not found — refresh guard."
        )
        offenders: list[tuple[int, str]] = []
        for call in calls:
            try_node = _find_try_wrapping_call(tree, call)
            if try_node is None:
                # Some dc_shadow_no_side call sites legitimately are not
                # wrapped in a Try (different code paths). Only enforce
                # the narrow on sites that ARE wrapped — for the POR
                # path (line ~2667), the existing Try is the surface
                # we're tightening.
                continue
            for handler in try_node.handlers:
                if not _is_sqlite3_operationalerror(handler.type):
                    dump = (
                        ast.dump(handler.type, annotate_fields=False)
                        if handler.type is not None
                        else "bare except:"
                    )
                    offenders.append((handler.lineno, dump))
        self.assertEqual(
            offenders,
            [],
            "dc_shadow_no_side insert_evaluated_opportunity is wrapped "
            f"in a broad `except Exception:` (or similar) — found: "
            f"{offenders}. Narrow to `except sqlite3.OperationalError "
            "as e:` so the L106 NameError/UnboundLocalError class "
            "propagates (B3-fu6 sister of B3-fu2).",
        )


class TestDcShadowNoSideOfaAdjustmentIsLiteralZero(unittest.TestCase):
    """B3-fu6: the dc_shadow_no_side POR-path
    `insert_evaluated_opportunity` call must pass a literal `0.0` for
    `ofa_adjustment`, not the bare name `ofa_adjustment`. At this
    point in `scan()`, the local `ofa_adjustment = 0.0` initialization
    has not yet executed (it's ~300 lines below) — referencing the
    name raises UnboundLocalError, exactly the B3 class.

    The LPNE candidate dict immediately above the LPNE insert uses the
    literal-0.0 pattern (`"ofa_adjustment": 0.0`) for the same reason.
    """

    def test_dc_shadow_no_side_ofa_adjustment_is_literal_zero(self):
        tree = _scanner_tree()
        calls = _find_insert_calls_by_strategy(tree, "dc_shadow_no_side")
        self.assertGreaterEqual(
            len(calls), 1, "dc_shadow_no_side insert call not found — refresh guard."
        )
        offenders: list[tuple[int, str]] = []
        for call in calls:
            ofa_kw = next(
                (kw for kw in call.keywords if kw.arg == "ofa_adjustment"),
                None,
            )
            if ofa_kw is None:
                # Missing kwarg is itself a regression — flag it.
                offenders.append((call.lineno, "missing ofa_adjustment kwarg"))
                continue
            val = ofa_kw.value
            if isinstance(val, ast.Constant) and val.value == 0.0:
                continue  # OK
            # Bare Name is the known-broken case (UnboundLocalError);
            # any other form is unexpected and worth flagging.
            if isinstance(val, ast.Name):
                offenders.append((call.lineno, f"Name(id={val.id!r}) — unbound"))
            else:
                offenders.append(
                    (call.lineno, ast.dump(val, annotate_fields=False))
                )
        self.assertEqual(
            offenders,
            [],
            "dc_shadow_no_side insert_evaluated_opportunity must pass "
            f"`ofa_adjustment=0.0` literal — found: {offenders}. The "
            "local `ofa_adjustment = 0.0` initialization is ~300 lines "
            "below this site; bare-name reference raises "
            "UnboundLocalError (same class as B3).",
        )


if __name__ == "__main__":
    unittest.main()
