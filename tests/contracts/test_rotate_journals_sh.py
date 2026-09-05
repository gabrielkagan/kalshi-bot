"""`ops/rotate_journals.sh` — hour-stamped, fail-closed journal rotation
(ticket 86bbvd50a, 2026-09-05).

RCA (kb/failures/vps-disk-full-journal-rotation-collision-sep05.md): the
VPS-local, untracked `rotate_journals.sh` named archives
`${journal}_${DATE}.jsonl`. When PR #64 moved the cron from daily to
`0 */4 * * *`, every run after the first of the day hit
`zstd: ... already exists; not overwritten` — the raw copy stayed, the
NEXT run's `cp` clobbered it. 436 intermediate 4-hour chunks per journal
(opportunity 52.0 GB raw, scan 41.5 GB raw) were silently destroyed over
109 days and 230 orphan raws (30.2 GB) filled the disk on 2026-09-04.

What this file pins:
  1. The script is TRACKED under `ops/` (drift no longer invisible) and
     executable, and parses under `bash -n`.
  2. The archive stem is hour-stamped (`%Y-%m-%dT%H`) — the PR-#64
     `_${DATE}.jsonl` collision shape must not come back.
  3. `ops/install.sh` validates the script on every install.
  4. Behavior (bash + zstd required, skipped otherwise):
     a. two runs in one UTC day at different hours → two distinct .zst
        archives, live file truncated after each, no raw left behind;
     b. a second run with the SAME stamp REFUSES before touching the live
        file (no cp, no truncate, non-zero exit, loud message);
     c. a failed `mv` (read-only archive dir) keeps the live file intact
        and exits non-zero (R1-M2: rotation is an atomic same-filesystem
        rename, not copy-then-truncate — the copy window dropped lines);
     d. below-threshold journals are skipped untouched;
     e. local retention prunes archives older than
        ROTATE_LOCAL_RETENTION_DAYS;
     f. a leftover uncompressed raw from an earlier failed compress is
        retried and compressed on the next run (R1-M3); legacy date-only
        raws from the old script are also seen — compressed when alone,
        flagged (errors=N) when a compressed twin exists (R2-M3);
     g. the "Done." line carries `errors=N` for monitor_watchdog.py.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "ops" / "rotate_journals.sh"
INSTALL_SH = REPO_ROOT / "ops" / "install.sh"

_TOOLS_MISSING = shutil.which("bash") is None or shutil.which("zstd") is None


# ─── 1-3. structural pins ────────────────────────────────────────────────────


def test_rotate_script_is_tracked_under_ops():
    assert SCRIPT.is_file(), (
        f"{SCRIPT} missing — rotate_journals.sh must live in git under ops/ "
        f"(L-rot-2: VPS-only ops scripts drift invisibly)."
    )


def test_rotate_script_is_executable():
    assert os.access(SCRIPT, os.X_OK), (
        f"{SCRIPT} is not executable; `chmod +x ops/rotate_journals.sh`."
    )


def test_rotate_script_parses_under_bash_n():
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_archive_stem_is_hour_stamped():
    src = SCRIPT.read_text()
    assert "%Y-%m-%dT%H" in src, (
        "archive stamp must include the UTC hour (date -u +%Y-%m-%dT%H); "
        "date-only stamps collide under the every-4h cron (PR #64 class)."
    )
    assert "_${DATE}.jsonl" not in src, (
        "the PR-#64 collision shape `${journal%.jsonl}_${DATE}.jsonl` is back."
    )


def test_install_sh_validates_rotate_script():
    src = INSTALL_SH.read_text()
    assert "rotate_journals.sh" in src, "install.sh must validate ops/rotate_journals.sh"
    assert "bash -n" in src, "install.sh must syntax-check the rotation script (bash -n)"


# ─── 4. behavior ─────────────────────────────────────────────────────────────


def _make_live(repo: Path, name: str, size_bytes: int) -> Path:
    p = repo / name
    line = b'{"type": "opportunity", "x": 1}\n'
    p.write_bytes(line * (size_bytes // len(line) + 1))
    return p


def _run(tmp_path: Path, stamp: str, *, journals=("opportunity_journal.jsonl",),
         extra_env=None) -> subprocess.CompletedProcess:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    archive_dir = tmp_path / "archives"
    env = dict(os.environ)
    env.update({
        "ROTATE_REPO_DIR": str(repo),
        "ROTATE_ARCHIVE_DIR": str(archive_dir),
        "ROTATE_STAMP": stamp,
        "ROTATE_JOURNALS": " ".join(journals),
    })
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True,
    )


@pytest.mark.skipif(_TOOLS_MISSING, reason="bash + zstd required")
def test_two_hours_same_day_produce_two_archives(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    live = _make_live(repo, "opportunity_journal.jsonl", 11 * 1024 * 1024)
    r1 = _run(tmp_path, "2026-09-05T00")
    assert r1.returncode == 0, r1.stdout + r1.stderr
    assert not live.exists() or live.stat().st_size == 0, (
        "live journal must be renamed away (the bot re-creates it on next append)"
    )
    assert "errors=0" in r1.stdout
    _make_live(repo, "opportunity_journal.jsonl", 11 * 1024 * 1024)
    r2 = _run(tmp_path, "2026-09-05T04")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    archives = sorted(p.name for p in (tmp_path / "archives").iterdir())
    assert archives == [
        "opportunity_journal_2026-09-05T00.jsonl.zst",
        "opportunity_journal_2026-09-05T04.jsonl.zst",
    ], archives
    assert not live.exists() or live.stat().st_size == 0


@pytest.mark.skipif(_TOOLS_MISSING, reason="bash + zstd required")
def test_same_stamp_rerun_refuses_before_touching_live(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    live = _make_live(repo, "opportunity_journal.jsonl", 11 * 1024 * 1024)
    r1 = _run(tmp_path, "2026-09-05T08")
    assert r1.returncode == 0, r1.stdout + r1.stderr
    _make_live(repo, "opportunity_journal.jsonl", 12 * 1024 * 1024)
    size_before = live.stat().st_size
    r2 = _run(tmp_path, "2026-09-05T08")
    assert r2.returncode != 0, "same-stamp rerun must exit non-zero (fail-closed)"
    assert "ERROR" in (r2.stdout + r2.stderr)
    assert "errors=1" in r2.stdout, "Done line must carry the error count for the watchdog"
    assert live.stat().st_size == size_before, (
        "live journal must NOT be truncated when the archive name collides"
    )
    names = sorted(p.name for p in (tmp_path / "archives").iterdir())
    assert names == ["opportunity_journal_2026-09-05T08.jsonl.zst"], (
        f"no raw .jsonl may be left behind and the existing .zst must be "
        f"untouched; got {names}"
    )


@pytest.mark.skipif(_TOOLS_MISSING, reason="bash + zstd required")
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_move_failure_keeps_live_intact(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    live = _make_live(repo, "scan_journal.jsonl", 11 * 1024 * 1024)
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    archive_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)  # mv → EACCES
    try:
        r = _run(tmp_path, "2026-09-05T12", journals=("scan_journal.jsonl",))
    finally:
        archive_dir.chmod(stat.S_IRWXU)
    assert r.returncode != 0
    assert "ERROR" in (r.stdout + r.stderr)
    assert live.stat().st_size > 0, "a failed rename must leave the live journal untouched"
    assert list(archive_dir.iterdir()) == []


@pytest.mark.skipif(_TOOLS_MISSING, reason="bash + zstd required")
def test_leftover_raw_from_failed_compress_is_retried(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    leftover = archive_dir / "opportunity_journal_2026-09-04T20.jsonl"
    leftover.write_bytes(b'{"x": 1}\n' * 1000)
    _make_live(repo, "opportunity_journal.jsonl", 1024)  # below threshold → SKIP
    r = _run(tmp_path, "2026-09-05T00")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RECOVERED" in r.stdout
    assert not leftover.exists()
    assert (archive_dir / "opportunity_journal_2026-09-04T20.jsonl.zst").is_file()


@pytest.mark.skipif(_TOOLS_MISSING, reason="bash + zstd required")
def test_legacy_date_only_raw_with_compressed_twin_is_flagged(tmp_path: Path):
    """R2-M3: the pre-2026-09-05 script left `<journal>_YYYY-MM-DD.jsonl`
    raws next to their day `.zst` on every collision. They must be seen
    (glob covers the date-only shape), NOT clobbered, and counted as an
    error so the watchdog surfaces them for manual triage."""
    repo = tmp_path / "repo"
    repo.mkdir()
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    legacy_raw = archive_dir / "scan_journal_2026-09-05.jsonl"
    legacy_raw.write_bytes(b'{"x": 1}\n' * 100)
    twin = archive_dir / "scan_journal_2026-09-05.jsonl.zst"
    twin.write_bytes(b"not-really-zstd")
    _make_live(repo, "scan_journal.jsonl", 1024)
    r = _run(tmp_path, "2026-09-05T20", journals=("scan_journal.jsonl",))
    assert r.returncode != 0
    assert "compressed twin" in r.stdout and "errors=1" in r.stdout
    assert legacy_raw.exists() and twin.read_bytes() == b"not-really-zstd"


@pytest.mark.skipif(_TOOLS_MISSING, reason="bash + zstd required")
def test_legacy_date_only_raw_without_twin_is_compressed(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    legacy_raw = archive_dir / "opportunity_journal_2026-09-05.jsonl"
    legacy_raw.write_bytes(b'{"x": 1}\n' * 100)
    _make_live(repo, "opportunity_journal.jsonl", 1024)
    r = _run(tmp_path, "2026-09-05T20")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not legacy_raw.exists()
    assert (archive_dir / "opportunity_journal_2026-09-05.jsonl.zst").is_file()


@pytest.mark.skipif(_TOOLS_MISSING, reason="bash + zstd required")
def test_small_journal_is_skipped_untouched(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    live = _make_live(repo, "rejection_journal.jsonl", 1024)
    r = _run(tmp_path, "2026-09-05T16", journals=("rejection_journal.jsonl",))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SKIP" in r.stdout
    assert live.stat().st_size > 0
    assert not (tmp_path / "archives").exists() or list((tmp_path / "archives").iterdir()) == []


@pytest.mark.skipif(_TOOLS_MISSING, reason="bash + zstd required")
def test_local_retention_prunes_old_archives(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    old = archive_dir / "opportunity_journal_2026-08-01T00.jsonl.zst"
    old.write_bytes(b"x")
    twenty_days_ago = time.time() - 20 * 86400
    os.utime(old, (twenty_days_ago, twenty_days_ago))
    fresh = archive_dir / "opportunity_journal_2026-09-04T20.jsonl.zst"
    fresh.write_bytes(b"x")
    _make_live(repo, "opportunity_journal.jsonl", 1024)
    r = _run(tmp_path, "2026-09-05T20", extra_env={"ROTATE_LOCAL_RETENTION_DAYS": "14"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert not old.exists(), "archives older than the retention window must be pruned"
    assert fresh.exists()
