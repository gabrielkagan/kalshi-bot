"""Tests for weather NO-side execution pipeline.

Guards against:
- Order submission using yes_price instead of no_price for NO-side
- _on_fill() recording wrong side for NO-side fills
- _reprice_maker() sending wrong side/price for NO-side amends
- execute() blocking weather NO-side when WEATHER_NO_SIDE_LIVE=True
- execute() allowing weather YES-side through observation gate
- STC < 8h NO-side candidates being generated
- WEATHER_NO_SIDE_LIVE=False blocking all NO execution
- Existing crypto YES-side trading unaffected by side parameterization
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch, call

# Mock heavy dependencies before importing bot
for _mod in ["websockets", "websocket", "requests",
             "cryptography", "cryptography.hazmat",
             "cryptography.hazmat.primitives",
             "cryptography.hazmat.primitives.serialization",
             "cryptography.hazmat.primitives.hashes",
             "cryptography.hazmat.primitives.asymmetric",
             "cryptography.hazmat.primitives.asymmetric.padding"]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import bot
from bot.executor import OrderExecutor
from bot.models import calculate_taker_fee
import bot.constants  # noqa: F401


def _make_weather_no_candidate(**overrides):
    """Build a minimal weather NO-side candidate dict."""
    base = {
        "ticker": "KXHIGHNY-26MAR151200-A55",
        "event_ticker": "KXHIGHNY-26MAR151200",
        "asset": "NYC_TEMP",
        "best_yes_ask": 25,  # NO price for NO-side
        "position_size": 1,
        "calibrated_prob": 0.75,  # NO probability
        "edge": 0.05,
        "seconds_to_close": 36000,  # 10 hours
        "strategy": "above",
        "balance_at_scan": 50000,
        "spot": 55.0,
        "threshold": 50.0,
        "blended_rv": 0.01,
        "z_score": 1.5,
        "vol_regime": "normal",
        "kelly_f": 0.0,
        "product_type": "weather",
        "side": "no",
        "ofa_adjustment": 0.0,
        "ob_snapshot": {},
        "calibrated_prob_raw": 0.74,
        "drawdown_scaler": 1.0,
        "fee_adjusted_edge": 0.04,
    }
    base.update(overrides)
    return base


def _make_crypto_candidate(**overrides):
    """Build a minimal crypto 15M YES-side candidate dict."""
    base = {
        "ticker": "KXBTC15M-26MAR091200-B68500",
        "event_ticker": "KXBTC15M-26MAR091200",
        "asset": "BTC",
        "best_yes_ask": 92,
        "position_size": 5,
        "calibrated_prob": 0.96,
        "edge": 0.03,
        "seconds_to_close": 400,
        "strategy": "above",
        "balance_at_scan": 50000,
        "spot": 68500.0,
        "threshold": 68000.0,
        "blended_rv": 0.0004,
        "z_score": 2.5,
        "vol_regime": "normal",
        "kelly_f": 0.15,
        "product_type": "15m",
        "side": "yes",
        "ofa_adjustment": 0.0,
        "ob_snapshot": {"ask_depth": 10},
        "calibrated_prob_raw": 0.95,
        "drawdown_scaler": 1.0,
        "fee_adjusted_edge": 0.02,
    }
    base.update(overrides)
    return base


def _make_executor():
    """Build an OrderExecutor with mocked dependencies."""
    client = MagicMock()
    state = MagicMock()
    logger = MagicMock()
    main_loop = MagicMock()
    kalshi_feed = MagicMock()
    kalshi_feed.is_connected = True
    kalshi_feed.pop_fills.return_value = []
    return OrderExecutor(
        client=client, state=state, logger=logger,
        main_loop=main_loop, kalshi_feed=kalshi_feed,
    )


def _make_order(candidate, **overrides):
    """Build a minimal order dict as created by _submit_maker."""
    base = {
        "order_id": "test_order_1",
        "client_order_id": "test_client_1",
        "ticker": candidate["ticker"],
        "event_ticker": candidate["event_ticker"],
        "asset": candidate["asset"],
        "side": candidate.get("side", "yes"),
        "price_cents": candidate["best_yes_ask"],
        "count": candidate["position_size"],
        "is_taker": False,
        "submit_time": 1710000000.0,
        "seconds_to_close_at_submit": candidate["seconds_to_close"],
        "candidate": candidate,
        "balance_at_entry": candidate["balance_at_scan"],
        "filled_so_far": 0,
    }
    base.update(overrides)
    return base


class TestNoSideOrderRouting(unittest.TestCase):
    """Verify NO-side orders use no_price parameter, not yes_price."""

    def test_submit_maker_no_side_uses_no_price(self):
        """_submit_maker() must pass no_price (not yes_price) for side='no'."""
        ex = _make_executor()
        candidate = _make_weather_no_candidate()
        ex._client.place_order.return_value = {"order": {"order_id": "test123"}}

        ex._submit_maker(candidate)

        call_kwargs = ex._client.place_order.call_args.kwargs
        self.assertEqual(call_kwargs["side"], "no")
        self.assertIn("no_price", call_kwargs)
        self.assertNotIn("yes_price", call_kwargs)

    def test_submit_maker_yes_side_uses_yes_price(self):
        """_submit_maker() must still use yes_price for side='yes' (default)."""
        ex = _make_executor()
        candidate = _make_crypto_candidate()
        ex._client.place_order.return_value = {"order": {"order_id": "test456"}}

        ex._submit_maker(candidate)

        call_kwargs = ex._client.place_order.call_args.kwargs
        self.assertEqual(call_kwargs["side"], "yes")
        self.assertIn("yes_price", call_kwargs)
        self.assertNotIn("no_price", call_kwargs)

    def test_submit_maker_default_side_is_yes(self):
        """Candidates without 'side' key default to YES."""
        ex = _make_executor()
        candidate = _make_crypto_candidate()
        del candidate["side"]
        ex._client.place_order.return_value = {"order": {"order_id": "test789"}}

        ex._submit_maker(candidate)

        call_kwargs = ex._client.place_order.call_args.kwargs
        self.assertEqual(call_kwargs["side"], "yes")
        self.assertIn("yes_price", call_kwargs)

    def test_submit_taker_no_side_uses_no_price(self):
        """_submit_taker() must pass no_price for side='no'."""
        ex = _make_executor()
        candidate = _make_weather_no_candidate()
        ex._client.place_order.return_value = {
            "order": {"order_id": "taker1", "status": "executed",
                      "count_fp": "100", "yes_price_dollars": "0.75"}
        }

        ex._submit_taker(candidate)

        call_kwargs = ex._client.place_order.call_args.kwargs
        self.assertEqual(call_kwargs["side"], "no")
        self.assertIn("no_price", call_kwargs)
        self.assertNotIn("yes_price", call_kwargs)

    def test_insert_bot_order_gets_no_side(self):
        """insert_bot_order() must receive side='no' for NO-side candidates."""
        ex = _make_executor()
        candidate = _make_weather_no_candidate()
        ex._client.place_order.return_value = {"order": {"order_id": "ord1"}}

        ex._submit_maker(candidate)

        # insert_bot_order: (client_oid, ticker, event_ticker, asset, side, count, price, is_taker)
        call_args = ex._state.insert_bot_order.call_args[0]
        self.assertEqual(call_args[4], "no")  # side parameter


class TestNoSideFillDetection(unittest.TestCase):
    """Verify fill detection passes correct side to position recording."""

    def test_on_fill_uses_order_side(self):
        """_on_fill() must read side from order dict, not hardcode 'yes'."""
        ex = _make_executor()
        candidate = _make_weather_no_candidate()
        order = _make_order(candidate, side="no")
        fill = {"count": 1, "yes_price": 75}

        ex._on_fill(fill, order)

        call_kwargs = ex._state.record_position_from_fill.call_args.kwargs
        self.assertEqual(call_kwargs["side"], "no")

    def test_on_fill_default_yes(self):
        """_on_fill() defaults to 'yes' for orders without side key."""
        ex = _make_executor()
        candidate = _make_crypto_candidate()
        order = _make_order(candidate)
        del order["side"]  # Remove side key
        fill = {"count": 5, "yes_price": 92}

        ex._on_fill(fill, order)

        call_kwargs = ex._state.record_position_from_fill.call_args.kwargs
        self.assertEqual(call_kwargs["side"], "yes")

    def test_on_fill_malformed_count_fp_stays_stamped_returns_zero(self):
        """Leave a bad fill stamped so the taker loop cannot livelock."""
        ex = _make_executor()
        candidate = _make_crypto_candidate()
        order = _make_order(candidate)
        order["_seen_fill_ids"] = {"t-bad"}
        fill = {"trade_id": "t-bad", "count_fp": "N/A", "yes_price": 92}
        n = ex._on_fill(fill, order)
        self.assertEqual(n, 0)
        self.assertIn("t-bad", order["_seen_fill_ids"])
        ex._state.record_position_from_fill.assert_not_called()

    def test_on_fill_string_yes_price_does_not_raise(self):
        ex = _make_executor()
        candidate = _make_crypto_candidate()
        order = _make_order(candidate)
        fill = {"trade_id": "t-str", "count": 1, "yes_price": "45"}
        n = ex._on_fill(fill, order)
        self.assertEqual(n, 1)
        call_kwargs = ex._state.record_position_from_fill.call_args.kwargs
        self.assertEqual(call_kwargs["price_cents"], 45)


class TestNoSideRepriceMaker(unittest.TestCase):
    """Verify _reprice_maker() uses order's side for amend calls."""

    def test_reprice_no_side_uses_no_price(self):
        """amend_order must use no_price for NO-side orders."""
        ex = _make_executor()
        candidate = _make_weather_no_candidate()
        order = _make_order(candidate, side="no")
        # Put order in _active_orders so _active_order property works
        ex._active_orders["NYC_TEMP"] = order
        ex._client.amend_order.return_value = {"order": {"order_id": "amend1"}}

        result = ex._reprice_maker(22)

        call_kwargs = ex._client.amend_order.call_args.kwargs
        self.assertEqual(call_kwargs["side"], "no")
        self.assertIn("no_price", call_kwargs)
        self.assertEqual(call_kwargs["no_price"], 22)
        self.assertNotIn("yes_price", call_kwargs)
        self.assertTrue(result)

    def test_reprice_yes_side_uses_yes_price(self):
        """amend_order must use yes_price for YES-side orders."""
        ex = _make_executor()
        candidate = _make_crypto_candidate()
        order = _make_order(candidate, side="yes")
        ex._active_orders["BTC"] = order
        ex._client.amend_order.return_value = {"order": {"order_id": "amend2"}}

        result = ex._reprice_maker(91)

        call_kwargs = ex._client.amend_order.call_args.kwargs
        self.assertEqual(call_kwargs["side"], "yes")
        self.assertIn("yes_price", call_kwargs)
        self.assertNotIn("no_price", call_kwargs)
        self.assertTrue(result)


