"""Tests for Terminal Momentum strategy.

Guards against:
- Price set: {95, 96, 97, 98, 99} — full 95-99c range
- Probability gate: calibrated_prob >= 0.93
- STC window: 61-300 seconds to close
- Fixed sizing: 50 contracts, no Kelly, no drawdown scaler
- Feature flag: TERMINAL_MOMENTUM_ENABLED gates all TM activity
- DC overlap: TM skips tickers already claimed by decided contracts
- Position overlap: TM skips tickers with existing positions
- Concurrent cap: max TM_MAX_CONCURRENT simultaneous TM candidates
- Candidate separation: TM candidates bypass single-asset-per-window filter
- Execute routing: terminal_momentum strategy routes to _execute_tm_taker
"""

import re
import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.py")


def _read_bot():
    with open(BOT_PATH) as f:
        return f.read()


def _extract_constant(source, name):
    """Extract a constant value from source code."""
    m = re.search(rf'^{name}\s*=\s*([^\s#]+)', source, re.MULTILINE)
    if m:
        val = m.group(1)
        try:
            return eval(val)
        except Exception:
            return val
    return None


class TestTMConstants(unittest.TestCase):
    """Verify TM constants are defined with correct values."""

    def setUp(self):
        self.source = _read_bot()

    def test_feature_flag_exists(self):
        self.assertIn("TERMINAL_MOMENTUM_ENABLED", self.source)

    def test_price_set_is_set_not_range(self):
        """TM_PRICE_SET must be a set literal {95, 96, 97, 98, 99}, NOT a range."""
        m = re.search(r'^TM_PRICE_SET\s*=\s*(\{[^}]+\})', self.source, re.MULTILINE)
        self.assertIsNotNone(m, "TM_PRICE_SET must be a set literal")
        price_set = eval(m.group(1))
        self.assertEqual(price_set, {95, 96, 97, 98, 99})

    def test_97_included_in_price_set(self):
        """97c promoted: 98.2% WR on 55 obs, above 97% breakeven."""
        m = re.search(r'^TM_PRICE_SET\s*=\s*(\{[^}]+\})', self.source, re.MULTILINE)
        self.assertIsNotNone(m)
        price_set = eval(m.group(1))
        self.assertIn(97, price_set, "97 should be in TM_PRICE_SET — 98.2% WR above 97% BE")

    def test_min_prob(self):
        self.assertEqual(_extract_constant(self.source, "TM_MIN_PROB"), 0.93)

    def test_stc_window(self):
        self.assertEqual(_extract_constant(self.source, "TM_MIN_STC"), 61)
        self.assertEqual(_extract_constant(self.source, "TM_MAX_STC"), 300)

    def test_fixed_contracts(self):
        self.assertEqual(_extract_constant(self.source, "TM_FIXED_CONTRACTS"), 50)

    def test_max_concurrent(self):
        self.assertEqual(_extract_constant(self.source, "TM_MAX_CONCURRENT"), 4)


class TestTMPriceSetGuard(unittest.TestCase):
    """Verify that TM_PRICE_SET is used correctly in the scan intercept."""

    def setUp(self):
        self.source = _read_bot()

    def test_price_check_uses_in_operator(self):
        """The TM intercept must use 'best_ask in TM_PRICE_SET', not a range check."""
        self.assertIn("best_ask in TM_PRICE_SET", self.source)

    def test_no_range_check_for_tm_prices(self):
        """Ensure no range like '95 <= best_ask <= 99' exists near TM code."""
        # Find TM intercept block
        tm_start = self.source.find("Terminal Momentum intercept")
        self.assertGreater(tm_start, 0)
        tm_block = self.source[tm_start:tm_start + 2000]
        self.assertNotIn("95 <= best_ask <= 99", tm_block)
        self.assertNotIn("best_ask >= 95 and best_ask <= 99", tm_block)

    def test_fresh_ask_also_checked_against_price_set(self):
        """_execute_tm_taker must verify fresh_ask is still in TM_PRICE_SET."""
        self.assertIn("fresh_ask not in TM_PRICE_SET", self.source)


class TestTMScanIntercept(unittest.TestCase):
    """Verify the TM intercept is correctly placed inside the insufficient_edge block."""

    def setUp(self):
        self.source = _read_bot()

    def test_intercept_inside_insufficient_edge(self):
        """TM intercept must be inside 'if fee_adjusted_edge < _min_edge:' block."""
        ie_pos = self.source.find("if fee_adjusted_edge < _min_edge:")
        self.assertGreater(ie_pos, 0)
        tm_pos = self.source.find("Terminal Momentum intercept")
        self.assertGreater(tm_pos, 0)
        self.assertGreater(tm_pos, ie_pos, "TM intercept must come AFTER the edge check")
        # And before the existing rejection code
        scan_stats_pos = self.source.find('scan_stats[asset]["insufficient_edge"]', tm_pos)
        self.assertGreater(scan_stats_pos, tm_pos, "TM intercept must come BEFORE the rejection counter")

    def test_observation_mode_gate(self):
        """TM must NOT fire in observation mode."""
        tm_block = self.source[self.source.find("Terminal Momentum intercept"):][:1500]
        self.assertIn("not OBSERVATION_MODE", tm_block)

    def test_product_type_gate(self):
        """TM is 15M only."""
        tm_block = self.source[self.source.find("Terminal Momentum intercept"):][:1500]
        self.assertIn('_pt in (None, "15m")', tm_block)

    def test_dc_overlap_check(self):
        """TM must check for DC overlap before building candidate."""
        tm_block = self.source[self.source.find("Terminal Momentum intercept"):][:2000]
        self.assertIn("decided_", tm_block)

    def test_position_overlap_check(self):
        """TM must check for existing positions."""
        tm_block = self.source[self.source.find("Terminal Momentum intercept"):][:2000]
        self.assertIn("get_open_positions", tm_block)

    def test_concurrent_cap_check(self):
        """TM must check TM_MAX_CONCURRENT."""
        tm_block = self.source[self.source.find("Terminal Momentum intercept"):][:3000]
        self.assertIn("TM_MAX_CONCURRENT", tm_block)


