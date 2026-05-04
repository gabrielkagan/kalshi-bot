"""Cancel 404 must pop _active_orders[asset] (asset-lockout regression).

May 4 2026 incident (kb/failures/cancel-404-asset-lockout-may04.md):
ETH and BTC orders auto-expired by Kalshi. Bot called cancel_order →
Kalshi returned 404. KalshiClient._request flattened that to None.
_cancel_order conservatively kept the order in self._active_orders to
avoid double-positions. Polling loop retried cancel forever; the
asset stayed in _active_orders, gating new entries on lines
19693/19989 (`if asset in self._active_orders`). ETH was blocked
~8.5h, BTC ~3.75h until bot restart.

Fix design V2 (after adversarial round 1):

1. KalshiClient._request distinguishes HTTP 404 from None on DELETE.
   On DELETE 404 it returns a sentinel `{"_error": True,
   "_status_code": 404}`. Other errors (5xx, ConnectionError,
   Timeout) still return None. Other methods' 404s unchanged.

2. OrderExecutor._cancel_order routes the 404 sentinel through a
   _handle_cancel_404 helper that VERIFIES the order is gone via
   get_orders(ticker=...) before popping (defense against
   wrong-order-id / caller bugs that would otherwise allow a
   duplicate position).

3. The cancel_pending reconciliation in _tick_one mirrors the same
   404 routing.

4. Terminal status written is "expired" (not "canceled") because
   Kalshi auto-aged the order — operator-initiated cancel vs
   time-based expiration are distinct events. Fill-model training
   label uses "expired" too, to avoid spurious correlations.

5. Logging at WARNING with a session counter, not INFO/ERROR. INFO
   removes the smoke alarm; ERROR ×2 (current) is spammy. WARNING
   ×1 + counter preserves visibility without log-flooding.

6. _tick_one gains a force-pop backstop: if `remaining < -60s` and
   the order is still in _active_orders, force-pop with WARNING.
   Catches any future 404-variant the targeted fix doesn't anticipate.

These tests fail on the unfixed code at the precise points the V2 fix
addresses, and pass after it lands.
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot


# ─── Helpers ─────────────────────────────────────────────────────────────

def _make_executor():
    """Stub OrderExecutor bypassing __init__."""
    e = bot.OrderExecutor.__new__(bot.OrderExecutor)
    e._active_orders = {}
    e._escalating_assets = set()
    e._client = MagicMock()
    # Default: get_orders returns no matching orders (i.e. order is gone).
    # Tests that exercise the wrong-order-id defense override this.
    e._client.get_orders = MagicMock(return_value={"orders": []})
    e._client.get_fills = MagicMock(return_value={"fills": []})
    e._state = MagicMock()
    e._logger = MagicMock()
    e._log_fill_model_sample = MagicMock()
    return e


def _reset_breaker_registry():
    """Tests that exercise real _request paths can pollute the global
    _BREAKER_REGISTRY across test runs (round-2 review critique #6).
    Call from setUp to ensure each test starts with a clean breaker
    state.

    Round-4 critique #4: hard-assert the attribute name so a future
    refactor of circuit_breaker.py surfaces here loudly instead of
    silently turning the reset into a no-op (yesterday's lesson:
    no silent test infrastructure failures)."""
    registry = bot._BREAKER_REGISTRY
    assert hasattr(registry, '_breakers'), (
        "circuit_breaker.REGISTRY internal attribute name drifted "
        "(was '_breakers'). Update _reset_breaker_registry to match.")
    registry._breakers.clear()


def _make_active_order(asset="ETH", order_id="test-uuid-404",
                       ticker="KXETH15M-26MAY032200-00",
                       count=30, price_cents=90,
                       seconds_to_close=600):
    """Shape mirrors what _submit_maker stores at line 22212.

    `_last_poll = 0` ensures _tick_one doesn't short-circuit on the
    MAKER_POLL_INTERVAL throttle at line 20480 — we want the
    reconciliation block at line 20484 to actually run.

    `cancel_pending = False` so test #7's assertion is non-vacuous."""
    return {
        "asset": asset,
        "order_id": order_id,
        "ticker": ticker,
        "count": count,
        "price_cents": price_cents,
        "submit_time": time.time() - 10,
        "_last_poll": 0,
        "_last_queue_poll": 0,
        "filled_so_far": 0,
        "candidate": {"calibrated_prob": 0.93},
        "seconds_to_close_at_submit": seconds_to_close,
        "_ask_history": [],
        "cancel_pending": False,
    }


def _make_client_with_mocked_session():
    """KalshiClient bypassing __init__ — we mock at session.request
    so the test exercises the full _request code path (the bug
    we're pinning is in _request, not at the client method
    boundary)."""
    c = bot.KalshiClient.__new__(bot.KalshiClient)
    c.session = MagicMock()
    c.api_key = "test-key"
    c._create_signature = MagicMock(return_value="sig")
    c._rate_limit_wait = MagicMock()
    return c


def _http_response(status_code, body_text='{"ok":true}'):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = body_text
    resp.headers = {}
    resp.content = body_text.encode()
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=resp)
    else:
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"ok": True}
    return resp


