"""B2a-2 — offline RMSE harness contract.

Ticket `86ba1zf5j` (B2a, plan kb/decisions/b2-synthetic-rti-feed-plan.md).

``scripts/research/synthetic_rti_rmse.py`` reconstructs per-second
consolidated order books from the 4 venues' bronze L2 across the last 60s
before each settled 15M market close, runs the shipped pure aggregator
``bot.feeds.synthetic_rti.compute_synthetic_rti`` per second, averages the
60 values (mirrors CFB settlement = 60s average of the RTI), and compares
to the market's ``expiration_value`` → per-asset RMSE in bps.

These tests pin the BOOK-RECONSTRUCTION core against deterministic
fixtures (no real bronze needed — that comes after the ~14d soak):
  - per-venue frame parsers (Bitstamp full-snapshot; Kraken/Gemini/Coinbase
    snapshot + apply-diffs);
  - symbol→asset reverse maps (incl. Kraken XDG/USD → DOGE);
  - the per-second replay + staleness-drop + 60s averaging;
  - the RMSE math + per-asset gate;
  - the thin bronze-reader + settled-market enumerator I/O layers
    (fixture dir + mock client).
"""
from __future__ import annotations

import datetime as _dt
import math

import pytest

from scripts.research import synthetic_rti_rmse as h


# ── OrderBook ─────────────────────────────────────────────────────────────


def test_orderbook_snapshot_then_delta_set_and_remove():
    ob = h.OrderBook()
    ob.apply_snapshot(
        bids=[(100.0, 5.0), (99.0, 3.0)],
        asks=[(101.0, 4.0), (102.0, 2.0)],
    )
    # Set a new bid level + remove a top ask (size 0).
    ob.apply_delta("bid", 100.5, 1.0)
    ob.apply_delta("ask", 101.0, 0.0)
    bids, asks = ob.snapshot()
    # bids descending by price.
    assert bids == [(100.5, 1.0), (100.0, 5.0), (99.0, 3.0)]
    # asks ascending; the 101.0 level was removed.
    assert asks == [(102.0, 2.0)]


def test_orderbook_delta_updates_existing_level_size():
    ob = h.OrderBook()
    ob.apply_snapshot([(100.0, 5.0)], [(101.0, 5.0)])
    ob.apply_delta("bid", 100.0, 8.0)  # resize, not add
    bids, _ = ob.snapshot()
    assert bids == [(100.0, 8.0)]


# ── Per-venue parsers ─────────────────────────────────────────────────────


def test_parse_kraken_snapshot_and_update_xdg_maps_to_doge():
    snap = {
        "channel": "book",
        "type": "snapshot",
        "data": [{
            "symbol": "XDG/USD",
            "bids": [{"price": 0.1, "qty": 100.0}],
            "asks": [{"price": 0.11, "qty": 100.0}],
        }],
    }
    u = h.parse_frame("kraken", snap)
    assert u is not None and u.asset == "DOGE" and u.kind == "snapshot"
    assert u.bids == [(0.1, 100.0)] and u.asks == [(0.11, 100.0)]

    upd = {
        "channel": "book",
        "type": "update",
        "data": [{
            "symbol": "XDG/USD",
            "bids": [{"price": 0.1, "qty": 0.0}],  # remove
            "asks": [{"price": 0.115, "qty": 50.0}],  # add
        }],
    }
    u2 = h.parse_frame("kraken", upd)
    assert u2 is not None and u2.kind == "delta"
    assert ("bid", 0.1, 0.0) in u2.deltas
    assert ("ask", 0.115, 50.0) in u2.deltas


def test_parse_kraken_non_book_frame_returns_none():
    assert h.parse_frame("kraken", {"method": "subscribe", "success": True}) is None
    assert h.parse_frame("kraken", {"channel": "heartbeat"}) is None


def test_parse_bitstamp_full_snapshot_btcusd():
    frame = {
        "event": "data",
        "channel": "order_book_btcusd",
        "data": {
            "microtimestamp": "1",
            "bids": [["100.0", "5.0"]],
            "asks": [["100.1", "5.0"]],
        },
    }
    u = h.parse_frame("bitstamp", frame)
    assert u is not None and u.asset == "BTC" and u.kind == "snapshot"
    assert u.bids == [(100.0, 5.0)] and u.asks == [(100.1, 5.0)]


def test_parse_bitstamp_subscription_ack_returns_none():
    assert h.parse_frame("bitstamp", {
        "event": "bts:subscription_succeeded", "channel": "order_book_btcusd",
    }) is None


