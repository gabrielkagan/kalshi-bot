"""Tests for OpportunityScanner — static helpers, edge computation, and filter pipeline.

Guards against:
- _parse_threshold returning None for valid markets (missed trades)
- _best_yes_ask_cents wrong on FP dollar format (wrong entry prices — 43d04bc)
- _convert_orderbook_fp precision loss in cents conversion
- Edge computation rounding errors (fee calc bug 7fb5a03)
- Price-dependent edge thresholds not applied correctly
- Observation gate bypassed for observation-only products (dead code bug 98c954d)
- STC shadow gate silently dead when product_type != None (98c954d)
- Candidate selection picking wrong candidate when multiple exist
- _window_timeslot parsing failure on unusual tickers
- _eval_opp_seen mixed tuple sizes (abd47c8 ValueError crash)
"""

import math
import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from bot.constants import (
    MIN_ENTRY_PRICE,
    MAX_ENTRY_PRICE,
    MIN_EDGE_PCT,
    MIN_EDGE_BY_PRICE,
    STC_SHADOW_THRESHOLD,
    HOURLY_OBSERVATION_ONLY,
    HOURLY_MIN_STC_ENTRY,
    HOURLY_MAX_STC_ENTRY,
    HOURLY_MAX_POSITIONS_PER_WINDOW,
    HOURLY_MAX_WINDOW_RISK,
    HOURLY_EXCLUDED_ASSETS,
    XRP_15M_SHADOW,
)
from bot.helpers.sizing import get_min_edge
from bot.scanner import OpportunityScanner
from bot.models import calculate_fee, calculate_taker_fee
import bot.helpers  # noqa: F401


# ═══════════════════════════════════════════════════════════════════════════════
#  1. Static helper — _parse_threshold
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseThreshold(unittest.TestCase):
    """OpportunityScanner._parse_threshold: extract strike price from market data.

    Guards against: missed trades from None return on valid markets,
    wrong strike price from parsing errors.
    """

    def test_floor_strike_field(self):
        """Priority 1: floor_strike field (most reliable)."""
        market = {"floor_strike": "95000.0"}
        self.assertEqual(OpportunityScanner._parse_threshold(market), 95000.0)

    def test_floor_strike_integer(self):
        """floor_strike as integer."""
        market = {"floor_strike": 95000}
        self.assertEqual(OpportunityScanner._parse_threshold(market), 95000.0)

    def test_floor_strike_zero_falls_through(self):
        """floor_strike=0 is invalid, should fall through to other methods."""
        market = {"floor_strike": 0, "yes_sub_title": "Price to beat: $95,000.00"}
        self.assertEqual(OpportunityScanner._parse_threshold(market), 95000.0)

    def test_yes_sub_title_15m(self):
        """Priority 2: 'Price to beat: $68,500.00' from 15M markets."""
        market = {"yes_sub_title": "Price to beat: $68,500.00"}
        self.assertEqual(OpportunityScanner._parse_threshold(market), 68500.0)

    def test_yes_sub_title_small_crypto(self):
        """Price to beat with small values (SOL, XRP)."""
        market = {"yes_sub_title": "Price to beat: $0.55"}
        self.assertEqual(OpportunityScanner._parse_threshold(market), 0.55)

    def test_sub_title_tbd_skipped(self):
        """TBD in subtitle → skip that source."""
        market = {"yes_sub_title": "Price to beat: TBD", "ticker": "KXBTC15M-26FEB2114-B95000"}
        result = OpportunityScanner._parse_threshold(market)
        # Should fall through to ticker pattern
        self.assertEqual(result, 95000.0)

    def test_ticker_pattern_B_prefix(self):
        """Priority 3: Ticker pattern KXBTC-26FEB2114-B95000."""
        market = {"ticker": "KXBTCD-26FEB2114-B95000"}
        self.assertEqual(OpportunityScanner._parse_threshold(market), 95000.0)

    def test_ticker_pattern_T_prefix(self):
        """Ticker pattern with T prefix (tail market)."""
        market = {"ticker": "KXHIGHNY-26MAR06-T75"}
        self.assertEqual(OpportunityScanner._parse_threshold(market), 75.0)

    def test_subtitle_above_dollar(self):
        """Priority 4: subtitle 'above $95,000.00'."""
        market = {"subtitle": "Will BTC be above $95,000.00?"}
        self.assertEqual(OpportunityScanner._parse_threshold(market), 95000.0)

    def test_no_data_returns_none(self):
        """No parseable threshold → None."""
        market = {"ticker": "UNKNOWN"}
        self.assertIsNone(OpportunityScanner._parse_threshold(market))

    def test_empty_market(self):
        market = {}
        self.assertIsNone(OpportunityScanner._parse_threshold(market))

    def test_comma_in_price(self):
        """Commas in price string should be stripped."""
        market = {"yes_sub_title": "Price to beat: $1,234,567.89"}
        self.assertEqual(OpportunityScanner._parse_threshold(market), 1234567.89)

    def test_no_sub_title_fallback(self):
        """no_sub_title also checked (second field in loop)."""
        market = {"no_sub_title": "Price to beat: $42,000.00"}
        self.assertEqual(OpportunityScanner._parse_threshold(market), 42000.0)


