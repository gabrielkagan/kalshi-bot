"""T1 regression tests for BNB asset onboarding (shadow observation).

These tests lock the atomic-activation safety invariant: if BNB is in
config.ASSETS, then the shadow gates and exclusion sets MUST contain it.
Partial reverts that leave ASSETS extended without the safety gates are
caught here (would otherwise risk live orders on an asset with no per-asset
risk sizing).

Mirrors tests/integration/test_doge_hype_onboarding_t1.py (HYPE/DOGE T1
SHIPPED 2026-05-10) with BNB-specific assertions, retargeted to current
HEAD (post-Bit-9.3-iii.c, post-Sprint-10 sibling-reorg, post-D1.6).

Plan:    agent_docs/bnb-t1-plan-may17.md
ClickUp: 86b9zmj0c
Umbrella: 86b9zmhyk
"""
from __future__ import annotations

import unittest


# ─── Ticker parser regression locks ──────────────────────────────────────
# These should be GREEN immediately — verify the existing suffix-strip
# parser correctly maps the new asset prefix. Locking in current behavior
# so a future parser refactor doesn't silently break BNB.

class TestTickerParserBnb(unittest.TestCase):
    def setUp(self):
        from bot.state import StateManager
        self.parse = StateManager._asset_from_ticker

    def test_bnb_15m_ticker(self):
        self.assertEqual(self.parse("KXBNB15M-26MAY171600-00"), "BNB")

    def test_bnb_15m_no_strike(self):
        self.assertEqual(self.parse("KXBNB15M-26MAY171600"), "BNB")

    def test_bnb_hourly_ticker(self):
        self.assertEqual(self.parse("KXBNBD-26MAY171600"), "BNB")

    def test_bnb_hourly_with_strike(self):
        self.assertEqual(self.parse("KXBNBD-26MAY1716-T844.99"), "BNB")

    def test_existing_assets_unchanged(self):
        # Regression: BNB rollout must not break existing parsers
        self.assertEqual(self.parse("KXBTC15M-26FEB211545-45"), "BTC")
        self.assertEqual(self.parse("KXETH15M-26FEB211545-45"), "ETH")
        self.assertEqual(self.parse("KXSOL15M-26FEB211545-45"), "SOL")
        self.assertEqual(self.parse("KXXRP15M-26FEB211545-45"), "XRP")
        self.assertEqual(self.parse("KXHYPE15M-26FEB211545-45"), "HYPE")
        self.assertEqual(self.parse("KXDOGE15M-26FEB211545-45"), "DOGE")
        self.assertEqual(self.parse("KXBTCD-26FEB211545"), "BTC")
        self.assertEqual(self.parse("KXXRPD-26FEB211545"), "XRP")


# ─── Atomic-activation safety invariant ──────────────────────────────────
# THE critical T1 contract: if ASSETS contains BNB then the safety
# gates MUST be wired. Partial reverts catch here.

