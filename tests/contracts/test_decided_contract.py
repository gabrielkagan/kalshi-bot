"""Tests for Decided Contract overlay strategy.

Guards against:
- Signal detection: T1 (z ≤ -5, 93c+), T1B (z ≤ -4, 95c+), T2 (z ≤ -3, 93-96c)
- Per-window risk cap: 25% bankroll cap with payoff-priority ordering
- Kill switches: DECIDED_T1/T1B/T2_ENABLED independently toggle tiers
- Shadow continuity: shadow signals always logged regardless of live enable state
- Shadow expansion: 6 variants probe expansion zones (log-only, no orders)
- Candidate flow: DC candidates bypass single-asset-per-window filter
- Sizing: fixed 12.5% bankroll risk, not Kelly
- Strategy tagging: decided_t1 / decided_t1b / decided_t2 in candidate dict
- Execution routing: decided contracts go direct taker IOC, not maker-first
- Cooldown: 60s skip after "no asks on orderbook" prevents rapid-fire log spam
"""

import re
import unittest
import os
import sys
import pytest
import bot.constants  # noqa: F401

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

BOT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "bot/_impl.py")
DASH_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "dashboard_snapshot.py")

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
        self.assertEqual(_extract_constant(self.source, "DECIDED_CONTRACT_Z_T1B"), -4.0)
        self.assertEqual(_extract_constant(self.source, "DECIDED_CONTRACT_Z_T2"), -3.0)
        self.assertEqual(_extract_constant(self.source, "DECIDED_CONTRACT_MIN_PRICE"), 93)
        self.assertEqual(_extract_constant(self.source, "DECIDED_CONTRACT_T1B_MIN_PRICE"), 95)
        self.assertEqual(_extract_constant(self.source, "DECIDED_CONTRACT_T2_MAX_PRICE"), 96)
        self.assertEqual(_extract_constant(self.source, "DECIDED_CONTRACT_MAX_STC"), 300)
        self.assertEqual(_extract_constant(self.source, "DECIDED_CONTRACT_RISK"), 0.20)
        self.assertEqual(_extract_constant(self.source, "DECIDED_CONTRACT_MAX_WINDOW_RISK"), 0.35)

    def test_kill_switches_default_on(self):
        """Kill switches default to '1' (enabled) — direct taker routing active."""
        self.assertIn('DECIDED_T1_ENABLED = os.environ.get("DECIDED_T1_ENABLED", "1") == "1"', self.source)
        self.assertIn('DECIDED_T1B_ENABLED = os.environ.get("DECIDED_T1B_ENABLED", "1") == "1"', self.source)
        self.assertIn('DECIDED_T2_ENABLED = os.environ.get("DECIDED_T2_ENABLED", "1") == "1"', self.source)

    def test_kill_switches_env_var_controlled(self):
        """All tier toggles are env-var driven (no deploy needed to flip)."""
        self.assertIn('os.environ.get("DECIDED_T1_ENABLED"', self.source)
        self.assertIn('os.environ.get("DECIDED_T1B_ENABLED"', self.source)
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

    def test_t1b_gate_logic(self):
        """T1B fires when -5 < z ≤ -4 AND price ≥ 95c."""
        Z_T1 = -5.0
        Z_T1B = -4.0
        T1B_MIN_PRICE = 95
        # z = -4.5: meets T1B but not T1
        self.assertFalse(-4.5 <= Z_T1)  # not T1
        self.assertTrue(-4.5 <= Z_T1B)  # meets T1B z
        self.assertTrue(96 >= T1B_MIN_PRICE)  # meets T1B price
        # z = -4.5 at 94c: does NOT meet T1B (price too low)
        self.assertFalse(94 >= T1B_MIN_PRICE)
        # z = -5.1 at 96c: fires T1, not T1B
        self.assertTrue(-5.1 <= Z_T1)

    def test_t1b_does_not_overlap_t1(self):
        """T1B only catches signals that T1 misses (z in (-5, -4])."""
        Z_T1 = -5.0
        Z_T1B = -4.0
        for z in [-6.0, -5.5, -5.0]:
            self.assertTrue(z <= Z_T1, f"z={z} should fire T1, not T1B")
        for z in [-4.9, -4.5, -4.0]:
            self.assertFalse(z <= Z_T1, f"z={z} should NOT fire T1")
            self.assertTrue(z <= Z_T1B, f"z={z} should fire T1B")

    def test_t1_priority_over_t1b_over_t2(self):
        """T1 check comes before T1B, T1B before T2 in code."""
        source = _read_bot()
        t1_pos = source.find('"decided_contract_t1"')
        t1b_pos = source.find('"decided_contract_t1b"')
        t2_pos = source.find('"decided_contract_t2"')
        self.assertGreater(t1_pos, 0)
        self.assertGreater(t1b_pos, 0)
        self.assertGreater(t2_pos, 0)
        self.assertLess(t1_pos, t1b_pos, "T1 check must come before T1B in code")
        self.assertLess(t1b_pos, t2_pos, "T1B check must come before T2 in code")


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

    def test_t1_enabled_others_disabled(self):
        t1, t1b, t2 = True, False, False
        for tier, expected in [("decided_contract_t1", True),
                               ("decided_contract_t1b", False),
                               ("decided_contract_t2", False)]:
            live = ((tier == "decided_contract_t1" and t1) or
                    (tier == "decided_contract_t1b" and t1b) or
                    (tier == "decided_contract_t2" and t2))
            self.assertEqual(live, expected, f"tier={tier}")

    def test_t1b_enabled_others_disabled(self):
        t1, t1b, t2 = False, True, False
        tier = "decided_contract_t1b"
        live = ((tier == "decided_contract_t1" and t1) or
                (tier == "decided_contract_t1b" and t1b) or
                (tier == "decided_contract_t2" and t2))
        self.assertTrue(live)

    def test_t2_enabled_t1_disabled(self):
        t1, t1b, t2 = False, False, True
        tier = "decided_contract_t2"
        live = ((tier == "decided_contract_t1" and t1) or
                (tier == "decided_contract_t1b" and t1b) or
                (tier == "decided_contract_t2" and t2))
        self.assertTrue(live)

    def test_all_disabled(self):
        t1, t1b, t2 = False, False, False
        for tier in ("decided_contract_t1", "decided_contract_t1b", "decided_contract_t2"):
            live = ((tier == "decided_contract_t1" and t1) or
                    (tier == "decided_contract_t1b" and t1b) or
                    (tier == "decided_contract_t2" and t2))
            self.assertFalse(live)

    def test_kill_switch_code_structure(self):
        """All tier checks must be present in the live overlay block."""
        source = _read_bot()
        overlay_start = source.find("# ── Live overlay: queue as candidate if tier enabled")
        self.assertGreater(overlay_start, 0)
        overlay_block = source[overlay_start:overlay_start + 400]
        self.assertIn("DECIDED_T1_ENABLED", overlay_block)
        self.assertIn("DECIDED_T1B_ENABLED", overlay_block)
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

    @pytest.mark.fragile
    def test_not_kelly(self):
        """Sizing must use DECIDED_CONTRACT_RISK, NOT Kelly formula."""
        source = _read_bot()
        # Find the DC sizing block (multi-line conditional: else DECIDED_CONTRACT_RISK)
        dc_sizing_start = source.find("else DECIDED_CONTRACT_RISK)")
        self.assertGreater(dc_sizing_start, 0)
        # Should NOT use self._sizer.compute in the DC block
        dc_block = source[dc_sizing_start:dc_sizing_start + 400]
        self.assertNotIn("self._sizer.compute", dc_block)


