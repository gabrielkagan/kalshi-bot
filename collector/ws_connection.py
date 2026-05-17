"""Kalshi WS consumer — D1.1.5 + D1.2 + D1.3 + D1.3-fu3 + D1.3-fu4
(tickets 86b9zdhz2 + 86b9ypn66 + 86b9ypn72 + 86b9zjyr0 + 86b9zk4hz,
2026-05-16 / 2026-05-17).

Thin consumer of ``kalshi_wire.ws_client.WSClient`` that pipes raw Kalshi
WS frames into per-channel bronze JSONL writers (``collector/writer.py``).
Per the 2026-05-16 AMENDMENT to ``kb/decisions/data-corpus-architecture.md``
§5, ``kalshi_wire/`` is the shared transport that both this module AND
``bot/feeds/kalshi.py`` consume — the "two sides of the same coin"
symmetry that makes bronze byte-equivalent to what the bot itself saw
on the wire.

Bit ordering:
  - **D1.1.5** shipped the WIRE-UP skeleton (auth + WSClient consumer +
    on_frame plumbing).
  - **D1.2** (`86b9ypn66`) shipped ``BronzeArchiver.run()`` body
    (blocking run loop with signal-handler-installed shutdown) and the
    ``_wire_recv_ts`` capture-at-ingress invariant (D0.3 §2) by passing
    ``frame.wire_recv_ts`` into ``build_envelope``.
  - **D1.3** (`86b9ypn72`) wires the ``on_session_start`` callback that
    dispatches subscribe frames assembled by
    ``collector/subscription_manager.py``, binds sid→channel from
    subscribe-acks (``type=subscribed``/``type=ok``), and routes data
    frames to per-channel ``BronzeWriter`` instances via the
    ``writers_by_channel`` constructor arg. **First-bronze-flow happens
    here** — the R1-C3 acceptance criterion deferred from D1.2 lands at
    D1.3 ship.
  - **D1.3-fu3** (`86b9zjyr0`) raised ``ping_timeout`` from the
    kalshi_wire default (10s) to 30s. Stopgap that proved insufficient
    under steady-state load (82 × 1011 errors in 32 min observed
    2026-05-17). Pin retained as defense-in-depth.
  - **D1.3-fu4** (`86b9zk4hz`) decouples ``_on_frame``'s heavy work
    (``build_envelope`` + ``writer.write``) onto a single daemon worker
    thread that drains a bounded ``queue.Queue``. The asyncio thread
    now only handles sid binding (must stay synchronous for race-free
    routing of immediately-subsequent data frames) + lock-held seq
    allocation + non-blocking enqueue. On queue full → drop counter +
    throttled log; never block, never raise. Closes the 1011
    keepalive-ping-timeout storm class that D1.3-fu3 only mitigated.

NO ``bot.*`` imports (pinned by ``collector-no-bot`` import-linter
contract). Auth + WS transport reach into ``kalshi_wire/`` only.
"""
from __future__ import annotations

import logging
import queue
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Union

from kalshi_wire.auth import load_private_key  # noqa: F401 — re-exported for callers
from kalshi_wire.ws_client import Frame, WSClient, build_envelope


# Kalshi subscribe-ack message types — both carry sid + cmd_id and bind
# to the channel that issued the originating cmd_id.
_SUBSCRIBE_ACK_TYPES = frozenset({"subscribed", "ok"})

# D1.3-fu4 default write-queue capacity. ~10s of buffering at typical
# steady-state per-conn load (~100-1000 frames/sec depending on subscribe
# breadth). Tuned high enough to absorb subscribe storms + backlog drain
# post-reconnect without dropping; low enough that an OOM-tier blowup
# cannot accumulate (10000 envelopes × few KB ≈ tens of MB worst case).
_DEFAULT_WRITE_QUEUE_MAXSIZE = 10_000

# Sentinel posted to ``_write_queue`` by ``stop()`` to signal the worker
# to drain remaining items + exit. Unique object identity (``is``-check)
# so a malicious/stray tuple cannot trip an early exit.
_SHUTDOWN_SENTINEL = object()

