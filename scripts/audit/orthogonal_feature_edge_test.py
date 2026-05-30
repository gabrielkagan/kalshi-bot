#!/usr/bin/env python3
"""Market-orthogonal feature edge test (read-only).

Question
--------
The market price is a better forecaster than the bot's model on every asset
(market Brier < model Brier, 30d audit 2026-05-28). So recalibration is a dead
end. The ONLY way to beat the market is a feature carrying information the
market price does NOT already contain. This script tests whether the
candidate market-orthogonal features — cross-exchange spot gap and Kalshi
order-flow — add predictive value *beyond the market price*, out of sample.

Anti-fooling design
-------------------
For each asset we fit, on a TEMPORAL train split, three forecasters and score
them on the held-out TEST split by Brier (lower = better):

  1. MARKET    : market_price/100, unmodified (the bar to beat).
  2. RECAL     : logistic(logit(market_prob))            — market recalibrated.
  3. FULL      : logistic(logit(market_prob) + orthogonal features).

The market price enters models 2 and 3 as a feature, so the orthogonal
features in model 3 are only credited for what they add BEYOND the price.
The number that matters is  Brier(RECAL) - Brier(FULL)  on the test set:
  > 0 (FULL lower)  => orthogonal features carry market-orthogonal signal.
  ~ 0              => they don't; the market already prices them in.

We compare RECAL (not MARKET) to FULL so that "the features helped" can't be
faked by mere recalibration. We also print Brier(MARKET) vs Brier(RECAL) — if
the market is already calibrated those should be ~equal (expected).

No class weighting (it trades calibration for recall and worsens Brier — see
the A1.b ablation lesson). Temporal split, train-only scaler fit: no
look-ahead. Read-only; run against a COPY of state.db.

Usage
-----
    python scripts/audit/orthogonal_feature_edge_test.py --db state.db \
        [--since 2026-04-01] [--min-test 200] [--band 85-96]
"""

import argparse
import math
import os
import sqlite3
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

CRYPTO_ASSETS = ("BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB")

# Candidate market-orthogonal features (used per-asset only if coverage is OK).
CANDIDATE_FEATURES = [
    "spot_coinbase_kraken_gap_bps",
    "kalshi_flow_depth_velocity",
    "kalshi_flow_depth_drain",
    "oft_imbalance_ratio",
]
MIN_COVERAGE = 0.40           # require >=40% non-null on the windowed set to use a feature
TRAIN_FRAC = 0.70


def connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def section(t: str) -> None:
    print(f"\n{'=' * 84}\n  {t}\n{'=' * 84}")


def logit(p: float) -> float:
    p = min(0.99, max(0.01, p))
    return math.log(p / (1.0 - p))


def brier(pred: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((pred - y) ** 2))


def select_features(rows: List[sqlite3.Row]) -> List[str]:
    """Keep candidate features with >= MIN_COVERAGE non-null on this row set."""
    n = len(rows)
    keep = []
    for f in CANDIDATE_FEATURES:
        cov = sum(1 for r in rows if r[f] is not None) / n if n else 0.0
        if cov >= MIN_COVERAGE:
            keep.append(f)
    return keep