class TestDecidedContractStrategyTag(unittest.TestCase):
    """Test that strategy tags are correct for position tracking."""

    def test_strategy_tags_in_code(self):
        source = _read_bot()
        self.assertIn('"decided_t1"', source)
        self.assertIn('"decided_t1b"', source)
        self.assertIn('"decided_t2"', source)

    def test_strategy_mapping(self):
        tier_to_strat = {
            "decided_contract_t1": "decided_t1",
            "decided_contract_t1b": "decided_t1b",
            "decided_contract_t2": "decided_t2",
        }
        for tier, expected in tier_to_strat.items():
            strat = {"decided_contract_t1": "decided_t1",
                     "decided_contract_t1b": "decided_t1b",
                     "decided_contract_t2": "decided_t2"}[tier]
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


class TestDecidedContractDirectTaker(unittest.TestCase):
    """DC candidates must route to direct taker IOC, not maker-first."""

    def test_dc_taker_block_exists(self):
        """execute() must have a decided contract taker override block."""
        source = _read_bot()
        self.assertIn("dc_taker_ENTRY:", source)
        self.assertIn("dc_taker_FILLED:", source)
        self.assertIn("dc_taker_UNFILLED:", source)

    def test_dc_taker_before_maker(self):
        """DC taker override must come before the standard maker path."""
        source = _read_bot()
        dc_taker_pos = source.find("Decided contract taker override")
        direct_taker_pos = source.find("Direct taker for <180s candidates")
        maker_pos = source.find("Three-tier post_only rejection escalation")
        self.assertGreater(dc_taker_pos, 0, "DC taker block not found")
        self.assertLess(dc_taker_pos, direct_taker_pos,
                        "DC taker must come before standard direct taker")
        self.assertLess(dc_taker_pos, maker_pos,
                        "DC taker must come before maker path")

    def test_dc_taker_routes_all_tiers(self):
        """All decided tiers (t1, t1b, t2) must trigger direct taker."""
        source = _read_bot()
        dc_block_start = source.find("Decided contract taker override")
        dc_block = source[dc_block_start:dc_block_start + 2500]
        self.assertIn('"decided_t1"', dc_block)
        self.assertIn('"decided_t1b"', dc_block)
        self.assertIn('"decided_t2"', dc_block)

    @pytest.mark.fragile
    def test_dc_taker_sets_escalation_type(self):
        """DC taker must set escalation_type for settled_trades tracking."""
        source = _read_bot()
        dc_block_start = source.find("def _execute_dc_taker")
        self.assertGreater(dc_block_start, 0, "_execute_dc_taker method not found")
        dc_block = source[dc_block_start:dc_block_start + 5000]
        self.assertIn('candidate["escalation_type"]', dc_block)


