"""Bit-9 (CI perf umbrella 86b9zjtzk) — integration-tier sharding contract.

Bit-7 split the historical single `test` job into 3 parallel GH jobs
(fast-tiers, integration-parallel, integration-serial). Post-Bit-8 cache
measurement showed integration-parallel as the remaining long pole at
~2:08 wall, of which ~113s was xdist test execution on a 2-vCPU runner.

Bit-9 splits `integration-parallel` into TWO hash-balanced shard jobs
via the `pytest-shard` plugin's `--shard-id=N --num-shards=2` flags.
Each shard runs ~half the integration test corpus on its own GH runner,
so the integration-tier wall drops by ~50%.

This contract pins:
  - test.yml has 4 jobs: fast-tiers + integration-shard-0 +
    integration-shard-1 + integration-serial
  - deploy.yml has 5 jobs: same 4 test jobs + deploy
  - Both shard jobs invoke `make test-integration-shard-0` and
    `-shard-1` (positive selection via --shard-id), so the union
    covers the full corpus with no overlap
  - pytest-shard is declared in pyproject.toml [dev] extras
  - The `serial` marker filter is preserved on both shards (each
    shard excludes @serial-marked tests so test-integration-serial
    still owns them)

Status: RED until Bit-9 lands the workflow restructure + Makefile
targets + dep. GREEN after.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

TEST_YML = REPO_ROOT / ".github" / "workflows" / "test.yml"
DEPLOY_YML = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"
MAKEFILE = REPO_ROOT / "Makefile"


@pytest.fixture(scope="module")
def test_yml_parsed():
    import yaml
    return yaml.safe_load(TEST_YML.read_text())


@pytest.fixture(scope="module")
def deploy_yml_parsed():
    import yaml
    return yaml.safe_load(DEPLOY_YML.read_text())


# ─── test.yml ───────────────────────────────────────────────────────────


def test_test_yml_has_four_parallel_jobs(test_yml_parsed):
    """Post-Bit-9 test.yml structure: fast-tiers + 2 integration shards
    + integration-serial = 4 parallel jobs."""
    jobs = set(test_yml_parsed.get("jobs", {}).keys())
    expected = {
        "fast-tiers",
        "integration-shard-0",
        "integration-shard-1",
        "integration-serial",
    }
    assert jobs == expected, (
        f"test.yml jobs drifted from Bit-9 spec.\n"
        f"  expected: {sorted(expected)}\n"
        f"  actual:   {sorted(jobs)}"
    )


def test_test_yml_no_lingering_integration_parallel_job(test_yml_parsed):
    """The legacy `integration-parallel` job name is gone — replaced by
    integration-shard-0 + integration-shard-1.
    """
    assert "integration-parallel" not in test_yml_parsed.get("jobs", {}), (
        "test.yml still has the Bit-7 `integration-parallel` job; "
        "Bit-9 splits it into integration-shard-0 + integration-shard-1."
    )


def test_test_yml_shard_0_invokes_test_integration_shard_0(test_yml_parsed):
    job = test_yml_parsed["jobs"]["integration-shard-0"]
    runs = " ".join(str(s.get("run", "")) for s in job["steps"])
    assert "make test-integration-shard-0" in runs, (
        "test.yml integration-shard-0 must invoke `make test-integration-shard-0`"
    )


def test_test_yml_shard_1_invokes_test_integration_shard_1(test_yml_parsed):
    job = test_yml_parsed["jobs"]["integration-shard-1"]
    runs = " ".join(str(s.get("run", "")) for s in job["steps"])
    assert "make test-integration-shard-1" in runs, (
        "test.yml integration-shard-1 must invoke `make test-integration-shard-1`"
    )


def test_test_yml_both_shards_are_informational(test_yml_parsed):
    """Both integration shards on test.yml inherit the Bit-7 informational
    semantics (continue-on-error)."""
    for job_name in ("integration-shard-0", "integration-shard-1"):
        job = test_yml_parsed["jobs"][job_name]
        tier_steps = [
            s for s in job["steps"]
            if "test-integration-shard" in str(s.get("run", ""))
        ]
        assert tier_steps, (
            f"test.yml job {job_name!r} has no test-integration-shard step"
        )
        for s in tier_steps:
            assert s.get("continue-on-error") is True, (
                f"test.yml {job_name!r} step {s.get('name')!r} must have "
                f"`continue-on-error: true` (informational on test.yml)."
            )


# ─── deploy.yml ─────────────────────────────────────────────────────────


def test_deploy_yml_has_five_jobs(deploy_yml_parsed):
    """Post-Bit-9 deploy.yml: 4 test jobs + deploy = 5 jobs."""
    jobs = set(deploy_yml_parsed.get("jobs", {}).keys())
    expected = {
        "fast-tiers",
        "integration-shard-0",
        "integration-shard-1",
        "integration-serial",
        "deploy",
    }
    assert jobs == expected, (
        f"deploy.yml jobs drifted from Bit-9 spec.\n"
        f"  expected: {sorted(expected)}\n"
        f"  actual:   {sorted(jobs)}"
    )


def test_deploy_yml_deploy_needs_all_four_test_jobs(deploy_yml_parsed):
    """deploy job's `needs:` must list all 4 test jobs so deploy is
    BLOCKING on each."""
    deploy_job = deploy_yml_parsed["jobs"]["deploy"]
    needs = deploy_job.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    expected = {
        "fast-tiers",
        "integration-shard-0",
        "integration-shard-1",
        "integration-serial",
    }
    assert set(needs) == expected, (
        f"deploy.yml deploy job `needs:` drifted from Bit-9 spec.\n"
        f"  expected: {sorted(expected)}\n"
        f"  actual:   {sorted(needs)}"
    )


def test_deploy_yml_shards_are_blocking(deploy_yml_parsed):
    """In deploy.yml both shard jobs must be BLOCKING (no continue-on-error)."""
    for job_name in ("integration-shard-0", "integration-shard-1"):
        job = deploy_yml_parsed["jobs"][job_name]
        tier_steps = [
            s for s in job["steps"]
            if "test-integration-shard" in str(s.get("run", ""))
        ]
        assert tier_steps, (
            f"deploy.yml job {job_name!r} has no test-integration-shard step"
        )
        for s in tier_steps:
            assert not s.get("continue-on-error"), (
                f"deploy.yml {job_name!r} step {s.get('name')!r} has "
                f"continue-on-error — must be BLOCKING on deploy."
            )


# ─── pyproject.toml — pytest-shard dep ──────────────────────────────────


def test_pyproject_has_pytest_shard_in_dev_extras():
    """pytest-shard must be declared in [dev] extras so CI installs it."""
    content = PYPROJECT.read_text()
    # Coarse substring check; sister tests validate finer structure.
    assert "pytest-shard" in content, (
        "pyproject.toml [dev] extras missing pytest-shard (Bit-9 dep)"
    )


# ─── Makefile — shard targets ───────────────────────────────────────────


def test_makefile_has_test_integration_shard_0_target():
    """Makefile must define `test-integration-shard-0` recipe invoking
    pytest with --shard-id=0 --num-shards=2."""
    content = MAKEFILE.read_text()
    assert "test-integration-shard-0:" in content, (
        "Makefile missing test-integration-shard-0 target"
    )
    # Recipe contains pytest-shard flags
    # Locate the recipe block and assert flags within
    lines = content.split("\n")
    in_block = False
    block_lines = []
    for line in lines:
        if line.startswith("test-integration-shard-0:"):
            in_block = True
            continue
        if in_block:
            if line and not line.startswith("\t") and not line.startswith(" "):
                break
            block_lines.append(line)
    block = " ".join(block_lines)
    assert "--shard-id=0" in block, (
        f"test-integration-shard-0 recipe missing `--shard-id=0`. Block: {block!r}"
    )
    assert "--num-shards=2" in block, (
        f"test-integration-shard-0 recipe missing `--num-shards=2`. Block: {block!r}"
    )


def test_makefile_has_test_integration_shard_1_target():
    """Makefile must define `test-integration-shard-1` recipe invoking
    pytest with --shard-id=1 --num-shards=2."""
    content = MAKEFILE.read_text()
    assert "test-integration-shard-1:" in content, (
        "Makefile missing test-integration-shard-1 target"
    )
    lines = content.split("\n")
    in_block = False
    block_lines = []
    for line in lines:
        if line.startswith("test-integration-shard-1:"):
            in_block = True
            continue
        if in_block:
            if line and not line.startswith("\t") and not line.startswith(" "):
                break
            block_lines.append(line)
    block = " ".join(block_lines)
    assert "--shard-id=1" in block, (
        f"test-integration-shard-1 recipe missing `--shard-id=1`. Block: {block!r}"
    )
    assert "--num-shards=2" in block, (
        f"test-integration-shard-1 recipe missing `--num-shards=2`. Block: {block!r}"
    )


def test_makefile_shards_preserve_serial_marker_filter():
    """Both shard recipes must include `not serial` in -m so @serial
    tests don't double-run (they own the test-integration-serial pass)."""
    content = MAKEFILE.read_text()
    for shard in ("0", "1"):
        # Find shard recipe block
        lines = content.split("\n")
        in_block = False
        block_lines = []
        for line in lines:
            if line.startswith(f"test-integration-shard-{shard}:"):
                in_block = True
                continue
            if in_block:
                if line and not line.startswith("\t") and not line.startswith(" "):
                    break
                block_lines.append(line)
        block = " ".join(block_lines)
        assert "not fragile" in block and "not serial" in block, (
            f"test-integration-shard-{shard} recipe must filter `not fragile "
            f"and not serial`. Block: {block!r}"
        )


