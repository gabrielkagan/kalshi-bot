"""rclone upload + verify + delete-local — D1.2 (ticket 86b9ypn66, 2026-05-16).

D0.3 §7 contract (load-bearing — mirrors the existing
``scripts/ops/journal_archives_s3_sync.py`` precedent from commit
``635afbf``):

  1. fsync the in-flight JSONL (writer side)
  2. zstd-compress level 6 to tmp/ (writer side)
  3. atomic ``os.replace`` to outbox/ (writer side)
  4. ``rclone copyto --checksum --immutable`` to S3 (uploader side)
  5. verify ``rclone size --json`` matches local stat (uploader side)
  6. ONLY THEN delete the local outbox + in-flight files (uploader side)

KEEP-local posture on rclone non-zero / size mismatch. ``--immutable``
guards against silent overwrites (returns exit-6 on divergence).
``copyto`` NOT ``sync`` — sync would mirror-delete S3 objects when local
files age out (catastrophic — journal_archives R1 C1 incident class).

``copyto`` (not ``copy``) is the file-to-file primitive: ``rclone copy``
treats the destination as a DIRECTORY when source is a file, which
would store ``chunk.jsonl.zst`` at ``s3://bucket/.../chunk.jsonl.zst/chunk.jsonl.zst``.
``copyto`` preserves the exact dest path.

NOTE on disk-pressure failure mode (D0.3 §6 last bullet): sustained
upload-stall accumulation can fill the bot VPS root volume in <1 hour.
D1.6 ships a passive ``shutil.disk_usage`` ≥ 80%-used Telegram alert
(via ``scripts/ops/collector_health_monitor.py``, operator-installed
cron every 5 min) — chosen over rclone-event grepping because the
disk-% watermark trips well before disk-full at typical chunk cadence
(D0.3 §6 off-peak: ~21 MB/min ⇒ ~19 min headroom from 80% threshold to
disk-full); at sustained-stall rates (D0.3 §6: ~21 GB/hour ≈ ~350
MB/min) the 5-min cron interval cannot precede disk-full (accepted
trade-off vs tighter polling cadence — the failure mode is delayed
alert, not data-loss beyond what disk-full would cause anyway).
The ERROR log emitted on rclone failure remains a useful diagnostic
for triage (operator can `journalctl -u kalshi-collector | grep ERROR`
to surface the specific rclone exit code) — but is NOT the D1.6 alert
hook.

Contract pins:
- tests/contracts/test_collector_uploader.py — flag pinning (copyto +
  --checksum + --immutable + NO sync), KEEP-local on rclone non-zero,
  KEEP-local on size-mismatch, delete-both on success, ERROR log
  on failure.
- tests/contracts/test_collector_idempotency.py — sweep_outbox restart
  semantics (re-upload leftover chunks on startup).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Subcommand chosen for file-to-file semantics — see module docstring.
# Test pins this as a substring-check ("copy" in "copyto") to ensure the
# `sync` catastrophe-class (R1 C1, ticket 86b9xgp7k journal_archives) is
# structurally impossible.
_RCLONE_SUBCOMMAND_UPLOAD: str = "copyto"
_RCLONE_SUBCOMMAND_VERIFY: str = "size"


def build_rclone_copy_argv(local_path: Path, s3_dest: str) -> List[str]:
    """Pure-function rclone upload argv. Test pins every load-bearing flag.

    Flags chosen to mirror ``scripts/ops/journal_archives_s3_sync.py``
    (commit ``635afbf``):

    - ``--checksum``: idempotent re-uploads via ETag short-circuit (survives
      mtime drift across collector restarts).
    - ``--immutable``: divergence-alarm guard. Exit-6 if local content
      differs from same-name S3 object — surfaces tampering or writer bugs.
    - ``--s3-no-check-bucket``: writer IAM has no CreateBucket; us-east-1
      rejects LocationConstraint.
    - ``--retries 3 --low-level-retries 10``: transient-network resilience.
    """
    return [
        "rclone", _RCLONE_SUBCOMMAND_UPLOAD,
        "--checksum",
        "--immutable",
        "--s3-no-check-bucket",
        "--retries", "3",
        "--low-level-retries", "10",
        str(local_path),
        s3_dest,
    ]


def build_rclone_size_argv(s3_dest: str) -> List[str]:
    """Pure-function rclone size-verify argv. ``--json`` for parseable output.

    Used in step 5 of D0.3 §7 to confirm S3 object byte-count matches local
    file size before the delete-local step.
    """
    return [
        "rclone", _RCLONE_SUBCOMMAND_VERIFY,
        "--json",
        s3_dest,
    ]


class RcloneUploader:
    """Upload bronze chunks to S3, verify, then delete local.

    KEEP-local on any failure — this is the data-loss seam where
    inverting the contract silently loses bronze. See D0.3 §7 step 6.
    """

    def __init__(self, rclone_remote: str, bucket: str) -> None:
        self.rclone_remote = rclone_remote
        self.bucket = bucket

    def _s3_dest(self, s3_key: str) -> str:
        return f"{self.rclone_remote}:{self.bucket}/{s3_key}"

    def upload_chunk(
        self,
        outbox_path: Path,
        in_flight_path: Optional[Path],
        s3_key: str,
    ) -> bool:
        """Upload one chunk + verify + delete-local on success.

        Returns True on success (both rclone copyto and size-verify
        succeeded; local files deleted). Returns False on any failure
        (local files preserved).
        """
        s3_dest = self._s3_dest(s3_key)
        local_size = outbox_path.stat().st_size

        # Step 4 — rclone copyto.
        copy_argv = build_rclone_copy_argv(outbox_path, s3_dest)
        copy_result = subprocess.run(
            copy_argv,
            capture_output=True,
            text=True,
        )
        if copy_result.returncode != 0:
            logger.error(
                "rclone %s exited %d for %s: stderr=%r",
                _RCLONE_SUBCOMMAND_UPLOAD,
                copy_result.returncode,
                s3_dest,
                copy_result.stderr,
            )
            return False

        # Step 5 — rclone size verify.
        size_argv = build_rclone_size_argv(s3_dest)
        size_result = subprocess.run(
            size_argv,
            capture_output=True,
            text=True,
        )
        if size_result.returncode != 0:
            logger.error(
                "rclone size exited %d for %s after successful copy: stderr=%r",
                size_result.returncode,
                s3_dest,
                size_result.stderr,
            )
            return False

        try:
            size_payload = json.loads(size_result.stdout)
            s3_bytes = int(size_payload["bytes"])
        except (ValueError, KeyError, TypeError) as exc:
            logger.error(
                "rclone size returned unparseable JSON for %s: %r (stdout=%r)",
                s3_dest, exc, size_result.stdout,
            )
            return False

        if s3_bytes != local_size:
            logger.error(
                "size mismatch for %s: local=%d, s3=%d — KEEP-local invoked",
                s3_dest, local_size, s3_bytes,
            )
            return False

        # Step 6 — delete BOTH outbox + in-flight. Only now.
        try:
            outbox_path.unlink()
        except FileNotFoundError:
            pass
        if in_flight_path is not None:
            try:
                in_flight_path.unlink()
            except FileNotFoundError:
                pass
        return True

    def sweep_outbox(self, root_dir: Path) -> int:
        """Re-upload any leftover outbox/*.jsonl.zst files under root_dir.

        Per D0.3 §7 last paragraph: "On collector restart, any leftover
        outbox/ files are re-uploaded before new rotations begin —
        bit-identical re-uploads no-op via --checksum."

        For each outbox chunk, the matching in-flight .jsonl shares the
        same chunk_id stem (per BronzeWriter._rotate's rename step) and
        lives in the sibling in_flight/ dir. We pass both to upload_chunk
        so the delete-on-success step removes both files.

        Returns the number of chunks successfully re-uploaded.
        """
        n_ok = 0
        root_dir = Path(root_dir)
        for outbox_dir in root_dir.rglob("outbox"):
            if not outbox_dir.is_dir():
                continue
            for chunk in outbox_dir.glob("*.jsonl.zst"):
                # s3_key is the chunk path relative to root_dir, prepended
                # with the "bronze/" prefix that D0.3 §3 reserves for the
                # raw tier. Strip the /outbox/ segment so silver ETL sees
                # one flat hive partition under bronze/.
                rel = chunk.relative_to(root_dir).as_posix()
                s3_key = "bronze/" + rel.replace("/outbox/", "/")
                # Paired in-flight shares the chunk_id stem (BronzeWriter
                # ._rotate renamed it from the wall-clock-µs name to
                # <chunk_id>.jsonl at rotation time). Tolerate-absent for
                # legacy chunks left over from pre-D1.2 collector boots.
                in_flight_path = (
                    outbox_dir.parent
                    / "in_flight"
                    / chunk.name.replace(".jsonl.zst", ".jsonl")
                )
                in_flight_arg: Optional[Path] = (
                    in_flight_path if in_flight_path.exists() else None
                )
                if self.upload_chunk(
                    outbox_path=chunk,
                    in_flight_path=in_flight_arg,
                    s3_key=s3_key,
                ):
                    n_ok += 1
        return n_ok