class TestAtomicActivationSafety(unittest.TestCase):
    def test_assets_registry_contains_bnb(self):
        from bot.config import ASSETS
        self.assertIn("BNB", ASSETS, "BNB missing from bot.config.ASSETS")

    def test_bnb_15m_shadow_flag_defined(self):
        """T1 ship state: BNB_15M_SHADOW must exist and be True (shadow mode)."""
        from bot.constants import BNB_15M_SHADOW
        self.assertIsInstance(BNB_15M_SHADOW, bool)
        # During T1 shadow phase, must be True. At T4 promotion this assertion
        # flips to False — but the constant must remain (kill-switch).
        self.assertTrue(
            BNB_15M_SHADOW,
            "T1 invariant: BNB_15M_SHADOW must be True (shadow). "
            "If you're flipping to False, ensure T4 prereq constants are wired "
            "(BNB_MIN_ENTRY_PRICE, BNB_MAX_RISK_PER_TRADE, TM_ASSET_RISK_CAPS['BNB'])."
        )

    def test_bnb_t4_prereqs_wired_when_shadow_flag_false(self):
        """Post-T4 live promotion: when BNB_15M_SHADOW=False, all T4 prerequisite
        constants MUST be wired (per-asset MIN_ENTRY_PRICE + MAX_RISK_PER_TRADE +
        TM cap). If still in T1 shadow state, this test is satisfied trivially."""
        from bot.config import ASSETS
        if "BNB" not in ASSETS:
            self.skipTest("BNB not yet in ASSETS")
        from bot.constants import BNB_15M_SHADOW
        if BNB_15M_SHADOW:
            # Pre-T4 shadow state: T4 prereq constants existing or not is moot
            # because the asset never reaches the elif chains.
            return
        # Post-T4: every prereq must be defined and reasonable.
        from bot.constants import (
            BNB_MIN_ENTRY_PRICE,
            BNB_MAX_RISK_PER_TRADE,
            TM_ASSET_RISK_CAPS,
        )
        self.assertGreaterEqual(BNB_MIN_ENTRY_PRICE, 50,
            f"BNB_MIN_ENTRY_PRICE={BNB_MIN_ENTRY_PRICE} too low")
        self.assertLessEqual(BNB_MIN_ENTRY_PRICE, 99,
            f"BNB_MIN_ENTRY_PRICE={BNB_MIN_ENTRY_PRICE} too high")
        self.assertGreater(BNB_MAX_RISK_PER_TRADE, 0.0,
            f"BNB_MAX_RISK_PER_TRADE={BNB_MAX_RISK_PER_TRADE} must be > 0")
        self.assertLessEqual(BNB_MAX_RISK_PER_TRADE, 0.25,
            f"BNB_MAX_RISK_PER_TRADE={BNB_MAX_RISK_PER_TRADE} exceeds global 0.25 cap")
        self.assertIn("BNB", TM_ASSET_RISK_CAPS,
            "TM_ASSET_RISK_CAPS missing BNB — TM strategy bypasses per-asset sizing")

    def test_bnb_in_assets_implies_hourly_excluded(self):
        """If BNB is active, hourly YES-side must be excluded (no per-asset risk)."""
        from bot.config import ASSETS
        if "BNB" not in ASSETS:
            self.skipTest("BNB not yet in ASSETS")
        from bot.constants import HOURLY_EXCLUDED_ASSETS
        self.assertIn(
            "BNB", HOURLY_EXCLUDED_ASSETS,
            "SAFETY: BNB missing from HOURLY_EXCLUDED_ASSETS while in ASSETS — "
            "hourly YES-side would route live without per-asset risk constants."
        )

    def test_bnb_in_assets_implies_no_side_excluded(self):
        """NO-side hourly belt: BNB must also be in HOURLY_NO_EXCLUDED_ASSETS."""
        from bot.config import ASSETS
        if "BNB" not in ASSETS:
            self.skipTest("BNB not yet in ASSETS")
        from bot.constants import HOURLY_NO_EXCLUDED_ASSETS
        self.assertIn(
            "BNB", HOURLY_NO_EXCLUDED_ASSETS,
            "SAFETY: BNB missing from HOURLY_NO_EXCLUDED_ASSETS while in ASSETS — "
            "if HOURLY_NO_SIDE_LIVE=1 in prod, NO-side hourly routes live."
        )


# ─── Registry completeness ───────────────────────────────────────────────
# Every asset registry that drives observation must include BNB.

class TestRegistryCompleteness(unittest.TestCase):
    def test_series_tickers_complete(self):
        from bot.constants import SERIES_TICKERS
        self.assertEqual(SERIES_TICKERS["BNB"], "KXBNB15M")

    def test_hourly_series_tickers_complete(self):
        from bot.constants import HOURLY_SERIES_TICKERS
        self.assertEqual(HOURLY_SERIES_TICKERS["BNB"], "KXBNBD")

    def test_coinbase_products_complete(self):
        from bot.constants import COINBASE_PRODUCTS
        self.assertEqual(COINBASE_PRODUCTS["BNB"], "BNB-USD")

    def test_all_registries_have_same_asset_keys(self):
        """Cross-registry consistency: any asset in ASSETS must be in all
        feed registries."""
        from bot.config import ASSETS
        from bot.constants import (
            SERIES_TICKERS,
            HOURLY_SERIES_TICKERS,
            COINBASE_PRODUCTS,
        )
        for asset in ASSETS:
            self.assertIn(asset, SERIES_TICKERS, f"{asset} not in SERIES_TICKERS")
            self.assertIn(
                asset, HOURLY_SERIES_TICKERS,
                f"{asset} not in HOURLY_SERIES_TICKERS"
            )
            self.assertIn(asset, COINBASE_PRODUCTS, f"{asset} not in COINBASE_PRODUCTS")


