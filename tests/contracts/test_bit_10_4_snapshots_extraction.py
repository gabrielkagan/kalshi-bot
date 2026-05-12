"""Sprint 10 Bit 10.4 — snapshots/ subpackage relocation (2026-05-12).

Moves 4 files from repo root → bot/snapshots/ (6,607 LOC total):
  - dashboard_snapshot.py            (4500 LOC)
  - bot_state_snapshot.py            ( 491 LOC)
  - market_observations_snapshotter.py ( 575 LOC)
  - supabase_sync.py                 (1049 LOC)

Per `feedback_modularization_skip_soak.md`: no shim, no soak.
Filename preserved per Sprint 10.1b/c/d + 10.2 + 10.5a + 10.5b + 10.6 precedent.

L32 Plan-agent pre-flight (CRITICAL × 3, all `__file__`-derived path sites):
  R1.a — dashboard_snapshot.py:1005 dist_config.json path resolves to repo root
         today (`os.path.dirname(__file__)` = repo root); post-move resolves
         to `bot/snapshots/dist_config.json` (missing) and nig_distribution
         silently becomes None on every dashboard tick.
  R1.b — supabase_sync.py:23 KILL_SWITCH_FILE same failure mode — operator
         `touch .supabase_kill_switch` at repo root would no longer disable
         sync.
  R1.c — supabase_sync.py:76 `state.db` path same failure mode — sqlite3
         would CREATE an empty DB at bot/snapshots/state.db, syncer pushes
         0 rows for process lifetime.

Fix pattern (matches bot/engines/weather_engine.py:806-807 precedent):
    _REPO_ROOT = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
For bot/snapshots/X.py → snapshots/ → bot/ → repo/, the 3-level dirname
chain anchors back to the repo root regardless of relocation.

R5 (.importlinter): helpers-leaf forbidden_modules must add `bot.snapshots`;
no `ignore_imports` carve-out (no bot/helpers/*.py references the 4
snapshot modules — verified pre-flight).

R6 (test surface): 4 contract-tier AST-walk literal-path retargets +
~13 top-import / function-scoped retargets across tests/integration/.

R7 (doc-drift, L86): scripts/pre_deploy_check.sh:18, agent_docs/bot_layout.md.

No bot/helpers/*.py edges → no carve-out → contract count unchanged at 5.
"""
from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


OLD_FILES = (
    REPO_ROOT / "dashboard_snapshot.py",
    REPO_ROOT / "bot_state_snapshot.py",
    REPO_ROOT / "market_observations_snapshotter.py",
    REPO_ROOT / "supabase_sync.py",
)
NEW_DIR = REPO_ROOT / "bot" / "snapshots"
NEW_INIT = NEW_DIR / "__init__.py"
NEW_FILES = (
    NEW_DIR / "dashboard_snapshot.py",
    NEW_DIR / "bot_state_snapshot.py",
    NEW_DIR / "market_observations_snapshotter.py",
    NEW_DIR / "supabase_sync.py",
)

MODULE_STEMS = (
    "dashboard_snapshot",
    "bot_state_snapshot",
    "market_observations_snapshotter",
    "supabase_sync",
)

SMOKE_NAMES = {
    "bot.snapshots.dashboard_snapshot": ("DashboardSnapshotBuilder",),
    "bot.snapshots.bot_state_snapshot": ("compute_bot_state_snapshot",),
    "bot.snapshots.market_observations_snapshotter": (
        "MarketObservationsSnapshotter",
        "extract_active_15m_tickers",
    ),
    "bot.snapshots.supabase_sync": ("SupabaseSyncer", "KILL_SWITCH_FILE"),
}


# ═════════════════════════════════════════════════════════════════════════════
# Section 1 — Filesystem layout
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("old_path", OLD_FILES, ids=lambda p: p.name)
def test_old_repo_root_file_is_gone(old_path):
    """Repo-root file DELETED post-move (no shim per skip-soak)."""
    assert not old_path.exists(), (
        f"{old_path.relative_to(REPO_ROOT)} still exists at repo root. "
        f"Sprint 10.4: file must be relocated to bot/snapshots/."
    )


def test_new_snapshots_subpackage_init_exists():
    """`bot/snapshots/__init__.py` exists (subpackage marker)."""
    assert NEW_INIT.exists(), f"{NEW_INIT.relative_to(REPO_ROOT)} missing."


@pytest.mark.parametrize("new_path", NEW_FILES, ids=lambda p: p.name)
def test_new_snapshots_file_exists(new_path):
    """Each of the 4 files exists at its new location under bot/snapshots/."""
    assert new_path.exists(), (
        f"{new_path.relative_to(REPO_ROOT)} missing — relocation not performed."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 2 — Importability + smoke
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "module_path,names",
    sorted(SMOKE_NAMES.items()),
    ids=lambda v: v if isinstance(v, str) else "...",
)
def test_smoke_importable_at_new_path(module_path, names):
    """Each load-bearing export importable from new module path."""
    mod = importlib.import_module(module_path)
    for name in names:
        assert getattr(mod, name, None) is not None, (
            f"{module_path}.{name} missing post-move."
        )


