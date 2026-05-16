"""Integration test for collector/main_loop.py end-to-end frame flow.

D1.2 (ticket 86b9ypn66, 2026-05-16). Verifies the boot-time wire-up:

  envelope (kalshi_wire.build_envelope shape)
      │
      ▼
  BronzeWriter.write(envelope)  ─► JSONL append + flush
      │
      ▼ (rotation triggered by size cap)
  rotated_outbox_paths.append((outbox_path, in_flight_renamed))
      │
      ▼ (drain thread unpacks the pair)
  uploader.upload_chunk(outbox, in_flight, s3_key)

Mocks ``BronzeArchiver`` (no real WS) and ``subprocess.run`` (no real
rclone) so the test runs deterministically on Mac in <1s. The actual
WS + rclone integration is gated by D1.5 (operator-driven systemd
deploy).
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import zstandard as zstd

from collector.main_loop import run as main_loop_run
from collector.writer import BronzeWriter
from kalshi_wire.ws_client import build_envelope


def test_main_loop_wires_writer_uploader_archiver_and_shuts_down_cleanly(
    tmp_path: Path,
    monkeypatch,
):
    """End-to-end smoke: writer + uploader + (mocked) archiver wire up,
    drain thread runs, shutdown event terminates everything cleanly.
    """
    bronze_root = tmp_path / "bronze"
    bronze_root.mkdir()

    # Mock BronzeArchiver — don't connect to real WS. The archiver's
    # run(shutdown_event) just waits on the event.
    fake_archiver = MagicMock()
    fake_archiver.run = lambda shutdown_event: shutdown_event.wait()

    archiver_ctor = MagicMock(return_value=fake_archiver)
    monkeypatch.setattr("collector.main_loop.BronzeArchiver", archiver_ctor)

    # Mock subprocess.run — no rclone. Return success for both copy + size.
    rclone_calls = []

    def fake_run(argv, *args, **kwargs):
        rclone_calls.append(argv)
        if "size" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=0,
                stdout=json.dumps({"count": 0, "bytes": 0}) + "\n",
                stderr="",
            )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
    monkeypatch.setattr("collector.uploader.subprocess.run", fake_run)

    # Drive shutdown from this thread after a short delay.
    shutdown = threading.Event()
    def _shutdown_soon():
        time.sleep(0.2)
        shutdown.set()
    t = threading.Thread(target=_shutdown_soon, daemon=True)
    t.start()

    main_loop_run(
        bronze_root=bronze_root,
        api_key="fake-key-id",
        private_key_path="/nonexistent.pem",
        shutdown_event=shutdown,
    )

    # Archiver was constructed and run.
    assert archiver_ctor.called, "BronzeArchiver was not constructed"
    ctor_kwargs = archiver_ctor.call_args.kwargs
    assert ctor_kwargs["api_key"] == "fake-key-id"
    assert ctor_kwargs["private_key_path"] == "/nonexistent.pem"
    assert callable(ctor_kwargs["writer"]), "writer kwarg should be a callable"


def test_main_loop_drain_routes_rotated_chunk_to_uploader(
    tmp_path: Path,
    monkeypatch,
):
    """When the writer rotates a chunk, the drain thread picks it up
    and calls uploader.upload_chunk with the right s3_key derivation."""
    bronze_root = tmp_path / "bronze"
    bronze_root.mkdir()

    # Use a SMALL_CAP writer so we trigger rotation quickly.
    # Inject our writer via the BronzeWriter constructor patch.
    real_writer_holder = {}
    real_ctor = BronzeWriter

    def capturing_ctor(**kwargs):
        # Force a small rotation cap so 3 writes trip it.
        kwargs["size_threshold_bytes"] = 1024
        w = real_ctor(**kwargs)
        real_writer_holder["w"] = w
        return w
    monkeypatch.setattr("collector.main_loop.BronzeWriter", capturing_ctor)

    fake_archiver = MagicMock()

    def archiver_run(shutdown_event):
        # Wait for the writer to be constructed, then feed it 3 envelopes
        # to trigger rotation.
        while "w" not in real_writer_holder:
            time.sleep(0.01)
        w = real_writer_holder["w"]
        for i in range(3):
            env = build_envelope(
                raw="p" * 500,
                source="kalshi_ws",
                channel=None,
                conn="A",
                collector_seq=i + 1,
                wire_recv_ts=datetime(2026, 5, 16, 14, 0, i, tzinfo=timezone.utc),
            )
            w.write(env)
        # Give the drain thread a moment to pick up the rotated chunk.
        time.sleep(0.3)
        shutdown_event.set()
    fake_archiver.run = archiver_run
    monkeypatch.setattr(
        "collector.main_loop.BronzeArchiver",
        MagicMock(return_value=fake_archiver),
    )

    rclone_calls = []
    def fake_run(argv, *args, **kwargs):
        rclone_calls.append(argv)
        if "size" in argv:
            # Find the chunk size by reading the local file referenced in
            # the previous copy call.
            return subprocess.CompletedProcess(
                args=argv, returncode=0,
                stdout=json.dumps({"count": 1, "bytes": 100}) + "\n",
                stderr="",
            )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    # Make size_check match the actual local size so delete-on-success fires.
    def fake_run_realistic(argv, *args, **kwargs):
        rclone_calls.append(argv)
        if "size" in argv:
            # Find the local file from the most recent copy argv.
            for prev in reversed(rclone_calls):
                if any("copy" in tok for tok in prev):
                    local_path = Path(prev[-2])  # second-to-last is local
                    if local_path.exists():
                        nb = local_path.stat().st_size
                        return subprocess.CompletedProcess(
                            args=argv, returncode=0,
                            stdout=json.dumps({"count": 1, "bytes": nb}) + "\n",
                            stderr="",
                        )
            return subprocess.CompletedProcess(
                args=argv, returncode=0,
                stdout=json.dumps({"count": 0, "bytes": 0}) + "\n",
                stderr="",
            )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
    monkeypatch.setattr("collector.uploader.subprocess.run", fake_run_realistic)

    shutdown = threading.Event()
    main_loop_run(
        bronze_root=bronze_root,
        api_key="fake-key-id",
        private_key_path="/nonexistent.pem",
        shutdown_event=shutdown,
    )

    # At least one rclone copy-family call should have happened for the
    # rotated chunk.
    copy_argvs = [a for a in rclone_calls if any("copy" in tok for tok in a)]
    assert copy_argvs, (
        f"no rclone copy call emitted — drain thread did not pick up the "
        f"rotated chunk. rclone_calls: {rclone_calls}"
    )
    # The s3 destination should be under bronze/, with NO outbox/ segment
    # (D0.3 §3 hive partition flattens the outbox/ subdir).
    s3_dest = copy_argvs[0][-1]
    assert "/bronze/" in s3_dest, f"s3 dest missing bronze/ prefix: {s3_dest}"
    assert "/outbox/" not in s3_dest, (
        f"s3 dest contains /outbox/ — silver ETL would see a non-flat hive "
        f"partition: {s3_dest}"
    )


def test_main_loop_missing_credentials_raises_clean_error(
    tmp_path: Path,
    monkeypatch,
):
    """If neither env nor explicit args provide credentials, run() raises
    EnvironmentError with a clear remediation message — BEFORE attempting
    any WS connect."""
    monkeypatch.delenv("KALSHI_COLLECTOR_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_COLLECTOR_KEY_PATH", raising=False)

    bronze_root = tmp_path / "bronze"
    import pytest as _pt
    with _pt.raises(EnvironmentError) as exc_info:
        main_loop_run(bronze_root=bronze_root)
    assert "KALSHI_COLLECTOR_KEY_ID" in str(exc_info.value)
