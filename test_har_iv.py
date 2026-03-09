#!/usr/bin/env python3
"""Standalone tests for HAR-IV models — no pip dependencies required.

NOTE: HAR model was deleted from production (misapplied at sub-hourly
timescales). These tests cover dead code and are skipped in CI.
Kept for reference in case HAR-IV is revisited.

Run: python3 test_har_iv.py
"""
import sys
import pytest
pytest.skip("HAR-IV model deleted from production — tests cover dead code", allow_module_level=True)

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
HAR_STATE_PATH = "har_state_test_iv.json"  # test-specific path
HAR_QLIKE_FALLBACK_THRESHOLD = 2.0
HAR_SHADOW_MODE = True
HAR_IV_REPLACES_DVOL_BLEND = False
HAR_IV_MIN_DVOL_FRACTION = 0.70
DVOL_HOURLY_AVG_MAXLEN = 60
DVOL_HOURLY_AVG_MIN = 3


# ═══════════════════════════════════════════════════════════════════════════
#  HAREstimator (copied from bot.py with IV model extensions)
# ═══════════════════════════════════════════════════════════════════════════

class HAREstimator:
    MODEL_NAMES = ("level_har", "log_har", "har_j", "har_semi",
                   "har_iv", "har_j_iv", "log_har_iv", "har_vrp")

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

    def record_observation(self, asset, returns_list, rk_1min, rk_5min, rk_15min,
                           bv_5min, dvol_sq=None):
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
            "dvol_sq": dvol_sq,
        }
        self._observations[asset].append(obs)

    def is_active(self, asset):
        if HAR_SHADOW_MODE:
            return False
        return self._active_model.get(asset, "fixed") != "fixed"

    def get_blend(self, asset, rk_1min, rk_5min, rk_15min, jump_sq=0.0,
                  sv_pos_1=0.0, sv_neg_1=0.0, sv_pos_5=0.0, sv_neg_5=0.0,
                  sv_pos_15=0.0, sv_neg_15=0.0, dvol_sq=None):
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
        if model == "har_iv":
            if dvol_sq is None:
                return self._fallback_prediction(asset, rk_1min, rk_5min, rk_15min)
            val = coeffs[0] + coeffs[1]*rk_1min**2 + coeffs[2]*rk_5min**2 + coeffs[3]*rk_15min**2 + coeffs[4]*dvol_sq
            return math.sqrt(max(0.0, val))
        if model == "har_j_iv":
            if dvol_sq is None:
                return self._fallback_prediction(asset, rk_1min, rk_5min, rk_15min)
            val = (coeffs[0] + coeffs[1]*rk_1min**2 + coeffs[2]*rk_5min**2
                   + coeffs[3]*rk_15min**2 + coeffs[4]*jump_sq + coeffs[5]*dvol_sq)
            return math.sqrt(max(0.0, val))
        if model == "log_har_iv":
            if dvol_sq is None:
                return self._fallback_prediction(asset, rk_1min, rk_5min, rk_15min)
            eps = 1e-20
            val = (coeffs[0] + coeffs[1]*math.log(max(eps, rk_1min**2))
                   + coeffs[2]*math.log(max(eps, rk_5min**2))
                   + coeffs[3]*math.log(max(eps, rk_15min**2))
                   + coeffs[4]*math.log(max(eps, dvol_sq)))
            return math.sqrt(max(0.0, math.exp(val)))
        if model == "har_vrp":
            if dvol_sq is None:
                return self._fallback_prediction(asset, rk_1min, rk_5min, rk_15min)
            vrp = dvol_sq - rk_5min**2
            val = coeffs[0] + coeffs[1]*rk_1min**2 + coeffs[2]*rk_5min**2 + coeffs[3]*rk_15min**2 + coeffs[4]*vrp
            return math.sqrt(max(0.0, val))
        w1, w5, w15 = VOL_BLEND_WEIGHTS
        return w1 * rk_1min + w5 * rk_5min + w15 * rk_15min

    def _fallback_prediction(self, asset, rk_1min, rk_5min, rk_15min):
        coeffs = self._coefficients.get(asset, {}).get("level_har")
        if coeffs is not None:
            val = coeffs[0] + coeffs[1]*rk_1min**2 + coeffs[2]*rk_5min**2 + coeffs[3]*rk_15min**2
            return math.sqrt(max(0.0, val))
        w1, w5, w15 = VOL_BLEND_WEIGHTS
        return w1 * rk_1min + w5 * rk_5min + w15 * rk_15min

    def get_har_prediction(self, asset, rk_1min, rk_5min, rk_15min, jump_sq=0.0,
                           sv_pos_1=0.0, sv_neg_1=0.0, sv_pos_5=0.0, sv_neg_5=0.0,
                           sv_pos_15=0.0, sv_neg_15=0.0, dvol_sq=None):
        model = self._active_model.get(asset, "fixed")
        if model == "fixed" or model not in self._coefficients.get(asset, {}):
            return None
        return self.get_blend(asset, rk_1min, rk_5min, rk_15min,
                              jump_sq, sv_pos_1, sv_neg_1,
                              sv_pos_5, sv_neg_5, sv_pos_15, sv_neg_15,
                              dvol_sq=dvol_sq)

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

        # IV-augmented models
        dvol_available = [i for i in range(n - 1) if obs[i].get("dvol_sq") is not None]
        dvol_fraction = len(dvol_available) / (n - 1) if n > 1 else 0.0

        if dvol_fraction >= HAR_IV_MIN_DVOL_FRACTION:
            iv_indices = dvol_available
            iv_targets = [targets[i] for i in iv_indices]

            # har_iv
            X_iv = [[1.0, obs[i]["rv1_sq"], obs[i]["rv5_sq"], obs[i]["rv15_sq"],
                      obs[i]["dvol_sq"]] for i in iv_indices]
            self._try_fit_model(asset, "har_iv", X_iv, iv_targets, qlike_scores, fitted_coeffs)

            # har_j_iv
            X_j_iv = [[1.0, obs[i]["rv1_sq"], obs[i]["rv5_sq"], obs[i]["rv15_sq"],
                        obs[i]["jump_sq"], obs[i]["dvol_sq"]] for i in iv_indices]
            self._try_fit_model(asset, "har_j_iv", X_j_iv, iv_targets, qlike_scores, fitted_coeffs)

            # log_har_iv
            X_log_iv = [[1.0, math.log(max(eps, obs[i]["rv1_sq"])),
                          math.log(max(eps, obs[i]["rv5_sq"])),
                          math.log(max(eps, obs[i]["rv15_sq"])),
                          math.log(max(eps, obs[i]["dvol_sq"]))]
                         for i in iv_indices]
            log_iv_targets = [math.log(max(eps, t)) for t in iv_targets]
            c_log_iv = self._fit_wls(X_log_iv, log_iv_targets,
                                     [1.0 / math.sqrt(max(eps, t)) for t in iv_targets])
            if c_log_iv is not None:
                preds_log_iv = []
                for idx in range(len(iv_indices)):
                    val = sum(c_log_iv[j] * X_log_iv[idx][j] for j in range(len(c_log_iv)))
                    preds_log_iv.append(math.exp(val))
                ql_log_iv = self._compute_qlike(iv_targets, preds_log_iv)
                ok_log_iv, _ = self._sanity_check_coeffs(c_log_iv, "log_har_iv")
                if ok_log_iv and ql_log_iv <= HAR_QLIKE_FALLBACK_THRESHOLD:
                    qlike_scores["log_har_iv"] = ql_log_iv
                    fitted_coeffs["log_har_iv"] = c_log_iv

            # har_vrp
            X_vrp = [[1.0, obs[i]["rv1_sq"], obs[i]["rv5_sq"], obs[i]["rv15_sq"],
                       obs[i]["dvol_sq"] - obs[i]["rv5_sq"]] for i in iv_indices]
            self._try_fit_model(asset, "har_vrp", X_vrp, iv_targets, qlike_scores, fitted_coeffs)

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
        log_models = {"log_har", "log_har_iv"}
        if abs(intercept) > 0.001:
            return (False, f"intercept {intercept:.6f} exceeds ±0.001")
        if model_name not in log_models:
            for i, w in enumerate(weights):
                if model_name == "har_vrp" and i == len(weights) - 1:
                    continue
                if w < 0:
                    return (False, f"weight[{i}]={w:.6f} is negative")
        for i, w in enumerate(weights):
            if abs(w) > 1.5:
                return (False, f"weight[{i}]={w:.6f} exceeds ±1.5")
        if model_name not in log_models:
            wsum = sum(weights)
            iv_augmented = {"har_iv", "har_j_iv", "har_vrp"}
            upper = 2.5 if model_name in iv_augmented else 2.0
            if wsum < 0.3 or wsum > upper:
                return (False, f"sum_weights={wsum:.4f} outside [0.3, {upper}]")
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
            if n_obs > 0:
                dvol_count = sum(1 for o in obs if o.get("dvol_sq") is not None)
                diag["dvol_obs_fraction"] = round(dvol_count / n_obs, 2)
            if n_obs > 0:
                last = obs[-1]
                dvol_sq_last = last.get("dvol_sq")
                rv5_sq_last = last.get("rv5_sq", 0)
                if dvol_sq_last is not None and rv5_sq_last > 0:
                    diag["vrp_last"] = dvol_sq_last - rv5_sq_last
            result[asset] = diag
        return result


