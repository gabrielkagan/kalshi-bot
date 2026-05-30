"""T1 regression tests for ADA + BCH asset onboarding (15M shadow).

Locks the atomic-activation safety invariant: if ADA/BCH are in
config.ASSETS, the shadow gates and exclusion sets MUST contain them.
Partial reverts that leave ASSETS extended without the safety gates are
caught here (would otherwise risk live orders on an asset with no per-asset
risk sizing — there are NONE for ADA/BCH at T1, so live routing must be
fully gated).

Mirrors tests/integration/test_bnb_onboarding_t1.py (the SHADOW half only —
ADA/BCH are T1 shadow, NOT T4 live, so the P2.x exact-value / live-routing
elif-chain classes are intentionally absent).

Pre-flight (2026-05-30):
  KXADA15M "Cardano 15 Minute" fifteen_min / KXBCH15M "Bitcoin Cash 15 Minute"
  fifteen_min — both CF Benchmarks. Coinbase ADA-USD / BCH-USD online.
  ADA has NO hourly series (KXADAD not live); BCH has KXBCHD. Both 15M
  series had 0 markets minted at wiring time (inert until Kalshi starts cycle).

Plan:    kb/decisions/ada-bch-15m-shadow-t1-plan.md
"""
from __future__ import annotations

import unittest


# ─── Ticker parser regression locks ──────────────────────────────────────
class TestTickerParserAdaBch(unittest.TestCase):
    def setUp(self):
        from bot.state import StateManager
        self.parse = StateManager._asset_from_ticker

    def test_ada_15m_ticker(self):
        self.assertEqual(self.parse("KXADA15M-26MAY171600-00"), "ADA")

    def test_ada_15m_no_strike(self):
        self.assertEqual(self.parse("KXADA15M-26MAY171600"), "ADA")

    def test_bch_15m_ticker(self):
        self.assertEqual(self.parse("KXBCH15M-26MAY171600-00"), "BCH")

    def test_bch_15m_no_strike(self):
        self.assertEqual(self.parse("KXBCH15M-26MAY171600"), "BCH")

    def test_bch_hourly_ticker(self):
        self.assertEqual(self.parse("KXBCHD-26MAY1716-T844.99"), "BCH")

    def test_ada_hourly_ticker(self):
        # KXADAD not live yet, but the parser must still map it correctly
        # (future-proof for the inert HOURLY_SERIES_TICKERS entry).
        self.assertEqual(self.parse("KXADAD-26MAY1716"), "ADA")

    def test_existing_assets_unchanged(self):
        self.assertEqual(self.parse("KXBTC15M-26FEB211545-45"), "BTC")
        self.assertEqual(self.parse("KXBNB15M-26FEB211545-45"), "BNB")
        self.assertEqual(self.parse("KXDOGE15M-26FEB211545-45"), "DOGE")
        self.assertEqual(self.parse("KXXRPD-26FEB211545"), "XRP")


# ─── Atomic-activation safety invariant ──────────────────────────────────
class TestAtomicActivationSafety(unittest.TestCase):
    def test_assets_registry_contains_ada_and_bch(self):
        from bot.config import ASSETS
        self.assertIn("ADA", ASSETS, "ADA missing from bot.config.ASSETS")
        self.assertIn("BCH", ASSETS, "BCH missing from bot.config.ASSETS")

    def test_ada_15m_shadow_flag_true(self):
        """T1 contract: ADA observes in shadow (no live routing)."""
        from bot.constants import ADA_15M_SHADOW
        self.assertIsInstance(ADA_15M_SHADOW, bool)
        self.assertTrue(ADA_15M_SHADOW,
            "ADA_15M_SHADOW must be True at T1 — shadow observation only")

    def test_bch_15m_shadow_flag_true(self):
        from bot.constants import BCH_15M_SHADOW
        self.assertIsInstance(BCH_15M_SHADOW, bool)
        self.assertTrue(BCH_15M_SHADOW,
            "BCH_15M_SHADOW must be True at T1 — shadow observation only")

    def test_in_assets_implies_hourly_excluded(self):
        """SAFETY: if active, hourly YES-side must be excluded (no per-asset risk)."""
        from bot.constants import HOURLY_EXCLUDED_ASSETS
        self.assertIn("ADA", HOURLY_EXCLUDED_ASSETS,
            "SAFETY: ADA missing from HOURLY_EXCLUDED_ASSETS while in ASSETS")
        self.assertIn("BCH", HOURLY_EXCLUDED_ASSETS,
            "SAFETY: BCH missing from HOURLY_EXCLUDED_ASSETS while in ASSETS")

    def test_in_assets_implies_no_side_excluded(self):
        from bot.constants import HOURLY_NO_EXCLUDED_ASSETS
        self.assertIn("ADA", HOURLY_NO_EXCLUDED_ASSETS,
            "SAFETY: ADA missing from HOURLY_NO_EXCLUDED_ASSETS")
        self.assertIn("BCH", HOURLY_NO_EXCLUDED_ASSETS,
            "SAFETY: BCH missing from HOURLY_NO_EXCLUDED_ASSETS")


