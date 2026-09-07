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
import asyncio
import inspect
import json
import logging
import time
import types
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


# ── venue reconnect book-desync regression (ticket 86bbvztem) ──────────────
# RCA: _ws_venue reconnects + resubscribes but NEVER clears self._books, so a
# venue resumes ingesting into its PRE-disconnect book.
#
#   coinbase / kraken  -> resubscribe replies with type="snapshot", which
#                         ingest_frame REPLACES the book with. Self-healed.
#   bitstamp           -> every order_book_<pair> frame is a full top-N book,
#                         parsed as "snapshot". Self-healed.
#   gemini             -> _parse_gemini maps EVERY l2_updates to "delta", but
#                         Gemini v2 sends the FULL book in the first
#                         l2_updates after subscribe (live-probed 2026-05-28;
#                         see collector/venue_l2_archiver.py module docstring).
#                         That full book was MERGED into the stale one, so
#                         levels that vanished during the outage were never
#                         removed and the book stayed wrong until process
#                         restart.
#
# Venue-membership evidence. Corrupt asset-days are a Track R measurement
# (2026-09), carried over, not re-derived here: BTC 62.1% / DOGE 26.2% / ETH
# 15.5% / SOL 8.7% corrupt; XRP / HYPE / BNB 0%. gemini is the
# UNIQUE venue whose _CFB_PARAMS constituent set {BTC, ETH, SOL, DOGE} is
# exactly the corrupt-asset set — no other venue's membership pattern
# separates the two groups.


def _gemini_l2_updates(symbol, bids, asks):
    """Gemini v2 l2_updates. The FIRST one after (re)subscribe carries the
    full book; later ones are incremental diffs. The wire shape is identical
    for both — which is exactly why the merge-into-stale bug was invisible."""
    changes = ([["buy", str(p), str(s)] for p, s in bids]
               + [["sell", str(p), str(s)] for p, s in asks])
    return {"type": "l2_updates", "symbol": symbol, "changes": changes}


def _coinbase_l2update(product_id, changes):
    """changes: [(side, price, size)] with side in {"buy", "sell"}."""
    return {"type": "l2update", "product_id": product_id,
            "changes": [[side, str(p), str(s)] for side, p, s in changes]}


def _kraken_book_update(symbol, bids, asks, checksum=None):
    d = {"symbol": symbol,
         "bids": [{"price": p, "qty": q} for p, q in bids],
         "asks": [{"price": p, "qty": q} for p, q in asks]}
    if checksum is not None:
        d["checksum"] = checksum
    return {"channel": "book", "type": "update", "data": [d]}


class _FakeWSSession:
    """Scripted stand-in for a websockets client connection: async CM +
    async iterator that yields the session's frames then ends cleanly (the
    ConnectionClosedOK path — `async for` stops without raising)."""

    def __init__(self, frames, on_exhausted=None):
        self._frames = list(frames)
        self._on_exhausted = on_exhausted
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for frame in self._frames:
            yield frame
        if self._on_exhausted is not None:
            self._on_exhausted()


