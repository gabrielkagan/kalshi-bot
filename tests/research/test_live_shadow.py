"""Live-shadow harness — TDD-first.

Tests order-book edge hypotheses on RELIABLE reconstructed books (the fix) with
REAL fills from actual trade prints (not assumed) on FORWARD clean data. The
methodology-critical piece is the fill model: would my resting order actually
have been hit, given the trades that really printed after I posted?

Fill convention (verified against trade frames): taker_side='no' is a YES-SELL
(the aggressor took the NO side = sold YES, hitting a YES bid); taker_side='yes'
is a YES-BUY (lifting a YES ask). So my YES bid fills on a YES-SELL at/below it;
my NO bid (=offer to sell YES at 100-no_bid) fills on a YES-BUY at/above 100-no_bid.

Parent: kb/decisions/settlement-convergence-worklist.md (live-shadow)
"""

from __future__ import annotations

import pytest

from scripts.research import phase1b_live_shadow as ls


def test_yes_bid_fills_only_on_a_real_yes_sell_reaching_it():
    # trades: (ts, yes_price_cents, taker_side). I post a YES bid at T=100.
    trades = [(90.0, 40, "no"), (105.0, 50, "no"), (110.0, 48, "no")]
    assert ls.would_yes_bid_fill(trades, post_ts=100.0, bid_cents=49) is True   # 48-sell after T
    assert ls.would_yes_bid_fill(trades, post_ts=100.0, bid_cents=47) is False  # nothing <=47
    assert ls.would_yes_bid_fill([(90.0, 40, "no")], post_ts=100.0, bid_cents=49) is False  # pre-T
    # a YES-BUY at 48 (taker_side='yes') does NOT hit my YES bid
    assert ls.would_yes_bid_fill([(105.0, 48, "yes")], post_ts=100.0, bid_cents=49) is False


def test_no_bid_fills_only_on_a_real_yes_buy_high_enough():
    trades = [(105.0, 56, "yes"), (110.0, 60, "yes")]
    # NO bid 45 = offer to sell YES at 55; fills on a YES-BUY at >=55 -> 56 -> True
    assert ls.would_no_bid_fill(trades, post_ts=100.0, no_bid_cents=45) is True
    # NO bid 35 = sell YES at 65; max YES-buy is 60 < 65 -> not filled
    assert ls.would_no_bid_fill(trades, post_ts=100.0, no_bid_cents=35) is False


def test_maker_realized_pnl_on_fill():
    assert ls.maker_pnl_cents(fill_price=40, side="yes", result="yes") == pytest.approx(60.0)
    assert ls.maker_pnl_cents(fill_price=40, side="yes", result="no") == pytest.approx(-40.0)
    assert ls.maker_pnl_cents(fill_price=30, side="no", result="no") == pytest.approx(70.0)
    assert ls.maker_pnl_cents(fill_price=30, side="no", result="yes") == pytest.approx(-30.0)
