#!/usr/bin/env python3
"""
brownian_bridge_firstpassage  (family: forecasting)
====================================================

Question: for the Kalshi crypto-15M *above/below* window, can a Brownian /
first-passage terminal-probability forecast beat the PRODUCTION Student-t
terminal-CDF baseline on real outcomes, measured by Brier score with a
bootstrap CI?

DATA
----
We backtest directly off evaluated_opportunities in /tmp/edge_daily/state.db.
Every row is a point-in-time decision tick the live bot recorded, carrying:
  - spot_price          : decision-time spot (the bot's own feed; NO lookahead)
  - threshold           : the strike
  - seconds_to_close    : time-to-expiry T (seconds)
  - volatility          : per-second price-return sigma the bot used
  - raw_prob            : PRODUCTION terminal prob for this side (Student-t)   [baseline A]
  - calibrated_prob     : PRODUCTION post-calibration prob for this side       [baseline B]
  - market_result       : realized settlement ('yes' = spot_T above strike)
We filter side='yes' so raw_prob / calibrated_prob / our forecast are all
directly P(spot_T above strike), and the binary label is market_result=='yes'.

MODEL (challenger)
------------------
Terminal Brownian forecast under arithmetic Brownian motion of spot with
zero drift (martingale; "what would Jane Street do" = price off fair value, no
directional view):
    sd_T   = spot * volatility * sqrt(T)
    z      = (spot - strike) / sd_T
    P_term = Phi(z)                     # P(spot_T >= strike), normal terminal CDF

This is the Brownian *terminal* law. For a TERMINAL-settled above/below
contract the Brownian-BRIDGE / first-passage machinery collapses to exactly
this terminal CDF -- the bridge only changes the answer for a TOUCH/barrier
payoff. Kalshi 15M settles on terminal spot vs strike, so the honest
"Brownian-bridge first-passage" forecast for THIS payoff IS P_term. We ALSO
compute and report the genuine first-passage "stayed strictly above for the
whole remaining window" probability as a documented secondary diagnostic
(it is the WRONG payoff for this contract and is expected to be worse; we
report it only to be transparent about the distinction):
    P_stay_above = 1 - exp(-2 * b_up * b_dn / var_T)   for spot>strike   (reflection)
which is < P_term, i.e. it would systematically under-forecast.

BASELINE / METRIC
-----------------
Headline metric = Brier(challenger) - Brier(production raw_prob), i.e. the
DELTA. Negative delta => challenger is BETTER (lower Brier). We bootstrap the
delta by resampling TICKERS (cluster bootstrap) so repeated ticks within a
window do not inflate n. We also report Brier vs calibrated_prob.

FEES: none modeled -- this is a forecasting-only evaluation (no fills, no
trading). The metric is pure probabilistic-forecast quality (Brier). fees
are irrelevant to a Brier comparison and we say so honestly.
"""
from __future__ import annotations

import math
import sqlite3
import sys
import random
from collections import defaultdict

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

DB = "/tmp/edge_daily/state.db"
WINDOW_START_ISO = "2026-05-30T21:06:00Z"
SPOT_ASSETS = {"BTC", "ETH", "SOL", "XRP"}  # have Coinbase spot file; rest are DB-spot only
N_BOOT = 2000
SEED = 12345


def ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def load_rows():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    sql = (
        "SELECT ticker, asset, seconds_to_close, spot_price, threshold, "
        "volatility, raw_prob, calibrated_prob, market_result "
        "FROM evaluated_opportunities "
        "WHERE product_type='15m' AND side='yes' "
        "AND market_result IN ('yes','no') AND threshold IS NOT NULL "
        "AND evaluation_time >= ?"
    )
    return conn.execute(sql, (WINDOW_START_ISO,)).fetchall()


def build_samples(rows):
    """Return list of dicts with the inputs + the three P(above) forecasts + label."""
    samples = []
    for r in rows:
        sp, K, T, vol = r["spot_price"], r["threshold"], r["seconds_to_close"], r["volatility"]
        rawp, calp = r["raw_prob"], r["calibrated_prob"]
        if None in (sp, K, T, vol, rawp) or sp <= 0 or vol <= 0 or T <= 0:
            continue
        sd = sp * vol * math.sqrt(T)
        if sd <= 0:
            continue
        z = (sp - K) / sd
        p_term = ncdf(z)  # Brownian terminal = bridge first-passage for terminal payoff

        # genuine first-passage "stayed above the whole window" (diagnostic only)
        var_T = sd * sd
        if sp > K:
            # P(min over [0,T] stays >= K) given start sp>K, drift 0:
            # = 1 - P(hit K) = 1 - exp(-2*(sp-K)*(sp-K)/var_T) ... but that's the
            # reflection touch prob for a single barrier; stay-above for one-sided:
            b = sp - K
            p_stay = max(0.0, 1.0 - math.exp(-2.0 * b * b / var_T))
        else:
            p_stay = 0.0  # already at/below strike => "stayed strictly above" ~ 0

        label = 1.0 if r["market_result"] == "yes" else 0.0
        samples.append(
            {
                "ticker": r["ticker"],
                "asset": r["asset"],
                "label": label,
                "p_term": min(max(p_term, 1e-6), 1 - 1e-6),
                "p_stay": min(max(p_stay, 1e-6), 1 - 1e-6),
                "p_raw": min(max(rawp, 1e-6), 1 - 1e-6),
                "p_cal": min(max(calp, 1e-6), 1 - 1e-6) if calp is not None else None,
            }
        )
    return samples


