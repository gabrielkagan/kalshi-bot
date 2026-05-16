"""D1.2 — collector/uploader.py rclone contract (ticket 86b9ypn66, 2026-05-16).

Pins the §7 contract: fsync → zstd-6 to tmp/ → atomic os.replace to outbox/ →
``rclone copy --checksum --immutable`` → ``rclone size`` verify → THEN delete
local. KEEP-local on any non-zero exit / size mismatch — the CATASTROPHIC
seam where bronze data loss happens if inverted.

Mirrors the ``test_journals_s3_sync.py``-style flag pinning from the
journal-archives precedent (commit 635afbf, R1 C1 "copy not sync").

Sister to ``test_collector_writer.py`` + ``test_collector_rotation.py`` +
``test_collector_idempotency.py``.

If this test fails:
- A new rclone flag was added/removed: update D0.3 §7 + this test atomically.
- KEEP-local-on-failure is broken: STOP. Don't deploy. This is the data-
  loss seam. Read §7 "Why each step" before proceeding.
- `sync` snuck in instead of `copy`: this is the journal-archives R1 C1
  catastrophic-failure-mode again. NEVER substitute.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from collector.uploader import (
    RcloneUploader,
    build_rclone_copy_argv,
    build_rclone_size_argv,
)


# ─── 1. Flag pin — copy + --checksum + --immutable + NOT sync ───────────────


def test_rclone_argv_uses_copy_not_sync():
    """``rclone copy`` (NOT ``sync``). The journal-archives R1 C1 catch:
    ``rclone sync`` mirror-deletes the destination when local files age out.
    For bronze, rotation deletes local files AFTER upload-success; if we
    used ``sync``, the next sync after that delete would erase the S3 object.

    Per D0.3 §7 "Why each step":
      > rclone copy not sync — sync mirror-deletes, would erase S3 objects
      > when local files age out. Per the R1 C1 catch on the existing
      > journals sync ticket, this is the catastrophic-failure-mode-to-avoid.
    """
    argv = build_rclone_copy_argv(
        local_path=Path("/tmp/fake_outbox/chunk.jsonl.zst"),
        s3_dest="s3prod:kalshi-bot-archive/bronze/kalshi_ws/orderbook_delta/year=2026/month=05/day=16/hour=14/conn=A/chunk.jsonl.zst",
    )
    assert "rclone" in argv[0] or argv[0] == "rclone"
    # `copy` substring permits `copyto` (the file-to-file primitive —
    # rclone-copy treats a file dest as a directory). The intent here is
    # "copy semantics, NOT sync".
    assert any("copy" in tok for tok in argv), (
        f"argv must use a copy-family subcommand (copy or copyto), got: {argv}"
    )
    assert "sync" not in argv, (
        f"argv contains `sync` — D0.3 §7 forbids; would mirror-delete S3 "
        f"objects when local files age out. argv: {argv}"
    )


def test_rclone_argv_has_checksum_and_immutable():
    """Both ``--checksum`` and ``--immutable`` flags present per D0.3 §7.

    --checksum: bit-exact upload verification by content hash; survives
                any clock skew or mtime weirdness.
    --immutable: exits with code 6 if a local file's content differs
                 from a same-name S3 object — alerts on tampering or
                 writer bugs that produce divergent re-uploads.
    """
    argv = build_rclone_copy_argv(
        local_path=Path("/tmp/chunk.jsonl.zst"),
        s3_dest="s3prod:bucket/key.jsonl.zst",
    )
    assert "--checksum" in argv, (
        f"missing --checksum flag — D0.3 §7 requires bit-exact ETag "
        f"verification. argv: {argv}"
    )
    assert "--immutable" in argv, (
        f"missing --immutable flag — D0.3 §7 uses this as the divergence-"
        f"alarm guard (exit-6 on content mismatch). argv: {argv}"
    )


def test_rclone_size_argv_is_size_subcommand():
    """`rclone size` is the verify primitive (D0.3 §7 step 5)."""
    argv = build_rclone_size_argv(
        s3_dest="s3prod:bucket/key.jsonl.zst",
    )
    assert "size" in argv, f"argv must use `size` subcommand, got: {argv}"


# ─── 2. KEEP-local discipline on rclone non-zero (THE data-loss seam) ───────


def test_keep_local_when_rclone_exits_non_zero(tmp_path: Path):
    """If rclone copy exits non-zero, the local outbox file MUST NOT be
    deleted. KEEP-local is the contract that prevents data loss on
    transient S3 errors.

    Mutation harness: monkeypatch subprocess.run to return a non-zero
    exit. Assert local file survives + uploader returns failure.

    This is the CRITICAL seam — inverting this (deleting on failure
    instead of success) silently loses bronze. D0.3 §7 ALERT-then-KEEP
    is the contract.
    """
    outbox_dir = tmp_path / "outbox"
    outbox_dir.mkdir()
    in_flight_dir = tmp_path / "in_flight"
    in_flight_dir.mkdir()

    chunk_path = outbox_dir / "chunk.jsonl.zst"
    chunk_path.write_bytes(b"compressed-bronze-bytes-pretend")
    in_flight_path = in_flight_dir / "chunk.jsonl"
    in_flight_path.write_bytes(b"raw bronze jsonl")

    uploader = RcloneUploader(
        rclone_remote="s3prod",
        bucket="kalshi-bot-archive",
    )

    # Simulate rclone copy failure exit-code 1.
    with patch("collector.uploader.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["rclone", "copy"], returncode=1, stdout="", stderr="network error",
        )
        ok = uploader.upload_chunk(
            outbox_path=chunk_path,
            in_flight_path=in_flight_path,
            s3_key="bronze/kalshi_ws/orderbook_delta/year=2026/month=05/day=16/hour=14/conn=A/chunk.jsonl.zst",
        )

    assert ok is False, "upload_chunk should return False on rclone non-zero"
    assert chunk_path.exists(), (
        "KEEP-local violated: outbox/chunk.jsonl.zst was DELETED after "
        "rclone exit=1. D0.3 §7 contract: KEEP-local on any non-zero. "
        "This is the data-loss seam — inverting this loses bronze."
    )
    assert in_flight_path.exists(), (
        "KEEP-local violated: in_flight/chunk.jsonl was DELETED after "
        "rclone exit=1. Both outbox + in-flight must survive a failed upload."
    )


def test_keep_local_when_rclone_size_mismatch(tmp_path: Path):
    """If rclone copy succeeded (exit 0) but the post-copy `rclone size`
    returns bytes != local file size, KEEP-local + return False.

    Size-mismatch indicates a partial upload, a network corruption, or
    --immutable detecting a stale local file. All three are KEEP-local
    cases per §7 step 5.
    """
    outbox_dir = tmp_path / "outbox"
    outbox_dir.mkdir()
    chunk_path = outbox_dir / "chunk.jsonl.zst"
    local_bytes = b"x" * 4096
    chunk_path.write_bytes(local_bytes)
    in_flight_path = tmp_path / "in_flight" / "chunk.jsonl"
    in_flight_path.parent.mkdir()
    in_flight_path.write_bytes(b"raw")

    uploader = RcloneUploader(rclone_remote="s3prod", bucket="kalshi-bot-archive")

    def fake_run(argv, *args, **kwargs):
        if "size" in argv:
            # Simulate a smaller s3 object than local.
            return subprocess.CompletedProcess(
                args=argv, returncode=0,
                stdout='{"count":1,"bytes":2048}\n', stderr="",
            )
        # copy
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    with patch("collector.uploader.subprocess.run", side_effect=fake_run):
        ok = uploader.upload_chunk(
            outbox_path=chunk_path,
            in_flight_path=in_flight_path,
            s3_key="bronze/kalshi_ws/orderbook_delta/year=2026/month=05/day=16/hour=14/conn=A/chunk.jsonl.zst",
        )

    assert ok is False, "upload_chunk must return False on size mismatch"
    assert chunk_path.exists(), (
        "KEEP-local violated on size-mismatch — D0.3 §7 step 5 contract."
    )
    assert in_flight_path.exists()


# ─── 3. Happy-path: success → delete BOTH outbox + in-flight ────────────────


def test_success_deletes_both_outbox_and_in_flight(tmp_path: Path):
    """On rclone copy success AND size-match, BOTH the outbox file and the
    in-flight file are deleted. Per D0.3 §7 step 6: "ONLY NOW delete the
    local outbox + in-flight files."

    The in-flight deletion is what reclaims disk in the steady-state cap
    calculation (§6 "current-rotation × 2"); skipping it would double the
    disk footprint.
    """
    outbox_dir = tmp_path / "outbox"
    outbox_dir.mkdir()
    in_flight_dir = tmp_path / "in_flight"
    in_flight_dir.mkdir()

    chunk_bytes = b"compressed-bronze" * 100
    chunk_path = outbox_dir / "chunk.jsonl.zst"
    chunk_path.write_bytes(chunk_bytes)
    in_flight_path = in_flight_dir / "chunk.jsonl"
    in_flight_path.write_bytes(b"raw")

    uploader = RcloneUploader(rclone_remote="s3prod", bucket="kalshi-bot-archive")

    def fake_run(argv, *args, **kwargs):
        if "size" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=0,
                stdout=json.dumps({"count": 1, "bytes": len(chunk_bytes)}) + "\n",
                stderr="",
            )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    with patch("collector.uploader.subprocess.run", side_effect=fake_run):
        ok = uploader.upload_chunk(
            outbox_path=chunk_path,
            in_flight_path=in_flight_path,
            s3_key="bronze/kalshi_ws/orderbook_delta/year=2026/month=05/day=16/hour=14/conn=A/chunk.jsonl.zst",
        )

    assert ok is True, "upload_chunk should return True on success"
    assert not chunk_path.exists(), (
        f"outbox file {chunk_path} survived a successful upload — disk "
        f"will fill up if delete-on-success is broken."
    )
    assert not in_flight_path.exists(), (
        f"in-flight file {in_flight_path} survived. D0.3 §7 step 6: "
        f"ONLY NOW delete local outbox + in-flight."
    )


# ─── 4. Alert log on failure (D1.6 observability hook) ──────────────────────


def test_failure_emits_alert_log(tmp_path: Path, caplog):
    """rclone non-zero exits MUST emit a structured log line that D1.6 can
    grep for. Per D0.3 §6 last bullet: D1.6 alerts on FIRST rclone non-zero,
    not waiting on passive df watermark.

    The log line must include enough context for ops to triage
    (exit code, stderr summary). The CONTRACT here is presence; exact
    format is a D1.6 concern.
    """
    import logging
    outbox_dir = tmp_path / "outbox"
    outbox_dir.mkdir()
    chunk_path = outbox_dir / "chunk.jsonl.zst"
    chunk_path.write_bytes(b"x" * 100)
    in_flight = tmp_path / "in_flight" / "chunk.jsonl"
    in_flight.parent.mkdir()
    in_flight.write_bytes(b"raw")

    uploader = RcloneUploader(rclone_remote="s3prod", bucket="kalshi-bot-archive")

    with patch("collector.uploader.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["rclone", "copy"], returncode=6,
            stdout="", stderr="some divergence",
        )
        with caplog.at_level(logging.ERROR, logger="collector.uploader"):
            uploader.upload_chunk(
                outbox_path=chunk_path,
                in_flight_path=in_flight,
                s3_key="bronze/.../chunk.jsonl.zst",
            )

    # Must produce at least one ERROR-level log with the exit code present.
    matching = [
        r for r in caplog.records
        if r.levelno >= logging.ERROR and "6" in r.getMessage()
    ]
    assert matching, (
        f"no ERROR-level log emitted on rclone exit=6. D0.3 §6 last bullet "
        f"requires D1.6 to alert on first rclone non-zero — uploader must "
        f"surface the failure as a structured log. Captured records: "
        f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )


# ─── 5. S3 key construction ─────────────────────────────────────────────────


def test_s3_key_is_passed_through_unchanged():
    """The s3_key passed to upload_chunk lands as the last segment of the
    rclone destination. The writer constructs the §3 hive-style path;
    the uploader does NOT re-parse or transform it (would let a writer
    bug propagate silently).
    """
    s3_key = "bronze/kalshi_ws/orderbook_delta/year=2026/month=05/day=16/hour=14/conn=A/chunk.jsonl.zst"
    argv = build_rclone_copy_argv(
        local_path=Path("/tmp/x"),
        s3_dest=f"s3prod:kalshi-bot-archive/{s3_key}",
    )
    dest = argv[-1]
    assert dest.endswith(s3_key), (
        f"s3 destination {dest!r} does not end with the passed s3_key "
        f"{s3_key!r}. The uploader must pass through the writer's path "
        f"verbatim."
    )