# ─── Invariant A: _request returns sentinel for DELETE 404 only ──────────

class TestRequestDistinguishes404FromOtherErrors(unittest.TestCase):
    """_request must thread the DELETE 404 status code through. Other
    methods and other status codes preserve current None semantics."""

    def test_delete_404_returns_sentinel_with_status_code(self):
        c = _make_client_with_mocked_session()
        c.session.request = MagicMock(
            return_value=_http_response(
                404, '{"error":{"code":"not_found"}}'))

        result = c._request("DELETE", "/trade-api/v2/portfolio/orders/x")

        self.assertIsNotNone(
            result,
            "DELETE 404 must NOT collapse to None — that loses the "
            "'idempotently gone' signal that lets _cancel_order pop "
            "the asset cleanly.")
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("_status_code"), 404)
        self.assertTrue(result.get("_error"))

    def test_get_404_still_returns_none(self):
        """Non-DELETE 404s preserve current behavior to avoid
        accidentally changing semantics for other call sites."""
        c = _make_client_with_mocked_session()
        c.session.request = MagicMock(
            return_value=_http_response(
                404, '{"error":{"code":"not_found"}}'))

        result = c._request("GET", "/trade-api/v2/portfolio/balance")

        self.assertIsNone(
            result,
            "Non-DELETE 404 must remain None — only DELETE has "
            "idempotent-success semantics. Changing GET/POST 404 "
            "behavior would silently affect other callers.")

    def test_delete_500_still_returns_none(self):
        c = _make_client_with_mocked_session()
        c.session.request = MagicMock(
            return_value=_http_response(500, "internal error"))

        result = c._request(
            "DELETE", "/trade-api/v2/portfolio/orders/x")

        self.assertIsNone(
            result,
            "5xx must remain None so transient-error semantics are "
            "preserved (callers retry / circuit-breaker trips).")

    def test_connection_error_still_returns_none(self):
        c = _make_client_with_mocked_session()
        c.session.request = MagicMock(
            side_effect=requests.exceptions.ConnectionError("network down"))

        result = c._request(
            "DELETE", "/trade-api/v2/portfolio/orders/x")

        self.assertIsNone(
            result,
            "Connection failures must remain None (transient).")


# ─── Invariant B/D/E: _cancel_order routes 404 → expired status ──────────

