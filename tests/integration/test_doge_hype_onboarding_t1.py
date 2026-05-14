"""T1 regression tests for HYPE + DOGE asset onboarding (shadow observation).

These tests lock the atomic-activation safety invariant: if HYPE/DOGE are in
config.ASSETS, then the shadow gates and exclusion sets MUST contain them.
Partial reverts that leave ASSETS extended without the safety gates are
caught here (would otherwise risk live orders on assets with no per-asset
risk sizing).

Spike: agent_docs/asset-onboarding-doge-hype-spike.md (9 adversarial rounds)
Plan:  agent_docs/doge-hype-t1-plan-may10.md
ClickUp: 86b9vecw9
"""
from __future__ import annotations

import unittest


# ─── Ticker parser regression locks ──────────────────────────────────────
# These should be GREEN immediately — verify the existing suffix-strip
# parser correctly maps the new asset prefixes. Locking in current
# behavior so a future parser refactor doesn't silently break HYPE/DOGE.

class TestTickerParserHypeDoge(unittest.TestCase):
    def setUp(self):
        from bot.state import StateManager
        self.parse = StateManager._asset_from_ticker

    def test_hype_15m_ticker(self):
        self.assertEqual(self.parse("KXHYPE15M-26MAY091300-00"), "HYPE")

    def test_hype_15m_no_strike(self):
        self.assertEqual(self.parse("KXHYPE15M-26MAY091300"), "HYPE")

    def test_hype_hourly_ticker(self):
        self.assertEqual(self.parse("KXHYPED-26MAY091300"), "HYPE")

    def test_doge_15m_ticker(self):
        self.assertEqual(self.parse("KXDOGE15M-26MAY091300-00"), "DOGE")

    def test_doge_hourly_ticker(self):
        self.assertEqual(self.parse("KXDOGED-26MAY091300"), "DOGE")

    def test_existing_assets_unchanged(self):
        # Regression: don't let HYPE/DOGE rollout break existing parsers
        self.assertEqual(self.parse("KXBTC15M-26FEB211545-45"), "BTC")
        self.assertEqual(self.parse("KXETH15M-26FEB211545-45"), "ETH")
        self.assertEqual(self.parse("KXSOL15M-26FEB211545-45"), "SOL")
        self.assertEqual(self.parse("KXXRP15M-26FEB211545-45"), "XRP")
        self.assertEqual(self.parse("KXBTCD-26FEB211545"), "BTC")
        self.assertEqual(self.parse("KXXRPD-26FEB211545"), "XRP")


# ─── Atomic-activation safety invariant ──────────────────────────────────
# THE critical T1 contract: if ASSETS contains HYPE/DOGE then the safety
# gates MUST be wired. Partial reverts catch here.

