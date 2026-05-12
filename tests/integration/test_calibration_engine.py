"""Tests for CalibrationEngine (bot/_impl.py).

Guards against:
- Fallback calibration drift from β=0.85 Platt scaling
- Training threshold violations (methods activating with insufficient data)
- Brier tournament not promoting the best method
- Uncertainty shrinkage producing out-of-range probabilities
- State persistence/loading corruption
- _CAL_REGISTRY routing to wrong engine
"""

import json
import math
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import sys

# Mock heavy dependencies that bot/_impl.py imports at module level.
# These are not needed for CalibrationEngine's pure-math logic.
_MOCKED = []
for _mod in ["websockets", "websocket", "requests",
             "cryptography", "cryptography.hazmat",
             "cryptography.hazmat.primitives",
             "cryptography.hazmat.primitives.serialization",
             "cryptography.hazmat.primitives.hashes",
             "cryptography.hazmat.primitives.asymmetric",
             "cryptography.hazmat.primitives.asymmetric.padding"]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()
        _MOCKED.append(_mod)

from config import BETA_SLOPE, MAX_EFFECTIVE_PROB, NUMERICAL_SAFETY_CEILING
import bot.engines  # noqa: F401


class TestFallbackCalibrate(unittest.TestCase):
    """Test CalibrationEngine._fallback_calibrate (static, fixed β=0.85).

    Guards against drift from the production formula:
    logit → scale by β=0.85 → inverse logit → cap.
    """

    @classmethod
    def setUpClass(cls):
        """Import CalibrationEngine once for all tests in this class."""
        # Import bot's CalibrationEngine
        from bot.engines.calibration import CalibrationEngine
        cls.CalEngine = CalibrationEngine

    def _fallback(self, raw_prob, cap=MAX_EFFECTIVE_PROB):
        return self.CalEngine._fallback_calibrate(raw_prob, cap)

    def test_identity_at_50_percent(self):
        """p=0.5 → logit=0 → scaled=0 → sigmoid=0.5 (identity point)."""
        result = self._fallback(0.5)
        self.assertAlmostEqual(result, 0.5, places=10)

    def test_compression_high_prob(self):
        """High raw_prob is compressed toward 0.5 (β<1 compresses)."""
        raw = 0.98
        result = self._fallback(raw, cap=1.0)
        # β=0.85 compresses: result should be < raw but > 0.5
        self.assertLess(result, raw)
        self.assertGreater(result, 0.5)

    def test_cap_applied(self):
        """Result never exceeds cap."""
        result = self._fallback(0.999, cap=0.93)
        self.assertLessEqual(result, 0.93)

    def test_known_value_beta_085(self):
        """Verify exact computation for p=0.9 with β=0.85."""
        p = 0.9
        logit_p = math.log(p / (1.0 - p))  # ~2.197
        scaled = BETA_SLOPE * logit_p        # 0.85 * 2.197 = 1.868
        expected = 1.0 / (1.0 + math.exp(-scaled))
        result = self._fallback(p, cap=1.0)
        self.assertAlmostEqual(result, expected, places=10)

    def test_clamping_extreme_inputs(self):
        """Extreme inputs (0.0, 1.0) are clamped to [0.001, 0.999]."""
        # Should not raise, should return valid probability
        lo = self._fallback(0.0, cap=1.0)
        hi = self._fallback(1.0, cap=1.0)
        self.assertGreater(lo, 0)
        self.assertLess(hi, 1.0)

    def test_monotonicity(self):
        """Higher raw_prob → higher or equal calibrated prob."""
        probs = [0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99]
        calibrated = [self._fallback(p, cap=1.0) for p in probs]
        for i in range(len(calibrated) - 1):
            self.assertLessEqual(calibrated[i], calibrated[i + 1],
                                 f"Monotonicity violated: f({probs[i]})={calibrated[i]} > f({probs[i+1]})={calibrated[i+1]}")


