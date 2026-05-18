"""Coinbase Exchange WS consumer — D2.2 (ticket 86b9zkppk, 2026-05-17).

Sub-Bit of the ``86b9zkkv4`` D2.x Coinbase WS bronzing umbrella; lands
on top of D2.1.5 (PR #79, ticket ``86b9zkpny``, ``coinbase_wire`` body).

Thin consumer of ``coinbase_wire.ws_client.WSClient`` that pipes raw
Coinbase Exchange WS frames into per-channel bronze JSONL writers
(``collector/writer.py``). Mirror of ``collector/ws_connection.py::
BronzeArchiver`` for the Coinbase side, adapted for Exchange WS:

  - **Single-conn.** Coinbase Exchange WS does not shard the way Kalshi
    does; one connection covers all subscribed product_ids × channels.
    Default ``conn_id="A"`` reserves the sharding seam for a future
    Bit if subscribe-fanout ever justifies it.
  - **No sids / no cmd_id binding.** Each Coinbase frame carries a
    top-level ``type`` field that the consumer maps directly to a
    channel name. The kalshi-side sid→channel binding machinery has
    no analog here.
  - **source="coinbase_ws"** on every envelope.
  - **on_session_start** builds the Coinbase Exchange WS subscribe
    payload via the public ``coinbase_wire.auth.build_public_subscribe_message``
    helper and dispatches through the public ``WSClient.send_frame``
    API. Decoupled from the wire library's ``_default_on_session_start``
    (R1-M1 retract — private-method coupling is a silent-partial-
    failure class: a wire-side rename would import cleanly + the
    callback would fire + the call would AttributeError inside the
    wire's outer try/except swallower, leaving no subscribe dispatched
    + no bronze flow + only the 90s silence-watchdog as the alert).

Lessons applied FROM DAY ONE (carried from the Kalshi-bronze ship arc):

  - **D1.3-fu4 worker-thread decouple** (ticket 86b9zk4hz, 2026-05-17).
    ``_on_frame`` runs on the wire library's asyncio thread and MUST
    return quickly to avoid blocking the keepalive ping-pong cycle.
    Heavy work (envelope build + per-channel writer's zstd-compress +
    disk IO) is decoupled onto a dedicated daemon worker thread that
    drains a bounded ``queue.Queue``. On queue full → drop counter +
    throttled log; never block, never raise. Closes the 1011
    keepalive-ping-timeout storm class before it can open.

  - **D1.3-fu5 skip-ack-enqueue** (ticket 86b9zky3u, 2026-05-17).
    Subscribe-ack frames (Coinbase Exchange WS sends ``type=subscriptions``
    on subscribe, ``type=error`` on subscribe-failure) are protocol
    metadata — no silver/gold pipeline consumes them. They are NOT
    enqueued for bronze writing. ``_ack_frames_processed`` observability
    counter tracks ack activity so a monitor can distinguish "healthy +
    receiving acks" from "wedged + no activity at all".

NO ``bot.*`` imports (pinned by ``collector-no-bot`` import-linter
contract). The Coinbase wire transport reach lives in ``coinbase_wire/``
only (mirror of the kalshi_wire/ → collector pattern).

Architecture (D2.2 SHIPPED):

    WSClient (coinbase_wire) ──► on_session_start() ──► _on_session_start
                                                        builds + sends one
                                                        batched subscribe
                                                        (build_public_subscribe_message
                                                         + WSClient.send_frame
                                                         — public API only,
                                                         per R1-M1)
                            └──► on_frame(Frame)   ──► _on_frame
                                                          │
                              ┌───────────────────────────┤
                              │                           │
                  (subscriptions / error ack)      (data frame)
                              │                           │
                  bump ack counter, RETURN          msg_type → channel
                  (skip-enqueue, skip seq)          via dispatch table
                                                          │
                                            allocate _collector_seq
                                            (lock-held, monotonic)
                                                          │
                                            enqueue (frame, channel, seq)
                                            ──► _write_queue (bounded)
                                                          │
                                                  worker thread drains
                                                  → build_envelope
                                                  → writers_by_channel[ch]
                                                    .write(envelope)
                            └──► on_session_end()  ──► (no per-session
                                                         state to clear;
                                                         placeholder for
                                                         R3/P0-A symmetry)
"""
from __future__ import annotations

