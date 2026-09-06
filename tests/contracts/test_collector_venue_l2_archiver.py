"""B2a-1 — lean multi-venue L2 bronze recorder archiver contract.

Ticket `86ba1zf5j` (B2a, plan kb/decisions/b2-synthetic-rti-feed-plan.md).

``collector/venue_l2_archiver.py`` is a LEAN multi-venue L2 WS bronze
recorder. It opens Kraken + Bitstamp + Gemini public L2 WebSockets in a
single asyncio thread (mirroring ``bot/feeds/cross_exchange.py``'s
reconnect/backoff shape — NOT the heavy ``coinbase_wire`` library) and
archives the RAW frames verbatim via ``collector/writer.py`` +
``kalshi_wire.build_envelope``. Coinbase L2 bronze is already live via the
``kalshi-coinbase-collector`` (``level2_batch`` channel); this recorder
covers the three remaining CFB-constituent venues so the B2a-2 offline
RMSE harness can reconstruct a 4-venue consolidated book.

Decided 2026-05-28 (plan-doc "B2a-1 architecture"):
  - LEAN recorder — NO per-venue wire libraries, NO book maintenance in
    the recorder (raw frames only; book reconstruction is the harness's
    job).
  - Per-venue bronze SOURCE (``kraken_ws`` / ``bitstamp_ws`` /
    ``gemini_ws``) + per-venue native L2 CHANNEL (``book`` / ``order_book``
    / ``l2``). All of a venue's subscribed assets share one channel; the
    asset is disambiguated by the symbol inside the raw frame — exactly
    mirrors ``coinbase_ws/level2_batch`` (all collector products in one channel).
  - Subscription-ack / heartbeat / control frames are NOT archived
    (bronze noise; mirrors the CoinbaseArchiver D1.3-fu5 skip-ack rule).

These contracts pin the recorder's structural shape so a future edit that
drifts the venue map, the subscribe payloads, or the routing fires here.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

import collector.venue_l2_archiver as _vmod
from collector.venue_l2_archiver import (
    BITSTAMP_WS_URL,
    DISCONNECT_LOG_MARKER,
    GEMINI_L2_WS_URL,
    KRAKEN_BOOK_DEPTH,
    KRAKEN_L2_WS_URL,
    VENUE_CHANNELS,
    VENUE_SOURCES,
    VENUE_SYMBOLS,
    VENUES,
    VenueL2Archiver,
    build_bitstamp_subscribe,
    build_gemini_subscribe,
    build_kraken_subscribe,
)


# ── Capture writer (stand-in for BronzeWriter; just records envelopes) ──


class _CaptureWriter:
    def __init__(self, source: str, channel: str, conn):
        self.source = source
        self.channel = channel
        self.conn = conn
        self.written: list = []

    def write(self, envelope, *, wire_recv_ts=None):
        self.written.append(envelope)


def _make_archiver():
    writers = {
        v: _CaptureWriter(VENUE_SOURCES[v], VENUE_CHANNELS[v], "A")
        for v in VENUES
    }
    # Deterministic ts so envelope round-trips are checkable.
    import datetime as _dt

    arch = VenueL2Archiver(
        writers_by_venue=writers,
        now_fn=lambda: _dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=_dt.timezone.utc),
    )
    return arch, writers


# ── 1. Venue map ────────────────────────────────────────────────────────


def test_venues_are_the_three_new_venues():
    """Coinbase L2 is already live; this recorder adds the OTHER three."""
    assert set(VENUES) == {"kraken", "bitstamp", "gemini"}
    assert "coinbase" not in VENUES


def test_sources_and_channels_cover_every_venue():
    for v in VENUES:
        assert v in VENUE_SOURCES, f"{v} missing a bronze source slug"
        assert v in VENUE_CHANNELS, f"{v} missing a bronze channel name"
    # Per-venue source slug (D0.3 §1 — one source per provider).
    assert VENUE_SOURCES == {
        "kraken": "kraken_ws",
        "bitstamp": "bitstamp_ws",
        "gemini": "gemini_ws",
    }
    # Native L2 channel name per venue (mirror coinbase_ws/level2_batch).
    assert VENUE_CHANNELS == {
        "kraken": "book",
        "bitstamp": "order_book",
        "gemini": "l2",
    }


def test_venue_symbols_match_b2_coverage():
    """Per-venue asset-coverage table. TWO distinct inclusion policies:

    RTI-ALIGNED (the 7 trading assets): a venue is EXCLUDED even when it
    lists the pair, if CFB does NOT use that venue for the asset's settling
    index — over-inclusion would bias the synthetic AWAY from settlement
    (e.g. Gemini excluded for XRP, Bitstamp excluded for DOGE).

    CORPUS-COLLECT (ADA, BCH — added 2026-05-30; NEAR, ZEC — added 2026-09-05,
    ticket 86bbvdc8y): Kalshi 15M assets the bot does NOT trade. Their CFB-RTI constituents are NOT yet resolved,
    so they are collected on EVERY venue that LISTS the pair (corpus-max).
    This is safe for the synthetic because both reconstruction gates skip
    any asset absent from their per-asset params map and neither has an
    ADA/BCH/NEAR/ZEC entry — the offline RMSE harness's local ``CFB_PARAMS``
    (``scripts/research/synthetic_rti_rmse.py``) and the B2b live feed's
    ``_CFB_PARAMS`` (``bot.feeds.synthetic_rti_feed``). Raw bronze
    accumulates but is not reconstructed until their RTI constituents are
    resolved (follow-up). Live-probed listings 2026-05-30:
    ADA on kraken+bitstamp (NOT Gemini); BCH on all four. 2026-09-05: NEAR on
    kraken+bitstamp (NOT Gemini); ZEC on kraken+bitstamp+gemini.

      - Kraken:   7 RTI-aligned + ADA + BCH + NEAR + ZEC = 11 (DOGE = XDG/USD).
      - Bitstamp: 5 RTI-aligned + ADA + BCH + NEAR + ZEC = 9.
      - Gemini:   4 RTI-aligned + BCH + ZEC             = 6 (ADA/NEAR not listed).
    """
    assert set(VENUE_SYMBOLS["kraken"]) == {
        "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "ADA", "BCH",
        "NEAR", "ZEC",
    }
    assert VENUE_SYMBOLS["kraken"]["DOGE"] == "XDG/USD", (
        "Kraken DOGE symbol must be XDG/USD (mirror CROSS_EXCHANGE_SYMBOLS), "
        "NOT DOGE/USD."
    )
    assert VENUE_SYMBOLS["kraken"]["ADA"] == "ADA/USD"
    assert VENUE_SYMBOLS["kraken"]["BCH"] == "BCH/USD"
    assert VENUE_SYMBOLS["kraken"]["NEAR"] == "NEAR/USD"
    assert VENUE_SYMBOLS["kraken"]["ZEC"] == "ZEC/USD"
    assert set(VENUE_SYMBOLS["bitstamp"]) == {
        "BTC", "ETH", "SOL", "XRP", "HYPE", "ADA", "BCH", "NEAR", "ZEC",
    }
    assert VENUE_SYMBOLS["bitstamp"]["ADA"] == "adausd"
    assert VENUE_SYMBOLS["bitstamp"]["BCH"] == "bchusd"
    assert VENUE_SYMBOLS["bitstamp"]["NEAR"] == "nearusd"
    assert VENUE_SYMBOLS["bitstamp"]["ZEC"] == "zecusd"
    assert set(VENUE_SYMBOLS["gemini"]) == {
        "BTC", "ETH", "SOL", "DOGE", "BCH", "ZEC",
    }
    assert VENUE_SYMBOLS["gemini"]["BCH"] == "BCHUSD"
    assert VENUE_SYMBOLS["gemini"]["ZEC"] == "ZECUSD"


def test_ws_urls_are_the_verified_endpoints():
    assert KRAKEN_L2_WS_URL == "wss://ws.kraken.com/v2"
    assert BITSTAMP_WS_URL == "wss://ws.bitstamp.net"
    assert GEMINI_L2_WS_URL == "wss://api.gemini.com/v2/marketdata"


# ── 2. Subscribe payload builders (verified protocols 2026-05-28) ─────────


def test_kraken_subscribe_payload_shape():
    msg = build_kraken_subscribe(["BTC/USD", "ETH/USD"], depth=KRAKEN_BOOK_DEPTH)
    assert msg == {
        "method": "subscribe",
        "params": {
            "channel": "book",
            "symbol": ["BTC/USD", "ETH/USD"],
            "depth": KRAKEN_BOOK_DEPTH,
        },
    }


def test_bitstamp_subscribe_is_per_pair_order_book():
    msg = build_bitstamp_subscribe("btcusd")
    assert msg == {
        "event": "bts:subscribe",
        "data": {"channel": "order_book_btcusd"},
    }


def test_gemini_subscribe_l2_payload_shape():
    msg = build_gemini_subscribe(["BTCUSD", "ETHUSD"])
    assert msg == {
        "type": "subscribe",
        "subscriptions": [{"name": "l2", "symbols": ["BTCUSD", "ETHUSD"]}],
    }


# ── 3. Frame routing → raw bronze envelope ────────────────────────────────


def test_kraken_book_frame_archived_raw():
    arch, writers = _make_archiver()
    raw = json.dumps({
        "channel": "book",
        "type": "snapshot",
        "data": [{
            "symbol": "BTC/USD",
            "bids": [{"price": 100.0, "qty": 5.0}],
            "asks": [{"price": 100.1, "qty": 5.0}],
        }],
    })
    arch._handle_kraken(raw)
    assert len(writers["kraken"].written) == 1
    env = writers["kraken"].written[0]
    assert env["_source"] == "kraken_ws"
    assert env["_channel"] == "book"
    assert env["_conn"] == "A"
    assert env["_raw"] == raw, "bronze must capture the RAW frame verbatim"
    # No cross-venue leakage.
    assert writers["bitstamp"].written == []
    assert writers["gemini"].written == []


def test_kraken_subscribe_ack_not_archived():
    arch, writers = _make_archiver()
    arch._handle_kraken(json.dumps({"method": "subscribe", "success": True}))
    arch._handle_kraken(json.dumps({"channel": "heartbeat"}))
    arch._handle_kraken(json.dumps({"channel": "status", "data": [{}]}))
    assert writers["kraken"].written == [], (
        "control / ack / heartbeat / status frames must NOT be archived "
        "(only channel==book data frames)."
    )


def test_bitstamp_data_frame_archived_ack_skipped():
    arch, writers = _make_archiver()
    data_raw = json.dumps({
        "event": "data",
        "channel": "order_book_btcusd",
        "data": {"microtimestamp": "1", "bids": [["100", "5"]], "asks": [["100.1", "5"]]},
    })
    arch._handle_bitstamp(data_raw)
    arch._handle_bitstamp(json.dumps({
        "event": "bts:subscription_succeeded", "channel": "order_book_btcusd",
    }))
    assert len(writers["bitstamp"].written) == 1
    assert writers["bitstamp"].written[0]["_raw"] == data_raw
    assert writers["bitstamp"].written[0]["_source"] == "bitstamp_ws"
    assert writers["bitstamp"].written[0]["_channel"] == "order_book"


def test_gemini_l2_updates_archived_heartbeat_skipped():
    arch, writers = _make_archiver()
    l2_raw = json.dumps({
        "type": "l2_updates",
        "symbol": "BTCUSD",
        "changes": [["buy", "100", "5"], ["sell", "100.1", "5"]],
    })
    arch._handle_gemini(l2_raw)
    arch._handle_gemini(json.dumps({"type": "heartbeat"}))
    arch._handle_gemini(json.dumps({"type": "subscription_ack", "subscriptionId": "x"}))
    assert len(writers["gemini"].written) == 1
    assert writers["gemini"].written[0]["_raw"] == l2_raw
    assert writers["gemini"].written[0]["_source"] == "gemini_ws"
    assert writers["gemini"].written[0]["_channel"] == "l2"


def test_collector_seq_is_monotone_across_venues():
    arch, writers = _make_archiver()
    arch._handle_kraken(json.dumps({"channel": "book", "type": "update", "data": [{"symbol": "BTC/USD"}]}))
    arch._handle_bitstamp(json.dumps({"event": "data", "channel": "order_book_ethusd", "data": {}}))
    arch._handle_gemini(json.dumps({"type": "l2_updates", "symbol": "SOLUSD", "changes": []}))
    seqs = [
        writers["kraken"].written[0]["_collector_seq"],
        writers["bitstamp"].written[0]["_collector_seq"],
        writers["gemini"].written[0]["_collector_seq"],
    ]
    assert seqs == sorted(seqs), "seq must be monotone increasing"
    assert len(set(seqs)) == 3, "each frame gets a UNIQUE seq"


def test_malformed_frame_is_swallowed_not_raised():
    arch, writers = _make_archiver()
    # A non-JSON frame must not crash the read loop (best-effort posture).
    arch._handle_kraken("not json{{")
    assert writers["kraken"].written == []


# ── 4. Health snapshot schema parity (cross-collector monitor) ────────────


def test_health_snapshot_schema_parity():
    arch, _ = _make_archiver()
    snap = arch.get_health_snapshot()
    for key in (
        "conn_id",
        "dropped_frames",
        "write_queue_size",
        "write_queue_maxsize",
        "write_worker_alive",
        "collector_seq",
        "ack_frames_processed",
    ):
        assert key in snap, f"health snapshot missing {key!r} (schema parity)"
    # Lean recorder has no bounded worker queue → no drops by design.
    assert snap["dropped_frames"] == 0
    assert snap["write_queue_size"] == 0
    assert snap["write_queue_maxsize"] == 0


# ── 5. Shutdown: stop() must quiesce the reader thread before returning ───
#
# The owning main loop calls archiver.stop() then writer.close() on the main
# thread; the reader thread calls writer.write() synchronously. If stop() were
# fire-and-forget, the two threads would race on the same BronzeWriter file
# handle at close (R1 MAJOR M1). stop() must JOIN the reader thread, and _run
# must cancel the venue coroutines on stop so the thread exits promptly even
# when a venue WS is silent mid-recv.
#
# HERMETIC: these tests do NOT touch the network. They monkeypatch
# websockets.connect with a fake that "connects" then blocks forever in a
# pure-Python asyncio.sleep inside the read loop (simulating a silent WS that
# never sends a frame). Cancelling the task on stop unwinds that sleep
# instantly. (An earlier version pointed a venue at ws://127.0.0.1:9 and
# relied on the OS refusing the connection fast — true on a dev Mac but NOT on
# a CI runner, where the connect hung + the leaked reader thread wedged the
# whole contract tier under xdist; see feedback_xdist_flake_cascade.)


class _HangingWS:
    """Fake websockets connection: connects, accepts a subscribe send, then
    blocks in the async-for read loop forever — a silent WS that never emits a
    frame. Cancellable instantly (pure-Python asyncio.sleep)."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, *_a):
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(3600)  # block; CancelledError unwinds it instantly
        raise StopAsyncIteration  # unreachable — present for protocol clarity


