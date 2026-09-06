"""Tests for scripts/ops/journal_archives_s3_sync.py.

Ticket: 86b9xgp7k — incremental S3 sync of ~/kalshi-bot-repo/journal_archives/
so per-tick forensic JSONL streams survive past the 14-day local rotation prune (`ops/rotate_journals.sh`, `-mtime +14`).

Why these tests:
  - The HARD AC is idempotency: re-running the script must be a no-op.
    rclone copy --checksum --immutable provides this; the test pins the
    argv shape so a future refactor (e.g., switching to `sync` which
    would mirror-delete, switching to `copyto` in a loop, or dropping
    --immutable) breaks loudly.
  - `--exclude *.jsonl` is load-bearing — without it we'd upload partial
    snapshots of the live current-day journal at every sync. Pinned by
    argv assertion.
  - --immutable: if a journal file's content ever diverges from its
    uploaded counterpart, rclone exits non-zero. The test pins the flag
    so we keep that signal.
  - Single-runner flock at /var/lock/kalshi-journal-sync.lock — A-M6
    pattern from state_db backup. Pinned by lock-collision test.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[2] / "scripts" / "ops"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


@pytest.fixture
def sync_module():
    import journal_archives_s3_sync
    return journal_archives_s3_sync


@pytest.fixture
def archives_dir(tmp_path: Path) -> Path:
    """Mimic the on-VPS journal_archives layout: rotated .zst files +
    one live .jsonl + a rotation.log."""
    d = tmp_path / "journal_archives"
    d.mkdir()
    (d / "opportunity_journal_2026-04-10.jsonl.zst").write_bytes(b"stub-1")
    (d / "opportunity_journal_2026-04-11.jsonl.zst").write_bytes(b"stub-2")
    (d / "scan_journal_2026-04-11.jsonl.gz").write_bytes(b"stub-3")
    # Live current-day file — MUST be excluded by sync.
    (d / "opportunity_journal_2026-05-13.jsonl").write_bytes(b"live-do-not-upload")
    (d / "rotation.log").write_bytes(b"2026-05-10: rotated\n")
    return d


# ── argv shape ─────────────────────────────────────────────────────────


class TestBuildRcloneArgv:
    def test_uses_copy_not_sync_subcommand(self, sync_module, archives_dir):
        """R1 C1 regression — `rclone sync` mirror-deletes pruned local files
        from S3 (defeats the AC `S3 copy survives local rotation`). Must be
        `rclone copy` (one-way: upload-or-skip, never deletes from dest)."""
        argv = sync_module.build_rclone_argv(archives_dir, "s3prod", "kalshi-bot-archive")
        assert argv[0] == "rclone"
        assert argv[1] == "copy", (
            f"expected 'rclone copy' (one-way; preserves S3 objects when "
            f"local rotation prunes), got {argv[1]!r}. NEVER use 'sync' — "
            f"it mirror-deletes, violating ticket 86b9xgp7k AC."
        )
        assert "sync" not in argv, (
            "sync subcommand reintroduced — would mirror-delete S3 objects "
            "when ops/rotate_journals.sh prunes local files at the 14d boundary"
        )

    def test_has_checksum_flag(self, sync_module, archives_dir):
        argv = sync_module.build_rclone_argv(archives_dir, "s3prod", "kalshi-bot-archive")
        assert "--checksum" in argv, (
            "--checksum needed for idempotency (re-run = no-op based on ETag, "
            "not mtime which can drift)"
        )

    def test_has_immutable_flag(self, sync_module, archives_dir):
        argv = sync_module.build_rclone_argv(archives_dir, "s3prod", "kalshi-bot-archive")
        assert "--immutable" in argv, (
            "--immutable is THE key flag — journal contents are fixed at "
            "rotation time. If content ever diverges, that's a bug/tampering. "
            "Removing this flag silently allows overwrites."
        )

    def test_has_s3_no_check_bucket(self, sync_module, archives_dir):
        argv = sync_module.build_rclone_argv(archives_dir, "s3prod", "kalshi-bot-archive")
        assert "--s3-no-check-bucket" in argv, (
            "writer IAM has no CreateBucket; us-east-1 rejects LocationConstraint. "
            "Same gotcha as scripts/ops/state_db_s3_backup.py."
        )

    def test_excludes_live_jsonl(self, sync_module, archives_dir):
        argv = sync_module.build_rclone_argv(archives_dir, "s3prod", "kalshi-bot-archive")
        # rclone --exclude takes a pattern as the next argv element.
        for i, tok in enumerate(argv):
            if tok == "--exclude" and i + 1 < len(argv):
                if argv[i + 1] == "*.jsonl":
                    return
        pytest.fail(
            f"--exclude *.jsonl missing from argv {argv!r}; live current-day "
            "journals would be uploaded mid-write without it"
        )

    def test_excludes_rotation_log(self, sync_module, archives_dir):
        """R1 C2 regression — rotate_journals.sh APPENDS to rotation.log
        every day. Without --exclude rotation.log, `--immutable` would
        abort the entire sync starting day 2 (rclone exit 6: 'Source and
        destination exist but do not match: immutable file modified')."""
        argv = sync_module.build_rclone_argv(archives_dir, "s3prod", "kalshi-bot-archive")
        for i, tok in enumerate(argv):
            if tok == "--exclude" and i + 1 < len(argv):
                if argv[i + 1] == "rotation.log":
                    return
        pytest.fail(
            f"--exclude rotation.log missing from argv {argv!r}; daily "
            "appends would trip --immutable from day 2 onward, breaking sync"
        )

    def test_destination_uses_journals_prefix(self, sync_module, archives_dir):
        argv = sync_module.build_rclone_argv(archives_dir, "s3prod", "kalshi-bot-archive")
        # Last argv element is the dest.
        assert argv[-1] == "s3prod:kalshi-bot-archive/journals/", argv[-1]

    def test_dry_run_appends_flag(self, sync_module, archives_dir):
        argv = sync_module.build_rclone_argv(
            archives_dir, "s3prod", "kalshi-bot-archive", dry_run=True
        )
        assert "--dry-run" in argv

    def test_custom_prefix_honored(self, sync_module, archives_dir):
        argv = sync_module.build_rclone_argv(
            archives_dir, "s3prod", "kalshi-bot-archive", prefix="custom/"
        )
        assert argv[-1] == "s3prod:kalshi-bot-archive/custom/"


# ── bucket validation ──────────────────────────────────────────────────


class TestBucketValidation:
    def test_invalid_bucket_rejected(self, sync_module):
        with pytest.raises(ValueError, match="bucket"):
            sync_module.validate_bucket("INVALID_UPPERCASE")

    def test_valid_bucket_accepted(self, sync_module):
        sync_module.validate_bucket("kalshi-bot-archive")  # should not raise


# ── single-runner flock ────────────────────────────────────────────────


class TestSingleRunnerLock:
    def test_second_lock_attempt_raises(self, sync_module, tmp_path):
        lock_path = tmp_path / "lock"
        cm1 = sync_module._single_runner_lock(lock_path=lock_path)
        cm1.__enter__()
        try:
            with pytest.raises(RuntimeError, match="in progress"):
                cm2 = sync_module._single_runner_lock(lock_path=lock_path)
                cm2.__enter__()
                cm2.__exit__(None, None, None)
        finally:
            cm1.__exit__(None, None, None)

    def test_lock_released_after_with_block(self, sync_module, tmp_path):
        lock_path = tmp_path / "lock"
        with sync_module._single_runner_lock(lock_path=lock_path):
            pass
        # Should be reacquirable now.
        with sync_module._single_runner_lock(lock_path=lock_path):
            pass


# ── orchestration ──────────────────────────────────────────────────────


class TestRunSync:
    def test_run_sync_invokes_rclone(self, sync_module, archives_dir, monkeypatch):
        captured = []

        class _CP:
            returncode = 0
            stdout = "Transferred: 3 / 3, 100%"
            stderr = ""

        def fake_run(cmd, *args, **kwargs):
            captured.append(list(cmd))
            return _CP()

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = sync_module.run_sync(
            archives_dir, "s3prod", "kalshi-bot-archive", use_lock=False
        )
        assert result.returncode == 0
        assert captured
        assert captured[0][0] == "rclone"
        assert "copy" in captured[0]  # R1 C1: must NOT be `sync` (mirror-deletes)
        assert "--immutable" in captured[0]

    def test_run_sync_raises_on_missing_src(self, sync_module, tmp_path):
        with pytest.raises(FileNotFoundError, match="not a directory"):
            sync_module.run_sync(
                tmp_path / "does-not-exist",
                "s3prod", "kalshi-bot-archive", use_lock=False,
            )

    def test_run_sync_propagates_rclone_failure(
        self, sync_module, archives_dir, monkeypatch
    ):
        # rclone v1.74.1: --immutable content divergence returns exit 6,
        # stderr "Source and destination exist but do not match: immutable
        # file modified". R1 M2 fixed: pin the actual exit code, not 9.
        class _CP:
            returncode = 6
            stdout = ""
            stderr = (
                "ERROR : journal.zst: Source and destination exist but do "
                "not match: immutable file modified"
            )

        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: _CP())
        result = sync_module.run_sync(
            archives_dir, "s3prod", "kalshi-bot-archive", use_lock=False
        )
        assert result.returncode == 6, "rclone --immutable exit 6 must propagate up"
        assert "immutable file modified" in result.stderr

    def test_run_sync_rejects_invalid_bucket(self, sync_module, archives_dir):
        with pytest.raises(ValueError, match="bucket"):
            sync_module.run_sync(
                archives_dir, "s3prod", "INVALID_UPPERCASE", use_lock=False
            )