import logging
import queue
import signal
import threading
from datetime import datetime, timezone
from typing import Dict, Mapping, Optional, Tuple

from coinbase_wire.auth import build_public_subscribe_message
from coinbase_wire.ws_client import (
    DEFAULT_CHANNELS,
    DEFAULT_PRODUCT_IDS,
    DEFAULT_WS_URL,
    Frame,
    WSClient,
    build_envelope,
)


# Coinbase Exchange WS ack-class message types. Both bind no state on
# our side (Coinbase has no sids; no cmd_id → channel correlation is
# needed — the dispatch table is static msg_type → channel) but both
# are protocol metadata that does NOT belong in the bronze write path.
#
#   - ``subscriptions`` — confirmation of currently-subscribed channels
#     (sent after every (re)connect, and on any subscribe/unsubscribe).
#     Payload includes the cumulative {channel: [product_ids]} map.
#   - ``error`` — subscribe-failure or other Coinbase-side error. We
#     log + skip; bronze should not capture handshake noise.
_SUBSCRIBE_ACK_TYPES = frozenset({"subscriptions", "error"})

# Static msg_type → channel dispatch. Coinbase Exchange WS carries the
# channel identity via the top-level ``type`` field on every frame;
# the consumer dispatches without a sid binding step (no analog to
# Kalshi's sid→channel map exists here).
#
# Naming quirk: Coinbase Exchange WS sends ``match`` (singular) frames
# on the ``matches`` (plural) channel. The dispatch table reflects this.
#
# ``level2_batch`` PROMOTED at D2.5 (ticket 86b9znq4w, 2026-05-18):
# the R0 reachability spike at D2.5 kickoff confirmed public access on
# the Exchange WS endpoint (1 snapshot + 502 l2update frames over 30s
# for BTC-USD alone, no ``type=error``). Both ``snapshot`` (the initial
# orderbook image dispatched on subscribe) and ``l2update`` (the
# streaming batched bid/ask updates) route to the ``level2_batch``
# channel. Bundled into D2.5 alongside the wire-library default-set
# extension + the sister-doc retract.
#
# Frames with unmapped msg_type (e.g., a future Coinbase channel
# Coinbase ships without a corresponding repo update) route to the
# ``None``-keyed ``_unrouted`` writer so bronze captures the bytes for
# silver QA to investigate.
DEFAULT_MSG_TYPE_TO_CHANNEL: Mapping[str, str] = {
    "ticker": "ticker",
    "match": "matches",
    "heartbeat": "heartbeat",
    "status": "status",
    "snapshot": "level2_batch",
    "l2update": "level2_batch",
}

# D1.3-fu4 default write-queue capacity. Per the R0 reachability spike
# at D2.5 kickoff (BTC-USD, 30s, level2_batch only): 502 l2update
# frames → ~17 frames/sec per product. Extrapolated to 7 products × 5
# channels: level2_batch ~120 frames/sec dominates the post-D2.5 rate;
# matches + ticker contribute ~50-80 frames/sec aggregate (more in
# volatile windows); heartbeat + status are <5 frames/sec combined.
# Steady-state total ≈ 200-300 frames/sec; the 10K queue gives
# ~30-50s buffering at that rate. Tuned the same as Kalshi
# for cross-collector consistency.
_DEFAULT_WRITE_QUEUE_MAXSIZE = 10_000

# Sentinel posted to ``_write_queue`` by ``stop()`` to signal the worker
# to drain remaining items + exit. Unique object identity (``is``-check)
# so a malicious/stray tuple cannot trip an early exit.
_SHUTDOWN_SENTINEL = object()

