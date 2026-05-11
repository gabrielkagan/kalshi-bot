"""Sprint 10.5b — models.py → bot/models.py (2026-05-11).

Largest single-file safe-to-move in Sprint 10.5 (1274 LOC). Pure-math
classes + fee helpers used by ~13 production import sites + ~8 test
modules. No __file__, no CLI, no mock.patch sites. Per
`feedback_modularization_skip_soak.md`: no shim, no soak.

Filename preserved per Sprint 10.1 b/c/d + 10.2 + 10.5a precedent.

Pre-flight R1-lesson coverage (10.1b/c/d/10.5a):
  - 0 mock.patch string-form sites
  - 0 __file__-derived path computations
  - 0 `from bot import` proxy chains
  - **CRITICAL**: bot/helpers/tm_sweep.py:5 has top-level
    `from models import calculate_taker_fee` — post-move this becomes
    `from bot.models import ...` which violates the helpers-leaf
    .importlinter contract (helpers can't import sibling subpackages).
    Same fix pattern as Sprint 10.5a: LAZY method-body import inside
    tm_sweep_counterfactual_pnl + .importlinter carve-out.

Real caller imports (~13 prod + ~8 test):
  bot/*:
    - bot/_impl.py:82 (top-level, large multi-name)
    - bot/main_loop.py:191 (top-level, multi-name)
    - bot/settlement.py:100 (top-level)
    - bot/state.py:129 (top-level)
    - bot/executor.py:97 (top-level) + 2543 (method-body)
    - bot/engines/volatility.py:104 (top-level)
    - bot/engines/calibration.py:125 (top-level)
    - **bot/helpers/tm_sweep.py:5 (top-level — REFACTOR TO LAZY)**
    - bot/scanner/__init__.py:75 + 3327 + 4270
  tests/:
    - test_scan_pipeline, test_execution, test_hwm_isolation,
      test_fee_calc, test_egarch (regression), test_stacking (×8),
      test_regression (×7)
  Plus regex pin in test_scanner_extraction.py:1058 (asserts
    `^from models import.*\\bPositionSizer\\b` exists in scanner — needs
    update to `^from bot.models import.*\\bPositionSizer\\b`).

Notable: bot/__main__.py:10 mentions `from models import ...` in a
docstring (NOT an import) — descriptive narrative. Update for consistency.

Deferred (Sprint 10.5c — separate Bit): watchdog.py (same risk class
as 10.3 ai/, __file__ + CLI).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest
import bot.helpers  # noqa: F401
import bot.helpers.breakers  # noqa: F401
import bot.helpers.tm_sweep  # noqa: F401
import bot.infra  # noqa: F401


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

OLD_PATH = REPO_ROOT / "models.py"
NEW_PATH = REPO_ROOT / "bot" / "models.py"


# Smoke-test entry points
SMOKE_NAMES = (
    "EGARCHEstimator", "MincerZarnowitzTracker", "PositionSizer",
    "calculate_fee", "calculate_taker_fee", "calculate_maker_fee",
    "compute_tv_rk_weights", "strategy_to_group",
)


# ═════════════════════════════════════════════════════════════════════════════
# Section 1 — Identity + behavioral
# ═════════════════════════════════════════════════════════════════════════════

def test_old_models_path_is_gone():
    """Root `models.py` DELETED post-move."""
    assert not OLD_PATH.exists(), (
        f"{OLD_PATH} still exists. Sprint 10.5b per skip-soak: no shim."
    )


def test_new_models_path_exists():
    """`bot/models.py` exists."""
    assert NEW_PATH.exists(), f"{NEW_PATH} missing — Sprint 10.5b move not performed."


def test_no_stale_from_models_imports():
    """No `from models import ...` AST nodes outside worktrees."""
    repo_files = (
        list(REPO_ROOT.glob("*.py"))
        + list((REPO_ROOT / "bot").rglob("*.py"))
        + list((REPO_ROOT / "tests").rglob("*.py"))
        + list((REPO_ROOT / "scripts").rglob("*.py"))
    )
    stale: list[str] = []
    for path in repo_files:
        if ".claude/worktrees/" in str(path):
            continue
        if path == NEW_PATH:
            continue
        if " " in path.stem:
            continue
        try:
            tree = ast.parse(path.read_text())
        except (UnicodeDecodeError, OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "models":
                stale.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not stale, (
        f"Stale `from models import ...` sites: {stale}. "
        f"Retarget each to `from bot.models import ...`."
    )


def test_no_stale_import_models():
    """No bare `import models` AST nodes outside worktrees."""
    repo_files = (
        list(REPO_ROOT.glob("*.py"))
        + list((REPO_ROOT / "bot").rglob("*.py"))
        + list((REPO_ROOT / "tests").rglob("*.py"))
        + list((REPO_ROOT / "scripts").rglob("*.py"))
    )
    stale: list[str] = []
    for path in repo_files:
        if ".claude/worktrees/" in str(path):
            continue
        if " " in path.stem:
            continue
        try:
            tree = ast.parse(path.read_text())
        except (UnicodeDecodeError, OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "models":
                        stale.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not stale, (
        f"Stale `import models` sites: {stale}. "
        f"Retarget to `import bot.models` or `from bot.models import ...`."
    )


@pytest.mark.parametrize("name", SMOKE_NAMES)
def test_smoke_importable_at_new_path(name):
    """Behavioral smoke: each entry point importable from new path."""
    mod = __import__("bot.models", fromlist=[name])
    assert getattr(mod, name, None) is not None, f"bot.models.{name} missing"


# ═════════════════════════════════════════════════════════════════════════════
# Section 2 — Helpers-leaf carve-out (10.5a R1 pattern)
# ═════════════════════════════════════════════════════════════════════════════

def test_helpers_tm_sweep_uses_lazy_models_import():
    """R1-lesson (Sprint 10.5a CRITICAL): bot/helpers/tm_sweep.py:5 had
    top-level `from models import calculate_taker_fee`. Pre-move this was
    OK (models was at repo root, NOT a bot.X sibling). Post-move it
    becomes `from bot.models import ...` which violates the helpers-leaf
    .importlinter contract.

    Fix: lazy import inside the function body that uses calculate_taker_fee
    (same pattern as bot/helpers/breakers.py post-10.5a)."""
    src = (REPO_ROOT / "bot" / "helpers" / "tm_sweep.py").read_text()
    tree = ast.parse(src)
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "models":
                pytest.fail(
                    f"bot/helpers/tm_sweep.py:{node.lineno} has stale top-level "
                    f"`from models import` — should be lazy + retargeted to bot.models."
                )
            if node.module == "bot.models":
                pytest.fail(
                    f"bot/helpers/tm_sweep.py:{node.lineno} has top-level "
                    f"`from bot.models import` — violates helpers-leaf contract. "
                    f"Use lazy method-body import (mirrors Sprint 10.5a breakers.py)."
                )


def test_importlinter_helpers_tm_sweep_carve_out():
    """The lazy refactor alone isn't enough — import-linter traces method-body
    imports too. Verify `.importlinter` has a carve-out for
    `bot.helpers.tm_sweep -> bot.models` (mirrors Sprint 10.5a breakers.py)."""
    src = (REPO_ROOT / ".importlinter").read_text()
    assert "bot.helpers.tm_sweep -> bot.models" in src, (
        ".importlinter missing `ignore_imports` for "
        "`bot.helpers.tm_sweep -> bot.models`. Required because the lazy "
        "import inside the function body is still traced by grimp. Same "
        "pattern as Sprint 10.5a `bot.helpers.breakers -> bot.infra.circuit_breaker`."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 3 — String-literal + mock.patch + scanner regex pin
# ═════════════════════════════════════════════════════════════════════════════

def test_scanner_extraction_regex_pin_uses_new_path():
    """tests/test_scanner_extraction.py:1058 has a regex pin checking the
    scanner source contains `^from models import.*\\bPositionSizer\\b`.
    Post-move this should be `^from bot.models import.*\\bPositionSizer\\b`.

    The test file uses raw-string regex `r"^from bot\\.models import.*\\b"`
    where `\\.` is an escaped dot. Search for that exact regex literal."""
    src = (REPO_ROOT / "tests" / "test_scanner_extraction.py").read_text()
    assert r"from bot\.models import" in src, (
        "tests/test_scanner_extraction.py: the regex pin checking the "
        "scanner imports models from the new path is missing. The pre-move "
        "pin (`^from models import`) is stale post-Sprint-10.5b. Expected "
        r"the raw-string regex `from bot\.models import` (with escaped dot)."
    )


def test_no_mock_patch_string_form_for_models():
    """R1-lesson (Sprint 10.1b): AST-walk for `patch("models.X")` string-form
    targets. Pre-flight returned 0 sites (the matches in cal_mlp tests are
    string PATH literals like `models/cal_mlp_<ASSET>/abc`, not import targets)."""
    test_dir = REPO_ROOT / "tests"
    stale: list[str] = []
    for path in test_dir.rglob("*.py"):
        if " " in path.stem:
            continue
        try:
            tree = ast.parse(path.read_text())
        except (UnicodeDecodeError, OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                fn_name = (
                    fn.attr if isinstance(fn, ast.Attribute) else
                    fn.id if isinstance(fn, ast.Name) else None
                )
                if fn_name == "patch" and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        # Filter false-positives: `"models/cal_mlp_<ASSET>/abc"`
                        # is a string PATH, not an import target. Match only
                        # `models` or `models.X` where X is an identifier
                        # (no slashes / spaces).
                        v = first.value
                        if v == "models" or (
                            v.startswith("models.")
                            and "/" not in v
                            and " " not in v
                        ):
                            stale.append(
                                f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                                f'patch("{v}")'
                            )
    assert not stale, (
        f"mock.patch string-form targets for models module: {stale}. "
        f"Retarget each to `patch('bot.models.X', ...)`."
    )