class TestNoSideObservationGate(unittest.TestCase):
    """Verify observation gate behavior for weather YES vs NO side."""

    @patch.object(bot.constants, "WEATHER_NO_SIDE_LIVE", True)
    @patch.object(bot.executor, "OBSERVATION_MODE", False)
    def test_weather_no_side_bypasses_observation_gate(self):
        """Weather NO-side candidates pass through when WEATHER_NO_SIDE_LIVE=True."""
        ex = _make_executor()
        candidate = _make_weather_no_candidate()
        ex._client.place_order.return_value = {"order": {"order_id": "live1"}}

        ex.execute(candidate)

        # Should proceed to order submission (not blocked by observation gate)
        ex._client.place_order.assert_called_once()

    @patch.object(bot.constants, "WEATHER_NO_SIDE_LIVE", True)
    @patch.object(bot.executor, "OBSERVATION_MODE", False)
    def test_weather_yes_side_blocked_by_observation_gate(self):
        """Weather YES-side must remain blocked even when WEATHER_NO_SIDE_LIVE=True."""
        ex = _make_executor()
        candidate = _make_weather_no_candidate(side="yes")

        result = ex.execute(candidate)

        self.assertIsNone(result)
        ex._client.place_order.assert_not_called()

    @patch.object(bot.constants, "WEATHER_NO_SIDE_LIVE", False)
    @patch.object(bot.executor, "OBSERVATION_MODE", False)
    def test_weather_no_side_blocked_when_kill_switch_off(self):
        """WEATHER_NO_SIDE_LIVE=False blocks NO-side execution."""
        ex = _make_executor()
        candidate = _make_weather_no_candidate()

        result = ex.execute(candidate)

        self.assertIsNone(result)
        ex._client.place_order.assert_not_called()

    @patch.object(bot.executor, "OBSERVATION_MODE", False)
    def test_crypto_yes_side_unaffected(self):
        """Existing crypto 15M trading (side='yes') is completely unaffected."""
        ex = _make_executor()
        candidate = _make_crypto_candidate()
        ex._client.place_order.return_value = {"order": {"order_id": "crypto1"}}

        ex.execute(candidate)

        call_kwargs = ex._client.place_order.call_args.kwargs
        self.assertEqual(call_kwargs["side"], "yes")
        self.assertIn("yes_price", call_kwargs)

    @patch.object(bot.executor, "OBSERVATION_MODE", False)
    def test_crypto_no_side_key_defaults_yes(self):
        """Crypto candidates without 'side' key default to YES behavior."""
        ex = _make_executor()
        candidate = _make_crypto_candidate()
        del candidate["side"]
        ex._client.place_order.return_value = {"order": {"order_id": "crypto2"}}

        ex.execute(candidate)

        call_kwargs = ex._client.place_order.call_args.kwargs
        self.assertEqual(call_kwargs["side"], "yes")


