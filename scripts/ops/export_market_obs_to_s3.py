#!/usr/bin/env python3
"""Nightly archive of market_observations_continuous to S3 (ticket 86b9xcdwg).

market_observations_continuous is the only retention-pruned table on the
VPS (DEFAULT_RETENTION_DAYS=5 per bot/snapshots/market_observations_
snapshotter.py — tightened from 14d by ticket 86ba0jb39 2026-05-19 to
reduce executemany lock-hold tail). Without this archive ~41.5K NBBO
rows/day are permanently lost.

Flow (one nightly run, 05:30 UTC, between journal-rotate @04:00 and
state.db backup @06:00):
  1. target_date = today_utc - 4d (one day inside the 5d retention
     boundary so rows still exist when the read fires).
  2. Read rows via read-only SQLite connection (mode=ro — cannot race
     the snapshotter or sweep, cannot mutate state.db).
  3. Write Parquet with internal zstd (~1-2 MB/day vs ~5 MB raw).
  4. `rclone copyto ... s3prod:kalshi-bot-archive/market_obs/<date>.parquet.zst`
     with --s3-no-check-bucket (writer IAM has no CreateBucket; us-east-1
     rejects LocationConstraint). Same gotchas as Phase 0a state.db backup.
  5. Cleanup tmp unconditionally. Non-zero exit → Telegram alert via
     h4_run_with_alert.py wrapper.

Idempotent: same date = S3 object overwrite. Bucket lifecycle routes
market_obs/ → Glacier IR from day 0; ~$0.02/mo at year 5.

LOCKSTEP: _DEFAULT_LOOKBACK_DAYS must remain strictly less than the
snapshotter's DEFAULT_RETENTION_DAYS (pinned by
tests/contracts/test_market_obs_retention_lockstep.py).
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

# Retention is 5d (post-86ba0jb39 2026-05-19); we archive day 4 so rows
# still exist during read. Must remain strictly less than the snapshotter's
# DEFAULT_RETENTION_DAYS — see module docstring + lockstep contract test.
_DEFAULT_LOOKBACK_DAYS = 4
DEFAULT_RCLONE_REMOTE = "s3prod"
DEFAULT_TABLE = "market_observations_continuous"
DEFAULT_TIME_COL = "observation_time"
DEFAULT_MIN_FREE_MB = 100  # Parquet+zstd ~1-2 MB/day; 100 MB generous.

# Same AWS bucket-name regex as scripts/ops/state_db_s3_backup.py — the
# rclone S3 backend silently mishandles invalid names.
_S3_BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")


# ── date math ──────────────────────────────────────────────────────────


def compute_target_date(today: date, lookback_days: int = _DEFAULT_LOOKBACK_DAYS) -> date:
    return today - timedelta(days=lookback_days)


def compute_object_key(target_date: date) -> str:
    return f"market_obs/{target_date.isoformat()}.parquet.zst"


# ── DB read ────────────────────────────────────────────────────────────


def read_rows_for_date(
    db_path: Path,
    target_date: date,
    table: str = DEFAULT_TABLE,
    time_col: str = DEFAULT_TIME_COL,
) -> Tuple[List[tuple], List[str]]:
    """Read rows whose `time_col` falls on `target_date`. Read-only mode
    so it can never race the snapshotter's writer/sweeper or mutate state.db."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    # PM-001: every sqlite3.connect on this codebase must set busy_timeout.
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        cur = conn.execute(
            f"SELECT * FROM {table} WHERE substr({time_col}, 1, 10) = ?",
            (target_date.isoformat(),),
        )
        columns = [d[0] for d in cur.description]
        return cur.fetchall(), columns
    finally:
        conn.close()


# ── Parquet write ──────────────────────────────────────────────────────


