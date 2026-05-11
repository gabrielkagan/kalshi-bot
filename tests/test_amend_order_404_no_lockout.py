"""Amend-order 404 must NOT create an asset-lockout pathway.

Sister concern to the cancel-404 lockout incident
(kb/failures/cancel-404-asset-lockout-may04.md). Both share the
expiry-race class: maker order auto-aged by Kalshi between bot
decision and API arrival.

Audit findings (P2.3, 2026-05-04):

  1. `KalshiClient.amend_order` (bot/_impl.py:2605) uses POST, not DELETE,
     so the cancel-404 sentinel `{"_error": True, "_status_code": 404}`
     does NOT fire on amend-404. Amend-404 → `_request` raises
     `HTTPError` → caught by `except RequestException` → returns None.

  2. `OrderExecutor._reprice_maker` (bot/_impl.py:21085) is the only
     consumer of `client.amend_order`. On `resp is None` it logs
     `amend_failed_fallback` and returns False. CRITICALLY it does
     NOT mutate `self._active_orders`. The asset-lockout class
     (cancel-404 May 4 incident) requires the order to be removed
     from internal state while remaining live on Kalshi — amend
     failure leaves both sides in sync (order still in
     `_active_orders`, still resting on Kalshi until natural
     expiry/fill/escalation).

  3. `_reprice_maker` itself has NO production callers in bot/_impl.py
     (only references are its own definition, a stale docstring in
     `_cancel_active`, and `tests/test_weather_no_side.py`).
     Residual code from before the multi-asset refactor (commit
     baed2ed). Dead code today; this test future-proofs the contract
     in case it gets rewired.

These tests pin the no-pop-on-amend-failure invariant. If a future
refactor adds `self._active_orders.pop(asset, None)` to the failure
branch of `_reprice_maker` (e.g. trying to "clean up" on amend
failure), the lockout-by-deletion class fires: bot's internal state
diverges from Kalshi's, a stale rest-on-Kalshi order can fill while
the bot has forgotten it, and the bot will place a duplicate order
on the next scan.
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot
import bot.executor  # noqa: F401
import bot.kalshi_client  # noqa: F401


def _make_executor():
    """Mirror the helper in test_cancel_404_pops_active_orders.py:
    bypass __init__, set the minimum required state for
    _reprice_maker to run."""
    e = bot.executor.OrderExecutor.__new__(bot.executor.OrderExecutor)
    e._active_orders = {}
    e._client = MagicMock()
    e._state = MagicMock()
    e._logger = MagicMock()
    # _reprice_maker writes to these counters.
    e._session_amend_attempts = 0
    e._session_amend_successes = 0
    return e


def _make_active_order(asset="ETH", order_id="amend-test-uuid",
                       ticker="KXETH15M-26MAY041030-30",
                       count=30, price_cents=90, side="yes"):
    """Minimal order shape sufficient for _reprice_maker."""
    return {
        "asset": asset,
        "order_id": order_id,
        "ticker": ticker,
        "count": count,
        "price_cents": price_cents,
        "side": side,
        "submit_time": time.time() - 10,
        "filled_so_far": 0,
    }


class TestAmend404DoesNotLockOut(unittest.TestCase):

    def test_amend_failure_does_not_pop_active_orders(self):
        """Amend returning None (the shape of any 404/5xx/network
        error from `_request`) must leave `_active_orders` untouched
        so the order remains tracked through its natural lifecycle.

        Popping here would be the lockout-by-deletion regression: bot
        forgets a still-resting Kalshi order, scans again, places a
        duplicate, possibly fills both → 2× position.
        """
        e = _make_executor()
        order = _make_active_order(asset="ETH")
        e._active_orders["ETH"] = order
        # Any 404/5xx/network error from `_request` collapses to None
        # at the amend_order boundary — sentinel is DELETE-only.
        e._client.amend_order = MagicMock(return_value=None)

        ok = e._reprice_maker(89)

        self.assertFalse(
            ok,
            "Amend failure must return False so callers can fall back "
            "to cancel-replace.")
        self.assertIn(
            "ETH", e._active_orders,
            "Amend failure must NOT remove the asset from "
            "_active_orders — the order is still resting on Kalshi "
            "(or just auto-expired). Cleanup happens via the normal "
            "lifecycle (fill/escalation/cancel via V8/force-pop "
            "backstop). Popping here re-creates the lockout class in "
            "a different shape: bot's state diverges from Kalshi's, "
            "and a stale rest-on-Kalshi order could fill while the "
            "bot has forgotten it.")
        # Order's tracked fields must also remain unchanged so the
        # natural lifecycle paths see a coherent state.
        self.assertEqual(
            e._active_orders["ETH"]["price_cents"], 90,
            "Failed amend must not mutate the tracked price — only a "
            "successful amend updates `price_cents`.")

    def test_amend_failure_is_counted_in_session_attempts(self):
        """Belt-and-suspenders: the attempt counter must increment
        even on failure (it's an attempts counter, not a successes
        counter). Pin this so a refactor that moves the increment
        below the success check is caught."""
        e = _make_executor()
        e._active_orders["ETH"] = _make_active_order(asset="ETH")
        e._client.amend_order = MagicMock(return_value=None)

        e._reprice_maker(89)

        self.assertEqual(
            e._session_amend_attempts, 1,
            "Attempts counter must increment on every call, "
            "including failures.")
        self.assertEqual(
            e._session_amend_successes, 0,
            "Successes counter must NOT increment on failure.")


class TestPost404DoesNotReturnSentinel(unittest.TestCase):
    """The cancel-404 sentinel is DELETE-only by design (bot/_impl.py:2486:
    `if resp.status_code == 404 and method == "DELETE"`). POST 404
    must fall through to the existing `RequestException` handler →
    return None.

    Why this matters for amend: if a future change broadens the
    sentinel to all 404s ("safer to be idempotent everywhere"),
    `amend_order` (a POST) would return the dict sentinel. Then
    `_reprice_maker`'s `if resp is None` check at bot/_impl.py:21099 would
    be False, the truthy dict would flow into the success path, and
    `price_cents` (line 21105) would be mutated against an order
    that no longer exists on Kalshi. State-divergence: bot thinks
    order has new price, Kalshi has no order at all → re-creates the
    lockout class via a different shape than the May 4 incident.

    Sister test in test_cancel_404_pops_active_orders.py covers GET
    404 → None; this covers the POST analog so the load-bearing
    DELETE-only invariant is pinned on both sides.
    """

    def test_post_404_returns_none_not_sentinel(self):
        c = bot.kalshi_client.KalshiClient.__new__(bot.kalshi_client.KalshiClient)
        c.session = MagicMock()
        c.api_key = "test-key"
        c._create_signature = MagicMock(return_value="sig")
        c._rate_limit_wait = MagicMock()

        resp = MagicMock()
        resp.status_code = 404
        resp.text = '{"error":{"code":"not_found"}}'
        resp.headers = {}
        resp.content = b'{"error":{"code":"not_found"}}'
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=resp)
        c.session.request = MagicMock(return_value=resp)

        result = c._request(
            "POST",
            "/trade-api/v2/portfolio/orders/test-uuid/amend",
            json_body={"yes_price": 89, "ticker": "x", "side": "yes",
                       "action": "buy"})

        self.assertIsNone(
            result,
            "POST 404 must return None — sentinel is DELETE-only. If "
            "this returns a dict, _reprice_maker treats 404 as a "
            "successful amend and mutates price_cents against an "
            "order that no longer exists on Kalshi.")


if __name__ == "__main__":
    unittest.main()