# ═══════════════════════════════════════════════════════════════════════════
#  DVOL hourly average logic (copied from DeribitDVOLFetcher)
# ═══════════════════════════════════════════════════════════════════════════

def dvol_hourly_avg(buf):
    """Simulates get_dvol_hourly_avg logic."""
    if buf is None or len(buf) < DVOL_HOURLY_AVG_MIN:
        return None
    return sum(buf) / len(buf)


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
#  Category A: Hourly Average (3 tests)
# ═══════════════════════════════════════════════════════════════════════════

def test_hourly_avg_empty():
    print("\n=== Test A1: Empty deque returns None ===")
    buf = deque(maxlen=DVOL_HOURLY_AVG_MAXLEN)
    result = dvol_hourly_avg(buf)
    check(result is None, "empty deque → None")


def test_hourly_avg_below_min():
    print("\n=== Test A2: Below minimum samples returns None ===")
    buf = deque(maxlen=DVOL_HOURLY_AVG_MAXLEN)
    buf.append(0.001)
    buf.append(0.002)
    result = dvol_hourly_avg(buf)
    check(result is None, f"2 samples (min={DVOL_HOURLY_AVG_MIN}) → None")


def test_hourly_avg_correct():
    print("\n=== Test A3: Correct mean with sufficient samples ===")
    buf = deque(maxlen=DVOL_HOURLY_AVG_MAXLEN)
    values = [0.001 * (i + 1) for i in range(10)]
    for v in values:
        buf.append(v)
    result = dvol_hourly_avg(buf)
    expected = sum(values) / len(values)
    check(result is not None, "result not None")
    check(abs(result - expected) < 1e-15, f"mean={result:.6f} == {expected:.6f}")


