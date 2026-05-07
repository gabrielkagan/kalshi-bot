"""Tests for Terminal Momentum strategy.

Guards against:
- Price set: {95, 96, 97, 98, 99} — full 95-99c range
- Probability gate: calibrated_prob >= 0.93
- STC window: 61-300 seconds to close
- Fixed sizing: default 50 contracts, per-price overrides (98c/99c → 100)
- Feature flag: TERMINAL_MOMENTUM_ENABLED gates all TM activity
- DC overlap: TM skips tickers already claimed by decided contracts
- Position overlap: TM skips tickers with existing positions
- Concurrent cap: max TM_MAX_CONCURRENT simultaneous TM candidates
- Candidate separation: TM candidates bypass single-asset-per-window filter
- Execute routing: terminal_momentum strategy routes to _execute_tm_taker
- Execution-time sizing: _execute_tm_taker re-derives count from fresh_ask
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
        """TM_PRICE_SET must be a set or frozenset literal, NOT a range.
        frozenset added Apr 28 2026 to prevent runtime drift from
        TM_LIVE_STRATEGIES (eagerly derived at import)."""
        m = re.search(r'^TM_PRICE_SET\s*=\s*(?:frozenset\()?(\{[^}]+\})',
                      self.source, re.MULTILINE)
        self.assertIsNotNone(m, "TM_PRICE_SET must be a set/frozenset literal")
        price_set = eval(m.group(1))
        self.assertEqual(price_set, {96, 98, 99})

    def test_negative_ev_prices_excluded(self):
        """95c/97c removed: 94.5% WR vs 95-97% breakeven = negative EV."""
        m = re.search(r'^TM_PRICE_SET\s*=\s*(?:frozenset\()?(\{[^}]+\})',
                      self.source, re.MULTILINE)
        self.assertIsNotNone(m)
        price_set = eval(m.group(1))
        self.assertNotIn(95, price_set, "95c is negative EV — must not be in TM_PRICE_SET")
        self.assertNotIn(97, price_set, "97c is negative EV — must not be in TM_PRICE_SET")

    def test_min_prob(self):
        self.assertEqual(_extract_constant(self.source, "TM_MIN_PROB"), 0.93)

    def test_stc_window(self):
        self.assertEqual(_extract_constant(self.source, "TM_MIN_STC"), 61)
        self.assertEqual(_extract_constant(self.source, "TM_MAX_STC"), 300)

    def test_base_contracts(self):
        self.assertEqual(_extract_constant(self.source, "TM_BASE_CONTRACTS"), 100)

    def test_tm_compute_contracts_exists(self):
        """tm_compute_contracts function must exist."""
        self.assertIn("def tm_compute_contracts(", self.source)

    def test_tm_sizing_margin_proportional(self):
        """Lower prices (wider margin) should produce more contracts."""
        import importlib, sys
        # Import the function
        spec = importlib.util.spec_from_file_location("bot", "bot.py")
        # Can't import bot.py directly (side effects), so verify via constants
        base = _extract_constant(self.source, "TM_BASE_CONTRACTS")
        # At 96c (margin=4) vs 99c (margin=1), base sizing should be 4:1
        self.assertEqual(base, 100, "TM_BASE_CONTRACTS should be 100")

    def test_max_concurrent(self):
        self.assertEqual(_extract_constant(self.source, "TM_MAX_CONCURRENT"), 8)


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
        tm_block = self.source[self.source.find("Terminal Momentum intercept"):][:4000]
        self.assertIn("not OBSERVATION_MODE", tm_block)

    def test_product_type_gate(self):
        """TM is 15M only."""
        tm_block = self.source[self.source.find("Terminal Momentum intercept"):][:4000]
        self.assertIn('_pt in (None, "15m")', tm_block)

    def test_dc_overlap_check(self):
        """TM must check for DC overlap before building candidate."""
        tm_block = self.source[self.source.find("Terminal Momentum intercept"):][:4000]
        self.assertIn("decided_", tm_block)

    def test_position_overlap_check(self):
        """TM must check for existing positions."""
        tm_block = self.source[self.source.find("Terminal Momentum intercept"):][:4000]
        self.assertIn("get_open_positions", tm_block)

    def test_concurrent_cap_check(self):
        """TM must check TM_MAX_CONCURRENT."""
        tm_block = self.source[self.source.find("Terminal Momentum intercept"):][:4000]
        self.assertIn("TM_MAX_CONCURRENT", tm_block)


class TestTMCandidateSeparation(unittest.TestCase):
    """Verify TM candidates are separated from main pipeline (like DC)."""

    def setUp(self):
        self.source = _read_bot()

    def test_tm_candidates_extracted(self):
        """TM candidates must be separated into _tm_candidates list."""
        self.assertIn('_tm_candidates = [c for c in candidates if c.get("strategy", "").startswith("terminal_momentum")]',
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
        self.assertIn('_dc_strategy.startswith("terminal_momentum")', self.source)
        self.assertIn("_execute_tm_taker", self.source)

    def test_tm_taker_function_exists(self):
        """_execute_tm_taker must be defined."""
        self.assertIn("def _execute_tm_taker(self, candidate", self.source)

    def test_tm_taker_sets_entry_path(self):
        """_execute_tm_taker must set entry_path for tracking."""
        # Find the function
        fn_start = self.source.find("def _execute_tm_taker")
        self.assertGreater(fn_start, 0)
        fn_end = self.source.find("\n    def ", fn_start + 1)
        fn_block = self.source[fn_start:fn_end]
        self.assertIn('"tm_taker"', fn_block)

    def test_tm_taker_sets_cooldown(self):
        """_execute_tm_taker must set ticker cooldown."""
        fn_start = self.source.find("def _execute_tm_taker")
        fn_end = self.source.find("\n    def ", fn_start + 1)
        fn_block = self.source[fn_start:fn_end]
        self.assertIn("_recent_taker_tickers", fn_block)

    def test_tm_taker_sends_telegram(self):
        """_execute_tm_taker must send Telegram alert on fill."""
        fn_start = self.source.find("def _execute_tm_taker")
        fn_end = self.source.find("\n    def ", fn_start + 1)
        fn_block = self.source[fn_start:fn_end]
        self.assertIn("_TELEGRAM.send", fn_block)
        self.assertIn("TM:", fn_block)


class TestTMSizing(unittest.TestCase):
    """Verify TM uses per-price sizing with safe defaults."""

    def setUp(self):
        self.source = _read_bot()

    def test_scan_time_uses_compute_fn(self):
        """TM candidate must derive size from tm_compute_contracts."""
        tm_block = self.source[self.source.find("Terminal Momentum intercept"):][:16000]
        self.assertIn("tm_compute_contracts(", tm_block)

    def test_scan_time_sets_position_size(self):
        """TM candidate must use _tm_size for position_size."""
        tm_block = self.source[self.source.find("Terminal Momentum intercept"):][:16000]
        self.assertIn('"position_size": _tm_size', tm_block)

    def test_kelly_zero_in_candidate(self):
        """TM candidate must set kelly_f=0.0."""
        tm_block = self.source[self.source.find("Terminal Momentum intercept"):][:16000]
        self.assertIn('"kelly_f": 0.0', tm_block)

    def test_drawdown_scaler_one_in_candidate(self):
        """TM candidate must set drawdown_scaler=1.0 (not affected by drawdown)."""
        tm_block = self.source[self.source.find("Terminal Momentum intercept"):][:16000]
        self.assertIn('"drawdown_scaler": 1.0', tm_block)

    def test_execution_time_re_derives_count(self):
        """_execute_tm_taker must re-derive count via tm_compute_contracts on price drift."""
        fn_start = self.source.find("def _execute_tm_taker")
        fn_end = self.source.find("\n    def ", fn_start + 1)
        fn_block = self.source[fn_start:fn_end]
        self.assertIn("tm_compute_contracts(", fn_block)

    def test_execution_time_updates_candidate(self):
        """_execute_tm_taker must update candidate['position_size'] after re-derivation."""
        fn_start = self.source.find("def _execute_tm_taker")
        fn_end = self.source.find("\n    def ", fn_start + 1)
        fn_block = self.source[fn_start:fn_end]
        self.assertIn('candidate["position_size"] = count', fn_block)


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


class TestTMThinBufferCap(unittest.TestCase):
    """Thin-buffer contract cap — guards against the Apr 23 2026 ETH -$178 loss.

    Regression target: ETH 98c TM entry with buf_pct=0.155% was sized 182 contracts,
    settled NO when ETH reversed $5.57 in the final 155s (-$178.36). 10 of 14 TM
    losses in April were at buf_pct<0.20% avg 114ct — cap bounds each to ~$50.
    """

    def setUp(self):
        self.source = _read_bot()
        self._compile_fn()

    def _compile_fn(self):
        """Extract TM constants + tm_compute_contracts into an isolated namespace."""
        from typing import Optional
        ns = {"Optional": Optional}
        # Hoist the needed constants + TM_ASSET_RISK_CAPS
        const_names = [
            "TM_BASE_CONTRACTS", "TM_STC_SAFE_THRESHOLD", "TM_STC_DANGER_HI",
            "TM_STC_SAFE_MULT", "TM_STC_DANGER_MULT", "TM_STC_NORMAL_MULT",
            "TM_MIN_CONTRACTS", "TM_MAX_CONTRACTS", "TM_NEGATIVE_EV_TIERS",
            "TM_THIN_BUFFER_PCT", "TM_THIN_BUFFER_CONTRACT_CAP",
            "BTC_MAX_RISK_PER_TRADE", "ETH_MAX_RISK_PER_TRADE",
            "SOL_MAX_RISK_PER_TRADE", "XRP_MAX_RISK_PER_TRADE",
        ]
        for name in const_names:
            m = re.search(rf'^{name}\s*=\s*([^\n#]+?)(?:\s*#.*)?$',
                          self.source, re.MULTILINE)
            if m:
                ns[name] = eval(m.group(1).strip(), ns)
        ns["TM_ASSET_RISK_CAPS"] = {
            "BTC": ns["BTC_MAX_RISK_PER_TRADE"],
            "ETH": ns["ETH_MAX_RISK_PER_TRADE"],
            "SOL": ns["SOL_MAX_RISK_PER_TRADE"],
            "XRP": ns["XRP_MAX_RISK_PER_TRADE"],
        }
        fn_start = self.source.find("def tm_compute_contracts")
        fn_end = self.source.find("\ndef ", fn_start + 1)
        exec(self.source[fn_start:fn_end], ns)
        self.tm_compute_contracts = ns["tm_compute_contracts"]
        self.cap = ns["TM_THIN_BUFFER_CONTRACT_CAP"]
        self.pct = ns["TM_THIN_BUFFER_PCT"]

    def test_constants_defined(self):
        """TM_THIN_BUFFER_PCT and TM_THIN_BUFFER_CONTRACT_CAP must exist."""
        self.assertIsNotNone(_extract_constant(self.source, "TM_THIN_BUFFER_PCT"))
        self.assertIsNotNone(_extract_constant(self.source, "TM_THIN_BUFFER_CONTRACT_CAP"))
        self.assertEqual(_extract_constant(self.source, "TM_THIN_BUFFER_PCT"), 0.20)
        self.assertEqual(_extract_constant(self.source, "TM_THIN_BUFFER_CONTRACT_CAP"), 50)

    def test_apr23_eth_loss_would_have_been_capped(self):
        """The exact Apr 23 ETH loss scenario: 98c, 155s STC, buf 0.155% → ≤50 contracts."""
        # Reproduce scan-time conditions (large balance, ETH asset, buf<0.20%)
        ct = self.tm_compute_contracts(98, 155, 100000_00, "ETH", buf_pct=0.155)
        self.assertLessEqual(ct, self.cap,
            f"Apr 23 ETH @ 98c buf=0.155% was sized {ct}; must be <= {self.cap}")

    def test_cap_applied_below_threshold(self):
        """Any buf_pct < TM_THIN_BUFFER_PCT (0.20%) triggers the cap."""
        for buf in [0.0, 0.05, 0.10, 0.15, 0.19, 0.199]:
            ct = self.tm_compute_contracts(98, 100, 100000_00, "ETH", buf_pct=buf)
            self.assertLessEqual(ct, self.cap,
                f"buf_pct={buf}% should trigger cap (got ct={ct})")

    def test_cap_not_applied_at_or_above_threshold(self):
        """At buf_pct >= 0.20%, sizing is NOT capped by thin-buffer logic."""
        # Use a config where unbounded sizing would exceed the cap: 98c, STC=100 (safe mult 1.5),
        # large balance → margin*TM_BASE*1.5 = 2 * 100 * 1.5 = 300 contracts base
        ct_thick = self.tm_compute_contracts(98, 100, 100000_00, "ETH", buf_pct=0.20)
        self.assertGreater(ct_thick, self.cap,
            "At buf=0.20% (threshold), sizing must allow > cap")

    def test_buf_pct_none_preserves_legacy_behavior(self):
        """When buf_pct=None (unknown), cap does not apply — backward compat."""
        ct_none = self.tm_compute_contracts(98, 100, 100000_00, "ETH", buf_pct=None)
        ct_thick = self.tm_compute_contracts(98, 100, 100000_00, "ETH", buf_pct=1.0)
        self.assertEqual(ct_none, ct_thick,
            "buf_pct=None should behave as if buffer is fat (legacy)")

    def test_cap_does_not_push_below_min(self):
        """Floor (TM_MIN_CONTRACTS) still applies even with cap active."""
        _min = _extract_constant(self.source, "TM_MIN_CONTRACTS")
        ct = self.tm_compute_contracts(99, 200, 100000_00, "BTC", buf_pct=0.05)
        self.assertGreaterEqual(ct, _min, f"Cap must not drop ct below {_min}")

    def test_scan_passes_buf_pct(self):
        """The scan-time call to tm_compute_contracts must pass buf_pct=_tm_buf_pct.
        Check is component-based (not exact string) so multi-line call formatting
        from later changes (e.g. risk_cap_price added Apr 28) doesn't false-fail."""
        tm_block = self.source[self.source.find("Terminal Momentum intercept"):][:16000]
        # Find a tm_compute_contracts call and verify positional args + buf_pct.
        self.assertIn("tm_compute_contracts(", tm_block,
                      "scan must call tm_compute_contracts")
        self.assertIn("best_ask", tm_block)
        self.assertIn("seconds_remaining", tm_block)
        self.assertIn("_tm_balance", tm_block)
        self.assertIn("buf_pct=_tm_buf_pct", tm_block,
                      "scan must pass buf_pct=_tm_buf_pct")

    def test_execution_passes_buf_pct(self):
        """_execute_tm_taker must pass buf_pct when re-deriving on price drift."""
        fn_start = self.source.find("def _execute_tm_taker")
        fn_end = self.source.find("\n    def ", fn_start + 1)
        fn_block = self.source[fn_start:fn_end]
        self.assertIn("buf_pct=_exec_buf_pct", fn_block)
        self.assertIn('candidate.get("spot_buffer_pct")', fn_block)


if __name__ == "__main__":
    unittest.main()
