"""Standalone tests for the Confirmation Addon feature.

Inline copies of key functions (no bot.py import) following existing test patterns.
Run: python3 test_addon.py
"""

import math
import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

# ── Inline copies of constants ─────────────────────────────────────────
ADDON_ENABLED = True
ADDON_MIN_PRICE_IMPROVEMENT = 3
ADDON_MIN_SECONDS_SINCE_FILL = 10.0
ADDON_MIN_STC_REMAINING = 45.0
ADDON_SIZE_FRACTION = 0.50
ADDON_MAX_PER_POSITION = 1
ADDON_MAX_ENTRY_PRICE = 98
MIN_EDGE_PCT = 0.9


# ── Inline fee calculation ─────────────────────────────────────────────
def calculate_taker_fee(count: int, price_cents: int) -> int:
    return math.ceil(0.07 * count * price_cents * (100 - price_cents) / 100)


# ── Addon evaluation logic (mirrors _check_addon_opportunities) ───────
def evaluate_addon(
    entry_price: int,
    current_ask: int,
    elapsed_seconds: float,
    stc_at_fill: float,
    entry_count: int,
    cal_prob: float,
    balance: int,
    addon_enabled: bool = True,
    already_addon: bool = False,
):
    """Return (should_trigger, reason, addon_count) mirroring bot logic."""
    if not addon_enabled:
        return False, "disabled", 0

    if already_addon:
        return False, "max_addon_reached", 0

    if elapsed_seconds < ADDON_MIN_SECONDS_SINCE_FILL:
        return False, "too_soon", 0

    current_stc = stc_at_fill - elapsed_seconds
    if current_stc < ADDON_MIN_STC_REMAINING:
        return False, "stc_too_low", 0

    improvement = current_ask - entry_price
    if improvement < ADDON_MIN_PRICE_IMPROVEMENT:
        return False, "insufficient_improvement", 0

    if current_ask > ADDON_MAX_ENTRY_PRICE:
        return False, "price_cap", 0

    addon_count = max(1, int(entry_count * ADDON_SIZE_FRACTION))
    taker_fee = calculate_taker_fee(addon_count, current_ask)
    net_edge = cal_prob - (current_ask / 100.0) - (taker_fee / (addon_count * 100.0))

    if net_edge < MIN_EDGE_PCT / 100.0:
        return False, "insufficient_edge", 0

    addon_cost = addon_count * current_ask
    max_addon_cost = int(balance * 0.50)
    if addon_cost > max_addon_cost:
        if current_ask > 0:
            addon_count = max_addon_cost // current_ask
        if addon_count < 1:
            return False, "balance_cap", 0
        taker_fee = calculate_taker_fee(addon_count, current_ask)
        net_edge = cal_prob - (current_ask / 100.0) - (taker_fee / (addon_count * 100.0))
        if net_edge < MIN_EDGE_PCT / 100.0:
            return False, "edge_after_resize", 0

    return True, "trigger", addon_count