# ─── Product-type categorizer ────────────────────────────────────────────
# Tickers must categorize to "15m" or "hourly" — otherwise downstream
# scan/settle logic drops them silently.

class TestProductTypeCategorizer(unittest.TestCase):
    """Verify the inline product_type categorizer in bot/state.py recognizes
    the new prefixes. Source-walk catches missing entries — behavior-level
    coverage via test_state_backfill_sql below."""

    def setUp(self):
        import bot.state
        import inspect
        self.source = inspect.getsource(bot.state)

    def test_bnb_15m_in_categorizer(self):
        self.assertIn(
            '"KXBNB15M"', self.source,
            "KXBNB15M not in inline categorizer tuple at bot/state.py "
            "(would silently drop BNB 15M settle rows)"
        )

    def test_bnb_hourly_in_categorizer(self):
        self.assertIn(
            '"KXBNBD"', self.source,
            "KXBNBD not in inline categorizer tuple at bot/state.py"
        )


# ─── Executor maker-block prefixes ───────────────────────────────────────
# Hourly tickers must NEVER be placed as maker orders (IOC only).

class TestExecutorHourlyPrefixes(unittest.TestCase):
    def test_bnb_hourly_in_maker_block_tuple(self):
        from bot.executor import OrderExecutor
        self.assertIn("KXBNBD-", OrderExecutor._HOURLY_SERIES_PREFIXES)


# ─── State backfill SQL ──────────────────────────────────────────────────
# Settled-trade backfill SQL must include the new ticker prefixes,
# otherwise their settle rows get product_type=NULL and silently drop.

class TestStateBackfillSQL(unittest.TestCase):
    """Read the state.py source and verify the LIKE clauses include the new
    prefixes. AST/source check (no DB roundtrip)."""

    def setUp(self):
        import bot.state
        import inspect
        self.source = inspect.getsource(bot.state)

    def test_15m_backfill_includes_bnb(self):
        # Two SQL sites: settled_trades + evaluated_opportunities
        count = self.source.count("KXBNB15M%")
        self.assertGreaterEqual(
            count, 2,
            f"KXBNB15M% missing from state.py SQL backfill "
            f"(found {count}, expected ≥ 2 sites: settled_trades + evaluated_opportunities)"
        )

    def test_hourly_backfill_includes_bnb(self):
        count = self.source.count("KXBNBD%")
        self.assertGreaterEqual(
            count, 2,
            f"KXBNBD% missing from state.py SQL backfill (found {count}, expected ≥ 2)"
        )


# ─── Scanner shadow gate wiring ──────────────────────────────────────────
# The HYPE/DOGE _15M_SHADOW gate pattern must be mirrored for BNB.
# Source-walk because gate behavior is deep in scan loop.

class TestScannerShadowGateWiring(unittest.TestCase):
    def setUp(self):
        import bot.scanner
        import inspect
        # bot.scanner is a package; OpportunityScanner lives in
        # bot/scanner/__init__.py
        self.source = inspect.getsource(bot.scanner)

    def test_bnb_15m_shadow_imported(self):
        self.assertIn(
            "BNB_15M_SHADOW", self.source,
            "BNB_15M_SHADOW not referenced in bot/scanner/__init__.py"
        )

    def test_bnb_shadow_strategy_name_present(self):
        # Strategy string for the shadow row insert
        self.assertIn(
            '"bnb_shadow"', self.source,
            'shadow row strategy=\'bnb_shadow\' not wired in scanner'
        )


# ─── Market config startup-assertion mirror ──────────────────────────────
# market_config.py hardcodes excluded_assets — must lock-step with
# bot/constants.py:HOURLY_EXCLUDED_ASSETS to avoid startup-assertion
# crash loop (CLAUDE.md rule).