def _drive_ws_venue(feed, venue, sessions, monkeypatch, subscribe_frames=None):
    """Run feed._ws_venue against a scripted list of WS sessions.

    Each element of ``sessions`` is a list of dict frames delivered by one
    connection; the connection then closes CLEANLY, which drives the
    reconnect path. The LAST session signals stop once its frames are
    exhausted, so the loop takes the post-session stop-check exit. That
    leaves the last session's books observable (a reconnect deliberately
    clears them, so a run ending on a reconnect would have nothing to assert
    against). This drives the post-session stop-check exit. A real `stop()`
    may reach a venue by this exit OR by cancellation — which one is a race
    (see `SyntheticRTIFeed.stop`), so neither this harness nor any assertion
    here claims a frequency.
    `test_cancellation_exits_the_venue_loop_without_reconnecting` drives the
    cancellation exit.

    The reconnect backoff sleep is stubbed to zero delay (the sleep is not
    what these tests are about) while preserving its stop-signal contract.
    """
    sessions = list(sessions)
    calls = {"n": 0}

    def fake_connect(url, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        if i >= len(sessions):  # defensive: the last session stops the loop
            feed._ws_stop_event.set()
            raise RuntimeError("no more scripted sessions")
        last = i == len(sessions) - 1
        return _FakeWSSession(
            [json.dumps(f) for f in sessions[i]],
            on_exhausted=feed._ws_stop_event.set if last else None)

    monkeypatch.setattr(feed_mod, "websockets",
                        types.SimpleNamespace(connect=fake_connect))

    async def _no_sleep(_wait):
        return feed._ws_stop_event.is_set()

    monkeypatch.setattr(feed, "_sleep_or_stop", _no_sleep)

    async def _run():
        feed._ws_stop_event = asyncio.Event()
        await feed._ws_venue(venue, subscribe_frames or [{"type": "subscribe"}])

    asyncio.run(_run())
    return calls["n"]


def test_gemini_reconnect_does_not_merge_into_stale_book(monkeypatch):
    """THE regression. Session 1 books BTC around 100; the venue drops; the
    market moves to 90; session 1's levels are gone from the venue's real
    book. The post-reconnect full book must REPLACE, not merge — otherwise
    the stale 100.0 bid survives above the live 90.1 ask (a crossed,
    permanently-wrong book that only a process restart clears)."""
    feed = SyntheticRTIFeed(enabled=True)
    session1 = [_gemini_l2_updates(
        "BTCUSD", [(100.0, 5.0), (99.0, 5.0)], [(100.1, 5.0), (101.0, 5.0)])]
    session2 = [_gemini_l2_updates(
        "BTCUSD", [(90.0, 5.0), (89.0, 5.0)], [(90.1, 5.0), (91.0, 5.0)])]

    _drive_ws_venue(feed, "gemini", [session1, session2], monkeypatch)

    book = feed._books.get(("gemini", "BTC"))
    assert book is not None, "session 2 must have rebuilt the book"
    bids, asks = book.levels()
    assert dict(bids) == {90.0: 5.0, 89.0: 5.0}, (
        "pre-disconnect bids survived the reconnect — the venue's books were "
        "not cleared, so the full-book first frame was merged, not replaced")
    assert dict(asks) == {90.1: 5.0, 91.0: 5.0}
    assert bids[0][0] < asks[0][0], "book is crossed after reconnect"


def test_reconnect_clears_only_the_reconnecting_venue(monkeypatch):
    """A gemini reconnect must not disturb the other venues' books."""
    feed = SyntheticRTIFeed(enabled=True)
    feed.ingest_frame(
        "coinbase",
        _coinbase_l2_snapshot("BTC-USD", [(100.0, 5.0)], [(100.1, 5.0)]),
        now=0.0)
    session1 = [_gemini_l2_updates("BTCUSD", [(100.0, 5.0)], [(100.1, 5.0)])]
    session2 = [_gemini_l2_updates("BTCUSD", [(95.0, 5.0)], [(95.1, 5.0)])]

    _drive_ws_venue(feed, "gemini", [session1, session2], monkeypatch)

    # The reset must have actually fired on the gemini side (otherwise this
    # test is vacuously true — pre-fix nothing cleared any venue)...
    gm = feed._books.get(("gemini", "BTC"))
    assert gm is not None and dict(gm.levels()[0]) == {95.0: 5.0}, (
        "gemini must have been rebuilt from session 2 alone")
    # ...and it must NOT have touched coinbase.
    cb = feed._books.get(("coinbase", "BTC"))
    assert cb is not None and cb.bids == {100.0: 5.0} and cb.asks == {100.1: 5.0}


def test_clean_close_logs_disconnect_marker(monkeypatch, caplog):
    """A server-side CLEAN close makes `async for` stop WITHOUT raising, so
    the pre-fix code skipped the except branch entirely: no disconnect log,
    no backoff, instant hot reconnect. Every session end must be visible."""
    feed = SyntheticRTIFeed(enabled=True)
    session = [_gemini_l2_updates("BTCUSD", [(100.0, 5.0)], [(100.1, 5.0)])]
    with caplog.at_level(logging.WARNING, logger=feed_mod.__name__):
        # 3 sessions: the first two close cleanly and must each be logged.
        # The third signals stop once its frames drain, so it produces no
        # reconnect and no log (fake_connect's raise is never reached).
        # Pre-fix NOTHING was logged here — a clean close skipped the except
        # branch entirely.
        _drive_ws_venue(feed, "gemini", [session, session, session], monkeypatch)
    marker_lines = [r.getMessage() for r in caplog.records
                    if feed_mod.DISCONNECT_LOG_MARKER in r.getMessage()
                    and "venue=gemini" in r.getMessage()]
    clean = [m for m in marker_lines if feed_mod.CLEAN_CLOSE_REASON in m]
    assert len(clean) == 2, (
        "a clean server close must still emit the disconnect marker — "
        f"otherwise reconnects are invisible in the journal; got {marker_lines}")


def test_reset_venue_excludes_book_until_a_snapshot_rebuilds_it():
    """After a (re)connect the venue's book is unusable until it has been
    REBUILT. A diff arriving before the post-resubscribe snapshot must not
    be allowed to contribute a half-built book to synthetic()."""
    feed = SyntheticRTIFeed(enabled=True)
    feed.ingest_frame(
        "coinbase",
        _coinbase_l2_snapshot("BTC-USD", [(100.0, 5.0)], [(100.1, 5.0)]),
        now=0.0)
    feed.ingest_frame(
        "kraken",
        _kraken_book_snapshot("BTC/USD", [("100.0", "5.0")], [("100.1", "5.0")]),
        now=0.0)
    assert feed.synthetic("BTC", now=0.0)[1] == 2

    feed.reset_venue("kraken")
    # A diff lands before the post-resubscribe snapshot.
    feed.ingest_frame(
        "kraken",
        _kraken_book_update("BTC/USD", [("100.0", "5.0")], [("100.1", "5.0")]),
        now=0.0)
    assert feed.synthetic("BTC", now=0.0)[1] == 1, (
        "a book rebuilt from diffs alone must not count as a constituent")

    # The real snapshot arrives -> the book is whole again.
    feed.ingest_frame(
        "kraken",
        _kraken_book_snapshot("BTC/USD", [("100.0", "5.0")], [("100.1", "5.0")]),
        now=0.0)
    assert feed.synthetic("BTC", now=0.0)[1] == 2


def test_delta_only_venue_rebuilds_from_its_first_frame():
    """Gemini has no distinct snapshot message type, so the reset must not
    strand it: its first l2_updates after (re)subscribe IS the full book and
    must both replace the book and mark it rebuilt."""
    feed = SyntheticRTIFeed(enabled=True)
    feed.ingest_frame(
        "coinbase",
        _coinbase_l2_snapshot("BTC-USD", [(100.0, 5.0)], [(100.1, 5.0)]),
        now=0.0)
    feed.ingest_frame(
        "gemini", _gemini_l2_updates("BTCUSD", [(100.0, 5.0)], [(100.1, 5.0)]),
        now=0.0)
    assert feed.synthetic("BTC", now=0.0)[1] == 2

    feed.reset_venue("gemini")
    assert feed.synthetic("BTC", now=0.0)[1] == 1, "excluded until rebuilt"

    feed.ingest_frame(
        "gemini", _gemini_l2_updates("BTCUSD", [(100.0, 5.0)], [(100.1, 5.0)]),
        now=0.0)
    assert feed.synthetic("BTC", now=0.0)[1] == 2, (
        "the first post-resubscribe l2_updates carries the full book")


def test_crossed_book_is_dropped_as_unhealthy():
    """A venue whose own top-of-book is crossed (best bid strictly above
    best ask) is definitionally corrupt — no matching engine publishes one.
    This is the cause-agnostic health check: it catches a wrong book even
    when no reconnect happened and the venue carries no checksum."""
    feed = SyntheticRTIFeed(enabled=True)
    feed.ingest_frame(
        "coinbase",
        _coinbase_l2_snapshot("BTC-USD", [(100.0, 5.0)], [(100.1, 5.0)]),
        now=0.0)
    feed.ingest_frame(
        "gemini", _gemini_l2_updates("BTCUSD", [(100.0, 5.0)], [(100.1, 5.0)]),
        now=0.0)
    assert feed.synthetic("BTC", now=0.0)[1] == 2

    # A diff puts an ask BELOW the standing bid -> crossed.
    feed.ingest_frame(
        "gemini", _gemini_l2_updates("BTCUSD", [], [(99.0, 5.0)]), now=0.0)
    assert feed.synthetic("BTC", now=0.0)[1] == 1, (
        "a crossed book must not contribute to the synthetic")


def test_confidence_falls_while_a_venue_is_awaiting_rebuild():
    """rti_confidence is the fraction of the CFB constituent set that
    contributed a USABLE book. The pre-fix defect made a reconnecting gemini
    keep contributing a stale book, so confidence read marginally HIGHER on
    corrupt rows than clean ones — the gate preferred bad data. A venue that
    is mid-rebuild must LOWER confidence."""
    feed = SyntheticRTIFeed(enabled=True)
    feed.ingest_frame(
        "coinbase",
        _coinbase_l2_snapshot("BTC-USD", [(100.0, 5.0)], [(100.1, 5.0)]),
        now=0.0)
    feed.ingest_frame(
        "kraken",
        _kraken_book_snapshot("BTC/USD", [("100.0", "5.0")], [("100.1", "5.0")]),
        now=0.0)
    feed.ingest_frame(
        "bitstamp", _bitstamp_snapshot("btcusd", [(100.0, 5.0)], [(100.1, 5.0)]),
        now=0.0)
    feed.ingest_frame(
        "gemini", _gemini_l2_updates("BTCUSD", [(100.0, 5.0)], [(100.1, 5.0)]),
        now=0.0)
    feed._refresh_cache(now=0.0)
    assert feed.get_cached_synthetic("BTC", now=0.0)[2] == pytest.approx(1.0)

    feed.reset_venue("gemini")
    feed._refresh_cache(now=0.0)
    _rti, n, conf = feed.get_cached_synthetic("BTC", now=0.0)
    assert n == 3
    assert conf == pytest.approx(3 / 4), (
        "a venue awaiting rebuild must reduce rti_confidence, not be counted")


def test_ws_venue_resets_books_on_every_connect():
    """AST pin: reset_venue must be called from inside _ws_venue's connect
    block, so a future edit cannot silently reintroduce the stale-book
    reconnect (the behavioural tests above route through the same path, but
    this pins WHERE the call lives)."""
    src = Path(inspect.getfile(feed_mod)).read_text()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "_ws_venue"),
              None)
    assert fn is not None, "_ws_venue not found"
    withs = [n for n in ast.walk(fn) if isinstance(n, ast.AsyncWith)]
    assert withs, "_ws_venue must open the connection with `async with`"
    in_connect = [
        n for w in withs for n in ast.walk(w)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "reset_venue"
    ]
    assert in_connect, (
        "_ws_venue must call self.reset_venue(venue) inside the connect "
        "block — every (re)connect starts a new book epoch")


