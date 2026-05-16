"""Collector run loop — D1.2 + D1.3 + D1.4 (tickets 86b9ypn66 + 86b9ypn72 + 86b9ypn8r, 2026-05-16).

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

D1.4 (`86b9ypn8r`) added the periodic REST catalog refresh: when no
``COLLECTOR_TICKERS_FILE`` is configured, the collector pulls the open-
market universe from Kalshi's REST ``/markets`` endpoint at boot and
re-polls hourly. On ticker-set changes the refresher invokes a callback
that rebuilds per-conn subscribe frames + force-reconnects each WS conn
so the new subscriptions take effect (Kalshi has no in-session
add/remove; reconnect-and-resubscribe is the protocol-level mechanism).
D1.5 (SHIPPED 2026-05-16, ticket ``86b9ypna4``, requires-approval)
deploys this loop via the ``ops/kalshi-collector.service`` systemd
unit on the VPS. The unit sources ``/home/botuser/.env.collector``
(dedicated home-rooted env file — separate from the bot's repo-
rooted ``.env``) so credential rotation cannot disturb the bot. The
3 D0.3 §12 operator decisions resolved at D1.5 kickoff:
``Restart=on-failure`` + ``RestartSec=10s`` + ``Nice=10`` lifecycle
posture; lifecycle Standard → DEEP_ARCHIVE @ 30d (skip IA);
``KALSHI_COLLECTOR_KEY_ID`` provisioned via operator runbook.

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

from collector.rest_snapshot import (
    DEFAULT_REFRESH_INTERVAL_SECONDS,
    RestSnapshotRefresher,
    fetch_tickers_by_tier,
)
from collector.subscription_manager import (
    CHANNELS_DEFAULT,
    DEFAULT_BATCH_SIZE,
    ConnPlan,
    SubscriptionManager,
)
from collector.uploader import RcloneUploader
from collector.writer import BronzeWriter
from collector.ws_connection import BronzeArchiver
from kalshi_wire.auth import load_private_key

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
            f"D1.5 sources /home/botuser/.env.collector (NOT the bot's "
            f".env); confirm the env file exists and contains "
            f"{name}=... (operator runbook in ops/CLAUDE.md). The "
            f"bronze collector cannot boot without auth credentials."
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
    SHIPPED the REST catalog refresh as the production default; this
    file-based seam is retained for offline / test boot (when the
    operator hand-curates a ticker set) and is selected when the env
    var is present + non-empty.

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
            "or unset the env var to fall back to the D1.4 REST refresher.",
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


def _replan_for_archivers(
    *,
    new_tickers_by_tier: Mapping[object, Sequence[str]],
    archivers: Sequence[BronzeArchiver],
    conn_count: int,
    batch_size: int,
) -> None:
    """REST-refresh callback: rebuild subscribe frames + force-reconnect.

    Called by RestSnapshotRefresher when the REST ticker set changes.
    Re-plans the per-conn ticker assignment using the same
    SubscriptionManager round-robin shape used at boot, then for each
    archiver:

      1. ``update_subscriptions(new_frames, new_cmd_id_to_channel)`` —
         atomically replaces the archiver's subscribe payload. The swap
         itself is lock-held; the reader sites (``_on_session_start`` +
         ``_handle_subscribe_ack``) rely on single-bytecode-op attribute
         reads under the GIL — see ``BronzeArchiver.update_subscriptions``
         docstring for the full safety model (R1-M4 retracted a broader
         protection claim that would be false if a future change
         iterated the dict multi-step).
      2. ``request_reconnect()`` — signals the WSClient to drop the
         current session. The session-end callback clears sid→channel;
         the session-start callback dispatches the NEW subscribe frames
         (Kalshi has no in-session add/remove, so reconnect-and-
         re-subscribe is the protocol-level update mechanism).

    cmd_id stride: refresh tick uses the same per-conn stride as boot
    so log/journalctl correlation across boot + refresh stays
    unambiguous (boot uses idx*100_000+1; refresh re-uses the same
    starting cmd_id since Kalshi sids reset per session and ack
    correlation is session-scoped anyway).
    """
    mgr = SubscriptionManager(
        tickers_by_tier=new_tickers_by_tier,
        conn_count=conn_count,
        channels=CHANNELS_DEFAULT,
        batch_size=batch_size,
    )
    plans = mgr.assign()
    if len(plans) != len(archivers):
        logger.warning(
            "Replan: plan_count=%d ≠ archiver_count=%d; skipping replan.",
            len(plans), len(archivers),
        )
        return
    for idx, (plan, archiver) in enumerate(zip(plans, archivers)):
        new_frames, new_map = SubscriptionManager.build_subscribe_frames(
            plan,
            cmd_id_start=idx * _PER_CONN_CMD_ID_STRIDE + 1,
            batch_size=batch_size,
        )
        try:
            archiver.update_subscriptions(new_frames, new_map)
            archiver.request_reconnect()
            logger.info(
                "Replan conn=%s: subscribe_frames=%d → reconnect requested.",
                plan.conn_id, len(new_frames),
            )
        except Exception:
            logger.exception(
                "Replan conn=%s: update_subscriptions/request_reconnect "
                "raised; archiver may be in inconsistent state until next "
                "refresh tick.",
                plan.conn_id,
            )


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
      - ``COLLECTOR_TICKERS_FILE`` — optional path to JSON
        ``{"<tier>": ["TICKER", ...]}`` map. When set, the file is
        authoritative and the REST refresher is NOT started (useful
        for tests / offline dev). When unset, D1.4 REST snapshot is
        used: synchronous fetch at boot + hourly background refresh.
      - ``COLLECTOR_BATCH_SIZE`` — subscribe-frame batch size (default
        1000; tunable for Kalshi WS message-size constraints)
      - ``COLLECTOR_REST_REFRESH_SECONDS`` — REST poll cadence (default
        3600 — hourly). Ignored when COLLECTOR_TICKERS_FILE is set.
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
    refresh_seconds = float(os.environ.get(
        "COLLECTOR_REST_REFRESH_SECONDS",
        str(DEFAULT_REFRESH_INTERVAL_SECONDS),
    ))

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
    #
    # Two seams:
    #   (a) COLLECTOR_TICKERS_FILE set → file-based loader. Authoritative;
    #       REST refresher is NOT started. Useful for tests + offline dev.
    #   (b) Default (no file) → D1.4 REST snapshot. Synchronous fetch at
    #       boot so the WS connects with a populated subscription set;
    #       RestSnapshotRefresher polls hourly + force-reconnects on change.
    #
    # R1-C1 (D1.4 adv round 1): in seam (b) we eagerly load_private_key()
    # at boot rather than wrapping it in try/except. The WS handshake
    # (BronzeArchiver.__init__ → load_private_key, ws_connection.py) MUST
    # succeed on the same PEM, so a soft-fail here would just defer the
    # crash by a few lines while emitting a misleading log message. Crash
    # loud + early at the REST step so the operator sees the real cause
    # before any WS connect attempt. Matches `_required_env`'s posture for
    # KALSHI_COLLECTOR_KEY_ID + KALSHI_COLLECTOR_KEY_PATH.
    rest_private_key = None
    if tickers_file:
        tickers_by_tier = _load_tickers_by_tier(tickers_file)
    else:
        rest_private_key = load_private_key(private_key_path)
        rest_initial = fetch_tickers_by_tier(
            api_key=api_key, private_key=rest_private_key,
        )
        if rest_initial is None:
            # Fetch failed (transient 5xx exhausted retries, partial
            # pagination, malformed response). Boot with empty subs and
            # rely on the refresher to recover; do NOT crash — the PEM
            # is valid, the network is the issue.
            logger.warning(
                "Initial REST snapshot failed; booting with empty ticker "
                "set. Refresher will retry every %.0fs.",
                refresh_seconds,
            )
            tickers_by_tier = {}
        else:
            tickers_by_tier = rest_initial
            logger.info(
                "Initial REST snapshot loaded %d tickers across %d tier(s).",
                sum(len(v) for v in tickers_by_tier.values()),
                len(tickers_by_tier),
            )
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

    # Step 5b (D1.4) — REST snapshot refresher (only when no
    # COLLECTOR_TICKERS_FILE override). Closes over `archivers` so an
    # on_refresh tick can rebuild each archiver's subscribe frames +
    # force-reconnect to push the new subscription set to Kalshi.
    refresher: Optional[RestSnapshotRefresher] = None
    if not tickers_file and rest_private_key is not None:
        def _on_refresh(new_tickers_by_tier: Dict[str, List[str]]) -> None:
            _replan_for_archivers(
                new_tickers_by_tier=new_tickers_by_tier,
                archivers=archivers,
                conn_count=conn_count,
                batch_size=batch_size,
            )
        refresher = RestSnapshotRefresher(
            api_key=api_key,
            private_key=rest_private_key,
            on_refresh=_on_refresh,
            shutdown_event=shutdown_event,
            interval_seconds=refresh_seconds,
        )

    logger.info(
        "Collector booted — bronze_root=%s, conn_count=%d, archivers=%d, "
        "rest_refresh=%s",
        bronze_root, conn_count, len(archivers),
        "on" if refresher is not None else "off (file-mode)",
    )

    try:
        # Step 6 — start all archivers, then block until shutdown.
        for archiver in archivers:
            archiver.start()
        if refresher is not None:
            refresher.start()
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
