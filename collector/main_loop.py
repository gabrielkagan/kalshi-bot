"""Collector run loop — D1.2 + D1.3 (tickets 86b9ypn66 + 86b9ypn72, 2026-05-16).

Orchestrates the bronze data plumbing in a multi-conn per-channel shape:

    SubscriptionManager.assign()  ──►  N ConnPlan instances
                                       (round-robin tickers across N conns)
                                                    │
                                                    ▼
    For each ConnPlan:
      build_subscribe_frames(plan) ──► subscribe_frames + cmd_id_to_channel
                                                    │
                                                    ▼
      Per-channel BronzeWriter instances ──►  writers_by_channel dict
                                              (one per channel + None
                                               for _unrouted fallback)
                                                    │
                                                    ▼
      BronzeArchiver(writers_by_channel=...,
                     subscribe_frames=...,
                     cmd_id_to_channel=...,
                     conn_id=plan.conn_id)
                                                    │
                                                    ▼
                                          WSClient ─►  on_session_start
                                                       dispatches subs
                                                       ─►  data frames
                                                       ─►  per-channel writer
                                                       ─►  rotation
                                                            │
                                                            ▼
                                                       drain thread
                                                       (fans out across
                                                        ALL writers)
                                                            │
                                                            ▼
                                                  rclone copyto → S3 → verify
                                                            │
                                                            ▼
                                                  delete local outbox + in-flight

D1.4 adds REST snapshot fallback (catalog refresh). D1.5 deploys via the
``ops/kalshi-collector.service`` systemd unit (requires-approval; 3 D0.3
§12 operator decisions still pending).

Sync + threading per CLAUDE.md anti-pattern ("Don't add async. Synchronous
+ threading for WS feeds is the design."). The drain thread is a daemon
that polls every writer's ``rotated_outbox_paths`` every second; production
frame cadence is well below 1Hz per (source, channel, conn), so polling
latency is bounded and acceptable.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from collector.subscription_manager import (
    CHANNELS_DEFAULT,
    DEFAULT_BATCH_SIZE,
    ConnPlan,
    SubscriptionManager,
)
from collector.uploader import RcloneUploader
from collector.writer import BronzeWriter
from collector.ws_connection import BronzeArchiver

logger = logging.getLogger(__name__)

# Drain-thread polling cadence. Frames arrive at <1Hz per partition;
# 1-second polling gives bounded uploader-latency without burning CPU.
_DRAIN_POLL_SECONDS: float = 1.0

# Per-archiver cmd_id starting offset — separates the cmd_id namespace
# of different conns so logging/correlation isn't ambiguous when a single
# operator reads journalctl across all archivers in one process. Each
# conn gets a 10K-id range, enough headroom for 15K subs per conn × 3
# channels even with batch_size=1 (worst case).
_PER_CONN_CMD_ID_STRIDE: int = 100_000


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


def _load_tickers_by_tier(
    path_str: str,
) -> Dict[object, Sequence[str]]:
    """Read the tier→tickers JSON file for the SubscriptionManager.

    D1.3 ship posture: the operator points ``COLLECTOR_TICKERS_FILE`` at
    a JSON file with shape ``{"<tier>": ["TICKER1", ...], ...}``. D1.4
    (REST snapshot) will replace this with a live catalog refresh; D1.3
    leaves the file-based seam so first-bronze-flow can be exercised
    end-to-end with a small hand-curated ticker set.

    Empty string / missing file / empty dict ⇒ ``{}`` (empty plan: WS
    conns connect but no subscribes go out, no data flows). This is the
    "stub deploy" posture that surfaces boot errors without producing
    real bronze data.
    """
    if not path_str:
        return {}
    p = Path(path_str)
    if not p.is_file():
        logger.warning(
            "COLLECTOR_TICKERS_FILE=%r not found; running with no tickers "
            "(WS will connect but no data will flow). Populate this file "
            "or wait for D1.4 REST snapshot.",
            path_str,
        )
        return {}
    try:
        raw = p.read_text()
        if not raw.strip():
            return {}
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error(
            "COLLECTOR_TICKERS_FILE=%r unreadable (%s); running with no "
            "tickers. Fix the file shape (top-level JSON object: "
            '{"<tier>": ["TICKER", ...]}) before retrying.',
            path_str, exc,
        )
        return {}
    if not isinstance(data, dict):
        logger.error(
            "COLLECTOR_TICKERS_FILE=%r top-level must be a JSON object; "
            "got %s. Running with no tickers.",
            path_str, type(data).__name__,
        )
        return {}
    # Validate each tier's value is a list of strings — soft-validate so
    # a partially-corrupt file still loads its valid tiers rather than
    # zeroing everything out.
    out: Dict[object, Sequence[str]] = {}
    for tier, tickers in data.items():
        if not isinstance(tickers, list):
            logger.warning(
                "COLLECTOR_TICKERS_FILE tier=%r value is not a list "
                "(got %s); skipping this tier.",
                tier, type(tickers).__name__,
            )
            continue
        out[tier] = [t for t in tickers if isinstance(t, str)]
    return out


def _s3_key_from_outbox(outbox_path: Path, bronze_root: Path) -> str:
    """Derive the S3 key for a rotated outbox/<chunk>.jsonl.zst.

    Strips the trailing ``/outbox/`` segment so silver ETL sees one flat
    hive partition under ``bronze/``, not an ``outbox/`` subdir.
    """
    rel = outbox_path.relative_to(bronze_root).as_posix()
    return "bronze/" + rel.replace("/outbox/", "/")


def _drain_rotated(
    writers: Sequence[BronzeWriter],
    uploader: RcloneUploader,
    bronze_root: Path,
    shutdown_event: threading.Event,
) -> None:
    """Drain ``rotated_outbox_paths`` across ALL writers → uploader until shutdown.

    Multi-conn fan-out (D1.3): the single drain thread iterates every
    writer's `rotated_outbox_paths` list. Per-writer queues stay small
    (each writer rotates ≤1 chunk every 5min or 100MB), so cross-writer
    polling is bounded.

    Runs on a daemon thread so the main thread can block on shutdown.
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

    def _drain_writers_once() -> None:
        for writer in writers:
            # pop in-place so a second iteration doesn't double-drain.
            while writer.rotated_outbox_paths:
                outbox_path, in_flight_path = writer.rotated_outbox_paths.pop(0)
                _drain_one(outbox_path, in_flight_path)

    while not shutdown_event.is_set():
        _drain_writers_once()
        shutdown_event.wait(timeout=_DRAIN_POLL_SECONDS)
    # Final drain post-shutdown.
    _drain_writers_once()


