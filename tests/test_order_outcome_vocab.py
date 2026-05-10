"""Regression tests for `order_outcome` vocabulary drift (2026-05-04).

Before fix: 3 maker-cancel sites in bot/_impl.py wrote `outcome = "partial_fill"`
inconsistent with 4 DC IOC retry sites using `order_outcome="partial_filled"`.
Production DB confirmed both string values for the same logical concept.

`partial_fill` (singular) is reserved for the DIFFERENT `event_type` field on
`order_lifecycle_snapshots` (DB CHECK constraint:
`event_type IN ('submit','fill','partial_fill','cancel')`). Don't conflate.
"""

import ast
import pathlib


ALLOWED_OUTCOMES = {
    "filled",
    "unfilled",
    "unfilled_retry",
    "skipped_near_close",
    "canceled",
    "escalation_edge_abort",
    "partial_retry",
    "partial_filled",
    "unfilled_window_closed",
    "unfilled_price_collapsed",
    "unfilled_price_drift",
    "expired",
}


BOT_PY = pathlib.Path(__file__).resolve().parents[1] / "bot/_impl.py"
EXECUTOR_PY = pathlib.Path(__file__).resolve().parents[1] / "bot/executor.py"
# Bit 9.1 (2026-05-10): order_outcome= kwargs originate in OrderExecutor (now in
# bot/executor.py post-extraction) — primarily the maker-cancel paths from the
# Bit 4.1 commit `dd3b23e` that this AST guard was originally written for. We
# walk BOTH bot/_impl.py (residual SettlementTracker + MainLoop sites) AND
# bot/executor.py to keep the guard's coverage complete. Future Bits 9.2/9.3
# extracting SettlementTracker / MainLoop will leave only bot/executor.py with
# the relevant order_outcome= sites.
SCANNED_PATHS = (BOT_PY, EXECUTOR_PY)


def _enclosing_function_id_map(tree):
    """Build a map node_id → enclosing function node_id (or None for module)."""
    parent_fn = {id(tree): None}
    stack = [(tree, None)]
    while stack:
        node, fn = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parent_fn[id(child)] = id(child)
                stack.append((child, id(child)))
            else:
                parent_fn[id(child)] = fn
                stack.append((child, fn))
    return parent_fn


def _collect_order_outcome_literals():
    """Return list of (path, lineno, literal_value) for every `order_outcome=`
    kwarg across SCANNED_PATHS (bot/_impl.py + bot/executor.py post-Bit-9.1) —
    direct Constant args AND via local variable named `outcome`/`_outcome`
    whose RHS is a Constant or IfExp[Constant, Constant].

    NOT covered (acceptable today; the scanned modules never use these patterns for
    order_outcome — would need an explicit extension if they ever do):
      - `ast.AnnAssign` (type-annotated assigns: `outcome: str = "..."`)
      - `ast.AugAssign` / walrus `outcome := "..."`
      - Variable names other than `outcome` / `_outcome`
      - Positional args (function signature is kwarg-only on the path)
      - F-strings, .format(), concatenation feeding into the kwarg
      - **kwargs passthrough / dict-unpack
    """
    found: list = []
    for path in SCANNED_PATHS:
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text())
        parent_fn = _enclosing_function_id_map(tree)

        # Pass 1: collect var assignments per enclosing function.
        var_assignments: dict = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            fn = parent_fn.get(id(node))
            for tgt in node.targets:
                if not (isinstance(tgt, ast.Name) and tgt.id in {"outcome", "_outcome"}):
                    continue
                rhs = node.value
                lits: list = []
                if isinstance(rhs, ast.Constant) and isinstance(rhs.value, str):
                    lits.append((path, node.lineno, rhs.value))
                elif isinstance(rhs, ast.IfExp):
                    for branch in (rhs.body, rhs.orelse):
                        if isinstance(branch, ast.Constant) and isinstance(branch.value, str):
                            lits.append((path, node.lineno, branch.value))
                if lits:
                    var_assignments.setdefault(fn, {}).setdefault(tgt.id, []).extend(lits)

        # Pass 2: walk all `order_outcome=` kwargs.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "order_outcome":
                    continue
                val = kw.value
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    found.append((path, val.lineno, val.value))
                elif isinstance(val, ast.Name):
                    fn = parent_fn.get(id(node))
                    for assign_path, lineno, lit in var_assignments.get(fn, {}).get(val.id, []):
                        found.append((assign_path, lineno, lit))
    return found


def test_order_outcome_literals_in_allowed_set():
    literals = _collect_order_outcome_literals()
    assert literals, (
        "Expected at least one order_outcome= literal across "
        f"{[str(p) for p in SCANNED_PATHS]}"
    )
    bad = [(p, ln, lit) for p, ln, lit in literals if lit not in ALLOWED_OUTCOMES]
    assert not bad, (
        "Disallowed order_outcome literals found:\n"
        + "\n".join(f"  {p.name}:{ln}: {lit!r}" for p, ln, lit in bad)
        + f"\nAllowed: {sorted(ALLOWED_OUTCOMES)}"
    )


def test_no_partial_fill_as_order_outcome():
    """The maker-cancel paths (force-pop backstop, cancel-404 handler,
    regular cancel) previously used 'partial_fill' for `order_outcome`.
    After the 2026-05-04 fix they must use 'partial_filled' to match
    the DC IOC retry sites and the production past-tense vocabulary
    (filled/unfilled/partial_filled).
    """
    literals = _collect_order_outcome_literals()
    bad = [(p, ln, lit) for p, ln, lit in literals if lit == "partial_fill"]
    assert not bad, (
        "Found 'partial_fill' as order_outcome literal — should be "
        "'partial_filled' (vocab drift regression):\n"
        + "\n".join(f"  {p.name}:{ln}" for p, ln, _ in bad)
    )


def test_partial_filled_appears_at_expected_sites():
    """Sanity check: post-fix, `partial_filled` should appear at the
    7 sites we know about (4 DC IOC retry + 3 maker-cancel).

    Bit 9.1: these sites are now in bot/executor.py (OrderExecutor extracted from
    bot/_impl.py); _collect_order_outcome_literals walks both files for coverage.
    """
    literals = _collect_order_outcome_literals()
    pf_count = sum(1 for _, _, lit in literals if lit == "partial_filled")
    assert pf_count >= 7, (
        f"Expected ≥7 'partial_filled' order_outcome sites (4 DC retry + "
        f"3 maker-cancel), found {pf_count}. Check the fix landed and the "
        f"AST collector is finding both direct kwargs and variable flows."
    )
