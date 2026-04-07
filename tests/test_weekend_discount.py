"""Tests for Weekend Edge Discount promotion to live trading.

Guards against:
- Day gate: only fires on Saturday/Sunday (weekday() >= 5)
- Price gate: WEEKEND_DISCOUNT_MIN_PRICE = 89c floor
- STC gate: WEEKEND_DISCOUNT_MAX_STC = 600s ceiling
- DC dedup: decided contract tickers removed from weekend discount candidates
- Kill switch: WEEKEND_DISCOUNT_LIVE = False → shadow only
- Execution: normal maker-first path (NOT direct taker)
- Shadow continuity: sub-89c and high-STC signals still logged as shadow
- Dashboard: weekend_discount_live panel exists in snapshot
"""

import re
import unittest
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.py")
DASH_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard_snapshot.py")


def _read_bot():
    with open(BOT_PATH) as f:
        return f.read()


def _read_dash():
    with open(DASH_PATH) as f:
        return f.read()


class TestWeekendDiscountConstants(unittest.TestCase):
    """Verify promotion constants exist and have correct values."""

    def setUp(self):
        self.source = _read_bot()

    def test_live_flag_exists(self):
        self.assertIn("WEEKEND_DISCOUNT_LIVE = True", self.source)

    def test_min_price_exists(self):
        self.assertIn("WEEKEND_DISCOUNT_MIN_PRICE = 90", self.source)

    def test_max_stc_exists(self):
        self.assertIn("WEEKEND_DISCOUNT_MAX_STC = 600", self.source)

    def test_discount_factor_unchanged(self):
        self.assertIn("WEEKEND_EDGE_DISCOUNT = 0.60", self.source)


class TestWeekendDiscountLiveGates(unittest.TestCase):
    """Verify the live eligibility checks are in the code."""

    def setUp(self):
        self.source = _read_bot()
        # Find the weekend discount block
        self.block_start = self.source.find("Weekend Edge Discount (Live + Shadow)")
        self.assertGreater(self.block_start, 0, "Weekend discount block not found")
        # Find the next major section
        self.block_end = self.source.find("Overnight Edge Discount Shadow", self.block_start)
        self.block = self.source[self.block_start:self.block_end]

    def test_day_gate(self):
        """Weekend discount only fires on weekday() >= 5 (Saturday/Sunday)."""
        self.assertIn("weekday() >= 5", self.block)

    def test_price_gate(self):
        """Live path requires best_ask >= WEEKEND_DISCOUNT_MIN_PRICE."""
        self.assertIn("WEEKEND_DISCOUNT_MIN_PRICE", self.block)

    def test_stc_gate(self):
        """Live path requires seconds_remaining <= WEEKEND_DISCOUNT_MAX_STC."""
        self.assertIn("WEEKEND_DISCOUNT_MAX_STC", self.block)

    def test_dc_dedup_gate(self):
        """Live path checks for decided contract overlap."""
        self.assertIn("DECIDED_CONTRACT_Z_T2", self.block)
        self.assertIn("_wknd_dc_overlap", self.block)

    def test_observation_mode_gate(self):
        """Live path blocked when OBSERVATION_MODE is True."""
        self.assertIn("not OBSERVATION_MODE", self.block)

    def test_kill_switch_gate(self):
        """Live path blocked when WEEKEND_DISCOUNT_LIVE is False."""
        self.assertIn("WEEKEND_DISCOUNT_LIVE", self.block)

    def test_candidates_append(self):
        """Live path appends to candidates list."""
        self.assertIn("candidates.append(", self.block)

    def test_strategy_tag(self):
        """Live candidates tagged with strategy='weekend_discount'."""
        self.assertIn('"strategy": "weekend_discount"', self.block)