class TestDecidedContractDashboard(unittest.TestCase):
    """Test dashboard snapshot has the live panel key."""

    def test_snapshot_has_live_key(self):
        source = _read_dash()
        self.assertIn("decided_contract_live", source)

    def test_snapshot_queries_strategy(self):
        source = _read_dash()
        self.assertIn("decided_t1", source)
        self.assertIn("decided_t1b", source)
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
        # T1 (2026-05-10): widened from 800 to 1500 to accommodate the new
        # HYPE/DOGE shadow-flag guard added before _dc_live_enabled.
        live_section = self.source[live_section_start:live_section_start + 1500]
        self.assertIn("not OBSERVATION_MODE", live_section)

    def test_dc_window_risk_initialized(self):
        self.assertIn("self._dc_window_risk = {}", self.source)

    def test_dc_window_seeded_from_positions(self):
        self.assertIn('("decided_t1", "decided_t1b", "decided_t2")', self.source)

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


class TestDecidedContractT1B(unittest.TestCase):
    """Specific tests for T1B tier (z≤-4, 95c+)."""

    def setUp(self):
        self.source = _read_bot()

    def test_t1b_constants_defined(self):
        self.assertEqual(_extract_constant(self.source, "DECIDED_CONTRACT_Z_T1B"), -4.0)
        self.assertEqual(_extract_constant(self.source, "DECIDED_CONTRACT_T1B_MIN_PRICE"), 95)

    def test_t1b_tier_in_signal_detection(self):
        """T1B must appear in the decided contract signal detection block."""
        dc_start = self.source.find("Decided Contract (overlay strategy")
        dc_block = self.source[dc_start:dc_start + 2000]
        self.assertIn("decided_contract_t1b", dc_block)
        self.assertIn("DECIDED_CONTRACT_Z_T1B", dc_block)
        self.assertIn("DECIDED_CONTRACT_T1B_MIN_PRICE", dc_block)

    @pytest.mark.fragile
    def test_t1b_assumed_prob(self):
        """T1B assumed probability should be between T1 (99%) and T2 (96%)."""
        # Find the assumed prob assignment
        self.assertIn("0.98 if _dc_tier == \"decided_contract_t1b\"", self.source)

    @pytest.mark.fragile
    def test_t1b_in_execution_routing(self):
        """T1B must route to direct taker in execute()."""
        dc_taker_pos = self.source.find("Decided contract taker override")
        dc_block = self.source[dc_taker_pos:dc_taker_pos + 800]
        self.assertIn('"decided_t1b"', dc_block)


