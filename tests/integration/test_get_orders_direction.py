"""GET /portfolio/orders — fail-closed outcome_side/book_side.

Docs (2026-09-07): GET is still /trade-api/v2/portfolio/orders, but
Order.side and Order.action are deprecated (removal allowed after
2026-05-14 / 2026-05-28). Canonical fields are outcome_side (yes|no)
and book_side (bid|ask). Callers (state._reconcile_orders KeyError on
order["side"]/order["action"], longshot boot o.get("side") or "yes")
must not crash or default-yes a NO quote.

buy-yes ≡ outcome_side=yes book_side=bid
buy-no  ≡ outcome_side=no  book_side=ask
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from bot.kalshi_client import KalshiClient, API_PATH_PREFIX

REPO = Path(__file__).resolve().parents[2]
TICKER = "KXBTC15M-26SEP071200-45"


def _client(resp):
    client = MagicMock()
    client._request.return_value = resp
    return client


def test_get_orders_path_is_portfolio_orders_not_events():
    src = (REPO / "bot" / "kalshi_client.py").read_text(encoding="utf-8")
    start = src.find("def get_orders(")
    end = src.find("\n    def get_fills(")
    body = src[start:end]
    assert f'"{API_PATH_PREFIX}/portfolio/orders"' in body or (
        "f\"{API_PATH_PREFIX}/portfolio/orders\"" in body
        or 'f"{API_PATH_PREFIX}/portfolio/orders"' in body)
    assert "/portfolio/events/orders" not in body


def test_outcome_side_yes_bid_is_buy_yes():
    client = _client({"orders": [{
        "order_id": "oid-1",
        "ticker": TICKER,
        "outcome_side": "yes",
        "book_side": "bid",
        "status": "resting",
    }]})
    result = KalshiClient.get_orders(client, status="resting")
    o = result["orders"][0]
    assert o["side"] == "yes"
    assert o["action"] == "buy"
    assert o["outcome_side"] == "yes"
    assert o["book_side"] == "bid"


def test_outcome_side_no_ask_is_buy_no():
    client = _client({"orders": [{
        "order_id": "oid-2",
        "ticker": TICKER,
        "outcome_side": "no",
        "book_side": "ask",
        "status": "resting",
    }]})
    result = KalshiClient.get_orders(client, status="resting")
    o = result["orders"][0]
    assert o["side"] == "no"
    assert o["action"] == "buy"


def test_legacy_side_action_still_round_trips():
    client = _client({"orders": [{
        "order_id": "oid-leg",
        "ticker": TICKER,
        "side": "no",
        "action": "buy",
        "status": "resting",
    }]})
    result = KalshiClient.get_orders(client, status="resting")
    o = result["orders"][0]
    assert o["side"] == "no"
    assert o["action"] == "buy"


def test_missing_direction_is_dropped_not_defaulted_yes():
    """Fail-closed: no outcome_side, book_side, or legacy side → drop."""
    client = _client({"orders": [
        {"order_id": "oid-bad", "ticker": TICKER, "status": "resting"},
        {"order_id": "oid-ok", "ticker": TICKER,
         "outcome_side": "yes", "book_side": "bid", "status": "resting"},
    ]})
    result = KalshiClient.get_orders(client)
    ids = [o["order_id"] for o in result["orders"]]
    assert ids == ["oid-ok"]


def test_book_side_only_maps_bid_to_yes():
    client = _client({"orders": [{
        "order_id": "oid-book",
        "ticker": TICKER,
        "book_side": "bid",
        "status": "resting",
    }]})
    result = KalshiClient.get_orders(client)
    o = result["orders"][0]
    assert o["side"] == "yes"
    assert o["action"] == "buy"


def test_book_side_only_maps_ask_to_no():
    client = _client({"orders": [{
        "order_id": "oid-ask",
        "ticker": TICKER,
        "book_side": "ask",
        "status": "resting",
    }]})
    result = KalshiClient.get_orders(client)
    o = result["orders"][0]
    assert o["side"] == "no"
    assert o["action"] == "buy"


def test_sell_no_legacy_action_kept_with_canonical_fields():
    """buy-yes ≡ sell-no ≡ (yes, bid). Legacy action disambiguates."""
    client = _client({"orders": [{
        "order_id": "oid-sn",
        "ticker": TICKER,
        "outcome_side": "yes",
        "book_side": "bid",
        "action": "sell",
        "status": "resting",
    }]})
    o = KalshiClient.get_orders(client)["orders"][0]
    assert o["side"] == "yes"
    assert o["action"] == "sell"


def test_none_response_passthrough():
    assert KalshiClient.get_orders(_client(None)) is None


def test_reconcile_orders_does_not_keyerror_side_or_action():
    """Defense: state._reconcile_orders must .get side/action, not []."""
    src = (REPO / "bot" / "state.py").read_text(encoding="utf-8")
    start = src.find("def _reconcile_orders(")
    end = src.find("\n    def ", start + 1)
    body = src[start:end]
    assert 'order["side"]' not in body
    assert 'order["action"]' not in body
    assert "RECONCILE_ORDER_DIRECTION_MALFORMED" in body


def test_longshot_boot_does_not_default_yes():
    """Defense: boot must not `o.get("side") or "yes"` a missing side."""
    src = (REPO / "bot" / "longshot.py").read_text(encoding="utf-8")
    start = src.find("def _boot_reconcile_orphans(")
    end = src.find("\n    def ", start + 1)
    body = src[start:end]
    assert 'o.get("side") or "yes"' not in body
    assert "LONGSHOT_BOOT_DIRECTION_MALFORMED" in body
