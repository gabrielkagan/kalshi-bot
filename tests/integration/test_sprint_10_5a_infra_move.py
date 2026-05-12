"""Sprint 10.5a — capital_allocator.py + circuit_breaker.py → bot/infra/ (2026-05-11).

Per master plan L2225-2238 Sprint 10 row "infra/" — partial bundle:

  - capital_allocator.py → bot/infra/capital_allocator.py (326 LOC)
  - circuit_breaker.py   → bot/infra/circuit_breaker.py   (412 LOC)

Filenames preserved (mirrors Sprint 10.1 b/c/d + 10.2 precedent).
Per `feedback_modularization_skip_soak.md`: no shim, no soak.

Pre-flight R1-lesson coverage (Sprint 10.1b/c/d):
  - 0 mock.patch string-form `patch("X.foo")` sites for either module
  - 0 `__file__`-derived path computations in either module
  - 0 `from bot import (X, Y, Z)` proxy chain imports in either module
  - Neither is a CLI script (no `if __name__` guard with operational
    invocation — circuit_breaker has REGISTRY singleton but no main)

Real caller imports:
  capital_allocator:
    - bot/main_loop.py:464 (method-body `from capital_allocator import CapitalAllocator`)
  circuit_breaker:
    - bot/_impl.py:52 (top-level `from circuit_breaker import REGISTRY as _BREAKER_REGISTRY`)
    - 30+ tests/integration/test_circuit_breaker.py method-body imports
    - bot/scanner/__init__.py + others (search-time discovery)

Deferred from Sprint 10.5 (filed as separate Bits):
  - 10.5b: models.py (8 prod + 6 test import sites — larger surface)
  - 10.5c: watchdog.py (__file__-derived paths + CLI invocation, same
    risk class as Sprint 10.3 ai/ bot/ai/auditor.py + bot/ai/researcher.py)
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


MODULES = (
    "capital_allocator",
    "circuit_breaker",
)

MODULE_OLD_PATHS = {m: REPO_ROOT / f"{m}.py" for m in MODULES}
MODULE_NEW_PATHS = {m: REPO_ROOT / "bot" / "infra" / f"{m}.py" for m in MODULES}

# Smoke-test entry-point names
MODULE_SMOKE_NAMES = {
    "capital_allocator": "CapitalAllocator",
    "circuit_breaker": "REGISTRY",
}


# ═════════════════════════════════════════════════════════════════════════════
# Section 1 — Identity + behavioral (parametrized × 2 modules)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("mod", MODULES)
def test_old_module_path_is_gone(mod):
    """Root `<mod>.py` DELETED post-move (no shim)."""
    old = MODULE_OLD_PATHS[mod]
    assert not old.exists(), (
        f"{old} still exists. Sprint 10.5a per skip-soak feedback says no shim."
    )


@pytest.mark.parametrize("mod", MODULES)
def test_new_module_path_exists(mod):
    """`bot/infra/<mod>.py` exists."""
    new = MODULE_NEW_PATHS[mod]
    assert new.exists(), f"{new} missing — Sprint 10.5a move not performed."


@pytest.mark.parametrize("mod", MODULES)
def test_no_stale_from_module_imports(mod):
    """No `from <mod> import ...` AST nodes outside worktrees."""
    repo_files = (
        list(REPO_ROOT.glob("*.py"))
        + list((REPO_ROOT / "bot").rglob("*.py"))
        + list((REPO_ROOT / "tests").rglob("*.py"))
        + list((REPO_ROOT / "scripts").rglob("*.py"))
    )
    stale: list[str] = []
    new_path = MODULE_NEW_PATHS[mod]
    for path in repo_files:
        if ".claude/worktrees/" in str(path):
            continue
        if path == new_path:
            continue
        if " " in path.stem:
            continue
        try:
            tree = ast.parse(path.read_text())
        except (UnicodeDecodeError, OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == mod:
                stale.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not stale, (
        f"Stale `from {mod} import ...` sites: {stale}. "
        f"Retarget each to `from bot.infra.{mod} import ...`."
    )


@pytest.mark.parametrize("mod", MODULES)
def test_no_stale_import_module(mod):
    """No bare `import <mod>` AST nodes outside worktrees."""
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
                    if alias.name == mod:
                        stale.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not stale, (
        f"Stale `import {mod}` sites: {stale}. "
        f"Retarget to `import bot.infra.{mod}` or `from bot.infra.{mod} import ...`."
    )


@pytest.mark.parametrize("mod", MODULES)
def test_module_smoke_importable_at_new_path(mod):
    """Behavioral smoke: key entry point importable from new path."""
    name = MODULE_SMOKE_NAMES[mod]
    pkg = __import__(f"bot.infra.{mod}", fromlist=[name])
    assert getattr(pkg, name, None) is not None, (
        f"bot.infra.{mod}.{name} missing"
    )


def test_main_loop_capital_allocator_uses_new_path():
    """bot/main_loop.py method-body import targets new path."""
    src = (REPO_ROOT / "bot" / "main_loop.py").read_text()
    assert "from bot.infra.capital_allocator import CapitalAllocator" in src, (
        "bot/main_loop.py must use `from bot.infra.capital_allocator import CapitalAllocator`."
    )
    assert "from capital_allocator import CapitalAllocator" not in src, (
        "bot/main_loop.py still has old-form `from capital_allocator import CapitalAllocator`."
    )


def test_bot_impl_circuit_breaker_uses_new_path():
    """bot/_impl.py top-level import targets new path."""
    if not (REPO_ROOT / "bot" / "_impl.py").exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — extraction-pin vacuous")
    src = (REPO_ROOT / "bot" / "_impl.py").read_text()
    assert "from bot.infra.circuit_breaker import REGISTRY" in src, (
        "bot/_impl.py must use `from bot.infra.circuit_breaker import REGISTRY`."
    )
    assert "from circuit_breaker import REGISTRY" not in src, (
        "bot/_impl.py still has old-form `from circuit_breaker import REGISTRY`."
    )


def test_bot_infra_init_exists():
    """`bot/infra/__init__.py` exists with a docstring."""
    init_path = REPO_ROOT / "bot" / "infra" / "__init__.py"
    assert init_path.exists(), "bot/infra/__init__.py missing"


# ═════════════════════════════════════════════════════════════════════════════
# Section 2 — String-literal path-reference sweep (parametrized)
# ═════════════════════════════════════════════════════════════════════════════

_PATH_LITERAL_ALLOW = {
    "tests/integration/test_sprint_10_5a_infra_move.py",
    "tests/integration/test_sprint_10_2_shadows_move.py",
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


@pytest.mark.parametrize("mod", MODULES)
def test_no_stale_string_literal_module_in_scripts_or_workflows(mod):
    """Path-literal references to bare `<mod>.py`. Allows lines that also
    mention the new path, comments, docstrings, multi-component
    `os.path.join("bot","infra","<mod>.py")` + Path-segment forms, README.md."""
    needle = f"{mod}.py"
    correct = f"bot/infra/{mod}.py"
    hits = _sweep_string_literal_refs(needle)
    stale = []
    for h in hits:
        rel, rest = h.split(": ", 1)
        if correct in rest:
            continue
        if '"bot", "infra"' in rest or "'bot', 'infra'" in rest:
            continue
        if '"bot" / "infra"' in rest or "'bot' / 'infra'" in rest:
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
        f"Stale string-literal references to bare `{mod}.py`. Found {len(stale)} sites:\n"
        + "\n".join(stale[:20])
    )


@pytest.mark.parametrize("mod", MODULES)
def test_no_mock_patch_string_form_for_module(mod):
    """R1-lesson regression pin (Sprint 10.1b): AST-walk for `patch("<mod>.X")`."""
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
                        if first.value == mod or first.value.startswith(f"{mod}."):
                            stale.append(
                                f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                                f'patch("{first.value}")'
                            )
    assert not stale, (
        f"mock.patch string-form targets for {mod} module: {stale}. "
        f"Retarget each to `patch('bot.infra.{mod}.X', ...)`."
    )
