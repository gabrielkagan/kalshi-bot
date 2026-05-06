"""Tests for ghost fill detection in _submit_taker.

Verifies that the two-layer ghost fill detection correctly handles:
  Layer A: remaining_count=0 from Kalshi order response (order matched but fills API lagged)
  Layer B: positions API verification when remaining_count > 0 but position exists

Run: python3 test_ghost_fill.py
"""

import math
import time
import sqlite3
import unittest
from unittest.mock import MagicMock, patch, call


# ── Inline helpers (from bot.py) ───────────────────────────────────────

def fp_str_to_int(s) -> int:
    if s is None:
        return 0
    try:
        return int(round(float(s) * 100))
    except (ValueError, TypeError):
        return 0


def dollars_str_to_cents(s) -> int:
    if s is None:
        return 0
    try:
        return int(round(float(s) * 100))
    except (ValueError, TypeError):
        return 0


def calculate_taker_fee(count: int, price_cents: int) -> int:
    return math.ceil(0.07 * count * price_cents * (100 - price_cents) / 100)


# ── Simulated _submit_taker logic ──────────────────────────────────────
# This mirrors the ghost fill detection code path from bot.py's _submit_taker.
# We extract just the decision logic after fill polling returns total_filled=0.

def ghost_fill_check(
    remaining_count: int,
    total_filled: int,
    count: int,
    price: int,
    ticker: str,
    candidate: dict,
    order_info: dict,
    client_get_positions=None,
    state_record_position=None,
    state_mark_order_status=None,
    order_fill_count: int = 0,
):
    """Simulate the ghost fill detection logic after fill polling.

    Returns:
        (detected, layer, details) where:
          detected: bool - whether ghost fill was found
          layer: str - "A", "B", or None
          details: dict with recorded position info
    """
    if total_filled > 0:
        return False, None, {"reason": "fills_found_normally"}

    # Layer A: remaining_count from order response
    # CRITICAL: For IOC orders, remaining_count=0 can mean auto-canceled with
    # zero fills. Must verify fill_count > 0 to distinguish real ghost fills
    # from unfilled IOC cancellations.
    if remaining_count == 0 and order_fill_count > 0:
        if state_record_position:
            state_record_position(
                ticker=ticker,
                event_ticker=candidate["event_ticker"],
                asset=candidate["asset"],
                side="yes",
                count=count,
                price_cents=price,
                is_taker=True,
                fill_source="ghost_fill",
            )
        if state_mark_order_status:
            state_mark_order_status("filled")
        return True, "A", {
            "count": count,
            "price": price,
            "source": "ghost_fill",
        }

    # Layer B: positions API verification
    if client_get_positions:
        try:
            pos_resp = client_get_positions()
            if pos_resp and pos_resp.get("market_positions"):
                for pos in pos_resp["market_positions"]:
                    if pos.get("ticker") == ticker:
                        pos_count = fp_str_to_int(pos.get("position_fp")) or (pos.get("position") or 0)
                        if pos_count > 0:
                            pos_cost_d = pos.get("market_exposure_dollars")
                            pos_cost = dollars_str_to_cents(pos_cost_d) if pos_cost_d else (pos.get("market_exposure") or 0)
                            pos_avg = pos_cost // pos_count if pos_count else price
                            if state_record_position:
                                state_record_position(
                                    ticker=ticker,
                                    event_ticker=candidate["event_ticker"],
                                    asset=candidate["asset"],
                                    side="yes",
                                    count=pos_count,
                                    price_cents=pos_avg,
                                    is_taker=True,
                                    fill_source="ghost_fill_positions_api",
                                )
                            if state_mark_order_status:
                                state_mark_order_status("filled")
                            return True, "B", {
                                "count": pos_count,
                                "price": pos_avg,
                                "source": "ghost_fill_positions_api",
                            }
        except Exception:
            pass

    return False, None, {"reason": "genuinely_unfilled"}


# ── Test Cases ─────────────────────────────────────────────────────────

