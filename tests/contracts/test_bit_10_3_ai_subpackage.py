r"""Sprint 10 Bit 10.3 — bot/ai/ subpackage relocation (2026-05-12).

Moves 3 files from repo root → bot/ai/ (~4,114 LOC total):
  - analyst.py    (1452 LOC) — Claude-API loss analysis + news sentiment
  - auditor.py    (1396 LOC) — Hourly deterministic checks → Telegram alerts
  - researcher.py (1266 LOC) — 3×/day performance reports → Telegram

Per `feedback_modularization_skip_soak.md`: no shim, no soak.
Filename preserved per Sprint 10.1b/c/d + 10.2 + 10.4 + 10.5a + 10.5b + 10.6 precedent.

L32 Plan-agent pre-flight (CRITICAL × 3, all `__file__`-derived path sites):
  R1.a — auditor.py:42 `SCRIPT_DIR = Path(__file__).resolve().parent` anchors
         state.db / auditor_state.db / .env / scan_journal.jsonl / opportunity_journal.jsonl
         to the file's directory. Today: repo root. Post-move: bot/ai/. Five
         data-file paths break: sqlite3 would CREATE empty DBs at bot/ai/state.db
         (auditor reads 0 rows, every hourly run silently green) + .env load
         silently no-op (Telegram token unset → alerts fall through).
  R1.b — researcher.py:50 SCRIPT_DIR same failure mode — state.db /
         auditor_state.db / researcher_state.db / .env all break.
  R1.c — analyst.py:35 `DEFAULT_DB_PATH = "state.db"` is a CWD-relative
         literal (not __file__-derived). Runtime invocation (`python3
         bot/ai/analyst.py`) from repo root keeps it working; from
         arbitrary CWD it would still break (latent today, not Bit 10.3-
         introduced). NOT fixing in this Bit (out-of-scope: tradeoff-
         relative-literal vs absolute is a separate decision).

Fix pattern (matches bot/snapshots/supabase_sync.py:30 precedent from Bit 10.4):
    _REPO_ROOT = Path(__file__).resolve().parent.parent.parent
For bot/ai/X.py → ai/ → bot/ → repo/, the 3-level dirname chain anchors
back to the repo root regardless of relocation.

R2 (caller surface): three files are STANDALONE entrypoints — no Python
import sites (`grep -rn "from analyst\b\|^import analyst$" --include="*.py"`
returns 0 matches). VPS crontab invokes via filesystem path (out-of-repo).
Crontab retarget (`python3 bot/ai/auditor.py`) is operator concern; this
Bit files a followup ticket but does NOT mutate VPS state. AST-pinned
caller surface = 4 test files + 1 path-literal in tests/contracts/.

R3 (.importlinter): no bot/helpers/*.py references the 3 ai/ modules
(verified pre-flight: grep returns 0 matches). helpers-leaf forbidden_modules
need NOT add `bot.ai`; no carve-out required. Contract count unchanged at 5.

R4 (Telegram routing): the three scripts are NOT routed via a single
dispatcher — each reads TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID from .env
and POSTs to Telegram HTTPS API directly. No router pin needed.

R5 (doc-drift, L86): README.md, README.template.md, agent_docs/bot_layout.md,
agent_docs/current_state.md, bot/CLAUDE.md (3 sites), TESTING_STRATEGY.md
(3 sites), POSTMORTEMS.md (3 sites), whitepaper.md + whitepaper_rendered.md
(4 sites each), kb/failures/database-contention.md (5 sites), kb/failures/
hwm-bugs.md (1 site), kb/decisions/openclaw-deferred.md (3 sites),
kb/decisions/config-models-extraction.md (1 site), kb/concepts/supabase-schema-
parity.md (1 site), kb-research/infrastructure/claude-code-automation.md (5
sites).

R6 (test surface): contract-tier AST-pin retargets in test_db_signatures.py,
test_insert_schema_parity.py, test_product_type_enum.py,
test_analyst_current_config_sync.py.

L83 module-attribute access: none of the 3 files reference runtime-
mutable singletons (_TELEGRAM, _CALIBRATION_ENGINE etc.) — they read
Telegram tokens from os.environ at module-load time and dispatch directly.
No bare-name re-binding hazard.

L98 staging-gap discipline: SCRIPT_DIR fix + path-literal retargets MUST
be in the same commit as the git mv; otherwise tests pass against stale
repo-root literals while the bot/ai/ files have broken paths.
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
    REPO_ROOT / "analyst.py",
    REPO_ROOT / "auditor.py",
    REPO_ROOT / "researcher.py",
)
NEW_DIR = REPO_ROOT / "bot" / "ai"
NEW_INIT = NEW_DIR / "__init__.py"
NEW_FILES = (
    NEW_DIR / "analyst.py",
    NEW_DIR / "auditor.py",
    NEW_DIR / "researcher.py",
)

MODULE_STEMS = ("analyst", "auditor", "researcher")


# ═════════════════════════════════════════════════════════════════════════════
# Section 1 — Filesystem layout
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("old_path", OLD_FILES, ids=lambda p: p.name)
def test_old_repo_root_file_is_gone(old_path):
    """Repo-root file DELETED post-move (no shim per skip-soak)."""
    assert not old_path.exists(), (
        f"{old_path.relative_to(REPO_ROOT)} still exists at repo root. "
        f"Sprint 10.3: file must be relocated to bot/ai/."
    )


def test_new_ai_subpackage_init_exists():
    """`bot/ai/__init__.py` exists (subpackage marker)."""
    assert NEW_INIT.exists(), f"{NEW_INIT.relative_to(REPO_ROOT)} missing."


@pytest.mark.parametrize("new_path", NEW_FILES, ids=lambda p: p.name)
def test_new_ai_file_exists(new_path):
    """Each of the 3 files exists at its new location under bot/ai/."""
    assert new_path.exists(), (
        f"{new_path.relative_to(REPO_ROOT)} missing — relocation not performed."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 2 — Importability + module identity
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("stem", MODULE_STEMS)
def test_bot_ai_module_is_importable(stem):
    """`import bot.ai.<stem>` succeeds.

    Skipped if optional heavy deps (anthropic / pydantic for analyst) are
    not installed in the test environment — this is invariant of relocation.
    """
    name = f"bot.ai.{stem}"
    try:
        mod = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        # Optional runtime deps: anthropic, pydantic (analyst-only)
        if exc.name in ("anthropic", "pydantic"):
            pytest.skip(f"{exc.name} not installed in test env")
        raise
    assert mod.__name__ == name


@pytest.mark.parametrize("stem", MODULE_STEMS)
def test_bot_ai_module_file_is_canonical_path(stem):
    """`bot.ai.<stem>.__file__` resolves under `bot/ai/` (no shim leak).

    INVARIANT — would fail if a re-export shim were left at repo root
    masking the canonical module.
    """
    name = f"bot.ai.{stem}"
    try:
        mod = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name in ("anthropic", "pydantic"):
            pytest.skip(f"{exc.name} not installed in test env")
        raise
    file_path = Path(mod.__file__).resolve()
    expected_suffix = Path("bot") / "ai" / f"{stem}.py"
    assert file_path == REPO_ROOT / expected_suffix, (
        f"{name}.__file__ = {file_path}, expected {REPO_ROOT / expected_suffix}"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 3 — __file__-derived path sites (RCA: SCRIPT_DIR repath)
# ═════════════════════════════════════════════════════════════════════════════
# Reads each new file's source and confirms SCRIPT_DIR / _REPO_ROOT is
# anchored via a 3-level .parent chain (or equivalent dirname triple),
# not a 1-level chain that would resolve to bot/ai/.


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "stem",
    ("auditor", "researcher"),
    ids=("auditor", "researcher"),
)
def test_script_dir_anchored_to_repo_root(stem):
    """SCRIPT_DIR (or REPO_ROOT) in auditor.py + researcher.py resolves
    to the repository root, not to bot/ai/.

    Pre-Bit-10.3 (repo root): Path(__file__).resolve().parent → repo root ✓
    Post-Bit-10.3 (bot/ai/):  Path(__file__).resolve().parent → bot/ai/ ✗
                              Path(__file__).resolve().parent.parent.parent → repo root ✓

    Invariant: the value of the path-anchor variable resolves to the
    repository root at runtime — not relocation-fragile.
    """
    new_path = NEW_DIR / f"{stem}.py"
    src = _read_text(new_path)
    # Parse the module and find SCRIPT_DIR (or _REPO_ROOT) assignment
    tree = ast.parse(src)
    anchor_value = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id in ("SCRIPT_DIR", "_REPO_ROOT", "REPO_ROOT")
                ):
                    # Execute the RHS in a minimal namespace to evaluate
                    rhs_src = ast.get_source_segment(src, node.value)
                    ns = {
                        "Path": Path,
                        "__file__": str(new_path),
                    }
                    try:
                        anchor_value = eval(rhs_src, ns)
                    except Exception as exc:
                        pytest.fail(
                            f"{stem}.py path-anchor RHS could not be eval'd: "
                            f"{rhs_src!r} → {exc!r}"
                        )
                    break
            if anchor_value is not None:
                break
    assert anchor_value is not None, (
        f"{stem}.py has no SCRIPT_DIR / _REPO_ROOT / REPO_ROOT top-level "
        f"assignment — relocation may have lost the path anchor."
    )
    resolved = Path(anchor_value).resolve()
    assert resolved == REPO_ROOT, (
        f"{stem}.py path anchor resolves to {resolved} (expected {REPO_ROOT}). "
        f"Use `Path(__file__).resolve().parent.parent.parent` for bot/ai/."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 4 — Caller surface: no stale `from analyst|auditor|researcher`
# ═════════════════════════════════════════════════════════════════════════════


_BAD_NAMES = frozenset({"analyst", "auditor", "researcher"})


def test_no_python_import_from_repo_root_names():
    """No Python file in the repo issues an actual `import X` /
    `from X import …` for any of analyst/auditor/researcher at the
    top level of an import statement.

    These three modules are standalone entrypoints; today the grep returns
    zero hits, but post-relocation a stale `from analyst import …` would
    raise ModuleNotFoundError. Invariant pin.

    Uses AST parsing (not substring match) so comments + docstrings + error
    messages mentioning the names are NOT flagged — only actual import
    statements that resolve `analyst` / `auditor` / `researcher` as
    top-level modules.
    """
    offenders = []
    for py in REPO_ROOT.rglob("*.py"):
        rel = py.relative_to(REPO_ROOT)
        parts = rel.parts
        # Skip vendor / cache / worktree / venv directories
        if any(
            p
            in {
                "__pycache__",
                ".venv",
                "venv",
                ".git",
                "node_modules",
            }
            for p in parts
        ):
            continue
        if ".claude" in parts and "worktrees" in parts:
            continue
        # Skip the bot/ai/ files themselves (they're now canonical home,
        # not stale roots) — though they shouldn't trigger anyway since
        # they don't self-import.
        try:
            src = py.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        try:
            tree = ast.parse(src, filename=str(rel))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    top = a.name.split(".", 1)[0]
                    if top in _BAD_NAMES:
                        offenders.append((str(rel), f"import {a.name}"))
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    top = node.module.split(".", 1)[0]
                    if top in _BAD_NAMES:
                        names = ", ".join(a.name for a in node.names)
                        offenders.append(
                            (str(rel), f"from {node.module} import {names}")
                        )
    assert not offenders, (
        f"Found stale repo-root imports of analyst/auditor/researcher:\n"
        + "\n".join(f"  {f}: `{p}`" for f, p in offenders)
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 5 — Doc-drift sentinels (L86)
# ═════════════════════════════════════════════════════════════════════════════
# Pin the high-value doc surfaces post-retarget. Specifically: README.md
# file map must list the new location; bot_layout.md class table must
# point bot/ai/.


def test_readme_file_map_references_bot_ai():
    """README.md file map points to bot/ai/ for analyst/auditor/researcher."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    # The pre-Bit map listed analyst.py at the top level under the file
    # tree. Post-Bit, it should be moved into bot/ai/ block.
    # Invariant: no bare "analyst.py" line at the top level of the file map.
    # We accept that prose references can remain ("AI analyst (analyst.py)" is
    # ambiguous), but the file-tree path should mention bot/ai/.
    if "analyst.py" not in readme:
        pytest.skip("README.md no longer mentions analyst.py at all")
    assert "bot/ai/" in readme or "bot/ai" in readme, (
        "README.md mentions analyst.py but doesn't reference bot/ai/ — "
        "file-map didn't get updated."
    )


def test_bot_layout_md_references_bot_ai():
    """agent_docs/bot_layout.md mentions bot/ai/ subpackage."""
    layout = (REPO_ROOT / "agent_docs" / "bot_layout.md").read_text(encoding="utf-8")
    assert "bot/ai/" in layout or "bot/ai" in layout, (
        "agent_docs/bot_layout.md missing bot/ai/ reference."
    )
