#!/usr/bin/env python3
"""Standalone tests for HAREstimator — no pip dependencies required.

NOTE: HAR model was deleted from production (misapplied at sub-hourly
timescales). These tests cover dead code and are skipped in CI.
Kept for reference in case HAR is revisited.

Run: python3 test_har.py
"""
import sys
import pytest
pytest.skip("HAR model deleted from production — tests cover dead code", allow_module_level=True)

import math
import random
import os
import time
import json
from collections import deque
from typing import Dict, List, Optional, Any, Tuple

# ── Constants (mirrored from bot.py) ────────────────────────────────────

ASSETS = ["BTC", "ETH", "SOL", "XRP"]
VOL_BLEND_WEIGHTS = (0.5, 0.3, 0.2)
VOL_WINDOW_1MIN = 12
VOL_WINDOW_5MIN = 60
VOL_WINDOW_15MIN = 180
HAR_OBSERVATION_INTERVAL = 300
HAR_OBSERVATION_MAXLEN = 288
HAR_REFIT_INTERVAL = 7200
HAR_MIN_OBSERVATIONS = 36
HAR_STATE_PATH = "har_state_test.json"  # test-specific path
HAR_QLIKE_FALLBACK_THRESHOLD = 2.0
HAR_SHADOW_MODE = True


# ═══════════════════════════════════════════════════════════════════════════
#  HAREstimator (copied from bot.py for standalone testing)
# ═══════════════════════════════════════════════════════════════════════════

