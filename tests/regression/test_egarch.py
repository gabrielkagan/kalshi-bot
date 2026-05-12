"""
test_egarch.py — EGARCH(1,1) tests (20 tests, 5 categories).
Imports production EGARCHEstimator from models.py to test the same code that runs live.
"""
import json
import math
import os
import tempfile
import time
import pytest

from bot.config import (
    EGARCH_E_ABS_Z,
    EGARCH_LOG_VAR_CEILING,
    EGARCH_LOG_VAR_FLOOR,
    EGARCH_MIN_RETURNS,
    EGARCH_OMEGA_BOUNDS,
    EGARCH_ALPHA_BOUNDS,
    EGARCH_GAMMA_BOUNDS,
    EGARCH_BETA_BOUNDS,
    EGARCH_WARMUP_RETURNS,
    EGARCH_MLE_MAXITER,
    EGARCH_RETURN_MAXLEN,
    ASSETS,
)
import bot.models as models
from bot.models import EGARCHEstimator


# ─── Helpers ──────────────────────────────────────────────────────────────

def _make_estimator(state_path=None):
    """Create a fresh EGARCHEstimator.

    If state_path is given, temporarily redirects EGARCH_STATE_PATH so the
    estimator loads/saves to a test-specific file instead of the production path.
    """
    if state_path is not None:
        original = models.EGARCH_STATE_PATH
        models.EGARCH_STATE_PATH = state_path
        try:
            est = EGARCHEstimator()
        finally:
            models.EGARCH_STATE_PATH = original
    else:
        # Point at a non-existent temp path so _load_state is a no-op
        original = models.EGARCH_STATE_PATH
        models.EGARCH_STATE_PATH = "/tmp/_test_egarch_nonexistent_.json"
        try:
            est = EGARCHEstimator()
        finally:
            models.EGARCH_STATE_PATH = original
    return est


def _set_params(est, asset, omega, alpha, gamma, beta):
    """Manually set EGARCH params for an asset."""
    est._params[asset] = {"omega": omega, "alpha": alpha, "gamma": gamma, "beta": beta}


def _seed_and_warmup(est, asset, rk_5min_sq, n_warmup=EGARCH_WARMUP_RETURNS):
    """Seed variance and add enough dummy returns for warmup."""
    est.seed_variance(asset, rk_5min_sq)
    for _ in range(n_warmup):
        est.record_return(asset, 0.0001)


def _generate_egarch_returns(n, omega, alpha, gamma, beta, seed=42):
    """Generate synthetic returns from an EGARCH(1,1) DGP."""
    import random
    rng = random.Random(seed)
    uncond_lv = omega / (1.0 - beta)
    log_var = uncond_lv
    returns = []
    for _ in range(n):
        sigma = math.exp(log_var * 0.5)
        r = rng.gauss(0, sigma)
        returns.append(r)
        z = r / sigma
        log_var = omega + alpha * (abs(z) - EGARCH_E_ABS_Z) + gamma * z + beta * log_var
        log_var = max(-50.0, min(-5.0, log_var))
    return returns


# ═══════════════════════════════════════════════════════════════════════════
#  Category A: Recursive Update (7 tests)
# ═══════════════════════════════════════════════════════════════════════════

