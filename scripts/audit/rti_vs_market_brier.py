#!/usr/bin/env python3
"""RTI-vs-market edge test harness (read-only).

Decides the ONE question that determines whether the synthetic multi-venue
RTI index gives the bot a real informational edge: does an RTI-fed
probability forecast beat the *market* (not just the old Coinbase-fed model)
on settled 15M outcomes, per asset?

Background — why this is the decisive test
------------------------------------------
A 30-day Brier audit (2026-05-28) showed the market is a better forecaster
than the bot's Coinbase-fed model on ALL 7 assets (market Brier < raw Brier
everywhere). A recalibrator's ceiling is the market, so it cannot create
edge. The only lever that can is a *better price input* — RTI. But the bar
is high: RTI must beat the MARKET'S implied probability, not merely the old
model. The RMSE gate (B2a) proves RTI tracks a reference; THIS proves (or
refutes) that RTI converts into money.

Method
------
Each evaluated_opportunities row stores spot_price (Coinbase, the price the
live model used), threshold, z_score, raw_prob (= 1 - CDF(z) for that asset),
and rti_synthetic (the multi-venue index at decision time). We back out the
move-scale the engine used:

    sigma_move = (threshold - spot_price) / z_score

then re-evaluate the SAME per-asset distribution with RTI as the spot:

    rti_z    = (threshold - rti_synthetic) / sigma_move
    rti_prob = ProbabilityEngine._cdf_complement(rti_z, asset)

This is apples-to-apples with raw_prob (both are uncapped CDF complements),
isolating the single change: Coinbase spot -> RTI spot. We then score
raw_prob (model), rti_prob (RTI), market_price/100 (market), and
shadow_cal_prob (the shadow calibrator) by Brier against the realized YES
outcome, per asset, and report:
  (1) does RTI beat the MARKET (the bar that means money)?
  (2) does RTI beat the old model (necessary but not sufficient)?
  (3) decision flips: where RTI would change the buy/skip vs market price,
      and how those rows actually settled.

Usage
-----
    scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
    python scripts/audit/rti_vs_market_brier.py [--db /tmp/state.db] \
        [--since 2026-05-28] [--min-n 30]

Read-only. Run against a COPY of state.db (never production directly).
"""

import argparse
import math
import os
import sqlite3
import sys
from collections import defaultdict
from typing import Dict, List, Optional

# Self-bootstrap sys.path so `from bot.*` resolves when run from anywhere
# (lesson: scripts that import bot.* must bootstrap at module top).
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from bot.engines.probability import ProbabilityEngine  # noqa: E402

CRYPTO_ASSETS = ("BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB")


# ── Helpers ──────────────────────────────────────────────────────

def connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n  {title}\n{'=' * 78}")


def wilson_ci(wins: int, n: int, z: float = 1.96):
    """Wilson score interval for a binomial proportion (n<200 convention)."""
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def reconstruct_rti_prob(row: sqlite3.Row) -> Optional[float]:
    """Re-evaluate the per-asset CDF with RTI substituted for Coinbase spot.

    Returns None when the stored columns can't support a faithful
    reconstruction (missing/degenerate z_score or spot==threshold).
    """
    spot = row["spot_price"]
    thr = row["threshold"]
    z = row["z_score"]
    rti = row["rti_synthetic"]
    asset = row["asset"]
    if None in (spot, thr, z, rti) or z == 0 or (thr - spot) == 0 or rti <= 0:
        return None
    sigma_move = (thr - spot) / z          # move-scale the engine used
    if sigma_move == 0:
        return None
    rti_z = (thr - rti) / sigma_move
    return ProbabilityEngine._cdf_complement(rti_z, asset)


def brier(pred: float, outcome: float) -> float:
    return (pred - outcome) * (pred - outcome)


# ── Core ─────────────────────────────────────────────────────────

def has_rti_column(conn: sqlite3.Connection) -> bool:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(evaluated_opportunities)")}
    return "rti_synthetic" in cols


def fetch_rows(conn: sqlite3.Connection, since: Optional[str]) -> List[sqlite3.Row]:
    sql = """
        SELECT asset, spot_price, threshold, z_score, raw_prob,
               shadow_cal_prob, market_price, market_result, rti_synthetic,
               rti_confidence, order_id, edge, settled_time
        FROM evaluated_opportunities
        WHERE (product_type = '15m' OR product_type IS NULL)
          AND asset IN ({placeholders})
          AND rti_synthetic IS NOT NULL
    """.format(placeholders=",".join("?" * len(CRYPTO_ASSETS)))
    params: List = list(CRYPTO_ASSETS)
    if since:
        sql += " AND evaluation_time >= ?"
        params.append(since)
    return conn.execute(sql, params).fetchall()


def coverage_report(rows: List[sqlite3.Row]) -> Dict[str, int]:
    settled = [r for r in rows if r["market_result"] in ("yes", "no")]
    section("RTI COVERAGE")
    print(f"  rows with RTI populated:  {len(rows)}")
    print(f"  ... of which SETTLED:     {len(settled)}")
    if rows:
        ts = [r["settled_time"] or "" for r in rows]
        print(f"  settled_time range:       {min(t for t in ts if t) if any(ts) else 'n/a'}"
              f"  ->  {max(ts) if ts else 'n/a'}")
    by_asset: Dict[str, int] = defaultdict(int)
    for r in settled:
        by_asset[r["asset"]] += 1
    if by_asset:
        print("  settled-with-RTI by asset: "
              + ", ".join(f"{a}={by_asset[a]}" for a in CRYPTO_ASSETS if by_asset[a]))
    return by_asset


