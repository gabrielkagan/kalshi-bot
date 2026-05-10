"""Tests for stacking infrastructure.

Guards against:
- Composite PK (ticker, strategy_group) support
- strategy_to_group mapping correctness
- Position merge vs stack logic
- Exposure caps (per-ticker 20%, per-window 25%)
- Settlement with multiple positions per ticker
- Addon exclusion from TM fills
- STACKING_ENABLED kill switch
"""

import re
import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot/_impl.py")
MODELS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models.py")


def _read_bot():
    """Bit 3.1 (+7.1): returns concat of bot/_impl.py + bot/constants.py +
    bot/state.py source. Tests that look for CONSTANT = value definitions
    (in bot/constants.py post-Bit-3.1) AND tests that look for StateManager
    method bodies / INSERT statements (in bot/state.py post-Bit-7.1) AND
    tests that look for OpportunityScanner / scan-loop patterns (still in
    bot/_impl.py) all find their targets in the concatenated source.
    """
    with open(BOT_PATH) as f:
        impl = f.read()
    # Bit 8.1 (2026-05-10): scanner moved to bot/scanner/__init__.py.
    # Concat its source so audits that grep for OpportunityScanner
    # content (filter_stage literals, gate comments, etc.) survive the move.
    _scanner_path = os.path.join(os.path.dirname(BOT_PATH), "scanner", "__init__.py")
    if os.path.isfile(_scanner_path):
        with open(_scanner_path) as _f:
            impl += "\n" + _f.read()
    parts = [impl]
    constants_path = os.path.join(os.path.dirname(BOT_PATH), "constants.py")
    if os.path.exists(constants_path):
        with open(constants_path) as f:
            parts.append(f.read())
    state_path = os.path.join(os.path.dirname(BOT_PATH), "state.py")
    if os.path.exists(state_path):
        with open(state_path) as f:
            parts.append(f.read())
    # Bit 8.1 (2026-05-10): scanner moved to bot/scanner/__init__.py.
    # Append its source so audits that grep for OpportunityScanner content survive the move.
    _scanner_path = os.path.join(os.path.dirname(BOT_PATH), "scanner", "__init__.py")
    if os.path.isfile(_scanner_path):
        with open(_scanner_path) as _f:
            parts.append(_f.read())
    # Bit 9.1 (2026-05-10): OrderExecutor extracted to bot/executor.py.
    # Append its source so audits that grep for OrderExecutor content survive the move.
    _executor_path = os.path.join(os.path.dirname(BOT_PATH), "executor.py") if "BOT_PATH" in globals() else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot", "executor.py")
    if os.path.isfile(_executor_path):
        with open(_executor_path) as _f:
            parts.append(_f.read())
    return "\n".join(parts)


def _read_models():
    with open(MODELS_PATH) as f:
        return f.read()


class TestStrategyToGroup(unittest.TestCase):
    """Verify strategy_to_group mapping is correct and lives in models.py."""

    def test_function_exists_in_models(self):
        source = _read_models()
        self.assertIn("def strategy_to_group", source,
                       "strategy_to_group must be defined in models.py, not bot/_impl.py")

    def test_none_maps_to_main(self):
        from models import strategy_to_group
        self.assertEqual(strategy_to_group(None), "main")

    def test_empty_maps_to_main(self):
        from models import strategy_to_group
        self.assertEqual(strategy_to_group(""), "main")

    def test_execution_strategies_map_to_main(self):
        from models import strategy_to_group
        for s in ("MAKER_PATIENT", "TAKER_NOW", "MAKER_AGGRESSIVE", "PANIC_CAPTURE"):
            self.assertEqual(strategy_to_group(s), "main", f"{s} should map to 'main'")

    def test_addon_strategies_map_to_main(self):
        from models import strategy_to_group
        for s in ("CONFIRMATION_ADDON", "DIP_ADDON"):
            self.assertEqual(strategy_to_group(s), "main", f"{s} should map to 'main'")

    def test_decided_strategies_map_to_decided(self):
        from models import strategy_to_group
        for s in ("decided_t1", "decided_t2", "decided_t2_z2", "decided_t2_z25"):
            self.assertEqual(strategy_to_group(s), "decided", f"{s} should map to 'decided'")

    def test_terminal_momentum(self):
        from models import strategy_to_group
        self.assertEqual(strategy_to_group("terminal_momentum"), "terminal_momentum")

    def test_weekend_discount(self):
        from models import strategy_to_group
        self.assertEqual(strategy_to_group("weekend_discount"), "weekend_discount")

    def test_bracket_no(self):
        from models import strategy_to_group
        self.assertEqual(strategy_to_group("bracket_no"), "bracket_no")