# ─── Registry completeness ───────────────────────────────────────────────
class TestRegistryCompleteness(unittest.TestCase):
    def test_series_tickers_complete(self):
        from bot.constants import SERIES_TICKERS
        self.assertEqual(SERIES_TICKERS["ADA"], "KXADA15M")
        self.assertEqual(SERIES_TICKERS["BCH"], "KXBCH15M")

    def test_hourly_series_tickers_complete(self):
        from bot.constants import HOURLY_SERIES_TICKERS
        # ADA hourly (KXADAD) is inert/future-proofed; BCH hourly (KXBCHD) is real.
        self.assertEqual(HOURLY_SERIES_TICKERS["ADA"], "KXADAD")
        self.assertEqual(HOURLY_SERIES_TICKERS["BCH"], "KXBCHD")

    def test_coinbase_products_complete(self):
        from bot.constants import COINBASE_PRODUCTS
        self.assertEqual(COINBASE_PRODUCTS["ADA"], "ADA-USD")
        self.assertEqual(COINBASE_PRODUCTS["BCH"], "BCH-USD")

    def test_all_registries_have_same_asset_keys(self):
        """Cross-registry invariant: every asset in ASSETS must be in all
        feed registries (load-bearing — avoids KeyErrors downstream)."""
        from bot.config import ASSETS
        from bot.constants import (
            SERIES_TICKERS, HOURLY_SERIES_TICKERS, COINBASE_PRODUCTS,
        )
        for asset in ASSETS:
            self.assertIn(asset, SERIES_TICKERS, f"{asset} not in SERIES_TICKERS")
            self.assertIn(asset, HOURLY_SERIES_TICKERS, f"{asset} not in HOURLY_SERIES_TICKERS")
            self.assertIn(asset, COINBASE_PRODUCTS, f"{asset} not in COINBASE_PRODUCTS")


# ─── Product-type categorizer (state.py source-walk) ─────────────────────
class TestProductTypeCategorizer(unittest.TestCase):
    def setUp(self):
        import bot.state, inspect
        self.source = inspect.getsource(bot.state)

    def test_ada_15m_in_categorizer(self):
        self.assertIn('"KXADA15M"', self.source,
            "KXADA15M not in inline categorizer (would drop ADA 15M settle rows)")

    def test_bch_15m_in_categorizer(self):
        self.assertIn('"KXBCH15M"', self.source)

    def test_bch_hourly_in_categorizer(self):
        self.assertIn('"KXBCHD"', self.source)

    def test_ada_hourly_in_categorizer(self):
        self.assertIn('"KXADAD"', self.source)


# ─── Executor maker-block prefixes ───────────────────────────────────────
class TestExecutorHourlyPrefixes(unittest.TestCase):
    def test_hourly_prefixes_include_ada_bch(self):
        from bot.executor import OrderExecutor
        self.assertIn("KXBCHD-", OrderExecutor._HOURLY_SERIES_PREFIXES)
        self.assertIn("KXADAD-", OrderExecutor._HOURLY_SERIES_PREFIXES)


# ─── State backfill SQL ──────────────────────────────────────────────────
class TestStateBackfillSQL(unittest.TestCase):
    def setUp(self):
        import bot.state, inspect
        self.source = inspect.getsource(bot.state)

    def test_15m_backfill_includes_ada(self):
        self.assertGreaterEqual(self.source.count("KXADA15M%"), 2,
            "KXADA15M% missing from state.py SQL backfill (expected ≥2 sites)")

    def test_15m_backfill_includes_bch(self):
        self.assertGreaterEqual(self.source.count("KXBCH15M%"), 2,
            "KXBCH15M% missing from state.py SQL backfill (expected ≥2 sites)")

    def test_hourly_backfill_includes_bch(self):
        self.assertGreaterEqual(self.source.count("KXBCHD%"), 2,
            "KXBCHD% missing from state.py SQL backfill (expected ≥2 sites)")

    def test_hourly_backfill_includes_ada(self):
        self.assertGreaterEqual(self.source.count("KXADAD%"), 2,
            "KXADAD% missing from state.py SQL backfill (expected ≥2 sites)")


# ─── Spot-at-decision columns (close the BNB-T1 gap) ─────────────────────
class TestSpotColumnsWired(unittest.TestCase):
    """ADA/BCH spot columns must be wired end-to-end in state.py so the
    scanner's ASSETS-driven dict-comp value is persisted rather than
    silently dropped (the gap that became BNB followup 86b9zn5pq)."""

    def setUp(self):
        import bot.state, inspect
        self.source = inspect.getsource(bot.state)

    def test_ada_spot_column_present(self):
        # ≥4 sites: schema, kwarg, _ext fallback, INSERT/ON-CONFLICT.
        self.assertGreaterEqual(self.source.count("ada_spot_at_decision"), 4,
            "ada_spot_at_decision not fully wired in state.py")

    def test_bch_spot_column_present(self):
        self.assertGreaterEqual(self.source.count("bch_spot_at_decision"), 4,
            "bch_spot_at_decision not fully wired in state.py")

    def test_insert_column_placeholder_alignment(self):
        """The evaluated_opportunities INSERT must have matching column-count
        and placeholder-count. A misaligned add of the 2 spot columns is a
        runtime crash on first write — pin it structurally."""
        import re
        # Find the INSERT INTO evaluated_opportunities ... VALUES (...) block.
        m = re.search(
            r"INSERT\s+(?:OR\s+REPLACE\s+)?INTO\s+evaluated_opportunities\s*\((.*?)\)\s*VALUES\s*\((.*?)\)",
            self.source, re.DOTALL | re.IGNORECASE)
        self.assertIsNotNone(m, "evaluated_opportunities INSERT block not found")
        cols = [c for c in m.group(1).split(",") if c.strip()]
        placeholders = [p for p in m.group(2).split(",") if p.strip()]
        self.assertEqual(
            len(cols), len(placeholders),
            f"INSERT column/placeholder mismatch: {len(cols)} cols vs "
            f"{len(placeholders)} placeholders — spot-column add misaligned")


