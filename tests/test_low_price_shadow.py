"""Tests for Low-Price Shadow (20-79c) dual-sizing simulation.

Phase C of shadow coverage expansion (2026-05-02): floor 70 → 20,
STC cap 600 → 900. See kb/decisions/shadow-coverage-expansion-may01.md.

Guards against:
- Price gate: captures 20-79c signals (LOW_PRICE_SHADOW_MIN/MAX_PRICE)
- STC gate: LOW_PRICE_SHADOW_MAX_STC = 900s ceiling (full window)
- Product type gate: 15M only (not hourly, weather, sports)
- Dual sizing: both full Kelly and capped Kelly (LP_*) are computed
- Correlation tracking: window_signal_count and hour_signal_count recorded
- Dedicated table: low_price_shadow_signals exists with correct schema
- Settlement: counterfactual_pnl_full and _capped are computed
- Dashboard: low_price_shadow panel exists in snapshot
- No live impact: no candidates appended, no MIN_ENTRY_PRICE changed
"""

import re
import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot/_impl.py")
DASH_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard_snapshot.py")


def _read_bot():
    with open(BOT_PATH) as f:
        return f.read()


def _read_dash():
    with open(DASH_PATH) as f:
        return f.read()


class TestLowPriceShadowConstants(unittest.TestCase):
    """Verify promotion constants exist and have correct values."""

    def setUp(self):
        self.source = _read_bot()

    def test_enabled_flag(self):
        self.assertIn("LOW_PRICE_SHADOW_ENABLED = True", self.source)

    def test_min_price(self):
        # Phase C of shadow coverage expansion (2026-05-02): 70 → 20.
        # Master plan: kb/decisions/shadow-coverage-expansion-may01.md.
        # MIN_ENTRY_PRICE (live floor) is unchanged — this is shadow-only.
        self.assertIn("LOW_PRICE_SHADOW_MIN_PRICE = 20", self.source)

    def test_max_price(self):
        self.assertIn("LOW_PRICE_SHADOW_MAX_PRICE = 79", self.source)

    def test_max_stc(self):
        # Phase C: 600 → 900 (full scan-window).
        self.assertIn("LOW_PRICE_SHADOW_MAX_STC = 900", self.source)

    def test_lp_max_risk(self):
        self.assertIn("LP_MAX_RISK_PER_TRADE = 0.10", self.source)

    def test_lp_kelly_fraction(self):
        self.assertIn("LP_KELLY_FRACTION = 0.25", self.source)

    def test_lp_window_cap(self):
        self.assertIn("LP_WINDOW_CAP = 2", self.source)

    def test_lp_hour_cap(self):
        self.assertIn("LP_HOUR_CAP = 4", self.source)


class TestLowPriceShadowGates(unittest.TestCase):
    """Verify queue gating conditions exist in code."""

    def setUp(self):
        self.source = _read_bot()

    def test_price_gate_por_path(self):
        """POR path checks LOW_PRICE_SHADOW_MIN_PRICE <= best_ask <= LOW_PRICE_SHADOW_MAX_PRICE."""
        self.assertIn("LOW_PRICE_SHADOW_MIN_PRICE <= best_ask <= LOW_PRICE_SHADOW_MAX_PRICE", self.source)

    def test_stc_gate(self):
        """STC gate: seconds_remaining <= LOW_PRICE_SHADOW_MAX_STC."""
        self.assertIn("seconds_remaining <= LOW_PRICE_SHADOW_MAX_STC", self.source)

    def test_product_type_gate(self):
        """Only 15M signals queued."""
        # Find the low_price_shadow queue append blocks — should have _pt in (None, "15m")
        blocks = re.findall(r'LOW_PRICE_SHADOW_ENABLED.*?_low_price_shadow_queue\.append', self.source, re.DOTALL)
        self.assertGreater(len(blocks), 0, "No queue append blocks found")
        for block in blocks:
            self.assertIn('_pt in (None, "15m")', block)


