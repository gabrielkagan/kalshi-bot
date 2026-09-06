"""T1 regression tests for NEAR + ZEC asset onboarding (15M shadow).

Ticket 86bbvdc8y (2026-09-05). Locks the atomic-activation safety invariant:
if NEAR/ZEC are in config.ASSETS, the shadow gates and exclusion sets MUST
contain them. Partial reverts that leave ASSETS extended without the safety
gates are caught here (would otherwise risk live orders on an asset with no
per-asset risk sizing — there are NONE for NEAR/ZEC at T1, so live routing
must be fully gated). Belt + braces: ASSET_LIVE_TRADING[NEAR|ZEC]=False is
ALSO pinned here (bot/trading_mode.py second fail-safe).

Mirrors tests/integration/test_ada_bch_onboarding_t1.py (itself the shadow
half of the BNB T1 suite — NEAR/ZEC are T1 shadow, NOT T4 live, so the P2.x
exact-value / live-routing elif-chain classes are intentionally absent).

Pre-flight (2026-09-05):
  KXNEAR15M "NEAR 15m" fifteen_min / KXZEC15M "Zcash 15min" fifteen_min —
  both CF Benchmarks (NEARUSDRTI / ZECUSDRTI), live on Kalshi since
  2026-06-30 (6,326 settled windows each). Coinbase NEAR-USD / ZEC-USD
  status=online, trading_disabled=false (ZEC-USDC delisted). Hourly KXNEARD /
  KXZECD BOTH exist on Kalshi (unlike the ADA precedent) but are NOT
  subscribed (15M-only).

Tracked anchor: agent_docs/current_state.md (15M shadow assets). Local KB:
kb/decisions/new-15m-series-collection-plan-sep05.md,
kb/findings/new-15m-series-discovery-sep05.md.
"""
from __future__ import annotations

import unittest


# ─── Ticker parser regression locks ──────────────────────────────────────
class TestTickerParserNearZec(unittest.TestCase):
    def setUp(self):
        from bot.state import StateManager
        self.parse = StateManager._asset_from_ticker

    def test_near_15m_ticker(self):
        self.assertEqual(self.parse("KXNEAR15M-26SEP051600-00"), "NEAR")

    def test_near_15m_no_strike(self):
        self.assertEqual(self.parse("KXNEAR15M-26SEP051600"), "NEAR")

    def test_zec_15m_ticker(self):
        self.assertEqual(self.parse("KXZEC15M-26SEP051600-00"), "ZEC")

    def test_zec_15m_no_strike(self):
        self.assertEqual(self.parse("KXZEC15M-26SEP051600"), "ZEC")

    def test_zec_hourly_ticker(self):
        self.assertEqual(self.parse("KXZECD-26SEP0516-T2.24"), "ZEC")

    def test_near_hourly_ticker(self):
        # KXNEARD exists on Kalshi (verified 2026-09-05); the parser must map
        # it even though the bot never subscribes it (15M-only design).
        self.assertEqual(self.parse("KXNEARD-26SEP0516-T2.24"), "NEAR")

    def test_existing_assets_unchanged(self):
        self.assertEqual(self.parse("KXBTC15M-26FEB211545-45"), "BTC")
        self.assertEqual(self.parse("KXBNB15M-26FEB211545-45"), "BNB")
        self.assertEqual(self.parse("KXDOGE15M-26FEB211545-45"), "DOGE")
        self.assertEqual(self.parse("KXXRPD-26FEB211545"), "XRP")


