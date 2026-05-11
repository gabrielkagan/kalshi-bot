"""Sprint 10 fu — defensive iCloud-conflict filter in contract walker (2026-05-10).

Ticket 86b9vr5hu. Per L93: macOS+iCloud creates `bot/orphan_db_watchdog 2.py`
etc. when iCloud syncs a file from another device while the local file is
also modified. These conflict files have spaces in their stems and are
NOT real Python modules; they break AST walkers and contract enumerations
that iterate `bot/`.

Bit 9.3.5 Phase 0 sweep deleted 32 conflict files; Bit 9.3-ii Phase 0
deleted ~0 (already clean). The recurring breakage pattern motivated this
defensive filter — even if a maintainer forgets to run the Phase 0 sweep,
the contract walker should not crash.

The filter targets `_enumerate_top_level_bot_modules()` in
tests/contracts/test_import_linter_contracts.py — the helpers-leaf
contract walker. A `bot/X 2.py` file would land in the walker's output as
`bot.X 2` (invalid module name with space), causing the
`test_helpers_leaf_forbidden_modules_covers_all_bot_top_level` test to
fail with a confusing "missing module" error.

Filter applied: `if " " in stem: continue` immediately after
`stem = entry.stem`. Robust against the common patterns:
  - `bot/X 2.py`, `bot/X 3.py`, ... (iCloud merge conflict variants)
  - `bot/X 2/` directory (iCloud subdirectory conflicts)
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def test_enumerate_top_level_bot_modules_skips_icloud_conflict_files(tmp_path, monkeypatch):
    """Defensive: `_enumerate_top_level_bot_modules()` MUST skip files
    with spaces in their stems (iCloud conflict pattern `* [23456].py`).

    Without the filter, a stale `bot/orphan_db_watchdog 2.py` left over
    from an iCloud sync would silently land in the walker output as
    'bot.orphan_db_watchdog 2', breaking the helpers-leaf coverage test
    with a confusing error. Filed as ticket 86b9vr5hu per L93.
    """
    from tests.contracts import test_import_linter_contracts as test_module

    # Set up a fake bot/ directory with real and conflict files.
    fake_bot = tmp_path / "bot"
    fake_bot.mkdir()
    (fake_bot / "real_module.py").touch()
    (fake_bot / "real_module 2.py").touch()      # iCloud conflict — must be skipped
    (fake_bot / "another 3.py").touch()           # iCloud conflict — must be skipped
    (fake_bot / "yet_another 4.py").touch()       # iCloud conflict — must be skipped
    (fake_bot / "__init__.py").touch()
    (fake_bot / "__main__.py").touch()

    # Subpackage variants
    real_pkg = fake_bot / "real_pkg"
    real_pkg.mkdir()
    (real_pkg / "__init__.py").touch()

    # iCloud conflict subdirectory (with __init__.py)
    conflict_pkg = fake_bot / "real_pkg 2"
    conflict_pkg.mkdir()
    (conflict_pkg / "__init__.py").touch()

    # Temporarily redirect REPO_ROOT so the walker scans our fake tree.
    monkeypatch.setattr(test_module, "REPO_ROOT", tmp_path)
    modules = test_module._enumerate_top_level_bot_modules()

    assert "bot.real_module" in modules, (
        f"Walker missed the real bot.real_module: {modules}"
    )
    assert "bot.real_pkg" in modules, (
        f"Walker missed the real bot.real_pkg subpackage: {modules}"
    )
    # Conflict files MUST NOT appear.
    for stale in (
        "bot.real_module 2",
        "bot.another 3",
        "bot.yet_another 4",
        "bot.real_pkg 2",
    ):
        assert stale not in modules, (
            f"Walker leaked iCloud conflict entry {stale!r} into output. "
            f"Filter `if ' ' in stem: continue` missing or broken. "
            f"Full modules set: {modules}"
        )


def test_no_icloud_conflicts_in_bot_directory_today():
    """Sanity: no iCloud conflict files in bot/ at current HEAD.

    This is a Phase-0-sweep companion check. Even with the walker filter
    in place, conflict files in bot/ are still pre-commit lint targets —
    they indicate iCloud sync conflict residue from another device that
    should be reconciled (not deleted blind)."""
    bot_dir = REPO_ROOT / "bot"
    conflicts = [
        p for p in bot_dir.rglob("*.py")
        if " " in p.stem
    ]
    assert not conflicts, (
        f"L93: iCloud conflict files in bot/ that the walker filter "
        f"would skip (but should still be reconciled): {[str(p) for p in conflicts]}. "
        f"Inspect each: `diff bot/X.py 'bot/X 2.py'` then delete the conflict "
        f"once you confirm it has no unique changes."
    )
