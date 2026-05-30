"""Retail-flow edge test helpers — TDD-first.

Hypothesis: SMALL (retail) aggressive takers are systematically -EV at the prices
they pay; the maker on the other side collects it. The load-bearing primitive is
the taker's realized PnL sign (who wins), pinned here.

Parent: kb/decisions/settlement-convergence-worklist.md (retail-flow corner)
"""

from __future__ import annotations

import pytest

from scripts.research import phase1b_retail_flow as rf


def test_taker_pnl_yes_side():
    # taker buys YES at 87c; wins if settles yes -> +13, else -87
    assert rf.taker_pnl_cents(yes_c=87, no_c=13, taker_side="yes", result="yes") == pytest.approx(13.0)
    assert rf.taker_pnl_cents(yes_c=87, no_c=13, taker_side="yes", result="no") == pytest.approx(-87.0)


def test_taker_pnl_no_side():
    # taker buys NO at 13c; wins if settles no -> +87, else -13
    assert rf.taker_pnl_cents(yes_c=87, no_c=13, taker_side="no", result="no") == pytest.approx(87.0)
    assert rf.taker_pnl_cents(yes_c=87, no_c=13, taker_side="no", result="yes") == pytest.approx(-13.0)


def test_parse_trade_extracts_fields():
    import json
    line = json.dumps({
        "_wire_recv_ts": "2026-05-29T05:03:51.592050Z",
        "_source": "kalshi_ws", "_channel": "trade",
        "_raw": json.dumps({
            "type": "trade", "msg": {
                "market_ticker": "KXETH15M-26MAY290115-15",
                "yes_price_dollars": "0.8700", "no_price_dollars": "0.1300",
                "count_fp": "5.00", "taker_side": "yes", "ts": 1780031028,
            }}),
    })
    t = rf.parse_trade(line)
    assert t["ticker"] == "KXETH15M-26MAY290115-15"
    assert t["asset"] == "ETH"
    assert t["taker_side"] == "yes"
    assert t["yes_c"] == pytest.approx(87.0)
    assert t["no_c"] == pytest.approx(13.0)
    assert t["count"] == pytest.approx(5.0)
    assert t["ts"] == pytest.approx(1780031028.0)


def test_parse_trade_skips_non_crypto():
    import json
    line = json.dumps({"_raw": json.dumps({"type": "trade", "msg": {
        "market_ticker": "KXNBA-LAL-BOS", "yes_price_dollars": "0.50",
        "no_price_dollars": "0.50", "count_fp": "1", "taker_side": "yes", "ts": 1}})})
    assert rf.parse_trade(line) is None