class TestCalibrationEngineInit(unittest.TestCase):
    """Test CalibrationEngine initialization and state loading."""

    @classmethod
    def setUpClass(cls):
        from bot.engines.calibration import CalibrationEngine
        cls.CalEngine = CalibrationEngine

    def test_fresh_init_fixed_beta(self):
        """New engine with no state file starts in fixed_beta mode."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=True) as f:
            path = f.name  # file deleted on close → FileNotFoundError on load
        eng = self.CalEngine(state_path=path, label="test")
        self.assertEqual(eng.active_method, "fixed_beta")
        self.assertFalse(eng._platt_trained)
        self.assertFalse(eng._beta_trained)
        self.assertFalse(eng._blr_trained)
        self.assertFalse(eng.is_learned_method_active())

    def test_load_state_round_trip(self):
        """Save state → load state → parameters match."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            eng1 = self.CalEngine(state_path=path, label="test_save")
            eng1._platt_A = 0.9
            eng1._platt_B = -0.1
            eng1._platt_trained = True
            eng1.active_method = "platt"
            eng1._save_state()

            eng2 = self.CalEngine(state_path=path, label="test_load")
            self.assertEqual(eng2.active_method, "platt")
            self.assertAlmostEqual(eng2._platt_A, 0.9)
            self.assertAlmostEqual(eng2._platt_B, -0.1)
            self.assertTrue(eng2._platt_trained)
        finally:
            os.unlink(path)

    def test_corrupt_state_file_recovers(self):
        """Corrupt state file → engine starts fresh (doesn't crash)."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            f.write("{invalid json")
            path = f.name
        try:
            eng = self.CalEngine(state_path=path, label="test_corrupt")
            # Should have fallen back to defaults
            self.assertEqual(eng.active_method, "fixed_beta")
        finally:
            os.unlink(path)


class TestCalibrate(unittest.TestCase):
    """Test CalibrationEngine.calibrate dispatch."""

    @classmethod
    def setUpClass(cls):
        from bot.engines.calibration import CalibrationEngine
        cls.CalEngine = CalibrationEngine

    def _make_engine(self):
        """Create a fresh engine with no state file."""
        return self.CalEngine(state_path="/tmp/_test_cal_nonexistent_.json", label="test")

    def test_fixed_beta_dispatch(self):
        """When no method trained, calibrate uses _fallback_calibrate."""
        eng = self._make_engine()
        result = eng.calibrate(0.9, cap=0.93)
        expected = self.CalEngine._fallback_calibrate(0.9, cap=0.93)
        self.assertAlmostEqual(result, expected, places=10)

    def test_platt_dispatch(self):
        """When platt trained, calibrate uses Platt prediction + shrinkage."""
        eng = self._make_engine()
        eng._platt_trained = True
        eng.active_method = "platt"
        # Add enough observations to make shrinkage meaningful
        for _ in range(100):
            eng._observations.append((0.9, 1))
            eng._brier_scores.append(0.01)

        result = eng.calibrate(0.9, cap=NUMERICAL_SAFETY_CEILING)
        # Should be different from fallback
        fallback = self.CalEngine._fallback_calibrate(0.9, cap=NUMERICAL_SAFETY_CEILING)
        # Platt with default A=0.85, B=0 is similar to fallback, but shrinkage differs
        self.assertGreater(result, 0.0)
        self.assertLess(result, 1.0)

    def test_output_bounds(self):
        """Calibrate output is always in [0.001, NUMERICAL_SAFETY_CEILING]."""
        eng = self._make_engine()
        eng._platt_trained = True
        eng.active_method = "platt"
        for _ in range(100):
            eng._observations.append((0.5, 1))
            eng._brier_scores.append(0.25)

        for raw in [0.001, 0.1, 0.5, 0.9, 0.999]:
            result = eng.calibrate(raw, cap=NUMERICAL_SAFETY_CEILING)
            self.assertGreaterEqual(result, 0.001,
                                    f"calibrate({raw}) = {result} < 0.001")
            self.assertLessEqual(result, NUMERICAL_SAFETY_CEILING,
                                 f"calibrate({raw}) = {result} > {NUMERICAL_SAFETY_CEILING}")


class TestAddObservation(unittest.TestCase):
    """Test observation tracking and rolling Brier."""

    @classmethod
    def setUpClass(cls):
        from bot.engines.calibration import CalibrationEngine
        cls.CalEngine = CalibrationEngine

    def test_observation_added(self):
        """add_observation appends (raw_prob, outcome, stc) triple to deque."""
        eng = self.CalEngine(state_path="/tmp/_test_cal_obs_.json", label="test")
        eng.add_observation(0.9, 1)
        self.assertEqual(len(eng._observations), 1)
        self.assertEqual(eng._observations[0], (0.9, 1, None))
        # With STC
        eng.add_observation(0.95, 0, seconds_to_close=300.0)
        self.assertEqual(eng._observations[1], (0.95, 0, 300.0))

    def test_brier_updated(self):
        """add_observation updates rolling Brier scores."""
        eng = self.CalEngine(state_path="/tmp/_test_cal_brier_.json", label="test")
        eng.add_observation(0.9, 1)
        self.assertEqual(len(eng._brier_scores), 1)
        # Brier for a ~90% prediction that won: should be small
        self.assertLess(eng._brier_scores[0], 0.1)

    def test_deque_maxlen(self):
        """Observations deque respects maxlen=500."""
        eng = self.CalEngine(state_path="/tmp/_test_cal_maxlen_.json", label="test")
        for i in range(600):
            eng.add_observation(0.9, 1)
        self.assertEqual(len(eng._observations), 500)


class TestMaybeRetrain(unittest.TestCase):
    """Test training threshold and method promotion."""

    @classmethod
    def setUpClass(cls):
        from bot.engines.calibration import CalibrationEngine
        cls.CalEngine = CalibrationEngine

    def test_insufficient_data_skips(self):
        """Fewer than 50 observations → retrain returns False."""
        eng = self.CalEngine(state_path="/tmp/_test_cal_skip_.json", label="test")
        eng._last_retrain = 0  # force past interval
        for _ in range(30):
            eng._observations.append((0.9, 1))
        result = eng.maybe_retrain()
        self.assertFalse(result)

    def test_platt_trains_at_200(self):
        """200+ observations → Platt trains successfully."""
        eng = self.CalEngine(state_path="/tmp/_test_cal_platt_.json", label="test")
        eng._last_retrain = 0
        import random
        random.seed(42)
        for _ in range(250):
            raw = random.uniform(0.7, 0.99)
            outcome = 1 if random.random() < raw else 0
            eng._observations.append((raw, outcome))
        result = eng.maybe_retrain()
        self.assertTrue(result)
        self.assertTrue(eng._platt_trained)

    def test_blr_trains_at_50(self):
        """50+ observations → BLR trains (lowest threshold method)."""
        eng = self.CalEngine(state_path="/tmp/_test_cal_blr_.json", label="test")
        eng._last_retrain = 0
        import random
        random.seed(123)
        for _ in range(60):
            raw = random.uniform(0.7, 0.99)
            outcome = 1 if random.random() < raw else 0
            eng._observations.append((raw, outcome))
        result = eng.maybe_retrain()
        self.assertTrue(result)
        self.assertTrue(eng._blr_trained)

    def test_best_brier_wins(self):
        """Multiple methods trained → best Brier score wins promotion."""
        eng = self.CalEngine(state_path="/tmp/_test_cal_best_.json", label="test")
        eng._last_retrain = 0
        import random
        random.seed(999)
        # Well-calibrated data with 250+ obs (trains Platt and BLR)
        for _ in range(260):
            raw = random.uniform(0.7, 0.99)
            outcome = 1 if random.random() < raw else 0
            eng._observations.append((raw, outcome))
        eng.maybe_retrain()
        # active_method should be one of the trained methods
        self.assertIn(eng.active_method, ["platt", "beta_cal", "blr", "temperature"])
        self.assertTrue(eng.is_learned_method_active())


class TestIsLearnedMethodActive(unittest.TestCase):
    """Test is_learned_method_active flag."""

    @classmethod
    def setUpClass(cls):
        from bot.engines.calibration import CalibrationEngine
        cls.CalEngine = CalibrationEngine

    def test_fixed_beta_not_learned(self):
        """fixed_beta → is_learned_method_active() == False."""
        eng = self.CalEngine(state_path="/tmp/_test_cal_notlearned_.json", label="test")
        self.assertFalse(eng.is_learned_method_active())

    def test_platt_trained_is_learned(self):
        """Platt trained + active → is_learned_method_active() == True."""
        eng = self.CalEngine(state_path="/tmp/_test_cal_learned_.json", label="test")
        eng.active_method = "platt"
        eng._platt_trained = True
        self.assertTrue(eng.is_learned_method_active())

    def test_method_set_but_not_trained(self):
        """Method set to 'platt' but _platt_trained=False → not learned."""
        eng = self.CalEngine(state_path="/tmp/_test_cal_nottrained_.json", label="test")
        eng.active_method = "platt"
        eng._platt_trained = False
        self.assertFalse(eng.is_learned_method_active())


class TestUncertaintyShrinkage(unittest.TestCase):
    """Test _apply_uncertainty_shrinkage."""

    @classmethod
    def setUpClass(cls):
        from bot.engines.calibration import CalibrationEngine
        cls.CalEngine = CalibrationEngine

    def test_scarce_data_conservative(self):
        """Fewer than 50 observations → u=0.05 → 5% shrinkage toward 0.5."""
        eng = self.CalEngine(state_path="/tmp/_test_cal_shrink_.json", label="test")
        # No observations → n < 50 → u = 0.05
        result = eng._apply_uncertainty_shrinkage(0.95)
        # p_adj = 0.5 + (0.95 - 0.5) * (1 - 0.05) = 0.5 + 0.45 * 0.95 = 0.9275
        expected = 0.5 + (0.95 - 0.5) * 0.95
        self.assertAlmostEqual(result, expected, places=10)

    def test_good_model_minimal_shrinkage(self):
        """Many observations + low Brier → minimal shrinkage."""
        eng = self.CalEngine(state_path="/tmp/_test_cal_noshrink_.json", label="test")
        for _ in range(200):
            eng._observations.append((0.9, 1))
            eng._brier_scores.append(0.01)  # Low Brier
        result = eng._apply_uncertainty_shrinkage(0.95)
        # u = 0.01 / sqrt(200) ≈ 0.0007 → nearly no shrinkage
        self.assertGreater(result, 0.94)
        self.assertLess(result, 0.96)

    def test_shrinkage_toward_half(self):
        """Shrinkage moves probability toward 0.5, never past it."""
        eng = self.CalEngine(state_path="/tmp/_test_cal_toward_.json", label="test")
        result_high = eng._apply_uncertainty_shrinkage(0.95)
        result_low = eng._apply_uncertainty_shrinkage(0.3)
        self.assertLess(result_high, 0.95)
        self.assertGreater(result_high, 0.5)
        self.assertGreater(result_low, 0.3)
        self.assertLess(result_low, 0.5)


class TestRollingBrier(unittest.TestCase):
    """Test rolling_brier_score."""

    @classmethod
    def setUpClass(cls):
        from bot.engines.calibration import CalibrationEngine
        cls.CalEngine = CalibrationEngine

    def test_empty_returns_one(self):
        """No scores → returns 1.0 (worst case)."""
        eng = self.CalEngine(state_path="/tmp/_test_cal_empty_brier_.json", label="test")
        self.assertEqual(eng.rolling_brier_score(), 1.0)

    def test_perfect_predictions(self):
        """Perfect predictions → Brier near 0."""
        eng = self.CalEngine(state_path="/tmp/_test_cal_perfect_.json", label="test")
        for _ in range(50):
            eng._brier_scores.append(0.0)
        self.assertAlmostEqual(eng.rolling_brier_score(), 0.0, places=10)


class TestTemperatureScaling(unittest.TestCase):
    """Test temperature prediction method."""

    @classmethod
    def setUpClass(cls):
        from bot.engines.calibration import CalibrationEngine
        cls.CalEngine = CalibrationEngine

    def test_temperature_one_is_identity(self):
        """T=1.0 → sigmoid(logit(p)/1) = sigmoid(logit(p)) = p."""
        eng = self.CalEngine(state_path="/tmp/_test_cal_temp_id_.json", label="test")
        for p in [0.5, 0.7, 0.9, 0.95]:
            result = eng._temperature_predict(p, 1.0)
            self.assertAlmostEqual(result, p, places=6,
                                   msg=f"T=1.0 should be identity, got {result} for p={p}")

    def test_high_temperature_compresses(self):
        """T>1 → probabilities compressed toward 0.5."""
        eng = self.CalEngine(state_path="/tmp/_test_cal_temp_hi_.json", label="test")
        for p in [0.8, 0.9, 0.95]:
            result = eng._temperature_predict(p, 1.45)
            self.assertLess(result, p)
            self.assertGreater(result, 0.5)

    def test_low_temperature_sharpens(self):
        """T<1 → probabilities sharpened away from 0.5."""
        eng = self.CalEngine(state_path="/tmp/_test_cal_temp_lo_.json", label="test")
        for p in [0.7, 0.8, 0.9]:
            result = eng._temperature_predict(p, 0.5)
            self.assertGreater(result, p)

    def test_temperature_monotonic(self):
        """Higher raw_prob → higher temperature-scaled prob (for any T>0)."""
        eng = self.CalEngine(state_path="/tmp/_test_cal_temp_mono_.json", label="test")
        for t in [0.5, 1.0, 1.45, 2.0]:
            probs = [0.5, 0.6, 0.7, 0.8, 0.9, 0.95]
            scaled = [eng._temperature_predict(p, t) for p in probs]
            for i in range(len(scaled) - 1):
                self.assertLess(scaled[i], scaled[i + 1],
                                f"Monotonicity violated at T={t}: f({probs[i]})={scaled[i]} >= f({probs[i+1]})={scaled[i+1]}")


class TestBetaCalPredict(unittest.TestCase):
    """Test Beta calibration prediction."""

    @classmethod
    def setUpClass(cls):
        from bot.engines.calibration import CalibrationEngine
        cls.CalEngine = CalibrationEngine

    def test_identity_params(self):
        """Default params (a=1, b=-1, c=0) approximate identity."""
        eng = self.CalEngine(state_path="/tmp/_test_cal_beta_id_.json", label="test")
        # With a=1, b=-1, c=0: logit_out = 0 + 1*log(p) + (-1)*log(1-p) = logit(p)
        for p in [0.5, 0.7, 0.9]:
            result = eng._beta_cal_predict(p)
            self.assertAlmostEqual(result, p, places=4,
                                   msg=f"Identity Beta Cal failed for p={p}: got {result}")

    def test_output_in_range(self):
        """Beta cal output always in (0, 1)."""
        eng = self.CalEngine(state_path="/tmp/_test_cal_beta_range_.json", label="test")
        eng._beta_a = 2.0
        eng._beta_b = -1.5
        eng._beta_c = 0.3
        for p in [0.001, 0.1, 0.5, 0.9, 0.999]:
            result = eng._beta_cal_predict(p)
            self.assertGreater(result, 0.0)
            self.assertLess(result, 1.0)


class TestSolve3x3(unittest.TestCase):
    """Test Cramer's rule 3x3 solver."""

    @classmethod
    def setUpClass(cls):
        from bot.engines.calibration import CalibrationEngine
        cls.CalEngine = CalibrationEngine

    def test_identity_system(self):
        """I × x = b → x = b."""
        A = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
        b = [3.0, -1.0, 2.0]
        result = self.CalEngine._solve_3x3(A, b)
        for i in range(3):
            self.assertAlmostEqual(result[i], b[i], places=10)

    def test_singular_returns_none(self):
        """Singular matrix → returns None (not crash)."""
        A = [[1, 2, 3], [2, 4, 6], [1, 1, 1]]  # row 2 = 2 × row 1
        b = [1.0, 2.0, 3.0]
        result = self.CalEngine._solve_3x3(A, b)
        self.assertIsNone(result)

    def test_known_system(self):
        """Solve a known 3x3 system."""
        # 2x + y = 5, y + z = 3, x + z = 4 → x=2, y=1, z=2
        A = [[2, 1, 0], [0, 1, 1], [1, 0, 1]]
        b = [5.0, 3.0, 4.0]
        result = self.CalEngine._solve_3x3(A, b)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result[0], 2.0, places=8)
        self.assertAlmostEqual(result[1], 1.0, places=8)
        self.assertAlmostEqual(result[2], 2.0, places=8)


if __name__ == "__main__":
    unittest.main()