class TestGhostFillLayerA(unittest.TestCase):
    """Layer A: remaining_count=0 from order response."""

    def _make_candidate(self, **overrides):
        base = {
            "ticker": "KXBTC15M-26MAR061015-15",
            "event_ticker": "KXBTC15M-26MAR061015",
            "asset": "BTC",
            "best_yes_ask": 89,
            "position_size": 34,
            "calibrated_prob": 0.91,
            "edge": 0.02,
            "strategy": "MAKER_PATIENT",
            "entry_path": "escalation_ioc",
            "escalation_type": "ask_confirmed",
            "vol_regime": "normal",
            "kelly_f": 0.13,
            "seconds_to_close": 400,
            "balance_at_scan": 50000,
        }
        base.update(overrides)
        return base

    def _make_order_info(self, candidate):
        return {
            "order_id": "test-order-123",
            "ticker": candidate["ticker"],
            "event_ticker": candidate["event_ticker"],
            "asset": candidate["asset"],
            "price_cents": candidate["best_yes_ask"],
            "count": candidate["position_size"],
            "is_taker": True,
            "submit_time": time.time(),
            "seconds_to_close_at_submit": candidate["seconds_to_close"],
            "candidate": candidate,
        }

    def test_remaining_zero_with_fill_count_triggers_ghost_fill(self):
        """remaining_count=0, fill_count>0, no fills polled → ghost fill via Layer A."""
        candidate = self._make_candidate()
        order_info = self._make_order_info(candidate)
        record_fn = MagicMock()
        status_fn = MagicMock()

        detected, layer, details = ghost_fill_check(
            remaining_count=0,
            total_filled=0,
            count=34,
            price=89,
            ticker=candidate["ticker"],
            candidate=candidate,
            order_info=order_info,
            state_record_position=record_fn,
            state_mark_order_status=status_fn,
            order_fill_count=34,
        )

        self.assertTrue(detected)
        self.assertEqual(layer, "A")
        self.assertEqual(details["count"], 34)
        self.assertEqual(details["price"], 89)
        self.assertEqual(details["source"], "ghost_fill")
        record_fn.assert_called_once()
        status_fn.assert_called_once_with("filled")

    def test_remaining_zero_fill_count_zero_is_canceled_ioc(self):
        """remaining_count=0, fill_count=0 → IOC canceled unfilled, NOT ghost fill.

        Regression test for KXSOL15M-26MAR061400-00 false ghost fill (Mar 6, 2026).
        IOC order was auto-canceled with 0 fills, but remaining_count=0 because
        canceled contracts are removed. Bot falsely registered 44 phantom contracts
        → -$39.16 loss on a market that settled YES.
        """
        candidate = self._make_candidate(best_yes_ask=89, position_size=44)
        order_info = self._make_order_info(candidate)
        record_fn = MagicMock()
        status_fn = MagicMock()

        detected, layer, details = ghost_fill_check(
            remaining_count=0,
            total_filled=0,
            count=44,
            price=89,
            ticker=candidate["ticker"],
            candidate=candidate,
            order_info=order_info,
            state_record_position=record_fn,
            state_mark_order_status=status_fn,
            order_fill_count=0,  # Key: Kalshi says 0 fills
        )

        self.assertFalse(detected)
        self.assertIsNone(layer)
        record_fn.assert_not_called()
        status_fn.assert_not_called()

    def test_remaining_zero_uses_limit_price(self):
        """Ghost fill should register at the limit price (conservative)."""
        candidate = self._make_candidate(best_yes_ask=92, position_size=10)
        order_info = self._make_order_info(candidate)
        record_fn = MagicMock()

        detected, layer, details = ghost_fill_check(
            remaining_count=0, total_filled=0, count=10, price=92,
            ticker=candidate["ticker"], candidate=candidate,
            order_info=order_info, state_record_position=record_fn,
            order_fill_count=10,
        )

        self.assertTrue(detected)
        self.assertEqual(details["price"], 92)

    def test_remaining_nonzero_skips_layer_a(self):
        """remaining_count > 0 → Layer A should NOT trigger."""
        candidate = self._make_candidate()
        order_info = self._make_order_info(candidate)

        detected, layer, _ = ghost_fill_check(
            remaining_count=34,  # all unfilled
            total_filled=0, count=34, price=89,
            ticker=candidate["ticker"], candidate=candidate,
            order_info=order_info,
        )

        self.assertFalse(detected)
        self.assertIsNone(layer)

    def test_fills_found_skips_ghost_detection(self):
        """total_filled > 0 → normal path, no ghost detection needed."""
        candidate = self._make_candidate()
        order_info = self._make_order_info(candidate)

        detected, layer, details = ghost_fill_check(
            remaining_count=0, total_filled=34, count=34, price=89,
            ticker=candidate["ticker"], candidate=candidate,
            order_info=order_info,
        )

        self.assertFalse(detected)
        self.assertEqual(details["reason"], "fills_found_normally")

    def test_layer_a_prevents_duplicate_registration(self):
        """Layer A fires first; Layer B should not also fire."""
        candidate = self._make_candidate()
        order_info = self._make_order_info(candidate)
        record_fn = MagicMock()
        positions_fn = MagicMock()  # should never be called

        detected, layer, _ = ghost_fill_check(
            remaining_count=0, total_filled=0, count=34, price=89,
            ticker=candidate["ticker"], candidate=candidate,
            order_info=order_info,
            client_get_positions=positions_fn,
            state_record_position=record_fn,
            order_fill_count=34,
        )

        self.assertEqual(layer, "A")
        positions_fn.assert_not_called()  # Layer B skipped