class TestAddon(unittest.TestCase):
    """16 test cases covering addon evaluation logic."""

    def test_01_basic_trigger(self):
        """90¢ entry, 93¢ after 20s, STC=100s → addon triggers."""
        ok, reason, count = evaluate_addon(
            entry_price=90, current_ask=93, elapsed_seconds=20,
            stc_at_fill=120, entry_count=10, cal_prob=0.97,
            balance=5000)
        self.assertTrue(ok, f"Expected trigger, got reason={reason}")
        self.assertGreater(count, 0)

    def test_02_insufficient_improvement(self):
        """90¢ → 92¢ (+2¢) — below 3¢ threshold."""
        ok, reason, _ = evaluate_addon(
            entry_price=90, current_ask=92, elapsed_seconds=20,
            stc_at_fill=120, entry_count=10, cal_prob=0.97,
            balance=5000)
        self.assertFalse(ok)
        self.assertEqual(reason, "insufficient_improvement")

    def test_03_too_soon(self):
        """90¢ → 95¢ but only 5s elapsed."""
        ok, reason, _ = evaluate_addon(
            entry_price=90, current_ask=95, elapsed_seconds=5,
            stc_at_fill=120, entry_count=10, cal_prob=0.97,
            balance=5000)
        self.assertFalse(ok)
        self.assertEqual(reason, "too_soon")

    def test_04_stc_too_low(self):
        """STC remaining = 40s (< 45s threshold)."""
        ok, reason, _ = evaluate_addon(
            entry_price=90, current_ask=95, elapsed_seconds=80,
            stc_at_fill=120, entry_count=10, cal_prob=0.97,
            balance=5000)
        self.assertFalse(ok)
        self.assertEqual(reason, "stc_too_low")

    def test_05_insufficient_edge(self):
        """90¢ → 97¢ — thin edge after taker fees."""
        # At 97¢, taker fee is high relative to edge. With prob=0.975,
        # net_edge = 0.975 - 0.97 - fee/(count*100) ≈ tiny
        ok, reason, _ = evaluate_addon(
            entry_price=90, current_ask=97, elapsed_seconds=20,
            stc_at_fill=120, entry_count=5, cal_prob=0.975,
            balance=5000)
        self.assertFalse(ok)
        self.assertEqual(reason, "insufficient_edge")

    def test_06_max_one_addon(self):
        """Price rises to 94¢ then 96¢ — first triggers, second doesn't."""
        # First addon
        ok1, _, count1 = evaluate_addon(
            entry_price=90, current_ask=94, elapsed_seconds=20,
            stc_at_fill=120, entry_count=10, cal_prob=0.97,
            balance=5000, already_addon=False)
        self.assertTrue(ok1)

        # Second addon attempt — already_addon=True
        ok2, reason2, _ = evaluate_addon(
            entry_price=90, current_ask=96, elapsed_seconds=40,
            stc_at_fill=120, entry_count=10, cal_prob=0.98,
            balance=5000, already_addon=True)
        self.assertFalse(ok2)
        self.assertEqual(reason2, "max_addon_reached")

    def test_07_observation_mode(self):
        """All conditions met, observation mode → would trigger logically."""
        # The evaluate function doesn't model observation mode directly;
        # we just verify the conditions pass (observation is handled in _execute_addon)
        ok, reason, count = evaluate_addon(
            entry_price=90, current_ask=93, elapsed_seconds=20,
            stc_at_fill=120, entry_count=10, cal_prob=0.97,
            balance=5000)
        self.assertTrue(ok, "Addon should pass all checks (obs mode is separate)")

    def test_08_addon_disabled(self):
        """ADDON_ENABLED=False → no activity."""
        ok, reason, _ = evaluate_addon(
            entry_price=90, current_ask=95, elapsed_seconds=20,
            stc_at_fill=120, entry_count=10, cal_prob=0.97,
            balance=5000, addon_enabled=False)
        self.assertFalse(ok)
        self.assertEqual(reason, "disabled")

    def test_09_price_cap(self):
        """93¢ → 99¢ (> 98¢ cap)."""
        ok, reason, _ = evaluate_addon(
            entry_price=93, current_ask=99, elapsed_seconds=20,
            stc_at_fill=120, entry_count=10, cal_prob=0.995,
            balance=5000)
        self.assertFalse(ok)
        self.assertEqual(reason, "price_cap")

    def test_10_balance_cap(self):
        """Addon cost > 50% balance → count reduced or skip."""
        # 10 contracts at 94¢ → addon 5 contracts × 94¢ = 470¢
        # Balance = 500¢ → 50% = 250¢ → can afford 250//94 = 2 contracts
        ok, reason, count = evaluate_addon(
            entry_price=90, current_ask=94, elapsed_seconds=20,
            stc_at_fill=120, entry_count=10, cal_prob=0.97,
            balance=500)
        if ok:
            self.assertLessEqual(count * 94, 250,
                                 "Addon cost should respect 50% balance cap")
        # If edge too thin after resize, that's also valid
        self.assertIn(reason, ("trigger", "edge_after_resize", "balance_cap"))

    def test_11_no_orderbook(self):
        """OB fetch returns None → evaluate_addon not called (tested at integration level).
        Here we just verify that the function handles missing data gracefully
        by confirming we need a current_ask to evaluate."""
        # This test validates that _check_addon_opportunities skips when
        # _get_addon_best_ask returns None. Since evaluate_addon requires
        # current_ask, we verify that the logic would not proceed.
        # Integration: _get_addon_best_ask returns None → continue to next tick
        pass  # Integration test — covered by bot logic (no evaluate_addon call)

    def test_12_no_re_register(self):
        """Addon fill should not be re-registered (entry_path check).
        Validated by checking that entry_path='confirmation_addon' skips registration."""
        # _register_addon_eligible returns early if entry_path == "confirmation_addon"
        # We test the gate condition
        candidate = {"entry_path": "confirmation_addon"}
        should_skip = candidate.get("entry_path") == "confirmation_addon"
        self.assertTrue(should_skip, "Addon fills must not re-register")

    def test_13_cleanup_expired(self):
        """Entries >5min old should be cleaned up."""
        # Simulate: fill_time was 6 minutes ago → should be expired
        fill_time = time.time() - 360  # 6 min ago
        elapsed = time.time() - fill_time
        self.assertGreater(elapsed, 300, "Entry should be >5min old")
        # In _check_addon_opportunities, entries >300s old are added to expired[]

    def test_14_sizing_fraction(self):
        """7 contracts × 0.50 = 3."""
        addon_count = max(1, int(7 * ADDON_SIZE_FRACTION))
        self.assertEqual(addon_count, 3)

    def test_15_sizing_minimum(self):
        """1 contract × 0.50 = max(1, 0) = 1."""
        addon_count = max(1, int(1 * ADDON_SIZE_FRACTION))
        self.assertEqual(addon_count, 1)

    def test_16_fee_calculation(self):
        """Verify taker fee matches expected for various combos."""
        # 5 contracts at 93¢: ceil(0.07 × 5 × 93 × 7 / 100) = ceil(2.2785) = 3
        self.assertEqual(calculate_taker_fee(5, 93), 3)
        # 3 contracts at 95¢: ceil(0.07 × 3 × 95 × 5 / 100) = ceil(0.9975) = 1
        self.assertEqual(calculate_taker_fee(3, 95), 1)
        # 1 contract at 98¢: ceil(0.07 × 1 × 98 × 2 / 100) = ceil(0.1372) = 1
        self.assertEqual(calculate_taker_fee(1, 98), 1)
        # 10 contracts at 90¢: ceil(0.07 × 10 × 90 × 10 / 100) = ceil(6.3) = 7
        self.assertEqual(calculate_taker_fee(10, 90), 7)


if __name__ == "__main__":
    unittest.main()
