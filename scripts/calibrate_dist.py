#!/usr/bin/env python3
"""Calibrate probability distribution parameters per crypto asset.

Fetches 7 days of 1-minute candles from Coinbase Exchange API,
computes standardized log returns, fits Student-t (multiple df) and
NIG distributions per asset, and writes the best-fit parameters to
dist_config.json.

Usage:
    python3 scripts/calibrate_dist.py
"""

import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import requests
from scipy.stats import t as student_t, norminvgauss, kstest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.join(SCRIPT_DIR, "..")
OUTPUT_PATH = os.path.join(REPO_DIR, "dist_config.json")

ASSETS = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "XRP": "XRP-USD",
}

COINBASE_API = "https://api.exchange.coinbase.com"
GRANULARITY = 60  # 1-minute candles
FETCH_DAYS = 7
CHUNK_HOURS = 5  # ~300 candles per request (max 350)
REQUEST_DELAY = 0.4  # seconds between API calls

DF_CANDIDATES = [2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0, 8.0]
NIG_ADVANTAGE_THRESHOLD = 10.0  # log-likelihood improvement required to prefer NIG


def fetch_candles(product_id: str, days: int = FETCH_DAYS) -> list:
    """Fetch historical 1-minute candles from Coinbase Exchange API."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    all_candles = []

    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(hours=CHUNK_HOURS), end)
        params = {
            "start": chunk_start.isoformat(),
            "end": chunk_end.isoformat(),
            "granularity": GRANULARITY,
        }

        try:
            resp = requests.get(
                f"{COINBASE_API}/products/{product_id}/candles",
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            candles = resp.json()
            if isinstance(candles, list):
                all_candles.extend(candles)
        except Exception as e:
            print(f"  Warning: fetch failed for {product_id} chunk {chunk_start}: {e}",
                  file=sys.stderr)

        chunk_start = chunk_end
        time.sleep(REQUEST_DELAY)

    # Coinbase returns [time, low, high, open, close, volume]
    # Sort by timestamp ascending, deduplicate
    all_candles.sort(key=lambda c: c[0])
    seen = set()
    unique = []
    for c in all_candles:
        if c[0] not in seen:
            seen.add(c[0])
            unique.append(c)

    return unique


def compute_log_returns(candles: list) -> np.ndarray:
    """Compute log returns from consecutive 1-minute candles.

    Only includes returns where consecutive timestamps differ by exactly
    60 seconds (no gaps).
    """
    returns = []
    for i in range(1, len(candles)):
        dt = candles[i][0] - candles[i - 1][0]
        if dt == GRANULARITY:
            close_prev = candles[i - 1][4]
            close_curr = candles[i][4]
            if close_prev > 0 and close_curr > 0:
                returns.append(math.log(close_curr / close_prev))
    return np.array(returns)


def standardize(returns: np.ndarray) -> np.ndarray:
    """Standardize returns to zero mean and unit variance."""
    mu = np.mean(returns)
    sigma = np.std(returns, ddof=1)
    if sigma < 1e-12:
        return returns - mu
    return (returns - mu) / sigma


def fit_student_t(z_returns: np.ndarray, df_candidates: list) -> list:
    """Fit Student-t at each candidate df, return list of (df, loglik, ks_stat, ks_pval)."""
    results = []
    for df in df_candidates:
        loglik = np.sum(student_t.logpdf(z_returns, df=df))
        ks_stat, ks_pval = kstest(z_returns, "t", args=(df,))
        results.append({
            "df": df,
            "loglik": float(loglik),
            "ks_stat": float(ks_stat),
            "ks_pval": float(ks_pval),
        })
    return results


def fit_nig(z_returns: np.ndarray) -> dict:
    """Fit NIG distribution via MLE, return params and fit stats."""
    try:
        a, b, loc, scale = norminvgauss.fit(z_returns)
        loglik = float(np.sum(norminvgauss.logpdf(z_returns, a, b, loc=loc, scale=scale)))
        ks_stat, ks_pval = kstest(z_returns, "norminvgauss", args=(a, b, loc, scale))
        return {
            "a": float(a),
            "b": float(b),
            "loc": float(loc),
            "scale": float(scale),
            "loglik": loglik,
            "ks_stat": float(ks_stat),
            "ks_pval": float(ks_pval),
            "success": True,
        }
    except Exception as e:
        print(f"  Warning: NIG fit failed: {e}", file=sys.stderr)
        return {"success": False, "error": str(e)}


def select_best(t_results: list, nig_result: dict) -> dict:
    """Select best distribution for an asset."""
    # Best Student-t by log-likelihood
    best_t = max(t_results, key=lambda r: r["loglik"])

    if nig_result.get("success") and nig_result["loglik"] > best_t["loglik"] + NIG_ADVANTAGE_THRESHOLD:
        return {
            "distribution": "nig",
            "student_t_df": best_t["df"],
            "nig_params": {
                "a": nig_result["a"],
                "b": nig_result["b"],
                "loc": nig_result["loc"],
                "scale": nig_result["scale"],
            },
        }
    else:
        result = {
            "distribution": "student_t",
            "student_t_df": best_t["df"],
        }
        if nig_result.get("success"):
            result["nig_params"] = {
                "a": nig_result["a"],
                "b": nig_result["b"],
                "loc": nig_result["loc"],
                "scale": nig_result["scale"],
            }
        return result


def print_report(asset: str, n_returns: int, raw_stats: dict,
                 t_results: list, nig_result: dict, selection: dict):
    """Print human-readable calibration report for one asset."""
    print(f"\n{'=' * 60}")
    print(f"  {asset}  ({n_returns:,} returns)")
    print(f"  Raw: mean={raw_stats['mean']:.6f}  std={raw_stats['std']:.6f}  "
          f"skew={raw_stats['skew']:.3f}  kurtosis={raw_stats['kurtosis']:.2f}")
    print(f"{'=' * 60}")

    print(f"\n  {'df':>5s}  {'LogLik':>12s}  {'KS stat':>10s}  {'KS p-val':>10s}")
    print(f"  {'-' * 5}  {'-' * 12}  {'-' * 10}  {'-' * 10}")
    for r in t_results:
        marker = " <-- best" if r["df"] == selection["student_t_df"] and selection["distribution"] == "student_t" else ""
        print(f"  {r['df']:5.1f}  {r['loglik']:12.1f}  {r['ks_stat']:10.4f}  {r['ks_pval']:10.4f}{marker}")

    if nig_result.get("success"):
        marker = " <-- SELECTED" if selection["distribution"] == "nig" else ""
        print(f"\n  NIG    {nig_result['loglik']:12.1f}  {nig_result['ks_stat']:10.4f}  "
              f"{nig_result['ks_pval']:10.4f}{marker}")
        print(f"         a={nig_result['a']:.4f}  b={nig_result['b']:.4f}  "
              f"loc={nig_result['loc']:.4f}  scale={nig_result['scale']:.4f}")
    else:
        print(f"\n  NIG    fit failed: {nig_result.get('error', 'unknown')}")

    print(f"\n  Selected: {selection['distribution']} "
          f"(df={selection['student_t_df']:.1f})" if selection["distribution"] == "student_t"
          else f"\n  Selected: NIG")


def main():
    print("Distribution Calibration for Kalshi Crypto Bot")
    print(f"Fetching {FETCH_DAYS} days of 1-minute candles from Coinbase...")

    config = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_days": FETCH_DAYS,
        "return_interval_seconds": GRANULARITY,
        "assets": {},
        "defaults": {"distribution": "student_t", "student_t_df": 4},
    }

    for asset, product_id in ASSETS.items():
        print(f"\nFetching {asset} ({product_id})...")
        candles = fetch_candles(product_id, FETCH_DAYS)
        print(f"  Got {len(candles):,} candles")

        if len(candles) < 100:
            print(f"  ERROR: Too few candles for {asset}, skipping", file=sys.stderr)
            continue

        raw_returns = compute_log_returns(candles)
        print(f"  {len(raw_returns):,} valid returns (gaps filtered)")

        if len(raw_returns) < 50:
            print(f"  ERROR: Too few returns for {asset}, skipping", file=sys.stderr)
            continue

        raw_stats = {
            "mean": float(np.mean(raw_returns)),
            "std": float(np.std(raw_returns, ddof=1)),
            "skew": float(np.mean(((raw_returns - np.mean(raw_returns)) / np.std(raw_returns, ddof=1)) ** 3)),
            "kurtosis": float(np.mean(((raw_returns - np.mean(raw_returns)) / np.std(raw_returns, ddof=1)) ** 4)),
        }

        z_returns = standardize(raw_returns)

        print(f"  Fitting Student-t at df = {DF_CANDIDATES}...")
        t_results = fit_student_t(z_returns, DF_CANDIDATES)

        print(f"  Fitting NIG via MLE...")
        nig_result = fit_nig(z_returns)

        selection = select_best(t_results, nig_result)
        print_report(asset, len(raw_returns), raw_stats, t_results, nig_result, selection)

        # Build config entry
        best_t = max(t_results, key=lambda r: r["loglik"])
        fit_stats = {
            "student_t_best_df": best_t["df"],
            "student_t_loglik": best_t["loglik"],
            "student_t_ks_pvalue": best_t["ks_pval"],
            "n_returns": len(raw_returns),
            "raw_kurtosis": raw_stats["kurtosis"],
            "raw_skew": raw_stats["skew"],
        }
        if nig_result.get("success"):
            fit_stats["nig_loglik"] = nig_result["loglik"]
            fit_stats["nig_ks_pvalue"] = nig_result["ks_pval"]

        entry = dict(selection)
        entry["fit_stats"] = fit_stats
        config["assets"][asset] = entry

    # Write config
    with open(OUTPUT_PATH, "w") as f:
        json.dump(config, f, indent=2)
    print(f"\n\nConfig written to {OUTPUT_PATH}")

    # Summary table
    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    print(f"  {'Asset':>6s}  {'Distribution':>14s}  {'Best df':>8s}  {'Kurtosis':>10s}")
    print(f"  {'-' * 6}  {'-' * 14}  {'-' * 8}  {'-' * 10}")
    for asset, entry in config["assets"].items():
        dist = entry["distribution"]
        df = entry.get("student_t_df", "—")
        kurt = entry.get("fit_stats", {}).get("raw_kurtosis", 0)
        print(f"  {asset:>6s}  {dist:>14s}  {df:>8}  {kurt:>10.2f}")


if __name__ == "__main__":
    main()