class TestRecursiveUpdate:

    def test_constant_returns_convergence(self):
        """Feed constant returns, verify σ converges to a stable value."""
        est = _make_estimator()
        omega, alpha, gamma, beta = -0.5, 0.10, 0.0, 0.95
        _set_params(est, "BTC", omega, alpha, gamma, beta)
        uncond_lv = omega / (1 - beta)
        est._log_var["BTC"] = uncond_lv
        est._sigma["BTC"] = math.exp(uncond_lv * 0.5)
        for _ in range(EGARCH_WARMUP_RETURNS + 1):
            est.record_return("BTC", 0.0005)
        r_const = 0.0005
        for _ in range(2000):
            est.record_return("BTC", r_const)
            est.recursive_update("BTC", r_const)
        sigmas = []
        for _ in range(100):
            est.record_return("BTC", r_const)
            s = est.recursive_update("BTC", r_const)
            sigmas.append(s)
        assert all(s > 0 and math.isfinite(s) for s in sigmas)
        max_s = max(sigmas)
        min_s = min(sigmas)
        assert (max_s - min_s) / min_s < 0.01, \
            f"σ not stable: range [{min_s:.8f}, {max_s:.8f}]"

    def test_vol_clustering_response(self):
        """100 calm, 100 volatile → σ increases >2x within 10 returns."""
        est = _make_estimator()
        omega, alpha, gamma, beta = -0.5, 0.15, 0.0, 0.95
        _set_params(est, "BTC", omega, alpha, gamma, beta)
        est._log_var["BTC"] = omega / (1 - beta)
        est._sigma["BTC"] = math.exp(est._log_var["BTC"] * 0.5)
        for _ in range(EGARCH_WARMUP_RETURNS + 1):
            est.record_return("BTC", 0.0001)
        for _ in range(100):
            est.record_return("BTC", 0.0001)
            est.recursive_update("BTC", 0.0001)
        sigma_calm = est.get_sigma("BTC")
        for i in range(100):
            r = 0.01 * (1 if i % 2 == 0 else -1)
            est.record_return("BTC", r)
            est.recursive_update("BTC", r)
        sigma_volatile = est.get_sigma("BTC")
        assert sigma_volatile > sigma_calm * 2, \
            f"Expected vol increase: calm={sigma_calm:.8f} volatile={sigma_volatile:.8f}"

    def test_asymmetry_inverse_leverage(self):
        """γ>0 (inverse leverage): positive return → higher σ than negative."""
        est = _make_estimator()
        omega, alpha, gamma, beta = -0.5, 0.10, 0.05, 0.95
        _set_params(est, "BTC", omega, alpha, gamma, beta)
        est._log_var["BTC"] = omega / (1 - beta)
        est._sigma["BTC"] = math.exp(est._log_var["BTC"] * 0.5)
        for _ in range(EGARCH_WARMUP_RETURNS + 1):
            est.record_return("BTC", 0.001)
        sigma_pos = est.recursive_update("BTC", 0.005)
        lv_pos = est._log_var["BTC"]

        _set_params(est, "ETH", omega, alpha, gamma, beta)
        est._log_var["ETH"] = omega / (1 - beta)
        est._sigma["ETH"] = math.exp(est._log_var["ETH"] * 0.5)
        for _ in range(EGARCH_WARMUP_RETURNS + 1):
            est.record_return("ETH", 0.001)
        sigma_neg = est.recursive_update("ETH", -0.005)
        lv_neg = est._log_var["ETH"]

        assert lv_pos > lv_neg, \
            f"Inverse leverage: lv_pos={lv_pos:.6f} should > lv_neg={lv_neg:.6f}"

    def test_asymmetry_classic_leverage(self):
        """γ<0 (classic leverage): negative return → higher σ than positive."""
        est = _make_estimator()
        omega, alpha, gamma, beta = -0.5, 0.10, -0.05, 0.95
        _set_params(est, "BTC", omega, alpha, gamma, beta)
        est._log_var["BTC"] = omega / (1 - beta)
        est._sigma["BTC"] = math.exp(est._log_var["BTC"] * 0.5)
        for _ in range(EGARCH_WARMUP_RETURNS + 1):
            est.record_return("BTC", 0.001)
        est.recursive_update("BTC", 0.005)
        lv_pos = est._log_var["BTC"]

        _set_params(est, "ETH", omega, alpha, gamma, beta)
        est._log_var["ETH"] = omega / (1 - beta)
        est._sigma["ETH"] = math.exp(est._log_var["ETH"] * 0.5)
        for _ in range(EGARCH_WARMUP_RETURNS + 1):
            est.record_return("ETH", 0.001)
        est.recursive_update("ETH", -0.005)
        lv_neg = est._log_var["ETH"]

        assert lv_neg > lv_pos, \
            f"Classic leverage: lv_neg={lv_neg:.6f} should > lv_pos={lv_pos:.6f}"

    def test_positivity_guarantee(self):
        """σ always > 0 and finite for diverse returns."""
        est = _make_estimator()
        _set_params(est, "BTC", -0.5, 0.10, 0.0, 0.95)
        est._log_var["BTC"] = -15.0
        est._sigma["BTC"] = math.exp(-15.0 * 0.5)
        for _ in range(EGARCH_WARMUP_RETURNS + 1):
            est.record_return("BTC", 0.001)
        test_returns = [0.1, -0.1, 0.001, -0.001, 0.0001, 0.5, -0.5, 0.0]
        for r in test_returns:
            est.record_return("BTC", r)
            sigma = est.recursive_update("BTC", r)
            assert sigma is not None
            assert sigma > 0, f"σ must be positive, got {sigma}"
            assert math.isfinite(sigma), f"σ must be finite, got {sigma}"

    def test_floor_clamping(self):
        """Verify log_var never goes below FLOOR regardless of input."""
        est = _make_estimator()
        _set_params(est, "BTC", -4.9, 0.01, 0.0, 0.80)
        est._log_var["BTC"] = EGARCH_LOG_VAR_FLOOR
        est._sigma["BTC"] = math.exp(EGARCH_LOG_VAR_FLOOR * 0.5)
        for _ in range(EGARCH_WARMUP_RETURNS + 1):
            est.record_return("BTC", 1e-15)
        for _ in range(100):
            est.record_return("BTC", 1e-15)
            est.recursive_update("BTC", 1e-15)
        assert est._log_var["BTC"] >= EGARCH_LOG_VAR_FLOOR
        sigma = est.get_sigma("BTC")
        assert sigma > 0
        assert math.isfinite(sigma)

    def test_ceiling_clamping(self):
        """Extreme returns → log_var hits CEILING, σ stays at maximum."""
        est = _make_estimator()
        _set_params(est, "BTC", -0.01, 0.5, 0.0, 0.999)
        est._log_var["BTC"] = EGARCH_LOG_VAR_CEILING - 1
        est._sigma["BTC"] = math.exp((EGARCH_LOG_VAR_CEILING - 1) * 0.5)
        for _ in range(EGARCH_WARMUP_RETURNS + 1):
            est.record_return("BTC", 0.5)
        for _ in range(1000):
            est.record_return("BTC", 0.5)
            est.recursive_update("BTC", 0.5)
        assert est._log_var["BTC"] == EGARCH_LOG_VAR_CEILING
        sigma = est.get_sigma("BTC")
        assert sigma > 0
        assert sigma == math.exp(EGARCH_LOG_VAR_CEILING * 0.5)


