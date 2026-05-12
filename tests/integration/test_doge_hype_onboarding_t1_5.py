"""T1.5 regression tests for HYPE + DOGE external-feed verify+add.

T1 (commit 5dca85a) activated HYPE + DOGE shadow observation. T1.5 extends
the external feeds (Binance/Kraken/Bybit spot + OKX perp + CoinGlass) so
the cal_mlp training set has feature parity with BTC/ETH/SOL/XRP.

Per-asset verification matrix (public REST + listing-page search):
  | Asset | Binance.com | Kraken (wsname) | Bybit spot | OKX perp        | CoinGlass |
  |-------|-------------|-----------------|------------|-----------------|-----------|
  | DOGE  | dogeusdt    | XDG/USD         | DOGEUSDT   | DOGE-USDT-SWAP  | DOGE      |
  | HYPE  | (gap)       | HYPE/USD        | HYPEUSDT   | HYPE-USDT-SWAP  | HYPE      |

HYPE is NOT on Binance.com (only on Binance.US). Per the spike contract
"if T1.5 verification reveals HYPE is missing from an exchange, that
asset/exchange entry stays absent — documented gap, not silent NULL", the
HYPE entry in CROSS_EXCHANGE_SYMBOLS omits the "binance" key. This
required a CrossExchangeFeed refactor: per-exchange optionality via
`v.get("binance")` filters at __init__-time map builds and at WS-subscribe
list builds.

DOGE's Kraken symbol is XDG/USD (Kraken's internal ticker is XDG), NOT
DOGE/USD. Direct REST verification.

Spike: agent_docs/asset-onboarding-doge-hype-spike.md
T1 plan: agent_docs/doge-hype-t1-plan-may10.md
T1.5 ClickUp: 86b9vre9p
"""
from __future__ import annotations

import unittest

import pytest


# ─── CROSS_EXCHANGE_SYMBOLS contents (verified-clean shape) ─────────────────

class TestCrossExchangeSymbolsContents(unittest.TestCase):
    def setUp(self):
        from bot.constants import CROSS_EXCHANGE_SYMBOLS
        self.symbols = CROSS_EXCHANGE_SYMBOLS

    def test_btc_eth_sol_xrp_unchanged_all_3_exchanges(self):
        """Regression: existing assets must retain all 3 exchange entries."""
        for asset, expected in [
            ("BTC", {"binance": "btcusdt", "kraken": "BTC/USD", "bybit": "BTCUSDT"}),
            ("ETH", {"binance": "ethusdt", "kraken": "ETH/USD", "bybit": "ETHUSDT"}),
            ("SOL", {"binance": "solusdt", "kraken": "SOL/USD", "bybit": "SOLUSDT"}),
            ("XRP", {"binance": "xrpusdt", "kraken": "XRP/USD", "bybit": "XRPUSDT"}),
        ]:
            self.assertIn(asset, self.symbols)
            self.assertEqual(self.symbols[asset], expected)

    def test_doge_present_on_all_3_exchanges(self):
        self.assertIn("DOGE", self.symbols)
        entry = self.symbols["DOGE"]
        self.assertEqual(entry.get("binance"), "dogeusdt")
        self.assertEqual(
            entry.get("kraken"), "XDG/USD",
            "Kraken's internal ticker for Doge is XDG, wsname XDG/USD. "
            "DOGE/USD is NOT a valid Kraken pair — verified via "
            "https://api.kraken.com/0/public/AssetPairs?pair=XDGUSD",
        )
        self.assertEqual(entry.get("bybit"), "DOGEUSDT")

    def test_hype_present_on_kraken_and_bybit_only(self):
        """HYPE has documented gap on Binance.com — only Binance.US lists it.

        Per spike contract: 'entry stays absent — documented gap, not silent
        NULL'. The "binance" key MUST be absent from the HYPE entry; an
        empty string or None would still get registered into the reverse
        map at CrossExchangeFeed init and trigger a WS subscribe-reject.
        """
        self.assertIn("HYPE", self.symbols)
        entry = self.symbols["HYPE"]
        self.assertNotIn(
            "binance", entry,
            "HYPE is NOT on Binance.com (only Binance.US). The 'binance' "
            "key MUST be ABSENT from the HYPE entry — not None, not empty "
            "string. CrossExchangeFeed uses v.get('binance') with skip-if-"
            "falsy semantics so this resolves cleanly.",
        )
        self.assertEqual(entry.get("kraken"), "HYPE/USD")
        self.assertEqual(entry.get("bybit"), "HYPEUSDT")

    def test_no_silent_none_or_empty_string_entries(self):
        """Per spike: 'documented gap, not silent NULL'. None/empty values
        in the inner dict are forbidden — they'd register a dead WS
        subscription. Use key-absence to signal a gap."""
        for asset, entry in self.symbols.items():
            for exchange, symbol in entry.items():
                self.assertIsInstance(
                    symbol, str,
                    f"{asset}.{exchange} value must be a non-empty string, "
                    f"got {symbol!r}. Use key-absence to signal a gap.",
                )
                self.assertNotEqual(
                    symbol, "",
                    f"{asset}.{exchange} is empty string. Omit the key "
                    "entirely to signal a documented gap.",
                )


