"""Phase 1b — Kalshi orderbook reconstruction: TDD-first test suite.

The methodology-critical core of Phase 1b. Replays `kalshi_ws/orderbook_delta`
bronze (snapshot + deltas) into a coherent book, then derives a NEVER-CROSSED
NBBO — the fix for R1-C1 (state.db scalar yes_ask/yes_bid are independently-
lagging and cross 22-38% of the time; a real resting book cannot cross).

Kalshi binary book convention (verified against real bronze frames 2026-05-30):
  - `yes_dollars_fp` = resting YES bids; best YES bid = max(yes prices).
  - `no_dollars_fp`  = resting NO bids;  best YES ASK = 100 - best NO bid
    (buying YES = lifting the NO bid). So yes_ask = 100 - max(no prices).
  - Prices are sub-cent ("0.0880" = 8.8c); keyed in ticks (1e-4 dollars).

Parent plan: kb/decisions/settlement-lag-convergence-edge-spike-plan.md (Phase 1b)
Worklist: kb/decisions/settlement-convergence-worklist.md
"""

from __future__ import annotations

import json

import pytest

from scripts.research import kalshi_book_reconstruct as kbr


# ----- Snapshot -> coherent NBBO ------------------------------------------


def test_snapshot_nbbo_basic():
    b = kbr.KalshiBook()
    b.apply_snapshot(
        yes_levels=[["0.4000", "100"], ["0.4500", "50"]],
        no_levels=[["0.5000", "100"], ["0.5200", "30"]],
    )
    assert b.best_yes_bid_cents() == pytest.approx(45.0)         # max yes price
    assert b.best_yes_ask_cents() == pytest.approx(48.0)         # 100 - max no price (52)
    assert b.best_yes_bid_cents() <= b.best_yes_ask_cents()       # NEVER crossed


def test_snapshot_subcent_prices():
    b = kbr.KalshiBook()
    b.apply_snapshot(yes_levels=[["0.0880", "100"]], no_levels=[["0.0910", "50"]])
    assert b.best_yes_bid_cents() == pytest.approx(8.8)
    assert b.best_yes_ask_cents() == pytest.approx(90.9)          # 100 - 9.1


# ----- Deltas -------------------------------------------------------------


def test_delta_adds_better_yes_bid():
    b = kbr.KalshiBook()
    b.apply_snapshot([["0.4000", "100"], ["0.4500", "50"]], [["0.5000", "100"]])
    b.apply_delta(side="yes", price_dollars="0.4600", delta_fp="10")
    assert b.best_yes_bid_cents() == pytest.approx(46.0)


def test_delta_to_zero_removes_level():
    b = kbr.KalshiBook()
    b.apply_snapshot([["0.4500", "50"]], [["0.5000", "100"], ["0.5200", "30"]])
    b.apply_delta(side="no", price_dollars="0.5200", delta_fp="-30")  # remove the 52 level
    assert b.best_yes_ask_cents() == pytest.approx(50.0)              # now 100 - 50
    b.apply_delta(side="no", price_dollars="0.5200", delta_fp="-5")   # already gone -> stays gone
    assert b.best_yes_ask_cents() == pytest.approx(50.0)


def test_delta_negative_size_clamped_removes():
    b = kbr.KalshiBook()
    b.apply_snapshot([["0.4500", "50"]], [["0.5000", "10"]])
    b.apply_delta(side="no", price_dollars="0.5000", delta_fp="-999")  # overshoots
    assert b.best_yes_ask_cents() is None  # no side empty -> ask undefined


# ----- Snapshot resets ----------------------------------------------------


def test_snapshot_replaces_prior_state():
    b = kbr.KalshiBook()
    b.apply_snapshot([["0.4500", "50"]], [["0.5000", "100"]])
    b.apply_delta(side="yes", price_dollars="0.4800", delta_fp="10")
    b.apply_snapshot([["0.3000", "5"]], [["0.6000", "5"]])  # fresh snapshot wipes the 0.48 delta
    assert b.best_yes_bid_cents() == pytest.approx(30.0)
    assert b.best_yes_ask_cents() == pytest.approx(40.0)


