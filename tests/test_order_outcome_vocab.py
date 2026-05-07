"""Regression tests for `order_outcome` vocabulary drift (2026-05-04).

Before fix: 3 maker-cancel sites in bot.py wrote `outcome = "partial_fill"`
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


BOT_PY = pathlib.Path(__file__).resolve().parents[1] / "bot.py"


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
    """Return list of (lineno, literal_value) for every `order_outcome=`
    kwarg in bot.py — direct Constant args AND via local variable named
    `outcome`/`_outcome` whose RHS is a Constant or IfExp[Constant, Constant].

    NOT covered (acceptable today; bot.py never uses these patterns for
    order_outcome — would need an explicit extension if it ever does):
      - `ast.AnnAssign` (type-annotated assigns: `outcome: str = "..."`)
      - `ast.AugAssign` / walrus `outcome := "..."`
      - Variable names other than `outcome` / `_outcome`
      - Positional args (function signature is kwarg-only on the path)
      - F-strings, .format(), concatenation feeding into the kwarg
      - **kwargs passthrough / dict-unpack
    """
    tree = ast.parse(BOT_PY.read_text())
    parent_fn = _enclosing_function_id_map(tree)

    # Pass 1: collect var assignments per enclosing function.
    # var_assignments[fn_id][var_name] = list of (lineno, literal)
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
                lits.append((node.lineno, rhs.value))
            elif isinstance(rhs, ast.IfExp):
                for branch in (rhs.body, rhs.orelse):
                    if isinstance(branch, ast.Constant) and isinstance(branch.value, str):
                        lits.append((node.lineno, branch.value))
            if lits:
                var_assignments.setdefault(fn, {}).setdefault(tgt.id, []).extend(lits)

    # Pass 2: walk all `order_outcome=` kwargs.
    found: list = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "order_outcome":
                continue
            val = kw.value
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                found.append((val.lineno, val.value))
            elif isinstance(val, ast.Name):
                fn = parent_fn.get(id(node))
                for lineno, lit in var_assignments.get(fn, {}).get(val.id, []):
                    found.append((lineno, lit))
    return found


def test_order_outcome_literals_in_allowed_set():
    literals = _collect_order_outcome_literals()
    assert literals, "Expected at least one order_outcome= literal in bot.py"
    bad = [(ln, lit) for ln, lit in literals if lit not in ALLOWED_OUTCOMES]
    assert not bad, (
        "Disallowed order_outcome literals found:\n"
        + "\n".join(f"  bot.py:{ln}: {lit!r}" for ln, lit in bad)
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
    bad = [(ln, lit) for ln, lit in literals if lit == "partial_fill"]
    assert not bad, (
        "Found 'partial_fill' as order_outcome literal — should be "
        "'partial_filled' (vocab drift regression):\n"
        + "\n".join(f"  bot.py:{ln}" for ln, _ in bad)
    )


def test_partial_filled_appears_at_expected_sites():
    """Sanity check: post-fix, `partial_filled` should appear at the
    7 sites we know about (4 DC IOC retry + 3 maker-cancel).
    """
    literals = _collect_order_outcome_literals()
    pf_count = sum(1 for _, lit in literals if lit == "partial_filled")
    assert pf_count >= 7, (
        f"Expected ≥7 'partial_filled' order_outcome sites (4 DC retry + "
        f"3 maker-cancel), found {pf_count}. Check the fix landed and the "
        f"AST collector is finding both direct kwargs and variable flows."
    )