class TestCancelOrder404Handling(unittest.TestCase):
    """Direct cancel returning 404 sentinel must pop _active_orders[asset]
    AND label the terminal status as 'expired' (not 'canceled')."""

    def test_404_pops_asset_from_active_orders(self):
        e = _make_executor()
        e._active_orders["ETH"] = _make_active_order(asset="ETH")
        e._client.cancel_order = MagicMock(
            return_value={"_error": True, "_status_code": 404})

        ok = e._cancel_order("ETH", "test_404_path")

        self.assertTrue(
            ok,
            "404 means Kalshi already expired/canceled the order — "
            "caller is safe to submit a replacement.")
        self.assertNotIn(
            "ETH", e._active_orders,
            "Asset must be popped so subsequent ticks can place "
            "new orders.")
        # Round-3 critique P0-1: prove V3 helper actually ran (verify
        # path was invoked). Without this, the test passes on unfixed
        # code via the existing else-branch pop, never exercising
        # `_handle_cancel_404`.
        e._client.get_orders.assert_called()

    def test_404_marks_state_as_expired_not_canceled(self):
        """Kalshi auto-aged the order (expired). Marking it 'canceled'
        is a category error — operator-initiated cancel vs time-based
        expiration are distinct lifecycle events. Tagging the wrong one
        pollutes the pending_orders audit + downstream analytics."""
        e = _make_executor()
        e._active_orders["ETH"] = _make_active_order(
            asset="ETH", order_id="uuid-eth")
        e._client.cancel_order = MagicMock(
            return_value={"_error": True, "_status_code": 404})

        e._cancel_order("ETH", "test_status_label")

        e._state.mark_order_status.assert_called_once_with(
            "uuid-eth", "expired")

    def test_404_logs_fill_model_sample_as_expired(self):
        """The fill-model trains on this label. Calling 404-popped
        orders 'canceled' would teach the model that low-bid maker
        orders correlate with cancellation events, when really they
        correlate with time-based expirations. Different physics."""
        e = _make_executor()
        e._active_orders["ETH"] = _make_active_order(asset="ETH")
        e._client.cancel_order = MagicMock(
            return_value={"_error": True, "_status_code": 404})

        e._cancel_order("ETH", "test_fill_model_label")

        e._log_fill_model_sample.assert_called_once()
        args, kwargs = e._log_fill_model_sample.call_args
        outcome = args[1] if len(args) > 1 else kwargs.get("outcome")
        self.assertEqual(
            outcome, "expired",
            "Fill-model sample outcome must be 'expired' not 'canceled'.")

    def test_404_clears_cancel_pending_flag(self):
        """If the order entered cancel_pending in a prior tick, the
        404 routing must clear it on pop (no leftover state).

        Round-3 critique P0-1: assert the V3 helper's distinctive
        cancel_reason substring 'kalshi_404' so this test fails on
        unfixed code (existing else-branch passes reason=reason verbatim,
        not prefixed with 'kalshi_404')."""
        e = _make_executor()
        order = _make_active_order(asset="ETH")
        order["cancel_pending"] = True
        e._active_orders["ETH"] = order
        e._client.cancel_order = MagicMock(
            return_value={"_error": True, "_status_code": 404})

        e._cancel_order("ETH", "test_clears_pending")

        self.assertNotIn("ETH", e._active_orders)
        # Verify V3-specific path ran: cancel_reason has 'kalshi_404'.
        e._log_fill_model_sample.assert_called_once()
        _, kwargs = e._log_fill_model_sample.call_args
        cancel_reason = kwargs.get("cancel_reason", "")
        self.assertIn(
            "kalshi_404", cancel_reason,
            "V3 helper must tag cancel_reason with 'kalshi_404' "
            "prefix to distinguish from operator-initiated cancels "
            "in fill-model journal.")


# ─── Invariant F/G: Defensive verify before pop ──────────────────────────

class TestCancel404DefensiveVerify(unittest.TestCase):
    """The ORIGINAL conservative branch existed for a reason: prevent
    double-positions when the bot's local order_id might be stale or
    wrong. The 404 fix must preserve that defense — verify the order
    is truly gone before popping."""

    def test_404_holds_when_get_orders_shows_order_still_resting(self):
        """Wrong order_id, permissions, or other 404 sources may not
        actually mean 'order is gone'. If get_orders returns the same
        order_id as resting, hold conservatively."""
        e = _make_executor()
        e._active_orders["ETH"] = _make_active_order(
            asset="ETH", order_id="uuid-suspect")
        e._client.cancel_order = MagicMock(
            return_value={"_error": True, "_status_code": 404})
        # Defense scenario: cancel got 404 but order is still in
        # /orders as resting → something is wrong; do NOT pop.
        e._client.get_orders = MagicMock(return_value={
            "orders": [{
                "order_id": "uuid-suspect",
                "ticker": "KXETH15M-26MAY032200-00",
                "status": "resting",
            }]
        })

        ok = e._cancel_order("ETH", "test_defensive_hold")

        self.assertFalse(
            ok,
            "Order still appears resting on Kalshi — must hold "
            "conservatively to prevent double-position on caller "
            "submitting a replacement.")
        self.assertIn(
            "ETH", e._active_orders,
            "Asset must NOT be popped when verify shows the order "
            "still alive.")
        self.assertTrue(
            e._active_orders["ETH"].get("cancel_pending"),
            "Should fall back to cancel_pending state for retry.")

    def test_404_pops_when_get_orders_raises_exception(self):
        """get_orders raised — exception path. Fall through to pop with
        a warning.

        Round-3 P0-1: assert get_orders was actually called. Without
        this, the test passes on unfixed code (which never calls
        get_orders)."""
        e = _make_executor()
        e._active_orders["ETH"] = _make_active_order(asset="ETH")
        e._client.cancel_order = MagicMock(
            return_value={"_error": True, "_status_code": 404})
        e._client.get_orders = MagicMock(
            side_effect=Exception("network down"))

        ok = e._cancel_order("ETH", "test_get_orders_fail_open")

        self.assertTrue(ok)
        self.assertNotIn("ETH", e._active_orders)
        e._client.get_orders.assert_called()

    def test_404_pops_when_get_orders_returns_none_breaker_open(self):
        """Real breakers return None (not raise) when OPEN. The
        breaker-open path must also fall through to pop AND emit a
        WARNING (round-2 critique #1: operators must see verify
        bypass). Round-3 P0-1: assert get_orders was called AND the
        warning substring is in the captured logs."""
        e = _make_executor()
        e._active_orders["ETH"] = _make_active_order(asset="ETH")
        e._client.cancel_order = MagicMock(
            return_value={"_error": True, "_status_code": 404})
        e._client.get_orders = MagicMock(return_value=None)

        with self.assertLogs(level="WARNING") as cm:
            ok = e._cancel_order("ETH", "test_breaker_open_path")

        self.assertTrue(ok)
        self.assertNotIn("ETH", e._active_orders)
        e._client.get_orders.assert_called()
        # Round-2 critique #1 assertion: emitted warning visible to
        # operator on the breaker-open path.
        self.assertTrue(
            any("cancel_404_verify_unavailable" in msg
                for msg in cm.output),
            f"V3 helper must emit 'cancel_404_verify_unavailable' "
            f"warning when get_orders returns None (breaker open). "
            f"Captured: {cm.output}")


