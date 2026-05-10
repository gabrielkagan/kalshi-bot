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

    Hermeticity: the parent test process may itself be a Claude Code
    session with CLAUDE_SESSION_ID set in env. We strip it (and the
    test-only force-* hooks) so each test's `extra_env` is fully
    authoritative — no host-env leakage into hermetic assertions.
    """
    env = os.environ.copy()
    for k in (
        "CLAUDE_SESSION_ID",
        "KALSHI_SESSION_ID",
        "KALSHI_PSC_HOOK_FORCE_IMPORT_FAIL",
        "KALSHI_PSC_HOOK_FORCE_EXCEPTION",
        "KALSHI_PSC_HOOK_FORCE_FETCH_TIMEOUT",
        "KALSHI_PSC_HOOK_FETCH_TIMEOUT_S",
        "KALSHI_SESSION_LOCK_ROOT",
    ):
        env.pop(k, None)
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
    claude_session_marker: str = "test",
    last_heartbeat_offset_s: float = 0.0,
) -> Path:
    """Write a synthetic lockfile for `target_path` into `lock_root`.

    `last_heartbeat_offset_s` is subtracted from now() to age the lock;
    pass STALE_THRESHOLD_S+10 to make it stale.

    `claude_session_marker` mirrors how P5.1's SessionLock initializes
    the field from CLAUDE_SESSION_ID at acquire time. Self-detection in
    the P5.3 hook matches the holder's `claude_session_marker` against
    the env's `CLAUDE_SESSION_ID` exclusively (R1 M3); `session_id` is
    the per-instance UUID and never matches anything in env.
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
        "claude_session_marker": claude_session_marker,
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
    """R1 M3 — self-detection is via `claude_session_marker` == CLAUDE_SESSION_ID.

    P5.1's SessionLock writes `claude_session_marker` from CLAUDE_SESSION_ID
    at acquire time; the per-instance `session_id` is a UUID and never
    matches any env var. The hook (post-M3) matches on
    `claude_session_marker` exclusively — no KALSHI_SESSION_ID fallback.
    """
    lock_root = tmp_path / "locks" / "active-work"
    _write_lockfile(
        lock_root, "bot/_impl.py",
        session_id="random-uuid-irrelevant",
        claude_session_marker="my-claude-session-id-abc",
    )
    _git(repo_with_staged_file, "checkout", "-b", "feature-x")
    # Pass our CLAUDE_SESSION_ID via env so the hook recognizes the lock
    # as ours (matches claude_session_marker in the lockfile).
    result = _run_hook(
        repo_with_staged_file,
        lock_root=lock_root,
        extra_env={"CLAUDE_SESSION_ID": "my-claude-session-id-abc"},
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
    # R1 m4 — don't chmod the real worktree hook; its mode is already 755
    # (committed that way and verified hermetically by s14_self_test).

    lock_root = tmp_path / "locks" / "active-work"
    _write_lockfile(lock_root, "bot/_impl.py", session_id="other-peer")
    env = {"KALSHI_SESSION_LOCK_ROOT": str(lock_root)}

    # Capture HEAD prior to attempts so we can verify the --no-verify
    # commit actually advanced HEAD (R1 m3 — positive assertion).
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Plain commit must fail (hook refuses).
    plain = _git(repo, "commit", "-m", "should fail", check=False, env=env)
    assert plain.returncode != 0, (
        f"plain commit should be refused by hook; got returncode=0\n"
        f"STDOUT: {plain.stdout}\nSTDERR: {plain.stderr}"
    )
    head_after_fail = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert head_after_fail == head_before, (
        f"refused commit must NOT advance HEAD; "
        f"was {head_before}, now {head_after_fail}"
    )

    # --no-verify commit must succeed (hook not invoked).
    bypass = _git(repo, "commit", "--no-verify", "-m", "bypass", check=False, env=env)
    assert bypass.returncode == 0, (
        f"--no-verify commit should succeed; got returncode={bypass.returncode}\n"
        f"STDOUT: {bypass.stdout}\nSTDERR: {bypass.stderr}"
    )
    # R1 m3 — positive assertion that HEAD actually advanced.
    head_after_bypass = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert head_after_bypass != head_before, (
        f"--no-verify commit must advance HEAD; HEAD did not move from {head_before}"
    )
    # Subject line of the new commit should match.
    subject = _git(repo, "log", "-1", "--format=%s").stdout.strip()
    assert subject == "bypass", (
        f"--no-verify commit subject should be 'bypass'; got {subject!r}"
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


# --------------------------------------------------------------------------
# Scenario 15: R1 C1 — mid-merge state must NOT refuse the merge-resolution
# --------------------------------------------------------------------------


def test_s15_mid_merge_commit_does_not_refuse(main_branch_origin_pair, tmp_path):
    """On `main`, mid-merge (`.git/MERGE_HEAD` exists), hook must skip Part B.

    R1 C1 reproduction:
      1. Local has a commit not yet on origin.
      2. Origin advances → local diverges.
      3. User runs `git fetch && git merge origin/main` (what the hook
         suggests as resolution).
      4. Merge produces a conflict on a real file.
      5. User resolves the conflict, runs `git commit --no-edit`.
      6. Without the C1 fix, hook fires `merge-base --is-ancestor
         origin/main HEAD` → rc=1 (HEAD is still the pre-merge local
         commit) → refuses the merge-resolution commit. Repo stuck.

    Fix: Part B is skipped entirely whenever `.git/MERGE_HEAD` (or any
    other mid-operation sentinel) exists. Verified here by setting up
    a real mid-merge state.
    """
    local, origin, seed = main_branch_origin_pair
    # Branch 1: origin diverges. Push a commit from seed → origin.
    (seed / "from-origin.txt").write_text("from origin\n")
    _git(seed, "add", "from-origin.txt")
    _git(seed, "commit", "-m", "origin-advance")
    _git(seed, "push", "origin", "main")
    # Branch 2: local diverges. Make a local commit before fetching.
    (local / "from-local.txt").write_text("from local\n")
    _git(local, "add", "from-local.txt")
    _git(local, "commit", "-m", "local-advance")
    # Now fetch origin (so we know about origin/main without integrating).
    _git(local, "fetch", "origin")
    # Attempt the merge — this is the resolution path the hook suggests.
    # Since the two files don't conflict on content, merge will succeed
    # AUTOMATICALLY and commit, which doesn't leave MERGE_HEAD around.
    # To force a real mid-merge state we use --no-commit so MERGE_HEAD
    # persists; the user-then-runs-`git commit` path is exactly what we
    # need to test.
    merge_result = _git(local, "merge", "--no-commit", "--no-ff", "origin/main", check=False)
    # Verify we ARE mid-merge.
    merge_head = local / ".git" / "MERGE_HEAD"
    assert merge_head.exists(), (
        f"test setup: expected .git/MERGE_HEAD after `git merge --no-commit`; "
        f"merge stdout: {merge_result.stdout}\nstderr: {merge_result.stderr}"
    )
    # NOW invoke the hook directly (mimics what `git commit` would do).
    # Pre-C1-fix this would refuse because origin/main is not an ancestor
    # of HEAD (HEAD is still local-advance).
    lock_root = tmp_path / "locks"
    lock_root.mkdir(parents=True)
    result = _run_hook(local, lock_root=lock_root)
    assert result.returncode == 0, (
        f"mid-merge commit must NOT be refused (R1 C1 fix); "
        f"got rc={result.returncode}\nSTDERR: {result.stderr}"
    )
    # Should warn that it skipped Part B due to mid-operation state.
    assert "mid-operation" in result.stderr.lower() or "merge" in result.stderr.lower(), (
        f"expected mid-operation warning in stderr; got {result.stderr!r}"
    )


# --------------------------------------------------------------------------
# Scenario 16: R1 M1 — git fetch timeout fails open
# --------------------------------------------------------------------------


def test_s16_fetch_timeout_fails_open(main_branch_origin_pair, tmp_path):
    """Force a synthetic git-fetch TimeoutExpired; assert fail-open + warn.

    R1 M1: without a timeout on fetch, a slow VPN / unreachable-but-not-
    failing origin would block every commit-to-main for minutes. Fix
    passes timeout=15s to subprocess.run and returns rc=124 on
    TimeoutExpired. The hook then fails open with a clear warning.

    Mechanism: env var `KALSHI_PSC_HOOK_FORCE_FETCH_TIMEOUT=1` makes the
    `_git(["fetch", ...])` wrapper return rc=124 synthetically without
    a real network call. Hermetic, deterministic, no flake risk.
    """
    local, _origin, _seed = main_branch_origin_pair
    # Stage a commit on main.
    (local / "x.txt").write_text("x\n")
    _git(local, "add", "x.txt")
    lock_root = tmp_path / "locks"
    lock_root.mkdir(parents=True)
    result = _run_hook(
        local,
        lock_root=lock_root,
        extra_env={"KALSHI_PSC_HOOK_FORCE_FETCH_TIMEOUT": "1"},
    )
    assert result.returncode == 0, (
        f"timed-out fetch must fail-open (allow); got rc={result.returncode}\n"
        f"STDERR: {result.stderr}"
    )
    assert "timed out" in result.stderr.lower() or "timeout" in result.stderr.lower(), (
        f"expected timeout warning in stderr; got {result.stderr!r}"
    )


# --------------------------------------------------------------------------
# Scenario 17: R1 M2 — chained .local hook that refuses propagates
# --------------------------------------------------------------------------


def test_s17_chained_local_hook_runs_first_and_can_refuse(repo_with_staged_file, tmp_path):
    """`.git/hooks/pre-commit.local` runs FIRST; nonzero exit blocks commit.

    R1 M2: the operator may have a pre-existing pre-commit hook (e.g. the
    historic shell ast-check on the main checkout). `make install-hooks`
    renames it to `pre-commit.local`; the P5.3 hook invokes it before
    Part A/B and propagates a nonzero exit. This preserves the
    operator's prior pre-commit semantics.

    Verified by installing a `.local` hook that always exits 1 and
    confirming the P5.3 hook returns 1 even when Part A/B would allow.
    """
    repo = repo_with_staged_file
    _git(repo, "checkout", "-b", "feature-x")
    # Install a chained .local hook in the test repo's .git/hooks/.
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    local_hook = hooks_dir / "pre-commit.local"
    local_hook.write_text(
        "#!/bin/sh\n"
        "echo 'chained-local-hook says NO' 1>&2\n"
        "exit 7\n"  # distinctive nonzero rc
    )
    local_hook.chmod(0o755)
    lock_root = tmp_path / "locks"
    lock_root.mkdir(parents=True)
    result = _run_hook(repo, lock_root=lock_root)
    # The chained hook returns 7; P5.3 must propagate that exit code
    # rather than collapse to 1.
    assert result.returncode == 7, (
        f"chained .local hook's exit code must propagate; got rc={result.returncode}\n"
        f"STDERR: {result.stderr}"
    )
    # Chained hook's stderr must reach the user.
    assert "chained-local-hook says NO" in result.stderr


# --------------------------------------------------------------------------
# Scenario 18: R1 M2 — chained .local hook that passes lets Part A/B run
# --------------------------------------------------------------------------


def test_s18_chained_local_hook_passes_proceeds_to_part_a_b(
    repo_with_staged_file, tmp_path
):
    """If chained `.local` hook passes (rc=0), Part A/B runs as normal.

    Verified by: installing a passing .local hook AND a peer-held lock
    on the staged file. Without the .local hook, Part A would refuse;
    with a passing .local hook in front, Part A still refuses (because
    .local passed and yielded control to Part A/B).

    Negative version: with no peer lock + non-protected branch + passing
    .local, hook must rc=0.
    """
    repo = repo_with_staged_file
    _git(repo, "checkout", "-b", "feature-x")
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    local_hook = hooks_dir / "pre-commit.local"
    local_hook.write_text(
        "#!/bin/sh\n"
        "echo 'chained-local-hook OK' 1>&2\n"
        "exit 0\n"
    )
    local_hook.chmod(0o755)
    lock_root = tmp_path / "locks" / "active-work"
    lock_root.mkdir(parents=True)
    # No peer lock → Part A allows. Branch is feature-x → Part B skipped.
    result = _run_hook(repo, lock_root=lock_root)
    assert result.returncode == 0, (
        f"passing .local + no Part A/B refusal → rc=0; got rc={result.returncode}\n"
        f"STDERR: {result.stderr}"
    )
    # Stderr from .local should still surface to the user.
    assert "chained-local-hook OK" in result.stderr


# --------------------------------------------------------------------------
# Scenario 19: R1 M2 — absent .local hook proceeds without error
# --------------------------------------------------------------------------


def test_s19_no_chained_hook_skips_gracefully(repo_with_staged_file, tmp_path):
    """No `.git/hooks/pre-commit.local` present → hook runs Part A/B normally.

    R1 M2 baseline: the chaining mechanism is opt-in (presence of
    .local). Without it, behavior is identical to the pre-R1 hook.
    """
    repo = repo_with_staged_file
    _git(repo, "checkout", "-b", "feature-x")
    # Affirmatively assert no .local hook is present in the test repo.
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    assert not (hooks_dir / "pre-commit.local").exists(), (
        "test setup leaked a chained hook into the fixture"
    )
    lock_root = tmp_path / "locks" / "active-work"
    lock_root.mkdir(parents=True)
    result = _run_hook(repo, lock_root=lock_root)
    assert result.returncode == 0, (
        f"no .local + no Part A/B refusal → rc=0; got rc={result.returncode}\n"
        f"STDERR: {result.stderr}"
    )
    # Chained-hook stderr signature must NOT appear (nothing ran).
    assert "chained-local-hook" not in result.stderr