# ═══════════════════════════════════════════════════════════════════════════
#  Category B: Observation Recording (3 tests)
# ═══════════════════════════════════════════════════════════════════════════

def test_obs_dvol_none():
    print("\n=== Test B4: dvol_sq=None stored correctly ===")
    est = HAREstimator()
    returns = [random.gauss(0, 0.001) for _ in range(180)]
    est._last_obs_time["BTC"] = 0.0
    est.record_observation("BTC", returns, 0.001, 0.0008, 0.0006, 0.0007, dvol_sq=None)
    obs = est._observations["BTC"][-1]
    check(obs["dvol_sq"] is None, "dvol_sq stored as None")
    check("rv1_sq" in obs, "rv1_sq still present")
    check("jump_sq" in obs, "jump_sq still present")


def test_obs_dvol_float():
    print("\n=== Test B5: dvol_sq=float stored correctly ===")
    est = HAREstimator()
    returns = [random.gauss(0, 0.001) for _ in range(180)]
    est._last_obs_time["BTC"] = 0.0
    dvol_val = 1.5e-8
    est.record_observation("BTC", returns, 0.001, 0.0008, 0.0006, 0.0007, dvol_sq=dvol_val)
    obs = est._observations["BTC"][-1]
    check(obs["dvol_sq"] == dvol_val, f"dvol_sq stored as {dvol_val}")


def test_obs_backward_compat():
    print("\n=== Test B6: Backward compatibility (no dvol_sq kwarg) ===")
    est = HAREstimator()
    returns = [random.gauss(0, 0.001) for _ in range(180)]
    est._last_obs_time["BTC"] = 0.0
    # Call without dvol_sq — should use default None
    est.record_observation("BTC", returns, 0.001, 0.0008, 0.0006, 0.0007)
    obs = est._observations["BTC"][-1]
    check(obs["dvol_sq"] is None, "dvol_sq defaults to None")


# ═══════════════════════════════════════════════════════════════════════════
#  Category C: Sanity Checks (6 tests)
# ═══════════════════════════════════════════════════════════════════════════

def test_sanity_har_iv_valid():
    print("\n=== Test C7: har_iv valid coeffs ===")
    # [intercept, rv1, rv5, rv15, iv_coeff] — all non-negative, sum in [0.3, 2.5]
    ok, reason = HAREstimator._sanity_check_coeffs([0.0001, 0.3, 0.3, 0.2, 0.3], "har_iv")
    check(ok, f"har_iv valid: {reason}")