# Throttle WARNING-level log lines for queue-full drops. Same policy as
# the Kalshi side — logs at counter == 1, then every _DROP_LOG_THROTTLE
# drops thereafter. Between log lines there are _DROP_LOG_THROTTLE-1
# silent drops; readers should rely on _dropped_frames for the total.
_DROP_LOG_THROTTLE = 1000

# Bound on stop()'s join wait for the worker. Long enough to drain a
# full queue at slow-disk speed; short enough that a wedged worker
# cannot block process shutdown indefinitely.
_WORKER_JOIN_TIMEOUT_S = 30.0


class CoinbaseArchiver:
    """Collector-side WSClient consumer for Coinbase Exchange WS.

    Per-process instance (Coinbase single-conn). The owner
    (post-D2.5 ``collector/coinbase_main_loop.py`` or similar) constructs
    one archiver with a ``writers_by_channel`` dict (per-channel
    BronzeWriter instances scoped to ``conn_id``) and starts it.

    No ``bot.*`` imports — collector-no-bot contract.
    """

    def __init__(
        self,
        *,
        writers_by_channel: Mapping[Optional[str], object],
        conn_id: str = "A",
        url: Optional[str] = None,
        channels: Optional[Tuple[str, ...]] = None,
        product_ids: Optional[Tuple[str, ...]] = None,
        msg_type_to_channel: Mapping[str, str] = DEFAULT_MSG_TYPE_TO_CHANNEL,
        write_queue_maxsize: int = _DEFAULT_WRITE_QUEUE_MAXSIZE,
    ):
        """Construct one CoinbaseArchiver.

        Args:
            writers_by_channel: dict keyed by channel name (or ``None``
                for the ``_unrouted`` fallback partition) → BronzeWriter
                instance. Each writer must already be constructed for
                its (source="coinbase_ws", channel, conn) tuple — the
                archiver does NOT allocate writers, it only dispatches.
            conn_id: identifier embedded in the bronze partition path
                (``conn=<conn_id>``) and in envelope ``_conn``. Must
                match the conn id on every BronzeWriter in
                writers_by_channel. Default ``"A"`` for single-conn.
            url: optional WS URL override (defaults to
                ``coinbase_wire.ws_client.DEFAULT_WS_URL`` =
                ``wss://ws-feed.exchange.coinbase.com``). Tests point
                at a localhost mock.
            channels: optional override for the WSClient's
                ``DEFAULT_CHANNELS``. When None, the wire library's
                post-D2.5 5-channel default (ticker + matches +
                heartbeat + status + level2_batch) is used; D2.1.5
                originally shipped with the 4-channel subset and D2.5
                promoted level2_batch.
            product_ids: optional override for the WSClient's
                ``DEFAULT_PRODUCT_IDS``. When None, the wire library's
                default (BTC/ETH/SOL/XRP/HYPE/DOGE/BNB) is used.
            msg_type_to_channel: dispatch table. Default covers the
                post-D2.5 6 msg-type entries dispatched across 5
                channels (ticker → ticker; match → matches; heartbeat
                → heartbeat; status → status; snapshot → level2_batch;
                l2update → level2_batch). Override if extending coverage.
            write_queue_maxsize: bound on the queue between
                ``_on_frame`` (asyncio thread) and the bronze writer
                worker thread. D1.3-fu4 default 10_000 ≈ ~10s
                buffering at typical load. Set to ``0`` is NOT
                supported — Python's ``queue.Queue(maxsize=0)`` means
                UNBOUNDED, which defeats the bounded-backpressure
                invariant.
        """
        if write_queue_maxsize <= 0:
            raise ValueError(
                f"write_queue_maxsize must be a positive int (got "
                f"{write_queue_maxsize!r}); queue.Queue(maxsize<=0) is "
                f"unbounded and breaks the D1.3-fu4 backpressure invariant."
            )
        self._writers_by_channel: Dict[Optional[str], object] = dict(
            writers_by_channel)
        self._msg_type_to_channel: Dict[str, str] = dict(msg_type_to_channel)
        self._conn_id = conn_id
        # Store the resolved channel + product set on self so
        # ``_on_session_start`` can build the subscribe payload directly
        # (R1-M1 retract — the prior implementation reached into
        # ``self._wire._default_on_session_start()`` which couples this
        # consumer to a wire-library private method. Building the payload
        # here decouples us from that surface and uses only the public
        # ``coinbase_wire.auth.build_public_subscribe_message`` +
        # ``WSClient.send_frame`` API.)
        self._channels: Tuple[str, ...] = (
            tuple(channels) if channels is not None else DEFAULT_CHANNELS
        )
        self._product_ids: Tuple[str, ...] = (
            tuple(product_ids) if product_ids is not None
            else DEFAULT_PRODUCT_IDS
        )
        self._lock = threading.Lock()
        self._collector_seq = 0

        # D1.3-fu4 worker-thread plumbing.
        self._write_queue: queue.Queue = queue.Queue(
            maxsize=write_queue_maxsize)
        self._write_worker: Optional[threading.Thread] = None
        self._dropped_frames: int = 0
        self._drop_log_counter: int = 0

        # D1.3-fu5 observability counter — increments per subscribe-ack
        # processed. Ack frames bind no state on the Coinbase side; the
        # counter exists so the monitor can distinguish "healthy +
        # receiving acks" from "wedged + no activity at all".
        self._ack_frames_processed: int = 0

        # Idempotency guard for stop().
        self._stop_called: bool = False

        # Direct kwargs (not **dict-spread) so AST contract tests can
        # statically verify on_frame + on_session_start + on_session_end
        # are wired without needing a dataflow analyzer (defense-in-depth
        # against the D1.2 R1-C3 class: callback wired in test fixture
        # but not in production code). Nullable overrides resolve to the
        # wire library's published defaults at construction time. The
        # ``channels`` + ``product_ids`` we pass here are the same values
        # cached on ``self._channels`` / ``self._product_ids`` above so
        # the wire's session-start defaults stay aligned with what THIS
        # consumer's ``_on_session_start`` would build.
        self._wire = WSClient(
            on_frame=self._on_frame,
            on_session_start=self._on_session_start,
            on_session_end=self._on_session_end,
            url=url if url is not None else DEFAULT_WS_URL,
            channels=self._channels,
            product_ids=self._product_ids,
        )

    # ── Health snapshot (cross-collector observability) ─────────────────

    def get_health_snapshot(self) -> Dict[str, object]:
        """Return a JSON-serializable per-archiver health dict.

        Schema mirrors ``BronzeArchiver.get_health_snapshot`` so the
        same downstream monitor (``scripts/ops/collector_health_monitor.py``)
        can consume it if a future Bit aggregates Coinbase + Kalshi
        archivers into one sidecar.

        Keys:
          - conn_id: WS conn identifier (default "A" — single-conn).
          - dropped_frames: cumulative count of queue.Full drops since
            worker (re)spawn.
          - write_queue_size: instantaneous Queue.qsize() — approximate
            under concurrent producer/consumer.
          - write_queue_maxsize: capacity (for saturation %).
          - write_worker_alive: True iff the worker thread is currently
            alive.
          - collector_seq: monotonic per-frame seq high-water mark.
          - ack_frames_processed: D1.3-fu5 observability — cumulative
            ack-class frames seen (subscriptions / error). Frames NOT
            enqueued for bronze writing.
        """
        worker = self._write_worker
        return {
            "conn_id": self._conn_id,
            "dropped_frames": self._dropped_frames,
            "write_queue_size": self._write_queue.qsize(),
            "write_queue_maxsize": self._write_queue.maxsize,
            "write_worker_alive": bool(worker is not None and worker.is_alive()),
            "collector_seq": self._collector_seq,
            "ack_frames_processed": self._ack_frames_processed,
        }

    # ── Session callbacks ───────────────────────────────────────────────

    def _on_session_start(self) -> None:
        """WSClient callback fired AFTER WS connect, BEFORE frame reads.

        Builds the Coinbase Exchange WS subscribe payload via the
        public ``coinbase_wire.auth.build_public_subscribe_message``
        helper and dispatches it through the wire's public
        ``send_frame`` API. Coinbase Exchange WS lets a single
        subscribe message cover all channels × product_ids, so one
        ``send_frame`` per session-start is sufficient.

        R1-M1 RCA: an earlier draft delegated to
        ``self._wire._default_on_session_start()`` — a private wire-
        library method. That coupling meant a wire-side rename of the
        private method would silently break the archiver: the AST
        contract test passes (callback IS wired), construction +
        import + start() all succeed, but at first WS connect
        ``AttributeError`` fires inside the wire's
        ``try: cb() except Exception: log.warning(...)`` swallower —
        no subscribe dispatches, no bronze flows, silence-watchdog
        only kicks in 90s later. Silent-partial-failure class. Fixed
        here by constructing the payload directly using the public
        helper + ``send_frame``.

        Race on close: ``send_frame`` raises ``ConnectionError`` if
        the WS dropped between the session-start callback fire and
        the send. Caught + logged; the wire layer's reconnect loop
        retries the session-start sequence next iteration.
        """
        if not self._channels or not self._product_ids:
            # Empty subscribe set — caller deliberately bypassed the
            # default channel/product. WS connects but no data flows.
            return
        payload = build_public_subscribe_message(
            channels=list(self._channels),
            product_ids=list(self._product_ids),
        )
        try:
            self._wire.send_frame(payload)
        except ConnectionError:
            logging.warning(
                "CoinbaseArchiver subscribe dispatch aborted (conn=%s) — "
                "WS closed between session_start fire and send_frame; "
                "reconnect loop will retry.",
                self._conn_id,
            )
        except Exception:
            # Defensive: any other send-side failure is logged so a
            # malformed subscribe payload surfaces in journals rather
            # than getting swallowed by the wire's outer try/except.
            logging.warning(
                "CoinbaseArchiver subscribe dispatch FAILED on "
                "session_start (conn=%s); continuing (wire will "
                "retry on next session).",
                self._conn_id, exc_info=True,
            )

    def _on_session_end(self) -> None:
        """WSClient callback fired AFTER WS close, BEFORE the backoff
        sleep (R3/P0-A invariant inherited from coinbase_wire.WSClient).

        Coinbase has no per-session sid binding, so there is no
        sid→channel map to clear (unlike the Kalshi side). The callback
        is wired for symmetry with the kalshi_wire.WSClient contract
        and as a future-extension seam — if a later Bit ever adds
        per-session consumer state (e.g., per-product sequence-gap
        detection cache), this is where it gets reset.
        """
        # No per-session state to clear on the Coinbase side at D2.2.
        # The pass keeps the callback method present + AST-discoverable.
        return

    # ── Per-frame dispatch ──────────────────────────────────────────────

    def _on_frame(self, frame: Frame) -> None:
        """WSClient callback (asyncio thread) — route the frame.

        D1.3-fu4 split: this method runs on the coinbase_wire asyncio
        thread + MUST return quickly. Heavy work (``build_envelope`` +
        the per-channel writer's zstd-compress + disk IO) is decoupled
        onto a dedicated worker thread that drains ``self._write_queue``.

        D1.3-fu5: subscribe-ack frames (``type=subscriptions`` /
        ``type=error``) are protocol metadata, NOT enqueued for bronze
        writing. The Coinbase ack payload is much smaller than the
        Kalshi cumulative-ticker case that motivated fu5, but the same
        architectural pattern applies for consistency + future-proofing.

        Steps:
          1. (acks only) Bump observability counter, RETURN. Acks do
             NOT consume a ``_collector_seq`` (symmetry with skip-enqueue
             — otherwise the partition seq would have unexplained gaps).
          2. (data frames) Resolve channel via static
             ``msg_type → channel`` dispatch table. Unknown / missing
             msg_type → ``None`` (routes to _unrouted writer; bronze
             still captures the bytes for silver QA to investigate).
          3. (data frames) Allocate ``collector_seq`` under lock so the
             worker can dispatch in FIFO + monotonic order.
          4. (data frames) ``put_nowait`` ``(frame, channel, seq)`` to
             the bounded ``_write_queue``. On queue.Full: drop + bump
             counter + throttled WARNING. NEVER block — the whole
             point is to keep this callback fast so the asyncio loop
             services its ping-pong cycle.
        """
        # Step 1: subscribe-ack — observability bump then RETURN.
        if frame.msg_type in _SUBSCRIBE_ACK_TYPES:
            with self._lock:
                self._ack_frames_processed += 1
            return

        # Step 2: resolve channel for this frame's envelope. Static
        # dispatch — no sid binding, no lock needed (dispatch table is
        # frozen at construction).
        channel: Optional[str] = None
        if frame.msg_type is not None:
            channel = self._msg_type_to_channel.get(frame.msg_type)
        # channel == None on unknown / missing msg_type → routes to the
        # _unrouted writer (writers_by_channel[None]).

        # Step 3: allocate seq under lock so the worker can dispatch in
        # FIFO + monotonic order.
        with self._lock:
            self._collector_seq += 1
            seq = self._collector_seq

        # Step 4: enqueue (frame, channel, seq) for the worker. MUST
        # use put_nowait — a blocking put on a stalled queue would
        # re-introduce the asyncio loop stall the D1.3-fu4 lesson closes.
        try:
            self._write_queue.put_nowait((frame, channel, seq))
        except queue.Full:
            with self._lock:
                self._dropped_frames += 1
                drop_total = self._dropped_frames
                self._drop_log_counter += 1
                should_log = (
                    self._drop_log_counter == 1
                    or self._drop_log_counter % _DROP_LOG_THROTTLE == 0
                )
            if should_log:
                logging.warning(
                    "CoinbaseArchiver write_queue full (conn=%s seq=%d) — "
                    "dropping frame; total dropped=%d. Worker may be "
                    "stalled or load exceeds queue capacity (%d).",
                    self._conn_id, seq, drop_total,
                    self._write_queue.maxsize,
                )

    def _drain_loop(self) -> None:
        """Worker thread: drain ``_write_queue`` and dispatch envelopes.

        Owned by ``self._write_worker``. Runs until ``stop()`` posts
        ``_SHUTDOWN_SENTINEL``. Per-item exceptions are caught + logged
        + skipped so one bad writer (disk full, malformed envelope)
        cannot kill the worker — silent total loss of bronze would be
        much worse than per-frame drops.
        """
        while True:
            item = self._write_queue.get()
            try:
                if item is _SHUTDOWN_SENTINEL:
                    return
                frame, channel, seq = item
                try:
                    # D0.3 §2 invariant: _wire_recv_ts captured at frame
                    # ingress (by coinbase_wire.WSClient BEFORE
                    # json.loads). Forwarding it here keeps bronze
                    # fidelity intact.
                    wire_recv_ts = datetime.fromtimestamp(
                        frame.wire_recv_ts, tz=timezone.utc,
                    )
                    envelope = build_envelope(
                        raw=frame.raw,
                        source="coinbase_ws",
                        channel=channel,
                        conn=self._conn_id,
                        collector_seq=seq,
                        wire_recv_ts=wire_recv_ts,
                    )
                    writer = self._writers_by_channel.get(channel)
                    if writer is None:
                        # Channel resolved but no writer — fall back to
                        # _unrouted so the frame survives. Log so silver
                        # QA can detect the misconfig.
                        writer = self._writers_by_channel.get(None)
                        logging.warning(
                            "CoinbaseArchiver no writer for channel=%r "
                            "(conn=%s seq=%d); falling back to _unrouted.",
                            channel, self._conn_id, seq,
                        )
                        if writer is None:
                            logging.warning(
                                "CoinbaseArchiver no _unrouted writer "
                                "either (conn=%s seq=%d) — frame dropped.",
                                self._conn_id, seq,
                            )
                            continue
                        envelope["_channel"] = None
                    writer.write(envelope)
                except Exception:
                    logging.warning(
                        "CoinbaseArchiver writer failed for frame seq=%d "
                        "(conn=%s)",
                        seq, self._conn_id, exc_info=True,
                    )
            finally:
                self._write_queue.task_done()

    # ── Lifecycle ───────────────────────────────────────────────────────

    def run(self, shutdown_event: Optional[threading.Event] = None) -> None:
        """Start the WS client and block until shutdown is signaled.

        ``shutdown_event``: if provided, ``run`` blocks on
        ``event.wait()``; caller drives shutdown. If None, installs
        SIGINT/SIGTERM handlers on the (assumed-main) thread and waits
        on an internal Event. Tests pass a controlled event to avoid
        signal-handler pollution.
        """
        owned_event = shutdown_event is None
        if owned_event:
            shutdown_event = threading.Event()
            try:
                signal.signal(signal.SIGINT, lambda *_: shutdown_event.set())
                signal.signal(signal.SIGTERM, lambda *_: shutdown_event.set())
            except ValueError:
                logging.warning(
                    "CoinbaseArchiver.run(): could not install SIGINT/SIGTERM "
                    "handlers (non-main thread). Caller must drive shutdown "
                    "via the event."
                )

        self.start()
        try:
            shutdown_event.wait()
        finally:
            self.stop()

    def start(self) -> None:
        """Start the worker thread, then the underlying WSClient.

        Order matters: the worker MUST be draining BEFORE the wire
        starts emitting frames, or the first burst would drop (small
        maxsize) or accumulate against a non-existent consumer.

        Re-entrancy: if start() is called after a prior stop(), a
        FRESH worker is spawned + counters reset so health monitors
        see per-session deltas from a known floor.
        """
        if self._write_worker is None or not self._write_worker.is_alive():
            with self._lock:
                self._dropped_frames = 0
                self._drop_log_counter = 0
                self._stop_called = False
            self._write_worker = threading.Thread(
                target=self._drain_loop,
                name=(
                    f"CoinbaseArchiver-writer-{self._conn_id}-{id(self):x}"
                ),
                daemon=True,
            )
            self._write_worker.start()
        self._wire.start()

    def stop(self) -> None:
        """Stop the underlying WSClient, then drain + join the worker.

        Order matters: stop the wire FIRST + JOIN its asyncio thread so
        no new frames enqueue after the sentinel is posted. Without
        the join, ``WSClient.stop()`` is fire-and-forget and tail
        frames could land behind the sentinel.

        Then post the shutdown sentinel — the worker drains any frames
        buffered ahead of the sentinel before exiting (FIFO queue
        guarantees this once the producer is quiesced). Finally join
        the worker with a bounded timeout: a wedged writer must not
        block process exit indefinitely.

        Idempotency: a second ``stop()`` call early-returns. The
        guard is defensive against a future graceful-shutdown coord-
        inator that might explicitly double-call ``stop()``.
        """
        with self._lock:
            if self._stop_called:
                logging.debug(
                    "CoinbaseArchiver.stop (conn=%s) called again — no-op "
                    "(idempotency guard).", self._conn_id,
                )
                return
            self._stop_called = True
        self._wire.stop(join_timeout=_WORKER_JOIN_TIMEOUT_S)
        worker = self._write_worker
        if worker is not None and worker.is_alive():
            self._write_queue.put(_SHUTDOWN_SENTINEL)
            worker.join(timeout=_WORKER_JOIN_TIMEOUT_S)
            if worker.is_alive():
                logging.warning(
                    "CoinbaseArchiver._write_worker (conn=%s) did not "
                    "exit within %.0fs of shutdown signal — abandoning "
                    "(daemon thread will be killed at process exit). "
                    "Pending queue size at abandon: %d.",
                    self._conn_id, _WORKER_JOIN_TIMEOUT_S,
                    self._write_queue.qsize(),
                )