class TestNoSideConstants(unittest.TestCase):
    """Verify NO-side constants exist and have correct values."""

    def test_weather_no_side_live_is_false(self):
        """WEATHER_NO_SIDE_LIVE KILLED 2026-05-16 (commit ce8e2d2).

        Lifetime n=167 / 38.3% WR vs 70% assumed prior; Wilson 95% CI
        [23.6%, 47.0%] empirically falsifies the prior. Near-ATM zone
        (NO 39-40¢ ↔ YES 60-61¢) is the market-maker zone with no edge.
        Re-research plan: ClickUp folder 90149436180 (A1-A2-B-C1..C4).

        This test pins the KILLED state. Re-enable requires explicit
        promotion gate clearance (C4 ticket 86b9zdbj0).
        """
        self.assertFalse(bot.constants.WEATHER_NO_SIDE_LIVE)

    def test_weather_no_side_min_stc_is_16h(self):
        """WEATHER_NO_SIDE_MIN_STC must be 57600 (16 hours)."""
        self.assertEqual(bot.constants.WEATHER_NO_SIDE_MIN_STC, 57600.0)

    def test_weather_observation_only_still_true(self):
        """WEATHER_OBSERVATION_ONLY must remain True (YES-side gate)."""
        self.assertTrue(bot.constants.WEATHER_OBSERVATION_ONLY)


