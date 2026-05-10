"""Pure-math model classes extracted from bot/_impl.py.

These classes have zero side effects (no API calls, no DB, no websockets).
They depend only on config.py constants and standard library + scipy.

bot/_impl.py imports these via `from models import ...` so runtime behavior
is unchanged.
"""

import json
import logging
import math
import os
import threading
import time
from collections import deque
from typing import Dict, List, Optional, Tuple

from config import (
    ASSETS,
    HWM_LOOKBACK_SECONDS,
    EGARCH_ALPHA_BOUNDS,
    EGARCH_BETA_BOUNDS,
    EGARCH_BUFFER_SAVE_INTERVAL,
    EGARCH_DF_BOUNDS,
    EGARCH_DF_DEFAULT,
    EGARCH_E_ABS_Z,
    EGARCH_GAMMA_BOUNDS,
    EGARCH_GAMMA_CONSTRAINTS,
    EGARCH_LOG_VAR_CEILING,
    EGARCH_LOG_VAR_FLOOR,
    EGARCH_MIN_RETURNS,
    EGARCH_MLE_EWL_LAMBDA,
    EGARCH_MLE_MAXITER,
    EGARCH_OMEGA_BOUNDS,
    EGARCH_REFIT_INTERVAL,
    EGARCH_REFIT_INTERVALS,
    EGARCH_RETURN_MAXLEN,
    EGARCH_SHADOW_MODE,
    EGARCH_STATE_PATH,
    EGARCH_WARMUP_RETURNS,
    EGARCH_BLEND_STATE_PATH,
    EGARCH_WEIGHT_BOUNDS,
    EGARCH_WEIGHT_DEFAULT,
    MZ_EMA_LAMBDA,
    MZ_EQUAL_WEIGHT_R2_THRESHOLD,
    MZ_MIN_OBS,
    MZ_RECOMPUTE_INTERVAL,
    MZ_SIGMOID_KAPPA,
    MZ_SIGMOID_Q_MID,
    MZ_SIGMOID_SHADOW_MODE,
    MZ_SIGMOID_W_MAX,
    MZ_WINDOW,
    DRAWDOWN_HALF_THRESHOLD,
    DRAWDOWN_HALT_THRESHOLD,
    DRAWDOWN_QUARTER_THRESHOLD,
    MAX_RISK_PER_TRADE,
    SIZING_TIERS,
)


# ═════════════════════════════════════════════════════════════════════════════
#  Strategy Group Mapping
# ═════════════════════════════════════════════════════════════════════════════

def strategy_to_group(strategy: str) -> str:
    """Map raw strategy string to strategy group for composite PK.
    Positions with the same group on the same ticker MERGE.
    Different groups STACK (when enabled)."""
    if not strategy:
        return "main"
    if strategy in ("MAKER_PATIENT", "TAKER_NOW", "MAKER_AGGRESSIVE", "PANIC_CAPTURE",
                     "CONFIRMATION_ADDON", "DIP_ADDON"):
        return "main"
    if strategy.startswith("decided_"):
        return "decided"
    return strategy


# ═════════════════════════════════════════════════════════════════════════════
#  Fee Helpers
# ═════════════════════════════════════════════════════════════════════════════

def calculate_fee(count: int, price_cents: int, is_taker: bool,
                   fee_mult_taker: float = 0.07, fee_mult_maker: float = 0.0) -> int:
    """Fee in cents. Ceil applied to TOTAL, not per contract.

    Taker:  ceil(fee_mult_taker × count × price × (100−price) / 100)
    Maker:  $0 — Kalshi charges no fee on maker fills (verified against API).

    The division by 100 converts from the raw product (price in cents ×
    complement in cents) back to cents.  Equivalent to the CLAUDE.md formula
    ceil(rate × C × P × (1−P)) evaluated in dollars, then converted to cents.
    SPX finance category gets 50% discount (fee_mult_taker=0.035).
    """
    if not is_taker:
        return 0
    return math.ceil(fee_mult_taker * count * price_cents * (100 - price_cents) / 100)


def calculate_taker_fee(count: int, price_cents: int) -> int:
    """Convenience wrapper — taker fee in cents."""
    return calculate_fee(count, price_cents, is_taker=True)


def calculate_maker_fee(count: int, price_cents: int) -> int:
    """Convenience wrapper — maker fee in cents. Kalshi charges $0 on maker fills."""
    return 0


# ── Shadow Time-Varying RK Weights ─────────────────────────────────────────
def compute_tv_rk_weights(seconds_to_close: float) -> Tuple[float, float, float]:
    """Compute time-varying RK blend weights based on seconds to expiry.

    Near expiry (< 60s): fast RK₁ dominates (most responsive).
    Far from expiry (> 180s): stable RK₅ and RK₁₅ dominate.
    Between: linear interpolation.

    Returns (w1, w5, w15) tuple that sums to 1.0.
    """
    if seconds_to_close <= 30:
        return (0.80, 0.15, 0.05)
    elif seconds_to_close <= 60:
        # Linear interp from 30→60
        t = (seconds_to_close - 30) / 30.0
        return (0.80 - 0.15 * t, 0.15 + 0.05 * t, 0.05 + 0.10 * t)
    elif seconds_to_close <= 120:
        # Linear interp from 60→120
        t = (seconds_to_close - 60) / 60.0
        return (0.65 - 0.15 * t, 0.20 + 0.05 * t, 0.15 + 0.05 * t)
    elif seconds_to_close <= 240:
        # Linear interp from 120→240
        t = (seconds_to_close - 120) / 120.0
        return (0.50 - 0.15 * t, 0.25 + 0.05 * t, 0.20 + 0.10 * t)
    else:
        return (0.35, 0.30, 0.35)


# ═════════════════════════════════════════════════════════════════════════════
#  _student_t_e_abs_z — helper for EGARCH Student-t innovations
# ═════════════════════════════════════════════════════════════════════════════

def _student_t_e_abs_z(df: float) -> float:
    """E[|z|] for z ~ Student-t(df). Falls back to Gaussian if df > 30 or invalid."""
    if df is None or df <= 2.0 or not math.isfinite(df):
        return EGARCH_E_ABS_Z  # Gaussian fallback
    if df > 30.0:
        return EGARCH_E_ABS_Z  # Essentially Gaussian
    half_df = df / 2.0
    try:
        e_abs_z = (math.sqrt(df - 2.0)
                   * math.exp(math.lgamma(half_df - 0.5) - math.lgamma(half_df))
                   / math.sqrt(math.pi))
    except (ValueError, OverflowError):
        logging.warning("Student-t E[|z|] computation failed for df=%.2f, using Gaussian", df)
        return EGARCH_E_ABS_Z
    if not math.isfinite(e_abs_z) or e_abs_z <= 0:
        return EGARCH_E_ABS_Z
    return e_abs_z


# ═════════════════════════════════════════════════════════════════════════════
#  EGARCHEstimator – Conditional volatility via EGARCH(1,1)
# ═════════════════════════════════════════════════════════════════════════════

