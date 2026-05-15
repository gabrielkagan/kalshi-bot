"""DD-2: drawdown_scaler column must never be a hardcoded 1.0 literal.

Origin (ClickUp 86b9z6y4k, parent umbrella 86b9z6qrf, 2026-05-15):
The scanner has 7+ sites that emit a `drawdown_scaler` value into
`evaluated_opportunities` (via candidate-dict + `insert_evaluated_opportunity`
kwarg). Sites that route through `PositionSizer.compute()` propagate the
real scaler. Sites that bypass the sizer (LPNE fixed contracts,
terminal_momentum per-asset cap) historically stubbed `drawdown_scaler=1.0`,
poisoning the column's distribution: with the bot in halt-floor (scaler=0.10),
half the rows show 1.0 (stub) and half show 0.10 (real).

This contract forbids the stub form: every `drawdown_scaler` keyword
arg AND every `"drawdown_scaler"` dict-key value in bot/scanner/__init__.py
must be either a Call (`_drawdown_scaler_readonly(...)`), a Subscript
(`_sizing["drawdown_scaler"]`), a Name (`_ie_drawdown`), or a BoolOp
fallback like `_wknd_drawdown or 1.0` — never a bare `Constant(1.0)` or
`Constant(1)`.

Sister anchors:
  - bot/models.py::PositionSizer._drawdown_scaler_readonly (helper home)
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


def _is_bad_constant_one(node: ast.AST) -> bool:
    """Return True iff the node is a bare numeric Constant equal to 1
    (e.g., 1.0 or 1) — the forbidden stub form."""
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v) == 1.0
    return False


def _find_drawdown_scaler_sites(tree: ast.Module) -> list[tuple[int, str, ast.AST]]:
    """Return [(lineno, kind, value_node), ...] for every drawdown_scaler
    emission point.

    kind ∈ {"kwarg", "dict_key"}:
      - kwarg: `insert_evaluated_opportunity(... drawdown_scaler=X ...)` or
        any other call passing `drawdown_scaler=X`
      - dict_key: `{"drawdown_scaler": X, ...}` literal dict
    """
    sites: list[tuple[int, str, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "drawdown_scaler":
                    sites.append((kw.value.lineno, "kwarg", kw.value))
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant)
                        and key.value == "drawdown_scaler"):
                    sites.append((value.lineno, "dict_key", value))
    return sites


def test_no_hardcoded_drawdown_scaler_one(scanner_ast: ast.Module) -> None:
    """Every drawdown_scaler emission MUST NOT be a bare Constant(1.0).

    Forbidden:
        "drawdown_scaler": 1.0,
        drawdown_scaler=1.0,

    Allowed (any of these forms route through real sizer state):
        "drawdown_scaler": self._sizer._drawdown_scaler_readonly(bal),
        "drawdown_scaler": _x_sizing["drawdown_scaler"],
        "drawdown_scaler": _x_drawdown,
        "drawdown_scaler": _x_drawdown or 1.0,  # BoolOp fallback OK —
            # left operand came from a real PositionSizer.compute() call.
    """
    sites = _find_drawdown_scaler_sites(scanner_ast)
    assert sites, "expected at least one drawdown_scaler emission site in scanner"

    bad: list[tuple[int, str]] = []
    for lineno, kind, value_node in sites:
        if _is_bad_constant_one(value_node):
            bad.append((lineno, kind))

    if bad:
        rendered = "\n".join(
            f"  line {ln}: {kind} drawdown_scaler=1.0 (forbidden stub)"
            for ln, kind in bad
        )
        raise AssertionError(
            "drawdown_scaler stubbed as Constant(1.0) in bot/scanner/__init__.py:\n"
            f"{rendered}\n\n"
            "Route through the real sizer instead — e.g.,\n"
            "  self._sizer._drawdown_scaler_readonly(self._get_balance_cached() or 0)\n"
            "or propagate from a sizing-dict subscript. See ClickUp 86b9z6y4k."
        )


def test_drawdown_scaler_emission_count_floor(scanner_ast: ast.Module) -> None:
    """Floor on total drawdown_scaler emission sites — catches gross deletion.

    Actual count was 36 at the DD-2 ship (2026-05-15): 11 readonly-routed
    paths (LPNE×2 + TM + DC + BRACKET_NO×2 + 1-ct fallback + WEATHER_NO×2
    + HOURLY_NO×2), plus 25 real-sizer paths (v2/ie/wknd/ovn and their
    sister candidate-dict + insert kwarg pairs). A drop to <30 indicates
    a refactor or accidental deletion that warrants re-audit."""
    sites = _find_drawdown_scaler_sites(scanner_ast)
    assert len(sites) >= 30, (
        f"drawdown_scaler emission count dropped to {len(sites)} "
        f"(floor ≥30). A site was deleted without re-audit. "
        f"Sites at lines: {[lineno for lineno, _, _ in sites]}"
    )


def _count_readonly_call_emissions(tree: ast.Module) -> int:
    """Count drawdown_scaler emissions whose value is a Call to
    `_drawdown_scaler_readonly`. These are the 11 newly-repaired
    LPNE/TM/DC/BRACKET_NO/1-ct/WEATHER_NO/HOURLY_NO sites."""
    n = 0
    for lineno, kind, value in _find_drawdown_scaler_sites(tree):
        # Match Call(func=Attribute(attr='_drawdown_scaler_readonly', ...))
        if (isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "_drawdown_scaler_readonly"):
            n += 1
    return n


def test_readonly_call_form_count_pins_eleven_repaired_sites(
    scanner_ast: ast.Module,
) -> None:
    """The 11 stub sites repaired in DD-2 (86b9z6y4k) must all retain the
    `_drawdown_scaler_readonly(...)` call form. Any drop indicates one
    of the repaired sites silently reverted or was deleted.

    Sites repaired: LPNE candidate-dict (2740) + LPNE insert kwarg (2784) +
    TM candidate-dict (3532) + DC candidate-dict (4366) + BRACKET_NO
    candidate-dict (5521) + BRACKET_NO insert kwarg (5558) + 1-ct fallback
    insert kwarg (5614) + WEATHER_NO insert kwarg (7561) + WEATHER_NO
    candidate-dict (7597) + HOURLY_NO insert kwarg (7659) + HOURLY_NO
    candidate-dict (7690). Line numbers are pre-fix anchors; AST count
    is the durable contract."""
    actual = _count_readonly_call_emissions(scanner_ast)
    assert actual >= 11, (
        f"_drawdown_scaler_readonly call-form emission count = {actual} "
        f"(expected ≥11). One of the DD-2 repaired sites reverted to a "
        f"stub or was deleted. Re-audit per the per-site enumeration in "
        f"this test's docstring before adjusting the floor."
    )
