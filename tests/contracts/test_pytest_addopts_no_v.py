"""CI perf Bit-3 (umbrella `86b9zjtzk`, this Bit `86b9zju0v`, 2026-05-17).

Asserts `-v` is NOT in pyproject's `[tool.pytest.ini_options].addopts`.

Why this is a contract, not a preference:
  - The umbrella's anatomy table (5:41 GH Actions run, May 17 2026)
    attributes ~5-15s of CI time to pytest's verbose reporter printing
    every one of ~4500 test names to stdout. Dropping `-v` from the
    default addopts converts the reporter back to the default
    `.`-per-pass / `F`-per-fail dot output without losing failure
    detail (--tb=short still prints tracebacks on red).
  - Per-step CI invocations that need verbose output (e.g.
    `.github/workflows/test.yml`'s fragile-tests step at line 75) pass
    `-v` explicitly, so this contract does NOT touch those.
  - Per-developer local debugging still works: `pytest -v <path>` at
    the CLI overrides addopts.

Cross-test invariant note (assertion-as-fossil lesson, per
`memory/feedback_modularization_skip_soak.md` + the May 15 DD-2
postmortem in `kb/findings/drawdown-signal-drift-may15.md`):
  Bit-3 also edits `tests/unit/test_pyproject.py:221` to remove `-v`
  from the expected-flags tuple AND adjusts a docstring claim in
  `tests/unit/test_makefile.py:337`. Both edits and this new test must
  land in the same commit so CI's `make test-unit` step stays green.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_addopts() -> list[str]:
    try:
        import tomllib  # py3.11+
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # type: ignore[no-redef,import-not-found]
        except ModuleNotFoundError:
            pytest.skip("tomli/tomllib not installed (py<3.11 without backport).")
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    addopts = (
        data.get("tool", {})
        .get("pytest", {})
        .get("ini_options", {})
        .get("addopts", [])
    )
    if isinstance(addopts, str):
        addopts = addopts.split()
    assert isinstance(addopts, list), f"addopts unexpected type: {type(addopts).__name__}"
    return addopts


def test_pytest_addopts_does_not_default_to_verbose():
    """`-v` must not appear in the default pytest addopts.

    Falsifying this means a future edit reintroduced `-v` and would
    silently regress CI by ~5-15s. The Bit-1 RCA finding
    (kb/findings/ci-test-perf-rca-may17.md, umbrella anatomy table)
    is the data source for the saving estimate.
    """
    addopts = _load_addopts()
    assert "-v" not in addopts, (
        f"pyproject `[tool.pytest.ini_options].addopts` contains '-v'. "
        f"CI perf Bit-3 (umbrella `86b9zjtzk`) removed it to drop the "
        f"verbose-reporter cost on CI's ~4500 tests. If a developer "
        f"needs verbose output locally, pass `-v` at the CLI: "
        f"`pytest -v <path>`. addopts was: {addopts!r}"
    )


def test_pytest_addopts_does_not_default_to_verbose_long_form():
    """`--verbose` is the long form of `-v`; same contract applies."""
    addopts = _load_addopts()
    assert "--verbose" not in addopts, (
        f"pyproject addopts contains '--verbose' (long form of '-v'). "
        f"Same contract as test_pytest_addopts_does_not_default_to_verbose. "
        f"addopts was: {addopts!r}"
    )


def test_pytest_addopts_preserves_essential_flags():
    """Bit-3 only removed `-v`. `--tb=short` and `--ignore=venv` must
    remain — they are correctness/output flags, not the perf target.

    `--ignore=venv` is the load-bearing flag per
    `tests/unit/test_makefile.py::test_makefile_ci_symmetry_via_pyproject_addopts`
    (Bit 1.2 + Pillar 5 contract): the Make tier recipes don't pass
    `--ignore=venv` explicitly and rely on addopts injection.
    """
    addopts = _load_addopts()
    for required in ("--tb=short", "--ignore=venv"):
        assert required in addopts, (
            f"Bit-3 was scoped to remove only '-v'; flag {required!r} "
            f"must remain. addopts was: {addopts!r}"
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
