#!/usr/bin/env python3
"""state.db nightly backup to S3 (via rclone).

Phase 0a per kb/decisions/autoresearch-design-may05.md hazards table —
"Loss of historical training data | No state.db backup | Phase 0a blocker".
Without this, a single VPS failure deletes the months of
evaluated_opportunities + settled_trades that all of Track A/B/C
autoresearch depends on.

WHY NOT rsync (despite the ticket title saying "rsync"):
state.db runs in WAL mode (journal_mode=WAL, busy_timeout=10000). A
literal `rsync state.db` captures only the main file mid-checkpoint —
committed-but-not-checkpointed transactions live in -wal (~140 MB
steady state per kb/decisions/wal-threshold-raise-may08.md). rsync
also chunks at file-block granularity, not SQLite page granularity,
producing torn pages on restore. rsyncing all three files
(state.db + -wal + -shm) is also wrong: they are mutually consistent
only at a single instant, and rsync iterates them serially.

The correct primitive is the SQLite online backup API
(`sqlite3.Connection.backup()`), which holds a brief shared lock per
copied page. Concurrent writes proceed; the backup picks up new pages
on its next pass. Stdlib. The VPS has no `sqlite3` CLI installed (see
ops/CLAUDE.md Bit 2.0.5.3 docs) so the Python stdlib API is the only
realistic path on the VPS anyway.

Architecture (one nightly run, 06:00 UTC, post H-4 cron chain):
  1. Pre-flight df check (need ~2x state.db worth of free tmp space).
  2. Snapshot live state.db -> /tmp/state-db-snapshot-<ts>.db via
     sqlite3.Connection.backup().
  3. Compress with `zstd -6` (fallback `gzip -9` if zstd missing).
  4. `rclone copyto --checksum LOCAL s3prod:bucket/daily/state-db-<date>.db.zst`.
  5. Cleanup tmp files (always, even on failure).
  6. Exit 0; non-zero triggers Telegram alert via h4_run_with_alert.py
     wrapper (already wired in setup_state_db_backup_timer.sh).

Lifecycle on the bucket transitions Standard -> Glacier IR (30 d) ->
Deep Archive (90 d). Snapshots never expire — see plan doc for cost
projection (~$0.30/mo at year 5).

IAM scope:
  - Writer creds (this script, on VPS): s3:PutObject only. NO Delete,
    NO Get, NO List. Compromised VPS cannot ransomware backups.
  - Reader creds (state_db_restore.py, on dev Mac, ~/.aws profile
    `kalshi-state-db-restore`): GetObject + ListBucket. Read-only.

Ticket: 86b9vd9e3.
Plan: kb/decisions/auto-research-phase-0a-plan-may09.md.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Protocol


# Defaults tuned for the VPS — overridable via CLI for test/staging.
DEFAULT_MIN_FREE_MB = 1500  # ~3x compressed size headroom on a 25 GB droplet
DEFAULT_ALGORITHM = "zstd"
DEFAULT_RCLONE_REMOTE = "s3prod"

# S3 bucket-name regex per AWS spec (lowercase, no double-dots, 3-63 chars).
# Round-1 finding A-M4 / B-M6: rclone S3 backend silently mishandles
# uppercase or dotted names; reject up front with a clear error.
_S3_BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")

# Whitelist of AWS S3 storage classes that support instant retrieval
# (no RestoreObject + thaw needed). Round-3 finding B3-M4: prior
# implementation was a denylist of {"GLACIER", "DEEP_ARCHIVE"} which
# would silently allow GetObject through on any unrecognized tier
# (typo in lifecycle, new AWS class, etc.) — exactly the opaque-error
# surface R2-M3 was meant to prevent. A whitelist fails closed.
_INSTANT_RETRIEVAL_TIERS = frozenset({
    "STANDARD",
    "STANDARD_IA",
    "ONEZONE_IA",
    "REDUCED_REDUNDANCY",
    "INTELLIGENT_TIERING",
    "GLACIER_IR",        # Glacier Instant Retrieval — ms access
    "EXPRESS_ONEZONE",   # S3 Express One Zone
})

# Single-runner lockfile. Round-1 finding A-M6: a manual `systemctl start`
# during the daily run can collide with the scheduled run. flock prevents
# the second instance from holding the SQLite write lock simultaneously.
#
# Round-2 R2-C3: must NOT live under /tmp because the systemd unit uses
# `PrivateTmp=true` (its /tmp is namespaced and not visible to other
# processes). An ad-hoc `python3 ... state_db_s3_backup.py` from an
# operator shell would create a different lockfile under the host /tmp,
# and the two runs couldn't see each other's lock — defeating the entire
# A-M6 protection in the manual+scheduled collision case it was designed
# to prevent. /var/lock is shared across PrivateTmp namespaces and is the
# FHS-standard location for advisory lockfiles. Falls back to /tmp if
# /var/lock isn't writable (Mac / non-systemd dev environments).
_LOCK_PATH = Path("/var/lock/kalshi-state-db-backup.lock") \
    if os.access("/var/lock", os.W_OK) \
    else Path("/tmp/kalshi-state-db-backup.lock")


# ── snapshot ───────────────────────────────────────────────────────────


# Tuned per Round-1 adversarial finding A-C1: `pages=-1` (the stdlib
# default) holds the SQLite write lock for the *entire* copy, which on
# the 447 MB live DB takes 30-60s and would force every concurrent bot
# write to hit `database is locked` past the bot's 10s busy_timeout.
# Yielding every ~100 pages (~400 KB at 4 KB page size) lets writers
# interleave between batches; `sleep=0.050` is the canonical value
# from the SQLite C-API docs. Total wall-clock grows ~10-15% but the
# bot stays unblocked. Test test_snapshot_under_concurrent_writes_at_scale
# pins this behavior.
_BACKUP_PAGES_PER_STEP = 100
_BACKUP_SLEEP_BETWEEN_STEPS_S = 0.050


def snapshot_sqlite(
    src: Path,
    dst: Path,
    force: bool = False,
    pages_per_step: int = _BACKUP_PAGES_PER_STEP,
    sleep_between_steps_s: float = _BACKUP_SLEEP_BETWEEN_STEPS_S,
) -> None:
    """Snapshot `src` -> `dst` using the SQLite online backup API.

    Safe under concurrent writes — that is the entire reason this script
    exists in this form. Yields to other writers between page batches;
    see the `_BACKUP_PAGES_PER_STEP` rationale above.

    Raises:
        FileNotFoundError: if `src` doesn't exist.
        FileExistsError: if `dst` exists and `force=False`.
    """
    src = Path(src); dst = Path(dst)
    if not src.exists():
        raise FileNotFoundError(f"source database not found: {src}")
    if dst.exists() and not force:
        raise FileExistsError(
            f"destination exists (pass force=True to overwrite): {dst}"
        )
    if dst.exists() and force:
        dst.unlink()

    src_conn = sqlite3.connect(str(src))
    # Apply the same WAL pragmas the bot uses on its connections — defensive,
    # since the source connection here is read-only-by-API but a runaway
    # caller could still race the bot for the writer lock without these.
    # Also satisfies tests/integration/test_regression.py::TestBusyTimeout::test_all_sqlite_connects_have_busy_timeout
    # (PM-001: every sqlite3.connect on this codebase MUST set busy_timeout).
    src_conn.execute("PRAGMA busy_timeout=10000")
    try:
        dst_conn = sqlite3.connect(str(dst))
        # dst is a brand-new file (no contention possible), but the
        # repo-wide PM-001 regression check is mechanical — set the
        # pragma defensively so the contract holds at every connect site.
        dst_conn.execute("PRAGMA busy_timeout=10000")
        try:
            src_conn.backup(
                dst_conn,
                pages=pages_per_step,
                sleep=sleep_between_steps_s,
            )
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


# ── compression ────────────────────────────────────────────────────────


def compress(src: Path, dst: Path, algorithm: str = DEFAULT_ALGORITHM) -> None:
    """Compress `src` -> `dst` using `algorithm` ('zstd' | 'gzip').

    Round-1 m1: if `algorithm='zstd'` but the binary is absent, falls
    back to gzip with a stderr warning rather than crashing. The
    installer's preflight already warns when zstd is missing; this
    second-line defense keeps the daily timer firing on the gzip path
    instead of generating a Telegram alert flood.
    """
    src = Path(src); dst = Path(dst)
    if algorithm == "zstd" and shutil.which("zstd") is None:
        print(
            "state_db_backup: WARN zstd not installed; falling back to gzip",
            file=sys.stderr,
        )
        algorithm = "gzip"
        # Adjust the destination extension if caller passed `.zst` —
        # otherwise compute_object_key callers will mismatch the key.
        if dst.suffix == ".zst":
            dst = dst.with_suffix(".gz")
    if algorithm == "zstd":
        # `-6` is zstd's balanced level (ticket 86b9xgu9c). The original
        # Phase 0a script used `-19` (highest compression) which took
        # ~11 min wall-clock on the first manual VPS backup — unacceptable
        # for a 06:00 UTC daily cron that overlaps the next H-4 chain. -6
        # produces ~10-15% larger output for ~5-15× faster compression
        # (Silesia-corpus benchmark; SQLite sparse pages tend toward the
        # tighter end of both ranges). Pinned by
        # tests/integration/test_state_db_s3_backup.py::TestCompression::
        # test_zstd_compression_level_pinned_at_6 — do NOT revert to -19.
        # `-T0` uses all cores (1 on the VPS, harmless on a Mac).
        # `-q` suppresses the compression-ratio progress chatter.
        # No `--rm` — we cleanup the source explicitly in run_backup so a
        # failed compress + retry doesn't lose the original snapshot.
        subprocess.run(
            ["zstd", "-6", "-T0", "-q", "-o", str(dst), str(src)],
            check=True,
        )
    elif algorithm == "gzip":
        # `gzip -9 -c` writes to stdout (preserving source for retry).
        with open(dst, "wb") as out:
            subprocess.run(
                ["gzip", "-9", "-c", str(src)],
                stdout=out,
                check=True,
            )
    else:
        raise ValueError(
            f"unknown compression algorithm: {algorithm!r} (expected 'zstd' or 'gzip')"
        )


def decompress(src: Path, dst: Path, algorithm: str = DEFAULT_ALGORITHM) -> None:
    """Decompress `src` -> `dst` using `algorithm`."""
    src = Path(src); dst = Path(dst)
    if algorithm == "zstd":
        subprocess.run(
            ["zstd", "-d", "-q", "-o", str(dst), str(src)],
            check=True,
        )
    elif algorithm == "gzip":
        with open(dst, "wb") as out:
            subprocess.run(
                ["gzip", "-d", "-c", str(src)],
                stdout=out,
                check=True,
            )
    else:
        raise ValueError(
            f"unknown compression algorithm: {algorithm!r} (expected 'zstd' or 'gzip')"
        )


# ── object key routing ─────────────────────────────────────────────────


def compute_object_key(d: date, ext: str = "zst") -> str:
    """ISO-8601 zero-padded daily prefix. Lexicographic sort == date sort.

    All snapshots go under `daily/`. No weekly/monthly fan-out needed
    because the bucket's lifecycle policy retains forever and tiers to
    cheaper storage classes (see plan doc D4).
    """
    return f"daily/state-db-{d.isoformat()}.db.{ext}"


# ── pre-flight ─────────────────────────────────────────────────────────


def check_disk_space(path: Path, min_free_mb: int) -> None:
    """Refuse to proceed if `path`'s filesystem has < `min_free_mb` free.

    Snapshot + compressed file together can briefly use ~2x state.db
    worth of disk. On the VPS (25 GB droplet) this is safe; the check
    exists so a future filesystem-fill incident gets a clear error
    instead of a corrupt half-write.
    """
    usage = shutil.disk_usage(path)
    free_mb = usage.free / (1024 * 1024)
    if free_mb < min_free_mb:
        raise RuntimeError(
            f"insufficient disk space at {path}: {free_mb:.0f} MB free, "
            f"need {min_free_mb} MB"
        )


# ── BackupStore ABC + impls ────────────────────────────────────────────


class BackupStore(Protocol):
    """Storage abstraction so tests can use a tmp-dir mock instead of
    needing real S3/rclone/moto/docker. Real S3 is S3RcloneStore."""

    def put(self, local: Path, key: str) -> None: ...
    def get(self, key: str, local: Path) -> None: ...
    def list(self, prefix: str) -> List[str]: ...


class LocalDirStore:
    """File-backed BackupStore for tests + smoke runs."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def put(self, local: Path, key: str) -> None:
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, target)

    def get(self, key: str, local: Path) -> None:
        source = self.root / key
        if not source.exists():
            raise FileNotFoundError(f"key not found in store: {key}")
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, local)

    def list(self, prefix: str) -> List[str]:
        prefix_dir = self.root / prefix
        if not prefix_dir.exists():
            return []
        return sorted(
            f"{prefix}{p.name}" for p in prefix_dir.iterdir() if p.is_file()
        )


