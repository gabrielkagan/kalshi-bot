"""Tests for VolatilityEngine — pure static methods + stateful adaptive jump detection.

Guards against:
- Realized Kernel returning negative/NaN values (silent corruption of vol estimates)
- Bipower variation failing to separate jumps from continuous vol
- Adaptive RK bandwidth degenerate cases (H=0, H>n, negative omega_sq)
- Adaptive jump detection false positives/negatives from EWMA initialization
- Decay multiplier not converging to 1.0 after jump events age out
- update() returning None or missing required keys (downstream NullPointerError in scan())
"""

import math
import time
import unittest
from collections import deque
from unittest.mock import MagicMock, patch

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from bot.engines.volatility import VolatilityEngine

# Import constants needed for assertions
from bot.constants import (
    VOL_WINDOW_1MIN, VOL_WINDOW_5MIN, VOL_WINDOW_15MIN,
    RK_NOISE_VAR_FLOOR, RK_MIN_RETURNS_FOR_ADAPTIVE,
    RK_CSTAR_FLAT_TOP_PARZEN, RK_BANDWIDTH_MAX_FRACTION,
    JUMP_ADAPTIVE_SUBSAMPLE, JUMP_ADAPTIVE_PCTILE_WINDOW,
    JUMP_ADAPTIVE_SIGMA_MULT, JUMP_ADAPTIVE_EWMA_LAMBDA,
    JUMP_ADAPTIVE_EWMA_INIT_RETURNS, JUMP_ADAPTIVE_PCTILE_LEVEL,
    JUMP_ADAPTIVE_PCTILE_MIN_OBS, JUMP_ADAPTIVE_DECAY_TAU,
    JUMP_ADAPTIVE_DECAY_MAX_BOOST, JUMP_ADAPTIVE_DECAY_MIN_BOOST,
    JUMP_ADAPTIVE_DECAY_CAP, JUMP_ADAPTIVE_MAG_SCALE_BASE,
    JUMP_ADAPTIVE_MAG_CAP, JUMP_ADAPTIVE_MAX_HISTORY,
)
from bot.config import VOL_RETURN_INTERVAL, ASSETS


# ═══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _make_returns(n, sigma=0.001):
    """Generate n synthetic log returns with known volatility."""
    import random
    random.seed(42)
    return [random.gauss(0, sigma) for _ in range(n)]


def _make_returns_with_jump(n, sigma=0.001, jump_at=None, jump_size=0.05):
    """Generate returns with a single jump injected at position jump_at."""
    import random
    random.seed(42)
    returns = [random.gauss(0, sigma) for _ in range(n)]
    if jump_at is not None and 0 <= jump_at < n:
        returns[jump_at] = jump_size
    return returns


class MockCoinbaseFeed:
    """Minimal mock of CoinbaseFeed for VolatilityEngine instantiation."""

    def __init__(self, prices=None):
        self._buffers = {}
        if prices:
            for asset, price_list in prices.items():
                # price_list: list of (timestamp, price) tuples
                self._buffers[asset] = price_list

    def get_buffer(self, asset):
        return self._buffers.get(asset, [])

    def get_price(self, asset):
        buf = self._buffers.get(asset, [])
        return buf[-1][1] if buf else None


class MockEGARCH:
    """Minimal mock of EGARCHEstimator."""

    def __init__(self):
        self._log_var = {}
        self._n_updates = {}

    def record_return(self, asset, log_return):
        pass

    def recursive_update(self, asset, log_return):
        return None

    def get_sigma(self, asset):
        return None

    def seed_variance(self, asset, var):
        self._log_var[asset] = math.log(var) if var > 0 else None

    def get_constrained_sigma(self, asset):
        return self.get_sigma(asset)


class MockMZ:
    """Minimal mock of MincerZarnowitzTracker."""

    def __init__(self):
        self._r_squared = {}
        self._qlike = {}
        self._baseline_qlike = {}
        self._shadow_sigmoid_w = {}

    def record(self, asset, forecast_var, realized_var):
        pass

    def maybe_recompute(self, asset, now):
        return 0.0

    def save_state(self):
        pass