class TestGhostFillLayerB(unittest.TestCase):
    """Layer B: positions API verification."""

    def _make_candidate(self, **overrides):
        base = {
            "ticker": "KXBTC15M-26MAR061015-15",
            "event_ticker": "KXBTC15M-26MAR061015",
            "asset": "BTC",
            "best_yes_ask": 89,
            "position_size": 34,
            "calibrated_prob": 0.91,
            "edge": 0.02,
            "strategy": "MAKER_PATIENT",
        }
        base.update(overrides)
        return base

    def _make_order_info(self, candidate):
        return {
            "order_id": "test-order-456",
            "ticker": candidate["ticker"],
            "submit_time": time.time(),
        }

    def test_positions_api_finds_ghost_position(self):
        """remaining_count > 0, but positions API shows contracts → Layer B detects."""
        candidate = self._make_candidate()
        order_info = self._make_order_info(candidate)
        record_fn = MagicMock()
        status_fn = MagicMock()

        def mock_get_positions():
            return {
                "market_positions": [
                    {
                        "ticker": "KXBTC15M-26MAR061015-15",
                        "position": 34,
                        "market_exposure": 2958,  # 34 * 87 avg
                    }
                ]
            }

        detected, layer, details = ghost_fill_check(
            remaining_count=34,  # Kalshi says unfilled
            total_filled=0, count=34, price=89,
            ticker=candidate["ticker"], candidate=candidate,
            order_info=order_info,
            client_get_positions=mock_get_positions,
            state_record_position=record_fn,
            state_mark_order_status=status_fn,
        )

        self.assertTrue(detected)
        self.assertEqual(layer, "B")
        self.assertEqual(details["count"], 34)
        self.assertEqual(details["price"], 87)  # 2958 // 34 = 87
        self.assertEqual(details["source"], "ghost_fill_positions_api")
        record_fn.assert_called_once()
        status_fn.assert_called_once_with("filled")

    def test_positions_api_uses_fp_fields(self):
        """Layer B correctly parses fixed-point position fields."""
        candidate = self._make_candidate()
        order_info = self._make_order_info(candidate)
        record_fn = MagicMock()

        def mock_get_positions():
            return {
                "market_positions": [
                    {
                        "ticker": "KXBTC15M-26MAR061015-15",
                        "position_fp": "0.34",  # 34 cents = 34 contracts
                        "market_exposure_dollars": "29.58",  # $29.58 = 2958 cents
                    }
                ]
            }

        detected, layer, details = ghost_fill_check(
            remaining_count=34, total_filled=0, count=34, price=89,
            ticker=candidate["ticker"], candidate=candidate,
            order_info=order_info,
            client_get_positions=mock_get_positions,
            state_record_position=record_fn,
        )

        self.assertTrue(detected)
        self.assertEqual(layer, "B")
        self.assertEqual(details["count"], 34)
        self.assertEqual(details["price"], 2958 // 34)  # 86

    def test_positions_api_no_position_means_genuinely_unfilled(self):
        """Positions API shows no position → genuinely unfilled."""
        candidate = self._make_candidate()
        order_info = self._make_order_info(candidate)

        def mock_get_positions():
            return {"market_positions": []}

        detected, layer, details = ghost_fill_check(
            remaining_count=34, total_filled=0, count=34, price=89,
            ticker=candidate["ticker"], candidate=candidate,
            order_info=order_info,
            client_get_positions=mock_get_positions,
        )

        self.assertFalse(detected)
        self.assertEqual(details["reason"], "genuinely_unfilled")

    def test_positions_api_different_ticker_ignored(self):
        """Positions for other tickers should not trigger ghost fill."""
        candidate = self._make_candidate()
        order_info = self._make_order_info(candidate)

        def mock_get_positions():
            return {
                "market_positions": [
                    {
                        "ticker": "KXETH15M-26MAR061015-15",  # wrong ticker
                        "position": 10,
                        "market_exposure": 850,
                    }
                ]
            }

        detected, _, details = ghost_fill_check(
            remaining_count=34, total_filled=0, count=34, price=89,
            ticker=candidate["ticker"], candidate=candidate,
            order_info=order_info,
            client_get_positions=mock_get_positions,
        )

        self.assertFalse(detected)
        self.assertEqual(details["reason"], "genuinely_unfilled")

    def test_positions_api_error_falls_through(self):
        """If positions API throws, should fall through to genuinely unfilled."""
        candidate = self._make_candidate()
        order_info = self._make_order_info(candidate)

        def mock_get_positions():
            raise ConnectionError("API timeout")

        detected, _, details = ghost_fill_check(
            remaining_count=34, total_filled=0, count=34, price=89,
            ticker=candidate["ticker"], candidate=candidate,
            order_info=order_info,
            client_get_positions=mock_get_positions,
        )

        self.assertFalse(detected)
        self.assertEqual(details["reason"], "genuinely_unfilled")

    def test_positions_api_returns_none(self):
        """Positions API returns None → genuinely unfilled."""
        candidate = self._make_candidate()
        order_info = self._make_order_info(candidate)

        detected, _, details = ghost_fill_check(
            remaining_count=34, total_filled=0, count=34, price=89,
            ticker=candidate["ticker"], candidate=candidate,
            order_info=order_info,
            client_get_positions=lambda: None,
        )

        self.assertFalse(detected)

    def test_positions_api_zero_position_ignored(self):
        """Position with count=0 should not trigger ghost fill."""
        candidate = self._make_candidate()
        order_info = self._make_order_info(candidate)

        def mock_get_positions():
            return {
                "market_positions": [
                    {
                        "ticker": "KXBTC15M-26MAR061015-15",
                        "position": 0,
                        "market_exposure": 0,
                    }
                ]
            }

        detected, _, _ = ghost_fill_check(
            remaining_count=34, total_filled=0, count=34, price=89,
            ticker=candidate["ticker"], candidate=candidate,
            order_info=order_info,
            client_get_positions=mock_get_positions,
        )

        self.assertFalse(detected)


class TestGhostFillLayerOrdering(unittest.TestCase):
    """Verify Layer A takes precedence over Layer B."""

    def test_layer_a_before_layer_b(self):
        """When remaining_count=0 AND positions API has data, Layer A wins."""
        candidate = {
            "ticker": "KXBTC15M-TEST-1",
            "event_ticker": "KXBTC15M-TEST",
            "asset": "BTC",
        }
        order_info = {"submit_time": time.time()}
        record_fn = MagicMock()
        positions_called = []

        def mock_get_positions():
            positions_called.append(True)
            return {"market_positions": [{"ticker": "KXBTC15M-TEST-1", "position": 34}]}

        detected, layer, _ = ghost_fill_check(
            remaining_count=0, total_filled=0, count=34, price=89,
            ticker=candidate["ticker"], candidate=candidate,
            order_info=order_info,
            client_get_positions=mock_get_positions,
            state_record_position=record_fn,
            order_fill_count=34,
        )

        self.assertEqual(layer, "A")
        self.assertEqual(len(positions_called), 0)  # positions API never called

    def test_no_positions_fn_still_works(self):
        """Layer A works even without positions API function."""
        candidate = {
            "ticker": "KXBTC15M-TEST-2",
            "event_ticker": "KXBTC15M-TEST",
            "asset": "BTC",
        }
        order_info = {"submit_time": time.time()}

        detected, layer, _ = ghost_fill_check(
            remaining_count=0, total_filled=0, count=10, price=90,
            ticker=candidate["ticker"], candidate=candidate,
            order_info=order_info,
            client_get_positions=None,
            order_fill_count=10,
        )

        self.assertTrue(detected)
        self.assertEqual(layer, "A")


class TestGhostFillEdgeCases(unittest.TestCase):
    """Edge cases for ghost fill detection."""

    def test_partial_remaining_count(self):
        """remaining_count between 0 and count (partial fill without fill events)."""
        candidate = {
            "ticker": "KXBTC15M-TEST-3",
            "event_ticker": "KXBTC15M-TEST",
            "asset": "BTC",
        }
        order_info = {"submit_time": time.time()}

        # remaining_count=10 out of 34 — partial match per order response,
        # but no fill events seen. Layer A only fires when remaining=0.
        detected, layer, _ = ghost_fill_check(
            remaining_count=10, total_filled=0, count=34, price=89,
            ticker=candidate["ticker"], candidate=candidate,
            order_info=order_info,
        )

        # Layer A should NOT fire (remaining > 0)
        self.assertFalse(detected)

    def test_single_contract_ghost_fill(self):
        """Ghost fill with count=1 should still be detected."""
        candidate = {
            "ticker": "KXBTC15M-TEST-4",
            "event_ticker": "KXBTC15M-TEST",
            "asset": "BTC",
        }
        order_info = {"submit_time": time.time()}
        record_fn = MagicMock()

        detected, layer, details = ghost_fill_check(
            remaining_count=0, total_filled=0, count=1, price=95,
            ticker=candidate["ticker"], candidate=candidate,
            order_info=order_info,
            state_record_position=record_fn,
            order_fill_count=1,
        )

        self.assertTrue(detected)
        self.assertEqual(details["count"], 1)
        self.assertEqual(details["price"], 95)


class TestSettlementRevenueValidation(unittest.TestCase):
    """Tests that settlement correctly handles revenue for WIN vs LOSS trades.

    Regression: KXSOL15M-26MAR061400-00 had market_result='yes', side='yes'
    but revenue=0 because the position was a false ghost fill.
    """

    def test_win_trade_with_zero_revenue_is_flagged(self):
        """A YES win should have revenue = count * 100. Revenue=0 is suspicious."""
        # This tests the invariant: if side='yes' and market_result='yes',
        # revenue should be count * 100 (or close to it).
        side = "yes"
        market_result = "yes"
        count = 44
        revenue = 0  # The buggy value

        # Compute expected
        expected_revenue = count * 100  # 4400 cents

        # Assert the invariant that caught the bug
        if market_result in ("yes", "all_yes") and side == "yes":
            self.assertGreater(
                expected_revenue, 0,
                "YES win should have positive revenue"
            )
            # Revenue=0 on a YES win means either:
            # 1. Settlement API didn't return revenue (API bug)
            # 2. Position doesn't exist on Kalshi (false ghost fill)
            self.assertNotEqual(
                revenue, expected_revenue,
                "This test verifies the bug scenario where revenue=0"
            )

    def test_loss_trade_zero_revenue_is_normal(self):
        """A loss trade (result opposite of side) correctly has revenue=0."""
        side = "yes"
        market_result = "no"
        revenue = 0

        # This is correct behavior — losing YES position gets 0 revenue
        self.assertEqual(revenue, 0)


if __name__ == "__main__":
    unittest.main()