# ----- Empty sides --------------------------------------------------------


def test_empty_sides_return_none():
    b = kbr.KalshiBook()
    assert b.best_yes_bid_cents() is None
    assert b.best_yes_ask_cents() is None


# ----- Coherence invariant on a realistic sequence ------------------------


def test_nbbo_never_crosses_over_a_sequence():
    b = kbr.KalshiBook()
    b.apply_snapshot(
        [["0.9000", "100"], ["0.9100", "50"]],
        [["0.0500", "100"], ["0.0800", "30"]],   # yes_ask = 100 - 8 = 92
    )
    seq = [
        ("yes", "0.9200", "10"), ("no", "0.0700", "20"), ("yes", "0.9100", "-50"),
        ("no", "0.0800", "-30"), ("yes", "0.9300", "5"),
    ]
    for side, p, d in seq:
        b.apply_delta(side=side, price_dollars=p, delta_fp=d)
        bid, ask = b.best_yes_bid_cents(), b.best_yes_ask_cents()
        if bid is not None and ask is not None:
            assert bid <= ask, f"crossed book: bid={bid} ask={ask} after {side} {p} {d}"


# ----- Bronze envelope parsing --------------------------------------------


def test_parse_envelope_extracts_inner_frame():
    line = json.dumps({
        "_wire_recv_ts": "2026-05-30T00:03:48.593254Z",
        "_source": "kalshi_ws", "_conn": "A", "_channel": "orderbook_delta",
        "_collector_seq": 29231384,
        "_raw": json.dumps({
            "type": "orderbook_delta", "sid": 1, "seq": 503,
            "msg": {"market_ticker": "HOUSECA22-26-D", "price_dollars": "0.1500",
                    "delta_fp": "2.00", "side": "yes",
                    "ts_ms": 1780099428263},
        }),
    })
    recv_ts, inner = kbr.parse_envelope(line)
    assert recv_ts == "2026-05-30T00:03:48.593254Z"
    assert inner["type"] == "orderbook_delta"
    assert inner["msg"]["market_ticker"] == "HOUSECA22-26-D"
    assert inner["msg"]["side"] == "yes"


def test_is_reliable_accepts_clean_rejects_drift():
    """A reliable book is uncrossed (yes_bid+no_bid<=100) AND shallow (few levels).
    Drift from dropped removal-deltas violates both."""
    b = kbr.KalshiBook()
    b.apply_snapshot([["0.5500", "10"], ["0.5400", "5"]], [["0.4000", "10"]])  # 55+40=95, few levels
    assert b.is_reliable() is True
    # crossed (drift): yes_bid 60 + no_bid 50 = 110 > 100
    c = kbr.KalshiBook()
    c.apply_snapshot([["0.6000", "10"]], [["0.5000", "10"]])
    assert c.is_reliable() is False
    # too many phantom levels (drift): 30 yes levels
    d = kbr.KalshiBook()
    d.apply_snapshot([[f"0.{i:04d}".replace("0.00", "0.") + "0" if False else f"0.{i:02d}00", "5"]
                      for i in range(10, 40)], [["0.0500", "5"]])
    assert d.is_reliable(max_levels_per_side=25) is False


def test_reliable_nbbo_at_returns_clean_refuses_drifted():
    snap = {"type": "orderbook_snapshot",
            "msg": {"yes_dollars_fp": [["0.5500", "10"]], "no_dollars_fp": [["0.4000", "10"]]}}
    assert kbr.reliable_nbbo_at([(100.0, snap)], 102.0) == (pytest.approx(55.0), pytest.approx(60.0))
    # a delta that crosses the book (no_bid up to 50 -> 55+50=105) is refused
    bad = {"type": "orderbook_delta", "msg": {"side": "no", "price_dollars": "0.5000", "delta_fp": "5"}}
    assert kbr.reliable_nbbo_at([(100.0, snap), (101.0, bad)], 102.0) == (None, None)