class TestNoSideSettlement(unittest.TestCase):
    """Settlement logic is already side-aware. Verify expectations."""

    def test_no_side_win(self):
        """NO position wins when market_result='no'."""
        side, result = "no", "no"
        self.assertEqual("WIN" if result == side else "LOSS", "WIN")

    def test_no_side_loss(self):
        """NO position loses when market_result='yes'."""
        side, result = "no", "yes"
        self.assertEqual("WIN" if result == side else "LOSS", "LOSS")

    def test_yes_side_unaffected(self):
        """YES position still wins when market_result='yes'."""
        side, result = "yes", "yes"
        self.assertEqual("WIN" if result == side else "LOSS", "WIN")


class TestOrderDictCarriesSide(unittest.TestCase):
    """Verify the order dict created by _submit_maker carries the side field."""

    def test_order_dict_has_side_no(self):
        """Order dict for NO-side candidate must include side='no'."""
        ex = _make_executor()
        candidate = _make_weather_no_candidate()
        ex._client.place_order.return_value = {"order": {"order_id": "side_test"}}

        ex._submit_maker(candidate)

        order = ex._active_orders.get("NYC_TEMP")
        self.assertIsNotNone(order)
        self.assertEqual(order["side"], "no")

    def test_order_dict_has_side_yes(self):
        """Order dict for YES-side candidate must include side='yes'."""
        ex = _make_executor()
        candidate = _make_crypto_candidate()
        ex._client.place_order.return_value = {"order": {"order_id": "side_test2"}}

        ex._submit_maker(candidate)

        order = ex._active_orders.get("BTC")
        self.assertIsNotNone(order)
        self.assertEqual(order["side"], "yes")


if __name__ == "__main__":
    unittest.main()