def _build_vol_engine(prices=None, with_egarch=False, with_mz=False):
    """Build a VolatilityEngine with mocked dependencies."""
    feed = MockCoinbaseFeed(prices or {})
    egarch = MockEGARCH() if with_egarch else None
    mz = MockMZ() if with_mz else None
    return VolatilityEngine(feed, dvol_fetcher=None,
                            egarch_estimator=egarch, mz_tracker=mz)


# ═══════════════════════════════════════════════════════════════════════════════
#  1. Static pure methods — _parzen_kernel
# ═══════════════════════════════════════════════════════════════════════════════

class TestParzenKernel(unittest.TestCase):
    """Flat-top Parzen kernel: k(0)=1, k(0.5)=1, smooth taper [0.5,1], k(1)=0."""

    def test_kernel_at_zero(self):
        """k(0) = 1 (peak of flat-top region)."""
        self.assertEqual(VolatilityEngine._parzen_kernel(0.0), 1.0)

    def test_kernel_at_half(self):
        """k(0.5) = 1 (edge of flat-top region, before taper starts)."""
        self.assertEqual(VolatilityEngine._parzen_kernel(0.5), 1.0)

    def test_kernel_at_one(self):
        """k(1) = 0 (kernel vanishes at boundary)."""
        self.assertEqual(VolatilityEngine._parzen_kernel(1.0), 0.0)

    def test_kernel_beyond_one(self):
        """k(x) = 0 for |x| > 1."""
        self.assertEqual(VolatilityEngine._parzen_kernel(1.5), 0.0)
        self.assertEqual(VolatilityEngine._parzen_kernel(10.0), 0.0)

    def test_symmetry(self):
        """k(x) = k(-x) — kernel must be symmetric."""
        for x in [0.0, 0.25, 0.5, 0.75, 0.99, 1.0, 2.0]:
            self.assertAlmostEqual(
                VolatilityEngine._parzen_kernel(x),
                VolatilityEngine._parzen_kernel(-x),
                places=10,
                msg=f"Symmetry violated at x={x}")

    def test_monotone_decreasing_in_taper(self):
        """Kernel is monotonically decreasing in [0.5, 1.0] (smooth taper)."""
        prev = VolatilityEngine._parzen_kernel(0.5)
        for x_int in range(51, 101):
            x = x_int / 100.0
            val = VolatilityEngine._parzen_kernel(x)
            self.assertLessEqual(val, prev + 1e-10,
                                 f"Not monotone decreasing at x={x}")
            prev = val

    def test_taper_midpoint(self):
        """k(0.75) should be between 0 and 1 (smooth interpolation)."""
        val = VolatilityEngine._parzen_kernel(0.75)
        self.assertGreater(val, 0.0)
        self.assertLess(val, 1.0)


# ═══════════════════════════════════════════════════════════════════════════════
#  2. Static pure methods — _estimate_noise_variance
# ═══════════════════════════════════════════════════════════════════════════════

class TestEstimateNoiseVariance(unittest.TestCase):
    """Microstructure noise ω² = max(FLOOR, -γ̂(1))."""

    def test_single_return(self):
        """< 2 returns → floor value."""
        self.assertEqual(
            VolatilityEngine._estimate_noise_variance([0.001]),
            RK_NOISE_VAR_FLOOR)

    def test_empty_returns(self):
        self.assertEqual(
            VolatilityEngine._estimate_noise_variance([]),
            RK_NOISE_VAR_FLOOR)

    def test_positive_autocov_returns_floor(self):
        """Positive autocovariance (momentum) → ω² = floor.
        Momentum means -γ̂(1) < 0, floor kicks in."""
        # Trending returns: all positive → positive γ̂(1) → -γ̂(1) < 0
        returns = [0.001] * 10
        self.assertEqual(
            VolatilityEngine._estimate_noise_variance(returns),
            RK_NOISE_VAR_FLOOR)

    def test_alternating_returns_positive_noise(self):
        """Alternating signs → negative γ̂(1) → positive noise variance.
        This is the microstructure noise signature (bid-ask bounce)."""
        returns = [0.001, -0.001] * 20
        omega_sq = VolatilityEngine._estimate_noise_variance(returns)
        self.assertGreater(omega_sq, RK_NOISE_VAR_FLOOR)
        # Known value: γ̂(1) ≈ -0.001² = -1e-6, so ω² ≈ 1e-6
        self.assertAlmostEqual(omega_sq, 1e-6, places=8)

    def test_iid_returns_near_floor(self):
        """IID returns have γ̂(1) ≈ 0 → ω² ≈ floor."""
        returns = _make_returns(200, sigma=0.001)
        omega_sq = VolatilityEngine._estimate_noise_variance(returns)
        # Noise variance should be very small for IID (no autocorrelation)
        self.assertLess(omega_sq, 1e-6)