class TestAtomicActivationSafety(unittest.TestCase):
    def test_assets_registry_contains_hype_doge(self):
        from bot.config import ASSETS
        self.assertIn("HYPE", ASSETS, "HYPE missing from config.ASSETS")
        self.assertIn("DOGE", ASSETS, "DOGE missing from config.ASSETS")

    def test_hype_t4_prereqs_wired_when_shadow_flag_false(self):
        """Post-T4 live promotion (P2.3, 2026-05-14, ClickUp 86b9xv66a):
        when HYPE_15M_SHADOW=False, all T4 prerequisite constants MUST be
        wired (per-asset MIN_ENTRY_PRICE + MAX_RISK_PER_TRADE + TM cap).
        If a future Bit flips the flag back to True (shadow), this assertion
        is satisfied trivially (the contract only fires post-flip)."""
        from bot.config import ASSETS
        if "HYPE" not in ASSETS:
            self.skipTest("HYPE not yet in ASSETS")
        from bot.constants import (
            HYPE_15M_SHADOW,
            HYPE_MIN_ENTRY_PRICE,
            HYPE_MAX_RISK_PER_TRADE,
            TM_ASSET_RISK_CAPS,
        )
        if HYPE_15M_SHADOW:
            # Pre-T4 shadow state: the T4 prereq constants existing or not
            # is moot because the asset never reaches the elif chains.
            return
        # Post-T4: every prereq must be defined and reasonable.
        self.assertGreaterEqual(HYPE_MIN_ENTRY_PRICE, 50,
            f"HYPE_MIN_ENTRY_PRICE={HYPE_MIN_ENTRY_PRICE} too low")
        self.assertLessEqual(HYPE_MIN_ENTRY_PRICE, 99,
            f"HYPE_MIN_ENTRY_PRICE={HYPE_MIN_ENTRY_PRICE} too high")
        self.assertGreater(HYPE_MAX_RISK_PER_TRADE, 0.0,
            f"HYPE_MAX_RISK_PER_TRADE={HYPE_MAX_RISK_PER_TRADE} must be > 0")
        self.assertLessEqual(HYPE_MAX_RISK_PER_TRADE, 0.25,
            f"HYPE_MAX_RISK_PER_TRADE={HYPE_MAX_RISK_PER_TRADE} exceeds global 0.25 cap")
        self.assertIn("HYPE", TM_ASSET_RISK_CAPS,
            "TM_ASSET_RISK_CAPS missing HYPE — TM strategy bypasses per-asset sizing")

    def test_doge_t4_prereqs_wired_when_shadow_flag_false(self):
        from bot.config import ASSETS
        if "DOGE" not in ASSETS:
            self.skipTest("DOGE not yet in ASSETS")
        from bot.constants import (
            DOGE_15M_SHADOW,
            DOGE_MIN_ENTRY_PRICE,
            DOGE_MAX_RISK_PER_TRADE,
            TM_ASSET_RISK_CAPS,
        )
        if DOGE_15M_SHADOW:
            return
        self.assertGreaterEqual(DOGE_MIN_ENTRY_PRICE, 50,
            f"DOGE_MIN_ENTRY_PRICE={DOGE_MIN_ENTRY_PRICE} too low")
        self.assertLessEqual(DOGE_MIN_ENTRY_PRICE, 99,
            f"DOGE_MIN_ENTRY_PRICE={DOGE_MIN_ENTRY_PRICE} too high")
        self.assertGreater(DOGE_MAX_RISK_PER_TRADE, 0.0)
        self.assertLessEqual(DOGE_MAX_RISK_PER_TRADE, 0.25)
        self.assertIn("DOGE", TM_ASSET_RISK_CAPS,
            "TM_ASSET_RISK_CAPS missing DOGE")

    def test_hype_in_assets_implies_hourly_excluded(self):
        """If HYPE is active, hourly YES-side must be excluded (no per-asset risk)."""
        from bot.config import ASSETS
        if "HYPE" not in ASSETS:
            self.skipTest("HYPE not yet in ASSETS")
        from bot.constants import HOURLY_EXCLUDED_ASSETS
        self.assertIn(
            "HYPE", HOURLY_EXCLUDED_ASSETS,
            "SAFETY: HYPE missing from HOURLY_EXCLUDED_ASSETS while in ASSETS — "
            "hourly YES-side would route live without per-asset risk constants."
        )

    def test_doge_in_assets_implies_hourly_excluded(self):
        from bot.config import ASSETS
        if "DOGE" not in ASSETS:
            self.skipTest("DOGE not yet in ASSETS")
        from bot.constants import HOURLY_EXCLUDED_ASSETS
        self.assertIn("DOGE", HOURLY_EXCLUDED_ASSETS)

    def test_hype_in_assets_implies_no_side_excluded(self):
        """NO-side hourly belt: HYPE must also be in HOURLY_NO_EXCLUDED_ASSETS."""
        from bot.config import ASSETS
        if "HYPE" not in ASSETS:
            self.skipTest("HYPE not yet in ASSETS")
        from bot.constants import HOURLY_NO_EXCLUDED_ASSETS
        self.assertIn(
            "HYPE", HOURLY_NO_EXCLUDED_ASSETS,
            "SAFETY: HYPE missing from HOURLY_NO_EXCLUDED_ASSETS while in ASSETS — "
            "if HOURLY_NO_SIDE_LIVE=1 in prod, NO-side hourly routes live."
        )

    def test_doge_in_assets_implies_no_side_excluded(self):
        from bot.config import ASSETS
        if "DOGE" not in ASSETS:
            self.skipTest("DOGE not yet in ASSETS")
        from bot.constants import HOURLY_NO_EXCLUDED_ASSETS
        self.assertIn("DOGE", HOURLY_NO_EXCLUDED_ASSETS)


# ─── Registry completeness ───────────────────────────────────────────────
# Every asset registry that drives observation must include HYPE/DOGE.

