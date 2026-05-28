"""Offline synthetic-RTI RMSE harness — B2a-2 (ticket 86ba1zf5j, 2026-05-28).

Plan: kb/decisions/b2-synthetic-rti-feed-plan.md ("B2a acceptance — offline
RMSE validation gate").

Reconstructs per-second CONSOLIDATED order books from the four venues' bronze
L2 (coinbase + kraken + bitstamp + gemini) across the **last 60 seconds before
each settled 15M market close**, runs the shipped pure aggregator
``bot.feeds.synthetic_rti.compute_synthetic_rti`` per second, **averages the 60
values** (mirrors CFB settlement = the 60-second average of the RTI before
close), and compares to the market's ``expiration_value`` → per-asset RMSE in
bps. The RMSE gate decides whether B2b (the live in-bot integration) is built or
the program escalates to B3 (paid CFB API):

    PASS (→ build B2b):  ≤15 bps for BTC/ETH/SOL/XRP/DOGE/BNB; ≤25 bps for HYPE.
    FAIL (→ escalate B3): otherwise.

Why offline replay (not a bot-shadow log): settlement is the 60-second AVERAGE
of per-second RTI before close. A bot shadow would log a single value at
decision time (the wrong quantity). Only offline replay can reconstruct
per-second books across the last 60s and average exactly as CFB does.

scripts/ MAY import bot.* and collector.* (the collector-no-bot contract binds
collector/, not scripts/). The pure aggregator is venue-source-agnostic, so this
harness feeds it books reconstructed from bronze JSONL; the deferred B2b live
path will feed it books from in-bot WS venue feeds.

KNOWN reconstruction caveats (measured by the RMSE gate itself, not assumed):
  - Missing 4 CFB venues for BTC/ETH (Bullish/Crypto.com/LMAX/itBit) — no free
    public L2 WS. A documented coverage gap (e.g. 4-of-8 for BTC).
  - Gemini has no explicit snapshot marker; book is reconstructed from the
    lookback window's deltas (near-top converges quickly). Kraken/Coinbase have
    explicit ``snapshot`` frames; the bronze reader seeds from the most recent
    snapshot in the lookback window. Bitstamp every frame is a full snapshot.
  - Kraken L2 CRC32 checksum verification IS implemented (ticket 86ba5xfyb):
    after each ``update`` the top-10 v2 checksum is recomputed and compared to
    the frame's ``checksum``; on mismatch the Kraken stream is marked desynced
    and DROPPED from the consolidated book for the rest of that 60s window (a
    fresh ``snapshot`` re-seeds + clears the flag). Frames lacking a checksum
    skip the check (e.g. unit fixtures). See ``kraken_book_checksum`` /
    ``_KrakenChecksumBook`` / ``_verify_kraken_checksum``.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import math
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

import zstandard as zstd

from bot.feeds.synthetic_rti import compute_synthetic_rti
from collector.venue_l2_archiver import VENUE_SYMBOLS

logger = logging.getLogger(__name__)

# Coinbase product_id per asset (matches coinbase_wire DEFAULT_PRODUCT_IDS).
COINBASE_PRODUCTS: Mapping[str, str] = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "XRP": "XRP-USD",
    "DOGE": "DOGE-USD",
    "BNB": "BNB-USD",
    "HYPE": "HYPE-USD",
}

# venue -> (bronze source, bronze channel). Coinbase L2 already lives under
# coinbase_ws/level2_batch (kalshi-coinbase-collector); the other three land
# from the B2a-1 venue-L2 collector.
VENUE_BRONZE: Mapping[str, Tuple[str, str]] = {
    "coinbase": ("coinbase_ws", "level2_batch"),
    "kraken": ("kraken_ws", "book"),
    "bitstamp": ("bitstamp_ws", "order_book"),
    "gemini": ("gemini_ws", "l2"),
}

# Per-asset CFB-shape parameters (verbatim from the plan-doc locked table:
# CME CF Real Time Indices Methodology v16.6 §6.2 for BTC/ETH/SOL/XRP;
# CF Spot Rate Methodology Guide v15.1 §6.2 for DOGE/BNB/HYPE). Defined
# LOCALLY here (NOT imported from bot.constants) so B2a touches zero bot
# process code; B2b migrates these to bot.constants.SYNTHETIC_RTI_CFB_PARAMS.
CFB_PARAMS: Mapping[str, Dict[str, object]] = {
    "BTC":  {"venues": ("coinbase", "kraken", "bitstamp", "gemini"), "spacing": 1.0,     "deviation_from_mid_pct": 0.5,  "potentially_erroneous_pct": 5.0,  "retrieval_lag_threshold_seconds": 10.0},
    "ETH":  {"venues": ("coinbase", "kraken", "bitstamp", "gemini"), "spacing": 25.0,    "deviation_from_mid_pct": 1.0,  "potentially_erroneous_pct": 5.0,  "retrieval_lag_threshold_seconds": 10.0},
    "SOL":  {"venues": ("coinbase", "kraken", "bitstamp", "gemini"), "spacing": 100.0,   "deviation_from_mid_pct": 1.0,  "potentially_erroneous_pct": 5.0,  "retrieval_lag_threshold_seconds": 10.0},
    "XRP":  {"venues": ("coinbase", "kraken", "bitstamp"),           "spacing": 10000.0, "deviation_from_mid_pct": 1.0,  "potentially_erroneous_pct": 10.0, "retrieval_lag_threshold_seconds": 10.0},
    "DOGE": {"venues": ("coinbase", "kraken", "gemini"),             "spacing": 10000.0, "deviation_from_mid_pct": 1.0,  "potentially_erroneous_pct": 10.0, "retrieval_lag_threshold_seconds": 10.0},
    "BNB":  {"venues": ("coinbase", "kraken"),                       "spacing": 1.0,     "deviation_from_mid_pct": 10.0, "potentially_erroneous_pct": 10.0, "retrieval_lag_threshold_seconds": 30.0},
    "HYPE": {"venues": ("coinbase", "kraken", "bitstamp"),           "spacing": 10.0,    "deviation_from_mid_pct": 1.0,  "potentially_erroneous_pct": 10.0, "retrieval_lag_threshold_seconds": 30.0},
}

# Per-asset RMSE gate (plan-doc acceptance section).
RMSE_GATE_BPS_HYPE = 25.0
RMSE_GATE_BPS_DEFAULT = 15.0

# Default 15M series ticker per asset (Kalshi).
ASSET_SERIES_15M: Mapping[str, str] = {
    "BTC": "KXBTC15M",
    "ETH": "KXETH15M",
    "SOL": "KXSOL15M",
    "XRP": "KXXRP15M",
    "DOGE": "KXDOGE15M",
    "BNB": "KXBNB15M",
    "HYPE": "KXHYPE15M",
}

# CFB averages the RTI over the last 60s before close.
SETTLEMENT_WINDOW_SECONDS = 60
# Per-market bronze read lookback before the window so the reader includes a
# snapshot to seed the book (kraken/coinbase snapshot; bitstamp every-frame;
# gemini delta-converge).
DEFAULT_LOOKBACK_SECONDS = 600


def asset_gate_bps(asset: str) -> float:
    """Per-asset RMSE pass threshold in bps."""
    return RMSE_GATE_BPS_HYPE if asset == "HYPE" else RMSE_GATE_BPS_DEFAULT


def _reverse_symbol_maps() -> Dict[str, Dict[str, str]]:
    """venue -> {wire-symbol-or-pair-or-product : asset}.

    Built from collector.venue_l2_archiver.VENUE_SYMBOLS (kraken/bitstamp/
    gemini) + COINBASE_PRODUCTS, so it stays lock-step with the recorder's
    subscribe map. Kraken XDG/USD → DOGE falls out automatically."""
    maps: Dict[str, Dict[str, str]] = {}
    for venue in ("kraken", "bitstamp", "gemini"):
        maps[venue] = {sym: asset for asset, sym in VENUE_SYMBOLS[venue].items()}
    maps["coinbase"] = {pid: asset for asset, pid in COINBASE_PRODUCTS.items()}
    return maps


# ── Order book ─────────────────────────────────────────────────────────────


class OrderBook:
    """Minimal price→size order book with snapshot-replace + delta-apply.

    Produces (bids descending, asks ascending) level lists matching the
    aggregator's ``Book`` type. NO checksum / sequence-gap detection — this
    is offline replay; occasional desync is averaged out over many markets."""

    def __init__(self) -> None:
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}

    def apply_snapshot(
        self,
        bids: Sequence[Tuple[float, float]],
        asks: Sequence[Tuple[float, float]],
    ) -> None:
        self.bids = {p: s for p, s in bids if s > 0}
        self.asks = {p: s for p, s in asks if s > 0}

    def apply_delta(self, side: str, price: float, size: float) -> None:
        book = self.bids if side == "bid" else self.asks
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size

    def snapshot(self) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
        bids = sorted(self.bids.items(), key=lambda kv: kv[0], reverse=True)
        asks = sorted(self.asks.items(), key=lambda kv: kv[0])
        return bids, asks


# ── Kraken v2 book checksum ──────────────────────────────────────────────────


def _kraken_fmt(s: str) -> str:
    """Kraken v2 checksum token: remove the decimal point, strip leading zeros.

    Operates on the ORIGINAL price/qty STRING (e.g. ``"59.50"`` -> ``"5950"``,
    ``"0.30709338"`` -> ``"30709338"``). A float-parsed value loses
    trailing-zero precision (``"59.50"`` -> ``59.5`` -> ``"595"``) and yields
    the wrong token, so callers must pass the precision-preserving string."""
    return s.replace(".", "").lstrip("0")


def kraken_book_checksum(
    asks: Sequence[Tuple[str, str]],
    bids: Sequence[Tuple[str, str]],
    depth: int = 10,
) -> int:
    """CRC32 of the top-``depth`` asks (ascending) then bids (descending).

    ``asks``/``bids`` are ``(price_str, qty_str)`` at book precision, sorted
    best-first. Mirrors Kraken WS v2's ``book`` channel checksum so a desynced
    book (from a dropped/out-of-order bronze diff) can be detected. Verified
    against a real recorded HYPE/USD snapshot (checksum 3129294453)."""
    parts: List[str] = []
    for price, qty in list(asks)[:depth]:
        parts.append(_kraken_fmt(price))
        parts.append(_kraken_fmt(qty))
    for price, qty in list(bids)[:depth]:
        parts.append(_kraken_fmt(price))
        parts.append(_kraken_fmt(qty))
    return zlib.crc32("".join(parts).encode())


class _KrakenChecksumBook:
    """String-keyed Kraken book (price_str -> qty_str) maintained alongside the
    float OrderBook purely to verify the v2 checksum. Detects a desynced book
    after a dropped/out-of-order bronze diff."""

    def __init__(self) -> None:
        self.bids: Dict[str, str] = {}
        self.asks: Dict[str, str] = {}

    def apply_snapshot(
        self,
        bids_str: Sequence[Tuple[str, str]],
        asks_str: Sequence[Tuple[str, str]],
    ) -> None:
        self.bids = {p: q for p, q in bids_str if float(q) > 0}
        self.asks = {p: q for p, q in asks_str if float(q) > 0}

    def apply_delta(self, side: str, price_str: str, qty_str: str) -> None:
        book = self.bids if side == "bid" else self.asks
        if float(qty_str) <= 0:
            book.pop(price_str, None)
        else:
            book[price_str] = qty_str

    def checksum(self) -> int:
        asks = sorted(self.asks.items(), key=lambda kv: float(kv[0]))[:10]
        bids = sorted(self.bids.items(), key=lambda kv: float(kv[0]), reverse=True)[:10]
        return kraken_book_checksum(asks, bids)


# ── Normalized venue update + parsers ───────────────────────────────────────


@dataclass
class VenueUpdate:
    asset: str
    kind: str  # "snapshot" | "delta"
    bids: List[Tuple[float, float]] = field(default_factory=list)
    asks: List[Tuple[float, float]] = field(default_factory=list)
    deltas: List[Tuple[str, float, float]] = field(default_factory=list)
    # Kraken-only: the v2 frame checksum + precision-preserving string levels,
    # used to detect a desynced book. None/empty for the other venues.
    checksum: Optional[int] = None
    bids_str: List[Tuple[str, str]] = field(default_factory=list)
    asks_str: List[Tuple[str, str]] = field(default_factory=list)
    deltas_str: List[Tuple[str, str, str]] = field(default_factory=list)


_REVERSE = _reverse_symbol_maps()


def _f(x) -> float:
    return float(x)


def _parse_kraken(raw: dict) -> Optional[VenueUpdate]:
    if raw.get("channel") != "book":
        return None
    data = raw.get("data") or []
    if not data:
        return None
    entry = data[0]
    asset = _REVERSE["kraken"].get(entry.get("symbol", ""))
    if asset is None:
        return None
    raw_bids = entry.get("bids", [])
    raw_asks = entry.get("asks", [])
    bids = [(_f(b["price"]), _f(b["qty"])) for b in raw_bids]
    asks = [(_f(a["price"]), _f(a["qty"])) for a in raw_asks]
    # Precision-preserving strings for the v2 checksum. str() is a no-op on the
    # bronze path (iter_bronze_frames parses _raw with parse_float=str).
    checksum = entry.get("checksum")
    if raw.get("type") == "snapshot":
        return VenueUpdate(
            asset=asset, kind="snapshot", bids=bids, asks=asks,
            checksum=checksum,
            bids_str=[(str(b["price"]), str(b["qty"])) for b in raw_bids],
            asks_str=[(str(a["price"]), str(a["qty"])) for a in raw_asks],
        )
    deltas = (
        [("bid", p, s) for p, s in bids] + [("ask", p, s) for p, s in asks]
    )
    deltas_str = (
        [("bid", str(b["price"]), str(b["qty"])) for b in raw_bids]
        + [("ask", str(a["price"]), str(a["qty"])) for a in raw_asks]
    )
    return VenueUpdate(
        asset=asset, kind="delta", deltas=deltas,
        checksum=checksum, deltas_str=deltas_str,
    )


def _parse_bitstamp(raw: dict) -> Optional[VenueUpdate]:
    if raw.get("event") != "data":
        return None
    channel = raw.get("channel", "")
    pair = channel[len("order_book_"):] if channel.startswith("order_book_") else ""
    asset = _REVERSE["bitstamp"].get(pair)
    if asset is None:
        return None
    data = raw.get("data") or {}
    bids = [(_f(p), _f(s)) for p, s in data.get("bids", [])]
    asks = [(_f(p), _f(s)) for p, s in data.get("asks", [])]
    # Bitstamp pushes a FULL top-N snapshot every frame.
    return VenueUpdate(asset=asset, kind="snapshot", bids=bids, asks=asks)


def _parse_gemini(raw: dict) -> Optional[VenueUpdate]:
    if raw.get("type") != "l2_updates":
        return None
    asset = _REVERSE["gemini"].get(raw.get("symbol", ""))
    if asset is None:
        return None
    deltas: List[Tuple[str, float, float]] = []
    for change in raw.get("changes", []):
        side, price, size = change
        deltas.append(("bid" if side == "buy" else "ask", _f(price), _f(size)))
    return VenueUpdate(asset=asset, kind="delta", deltas=deltas)


def _parse_coinbase(raw: dict) -> Optional[VenueUpdate]:
    asset = _REVERSE["coinbase"].get(raw.get("product_id", ""))
    if asset is None:
        return None
    mtype = raw.get("type")
    if mtype == "snapshot":
        bids = [(_f(p), _f(s)) for p, s in raw.get("bids", [])]
        asks = [(_f(p), _f(s)) for p, s in raw.get("asks", [])]
        return VenueUpdate(asset=asset, kind="snapshot", bids=bids, asks=asks)
    if mtype == "l2update":
        deltas: List[Tuple[str, float, float]] = []
        for change in raw.get("changes", []):
            side, price, size = change
            deltas.append(("bid" if side == "buy" else "ask", _f(price), _f(size)))
        return VenueUpdate(asset=asset, kind="delta", deltas=deltas)
    return None


_PARSERS = {
    "kraken": _parse_kraken,
    "bitstamp": _parse_bitstamp,
    "gemini": _parse_gemini,
    "coinbase": _parse_coinbase,
}


def parse_frame(venue: str, raw: dict) -> Optional[VenueUpdate]:
    """Parse one raw bronze frame into a normalized VenueUpdate, or None if
    it is not a routable L2 book frame (control / ack / wrong-asset)."""
    parser = _PARSERS.get(venue)
    if parser is None:
        return None
    try:
        return parser(raw)
    except (KeyError, ValueError, TypeError, IndexError):
        return None


def _apply_update(book: OrderBook, u: VenueUpdate) -> None:
    if u.kind == "snapshot":
        book.apply_snapshot(u.bids, u.asks)
    else:
        for side, price, size in u.deltas:
            book.apply_delta(side, price, size)


def _verify_kraken_checksum(stream: dict, u: VenueUpdate) -> None:
    """Maintain the Kraken string book and flag the stream desynced on a v2
    checksum mismatch (a dropped/out-of-order diff). A snapshot re-seeds the
    book and clears any prior desync. Frames without a checksum skip the check."""
    cb = stream["cbook"]
    if u.kind == "snapshot":
        cb.apply_snapshot(u.bids_str, u.asks_str)
        stream["desynced"] = False
    else:
        for side, price, qty in u.deltas_str:
            cb.apply_delta(side, price, qty)
    if u.checksum is not None and not stream["desynced"]:
        # INVARIANT: checksum-bearing frames carry string price/qty (bronze
        # forces parse_float=str); a float price would falsely desync (safe dir).
        if cb.checksum() != u.checksum:
            stream["desynced"] = True


# ── Per-second reconstruction + 60s average ─────────────────────────────────


def synthetic_rti_60s_average(
    frames_by_venue: Mapping[str, Sequence[Tuple[float, dict]]],
    asset: str,
    close_ts: float,
    params: Mapping[str, object],
    window: int = SETTLEMENT_WINDOW_SECONDS,
    step: float = 1.0,
) -> Optional[float]:
    """Reconstruct per-second consolidated books over the last ``window``
    seconds before ``close_ts``, run compute_synthetic_rti each second, and
    return the mean of the non-None values (None if no second produced a value).

    ``frames_by_venue``: venue -> iterable of (wire_recv_ts_epoch, raw_dict),
    pre-loaded by ``iter_bronze_frames`` (must include a seed snapshot before
    the window for snapshot-bearing venues). Frames for other assets and
    non-book control frames are filtered out via parse_frame."""
    venues = tuple(params["venues"])  # type: ignore[index]
    spacing = float(params["spacing"])  # type: ignore[arg-type]
    dev = float(params["deviation_from_mid_pct"])  # type: ignore[arg-type]
    perr = float(params["potentially_erroneous_pct"])  # type: ignore[arg-type]
    lag = float(params["retrieval_lag_threshold_seconds"])  # type: ignore[arg-type]

    # Pre-parse + sort each venue's updates for THIS asset.
    streams: Dict[str, dict] = {}
    for venue in venues:
        ups: List[Tuple[float, VenueUpdate]] = []
        for ts, raw in frames_by_venue.get(venue, []):
            u = parse_frame(venue, raw)
            if u is not None and u.asset == asset:
                ups.append((ts, u))
        ups.sort(key=lambda x: x[0])
        streams[venue] = {
            "ups": ups, "i": 0, "book": OrderBook(), "last": None,
            "cbook": _KrakenChecksumBook() if venue == "kraken" else None,
            "desynced": False,
        }

    n_samples = int(round(window / step))
    samples = [close_ts - window + (i + 1) * step for i in range(n_samples)]

    values: List[float] = []
    for t in samples:
        books: Dict[str, Tuple[list, list]] = {}
        for venue in venues:
            s = streams[venue]
            ups = s["ups"]
            while s["i"] < len(ups) and ups[s["i"]][0] <= t:
                ts_u, u = ups[s["i"]]
                _apply_update(s["book"], u)
                if s["cbook"] is not None:
                    _verify_kraken_checksum(s, u)
                s["last"] = ts_u
                s["i"] += 1
            if s["last"] is None or (t - s["last"]) > lag:
                continue  # no data yet, or stale beyond retrieval-lag → drop
            if s["desynced"]:
                continue  # checksum mismatch → corrupt book, drop the venue
            bids, asks = s["book"].snapshot()
            if bids and asks:
                books[venue] = (bids, asks)
        if not books:
            continue
        rti = compute_synthetic_rti(books, spacing, dev, perr)
        if rti is not None:
            values.append(rti)

    if not values:
        return None
    return sum(values) / len(values)


# ── RMSE math ────────────────────────────────────────────────────────────────


def bps_error(synthetic: float, expiration_value: float) -> float:
    """Signed deviation of the synthetic from the settlement value, in bps."""
    return (synthetic - expiration_value) / expiration_value * 10000.0


def rmse_bps(errors_bps: Sequence[float]) -> float:
    """Root-mean-square of the per-market bps errors (NaN on empty input)."""
    if not errors_bps:
        return float("nan")
    return math.sqrt(sum(e * e for e in errors_bps) / len(errors_bps))


# ── Settled-market enumerator (Kalshi REST) ─────────────────────────────────


@dataclass
class SettledMarket:
    ticker: str
    asset: str
    close_ts: float
    expiration_value: float


def _iso_to_epoch(iso: str) -> float:
    """Parse an ISO-8601 UTC string (with trailing Z, optional fractional
    seconds) to an epoch float. Handles both the envelope ``_wire_recv_ts``
    (microsecond + Z) and Kalshi ``close_time`` (second + Z) shapes."""
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return _dt.datetime.fromisoformat(s).timestamp()


def _parse_expiration_value(market: dict) -> Optional[float]:
    raw = market.get("expiration_value")
    if raw in (None, ""):
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def load_settled_markets(
    client,
    asset_series: Mapping[str, str],
    start_ts: float,
    end_ts: float,
) -> List[SettledMarket]:
    """Enumerate settled 15M markets per asset in [start_ts, end_ts] via the
    Kalshi REST ``get_markets`` endpoint (status=settled, close-ts window,
    cursor pagination). Skips markets with an empty/zero ``expiration_value``
    (not yet populated). ``client`` is any object exposing ``get_markets``."""
    out: List[SettledMarket] = []
    for asset, series in asset_series.items():
        cursor: Optional[str] = None
        while True:
            resp = client.get_markets(
                series_ticker=series,
                status="settled",
                min_close_ts=int(start_ts),
                max_close_ts=int(end_ts),
                cursor=cursor,
            )
            if not resp:
                break
            for market in resp.get("markets", []):
                exp = _parse_expiration_value(market)
                if exp is None:
                    continue
                close_time = market.get("close_time")
                if not close_time:
                    continue
                try:
                    close_ts = _iso_to_epoch(close_time)
                except ValueError:
                    continue
                out.append(SettledMarket(
                    ticker=market.get("ticker", ""),
                    asset=asset,
                    close_ts=close_ts,
                    expiration_value=exp,
                ))
            cursor = resp.get("cursor") or ""
            if not cursor:
                break
    return out


# ── Bronze reader ────────────────────────────────────────────────────────────


def _partition_hour_epoch(path: Path) -> Optional[float]:
    """Extract the hour-partition start epoch from a hive-partitioned bronze
    chunk path (year=/month=/day=/hour= segments), or None if absent."""
    parts = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in path.parts if "=" in p}
    try:
        return _dt.datetime(
            int(parts["year"]), int(parts["month"]), int(parts["day"]),
            int(parts["hour"]), tzinfo=_dt.timezone.utc,
        ).timestamp()
    except (KeyError, ValueError):
        return None


def iter_bronze_frames(
    bronze_root: Path,
    source: str,
    channel: str,
    start_ts: float,
    end_ts: float,
) -> Iterator[Tuple[float, dict]]:
    """Yield (wire_recv_ts_epoch, raw_dict) for every bronze envelope in
    [start_ts, end_ts] under ``bronze_root/source/channel`` (any hour
    partition / conn / outbox/flat layout).

    Decompresses each ``*.jsonl.zst`` chunk, unwraps the 6-field envelope, and
    yields the parsed ``_raw`` payload. Chunks whose hour-partition lies wholly
    outside the window are skipped for I/O efficiency; the per-line ts filter
    is authoritative."""
    base = Path(bronze_root) / source / channel
    if not base.is_dir():
        return
    dctx = zstd.ZstdDecompressor()
    for chunk in sorted(base.rglob("*.jsonl.zst")):
        hour_epoch = _partition_hour_epoch(chunk)
        if hour_epoch is not None and (
            hour_epoch > end_ts or hour_epoch + 3600 < start_ts
        ):
            continue
        try:
            data = dctx.decompress(chunk.read_bytes())
        except (OSError, zstd.ZstdError):
            logger.warning("could not decompress bronze chunk %s", chunk)
            continue
        for line in data.splitlines():
            if not line.strip():
                continue
            try:
                env = json.loads(line)
                ts = _iso_to_epoch(env["_wire_recv_ts"])
            except (ValueError, KeyError):
                continue
            if ts < start_ts or ts > end_ts:
                continue
            try:
                # parse_float=str preserves price/qty precision (Kraken sends
                # them as JSON numbers) for the v2 checksum; harmless elsewhere
                # (other venues send string prices; parsers float() via _f).
                raw = json.loads(env["_raw"], parse_float=str)
            except (ValueError, KeyError, TypeError):
                continue
            yield ts, raw


# ── End-to-end per-asset RMSE ────────────────────────────────────────────────


def compute_per_asset_rmse(
    markets: Sequence[SettledMarket],
    bronze_root: Path,
    lookback_seconds: int = DEFAULT_LOOKBACK_SECONDS,
) -> Dict[str, dict]:
    """For each settled market, load each constituent venue's bronze L2 over
    [close-lookback, close], compute the 60s-average synthetic, and accumulate
    per-asset bps errors → RMSE + pass/fail vs the gate.

    Returns {asset: {n, rmse_bps, gate_bps, passed, errors}}."""
    errors_by_asset: Dict[str, List[float]] = {}
    for m in markets:
        params = CFB_PARAMS.get(m.asset)
        if params is None:
            continue
        venues = tuple(params["venues"])  # type: ignore[index]
        start = m.close_ts - lookback_seconds
        frames_by_venue: Dict[str, List[Tuple[float, dict]]] = {}
        for venue in venues:
            source, channel = VENUE_BRONZE[venue]
            frames_by_venue[venue] = list(iter_bronze_frames(
                bronze_root, source, channel, start, m.close_ts,
            ))
        synth = synthetic_rti_60s_average(
            frames_by_venue, m.asset, m.close_ts, params,
        )
        if synth is None:
            continue
        errors_by_asset.setdefault(m.asset, []).append(
            bps_error(synth, m.expiration_value)
        )

    result: Dict[str, dict] = {}
    for asset, errs in errors_by_asset.items():
        r = rmse_bps(errs)
        gate = asset_gate_bps(asset)
        result[asset] = {
            "n": len(errs),
            "rmse_bps": r,
            "gate_bps": gate,
            "passed": (not math.isnan(r)) and r <= gate,
            "errors": errs,
        }
    return result


def _build_kalshi_client():
    """Construct a read-only Kalshi client for the settled-market enumerator.

    Imported lazily so the core (book reconstruction + RMSE) is testable
    without bot runtime / credentials."""
    from bot.kalshi_client import KalshiClient  # type: ignore
    return KalshiClient()


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Offline synthetic-RTI RMSE harness (B2a-2)")
    ap.add_argument("--bronze-root", required=True, type=Path,
                    help="local bronze root (rclone-synced window of the 4 venues' L2)")
    ap.add_argument("--start", required=True, help="window start ISO-8601 UTC (e.g. 2026-06-01T00:00:00Z)")
    ap.add_argument("--end", required=True, help="window end ISO-8601 UTC")
    ap.add_argument("--markets-json", type=Path, default=None,
                    help="optional pre-fetched settled markets JSON "
                         "([{ticker,asset,close_time,expiration_value}, ...]); "
                         "bypasses the Kalshi API")
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK_SECONDS)
    ap.add_argument("--assets", default=",".join(ASSET_SERIES_15M),
                    help="comma-separated assets to evaluate")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    start_ts = _iso_to_epoch(args.start)
    end_ts = _iso_to_epoch(args.end)
    assets = [a.strip() for a in args.assets.split(",") if a.strip()]

    if args.markets_json is not None:
        raw_markets = json.loads(args.markets_json.read_text())
        markets = []
        for mk in raw_markets:
            exp = _parse_expiration_value(mk)
            if exp is None or mk.get("asset") not in assets:
                continue
            markets.append(SettledMarket(
                ticker=mk.get("ticker", ""), asset=mk["asset"],
                close_ts=_iso_to_epoch(mk["close_time"]), expiration_value=exp,
            ))
    else:
        client = _build_kalshi_client()
        series = {a: ASSET_SERIES_15M[a] for a in assets if a in ASSET_SERIES_15M}
        markets = load_settled_markets(client, series, start_ts, end_ts)

    logger.info("Evaluating %d settled markets across %s", len(markets), assets)
    result = compute_per_asset_rmse(markets, args.bronze_root, args.lookback)

    print("\n=== Synthetic-RTI offline RMSE (B2a) ===")
    all_pass = True
    for asset in assets:
        r = result.get(asset)
        if r is None:
            print(f"  {asset:5s}  n=0     (no reconstructable markets)")
            continue
        verdict = "PASS" if r["passed"] else "FAIL"
        if not r["passed"]:
            all_pass = False
        print(f"  {asset:5s}  n={r['n']:<5d} rmse={r['rmse_bps']:.2f}bps "
              f"gate={r['gate_bps']:.0f}bps  {verdict}")
    print(f"\nOverall gate: {'PASS — build B2b' if all_pass else 'FAIL — escalate to B3 (paid CFB)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