class TestMarketConfigMirror(unittest.TestCase):
    def test_hourly_excluded_assets_mirror(self):
        from bot.constants import HOURLY_EXCLUDED_ASSETS
        from market_config import MARKET_CONFIGS
        cfg_h = MARKET_CONFIGS["hourly"]
        self.assertEqual(
            cfg_h.excluded_assets,
            frozenset(HOURLY_EXCLUDED_ASSETS),
            "market_config.py:144 excluded_assets does not match "
            "bot/constants.py:HOURLY_EXCLUDED_ASSETS — startup will crash."
        )

    def test_bnb_in_market_config_excluded(self):
        from market_config import MARKET_CONFIGS
        cfg_h = MARKET_CONFIGS["hourly"]
        self.assertIn("BNB", cfg_h.excluded_assets,
            "BNB missing from market_config.py:hourly.excluded_assets")


# ─── Strategy kill-switch clauses (RCA from HYPE/DOGE T1 R1 ─────────────
# These tests pin the SOURCE-LEVEL presence of `BNB_15M_SHADOW` clauses
# inside the TM / WKND / OVN / DC strategy eligibility checks. The XRP /
# HYPE / DOGE shadow gates downstream at bot/scanner/__init__.py:5975-6037
# fire AFTER Terminal Momentum (~:3303), Weekend Discount (~:3781),
# Overnight Discount (~:3943), and Decided Contracts (~:4207) have
# already appended candidates. Without the kill-switch clauses inside
# each strategy, flipping BNB_15M_SHADOW back to True at T4 rollback
# would have no effect on these strategies' live routing.

class TestStrategyKillSwitchClauses(unittest.TestCase):
    def setUp(self):
        import bot.scanner
        import inspect
        self.source = inspect.getsource(bot.scanner)

    def test_terminal_momentum_has_bnb_kill_switch_clause(self):
        tm_idx = self.source.find("if (TERMINAL_MOMENTUM_ENABLED")
        self.assertGreater(tm_idx, 0, "TM if-gate not found")
        tm_window = self.source[tm_idx:tm_idx + 1000]
        self.assertIn("BNB_15M_SHADOW", tm_window,
            "TM intercept gate missing BNB_15M_SHADOW kill-switch — "
            "flipping BNB_15M_SHADOW=True would NOT revert BNB from TM live routing")

    def test_weekend_discount_has_bnb_kill_switch_clause(self):
        wknd_idx = self.source.find("WEEKEND_DISCOUNT_LIVE\n")
        self.assertGreater(wknd_idx, 0, "WEEKEND_DISCOUNT_LIVE eligibility flag not found")
        wknd_window = self.source[wknd_idx:wknd_idx + 800]
        self.assertIn("BNB_15M_SHADOW", wknd_window)

    def test_overnight_discount_has_bnb_kill_switch_clause(self):
        ovn_idx = self.source.find("OVERNIGHT_DISCOUNT_LIVE\n")
        self.assertGreater(ovn_idx, 0)
        ovn_window = self.source[ovn_idx:ovn_idx + 800]
        self.assertIn("BNB_15M_SHADOW", ovn_window)

    def test_decided_contracts_has_bnb_kill_switch_clause(self):
        # DC shape: `_dc_live_enabled = (...)` initial assignment, with
        # HYPE/DOGE shadow flags as `or`-clauses that clear _dc_live_enabled
        # when the asset is in shadow. Anchor on the assignment region
        # before the `if (_dc_live_enabled` live-gate.
        dc_assign = self.source.find("_dc_live_enabled = (")
        self.assertGreater(dc_assign, 0)
        dc_gate = self.source.find("if (_dc_live_enabled", dc_assign)
        self.assertGreater(dc_gate, dc_assign)
        dc_window = self.source[dc_assign:dc_gate]
        self.assertIn("BNB_15M_SHADOW", dc_window,
            "DC eligibility missing BNB_15M_SHADOW kill-switch — "
            "flipping BNB_15M_SHADOW=True would NOT revert BNB from DC live")


if __name__ == "__main__":
    unittest.main()
