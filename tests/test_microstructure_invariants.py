"""Microstructure invariant tests for WS orderbook handling.

Why this file exists
--------------------
The WS contract tests (test_kalshi_ws_contracts.py) verify that wire-format
messages deserialize into the internal [cents, qty] format correctly. But
the contract tests use "clean touch" golden samples where
max(yes_bid) + max(no_bid) = 100. They do not exercise:

1. The spread-non-negativity invariant on well-ordered books (orthogonal
   to wire format — tests SEMANTICS of the downstream helpers).
2. The behavior of downstream helpers on a crossed book (observed in
   production ~35% of BTC rows and ~57% of ETH rows post-WS-fix Apr 23
   2026). Documents-current-behavior so future changes are visible.
3. The per-product calibration_confidence auto-populate path — the
   existing TestCalibrationConfidenceIntegration covers only 15M.

These tests are NOT redundant with the contract tests. They test semantic
invariants, not wire-format fidelity. See
kb/failures/kalshi-ws-schema-drift.md § "test-as-spec addendum" for why
wire-format-only contract tests are insufficient prevention.
"""

import os
import sys
import random
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import (
    OrderExecutor,
    OpportunityScanner,
    StateManager,
    compute_derived_features,
)
import bot


# ─────────────────────────────────────────────────────────────────────────────
# Spread invariant on well-ordered books
# ─────────────────────────────────────────────────────────────────────────────

class TestCleanBookSpreadInvariant(unittest.TestCase):
    """Property: on any book where max(yes_bid) + max(no_bid) <= 100, the
    derived spread (= (100 - max_no_bid) - max_yes_bid) must be >= 0.

    This is the invariant the Phase 1 KB assumes but never tests. The
    original end-to-end contract test only used max_yes_bid + max_no_bid =
    100 (clean touch), which is a single point on the invariant surface.
    """

    def test_clean_touch_produces_zero_spread(self):
        ob = {"yes": [[95, 100]], "no": [[5, 100]]}
        best_ask = OpportunityScanner._best_yes_ask_cents(ob)
        best_bid = OrderExecutor._best_yes_bid(ob)
        self.assertEqual(best_ask - best_bid, 0)

    def test_well_priced_book_produces_positive_spread(self):
        # yes_bid=95, no_bid=4 (sum=99) → ask=96, bid=95, spread=1
        ob = {"yes": [[95, 100]], "no": [[4, 50]]}
        best_ask = OpportunityScanner._best_yes_ask_cents(ob)
        best_bid = OrderExecutor._best_yes_bid(ob)
        self.assertEqual(best_ask - best_bid, 1)

    def test_wide_book_produces_positive_spread(self):
        # yes_bid=60, no_bid=20 (sum=80) → ask=80, bid=60, spread=20
        ob = {"yes": [[60, 10]], "no": [[20, 10]]}
        best_ask = OpportunityScanner._best_yes_ask_cents(ob)
        best_bid = OrderExecutor._best_yes_bid(ob)
        self.assertEqual(best_ask - best_bid, 20)

    def test_spread_nonneg_property(self):
        """Randomized: for any yes_bid + no_bid <= 100, spread >= 0."""
        random.seed(42)
        for _ in range(300):
            yes_bid = random.randint(1, 99)
            max_no = 100 - yes_bid
            if max_no < 1:
                continue
            no_bid = random.randint(1, max_no)
            ob = {"yes": [[yes_bid, 10]], "no": [[no_bid, 10]]}
            best_ask = OpportunityScanner._best_yes_ask_cents(ob)
            best_bid = OrderExecutor._best_yes_bid(ob)
            spread = best_ask - best_bid
            self.assertGreaterEqual(
                spread, 0,
                f"Spread negative on well-ordered book: yes_bid={yes_bid}, "
                f"no_bid={no_bid}, ask={best_ask}, bid={best_bid}, spread={spread}")

    def test_multi_level_book_uses_best_of_each_side(self):
        """Helpers must pick max on both sides regardless of list order."""
        ob = {
            "yes": [[40, 100], [92, 5], [70, 50]],
            "no":  [[2, 200], [7, 10],  [3, 500]],
        }
        best_ask = OpportunityScanner._best_yes_ask_cents(ob)
        best_bid = OrderExecutor._best_yes_bid(ob)
        self.assertEqual(best_ask, 93)   # 100 - max(no) = 100 - 7
        self.assertEqual(best_bid, 92)   # max(yes)
        self.assertEqual(best_ask - best_bid, 1)


