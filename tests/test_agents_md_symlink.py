"""Regression tests for AGENTS.md symlink (Bit 1.3 of repo modularization).

Sprint 1 of repo modularization plan
(kb/decisions/repo-modularization-plan-may05.md).

Pins the Bit 1.3 contract:
- AGENTS.md exists at repo root.
- AGENTS.md is a symlink (not a copy) — single source of truth lives in
  CLAUDE.md so Bit 1.4's CLAUDE.md prune (≤80 lines) automatically prunes
  AGENTS.md too. A future agent who "fixes" the symlink by replacing it
  with a file copy creates two-way drift; these tests catch that.
- Symlink target is the literal string "CLAUDE.md" — relative, so the link
  travels with the repo regardless of checkout path.
- Symlink resolves to the real CLAUDE.md file at repo root (not a dangling
  link, not somewhere else, not outside the repo).
- Reading through AGENTS.md yields byte-identical content to CLAUDE.md.
- Git tracks AGENTS.md as mode 120000 (symlink). Critical because Git for
  Windows can store symlinks as plain text files unless
  `core.symlinks=true` — if a Windows contributor commits a replacement,
  the index mode flips to 100644 and the file silently drifts from
  CLAUDE.md. (No Windows users today; flag here so the regression catches
  it the moment one shows up.)
"""
import ast
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_MD = REPO_ROOT / "AGENTS.md"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
DOC_DRIFT_SCRIPT = REPO_ROOT / "scripts" / "doc_drift_check.py"
MAKEFILE = REPO_ROOT / "Makefile"


def test_agents_md_exists():
    """AGENTS.md must be present at repo root (as a symlink OR a file).

    Subtle: `Path.exists()` follows symlinks and returns False on a
    dangling symlink (target missing). For a "path is present" check that
    doesn't mask the dangling case, use `is_symlink()` (which uses lstat
    and is True for any symlink including dangling) OR `is_file()` (which
    catches a non-symlink regular file). Together they admit both shapes
    Bit 1.3 might land in (today: symlink; if a future Bit flips to
    two-file mode, regular file). The dangling-symlink case is caught here
    via the is_symlink() branch; the broken target itself is caught
    separately by `test_agents_md_resolves_to_claude_md` (strict=True
    raises on dangling).
    """
    assert AGENTS_MD.is_symlink() or AGENTS_MD.is_file(), (
        f"AGENTS.md must exist at repo root: {AGENTS_MD}"
    )


def test_agents_md_is_symlink():
    """Must be a symlink, not a regular file.

    A copy would drift from CLAUDE.md silently. The whole point of Bit 1.3
    is one source of truth.
    """
    assert AGENTS_MD.is_symlink(), (
        "AGENTS.md must be a symlink to CLAUDE.md (not a copy). If a tool "
        "or contributor replaced it with a regular file, recreate with: "
        "rm AGENTS.md && ln -s CLAUDE.md AGENTS.md"
    )


def test_agents_md_target_is_relative_claude_md():
    """Symlink target must be the literal string 'CLAUDE.md' (relative).

    A relative symlink travels with the repo. An absolute symlink
    (`/Users/.../CLAUDE.md`) breaks the moment the repo is cloned to a
    different path — including the VPS. Use os.readlink (NOT Path.resolve)
    to read the link's stored target text without dereferencing.
    """
    target = os.readlink(AGENTS_MD)
    assert target == "CLAUDE.md", (
        f"AGENTS.md symlink target must be the literal string 'CLAUDE.md' "
        f"(relative, same dir); got {target!r}. Recreate with: "
        f"rm AGENTS.md && ln -s CLAUDE.md AGENTS.md"
    )


def test_agents_md_resolves_to_claude_md():
    """Following the symlink lands on the real CLAUDE.md file at repo root."""
    resolved = AGENTS_MD.resolve(strict=True)  # strict=True raises on dangling
    assert resolved == CLAUDE_MD.resolve(strict=True), (
        f"AGENTS.md must resolve to {CLAUDE_MD}; got {resolved}"
    )
    assert resolved.is_file()