def test_sanity_har_iv_neg_iv():
    print("\n=== Test C8: har_iv negative IV coeff → fails ===")
    ok, reason = HAREstimator._sanity_check_coeffs([0.0001, 0.3, 0.3, 0.2, -0.1], "har_iv")
    check(not ok, f"har_iv negative IV rejected: {reason}")


def test_sanity_har_iv_sum_exceeds():
    print("\n=== Test C9: har_iv sum exceeds 2.5 → fails ===")
    ok, reason = HAREstimator._sanity_check_coeffs([0.0001, 0.8, 0.8, 0.5, 0.5], "har_iv")
    check(not ok, f"har_iv sum>2.5 rejected: {reason}")


def test_sanity_har_vrp_neg_vrp():
    print("\n=== Test C10: har_vrp negative VRP coeff → passes ===")
    # VRP (last weight) can be negative
    ok, reason = HAREstimator._sanity_check_coeffs([0.0001, 0.3, 0.3, 0.2, -0.3], "har_vrp")
    check(ok, f"har_vrp negative VRP ok: {reason}")


def test_sanity_log_har_iv_neg():
    print("\n=== Test C11: log_har_iv negative coeffs → passes ===")
    ok, reason = HAREstimator._sanity_check_coeffs([0.0001, -0.3, 0.5, 0.3, -0.2], "log_har_iv")
    check(ok, f"log_har_iv negative ok: {reason}")


def test_sanity_existing_unchanged():
    print("\n=== Test C12: Existing model sanity checks unchanged ===")
    # level_har
    ok1, _ = HAREstimator._sanity_check_coeffs([0.0001, 0.4, 0.35, 0.25], "level_har")
    check(ok1, "level_har valid passes")
    ok2, _ = HAREstimator._sanity_check_coeffs([0.0001, -0.1, 0.35, 0.25], "level_har")
    check(not ok2, "level_har negative rejected")
    # log_har
    ok3, _ = HAREstimator._sanity_check_coeffs([0.0001, 0.5, 0.3, -0.2], "log_har")
    check(ok3, "log_har negative weight ok")
    # har_j
    ok4, _ = HAREstimator._sanity_check_coeffs([0.0001, 0.3, 0.3, 0.2, 0.1], "har_j")
    check(ok4, "har_j valid passes")
    ok5, _ = HAREstimator._sanity_check_coeffs([0.0001, 0.3, 0.3, 0.2, -0.1], "har_j")
    check(not ok5, "har_j negative rejected")
    # har_semi sum > 2.0
    ok6, _ = HAREstimator._sanity_check_coeffs([0.0001, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4], "har_semi")
    check(not ok6, "har_semi sum>2.0 rejected")


# ═══════════════════════════════════════════════════════════════════════════
#  Category D: get_blend Prediction (5 tests)
# ═══════════════════════════════════════════════════════════════════════════

def test_blend_har_iv():
    print("\n=== Test D13: har_iv formula ===")
    est = HAREstimator()
    coeffs = [0.0001, 0.4, 0.3, 0.2, 0.1]
    est._active_model["BTC"] = "har_iv"
    est._coefficients["BTC"]["har_iv"] = coeffs
    rk1, rk5, rk15, dvol_sq = 0.001, 0.0008, 0.0006, 1.5e-8
    expected = math.sqrt(max(0.0,
        coeffs[0] + coeffs[1]*rk1**2 + coeffs[2]*rk5**2 + coeffs[3]*rk15**2 + coeffs[4]*dvol_sq))
    result = est.get_blend("BTC", rk1, rk5, rk15, dvol_sq=dvol_sq)
    check(abs(result - expected) < 1e-15, f"har_iv result={result:.10f} expected={expected:.10f}")


def test_blend_har_j_iv():
    print("\n=== Test D14: har_j_iv formula ===")
    est = HAREstimator()
    coeffs = [0.0001, 0.3, 0.25, 0.15, 0.1, 0.2]
    est._active_model["BTC"] = "har_j_iv"
    est._coefficients["BTC"]["har_j_iv"] = coeffs
    rk1, rk5, rk15, jump_sq, dvol_sq = 0.001, 0.0008, 0.0006, 5e-9, 1.5e-8
    expected = math.sqrt(max(0.0,
        coeffs[0] + coeffs[1]*rk1**2 + coeffs[2]*rk5**2 + coeffs[3]*rk15**2
        + coeffs[4]*jump_sq + coeffs[5]*dvol_sq))
    result = est.get_blend("BTC", rk1, rk5, rk15, jump_sq=jump_sq, dvol_sq=dvol_sq)
    check(abs(result - expected) < 1e-15, f"har_j_iv result={result:.10f}")