class TestLowPriceShadowHandler(unittest.TestCase):
    """Verify _process_low_price_shadow method exists with correct structure."""

    def setUp(self):
        self.source = _read_bot()
        self.handler_start = self.source.find("def _process_low_price_shadow")
        self.assertGreater(self.handler_start, 0, "_process_low_price_shadow not found")
        self.handler_end = self.source.find("\n    def ", self.handler_start + 10)
        self.handler = self.source[self.handler_start:self.handler_end]

    def test_dual_sizing(self):
        """Both full Kelly and capped Kelly sizing computed."""
        self.assertIn("full_kelly", self.handler.lower())
        self.assertIn("capped", self.handler.lower())
        self.assertIn("LP_MAX_RISK_PER_TRADE", self.handler)
        self.assertIn("LP_KELLY_FRACTION", self.handler)

    def test_correlation_tracking(self):
        """Window and hour signal counts tracked."""
        self.assertIn("_lp_window_counts", self.handler)
        self.assertIn("_lp_hour_signals", self.handler)
        self.assertIn("window_signal_count", self.handler)
        self.assertIn("hour_signal_count", self.handler)

    def test_dedup(self):
        """Dedup key uses 2-tuple (ticker, 'low_price_shadow')."""
        self.assertIn('(ticker, "low_price_shadow")', self.handler)

    def test_db_insert_eval_opp(self):
        """Inserts to evaluated_opportunities for settlement linking."""
        self.assertIn("insert_evaluated_opportunity", self.handler)
        self.assertIn('"low_price_shadow"', self.handler)

    def test_db_insert_dedicated_table(self):
        """Inserts to low_price_shadow_signals dedicated table."""
        self.assertIn("low_price_shadow_signals", self.handler)

    def test_jsonl_log(self):
        """JSONL log written."""
        self.assertIn("log_opportunity", self.handler)

    def test_try_except_isolation(self):
        """Entire handler wrapped in try/except for isolation."""
        self.assertIn("low_price_shadow processing error", self.handler)

    def test_por_path_handling(self):
        """POR path (has_prob=False) runs ProbabilityEngine."""
        self.assertIn("ProbabilityEngine.compute", self.handler)

    def test_ie_path_handling(self):
        """IE path (has_prob=True) reuses pre-computed values."""
        self.assertIn('has_prob', self.handler)


