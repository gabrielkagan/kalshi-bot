"""Kalshi WS consumer — D1.1.5 + D1.2 + D1.3 (tickets 86b9zdhz2 +
86b9ypn66 + 86b9ypn72, 2026-05-16).

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

NO ``bot.*`` imports (pinned by ``collector-no-bot`` import-linter
contract). Auth + WS transport reach into ``kalshi_wire/`` only.
"""
from __future__ import annotations

import logging
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
        """
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

        # Direct kwargs (not **dict-spread) so AST contract tests can
        # statically verify on_session_start + on_session_end are wired
        # without needing a dataflow analyzer (defense-in-depth against
        # the D1.2 R1-C3 class: callback wired in test fixture but not
        # in production code).
        if url is None:
            self._wire = WSClient(
                api_key=api_key,
                private_key=self._private_key,
                on_frame=self._on_frame,
                on_session_start=self._on_session_start,
                on_session_end=self._on_session_end,
            )
        else:
            self._wire = WSClient(
                api_key=api_key,
                private_key=self._private_key,
                on_frame=self._on_frame,
                on_session_start=self._on_session_start,
                on_session_end=self._on_session_end,
                url=url,
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
        """WSClient callback: route the frame.

        Three branches:

          1. Subscribe-ack (``type=subscribed`` / ``type=ok``): bind
             sid → channel via ``cmd_id_to_channel`` lookup. The ack
             ALSO routes through the data path (we still write it to
             bronze — the ack itself is part of the wire trace).
          2. Data frame with sid mapped → envelope ``_channel`` set,
             route to ``writers_by_channel[channel]``.
          3. Data frame with sid unmapped (or sid=None) → envelope
             ``_channel`` is None, route to ``writers_by_channel[None]``
             (the ``_unrouted`` partition). Bronze captures every frame;
             unrouted bytes go to a separate path for silver QA.
        """
        # Branch 1: subscribe-ack binds sid → channel. Note: type=subscribed
        # nests sid in ``msg.sid`` (NOT at the envelope top level) while
        # kalshi_wire.WSClient only pulls top-level ``sid`` onto
        # ``Frame.sid``. So Frame.sid is None for the type=subscribed
        # case — _handle_subscribe_ack does its own msg.sid lookup.
        # type=ok puts sid at top level and Frame.sid IS populated.
        if frame.msg_type in _SUBSCRIBE_ACK_TYPES:
            self._handle_subscribe_ack(frame)

        # Resolve channel for this frame's envelope.
        channel: Optional[str] = None
        if frame.sid is not None:
            with self._lock:
                channel = self._sid_to_channel.get(frame.sid)

        # Allocate seq + build envelope + dispatch.
        with self._lock:
            self._collector_seq += 1
            seq = self._collector_seq
        try:
            # CRITICAL D0.3 §2 invariant: _wire_recv_ts MUST be captured
            # at frame ingress, NOT at envelope-build time. WSClient
            # stamps Frame.wire_recv_ts BEFORE json.loads at the ws-recv
            # site; forwarding it here keeps bronze fidelity intact.
            # Default-None on build_envelope would silently substitute
            # datetime.now() — which is dispatch-callback-time, NOT
            # wire-ingress-time, and the difference grows under load.
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
                # Channel resolved but no writer for it — should not
                # happen if main_loop pre-allocated writers for every
                # channel in the planner's set. Fall back to _unrouted
                # so the frame survives. Log so silver QA can detect
                # the misconfig.
                writer = self._writers_by_channel.get(None)
                logging.warning(
                    "BronzeArchiver no writer for channel=%r (conn=%s "
                    "seq=%d); falling back to _unrouted.",
                    channel, self._conn_id, seq,
                )
                if writer is None:
                    # No fallback writer either — drop with WARNING; the
                    # caller misconfigured writers_by_channel.
                    logging.warning(
                        "BronzeArchiver no _unrouted writer either "
                        "(conn=%s seq=%d) — frame dropped.",
                        self._conn_id, seq,
                    )
                    return
                # When falling back to the _unrouted writer, the envelope
                # must declare _channel=None to match the writer's
                # constructor channel — BronzeWriter.write() validates
                # this and would raise ValueError otherwise.
                envelope["_channel"] = None
            writer.write(envelope)
        except Exception:
            logging.warning(
                "BronzeArchiver writer failed for frame seq=%d (conn=%s)",
                seq, self._conn_id, exc_info=True,
            )

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
        """Start the underlying WSClient — invoked by ``run()`` and
        exercised independently by the kalshi_wire differential test.
        Sync (matches WSClient.start)."""
        self._wire.start()

    def stop(self) -> None:
        """Stop the underlying WSClient."""
        self._wire.stop()
