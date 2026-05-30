"""Tests for the multi-venue L2 book reconstruction (B2b synthetic-RTI).

Hand-checked fixtures for Kraken / Bitstamp / Gemini, mirroring the proven
`test_kalshi_book_reconstruct.py` shape. The reconstructors turn each venue's
raw L2 frames (snapshot + delta) into a never-crossed best_bid/best_ask/mid so
the multi-venue averager can rebuild the CF Benchmarks RTI.

All three venues quote ABSOLUTE size at a level (kraken `update` qty, gemini
`l2_updates` change qty, bitstamp full `order_book` snapshot) — unlike Kalshi's
additive deltas — so `apply_delta` here is a SET, not an add. The fixtures pin
that semantics explicitly.
"""
from __future__ import annotations

import json
import os

import pytest

from scripts.research import venue_book_reconstruct as vbr


# --------------------------------------------------------------------------- #
# Kraken (v2 `book` channel): {type, data:[{symbol, bids:[{price,qty}], asks}]}
# --------------------------------------------------------------------------- #
def test_kraken_snapshot_then_update():
    b = vbr.KrakenBook()
    b.apply_frame({
        "channel": "book", "type": "snapshot",
        "data": [{"symbol": "BTC/USD",
                  "bids": [{"price": 100.0, "qty": 1.0}, {"price": 99.5, "qty": 2.0}],
                  "asks": [{"price": 100.5, "qty": 1.5}, {"price": 101.0, "qty": 3.0}]}],
    })
    assert b.best_bid() == 100.0
    assert b.best_ask() == 100.5
    assert b.mid() == 100.25

    # update: qty=0 REMOVES the top bid; a new bid at 100.2 is added (absolute set)
    b.apply_frame({
        "type": "update",
        "data": [{"symbol": "BTC/USD",
                  "bids": [{"price": 100.0, "qty": 0.0}, {"price": 100.2, "qty": 1.0}],
                  "asks": []}],
    })
    assert b.best_bid() == 100.2
    assert b.best_ask() == 100.5
    assert b.mid() == 100.35

    # update: a new ask at 100.3 tightens the spread (still never crossed)
    b.apply_frame({"type": "update",
                   "data": [{"symbol": "BTC/USD", "bids": [],
                             "asks": [{"price": 100.3, "qty": 2.0}]}]})
    assert b.best_ask() == 100.3
    assert b.best_bid() <= b.best_ask()  # never crossed
    assert b.mid() == pytest.approx(100.25)


# --------------------------------------------------------------------------- #
# Bitstamp (`order_book` channel): full top-N snapshot each message.
# --------------------------------------------------------------------------- #
def test_bitstamp_full_snapshot_each_frame():
    b = vbr.BitstampBook()
    b.apply_frame({
        "channel": "order_book_btcusd", "event": "data",
        "data": {"timestamp": "1", "microtimestamp": "1000000",
                 "bids": [["100.00", "1.0"], ["99.50", "2.0"]],
                 "asks": [["100.50", "1.5"], ["101.00", "2.0"]]},
    })
    assert b.best_bid() == 100.0
    assert b.best_ask() == 100.5
    assert b.mid() == 100.25

    # next frame is a COMPLETE replacement (not a diff) — old levels gone
    b.apply_frame({
        "channel": "order_book_btcusd", "event": "data",
        "data": {"bids": [["100.20", "1.0"]], "asks": [["100.40", "1.0"]]},
    })
    assert b.best_bid() == 100.2
    assert b.best_ask() == 100.4
    assert b.mid() == pytest.approx(100.3)
    assert b.best_bid() <= b.best_ask()


