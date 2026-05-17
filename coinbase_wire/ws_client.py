"""Coinbase Exchange WS transport — D2.1.5 (ticket 86b9zkpny, 2026-05-17).

Pure-transport leaf consumed by ``collector/coinbase_archiver.py``
(D2.2 SHIPPED 2026-05-17, ticket 86b9zkppk) and (post-D2.3) the
refactored ``bot/feeds/coinbase.py``. Mirrors the D1.1.5 ``kalshi_wire/ws_client.py``
shape — same "two sides of the same coin" symmetry per the 2026-05-16
AMENDMENT to ``kb/decisions/data-corpus-architecture.md`` §5 — but
adapted for Coinbase Exchange WS's wire shape (public-only auth at
D2.1.5; per-product ``sequence`` for gap detection at the consumer
layer; single batched subscribe message covering multiple channels).

**Protocol surface — Coinbase Exchange WS, not Advanced Trade WS.** The
bot already consumes ``wss://ws-feed.exchange.coinbase.com`` via
``bot/feeds/coinbase.py`` (see ``bot.constants.COINBASE_WS_URL``);
mirroring the same endpoint here keeps the future D2.3 refactor a
*structural* refactor rather than a protocol-flip. Coinbase Advanced
Trade WS (``wss://advanced-trade-ws.coinbase.com``) is a separate API
surface that would change the bot's existing product coverage (HYPE-USD
is on Exchange but not currently confirmed on Advanced Trade) — out of
scope for D2.1.5.

This module owns:

  - WS connect / reconnect (exponential backoff with jitter)
  - Public-channel subscribe (no signature;
    ``coinbase_wire.auth.build_public_subscribe_message``)
  - Silence watchdog (force-reconnect if no frame for N seconds)
  - Frame parse (top-level ``type`` + per-product ``sequence`` extraction)
  - Thread-safe outgoing-frame queue
  - 4 sync callbacks invoked from the asyncio thread:
    * ``on_session_start()`` — fires after WS connect, BEFORE reading
      frames. The default implementation sends a SINGLE
      ``build_public_subscribe_message`` covering the constructor's
      channels + product_ids defaults (Coinbase Exchange WS batches
      channels in one subscribe frame; consumers that need staggered
      subscribes supply their own callback).
    * ``on_frame(Frame)`` — fires for every incoming WS message. The
      watchdog ``_last_msg_ts`` is set BEFORE invocation (Apr-24
      silence-watchdog ordering — load-bearing).
    * ``on_session_end()`` — fires AFTER WS closes (graceful or
      excepted) BEFORE the reconnect backoff sleep (R3/P0-A data-
      integrity invariant inherited from kalshi_wire).
    * ``on_drain_tick()`` — fires every ``drain_tick_interval_s``
      seconds on the asyncio thread.

The class shape (constructor signature, public method names,
``is_connected`` property) is pinned by
``tests/contracts/test_coinbase_wire_ws_client.py``.

NO imports from ``bot.*`` or ``collector.*`` (pinned by import-linter
contracts ``coinbase_wire-no-bot`` + ``coinbase_wire-no-collector``).

Anti-patterns honored (root ``CLAUDE.md``):
  - Synchronous public API. asyncio is INTERNAL to this class.
  - No SQLite touch (wire-only).
  - All silence-watchdog / reconnect tunables are constructor kwargs;
    we never reach into a bot-side config singleton.

Lessons carried forward from Kalshi-bronze ship arc (2026-05-17):
  - D1.3-fu1: ``ws_max_size`` default 16 MiB (vs python-websockets
    default 1 MiB). Defense-in-depth against future protocol growth.
  - D1.3-fu3: ``ping_timeout`` 30s default (vs websockets default 20s).
    Permissive timeout reduces spurious 1011 close cycles.
  - D1.3-fu4 / fu5 patterns are NOT relevant here — those are consumer
    (collector) concerns about whether write dispatch blocks the
    asyncio thread. coinbase_wire is pure-transport; the consumer
    (``collector/coinbase_archiver.py``, D2.2 SHIPPED) applies worker-
    thread decouple + skip-ack-enqueue from day 1 because we already
    paid those lessons on Kalshi.

Wire-level seq-gap detection: deliberately NOT implemented here.
Coinbase Exchange WS ``sequence`` is per-product monotonic (not
per-connection like Kalshi's per-sid ``seq``), and the per-product
grouping requires joining ``sequence`` with ``product_id`` — context
the consumer (``collector/coinbase_archiver.py``, D2.2 SHIPPED) holds
more cleanly than the wire layer. ``Frame.sequence_num`` is passed
through verbatim so consumers can run their own gap detection.
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
from typing import Any, Callable, Dict, Optional, Tuple

import websockets

from coinbase_wire.auth import build_public_subscribe_message


# Default URL — Coinbase Exchange WS public endpoint. Matches
# ``bot.constants.COINBASE_WS_URL`` (sourced from constants in the bot;
# duplicated here to keep coinbase_wire-no-bot intact). Overridable via
# WSClient(url=...).
DEFAULT_WS_URL = "wss://ws-feed.exchange.coinbase.com"

# Default public channel set — 4 Coinbase Exchange WS channels with
# in-repo verified public reachability:
#   - ``ticker``     — production ``bot/feeds/coinbase.py:278`` already
#                      subscribes here on the live bot, so the path is
#                      proven live without authentication.
#   - ``matches``    — Coinbase public docs list ``matches`` as part of
#                      the unauthenticated public WS feed alongside
#                      ``ticker``. Trade-flow features (``oft_*`` in
#                      evaluated_opportunities) derive from these ticks.
#   - ``heartbeat``  — server liveness frames; subscribing makes per-
#                      product gaps visible even when other channels
#                      are quiet.
#   - ``status``     — product online/offline transitions — rare frames,
#                      useful for outage forensics.
#
# **``level2_batch`` deliberately omitted at D2.1.5.** Coinbase has
# progressively gated some `level2` access since 2024; the public
# reachability of ``level2_batch`` on ``wss://ws-feed.exchange.coinbase.com``
# is not in-repo verified (the production bot only consumes ``ticker``).
# An in-archiver reachability check (observing Coinbase's ``type=error``
# response to an unauthorized subscribe before bronze goes silent on
# that channel) is the right place to verify-then-extend the default
# channel set. Adding ``level2_batch`` to the default set without
# verification risks shipping a silent partial-degradation failure
# mode that the wire library cannot detect on its own. A followup
# ticket promotes ``level2_batch`` into ``DEFAULT_CHANNELS`` once
# subscribe-success is verified.
#
# Coinbase Exchange WS has no ``candles`` channel; OHLC is derived from
# ``matches`` downstream.
DEFAULT_CHANNELS: Tuple[str, ...] = (
    "ticker",
    "matches",
    "heartbeat",
    "status",
)

# Default Coinbase product IDs — all 7 of the bot's live + T1-shadow
# assets per ``bot.constants.COINBASE_PRODUCTS`` (BTC/ETH/SOL/XRP/HYPE/
# DOGE/BNB). HYPE-USD verified live on Coinbase Exchange 2026-05-10;
# BNB-USD landed in main 2026-05-17 via the BNB T1-onboarding ship
# (PR #78, ticket 86b9zmj0c) with the verification "status=online +
# trading_disabled=false" on Coinbase Exchange. Duplicated here from
# constants to keep coinbase_wire-no-bot intact; the values must stay
# in sync with bot.constants.COINBASE_PRODUCTS.
DEFAULT_PRODUCT_IDS: Tuple[str, ...] = (
    "BTC-USD",
    "ETH-USD",
    "SOL-USD",
    "XRP-USD",
    "HYPE-USD",
    "DOGE-USD",
    "BNB-USD",
)


@dataclass
class Frame:
    """One Coinbase Exchange WS frame as observed at the wire.

    Per D0.3 §2 spec, ``wire_recv_ts`` is captured at frame ingress
    BEFORE ``json.loads(raw)``. This is the ONLY field that cannot be
    reconstructed from ``raw`` after the fact.

    Differences from ``kalshi_wire.ws_client.Frame``:
      - Coinbase Exchange WS has no top-level ``channel`` field;
        ``channel`` is left None at the wire layer. Consumers map
        ``msg_type`` → channel using their own dispatch table
        (``match`` → matches, ``ticker`` → ticker, ``heartbeat`` →
        heartbeat, ``status`` → status; ``snapshot`` / ``l2update``
        → level2_batch lands once that channel is promoted into the
        default set per the L99 ratchet pin).
      - ``sequence_num`` is populated from Coinbase's ``sequence`` field
        when present. Per-product monotonic (NOT per-connection like
        Kalshi). Wire-level gap detection is deferred to the consumer
        which holds per-product context.

    Fields:
        wire_recv_ts: Unix-epoch seconds with microsecond precision
            (``time.time()`` return value).
        raw: The full raw wire payload as a string. Bronze captures this
            verbatim — no decoding, no normalization.
        parsed: ``json.loads(raw)`` result. ``None`` if parse failed.
        channel: Always None on Coinbase Exchange WS (no top-level
            ``channel`` field in this protocol). Retained on the
            dataclass so consumers can populate it post-dispatch if
            they want a single object carrying both the wire payload
            and the consumer-resolved channel-name.
        msg_type: ``parsed["type"]`` if present, else None. The dispatch
            key on Exchange WS — every frame carries a type identifier.
        sequence_num: ``parsed["sequence"]`` if present and int, else
            None. Per-product monotonic; consumer joins with
            ``parsed["product_id"]`` for gap detection.
    """

    wire_recv_ts: float
    raw: str
    parsed: Optional[Dict[str, Any]]
    channel: Optional[str]
    msg_type: Optional[str]
    sequence_num: Optional[int]


def build_envelope(
    raw: str,
    *,
    source: str,
    channel: Optional[str],
    conn: Optional[str],
    collector_seq: int,
    wire_recv_ts: Optional[_dt.datetime] = None,
) -> Dict[str, Any]:
    """Construct the D0.3 §2 6-field bronze envelope for a Coinbase frame.

    Identical contract to ``kalshi_wire.ws_client.build_envelope`` — the
    envelope IS the bronze contract per D0.3 §2 and applies symmetrically
    across wire sources. Silver ETL dispatches on these keys; downstream
    readers depend on the shape staying stable.

    **DO NOT add fields here** (per D0.3 §2: bronze is immutable; schema-
    rev lives at silver).

    Args:
        raw: The full raw wire payload as a string. NOT JSON-parsed —
            bronze captures bytes verbatim per the D0.3 §0 operator
            principle ("store all raw data, transform downstream with
            dbt").
        source: e.g. ``coinbase_ws``, ``coinbase_rest``. Used by silver
            ETL dispatch.
        channel: WS channel name. Coinbase Exchange WS frames don't
            carry a top-level ``channel`` field, so the consumer maps
            ``Frame.msg_type`` → channel and passes the mapped value
            here. Use None for REST snapshots.
        conn: WS connection id (A/B/C/... if Coinbase ever needs multi-
            connection sharding) or None for REST snapshots. Lets silver
            QA detect single-conn outages without joining a separate
            health log.
        collector_seq: Monotone-increasing per-collector-process sequence
            from boot. Distinct from Coinbase's per-product ``sequence``
            field (which lives inside ``_raw``) — this counter survives
            any wire-side sequence resets.
        wire_recv_ts: Override for the receipt timestamp; default None
            samples ``datetime.now(UTC)`` at call time.

    Returns:
        A dict with the 6 reserved fields in the spec-required order:
        ``_wire_recv_ts``, ``_source``, ``_conn``, ``_channel``,
        ``_collector_seq``, ``_raw``. JSON-serializable as a single line.
    """
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
    """Coinbase Exchange WS transport client.

    Consumers (``collector/coinbase_archiver.py`` SHIPPED at D2.2,
    post-D2.3 refactored ``bot/feeds/coinbase.py``) own state —
    orderbook caches, schema probes, blacklists. This client owns transport: connect,
    reconnect, public subscribe, silence watchdog, frame parse, thread-
    safe send queue.

    Threading model (mirrors kalshi_wire's pattern):
      - ``start()`` spawns one daemon thread that runs an asyncio event
        loop. All ws.send / ws.recv happen on that thread.
      - All callbacks run on the asyncio thread; consumers must treat
        them as if they hold no GIL preference.
      - ``send_frame(payload)`` is thread-safe via
        ``loop.call_soon_threadsafe``.
      - ``request_reconnect()`` is thread-safe; the silence watchdog
        observes the flag.
      - ``stop()`` is thread-safe; signals the asyncio stop_event.

    Reconnect cleanup (R3/P0-A — preserved from kalshi_wire):
      - On exception: ``on_session_end()`` fires BEFORE the backoff sleep
      - On graceful close: ``on_session_end()`` fires BEFORE the next
        loop iteration
      - This ensures the consumer's per-session caches are cleared while
        ``is_connected`` reads False, so downstream paths see no-data
        instead of stale state.
    """

    def __init__(
        self,
        *,
        on_frame: Callable[[Frame], None],
        url: str = DEFAULT_WS_URL,
        channels: Tuple[str, ...] = DEFAULT_CHANNELS,
        product_ids: Tuple[str, ...] = DEFAULT_PRODUCT_IDS,
        on_session_start: Optional[Callable[[], None]] = None,
        on_session_end: Optional[Callable[[], None]] = None,
        on_drain_tick: Optional[Callable[[], None]] = None,
        silence_grace_s: float = 30.0,
        silence_timeout_s: float = 90.0,
        watchdog_check_interval: float = 15.0,
        drain_tick_interval_s: float = 2.0,
        ping_interval: float = 30.0,
        ping_timeout: float = 30.0,
        max_backoff_s: float = 60.0,
        ws_max_size: int = 16 * 1024 * 1024,
    ):
        # ws_max_size validation — D1.3-fu1 lesson carried forward.
        # Reject None (would disable the cap → unbounded memory),
        # non-int, sub-1 values. Same shape as kalshi_wire's validator
        # so the cross-library contract is consistent.
        if not isinstance(ws_max_size, int) or isinstance(ws_max_size, bool):
            raise TypeError(
                f"ws_max_size must be int (got "
                f"{type(ws_max_size).__name__}). The wire library declines "
                "None/non-int to keep incoming-frame memory bounded "
                "against accidental no-cap configuration."
            )
        if ws_max_size < 1:
            raise ValueError(
                f"ws_max_size must be ≥ 1 (got {ws_max_size})."
            )
        self._on_frame = on_frame
        self._url = url
        self._channels = tuple(channels)
        self._product_ids = tuple(product_ids)
        self._user_on_session_start = on_session_start
        self._on_session_end = on_session_end
        self._on_drain_tick = on_drain_tick
        self._silence_grace_s = silence_grace_s
        self._silence_timeout_s = silence_timeout_s
        self._watchdog_check_interval = watchdog_check_interval
        self._drain_tick_interval_s = drain_tick_interval_s
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._max_backoff_s = max_backoff_s
        self._ws_max_size = ws_max_size

        # Internal state — owned by the asyncio thread (except where
        # noted).
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._send_queue: Optional[asyncio.Queue] = None
        self._thread: Optional[threading.Thread] = None
        self._ws = None
        # Connection state — accessed from any thread; protected by
        # _state_lock. Independent of the consumer's own lock.
        self._state_lock = threading.Lock()
        self._connected_state = False
        self._connect_ts = 0.0
        self._last_msg_ts = 0.0
        self._force_reconnect_requested = False

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        """True iff the WS session is currently connected to Coinbase."""
        with self._state_lock:
            return self._connected_state

    def start(self) -> None:
        """Start the WS client — spawns the asyncio daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_thread, daemon=True)
        self._thread.start()

    def stop(self, *, join_timeout: float = 0.0) -> None:
        """Stop the WS client — signal the event loop, schedule a close
        of the active websocket, and (optionally) join the asyncio
        thread before returning.

        Args:
            join_timeout: when > 0, block up to this many seconds waiting
                for the asyncio daemon thread to exit. When 0 (default),
                the call is fire-and-forget — callbacks may still fire
                after this returns; callers that need deterministic
                shutdown (e.g., D2.2 ``CoinbaseArchiver.stop``) must
                pass a positive timeout to sequence
                ``wire.stop → drain → join``.

        Mirrors the D1.3-fu4 ``kalshi_wire.WSClient.stop`` contract
        (R2-M1): both stop_event AND ws.close are scheduled so the
        ``async for raw in ws`` read loop terminates promptly under
        WS quiescence.
        """
        if self._loop is not None and self._stop_event is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                # Loop already closed; nothing to signal.
                pass
            ws = self._ws
            loop = self._loop
            if ws is not None:
                def _schedule_close():
                    try:
                        loop.create_task(ws.close())
                    except Exception:
                        pass
                try:
                    loop.call_soon_threadsafe(_schedule_close)
                except RuntimeError:
                    pass
        if join_timeout > 0 and self._thread is not None:
            self._thread.join(timeout=join_timeout)

    def send_frame(self, payload: Dict[str, Any]) -> None:
        """Thread-safe enqueue of an outgoing WS frame.

        Callable from any thread. Validates synchronously:
          1. JSON-serializable (raises TypeError immediately)
          2. WS connected (raises ConnectionError immediately)
        — so callers' exception handlers fire on the SAME thread that
        invoked send_frame.

        Consumers own subscribe-message construction (typically via
        ``coinbase_wire.auth.build_public_subscribe_message``); this
        method just wraps the wire write.
        """
        json.dumps(payload)
        if not self.is_connected:
            raise ConnectionError(
                "coinbase_wire.WSClient.send_frame: WS not connected; "
                "frame dropped. Caller should clean up any per-frame "
                "state before retrying.")
        if self._loop is None or self._send_queue is None:
            raise ConnectionError(
                "coinbase_wire.WSClient.send_frame: event loop not "
                "ready; client not started or already stopped.")
        if self._loop.is_closed():
            raise ConnectionError(
                "coinbase_wire.WSClient.send_frame: event loop closed; "
                "client stopped.")
        try:
            self._loop.call_soon_threadsafe(
                self._send_queue.put_nowait, payload)
        except RuntimeError as exc:
            raise ConnectionError(
                "coinbase_wire.WSClient.send_frame: event loop closed "
                "between connectivity check and enqueue (race)."
            ) from exc

    def request_reconnect(self) -> None:
        """Thread-safe signal to force a WS reconnect.

        Used by consumers when the wire state is suspect (e.g., subscribe
        ack never arrived) and only a fresh WS session can recover it.
        The silence-watchdog observes the flag and force-closes the WS.
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
            logging.error("Coinbase wire thread crashed", exc_info=True)
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    def _default_on_session_start(self) -> None:
        """Dispatch a single batched subscribe covering all channels.

        Coinbase Exchange WS lets a single subscribe message carry an
        array of channel names — preferable to per-channel subscribes
        because (a) fewer wire round-trips, (b) matches the existing
        ``bot/feeds/coinbase.py`` shape, (c) sub-second total subscribe
        latency.
        """
        if not self._channels or not self._product_ids:
            return
        payload = build_public_subscribe_message(
            channels=list(self._channels),
            product_ids=list(self._product_ids),
        )
        try:
            self.send_frame(payload)
        except ConnectionError:
            # Race: WS closed between session_start invocation and
            # send_frame. Outer loop will reconnect; log + return.
            logging.warning(
                "coinbase_ws default-subscribe aborted (WS closed "
                "between callback fire and send_frame); reconnect "
                "will retry.")

    async def _ws_loop(self):
        backoff = 1.0

        while not self._stop_event.is_set():
            try:
                async with websockets.connect(
                    self._url,
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
                        self._force_reconnect_requested = False
                    backoff = 1.0
                    logging.info(f"coinbase_ws_connected: url={self._url}")

                    # Notify consumer (or run the default subscribe).
                    cb = self._user_on_session_start
                    if cb is None:
                        cb = self._default_on_session_start
                    try:
                        cb()
                    except Exception:
                        logging.warning(
                            "coinbase_ws on_session_start callback failed",
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
                                    "coinbase_ws send_frame ws.send FAILED",
                                    exc_info=True)
                    _send_drain_task = asyncio.create_task(_send_drain())

                    # Periodic drain-tick callback.
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
                                            "coinbase_ws on_drain_tick "
                                            "callback failed", exc_info=True)
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                logging.debug(
                                    "drain-tick iteration failed",
                                    exc_info=True)
                    _drain_tick_task = asyncio.create_task(_drain_tick_loop())

                    # Silence watchdog — close the ws so the outer
                    # reconnect kicks in if no frame arrives in
                    # silence_timeout_s.
                    async def _silence_watchdog():
                        while not self._stop_event.is_set():
                            try:
                                await asyncio.sleep(
                                    self._watchdog_check_interval)
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
                # are served as live data.
                self._on_session_end_safe()
                jitter = backoff * random.uniform(0, 0.25)
                wait = backoff + jitter
                logging.warning(
                    f"coinbase_ws_disconnected: reason={e} "
                    f"reconnect_backoff={wait:.1f}s")
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=wait)
                    break
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, self._max_backoff_s)
            else:
                # R3/P0-A graceful-close path (watchdog ws.close, server-
                # initiated close, async-with normal exit). Without this,
                # stale per-session caches would survive into the next
                # iteration.
                self._on_session_end_safe()

        with self._state_lock:
            self._connected_state = False
        self._ws = None
        logging.info("Coinbase wire stopped")

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
                    "coinbase_ws on_session_end callback failed",
                    exc_info=True)

    def _handle_raw_frame(self, raw: str) -> None:
        """Parse a raw WS frame, update wire-level watchdog state, and
        dispatch to the consumer's ``on_frame``.

        Watchdog ordering (Apr-24 silence-watchdog fix — load-bearing):
        ``_last_msg_ts`` is set BEFORE invoking the consumer callback.
        Any message from the server — even an error or unknown type we
        don't dispatch — proves the WS session is healthy.

        Coinbase Exchange WS frames carry no top-level ``channel`` field;
        ``Frame.channel`` is therefore None at the wire layer. The
        ``type`` field, when present and a string, becomes
        ``Frame.msg_type`` and serves as the consumer's dispatch key.
        A malformed frame missing ``type`` flows through with
        ``msg_type=None`` (defensive code; consumer can choose to drop
        or surface).
        """
        now = time.time()
        with self._state_lock:
            self._last_msg_ts = now

        parsed: Optional[Dict[str, Any]]
        msg_type: Optional[str]
        sequence_num: Optional[int]
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            parsed = None
            msg_type = None
            sequence_num = None
        else:
            if isinstance(parsed, dict):
                _t = parsed.get("type")
                msg_type = _t if isinstance(_t, str) else None
                _s = parsed.get("sequence")
                sequence_num = _s if isinstance(_s, int) else None
            else:
                msg_type = None
                sequence_num = None

        frame = Frame(
            wire_recv_ts=now,
            raw=raw,
            parsed=parsed if isinstance(parsed, dict) else None,
            channel=None,
            msg_type=msg_type,
            sequence_num=sequence_num,
        )
        try:
            self._on_frame(frame)
        except Exception:
            logging.warning(
                "coinbase_ws on_frame callback failed", exc_info=True)
