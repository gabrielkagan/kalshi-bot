"""Sprint 10.1d — `sports_engine.py` → `bot/engines/sports_engine.py` (2026-05-11).

FINAL sub-move of Sprint 10.1 engines/ sibling-reorg. Largest of the four
(~2280 LOC). Filename preserved (mirrors 10.1b/c precedent).

Per `feedback_modularization_skip_soak.md`: no shim re-export at old path,
no soak. All caller imports + path-literal refs retargeted atomically.

Pre-flight R1-lesson checks (10.1b + 10.1c):
  - mock.patch string-form `patch("sports_engine.X")` sites: 0 detected
  - `__file__`-derived path computations in sports_engine.py: 0 detected
    (no cache file like weather; sports_data already at bot/engines/)

Real caller imports (6 sites):
  - bot/main_loop.py:451 (method-body `from sports_engine import SportsEngine`)
  - tests/integration/test_extended_features.py:341 (`from sports_engine import GameState, ComebackSignal`)
  - tests/integration/test_sports_ask_depth_int.py:39,74 (`import sports_engine` × 2)
  - tests/integration/test_regression.py:2486,2499,2506 (`from sports_engine import _parse_orderbook` × 3)

Internal sports_engine.py import (already correct):
  - line 29: `from bot.engines.sports_data import (...)` — set by Sprint 10.1a

Path-literal refs (~65 across):
  - scripts/ops/pre_deploy_check.sh (bash for-loop)
  - scripts/audit/doc_drift_check.py (SOURCE_FILES + count_sports_leagues fname iteration)
  - scripts/audit/sports_shadow_audit.py (git pathspec — extend BOTH paths like 10.1a sports_data)
  - scripts/audit/sports_alpha_research.py (git pathspec — same)
  - .github/workflows/whitepaper.yml (`paths:` trigger — add new path)
  - README.template.md + README.md (file-tree literal)
  - agent_docs/bot_layout.md (project file-map)
  - bot/CLAUDE.md (engines paragraph)
  - docs/testing-strategy.md (2 sites)
  - supabase_sync.py:560 (comment line-number citation)
  - tests/contracts/test_call_sites.py (ENGINE_FILES + ALLOWED_RAW_INSERTERS + file-open)
  - tests/contracts/test_db_signatures.py (2 sites)
  - tests/integration/test_db_writer_registry.py (parametrize)
  - tests/integration/test_insert_schema_parity.py (9 sites incl. ALLOWED_RAW_INSERTERS + file-open at L272)
  - tests/integration/test_product_type_enum.py (parametrize)
  - tests/integration/test_regression.py (multiple incl. CRITICAL_FILES)
  - tests/integration/test_sports_ask_depth_int.py (alias + file-open at L91)
  - tests/integration/test_orderbook_logging_schema.py (prose)
  - tests/integration/test_15m_silence_alert.py (prose, cosmetic)
  - tests/integration/test_sprint_10_1b_spx_engine_move.py + tests/integration/test_sprint_10_1c_weather_engine_move.py
    (roadmap docstring lines 35 + 39 — cosmetic but pin)
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

OLD_PATH = REPO_ROOT / "sports_engine.py"
NEW_PATH = REPO_ROOT / "bot" / "engines" / "sports_engine.py"


# ═════════════════════════════════════════════════════════════════════════════
# Section 1 — Identity + behavioral (7 pins)
# ═════════════════════════════════════════════════════════════════════════════

def test_old_sports_engine_path_is_gone():
    """Root `sports_engine.py` DELETED post-move (no shim, per skip-soak)."""
    assert not OLD_PATH.exists(), (
        f"{OLD_PATH} still exists. Sprint 10.1d per skip-soak feedback says "
        f"no shim re-export; file should be `git mv`'d cleanly."
    )


def test_new_sports_engine_path_exists():
    """`bot/engines/sports_engine.py` exists."""
    assert NEW_PATH.exists(), f"{NEW_PATH} missing — Sprint 10.1d move not performed."


def test_no_stale_from_sports_engine_imports():
    """No `from sports_engine import ...` AST nodes outside worktrees."""
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
            if isinstance(node, ast.ImportFrom) and node.module == "sports_engine":
                stale.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not stale, (
        f"Stale `from sports_engine import ...` sites: {stale}. "
        f"Retarget each to `from bot.engines.sports_engine import ...`."
    )


def test_no_stale_import_sports_engine():
    """No bare `import sports_engine` AST nodes outside worktrees."""
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
                    if alias.name == "sports_engine":
                        stale.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not stale, (
        f"Stale `import sports_engine` sites: {stale}. Retarget to "
        f"`import bot.engines.sports_engine` or `from bot.engines.sports_engine import ...`."
    )


def test_sports_engine_class_importable_at_new_path():
    """Behavioral smoke: `from bot.engines.sports_engine import SportsEngine` works."""
    from bot.engines.sports_engine import SportsEngine
    assert SportsEngine is not None


def test_sports_engine_main_loop_uses_new_path():
    """bot/main_loop.py method-body import targets the new path."""
    src = (REPO_ROOT / "bot" / "main_loop.py").read_text()
    assert "from bot.engines.sports_engine import SportsEngine" in src, (
        "bot/main_loop.py must use `from bot.engines.sports_engine import SportsEngine`."
    )
    assert "from sports_engine import SportsEngine" not in src, (
        "bot/main_loop.py still has old-form `from sports_engine import SportsEngine`."
    )


def test_bot_engines_does_not_reexport_sports_engine():
    """bot/engines/__init__.py focused on the 3 small engine classes only —
    callers reach sports_engine via direct submodule import."""
    src = (REPO_ROOT / "bot" / "engines" / "__init__.py").read_text()
    assert "from sports_engine import" not in src, (
        "bot/engines/__init__.py uses old-form `from sports_engine import`."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 2 — String-literal path-reference sweep
# ═════════════════════════════════════════════════════════════════════════════

_PATH_LITERAL_ALLOW = {
    "tests/integration/test_sprint_10_1d_sports_engine_move.py",
    "tests/integration/test_sprint_10_1c_weather_engine_move.py",
    "tests/integration/test_sprint_10_1b_spx_engine_move.py",
    "tests/integration/test_sprint_10_1a_sports_data_move.py",
    "tests/CLAUDE.md",
    "kb/decisions",
    "kb/concepts",
    "kb/failures",
    "kb-research",
    ".claude/worktrees",
    ".claude/skills",
    ".git",
    "venv",
}


def _sweep_string_literal_refs(needle: str, suffixes: tuple = (".py", ".yml", ".yaml", ".md", ".sh")) -> list[str]:
    hits: list[str] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in suffixes:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        if any(rel.startswith(allow) for allow in _PATH_LITERAL_ALLOW):
            continue
        if " " in path.stem:
            continue
        try:
            src = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if needle in src:
            for lineno, line in enumerate(src.splitlines(), 1):
                if needle in line:
                    hits.append(f"{rel}:{lineno}: {line.strip()[:120]}")
    return hits


def test_no_stale_string_literal_sports_engine_in_scripts_or_workflows():
    """Path-literal references to bare `sports_engine.py`. Allows lines that
    also mention the new path, comment lines, docstring lines, multi-component
    `os.path.join("bot","engines","sports_engine.py")` form, Path-segment form,
    and auto-regen README.md."""
    needle = "sports_engine.py"
    correct = "bot/engines/sports_engine.py"
    hits = _sweep_string_literal_refs(needle)
    stale = []
    for h in hits:
        rel, rest = h.split(": ", 1)
        if correct in rest:
            continue
        if '"bot", "engines"' in rest or "'bot', 'engines'" in rest:
            continue
        if '"bot" / "engines"' in rest or "'bot' / 'engines'" in rest:
            continue
        stripped = rest.strip()
        if stripped.startswith("#"):
            continue
        if '"""' in stripped or "'''" in stripped:
            continue
        if rel == "README.md":
            continue
        if f'"{needle}"' not in stripped and f"'{needle}'" not in stripped:
            continue
        stale.append(h)
    assert not stale, (
        f"Stale string-literal references to bare `sports_engine.py`. Each must "
        f"be updated to `bot/engines/sports_engine.py` (or explicitly include "
        f"both pre+post paths for git pathspecs). Found {len(stale)} sites:\n"
        + "\n".join(stale[:25])
    )


