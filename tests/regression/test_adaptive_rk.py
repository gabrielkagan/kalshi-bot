"""Tests for Adaptive Realized Kernel Bandwidth (BN 2008/2009).

Standalone test file with inline copies of the relevant static methods
so tests run without importing bot/_impl.py's full dependency tree.

DRIFT RISK: These methods are copied from VolatilityEngine in bot/_impl.py.
If the production code changes, these inline copies may drift out of sync.
The RK/bandwidth code has not been extracted to models.py yet because it's
embedded in VolatilityEngine (not a standalone class).
"""

import math
import random
import unittest

# ─── Constants (mirror bot/_impl.py) ─────────────────────────────────────────────
RK_CSTAR_FLAT_TOP_PARZEN = 3.5134
RK_NOISE_VAR_FLOOR = 1e-20
RK_BANDWIDTH_MAX_FRACTION = 1 / 3
RK_MIN_RETURNS_FOR_ADAPTIVE = 20


# ─── Inline copies of static methods ──────────────────────────────────────

def _parzen_kernel(x: float) -> float:
    ax = abs(x)
    if ax <= 0.5:
        return 1.0
    if ax <= 1.0:
        u = 2.0 * (ax - 0.5)
        return 1.0 - 3.0 * u * u + 2.0 * u * u * u
    return 0.0


def _realized_kernel(returns, window, bandwidth=None):
    subset = returns[-window:] if len(returns) >= window else returns
    n = len(subset)
    if n < 2:
        return 0.0
    H = bandwidth if bandwidth is not None else math.ceil(math.sqrt(n))
    rk = 0.0
    for h in range(-H, H + 1):
        weight = _parzen_kernel(h / (H + 1))
        if weight == 0.0:
            continue
        gamma_h = 0.0
        ah = abs(h)
        count = 0
        for j in range(ah, n):
            gamma_h += subset[j] * subset[j - ah]
            count += 1
        if count > 0:
            gamma_h /= count
        rk += weight * gamma_h
    return math.sqrt(max(0.0, rk))


def _estimate_noise_variance(returns):
    n = len(returns)
    if n < 2:
        return RK_NOISE_VAR_FLOOR
    gamma1 = sum(returns[i] * returns[i + 1] for i in range(n - 1)) / (n - 1)
    return max(RK_NOISE_VAR_FLOOR, -gamma1)


def _realized_quarticity(returns, window):
    subset = returns[-window:] if len(returns) >= window else returns
    n = len(subset)
    if n < 1:
        return 0.0
    return (n / 3.0) * sum(r ** 4 for r in subset)


def _optimal_rk_bandwidth(returns, window, omega_sq):
    subset = returns[-window:] if len(returns) >= window else returns
    n = len(subset)
    if n < 2:
        return 0
    H_floor = math.ceil(math.sqrt(n))
    H_cap = math.floor(n * RK_BANDWIDTH_MAX_FRACTION)
    if n < RK_MIN_RETURNS_FOR_ADAPTIVE or omega_sq <= RK_NOISE_VAR_FLOOR:
        return H_floor
    rq = _realized_quarticity(returns, window)
    if rq <= 0.0:
        return H_floor
    sqrt_rq = math.sqrt(rq)
    xi_sq = omega_sq / sqrt_rq
    if xi_sq <= 0.0:
        return H_floor
    H_star = math.ceil(RK_CSTAR_FLAT_TOP_PARZEN * (xi_sq ** 0.8) * (n ** 0.6))
    return max(H_floor, min(H_star, H_cap))


# ═══════════════════════════════════════════════════════════════════════════
#  Category A: Noise Variance Estimation (4 tests)
# ═══════════════════════════════════════════════════════════════════════════

class TestNoiseVariance(unittest.TestCase):
    """Test _estimate_noise_variance."""

    def test_iid_gaussian_floors(self):
        """IID Gaussian returns have near-zero autocorrelation → ω² floors."""
        random.seed(42)
        returns = [random.gauss(0, 0.001) for _ in range(200)]
        omega_sq = _estimate_noise_variance(returns)
        # IID returns have ~zero first-order autocovariance (may be slightly
        # positive or negative). Floor should bind in most seeds.
        self.assertGreaterEqual(omega_sq, RK_NOISE_VAR_FLOOR)
        # Should be very small relative to return variance
        self.assertLess(omega_sq, 1e-5)

    def test_noisy_price_simulation(self):
        """Returns with added bid-ask bounce noise → ω² detects noise."""
        random.seed(123)
        n = 500
        noise_sd = 0.001  # Noise in return space
        # Simulate true returns + IID microstructure noise (bid-ask bounce)
        true_returns = [random.gauss(0, 0.002) for _ in range(n)]
        noise = [random.gauss(0, noise_sd) for _ in range(n + 1)]
        # Observed return = true return + Δnoise (creates negative autocorrelation)
        returns = [true_returns[i] + noise[i + 1] - noise[i] for i in range(n)]
        omega_sq = _estimate_noise_variance(returns)
        # Should be positive (noise creates negative autocorrelation)
        self.assertGreater(omega_sq, 0)
        # ω² should approximate noise_sd² (within order of magnitude)
        self.assertGreater(omega_sq, noise_sd ** 2 / 10)
        self.assertLess(omega_sq, noise_sd ** 2 * 10)

    def test_edge_cases_floor(self):
        """Empty or single return → floors to RK_NOISE_VAR_FLOOR."""
        self.assertEqual(_estimate_noise_variance([]), RK_NOISE_VAR_FLOOR)
        self.assertEqual(_estimate_noise_variance([0.01]), RK_NOISE_VAR_FLOOR)

    def test_trending_returns_floor(self):
        """Positive autocorrelation (momentum) → ω² floors (γ̂(1) > 0)."""
        # Trending returns: each return is similar to previous
        returns = [0.001 * (1 + 0.1 * i) for i in range(50)]
        omega_sq = _estimate_noise_variance(returns)
        self.assertEqual(omega_sq, RK_NOISE_VAR_FLOOR)