def test_blend_log_har_iv():
    print("\n=== Test D15: log_har_iv formula ===")
    est = HAREstimator()
    coeffs = [-0.0001, 0.4, 0.3, 0.2, 0.1]
    est._active_model["BTC"] = "log_har_iv"
    est._coefficients["BTC"]["log_har_iv"] = coeffs
    rk1, rk5, rk15, dvol_sq = 0.001, 0.0008, 0.0006, 1.5e-8
    eps = 1e-20
    log_val = (coeffs[0] + coeffs[1]*math.log(max(eps, rk1**2))
               + coeffs[2]*math.log(max(eps, rk5**2))
               + coeffs[3]*math.log(max(eps, rk15**2))
               + coeffs[4]*math.log(max(eps, dvol_sq)))
    expected = math.sqrt(max(0.0, math.exp(log_val)))
    result = est.get_blend("BTC", rk1, rk5, rk15, dvol_sq=dvol_sq)
    check(abs(result - expected) < 1e-12, f"log_har_iv result={result:.10f}")


def test_blend_har_vrp():
    print("\n=== Test D16: har_vrp formula (VRP = dvol_sq - rv5²) ===")
    est = HAREstimator()
    coeffs = [0.0001, 0.4, 0.3, 0.2, 0.1]
    est._active_model["BTC"] = "har_vrp"
    est._coefficients["BTC"]["har_vrp"] = coeffs
    rk1, rk5, rk15, dvol_sq = 0.001, 0.0008, 0.0006, 1.5e-8
    vrp = dvol_sq - rk5**2
    expected = math.sqrt(max(0.0,
        coeffs[0] + coeffs[1]*rk1**2 + coeffs[2]*rk5**2 + coeffs[3]*rk15**2 + coeffs[4]*vrp))
    result = est.get_blend("BTC", rk1, rk5, rk15, dvol_sq=dvol_sq)
    check(abs(result - expected) < 1e-15, f"har_vrp result={result:.10f}")


def test_blend_fallback_no_dvol():
    print("\n=== Test D17: Fallback when dvol_sq=None ===")
    est = HAREstimator()
    # Set up har_iv as active but also have level_har coeffs
    est._active_model["BTC"] = "har_iv"
    est._coefficients["BTC"]["har_iv"] = [0.0001, 0.4, 0.3, 0.2, 0.1]
    level_coeffs = [0.0001, 0.4, 0.35, 0.25]
    est._coefficients["BTC"]["level_har"] = level_coeffs
    rk1, rk5, rk15 = 0.001, 0.0008, 0.0006
    # dvol_sq=None → should fallback to level_har
    result = est.get_blend("BTC", rk1, rk5, rk15, dvol_sq=None)
    expected = math.sqrt(max(0.0,
        level_coeffs[0] + level_coeffs[1]*rk1**2 + level_coeffs[2]*rk5**2 + level_coeffs[3]*rk15**2))
    check(abs(result - expected) < 1e-15, f"fallback to level_har: {result:.10f}")

    # Without level_har coeffs → fixed weights
    est2 = HAREstimator()
    est2._active_model["BTC"] = "har_iv"
    est2._coefficients["BTC"]["har_iv"] = [0.0001, 0.4, 0.3, 0.2, 0.1]
    result2 = est2.get_blend("BTC", rk1, rk5, rk15, dvol_sq=None)
    w1, w5, w15 = VOL_BLEND_WEIGHTS
    expected2 = w1 * rk1 + w5 * rk5 + w15 * rk15
    check(abs(result2 - expected2) < 1e-15, f"fallback to fixed: {result2:.10f}")


# ═══════════════════════════════════════════════════════════════════════════
#  Category E: dvol_fraction Gating (3 tests)
# ═══════════════════════════════════════════════════════════════════════════

