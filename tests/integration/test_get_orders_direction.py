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

import sqlite3
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


def test_missing_direction_kept_without_side_action():
    """Fail-closed on direction, not existence: keep oid for cancel-sweep."""
    client = _client({"orders": [
        {"order_id": "oid-bad", "ticker": TICKER, "status": "resting"},
        {"order_id": "oid-ok", "ticker": TICKER,
         "outcome_side": "yes", "book_side": "bid", "status": "resting"},
    ]})
    result = KalshiClient.get_orders(client)
    ids = [o["order_id"] for o in result["orders"]]
    assert ids == ["oid-bad", "oid-ok"]
    bad = result["orders"][0]
    assert bad.get("side") not in ("yes", "no")
    assert bad.get("action") not in ("buy", "sell")


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


def test_outcome_side_only_yes_defaults_book_bid_buy():
    client = _client({"orders": [{
        "order_id": "oid-oy",
        "ticker": TICKER,
        "outcome_side": "yes",
        "status": "resting",
    }]})
    o = KalshiClient.get_orders(client)["orders"][0]
    assert o["side"] == "yes"
    assert o["book_side"] == "bid"
    assert o["action"] == "buy"


def test_outcome_side_only_no_defaults_book_ask_buy():
    client = _client({"orders": [{
        "order_id": "oid-on",
        "ticker": TICKER,
        "outcome_side": "no",
        "status": "resting",
    }]})
    o = KalshiClient.get_orders(client)["orders"][0]
    assert o["side"] == "no"
    assert o["book_side"] == "ask"
    assert o["action"] == "buy"


def test_non_string_direction_is_stripped_not_raised():
    """Schema slip (int/list) must not AttributeError out of get_orders."""
    result = KalshiClient.get_orders(_client({"orders": [{
        "order_id": "oid-ns",
        "ticker": TICKER,
        "outcome_side": 123,
        "book_side": ["bid"],
        "status": "resting",
    }]}))
    o = result["orders"][0]
    assert o["order_id"] == "oid-ns"
    assert o.get("side") not in ("yes", "no")
    assert o.get("action") not in ("buy", "sell")


def test_sell_no_mirror_pair_maps_to_buy_yes_exposure():
    """buy-yes ≡ sell-no ≡ (yes, bid). Mirror legacy pair → buy-yes.

    Mixing canonical outcome=yes with legacy action=sell (and omitting
    legacy side) used to emit sell-YES — inverted exposure. The mirror
    pair is side=no action=sell.
    """
    client = _client({"orders": [{
        "order_id": "oid-sn",
        "ticker": TICKER,
        "outcome_side": "yes",
        "book_side": "bid",
        "side": "no",
        "action": "sell",
        "status": "resting",
    }]})
    o = KalshiClient.get_orders(client)["orders"][0]
    assert o["side"] == "yes"
    assert o["action"] == "buy"


def test_conflicting_legacy_buy_no_inverts_off_canonical_buy_yes():
    """Invert branch is the only path that changes pair-default action.

    (yes, bid) defaults to buy. Legacy (no, buy) conflicts — invert
    yields sell. Deleting the elif leaves action=buy and this fails.
    """
    client = _client({"orders": [{
        "order_id": "oid-inv",
        "ticker": TICKER,
        "outcome_side": "yes",
        "book_side": "bid",
        "side": "no",
        "action": "buy",
        "status": "resting",
    }]})
    o = KalshiClient.get_orders(client)["orders"][0]
    assert o["side"] == "yes"
    assert o["action"] == "sell"


def test_legacy_action_alone_does_not_override_canonical_buy():
    """action=sell without legacy side is ambiguous — keep (yes,bid)=buy."""
    client = _client({"orders": [{
        "order_id": "oid-amb",
        "ticker": TICKER,
        "outcome_side": "yes",
        "book_side": "bid",
        "action": "sell",
        "status": "resting",
    }]})
    o = KalshiClient.get_orders(client)["orders"][0]
    assert o["side"] == "yes"
    assert o["action"] == "buy"


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


def test_reconcile_cancels_malformed_direction_order(tmp_path):
    """Dropped oids skip the cancel sweep then get flipped local-canceled.

    Keep the id so cancel_order still fires. Direction guards skip INSERT.
    """
    from bot.state import StateManager
    s = StateManager(str(tmp_path / "get_orders_dir.db"))
    try:
        event = "KXBTC15M-26SEP071200"
        s.insert_bot_order("mk-bad", TICKER, event, "BTC", "yes", 2, 45, False)
        s.confirm_order_submitted("mk-bad", "oid-bad")
        wrapped = KalshiClient.get_orders(_client({"orders": [
            {"order_id": "oid-bad", "client_order_id": "mk-bad",
             "ticker": TICKER, "status": "resting"},
        ]}))
        client = MagicMock()
        client.get_orders.return_value = wrapped
        client.cancel_order.return_value = {"order": {"status": "canceled"}}
        s._reconcile_orders(client, "2026-09-07T00:00:00.000000Z")
        assert client.cancel_order.called, (
            "malformed-direction oid must stay in the cancel sweep")
        st = s.conn.execute(
            "SELECT status FROM pending_orders WHERE order_id='oid-bad'"
        ).fetchone()["status"]
        assert st == "canceled"
    finally:
        s.close()


