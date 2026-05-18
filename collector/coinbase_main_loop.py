"""Coinbase collector run loop — D2.5 (ticket 86b9znq4w, 2026-05-18).

Orchestrator invoked by ``ops/kalshi-coinbase-collector.service`` via
``coinbase-collector-start.sh`` as ``python3 -m collector.coinbase_main_loop``.
Mirrors ``collector/main_loop.py`` (Kalshi-side D1.2-D1.6 ship arc)
adapted for the Coinbase-side structural deltas:

  - **Single-conn.** Coinbase Exchange WS is single-connection by
    design (D2.2 docstring); one WSClient covers all subscribed
    product_ids × channels. No SubscriptionManager fan-out / no
    ConnPlan iteration.
  - **No REST refresh.** Coinbase product_ids are static at
    ``coinbase_wire.ws_client.DEFAULT_PRODUCT_IDS`` — adding a product
    is a repo-commit + deploy event, not an hourly REST poll. No
    ``RestSnapshotRefresher`` wiring.
  - **No PEM / no RSA-PSS auth.** D2.5 SHIPPED with public channels
    only (per D2.1.5 narrowed auth scope). The wire library's
    ``coinbase_wire.auth`` HMAC sign + ws_headers helpers remain stub-
    sentinels for a future private-channel Bit; this loop never calls
    them.
  - **5-channel default.** R0 reachability spike (2026-05-18) verified
    ``level2_batch`` is publicly subscribable on the Exchange WS
    endpoint (1 snapshot + 502 l2update frames over 30s for BTC-USD
    alone, no ``type=error``). D2.5 bundles the level2_batch promotion
    into ``coinbase_wire.DEFAULT_CHANNELS``; the per-channel writer
    allocation walks the wire library's published default + adds the
    ``None``-keyed ``_unrouted`` fallback.

Bronze partition path (per D0.3 §3):

    bronze/coinbase_ws/<channel>/year=YYYY/month=MM/day=DD/hour=HH/conn=A/<chunk>.jsonl.zst

Single-conn means ``conn=A`` is the only conn segment. The post-D2.5
default writer set is 6 writers: ticker, matches, heartbeat, status,
level2_batch, plus the None-keyed _unrouted writer for frames whose
``msg_type`` falls outside the static dispatch table.

Anti-patterns honored (root ``CLAUDE.md``):

  - Synchronous + threading (NOT asyncio at the public API). asyncio
    lives INSIDE ``coinbase_wire.WSClient`` but does not leak.
  - SAME venv as bot + Kalshi collector — websockets + zstandard +
    cryptography are shared (D0.3 §6 isolation strengthens at
    deploy.yml D1.5.1 `pip install -r requirements.txt`).
  - NO ``bot.*`` imports — pinned by ``collector-no-bot`` import-linter
    contract (Contract 7) + AST defense-in-depth in
    ``tests/contracts/test_collector_coinbase_main_loop.py``.

Per-tick drain thread fans out across all writers (same shape as the
Kalshi side); each archiver's health snapshot is written to a
JSON sidecar for the cron-driven
``scripts/ops/collector_health_monitor.py`` to poll.

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

from collector.coinbase_archiver import CoinbaseArchiver
from collector.uploader import RcloneUploader
from collector.writer import BronzeWriter
from coinbase_wire.ws_client import (
    DEFAULT_CHANNELS,
    DEFAULT_PRODUCT_IDS,
    DEFAULT_WS_URL,
)

logger = logging.getLogger(__name__)

# Drain-thread polling cadence. Coinbase steady-state load is ~50-200
# frames/sec across all 5 channels × 7 products; 1s polling gives
# bounded uploader latency without burning CPU. Same cadence as the
# Kalshi side for cross-collector consistency.
_DRAIN_POLL_SECONDS: float = 1.0

# Single-conn identifier — Coinbase Exchange WS does not shard
# (D2.2 docstring); reserves the sharding seam for a future Bit if
# subscribe-fanout ever justifies it.
_COINBASE_CONN_ID: str = "A"

# Bronze source string — fed into BronzeWriter + envelope ``_source``
# field. Pinned at the CoinbaseArchiver D2.2 ship; replicated here so
# writer allocation matches the dispatcher's hardcoded source string.
# Any future change to this string must be made in BOTH places + the
# bronze partition prefix audit (D0.3 §3) must be updated.
_BRONZE_SOURCE: str = "coinbase_ws"


def _build_uploader() -> RcloneUploader:
    rclone_remote = os.environ.get("RCLONE_REMOTE", "s3prod")
    bucket = os.environ.get("S3_BUCKET", "kalshi-bot-archive")
    return RcloneUploader(rclone_remote=rclone_remote, bucket=bucket)


def _s3_key_from_outbox(outbox_path: Path, bronze_root: Path) -> str:
    """Derive the S3 key for a rotated outbox/<chunk>.jsonl.zst.

    Strips the trailing ``/outbox/`` segment so silver ETL sees one
    flat hive partition under ``bronze/``, not an ``outbox/`` subdir.
    Mirrors ``collector.main_loop._s3_key_from_outbox`` exactly so the
    bronze tape from both collectors lands in the same partition
    shape under the same bucket prefix.
    """
    rel = outbox_path.relative_to(bronze_root).as_posix()
    return "bronze/" + rel.replace("/outbox/", "/")


def write_bronze_health_sidecar(
    archiver: CoinbaseArchiver,
    path: Path,
) -> None:
    """Write a single-archiver bronze health snapshot JSON file.

    Mirrors ``collector.main_loop.write_bronze_health_sidecar`` schema
    (schema_version=1, archivers list, total_dropped_frames,
    total_queue_size) but with a single-element ``archivers`` list
    (Coinbase is single-conn). Separate sidecar file from the Kalshi
    side per the D2.5 Option B isolation posture — defaults to
    ``/var/lib/kalshi-coinbase-collector/bronze_health.json``.

    Atomic-replace via tmp file + os.replace so the cron-driven
    monitor never observes a torn JSON write.
    """
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
    writers: Sequence[BronzeWriter],
    uploader: RcloneUploader,
    bronze_root: Path,
    shutdown_event: threading.Event,
    archiver: Optional[CoinbaseArchiver] = None,
    health_sidecar_path: Optional[Path] = None,
) -> None:
    """Drain rotated_outbox_paths across all writers → uploader until shutdown.

    Daemon thread; polls every writer's ``rotated_outbox_paths`` every
    ``_DRAIN_POLL_SECONDS``. Per-writer queues stay small (each rotates
    ≤1 chunk per 5min or 100MB), so cross-writer polling is bounded.

    On shutdown, drains any final rotations before returning. Caller
    joins us with a bounded timeout.
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
            write_bronze_health_sidecar(archiver, health_sidecar_path)
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
) -> Dict[Optional[str], BronzeWriter]:
    """Allocate per-channel BronzeWriter instances + None-keyed fallback.

    Returns a dict keyed by channel name (str) or None (for the
    _unrouted partition used by CoinbaseArchiver when ``msg_type``
    doesn't appear in its dispatch table). All writers share
    ``source="coinbase_ws"`` and ``conn=_COINBASE_CONN_ID``; they
    differ only by ``channel``.
    """
    writers: Dict[Optional[str], BronzeWriter] = {}
    for channel in channels:
        writers[channel] = BronzeWriter(
            root_dir=bronze_root,
            source=_BRONZE_SOURCE,
            channel=channel,
            conn=_COINBASE_CONN_ID,
        )
    writers[None] = BronzeWriter(
        root_dir=bronze_root,
        source=_BRONZE_SOURCE,
        channel=None,
        conn=_COINBASE_CONN_ID,
    )
    return writers


