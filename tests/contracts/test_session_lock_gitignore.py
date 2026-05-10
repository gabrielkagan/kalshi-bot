"""Sprint PSC Bit P5.1 R2 M6 — contract: lock artifacts must be gitignored.

The `_session_lock.SessionLock` primitive writes per-target lockfiles,
tmp sidecars, quarantine sidecars, and a reclaim audit log under
`.claude/locks/`. All carry machine-specific PIDs / session_ids / wall-
clock heartbeats — pure runtime state, never meant for the repo.

Without this gitignore, any agent running `git add -A`, `git add
.claude/`, or an over-eager glob commits machine-specific PIDs into the
repo. That's self-defeating: a coordination primitive that pollutes the
repo it coordinates.

Verifies via `git check-ignore` (subprocess) — the same logic git itself
uses, not a re-implementation of the matcher.

If this test fails:
- Inspect `.gitignore` for the four expected patterns under
  `.claude/locks/`.
- The block is grouped under a `# Sprint PSC Bit P5.1` comment near
  the existing `.claude/scheduled_tasks*.lock` entry.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _check_ignore(rel_path: str) -> bool:
    """True iff `git check-ignore <rel_path>` exits 0 (i.e. path IS ignored)."""
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", rel_path],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    # git check-ignore exits 0 when ignored, 1 when NOT ignored,
    # >1 on error.
    if result.returncode not in (0, 1):
        pytest.fail(
            f"git check-ignore errored on {rel_path!r}: "
            f"rc={result.returncode} stderr={result.stderr!r}"
        )
    return result.returncode == 0


@pytest.mark.parametrize(
    "rel_path",
    [
        # Canonical lockfiles for flattened target paths.
        ".claude/locks/active-work/bot__SLASH___impl.py.lock",
        ".claude/locks/active-work/bot__SLASH__scanner__SLASH____init__.py.lock",
        # In-flight tmp publish sidecars from _try_create / _tick_heartbeat.
        ".claude/locks/active-work/bot__SLASH___impl.py.lock.tmp.1234.deadbeef",
        # Quarantined malformed lockfiles (millis + reclaimer-pid stamp).
        ".claude/locks/active-work/bot__SLASH___impl.py.123456789012.345.quarantine",
        # The reclaim audit log itself.
        ".claude/locks/reclaim.log",
    ],
)
def test_lock_artifact_is_gitignored(rel_path):
    """Every lock-runtime artifact must be ignored by git.

    Without this, `git status` reports `?? .claude/locks/reclaim.log`
    after the first SessionLock acquire + release, and any commit using
    `git add -A` or `git add .claude/` silently bundles a machine-
    specific PID into the repo.
    """
    assert _check_ignore(rel_path), (
        f"{rel_path!r} is NOT gitignored; lock-runtime artifacts must "
        f"never enter the repo. Check .gitignore for the Sprint PSC "
        f"Bit P5.1 block."
    )


def test_gitkeep_is_NOT_gitignored():
    """`.claude/locks/active-work/.gitkeep` keeps the dir alive in a
    fresh clone. The Bit P5.1 patterns must not catch it.

    If this fails, the gitignore pattern is too broad and the lock-dir
    won't survive a fresh clone — first SessionLock acquire would have
    to `mkdir(parents=True, exist_ok=True)` against a missing parent.
    """
    assert not _check_ignore(".claude/locks/active-work/.gitkeep"), (
        ".gitkeep must remain tracked — it preserves the lock dir in a "
        "fresh clone. The gitignore pattern is over-broad."
    )


def test_gitkeep_currently_tracked():
    """Defense in depth — verify `.gitkeep` is actually in the index,
    not just "would-be-trackable". A gitignore that catches `.gitkeep`
    AFTER it was added to the index doesn't un-track it, but a fresh
    clone would mysteriously miss the file.
    """
    result = subprocess.run(
        ["git", "ls-files", ".claude/locks/active-work/.gitkeep"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert ".gitkeep" in result.stdout, (
        f"`.claude/locks/active-work/.gitkeep` not tracked: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_check_ignore_negative_smoke():
    """Sanity: the subprocess detector positively reports a path that
    is NOT ignored. Catches a regression where `_check_ignore` always
    returns True (e.g. inverted exit code, swallowed error).

    `README.md` lives at the repo root and is not under any gitignore
    pattern.
    """
    assert not _check_ignore("README.md"), (
        "negative smoke broke: README.md should NOT be gitignored — "
        "the _check_ignore helper is returning the wrong polarity."
    )
