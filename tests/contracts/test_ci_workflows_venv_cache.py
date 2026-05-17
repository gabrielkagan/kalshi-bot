"""Bit-8 (CI perf umbrella 86b9zjtzk) — venv cache pinning contract.

Bit-2 added pip's wheel/download cache via setup-python@v5's
``cache: 'pip'``. That saves wheel-download time but NOT the wheel
install / setuptools build time — each Bit-7 parallel job still pays
~29s for ``pip install -r requirements.txt && pip install -e '.[dev]'``.

Bit-8 adds ``actions/cache@v4`` for ``${{ env.pythonLocation }}`` —
the Python install path that setup-python writes site-packages into.
On cache hit, the subsequent ``pip install`` sees packages already
present at correct versions → no-op (~3-5s). On miss, the install
runs and populates the cache for next run.

Cache key hashes ``requirements.txt + pyproject.toml`` so any dep
change busts the cache.

Status: RED until Bit-8 lands the cache steps. GREEN after.
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


def _cache_step(job: dict) -> dict | None:
    """Find the actions/cache@v4 step (Bit-8) in a job's steps list."""
    for step in job.get("steps", []):
        uses = str(step.get("uses", ""))
        if uses.startswith("actions/cache@"):
            return step
    return None


# ─── test.yml ───────────────────────────────────────────────────────────


def test_test_yml_fast_tiers_has_venv_cache(test_yml_parsed):
    """fast-tiers job must have actions/cache@v4 step caching the
    Python install location.

    Bit-8: this caches the installed site-packages between runs so
    `pip install` becomes a no-op on cache hit (~29s → ~3-5s saving).
    """
    job = test_yml_parsed["jobs"]["fast-tiers"]
    cache_step = _cache_step(job)
    assert cache_step is not None, (
        "test.yml fast-tiers job missing actions/cache@v4 step (Bit-8 venv cache)"
    )
    assert cache_step["uses"].startswith("actions/cache@v4"), (
        f"test.yml fast-tiers cache step uses {cache_step['uses']}; "
        "Bit-8 requires actions/cache@v4 (v3 cache key format incompatible)"
    )
    with_cfg = cache_step.get("with", {})
    path = str(with_cfg.get("path", ""))
    assert "pythonLocation" in path, (
        f"test.yml fast-tiers cache `path` must include "
        f"${{{{ env.pythonLocation }}}}; got {path!r}"
    )
    key = str(with_cfg.get("key", ""))
    assert "hashFiles" in key and "requirements.txt" in key and "pyproject.toml" in key, (
        f"test.yml fast-tiers cache `key` must hashFiles requirements.txt + "
        f"pyproject.toml so dep changes bust the cache; got {key!r}"
    )


def test_test_yml_integration_shards_have_venv_cache(test_yml_parsed):
    """Both Bit-9 integration shards must have actions/cache@v4 (venv cache)."""
    for shard in ("0", "1"):
        job = test_yml_parsed["jobs"][f"integration-shard-{shard}"]
        cache_step = _cache_step(job)
        assert cache_step is not None, (
            f"test.yml integration-shard-{shard} missing actions/cache@v4 (Bit-8)"
        )
        assert "pythonLocation" in str(cache_step["with"]["path"])
        assert "hashFiles" in str(cache_step["with"]["key"])


def test_test_yml_integration_serial_has_venv_cache(test_yml_parsed):
    job = test_yml_parsed["jobs"]["integration-serial"]
    cache_step = _cache_step(job)
    assert cache_step is not None, (
        "test.yml integration-serial missing actions/cache@v4 (Bit-8)"
    )
    assert "pythonLocation" in str(cache_step["with"]["path"])
    assert "hashFiles" in str(cache_step["with"]["key"])


# ─── deploy.yml ─────────────────────────────────────────────────────────


def test_deploy_yml_fast_tiers_has_venv_cache(deploy_yml_parsed):
    job = deploy_yml_parsed["jobs"]["fast-tiers"]
    cache_step = _cache_step(job)
    assert cache_step is not None, (
        "deploy.yml fast-tiers missing actions/cache@v4 (Bit-8)"
    )
    assert "pythonLocation" in str(cache_step["with"]["path"])
    assert "hashFiles" in str(cache_step["with"]["key"])


def test_deploy_yml_integration_shards_have_venv_cache(deploy_yml_parsed):
    """Both Bit-9 integration shards in deploy.yml must have venv cache."""
    for shard in ("0", "1"):
        job = deploy_yml_parsed["jobs"][f"integration-shard-{shard}"]
        cache_step = _cache_step(job)
        assert cache_step is not None, (
            f"deploy.yml integration-shard-{shard} missing actions/cache@v4 (Bit-8)"
        )
        assert "pythonLocation" in str(cache_step["with"]["path"])
        assert "hashFiles" in str(cache_step["with"]["key"])


def test_deploy_yml_integration_serial_has_venv_cache(deploy_yml_parsed):
    job = deploy_yml_parsed["jobs"]["integration-serial"]
    cache_step = _cache_step(job)
    assert cache_step is not None, (
        "deploy.yml integration-serial missing actions/cache@v4 (Bit-8)"
    )
    assert "pythonLocation" in str(cache_step["with"]["path"])
    assert "hashFiles" in str(cache_step["with"]["key"])


# ─── Symmetry pins ───────────────────────────────────────────────────────


def test_all_cache_keys_consistent_across_jobs(test_yml_parsed, deploy_yml_parsed):
    """All 8 jobs (4 in test.yml + 4 in deploy.yml — Bit-9 raised count
    from 6 to 8 by sharding integration-parallel) must use IDENTICAL
    cache keys so they share the same cache entry across workflows.

    Without this: a fresh cache fill in test.yml wouldn't be reusable
    by deploy.yml (different key → different cache namespace), doubling
    the cold-cache install cost on every dep change.
    """
    keys = []
    for jobs in (test_yml_parsed["jobs"], deploy_yml_parsed["jobs"]):
        for job_name in ("fast-tiers", "integration-shard-0", "integration-shard-1", "integration-serial"):
            cache = _cache_step(jobs[job_name])
            keys.append(cache["with"]["key"])
    assert len(set(keys)) == 1, (
        f"Cache keys must be identical across all 8 jobs for cross-workflow reuse. "
        f"Got {len(set(keys))} distinct keys: {set(keys)}"
    )


def test_cache_step_runs_before_install_dependencies(test_yml_parsed, deploy_yml_parsed):
    """The actions/cache step must run BEFORE the install step so the
    cache restore (if any) is in place when pip install runs.
    """
    for workflow_name, parsed in [("test.yml", test_yml_parsed), ("deploy.yml", deploy_yml_parsed)]:
        for job_name in ("fast-tiers", "integration-shard-0", "integration-shard-1", "integration-serial"):
            steps = parsed["jobs"][job_name]["steps"]
            cache_idx = None
            install_idx = None
            for i, step in enumerate(steps):
                if str(step.get("uses", "")).startswith("actions/cache@"):
                    cache_idx = i
                if "pip install" in str(step.get("run", "")):
                    install_idx = i
                    break  # first install step is the one to gate
            assert cache_idx is not None, (
                f"{workflow_name} {job_name}: no actions/cache step found"
            )
            assert install_idx is not None, (
                f"{workflow_name} {job_name}: no pip install step found"
            )
            assert cache_idx < install_idx, (
                f"{workflow_name} {job_name}: cache step (idx={cache_idx}) "
                f"must precede install step (idx={install_idx})"
            )