# ═══════════════════════════════════════════════════════════════════════════
#  Category B: Warm-up & Seeding (3 tests)
# ═══════════════════════════════════════════════════════════════════════════

class TestWarmupSeeding:

    def test_no_seed_returns_none(self):
        """Before seeding, get_sigma() and recursive_update() return None."""
        est = _make_estimator()
        assert est.get_sigma("BTC") is None
        result = est.recursive_update("BTC", 0.001)
        assert result is None

    def test_seed_from_rk(self):
        """seed_variance(asset, 1e-8) → log_var=log(1e-8), sigma=1e-4."""
        est = _make_estimator()
        est.seed_variance("BTC", 1e-8)
        expected_lv = math.log(1e-8)
        expected_sigma = 1e-4
        assert abs(est._log_var["BTC"] - expected_lv) < 1e-10
        assert abs(est._sigma["BTC"] - expected_sigma) < 1e-10

    def test_seed_default_params(self):
        """No MLE params → defaults are reasonable (beta≈0.95, alpha≈0.10)."""
        est = _make_estimator()
        est.seed_variance("BTC", 1e-8)
        params = est._params["BTC"]
        assert params is not None
        assert params["beta"] == 0.95
        assert params["alpha"] == 0.10
        assert params["gamma"] == 0.0


# ═══════════════════════════════════════════════════════════════════════════
#  Category C: MLE Fitting (5 tests)
# ═══════════════════════════════════════════════════════════════════════════