def brier_table(rows: List[sqlite3.Row], min_n: int) -> None:
    section("PER-ASSET BRIER: model vs RTI vs market  (lower = better)")
    acc: Dict[str, Dict[str, List[float]]] = defaultdict(
        lambda: {"model": [], "rti": [], "market": [], "shadow": [], "y": []})
    for r in rows:
        if r["market_result"] not in ("yes", "no"):
            continue
        if r["raw_prob"] is None or r["market_price"] is None:
            continue
        rti_p = reconstruct_rti_prob(r)
        if rti_p is None:
            continue
        y = 1.0 if r["market_result"] == "yes" else 0.0
        a = acc[r["asset"]]
        a["model"].append(brier(r["raw_prob"], y))
        a["rti"].append(brier(rti_p, y))
        a["market"].append(brier(r["market_price"] / 100.0, y))
        if r["shadow_cal_prob"] is not None:
            a["shadow"].append(brier(r["shadow_cal_prob"], y))
        a["y"].append(y)

    header = f"  {'asset':<6}{'n':>6}{'model':>10}{'RTI':>10}{'market':>10}" \
             f"{'RTI-mkt':>10}{'verdict':>22}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    any_asset = False
    for asset in CRYPTO_ASSETS:
        a = acc.get(asset)
        if not a or len(a["model"]) < min_n:
            continue
        any_asset = True
        n = len(a["model"])
        b_model = sum(a["model"]) / n
        b_rti = sum(a["rti"]) / n
        b_mkt = sum(a["market"]) / n
        rti_minus_mkt = b_rti - b_mkt
        if rti_minus_mkt < -0.002:
            verdict = "RTI BEATS MARKET *"
        elif rti_minus_mkt < 0:
            verdict = "RTI ~ market (noise)"
        else:
            verdict = "market still wins"
        print(f"  {asset:<6}{n:>6}{b_model:>10.4f}{b_rti:>10.4f}{b_mkt:>10.4f}"
              f"{rti_minus_mkt:>+10.4f}{verdict:>22}")
    if not any_asset:
        print(f"  PARKED: no asset has >= {min_n} settled RTI rows yet.")
        print("  Re-run once RTI data accumulates (B2a clock ~2026-06-11), or")
        print("  after reconstructing RTI for past windows from bronze venue-L2.")
    else:
        print("\n  '*' = RTI beats the MARKET by > 0.002 Brier — the bar that means")
        print("  money. 'RTI ~ market' is within noise (necessary work, not edge).")


def decision_flip_report(rows: List[sqlite3.Row], min_n: int) -> None:
    """Where would RTI flip the buy/skip vs market price, and how did it settle?

    A 'buy' signal = forecast prob > market implied price. We compare the
    RTI-fed signal to the Coinbase-fed (model) signal: rows where they
    disagree are where RTI would actually change behavior. We then report
    the realized YES-rate of each disagreement bucket — i.e. was RTI right
    to flip?
    """
    section("DECISION FLIPS: where RTI disagrees with the model vs the price")
    flips_rti_buy: List[float] = []   # RTI says buy, model says skip -> did YES happen?
    flips_rti_skip: List[float] = []  # RTI says skip, model says buy
    for r in rows:
        if r["market_result"] not in ("yes", "no") or r["raw_prob"] is None \
                or r["market_price"] is None:
            continue
        rti_p = reconstruct_rti_prob(r)
        if rti_p is None:
            continue
        price = r["market_price"] / 100.0
        model_buy = r["raw_prob"] > price
        rti_buy = rti_p > price
        if model_buy == rti_buy:
            continue
        y = 1.0 if r["market_result"] == "yes" else 0.0
        (flips_rti_buy if rti_buy else flips_rti_skip).append(y)

    for label, bucket in (("RTI buys where model skips", flips_rti_buy),
                          ("RTI skips where model buys", flips_rti_skip)):
        n = len(bucket)
        if n == 0:
            print(f"  {label}: 0 rows")
            continue
        yes_rate = sum(bucket) / n
        lo, hi = wilson_ci(int(sum(bucket)), n)
        note = ""
        if "buys" in label:
            note = "(RTI right to add IF yes_rate clears the price+fee)"
        else:
            note = "(RTI right to veto IF yes_rate is LOW)"
        print(f"  {label}: n={n}  realized YES-rate={yes_rate:.3f} "
              f"[Wilson {lo:.3f}-{hi:.3f}]  {note}")
    if len(flips_rti_buy) + len(flips_rti_skip) < min_n:
        print(f"\n  (Total flips < {min_n}; treat as directional only until more data.)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="/tmp/state.db", help="path to a COPY of state.db")
    ap.add_argument("--since", default=None, help="ISO date floor on evaluation_time")
    ap.add_argument("--min-n", type=int, default=30,
                    help="min settled RTI rows per asset to score (default 30)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: db not found at {args.db}\n"
              f"  scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db {args.db}",
              file=sys.stderr)
        return 64

    conn = connect_db(args.db)
    if not has_rti_column(conn):
        print(f"PARKED: {args.db} predates RTI instrumentation "
              "(column 'rti_synthetic' is absent on evaluated_opportunities).\n"
              "  Pull a fresh copy from the VPS, which started logging RTI 2026-05-28:\n"
              f"  scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db {args.db}")
        return 0
    rows = fetch_rows(conn, args.since)
    coverage_report(rows)
    brier_table(rows, args.min_n)
    decision_flip_report(rows, args.min_n)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
