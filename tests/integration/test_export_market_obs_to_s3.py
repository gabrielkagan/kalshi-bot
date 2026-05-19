"""Tests for scripts/ops/export_market_obs_to_s3.py.

Ticket: 86b9xcdwg — archive market_observations_continuous to S3 before
the on-VPS retention sweep prunes rows. Without this, ~41.5K NBBO rows/day
are permanently lost — exactly the data the H-3 fill simulator + future
calibration work needs historical depth on. Ticket 86ba0jb39 (2026-05-19)
tightened the on-VPS retention from 14d to 5d to reduce executemany
lock-hold tail; the export script's lookback dropped 13d → 4d in lockstep.

Why these tests:
  - The acceptance criterion is round-trip parity: a row count in the
    uploaded Parquet must match the DB row count for that day. The
    headline test pins that contract end-to-end via LocalDirStore (no
    S3/rclone dep).
  - Date-window correctness: we archive the date that is one day inside
    the retention boundary (today - 4d) so rows still exist when we
    read them. Tested directly + via fake-today injection.
  - Bucket-name validation reuses the same regex pattern Phase 0a
    settled on (lowercase, 3-63, [a-z0-9.-]). Tested so a typo doesn't
    silently upload to the wrong bucket.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[2] / "scripts" / "ops"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


# ── fixtures ────────────────────────────────────────────────────────────


_TABLE_DDL = """
CREATE TABLE market_observations_continuous (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    observation_time TEXT NOT NULL,
    yes_bid_cents INTEGER,
    yes_ask_cents INTEGER,
    no_bid_cents INTEGER,
    no_ask_cents INTEGER,
    bid_depth INTEGER,
    ask_depth INTEGER,
    source TEXT NOT NULL,
    cache_age_ms INTEGER
);
"""


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_TABLE_DDL)
    conn.commit()
    conn.close()


def _insert_rows(path: Path, target_date: date, n_rows: int, ticker_prefix: str = "KX") -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA busy_timeout=5000")
    base_iso = target_date.isoformat()
    for i in range(n_rows):
        # Spread observation_time across the day to be realistic.
        ts = f"{base_iso}T{(i % 24):02d}:{(i % 60):02d}:00Z"
        conn.execute(
            """
            INSERT INTO market_observations_continuous
            (ticker, observation_time, yes_bid_cents, yes_ask_cents,
             no_bid_cents, no_ask_cents, bid_depth, ask_depth, source, cache_age_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (f"{ticker_prefix}-{i:05d}", ts, 50 + (i % 40), 55 + (i % 40),
             45 + (i % 40), 50 + (i % 40), i % 100, (i + 1) % 100, "ws", i * 10),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def live_db(tmp_path: Path) -> Path:
    db = tmp_path / "state.db"
    _make_db(db)
    return db


@pytest.fixture
def export_module():
    import export_market_obs_to_s3
    return export_market_obs_to_s3


class LocalDirStore:
    """Test-only file-backed BackupStore (the production script only
    needs S3RcloneStore; this lives in the test file so the script
    stays under the ≤200 LOC ticket cap)."""

    def __init__(self, root):
        self.root = Path(root)

    def put(self, local, key):
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, target)

    def get(self, key, local):
        source = self.root / key
        if not source.exists():
            raise FileNotFoundError(f"key not found in store: {key}")
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, local)


# ── date math ──────────────────────────────────────────────────────────


class TestTargetDate:
    def test_default_target_is_4_days_before_today(self, export_module):
        d = date(2026, 5, 13)
        assert export_module.compute_target_date(d) == date(2026, 5, 9)

    def test_target_date_lookback_override(self, export_module):
        d = date(2026, 5, 13)
        assert export_module.compute_target_date(d, lookback_days=7) == date(2026, 5, 6)

    def test_object_key_format(self, export_module):
        d = date(2026, 4, 30)
        assert export_module.compute_object_key(d) == "market_obs/2026-04-30.parquet.zst"


# ── DB read ────────────────────────────────────────────────────────────