def test_reconcile_skips_insert_of_unknown_malformed_oid(tmp_path, caplog):
    """INSERT path of _reconcile_orders must skip, not KeyError, no row."""
    import logging
    from bot.state import StateManager
    s = StateManager(str(tmp_path / "get_orders_insert.db"))
    try:
        wrapped = KalshiClient.get_orders(_client({"orders": [
            {"order_id": "oid-new-bad", "client_order_id": "mk-new-bad",
             "ticker": TICKER, "status": "resting"},
        ]}))
        client = MagicMock()
        client.get_orders.return_value = wrapped
        client.cancel_order.return_value = {"order": {"status": "canceled"}}
        with caplog.at_level(logging.WARNING):
            s._reconcile_orders(client, "2026-09-07T00:00:00.000000Z")
        assert client.cancel_order.called
        row = s.conn.execute(
            "SELECT 1 FROM pending_orders WHERE order_id='oid-new-bad'"
        ).fetchone()
        assert row is None, "malformed unknown oid must not be INSERTed"
        assert "RECONCILE_ORDER_DIRECTION_MALFORMED" in caplog.text
    finally:
        s.close()


def test_longshot_boot_malformed_recovers_side_from_ledger(tmp_path):
    """ls- orders are skipped by state reconcile. Recover buy_side from
    the local row so adopt+cancel still runs."""
    from bot.state import StateManager
    from bot.longshot import LongshotEngine
    s = StateManager(str(tmp_path / "ls_dir_ledger.db"))
    try:
        event = "KXBTC15M-26SEP071200"
        s.insert_bot_order("ls-bad", TICKER, event, "BTC", "no", 2, 45, False)
        s.confirm_order_submitted("ls-bad", "oid-ls-bad")
        wrapped = KalshiClient.get_orders(_client({"orders": [
            {"order_id": "oid-ls-bad", "client_order_id": "ls-bad",
             "ticker": TICKER, "status": "resting",
             "no_price": 45, "remaining_count": 2, "count": 2},
        ]}))
        client = MagicMock()
        client.get_orders.return_value = wrapped
        client.get_fills.return_value = {"fills": [
            {"order_id": "oid-ls-bad", "client_order_id": "ls-bad",
             "trade_id": "t-ls-bad", "count": 2, "ts": 1_000_000.0},
        ]}
        client.cancel_order.return_value = {"order": {"status": "canceled"}}
        engine = LongshotEngine(client, s)
        engine.tick()
        assert client.get_fills.called, (
            "ledger recovery must adopt (fill-poll) not last-resort cancel")
        pos = s.conn.execute(
            "SELECT side, count FROM positions WHERE ticker=? AND status='open'",
            (TICKER,)).fetchone()
        assert pos is not None and pos["side"] == "no" and pos["count"] == 2
        assert client.cancel_order.called
        st = s.conn.execute(
            "SELECT status FROM pending_orders WHERE order_id='oid-ls-bad'"
        ).fetchone()["status"]
        assert st != "resting"
        assert engine.resting_count() == 0
    finally:
        s.close()


def test_longshot_boot_malformed_no_local_side_cancels(tmp_path):
    """No API direction and no local row: cancel the unadoptable orphan."""
    from bot.state import StateManager
    from bot.longshot import LongshotEngine
    s = StateManager(str(tmp_path / "ls_dir_nolocal.db"))
    try:
        wrapped = KalshiClient.get_orders(_client({"orders": [
            {"order_id": "oid-ls-orphan", "client_order_id": "ls-orphan",
             "ticker": TICKER, "status": "resting"},
        ]}))
        client = MagicMock()
        client.get_orders.return_value = wrapped
        client.get_fills.return_value = {"fills": []}
        client.cancel_order.return_value = {"order": {"status": "canceled"}}
        engine = LongshotEngine(client, s)
        engine.tick()
        assert client.cancel_order.called, (
            "unadoptable ls- orphan must be canceled, not left resting")
        assert engine.resting_count() == 0
        assert not client.get_fills.called, (
            "last-resort cancel must not be confused with adopt+poll")
    finally:
        s.close()