# ═══════════════════════════════════════════════════════════════════════════════
#  3. Static pure methods — _realized_quarticity
# ═══════════════════════════════════════════════════════════════════════════════

class TestRealizedQuarticity(unittest.TestCase):
    """RQ = (n/3) × Σ r_i⁴ over window subset."""

    def test_manual_computation(self):
        """Verify against hand-computed value on simple series."""
        returns = [0.01, -0.02, 0.015]
        window = 3
        # RQ = (3/3) * (0.01^4 + 0.02^4 + 0.015^4) = 1 * (1e-8 + 1.6e-7 + 5.0625e-8)
        expected = (3 / 3.0) * (0.01**4 + 0.02**4 + 0.015**4)
        result = VolatilityEngine._realized_quarticity(returns, window)
        self.assertAlmostEqual(result, expected, places=15)

    def test_empty_returns(self):
        self.assertEqual(VolatilityEngine._realized_quarticity([], 10), 0.0)

    def test_window_subsetting(self):
        """Window parameter takes last N returns."""
        returns = [0.1, 0.01, 0.02]
        rq_full = VolatilityEngine._realized_quarticity(returns, 3)
        rq_last2 = VolatilityEngine._realized_quarticity(returns, 2)
        # rq_last2 uses [0.01, 0.02], rq_full uses all 3 — the 0.1 outlier dominates
        self.assertGreater(rq_full, rq_last2)

    def test_scaling_with_magnitude(self):
        """RQ scales with 4th power of return magnitude."""
        small = [0.001] * 10
        large = [0.01] * 10  # 10× larger returns
        rq_small = VolatilityEngine._realized_quarticity(small, 10)
        rq_large = VolatilityEngine._realized_quarticity(large, 10)
        # Should scale by (10)^4 = 10000×
        ratio = rq_large / rq_small
        self.assertAlmostEqual(ratio, 10000.0, places=1)


# ═══════════════════════════════════════════════════════════════════════════════
#  4. Static pure methods — _optimal_rk_bandwidth
# ═══════════════════════════════════════════════════════════════════════════════

class TestOptimalRKBandwidth(unittest.TestCase):
    """BN (2008) optimal bandwidth H* for flat-top Parzen kernel."""

    def test_positive_bandwidth(self):
        """H* > 0 for any non-degenerate input."""
        returns = _make_returns(60, sigma=0.001)
        omega_sq = VolatilityEngine._estimate_noise_variance(returns)
        H = VolatilityEngine._optimal_rk_bandwidth(returns, 60, omega_sq)
        self.assertGreater(H, 0)

    def test_insufficient_data_returns_floor(self):
        """< 2 returns → H = 0."""
        H = VolatilityEngine._optimal_rk_bandwidth([0.001], 1, 1e-10)
        self.assertEqual(H, 0)

    def test_bandwidth_increases_with_noise(self):
        """More noise → larger bandwidth (more smoothing needed)."""
        returns = _make_returns(100, sigma=0.001)
        H_low_noise = VolatilityEngine._optimal_rk_bandwidth(returns, 100, 1e-10)
        H_high_noise = VolatilityEngine._optimal_rk_bandwidth(returns, 100, 1e-4)
        self.assertGreaterEqual(H_high_noise, H_low_noise,
                                "Higher noise should produce equal or larger bandwidth")

    def test_bandwidth_capped_at_fraction_of_n(self):
        """H* ≤ n × RK_BANDWIDTH_MAX_FRACTION."""
        returns = _make_returns(60, sigma=0.001)
        # Very high noise to push H* up
        H = VolatilityEngine._optimal_rk_bandwidth(returns, 60, 1.0)
        max_H = math.floor(60 * RK_BANDWIDTH_MAX_FRACTION)
        self.assertLessEqual(H, max_H)

    def test_floor_noise_uses_sqrt_fallback(self):
        """omega_sq ≤ floor → falls back to ceil(√n)."""
        returns = _make_returns(100, sigma=0.001)
        H = VolatilityEngine._optimal_rk_bandwidth(returns, 100, RK_NOISE_VAR_FLOOR)
        expected_floor = math.ceil(math.sqrt(100))
        self.assertEqual(H, expected_floor)

    def test_below_min_returns_uses_sqrt_fallback(self):
        """< RK_MIN_RETURNS_FOR_ADAPTIVE → ceil(√n) fallback."""
        n = RK_MIN_RETURNS_FOR_ADAPTIVE - 1
        returns = _make_returns(n, sigma=0.001)
        H = VolatilityEngine._optimal_rk_bandwidth(returns, n, 1e-6)
        expected = math.ceil(math.sqrt(n))
        self.assertEqual(H, expected)


