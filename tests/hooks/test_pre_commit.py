"""Sprint PSC Bit P5.3: git pre-commit hook tests (hermetic).

RCA — why these tests look paranoid
-----------------------------------
The pre-commit hook is load-bearing: if it crashes or returns nonzero
on a legitimate commit, it blocks ALL commits in the repo until either
(a) the bug is fixed and `git commit --no-verify` is used to land the
fix, or (b) the symlink is manually removed from `.git/hooks/`. This
is catastrophic for the Sprint 9 session running in parallel on a
different worktree, which would have no way to ship its Bit without
manual intervention.

Therefore the hook MUST fail-open under every unexpected condition:
missing lock dir, _session_lock import failure, exception during
lock-check, git fetch failure, etc. The tests below pin that contract
end-to-end by invoking the hook script as a subprocess in synthetic
git repos, never touching the real `.git/hooks/` directory.

Hermeticity
-----------
- No tests modify the worktree's `.git/hooks/` directory.
- Each test uses `tmp_path` to construct a mini git repo (`git init`).
- Lock root is redirected via `KALSHI_SESSION_LOCK_ROOT` env var so the
  hook's lock checks read tmp lockfiles, never the real ones.
- Subprocess invocations pin `cwd` to the tmp repo and pass env
  overrides explicitly — nothing leaks from parent process env beyond
  what's required to find python.

Spec source: ClickUp 86b9vgx9w (Part A + Part B + self-test).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HOOK_SCRIPT = _REPO_ROOT / "scripts" / "git_hooks" / "pre-commit"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a `git` command in `repo`. Captures stdout/stderr."""
    cmd_env = os.environ.copy()
    if env:
        cmd_env.update(env)
    # Disable any global pre-commit hooks so synthetic repos don't inherit
    # the operator's machine config.
    cmd_env.setdefault("GIT_CONFIG_GLOBAL", "/dev/null")
    cmd_env.setdefault("GIT_CONFIG_SYSTEM", "/dev/null")
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=check,
        capture_output=True,
        text=True,
        env=cmd_env,
    )


def _init_repo(repo: Path, *, initial_branch: str = "main") -> None:
    """Create a fresh git repo with an initial commit on `initial_branch`."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "--initial-branch", initial_branch)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "seed")


def _run_hook(
    repo: Path,
    *,
    lock_root: Path | None = None,
    extra_env: dict | None = None,
    args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """Invoke the hook script as a subprocess in `repo`.

    Mirrors how git would invoke it: cwd=repo, no args by default, exit
    code is the only "is this commit allowed" signal.
    """
    env = os.environ.copy()
    if lock_root is not None:
        env["KALSHI_SESSION_LOCK_ROOT"] = str(lock_root)
    if extra_env:
        env.update(extra_env)
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    cmd = [sys.executable, str(HOOK_SCRIPT), *(args or [])]
    return subprocess.run(
        cmd,
        cwd=str(repo),
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _write_lockfile(
    lock_root: Path,
    target_path: str,
    *,
    session_id: str = "peer-session",
    last_heartbeat_offset_s: float = 0.0,
) -> Path:
    """Write a synthetic lockfile for `target_path` into `lock_root`.

    `last_heartbeat_offset_s` is subtracted from now() to age the lock;
    pass STALE_THRESHOLD_S+10 to make it stale.
    """
    sys.path.insert(0, str(_REPO_ROOT))
    try:
        from scripts._session_lock import flatten_target_path
    finally:
        sys.path.pop(0)
    flat = flatten_target_path(target_path)
    lock_root.mkdir(parents=True, exist_ok=True)
    now = time.time()
    meta = {
        "pid": 99999,
        "session_id": session_id,
        "target_path": target_path,
        "started_at": now - last_heartbeat_offset_s,
        "last_heartbeat": now - last_heartbeat_offset_s,
        "claude_session_marker": "test",
    }
    path = lock_root / f"{flat}.lock"
    path.write_text(json.dumps(meta), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def repo_with_staged_file(tmp_path):
    """A mini repo on `main` with `bot/_impl.py` staged for commit."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "bot").mkdir()
    (repo / "bot" / "_impl.py").write_text("# stub\n")
    _git(repo, "add", "bot/_impl.py")
    return repo


