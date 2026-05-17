"""T1.5 regression tests for BNB external-feed verify+add.

T1 (commit fbfc25d, 2026-05-17) activated BNB shadow observation. T1.5
extends the external feeds (Binance/Kraken/Bybit spot + OKX perp +
CoinGlass + Coinbase + OKX backfill) so the cal_mlp T3 training set
has feature parity with BTC/ETH/SOL/XRP/HYPE/DOGE.

Per-asset verification matrix (public REST + listing-page search):
  | Asset | Binance.com | Kraken (wsname) | Bybit spot | OKX perp        | Coinbase   | CoinGlass | Deribit perp |
  |-------|-------------|-----------------|------------|-----------------|------------|-----------|--------------|
  | BNB   | bnbusdt     | BNB/USD         | BNBUSDT    | BNB-USDT-SWAP   | BNB-USD    | BNB       | (gap)        |

Verification 2026-05-17:
- Binance.com BNBUSDT: bot's BINANCE_FEED_ENABLED=0 gates US-VPS at module
  level; the pair IS listed on Binance.com (BNB is Binance's native token).
  Both Mac + VPS return HTTP 451 "Service unavailable from a restricted
  location" — geo-block, not delisting. Filed separate ticket 86b9zn45p
  for the EU-proxy spike. Per the existing BTC/ETH/SOL/XRP/DOGE pattern,
  the "binance" key is INCLUDED in CROSS_EXCHANGE_SYMBOLS["BNB"] as
  REGISTRY metadata; the runtime kill is BINANCE_FEED_ENABLED at module
  level (mirrors DOGE which is also on Binance.com but disabled in prod).
- Kraken BNB/USD: api.kraken.com/0/public/AssetPairs?pair=BNBUSD returns
  status="online", base="BNB", wsname="BNB/USD".
- Bybit BNBUSDT spot: Mac probes return CloudFront 403 (consumer-ISP
  artifact, same class as the doge-hype Phase 2 replay backfill Mac
  blocker). VPS journalctl 2026-05-17 21:27:48 shows "Bybit feed
  connected" — Bybit data plane (stream.bybit.com) reaches the
  DigitalOcean NYC3 VPS fine. BNBUSDT is a well-established spot pair
  on Bybit (one of the highest-volume USDT pairs).
- OKX BNB-USDT-SWAP: api.okx.com/api/v5/public/instruments?instType=SWAP
  returns state="live", listTime=1671778428049 (Dec 2022).
- Coinbase BNB-USD: api.exchange.coinbase.com/products/BNB-USD returns
  status="online", trading_disabled=False. Already in COINBASE_PRODUCTS
  (T1 ship).
- CoinGlass BNB: open-api-v3.coinglass.com uses the same 3-letter symbol
  as the asset name — verified pattern from BTC/ETH/SOL/XRP/DOGE/HYPE
  precedent.
- Deribit BNB perp: api.deribit.com/api/v2/public/get_instruments?
  currency=BNB&kind=future returns result=[] (empty). BNB stays ABSENT
  from DERIBIT_FUNDING_INSTRUMENTS — documented gap, mirrors HYPE.

T1 ship: agent_docs/bnb-t1-plan-may17.md (commit fbfc25d, PR #78)
T1.5 ClickUp: 86b9zmj15
Binance proxy spike: 86b9zn45p
"""
from __future__ import annotations

import unittest


# ─── CROSS_EXCHANGE_SYMBOLS contents (verified-clean shape) ─────────────────