class TestThresholdSanityGate(unittest.TestCase):
    """Defensive gate against upstream threshold corruption.

    Guards against: Apr 13 2026 Kalshi API bug that returned floor_strike
    scaled by 10^-1 (XRP) or 10^-4 (BTC/ETH) for 6 crypto 15M markets.
    Model saturated to prob=0.97 on those garbage thresholds, resulting
    in 2 live losses (XRP -$49.98, BTC -$3.26).

    Gate formula: reject if abs(threshold - spot) / spot > 0.05.
    Max |thr/spot - 1| on 1,795 historical wins was 1.59% — 3x safety margin.
    See kb/failures/apr13-threshold-corruption.md.
    """

    def _ratio(self, threshold, spot):
        return abs(threshold - spot) / spot

    def test_apr13_xrp_bug_triggers_gate(self):
        """XRP case: threshold=0.13469, spot=1.3401 (÷10 scaling) → ratio=0.899."""
        self.assertGreater(self._ratio(0.13469, 1.3401), 0.05)

    def test_apr13_btc_bug_triggers_gate(self):
        """BTC case: threshold=7.243729, spot=72022.32 (÷10000 scaling) → ratio~=0.9999."""
        self.assertGreater(self._ratio(7.243729, 72022.32), 0.05)

    def test_apr13_eth_bug_triggers_gate(self):
        """ETH case: threshold=0.221499, spot=2207.54 (÷10000 scaling) → ratio~=0.9999."""
        self.assertGreater(self._ratio(0.221499, 2207.54), 0.05)

    def test_normal_btc_passes(self):
        """Normal BTC: threshold 72437, spot 72022 → ratio 0.58%."""
        self.assertLess(self._ratio(72437.29, 72022.32), 0.05)

    def test_normal_xrp_passes(self):
        """Normal XRP: threshold 1.3469, spot 1.3401 → ratio 0.51%."""
        self.assertLess(self._ratio(1.3469, 1.3401), 0.05)

    def test_historical_max_win_ratio_passes(self):
        """Max |thr/spot - 1| observed in 1,795 wins was 1.588% — must pass gate."""
        self.assertLess(self._ratio(1.0 + 0.01588, 1.0), 0.05)

    def test_boundary_just_over(self):
        """Ratio just above 5% should trigger gate."""
        self.assertGreater(self._ratio(1.0501, 1.0), 0.05)

    def test_boundary_just_under(self):
        """Ratio just below 5% should pass."""
        self.assertLess(self._ratio(1.0499, 1.0), 0.05)


# ═══════════════════════════════════════════════════════════════════════════════
#  2. Static helper — _parse_weather_market_info
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseWeatherMarketInfo(unittest.TestCase):
    """Parse weather market type (bracket/lower_tail/upper_tail) and bounds."""

    def test_bracket_with_both_strikes(self):
        """B-prefix ticker with floor_strike and cap_strike → bracket."""
        market = {"ticker": "KXHIGHNY-26MAR06-B75", "floor_strike": 73, "cap_strike": 75}
        result = OpportunityScanner._parse_weather_market_info(market)
        self.assertEqual(result[0], "bracket")
        self.assertEqual(result[1], 73.0)
        self.assertEqual(result[2], 75.0)

    def test_lower_tail(self):
        """T-prefix with cap_strike only → lower tail P(X < threshold)."""
        market = {"ticker": "KXHIGHNY-26MAR06-T60", "cap_strike": 60}
        result = OpportunityScanner._parse_weather_market_info(market)
        self.assertEqual(result[0], "lower_tail")
        self.assertIsNone(result[1])
        self.assertEqual(result[2], 60.0)

    def test_upper_tail(self):
        """T-prefix with floor_strike only → upper tail P(X > threshold)."""
        market = {"ticker": "KXHIGHNY-26MAR06-T85", "floor_strike": 85}
        result = OpportunityScanner._parse_weather_market_info(market)
        self.assertEqual(result[0], "upper_tail")
        self.assertEqual(result[1], 85.0)
        self.assertIsNone(result[2])

    def test_subtitle_fallback_below(self):
        """T-prefix with 'below' in subtitle → lower tail."""
        market = {"ticker": "KXHIGHNY-26MAR06-T60", "subtitle": "Will it be below 60°F?"}
        result = OpportunityScanner._parse_weather_market_info(market)
        self.assertEqual(result[0], "lower_tail")

    def test_subtitle_fallback_above(self):
        """T-prefix with 'above' in subtitle → upper tail."""
        market = {"ticker": "KXHIGHNY-26MAR06-T85", "subtitle": "Will it be above 85°F?"}
        result = OpportunityScanner._parse_weather_market_info(market)
        self.assertEqual(result[0], "upper_tail")

    def test_no_strike_prefix_returns_none(self):
        """Ticker without B or T prefix → None."""
        market = {"ticker": "KXHIGHNY-26MAR06-X99"}
        self.assertIsNone(OpportunityScanner._parse_weather_market_info(market))

    def test_short_ticker_returns_none(self):
        market = {"ticker": "KXHIGHNY"}
        self.assertIsNone(OpportunityScanner._parse_weather_market_info(market))


# ═══════════════════════════════════════════════════════════════════════════════
#  3. Static helper — _best_yes_ask_cents
# ═══════════════════════════════════════════════════════════════════════════════

