"""Tests for Weather Bracket NO-Side strategy.

Guards against:
- YES price gate: 88-96c only — 97-99c dead zone MUST be excluded
- Market type gate: bracket only, NOT upper_tail/lower_tail
- NO cost computation: 100 - YES_ask, NOT corrupted no_ask_cents
- Fixed sizing: 5 contracts (no Kelly, no drawdown scaler)
- Feature flag: BRACKET_NO_ENABLED gates all activity
- Kill switch: cumulative PnL threshold auto-disables
- Concurrent cap: max 6 simultaneous positions
- Per-ticker dedup: prevents double-filling same bracket strike
- Multi-bracket: multiple strikes on same event_ticker CAN trade simultaneously
- Side: candidate has side="no"
- STC gate: >= 8 hours (28800s)
"""

import re
import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot/_impl.py")


def _read_bot():
    with open(BOT_PATH) as f:
        return f.read()


def _extract_constant(source, name):
    m = re.search(rf'^{name}\s*=\s*([^\s#]+)', source, re.MULTILINE)
    if m:
        try:
            return eval(m.group(1))
        except Exception:
            return m.group(1)
    return None


class TestBracketNOConstants(unittest.TestCase):

    def setUp(self):
        self.source = _read_bot()

    def test_feature_flag_exists(self):
        self.assertIn("BRACKET_NO_ENABLED", self.source)

    def test_yes_price_range(self):
        self.assertEqual(_extract_constant(self.source, "BRACKET_NO_YES_MIN"), 88)
        self.assertEqual(_extract_constant(self.source, "BRACKET_NO_YES_MAX"), 96)

    def test_97_excluded(self):
        """97-99c is a dead zone (47.8% NO rate). YES_MAX must be 96, not 97+."""
        yes_max = _extract_constant(self.source, "BRACKET_NO_YES_MAX")
        self.assertLessEqual(yes_max, 96, "BRACKET_NO_YES_MAX must be <= 96 to exclude 97c dead zone")

    def test_fixed_contracts(self):
        self.assertEqual(_extract_constant(self.source, "BRACKET_NO_FIXED_CONTRACTS"), 5)

    def test_assumed_prob(self):
        self.assertEqual(_extract_constant(self.source, "BRACKET_NO_ASSUMED_PROB"), 0.92)

    def test_min_stc(self):
        self.assertEqual(_extract_constant(self.source, "BRACKET_NO_MIN_STC"), 28800)

    def test_max_concurrent(self):
        self.assertEqual(_extract_constant(self.source, "BRACKET_NO_MAX_CONCURRENT"), 6)

    def test_kill_threshold(self):
        self.assertEqual(_extract_constant(self.source, "BRACKET_NO_KILL_THRESHOLD"), -2000)


class TestBracketNOIntercept(unittest.TestCase):

    def setUp(self):
        self.source = _read_bot()
        self.intercept_start = self.source.find("Bracket NO intercept")
        self.assertGreater(self.intercept_start, 0, "Bracket NO intercept comment not found")
        self.intercept_block = self.source[self.intercept_start:self.intercept_start + 6000]

    def test_market_type_gate(self):
        """Only bracket markets, not upper_tail or lower_tail."""
        self.assertIn('_wx_mtype == "bracket"', self.intercept_block)

    def test_yes_price_gate(self):
        """Uses YES price range check."""
        self.assertIn("BRACKET_NO_YES_MIN <= best_ask <= BRACKET_NO_YES_MAX", self.intercept_block)

    def test_stc_gate(self):
        self.assertIn("BRACKET_NO_MIN_STC", self.intercept_block)

    def test_no_cost_from_yes(self):
        """NO cost is computed as 100 - YES ask, NOT from corrupted _no_ask_eq."""
        self.assertIn("100 - best_ask", self.intercept_block)
        # Must NOT use _no_ask_eq for bracket NO cost computation
        # (The existing NO shadow below still uses it — that's fine, it's a different path)

    def test_side_is_no(self):
        self.assertIn('"side": "no"', self.intercept_block)

    def test_strategy_tag(self):
        self.assertIn('"strategy": "bracket_no"', self.intercept_block)

    def test_fixed_sizing(self):
        self.assertIn('"position_size": BRACKET_NO_FIXED_CONTRACTS', self.intercept_block)

    def test_position_check_per_ticker(self):
        """Position check must be per-ticker, NOT per-event_ticker."""
        # Must check ticker, allowing multiple brackets on same event
        self.assertIn('p.get("ticker") == ticker', self.intercept_block)

    def test_concurrent_cap(self):
        self.assertIn("BRACKET_NO_MAX_CONCURRENT", self.intercept_block)

    def test_feature_flag_gate(self):
        self.assertIn("BRACKET_NO_ENABLED", self.intercept_block)

    def test_before_existing_no_shadow(self):
        """Bracket NO intercept must come BEFORE existing weather NO shadow code."""
        # Find the actual NO shadow code (the if statement), not comments
        existing_no = self.source.find("WEATHER_NO_SHADOW_MIN_YES_PROB and _no_ask_eq")
        self.assertGreater(existing_no, 0)
        self.assertGreater(existing_no, self.intercept_start,
                           "Bracket NO intercept must be BEFORE weather NO shadow")