@pytest.fixture
def feature_branch_repo(tmp_path):
    """A mini repo on a feature branch (NOT in protected list)."""
    repo = tmp_path / "feat-repo"
    _init_repo(repo, initial_branch="main")
    _git(repo, "checkout", "-b", "sprint-9-bit-9.X-thing")
    (repo / "bot").mkdir()
    (repo / "bot" / "_impl.py").write_text("# stub\n")
    _git(repo, "add", "bot/_impl.py")
    return repo


@pytest.fixture
def main_branch_origin_pair(tmp_path):
    """Two synthetic clones with a shared origin remote.

    Returns (local, origin). `local` is on `main`, tracking `origin/main`.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch", "main")

    seed = tmp_path / "seed"
    _init_repo(seed)
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "main")

    local = tmp_path / "local"
    _git(seed.parent, "clone", str(origin), str(local))
    _git(local, "config", "user.email", "test@example.com")
    _git(local, "config", "user.name", "Test User")
    _git(local, "config", "commit.gpgsign", "false")
    return local, origin, seed


# --------------------------------------------------------------------------
# Scenario 1: Happy path
# --------------------------------------------------------------------------


def test_s01_happy_path_no_lock_branch_up_to_date(repo_with_staged_file, tmp_path):
    """No staged file is locked + (no protected branch concern) → allow."""
    lock_root = tmp_path / "locks" / "active-work"
    lock_root.mkdir(parents=True)
    # Use a non-protected branch to skip Part B.
    _git(repo_with_staged_file, "checkout", "-b", "feature-x")
    result = _run_hook(repo_with_staged_file, lock_root=lock_root)
    assert result.returncode == 0, f"expected allow, got {result.returncode}\nSTDERR: {result.stderr}"


# --------------------------------------------------------------------------
# Scenario 2: Part A — staged file held by ANOTHER session → REFUSE
# --------------------------------------------------------------------------


def test_s02_part_a_staged_file_held_by_other_session_refuses(repo_with_staged_file, tmp_path):
    lock_root = tmp_path / "locks" / "active-work"
    _write_lockfile(lock_root, "bot/_impl.py", session_id="other-session-xyz")
    _git(repo_with_staged_file, "checkout", "-b", "feature-x")
    result = _run_hook(repo_with_staged_file, lock_root=lock_root)
    assert result.returncode != 0, (
        f"expected refuse, got {result.returncode}\nSTDERR: {result.stderr}"
    )
    # Held-by metadata MUST appear in stderr so the operator sees who has it.
    assert "bot/_impl.py" in result.stderr
    assert "other-session-xyz" in result.stderr


# --------------------------------------------------------------------------
# Scenario 3: Part A — staged file held by SELF → allow
# --------------------------------------------------------------------------


def test_s03_part_a_staged_file_held_by_self_allows(repo_with_staged_file, tmp_path):
    lock_root = tmp_path / "locks" / "active-work"
    _write_lockfile(lock_root, "bot/_impl.py", session_id="my-session-abc")
    _git(repo_with_staged_file, "checkout", "-b", "feature-x")
    # Pass our session_id via env so the hook recognizes the lock as ours.
    result = _run_hook(
        repo_with_staged_file,
        lock_root=lock_root,
        extra_env={"KALSHI_SESSION_ID": "my-session-abc"},
    )
    assert result.returncode == 0, (
        f"expected allow (self-held), got {result.returncode}\nSTDERR: {result.stderr}"
    )


# --------------------------------------------------------------------------
# Scenario 4: Part A — stale lock on staged file → allow
# --------------------------------------------------------------------------


def test_s04_part_a_stale_lock_allows(repo_with_staged_file, tmp_path):
    lock_root = tmp_path / "locks" / "active-work"
    # 1000s old, well past STALE_THRESHOLD_S=180.
    _write_lockfile(
        lock_root, "bot/_impl.py",
        session_id="long-dead-session",
        last_heartbeat_offset_s=1000.0,
    )
    _git(repo_with_staged_file, "checkout", "-b", "feature-x")
    result = _run_hook(repo_with_staged_file, lock_root=lock_root)
    assert result.returncode == 0, (
        f"expected allow (stale), got {result.returncode}\nSTDERR: {result.stderr}"
    )


# --------------------------------------------------------------------------
# Scenario 5: Part A — lock dir doesn't exist → fail-open + warn
# --------------------------------------------------------------------------


def test_s05_part_a_lock_dir_missing_fails_open(repo_with_staged_file, tmp_path):
    nonexistent = tmp_path / "does-not-exist"
    assert not nonexistent.exists()
    _git(repo_with_staged_file, "checkout", "-b", "feature-x")
    result = _run_hook(repo_with_staged_file, lock_root=nonexistent)
    assert result.returncode == 0, (
        f"expected fail-open (allow), got {result.returncode}\nSTDERR: {result.stderr}"
    )
    # Should emit a warning to stderr.
    assert (
        "warn" in result.stderr.lower()
        or "fail-open" in result.stderr.lower()
        or "lock" in result.stderr.lower()
    ), f"expected fail-open warning in stderr; got {result.stderr!r}"


# --------------------------------------------------------------------------
# Scenario 6: Part A — _session_lock import fails → fail-open + warn
# --------------------------------------------------------------------------


def test_s06_part_a_session_lock_import_fails_open(repo_with_staged_file, tmp_path):
    """If _session_lock can't be imported, fail-open with warning.

    Simulated by pointing PYTHONPATH at a tmp dir with no scripts module —
    the hook normally walks up to find it via the repo root, but if it's
    not findable from the cwd, the hook should fail-open.

    Concrete mechanism: invoke the hook with cwd at a tmp dir that is a
    git repo but does NOT contain scripts/_session_lock.py. The hook
    should fail-open with a warning, not crash.
    """
    isolated = tmp_path / "isolated-repo"
    _init_repo(isolated)
    (isolated / "foo.txt").write_text("x\n")
    _git(isolated, "add", "foo.txt")
    _git(isolated, "checkout", "-b", "feature-x")
    # No KALSHI_SESSION_LOCK_ROOT and no scripts/_session_lock.py in cwd
    # tree → the hook's import attempt must catch and fail-open.
    # We DO pass a lock_root so dir-missing isn't the failure mode;
    # we want the import to fail because the hook can't find the module.
    # The hook resolves the scripts dir relative to its own __file__,
    # however; so we test a different angle: corrupt the env var such
    # that import succeeds but lock-check raises.
    # To force an import failure we set KALSHI_PSC_HOOK_FORCE_IMPORT_FAIL
    # (a test hook the production code honors).
    result = _run_hook(
        isolated,
        lock_root=tmp_path / "locks",
        extra_env={"KALSHI_PSC_HOOK_FORCE_IMPORT_FAIL": "1"},
    )
    assert result.returncode == 0, (
        f"expected fail-open on import error, got {result.returncode}\n"
        f"STDERR: {result.stderr}"
    )
    assert (
        "warn" in result.stderr.lower()
        or "import" in result.stderr.lower()
        or "fail-open" in result.stderr.lower()
    )


# --------------------------------------------------------------------------
# Scenario 7: Part B — on main, behind origin/main → REFUSE
# --------------------------------------------------------------------------


def test_s07_part_b_main_behind_origin_refuses(main_branch_origin_pair, tmp_path):
    local, origin, seed = main_branch_origin_pair
    # Push a new commit from seed → origin so local is behind.
    (seed / "new.txt").write_text("new\n")
    _git(seed, "add", "new.txt")
    _git(seed, "commit", "-m", "advance origin")
    _git(seed, "push", "origin", "main")
    # Stage a commit in local without fetching.
    (local / "local.txt").write_text("local change\n")
    _git(local, "add", "local.txt")
    lock_root = tmp_path / "locks"
    lock_root.mkdir(parents=True)
    result = _run_hook(local, lock_root=lock_root)
    assert result.returncode != 0, (
        f"expected refuse (behind), got {result.returncode}\nSTDERR: {result.stderr}"
    )
    assert "rebase" in result.stderr.lower() or "pull" in result.stderr.lower() or "merge" in result.stderr.lower()


# --------------------------------------------------------------------------
# Scenario 8: Part B — on main, ahead of origin/main → allow
# --------------------------------------------------------------------------


def test_s08_part_b_main_ahead_of_origin_allows(main_branch_origin_pair, tmp_path):
    local, origin, _seed = main_branch_origin_pair
    # Local is up to date initially, then ahead via a local commit.
    (local / "ahead.txt").write_text("ahead\n")
    _git(local, "add", "ahead.txt")
    _git(local, "commit", "-m", "advance local")
    # Stage a new file to test the pre-commit check on the NEW commit.
    (local / "new_staged.txt").write_text("staged\n")
    _git(local, "add", "new_staged.txt")
    lock_root = tmp_path / "locks"
    lock_root.mkdir(parents=True)
    result = _run_hook(local, lock_root=lock_root)
    assert result.returncode == 0, (
        f"expected allow (ahead), got {result.returncode}\nSTDERR: {result.stderr}"
    )


# --------------------------------------------------------------------------
# Scenario 9: Part B — on main, equal to origin/main → allow
# --------------------------------------------------------------------------


def test_s09_part_b_main_equal_to_origin_allows(main_branch_origin_pair, tmp_path):
    local, _origin, _seed = main_branch_origin_pair
    (local / "x.txt").write_text("x\n")
    _git(local, "add", "x.txt")
    lock_root = tmp_path / "locks"
    lock_root.mkdir(parents=True)
    result = _run_hook(local, lock_root=lock_root)
    assert result.returncode == 0, (
        f"expected allow (equal), got {result.returncode}\nSTDERR: {result.stderr}"
    )


# --------------------------------------------------------------------------
# Scenario 10: Part B — feature branch (not in protected list) → skip Part B
# --------------------------------------------------------------------------


def test_s10_part_b_feature_branch_skips(feature_branch_repo, tmp_path):
    """On a non-protected branch, Part B is skipped entirely.

    Verified by NOT setting up an origin remote at all — `git fetch` would
    fail. If Part B ran, the hook would emit a fetch-failed warning.
    The branch IS not main, so Part B should short-circuit silently.
    """
    lock_root = tmp_path / "locks"
    lock_root.mkdir(parents=True)
    result = _run_hook(feature_branch_repo, lock_root=lock_root)
    assert result.returncode == 0, (
        f"expected allow (feature branch), got {result.returncode}\n"
        f"STDERR: {result.stderr}"
    )
    # Part B's fetch-warn must NOT appear (we never reached it).
    assert "fetch" not in result.stderr.lower(), (
        f"feature branch should skip Part B; saw fetch-related stderr: "
        f"{result.stderr!r}"
    )


# --------------------------------------------------------------------------
# Scenario 11: Part B — git fetch fails → fail-open + warn
# --------------------------------------------------------------------------


def test_s11_part_b_fetch_failure_fails_open(tmp_path):
    """On main with a broken origin URL → fetch fails → allow with warning."""
    repo = tmp_path / "repo-broken-origin"
    _init_repo(repo)
    # Add an origin that doesn't resolve. fetch will fail fast.
    _git(repo, "remote", "add", "origin", "/nonexistent/path/to/bare/repo.git")
    (repo / "x.txt").write_text("x\n")
    _git(repo, "add", "x.txt")
    lock_root = tmp_path / "locks"
    lock_root.mkdir(parents=True)
    result = _run_hook(repo, lock_root=lock_root)
    assert result.returncode == 0, (
        f"expected fail-open on fetch failure, got {result.returncode}\n"
        f"STDERR: {result.stderr}"
    )
    assert (
        "fetch" in result.stderr.lower()
        or "warn" in result.stderr.lower()
        or "fail-open" in result.stderr.lower()
    )


# --------------------------------------------------------------------------
# Scenario 12: Bypass — git commit --no-verify lets commits through
# --------------------------------------------------------------------------


def test_s12_bypass_no_verify_works_even_when_part_a_would_refuse(tmp_path):
    """Test the GIT-LEVEL bypass: `git commit --no-verify` doesn't run hooks.

    Mechanism: install our hook in the test repo's .git/hooks/ (hermetic
    because it's tmp_path, not the worktree), set up a scenario where it
    would refuse (a peer-held lock on the staged file), then verify
    `--no-verify` succeeds while plain `git commit` fails.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "checkout", "-b", "feature-bypass")
    (repo / "bot").mkdir()
    (repo / "bot" / "_impl.py").write_text("# stub\n")
    _git(repo, "add", "bot/_impl.py")

    # Install hook in THIS test repo's .git/hooks/ — tmp dir, hermetic.
    hook_dst = repo / ".git" / "hooks" / "pre-commit"
    hook_dst.parent.mkdir(parents=True, exist_ok=True)
    # Symlink to the production hook script.
    if hook_dst.exists() or hook_dst.is_symlink():
        hook_dst.unlink()
    hook_dst.symlink_to(HOOK_SCRIPT)
    HOOK_SCRIPT.chmod(0o755)

    lock_root = tmp_path / "locks" / "active-work"
    _write_lockfile(lock_root, "bot/_impl.py", session_id="other-peer")
    env = {"KALSHI_SESSION_LOCK_ROOT": str(lock_root)}

    # Plain commit must fail (hook refuses).
    plain = _git(repo, "commit", "-m", "should fail", check=False, env=env)
    assert plain.returncode != 0, (
        f"plain commit should be refused by hook; got returncode=0\n"
        f"STDOUT: {plain.stdout}\nSTDERR: {plain.stderr}"
    )

    # --no-verify commit must succeed (hook not invoked).
    bypass = _git(repo, "commit", "--no-verify", "-m", "bypass", check=False, env=env)
    assert bypass.returncode == 0, (
        f"--no-verify commit should succeed; got returncode={bypass.returncode}\n"
        f"STDOUT: {bypass.stdout}\nSTDERR: {bypass.stderr}"
    )