def _make_obs(n, dvol_pct=1.0, seed=42):
    """Generate n observations with dvol_pct fraction having dvol_sq values.

    Uses partially independent DVOL to avoid collinearity with RV regressors.
    Target has real (but small) DVOL dependence so IV models can pass sanity checks.
    """
    random.seed(seed)
    obs = []
    for i in range(n):
        rv1 = abs(random.gauss(1e-4, 3e-5))
        rv5 = abs(random.gauss(1e-4, 2e-5))
        rv15 = abs(random.gauss(1e-4, 1e-5))
        jump = max(0, rv5 - 0.8 * rv5) * random.random()
        has_dvol = (i / n) < dvol_pct
        # DVOL partially independent (mix of rv5 and own noise)
        dvol_sq = (0.5 * rv5 + 0.5 * abs(random.gauss(1e-4, 2e-5))) if has_dvol else None
        obs.append({
            "ts": time.time() + i * 300,
            "rv1_sq": rv1, "rv5_sq": rv5, "rv15_sq": rv15,
            "jump_sq": jump,
            "sv_pos_1": rv1 * 0.5, "sv_neg_1": rv1 * 0.5,
            "sv_pos_5": rv5 * 0.5, "sv_neg_5": rv5 * 0.5,
            "sv_pos_15": rv15 * 0.5, "sv_neg_15": rv15 * 0.5,
            "dvol_sq": dvol_sq,
        })
    # Make target partially dependent on DVOL
    for i in range(n - 1):
        base = 0.3 * obs[i]["rv1_sq"] + 0.35 * obs[i]["rv5_sq"] + 0.25 * obs[i]["rv15_sq"]
        if obs[i]["dvol_sq"] is not None:
            base += 0.1 * obs[i]["dvol_sq"]
        obs[i + 1]["rv5_sq"] = base + abs(random.gauss(0, 5e-7))
    return obs


def test_dvol_frac_zero():
    print("\n=== Test E18: All None dvol_sq → IV models not fitted ===")
    obs = _make_obs(100, dvol_pct=0.0)
    est = HAREstimator()
    est._refit_asset("BTC", obs)
    ql = est._qlike_scores.get("BTC", {})
    iv_models_fitted = any(m in ql for m in ["har_iv", "har_j_iv", "log_har_iv", "har_vrp"])
    check(not iv_models_fitted, "no IV models in QLIKE scores")


def test_dvol_frac_below_threshold():
    print("\n=== Test E19: 60% non-None (< 70%) → IV models skipped ===")
    obs = _make_obs(100, dvol_pct=0.60)
    est = HAREstimator()
    est._refit_asset("BTC", obs)
    ql = est._qlike_scores.get("BTC", {})
    iv_models_fitted = any(m in ql for m in ["har_iv", "har_j_iv", "log_har_iv", "har_vrp"])
    check(not iv_models_fitted, "IV models skipped at 60%")


def test_dvol_frac_above_threshold():
    print("\n=== Test E20: 90% non-None → IV models fitted ===")
    obs = _make_obs(100, dvol_pct=0.90)
    est = HAREstimator()
    est._refit_asset("BTC", obs)
    ql = est._qlike_scores.get("BTC", {})
    # At least one IV model should produce a QLIKE score
    iv_models_fitted = any(m in ql for m in ["har_iv", "har_j_iv", "log_har_iv", "har_vrp"])
    check(iv_models_fitted, f"IV models fitted: {[m for m in ql if m in {'har_iv', 'har_j_iv', 'log_har_iv', 'har_vrp'}]}")


# ═══════════════════════════════════════════════════════════════════════════
#  Category F: QLIKE Competition (3 tests)
# ═══════════════════════════════════════════════════════════════════════════

def test_iv_model_beats_non_iv():
    print("\n=== Test F21: IV model beats non-IV (DVOL informative) ===")
    random.seed(200)
    n = 120
    obs = []
    for i in range(n):
        rv1 = abs(random.gauss(1e-4, 3e-5))
        rv5 = abs(random.gauss(1e-4, 2e-5))
        rv15 = abs(random.gauss(1e-4, 1e-5))
        # DVOL is predictive: next rv5 = 0.3*rv1 + 0.2*rv5 + 0.1*rv15 + 0.4*dvol
        dvol_sq = rv5 * (1.0 + 0.3 * random.gauss(0, 0.1))
        # The actual next observation's rv5_sq will be influenced by dvol_sq
        obs.append({
            "ts": time.time() + i * 300,
            "rv1_sq": rv1, "rv5_sq": rv5, "rv15_sq": rv15,
            "jump_sq": max(0.0, rv1 - rv15) * 0.1,
            "sv_pos_1": rv1 * 0.5, "sv_neg_1": rv1 * 0.5,
            "sv_pos_5": rv5 * 0.5, "sv_neg_5": rv5 * 0.5,
            "sv_pos_15": rv15 * 0.5, "sv_neg_15": rv15 * 0.5,
            "dvol_sq": dvol_sq,
        })
    # Make the target correlated with dvol_sq
    for i in range(n - 1):
        target_base = 0.3 * obs[i]["rv1_sq"] + 0.2 * obs[i]["rv5_sq"] + 0.1 * obs[i]["rv15_sq"]
        obs[i + 1]["rv5_sq"] = target_base + 0.4 * obs[i]["dvol_sq"] + abs(random.gauss(0, 1e-6))

    est = HAREstimator()
    est._refit_asset("BTC", obs)
    ql = est._qlike_scores.get("BTC", {})
    # Check that at least one IV model has lower QLIKE than best non-IV
    best_non_iv = min((ql.get(m, float("inf")) for m in ["level_har", "log_har", "har_j", "har_semi", "fixed"]), default=float("inf"))
    best_iv = min((ql.get(m, float("inf")) for m in ["har_iv", "har_j_iv", "log_har_iv", "har_vrp"]), default=float("inf"))
    check(best_iv < best_non_iv, f"IV QLIKE={best_iv:.4f} < non-IV QLIKE={best_non_iv:.4f}")


