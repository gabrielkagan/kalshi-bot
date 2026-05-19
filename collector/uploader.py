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

B-orphan-sweep AMENDMENT 2026-05-19 (ticket 86ba0jmz9): the 6-step
contract above describes the GRACEFUL path. When the collector process
is SIGKILL'd / OOM-killed, the writer's ``writer.close()`` in
collector/main_loop.py's finally block does NOT run, leaving bare
``in_flight/in_flight_<usec>.jsonl`` orphans (un-rotated, un-renamed,
un-paired with any outbox/.jsonl.zst). The restart-sweep contract is
EXTENDED symmetrically: ``sweep_outbox(root_dir)`` first invokes
``salvage_in_flight_orphans(root_dir)``, which re-rotates each
salvageable orphan into the standard outbox + in_flight pair before
the existing outbox loop runs. See ``salvage_in_flight_orphans``
docstring for safety rules (90s mtime threshold, empty-orphan delete,
malformed KEEP-local, chunk_id collision preserve).

Contract pins (B-orphan-sweep amendment):
- tests/contracts/test_collector_in_flight_recovery.py — salvage path
  + chunk_id collision ratchet (R1-C1/C2) + safety-age + zstd round-
  trip + empty/malformed handling.
- The salvage zstd level MUST match the writer's. Pinned by the
  module-level ``_SALVAGE_ZSTD_LEVEL == writer._ZSTD_LEVEL`` constant
  + a behavioral test that proves byte-identical output for the same
  input across both producers (test_salvage_zstd_level_matches_writer).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import zstandard as zstd

# Single-source the salvage zstd level by importing the writer's pinned
# constant. Drift would break the silver-ETL byte-identical guarantee
# (R2-M6 structural ratchet — pre-fix this was a copy in module scope
# with only an honor-system comment claiming it matched).
from collector.writer import _ZSTD_LEVEL as _WRITER_ZSTD_LEVEL

logger = logging.getLogger(__name__)

# Subcommand chosen for file-to-file semantics — see module docstring.
# Test pins this as a substring-check ("copy" in "copyto") to ensure the
# `sync` catastrophe-class (R1 C1, ticket 86b9xgp7k journal_archives) is
# structurally impossible.
_RCLONE_SUBCOMMAND_UPLOAD: str = "copyto"
_RCLONE_SUBCOMMAND_VERIFY: str = "size"

# B-orphan-sweep (2026-05-19) — in_flight salvage tunables.
#
# Don't touch files whose mtime is within this many seconds of "now" —
# they could be the active writer's still-open first-frame in_flight.
# Writer's normal rotation cadence is 5 min; 90s gives a healthy margin
# while still salvaging post-crash orphans (mtime is the wall-clock of
# the LAST write to the file; if no writer has written for 90s+, the
# previous owner is gone — either crashed or rotated cleanly).
_IN_FLIGHT_SAFETY_AGE_SECONDS: float = 90.0