# ─── Atomic-activation safety invariant ──────────────────────────────────
class TestAtomicActivationSafety(unittest.TestCase):
    def test_assets_registry_contains_near_and_zec(self):
        from bot.config import ASSETS
        self.assertIn("NEAR", ASSETS, "NEAR missing from bot.config.ASSETS")
        self.assertIn("ZEC", ASSETS, "ZEC missing from bot.config.ASSETS")

    def test_near_15m_shadow_flag_true(self):
        """T1 contract: NEAR observes in shadow (no live routing)."""
        from bot.constants import NEAR_15M_SHADOW
        self.assertIsInstance(NEAR_15M_SHADOW, bool)
        self.assertTrue(NEAR_15M_SHADOW,
            "NEAR_15M_SHADOW must be True at T1 — shadow observation only")

    def test_zec_15m_shadow_flag_true(self):
        from bot.constants import ZEC_15M_SHADOW
        self.assertIsInstance(ZEC_15M_SHADOW, bool)
        self.assertTrue(ZEC_15M_SHADOW,
            "ZEC_15M_SHADOW must be True at T1 — shadow observation only")

    def test_in_assets_implies_hourly_excluded(self):
        """SAFETY: if active, hourly YES-side must be excluded (no per-asset risk)."""
        from bot.constants import HOURLY_EXCLUDED_ASSETS
        self.assertIn("NEAR", HOURLY_EXCLUDED_ASSETS,
            "SAFETY: NEAR missing from HOURLY_EXCLUDED_ASSETS while in ASSETS")
        self.assertIn("ZEC", HOURLY_EXCLUDED_ASSETS,
            "SAFETY: ZEC missing from HOURLY_EXCLUDED_ASSETS while in ASSETS")

    def test_in_assets_implies_no_side_excluded(self):
        from bot.constants import HOURLY_NO_EXCLUDED_ASSETS
        self.assertIn("NEAR", HOURLY_NO_EXCLUDED_ASSETS,
            "SAFETY: NEAR missing from HOURLY_NO_EXCLUDED_ASSETS")
        self.assertIn("ZEC", HOURLY_NO_EXCLUDED_ASSETS,
            "SAFETY: ZEC missing from HOURLY_NO_EXCLUDED_ASSETS")

    def test_trading_mode_gate_lists_near_zec_shadow(self):
        """Second fail-safe (bot/trading_mode.py, 2026-05-30): the per-asset
        live map must list NEAR/ZEC explicitly (an unlisted series BYPASSES
        the ticker → asset resolver) and the SHIPPED value must be False.
        tests/integration/conftest.py monkeypatches the runtime map to all-True
        for the integration suite, so the shipped value is pinned from SOURCE
        (same technique as tests/unit/test_trading_mode.py's shipped-default pin)."""
        import inspect
        from bot import constants as C
        for a in ("NEAR", "ZEC"):
            self.assertIn(a, C.ASSET_LIVE_TRADING, f"{a} missing from ASSET_LIVE_TRADING")
        src = inspect.getsource(C)
        block = src[src.index("ASSET_LIVE_TRADING = {"):]
        block = block[:block.index("}")]
        for a in ("NEAR", "ZEC"):
            self.assertRegex(block, rf'"{a}":\s*False',
                f"ASSET_LIVE_TRADING[{a}] must ship as False (shadow) at T1")


    def test_near_zec_outside_engine_live_universes(self):
        """The ONLY currently-live order paths are the longshot/twaplock
        override legs, gated by ``asset in LONGSHOT_LIVE_ASSETS`` /
        ``TWAPLOCK_LIVE_ASSETS`` (validated universes; ADA/BCH precedent).
        NEAR/ZEC have real settled windows (unlike ADA/BCH at their T1), so
        this path WILL be exercised on first boot — pin the exclusion."""
        from bot import constants as C
        for a in ("NEAR", "ZEC"):
            self.assertNotIn(a, C.LONGSHOT_LIVE_ASSETS)
            self.assertNotIn(a, C.TWAPLOCK_LIVE_ASSETS)

    def test_engine_overrides_do_not_make_near_zec_live(self):
        """Even with BOTH engine overrides ON, strategy_is_live must stay
        False for NEAR/ZEC (mirror of test_twaplock_strategy's ADA/BCH pin)."""
        from unittest import mock
        from bot import constants as C
        import bot.trading_mode as tm
        with mock.patch.object(C, "GLOBAL_LIVE_TRADING", False), \
             mock.patch.object(C, "LONGSHOT_LIVE_OVERRIDE", True), \
             mock.patch.object(C, "TWAPLOCK_LIVE_OVERRIDE", True):
            for a in ("NEAR", "ZEC"):
                self.assertFalse(tm.strategy_is_live("longshot", a))
                self.assertFalse(tm.strategy_is_live("twaplock", a))
                self.assertFalse(tm.strategy_is_live("above", a))

    def test_place_order_backstop_refuses_engine_orders_on_near_zec(self):
        """KalshiClient.place_order backstop (executor-independent chokepoint)
        must refuse ls-/tw- orders on KXNEAR15M/KXZEC15M with overrides ON."""
        from unittest import mock
        from bot import constants as C
        from bot.kalshi_client import KalshiClient
        with mock.patch.object(C, "GLOBAL_LIVE_TRADING", False), \
             mock.patch.object(C, "LONGSHOT_LIVE_OVERRIDE", True), \
             mock.patch.object(C, "TWAPLOCK_LIVE_OVERRIDE", True):
            for ticker in ("KXNEAR15M-26SEP051600-00", "KXZEC15M-26SEP051600-00"):
                for oid in ("ls-x1", "tw-x1"):
                    client = mock.MagicMock()
                    result = KalshiClient.place_order(
                        client, ticker, "yes", "buy", 1, yes_price=95,
                        client_order_id=oid)
                    self.assertIsNone(result, f"{oid} on {ticker} must be refused")
                    client._request.assert_not_called()


