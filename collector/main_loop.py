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
that rebuilds per-conn subscribe frames + force-reconnects each WS conn —
STAGGERED in wall-clock time by ``_RECONNECT_STAGGER_SECONDS`` (20s
default) per D1.3-fu4-oom-closure 2026-05-19, ticket ``86b9zk4hz``
REUSED — so the new subscriptions take effect (Kalshi has no in-session
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
import time
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

# D1.3-fu4-oom-closure (ticket 86b9zk4hz, 2026-05-19): wall-clock seconds
# to wait between per-archiver `request_reconnect()` calls when REST
# refresh triggers a re-plan. Pre-Bit the for-loop dispatched all 7
# archivers' reconnect within <100ms — the resulting concurrent
# subscribe-ack flood (peak ~7 × 5 MB cumulative-ticker payloads parsed
# simultaneously on the asyncio threads) pushed the Python heap past the
# 512M MemoryMax cgroup cap → OOM-kill → 84 restarts over 41 hours. The
# stagger spreads conn N+1's reconnect by this many seconds after conn
# N's, giving the per-conn ack burst time to drain before the next conn
# starts. With 7 conns and the 20s default the full reconnect window is
# 6 × 20s = 120s; well under DEFAULT_REFRESH_INTERVAL_SECONDS=3600 (no
# overlap with next REST tick). See
# kb/decisions/d1-3-fu4-oom-closure-plan.md §RCA for the derivation.
#
# D1.3-fu4-boot-stagger (2026-05-19, post-PR-#110 follow-up): this
# constant is ALSO reused as the default `archiver.start()` stagger
# interval at boot via `_start_archivers_staggered`. Post-PR-#110
# deploy verification measured cgroup memory peak at 510.7 MiB /
# 512 MiB (99.7%) within ~30s of boot, confirming the boot subscribe-
# burst hits the same OOM-precursor mechanism the REST-refresh path
# does. Single source of truth: both call sites read this same
# constant by design (the boot mechanism is identical to the refresh
# mechanism; same value applies). See
# kb/decisions/d1-3-fu4-boot-stagger-plan.md §RCA for the boot-side
# mechanism trace. Pinned by
# `tests/contracts/test_collector_boot_stagger.py::test_boot_stagger_default_matches_reconnect_constant`.
_RECONNECT_STAGGER_SECONDS: float = 20.0


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


def write_bronze_health_sidecar(
    archivers: Sequence[BronzeArchiver],
    path: Path,
) -> None:
    """Write an aggregated bronze health snapshot JSON file (D1.6 fu).

    Atomic-replace via tmp file + os.replace so a reader (the cron-driven
    ``scripts/ops/collector_health_monitor.py``) never observes a torn
    JSON write. Schema documented in
    ``tests/contracts/test_bronze_health_sidecar.py`` module docstring.

    Called from the drain thread on every tick (~1s cadence). Cheap:
    each ``BronzeArchiver.get_health_snapshot`` does a handful of
    attribute reads + a ``queue.qsize()`` call + ``Thread.is_alive()``.
    """
    import datetime as _dt
    snapshots = [a.get_health_snapshot() for a in archivers]
    total_dropped = sum(int(s.get("dropped_frames", 0)) for s in snapshots)
    total_queue = sum(int(s.get("write_queue_size", 0)) for s in snapshots)
    written_at = _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ",
    )
    payload = {
        "schema_version": 1,
        "written_at": written_at,
        "archivers": snapshots,
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
    archivers: Sequence[BronzeArchiver] = (),
    health_sidecar_path: Optional[Path] = None,
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

    def _write_health_sidecar_safe() -> None:
        if health_sidecar_path is None or not archivers:
            return
        try:
            write_bronze_health_sidecar(archivers, health_sidecar_path)
        except Exception:
            # Sidecar writes are best-effort observability. A disk-full
            # or permission error here MUST NOT stall the drain loop
            # (the drain loop is the upload path that frees disk).
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


def _start_archivers_staggered(
    archivers: Sequence[BronzeArchiver],
    *,
    shutdown_event: threading.Event,
    stagger_seconds: float = _RECONNECT_STAGGER_SECONDS,
) -> int:
    """Start each archiver in sequence with a wall-clock stagger between starts.

    Boot-time counterpart to ``_replan_for_archivers``. PR #110
    (D1.3-fu4-oom-closure) staggered the REST-refresh-driven
    ``request_reconnect()`` dispatch; post-deploy verification on
    2026-05-19 11:42 UTC found that the BOOT path also drove the cgroup
    memory peak to 510.7 MiB / 512 MiB (99.7%) because all 7 archivers'
    ``start()`` ran in tight succession — each triggers a WS connect +
    subscribe-burst whose cumulative-ticker-payload acks (~5 MB per
    frame) land near-simultaneously across the 7 asyncio threads. This
    helper applies the same stagger pattern to the boot loop so peak
    in-flight ack memory across conns is bounded to ~1 × ack_size
    instead of ~N × ack_size.

    Semantics (mirror ``_replan_for_archivers``):
      - First archiver starts immediately (NO leading stagger).
      - Between iterations: ``shutdown_event.wait(timeout=stagger_seconds)``
        — cancellable so a graceful ``systemctl stop`` mid-boot exits
        in ≤ one stagger interval instead of blocking for
        ``(N-1) * stagger_seconds``.
      - Return value: count of archivers that actually received
        ``.start()`` before any cancellation. Lets the caller log a
        partial-boot warning + decide whether to proceed to
        ``refresher.start()``.

    See ``kb/decisions/d1-3-fu4-boot-stagger-plan.md`` for the full
    RCA + boot-peak mechanism trace.
    """
    n = len(archivers)
    started = 0
    for idx, archiver in enumerate(archivers):
        if idx > 0 and stagger_seconds > 0:
            if shutdown_event.wait(timeout=stagger_seconds):
                logger.info(
                    "Boot stagger: shutdown_event fired before archiver "
                    "%d of %d; partial boot.", idx, n,
                )
                break
        try:
            archiver.start()
            started += 1
        except Exception:
            logger.exception(
                "Boot stagger: archiver.start() raised for conn=%s; "
                "continuing with remaining archivers.",
                getattr(archiver, "_conn_id", "?"),
            )
    return started


def _replan_for_archivers(
    *,
    new_tickers_by_tier: Mapping[object, Sequence[str]],
    archivers: Sequence[BronzeArchiver],
    conn_count: int,
    batch_size: int,
    shutdown_event: Optional[threading.Event] = None,
    stagger_seconds: float = _RECONNECT_STAGGER_SECONDS,
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

    D1.3-fu4-oom-closure (ticket 86b9zk4hz, 2026-05-19): per-archiver
    reconnects are STAGGERED in time by ``stagger_seconds`` (default
    ``_RECONNECT_STAGGER_SECONDS``=20s). Pre-Bit code fired all 7
    archivers' ``request_reconnect`` within <100ms — the resulting
    concurrent subscribe-ack flood (~7 × 5 MB cumulative-ticker
    payloads parsed simultaneously on the asyncio threads) drove the
    Python heap past the 512M cgroup cap → OOM-kill → 84 restarts
    over 41 hours. See ``kb/decisions/d1-3-fu4-oom-closure-plan.md``
    §RCA.

    Stagger semantics:
      - Sleep BETWEEN iterations, never AFTER the last archiver
        (avoids spurious trailing wall-clock cost on every replan).
      - When ``shutdown_event`` is provided, the stagger sleep uses
        ``event.wait(timeout=stagger_seconds)`` so a graceful
        shutdown does not block on the remaining replan window
        (worst-case shutdown latency post-Bit: ``stagger_seconds``
        not ``(N-1) * stagger_seconds``).
      - When the event is not provided (tests / future callers), the
        stagger falls back to ``time.sleep`` which is uninterruptible
        but otherwise equivalent.

    Subscription-drift note: archivers N+1..end are still on OLD
    subscribe frames during the stagger window (conns 0..N have
    already reconnected with the new set). Existing data flow on the
    OLD subscription set continues uninterrupted; only NEW tickers
    added in this refresh have a ≤ ``(n_archivers - 1) * stagger_seconds``
    lag before the last conn subscribes to them. At
    ``DEFAULT_REFRESH_INTERVAL_SECONDS=3600`` and ``stagger_seconds=20``
    with 7 conns, the worst-case new-ticker subscribe lag is
    ``6 × 20s = 120s`` on top of the REST-poll cadence (NOT
    ``7 × 20s`` — the last conn has no trailing stagger).
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
    n_archivers = len(archivers)
    for idx, (plan, archiver) in enumerate(zip(plans, archivers)):
        # Honor shutdown_event BEFORE each iteration so a signal that
        # arrived during the previous stagger sleep — or before this
        # call started — short-circuits the remaining work without
        # forcing a partial reconnect on the next archiver.
        if shutdown_event is not None and shutdown_event.is_set():
            logger.info(
                "Replan: shutdown_event set; breaking after %d of %d "
                "archivers.", idx, n_archivers,
            )
            break
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
        # Stagger BETWEEN iterations only — no trailing sleep after the
        # last archiver. The cancellable event.wait lets graceful
        # shutdown break out of the stagger without waiting the full
        # interval. ``stagger_seconds <= 0`` (which the contract test
        # forbids on the module constant) would no-op the sleep — keep
        # the branch tight so a future test override of stagger_seconds=0
        # exhibits the pre-Bit behavior loudly.
        is_last = idx == n_archivers - 1
        if is_last or stagger_seconds <= 0:
            continue
        if shutdown_event is not None:
            if shutdown_event.wait(timeout=stagger_seconds):
                logger.info(
                    "Replan: shutdown_event fired during stagger after "
                    "archiver %d of %d; breaking.", idx + 1, n_archivers,
                )
                break
        else:
            time.sleep(stagger_seconds)


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
      2. Restart sweep: re-upload any leftover outbox/ chunks AND
         re-rotate bare-in_flight_<usec>.jsonl orphans across the entire
         bronze tree (D0.3 §7 last paragraph + B-orphan-sweep AMENDMENT
         2026-05-19 ticket 86ba0jmz9 — symmetric salvage across both
         chunk-pair sides via uploader.salvage_in_flight_orphans).
      3. Plan subscriptions via SubscriptionManager.assign().
      4. For each ConnPlan: build per-channel writers + subscribe-frames
         + archiver.
      5. Single drain thread fans out across all writers.
      6. Hand control to per-conn ``BronzeArchiver.start()`` via
         ``_start_archivers_staggered`` (staggered by
         ``_RECONNECT_STAGGER_SECONDS`` per D1.3-fu4-boot-stagger
         2026-05-19); main thread blocks on shutdown_event.
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
    health_sidecar_env = os.environ.get(
        "COLLECTOR_HEALTH_SIDECAR_PATH",
        # Default: alongside bronze data dir so a single mount holds
        # both data + observability state.
        str(bronze_root.parent / "bronze_health.json"),
    ).strip()
    health_sidecar_path: Optional[Path] = (
        Path(health_sidecar_env) if health_sidecar_env else None
    )

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
    # D1.9 (ticket 86ba0pmzz, 2026-05-19): kalshi_rest/markets bronze
    # writer. Only constructed in the production REST path (file-mode
    # boot has no REST fetch → no bronze write surface). Registered
    # with the drain thread below via ``all_writers`` so the rotation
    # → upload → delete cycle picks it up automatically.
    kalshi_rest_writer: Optional[BronzeWriter] = None
    if tickers_file:
        tickers_by_tier = _load_tickers_by_tier(tickers_file)
    else:
        rest_private_key = load_private_key(private_key_path)
        kalshi_rest_writer = BronzeWriter(
            root_dir=bronze_root,
            source="kalshi_rest",
            channel="markets",
            conn=None,
        )
        rest_initial = fetch_tickers_by_tier(
            api_key=api_key,
            private_key=rest_private_key,
            bronze_writer=kalshi_rest_writer,
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
    # D1.9: include the kalshi_rest/markets writer in the drain set so
    # rotation → upload → delete fires for it on the same cadence as the
    # WS writers. Order is irrelevant to drain semantics (each writer's
    # rotated_outbox_paths is drained independently).
    if kalshi_rest_writer is not None:
        all_writers.append(kalshi_rest_writer)
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

    # Step 5 — single drain thread fans out across ALL writers + writes
    # the D1.6 fu bronze_health.json sidecar each tick for the cron-
    # driven scripts/ops/collector_health_monitor.py to poll.
    drain_thread = threading.Thread(
        target=_drain_rotated,
        kwargs={
            "writers": all_writers,
            "uploader": uploader,
            "bronze_root": bronze_root,
            "shutdown_event": shutdown_event,
            "archivers": archivers,
            "health_sidecar_path": health_sidecar_path,
        },
        daemon=True,
        name="bronze-drain",
    )
    drain_thread.start()

    # Step 5b (D1.4) — REST snapshot refresher (only when no
    # COLLECTOR_TICKERS_FILE override). Closes over `archivers` so an
    # on_refresh tick can rebuild each archiver's subscribe frames +
    # force-reconnect to push the new subscription set to Kalshi.
    #
    # D1.3-fu4-oom-closure (86b9zk4hz, 2026-05-19): pass
    # ``shutdown_event`` through to `_replan_for_archivers` so the
    # ``_RECONNECT_STAGGER_SECONDS`` between-archiver stagger sleeps
    # are cancellable — a `systemctl stop kalshi-collector` mid-replan
    # exits in ≤ one stagger interval instead of pinning the refresher
    # thread for ~(N-1) * stagger seconds.
    refresher: Optional[RestSnapshotRefresher] = None
    if not tickers_file and rest_private_key is not None:
        def _on_refresh(new_tickers_by_tier: Dict[str, List[str]]) -> None:
            _replan_for_archivers(
                new_tickers_by_tier=new_tickers_by_tier,
                archivers=archivers,
                conn_count=conn_count,
                batch_size=batch_size,
                shutdown_event=shutdown_event,
            )
        refresher = RestSnapshotRefresher(
            api_key=api_key,
            private_key=rest_private_key,
            on_refresh=_on_refresh,
            shutdown_event=shutdown_event,
            interval_seconds=refresh_seconds,
            bronze_writer=kalshi_rest_writer,  # D1.9
        )

    logger.info(
        "Collector booted — bronze_root=%s, conn_count=%d, archivers=%d, "
        "rest_refresh=%s",
        bronze_root, conn_count, len(archivers),
        "on" if refresher is not None else "off (file-mode)",
    )

    try:
        # Step 6 — start all archivers (staggered per
        # D1.3-fu4-boot-stagger 2026-05-19 — see
        # kb/decisions/d1-3-fu4-boot-stagger-plan.md §RCA), then block
        # until shutdown. Pre-Bit code fired all archivers' .start()
        # within <100ms, driving cgroup memory peak to 99.7% of the
        # 512M cap during the concurrent boot subscribe-ack flood.
        # The stagger spreads conn N+1's WS connect + subscribe
        # dispatch by `_RECONNECT_STAGGER_SECONDS` after conn N's,
        # bounding peak ack memory to ~1 × ack_size instead of
        # ~N × ack_size.
        n_started = _start_archivers_staggered(
            archivers, shutdown_event=shutdown_event,
        )
        if n_started < len(archivers):
            # R1-m1: helper decrements `started` ONLY when
            # shutdown_event.wait() fires OR archiver.start() raises.
            # Differentiate the two by checking the event so the
            # WARNING text accurately reflects which path triggered
            # the partial boot.
            if shutdown_event.is_set():
                reason = "shutdown fired mid-boot"
            else:
                reason = "archiver.start() raised on at least one conn"
            logger.warning(
                "Boot: only %d of %d archivers started (%s); proceeding "
                "to refresher.start() + shutdown.",
                n_started, len(archivers), reason,
            )
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