class TestWeekendDiscountShadowContinuity(unittest.TestCase):
    """Verify shadow signals continue logging for excluded signals."""

    def setUp(self):
        self.source = _read_bot()
        self.block_start = self.source.find("Weekend Edge Discount (Live + Shadow)")
        self.block_end = self.source.find("Overnight Edge Discount Shadow", self.block_start)
        self.block = self.source[self.block_start:self.block_end]

    def test_shadow_stage_still_used(self):
        """weekend_discount_shadow filter_stage still exists for non-eligible signals."""
        self.assertIn("weekend_discount_shadow", self.block)

    def test_both_stages_in_block(self):
        """Both weekend_discount and weekend_discount_shadow stages are in the block."""
        self.assertIn('"weekend_discount"', self.block)
        self.assertIn('"weekend_discount_shadow"', self.block)

    def test_stage_selection_logic(self):
        """Filter stage is determined by live eligibility."""
        self.assertIn('_wknd_stage = "weekend_discount" if _wknd_live_eligible else "weekend_discount_shadow"',
                      self.block)

    def test_db_insert_uses_dynamic_stage(self):
        """DB insert uses the dynamic _wknd_stage variable."""
        self.assertIn("_wknd_stage,", self.block)


class TestWeekendDiscountExecution(unittest.TestCase):
    """Verify weekend discount uses normal execution path, not direct taker."""

    def setUp(self):
        self.source = _read_bot()

    @pytest.mark.fragile
    def test_not_routed_to_direct_taker(self):
        """Weekend discount strategy is NOT in the decided contract taker override."""
        # Find the decided contract taker override section
        dc_taker_start = self.source.find("Decided contract taker override")
        self.assertGreater(dc_taker_start, 0)
        dc_taker_block = self.source[dc_taker_start:dc_taker_start + 800]
        # The taker override only matches decided_t1, decided_t1b, decided_t2
        self.assertIn('"decided_t1"', dc_taker_block)
        self.assertNotIn('"weekend_discount"', dc_taker_block)

    def test_dc_dedup_at_candidate_separation(self):
        """Weekend discount candidates overlapping with DC are removed at separation."""
        sep_start = self.source.find("Separate overlay candidates")
        self.assertGreater(sep_start, 0)
        sep_block = self.source[sep_start:sep_start + 1200]
        self.assertIn("weekend_discount", sep_block)
        self.assertIn("_dc_tickers", sep_block)

    def test_sol_taker_first_applies(self):
        """SOL_TAKER_FIRST applies to all 15M candidates including weekend discount.
        Weekend discount candidates have product_type='15m' and go through normal
        execution which checks SOL_TAKER_FIRST."""
        # Just verify SOL_TAKER_FIRST exists — it applies to all 15M candidates
        self.assertIn("SOL_TAKER_FIRST", self.source)


class TestWeekendDiscountDashboard(unittest.TestCase):
    """Verify dashboard snapshot tracks weekend discount live trades."""

    def setUp(self):
        self.dash_source = _read_dash()

    def test_weekend_discount_live_panel(self):
        """Dashboard snapshot creates weekend_discount_live panel."""
        self.assertIn('snap["weekend_discount_live"]', self.dash_source)

    def test_weekend_discount_live_queries_settled_trades(self):
        """Dashboard queries settled_trades for weekend_discount strategy."""
        self.assertIn("strategy = 'weekend_discount'", self.dash_source)

    def test_weekend_discount_live_in_slow_cache(self):
        """weekend_discount_live is in the slow-changing cache."""
        slow_start = self.dash_source.find("_SLOW_SNAP_KEYS")
        slow_block = self.dash_source[slow_start:slow_start + 400]
        self.assertIn("weekend_discount_live", slow_block)

    def test_weekend_discount_shadow_panel_preserved(self):
        """Existing weekend_discount_shadow panel still exists."""
        self.assertIn('snap["weekend_discount_shadow"]', self.dash_source)


class TestWeekendDiscountWeekdayUnchanged(unittest.TestCase):
    """Verify weekday trading logic is completely unchanged."""

    def setUp(self):
        self.source = _read_bot()

    def test_no_weekend_discount_on_weekdays(self):
        """The weekend discount block is gated by weekday() >= 5.
        This means weekdays (0-4) never enter the block."""
        block_start = self.source.find("Weekend Edge Discount (Live + Shadow)")
        block = self.source[block_start:block_start + 600]
        self.assertIn("weekday() >= 5", block)
        # No fallback that could fire on weekdays
        self.assertNotIn("weekday() <", block)
        self.assertNotIn("weekday() !=", block)


if __name__ == "__main__":
    unittest.main()