def write_parquet(rows: Sequence[tuple], columns: Sequence[str], dst: Path) -> int:
    """Write `rows` (list of tuples in `columns` order) → Parquet at `dst`
    with internal zstd. Returns dst.stat().st_size."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    columnar = {c: [r[i] for r in rows] for i, c in enumerate(columns)}
    # compression_level=6 mirrors state_db_s3_backup zstd -6 (ticket 86b9xgu9c).
    pq.write_table(
        pa.Table.from_pydict(columnar),
        str(dst),
        compression="zstd",
        compression_level=6,
    )
    return dst.stat().st_size


# ── S3 store (rclone) ──────────────────────────────────────────────────


class S3RcloneStore:
    """rclone-backed S3 store. Gotchas mirror scripts/ops/state_db_s3_backup.py:
    --s3-no-check-bucket on every call (writer IAM has no CreateBucket;
    us-east-1 rejects LocationConstraint), bucket regex pre-validated."""

    def __init__(self, remote: str, bucket: str):
        if not _S3_BUCKET_NAME_RE.match(bucket):
            raise ValueError(
                f"invalid S3 bucket name {bucket!r}: must match AWS bucket "
                "name regex (lowercase, 3-63, [a-z0-9.-], alphanumeric start/end)."
            )
        self.remote = remote
        self.bucket = bucket

    def put(self, local: Path, key: str) -> None:
        cmd = [
            "rclone", "copyto",
            "--checksum",
            "--s3-no-check-bucket",
            "--retries", "3",
            "--low-level-retries", "10",
            str(local),
            f"{self.remote}:{self.bucket}/{key}",
        ]
        try:
            cp = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError as e:
            raise RuntimeError(
                "rclone binary not found. Install: "
                "curl https://rclone.org/install.sh | sudo bash"
            ) from e
        if cp.returncode != 0:
            raise RuntimeError(
                f"rclone put failed (exit {cp.returncode}): {cp.stderr.strip()}"
            )


# ── orchestration ──────────────────────────────────────────────────────


@dataclasses.dataclass
class ExportResult:
    key: str
    target_date: date
    rows: int
    bytes_uploaded: int
    duration_s: float


def run_export(
    db_path: Path,
    store,
    today: Optional[date] = None,
    tmp_dir: Optional[Path] = None,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    min_free_mb: int = DEFAULT_MIN_FREE_MB,
) -> ExportResult:
    """Read → Parquet → upload, cleaning tmp on success OR failure."""
    db_path = Path(db_path)
    today = today or datetime.now(timezone.utc).date()
    target_date = compute_target_date(today, lookback_days)
    key = compute_object_key(target_date)

    owns_tmp = tmp_dir is None
    tmp_dir = Path(tmp_dir) if tmp_dir else Path(tempfile.mkdtemp(prefix="market_obs_export_"))
    tmp_dir.mkdir(parents=True, exist_ok=True)

    usage = shutil.disk_usage(tmp_dir)
    free_mb = usage.free / (1024 * 1024)
    if free_mb < min_free_mb:
        raise RuntimeError(
            f"insufficient disk space at {tmp_dir}: {free_mb:.0f} MB < {min_free_mb} MB"
        )

    pq_path = tmp_dir / f"{target_date.isoformat()}.parquet.zst"
    started = time.monotonic()
    try:
        rows, columns = read_rows_for_date(db_path, target_date)
        bytes_uploaded = write_parquet(rows, columns, pq_path)
        store.put(pq_path, key)
        return ExportResult(
            key=key, target_date=target_date, rows=len(rows),
            bytes_uploaded=bytes_uploaded, duration_s=time.monotonic() - started,
        )
    finally:
        try:
            if pq_path.exists():
                pq_path.unlink()
        except OSError:
            pass
        if owns_tmp:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ── CLI ────────────────────────────────────────────────────────────────


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--db", required=True, type=Path)
    p.add_argument("--bucket", default=None,
                   help="S3 bucket name (default: $S3_BACKUP_BUCKET)")
    p.add_argument("--rclone-remote", default=DEFAULT_RCLONE_REMOTE)
    p.add_argument("--lookback-days", type=int, default=_DEFAULT_LOOKBACK_DAYS)
    p.add_argument("--min-free-mb", type=int, default=DEFAULT_MIN_FREE_MB)
    p.add_argument("--tmp-dir", default=None, type=Path)
    p.add_argument("--date", default=None,
                   help="override target date (YYYY-MM-DD); useful for backfill")
    args = p.parse_args(argv)

    bucket = args.bucket or os.environ.get("S3_BACKUP_BUCKET", "").strip()
    if not bucket:
        print("FAIL: --bucket not given and S3_BACKUP_BUCKET not set", file=sys.stderr)
        return 1
    store = S3RcloneStore(remote=args.rclone_remote, bucket=bucket)

    today = None
    if args.date:
        target = date.fromisoformat(args.date)
        today = target + timedelta(days=args.lookback_days)

    try:
        result = run_export(
            db_path=args.db, store=store, today=today,
            tmp_dir=args.tmp_dir, lookback_days=args.lookback_days,
            min_free_mb=args.min_free_mb,
        )
    except Exception as e:
        print(f"market_obs_export: FAILED: {e}", file=sys.stderr)
        return 1

    if result.rows == 0:
        # An empty archive is a valid pass-through (idempotent overwrite of
        # an empty Parquet) but ALSO the signature of a 24h snapshotter
        # outage. Flag it so journalctl shows a discoverable line; do not
        # fail (exit 0) — the day's S3 object still exists for restore.
        print(
            f"market_obs_export: WARN target_date={result.target_date.isoformat()} "
            "had 0 rows; verify snapshotter health "
            "(bot/snapshots/market_observations_snapshotter.py)",
            file=sys.stderr,
        )
    print(
        f"market_obs_export: OK key={result.key} "
        f"target_date={result.target_date.isoformat()} "
        f"rows={result.rows:,} bytes={result.bytes_uploaded:,} "
        f"duration={result.duration_s:.1f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