def run(
    *,
    bronze_root: Optional[Path] = None,
    url: Optional[str] = None,
    shutdown_event: Optional[threading.Event] = None,
) -> None:
    """Boot the Coinbase collector — wire CoinbaseArchiver → writers → S3.

    Args are mostly for testability; in production all defaults come
    from env vars (see below) + the wire library's published constants
    (``DEFAULT_WS_URL``, ``DEFAULT_CHANNELS``, ``DEFAULT_PRODUCT_IDS``).
    Pass ``shutdown_event`` to drive shutdown from a test harness
    without signal-handler pollution.

    Env-driven config:
      - ``COINBASE_BRONZE_ROOT`` — bronze root dir (default
        ``/var/lib/kalshi-coinbase-collector/bronze``)
      - ``COINBASE_HEALTH_SIDECAR_PATH`` — bronze_health.json path
        (default alongside bronze root)
      - ``RCLONE_REMOTE`` — rclone S3 remote name (default ``s3prod``)
      - ``S3_BUCKET`` — bucket name (default ``kalshi-bot-archive``)

    Boot sequence:
      1. Resolve config (env vars, with explicit args overriding).
      2. Restart sweep: re-upload any leftover outbox/ chunks across
         the entire bronze tree (D0.3 §7 last paragraph).
      3. Allocate per-channel writers (one per channel in
         ``DEFAULT_CHANNELS`` + None-keyed fallback).
      4. Construct CoinbaseArchiver wired to the writers dict.
      5. Single drain thread fans out across all writers + writes the
         bronze_health.json sidecar.
      6. Start the archiver; main thread blocks on shutdown_event.
      7. On shutdown: stop archiver, close writers, join drain, final
         outbox sweep.
    """
    if bronze_root is None:
        bronze_root = Path(
            os.environ.get(
                "COINBASE_BRONZE_ROOT",
                "/var/lib/kalshi-coinbase-collector/bronze",
            )
        )
    bronze_root.mkdir(parents=True, exist_ok=True)

    health_sidecar_env = os.environ.get(
        "COINBASE_HEALTH_SIDECAR_PATH",
        # Default: alongside bronze data dir so a single mount holds
        # both data + observability state.
        str(bronze_root.parent / "bronze_health.json"),
    ).strip()
    health_sidecar_path: Optional[Path] = (
        Path(health_sidecar_env) if health_sidecar_env else None
    )

    if url is None:
        url = DEFAULT_WS_URL

    uploader = _build_uploader()

    # Step 2 — restart sweep BEFORE new rotations start. sweep_outbox
    # recursively walks bronze_root so it covers every channel's
    # partition allocated below.
    n_swept = uploader.sweep_outbox(root_dir=bronze_root)
    if n_swept:
        logger.info(
            "Coinbase restart sweep re-uploaded %d leftover outbox chunks",
            n_swept,
        )

    # Step 3 — allocate per-channel writers.
    channels = DEFAULT_CHANNELS
    writers_by_channel = _build_writers(bronze_root, channels)
    all_writers: List[BronzeWriter] = list(writers_by_channel.values())

    # Step 4 — construct the archiver. CoinbaseArchiver's D2.2
    # constructor defaults align with the wire library's published
    # defaults (channels + product_ids), so passing them explicitly is
    # belt-and-suspenders for AST-discoverability.
    archiver = CoinbaseArchiver(
        writers_by_channel=writers_by_channel,
        conn_id=_COINBASE_CONN_ID,
        url=url,
        channels=channels,
        product_ids=DEFAULT_PRODUCT_IDS,
    )

    owned_event = shutdown_event is None
    if owned_event:
        shutdown_event = threading.Event()
        # When the loop creates the event itself, install SIGINT/SIGTERM
        # handlers so a systemd `kill -TERM <pid>` (or a Ctrl-C in
        # development) triggers the graceful-shutdown finally block
        # below. Tests pass an explicit shutdown_event and bypass this
        # path. ValueError tolerance: signal.signal raises on non-main
        # thread.
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
        name="coinbase-bronze-drain",
    )
    drain_thread.start()

    logger.info(
        "Coinbase collector booted — bronze_root=%s, channels=%s, "
        "products=%d, url=%s, health_sidecar=%s",
        bronze_root, list(channels), len(DEFAULT_PRODUCT_IDS), url,
        health_sidecar_path,
    )

    try:
        # Step 6 — start the archiver, then block until shutdown.
        archiver.start()
        shutdown_event.wait()
    finally:
        # Step 7 — graceful shutdown.
        shutdown_event.set()
        try:
            archiver.stop()
        except Exception:
            logger.exception(
                "Coinbase archiver.stop() raised (conn=%s)",
                _COINBASE_CONN_ID,
            )
        for writer in all_writers:
            try:
                writer.close()
            except Exception:
                logger.exception(
                    "writer.close() raised (channel=%s conn=%s)",
                    getattr(writer, "channel", "?"),
                    getattr(writer, "conn", "?"),
                )
        drain_thread.join(timeout=10.0)
        # Final sweep — upload anything still in outbox/ that the drain
        # thread didn't reach (e.g., crash mid-write).
        try:
            n_final = uploader.sweep_outbox(root_dir=bronze_root)
            if n_final:
                logger.info(
                    "Coinbase final sweep re-uploaded %d outbox chunks "
                    "on shutdown",
                    n_final,
                )
        except Exception:
            logger.exception(
                "Coinbase final sweep_outbox raised on shutdown",
            )


if __name__ == "__main__":
    # logging.basicConfig matches collector/__main__.py — must run
    # BEFORE any module-load-time getLogger calls fire so INFO/ERROR
    # lines reach stderr (and from there the systemd journal).
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stderr)],
        force=True,
    )
    run()