# ─── Partial-fill preservation in 404 path ───────────────────────────────

class TestCancel404PartialFillLabeling(unittest.TestCase):
    """If an order partially filled before Kalshi 404'd the cancel,
    label preservation matters: status='partial_canceled' (not 'expired')
    and order_outcome='partial_fill' (not 'expired'). Round-2 review
    flagged this — `_handle_cancel_404` was overwriting partial-fill
    information with the unconditional 'expired' label."""

    def test_404_with_partial_fill_marks_partial_canceled(self):
        e = _make_executor()
        order = _make_active_order(
            asset="ETH", order_id="uuid-partial", count=30)
        order["filled_so_far"] = 10  # Partial fill before 404
        e._active_orders["ETH"] = order
        e._client.cancel_order = MagicMock(
            return_value={"_error": True, "_status_code": 404})

        e._cancel_order("ETH", "test_partial_fill")

        e._state.mark_order_status.assert_called_once_with(
            "uuid-partial", "partial_canceled")
        # Round-3 P0-1: tighten so this fails on unfixed code.
        # Existing else branch also writes "partial_canceled" so the
        # mark_order_status assertion alone passes vacuously. Verify
        # the V3 helper actually ran.
        e._client.get_orders.assert_called()

    def test_404_with_partial_fill_logs_outcome_partial_fill(self):
        e = _make_executor()
        order = _make_active_order(
            asset="ETH", order_id="uuid-partial", count=30)
        order["filled_so_far"] = 10
        e._active_orders["ETH"] = order
        e._client.cancel_order = MagicMock(
            return_value={"_error": True, "_status_code": 404})

        e._cancel_order("ETH", "test_partial_outcome")

        e._state.update_evaluated_opportunity_order.assert_called_once()
        _, kwargs = e._state.update_evaluated_opportunity_order.call_args
        self.assertEqual(
            kwargs.get("order_outcome"), "partial_fill",
            "Partial-fill 404s must preserve partial_fill outcome — "
            "kalshi_fill_simulator.py treats partial_canceled as "
            "label=1 in fill-model training.")
        # Round-3 P0-1: ensure V3 helper ran (existing else branch
        # also passes this assertion vacuously without verify).
        e._client.get_orders.assert_called()


# ─── Force-pop must also emit fill-model sample ──────────────────────────

class TestForcePopWritesFillModelSample(unittest.TestCase):
    """Round-2 critique #2: force-pop writes mark_order_status and
    update_evaluated_opportunity_order, but NOT _log_fill_model_sample.
    The journal entries dropped on this path are precisely the
    long-tail data the fill model needs (orders that hit the backstop
    are by definition unusual)."""

    def test_force_pop_emits_fill_model_sample_as_expired(self):
        e = _make_executor()
        order = _make_active_order(asset="ETH", seconds_to_close=10)
        order["submit_time"] = time.time() - 100  # remaining = -90s
        e._active_orders["ETH"] = order
        e._client.cancel_order = MagicMock(return_value=None)

        e._tick_one(order, "ETH", ws_fills=[])

        e._log_fill_model_sample.assert_called()
        # The first call after backstop fires should be expired-labeled.
        args, kwargs = e._log_fill_model_sample.call_args_list[0]
        outcome = args[1] if len(args) > 1 else kwargs.get("outcome")
        self.assertEqual(
            outcome, "expired",
            "Force-pop must call _log_fill_model_sample so the backstop "
            "doesn't silently drop fill-model training data.")