# ─── COINGLASS_SYMBOLS contents ─────────────────────────────────────────────

class TestCoinGlassSymbolsContents(unittest.TestCase):
    def setUp(self):
        from bot.constants import COINGLASS_SYMBOLS
        self.symbols = COINGLASS_SYMBOLS

    def test_existing_assets_unchanged(self):
        for asset in ("BTC", "ETH", "SOL", "XRP"):
            self.assertEqual(self.symbols.get(asset), asset)

    def test_doge_present(self):
        self.assertEqual(self.symbols.get("DOGE"), "DOGE")

    def test_hype_present(self):
        self.assertEqual(self.symbols.get("HYPE"), "HYPE")


# ─── OKX poller FUNDING_SYMBOLS / OI_SYMBOLS ────────────────────────────────

class TestOkxPollerSymbols(unittest.TestCase):
    def setUp(self):
        import scripts.backfill.external_market_poller as poller
        self.funding = poller.FUNDING_SYMBOLS
        self.oi = poller.OI_SYMBOLS

    def test_funding_symbols_includes_existing_4(self):
        for sym in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP"):
            self.assertIn(sym, self.funding)

    def test_funding_symbols_includes_doge_hype(self):
        self.assertIn(
            "DOGE-USDT-SWAP", self.funding,
            "OKX DOGE-USDT-SWAP verified live via "
            "https://www.okx.com/api/v5/public/instruments?instType=SWAP",
        )
        self.assertIn(
            "HYPE-USDT-SWAP", self.funding,
            "OKX HYPE-USDT-SWAP verified live via the same endpoint",
        )

    def test_oi_symbols_includes_existing_4(self):
        for sym in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP"):
            self.assertIn(sym, self.oi)

    def test_oi_symbols_includes_doge_hype(self):
        self.assertIn("DOGE-USDT-SWAP", self.oi)
        self.assertIn("HYPE-USDT-SWAP", self.oi)

    def test_funding_and_oi_symbols_in_sync(self):
        """OKX exposes both /funding-rate and /open-interest for the same
        perp instId. The two lists should track each other — divergence
        signals an asymmetric activation."""
        self.assertEqual(
            set(self.funding), set(self.oi),
            "FUNDING_SYMBOLS and OI_SYMBOLS must contain the same "
            "instIds — they both target the same OKX perp universe.",
        )


# ─── shadow_coverage_backfill OKX/Deribit instrument dicts ──────────────────
# R1-MAJOR-1: the live external_market_poller writes to a SEPARATE
# external_market_data table; evaluated_opportunities.okx_funding_rate_at_decision
# is populated by the backfill harness, which has its own hardcoded
# instrument dicts. Without extending these, HYPE/DOGE shadow rows will
# keep okx_funding_rate_at_decision IS NULL for the entire ~3-4wk T3
# accumulation window, biasing the eventual cal_mlp training set.