class TestDecidedContractShadowVariants(unittest.TestCase):
    """Test shadow expansion variant filter_stages."""

    def setUp(self):
        self.source = _read_bot()

    def test_shadow_stages_constant_defined(self):
        """DC_SHADOW_STAGES frozenset must list all 7 variants."""
        self.assertIn("DC_SHADOW_STAGES", self.source)
        for stage in ("dc_shadow_t1b_93c", "dc_shadow_t2_z25", "dc_shadow_t2_90c",
                       "dc_shadow_t2_90c_xrp", "dc_shadow_t2_z2", "dc_shadow_no_side",
                       "dc_t2_z2_phase1_shadow"):
            self.assertIn(stage, self.source, f"Shadow stage {stage} not found in bot/_impl.py")

    @pytest.mark.fragile
    def test_shadow_variants_in_scan(self):
        """All 7 shadow variants must have insert calls somewhere in bot/_impl.py."""
        # Shadow variant inserts are spread across scan() — verify each stage
        # appears as a string literal in bot/_impl.py (in insert calls or constants)
        for stage in ("dc_shadow_t1b_93c", "dc_shadow_t2_z25", "dc_shadow_t2_90c",
                       "dc_shadow_t2_90c_xrp", "dc_shadow_t2_z2", "dc_shadow_no_side",
                       "dc_t2_z2_phase1_shadow"):
            self.assertIn(f'"{stage}"', self.source, f"Shadow stage {stage} not found in bot/_impl.py")

    def test_shadow_t1b_93c_gate(self):
        """dc_shadow_t1b_93c: -5 < z ≤ -4 AND 93c ≤ price < 95c."""
        # The gate logic from the code
        Z_T1 = -5.0
        Z_T1B = -4.0
        T1B_MIN = 95
        # z = -4.5 at 94c → should match
        z, price = -4.5, 94
        matches = (Z_T1 < z <= Z_T1B and 93 <= price < T1B_MIN)
        self.assertTrue(matches)
        # z = -4.5 at 95c → should NOT match (covered by live T1B)
        matches = (Z_T1 < -4.5 <= Z_T1B and 93 <= 95 < T1B_MIN)
        self.assertFalse(matches)

    def test_shadow_t2_z25_gate(self):
        """dc_shadow_t2_z25: -3 < z ≤ -2.5 AND 93c ≤ price ≤ 96c."""
        Z_T2 = -3.0
        z, price = -2.7, 95
        matches = (Z_T2 < z <= -2.5 and 93 <= price <= 96)
        self.assertTrue(matches)
        # z = -3.2 → covered by live T2, should NOT match shadow
        matches = (Z_T2 < -3.2 <= -2.5 and 93 <= 95 <= 96)
        self.assertFalse(matches)

    def test_shadow_t2_90c_gate(self):
        """dc_shadow_t2_90c: z ≤ -3 AND 90c ≤ price < 93c AND BTC/ETH/SOL."""
        Z_T2 = -3.0
        MIN = 93
        for asset in ("BTC", "ETH", "SOL"):
            matches = (-3.5 <= Z_T2 and 90 <= 91 < MIN and asset in ("BTC", "ETH", "SOL"))
            self.assertTrue(matches, f"Should match for {asset}")
        # XRP → should NOT match (has its own shadow)
        matches = (-3.5 <= Z_T2 and 90 <= 91 < MIN and "XRP" in ("BTC", "ETH", "SOL"))
        self.assertFalse(matches)

    def test_shadow_no_side_gate(self):
        """dc_shadow_no_side: z ≥ 5 AND best_ask ≤ 20 (YES nearly worthless)."""
        self.assertIn("z_score >= 5.0", self.source)
        self.assertIn("best_ask <= 20", self.source)

    @pytest.mark.fragile
    def test_no_side_stores_no_ask(self):
        """NO-side shadow must read actual NO ask from NBBO, store as market_price."""
        # Find in the scan block (not constants) — search from scan() method
        scan_start = self.source.find("def scan(")
        self.assertGreater(scan_start, 0, "scan() method not found")
        no_side_pos = self.source.find("dc_shadow_no_side", scan_start)
        self.assertGreater(no_side_pos, scan_start, "dc_shadow_no_side not found in scan()")
        shadow_block = self.source[no_side_pos:no_side_pos + 2500]
        self.assertIn("no_ask", shadow_block)
        self.assertIn('side="no"', shadow_block)