# ─────────────────────────────────────────────────────────────────────────────
# Crossed-book behavior — DOCUMENTS current output, does not "fix" it
# ─────────────────────────────────────────────────────────────────────────────

class TestCrossedBookDocumentedBehavior(unittest.TestCase):
    """When max(yes_bid) + max(no_bid) > 100, the formula
    yes_spread_cents = (100 - max_no_bid) - max_yes_bid produces a NEGATIVE
    value. Whether this reflects:

      (H1) Real Kalshi state: stale limit orders on illiquid 15M markets
           that no arb bot has bothered to clear, AND
      (H2) WS delta mis-application producing phantom levels that never
           clear from our cached state,

    is UNRESOLVED as of 2026-04-24 pending empirical probe (see
    kb/failures/kalshi-ws-schema-drift.md § "test-as-spec addendum").

    These tests pin the CURRENT OUTPUT so any future formula change is
    visible in the diff. They do NOT assert correctness — they document
    behavior.
    """

    def test_crossed_book_sample_from_production(self):
        """Modeled on KXBTC15M-26APR232145-45 observed 2026-04-24 01:38 UTC:
        yes_spread_cents=-66, bid_depth=2287, ask_depth=1.
        Implied: best_yes_bid ≈ 72, best_no_bid ≈ 94, ask = 6, spread = -66.
        """
        ob = {"yes": [[72, 2287]], "no": [[94, 1]]}
        best_ask = OpportunityScanner._best_yes_ask_cents(ob)
        best_bid = OrderExecutor._best_yes_bid(ob)
        self.assertEqual(best_ask, 6)
        self.assertEqual(best_bid, 72)
        self.assertEqual(best_ask - best_bid, -66)

    def test_crossed_magnitude_equals_arbitrage_value(self):
        """Arb value on a crossed book = (yes_bid + no_bid) - 100.
        This should equal -(spread), i.e. how much free money is on the
        table if anyone bothers to sweep both sides.
        """
        yes_bid, no_bid = 72, 94
        ob = {"yes": [[yes_bid, 10]], "no": [[no_bid, 10]]}
        best_ask = OpportunityScanner._best_yes_ask_cents(ob)
        best_bid = OrderExecutor._best_yes_bid(ob)
        spread = best_ask - best_bid
        arb_value = (yes_bid + no_bid) - 100
        self.assertEqual(-spread, arb_value)

    def test_crossed_book_depths_are_independent_of_spread_sign(self):
        """bid_depth and ask_depth read top-of-book qty on their respective
        sides — the qty fields are unaffected by the crossed-vs-clean
        distinction. Documents that depth remains meaningful even when
        spread is nonsensical.
        """
        ob = {"yes": [[72, 2287]], "no": [[94, 1]]}
        self.assertEqual(OrderExecutor._best_yes_bid_depth(ob), 2287)
        self.assertEqual(OrderExecutor._best_ask_depth(ob), 1)


# ─────────────────────────────────────────────────────────────────────────────
# calibration_confidence per-product coverage
# ─────────────────────────────────────────────────────────────────────────────