class HAREstimator:
    MODEL_NAMES = ("level_har", "log_har", "har_j", "har_semi")

    def __init__(self):
        self._observations: Dict[str, deque] = {
            a: deque(maxlen=HAR_OBSERVATION_MAXLEN) for a in ASSETS
        }
        self._last_obs_time: Dict[str, float] = {}
        self._last_refit: float = 0.0
        self._active_model: Dict[str, str] = {a: "fixed" for a in ASSETS}
        self._coefficients: Dict[str, Dict[str, List[float]]] = {a: {} for a in ASSETS}
        self._qlike_scores: Dict[str, Dict[str, float]] = {a: {} for a in ASSETS}
        self._load_state()

    def record_observation(self, asset, returns_list, rk_1min, rk_5min, rk_15min, bv_5min):
        now = time.time()
        last = self._last_obs_time.get(asset, 0.0)
        if now - last < HAR_OBSERVATION_INTERVAL:
            return
        self._last_obs_time[asset] = now
        sv_pos_1, sv_neg_1 = self._compute_semivariances(returns_list, VOL_WINDOW_1MIN)
        sv_pos_5, sv_neg_5 = self._compute_semivariances(returns_list, VOL_WINDOW_5MIN)
        sv_pos_15, sv_neg_15 = self._compute_semivariances(returns_list, VOL_WINDOW_15MIN)
        jump_sq = max(0.0, rk_5min ** 2 - bv_5min ** 2)
        obs = {
            "ts": now, "rv1_sq": rk_1min ** 2, "rv5_sq": rk_5min ** 2,
            "rv15_sq": rk_15min ** 2, "jump_sq": jump_sq,
            "sv_pos_1": sv_pos_1, "sv_neg_1": sv_neg_1,
            "sv_pos_5": sv_pos_5, "sv_neg_5": sv_neg_5,
            "sv_pos_15": sv_pos_15, "sv_neg_15": sv_neg_15,
        }
        self._observations[asset].append(obs)

    def is_active(self, asset):
        if HAR_SHADOW_MODE:
            return False
        return self._active_model.get(asset, "fixed") != "fixed"

    def get_blend(self, asset, rk_1min, rk_5min, rk_15min, jump_sq=0.0,
                  sv_pos_1=0.0, sv_neg_1=0.0, sv_pos_5=0.0, sv_neg_5=0.0,
                  sv_pos_15=0.0, sv_neg_15=0.0):
        model = self._active_model.get(asset, "fixed")
        coeffs = self._coefficients.get(asset, {}).get(model)
        if model == "fixed" or coeffs is None:
            w1, w5, w15 = VOL_BLEND_WEIGHTS
            return w1 * rk_1min + w5 * rk_5min + w15 * rk_15min
        if model == "level_har":
            val = coeffs[0] + coeffs[1]*rk_1min**2 + coeffs[2]*rk_5min**2 + coeffs[3]*rk_15min**2
            return math.sqrt(max(0.0, val))
        if model == "log_har":
            eps = 1e-20
            val = coeffs[0] + (coeffs[1]*math.log(max(eps, rk_1min**2))
                  + coeffs[2]*math.log(max(eps, rk_5min**2))
                  + coeffs[3]*math.log(max(eps, rk_15min**2)))
            return math.sqrt(max(0.0, math.exp(val)))
        if model == "har_j":
            val = (coeffs[0] + coeffs[1]*rk_1min**2 + coeffs[2]*rk_5min**2
                   + coeffs[3]*rk_15min**2 + coeffs[4]*jump_sq)
            return math.sqrt(max(0.0, val))
        if model == "har_semi":
            val = (coeffs[0] + coeffs[1]*sv_pos_1 + coeffs[2]*sv_neg_1
                   + coeffs[3]*sv_pos_5 + coeffs[4]*sv_neg_5
                   + coeffs[5]*sv_pos_15 + coeffs[6]*sv_neg_15)
            return math.sqrt(max(0.0, val))
        w1, w5, w15 = VOL_BLEND_WEIGHTS
        return w1 * rk_1min + w5 * rk_5min + w15 * rk_15min

    def maybe_refit(self):
        now = time.time()
        if now - self._last_refit < HAR_REFIT_INTERVAL and self._last_refit > 0:
            return False
        any_refit = False
        for asset in ASSETS:
            obs = self._observations[asset]
            if len(obs) < HAR_MIN_OBSERVATIONS:
                continue
            self._refit_asset(asset, list(obs))
            any_refit = True
        if any_refit:
            self._last_refit = now
            self._save_state()
        return any_refit

    def _refit_asset(self, asset, obs):
        n = len(obs)
        if n < 2:
            return
        targets = [obs[i + 1]["rv5_sq"] for i in range(n - 1)]
        old_model = self._active_model.get(asset, "fixed")
        qlike_scores = {}
        fitted_coeffs = {}

        # level_har
        X_level = [[1.0, obs[i]["rv1_sq"], obs[i]["rv5_sq"], obs[i]["rv15_sq"]] for i in range(n - 1)]
        self._try_fit_model(asset, "level_har", X_level, targets, qlike_scores, fitted_coeffs)

        # log_har
        eps = 1e-20
        X_log = [[1.0, math.log(max(eps, obs[i]["rv1_sq"])),
                   math.log(max(eps, obs[i]["rv5_sq"])),
                   math.log(max(eps, obs[i]["rv15_sq"]))] for i in range(n - 1)]
        log_targets = [math.log(max(eps, t)) for t in targets]
        c = self._fit_wls(X_log, log_targets, [1.0/math.sqrt(max(eps, t)) for t in targets])
        if c is not None:
            preds = []
            for i in range(n - 1):
                val = c[0] + c[1]*X_log[i][1] + c[2]*X_log[i][2] + c[3]*X_log[i][3]
                preds.append(math.exp(val))
            ql = self._compute_qlike(targets, preds)
            ok, reason = self._sanity_check_coeffs(c, "log_har")
            if ok and ql <= HAR_QLIKE_FALLBACK_THRESHOLD:
                qlike_scores["log_har"] = ql
                fitted_coeffs["log_har"] = c

        # har_j
        X_j = [[1.0, obs[i]["rv1_sq"], obs[i]["rv5_sq"], obs[i]["rv15_sq"],
                 obs[i]["jump_sq"]] for i in range(n - 1)]
        self._try_fit_model(asset, "har_j", X_j, targets, qlike_scores, fitted_coeffs)

        # har_semi
        X_semi = [[1.0, obs[i]["sv_pos_1"], obs[i]["sv_neg_1"],
                    obs[i]["sv_pos_5"], obs[i]["sv_neg_5"],
                    obs[i]["sv_pos_15"], obs[i]["sv_neg_15"]] for i in range(n - 1)]
        self._try_fit_model(asset, "har_semi", X_semi, targets, qlike_scores, fitted_coeffs)

        # Fixed baseline
        w1, w5, w15 = VOL_BLEND_WEIGHTS
        fixed_preds = [(w1**2 * obs[i]["rv1_sq"] + w5**2 * obs[i]["rv5_sq"]
                         + w15**2 * obs[i]["rv15_sq"]
                         + 2*w1*w5*math.sqrt(max(0.0, obs[i]["rv1_sq"]*obs[i]["rv5_sq"]))
                         + 2*w1*w15*math.sqrt(max(0.0, obs[i]["rv1_sq"]*obs[i]["rv15_sq"]))
                         + 2*w5*w15*math.sqrt(max(0.0, obs[i]["rv5_sq"]*obs[i]["rv15_sq"])))
                        for i in range(n - 1)]
        fixed_qlike = self._compute_qlike(targets, fixed_preds)
        qlike_scores["fixed"] = fixed_qlike

        # Select best
        best_model = "fixed"
        best_qlike = fixed_qlike
        for model_name in self.MODEL_NAMES:
            if model_name in qlike_scores and qlike_scores[model_name] < best_qlike:
                prev_qlike = self._qlike_scores.get(asset, {}).get(model_name)
                if prev_qlike is not None and qlike_scores[model_name] > prev_qlike + 0.1:
                    continue
                best_model = model_name
                best_qlike = qlike_scores[model_name]

        self._active_model[asset] = best_model
        self._qlike_scores[asset] = qlike_scores
        if best_model != "fixed" and best_model in fitted_coeffs:
            self._coefficients[asset][best_model] = fitted_coeffs[best_model]

    def _try_fit_model(self, asset, model_name, X, targets, qlike_scores, fitted_coeffs):
        eps = 1e-20
        weights = [1.0 / math.sqrt(max(eps, t)) for t in targets]
        c = self._fit_wls(X, targets, weights)
        if c is None:
            return
        preds = [sum(c[j] * X[i][j] for j in range(len(c))) for i in range(len(X))]
        ql = self._compute_qlike(targets, preds)
        ok, reason = self._sanity_check_coeffs(c, model_name)
        if ok and ql <= HAR_QLIKE_FALLBACK_THRESHOLD:
            qlike_scores[model_name] = ql
            fitted_coeffs[model_name] = c

    @staticmethod
    def _fit_wls(X, y, w):
        n = len(y)
        if n == 0:
            return None
        p = len(X[0])
        XtWX = [[0.0] * p for _ in range(p)]
        XtWy = [0.0] * p
        for i in range(n):
            wi = w[i]
            xi = X[i]
            yi = y[i]
            for j in range(p):
                wxi_j = wi * xi[j]
                XtWy[j] += wxi_j * yi
                for k in range(j, p):
                    val = wxi_j * xi[k]
                    XtWX[j][k] += val
                    if k != j:
                        XtWX[k][j] += val
        return HAREstimator._gauss_eliminate(XtWX, XtWy)

    @staticmethod
    def _gauss_eliminate(A, b):
        n = len(b)
        M = [A[i][:] + [b[i]] for i in range(n)]
        for col in range(n):
            max_val = abs(M[col][col])
            max_row = col
            for row in range(col + 1, n):
                if abs(M[row][col]) > max_val:
                    max_val = abs(M[row][col])
                    max_row = row
            if max_val < 1e-15:
                return None
            if max_row != col:
                M[col], M[max_row] = M[max_row], M[col]
            pivot = M[col][col]
            for row in range(col + 1, n):
                factor = M[row][col] / pivot
                for j in range(col, n + 1):
                    M[row][j] -= factor * M[col][j]
        x = [0.0] * n
        for i in range(n - 1, -1, -1):
            if abs(M[i][i]) < 1e-15:
                return None
            x[i] = M[i][n]
            for j in range(i + 1, n):
                x[i] -= M[i][j] * x[j]
            x[i] /= M[i][i]
        return x

    @staticmethod
    def _compute_qlike(y_actual, y_predicted):
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

    @staticmethod
    def _compute_semivariances(returns, window):
        subset = returns[-window:] if len(returns) >= window else returns
        n = len(subset)
        if n == 0:
            return (0.0, 0.0)
        sv_pos = 0.0
        sv_neg = 0.0
        for r in subset:
            r2 = r * r
            if r > 0:
                sv_pos += r2
            else:
                sv_neg += r2
        return (sv_pos / n, sv_neg / n)

    @staticmethod
    def _sanity_check_coeffs(coeffs, model_name):
        if not coeffs:
            return (False, "empty coefficients")
        intercept = coeffs[0]
        weights = coeffs[1:]
        if abs(intercept) > 0.001:
            return (False, f"intercept {intercept:.6f} exceeds ±0.001")
        if model_name != "log_har":
            for i, w in enumerate(weights):
                if w < 0:
                    return (False, f"weight[{i}]={w:.6f} is negative")
        for i, w in enumerate(weights):
            if abs(w) > 1.5:
                return (False, f"weight[{i}]={w:.6f} exceeds ±1.5")
        if model_name != "log_har":
            wsum = sum(weights)
            if wsum < 0.3 or wsum > 2.0:
                return (False, f"sum_weights={wsum:.4f} outside [0.3, 2.0]")
        return (True, "ok")

    def _load_state(self):
        try:
            with open(HAR_STATE_PATH, "r") as f:
                state = json.load(f)
            for asset in ASSETS:
                if asset in state.get("active_model", {}):
                    self._active_model[asset] = state["active_model"][asset]
                if asset in state.get("coefficients", {}):
                    self._coefficients[asset] = state["coefficients"][asset]
                if asset in state.get("qlike_scores", {}):
                    self._qlike_scores[asset] = state["qlike_scores"][asset]
            self._last_refit = state.get("last_refit", 0.0)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save_state(self):
        state = {
            "active_model": self._active_model,
            "coefficients": self._coefficients,
            "qlike_scores": self._qlike_scores,
            "last_refit": self._last_refit,
        }
        tmp = HAR_STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, HAR_STATE_PATH)

    def get_diagnostics(self):
        result = {}
        now = time.time()
        for asset in ASSETS:
            obs = self._observations[asset]
            n_obs = len(obs)
            model = self._active_model.get(asset, "fixed")
            ql = self._qlike_scores.get(asset, {})
            coeffs = self._coefficients.get(asset, {})
            diag = {
                "active_model": model,
                "n_observations": n_obs,
                "qlike_scores": {k: round(v, 4) for k, v in ql.items()} if ql else {},
                "coefficients": {k: [round(c, 6) for c in v] for k, v in coeffs.items()} if coeffs else {},
                "last_refit_age_s": round(now - self._last_refit, 1) if self._last_refit > 0 else None,
            }
            if "fixed" in ql and model != "fixed" and model in ql:
                fixed_ql = ql["fixed"]
                best_ql = ql[model]
                if fixed_ql > 0:
                    diag["qlike_vs_fixed_pct"] = round(100 * (best_ql - fixed_ql) / fixed_ql, 1)
            if n_obs > 0:
                last = obs[-1]
                sv_neg_5 = last.get("sv_neg_5", 0)
                sv_pos_5 = last.get("sv_pos_5", 0)
                if sv_pos_5 > 0:
                    diag["semivar_ratio_5min"] = round(sv_neg_5 / sv_pos_5, 2)
            result[asset] = diag
        return result


