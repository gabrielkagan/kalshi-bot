"""B2b-1 — SyntheticRTIFeed core (in-bot multi-venue L2 -> synthetic RTI), shadow by default.

Ticket 86ba64h2w (program 86ba64gyq). Plan: kb/decisions/b2b-1-core-shadow-plan.md.

These pin the OFFLINE-testable core of bot/feeds/synthetic_rti_feed.py
(parse venue frame -> per-venue L2 book via the module-level _CFB_PARAMS table
-> compute_synthetic_rti), the kill-switch, the off-hot-path sampler/cache, and
the Kraken CRC32 desync-drop. The live-WS thread + scanner wiring +
evaluated_opportunities schema are exercised by sibling tests.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import bot.feeds.synthetic_rti_feed as feed_mod
from bot.feeds.synthetic_rti_feed import SyntheticRTIFeed


# ── ws frame-size regression (86ba67npq) ───────────────────────────────────
# Coinbase level2_batch full-book SNAPSHOTS are ~1.04 MB, which exceeds the
# python-websockets default max_size of 1 MiB (1048576) → the lib closes the
# conn with 1009 "message too big" before the frame is delivered → infinite
# reconnect (observed 29x/2min on the 2026-05-28 flip; postmortem
# kb/failures/b2b-1-shadow-flip-deploy-may28.md). _ws_venue MUST raise
# max_size. Mirrors the D1.3-fu1 lesson at coinbase_wire/ws_client.py
# (ws_max_size=16 MiB). This closes the verification gap: the original 25
# tests used tiny synthetic frames and never exercised a >1 MiB snapshot.

# Largest Coinbase snapshot observed before the lib rejected it (bytes).
_OBSERVED_COINBASE_SNAPSHOT_BYTES = 1_043_358
_WEBSOCKETS_DEFAULT_MAX_SIZE = 1 << 20  # 1 MiB — the footgun default.


def test_ws_max_size_constant_exceeds_coinbase_snapshot():
    """The feed must define a max_size well above the observed Coinbase
    snapshot (or None = unbounded)."""
    assert hasattr(feed_mod, "_WS_MAX_SIZE"), (
        "_ws_venue must use an explicit _WS_MAX_SIZE (the websockets 1 MiB "
        "default rejects Coinbase level2_batch snapshots → 1009 flap)")
    mx = feed_mod._WS_MAX_SIZE
    assert mx is None or mx > _OBSERVED_COINBASE_SNAPSHOT_BYTES, (
        f"_WS_MAX_SIZE={mx} must exceed the observed Coinbase snapshot "
        f"{_OBSERVED_COINBASE_SNAPSHOT_BYTES} bytes")
    assert mx is None or mx > _WEBSOCKETS_DEFAULT_MAX_SIZE, (
        "_WS_MAX_SIZE must exceed the python-websockets 1 MiB default")


def test_ws_venue_passes_max_size_to_connect():
    """AST pin: the websockets.connect call inside the feed must pass a
    max_size keyword (so a future edit can't silently drop it back to the
    1 MiB default)."""
    src = Path(inspect.getfile(feed_mod)).read_text()
    tree = ast.parse(src)
    connect_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute) and n.func.attr == "connect"
    ]
    assert connect_calls, "no websockets.connect call found in the feed"
    for call in connect_calls:
        kw = {k.arg for k in call.keywords}
        assert "max_size" in kw, (
            f"websockets.connect at line {call.lineno} must pass max_size "
            "(else Coinbase level2_batch snapshots trip the 1 MiB 1009 limit)")


# ── frame builders (native per-venue WS shapes) ────────────────────────────


def _coinbase_l2_snapshot(product_id, bids, asks):
    return {"type": "snapshot", "product_id": product_id,
            "bids": [[str(p), str(s)] for p, s in bids],
            "asks": [[str(p), str(s)] for p, s in asks]}


def _kraken_book_snapshot(symbol, bids, asks, checksum=None):
    d = {"symbol": symbol,
         "bids": [{"price": p, "qty": q} for p, q in bids],
         "asks": [{"price": p, "qty": q} for p, q in asks]}
    if checksum is not None:
        d["checksum"] = checksum
    return {"channel": "book", "type": "snapshot", "data": [d]}


def _bitstamp_snapshot(pair, bids, asks):
    return {"event": "data", "channel": f"order_book_{pair}",
            "data": {"bids": [[str(p), str(s)] for p, s in bids],
                     "asks": [[str(p), str(s)] for p, s in asks]}}


# ── core behaviour ─────────────────────────────────────────────────────────


def test_consolidates_two_venues_to_rti():
    """Two venues, identical 1-level books bid 100.0 / ask 100.1 -> the
    consolidated RTI is the mid 100.05 (PV curve over one level is flat,
    so independent of the per-asset spacing)."""
    feed = SyntheticRTIFeed(enabled=True)
    feed.ingest_frame("coinbase", _coinbase_l2_snapshot("BTC-USD", [(100.0, 5.0)], [(100.1, 5.0)]))
    feed.ingest_frame("kraken", _kraken_book_snapshot("BTC/USD", [("100.0", "5.0")], [("100.1", "5.0")]))
    rti, n = feed.synthetic("BTC")
    assert rti == pytest.approx(100.05, abs=1e-6)
    assert n == 2  # both venues contributed


def test_returns_none_when_no_frames():
    feed = SyntheticRTIFeed(enabled=True)
    rti, n = feed.synthetic("BTC")
    assert rti is None and n == 0


def test_disabled_kill_switch_returns_none_even_with_data():
    """enabled=False is the default-shadow kill-switch: no synthetic is
    produced regardless of ingested data (the COMPUTE is gated, not decisions)."""
    feed = SyntheticRTIFeed(enabled=False)
    feed.ingest_frame("coinbase", _coinbase_l2_snapshot("BTC-USD", [(100.0, 5.0)], [(100.1, 5.0)]))
    rti, n = feed.synthetic("BTC")
    assert rti is None and n == 0


def test_kraken_desync_on_bad_checksum_drops_that_venue():
    """A Kraken snapshot whose v2 CRC32 checksum does not match the book is
    treated as desynced and EXCLUDED — only the synced venues contribute."""
    feed = SyntheticRTIFeed(enabled=True)
    feed.ingest_frame("coinbase", _coinbase_l2_snapshot("BTC-USD", [(100.0, 5.0)], [(100.1, 5.0)]))
    # Deliberately wrong checksum (1 is not the real CRC32 of this book).
    feed.ingest_frame("kraken", _kraken_book_snapshot("BTC/USD", [("100.0", "5.0")], [("100.1", "5.0")], checksum=1))
    rti, n = feed.synthetic("BTC")
    assert n == 1  # kraken dropped, only coinbase contributes
    assert rti == pytest.approx(100.05, abs=1e-6)


def test_bitstamp_full_snapshot_contributes():
    feed = SyntheticRTIFeed(enabled=True)
    feed.ingest_frame("coinbase", _coinbase_l2_snapshot("BTC-USD", [(100.0, 5.0)], [(100.1, 5.0)]))
    feed.ingest_frame("bitstamp", _bitstamp_snapshot("btcusd", [(100.0, 5.0)], [(100.1, 5.0)]))
    rti, n = feed.synthetic("BTC")
    assert rti == pytest.approx(100.05, abs=1e-6)
    assert n == 2


# ── per-venue staleness drop ───────────────────────────────────────────────


def test_all_stale_returns_none():
    feed = SyntheticRTIFeed(enabled=True)
    feed.ingest_frame("coinbase", _coinbase_l2_snapshot("BTC-USD", [(100.0, 5.0)], [(100.1, 5.0)]), now=0.0)
    feed.ingest_frame("kraken", _kraken_book_snapshot("BTC/USD", [("100.0", "5.0")], [("100.1", "5.0")]), now=0.0)
    rti, n = feed.synthetic("BTC", now=100.0)
    assert rti is None and n == 0


def test_fresh_venue_survives_stale_peer():
    """Coinbase fresh at t=100, Kraken stale (t=0) → only coinbase contributes."""
    feed = SyntheticRTIFeed(enabled=True)
    feed.ingest_frame("kraken", _kraken_book_snapshot("BTC/USD", [("100.0", "5.0")], [("100.1", "5.0")]), now=0.0)
    feed.ingest_frame("coinbase", _coinbase_l2_snapshot("BTC-USD", [(100.0, 5.0)], [(100.1, 5.0)]), now=100.0)
    rti, n = feed.synthetic("BTC", now=100.0)
    assert n == 1
    assert rti == pytest.approx(100.05, abs=1e-6)


# ── Kraken parse_float=str precision invariant ─────────────────────────────


def test_on_ws_frame_kraken_preserves_trailing_zero_precision():
    """The live WS path MUST json-parse Kraken with parse_float=str so the
    checksum tokens keep trailing zeros ("100.10" must not collapse to
    "100.1"). _on_ws_frame takes the RAW string and must do this internally.
    A book whose checksum matches stays in-sync (contributes)."""
    import json
    import zlib
    from bot.feeds.synthetic_rti_feed import _kraken_fmt

    feed = SyntheticRTIFeed(enabled=True)
    # Prices with a trailing zero that float()->str() would destroy.
    asks = [("100.10", "5.00")]
    bids = [("100.00", "5.00")]
    # Compute the correct CRC32 over the STRING tokens (top-10 asks then bids).
    tokens = []
    for p, q in asks:
        tokens.append(_kraken_fmt(p)); tokens.append(_kraken_fmt(q))
    for p, q in bids:
        tokens.append(_kraken_fmt(p)); tokens.append(_kraken_fmt(q))
    checksum = zlib.crc32("".join(tokens).encode())
    frame = {"channel": "book", "type": "snapshot",
             "data": [{"symbol": "BTC/USD",
                       "bids": [{"price": p, "qty": q} for p, q in bids],
                       "asks": [{"price": p, "qty": q} for p, q in asks],
                       "checksum": checksum}]}
    feed.ingest_frame("coinbase", _coinbase_l2_snapshot("BTC-USD", [(100.0, 5.0)], [(100.1, 5.0)]))
    # Route the RAW string (json.dumps drops the trailing zero on floats, but
    # the test sends strings, so the on-wire form preserves them).
    feed._on_ws_frame("kraken", json.dumps(frame))
    rti, n = feed.synthetic("BTC")
    assert n == 2  # kraken stayed in-sync → contributed


# ── off-hot-path sampler + cache ───────────────────────────────────────────


def test_cache_empty_before_sample():
    feed = SyntheticRTIFeed(enabled=True)
    feed.ingest_frame("coinbase", _coinbase_l2_snapshot("BTC-USD", [(100.0, 5.0)], [(100.1, 5.0)]))
    feed.ingest_frame("kraken", _kraken_book_snapshot("BTC/USD", [("100.0", "5.0")], [("100.1", "5.0")]))
    # No sampler pass yet → cache miss.
    rti, n, conf = feed.get_cached_synthetic("BTC")
    assert rti is None and n == 0 and conf is None


def test_sampler_populates_cache_with_confidence():
    feed = SyntheticRTIFeed(enabled=True)
    feed.ingest_frame("coinbase", _coinbase_l2_snapshot("BTC-USD", [(100.0, 5.0)], [(100.1, 5.0)]), now=0.0)
    feed.ingest_frame("kraken", _kraken_book_snapshot("BTC/USD", [("100.0", "5.0")], [("100.1", "5.0")]), now=0.0)
    feed._refresh_cache(now=0.0)
    rti, n, conf = feed.get_cached_synthetic("BTC", now=0.0)
    assert rti == pytest.approx(100.05, abs=1e-6)
    assert n == 2
    # BTC expects 4 venues (coinbase/kraken/bitstamp/gemini); 2 present.
    assert feed.n_expected_venues("BTC") == 4
    assert conf == pytest.approx(2 / 4, abs=1e-9)


def test_cached_synthetic_goes_stale():
    """A cache entry older than the freshness window reads as a miss so the
    scanner never logs a synthetic computed many ticks ago."""
    feed = SyntheticRTIFeed(enabled=True)
    feed.ingest_frame("coinbase", _coinbase_l2_snapshot("BTC-USD", [(100.0, 5.0)], [(100.1, 5.0)]), now=0.0)
    feed.ingest_frame("kraken", _kraken_book_snapshot("BTC/USD", [("100.0", "5.0")], [("100.1", "5.0")]), now=0.0)
    feed._refresh_cache(now=0.0)
    assert feed.get_cached_synthetic("BTC", now=0.0)[0] is not None
    # 1000s later the cached value is far past the freshness window.
    rti, n, conf = feed.get_cached_synthetic("BTC", now=1000.0)
    assert rti is None and n == 0 and conf is None


def test_cache_disabled_returns_none():
    feed = SyntheticRTIFeed(enabled=False)
    feed.ingest_frame("coinbase", _coinbase_l2_snapshot("BTC-USD", [(100.0, 5.0)], [(100.1, 5.0)]))
    feed._refresh_cache()
    assert feed.get_cached_synthetic("BTC") == (None, 0, None)


def test_start_stop_noop_when_disabled():
    """The kill-switch gates the WS + sampler threads entirely — start() on a
    disabled feed spins up NO threads (zero footprint = scan latency
    unaffected proof)."""
    feed = SyntheticRTIFeed(enabled=False)
    feed.start()
    assert not feed.is_running()
    feed.stop()  # must be safe even though nothing started