def test_exclusion_counters_name_the_reason_a_venue_is_missing():
    """A low rti_constituent_count is unfalsifiable from the corpus alone —
    "venue absent" / "stale" / "awaiting rebuild" / "crossed" all render as
    the same missing integer. synthetic() tallies the reason so a journal
    grep can answer it (e.g. DOGE never reaching its 3rd constituent)."""
    feed = SyntheticRTIFeed(enabled=True)
    feed.ingest_frame(
        "coinbase",
        _coinbase_l2_snapshot("BTC-USD", [(100.0, 5.0)], [(100.1, 5.0)]),
        now=0.0)
    feed.ingest_frame(
        "kraken",
        _kraken_book_snapshot("BTC/USD", [("100.0", "5.0")], [("100.1", "5.0")]),
        now=0.0)
    feed.ingest_frame(
        "gemini", _gemini_l2_updates("BTCUSD", [(100.0, 5.0)], [(100.1, 5.0)]),
        now=0.0)
    feed.reset_venue("gemini")
    feed.synthetic("BTC", now=0.0)

    counts = feed.exclusion_counts()
    # bitstamp was never fed at all; gemini is mid-rebuild.
    assert counts.get(("bitstamp", "BTC", "absent")) == 1
    assert counts.get(("gemini", "BTC", "awaiting_rebuild")) == 1
    # the two healthy venues are not tallied
    assert not [k for k in counts if k[0] in ("coinbase", "kraken")]