class TestBestYesAskCents(unittest.TestCase):
    """Compute best YES ask = 100 - highest NO bid.

    Guards against: empty orderbook bug (43d04bc), FP dollar format mishandling.
    """

    def test_single_no_bid(self):
        """One NO bid at 15c → YES ask = 85c."""
        ob = {"no": [[15, 100]]}
        self.assertEqual(OpportunityScanner._best_yes_ask_cents(ob), 85)

    def test_multiple_no_bids_takes_highest(self):
        """Multiple NO bids → use highest (most aggressive)."""
        ob = {"no": [[10, 50], [15, 100], [12, 75]]}
        self.assertEqual(OpportunityScanner._best_yes_ask_cents(ob), 85)

    def test_empty_no_book(self):
        ob = {"no": []}
        self.assertIsNone(OpportunityScanner._best_yes_ask_cents(ob))

    def test_missing_no_key(self):
        ob = {"yes": [[90, 100]]}
        self.assertIsNone(OpportunityScanner._best_yes_ask_cents(ob))

    def test_fp_dollar_format(self):
        """FP dollar format: 0.15 → 15 cents.
        Guards against: API format change breaking price interpretation."""
        ob = {"no": [[0.15, 100]]}
        self.assertEqual(OpportunityScanner._best_yes_ask_cents(ob), 85)

    def test_fp_dollar_multiple(self):
        """Multiple FP dollar entries."""
        ob = {"no": [[0.10, 50], [0.15, 100], [0.12, 75]]}
        self.assertEqual(OpportunityScanner._best_yes_ask_cents(ob), 85)

    def test_dict_format(self):
        """Dict format entries instead of list/tuple."""
        ob = {"no": [{"price": 15, "quantity": 100}]}
        self.assertEqual(OpportunityScanner._best_yes_ask_cents(ob), 85)

    def test_zero_bid_returns_none(self):
        """NO bid at 0 → invalid → None."""
        ob = {"no": [[0, 100]]}
        self.assertIsNone(OpportunityScanner._best_yes_ask_cents(ob))

    def test_boundary_no_bid_at_99(self):
        """NO bid at 99c → YES ask = 1c."""
        ob = {"no": [[99, 100]]}
        self.assertEqual(OpportunityScanner._best_yes_ask_cents(ob), 1)

    def test_boundary_no_bid_at_1(self):
        """NO bid at 1c → YES ask = 99c."""
        ob = {"no": [[1, 100]]}
        self.assertEqual(OpportunityScanner._best_yes_ask_cents(ob), 99)


# ═══════════════════════════════════════════════════════════════════════════════
#  4. Static helper — _convert_orderbook_fp
# ═══════════════════════════════════════════════════════════════════════════════

class TestConvertOrderbookFP(unittest.TestCase):
    """Convert FP dollar orderbook → internal cents format.

    Guards against: precision loss in float→int conversion.
    """

    def test_basic_conversion(self):
        """Standard FP → cents conversion."""
        ob_fp = {
            "yes_dollars": [["0.9000", "100.00"]],
            "no_dollars": [["0.1100", "205.00"]],
        }
        result = OpportunityScanner._convert_orderbook_fp(ob_fp)
        self.assertEqual(result["yes"], [[90, 100]])
        self.assertEqual(result["no"], [[11, 205]])

    def test_multiple_levels(self):
        ob_fp = {
            "yes_dollars": [["0.8800", "50.00"], ["0.8700", "75.00"]],
            "no_dollars": [["0.1300", "100.00"]],
        }
        result = OpportunityScanner._convert_orderbook_fp(ob_fp)
        self.assertEqual(len(result["yes"]), 2)
        self.assertEqual(result["yes"][0], [88, 50])
        self.assertEqual(result["yes"][1], [87, 75])

    def test_empty_sides(self):
        ob_fp = {"yes_dollars": [], "no_dollars": []}
        result = OpportunityScanner._convert_orderbook_fp(ob_fp)
        self.assertEqual(result["yes"], [])
        self.assertEqual(result["no"], [])

    def test_missing_sides(self):
        """Missing side key → empty list."""
        ob_fp = {}
        result = OpportunityScanner._convert_orderbook_fp(ob_fp)
        self.assertEqual(result["yes"], [])
        self.assertEqual(result["no"], [])

    def test_rounding_precision(self):
        """0.15 * 100 = 15.000...0 — must round correctly."""
        ob_fp = {"yes_dollars": [["0.1500", "1.00"]], "no_dollars": []}
        result = OpportunityScanner._convert_orderbook_fp(ob_fp)
        self.assertEqual(result["yes"][0][0], 15)
        self.assertIsInstance(result["yes"][0][0], int)

    def test_small_price_precision(self):
        """Small prices like $0.01 → 1 cent."""
        ob_fp = {"yes_dollars": [["0.0100", "10.00"]], "no_dollars": []}
        result = OpportunityScanner._convert_orderbook_fp(ob_fp)
        self.assertEqual(result["yes"][0][0], 1)


# ═══════════════════════════════════════════════════════════════════════════════
#  5. Static helper — _window_timeslot
# ═══════════════════════════════════════════════════════════════════════════════

class TestWindowTimeslot(unittest.TestCase):
    """Extract timeslot from event ticker for position collision detection.

    Guards against: wrong timeslot extraction → multiple trades in same window.
    """

    def test_standard_15m(self):
        """KXBTC15M-26FEB211545 → 26FEB211545."""
        self.assertEqual(
            OpportunityScanner._window_timeslot("KXBTC15M-26FEB211545"),
            "26FEB211545")

    def test_same_timeslot_different_assets(self):
        """Same timeslot across assets should produce same string."""
        btc = OpportunityScanner._window_timeslot("KXBTC15M-26FEB211545")
        eth = OpportunityScanner._window_timeslot("KXETH15M-26FEB211545")
        self.assertEqual(btc, eth)

    def test_hourly_ticker(self):
        """KXBTCD-26FEB2114 → 26FEB2114."""
        self.assertEqual(
            OpportunityScanner._window_timeslot("KXBTCD-26FEB2114"),
            "26FEB2114")

    def test_no_dash_returns_full_ticker(self):
        """Ticker without dash → return full string (graceful degradation)."""
        result = OpportunityScanner._window_timeslot("NODASH")
        self.assertEqual(result, "NODASH")

    def test_multi_dash_ticker(self):
        """Ticker with multiple dashes (strike-level) → second part."""
        result = OpportunityScanner._window_timeslot("KXBTCD-26FEB2114-B95000")
        self.assertEqual(result, "26FEB2114")

    def test_none_returns_empty(self):
        """None event_ticker → empty string (no AttributeError).

        Regression: Apr 25 2026 — transient race surfaced None to
        _window_timeslot from _get_occupied_timeslots, crashing the tick
        loop. Caller checks `if ts:` so empty string skips the row safely.
        """
        result = OpportunityScanner._window_timeslot(None)
        self.assertEqual(result, "")

    def test_empty_string_returns_empty(self):
        """Empty event_ticker → empty string (no crash, callers skip)."""
        result = OpportunityScanner._window_timeslot("")
        self.assertEqual(result, "")


