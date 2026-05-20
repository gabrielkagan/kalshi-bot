"""Integration test for collector/main_loop.py end-to-end frame flow.

D1.2 + D1.3 (tickets 86b9ypn66 + 86b9ypn72, 2026-05-16). Verifies the
boot-time wire-up:

  envelope (kalshi_wire.build_envelope shape)
      │
      ▼
  BronzeWriter.write(envelope, wire_recv_ts=...)  ─► JSONL append
      (post-P1-A 2026-05-20 ticket 86ba1pqqx: no per-frame flush;
       flush + fsync deferred to rotation/close per D0.3 §7)
      │
      ▼ (rotation triggered by size cap)
  rotated_outbox_paths.append((outbox_path, in_flight_renamed))
      │
      ▼ (drain thread unpacks the pair across ALL writers — D1.3 fan-out)
  uploader.upload_chunk(outbox, in_flight, s3_key)

D1.3 generalized the writer-dispatch surface from D1.2's single-writer
shape to one writer per (channel, conn). The BronzeArchiver constructor
signature changed from ``writer=`` (single callable) to
``writers_by_channel=`` (dict keyed by channel) + new
``subscribe_frames=`` / ``cmd_id_to_channel=`` kwargs for on_session_start
dispatch.

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


def _generate_pem_file(tmp_path: Path) -> Path:
    """D1.4 R1-C1: after the boot-time REST-path PEM-load was hardened
    to raise-loud rather than soft-fail, the wireup tests can no longer
    pass ``/nonexistent.pem`` against the real ``load_private_key``.
    This helper generates a throwaway RSA key for tests that need a
    real PEM. The collector NEVER signs anything against this key in
    tests (REST fetch is mocked by stubbing out the network)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_path = tmp_path / "test_collector.pem"
    pem_path.write_bytes(pk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    return pem_path


def _stub_rest_fetch(monkeypatch, returns=None) -> None:
    """Replace ``collector.main_loop.fetch_tickers_by_tier`` with a stub
    that returns ``returns`` (default empty dict). Lets wireup tests
    exercise the REST seam without making real HTTP calls."""
    if returns is None:
        returns = {}
    monkeypatch.setattr(
        "collector.main_loop.fetch_tickers_by_tier",
        lambda **kwargs: returns,
    )


def test_main_loop_wires_writer_uploader_archiver_and_shuts_down_cleanly(
    tmp_path: Path,
    monkeypatch,
):
    """End-to-end smoke: writer + uploader + (mocked) archiver wire up,
    drain thread runs, shutdown event terminates everything cleanly.

    D1.3 update: archiver kwargs now include writers_by_channel (dict),
    subscribe_frames, cmd_id_to_channel, conn_id — replacing the D1.2
    single ``writer=`` callable.
    """
    bronze_root = tmp_path / "bronze"
    bronze_root.mkdir()

    # Mock BronzeArchiver — don't connect to real WS. The archiver's
    # start() returns; main_loop blocks on shutdown_event independently.
    fake_archiver = MagicMock()

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

    pem_path = _generate_pem_file(tmp_path)
    _stub_rest_fetch(monkeypatch)
    main_loop_run(
        bronze_root=bronze_root,
        api_key="fake-key-id",
        private_key_path=str(pem_path),
        shutdown_event=shutdown,
    )

    # Archiver was constructed and run.
    assert archiver_ctor.called, "BronzeArchiver was not constructed"
    ctor_kwargs = archiver_ctor.call_args.kwargs
    assert ctor_kwargs["api_key"] == "fake-key-id"
    assert ctor_kwargs["private_key_path"] == str(pem_path)
    # D1.3: writers_by_channel is a dict (None-keyed _unrouted writer
    # plus one writer per channel in CHANNELS_DEFAULT).
    assert isinstance(ctor_kwargs["writers_by_channel"], dict)
    # D1.3 surface: subscribe_frames + cmd_id_to_channel are present.
    # This test exercises the WIRE-UP shape only. BronzeArchiver is
    # monkey-patched above so we never call the real __init__ (which
    # would eager-load the PEM). REST fetch is stubbed via
    # ``_stub_rest_fetch`` to return ``{}``, so subscribe_frames is
    # empty. The production PEM-load crash path is covered by
    # ``test_main_loop_bad_pem_crashes_loud_at_rest_seam`` (R1-C1
    # regression).
    assert "subscribe_frames" in ctor_kwargs
    assert "cmd_id_to_channel" in ctor_kwargs
    # The default-deploy posture is conn_count=1, no tickers file → 1
    # archiver with conn_id=A and empty subscribes.
    assert ctor_kwargs["conn_id"] == "A"
    assert list(ctor_kwargs["subscribe_frames"]) == []


def test_main_loop_drain_routes_rotated_chunk_to_uploader(
    tmp_path: Path,
    monkeypatch,
):
    """When the writer rotates a chunk, the drain thread picks it up
    and calls uploader.upload_chunk with the right s3_key derivation.

    D1.3 update: main_loop now allocates 4 writers per conn (3 channels +
    _unrouted). We capture the _unrouted (None-channel) writer to feed
    envelopes through it for the drain-fan-out assertion.
    """
    bronze_root = tmp_path / "bronze"
    bronze_root.mkdir()

    # Use a SMALL_CAP writer so we trigger rotation quickly.
    # Capture every constructed BronzeWriter so we can drive the unrouted one.
    real_writer_holder = {"writers": []}
    real_ctor = BronzeWriter

    def capturing_ctor(**kwargs):
        # Force a small rotation cap so 3 writes trip it.
        kwargs["size_threshold_bytes"] = 1024
        w = real_ctor(**kwargs)
        real_writer_holder["writers"].append(w)
        return w
    monkeypatch.setattr("collector.main_loop.BronzeWriter", capturing_ctor)

    fake_archiver = MagicMock()

    def archiver_start():
        # Wait for the writer set to be constructed, then find the
        # _unrouted (channel=None) writer and feed it 3 envelopes to
        # trigger rotation.
        deadline = time.time() + 5.0
        while not real_writer_holder["writers"] and time.time() < deadline:
            time.sleep(0.01)
        unrouted = next(
            (w for w in real_writer_holder["writers"] if w.channel is None),
            None,
        )
        assert unrouted is not None, "no _unrouted writer was constructed"
        for i in range(3):
            env = build_envelope(
                raw="p" * 500,
                source="kalshi_ws",
                channel=None,
                conn="A",
                collector_seq=i + 1,
                wire_recv_ts=datetime(2026, 5, 16, 14, 0, i, tzinfo=timezone.utc),
            )
            unrouted.write(env)
    # In production BronzeArchiver.start() returns quickly (it just
    # spawns the WSClient thread). Mirror that — main_loop blocks on
    # shutdown_event, not on archiver.start().
    fake_archiver.start = archiver_start
    monkeypatch.setattr(
        "collector.main_loop.BronzeArchiver",
        MagicMock(return_value=fake_archiver),
    )

    rclone_calls = []
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
    # Trigger shutdown ~0.5s after writes (allow drain to pick up).
    def _shutdown_soon():
        time.sleep(0.5)
        shutdown.set()
    threading.Thread(target=_shutdown_soon, daemon=True).start()

    pem_path = _generate_pem_file(tmp_path)
    _stub_rest_fetch(monkeypatch)
    main_loop_run(
        bronze_root=bronze_root,
        api_key="fake-key-id",
        private_key_path=str(pem_path),
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


def test_main_loop_bad_pem_crashes_loud_at_rest_seam(
    tmp_path: Path,
    monkeypatch,
):
    """R1-C1: when COLLECTOR_TICKERS_FILE is unset (D1.4 REST path),
    a missing/invalid PEM MUST raise immediately at the REST seam
    (before any WS connect attempt). The pre-R1 code wrapped this in
    try/except and continued — but BronzeArchiver._init__ would then
    re-load the same bad PEM and crash, after a misleading "Refresher
    will retry" log. Crash loud + early is the correct posture; this
    test pins it."""
    bronze_root = tmp_path / "bronze"
    bronze_root.mkdir()
    monkeypatch.delenv("COLLECTOR_TICKERS_FILE", raising=False)

    import pytest as _pt
    with _pt.raises(FileNotFoundError):
        main_loop_run(
            bronze_root=bronze_root,
            api_key="fake-key-id",
            private_key_path="/nonexistent.pem",
            shutdown_event=threading.Event(),
        )


# ─── D1.3 multi-conn fan-out ────────────────────────────────────────────────


def test_main_loop_multi_conn_fan_out_constructs_one_archiver_per_conn(
    tmp_path: Path,
    monkeypatch,
):
    """COLLECTOR_CONN_COUNT=3 ⇒ 3 BronzeArchivers constructed with
    distinct conn_ids (A, B, C). Each gets its own writers_by_channel
    dict.
    """
    bronze_root = tmp_path / "bronze"
    bronze_root.mkdir()
    monkeypatch.setenv("COLLECTOR_CONN_COUNT", "3")

    fake_archiver = MagicMock()
    archiver_ctor = MagicMock(return_value=fake_archiver)
    monkeypatch.setattr("collector.main_loop.BronzeArchiver", archiver_ctor)

    def fake_run(argv, *args, **kwargs):
        if "size" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=0,
                stdout=json.dumps({"count": 0, "bytes": 0}) + "\n",
                stderr="",
            )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
    monkeypatch.setattr("collector.uploader.subprocess.run", fake_run)

    shutdown = threading.Event()
    threading.Thread(
        target=lambda: (time.sleep(0.2), shutdown.set()), daemon=True,
    ).start()

    pem_path = _generate_pem_file(tmp_path)
    _stub_rest_fetch(monkeypatch)
    main_loop_run(
        bronze_root=bronze_root,
        api_key="fake-key-id",
        private_key_path=str(pem_path),
        shutdown_event=shutdown,
    )

    assert archiver_ctor.call_count == 3, (
        f"expected 3 archivers (1 per conn), got {archiver_ctor.call_count}"
    )
    conn_ids = [c.kwargs["conn_id"] for c in archiver_ctor.call_args_list]
    assert conn_ids == ["A", "B", "C"], (
        f"conn_ids should be A,B,C in order; got {conn_ids}"
    )
    # Each archiver's writers_by_channel must be a fresh dict (not shared).
    dicts = [c.kwargs["writers_by_channel"] for c in archiver_ctor.call_args_list]
    assert dicts[0] is not dicts[1], (
        "writers_by_channel dict shared across archivers — would conflate "
        "rotations and corrupt the per-(channel,conn) partition contract."
    )


def test_main_loop_with_tickers_file_populates_subscribe_frames(
    tmp_path: Path,
    monkeypatch,
):
    """When COLLECTOR_TICKERS_FILE points at a valid JSON tier-map, each
    archiver's subscribe_frames is non-empty and cmd_id_to_channel covers
    every frame's id.
    """
    bronze_root = tmp_path / "bronze"
    bronze_root.mkdir()
    tickers_path = tmp_path / "tickers.json"
    tickers_path.write_text(json.dumps({
        "1": ["KXBTC-A", "KXBTC-B", "KXETH-A"],
        "2": ["KXSOL-A"],
    }))
    monkeypatch.setenv("COLLECTOR_CONN_COUNT", "2")
    monkeypatch.setenv("COLLECTOR_TICKERS_FILE", str(tickers_path))

    fake_archiver = MagicMock()
    archiver_ctor = MagicMock(return_value=fake_archiver)
    monkeypatch.setattr("collector.main_loop.BronzeArchiver", archiver_ctor)

    def fake_run(argv, *args, **kwargs):
        if "size" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=0,
                stdout=json.dumps({"count": 0, "bytes": 0}) + "\n",
                stderr="",
            )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
    monkeypatch.setattr("collector.uploader.subprocess.run", fake_run)

    shutdown = threading.Event()
    threading.Thread(
        target=lambda: (time.sleep(0.2), shutdown.set()), daemon=True,
    ).start()

    main_loop_run(
        bronze_root=bronze_root,
        api_key="fake-key-id",
        private_key_path="/nonexistent.pem",
        shutdown_event=shutdown,
    )

    # 2 conns ⇒ 2 archivers.
    assert archiver_ctor.call_count == 2
    # Each should have at least 3 subscribe frames (one per channel),
    # since each conn gets a non-empty share of tickers.
    for call in archiver_ctor.call_args_list:
        kwargs = call.kwargs
        frames = list(kwargs["subscribe_frames"])
        cmd_id_map = dict(kwargs["cmd_id_to_channel"])
        assert len(frames) >= 3, (
            f"conn={kwargs['conn_id']} got only {len(frames)} subscribe "
            "frames; expected ≥3 (one per channel for the assigned tickers)."
        )
        for f in frames:
            assert f["id"] in cmd_id_map, (
                f"frame.id={f['id']} not in cmd_id_to_channel — sid binding "
                "will fail when this cmd_id's ack arrives."
            )
            assert cmd_id_map[f["id"]] == f["params"]["channels"][0], (
                "cmd_id_to_channel must agree with the frame's channel "
                "so subscribe-ack sid binding routes to the right writer."
            )