# ─── Registry completeness ───────────────────────────────────────────────
class TestRegistryCompleteness(unittest.TestCase):
    def test_series_tickers_complete(self):
        from bot.constants import SERIES_TICKERS
        self.assertEqual(SERIES_TICKERS["NEAR"], "KXNEAR15M")
        self.assertEqual(SERIES_TICKERS["ZEC"], "KXZEC15M")

    def test_hourly_series_tickers_complete(self):
        from bot.constants import HOURLY_SERIES_TICKERS
        # Both hourly series exist on Kalshi (verified 2026-09-05) but are NOT
        # subscribed — 15M-only design; entries satisfy the cross-registry invariant.
        self.assertEqual(HOURLY_SERIES_TICKERS["NEAR"], "KXNEARD")
        self.assertEqual(HOURLY_SERIES_TICKERS["ZEC"], "KXZECD")

    def test_coinbase_products_complete(self):
        from bot.constants import COINBASE_PRODUCTS
        self.assertEqual(COINBASE_PRODUCTS["NEAR"], "NEAR-USD")
        self.assertEqual(COINBASE_PRODUCTS["ZEC"], "ZEC-USD")

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
        import inspect
        import bot.state
        self.source = inspect.getsource(bot.state)

    def test_near_15m_in_categorizer(self):
        self.assertIn('"KXNEAR15M"', self.source,
            "KXNEAR15M not in inline categorizer (would drop NEAR 15M settle rows)")

    def test_zec_15m_in_categorizer(self):
        self.assertIn('"KXZEC15M"', self.source)

    def test_zec_hourly_in_categorizer(self):
        self.assertIn('"KXZECD"', self.source)

    def test_near_hourly_in_categorizer(self):
        self.assertIn('"KXNEARD"', self.source)


# ─── Executor maker-block prefixes ───────────────────────────────────────
class TestExecutorHourlyPrefixes(unittest.TestCase):
    def test_hourly_prefixes_include_near_zec(self):
        from bot.executor import OrderExecutor
        self.assertIn("KXZECD-", OrderExecutor._HOURLY_SERIES_PREFIXES)
        self.assertIn("KXNEARD-", OrderExecutor._HOURLY_SERIES_PREFIXES)


# ─── State backfill SQL ──────────────────────────────────────────────────
class TestStateBackfillSQL(unittest.TestCase):
    def setUp(self):
        import inspect
        import bot.state
        self.source = inspect.getsource(bot.state)

    def test_15m_backfill_includes_near(self):
        self.assertGreaterEqual(self.source.count("KXNEAR15M%"), 2,
            "KXNEAR15M% missing from state.py SQL backfill (expected ≥2 sites)")

    def test_15m_backfill_includes_zec(self):
        self.assertGreaterEqual(self.source.count("KXZEC15M%"), 2,
            "KXZEC15M% missing from state.py SQL backfill (expected ≥2 sites)")

    def test_hourly_backfill_includes_zec(self):
        self.assertGreaterEqual(self.source.count("KXZECD%"), 2,
            "KXZECD% missing from state.py SQL backfill (expected ≥2 sites)")

    def test_hourly_backfill_includes_near(self):
        self.assertGreaterEqual(self.source.count("KXNEARD%"), 2,
            "KXNEARD% missing from state.py SQL backfill (expected ≥2 sites)")