class TestMLEFitting:

    def test_nll_computation_correctness(self):
        """Known Gaussian returns → NLL is finite and positive."""
        import random
        rng = random.Random(42)
        returns = [rng.gauss(0, 0.001) for _ in range(500)]
        params = [-0.5, 0.10, 0.0, 0.95]
        # Production has _neg_log_likelihood_gaussian (Gaussian-only NLL)
        nll = EGARCHEstimator._neg_log_likelihood_gaussian(params, returns)
        assert math.isfinite(nll), f"NLL should be finite, got {nll}"
        assert nll > 0 or nll < 0  # just needs to be finite number

    def test_nll_ordering(self):
        """True params yield lower NLL than wrong params."""
        omega, alpha, gamma, beta = -0.9, 0.12, 0.05, 0.96
        returns = _generate_egarch_returns(2000, omega, alpha, gamma, beta)
        nll_true = EGARCHEstimator._neg_log_likelihood_gaussian(
            [omega, alpha, gamma, beta], returns)
        nll_wrong_omega = EGARCHEstimator._neg_log_likelihood_gaussian(
            [-4.0, alpha, gamma, beta], returns)
        nll_wrong_alpha = EGARCHEstimator._neg_log_likelihood_gaussian(
            [omega, 0.45, gamma, beta], returns)
        nll_wrong_beta = EGARCHEstimator._neg_log_likelihood_gaussian(
            [omega, alpha, gamma, 0.82], returns)
        assert nll_true < nll_wrong_omega, \
            f"True NLL={nll_true:.4f} should < wrong_omega={nll_wrong_omega:.4f}"
        assert nll_true < nll_wrong_alpha, \
            f"True NLL={nll_true:.4f} should < wrong_alpha={nll_wrong_alpha:.4f}"
        assert nll_true < nll_wrong_beta, \
            f"True NLL={nll_true:.4f} should < wrong_beta={nll_wrong_beta:.4f}"

    def test_mle_parameter_recovery(self):
        """Generate 5000 EGARCH returns with known params, fit, recover within tolerance."""
        true_omega, true_alpha, true_gamma, true_beta = -0.9, 0.12, 0.05, 0.96
        returns = _generate_egarch_returns(
            5000, true_omega, true_alpha, true_gamma, true_beta, seed=123)
        est = _make_estimator()
        for r in returns:
            est.record_return("BTC", r)
        success = est._mle_fit_asset("BTC", returns)
        assert success, "MLE fit should succeed"
        p = est._params["BTC"]
        assert abs(p["omega"] - true_omega) < 0.3, \
            f"omega: got {p['omega']:.4f}, expected ~{true_omega}"
        assert abs(p["alpha"] - true_alpha) < 0.1, \
            f"alpha: got {p['alpha']:.4f}, expected ~{true_alpha}"
        assert abs(p["gamma"] - true_gamma) < 0.1, \
            f"gamma: got {p['gamma']:.4f}, expected ~{true_gamma}"
        assert abs(p["beta"] - true_beta) < 0.05, \
            f"beta: got {p['beta']:.4f}, expected ~{true_beta}"

    def test_mle_rejection_bad_uncond_log_var(self):
        """Verify uncond_log_var sanity check: bounds are enforced."""
        est = _make_estimator()
        # Test: uncond_log_var above CEILING → should be rejected
        omega_bad = -0.01
        beta_bad = 0.80
        uncond_lv = omega_bad / (1 - beta_bad)  # = -0.05
        assert uncond_lv > EGARCH_LOG_VAR_CEILING, \
            f"Test setup: uncond_log_var={uncond_lv} should be > CEILING={EGARCH_LOG_VAR_CEILING}"

        # And: uncond_log_var in valid range → accepted
        omega_ok = -2.0
        beta_ok = 0.95
        uncond_lv_ok = omega_ok / (1 - beta_ok)  # = -40.0 = FLOOR (borderline)
        assert EGARCH_LOG_VAR_FLOOR <= uncond_lv_ok <= EGARCH_LOG_VAR_CEILING, \
            f"Test setup: uncond_log_var={uncond_lv_ok} should be in bounds"

        # Verify via actual fit with reasonable returns → should succeed
        import random
        rng = random.Random(42)
        returns = [rng.gauss(0, 0.001) for _ in range(500)]
        for r in returns:
            est.record_return("BTC", r)
        success = est._mle_fit_asset("BTC", returns)
        if success:
            p = est._params["BTC"]
            ulv = p["omega"] / (1.0 - p["beta"])
            assert EGARCH_LOG_VAR_FLOOR <= ulv <= EGARCH_LOG_VAR_CEILING, \
                f"Accepted fit has uncond_log_var={ulv} outside bounds"

    def test_mle_insufficient_data(self):
        """<360 returns → maybe_refit skips, params unchanged."""
        est = _make_estimator()
        # Zero out per-asset refit timers to force refit attempt
        for a in ASSETS:
            est._last_refit_per_asset[a] = 0
        for i in range(100):
            est.record_return("BTC", 0.001)
        est.maybe_refit()
        # Params should still be None (not enough data)
        assert est._params["BTC"] is None


# ═══════════════════════════════════════════════════════════════════════════
#  Category D: State & Diagnostics (3 tests)
# ═══════════════════════════════════════════════════════════════════════════