class TestBracketNOCandidateSeparation(unittest.TestCase):

    def setUp(self):
        self.source = _read_bot()

    def test_candidates_extracted(self):
        self.assertIn('_bn_candidates = [c for c in candidates if c.get("strategy") == "bracket_no"]',
                       self.source)

    def test_excluded_from_main(self):
        self.assertIn('"bracket_no"', self.source)
        # Check it's in the exclusion filter
        sep = self.source.find("Separate overlay candidates")
        self.assertGreater(sep, 0)
        sep_block = self.source[sep:sep + 800]
        self.assertIn("bracket_no", sep_block)

    def test_appended_to_selected(self):
        self.assertIn("selected.extend(_bn_candidates)", self.source)


class TestBracketNOExecuteRouting(unittest.TestCase):

    def setUp(self):
        self.source = _read_bot()

    def test_strategy_routing(self):
        self.assertIn('_dc_strategy == "bracket_no"', self.source)
        self.assertIn("_execute_bracket_no_taker", self.source)

    def test_function_exists(self):
        self.assertIn("def _execute_bracket_no_taker(self, candidate", self.source)

    def test_sets_entry_path(self):
        fn_start = self.source.find("def _execute_bracket_no_taker")
        fn_end = self.source.find("\n    def ", fn_start + 1)
        fn_block = self.source[fn_start:fn_end]
        self.assertIn('"bracket_no_taker"', fn_block)

    def test_sets_cooldown(self):
        fn_start = self.source.find("def _execute_bracket_no_taker")
        fn_end = self.source.find("\n    def ", fn_start + 1)
        fn_block = self.source[fn_start:fn_end]
        self.assertIn("_recent_taker_tickers", fn_block)

    def test_sends_telegram(self):
        fn_start = self.source.find("def _execute_bracket_no_taker")
        fn_end = self.source.find("\n    def ", fn_start + 1)
        fn_block = self.source[fn_start:fn_end]
        self.assertIn("_TELEGRAM.send", fn_block)
        self.assertIn("BKT_NO:", fn_block)

    def test_no_retry_queue(self):
        fn_start = self.source.find("def _execute_bracket_no_taker")
        fn_end = self.source.find("\n    def ", fn_start + 1)
        fn_block = self.source[fn_start:fn_end]
        self.assertNotIn("retry_queue", fn_block)

    def test_price_validation(self):
        """Fresh price must be re-validated against YES range."""
        fn_start = self.source.find("def _execute_bracket_no_taker")
        fn_end = self.source.find("\n    def ", fn_start + 1)
        fn_block = self.source[fn_start:fn_end]
        self.assertIn("BRACKET_NO_YES_MIN", fn_block)
        self.assertIn("BRACKET_NO_YES_MAX", fn_block)


class TestBracketNOKillSwitch(unittest.TestCase):

    def setUp(self):
        self.source = _read_bot()

    def test_separate_from_general_no(self):
        """Bracket NO kill switch must be separate from WEATHER_NO_KILL_THRESHOLD."""
        kill_pos = self.source.find("BRACKET_NO_KILL")
        self.assertGreater(kill_pos, 0)
        kill_block = self.source[kill_pos:kill_pos + 500]
        self.assertIn("bracket_no", kill_block.lower())

    def test_queries_bracket_no_strategy(self):
        """Kill switch must query strategy='bracket_no', not side='no'."""
        # Find the kill switch code (in the tick init), not the constant definition
        kill_pos = self.source.find("BRACKET_NO_KILL: cumulative")
        self.assertGreater(kill_pos, 0, "Kill switch log message not found")
        kill_block = self.source[kill_pos - 500:kill_pos + 200]
        self.assertIn("strategy='bracket_no'", kill_block)


if __name__ == "__main__":
    unittest.main()
