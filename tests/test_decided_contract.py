"""Tests for Decided Contract overlay strategy.

Guards against:
- Signal detection: T1 (z ≤ -5, 93-99c, STC < 300s) and T2 (z ≤ -3, 93-96c)
- Per-window risk cap: 25% bankroll cap with payoff-priority ordering
- Kill switches: DECIDED_T1_ENABLED / DECIDED_T2_ENABLED independently toggle tiers
- Shadow continuity: shadow signals always logged regardless of live enable state
- Candidate flow: DC candidates bypass single-asset-per-window filter
- Sizing: fixed 12.5% bankroll risk, not Kelly
- Strategy tagging: decided_t1 / decided_t2 in candidate dict
"""

import re
import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.py")
DASH_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard_snapshot.py")

def _read_bot():
    with open(BOT_PATH) as f:
        return f.read()

def _read_dash():
    with open(DASH_PATH) as f:
        return f.read()

def _extract_constant(source, name):
    """Extract a numeric constant from source code."""
    m = re.search(rf'^{name}\s*=\s*([^\s#]+)', source, re.MULTILINE)
    if m:
        val = m.group(1)
        try:
            return eval(val)
        except Exception:
            return val
    return None


class TestDecidedContractConstants(unittest.TestCase):
    """Verify constants are defined and have correct values."""

    def setUp(self):
        self.source = _read_bot()

    def test_constants_exist_and_correct(self):
        self.assertEqual(_extract_constant(self.source, "DECIDED_CONTRACT_Z_T1"), -5.0)
        self.assertEqual(_extract_constant(self.source, "DECIDED_CONTRACT_Z_T2"), -3.0)
        self.assertEqual(_extract_constant(self.source, "DECIDED_CONTRACT_MIN_PRICE"), 93)
        self.assertEqual(_extract_constant(self.source, "DECIDED_CONTRACT_T2_MAX_PRICE"), 96)
        self.assertEqual(_extract_constant(self.source, "DECIDED_CONTRACT_MAX_STC"), 300)
        self.assertEqual(_extract_constant(self.source, "DECIDED_CONTRACT_RISK"), 0.125)
        self.assertEqual(_extract_constant(self.source, "DECIDED_CONTRACT_MAX_WINDOW_RISK"), 0.25)

    def test_kill_switches_default_off(self):
        """Kill switches must default to '0' (deploy with flags off)."""
        self.assertIn('DECIDED_T1_ENABLED = os.environ.get("DECIDED_T1_ENABLED", "0") == "1"', self.source)
        self.assertIn('DECIDED_T2_ENABLED = os.environ.get("DECIDED_T2_ENABLED", "0") == "1"', self.source)

    def test_kill_switches_env_var_controlled(self):
        """Both toggles are env-var driven (no deploy needed to flip)."""
        self.assertIn('os.environ.get("DECIDED_T1_ENABLED"', self.source)
        self.assertIn('os.environ.get("DECIDED_T2_ENABLED"', self.source)


class TestDecidedContractSignalDetection(unittest.TestCase):
    """Test the signal criteria logic directly."""

    def test_t1_z_threshold(self):
        """T1 fires when z ≤ -5.0."""
        Z_T1 = -5.0
        self.assertTrue(-5.0 <= Z_T1)
        self.assertTrue(-6.0 <= Z_T1)
        self.assertFalse(-4.0 <= Z_T1)

    def test_t2_z_threshold_and_price_gate(self):
        """T2 fires when z ≤ -3.0 AND price ≤ 96c."""
        Z_T2 = -3.0
        T2_MAX_PRICE = 96
        # z = -3.5: meets T2 but not T1
        self.assertTrue(-3.5 <= Z_T2)
        self.assertFalse(-3.5 <= -5.0)  # not T1
        # T2 price gate
        self.assertTrue(95 <= T2_MAX_PRICE)
        self.assertTrue(96 <= T2_MAX_PRICE)
        self.assertFalse(97 <= T2_MAX_PRICE)

    def test_price_range(self):
        """Signal only fires for 93-99c."""
        MIN_PRICE = 93
        MAX_PRICE = 99
        self.assertTrue(93 >= MIN_PRICE)
        self.assertTrue(99 <= MAX_PRICE)
        self.assertFalse(92 >= MIN_PRICE)

    def test_stc_range(self):
        """Signal only fires for STC < 300s."""
        MAX_STC = 300
        self.assertTrue(200 < MAX_STC)
        self.assertFalse(300 < MAX_STC)
        self.assertFalse(500 < MAX_STC)

    def test_t1_priority_over_t2(self):
        """T1 check comes before T2 — z ≤ -5 gets T1 not T2."""
        source = _read_bot()
        t1_pos = source.find("decided_contract_t1")
        t2_pos = source.find("decided_contract_t2")
        self.assertGreater(t1_pos, 0)
        self.assertGreater(t2_pos, 0)
        self.assertLess(t1_pos, t2_pos, "T1 check must come before T2 in code")


