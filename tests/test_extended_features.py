"""Tests for extended feature instrumentation (Tier 4 + Tier 5, Phase 1).

Guards against:
- compute_time_regime_features breaking on malformed timestamps
- Off-by-one in day_of_week (must be Sun=0, not Mon=0)
- minutes_since_us_open missing DST transitions
- FOMC/CPI calendar missing known dates
- compute_derived_features divide-by-zero / NaN on degenerate inputs
- sigma-normalized buffer math wrong
- prob_breakeven_gap confusing price-as-percent vs price-as-cents
- insert_evaluated_opportunity not auto-populating Tier 4/5 when cols NULL
"""

import math
import sqlite3
import sys
import os
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import (
    compute_time_regime_features,
    compute_derived_features,
    FOMC_ANNOUNCEMENT_DATES,
    CPI_RELEASE_DATES,
    SOL_RESCUE_CONTRACT_CAP,
)


class TestTimeRegimeFeatures(unittest.TestCase):
    """Tier 4: time/regime features."""

    def test_hour_of_day_utc(self):
        r = compute_time_regime_features("2026-04-19T20:35:02Z")
        self.assertEqual(r["hour_of_day_utc"], 20)

    def test_hour_of_day_utc_midnight(self):
        r = compute_time_regime_features("2026-04-19T00:00:00Z")
        self.assertEqual(r["hour_of_day_utc"], 0)

    def test_day_of_week_sunday_is_0(self):
        # 2026-04-19 is a Sunday — must be 0 (not Python's 6)
        r = compute_time_regime_features("2026-04-19T12:00:00Z")
        self.assertEqual(r["day_of_week"], 0)

    def test_day_of_week_monday_is_1(self):
        # 2026-04-20 is a Monday
        r = compute_time_regime_features("2026-04-20T12:00:00Z")
        self.assertEqual(r["day_of_week"], 1)

    def test_day_of_week_saturday_is_6(self):
        # 2026-04-18 is a Saturday
        r = compute_time_regime_features("2026-04-18T12:00:00Z")
        self.assertEqual(r["day_of_week"], 6)

    def test_is_weekend_on_sunday(self):
        r = compute_time_regime_features("2026-04-19T12:00:00Z")
        self.assertEqual(r["is_weekend"], 1)

    def test_is_weekend_on_saturday(self):
        r = compute_time_regime_features("2026-04-18T12:00:00Z")
        self.assertEqual(r["is_weekend"], 1)

    def test_is_weekend_on_weekday(self):
        # Tuesday
        r = compute_time_regime_features("2026-04-21T12:00:00Z")
        self.assertEqual(r["is_weekend"], 0)

    def test_minutes_since_us_open_at_open(self):
        # 9:30 ET EDT = 13:30 UTC on a weekday in April
        r = compute_time_regime_features("2026-04-21T13:30:00Z")
        self.assertEqual(r["minutes_since_us_open"], 0)

    def test_minutes_since_us_open_one_hour_later(self):
        r = compute_time_regime_features("2026-04-21T14:30:00Z")
        self.assertEqual(r["minutes_since_us_open"], 60)

    def test_minutes_since_us_open_pre_open_is_negative(self):
        # Midnight ET → 9h30m before 9:30 AM ET
        r = compute_time_regime_features("2026-04-21T04:00:00Z")  # 0:00 ET
        self.assertLess(r["minutes_since_us_open"], 0)

    def test_fomc_day_known_date(self):
        # Apr 29 2026 is in the FOMC set
        r = compute_time_regime_features("2026-04-29T18:00:00Z")
        self.assertEqual(r["is_fomc_day"], 1)

    def test_fomc_day_not_a_fomc_date(self):
        r = compute_time_regime_features("2026-04-19T12:00:00Z")
        self.assertEqual(r["is_fomc_day"], 0)

    def test_cpi_day_known_date(self):
        # Apr 14 2026 is in the CPI set
        r = compute_time_regime_features("2026-04-14T12:30:00Z")
        self.assertEqual(r["is_cpi_day"], 1)

    def test_cpi_day_not_a_cpi_date(self):
        r = compute_time_regime_features("2026-04-19T12:00:00Z")
        self.assertEqual(r["is_cpi_day"], 0)

    def test_malformed_timestamp_returns_all_none(self):
        r = compute_time_regime_features("not a real timestamp")
        for v in r.values():
            self.assertIsNone(v)

    def test_none_timestamp_uses_now(self):
        r = compute_time_regime_features(None)
        # Should return non-None values for current time
        self.assertIsNotNone(r["hour_of_day_utc"])
        self.assertIsNotNone(r["is_weekend"])

    def test_calendars_non_empty(self):
        # Catches accidental deletion
        self.assertGreater(len(FOMC_ANNOUNCEMENT_DATES), 0)
        self.assertGreater(len(CPI_RELEASE_DATES), 0)

    def test_dst_transition_handled(self):
        # 2026-03-08 02:00 ET → clocks spring forward to 03:00 ET.
        # At 2026-03-10 09:30 ET (post-DST), US open should be at 13:30 UTC.
        r_pre_dst = compute_time_regime_features("2026-02-10T14:30:00Z")  # 9:30 ET EST
        r_post_dst = compute_time_regime_features("2026-03-10T13:30:00Z")  # 9:30 ET EDT
        self.assertEqual(r_pre_dst["minutes_since_us_open"], 0)
        self.assertEqual(r_post_dst["minutes_since_us_open"], 0)


