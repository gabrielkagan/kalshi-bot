#!/usr/bin/env python3
"""Incremental S3 sync of ~/kalshi-bot-repo/journal_archives/ (ticket 86b9xgp7k).

journal_archives/ holds the per-tick forensic JSONL streams
(opportunity_journal, scan_journal, rejection_journal, ...). The
ops/rotate_journals.sh cron deletes locally at the 14-day retention boundary;
without S3 archival the per-tick record is gone forever. This sync runs
30 min AFTER rotate_journals.sh (04:30 UTC) so yesterday's journal is
fully compressed before upload.

Primitive: `rclone copy --checksum --immutable` (NOT `sync` and NOT
`copyto` in a loop).

WHY `copy` AND NOT `sync` (R1 C1, ticket 86b9xgp7k): `rclone sync` makes
the destination MIRROR the source — when `rotate_journals.sh` prunes a
journal locally at the 14-day boundary, the next `sync` would DELETE
the corresponding S3 object. That defeats the entire reason this script
exists ("the per-tick record is gone forever once rotation deletes them").
`rclone copy` is the one-way primitive: it uploads new files + skips
existing ones (via --checksum ETag short-circuit) but never deletes
from the destination. The ticket spec mentions `sync` but the AC
("After local rotation deletes a journal, the S3 copy survives") only
holds for `copy`.

--immutable surfaces content divergence as a non-zero exit (rclone
exit-code 6 on v1.74.1), which the h4_run_with_alert.py wrapper
escalates to Telegram — bug or tampering alert. The live current-day
`.jsonl` (uncompressed, mid-write) is excluded via filter, and
`rotation.log` is also excluded (R1 C2): rotate_journals.sh APPENDS
to rotation.log every day, so its content changes between runs and
--immutable would abort the sync on day 2 of the schedule.

Bucket lifecycle for `journals/` prefix (audit-confirmed live state +
canonical post-D1.5 template at `scripts/STATE_DB_BACKUP_SETUP.md` §2):
Standard → DEEP_ARCHIVE @ 30d, never expire. ~$0.50/mo year 1,
~$0.40/mo year 5. (Pre-D1.5 this docstring claimed a 30d→GLACIER_IR
→90d→DEEP_ARCHIVE step pattern; that text was speculative and never
matched the live bucket; D1.5 R3 corrected it.)
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

DEFAULT_RCLONE_REMOTE = "s3prod"
DEFAULT_PREFIX = "journals/"

# Same A-M6 single-runner pattern as scripts/ops/state_db_s3_backup.py —
# /var/lock survives PrivateTmp=true; falls back to /tmp on dev Mac.
_LOCK_PATH = Path("/var/lock/kalshi-journal-sync.lock") \
    if os.access("/var/lock", os.W_OK) \
    else Path("/tmp/kalshi-journal-sync.lock")

# AWS S3 bucket-name regex (matches scripts/ops/state_db_s3_backup.py).
_S3_BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")


def validate_bucket(bucket: str) -> None:
    if not _S3_BUCKET_NAME_RE.match(bucket):
        raise ValueError(
            f"invalid S3 bucket name {bucket!r}: must match AWS bucket "
            "regex (lowercase, 3-63 chars, [a-z0-9.-], alphanumeric start/end)."
        )


# ── flock ──────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _single_runner_lock(lock_path: Optional[Path] = None):
    """fcntl flock guard — refuses second runner. Matches A-M6 pattern in
    scripts/ops/state_db_s3_backup.py:_single_runner_lock. Mode 0o666
    prevents lockout if a root-run debug invocation creates the file."""
    if lock_path is None:
        lock_path = _LOCK_PATH
    fd = None
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o666)
        try:
            os.fchmod(fd, 0o666)
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise RuntimeError(
                f"another journal sync is in progress (lock {lock_path} held); "
                "refusing to run concurrently."
            ) from e
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass


# ── rclone argv ────────────────────────────────────────────────────────


def build_rclone_argv(
    src_dir: Path,
    remote: str,
    bucket: str,
    prefix: str = DEFAULT_PREFIX,
    dry_run: bool = False,
) -> List[str]:
    """Assemble the rclone copy command. Kept as a pure function so the
    test suite can pin every load-bearing flag without a real rclone call."""
    cmd = [
        "rclone", "copy",     # NOT `sync` — see module docstring R1 C1
        "--checksum",         # idempotency: ETag-based, ignores mtime drift
        "--immutable",        # alert on content divergence (bug/tampering)
        "--s3-no-check-bucket",  # writer IAM has no CreateBucket; us-east-1
                                 # rejects LocationConstraint
        "--retries", "3",
        "--low-level-retries", "10",
        "--exclude", "*.jsonl",        # exclude live current-day journals
        "--exclude", "rotation.log",   # R1 C2: rotate_journals.sh APPENDS
                                       # to this every run (4h); --immutable would
                                       # abort the sync on day 2
        str(src_dir),
        f"{remote}:{bucket}/{prefix}",
    ]
    if dry_run:
        cmd.append("--dry-run")
    return cmd


# ── orchestration ──────────────────────────────────────────────────────


@dataclasses.dataclass
class SyncResult:
    src_dir: Path
    dest: str
    rclone_argv: List[str]
    returncode: int
    stdout: str
    stderr: str


def run_sync(
    src_dir: Path,
    remote: str,
    bucket: str,
    prefix: str = DEFAULT_PREFIX,
    dry_run: bool = False,
    use_lock: bool = True,
) -> SyncResult:
    """Validate inputs → flock → invoke rclone → return result. Caller
    handles exit-code dispatch (the h4_run_with_alert.py wrapper turns
    non-zero into Telegram alerts)."""
    src_dir = Path(src_dir)
    if not src_dir.is_dir():
        raise FileNotFoundError(f"src_dir is not a directory: {src_dir}")
    validate_bucket(bucket)
    cmd = build_rclone_argv(src_dir, remote, bucket, prefix, dry_run)
    dest = f"{remote}:{bucket}/{prefix}"

    lock_cm = _single_runner_lock() if use_lock else contextlib.nullcontext()
    with lock_cm:
        try:
            cp = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError as e:
            raise RuntimeError(
                "rclone binary not found in PATH. Install via: "
                "curl https://rclone.org/install.sh | sudo bash"
            ) from e
    return SyncResult(
        src_dir=src_dir, dest=dest, rclone_argv=cmd,
        returncode=cp.returncode, stdout=cp.stdout, stderr=cp.stderr,
    )


# ── CLI ────────────────────────────────────────────────────────────────


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--src", required=True, type=Path,
                   help="local journal_archives directory")
    p.add_argument("--bucket", default=None,
                   help="S3 bucket (default: $S3_BACKUP_BUCKET)")
    p.add_argument("--rclone-remote", default=DEFAULT_RCLONE_REMOTE)
    p.add_argument("--prefix", default=DEFAULT_PREFIX)
    p.add_argument("--dry-run", action="store_true",
                   help="pass --dry-run to rclone (preview only)")
    args = p.parse_args(argv)

    bucket = args.bucket or os.environ.get("S3_BACKUP_BUCKET", "").strip()
    if not bucket:
        print("FAIL: --bucket not given and S3_BACKUP_BUCKET not set", file=sys.stderr)
        return 1

    try:
        result = run_sync(args.src, args.rclone_remote, bucket, args.prefix, args.dry_run)
    except Exception as e:
        print(f"journal_sync: FAILED: {e}", file=sys.stderr)
        return 1

    if result.returncode != 0:
        print(
            f"journal_sync: FAILED rclone exit={result.returncode}\n"
            f"STDERR: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return result.returncode

    print(f"journal_sync: OK dest={result.dest}")
    if result.stdout.strip():
        # rclone copy's transfer summary; preserve for journalctl forensics.
        print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