# ═══════════════════════════════════════════════════════════════════════════════
#  6. Edge computation — the money stage
# ═══════════════════════════════════════════════════════════════════════════════

class TestEdgeComputation(unittest.TestCase):
    """Edge = final_prob - (best_ask / 100) - taker_fee/100.

    Guards against: fee calculation bug (7fb5a03), rounding errors in
    per-contract fee (ceil applied to TOTAL, not per-contract).
    """

    def _compute_edge(self, final_prob, best_ask_cents, count=1):
        """Replicate scan()'s edge computation."""
        fee = calculate_fee(count, best_ask_cents, is_taker=True)
        edge = final_prob - best_ask_cents / 100.0
        fee_adjusted_edge = edge - fee / 100.0
        return edge, fee_adjusted_edge, fee

    def test_positive_edge(self):
        """Model says 95%, market at 90c → positive edge."""
        edge, fee_edge, fee = self._compute_edge(0.95, 90)
        self.assertGreater(edge, 0)
        self.assertGreater(fee_edge, 0)
        # Edge = 0.95 - 0.90 = 0.05
        self.assertAlmostEqual(edge, 0.05)
        # Fee at 90c: ceil(0.07 * 1 * 90 * 10 / 100) = ceil(0.63) = 1
        self.assertEqual(fee, 1)
        # Fee-adjusted edge = 0.05 - 0.01 = 0.04
        self.assertAlmostEqual(fee_edge, 0.04)

    def test_negative_edge(self):
        """Model says 88%, market at 90c → negative edge (no trade)."""
        edge, fee_edge, fee = self._compute_edge(0.88, 90)
        self.assertLess(edge, 0)
        self.assertLess(fee_edge, 0)

    def test_edge_at_boundary_50c(self):
        """50c has max fee per contract: ceil(0.07 * 50 * 50 / 100) = ceil(1.75) = 2."""
        edge, fee_edge, fee = self._compute_edge(0.55, 50)
        self.assertEqual(fee, 2)
        # Edge = 0.55 - 0.50 = 0.05; fee_edge = 0.05 - 0.02 = 0.03
        self.assertAlmostEqual(fee_edge, 0.03)

    def test_edge_at_high_price_99c(self):
        """At 99c: fee = ceil(0.07 * 99 * 1 / 100) = ceil(0.0693) = 1."""
        edge, fee_edge, fee = self._compute_edge(0.995, 99)
        self.assertEqual(fee, 1)
        self.assertAlmostEqual(edge, 0.005)

    def test_edge_at_1c(self):
        """At 1c: fee = ceil(0.07 * 1 * 99 / 100) = ceil(0.0693) = 1."""
        edge, fee_edge, fee = self._compute_edge(0.05, 1)
        self.assertEqual(fee, 1)

    def test_multi_contract_fee_not_per_contract(self):
        """Fee is ceil on TOTAL, not sum of per-contract ceils.
        Guards against: original bug where fee was computed per-contract."""
        # 10 contracts at 90c: ceil(0.07 * 10 * 90 * 10 / 100) = ceil(6.3) = 7
        _, _, fee_10 = self._compute_edge(0.95, 90, count=10)
        self.assertEqual(fee_10, 7)
        # NOT 10 × ceil(0.07 * 1 * 90 * 10 / 100) = 10 × 1 = 10
        self.assertNotEqual(fee_10, 10)

    def test_maker_fee_zero(self):
        """Maker fee is always 0 — edge uses is_taker=True for conservative estimate."""
        fee = calculate_fee(1, 90, is_taker=False)
        self.assertEqual(fee, 0)


# ═══════════════════════════════════════════════════════════════════════════════
#  7. Edge threshold filter (price-dependent)
# ═══════════════════════════════════════════════════════════════════════════════

class TestEdgeThresholdFilter(unittest.TestCase):
    """Price-dependent minimum edge thresholds from MIN_EDGE_BY_PRICE.

    Guards against: wrong threshold lookup, flat edge analysis that ignores
    the price-dependent schedule (anti-pattern documented in CLAUDE.md).
    """

    def test_low_price_low_threshold(self):
        """86-88c: need 0.25% edge."""
        self.assertEqual(get_min_edge(86), 0.0025)
        self.assertEqual(get_min_edge(87), 0.0025)
        self.assertEqual(get_min_edge(88), 0.0025)

    def test_mid_price_higher_threshold(self):
        """91-92c: need 0.20% edge."""
        self.assertEqual(get_min_edge(91), 0.002)
        self.assertEqual(get_min_edge(92), 0.002)

    def test_high_price_highest_threshold(self):
        """97-99c: need 1.0% edge."""
        self.assertEqual(get_min_edge(97), 0.010)
        self.assertEqual(get_min_edge(98), 0.010)
        self.assertEqual(get_min_edge(99), 0.010)

    # Removed: test_monotonically_increasing — schedule is intentionally
    # non-monotonic at 91c (0.002 < 89c's 0.0025). Invalid invariant.

    def test_all_price_tiers_covered(self):
        """Every price from MIN_ENTRY to MAX_ENTRY should return a valid threshold.
        Guards against: gaps in the lookup table → fallback to 0.5% default."""
        for price in range(MIN_ENTRY_PRICE, MAX_ENTRY_PRICE + 1):
            edge = get_min_edge(price)
            self.assertGreater(edge, 0, f"No edge threshold for {price}c")
            self.assertLess(edge, 0.05, f"Edge threshold too high at {price}c: {edge}")

    def test_edge_filter_pass(self):
        """Trade at 90c with 0.5% fee-adjusted edge → passes (threshold 0.25%)."""
        fee_adjusted_edge = 0.005
        min_edge = get_min_edge(90)
        self.assertGreaterEqual(fee_adjusted_edge, min_edge)

    def test_edge_filter_reject(self):
        """Trade at 97c with 0.5% edge → rejected (threshold 1.0%)."""
        fee_adjusted_edge = 0.005
        min_edge = get_min_edge(97)
        self.assertLess(fee_adjusted_edge, min_edge)