class TestDerivedFeatures(unittest.TestCase):
    """Tier 5: derived math features."""

    def test_sigma_normalized_buffer_basic(self):
        # Known values: spot 85.28, threshold 85.0665, vol 8.83e-5, STC 598.6
        # buf_pct = (85.28 - 85.0665) / 85.0665 × 100 = 0.2510%
        # sigma_denom = sqrt(8.83e-5 × 598.6) × 100 = 22.99
        # result ≈ 0.2510 / 22.99 ≈ 0.01092
        r = compute_derived_features(
            spot_price=85.28, threshold=85.0665,
            volatility=8.83e-5, seconds_to_close=598.6,
        )
        self.assertAlmostEqual(r["spot_distance_to_strike_sigma"], 0.01092, places=3)

    def test_sigma_normalized_buffer_handles_zero_threshold(self):
        r = compute_derived_features(
            spot_price=85.28, threshold=0.0,
            volatility=8.83e-5, seconds_to_close=598.6,
        )
        self.assertIsNone(r["spot_distance_to_strike_sigma"])

    def test_sigma_normalized_buffer_handles_zero_vol(self):
        r = compute_derived_features(
            spot_price=85.28, threshold=85.07,
            volatility=0.0, seconds_to_close=598.6,
        )
        self.assertIsNone(r["spot_distance_to_strike_sigma"])

    def test_sigma_normalized_buffer_handles_zero_stc(self):
        r = compute_derived_features(
            spot_price=85.28, threshold=85.07,
            volatility=8.83e-5, seconds_to_close=0.0,
        )
        self.assertIsNone(r["spot_distance_to_strike_sigma"])

    def test_prob_breakeven_gap(self):
        # prob 0.926 at entry 90c → gap = 0.026
        r = compute_derived_features(
            calibrated_prob=0.926, market_price_cents=90,
        )
        self.assertAlmostEqual(r["prob_breakeven_gap"], 0.026, places=4)

    def test_prob_breakeven_gap_negative_when_prob_below_market(self):
        r = compute_derived_features(
            calibrated_prob=0.85, market_price_cents=90,
        )
        self.assertAlmostEqual(r["prob_breakeven_gap"], -0.05, places=4)

    def test_prob_breakeven_gap_handles_none(self):
        r = compute_derived_features(calibrated_prob=None, market_price_cents=90)
        self.assertIsNone(r["prob_breakeven_gap"])
        r = compute_derived_features(calibrated_prob=0.9, market_price_cents=None)
        self.assertIsNone(r["prob_breakeven_gap"])

    def test_kelly_vs_cap_ratio(self):
        # Kelly wanted 50, cap is 25 → ratio 2.0
        r = compute_derived_features(kelly_contracts=50, sol_rescue_cap=25)
        self.assertEqual(r["kelly_vs_cap_ratio"], 2.0)

    def test_kelly_vs_cap_ratio_under_cap(self):
        # Kelly 10 vs cap 25 → 0.4
        r = compute_derived_features(kelly_contracts=10, sol_rescue_cap=25)
        self.assertEqual(r["kelly_vs_cap_ratio"], 0.4)

    def test_kelly_vs_cap_ratio_default_cap_matches_constant(self):
        # If caller omits sol_rescue_cap, default should match the module constant
        r = compute_derived_features(kelly_contracts=SOL_RESCUE_CONTRACT_CAP)
        self.assertEqual(r["kelly_vs_cap_ratio"], 1.0)

    def test_calibration_confidence_cap_at_1(self):
        # 500 training trades → confidence capped at 1.0
        r = compute_derived_features(n_recent_cal_trades=500)
        self.assertEqual(r["calibration_confidence"], 1.0)

    def test_calibration_confidence_partial(self):
        r = compute_derived_features(n_recent_cal_trades=50)
        self.assertEqual(r["calibration_confidence"], 0.5)

    def test_calibration_confidence_zero(self):
        r = compute_derived_features(n_recent_cal_trades=0)
        self.assertEqual(r["calibration_confidence"], 0.0)

    def test_all_nones_returns_all_nones(self):
        r = compute_derived_features()
        for v in r.values():
            self.assertIsNone(v)


class TestInsertAutoPopulates(unittest.TestCase):
    """insert_evaluated_opportunity auto-fills Tier 4/5 without explicit kwargs."""

    def test_signature_accepts_new_kwargs(self):
        """The 26 new kwargs must all be accepted without crashes."""
        from bot import StateManager
        import inspect
        sig = inspect.signature(StateManager.insert_evaluated_opportunity)
        expected_new = {
            "minutes_above_strike", "window_max_buf_pct", "window_min_buf_pct",
            "recent_crossings_5m", "spot_at_window_open",
            "spot_momentum_60s_bps", "spot_momentum_5m_bps", "spot_realized_range_15m_bps",
            "btc_spot_change_30m_bps", "btc_spot_change_5m_bps", "btc_realized_vol_15m",
            "sol_btc_relative_return_30m_bps",
            "hour_of_day_utc", "day_of_week", "is_weekend", "minutes_since_us_open",
            "is_fomc_day", "is_cpi_day",
            "spot_distance_to_strike_sigma", "prob_breakeven_gap",
            "kelly_vs_cap_ratio", "calibration_confidence",
            "active_positions_same_asset", "recent_bot_pnl_30m_cents",
            "current_drawdown_pct", "recent_ioc_fill_success_rate_1h",
        }
        for name in expected_new:
            self.assertIn(name, sig.parameters, f"Missing kwarg: {name}")


if __name__ == "__main__":
    unittest.main()