def test_reliable_nbbo_at_resets_on_fresh_snapshot():
    """A fresh snapshot re-anchors truth — drift before it is discarded."""
    snap1 = {"type": "orderbook_snapshot",
             "msg": {"yes_dollars_fp": [["0.5500", "10"]], "no_dollars_fp": [["0.4000", "10"]]}}
    bad = {"type": "orderbook_delta", "msg": {"side": "no", "price_dollars": "0.5000", "delta_fp": "5"}}
    snap2 = {"type": "orderbook_snapshot",
             "msg": {"yes_dollars_fp": [["0.5600", "10"]], "no_dollars_fp": [["0.4100", "10"]]}}
    frames = [(100.0, snap1), (101.0, bad), (200.0, snap2)]
    bid, ask = kbr.reliable_nbbo_at(frames, 250.0)
    assert bid == pytest.approx(56.0)  # snap2 reset cleared the crossing delta


def test_reliable_nbbo_at_refuses_when_no_snapshot_anchor():
    # only deltas, no snapshot -> no anchor -> refuse
    d = {"type": "orderbook_delta", "msg": {"side": "yes", "price_dollars": "0.5000", "delta_fp": "5"}}
    assert kbr.reliable_nbbo_at([(100.0, d)], 102.0) == (None, None)


def test_nbbo_at_replays_only_up_to_cutoff_no_lookahead():
    """nbbo_at applies frames with recv_epoch <= cutoff ONLY (no look-ahead past
    T-15s). frames are (recv_epoch, inner) sorted ascending."""
    snap = {"type": "orderbook_snapshot",
            "msg": {"yes_dollars_fp": [["0.4500", "50"]], "no_dollars_fp": [["0.5000", "100"]]}}
    d1 = {"type": "orderbook_delta", "msg": {"side": "yes", "price_dollars": "0.4800", "delta_fp": "10"}}
    d2 = {"type": "orderbook_delta", "msg": {"side": "yes", "price_dollars": "0.4900", "delta_fp": "10"}}
    frames = [(100.0, snap), (105.0, d1), (110.0, d2)]
    bid, ask = kbr.nbbo_at(frames, cutoff_epoch=106.0)  # excludes the 110.0 frame
    assert bid == pytest.approx(48.0)   # snapshot + d1 only; d2 (0.49) excluded
    assert ask == pytest.approx(50.0)
    bid2, _ = kbr.nbbo_at(frames, cutoff_epoch=999.0)   # all frames
    assert bid2 == pytest.approx(49.0)
    assert kbr.nbbo_at(frames, cutoff_epoch=50.0) == (None, None)  # before any frame


def test_best_ask_bid_depth():
    """Depth = size you could actually lift. best_yes_ask_depth = size resting at
    the best NO bid (buying YES lifts the NO bid); best_yes_bid_depth = size at
    the best YES bid."""
    b = kbr.KalshiBook()
    b.apply_snapshot(yes_levels=[["0.7500", "10"], ["0.7000", "5"]],
                     no_levels=[["0.2000", "30"], ["0.1800", "7"]])
    assert b.best_yes_ask_cents() == pytest.approx(80.0)     # 100 - 20
    assert b.best_yes_ask_depth() == pytest.approx(30.0)     # size at best no (0.20)
    assert b.best_yes_bid_cents() == pytest.approx(75.0)
    assert b.best_yes_bid_depth() == pytest.approx(10.0)     # size at best yes (0.75)


def test_depth_none_when_side_empty():
    b = kbr.KalshiBook()
    assert b.best_yes_ask_depth() is None
    assert b.best_yes_bid_depth() is None