# --------------------------------------------------------------------------- #
# Gemini (`l2` channel): {type:"l2_updates", symbol, changes:[[side,price,qty]]}
# --------------------------------------------------------------------------- #
def test_gemini_l2_updates_absolute_set_and_remove():
    b = vbr.GeminiBook()
    # first l2_updates is the full snapshot (all levels present)
    b.apply_frame({
        "type": "l2_updates", "symbol": "BTCUSD",
        "changes": [["buy", "100.00", "1.0"], ["buy", "99.50", "2.0"],
                    ["sell", "100.50", "1.5"], ["sell", "101.00", "2.0"]],
    })
    assert b.best_bid() == 100.0
    assert b.best_ask() == 100.5
    assert b.mid() == 100.25

    # delta: remove 100.00 (qty 0), add a higher bid 100.10
    b.apply_frame({"type": "l2_updates", "symbol": "BTCUSD",
                   "changes": [["buy", "100.00", "0.00"], ["buy", "100.10", "1.0"]]})
    assert b.best_bid() == pytest.approx(100.1)
    assert b.best_ask() == 100.5
    assert b.mid() == pytest.approx(100.3)

    # non-book frames (trade / auction) are ignored
    b.apply_frame({"type": "trade", "symbol": "BTCUSD", "price": "100.2"})
    assert b.best_bid() == pytest.approx(100.1)
    assert b.best_bid() <= b.best_ask()


def test_crossed_snapshot_keeps_raw_top_but_guards_mid():
    """Bitstamp's periodic `order_book` snapshot can self-cross at dust sizes
    (real bronze: raw max_bid > raw min_ask, ~0.2% of ticks). best_bid/best_ask
    stay faithful to the raw book; is_crossed() flags it; mid() refuses to emit
    from an incoherent book."""
    b = vbr.BitstampBook()
    b.apply_frame({"channel": "order_book_btcusd", "event": "data",
                   "data": {"bids": [["73483.01", "0.045"], ["73482.01", "1.8"]],
                            "asks": [["73482.02", "0.225"], ["73483.45", "0.0068"]]}})
    assert b.best_bid() == pytest.approx(73483.01)  # raw top-of-book preserved
    assert b.best_ask() == pytest.approx(73482.02)
    assert b.is_crossed() is True
    assert b.mid() is None                          # no mid emitted while crossed
    # the next (clean) snapshot recovers a coherent mid
    b.apply_frame({"channel": "order_book_btcusd", "event": "data",
                   "data": {"bids": [["73482.00", "1.0"]], "asks": [["73483.00", "1.0"]]}})
    assert b.is_crossed() is False
    assert b.mid() == pytest.approx(73482.5)


def test_apply_delta_absolute_set_semantics():
    # apply_delta SETS the level (overwrite), unlike Kalshi's additive deltas.
    for cls in (vbr.KrakenBook, vbr.BitstampBook, vbr.GeminiBook):
        b = cls()
        b.apply_delta("bid", "100.0", "1.0")
        b.apply_delta("bid", "100.0", "5.0")   # overwrite, not +1+5
        b.apply_delta("ask", "101.0", "2.0")
        assert b.best_bid() == 100.0
        assert b.best_ask() == 101.0
        assert b.bids[vbr._tick("100.0")] == 5.0
        b.apply_delta("bid", "100.0", "0")     # zero removes the level
        assert b.best_bid() is None


# --------------------------------------------------------------------------- #
# mid_at — no look-ahead past the decision cutoff.
# --------------------------------------------------------------------------- #
def test_mid_at_no_look_ahead():
    snap = {"type": "snapshot",
            "data": [{"symbol": "BTC/USD",
                      "bids": [{"price": 100.0, "qty": 1.0}],
                      "asks": [{"price": 100.5, "qty": 1.0}]}]}
    # a LATER frame moves the market down hard — must be excluded before cutoff
    later = {"type": "update",
             "data": [{"symbol": "BTC/USD",
                       "bids": [{"price": 100.0, "qty": 0.0}, {"price": 90.0, "qty": 1.0}],
                       "asks": [{"price": 90.5, "qty": 1.0}, {"price": 100.5, "qty": 0.0}]}]}
    frames = [(1000.0, snap), (2000.0, later)]

    assert vbr.mid_at(frames, 1500.0, vbr.KrakenBook()) == 100.25  # only snap applied
    assert vbr.mid_at(frames, 2500.0, vbr.KrakenBook()) == pytest.approx(90.25)  # both
    # cutoff before any frame -> empty book -> None
    assert vbr.mid_at(frames, 500.0, vbr.KrakenBook()) is None


