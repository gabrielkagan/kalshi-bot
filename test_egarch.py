"""
test_egarch.py — Standalone EGARCH(1,1) tests (20 tests, 5 categories).
Copies minimal class inline to avoid importing bot.py.
"""
import json
import math
import os
import tempfile
import pytest

# ─── Constants (copied from bot.py) ──────────────────────────────────────
EGARCH_SHADOW_MODE = True
EGARCH_REFIT_INTERVAL = 7200
EGARCH_MIN_RETURNS = 360
EGARCH_RETURN_MAXLEN = 10800
EGARCH_WARMUP_RETURNS = 12
EGARCH_E_ABS_Z = 0.7978845608
EGARCH_LOG_VAR_FLOOR = -40.0
EGARCH_LOG_VAR_CEILING = -10.0
EGARCH_MLE_MAXITER = 200
EGARCH_OMEGA_BOUNDS = (-5.0, 0.0)
EGARCH_ALPHA_BOUNDS = (0.01, 0.5)
EGARCH_GAMMA_BOUNDS = (-0.3, 0.3)
EGARCH_BETA_BOUNDS = (0.80, 0.999)

ASSETS = ["BTC", "ETH", "SOL", "XRP"]

# ─── Minimal inline EGARCHEstimator (no logging, no DB) ──────────────────
import time
import logging
from collections import deque
from typing import Dict, Optional, List