# ─── Invariant I: None still keeps order (preserve conservative branch) ──

class TestCancelNoneKeepsActiveOrders(unittest.TestCase):
    """Plain None (transient error) must preserve the conservative
    branch — order may still be live, do not pop."""

    def test_none_keeps_asset_in_active_orders(self):
        e = _make_executor()
        e._active_orders["BTC"] = _make_active_order(asset="BTC")
        e._client.cancel_order = MagicMock(return_value=None)

        ok = e._cancel_order("BTC", "test_transient")

        self.assertFalse(ok)
        self.assertIn("BTC", e._active_orders)
        self.assertTrue(
            e._active_orders["BTC"].get("cancel_pending"),
            "Transient failure must mark cancel_pending so "
            "reconciliation retries.")


# ─── Invariant C: Reconciliation block also routes 404 → pop ─────────────

class TestReconciliation404Pops(unittest.TestCase):
    """The cancel_pending reconciliation block (line 20484) must also
    treat 404 as terminal. This test mocks at session.request so it
    actually pins the _request fix end-to-end (not just the caller
    contract)."""

    def setUp(self):
        # Round-2 critique #6: clear global breaker state to prevent
        # cross-test pollution (these tests touch real _request +
        # @_kalshi_breaker decorators).
        _reset_breaker_registry()

    def test_reconciliation_404_via_real_request_path(self):
        e = _make_executor()
        order = _make_active_order(asset="ETH")
        order["cancel_pending"] = True
        e._active_orders["ETH"] = order

        # Real KalshiClient with mocked session — exercises _request.
        c = _make_client_with_mocked_session()
        c.session.request = MagicMock(
            return_value=_http_response(
                404, '{"error":{"code":"not_found"}}'))
        e._client = c
        # get_orders also goes through _request — return empty.
        # We need session.request to handle multiple paths; route by URL.
        def session_request(method, url, **_):
            if "/portfolio/orders" in url and method == "GET":
                return _http_response(200, '{"orders":[]}')
            if method == "DELETE":
                return _http_response(
                    404, '{"error":{"code":"not_found"}}')
            return _http_response(200)
        c.session.request = MagicMock(side_effect=session_request)

        e._tick_one(order, "ETH", ws_fills=[])

        self.assertNotIn(
            "ETH", e._active_orders,
            "Reconciliation must pop on 404 — the order has expired "
            "on Kalshi. Without this, the asset is locked out until "
            "restart (the original May 4 incident).")


# ─── Option 3: force-pop backstop after window close ─────────────────────