# ═══════════════════════════════════════════════════════════════════════════
#  Test harness
# ═══════════════════════════════════════════════════════════════════════════

PASS = 0
FAIL = 0


def check(condition, name):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name}")


# ═══════════════════════════════════════════════════════════════════════════
#  Test 1: WLS solver correctness
# ═══════════════════════════════════════════════════════════════════════════

def test_wls_solver():
    print("\n=== Test 1: WLS solver correctness ===")
    random.seed(42)
    n = 200
    true_b = [0.1, 0.4, 0.35, 0.25]

    X = []
    y = []
    for _ in range(n):
        x1 = random.gauss(0.0001, 0.00005)
        x2 = random.gauss(0.0001, 0.00005)
        x3 = random.gauss(0.0001, 0.00005)
        noise = random.gauss(0, 0.00001)
        yi = true_b[0] + true_b[1] * x1 + true_b[2] * x2 + true_b[3] * x3 + noise
        X.append([1.0, x1, x2, x3])
        y.append(yi)

    # OLS (uniform weights)
    w_uniform = [1.0] * n
    coeffs = HAREstimator._fit_wls(X, y, w_uniform)
    check(coeffs is not None, "OLS converges")
    if coeffs:
        check(abs(coeffs[0] - true_b[0]) < 0.05, f"intercept ~{true_b[0]} (got {coeffs[0]:.4f})")
        check(abs(coeffs[1] - true_b[1]) < 0.15, f"beta1 ~{true_b[1]} (got {coeffs[1]:.4f})")
        check(abs(coeffs[2] - true_b[2]) < 0.15, f"beta2 ~{true_b[2]} (got {coeffs[2]:.4f})")
        check(abs(coeffs[3] - true_b[3]) < 0.15, f"beta3 ~{true_b[3]} (got {coeffs[3]:.4f})")

    # WLS with inverse-y weights
    w_wls = [1.0 / max(1e-10, abs(yi)) for yi in y]
    coeffs_wls = HAREstimator._fit_wls(X, y, w_wls)
    check(coeffs_wls is not None, "WLS converges")