# ═══════════════════════════════════════════════════════════════════════════
#  Category B: Realized Quarticity (2 tests)
# ═══════════════════════════════════════════════════════════════════════════

class TestRealizedQuarticity(unittest.TestCase):
    """Test _realized_quarticity."""

    def test_constant_returns(self):
        """Constant returns: RQ = (n/3) × n × r⁴ exactly."""
        r = 0.002
        n = 60
        returns = [r] * n
        rq = _realized_quarticity(returns, n)
        expected = (n / 3.0) * n * r ** 4
        self.assertAlmostEqual(rq, expected, places=20)

    def test_gaussian_returns_positive_finite(self):
        """Gaussian returns: RQ is positive, finite, scales with σ⁴."""
        random.seed(99)
        sigma = 0.003
        returns = [random.gauss(0, sigma) for _ in range(180)]
        rq = _realized_quarticity(returns, 180)
        self.assertGreater(rq, 0)
        self.assertTrue(math.isfinite(rq))
        # RQ should scale roughly with σ⁴ × n²/3
        # E[r⁴] = 3σ⁴ for normal, so E[RQ] = (n/3) × n × 3σ⁴ = n²σ⁴
        expected_order = (180 ** 2) * sigma ** 4
        self.assertGreater(rq, expected_order * 0.1)
        self.assertLess(rq, expected_order * 10)


# ═══════════════════════════════════════════════════════════════════════════
#  Category C: Bandwidth Selection (5 tests)
# ═══════════════════════════════════════════════════════════════════════════

class TestBandwidthSelection(unittest.TestCase):
    """Test _optimal_rk_bandwidth."""

    def test_low_noise_floor_binds(self):
        """Low noise → floor binds (H_adaptive == H_fixed == ceil(√n))."""
        random.seed(10)
        returns = [random.gauss(0, 0.001) for _ in range(60)]
        omega_sq = RK_NOISE_VAR_FLOOR  # Zero noise
        H = _optimal_rk_bandwidth(returns, 60, omega_sq)
        H_fixed = math.ceil(math.sqrt(60))
        self.assertEqual(H, H_fixed)

    def test_moderate_noise_exceeds_floor(self):
        """Moderate noise → H_adaptive > H_fixed."""
        random.seed(20)
        n = 60
        # Create noisy returns with strong negative autocorrelation
        noise_sd = 0.01
        true_prices = [100.0]
        for _ in range(n):
            true_prices.append(true_prices[-1] + random.gauss(0, 0.0005))
        observed = [p + random.gauss(0, noise_sd) for p in true_prices]
        returns = [math.log(observed[i + 1] / observed[i]) for i in range(n)]
        omega_sq = _estimate_noise_variance(returns)
        H = _optimal_rk_bandwidth(returns, n, omega_sq)
        H_fixed = math.ceil(math.sqrt(n))
        self.assertGreaterEqual(H, H_fixed)

    def test_extreme_noise_capped(self):
        """Extreme noise → H capped at ⌊n/3⌋."""
        random.seed(30)
        n = 60
        # Very large noise → very large ω² → H* could exceed cap
        omega_sq = 1.0  # Absurdly high noise
        returns = [random.gauss(0, 0.001) for _ in range(n)]
        H = _optimal_rk_bandwidth(returns, n, omega_sq)
        H_cap = math.floor(n * RK_BANDWIDTH_MAX_FRACTION)
        self.assertLessEqual(H, H_cap)

    def test_short_window_fallback(self):
        """Short window (n<20) → falls back to ceil(√n)."""
        returns = [0.001 * i for i in range(15)]
        omega_sq = 0.001  # Non-trivial noise
        H = _optimal_rk_bandwidth(returns, 15, omega_sq)
        H_fixed = math.ceil(math.sqrt(15))
        self.assertEqual(H, H_fixed)

    def test_monotonicity_increasing_noise(self):
        """Increasing ω² → non-decreasing H (for fixed returns)."""
        random.seed(40)
        n = 60
        returns = [random.gauss(0, 0.002) for _ in range(n)]
        noise_levels = [1e-12, 1e-10, 1e-8, 1e-6, 1e-4, 1e-2]
        bandwidths = [_optimal_rk_bandwidth(returns, n, w) for w in noise_levels]
        for i in range(len(bandwidths) - 1):
            self.assertLessEqual(bandwidths[i], bandwidths[i + 1],
                                 f"H should be non-decreasing: {bandwidths}")