class TestCrossExchangeSymbolsContents(unittest.TestCase):
    def setUp(self):
        from bot.constants import CROSS_EXCHANGE_SYMBOLS
        self.symbols = CROSS_EXCHANGE_SYMBOLS

    def test_existing_assets_unchanged(self):
        """Regression: existing assets must retain their entries."""
        for asset, expected in [
            ("BTC", {"binance": "btcusdt", "kraken": "BTC/USD", "bybit": "BTCUSDT"}),
            ("ETH", {"binance": "ethusdt", "kraken": "ETH/USD", "bybit": "ETHUSDT"}),
            ("SOL", {"binance": "solusdt", "kraken": "SOL/USD", "bybit": "SOLUSDT"}),
            ("XRP", {"binance": "xrpusdt", "kraken": "XRP/USD", "bybit": "XRPUSDT"}),
            ("DOGE", {"binance": "dogeusdt", "kraken": "XDG/USD", "bybit": "DOGEUSDT"}),
        ]:
            self.assertIn(asset, self.symbols)
            self.assertEqual(self.symbols[asset], expected)

    def test_hype_documented_gap_preserved(self):
        """HYPE retains the "binance" key absence (Binance.US only — NOT on
        Binance.com). T1.5 BNB extension must not regress this."""
        self.assertIn("HYPE", self.symbols)
        entry = self.symbols["HYPE"]
        self.assertNotIn("binance", entry)
        self.assertEqual(entry.get("kraken"), "HYPE/USD")
        self.assertEqual(entry.get("bybit"), "HYPEUSDT")

    def test_bnb_present_on_all_3_exchanges(self):
        """BNB is Binance's native token — listed on Binance.com itself.
        The US-VPS geo-block is at INFRASTRUCTURE level (HTTP 451 from
        api.binance.com + stream.binance.com) and is gated by the
        BINANCE_FEED_ENABLED=0 runtime kill, NOT by per-asset key
        absence. Mirrors DOGE precedent (DOGE also on Binance.com but
        disabled at module level in prod).

        Key absence is reserved for "exchange does not list this pair"
        gaps (HYPE on Binance.com). BNB-on-Binance.com is a "we can't
        reach it from US IPs" gap — separate concern, ticket 86b9zn45p
        (EU-proxy spike).
        """
        self.assertIn("BNB", self.symbols)
        entry = self.symbols["BNB"]
        self.assertEqual(
            entry.get("binance"), "bnbusdt",
            "BNB IS listed on Binance.com (it's their native token). "
            "Geo-block is gated at module level via BINANCE_FEED_ENABLED. "
            "See ticket 86b9zn45p for the EU-proxy spike.",
        )
        self.assertEqual(
            entry.get("kraken"), "BNB/USD",
            "Kraken BNB/USD verified online via "
            "https://api.kraken.com/0/public/AssetPairs?pair=BNBUSD "
            "(status=online, base=BNB, wsname=BNB/USD).",
        )
        self.assertEqual(
            entry.get("bybit"), "BNBUSDT",
            "Bybit BNBUSDT spot — well-established USDT-quoted pair. "
            "Mac CloudFront 403 is consumer-ISP artifact; VPS journalctl "
            "confirms 'Bybit feed connected' from prod.",
        )

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
        for asset in ("BTC", "ETH", "SOL", "XRP", "DOGE", "HYPE"):
            self.assertEqual(self.symbols.get(asset), asset)

    def test_bnb_present(self):
        self.assertEqual(self.symbols.get("BNB"), "BNB")


# ─── OKX poller FUNDING_SYMBOLS / OI_SYMBOLS ────────────────────────────────

class TestOkxPollerSymbols(unittest.TestCase):
    def setUp(self):
        import scripts.backfill.external_market_poller as poller
        self.funding = poller.FUNDING_SYMBOLS
        self.oi = poller.OI_SYMBOLS

    def test_funding_symbols_includes_existing_6(self):
        for sym in (
            "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",
            "XRP-USDT-SWAP", "DOGE-USDT-SWAP", "HYPE-USDT-SWAP",
        ):
            self.assertIn(sym, self.funding)

    def test_funding_symbols_includes_bnb(self):
        self.assertIn(
            "BNB-USDT-SWAP", self.funding,
            "OKX BNB-USDT-SWAP verified live via "
            "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
            "&instId=BNB-USDT-SWAP (state=live, listTime=Dec 2022).",
        )

    def test_oi_symbols_includes_existing_6(self):
        for sym in (
            "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",
            "XRP-USDT-SWAP", "DOGE-USDT-SWAP", "HYPE-USDT-SWAP",
        ):
            self.assertIn(sym, self.oi)

    def test_oi_symbols_includes_bnb(self):
        self.assertIn("BNB-USDT-SWAP", self.oi)

    def test_funding_and_oi_symbols_in_sync(self):
        """OKX exposes both /funding-rate and /open-interest for the same
        perp instId. The two lists should track each other — divergence
        signals an asymmetric activation."""
        self.assertEqual(
            set(self.funding), set(self.oi),
            "FUNDING_SYMBOLS and OI_SYMBOLS must contain the same "
            "instIds — they both target the same OKX perp universe.",
        )


