"""B3-fu7 (ticket 86ba067mg, 2026-05-18) — generalized narrow-except contract.

B3 (`86b9zud6t`) was a 42-day silent LPNE row drop driven by an
`UnboundLocalError` at an `insert_evaluated_opportunity` site, hidden
behind a bare `except Exception:` that downgraded the crash to a
WARNING. B3-fu2 + B3-fu6 narrowed two known-bad sites (LPNE +
dc_shadow_no_side POR) to `except sqlite3.OperationalError as e:`. B3-fu7
generalizes the discipline to EVERY simple-body try-wrap around an
insert site in `bot/scanner/__init__.py`.

## Rule

For every `ast.Try` node in `bot/scanner/__init__.py` whose body
contains a call to `insert_evaluated_opportunity` or `insert_rejection`:

  - If the body is "simple" — zero non-trivial statements beyond the
    insert/log/append/add calls AND the body spans fewer than
    `MAX_SIMPLE_BODY_LINES` lines — the handler MUST be
    `sqlite3.OperationalError` (or a Tuple containing it).
  - If the body is "complex" — any additional compute that could
    throw non-DB exceptions (e.g., `_sizer.compute()`, dict reads
    on non-stable shapes, `evaluate_execution_strategy` calls) — the
    try-wrap is EXEMPT by shape. These sites are tracked under the
    B3-fu7-followup ticket for individual review; the L106 lesson
    applies, but a single mechanical narrow would silence legitimate
    bug surfaces.

The structural classifier is intentionally conservative: a `body_lines`
window + `non_trivial_statement_count == 0` heuristic. Both can be
tuned over time as the call sites refactor; the test fires loud when
a new simple-body insert site lands without the narrow, OR when a
previously-complex site refactors below the threshold without being
narrowed.

## Why narrow `sqlite3.OperationalError` specifically?

`OperationalError` is the family `state.db` raises for transient
conditions (`database is locked`, disk-full, busy_timeout exceeded).
These are the WARNINGs we want to keep. `NameError` /
`UnboundLocalError` / `AttributeError` / `KeyError` are the **silent
bug class** B3 closed — they indicate broken code that should crash
the scan tick, not silently drop a row.

## Out of scope

- Narrowing the 12 COMPLEX try-wraps (deferred — each needs per-site
  judgement on which compute exceptions to keep swallowed for
  resilience vs. which to surface).
- Narrowing bare-except elsewhere in the scanner (e.g., balance fetch
  resilience, OFA signal compute, fill latency tracking). Those are
  different concerns and live in different gates.

This is TDD-RED against unfixed code: the 44 simple-body sites that
remain bare-except will fail.
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

# Method attrs the body can contain WITHOUT being "non-trivial".
INSERT_NAMES = {"insert_evaluated_opportunity", "insert_rejection"}
TRIVIAL_METHOD_ATTRS = INSERT_NAMES | {
    "log_opportunity", "log_execution", "log_trade",
    "append", "add",
}

# A try-block whose body is shorter than this AND has zero non-trivial
# statements counts as "simple". Threshold tuned to cover the simple
# dedup-wrapped insert sites (max observed: 33 lines) without sweeping
# in the larger compute-heavy blocks (min observed complex: 56 lines).
MAX_SIMPLE_BODY_LINES = 50


def _scanner_source() -> str:
    with open(SCANNER_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _scanner_tree() -> ast.AST:
    return ast.parse(_scanner_source(), filename=SCANNER_PATH)


def _body_contains_insert(body_list):
    for stmt in body_list:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in INSERT_NAMES:
                    return node
    return None


def _is_narrow(handler_type) -> bool:
    if handler_type is None:
        return False
    if (
        isinstance(handler_type, ast.Attribute)
        and isinstance(handler_type.value, ast.Name)
        and handler_type.value.id == "sqlite3"
        and handler_type.attr == "OperationalError"
    ):
        return True
    if isinstance(handler_type, ast.Tuple):
        return any(_is_narrow(e) for e in handler_type.elts)
    return False


def _is_trivial_statement(node) -> bool:
    """`Expr` wrapping a method call to one of the trivial method
    attrs (`insert_*`, `log_*`, `.append()`, `.add()`).
    """
    if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
        return False
    call = node.value
    attr = call.func.attr if isinstance(call.func, ast.Attribute) else ""
    return (
        attr in TRIVIAL_METHOD_ATTRS
        or attr.startswith("log_")
    )


def _classify_body(body):
    """Return (non_trivial_statement_count, expanded_statement_count).

    Expands single-level dedup `if X not in seen: seen.add(X); insert(...)`
    guards so the inner statements are counted directly (the `if`
    wrapper is considered structural, not compute).
    """
    expanded = []
    for s in body:
        if isinstance(s, ast.If):
            expanded.extend(s.body)
        else:
            expanded.append(s)
    nontriv = sum(0 if _is_trivial_statement(s) else 1 for s in expanded)
    return nontriv, len(expanded)


def _enumerate_simple_bare_sites():
    """Return [(try_lineno, handler_lineno, insert_attr), ...] for every
    SIMPLE-body try-wrap around an insert call whose handler is NOT
    narrowed to sqlite3.OperationalError. These are the B3-fu7 target
    sites that must be tightened.
    """
    tree = _scanner_tree()
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        ic = _body_contains_insert(node.body)
        if ic is None:
            continue
        nontriv, _total = _classify_body(node.body)
        # body_lines: span from try-start to first handler.
        if not node.handlers:
            continue
        first_handler = node.handlers[0]
        body_lines = first_handler.lineno - node.lineno
        if nontriv != 0 or body_lines >= MAX_SIMPLE_BODY_LINES:
            continue  # complex — exempt by shape
        for h in node.handlers:
            if not _is_narrow(h.type):
                ht = ast.unparse(h.type) if h.type is not None else "BARE_EXCEPT"
                offenders.append((node.lineno, h.lineno, ic.func.attr, ht))
                break  # one report per try-block
    return offenders


class TestB3Fu7AllSimpleInsertSitesAreNarrowed(unittest.TestCase):
    """Every simple-body try-wrap around `insert_evaluated_opportunity`
    or `insert_rejection` in `bot/scanner/__init__.py` must catch
    `sqlite3.OperationalError`, NOT `Exception`. Closes the L106
    silent-bug class generalized from B3-fu2/fu6 to all simple sites.
    """

    def test_no_simple_insert_site_uses_bare_exception(self):
        offenders = _enumerate_simple_bare_sites()
        if offenders:
            details = "\n".join(
                f"  - try@L{tl} except@L{hl} ({ia}) currently `except {ht}:`"
                for tl, hl, ia, ht in offenders
            )
            self.fail(
                "B3-fu7 narrow-except rule violated. The following simple-body "
                "try-wraps around insert_evaluated_opportunity / insert_rejection "
                "must catch `sqlite3.OperationalError` (not `Exception`), so "
                "NameError / UnboundLocalError / AttributeError / KeyError "
                "propagate to the WS-thread top level instead of being silently "
                "swallowed (B3 RCA class). Found "
                f"{len(offenders)} offending site(s):\n{details}\n\n"
                "Fix: change each `except Exception:` to "
                "`except sqlite3.OperationalError as e:` (and keep the existing "
                "logging.warning(...) call). The L106 lesson applies to every "
                "site where the try-body is just the insert + log call — no "
                "compute that could throw a non-DB exception."
            )


class TestB3Fu7ScannerSqlite3ImportPresent(unittest.TestCase):
    """The `import sqlite3` top-import must be present for the narrow
    handlers to resolve. Sister of `test_b3_fu2_fu6_lpne_narrow_except_regression.py`'s
    import check — duplicated here so a future deletion of that file
    doesn't silently break B3-fu7's contract.
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
                found = True
            if found:
                break
        self.assertTrue(
            found,
            "bot/scanner/__init__.py must top-import `sqlite3` so the narrow "
            "`except sqlite3.OperationalError` clauses resolve at module load.",
        )


if __name__ == "__main__":
    unittest.main()