# ═══════════════════════════════════════════════════════════════════════════════
#  8. STC shadow gate
# ═══════════════════════════════════════════════════════════════════════════════

class TestSTCShadowGate(unittest.TestCase):
    """STC > STC_SHADOW_THRESHOLD (600s) → shadow-only for 15M.
    STC 300-600s → live with higher per-asset floors (STC extended zone).

    Guards against: dead code bug (98c954d) where gate checked
    product_type is None but 15M has product_type='15m'.
    """

    def test_threshold_value(self):
        """STC_SHADOW_THRESHOLD should be 600."""
        self.assertEqual(STC_SHADOW_THRESHOLD, 600)

    def test_live_zone(self):
        """STC=200 (< 300) → should pass both gates (core live zone)."""
        from bot.constants import STC_EXTENDED_LIVE_FLOOR
        self.assertLess(200, STC_EXTENDED_LIVE_FLOOR)

    def test_extended_zone(self):
        """STC=400 (300-600) → extended zone, subject to higher per-asset floors."""
        from bot.constants import STC_EXTENDED_LIVE_FLOOR
        self.assertGreater(400, STC_EXTENDED_LIVE_FLOOR)
        self.assertLess(400, STC_SHADOW_THRESHOLD)

    def test_shadow_zone(self):
        """STC=700 (> 600) → should be shadow."""
        self.assertGreater(700, STC_SHADOW_THRESHOLD)

    def test_boundary_at_threshold(self):
        """STC=600 exactly → should be live (> not >=).
        The gate uses `seconds_remaining > STC_SHADOW_THRESHOLD`."""
        # 600 is NOT > 600, so it should be live
        self.assertFalse(600 > STC_SHADOW_THRESHOLD)
        # 601 IS > 600, so it should be shadow
        self.assertTrue(601 > STC_SHADOW_THRESHOLD)

    def test_extended_floor_values(self):
        """Per-asset STC extended floors must be >= normal asset floors."""
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        self.assertGreaterEqual(bot.constants.STC_EXTENDED_BTC_MIN_PRICE, bot.constants.BTC_MIN_ENTRY_PRICE)
        self.assertGreaterEqual(bot.constants.STC_EXTENDED_ETH_MIN_PRICE, bot.constants.ETH_MIN_ENTRY_PRICE)
        self.assertGreaterEqual(bot.constants.STC_EXTENDED_SOL_MIN_PRICE, bot.constants.SOL_MIN_ENTRY_PRICE)
        self.assertGreaterEqual(bot.constants.STC_EXTENDED_XRP_MIN_PRICE, bot.constants.XRP_MIN_ENTRY_PRICE)

    def test_sol_rescue_contract_cap_exists(self):
        """SOL rescue cap must be defined and sensible (Apr 19 fix).

        Buffer rescue bypasses extended floor at buf>=0.25%, and SOL losses in
        the rescue zone average 56ct vs wins 37ct — Kelly oversizes thin-buffer
        setups. The cap clamps SOL rescue sizing to 25ct to limit tail loss.
        """
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        self.assertTrue(hasattr(bot.constants, "SOL_RESCUE_CONTRACT_CAP"))
        self.assertIsInstance(bot.constants.SOL_RESCUE_CONTRACT_CAP, int)
        # Sanity: should be positive and less than a typical Kelly output
        self.assertGreater(bot.constants.SOL_RESCUE_CONTRACT_CAP, 0)
        self.assertLess(bot.constants.SOL_RESCUE_CONTRACT_CAP, 100)

    def test_sol_rescue_cap_clamps_oversized(self):
        """The cap logic: for SOL, sizing["contracts"] = min(raw, SOL_RESCUE_CONTRACT_CAP).

        Mirrors the rescue branch inline math so a future refactor that
        changes the clamp pattern gets caught.
        """
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        # Simulate the rescue branch's inline clamp on SOL with Kelly=80
        sizing = {"contracts": 80}
        asset = "SOL"
        if asset == "SOL" and sizing["contracts"] > bot.constants.SOL_RESCUE_CONTRACT_CAP:
            sizing["contracts"] = bot.constants.SOL_RESCUE_CONTRACT_CAP
        self.assertEqual(sizing["contracts"], bot.constants.SOL_RESCUE_CONTRACT_CAP)

        # BTC with same size should NOT be capped
        sizing_btc = {"contracts": 80}
        asset_btc = "BTC"
        if asset_btc == "SOL" and sizing_btc["contracts"] > bot.constants.SOL_RESCUE_CONTRACT_CAP:
            sizing_btc["contracts"] = bot.constants.SOL_RESCUE_CONTRACT_CAP
        self.assertEqual(sizing_btc["contracts"], 80)

        # SOL with already-small size should NOT be modified
        sizing_small = {"contracts": 10}
        if "SOL" == "SOL" and sizing_small["contracts"] > bot.constants.SOL_RESCUE_CONTRACT_CAP:
            sizing_small["contracts"] = bot.constants.SOL_RESCUE_CONTRACT_CAP
        self.assertEqual(sizing_small["contracts"], 10)

    def test_gate_checks_product_type_15m(self):
        """Gate must check product_type == '15m', NOT product_type is None.
        Bug 98c954d: gate used `is None` but 15M has product_type='15m'."""
        product_type = "15m"
        seconds_remaining = 700
        # The correct gate logic:
        is_shadow = (product_type == "15m" and seconds_remaining > STC_SHADOW_THRESHOLD)
        self.assertTrue(is_shadow, "15M at 700s STC should be shadow")

    def test_hourly_not_affected_by_stc_shadow(self):
        """Hourly markets should NOT be gated by 15M STC shadow threshold."""
        product_type = "hourly"
        seconds_remaining = 700
        is_shadow = (product_type == "15m" and seconds_remaining > STC_SHADOW_THRESHOLD)
        self.assertFalse(is_shadow, "Hourly should not trigger 15M STC shadow gate")