# ─── shadow_coverage_backfill instrument dicts ──────────────────────────────
# Mirrors HYPE/DOGE T1.5 R1-MAJOR-1: the backfill harness has separate
# hardcoded instrument dicts. Without extending these, BNB shadow rows
# will have NULL okx_funding_rate_at_decision for the entire ~3-4wk T3
# accumulation window, biasing the eventual training set.

class TestBackfillOkxFundingInstruments(unittest.TestCase):
    def setUp(self):
        import scripts.backfill.shadow_coverage_backfill as bf
        self.instruments = bf.OKX_FUNDING_INSTRUMENTS

    def test_existing_6_unchanged(self):
        for asset, expected in [
            ("BTC", "BTC-USDT-SWAP"),
            ("ETH", "ETH-USDT-SWAP"),
            ("SOL", "SOL-USDT-SWAP"),
            ("XRP", "XRP-USDT-SWAP"),
            ("DOGE", "DOGE-USDT-SWAP"),
            ("HYPE", "HYPE-USDT-SWAP"),
        ]:
            self.assertEqual(self.instruments.get(asset), expected)

    def test_bnb_present(self):
        """OKX BNB-USDT-SWAP verified live via
        /api/v5/public/instruments?instType=SWAP."""
        self.assertEqual(self.instruments.get("BNB"), "BNB-USDT-SWAP")

    def test_backfill_okx_dict_matches_live_poller(self):
        """The backfill and the live poller use the SAME OKX universe."""
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
    """T1 (commit fbfc25d) already added BNB to scripts/backfill/
    shadow_coverage_backfill.COINBASE_PRODUCTS. This test pins the
    presence + lock-step parity with bot.constants.COINBASE_PRODUCTS."""

    def setUp(self):
        import scripts.backfill.shadow_coverage_backfill as bf
        self.products = bf.COINBASE_PRODUCTS

    def test_existing_6_unchanged(self):
        for asset, expected in [
            ("BTC", "BTC-USD"),
            ("ETH", "ETH-USD"),
            ("SOL", "SOL-USD"),
            ("XRP", "XRP-USD"),
            ("DOGE", "DOGE-USD"),
            ("HYPE", "HYPE-USD"),
        ]:
            self.assertEqual(self.products.get(asset), expected)

    def test_bnb_present(self):
        self.assertEqual(self.products.get("BNB"), "BNB-USD")

    def test_backfill_matches_bot_constants(self):
        """The backfill mirror must stay in sync with bot/constants.py's
        COINBASE_PRODUCTS — divergence biases the historical backfill."""
        import bot.constants
        self.assertEqual(self.products, bot.constants.COINBASE_PRODUCTS)


class TestBackfillDeribitFundingInstruments(unittest.TestCase):
    def setUp(self):
        import scripts.backfill.shadow_coverage_backfill as bf
        self.instruments = bf.DERIBIT_FUNDING_INSTRUMENTS

    def test_existing_5_unchanged(self):
        for asset, expected in [
            ("BTC", "BTC-PERPETUAL"),
            ("ETH", "ETH-PERPETUAL"),
            ("SOL", "SOL_USDC-PERPETUAL"),
            ("XRP", "XRP_USDC-PERPETUAL"),
            ("DOGE", "DOGE_USDC-PERPETUAL"),
        ]:
            self.assertEqual(self.instruments.get(asset), expected)

    def test_hype_absent_documented_gap_preserved(self):
        """HYPE precedent: Deribit does not list a HYPE perpetual.
        Regression check."""
        self.assertNotIn("HYPE", self.instruments)

    def test_bnb_absent_documented_gap(self):
        """Deribit does NOT list a BNB perpetual. Verified via
        /api/v2/public/get_instruments?currency=BNB&kind=future returning
        result=[] (empty array). Per the verify-first contract, BNB stays
        ABSENT from DERIBIT_FUNDING_INSTRUMENTS — documented gap, not
        silent NULL. Mirrors HYPE precedent.

        Consumer code at scripts/backfill/shadow_coverage_backfill.py:1134
        uses DERIBIT_FUNDING_INSTRUMENTS.get(asset) — safe under
        key-absence semantics."""
        self.assertNotIn(
            "BNB", self.instruments,
            "Deribit does not list a BNB perpetual — verified via "
            "/api/v2/public/get_instruments?currency=BNB&kind=future "
            "returning empty result. BNB stays absent.",
        )