# Salvage zstd level is single-sourced from the writer via the import
# above (R2-M6 structural ratchet). The alias remains so the rest of
# the module reads as ``_SALVAGE_ZSTD_LEVEL`` (intent-clear locally)
# while drift is structurally impossible — re-binding the writer's
# constant would break ``_WRITER_ZSTD_LEVEL`` import everywhere it's
# read, and the contract test ``test_salvage_zstd_level_matches_writer``
# pins the equality behaviorally on top of the static lock-step.
_SALVAGE_ZSTD_LEVEL: int = _WRITER_ZSTD_LEVEL


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

        Per D0.3 §7 last paragraph (B-orphan-sweep AMENDMENT 2026-05-19,
        ticket 86ba0jmz9): "On collector restart, any leftover outbox/
        files are re-uploaded before new rotations begin — bit-identical
        re-uploads no-op via --checksum." The amendment extends symmetry
        to the in_flight side: bare-``in_flight_<usec>.jsonl`` orphans
        are also recovered via ``salvage_in_flight_orphans`` BEFORE the
        outbox loop iterates.

        For each outbox chunk, the matching in-flight .jsonl shares the
        same chunk_id stem (per BronzeWriter._rotate's rename step) and
        lives in the sibling in_flight/ dir. We pass both to upload_chunk
        so the delete-on-success step removes both files.

        B-orphan-sweep (2026-05-19): before iterating outbox/, salvage any
        bare-``in_flight_<usec>.jsonl`` orphans left behind by SIGKILL/OOM-
        killed prior processes (whose ``writer.close()``-in-finally never
        ran). Salvage re-rotates each orphan into the standard
        ``outbox/{chunk_id}.jsonl.zst`` + ``in_flight/{chunk_id}.jsonl``
        pair, after which the existing outbox loop below picks them up
        identically to a graceful-rotate chunk. See the salvage method
        docstring for parsing + safety details.

        Returns the number of chunks successfully re-uploaded.
        """
        n_ok = 0
        root_dir = Path(root_dir)
        # Step 0: salvage in_flight orphans into the outbox shape so the
        # main loop below treats them identically to graceful rotations.
        self.salvage_in_flight_orphans(root_dir)
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

    def salvage_in_flight_orphans(self, root_dir: Path) -> int:
        """Re-rotate bare-``in_flight_<usec>.jsonl`` orphans into outbox.

        B-orphan-sweep (2026-05-19, ticket 86ba0jmz9). Background: when
        the collector process is SIGKILL'd or OOM-killed by systemd, the
        graceful ``writer.close()`` in main_loop's finally block does
        NOT run. Any open in_flight files are left on disk with their
        pre-rotation filenames (``in_flight_<wall-clock-µs>.jsonl``) and
        are invisible to the chunk_id-stem-based pairing in
        ``sweep_outbox``. This method walks the tree, re-rotates each
        salvageable orphan into the standard
        ``outbox/{chunk_id}.jsonl.zst`` + ``in_flight/{chunk_id}.jsonl``
        shape that ``sweep_outbox`` expects, then returns.

        The salvage is defensive and applies UNIFORMLY across all three
        collector services (kalshi-collector, kalshi-coinbase-collector,
        kalshi-weather-collector). Each service holds its own
        ``bronze_root`` so the rglob walks are partition-isolated; the
        observed leak in the Kalshi service is one instance of the same
        latent class on all three.

        Safety rules:
        - Files with mtime within ``_IN_FLIGHT_SAFETY_AGE_SECONDS`` (90s)
          of "now" are skipped — they could belong to an active writer
          whose first frame is still mid-rotation interval. The writer's
          5-min rotation cadence means this window is large enough to
          identify a truly-orphaned file with healthy margin.
        - Empty (0-byte) orphans have no derivable chunk_id; they are
          unlinked + logged.
        - Malformed orphans (non-JSON content / unparseable envelopes)
          are LOGGED + PRESERVED on disk for operator triage. Silent
          data-loss-on-corruption is worse than disk-pressure.
        - chunk_id collision (R1-C1/C2 ratchet): if salvage would
          produce an ``outbox/{chunk_id}.jsonl.zst`` or
          ``in_flight/{chunk_id}.jsonl`` that ALREADY exists on disk
          (e.g., a prior boot's graceful rotation left a canonical pair,
          AND a subsequent SIGKILL produced an orphan with same first/
          last frame ts+seq derivation), the orphan is LOGGED + PRESERVED
          rather than clobbering the canonical chunk. Pre-fix this was a
          silent-overwrite bronze-loss class.

        Returns the number of orphans successfully salvaged into outbox.
        """
        n_salvaged = 0
        now = time.time()
        root_dir = Path(root_dir)
        for in_flight_dir in root_dir.rglob("in_flight"):
            if not in_flight_dir.is_dir():
                continue
            # Glob ONLY the bare-µs pattern, not chunk_id-named files
            # (those are paired with outbox/ chunks and handled by the
            # sweep_outbox loop).
            for orphan in in_flight_dir.glob("in_flight_*.jsonl"):
                try:
                    if self._salvage_one_orphan(orphan, now=now):
                        n_salvaged += 1
                except Exception:
                    # Per-orphan failure must not block other orphans.
                    # Log with traceback for operator triage; KEEP-local
                    # since the file is still on disk.
                    logger.exception(
                        "salvage_in_flight_orphans: unexpected error "
                        "for %s — file preserved for manual triage.",
                        orphan,
                    )
        # R3-m1: ensure salvage activity is operator-visible WITHOUT
        # requiring every caller to read the return value + log it. A
        # bare INFO when n>0 prevents the "silent salvage" failure mode
        # the R3 reviewer flagged (the operator-facing "%d leftover
        # outbox chunks" log only counts upload-success, not salvage).
        if n_salvaged:
            logger.info(
                "salvage_in_flight_orphans: re-rotated %d in_flight "
                "orphan(s) into outbox shape under %s",
                n_salvaged, root_dir,
            )
        return n_salvaged

    def _salvage_one_orphan(self, orphan: Path, *, now: float) -> bool:
        """Re-rotate a single bare-µs orphan into outbox/in_flight shape.

        Returns True if and only if a NEW outbox file was created AND
        the orphan was renamed to its chunk_id-stemmed pair (i.e., a
        real bronze data salvage). All other paths (fresh-skip / empty-
        delete / whitespace-delete / malformed-keep / collision-keep)
        return False so the caller's counter reflects only true
        salvages, not cleanup operations (R2-m1 honest-counter ratchet).

        Side-effects:
        - On True: writes ``outbox/{chunk_id}.jsonl.zst`` (atomic) +
          renames the orphan to ``in_flight/{chunk_id}.jsonl``.
        - On False with empty/whitespace input: orphan unlinked.
        - On False with malformed/collision/fresh input: orphan
          preserved on disk for next-pass retry or manual triage.
        """
        # Safety: skip files an active writer might still be appending.
        try:
            mtime = orphan.stat().st_mtime
        except FileNotFoundError:
            return False  # raced; another sweep handled it.
        if now - mtime < _IN_FLIGHT_SAFETY_AGE_SECONDS:
            return False

        raw_bytes = orphan.read_bytes()
        if not raw_bytes:
            # Empty orphan — no chunk_id derivable; clean up the inode.
            try:
                orphan.unlink()
                logger.info("salvage: empty in_flight orphan deleted: %s", orphan)
            except FileNotFoundError:
                pass
            return False

        # Parse first + last non-empty lines for ts + seq. KEEP-local on
        # any parse failure — operator triage > silent loss. ``.strip()``
        # filters whitespace-only lines (R1-n1 hardening; defensive
        # against any non-writer producer that might inject blank lines).
        lines = [b for b in raw_bytes.split(b"\n") if b.strip()]
        if not lines:
            try:
                orphan.unlink()
                logger.info("salvage: whitespace-only in_flight orphan deleted: %s", orphan)
            except FileNotFoundError:
                pass
            return False
        try:
            first_env = json.loads(lines[0].decode("utf-8"))
            last_env = json.loads(lines[-1].decode("utf-8"))
            first_ts = _parse_wire_recv_ts(first_env["_wire_recv_ts"])
            last_ts = _parse_wire_recv_ts(last_env["_wire_recv_ts"])
            first_seq = int(first_env["_collector_seq"])
            last_seq = int(last_env["_collector_seq"])
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            logger.warning(
                "salvage: malformed in_flight orphan %s (%r) — preserved "
                "for manual triage.",
                orphan, exc,
            )
            return False

        chunk_id = (
            f"{_format_compact_iso(first_ts)}"
            f"_to_{_format_compact_iso(last_ts)}"
            f"_seq{first_seq}-{last_seq}"
        )
        partition = orphan.parent.parent  # strip in_flight/ + filename
        tmp_dir = partition / "tmp"
        outbox_dir = partition / "outbox"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        outbox_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / f"{chunk_id}.jsonl.zst.tmp"
        outbox_path = outbox_dir / f"{chunk_id}.jsonl.zst"
        renamed_in_flight = orphan.parent / f"{chunk_id}.jsonl"

        # R1-C1/C2 ratchet: chunk_id collision pre-check. Writer's
        # ``_format_compact_iso`` is second-resolution + ``_collector_seq``
        # resets across process restarts, so two boots within the same
        # wall-clock-second that ingest overlapping seq ranges CAN derive
        # the same chunk_id. If a canonical pair already exists on disk
        # (graceful rotation from a prior boot), refuse to clobber —
        # silent overwrite of canonical bronze with partial orphan
        # content would be a data-loss class equivalent to the bug this
        # method is fixing.
        if outbox_path.exists() or renamed_in_flight.exists():
            logger.warning(
                "salvage: chunk_id collision for orphan=%s "
                "(would clobber outbox=%s in_flight=%s); preserving "
                "orphan for manual triage.",
                orphan.name, outbox_path, renamed_in_flight,
            )
            return False

        # Compress + fsync + atomic-rename to outbox. Same zstd level
        # as the writer so silver ETL sees byte-identical compressed
        # bronze regardless of recovery path.
        cctx = zstd.ZstdCompressor(level=_SALVAGE_ZSTD_LEVEL)
        with open(tmp_path, "wb") as dst:
            dst.write(cctx.compress(raw_bytes))
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp_path, outbox_path)

        # Rename orphan → chunk_id-stemmed in_flight so the sweep_outbox
        # pairing loop finds it via its name-based lookup. Log both the
        # ORIGINAL wall-clock-µs filename AND the recovered chunk_id so
        # operators can correlate against journalctl OOM-kill traces
        # (R1-m2 forensic-anchor hardening).
        orphan_original_name = orphan.name
        os.replace(orphan, renamed_in_flight)
        logger.info(
            "salvage: in_flight orphan recovered orphan=%s → outbox=%s "
            "(%d bytes raw, %d bytes compressed)",
            orphan_original_name, outbox_path,
            len(raw_bytes), outbox_path.stat().st_size,
        )
        return True


# ─── Salvage helpers (module-private) ────────────────────────────────────────


def _parse_wire_recv_ts(ts_str: str) -> datetime:
    """Mirror of BronzeWriter's ts-parse — tz-aware UTC from ISO-µs string.

    Kept local to avoid an uploader→writer import edge that would tighten
    the import-linter graph beyond what this Bit needs.
    """
    return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc,
    )


def _format_compact_iso(ts: datetime) -> str:
    """Mirror of BronzeWriter._format_compact_iso for chunk_id naming.

    Local copy avoids an uploader→writer import edge. The pinned format
    ``YYYYMMDDTHHMMSSZ`` is single-sourced via the contract test that
    asserts the salvage chunk_id stem matches the writer's stem given
    the same first/last frame ts.
    """
    return ts.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
