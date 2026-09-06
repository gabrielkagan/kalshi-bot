"""ESPN collector run loop — D1.11.a (ticket 86ba0ppy0, 2026-05-19).

Orchestrator invoked by ``ops/kalshi-espn-collector.service`` via
``espn-collector-start.sh`` as ``python3 -m collector.espn_main_loop``.

Mirrors ``collector/weather_main_loop.py`` (D1.8) for the ESPN deltas:

  - **HTTP-poll loop, NOT a WS reader.** No WSClient construction.
    The run loop fires ``archiver.poll_once()`` on a 60-second cadence
    (configurable via ``ESPN_POLL_INTERVAL_SECONDS``) + sleeps
    between cycles.
  - **23 BronzeWriters** (one per enabled league in
    ``espn_archiver.LEAGUES_ESPN``) at ``interval_seconds=3600``
    (60-min rotation, NOT D0.3 §4 default 5-min). The 60-min cadence
    keeps each chunk to ~60 polls' payload (~30-50 KB compressed per
    league per hour at active load; far less when idle); the D0.3 §4
    default would produce sub-optimal chunks at the 60s poll rate.
  - **No PEM / no auth.** ESPN site.api.espn.com is free + keyless.
    The dedicated ``.env.espn-collector`` carries only
    ``ESPN_BRONZE_ROOT`` / ``ESPN_POLL_INTERVAL_SECONDS`` /
    ``RCLONE_REMOTE`` / ``S3_BUCKET`` knobs.
  - **Same drain thread + uploader pattern** as Kalshi/Coinbase/Weather
    sides.

Bronze partition path (per D0.3 §3, with ``_conn=None``):

    bronze/espn/<league>/year=YYYY/month=MM/day=DD/hour=HH/conn=none/<chunk>.jsonl.zst

Anti-patterns honored (root ``CLAUDE.md``):
  - Synchronous + threading (NOT asyncio at the public API).
  - SAME venv as bot + Kalshi/Coinbase/Weather collectors (requests is
    already a bot dependency).
  - NO ``bot.*`` imports — pinned by ``collector-no-bot`` import-
    linter contract + AST defense-in-depth in
    ``tests/contracts/test_collector_espn_main_loop.py``.

D0.3 §10 bot-isolation invariant: collector failure ⇒ bot keeps
trading; bot failure ⇒ collector keeps capturing. The systemd unit's
``MemoryMax=256M`` + ``MemorySwapMax=0`` + ``Nice=10`` enforce the
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

from collector.espn_archiver import (
    DEFAULT_CHANNELS,
    ESPNArchiver,
    SOURCE as _BRONZE_SOURCE,
)
from collector.uploader import RcloneUploader
from collector.writer import BronzeWriter

logger = logging.getLogger(__name__)

# Drain-thread polling cadence. ESPN is light — 23 leagues × 60s polls
# = ~23 envelopes per second peak (well below WS-collector burst rates).
# 5s polling is generous; rotation cadence (60-min) bounds drain backlog.
_DRAIN_POLL_SECONDS: float = 5.0

# Default poll interval in seconds (60s). Justified at the D1.11.a
# plan-doc §2:
#   - Bot fidelity: matches the bot's effective ESPN polling cadence
#     during live games. Captures every meaningful state transition
#     (basketball clock 60s+ granularity).
#   - No quota: ESPN doesn't document a rate limit at site.api.espn.com;
#     bot has polled at this rate for ~year without throttling.
#   - 1440 ticks/day × 23 leagues = 33,120 calls/day — negligible.
DEFAULT_POLL_INTERVAL_SECONDS: int = 60

# Default rotation cadence (60 min). Aligns chunk boundaries with the
# hour-partition boundaries (D0.3 §3). At 60s poll cadence, each writer
# sees ~60 envelopes per hour; rotation absorbs that cleanly without
# producing sub-DEEP_ARCHIVE-minimum chunks.
DEFAULT_ROTATION_INTERVAL_SECONDS: int = 3600


def _build_uploader() -> RcloneUploader:
    rclone_remote = os.environ.get("RCLONE_REMOTE", "s3prod")
    bucket = os.environ.get("S3_BUCKET", "kalshi-bot-archive")
    return RcloneUploader(rclone_remote=rclone_remote, bucket=bucket)


def _s3_key_from_outbox(outbox_path: Path, bronze_root: Path) -> str:
    """Derive the S3 key for a rotated outbox/<chunk>.jsonl.zst.

    Mirrors ``collector.weather_main_loop._s3_key_from_outbox`` —
    strips the trailing ``/outbox/`` segment so silver ETL sees one
    flat hive partition under ``bronze/``, not an ``outbox/`` subdir.
    """
    rel = outbox_path.relative_to(bronze_root).as_posix()
    return "bronze/" + rel.replace("/outbox/", "/")


def write_bronze_health_sidecar(
    archiver: ESPNArchiver,
    writers: Sequence[BronzeWriter],
    path: Path,
) -> None:
    """Write a bronze health snapshot JSON file for the cron monitor.

    Mirror of ``collector.weather_main_loop.write_bronze_health_sidecar``
    schema (schema_version=1). ESPN has no worker-queue drops by design
    (single synchronous HTTP-poll loop; ``BronzeWriter.write`` is
    in-line). We still emit a ``total_dropped_frames=0`` entry to keep
    schema parity with the Kalshi/Coinbase/Weather sidecars so the
    monitor's tier-uniform shape works without per-tier branches.

    Ticket 86bbvqhyr (2026-09-06): ADDITIVE ``espn_http_status_1h`` key
    (per-league rolling-1h polls / non_200 / non_200_rate / last_status
    from ``ESPNArchiver.get_http_status_stats``) so
    ``collector_health_monitor.check_espn_http_errors`` can alert on the
    CONTENT class of what is landing, not just that chunks land.
    schema_version STAYS 1 — additive backward-compat, same precedent as
    ``write_queue_peak_size`` (86ba1xraq).

    Atomic-replace via tmp file + os.replace so the cron-driven monitor
    never observes a torn JSON write.
    """
    written_at = _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ",
    )
    archiver_snapshot = {
        "conn_id": None,
        "dropped_frames": 0,
        "write_queue_size": 0,
        "write_queue_maxsize": 0,
        "write_worker_alive": True,
        "collector_seq": getattr(archiver, "_collector_seq", 0),
        "ack_frames_processed": 0,
    }
    _stats_fn = getattr(archiver, "get_http_status_stats", None)
    espn_http_status_1h = _stats_fn() if callable(_stats_fn) else {}
    payload = {
        "schema_version": 1,
        "written_at": written_at,
        "archivers": [archiver_snapshot],
        "total_dropped_frames": 0,
        "total_queue_size": 0,
        "espn_http_status_1h": espn_http_status_1h,
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
    archiver: Optional[ESPNArchiver] = None,
    health_sidecar_path: Optional[Path] = None,
) -> None:
    """Drain rotated_outbox_paths across all writers → uploader until shutdown.

    Daemon thread; polls every writer's ``rotated_outbox_paths`` every
    ``_DRAIN_POLL_SECONDS``. ESPN rotation cadence (60-min) bounds
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

    ESPN has no ``conn=`` dimension (HTTP polling has no persistent
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
    """Boot the ESPN collector — wire ESPNArchiver → writers → S3.

    Args are mostly for testability; in production all defaults come
    from env vars.

    Env-driven config:
      - ``ESPN_BRONZE_ROOT`` — bronze root dir (default
        ``/var/lib/kalshi-espn-collector/bronze``).
      - ``ESPN_POLL_INTERVAL_SECONDS`` — poll cadence (default 60).
      - ``ESPN_HEALTH_SIDECAR_PATH`` — bronze_health.json path
        (default alongside bronze root).
      - ``RCLONE_REMOTE`` — rclone S3 remote name (default ``s3prod``).
      - ``S3_BUCKET`` — bucket name (default ``kalshi-bot-archive``).

    Boot sequence:
      1. Resolve config (env vars, with explicit args overriding).
      2. Restart sweep: re-upload any leftover outbox/ chunks.
      3. Allocate per-channel writers (one per league in DEFAULT_CHANNELS).
      4. Construct ESPNArchiver wired to the writers dict.
      5. Single drain thread fans out across all writers + writes the
         bronze_health.json sidecar.
      6. Poll loop: archiver.poll_once() then sleep until next tick.
      7. On shutdown: stop polling, close writers, join drain, final
         outbox sweep.
    """
    if bronze_root is None:
        bronze_root = Path(
            os.environ.get(
                "ESPN_BRONZE_ROOT",
                "/var/lib/kalshi-espn-collector/bronze",
            )
        )
    bronze_root.mkdir(parents=True, exist_ok=True)

    if poll_interval_seconds is None:
        try:
            poll_interval_seconds = int(os.environ.get(
                "ESPN_POLL_INTERVAL_SECONDS",
                DEFAULT_POLL_INTERVAL_SECONDS,
            ))
        except ValueError:
            logger.warning(
                "ESPN_POLL_INTERVAL_SECONDS env var is non-integer; "
                "falling back to default %d.",
                DEFAULT_POLL_INTERVAL_SECONDS,
            )
            poll_interval_seconds = DEFAULT_POLL_INTERVAL_SECONDS

    health_sidecar_env = os.environ.get(
        "ESPN_HEALTH_SIDECAR_PATH",
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
            "ESPN restart sweep re-uploaded %d leftover outbox chunks",
            n_swept,
        )

    # Step 3 — allocate per-channel writers (one per league).
    channels = DEFAULT_CHANNELS
    writers_by_channel = _build_writers(
        bronze_root, channels,
        rotation_interval_seconds=DEFAULT_ROTATION_INTERVAL_SECONDS,
    )
    all_writers: List[BronzeWriter] = list(writers_by_channel.values())

    # Step 4 — construct the archiver.
    archiver = ESPNArchiver(
        writers_by_channel=writers_by_channel,
        leagues=channels,
    )

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
        name="espn-bronze-drain",
    )
    drain_thread.start()

    logger.info(
        "ESPN collector booted — bronze_root=%s, channels=%d, "
        "poll_interval_s=%d, health_sidecar=%s",
        bronze_root, len(channels), poll_interval_seconds,
        health_sidecar_path,
    )

    try:
        # Step 6 — poll loop.
        while not shutdown_event.is_set():
            try:
                archiver.poll_once()
            except Exception:
                logger.exception(
                    "ESPNArchiver.poll_once raised; sleeping until "
                    "next tick."
                )
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
        try:
            n_final = uploader.sweep_outbox(root_dir=bronze_root)
            if n_final:
                logger.info(
                    "ESPN final sweep re-uploaded %d outbox chunks "
                    "on shutdown",
                    n_final,
                )
        except Exception:
            logger.exception(
                "ESPN final sweep_outbox raised on shutdown",
            )


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