# ─── DVOL stays BTC/ETH only (negative regression) ──────────────────────────

class TestDvolStaysBtcEthOnly(unittest.TestCase):
    """Deribit DVOL is a BTC/ETH-only index. T1.5 BNB extension must not
    regress this (BNB has no DVOL listing on Deribit)."""

    def test_deribit_dvol_currencies_stays_btc_eth(self):
        from bot.constants import DERIBIT_DVOL_CURRENCIES
        self.assertEqual(set(DERIBIT_DVOL_CURRENCIES.keys()), {"BTC", "ETH"})

    def test_poller_dvol_symbols_stays_btc_eth(self):
        import scripts.backfill.external_market_poller as poller
        self.assertEqual(set(poller.DVOL_SYMBOLS), {"btcdvol_usdc", "ethdvol_usdc"})


# ─── CrossExchangeFeed per-exchange optionality (refactor regression) ───────

class TestCrossExchangeFeedHandlesBnb(unittest.TestCase):
    """Sanity check: the CrossExchangeFeed init shouldn't break when BNB
    is added with all 3 exchanges (mirrors BTC/ETH/SOL/XRP/DOGE pattern).
    Also confirms the per-exchange optionality from HYPE T1.5 still holds."""

    def _make_feed(self):
        from bot.feeds.cross_exchange import CrossExchangeFeed
        class _StubCoinbase:
            def get_price(self, _asset): return None
            def get_all_prices(self): return {}
        return CrossExchangeFeed(_StubCoinbase())

    def test_init_does_not_raise_for_bnb(self):
        feed = self._make_feed()
        self.assertIsNotNone(feed)

    def test_binance_map_includes_bnb(self):
        feed = self._make_feed()
        self.assertIn("BNB", feed._binance_map.values())

    def test_kraken_map_includes_bnb(self):
        feed = self._make_feed()
        self.assertIn("BNB", feed._kraken_map.values())
        self.assertEqual(feed._kraken_map.get("BNB/USD"), "BNB")

    def test_bybit_map_includes_bnb(self):
        feed = self._make_feed()
        self.assertIn("BNB", feed._bybit_map.values())


# ─── Atomic activation invariant (T1.5 extension) ───────────────────────────

class TestAtomicActivationT15(unittest.TestCase):
    """T1's atomic-activation invariant: if X in ASSETS → safety gates
    wired. T1.5 extends with: if X in ASSETS → external feeds registered
    for the verified exchanges. Partial reverts that leave ASSETS extended
    but external feeds stale will cause cal_mlp T3 training to see NULL
    feature columns for BNB rows while BTC/ETH/SOL/XRP/HYPE/DOGE have them."""

    def test_bnb_in_assets_implies_external_feed_registration(self):
        from bot.config import ASSETS
        if "BNB" not in ASSETS:
            self.skipTest("BNB not in ASSETS yet")
        from bot.constants import CROSS_EXCHANGE_SYMBOLS, COINGLASS_SYMBOLS
        import scripts.backfill.external_market_poller as poller
        import scripts.backfill.shadow_coverage_backfill as backfill
        self.assertIn(
            "BNB", CROSS_EXCHANGE_SYMBOLS,
            "BNB active in ASSETS but missing from CROSS_EXCHANGE_SYMBOLS. "
            "T3 (cal_mlp training) will see NULL cross-exchange features.",
        )
        self.assertIn("BNB", COINGLASS_SYMBOLS)
        self.assertIn("BNB-USDT-SWAP", poller.FUNDING_SYMBOLS)
        self.assertIn("BNB-USDT-SWAP", poller.OI_SYMBOLS)
        # Backfill mirrors
        self.assertIn("BNB", backfill.OKX_FUNDING_INSTRUMENTS)
        self.assertIn("BNB", backfill.COINBASE_PRODUCTS)
        # Deribit BNB perp: documented gap (mirrors HYPE).
        self.assertNotIn(
            "BNB", backfill.DERIBIT_FUNDING_INSTRUMENTS,
            "Deribit does not list a BNB perpetual — verified via "
            "/api/v2/public/get_instruments?currency=BNB&kind=future "
            "returning empty. BNB stays absent.",
        )


if __name__ == "__main__":
    unittest.main()