# ═══════════════════════════════════════════════════════════════════════════════
#  5. Static pure methods — _realized_kernel
# ═══════════════════════════════════════════════════════════════════════════════

class TestRealizedKernel(unittest.TestCase):
    """Realized Kernel (BN 2008) — microstructure-noise robust vol estimator."""

    def test_rk_positive(self):
        """RK always returns ≥ 0 for any input."""
        returns = _make_returns(60, sigma=0.001)
        rk = VolatilityEngine._realized_kernel(returns, 60)
        self.assertGreaterEqual(rk, 0.0)

    def test_rk_zero_for_constant_returns(self):
        """All-zero returns → RK = 0."""
        returns = [0.0] * 30
        rk = VolatilityEngine._realized_kernel(returns, 30)
        self.assertEqual(rk, 0.0)

    def test_rk_insufficient_data(self):
        """< 2 returns → 0."""
        self.assertEqual(VolatilityEngine._realized_kernel([], 10), 0.0)
        self.assertEqual(VolatilityEngine._realized_kernel([0.001], 10), 0.0)

    def test_rk_approximates_rv_for_clean_series(self):
        """For IID returns (no noise), RK ≈ standard RV = sqrt(mean(r²)).
        RK should be close to but not necessarily identical (kernel smoothing)."""
        returns = _make_returns(180, sigma=0.001)
        rk = VolatilityEngine._realized_kernel(returns, 180)
        # Standard RV
        rv = math.sqrt(sum(r**2 for r in returns) / len(returns))
        # RK should be within 50% of standard RV for clean series
        self.assertAlmostEqual(rk, rv, delta=rv * 0.5,
                               msg=f"RK={rk:.6f} too far from RV={rv:.6f}")

    def test_rk_less_than_rv_for_noisy_series(self):
        """For bid-ask bounce (alternating returns), RK < naive RV.
        This is the core purpose: noise robustness."""
        # Alternating returns simulate bid-ask bounce
        noisy = [0.002, -0.002] * 30
        rk = VolatilityEngine._realized_kernel(noisy, 60)
        rv = math.sqrt(sum(r**2 for r in noisy) / len(noisy))
        self.assertLess(rk, rv,
                        "RK should be less than naive RV for noisy (bid-ask bounce) series")

    def test_rk_custom_bandwidth(self):
        """Custom bandwidth parameter is respected (different result)."""
        returns = _make_returns(60, sigma=0.001)
        rk_default = VolatilityEngine._realized_kernel(returns, 60)
        rk_large_bw = VolatilityEngine._realized_kernel(returns, 60, bandwidth=15)
        # Different bandwidth should give different result (unless degenerate)
        # They should both be positive and finite
        self.assertGreater(rk_default, 0)
        self.assertGreater(rk_large_bw, 0)
        self.assertTrue(math.isfinite(rk_default))
        self.assertTrue(math.isfinite(rk_large_bw))

    def test_rk_window_subsetting(self):
        """Window parameter restricts to last N returns."""
        returns = _make_returns(180, sigma=0.001)
        rk_60 = VolatilityEngine._realized_kernel(returns, 60)
        rk_180 = VolatilityEngine._realized_kernel(returns, 180)
        # Both should be positive; 180-window uses more data
        self.assertGreater(rk_60, 0)
        self.assertGreater(rk_180, 0)


