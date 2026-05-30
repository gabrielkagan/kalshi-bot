"""Multi-venue L2 order-book reconstruction (B2b synthetic-RTI).

Kalshi crypto 15M settles to the CF Benchmarks RTI ≈ the average of constituent
venues' top-of-book mids. Coinbase bronze ships a ready `ticker` (best_bid/ask),
but Kraken / Bitstamp / Gemini are archived as raw L2 order books only — this
module replays their snapshot+delta streams into a coherent, NEVER-CROSSED
best_bid / best_ask / mid so the multi-venue averager can rebuild each asset's
RTI without look-ahead.

Mirrors the proven `kalshi_book_reconstruct` shape: a per-venue `*Book` class
with `apply_snapshot` / `apply_delta` / `apply_frame` + `best_bid()` /
`best_ask()` / `mid()` (USD floats), a module-level `mid_at(frames, cutoff, book)`
that replays `(recv_epoch, inner)` frames with `recv_epoch <= cutoff`, and the
same `parse_envelope` convention.

Wire formats (verified against real bronze 2026-05-29, hour 05):
  - Kraken (v2 `book`): {"type":"snapshot"|"update",
      "data":[{"symbol":"BTC/USD","bids":[{"price","qty"}],"asks":[...]}]}.
      `update` qty is the new ABSOLUTE size at that level; qty==0 removes it;
      `snapshot` replaces the whole book. Symbols: "<BASE>/USD" (all 7 assets).
  - Bitstamp (`order_book`): {"channel":"order_book_btcusd",
      "data":{"bids":[[price,size],...],"asks":[...]}} — a FULL top-100 snapshot
      on every message (not a diff). Symbol lives in the channel suffix.
  - Gemini (`l2`): {"type":"l2_updates","symbol":"BTCUSD",
      "changes":[[side,price,qty],...]}. The first l2_updates per symbol is the
      full snapshot; each change SETS the absolute qty (qty=="0" removes).
      `trade` / `auction_indicative` frames are ignored.

All three quote absolute size at a level, so `apply_delta` here is a SET (not the
additive delta Kalshi uses). Kraken / Gemini rebuild from incremental deltas and
never cross; Bitstamp's throttled `order_book` is a periodic full snapshot whose
two sides self-cross ~0.2% of ticks at dust sizes (a known feed artifact — see
`is_crossed`). `mid()` returns None on a crossed book so every EMITTED mid stays
coherent. Per-asset venue constituents come from
`kb-research/cfb-rti-constituent-exchanges-may28.md`; a venue that doesn't
constitute an asset is omitted from `VENUE_SYMBOLS` (we never fabricate a feed).

Parent program: B2b multi-venue synthetic-RTI (tickets 86ba64h2w / 86ba5ww4k).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence

# Price scale: integer ticks of $1e-8 avoid float-dict-key fragility while
# preserving every venue's quoted precision. ASSUMPTION: no venue quotes finer
# than 8 decimal places (verified on real bronze 2026-05-29 — BTC/ETH ~2dp,
# XRP/DOGE/HYPE ~5-6dp, all exact at 1e-8). A future higher-precision venue
# would silently collide adjacent levels here; widen _SCALE if that ever lands.
_SCALE = 100_000_000


def _tick(price) -> int:
    """Price (str|float) -> integer ticks of $1e-8 (stable dict key)."""
    return int(round(float(price) * _SCALE))


def _price(tick: int) -> float:
    """Integer tick -> USD price (float)."""
    return tick / _SCALE


def _pairs(levels: Iterable):
    """Normalize a level list to (price, qty) pairs.

    Accepts Kraken-style dicts ({"price","qty"}) and Bitstamp/Gemini-style
    [price, qty] sequences."""
    for lv in levels:
        if isinstance(lv, dict):
            yield lv["price"], lv["qty"]
        else:
            yield lv[0], lv[1]


# Per-venue symbol map. ONLY the assets each venue actually constitutes in the
# CF Benchmarks RTI (kb-research/cfb-rti-constituent-exchanges-may28.md). Bronze
# may contain more symbols (e.g. Gemini archives XRPUSD) but a non-constituent
# is intentionally omitted so the averager never blends a venue that CFB doesn't.
VENUE_SYMBOLS: dict[str, dict[str, str]] = {
    "kraken": {  # all 7 assets, "<BASE>/USD" (v2 WS uses BTC not XBT)
        "BTC": "BTC/USD", "ETH": "ETH/USD", "SOL": "SOL/USD", "XRP": "XRP/USD",
        "DOGE": "DOGE/USD", "BNB": "BNB/USD", "HYPE": "HYPE/USD",
    },
    "bitstamp": {  # no DOGE, no BNB
        "BTC": "btcusd", "ETH": "ethusd", "SOL": "solusd", "XRP": "xrpusd",
        "HYPE": "hypeusd",
    },
    "gemini": {  # no XRP, no BNB, no HYPE
        "BTC": "BTCUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "DOGE": "DOGEUSD",
    },
}

VENUES = tuple(VENUE_SYMBOLS)


class _Book:
    """Shared mutable L2 book: price-tick -> resting size, per side.

    `apply_delta` SETS the absolute size at a level (all three venues quote
    absolute remaining size, unlike Kalshi's additive deltas). `apply_snapshot`
    replaces both sides wholesale. Subclasses implement only `apply_frame`."""

    __slots__ = ("bids", "asks")

    def __init__(self) -> None:
        self.bids: dict[int, float] = {}
        self.asks: dict[int, float] = {}

    def _side(self, side: str) -> dict[int, float]:
        s = side.lower()
        if s in ("bid", "buy", "b"):
            return self.bids
        if s in ("ask", "sell", "a", "s"):
            return self.asks
        raise ValueError(f"unknown side: {side!r}")

    def apply_snapshot(self, bids: Iterable, asks: Iterable) -> None:
        """Replace BOTH sides wholesale (drop any zero/empty levels)."""
        self.bids = {_tick(p): float(q) for p, q in _pairs(bids) if float(q) > 0}
        self.asks = {_tick(p): float(q) for p, q in _pairs(asks) if float(q) > 0}

    def apply_delta(self, side: str, price, qty) -> None:
        """SET the absolute size at one level; remove it if qty <= 0."""
        book = self._side(side)
        t = _tick(price)
        q = float(qty)
        if q > 1e-12:
            book[t] = q
        else:
            book.pop(t, None)

    def apply_frame(self, inner: dict) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def best_bid(self) -> Optional[float]:
        return _price(max(self.bids)) if self.bids else None

    def best_ask(self) -> Optional[float]:
        return _price(min(self.asks)) if self.asks else None

    def is_crossed(self) -> bool:
        """True iff best_bid > best_ask. Delta-reconstructed books (Kraken,
        Gemini) never cross; Bitstamp's throttled periodic `order_book`
        snapshots self-cross ~0.2% of ticks at dust sizes (verified on real
        bronze 2026-05-29) — a known feed artifact, not a reconstruction bug."""
        b, a = self.best_bid(), self.best_ask()
        return b is not None and a is not None and b > a

    def mid(self) -> Optional[float]:
        """Top-of-book mid in USD, or None if either side is empty OR the book
        is crossed. Guarding the cross keeps EVERY emitted mid coherent — the
        multi-venue averager simply skips a venue on a tick with no clean mid.
        A consumer that wants the raw (possibly-crossed) top-of-book can read
        `best_bid()` / `best_ask()` directly."""
        b, a = self.best_bid(), self.best_ask()
        if b is None or a is None or b > a:
            return None
        return (b + a) / 2.0


class KrakenBook(_Book):
    """Kraken v2 `book`: per-symbol snapshot/update of {price,qty} levels."""

    def apply_frame(self, inner: dict) -> None:
        t = inner.get("type")
        for entry in inner.get("data", []):
            bids = entry.get("bids", [])
            asks = entry.get("asks", [])
            if t == "snapshot":
                self.apply_snapshot(bids, asks)
            elif t == "update":
                for lv in bids:
                    self.apply_delta("bid", lv["price"], lv["qty"])
                for lv in asks:
                    self.apply_delta("ask", lv["price"], lv["qty"])
            # other types (heartbeat/status) leave the book untouched


class BitstampBook(_Book):
    """Bitstamp `order_book`: every message is a FULL top-N snapshot."""

    def apply_frame(self, inner: dict) -> None:
        data = inner.get("data") or {}
        if "bids" in data or "asks" in data:
            self.apply_snapshot(data.get("bids", []), data.get("asks", []))


class GeminiBook(_Book):
    """Gemini `l2`: l2_updates whose changes SET absolute size per level."""

    def apply_frame(self, inner: dict) -> None:
        if inner.get("type") != "l2_updates":
            return  # ignore trade / auction_indicative / heartbeat
        for change in inner.get("changes", []):
            if len(change) < 3:
                continue  # skip a truncated/malformed row (don't abort the replay)
            side, price, qty = change[0], change[1], change[2]
            self.apply_delta(side, price, qty)


def mid_at(frames: Sequence, cutoff_epoch: float, book: _Book) -> Optional[float]:
    """Replay `(recv_epoch, inner)` frames in order, applying only those with
    `recv_epoch <= cutoff_epoch` (NO look-ahead past the decision time), and
    return the book mid at the cutoff (None if either side is empty).

    `frames` must be sorted ascending by recv_epoch; `book` is a fresh
    venue-appropriate instance supplied by the caller."""
    for ts, inner in frames:
        if ts > cutoff_epoch:
            break
        book.apply_frame(inner)
    return book.mid()


def parse_envelope(line) -> tuple:
    """Bronze JSONL line (str or dict) -> (_wire_recv_ts, inner_frame_dict)."""
    env = json.loads(line) if isinstance(line, str) else line
    inner = json.loads(env["_raw"])
    return env.get("_wire_recv_ts"), inner


def _epoch(iso: str) -> float:
    """ISO-8601 `_wire_recv_ts` (e.g. '2026-05-29T05:26:45.282318Z') -> unix s."""
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _extract_frame(venue: str, inner: dict, symbol: str) -> Optional[dict]:
    """Return the frame to keep for (venue, symbol), or None if it doesn't match.

    For Kraken the returned dict is a SHALLOW COPY narrowed to the requested
    symbol's `data` entries (a single Kraken frame can batch multiple symbols);
    the caller's parsed `inner` is never mutated, so the same frame can be
    extracted again for a different symbol. Bitstamp/Gemini frames carry one
    symbol each and are returned as-is."""
    if venue == "kraken":
        data = [e for e in inner.get("data", []) if e.get("symbol") == symbol]
        if not data:
            return None
        return {**inner, "data": data}
    if venue == "bitstamp":
        return inner if inner.get("channel") == f"order_book_{symbol}" else None
    if venue == "gemini":
        if inner.get("type") == "l2_updates" and inner.get("symbol") == symbol:
            return inner
        return None
    return None


def load_venue_frames(jsonl_path: str, venue: str, asset: str) -> list:
    """Load `(recv_epoch, inner)` frames for ONE venue+asset from a plain-JSONL
    bronze file (typically grep-prefiltered to the venue), sorted ascending by
    recv_epoch. Mirrors `load_frames_jsonl` in phase1b_real_price_economics.py.

    Raises KeyError if the venue doesn't constitute the asset (no fabrication).

    COMPLETENESS is the consumer's responsibility (same posture as the Kalshi
    sibling, where `phase1b_real_price_economics.py` checks for a snapshot frame
    before trusting the book — KalshiBook itself does not). Per venue, before
    relying on `mid_at` at a cutoff, ensure a baseline precedes it:
      - Kraken: a `type == "snapshot"` frame must appear at/before the cutoff
        (Kraken snapshots once per conn; a window that starts mid-stream has only
        absolute-set updates and a partial book). Check
        `any(i.get("type") == "snapshot" for _, i in frames if ...)`.
      - Bitstamp: self-complete — every frame is a full top-N snapshot, so any
        frame at/before the cutoff yields a complete book.
      - Gemini: the first `l2_updates` per symbol IS the snapshot but carries NO
        distinguishing marker, so start the frame window at the conn's session
        start to guarantee the baseline is included."""
    symbol = VENUE_SYMBOLS[venue][asset]  # KeyError on unsupported (venue,asset)
    expect_source = f"{venue}_ws"
    out: list = []
    with open(jsonl_path) as fh:
        for line in fh:
            if not line.strip():
                continue
            env = json.loads(line)
            src = env.get("_source")
            if src is not None and src != expect_source:
                continue
            ts, inner = parse_envelope(env)
            frame = _extract_frame(venue, inner, symbol)
            if frame is None:
                continue
            out.append((_epoch(ts), frame))
    out.sort(key=lambda x: x[0])
    return out