class TestLowPriceShadowTable(unittest.TestCase):
    """Verify dedicated table schema."""

    def setUp(self):
        self.source = _read_bot()

    def test_table_creation(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS low_price_shadow_signals", self.source)

    def test_dual_sizing_columns(self):
        self.assertIn("full_kelly_risk_fraction", self.source)
        self.assertIn("full_kelly_contracts", self.source)
        self.assertIn("capped_risk_fraction", self.source)
        self.assertIn("capped_contracts", self.source)

    def test_correlation_columns(self):
        self.assertIn("window_signal_count", self.source)
        self.assertIn("hour_signal_count", self.source)

    def test_settlement_columns(self):
        self.assertIn("counterfactual_pnl_full", self.source)
        self.assertIn("counterfactual_pnl_capped", self.source)


class TestLowPriceShadowSettlement(unittest.TestCase):
    """Verify settlement linking exists."""

    def setUp(self):
        self.source = _read_bot()

    def test_settlement_block_exists(self):
        """Settlement code updates low_price_shadow_signals on settlement."""
        settle_start = self.source.find("Settle low_price_shadow_signals")
        self.assertGreater(settle_start, 0, "Settlement block not found")
        settle_block = self.source[settle_start:settle_start + 2000]
        self.assertIn("counterfactual_pnl_full", settle_block)
        self.assertIn("counterfactual_pnl_capped", settle_block)
        self.assertIn("status='settled'", settle_block)


class TestLowPriceShadowNoLiveImpact(unittest.TestCase):
    """Verify shadow doesn't affect live trading."""

    def setUp(self):
        self.source = _read_bot()
        self.handler_start = self.source.find("def _process_low_price_shadow")
        self.handler_end = self.source.find("\n    def ", self.handler_start + 10)
        self.handler = self.source[self.handler_start:self.handler_end]

    def test_no_candidates_append(self):
        """Shadow handler never appends to candidates."""
        self.assertNotIn("candidates.append", self.handler)

    def test_no_execute_order(self):
        """Shadow handler never calls execute()."""
        self.assertNotIn("self._executor.execute", self.handler)
        self.assertNotIn("execute_order", self.handler)

    def test_min_entry_price_unchanged(self):
        """MIN_ENTRY_PRICE still 75, per-asset floors at current values."""
        self.assertIn("MIN_ENTRY_PRICE = 75", self.source)
        self.assertIn("BTC_MIN_ENTRY_PRICE = 88", self.source)
        self.assertIn("ETH_MIN_ENTRY_PRICE = 90", self.source)
        self.assertIn("XRP_MIN_ENTRY_PRICE = 92", self.source)


class TestLowPriceShadowDashboard(unittest.TestCase):
    """Verify dashboard snapshot tracks low-price shadow."""

    def setUp(self):
        self.dash_source = _read_dash()

    def test_panel_exists(self):
        self.assertIn('snap["low_price_shadow"]', self.dash_source)

    def test_in_shadow_stages(self):
        self.assertIn("'low_price_shadow'", self.dash_source)

    def test_in_slow_cache(self):
        slow_start = self.dash_source.find("_SLOW_SNAP_KEYS")
        slow_block = self.dash_source[slow_start:slow_start + 700]
        self.assertIn("low_price_shadow", slow_block)

    def test_price_tier_bucketer(self):
        self.assertIn("_bucket_low_price", self.dash_source)

    def test_correlation_panel(self):
        self.assertIn("correlation", self.dash_source)
        self.assertIn("avg_window_ct", self.dash_source)

    def test_capped_pnl(self):
        self.assertIn("capped_pnl_cents", self.dash_source)
        self.assertIn("full_pnl_cents", self.dash_source)


class TestLowPriceShadowQueueProcessing(unittest.TestCase):
    """Verify queue is initialized and processed."""

    def setUp(self):
        self.source = _read_bot()

    def test_queue_initialized(self):
        self.assertIn("_low_price_shadow_queue = []", self.source)

    def test_queue_processed(self):
        self.assertIn("self._process_low_price_shadow(_low_price_shadow_queue)", self.source)

    def test_enabled_gate_on_processing(self):
        """Processing is gated by LOW_PRICE_SHADOW_ENABLED."""
        process_idx = self.source.find("self._process_low_price_shadow(_low_price_shadow_queue)")
        pre_block = self.source[process_idx - 200:process_idx]
        self.assertIn("LOW_PRICE_SHADOW_ENABLED", pre_block)


class TestLowPriceShadowDashboardBucketer(unittest.TestCase):
    """Phase C of shadow coverage expansion widened the band to 20-79¢; the
    dashboard `_bucket_low_price` rollup must cover the full new range so
    20-69¢ rows don't silently bucket into 70-74."""

    def setUp(self):
        self.dash_source = _read_dash()

    def test_bucketer_covers_new_tiers(self):
        """Tiers 20-39, 40-54, 55-69 must exist alongside legacy 70-74, 75-79."""
        bucketer_idx = self.dash_source.find("def _bucket_low_price")
        self.assertGreater(bucketer_idx, 0)
        body = self.dash_source[bucketer_idx:bucketer_idx + 600]
        for tier in ('"20-39"', '"40-54"', '"55-69"', '"70-74"', '"75-79"'):
            self.assertIn(tier, body, f"bucketer missing tier {tier}")


class TestLowPriceShadowDocReference(unittest.TestCase):
    """agent_docs/config_reference.md must list LOW_PRICE_SHADOW_* with the
    current values (Phase C of shadow coverage expansion). Doc-drift rule
    per CLAUDE.md."""

    def setUp(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "agent_docs", "config_reference.md")
        with open(path) as f:
            self.doc = f.read()

    def test_min_price_documented(self):
        self.assertIn("LOW_PRICE_SHADOW_MIN_PRICE", self.doc)
        # value must appear as a whole-number token (avoids matching "120", "200", etc.)
        for line in self.doc.splitlines():
            if "LOW_PRICE_SHADOW_MIN_PRICE" in line:
                self.assertRegex(
                    line, r"(?<!\d)20(?!\d)",
                    f"min_price doc row missing standalone 20: {line!r}")
                return
        self.fail("LOW_PRICE_SHADOW_MIN_PRICE not in config_reference.md")

    def test_max_stc_documented(self):
        for line in self.doc.splitlines():
            if "LOW_PRICE_SHADOW_MAX_STC" in line:
                self.assertRegex(
                    line, r"(?<!\d)900(?!\d)",
                    f"max_stc doc row missing standalone 900: {line!r}")
                return
        self.fail("LOW_PRICE_SHADOW_MAX_STC not in config_reference.md")


if __name__ == "__main__":
    unittest.main()
