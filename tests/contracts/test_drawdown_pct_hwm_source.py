"""DD-3: current_drawdown_pct must read PositionSizer rolling HWM,
not MainLoop._session_hwm_balance.

Origin (ClickUp 86b9z6yhw, parent umbrella 86b9z6qrf, 2026-05-15):
The scanner had two competing HWM sources:
  (1) PositionSizer.get_rolling_hwm() — 7-day rolling, used by the
      drawdown scaler that actually clamps sizing.
  (2) MainLoop._session_hwm_balance — session-scoped, resets each restart.

The `current_drawdown_pct` feature column was wired to (2), so it read
"0.0" while (1) was actively in halt-floor at ratio=0.626. Two HWMs,
two contradictory answers. This contract pins `current_drawdown_pct`
to source (1) — the same HWM the scaler reads — so the column and the
scaler can never disagree again.

Sister anchors:
  - bot/models.py::PositionSizer.get_rolling_hwm (canonical HWM)
  - bot/models.py::PositionSizer._balance_history (deque of (t, balance))
  - kb/findings/drawdown-signal-drift-may15.md (postmortem, post-ship)
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNER_PY = REPO_ROOT / "bot" / "scanner" / "__init__.py"


@pytest.fixture(scope="module")
def scanner_source() -> str:
    assert SCANNER_PY.exists(), f"canonical source missing: {SCANNER_PY}"
    return SCANNER_PY.read_text()


@pytest.fixture(scope="module")
def scanner_ast(scanner_source: str) -> ast.Module:
    return ast.parse(scanner_source, filename=str(SCANNER_PY))


def _find_drawdown_pct_assignments(tree: ast.Module) -> list[ast.Assign]:
    """Return every `drawdown_pct = ...` assignment in the scanner.

    Matches `ast.Assign` nodes whose single target is `Name('drawdown_pct')`.
    The 0.0-initialization and the conditional reassignment are both
    Assign nodes; both must come from the same scope to be considered."""
    found: list[ast.Assign] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "drawdown_pct":
            found.append(node)
    return found


def _enclosing_function(
    tree: ast.Module, target_node: ast.AST
) -> ast.FunctionDef | None:
    """Find the FunctionDef/AsyncFunctionDef that lexically contains
    `target_node`. Walks every function and checks line range."""
    target_line = getattr(target_node, "lineno", None)
    if target_line is None:
        return None
    candidates: list[ast.FunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", None) or node.lineno
            if node.lineno <= target_line <= end:
                candidates.append(node)
    if not candidates:
        return None
    # Innermost wins (largest start lineno)
    return max(candidates, key=lambda fn: fn.lineno)


def _function_source_slice(
    source: str, fn: ast.FunctionDef
) -> str:
    """Return the source text of the function body."""
    lines = source.splitlines()
    start = fn.lineno - 1
    end = (getattr(fn, "end_lineno", None) or fn.lineno)
    return "\n".join(lines[start:end])


def test_drawdown_pct_reads_sizer_rolling_hwm(
    scanner_source: str, scanner_ast: ast.Module
) -> None:
    """The function that computes `drawdown_pct` must reference the
    canonical PositionSizer HWM source, NOT MainLoop's session-scoped
    `_session_hwm_balance`.

    Required substrings in the function body:
      - `_sizer.get_rolling_hwm` (canonical HWM accessor)
      - `_sizer._balance_history` (current portfolio balance source)

    Forbidden substrings in the function body:
      - `_session_hwm_balance` (session-scoped — the bug source)
      - `_last_known_balance` (paired with the bug source; OK to use
        elsewhere in scanner but NOT inside the drawdown_pct computation)
    """
    assigns = _find_drawdown_pct_assignments(scanner_ast)
    assert assigns, (
        "no `drawdown_pct = ...` assignment found in bot/scanner/__init__.py "
        "— site was deleted or renamed; update this test in lockstep."
    )

    # The conditional reassignment (the one inside the if-block) is the
    # one that matters. Pick the assignment with the highest lineno —
    # that's the final write to drawdown_pct before it leaves the scope.
    final_assign = max(assigns, key=lambda a: a.lineno)
    enclosing = _enclosing_function(scanner_ast, final_assign)
    assert enclosing is not None, (
        f"drawdown_pct assignment at line {final_assign.lineno} is not "
        "inside any function — unexpected scope."
    )
    body_src = _function_source_slice(scanner_source, enclosing)

    # Required semantic content — match the accessors regardless of whether
    # the code uses `self._sizer.X` directly or via a `getattr(self, "_sizer", ...)`
    # local rebinding. The presence of all three substrings pins the
    # PositionSizer rolling-HWM source.
    required = ("_sizer", "get_rolling_hwm", "_balance_history")
    missing = [r for r in required if r not in body_src]
    forbidden = ("_session_hwm_balance", "_last_known_balance")
    present = [f for f in forbidden if f in body_src]

    msgs: list[str] = []
    if missing:
        msgs.append(
            f"required HWM source(s) missing from {enclosing.name!r} body "
            f"(line {enclosing.lineno}): {missing}"
        )
    if present:
        msgs.append(
            f"forbidden session-HWM reference(s) present in {enclosing.name!r} body "
            f"(line {enclosing.lineno}): {present}. These read MainLoop's "
            "session-scoped HWM which disagrees with the scaler's rolling HWM."
        )
    if msgs:
        raise AssertionError(
            "current_drawdown_pct must source from PositionSizer's rolling HWM "
            "(see ClickUp 86b9z6yhw):\n  - " + "\n  - ".join(msgs)
        )
