"""Tests for OrderExecutor (bot.py).

Guards against:
- Maker-first execution not using post_only=True
- IOC taker using wrong time_in_force string (bug: "ioc" instead of "immediate_or_cancel")
- Ghost fill false positives (IOC remaining_count=0 with fill_count=0 ≠ real fill)
- Post-only rejection tiers not escalating correctly
- Maker price offset logic (1¢ vs 2¢ vs degraded)
- Escalation wait timing (per-STC urgency tiers, BTC override)
- Direct taker path for STC < DIRECT_TAKER_THRESHOLD
- SOL taker-first override bypassing maker entirely
- Observation mode safety belt blocking execute()
- Cancel-before-taker preventing double positions
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
from collections import deque

# Mock heavy dependencies before importing bot
_MOCKED = []
for _mod in ["websockets", "websocket", "requests",
             "cryptography", "cryptography.hazmat",
             "cryptography.hazmat.primitives",
             "cryptography.hazmat.primitives.serialization",
             "cryptography.hazmat.primitives.hashes",
             "cryptography.hazmat.primitives.asymmetric",
             "cryptography.hazmat.primitives.asymmetric.padding"]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()
        _MOCKED.append(_mod)

from bot import (
    OrderExecutor,
    MAKER_PRICE_OFFSET, MAKER_POLL_INTERVAL, ESCALATION_MAX_ENTRY,
    MAKER_TIMEOUT_SECONDS, DIRECT_TAKER_THRESHOLD, MAKER_ONLY_THRESHOLD,
    SOL_TAKER_FIRST, ESCALATION_WAIT_LONG, ESCALATION_WAIT_MEDIUM,
    ESCALATION_WAIT_SHORT, BTC_ESCALATION_WAIT_OVERRIDE,
    POST_ONLY_MAX_SAME_PRICE, POST_ONLY_DEGRADED_EXTRA_OFFSET,
    POST_ONLY_REJECTION_EXPIRY, EARLY_ESCALATION_MIN_MOVE,
    MIN_ENTRY_PRICE, MAX_ENTRY_PRICE, MIN_EDGE_PCT,
    calculate_taker_fee,
)


def _make_candidate(**overrides):
    """Build a minimal candidate dict for OrderExecutor.execute()."""
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
        "ofa_adjustment": 0.0,
        "ob_snapshot": {"ask_depth": 10},
        "calibrated_prob_raw": 0.95,
        "drawdown_scaler": 1.0,
    }
    base.update(overrides)
    return base


def _make_executor(**overrides):
    """Build an OrderExecutor with mocked dependencies."""
    client = MagicMock()
    state = MagicMock()
    logger = MagicMock()
    main_loop = MagicMock()
    kalshi_feed = MagicMock()
    kalshi_feed.is_connected = True
    kalshi_feed.pop_fills.return_value = []

    executor = OrderExecutor(
        client=client,
        state=state,
        logger=logger,
        main_loop=main_loop,
        kalshi_feed=kalshi_feed,
    )
    return executor


class TestMakerFirstExecution(unittest.TestCase):
    """execute() submits maker order with post_only=True, correct price offset."""

    def test_maker_order_uses_post_only(self):
        """Maker submissions must always include post_only=True."""
        ex = _make_executor()
        ex._client.place_order.return_value = {
            "order": {"order_id": "ord-123", "status": "resting"}
        }
        candidate = _make_candidate(best_yes_ask=92, seconds_to_close=400)

        with patch("bot.OBSERVATION_MODE", False), \
             patch("bot.get_market_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(observation_only=False, min_entry_price=86)
            ex.execute(candidate)

        # Verify place_order was called with post_only=True
        call_args = ex._client.place_order.call_args
        self.assertTrue(call_args.kwargs.get("post_only", call_args[1].get("post_only") if len(call_args) > 1 else None),
                        "Maker order must use post_only=True")

    def test_maker_price_offset_high_price(self):
        """Prices >= 90c get 1¢ offset (patient maker)."""
        ex = _make_executor()
        ex._client.place_order.return_value = {
            "order": {"order_id": "ord-123"}
        }
        candidate = _make_candidate(best_yes_ask=92, seconds_to_close=400)

        with patch("bot.OBSERVATION_MODE", False), \
             patch("bot.get_market_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(observation_only=False, min_entry_price=86)
            ex.execute(candidate)

        call_args = ex._client.place_order.call_args
        submitted_price = call_args.kwargs.get("yes_price")
        # 92 - 1 = 91
        self.assertEqual(submitted_price, 91,
                         f"Price >= 90c should get 1c offset: 92 - 1 = 91, got {submitted_price}")

    def test_maker_price_offset_low_price(self):
        """Prices < 90c get 2¢ offset (patient maker)."""
        ex = _make_executor()
        ex._client.place_order.return_value = {
            "order": {"order_id": "ord-123"}
        }
        # Use SOL (default floor=80, no per-asset override) so 88-2=86 clears the floor.
        # BTC has BTC_MIN_ENTRY_PRICE=89 which would block 86.
        candidate = _make_candidate(
            best_yes_ask=88, seconds_to_close=400,
            asset="SOL", ticker="KXSOL15M-26MAR091200-S100",
        )

        with patch("bot.OBSERVATION_MODE", False), \
             patch("bot.get_market_config") as mock_cfg, \
             patch("bot.SOL_TAKER_FIRST", False):
            mock_cfg.return_value = MagicMock(observation_only=False, min_entry_price=80)
            ex.execute(candidate)

        call_args = ex._client.place_order.call_args
        submitted_price = call_args.kwargs.get("yes_price")
        # 88 - 2 = 86
        self.assertEqual(submitted_price, 86,
                         f"Price < 90c should get 2c offset: 88 - 2 = 86, got {submitted_price}")

    def test_maker_below_min_entry_skipped(self):
        """If maker price after offset < MIN_ENTRY_PRICE, skip order."""
        ex = _make_executor()
        # best_yes_ask=81, offset=2 → maker price=79 < MIN_ENTRY_PRICE=80
        candidate = _make_candidate(best_yes_ask=81, seconds_to_close=400)

        with patch("bot.OBSERVATION_MODE", False), \
             patch("bot.get_market_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(observation_only=False, min_entry_price=80)
            ex.execute(candidate)

        # place_order should NOT be called (maker price 79 < min 80)
        ex._client.place_order.assert_not_called()

    def test_maker_persists_to_db_before_api(self):
        """insert_bot_order is called before place_order (crash safety)."""
        ex = _make_executor()
        call_order = []
        ex._state.insert_bot_order.side_effect = lambda *a, **kw: call_order.append("db")
        ex._client.place_order.side_effect = lambda **kw: (call_order.append("api"),
            {"order": {"order_id": "ord-123"}})[1]
        candidate = _make_candidate(best_yes_ask=92, seconds_to_close=400)

        with patch("bot.OBSERVATION_MODE", False), \
             patch("bot.get_market_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(observation_only=False, min_entry_price=86)
            ex.execute(candidate)

        self.assertEqual(call_order, ["db", "api"],
                         "DB persist must happen before API call")

    def test_maker_order_side_is_yes(self):
        """All orders submitted as side='yes'."""
        ex = _make_executor()
        ex._client.place_order.return_value = {
            "order": {"order_id": "ord-123"}
        }
        candidate = _make_candidate(seconds_to_close=400)

        with patch("bot.OBSERVATION_MODE", False), \
             patch("bot.get_market_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(observation_only=False, min_entry_price=86)
            ex.execute(candidate)

        call_args = ex._client.place_order.call_args
        self.assertEqual(call_args.kwargs.get("side"), "yes")
        self.assertEqual(call_args.kwargs.get("action"), "buy")

    def test_active_order_blocks_same_asset(self):
        """Cannot submit two orders for the same asset."""
        ex = _make_executor()
        ex._active_orders["BTC"] = {"order_id": "existing"}
        candidate = _make_candidate(asset="BTC", seconds_to_close=400)

        with patch("bot.OBSERVATION_MODE", False), \
             patch("bot.get_market_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(observation_only=False, min_entry_price=86)
            result = ex.execute(candidate)

        self.assertIsNone(result)
        ex._client.place_order.assert_not_called()

    def test_maker_api_failure_records_rejection(self):
        """API returning None → mark api_error, increment rejection counter."""
        ex = _make_executor()
        ex._client.place_order.return_value = None  # API failure
        candidate = _make_candidate(best_yes_ask=92, seconds_to_close=400)

        with patch("bot.OBSERVATION_MODE", False), \
             patch("bot.get_market_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(observation_only=False, min_entry_price=86)
            ex.execute(candidate)

        ex._state.mark_order_status.assert_called()
        self.assertEqual(ex._session_post_only_rejections, 1)
        # Should not have an active order
        self.assertNotIn("BTC", ex._active_orders)


class TestObservationModeSafety(unittest.TestCase):
    """Observation-only configs must not reach actual order submission."""

    def test_observation_mode_blocks_execution(self):
        """Observation-only product types return None immediately."""
        ex = _make_executor()
        candidate = _make_candidate(product_type="hourly")

        with patch("bot.OBSERVATION_MODE", False), \
             patch("bot.get_market_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(observation_only=True, product_type="hourly")
            result = ex.execute(candidate)

        self.assertIsNone(result)
        ex._client.place_order.assert_not_called()

    def test_global_observation_mode_logs_only(self):
        """OBSERVATION_MODE=True logs but does not submit orders."""
        ex = _make_executor()
        candidate = _make_candidate(seconds_to_close=400)

        with patch("bot.OBSERVATION_MODE", True), \
             patch("bot.get_market_config") as mock_cfg, \
             patch("bot._TELEGRAM", None):
            mock_cfg.return_value = MagicMock(observation_only=False, min_entry_price=86)
            result = ex.execute(candidate)

        self.assertIsNone(result)
        ex._client.place_order.assert_not_called()
        # Should log to evaluated_opportunities as observation_trade
        ex._state.insert_evaluated_opportunity.assert_called()


class TestDirectTakerPath(unittest.TestCase):
    """STC < DIRECT_TAKER_THRESHOLD → direct IOC taker, skip maker."""

    def test_direct_taker_uses_ioc(self):
        """Low-STC candidates go directly to IOC taker."""
        ex = _make_executor()
        ex._client.place_order.return_value = {
            "order": {
                "order_id": "ord-taker-1",
                "remaining_count": 0,
                "fill_count": 5,
                "fill_count_fp": None,
            }
        }
        ex._client.get_fills.return_value = {"fills": []}
        ex._client.get_positions.return_value = {"market_positions": []}
        candidate = _make_candidate(
            seconds_to_close=100,  # < 180 = DIRECT_TAKER_THRESHOLD
            calibrated_prob=0.97,
            best_yes_ask=92,
            position_size=5,
        )

        with patch("bot.OBSERVATION_MODE", False), \
             patch("bot.get_market_config") as mock_cfg, \
             patch("bot.time") as mock_time, \
             patch("bot.fp_str_to_int", return_value=5):
            mock_cfg.return_value = MagicMock(observation_only=False, min_entry_price=86)
            mock_time.time.return_value = 1000.0
            mock_time.sleep = MagicMock()  # Don't actually sleep
            # Need _get_addon_best_ask to return a price for liquidity check
            ex._get_addon_best_ask = MagicMock(return_value=92)
            ex.execute(candidate)

        # Should use time_in_force="immediate_or_cancel", NOT "ioc"
        call_args = ex._client.place_order.call_args
        self.assertEqual(call_args.kwargs.get("time_in_force"), "immediate_or_cancel",
                         "IOC must use full string 'immediate_or_cancel', not 'ioc'")
        # Should NOT use post_only
        self.assertIsNone(call_args.kwargs.get("post_only"),
                          "Taker orders must not use post_only")

    def test_direct_taker_increments_counters(self):
        """Direct taker path increments session counters."""
        ex = _make_executor()
        ex._client.place_order.return_value = {
            "order": {
                "order_id": "ord-taker-2",
                "remaining_count": 5,
            }
        }
        ex._client.get_fills.return_value = {"fills": []}
        ex._client.get_positions.return_value = {"market_positions": []}
        candidate = _make_candidate(
            seconds_to_close=100,
            calibrated_prob=0.97,
            best_yes_ask=92,
            position_size=5,
        )

        with patch("bot.OBSERVATION_MODE", False), \
             patch("bot.get_market_config") as mock_cfg, \
             patch("bot.time") as mock_time, \
             patch("bot.fp_str_to_int", return_value=0):
            mock_cfg.return_value = MagicMock(observation_only=False, min_entry_price=86)
            mock_time.time.return_value = 1000.0
            mock_time.sleep = MagicMock()
            ex._get_addon_best_ask = MagicMock(return_value=92)
            ex.execute(candidate)

        self.assertEqual(ex._session_direct_taker_attempts, 1)

    def test_direct_taker_edge_check(self):
        """Direct taker skipped if net edge < MIN_EDGE_PCT after fees."""
        ex = _make_executor()
        # Low calibrated_prob → edge too low after taker fee
        candidate = _make_candidate(
            seconds_to_close=100,
            calibrated_prob=0.921,  # edge ~0.1% — below MIN_EDGE_PCT after fee
            best_yes_ask=92,
            position_size=5,
        )

        with patch("bot.OBSERVATION_MODE", False), \
             patch("bot.get_market_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(observation_only=False, min_entry_price=86)
            result = ex.execute(candidate)

        self.assertIsNone(result)
        self.assertEqual(ex._session_direct_taker_skipped, 1)


class TestSOLTakerOverride(unittest.TestCase):
    """SOL_TAKER_FIRST → SOL bypasses maker entirely."""

    def test_sol_goes_direct_taker(self):
        """SOL candidates skip maker, go IOC regardless of STC."""
        ex = _make_executor()
        ex._client.place_order.return_value = {
            "order": {
                "order_id": "ord-sol-1",
                "remaining_count": 0,
                "fill_count_fp": None,
            }
        }
        ex._client.get_fills.return_value = {"fills": []}
        ex._client.get_positions.return_value = {"market_positions": []}
        candidate = _make_candidate(
            asset="SOL",
            ticker="KXSOL15M-26MAR091200-B100",
            seconds_to_close=400,  # high STC — would normally go maker
            calibrated_prob=0.96,
            best_yes_ask=92,
            position_size=5,
        )

        with patch("bot.OBSERVATION_MODE", False), \
             patch("bot.get_market_config") as mock_cfg, \
             patch("bot.time") as mock_time, \
             patch("bot.SOL_TAKER_FIRST", True), \
             patch("bot.fp_str_to_int", return_value=5):
            mock_cfg.return_value = MagicMock(observation_only=False, min_entry_price=86)
            mock_time.time.return_value = 1000.0
            mock_time.sleep = MagicMock()
            ex._get_addon_best_ask = MagicMock(return_value=92)
            ex.execute(candidate)

        # Should use IOC, not post_only maker
        call_args = ex._client.place_order.call_args
        self.assertEqual(call_args.kwargs.get("time_in_force"), "immediate_or_cancel")

    def test_sol_with_sol_taker_first_disabled(self):
        """When SOL_TAKER_FIRST=False, SOL uses normal maker path."""
        ex = _make_executor()
        ex._client.place_order.return_value = {
            "order": {"order_id": "ord-sol-2"}
        }
        candidate = _make_candidate(
            asset="SOL",
            ticker="KXSOL15M-26MAR091200-B100",
            seconds_to_close=400,
            calibrated_prob=0.96,
            best_yes_ask=92,
        )

        with patch("bot.OBSERVATION_MODE", False), \
             patch("bot.get_market_config") as mock_cfg, \
             patch("bot.SOL_TAKER_FIRST", False):
            mock_cfg.return_value = MagicMock(observation_only=False, min_entry_price=86)
            ex.execute(candidate)

        call_args = ex._client.place_order.call_args
        self.assertTrue(call_args.kwargs.get("post_only"),
                        "SOL without taker_first should use maker (post_only=True)")


class TestIOCBehavior(unittest.TestCase):
    """IOC taker fill detection and ghost fill protection."""

    def _make_taker_executor(self):
        """Build executor and submit a taker directly via _submit_taker."""
        ex = _make_executor()
        return ex

    def test_ioc_full_fill_via_polling(self):
        """IOC with fills detected via REST polling returns order_info."""
        ex = self._make_taker_executor()
        ex._client.place_order.return_value = {
            "order": {
                "order_id": "ord-ioc-1",
                "remaining_count": 0,
                "fill_count": 5,
                "fill_count_fp": None,
            }
        }
        # First check_for_fill returns a fill, second returns None
        ex._client.get_fills.side_effect = [
            {"fills": [{"order_id": "ord-ioc-1", "trade_id": "t1", "count": 5, "price": 92}]},
            {"fills": []},
            {"fills": []},  # second pass
        ]
        candidate = _make_candidate()

        with patch("bot.time") as mock_time, \
             patch("bot.fp_str_to_int", return_value=5):
            mock_time.time.return_value = 1000.0
            mock_time.sleep = MagicMock()
            result = ex._submit_taker(candidate)

        self.assertIsNotNone(result)
        self.assertEqual(result["order_id"], "ord-ioc-1")
        self.assertTrue(result["is_taker"])

    def test_ioc_no_fill_returns_none(self):
        """IOC with no fills and remaining_count > 0 returns None."""
        ex = self._make_taker_executor()
        ex._client.place_order.return_value = {
            "order": {
                "order_id": "ord-ioc-2",
                "remaining_count": 5,
                "fill_count_fp": None,
            }
        }
        ex._client.get_fills.return_value = {"fills": []}
        ex._client.get_positions.return_value = {"market_positions": []}
        candidate = _make_candidate()

        with patch("bot.time") as mock_time, \
             patch("bot.fp_str_to_int", return_value=0):
            mock_time.time.return_value = 1000.0
            mock_time.sleep = MagicMock()
            result = ex._submit_taker(candidate)

        self.assertIsNone(result)
        ex._state.mark_order_status.assert_called_with("ord-ioc-2", "canceled")

    def test_ioc_api_failure_returns_none(self):
        """API returning None for taker → returns None, increments counter."""
        ex = self._make_taker_executor()
        ex._client.place_order.return_value = None
        candidate = _make_candidate()

        result = ex._submit_taker(candidate)

        self.assertIsNone(result)
        ex._state.mark_order_status.assert_called()


class TestGhostFillProtection(unittest.TestCase):
    """Ghost fill detection layers A and B."""

    def test_layer_a_remaining_zero_fill_count_positive(self):
        """remaining_count=0 AND fill_count>0 → ghost fill detected, position registered."""
        ex = _make_executor()
        ex._client.place_order.return_value = {
            "order": {
                "order_id": "ord-ghost-a",
                "remaining_count": 0,
                "fill_count_fp": None,
            }
        }
        # No fills from REST polling
        ex._client.get_fills.return_value = {"fills": []}
        candidate = _make_candidate()

        with patch("bot.time") as mock_time, \
             patch("bot.fp_str_to_int", return_value=5):  # fill_count=5
            mock_time.time.return_value = 1000.0
            mock_time.sleep = MagicMock()
            result = ex._submit_taker(candidate)

        # Should register ghost fill position
        self.assertIsNotNone(result, "Ghost fill Layer A should return order_info")
        ex._state.record_position_from_fill.assert_called_once()
        call_kwargs = ex._state.record_position_from_fill.call_args.kwargs
        self.assertEqual(call_kwargs["fill_source"], "ghost_fill")
        self.assertEqual(call_kwargs["count"], 5)

    def test_layer_a_remaining_zero_fill_count_zero_NOT_ghost(self):
        """remaining_count=0 AND fill_count=0 → IOC auto-cancel, NOT ghost fill.

        This is the exact bug from KXSOL15M-26MAR061400-00 (Mar 6 2026):
        Old code registered 44 phantom contracts → -$39.16 false loss.
        """
        ex = _make_executor()
        ex._client.place_order.return_value = {
            "order": {
                "order_id": "ord-ghost-false",
                "remaining_count": 0,
                "fill_count_fp": None,
            }
        }
        ex._client.get_fills.return_value = {"fills": []}
        ex._client.get_positions.return_value = {"market_positions": []}
        candidate = _make_candidate()

        with patch("bot.time") as mock_time, \
             patch("bot.fp_str_to_int", return_value=0):  # fill_count=0
            mock_time.time.return_value = 1000.0
            mock_time.sleep = MagicMock()
            result = ex._submit_taker(candidate)

        # Should NOT register a ghost fill
        self.assertIsNone(result, "remaining=0 + fill_count=0 must NOT be treated as ghost fill")
        ex._state.record_position_from_fill.assert_not_called()
        # Should mark as canceled
        ex._state.mark_order_status.assert_called_with("ord-ghost-false", "canceled")

    def test_layer_b_positions_api_detects_ghost(self):
        """Positions API shows contracts → ghost fill Layer B registers position."""
        ex = _make_executor()
        ex._client.place_order.return_value = {
            "order": {
                "order_id": "ord-ghost-b",
                "remaining_count": 3,  # looks unfilled
                "fill_count_fp": None,
            }
        }
        ex._client.get_fills.return_value = {"fills": []}
        # But positions API shows we have contracts
        ex._client.get_positions.return_value = {
            "market_positions": [{
                "ticker": "KXBTC15M-26MAR091200-B68500",
                "position": 5,
                "position_fp": None,
                "market_exposure": 460,
                "market_exposure_dollars": None,
            }]
        }
        candidate = _make_candidate()

        with patch("bot.time") as mock_time, \
             patch("bot.fp_str_to_int", return_value=0), \
             patch("bot.dollars_str_to_cents", return_value=460):
            mock_time.time.return_value = 1000.0
            mock_time.sleep = MagicMock()
            result = ex._submit_taker(candidate)

        self.assertIsNotNone(result, "Ghost fill Layer B should detect via positions API")
        ex._state.record_position_from_fill.assert_called_once()
        call_kwargs = ex._state.record_position_from_fill.call_args.kwargs
        self.assertEqual(call_kwargs["fill_source"], "ghost_fill_positions_api")


class TestPostOnlyRejectionTiers(unittest.TestCase):
    """Three-tier post_only rejection escalation."""

    def test_tier1_normal_maker(self):
        """0-1 rejections → normal maker submission."""
        ex = _make_executor()
        ex._client.place_order.return_value = {
            "order": {"order_id": "ord-t1"}
        }
        candidate = _make_candidate(seconds_to_close=400)

        with patch("bot.OBSERVATION_MODE", False), \
             patch("bot.get_market_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(observation_only=False, min_entry_price=86)
            ex.execute(candidate)

        call_args = ex._client.place_order.call_args
        self.assertTrue(call_args.kwargs.get("post_only"))
        # Normal offset, no degradation
        self.assertEqual(call_args.kwargs.get("yes_price"), 91)  # 92 - 1

    def test_tier2_degraded_maker(self):
        """POST_ONLY_MAX_SAME_PRICE rejections → degraded maker (extra offset)."""
        ex = _make_executor()
        ticker = "KXBTC15M-26MAR091200-B68500"
        # Seed rejection count to exactly POST_ONLY_MAX_SAME_PRICE (2)
        ex._post_only_rejections[ticker] = (POST_ONLY_MAX_SAME_PRICE, time.time())

        ex._client.place_order.return_value = {
            "order": {"order_id": "ord-t2"}
        }
        candidate = _make_candidate(seconds_to_close=400)

        with patch("bot.OBSERVATION_MODE", False), \
             patch("bot.get_market_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(observation_only=False, min_entry_price=86)
            ex.execute(candidate)

        call_args = ex._client.place_order.call_args
        self.assertTrue(call_args.kwargs.get("post_only"))
        # Degraded: offset + extra = (92 - 1) - 1 = 90
        self.assertEqual(call_args.kwargs.get("yes_price"), 90,
                         "Degraded maker should be normal_offset + POST_ONLY_DEGRADED_EXTRA_OFFSET")
        self.assertEqual(ex._session_post_only_degraded_attempts, 1)

    def test_tier3_taker_escalation(self):
        """3+ rejections → taker IOC escalation."""
        ex = _make_executor()
        ticker = "KXBTC15M-26MAR091200-B68500"
        # Seed rejection count to POST_ONLY_MAX_SAME_PRICE + 1 (3)
        ex._post_only_rejections[ticker] = (POST_ONLY_MAX_SAME_PRICE + 1, time.time())

        ex._client.place_order.return_value = {
            "order": {
                "order_id": "ord-t3-taker",
                "remaining_count": 0,
                "fill_count_fp": None,
            }
        }
        ex._client.get_fills.return_value = {"fills": []}
        ex._client.get_positions.return_value = {"market_positions": []}
        candidate = _make_candidate(
            seconds_to_close=400,
            calibrated_prob=0.96,
            best_yes_ask=92,
            position_size=5,
        )

        with patch("bot.OBSERVATION_MODE", False), \
             patch("bot.get_market_config") as mock_cfg, \
             patch("bot.time") as mock_time, \
             patch("bot.fp_str_to_int", return_value=5):
            mock_cfg.return_value = MagicMock(observation_only=False, min_entry_price=86)
            mock_time.time.return_value = 1000.0
            mock_time.sleep = MagicMock()
            ex._get_addon_best_ask = MagicMock(return_value=92)
            ex.execute(candidate)

        call_args = ex._client.place_order.call_args
        self.assertEqual(call_args.kwargs.get("time_in_force"), "immediate_or_cancel",
                         "Tier 3 must escalate to IOC taker")
        self.assertEqual(ex._session_post_only_taker_escalations, 1)

    def test_rejection_expiry(self):
        """Stale rejections (> POST_ONLY_REJECTION_EXPIRY) are cleared."""
        ex = _make_executor()
        ticker = "KXBTC15M-26MAR091200-B68500"
        # Seed 5 rejections but from long ago
        ex._post_only_rejections[ticker] = (5, time.time() - POST_ONLY_REJECTION_EXPIRY - 1)

        count = ex._get_post_only_rejection_count(ticker)
        self.assertEqual(count, 0, "Expired rejections should return 0")
        self.assertNotIn(ticker, ex._post_only_rejections,
                         "Expired rejections should be cleaned up")

    def test_rejection_counting(self):
        """_record_post_only_rejection increments correctly."""
        ex = _make_executor()
        ticker = "KXBTC-TEST"

        ex._record_post_only_rejection(ticker)
        self.assertEqual(ex._get_post_only_rejection_count(ticker), 1)

        ex._record_post_only_rejection(ticker)
        self.assertEqual(ex._get_post_only_rejection_count(ticker), 2)

        ex._record_post_only_rejection(ticker)
        self.assertEqual(ex._get_post_only_rejection_count(ticker), 3)


class TestEscalationWaitTiming(unittest.TestCase):
    """Urgency-based escalation wait windows."""

    def test_long_remaining_default(self):
        """>=180s to close, non-BTC → ESCALATION_WAIT_LONG (15s)."""
        wait = OrderExecutor._escalation_wait(300, asset="ETH")
        self.assertEqual(wait, ESCALATION_WAIT_LONG)

    def test_long_remaining_btc_override(self):
        """>=180s to close, BTC → BTC_ESCALATION_WAIT_OVERRIDE (7s)."""
        wait = OrderExecutor._escalation_wait(300, asset="BTC")
        self.assertEqual(wait, BTC_ESCALATION_WAIT_OVERRIDE)

    def test_medium_remaining(self):
        """120-180s to close → ESCALATION_WAIT_MEDIUM (7s)."""
        wait = OrderExecutor._escalation_wait(150, asset="ETH")
        self.assertEqual(wait, ESCALATION_WAIT_MEDIUM)

    def test_short_remaining(self):
        """<120s to close → ESCALATION_WAIT_SHORT (5s)."""
        wait = OrderExecutor._escalation_wait(90, asset="ETH")
        self.assertEqual(wait, ESCALATION_WAIT_SHORT)

    def test_boundary_180(self):
        """Exactly 180s → long tier."""
        wait = OrderExecutor._escalation_wait(180, asset="ETH")
        self.assertEqual(wait, ESCALATION_WAIT_LONG)

    def test_boundary_120(self):
        """Exactly 120s → medium tier."""
        wait = OrderExecutor._escalation_wait(120, asset="ETH")
        self.assertEqual(wait, ESCALATION_WAIT_MEDIUM)


class TestMakerToTakerEscalation(unittest.TestCase):
    """tick() escalation from maker to taker."""

    def test_escalation_cancels_before_taker(self):
        """_escalate_to_taker must cancel maker before submitting taker IOC."""
        ex = _make_executor()
        call_order = []

        def mock_cancel(asset, reason):
            call_order.append("cancel")
            ex._active_orders.pop(asset, None)
            return True

        def mock_place(**kwargs):
            call_order.append("taker_submit")
            return {
                "order": {
                    "order_id": "ord-esc-1",
                    "remaining_count": 0,
                    "fill_count_fp": None,
                }
            }

        ex._cancel_order = mock_cancel
        ex._client.place_order.side_effect = mock_place
        ex._client.get_fills.return_value = {"fills": []}
        ex._client.get_positions.return_value = {"market_positions": []}
        # Mock orderbook fetch for escalation
        ex._client.get_orderbook.return_value = {
            "orderbook": {"no": [[8, 10]]}  # best YES ask = 100-8 = 92
        }

        order = {
            "order_id": "maker-1",
            "client_order_id": "cid-1",
            "ticker": "KXBTC15M-26MAR091200-B68500",
            "event_ticker": "KXBTC15M-26MAR091200",
            "asset": "BTC",
            "price_cents": 91,
            "count": 5,
            "is_taker": False,
            "submit_time": time.time() - 20,
            "seconds_to_close_at_submit": 400,
            "candidate": _make_candidate(),
            "balance_at_entry": 50000,
            "_last_poll": 0,
            "_ask_history": deque(maxlen=30),
            "_last_queue_poll": 0,
        }
        ex._active_orders["BTC"] = order

        with patch("bot.time") as mock_time, \
             patch("bot.fp_str_to_int", return_value=5):
            mock_time.time.return_value = time.time()
            mock_time.sleep = MagicMock()
            # Use a reasonable ask
            with patch.object(type(ex), '_best_ask_depth', return_value=10):
                ex._escalate_to_taker(order, remaining=300)

        self.assertEqual(call_order, ["cancel", "taker_submit"],
                         "Must cancel maker before submitting taker")

    def test_cancel_failure_aborts_taker(self):
        """If cancel fails, do NOT submit taker (prevents double position)."""
        ex = _make_executor()
        ex._cancel_order = MagicMock(return_value=False)
        ex._client.get_orderbook.return_value = {
            "orderbook": {"no": [[8, 10]]}
        }

        order = {
            "order_id": "maker-2",
            "ticker": "KXBTC15M-26MAR091200-B68500",
            "event_ticker": "KXBTC15M-26MAR091200",
            "asset": "BTC",
            "price_cents": 91,
            "count": 5,
            "submit_time": time.time() - 20,
            "seconds_to_close_at_submit": 400,
            "candidate": _make_candidate(),
            "_last_poll": 0,
        }
        ex._active_orders["BTC"] = order

        with patch("bot.OpportunityScanner") as mock_scanner:
            mock_scanner._best_yes_ask_cents.return_value = 92
            mock_scanner._convert_orderbook_fp.return_value = {"no": [[8, 10]]}
            result = ex._escalate_to_taker(order, remaining=300)

        self.assertIsNone(result, "Cancel failure must abort taker to prevent double position")
        # place_order should NOT have been called for taker
        ex._client.place_order.assert_not_called()

    def test_escalation_price_out_of_range_aborts(self):
        """If best ask > ESCALATION_MAX_ENTRY, cancel and abort."""
        ex = _make_executor()
        cancelled = []
        ex._cancel_order = lambda a, r: (cancelled.append(a), True)[1]
        # Return orderbook with no asks (empty)
        ex._client.get_orderbook.return_value = {
            "orderbook": {"no": []}
        }

        order = {
            "order_id": "maker-3",
            "ticker": "KXBTC15M-26MAR091200-B68500",
            "event_ticker": "KXBTC15M-26MAR091200",
            "asset": "BTC",
            "price_cents": 91,
            "count": 5,
            "submit_time": time.time() - 20,
            "seconds_to_close_at_submit": 400,
            "candidate": _make_candidate(),
            "_last_poll": 0,
        }
        ex._active_orders["BTC"] = order

        with patch("bot.OpportunityScanner") as mock_scanner:
            mock_scanner._best_yes_ask_cents.return_value = None
            mock_scanner._convert_orderbook_fp.return_value = {"no": []}
            result = ex._escalate_to_taker(order, remaining=300)

        self.assertIsNone(result)
        self.assertIn("BTC", cancelled)


class TestTickMechanics(unittest.TestCase):
    """tick() polling, WS fill detection, timeout."""

    def test_tick_returns_none_when_no_active_orders(self):
        """No active orders → tick() returns None immediately."""
        ex = _make_executor()
        result = ex.tick()
        self.assertIsNone(result)

    def test_tick_respects_poll_interval(self):
        """tick() skips if less than MAKER_POLL_INTERVAL since last poll."""
        ex = _make_executor()
        now = time.time()
        order = {
            "order_id": "ord-poll",
            "ticker": "KXBTC15M-TEST",
            "asset": "BTC",
            "count": 5,
            "price_cents": 91,
            "submit_time": now - 5,
            "seconds_to_close_at_submit": 400,
            "_last_poll": now - 1,  # polled 1s ago (< 2s interval)
            "_ask_history": deque(maxlen=30),
            "_last_queue_poll": 0,
            "candidate": _make_candidate(),
        }
        ex._active_orders["BTC"] = order
        ex._kalshi_feed.pop_fills.return_value = []

        result = ex.tick()
        # Should skip polling (too recent)
        ex._client.get_fills.assert_not_called()

    def test_tick_ws_fill_detection(self):
        """WS fills are processed before REST polling."""
        ex = _make_executor()
        now = time.time()
        order = {
            "order_id": "ord-ws",
            "ticker": "KXBTC15M-TEST",
            "event_ticker": "KXBTC15M-26MAR091200",
            "asset": "BTC",
            "count": 5,
            "price_cents": 91,
            "submit_time": now - 5,
            "seconds_to_close_at_submit": 400,
            "_last_poll": now - 5,  # old enough to poll
            "_ask_history": deque(maxlen=30),
            "_last_queue_poll": 0,
            "candidate": _make_candidate(),
            "balance_at_entry": 50000,
            "is_taker": False,
            "filled_so_far": 0,
        }
        ex._active_orders["BTC"] = order

        ws_fill = {
            "order_id": "ord-ws",
            "trade_id": "ws-t1",
            "count": 5,
            "price": 91,
        }
        ex._kalshi_feed.pop_fills.return_value = [ws_fill]

        # Mock _on_fill to return the fill count and update filled_so_far
        def on_fill_side_effect(fill, ord_dict):
            ord_dict["filled_so_far"] = ord_dict.get("filled_so_far", 0) + 5
            return 5
        ex._on_fill = MagicMock(side_effect=on_fill_side_effect)

        result = ex.tick()

        self.assertIsNotNone(result)
        self.assertEqual(ex._session_ws_fills, 1)
        # Should be removed from active orders after full fill
        self.assertNotIn("BTC", ex._active_orders)

    def test_hard_timeout_cancels_order(self):
        """Orders exceeding MAKER_TIMEOUT_SECONDS get canceled."""
        ex = _make_executor()
        now = time.time()
        order = {
            "order_id": "ord-timeout",
            "ticker": "KXBTC15M-TEST",
            "event_ticker": "KXBTC15M-26MAR091200",
            "asset": "BTC",
            "count": 5,
            "price_cents": 91,
            "submit_time": now - MAKER_TIMEOUT_SECONDS - 1,  # expired
            "seconds_to_close_at_submit": 400,
            "_last_poll": now - 5,
            "_ask_history": deque(maxlen=30),
            "_last_queue_poll": 0,
            "candidate": _make_candidate(),
            "queue_position": None,
            "escalated": True,  # already escalated → skip escalation path, hit timeout
        }
        ex._active_orders["BTC"] = order
        ex._kalshi_feed.pop_fills.return_value = []
        ex._client.get_fills.return_value = {"fills": []}
        ex._cancel_order = MagicMock()
        ex._get_addon_best_ask = MagicMock(return_value=92)
        ex._client.get_queue_position.return_value = None

        result = ex.tick()
        ex._cancel_order.assert_called_with("BTC", "timeout")


class TestCooldownAndDedup(unittest.TestCase):
    """Ticker cooldown after IOC attempts."""

    def test_recent_taker_ticker_blocked(self):
        """Tickers recently IOC'd are blocked for 60s."""
        ex = _make_executor()
        ticker = "KXBTC15M-26MAR091200-B68500"
        ex._recent_taker_tickers[ticker] = time.time()  # just now
        candidate = _make_candidate(seconds_to_close=400)

        with patch("bot.OBSERVATION_MODE", False), \
             patch("bot.get_market_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(observation_only=False, min_entry_price=86)
            result = ex.execute(candidate)

        self.assertIsNone(result)
        ex._client.place_order.assert_not_called()

    def test_expired_cooldown_allows_execution(self):
        """Tickers cooled down > 60s ago are allowed."""
        ex = _make_executor()
        ticker = "KXBTC15M-26MAR091200-B68500"
        ex._recent_taker_tickers[ticker] = time.time() - 61  # expired
        ex._client.place_order.return_value = {
            "order": {"order_id": "ord-cooldown"}
        }
        candidate = _make_candidate(seconds_to_close=400)

        with patch("bot.OBSERVATION_MODE", False), \
             patch("bot.get_market_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(observation_only=False, min_entry_price=86)
            ex.execute(candidate)

        # Should have submitted an order
        ex._client.place_order.assert_called_once()

    def test_escalating_asset_blocked(self):
        """Assets currently in escalation cannot have new orders."""
        ex = _make_executor()
        ex._escalating_assets.add("BTC")
        candidate = _make_candidate(asset="BTC", seconds_to_close=400)

        with patch("bot.OBSERVATION_MODE", False), \
             patch("bot.get_market_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(observation_only=False, min_entry_price=86)
            result = ex.execute(candidate)

        self.assertIsNone(result)
        ex._client.place_order.assert_not_called()


class TestFeeCalculationInExecution(unittest.TestCase):
    """Fee-adjusted edge checks in execution paths."""

    def test_taker_fee_deducted_from_edge(self):
        """Direct taker path computes net_edge = cal_prob - price/100 - fee/(count*100)."""
        # At 92c, 5 contracts: taker_fee = ceil(0.07 * 5 * 92 * 8 / 100) = ceil(2.576) = 3
        fee = calculate_taker_fee(5, 92)
        self.assertEqual(fee, 3)

        # net_edge = 0.96 - 0.92 - 3/(5*100) = 0.04 - 0.006 = 0.034
        net_edge = 0.96 - (92 / 100.0) - (fee / (5 * 100.0))
        self.assertAlmostEqual(net_edge, 0.034, places=3)
        self.assertGreater(net_edge, MIN_EDGE_PCT / 100.0,
                           "This edge should pass the minimum")

    def test_maker_orders_have_zero_fee(self):
        """Maker orders use post_only=True → $0 fees."""
        # The executor doesn't compute fees for maker path since fees are zero
        # Just verify the constant is correct
        from models import calculate_maker_fee
        self.assertEqual(calculate_maker_fee(10, 92), 0)


class TestEdgeCases(unittest.TestCase):
    """Boundary conditions and error handling."""

    def test_zero_position_size_skipped(self):
        """position_size=0 in direct taker → skipped."""
        ex = _make_executor()
        candidate = _make_candidate(
            seconds_to_close=100,
            position_size=0,
            calibrated_prob=0.96,
        )

        with patch("bot.OBSERVATION_MODE", False), \
             patch("bot.get_market_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(observation_only=False, min_entry_price=86)
            result = ex.execute(candidate)

        self.assertIsNone(result)
        self.assertEqual(ex._session_direct_taker_skipped, 1)

    def test_different_assets_can_have_concurrent_orders(self):
        """BTC and ETH can both have active maker orders."""
        ex = _make_executor()
        ex._active_orders["BTC"] = {"order_id": "btc-1"}

        ex._client.place_order.return_value = {
            "order": {"order_id": "eth-1"}
        }
        candidate = _make_candidate(
            asset="ETH",
            ticker="KXETH15M-26MAR091200-A3200",
            seconds_to_close=400,
        )

        with patch("bot.OBSERVATION_MODE", False), \
             patch("bot.get_market_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(observation_only=False, min_entry_price=86)
            ex.execute(candidate)

        # ETH order should be submitted
        ex._client.place_order.assert_called_once()
        self.assertIn("ETH", ex._active_orders)
        self.assertIn("BTC", ex._active_orders)

    def test_has_active_order_property(self):
        """has_active_order reflects _active_orders state."""
        ex = _make_executor()
        self.assertFalse(ex.has_active_order)

        ex._active_orders["BTC"] = {"order_id": "test"}
        self.assertTrue(ex.has_active_order)

    def test_backwards_compat_active_order_property(self):
        """_active_order returns first order for dashboard compat."""
        ex = _make_executor()
        self.assertIsNone(ex._active_order)

        order = {"order_id": "test-1"}
        ex._active_orders["BTC"] = order
        self.assertEqual(ex._active_order, order)


class TestConstants(unittest.TestCase):
    """Verify critical execution constants haven't drifted."""

    def test_direct_taker_threshold(self):
        self.assertEqual(DIRECT_TAKER_THRESHOLD, 180.0)

    def test_maker_only_threshold(self):
        self.assertEqual(MAKER_ONLY_THRESHOLD, 0.0)

    def test_escalation_waits(self):
        self.assertEqual(ESCALATION_WAIT_LONG, 15.0)
        self.assertEqual(ESCALATION_WAIT_MEDIUM, 7.0)
        self.assertEqual(ESCALATION_WAIT_SHORT, 5.0)

    def test_btc_override(self):
        self.assertEqual(BTC_ESCALATION_WAIT_OVERRIDE, 7.0)

    def test_post_only_config(self):
        self.assertEqual(POST_ONLY_MAX_SAME_PRICE, 2)
        self.assertEqual(POST_ONLY_DEGRADED_EXTRA_OFFSET, 1)
        self.assertEqual(POST_ONLY_REJECTION_EXPIRY, 30.0)

    def test_maker_price_offset(self):
        self.assertEqual(MAKER_PRICE_OFFSET, 1)

    def test_escalation_max_entry(self):
        self.assertEqual(ESCALATION_MAX_ENTRY, 99)

    def test_sol_taker_first(self):
        self.assertTrue(SOL_TAKER_FIRST)

    def test_early_escalation_min_move(self):
        self.assertEqual(EARLY_ESCALATION_MIN_MOVE, 5)


if __name__ == "__main__":
    unittest.main()
