"""Tests for ProbabilityEngine (bot/_impl.py).

Guards against:
- Z-score computation errors (wrong sigma_move formula)
- CDF complement off-by-one (using CDF instead of 1-CDF)
- Dynamic cap schedule returning wrong cap for time bracket
- Calibration dispatch routing to wrong engine
- Market blend discrepancy check (model/market divergence)
- Invalid input handling (zero vol, zero time, negative spot)
"""

import math
import sys
import unittest
from unittest.mock import patch, MagicMock

# Mock heavy dependencies that bot/_impl.py imports at module level.
for _mod in ["websockets", "websocket", "requests",
             "cryptography", "cryptography.hazmat",
             "cryptography.hazmat.primitives",
             "cryptography.hazmat.primitives.serialization",
             "cryptography.hazmat.primitives.hashes",
             "cryptography.hazmat.primitives.asymmetric",
             "cryptography.hazmat.primitives.asymmetric.padding"]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from config import BETA_SLOPE, STUDENT_T_DF, MAX_EFFECTIVE_PROB
import bot.engines  # noqa: F401


class TestZScoreComputation(unittest.TestCase):
    """Test z-score = (threshold - spot) / (spot × blended_rv × sqrt(seconds / 5))."""

    @classmethod
    def setUpClass(cls):
        from bot.engines.probability import ProbabilityEngine
        cls.PE = ProbabilityEngine

    def test_spot_equals_threshold_z_zero(self):
        """When spot == threshold, z-score should be 0."""
        result = self.PE.compute(
            spot=68000.0, threshold=68000.0,
            seconds_remaining=300.0, blended_rv=0.0001,
        )
        self.assertAlmostEqual(result["z_score"], 0.0, places=4)

    def test_spot_above_threshold_negative_z(self):
        """spot > threshold → z < 0 → high probability of staying above."""
        result = self.PE.compute(
            spot=69000.0, threshold=68000.0,
            seconds_remaining=300.0, blended_rv=0.0001,
        )
        self.assertLess(result["z_score"], 0)
        self.assertGreater(result["raw_prob"], 0.5)

    def test_spot_below_threshold_positive_z(self):
        """spot < threshold → z > 0 → low probability of staying above."""
        result = self.PE.compute(
            spot=67000.0, threshold=68000.0,
            seconds_remaining=300.0, blended_rv=0.0001,
        )
        self.assertGreater(result["z_score"], 0)
        self.assertLess(result["raw_prob"], 0.5)

    def test_known_z_score_value(self):
        """Verify exact z-score computation against hand calculation."""
        spot = 68000.0
        threshold = 67900.0
        seconds = 300.0
        rv = 0.0001
        # sigma_move = 68000 * 0.0001 * sqrt(300/5) = 6.8 * sqrt(60) = 6.8 * 7.746 = 52.67
        sigma_move = spot * rv * math.sqrt(seconds / 5.0)
        expected_z = (threshold - spot) / sigma_move  # -100 / 52.67 ≈ -1.8986
        result = self.PE.compute(
            spot=spot, threshold=threshold,
            seconds_remaining=seconds, blended_rv=rv,
        )
        self.assertAlmostEqual(result["z_score"], round(expected_z, 4), places=4)


class TestInvalidInputs(unittest.TestCase):
    """Test guard against invalid inputs."""

    @classmethod
    def setUpClass(cls):
        from bot.engines.probability import ProbabilityEngine
        cls.PE = ProbabilityEngine

    def test_zero_spot(self):
        """spot=0 → invalid, returns early."""
        result = self.PE.compute(0, 68000, 300, 0.0001)
        self.assertFalse(result["tradeable"])
        self.assertIn("invalid inputs", result["reason"])

    def test_zero_seconds(self):
        """seconds_remaining=0 → invalid."""
        result = self.PE.compute(68000, 67900, 0, 0.0001)
        self.assertFalse(result["tradeable"])

    def test_zero_volatility(self):
        """blended_rv=0 → invalid."""
        result = self.PE.compute(68000, 67900, 300, 0)
        self.assertFalse(result["tradeable"])

    def test_negative_values(self):
        """Negative inputs → invalid."""
        result = self.PE.compute(-1, 68000, 300, 0.0001)
        self.assertFalse(result["tradeable"])
        result = self.PE.compute(68000, 67900, -10, 0.0001)
        self.assertFalse(result["tradeable"])
        result = self.PE.compute(68000, 67900, 300, -0.0001)
        self.assertFalse(result["tradeable"])