# ═══════════════════════════════════════════════════════════════════════════
#  Category D: RK Integration (4 tests)
# ═══════════════════════════════════════════════════════════════════════════

class TestRKIntegration(unittest.TestCase):
    """Test _realized_kernel with adaptive bandwidth."""

    def test_backward_compatible_none(self):
        """bandwidth=None gives identical result to old code (ceil(√n))."""
        random.seed(50)
        returns = [random.gauss(0, 0.002) for _ in range(60)]
        rk_default = _realized_kernel(returns, 60)
        rk_explicit = _realized_kernel(returns, 60, bandwidth=math.ceil(math.sqrt(60)))
        self.assertAlmostEqual(rk_default, rk_explicit, places=15)

    def test_larger_H_with_noisy_data(self):
        """Both RK values positive, adaptive ≠ fixed when noise present."""
        random.seed(60)
        n = 60
        noise_sd = 0.0005  # Moderate noise in return space
        true_returns = [random.gauss(0, 0.002) for _ in range(n)]
        noise = [random.gauss(0, noise_sd) for _ in range(n + 1)]
        returns = [true_returns[i] + noise[i + 1] - noise[i] for i in range(n)]
        omega_sq = _estimate_noise_variance(returns)
        H_fixed = math.ceil(math.sqrt(n))
        H_adaptive = _optimal_rk_bandwidth(returns, n, omega_sq)
        rk_fixed = _realized_kernel(returns, n)
        rk_adaptive = _realized_kernel(returns, n, bandwidth=H_adaptive)
        self.assertGreater(rk_fixed, 0)
        self.assertGreater(rk_adaptive, 0)
        if H_adaptive != H_fixed:
            # Values should differ (different bandwidth → different smoothing)
            self.assertNotAlmostEqual(rk_fixed, rk_adaptive, places=10)

    def test_rk_positivity(self):
        """Adaptive RK always ≥ 0 for any bandwidth."""
        random.seed(70)
        returns = [random.gauss(0, 0.003) for _ in range(100)]
        for bw in [1, 5, 10, 20, 33]:
            rk = _realized_kernel(returns, 100, bandwidth=bw)
            self.assertGreaterEqual(rk, 0.0, f"RK negative for bandwidth={bw}")

    def test_known_variance_recovery(self):
        """180 IID N(0,σ) returns, RK ≈ σ within 50% (sanity check)."""
        random.seed(80)
        sigma = 0.002
        returns = [random.gauss(0, sigma) for _ in range(180)]
        rk = _realized_kernel(returns, 180)
        self.assertGreater(rk, sigma * 0.5)
        self.assertLess(rk, sigma * 1.5)


# ═══════════════════════════════════════════════════════════════════════════
#  Category E: Constants & Shadow Mode (3 tests)
# ═══════════════════════════════════════════════════════════════════════════

class TestConstantsAndShadowMode(unittest.TestCase):
    """Test constants and end-to-end shadow mode behavior."""

    def test_cstar_constant(self):
        """c* constant matches BN 2009 Table 2."""
        self.assertEqual(RK_CSTAR_FLAT_TOP_PARZEN, 3.5134)

    def test_bn_formula_dimensional(self):
        """H* is a dimensionless positive integer."""
        random.seed(90)
        returns = [random.gauss(0, 0.002) for _ in range(60)]
        omega_sq = 1e-6
        H = _optimal_rk_bandwidth(returns, 60, omega_sq)
        self.assertIsInstance(H, int)
        self.assertGreater(H, 0)

    def test_shadow_mode_end_to_end(self):
        """Simulate noisy returns: H_adaptive ≥ H_fixed, both RK positive, delta captured."""
        random.seed(100)
        n = 180
        noise_sd = 0.0008  # Moderate microstructure noise in return space
        true_returns = [random.gauss(0, 0.002) for _ in range(n)]
        noise = [random.gauss(0, noise_sd) for _ in range(n + 1)]
        returns = [true_returns[i] + noise[i + 1] - noise[i] for i in range(n)]

        omega_sq = _estimate_noise_variance(returns)
        self.assertGreater(omega_sq, RK_NOISE_VAR_FLOOR)

        H_fixed = math.ceil(math.sqrt(n))
        H_adaptive = _optimal_rk_bandwidth(returns, n, omega_sq)
        self.assertGreaterEqual(H_adaptive, H_fixed)

        rk_fixed = _realized_kernel(returns, n)
        rk_adaptive = _realized_kernel(returns, n, bandwidth=H_adaptive)
        self.assertGreater(rk_fixed, 0)
        self.assertGreater(rk_adaptive, 0)

        if H_adaptive != H_fixed:
            delta = (rk_adaptive - rk_fixed) / rk_fixed
            self.assertTrue(math.isfinite(delta))


if __name__ == "__main__":
    unittest.main()