class TestForcePopBackstopAfterWindowClose(unittest.TestCase):
    """If an order is still in _active_orders >60s after its window
    closed, something has gone wrong (any future 404-variant the
    targeted fix doesn't anticipate). Force-pop with WARNING."""

    def test_force_pop_does_not_fire_at_remaining_minus_30s(self):
        """Round-4 critique #6: pin the threshold. If the constant
        gets refactored to -30 or the comparison flips, backstop
        marks status='expired' which this test catches.

        Round-5 critique C3: explicit precondition — this test pins
        behavior assuming MIN_SECONDS_BEFORE_CLOSE=0. If the constant
        changes, the test must be re-derived (otherwise it silently
        no-ops because close_approaching path also stops firing)."""
        # Hard-pin the precondition. If MIN_SECONDS_BEFORE_CLOSE
        # changes, this fails fast — the test author must re-derive
        # the boundary. Yesterday's lesson: silent test-infra
        # failures are exactly what causes prod regressions.
        self.assertEqual(
            bot.MIN_SECONDS_BEFORE_CLOSE, 0,
            "test_force_pop_does_not_fire_at_remaining_minus_30s "
            "assumes MIN_SECONDS_BEFORE_CLOSE=0. The constant changed "
            "— re-derive the -30s boundary or rewrite the test.")

        e = _make_executor()
        order = _make_active_order(asset="ETH", seconds_to_close=10)
        order["submit_time"] = time.time() - 40  # remaining = -30
        e._active_orders["ETH"] = order
        e._client.cancel_order = MagicMock(return_value={"ok": True})

        e._tick_one(order, "ETH", ws_fills=[])

        # At remaining=-30, the existing close_approaching path may
        # pop with status="canceled". The V3 backstop must NOT fire
        # (it's gated to remaining<-60). Differentiator: backstop →
        # "expired", close_approaching → "canceled".
        for call in e._state.mark_order_status.call_args_list:
            args, _ = call
            self.assertNotEqual(
                args[1], "expired",
                "At remaining=-30s the V3 backstop must NOT fire. "
                "If status='expired', the -60s threshold has been "
                "crossed too early.")

    def test_force_pop_when_cancel_broken_and_window_closed(self):
        """Pin the backstop specifically: cancel API is broken (returns
        None — transient error) AND window has been closed 90s. Without
        the backstop, the asset stays locked. With it, force-pop fires.

        This is the future-proofing branch — if Kalshi ever returns a
        404-variant the targeted fix doesn't anticipate (5xx that
        actually means expired, 409, 200-with-empty-body), this still
        catches it."""
        e = _make_executor()
        order = _make_active_order(asset="ETH", seconds_to_close=10)
        # submit_time 100s ago with 10s window → remaining = -90s
        order["submit_time"] = time.time() - 100
        e._active_orders["ETH"] = order
        # Cancel API broken — exact scenario where backstop is needed.
        e._client.cancel_order = MagicMock(return_value=None)

        e._tick_one(order, "ETH", ws_fills=[])

        self.assertNotIn(
            "ETH", e._active_orders,
            "Backstop: window closed >60s + cancel broken → force-pop "
            "to prevent indefinite lockout. This is the future-proofing "
            "branch the postmortem recommended alongside the targeted fix.")


# ─── End-to-end: full state machine regression ───────────────────────────

class TestEndToEnd404LoopRegression(unittest.TestCase):
    """Pin the actual May 4 incident: simulate cancel returning 404
    repeatedly via the real _request code path. Asset must be popped
    within 1-2 ticks."""

    def setUp(self):
        # Round-2 critique #6: see TestReconciliation404Pops.setUp
        _reset_breaker_registry()

    def test_404_storm_pops_asset_within_two_ticks(self):
        e = _make_executor()
        order = _make_active_order(asset="ETH", seconds_to_close=600)
        e._active_orders["ETH"] = order

        c = _make_client_with_mocked_session()
        def session_request(method, url, **_):
            if "/portfolio/orders" in url and method == "GET":
                return _http_response(200, '{"orders":[]}')
            if method == "DELETE":
                return _http_response(
                    404, '{"error":{"code":"not_found"}}')
            return _http_response(200)
        c.session.request = MagicMock(side_effect=session_request)
        e._client = c

        # Tick 1: triggers cancel via close-approaching or some path.
        # In production the trigger varies; here we directly call
        # _cancel_order to model the worst case.
        e._cancel_order("ETH", "e2e_test")

        self.assertNotIn(
            "ETH", e._active_orders,
            "End-to-end: the actual May 4 incident must be impossible "
            "after V2 fix — 404 from cancel pops asset, no infinite "
            "retry, no asset lockout.")


# ─── Audit-journal coverage (round-5 critique C5) ────────────────────────