class TestCalibrationConfidencePerProduct(unittest.TestCase):
    """Existing TestCalibrationConfidenceIntegration covers only the 15M
    path (_CALIBRATION_ENGINE). Production data (2026-04-24) shows 100%
    NULL for hourly/weather/sports rows — the per-product CalEngine
    branch at bot/_impl.py:2857 is uncovered.

    These tests exercise the non-15M branch so the coverage gap that
    produced 266 hourly + 240 weather + 26 sports NULL rows does not
    silently recur.
    """

    def _fresh_state_manager(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return StateManager(db_path=tmp.name)

    def _with_mock_get_cal_engine(self, product_type, n_obs):
        """Patch bot._resolve_cal_engine to return a mock for the given product."""
        mock_engine = MagicMock()
        mock_engine._observations = list(range(n_obs))

        def _patched(pt, asset, require_enabled=False):
            if pt == product_type:
                return mock_engine
            return None
        return _patched

    def test_hourly_populates_calibration_confidence(self):
        original = bot._resolve_cal_engine
        bot._resolve_cal_engine = self._with_mock_get_cal_engine("hourly", n_obs=50)
        try:
            sm = self._fresh_state_manager()
            sm.insert_evaluated_opportunity(
                ticker="KXHOUR-1", event_ticker="KXBTCD",
                asset="BTC", filter_stage="candidate",
                product_type="hourly",
            )
            row = sm.conn.execute(
                "SELECT calibration_confidence FROM evaluated_opportunities "
                "WHERE ticker=?", ("KXHOUR-1",)
            ).fetchone()
            self.assertIsNotNone(row["calibration_confidence"])
            self.assertAlmostEqual(row["calibration_confidence"], 0.5, places=6)
        finally:
            bot._resolve_cal_engine = original

    def test_weather_populates_calibration_confidence(self):
        original = bot._resolve_cal_engine
        bot._resolve_cal_engine = self._with_mock_get_cal_engine("weather", n_obs=25)
        try:
            sm = self._fresh_state_manager()
            sm.insert_evaluated_opportunity(
                ticker="KXWX-1", event_ticker="KXHIGHNY",
                asset="NY_TEMP", filter_stage="candidate",
                product_type="weather",
            )
            row = sm.conn.execute(
                "SELECT calibration_confidence FROM evaluated_opportunities "
                "WHERE ticker=?", ("KXWX-1",)
            ).fetchone()
            self.assertIsNotNone(row["calibration_confidence"])
            self.assertAlmostEqual(row["calibration_confidence"], 0.25, places=6)
        finally:
            bot._resolve_cal_engine = original

    def test_missing_per_product_engine_yields_null(self):
        """When _get_cal_engine returns None, calibration_confidence is None.
        Confirms the failure mode, doesn't mask it.
        """
        original = bot._resolve_cal_engine
        bot._resolve_cal_engine = lambda pt, asset, require_enabled=False: None
        try:
            sm = self._fresh_state_manager()
            sm.insert_evaluated_opportunity(
                ticker="KXSPORTS-1", event_ticker="KXNBAGAME",
                asset="NBA", filter_stage="candidate",
                product_type="sports",
            )
            row = sm.conn.execute(
                "SELECT calibration_confidence FROM evaluated_opportunities "
                "WHERE ticker=?", ("KXSPORTS-1",)
            ).fetchone()
            self.assertIsNone(row["calibration_confidence"])
        finally:
            bot._resolve_cal_engine = original


# ─────────────────────────────────────────────────────────────────────────────
# calibration_confidence saturation — DOCUMENTS current formula
# ─────────────────────────────────────────────────────────────────────────────

class TestCalibrationConfidenceSaturationDocumented(unittest.TestCase):
    """compute_derived_features uses `min(n/100, 1.0)`. For any CalEngine
    with ≥100 observations the output saturates to 1.0, eliminating
    differentiation between partially-trained and fully-trained engines.

    Production data (2026-04-24): 100% of 15M rows report 1.0 because the
    15M CalEngine has years of observations. Whether this formula should
    saturate at all is an open spec question (kb/decisions/TBD).

    These tests pin the current formula. Any future spec change must
    update both the formula AND these tests in the same commit.
    """

    def test_saturates_at_n_100(self):
        r = compute_derived_features(n_recent_cal_trades=100)
        self.assertEqual(r["calibration_confidence"], 1.0)

    def test_saturates_at_n_1000(self):
        r = compute_derived_features(n_recent_cal_trades=1000)
        self.assertEqual(r["calibration_confidence"], 1.0)

    def test_does_not_distinguish_well_trained_from_overtrained(self):
        """Key finding: once an engine crosses 100 obs, the feature stops
        carrying information. This is the production symptom.
        """
        r100 = compute_derived_features(n_recent_cal_trades=100)
        r10000 = compute_derived_features(n_recent_cal_trades=10000)
        self.assertEqual(r100["calibration_confidence"],
                         r10000["calibration_confidence"])


if __name__ == "__main__":
    unittest.main()