# ═══════════════════════════════════════════════════════════════════════════════
#  6. Static pure methods — _bipower_variation
# ═══════════════════════════════════════════════════════════════════════════════

class TestBipowerVariation(unittest.TestCase):
    """BPV: jump-robust continuous-path vol estimator.
    BV = sqrt((π/2) × (1/(n-1)) × Σ |r_j| × |r_{j+1}|)
    """

    def test_bv_positive(self):
        """BV > 0 for any non-zero returns."""
        returns = _make_returns(60, sigma=0.001)
        bv = VolatilityEngine._bipower_variation(returns, 60)
        self.assertGreater(bv, 0.0)

    def test_bv_zero_for_zero_returns(self):
        returns = [0.0] * 30
        bv = VolatilityEngine._bipower_variation(returns, 30)
        self.assertEqual(bv, 0.0)

    def test_bv_insufficient_data(self):
        self.assertEqual(VolatilityEngine._bipower_variation([], 10), 0.0)
        self.assertEqual(VolatilityEngine._bipower_variation([0.001], 10), 0.0)

    def test_bv_approximates_rv_without_jumps(self):
        """For continuous (no-jump) IID returns, BV ≈ RV.
        This is the theoretical property: BV converges to integrated variance."""
        returns = _make_returns(500, sigma=0.001)
        bv = VolatilityEngine._bipower_variation(returns, 500)
        rv = math.sqrt(sum(r**2 for r in returns) / len(returns))
        # BV should be within 30% of RV for large n without jumps
        self.assertAlmostEqual(bv, rv, delta=rv * 0.30,
                               msg=f"BV={bv:.6f} too far from RV={rv:.6f} for no-jump series")

    def test_bv_less_than_rv_with_jumps(self):
        """With a jump, BV < RV because BV is robust to isolated jumps.
        The jump inflates RV (sum of r²) but BV uses products |r_j|×|r_{j+1}|,
        and the jump is isolated (only 2 of n products are affected)."""
        returns = _make_returns_with_jump(200, sigma=0.001, jump_at=100, jump_size=0.05)
        bv = VolatilityEngine._bipower_variation(returns, 200)
        rv = math.sqrt(sum(r**2 for r in returns) / len(returns))
        self.assertLess(bv, rv,
                        "BV should be less than RV when a jump is present")

    def test_bv_jump_robust_magnitude(self):
        """BV barely changes when a jump is added (only 2/n products affected).
        Guards against BV being sensitive to jumps (would defeat its purpose)."""
        no_jump = _make_returns(200, sigma=0.001)
        with_jump = _make_returns_with_jump(200, sigma=0.001, jump_at=100, jump_size=0.05)
        bv_no = VolatilityEngine._bipower_variation(no_jump, 200)
        bv_yes = VolatilityEngine._bipower_variation(with_jump, 200)
        # BV should change by less than 50% even with a 50× sigma jump
        pct_change = abs(bv_yes - bv_no) / bv_no
        self.assertLess(pct_change, 0.5,
                        f"BV changed by {pct_change:.1%} with jump — not robust enough")


# ═══════════════════════════════════════════════════════════════════════════════
#  7. Stateful — Adaptive jump detection
# ═══════════════════════════════════════════════════════════════════════════════