class TestStackingConstants(unittest.TestCase):
    """Verify stacking constants exist with correct values."""

    def setUp(self):
        self.source = _read_bot()

    def test_stacking_enabled_exists(self):
        self.assertIn("STACKING_ENABLED", self.source)

    def test_max_ticker_risk_exists(self):
        m = re.search(r'^MAX_TICKER_RISK\s*=\s*([^\s#]+)', self.source, re.MULTILINE)
        self.assertIsNotNone(m, "MAX_TICKER_RISK must be defined in bot/_impl.py")
        self.assertAlmostEqual(float(m.group(1)), 0.25)

    def test_max_window_risk_exists(self):
        m = re.search(r'^MAX_WINDOW_RISK\s*=\s*([^\s#]+)', self.source, re.MULTILINE)
        self.assertIsNotNone(m, "MAX_WINDOW_RISK must be defined in bot/_impl.py")
        self.assertAlmostEqual(float(m.group(1)), 0.30)


class TestRecordPositionFromFill(unittest.TestCase):
    """Verify record_position_from_fill uses composite PK (ticker, strategy_group)."""

    def setUp(self):
        self.source = _read_bot()

    def test_select_uses_strategy_group(self):
        self.assertIn("AND strategy_group=?", self.source,
                       "Position SELECT must filter by strategy_group for composite PK")

    def test_update_uses_strategy_group(self):
        # Find UPDATE positions ... AND strategy_group=?
        update_pattern = re.search(r'UPDATE positions.*AND strategy_group=\?', self.source, re.DOTALL)
        self.assertIsNotNone(update_pattern,
                              "Position UPDATE must include AND strategy_group=?")

    def test_insert_has_strategy_group(self):
        # Find INSERT INTO positions that includes strategy_group in column list
        insert_pattern = re.search(r'INSERT INTO positions.*strategy_group', self.source, re.DOTALL)
        self.assertIsNotNone(insert_pattern,
                              "Position INSERT must include strategy_group column")

    def test_insert_has_is_stacked(self):
        insert_pattern = re.search(r'INSERT INTO positions.*is_stacked', self.source, re.DOTALL)
        self.assertIsNotNone(insert_pattern,
                              "Position INSERT must include is_stacked column")

    def test_calls_strategy_to_group(self):
        self.assertIn("strategy_to_group", self.source,
                       "Must call strategy_to_group to compute group from strategy")


class TestSettlementRefactor(unittest.TestCase):
    """Verify settlement handles multiple positions per ticker."""

    def setUp(self):
        self.source = _read_bot()

    def test_process_settlement_uses_fetchall(self):
        # _process_settlement must use fetchall to handle multiple rows per ticker
        m = re.search(r'def _process_settlement.*?(?=\n    def |\nclass |\Z)',
                       self.source, re.DOTALL)
        self.assertIsNotNone(m, "_process_settlement must exist")
        self.assertIn("fetchall()", m.group(0),
                       "_process_settlement must use fetchall() for multi-position support")

    def test_per_row_revenue(self):
        m = re.search(r'def _process_settlement.*?(?=\n    def |\nclass |\Z)',
                       self.source, re.DOTALL)
        self.assertIsNotNone(m)
        body = m.group(0)
        self.assertTrue("count * 100" in body or "recorded_count * 100" in body,
                         "_process_settlement must compute per-row revenue")

    def test_record_settlement_has_pos_param(self):
        # pos parameter may be on a continuation line
        fn_start = self.source.find("def record_settlement")
        fn_sig = self.source[fn_start:fn_start + 300]
        self.assertIn("pos", fn_sig, "record_settlement must accept pos parameter")

    def test_record_settlement_insert_has_strategy_group(self):
        m = re.search(r'def record_settlement.*?(?=\n    def |\nclass |\Z)',
                       self.source, re.DOTALL)
        self.assertIsNotNone(m)
        body = m.group(0)
        insert_m = re.search(r'INSERT.*?settled_trades.*?strategy_group', body, re.DOTALL)
        self.assertIsNotNone(insert_m,
                              "record_settlement INSERT must include strategy_group")

    def test_record_settlement_insert_has_is_stacked(self):
        m = re.search(r'def record_settlement.*?(?=\n    def |\nclass |\Z)',
                       self.source, re.DOTALL)
        self.assertIsNotNone(m)
        body = m.group(0)
        insert_m = re.search(r'INSERT.*?settled_trades.*?is_stacked', body, re.DOTALL)
        self.assertIsNotNone(insert_m,
                              "record_settlement INSERT must include is_stacked")

    def test_record_settlement_has_revenue_override(self):
        """revenue_override prevents stacked positions from each getting full API revenue."""
        fn_start = self.source.find("def record_settlement")
        fn_sig = self.source[fn_start:fn_start + 400]
        self.assertIn("revenue_override", fn_sig,
                       "record_settlement must accept revenue_override parameter")

    def test_process_settlement_passes_revenue_override(self):
        """_process_settlement must pass revenue_override to record_settlement."""
        m = re.search(r'def _process_settlement.*?(?=\n    def |\nclass |\Z)',
                       self.source, re.DOTALL)
        self.assertIsNotNone(m)
        body = m.group(0)
        self.assertIn("revenue_override=row_revenue", body,
                       "_process_settlement must pass revenue_override=row_revenue")

    def test_record_settlement_applies_revenue_override(self):
        """record_settlement must use revenue_override when provided."""
        m = re.search(r'def record_settlement.*?(?=\n    def |\nclass |\Z)',
                       self.source, re.DOTALL)
        self.assertIsNotNone(m)
        body = m.group(0)
        self.assertIn("revenue_override is not None", body,
                       "record_settlement must check and apply revenue_override")

    def test_record_settlement_no_update_positions_settled(self):
        m = re.search(r'def record_settlement.*?(?=\n    def |\nclass |\Z)',
                       self.source, re.DOTALL)
        self.assertIsNotNone(m)
        body = m.group(0)
        self.assertNotIn("UPDATE positions SET status='settled'", body,
                          "record_settlement must NOT update positions — moved to _process_settlement")