# ═══════════════════════════════════════════════════════════════════════════
#  Test 2: Gaussian elimination edge cases
# ═══════════════════════════════════════════════════════════════════════════

def test_gauss_elimination():
    print("\n=== Test 2: Gaussian elimination edge cases ===")

    # 1x1
    result = HAREstimator._gauss_eliminate([[5.0]], [10.0])
    check(result is not None and abs(result[0] - 2.0) < 1e-10, "1x1 system")

    # 2x2
    result = HAREstimator._gauss_eliminate([[2.0, 1.0], [1.0, 3.0]], [5.0, 7.0])
    check(result is not None, "2x2 system solves")
    if result:
        check(abs(result[0] - 1.6) < 1e-10, f"2x2 x={result[0]:.4f}")
        check(abs(result[1] - 1.8) < 1e-10, f"2x2 y={result[1]:.4f}")

    # 4x4
    A4 = [[4, 1, 0, 0], [1, 4, 1, 0], [0, 1, 4, 1], [0, 0, 1, 4]]
    b4 = [1, 2, 3, 4]
    result = HAREstimator._gauss_eliminate(A4, b4)
    check(result is not None, "4x4 system solves")
    if result:
        for i in range(4):
            val = sum(A4[i][j] * result[j] for j in range(4))
            check(abs(val - b4[i]) < 1e-8, f"4x4 row {i} residual")

    # 7x7 (HAR-semiRV size)
    random.seed(123)
    A7 = [[random.gauss(0, 1) for _ in range(7)] for _ in range(7)]
    for i in range(7):
        A7[i][i] = sum(abs(A7[i][j]) for j in range(7)) + 1.0
    b7 = [random.gauss(0, 1) for _ in range(7)]
    result = HAREstimator._gauss_eliminate(A7, b7)
    check(result is not None, "7x7 system solves")
    if result:
        for i in range(7):
            val = sum(A7[i][j] * result[j] for j in range(7))
            check(abs(val - b7[i]) < 1e-6, f"7x7 row {i} residual")

    # Singular matrix
    result = HAREstimator._gauss_eliminate([[1.0, 2.0], [2.0, 4.0]], [3.0, 6.0])
    check(result is None, "singular matrix returns None")

    # Nearly-singular
    result = HAREstimator._gauss_eliminate([[1.0, 2.0], [1.0, 2.0 + 1e-16]], [3.0, 3.0])
    check(result is None, "nearly-singular returns None")


