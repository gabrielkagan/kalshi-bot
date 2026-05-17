"""Sprint 10.1a — `sports_data.py` → `bot/engines/sports_data.py` (2026-05-11).

First sub-move of the Sprint 10 sibling-reorg pattern (master plan
L2225-2238 — Sprint 10 row "engines/" 4 sub-moves). sports_data.py is
the smallest of the 4 engines (~477 LOC, pure data/config — no class
state, no runtime side effects). Moving it first establishes the
sibling-reorg shape with the lowest blast radius.

Sub-moves left in Sprint 10.1 after this Bit:
  - 10.1b: spx_engine.py → bot/engines/spx_engine.py (filename preserved)
  - 10.1c: weather_engine.py → bot/engines/weather_engine.py (filename preserved per 10.1b precedent)
  - 10.1d: sports_engine.py → bot/engines/sports.py (bundles with .1a since
    sports_engine imports from sports_data)

Per `feedback_modularization_skip_soak.md`: no shim re-export at the old
path, no soak window. All caller imports retargeted atomically in the same
commit. Verification = post-commit regression test + post-deploy verify.

5 things must hold post-Bit:
  1. `bot/engines/sports_data.py` exists; root `sports_data.py` is GONE.
  2. `bot/engines/__init__.py` does NOT re-export sports_data symbols
     (keeping bot.engines.__init__ focused on the 3 engine classes;
     callers use `from bot.engines.sports_data import LEAGUES` directly).
  3. All 5 caller import sites updated:
     - sports_engine.py (top-level `from sports_data import ...`)
     - market_config.py:~422 (method-body `from sports_data import SPORT_GROUPS`)
     - tests/integration/test_extended_features.py:~342 (method-body)
     - tests/integration/test_step3_soccer_reenabled.py:~45 (method-body)
     - bot/engines/calibration.py:~1163,~1183 (method-body)
  4. No stale `from sports_data import` outside `.claude/worktrees/` (excluded).
  5. Behavioral: `from bot.engines.sports_data import LEAGUES` works at
     runtime.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest
import bot.engines.__init__  # noqa: F401


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

OLD_PATH = REPO_ROOT / "sports_data.py"
NEW_PATH = REPO_ROOT / "bot" / "engines" / "sports_data.py"


def test_old_sports_data_path_is_gone():
    """Root `sports_data.py` must be DELETED post-move (no shim left at
    the old path, per skip-soak feedback)."""
    assert not OLD_PATH.exists(), (
        f"{OLD_PATH} still exists. Sprint 10.1a per skip-soak feedback "
        f"says no shim re-export; the file should be moved cleanly via "
        f"`git mv sports_data.py bot/engines/sports_data.py`."
    )


def test_new_sports_data_path_exists():
    """`bot/engines/sports_data.py` exists post-move."""
    assert NEW_PATH.exists(), (
        f"{NEW_PATH} missing — Sprint 10.1a move not performed."
    )


def test_no_stale_from_sports_data_imports(repo_ast_cache):
    """No `from sports_data import ...` outside worktrees post-move.

    All 5 real caller sites must be retargeted to
    `from bot.engines.sports_data import ...` atomically.

    Bit-4.5 (2026-05-17): consumes session-scoped ``repo_ast_cache``.
    """
    stale_sites: list[str] = []
    for path, tree in repo_ast_cache.items():
        if tree is None:
            continue
        if path == NEW_PATH:
            # The new file itself is fine; it doesn't import from itself
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "sports_data":
                stale_sites.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not stale_sites, (
        f"Stale `from sports_data import ...` sites post-move: {stale_sites}. "
        f"Retarget each to `from bot.engines.sports_data import ...`."
    )


def test_no_stale_import_sports_data(repo_ast_cache):
    """No bare `import sports_data` outside worktrees.

    Bit-4.5 (2026-05-17): consumes session-scoped ``repo_ast_cache``.
    """
    stale_sites: list[str] = []
    for path, tree in repo_ast_cache.items():
        if tree is None:
            continue
        if " " in path.stem:  # L93 iCloud filter
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "sports_data":
                        stale_sites.append(
                            f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
                        )
    assert not stale_sites, (
        f"Stale `import sports_data` sites: {stale_sites}. Retarget to "
        f"`import bot.engines.sports_data` or `from bot.engines.sports_data import ...`."
    )


def test_sports_data_importable_at_new_path():
    """Behavioral smoke: `from bot.engines.sports_data import LEAGUES` works."""
    from bot.engines.sports_data import LEAGUES
    assert LEAGUES, "sports_data.LEAGUES is empty — import resolved but data missing"


def test_sports_data_sport_groups_accessible():
    """Behavioral smoke: SPORT_GROUPS used by market_config.py is accessible."""
    from bot.engines.sports_data import SPORT_GROUPS
    assert SPORT_GROUPS, "SPORT_GROUPS is empty"


def test_bot_engines_does_not_reexport_sports_data():
    """bot/engines/__init__.py focused on the 3 engine classes only —
    callers reach sports_data via `from bot.engines.sports_data import ...`
    directly, not via `from bot.engines import LEAGUES`."""
    src = (REPO_ROOT / "bot" / "engines" / "__init__.py").read_text()
    # The old-form `from sports_data import` should not appear in the package init.
    assert "from sports_data import" not in src, (
        "bot/engines/__init__.py uses old-form `from sports_data import` — "
        "should be `from bot.engines.sports_data import ...` if needed at all "
        "(prefer not exposing in package __init__; callers use direct submodule)."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 2 — String-literal path-reference sweep (Plan-agent R1 finding)
# ═════════════════════════════════════════════════════════════════════════════
# The AST tests above catch `from sports_data import ...` and `import sports_data`,
# but Sprint 10.1a R1 surfaced 6 MAJOR breakages from STRING-LITERAL references
# to "sports_data.py" in CI workflows + scripts (e.g., `os.path.join(REPO_DIR,
# "sports_data.py")`, `git log -- "sports_data.py"`, GitHub Actions
# `paths: ['sports_data.py']`). These survive AST walks because the file path
# is a string, not an import. This section grep-sweeps for stale string-literal
# references and is REUSABLE for the remaining Sprint 10.1 sub-moves (just
# parametrize on the moved-file name).


# Paths where `"sports_data.py"` may legitimately appear (operator runbook,
# closeout doc, this test file's docstring, etc.). Allow-list keeps the
# sweep precise.
_PATH_LITERAL_ALLOW = {
    "tests/integration/test_sprint_10_1a_sports_data_move.py",      # this file
    "tests/CLAUDE.md",                                  # may reference historical paths
    "kb/decisions",                                     # local-only KB
    "kb-research",                                      # local-only KB
    ".claude/worktrees",                                # parallel-session branches
    ".git",                                             # internal
    "venv",                                             # virtualenv
}


def _sweep_string_literal_refs(needle: str, suffixes: tuple = (".py", ".yml", ".yaml", ".md", ".sh")) -> list[str]:
    """Walk the repo for files containing `needle` as a literal substring.

    Returns relative paths of files where `needle` appears AND the path is
    NOT in the allow-list. Used to detect file-relocation drift in CI
    workflows, scripts, and docs that AST walks can't see."""
    hits: list[str] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in suffixes:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        # Allow-list filter
        if any(rel.startswith(allow) for allow in _PATH_LITERAL_ALLOW):
            continue
        # L93 iCloud filter
        if " " in path.stem:
            continue
        try:
            src = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if needle in src:
            # Distinguish allowed from disallowed sites with a line-number snippet
            for lineno, line in enumerate(src.splitlines(), 1):
                if needle in line:
                    hits.append(f"{rel}:{lineno}: {line.strip()[:100]}")
    return hits


