"""Bit-7 + Bit-9 (CI perf umbrella 86b9zjtzk) — pin the parallel-jobs
structure of test.yml and deploy.yml.

Pre-Bit-7, both workflows had a single `test` job with sequential steps:
unit → contract → equivalence → integration → fragile. Integration was
the long pole (~157s on CI); the other tiers (~73s combined) waited.

Bit-7 (2026-05-17) split the workflows into 3 parallel jobs. Bit-9
(2026-05-17) further split integration-parallel into 2 hash-balanced
shards via pytest-shard. Post-Bit-9 structure (4 jobs):

  - `fast-tiers` (BLOCKING)   — unit + contract-pytest + contract-lint
                                + equivalence + fragile.
  - `integration-shard-0`     — `make test-integration-shard-0`
                                (pytest-shard --shard-id=0 --num-shards=2
                                + xdist --dist=loadfile -n auto).
  - `integration-shard-1`     — `make test-integration-shard-1`
                                (pytest-shard --shard-id=1 --num-shards=2
                                + xdist --dist=loadfile -n auto).
  - `integration-serial`      — `make test-integration-serial`.

In test.yml, the 3 integration jobs are INFORMATIONAL (continue-on-error)
matching pre-Bit-7 semantics; fast-tiers is BLOCKING.

In deploy.yml, all 4 jobs are BLOCKING, and the `deploy` job depends
on all 4 via
`needs: [fast-tiers, integration-shard-0, integration-shard-1, integration-serial]`.

Each job independently runs `actions/setup-python@v5` with `cache: 'pip'`
(Bit-2 pip-cache) AND `actions/cache@v4` for `${{ env.pythonLocation }}`
(Bit-8 venv cache).

Status: post-Bit-9 spec; sister contract pin at
`tests/contracts/test_ci_workflows_integration_sharding.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

TEST_YML = REPO_ROOT / ".github" / "workflows" / "test.yml"
DEPLOY_YML = REPO_ROOT / ".github" / "workflows" / "deploy.yml"


@pytest.fixture(scope="module")
def test_yml_parsed():
    import yaml
    return yaml.safe_load(TEST_YML.read_text())


@pytest.fixture(scope="module")
def deploy_yml_parsed():
    import yaml
    return yaml.safe_load(DEPLOY_YML.read_text())


# ─── Section 1 — test.yml job structure ─────────────────────────────────


def test_test_yml_has_four_parallel_jobs(test_yml_parsed):
    """test.yml must define exactly the 4 post-Bit-9 jobs.

    Pre-Bit-7 the file had 1 job ("test"). Bit-7 split into 3 jobs.
    Bit-9 (2026-05-17) further splits integration-parallel into 2
    hash-balanced shards via pytest-shard → 4 total.
    """
    jobs = test_yml_parsed.get("jobs", {})
    expected = {"fast-tiers", "integration-shard-0", "integration-shard-1", "integration-serial"}
    actual = set(jobs.keys())
    assert actual == expected, (
        f"test.yml jobs drifted from Bit-9 spec.\n"
        f"  expected: {sorted(expected)}\n"
        f"  actual:   {sorted(actual)}"
    )


def test_test_yml_each_job_has_setup_python_with_pip_cache(test_yml_parsed):
    """Each parallel job must independently `actions/setup-python@v5`
    with `cache: 'pip'` (Bit-2 pip cache reused per-job).
    """
    jobs = test_yml_parsed.get("jobs", {})
    for job_name, job in jobs.items():
        steps = job.get("steps", [])
        setup_step = next(
            (s for s in steps if "setup-python" in str(s.get("uses", ""))),
            None,
        )
        assert setup_step is not None, (
            f"test.yml job {job_name!r} missing actions/setup-python@v5"
        )
        assert setup_step.get("with", {}).get("cache") == "pip", (
            f"test.yml job {job_name!r} setup-python missing `cache: pip` "
            f"(Bit-2 pip cache reuse per-job). Got: {setup_step.get('with')}"
        )


def test_test_yml_fast_tiers_is_blocking(test_yml_parsed):
    """fast-tiers job's tier steps must NOT have `continue-on-error: true`
    (BLOCKING gate per Bit-7).
    """
    job = test_yml_parsed["jobs"]["fast-tiers"]
    tier_steps = [
        s for s in job["steps"]
        if any(t in str(s.get("run", "")) for t in (
            "test-unit", "test-contract-pytest", "test-contract-lint",
            "test-equivalence",
        ))
    ]
    assert tier_steps, "fast-tiers has no tier steps — Bit-7 spec violated"
    for s in tier_steps:
        assert not s.get("continue-on-error"), (
            f"fast-tiers step {s.get('name')!r} has continue-on-error — "
            f"must be BLOCKING per Bit-7."
        )


def test_test_yml_integration_jobs_are_informational(test_yml_parsed):
    """Both integration jobs in test.yml must have `continue-on-error: true`
    (informational, matches pre-Bit-7 test.yml semantics).
    """
    for job_name in ("integration-shard-0", "integration-shard-1", "integration-serial"):
        job = test_yml_parsed["jobs"][job_name]
        tier_steps = [
            s for s in job["steps"]
            if "test-integration" in str(s.get("run", ""))
        ]
        assert tier_steps, (
            f"test.yml job {job_name!r} has no test-integration step"
        )
        for s in tier_steps:
            assert s.get("continue-on-error") is True, (
                f"test.yml {job_name!r} step {s.get('name')!r} must have "
                f"`continue-on-error: true` (informational on test.yml)."
            )


def test_test_yml_integration_shard_jobs_invoke_their_targets(test_yml_parsed):
    """Each integration-shard-N job must invoke its specific
    `make test-integration-shard-N` target. Bit-9 (2026-05-17) replaced
    the single integration-parallel job with two shard jobs.
    """
    for shard in ("0", "1"):
        job = test_yml_parsed["jobs"][f"integration-shard-{shard}"]
        runs = " ".join(str(s.get("run", "")) for s in job["steps"])
        assert f"make test-integration-shard-{shard}" in runs, (
            f"integration-shard-{shard} must invoke `make test-integration-shard-{shard}`"
        )
        assert "test-integration-serial" not in runs, (
            f"integration-shard-{shard} must NOT invoke test-integration-serial "
            "(serial pass has its own job)."
        )


def test_test_yml_integration_serial_invokes_make_test_integration_serial(test_yml_parsed):
    """integration-serial must invoke `make test-integration-serial` ONLY."""
    job = test_yml_parsed["jobs"]["integration-serial"]
    runs = " ".join(str(s.get("run", "")) for s in job["steps"])
    assert "make test-integration-serial" in runs, (
        "integration-serial must invoke `make test-integration-serial`"
    )


# ─── Section 2 — deploy.yml job structure ────────────────────────────────


def test_deploy_yml_has_four_test_jobs_plus_deploy(deploy_yml_parsed):
    """deploy.yml must have the 4 post-Bit-9 test jobs + deploy job.

    Bit-7 split `test` into fast-tiers + integration-parallel +
    integration-serial. Bit-9 (2026-05-17) further split integration-
    parallel into 2 hash-balanced shards → 4 test jobs + deploy = 5.
    """
    jobs = deploy_yml_parsed.get("jobs", {})
    expected = {
        "fast-tiers", "integration-shard-0", "integration-shard-1",
        "integration-serial", "deploy",
    }
    actual = set(jobs.keys())
    assert actual == expected, (
        f"deploy.yml jobs drifted from Bit-9 spec.\n"
        f"  expected: {sorted(expected)}\n"
        f"  actual:   {sorted(actual)}"
    )


def test_deploy_yml_deploy_needs_all_four_test_jobs(deploy_yml_parsed):
    """deploy job's `needs:` must list all 4 test jobs so deploy is
    BLOCKING on each. Bit-9 raised count 3 → 4 (two integration shards).
    """
    deploy_job = deploy_yml_parsed["jobs"]["deploy"]
    needs = deploy_job.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    expected = {
        "fast-tiers", "integration-shard-0", "integration-shard-1",
        "integration-serial",
    }
    assert set(needs) == expected, (
        f"deploy.yml deploy job `needs:` drifted from Bit-9 spec.\n"
        f"  expected: {sorted(expected)}\n"
        f"  actual:   {sorted(needs)}"
    )


def test_deploy_yml_integration_jobs_are_blocking(deploy_yml_parsed):
    """In deploy.yml, both integration jobs must be BLOCKING (NO
    `continue-on-error: true`). Deploy is the higher-stakes gate.
    """
    for job_name in ("integration-shard-0", "integration-shard-1", "integration-serial"):
        job = deploy_yml_parsed["jobs"][job_name]
        tier_steps = [
            s for s in job["steps"]
            if "test-integration" in str(s.get("run", ""))
        ]
        assert tier_steps, (
            f"deploy.yml job {job_name!r} has no test-integration step"
        )
        for s in tier_steps:
            assert not s.get("continue-on-error"), (
                f"deploy.yml {job_name!r} step {s.get('name')!r} "
                f"has continue-on-error — must be BLOCKING on deploy."
            )


def test_deploy_yml_each_test_job_has_setup_python_with_pip_cache(deploy_yml_parsed):
    """deploy.yml's 4 test jobs (post-Bit-9) each independently
    `actions/setup-python@v5` with `cache: 'pip'`. Same per-job-cache-
    reuse pattern as test.yml.
    """
    for job_name in ("fast-tiers", "integration-shard-0", "integration-shard-1", "integration-serial"):
        job = deploy_yml_parsed["jobs"][job_name]
        steps = job.get("steps", [])
        setup_step = next(
            (s for s in steps if "setup-python" in str(s.get("uses", ""))),
            None,
        )
        assert setup_step is not None, (
            f"deploy.yml job {job_name!r} missing actions/setup-python@v5"
        )
        assert setup_step.get("with", {}).get("cache") == "pip", (
            f"deploy.yml job {job_name!r} setup-python missing `cache: pip`"
        )
