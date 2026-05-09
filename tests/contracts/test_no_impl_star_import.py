"""Pillar 2 of the testing-foundation-sprint (ticket 86b9ve0yt).

AC item: "No module imports ``from bot._impl import *``".

Why this is a separate AST scan (not an import-linter contract):
import-linter sees imports through the grimp graph, which
indistinguishably represents both ``from bot._impl import X`` and
``from bot._impl import *`` as an edge from the source module to
``bot._impl``. The wildcard form is structurally worse — every public
name in ``bot._impl`` becomes a transparent re-export at the import
site, defeating the whole point of the bot._impl/proxy split — but
import-linter's graph-level "forbidden" type can't tell them apart.
A targeted AST guard is the only reliable enforcement.

Sister to ``tests/test_helpers_extraction.py::test_no_circular_bot_
impl_imports_in_helpers`` (which bans bot._impl imports of any kind in
``bot/helpers/``); this one is broader-scope (bans the wildcard form
across the whole repo, including tests/scripts).

If this test fails:
- ``from bot._impl import *`` is opaque dependency. Replace with the
  explicit named imports the call site actually needs, OR — for runtime
  proxy access — use ``import bot`` and read ``bot.X`` (the
  ``_BotProxy`` resolves through to ``bot._impl``).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

SCAN_DIRS = (
    "bot",
    "tests",
    "scripts",
    "ops",
    "analysis",
    "dashboard",
    "research",
)
# Subset that must ALWAYS contain Python files. The other entries in
# SCAN_DIRS are walked best-effort (ops/ is shell-only today,
# dashboard/ may not exist on every branch, analysis/ + research/ are
# sparse). Used by test_scan_walks_required_dirs.
PYTHON_REQUIRED_DIRS = ("bot", "tests", "scripts")
EXCLUDE_DIRS = {
    "__pycache__",
    "venv",
    ".venv",
    "build",
    "dist",
    "node_modules",
    ".smart-env",
    ".pytest_cache",
}


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for top in SCAN_DIRS:
        root = REPO_ROOT / top
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if any(part in EXCLUDE_DIRS for part in path.parts):
                continue
            files.append(path)
    return files


def _has_star_import_from(path: Path, target: str) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        # bot/_impl.py syntax errors fail the syntax-check CI step;
        # surface here too so test failure is loud + diagnostic.
        pytest.fail(f"SyntaxError parsing {path} — fix syntax before this scan can run.")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == target and any(a.name == "*" for a in node.names):
                return True
    return False


def test_no_star_import_from_bot_impl():
    """Repository-wide: no file does ``from bot._impl import *``.

    Walks bot/ tests/ scripts/ ops/ analysis/ dashboard/ research/ and
    parses each .py with stdlib ast. Any wildcard target whose module
    is exactly ``bot._impl`` fails the test.
    """
    offenders: list[str] = []
    for path in _iter_python_files():
        if _has_star_import_from(path, "bot._impl"):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        "The following files use `from bot._impl import *` — replace "
        "with explicit named imports OR access via `import bot` and "
        "the _BotProxy:\n  " + "\n  ".join(offenders)
    )


def test_scan_walks_required_dirs():
    """Defense-in-depth against accidental scope drift.

    Asserts each entry in PYTHON_REQUIRED_DIRS contributes at least
    one .py file to the scan. ops/ (shell-only), analysis/ and
    research/ (sparse), dashboard/ (may not exist) are walked best-
    effort but not asserted — would generate flaky failures otherwise.

    More durable than a magic floor count that drifts as the repo
    grows; fails loudly if EXCLUDE_DIRS or the glob accidentally
    excludes one of the Python-dense trees.
    """
    files = _iter_python_files()
    by_top: dict[str, int] = {top: 0 for top in SCAN_DIRS}
    for path in files:
        rel = path.relative_to(REPO_ROOT)
        top = rel.parts[0]
        if top in by_top:
            by_top[top] += 1
    for top in PYTHON_REQUIRED_DIRS:
        assert by_top[top] > 0, (
            f"_iter_python_files() found 0 files under {top}/ "
            f"despite it being a Python-dense required dir. "
            f"EXCLUDE_DIRS may be over-broad, or the scan glob has "
            f"drifted."
        )


def test_scan_detects_synthetic_wildcard(tmp_path: Path):
    """Negative smoke: the AST detector positively identifies a
    synthetic ``from bot._impl import *`` in a tmp file.

    Without this, a regression in _has_star_import_from (e.g., misnamed
    field, ast API change) would silently false-green.
    """
    p = tmp_path / "synthetic.py"
    p.write_text("from bot._impl import *\n")
    assert _has_star_import_from(p, "bot._impl") is True

    p2 = tmp_path / "synthetic_clean.py"
    p2.write_text("from bot._impl import MainLoop\n")
    assert _has_star_import_from(p2, "bot._impl") is False

    p3 = tmp_path / "synthetic_other.py"
    p3.write_text("from bot.constants import *\n")
    # Different module — must not match a bot._impl scan.
    assert _has_star_import_from(p3, "bot._impl") is False
