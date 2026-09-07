"""Kalshi create-order V2 — 410 deprecated_v1_order_endpoint.

VPS 2026-09-06 21:43Z:
  POST /trade-api/v2/portfolio/orders -> 410 Gone
  code=deprecated_v1_order_endpoint
  "Please switch to the V2 endpoints"
  details=https://docs.kalshi.com/api-reference/orders/create-order-v2

The /trade-api/v2 prefix is not enough: create moved to
POST /trade-api/v2/portfolio/events/orders with bid/ask + fixed-point
dollar price. Executor callers keep side=yes/no + yes_price/no_price
cents; place_order is the adapter. Response is wrapped back to the
pre-existing {order: {order_id, fill_count_fp}} shape.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import bot.constants as C
from bot.kalshi_client import KalshiClient
from bot.helpers.strings import cents_to_dollars_str, int_to_fp_str

REPO = Path(__file__).resolve().parents[2]
TICKER = "KXBTC15M-26SEP061745-45"


def _live(monkeypatch):
    monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", True)
    monkeypatch.setattr(C, "ASSET_LIVE_TRADING", {"BTC": True})
    monkeypatch.setattr(C, "ASSET_LIVE_TRADING_DEFAULT", False)


def _client(resp):
    client = MagicMock()
    client._request.return_value = resp
    return client


def _v2_ok(**extra):
    body = {
        "order_id": "oid-v2-1",
        "client_order_id": "tw-x",
        "fill_count": "0.00",
        "remaining_count": "2.00",
        "ts_ms": 1,
    }
    body.update(extra)
    return body


def test_buy_yes_posts_events_orders_bid(monkeypatch):
    _live(monkeypatch)
    client = _client(_v2_ok(fill_count="0.00", remaining_count="2.00"))
    result = KalshiClient.place_order(
        client, TICKER, "yes", "buy", 2, yes_price=18, client_order_id="tw-x",
        time_in_force="immediate_or_cancel")
    client._request.assert_called_once()
    args, kwargs = client._request.call_args
    assert args[0] == "POST"
    assert args[1] == f"{C.API_PATH_PREFIX}/portfolio/events/orders"
    body = kwargs["json_body"]
    assert body["ticker"] == TICKER
    assert body["side"] == "bid"
    assert body["price"] == cents_to_dollars_str(18)
    assert body["count"] == int_to_fp_str(2)
    assert body["time_in_force"] == "immediate_or_cancel"
    assert body["self_trade_prevention_type"] == "taker_at_cross"
    assert "yes_price" not in body
    assert "action" not in body
    assert "type" not in body
    assert result["order"]["order_id"] == "oid-v2-1"
    assert result["order"]["fill_count_fp"] == "0.00"
    assert result["order"]["remaining_count"] == 2


def test_full_fill_remaining_count_is_zero(monkeypatch):
    """Ghost-fill Layer A keys remaining_count==0, not remaining_count_fp."""
    _live(monkeypatch)
    client = _client(_v2_ok(fill_count="5.00", remaining_count="0.00"))
    result = KalshiClient.place_order(
        client, TICKER, "yes", "buy", 5, yes_price=18, client_order_id="tw-x",
        time_in_force="immediate_or_cancel")
    assert result["order"]["remaining_count"] == 0
    assert result["order"]["fill_count"] == 5


def test_cancel_uses_events_orders_and_market_ticker(monkeypatch):
    client = MagicMock()
    client._request.return_value = {
        "order_id": "oid-c", "reduced_by": "2.00", "ts_ms": 1,
    }
    result = KalshiClient.cancel_order(client, "oid-c", ticker=TICKER)
    args, kwargs = client._request.call_args
    assert args[0] == "DELETE"
    assert args[1] == f"{C.API_PATH_PREFIX}/portfolio/events/orders/oid-c"
    params = kwargs.get("params") or {}
    assert params.get("market_ticker") == TICKER
    assert params.get("exchange_index") == -1
    assert result["order"]["order_id"] == "oid-c"
    assert "fill_count" not in result["order"]
    assert "remaining_count" not in result["order"]
    assert result["order"]["reduced_by"] == 2
    assert result["order"]["reduced_by_fp"] == "2.00"


def test_amend_posts_events_orders_bid(monkeypatch):
    _live(monkeypatch)
    client = MagicMock()
    client._request.return_value = {
        "order_id": "oid-a", "fill_count": "0.00",
        "remaining_count": "1.00", "ts_ms": 1,
    }
    result = KalshiClient.amend_order(
        client, "oid-a", TICKER, "yes", "buy", count=1, yes_price=55)
    args, kwargs = client._request.call_args
    assert args[0] == "POST"
    assert args[1] == (
        f"{C.API_PATH_PREFIX}/portfolio/events/orders/oid-a/amend")
    body = kwargs["json_body"]
    assert body["side"] == "bid"
    assert body["price"] == cents_to_dollars_str(55)
    assert body["count"] == int_to_fp_str(1)
    assert "yes_price" not in body
    assert result["order"]["order_id"] == "oid-a"


def test_buy_no_is_ask_at_one_minus_price(monkeypatch):
    """Buy NO @ 40c = sell YES @ 60c on the single YES book."""
    _live(monkeypatch)
    client = _client(_v2_ok())
    KalshiClient.place_order(
        client, TICKER, "no", "buy", 1, no_price=40, client_order_id="tw-x")
    body = client._request.call_args.kwargs["json_body"]
    assert body["side"] == "ask"
    assert body["price"] == cents_to_dollars_str(60)
    assert body["time_in_force"] == "good_till_canceled"


def test_place_order_never_posts_deprecated_v1_path():
    src = (REPO / "bot" / "kalshi_client.py").read_text(encoding="utf-8")
    assert '_CREATE_ORDER_V2_PATH = f"{API_PATH_PREFIX}/portfolio/events/orders"' in src
    start = src.find("def place_order(")
    end = src.find("\n    def cancel_order(")
    body = src[start:end]
    assert "_CREATE_ORDER_V2_PATH" in body
    assert 'f"{API_PATH_PREFIX}/portfolio/orders"' not in body
    cancel_src = src[src.find("def cancel_order("):src.find("\n    def amend_order(")]
    assert "/portfolio/events/orders/" in cancel_src
    assert 'f"{API_PATH_PREFIX}/portfolio/orders/' not in cancel_src


def test_unmapped_action_does_not_post(monkeypatch):
    _live(monkeypatch)
    client = _client(_v2_ok())
    result = KalshiClient.place_order(
        client, TICKER, "yes", "sell", 1, yes_price=50)
    assert result is None
    client._request.assert_not_called()
