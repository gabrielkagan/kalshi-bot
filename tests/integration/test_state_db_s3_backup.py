"""Tests for scripts/state_db_s3_backup.py — Phase 0a state.db backup.

Phase 0a per kb/decisions/autoresearch-design-may05.md hazards table:
the worst-case failure mode for the autoresearch track is losing
months of training data because we never set up backups.

Why these tests:
  - rsync of a hot WAL-mode SQLite is unsafe (page tearing); we use
    sqlite3.Connection.backup() instead. Tests pin that behavior with
    a concurrent-writes integration test.
  - The S3 transport is mocked with a LocalDirStore implementing the
    same BackupStore ABC as the real S3RcloneStore. No moto/boto3/
    docker dep — keeps pyproject.toml [dev] untouched (parallel
    Pillar 4 + 5 sessions own that surface).
  - Restore parity is the AC: a snapshot must round-trip through
    snapshot+compress+put+get+decompress and produce a byte- (and
    row-count-) identical database.

Ticket: 86b9vd9e3.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


# ── fixtures ────────────────────────────────────────────────────────────


def _make_test_db(path: Path, n_rows: int = 100) -> None:
    """Build a small WAL-mode SQLite that mimics state.db's shape.

    We don't care about exact schema — just three tables with rows so
    the snapshot/restore parity check has something to compare.
    """
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(
        """
        CREATE TABLE settled_trades (
            ticker TEXT PRIMARY KEY,
            asset TEXT,
            pnl_cents INTEGER
        );
        CREATE TABLE evaluated_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            edge REAL
        );
        CREATE TABLE rejected_opportunities (
            ticker TEXT PRIMARY KEY,
            rejection_reason TEXT
        );
        """
    )
    for i in range(n_rows):
        conn.execute(
            "INSERT INTO settled_trades VALUES (?, ?, ?)",
            (f"KX-{i:05d}", ("BTC", "ETH", "SOL", "XRP")[i % 4], i * 10),
        )
        conn.execute(
            "INSERT INTO evaluated_opportunities (ticker, edge) VALUES (?, ?)",
            (f"KX-{i:05d}", 0.01 + i * 0.001),
        )
        conn.execute(
            "INSERT INTO rejected_opportunities VALUES (?, ?)",
            (f"REJ-{i:05d}", "edge_too_low"),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def live_db(tmp_path: Path) -> Path:
    """Pretend live state.db with WAL active and known rows."""
    db = tmp_path / "state.db"
    _make_test_db(db, n_rows=200)
    return db


@pytest.fixture
def backup_module():
    """Lazy import after path patching so the test collector doesn't fail
    if scripts/state_db_s3_backup.py was renamed/removed (then the test
    file's import-time line itself becomes the regression flag)."""
    import state_db_s3_backup
    return state_db_s3_backup


# ── snapshot integrity ─────────────────────────────────────────────────


class TestSnapshot:
    def test_snapshot_produces_readable_sqlite(self, live_db, tmp_path, backup_module):
        snap = tmp_path / "snap.db"
        backup_module.snapshot_sqlite(live_db, snap)
        assert snap.exists()
        # File must be openable and look like SQLite (header magic)
        assert snap.read_bytes()[:16] == b"SQLite format 3\x00"

    def test_snapshot_row_count_parity(self, live_db, tmp_path, backup_module):
        snap = tmp_path / "snap.db"
        backup_module.snapshot_sqlite(live_db, snap)

        live = sqlite3.connect(str(live_db))
        snap_conn = sqlite3.connect(str(snap))
        try:
            for table in ("settled_trades", "evaluated_opportunities", "rejected_opportunities"):
                live_n = live.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                snap_n = snap_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                assert live_n == snap_n, f"{table}: live={live_n} snap={snap_n}"
        finally:
            live.close(); snap_conn.close()

    def test_snapshot_passes_integrity_check(self, live_db, tmp_path, backup_module):
        snap = tmp_path / "snap.db"
        backup_module.snapshot_sqlite(live_db, snap)
        conn = sqlite3.connect(str(snap))
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            assert result == "ok", f"integrity_check returned {result!r}"
        finally:
            conn.close()

    def test_backup_uses_yielding_pages_constants(self, backup_module):
        """Round-1 A-C1: pin the yielding constants so a future change
        back to pages=-1 (the stdlib default) gets caught here. The
        default would lock writers for the entire 30-60s copy on the
        VPS, breaching the bot's 10s busy_timeout."""
        assert backup_module._BACKUP_PAGES_PER_STEP > 0, (
            "must yield to writers; never set pages=-1 (whole-copy lock)"
        )
        assert backup_module._BACKUP_PAGES_PER_STEP <= 1000, (
            "yielding too coarse-grained; large batches re-introduce the lock window"
        )
        assert backup_module._BACKUP_SLEEP_BETWEEN_STEPS_S > 0, (
            "must sleep between batches so writers actually win the lock"
        )

    def test_writer_makes_progress_during_snapshot(
        self, tmp_path, backup_module
    ):
        """Round-1 A-C1: the original concurrent-writes test was structurally
        false-green (writer's own busy_timeout swallowed any BUSY silently).
        This test asserts the writer COMPLETES INSERTS during the snapshot
        window — the actual production guarantee that A-C1 was about."""
        # Larger DB so snapshot takes long enough for writer to interleave
        live_db = tmp_path / "live.db"
        _make_test_db(live_db, n_rows=2000)

        stop = threading.Event()
        successful_writes_during_snapshot = [0]
        snapshot_started = threading.Event()
        snapshot_done = threading.Event()

        def writer():
            conn = sqlite3.connect(str(live_db), timeout=15)
            conn.execute("PRAGMA busy_timeout=10000")
            i = 0
            # Wait for snapshot to actually start before counting
            snapshot_started.wait(timeout=5)
            while not snapshot_done.is_set():
                try:
                    conn.execute(
                        "INSERT INTO settled_trades VALUES (?, ?, ?)",
                        (f"DURING-SNAP-{i:05d}", "BTC", i),
                    )
                    conn.commit()
                    successful_writes_during_snapshot[0] += 1
                    i += 1
                except sqlite3.OperationalError:
                    pass
                time.sleep(0.001)  # don't completely starve other threads
            conn.close()

        t = threading.Thread(target=writer)
        t.start()
        try:
            snapshot_started.set()
            snap = tmp_path / "snap.db"
            backup_module.snapshot_sqlite(live_db, snap)
        finally:
            snapshot_done.set()
            t.join(timeout=10)

        # The whole point of yielding pages: writer must make non-zero
        # progress while the snapshot is in flight.
        assert successful_writes_during_snapshot[0] > 0, (
            "writer made ZERO successful writes during snapshot — yielding "
            "is broken; bot would get locked out in production"
        )

        # Snapshot must still be integrity-clean
        conn = sqlite3.connect(str(snap))
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()

    def test_snapshot_under_concurrent_writes(self, live_db, tmp_path, backup_module):
        """Critical safety test — the whole reason we use .backup() not rsync.

        Spawn a writer thread hammering the live DB while we snapshot. The
        snapshot must remain integrity-clean (no torn pages) and the row
        count in the snapshot must equal SOME valid intermediate state
        (>= initial, <= initial+writes_completed).
        """
        initial_n = sqlite3.connect(str(live_db)).execute(
            "SELECT COUNT(*) FROM settled_trades"
        ).fetchone()[0]

        stop = threading.Event()
        write_count = [0]

        def writer():
            conn = sqlite3.connect(str(live_db), timeout=10)
            conn.execute("PRAGMA busy_timeout=5000")
            i = 0
            while not stop.is_set():
                try:
                    conn.execute(
                        "INSERT INTO settled_trades VALUES (?, ?, ?)",
                        (f"CONCURRENT-{i:05d}", "BTC", i),
                    )
                    conn.commit()
                    write_count[0] += 1
                    i += 1
                except sqlite3.OperationalError:
                    pass
            conn.close()

        t = threading.Thread(target=writer)
        t.start()
        time.sleep(0.05)  # let writer warm up
        try:
            snap = tmp_path / "snap.db"
            backup_module.snapshot_sqlite(live_db, snap)
        finally:
            stop.set()
            t.join(timeout=5)

        # Integrity must hold despite concurrent writes.
        conn = sqlite3.connect(str(snap))
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            snap_n = conn.execute("SELECT COUNT(*) FROM settled_trades").fetchone()[0]
        finally:
            conn.close()

        # Snapshot is a consistent point-in-time view: must be >= initial
        # (writer only adds) and <= initial + total writes that completed.
        assert initial_n <= snap_n <= initial_n + write_count[0], (
            f"snap_n={snap_n} not in [{initial_n}, {initial_n + write_count[0]}]"
        )

    def test_snapshot_refuses_overwrite_existing(self, live_db, tmp_path, backup_module):
        """Don't silently clobber — caller must rm or pass --force."""
        snap = tmp_path / "snap.db"
        snap.write_bytes(b"existing")
        with pytest.raises(FileExistsError):
            backup_module.snapshot_sqlite(live_db, snap)

    def test_snapshot_overwrite_with_force(self, live_db, tmp_path, backup_module):
        snap = tmp_path / "snap.db"
        snap.write_bytes(b"existing")
        backup_module.snapshot_sqlite(live_db, snap, force=True)
        assert snap.read_bytes()[:16] == b"SQLite format 3\x00"


# ── compression ─────────────────────────────────────────────────────────


class TestCompression:
    def test_zstd_round_trip(self, live_db, tmp_path, backup_module):
        snap = tmp_path / "snap.db"
        backup_module.snapshot_sqlite(live_db, snap)
        original_bytes = snap.read_bytes()

        compressed = tmp_path / "snap.db.zst"
        backup_module.compress(snap, compressed, algorithm="zstd")
        assert compressed.exists()
        # Compression should yield smaller file (SQLite is highly compressible)
        assert compressed.stat().st_size < snap.stat().st_size

        # Round-trip
        restored = tmp_path / "restored.db"
        backup_module.decompress(compressed, restored, algorithm="zstd")
        assert restored.read_bytes() == original_bytes

    def test_gzip_round_trip(self, live_db, tmp_path, backup_module):
        snap = tmp_path / "snap.db"
        backup_module.snapshot_sqlite(live_db, snap)
        original_bytes = snap.read_bytes()

        compressed = tmp_path / "snap.db.gz"
        backup_module.compress(snap, compressed, algorithm="gzip")
        assert compressed.exists()
        assert compressed.stat().st_size < snap.stat().st_size

        restored = tmp_path / "restored.db"
        backup_module.decompress(compressed, restored, algorithm="gzip")
        assert restored.read_bytes() == original_bytes

    def test_compress_unknown_algorithm_raises(self, live_db, tmp_path, backup_module):
        snap = tmp_path / "snap.db"
        backup_module.snapshot_sqlite(live_db, snap)
        with pytest.raises(ValueError, match="algorithm"):
            backup_module.compress(snap, tmp_path / "x.bz2", algorithm="bzip2")


# ── object key routing ─────────────────────────────────────────────────


class TestObjectKey:
    def test_daily_key_format(self, backup_module):
        d = date(2026, 5, 9)
        assert backup_module.compute_object_key(d, ext="zst") == "daily/state-db-2026-05-09.db.zst"

    def test_daily_key_gzip(self, backup_module):
        d = date(2026, 1, 1)
        assert backup_module.compute_object_key(d, ext="gz") == "daily/state-db-2026-01-01.db.gz"

    def test_compute_object_key_pads_zeros(self, backup_module):
        d = date(2026, 3, 5)
        # Must be ISO-8601 zero-padded (sorts lexicographically by date)
        assert backup_module.compute_object_key(d, ext="zst") == "daily/state-db-2026-03-05.db.zst"


# ── BackupStore ABC + LocalDirStore ────────────────────────────────────


class TestBackupStore:
    def test_local_dir_store_put_creates_file(self, tmp_path, backup_module):
        store_root = tmp_path / "store"
        store_root.mkdir()
        store = backup_module.LocalDirStore(store_root)

        local = tmp_path / "local.bin"
        local.write_bytes(b"hello")
        store.put(local, "daily/state-db-2026-05-09.db.zst")

        target = store_root / "daily" / "state-db-2026-05-09.db.zst"
        assert target.exists()
        assert target.read_bytes() == b"hello"

    def test_local_dir_store_get_reads_file(self, tmp_path, backup_module):
        store_root = tmp_path / "store"
        (store_root / "daily").mkdir(parents=True)
        (store_root / "daily" / "state-db-2026-05-09.db.zst").write_bytes(b"world")

        store = backup_module.LocalDirStore(store_root)
        out = tmp_path / "out.bin"
        store.get("daily/state-db-2026-05-09.db.zst", out)
        assert out.read_bytes() == b"world"

    def test_local_dir_store_list_returns_sorted_keys(self, tmp_path, backup_module):
        store_root = tmp_path / "store"
        (store_root / "daily").mkdir(parents=True)
        for d in ("2026-05-07", "2026-05-09", "2026-05-08"):
            (store_root / "daily" / f"state-db-{d}.db.zst").write_bytes(b"x")

        store = backup_module.LocalDirStore(store_root)
        keys = store.list("daily/")
        assert keys == [
            "daily/state-db-2026-05-07.db.zst",
            "daily/state-db-2026-05-08.db.zst",
            "daily/state-db-2026-05-09.db.zst",
        ]

    def test_local_dir_store_get_missing_key_raises(self, tmp_path, backup_module):
        store = backup_module.LocalDirStore(tmp_path / "empty")
        with pytest.raises(FileNotFoundError):
            store.get("daily/missing.db.zst", tmp_path / "out")


# ── pre-flight + safety ────────────────────────────────────────────────


class TestSingleRunnerLock:
    """Round-1 A-M6: a manual `systemctl start` during the daily run can
    collide with the scheduled run. flock prevents the second instance
    from holding the SQLite write lock simultaneously."""

    def test_concurrent_run_backup_blocks(self, live_db, tmp_path, backup_module, monkeypatch):
        # Use a non-default lock path to avoid colliding with anything else
        lock_path = tmp_path / "test-lock"
        monkeypatch.setattr(backup_module, "_LOCK_PATH", lock_path)

        store = backup_module.LocalDirStore(tmp_path / "store")

        # Acquire the lock manually (simulating an in-flight backup),
        # then assert run_backup refuses to proceed.
        with backup_module._single_runner_lock(lock_path):
            with pytest.raises(RuntimeError, match="another state.db backup"):
                # We need to call run_backup with use_lock=True to test the
                # lock path; pass a fresh inner lock attempt.
                backup_module.run_backup(
                    db_path=live_db,
                    store=store,
                    today=date(2026, 5, 9),
                    tmp_dir=tmp_path / "tmp",
                    algorithm="zstd",
                    min_free_mb=1,
                    use_lock=True,
                )

    def test_lock_released_after_run(self, live_db, tmp_path, backup_module, monkeypatch):
        """After run_backup finishes, the lock must be free for the next run."""
        lock_path = tmp_path / "test-lock"
        monkeypatch.setattr(backup_module, "_LOCK_PATH", lock_path)
        store = backup_module.LocalDirStore(tmp_path / "store")

        backup_module.run_backup(
            db_path=live_db, store=store, today=date(2026, 5, 9),
            tmp_dir=tmp_path / "tmp1", algorithm="zstd", min_free_mb=1,
            use_lock=True,
        )
        # Should be able to acquire fresh; if not, lock leaked.
        with backup_module._single_runner_lock(lock_path):
            pass  # acquired and released cleanly


class TestMkdtempCleanup:
    """Round-1 A-M7: prior implementation called mkdtemp() but only
    unlinked files inside, leaking empty dirs forever in /tmp."""

    def test_auto_created_tmp_dir_removed_after_run(self, live_db, tmp_path, backup_module, monkeypatch):
        # Override tempfile.mkdtemp to use our tmp_path so we can observe
        # the directory after the run.
        created_dirs = []
        real_mkdtemp = tempfile.mkdtemp
        def tracking_mkdtemp(prefix=""):
            d = real_mkdtemp(prefix=prefix, dir=str(tmp_path))
            created_dirs.append(Path(d))
            return d
        monkeypatch.setattr(tempfile, "mkdtemp", tracking_mkdtemp)
        # Disable lock to avoid /tmp lock collisions in parallel test runs.
        store = backup_module.LocalDirStore(tmp_path / "store")
        backup_module.run_backup(
            db_path=live_db, store=store, today=date(2026, 5, 9),
            algorithm="zstd", min_free_mb=1, use_lock=False,
        )
        # Auto-created tmp dir must NOT exist after success
        assert created_dirs, "mkdtemp was never called"
        assert not created_dirs[0].exists(), (
            f"auto-created tmp dir leaked: {created_dirs[0]}"
        )

    def test_caller_supplied_tmp_dir_preserved(self, live_db, tmp_path, backup_module):
        """If caller passed --tmp-dir, we must NOT rmtree it (caller owns it)."""
        store = backup_module.LocalDirStore(tmp_path / "store")
        my_tmp = tmp_path / "my-tmp"
        backup_module.run_backup(
            db_path=live_db, store=store, today=date(2026, 5, 9),
            tmp_dir=my_tmp, algorithm="zstd", min_free_mb=1, use_lock=False,
        )
        # Caller-supplied dir should still exist (just be empty)
        assert my_tmp.exists()


class TestZstdFallback:
    """Round-1 m1: if zstd binary missing, fall back to gzip with a
    warning rather than crashing the daily timer."""

    def test_compress_falls_back_to_gzip_when_zstd_missing(
        self, live_db, tmp_path, backup_module, monkeypatch
    ):
        # Pretend zstd is not installed
        real_which = shutil.which
        monkeypatch.setattr(
            shutil, "which",
            lambda binary: None if binary == "zstd" else real_which(binary),
        )

        snap = tmp_path / "snap.db"
        backup_module.snapshot_sqlite(live_db, snap)

        # Caller asked for zstd extension, but we should write a .gz file
        out = tmp_path / "out.zst"
        backup_module.compress(snap, out, algorithm="zstd")

        # The .gz file should exist (not the .zst)
        gz_out = tmp_path / "out.gz"
        assert gz_out.exists(), f"expected gzip fallback at {gz_out}"

    def test_run_backup_uses_gz_extension_on_zstd_fallback(
        self, live_db, tmp_path, backup_module, monkeypatch
    ):
        real_which = shutil.which
        monkeypatch.setattr(
            shutil, "which",
            lambda binary: None if binary == "zstd" else real_which(binary),
        )
        store = backup_module.LocalDirStore(tmp_path / "store")
        result = backup_module.run_backup(
            db_path=live_db, store=store, today=date(2026, 5, 9),
            tmp_dir=tmp_path / "tmp", algorithm="zstd", min_free_mb=1,
            use_lock=False,
        )
        # Key must reflect the actual upload extension, not the requested one
        assert result.key.endswith(".gz")
        assert result.key == "daily/state-db-2026-05-09.db.gz"


class TestPreflight:
    def test_disk_space_check_passes_with_room(self, tmp_path, backup_module):
        # tmp_path on a normal dev machine has GB free
        # min_free_mb = 1 should always pass
        backup_module.check_disk_space(tmp_path, min_free_mb=1)

    def test_disk_space_check_raises_when_low(self, tmp_path, backup_module, monkeypatch):
        # Force shutil.disk_usage to report low free space
        Usage = type("Usage", (), {})
        usage = Usage()
        usage.total = 1_000_000_000
        usage.used = 999_990_000
        usage.free = 10_000  # 10 KB free
        monkeypatch.setattr(shutil, "disk_usage", lambda p: usage)

        with pytest.raises(RuntimeError, match="disk space"):
            backup_module.check_disk_space(tmp_path, min_free_mb=100)

    def test_refuses_when_db_path_missing(self, tmp_path, backup_module):
        with pytest.raises(FileNotFoundError):
            backup_module.snapshot_sqlite(
                tmp_path / "nonexistent.db", tmp_path / "snap.db"
            )


# ── end-to-end orchestration (LocalDirStore) ───────────────────────────


class TestEndToEnd:
    def test_full_pipeline_round_trip(self, live_db, tmp_path, backup_module):
        """The headline test: snapshot → compress → put → get → decompress
        produces a byte-identical (and row-count-identical) database."""
        store_root = tmp_path / "store"
        store_root.mkdir()
        store = backup_module.LocalDirStore(store_root)

        result = backup_module.run_backup(
            db_path=live_db,
            store=store,
            today=date(2026, 5, 9),
            tmp_dir=tmp_path / "tmp",
            algorithm="zstd",
            min_free_mb=1,
        )

        assert result.key == "daily/state-db-2026-05-09.db.zst"
        assert result.bytes_uploaded > 0
        assert result.bytes_uncompressed > result.bytes_uploaded  # compression worked

        # Now restore and verify
        downloaded = tmp_path / "downloaded.db.zst"
        store.get(result.key, downloaded)
        restored = tmp_path / "restored.db"
        backup_module.decompress(downloaded, restored, algorithm="zstd")

        # Row-count parity
        live = sqlite3.connect(str(live_db))
        rest = sqlite3.connect(str(restored))
        try:
            for table in ("settled_trades", "evaluated_opportunities", "rejected_opportunities"):
                live_n = live.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                rest_n = rest.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                assert live_n == rest_n, f"{table}: live={live_n} restored={rest_n}"
            assert rest.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            live.close(); rest.close()

    def test_run_backup_cleans_up_tmp_files(self, live_db, tmp_path, backup_module):
        store = backup_module.LocalDirStore(tmp_path / "store")
        tmp_dir = tmp_path / "tmp"

        backup_module.run_backup(
            db_path=live_db,
            store=store,
            today=date(2026, 5, 9),
            tmp_dir=tmp_dir,
            algorithm="zstd",
            min_free_mb=1,
        )

        # Tmp dir should be empty (or not exist) after success
        if tmp_dir.exists():
            assert list(tmp_dir.iterdir()) == [], (
                f"tmp dir not cleaned: {list(tmp_dir.iterdir())}"
            )

    def test_run_backup_cleans_up_on_failure(self, live_db, tmp_path, backup_module):
        """If upload fails, tmp snapshot+compressed must still be cleaned —
        otherwise repeated cron failures fill the disk."""
        class FailingStore:
            def put(self, local, key):
                raise RuntimeError("simulated upload failure")

        tmp_dir = tmp_path / "tmp"
        with pytest.raises(RuntimeError, match="simulated upload failure"):
            backup_module.run_backup(
                db_path=live_db,
                store=FailingStore(),
                today=date(2026, 5, 9),
                tmp_dir=tmp_dir,
                algorithm="zstd",
                min_free_mb=1,
            )
        if tmp_dir.exists():
            assert list(tmp_dir.iterdir()) == [], (
                f"tmp dir not cleaned after failure: {list(tmp_dir.iterdir())}"
            )


# ── S3RcloneStore (subprocess shape only — rclone not installed in CI) ─


class TestS3RcloneStore:
    """Smoke-test the rclone command shape without invoking rclone itself.

    rclone is not a CI dep (and isn't installed locally on dev macs by
    default — verified 2026-05-09). These tests stub subprocess.run and
    pin the command shape so a refactor that drops the --checksum flag
    or the s3:bucket/ prefix gets caught.
    """

    def test_put_invokes_rclone_copyto(self, tmp_path, backup_module, monkeypatch):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            class CP:
                returncode = 0
                stdout = ""
                stderr = ""
            return CP()

        monkeypatch.setattr(subprocess, "run", fake_run)

        store = backup_module.S3RcloneStore(remote="s3prod", bucket="test-bucket")
        local = tmp_path / "snap.db.zst"
        local.write_bytes(b"x")
        store.put(local, "daily/state-db-2026-05-09.db.zst")

        assert len(calls) == 1
        cmd = calls[0]
        # First arg is the binary
        assert cmd[0] == "rclone"
        # copyto, not copy (we want the exact destination key)
        assert "copyto" in cmd
        assert str(local) in cmd
        # Destination must include the s3 remote + bucket + key
        assert any("s3prod:test-bucket/daily/state-db-2026-05-09.db.zst" in str(a) for a in cmd)
        # Must include --checksum to verify upload integrity
        assert "--checksum" in cmd

    def test_put_raises_on_nonzero_exit(self, tmp_path, backup_module, monkeypatch):
        def fake_run(cmd, **kw):
            class CP:
                returncode = 1
                stdout = ""
                stderr = "auth error"
            return CP()
        monkeypatch.setattr(subprocess, "run", fake_run)

        store = backup_module.S3RcloneStore(remote="s3prod", bucket="test-bucket")
        local = tmp_path / "x"
        local.write_bytes(b"x")
        with pytest.raises(RuntimeError, match="rclone"):
            store.put(local, "daily/x.db.zst")

    def test_get_invokes_rclone_copyto_reverse(self, tmp_path, backup_module, monkeypatch):
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            class CP:
                returncode = 0; stdout = ""; stderr = ""
            # Round-1 B-C1: get() now does an lsjson preflight to detect
            # Glacier-tiered keys. Return STANDARD-tier metadata so the
            # preflight passes and the copyto proceeds.
            if "lsjson" in cmd:
                CP.stdout = '[{"Tier": "STANDARD", "Size": 100}]'
            return CP()
        monkeypatch.setattr(subprocess, "run", fake_run)

        store = backup_module.S3RcloneStore(remote="s3prod", bucket="test-bucket")
        store.get("daily/x.db.zst", tmp_path / "out")

        # Two calls: lsjson (preflight) then copyto (download)
        assert len(calls) == 2
        assert "lsjson" in calls[0]
        copyto_cmd = calls[1]
        assert copyto_cmd[0] == "rclone"
        assert "copyto" in copyto_cmd
        assert any("s3prod:test-bucket/daily/x.db.zst" in str(a) for a in copyto_cmd)
        assert str(tmp_path / "out") in copyto_cmd

    def test_get_refuses_glacier_tiered_key(self, tmp_path, backup_module, monkeypatch):
        """Round-1 B-C1: an object in DEEP_ARCHIVE needs RestoreObject
        before GetObject. Pre-check + clear error pointing at recovery."""
        def fake_run(cmd, **kw):
            class CP:
                returncode = 0
                stdout = '[{"Tier": "DEEP_ARCHIVE", "Size": 100}]'
                stderr = ""
            return CP()
        monkeypatch.setattr(subprocess, "run", fake_run)

        store = backup_module.S3RcloneStore(remote="s3prod", bucket="test-bucket")
        with pytest.raises(RuntimeError, match="DEEP_ARCHIVE"):
            store.get("daily/old.db.zst", tmp_path / "out")

    def test_get_refuses_glacier_standard_tiered_key(
        self, tmp_path, backup_module, monkeypatch
    ):
        """Same protection for plain GLACIER tier (3-5h retrieval)."""
        def fake_run(cmd, **kw):
            class CP:
                returncode = 0
                stdout = '[{"Tier": "GLACIER", "Size": 100}]'
                stderr = ""
            return CP()
        monkeypatch.setattr(subprocess, "run", fake_run)

        store = backup_module.S3RcloneStore(remote="s3prod", bucket="test-bucket")
        with pytest.raises(RuntimeError, match=r"(GLACIER|restore-object)"):
            store.get("daily/old.db.zst", tmp_path / "out")

    def test_invalid_bucket_name_rejected(self, backup_module):
        """Round-1 A-M4 / B-M6: rclone S3 silently mishandles invalid
        bucket names. Reject early with a clear error."""
        with pytest.raises(ValueError, match="bucket name"):
            backup_module.S3RcloneStore(remote="s3prod", bucket="MyBucket")
        with pytest.raises(ValueError, match="bucket name"):
            backup_module.S3RcloneStore(remote="s3prod", bucket="ab")
        with pytest.raises(ValueError, match="bucket name"):
            backup_module.S3RcloneStore(remote="s3prod", bucket="x" * 64)
        with pytest.raises(ValueError, match="bucket name"):
            backup_module.S3RcloneStore(remote="s3prod", bucket="-leading-dash")
        # Valid names must succeed:
        backup_module.S3RcloneStore(remote="s3prod", bucket="kalshi-state-db-backup")
        backup_module.S3RcloneStore(remote="s3prod", bucket="abc")

    def test_get_glacier_ir_does_not_require_restore(
        self, tmp_path, backup_module, monkeypatch
    ):
        """Round-2 R2-M4: GLACIER_IR (Instant Retrieval) is millisecond-
        accessible like STANDARD. Does NOT require RestoreObject. Only
        plain GLACIER (Flexible) and DEEP_ARCHIVE need a thaw."""
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            class CP:
                returncode = 0; stderr = ""
            if "lsjson" in cmd:
                CP.stdout = '[{"Tier": "GLACIER_IR", "Size": 100}]'
            else:
                CP.stdout = ""
            return CP()
        monkeypatch.setattr(subprocess, "run", fake_run)

        store = backup_module.S3RcloneStore(remote="s3prod", bucket="test-bucket")
        # Should NOT raise — GLACIER_IR is instant-retrievable.
        store.get("daily/x.db.zst", tmp_path / "out")
        # Two rclone calls (lsjson + copyto), both fired
        assert len(calls) == 2

    def test_get_fails_closed_on_unknown_tier(
        self, tmp_path, backup_module, monkeypatch
    ):
        """Round-2 R2-M3: rclone < 1.55 doesn't surface Tier in lsjson.
        Without storage-class info, fail closed unless caller passes
        --allow-unknown-tier."""
        def fake_run(cmd, **kw):
            class CP:
                returncode = 0
                stderr = ""
                # lsjson returns valid JSON but no Tier field (old rclone)
                stdout = '[{"Size": 100, "Name": "x.db.zst"}]'
            return CP()
        monkeypatch.setattr(subprocess, "run", fake_run)

        store = backup_module.S3RcloneStore(remote="s3prod", bucket="test-bucket")
        # Default fails closed
        with pytest.raises(RuntimeError, match="storage class"):
            store.get("daily/x.db.zst", tmp_path / "out")
        # Explicit override allows it (caller takes responsibility)
        store.get("daily/x.db.zst", tmp_path / "out", allow_unknown_tier=True)

    def test_rclone_not_found_friendly_error(
        self, tmp_path, backup_module, monkeypatch
    ):
        """Round-1 MN4: missing rclone binary gets a friendly error
        (with install instructions) instead of opaque ENOENT."""
        def fake_run(cmd, **kw):
            raise FileNotFoundError(2, "No such file or directory: 'rclone'")
        monkeypatch.setattr(subprocess, "run", fake_run)

        store = backup_module.S3RcloneStore(remote="s3prod", bucket="test-bucket")
        local = tmp_path / "x"
        local.write_bytes(b"x")
        with pytest.raises(RuntimeError, match=r"rclone.*not found.*install"):
            store.put(local, "daily/x.db.zst")

    def test_list_invokes_rclone_lsf(self, tmp_path, backup_module, monkeypatch):
        def fake_run(cmd, **kw):
            class CP:
                returncode = 0
                stdout = "state-db-2026-05-07.db.zst\nstate-db-2026-05-09.db.zst\nstate-db-2026-05-08.db.zst\n"
                stderr = ""
            return CP()
        monkeypatch.setattr(subprocess, "run", fake_run)

        store = backup_module.S3RcloneStore(remote="s3prod", bucket="test-bucket")
        keys = store.list("daily/")
        # Sorted output
        assert keys == [
            "daily/state-db-2026-05-07.db.zst",
            "daily/state-db-2026-05-08.db.zst",
            "daily/state-db-2026-05-09.db.zst",
        ]


class TestLockPathNotInPrivateTmp:
    """Round-2 R2-C3 + Round-3 B3-M1: lockfile must NOT live under /tmp
    because the systemd unit uses PrivateTmp=true (its /tmp is namespaced
    and not visible to other processes). The advisory lock would silently
    fail to coordinate manual + scheduled runs."""

    def test_lock_path_default_not_under_tmp(self, backup_module):
        # B3-M1: skip explicitly so test output makes the host-state
        # gate visible (silent fall-through hides the regression).
        if not os.access("/var/lock", os.W_OK):
            pytest.skip("/var/lock not writable on this host (Mac dev or hardened VPS)")
        assert "/var/lock" in str(backup_module._LOCK_PATH), (
            f"_LOCK_PATH should use /var/lock when writable, "
            f"got {backup_module._LOCK_PATH}"
        )