def test_agents_md_content_matches_claude_md():
    """Reading through the symlink yields byte-identical content.

    Belt-and-suspenders: if `is_symlink` and `readlink` both pass but the
    underlying file has somehow diverged (e.g., the symlink points to a
    stale CLAUDE.md elsewhere on disk), this catches it.
    """
    assert AGENTS_MD.read_bytes() == CLAUDE_MD.read_bytes(), (
        "AGENTS.md content does not match CLAUDE.md byte-for-byte. "
        "If AGENTS.md is a symlink to CLAUDE.md, this should be impossible "
        "unless the symlink points to a different CLAUDE.md."
    )


def _has_git_working_tree() -> bool:
    """True if a git working tree is reachable from REPO_ROOT.

    `.git` is a directory in normal clones, a regular file in linked
    worktrees (`man git-worktree`), and absent in `git archive` exports
    or sdist/wheel installs. `Path.exists()` is True for the first two,
    so it correctly admits both. Combine with a `shutil.which('git')`
    check so the subsequent subprocess call has the binary it needs.
    """
    return (REPO_ROOT / ".git").exists() and shutil.which("git") is not None


@pytest.mark.skipif(
    not _has_git_working_tree(),
    reason="requires a git working tree + git binary (absent in sdist/wheel/archive exports)",
)
def test_agents_md_git_mode_is_symlink():
    """Git index must track AGENTS.md as mode 120000 (symlink).

    Git for Windows can store symlinks as plain text files when
    `core.symlinks=false`. If a Windows contributor commits a replacement
    in that mode, the index mode flips from 120000 to 100644 and AGENTS.md
    silently becomes a static text snapshot of CLAUDE.md — which then
    drifts as CLAUDE.md updates.

    No Windows contributors today (VPS = Linux, dev box = macOS), so this
    is a forward-looking regression. If you ARE adding a Windows
    contributor, set `git config core.symlinks true` BEFORE first
    checkout.
    """
    out = subprocess.check_output(
        ["git", "ls-files", "--stage", "AGENTS.md"],
        cwd=REPO_ROOT,
        text=True,
    ).strip()
    if not out:
        pytest.fail(
            "AGENTS.md is not tracked by git. After creating the symlink, run: "
            "git add AGENTS.md"
        )
    # Format: "<mode> <sha1> <stage>\t<path>"
    mode = out.split()[0]
    assert mode == "120000", (
        f"AGENTS.md must be tracked as a symlink (git mode 120000); got {mode!r}. "
        f"Likely cause: committed on Windows with core.symlinks=false. "
        f"Fix: rm AGENTS.md && git config core.symlinks true && "
        f"ln -s CLAUDE.md AGENTS.md && git add AGENTS.md"
    )


def _doc_files_literal_elements() -> list:
    """AST-parse `scripts/doc_drift_check.py` and return the literal
    string elements of the EFFECTIVE module-level `DOC_FILES` list.

    AST (not import) avoids the script's import-time side effects
    (requests/urllib top-level imports). AST (not string regex) avoids
    false-positive substring matches in comments, docstrings, or
    unrelated assignments.

    Shapes accepted:
    - `DOC_FILES = [...]`              (ast.Assign with List/Tuple value)
    - `DOC_FILES: list[str] = [...]`   (ast.AnnAssign with List/Tuple value)

    Shapes rejected (fail loudly so a future maintainer notices):
    - Non-literal RHS (`DOC_FILES = some_func()` etc.) — this helper
      cannot evaluate that without importing.
    - `DOC_FILES += [...]` (ast.AugAssign): ambiguous because we'd need
      to fold all augmentations; explicitly out of scope.

    "Effective" = last module-level assignment in source order, matching
    Python name-resolution semantics. If a future patch adds a later
    override (e.g., a temp-shadow during refactor), this returns the
    override — same as runtime — so the test reflects what the script
    actually scans.
    """
    tree = ast.parse(DOC_DRIFT_SCRIPT.read_text())
    matches = []
    for node in ast.iter_child_nodes(tree):  # module level only — no nested
        # Plain assignment: `DOC_FILES = [...]`
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "DOC_FILES":
                    matches.append(node.value)
        # Annotated assignment: `DOC_FILES: list[str] = [...]`
        elif isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and node.target.id == "DOC_FILES"
                and node.value is not None
            ):
                matches.append(node.value)
        elif isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "DOC_FILES":
                pytest.fail(
                    "DOC_FILES uses augmented assignment (`+=`/`*=`/...). "
                    "_doc_files_literal_elements does not fold augmentations; "
                    "either inline the elements into a single assignment or "
                    "extend this helper."
                )
    if not matches:
        pytest.fail(
            "scripts/doc_drift_check.py is missing the DOC_FILES top-level "
            "assignment — likely renamed/refactored. Update this regression "
            "test to point at the new symbol."
        )
    effective = matches[-1]  # Python name-resolution: last assignment wins
    if not isinstance(effective, (ast.List, ast.Tuple)):
        pytest.fail(
            f"DOC_FILES must be a list/tuple literal (so this AST check "
            f"can read its elements); got {type(effective).__name__}"
        )
    return [
        elt.value
        for elt in effective.elts
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
    ]