# ═════════════════════════════════════════════════════════════════════════════
# Section 3 — Stale `from <stem> import …` / `import <stem>` AST sweep
# ═════════════════════════════════════════════════════════════════════════════


def _repo_py_files():
    return (
        list(REPO_ROOT.glob("*.py"))
        + list((REPO_ROOT / "bot").rglob("*.py"))
        + list((REPO_ROOT / "tests").rglob("*.py"))
        + list((REPO_ROOT / "scripts").rglob("*.py"))
    )


@pytest.mark.parametrize("stem", MODULE_STEMS)
def test_no_stale_from_imports(stem):
    """No `from <stem> import …` AST nodes outside worktrees + iCloud dups.

    Files under `bot/snapshots/` itself are intra-package and excluded.
    """
    stale: list[str] = []
    for path in _repo_py_files():
        if ".claude/worktrees/" in str(path):
            continue
        if " " in path.stem:  # iCloud "file 2.py" / "file 3.py" duplicates
            continue
        if str(NEW_DIR) in str(path.parent):
            continue
        try:
            tree = ast.parse(path.read_text())
        except (UnicodeDecodeError, OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == stem:
                stale.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not stale, (
        f"Stale `from {stem} import …` sites: {stale}. "
        f"Retarget each to `from bot.snapshots.{stem} import …`."
    )


@pytest.mark.parametrize("stem", MODULE_STEMS)
def test_no_stale_bare_imports(stem):
    """No bare `import <stem>` AST nodes outside worktrees + iCloud dups."""
    stale: list[str] = []
    for path in _repo_py_files():
        if ".claude/worktrees/" in str(path):
            continue
        if " " in path.stem:
            continue
        if str(NEW_DIR) in str(path.parent):
            continue
        try:
            tree = ast.parse(path.read_text())
        except (UnicodeDecodeError, OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == stem:
                        stale.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not stale, (
        f"Stale `import {stem}` sites: {stale}. "
        f"Retarget to `import bot.snapshots.{stem} as {stem}` "
        f"or `from bot.snapshots.{stem} import …`."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 4 — `__file__`-derived path resolution (R1 CRITICAL fixes)
# ═════════════════════════════════════════════════════════════════════════════


def _read(p: Path) -> str:
    return p.read_text()


def test_dashboard_snapshot_dist_config_anchored_to_repo_root():
    """R1.a CRITICAL — dashboard_snapshot.py reads dist_config.json from
    REPO ROOT, but pre-move `os.path.dirname(__file__)` happened to be
    repo root. Post-move the file lives at bot/snapshots/ so the same
    expression resolves to `bot/snapshots/dist_config.json` (missing) and
    `nig_distribution` silently becomes None on every dashboard tick.

    Pin: the source must contain the 3-level dirname anchor pattern
    (matching bot/engines/weather_engine.py:806-807 precedent) AND a
    join referencing `dist_config.json` against that anchor.
    """
    src = _read(NEW_DIR / "dashboard_snapshot.py")
    assert (
        "os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))"
        in src
        or "Path(__file__).resolve().parents[2]" in src
    ), (
        "bot/snapshots/dashboard_snapshot.py is missing the 3-level "
        "repo-root anchor (`os.path.dirname(os.path.dirname(os.path.dirname"
        "(os.path.abspath(__file__))))` or `Path(__file__).resolve().parents[2]`). "
        "Required to resolve dist_config.json correctly post-move. "
        "Fix pattern: see bot/engines/weather_engine.py:806-807."
    )
    # And the anchor must be used to find dist_config.json (string anywhere
    # in src referencing the file is enough — the AST exhaustively grepping
    # for a specific shape would be too brittle).
    assert "dist_config.json" in src


def test_supabase_sync_kill_switch_resolves_to_repo_root():
    """R1.b CRITICAL — KILL_SWITCH_FILE must resolve to REPO ROOT.

    Loads supabase_sync as a module and asserts the KILL_SWITCH_FILE
    constant's directory is REPO_ROOT (not bot/snapshots/). Operator
    workflow per supabase_sync.py:6 says repo root.
    """
    import bot.snapshots.supabase_sync as ss

    actual_dir = Path(ss.KILL_SWITCH_FILE).resolve().parent
    assert actual_dir == REPO_ROOT, (
        f"bot.snapshots.supabase_sync.KILL_SWITCH_FILE resolves to "
        f"{actual_dir!s} but must resolve to {REPO_ROOT!s} so that "
        f"operator `touch <repo>/.supabase_kill_switch` continues to "
        f"disable the syncer. Fix: anchor with the 3-level dirname "
        f"chain (mirrors bot/engines/weather_engine.py:806-807)."
    )


def test_supabase_sync_state_db_anchored_to_repo_root():
    """R1.c CRITICAL — sqlite3.connect target for state.db must anchor
    to REPO ROOT. Pre-move `os.path.dirname(__file__)` happened to be
    repo root; post-move it becomes `bot/snapshots/` and sqlite3 would
    silently CREATE an empty DB there.

    AST-walks supabase_sync.py and asserts no remaining
    `os.path.join(os.path.dirname(__file__) or ".", "state.db")` shape.
    Pin: source must contain the 3-level repo-root anchor.
    """
    src = _read(NEW_DIR / "supabase_sync.py")
    # The original brittle expression must be gone.
    assert (
        'os.path.dirname(__file__) or "."' not in src
    ), (
        "bot/snapshots/supabase_sync.py still uses "
        "`os.path.dirname(__file__) or '.'` — that expression resolves to "
        "bot/snapshots/ post-move, silently creating bot/snapshots/state.db. "
        "Replace with the 3-level dirname chain anchored to repo root."
    )
    # The repo-root anchor pattern must be present.
    assert (
        "os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))"
        in src
        or "Path(__file__).resolve().parents[2]" in src
    ), (
        "bot/snapshots/supabase_sync.py missing the 3-level repo-root "
        "anchor. Fix pattern: see bot/engines/weather_engine.py:806-807."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 5 — `.importlinter` helpers-leaf coverage (R5 MAJOR)
# ═════════════════════════════════════════════════════════════════════════════


def test_importlinter_helpers_leaf_lists_bot_snapshots():
    """Sprint 10.1c/10.5b precedent: helpers-leaf `forbidden_modules`
    enumerates every top-level bot.X subpackage (verified by
    test_helpers_leaf_forbidden_modules_covers_all_bot_top_level in
    tests/contracts/test_import_linter_contracts.py). Adding
    `bot/snapshots/__init__.py` without updating .importlinter fails
    that walker; this pin catches the omission directly.
    """
    src = _read(REPO_ROOT / ".importlinter")
    # Match a line containing `bot.snapshots` in the forbidden_modules block,
    # not in commentary.
    forbidden_block = src.split("[importlinter:contract:helpers-leaf]", 1)[-1]
    forbidden_block = forbidden_block.split("[importlinter:contract:", 1)[0]
    assert "bot.snapshots" in forbidden_block, (
        ".importlinter helpers-leaf `forbidden_modules` missing `bot.snapshots`. "
        "Append it alphabetically (near `bot.shadows`). The "
        "test_helpers_leaf_forbidden_modules_covers_all_bot_top_level walker "
        "fails until the entry is added."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 6 — main_loop.py late-binding imports retargeted
# ═════════════════════════════════════════════════════════════════════════════


def test_main_loop_imports_retargeted_to_bot_snapshots():
    """bot/main_loop.py has 4 late-binding imports of these modules:
      - line ~379: `from market_observations_snapshotter import (`
      - line ~545: `from bot_state_snapshot import compute_bot_state_snapshot`
      - line ~795: `from dashboard_snapshot import DashboardSnapshotBuilder`
      - line ~803: `from supabase_sync import SupabaseSyncer`

    All four must retarget to `bot.snapshots.<name>`.
    """
    src = _read(REPO_ROOT / "bot" / "main_loop.py")
    tree = ast.parse(src)
    stale: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in MODULE_STEMS:
                stale.append(f"main_loop.py:{node.lineno}: from {node.module} import …")
    assert not stale, (
        f"bot/main_loop.py has stale top-of-old-name imports: {stale}. "
        f"Retarget each to `from bot.snapshots.<name> import …`."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 7 — No `__file__`-derived sentinel anywhere in the 4 modules
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("stem", MODULE_STEMS)
def test_no_naive_dirname_file_usage(stem):
    """Any `os.path.dirname(__file__)` (single-level) inside the 4
    snapshot modules will be wrong post-move. Pin: zero occurrences
    of the naive single-level pattern.

    The 3-level anchor (`os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))`) is fine and not matched by this check.
    """
    path = NEW_DIR / f"{stem}.py"
    if not path.exists():
        pytest.skip(f"{path} not yet present (move pending)")
    src = path.read_text()
    # Strip the 3-level anchor (and inline comments) before checking for
    # the naive single-level shape.
    sanitized = src.replace(
        "os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))",
        "_REPO_ROOT_SAFE_",
    )
    sanitized = sanitized.replace(
        "Path(__file__).resolve().parents[2]",
        "_REPO_ROOT_SAFE_",
    )
    assert "os.path.dirname(__file__)" not in sanitized, (
        f"bot/snapshots/{stem}.py contains naive `os.path.dirname(__file__)` — "
        f"resolves to bot/snapshots/ post-move (likely WRONG). Use the "
        f"3-level repo-root anchor."
    )
