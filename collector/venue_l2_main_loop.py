"""Venue-L2 collector run loop — B2a-1 (ticket 86ba1zf5j, 2026-05-28).

Orchestrator invoked by ``ops/kalshi-venue-l2-collector.service`` via
``venue-l2-collector-start.sh`` as ``python3 -m collector.venue_l2_main_loop``.

Mirrors ``collector/coinbase_main_loop.py`` (D2.5) adapted for the
multi-venue lean L2 recorder deltas:

  - **One BronzeWriter PER VENUE** (not per channel). Each venue is its
    own bronze source (``kraken_ws`` / ``bitstamp_ws`` / ``gemini_ws``)
    with its own native L2 channel (``book`` / ``order_book`` / ``l2``).
    No ``_unrouted`` fallback — the archiver routes strictly to the three
    venue writers (a frame that doesn't match a venue's data shape is
    skipped at the archiver, never enqueued).
  - **Reduced rotation size** (16MB vs the D0.3 §4 default 100MB). The
    recorder writes synchronously on the asyncio thread (lean — no
    worker-thread decouple); a 100MB chunk's zstd compress would block the
    other venues' reads for seconds and trip their keepalive. 16MB keeps
    the per-rotation compress to a few-hundred-ms blip while staying well
    above the DEEP_ARCHIVE 40 KB/object minimum. The 5-min timer still
    bounds chunk age for low-volume venues.
  - **No PEM / no REST refresh / no SubscriptionManager.** All three
    venues use free PUBLIC L2 WS; symbols are static (a new asset is a
    repo commit + deploy, not an hourly poll).

Same drain-thread + uploader + bronze_health.json sidecar pattern as the
Kalshi / Coinbase / Weather sides.

Bronze partition path (per D0.3 §3):

    bronze/<venue>_ws/<channel>/year=YYYY/month=MM/day=DD/hour=HH/conn=A/<chunk>.jsonl.zst

NO ``bot.*`` imports — pinned by the collector-no-bot import-linter
contract + AST defense-in-depth in
tests/contracts/test_collector_no_bot_imports.py.

D0.3 §10 bot-isolation invariant: collector failure ⇒ bot keeps trading;
bot failure ⇒ collector keeps capturing. The systemd unit's
``MemoryMax=512M`` + ``MemorySwapMax=0`` + ``Nice=10`` enforce the
structural bound from the OS side.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import signal
import threading
from pathlib import Path
from typing import Dict, List, Optional

from collector.uploader import RcloneUploader
from collector.venue_l2_archiver import (
    VENUE_CHANNELS,
    VENUE_SOURCES,
    VENUES,
    VenueL2Archiver,
    _CONN_ID,
)
from collector.writer import BronzeWriter

logger = logging.getLogger(__name__)

# Drain-thread polling cadence. Same as the Kalshi / Coinbase sides; 1s
# gives bounded uploader latency without burning CPU.
_DRAIN_POLL_SECONDS: float = 1.0

# Reduced rotation size cap (16MB). The recorder writes synchronously on
# the asyncio thread; a smaller cap bounds the per-rotation zstd compress
# block so it can't stall the other venues' WS keepalive for multiple
# seconds (a 100MB chunk would). The 5-min timer (BronzeWriter default
# interval_seconds) still bounds chunk age for low-volume venues.
_ROTATION_SIZE_BYTES: int = 16 * 1024 * 1024


def _build_uploader() -> RcloneUploader:
    rclone_remote = os.environ.get("RCLONE_REMOTE", "s3prod")
    bucket = os.environ.get("S3_BUCKET", "kalshi-bot-archive")
    return RcloneUploader(rclone_remote=rclone_remote, bucket=bucket)


def _s3_key_from_outbox(outbox_path: Path, bronze_root: Path) -> str:
    """Derive the S3 key for a rotated outbox/<chunk>.jsonl.zst.

    Mirrors ``collector.coinbase_main_loop._s3_key_from_outbox`` — strips
    the trailing ``/outbox/`` segment so silver ETL sees one flat hive
    partition under ``bronze/``, not an ``outbox/`` subdir."""
    rel = outbox_path.relative_to(bronze_root).as_posix()
    return "bronze/" + rel.replace("/outbox/", "/")


def write_bronze_health_sidecar(
    archiver: VenueL2Archiver,
    path: Path,
) -> None:
    """Write a single-archiver bronze health snapshot JSON file.

    Mirrors ``collector.coinbase_main_loop.write_bronze_health_sidecar``
    (schema_version=1, single-element archivers list) — one VenueL2Archiver
    covers all three venues. Atomic-replace via tmp + os.replace so the
    cron monitor never observes a torn write."""
    snapshot = archiver.get_health_snapshot()
    total_dropped = int(snapshot.get("dropped_frames", 0))
    total_queue = int(snapshot.get("write_queue_size", 0))
    written_at = _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ",
    )
    payload = {
        "schema_version": 1,
        "written_at": written_at,
        "archivers": [snapshot],
        "total_dropped_frames": total_dropped,
        "total_queue_size": total_queue,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload))
    os.replace(tmp_path, path)


def _drain_rotated(
    writers: List[BronzeWriter],
    uploader: RcloneUploader,
    bronze_root: Path,
    shutdown_event: threading.Event,
    archiver: Optional[VenueL2Archiver] = None,
    health_sidecar_path: Optional[Path] = None,
) -> None:
    """Drain rotated_outbox_paths across all venue writers → uploader.

    Daemon thread; polls every writer's ``rotated_outbox_paths`` every
    ``_DRAIN_POLL_SECONDS``. On shutdown, drains any final rotations."""

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
                logger.error(
                    "upload_chunk returned False for %s; left local for "
                    "sweep_outbox retry on next boot.",
                    outbox_path,
                )
        except Exception:
            logger.exception(
                "upload_chunk raised for %s; left local for sweep_outbox "
                "retry on next boot.",
                outbox_path,
            )

    def _drain_writers_once() -> None:
        for writer in writers:
            while writer.rotated_outbox_paths:
                outbox_path, in_flight_path = writer.rotated_outbox_paths.pop(0)
                _drain_one(outbox_path, in_flight_path)

    def _write_health_sidecar_safe() -> None:
        if health_sidecar_path is None or archiver is None:
            return
        try:
            write_bronze_health_sidecar(archiver, health_sidecar_path)
        except Exception:
            logger.warning(
                "write_bronze_health_sidecar failed (path=%s); continuing.",
                health_sidecar_path, exc_info=True,
            )

    while not shutdown_event.is_set():
        _drain_writers_once()
        _write_health_sidecar_safe()
        shutdown_event.wait(timeout=_DRAIN_POLL_SECONDS)
    # Final drain post-shutdown.
    _drain_writers_once()
    _write_health_sidecar_safe()


def _build_writers(bronze_root: Path) -> Dict[str, BronzeWriter]:
    """Allocate one BronzeWriter per venue (conn=A).

    Each writer is scoped to (source=<venue>_ws, channel=<native L2>,
    conn=A) with the reduced rotation-size cap."""
    writers: Dict[str, BronzeWriter] = {}
    for venue in VENUES:
        writers[venue] = BronzeWriter(
            root_dir=bronze_root,
            source=VENUE_SOURCES[venue],
            channel=VENUE_CHANNELS[venue],
            conn=_CONN_ID,
            size_threshold_bytes=_ROTATION_SIZE_BYTES,
        )
    return writers


def run(
    *,
    bronze_root: Optional[Path] = None,
    urls: Optional[Dict[str, str]] = None,
    shutdown_event: Optional[threading.Event] = None,
) -> None:
    """Boot the venue-L2 collector — wire VenueL2Archiver → writers → S3.

    Env-driven config:
      - ``VENUE_L2_BRONZE_ROOT`` — bronze root dir (default
        ``/var/lib/kalshi-venue-l2-collector/bronze``).
      - ``VENUE_L2_HEALTH_SIDECAR_PATH`` — bronze_health.json path
        (default alongside bronze root).
      - ``RCLONE_REMOTE`` — rclone S3 remote (default ``s3prod``).
      - ``S3_BUCKET`` — bucket (default ``kalshi-bot-archive``).

    Boot sequence mirrors coinbase_main_loop: resolve config → restart
    outbox sweep → allocate per-venue writers → construct archiver →
    single drain thread fans out + writes sidecar → start archiver →
    block on shutdown → graceful stop."""
    if bronze_root is None:
        bronze_root = Path(
            os.environ.get(
                "VENUE_L2_BRONZE_ROOT",
                "/var/lib/kalshi-venue-l2-collector/bronze",
            )
        )
    bronze_root.mkdir(parents=True, exist_ok=True)

    health_sidecar_env = os.environ.get(
        "VENUE_L2_HEALTH_SIDECAR_PATH",
        str(bronze_root.parent / "bronze_health.json"),
    ).strip()
    health_sidecar_path: Optional[Path] = (
        Path(health_sidecar_env) if health_sidecar_env else None
    )

    uploader = _build_uploader()

    # Restart sweep BEFORE new rotations start — recursively walks
    # bronze_root so it covers every venue's partition allocated below.
    n_swept = uploader.sweep_outbox(root_dir=bronze_root)
    if n_swept:
        logger.info(
            "Venue-L2 restart sweep re-uploaded %d leftover outbox chunks",
            n_swept,
        )

    writers_by_venue = _build_writers(bronze_root)
    all_writers: List[BronzeWriter] = list(writers_by_venue.values())

    archiver = VenueL2Archiver(writers_by_venue=writers_by_venue, urls=urls)

    owned_event = shutdown_event is None
    if owned_event:
        shutdown_event = threading.Event()
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
            "writers": all_writers,
            "uploader": uploader,
            "bronze_root": bronze_root,
            "shutdown_event": shutdown_event,
            "archiver": archiver,
            "health_sidecar_path": health_sidecar_path,
        },
        daemon=True,
        name="venue-l2-bronze-drain",
    )
    drain_thread.start()

    logger.info(
        "Venue-L2 collector booted — bronze_root=%s, venues=%s, "
        "health_sidecar=%s",
        bronze_root, list(VENUES), health_sidecar_path,
    )

    try:
        archiver.start()
        shutdown_event.wait()
    finally:
        shutdown_event.set()
        try:
            archiver.stop()
        except Exception:
            logger.exception("Venue-L2 archiver.stop() raised")
        for writer in all_writers:
            try:
                writer.close()
            except Exception:
                logger.exception(
                    "writer.close() raised (channel=%s)",
                    getattr(writer, "channel", "?"),
                )
        drain_thread.join(timeout=10.0)
        try:
            n_final = uploader.sweep_outbox(root_dir=bronze_root)
            if n_final:
                logger.info(
                    "Venue-L2 final sweep re-uploaded %d outbox chunks on "
                    "shutdown",
                    n_final,
                )
        except Exception:
            logger.exception("Venue-L2 final sweep_outbox raised on shutdown")


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stderr)],
        force=True,
    )
    run()