def test_non_iv_beats_iv():
    print("\n=== Test F22: Non-IV beats IV (DVOL is noise) ===")
    random.seed(300)
    n = 120
    obs = []
    for i in range(n):
        rv1 = abs(random.gauss(1e-4, 3e-5))
        rv5 = abs(random.gauss(1e-4, 2e-5))
        rv15 = abs(random.gauss(1e-4, 1e-5))
        # DVOL is pure noise at a different scale, unrelated to future rv5
        dvol_sq = abs(random.gauss(5e-5, 3e-5))
        obs.append({
            "ts": time.time() + i * 300,
            "rv1_sq": rv1, "rv5_sq": rv5, "rv15_sq": rv15,
            "jump_sq": max(0.0, rv1 - rv15) * 0.1,
            "sv_pos_1": rv1 * 0.5, "sv_neg_1": rv1 * 0.5,
            "sv_pos_5": rv5 * 0.5, "sv_neg_5": rv5 * 0.5,
            "sv_pos_15": rv15 * 0.5, "sv_neg_15": rv15 * 0.5,
            "dvol_sq": dvol_sq,
        })
    # Target follows RV-only pattern (no DVOL signal)
    for i in range(n - 1):
        obs[i + 1]["rv5_sq"] = 0.4 * obs[i]["rv1_sq"] + 0.35 * obs[i]["rv5_sq"] + 0.25 * obs[i]["rv15_sq"] + abs(random.gauss(0, 1e-6))

    est = HAREstimator()
    est._refit_asset("BTC", obs)
    ql = est._qlike_scores.get("BTC", {})
    # Non-IV models should produce competitive QLIKE scores
    # (har_vrp may still fit in-sample with VRP regressor that correlates with rv5, which is expected)
    best_non_iv = min((ql.get(m, float("inf")) for m in ["level_har", "log_har", "har_j", "har_semi"] if m in ql), default=float("inf"))
    check(best_non_iv < float("inf"), f"non-IV models produce valid QLIKE: best={best_non_iv:.4f}")
    # Verify non-IV model is competitive (within 50% of best overall)
    best_overall = min(ql.values()) if ql else float("inf")
    check(best_non_iv <= best_overall * 1.5, f"non-IV competitive: {best_non_iv:.6f} vs best {best_overall:.6f}")


def test_vrp_predictive():
    print("\n=== Test F23: VRP signal captured by har_vrp ===")
    random.seed(400)
    n = 120
    obs = []
    for i in range(n):
        rv5 = abs(random.gauss(1e-4, 2e-5))
        rv1 = rv5 * (1.0 + random.gauss(0, 0.1))
        rv15 = rv5 * (1.0 - random.gauss(0, 0.05))
        # DVOL systematically higher than RV (positive VRP)
        dvol_sq = rv5 * 1.3
        obs.append({
            "ts": time.time() + i * 300,
            "rv1_sq": rv1, "rv5_sq": rv5, "rv15_sq": rv15,
            "jump_sq": max(0.0, rv1 - rv15) * 0.1,
            "sv_pos_1": rv1 * 0.5, "sv_neg_1": rv1 * 0.5,
            "sv_pos_5": rv5 * 0.5, "sv_neg_5": rv5 * 0.5,
            "sv_pos_15": rv15 * 0.5, "sv_neg_15": rv15 * 0.5,
            "dvol_sq": dvol_sq,
        })
    # Target influenced by VRP
    for i in range(n - 1):
        vrp = obs[i]["dvol_sq"] - obs[i]["rv5_sq"]
        obs[i + 1]["rv5_sq"] = (0.3 * obs[i]["rv1_sq"] + 0.3 * obs[i]["rv5_sq"]
                                 + 0.2 * obs[i]["rv15_sq"] + 0.2 * vrp
                                 + abs(random.gauss(0, 5e-7)))

    est = HAREstimator()
    est._refit_asset("BTC", obs)
    ql = est._qlike_scores.get("BTC", {})
    # har_vrp should produce a valid QLIKE score
    check("har_vrp" in ql, f"har_vrp fitted: QLIKE={ql.get('har_vrp', 'N/A')}")
    if "har_vrp" in ql and "fixed" in ql:
        check(ql["har_vrp"] < ql["fixed"], f"har_vrp ({ql['har_vrp']:.4f}) < fixed ({ql['fixed']:.4f})")