def test_makefile_test_target_chains_shards():
    """The top-level `test:` target must chain BOTH shards + serial."""
    content = MAKEFILE.read_text()
    lines = content.split("\n")
    in_block = False
    block_lines = []
    for line in lines:
        if line.startswith("test:"):
            in_block = True
            continue
        if in_block:
            if line and not line.startswith("\t") and not line.startswith(" "):
                break
            block_lines.append(line)
    block = " ".join(block_lines)
    assert "test-integration-shard-0" in block, (
        f"`make test` must chain test-integration-shard-0. Block: {block!r}"
    )
    assert "test-integration-shard-1" in block, (
        f"`make test` must chain test-integration-shard-1. Block: {block!r}"
    )
    assert "test-integration-serial" in block, (
        f"`make test` must still chain test-integration-serial. Block: {block!r}"
    )


def test_makefile_legacy_test_integration_target_removed_or_alias():
    """Either remove the old test-integration target OR keep it as an
    alias for both shards. Don't leave a stale recipe that doesn't run
    in CI.

    Accepts two equivalent Make idioms:
      - prerequisites form: ``test-integration: test-integration-shard-0 test-integration-shard-1``
      - recipe form: ``test-integration:`` followed by tabbed recipe lines invoking both shards
    """
    content = MAKEFILE.read_text()
    if "test-integration:" not in content and "test-integration: " not in content:
        return  # cleanly removed
    # Find the target line. Match `test-integration:` followed by either
    # end-of-line OR space (the latter = prerequisites form).
    lines = content.split("\n")
    target_idx = None
    target_line = None
    for i, line in enumerate(lines):
        # Match `test-integration:` exactly OR `test-integration: <deps>`
        # but NOT `test-integration-shard-N:` or `test-integration-serial:`.
        if line == "test-integration:" or line.startswith("test-integration: "):
            target_idx = i
            target_line = line
            break
    assert target_idx is not None, (
        "test-integration target found via substring but no exact line match"
    )
    # Check prerequisites form (deps on same line as target)
    if " " in target_line:
        deps = target_line.split(":", 1)[1].strip()
        assert "test-integration-shard-0" in deps and "test-integration-shard-1" in deps, (
            f"Legacy `test-integration` target uses prerequisites form but missing "
            f"shard deps. Line: {target_line!r}"
        )
        return
    # Otherwise recipe form — read tabbed block
    block_lines = []
    for line in lines[target_idx + 1:]:
        if line and not line.startswith("\t") and not line.startswith(" "):
            break
        block_lines.append(line)
    block = " ".join(block_lines)
    assert ("test-integration-shard-0" in block and
            "test-integration-shard-1" in block), (
        "Legacy `test-integration` target's recipe must invoke both shards. "
        f"Block: {block!r}"
    )