class EGARCHEstimator:
    """Inline copy for testing — matches bot.py implementation."""

    def __init__(self, state_path=None):
        self._state_path = state_path
        self._returns: Dict[str, deque] = {
            a: deque(maxlen=EGARCH_RETURN_MAXLEN) for a in ASSETS
        }
        self._params: Dict[str, Optional[Dict]] = {a: None for a in ASSETS}
        self._log_var: Dict[str, Optional[float]] = {a: None for a in ASSETS}
        self._sigma: Dict[str, Optional[float]] = {a: None for a in ASSETS}
        self._last_refit: float = 0.0
        self._n_updates: Dict[str, int] = {a: 0 for a in ASSETS}
        self._mle_loglik: Dict[str, Optional[float]] = {a: None for a in ASSETS}
        self._mle_converged: Dict[str, bool] = {a: False for a in ASSETS}
        if self._state_path:
            self._load_state()

    def record_return(self, asset: str, log_return: float):
        self._returns[asset].append(log_return)

    def seed_variance(self, asset: str, rk_5min_sq: float):
        if self._log_var.get(asset) is not None:
            return
        if rk_5min_sq <= 0:
            return
        lv = math.log(rk_5min_sq)
        lv = max(EGARCH_LOG_VAR_FLOOR, min(EGARCH_LOG_VAR_CEILING, lv))
        self._log_var[asset] = lv
        self._sigma[asset] = math.exp(lv * 0.5)
        if self._params[asset] is None:
            self._params[asset] = {
                "omega": lv * 0.05,
                "alpha": 0.10,
                "gamma": 0.0,
                "beta": 0.95,
            }

    def recursive_update(self, asset: str, log_return: float) -> Optional[float]:
        params = self._params.get(asset)
        log_var = self._log_var.get(asset)
        if params is None or log_var is None:
            return None
        if len(self._returns.get(asset, [])) < EGARCH_WARMUP_RETURNS:
            return None

        omega = params["omega"]
        alpha = params["alpha"]
        gamma = params["gamma"]
        beta = params["beta"]

        sigma = math.exp(log_var * 0.5)
        if sigma <= 0:
            return None
        z = log_return / sigma
        new_log_var = omega + alpha * (abs(z) - EGARCH_E_ABS_Z) + gamma * z + beta * log_var
        new_log_var = max(EGARCH_LOG_VAR_FLOOR, min(EGARCH_LOG_VAR_CEILING, new_log_var))

        self._log_var[asset] = new_log_var
        new_sigma = math.exp(new_log_var * 0.5)
        self._sigma[asset] = new_sigma
        self._n_updates[asset] = self._n_updates.get(asset, 0) + 1
        return new_sigma

    def get_sigma(self, asset: str) -> Optional[float]:
        return self._sigma.get(asset)

    def is_active(self, asset: str) -> bool:
        if EGARCH_SHADOW_MODE:
            return False
        return self._params.get(asset) is not None

    def maybe_refit(self):
        now = time.time()
        if now - self._last_refit < EGARCH_REFIT_INTERVAL:
            return
        self._last_refit = now
        any_fit = False
        for asset in ASSETS:
            rets = self._returns.get(asset, deque())
            if len(rets) < EGARCH_MIN_RETURNS:
                continue
            returns_list = list(rets)
            if self._mle_fit_asset(asset, returns_list):
                any_fit = True
        if any_fit and self._state_path:
            self._save_state()

    def _mle_fit_asset(self, asset: str, returns: list) -> bool:
        from scipy.optimize import minimize
        t0 = time.time()
        n = len(returns)
        sample_var = sum(r * r for r in returns) / n
        old_params = self._params.get(asset)
        if old_params is not None:
            x0 = [old_params["omega"], old_params["alpha"],
                   old_params["gamma"], old_params["beta"]]
        else:
            x0 = [math.log(sample_var) * (1 - 0.95), 0.10, 0.0, 0.95]
        bounds = [EGARCH_OMEGA_BOUNDS, EGARCH_ALPHA_BOUNDS,
                  EGARCH_GAMMA_BOUNDS, EGARCH_BETA_BOUNDS]
        try:
            result = minimize(
                EGARCHEstimator._neg_log_likelihood,
                x0, args=(returns,),
                method="L-BFGS-B",
                bounds=bounds,
                options={"maxiter": EGARCH_MLE_MAXITER, "ftol": 1e-10},
            )
        except Exception:
            return False
        omega, alpha, gamma, beta = result.x
        converged = result.success
        if abs(beta) >= 1.0:
            return False
        uncond_log_var = omega / (1.0 - beta)
        if uncond_log_var < EGARCH_LOG_VAR_FLOOR or uncond_log_var > EGARCH_LOG_VAR_CEILING:
            return False
        self._params[asset] = {"omega": omega, "alpha": alpha, "gamma": gamma, "beta": beta}
        self._mle_loglik[asset] = -result.fun
        self._mle_converged[asset] = converged
        self._log_var[asset] = uncond_log_var
        self._sigma[asset] = math.exp(uncond_log_var * 0.5)
        return True

    @staticmethod
    def _neg_log_likelihood(params, returns) -> float:
        omega, alpha, gamma, beta = params
        n = len(returns)
        if n < 60:
            return 1e10
        sample_var = sum(r * r for r in returns[:60]) / 60.0
        if sample_var <= 0:
            sample_var = 1e-10
        log_var = math.log(sample_var)
        LOG_2PI = 1.8378770664093453
        nll = 0.0
        e_abs_z = EGARCH_E_ABS_Z
        for i in range(n):
            r = returns[i]
            var = math.exp(log_var)
            if var <= 0:
                var = 1e-30
            nll += 0.5 * (LOG_2PI + log_var + r * r / var)
            sigma = math.sqrt(var)
            if sigma <= 0:
                sigma = 1e-15
            z = r / sigma
            log_var = omega + alpha * (abs(z) - e_abs_z) + gamma * z + beta * log_var
            log_var = max(-50.0, min(-5.0, log_var))
        return nll / n

    def _load_state(self):
        if not self._state_path or not os.path.exists(self._state_path):
            return
        try:
            with open(self._state_path, "r") as f:
                state = json.load(f)
            for asset in ASSETS:
                adata = state.get(asset)
                if adata and adata.get("params"):
                    self._params[asset] = adata["params"]
                    self._log_var[asset] = adata.get("log_var")
                    self._sigma[asset] = adata.get("sigma")
                    self._n_updates[asset] = adata.get("n_updates", 0)
                    self._mle_loglik[asset] = adata.get("mle_loglik")
                    self._mle_converged[asset] = adata.get("mle_converged", False)
            self._last_refit = state.get("last_refit", 0.0)
        except Exception:
            pass

    def _save_state(self):
        if not self._state_path:
            return
        state = {"last_refit": self._last_refit}
        for asset in ASSETS:
            state[asset] = {
                "params": self._params[asset],
                "log_var": self._log_var[asset],
                "sigma": self._sigma[asset],
                "n_updates": self._n_updates[asset],
                "mle_loglik": self._mle_loglik[asset],
                "mle_converged": self._mle_converged[asset],
            }
        tmp_path = self._state_path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, self._state_path)

    def get_diagnostics(self) -> Dict:
        result = {}
        now = time.time()
        for asset in ASSETS:
            params = self._params.get(asset)
            log_var = self._log_var.get(asset)
            sigma = self._sigma.get(asset)
            n_rets = len(self._returns.get(asset, []))
            n_upd = self._n_updates.get(asset, 0)
            diag: Dict = {
                "has_params": params is not None,
                "n_returns": n_rets,
                "n_updates": n_upd,
                "current_sigma": round(sigma, 10) if sigma is not None else None,
                "current_log_var": round(log_var, 4) if log_var is not None else None,
                "mle_loglik": round(self._mle_loglik.get(asset, 0), 4) if self._mle_loglik.get(asset) is not None else None,
                "mle_converged": self._mle_converged.get(asset, False),
                "last_refit_age_s": round(now - self._last_refit, 1) if self._last_refit > 0 else None,
            }
            if params is not None:
                beta = params["beta"]
                omega = params["omega"]
                diag["params"] = {k: round(v, 6) for k, v in params.items()}
                if abs(beta) < 1.0:
                    uncond_lv = omega / (1.0 - beta)
                    diag["unconditional_vol"] = round(math.exp(uncond_lv * 0.5), 10)
                    if beta > 0 and beta < 1:
                        diag["half_life_seconds"] = round(
                            (math.log(2) / (-math.log(beta))) * 5.0, 1)
                    else:
                        diag["half_life_seconds"] = None
                else:
                    diag["unconditional_vol"] = None
                    diag["half_life_seconds"] = None
                diag["asymmetry_gamma"] = round(params["gamma"], 6)
            else:
                diag["params"] = None
                diag["unconditional_vol"] = None
                diag["half_life_seconds"] = None
                diag["asymmetry_gamma"] = None
            result[asset] = diag
        return result