class TestBackfillOkxFundingInstruments(unittest.TestCase):
    def setUp(self):
        import scripts.backfill.shadow_coverage_backfill as bf
        self.instruments = bf.OKX_FUNDING_INSTRUMENTS

    def test_existing_4_unchanged(self):
        self.assertEqual(self.instruments.get("BTC"), "BTC-USDT-SWAP")
        self.assertEqual(self.instruments.get("ETH"), "ETH-USDT-SWAP")
        self.assertEqual(self.instruments.get("SOL"), "SOL-USDT-SWAP")
        self.assertEqual(self.instruments.get("XRP"), "XRP-USDT-SWAP")

    def test_doge_present(self):
        """OKX DOGE-USDT-SWAP verified live via /funding-rate-history endpoint."""
        self.assertEqual(self.instruments.get("DOGE"), "DOGE-USDT-SWAP")

    def test_hype_present(self):
        """OKX HYPE-USDT-SWAP verified live via /funding-rate-history endpoint."""
        self.assertEqual(self.instruments.get("HYPE"), "HYPE-USDT-SWAP")

    def test_backfill_okx_dict_matches_live_poller(self):
        """The backfill and the live poller use the SAME OKX universe —
        instId divergence would mean either (a) the live poller emits
        signals for rows the backfill can't repopulate, or (b) the
        backfill fills rows the live poller doesn't track."""
        import scripts.backfill.external_market_poller as poller
        backfill_instids = set(self.instruments.values())
        live_instids = set(poller.FUNDING_SYMBOLS)
        self.assertEqual(
            backfill_instids, live_instids,
            f"Backfill OKX_FUNDING_INSTRUMENTS instId set "
            f"({backfill_instids}) must equal live poller FUNDING_SYMBOLS "
            f"({live_instids}) — divergence biases T3 training data.",
        )


class TestBackfillCoinbaseProducts(unittest.TestCase):
    """R2 minor-1 (same class as R1-MAJOR-1): Phase G-2 path-metrics
    backfill has its own hardcoded 4-asset COINBASE_PRODUCTS dict at
    scripts/backfill/shadow_coverage_backfill.py:376-380. When run with HYPE/DOGE
    rows present in the source DB, the backfill silently writes NULL path
    metrics for HYPE/DOGE rows + advances the checkpoint. Live capture is
    the primary writer so impact is reduced — but the symmetric defensive
    fix is to extend this dict atomically with T1.5."""

    def setUp(self):
        import scripts.backfill.shadow_coverage_backfill as bf
        self.products = bf.COINBASE_PRODUCTS

    def test_existing_4_unchanged(self):
        self.assertEqual(self.products.get("BTC"), "BTC-USD")
        self.assertEqual(self.products.get("ETH"), "ETH-USD")
        self.assertEqual(self.products.get("SOL"), "SOL-USD")
        self.assertEqual(self.products.get("XRP"), "XRP-USD")

    def test_doge_present(self):
        self.assertEqual(self.products.get("DOGE"), "DOGE-USD")

    def test_hype_present(self):
        self.assertEqual(self.products.get("HYPE"), "HYPE-USD")

    def test_backfill_matches_bot_constants(self):
        """The backfill mirror must stay in sync with bot/constants.py's
        COINBASE_PRODUCTS — divergence biases the historical backfill."""
        import bot.constants
        self.assertEqual(self.products, bot.constants.COINBASE_PRODUCTS)


class TestBackfillDeribitFundingInstruments(unittest.TestCase):
    def setUp(self):
        import scripts.backfill.shadow_coverage_backfill as bf
        self.instruments = bf.DERIBIT_FUNDING_INSTRUMENTS

    def test_existing_4_unchanged(self):
        self.assertEqual(self.instruments.get("BTC"), "BTC-PERPETUAL")
        self.assertEqual(self.instruments.get("ETH"), "ETH-PERPETUAL")
        self.assertEqual(self.instruments.get("SOL"), "SOL_USDC-PERPETUAL")
        self.assertEqual(self.instruments.get("XRP"), "XRP_USDC-PERPETUAL")

    def test_doge_present(self):
        """Deribit DOGE_USDC-PERPETUAL verified live via
        /public/get_funding_rate_history. Matches the SOL/XRP USDC-PERPETUAL
        pattern for non-major coins."""
        self.assertEqual(self.instruments.get("DOGE"), "DOGE_USDC-PERPETUAL")

    def test_hype_absent_documented_gap(self):
        """Deribit does NOT list a HYPE perpetual (verified via
        /public/get_instruments?currency=HYPE&kind=future returning empty).
        Per the verify-first contract, HYPE stays absent from this dict —
        documented gap, not silent NULL.

        Consumer code at scripts/backfill/shadow_coverage_backfill.py:1096 uses
        DERIBIT_FUNDING_INSTRUMENTS.get(asset) and at :1182 iterates the
        dict — both safe under key-absence semantics."""
        self.assertNotIn("HYPE", self.instruments)