def _fake_connect(*_a, **_k):
    return _HangingWS()


def _hermetic_archiver():
    """One-venue archiver; the venue's WS is the fake hanging connection (the
    caller must monkeypatch websockets.connect first)."""
    writers = {"kraken": _CaptureWriter("kraken_ws", "book", "A")}
    return VenueL2Archiver(writers_by_venue=writers, venues=("kraken",))


def test_stop_joins_reader_thread(monkeypatch):
    monkeypatch.setattr(_vmod.websockets, "connect", _fake_connect)
    arch = _hermetic_archiver()
    arch.start()
    time.sleep(0.2)  # let the loop spin up, "connect", + enter the read loop
    arch.stop()
    assert arch._thread is not None
    assert not arch._thread.is_alive(), (
        "stop() must JOIN the reader thread so the producer is quiesced "
        "before the owning main loop calls writer.close() (R1 M1). _run must "
        "cancel the venue coroutine on stop so a silent-WS read unwinds."
    )


def test_stop_before_loop_ready_still_terminates(monkeypatch):
    """stop() racing the reader thread's event-loop construction must still
    take effect (the _stop_requested plain flag closes that race)."""
    monkeypatch.setattr(_vmod.websockets, "connect", _fake_connect)
    arch = _hermetic_archiver()
    arch.start()
    arch.stop()  # may fire before _run_thread created the asyncio loop/event
    assert not arch._thread.is_alive(), (
        "stop() called immediately after start() must still terminate the "
        "reader thread (the _stop_requested flag is honored on loop creation)."
    )


def test_disconnect_log_marker_is_venue_scoped():
    """The health monitor's check_ws_reconnects filters journal lines by
    this marker; it MUST differ from the Kalshi/Coinbase markers so the
    venue-l2 reconnect-storm alert doesn't cross-match other tiers."""
    assert DISCONNECT_LOG_MARKER == "venue_l2_ws_disconnected"
    assert "kalshi" not in DISCONNECT_LOG_MARKER
    assert "coinbase" not in DISCONNECT_LOG_MARKER