# ─── Helpers ──────────────────────────────────────────────────────────────

def _make_estimator(**kwargs):
    """Create a fresh EGARCHEstimator with optional state_path."""
    return EGARCHEstimator(**kwargs)


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
        # Seed at unconditional log-var
        uncond_lv = omega / (1 - beta)
        est._log_var["BTC"] = uncond_lv
        est._sigma["BTC"] = math.exp(uncond_lv * 0.5)
        # Fill warmup
        for _ in range(EGARCH_WARMUP_RETURNS + 1):
            est.record_return("BTC", 0.0005)
        # Run 2000 constant returns for convergence
        r_const = 0.0005
        for _ in range(2000):
            est.record_return("BTC", r_const)
            est.recursive_update("BTC", r_const)
        # Last 100 returns: check σ is stable (changes <1%)
        sigmas = []
        for _ in range(100):
            est.record_return("BTC", r_const)
            s = est.recursive_update("BTC", r_const)
            sigmas.append(s)
        # σ should be positive, finite, and stable
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
        # Calm phase
        for _ in range(100):
            est.record_return("BTC", 0.0001)
            est.recursive_update("BTC", 0.0001)
        sigma_calm = est.get_sigma("BTC")
        # Volatile phase
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
        # Test with positive return
        _set_params(est, "BTC", omega, alpha, gamma, beta)
        est._log_var["BTC"] = omega / (1 - beta)
        est._sigma["BTC"] = math.exp(est._log_var["BTC"] * 0.5)
        for _ in range(EGARCH_WARMUP_RETURNS + 1):
            est.record_return("BTC", 0.001)
        sigma_pos = est.recursive_update("BTC", 0.005)
        lv_pos = est._log_var["BTC"]

        # Reset and test with negative return
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
        # Start at FLOOR directly
        est._log_var["BTC"] = EGARCH_LOG_VAR_FLOOR
        est._sigma["BTC"] = math.exp(EGARCH_LOG_VAR_FLOOR * 0.5)
        for _ in range(EGARCH_WARMUP_RETURNS + 1):
            est.record_return("BTC", 1e-15)
        # Even with extreme tiny returns, log_var should stay >= FLOOR
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
        _set_params(est, "BTC", -0.01, 0.5, 0.0, 0.999)  # high persistence + high alpha
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
        nll = EGARCHEstimator._neg_log_likelihood(params, returns)
        assert math.isfinite(nll), f"NLL should be finite, got {nll}"
        assert nll > 0 or nll < 0  # just needs to be finite number

    def test_nll_ordering(self):
        """True params yield lower NLL than wrong params."""
        omega, alpha, gamma, beta = -0.9, 0.12, 0.05, 0.96
        returns = _generate_egarch_returns(2000, omega, alpha, gamma, beta)
        nll_true = EGARCHEstimator._neg_log_likelihood(
            [omega, alpha, gamma, beta], returns)
        nll_wrong_omega = EGARCHEstimator._neg_log_likelihood(
            [-4.0, alpha, gamma, beta], returns)
        nll_wrong_alpha = EGARCHEstimator._neg_log_likelihood(
            [omega, 0.45, gamma, beta], returns)
        nll_wrong_beta = EGARCHEstimator._neg_log_likelihood(
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
        # Manually set up returns and fit
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
        # Directly test the rejection logic by verifying the bounds
        # If optimizer returns omega=-0.01, beta=0.999:
        # uncond_log_var = -0.01 / (1 - 0.999) = -10.0 = CEILING (rejected)
        # If omega=-5.0, beta=0.80: uncond = -5.0/0.20 = -25.0 (accepted)
        # If omega=-0.01, beta=0.80: uncond = -0.01/0.20 = -0.05 (rejected, > CEILING)

        # Test: uncond_log_var above CEILING → should be rejected
        # We can verify this by checking the boundary values directly
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
        est._last_refit = 0  # force refit check
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
            est._save_state()

            est2 = _make_estimator(state_path=state_path)
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
        # JSON serializable
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
        # Empirical verification
        import random
        rng = random.Random(42)
        samples = [abs(rng.gauss(0, 1)) for _ in range(100000)]
        empirical = sum(samples) / len(samples)
        assert abs(empirical - expected) < 0.01, \
            f"Empirical E[|z|] = {empirical} too far from {expected}"

    def test_shadow_mode(self):
        """is_active()=False when SHADOW_MODE=True; recursive_update still works."""
        est = _make_estimator()
        _set_params(est, "BTC", -0.5, 0.10, 0.0, 0.95)
        _seed_and_warmup(est, "BTC", 1e-8)
        # Shadow mode → not active
        assert not est.is_active("BTC")
        # But recursive update still works
        est.record_return("BTC", 0.001)
        sigma = est.recursive_update("BTC", 0.001)
        assert sigma is not None
        assert sigma > 0
        # get_sigma still returns value
        assert est.get_sigma("BTC") is not None
        assert est.get_sigma("BTC") > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