# ─── DVOL stays BTC/ETH only (negative regression) ──────────────────────────

class TestDvolStaysBtcEthOnly(unittest.TestCase):
    """Deribit DVOL is a BTC/ETH-only index. The spike + plan doc
    explicitly skip DERIBIT_DVOL_CURRENCIES and the poller's DVOL_SYMBOLS
    from T1.5. Lock this with a negative regression."""

    def test_deribit_dvol_currencies_stays_btc_eth(self):
        from bot.constants import DERIBIT_DVOL_CURRENCIES
        self.assertEqual(set(DERIBIT_DVOL_CURRENCIES.keys()), {"BTC", "ETH"})

    def test_poller_dvol_symbols_stays_btc_eth(self):
        import scripts.backfill.external_market_poller as poller
        self.assertEqual(set(poller.DVOL_SYMBOLS), {"btcdvol_usdc", "ethdvol_usdc"})


# ─── CrossExchangeFeed per-exchange optionality (refactor regression) ───────

class TestCrossExchangeFeedOptionalBinance(unittest.TestCase):
    """The CROSS_EXCHANGE_SYMBOLS shape changed in T1.5 from
    'mandatory 3 exchanges per asset' to 'per-exchange optional via
    key-absence'. CrossExchangeFeed init and WS-subscribe builders had to
    refactor to skip-if-missing. Regression-lock the refactor against
    accidental re-introduction of direct key access (which would KeyError
    on HYPE)."""

    def _make_feed(self):
        from bot.feeds.cross_exchange import CrossExchangeFeed
        # Stub Coinbase — not exercised by init paths under test.
        class _StubCoinbase:
            def get_price(self, _asset): return None
            def get_all_prices(self): return {}
        return CrossExchangeFeed(_StubCoinbase())

    def test_init_does_not_raise_keyerror_for_hype(self):
        """If the consumer code reverts to `v["binance"]`, this would
        KeyError because HYPE has no "binance" key."""
        feed = self._make_feed()
        self.assertIsNotNone(feed)

    def test_binance_map_excludes_hype(self):
        """HYPE has no binance key → must not appear in binance reverse map."""
        feed = self._make_feed()
        self.assertNotIn("HYPE", feed._binance_map.values())

    def test_binance_map_includes_doge(self):
        """DOGE has a binance key → must appear in binance reverse map."""
        feed = self._make_feed()
        self.assertIn("DOGE", feed._binance_map.values())

    def test_kraken_map_includes_hype_and_doge(self):
        feed = self._make_feed()
        self.assertIn("HYPE", feed._kraken_map.values())
        self.assertIn("DOGE", feed._kraken_map.values())

    def test_kraken_map_uses_xdg_for_doge(self):
        """Kraken's internal ticker for Doge is XDG, NOT DOGE."""
        feed = self._make_feed()
        # Reverse map: wsname -> asset
        self.assertEqual(feed._kraken_map.get("XDG/USD"), "DOGE")
        self.assertNotIn("DOGE/USD", feed._kraken_map)

    def test_bybit_map_includes_hype_and_doge(self):
        feed = self._make_feed()
        self.assertIn("HYPE", feed._bybit_map.values())
        self.assertIn("DOGE", feed._bybit_map.values())


# ─── Source-walk regression: refactor pattern hold-out ──────────────────────

class TestCrossExchangeRefactorSourceWalk(unittest.TestCase):
    """Source-walk the consumer code to verify it uses skip-if-missing
    semantics for per-exchange keys. A future maintainer might accidentally
    revert to `v["binance"]` direct access — this test catches it.

    Pattern source-walked:
      - dict-comp filters: `if v.get("binance")` etc. (3 sites)
      - WS-subscribe-list comps: `if v.get("binance")` etc. (3 sites)
    """

    def test_cross_exchange_source_uses_get_for_optional_exchanges(self):
        """R2 minor-2 tightening: count >= 2 per exchange so a partial revert
        of one site (e.g. reverting the WS-subscribe builder while leaving
        the init-time map-builder intact) still trips the test.

        Per-exchange expected site count = 2:
          1. init-time reverse-map dict comprehension (lines 73-83)
          2. WS-subscribe builder (binance line 205-207, kraken line 254,
             bybit line 306)
        """
        import inspect
        import bot.feeds.cross_exchange as bfx
        source = inspect.getsource(bfx)
        for exchange in ("binance", "kraken", "bybit"):
            pattern = f'v.get("{exchange}")'
            count = source.count(pattern)
            self.assertGreaterEqual(
                count, 2,
                f'bot/feeds/cross_exchange.py must use v.get("{exchange}") '
                f'at >= 2 sites (init-time map-builder + WS-subscribe '
                f'builder). Found only {count}. Direct v["{exchange}"] '
                f'key access would KeyError on assets without that '
                f'exchange (e.g. HYPE has no "binance" key).',
            )