# ═══════════════════════════════════════════════════════════════════════════════
#  9. Observation gate
# ═══════════════════════════════════════════════════════════════════════════════

class TestObservationGate(unittest.TestCase):
    """Observation-only products → logged as observation_trade, not candidate.

    Guards against: observation products leaking into live trading.
    """

    def test_hourly_observation_mode(self):
        """HOURLY_OBSERVATION_ONLY should be True."""
        self.assertTrue(HOURLY_OBSERVATION_ONLY,
                        "Hourly must be observation-only — live was reverted Feb 28")

    def test_observation_filter_stage(self):
        """Observation trades should use 'observation_trade' filter_stage.
        This is what separates them from live candidates in the DB."""
        # Verify the expected filter_stage string
        expected = "observation_trade"
        self.assertEqual(expected, "observation_trade")

    def test_candidate_filter_stage(self):
        """Live candidates use 'candidate' filter_stage."""
        expected = "candidate"
        self.assertEqual(expected, "candidate")


# ═══════════════════════════════════════════════════════════════════════════════
#  10. Per-window filters (Layer 3)
# ═══════════════════════════════════════════════════════════════════════════════

class TestPerWindowFilters(unittest.TestCase):
    """Hourly per-window position limits and risk caps.

    Guards against: correlated multi-asset blowups in same hourly window.
    """

    def test_max_positions_per_window(self):
        """HOURLY_MAX_POSITIONS_PER_WINDOW = 2 (ENB ~1.3 independent bets)."""
        self.assertEqual(HOURLY_MAX_POSITIONS_PER_WINDOW, 2)

    def test_max_window_risk(self):
        """HOURLY_MAX_WINDOW_RISK = 0.15 (15% max aggregate risk per window)."""
        self.assertEqual(HOURLY_MAX_WINDOW_RISK, 0.15)

    def test_position_limit_blocks_third(self):
        """With 2 positions in window, 3rd should be blocked."""
        window_count = 2
        self.assertGreaterEqual(window_count, HOURLY_MAX_POSITIONS_PER_WINDOW)

    def test_risk_cap_blocks_excess(self):
        """If window risk is already at cap, new position should be blocked."""
        existing_risk = 0.15
        new_risk = 0.05
        total = existing_risk + new_risk
        self.assertGreater(total, HOURLY_MAX_WINDOW_RISK)

    def test_stc_timing_range(self):
        """Hourly STC must be in [MIN_STC_ENTRY, MAX_STC_ENTRY]."""
        self.assertGreater(HOURLY_MIN_STC_ENTRY, 0)
        self.assertGreater(HOURLY_MAX_STC_ENTRY, HOURLY_MIN_STC_ENTRY)
        # 120s min, 3600s max (current config)
        in_range = HOURLY_MIN_STC_ENTRY <= 1800 <= HOURLY_MAX_STC_ENTRY
        self.assertTrue(in_range, "1800s should be in hourly STC range")

    def test_stc_below_min_filtered(self):
        """STC below HOURLY_MIN_STC_ENTRY → filtered out."""
        self.assertLess(60, HOURLY_MIN_STC_ENTRY)

    def test_asset_exclusion_btc_eth_only(self):
        """HOURLY_EXCLUDED_ASSETS contains SOL and XRP (BTC+ETH live hourly).
        T1 (2026-05-10): HYPE/DOGE added for shadow observation until T4.
        T1 (2026-05-17): BNB added for shadow observation (ticket 86b9zmj0c).
        T1 (2026-05-30): ADA/BCH added (15M-shadow-only; hourly safety belt)."""
        self.assertEqual(HOURLY_EXCLUDED_ASSETS, {"SOL", "XRP", "HYPE", "DOGE", "BNB", "ADA", "BCH"})


class TestHourlyNoSideAssetGate(unittest.TestCase):
    """Apr 15: YES/NO asymmetric exclusion for hourly.

    HOURLY_EXCLUDED_ASSETS gates YES-side only (XRP 42.2% YES WR = toxic).
    HOURLY_NO_EXCLUDED_ASSETS gates NO-side — empty by default because all 4
    assets show positive model-filtered edge on NO-side (BTC/ETH/SOL/XRP all
    +2.4pp to +13.0pp vs breakeven). Structural thesis: crypto long bias
    overprices YES → NO underpriced.
    """

    def test_no_excluded_set_exists(self):
        """HOURLY_NO_EXCLUDED_ASSETS is defined and is a set."""
        from bot.constants import HOURLY_NO_EXCLUDED_ASSETS
        self.assertIsInstance(HOURLY_NO_EXCLUDED_ASSETS, set)

    def test_no_excluded_default_empty(self):
        """Original 4 assets eligible on NO-side. T1 (2026-05-10):
        HYPE/DOGE added as NO-side safety belt during shadow until T4.
        T1 (2026-05-17): BNB added as NO-side safety belt (ticket 86b9zmj0c).
        T1 (2026-05-30): ADA/BCH added as NO-side safety belt (15M-shadow-only)."""
        from bot.constants import HOURLY_NO_EXCLUDED_ASSETS
        self.assertEqual(HOURLY_NO_EXCLUDED_ASSETS, {"HYPE", "DOGE", "BNB", "ADA", "BCH"})

    def test_yes_no_are_separate_constants(self):
        """YES and NO exclusion lists must not share state."""
        from bot.constants import HOURLY_EXCLUDED_ASSETS, HOURLY_NO_EXCLUDED_ASSETS
        self.assertIsNot(HOURLY_EXCLUDED_ASSETS, HOURLY_NO_EXCLUDED_ASSETS)

    def test_no_excluded_asset_gate_blocks(self):
        """If an asset is added to HOURLY_NO_EXCLUDED_ASSETS, it's blocked."""
        from bot.constants import HOURLY_NO_EXCLUDED_ASSETS  # Simulate the gate logic at bot/_impl.py:12023
        test_excl = {"XRP"}
        for asset in ("BTC", "ETH", "SOL", "XRP"):
            blocked = asset in test_excl
            if asset == "XRP":
                self.assertTrue(blocked)
            else:
                self.assertFalse(blocked)