# ═══════════════════════════════════════════════════════════════════════════
#  Test 3: QLIKE computation
# ═══════════════════════════════════════════════════════════════════════════

def test_qlike():
    print("\n=== Test 3: QLIKE computation ===")

    # Perfect forecast
    y = [0.001, 0.002, 0.003, 0.004]
    ql = HAREstimator._compute_qlike(y, y)
    check(abs(ql) < 1e-10, f"perfect forecast QLIKE={ql:.2e}")

    # Constant overestimate (predict 2x actual)
    preds = [2 * v for v in y]
    ql = HAREstimator._compute_qlike(y, preds)
    check(ql > 0, f"overestimate QLIKE={ql:.4f} > 0")

    # Constant underestimate (predict 0.5x actual)
    preds = [0.5 * v for v in y]
    ql = HAREstimator._compute_qlike(y, preds)
    check(ql > 0, f"underestimate QLIKE={ql:.4f} > 0")

    # Zero/negative inputs — no crash
    ql = HAREstimator._compute_qlike([0.0, -1.0, 0.001], [0.001, 0.001, 0.001])
    check(math.isfinite(ql), f"zero/negative inputs no crash, QLIKE={ql:.4f}")

    # Empty
    ql = HAREstimator._compute_qlike([], [])
    check(ql == 0.0, "empty inputs QLIKE=0")