# ─── Atomic activation invariant (T1.5 extension) ───────────────────────────

class TestAtomicActivationT15(unittest.TestCase):
    """T1's atomic-activation invariant was: if X in ASSETS → safety gates
    wired. T1.5 extends with: if X in ASSETS → external feeds registered
    for the verified exchanges. Partial reverts that leave ASSETS extended
    but external feeds stale will cause cal_mlp T3 training to see NULL
    feature columns for HYPE/DOGE while BTC/ETH/SOL/XRP have them."""

    def test_hype_in_assets_implies_external_feed_registration(self):
        from bot.config import ASSETS
        if "HYPE" not in ASSETS:
            self.skipTest("HYPE not in ASSETS yet")
        from bot.constants import CROSS_EXCHANGE_SYMBOLS, COINGLASS_SYMBOLS
        import scripts.backfill.external_market_poller as poller
        import scripts.backfill.shadow_coverage_backfill as backfill
        self.assertIn(
            "HYPE", CROSS_EXCHANGE_SYMBOLS,
            "HYPE active in ASSETS but missing from CROSS_EXCHANGE_SYMBOLS. "
            "T3 (cal_mlp training) will see NULL cross-exchange features.",
        )
        self.assertIn("HYPE", COINGLASS_SYMBOLS)
        self.assertIn("HYPE-USDT-SWAP", poller.FUNDING_SYMBOLS)
        self.assertIn("HYPE-USDT-SWAP", poller.OI_SYMBOLS)
        # R1-MAJOR-1: OKX backfill must include HYPE. Deribit DOES NOT list
        # a HYPE perpetual — DERIBIT_FUNDING_INSTRUMENTS["HYPE"] stays
        # absent as a documented gap (not silent NULL).
        self.assertIn("HYPE", backfill.OKX_FUNDING_INSTRUMENTS)
        self.assertNotIn(
            "HYPE", backfill.DERIBIT_FUNDING_INSTRUMENTS,
            "Deribit does not list a HYPE perpetual — verified via "
            "/public/get_instruments?currency=HYPE&kind=future returning "
            "empty. HYPE stays absent from DERIBIT_FUNDING_INSTRUMENTS.",
        )
        # R2 minor-1: Phase G-2 path-metrics backfill mirror
        self.assertIn("HYPE", backfill.COINBASE_PRODUCTS)

    def test_doge_in_assets_implies_external_feed_registration(self):
        from bot.config import ASSETS
        if "DOGE" not in ASSETS:
            self.skipTest("DOGE not in ASSETS yet")
        from bot.constants import CROSS_EXCHANGE_SYMBOLS, COINGLASS_SYMBOLS
        import scripts.backfill.external_market_poller as poller
        import scripts.backfill.shadow_coverage_backfill as backfill
        self.assertIn("DOGE", CROSS_EXCHANGE_SYMBOLS)
        self.assertIn("DOGE", COINGLASS_SYMBOLS)
        self.assertIn("DOGE-USDT-SWAP", poller.FUNDING_SYMBOLS)
        self.assertIn("DOGE-USDT-SWAP", poller.OI_SYMBOLS)
        # R1-MAJOR-1: backfill dicts must also include DOGE — otherwise
        # evaluated_opportunities.okx_funding_rate_at_decision will be NULL
        # for DOGE rows even after the backfill runs.
        self.assertIn("DOGE", backfill.OKX_FUNDING_INSTRUMENTS)
        self.assertIn("DOGE", backfill.DERIBIT_FUNDING_INSTRUMENTS)
        # R2 minor-1: Phase G-2 path-metrics backfill mirror
        self.assertIn("DOGE", backfill.COINBASE_PRODUCTS)


if __name__ == "__main__":
    unittest.main()