def test_no_stale_string_literal_sports_data_in_scripts_or_workflows():
    """R1 finding: `os.path.join(REPO_DIR, "sports_data.py")` and similar
    string-literal references break silently post-move (silent return of
    empty dict/None → silent corruption of generated docs / CI gates).

    Narrowed scope (vs the initial implementation): flag ONLY lines that
    are CLEARLY path-anchored references to the bare repo-root path, not
    every line that mentions the filename. Two failure modes:

      1. CI workflow `paths:` triggers — `- 'sports_data.py'` in a YAML
         `paths:` block silently fails to trigger the workflow post-move.
      2. `os.path.join(REPO_DIR, "sports_data.py")` or similar bare
         single-component path literals that resolve to repo root.

    Allows:
      - Lines that ALSO mention `bot/engines/sports_data.py` (the correct
        post-move path) — these are comments/docstrings annotating the
        relocation.
      - Lines that are part of `os.path.join(REPO_DIR, "bot", "engines",
        "sports_data.py")` (multi-component path join — correct form).
      - Comment lines (start with `#` after whitespace).
      - Docstrings (matched heuristically: triple-quote on line).
      - README.md (auto-generated from README.template.md, which IS pinned).
    """
    needle = "sports_data.py"
    correct = "bot/engines/sports_data.py"
    hits = _sweep_string_literal_refs(needle)
    stale = []
    for h in hits:
        rel, rest = h.split(": ", 1)
        # 1. Allow lines that also reference the correct path
        if correct in rest:
            continue
        # 2. Allow lines that are part of the multi-component os.path.join form
        if '"bot", "engines"' in rest or "'bot', 'engines'" in rest:
            continue
        # 3. Skip pure comment lines (start with `#` after whitespace)
        stripped = rest.strip()
        if stripped.startswith("#"):
            continue
        # 4. Skip docstring lines (triple-quote present on line)
        if '"""' in stripped or "'''" in stripped:
            continue
        # 5. README.md is auto-generated from README.template.md (already pinned)
        if rel == "README.md":
            continue
        # 6. Skip lines that look like pure prose (no quoting around the needle)
        # e.g. "Count leagues in sports_data.py LEAGUES dict" (docstring fragment)
        # Detect by absence of `"sports_data.py"` or `'sports_data.py'` quoted form.
        if f'"{needle}"' not in stripped and f"'{needle}'" not in stripped:
            continue
        stale.append(h)
    assert not stale, (
        f"Stale string-literal references to bare `sports_data.py` (path-anchored "
        f"reads, CI workflow triggers, git pathspecs) survived the move. Each must "
        f"be updated to `bot/engines/sports_data.py` (or explicitly include both "
        f"pre+post paths for git log/diff pathspecs that need to span the rename "
        f"commit). Found {len(stale)} sites:\n"
        + "\n".join(stale[:20])
    )
