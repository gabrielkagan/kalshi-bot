"""CoinbaseFeed — Coinbase WS feed consumer with persistent 30-min spot buffer.

Extracted from bot/_impl.py in Sprint 4 Bit 4.5a (2026-05-08). Daemon
thread that maintains per-asset spot prices for every symbol in
``bot.config.ASSETS`` (BTC, ETH, SOL, XRP, HYPE, DOGE post-T1 2026-05-10;
BNB post-T1 2026-05-17 ticket 86b9zmj0c; ADA + BCH post-T1 2026-05-30 (15M
shadow) — ASSETS is the canonical source;
see kb/decisions/asset-onboarding-doge-hype-bit-1-shipped-may10.md +
bit-1-5-shipped-may10.md + agent_docs/bnb-t1-plan-may17.md). Maintains a
1-second-resolution rolling buffer (PRICE_BUFFER_SIZE), and persists the
buffer to disk every SPOT_BUFFER_PERSIST_INTERVAL_S so 30-min momentum
features (5m/30m used by cal_mlp) don't go NULL on restart.

**D2.3 (2026-05-17, ticket 86b9zkppt)** refactored the WS transport spine
out of this class into ``coinbase_wire.ws_client.WSClient`` per the
2026-05-16 §5 AMENDMENT to ``kb/decisions/data-corpus-architecture.md``
("two sides of the same coin" — bot + collector share one transport).
CoinbaseFeed is now a WSClient consumer: it instantiates one ``WSClient``
wire instance, hands it 3 sync callbacks (``_on_session_start`` /
``_on_frame`` / ``_on_session_end``), and keeps ALL bot-state machinery
— price dict, rolling 1-second buffer, 30-min persistence. The asyncio
event loop, ``websockets.connect``, exponential-backoff reconnect,
silence watchdog, and frame parse live in WSClient.

Sister class ``KalshiFeed`` (``bot/feeds/kalshi.py``) shipped the
parallel kalshi_wire-consumer refactor at D1.1.5 (PR ~30, ticket
86b9zdhz2, 2026-05-16); D2.3 brings the Coinbase side into the same
shape so both feeds + both collector archivers route through the
parallel wire libraries.

Subscribe scope: bot subscribes to ``channels=("ticker",)`` only — the
wire library's post-D2.5 ``DEFAULT_CHANNELS`` defaults to a 5-channel
set (ticker + matches + heartbeat + status + level2_batch) that the
collector uses for bronze archiving. The bot only needs spot price;
matches/heartbeat/status frames would be CPU + GIL noise on the bot
side, and the post-D2.5 level2_batch firehose (~17 l2update/sec per
product × 7 products) would dwarf the ticker rate. Narrowing here
keeps the bot path lean while the collector keeps the wide set.

Threading model (post-D2.3):

  - ``WSClient`` (asyncio thread, owned by coinbase_wire) — websocket
    connect/reconnect, silence watchdog, frame parse, callback dispatch.
  - Sampler daemon thread (owned by CoinbaseFeed) — every 1s, snapshot
    ``self._prices`` into ``self._buffers``; every
    SPOT_BUFFER_PERSIST_INTERVAL_S, persist buffer to disk.

The sampler thread replaces the pre-D2.3 asyncio ``_snapshot_loop``
coroutine since WSClient now owns the asyncio event loop and we cannot
``asyncio.gather`` from outside it. Disk I/O lives on the sampler
thread (not the wire's asyncio thread) so persist failures + slow disk
cannot block the WS keepalive ping cycle.

Imports are deliberate: stdlib + ``bot.constants`` (5 explicit names) +
``ASSETS`` from ``bot/config.py`` (left there in Bit 3.1 because it's
used by models.py + tests outside the bot package) + ``coinbase_wire``
(WSClient + Frame + build_public_subscribe_message). Does NOT import
``asyncio`` / ``websockets`` / ``random`` (those live in the wire) and
does NOT import ``bot._impl`` (deleted in Bit 9.3-iii.c).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from typing import Dict, List, Optional, Tuple

from bot.constants import (
    COINBASE_PRODUCTS,
    COINBASE_WS_URL,
    PRICE_BUFFER_SIZE,
    SPOT_BUFFER_PERSIST_INTERVAL_S,
    SPOT_BUFFER_PERSIST_PATH,
)
from bot.config import ASSETS
from coinbase_wire.auth import build_public_subscribe_message
from coinbase_wire.ws_client import Frame, WSClient


# Bot subscribes to ticker channel only — narrower than the wire's
# post-D2.5 DEFAULT_CHANNELS (ticker + matches + heartbeat + status +
# level2_batch). The bot only consumes spot price; the other 4
# channels would be CPU/GIL noise (level2_batch in particular is high-
# volume orderbook updates that aren't part of any bot decision path).
# The collector (D2.2 + D2.5 level2_batch promotion) uses the wider
# set for bronze archiving.
_BOT_CHANNELS: Tuple[str, ...] = ("ticker",)


class CoinbaseFeed:
    """Coinbase WebSocket feed consumer for real-time crypto prices.

    Post-D2.3 wraps ``coinbase_wire.ws_client.WSClient`` with 3 sync
    callbacks (``_on_session_start`` / ``_on_frame`` / ``_on_session_end``).
    All consumer state (price dict, rolling 30-min buffer, periodic
    persistence) stays on this class. WSClient owns the asyncio thread,
    connect/reconnect, silence watchdog, and frame parse; the bot owns
    a separate sampler daemon thread for the 1-second snapshot cadence.
    """

    def __init__(self, persist_path: str = SPOT_BUFFER_PERSIST_PATH):
        self._prices: Dict[str, float] = {}
        # Bit S.1 (86ba1wrcg, 2026-05-21): per-asset monotonic timestamp of
        # the last WS tick that wrote `_prices[asset]`. Time base is
        # `time.monotonic()` (no NTP/leap-second jumps; process-wide clock).
        # **Atomicity contract**: BOTH `_prices[asset] = price` AND
        # `_price_ts[asset] = time.monotonic()` are written under a single
        # `with self._lock:` acquisition in `_on_frame`. Readers via
        # `get_price_with_ts()` acquire the same lock so they see both
        # writes atomically or neither — staleness = `now - ts` is
        # well-defined. Empty until the first WS tick lands per asset.
        # Spot-staleness umbrella `86ba1wrad` instrumentation — no
        # behavior change, just observability for downstream S.3 gate.
        self._price_ts: Dict[str, float] = {}
        self._buffers: Dict[str, deque] = {
            asset: deque(maxlen=PRICE_BUFFER_SIZE) for asset in ASSETS
        }
        self._lock = threading.Lock()
        # Reverse lookup: "BTC-USD" -> "BTC"
        self._product_to_asset = {v: k for k, v in COINBASE_PRODUCTS.items()}
        # R-p7-deploy-r11: persist 30-min buffer across restarts so cal_mlp
        # 5m/30m momentum features don't go NULL post-deploy. See
        # kb/concepts/calibrator-data-hygiene-apr29.md.
        # _persist_lock is defensive — at D2.3 the only persist site is
        # the sampler thread (single-threaded) + the final flush in
        # stop() (after sampler join). The lock pins safety against a
        # future Bit that adds an external persist_buffer() invocation.
        self._persist_path = persist_path
        # Seed _last_persist_ts at wall-clock NOW so the first sampler
        # tick defers persist by the full SPOT_BUFFER_PERSIST_INTERVAL_S.
        # A 0.0 seed would trigger an immediate persist on the first
        # sample (since any epoch second ≫ 30s), overwriting the freshly
        # loaded persisted buffer with one fresh sample appended —
        # wasteful disk I/O on every boot for no benefit.
        self._last_persist_ts: float = time.time()
        self._persist_lock = threading.Lock()
        self._load_persisted_buffer()
        # D2.3: WS transport delegated to coinbase_wire.WSClient.
        # ``channels`` narrows the wire default to ticker only (bot
        # doesn't need matches/heartbeat/status/level2_batch — see
        # _BOT_CHANNELS constant docstring). product_ids comes from
        # bot.constants.COINBASE_PRODUCTS so a new-asset onboarding
        # flows naturally into the WS subscribe via the bot's existing
        # config surface.
        self._product_ids: Tuple[str, ...] = tuple(
            COINBASE_PRODUCTS.values())
        self._channels: Tuple[str, ...] = _BOT_CHANNELS
        self._wire = WSClient(
            on_frame=self._on_frame,
            on_session_start=self._on_session_start,
            on_session_end=self._on_session_end,
            url=COINBASE_WS_URL,
            channels=self._channels,
            product_ids=self._product_ids,
        )
        # D2.3: sampler runs in its own daemon thread (replaces the
        # pre-D2.3 asyncio _snapshot_loop coroutine). WSClient owns its
        # own asyncio loop; the sampler cannot share it via
        # asyncio.gather from outside.
        self._sampler_stop = threading.Event()
        self._sampler_thread: Optional[threading.Thread] = None

    def _load_persisted_buffer(self) -> None:
        """Restore buffers from `self._persist_path` if present. Drops
        entries older than PRICE_BUFFER_SIZE seconds AND future-dated
        entries beyond a 60s clock-skew tolerance (R2 fix: corrupt files
        or system-clock skew producing far-future timestamps would otherwise
        never age out of the deque). Silently no-ops on missing/corrupt
        file — bot must boot regardless of persistence state."""
        try:
            with open(self._persist_path, 'r') as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if not isinstance(data, dict):
            return
        now = time.time()
        cutoff_low = now - PRICE_BUFFER_SIZE
        cutoff_high = now + 60  # 60s clock-skew tolerance
        loaded = 0
        with self._lock:
            for asset, series in data.items():
                if asset not in self._buffers:
                    continue  # unknown asset (e.g., dropped from ASSETS)
                if not isinstance(series, list):
                    continue
                for entry in series:
                    if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                        continue
                    try:
                        ts = float(entry[0])
                        price = float(entry[1])
                    except (ValueError, TypeError):
                        continue
                    if ts < cutoff_low or ts > cutoff_high:
                        continue  # stale OR future-dated; both poison
                    self._buffers[asset].append((ts, price))
                    loaded += 1
        if loaded:
            logging.info(
                "CoinbaseFeed: restored %d buffer entries from %s",
                loaded, self._persist_path,
            )

    def persist_buffer(self) -> None:
        """Write current buffers to disk atomically (.tmp + os.replace).
        Called from the sampler thread every SPOT_BUFFER_PERSIST_INTERVAL_S
        and from `stop()` for shutdown-flush.

        The .tmp filename includes pid + uuid8 so two processes (test
        isolation, parallel deploys) don't race over a fixed name.

        R5 (HIGH): wrapped entirely in `_persist_lock` so concurrent
        callers serialize. Without this, two snapshots taken at
        different instants both call os.replace; the later-finishing
        thread wins regardless of which snapshot was fresher → silent
        data loss in the silent-WS shutdown path.
        """
        with self._persist_lock:
            # R-p7-deploy-r11 R2: ensure parent dir exists once-per-instance
            # rather than on every 30s call. Tracked via a flag.
            if not getattr(self, '_persist_dir_ready', False):
                try:
                    os.makedirs(
                        os.path.dirname(self._persist_path) or '.',
                        exist_ok=True)
                    self._persist_dir_ready = True
                except OSError:
                    return
            with self._lock:
                data = {
                    asset: [[ts, price] for ts, price in buf]
                    for asset, buf in self._buffers.items()
                }
            tmp = (f"{self._persist_path}.tmp-{os.getpid()}-"
                   f"{uuid.uuid4().hex[:8]}")
            try:
                with open(tmp, 'w') as f:
                    json.dump(data, f, separators=(',', ':'))
                os.replace(tmp, self._persist_path)
            except OSError as e:
                logging.warning(
                    "CoinbaseFeed: persist_buffer failed: %s", e,
                )
                # R-p7-deploy-r11 R3-M1: clear dir-ready flag so the next
                # persist re-attempts makedirs. Without this, a one-shot dir
                # deletion (sysadmin cleanup, container volume remount)
                # permanently breaks persistence until restart.
                self._persist_dir_ready = False
                # Best-effort cleanup of orphaned tmp on disk-full / mid-write fail.
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # ── Public API (called from main thread) ──────────────────────────────

    def start(self):
        """Start sampler thread, then the underlying WSClient.

        Order: sampler first so it's running before the first ticks
        arrive (no hard ordering requirement — sampler reads
        ``self._prices`` written by ``_on_frame``; an empty initial
        sample is harmless — but the precedent in D2.2's
        ``CoinbaseArchiver.start`` starts the worker before the wire
        for symmetry).

        Re-entrancy: if ``start()`` is called after a prior ``stop()``,
        a fresh sampler thread is spawned. WSClient handles its own
        re-entrancy on ``start()``.
        """
        if (self._sampler_thread is None
                or not self._sampler_thread.is_alive()):
            self._sampler_stop.clear()
            self._sampler_thread = threading.Thread(
                target=self._sampler_loop,
                name=f"CoinbaseFeed-sampler-{id(self):x}",
                daemon=True,
            )
            self._sampler_thread.start()
        self._wire.start()

    def stop(self):
        """Stop the WSClient (best-effort 2s join), signal sampler stop,
        join (2s), then do a final synchronous persist flush so the last
        30s of ticks survive restart.

        Ordering:
          1. ``self._wire.stop(join_timeout=2.0)`` — schedules the wire
             stop_event + ws.close. Under normal shutdown the asyncio
             thread breaks out of its frame loop, fires ``_on_session_end``
             (which sets ``_connected_state=False``), and exits well
             within 2s. Under wire-side backoff-sleep the 2s timeout
             may return before the asyncio thread has fully exited;
             that's acceptable here because the wire only mutates
             ``self._prices`` (under ``self._lock``) on the frame path,
             never ``self._buffers`` — the buffer surface flushed below.
             The wire's daemon thread continues running in the background
             until its current backoff sleep wakes and the stop_event
             check fires (next iteration breaks out); process-exit
             cleanup is the final backstop for the daemon thread.
          2. Signal + JOIN the sampler thread (2s timeout). The sampler
             is the only writer to ``self._buffers``, so joining it
             quiesces the persist-write set.
          3. Synchronous ``persist_buffer()`` final flush. Reads
             ``self._buffers`` under ``self._lock`` (safe regardless of
             whether the wire thread is still in flight).
        """
        try:
            self._wire.stop(join_timeout=2.0)
        except Exception:
            logging.warning("CoinbaseFeed _wire.stop failed", exc_info=True)
        self._sampler_stop.set()
        if self._sampler_thread is not None:
            self._sampler_thread.join(timeout=2.0)
        try:
            self.persist_buffer()
        except Exception:
            logging.warning(
                "persist_buffer in stop() failed", exc_info=True)

    def get_price(self, asset: str) -> Optional[float]:
        with self._lock:
            return self._prices.get(asset)

    def get_price_with_ts(self, asset: str) -> Optional[Tuple[float, float]]:
        """Return `(price, last_tick_monotonic_ts)` or None.

        Bit S.1 (86ba1wrcg). Lock-step read of `_prices[asset]` +
        `_price_ts[asset]`. Returns None when the asset has never
        received a WS tick. Both values land atomically in the WS
        frame handler under `self._lock`, so a non-None price implies
        a non-None timestamp.

        Spot staleness at evaluation = `time.monotonic() - ts`.
        """
        with self._lock:
            price = self._prices.get(asset)
            if price is None:
                return None
            ts = self._price_ts.get(asset)
            if ts is None:
                # Defensive: shouldn't happen given lock-step writes, but
                # honest-NULL the staleness rather than emit a fake ts.
                return None
            return (price, ts)

    def get_all_prices(self) -> Dict[str, Optional[float]]:
        with self._lock:
            return {a: self._prices.get(a) for a in ASSETS}

    def get_buffer(self, asset: str) -> List[Tuple[float, float]]:
        with self._lock:
            return list(self._buffers.get(asset, []))

    def get_price_trailing_avg(
            self, asset: str, seconds: int = 60) -> Optional[float]:
        """Return the average spot price over the last N seconds.

        Uses the 1-second snapshot buffer (PRICE_BUFFER_SIZE=300, 5 min of
        data). Returns None if fewer than 5 samples available (feed just
        started or reconnected). This approximates the CFB RTI 60-second
        settlement averaging mechanism.
        """
        now = time.time()
        with self._lock:
            buf = self._buffers.get(asset)
            if not buf:
                return None
            # Copy under lock to prevent "deque mutated during iteration"
            snapshot = list(buf)
        # Filter to entries within the time window
        cutoff = now - seconds
        prices = [price for ts, price in snapshot if ts >= cutoff]
        if len(prices) < 5:
            return None
        return sum(prices) / len(prices)

    @property
    def is_connected(self) -> bool:
        """Delegate to WSClient (D2.3). Pre-D2.3 this read a local
        ``_connected`` flag set by the now-deleted in-class WS loop."""
        return self._wire.is_connected

    # ── WSClient callbacks ────────────────────────────────────────────────

    def _on_session_start(self) -> None:
        """Fires AFTER WS connect, BEFORE WSClient starts reading frames.

        Builds the Coinbase Exchange WS subscribe payload via the public
        ``coinbase_wire.auth.build_public_subscribe_message`` helper and
        dispatches via the wire's public ``WSClient.send_frame`` API.

        R1-M1 D2.2 RCA carried forward: NOT reaching into
        ``self._wire._default_on_session_start()`` (a private wire-library
        method). A wire-side rename of the private method would silently
        strand the consumer: construction + import + start() all succeed,
        but at first WS connect ``AttributeError`` fires inside the wire's
        ``try: cb() except Exception: log.warning(...)`` swallower — no
        subscribe dispatches, no price ticks reach the bot, and only the
        90s silence-watchdog as the alert. Building via the public helper
        keeps the consumer-side subscribe path AST-discoverable and
        decoupled from wire-library internals.
        """
        if not self._channels or not self._product_ids:
            return
        payload = build_public_subscribe_message(
            channels=list(self._channels),
            product_ids=list(self._product_ids),
        )
        try:
            self._wire.send_frame(payload)
        except ConnectionError:
            logging.warning(
                "CoinbaseFeed subscribe dispatch aborted (WS closed "
                "between session_start fire and send_frame); reconnect "
                "will retry.")

    def _on_session_end(self) -> None:
        """Fires AFTER WS close, BEFORE the WSClient backoff sleep.

        CoinbaseFeed has no per-session state to clear: the rolling 30-min
        buffer is INTENTIONALLY cross-session (cal_mlp momentum features
        need continuity across WS blips — a forced clear on every
        reconnect would defeat the persistence design). The ``_prices``
        dict naturally re-populates on the next ticker tick; a brief
        silent-window read returns the last-known price, which is the
        right behavior for a momentary disconnect.

        Callback wired for R3/P0-A symmetry with the wire contract
        (mirrors ``CoinbaseArchiver._on_session_end`` and the kalshi-side
        ``KalshiFeed._on_session_end``) + as a future-extension seam.
        """
        return

    def _on_frame(self, frame: Frame) -> None:
        """Fires for every incoming WS frame. Watchdog ``_last_msg_ts``
        is already set by WSClient BEFORE this callback (Apr-24
        silence-watchdog ordering — carried over from the kalshi side).

        Filters to ``type=ticker``, extracts price for the mapped asset,
        writes to ``self._prices`` under ``self._lock``. Pre-D2.3 this
        was ``_handle_message`` invoked directly from ``async for raw
        in ws:``; the wire library now owns the JSON parse so this
        method consumes ``frame.parsed`` + ``frame.msg_type`` directly.

        Frames with unmapped ``product_id`` (e.g., a future product we
        didn't subscribe to but Coinbase echoed back) are silently
        dropped — the reverse lookup in ``self._product_to_asset``
        returns None and we early-exit.
        """
        if frame.msg_type != "ticker":
            return
        data = frame.parsed
        if data is None:  # JSON parse failed in the wire
            return
        product_id = data.get("product_id", "")
        price_str = data.get("price")
        asset = self._product_to_asset.get(product_id)
        if not asset or not price_str:
            return
        try:
            price = float(price_str)
        except (ValueError, TypeError):
            return
        with self._lock:
            self._prices[asset] = price
            self._price_ts[asset] = time.monotonic()

    # ── Sampler thread (1-second snapshot + periodic persist) ─────────────

    def _sampler_loop(self) -> None:
        """Daemon-thread version of the pre-D2.3 asyncio ``_snapshot_loop``.

        Every 1 second:
          1. Sample current ``self._prices`` into per-asset deques.

        Every SPOT_BUFFER_PERSIST_INTERVAL_S seconds:
          2. Persist buffer to disk (best-effort; failures non-fatal,
             retried on next pass).

        Lives on its own thread because WSClient owns its own asyncio
        loop and we cannot ``asyncio.gather`` from outside. Disk I/O
        on the sampler thread (not the wire's asyncio thread) ensures
        a slow disk + persist failure cannot block the WS keepalive
        ping-pong cycle (the lesson D1.3-fu4 closed for the collector
        side — same architectural principle applies here even though
        the bot doesn't need a full worker-thread queue).
        """
        while not self._sampler_stop.is_set():
            # Wait either 1s or until stop is set, whichever first.
            if self._sampler_stop.wait(timeout=1.0):
                break
            now = time.time()
            with self._lock:
                for asset, price in self._prices.items():
                    self._buffers[asset].append((now, price))
            if (now - self._last_persist_ts
                    >= SPOT_BUFFER_PERSIST_INTERVAL_S):
                self._last_persist_ts = now
                try:
                    self.persist_buffer()
                except Exception:
                    logging.warning(
                        "CoinbaseFeed sampler persist failed",
                        exc_info=True)