class TestCancel404WritesOrderJournal(unittest.TestCase):
    """The existing _cancel_order success branch writes to
    ORDER_JOURNAL via `self._logger.log_order({"action":
    "maker_canceled", ...})`. The V3/V4 helper must do the same or
    operator/audit scripts greppping ORDER_JOURNAL for
    "action":"maker_canceled" silently miss every 404-routed cancel.

    Round-5 critique C5: tests must catch operator-pastes-incomplete
    of the helper. Yesterday's lesson: tests that pass on under-
    implementation are how Shape D shipped."""

    def test_cancel_404_writes_maker_canceled_to_order_journal(self):
        e = _make_executor()
        e._active_orders["ETH"] = _make_active_order(
            asset="ETH", order_id="uuid-eth")
        e._client.cancel_order = MagicMock(
            return_value={"_error": True, "_status_code": 404})

        e._cancel_order("ETH", "close_approaching")

        e._logger.log_order.assert_called_once()
        args, _ = e._logger.log_order.call_args
        entry = args[0]
        self.assertEqual(
            entry.get("action"), "maker_canceled",
            "ORDER_JOURNAL action must be 'maker_canceled' so the "
            "existing audit/grep pattern continues to work.")
        self.assertEqual(entry.get("order_id"), "uuid-eth")
        self.assertTrue(
            entry.get("reason", "").startswith("kalshi_404_"),
            f"reason must be tagged kalshi_404_*, got {entry.get('reason')!r}")

    def test_force_pop_writes_maker_canceled_to_order_journal(self):
        e = _make_executor()
        order = _make_active_order(
            asset="ETH", order_id="uuid-eth", seconds_to_close=10)
        order["submit_time"] = time.time() - 100
        e._active_orders["ETH"] = order
        e._client.cancel_order = MagicMock(return_value=None)

        e._tick_one(order, "ETH", ws_fills=[])

        e._logger.log_order.assert_called()
        backstop_calls = [
            c for c in e._logger.log_order.call_args_list
            if "force_pop_after_close" in (c[0][0].get("reason", ""))
        ]
        self.assertTrue(
            len(backstop_calls) >= 1,
            "Force-pop backstop must write a maker_canceled entry to "
            "ORDER_JOURNAL with reason='force_pop_after_close*'.")
        # Round-6 critique C3: also assert action='maker_canceled'.
        # Without this, an operator pasting `"action": "force_pop"`
        # would still pass the reason-substring check; but
        # ORDER_JOURNAL audit greppers look for action label.
        backstop_entry = backstop_calls[0][0][0]
        self.assertEqual(
            backstop_entry.get("action"), "maker_canceled",
            f"Force-pop ORDER_JOURNAL action must be 'maker_canceled' "
            f"to match existing audit-grep pattern. Got: "
            f"{backstop_entry.get('action')!r}")


# ─── Round-7 C3: unknown source must downgrade to "unknown", not crash ───

class TestCancel404UnknownSourceDowngrade(unittest.TestCase):
    """Round-6 C1 fix: bad `source` strings (caller bug, future
    refactor) must NOT raise an AssertionError because the
    reconciliation block's broad `except Exception` would swallow it
    and re-create the asset lockout. The V6 helper logs and
    downgrades to "unknown" so the pop still happens.

    Round-7 C3: this defensive branch must be pinned by a test or it
    silently regresses if a future cleanup restores the assertion."""

    def test_helper_with_bad_source_still_pops_and_logs(self):
        e = _make_executor()
        order = _make_active_order(asset="ETH", order_id="uuid-eth")
        e._active_orders["ETH"] = order

        # Round-8 C3: helpful failure message if helper missing.
        if not hasattr(e, "_handle_cancel_404"):
            self.fail(
                "OrderExecutor._handle_cancel_404 missing — V6 round-6 "
                "C1 unknown-source downgrade unimplemented. See "
                "kb/decisions/cancel-404-fix-v2-design-may04.md Site 2.")

        with self.assertLogs(level="ERROR") as cm:
            ok = e._handle_cancel_404(
                order, "ETH",
                reason="test_reason",
                source="bogus_caller_typo")

        self.assertTrue(ok, "Bad source must NOT block the pop.")
        self.assertNotIn("ETH", e._active_orders)
        self.assertTrue(
            any("unknown source" in r.getMessage()
                or "bogus_caller_typo" in r.getMessage()
                for r in cm.records),
            f"Bad source must emit an ERROR log naming the bad value. "
            f"Got: {[r.getMessage() for r in cm.records]}")
        # Verify cancel_reason carries 'unknown' downgrade
        e._log_fill_model_sample.assert_called_once()
        _, kwargs = e._log_fill_model_sample.call_args
        self.assertIn(
            "kalshi_404_unknown_", kwargs.get("cancel_reason", ""),
            "cancel_reason must reflect the downgrade so downstream "
            "fill-model training can filter on it.")


# ─── Round-6 C5: pop must succeed even if audit writes raise ─────────────

