"""Bit 1.5 invariant — no `test_*.py` at the repo root.

Sprint 1 of the modularization plan
(`kb/decisions/repo-modularization-plan-may05.md`) consolidated all
root-level `test_*.py` files into `tests/regression/` (real tests) or
`research/scratch/` (scratchpads, renamed to drop the `test_` prefix).

This test pins the new layout so a future agent who creates a fresh
root-level scratch trips it. It catches a silent drift class — pytest
will happily collect a stray root `test_X.py` (testpaths=["."]) without
any other guardrail noticing.

Sprint-2 hand-off note: when Bit 2.1 creates the `bot/` package and
deletes `bot.py`, this test continues to be load-bearing — it remains
valid regardless of how `bot.py` is laid out.
"""

import glob
import os
import pathlib
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_no_root_test_files():
    """No `test_*.py` may live at the repo root.

    Real tests belong under `tests/` (top-level or `tests/regression/`);
    scratchpads belong under `research/scratch/` with the `test_`
    prefix dropped so pytest does not collect them.
    """
    pattern = os.path.join(PROJECT_ROOT, "test_*.py")
    matches = glob.glob(pattern)
    # Strip the project root prefix for a readable failure message.
    relative = sorted(os.path.relpath(p, PROJECT_ROOT) for p in matches)
    assert relative == [], (
        f"Found {len(relative)} test_*.py file(s) at repo root: {relative}. "
        f"Move real tests to tests/regression/, scratchpads to "
        f"research/scratch/ (rename to drop test_ prefix)."
    )


def test_relocated_real_tests_present():
    """The 7 real tests Bit 1.5 moved must exist under tests/regression/.

    Catches a partial migration where the move-pyproject edit succeeds
    but a file is left behind or accidentally deleted.
    """
    expected = [
        "test_adaptive_jump.py",
        "test_adaptive_rk.py",
        "test_addon.py",
        "test_buffer_persistence.py",
        "test_dashboard_contract.py",
        "test_egarch.py",
        "test_ghost_fill.py",
    ]
    regression_dir = os.path.join(PROJECT_ROOT, "tests", "regression")
    missing = [
        name
        for name in expected
        if not os.path.isfile(os.path.join(regression_dir, name))
    ]
    assert missing == [], (
        f"Missing from tests/regression/: {missing}. "
        f"Bit 1.5 moved these from the repo root; verify the move "
        f"completed."
    )


def test_relocated_scratchpads_present():
    """The 2 HAR scratchpads must exist under research/scratch/ with the
    `test_` prefix dropped so pytest does not collect them.
    """
    expected = ["har.py", "har_iv.py"]
    scratch_dir = os.path.join(PROJECT_ROOT, "research", "scratch")
    missing = [
        name
        for name in expected
        if not os.path.isfile(os.path.join(scratch_dir, name))
    ]
    assert missing == [], (
        f"Missing from research/scratch/: {missing}. "
        f"Bit 1.5 renamed test_har.py → har.py and test_har_iv.py → "
        f"har_iv.py; verify the move completed."
    )


def test_test_fast_recipe_invokes_no_root_test_files_test():
    """`make test-fast` must run `tests/test_no_root_test_files.py`.

    Same pin pattern Bit 1.3 / Bit 1.4 introduced for their invariant
    files. Without this, a future Makefile edit could silently drop
    Bit 1.5's invariant from the dev-tooling fast tier — `make test`
    would still cover it, but the sub-second feedback loop would lose
    a real-world drift catch.

    Brittleness note: the regex matches the literal recipe text, which
    works only while the recipe is a single inline `pytest <files...>`
    line. If a future Bit refactors `test-fast` to use a Make variable
    (e.g. `TEST_FAST_FILES = ...; pytest $(TEST_FAST_FILES)`), the
    literal substring check below fails even when behavior is
    preserved. Update this assertion to either expand variables (read
    `make -n test-fast`) or split-and-search both the variable defn
    and the recipe.
    """
    makefile = pathlib.Path(PROJECT_ROOT) / "Makefile"
    text = makefile.read_text()
    folded = re.sub(r"\\\n", " ", text)
    recipe_match = re.search(
        r"^test-fast:[^\n]*\n((?:\t.*\n?)+)",
        folded,
        re.MULTILINE,
    )
    assert recipe_match is not None, (
        "Makefile is missing a `test-fast:` target. This is the Bit 1.2 "
        "contract — likely an unrelated regression; check "
        "tests/test_makefile.py."
    )
    recipe = recipe_match.group(1)
    assert "tests/test_no_root_test_files.py" in recipe, (
        f"`make test-fast` recipe must invoke "
        f"tests/test_no_root_test_files.py (Bit 1.5). Current recipe: "
        f"{recipe!r}"
    )