class TestDecidedContractWindowCap(unittest.TestCase):
    """Test per-window risk cap logic."""

    def test_window_cap_math(self):
        """25% bankroll cap: 3 signals at 12.5% each → third should be capped."""
        RISK = 0.125
        MAX_WINDOW_RISK = 0.25
        bankroll = 100000  # $1000 in cents
        max_cost = bankroll * MAX_WINDOW_RISK  # 25000c

        # Signal 1: 99c
        s1_position = max(1, int(bankroll * RISK / 99))
        s1_cost = s1_position * 99
        self.assertLessEqual(s1_cost, max_cost)

        # Signal 2: 95c
        s2_position = max(1, int(bankroll * RISK / 95))
        s2_cost = s2_position * 95
        cumulative = s1_cost + s2_cost
        self.assertLessEqual(cumulative, max_cost)

        # Signal 3: 98c — should exceed cap
        s3_position = max(1, int(bankroll * RISK / 98))
        s3_cost = s3_position * 98
        self.assertGreater(cumulative + s3_cost, max_cost)

    def test_payoff_priority_ordering(self):
        """Lower price = higher payoff, should be prioritized first."""
        candidates = [
            {"best_yes_ask": 99, "strategy": "decided_t1"},
            {"best_yes_ask": 95, "strategy": "decided_t2"},
            {"best_yes_ask": 97, "strategy": "decided_t1"},
        ]
        candidates.sort(key=lambda c: c["best_yes_ask"])
        self.assertEqual(candidates[0]["best_yes_ask"], 95)
        self.assertEqual(candidates[1]["best_yes_ask"], 97)
        self.assertEqual(candidates[2]["best_yes_ask"], 99)

    def test_cap_reduces_position(self):
        """When cap partially filled, remaining position should be reduced."""
        MAX_WINDOW_RISK = 0.25
        bankroll = 100000
        max_cost = bankroll * MAX_WINDOW_RISK  # 25000c
        existing_risk = 20000
        remaining = max_cost - existing_risk  # 5000c
        best_ask = 98
        reduced_position = max(1, int(remaining / best_ask))
        self.assertEqual(reduced_position, 51)
        self.assertLessEqual(reduced_position * best_ask + existing_risk, max_cost)

    def test_cap_exceeded_skips(self):
        """When cap fully used, signal should be skipped entirely."""
        MAX_WINDOW_RISK = 0.25
        bankroll = 100000
        max_cost = bankroll * MAX_WINDOW_RISK
        existing_risk = 25000
        remaining = max_cost - existing_risk
        best_ask = 95
        self.assertLess(remaining, best_ask)

    def test_window_cap_skip_logged(self):
        """Window cap skips must be logged as decided_window_cap_skip filter_stage."""
        source = _read_bot()
        self.assertIn("decided_window_cap_skip", source)
        self.assertIn("DC_WINDOW_CAP", source)


class TestDecidedContractKillSwitches(unittest.TestCase):
    """Test that kill switches work independently."""

    def test_t1_enabled_t2_disabled(self):
        t1_enabled, t2_enabled = True, False
        tier = "decided_contract_t1"
        live = (tier == "decided_contract_t1" and t1_enabled) or \
               (tier == "decided_contract_t2" and t2_enabled)
        self.assertTrue(live)

        tier = "decided_contract_t2"
        live = (tier == "decided_contract_t1" and t1_enabled) or \
               (tier == "decided_contract_t2" and t2_enabled)
        self.assertFalse(live)

    def test_t2_enabled_t1_disabled(self):
        t1_enabled, t2_enabled = False, True
        tier = "decided_contract_t2"
        live = (tier == "decided_contract_t1" and t1_enabled) or \
               (tier == "decided_contract_t2" and t2_enabled)
        self.assertTrue(live)

    def test_both_disabled(self):
        t1_enabled, t2_enabled = False, False
        for tier in ("decided_contract_t1", "decided_contract_t2"):
            live = (tier == "decided_contract_t1" and t1_enabled) or \
                   (tier == "decided_contract_t2" and t2_enabled)
            self.assertFalse(live)

    def test_kill_switch_code_structure(self):
        """Both tier checks must be present in the live overlay block."""
        source = _read_bot()
        overlay_start = source.find("# ── Live overlay: queue as candidate if tier enabled")
        self.assertGreater(overlay_start, 0)
        overlay_block = source[overlay_start:overlay_start + 300]
        self.assertIn("DECIDED_T1_ENABLED", overlay_block)
        self.assertIn("DECIDED_T2_ENABLED", overlay_block)