def test_no_bid_and_yes_no_overround_arb():
    """Within-book arb: best_yes_bid + best_no_bid > 100 means you can SELL both
    sides and collect >100 for a 100-payout contract (risk-free, direction-neutral)."""
    b = kbr.KalshiBook()
    # YES bidders up to 0.60, NO bidders up to 0.45 -> sum 105 > 100 = 5c arb
    b.apply_snapshot(yes_levels=[["0.6000", "30"]], no_levels=[["0.4500", "20"]])
    assert b.best_yes_bid_cents() == pytest.approx(60.0)
    assert b.best_no_bid_cents() == pytest.approx(45.0)
    assert b.best_no_bid_depth() == pytest.approx(20.0)
    # overround = yes_bid + no_bid ; >100 = arb, arb size = min depth
    assert b.best_yes_bid_cents() + b.best_no_bid_cents() == pytest.approx(105.0)
    # a normal book sums to <100 (no arb)
    b2 = kbr.KalshiBook()
    b2.apply_snapshot([["0.5500", "10"]], [["0.4000", "10"]])  # 95 < 100
    assert b2.best_yes_bid_cents() + b2.best_no_bid_cents() == pytest.approx(95.0)


def test_simulate_maker_bid_fill_fills_when_ask_crosses_down():
    """Post a YES bid at the best bid at post_epoch; it FILLS iff the best ask
    later trades down to <= our bid (a seller crosses to us = adverse selection)."""
    snap = {"type": "orderbook_snapshot",
            "msg": {"yes_dollars_fp": [["0.7500", "10"]], "no_dollars_fp": [["0.2000", "10"]]}}
    # at post: yes_bid=75, yes_ask=100-20=80. our posted bid = 75.
    ask_down = {"type": "orderbook_delta", "msg": {"side": "no", "price_dollars": "0.2600", "delta_fp": "5"}}
    # now best no=26 -> yes_ask=74 <= 75 -> our bid fills
    frames = [(100.0, snap), (130.0, ask_down)]
    our_bid, filled = kbr.simulate_maker_bid_fill(frames, post_epoch=100.0)
    assert our_bid == pytest.approx(75.0)
    assert filled is True


def test_simulate_maker_bid_fill_no_fill_when_ask_stays_above():
    snap = {"type": "orderbook_snapshot",
            "msg": {"yes_dollars_fp": [["0.7500", "10"]], "no_dollars_fp": [["0.2000", "10"]]}}
    ask_up = {"type": "orderbook_delta", "msg": {"side": "no", "price_dollars": "0.1000", "delta_fp": "5"}}
    # best no=10 -> yes_ask=90, stays above our bid 75 -> no fill
    frames = [(100.0, snap), (130.0, ask_up)]
    our_bid, filled = kbr.simulate_maker_bid_fill(frames, post_epoch=100.0)
    assert our_bid == pytest.approx(75.0)
    assert filled is False


def test_simulate_maker_bid_fill_only_counts_post_decision_crosses():
    """A cross that happened BEFORE we posted must not count as our fill."""
    snap = {"type": "orderbook_snapshot",
            "msg": {"yes_dollars_fp": [["0.7500", "10"]], "no_dollars_fp": [["0.2600", "10"]]}}
    # pre-post the ask is already 74 (<75) but we only post at t=200
    later_up = {"type": "orderbook_delta", "msg": {"side": "no", "price_dollars": "0.2600", "delta_fp": "-10"}}
    later_up2 = {"type": "orderbook_delta", "msg": {"side": "no", "price_dollars": "0.1000", "delta_fp": "10"}}
    frames = [(100.0, snap), (150.0, later_up), (160.0, later_up2)]
    # at post (t=200) best no=10 -> ask=90; our bid=75; nothing after -> no fill
    our_bid, filled = kbr.simulate_maker_bid_fill(frames, post_epoch=200.0)
    assert filled is False


def test_apply_frame_dispatches_snapshot_and_delta():
    b = kbr.KalshiBook()
    b.apply_frame({"type": "orderbook_snapshot",
                   "msg": {"yes_dollars_fp": [["0.4500", "50"]],
                           "no_dollars_fp": [["0.5000", "100"]]}})
    assert b.best_yes_bid_cents() == pytest.approx(45.0)
    b.apply_frame({"type": "orderbook_delta",
                   "msg": {"side": "yes", "price_dollars": "0.4700", "delta_fp": "10"}})
    assert b.best_yes_bid_cents() == pytest.approx(47.0)