def build_matrix(rows: List[sqlite3.Row], feats: List[str]
                 ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (X_orthogonal, market_logit_col, y) for rows with all feats present."""
    X, mkt, y = [], [], []
    for r in rows:
        if r["market_price"] is None or any(r[f] is None for f in feats):
            continue
        X.append([float(r[f]) for f in feats])
        mkt.append(logit(r["market_price"] / 100.0))
        y.append(1.0 if r["market_result"] == "yes" else 0.0)
    return np.array(X), np.array(mkt).reshape(-1, 1), np.array(y)


def fit_brier(Xtr, ytr, Xte, yte) -> float:
    """Fit a logistic model and return TEST Brier. Handles single-class train."""
    if len(set(ytr.tolist())) < 2:
        # Degenerate train (all one class): predict the train base rate.
        base = float(np.mean(ytr))
        return brier(np.full_like(yte, base), yte)
    m = LogisticRegression(max_iter=1000, C=1.0)  # no class_weight (Brier!)
    m.fit(Xtr, ytr)
    p = m.predict_proba(Xte)[:, 1]
    return brier(p, yte)


def run_asset(asset: str, rows: List[sqlite3.Row], min_test: int) -> Optional[dict]:
    rows = sorted(rows, key=lambda r: r["evaluation_time"])  # temporal order
    feats = select_features(rows)
    if not feats:
        return {"asset": asset, "skip": "no orthogonal feature >=40% coverage"}
    Xo, mkt, y = build_matrix(rows, feats)
    n = len(y)
    if n < int(min_test / (1 - TRAIN_FRAC)):
        return {"asset": asset, "skip": f"only {n} complete-feature rows", "feats": feats}

    cut = int(n * TRAIN_FRAC)
    yte = y[cut:]
    n_test = len(yte)
    if n_test < min_test:
        return {"asset": asset, "skip": f"test split {n_test} < {min_test}", "feats": feats}

    # Standardize orthogonal features on TRAIN only (no look-ahead).
    sc = StandardScaler().fit(Xo[:cut])
    Xo_s = sc.transform(Xo)

    mkt_tr, mkt_te = mkt[:cut], mkt[cut:]
    full_tr = np.hstack([mkt[:cut], Xo_s[:cut]])
    full_te = np.hstack([mkt[cut:], Xo_s[cut:]])
    ytr = y[:cut]

    b_market = brier(np.array([1 / (1 + math.exp(-v)) for v in mkt_te.ravel()]), yte)
    b_recal = fit_brier(mkt_tr, ytr, mkt_te, yte)
    b_full = fit_brier(full_tr, ytr, full_te, yte)

    return {
        "asset": asset, "feats": feats, "n_train": cut, "n_test": n_test,
        "b_market": b_market, "b_recal": b_recal, "b_full": b_full,
        "ortho_value": b_recal - b_full,   # >0 => features helped, out of sample
    }


def fetch(conn, since: Optional[str], band: Optional[Tuple[int, int]]) -> Dict[str, list]:
    cols = ", ".join(["asset", "market_price", "market_result", "evaluation_time"]
                     + CANDIDATE_FEATURES)
    sql = f"""SELECT {cols} FROM evaluated_opportunities
              WHERE (product_type='15m' OR product_type IS NULL)
                AND asset IN ({",".join("?" * len(CRYPTO_ASSETS))})
                AND market_result IN ('yes','no')"""
    params: List = list(CRYPTO_ASSETS)
    if since:
        sql += " AND evaluation_time >= ?"; params.append(since)
    if band:
        sql += " AND market_price >= ? AND market_price <= ?"; params += [band[0], band[1]]
    out: Dict[str, list] = {a: [] for a in CRYPTO_ASSETS}
    for r in conn.execute(sql, params):
        out[r["asset"]].append(r)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="state.db")
    ap.add_argument("--since", default="2026-04-01")
    ap.add_argument("--min-test", type=int, default=200)
    ap.add_argument("--band", default=None, help="market_price band e.g. 85-96")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: db not found at {args.db}", file=sys.stderr); return 64
    band = None
    if args.band:
        lo, hi = args.band.split("-"); band = (int(lo), int(hi))

    conn = connect_db(args.db)
    by_asset = fetch(conn, args.since, band)

    section(f"ORTHOGONAL-FEATURE EDGE TEST  (since {args.since}"
            + (f", band {args.band}c" if band else "") + ")")
    print("  Does cross-exchange gap + order-flow beat the MARKET out of sample?")
    print(f"  {'asset':<6}{'n_test':>7}{'market':>10}{'recal':>10}{'full':>10}"
          f"{'ortho_val':>11}   features / verdict")
    print("  " + "-" * 90)
    results = []
    for asset in CRYPTO_ASSETS:
        res = run_asset(asset, by_asset[asset], args.min_test)
        if res is None:
            continue
        if "skip" in res:
            print(f"  {asset:<6}{'—':>7}{'':>31}            SKIP: {res['skip']}")
            continue
        results.append(res)
        ov = res["ortho_value"]
        if ov > 0.002:
            verdict = "FEATURES ADD SIGNAL *"
        elif ov > 0:
            verdict = "marginal (noise band)"
        else:
            verdict = "no orthogonal signal"
        print(f"  {res['asset']:<6}{res['n_test']:>7}{res['b_market']:>10.4f}"
              f"{res['b_recal']:>10.4f}{res['b_full']:>10.4f}{ov:>+11.4f}"
              f"   {','.join(f.replace('spot_coinbase_kraken_','').replace('kalshi_flow_','kf_') for f in res['feats'])}")
        print(f"  {'':<44}{verdict:>11}")

    print("\n  ortho_val = Brier(recal) - Brier(full), out of sample.")
    print("  '*' (> +0.002) = orthogonal features beat the recalibrated market — a real,")
    print("  market-orthogonal signal worth building on. <=0 = the market already prices it in.")
    print("  (market vs recal ~equal is expected — confirms the market is already calibrated.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
