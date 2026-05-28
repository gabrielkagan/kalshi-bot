"""Lean multi-venue L2 bronze recorder — B2a-1 (ticket 86ba1zf5j, 2026-05-28).

Plan: kb/decisions/b2-synthetic-rti-feed-plan.md ("B2a-1 architecture").

Opens Kraken + Bitstamp + Gemini PUBLIC L2 order-book WebSockets in a
single asyncio thread and archives the RAW frames verbatim to bronze.
Coinbase L2 bronze is ALREADY live via ``kalshi-coinbase-collector``
(``level2_batch`` channel); this recorder captures the three remaining
CFB-constituent venues so the B2a-2 offline RMSE harness can reconstruct
a 4-venue consolidated order book.

WHY LEAN (decided 2026-05-28): the codebase pattern is per-venue WIRE
LIBRARIES (``kalshi_wire`` / ``coinbase_wire``) shared by collector + bot.
Applying that here would mean building three new wire packages BEFORE the
RMSE hypothesis is validated — contradicting the bronze-first de-risking
logic. Instead this mirrors the SIMPLE asyncio + reconnect/backoff shape of
``bot/feeds/cross_exchange.py`` and reuses the existing
``collector/writer.py`` + ``kalshi_wire.build_envelope``. If RMSE fails the
recorder is discarded cheaply; if it passes, B2b builds the proper wire
libraries (then justified by data + reused by the live bot feeds).

NO BOOK MAINTENANCE here — raw frames only. The recorder parses each frame
ONLY enough to route it (which venue writer) and to skip non-data control
frames (subscription acks / heartbeats / status); book reconstruction
(snapshot + apply-diffs) is the harness's job. This mirrors how the
CoinbaseArchiver reads ``msg_type`` to dispatch + skips ack frames
(D1.3-fu5) while still archiving the raw payload.

Bronze layout — per-venue SOURCE, per-venue native L2 CHANNEL:

    bronze/kraken_ws/book/year=.../.../conn=A/<chunk>.jsonl.zst
    bronze/bitstamp_ws/order_book/year=.../.../conn=A/<chunk>.jsonl.zst
    bronze/gemini_ws/l2/year=.../.../conn=A/<chunk>.jsonl.zst

All of a venue's subscribed assets share ONE channel; the asset is
disambiguated by the symbol field inside the raw frame — exactly mirrors
``coinbase_ws/level2_batch`` (all 7 products in one channel).

WRITE MODEL: writes are SYNCHRONOUS on the asyncio thread (faithful to
cross_exchange; the lean recorder does NOT need the bounded-queue
worker-thread decouple the live coinbase path has). The owning main loop
gives each writer a reduced ``size_threshold_bytes`` so the per-rotation
zstd compress stays short and cannot stall the other venues' keepalive for
multiple seconds. Bronze tolerates gaps (the harness skips incomplete 60s
windows), so an occasional rotation-induced reconnect is acceptable.

Verified WS protocols (live-probed 2026-05-28; see plan doc):
  - Bitstamp ``wss://ws.bitstamp.net`` — per-pair ``order_book_<pair>``
    subscribe; every data frame is a FULL top-100 snapshot.
  - Kraken v2 ``wss://ws.kraken.com/v2`` — ``book`` channel; ``snapshot``
    then ``update`` diff frames (+ checksum, verified in the harness).
  - Gemini v2 ``wss://api.gemini.com/v2/marketdata`` — ``l2`` subscribe;
    first ``l2_updates`` is the full snapshot, then incremental diffs.

NO ``bot.*`` imports (collector-no-bot import-linter contract; pinned by
tests/contracts/test_collector_no_bot_imports.py which AST-walks every
collector/*.py).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import random
import signal
import threading
from typing import Callable, Dict, List, Mapping, Optional, Sequence

import websockets

from kalshi_wire.ws_client import build_envelope

logger = logging.getLogger(__name__)

# ── Venue map ───────────────────────────────────────────────────────────

VENUES = ("kraken", "bitstamp", "gemini")

# Per-venue bronze source slug (D0.3 §1 — one source per provider).
VENUE_SOURCES: Mapping[str, str] = {
    "kraken": "kraken_ws",
    "bitstamp": "bitstamp_ws",
    "gemini": "gemini_ws",
}

# Per-venue native L2 channel name (one channel per venue, all assets
# mixed; mirror coinbase_ws/level2_batch).
VENUE_CHANNELS: Mapping[str, str] = {
    "kraken": "book",
    "bitstamp": "order_book",
    "gemini": "l2",
}

# asset -> per-venue wire symbol. A key is ABSENT when CFB does NOT use
# that venue for the asset's settling index (even if the venue lists the
# pair) — adding a non-constituent venue would bias the synthetic AWAY
# from the settlement reference. Collector-local (no bot.* import); the
# Kraken DOGE symbol mirrors bot.constants.CROSS_EXCHANGE_SYMBOLS["DOGE"]
# ["kraken"] = "XDG/USD".
VENUE_SYMBOLS: Mapping[str, Mapping[str, str]] = {
    "kraken": {
        "BTC": "BTC/USD",
        "ETH": "ETH/USD",
        "SOL": "SOL/USD",
        "XRP": "XRP/USD",
        "DOGE": "XDG/USD",
        "BNB": "BNB/USD",
        "HYPE": "HYPE/USD",
    },
    "bitstamp": {
        "BTC": "btcusd",
        "ETH": "ethusd",
        "SOL": "solusd",
        "XRP": "xrpusd",
        "HYPE": "hypeusd",
    },
    "gemini": {
        "BTC": "BTCUSD",
        "ETH": "ETHUSD",
        "SOL": "SOLUSD",
        "DOGE": "DOGEUSD",
    },
}

# Verified public L2 WS endpoints (live-probed 2026-05-28).
KRAKEN_L2_WS_URL = "wss://ws.kraken.com/v2"
BITSTAMP_WS_URL = "wss://ws.bitstamp.net"
GEMINI_L2_WS_URL = "wss://api.gemini.com/v2/marketdata"

# Kraken v2 book depth. 100 levels gives the harness ample near-top depth
# for the synthetic's utilized-depth window (CFB only weights depth within
# ~0.5-1% of mid). Valid Kraken values: 10/25/100/500/1000.
KRAKEN_BOOK_DEPTH = 100

# Conn id segment in the bronze partition. Single conn PER VENUE (the
# source slug already distinguishes venues); mirror coinbase conn=A.
_CONN_ID = "A"

# Reconnect backoff ceiling (seconds). Mirror cross_exchange.
_RECONNECT_BACKOFF_MAX = 60.0

# Bound on stop()'s join wait for the asyncio reader thread. Long enough
# for cancel-on-stop to unwind 3 venue coroutines + close their WS conns;
# short enough that a wedged read can't block process shutdown forever.
_STOP_JOIN_TIMEOUT = 10.0

# Journal marker emitted on every venue disconnect. The collector health
# monitor's check_ws_reconnects filters journal lines by this substring;
# it MUST differ from the Kalshi (kalshi_ws_disconnected) and Coinbase
# (coinbase_ws_disconnected) markers so the venue-l2 reconnect-storm alert
# does not cross-match the other tiers.
DISCONNECT_LOG_MARKER = "venue_l2_ws_disconnected"


# ── Subscribe payload builders (verified 2026-05-28) ──────────────────────


def build_kraken_subscribe(symbols: Sequence[str], depth: int) -> dict:
    """Kraken v2 book subscribe — one message covers all symbols."""
    return {
        "method": "subscribe",
        "params": {
            "channel": "book",
            "symbol": list(symbols),
            "depth": depth,
        },
    }


def build_bitstamp_subscribe(pair: str) -> dict:
    """Bitstamp order_book subscribe — ONE message PER pair."""
    return {
        "event": "bts:subscribe",
        "data": {"channel": f"order_book_{pair}"},
    }


def build_gemini_subscribe(symbols: Sequence[str]) -> dict:
    """Gemini v2 marketdata l2 subscribe — one message covers all symbols."""
    return {
        "type": "subscribe",
        "subscriptions": [{"name": "l2", "symbols": list(symbols)}],
    }


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class VenueL2Archiver:
    """Single-thread, multi-venue lean L2 WS bronze recorder.

    Owner (``collector/venue_l2_main_loop.py``) constructs one archiver
    with a ``writers_by_venue`` dict (one BronzeWriter per venue, each
    scoped to its (source, channel, conn=A) partition) and drives the
    lifecycle via start()/stop() or run().
    """

    def __init__(
        self,
        *,
        writers_by_venue: Mapping[str, object],
        venues: Sequence[str] = VENUES,
        assets: Optional[Sequence[str]] = None,
        urls: Optional[Mapping[str, str]] = None,
        now_fn: Callable[[], _dt.datetime] = _utc_now,
    ) -> None:
        self._writers_by_venue: Dict[str, object] = dict(writers_by_venue)
        # Only run coroutines for venues that BOTH appear in `venues` AND
        # have a writer (lets tests / partial configs spin up a subset).
        self._venues = tuple(v for v in venues if v in self._writers_by_venue)
        self._assets = tuple(assets) if assets is not None else None
        self._urls: Dict[str, str] = {
            "kraken": KRAKEN_L2_WS_URL,
            "bitstamp": BITSTAMP_WS_URL,
            "gemini": GEMINI_L2_WS_URL,
        }
        if urls:
            self._urls.update(urls)
        self._now_fn = now_fn

        self._lock = threading.Lock()
        self._collector_seq = 0
        self._connected: Dict[str, bool] = {v: False for v in self._venues}

        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        # Plain GIL-atomic flag so a stop() that races the reader thread's
        # event-loop creation still takes effect (the thread checks it right
        # after constructing self._stop_event).
        self._stop_requested = False

    # ── Symbol resolution ────────────────────────────────────────────────

    def _symbols_for(self, venue: str) -> List[str]:
        """Wire symbols this archiver subscribes for ``venue``.

        Defaults to every asset the venue lists in VENUE_SYMBOLS; when
        ``assets`` was supplied at construction, intersect (preserving the
        venue's asset order)."""
        venue_map = VENUE_SYMBOLS.get(venue, {})
        if self._assets is None:
            return list(venue_map.values())
        return [venue_map[a] for a in venue_map if a in self._assets]

    # ── Health snapshot (cross-collector observability) ──────────────────

    def get_health_snapshot(self) -> Dict[str, object]:
        """Per-archiver health dict mirroring the BronzeArchiver schema so
        ``collector/venue_l2_main_loop.write_bronze_health_sidecar`` +
        the cron health monitor consume it unmodified. The lean recorder
        has no bounded worker queue, so the queue/drop fields are 0 and
        ``write_worker_alive`` reflects the asyncio read thread."""
        thread = self._thread
        with self._lock:
            seq = self._collector_seq
        return {
            "conn_id": _CONN_ID,
            "dropped_frames": 0,
            "write_queue_size": 0,
            "write_queue_maxsize": 0,
            "write_worker_alive": bool(thread is not None and thread.is_alive()),
            "collector_seq": seq,
            "ack_frames_processed": 0,
        }

    # ── Raw-frame archival ───────────────────────────────────────────────

    def _archive(self, venue: str, raw: str) -> None:
        """Wrap one raw frame in the 6-field bronze envelope + write.

        Synchronous on the asyncio thread (see module docstring). Per-frame
        exceptions are swallowed + logged so one bad frame / disk hiccup
        cannot kill the read loop — silent total loss of bronze would be
        far worse than a single dropped frame."""
        writer = self._writers_by_venue.get(venue)
        if writer is None:
            return
        with self._lock:
            self._collector_seq += 1
            seq = self._collector_seq
        wire_recv_ts = self._now_fn()
        try:
            envelope = build_envelope(
                raw=raw,
                source=VENUE_SOURCES[venue],
                channel=VENUE_CHANNELS[venue],
                conn=_CONN_ID,
                collector_seq=seq,
                wire_recv_ts=wire_recv_ts,
            )
            writer.write(envelope, wire_recv_ts=wire_recv_ts)
        except Exception:
            logger.warning(
                "VenueL2Archiver write failed (venue=%s seq=%d)",
                venue, seq, exc_info=True,
            )

    # ── Per-venue frame routing (parse-to-route only; archive RAW) ────────

    def _handle_kraken(self, raw: str) -> None:
        """Archive Kraken ``book`` data frames (snapshot + update); skip
        subscribe responses / heartbeat / status."""
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if isinstance(msg, dict) and msg.get("channel") == "book":
            self._archive("kraken", raw)

    def _handle_bitstamp(self, raw: str) -> None:
        """Archive Bitstamp ``event=="data"`` frames (full snapshots); skip
        bts:subscription_succeeded / bts:request_reconnect / etc."""
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if isinstance(msg, dict) and msg.get("event") == "data":
            self._archive("bitstamp", raw)

    def _handle_gemini(self, raw: str) -> None:
        """Archive Gemini ``type=="l2_updates"`` frames (first = full
        snapshot, then diffs); skip heartbeat / subscription_ack / trade /
        auction frames."""
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if isinstance(msg, dict) and msg.get("type") == "l2_updates":
            self._archive("gemini", raw)

    # ── Background asyncio thread ─────────────────────────────────────────

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run_thread,
            name="venue-l2-ws-reader",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal shutdown and JOIN the reader thread before returning.

        Joining here is the load-bearing invariant: the owning main loop
        calls ``stop()`` then ``writer.close()`` on the main thread, so the
        reader thread (which calls ``writer.write()`` synchronously) MUST be
        quiesced before ``stop()`` returns — otherwise the two threads race
        on the same BronzeWriter file handle + in-flight state at the close.
        Mirrors ``CoinbaseArchiver.stop()``'s producer-quiesce-before-close
        guarantee (which the lean recorder dropped pre-R1 of the B2a gate).

        Sets the plain ``_stop_requested`` flag FIRST so a stop() that races
        the reader thread's event-loop construction still takes effect, then
        schedules the asyncio Event set (which ``_run`` waits on + cancels
        the venue coroutines from — so the thread exits promptly even when a
        venue WS is silent mid-``recv``)."""
        self._stop_requested = True
        loop, ev = self._loop, self._stop_event
        if loop is not None and ev is not None:
            loop.call_soon_threadsafe(ev.set)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=_STOP_JOIN_TIMEOUT)
            if thread.is_alive():
                logger.warning(
                    "VenueL2Archiver reader thread did not exit within %.0fs "
                    "of stop signal — abandoning (daemon thread killed at "
                    "process exit). A graceful writer.close() may race a "
                    "still-live write.",
                    _STOP_JOIN_TIMEOUT,
                )

    def _run_thread(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        # Close the start/stop race: if stop() already fired before the loop
        # existed, honor it immediately.
        if self._stop_requested:
            self._stop_event.set()
        try:
            self._loop.run_until_complete(self._run())
        except Exception:
            logger.error("VenueL2Archiver thread crashed", exc_info=True)
        finally:
            self._loop.close()

    async def _run(self) -> None:
        coros = []
        if "kraken" in self._venues:
            coros.append(self._ws_kraken())
        if "bitstamp" in self._venues:
            coros.append(self._ws_bitstamp())
        if "gemini" in self._venues:
            coros.append(self._ws_gemini())
        if not coros:
            logger.warning("VenueL2Archiver started with no venues to run")
            return
        # Race the venue coroutines against the stop signal. On stop, CANCEL
        # the venue tasks so a coroutine blocked in `await ws.recv()` (silent
        # WS) unwinds promptly — each `_ws_*` catches CancelledError + breaks.
        # Without this, the `async for` only re-checks the stop flag AFTER the
        # next frame arrives, so stop()'s join could hang on a quiet venue.
        tasks = [asyncio.ensure_future(c) for c in coros]
        stop_task = asyncio.ensure_future(self._stop_event.wait())
        try:
            await asyncio.wait(
                set(tasks) | {stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in tasks:
                t.cancel()
            stop_task.cancel()
            await asyncio.gather(*tasks, stop_task, return_exceptions=True)

    async def _sleep_or_stop(self, wait: float) -> bool:
        """Sleep ``wait`` seconds OR return early if stop was signaled.

        Returns True if stop was signaled (caller should break)."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=wait)
            return True
        except asyncio.TimeoutError:
            return False

    async def _ws_kraken(self) -> None:
        symbols = self._symbols_for("kraken")
        url = self._urls["kraken"]
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(url) as ws:
                    await ws.send(json.dumps(
                        build_kraken_subscribe(symbols, KRAKEN_BOOK_DEPTH)
                    ))
                    self._connected["kraken"] = True
                    backoff = 1.0
                    logger.info("Kraken L2 feed connected (%d symbols)", len(symbols))
                    async for raw in ws:
                        if self._stop_event.is_set():
                            break
                        self._handle_kraken(raw)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected["kraken"] = False
                wait = backoff + backoff * random.uniform(0, 0.25)
                logger.warning(
                    "%s venue=kraken: %s — reconnecting in %.1fs",
                    DISCONNECT_LOG_MARKER, e, wait,
                )
                if await self._sleep_or_stop(wait):
                    break
                backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)
        self._connected["kraken"] = False

    async def _ws_bitstamp(self) -> None:
        symbols = self._symbols_for("bitstamp")
        url = self._urls["bitstamp"]
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(url) as ws:
                    for pair in symbols:
                        await ws.send(json.dumps(build_bitstamp_subscribe(pair)))
                    self._connected["bitstamp"] = True
                    backoff = 1.0
                    logger.info("Bitstamp L2 feed connected (%d pairs)", len(symbols))
                    async for raw in ws:
                        if self._stop_event.is_set():
                            break
                        self._handle_bitstamp(raw)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected["bitstamp"] = False
                wait = backoff + backoff * random.uniform(0, 0.25)
                logger.warning(
                    "%s venue=bitstamp: %s — reconnecting in %.1fs",
                    DISCONNECT_LOG_MARKER, e, wait,
                )
                if await self._sleep_or_stop(wait):
                    break
                backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)
        self._connected["bitstamp"] = False

    async def _ws_gemini(self) -> None:
        symbols = self._symbols_for("gemini")
        url = self._urls["gemini"]
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(url) as ws:
                    await ws.send(json.dumps(build_gemini_subscribe(symbols)))
                    self._connected["gemini"] = True
                    backoff = 1.0
                    logger.info("Gemini L2 feed connected (%d symbols)", len(symbols))
                    async for raw in ws:
                        if self._stop_event.is_set():
                            break
                        self._handle_gemini(raw)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected["gemini"] = False
                wait = backoff + backoff * random.uniform(0, 0.25)
                logger.warning(
                    "%s venue=gemini: %s — reconnecting in %.1fs",
                    DISCONNECT_LOG_MARKER, e, wait,
                )
                if await self._sleep_or_stop(wait):
                    break
                backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)
        self._connected["gemini"] = False

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def run(self, shutdown_event: Optional[threading.Event] = None) -> None:
        """Start the reader thread and block until shutdown is signaled.

        If ``shutdown_event`` is None, install SIGINT/SIGTERM handlers on
        the (assumed-main) thread + wait on an internal Event. Tests pass a
        controlled event to avoid signal-handler pollution. Mirrors
        CoinbaseArchiver.run."""
        owned = shutdown_event is None
        if owned:
            shutdown_event = threading.Event()
            try:
                signal.signal(signal.SIGINT, lambda *_: shutdown_event.set())
                signal.signal(signal.SIGTERM, lambda *_: shutdown_event.set())
            except ValueError:
                logger.warning(
                    "VenueL2Archiver.run(): could not install signal handlers "
                    "(non-main thread); caller must drive shutdown via the event."
                )
        self.start()
        try:
            shutdown_event.wait()
        finally:
            # stop() joins the reader thread internally (producer-quiesce
            # invariant), so no separate join is needed here.
            self.stop()