def _run_rclone(cmd: List[str]) -> subprocess.CompletedProcess:
    """Wrap subprocess.run with rclone-not-found friendly error.

    Round-1 MN4: a missing rclone binary surfaces as Telegram alert
    "FAILED: [Errno 2] No such file or directory: 'rclone'" — accurate
    but uninformative. Catch FileNotFoundError and re-raise with the
    install command embedded.
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        if "rclone" in str(e):
            raise RuntimeError(
                "rclone binary not found in PATH. "
                "Install on the VPS with: sudo apt-get install -y rclone "
                "(or single-binary: curl https://rclone.org/install.sh | sudo bash). "
                "Then re-run scripts/ops/setup_state_db_backup_timer.sh."
            ) from e
        raise


class S3RcloneStore:
    """rclone-backed S3 store. rclone is a single static binary
    (~50 MB), `apt install rclone` on Ubuntu. Config is generated by
    setup_state_db_backup_timer.sh from .env vars; this class just
    invokes the CLI.

    Why rclone (vs boto3): zero Python deps to drag into pyproject.toml
    (parallel Pillar 4/5 sessions own [dev]). Native retry + resume +
    --checksum verification. Also lets us swap to B2/DO Spaces by
    editing one rclone.conf line, no code change.
    """

    def __init__(self, remote: str, bucket: str):
        # Round-1 A-M4 / B-M6: validate bucket name early so we never
        # silently upload to a typo'd or invalid bucket name.
        if not _S3_BUCKET_NAME_RE.match(bucket):
            raise ValueError(
                f"invalid S3 bucket name {bucket!r}: must be lowercase, "
                "3-63 chars, [a-z0-9.-], start/end alphanumeric. "
                "Per AWS S3 bucket-name rules; rclone S3 backend silently "
                "mishandles names that fail this check."
            )
        self.remote = remote
        self.bucket = bucket

    def _dest(self, key: str) -> str:
        return f"{self.remote}:{self.bucket}/{key}"

    def put(self, local: Path, key: str) -> None:
        cmd = [
            "rclone", "copyto",
            "--checksum",  # verify upload via S3 ETag
            # rclone v1.55+ otherwise calls CreateBucket on every transaction
            # to "ensure" the bucket exists. With our writer-scoped IAM that
            # 403s, AND for us-east-1 specifically the CreateBucket fails with
            # InvalidLocationConstraint regardless of permission. Skip it —
            # the bucket is set up out of band by STATE_DB_BACKUP_SETUP.md.
            "--s3-no-check-bucket",
            "--retries", "3",
            "--low-level-retries", "10",
            str(local),
            self._dest(key),
        ]
        cp = _run_rclone(cmd)
        if cp.returncode != 0:
            raise RuntimeError(
                f"rclone put failed (exit {cp.returncode}): {cp.stderr.strip()}"
            )

    def storage_class(self, key: str) -> Optional[str]:
        """Return S3 storage class for `key` ('STANDARD', 'GLACIER_IR',
        'GLACIER', 'DEEP_ARCHIVE', etc.) or None if unable to determine.

        Round-1 finding B-C1: objects in GLACIER (Flexible Retrieval) and
        DEEP_ARCHIVE require RestoreObject before GetObject. GLACIER_IR
        (Instant Retrieval) does NOT — it's millisecond-accessible like
        STANDARD. The `get()` caller distinguishes these.

        Round-2 R2-M3: rclone < 1.55 doesn't surface `Tier` in lsjson
        output; this returns None, and the caller fails closed.
        """
        cmd = ["rclone", "lsjson", "--no-mimetype", self._dest(key)]
        cp = _run_rclone(cmd)
        if cp.returncode != 0:
            return None
        try:
            entries = json.loads(cp.stdout)
        except json.JSONDecodeError:
            return None
        if not entries:
            return None
        # rclone lsjson surfaces tier as 'Tier' (S3 backend) — STANDARD,
        # GLACIER, GLACIER_IR, DEEP_ARCHIVE, etc.
        return entries[0].get("Tier") or entries[0].get("StorageClass")

    def get(self, key: str, local: Path, allow_unknown_tier: bool = False) -> None:
        Path(local).parent.mkdir(parents=True, exist_ok=True)

        # Pre-flight tier check (Round-1 B-C1; Round-2 R2-M3 + R2-M4;
        # Round-3 B3-M4 — invert from denylist of GLACIER+DEEP_ARCHIVE
        # to a WHITELIST of known-instant tiers, so any future AWS
        # storage class fails closed instead of silently falling
        # through to opaque copyto errors).
        sc = self.storage_class(key)
        if sc is None:
            if not allow_unknown_tier:
                raise RuntimeError(
                    f"could not determine storage class for {key!r}. "
                    "rclone lsjson returned no Tier metadata — likely an "
                    "rclone version < 1.55, or a transient network failure. "
                    "Upgrade rclone (`apt install --only-upgrade rclone`) "
                    "and retry, OR pass --allow-unknown-tier to proceed "
                    "anyway (a copyto from a Glacier-tiered key will fail "
                    "with InvalidObjectState if it IS tiered)."
                )
        else:
            sc_upper = sc.upper()
            if sc_upper not in _INSTANT_RETRIEVAL_TIERS:
                # Either a known thaw-required tier (GLACIER, DEEP_ARCHIVE)
                # OR a tier the whitelist doesn't recognize (new AWS class,
                # typo in lifecycle config, etc.). Both fail closed.
                if sc_upper == "DEEP_ARCHIVE":
                    wait_msg = "12 hours (DEEP_ARCHIVE)"
                elif sc_upper == "GLACIER":
                    wait_msg = "3-5 hours (GLACIER Flexible Retrieval)"
                else:
                    wait_msg = "(check AWS storage-class docs — tier not in instant-retrieval whitelist)"
                raise RuntimeError(
                    f"key {key!r} is in storage class {sc!r}; rclone GetObject "
                    f"will fail with InvalidObjectState OR storage class is "
                    f"unknown to this script (whitelist: {sorted(_INSTANT_RETRIEVAL_TIERS)}). "
                    f"Restore first via:\n"
                    f"  aws s3api restore-object \\\n"
                    f"    --bucket {self.bucket} \\\n"
                    f"    --key {key} \\\n"
                    f"    --restore-request '{{\"Days\":7,\"GlacierJobParameters\":{{\"Tier\":\"Standard\"}}}}'\n"
                    f"Then wait {wait_msg} before re-running this restore. "
                    f"See scripts/STATE_DB_BACKUP_SETUP.md §9 for the full procedure. "
                    f"If the tier is novel and instant-accessible, re-run with --allow-unknown-tier."
                )

        cmd = [
            "rclone", "copyto",
            "--checksum",
            "--s3-no-check-bucket",  # see put() — same rationale on restore
            "--retries", "3",
            self._dest(key),
            str(local),
        ]
        cp = _run_rclone(cmd)
        if cp.returncode != 0:
            raise RuntimeError(
                f"rclone get failed (exit {cp.returncode}): {cp.stderr.strip()}"
            )

    def list(self, prefix: str) -> List[str]:
        cmd = [
            "rclone", "lsf",
            "--files-only",
            self._dest(prefix),
        ]
        cp = _run_rclone(cmd)
        if cp.returncode != 0:
            raise RuntimeError(
                f"rclone list failed (exit {cp.returncode}): {cp.stderr.strip()}"
            )
        names = [n for n in cp.stdout.split("\n") if n.strip()]
        return sorted(f"{prefix}{n}" for n in names)


# ── orchestration ──────────────────────────────────────────────────────


@dataclasses.dataclass
class BackupResult:
    key: str
    bytes_uncompressed: int
    bytes_uploaded: int
    duration_s: float


@contextlib.contextmanager
def _single_runner_lock(lock_path: Optional[Path] = None):
    """flock-based single-runner guard. Round-1 finding A-M6.

    A second concurrent backup (manual + scheduled) would both run
    sqlite3.Connection.backup() against the same live DB, doubling
    SQLite contention. systemd Type=oneshot prevents same-unit double-
    fire but doesn't catch ad-hoc invocations. flock LOCK_EX + LOCK_NB
    fails fast if another runner holds the lock.

    Default `_LOCK_PATH` is read at call-time (not function-def time)
    so tests can monkeypatch the module-level constant.
    """
    if lock_path is None:
        lock_path = _LOCK_PATH
    fd = None
    try:
        # Round-3 lens-A M2: lockfile content is just the PID — not
        # sensitive. Mode 0o666 + post-open fchmod prevents lockout
        # when an ad-hoc root-run incident-debug invocation creates
        # the lockfile, then subsequent botuser scheduled runs fail
        # with EACCES on open before flock is even attempted.
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o666)
        try:
            os.fchmod(fd, 0o666)  # ensure existing file's mode is permissive
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise RuntimeError(
                f"another state.db backup is in progress (lock {lock_path} held); "
                "refusing to run concurrently. Wait for the prior run to finish."
            ) from e
        # Write our PID for forensic purposes.
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


def run_backup(
    db_path: Path,
    store: BackupStore,
    today: Optional[date] = None,
    tmp_dir: Optional[Path] = None,
    algorithm: str = DEFAULT_ALGORITHM,
    min_free_mb: int = DEFAULT_MIN_FREE_MB,
    use_lock: bool = True,
) -> BackupResult:
    """Snapshot -> compress -> upload. Cleans up tmp files unconditionally
    (success OR failure). Caller is responsible for top-level retries +
    Telegram alerting (h4_run_with_alert.py wraps non-zero exit codes).

    `use_lock=False` is for tests only — production callers always lock.
    """
    db_path = Path(db_path)
    today = today or datetime.now(timezone.utc).date()

    # Track whether we own the tmp dir (so we know to rmtree it after).
    # Round-1 finding A-M7: prior implementation called mkdtemp() but
    # only unlinked files inside, leaking empty dirs forever in /tmp.
    owns_tmp_dir = tmp_dir is None
    tmp_dir = Path(tmp_dir) if tmp_dir else Path(tempfile.mkdtemp(prefix="state_db_backup_"))
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Compression-fallback can rewrite the algorithm; resolve key + ext
    # AFTER snapshot+compress so the key matches what we actually uploaded.
    snap_path = tmp_dir / f"state-db-{today.isoformat()}.db"

    lock_cm = _single_runner_lock() if use_lock else contextlib.nullcontext()
    started = time.monotonic()
    actual_compressed_path: Optional[Path] = None
    actual_key: Optional[str] = None

    try:
        with lock_cm:
            check_disk_space(tmp_dir, min_free_mb=min_free_mb)
            snapshot_sqlite(db_path, snap_path, force=True)
            bytes_uncompressed = snap_path.stat().st_size

            # Resolve algorithm + extension AFTER potential zstd-fallback
            # (compress() may quietly switch to gzip if zstd is missing).
            effective_algorithm = (
                "gzip" if (algorithm == "zstd" and shutil.which("zstd") is None)
                else algorithm
            )
            ext = "zst" if effective_algorithm == "zstd" else "gz"
            actual_key = compute_object_key(today, ext=ext)
            actual_compressed_path = tmp_dir / f"state-db-{today.isoformat()}.db.{ext}"

            compress(snap_path, actual_compressed_path, algorithm=algorithm)
            bytes_uploaded = actual_compressed_path.stat().st_size
            store.put(actual_compressed_path, actual_key)
            duration_s = time.monotonic() - started
            return BackupResult(
                key=actual_key,
                bytes_uncompressed=bytes_uncompressed,
                bytes_uploaded=bytes_uploaded,
                duration_s=duration_s,
            )
    finally:
        # Always clean up. Repeated cron failures must not fill the disk.
        for p in (snap_path, actual_compressed_path):
            if p is None:
                continue
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
        # Also remove any other unexpected leftovers (e.g., a half-written
        # `.zst` that compress() wrote before failing).
        if owns_tmp_dir:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except OSError:
                pass


# ── CLI ────────────────────────────────────────────────────────────────


def _resolve_store(args) -> BackupStore:
    if args.store == "s3":
        bucket = args.bucket or os.environ.get("S3_BACKUP_BUCKET", "").strip()
        if not bucket:
            raise SystemExit(
                "FAIL: --bucket not given and S3_BACKUP_BUCKET not set in env"
            )
        return S3RcloneStore(remote=args.rclone_remote, bucket=bucket)
    elif args.store == "local":
        if not args.local_root:
            raise SystemExit("FAIL: --local-root required when --store=local")
        return LocalDirStore(Path(args.local_root))
    else:
        raise SystemExit(f"FAIL: unknown --store {args.store!r}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--db", required=True, type=Path,
                   help="path to live state.db on the VPS")
    p.add_argument("--store", choices=("s3", "local"), default="s3",
                   help="storage backend (default: s3)")
    p.add_argument("--bucket", default=None,
                   help="S3 bucket name (default: $S3_BACKUP_BUCKET)")
    p.add_argument("--rclone-remote", default=DEFAULT_RCLONE_REMOTE,
                   help=f"rclone remote name (default: {DEFAULT_RCLONE_REMOTE})")
    p.add_argument("--local-root", default=None, type=Path,
                   help="root dir for --store=local (testing/staging only)")
    p.add_argument("--algorithm", choices=("zstd", "gzip"), default=DEFAULT_ALGORITHM,
                   help=f"compression algorithm (default: {DEFAULT_ALGORITHM})")
    p.add_argument("--min-free-mb", type=int, default=DEFAULT_MIN_FREE_MB,
                   help=f"abort if free disk < N MB (default: {DEFAULT_MIN_FREE_MB})")
    p.add_argument("--tmp-dir", default=None, type=Path,
                   help="working dir for snapshot+compressed files (default: mkdtemp)")
    p.add_argument("--date", default=None,
                   help="override today's date (YYYY-MM-DD); useful for retries")
    args = p.parse_args(argv)

    store = _resolve_store(args)
    today = date.fromisoformat(args.date) if args.date else None

    try:
        result = run_backup(
            db_path=args.db,
            store=store,
            today=today,
            tmp_dir=args.tmp_dir,
            algorithm=args.algorithm,
            min_free_mb=args.min_free_mb,
        )
    except Exception as e:
        print(f"state_db_backup: FAILED: {e}", file=sys.stderr)
        return 1

    ratio = result.bytes_uncompressed / max(1, result.bytes_uploaded)
    print(
        f"state_db_backup: OK key={result.key} "
        f"uncompressed={result.bytes_uncompressed:,}B "
        f"uploaded={result.bytes_uploaded:,}B "
        f"ratio={ratio:.1f}x "
        f"duration={result.duration_s:.1f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