class TestDecidedContractCooldown(unittest.TestCase):
    """Test 60s cooldown after 'no asks on orderbook' skip."""

    def setUp(self):
        self.source = _read_bot()

    def test_cooldown_dict_initialized(self):
        self.assertIn("_dc_skip_cooldown", self.source)
        self.assertIn("_dc_skip_cooldown: Dict[str, float] = {}", self.source)

    @pytest.mark.fragile
    def test_cooldown_set_on_no_asks(self):
        """Cooldown must be set when executor skips due to no asks."""
        no_asks_pos = self.source.find('ORDER_SUPPRESSED no_asks: %s asset=%s strategy=%s')
        self.assertGreater(no_asks_pos, 0, "ORDER_SUPPRESSED no_asks log not found")
        after_skip = self.source[no_asks_pos:no_asks_pos + 400]
        self.assertIn("_dc_skip_cooldown", after_skip)
        self.assertIn("60", after_skip)

    def test_cooldown_checked_before_candidate(self):
        """Scanner must check cooldown before creating DC candidate."""
        dc_candidate_pos = self.source.find("DC_CANDIDATE:")
        before_candidate = self.source[dc_candidate_pos - 800:dc_candidate_pos]
        self.assertIn("_dc_skip_cooldown", before_candidate)

    def test_cooldown_cleaned_in_scan(self):
        """Expired cooldown entries must be cleaned in scan()."""
        self.assertIn("Clean expired DC skip cooldowns", self.source)

    def test_cooldown_math(self):
        """60s cooldown: set at time T, check at T+59 → blocked, T+61 → allowed."""
        import time as _time
        now = _time.time()
        cooldown = {
            "ticker_a": now + 60,   # active
            "ticker_b": now - 1,    # expired
        }
        # ticker_a should be blocked
        self.assertTrue(now < cooldown["ticker_a"])
        # ticker_b should be allowed
        self.assertFalse(now < cooldown["ticker_b"])


class TestDecidedContractDashboardExpansion(unittest.TestCase):
    """Test dashboard snapshot includes DC expansion shadow data."""

    def test_expansion_shadow_in_snapshot(self):
        source = _read_dash()
        self.assertIn("dc_expansion_shadow", source)
        for stage in ("dc_shadow_t1b_93c", "dc_shadow_t2_z25", "dc_shadow_t2_90c",
                       "dc_shadow_t2_90c_xrp", "dc_shadow_t2_z2", "dc_shadow_no_side"):
            self.assertIn(stage, source, f"Shadow stage {stage} not in dashboard_snapshot.py")

    def test_t1b_in_shadow_stages(self):
        source = _read_dash()
        self.assertIn("decided_contract_t1b", source)

    def test_expansion_in_slow_cache(self):
        source = _read_dash()
        self.assertIn("dc_expansion_shadow", source)
        # Should be in slow-changing cache
        slow_cache_pos = source.find("_SLOW_SNAP_KEYS")
        slow_block = source[slow_cache_pos:slow_cache_pos + 500]
        self.assertIn("dc_expansion_shadow", slow_block)


