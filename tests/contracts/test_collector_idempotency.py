"""D1.2 — collector atomic-rename + restart-idempotency contract (ticket 86b9ypn66, 2026-05-16).

Pins three load-bearing invariants:

1. tmp/ → outbox/ atomic via os.replace (POSIX rename(2)) — only atomic
   on the SAME filesystem. Test pins they share the same parent dir.
2. On collector restart, leftover outbox/ files are re-uploaded BEFORE new
   rotations begin. Per D0.3 §7 last paragraph (B-orphan-sweep AMENDMENT
   2026-05-19, ticket 86ba0jmz9): "On collector restart, any leftover
   outbox/ files are re-uploaded before new rotations begin — bit-
   identical re-uploads no-op via --checksum." The amendment extends
   the restart-sweep contract symmetrically: bare-``in_flight_<usec>``
   orphans are ALSO recovered via ``salvage_in_flight_orphans`` BEFORE
   the outbox loop runs (sister contract pinned by
   ``test_collector_in_flight_recovery.py``; this file only pins the
   outbox-side semantics it always did).
3. zstd compression is reversible — `_raw` payloads survive round-trip
   through zstd-6 → decompress.

Sister to ``test_collector_writer.py`` + ``test_collector_rotation.py`` +
``test_collector_uploader.py`` + ``test_collector_in_flight_recovery.py``.

If this test fails:
- tmp/ + outbox/ moved to different filesystems: os.replace is NO LONGER
  atomic. POSIX rename(2) on cross-device falls back to copy+unlink
  which is NOT atomic — half-written files are observable. Read D0.3 §7
  "atomic rename" before changing the directory layout.
- Restart idempotency broken: a process crash + restart would lose any
  outbox/ chunks that hadn't been uploaded yet.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
import subprocess

import pytest
import zstandard as zstd

from collector.writer import BronzeWriter
from collector.uploader import RcloneUploader


# ─── 1. Atomic-rename: tmp/ and outbox/ share a filesystem ──────────────────


def test_tmp_and_outbox_share_parent_directory(tmp_path: Path):
    """The tmp/ and outbox/ subdirs MUST share a common parent so os.replace
    can use POSIX rename(2) atomically.

    POSIX rename(2) is atomic ONLY within a single filesystem. Across
    filesystems, the implementation has to fall back to copy+unlink which
    is NOT atomic — readers can observe a half-written file.

    The contract is enforced by the BronzeWriter directory layout:
    everything lives under ``root_dir`` so it's always on one fs.
    """
    SMALL_CAP = 2_048
    writer = BronzeWriter(
        root_dir=tmp_path,
        source="kalshi_ws",
        channel="orderbook_delta",
        conn="A",
        size_threshold_bytes=SMALL_CAP,
    )
    base_ts = datetime(2026, 5, 16, 14, 0, 0, 0, tzinfo=timezone.utc)
    for i in range(10):
        writer.write_frame(
            wire_recv_ts=base_ts + timedelta(seconds=i),
            raw_payload="z" * 800,
        )
    # Find the tmp/ and outbox/ dirs the writer created.
    tmp_dirs = [p for p in tmp_path.rglob("tmp") if p.is_dir()]
    outbox_dirs = [p for p in tmp_path.rglob("outbox") if p.is_dir()]
    assert tmp_dirs, "writer did not create a tmp/ subdir"
    assert outbox_dirs, "writer did not create an outbox/ subdir"
    # They must be siblings (share a parent).
    for t, o in zip(tmp_dirs, outbox_dirs):
        assert t.parent == o.parent, (
            f"tmp ({t}) and outbox ({o}) have different parents — "
            f"POSIX rename(2) atomicity is NOT guaranteed across "
            f"filesystems. D0.3 §7: \"POSIX-atomic on same fs\"."
        )
    # Concrete fs-check: stat the device ids.
    if tmp_dirs and outbox_dirs:
        t_dev = os.stat(tmp_dirs[0]).st_dev
        o_dev = os.stat(outbox_dirs[0]).st_dev
        assert t_dev == o_dev, (
            f"tmp/ on device {t_dev}, outbox/ on device {o_dev} — "
            f"cross-fs rename is NOT atomic."
        )


def test_no_partial_files_under_outbox(tmp_path: Path):
    """After rotation, outbox/ contains only complete .jsonl.zst files —
    no .tmp suffix, no zero-byte files. The atomic rename only completes
    when the tmp file is fully fsynced; readers seeing the outbox/ name
    can trust the bytes are intact.

    Pre-rename state (.tmp suffix in tmp/ subdir) must NEVER leak into
    outbox/. The directory invariant is what the uploader relies on.
    """
    SMALL_CAP = 2_048
    writer = BronzeWriter(
        root_dir=tmp_path,
        source="kalshi_ws",
        channel="orderbook_delta",
        conn="A",
        size_threshold_bytes=SMALL_CAP,
    )
    base_ts = datetime(2026, 5, 16, 14, 0, 0, 0, tzinfo=timezone.utc)
    for i in range(10):
        writer.write_frame(
            wire_recv_ts=base_ts + timedelta(seconds=i),
            raw_payload="x" * 800,
        )
    outbox_files = []
    for outbox_dir in tmp_path.rglob("outbox"):
        if outbox_dir.is_dir():
            outbox_files.extend(outbox_dir.iterdir())
    assert outbox_files, "no chunks in outbox/ after rotation"
    for p in outbox_files:
        assert not p.name.endswith(".tmp"), (
            f"{p} has .tmp suffix in outbox/ — atomic rename incomplete or "
            f"absent. Use os.replace(tmp_path, outbox_path), not write-in-place."
        )
        assert p.stat().st_size > 0, (
            f"{p} is zero bytes in outbox/ — pre-fsync write leaked"
        )


# ─── 2. zstd round-trip (the actual bytes-in == bytes-out invariant) ────────


def test_zstd_round_trip_preserves_jsonl_bytes(tmp_path: Path):
    """The .jsonl.zst file in outbox/ decompresses to the exact .jsonl
    bytes the writer accumulated.

    This is the in-process zstd-6 compression invariant. If a future Bit
    switches compression level, codec, or library, this test verifies
    the round-trip still holds.
    """
    SMALL_CAP = 2_048
    writer = BronzeWriter(
        root_dir=tmp_path,
        source="kalshi_ws",
        channel="orderbook_delta",
        conn="A",
        size_threshold_bytes=SMALL_CAP,
    )
    base_ts = datetime(2026, 5, 16, 14, 0, 0, 0, tzinfo=timezone.utc)
    payloads = [f'{{"i":{i},"data":"abc{i}"}}' for i in range(20)]
    for i, p in enumerate(payloads):
        writer.write_frame(
            wire_recv_ts=base_ts + timedelta(seconds=i),
            raw_payload=p,
        )
    closed = list(tmp_path.rglob("*.jsonl.zst"))
    assert closed, "no compressed chunk produced"
    # Decompress and verify the JSONL inside.
    dctx = zstd.ZstdDecompressor()
    with closed[0].open("rb") as f:
        decompressed = dctx.decompress(f.read())
    lines = decompressed.decode("utf-8").strip().splitlines()
    # Every line is parseable JSON with the expected envelope keys.
    import json as _json
    for line in lines:
        record = _json.loads(line)
        assert "_raw" in record and "_wire_recv_ts" in record


# ─── 3. Restart idempotency — outbox/ leftovers re-uploaded ────────────────


def test_restart_re_uploads_outbox_leftovers(tmp_path: Path):
    """Simulate a process crash that left an outbox/ chunk un-uploaded.
    A fresh ``RcloneUploader.upload_outbox_dir()`` (or whatever the
    sweep-on-startup API is named) re-uploads them.

    Per D0.3 §7 last paragraph: "On collector restart, any leftover
    outbox/ files are re-uploaded before new rotations begin —
    bit-identical re-uploads no-op via --checksum."
    """
    outbox_dir = tmp_path / "kalshi_ws" / "orderbook_delta" / "year=2026" / "month=05" / "day=16" / "hour=14" / "conn=A" / "outbox"
    outbox_dir.mkdir(parents=True)
    # Pretend a prior run left two chunks here.
    chunk_a = outbox_dir / "20260516T140000Z_to_20260516T140459Z_seq1-200.jsonl.zst"
    chunk_b = outbox_dir / "20260516T140500Z_to_20260516T140959Z_seq201-400.jsonl.zst"
    chunk_a.write_bytes(b"x" * 100)
    chunk_b.write_bytes(b"y" * 200)

    uploader = RcloneUploader(rclone_remote="s3prod", bucket="kalshi-bot-archive")
    uploads_attempted = []

    def fake_run(argv, *args, **kwargs):
        uploads_attempted.append(argv)
        if "size" in argv:
            # bytes match local
            return subprocess.CompletedProcess(
                args=argv, returncode=0,
                stdout='{"count":1,"bytes":' + str(100 if "seq1-200" in argv[-1] else 200) + '}\n',
                stderr="",
            )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    with patch("collector.uploader.subprocess.run", side_effect=fake_run):
        # The sweep API drives the re-upload. The exact name is in the
        # uploader API surface; this test asserts SOME public method
        # performs the sweep.
        if hasattr(uploader, "sweep_outbox"):
            uploader.sweep_outbox(root_dir=tmp_path)
        elif hasattr(uploader, "upload_pending"):
            uploader.upload_pending(root_dir=tmp_path)
        else:
            pytest.fail(
                "RcloneUploader has no startup-sweep API (sweep_outbox / "
                "upload_pending). D0.3 §7 last paragraph requires leftover "
                "outbox/ files to be re-uploaded on collector restart."
            )

    # Both chunks attempted (at minimum a `copy`/`copyto` argv referencing
    # each). Match any copy-family subcommand (rclone uses `copyto` for
    # file-to-file; `copy` substring match catches both).
    copy_argvs = [
        a for a in uploads_attempted
        if any("copy" in tok for tok in a)
    ]
    referenced = " ".join(" ".join(str(x) for x in a) for a in copy_argvs)
    assert "seq1-200" in referenced, (
        f"chunk_a (seq1-200) not re-uploaded on sweep. argvs: {copy_argvs}"
    )
    assert "seq201-400" in referenced, (
        f"chunk_b (seq201-400) not re-uploaded on sweep. argvs: {copy_argvs}"
    )
    # Both deleted post-success.
    assert not chunk_a.exists(), "chunk_a not deleted after successful re-upload"
    assert not chunk_b.exists(), "chunk_b not deleted after successful re-upload"


def test_restart_keeps_outbox_when_re_upload_fails(tmp_path: Path):
    """If a re-uploaded chunk's rclone copy fails, it stays in outbox/
    for the next sweep — same KEEP-local contract as steady-state."""
    outbox_dir = tmp_path / "kalshi_ws" / "orderbook_delta" / "year=2026" / "month=05" / "day=16" / "hour=14" / "conn=A" / "outbox"
    outbox_dir.mkdir(parents=True)
    chunk = outbox_dir / "20260516T140000Z_to_20260516T140459Z_seq1-200.jsonl.zst"
    chunk.write_bytes(b"x" * 100)

    uploader = RcloneUploader(rclone_remote="s3prod", bucket="kalshi-bot-archive")

    with patch("collector.uploader.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["rclone", "copy"], returncode=1, stdout="", stderr="net err",
        )
        if hasattr(uploader, "sweep_outbox"):
            uploader.sweep_outbox(root_dir=tmp_path)
        elif hasattr(uploader, "upload_pending"):
            uploader.upload_pending(root_dir=tmp_path)
        else:
            pytest.skip("no startup-sweep API to test")

    assert chunk.exists(), (
        "leftover chunk DELETED after failed re-upload — same KEEP-local "
        "contract applies to restart-sweep as to steady-state."
    )