def brier(samples, key):
    return sum((s[key] - s["label"]) ** 2 for s in samples) / len(samples)


def cluster_bootstrap_delta(samples, chal_key, base_key, n_boot=N_BOOT, seed=SEED):
    """Bootstrap Brier(chal) - Brier(base) by resampling tickers (clusters)."""
    rng = random.Random(seed)
    by_ticker = defaultdict(list)
    for s in samples:
        by_ticker[s["ticker"]].append(s)
    tickers = list(by_ticker.keys())
    deltas = []
    for _ in range(n_boot):
        boot = []
        for _ in range(len(tickers)):
            t = rng.choice(tickers)
            boot.extend(by_ticker[t])
        b_chal = sum((s[chal_key] - s["label"]) ** 2 for s in boot) / len(boot)
        b_base = sum((s[base_key] - s["label"]) ** 2 for s in boot) / len(boot)
        deltas.append(b_chal - b_base)
    deltas.sort()
    lo = deltas[int(0.025 * n_boot)]
    hi = deltas[int(0.975 * n_boot)]
    point = brier(samples, chal_key) - brier(samples, base_key)
    return point, lo, hi


def report(label, samples):
    if not samples:
        print(f"[{label}] n=0 -- no usable rows")
        return None
    n = len(samples)
    nt = len(set(s["ticker"] for s in samples))
    base_rate = sum(s["label"] for s in samples) / n
    b_term = brier(samples, "p_term")
    b_stay = brier(samples, "p_stay")
    b_raw = brier(samples, "p_raw")
    cal_ok = [s for s in samples if s["p_cal"] is not None]
    b_cal = (sum((s["p_cal"] - s["label"]) ** 2 for s in cal_ok) / len(cal_ok)) if cal_ok else float("nan")
    print(f"\n=== {label} ===")
    print(f"n_ticks={n}  n_tickers={nt}  base_rate(above)={base_rate:.3f}")
    print(f"Brier  Brownian-terminal (challenger) = {b_term:.5f}")
    print(f"Brier  first-passage stay-above (diag) = {b_stay:.5f}")
    print(f"Brier  production raw_prob (baseline)  = {b_raw:.5f}")
    print(f"Brier  production calibrated_prob       = {b_cal:.5f}")
    pt, lo, hi = cluster_bootstrap_delta(samples, "p_term", "p_raw")
    print(f"DELTA Brier (Brownian - raw_prob) = {pt:+.5f}  95%CI [{lo:+.5f}, {hi:+.5f}]  (neg=challenger better)")
    ptc, loc, hic = cluster_bootstrap_delta(samples, "p_term", "p_cal")
    print(f"DELTA Brier (Brownian - calibrated) = {ptc:+.5f}  95%CI [{loc:+.5f}, {hic:+.5f}]")
    return {
        "n": n, "n_tickers": nt, "base_rate": base_rate,
        "b_term": b_term, "b_raw": b_raw, "b_cal": b_cal,
        "delta_vs_raw": pt, "ci_lo": lo, "ci_hi": hi,
        "delta_vs_cal": ptc, "ci_lo_cal": loc, "ci_hi_cal": hic,
    }


def main():
    rows = load_rows()
    samples = build_samples(rows)
    print(f"loaded {len(rows)} side='yes' in-window rows; {len(samples)} usable after input checks")

    overall = report("ALL 7 ASSETS (DB decision-time spot)", samples)

    spot_only = [s for s in samples if s["asset"] in SPOT_ASSETS]
    report("BTC/ETH/SOL/XRP only (have Coinbase spot file)", spot_only)

    # per-asset thin slices (humility)
    print("\n--- per-asset (thin; wide CIs) ---")
    by_asset = defaultdict(list)
    for s in samples:
        by_asset[s["asset"]].append(s)
    for a in sorted(by_asset):
        sa = by_asset[a]
        bt = brier(sa, "p_term")
        br = brier(sa, "p_raw")
        print(f"  {a:5s} n={len(sa):4d} tk={len(set(s['ticker'] for s in sa)):3d} "
              f"Brier_term={bt:.4f} Brier_raw={br:.4f} delta={bt-br:+.4f}")

    return overall, samples


if __name__ == "__main__":
    main()