def test_doc_drift_check_includes_agents_md():
    """`scripts/doc_drift_check.py` DOC_FILES must include 'AGENTS.md'.

    Plan reference: `kb/decisions/repo-modularization-plan-may05.md` line 897
    ("doc_drift_check.py extended to detect symlink target drift").

    Today the entry is dormant — `doc_drift_check.py` skips symlinks in
    its scan loop, so AGENTS.md (a symlink to CLAUDE.md) is bypassed and
    only the canonical CLAUDE.md is scanned. The entry's purpose is
    forward-looking: if a future Bit flips the GO/NO-GO to two-file mode
    (Phase α1 option 2, plan line 891), AGENTS.md becomes a regular file,
    the symlink-skip falls through, and this entry is what makes
    doc-drift report divergence between the two surfaces for tracked
    config values.

    Structural (AST-parse) check, not string regex — see
    `_doc_files_literal_elements` docstring for the rationale.
    """
    elements = _doc_files_literal_elements()
    assert "AGENTS.md" in elements, (
        f"scripts/doc_drift_check.py DOC_FILES must include 'AGENTS.md' "
        f"per Phase α1 of repo-modularization-plan-may05.md (line 897). "
        f"Without it, two-file-mode divergence between AGENTS.md and "
        f"CLAUDE.md goes silent. Current DOC_FILES: {elements}"
    )


def test_test_fast_recipe_invokes_agents_md_test():
    """`make test-fast` must run `tests/test_agents_md_symlink.py`.

    Bit 1.3 introduces sub-second symlink invariants that belong in the
    dev-tooling fast-tier alongside `test_pyproject.py`,
    `test_repo_hygiene.py`, `test_makefile.py`. A future Makefile edit
    that drops this file from the recipe goes silent — the broader
    `make test` would still cover it, but the fast-tier guarantee Bit 1.3
    contributes is unpinned. This test pins the contract from the
    Bit 1.3 side; `tests/test_makefile.py::test_test_fast_invokes_invariant_tests`
    pins the same contract for the original trio.
    """
    text = MAKEFILE.read_text()
    # Find the test-fast recipe — the line(s) following `test-fast:`.
    # Make recipes are tab-indented; capture from `test-fast:` to the next
    # non-tab line. Fold backslash-continuations to handle multi-line
    # recipes.
    folded = re.sub(r"\\\n", " ", text)
    # Tolerate optional prereqs on the target line (`test-fast: deps ...`)
    # and trailing whitespace before the newline. The recipe lines are the
    # tab-indented block immediately after.
    recipe_match = re.search(
        r"^test-fast:[^\n]*\n((?:\t.*\n?)+)",
        folded,
        re.MULTILINE,
    )
    assert recipe_match is not None, (
        "Makefile is missing a `test-fast:` target. This is the Bit 1.2 "
        "contract — likely an unrelated regression; check tests/test_makefile.py."
    )
    recipe = recipe_match.group(1)
    assert "tests/test_agents_md_symlink.py" in recipe, (
        f"`make test-fast` recipe must invoke tests/test_agents_md_symlink.py "
        f"(Bit 1.3). Current recipe: {recipe!r}"
    )