def test_exclusion_summary_is_logged_and_cleared_on_an_interval(caplog):
    """The summary is rate-limited and drains its tallies, so the counters
    cannot grow without bound and the journal is not firehosed."""
    feed = SyntheticRTIFeed(enabled=True)
    feed.ingest_frame(
        "coinbase",
        _coinbase_l2_snapshot("BTC-USD", [(100.0, 5.0)], [(100.1, 5.0)]),
        now=0.0)
    with caplog.at_level(logging.INFO, logger=feed_mod.__name__):
        feed._refresh_cache(now=1000.0)
        assert feed.exclusion_counts() == {}, "tallies must drain on log"
        emitted = [r.getMessage() for r in caplog.records
                   if feed_mod.EXCLUSION_LOG_MARKER in r.getMessage()]
        assert len(emitted) == 1 and "bitstamp/BTC/absent=" in emitted[0]

        # A second pass inside the interval must NOT log again.
        feed._refresh_cache(now=1000.0 + feed_mod._EXCLUSION_LOG_INTERVAL_SECONDS / 2)
        again = [r.getMessage() for r in caplog.records
                 if feed_mod.EXCLUSION_LOG_MARKER in r.getMessage()]
        assert len(again) == 1, "summary must be rate-limited"


def test_short_session_escalates_backoff(monkeypatch):
    """A connect-and-die flap must escalate the reconnect backoff. Counting
    delivered frames was not enough: the 86ba67npq 1009 flap delivered a
    subscribe ack before dying, which would have pinned backoff at ~1s."""
    feed = SyntheticRTIFeed(enabled=True)
    session = [_gemini_l2_updates("BTCUSD", [(100.0, 5.0)], [(100.1, 5.0)])]
    waits = []

    async def _record_sleep(wait):
        waits.append(wait)
        return feed._ws_stop_event.is_set()

    calls = {"n": 0}
    # 4 short sessions: each ends well under _HEALTHY_SESSION_SECONDS.
    sessions = [session] * 4

    def fake_connect(url, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        if i >= len(sessions):
            feed._ws_stop_event.set()
            raise RuntimeError("no more scripted sessions")
        last = i == len(sessions) - 1
        return _FakeWSSession(
            [json.dumps(f) for f in sessions[i]],
            on_exhausted=feed._ws_stop_event.set if last else None)

    monkeypatch.setattr(feed_mod, "websockets",
                        types.SimpleNamespace(connect=fake_connect))
    monkeypatch.setattr(feed, "_sleep_or_stop", _record_sleep)

    async def _run():
        feed._ws_stop_event = asyncio.Event()
        await feed._ws_venue("gemini", [{"type": "subscribe"}])

    asyncio.run(_run())

    assert len(waits) == 3, waits
    # backoff base doubles 1 -> 2 -> 4; jitter is at most +25%.
    assert waits[0] < waits[1] < waits[2]
    assert waits[2] >= 4.0


def test_crossed_book_warning_is_edge_triggered_and_logged_outside_the_lock(
        caplog):
    """The crossed warning must fire ONCE per crossing, not once per sampler
    tick — at a 2s cadence across 7 assets a level-triggered log would
    firehose the journal. It must also be emitted after `self._lock` is
    released, since the asyncio reader thread blocks on that same lock."""
    feed = SyntheticRTIFeed(enabled=True)
    feed.ingest_frame(
        "coinbase",
        _coinbase_l2_snapshot("BTC-USD", [(100.0, 5.0)], [(100.1, 5.0)]),
        now=0.0)
    feed.ingest_frame(
        "gemini", _gemini_l2_updates("BTCUSD", [(100.0, 5.0)], [(100.1, 5.0)]),
        now=0.0)
    # Cross it.
    feed.ingest_frame(
        "gemini", _gemini_l2_updates("BTCUSD", [], [(99.0, 5.0)]), now=0.0)

    with caplog.at_level(logging.WARNING, logger=feed_mod.__name__):
        feed.synthetic("BTC", now=0.0)
        feed.synthetic("BTC", now=0.0)
        crossed = [r for r in caplog.records
                   if "synthetic_rti_book_crossed" in r.getMessage()]
        assert len(crossed) == 1, (
            f"crossed warning must be edge-triggered, got {len(crossed)}")
        assert "venue=gemini" in crossed[0].getMessage()

        # Uncrossing re-arms the trigger.
        feed.ingest_frame(
            "gemini", _gemini_l2_updates("BTCUSD", [], [(99.0, 0.0)]), now=0.0)
        feed.synthetic("BTC", now=0.0)          # healthy -> clears the flag
        feed.ingest_frame(
            "gemini", _gemini_l2_updates("BTCUSD", [], [(98.0, 5.0)]), now=0.0)
        feed.synthetic("BTC", now=0.0)          # crossed again -> logs again
        crossed = [r for r in caplog.records
                   if "synthetic_rti_book_crossed" in r.getMessage()]
        assert len(crossed) == 2

    # Asserting AFTER synthetic() returns proves nothing (the `with` block
    # has released either way). Probe the lock from INSIDE the log handler,
    # which is the only moment that distinguishes deferred emission from
    # emission under the lock.
    class _LockProbe(logging.Handler):
        def __init__(self):
            super().__init__()
            self.free_at_emit = []

        def emit(self, record):
            if "synthetic_rti_book_crossed" not in record.getMessage():
                return
            got = feed._lock.acquire(blocking=False)
            self.free_at_emit.append(got)
            if got:
                feed._lock.release()

    probe = _LockProbe()
    log = logging.getLogger(feed_mod.__name__)
    log.addHandler(probe)
    try:
        # Uncross so the edge trigger re-arms, then cross again so the
        # warning actually fires while the probe is installed.
        feed.ingest_frame(
            "gemini", _gemini_l2_updates("BTCUSD", [], [(98.0, 0.0)]), now=0.0)
        feed.synthetic("BTC", now=0.0)
        feed.ingest_frame(
            "gemini", _gemini_l2_updates("BTCUSD", [], [(97.0, 5.0)]), now=0.0)
        feed.synthetic("BTC", now=0.0)
    finally:
        log.removeHandler(probe)
    assert probe.free_at_emit == [True], (
        "the crossed warning must be emitted AFTER self._lock is released — "
        "the asyncio reader thread blocks on that same lock in ingest_frame")


def test_empty_first_gemini_frame_does_not_disarm_the_rebuild_guard():
    """An `l2_updates` with an empty `changes` array must NOT clear
    `awaiting_snapshot`: doing so would leave an EMPTY book marked rebuilt,
    and the next real diff would merge into it — the same partial-book class
    this Bit closes."""
    feed = SyntheticRTIFeed(enabled=True)
    feed.reset_venue("gemini")
    feed.ingest_frame(
        "gemini", {"type": "l2_updates", "symbol": "BTCUSD", "changes": []},
        now=0.0)
    book = feed._books.get(("gemini", "BTC"))
    assert book is None or book.awaiting_snapshot, (
        "an empty first frame must leave the book awaiting its real rebuild")

    # The real full book still rebuilds it.
    feed.ingest_frame(
        "gemini", _gemini_l2_updates("BTCUSD", [(100.0, 5.0)], [(100.1, 5.0)]),
        now=0.0)
    book = feed._books[("gemini", "BTC")]
    assert not book.awaiting_snapshot
    assert book.bids == {100.0: 5.0} and book.asks == {100.1: 5.0}


def test_books_are_cleared_during_reconnect_backoff(monkeypatch):
    """The connect-time reset masks this: without the SECOND reset on the
    disconnect path, a frozen pre-disconnect book keeps contributing for the
    whole of the asset's lag window (10-30s) on every reconnect. It is not
    crossed and not yet stale, so neither health check catches it — the only
    thing that does is clearing the book the moment the socket dies."""
    feed = SyntheticRTIFeed(enabled=True)
    feed.ingest_frame(
        "coinbase",
        _coinbase_l2_snapshot("BTC-USD", [(100.0, 5.0)], [(100.1, 5.0)]),
        now=0.0)
    session = [_gemini_l2_updates("BTCUSD", [(100.0, 5.0)], [(100.1, 5.0)])]
    observed = {}

    async def _observe_during_backoff(_wait):
        # We are inside the reconnect gap: the socket is gone but the lag
        # window has not expired. Gemini must NOT be a constituent here.
        _rti, n = feed.synthetic("BTC", now=0.0)
        observed["n"] = n
        observed["reasons"] = feed.exclusion_counts()
        return feed._ws_stop_event.is_set()

    calls = {"n": 0}
    sessions = [session, session]

    def fake_connect(url, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        if i >= len(sessions):
            feed._ws_stop_event.set()
            raise RuntimeError("no more scripted sessions")
        last = i == len(sessions) - 1
        return _FakeWSSession(
            [json.dumps(f) for f in sessions[i]],
            on_exhausted=feed._ws_stop_event.set if last else None)

    monkeypatch.setattr(feed_mod, "websockets",
                        types.SimpleNamespace(connect=fake_connect))
    monkeypatch.setattr(feed, "_sleep_or_stop", _observe_during_backoff)

    async def _run():
        feed._ws_stop_event = asyncio.Event()
        await feed._ws_venue("gemini", [{"type": "subscribe"}])

    asyncio.run(_run())

    assert observed, "the backoff path never ran"
    assert observed["n"] == 1, (
        "only coinbase may contribute during a gemini reconnect — the stale "
        "gemini book must have been cleared when the session ended")
    assert observed["reasons"].get(("gemini", "BTC", "awaiting_rebuild")) == 1


def test_slow_connect_is_not_counted_as_session_uptime(monkeypatch):
    """`session_start` must be taken once the socket is OPEN. If it were
    captured before `websockets.connect`, a slow connect would be scored as
    uptime and would wrongly reset the backoff ladder on a connect-and-die
    flap."""
    feed = SyntheticRTIFeed(enabled=True)
    session = [_gemini_l2_updates("BTCUSD", [(100.0, 5.0)], [(100.1, 5.0)])]
    # Patch the module's `time` BINDING, not the shared stdlib module object
    # (`feed_mod.time is time`), so a frozen clock cannot leak into asyncio's
    # own `loop.time()` or any other importer. `ingest_frame` calls
    # `time.time()` on the same path, so the stand-in must carry it too.
    clock = {"t": 0.0}
    monkeypatch.setattr(
        feed_mod, "time",
        types.SimpleNamespace(monotonic=lambda: clock["t"], time=time.time))

    waits = []

    async def _record_sleep(wait):
        waits.append(wait)
        return feed._ws_stop_event.is_set()

    calls = {"n": 0}
    sessions = [session] * 4

    def fake_connect(url, **kwargs):
        # Each connect ATTEMPT burns far more than _HEALTHY_SESSION_SECONDS;
        # the session itself is instantaneous.
        clock["t"] += 100.0
        i = calls["n"]
        calls["n"] += 1
        if i >= len(sessions):
            feed._ws_stop_event.set()
            raise RuntimeError("no more scripted sessions")
        last = i == len(sessions) - 1
        return _FakeWSSession(
            [json.dumps(f) for f in sessions[i]],
            on_exhausted=feed._ws_stop_event.set if last else None)

    monkeypatch.setattr(feed_mod, "websockets",
                        types.SimpleNamespace(connect=fake_connect))
    monkeypatch.setattr(feed, "_sleep_or_stop", _record_sleep)

    async def _run():
        feed._ws_stop_event = asyncio.Event()
        await feed._ws_venue("gemini", [{"type": "subscribe"}])

    asyncio.run(_run())

    assert len(waits) == 3, waits
    assert waits[0] < waits[1] < waits[2], (
        "connect-attempt time was counted as session uptime, so the backoff "
        "ladder reset instead of escalating")


def test_cancellation_exits_the_venue_loop_without_reconnecting(monkeypatch):
    """A stopping feed can reach a venue task as a `CancelledError` from
    `_run`'s `finally` (see `SyntheticRTIFeed.stop` — whether that or the
    stop-check break wins is a race, and nothing here asserts which). That
    exit must terminate the loop: it must NOT be swallowed into the
    reconnect handling, since a swallowed cancel would reconnect a venue the
    operator just stopped, and it must leave `_connected` false."""
    feed = SyntheticRTIFeed(enabled=True)
    calls = {"n": 0}

    def fake_connect(url, **kwargs):
        calls["n"] += 1
        raise asyncio.CancelledError()

    monkeypatch.setattr(feed_mod, "websockets",
                        types.SimpleNamespace(connect=fake_connect))

    async def _boom(_wait):  # must never be reached
        raise AssertionError("cancellation must not enter the backoff path")

    monkeypatch.setattr(feed, "_sleep_or_stop", _boom)

    async def _run():
        feed._ws_stop_event = asyncio.Event()
        await feed._ws_venue("gemini", [{"type": "subscribe"}])

    asyncio.run(_run())
    assert calls["n"] == 1, "must not retry after cancellation"
    assert feed._connected["gemini"] is False


def test_locked_book_still_contributes():
    """The crossed check is `>` not `>=` on purpose: a momentarily LOCKED
    book (best bid == best ask) is not proof of corruption, and dropping it
    would shrink rti_confidence on healthy data. Pins the carve-out so a
    future tightening to `>=` is a deliberate, RED-test decision."""
    feed = SyntheticRTIFeed(enabled=True)
    feed.ingest_frame(
        "coinbase",
        _coinbase_l2_snapshot("BTC-USD", [(100.0, 5.0)], [(100.1, 5.0)]),
        now=0.0)
    feed.ingest_frame(
        "gemini", _gemini_l2_updates("BTCUSD", [(100.0, 5.0)], [(100.0, 5.0)]),
        now=0.0)
    book = feed._books[("gemini", "BTC")]
    bids, asks = book.levels()
    assert bids[0][0] == asks[0][0], "test fixture must be a LOCKED book"
    assert feed.synthetic("BTC", now=0.0)[1] == 2, (
        "a locked book must still contribute — only a STRICTLY crossed book "
        "is corrupt")
    assert not book.crossed


def test_delta_only_venues_membership_is_pinned():
    """Only a venue with NO snapshot message type belongs in
    `_DELTA_ONLY_VENUES`. Adding one that does have a snapshot would promote
    its first post-reset PARTIAL diff to a full-book replace — the exact
    partial-book class this Bit closes. Widening this set must be a
    deliberate, RED-test decision."""
    assert feed_mod._DELTA_ONLY_VENUES == frozenset({"gemini"}), (
        "gemini is the only venue whose wire protocol has no snapshot frame "
        "type (Coinbase/Kraken send type=snapshot; every Bitstamp data frame "
        "is an independent full top-100 book)")


def test_delta_only_venue_promotion_would_corrupt_a_snapshot_venue():
    """The behavioural half of the pin: show WHY coinbase must not be in
    `_DELTA_ONLY_VENUES` — under the promotion rule its first post-reset
    l2update would replace the book with a two-level fragment."""
    feed = SyntheticRTIFeed(enabled=True)
    feed.ingest_frame(
        "coinbase",
        _coinbase_l2_snapshot(
            "BTC-USD", [(100.0, 5.0), (99.0, 5.0)], [(100.1, 5.0)]),
        now=0.0)
    feed.reset_venue("coinbase")
    # A partial diff arrives before the resubscribe snapshot.
    feed.ingest_frame(
        "coinbase",
        _coinbase_l2update("BTC-USD", [("buy", 98.0, 1.0), ("sell", 98.5, 1.0)]),
        now=0.0)
    book = feed._books[("coinbase", "BTC")]
    assert book.awaiting_snapshot, (
        "coinbase is NOT delta-only: its partial diff must not be promoted "
        "to a full-book replace, and must not clear the rebuild guard")
    assert feed.synthetic("BTC", now=0.0) == (None, 0)


def test_one_sided_book_is_excluded_and_tallied():
    """A book with bids but no asks (or vice versa) has no mid, so it cannot
    contribute. Pins both the exclusion and its `one_sided` tally."""
    feed = SyntheticRTIFeed(enabled=True)
    feed.ingest_frame(
        "coinbase",
        _coinbase_l2_snapshot("BTC-USD", [(100.0, 5.0)], [(100.1, 5.0)]),
        now=0.0)
    # Bids only — Gemini's first frame is promoted to a snapshot, so this
    # lands as a genuinely one-sided book rather than a partial merge.
    feed.ingest_frame(
        "gemini", _gemini_l2_updates("BTCUSD", [(100.0, 5.0)], []), now=0.0)
    book = feed._books[("gemini", "BTC")]
    assert book.bids and not book.asks, "fixture must be one-sided"
    assert not book.awaiting_snapshot, "the frame did rebuild the book"

    assert feed.synthetic("BTC", now=0.0)[1] == 1
    assert feed.exclusion_counts().get(("gemini", "BTC", "one_sided")) == 1
