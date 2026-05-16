"""Sprint 10.1b — `spx_engine.py` → `bot/engines/spx_engine.py` (2026-05-11).

Second sub-move of Sprint 10.1 engines/ sibling-reorg. ~1162 LOC, mostly
class body (SPXEngine + helpers). Runs as a separate thread/process within
MainLoop (per CLAUDE.md "Engines run as separate threads/processes").

KEEPING THE FILENAME `spx_engine.py` (not renaming to `spx.py`) because:
  1. Operator runbook + skills + KB extensively reference `spx_engine.py`
  2. Audit/research scripts (sports_alpha_research-style) use it in git
     pathspecs; keeping the filename minimizes pathspec churn
  3. Master plan L2229 says `engines/ (spx, weather, sports, sports_data)`
     but the body of each file is the engine module name (mirrors what we
     did for sports_data — kept `sports_data.py` filename, just relocated).

Per `feedback_modularization_skip_soak.md`: no shim re-export, no soak.

Real caller imports (3 sites):
  - bot/main_loop.py:350 (method-body `from spx_engine import SPXEngine`)
  - tests/integration/test_spx_price_feed.py:11 (`import spx_engine`)
  - tests/integration/test_spx_price_feed.py:12 (`from spx_engine import (...)`)

Path-literal refs (>= 9 sites):
  - scripts/ops/pre_deploy_check.sh:18 (`for f in ... spx_engine.py ...`)
  - README.template.md:222 (file-tree)
  - tests/integration/test_insert_schema_parity.py:110 (parametrize list — opens file)
  - tests/integration/test_product_type_enum.py:51 (parametrize list — opens file)
  - tests/contracts/test_db_signatures.py:40,310 (parametrize lists — opens file × 2)
  - tests/contracts/test_call_sites.py:32 (parametrize list — opens file)
  - tests/integration/test_regression.py:561 (parametrize list — opens file)
  - .github/workflows/whitepaper.yml (path trigger — verify)
  - docs/testing-strategy.md:50,57 (prose; cosmetic, low priority)

Sister Sprint 10.1 sub-moves still pending after this Bit:
  - 10.1c: weather_engine.py → bot/engines/weather_engine.py
  - 10.1d: sports_engine.py → bot/engines/sports_engine.py (filename preserved; shipped 2026-05-11)
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

OLD_PATH = REPO_ROOT / "spx_engine.py"
NEW_PATH = REPO_ROOT / "bot" / "engines" / "spx_engine.py"


# ═════════════════════════════════════════════════════════════════════════════
# Section 1 — Identity + behavioral (7 pins)
# ═════════════════════════════════════════════════════════════════════════════

def test_old_spx_engine_path_is_gone():
    """Root `spx_engine.py` DELETED post-move (no shim, per skip-soak feedback)."""
    assert not OLD_PATH.exists(), (
        f"{OLD_PATH} still exists. Sprint 10.1b per skip-soak feedback says "
        f"no shim re-export; file should be `git mv`'d cleanly."
    )


def test_new_spx_engine_path_exists():
    """`bot/engines/spx_engine.py` exists."""
    assert NEW_PATH.exists(), f"{NEW_PATH} missing — Sprint 10.1b move not performed."


def test_no_stale_from_spx_engine_imports():
    """No `from spx_engine import ...` AST nodes outside worktrees."""
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
        if " " in path.stem:  # L93 iCloud filter
            continue
        try:
            tree = ast.parse(path.read_text())
        except (UnicodeDecodeError, OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "spx_engine":
                stale.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not stale, (
        f"Stale `from spx_engine import ...` sites: {stale}. "
        f"Retarget each to `from bot.engines.spx_engine import ...`."
    )


def test_no_stale_import_spx_engine():
    """No bare `import spx_engine` AST nodes outside worktrees."""
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
                    if alias.name == "spx_engine":
                        stale.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not stale, (
        f"Stale `import spx_engine` sites: {stale}. Retarget to "
        f"`import bot.engines.spx_engine` or `from bot.engines.spx_engine import ...`."
    )


def test_spx_engine_class_importable_at_new_path():
    """Behavioral smoke: `from bot.engines.spx_engine import SPXEngine` works."""
    from bot.engines.spx_engine import SPXEngine
    assert SPXEngine is not None


def test_spx_engine_main_loop_uses_new_path():
    """bot/main_loop.py method-body import targets the new path."""
    src = (REPO_ROOT / "bot" / "main_loop.py").read_text()
    assert "from bot.engines.spx_engine import SPXEngine" in src, (
        "bot/main_loop.py method-body late-binding for SPXEngine must use "
        "`from bot.engines.spx_engine import SPXEngine` (Sprint 10.1b retarget)."
    )
    assert "from spx_engine import SPXEngine" not in src, (
        "bot/main_loop.py still has old-form `from spx_engine import SPXEngine`. "
        "Retarget to bot.engines.spx_engine."
    )


def test_bot_engines_does_not_reexport_spx_engine():
    """bot/engines/__init__.py focused on the 3 small engine classes only —
    callers reach spx_engine via direct submodule import (mirrors sports_data
    pattern from Sprint 10.1a)."""
    src = (REPO_ROOT / "bot" / "engines" / "__init__.py").read_text()
    assert "from spx_engine import" not in src, (
        "bot/engines/__init__.py uses old-form `from spx_engine import` — "
        "callers should use `from bot.engines.spx_engine import SPXEngine` direct."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 2 — String-literal path-reference sweep
# (Reused pattern from Sprint 10.1a)
# ═════════════════════════════════════════════════════════════════════════════

_PATH_LITERAL_ALLOW = {
    "tests/integration/test_sprint_10_1b_spx_engine_move.py",      # this file
    "tests/integration/test_sprint_10_1a_sports_data_move.py",     # mentions 10.1b roadmap
    "tests/CLAUDE.md",
    "kb/decisions",
    "kb/concepts",
    "kb/failures",
    "kb-research",
    ".claude/worktrees",
    ".claude/skills",                                  # operator skill docs (low risk; manual sweep)
    ".git",
    "venv",
}


def _sweep_string_literal_refs(needle: str, suffixes: tuple = (".py", ".yml", ".yaml", ".md", ".sh")) -> list[str]:
    """Walk the repo for files containing `needle` as a literal substring.

    Returns relative paths with line numbers where `needle` appears AND the
    path is NOT in the allow-list. Used to detect file-relocation drift in
    CI workflows, scripts, parametrize lists, file-tree READMEs that AST
    walks can't see."""
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