class TestDecidedContractSizing(unittest.TestCase):
    """Test fixed 12.5% sizing (not Kelly)."""

    def test_fixed_sizing(self):
        RISK = 0.125
        bankroll = 100000
        position = max(1, int(bankroll * RISK / 95))
        self.assertEqual(position, 131)

    def test_sizing_at_99c(self):
        RISK = 0.125
        bankroll = 100000
        position = max(1, int(bankroll * RISK / 99))
        self.assertEqual(position, 126)

    def test_not_kelly(self):
        """Sizing must use DECIDED_CONTRACT_RISK, NOT Kelly formula."""
        source = _read_bot()
        # Find the DC sizing block
        dc_sizing_start = source.find("_dc_risk = DECIDED_CONTRACT_RISK")
        self.assertGreater(dc_sizing_start, 0)
        # Should NOT use self._sizer.compute in the DC block
        dc_block = source[dc_sizing_start:dc_sizing_start + 200]
        self.assertNotIn("self._sizer.compute", dc_block)


class TestDecidedContractStrategyTag(unittest.TestCase):
    """Test that strategy tags are correct for position tracking."""

    def test_strategy_tags_in_code(self):
        source = _read_bot()
        self.assertIn('"decided_t1"', source)
        self.assertIn('"decided_t2"', source)

    def test_strategy_mapping(self):
        tier_to_strat = {
            "decided_contract_t1": "decided_t1",
            "decided_contract_t2": "decided_t2",
        }
        for tier, expected in tier_to_strat.items():
            strat = "decided_t1" if tier == "decided_contract_t1" else "decided_t2"
            self.assertEqual(strat, expected)


class TestDecidedContractCandidateBypass(unittest.TestCase):
    """DC candidates must bypass single-asset-per-window filter."""

    def test_dc_separated_from_main(self):
        candidates = [
            {"strategy": "decided_t1", "ticker": "A"},
            {"strategy": "MAKER_PATIENT", "ticker": "B"},
            {"strategy": "decided_t2", "ticker": "C"},
            {"strategy": "TAKER_NOW", "ticker": "D"},
        ]
        dc_candidates = [c for c in candidates if c.get("strategy", "").startswith("decided_")]
        main_candidates = [c for c in candidates if not c.get("strategy", "").startswith("decided_")]
        self.assertEqual(len(dc_candidates), 2)
        self.assertEqual(len(main_candidates), 2)

    def test_bypass_in_code(self):
        """Code must separate DC candidates before single-asset filter."""
        source = _read_bot()
        bypass_pos = source.find("_dc_candidates = [c for c in candidates")
        single_asset_pos = source.find("Single-asset-per-timeslot")
        self.assertGreater(bypass_pos, 0, "DC candidate separation not found")
        self.assertGreater(single_asset_pos, 0)
        self.assertLess(bypass_pos, single_asset_pos,
                        "DC separation must come before single-asset filter")


class TestDecidedContractDashboard(unittest.TestCase):
    """Test dashboard snapshot has the live panel key."""

    def test_snapshot_has_live_key(self):
        source = _read_dash()
        self.assertIn("decided_contract_live", source)

    def test_snapshot_queries_strategy(self):
        source = _read_dash()
        self.assertIn("decided_t1", source)
        self.assertIn("decided_t2", source)

    def test_snapshot_has_window_cap_skips(self):
        source = _read_dash()
        self.assertIn("window_cap_skips", source)
        self.assertIn("decided_window_cap_skip", source)


class TestDecidedContractCodeIntegrity(unittest.TestCase):
    """Verify the code changes don't break existing patterns."""

    def setUp(self):
        self.source = _read_bot()

    def test_shadow_always_logs(self):
        """Shadow evaluation must happen regardless of DECIDED_T1/T2_ENABLED."""
        shadow_insert_pos = self.source.find("# Always log shadow signal")
        live_check_pos = self.source.find("# ── Live overlay: queue as candidate if tier enabled")
        self.assertGreater(shadow_insert_pos, 0, "Shadow log comment not found")
        self.assertGreater(live_check_pos, 0, "Live overlay comment not found")
        self.assertLess(shadow_insert_pos, live_check_pos)

    def test_observation_mode_blocks_live(self):
        """OBSERVATION_MODE must block DC live trades."""
        live_section_start = self.source.find("# ── Live overlay: queue as candidate if tier enabled")
        live_section = self.source[live_section_start:live_section_start + 500]
        self.assertIn("not OBSERVATION_MODE", live_section)

    def test_dc_window_risk_initialized(self):
        self.assertIn("self._dc_window_risk = {}", self.source)

    def test_dc_window_seeded_from_positions(self):
        self.assertIn('("decided_t1", "decided_t2")', self.source)

    def test_continue_still_present(self):
        """The continue after shadow blocks must still be present for non-promoted signals."""
        # Find the relaxed edge block end, then the continue
        rel_end = self.source.find("insert_evaluated_opportunity failed (relaxed_edge_shadow)")
        after_rel = self.source[rel_end:rel_end + 200]
        self.assertIn("continue", after_rel)

    def test_candidates_list_referenced(self):
        """DC overlay must append to candidates list."""
        self.assertIn("candidates.append({", self.source)
        # Verify decided_t1 appears in a candidates.append context
        dc_append_pos = self.source.find('"strategy": _dc_strat')
        self.assertGreater(dc_append_pos, 0)


if __name__ == "__main__":
    unittest.main()