# ─── Spot-at-decision columns (close the BNB-T1 gap) ─────────────────────
class TestSpotColumnsWired(unittest.TestCase):
    """NEAR/ZEC spot columns must be wired end-to-end in state.py so the
    scanner's ASSETS-driven dict-comp value is persisted rather than
    silently dropped (the gap that became BNB followup 86b9zn5pq)."""

    def setUp(self):
        import inspect
        import bot.state
        self.source = inspect.getsource(bot.state)

    def test_near_spot_column_present(self):
        # ≥4 sites: schema, kwarg, _ext fallback, INSERT/ON-CONFLICT.
        self.assertGreaterEqual(self.source.count("near_spot_at_decision"), 4,
            "near_spot_at_decision not fully wired in state.py")

    def test_zec_spot_column_present(self):
        self.assertGreaterEqual(self.source.count("zec_spot_at_decision"), 4,
            "zec_spot_at_decision not fully wired in state.py")

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
        import inspect
        import bot.scanner
        self.source = inspect.getsource(bot.scanner)

    def test_near_shadow_imported(self):
        self.assertIn("NEAR_15M_SHADOW", self.source,
            "NEAR_15M_SHADOW not referenced in bot/scanner/__init__.py")

    def test_zec_shadow_imported(self):
        self.assertIn("ZEC_15M_SHADOW", self.source)

    def test_near_shadow_strategy_name_present(self):
        self.assertIn('"near_shadow"', self.source,
            "shadow row strategy='near_shadow' not wired in scanner")

    def test_zec_shadow_strategy_name_present(self):
        self.assertIn('"zec_shadow"', self.source)

    def test_no_side_shadow_stages_present(self):
        self.assertIn('"no_side_near_shadow"', self.source)
        self.assertIn('"no_side_zec_shadow"', self.source)


# ─── Market config startup-assert mirror ─────────────────────────────────
class TestMarketConfigMirror(unittest.TestCase):
    def test_hourly_excluded_assets_mirror(self):
        from bot.constants import HOURLY_EXCLUDED_ASSETS
        from market_config import MARKET_CONFIGS
        cfg_h = MARKET_CONFIGS["hourly"]
        self.assertEqual(cfg_h.excluded_assets, frozenset(HOURLY_EXCLUDED_ASSETS),
            "market_config.py excluded_assets != HOURLY_EXCLUDED_ASSETS — startup crash")

    def test_near_zec_in_market_config_excluded(self):
        from market_config import MARKET_CONFIGS
        cfg_h = MARKET_CONFIGS["hourly"]
        self.assertIn("NEAR", cfg_h.excluded_assets)
        self.assertIn("ZEC", cfg_h.excluded_assets)


# ─── Strategy kill-switch clauses (RCA Finding 2 — L96) ──────────────────
class TestStrategyKillSwitchClauses(unittest.TestCase):
    def setUp(self):
        import inspect
        import bot.scanner
        self.source = inspect.getsource(bot.scanner)

    def test_terminal_momentum_has_near_zec_kill_switch(self):
        idx = self.source.find("if (TERMINAL_MOMENTUM_ENABLED")
        self.assertGreater(idx, 0, "TM if-gate not found")
        # 1200 → 1600: each onboarded shadow asset adds a kill-switch line here.
        window = self.source[idx:idx + 1600]
        self.assertIn("NEAR_15M_SHADOW", window,
            "TM gate missing NEAR_15M_SHADOW kill-switch")
        self.assertIn("ZEC_15M_SHADOW", window,
            "TM gate missing ZEC_15M_SHADOW kill-switch")

    def test_weekend_discount_has_near_zec_kill_switch(self):
        idx = self.source.find("WEEKEND_DISCOUNT_LIVE\n")
        self.assertGreater(idx, 0, "WEEKEND_DISCOUNT_LIVE flag not found")
        # 900 → 1100: measured offset of the first ZEC_15M_SHADOW from this
        # anchor is 871 (match ends 885), i.e. the inherited 900 was 14 chars
        # from silently going RED on the next onboarding. 1100 restores slack.
        window = self.source[idx:idx + 1100]
        self.assertIn("NEAR_15M_SHADOW", window)
        self.assertIn("ZEC_15M_SHADOW", window)

    def test_overnight_discount_has_near_zec_kill_switch(self):
        idx = self.source.find("OVERNIGHT_DISCOUNT_LIVE\n")
        self.assertGreater(idx, 0)
        # 900 → 1100: measured first ZEC_15M_SHADOW offset here is 873
        # (match ends 887) — same 14-char margin as the weekend gate above.
        window = self.source[idx:idx + 1100]
        self.assertIn("NEAR_15M_SHADOW", window)
        self.assertIn("ZEC_15M_SHADOW", window)

    def test_decided_contracts_has_near_zec_kill_switch(self):
        dc_assign = self.source.find("_dc_live_enabled = (")
        self.assertGreater(dc_assign, 0)
        dc_gate = self.source.find("if (_dc_live_enabled", dc_assign)
        self.assertGreater(dc_gate, dc_assign)
        window = self.source[dc_assign:dc_gate]
        self.assertIn("NEAR_15M_SHADOW", window,
            "DC eligibility missing NEAR_15M_SHADOW kill-switch")
        self.assertIn("ZEC_15M_SHADOW", window,
            "DC eligibility missing ZEC_15M_SHADOW kill-switch")


if __name__ == "__main__":
    unittest.main()