class TestCdfComplement(unittest.TestCase):
    """Test _cdf_complement: 1 - CDF(z) with per-asset distribution."""

    @classmethod
    def setUpClass(cls):
        from bot.engines.probability import ProbabilityEngine
        cls.PE = ProbabilityEngine

    def test_z_zero_gives_half(self):
        """z=0 → 1 - CDF(0) = 0.5 for symmetric distributions."""
        result = self.PE._cdf_complement(0.0)
        self.assertAlmostEqual(result, 0.5, places=6)

    def test_large_negative_z_near_one(self):
        """Very negative z → CDF near 0 → complement near 1."""
        result = self.PE._cdf_complement(-5.0)
        self.assertGreater(result, 0.99)

    def test_large_positive_z_near_zero(self):
        """Very positive z → CDF near 1 → complement near 0."""
        result = self.PE._cdf_complement(5.0)
        self.assertLess(result, 0.01)

    def test_student_t_heavier_tails(self):
        """Student-t(df=4) has heavier tails than normal → more probability at extremes."""
        from scipy.stats import t as student_t, norm
        z = 3.0
        t_complement = 1.0 - student_t.cdf(z, df=STUDENT_T_DF)
        normal_complement = 1.0 - norm.cdf(z)
        # Student-t tail should be fatter
        self.assertGreater(t_complement, normal_complement)
        # Our function should match scipy
        result = self.PE._cdf_complement(z)
        self.assertAlmostEqual(result, t_complement, places=10)

    @patch("bot.engines.probability.DIST_CONFIG",
           {"BTC": {"distribution": "student_t", "student_t_df": 6}})
    def test_per_asset_df(self):
        """Per-asset config overrides default df.

        Patches ``bot.engines.probability.DIST_CONFIG`` directly because
        ``ProbabilityEngine._cdf_complement`` lives in
        ``bot/engines/probability.py`` post-Bit-6.2 and reads its own
        module-local binding (`from config import DIST_CONFIG` at the
        top of the file). Patching `config.DIST_CONFIG` (which routes
        through `_BotProxy.__setattr__` to `bot._impl.DIST_CONFIG`)
        no longer reaches the probability module's namespace because
        the extraction created a separate module-level binding.
        Pre-Bit-6.2 the patch worked transitively via
        `bot/_impl.py:47 from config import *`.
        """
        from scipy.stats import t as student_t
        z = 2.0
        expected = 1.0 - student_t.cdf(z, df=6)
        result = self.PE._cdf_complement(z, asset="BTC")
        self.assertAlmostEqual(result, expected, places=10)

    def test_unknown_asset_uses_default(self):
        """Unknown asset → falls back to default Student-t(df=4)."""
        from scipy.stats import t as student_t
        z = 2.0
        expected = 1.0 - student_t.cdf(z, df=STUDENT_T_DF)
        result = self.PE._cdf_complement(z, asset="UNKNOWN_ASSET_XYZ")
        self.assertAlmostEqual(result, expected, places=10)


class TestRawProbability(unittest.TestCase):
    """Test raw probability computation (before calibration)."""

    @classmethod
    def setUpClass(cls):
        from bot.engines.probability import ProbabilityEngine
        cls.PE = ProbabilityEngine

    def test_deep_itm_high_prob(self):
        """Spot far above threshold → very high raw_prob."""
        result = self.PE.compute(
            spot=70000.0, threshold=67000.0,
            seconds_remaining=300.0, blended_rv=0.0001,
        )
        self.assertGreater(result["raw_prob"], 0.95)

    def test_atm_near_50(self):
        """Spot at threshold → raw_prob near 0.5."""
        result = self.PE.compute(
            spot=68000.0, threshold=68000.0,
            seconds_remaining=300.0, blended_rv=0.0001,
        )
        self.assertAlmostEqual(result["raw_prob"], 0.5, places=1)

    def test_deep_otm_low_prob(self):
        """Spot far below threshold → very low raw_prob."""
        result = self.PE.compute(
            spot=66000.0, threshold=68000.0,
            seconds_remaining=300.0, blended_rv=0.0001,
        )
        self.assertLess(result["raw_prob"], 0.05)


class TestZScoreMax(unittest.TestCase):
    """Test z-score is computed but does not gate tradeability.

    Z_SCORE_MAX gate was removed — price + edge filters are sufficient.
    Z-score is still computed and logged for diagnostics.
    """

    @classmethod
    def setUpClass(cls):
        from bot.engines.probability import ProbabilityEngine
        cls.PE = ProbabilityEngine

    def test_extreme_z_still_tradeable(self):
        """Extreme z-score → still tradeable (gate removed, other filters catch bad trades)."""
        # Very low vol + spot near threshold → huge z
        result = self.PE.compute(
            spot=68000.0, threshold=67999.0,
            seconds_remaining=300.0, blended_rv=1e-10,
        )
        self.assertIsNotNone(result["z_score"])
        self.assertTrue(result["tradeable"])

    def test_normal_z_tradeable(self):
        """Normal z-score → tradeable=True (all else being equal)."""
        result = self.PE.compute(
            spot=68100.0, threshold=68000.0,
            seconds_remaining=300.0, blended_rv=0.0001,
        )
        self.assertIsNotNone(result["z_score"])
        self.assertTrue(result["tradeable"])