# Throttle WARNING-level log lines for queue-full drops so a sustained
# overload cannot itself become a logging-IO source of stall. Logs fire
# at counter == 1, 1000, 2000, 3000, ... (the first drop, then every
# _DROP_LOG_THROTTLE-th drop on a multiple-of-N cadence — NOT "every Nth
# after the first" which would be 1, 1001, 2001, ...). Between log lines
# there are _DROP_LOG_THROTTLE-1 silent drops; readers should rely on
# the ``_dropped_frames`` counter for the true total.
_DROP_LOG_THROTTLE = 1000

# Bound on stop()'s join wait for the worker. Long enough to drain a
# full queue at slow-disk speed; short enough that a wedged worker
# cannot block process shutdown indefinitely.
_WORKER_JOIN_TIMEOUT_S = 30.0


class BronzeArchiver:
    """Collector-side WSClient consumer — pipes Frame.raw to per-channel writers.

    Architecture (post-D1.3):

        WSClient (kalshi_wire) ──► on_session_start() ──► dispatch
                                                          subscribe_frames
                                                          via send_frame
                              └──► on_frame(Frame)   ──► _on_frame
                                                            │
                                ┌───────────────────────────┤
                                │                           │
                       (subscribed/ok ack)           (data frame)
                                │                           │
                       bind sid→channel via            resolve channel
                       cmd_id_to_channel lookup        via sid→channel map
                                │                           │
                                └──── build_envelope ──────►│
                                                            ▼
                                           writers_by_channel[channel].write(env)
                              └──► on_session_end()  ──► clear sid→channel map

    Per-conn instance. The owner (collector/main_loop.py) constructs one
    archiver per planned WS connection, each with its own
    ``writers_by_channel`` dict (per-channel BronzeWriter instances scoped
    to ``conn_id``) and its own pre-built ``subscribe_frames`` from the
    SubscriptionManager planner.

    No ``bot.*`` imports — collector-no-bot contract.
    """

    def __init__(
        self,
        api_key: str,
        private_key_path: Union[str, Path],
        *,
        writers_by_channel: Mapping[Optional[str], object],
        subscribe_frames: Sequence[Dict] = (),
        cmd_id_to_channel: Mapping[int, str] = (),
        conn_id: str = "A",
        url: Optional[str] = None,
        write_queue_maxsize: int = _DEFAULT_WRITE_QUEUE_MAXSIZE,
    ):
        """Construct one per-conn BronzeArchiver.

        Args:
            api_key: Kalshi API key id.
            private_key_path: filesystem path to the RSA-PSS PEM. Loaded
                eagerly via ``kalshi_wire.auth.load_private_key``; a
                missing/invalid PEM raises BEFORE any WS connect.
            writers_by_channel: dict keyed by channel name (or ``None``
                for the ``_unrouted`` fallback partition) → BronzeWriter
                instance. Each writer must already be constructed for
                its (source, channel, conn) tuple — the archiver does NOT
                allocate writers, it only dispatches.
            subscribe_frames: sequence of pre-built subscribe payloads
                (from ``SubscriptionManager.build_subscribe_frames``). May
                be empty (boot raced the first D1.4 REST refresh tick,
                OR Kalshi legitimately had zero open markets at boot) —
                the WS still connects, just receives no data frames
                until the refresher populates the set + force-reconnects.
            cmd_id_to_channel: mapping populated by
                ``build_subscribe_frames`` so subscribe-acks
                (``type=subscribed``/``type=ok``) bind ``sid`` → channel.
            conn_id: identifier embedded in the bronze partition path
                (``conn=<conn_id>``) and in envelope ``_conn``. Must
                match the conn_id on every BronzeWriter in
                writers_by_channel.
            url: optional WS URL override (defaults to kalshi_wire's
                production URL). Tests point at a localhost mock.
            write_queue_maxsize: bound on the queue between
                ``_on_frame`` (asyncio thread) and the bronze writer
                worker thread. D1.3-fu4 default 10_000 ≈ ~10s buffering
                at typical load. Smaller values trade latency-stability
                for memory headroom; larger values trade memory for
                burst-absorption. Set to ``0`` is NOT supported — Python's
                ``queue.Queue(maxsize=0)`` means UNBOUNDED, which defeats
                the bounded-backpressure invariant of this Bit.
        """
        if write_queue_maxsize <= 0:
            # ``queue.Queue(maxsize=0)`` would be UNBOUNDED in stdlib
            # semantics — silent OOM class under sustained overload.
            # Reject loudly at construction.
            raise ValueError(
                f"write_queue_maxsize must be a positive int (got "
                f"{write_queue_maxsize!r}); queue.Queue(maxsize<=0) is "
                f"unbounded and breaks the D1.3-fu4 backpressure invariant."
            )
        self._api_key = api_key
        self._private_key = load_private_key(private_key_path)
        # Defensive copies — caller mutating their dict mid-session would
        # be a subtle bug class; freeze the mapping at construction.
        self._writers_by_channel: Dict[Optional[str], object] = dict(
            writers_by_channel)
        self._subscribe_frames: Sequence[Dict] = tuple(subscribe_frames)
        self._cmd_id_to_channel: Dict[int, str] = dict(cmd_id_to_channel)
        self._conn_id = conn_id
        self._lock = threading.Lock()
        self._collector_seq = 0
        # Session-scoped sid → channel binding. Cleared in
        # ``_on_session_end`` (matches kalshi_wire R3/P0-A invariant
        # since Kalshi reassigns sids per session).
        self._sid_to_channel: Dict[int, str] = {}

        # D1.3-fu4 worker-thread plumbing. The queue carries
        # ``(frame, channel, seq)`` tuples or ``_SHUTDOWN_SENTINEL``.
        # See ``_on_frame`` (producer) + ``_drain_loop`` (consumer).
        self._write_queue: queue.Queue = queue.Queue(
            maxsize=write_queue_maxsize)
        self._write_worker: Optional[threading.Thread] = None
        self._dropped_frames: int = 0
        # Throttle counter — distinct from _dropped_frames so we can log
        # the first drop deterministically while suppressing the next
        # ~1000 (storm-time logspam is its own stall risk).
        self._drop_log_counter: int = 0
        # R2-M5 idempotency guard: a second ``stop()`` call (e.g., signal
        # handler + finally-block chain) must NOT block on putting a
        # second sentinel when the worker is already wedged.
        self._stop_called: bool = False

        # Direct kwargs (not **dict-spread) so AST contract tests can
        # statically verify on_session_start + on_session_end are wired
        # without needing a dataflow analyzer (defense-in-depth against
        # the D1.2 R1-C3 class: callback wired in test fixture but not
        # in production code).
        # D1.3-fu3 (ticket 86b9zjyr0, 2026-05-17): raise ping_timeout from
        # the kalshi_wire default (10s) to 30s. Post-D1.3-fu1 ws_max_size
        # uncap, Kalshi sends 3-4 MiB subscribe-ack messages; processing
        # them on the single asyncio thread blocks the loop past the 10s
        # ping-timeout window → WS lib raises 1011 (keepalive ping timeout)
        # → reconnect storm. Per-collector override (bot's default 10s
        # stays unchanged — bot subscribes to ~50-100 tickers, no buffer
        # backup). Pin retained as defense-in-depth post-D1.3-fu4 (the
        # worker-thread decouple closed the 1011 storm class; this 30s
        # timeout now hedges against any unforeseen residual asyncio-
        # thread block). Pinned by
        # tests/contracts/test_collector_ws_client_ping_timeout.py.
        if url is None:
            self._wire = WSClient(
                api_key=api_key,
                private_key=self._private_key,
                on_frame=self._on_frame,
                on_session_start=self._on_session_start,
                on_session_end=self._on_session_end,
                ping_timeout=30.0,
            )
        else:
            self._wire = WSClient(
                api_key=api_key,
                private_key=self._private_key,
                on_frame=self._on_frame,
                on_session_start=self._on_session_start,
                on_session_end=self._on_session_end,
                url=url,
                ping_timeout=30.0,
            )

    # ── Subscription updates (D1.4) ─────────────────────────────────────

    def update_subscriptions(
        self,
        subscribe_frames: Sequence[Dict],
        cmd_id_to_channel: Mapping[int, str],
    ) -> None:
        """Atomically replace the subscribe frames + cmd_id_to_channel map.

        Called by ``RestSnapshotRefresher`` (via main_loop's on_refresh
        callback) when the REST catalog refresh produces a new ticker
        set. After ``update_subscriptions``, the caller should invoke
        ``request_reconnect`` so the WSClient cycles its session, which
        fires ``on_session_end`` (clears sid→channel) → ``on_session_start``
        (dispatches the NEW subscribe_frames).

        Safety model (R1-M4 from D1.4 adv round 1 — DO NOT overclaim):
        the writes here use an ATOMIC-REPLACE pattern (full attribute
        reassignment with the lock held). ``_on_session_start`` and
        ``_handle_subscribe_ack`` read these attributes WITHOUT acquiring
        ``self._lock`` — that is safe in the current code because:
          - ``_on_session_start`` does ``for frame in self._subscribe_frames``:
            the LOAD_ATTR is a single bytecode op and captures the
            current tuple by reference; a subsequent rebind via
            ``update_subscriptions`` cannot affect the already-captured
            tuple (tuples are immutable).
          - ``_handle_subscribe_ack`` does ``self._cmd_id_to_channel.get(cmd_id)``:
            single attribute lookup + single dict.get call, both atomic
            under the GIL.
        Any future change that iterates these attributes across MULTIPLE
        bytecode ops without snapshotting (e.g., a
        ``for cmd_id, channel in self._cmd_id_to_channel.items()``
        WITHOUT capturing to a local first) MUST acquire ``self._lock``
        in the reader, OR copy the attribute to a local variable first
        and iterate the local. The lock here also bounds the window
        where ``_subscribe_frames`` (new) and ``_cmd_id_to_channel``
        (old) could be in mixed states across a concurrent ``on_frame``;
        an ack arriving against the new frames before the
        cmd_id_to_channel update lands would just no-op (lookup
        returns None → handle_subscribe_ack returns).
        """
        with self._lock:
            self._subscribe_frames = tuple(subscribe_frames)
            self._cmd_id_to_channel = dict(cmd_id_to_channel)

    def request_reconnect(self) -> None:
        """Delegate to the underlying ``WSClient.request_reconnect``.

        D1.4 surface: lets main_loop force-cycle the WS session after a
        ``update_subscriptions`` so the new tickers actually take effect
        (Kalshi has no in-session add/remove API; we have to reconnect
        and re-subscribe).
        """
        self._wire.request_reconnect()

    # ── Session callbacks ───────────────────────────────────────────────

    def _on_session_start(self) -> None:
        """WSClient callback fired AFTER WS connect, BEFORE frame reads.

        Dispatches every pre-built subscribe frame via ``self._wire.send_frame``.
        Mirrors ``bot/feeds/kalshi.py::_on_session_start``'s reconnect-
        time re-subscribe pattern — Kalshi reassigns sids per session,
        so we send every frame on every reconnect (idempotent at Kalshi
        per their protocol). Per-frame exceptions are swallowed (mirroring
        the bot's ``for ticker in resub_tickers: try: ... except: log``
        shape) so a single send failure doesn't abort the dispatch of
        the remaining frames.
        """
        for frame in self._subscribe_frames:
            try:
                self._wire.send_frame(frame)
            except Exception:
                cmd_id = frame.get("id") if isinstance(frame, dict) else None
                logging.warning(
                    "BronzeArchiver subscribe send FAILED on session_start "
                    "(conn=%s cmd_id=%s); continuing with remaining frames.",
                    self._conn_id, cmd_id, exc_info=True,
                )

    def _on_session_end(self) -> None:
        """WSClient callback fired AFTER WS close, BEFORE the backoff
        sleep (R3/P0-A invariant from kalshi_wire.WSClient).

        Sids are Kalshi-session-scoped — fresh session = fresh sids.
        Clearing the map here means a stale sid binding from session N
        cannot mis-route data frames from session N+1 (which Kalshi may
        assign sids starting at the same low numbers). Matches
        ``bot/feeds/kalshi.py::_on_session_end``'s
        ``_ticker_to_sid.clear()`` pattern.
        """
        with self._lock:
            self._sid_to_channel.clear()

    # ── Per-frame dispatch ──────────────────────────────────────────────

    def _on_frame(self, frame: Frame) -> None:
        """WSClient callback (asyncio thread) — route the frame.

        D1.3-fu4 split: this method runs on the kalshi_wire asyncio
        thread + MUST return quickly to avoid blocking the WS keepalive
        ping pong cycle. The heavy work (``build_envelope`` + the
        per-channel writer's zstd-compress + disk IO) is decoupled onto
        a dedicated worker thread that drains ``self._write_queue``.

        Three steps execute synchronously here (must, for correctness):

          1. Subscribe-ack (``type=subscribed`` / ``type=ok``): bind
             sid → channel via ``cmd_id_to_channel`` lookup. The ack
             ALSO routes through the data path (we still write it to
             bronze — the ack itself is part of the wire trace). The
             binding MUST be visible to the very next ``_on_frame``
             call in this tick — deferring it to the worker would let
             data frames arriving immediately after the ack route to
             the _unrouted partition (silent ordering bug).
          2. sid → channel lookup (read lock). Must happen here because
             the binding map is also updated synchronously above.
          3. ``collector_seq`` allocation (write lock). Single-FIFO-worker
             dispatch preserves the monotonic-emit ordering only if the
             producer assigns seq under the same lock; deferring would
             let the worker observe out-of-order seqs on bursts.

        Step 4 (envelope build + writer dispatch) is enqueued + handled
        by ``_drain_loop`` on the worker thread.

        On queue-full: drop the frame, bump ``_dropped_frames`` counter,
        log a throttled WARNING. We deliberately do NOT block here —
        the whole purpose of the Bit is to keep this callback fast so
        the asyncio loop services its ping-pong cycle.
        """
        # Step 1: subscribe-ack binds sid → channel. Note: type=subscribed
        # nests sid in ``msg.sid`` (NOT at the envelope top level) while
        # kalshi_wire.WSClient only pulls top-level ``sid`` onto
        # ``Frame.sid``. So Frame.sid is None for the type=subscribed
        # case — _handle_subscribe_ack does its own msg.sid lookup.
        # type=ok puts sid at top level and Frame.sid IS populated.
        if frame.msg_type in _SUBSCRIBE_ACK_TYPES:
            self._handle_subscribe_ack(frame)

        # Step 2: resolve channel for this frame's envelope.
        channel: Optional[str] = None
        if frame.sid is not None:
            with self._lock:
                channel = self._sid_to_channel.get(frame.sid)

        # Step 3: allocate seq under lock so the worker can dispatch in
        # FIFO+monotonic order.
        with self._lock:
            self._collector_seq += 1
            seq = self._collector_seq

        # Step 4 deferred: enqueue (frame, channel, seq) for the worker.
        # MUST use put_nowait — a blocking put on a stalled queue would
        # re-introduce the asyncio loop stall this whole Bit closes.
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
                # Logging itself can block briefly under contention, but
                # the throttle bounds the frequency. We accept ms-scale
                # log waits on the asyncio thread in exchange for
                # observability — total silence would be worse.
                logging.warning(
                    "BronzeArchiver write_queue full (conn=%s seq=%d) — "
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
                    # CRITICAL D0.3 §2 invariant: _wire_recv_ts MUST be
                    # captured at frame ingress, NOT at envelope-build
                    # time. WSClient stamps Frame.wire_recv_ts BEFORE
                    # json.loads at the ws-recv site; forwarding it here
                    # keeps bronze fidelity intact. Default-None on
                    # build_envelope would silently substitute
                    # datetime.now() — which is worker-thread-time post-
                    # D1.3-fu4 (further removed from ingress than the
                    # pre-fix dispatch-callback time), making the drift
                    # *worse* if the kwarg ever gets dropped.
                    wire_recv_ts = datetime.fromtimestamp(
                        frame.wire_recv_ts, tz=timezone.utc,
                    )
                    envelope = build_envelope(
                        raw=frame.raw,
                        source="kalshi_ws",
                        channel=channel,
                        conn=self._conn_id,
                        collector_seq=seq,
                        wire_recv_ts=wire_recv_ts,
                    )
                    writer = self._writers_by_channel.get(channel)
                    if writer is None:
                        # Channel resolved but no writer for it — should
                        # not happen if main_loop pre-allocated writers
                        # for every channel in the planner's set. Fall
                        # back to _unrouted so the frame survives. Log
                        # so silver QA can detect the misconfig.
                        writer = self._writers_by_channel.get(None)
                        logging.warning(
                            "BronzeArchiver no writer for channel=%r "
                            "(conn=%s seq=%d); falling back to _unrouted.",
                            channel, self._conn_id, seq,
                        )
                        if writer is None:
                            # No fallback writer either — drop with
                            # WARNING; the caller misconfigured
                            # writers_by_channel.
                            logging.warning(
                                "BronzeArchiver no _unrouted writer "
                                "either (conn=%s seq=%d) — frame dropped.",
                                self._conn_id, seq,
                            )
                            continue
                        # When falling back to the _unrouted writer, the
                        # envelope must declare _channel=None to match
                        # the writer's constructor channel — BronzeWriter
                        # .write() validates this and would raise
                        # ValueError otherwise.
                        envelope["_channel"] = None
                    writer.write(envelope)
                except Exception:
                    # Per-frame failure: log + continue. The worker MUST
                    # NOT die — silent total loss of bronze writes would
                    # be much worse than per-frame drops.
                    logging.warning(
                        "BronzeArchiver writer failed for frame seq=%d "
                        "(conn=%s)",
                        seq, self._conn_id, exc_info=True,
                    )
            finally:
                self._write_queue.task_done()

    def _handle_subscribe_ack(self, frame: Frame) -> None:
        """Bind ``sid → channel`` for subsequent data frames.

        Kalshi's two ack shapes (both carry sid + cmd_id, but in
        DIFFERENT positions):

          - ``type=subscribed`` (channel establishment, first per channel
            per session): ``{"id": cmd_id, "type": "subscribed", "msg":
            {"channel": "orderbook_delta", "sid": N}}`` — sid is in
            ``msg.sid``.
          - ``type=ok`` (subsequent subscribes): ``{"id": cmd_id, "type":
            "ok", "sid": N, "seq": M, "msg": {"market_tickers": [...]}}``
            — sid is at the envelope top level.

        kalshi_wire.WSClient only lifts the TOP-LEVEL sid onto
        ``Frame.sid``, so the type=subscribed branch has Frame.sid=None
        and we must reach into ``parsed["msg"]["sid"]``. Matches the
        bot-side dual-path in bot/feeds/kalshi.py:1372-1391.

        We use the cmd_id echoed back to look up which channel WE
        subscribed (via cmd_id_to_channel from the planner). More
        reliable than reading ``msg.channel`` from the ack — only
        ``type=subscribed`` carries that field, and trusting the
        cmd_id→channel map (which we control) means a future Kalshi
        protocol tweak that drops ``msg.channel`` from acks doesn't
        silently break sid binding.
        """
        if not isinstance(frame.parsed, dict):
            return
        cmd_id = frame.parsed.get("id")
        if not isinstance(cmd_id, int):
            return
        channel = self._cmd_id_to_channel.get(cmd_id)
        if channel is None:
            return
        # Sid lookup — top-level first (type=ok shape), then msg.sid
        # (type=subscribed shape).
        sid_value: Optional[int] = frame.sid
        if sid_value is None:
            msg = frame.parsed.get("msg")
            if isinstance(msg, dict):
                _s = msg.get("sid")
                if isinstance(_s, int):
                    sid_value = _s
        if sid_value is None:
            return
        with self._lock:
            self._sid_to_channel[sid_value] = channel

    # ── Lifecycle ───────────────────────────────────────────────────────

    def run(self, shutdown_event: Optional[threading.Event] = None) -> None:
        """Start the WS client and block until shutdown is signaled.

        Per-conn instance — main_loop.run() constructs and starts one
        archiver per planned WS connection.

        ``shutdown_event``: if provided, ``run`` blocks on
        ``event.wait()``; caller drives shutdown. If None, installs
        SIGINT/SIGTERM handlers on the (assumed-main) thread and waits
        on an internal Event. Tests pass a controlled event to avoid
        signal-handler pollution. With multiple archivers per process
        (D1.3 multi-conn), the caller should own the event and pass it
        to all archivers so signals fan out — main_loop.run() does this.
        """
        owned_event = shutdown_event is None
        if owned_event:
            shutdown_event = threading.Event()
            # signal.signal raises ValueError on non-main thread; tolerate
            # so a test or alt-thread invocation falls back to event-only
            # control (test must call .stop() explicitly via the event).
            try:
                signal.signal(signal.SIGINT, lambda *_: shutdown_event.set())
                signal.signal(signal.SIGTERM, lambda *_: shutdown_event.set())
            except ValueError:
                logging.warning(
                    "BronzeArchiver.run(): could not install SIGINT/SIGTERM "
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
        starts emitting frames, or the first burst of frames would
        either drop (if maxsize is small) or accumulate against a
        non-existent consumer.

        Sync (matches WSClient.start). Idempotent at the WIRE level
        (WSClient.start no-ops on a live thread). Re-entrancy semantics
        for the WORKER: if start() is called after a prior stop(), a
        FRESH worker is spawned + ``_dropped_frames`` / ``_drop_log_counter``
        are reset to zero so health monitors see per-session deltas
        from a known floor. Test fixtures that auto-start the worker
        (``_make_archiver``) rely on this reset so cross-test state
        doesn't leak via the counter.
        """
        if self._write_worker is None or not self._write_worker.is_alive():
            with self._lock:
                # R1-M2: reset counters on worker (re)spawn so per-session
                # observability has a known floor.
                self._dropped_frames = 0
                self._drop_log_counter = 0
                # R2-M5: clear the idempotency guard so a subsequent
                # stop() (post-restart) executes its drain logic instead
                # of early-returning.
                self._stop_called = False
            self._write_worker = threading.Thread(
                target=self._drain_loop,
                # R1-M6: include id(self) so multiple archivers with the
                # same conn_id (test fixture re-runs) have distinguishable
                # thread names for debugging.
                name=(
                    f"BronzeArchiver-writer-{self._conn_id}-{id(self):x}"
                ),
                daemon=True,
            )
            self._write_worker.start()
        self._wire.start()

    def stop(self) -> None:
        """Stop the underlying WSClient, then drain + join the worker.

        Order matters: stop the wire FIRST + JOIN its asyncio thread so
        no new frames enqueue after the sentinel is posted. Without the
        join (R1-C1), ``WSClient.stop()`` is fire-and-forget — the
        asyncio thread keeps running and could call ``_on_frame``
        (→ enqueue) AFTER our sentinel landed, leaving tail frames
        behind the sentinel that the FIFO worker would never drain.

        Then post the shutdown sentinel — the worker drains any frames
        buffered ahead of the sentinel before exiting (FIFO queue
        guarantees this once the producer is quiesced). Finally join
        the worker with a bounded timeout: a wedged writer must not
        block process exit indefinitely.

        Both joins use ``_WORKER_JOIN_TIMEOUT_S``. Total worst-case
        ``stop()`` latency = 2 × timeout (wire-thread join + worker join);
        in practice the wire-thread exits in milliseconds after the
        ``_stop_event.set`` + ``ws.close()`` schedule lands (R2-M1).

        R2-M5 idempotency: a second ``stop()`` call early-returns. The
        ``run()`` signal handler at present only sets the shutdown
        event (not calling stop() directly), so the current code paths
        don't exercise this — the guard is defensive against future
        callers that might explicitly double-call ``stop()`` (e.g., an
        operator-facing graceful-shutdown coordinator). Without the
        guard, the second call's blocking ``put(_SHUTDOWN_SENTINEL)``
        would deadlock against a wedged worker, since the asyncio
        thread (the only other producer) is already joined.
        """
        with self._lock:
            if self._stop_called:
                logging.debug(
                    "BronzeArchiver.stop (conn=%s) called again — no-op "
                    "(idempotency guard).", self._conn_id,
                )
                return
            self._stop_called = True
        # R1-C1 fix + R2-M1: join the asyncio thread (now waked by
        # ws.close() schedule in WSClient.stop) so no _on_frame can fire
        # after this returns. join_timeout > 0 triggers the join (default
        # 0 preserves the bot-side fire-and-forget caller).
        self._wire.stop(join_timeout=_WORKER_JOIN_TIMEOUT_S)
        worker = self._write_worker
        if worker is not None and worker.is_alive():
            # ``put`` (blocking) rather than ``put_nowait`` because we
            # MUST get the sentinel into the queue even if it's at
            # capacity — backpressure on shutdown defeats the drain
            # guarantee. The blocking is bounded by worker throughput
            # (one slot opens per writer.write completion) and the
            # join timeout below caps the total wait.
            self._write_queue.put(_SHUTDOWN_SENTINEL)
            worker.join(timeout=_WORKER_JOIN_TIMEOUT_S)
            if worker.is_alive():
                # Daemon thread will be killed at process exit; warn so
                # the abandoned-tail is at least visible in journals.
                logging.warning(
                    "BronzeArchiver._write_worker (conn=%s) did not "
                    "exit within %.0fs of shutdown signal — abandoning "
                    "(daemon thread will be killed at process exit). "
                    "Pending queue size at abandon: %d.",
                    self._conn_id, _WORKER_JOIN_TIMEOUT_S,
                    self._write_queue.qsize(),
                )