class TestRegistryCompleteness(unittest.TestCase):
    def test_series_tickers_complete(self):
        from bot.constants import SERIES_TICKERS
        self.assertEqual(SERIES_TICKERS["HYPE"], "KXHYPE15M")
        self.assertEqual(SERIES_TICKERS["DOGE"], "KXDOGE15M")

    def test_hourly_series_tickers_complete(self):
        from bot.constants import HOURLY_SERIES_TICKERS
        self.assertEqual(HOURLY_SERIES_TICKERS["HYPE"], "KXHYPED")
        self.assertEqual(HOURLY_SERIES_TICKERS["DOGE"], "KXDOGED")

    def test_coinbase_products_complete(self):
        from bot.constants import COINBASE_PRODUCTS
        self.assertEqual(COINBASE_PRODUCTS["HYPE"], "HYPE-USD")
        self.assertEqual(COINBASE_PRODUCTS["DOGE"], "DOGE-USD")

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

    def test_hype_15m_in_categorizer(self):
        # Anchor on the bare prefix (without %) — the inline categorizer
        # at bot/state.py:1311 uses startswith() with this string.
        self.assertIn(
            '"KXHYPE15M"', self.source,
            "KXHYPE15M not in inline categorizer tuple at bot/state.py "
            "(would silently drop HYPE 15M settle rows)"
        )

    def test_hype_hourly_in_categorizer(self):
        self.assertIn(
            '"KXHYPED"', self.source,
            "KXHYPED not in inline categorizer tuple at bot/state.py"
        )

    def test_doge_15m_in_categorizer(self):
        self.assertIn('"KXDOGE15M"', self.source)

    def test_doge_hourly_in_categorizer(self):
        self.assertIn('"KXDOGED"', self.source)


# ─── Executor maker-block prefixes ───────────────────────────────────────
# Hourly tickers must NEVER be placed as maker orders (IOC only).

class TestExecutorHourlyPrefixes(unittest.TestCase):
    def test_hype_hourly_in_maker_block_tuple(self):
        from bot.executor import OrderExecutor
        self.assertIn("KXHYPED-", OrderExecutor._HOURLY_SERIES_PREFIXES)

    def test_doge_hourly_in_maker_block_tuple(self):
        from bot.executor import OrderExecutor
        self.assertIn("KXDOGED-", OrderExecutor._HOURLY_SERIES_PREFIXES)


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

    def test_15m_backfill_includes_hype(self):
        # Two sites: settled_trades + evaluated_opportunities
        count = self.source.count("KXHYPE15M%")
        self.assertGreaterEqual(
            count, 2,
            f"KXHYPE15M% missing from state.py SQL backfill "
            f"(found {count}, expected ≥ 2 sites: settled_trades + evaluated_opportunities)"
        )

    def test_15m_backfill_includes_doge(self):
        count = self.source.count("KXDOGE15M%")
        self.assertGreaterEqual(count, 2)

    def test_hourly_backfill_includes_hype(self):
        count = self.source.count("KXHYPED%")
        self.assertGreaterEqual(count, 2)

    def test_hourly_backfill_includes_doge(self):
        count = self.source.count("KXDOGED%")
        self.assertGreaterEqual(count, 2)


# ─── Scanner shadow gate wiring ──────────────────────────────────────────
# The XRP_15M_SHADOW gate pattern must be mirrored for HYPE and DOGE.
# Source-walk because gate behavior is deep in scan loop.

class TestScannerShadowGateWiring(unittest.TestCase):
    def setUp(self):
        import bot.scanner
        import inspect
        # bot.scanner is a package; OpportunityScanner lives in
        # bot/scanner/__init__.py
        self.source = inspect.getsource(bot.scanner)

    def test_hype_15m_shadow_imported(self):
        self.assertIn(
            "HYPE_15M_SHADOW", self.source,
            "HYPE_15M_SHADOW not referenced in bot/scanner/__init__.py"
        )

    def test_doge_15m_shadow_imported(self):
        self.assertIn("DOGE_15M_SHADOW", self.source)

    def test_hype_shadow_strategy_name_present(self):
        # Strategy string for the shadow row insert
        self.assertIn(
            '"hype_shadow"', self.source,
            'shadow row strategy=\'hype_shadow\' not wired in scanner'
        )

    def test_doge_shadow_strategy_name_present(self):
        self.assertIn('"doge_shadow"', self.source)


# ─── Market config startup-assertion mirror ──────────────────────────────
# market_config.py:117 hardcodes excluded_assets — must lock-step with
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
            "market_config.py:117 excluded_assets does not match "
            "bot/constants.py:HOURLY_EXCLUDED_ASSETS — startup will crash."
        )