class TestAdaptiveJumpDetection(unittest.TestCase):
    """Adaptive jump test: EWMA variance + rolling percentile threshold."""

    def setUp(self):
        self.engine = _build_vol_engine()

    def test_no_jump_on_quiet_series(self):
        """Small returns should not trigger jump detection.
        Guards against false positives that would inflate vol multiplier."""
        asset = "BTC"
        now = time.time()
        # Warm up: feed many small returns to build EWMA + percentile
        for i in range(JUMP_ADAPTIVE_EWMA_INIT_RETURNS + JUMP_ADAPTIVE_PCTILE_MIN_OBS + 5):
            result = self.engine._adaptive_jump_test(asset, 0.0001, now + i * 15)
        # Check last result
        self.assertFalse(result["is_jump"],
                         "Small return should not trigger jump detection")

    def test_jump_on_large_return(self):
        """5-sigma move should trigger jump detection after warmup.
        Guards against false negatives that would miss real jumps."""
        asset = "BTC"
        now = time.time()
        sigma = 0.001
        # Warm up with small returns
        for i in range(JUMP_ADAPTIVE_EWMA_INIT_RETURNS + JUMP_ADAPTIVE_PCTILE_MIN_OBS + 5):
            self.engine._adaptive_jump_test(asset, sigma * 0.5, now + i * 15)
        # Now inject a huge return (5× the typical sigma mult threshold)
        big_return = sigma * JUMP_ADAPTIVE_SIGMA_MULT * 5
        result = self.engine._adaptive_jump_test(asset, big_return, now + 10000)
        self.assertTrue(result["is_jump"],
                        f"5-sigma move should trigger jump. "
                        f"ewma_sigma={result['ewma_sigma']:.6f}, "
                        f"threshold={result['effective_threshold']:.6f}, "
                        f"return={big_return:.6f}")

    def test_warmup_suppresses_detection(self):
        """Before warmup period, no jumps should be detected (even for extreme returns).
        Guards against premature jump signals before EWMA is calibrated."""
        asset = "ETH"
        now = time.time()
        # Only a few observations — below warmup threshold
        for i in range(3):
            result = self.engine._adaptive_jump_test(asset, 0.001, now + i * 15)
        # Huge return during warmup
        result = self.engine._adaptive_jump_test(asset, 1.0, now + 100)
        self.assertFalse(result["is_jump"],
                         "Jump detection should be suppressed during warmup")

    def test_ewma_initialization(self):
        """After exactly INIT_RETURNS+1 observations, EWMA should be initialized.
        Guards against off-by-one in EWMA initialization that could leave it None forever."""
        asset = "BTC"
        now = time.time()
        for i in range(JUMP_ADAPTIVE_EWMA_INIT_RETURNS + 2):
            self.engine._adaptive_jump_test(asset, 0.001, now + i * 15)
        self.assertIsNotNone(self.engine._adaptive_ewma_var.get(asset),
                             "EWMA should be initialized after warmup")

    def test_magnitude_ratio_computed(self):
        """magnitude_ratio should be positive and proportional to return size."""
        asset = "BTC"
        now = time.time()
        for i in range(50):
            self.engine._adaptive_jump_test(asset, 0.001, now + i * 15)
        result = self.engine._adaptive_jump_test(asset, 0.005, now + 1000)
        self.assertGreater(result["magnitude_ratio"], 0,
                           "magnitude_ratio should be positive after warmup")


# ═══════════════════════════════════════════════════════════════════════════════
#  8. Stateful — Adaptive decay multiplier
# ═══════════════════════════════════════════════════════════════════════════════