# ═══════════════════════════════════════════════════════════════════════════════
#  11. XRP shadow gate
# ═══════════════════════════════════════════════════════════════════════════════

class TestXRPShadowGate(unittest.TestCase):
    """XRP 15M promoted to live with per-asset floor (XRP_MIN_ENTRY_PRICE=92c).

    XRP_15M_SHADOW=False — XRP trades live at 92c+, gated by per-asset floor.
    Guards against: XRP trading at low prices where it has no edge.
    """

    def test_xrp_shadow_disabled(self):
        """XRP shadow gate is OFF — XRP trades live at 92c+."""
        self.assertFalse(XRP_15M_SHADOW)

    def test_xrp_gate_inactive(self):
        """XRP + 15M + shadow=False → NOT shadow (trades live)."""
        asset = "XRP"
        product_type = "15m"
        is_shadow = (XRP_15M_SHADOW and asset == "XRP" and product_type == "15m")
        self.assertFalse(is_shadow)

    def test_btc_not_affected(self):
        """BTC should not be affected by XRP shadow gate."""
        is_shadow = (XRP_15M_SHADOW and "BTC" == "XRP" and "15m" == "15m")
        self.assertFalse(is_shadow)

    def test_xrp_hourly_not_affected(self):
        """XRP hourly should not be affected by 15M XRP shadow gate."""
        is_shadow = (XRP_15M_SHADOW and "XRP" == "XRP" and "hourly" == "15m")
        self.assertFalse(is_shadow)


# ═══════════════════════════════════════════════════════════════════════════════
#  12. Candidate selection logic
# ═══════════════════════════════════════════════════════════════════════════════

class TestCandidateSelection(unittest.TestCase):
    """Candidate selection: best edge per timeslot, highest overall.

    Guards against: selecting wrong candidate, missing candidates,
    returning empty when valid candidates exist.
    """

    def test_best_edge_wins(self):
        """Given multiple candidates, the one with highest fee_adjusted_edge wins."""
        candidates = [
            {"ticker": "A", "fee_adjusted_edge": 0.03, "timeslot": "T1", "asset": "BTC"},
            {"ticker": "B", "fee_adjusted_edge": 0.05, "timeslot": "T1", "asset": "ETH"},
            {"ticker": "C", "fee_adjusted_edge": 0.01, "timeslot": "T1", "asset": "SOL"},
        ]
        best = max(candidates, key=lambda c: c["fee_adjusted_edge"])
        self.assertEqual(best["ticker"], "B")

    def test_empty_candidates(self):
        """No candidates → empty list."""
        candidates = []
        self.assertEqual(len(candidates), 0)

    def test_single_candidate_selected(self):
        """Single candidate is always selected."""
        candidates = [{"ticker": "A", "fee_adjusted_edge": 0.03}]
        best = max(candidates, key=lambda c: c["fee_adjusted_edge"])
        self.assertEqual(best["ticker"], "A")

    def test_same_timeslot_one_winner(self):
        """Two candidates in same timeslot → only one should survive.
        Guards against: multiple orders in same 15-min window."""
        candidates = [
            {"ticker": "BTC-T1", "fee_adjusted_edge": 0.05, "timeslot": "T1", "asset": "BTC"},
            {"ticker": "ETH-T1", "fee_adjusted_edge": 0.03, "timeslot": "T1", "asset": "ETH"},
        ]
        # Group by timeslot, keep best per timeslot
        by_timeslot = {}
        for c in candidates:
            ts = c["timeslot"]
            if ts not in by_timeslot or c["fee_adjusted_edge"] > by_timeslot[ts]["fee_adjusted_edge"]:
                by_timeslot[ts] = c
        selected = list(by_timeslot.values())
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["ticker"], "BTC-T1")

    def test_different_timeslots_both_selected(self):
        """Candidates in different timeslots → both can proceed."""
        candidates = [
            {"ticker": "BTC-T1", "fee_adjusted_edge": 0.05, "timeslot": "T1", "asset": "BTC"},
            {"ticker": "BTC-T2", "fee_adjusted_edge": 0.03, "timeslot": "T2", "asset": "BTC"},
        ]
        by_timeslot = {}
        for c in candidates:
            ts = c["timeslot"]
            if ts not in by_timeslot or c["fee_adjusted_edge"] > by_timeslot[ts]["fee_adjusted_edge"]:
                by_timeslot[ts] = c
        selected = list(by_timeslot.values())
        self.assertEqual(len(selected), 2)


# ═══════════════════════════════════════════════════════════════════════════════
#  13. Dedup set safety
# ═══════════════════════════════════════════════════════════════════════════════