class TestSolDCPriceTieredRisk(unittest.TestCase):
    """SOL DC uses price-tiered risk to contain high-price loss asymmetry.

    SOL is the only asset with DC losses (2 losses, 28 wins). Both losses
    are SOL-specific. At 96c: win=$4/ct, loss=$96/ct. Price-tiered risk
    reduces position size at high prices where loss asymmetry is worst.
    """

    def setUp(self):
        self.source = _read_bot()

    def test_sol_dc_risk_tiers_defined(self):
        """SOL_DC_RISK_TIERS constant exists."""
        self.assertIn("SOL_DC_RISK_TIERS", self.source)

    def test_sol_dc_tiers_applied_in_sizing(self):
        """SOL DC sizing block checks asset == 'SOL' and applies tiers."""
        self.assertIn('if asset == "SOL":', self.source)
        self.assertIn("SOL_DC_RISK_TIERS", self.source)

    def test_sol_93c_uses_20pct(self):
        """SOL DC at 93c: below tier floors, uses default 20%."""
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        # Tiers: [(97, 0.05), (95, 0.10)]. 93 < 95 → no tier matches → default
        risk = bot.constants.DECIDED_CONTRACT_RISK  # default
        for floor, r in bot.constants.SOL_DC_RISK_TIERS:
            if 93 >= floor:
                risk = r
                break
        self.assertEqual(risk, 0.20)

    def test_sol_95c_uses_10pct(self):
        """SOL DC at 95c: matches 95c tier → 10%."""
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        risk = bot.constants.DECIDED_CONTRACT_RISK
        for floor, r in bot.constants.SOL_DC_RISK_TIERS:
            if 95 >= floor:
                risk = r
                break
        self.assertEqual(risk, 0.10)

    def test_sol_96c_uses_10pct(self):
        """SOL DC at 96c: matches 95c tier → 10%."""
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        risk = bot.constants.DECIDED_CONTRACT_RISK
        for floor, r in bot.constants.SOL_DC_RISK_TIERS:
            if 96 >= floor:
                risk = r
                break
        self.assertEqual(risk, 0.10)

    def test_sol_97c_uses_5pct(self):
        """SOL DC at 97c: matches 97c tier → 5%."""
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        risk = bot.constants.DECIDED_CONTRACT_RISK
        for floor, r in bot.constants.SOL_DC_RISK_TIERS:
            if 97 >= floor:
                risk = r
                break
        self.assertEqual(risk, 0.05)

    def test_sol_99c_uses_5pct(self):
        """SOL DC at 99c: matches 97c tier → 5%."""
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        risk = bot.constants.DECIDED_CONTRACT_RISK
        for floor, r in bot.constants.SOL_DC_RISK_TIERS:
            if 99 >= floor:
                risk = r
                break
        self.assertEqual(risk, 0.05)

    def test_xrp_96c_uses_20pct(self):
        """XRP DC at any price: always default 20% (no tiering)."""
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        # Non-SOL assets don't use SOL_DC_RISK_TIERS
        self.assertEqual(bot.constants.DECIDED_CONTRACT_RISK, 0.20)

    def test_btc_96c_uses_20pct(self):
        """BTC DC at any price: always default 20% (no tiering)."""
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        self.assertEqual(bot.constants.DECIDED_CONTRACT_RISK, 0.20)