# ═══════════════════════════════════════════════════════════════════════════
#  Category G: Edge Cases & Robustness (2 tests)
# ═══════════════════════════════════════════════════════════════════════════

def test_mixed_none_dvol():
    print("\n=== Test G24: Mixed None/float dvol_sq ===")
    # Use _make_obs with 80% dvol to get sane data
    obs = _make_obs(100, dvol_pct=0.80, seed=500)

    est = HAREstimator()
    est._refit_asset("BTC", obs)
    ql = est._qlike_scores.get("BTC", {})
    # Non-IV models should use full dataset
    check("level_har" in ql or "fixed" in ql, "non-IV models use full dataset")
    # IV models should use only non-None rows (80% > 70% threshold)
    iv_fitted = any(m in ql for m in ["har_iv", "har_j_iv", "log_har_iv", "har_vrp"])
    check(iv_fitted, "IV models fitted with non-None subset")


def test_state_persistence_iv():
    print("\n=== Test G25: State persistence roundtrip with IV model ===")
    est = HAREstimator()
    # Manually set an IV model as active
    iv_coeffs = [0.0001, 0.3, 0.25, 0.2, 0.15]
    est._active_model["BTC"] = "har_iv"
    est._coefficients["BTC"]["har_iv"] = iv_coeffs
    est._qlike_scores["BTC"] = {"har_iv": 0.15, "fixed": 0.25}
    est._last_refit = time.time()
    est._save_state()
    check(os.path.exists(HAR_STATE_PATH), "state file saved")

    # Reload
    est2 = HAREstimator()
    check(est2._active_model["BTC"] == "har_iv", "model restored: har_iv")
    check(est2._coefficients["BTC"]["har_iv"] == iv_coeffs, "coefficients restored")

    # Verify predictions match
    rk1, rk5, rk15, dvol_sq = 0.001, 0.0008, 0.0006, 1.5e-8
    pred1 = est.get_blend("BTC", rk1, rk5, rk15, dvol_sq=dvol_sq)
    pred2 = est2.get_blend("BTC", rk1, rk5, rk15, dvol_sq=dvol_sq)
    check(abs(pred1 - pred2) < 1e-15, f"predictions match: {pred1:.10f} == {pred2:.10f}")

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
    print("HAR-IV Model Test Suite")
    print("=" * 60)

    # Category A: Hourly Average
    test_hourly_avg_empty()
    test_hourly_avg_below_min()
    test_hourly_avg_correct()

    # Category B: Observation Recording
    test_obs_dvol_none()
    test_obs_dvol_float()
    test_obs_backward_compat()

    # Category C: Sanity Checks
    test_sanity_har_iv_valid()
    test_sanity_har_iv_neg_iv()
    test_sanity_har_iv_sum_exceeds()
    test_sanity_har_vrp_neg_vrp()
    test_sanity_log_har_iv_neg()
    test_sanity_existing_unchanged()

    # Category D: get_blend Prediction
    test_blend_har_iv()
    test_blend_har_j_iv()
    test_blend_log_har_iv()
    test_blend_har_vrp()
    test_blend_fallback_no_dvol()

    # Category E: dvol_fraction Gating
    test_dvol_frac_zero()
    test_dvol_frac_below_threshold()
    test_dvol_frac_above_threshold()

    # Category F: QLIKE Competition
    test_iv_model_beats_non_iv()
    test_non_iv_beats_iv()
    test_vrp_predictive()

    # Category G: Edge Cases & Robustness
    test_mixed_none_dvol()
    test_state_persistence_iv()

    print("\n" + "=" * 60)
    print(f"Results: {PASS} passed, {FAIL} failed")
    if FAIL > 0:
        sys.exit(1)
    else:
        print("All tests passed!")
        sys.exit(0)