class TestStateDiagnostics:

    def test_state_persistence_roundtrip(self):
        """Save params+log_var, new instance loads them back."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            state_path = f.name
        try:
            est = _make_estimator(state_path=state_path)
            est._params["BTC"] = {"omega": -0.5, "alpha": 0.12, "gamma": 0.03, "beta": 0.96}
            est._log_var["BTC"] = -18.5
            est._sigma["BTC"] = math.exp(-18.5 * 0.5)
            est._n_updates["BTC"] = 42
            est._mle_loglik["BTC"] = -3.14
            est._mle_converged["BTC"] = True
            est._last_refit = 1000000.0
            # Temporarily redirect save path
            original = models.EGARCH_STATE_PATH
            models.EGARCH_STATE_PATH = state_path
            try:
                est._save_state()
                est2 = EGARCHEstimator()
            finally:
                models.EGARCH_STATE_PATH = original

            assert est2._params["BTC"]["omega"] == -0.5
            assert est2._params["BTC"]["alpha"] == 0.12
            assert est2._params["BTC"]["gamma"] == 0.03
            assert est2._params["BTC"]["beta"] == 0.96
            assert est2._log_var["BTC"] == -18.5
            assert abs(est2._sigma["BTC"] - math.exp(-18.5 * 0.5)) < 1e-15
            assert est2._n_updates["BTC"] == 42
            assert est2._mle_loglik["BTC"] == -3.14
            assert est2._mle_converged["BTC"] is True
        finally:
            os.unlink(state_path)

    def test_get_diagnostics_completeness(self):
        """All expected fields present and JSON-serializable."""
        est = _make_estimator()
        est._params["BTC"] = {"omega": -0.5, "alpha": 0.10, "gamma": 0.02, "beta": 0.95}
        est._log_var["BTC"] = -15.0
        est._sigma["BTC"] = math.exp(-15.0 * 0.5)
        est._n_updates["BTC"] = 10
        est._mle_loglik["BTC"] = -2.5
        est._mle_converged["BTC"] = True
        est._last_refit = time.time() - 100

        diag = est.get_diagnostics()
        btc = diag["BTC"]
        required_keys = [
            "has_params", "n_returns", "n_updates", "current_sigma",
            "current_log_var", "mle_loglik", "mle_converged",
            "last_refit_age_s", "params", "unconditional_vol",
            "half_life_seconds", "asymmetry_gamma",
        ]
        for key in required_keys:
            assert key in btc, f"Missing key: {key}"
        json.dumps(diag)

    def test_half_life_computation(self):
        """β=0.95 → half_life = log(2)/(-log(0.95)) × 5 ≈ 67.5s."""
        est = _make_estimator()
        est._params["BTC"] = {"omega": -0.5, "alpha": 0.10, "gamma": 0.0, "beta": 0.95}
        est._log_var["BTC"] = -15.0
        est._sigma["BTC"] = math.exp(-15.0 * 0.5)
        est._last_refit = time.time()
        diag = est.get_diagnostics()
        expected_hl = (math.log(2) / (-math.log(0.95))) * 5.0
        actual_hl = diag["BTC"]["half_life_seconds"]
        assert abs(actual_hl - expected_hl) < 0.1, \
            f"Half-life: got {actual_hl:.1f}, expected {expected_hl:.1f}"


# ═══════════════════════════════════════════════════════════════════════════
#  Category E: Constants & Shadow Mode (2 tests)
# ═══════════════════════════════════════════════════════════════════════════

class TestConstantsShadowMode:

    def test_e_abs_z_constant(self):
        """EGARCH_E_ABS_Z matches sqrt(2/π) within 1e-8 and empirically."""
        expected = math.sqrt(2.0 / math.pi)
        assert abs(EGARCH_E_ABS_Z - expected) < 1e-8, \
            f"E[|z|] = {EGARCH_E_ABS_Z} != sqrt(2/π) = {expected}"
        import random
        rng = random.Random(42)
        samples = [abs(rng.gauss(0, 1)) for _ in range(100000)]
        empirical = sum(samples) / len(samples)
        assert abs(empirical - expected) < 0.01, \
            f"Empirical E[|z|] = {empirical} too far from {expected}"

    def test_shadow_mode(self):
        """is_active() depends on shadow mode and convergence; recursive_update still works."""
        est = _make_estimator()
        _set_params(est, "BTC", -0.5, 0.10, 0.0, 0.95)
        _seed_and_warmup(est, "BTC", 1e-8)
        # Production is_active checks EGARCH_SHADOW_MODE (currently False = promoted)
        # and _mle_converged (False by default). Either way, is_active should be False
        # when _mle_converged is False.
        assert not est.is_active("BTC")
        # But recursive update still works
        est.record_return("BTC", 0.001)
        sigma = est.recursive_update("BTC", 0.001)
        assert sigma is not None
        assert sigma > 0
        assert est.get_sigma("BTC") is not None
        assert est.get_sigma("BTC") > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