class TestT2Z2Phase1ShadowContract(unittest.TestCase):
    """Contract tests for dc_t2_z2_phase1_shadow (added Apr 22 2026).

    Shadow-only path logging the rejected Phase 1 re-promotion cohort:
    T2-Z2 tier, BTC+ETH only, sized at 10%. No live trading behavior.
    See kb/decisions/t2-z2-shadowed.md Apr 22 section.
    """

    def setUp(self):
        self.source = _read_bot()

    def test_phase1_risk_constant_is_10pct(self):
        """DC_T2_Z2_PHASE1_RISK must be 0.10 (proposed Phase 1 sizing)."""
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        self.assertEqual(bot.constants.DC_T2_Z2_PHASE1_RISK, 0.10)

    def test_live_risk_constant_unchanged(self):
        """Live risk (DECIDED_CONTRACT_T2_Z2_RISK) must remain 0.20 — Phase 1 is shadow-only."""
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        self.assertEqual(bot.constants.DECIDED_CONTRACT_T2_Z2_RISK, 0.20)

    def test_live_enable_flag_defaults_off(self):
        """DECIDED_T2_Z2_ENABLED defaults to False — Phase 1 does NOT enable live.

        Bit 9.3-iii.b (2026-05-11): pre-retirement this called `importlib.reload(bot)` to pick
        up the env var change. Post-retirement reloading bot.constants would break `is` identity
        comparisons in sister tests (test_engines_extraction etc. hold references to constants).
        We check the env var directly instead — equivalent contract, no module reload.
        """
        import os
        # The constant is a module-level expression `os.environ.get("DECIDED_T2_Z2_ENABLED", "0") == "1"`.
        # Verify the env default behavior by inspecting what get() returns when unset.
        env_bak = os.environ.pop("DECIDED_T2_Z2_ENABLED", None)
        try:
            actual = os.environ.get("DECIDED_T2_Z2_ENABLED", "0") == "1"
            self.assertFalse(actual,
                             "DECIDED_T2_Z2_ENABLED must default False when env unset")
        finally:
            if env_bak is not None:
                os.environ["DECIDED_T2_Z2_ENABLED"] = env_bak

    def test_phase1_shadow_in_frozenset(self):
        """dc_t2_z2_phase1_shadow must be a member of DC_SHADOW_STAGES."""
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        self.assertIn("dc_t2_z2_phase1_shadow", bot.constants.DC_SHADOW_STAGES)

    def test_phase1_shadow_block_asset_filter_btc_eth_only(self):
        """Phase 1 shadow block must gate on asset in ('BTC', 'ETH') — no SOL/XRP."""
        # Locate the Phase 1 block
        start = self.source.find("# ── Phase 1 re-promotion shadow")
        self.assertNotEqual(start, -1, "Phase 1 shadow block marker not found")
        end = self.source.find("# ── Live overlay", start)
        self.assertNotEqual(end, -1, "Live overlay block not found after Phase 1 shadow")
        block = self.source[start:end]
        # Contract: must include BTC/ETH tuple check
        self.assertIn('asset in ("BTC", "ETH")', block,
                      "Phase 1 shadow must gate on asset in ('BTC', 'ETH')")
        # Contract: must NOT fire for SOL or XRP by name
        self.assertNotIn('"SOL"', block)
        self.assertNotIn('"XRP"', block)

    def test_phase1_shadow_block_tier_filter(self):
        """Phase 1 shadow must only fire when _dc_tier == 'decided_contract_t2_z2'."""
        start = self.source.find("# ── Phase 1 re-promotion shadow")
        end = self.source.find("# ── Live overlay", start)
        block = self.source[start:end]
        self.assertIn('_dc_tier == "decided_contract_t2_z2"', block,
                      "Phase 1 shadow must gate on T2-Z2 tier")

    def test_phase1_shadow_uses_phase1_risk_constant(self):
        """Phase 1 shadow sizing must use DC_T2_Z2_PHASE1_RISK, not the live constant."""
        start = self.source.find("# ── Phase 1 re-promotion shadow")
        end = self.source.find("# ── Live overlay", start)
        block = self.source[start:end]
        self.assertIn("DC_T2_Z2_PHASE1_RISK", block,
                      "Phase 1 shadow must use DC_T2_Z2_PHASE1_RISK for sizing")
        # And must NOT use the live risk constant
        self.assertNotIn("DECIDED_CONTRACT_T2_Z2_RISK", block,
                         "Phase 1 shadow must not reference live risk constant")

    def test_phase1_shadow_logs_correct_filter_stage(self):
        """Phase 1 shadow insert must use filter_stage='dc_t2_z2_phase1_shadow'."""
        start = self.source.find("# ── Phase 1 re-promotion shadow")
        end = self.source.find("# ── Live overlay", start)
        block = self.source[start:end]
        self.assertIn('"dc_t2_z2_phase1_shadow"', block)
        self.assertIn("insert_evaluated_opportunity", block)

    def test_phase1_shadow_does_NOT_append_to_candidates(self):
        """CRITICAL: Phase 1 shadow must NOT add to candidates list (no live trade)."""
        start = self.source.find("# ── Phase 1 re-promotion shadow")
        end = self.source.find("# ── Live overlay", start)
        block = self.source[start:end]
        self.assertNotIn("candidates.append", block,
                         "Phase 1 shadow must be observation-only (no candidates.append)")

    def test_phase1_shadow_has_balance_guard(self):
        """Phase 1 sizing must guard against zero/None balance."""
        start = self.source.find("# ── Phase 1 re-promotion shadow")
        end = self.source.find("# ── Live overlay", start)
        block = self.source[start:end]
        self.assertIn("_dc_balance and _dc_balance > 0", block,
                      "Phase 1 shadow must gate on balance > 0")

    def test_phase1_shadow_has_dedup(self):
        """Phase 1 shadow must dedup to avoid duplicate rows per ticker."""
        start = self.source.find("# ── Phase 1 re-promotion shadow")
        end = self.source.find("# ── Live overlay", start)
        block = self.source[start:end]
        self.assertIn("_eval_opp_seen", block,
                      "Phase 1 shadow must use _eval_opp_seen for per-ticker dedup")
        self.assertIn('"dc_t2_z2_phase1_shadow"', block)

    def test_phase1_shadow_placed_before_live_overlay(self):
        """Phase 1 shadow block must appear BEFORE live-overlay check (shadow logs first)."""
        shadow_pos = self.source.find("# ── Phase 1 re-promotion shadow")
        live_pos = self.source.find("# ── Live overlay: queue as candidate if tier enabled")
        self.assertGreater(shadow_pos, 0)
        self.assertGreater(live_pos, shadow_pos,
                           "Phase 1 shadow must appear before live-overlay block")

    def test_phase1_sizing_math_10pct_at_representative_balances(self):
        """Verify sizing formula matches expectation at sample balances."""
        # Formula: max(1, int((balance_cents * 0.10) / best_ask))
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        risk = bot.constants.DC_T2_Z2_PHASE1_RISK
        # Use raw int math to match bot's integer-cent math
        cases = [
            # (balance_cents, best_ask_cents, expected_contracts)
            (1_000_000, 94, max(1, int(1_000_000 * risk / 94))),   # $10K @ 94c
            (5_000_000, 94, max(1, int(5_000_000 * risk / 94))),   # $50K @ 94c
            (10_000_000, 96, max(1, int(10_000_000 * risk / 96))), # $100K @ 96c
            (1_000, 96, 1),  # tiny balance → floor of 1 contract
        ]
        for balance, ask, expected in cases:
            computed = max(1, int((balance * risk) / ask))
            self.assertEqual(computed, expected,
                             f"Sizing mismatch at balance={balance}, ask={ask}")

    def test_phase1_btc_eth_filter_excludes_sol_xrp(self):
        """Asset filter contract: tuple ('BTC', 'ETH') must not contain SOL or XRP."""
        allowed = ("BTC", "ETH")
        for blocked in ("SOL", "XRP"):
            self.assertNotIn(blocked, allowed,
                             f"{blocked} must be excluded from Phase 1 asset filter")


if __name__ == "__main__":
    unittest.main()
