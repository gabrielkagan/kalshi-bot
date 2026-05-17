"""Bit-7 (CI perf umbrella 86b9zjtzk) — pin the parallel-jobs structure
of test.yml and deploy.yml.

Pre-Bit-7, both workflows had a single `test` job with sequential steps:
unit → contract → equivalence → integration → fragile. Integration was
the long pole (~157s on CI); the other tiers (~73s combined) waited.

Bit-7 splits the workflows into 3 parallel jobs:

  - `fast-tiers` (BLOCKING)   — unit + contract-pytest + contract-lint
                                + equivalence + fragile.
  - `integration-parallel`    — `make test-integration` (xdist-n-auto).
  - `integration-serial`      — `make test-integration-serial`.

In test.yml, the 2 integration jobs are INFORMATIONAL (continue-on-error)
matching pre-Bit-7 semantics; fast-tiers is BLOCKING.

In deploy.yml, all 3 jobs are BLOCKING, and the `deploy` job depends on
all 3 via `needs: [fast-tiers, integration-parallel, integration-serial]`.

Each job independently runs `actions/setup-python@v5` with `cache: 'pip'`
(Bit-2 pip-cache reused per-job).

Status: RED until Bit-7 lands the restructure. GoesGREEN after.
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


def test_test_yml_has_three_parallel_jobs(test_yml_parsed):
    """test.yml must define exactly the 3 Bit-7 jobs.

    Pre-Bit-7 the file had 1 job ("test"). Bit-7 splits into 3 named
    jobs so the long-pole integration tier doesn't gate the fast tiers.
    """
    jobs = test_yml_parsed.get("jobs", {})
    expected = {"fast-tiers", "integration-parallel", "integration-serial"}
    actual = set(jobs.keys())
    assert actual == expected, (
        f"test.yml jobs drifted from Bit-7 spec.\n"
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
    for job_name in ("integration-parallel", "integration-serial"):
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


def test_test_yml_integration_parallel_invokes_make_test_integration(test_yml_parsed):
    """integration-parallel must invoke `make test-integration` (the
    xdist-parallel target), NOT chain with serial.
    """
    job = test_yml_parsed["jobs"]["integration-parallel"]
    runs = " ".join(str(s.get("run", "")) for s in job["steps"])
    assert "make test-integration" in runs, (
        "integration-parallel must invoke `make test-integration`"
    )
    assert "test-integration-serial" not in runs, (
        "integration-parallel must NOT invoke test-integration-serial "
        "(it has its own job per Bit-7)."
    )


def test_test_yml_integration_serial_invokes_make_test_integration_serial(test_yml_parsed):
    """integration-serial must invoke `make test-integration-serial` ONLY."""
    job = test_yml_parsed["jobs"]["integration-serial"]
    runs = " ".join(str(s.get("run", "")) for s in job["steps"])
    assert "make test-integration-serial" in runs, (
        "integration-serial must invoke `make test-integration-serial`"
    )


# ─── Section 2 — deploy.yml job structure ────────────────────────────────


def test_deploy_yml_has_three_test_jobs_plus_deploy(deploy_yml_parsed):
    """deploy.yml must have the same 3 test jobs + the deploy job.

    Pre-Bit-7 deploy.yml had `test` + `deploy`. Bit-7 splits `test` into
    fast-tiers + integration-parallel + integration-serial; `deploy`
    needs all 3.
    """
    jobs = deploy_yml_parsed.get("jobs", {})
    expected = {
        "fast-tiers", "integration-parallel", "integration-serial", "deploy",
    }
    actual = set(jobs.keys())
    assert actual == expected, (
        f"deploy.yml jobs drifted from Bit-7 spec.\n"
        f"  expected: {sorted(expected)}\n"
        f"  actual:   {sorted(actual)}"
    )


def test_deploy_yml_deploy_needs_all_three_test_jobs(deploy_yml_parsed):
    """deploy job's `needs:` must list all 3 test jobs so deploy is
    BLOCKING on each.
    """
    deploy_job = deploy_yml_parsed["jobs"]["deploy"]
    needs = deploy_job.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    expected = {"fast-tiers", "integration-parallel", "integration-serial"}
    assert set(needs) == expected, (
        f"deploy.yml deploy job `needs:` drifted from Bit-7 spec.\n"
        f"  expected: {sorted(expected)}\n"
        f"  actual:   {sorted(needs)}"
    )


def test_deploy_yml_integration_jobs_are_blocking(deploy_yml_parsed):
    """In deploy.yml, both integration jobs must be BLOCKING (NO
    `continue-on-error: true`). Deploy is the higher-stakes gate.
    """
    for job_name in ("integration-parallel", "integration-serial"):
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
    """deploy.yml's 3 test jobs each independently `actions/setup-python@v5`
    with `cache: 'pip'`. Same per-job-cache-reuse pattern as test.yml.
    """
    for job_name in ("fast-tiers", "integration-parallel", "integration-serial"):
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
