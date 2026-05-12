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
deletes `bot/_impl.py`, this test continues to be load-bearing — it remains
valid regardless of how `bot/_impl.py` is laid out.
"""

import glob
import os
import pathlib
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_no_root_test_files():
    """No `test_*.py` may live at the repo root.

    Post-Bit-12.2 (2026-05-11), real tests belong under a tier subdir
    (`tests/unit/`, `tests/contracts/`, `tests/integration/`,
    `tests/equivalence/`, or `tests/regression/`); scratchpads belong
    under `research/scratch/` with the `test_` prefix dropped so pytest
    does not collect them. The sister `test_no_tests_root_test_files`
    below enforces the additional Bit-12.2 invariant that nothing lives
    directly at `tests/` root either.
    """
    pattern = os.path.join(PROJECT_ROOT, "test_*.py")
    matches = glob.glob(pattern)
    # Strip the project root prefix for a readable failure message.
    relative = sorted(os.path.relpath(p, PROJECT_ROOT) for p in matches)
    assert relative == [], (
        f"Found {len(relative)} test_*.py file(s) at repo root: {relative}. "
        f"Move real tests to the appropriate tier subdir (tests/unit/, "
        f"tests/contracts/, tests/integration/, tests/regression/); "
        f"scratchpads to research/scratch/ (rename to drop test_ prefix)."
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


def test_test_unit_tier_invokes_no_root_test_files_test():
    """`make test-unit` must run this file.

    Bit 12.2 (Sprint 12, 2026-05-11) moved this file into tests/unit/
    and switched UNIT_FILES from an enumerated file list to a directory
    glob (`tests/unit`). The pin now accepts either form: explicit path
    enumeration (legacy) OR the tests/unit dir glob that subsumes it.
    """
    makefile = pathlib.Path(PROJECT_ROOT) / "Makefile"
    text = makefile.read_text()
    folded = re.sub(r"\\\n", " ", text)
    fragments = []
    for target in ("test-unit", "test-fast"):
        m = re.search(
            rf"^{target}:[^\n]*\n((?:\t.*\n?)+)",
            folded,
            re.MULTILINE,
        )
        if m:
            fragments.append(m.group(1))
    var_match = re.search(
        r"^UNIT_FILES\s*[:?]?=\s*([^\n]+)$",
        folded,
        re.MULTILINE,
    )
    if var_match:
        fragments.append(var_match.group(1))
    assert fragments, (
        "Makefile has neither a `test-unit:` nor a `test-fast:` target "
        "with a recipe body, and no `UNIT_FILES` variable. Likely an "
        "unrelated regression — check tests/unit/test_makefile.py."
    )
    combined = "\n".join(fragments)
    legacy_path = "tests/unit/test_no_root_test_files.py"
    new_path = "tests/unit/test_no_root_test_files.py"
    unit_dir = "tests/unit"
    accepted = legacy_path in combined or new_path in combined or unit_dir in combined
    assert accepted, (
        f"Unit tier (test-unit/test-fast/UNIT_FILES) doesn't invoke "
        f"tests/unit/test_no_root_test_files.py (Bit 1.5 + 12.2). "
        f"Combined surface: {combined!r}"
    )


def test_no_tests_root_test_files():
    """Bit 12.2 invariant — no `test_*.py` directly under tests/.

    Sprint 12 Bit 12.2 reorganized tests/ into tier subdirs
    (unit/integration/contracts/equivalence/regression/hooks). This pin
    catches a future drift where a file lands at tests/ root and bypasses
    tier classification.
    """
    pattern = os.path.join(PROJECT_ROOT, "tests", "test_*.py")
    matches = glob.glob(pattern)
    relative = sorted(os.path.relpath(p, PROJECT_ROOT) for p in matches)
    assert relative == [], (
        f"Found {len(relative)} test_*.py file(s) directly under tests/: "
        f"{relative}. Bit 12.2 requires tier classification — move to "
        f"tests/unit/, tests/integration/, tests/contracts/, or "
        f"tests/regression/."
    )
