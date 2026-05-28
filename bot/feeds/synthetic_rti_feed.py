"""SyntheticRTIFeed — in-bot multi-venue L2 -> synthetic RTI (B2b-1, SHADOW-only).

Ticket 86ba64h2w (program 86ba64gyq). Plan: kb/decisions/b2b-1-core-shadow-plan.md.

Maintains per-(venue, asset) L2 order books from Coinbase / Kraken / Bitstamp /
Gemini WS frames and computes the CFB-shape synthetic RTI via the validated
``bot.feeds.synthetic_rti.compute_synthetic_rti`` aggregator.

SHADOW-only: this feed only COMPUTES + exposes the synthetic for logging — it
feeds NO trade decision in B2b-1. The ``enabled`` kill-switch (default False)
gates the compute.

Bot-side lean parsers (NOT a ``collector`` import) — per D0.3 isolation,
duplicate the minimum rather than couple bot -> collector. The Kraken v2 CRC32
desync-drop is ported from the 86ba5xfyb harness verifier: a snapshot/update
whose recomputed top-10 checksum disagrees with the frame's ``checksum`` marks
the (venue, asset) book desynced and excludes it until a fresh in-sync snapshot.

INVARIANT: Kraken frames must carry price/qty as STRINGS (the live WS handler
parses with ``parse_float=str``) so the checksum tokens preserve precision —
``"100.10"`` must stay ``"100.10"``, not become ``100.1``.

numpy-only via ``compute_synthetic_rti``; no torch/pandas (import-linter pinned).
The live WS thread + scanner wiring + ``evaluated_opportunities`` schema land in
later steps of B2b-1; this module is the offline-testable core.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time
import zlib
from typing import Dict, List, Optional, Sequence, Tuple

import websockets

from bot.feeds.synthetic_rti import compute_synthetic_rti

logger = logging.getLogger(__name__)

Level = Tuple[float, float]

# Per-asset CFB-shape params. Mirrors the B2a harness table (CME CF Real Time
# Indices Methodology v16.6 / CF Spot Rate Methodology Guide v15.1). Kept
# module-level (the scanner wiring reads the feed, not these params); promote to
# bot.constants only if a future Bit needs runtime override.
_CFB_PARAMS: Dict[str, Dict[str, object]] = {
    "BTC":  {"venues": ("coinbase", "kraken", "bitstamp", "gemini"), "spacing": 1.0,     "dev": 0.5,  "perr": 5.0,  "lag": 10.0},
    "ETH":  {"venues": ("coinbase", "kraken", "bitstamp", "gemini"), "spacing": 25.0,    "dev": 1.0,  "perr": 5.0,  "lag": 10.0},
    "SOL":  {"venues": ("coinbase", "kraken", "bitstamp", "gemini"), "spacing": 100.0,   "dev": 1.0,  "perr": 5.0,  "lag": 10.0},
    "XRP":  {"venues": ("coinbase", "kraken", "bitstamp"),           "spacing": 10000.0, "dev": 1.0,  "perr": 10.0, "lag": 10.0},
    "DOGE": {"venues": ("coinbase", "kraken", "gemini"),             "spacing": 10000.0, "dev": 1.0,  "perr": 10.0, "lag": 10.0},
    "BNB":  {"venues": ("coinbase", "kraken"),                       "spacing": 1.0,     "dev": 10.0, "perr": 10.0, "lag": 30.0},
    "HYPE": {"venues": ("coinbase", "kraken", "bitstamp"),           "spacing": 10.0,    "dev": 1.0,  "perr": 10.0, "lag": 30.0},
}

# asset -> per-venue wire symbol (bot-side copy of collector.venue_l2_archiver
# VENUE_SYMBOLS + coinbase product ids).
_VENUE_SYMBOLS: Dict[str, Dict[str, str]] = {
    "coinbase": {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD", "DOGE": "DOGE-USD", "BNB": "BNB-USD", "HYPE": "HYPE-USD"},
    "kraken":   {"BTC": "BTC/USD", "ETH": "ETH/USD", "SOL": "SOL/USD", "XRP": "XRP/USD", "DOGE": "XDG/USD", "BNB": "BNB/USD", "HYPE": "HYPE/USD"},
    "bitstamp": {"BTC": "btcusd", "ETH": "ethusd", "SOL": "solusd", "XRP": "xrpusd", "HYPE": "hypeusd"},
    "gemini":   {"BTC": "BTCUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "DOGE": "DOGEUSD"},
}
_REVERSE: Dict[str, Dict[str, str]] = {v: {sym: a for a, sym in m.items()} for v, m in _VENUE_SYMBOLS.items()}

# ── live WS config ─────────────────────────────────────────────────────────
# Verified public L2 WS endpoints (mirror collector.venue_l2_archiver +
# coinbase_wire — the bot already consumes the Coinbase Exchange endpoint
# for ticker; this feed opens a SEPARATE level2_batch connection on it).
_WS_URLS: Dict[str, str] = {
    "coinbase": "wss://ws-feed.exchange.coinbase.com",
    "kraken": "wss://ws.kraken.com/v2",
    "bitstamp": "wss://ws.bitstamp.net",
    "gemini": "wss://api.gemini.com/v2/marketdata",
}
_KRAKEN_BOOK_DEPTH = 100  # mirror collector; CFB only weights near-mid depth.
# Off-hot-path recompute cadence. The scan loop reads the cache O(1); the
# ~7ms-per-asset synthetic compute (measured on a 3000-level Coinbase book)
# runs here on the sampler daemon, NOT in scanner.scan() (SCAN_BODY_SLOW
# budget = 1.5s; 7 assets × 7ms would be a measurable hot-path regression).
_SAMPLE_INTERVAL_SECONDS = 2.0
# A cached synthetic older than this reads as a miss — so a stalled sampler
# (WS outage, thread death) yields honest-NULL rti columns, never a value
# computed many ticks ago.
_CACHE_STALE_SECONDS = 15.0
_RECONNECT_BACKOFF_MAX = 60.0
# Max WS frame size. python-websockets defaults to 1 MiB, but Coinbase
# level2_batch full-book SNAPSHOTS are ~1.04 MB → the lib closes the conn with
# 1009 "message too big" before delivering the frame → infinite reconnect
# (observed 29x/2min on the 2026-05-28 flip; postmortem
# kb/failures/b2b-1-shadow-flip-deploy-may28.md, ticket 86ba67npq). 16 MiB
# mirrors the D1.3-fu1 lesson already baked into coinbase_wire/ws_client.py
# (ws_max_size=16 MiB). Kraken/Bitstamp/Gemini frames are far smaller; the
# cap is harmless for them.
_WS_MAX_SIZE = 16 * 1024 * 1024
# Bound on stop()'s join wait for the asyncio reader thread (mirror
# collector.venue_l2_archiver._STOP_JOIN_TIMEOUT).
_STOP_JOIN_TIMEOUT = 10.0
# Journal marker on every venue disconnect — distinct from the collector
# (venue_l2_ws_disconnected) and bot feed markers so log greps don't cross-
# match. In-bot SHADOW feed.
DISCONNECT_LOG_MARKER = "synthetic_rti_ws_disconnected"


def _symbols_for(venue: str) -> List[str]:
    """Wire symbols this feed subscribes for ``venue`` — every asset that
    BOTH lists the venue in ``_VENUE_SYMBOLS`` AND names it in the asset's
    CFB constituent set (``_CFB_PARAMS[asset]["venues"]``). Subscribing a
    venue the CFB index does not use for an asset would maintain a book that
    ``synthetic`` never reads."""
    out: List[str] = []
    for asset, sym in _VENUE_SYMBOLS.get(venue, {}).items():
        params = _CFB_PARAMS.get(asset)
        if params is not None and venue in params["venues"]:  # type: ignore[operator]
            out.append(sym)
    return out


def _build_coinbase_subscribe(product_ids: Sequence[str]) -> dict:
    return {"type": "subscribe", "product_ids": list(product_ids),
            "channels": ["level2_batch"]}


def _build_kraken_subscribe(symbols: Sequence[str], depth: int) -> dict:
    return {"method": "subscribe",
            "params": {"channel": "book", "symbol": list(symbols), "depth": depth}}


def _build_bitstamp_subscribe(pair: str) -> dict:
    return {"event": "bts:subscribe", "data": {"channel": f"order_book_{pair}"}}


def _build_gemini_subscribe(symbols: Sequence[str]) -> dict:
    return {"type": "subscribe",
            "subscriptions": [{"name": "l2", "symbols": list(symbols)}]}


def _f(x) -> float:
    return float(x)


def _kraken_fmt(s: str) -> str:
    """Kraken v2 checksum token: remove decimal point, strip leading zeros."""
    return str(s).replace(".", "").lstrip("0")


def _kraken_checksum(asks_str: List[Tuple[str, str]], bids_str: List[Tuple[str, str]]) -> int:
    """CRC32 over top-10 asks (asc) then bids (desc) — ported from 86ba5xfyb."""
    parts: List[str] = []
    for p, q in asks_str[:10]:
        parts.append(_kraken_fmt(p)); parts.append(_kraken_fmt(q))
    for p, q in bids_str[:10]:
        parts.append(_kraken_fmt(p)); parts.append(_kraken_fmt(q))
    return zlib.crc32("".join(parts).encode())


class _Book:
    """Per-(venue, asset) book: float price->size for the aggregator, plus a
    string-keyed Kraken view for checksum verification."""
    __slots__ = ("bids", "asks", "ts", "desynced", "kbids_str", "kasks_str")

    def __init__(self) -> None:
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.ts: float = 0.0
        self.desynced: bool = False
        self.kbids_str: Dict[str, str] = {}
        self.kasks_str: Dict[str, str] = {}

    def levels(self) -> Tuple[List[Level], List[Level]]:
        bids = sorted(self.bids.items(), key=lambda kv: kv[0], reverse=True)
        asks = sorted(self.asks.items(), key=lambda kv: kv[0])
        return bids, asks

    def kraken_checksum(self) -> int:
        asks = sorted(self.kasks_str.items(), key=lambda kv: float(kv[0]))[:10]
        bids = sorted(self.kbids_str.items(), key=lambda kv: float(kv[0]), reverse=True)[:10]
        return _kraken_checksum(asks, bids)


# ── parsing: native venue frame -> normalized update ───────────────────────

# Normalized: (asset, kind, bids[float], asks[float], deltas[(side,p,s)],
#              checksum|None, bids_str, asks_str)  — str fields kraken-only.


def _parse_coinbase(raw: dict):
    asset = _REVERSE["coinbase"].get(raw.get("product_id", ""))
    if asset is None:
        return None
    t = raw.get("type")
    if t == "snapshot":
        bids = [(_f(p), _f(s)) for p, s in raw.get("bids", [])]
        asks = [(_f(p), _f(s)) for p, s in raw.get("asks", [])]
        return (asset, "snapshot", bids, asks, [], None, [], [])
    if t == "l2update":
        deltas = []
        for side, price, size in raw.get("changes", []):
            deltas.append(("bid" if side == "buy" else "ask", _f(price), _f(size)))
        return (asset, "delta", [], [], deltas, None, [], [])
    return None


def _parse_kraken(raw: dict):
    if raw.get("channel") != "book":
        return None
    data = raw.get("data") or []
    if not data:
        return None
    e = data[0]
    asset = _REVERSE["kraken"].get(e.get("symbol", ""))
    if asset is None:
        return None
    rb, ra = e.get("bids", []), e.get("asks", [])
    bids = [(_f(b["price"]), _f(b["qty"])) for b in rb]
    asks = [(_f(a["price"]), _f(a["qty"])) for a in ra]
    bids_str = [(str(b["price"]), str(b["qty"])) for b in rb]
    asks_str = [(str(a["price"]), str(a["qty"])) for a in ra]
    checksum = e.get("checksum")
    kind = "snapshot" if raw.get("type") == "snapshot" else "delta"
    if kind == "snapshot":
        return (asset, "snapshot", bids, asks, [], checksum, bids_str, asks_str)
    deltas = [("bid", p, s) for p, s in bids] + [("ask", p, s) for p, s in asks]
    return (asset, "delta", [], [], deltas, checksum, bids_str, asks_str)


def _parse_bitstamp(raw: dict):
    if raw.get("event") != "data":
        return None
    ch = raw.get("channel", "")
    pair = ch[len("order_book_"):] if ch.startswith("order_book_") else ""
    asset = _REVERSE["bitstamp"].get(pair)
    if asset is None:
        return None
    d = raw.get("data") or {}
    bids = [(_f(p), _f(s)) for p, s in d.get("bids", [])]
    asks = [(_f(p), _f(s)) for p, s in d.get("asks", [])]
    return (asset, "snapshot", bids, asks, [], None, [], [])  # full snapshot every frame


def _parse_gemini(raw: dict):
    if raw.get("type") != "l2_updates":
        return None
    asset = _REVERSE["gemini"].get(raw.get("symbol", ""))
    if asset is None:
        return None
    deltas = []
    for side, price, size in raw.get("changes", []):
        deltas.append(("bid" if side == "buy" else "ask", _f(price), _f(size)))
    return (asset, "delta", [], [], deltas, None, [], [])


_PARSERS = {"coinbase": _parse_coinbase, "kraken": _parse_kraken,
            "bitstamp": _parse_bitstamp, "gemini": _parse_gemini}


class SyntheticRTIFeed:
    def __init__(self, enabled: bool = False,
                 urls: Optional[Dict[str, str]] = None) -> None:
        self.enabled = enabled
        self._books: Dict[Tuple[str, str], _Book] = {}
        self._lock = threading.Lock()
        self._urls: Dict[str, str] = dict(_WS_URLS)
        if urls:
            self._urls.update(urls)
        # Per-asset cache populated off the hot path by the sampler thread:
        # asset -> (rti, n_constituents, confidence, ts). Read O(1) by
        # get_cached_synthetic (the scan-loop consumer).
        self._cache: Dict[str, Tuple[float, int, Optional[float], float]] = {}
        self._cache_lock = threading.Lock()
        # WS asyncio reader thread + sampler daemon thread handles.
        self._ws_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws_stop_event: Optional[asyncio.Event] = None
        self._ws_stop_requested = False
        self._sampler_thread: Optional[threading.Thread] = None
        self._sampler_stop = threading.Event()
        self._connected: Dict[str, bool] = {v: False for v in _WS_URLS}

    def ingest_frame(self, venue: str, raw: dict, now: Optional[float] = None) -> None:
        parser = _PARSERS.get(venue)
        if parser is None:
            return
        try:
            parsed = parser(raw)
        except (KeyError, ValueError, TypeError, IndexError):
            return
        if parsed is None:
            return
        asset, kind, bids, asks, deltas, checksum, bids_str, asks_str = parsed
        ts = time.time() if now is None else now
        with self._lock:
            key = (venue, asset)
            book = self._books.get(key)
            if book is None:
                book = self._books[key] = _Book()
            if kind == "snapshot":
                book.bids = {p: s for p, s in bids if s > 0}
                book.asks = {p: s for p, s in asks if s > 0}
                book.desynced = False
                if venue == "kraken":
                    book.kbids_str = {p: q for p, q in bids_str if float(q) > 0}
                    book.kasks_str = {p: q for p, q in asks_str if float(q) > 0}
            else:
                for side, p, s in deltas:
                    target = book.bids if side == "bid" else book.asks
                    if s <= 0:
                        target.pop(p, None)
                    else:
                        target[p] = s
                if venue == "kraken":
                    for side, p, q in _zip_kraken_delta_str(bids_str, asks_str):
                        sd = book.kbids_str if side == "bid" else book.kasks_str
                        if float(q) <= 0:
                            sd.pop(p, None)
                        else:
                            sd[p] = q
            if venue == "kraken" and checksum is not None and not book.desynced:
                if book.kraken_checksum() != checksum:
                    book.desynced = True
            book.ts = ts

    def synthetic(self, asset: str, now: Optional[float] = None) -> Tuple[Optional[float], int]:
        """(rti, n_constituent_venues). Drops stale + desynced + empty books.
        Returns (None, 0) when disabled, unknown asset, or no venue contributes."""
        if not self.enabled:
            return (None, 0)
        params = _CFB_PARAMS.get(asset)
        if params is None:
            return (None, 0)
        t = time.time() if now is None else now
        lag = float(params["lag"])  # type: ignore[arg-type]
        books: Dict[str, Tuple[List[Level], List[Level]]] = {}
        with self._lock:
            for venue in params["venues"]:  # type: ignore[union-attr]
                book = self._books.get((venue, asset))
                if book is None or book.desynced:
                    continue
                if (t - book.ts) > lag:
                    continue
                bids, asks = book.levels()
                if bids and asks:
                    books[venue] = (bids, asks)
        if not books:
            return (None, 0)
        rti = compute_synthetic_rti(
            books, float(params["spacing"]), float(params["dev"]), float(params["perr"]),  # type: ignore[arg-type]
        )
        return (rti, len(books))

    # ── confidence + off-hot-path cache ────────────────────────────────────

    @staticmethod
    def n_expected_venues(asset: str) -> int:
        """How many CFB-constituent venues this feed CAN source for ``asset``
        (the denominator for ``rti_confidence``)."""
        params = _CFB_PARAMS.get(asset)
        return len(params["venues"]) if params is not None else 0  # type: ignore[arg-type]

    def _refresh_cache(self, now: Optional[float] = None) -> None:
        """Recompute every asset's synthetic into the cache. Runs on the
        sampler daemon (off the scan hot path). Disabled feed → no-op."""
        if not self.enabled:
            return
        t = time.time() if now is None else now
        for asset in _CFB_PARAMS:
            try:
                rti, n = self.synthetic(asset, now=t)
            except Exception:
                logger.warning("synthetic compute failed for %s", asset,
                               exc_info=True)
                rti, n = None, 0
            with self._cache_lock:
                if rti is None:
                    self._cache.pop(asset, None)
                else:
                    n_exp = self.n_expected_venues(asset)
                    conf = (n / n_exp) if n_exp else None
                    self._cache[asset] = (rti, n, conf, t)

    def get_cached_synthetic(
        self, asset: str, now: Optional[float] = None,
    ) -> Tuple[Optional[float], int, Optional[float]]:
        """(rti, n_constituents, confidence) from the sampler cache — the
        scan-loop consumer. O(1). Returns (None, 0, None) when disabled, on a
        cache miss, or when the cached value is older than the freshness
        window (sampler stalled → honest-NULL, never a stale reading)."""
        if not self.enabled:
            return (None, 0, None)
        t = time.time() if now is None else now
        with self._cache_lock:
            entry = self._cache.get(asset)
        if entry is None:
            return (None, 0, None)
        rti, n, conf, ts = entry
        if (t - ts) > _CACHE_STALE_SECONDS:
            return (None, 0, None)
        return (rti, n, conf)

    # ── live WS path ───────────────────────────────────────────────────────

    def _on_ws_frame(self, venue: str, raw: str, now: Optional[float] = None) -> None:
        """Parse one raw WS frame string + feed it to ``ingest_frame``.

        Kraken MUST be parsed with ``parse_float=str`` so the v2 checksum
        tokens keep trailing zeros (``"100.10"`` must not collapse to
        ``100.1`` → checksum mismatch → spurious desync-drop). The other
        venues carry prices as strings on the wire already, so default
        parsing is fine. Per-frame exceptions are swallowed so one bad frame
        can't kill the read loop."""
        try:
            if venue == "kraken":
                obj = json.loads(raw, parse_float=str)
            else:
                obj = json.loads(raw)
        except (ValueError, TypeError):
            return
        if isinstance(obj, dict):
            self.ingest_frame(venue, obj, now=now)

    def is_running(self) -> bool:
        t = self._ws_thread
        return bool(t is not None and t.is_alive())

    def start(self) -> None:
        """Start the WS reader + sampler threads. No-op when disabled — the
        kill-switch gives the OFF state a zero-thread/zero-socket footprint
        (the 'scan latency unaffected when off' guarantee)."""
        if not self.enabled:
            return
        if self.is_running():
            return
        self._sampler_stop.clear()
        self._ws_stop_requested = False
        self._ws_thread = threading.Thread(
            target=self._run_ws_thread, name="synthetic-rti-ws", daemon=True)
        self._ws_thread.start()
        self._sampler_thread = threading.Thread(
            target=self._sampler_loop, name="synthetic-rti-sampler", daemon=True)
        self._sampler_thread.start()

    def stop(self) -> None:
        """Signal both threads to stop and join the WS reader. Safe to call
        even if start() never ran (disabled feed)."""
        self._sampler_stop.set()
        self._ws_stop_requested = True
        loop, ev = self._loop, self._ws_stop_event
        if loop is not None and ev is not None:
            loop.call_soon_threadsafe(ev.set)
        t = self._ws_thread
        if t is not None and t.is_alive():
            t.join(timeout=_STOP_JOIN_TIMEOUT)
            if t.is_alive():
                logger.warning(
                    "SyntheticRTIFeed WS thread did not exit within %.0fs of "
                    "stop signal — abandoning (daemon killed at process exit).",
                    _STOP_JOIN_TIMEOUT)

    def _sampler_loop(self) -> None:
        while not self._sampler_stop.is_set():
            if self._sampler_stop.wait(timeout=_SAMPLE_INTERVAL_SECONDS):
                break
            try:
                self._refresh_cache()
            except Exception:
                logger.warning("synthetic-rti sampler pass failed", exc_info=True)

    def _run_ws_thread(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ws_stop_event = asyncio.Event()
        if self._ws_stop_requested:
            self._ws_stop_event.set()
        try:
            self._loop.run_until_complete(self._run())
        except Exception:
            logger.error("SyntheticRTIFeed WS thread crashed", exc_info=True)
        finally:
            self._loop.close()

    async def _run(self) -> None:
        coros = [
            self._ws_coinbase(), self._ws_kraken(),
            self._ws_bitstamp(), self._ws_gemini(),
        ]
        tasks = [asyncio.ensure_future(c) for c in coros]
        stop_task = asyncio.ensure_future(self._ws_stop_event.wait())
        try:
            await asyncio.wait(set(tasks) | {stop_task},
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            stop_task.cancel()
            await asyncio.gather(*tasks, stop_task, return_exceptions=True)

    async def _sleep_or_stop(self, wait: float) -> bool:
        try:
            await asyncio.wait_for(self._ws_stop_event.wait(), timeout=wait)
            return True
        except asyncio.TimeoutError:
            return False

    async def _ws_venue(self, venue: str, subscribe_frames: List[dict]) -> None:
        """Generic per-venue connect/subscribe/read with reconnect+backoff +
        cancel-on-stop (mirror collector.venue_l2_archiver._ws_*)."""
        url = self._urls[venue]
        backoff = 1.0
        while not self._ws_stop_event.is_set():
            try:
                async with websockets.connect(url, max_size=_WS_MAX_SIZE) as ws:
                    for frame in subscribe_frames:
                        await ws.send(json.dumps(frame))
                    self._connected[venue] = True
                    backoff = 1.0
                    logger.info("SyntheticRTIFeed %s L2 connected", venue)
                    async for raw in ws:
                        if self._ws_stop_event.is_set():
                            break
                        self._on_ws_frame(venue, raw)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected[venue] = False
                wait = backoff + backoff * random.uniform(0, 0.25)
                logger.warning("%s venue=%s: %s — reconnecting in %.1fs",
                               DISCONNECT_LOG_MARKER, venue, e, wait)
                if await self._sleep_or_stop(wait):
                    break
                backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)
        self._connected[venue] = False

    async def _ws_coinbase(self) -> None:
        syms = _symbols_for("coinbase")
        await self._ws_venue("coinbase", [_build_coinbase_subscribe(syms)])

    async def _ws_kraken(self) -> None:
        syms = _symbols_for("kraken")
        await self._ws_venue("kraken", [_build_kraken_subscribe(syms, _KRAKEN_BOOK_DEPTH)])

    async def _ws_bitstamp(self) -> None:
        # Bitstamp needs ONE subscribe frame per pair.
        frames = [_build_bitstamp_subscribe(p) for p in _symbols_for("bitstamp")]
        await self._ws_venue("bitstamp", frames)

    async def _ws_gemini(self) -> None:
        syms = _symbols_for("gemini")
        await self._ws_venue("gemini", [_build_gemini_subscribe(syms)])


def _zip_kraken_delta_str(bids_str, asks_str):
    for p, q in bids_str:
        yield ("bid", p, q)
    for p, q in asks_str:
        yield ("ask", p, q)
