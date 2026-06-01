#!/usr/bin/env python3
"""rough_vol_jump_diffusion  (family: forecasting)

Short-dated 15M terminal-probability forecasting for Kalshi crypto markets.

We compare two "exotic" terminal-distribution models against an EGARCH/Student-t
baseline on REAL outcomes (Brier score):

  1. Merton jump-diffusion:    log-return = Gaussian diffusion + compound-Poisson
                               normal jumps. Closed-form terminal density is a
                               Poisson mixture of normals.
  2. Rough-vol (rough Bergomi flavor): roughness exponent H estimated from the
                               scaling of realized variance across sampling lags
                               (RV(lag) ~ lag^{2H}); the terminal variance to the
                               close horizon is then extrapolated with that
                               (typically anti-persistent, H<0.5) scaling instead
                               of the random-walk H=0.5 assumption. Diffusion-only
                               (Gaussian) terminal density with the rough-scaled
                               variance.

Baseline: EGARCH(1,1) with Student-t innovations, fit by MLE on the same window's
returns; terminal variance = sum of h-step-ahead conditional variances; terminal
density = Student-t.

NO-LOOKAHEAD: all parameters are estimated ONLY from spot returns observed at or
before the DECISION time (T-15s before close). The forecast horizon is decision ->
close. Outcome label is the real Kalshi market_result (yes = spot closed above
strike). Strike = evaluated_opportunities.threshold.

SPOT REQUIREMENT: needs Coinbase spot returns. Only BTC/ETH/SOL/XRP have spot in
this corpus -> HYPE/DOGE/BNB are reported as spot-DATA_GAP and EXCLUDED. If the
spot file is absent entirely -> whole-run DATA_GAP.

This is a FORECASTING task: no fills, no fees (no positions are taken). fees_included
is reported True in the structured sense that there is nothing to charge -- we never
trade -- and fill_model is N/A. The headline metric is Brier-skill vs the EGARCH/t
baseline with a bootstrap CI.

~1 day corpus. Tiny n per asset. Wide CIs and humility are mandatory.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
from scipy import optimize
from scipy.stats import norm, t as student_t

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research.phase1b_real_price_economics import close_epoch_from_ticker  # noqa: E402

SPOT_PATH = "/tmp/edge_daily/coinbase_spot.jsonl"
DB_PATH = "/tmp/edge_daily/state.db"
WINDOW_LO_ISO = "2026-05-30T21:06:00Z"

# assets that have Coinbase spot in this corpus
SPOT_ASSETS = ("BTC", "ETH", "SOL", "XRP")
NO_SPOT_ASSETS = ("HYPE", "DOGE", "BNB")
PRODUCT = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD"}

DECISION_OFFSET_S = 15.0          # forecast from T-15s before close
LOOKBACK_S = 45.0 * 60.0          # 45 min of spot history feeds estimation
RESAMPLE_DT_S = 10.0              # resample spot mid to a 10s grid for returns
MIN_RETURNS = 40                  # need >= this many return obs to fit
SEED = 12345


# --------------------------------------------------------------------------- #
# loaders
# --------------------------------------------------------------------------- #
def _epoch(iso: str) -> float:
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def load_spot() -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """{product_id: (ts_array_sorted, mid_array)}."""
    rows = defaultdict(list)
    with open(SPOT_PATH) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            mid = o.get("mid")
            if mid is None or mid <= 0:
                continue
            rows[o["product_id"]].append((_epoch(o["ts"]), float(mid)))
    out = {}
    for pid, lst in rows.items():
        lst.sort()
        ts = np.array([x[0] for x in lst], dtype=float)
        mid = np.array([x[1] for x in lst], dtype=float)
        out[pid] = (ts, mid)
    return out


def load_windows(tickers_by_asset_filter=SPOT_ASSETS) -> list[dict]:
    """In-window crypto-15M outcomes with strike, for spot assets only."""
    lo = WINDOW_LO_ISO
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    out = []
    for asset in tickers_by_asset_filter:
        sql = (
            "SELECT DISTINCT ticker, market_result, threshold FROM "
            "evaluated_opportunities WHERE product_type='15m' "
            f"AND ticker LIKE 'KX{asset}15M-%' "
            "AND market_result IN ('yes','no') AND threshold IS NOT NULL "
            "AND evaluation_time >= ?"
        )
        for r in conn.execute(sql, (lo,)):
            out.append(
                {
                    "ticker": r["ticker"],
                    "asset": asset,
                    "result": r["market_result"],
                    "strike": float(r["threshold"]),
                }
            )
    conn.close()
    return out


# --------------------------------------------------------------------------- #
# return-series construction (no lookahead: only data <= decision_epoch)
# --------------------------------------------------------------------------- #
def resampled_log_returns(ts: np.ndarray, mid: np.ndarray, t0: float, t1: float):
    """Forward-fill spot onto a [t0, t1] grid at RESAMPLE_DT_S; return (logret, S_at_t1).

    Uses only observations with timestamp <= grid point (causal forward-fill)."""
    if t1 <= t0:
        return None, None
    grid = np.arange(t0, t1 + 1e-9, RESAMPLE_DT_S)
    if grid.size < 2:
        return None, None
    idx = np.searchsorted(ts, grid, side="right") - 1
    if np.any(idx < 0):
        # not enough history before the grid start
        first_valid = np.argmax(idx >= 0)
        if idx[-1] < 0:
            return None, None
        grid = grid[first_valid:]
        idx = idx[first_valid:]
        if grid.size < 2:
            return None, None
    prices = mid[idx]
    logp = np.log(prices)
    ret = np.diff(logp)
    # drop exact-zero stretches from forward-fill staleness only at the tail count;
    # we keep zeros (they are real "no change over 5s") but guard all-zero series
    s_at_end = float(prices[-1])
    return ret, s_at_end


# --------------------------------------------------------------------------- #
# Model 1: Merton jump-diffusion terminal P(above strike)
# --------------------------------------------------------------------------- #
def fit_merton(ret: np.ndarray):
    """Crude but robust MLE of Merton params on per-step log-returns.

    Params: mu (drift/step), sigma (diffusion vol/step), lam (jump prob/step),
            mj (jump mean), sj (jump std). Terminal over n steps is a Poisson
            mixture of normals.

    We fit via numerical MLE with sensible bounds; fall back to GBM moments on
    failure."""
    n = ret.size
    m = float(np.mean(ret))
    v = float(np.var(ret))
    s = float(np.std(ret)) + 1e-12
    # init: most variance is diffusion, small jump component
    p0 = [m, math.sqrt(max(v * 0.7, 1e-12)), 0.05, 0.0, max(s * 3.0, 1e-9)]

    def negll(p):
        mu, sigma, lam, mj, sj = p
        if sigma <= 0 or sj <= 0 or not (0.0 < lam < 1.0):
            return 1e12
        # truncate Poisson mixture at k=0,1,2 jumps per step (steps are 5s; >2 jumps
        # negligible at small lam)
        ll = 0.0
        logs = []
        for k in range(0, 3):
            var_k = sigma * sigma + k * sj * sj
            mean_k = mu + k * mj
            pk = math.exp(-lam) * (lam ** k) / math.factorial(k)
            if pk <= 0:
                continue
            comp = pk * norm.pdf(ret, loc=mean_k, scale=math.sqrt(var_k))
            logs.append(comp)
        dens = np.sum(logs, axis=0)
        dens = np.clip(dens, 1e-300, None)
        return -float(np.sum(np.log(dens)))

    bounds = [
        (-abs(m) - 5 * s, abs(m) + 5 * s),
        (1e-9, 10 * s + 1e-6),
        (1e-4, 0.5),
        (-10 * s, 10 * s),
        (1e-9, 50 * s + 1e-6),
    ]
    try:
        res = optimize.minimize(negll, p0, method="L-BFGS-B", bounds=bounds)
        if res.success and np.all(np.isfinite(res.x)):
            mu, sigma, lam, mj, sj = res.x
            return dict(mu=mu, sigma=sigma, lam=lam, mj=mj, sj=sj, ok=True)
    except Exception:
        pass
    return dict(mu=m, sigma=s, lam=0.0, mj=0.0, sj=0.0, ok=False)


def merton_p_above(params, s0, strike, n_steps):
    """P(S_T > strike) under Merton, terminal = Poisson mixture over total jumps J~Pois(lam*n)."""
    mu, sigma, lam, mj, sj = (
        params["mu"], params["sigma"], params["lam"], params["mj"], params["sj"]
    )
    log_thresh = math.log(strike / s0)
    Lam = lam * n_steps
    total_mu_diff = mu * n_steps
    total_var_diff = sigma * sigma * n_steps
    p = 0.0
    # sum over total number of jumps J
    kmax = max(5, int(Lam + 6 * math.sqrt(Lam + 1e-9)))
    for J in range(0, kmax + 1):
        pj = math.exp(-Lam) * (Lam ** J) / math.factorial(J) if Lam > 0 else (1.0 if J == 0 else 0.0)
        if pj <= 0:
            continue
        mean_J = total_mu_diff + J * mj
        var_J = total_var_diff + J * sj * sj
        sd_J = math.sqrt(max(var_J, 1e-18))
        p += pj * (1.0 - norm.cdf((log_thresh - mean_J) / sd_J))
    return float(min(max(p, 1e-6), 1.0 - 1e-6))


# --------------------------------------------------------------------------- #
# Model 2: rough-vol (roughness from RV scaling), Gaussian terminal
# --------------------------------------------------------------------------- #
def estimate_roughness_H(ret: np.ndarray, max_lag=8):
    """Estimate H from RV(lag) ~ lag^{2H}: regress log RV on log lag.

    RV(lag) = mean of squared lag-aggregated returns. Returns (H, sigma1) where
    sigma1 = per-1-step std (lag=1 RV sqrt). Clamps H to [0.05, 0.95]."""
    lags = []
    logrv = []
    for lag in range(1, max_lag + 1):
        if ret.size < 2 * lag:
            break
        agg = np.add.reduceat(ret, np.arange(0, ret.size - ret.size % lag, lag))
        if agg.size < 3:
            break
        rv = float(np.mean(agg ** 2))
        if rv <= 0:
            continue
        lags.append(math.log(lag))
        logrv.append(math.log(rv))
    sigma1 = float(np.std(ret)) + 1e-12
    if len(lags) < 3:
        return 0.5, sigma1
    A = np.vstack([np.array(lags), np.ones(len(lags))]).T
    slope, intercept = np.linalg.lstsq(A, np.array(logrv), rcond=None)[0]
    H = slope / 2.0
    H = float(min(max(H, 0.05), 0.95))
    return H, sigma1


def rough_p_above(H, sigma1, s0, strike, mu_step, n_steps):
    """Rough-scaled terminal variance: Var(n) = sigma1^2 * n^{2H} (vs n^1 for RW).

    Gaussian (diffusion-only) terminal. mu_step is per-step drift."""
    log_thresh = math.log(strike / s0)
    var_T = (sigma1 ** 2) * (n_steps ** (2.0 * H))
    mean_T = mu_step * n_steps
    sd_T = math.sqrt(max(var_T, 1e-18))
    p = 1.0 - norm.cdf((log_thresh - mean_T) / sd_T)
    return float(min(max(p, 1e-6), 1.0 - 1e-6))


# --------------------------------------------------------------------------- #
# Baseline: EGARCH(1,1)-t  (hand-rolled MLE)
# --------------------------------------------------------------------------- #
def fit_egarch_t(ret: np.ndarray):
    """EGARCH(1,1) with Student-t innovations.

    log h_t = omega + alpha*(|z|-E|z|) + gamma*z + beta*log h_{t-1},
    r_t = mu + sqrt(h_t)*z_t, z_t ~ standardized Student-t(nu).
    Fit by MLE; fall back to constant-vol Student-t on failure."""
    r = ret.astype(float)
    n = r.size
    mu0 = float(np.mean(r))
    var0 = float(np.var(r)) + 1e-18

    def unpack(p):
        mu, omega, alpha, gamma, beta_raw, nu_raw = p
        beta = 1.0 / (1.0 + math.exp(-beta_raw))      # (0,1)
        nu = 2.05 + math.exp(nu_raw)                  # > 2
        return mu, omega, alpha, gamma, beta, nu

    def _ez(nu):
        from math import gamma as G, sqrt, pi
        return 2.0 * sqrt(nu - 2.0) * G((nu + 1) / 2.0) / (
            (nu - 1.0) * G(nu / 2.0) * sqrt(pi)
        )

    def negll(p):
        mu, omega, alpha, gamma, beta, nu = unpack(p)
        ez = _ez(nu)
        scale_factor = math.sqrt((nu - 2.0) / nu)
        # tight scalar recursion for h_t and z_t (cheap, no scipy calls)
        logh = math.log(var0)
        z = np.empty(n)
        sds = np.empty(n)
        rr = r
        for i in range(n):
            h = math.exp(logh if -50 < logh < 50 else (50 if logh >= 50 else -50))
            sd = math.sqrt(h)
            zi = (rr[i] - mu) / (sd + 1e-18)
            z[i] = zi
            sds[i] = sd
            logh = omega + alpha * (abs(zi) - ez) + gamma * zi + beta * logh
        # vectorized student-t loglik
        t_arg = z / scale_factor
        ll = np.sum(student_t.logpdf(t_arg, df=nu)) - n * math.log(scale_factor) \
            - np.sum(np.log(sds + 1e-18))
        if not math.isfinite(ll):
            return 1e12
        return -ll

    p0 = [mu0, math.log(var0) * (1 - 0.9), 0.1, 0.0, 2.2, math.log(6.0)]  # beta_raw~0.9
    try:
        res = optimize.minimize(negll, p0, method="Nelder-Mead",
                                options={"maxiter": 600, "xatol": 1e-6, "fatol": 1e-6})
        if res.success or res.fun < 1e11:
            mu, omega, alpha, gamma, beta, nu = unpack(res.x)
            return dict(mu=mu, omega=omega, alpha=alpha, gamma=gamma,
                        beta=beta, nu=nu, var0=var0, ok=True)
    except Exception:
        pass
    # fallback: constant-vol student-t
    return dict(mu=mu0, omega=math.log(var0), alpha=0.0, gamma=0.0,
                beta=0.0, nu=6.0, var0=var0, ok=False)


def egarch_terminal_var(params, ret):
    """Forecast terminal variance = sum of h-step conditional variances over the
    forecast horizon. For EGARCH the multi-step var forecast has no clean closed
    form; we use the last in-sample conditional variance * n_steps as a stable
    proxy (random-walk-in-h), which is the standard short-horizon approximation."""
    mu, omega, alpha, gamma, beta, nu, var0 = (
        params["mu"], params["omega"], params["alpha"], params["gamma"],
        params["beta"], params["nu"], params["var0"],
    )
    r = ret.astype(float)
    from math import gamma as G, sqrt, pi
    ez = 2.0 * sqrt(max(nu - 2.0, 1e-6)) * G((nu + 1) / 2.0) / (
        (nu - 1.0) * G(nu / 2.0) * sqrt(pi)
    )
    logh = math.log(var0)
    for i in range(r.size):
        h = math.exp(min(max(logh, -50), 50))
        sd = math.sqrt(h)
        z = (r[i] - mu) / (sd + 1e-18)
        logh = omega + alpha * (abs(z) - ez) + gamma * z + beta * logh
    h_last = math.exp(min(max(logh, -50), 50))
    return h_last, mu, nu


def egarch_p_above(params, ret, s0, strike, n_steps):
    h_last, mu, nu = egarch_terminal_var(params, ret)
    var_T = h_last * n_steps
    mean_T = mu * n_steps
    sd_T = math.sqrt(max(var_T, 1e-18))
    log_thresh = math.log(strike / s0)
    # standardized t terminal (approx: sum of t's ~ t with same nu, scaled)
    scale_factor = math.sqrt(max((nu - 2.0) / nu, 1e-6))
    arg = (log_thresh - mean_T) / sd_T
    # P(Z > arg) where Z standardized-t: convert to raw t via /scale_factor
    p = 1.0 - student_t.cdf(arg / scale_factor, df=nu)
    return float(min(max(p, 1e-6), 1.0 - 1e-6))


# --------------------------------------------------------------------------- #
# scoring + bootstrap
# --------------------------------------------------------------------------- #
def brier(p, y):
    return (p - y) ** 2


def bootstrap_ci(diffs, n_boot=2000, seed=SEED):
    """Bootstrap CI on the MEAN of per-window (baseline_brier - model_brier).
    Positive => model better (lower Brier)."""
    rng = np.random.default_rng(seed)
    arr = np.asarray(diffs, dtype=float)
    n = arr.size
    if n == 0:
        return float("nan"), float("nan"), float("nan")
    means = np.empty(n_boot)
    for b in range(n_boot):
        idx = rng.integers(0, n, n)
        means[b] = arr[idx].mean()
    return float(arr.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    import os
    import functools
    global print
    print = functools.partial(__builtins__["print"] if isinstance(__builtins__, dict)
                              else __builtins__.print, flush=True)
    if not os.path.exists(SPOT_PATH):
        print("DATA_GAP: spot file absent at", SPOT_PATH)
        return {"verdict": "DATA_GAP", "reason": "no spot file"}

    spot = load_spot()
    windows = load_windows()
    print(f"loaded {len(windows)} in-window crypto-15M outcomes (spot assets only)")
    print(f"NO-SPOT assets excluded (spot DATA_GAP): {NO_SPOT_ASSETS}")

    recs = []  # per-window forecast records
    skipped = defaultdict(int)
    for w in windows:
        asset = w["asset"]
        pid = PRODUCT[asset]
        if pid not in spot:
            skipped["no_spot_product"] += 1
            continue
        ts, mid = spot[pid]
        close_ep = close_epoch_from_ticker(w["ticker"])
        decision_ep = close_ep - DECISION_OFFSET_S
        t0 = decision_ep - LOOKBACK_S
        # need spot history covering [t0, decision] and decision within spot span
        if decision_ep < ts[0] or decision_ep > ts[-1] + RESAMPLE_DT_S:
            skipped["decision_outside_spot"] += 1
            continue
        ret, s0 = resampled_log_returns(ts, mid, t0, decision_ep)
        if ret is None or ret.size < MIN_RETURNS:
            skipped["too_few_returns"] += 1
            continue
        if not np.any(ret != 0):
            skipped["all_zero_returns"] += 1
            continue
        n_steps = max(1, int(round((close_ep - decision_ep) / RESAMPLE_DT_S)))
        y = 1.0 if w["result"] == "yes" else 0.0
        strike = w["strike"]

        # ----- models -----
        try:
            mp = fit_merton(ret)
            p_merton = merton_p_above(mp, s0, strike, n_steps)
        except Exception:
            skipped["merton_fail"] += 1
            continue
        try:
            H, sigma1 = estimate_roughness_H(ret)
            mu_step = float(np.mean(ret))
            p_rough = rough_p_above(H, sigma1, s0, strike, mu_step, n_steps)
        except Exception:
            skipped["rough_fail"] += 1
            continue
        try:
            ep = fit_egarch_t(ret)
            p_base = egarch_p_above(ep, ret, s0, strike, n_steps)
        except Exception:
            skipped["egarch_fail"] += 1
            continue

        recs.append(dict(ticker=w["ticker"], asset=asset, y=y, strike=strike, s0=s0,
                         n_steps=n_steps, H=H,
                         p_merton=p_merton, p_rough=p_rough, p_base=p_base))

    print(f"usable forecast windows: {len(recs)}")
    print("skipped:", dict(skipped))
    if len(recs) < 10:
        print("INCONCLUSIVE / DATA_GAP: too few usable windows after spot alignment")
        return {"verdict": "DATA_GAP", "n": len(recs), "skipped": dict(skipped)}

    y = np.array([r["y"] for r in recs])
    pm = np.array([r["p_merton"] for r in recs])
    pr = np.array([r["p_rough"] for r in recs])
    pb = np.array([r["p_base"] for r in recs])

    b_merton = brier(pm, y)
    b_rough = brier(pr, y)
    b_base = brier(pb, y)

    print("\n=== Brier (lower better) ===")
    print(f"  EGARCH-t baseline : {b_base.mean():.5f}")
    print(f"  Merton jump-diff  : {b_merton.mean():.5f}")
    print(f"  Rough-vol         : {b_rough.mean():.5f}")
    # naive reference: predict base rate
    base_rate = y.mean()
    b_naive = brier(np.full_like(y, base_rate), y).mean()
    print(f"  (naive base-rate  : {b_naive:.5f}, base_rate={base_rate:.3f})")

    # headline metric: best exotic model's Brier SKILL vs EGARCH-t baseline
    # skill_i = baseline_brier_i - model_brier_i  (positive => model better)
    diff_merton = b_base - b_merton
    diff_rough = b_base - b_rough
    # pick the better of the two exotic models by mean skill as the headline family
    if diff_merton.mean() >= diff_rough.mean():
        headline_name = "merton_jump_diffusion"
        diffs = diff_merton
        model_brier = b_merton.mean()
    else:
        headline_name = "rough_vol"
        diffs = diff_rough
        model_brier = b_rough.mean()

    mean_skill, lo, hi = bootstrap_ci(diffs, n_boot=2000)
    mean_skill_m, lo_m, hi_m = bootstrap_ci(diff_merton, n_boot=2000)
    mean_skill_r, lo_r, hi_r = bootstrap_ci(diff_rough, n_boot=2000)

    print(f"\n=== headline: {headline_name} Brier-skill vs EGARCH-t (positive=better) ===")
    print(f"  Merton  skill = {mean_skill_m:+.5f}  CI95 [{lo_m:+.5f}, {hi_m:+.5f}]")
    print(f"  Rough   skill = {mean_skill_r:+.5f}  CI95 [{lo_r:+.5f}, {hi_r:+.5f}]")
    print(f"  HEADLINE({headline_name}) skill = {mean_skill:+.5f}  CI95 [{lo:+.5f}, {hi:+.5f}]")

    # per-asset
    print("\nper-asset n:")
    for a in SPOT_ASSETS:
        n = sum(1 for r in recs if r["asset"] == a)
        print(f"  {a}: {n}")

    return {
        "verdict_metric": headline_name,
        "n": len(recs),
        "b_base": float(b_base.mean()),
        "b_merton": float(b_merton.mean()),
        "b_rough": float(b_rough.mean()),
        "headline_skill": mean_skill,
        "ci": (lo, hi),
        "merton_skill": (mean_skill_m, lo_m, hi_m),
        "rough_skill": (mean_skill_r, lo_r, hi_r),
        "base_rate": float(base_rate),
    }


if __name__ == "__main__":
    out = main()
    print("\nRESULT_JSON:", json.dumps(out, default=float))