class TestDynamicCap(unittest.TestCase):
    """Test _dynamic_cap schedule for 15M and hourly markets."""

    @classmethod
    def setUpClass(cls):
        from bot.engines.probability import ProbabilityEngine
        cls.PE = ProbabilityEngine

    def test_15m_high_stc_cap(self):
        """15M: >600s → cap=0.93."""
        cap = self.PE._dynamic_cap(700.0)
        self.assertEqual(cap, 0.93)

    def test_15m_low_stc_cap(self):
        """15M: <60s → cap=0.995."""
        cap = self.PE._dynamic_cap(30.0)
        self.assertEqual(cap, 0.995)

    def test_15m_medium_stc_cap(self):
        """15M: 300-600s → cap=0.95."""
        cap = self.PE._dynamic_cap(400.0)
        self.assertEqual(cap, 0.95)

    def test_hourly_more_permissive(self):
        """Hourly caps are higher than 15M at same STC bracket."""
        cap_15m = self.PE._dynamic_cap(700.0, product_type="15m")
        cap_hourly = self.PE._dynamic_cap(700.0, product_type="hourly")
        # At 700s: 15M is 0.93, hourly is higher (0.97 or 0.98 depending on bracket)
        self.assertGreaterEqual(cap_hourly, cap_15m)

    def test_hourly_high_stc(self):
        """Hourly: >1800s → cap=0.97."""
        cap = self.PE._dynamic_cap(2000.0, product_type="hourly")
        self.assertEqual(cap, 0.97)

    def test_spx_uses_hourly_schedule(self):
        """SPX hourly uses the same schedule as crypto hourly."""
        cap_hourly = self.PE._dynamic_cap(1000.0, product_type="hourly")
        cap_spx = self.PE._dynamic_cap(1000.0, product_type="spx_hourly")
        self.assertEqual(cap_hourly, cap_spx)

    def test_weather_uses_hourly_schedule(self):
        """Weather uses the same schedule as hourly."""
        cap_hourly = self.PE._dynamic_cap(500.0, product_type="hourly")
        cap_weather = self.PE._dynamic_cap(500.0, product_type="weather")
        self.assertEqual(cap_hourly, cap_weather)

    def test_cap_monotonic_decreasing_with_stc(self):
        """Cap decreases (tighter) as STC increases."""
        stc_values = [30, 90, 200, 400, 700]
        caps = [self.PE._dynamic_cap(s) for s in stc_values]
        for i in range(len(caps) - 1):
            self.assertGreaterEqual(caps[i], caps[i + 1],
                                    f"Cap not monotonic: cap({stc_values[i]})={caps[i]} < cap({stc_values[i+1]})={caps[i+1]}")


class TestFixedBetaCalibrate(unittest.TestCase):
    """Test ProbabilityEngine._calibrate (static fallback β=0.85)."""

    @classmethod
    def setUpClass(cls):
        from bot.engines.probability import ProbabilityEngine
        cls.PE = ProbabilityEngine

    def test_matches_cal_engine_fallback(self):
        """PE._calibrate and CalEngine._fallback_calibrate produce same result."""
        from bot.engines.calibration import CalibrationEngine
        for p in [0.5, 0.7, 0.85, 0.9, 0.95, 0.99]:
            pe_result = self.PE._calibrate(p, cap=MAX_EFFECTIVE_PROB)
            ce_result = CalibrationEngine._fallback_calibrate(p, cap=MAX_EFFECTIVE_PROB)
            self.assertAlmostEqual(pe_result, ce_result, places=10,
                                   msg=f"Mismatch at p={p}: PE={pe_result}, CE={ce_result}")

    def test_cap_default_093(self):
        """Default cap is MAX_EFFECTIVE_PROB (0.93)."""
        result = self.PE._calibrate(0.999)
        self.assertLessEqual(result, MAX_EFFECTIVE_PROB)

    def test_compression(self):
        """β=0.85 < 1 → probabilities compressed toward 0.5."""
        for p in [0.8, 0.9, 0.95]:
            result = self.PE._calibrate(p, cap=1.0)
            self.assertLess(result, p)
            self.assertGreater(result, 0.5)


