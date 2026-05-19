"""T1 + T4 regression tests for BNB asset onboarding.

These tests lock the atomic-activation safety invariant: if BNB is in
config.ASSETS, then the shadow gates and exclusion sets MUST contain it.
Partial reverts that leave ASSETS extended without the safety gates are
caught here (would otherwise risk live orders on an asset with no per-asset
risk sizing).

Mirrors tests/integration/test_doge_hype_onboarding_t1.py (HYPE/DOGE T1
SHIPPED 2026-05-10, T4 SHIPPED 2026-05-14 as P2.3 promotion) with
BNB-specific assertions, retargeted to current HEAD (post-Bit-9.3-iii.c,
post-Sprint-10 sibling-reorg, post-D1.6).

**Post-P2.4 (BNB live promotion, 2026-05-19, sibling to P2.3 86b9xv66a):**
the `test_bnb_15m_shadow_flag_defined` T1-only assertion was REMOVED
(matching HYPE/DOGE post-T4 file shape). T4 contract is enforced via
`test_bnb_t4_prereqs_wired_when_shadow_flag_false` (returns early
trivially if flag still True, asserts when False). The
`TestBnbP24ConstantValues` and `TestBnbP24ScannerLiveRouting` classes pin
exact post-P2.4 values + scanner elif-chain wiring.

Plan:    agent_docs/bnb-t1-plan-may17.md, kb/decisions/p2-4-bnb-live-promotion-plan.md
ClickUp: 86b9zmj0c (T1), 86b9zmj37 (T4 P2.4)
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

    def test_bnb_15m_shadow_flag_is_bool(self):
        """Post-P2.4 (2026-05-19): the T1 `assertTrue(BNB_15M_SHADOW)` assertion
        was retired matching HYPE/DOGE post-T4 file shape. Kill-switch type
        contract preserved — flag must remain a bool so flipping back to True
        (rollback) works without surprise."""
        from bot.constants import BNB_15M_SHADOW
        self.assertIsInstance(BNB_15M_SHADOW, bool)

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


# ─── P2.4 BNB live promotion — exact-value pins ──────────────────────────
# Post-2026-05-19 P2.4 ship: pin the 5 prereq constants to their
# data-justified values. Plan doc: kb/decisions/p2-4-bnb-live-promotion-plan.md.
# Brier sweep: w=0.20 argmin at n=721. Per-tier WR: 90c+ 100% WR n=272.
# Risk cap: HYPE/DOGE conservative-new-asset precedent (0.10).
# NBBO: analog default mirror of ETH (90, 99, 300.0).

class TestBnbP24ConstantValues(unittest.TestCase):
    """Exact-value pins for P2.4. These guard against silent edits that
    drift the constants away from their data justifications without
    re-running the sweep methodology."""

    def test_bnb_min_entry_price_value(self):
        from bot.constants import BNB_MIN_ENTRY_PRICE
        # Per-tier WR analysis (kb/decisions/p2-4-bnb-live-promotion-plan.md
        # § RCA #1): 85-89c BNB is +0.08c naive (sub-fee EV); 90c+ is +7.60c
        # at 100% WR n=272 (exceeds HYPE n=250 precedent).
        self.assertEqual(BNB_MIN_ENTRY_PRICE, 90)

    def test_bnb_max_risk_per_trade_value(self):
        from bot.constants import BNB_MAX_RISK_PER_TRADE
        # HYPE/DOGE conservative-new-asset precedent (bot/constants.py:103-104).
        self.assertEqual(BNB_MAX_RISK_PER_TRADE, 0.10)

    def test_tm_asset_risk_caps_bnb_entry(self):
        from bot.constants import TM_ASSET_RISK_CAPS, BNB_MAX_RISK_PER_TRADE
        # Mechanical mirror: TM must not bypass the per-asset cap.
        self.assertIn("BNB", TM_ASSET_RISK_CAPS)
        self.assertEqual(TM_ASSET_RISK_CAPS["BNB"], BNB_MAX_RISK_PER_TRADE)

    def test_market_blend_w_by_asset_bnb_value(self):
        from bot.constants import MARKET_BLEND_W_BY_ASSET
        # B.1-equivalent Brier sweep argmin: w=0.20 at n=721 settled
        # evaluations (kb/decisions/p2-4-bnb-live-promotion-plan.md § RCA #4).
        # Matches ETH pattern (interior argmin) — opposite of HYPE/DOGE
        # which needed heavy market blend (0.80/0.60) because their raw
        # models were overconfident; BNB's raw model is well-calibrated.
        self.assertIn("BNB", MARKET_BLEND_W_BY_ASSET)
        self.assertEqual(MARKET_BLEND_W_BY_ASSET["BNB"], 0.20)

    def test_nbbo_fallback_gates_bnb_entry(self):
        from bot.constants import NBBO_FALLBACK_GATES, BNB_MIN_ENTRY_PRICE
        # Analog default mirror of ETH. Post-T4 follow-up to refine from
        # actual NBBO-fallback observations once they accumulate.
        self.assertIn("BNB", NBBO_FALLBACK_GATES)
        gate = NBBO_FALLBACK_GATES["BNB"]
        self.assertEqual(gate, (90, 99, 300.0))
        # Consistency: NBBO floor matches non-NBBO MIN_ENTRY_PRICE.
        self.assertEqual(gate[0], BNB_MIN_ENTRY_PRICE)


class TestBnbP24ScannerLiveRouting(unittest.TestCase):
    """Source-walk AST guards pinning the 3 new BNB elif clauses in the
    scanner: per-asset floor, 15M sizer cap, DC asset cap. Without these
    elif branches, BNB would fall back to globals (MIN_ENTRY_PRICE=75,
    no asset cap) — defeating the per-asset T4 contract."""

    def setUp(self):
        import bot.scanner
        import inspect
        self.source = inspect.getsource(bot.scanner)

    def test_scanner_imports_bnb_floor_and_risk_constants(self):
        # Top-of-file import block must include both new constants so
        # the elif branches below resolve.
        self.assertIn("BNB_MIN_ENTRY_PRICE", self.source,
            "BNB_MIN_ENTRY_PRICE not imported into bot/scanner/__init__.py")
        self.assertIn("BNB_MAX_RISK_PER_TRADE", self.source,
            "BNB_MAX_RISK_PER_TRADE not imported into bot/scanner/__init__.py")

    def test_scanner_per_asset_floor_elif_for_bnb(self):
        # Anchor on the DOGE branch in the per-asset floor elif chain
        # (~line 2713 pre-P2.4) and verify BNB has a parallel branch.
        # Mirrors HYPE/DOGE T4 elif insertion shape.
        floor_idx = self.source.find('elif asset == "DOGE":\n                        _asset_floor = DOGE_MIN_ENTRY_PRICE')
        self.assertGreater(floor_idx, 0,
            "DOGE per-asset floor elif anchor not found — file shape changed")
        # BNB elif must appear within 200 chars after DOGE branch
        bnb_window = self.source[floor_idx:floor_idx + 400]
        self.assertIn('elif asset == "BNB":', bnb_window,
            "Scanner per-asset floor elif missing BNB branch — "
            "BNB would fall back to global MIN_ENTRY_PRICE=75 (defeats P2.4 floor=90)")
        self.assertIn("BNB_MIN_ENTRY_PRICE", bnb_window)

    def test_scanner_15m_sizer_cap_elif_for_bnb(self):
        # Anchor on the DOGE branch in the 15M sizer cap elif chain
        # (~line 4889 pre-P2.4) and verify BNB parallel branch.
        sizer_anchor = self.source.find('elif asset == "DOGE" and _pt in (None, "15m"):\n                    _doge_max = int((_sizing_balance * DOGE_MAX_RISK_PER_TRADE)')
        self.assertGreater(sizer_anchor, 0,
            "DOGE 15M sizer cap elif anchor not found — file shape changed")
        # BNB elif must follow within 500 chars
        bnb_window = self.source[sizer_anchor:sizer_anchor + 800]
        self.assertIn('elif asset == "BNB" and _pt in (None, "15m"):', bnb_window,
            "Scanner 15M sizer cap elif missing BNB branch — "
            "BNB would size uncapped (defeats P2.4 MAX_RISK_PER_TRADE=0.10)")
        self.assertIn("BNB_MAX_RISK_PER_TRADE", bnb_window)

    def test_scanner_dc_asset_cap_elif_for_bnb(self):
        # Anchor on the DOGE branch in the DC asset cap elif chain
        # (~line 4316 pre-P2.4) and verify BNB parallel branch.
        dc_anchor = self.source.find('elif asset == "DOGE":\n                                    _dc_asset_max = int((_dc_balance * DOGE_MAX_RISK_PER_TRADE)')
        self.assertGreater(dc_anchor, 0,
            "DOGE DC asset cap elif anchor not found — file shape changed")
        bnb_window = self.source[dc_anchor:dc_anchor + 600]
        self.assertIn('elif asset == "BNB":', bnb_window,
            "Scanner DC asset cap elif missing BNB branch — "
            "DC strategy would bypass the per-asset cap for BNB")
        self.assertIn("BNB_MAX_RISK_PER_TRADE", bnb_window)


class TestBnbP24ExecutorLockstep(unittest.TestCase):
    """Source-walk AST guards pinning the 3 BNB executor mirror sites:
    escalation floor, maker floor, sub-floor-fill telemetry map.

    Discovered via R4 adv review: R1-R3 swept only scanner sites; the
    executor mirror was missed entirely. Without these 3 mirrors, BNB
    maker/escalation orders at 76-89c would BYPASS the BNB_MIN_ENTRY_PRICE=90
    contract (falling through to the 15M default 75c floor) — i.e., live
    trading bug.

    These tests close the lockstep contract documented in bot/constants.py
    banner (executor mirrors at ~:2097 + ~:3197 + ~:4638)."""

    def setUp(self):
        import bot.executor
        import inspect
        self.source = inspect.getsource(bot.executor)

    def test_executor_imports_bnb_min_entry_price(self):
        self.assertIn("BNB_MIN_ENTRY_PRICE", self.source,
            "BNB_MIN_ENTRY_PRICE not imported into bot/executor.py")

    def test_executor_escalation_floor_elif_for_bnb(self):
        anchor = self.source.find('elif _esc_asset == "DOGE":\n            _esc_floor = DOGE_MIN_ENTRY_PRICE')
        self.assertGreater(anchor, 0,
            "DOGE escalation-floor elif anchor not found — file shape changed")
        window = self.source[anchor:anchor + 300]
        self.assertIn('elif _esc_asset == "BNB":', window,
            "Executor escalation floor missing BNB branch — "
            "BNB escalation would fall through to global MIN_ENTRY_PRICE=75 (defeats P2.4 floor=90)")
        self.assertIn("BNB_MIN_ENTRY_PRICE", window)

    def test_executor_maker_floor_elif_for_bnb(self):
        anchor = self.source.find('elif _asset == "DOGE":\n                _floor = DOGE_MIN_ENTRY_PRICE')
        self.assertGreater(anchor, 0,
            "DOGE maker-floor elif anchor not found — file shape changed")
        window = self.source[anchor:anchor + 300]
        self.assertIn('elif _asset == "BNB":', window,
            "Executor maker floor missing BNB branch — "
            "BNB maker orders could be placed at 76-89c (defeats P2.4 floor=90)")
        self.assertIn("BNB_MIN_ENTRY_PRICE", window)

    def test_executor_sub_floor_fill_telemetry_includes_bnb(self):
        anchor = self.source.find('_ASSET_FLOOR_MAP = {')
        self.assertGreater(anchor, 0,
            "_ASSET_FLOOR_MAP anchor not found in bot/executor.py")
        window = self.source[anchor:anchor + 400]
        self.assertIn('"BNB"', window,
            "Executor sub-floor-fill telemetry map missing BNB — "
            "BNB sub-90c fills would NOT trigger SUB_FLOOR_FILL Telegram alert (operator loses defense-in-depth visibility on the very class of BNB fill the P2.4 contract is designed to prevent)")
        self.assertIn("BNB_MIN_ENTRY_PRICE", window)


if __name__ == "__main__":
    unittest.main()