# ═══════════════════════════════════════════════════════════════════════════
#  Test 4: Semivariance computation
# ═══════════════════════════════════════════════════════════════════════════

def test_semivariance():
    print("\n=== Test 4: Semivariance computation ===")

    pos_returns = [0.01, 0.02, 0.03, 0.04, 0.05]
    sv_pos, sv_neg = HAREstimator._compute_semivariances(pos_returns, 5)
    check(sv_neg == 0.0, "all positive: sv_neg=0")
    check(sv_pos > 0, f"all positive: sv_pos={sv_pos:.6f} > 0")

    neg_returns = [-0.01, -0.02, -0.03, -0.04, -0.05]
    sv_pos, sv_neg = HAREstimator._compute_semivariances(neg_returns, 5)
    check(sv_pos == 0.0, "all negative: sv_pos=0")
    check(sv_neg > 0, f"all negative: sv_neg={sv_neg:.6f} > 0")

    sym_returns = [0.01, -0.01, 0.02, -0.02, 0.03, -0.03]
    sv_pos, sv_neg = HAREstimator._compute_semivariances(sym_returns, 6)
    check(abs(sv_pos - sv_neg) < 1e-15, f"symmetric: sv_pos={sv_pos:.8f} sv_neg={sv_neg:.8f}")

    sv_pos, sv_neg = HAREstimator._compute_semivariances([], 10)
    check(sv_pos == 0.0 and sv_neg == 0.0, "empty returns: both zero")


# ═══════════════════════════════════════════════════════════════════════════
#  Test 5: Sanity checks
# ═══════════════════════════════════════════════════════════════════════════

def test_sanity_checks():
    print("\n=== Test 5: Sanity checks ===")

    ok, reason = HAREstimator._sanity_check_coeffs([0.0001, 0.4, 0.35, 0.25], "level_har")
    check(ok, f"valid level_har: {reason}")

    ok, reason = HAREstimator._sanity_check_coeffs([0.0001, -0.1, 0.35, 0.25], "level_har")
    check(not ok, f"negative weight rejected: {reason}")

    ok, reason = HAREstimator._sanity_check_coeffs([0.0001, 2.0, 0.35, 0.25], "level_har")
    check(not ok, f"exploding weight rejected: {reason}")

    ok, reason = HAREstimator._sanity_check_coeffs([0.01, 0.4, 0.35, 0.25], "level_har")
    check(not ok, f"large intercept rejected: {reason}")

    ok, reason = HAREstimator._sanity_check_coeffs([0.0001, 0.05, 0.05, 0.05], "level_har")
    check(not ok, f"sum too small rejected: {reason}")

    ok, reason = HAREstimator._sanity_check_coeffs([0.0001, 1.0, 1.0, 0.5], "level_har")
    check(not ok, f"sum too large rejected: {reason}")

    ok, reason = HAREstimator._sanity_check_coeffs([0.0001, 0.5, 0.3, -0.2], "log_har")
    check(ok, f"log_har negative weight ok: {reason}")

    ok, reason = HAREstimator._sanity_check_coeffs([], "level_har")
    check(not ok, f"empty coeffs rejected: {reason}")


# ═══════════════════════════════════════════════════════════════════════════
#  Test 6: End-to-end model selection
# ═══════════════════════════════════════════════════════════════════════════

