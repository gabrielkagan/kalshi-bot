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

import os
import re
import sys
import unittest

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

BOT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "bot/_impl.py")
DASH_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "bot", "snapshots", "dashboard_snapshot.py")  # Sprint 10 Bit 10.4 (2026-05-12)


def _read_bot():
    """Bit 3.1: returns concat of bot/_impl.py + bot/constants.py source.
    Tests that look for CONSTANT = value definitions (post-extraction
    they live in bot/constants.py) AND tests that look for class /
    function / log-string patterns (still in bot/_impl.py) both find
    their targets in the concatenated source.
    """
    impl = ""
    if os.path.exists(BOT_PATH):
        with open(BOT_PATH) as f:
            impl = f.read()
    # Bit 8.1 (2026-05-10): scanner moved to bot/scanner/__init__.py.
    # Concat its source so audits that grep for OpportunityScanner
    # content (filter_stage literals, gate comments, etc.) survive the move.
    _scanner_path = os.path.join(os.path.dirname(BOT_PATH), "scanner", "__init__.py")
    if os.path.isfile(_scanner_path):
        with open(_scanner_path) as _f:
            impl += "\n" + _f.read()
    # Bit 9.1 (2026-05-10): OrderExecutor extracted to bot/executor.py.
    _executor_path = os.path.join(os.path.dirname(BOT_PATH), "executor.py")
    if os.path.isfile(_executor_path):
        with open(_executor_path) as _f:
            impl += "\n" + _f.read()
    constants_path = os.path.join(os.path.dirname(BOT_PATH), "constants.py")
    constants = ""
    if os.path.exists(constants_path):
        with open(constants_path) as f:
            constants = f.read()
        return impl + "\n" + constants
    # Bit 8.1 (2026-05-10): scanner moved to bot/scanner/__init__.py.
    # Concat its source so audits that grep for OpportunityScanner
    # content (filter_stage literals, gate comments, etc.) survive the move.
    _scanner_path = os.path.join(os.path.dirname(BOT_PATH), "scanner", "__init__.py")
    if os.path.isfile(_scanner_path):
        with open(_scanner_path) as _f:
            impl += "\n" + _f.read()
    return impl


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
        self.block_end = self.source.find("Overnight Edge Discount", self.block_start)
        self.assertGreater(self.block_end, self.block_start,
                           "Overnight Edge Discount block-end anchor not found")
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
        self.assertGreater(self.block_start, 0, "Weekend discount block not found")
        self.block_end = self.source.find("Overnight Edge Discount", self.block_start)
        self.assertGreater(self.block_end, self.block_start,
                           "Overnight Edge Discount block-end anchor not found")
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
        self.assertGreater(block_start, 0, "Weekend discount block not found")
        block_end = self.source.find("Overnight Edge Discount", block_start)
        self.assertGreater(block_end, block_start,
                           "Overnight Edge Discount block-end anchor not found")
        block = self.source[block_start:block_end]
        self.assertIn("weekday() >= 5", block)
        # No fallback that could fire on weekdays
        self.assertNotIn("weekday() <", block)
        self.assertNotIn("weekday() !=", block)


class TestWeekendDiscountNoHardcodedBlockStartWindow(unittest.TestCase):
    """Regression: no `block_start + <int>` hardcoded char-window pattern in
    this file. Same brittleness class as 86b9zk0cn (test-anchor weakness) —
    hardcoded windows silently truncate when canonical source layout shifts.
    Canonical pattern: `find("Overnight Edge Discount", block_start)` +
    `assertGreater(block_end, block_start)`.

    Scope: guards the `block_start` identifier specifically — other hardcoded
    windows in this file (`dc_taker_start + 800` at L181, `sep_start + 1200`
    at L190, `slow_start + 400` at L219) are a separate brittleness-cleanup
    surface tracked at ticket 86b9zk74n. The `fallback_anchor + 300` sites
    (L340, L351) are intentional narrow-window grabs after a known comment
    anchor and not in scope. Surfaced by 86b9zk0cn R1 N1. Ticket 86b9zk118.
    """

    def test_no_hardcoded_block_start_window_in_test_file(self):
        """AST guard: no `block_start + <int>` patterns in this file."""
        this_file = os.path.abspath(__file__)
        with open(this_file) as f:
            source = f.read()
        offending = []
        for lineno, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            # Skip docstring/comment lines + the pattern-string-literal itself
            if stripped.startswith('"""') or stripped.startswith('#'):
                continue
            if stripped.startswith('pattern') or stripped.startswith('r"') or stripped.startswith("r'"):
                continue
            if re.search(r'\bblock_start\s*\+\s*\d+', line):
                offending.append((lineno, line.rstrip()))
        self.assertEqual(offending, [],
                         f"Hardcoded `block_start + <int>` patterns found: {offending}. "
                         f"Use the find()+assertGreater anchor pattern instead.")