def test_parse_gemini_l2_updates_buy_sell_to_bid_ask():
    frame = {
        "type": "l2_updates",
        "symbol": "ETHUSD",
        "changes": [["buy", "100.0", "5.0"], ["sell", "100.1", "0.0"]],
    }
    u = h.parse_frame("gemini", frame)
    assert u is not None and u.asset == "ETH" and u.kind == "delta"
    assert ("bid", 100.0, 5.0) in u.deltas
    assert ("ask", 100.1, 0.0) in u.deltas


def test_parse_gemini_heartbeat_returns_none():
    assert h.parse_frame("gemini", {"type": "heartbeat"}) is None


def test_parse_coinbase_snapshot_and_l2update():
    snap = {
        "type": "snapshot",
        "product_id": "BTC-USD",
        "bids": [["100.0", "5.0"]],
        "asks": [["100.1", "5.0"]],
    }
    u = h.parse_frame("coinbase", snap)
    assert u is not None and u.asset == "BTC" and u.kind == "snapshot"

    upd = {
        "type": "l2update",
        "product_id": "BTC-USD",
        "changes": [["buy", "100.0", "0.0"], ["sell", "100.2", "3.0"]],
    }
    u2 = h.parse_frame("coinbase", upd)
    assert u2 is not None and u2.kind == "delta"
    assert ("bid", 100.0, 0.0) in u2.deltas
    assert ("ask", 100.2, 3.0) in u2.deltas


# ── CFB params table (matches the plan's locked table) ────────────────────