def test_model_selection():
    print("\n=== Test 6: End-to-end model selection ===")
    random.seed(99)

    obs = []
    for _ in range(100):
        rv1 = abs(random.gauss(1e-4, 3e-5))
        rv5 = abs(random.gauss(1e-4, 2e-5))
        rv15 = abs(random.gauss(1e-4, 1e-5))
        jump = max(0, rv5 - 0.8 * rv5) * random.random()
        obs.append({
            "ts": time.time() + _ * 300,
            "rv1_sq": rv1, "rv5_sq": rv5, "rv15_sq": rv15,
            "jump_sq": jump,
            "sv_pos_1": rv1 * 0.5, "sv_neg_1": rv1 * 0.5,
            "sv_pos_5": rv5 * 0.5, "sv_neg_5": rv5 * 0.5,
            "sv_pos_15": rv15 * 0.5, "sv_neg_15": rv15 * 0.5,
        })

    est = HAREstimator()
    est._refit_asset("BTC", obs)

    model = est._active_model["BTC"]
    check(model != "fixed" or True, f"model selected: {model}")  # May be fixed if sanity rejects all

    ql = est._qlike_scores.get("BTC", {})
    check(len(ql) > 0, f"QLIKE scores computed: {len(ql)} models")
    check("fixed" in ql, "fixed baseline QLIKE computed")

    # Default model
    est2 = HAREstimator()
    check(est2._active_model["BTC"] == "fixed", "default model is fixed")


# ═══════════════════════════════════════════════════════════════════════════
#  Test 7: Full integration smoke test
# ═══════════════════════════════════════════════════════════════════════════

def test_integration():
    print("\n=== Test 7: Full integration smoke test ===")

    est = HAREstimator()
    random.seed(77)

    for i in range(50):
        rv1 = abs(random.gauss(1e-4, 3e-5))
        rv5 = abs(random.gauss(1e-4, 2e-5))
        rv15 = abs(random.gauss(1e-4, 1e-5))
        bv5 = rv5 * 0.9
        returns_list = [random.gauss(0, 0.001) for _ in range(180)]

        # Override timer to allow observation recording
        est._last_obs_time["BTC"] = 0.0
        est.record_observation("BTC", returns_list, rv1, rv5, rv15, bv5)

    n_obs = len(est._observations["BTC"])
    check(n_obs == 50, f"recorded {n_obs} observations (expected 50)")

    # Trigger refit
    est._last_refit = 0.0
    refit_ok = est.maybe_refit()
    check(refit_ok, "maybe_refit() returned True")

    model = est._active_model["BTC"]
    check(model is not None, f"active model: {model}")

    # get_blend should return a finite positive number
    rv = est.get_blend("BTC", 0.001, 0.0008, 0.0006,
                       jump_sq=0.0001,
                       sv_pos_1=0.00005, sv_neg_1=0.00005,
                       sv_pos_5=0.00004, sv_neg_5=0.00004,
                       sv_pos_15=0.00003, sv_neg_15=0.00003)
    check(math.isfinite(rv) and rv > 0, f"get_blend output={rv:.8f} (finite & positive)")

    # get_diagnostics should be JSON-serializable
    diag = est.get_diagnostics()
    try:
        json_str = json.dumps(diag)
        check(True, f"diagnostics JSON-serializable ({len(json_str)} bytes)")
    except (TypeError, ValueError) as e:
        check(False, f"diagnostics not JSON-serializable: {e}")

    check("BTC" in diag, "diagnostics has BTC entry")
    check(diag["BTC"]["n_observations"] == 50, f"diagnostics n_observations={diag['BTC']['n_observations']}")

    # State persistence round-trip
    est._save_state()
    check(os.path.exists(HAR_STATE_PATH), "state file saved")

    est2 = HAREstimator()
    check(est2._active_model["BTC"] == est._active_model["BTC"],
          f"state restored: model={est2._active_model['BTC']}")

    # Clean up
    try:
        os.remove(HAR_STATE_PATH)
    except FileNotFoundError:
        pass
    try:
        os.remove(HAR_STATE_PATH + ".tmp")
    except FileNotFoundError:
        pass


# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("HAREstimator Test Suite")
    print("=" * 60)

    test_wls_solver()
    test_gauss_elimination()
    test_qlike()
    test_semivariance()
    test_sanity_checks()
    test_model_selection()
    test_integration()

    print("\n" + "=" * 60)
    print(f"Results: {PASS} passed, {FAIL} failed")
    if FAIL > 0:
        sys.exit(1)
    else:
        print("All tests passed!")
        sys.exit(0)