# ── idempotency (HARD AC) ──────────────────────────────────────────────


class TestIdempotency:
    def test_argv_is_deterministic_and_contains_noop_primitives(
        self, sync_module, archives_dir
    ):
        """The ticket's hardest AC: re-running the script must be a no-op.
        rclone --checksum --immutable handles this at the byte level; we
        can't run real rclone in CI, so we pin the *intent* — the argv is
        deterministic AND contains the per-file no-op-via-checksum +
        no-overwrite-via-immutable flags. End-to-end no-op behavior is
        verified manually via the smoke-fire-then-immediate-re-run loop
        documented in scripts/STATE_DB_BACKUP_SETUP.md §12.3."""
        argv_a = sync_module.build_rclone_argv(
            archives_dir, "s3prod", "kalshi-bot-archive"
        )
        argv_b = sync_module.build_rclone_argv(
            archives_dir, "s3prod", "kalshi-bot-archive"
        )
        assert argv_a == argv_b, "argv must be deterministic for idempotency"
        # The checksum + immutable combo is what makes rclone short-circuit on re-run.
        assert "--checksum" in argv_a
        assert "--immutable" in argv_a


# ── CLI ────────────────────────────────────────────────────────────────


class TestMain:
    def test_main_fails_when_bucket_missing(
        self, sync_module, archives_dir, monkeypatch, capsys
    ):
        monkeypatch.delenv("S3_BACKUP_BUCKET", raising=False)
        rc = sync_module.main(["--src", str(archives_dir)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "bucket" in err.lower()

    def test_main_succeeds_with_explicit_bucket(
        self, sync_module, archives_dir, monkeypatch, capsys
    ):
        class _CP:
            returncode = 0
            stdout = "Transferred: 0 / 0, no-op"
            stderr = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _CP())
        # Real `/tmp/kalshi-journal-sync.lock` is used on dev Mac (the
        # module-level _LOCK_PATH falls back from /var/lock to /tmp when
        # /var/lock isn't writable). The in-process flock is released on
        # context exit; lock collision behavior is exercised by TestSingleRunnerLock.
        rc = sync_module.main(
            ["--src", str(archives_dir), "--bucket", "kalshi-bot-archive"]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "OK" in out

    def test_main_propagates_nonzero_exit(
        self, sync_module, archives_dir, monkeypatch, capsys
    ):
        # rclone v1.74.1 --immutable divergence returns exit 6 (R1 M2 fix).
        class _CP:
            returncode = 6
            stdout = ""
            stderr = (
                "ERROR : journal.zst: Source and destination exist but do "
                "not match: immutable file modified"
            )

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _CP())
        rc = sync_module.main(
            ["--src", str(archives_dir), "--bucket", "kalshi-bot-archive"]
        )
        assert rc == 6
        err = capsys.readouterr().err
        assert "immutable file modified" in err