def test_cfb_params_match_plan_table():
    p = h.CFB_PARAMS
    assert set(p) == {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"}
    assert p["BTC"]["venues"] == ("coinbase", "kraken", "bitstamp", "gemini")
    assert p["XRP"]["venues"] == ("coinbase", "kraken", "bitstamp")
    assert p["DOGE"]["venues"] == ("coinbase", "kraken", "gemini")
    assert p["BNB"]["venues"] == ("coinbase", "kraken")
    assert p["HYPE"]["venues"] == ("coinbase", "kraken", "bitstamp")
    assert p["BTC"]["spacing"] == 1.0
    assert p["ETH"]["spacing"] == 25.0
    assert p["SOL"]["spacing"] == 100.0
    assert p["BNB"]["deviation_from_mid_pct"] == 10.0
    assert p["BTC"]["potentially_erroneous_pct"] == 5.0


# ── 60s averaging (the heart) ──────────────────────────────────────────────


def _tight_book_frame(venue: str, asset: str, kind: str = "snapshot"):
    """A 1-level book (bid 100.0 / ask 100.1) in the given venue's frame
    shape — used so depth<spacing collapses RTI to the consolidated mid
    100.05 deterministically (mirrors the aggregator's degenerate test)."""
    if venue == "kraken":
        return {
            "channel": "book", "type": kind,
            "data": [{"symbol": "BTC/USD",
                      "bids": [{"price": 100.0, "qty": 5.0}],
                      "asks": [{"price": 100.1, "qty": 5.0}]}],
        }
    if venue == "coinbase":
        return {
            "type": "snapshot", "product_id": "BTC-USD",
            "bids": [["100.0", "5.0"]], "asks": [["100.1", "5.0"]],
        }
    raise ValueError(venue)


def test_60s_average_degenerate_depth_collapses_to_consolidated_mid():
    """Two venues, tight 1-level books, spacing huge → each second's RTI
    is the consolidated mid 100.05; averaging 60 identical seconds = 100.05."""
    close_ts = 1_000_000.0
    params = {
        "venues": ("coinbase", "kraken"),
        "spacing": 1000.0,
        "deviation_from_mid_pct": 1.0,
        "potentially_erroneous_pct": 10.0,
        "retrieval_lag_threshold_seconds": 9999.0,
    }
    # One snapshot per venue well before the window; book persists across
    # all 60 samples.
    frames_by_venue = {
        "coinbase": [(close_ts - 100, _tight_book_frame("coinbase", "BTC"))],
        "kraken": [(close_ts - 100, _tight_book_frame("kraken", "BTC"))],
    }
    avg = h.synthetic_rti_60s_average(frames_by_venue, "BTC", close_ts, params)
    assert avg == pytest.approx(100.05, abs=1e-6)


def test_60s_average_drops_stale_venue():
    """A venue whose last frame is older than retrieval_lag is dropped; the
    fresh venue still carries the RTI (no None, no crash)."""
    close_ts = 1_000_000.0
    params = {
        "venues": ("coinbase", "kraken"),
        "spacing": 1000.0,
        "deviation_from_mid_pct": 1.0,
        "potentially_erroneous_pct": 10.0,
        "retrieval_lag_threshold_seconds": 10.0,
    }
    frames_by_venue = {
        # Coinbase fresh inside the window.
        "coinbase": [(close_ts - 5, _tight_book_frame("coinbase", "BTC"))],
        # Kraken's last frame is 500s stale → dropped at every sample.
        "kraken": [(close_ts - 500, _tight_book_frame("kraken", "BTC"))],
    }
    avg = h.synthetic_rti_60s_average(frames_by_venue, "BTC", close_ts, params)
    # Only coinbase contributes (after close_ts-5); its tight book → 100.05.
    assert avg == pytest.approx(100.05, abs=1e-6)


def test_60s_average_no_frames_returns_none():
    params = {
        "venues": ("coinbase", "kraken"),
        "spacing": 1000.0, "deviation_from_mid_pct": 1.0,
        "potentially_erroneous_pct": 10.0,
        "retrieval_lag_threshold_seconds": 10.0,
    }
    assert h.synthetic_rti_60s_average({}, "BTC", 1_000_000.0, params) is None


# ── RMSE math + gate ──────────────────────────────────────────────────────


def test_bps_error():
    assert h.bps_error(100.05, 100.0) == pytest.approx(5.0, abs=1e-9)
    assert h.bps_error(99.95, 100.0) == pytest.approx(-5.0, abs=1e-9)


def test_rmse_bps():
    assert h.rmse_bps([30.0, 40.0]) == pytest.approx(math.sqrt(1250.0), abs=1e-9)
    assert h.rmse_bps([10.0]) == pytest.approx(10.0, abs=1e-9)
    assert math.isnan(h.rmse_bps([]))


def test_asset_gate_bps():
    assert h.asset_gate_bps("HYPE") == 25.0
    for a in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"):
        assert h.asset_gate_bps(a) == 15.0


# ── Settled-market enumerator (mock Kalshi client) ────────────────────────


class _FakeClient:
    """Returns one page of settled markets then no cursor."""

    def __init__(self, markets):
        self._markets = markets
        self.calls = []

    def get_markets(self, **kwargs):
        self.calls.append(kwargs)
        return {"markets": self._markets, "cursor": ""}


def test_load_settled_markets_parses_and_skips_empty_expiration():
    markets = [
        {"ticker": "KXBTC15M-A", "close_time": "2026-05-28T12:00:00Z",
         "expiration_value": "100000.5", "result": "yes"},
        # Empty expiration_value (not yet populated) → skipped.
        {"ticker": "KXBTC15M-B", "close_time": "2026-05-28T12:15:00Z",
         "expiration_value": "", "result": ""},
    ]
    client = _FakeClient(markets)
    out = h.load_settled_markets(
        client, {"BTC": "KXBTC15M"},
        start_ts=0, end_ts=2_000_000_000,
    )
    assert len(out) == 1
    m = out[0]
    assert m.ticker == "KXBTC15M-A" and m.asset == "BTC"
    assert m.expiration_value == pytest.approx(100000.5)
    # close_time ISO → epoch.
    expected = _dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=_dt.timezone.utc).timestamp()
    assert m.close_ts == pytest.approx(expected, abs=1.0)
    # status=settled + window bounds forwarded to the API.
    assert client.calls[0]["status"] == "settled"


# ── Bronze reader (fixture chunk round-trip) ──────────────────────────────


def test_iter_bronze_frames_reads_and_time_filters(tmp_path):
    """Write a real bronze chunk via BronzeWriter, then read it back through
    the harness's bronze reader, asserting envelopes are unwrapped to raw
    dicts and time-filtered to the window."""
    from collector.writer import BronzeWriter

    bronze_root = tmp_path / "bronze"
    w = BronzeWriter(
        root_dir=bronze_root, source="kraken_ws", channel="book", conn="A",
    )
    base = _dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=_dt.timezone.utc)
    import json as _json
    for i in range(3):
        ts = base + _dt.timedelta(seconds=i)
        raw = _json.dumps({"channel": "book", "type": "update", "seq": i})
        w.write_frame(ts, raw)
    w.close()  # force rotation so the chunk lands in outbox/

    start = base.timestamp() - 1
    end = (base + _dt.timedelta(seconds=1)).timestamp()  # only i=0,1 in window
    frames = list(h.iter_bronze_frames(
        bronze_root, "kraken_ws", "book", start, end,
    ))
    seqs = sorted(raw["seq"] for _ts, raw in frames)
    assert seqs == [0, 1], f"expected frames 0,1 in window; got {seqs}"
