"""Sprint 10.6 — migrations/ directory (2026-05-11).

Per master plan L2234 + Phase GG: create NEW top-level `migrations/` directory
(NOT `bot/migrations/` — master plan says "NOT on the default import path so
it can't be accidentally triggered"). Two tracked migration scripts moved:

  - migrate_to_supabase.py        → migrations/migrate_to_supabase.py
  - scripts/migrate_composite_pk.py → migrations/migrate_composite_pk.py

Plus NEW migrations/README.md.

Per `feedback_modularization_skip_soak.md`: no shim, no soak.

Risk profile: LOW. Both are operator-run-only (not in any cron/systemd
chain). Verified pre-flight:
  - 0 mock.patch string-form sites
  - migrate_to_supabase.py:226 has __file__-derived state.db path — FIXED
    in-Bit via explicit 2-level parent navigation (migrations/X.py → migrations/
    → repo/) following 10.1c precedent
  - scripts/migrate_composite_pk.py:17 uses os.environ + CWD-relative
    state.db — operator-documented; no fix needed
  - 0 production imports (one-shot scripts; never imported by bot/*)

NOT a top-level Python package — no __init__.py (per master plan: "NOT on
default import path"). README.md documents the operator workflow.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


MIGRATIONS = (
    ("migrate_to_supabase.py", REPO_ROOT / "migrate_to_supabase.py"),
    ("migrate_composite_pk.py", REPO_ROOT / "scripts" / "migrate_composite_pk.py"),
)

NEW_PATHS = {
    "migrate_to_supabase.py": REPO_ROOT / "migrations" / "migrate_to_supabase.py",
    "migrate_composite_pk.py": REPO_ROOT / "migrations" / "migrate_composite_pk.py",
}


# ═════════════════════════════════════════════════════════════════════════════
# Section 1 — Identity (parametrized × 2 migrations)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("name,old_path", MIGRATIONS)
def test_old_migration_path_is_gone(name, old_path):
    """Old path DELETED post-move (no shim)."""
    assert not old_path.exists(), (
        f"{old_path} still exists. Sprint 10.6 per skip-soak feedback: no shim."
    )


@pytest.mark.parametrize("name,_old", MIGRATIONS)
def test_new_migration_path_exists(name, _old):
    """`migrations/<name>` exists."""
    new = NEW_PATHS[name]
    assert new.exists(), f"{new} missing — Sprint 10.6 move not performed."


def test_migrations_readme_exists():
    """`migrations/README.md` exists with operator runbook."""
    readme = REPO_ROOT / "migrations" / "README.md"
    assert readme.exists(), "migrations/README.md missing — operator runbook required"
    content = readme.read_text()
    # Verify both migration scripts are referenced
    assert "migrate_to_supabase.py" in content, "README must reference migrate_to_supabase.py"
    assert "migrate_composite_pk.py" in content, "README must reference migrate_composite_pk.py"


def test_migrations_NOT_python_package():
    """Per master plan: migrations/ is NOT on default import path. Must NOT
    have __init__.py (would make it a package + importable)."""
    init_path = REPO_ROOT / "migrations" / "__init__.py"
    assert not init_path.exists(), (
        f"{init_path} exists — but master plan L2234 specifies migrations/ "
        f"is NOT on default import path ('so it can't be accidentally "
        f"triggered'). Delete __init__.py to make it a plain directory."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 2 — __file__-derived path fix verification (10.1c R1 lesson)
# ═════════════════════════════════════════════════════════════════════════════

def test_migrate_to_supabase_state_db_path_anchored_to_repo_root():
    """R1-lesson: migrate_to_supabase.py used `os.path.dirname(__file__)`
    to compute `state.db` path. Pre-move it resolved to repo root; post-move
    it would silently resolve to `<repo>/migrations/state.db` (doesn't
    exist) → "0 rows migrated" silently.

    Fix: explicit 2-level parent navigation
    (migrations/migrate_to_supabase.py → migrations/ → repo/).
    """
    src = NEW_PATHS["migrate_to_supabase.py"].read_text()
    # The fix should NOT use bare `os.path.dirname(__file__)` for state.db.
    # It should use either: (a) explicit dirname(dirname(__file__)),
    # (b) Path(__file__).resolve().parents[2], or (c) document explicit
    # CWD requirement with a check.
    bad_pattern = 'os.path.join(os.path.dirname(__file__) or ".", "state.db")'
    assert bad_pattern not in src, (
        "migrate_to_supabase.py still uses the bare __file__-derived state.db "
        "path. Post-move this resolves to migrations/state.db (doesn't exist). "
        "Fix with explicit 2-level parent navigation."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 3 — Sweep + R1-lesson regression pins (parametrized)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("name,_old", MIGRATIONS)
def test_no_mock_patch_string_form_for_migration(name, _old):
    """R1-lesson regression pin (Sprint 10.1b): AST-walk for
    `patch("<migration_stem>.X")`. Pre-flight returned 0 sites."""
    stem = name.rsplit(".", 1)[0]
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
                        if first.value == stem or first.value.startswith(f"{stem}."):
                            stale.append(
                                f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                                f'patch("{first.value}")'
                            )
    assert not stale, (
        f"mock.patch string-form targets for {name}: {stale}. "
        f"Retarget each to `patch('migrations.{stem}.X', ...)`."
    )


@pytest.mark.parametrize("name,_old", MIGRATIONS)
def test_no_stale_import_migration(name, _old):
    """No stale `from <migration_stem> import` or `import <migration_stem>` AST
    nodes anywhere (these are one-shot scripts; no production code should import them)."""
    stem = name.rsplit(".", 1)[0]
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
            if isinstance(node, ast.ImportFrom) and node.module == stem:
                stale.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}:from")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == stem:
                        stale.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}:import")
    assert not stale, (
        f"Stale imports of {stem}: {stale}. These are one-shot scripts — "
        f"production code should not import them at all."
    )
