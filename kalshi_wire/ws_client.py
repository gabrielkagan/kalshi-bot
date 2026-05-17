"""Kalshi WS transport — D1.1.5 Phase 3b (ticket 86b9zdhz2, 2026-05-16).

Pure-transport leaf consumed by ``bot/feeds/kalshi.py`` (KalshiFeed) AND
(post-Phase-4) ``collector/ws_connection.py``. The 2026-05-16 AMENDMENT
to ``kb/decisions/data-corpus-architecture.md`` §5 SUPERSEDED the
original "duplicate the minimum in collector" plan after the external-
advisor pivot: capture + replay must be "two sides of the same coin".

This module owns:
  - WS connect / reconnect (exponential backoff with jitter)
  - RSA-PSS handshake auth (via ``kalshi_wire.auth.make_ws_headers``)
  - Silence watchdog (force-reconnect if no frame for N seconds)
  - Frame parse + envelope ``(sid, seq)`` gap detection
  - Thread-safe outgoing-frame queue
  - 4 sync callbacks invoked from the asyncio thread:
    * ``on_session_start()`` — fires after WS connect, BEFORE reading
      frames. Consumer uses this to send channel-subscribe frames.
    * ``on_frame(Frame)`` — fires for every incoming WS message. Watchdog
      ``_last_msg_ts`` is set BEFORE invocation (Apr-24 silence-watchdog
      ordering — load-bearing).
    * ``on_session_end()`` — fires AFTER WS closes (graceful or excepted)
      BEFORE the reconnect backoff sleep. Consumer uses this to clear
      its per-session caches (R3/P0-A data-integrity invariant).
    * ``on_drain_tick()`` — fires every ``drain_tick_interval_s``
      seconds on the asyncio thread. Consumer uses this to run periodic
      maintenance (snapshot timeouts, drain pending sub queues, etc.).

The class shape (constructor signature, public method names,
``is_connected`` property) is pinned by
``tests/contracts/test_kalshi_wire_ws_client.py``; differential behavior
is pinned by ``tests/equivalence/test_kalshi_wire_differential.py``.

NO imports from ``bot.*`` or ``collector.*`` (pinned by import-linter
contracts ``kalshi_wire-no-bot`` + ``kalshi_wire-no-collector``).

Anti-patterns honored (root ``CLAUDE.md``):
  - Synchronous public API. asyncio is INTERNAL to this class.
  - No SQLite touch (this is wire-only).
  - All silence-watchdog / reconnect tunables are constructor kwargs;
    we never reach into a bot-side config singleton.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import websockets

from kalshi_wire.auth import make_ws_headers


# Default URL — overridable via WSClient(url=...). Sourcing from a
# constant here (not bot.constants) preserves the kalshi_wire-no-bot
# contract. The historical bot.constants.KALSHI_WS_URL value is
# unchanged.
DEFAULT_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"


@dataclass
class Frame:
    """One Kalshi WS frame as observed at the wire.

    Per D0.3 §2 spec, ``wire_recv_ts`` is captured at frame ingress
    BEFORE ``json.loads(raw)``. This is the ONLY field that cannot be
    reconstructed from ``raw`` after the fact — Kalshi's server-side
    timestamp (inside the payload) doesn't include receipt latency.

    Fields:
        wire_recv_ts: Unix-epoch seconds with microsecond precision
            (``time.time()`` return value). Use ``build_envelope`` to
            format for bronze.
        raw: The full raw wire payload as a string. Bronze captures this
            verbatim — no decoding, no normalization, no field re-ordering.
        parsed: ``json.loads(raw)`` result. ``None`` if parse failed
            (malformed frame from the wire).
        msg_type: ``parsed["type"]`` if present, else None. Pre-extracted
            for fast dispatch by consumers.
        sid: Envelope subscription id (Kalshi assigns; channel-scoped post
            Phase 2.10).
        seq: Per-subscription monotonic sequence number. Gaps indicate
            dropped/reordered messages (diagnosed via
            ``WS_SEQ_GAP`` log in the original KalshiFeed; this module
            emits the same log when a gap is detected).
    """

    wire_recv_ts: float
    raw: str
    parsed: Optional[Dict[str, Any]]
    msg_type: Optional[str]
    sid: Optional[int]
    seq: Optional[int]


def build_envelope(
    raw: str,
    *,
    source: str,
    channel: Optional[str],
    conn: Optional[str],
    collector_seq: int,
    wire_recv_ts: Optional[_dt.datetime] = None,
) -> Dict[str, Any]:
    """Construct the D0.3 §2 6-field bronze envelope.

    The envelope IS the bronze contract — silver ETL dispatches on these
    keys; downstream readers depend on the shape staying stable. **DO NOT
    add fields here** (per D0.3 §2: bronze is immutable; schema-rev lives
    at silver).

    Args:
        raw: The full raw wire payload as a string. NOT JSON-parsed —
            bronze captures bytes verbatim per the D0.3 §0 operator
            principle ("store all raw data, transform downstream with dbt").
        source: e.g. ``kalshi_ws``, ``coinbase_ws``, ``nws_hrrr``. Used by
            silver ETL dispatch.
        channel: WS channel name (``orderbook_delta`` / ``trade`` /
            ``market_lifecycle_v2``) or None for REST snapshots.
        conn: WS connection id (A/B/C/D/E/F for Kalshi multi-conn) or None
            for REST snapshots. Lets silver QA detect single-conn outages
            without joining a separate health log.
        collector_seq: Monotone-increasing per-collector-process sequence
            from boot, used by silver QA (D2.4) to detect gaps independent
            of ``wire_recv_ts``.

    Returns:
        A dict with the 6 reserved fields in the spec-required order:
        ``_wire_recv_ts``, ``_source``, ``_conn``, ``_channel``,
        ``_collector_seq``, ``_raw``. JSON-serializable as a single line
        (JSONL invariant).
    """
    # wire_recv_ts kwarg (added 2026-05-16 in D1.2 for deterministic-ts
    # testing). Default-None preserves the prior "capture-at-call-time"
    # behavior used by the WS read loop — the differential test sees
    # byte-identical output for default callers.
    if wire_recv_ts is None:
        wire_recv_ts = _dt.datetime.now(_dt.timezone.utc)
    else:
        wire_recv_ts = wire_recv_ts.astimezone(_dt.timezone.utc)
    ts_iso = wire_recv_ts.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    return {
        "_wire_recv_ts": ts_iso,
        "_source": source,
        "_conn": conn,
        "_channel": channel,
        "_collector_seq": collector_seq,
        "_raw": raw,
    }


class WSClient:
    """Kalshi WS transport client — shared by bot and collector.

    Consumers (bot/feeds/kalshi.py KalshiFeed, collector/ws_connection.py)
    own state — orderbook caches, sid maps, cmd_id management, blacklists.
    This client owns transport: connect, reconnect, auth handshake,
    silence watchdog, frame parse + seq-gap detect, thread-safe send
    queue.

    Threading model (mirrors KalshiFeed's pre-extraction shape):
      - ``start()`` spawns one daemon thread that runs an asyncio event
        loop. All ws.send / ws.recv happen on that thread.
      - All callbacks (``on_session_start``, ``on_frame``,
        ``on_session_end``, ``on_drain_tick``) run on the asyncio thread.
        Consumers MUST treat them as if they hold no GIL preference.
      - ``send_frame(payload)`` is thread-safe (callable from any
        thread); it enqueues on an asyncio.Queue via
        ``loop.call_soon_threadsafe``.
      - ``request_reconnect()`` is thread-safe; it sets a flag the
        silence watchdog observes.
      - ``stop()`` is thread-safe; signals the asyncio stop_event.

    Reconnect cleanup (R3/P0-A — preserved exactly from pre-extraction):
      - On exception: ``on_session_end()`` fires BEFORE the backoff sleep
      - On graceful close: ``on_session_end()`` fires BEFORE the next
        loop iteration
      - This ensures the consumer's per-session caches are cleared while
        ``is_connected`` reads False, so scan paths see no_orderbook
        instead of stale state.
    """

    def __init__(
        self,
        api_key: str,
        private_key,
        *,
        on_frame: Callable[[Frame], None],
        url: str = DEFAULT_WS_URL,
        on_session_start: Optional[Callable[[], None]] = None,
        on_session_end: Optional[Callable[[], None]] = None,
        on_drain_tick: Optional[Callable[[], None]] = None,
        silence_grace_s: float = 30.0,
        silence_timeout_s: float = 90.0,
        watchdog_check_interval: float = 15.0,
        drain_tick_interval_s: float = 2.0,
        ping_interval: float = 30.0,
        ping_timeout: float = 10.0,
        max_backoff_s: float = 60.0,
        seq_gap_max_logs: int = 500,
        ws_max_size: int = 16 * 1024 * 1024,
        _test_skip_auth: bool = False,
    ):
        # ws_max_size: incoming-message ceiling passed to
        # ``websockets.connect(max_size=...)``. Default 16 MiB.
        #
        # D1.3-fu1 (86b9zju8h, 2026-05-17): python websockets defaults
        # max_size=1 MiB. Kalshi's type=subscribed/type=ok acks include
        # the cumulative subscribed-ticker list per sid, so collector
        # subscriptions (~74K tickers/conn) produce acks that grow past
        # 1 MiB → our lib closes with 1009 → reconnect → loop (verified
        # bronze ack at cmd_id=21: 21,000 tickers, ~951 KB). Bot is
        # unaffected (subscribes to ~50-100 tickers, acks tiny). 16 MiB
        # default clears worst-case Kalshi ack at full subscription
        # growth (~74K × ~50 bytes ≈ 3.7 MiB) with ~4x headroom. See
        # ``kb/decisions/d1-3-fu1-max-size-fix-plan.md``.
        if not isinstance(ws_max_size, int) or isinstance(ws_max_size, bool):
            raise TypeError(
                f"ws_max_size must be int (got {type(ws_max_size).__name__}). "
                "The wire library declines None/non-int to keep incoming-frame "
                "memory bounded against accidental no-cap configuration."
            )
        if ws_max_size < 1:
            raise ValueError(
                f"ws_max_size must be ≥ 1 (got {ws_max_size})."
            )
        self._api_key = api_key
        self._private_key = private_key
        self._url = url
        self._on_frame = on_frame
        self._on_session_start = on_session_start
        self._on_session_end = on_session_end
        self._on_drain_tick = on_drain_tick
        self._silence_grace_s = silence_grace_s
        self._silence_timeout_s = silence_timeout_s
        self._watchdog_check_interval = watchdog_check_interval
        self._drain_tick_interval_s = drain_tick_interval_s
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._max_backoff_s = max_backoff_s
        self._seq_gap_max_logs = seq_gap_max_logs
        self._ws_max_size = ws_max_size
        # Internal flag — allow tests to skip RSA-PSS signing when pointing
        # at a mock server. Not part of the public API surface.
        self._skip_auth = _test_skip_auth

        # Internal state — owned by the asyncio thread (except where noted).
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._send_queue: Optional[asyncio.Queue] = None
        self._thread: Optional[threading.Thread] = None
        self._ws = None
        # Connection state — accessed from any thread; protect with
        # _state_lock. Independent of the consumer's own lock.
        self._state_lock = threading.Lock()
        self._connected_state = False
        self._connect_ts = 0.0
        self._last_msg_ts = 0.0
        self._force_reconnect_requested = False
        # Seq-gap diagnostics — wire-level concern (not bot-state).
        self._ws_last_seq: Dict[int, int] = {}
        self._ws_seq_gap_logs = 0

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        """True iff the WS session is currently connected to Kalshi."""
        with self._state_lock:
            return self._connected_state

    def start(self) -> None:
        """Start the WS client — spawns the asyncio daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_thread, daemon=True)
        self._thread.start()

    def stop(self, *, join_timeout: float = 0.0) -> None:
        """Stop the WS client — signal the event loop and (optionally) join
        the asyncio thread before returning.

        Args:
            join_timeout: when > 0, block up to this many seconds waiting
                for the asyncio daemon thread to exit. When 0 (default,
                preserves pre-2026-05-17 behavior), the call is
                fire-and-forget — callers must NOT assume that subsequent
                actions race-freely follow the stop signal. The on_frame /
                on_session_start / on_session_end callbacks may still
                fire after this returns.

                D1.3-fu4 (collector/BronzeArchiver.stop) passes a positive
                timeout so it can sequence ``wire.stop → drain → join``
                deterministically: without the join the BronzeArchiver
                worker-queue can receive frames AFTER the shutdown
                sentinel is posted (because the asyncio thread is still
                alive), and those tail frames are then abandoned behind
                the sentinel by the FIFO worker.

                On timeout the thread keeps running (it's a daemon, so
                process exit will reap it). Returns silently — the caller
                can inspect ``is_connected`` or thread state if it needs
                to differentiate clean-shutdown from timeout-abandon.
        """
        if self._loop is not None and self._stop_event is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                # Loop already closed; nothing to signal.
                pass
        if join_timeout > 0 and self._thread is not None:
            # Wait for the asyncio thread to drain pending callbacks +
            # exit. Daemon thread so timeout-without-exit is non-fatal.
            self._thread.join(timeout=join_timeout)

    def send_frame(self, payload: Dict[str, Any]) -> None:
        """Thread-safe enqueue of an outgoing WS frame.

        Callable from any thread. Validates synchronously:
          1. JSON-serializable (raises TypeError immediately)
          2. WS connected (raises ConnectionError immediately)
        — so callers' exception handlers fire on the SAME thread that
        invoked send_frame. This restores the pre-extraction Phase 2.6
        R-review A3 protection: ``_send_ob_subscribe`` pops orphan
        cmd_ids from ``_outstanding_subscribes`` in its except block
        when the send fails (pre-extraction the synchronous
        ``await ws.send`` could raise; post-extraction the actual
        wire-write is deferred to ``_send_drain`` but THIS method
        raises if WS is not currently connected, preserving the
        synchronous-failure contract callers rely on).

        Consumers own cmd_id management, sid mapping, and ack
        correlation — this method just wraps ``json.dumps(payload)``
        for the wire.
        """
        # Validate JSON-serializable up front so callers see TypeError
        # immediately, not async-deferred.
        json.dumps(payload)
        # Phase 2.6 R-review A3 protection: raise immediately if WS
        # isn't connected so caller's exception path pops the orphan
        # cmd_id (pre-extraction ws.send raised synchronously here).
        if not self.is_connected:
            raise ConnectionError(
                "kalshi_wire.WSClient.send_frame: WS not connected; "
                "frame dropped. Caller should clean up any per-frame "
                "state (outstanding cmd_id, etc.) before retrying.")
        if self._loop is None or self._send_queue is None:
            raise ConnectionError(
                "kalshi_wire.WSClient.send_frame: event loop not "
                "ready; client not started or already stopped.")
        if self._loop.is_closed():
            raise ConnectionError(
                "kalshi_wire.WSClient.send_frame: event loop closed; "
                "client stopped.")
        try:
            self._loop.call_soon_threadsafe(
                self._send_queue.put_nowait, payload)
        except RuntimeError as exc:
            # Race: loop closed between the check and the call.
            # Raise so caller can clean up — mirrors the closed-WS
            # case above.
            raise ConnectionError(
                "kalshi_wire.WSClient.send_frame: event loop closed "
                "between connectivity check and enqueue (race)."
            ) from exc

    def request_reconnect(self) -> None:
        """Thread-safe signal to force a WS reconnect.

        Used by KalshiFeed's B2 watchdog (Phase 2.6 R4 / A1+A2+A3) when a
        ticker's subscribe gets stuck and only a fresh WS session can
        recover it. The silence-watchdog task observes the flag and
        force-closes the WS.
        """
        with self._state_lock:
            self._force_reconnect_requested = True

    # ── Internal — asyncio thread ─────────────────────────────────────────

    def _run_thread(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        self._send_queue = asyncio.Queue()
        try:
            self._loop.run_until_complete(self._ws_loop())
        except Exception:
            logging.error("Kalshi wire thread crashed", exc_info=True)
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    def _build_headers(self) -> Dict[str, str]:
        if self._skip_auth or self._private_key is None:
            return {}
        return make_ws_headers(self._api_key, self._private_key)

    async def _ws_loop(self):
        backoff = 1.0

        while not self._stop_event.is_set():
            try:
                headers = self._build_headers()
                async with websockets.connect(
                    self._url,
                    additional_headers=headers,
                    ping_interval=self._ping_interval,
                    ping_timeout=self._ping_timeout,
                    max_size=self._ws_max_size,
                ) as ws:
                    self._ws = ws
                    now = time.time()
                    with self._state_lock:
                        self._connected_state = True
                        self._connect_ts = now
                        self._last_msg_ts = now
                        # Reset wire-level seq tracking on new session
                        # — sids are session-scoped so old seq state
                        # would produce spurious gap warnings.
                        self._ws_last_seq.clear()
                        self._ws_seq_gap_logs = 0
                        # Reset force-reconnect flag — if we're here,
                        # the prior request was honored by closing the
                        # session. Note: pre-extraction
                        # (``bot/feeds/kalshi.py`` HEAD revision
                        # ``_cleanup_session_state``) this reset happened
                        # on session END (before the backoff sleep);
                        # post-extraction we reset on session START
                        # (after backoff). Both callers
                        # (``OpportunityScanner.scan`` R2 escalation +
                        # ``_check_snapshot_timeouts``) gate on
                        # ``is_connected=True`` so neither can set the
                        # flag during the backoff window — the timing
                        # shift is a wash in practice.
                        self._force_reconnect_requested = False
                    # R3/P0-A — reset reconnect backoff on successful
                    # connect (mirrors the pre-extraction reset at
                    # bot/feeds/kalshi.py:854 of the HEAD revision). A
                    # long-lived session that ends gracefully should
                    # reconnect fast; without this reset, the backoff
                    # accumulates from the last exception path and
                    # cascades 30-60s delays on otherwise-healthy
                    # reconnects.
                    backoff = 1.0
                    logging.info(f"kalshi_ws_connected: url={self._url}")

                    # Notify consumer that a fresh session is up. The
                    # consumer typically uses this hook to send its
                    # channel-subscribe frames; those calls flow back
                    # through self.send_frame → self._send_queue → the
                    # _send_drain task below.
                    if self._on_session_start is not None:
                        try:
                            self._on_session_start()
                        except Exception:
                            logging.warning(
                                "kalshi_ws on_session_start callback failed",
                                exc_info=True)

                    # Drain the outgoing-frame queue.
                    async def _send_drain():
                        while not self._stop_event.is_set():
                            try:
                                payload = await self._send_queue.get()
                            except asyncio.CancelledError:
                                raise
                            try:
                                await ws.send(json.dumps(payload))
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                logging.warning(
                                    "kalshi_ws send_frame ws.send FAILED",
                                    exc_info=True)
                    _send_drain_task = asyncio.create_task(_send_drain())

                    # Periodic drain-tick callback — gives the consumer a
                    # heartbeat to run snapshot-timeout checks and drain
                    # its pending queues. Independent of incoming
                    # messages so the deadlock from
                    # kb/failures/ws-subscription-deadlock.md cannot
                    # recur.
                    async def _drain_tick_loop():
                        while not self._stop_event.is_set():
                            try:
                                await asyncio.sleep(
                                    self._drain_tick_interval_s)
                                if self._on_drain_tick is not None:
                                    try:
                                        self._on_drain_tick()
                                    except Exception:
                                        logging.debug(
                                            "kalshi_ws on_drain_tick "
                                            "callback failed", exc_info=True)
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                logging.debug(
                                    "drain-tick iteration failed",
                                    exc_info=True)
                    _drain_tick_task = asyncio.create_task(_drain_tick_loop())

                    # Silence watchdog (preserves the bot's Apr-24
                    # 17:30 UTC 15M-outage fix). Kalshi's WS can stay
                    # "connected" (ping/pong healthy internally) while
                    # delivering zero protocol messages for minutes.
                    # Close the ws so the outer reconnect kicks in.
                    async def _silence_watchdog():
                        while not self._stop_event.is_set():
                            try:
                                await asyncio.sleep(
                                    self._watchdog_check_interval)
                                # Explicit reconnect request from caller.
                                with self._state_lock:
                                    force = self._force_reconnect_requested
                                if force:
                                    logging.error(
                                        "WS_FORCE_RECONNECT — reconnect "
                                        "requested by consumer "
                                        "(request_reconnect()); closing WS.")
                                    try:
                                        await ws.close()
                                    except Exception:
                                        pass
                                    return
                                now2 = time.time()
                                with self._state_lock:
                                    connect_ts = self._connect_ts
                                    last_msg_ts = self._last_msg_ts
                                if (now2 - connect_ts
                                        < self._silence_grace_s):
                                    continue
                                silent = now2 - last_msg_ts
                                if silent > self._silence_timeout_s:
                                    logging.error(
                                        "WS_SILENCE_WATCHDOG: no msg in "
                                        "%.0fs (timeout=%ds) — forcing "
                                        "reconnect", silent,
                                        int(self._silence_timeout_s))
                                    try:
                                        await ws.close()
                                    except Exception:
                                        pass
                                    return
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                logging.debug(
                                    "silence watchdog iteration failed",
                                    exc_info=True)
                    _watchdog_task = asyncio.create_task(_silence_watchdog())

                    try:
                        async for raw in ws:
                            if self._stop_event.is_set():
                                break
                            self._handle_raw_frame(raw)
                    finally:
                        for _bg in (_send_drain_task, _drain_tick_task,
                                    _watchdog_task):
                            _bg.cancel()
                            try:
                                await _bg
                            except (asyncio.CancelledError, Exception):
                                pass

            except asyncio.CancelledError:
                self._on_session_end_safe()
                break
            except Exception as e:
                # R3/P0-A: on_session_end MUST run BEFORE the backoff
                # sleep. Otherwise during the up-to-60s wait,
                # is_connected reads True and stale per-session caches
                # are served as live data. Mirrors the pre-extraction
                # invariant at bot/feeds/kalshi.py:982.
                self._on_session_end_safe()
                jitter = backoff * random.uniform(0, 0.25)
                wait = backoff + jitter
                logging.warning(
                    f"kalshi_ws_disconnected: reason={e} "
                    f"reconnect_backoff={wait:.1f}s")
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=wait)
                    break
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, self._max_backoff_s)
            else:
                # R3/P0-A graceful-close path (watchdog ws.close,
                # server-initiated close, async-with normal exit).
                # Without this, stale per-session caches would survive
                # into the next iteration.
                self._on_session_end_safe()

        with self._state_lock:
            self._connected_state = False
        self._ws = None
        logging.info("Kalshi wire stopped")

    def _on_session_end_safe(self) -> None:
        """Combined consumer-callback fire + state reset.

        Always pairs ``connected_state = False`` with the consumer's
        cleanup hook. Both run BEFORE the reconnect backoff sleep so
        ``is_connected`` reads False during the wait window.
        """
        with self._state_lock:
            self._connected_state = False
        self._ws = None
        if self._on_session_end is not None:
            try:
                self._on_session_end()
            except Exception:
                logging.warning(
                    "kalshi_ws on_session_end callback failed",
                    exc_info=True)

    def _handle_raw_frame(self, raw: str) -> None:
        """Parse a raw WS frame, update wire-level watchdog state, and
        dispatch to the consumer's ``on_frame``.

        Watchdog ordering (Apr-24 silence-watchdog fix — load-bearing):
        ``_last_msg_ts`` is set BEFORE invoking the consumer callback.
        Any message from the server — even an error or unknown type we
        don't dispatch — proves the WS session is healthy.
        """
        now = time.time()
        with self._state_lock:
            self._last_msg_ts = now

        parsed: Optional[Dict[str, Any]]
        msg_type: Optional[str]
        sid: Optional[int]
        seq: Optional[int]
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            parsed = None
            msg_type = None
            sid = None
            seq = None
        else:
            if isinstance(parsed, dict):
                _t = parsed.get("type")
                msg_type = _t if isinstance(_t, str) else None
                _s = parsed.get("sid")
                sid = _s if isinstance(_s, int) else None
                _q = parsed.get("seq")
                seq = _q if isinstance(_q, int) else None
            else:
                msg_type = None
                sid = None
                seq = None

        # Seq-gap detector — wire-level diagnostic, identical to the
        # bot's pre-extraction logic. Bot dispatches still get the frame
        # via on_frame regardless of gap state.
        if sid is not None and seq is not None:
            prev = self._ws_last_seq.get(sid)
            if prev is not None and seq != prev + 1:
                if self._ws_seq_gap_logs < self._seq_gap_max_logs:
                    ticker = "?"
                    if isinstance(parsed, dict):
                        msg = parsed.get("msg")
                        if isinstance(msg, dict):
                            ticker = msg.get("market_ticker", "?")
                    logging.warning(
                        "WS_SEQ_GAP sid=%s ticker=%s type=%s "
                        "expected_seq=%d actual_seq=%d gap=%d",
                        sid, ticker, msg_type or "?", prev + 1, seq,
                        seq - prev - 1)
                    self._ws_seq_gap_logs += 1
            self._ws_last_seq[sid] = seq

        frame = Frame(
            wire_recv_ts=now,
            raw=raw,
            parsed=parsed if isinstance(parsed, dict) else None,
            msg_type=msg_type,
            sid=sid,
            seq=seq,
        )
        try:
            self._on_frame(frame)
        except Exception:
            logging.warning(
                "kalshi_ws on_frame callback failed", exc_info=True)