def test_longshot_boot_failed_cancel_does_not_latch(tmp_path):
    """cancel_order raise must not set _boot_reconciled or mark canceled."""
    from bot.state import StateManager
    from bot.longshot import LongshotEngine
    s = StateManager(str(tmp_path / "ls_dir_cancelfail.db"))
    try:
        wrapped = KalshiClient.get_orders(_client({"orders": [
            {"order_id": "oid-ls-cf", "client_order_id": "ls-cf",
             "ticker": TICKER, "status": "resting"},
        ]}))
        client = MagicMock()
        client.get_orders.return_value = wrapped
        client.get_fills.return_value = {"fills": []}
        client.cancel_order.side_effect = RuntimeError("breaker open")
        engine = LongshotEngine(client, s)
        engine.tick()
        assert engine._boot_reconciled is False
        assert engine.resting_count() == 0
    finally:
        s.close()


def test_longshot_boot_cancel_none_does_not_latch(tmp_path):
    """cancel_order returning None (timeout/5xx) must not latch boot."""
    from bot.state import StateManager
    from bot.longshot import LongshotEngine
    s = StateManager(str(tmp_path / "ls_dir_cancelnone.db"))
    try:
        wrapped = KalshiClient.get_orders(_client({"orders": [
            {"order_id": "oid-ls-cn", "client_order_id": "ls-cn",
             "ticker": TICKER, "status": "resting"},
        ]}))
        client = MagicMock()
        client.get_orders.return_value = wrapped
        client.get_fills.return_value = {"fills": []}
        client.cancel_order.return_value = None
        engine = LongshotEngine(client, s)
        engine.tick()
        assert engine._boot_reconciled is False
    finally:
        s.close()


def test_longshot_boot_cancel_500_does_not_latch(tmp_path):
    from bot.state import StateManager
    from bot.longshot import LongshotEngine
    s = StateManager(str(tmp_path / "ls_dir_cancel500.db"))
    try:
        wrapped = KalshiClient.get_orders(_client({"orders": [
            {"order_id": "oid-ls-c5", "client_order_id": "ls-c5",
             "ticker": TICKER, "status": "resting"},
        ]}))
        client = MagicMock()
        client.get_orders.return_value = wrapped
        client.get_fills.return_value = {"fills": []}
        client.cancel_order.return_value = {
            "_error": True, "_status_code": 500}
        engine = LongshotEngine(client, s)
        engine.tick()
        assert engine._boot_reconciled is False
    finally:
        s.close()


def test_longshot_boot_ledger_lock_retries_does_not_cancel(tmp_path):
    """database is locked on the side SELECT must not last-resort cancel."""
    from bot.state import StateManager
    from bot.longshot import LongshotEngine
    s = StateManager(str(tmp_path / "ls_dir_lock.db"))
    try:
        event = "KXBTC15M-26SEP071200"
        s.insert_bot_order("ls-lock", TICKER, event, "BTC", "no", 2, 45, False)
        s.confirm_order_submitted("ls-lock", "oid-ls-lock")
        wrapped = KalshiClient.get_orders(_client({"orders": [
            {"order_id": "oid-ls-lock", "client_order_id": "ls-lock",
             "ticker": TICKER, "status": "resting",
             "no_price": 45, "remaining_count": 2, "count": 2},
        ]}))
        client = MagicMock()
        client.get_orders.return_value = wrapped
        client.get_fills.return_value = {"fills": []}
        client.cancel_order.return_value = {"order": {"status": "canceled"}}
        engine = LongshotEngine(client, s)
        real_conn = s.conn

        class _ConnProxy:
            def execute(self, sql, *a, **k):
                if isinstance(sql, str) and "SELECT side FROM pending_orders" in sql:
                    raise sqlite3.OperationalError("database is locked")
                return real_conn.execute(sql, *a, **k)

            def __getattr__(self, name):
                return getattr(real_conn, name)

        class _StateProxy:
            def __init__(self, inner):
                self._inner = inner

            @property
            def conn(self):
                return _ConnProxy()

            def __getattr__(self, name):
                return getattr(self._inner, name)

        engine._state = _StateProxy(s)
        engine.tick()
        assert not client.cancel_order.called, (
            "ledger lock must retry next tick, not cancel a recoverable orphan")
        assert engine._boot_reconciled is False
        st = real_conn.execute(
            "SELECT status FROM pending_orders WHERE order_id='oid-ls-lock'"
        ).fetchone()["status"]
        assert st == "resting"
    finally:
        s.close()