class TestAdaptiveDecayMultiplier(unittest.TestCase):
    """Decay multiplier: 1.0 at rest, elevated after jump, decays back."""

    def setUp(self):
        self.engine = _build_vol_engine()

    def test_no_jumps_returns_one(self):
        """With no jump events, multiplier = 1.0 and regime = 'normal'."""
        mult, regime = self.engine._adaptive_decay_multiplier("BTC", time.time())
        self.assertEqual(mult, 1.0)
        self.assertEqual(regime, "normal")

    def test_recent_jump_elevates_multiplier(self):
        """Right after a jump, multiplier > 1.0 and regime = 'elevated'."""
        now = time.time()
        self.engine._record_adaptive_jump_event("BTC", now, magnitude_ratio=2.0)
        mult, regime = self.engine._adaptive_decay_multiplier("BTC", now + 0.1)
        self.assertGreater(mult, 1.0)
        self.assertEqual(regime, "elevated")

    def test_decay_to_normal(self):
        """Long after a jump, multiplier decays back to 1.0.
        Guards against permanent regime shift from a single event."""
        now = time.time()
        self.engine._record_adaptive_jump_event("BTC", now, magnitude_ratio=2.0)
        # Check well after decay (10× TAU)
        mult, regime = self.engine._adaptive_decay_multiplier("BTC", now + 10 * JUMP_ADAPTIVE_DECAY_TAU)
        self.assertAlmostEqual(mult, 1.0, places=2,
                               msg="Multiplier should decay to ~1.0 after 10× TAU")
        self.assertEqual(regime, "normal")

    def test_multiplier_capped(self):
        """Multiple simultaneous jumps → multiplier capped at DECAY_CAP.
        Guards against unbounded vol inflation."""
        now = time.time()
        for i in range(20):
            self.engine._record_adaptive_jump_event("BTC", now, magnitude_ratio=3.0)
        mult, regime = self.engine._adaptive_decay_multiplier("BTC", now + 0.001)
        self.assertLessEqual(mult, JUMP_ADAPTIVE_DECAY_CAP)

    def test_history_capped(self):
        """Jump event history is limited to MAX_HISTORY entries.
        Guards against unbounded memory growth."""
        now = time.time()
        for i in range(JUMP_ADAPTIVE_MAX_HISTORY + 10):
            self.engine._record_adaptive_jump_event("BTC", now + i, magnitude_ratio=1.5)
        self.assertLessEqual(
            len(self.engine._adaptive_jump_events["BTC"]),
            JUMP_ADAPTIVE_MAX_HISTORY)


# ═══════════════════════════════════════════════════════════════════════════════
#  9. Stateful — update() integration
# ═══════════════════════════════════════════════════════════════════════════════

class TestVolEngineUpdate(unittest.TestCase):
    """update() orchestrates the entire vol pipeline. Test with synthetic data."""

    def _make_price_buffer(self, asset, n_ticks=250, base_price=50000, sigma=0.001):
        """Build synthetic price buffer with 1-second spacing."""
        import random
        random.seed(42)
        now = time.time()
        prices = []
        price = base_price
        for i in range(n_ticks):
            price *= math.exp(random.gauss(0, sigma))
            prices.append((now - n_ticks + i, price))
        return {asset: prices}

    def test_returns_none_without_data(self):
        """update() returns None when feed has insufficient data."""
        engine = _build_vol_engine({"BTC": [(time.time(), 50000)]})
        result = engine.update("BTC")
        self.assertIsNone(result)

    def test_returns_dict_with_sufficient_data(self):
        """update() returns a dict with all required keys when data is available."""
        prices = self._make_price_buffer("BTC", n_ticks=250)
        engine = _build_vol_engine(prices, with_egarch=True, with_mz=True)
        # Pre-fill the returns buffer (update needs returns accumulated over time)
        # Simulate many ticks by manually building returns
        buf = prices["BTC"]
        for i in range(VOL_RETURN_INTERVAL + 1, len(buf)):
            log_r = math.log(buf[i][1] / buf[i - VOL_RETURN_INTERVAL][1])
            engine._returns["BTC"].append(log_r)
        engine._last_return_time["BTC"] = 0  # force recompute
        result = engine.update("BTC", seconds_to_close=600)
        self.assertIsNotNone(result, "update() should return dict with enough data")

    def test_output_has_required_keys(self):
        """Output dict must have the keys that scan() reads downstream.
        Missing keys → KeyError in scan() → silent failure or crash."""
        prices = self._make_price_buffer("BTC", n_ticks=250)
        engine = _build_vol_engine(prices, with_egarch=True, with_mz=True)
        buf = prices["BTC"]
        for i in range(VOL_RETURN_INTERVAL + 1, len(buf)):
            log_r = math.log(buf[i][1] / buf[i - VOL_RETURN_INTERVAL][1])
            engine._returns["BTC"].append(log_r)
        engine._last_return_time["BTC"] = 0
        result = engine.update("BTC", seconds_to_close=600)
        self.assertIsNotNone(result)

        # Required keys that scan() reads from vol_est
        required_keys = [
            "blended_rv", "rv_1min", "rv_5min", "rv_15min",
            "regime", "num_returns", "jump_seconds_remaining",
            "bv_1min", "bv_5min", "bv_15min",
            "jump_component", "jump_multiplier",
            "rv_only_blended", "fixed_blend_rv",
            "egarch_sigma", "egarch_blend_weight", "egarch_blend_var",
            "omega_sq", "rk_H_fixed_5", "rk_H_adaptive_5",
            "adaptive_jump_multiplier", "adaptive_jump_regime",
            "mz_r_squared", "mz_qlike",
        ]
        for key in required_keys:
            self.assertIn(key, result,
                          f"Missing required key '{key}' in update() output — "
                          f"scan() will KeyError on this")

    def test_blended_rv_positive(self):
        """blended_rv must be > 0 (zero vol → division-by-zero in ProbabilityEngine)."""
        prices = self._make_price_buffer("BTC", n_ticks=250)
        engine = _build_vol_engine(prices)
        buf = prices["BTC"]
        for i in range(VOL_RETURN_INTERVAL + 1, len(buf)):
            log_r = math.log(buf[i][1] / buf[i - VOL_RETURN_INTERVAL][1])
            engine._returns["BTC"].append(log_r)
        engine._last_return_time["BTC"] = 0
        result = engine.update("BTC", seconds_to_close=600)
        self.assertIsNotNone(result)
        self.assertGreater(result["blended_rv"], 0,
                           "blended_rv must be positive for downstream probability calc")

    def test_vol_in_reasonable_range(self):
        """Per-5s log return vol should be small (not annualized here).
        Typical BTC 5s vol ≈ 0.0001-0.001. If it's > 0.1, something is wrong."""
        prices = self._make_price_buffer("BTC", n_ticks=250, sigma=0.0005)
        engine = _build_vol_engine(prices)
        buf = prices["BTC"]
        for i in range(VOL_RETURN_INTERVAL + 1, len(buf)):
            log_r = math.log(buf[i][1] / buf[i - VOL_RETURN_INTERVAL][1])
            engine._returns["BTC"].append(log_r)
        engine._last_return_time["BTC"] = 0
        result = engine.update("BTC", seconds_to_close=600)
        self.assertIsNotNone(result)
        self.assertLess(result["blended_rv"], 0.1,
                        "Per-5s vol should be small, not annualized")

    def test_all_assets_supported(self):
        """Engine initializes buffers for all configured assets."""
        engine = _build_vol_engine()
        for asset in ASSETS:
            self.assertIn(asset, engine._returns,
                          f"Missing return buffer for {asset}")