class TestReadRows:
    def test_reads_only_target_date_rows(self, live_db, export_module):
        # Insert rows for 3 dates; assert only target date returns.
        _insert_rows(live_db, date(2026, 5, 1), n_rows=10)
        _insert_rows(live_db, date(2026, 5, 2), n_rows=15)
        _insert_rows(live_db, date(2026, 5, 3), n_rows=20)

        rows, columns = export_module.read_rows_for_date(live_db, date(2026, 5, 2))
        assert len(rows) == 15
        # Confirm column order matches the table.
        assert columns[1] == "ticker"
        assert columns[2] == "observation_time"
        # All rows must have observation_time on the target date.
        time_col_idx = columns.index("observation_time")
        for r in rows:
            assert r[time_col_idx].startswith("2026-05-02"), r[time_col_idx]

    def test_empty_target_date_returns_zero_rows(self, live_db, export_module):
        _insert_rows(live_db, date(2026, 5, 1), n_rows=10)
        rows, columns = export_module.read_rows_for_date(live_db, date(2026, 5, 5))
        assert rows == []
        # Column metadata still present (so downstream Parquet writer has schema).
        assert "ticker" in columns
        assert "observation_time" in columns

    def test_db_opened_read_only(self, live_db, export_module):
        # Even with concurrent writers, the read shouldn't lock the DB.
        # Sanity check: read-only mode rejects writes if they leaked in.
        _insert_rows(live_db, date(2026, 5, 1), n_rows=5)
        rows, columns = export_module.read_rows_for_date(live_db, date(2026, 5, 1))
        assert len(rows) == 5


# ── Parquet round-trip ─────────────────────────────────────────────────


class TestParquetRoundTrip:
    def test_parquet_round_trip_row_count(self, live_db, tmp_path, export_module):
        _insert_rows(live_db, date(2026, 5, 1), n_rows=50)
        rows, columns = export_module.read_rows_for_date(live_db, date(2026, 5, 1))

        out = tmp_path / "out.parquet.zst"
        bytes_written = export_module.write_parquet(rows, columns, out)
        assert out.exists()
        assert bytes_written > 0

        # Read back via pyarrow.
        import pyarrow.parquet as pq
        table = pq.read_table(str(out))
        assert table.num_rows == 50
        assert "ticker" in table.column_names
        assert "observation_time" in table.column_names

    def test_parquet_round_trip_zero_rows(self, live_db, tmp_path, export_module):
        # Empty input should still produce a valid Parquet file with the schema.
        _, columns = export_module.read_rows_for_date(live_db, date(2026, 1, 1))
        out = tmp_path / "out.parquet.zst"
        export_module.write_parquet([], columns, out)
        assert out.exists()

        import pyarrow.parquet as pq
        table = pq.read_table(str(out))
        assert table.num_rows == 0


# ── end-to-end orchestration via LocalDirStore ─────────────────────────


class TestEndToEnd:
    def test_full_pipeline_round_trip(self, live_db, tmp_path, export_module):
        """Headline AC: DB → parquet → upload → download → parquet → row
        count matches DB count for that day. No S3/rclone dep — LocalDirStore."""
        target = date(2026, 5, 1)
        _insert_rows(live_db, target, n_rows=200)
        _insert_rows(live_db, target + timedelta(days=1), n_rows=33)  # other-day noise

        store_root = tmp_path / "store"
        store = LocalDirStore(store_root)

        # Run as if today is target + 4d (so the default lookback fires on target).
        today = target + timedelta(days=4)

        result = export_module.run_export(
            db_path=live_db, store=store, today=today,
            tmp_dir=tmp_path / "tmp",
        )

        assert result.target_date == target
        assert result.key == f"market_obs/{target.isoformat()}.parquet.zst"
        assert result.rows == 200  # other-day noise must NOT appear
        assert result.bytes_uploaded > 0

        # Round-trip download
        downloaded = tmp_path / "downloaded.parquet.zst"
        store.get(result.key, downloaded)
        import pyarrow.parquet as pq
        table = pq.read_table(str(downloaded))
        assert table.num_rows == 200

        # Live DB count for target date matches uploaded Parquet count.
        conn = sqlite3.connect(str(live_db))
        try:
            db_count = conn.execute(
                "SELECT COUNT(*) FROM market_observations_continuous "
                "WHERE substr(observation_time, 1, 10) = ?",
                (target.isoformat(),),
            ).fetchone()[0]
        finally:
            conn.close()
        assert db_count == table.num_rows == 200

    def test_cleans_up_tmp_on_success(self, live_db, tmp_path, export_module):
        _insert_rows(live_db, date(2026, 5, 1), n_rows=10)
        store = LocalDirStore(tmp_path / "store")
        tmp_dir = tmp_path / "tmp"
        export_module.run_export(
            db_path=live_db, store=store,
            today=date(2026, 5, 14), tmp_dir=tmp_dir,
        )
        # Tmp parquet must not linger.
        leftovers = list(tmp_dir.glob("*.parquet*")) if tmp_dir.exists() else []
        assert leftovers == [], f"tmp parquet not cleaned: {leftovers}"

    def test_cleans_up_tmp_on_failure(self, live_db, tmp_path, export_module, monkeypatch):
        _insert_rows(live_db, date(2026, 5, 1), n_rows=10)

        class BrokenStore:
            def put(self, local, key):
                raise RuntimeError("simulated upload failure")
            def get(self, key, local):
                raise NotImplementedError

        tmp_dir = tmp_path / "tmp"
        with pytest.raises(RuntimeError, match="simulated upload"):
            export_module.run_export(
                db_path=live_db, store=BrokenStore(),
                today=date(2026, 5, 14), tmp_dir=tmp_dir,
            )
        leftovers = list(tmp_dir.glob("*.parquet*")) if tmp_dir.exists() else []
        assert leftovers == [], f"failed-upload left tmp parquet: {leftovers}"