def _build_writers_for_plan(
    plan: ConnPlan, bronze_root: Path,
) -> Dict[Optional[str], BronzeWriter]:
    """Allocate per-(channel, conn) BronzeWriter instances for one ConnPlan.

    The dict carries a writer for every channel in plan.channels PLUS
    a None-keyed writer for the _unrouted fallback partition (used when
    a data frame's sid is unmapped — race between subscribe-burst and
    first data frame). All writers share ``source="kalshi_ws"`` and the
    plan's ``conn_id``; they differ only by ``channel``.
    """
    writers: Dict[Optional[str], BronzeWriter] = {}
    # Channel set + None fallback.
    for channel in plan.channels:
        writers[channel] = BronzeWriter(
            root_dir=bronze_root,
            source="kalshi_ws",
            channel=channel,
            conn=plan.conn_id,
        )
    writers[None] = BronzeWriter(
        root_dir=bronze_root,
        source="kalshi_ws",
        channel=None,
        conn=plan.conn_id,
    )
    return writers


def run(
    *,
    bronze_root: Optional[Path] = None,
    api_key: Optional[str] = None,
    private_key_path: Optional[str] = None,
    shutdown_event: Optional[threading.Event] = None,
) -> None:
    """Boot the collector — wire SubscriptionManager → archivers → S3.

    Args are mostly for testability; in production all defaults come
    from env vars (see ``_required_env`` + ``_load_tickers_by_tier``).
    Pass ``shutdown_event`` to drive shutdown from a test harness
    without signal-handler pollution.

    Env-driven config:
      - ``KALSHI_COLLECTOR_KEY_ID`` (required) — Kalshi API key id
      - ``KALSHI_COLLECTOR_KEY_PATH`` (required) — RSA-PSS PEM path
      - ``COLLECTOR_BRONZE_ROOT`` — bronze root dir (default
        ``/var/lib/kalshi-collector/bronze``)
      - ``COLLECTOR_CONN_COUNT`` — number of WS conns (default 1;
        D0.2 strict-tested floor is 7-8 at 10K subs/conn)
      - ``COLLECTOR_TICKERS_FILE`` — path to JSON
        ``{"<tier>": ["TICKER", ...]}`` map (default empty — no subs
        sent, WS connects then idles; D1.4 replaces with REST snapshot)
      - ``COLLECTOR_BATCH_SIZE`` — subscribe-frame batch size (default
        1000; tunable for Kalshi WS message-size constraints)
      - ``RCLONE_REMOTE`` — rclone S3 remote name (default ``s3prod``)
      - ``S3_BUCKET`` — bucket name (default ``kalshi-bot-archive``)

    Boot sequence:
      1. Resolve config (env vars, with explicit args overriding).
      2. Restart sweep: re-upload any leftover outbox/ chunks across
         the entire bronze tree (D0.3 §7 last paragraph).
      3. Plan subscriptions via SubscriptionManager.assign().
      4. For each ConnPlan: build per-channel writers + subscribe-frames
         + archiver.
      5. Single drain thread fans out across all writers.
      6. Hand control to per-conn ``BronzeArchiver.start()``; main
         thread blocks on shutdown_event.
      7. On shutdown: stop all archivers, close all writers, join drain,
         final outbox sweep.
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

    conn_count = int(os.environ.get("COLLECTOR_CONN_COUNT", "1"))
    if conn_count < 1:
        raise EnvironmentError(
            f"COLLECTOR_CONN_COUNT={conn_count} invalid (must be ≥ 1)."
        )
    tickers_file = os.environ.get("COLLECTOR_TICKERS_FILE", "").strip()
    batch_size = int(os.environ.get(
        "COLLECTOR_BATCH_SIZE", str(DEFAULT_BATCH_SIZE)))

    uploader = _build_uploader()

    # Step 2 — restart sweep BEFORE new rotations start. sweep_outbox
    # recursively walks bronze_root so it covers EVERY (channel, conn)
    # partition allocated below.
    n_swept = uploader.sweep_outbox(root_dir=bronze_root)
    if n_swept:
        logger.info(
            "Restart sweep re-uploaded %d leftover outbox chunks", n_swept
        )

    # Step 3 — plan subscriptions.
    tickers_by_tier = _load_tickers_by_tier(tickers_file)
    mgr = SubscriptionManager(
        tickers_by_tier=tickers_by_tier,
        conn_count=conn_count,
        channels=CHANNELS_DEFAULT,
        batch_size=batch_size,
    )
    plans = mgr.assign()

    # Step 4 — per-conn components.
    archivers: List[BronzeArchiver] = []
    all_writers: List[BronzeWriter] = []
    for idx, plan in enumerate(plans):
        writers_by_channel = _build_writers_for_plan(plan, bronze_root)
        all_writers.extend(writers_by_channel.values())
        subscribe_frames, cmd_id_to_channel = SubscriptionManager.build_subscribe_frames(
            plan,
            cmd_id_start=idx * _PER_CONN_CMD_ID_STRIDE + 1,
            batch_size=batch_size,
        )
        archiver = BronzeArchiver(
            api_key=api_key,
            private_key_path=private_key_path,
            writers_by_channel=writers_by_channel,
            subscribe_frames=subscribe_frames,
            cmd_id_to_channel=cmd_id_to_channel,
            conn_id=plan.conn_id,
        )
        archivers.append(archiver)
        logger.info(
            "Collector conn=%s wired: tickers=%d channels=%s subscribes=%d",
            plan.conn_id, len(plan.market_tickers),
            list(plan.channels), len(subscribe_frames),
        )

    owned_event = shutdown_event is None
    if owned_event:
        shutdown_event = threading.Event()
        # When main_loop creates the event itself, install SIGINT/SIGTERM
        # handlers so a systemd `kill -TERM <pid>` (or a Ctrl-C in
        # development) triggers the graceful-shutdown finally block
        # below — close all writers, drain rotations, sweep outbox. Tests
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

    # Step 5 — single drain thread fans out across ALL writers.
    drain_thread = threading.Thread(
        target=_drain_rotated,
        kwargs={
            "writers": all_writers,
            "uploader": uploader,
            "bronze_root": bronze_root,
            "shutdown_event": shutdown_event,
        },
        daemon=True,
        name="bronze-drain",
    )
    drain_thread.start()

    logger.info(
        "Collector booted — bronze_root=%s, conn_count=%d, archivers=%d",
        bronze_root, conn_count, len(archivers),
    )

    try:
        # Step 6 — start all archivers, then block until shutdown.
        for archiver in archivers:
            archiver.start()
        shutdown_event.wait()
    finally:
        # Step 7 — graceful shutdown.
        shutdown_event.set()
        for archiver in archivers:
            try:
                archiver.stop()
            except Exception:
                logger.exception(
                    "archiver.stop() raised for conn=%s",
                    getattr(archiver, "_conn_id", "?"),
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
                    "Final sweep re-uploaded %d outbox chunks on shutdown",
                    n_final,
                )
        except Exception:
            logger.exception("final sweep_outbox raised on shutdown")