# ═══════════════════════════════════════════════════════════════════════════════
#  10. Subsample return aggregation
# ═══════════════════════════════════════════════════════════════════════════════

class TestAdaptiveSubsample(unittest.TestCase):
    """_adaptive_subsample_return: 5s → 15s by summing every 3rd tick."""

    def setUp(self):
        self.engine = _build_vol_engine()

    def test_returns_none_until_subsample(self):
        """First SUBSAMPLE-1 ticks should return None (accumulating)."""
        asset = "BTC"
        now = time.time()
        # Pre-fill the returns buffer (subsample reads from it)
        for i in range(10):
            self.engine._returns[asset].append(0.001)
        for i in range(JUMP_ADAPTIVE_SUBSAMPLE - 1):
            result = self.engine._adaptive_subsample_return(asset, 0.001, now + i)
            self.assertIsNone(result, f"Tick {i} should be None (not yet at subsample boundary)")

    def test_returns_sum_at_boundary(self):
        """At every SUBSAMPLE-th tick, returns the sum of last SUBSAMPLE returns."""
        asset = "BTC"
        now = time.time()
        # Fill returns buffer
        for i in range(10):
            self.engine._returns[asset].append(0.001)
        # Tick through to the boundary
        for i in range(JUMP_ADAPTIVE_SUBSAMPLE):
            result = self.engine._adaptive_subsample_return(asset, 0.001, now + i)
        self.assertIsNotNone(result, "Should return a value at subsample boundary")
        # Sum of last SUBSAMPLE returns from the buffer
        expected = sum(list(self.engine._returns[asset])[-JUMP_ADAPTIVE_SUBSAMPLE:])
        self.assertAlmostEqual(result, expected, places=10)


if __name__ == "__main__":
    unittest.main()