def test_parse_envelope_roundtrip():
    inner = {"type": "update", "data": [{"symbol": "BTC/USD", "bids": [], "asks": []}]}
    env = {"_wire_recv_ts": "2026-05-29T05:26:45.282318Z", "_source": "kraken_ws",
           "_raw": json.dumps(inner)}
    ts, parsed = vbr.parse_envelope(json.dumps(env))
    assert ts == "2026-05-29T05:26:45.282318Z"
    assert parsed == inner
    # dict input works too
    ts2, parsed2 = vbr.parse_envelope(env)
    assert (ts2, parsed2) == (ts, inner)


# --------------------------------------------------------------------------- #
# VENUE_SYMBOLS — only map the assets each venue actually constitutes.
# --------------------------------------------------------------------------- #
def test_venue_symbols_constituent_coverage():
    vs = vbr.VENUE_SYMBOLS
    assert set(vs["kraken"]) == {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"}
    assert set(vs["bitstamp"]) == {"BTC", "ETH", "SOL", "XRP", "HYPE"}  # no DOGE/BNB
    assert set(vs["gemini"]) == {"BTC", "ETH", "SOL", "DOGE"}            # no XRP/BNB/HYPE
    # symbol naming per venue (verified against real bronze 2026-05-29)
    assert vs["kraken"]["BTC"] == "BTC/USD"
    assert vs["bitstamp"]["BTC"] == "btcusd"
    assert vs["gemini"]["BTC"] == "BTCUSD"


# --------------------------------------------------------------------------- #
# load_venue_frames — filter to one venue+symbol, sorted ascending by recv_epoch.
# --------------------------------------------------------------------------- #
def _env(source, recv_ts, inner):
    return json.dumps({"_wire_recv_ts": recv_ts, "_source": source,
                       "_raw": json.dumps(inner)})


def test_load_venue_frames_filters_and_sorts(tmp_path):
    p = tmp_path / "kr.jsonl"
    btc = {"type": "update", "data": [{"symbol": "BTC/USD",
                                       "bids": [{"price": 100.0, "qty": 1.0}], "asks": []}]}
    eth = {"type": "update", "data": [{"symbol": "ETH/USD",
                                       "bids": [{"price": 50.0, "qty": 1.0}], "asks": []}]}
    # interleaved symbols + OUT OF ORDER timestamps to prove the sort
    p.write_text("\n".join([
        _env("kraken_ws", "2026-05-29T05:00:02.000000Z", btc),
        _env("kraken_ws", "2026-05-29T05:00:09.000000Z", eth),
        _env("kraken_ws", "2026-05-29T05:00:01.000000Z", btc),
    ]) + "\n")

    frames = vbr.load_venue_frames(str(p), "kraken", "BTC")
    assert len(frames) == 2  # ETH excluded
    epochs = [ts for ts, _ in frames]
    assert epochs == sorted(epochs)  # ascending
    # every retained frame's data is narrowed to the requested symbol
    for _, inner in frames:
        assert all(e["symbol"] == "BTC/USD" for e in inner["data"])


def test_load_venue_frames_bitstamp_channel_match(tmp_path):
    p = tmp_path / "bs.jsonl"
    btc = {"channel": "order_book_btcusd", "event": "data",
           "data": {"bids": [["100.00", "1.0"]], "asks": [["100.50", "1.0"]]}}
    xrp = {"channel": "order_book_xrpusd", "event": "data",
           "data": {"bids": [["2.00", "1.0"]], "asks": [["2.01", "1.0"]]}}
    p.write_text(_env("bitstamp_ws", "2026-05-29T05:00:01Z", btc) + "\n" +
                 _env("bitstamp_ws", "2026-05-29T05:00:02Z", xrp) + "\n")
    frames = vbr.load_venue_frames(str(p), "bitstamp", "BTC")
    assert len(frames) == 1
    assert frames[0][1]["channel"] == "order_book_btcusd"


def test_load_venue_frames_unsupported_asset_raises():
    # Gemini doesn't constitute XRP -> mapping absent -> KeyError (don't fabricate)
    with pytest.raises(KeyError):
        vbr.load_venue_frames("/dev/null", "gemini", "XRP")


def test_load_venue_frames_kraken_narrows_multisymbol_frame(tmp_path):
    """A single Kraken frame can batch multiple symbols' data. Loading one asset
    must narrow to that symbol ONLY (no other-symbol level bleed into the book)."""
    p = tmp_path / "kr.jsonl"
    multi = {"type": "update", "data": [
        {"symbol": "BTC/USD", "bids": [{"price": 100.0, "qty": 1.0}], "asks": []},
        {"symbol": "ETH/USD", "bids": [{"price": 50.0, "qty": 1.0}], "asks": []}]}
    p.write_text(_env("kraken_ws", "2026-05-29T05:00:01Z", multi) + "\n")

    btc = vbr.load_venue_frames(str(p), "kraken", "BTC")
    assert len(btc) == 1
    assert [e["symbol"] for e in btc[0][1]["data"]] == ["BTC/USD"]  # narrowed
    bb = vbr.KrakenBook()
    bb.apply_frame(btc[0][1])
    assert bb.best_bid() == 100.0  # only BTC level present, no ETH bleed


def test_extract_frame_does_not_mutate_source(tmp_path):
    """_extract_frame must narrow Kraken data WITHOUT mutating the caller's parsed
    frame, so the same `inner` can be safely extracted for a second symbol."""
    inner = {"type": "update", "data": [
        {"symbol": "BTC/USD", "bids": [{"price": 100.0, "qty": 1.0}], "asks": []},
        {"symbol": "ETH/USD", "bids": [{"price": 50.0, "qty": 1.0}], "asks": []}]}
    btc_frame = vbr._extract_frame("kraken", inner, "BTC/USD")
    assert [e["symbol"] for e in btc_frame["data"]] == ["BTC/USD"]
    # source untouched -> a subsequent ETH extraction still sees both entries
    assert [e["symbol"] for e in inner["data"]] == ["BTC/USD", "ETH/USD"]
    eth_frame = vbr._extract_frame("kraken", inner, "ETH/USD")
    assert [e["symbol"] for e in eth_frame["data"]] == ["ETH/USD"]
    # non-matching venue/symbol -> None
    assert vbr._extract_frame("kraken", inner, "SOL/USD") is None


def test_gemini_ignores_malformed_short_change():
    """A truncated change row must not abort the whole replay (one bad frame in a
    multi-hour replay would otherwise kill mid_at)."""
    b = vbr.GeminiBook()
    b.apply_frame({"type": "l2_updates", "symbol": "BTCUSD",
                   "changes": [["buy", "100.0", "1.0"], ["buy"], [],  # malformed rows
                               ["sell", "100.5", "1.0"]]})
    assert b.best_bid() == 100.0   # valid rows still applied
    assert b.best_ask() == 100.5


# --------------------------------------------------------------------------- #
# Optional real-bronze validation — runs only when a local sample is present.
# Set VBR_BRONZE_SAMPLE to a decompressed JSONL of venue envelopes to exercise.
# --------------------------------------------------------------------------- #
_BRONZE = os.environ.get("VBR_BRONZE_SAMPLE")


@pytest.mark.skipif(not _BRONZE or not os.path.exists(_BRONZE),
                    reason="no real-bronze sample (set VBR_BRONZE_SAMPLE)")
@pytest.mark.parametrize("venue,book_cls,delta_reconstructed", [
    ("kraken", vbr.KrakenBook, True),
    ("bitstamp", vbr.BitstampBook, False),  # periodic snapshot -> may self-cross
    ("gemini", vbr.GeminiBook, True)])
def test_real_bronze_emitted_mids_coherent(venue, book_cls, delta_reconstructed):
    frames = vbr.load_venue_frames(_BRONZE, venue, "BTC")
    if not frames:
        pytest.skip(f"no {venue} BTC frames in sample")
    book = book_cls()
    raw_crossed = 0
    for _, inner in frames:
        book.apply_frame(inner)
        if book.is_crossed():
            raw_crossed += 1
            assert book.mid() is None  # never emit a mid from a crossed book
        else:
            mid = book.mid()
            if mid is not None:
                assert book.best_bid() <= book.best_ask()  # emitted mid coherent
    if delta_reconstructed:
        # delta replay (Kraken/Gemini) yields a coherent book that never crosses
        assert raw_crossed == 0, f"{venue} delta book crossed {raw_crossed} times"
    else:
        # Bitstamp periodic snapshots self-cross rarely (dust); bound it
        assert raw_crossed <= 0.01 * len(frames), (
            f"{venue} raw-cross rate {raw_crossed}/{len(frames)} exceeds 1%")
