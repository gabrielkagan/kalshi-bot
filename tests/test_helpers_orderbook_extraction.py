"""Bit 86b9vpp2z — `_convert_orderbook_fp` + `_best_yes_ask_cents` relocated
from `OpportunityScanner` staticmethods to `bot/helpers/orderbook.py`
module-level functions (2026-05-11).

Eliminates the `_get_opportunity_scanner()` cycle-break helper in
`bot/executor.py` — OrderExecutor now imports the orderbook helpers directly
from `bot.helpers.orderbook` instead of going through the scanner singleton.

Path-B (wrapper preservation): the two `OpportunityScanner` staticmethods
become 1-line delegates so the ~20 test sites that use
`OpportunityScanner._X(...)` staticmethod-via-class form keep working
unchanged. Scanner-internal `self._X(...)` calls also continue to resolve.

5 things must hold post-Bit:
  1. `bot/helpers/orderbook.py` exists with `convert_orderbook_fp` +
     `best_yes_ask_cents` as module-level functions.
  2. The two `OpportunityScanner` staticmethods remain (as delegates).
  3. `bot/executor.py` retires `_get_opportunity_scanner()` (the
     cycle-break helper has no remaining callers).
  4. `bot/executor.py` no longer has any `_get_opportunity_scanner()` or
     `self._ml.scanner._convert_orderbook_fp` / `._best_yes_ask_cents`
     call sites — uses direct `convert_orderbook_fp` / `best_yes_ask_cents`
     bare-name calls via `from bot.helpers.orderbook import ...` or
     `from bot.helpers import *` (helpers star-import laundry).
  5. Behavioral pins: round-trip `convert_orderbook_fp` then
     `best_yes_ask_cents` returns expected values for a known orderbook.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

ORDERBOOK_HELPER_PY = REPO_ROOT / "bot" / "helpers" / "orderbook.py"
SCANNER_PY = REPO_ROOT / "bot" / "scanner" / "__init__.py"
EXECUTOR_PY = REPO_ROOT / "bot" / "executor.py"


def test_helpers_orderbook_module_exists():
    """bot/helpers/orderbook.py exists post-Bit."""
    assert ORDERBOOK_HELPER_PY.exists(), (
        "bot/helpers/orderbook.py missing — Bit 86b9vpp2z extraction not performed"
    )


def test_helpers_orderbook_defines_module_level_functions():
    """Two module-level functions: `convert_orderbook_fp` and `best_yes_ask_cents`."""
    tree = ast.parse(ORDERBOOK_HELPER_PY.read_text())
    funcs = {
        n.name for n in ast.iter_child_nodes(tree) if isinstance(n, ast.FunctionDef)
    }
    assert "convert_orderbook_fp" in funcs, (
        f"bot/helpers/orderbook.py missing `convert_orderbook_fp` module-level "
        f"function. Functions present: {sorted(funcs)}"
    )
    assert "best_yes_ask_cents" in funcs, (
        f"bot/helpers/orderbook.py missing `best_yes_ask_cents` module-level "
        f"function. Functions present: {sorted(funcs)}"
    )


def test_executor_retires_get_opportunity_scanner_helper():
    """Bit 86b9vpp2z eliminates the `_get_opportunity_scanner()` cycle-break
    helper in bot/executor.py — its only callers (_convert_orderbook_fp and
    _best_yes_ask_cents staticmethod accesses) are retired in favor of direct
    bot.helpers.orderbook imports.

    AST-based: docstring/comment mentions of the retired helper are NOT
    false-positive (they're historical context that's expected to remain)."""
    tree = ast.parse(EXECUTOR_PY.read_text())
    # No module-level FunctionDef named `_get_opportunity_scanner`
    for n in ast.iter_child_nodes(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_get_opportunity_scanner":
            pytest.fail(
                f"bot/executor.py still defines `_get_opportunity_scanner()` at "
                f"line {n.lineno}. Retire it; route the 10 call sites to direct "
                f"`convert_orderbook_fp(...)` / `best_yes_ask_cents(...)` imports."
            )
    # No call sites: walk Call nodes looking for Name(_get_opportunity_scanner)
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            fn = n.func
            if isinstance(fn, ast.Name) and fn.id == "_get_opportunity_scanner":
                pytest.fail(
                    f"bot/executor.py still has `_get_opportunity_scanner()` call "
                    f"at line {n.lineno}. Replace with direct bare-name call "
                    f"to convert_orderbook_fp / best_yes_ask_cents."
                )


def test_executor_uses_helpers_orderbook_directly():
    """bot/executor.py imports the new helpers (via top-level `from
    bot.helpers.orderbook import ...` or via the existing `from bot.helpers
    import *` star-laundry)."""
    src = EXECUTOR_PY.read_text()
    # Either form is acceptable.
    direct = "from bot.helpers.orderbook import" in src
    star = "from bot.helpers import *" in src
    # Bare-name call sites must be present (replacing the old _get_opportunity_scanner() form)
    assert direct or star, (
        "bot/executor.py missing a path to convert_orderbook_fp / "
        "best_yes_ask_cents. Add either `from bot.helpers.orderbook import "
        "convert_orderbook_fp, best_yes_ask_cents` or rely on `from "
        "bot.helpers import *` star-laundry."
    )


def test_scanner_staticmethods_remain_as_delegates():
    """Path-B preservation: OpportunityScanner._convert_orderbook_fp and
    _best_yes_ask_cents staticmethods MUST still exist post-Bit (as 1-line
    delegates calling the new bot.helpers.orderbook functions). This keeps
    the ~20 test sites that use `OpportunityScanner._X(...)` working."""
    tree = ast.parse(SCANNER_PY.read_text())
    scanner_cls = next(
        n for n in ast.iter_child_nodes(tree)
        if isinstance(n, ast.ClassDef) and n.name == "OpportunityScanner"
    )
    method_names = {
        m.name for m in scanner_cls.body if isinstance(m, ast.FunctionDef)
    }
    assert "_convert_orderbook_fp" in method_names, (
        "OpportunityScanner._convert_orderbook_fp removed entirely. Bit 86b9vpp2z "
        "is Path-B (wrapper preservation): keep the staticmethod as a 1-line "
        "delegate to bot.helpers.orderbook.convert_orderbook_fp."
    )
    assert "_best_yes_ask_cents" in method_names, (
        "OpportunityScanner._best_yes_ask_cents removed entirely. Bit 86b9vpp2z "
        "is Path-B (wrapper preservation): keep the staticmethod as a 1-line "
        "delegate to bot.helpers.orderbook.best_yes_ask_cents."
    )


def test_helpers_orderbook_behavioral_smoke():
    """Round-trip smoke: convert_orderbook_fp + best_yes_ask_cents work end-to-end."""
    import bot.helpers.orderbook as obh
    ob_fp = {
        "no_dollars": [["0.1100", "205.00"], ["0.1500", "300.00"]],
        "yes_dollars": [["0.8500", "200.00"]],
    }
    converted = obh.convert_orderbook_fp(ob_fp)
    assert "yes" in converted
    assert "no" in converted
    assert converted["no"] == [[11, 205], [15, 300]]
    # best_yes_ask_cents picks highest NO bid (15) → returns 100 - 15 = 85
    best = obh.best_yes_ask_cents(converted)
    assert best == 85, f"expected best YES ask = 100 - 15 = 85; got {best}"


def test_scanner_delegate_still_works():
    """OpportunityScanner._convert_orderbook_fp + _best_yes_ask_cents
    delegates work end-to-end (regression for the 20 test sites using
    staticmethod-via-class form)."""
    import bot
    import bot.scanner
    Cls = bot.scanner.OpportunityScanner
    ob_fp = {
        "no_dollars": [["0.2000", "100.00"]],
        "yes_dollars": [["0.8000", "100.00"]],
    }
    converted = Cls._convert_orderbook_fp(ob_fp)
    assert converted["no"] == [[20, 100]]
    best = Cls._best_yes_ask_cents(converted)
    assert best == 80  # 100 - 20