def test_no_stale_string_literal_spx_engine_in_scripts_or_workflows():
    """Path-literal references to bare `spx_engine.py` (in `os.path.join(...)`,
    quoted parametrize entries, CI path triggers, file-trees) break silently
    post-move. Allows lines that ALSO mention the new path, comment lines,
    docstring lines, multi-component `os.path.join("bot","engines","spx_engine.py")`
    form, and auto-regenerated README.md."""
    needle = "spx_engine.py"
    correct = "bot/engines/spx_engine.py"
    hits = _sweep_string_literal_refs(needle)
    stale = []
    for h in hits:
        rel, rest = h.split(": ", 1)
        if correct in rest:
            continue
        if '"bot", "engines"' in rest or "'bot', 'engines'" in rest:
            continue
        stripped = rest.strip()
        if stripped.startswith("#"):
            continue
        if '"""' in stripped or "'''" in stripped:
            continue
        if rel == "README.md":
            continue
        # Allow lines without quoted form (pure prose)
        if f'"{needle}"' not in stripped and f"'{needle}'" not in stripped:
            continue
        stale.append(h)
    assert not stale, (
        f"Stale string-literal references to bare `spx_engine.py`. Each must "
        f"be updated to `bot/engines/spx_engine.py` (or explicitly include "
        f"both pre+post paths for git log/diff pathspecs). Found {len(stale)} "
        f"sites:\n" + "\n".join(stale[:20])
    )
