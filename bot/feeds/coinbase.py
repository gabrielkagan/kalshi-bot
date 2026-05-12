"""CoinbaseFeed — Coinbase WS feed with persistent 30-min snapshot buffer.

Extracted from bot/_impl.py in Sprint 4 Bit 4.5a (2026-05-08). Daemon
thread that subscribes to Coinbase ticker channel for every symbol in
`bot.config.ASSETS` (BTC, ETH, SOL, XRP, HYPE, DOGE post-T1 2026-05-10 —
ASSETS is the canonical source; see kb/decisions/asset-onboarding-doge-hype-bit-1-shipped-may10.md
+ bit-1-5-shipped-may10.md). Maintains a 1-second-resolution rolling
buffer (PRICE_BUFFER_SIZE), and persists the buffer to disk every
SPOT_BUFFER_PERSIST_INTERVAL_S so 30-min momentum features (5m/30m
used by cal_mlp) don't go NULL on restart.

The module-level helper ``_swallow_persist_exception`` is the
done-callback for the off-loop persist task. It moved here from
bot/_impl.py because CoinbaseFeed is its sole consumer.

Imports are deliberate: stdlib + ``websockets`` + ``bot.constants``
(5 explicit names) + ``ASSETS`` from ``bot/config.py`` (left there in
Bit 3.1 because it's used by models.py + tests outside the bot
package). Does NOT import ``bot._impl`` (would create a circular
import — `_impl` imports this module).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import threading
import time
import uuid
from collections import deque
from typing import Dict, List, Optional, Tuple

import websockets

from bot.constants import (
    COINBASE_PRODUCTS,
    COINBASE_WS_URL,
    PRICE_BUFFER_SIZE,
    SPOT_BUFFER_PERSIST_INTERVAL_S,
    SPOT_BUFFER_PERSIST_PATH,
)
from bot.config import ASSETS


def _swallow_persist_exception(fut):
    """Done-callback for the off-loop persist task. Logs but doesn't
    propagate — a persist failure is non-fatal (next pass retries)."""
    exc = fut.exception()
    if exc is not None:
        logging.warning("persist_buffer (off-loop) failed: %s", exc)


class CoinbaseFeed:
    """Coinbase WebSocket feed for real-time crypto prices.

    Runs an asyncio event loop in a daemon thread. Shares price data with
    the synchronous main loop via a lock-protected dict and deque buffers.
    """

    def __init__(self, persist_path: str = SPOT_BUFFER_PERSIST_PATH):
        self._prices: Dict[str, float] = {}
        self._buffers: Dict[str, deque] = {
            asset: deque(maxlen=PRICE_BUFFER_SIZE) for asset in ASSETS
        }
        self._lock = threading.Lock()
        self._connected = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        # Reverse lookup: "BTC-USD" -> "BTC"
        self._product_to_asset = {v: k for k, v in COINBASE_PRODUCTS.items()}
        # R-p7-deploy-r11: persist 30-min buffer across restarts so cal_mlp
        # 5m/30m momentum features don't go NULL post-deploy. See
        # kb/concepts/calibrator-data-hygiene-apr29.md.
        # R5 (HIGH): persist_lock serializes concurrent persists between
        # stop() (main thread) and the asyncio to_thread executor.
        # Without this, two snapshots race on os.replace and the later-
        # finishing one wins regardless of which had fresher data.
        self._persist_path = persist_path
        self._last_persist_ts: float = 0.0
        self._persist_lock = threading.Lock()
        self._load_persisted_buffer()

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
        Called from `_snapshot_loop` every SPOT_BUFFER_PERSIST_INTERVAL_S
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
                    os.makedirs(os.path.dirname(self._persist_path) or '.', exist_ok=True)
                    self._persist_dir_ready = True
                except OSError:
                    return
            with self._lock:
                data = {
                    asset: [[ts, price] for ts, price in buf]
                    for asset, buf in self._buffers.items()
                }
            tmp = f"{self._persist_path}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
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
        self._thread = threading.Thread(target=self._run_thread, daemon=True)
        self._thread.start()

    def stop(self):
        # R-p7-deploy-r11 R2: flush persistent buffer on shutdown so
        # last 30s of ticks survive restart. R4 (MED): order matters —
        # we MUST set stop_event FIRST so _snapshot_loop won't schedule
        # another `to_thread(persist_buffer)` after our final flush.
        # Without the ordering, two concurrent persists race on os.replace
        # and the LATER-running thread wins, potentially writing a
        # SLIGHTLY OLDER snapshot. Set stop_event first, then briefly
        # wait for any in-flight to_thread, then do the final synchronous
        # flush.
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        # 250ms join + up to ~100ms _persist_lock contention + 5-10ms
        # final persist. Worst case (silent-WS path where thread.join
        # times out + asyncio thread is mid-persist holding the lock):
        # ~360ms total stop() latency. Well below any reasonable
        # TimeoutStopSec — but the unit file is outside this repo, so
        # operators should verify their TimeoutStopSec is ≥ 1s.
        if self._thread is not None:
            self._thread.join(timeout=0.25)
        try:
            self.persist_buffer()
        except Exception:
            logging.warning("persist_buffer in stop() failed", exc_info=True)

    def get_price(self, asset: str) -> Optional[float]:
        with self._lock:
            return self._prices.get(asset)

    def get_all_prices(self) -> Dict[str, Optional[float]]:
        with self._lock:
            return {a: self._prices.get(a) for a in ASSETS}

    def get_buffer(self, asset: str) -> List[Tuple[float, float]]:
        with self._lock:
            return list(self._buffers.get(asset, []))

    def get_price_trailing_avg(self, asset: str, seconds: int = 60) -> Optional[float]:
        """Return the average spot price over the last N seconds.

        Uses the 1-second snapshot buffer (PRICE_BUFFER_SIZE=300, 5 min of data).
        Returns None if fewer than 5 samples available (feed just started or
        reconnected). This approximates the CFB RTI 60-second settlement
        averaging mechanism.
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
        return self._connected

    # ── Background thread ─────────────────────────────────────────────────

    def _run_thread(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        try:
            self._loop.run_until_complete(self._run())
        except Exception:
            logging.error("Coinbase feed thread crashed", exc_info=True)
        finally:
            self._loop.close()

    async def _run(self):
        """Top-level coroutine: run WS listener and snapshot sampler."""
        await asyncio.gather(
            self._ws_loop(),
            self._snapshot_loop(),
        )

    # ── WebSocket connection with reconnect ───────────────────────────────

    async def _ws_loop(self):
        backoff = 1.0
        max_backoff = 60.0

        while not self._stop_event.is_set():
            try:
                async with websockets.connect(COINBASE_WS_URL) as ws:
                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "product_ids": list(COINBASE_PRODUCTS.values()),
                        "channels": ["ticker"],
                    }))
                    self._connected = True
                    backoff = 1.0  # reset on successful connect
                    logging.info("Coinbase feed connected")

                    async for raw in ws:
                        if self._stop_event.is_set():
                            break
                        self._handle_message(raw)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                jitter = backoff * random.uniform(0, 0.25)
                wait = backoff + jitter
                logging.warning(
                    f"Coinbase feed disconnected: {e} — "
                    f"reconnecting in {wait:.1f}s"
                )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=wait
                    )
                    break  # stop_event was set during wait
                except asyncio.TimeoutError:
                    pass  # timeout elapsed, retry
                backoff = min(backoff * 2, max_backoff)

        self._connected = False
        logging.info("Coinbase feed stopped")

    def _handle_message(self, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = data.get("type")
        if msg_type != "ticker":
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

    # ── 1-second snapshot sampler ─────────────────────────────────────────

    async def _snapshot_loop(self):
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=1.0
                )
                break  # stop_event was set
            except asyncio.TimeoutError:
                pass  # 1 second elapsed

            now = time.time()
            with self._lock:
                for asset, price in self._prices.items():
                    self._buffers[asset].append((now, price))

            # Persist every SPOT_BUFFER_PERSIST_INTERVAL_S (~30s).
            # R-p7-deploy-r11 R2: offload disk I/O to a thread — JSON
            # serialize + os.replace on a 1800×4 buffer is ~14k tuples
            # of synchronous I/O; doing it on the event-loop thread
            # would block WS message handling, dropping fresh price
            # ticks (the very thing we're trying to preserve).
            if now - self._last_persist_ts >= SPOT_BUFFER_PERSIST_INTERVAL_S:
                self._last_persist_ts = now
                # asyncio.to_thread (Py3.9+) drops the GIL during the
                # blocking call so the event loop keeps processing.
                # Don't await — fire-and-forget; if a write fails the
                # next pass will retry. Capture exception via callback
                # so unhandled-exception warnings don't fire.
                fut = asyncio.ensure_future(
                    asyncio.to_thread(self.persist_buffer)
                )
                fut.add_done_callback(_swallow_persist_exception)