class TestReconciliation(unittest.TestCase):
    """Verify reconciliation handles composite PK correctly."""

    def setUp(self):
        self.source = _read_bot()

    def test_reconcile_multi_mismatch(self):
        self.assertIn("RECONCILE_MULTI_MISMATCH", self.source)

    def test_reconcile_update_uses_strategy_group(self):
        # Reconciliation UPDATE must filter by strategy_group
        self.assertTrue(
            bool(re.search(r'RECONCILE.*strategy_group=\?', self.source, re.DOTALL)) or
            bool(re.search(r'AND strategy_group=\?.*RECONCILE', self.source, re.DOTALL)),
            "Reconciliation UPDATE must include AND strategy_group=?"
        )

    def test_reconcile_delete_unsettled(self):
        self.assertIn("RECONCILE_DELETE_UNSETTLED", self.source)


class TestExposureCaps(unittest.TestCase):
    """Verify exposure caps are in execute() and placed correctly."""

    def setUp(self):
        self.source = _read_bot()
        # Extract execute() method body
        m = re.search(r'def execute\(self.*?(?=\n    def |\nclass |\Z)',
                       self.source, re.DOTALL)
        self.execute_body = m.group(0) if m else ""

    def test_max_ticker_risk_in_execute(self):
        self.assertIn("MAX_TICKER_RISK", self.execute_body)

    def test_max_window_risk_in_execute(self):
        self.assertIn("MAX_WINDOW_RISK", self.execute_body)

    def test_ticker_cap_skipped(self):
        self.assertIn("TICKER_CAP_SKIPPED", self.execute_body)

    def test_window_cap_skipped(self):
        self.assertIn("WINDOW_CAP_SKIPPED", self.execute_body)

    def test_ticker_cap_reduced(self):
        self.assertIn("TICKER_CAP_REDUCED", self.execute_body)

    def test_window_cap_reduced(self):
        self.assertIn("WINDOW_CAP_REDUCED", self.execute_body)

    def test_caps_before_strategy_routing(self):
        # Cap code must appear BEFORE the hourly taker-only path
        cap_pos = self.execute_body.find("MAX_TICKER_RISK")
        hourly_pos = self.execute_body.find("HOURLY TAKER-ONLY PATH")
        self.assertGreater(cap_pos, 0, "MAX_TICKER_RISK not found in execute()")
        self.assertGreater(hourly_pos, 0, "HOURLY TAKER-ONLY PATH not found in execute()")
        self.assertLess(cap_pos, hourly_pos,
                         "Exposure caps must appear BEFORE strategy routing")


class TestStackingPositionChecks(unittest.TestCase):
    """Verify TM and DC check STACKING_ENABLED before position logic."""

    def setUp(self):
        self.source = _read_bot()

    def test_tm_position_check_references_stacking(self):
        # Near _tm_has_position or TM position check, STACKING_ENABLED must appear
        m = re.search(r'_tm_has_position.*?STACKING_ENABLED|STACKING_ENABLED.*?_tm_has_position',
                       self.source, re.DOTALL)
        self.assertIsNotNone(m,
                              "TM position check must reference STACKING_ENABLED")

    def test_dc_position_check_references_stacking(self):
        # DC existing position subtraction should reference STACKING_ENABLED
        self.assertTrue(
            "STACKING_ENABLED" in self.source,
            "STACKING_ENABLED must be referenced for DC position checks"
        )

    def test_tm_stacking_checks_group(self):
        # When stacking, TM path must verify strategy_group matches price-encoded TM group
        self.assertIn('"terminal_momentum_{best_ask}"', self.source)


class TestAddonExclusion(unittest.TestCase):
    """Verify addon fills don't trigger for TM/bracket_no strategies."""

    def setUp(self):
        self.source = _read_bot()

    def test_addon_skips_tm_taker(self):
        self.assertIn("tm_taker", self.source,
                       "tm_taker must be in addon exclusion list")

    def test_addon_skips_bracket_no_taker(self):
        self.assertIn("bracket_no_taker", self.source,
                       "bracket_no_taker must be in addon exclusion list")


class TestStartupWarning(unittest.TestCase):
    """Verify startup logs a warning when stacking is disabled."""

    def setUp(self):
        self.source = _read_bot()

    def test_stacking_disabled_warning(self):
        self.assertIn("STACKING_DISABLED but", self.source)


if __name__ == "__main__":
    unittest.main()