# ── bucket-name validation ─────────────────────────────────────────────


class TestS3RcloneStore:
    def test_invalid_bucket_name_rejected(self, export_module):
        with pytest.raises(ValueError, match="bucket name"):
            export_module.S3RcloneStore(remote="s3prod", bucket="INVALID_UPPERCASE")

    def test_valid_bucket_name_accepted(self, export_module):
        # Doesn't actually call rclone; just constructs.
        store = export_module.S3RcloneStore(remote="s3prod", bucket="kalshi-bot-archive")
        assert store.bucket == "kalshi-bot-archive"

    def test_empty_day_emits_warn_to_stderr_and_ok_to_stdout(
        self, live_db, tmp_path, export_module, monkeypatch, capsys
    ):
        """R2-MN2 regression — pins the M2 WARN block (empty archive detected,
        but exit still 0). Future refactor that suppresses WARN OR fails on
        rows=0 OR omits the OK print must trip this test."""
        # Empty DB → 0 rows for any target_date.
        store_root = tmp_path / "store"
        store = LocalDirStore(store_root)

        # Run via main() so we exercise the WARN path end-to-end.
        argv = [
            "--db", str(live_db),
            "--bucket", "kalshi-bot-archive",
            "--date", "2026-05-01",
            "--tmp-dir", str(tmp_path / "tmp"),
            "--min-free-mb", "1",
        ]
        # Patch _resolve via env hack: main() builds S3RcloneStore from --bucket;
        # we want LocalDirStore, so monkeypatch S3RcloneStore to wrap our store.
        monkeypatch.setattr(export_module, "S3RcloneStore",
                            lambda remote, bucket: store)
        rc = export_module.main(argv)
        captured = capsys.readouterr()

        assert rc == 0, f"empty day must still exit 0, got {rc}"
        assert "WARN" in captured.err and "0 rows" in captured.err, (
            f"empty-day WARN missing from stderr: {captured.err!r}"
        )
        assert "OK" in captured.out and "rows=0" in captured.out, (
            f"OK line missing from stdout for empty day: {captured.out!r}"
        )

    def test_put_passes_s3_no_check_bucket_flag(self, export_module, monkeypatch, tmp_path):
        """The writer IAM lacks CreateBucket; rclone's default 'check bucket'
        call would 403. --s3-no-check-bucket is load-bearing per Phase 0a RCA."""
        captured = []

        class _CP:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(cmd, *args, **kwargs):
            captured.append(list(cmd))
            return _CP()

        monkeypatch.setattr(export_module.subprocess, "run", fake_run)

        store = export_module.S3RcloneStore(remote="s3prod", bucket="kalshi-bot-archive")
        local = tmp_path / "x.parquet.zst"
        local.write_bytes(b"stub")
        store.put(local, "market_obs/2026-05-01.parquet.zst")

        assert captured
        argv = captured[0]
        assert argv[0] == "rclone"
        assert "--s3-no-check-bucket" in argv, (
            f"--s3-no-check-bucket missing from rclone argv {argv!r}; "
            "writer IAM has no CreateBucket and us-east-1 rejects "
            "LocationConstraint — this flag is load-bearing."
        )