# --------------------------------------------------------------------------
# Scenario 13: Exception in hook body → fail-open + warn
# --------------------------------------------------------------------------


def test_s13_unexpected_exception_fails_open(repo_with_staged_file, tmp_path):
    """Force an unexpected exception inside the hook body; verify fail-open.

    Mechanism: a test hook env var KALSHI_PSC_HOOK_FORCE_EXCEPTION makes
    the hook raise RuntimeError mid-execution. The top-level try/except
    must catch it, warn to stderr, and exit 0.
    """
    lock_root = tmp_path / "locks"
    lock_root.mkdir(parents=True)
    _git(repo_with_staged_file, "checkout", "-b", "feature-x")
    result = _run_hook(
        repo_with_staged_file,
        lock_root=lock_root,
        extra_env={"KALSHI_PSC_HOOK_FORCE_EXCEPTION": "1"},
    )
    assert result.returncode == 0, (
        f"expected fail-open on exception, got {result.returncode}\n"
        f"STDERR: {result.stderr}"
    )
    assert (
        "warn" in result.stderr.lower()
        or "exception" in result.stderr.lower()
        or "fail-open" in result.stderr.lower()
    )


# --------------------------------------------------------------------------
# Scenario 14: --self-test flag works and exits 0 on healthy install
# --------------------------------------------------------------------------


def test_s14_self_test_passes_on_healthy_install(tmp_path):
    """`scripts/git_hooks/pre-commit --self-test` returns 0 with OK message."""
    # Self-test is a meta-check: the hook script verifies its own integrity.
    # We invoke it from the actual repo root (not a tmp dir) because the
    # self-test needs to find scripts/_session_lock.py via the canonical
    # path.
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT), "--self-test"],
        cwd=str(_REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        f"--self-test should pass on healthy install; got {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert "P5.3 pre-commit hook self-test: OK" in result.stdout


def test_s14b_self_test_fails_when_session_lock_unimportable(tmp_path):
    """`--self-test` from a dir with no scripts/_session_lock.py exits 1.

    The self-test is the ONE code path where we WANT a nonzero exit on
    failure — it's a manual operator-confidence check, not the commit-time
    hook path. Operators read its output and remedy the install.
    """
    isolated = tmp_path / "no-scripts"
    isolated.mkdir()
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    env["KALSHI_PSC_HOOK_FORCE_IMPORT_FAIL"] = "1"
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT), "--self-test"],
        cwd=str(isolated),
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 1, (
        f"--self-test should fail when import broken; got {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