# ─── Strategy kill-switch clauses (RCA from adversarial review R1) ───────
# These tests pin the SOURCE-LEVEL presence of `HYPE_15M_SHADOW` /
# `DOGE_15M_SHADOW` clauses inside the TM / WKND / OVN / DC strategy
# eligibility checks. The clauses serve dual roles depending on flag state:
#   - Pre-T4 (flags True, before P2.3 ship 2026-05-14): gates exclude
#     HYPE/DOGE from these strategies' candidate.append sites.
#   - Post-T4 (flags False, current state): gates degrade to True and
#     allow HYPE/DOGE through TM/WKND/OVN/DC live; the clauses are
#     preserved as KILL-SWITCHES — flip the flag to revert the asset.
# The XRP_15M_SHADOW gate at bot/scanner/__init__.py:5867 fires AFTER
# Terminal Momentum (~:3303), Weekend Discount (~:3781), Overnight Discount
# (~:3943), and Decided Contracts (~:4207) have already appended candidates.
# Without the kill-switch clauses inside each strategy, flipping a shadow
# flag back to True would have no effect on these strategies' live routing.

class TestStrategyKillSwitchClauses(unittest.TestCase):
    def setUp(self):
        import bot.scanner
        import inspect
        self.source = inspect.getsource(bot.scanner)

    def test_terminal_momentum_has_hype_doge_kill_switch_clauses(self):
        # TM intercept must include "and not (HYPE_15M_SHADOW and asset == \"HYPE\")"
        # near the TERMINAL_MOMENTUM_ENABLED check. Anchor on the if-condition (not
        # the import) by requiring "if (TERMINAL_MOMENTUM_ENABLED" prefix.
        tm_idx = self.source.find("if (TERMINAL_MOMENTUM_ENABLED")
        self.assertGreater(tm_idx, 0, "TM if-gate not found")
        # Look 1000 chars forward to cover the full if-chain
        tm_window = self.source[tm_idx:tm_idx + 1000]
        self.assertIn("HYPE_15M_SHADOW", tm_window,
            "TM intercept gate at scanner:~3303 missing HYPE_15M_SHADOW kill-switch — "
            "flipping HYPE_15M_SHADOW=True would NOT revert HYPE from TM live routing")
        self.assertIn("DOGE_15M_SHADOW", tm_window,
            "TM intercept gate missing DOGE_15M_SHADOW kill-switch")

    def test_weekend_discount_has_hype_doge_kill_switch_clauses(self):
        wknd_idx = self.source.find("WEEKEND_DISCOUNT_LIVE\n")
        self.assertGreater(wknd_idx, 0, "WEEKEND_DISCOUNT_LIVE eligibility flag not found")
        wknd_window = self.source[wknd_idx:wknd_idx + 800]
        self.assertIn("HYPE_15M_SHADOW", wknd_window)
        self.assertIn("DOGE_15M_SHADOW", wknd_window)

    def test_overnight_discount_has_hype_doge_kill_switch_clauses(self):
        ovn_idx = self.source.find("OVERNIGHT_DISCOUNT_LIVE\n")
        self.assertGreater(ovn_idx, 0)
        ovn_window = self.source[ovn_idx:ovn_idx + 800]
        self.assertIn("HYPE_15M_SHADOW", ovn_window)
        self.assertIn("DOGE_15M_SHADOW", ovn_window)

    def test_decided_contracts_has_hype_doge_kill_switch_clauses(self):
        # DC shape: `_dc_live_enabled = (...)` initial assignment, then a
        # short shadow-flag clear-block (`if HYPE/DOGE shadow: _dc_live_enabled = False`),
        # then the `if (_dc_live_enabled and not OBSERVATION_MODE...)` gate.
        # Anchor on the post-assignment region between the initial `=` and the
        # live-gate `if (_dc_live_enabled`.
        dc_assign = self.source.find("_dc_live_enabled = (")
        self.assertGreater(dc_assign, 0)
        dc_gate = self.source.find("if (_dc_live_enabled", dc_assign)
        self.assertGreater(dc_gate, dc_assign)
        dc_window = self.source[dc_assign:dc_gate]
        self.assertIn("HYPE_15M_SHADOW", dc_window,
            "DC eligibility missing HYPE_15M_SHADOW kill-switch clear-flag — "
            "flipping HYPE_15M_SHADOW=True would NOT revert HYPE from DC live")
        self.assertIn("DOGE_15M_SHADOW", dc_window)


if __name__ == "__main__":
    unittest.main()