def test_no_mock_patch_string_form_for_sports_engine():
    """Sprint 10.1b R1 CRITICAL prevention: AST-walk for `patch("sports_engine.X")`
    targets. `unittest.mock.patch` resolves dotted-name strings via
    importlib.import_module — post-move these would raise ModuleNotFoundError."""
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
                        if first.value == "sports_engine" or first.value.startswith("sports_engine."):
                            stale.append(
                                f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                                f'patch("{first.value}")'
                            )
    assert not stale, (
        f"mock.patch string-form targets for sports_engine module: {stale}. "
        f"Retarget each to `patch('bot.engines.sports_engine.X', ...)`."
    )


def test_sports_engine_internal_sports_data_import_correct():
    """sports_engine.py imports from bot.engines.sports_data (set by Sprint
    10.1a). Verify this internal import survived the 10.1d move."""
    src = NEW_PATH.read_text()
    assert "from bot.engines.sports_data import" in src, (
        "bot/engines/sports_engine.py must import from `bot.engines.sports_data` "
        "(set by Sprint 10.1a). If reverted to `from sports_data import`, that's "
        "a broken cross-Bit chain."
    )
    assert "from sports_data import" not in src, (
        "bot/engines/sports_engine.py still has old-form `from sports_data import` — "
        "10.1a retarget regressed."
    )