def test_longshot_boot_honors_wrapped_sell_action(tmp_path):
    """outcome=yes action=sell is sell-YES = long NO, not buy-YES."""
    from bot.state import StateManager
    from bot.longshot import LongshotEngine
    s = StateManager(str(tmp_path / "ls_dir_sell.db"))
    try:
        event = "KXBTC15M-26SEP071200"
        s.insert_bot_order("ls-sell", TICKER, event, "BTC", "no", 2, 90, False)
        s.confirm_order_submitted("ls-sell", "oid-ls-sell")
        wrapped = KalshiClient.get_orders(_client({"orders": [
            {"order_id": "oid-ls-sell", "client_order_id": "ls-sell",
             "ticker": TICKER, "status": "resting",
             "outcome_side": "yes", "book_side": "ask",
             "yes_price": 10, "no_price": 90,
             "remaining_count": 2, "count": 2},
        ]}))
        assert wrapped["orders"][0]["side"] == "yes"
        assert wrapped["orders"][0]["action"] == "sell"
        client = MagicMock()
        client.get_orders.return_value = wrapped
        client.get_fills.return_value = {"fills": [
            {"order_id": "oid-ls-sell", "client_order_id": "ls-sell",
             "trade_id": "t-ls-sell", "count": 2, "ts": 1_000_000.0},
        ]}
        client.cancel_order.return_value = {"order": {"status": "canceled"}}
        engine = LongshotEngine(client, s)
        engine.tick()
        pos = s.conn.execute(
            "SELECT side, count, avg_price_cents FROM positions "
            "WHERE ticker=? AND status='open'", (TICKER,)).fetchone()
        assert pos is not None and pos["side"] == "no" and pos["count"] == 2
        assert pos["avg_price_cents"] == 90
    finally:
        s.close()


def test_longshot_boot_sell_yes_yes_book_only_price(tmp_path):
    """v2 YES-book body has only yes_price_dollars; buy-NO is 100-yes."""
    from bot.state import StateManager
    from bot.longshot import LongshotEngine
    s = StateManager(str(tmp_path / "ls_dir_ybook.db"))
    try:
        event = "KXBTC15M-26SEP071200"
        s.insert_bot_order("ls-yb", TICKER, event, "BTC", "no", 2, 90, False)
        s.confirm_order_submitted("ls-yb", "oid-ls-yb")
        wrapped = KalshiClient.get_orders(_client({"orders": [
            {"order_id": "oid-ls-yb", "client_order_id": "ls-yb",
             "ticker": TICKER, "status": "resting",
             "outcome_side": "yes", "book_side": "ask",
             "yes_price_dollars": "0.1000",
             "remaining_count": 2, "count": 2},
        ]}))
        client = MagicMock()
        client.get_orders.return_value = wrapped
        client.get_fills.return_value = {"fills": [
            {"order_id": "oid-ls-yb", "client_order_id": "ls-yb",
             "trade_id": "t-ls-yb", "count": 2, "ts": 1_000_000.0},
        ]}
        client.cancel_order.return_value = {"order": {"status": "canceled"}}
        engine = LongshotEngine(client, s)
        engine.tick()
        pos = s.conn.execute(
            "SELECT side, avg_price_cents FROM positions "
            "WHERE ticker=? AND status='open'", (TICKER,)).fetchone()
        assert pos is not None and pos["side"] == "no"
        assert pos["avg_price_cents"] == 90
    finally:
        s.close()


def test_longshot_boot_outcome_only_yes_uses_yes_price(tmp_path):
    """outcome_side=yes with no book_side is buy-YES, not inverted NO."""
    from bot.state import StateManager
    from bot.longshot import LongshotEngine
    s = StateManager(str(tmp_path / "ls_dir_oy.db"))
    try:
        event = "KXBTC15M-26SEP071200"
        s.insert_bot_order("ls-oy", TICKER, event, "BTC", "yes", 2, 45, False)
        s.confirm_order_submitted("ls-oy", "oid-ls-oy")
        wrapped = KalshiClient.get_orders(_client({"orders": [
            {"order_id": "oid-ls-oy", "client_order_id": "ls-oy",
             "ticker": TICKER, "status": "resting",
             "outcome_side": "yes", "yes_price": 45, "no_price": 55,
             "remaining_count": 2, "count": 2},
        ]}))
        assert wrapped["orders"][0]["action"] == "buy"
        assert wrapped["orders"][0]["book_side"] == "bid"
        client = MagicMock()
        client.get_orders.return_value = wrapped
        client.get_fills.return_value = {"fills": [
            {"order_id": "oid-ls-oy", "client_order_id": "ls-oy",
             "trade_id": "t-ls-oy", "count": 2, "ts": 1_000_000.0},
        ]}
        client.cancel_order.return_value = {"order": {"status": "canceled"}}
        engine = LongshotEngine(client, s)
        engine.tick()
        pos = s.conn.execute(
            "SELECT side, count, avg_price_cents FROM positions "
            "WHERE ticker=? AND status='open'", (TICKER,)).fetchone()
        assert pos is not None and pos["side"] == "yes" and pos["count"] == 2
        assert pos["avg_price_cents"] == 45
    finally:
        s.close()