# ─── Scanner shadow gate wiring (source-walk) ────────────────────────────
class TestScannerShadowGateWiring(unittest.TestCase):
    def setUp(self):
        import bot.scanner, inspect
        self.source = inspect.getsource(bot.scanner)

    def test_ada_shadow_imported(self):
        self.assertIn("ADA_15M_SHADOW", self.source,
            "ADA_15M_SHADOW not referenced in bot/scanner/__init__.py")

    def test_bch_shadow_imported(self):
        self.assertIn("BCH_15M_SHADOW", self.source)

    def test_ada_shadow_strategy_name_present(self):
        self.assertIn('"ada_shadow"', self.source,
            "shadow row strategy='ada_shadow' not wired in scanner")

    def test_bch_shadow_strategy_name_present(self):
        self.assertIn('"bch_shadow"', self.source)

    def test_no_side_shadow_stages_present(self):
        self.assertIn('"no_side_ada_shadow"', self.source)
        self.assertIn('"no_side_bch_shadow"', self.source)


# ─── Market config startup-assert mirror ─────────────────────────────────
class TestMarketConfigMirror(unittest.TestCase):
    def test_hourly_excluded_assets_mirror(self):
        from bot.constants import HOURLY_EXCLUDED_ASSETS
        from market_config import MARKET_CONFIGS
        cfg_h = MARKET_CONFIGS["hourly"]
        self.assertEqual(cfg_h.excluded_assets, frozenset(HOURLY_EXCLUDED_ASSETS),
            "market_config.py excluded_assets != HOURLY_EXCLUDED_ASSETS — startup crash")

    def test_ada_bch_in_market_config_excluded(self):
        from market_config import MARKET_CONFIGS
        cfg_h = MARKET_CONFIGS["hourly"]
        self.assertIn("ADA", cfg_h.excluded_assets)
        self.assertIn("BCH", cfg_h.excluded_assets)


# ─── Strategy kill-switch clauses (RCA Finding 2 — L96) ──────────────────
class TestStrategyKillSwitchClauses(unittest.TestCase):
    def setUp(self):
        import bot.scanner, inspect
        self.source = inspect.getsource(bot.scanner)

    def test_terminal_momentum_has_ada_bch_kill_switch(self):
        idx = self.source.find("if (TERMINAL_MOMENTUM_ENABLED")
        self.assertGreater(idx, 0, "TM if-gate not found")
        window = self.source[idx:idx + 1200]
        self.assertIn("ADA_15M_SHADOW", window,
            "TM gate missing ADA_15M_SHADOW kill-switch")
        self.assertIn("BCH_15M_SHADOW", window,
            "TM gate missing BCH_15M_SHADOW kill-switch")

    def test_weekend_discount_has_ada_bch_kill_switch(self):
        idx = self.source.find("WEEKEND_DISCOUNT_LIVE\n")
        self.assertGreater(idx, 0, "WEEKEND_DISCOUNT_LIVE flag not found")
        window = self.source[idx:idx + 900]
        self.assertIn("ADA_15M_SHADOW", window)
        self.assertIn("BCH_15M_SHADOW", window)

    def test_overnight_discount_has_ada_bch_kill_switch(self):
        idx = self.source.find("OVERNIGHT_DISCOUNT_LIVE\n")
        self.assertGreater(idx, 0)
        window = self.source[idx:idx + 900]
        self.assertIn("ADA_15M_SHADOW", window)
        self.assertIn("BCH_15M_SHADOW", window)

    def test_decided_contracts_has_ada_bch_kill_switch(self):
        dc_assign = self.source.find("_dc_live_enabled = (")
        self.assertGreater(dc_assign, 0)
        dc_gate = self.source.find("if (_dc_live_enabled", dc_assign)
        self.assertGreater(dc_gate, dc_assign)
        window = self.source[dc_assign:dc_gate]
        self.assertIn("ADA_15M_SHADOW", window,
            "DC eligibility missing ADA_15M_SHADOW kill-switch")
        self.assertIn("BCH_15M_SHADOW", window,
            "DC eligibility missing BCH_15M_SHADOW kill-switch")


if __name__ == "__main__":
    unittest.main()
