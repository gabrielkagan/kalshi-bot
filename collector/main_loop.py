"""Collector run loop — D1.2 (ticket 86b9ypn66, 2026-05-16).

Orchestrates the bronze data plumbing in a single-conn no-tier shape:

    WSClient (kalshi_wire) ─► BronzeArchiver ─► BronzeWriter.write(env)
                                                   │
                                          (rotation: outbox/<chunk>.jsonl.zst)
                                                   │
                                                   ▼
                                          BronzeUploader (drain thread)
                                                   │
                                                   ▼
                                       rclone copyto → S3 → verify
                                                   │
                                                   ▼
                                       delete local outbox + in-flight

D1.3 will generalize to multi-conn via ``collector/subscription_manager.py``;
D1.4 adds REST snapshot fallback. D1.5 deploys via the
``ops/kalshi-collector.service`` systemd unit (requires-approval; 3
D0.3 §12 operator decisions still pending).

Sync + threading per CLAUDE.md anti-pattern ("Don't add async. Synchronous
+ threading for WS feeds is the design."). The drain thread is a daemon
that polls writer.rotated_outbox_paths every second; production frame
cadence is well below 1Hz per (source, channel, conn), so polling latency
is bounded and acceptable.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
from pathlib import Path
from typing import Optional

from collector.uploader import RcloneUploader
from collector.writer import BronzeWriter
from collector.ws_connection import BronzeArchiver

logger = logging.getLogger(__name__)

# Drain-thread polling cadence. Frames arrive at <1Hz per partition;
# 1-second polling gives bounded uploader-latency without burning CPU.
_DRAIN_POLL_SECONDS: float = 1.0

# Default channel for D1.2 single-conn shape. None routes envelopes
# to <root>/<source>/_unrouted/... per D0.3 §2 (channel-None fallback).
# D1.3 replaces with the real per-sid → channel mapping.
_DEFAULT_CHANNEL: Optional[str] = None
_DEFAULT_CONN: str = "A"


def _required_env(name: str) -> str:
    """Read an env var or raise with a clear remediation message.

    Boot-time misconfiguration should surface BEFORE the WS connect
    attempt so the operator sees "missing config" instead of an
    auth-handshake failure deep in the WS thread.
    """
    val = os.environ.get(name, "").strip()
    if not val:
        raise EnvironmentError(
            f"Required env var {name!r} is empty or unset. "
            f"D0.3 §12 lists the 3 operator decisions blocking D1.5; "
            f"the bronze collector cannot boot without auth credentials."
        )
    return val


def _build_uploader() -> RcloneUploader:
    rclone_remote = os.environ.get("RCLONE_REMOTE", "s3prod")
    bucket = os.environ.get("S3_BUCKET", "kalshi-bot-archive")
    return RcloneUploader(rclone_remote=rclone_remote, bucket=bucket)


def _s3_key_from_outbox(outbox_path: Path, bronze_root: Path) -> str:
    """Derive the S3 key for a rotated outbox/<chunk>.jsonl.zst.

    Strips the trailing ``/outbox/`` segment so silver ETL sees one flat
    hive partition under ``bronze/``, not an ``outbox/`` subdir.
    """
    rel = outbox_path.relative_to(bronze_root).as_posix()
    return "bronze/" + rel.replace("/outbox/", "/")


def _drain_rotated(
    writer: BronzeWriter,
    uploader: RcloneUploader,
    bronze_root: Path,
    shutdown_event: threading.Event,
) -> None:
    """Drain ``writer.rotated_outbox_paths`` → uploader until shutdown.

    Runs on a daemon thread so the main thread can block on the WS run.
    On shutdown, drains any final rotations before returning (caller
    joins us with a bounded timeout).
    """
    def _drain_one(outbox_path: Path, in_flight_path: Path) -> None:
        s3_key = _s3_key_from_outbox(outbox_path, bronze_root)
        in_flight_arg: Optional[Path] = (
            in_flight_path if in_flight_path.exists() else None
        )
        try:
            ok = uploader.upload_chunk(
                outbox_path=outbox_path,
                in_flight_path=in_flight_arg,
                s3_key=s3_key,
            )
            if not ok:
                # KEEP-local already enforced by upload_chunk;
                # sweep_outbox will retry on next process boot.
                logger.error(
                    "upload_chunk returned False for %s; left local "
                    "for sweep_outbox retry on next boot.",
                    outbox_path,
                )
        except Exception:
            logger.exception(
                "upload_chunk raised for %s; left local for sweep_outbox "
                "retry on next boot.",
                outbox_path,
            )

    while not shutdown_event.is_set():
        while writer.rotated_outbox_paths:
            outbox_path, in_flight_path = writer.rotated_outbox_paths.pop(0)
            _drain_one(outbox_path, in_flight_path)
        shutdown_event.wait(timeout=_DRAIN_POLL_SECONDS)
    # Final drain post-shutdown.
    while writer.rotated_outbox_paths:
        outbox_path, in_flight_path = writer.rotated_outbox_paths.pop(0)
        _drain_one(outbox_path, in_flight_path)


def run(
    *,
    bronze_root: Optional[Path] = None,
    api_key: Optional[str] = None,
    private_key_path: Optional[str] = None,
    shutdown_event: Optional[threading.Event] = None,
) -> None:
    """Boot the collector — wire WS → BronzeWriter → uploader → S3.

    Args are mostly for testability; in production all defaults come
    from env vars (see ``_required_env``). Pass ``shutdown_event`` to
    drive shutdown from a test harness without signal-handler
    pollution.

    Boot sequence:
      1. Resolve config (env vars, with explicit args overriding).
      2. Restart sweep: re-upload any leftover outbox/ chunks from a
         prior crash (D0.3 §7 last paragraph).
      3. Build writer + archiver + drain thread.
      4. Hand control to ``BronzeArchiver.run()`` which blocks until
         shutdown signal.
      5. On shutdown: stop archiver, flush writer, join drain, final
         outbox sweep.
    """
    if bronze_root is None:
        bronze_root = Path(
            os.environ.get(
                "COLLECTOR_BRONZE_ROOT",
                "/var/lib/kalshi-collector/bronze",
            )
        )
    bronze_root.mkdir(parents=True, exist_ok=True)

    if api_key is None:
        api_key = _required_env("KALSHI_COLLECTOR_KEY_ID")
    if private_key_path is None:
        private_key_path = _required_env("KALSHI_COLLECTOR_KEY_PATH")

    uploader = _build_uploader()

    # Step 2 — restart sweep BEFORE new rotations start.
    n_swept = uploader.sweep_outbox(root_dir=bronze_root)
    if n_swept:
        logger.info(
            "Restart sweep re-uploaded %d leftover outbox chunks", n_swept
        )

    # Step 3 — components.
    writer = BronzeWriter(
        root_dir=bronze_root,
        source="kalshi_ws",
        channel=_DEFAULT_CHANNEL,
        conn=_DEFAULT_CONN,
    )

    owned_event = shutdown_event is None
    if owned_event:
        shutdown_event = threading.Event()
        # When main_loop creates the event itself, install SIGINT/SIGTERM
        # handlers so a systemd `kill -TERM <pid>` (or a Ctrl-C in
        # development) triggers the graceful-shutdown finally block
        # below — close the writer, drain rotations, sweep outbox. Tests
        # pass an explicit shutdown_event and bypass this path. ValueError
        # tolerance: signal.signal raises on non-main thread.
        try:
            signal.signal(signal.SIGINT, lambda *_: shutdown_event.set())
            signal.signal(signal.SIGTERM, lambda *_: shutdown_event.set())
        except ValueError:
            logger.warning(
                "Could not install SIGINT/SIGTERM handlers (not on main "
                "thread). External shutdown source must set the event."
            )

    drain_thread = threading.Thread(
        target=_drain_rotated,
        kwargs={
            "writer": writer,
            "uploader": uploader,
            "bronze_root": bronze_root,
            "shutdown_event": shutdown_event,
        },
        daemon=True,
        name="bronze-drain",
    )
    drain_thread.start()

    archiver = BronzeArchiver(
        api_key=api_key,
        private_key_path=private_key_path,
        writer=writer.write,
        conn_id=_DEFAULT_CONN,
    )

    logger.info(
        "Collector booted — bronze_root=%s, conn=%s, channel=%s",
        bronze_root, _DEFAULT_CONN, _DEFAULT_CHANNEL,
    )

    try:
        # Step 4 — block on archiver until shutdown.
        archiver.run(shutdown_event=shutdown_event)
    finally:
        # Step 5 — graceful shutdown.
        shutdown_event.set()
        writer.close()
        drain_thread.join(timeout=10.0)
        # Final sweep — upload anything still in outbox/ that the drain
        # thread didn't reach (e.g., crash mid-write).
        try:
            n_final = uploader.sweep_outbox(root_dir=bronze_root)
            if n_final:
                logger.info(
                    "Final sweep re-uploaded %d outbox chunks on shutdown",
                    n_final,
                )
        except Exception:
            logger.exception("final sweep_outbox raised on shutdown")