class EGARCHEstimator:
    """EGARCH(1,1) conditional volatility estimator.

    Model: log(σ²_t) = ω + α·(|z_{t-1}| - E[|z|]) + γ·z_{t-1} + β·log(σ²_{t-1})
    where z_t = r_t / σ_t

    Log-variance specification guarantees σ²>0 without parameter constraints.
    The γ parameter captures crypto's documented inverse leverage effect.
    """

    def __init__(self):
        self._returns: Dict[str, deque] = {
            a: deque(maxlen=EGARCH_RETURN_MAXLEN) for a in ASSETS
        }
        self._params: Dict[str, Optional[Dict]] = {a: None for a in ASSETS}
        self._log_var: Dict[str, Optional[float]] = {a: None for a in ASSETS}
        self._sigma: Dict[str, Optional[float]] = {a: None for a in ASSETS}
        self._last_refit: float = time.time()  # avoid wasteful first-tick refit
        self._last_refit_per_asset: Dict[str, float] = {a: time.time() for a in ASSETS}
        self._n_updates: Dict[str, int] = {a: 0 for a in ASSETS}
        self._mle_loglik: Dict[str, Optional[float]] = {a: None for a in ASSETS}
        self._mle_converged: Dict[str, bool] = {a: False for a in ASSETS}
        self._last_buffer_save: float = 0.0
        self._lock = threading.Lock()  # protects param reads during MLE refit
        # Shadow: constrained EGARCH (alpha + beta ≤ 0.98) for comparison
        self._constrained_params: Dict[str, Optional[Dict]] = {a: None for a in ASSETS}
        self._constrained_log_var: Dict[str, Optional[float]] = {a: None for a in ASSETS}
        self._constrained_sigma: Dict[str, Optional[float]] = {a: None for a in ASSETS}
        self._load_state()

    def record_return(self, asset: str, log_return: float):
        """Append return to MLE buffer."""
        self._returns[asset].append(log_return)
        now = time.time()
        if now - self._last_buffer_save >= EGARCH_BUFFER_SAVE_INTERVAL:
            self._save_state()
            self._last_buffer_save = now
            try:
                fsize = os.path.getsize(EGARCH_STATE_PATH) / 1024.0
            except OSError:
                fsize = 0.0
            logging.info(
                "EGARCH buffer saved: %s (file_size=%.1fKB)",
                ", ".join(f"{a}={len(self._returns[a])}" for a in ASSETS),
                fsize)

    def seed_variance(self, asset: str, rk_5min_sq: float):
        """First-time init: set log_var from realized kernel variance."""
        if self._log_var.get(asset) is not None:
            return
        if rk_5min_sq <= 0:
            return
        lv = math.log(rk_5min_sq)
        lv = max(EGARCH_LOG_VAR_FLOOR, min(EGARCH_LOG_VAR_CEILING, lv))
        using_defaults = False
        with self._lock:
            self._log_var[asset] = lv
            self._sigma[asset] = math.exp(lv * 0.5)
            if self._params[asset] is None:
                self._params[asset] = {
                    "omega": lv * 0.05,
                    "alpha": 0.10,
                    "gamma": 0.0,
                    "beta": 0.95,
                }
                using_defaults = True
        logging.info(
            "EGARCH %s: seeded from RK (rk_5min=%.6f, log_var=%.4f, using_defaults=%s)",
            asset, math.sqrt(rk_5min_sq), lv, using_defaults)

    def recursive_update(self, asset: str, log_return: float) -> Optional[float]:
        """O(1) recursive EGARCH update. Returns new σ or None."""
        with self._lock:
            params = dict(self._params[asset]) if self._params.get(asset) else None
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
        if sigma <= 0 or not math.isfinite(sigma):
            return None
        z = log_return / sigma
        if not math.isfinite(z):
            return None
        # Dynamic E[|z|] based on Student-t df (if available)
        df = params.get("df")
        e_abs_z = _student_t_e_abs_z(df) if df is not None else EGARCH_E_ABS_Z
        raw_lv = omega + alpha * (abs(z) - e_abs_z) + gamma * z + beta * log_var
        if not math.isfinite(raw_lv):
            logging.warning("EGARCH %s: raw_lv is NaN/inf (z=%.4f, log_var=%.4f) — skipping update",
                            asset, z, log_var)
            return None
        new_log_var = max(EGARCH_LOG_VAR_FLOOR, min(EGARCH_LOG_VAR_CEILING, raw_lv))

        # Anomaly logging
        if raw_lv < EGARCH_LOG_VAR_FLOOR:
            logging.debug(
                "EGARCH %s: log_var clamped to FLOOR (was %.4f) — possible underflow",
                asset, raw_lv)
        elif raw_lv > EGARCH_LOG_VAR_CEILING:
            logging.debug(
                "EGARCH %s: log_var clamped to CEILING (was %.4f) — possible explosion",
                asset, raw_lv)

        new_sigma = math.exp(new_log_var * 0.5)
        if not math.isfinite(new_sigma) or new_sigma <= 0:
            logging.warning("EGARCH %s: new_sigma invalid (%.6f) — skipping", asset, new_sigma)
            return None
        with self._lock:
            self._log_var[asset] = new_log_var
            self._sigma[asset] = new_sigma
            self._n_updates[asset] = self._n_updates.get(asset, 0) + 1

        # Shadow: constrained EGARCH update (alpha + beta ≤ 0.98)
        c_params = self._constrained_params.get(asset)
        c_log_var = self._constrained_log_var.get(asset)
        if c_params is not None and c_log_var is not None:
            try:
                c_sigma = math.exp(c_log_var * 0.5)
                if c_sigma > 1e-12:
                    c_z = log_return / c_sigma
                    c_df = c_params.get("df")
                    c_e_abs_z = _student_t_e_abs_z(c_df) if c_df is not None else EGARCH_E_ABS_Z
                    c_raw_lv = (c_params["omega"]
                                + c_params["alpha"] * (abs(c_z) - c_e_abs_z)
                                + c_params["gamma"] * c_z
                                + c_params["beta"] * c_log_var)
                    if math.isfinite(c_raw_lv):
                        c_new_lv = max(EGARCH_LOG_VAR_FLOOR, min(EGARCH_LOG_VAR_CEILING, c_raw_lv))
                        c_new_sig = math.exp(c_new_lv * 0.5)
                        if math.isfinite(c_new_sig) and c_new_sig > 0:
                            self._constrained_log_var[asset] = c_new_lv
                            self._constrained_sigma[asset] = c_new_sig
            except Exception:
                pass  # Shadow — never crash

        return new_sigma

    def get_constrained_sigma(self, asset: str) -> Optional[float]:
        """Return shadow constrained EGARCH sigma."""
        return self._constrained_sigma.get(asset)

    def get_sigma(self, asset: str) -> Optional[float]:
        """Return current conditional σ."""
        with self._lock:
            return self._sigma.get(asset)

    def is_active(self, asset: str) -> bool:
        """Returns False if shadow mode, or no successful MLE fit yet."""
        if EGARCH_SHADOW_MODE:
            return False
        return self._mle_converged.get(asset, False)

    def maybe_refit(self):
        """Per-asset refit timers (SOL/XRP 1h, BTC/ETH 2h)."""
        now = time.time()
        any_fit = False
        for asset in ASSETS:
            interval = EGARCH_REFIT_INTERVALS.get(asset, EGARCH_REFIT_INTERVAL)
            if now - self._last_refit_per_asset.get(asset, 0) < interval:
                continue
            self._last_refit_per_asset[asset] = now
            rets = self._returns.get(asset, deque())
            if len(rets) < EGARCH_MIN_RETURNS:
                logging.info(
                    "EGARCH refit %s: SKIPPED (n_returns=%d < %d)",
                    asset, len(rets), EGARCH_MIN_RETURNS)
                continue
            returns_list = list(rets)
            if self._mle_fit_asset(asset, returns_list):
                any_fit = True
            # Shadow: constrained fit (alpha + beta ≤ 0.98)
            self._constrained_fit_asset(asset, returns_list)
        if any_fit:
            self._save_state()

    def _mle_fit_asset(self, asset: str, returns: list) -> bool:
        """Fit EGARCH(1,1) via scipy L-BFGS-B. Returns True on success."""
        try:
            from scipy.optimize import minimize
        except ImportError:
            logging.warning("EGARCH refit %s: scipy not available", asset)
            return False

        t0 = time.time()
        n = len(returns)
        sample_var = sum(r * r for r in returns) / n
        if sample_var <= 0:
            sample_var = 1e-10  # guard against log(0) when all returns are zero

        # Initial guess: previous params or heuristic (5 params: omega, alpha, gamma, beta, df)
        old_params = self._params.get(asset)
        if old_params is not None:
            x0 = [old_params["omega"], old_params["alpha"],
                   old_params["gamma"], old_params["beta"],
                   old_params.get("df", EGARCH_DF_DEFAULT)]
        else:
            x0 = [math.log(sample_var) * (1 - 0.95), 0.10, 0.0, 0.95, EGARCH_DF_DEFAULT]

        gamma_bounds = EGARCH_GAMMA_CONSTRAINTS.get(asset, EGARCH_GAMMA_BOUNDS)
        gamma_lo, gamma_hi = gamma_bounds
        if gamma_lo == gamma_hi:
            x0[2] = gamma_lo  # force initial guess when gamma is constrained
        bounds = [
            EGARCH_OMEGA_BOUNDS,
            EGARCH_ALPHA_BOUNDS,
            gamma_bounds,              # per-asset (was EGARCH_GAMMA_BOUNDS)
            EGARCH_BETA_BOUNDS,
            EGARCH_DF_BOUNDS,
        ]

        # Try Student-t first, Gaussian fallback
        df = None
        distribution = "gaussian"
        try:
            result = minimize(
                EGARCHEstimator._neg_log_likelihood_student_t,
                x0, args=(returns, EGARCH_MLE_EWL_LAMBDA),
                method="L-BFGS-B",
                bounds=bounds,
                options={"maxiter": EGARCH_MLE_MAXITER, "ftol": 1e-10},
            )
            if not result.success or not math.isfinite(result.fun):
                raise ValueError(f"Student-t failed: success={result.success} fun={result.fun}")
            omega, alpha, gamma, beta, df = result.x
            distribution = "student_t"
        except Exception as e:
            logging.warning("EGARCH refit %s: Student-t failed (%s), trying Gaussian fallback", asset, e)
            x0_gauss = x0[:4]
            bounds_gauss = bounds[:4]
            try:
                result = minimize(
                    EGARCHEstimator._neg_log_likelihood_gaussian,
                    x0_gauss, args=(returns, EGARCH_MLE_EWL_LAMBDA),
                    method="L-BFGS-B",
                    bounds=bounds_gauss,
                    options={"maxiter": EGARCH_MLE_MAXITER, "ftol": 1e-10},
                )
                if not result.success or not math.isfinite(result.fun):
                    logging.warning("EGARCH refit %s: Gaussian also failed", asset)
                    return False
                omega, alpha, gamma, beta = result.x
                df = None
                distribution = "gaussian"
            except Exception as e2:
                logging.warning("EGARCH refit %s REJECTED: both Student-t and Gaussian failed (%s)", asset, e2)
                return False

        converged = result.success

        # NLL validation
        if not math.isfinite(result.fun):
            logging.warning("EGARCH refit %s REJECTED: NLL=%s is not finite", asset, result.fun)
            return False

        # Sanity check: unconditional log-var
        if abs(beta) >= 1.0:
            logging.warning(
                "EGARCH refit %s REJECTED: reason=beta>=1.0 (%.6f)", asset, beta)
            return False
        uncond_log_var = omega / (1.0 - beta)
        if uncond_log_var < EGARCH_LOG_VAR_FLOOR or uncond_log_var > EGARCH_LOG_VAR_CEILING:
            logging.warning(
                "EGARCH refit %s REJECTED: reason=uncond_log_var out of bounds (%.4f)",
                asset, uncond_log_var)
            return False

        # Parameter change detection
        if old_params is not None:
            d_omega = omega - old_params["omega"]
            d_alpha = alpha - old_params["alpha"]
            d_gamma = gamma - old_params["gamma"]
            d_beta = beta - old_params["beta"]
            logging.info(
                "EGARCH %s param delta: Δω=%.4f Δα=%.4f Δγ=%.4f Δβ=%.6f",
                asset, d_omega, d_alpha, d_gamma, d_beta)

        # Update params (lock protects concurrent reads from dashboard snapshot)
        new_params = {"omega": omega, "alpha": alpha, "gamma": gamma, "beta": beta}
        if df is not None:
            new_params["df"] = df
        with self._lock:
            self._params[asset] = new_params
            self._mle_loglik[asset] = -result.fun
            self._mle_converged[asset] = converged
            self._log_var[asset] = uncond_log_var
            self._sigma[asset] = math.exp(uncond_log_var * 0.5)

        uncond_vol = math.exp(uncond_log_var * 0.5)
        half_life = (math.log(2) / (-math.log(beta))) * 5.0 if beta > 0 and beta < 1 else float('inf')
        elapsed_ms = (time.time() - t0) * 1000

        # Gamma sign interpretation
        if gamma > 0.01:
            gamma_sign = "positive=inverse_leverage"
        elif gamma < -0.01:
            gamma_sign = "negative=classic_leverage"
        else:
            gamma_sign = "near_zero"

        if df is not None:
            e_abs_z_val = _student_t_e_abs_z(df)
            logging.info(
                "EGARCH refit %s: omega=%.4f alpha=%.4f gamma=%.4f beta=%.4f df=%.2f "
                "E[|z|]=%.4f dist=%s loglik=%.2f uncond_vol=%.8f half_life=%.1fs converged=%s n=%d elapsed_ms=%.1f",
                asset, omega, alpha, gamma, beta, df,
                e_abs_z_val, distribution, -result.fun, uncond_vol, half_life, converged, n, elapsed_ms)
        else:
            logging.info(
                "EGARCH refit %s: omega=%.4f alpha=%.4f gamma=%.4f beta=%.4f "
                "dist=gaussian loglik=%.2f uncond_vol=%.8f half_life=%.1fs converged=%s n=%d elapsed_ms=%.1f",
                asset, omega, alpha, gamma, beta,
                -result.fun, uncond_vol, half_life, converged, n, elapsed_ms)
        logging.info("EGARCH %s gamma sign: %s", asset, gamma_sign)

        return True

    def _constrained_fit_asset(self, asset: str, returns: list):
        """Shadow: fit EGARCH with alpha + beta ≤ 0.98 constraint. Never affects live."""
        try:
            from scipy.optimize import minimize

            n = len(returns)
            if n < EGARCH_MIN_RETURNS:
                return
            sample_var = sum(r * r for r in returns) / n
            if sample_var <= 0:
                sample_var = 1e-10

            x0 = [math.log(sample_var) * (1 - 0.90), 0.08, 0.0, 0.90]
            gamma_bounds = EGARCH_GAMMA_CONSTRAINTS.get(asset, EGARCH_GAMMA_BOUNDS)
            # Key constraint: beta upper = 0.98 - alpha_lower = 0.97
            bounds = [
                EGARCH_OMEGA_BOUNDS,
                (0.01, 0.20),       # tighter alpha
                gamma_bounds,
                (0.80, 0.97),       # tighter beta — ensures alpha + beta ≤ ~0.98
            ]
            result = minimize(
                EGARCHEstimator._neg_log_likelihood_gaussian,
                x0, args=(returns, EGARCH_MLE_EWL_LAMBDA),
                method="L-BFGS-B", bounds=bounds,
                options={"maxiter": EGARCH_MLE_MAXITER, "ftol": 1e-10},
            )
            if not result.success:
                return
            omega, alpha, gamma, beta = result.x
            if abs(beta) >= 1.0 or alpha + beta >= 0.99:
                return
            uncond_log_var = omega / (1.0 - beta)
            if uncond_log_var < EGARCH_LOG_VAR_FLOOR or uncond_log_var > EGARCH_LOG_VAR_CEILING:
                return

            new_params = {"omega": omega, "alpha": alpha, "gamma": gamma, "beta": beta}
            self._constrained_params[asset] = new_params
            self._constrained_log_var[asset] = uncond_log_var
            self._constrained_sigma[asset] = math.exp(uncond_log_var * 0.5)

            half_life = (math.log(2) / (-math.log(beta))) * 5.0 if beta < 1 else float('inf')
            logging.info(
                "EGARCH_CONSTRAINED %s: omega=%.4f alpha=%.4f gamma=%.4f beta=%.4f "
                "persistence=%.4f half_life=%.1fs sigma=%.6f",
                asset, omega, alpha, gamma, beta,
                alpha + beta, half_life, self._constrained_sigma[asset])
        except Exception as e:
            logging.debug("EGARCH constrained fit %s failed: %s", asset, e)

    @staticmethod
    def _neg_log_likelihood_student_t(params, returns, ewl_lambda=1.0) -> float:
        """Negative log-likelihood for EGARCH(1,1) with Student-t innovations.

        When ewl_lambda < 1, applies exponential weighting: recent observations
        weighted more heavily (lambda^(n-1-i)), smoothing ghost features from
        hard rolling windows.
        """
        omega, alpha, gamma, beta, df = params
        n = len(returns)
        if n < 60:
            return 1e10
        if df < 3.0:
            return 1e10  # df<3 → infinite variance — reject

        sample_var = sum(r * r for r in returns[:60]) / 60.0
        if sample_var <= 0:
            sample_var = 1e-10
        log_var = math.log(sample_var)

        # Student-t constants (precompute once per NLL evaluation)
        half_dfp1 = (df + 1.0) / 2.0
        half_df = df / 2.0
        try:
            log_const = (math.lgamma(half_dfp1) - math.lgamma(half_df)
                         - 0.5 * math.log(math.pi * (df - 2.0)))
        except (ValueError, OverflowError):
            return 1e10
        e_abs_z = _student_t_e_abs_z(df)

        # Precompute exponential weights if lambda < 1
        use_ewl = ewl_lambda < 1.0
        if use_ewl:
            weights = [ewl_lambda ** (n - 1 - i) for i in range(n)]
            w_sum = sum(weights)
        else:
            w_sum = float(n)

        nll = 0.0
        for i in range(n):
            r = returns[i]
            var = math.exp(log_var)
            if var <= 0:
                var = 1e-30
            # Student-t log-likelihood contribution
            ll_i = (-log_const + 0.5 * log_var
                    + half_dfp1 * math.log(1.0 + r * r / (var * (df - 2.0))))
            nll += (weights[i] * ll_i) if use_ewl else ll_i

            # EGARCH recursion with Student-t E[|z|]
            sigma = math.sqrt(var)
            if sigma <= 0:
                sigma = 1e-15
            z = r / sigma
            log_var = omega + alpha * (abs(z) - e_abs_z) + gamma * z + beta * log_var
            log_var = max(EGARCH_LOG_VAR_FLOOR, min(EGARCH_LOG_VAR_CEILING, log_var))

        return nll / w_sum

    @staticmethod
    def _neg_log_likelihood_gaussian(params, returns, ewl_lambda=1.0) -> float:
        """Negative log-likelihood for EGARCH(1,1) with Gaussian innovations (fallback)."""
        omega, alpha, gamma, beta = params
        n = len(returns)
        if n < 60:
            return 1e10

        # Init log_var from sample variance of first 60 returns
        sample_var = sum(r * r for r in returns[:60]) / 60.0
        if sample_var <= 0:
            sample_var = 1e-10
        log_var = math.log(sample_var)

        LOG_2PI = 1.8378770664093453  # log(2π)
        e_abs_z = EGARCH_E_ABS_Z

        # Precompute exponential weights if lambda < 1
        use_ewl = ewl_lambda < 1.0
        if use_ewl:
            weights = [ewl_lambda ** (n - 1 - i) for i in range(n)]
            w_sum = sum(weights)
        else:
            w_sum = float(n)

        nll = 0.0
        for i in range(n):
            r = returns[i]
            # NLL contribution: 0.5 * (log(2π) + log_var + r²/exp(log_var))
            var = math.exp(log_var)
            if var <= 0:
                var = 1e-30
            ll_i = 0.5 * (LOG_2PI + log_var + r * r / var)
            nll += (weights[i] * ll_i) if use_ewl else ll_i

            # EGARCH recursion
            sigma = math.sqrt(var)
            if sigma <= 0:
                sigma = 1e-15
            z = r / sigma
            log_var = omega + alpha * (abs(z) - e_abs_z) + gamma * z + beta * log_var
            log_var = max(EGARCH_LOG_VAR_FLOOR, min(EGARCH_LOG_VAR_CEILING, log_var))

        return nll / w_sum

    def _load_state(self):
        """Load params and state from JSON file."""
        if not os.path.exists(EGARCH_STATE_PATH):
            logging.info("EGARCH loaded: 0 active, no state file")
            return
        try:
            with open(EGARCH_STATE_PATH, "r") as f:
                state = json.load(f)
            active_count = 0
            now = time.time()
            oldest_age = 0.0
            for asset in ASSETS:
                adata = state.get(asset)
                if adata and adata.get("params"):
                    self._params[asset] = adata["params"]
                    self._log_var[asset] = adata.get("log_var")
                    self._sigma[asset] = adata.get("sigma")
                    self._n_updates[asset] = adata.get("n_updates", 0)
                    self._mle_loglik[asset] = adata.get("mle_loglik")
                    self._mle_converged[asset] = adata.get("mle_converged", False)
                    active_count += 1
                    logging.info(
                        "EGARCH %s: restored params omega=%.4f alpha=%.4f "
                        "gamma=%.4f beta=%.4f log_var=%.4f",
                        asset,
                        adata["params"]["omega"], adata["params"]["alpha"],
                        adata["params"]["gamma"], adata["params"]["beta"],
                        adata.get("log_var", 0))
                # Restore return buffer
                if adata:
                    try:
                        for r in adata.get("returns", []):
                            self._returns[asset].append(r)
                    except Exception as re:
                        logging.warning("EGARCH returns load failed for %s: %s (starting fresh)", asset, re)
                        self._returns[asset].clear()
            self._last_refit = state.get("last_refit", 0.0)
            # Restore per-asset refit times (backward compat: migrate from single timestamp)
            if "last_refit_per_asset" in state:
                self._last_refit_per_asset = state["last_refit_per_asset"]
            elif self._last_refit > 0:
                for a in ASSETS:
                    self._last_refit_per_asset[a] = self._last_refit
            age = time.time() - self._last_refit if self._last_refit > 0 else float('inf')
            logging.info("EGARCH loaded: %d active, state_age=%.0fs", active_count, age)
            # Log restored return counts
            ret_counts = {a: len(self._returns[a]) for a in ASSETS}
            if any(ret_counts.values()):
                logging.info(
                    "EGARCH returns restored: %s",
                    ", ".join(f"{a}={ret_counts[a]}" for a in ASSETS))
            # Warm-start: replay last N returns through recursive_update to rebuild
            # sigma from actual return history. Without this, sigma drops to ~50% of
            # steady state after restart because the first new live returns are small.
            # The persisted log_var is used as the starting point, and replaying the
            # saved returns re-derives the correct sigma trajectory.
            WARMSTART_RETURNS = 180  # ~15 min of 5s returns — enough to rebuild sigma
            for asset in ASSETS:
                rets = list(self._returns[asset])
                if not rets or self._params.get(asset) is None or self._log_var.get(asset) is None:
                    continue
                replay = rets[-WARMSTART_RETURNS:]
                pre_sigma = self._sigma.get(asset, 0)
                for r in replay:
                    self.recursive_update(asset, r)
                post_sigma = self._sigma.get(asset, 0)
                logging.info(
                    "EGARCH %s: warm-start replayed %d returns (sigma %.6f -> %.6f)",
                    asset, len(replay), pre_sigma or 0, post_sigma or 0)
        except Exception as e:
            logging.warning("EGARCH state load failed: %s", e)

    def _save_state(self):
        """Save state and return buffers to JSON file (atomic write)."""
        state = {
            "version": 2,
            "last_refit": self._last_refit,
            "last_refit_per_asset": self._last_refit_per_asset,
        }
        for asset in ASSETS:
            state[asset] = {
                "params": self._params[asset],
                "log_var": self._log_var[asset],
                "sigma": self._sigma[asset],
                "n_updates": self._n_updates[asset],
                "mle_loglik": self._mle_loglik[asset],
                "mle_converged": self._mle_converged[asset],
                "returns": list(self._returns[asset]),
            }
        tmp_path = EGARCH_STATE_PATH + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_path, EGARCH_STATE_PATH)
        except Exception as e:
            logging.warning("EGARCH state save failed: %s", e)

    def get_diagnostics(self) -> Dict:
        """Per-asset diagnostics dict for dashboard (thread-safe)."""
        result = {}
        now = time.time()
        for asset in ASSETS:
            with self._lock:
                params = self._params.get(asset)
                log_var = self._log_var.get(asset)
                sigma = self._sigma.get(asset)
                mle_ll = self._mle_loglik.get(asset)
                mle_conv = self._mle_converged.get(asset, False)
            n_rets = len(self._returns.get(asset, []))
            n_upd = self._n_updates.get(asset, 0)

            diag: Dict = {
                "has_params": params is not None,
                "n_returns": n_rets,
                "n_updates": n_upd,
                "current_sigma": round(sigma, 10) if sigma is not None else None,
                "current_log_var": round(log_var, 4) if log_var is not None else None,
                "mle_loglik": round(mle_ll, 4) if mle_ll is not None else None,
                "mle_converged": mle_conv,
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
                diag["distribution"] = "student_t" if "df" in params else "gaussian"
                if "df" in params:
                    diag["student_t_df"] = round(params["df"], 2)
                    diag["student_t_e_abs_z"] = round(_student_t_e_abs_z(params["df"]), 6)
                diag["refit_interval_s"] = EGARCH_REFIT_INTERVALS.get(asset, EGARCH_REFIT_INTERVAL)
            else:
                diag["params"] = None
                diag["unconditional_vol"] = None
                diag["half_life_seconds"] = None
                diag["asymmetry_gamma"] = None

            result[asset] = diag
        return result


# ── QLIKE loss (standalone utility) ──────────────────────────────────────────

def _compute_qlike(y_actual: List[float], y_predicted: List[float]) -> float:
    """QLIKE loss: mean(actual/predicted - log(actual/predicted) - 1).

    Both in variance scale. Handles zero/negative with penalty.
    """
    eps = 1e-20
    total = 0.0
    n = 0
    for a, p in zip(y_actual, y_predicted):
        a = max(eps, a)
        p = max(eps, p)
        ratio = a / p
        total += ratio - math.log(ratio) - 1.0
        n += 1
    return total / max(n, 1)


# ═════════════════════════════════════════════════════════════════════════════
#  MincerZarnowitzTracker – Rolling R² for EGARCH forecast evaluation
# ═════════════════════════════════════════════════════════════════════════════

class MincerZarnowitzTracker:
    """Rolling Mincer-Zarnowitz R² for EGARCH forecast evaluation.

    Regression: σ²_realized = α + β·σ²_forecast + ε
    R² measures how well EGARCH forecasts explain realized variance.
    Higher R² → more weight to EGARCH in the blend.

    Also tracks QLIKE loss for shadow evaluation.
    """

    def __init__(self):
        # Rolling buffers: (forecast_var, realized_var) pairs
        self._pairs: Dict[str, deque] = {
            a: deque(maxlen=MZ_WINDOW) for a in ASSETS
        }
        self._r_squared: Dict[str, Optional[float]] = {a: None for a in ASSETS}
        self._qlike: Dict[str, Optional[float]] = {a: None for a in ASSETS}
        self._last_recompute: Dict[str, float] = {a: 0.0 for a in ASSETS}
        self._egarch_weight: Dict[str, float] = {a: EGARCH_WEIGHT_DEFAULT for a in ASSETS}
        self._prev_weight: Dict[str, Optional[float]] = {a: None for a in ASSETS}
        # Shadow sigmoid QLIKE tracking
        self._baseline_qlike: Dict[str, Optional[float]] = {a: None for a in ASSETS}
        self._shadow_sigmoid_w: Dict[str, Optional[float]] = {a: None for a in ASSETS}
        self._load_state()

    def record(self, asset: str, egarch_var: float, realized_var: float):
        """Record a (forecast, realized) variance pair."""
        if egarch_var <= 0 or realized_var <= 0:
            return
        self._pairs[asset].append((egarch_var, realized_var))

    def maybe_recompute(self, asset: str, now: float) -> float:
        """Recompute R² and weight if enough time has passed. Returns current weight."""
        if now - self._last_recompute.get(asset, 0) < MZ_RECOMPUTE_INTERVAL:
            return self._egarch_weight[asset]

        self._last_recompute[asset] = now
        pairs = list(self._pairs[asset])
        n = len(pairs)

        if n < MZ_MIN_OBS:
            self._egarch_weight[asset] = EGARCH_WEIGHT_DEFAULT
            return EGARCH_WEIGHT_DEFAULT

        forecasts = [p[0] for p in pairs]
        actuals = [p[1] for p in pairs]

        # OLS: actual = alpha + beta * forecast
        mean_f = sum(forecasts) / n
        mean_a = sum(actuals) / n
        cov_fa = sum((f - mean_f) * (a - mean_a) for f, a in zip(forecasts, actuals)) / n
        var_f = sum((f - mean_f) ** 2 for f in forecasts) / n
        var_a = sum((a - mean_a) ** 2 for a in actuals) / n

        if var_f < 1e-30 or var_a < 1e-30:
            self._r_squared[asset] = 0.0
            self._egarch_weight[asset] = EGARCH_WEIGHT_DEFAULT
            return EGARCH_WEIGHT_DEFAULT

        r_sq = (cov_fa ** 2) / (var_f * var_a)
        r_sq = max(0.0, min(1.0, r_sq))
        self._r_squared[asset] = round(r_sq, 4)

        # Map R² to weight within asset-specific bounds (BEFORE QLIKE so weight is always set)
        lo, hi = EGARCH_WEIGHT_BOUNDS.get(asset, (0.05, 0.25))
        raw_w = lo + r_sq * (hi - lo)
        w = raw_w

        # EMA smoothing (Stock & Watson 2004): dampens weight oscillation
        prev_w = self._prev_weight.get(asset)
        if prev_w is not None:
            w = MZ_EMA_LAMBDA * prev_w + (1.0 - MZ_EMA_LAMBDA) * w
        w = round(w, 4)
        self._prev_weight[asset] = w

        # Equal-weight fallback: if R² too low, EGARCH forecasts are noise
        if r_sq < MZ_EQUAL_WEIGHT_R2_THRESHOLD:
            w = round((lo + hi) / 2.0, 4)
            logging.info("MZ %s: R²=%.4f < %.2f, using equal-weight fallback w=%.4f",
                         asset, r_sq, MZ_EQUAL_WEIGHT_R2_THRESHOLD, w)

        self._egarch_weight[asset] = w

        logging.info(
            "MZ %s: R²=%.4f QLIKE=%.4f raw_w=%.4f ema_w=%.4f final_w=%.4f (prev=%.4f, fallback=%s)",
            asset, r_sq, self._qlike[asset] or 0, round(raw_w, 4), round(self._prev_weight[asset], 4),
            self._egarch_weight[asset], prev_w or 0,
            r_sq < MZ_EQUAL_WEIGHT_R2_THRESHOLD,
        )

        # QLIKE for shadow evaluation
        try:
            self._qlike[asset] = round(_compute_qlike(actuals, forecasts), 6)
        except Exception:
            logging.warning("MZ tracker: QLIKE computation failed for %s", asset, exc_info=True)

        # Shadow sigmoid QLIKE-ratio weight mapping
        if MZ_SIGMOID_SHADOW_MODE:
            try:
                egarch_qlike = self._qlike.get(asset)
                if egarch_qlike is not None and n >= MZ_MIN_OBS:
                    # Baseline QLIKE: RV-only forecast (each forecast = previous realized var)
                    # This gives the "no-model" QLIKE score
                    rv_forecasts = actuals[:-1]  # lag-1 realized var as forecast
                    rv_actuals = actuals[1:]
                    if len(rv_forecasts) >= MZ_MIN_OBS:
                        baseline_q = _compute_qlike(rv_actuals, rv_forecasts)
                        self._baseline_qlike[asset] = round(baseline_q, 6)

                        # Improvement ratio: how much better EGARCH is vs naive
                        if baseline_q > 1e-10:
                            improvement = max(0.0, (baseline_q - egarch_qlike) / baseline_q)
                            # Sigmoid: w = w_max * sigmoid(kappa * (improvement - q_mid))
                            sigmoid_arg = MZ_SIGMOID_KAPPA * (improvement - MZ_SIGMOID_Q_MID)
                            sigmoid_arg = max(-20.0, min(20.0, sigmoid_arg))  # clamp for exp
                            sigmoid_val = 1.0 / (1.0 + math.exp(-sigmoid_arg))
                            self._shadow_sigmoid_w[asset] = round(MZ_SIGMOID_W_MAX * sigmoid_val, 6)
                        else:
                            self._shadow_sigmoid_w[asset] = 0.0
            except Exception:
                logging.debug("MZ sigmoid QLIKE failed for %s", asset, exc_info=True)

        return self._egarch_weight[asset]

    def get_weight(self, asset: str) -> float:
        return self._egarch_weight.get(asset, EGARCH_WEIGHT_DEFAULT)

    def _load_state(self):
        try:
            with open(EGARCH_BLEND_STATE_PATH, "r") as f:
                state = json.load(f)
            for asset in ASSETS:
                if asset in state.get("r_squared", {}):
                    self._r_squared[asset] = state["r_squared"][asset]
                if asset in state.get("weights", {}):
                    self._egarch_weight[asset] = state["weights"][asset]
                if asset in state.get("prev_weights", {}):
                    self._prev_weight[asset] = state["prev_weights"][asset]
                if asset in state.get("pairs", {}):
                    for p in state["pairs"][asset][-MZ_WINDOW:]:
                        self._pairs[asset].append(tuple(p))
                if asset in state.get("baseline_qlike", {}):
                    self._baseline_qlike[asset] = state["baseline_qlike"][asset]
                if asset in state.get("shadow_sigmoid_w", {}):
                    self._shadow_sigmoid_w[asset] = state["shadow_sigmoid_w"][asset]
            logging.info("MZ tracker state loaded: R²=%s weights=%s",
                         self._r_squared, self._egarch_weight)
        except (FileNotFoundError, json.JSONDecodeError):
            logging.info("MZ tracker: no saved state, starting fresh")

    def save_state(self):
        try:
            state = {
                "version": 2,
                "r_squared": self._r_squared,
                "weights": self._egarch_weight,
                "prev_weights": {a: self._prev_weight[a] for a in ASSETS},
                "qlike": self._qlike,
                "pairs": {a: list(self._pairs[a])[-MZ_WINDOW:] for a in ASSETS},
                "baseline_qlike": self._baseline_qlike,
                "shadow_sigmoid_w": self._shadow_sigmoid_w,
            }
            tmp_path = EGARCH_BLEND_STATE_PATH + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(state, f)
            os.replace(tmp_path, EGARCH_BLEND_STATE_PATH)
        except Exception:
            logging.warning("MZ tracker: save_state failed", exc_info=True)


# ═════════════════════════════════════════════════════════════════════════════
#  PositionSizer
# ═════════════════════════════════════════════════════════════════════════════

class PositionSizer:
    """Edge-tiered position sizing with drawdown scaling.

    Sizing tiers (from SIZING_TIERS, fee-adjusted edge):
        fee-adj edge ≥ 4.0% → risk 25% of bankroll
        fee-adj edge ≥ 2.0% → risk 20% of bankroll
        fee-adj edge ≥ 1.5% → risk 15% of bankroll
        fee-adj edge ≥ 1.0% → risk 10% of bankroll

    Contracts = floor(bankroll × risk_fraction / price).

    Hard cap: MAX_RISK_PER_TRADE of bankroll (safety ceiling).
    Drawdown scaler: halves below 92%, quarters below 85%.
    """

    def __init__(self, starting_balance_cents: int = 0):
        self.starting_balance_cents = starting_balance_cents
        self._balance_history: deque = deque(maxlen=60480)  # 7 days at 10s intervals
        self._override_hwm_cents: Optional[int] = None
        # HWM warmup: don't trust the first few balance readings after restart.
        # Kalshi API sometimes returns inflated values (pending order exposure).
        # Collect 5 readings, use median to set initial HWM.
        self._hwm_warmup_readings: list = []
        self._hwm_initialized: bool = False
        self._HWM_WARMUP_COUNT = 5
        # Consecutive spike rejection counter (for alerting)
        self._consecutive_spike_rejections: int = 0
        # Check env var for manual HWM override (dollars)
        override = os.environ.get("OVERRIDE_HWM")
        if override:
            try:
                self._override_hwm_cents = int(float(override) * 100)
                logging.info("HWM override: $%.2f (%d cents)", float(override), self._override_hwm_cents)
            except ValueError:
                logging.warning("Invalid OVERRIDE_HWM value: %s", override)

    def compute(self, win_prob: float, price_cents: int,
                balance_cents: int) -> Dict:
        """Compute position size.

        Returns dict with: contracts, kelly_f, raw_contracts, drawdown_scaler, reason
        """
        result: Dict = {
            "contracts": 0,
            "kelly_f": 0.0,
            "raw_contracts": 0,
            "drawdown_scaler": 1.0,
            "reason": "",
        }

        if price_cents <= 0 or price_cents >= 100:
            result["reason"] = "invalid price"
            return result

        if balance_cents <= 0:
            result["reason"] = "no balance"
            return result

        # Compute fee-adjusted edge
        fee_1c = calculate_taker_fee(1, price_cents)
        b = (100 - price_cents - fee_1c) / (price_cents + fee_1c)
        if b <= 0:
            result["reason"] = "zero payout after fees"
            return result
        p = win_prob
        q = 1.0 - p
        kelly_edge = (b * p - q) / b
        result["kelly_f"] = round(kelly_edge, 6)

        if kelly_edge <= 0:
            result["reason"] = "negative edge (Kelly <= 0)"
            return result

        # Edge-based tier selection: higher edge → larger risk fraction
        # Use fee-adjusted edge (consistent with scanner MIN_EDGE_PCT filter)
        edge = win_prob - price_cents / 100.0 - fee_1c / 100.0
        risk_fraction = 0.0
        for min_edge, frac in SIZING_TIERS:
            if edge >= min_edge:
                risk_fraction = frac
                break

        if risk_fraction <= 0:
            result["reason"] = "edge below minimum tier"
            return result

        # Contracts = floor(bankroll × risk_fraction / price)
        raw_contracts = math.floor((balance_cents * risk_fraction) / price_cents)
        result["raw_contracts"] = raw_contracts

        if raw_contracts <= 0:
            result["reason"] = "risk budget rounds to 0 contracts"
            return result

        # Apply drawdown scaler
        scaler = self._drawdown_scaler(balance_cents)
        result["drawdown_scaler"] = scaler
        if scaler <= 0:
            result["reason"] = "drawdown halt — trading suspended"
            return result
        scaled_contracts = math.floor(raw_contracts * scaler)

        # Safety ceiling: MAX_RISK_PER_TRADE of bankroll
        max_by_risk = int((balance_cents * MAX_RISK_PER_TRADE) / price_cents)

        if max_by_risk < 1:
            result["reason"] = "balance too small for 1 contract within risk limit"
            return result

        contracts = min(scaled_contracts, max_by_risk)

        result["contracts"] = contracts
        result["reason"] = "ok"
        logging.info(
            "sizing_decision: edge=%.4f fee_adj=%.4f tier_frac=%.2f "
            "balance=$%.2f raw_contracts=%d final_contracts=%d",
            win_prob - price_cents / 100.0, edge, risk_fraction,
            balance_cents / 100, raw_contracts, contracts)
        return result

    def record_balance(self, balance_cents: int):
        """Record FULL portfolio balance for rolling HWM computation.

        IMPORTANT: Only call with the FULL portfolio balance (available cash +
        open position exposure). Never call with fractional bankroll amounts
        (e.g., hourly's 10% or SPX's 15%). Fractional values poison the spike
        rejection history and permanently break HWM tracking.
        (Learned: fractional bankroll from hourly/SPX ratcheted 'last' down to
        ~$118, causing real $1,191 balance to be rejected as +910% spike for 18h.
        Mar 25-26 2026.)

        Call once per scan cycle from _tick(), NOT from compute()/_drawdown_scaler().
        """
        if balance_cents <= 0:
            return  # Skip bad readings

        # Warmup: collect first N readings, use median to initialize HWM
        if not self._hwm_initialized:
            self._hwm_warmup_readings.append(balance_cents)
            if len(self._hwm_warmup_readings) >= self._HWM_WARMUP_COUNT:
                sorted_readings = sorted(self._hwm_warmup_readings)
                median_balance = sorted_readings[len(sorted_readings) // 2]
                self._balance_history.append((time.time(), median_balance))
                self._hwm_initialized = True
                logging.info(
                    "HWM warmup complete: median=%dc ($%.2f) from readings %s",
                    median_balance, median_balance / 100,
                    [f"${r/100:.2f}" for r in sorted_readings])
            return  # Don't record individual warmup readings

        # Floor guard: reject readings < 50% of current HWM (likely bad API read
        # or fractional bankroll leaking through)
        if self._balance_history:
            hwm = self.get_rolling_hwm()
            if hwm > 0 and balance_cents < hwm * 0.50:
                logging.warning(
                    "DRAWDOWN: balance floor guard %dc < 50%% of HWM %dc — skipping "
                    "(possible fractional bankroll or bad API read)",
                    balance_cents, hwm)
                return

        # Spike rejection: skip readings >20% above the last recorded value
        # EXCEPTION 1: allow recovery near HWM (settlement timing can crash balance
        # temporarily, then recovery is rejected as "spike" and history gets stuck).
        # (Learned: $1,377→$1,051→$1,400 recovery rejected, ds=0.50 stuck permanently. Mar 30 2026.)
        # EXCEPTION 2: after 60 consecutive rejections (~1 min), accept the reading.
        # A sustained "spike" is reality, not noise. Without this, balance history
        # gets permanently stuck and drawdown_scaler locks at 0.10 indefinitely.
        # (Learned: 204+ consecutive rejections, $381→$837 stuck for hours, Apr 13 2026.)
        _SPIKE_MAX_CONSECUTIVE = 60
        if self._balance_history:
            _, last_balance = self._balance_history[-1]
            if last_balance > 0 and balance_cents > last_balance * 1.20:
                # Allow if reading is within 10% of rolling HWM (returning to known-good level)
                hwm = self.get_rolling_hwm()
                if hwm > 0 and balance_cents <= hwm * 1.10:
                    logging.info(
                        "DRAWDOWN: spike guard BYPASSED — recovery to %dc near HWM %dc "
                        "(ratio=%.2f, last=%dc +%.0f%%)",
                        balance_cents, hwm, balance_cents / hwm,
                        last_balance, (balance_cents - last_balance) / last_balance * 100)
                elif self._consecutive_spike_rejections >= _SPIKE_MAX_CONSECUTIVE:
                    logging.warning(
                        "DRAWDOWN: spike guard FORCE-ACCEPT after %d consecutive rejections — "
                        "%dc vs last %dc (+%.0f%%), hwm=%dc. Accepting as new reality.",
                        self._consecutive_spike_rejections,
                        balance_cents, last_balance,
                        (balance_cents - last_balance) / last_balance * 100,
                        hwm if hwm > 0 else 0)
                else:
                    self._consecutive_spike_rejections += 1
                    logging.warning(
                        "DRAWDOWN: balance spike %dc vs last %dc (+%.0f%%) — skipping "
                        "(consecutive=%d, hwm=%dc)",
                        balance_cents, last_balance,
                        (balance_cents - last_balance) / last_balance * 100,
                        self._consecutive_spike_rejections, hwm if hwm > 0 else 0)
                    return

        self._consecutive_spike_rejections = 0
        self._balance_history.append((time.time(), balance_cents))

    def get_rolling_hwm(self) -> int:
        """Get high-water mark: max balance over last 7 days (or env override)."""
        if self._override_hwm_cents is not None:
            return self._override_hwm_cents
        if not self._balance_history:
            return self.starting_balance_cents
        cutoff = time.time() - HWM_LOOKBACK_SECONDS
        recent = [b for t, b in self._balance_history if t >= cutoff]
        if not recent:
            # All entries older than 7 days — use most recent
            return self._balance_history[-1][1]
        return max(recent)

    def _drawdown_scaler(self, balance_cents: int) -> float:
        """Scale position based on drawdown from rolling 7-day peak HWM.

        READ-ONLY: does NOT call record_balance(). The caller (_tick) must call
        record_balance() once per cycle with the FULL portfolio balance.

        IMPORTANT: The ratio is computed from the RECORDED portfolio balance
        (from _balance_history), NOT from the balance_cents parameter. The
        balance_cents param may be a product-level fractional bankroll (hourly
        10%, SPX 15%, or available cash with positions open). Using it for the
        ratio would falsely trigger drawdown halt.
        (Learned: SOL trade sized to 5 instead of 160 because available cash
        $400 vs HWM $1,117 gave ratio=0.358 → halt floor. Mar 27 2026.)
        """
        # Guard: if balance fetch failed (0 or negative), don't halt
        if balance_cents <= 0:
            return 1.0
        # During warmup, no drawdown scaling — just restarted, no drawdown possible
        if not self._hwm_initialized:
            return 1.0
        hwm = self.get_rolling_hwm()
        if hwm <= 0:
            return 1.0
        # Keep starting_balance_cents in sync for backward compat (dashboard reads it)
        self.starting_balance_cents = hwm
        # Use RECORDED portfolio balance for ratio, not the passed sizing balance.
        # balance_cents may be fractional (hourly 10%, SPX 15%) or available cash
        # (excluding open position margin). The ratio must compare portfolio-level
        # values on both sides.
        if self._balance_history:
            _, portfolio_balance = self._balance_history[-1]
        else:
            portfolio_balance = balance_cents  # fallback if no history yet
        ratio = portfolio_balance / hwm
        if ratio < DRAWDOWN_HALT_THRESHOLD:
            # Floor: never fully halt. Even during drawdown, place minimum-size trades
            # so the system can recover. The HWM can get inflated by API jitter,
            # causing false halts on a healthy balance (e.g., $927 vs $1427 phantom HWM).
            logging.warning(
                "DRAWDOWN_SCALER: ratio=%.3f < halt=%.2f (balance=%dc hwm=%dc) — "
                "using floor=0.10 instead of halt",
                ratio, DRAWDOWN_HALT_THRESHOLD, balance_cents, hwm)
            return 0.10   # was 0.0 — floor prevents complete lockout
        if ratio < DRAWDOWN_QUARTER_THRESHOLD:
            return 0.25
        if ratio < DRAWDOWN_HALF_THRESHOLD:
            return 0.5
        return 1.0

    def _drawdown_scaler_readonly(self, balance_cents: int) -> float:
        """Read-only version for dashboard display. Does NOT update HWM."""
        hwm = self.get_rolling_hwm()
        if hwm <= 0:
            return 1.0
        if self._balance_history:
            _, portfolio_balance = self._balance_history[-1]
        else:
            portfolio_balance = balance_cents
        ratio = portfolio_balance / hwm
        if ratio < DRAWDOWN_HALT_THRESHOLD:
            return 0.0
        if ratio < DRAWDOWN_QUARTER_THRESHOLD:
            return 0.25
        if ratio < DRAWDOWN_HALF_THRESHOLD:
            return 0.5
        return 1.0