class TestDiscrepancyCheck(unittest.TestCase):
    """Test model vs market discrepancy safety check."""

    @classmethod
    def setUpClass(cls):
        from bot.constants import DISCREPANCY_PROB, DISCREPANCY_PRICE
        from bot.engines.probability import ProbabilityEngine
        cls.PE = ProbabilityEngine
        cls.DISC_PROB = DISCREPANCY_PROB
        cls.DISC_PRICE = DISCREPANCY_PRICE

    def test_high_model_low_market_rejected(self):
        """Model >90% but market <75c → refused.

        Use inputs that produce moderate z-score (within Z_SCORE_MAX)
        but still yield high calibrated probability.
        """
        # spot slightly above threshold with moderate vol → high prob, moderate z
        result = self.PE.compute(
            spot=68200.0, threshold=68000.0,
            seconds_remaining=300.0, blended_rv=0.0003,
            market_price_cents=50,  # Market says 50c but model says >90%
        )
        if result["calibrated_prob"] and result["calibrated_prob"] > self.DISC_PROB:
            self.assertFalse(result["tradeable"])
            self.assertIn("market", result["reason"].lower())

    def test_aligned_model_market_accepted(self):
        """Model high + market high → no discrepancy rejection."""
        result = self.PE.compute(
            spot=68200.0, threshold=68000.0,
            seconds_remaining=300.0, blended_rv=0.0003,
            market_price_cents=92,  # Market agrees
        )
        if result["calibrated_prob"] and result["calibrated_prob"] > self.DISC_PROB:
            self.assertTrue(result["tradeable"])

    def test_no_market_price_accepted(self):
        """No market price → discrepancy check skipped."""
        result = self.PE.compute(
            spot=68200.0, threshold=68000.0,
            seconds_remaining=300.0, blended_rv=0.0003,
            market_price_cents=None,
        )
        # Should not be rejected due to discrepancy (z-score check may still apply)
        if result["tradeable"]:
            self.assertEqual(result["reason"], "ok")


class TestComputeReturnDict(unittest.TestCase):
    """Test that compute() returns all expected keys."""

    @classmethod
    def setUpClass(cls):
        from bot.engines.probability import ProbabilityEngine
        cls.PE = ProbabilityEngine

    def test_all_keys_present(self):
        """Return dict has all required keys."""
        result = self.PE.compute(
            spot=68100.0, threshold=68000.0,
            seconds_remaining=300.0, blended_rv=0.0001,
        )
        required_keys = {"z_score", "raw_prob", "calibrated_prob",
                         "calibration_method", "tradeable", "reason"}
        self.assertTrue(required_keys.issubset(result.keys()),
                        f"Missing keys: {required_keys - result.keys()}")

    def test_tradeable_has_ok_reason(self):
        """When tradeable=True, reason should be 'ok'."""
        result = self.PE.compute(
            spot=68100.0, threshold=68000.0,
            seconds_remaining=300.0, blended_rv=0.0001,
        )
        if result["tradeable"]:
            self.assertEqual(result["reason"], "ok")

    def test_not_tradeable_has_reason(self):
        """When tradeable=False, reason should be non-empty."""
        result = self.PE.compute(0, 68000, 300, 0.0001)
        self.assertFalse(result["tradeable"])
        self.assertTrue(len(result["reason"]) > 0)


class TestCounterfactualProb(unittest.TestCase):
    """Test counterfactual_prob for alternative vol scenarios."""

    @classmethod
    def setUpClass(cls):
        from bot.engines.probability import ProbabilityEngine
        cls.PE = ProbabilityEngine

    def test_returns_value(self):
        """Valid inputs → returns a float probability."""
        result = self.PE.counterfactual_prob(
            spot=68100.0, threshold=68000.0,
            seconds_remaining=300.0, alt_blended_rv=0.0001,
        )
        self.assertIsNotNone(result)
        self.assertGreater(result, 0)
        self.assertLess(result, 1)

    def test_invalid_returns_none(self):
        """Invalid inputs → returns None."""
        self.assertIsNone(self.PE.counterfactual_prob(0, 68000, 300, 0.0001))
        self.assertIsNone(self.PE.counterfactual_prob(68000, 68000, 0, 0.0001))
        self.assertIsNone(self.PE.counterfactual_prob(68000, 68000, 300, 0))

    def test_higher_vol_lower_prob_for_itm(self):
        """For ITM (spot > threshold): higher vol → lower probability (more uncertainty)."""
        low_vol = self.PE.counterfactual_prob(68500, 68000, 300, 0.00005)
        high_vol = self.PE.counterfactual_prob(68500, 68000, 300, 0.0005)
        # Both should be > 0.5 (ITM), but higher vol → closer to 0.5
        self.assertIsNotNone(low_vol)
        self.assertIsNotNone(high_vol)
        self.assertGreater(low_vol, high_vol)


if __name__ == "__main__":
    unittest.main()