class TestCancel404PopFirstOrdering(unittest.TestCase):
    """Round-6 critique C5: the pop is the whole point of the fix.
    If audit writes (mark_order_status, log_order, fill_model_sample)
    raise, the asset must STILL be popped — otherwise we re-create
    the May 4 lockout in a new shape (audit-write failure resurrects
    the bug). Yesterday's lesson: defensive code adding error surface
    area must never block the primary action."""

    def test_pop_completes_even_if_mark_order_status_raises(self):
        e = _make_executor()
        e._active_orders["ETH"] = _make_active_order(
            asset="ETH", order_id="uuid-eth")
        e._client.cancel_order = MagicMock(
            return_value={"_error": True, "_status_code": 404})
        e._state.mark_order_status = MagicMock(
            side_effect=Exception("sqlite locked"))

        # Round-7 critique C2: wrap to surface helpful assertion msg
        # rather than raw exception when V6 regression is present.
        ok = False
        try:
            ok = e._cancel_order(
                "ETH", "test_pop_resilient_to_audit_fail")
        except Exception:
            pass

        self.assertTrue(
            ok,
            "Helper raised instead of popping — V3-V5 audit-before-pop "
            "ordering regression. Pop must happen BEFORE audit writes "
            "so audit failures don't resurrect the lockout. See "
            "kb/failures/cancel-404-asset-lockout-may04.md.")
        self.assertNotIn(
            "ETH", e._active_orders,
            "POP MUST COMPLETE despite mark_order_status failure.")

    def test_pop_completes_even_if_log_order_raises(self):
        e = _make_executor()
        e._active_orders["BTC"] = _make_active_order(
            asset="BTC", order_id="uuid-btc")
        e._client.cancel_order = MagicMock(
            return_value={"_error": True, "_status_code": 404})
        e._logger.log_order = MagicMock(
            side_effect=OSError("disk full"))

        ok = False
        try:
            ok = e._cancel_order(
                "BTC", "test_pop_resilient_to_journal_fail")
        except Exception:
            pass

        self.assertTrue(
            ok,
            "Helper raised instead of popping — pop must happen "
            "BEFORE log_order audit write.")
        self.assertNotIn("BTC", e._active_orders)


# ─── Round-5 C4 lockdown: NO error log on DELETE 404 ─────────────────────

class TestDelete404DoesNotEmitErrorLog(unittest.TestCase):
    """Round-5 critique C4: pin that the V4 fix's sentinel return
    fires BEFORE the existing `if status_code >= 400: logging.error`
    block (line 2477-2478 of bot.py). Reviewer was concerned the
    early-return placement might be wrong; this test locks in the
    correct placement so a future refactor doesn't re-introduce the
    spam."""

    def setUp(self):
        _reset_breaker_registry()

    def test_delete_404_emits_no_api_error_log_line(self):
        c = _make_client_with_mocked_session()
        c.session.request = MagicMock(
            return_value=_http_response(
                404, '{"error":{"code":"not_found"}}'))

        # Capture ERROR-level logs only.
        with self.assertLogs(level="ERROR") as cm:
            c._request("DELETE", "/trade-api/v2/portfolio/orders/x")
            # assertLogs requires at least one record — emit a dummy
            # so context manager doesn't fail when no real logs.
            import logging as _logging
            _logging.error("dummy_anchor_for_assertlogs")

        # Round-6 critique C2: assert NO ERROR records originate from
        # the _request function for DELETE 404. Substring on the
        # message is fragile (a future refactor could change the
        # format and accidentally pass this test even with a
        # half-broken fix). Pin via record.funcName instead.
        request_errors = [
            r for r in cm.records
            if r.funcName == "_request"
        ]
        self.assertEqual(
            len(request_errors), 0,
            f"V4+ fix must short-circuit DELETE 404 BEFORE any "
            f"ERROR-log emission inside _request. Found "
            f"{len(request_errors)} unexpected ERROR records from "
            f"_request: {[r.getMessage() for r in request_errors]}")


# ─── Meta: test infrastructure itself ────────────────────────────────────

class TestResetBreakerRegistryActuallyResets(unittest.TestCase):
    """Round-4 critique #4: pin that _reset_breaker_registry is not a
    silent no-op. If circuit_breaker.REGISTRY internal name drifts,
    this fails immediately rather than letting cross-test pollution
    silently return."""

    def test_reset_clears_breaker_state(self):
        # Plant a breaker in the registry.
        registry = bot._BREAKER_REGISTRY
        registry.get("test_meta_breaker", failures_to_open=3,
                     recovery_seconds=10)
        self.assertIn("test_meta_breaker", registry._breakers)

        _reset_breaker_registry()

        self.assertNotIn(
            "test_meta_breaker", registry._breakers,
            "_reset_breaker_registry must actually clear state. If "
            "this fails, the setUp pattern in classes that exercise "
            "real _request is a no-op and round-2 critique #6 "
            "(cross-test breaker pollution) is unfixed.")


if __name__ == "__main__":
    unittest.main()