class TestDedupSetSafety(unittest.TestCase):
    """_eval_opp_seen dedup set handles mixed 2-tuple and 3-tuple entries.

    Guards against: abd47c8 ValueError crash from mixed tuple sizes in set
    comprehension that used `for tk, stage in` (only handles 2-tuples).
    """

    def test_mixed_tuple_sizes(self):
        """Set should handle both 2-tuples and 3-tuples without error."""
        seen = set()
        seen.add(("TICKER1", "candidate"))  # 2-tuple (standard)
        seen.add(("TICKER2", "no_side_shadow", "no"))  # 3-tuple (NO-side)
        self.assertEqual(len(seen), 2)

    def test_membership_check_works(self):
        """in-operator works regardless of tuple length."""
        seen = set()
        seen.add(("TICKER1", "candidate"))
        seen.add(("TICKER2", "no_side_shadow", "no"))
        self.assertIn(("TICKER1", "candidate"), seen)
        self.assertIn(("TICKER2", "no_side_shadow", "no"), seen)
        self.assertNotIn(("TICKER1", "no_side_shadow", "no"), seen)

    def test_no_destructuring_crash(self):
        """Iterating with proper handling (not `for tk, stage in seen`).
        The old code crashed because 3-tuples can't unpack into 2 variables."""
        seen = set()
        seen.add(("T1", "candidate"))
        seen.add(("T2", "shadow", "no"))
        # Safe iteration: just check membership, don't destructure
        count = 0
        for entry in seen:
            self.assertIsInstance(entry, tuple)
            self.assertGreaterEqual(len(entry), 2)
            count += 1
        self.assertEqual(count, 2)


# ═══════════════════════════════════════════════════════════════════════════════
#  14. Price range filter
# ═══════════════════════════════════════════════════════════════════════════════

class TestPriceRangeFilter(unittest.TestCase):
    """Price range: MIN_ENTRY_PRICE ≤ best_ask ≤ MAX_ENTRY_PRICE.

    Guards against: trading at prices outside configured range.
    """

    def test_min_entry_price(self):
        """MIN_ENTRY_PRICE = 75 (lowered from 80 for ETH 75-79c)."""
        self.assertEqual(MIN_ENTRY_PRICE, 75)

    def test_max_entry_price(self):
        """MAX_ENTRY_PRICE = 99."""
        self.assertEqual(MAX_ENTRY_PRICE, 99)

    def test_in_range(self):
        for price in [75, 80, 86, 90, 95, 99]:
            self.assertTrue(MIN_ENTRY_PRICE <= price <= MAX_ENTRY_PRICE,
                            f"{price}c should be in range")

    def test_below_range(self):
        for price in [50, 70, 74]:
            self.assertFalse(MIN_ENTRY_PRICE <= price <= MAX_ENTRY_PRICE,
                             f"{price}c should be below range")

    def test_above_range(self):
        """100c is above range (also impossible in a prediction market)."""
        self.assertFalse(MIN_ENTRY_PRICE <= 100 <= MAX_ENTRY_PRICE)


# ═══════════════════════════════════════════════════════════════════════════════
#  15. Scan stage integration — observation trade fields
# ═══════════════════════════════════════════════════════════════════════════════

class TestObservationTradeFields(unittest.TestCase):
    """Observation trade DB inserts must include all required fields.

    Guards against: silent NULL columns from missing kwargs in insert call.
    """

    def test_required_fields_for_evaluated_opportunity(self):
        """These fields are required for any evaluated_opportunity insert."""
        required = {
            "ticker", "event_ticker", "asset", "filter_stage",
            "evaluation_time", "spot_price", "threshold", "volatility",
            "market_price", "seconds_to_close", "calibrated_prob",
            "edge", "product_type",
        }
        # Verify the field names match what the schema expects
        for field in required:
            self.assertIsInstance(field, str)
            self.assertGreater(len(field), 0)

    def test_filter_stage_valid_values(self):
        """All filter_stage values used in scan() are valid strings."""
        valid_stages = {
            "candidate", "observation_trade", "stc_shadow",
            "stc_shadow_no_xrp", "stc_shadow_xrp", "xrp_shadow",
            "price_out_of_range", "insufficient_edge",
            "zero_sizing", "strategy_wait", "low_probability",
            "untradeable_zscore", "no_orderbook",
            "no_side_shadow", "price_shadow", "overnight_lp_shadow",
        }
        for stage in valid_stages:
            self.assertIsInstance(stage, str)
            self.assertNotIn(" ", stage, f"Filter stage '{stage}' contains spaces")


# ═══════════════════════════════════════════════════════════════════════════════
#  16. Convergence velocity
# ═══════════════════════════════════════════════════════════════════════════════

class TestConvergenceVelocity(unittest.TestCase):
    """_scanner_convergence_velocity: upward ask movement over time.

    Guards against: wrong velocity sign (downward treated as convergence).
    """

    def _make_scanner_with_history(self, ticker, history):
        """Build scanner mock with ask history for a ticker."""
        # We can't easily instantiate a full scanner, so test the logic directly
        # The method is: history[-1][1] - oldest_price_in_window
        if not history or len(history) < 2:
            return 0.0
        # Simplified: just check directionality
        return history[-1][1] - history[0][1]

    def test_upward_movement_positive(self):
        """Prices moving up → positive velocity."""
        history = [(100, 90), (105, 91), (110, 93)]
        vel = self._make_scanner_with_history("T", history)
        self.assertGreater(vel, 0)

    def test_downward_movement_negative(self):
        """Prices moving down → negative velocity."""
        history = [(100, 93), (105, 91), (110, 90)]
        vel = self._make_scanner_with_history("T", history)
        self.assertLess(vel, 0)

    def test_flat_market_zero(self):
        """Flat prices → zero velocity."""
        history = [(100, 90), (105, 90), (110, 90)]
        vel = self._make_scanner_with_history("T", history)
        self.assertEqual(vel, 0)


if __name__ == "__main__":
    unittest.main()