class TestWeekendDiscountSisterBlockBoundaries(unittest.TestCase):
    """Regression: sister `setUp` methods must use a `block_end` anchor that
    actually exists in the source. Pre-86b9zk0cn, the end-anchor substring
    `"Overnight Edge Discount Shadow"` returned -1 because the canonical
    comment is `"Overnight Edge Discount (Live + Shadow)"` — making both
    TestWeekendDiscountLiveGates and TestWeekendDiscountShadowContinuity
    silently slice `source[block_start:-1]` (most of the concatenated
    source) instead of the narrower weekend-discount block. Their
    `assertIn` checks then passed trivially against the bloated block.
    Surfaced by 86b9zjx7r R3 adversarial review.
    """

    def test_live_gates_block_end_valid(self):
        live = TestWeekendDiscountLiveGates()
        live.setUp()
        self.assertGreater(live.block_end, live.block_start,
                           f"TestWeekendDiscountLiveGates: block_end={live.block_end} "
                           f"block_start={live.block_start} — end anchor must locate a "
                           f"valid position past start (find() returned -1?).")

    def test_shadow_continuity_block_end_valid(self):
        shadow = TestWeekendDiscountShadowContinuity()
        shadow.setUp()
        self.assertGreater(shadow.block_end, shadow.block_start,
                           f"TestWeekendDiscountShadowContinuity: block_end={shadow.block_end} "
                           f"block_start={shadow.block_start} — end anchor must locate a "
                           f"valid position past start (find() returned -1?).")


class TestWeekendDiscountFixedFallbackKellySign(unittest.TestCase):
    """Regression: weekend_discount fixed-size fallback must gate on Kelly sign.

    Surfaced 2026-05-17 by P4.1 (band-calibrated sizing) — SOL 91c shadow
    trace showed Kelly=-0.825 but the fallback fired at WEEKEND_FIXED_RISK
    because the predicate only checked `_wknd_position == 0`. Negative Kelly
    is clamped to 0 contracts by PositionSizer.compute() (bot/models.py:1063),
    making the bare position-check ambiguous between "small positive Kelly
    rounded to 0" (fallback should fire) and "negative Kelly" (fallback
    must NOT fire). Ticket 86b9zjx7r.
    """

    def setUp(self):
        self.source = _read_bot()
        self.block_start = self.source.find("Weekend Edge Discount (Live + Shadow)")
        self.assertGreater(self.block_start, 0, "Weekend discount block not found")
        self.block_end = self.source.find("Overnight Edge Discount", self.block_start)
        self.assertGreater(self.block_end, self.block_start,
                           "Overnight Edge Discount block-end anchor not found")
        self.block = self.source[self.block_start:self.block_end]

    def test_fallback_predicate_references_kelly(self):
        """The fixed-size fallback predicate must reference `_wknd_kelly_f`
        — without it, the gate cannot distinguish positive-but-tiny Kelly
        (fallback intended) from negative Kelly (fallback must skip)."""
        fallback_anchor = self.block.find("Fixed sizing fallback when Kelly")
        self.assertGreater(fallback_anchor, 0, "Fallback site comment not found")
        # The predicate + body live in the ~200 chars after the anchor
        predicate_region = self.block[fallback_anchor:fallback_anchor + 300]
        self.assertIn("_wknd_kelly_f", predicate_region,
                      "Fallback predicate must reference _wknd_kelly_f for Kelly-sign gate")

    def test_fallback_if_line_includes_positive_kelly_check(self):
        """The fallback if-line itself (not the body) must include a positive-Kelly
        check on the SAME conditional. Pinning to the if-line prevents the
        regression where the Kelly check could be added below as a no-op
        log-only branch while the fallback still fires unconditionally."""
        fallback_anchor = self.block.find("Fixed sizing fallback when Kelly")
        self.assertGreater(fallback_anchor, 0)
        predicate_region = self.block[fallback_anchor:fallback_anchor + 300]
        if_idx = predicate_region.find("if _wknd_position == 0")
        self.assertGreater(if_idx, 0, "Could not locate fallback predicate line")
        colon_idx = predicate_region.find(":", if_idx)
        self.assertGreater(colon_idx, if_idx)
        if_line = predicate_region[if_idx:colon_idx]
        self.assertIn("_wknd_kelly_f", if_line,
                      f"Fallback if-line must include Kelly-sign check on same line. Got: {if_line!r}")
        # The check must be a positivity test, not just non-None
        positivity_present = (
            "_wknd_kelly_f > 0" in if_line
            or "(_wknd_kelly_f or 0) > 0" in if_line
        )
        self.assertTrue(positivity_present,
                        f"Fallback predicate must check `_wknd_kelly_f > 0` (positivity, not non-None). Got: {if_line!r}")


if __name__ == "__main__":
    unittest.main()