class TestTMCandidateSeparation(unittest.TestCase):
    """Verify TM candidates are separated from main pipeline (like DC)."""

    def setUp(self):
        self.source = _read_bot()

    def test_tm_candidates_extracted(self):
        """TM candidates must be separated into _tm_candidates list."""
        self.assertIn('_tm_candidates = [c for c in candidates if c.get("strategy") == "terminal_momentum"]',
                       self.source)

    def test_tm_excluded_from_main(self):
        """TM candidates must be excluded from _main_candidates."""
        sep = self.source.find("Separate overlay candidates")
        self.assertGreater(sep, 0)
        sep_block = self.source[sep:sep + 800]
        self.assertIn('"terminal_momentum"', sep_block)

    def test_tm_appended_to_selected(self):
        """TM candidates must be appended to selected list."""
        self.assertIn("selected.extend(_tm_candidates)", self.source)


class TestTMExecuteRouting(unittest.TestCase):
    """Verify TM routes to its own taker function in execute()."""

    def setUp(self):
        self.source = _read_bot()

    def test_strategy_routing(self):
        """terminal_momentum strategy must route to _execute_tm_taker."""
        self.assertIn('_dc_strategy == "terminal_momentum"', self.source)
        self.assertIn("_execute_tm_taker", self.source)

    def test_tm_taker_function_exists(self):
        """_execute_tm_taker must be defined."""
        self.assertIn("def _execute_tm_taker(self, candidate", self.source)

    def test_tm_taker_sets_entry_path(self):
        """_execute_tm_taker must set entry_path for tracking."""
        # Find the function
        fn_start = self.source.find("def _execute_tm_taker")
        self.assertGreater(fn_start, 0)
        fn_block = self.source[fn_start:fn_start + 3000]
        self.assertIn('"tm_taker"', fn_block)

    def test_tm_taker_sets_cooldown(self):
        """_execute_tm_taker must set ticker cooldown."""
        fn_start = self.source.find("def _execute_tm_taker")
        fn_block = self.source[fn_start:fn_start + 3000]
        self.assertIn("_recent_taker_tickers", fn_block)

    def test_tm_taker_sends_telegram(self):
        """_execute_tm_taker must send Telegram alert on fill."""
        fn_start = self.source.find("def _execute_tm_taker")
        fn_end = self.source.find("\n    def ", fn_start + 1)
        fn_block = self.source[fn_start:fn_end]
        self.assertIn("_TELEGRAM.send", fn_block)
        self.assertIn("TM:", fn_block)


class TestTMSizing(unittest.TestCase):
    """Verify TM uses fixed sizing, not Kelly."""

    def setUp(self):
        self.source = _read_bot()

    def test_fixed_contracts_in_candidate(self):
        """TM candidate must use TM_FIXED_CONTRACTS for position_size."""
        tm_block = self.source[self.source.find("Terminal Momentum intercept"):][:6000]
        self.assertIn('"position_size": TM_FIXED_CONTRACTS', tm_block)

    def test_kelly_zero_in_candidate(self):
        """TM candidate must set kelly_f=0.0."""
        tm_block = self.source[self.source.find("Terminal Momentum intercept"):][:6000]
        self.assertIn('"kelly_f": 0.0', tm_block)

    def test_drawdown_scaler_one_in_candidate(self):
        """TM candidate must set drawdown_scaler=1.0 (not affected by drawdown)."""
        tm_block = self.source[self.source.find("Terminal Momentum intercept"):][:6000]
        self.assertIn('"drawdown_scaler": 1.0', tm_block)


class TestTMNoRetryQueue(unittest.TestCase):
    """Verify TM does NOT use DC retry queue."""

    def setUp(self):
        self.source = _read_bot()

    def test_no_retry_queue_in_tm_taker(self):
        """_execute_tm_taker must NOT reference dc_retry_queue."""
        fn_start = self.source.find("def _execute_tm_taker")
        fn_end = self.source.find("\n    def ", fn_start + 1)
        fn_block = self.source[fn_start:fn_end]
        self.assertNotIn("dc_retry_queue", fn_block)
        self.assertNotIn("retry_queue", fn_block)


if __name__ == "__main__":
    unittest.main()
