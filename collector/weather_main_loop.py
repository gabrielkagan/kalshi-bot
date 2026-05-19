"""Weather collector run loop — D1.8 (ticket 86ba0duck, 2026-05-18).

Orchestrator invoked by ``ops/kalshi-weather-collector.service`` via
``weather-collector-start.sh`` as
``python3 -m collector.weather_main_loop``.

Mirrors ``collector/coinbase_main_loop.py`` (D2.5) adapted for the
weather deltas:

  - **HTTP-poll loop, NOT a WS reader.** No WSClient construction.
    The run loop fires ``archiver.poll_once()`` on a 60-min cadence
    (configurable via ``WEATHER_POLL_INTERVAL_SECONDS``) + sleeps
    between cycles.
  - **4 BronzeWriters** (one per channel) at ``interval_seconds=3600``
    (60-min rotation, NOT D0.3 §4 default 5-min). The 60-min cadence
    aligns with the poll interval and keeps each chunk to one
    cycle's payload (~20-30 KB compressed); the D0.3 §4 default
    would produce mostly-empty chunks for HTTP-polled sources.
  - **No PEM / no auth.** Open-Meteo is free + keyless (10K req/day
    quota). The dedicated ``.env.weather-collector`` carries only
    ``WEATHER_BRONZE_ROOT`` / ``WEATHER_POLL_INTERVAL_SECONDS`` /
    ``RCLONE_REMOTE`` / ``S3_BUCKET`` knobs.
  - **Same drain thread + uploader pattern** as Kalshi/Coinbase
    sides.

Bronze partition path (per D0.3 §3, with ``_conn=None``):

    bronze/open_meteo/<channel>/year=YYYY/month=MM/day=DD/hour=HH/conn=none/<chunk>.jsonl.zst

Anti-patterns honored (root ``CLAUDE.md``):
  - Synchronous + threading (NOT asyncio at the public API).
  - SAME venv as bot + Kalshi + Coinbase collectors (requests is
    already a bot dependency).
  - NO ``bot.*`` imports — pinned by ``collector-no-bot`` import-
    linter contract + AST defense-in-depth in
    ``tests/contracts/test_collector_weather_main_loop.py``.

D0.3 §10 bot-isolation invariant: collector failure ⇒ bot keeps
trading; bot failure ⇒ collector keeps capturing. The systemd unit's
``MemoryMax=128M`` + ``MemorySwapMax=0`` + ``Nice=10`` enforce the
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
from typing import Dict, List, Optional, Sequence

from collector.uploader import RcloneUploader
from collector.weather_archiver import (
    DEFAULT_CHANNELS,
    SOURCE as _BRONZE_SOURCE,
    WeatherArchiver,
)
from collector.writer import BronzeWriter

logger = logging.getLogger(__name__)

# Drain-thread polling cadence. Weather is the lightest of all 4
# collector tiers — 1-cycle-per-hour × 19 cities × ≤4 channels (57
# typical = 3 forecast × 19 cities; 76 max with archive_observed at 06:00 UTC)
# envelopes per cycle. 5s polling is generous; rotation cadence
# (60-min) bounds drain backlog.
_DRAIN_POLL_SECONDS: float = 5.0

# Default poll interval in seconds (60 min). Justified at the D1.8
# plan-doc:
#   - Open-Meteo quota math: bot already burns ~5,472/day; collector
#     at 60min adds ~1,387/day (24 cycles × 19 cities × 3 model
#     fetches per cycle = 1,368, plus 19 archive_observed/day = 1,387);
#     total ~6,859 < 10K free-tier quota.
#   - Research signal cadence: weather daily-high markets settle once
#     per day, so 24 snapshots/day is more than sufficient for
#     bias-correction + ensemble-quality + regime-filter iteration.
DEFAULT_POLL_INTERVAL_SECONDS: int = 3600

# Default rotation cadence (60 min). Aligns with the poll interval
# so each chunk holds ~1 cycle's payload. The 5-min D0.3 §4 default
# would produce mostly-empty chunks (the writer skips truly-empty
# rotations, but minimum-1-frame chunks waste DEEP_ARCHIVE 40 KB/object
# minimum overhead).
DEFAULT_ROTATION_INTERVAL_SECONDS: int = 3600


def _build_uploader() -> RcloneUploader:
    rclone_remote = os.environ.get("RCLONE_REMOTE", "s3prod")
    bucket = os.environ.get("S3_BUCKET", "kalshi-bot-archive")
    return RcloneUploader(rclone_remote=rclone_remote, bucket=bucket)


def _s3_key_from_outbox(outbox_path: Path, bronze_root: Path) -> str:
    """Derive the S3 key for a rotated outbox/<chunk>.jsonl.zst.

    Mirrors ``collector.coinbase_main_loop._s3_key_from_outbox`` —
    strips the trailing ``/outbox/`` segment so silver ETL sees one
    flat hive partition under ``bronze/``, not an ``outbox/`` subdir.
    """
    rel = outbox_path.relative_to(bronze_root).as_posix()
    return "bronze/" + rel.replace("/outbox/", "/")


def write_bronze_health_sidecar(
    archiver: WeatherArchiver,
    writers: Sequence[BronzeWriter],
    path: Path,
) -> None:
    """Write a bronze health snapshot JSON file for the cron monitor.

    Mirror of ``collector.coinbase_main_loop.write_bronze_health_sidecar``
    schema (schema_version=1) — the cron-driven health monitor
    (``scripts/ops/collector_health_monitor.py``) reads this file via
    ``check_dropped_frames`` and alerts on staleness / schema skew /
    threshold breaches.

    Weather has no worker-queue drops by design (single synchronous
    HTTP-poll loop; ``BronzeWriter.write`` is in-line). We still emit
    a ``total_dropped_frames=0`` entry to keep schema parity with the
    Kalshi/Coinbase sidecars + so the monitor's tier-uniform shape
    works without per-tier branches.

    Atomic-replace via tmp file + os.replace so the cron-driven
    monitor never observes a torn JSON write.
    """
    written_at = _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ",
    )
    # Single-archiver shape (mirrors coinbase sidecar's single-element
    # archivers list). Weather is single-archiver by design.
    archiver_snapshot = {
        "conn_id": None,
        "dropped_frames": 0,
        "write_queue_size": 0,
        "write_queue_maxsize": 0,
        "write_worker_alive": True,
        "collector_seq": getattr(archiver, "_collector_seq", 0),
        "ack_frames_processed": 0,
    }
    payload = {
        "schema_version": 1,
        "written_at": written_at,
        "archivers": [archiver_snapshot],
        "total_dropped_frames": 0,
        "total_queue_size": 0,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload))
    os.replace(tmp_path, path)


def _drain_rotated(
    writers: Sequence[BronzeWriter],
    uploader: RcloneUploader,
    bronze_root: Path,
    shutdown_event: threading.Event,
    archiver: Optional[WeatherArchiver] = None,
    health_sidecar_path: Optional[Path] = None,
) -> None:
    """Drain rotated_outbox_paths across all writers → uploader until shutdown.

    Daemon thread; polls every writer's ``rotated_outbox_paths`` every
    ``_DRAIN_POLL_SECONDS``. Weather rotation cadence (60-min) bounds
    backlog growth — each writer rotates ≤1 chunk per 60min in
    steady-state.

    On shutdown, drains any final rotations before returning.
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
                logger.error(
                    "upload_chunk returned False for %s; left local "
                    "for sweep_outbox retry on next boot.",
                    outbox_path,
                )
        except Exception:
            logger.exception(
                "upload_chunk raised for %s; left local for "
                "sweep_outbox retry on next boot.",
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
            write_bronze_health_sidecar(archiver, writers, health_sidecar_path)
        except Exception:
            # Sidecar writes are best-effort observability. A disk-full
            # or permission error here MUST NOT stall the drain loop
            # (the drain loop is the upload path that frees disk).
            logger.warning(
                "write_bronze_health_sidecar failed (path=%s); "
                "continuing.",
                health_sidecar_path, exc_info=True,
            )

    while not shutdown_event.is_set():
        _drain_writers_once()
        _write_health_sidecar_safe()
        shutdown_event.wait(timeout=_DRAIN_POLL_SECONDS)
    # Final drain post-shutdown.
    _drain_writers_once()
    _write_health_sidecar_safe()


def _build_writers(
    bronze_root: Path,
    channels: Sequence[str],
    rotation_interval_seconds: int = DEFAULT_ROTATION_INTERVAL_SECONDS,
) -> Dict[str, BronzeWriter]:
    """Allocate per-channel BronzeWriter instances (conn=None).

    Weather has no ``conn=`` dimension (HTTP polling has no persistent
    connection); the writer creates a ``conn=none`` segment in the
    partition path automatically (writer.py:245 fallback).

    Returns a dict keyed by channel name. NO None-keyed fallback —
    the archiver dispatches strictly by the configured channel list
    (every diagnostic envelope has an explicit channel; no _unrouted
    class exists for HTTP-polled sources).
    """
    writers: Dict[str, BronzeWriter] = {}
    for channel in channels:
        writers[channel] = BronzeWriter(
            root_dir=bronze_root,
            source=_BRONZE_SOURCE,
            channel=channel,
            conn=None,
            interval_seconds=rotation_interval_seconds,
        )
    return writers


def run(
    *,
    bronze_root: Optional[Path] = None,
    poll_interval_seconds: Optional[int] = None,
    shutdown_event: Optional[threading.Event] = None,
) -> None:
    """Boot the weather collector — wire WeatherArchiver → writers → S3.

    Args are mostly for testability; in production all defaults come
    from env vars.

    Env-driven config:
      - ``WEATHER_BRONZE_ROOT`` — bronze root dir (default
        ``/var/lib/kalshi-weather-collector/bronze``).
      - ``WEATHER_POLL_INTERVAL_SECONDS`` — poll cadence (default 3600 =
        60 min). NOT 15 min (would over-burn Open-Meteo quota when
        combined with the bot's 15-min poller).
      - ``WEATHER_HEALTH_SIDECAR_PATH`` — bronze_health.json path
        (default alongside bronze root).
      - ``RCLONE_REMOTE`` — rclone S3 remote name (default ``s3prod``).
      - ``S3_BUCKET`` — bucket name (default ``kalshi-bot-archive``).

    Boot sequence:
      1. Resolve config (env vars, with explicit args overriding).
      2. Restart sweep: re-upload any leftover outbox/ chunks AND
         re-rotate bare-in_flight_<usec>.jsonl orphans across the entire
         bronze tree (D0.3 §7 last paragraph + B-orphan-sweep AMENDMENT
         2026-05-19 ticket 86ba0jmz9 — symmetric salvage across both
         chunk-pair sides via uploader.salvage_in_flight_orphans).
      3. Allocate per-channel writers (one per channel in
         ``DEFAULT_CHANNELS``).
      4. Construct WeatherArchiver wired to the writers dict.
      5. Single drain thread fans out across all writers + writes the
         bronze_health.json sidecar.
      6. Poll loop: archiver.poll_once() then sleep until next tick.
      7. On shutdown: stop polling, close writers, join drain, final
         outbox sweep.
    """
    if bronze_root is None:
        bronze_root = Path(
            os.environ.get(
                "WEATHER_BRONZE_ROOT",
                "/var/lib/kalshi-weather-collector/bronze",
            )
        )
    bronze_root.mkdir(parents=True, exist_ok=True)

    if poll_interval_seconds is None:
        try:
            poll_interval_seconds = int(os.environ.get(
                "WEATHER_POLL_INTERVAL_SECONDS",
                DEFAULT_POLL_INTERVAL_SECONDS,
            ))
        except ValueError:
            logger.warning(
                "WEATHER_POLL_INTERVAL_SECONDS env var is non-integer; "
                "falling back to default %d.",
                DEFAULT_POLL_INTERVAL_SECONDS,
            )
            poll_interval_seconds = DEFAULT_POLL_INTERVAL_SECONDS

    health_sidecar_env = os.environ.get(
        "WEATHER_HEALTH_SIDECAR_PATH",
        # Default: alongside bronze data dir so a single mount holds
        # both data + observability state.
        str(bronze_root.parent / "bronze_health.json"),
    ).strip()
    health_sidecar_path: Optional[Path] = (
        Path(health_sidecar_env) if health_sidecar_env else None
    )

    uploader = _build_uploader()

    # Step 2 — restart sweep BEFORE new rotations start.
    n_swept = uploader.sweep_outbox(root_dir=bronze_root)
    if n_swept:
        logger.info(
            "Weather restart sweep re-uploaded %d leftover outbox chunks",
            n_swept,
        )

    # Step 3 — allocate per-channel writers.
    channels = DEFAULT_CHANNELS
    writers_by_channel = _build_writers(
        bronze_root, channels,
        rotation_interval_seconds=DEFAULT_ROTATION_INTERVAL_SECONDS,
    )
    all_writers: List[BronzeWriter] = list(writers_by_channel.values())

    # Step 4 — construct the archiver.
    archiver = WeatherArchiver(
        writers_by_channel=writers_by_channel,
        channels=channels,
    )

    owned_event = shutdown_event is None
    if owned_event:
        shutdown_event = threading.Event()
        # When the loop creates the event itself, install SIGINT/SIGTERM
        # handlers so a systemd `kill -TERM <pid>` (or a Ctrl-C in
        # development) triggers the graceful-shutdown finally block
        # below.
        try:
            signal.signal(signal.SIGINT, lambda *_: shutdown_event.set())
            signal.signal(signal.SIGTERM, lambda *_: shutdown_event.set())
        except ValueError:
            logger.warning(
                "Could not install SIGINT/SIGTERM handlers (not on main "
                "thread). External shutdown source must set the event."
            )

    # Step 5 — single drain thread fans out across ALL writers + writes
    # the bronze_health.json sidecar each tick.
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
        name="weather-bronze-drain",
    )
    drain_thread.start()

    logger.info(
        "Weather collector booted — bronze_root=%s, channels=%s, "
        "cities=%d, poll_interval_s=%d, health_sidecar=%s",
        bronze_root, list(channels), len(archiver._cities),
        poll_interval_seconds, health_sidecar_path,
    )

    try:
        # Step 6 — poll loop.
        while not shutdown_event.is_set():
            try:
                archiver.poll_once()
            except Exception:
                # Per-cycle failure must NOT terminate the loop. Log
                # and continue — the bot's weather poller has the same
                # posture (best-effort).
                logger.exception(
                    "WeatherArchiver.poll_once raised; sleeping until "
                    "next tick."
                )
            # Sleep until next tick (interruptible).
            shutdown_event.wait(timeout=poll_interval_seconds)
    finally:
        # Step 7 — graceful shutdown.
        shutdown_event.set()
        for writer in all_writers:
            try:
                writer.close()
            except Exception:
                logger.exception(
                    "writer.close() raised (channel=%s)",
                    getattr(writer, "channel", "?"),
                )
        drain_thread.join(timeout=10.0)
        # Final sweep — upload anything still in outbox/ that the drain
        # thread didn't reach (e.g., crash mid-write).
        try:
            n_final = uploader.sweep_outbox(root_dir=bronze_root)
            if n_final:
                logger.info(
                    "Weather final sweep re-uploaded %d outbox chunks "
                    "on shutdown",
                    n_final,
                )
        except Exception:
            logger.exception(
                "Weather final sweep_outbox raised on shutdown",
            )


if __name__ == "__main__":
    # logging.basicConfig matches collector/__main__.py + the
    # coinbase_main_loop entrypoint — must run BEFORE any module-load-
    # time getLogger calls fire so INFO/ERROR lines reach stderr (and
    # from there the systemd journal).
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stderr)],
        force=True,
    )
    run()
