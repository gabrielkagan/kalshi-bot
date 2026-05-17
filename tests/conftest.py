"""Top-level tests/ conftest — shared session-scoped fixtures.

Currently houses ``repo_ast_cache`` (Bit-4.5 CI perf, 2026-05-17,
ticket ``86b9zjw8a``). The cache parses every .py file in the
canonical 4-glob exactly once per pytest session and shares the
parsed ``ast.Module`` trees across all AST-walk-the-repo modularization
audits (Sprint 10.1a/b/c/d + 10.2 + 10.5a/b + 10.6 + Bit 10.4 + 12.1 +
9.3-iii.c).

Pre-cache, the 43 in-scope audits each independently ``ast.parse`` the
same ~420 .py files — ~18,000 redundant parse calls per integration
run (~58s on Mac, ~115s on CI). Post-cache, parsing happens once at
session start; each audit's per-test cost drops to ``ast.walk`` +
predicate-check on cached trees (~0.2s/test).

Fixture contract pinned by
``tests/contracts/test_ast_cache_fixture.py``:

  - Returns ``dict[Path, ast.Module | None]``.
  - Keys are exactly the canonical 4-glob: ``REPO_ROOT/*.py`` +
    ``REPO_ROOT/bot/**/*.py`` + ``REPO_ROOT/tests/**/*.py`` +
    ``REPO_ROOT/scripts/**/*.py``, excluding any path under
    ``.claude/worktrees/`` (sister-session worktrees).
  - Values are the parsed ``ast.Module`` for parseable files; ``None``
    for files that raised ``UnicodeDecodeError | OSError |
    SyntaxError``. Audits MUST skip ``None`` entries.
  - Session-scoped: built once per pytest invocation.

Out-of-scope audits (walk broader than 4-glob — ``REPO_ROOT.rglob`` or
7-dir scan): ``tests/contracts/test_no_impl_star_import.py`` and
``tests/contracts/test_bit_10_3_ai_subpackage.py::test_no_python_import_from_repo_root_names``.
Filed as ClickUp ticket ``86b9zk0ww`` (Bit-4.5-fu — extend cache to
broader-scope audits) for a separate Bit. Refactoring these to the
4-glob cache would silently narrow their scan and regress detection
coverage; widening the cache OR adding a secondary fixture is the
right approach.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def repo_ast_cache() -> dict[Path, ast.Module | None]:
    """Parse every .py in the canonical 4-glob once per session.

    See module docstring for the contract; see
    ``tests/contracts/test_ast_cache_fixture.py`` for the pinned
    invariants.
    """
    cache: dict[Path, ast.Module | None] = {}
    paths = (
        list(REPO_ROOT.glob("*.py"))
        + list((REPO_ROOT / "bot").rglob("*.py"))
        + list((REPO_ROOT / "tests").rglob("*.py"))
        + list((REPO_ROOT / "scripts").rglob("*.py"))
    )
    for path in paths:
        if ".claude/worktrees/" in str(path):
            continue
        try:
            cache[path] = ast.parse(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, OSError, SyntaxError):
            cache[path] = None
    return cache
