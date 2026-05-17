"""CI perf Bit-2 (umbrella `86b9zjtzk`, this Bit `86b9zju0d`, 2026-05-17).

Asserts that `.github/workflows/test.yml` and `.github/workflows/deploy.yml`
configure `actions/setup-python` with `cache: 'pip'` AND with a
`cache-dependency-path` that covers BOTH `requirements.txt` AND
`pyproject.toml`.

Why both files in `cache-dependency-path`:
  - `actions/setup-python@v5` defaults the pip cache key to a hash of
    `**/requirements.txt` — well-defined, but NARROW. With
    `[project.optional-dependencies].dev` in pyproject (used via
    `pip install -e '.[dev]'`), a pyproject-only dep bump would NOT
    bust the cache and CI could install stale deps.
  - Explicit `cache-dependency-path: requirements.txt\npyproject.toml`
    forces BOTH file hashes into the cache key so a change in either
    invalidates.

Why this is a contract, not a preference:
  - Umbrella's anatomy table (5:41 GH Actions run, May 17 2026)
    attributes 34s / 10 % of total CI time to the cold pip install.
    Bit-2 estimates ~20-25s saving by caching the pip download +
    wheel build cache (extracted from the GitHub-hosted runners'
    ephemeral disk to GitHub's actions cache).
  - The whitepaper workflow (`.github/workflows/whitepaper.yml`) uses
    `setup-python@v6` with python 3.12 on a cron trigger and is
    explicitly OUT OF SCOPE per the umbrella (not on the PR critical
    path; its pip-install cost does not affect PR wall time).

Cross-test-file note:
  This test parses the workflow YAMLs and walks the `steps:` list to
  find the `actions/setup-python` entry. It does NOT regex-match the
  raw YAML text, because indentation drift would silently pass a
  regex while breaking the actual workflow.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEST_YML = REPO_ROOT / ".github" / "workflows" / "test.yml"
DEPLOY_YML = REPO_ROOT / ".github" / "workflows" / "deploy.yml"


def _load_yaml(path: Path) -> dict:
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        pytest.skip("pyyaml not installed (transitive dep — installed via [dev]).")
    with path.open() as f:
        return yaml.safe_load(f)


def _find_setup_python_with_block(workflow: dict) -> dict:
    """Return the `with:` dict of the FIRST `uses: actions/setup-python@*`
    step found in ANY job. Raises AssertionError if no such step exists.

    Searches all jobs because deploy.yml has 2 jobs (`test` + `deploy`)
    where only `test` calls setup-python; test.yml has 1 job. If a
    future edit adds a SECOND setup-python step (e.g. python 3.12 for
    a docs job), this helper returns the first match and the test
    surface should be widened to enumerate all matches.
    """
    jobs = workflow.get("jobs") or {}
    assert jobs, "workflow has no `jobs:` block"
    for job in jobs.values():
        steps = job.get("steps") or []
        for step in steps:
            uses = step.get("uses", "")
            if uses.startswith("actions/setup-python@"):
                return step.get("with") or {}
    raise AssertionError(
        f"no `uses: actions/setup-python@*` step found in any job. "
        f"Bit-2 requires setup-python to declare `cache: 'pip'` and "
        f"`cache-dependency-path`."
    )


@pytest.mark.parametrize(
    "workflow_path,label",
    [
        (TEST_YML, "test.yml"),
        (DEPLOY_YML, "deploy.yml"),
    ],
)
def test_workflow_setup_python_declares_pip_cache(workflow_path: Path, label: str):
    """`cache: 'pip'` must be set on `actions/setup-python` so the
    pip download + wheel build artifacts are cached across runs.
    """
    workflow = _load_yaml(workflow_path)
    with_block = _find_setup_python_with_block(workflow)
    assert with_block.get("cache") == "pip", (
        f"{label}: actions/setup-python `with` block missing "
        f"`cache: 'pip'` (or it has a different value). CI perf Bit-2 "
        f"(umbrella `86b9zjtzk`) requires it for the ~20-25s pip-install "
        f"saving. with-block was: {with_block!r}"
    )


@pytest.mark.parametrize(
    "workflow_path,label",
    [
        (TEST_YML, "test.yml"),
        (DEPLOY_YML, "deploy.yml"),
    ],
)
def test_workflow_setup_python_cache_dependency_path_covers_both_files(
    workflow_path: Path, label: str
):
    """Both `requirements.txt` AND `pyproject.toml` must appear in
    `cache-dependency-path`, so a dep change in EITHER file invalidates
    the cache. Without this, a `[dev]` extras change in pyproject would
    NOT bust the cache and CI could install stale deps.
    """
    workflow = _load_yaml(workflow_path)
    with_block = _find_setup_python_with_block(workflow)
    raw = with_block.get("cache-dependency-path")
    assert raw is not None, (
        f"{label}: actions/setup-python `with` block missing "
        f"`cache-dependency-path`. Bit-2 requires it explicit to defend "
        f"against auto-discovery picking only one of requirements.txt / "
        f"pyproject.toml. with-block was: {with_block!r}"
    )
    # YAML's | / >- block-scalar may produce a multi-line string; the
    # list form is also valid. Normalize to a set of stripped path entries.
    if isinstance(raw, str):
        entries = {line.strip() for line in raw.splitlines() if line.strip()}
    elif isinstance(raw, list):
        entries = {str(e).strip() for e in raw}
    else:
        raise AssertionError(
            f"{label}: cache-dependency-path unexpected type "
            f"{type(raw).__name__}: {raw!r}"
        )
    for required in ("requirements.txt", "pyproject.toml"):
        assert required in entries, (
            f"{label}: cache-dependency-path must list {required!r}. "
            f"Got entries: {entries!r}"
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
