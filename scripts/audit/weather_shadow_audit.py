#!/usr/bin/env python3
"""Weather shadow mode audit script — comprehensive analysis of weather observation data.

Reads from state.db evaluated_opportunities (product_type='weather').
Follows SPX audit pattern: --since, --json, --db flags, PRAGMA busy_timeout.

Usage:
    scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
    python3 scripts/audit/weather_shadow_audit.py [--db /tmp/state.db] [--since 2026-03-02T16:54:00] [--json weather_audit.json]
"""

import argparse
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple


# ─── Helpers ──────────────────────────────────────────────────────────────────

def connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def pct(num, denom) -> str:
    if denom == 0:
        return "n/a"
    return f"{num / denom * 100:.1f}%"


def safe_div(a, b, default=0.0):
    return a / b if b else default


def header(title: str):
    print(f"\n{'=' * 72}")
    print(f"  {title}")
    print(f"{'=' * 72}")


def subheader(title: str):
    print(f"\n  ── {title} ──")


def where_clause(since: Optional[str], time_col: str = "evaluation_time") -> str:
    if since:
        return f"AND {time_col} >= '{since}'"
    return ""


def maker_fee(price_cents: int) -> int:
    """Kalshi charges $0 on maker fills."""
    return 0


def taker_fee(price_cents: int) -> int:
    p = price_cents / 100.0
    return math.ceil(0.07 * 100 * p * (1 - p))


def breakeven_wr(price_cents: int) -> float:
    """Breakeven win rate at a given entry price (taker fees)."""
    fee = taker_fee(price_cents)
    return (price_cents + fee) / 100.0


def cf_pnl(price_cents: int, result: str, is_maker: bool = False) -> int:
    """Counterfactual PnL per contract in cents. YES-side only."""
    fee = maker_fee(price_cents) if is_maker else taker_fee(price_cents)
    if result in ("yes", "all_yes"):
        return 100 - price_cents - fee
    else:
        return -(price_cents + fee)


def sized_pnl(price_cents: int, result: str, position_size: int, is_maker: bool = False) -> float:
    """Sized PnL in dollars."""
    pnl_per = cf_pnl(price_cents, result, is_maker)
    return pnl_per * position_size / 100.0


def wilson_ci(wins: int, total: int, z: float = 1.96) -> Tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p_hat = wins / total
    denom = 1 + z * z / total
    center = (p_hat + z * z / (2 * total)) / denom
    spread = z * math.sqrt((p_hat * (1 - p_hat) + z * z / (4 * total)) / total) / denom
    return (max(0, center - spread), min(1, center + spread))


def significance_tag(n: int) -> str:
    if n < 5:
        return "*** VERY SMALL SAMPLE"
    if n < 15:
        return "** NOT SIGNIFICANT"
    if n < 30:
        return "* SMALL SAMPLE"
    return ""


def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(c["name"] == column for c in cols)


# ─── Section 1: Overview ──────────────────────────────────────────────────────

def section_overview(conn, since):
    header("1. SHADOW PERFORMANCE SUMMARY")
    W = where_clause(since)

    row = conn.execute(f"""
        SELECT COUNT(*) AS total,
          SUM(CASE WHEN filter_stage='price_out_of_range' THEN 1 ELSE 0 END) AS por,
          SUM(CASE WHEN filter_stage='strategy_wait' THEN 1 ELSE 0 END) AS sw,
          SUM(CASE WHEN filter_stage='insufficient_edge' THEN 1 ELSE 0 END) AS ie,
          SUM(CASE WHEN filter_stage='weather_observation' THEN 1 ELSE 0 END) AS wo,
          SUM(CASE WHEN filter_stage='low_probability' THEN 1 ELSE 0 END) AS lp,
          SUM(CASE WHEN filter_stage='data_unavailable' THEN 1 ELSE 0 END) AS du,
          SUM(CASE WHEN filter_stage='zero_sizing' THEN 1 ELSE 0 END) AS zs,
          MIN(evaluation_time) AS first_t, MAX(evaluation_time) AS last_t
        FROM evaluated_opportunities WHERE product_type='weather' {W}
    """).fetchone()

    total = row["total"] or 0
    signals = row["wo"] or 0
    por = row["por"] or 0
    sw = row["sw"] or 0
    ie = row["ie"] or 0

    span_hrs = 0
    if row["first_t"] and row["last_t"]:
        t1 = datetime.fromisoformat(row["first_t"].replace("Z", ""))
        t2 = datetime.fromisoformat(row["last_t"].replace("Z", ""))
        span_hrs = (t2 - t1).total_seconds() / 3600

    print(f"Total evaluations:     {total}")
    print(f"Signals fired:         {signals}")
    print(f"Signal rate:           {pct(signals, total)}")
    print(f"Time span:             {span_hrs:.1f} hours ({span_hrs/24:.1f} days)")
    if span_hrs > 0:
        print(f"Eval rate:             {total/span_hrs:.1f}/hr")
    print(f"First: {row['first_t'] or '—'}")
    print(f"Last:  {row['last_t'] or '—'}")
    print()

    print("Filter stage breakdown:")
    stages = [
        ("weather_observation", signals), ("insufficient_edge", ie),
        ("price_out_of_range", por), ("strategy_wait", sw),
    ]
    for name, count in stages:
        if count > 0:
            print(f"  {name:<25} {count:>4} ({pct(count, total)})")

    return {
        "total": total, "signals": signals, "span_hrs": span_hrs,
        "por": por, "sw": sw, "ie": ie,
    }


# ─── Section 1a: Per-City Breakdown ──────────────────────────────────────────

def section_per_city(conn, since):
    header("1a. PER-CITY PERFORMANCE")
    W = where_clause(since)

    rows = conn.execute(f"""
        SELECT asset,
          COUNT(*) AS n,
          SUM(CASE WHEN filter_stage='weather_observation' THEN 1 ELSE 0 END) AS sigs,
          SUM(CASE WHEN filter_stage='weather_observation' AND market_result IN ('yes','all_yes') THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN filter_stage='weather_observation' AND market_result IN ('no','all_no') THEN 1 ELSE 0 END) AS losses,
          SUM(CASE WHEN filter_stage='weather_observation' AND market_result IS NULL THEN 1 ELSE 0 END) AS pending,
          AVG(CASE WHEN wx_n_members IS NOT NULL THEN wx_n_members END) AS avg_mem
        FROM evaluated_opportunities WHERE product_type='weather' {W}
        GROUP BY asset ORDER BY sigs DESC
    """).fetchall()

    # Compute PnL per city
    pnl_rows = conn.execute(f"""
        SELECT asset, market_price, market_result, position_size
        FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND filter_stage='weather_observation' AND market_result IS NOT NULL
    """).fetchall()

    city_pnl = defaultdict(lambda: {"pnl_1c": 0, "pnl_sized": 0.0})
    for r in pnl_rows:
        price = int(r["market_price"])
        pnl_val = cf_pnl(price, r["market_result"])
        city_pnl[r["asset"]]["pnl_1c"] += pnl_val
        if r["position_size"] and r["position_size"] > 0:
            city_pnl[r["asset"]]["pnl_sized"] += sized_pnl(price, r["market_result"], r["position_size"])

    print(f"  {'City':<12} {'Evals':>5} {'Sigs':>5} {'W':>3} {'L':>3} {'WR':>6} {'1c PnL':>8} {'Sized$':>9} {'Mem':>4}")
    print(f"  {'-'*60}")
    for r in rows:
        w = r["wins"] or 0
        l = r["losses"] or 0
        settled = w + l
        wr = pct(w, settled) if settled > 0 else "—"
        p = city_pnl.get(r["asset"], {"pnl_1c": 0, "pnl_sized": 0})
        mem = f"{r['avg_mem']:.0f}" if r["avg_mem"] else "—"
        pnl_str = f"{p['pnl_1c']:+d}c" if settled > 0 else "—"
        sized_str = f"${p['pnl_sized']:+.2f}" if settled > 0 else "—"
        print(f"  {r['asset']:<12} {r['n']:>5} {r['sigs']:>5} {w:>3} {l:>3} {wr:>6} {pnl_str:>8} {sized_str:>9} {mem:>4}")


# ─── Section 1b: Daily PnL Timeline ──────────────────────────────────────────

def section_daily_pnl(conn, since):
    header("1b. DAILY P&L TIMELINE")
    W = where_clause(since)

    rows = conn.execute(f"""
        SELECT market_price, market_result, position_size, DATE(evaluation_time) AS day
        FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND filter_stage='weather_observation' AND market_result IS NOT NULL
        ORDER BY evaluation_time
    """).fetchall()

    if not rows:
        print("  No settled data yet")
        return

    daily = defaultdict(lambda: {"w": 0, "l": 0, "pnl_1c": 0, "pnl_sized": 0.0})
    for r in rows:
        price = int(r["market_price"])
        is_win = r["market_result"] in ("yes", "all_yes")
        pnl_val = cf_pnl(price, r["market_result"])
        day = r["day"]
        daily[day]["w" if is_win else "l"] += 1
        daily[day]["pnl_1c"] += pnl_val
        if r["position_size"] and r["position_size"] > 0:
            daily[day]["pnl_sized"] += sized_pnl(price, r["market_result"], r["position_size"])

    cum_1c = 0
    cum_sized = 0.0
    print(f"  {'Date':12s} {'N':>3} {'W':>3} {'L':>3} {'WR':>6} {'1c PnL':>8} {'Sized$':>9} {'Cum 1c':>8} {'Cum Sized':>10}")
    print(f"  {'-'*75}")
    for day in sorted(daily.keys()):
        s = daily[day]
        n = s["w"] + s["l"]
        wr = s["w"] / n * 100 if n > 0 else 0
        cum_1c += s["pnl_1c"]
        cum_sized += s["pnl_sized"]
        print(f"  {day:12s} {n:>3} {s['w']:>3} {s['l']:>3} {wr:>5.0f}% {s['pnl_1c']:>+7d}c ${s['pnl_sized']:>+8.2f} {cum_1c:>+7d}c ${cum_sized:>+9.2f}")


# ─── Section 2: Settlement + PnL by Market Type ──────────────────────────────

def section_market_type_pnl(conn, since):
    header("2. PERFORMANCE BY MARKET TYPE")
    W = where_clause(since)

    rows = conn.execute(f"""
        SELECT COALESCE(wx_market_type, 'unknown') AS mtype,
          market_price, market_result, position_size, calibrated_prob
        FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND filter_stage='weather_observation' AND market_result IS NOT NULL
    """).fetchall()

    if not rows:
        print("  No settled signal data")
        return

    stats = defaultdict(lambda: {"w": 0, "l": 0, "pnl_1c": 0, "pnl_sized": 0.0, "prices": [], "probs": []})
    for r in rows:
        mt = r["mtype"]
        price = int(r["market_price"])
        is_win = r["market_result"] in ("yes", "all_yes")
        stats[mt]["w" if is_win else "l"] += 1
        stats[mt]["pnl_1c"] += cf_pnl(price, r["market_result"])
        if r["position_size"] and r["position_size"] > 0:
            stats[mt]["pnl_sized"] += sized_pnl(price, r["market_result"], r["position_size"])
        stats[mt]["prices"].append(price)
        if r["calibrated_prob"]:
            stats[mt]["probs"].append(r["calibrated_prob"])

    print(f"  {'Type':<15} {'W':>3} {'L':>3} {'WR':>6} {'1c PnL':>8} {'Sized$':>9} {'AvgPrice':>9} {'AvgProb':>8} {'Sig':>20}")
    print(f"  {'-'*85}")
    for mt in sorted(stats.keys()):
        s = stats[mt]
        n = s["w"] + s["l"]
        wr = s["w"] / n * 100 if n > 0 else 0
        avg_p = sum(s["prices"]) / len(s["prices"]) if s["prices"] else 0
        avg_prob = sum(s["probs"]) / len(s["probs"]) if s["probs"] else 0
        tag = significance_tag(n)
        print(f"  {mt:<15} {s['w']:>3} {s['l']:>3} {wr:>5.1f}% {s['pnl_1c']:>+7d}c ${s['pnl_sized']:>+8.2f} {avg_p:>8.1f}c {avg_prob:>7.1%} {tag}")


# ─── Section 2a: Price Bucket Performance ─────────────────────────────────────

def section_price_buckets(conn, since):
    header("2a. PERFORMANCE BY PRICE BUCKET")
    W = where_clause(since)

    rows = conn.execute(f"""
        SELECT market_price, market_result, position_size
        FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND filter_stage='weather_observation' AND market_result IS NOT NULL
    """).fetchall()

    if not rows:
        print("  No settled signal data")
        return

    buckets = defaultdict(lambda: {"w": 0, "l": 0, "pnl_1c": 0, "pnl_sized": 0.0})
    bucket_order = ["1-10c", "11-20c", "21-30c", "31-50c", "51-70c", "71c+"]

    for r in rows:
        price = int(r["market_price"])
        if price <= 10: b = "1-10c"
        elif price <= 20: b = "11-20c"
        elif price <= 30: b = "21-30c"
        elif price <= 50: b = "31-50c"
        elif price <= 70: b = "51-70c"
        else: b = "71c+"

        is_win = r["market_result"] in ("yes", "all_yes")
        buckets[b]["w" if is_win else "l"] += 1
        buckets[b]["pnl_1c"] += cf_pnl(price, r["market_result"])
        if r["position_size"] and r["position_size"] > 0:
            buckets[b]["pnl_sized"] += sized_pnl(price, r["market_result"], r["position_size"])

    print(f"  {'Bucket':10s} {'W':>3} {'L':>3} {'WR':>6} {'BE WR':>6} {'Gap':>7} {'1c PnL':>8} {'Sized$':>9} {'Sig':>20}")
    print(f"  {'-'*80}")
    for b in bucket_order:
        if b not in buckets:
            continue
        s = buckets[b]
        n = s["w"] + s["l"]
        wr = s["w"] / n if n > 0 else 0
        # Approximate BE WR for bucket midpoint
        mid = {"1-10c": 6, "11-20c": 15, "21-30c": 25, "31-50c": 40, "51-70c": 60, "71c+": 85}[b]
        be = breakeven_wr(mid)
        gap = wr - be
        tag = significance_tag(n)
        verdict = "OK" if gap >= 0 else "LOSING"
        print(f"  {b:10s} {s['w']:>3} {s['l']:>3} {wr:>5.1%} {be:>5.1%} {gap:>+6.1%} {s['pnl_1c']:>+7d}c ${s['pnl_sized']:>+8.2f} {tag}")

    # Flag the losing buckets
    losing = [(b, buckets[b]) for b in bucket_order if b in buckets and buckets[b]["w"] / max(1, buckets[b]["w"] + buckets[b]["l"]) < breakeven_wr({"1-10c": 6, "11-20c": 15, "21-30c": 25, "31-50c": 40, "51-70c": 60, "71c+": 85}[b])]
    if losing:
        print(f"\n  *** BELOW-BREAKEVEN BUCKETS:")
        for b, s in losing:
            n = s["w"] + s["l"]
            wr = s["w"] / n if n > 0 else 0
            print(f"      {b}: {s['w']}W/{s['l']}L ({wr:.1%}), 1c PnL {s['pnl_1c']:+d}c")


# ─── Section 3: Calibration Analysis ─────────────────────────────────────────

def section_calibration(conn, since):
    header("3. CALIBRATION ANALYSIS")
    W = where_clause(since)

    rows = conn.execute(f"""
        SELECT calibrated_prob, raw_prob, market_price, market_result, wx_market_type
        FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND filter_stage='weather_observation' AND market_result IS NOT NULL
          AND calibrated_prob IS NOT NULL
    """).fetchall()

    if not rows:
        print("  No settled signal data with calibrated_prob")
        return

    # Brier score
    brier_sum = sum((r["calibrated_prob"] - (1 if r["market_result"] in ("yes", "all_yes") else 0)) ** 2 for r in rows)
    brier = brier_sum / len(rows)
    print(f"  Brier Score: {brier:.4f} (n={len(rows)})")

    # By predicted probability bucket
    subheader("By Predicted Probability")
    buckets = defaultdict(lambda: {"n": 0, "wins": 0, "sum_prob": 0.0})
    for r in rows:
        prob = r["calibrated_prob"]
        if prob < 0.15: b = "<15%"
        elif prob < 0.25: b = "15-25%"
        elif prob < 0.40: b = "25-40%"
        elif prob < 0.60: b = "40-60%"
        else: b = "60%+"
        buckets[b]["n"] += 1
        buckets[b]["sum_prob"] += prob
        if r["market_result"] in ("yes", "all_yes"):
            buckets[b]["wins"] += 1

    print(f"  {'Bucket':10s} {'N':>4} {'Predicted':>10} {'Actual':>8} {'Gap':>8} {'Verdict':>15} {'Sig':>20}")
    print(f"  {'-'*80}")
    for b in ["<15%", "15-25%", "25-40%", "40-60%", "60%+"]:
        if b not in buckets:
            continue
        s = buckets[b]
        avg_pred = s["sum_prob"] / s["n"]
        act = s["wins"] / s["n"]
        gap = avg_pred - act
        verdict = "OVERCONFIDENT" if gap > 0.05 else "UNDERCONFIDENT" if gap < -0.05 else "OK"
        tag = significance_tag(s["n"])
        print(f"  {b:10s} {s['n']:>4} {avg_pred:>9.1%} {act:>7.1%} {gap:>+7.1%} {verdict:>15} {tag}")

    # Per market type Brier
    subheader("Brier by Market Type")
    mt_brier = defaultdict(lambda: {"sum": 0.0, "n": 0})
    for r in rows:
        mt = r["wx_market_type"] or "unknown"
        outcome = 1 if r["market_result"] in ("yes", "all_yes") else 0
        mt_brier[mt]["sum"] += (r["calibrated_prob"] - outcome) ** 2
        mt_brier[mt]["n"] += 1

    for mt in sorted(mt_brier.keys()):
        s = mt_brier[mt]
        b = s["sum"] / s["n"]
        tag = significance_tag(s["n"])
        print(f"  {mt:<15} Brier={b:.4f}  n={s['n']} {tag}")


# ─── Section 4: Forecast Accuracy ────────────────────────────────────────────

def section_forecast_accuracy(conn, since):
    header("4. FORECAST vs OBSERVED ACCURACY")
    W = where_clause(since)

    if not has_column(conn, "evaluated_opportunities", "wx_actual_high_temp"):
        print("  DATA GAP: wx_actual_high_temp column not present")
        return

    rows = conn.execute(f"""
        SELECT asset, wx_ensemble_mean, wx_actual_high_temp, wx_bias_correction,
               DATE(evaluation_time) AS eval_date
        FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND wx_actual_high_temp IS NOT NULL AND wx_ensemble_mean IS NOT NULL
        GROUP BY asset, DATE(evaluation_time)
        ORDER BY asset, eval_date
    """).fetchall()

    if not rows:
        print("  No observed high temp data yet (archive API populates after settlement)")
        return

    city_errors = defaultdict(list)
    for r in rows:
        error = r["wx_actual_high_temp"] - r["wx_ensemble_mean"]
        city_errors[r["asset"]].append({"date": r["eval_date"], "error": error,
                                         "forecast": r["wx_ensemble_mean"], "actual": r["wx_actual_high_temp"]})

    print(f"  {'City':<12} {'N':>4} {'MAE':>7} {'RMSE':>7} {'Bias':>7} {'Direction':>10} {'Sig':>20}")
    print(f"  {'-'*75}")
    all_errors = []
    for city in sorted(city_errors.keys()):
        errors = [e["error"] for e in city_errors[city]]
        n = len(errors)
        mae = sum(abs(e) for e in errors) / n
        rmse = math.sqrt(sum(e ** 2 for e in errors) / n)
        bias = sum(errors) / n
        direction = "low" if bias > 0.5 else "high" if bias < -0.5 else "neutral"
        tag = significance_tag(n)
        print(f"  {city:<12} {n:>4} {mae:>6.1f}F {rmse:>6.1f}F {bias:>+6.1f}F {direction:>10} {tag}")
        all_errors.extend(errors)

    if all_errors:
        mae = sum(abs(e) for e in all_errors) / len(all_errors)
        rmse = math.sqrt(sum(e ** 2 for e in all_errors) / len(all_errors))
        bias = sum(all_errors) / len(all_errors)
        print(f"\n  Overall:     {len(all_errors):>4} {mae:>6.1f}F {rmse:>6.1f}F {bias:>+6.1f}F")

    # Flag cities with consistent bias (>2F)
    biased = [(c, sum(e["error"] for e in errs) / len(errs))
              for c, errs in city_errors.items()
              if len(errs) >= 3 and abs(sum(e["error"] for e in errs) / len(errs)) > 2.0]
    if biased:
        print(f"\n  *** HIGH-BIAS CITIES (|bias| > 2F, n>=3):")
        for city, bias in biased:
            direction = "forecasts too HIGH" if bias < 0 else "forecasts too LOW"
            print(f"      {city}: {bias:+.1f}F ({direction})")

    # HRRR comparison if available
    if has_column(conn, "evaluated_opportunities", "wx_hrrr_temp"):
        hrrr_rows = conn.execute(f"""
            SELECT asset, wx_hrrr_temp, wx_actual_high_temp, wx_ensemble_mean,
                   DATE(evaluation_time) AS eval_date
            FROM evaluated_opportunities
            WHERE product_type='weather' {W}
              AND wx_hrrr_temp IS NOT NULL AND wx_actual_high_temp IS NOT NULL
            GROUP BY asset, DATE(evaluation_time)
        """).fetchall()
        if hrrr_rows:
            subheader("HRRR vs Ensemble Accuracy")
            hrrr_errors = [r["wx_actual_high_temp"] - r["wx_hrrr_temp"] for r in hrrr_rows]
            ens_errors = [r["wx_actual_high_temp"] - r["wx_ensemble_mean"] for r in hrrr_rows]
            hrrr_mae = sum(abs(e) for e in hrrr_errors) / len(hrrr_errors)
            ens_mae = sum(abs(e) for e in ens_errors) / len(ens_errors)
            print(f"  HRRR MAE:     {hrrr_mae:.1f}F (n={len(hrrr_rows)})")
            print(f"  Ensemble MAE: {ens_mae:.1f}F (n={len(hrrr_rows)})")
            if hrrr_mae < ens_mae:
                print(f"  >>> HRRR is {ens_mae - hrrr_mae:.1f}F more accurate than ensemble")
            else:
                print(f"  >>> Ensemble is {hrrr_mae - ens_mae:.1f}F more accurate than HRRR")
        else:
            print(f"\n  HRRR data: not yet in DB (deploy pending)")


# ─── Section 5: Ensemble Health ───────────────────────────────────────────────

def section_ensemble_health(conn, since):
    header("5. ENSEMBLE DATA QUALITY")
    W = where_clause(since)

    row = conn.execute(f"""
        SELECT
          SUM(CASE WHEN wx_ensemble_mean IS NOT NULL THEN 1 ELSE 0 END) AS has_ens,
          COUNT(*) AS total,
          AVG(CASE WHEN wx_n_members IS NOT NULL THEN wx_n_members END) AS avg_mem,
          MIN(CASE WHEN wx_n_members IS NOT NULL THEN wx_n_members END) AS min_mem,
          MAX(CASE WHEN wx_n_members IS NOT NULL THEN wx_n_members END) AS max_mem,
          AVG(CASE WHEN wx_ensemble_std IS NOT NULL THEN wx_ensemble_std END) AS avg_std,
          MIN(CASE WHEN wx_ensemble_std IS NOT NULL THEN wx_ensemble_std END) AS min_std,
          MAX(CASE WHEN wx_ensemble_std IS NOT NULL THEN wx_ensemble_std END) AS max_std
        FROM evaluated_opportunities WHERE product_type='weather' {W}
    """).fetchone()

    total = row["total"] or 0
    has_ens = row["has_ens"] or 0
    coverage = safe_div(has_ens, total) * 100

    print(f"  Ensemble coverage: {has_ens}/{total} ({coverage:.1f}%)")
    if row["avg_mem"]:
        print(f"  Members:           avg={row['avg_mem']:.0f} min={row['min_mem']} max={row['max_mem']} (expected 82: 31 GFS + 51 ECMWF)")
        ecmwf = (row["max_mem"] or 0) > 31
        print(f"  ECMWF status:      {'PRESENT' if ecmwf else 'ABSENT (only GFS)'}")
    if row["avg_std"]:
        print(f"  Ensemble std:      avg={row['avg_std']:.2f}F min={row['min_std']:.2f}F max={row['max_std']:.2f}F")

    # Ensemble std vs outcome (settled observations only)
    subheader("Ensemble Std vs Win Rate (settled obs)")
    std_rows = conn.execute(f"""
        SELECT wx_ensemble_std, market_result, market_price
        FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND filter_stage='weather_observation' AND market_result IS NOT NULL
          AND wx_ensemble_std IS NOT NULL
    """).fetchall()

    if std_rows:
        buckets = defaultdict(lambda: {"w": 0, "l": 0, "prices": []})
        for r in std_rows:
            std = r["wx_ensemble_std"]
            if std < 1.0: b = "<1.0F"
            elif std < 1.5: b = "1.0-1.5F"
            elif std < 2.0: b = "1.5-2.0F"
            elif std < 2.5: b = "2.0-2.5F"
            else: b = "2.5F+"
            is_win = r["market_result"] in ("yes", "all_yes")
            buckets[b]["w" if is_win else "l"] += 1
            buckets[b]["prices"].append(int(r["market_price"]))

        print(f"  {'Std Bucket':10s} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} {'AvgPrice':>9} {'Sig':>20}")
        print(f"  {'-'*60}")
        for b in ["<1.0F", "1.0-1.5F", "1.5-2.0F", "2.0-2.5F", "2.5F+"]:
            if b not in buckets:
                continue
            s = buckets[b]
            n = s["w"] + s["l"]
            wr = s["w"] / n if n > 0 else 0
            avg_p = sum(s["prices"]) / len(s["prices"]) if s["prices"] else 0
            tag = significance_tag(n)
            print(f"  {b:10s} {n:>4} {s['w']:>3} {s['l']:>3} {wr:>5.1%} {avg_p:>8.1f}c {tag}")

    # raw_prob availability
    subheader("raw_prob Pipeline")
    rp = conn.execute(f"""
        SELECT SUM(CASE WHEN raw_prob IS NOT NULL THEN 1 ELSE 0 END) AS has,
               COUNT(*) AS total
        FROM evaluated_opportunities WHERE product_type='weather' {W}
    """).fetchone()
    rp_pct = safe_div(rp["has"] or 0, rp["total"] or 1) * 100
    print(f"  raw_prob populated: {rp['has']}/{rp['total']} ({rp_pct:.1f}%)")

    # Column fill rates (observations only)
    subheader("Column Fill Rates (weather_observation entries)")
    obs_total = conn.execute(f"""
        SELECT COUNT(*) FROM evaluated_opportunities
        WHERE product_type='weather' {W} AND filter_stage='weather_observation'
    """).fetchone()[0]

    wx_cols = ["wx_ensemble_mean", "wx_ensemble_std", "wx_bias_correction", "wx_n_members",
               "wx_market_type", "wx_actual_high_temp", "wx_no_side_edge"]
    # Check for new columns
    for extra in ["wx_hrrr_temp", "wx_corrected_mean"]:
        if has_column(conn, "evaluated_opportunities", extra):
            wx_cols.append(extra)

    for col in wx_cols:
        filled = conn.execute(f"""
            SELECT COUNT(*) FROM evaluated_opportunities
            WHERE product_type='weather' {W} AND filter_stage='weather_observation'
              AND {col} IS NOT NULL
        """).fetchone()[0]
        status = "OK" if filled == obs_total else ("PARTIAL" if filled > 0 else "EMPTY")
        print(f"  {col:30s} {filled}/{obs_total} ({pct(filled, obs_total):>6s}) {status}")

    return {"coverage": coverage, "raw_prob_pct": rp_pct}


# ─── Section 6: Blend Weight Simulation ──────────────────────────────────────

def section_blend_sim(conn, since):
    header("6. BLEND WEIGHT SIMULATION")
    W = where_clause(since)

    rows = conn.execute(f"""
        SELECT raw_prob, market_price, market_result, position_size
        FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND filter_stage IN ('weather_observation', 'insufficient_edge')
          AND raw_prob IS NOT NULL AND market_price IS NOT NULL AND market_result IS NOT NULL
    """).fetchall()

    if len(rows) < 5:
        print(f"  INSUFFICIENT DATA: {len(rows)} settled rows (need >= 5)")
        return

    blend_weights = [0.0, 0.10, 0.20, 0.30, 0.40, 0.50]

    print(f"  {'BLEND_W':>8} {'Brier':>8} {'PnL¢':>8} {'Sized$':>9} {'Sigs':>6} {'WR':>6} {'W':>4} {'L':>4}")
    print(f"  {'-'*60}")

    for bw in blend_weights:
        total_brier = 0
        total_pnl = 0
        total_sized = 0.0
        sigs = 0
        wins = 0

        for r in rows:
            rp = r["raw_prob"]
            mp = int(r["market_price"])
            market_p = mp / 100.0
            blended = (1.0 - bw) * rp + bw * market_p
            outcome = 1 if r["market_result"] in ("yes", "all_yes") else 0
            total_brier += (blended - outcome) ** 2

            edge = blended - market_p
            fee = taker_fee(mp)
            fee_edge = edge - fee / 100.0
            if fee_edge >= 0.001:
                sigs += 1
                total_pnl += cf_pnl(mp, r["market_result"])
                ps = r["position_size"]
                if ps and ps > 0:
                    total_sized += sized_pnl(mp, r["market_result"], ps)
                if outcome:
                    wins += 1

        brier = total_brier / len(rows)
        losses = sigs - wins
        wr = pct(wins, sigs) if sigs > 0 else "—"
        print(f"  {bw:>8.2f} {brier:>8.4f} {total_pnl:>+8d} ${total_sized:>+8.2f} {sigs:>6} {wr:>6} {wins:>4} {losses:>4}")

    print(f"\n  Note: Brier computed on ALL settled data. PnL on simulated signals only.")


# ─── Section 7: STC Analysis ─────────────────────────────────────────────────

def section_stc(conn, since):
    header("7. TIMING ANALYSIS (STC)")
    W = where_clause(since)

    rows = conn.execute(f"""
        SELECT seconds_to_close, market_price, market_result, position_size
        FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND filter_stage='weather_observation' AND market_result IS NOT NULL
          AND seconds_to_close IS NOT NULL
    """).fetchall()

    if not rows:
        print("  No settled observation data")
        return

    buckets = defaultdict(lambda: {"w": 0, "l": 0, "pnl_1c": 0, "pnl_sized": 0.0, "prices": []})
    for r in rows:
        stc = r["seconds_to_close"]
        if stc < 3600: b = "<1h"
        elif stc < 7200: b = "1-2h"
        elif stc < 14400: b = "2-4h"
        elif stc < 28800: b = "4-8h"
        elif stc < 43200: b = "8-12h"
        elif stc < 57600: b = "12-16h"
        else: b = "16h+"

        price = int(r["market_price"])
        is_win = r["market_result"] in ("yes", "all_yes")
        buckets[b]["w" if is_win else "l"] += 1
        buckets[b]["pnl_1c"] += cf_pnl(price, r["market_result"])
        ps = r["position_size"]
        if ps and ps > 0:
            buckets[b]["pnl_sized"] += sized_pnl(price, r["market_result"], ps)
        buckets[b]["prices"].append(price)

    print(f"  {'STC Bucket':10s} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} {'1c PnL':>8} {'Sized$':>9} {'AvgPrice':>9} {'Sig':>20}")
    print(f"  {'-'*80}")
    for b in ["<1h", "1-2h", "2-4h", "4-8h", "8-12h", "12-16h", "16h+"]:
        if b not in buckets:
            continue
        s = buckets[b]
        n = s["w"] + s["l"]
        wr = s["w"] / n if n > 0 else 0
        avg_p = sum(s["prices"]) / len(s["prices"]) if s["prices"] else 0
        tag = significance_tag(n)
        print(f"  {b:10s} {n:>4} {s['w']:>3} {s['l']:>3} {wr:>5.1%} {s['pnl_1c']:>+7d}c ${s['pnl_sized']:>+8.2f} {avg_p:>8.1f}c {tag}")


# ─── Section 8: Leak / Counterfactual Analysis ───────────────────────────────

def section_leaks(conn, since):
    header("8. LEAK / COUNTERFACTUAL ANALYSIS")
    W = where_clause(since)

    # IE near-misses
    subheader("insufficient_edge Counterfactual")
    ie_rows = conn.execute(f"""
        SELECT market_price, market_result, fee_adjusted_edge, position_size
        FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND filter_stage='insufficient_edge' AND market_result IS NOT NULL
    """).fetchall()

    if ie_rows:
        ie_wins = sum(1 for r in ie_rows if r["market_result"] in ("yes", "all_yes"))
        ie_total = len(ie_rows)
        ie_pnl = sum(cf_pnl(int(r["market_price"]), r["market_result"]) for r in ie_rows)
        ie_sized = sum(sized_pnl(int(r["market_price"]), r["market_result"], r["position_size"])
                       for r in ie_rows if r["position_size"] and r["position_size"] > 0)
        wr = ie_wins / ie_total * 100
        print(f"  N={ie_total}, {ie_wins}W/{ie_total - ie_wins}L ({wr:.1f}%)")
        print(f"  Counterfactual PnL: {ie_pnl:+d}c (1-contract), ${ie_sized:+.2f} (sized)")
        print(f"  >>> {'FILTER CORRECT' if ie_pnl <= 0 else 'FILTER TOO STRICT'} (counterfactual {'negative' if ie_pnl <= 0 else 'positive'})")

        # Near-misses (edge > -0.02)
        near = [r for r in ie_rows if r["fee_adjusted_edge"] and r["fee_adjusted_edge"] > -0.02]
        if near:
            near_wins = sum(1 for r in near if r["market_result"] in ("yes", "all_yes"))
            near_pnl = sum(cf_pnl(int(r["market_price"]), r["market_result"]) for r in near)
            near_sized = sum(sized_pnl(int(r["market_price"]), r["market_result"], r["position_size"])
                             for r in near if r["position_size"] and r["position_size"] > 0)
            print(f"  Near-miss (edge > -2%): {len(near)} entries, {near_wins}W, PnL={near_pnl:+d}c, Sized=${near_sized:+.2f}")

    # strategy_wait counterfactual
    subheader("strategy_wait Counterfactual")
    sw_rows = conn.execute(f"""
        SELECT market_price, market_result, position_size
        FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND filter_stage='strategy_wait' AND market_result IS NOT NULL
    """).fetchall()

    if sw_rows:
        sw_wins = sum(1 for r in sw_rows if r["market_result"] in ("yes", "all_yes"))
        sw_total = len(sw_rows)
        sw_pnl = sum(cf_pnl(int(r["market_price"]), r["market_result"]) for r in sw_rows)
        sw_sized = sum(sized_pnl(int(r["market_price"]), r["market_result"], r["position_size"])
                       for r in sw_rows if r["position_size"] and r["position_size"] > 0)
        print(f"  N={sw_total}, {sw_wins}W/{sw_total - sw_wins}L")
        print(f"  Counterfactual PnL: {sw_pnl:+d}c (1-contract), ${sw_sized:+.2f} (sized)")
        print(f"  >>> {'FILTER CORRECT' if sw_pnl <= 0 else 'FILTER MAY BE TOO STRICT'}")
    else:
        print("  No settled strategy_wait entries")

    # POR counterfactual
    subheader("price_out_of_range Counterfactual")
    por_rows = conn.execute(f"""
        SELECT market_price, market_result, position_size
        FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND filter_stage='price_out_of_range' AND market_result IS NOT NULL
    """).fetchall()

    if por_rows:
        por_wins = sum(1 for r in por_rows if r["market_result"] in ("yes", "all_yes"))
        por_total = len(por_rows)
        por_pnl = sum(cf_pnl(int(r["market_price"]), r["market_result"]) for r in por_rows)
        por_sized = sum(sized_pnl(int(r["market_price"]), r["market_result"], r["position_size"])
                        for r in por_rows if r["position_size"] and r["position_size"] > 0)
        print(f"  N={por_total}, {por_wins}W/{por_total - por_wins}L, PnL={por_pnl:+d}c, Sized=${por_sized:+.2f}")
    else:
        print("  No settled POR entries")

    # NO-side edge analysis
    subheader("NO-Side Edge Analysis")
    if has_column(conn, "evaluated_opportunities", "wx_no_side_edge"):
        no_rows = conn.execute(f"""
            SELECT wx_no_side_edge, wx_market_type, market_result, market_price
            FROM evaluated_opportunities
            WHERE product_type='weather' {W}
              AND wx_no_side_edge IS NOT NULL AND market_result IS NOT NULL
              AND filter_stage='weather_observation'
        """).fetchall()
        if no_rows:
            pos_edge = [r for r in no_rows if r["wx_no_side_edge"] > 0]
            print(f"  Total with NO edge data: {len(no_rows)}")
            print(f"  Positive NO edge: {len(pos_edge)}/{len(no_rows)} ({pct(len(pos_edge), len(no_rows))})")
            if pos_edge:
                no_wins = sum(1 for r in pos_edge if r["market_result"] in ("no", "all_no"))
                no_pnl = sum(cf_pnl(100 - int(r["market_price"]), "yes" if r["market_result"] in ("no", "all_no") else "no") for r in pos_edge)
                print(f"  If traded NO side: {no_wins}W/{len(pos_edge) - no_wins}L, PnL={no_pnl:+d}c")
        else:
            print("  No settled data with NO edge")


# ─── Section 9: Config Sensitivity ───────────────────────────────────────────

def section_config(conn, since):
    header("9. CONFIG SENSITIVITY")
    W = where_clause(since)

    # Min entry price sweep
    subheader("WEATHER_MIN_ENTRY_PRICE sweep")
    print(f"  {'MinPrice':>8} {'Sigs':>5} {'W':>4} {'L':>4} {'WR':>6} {'1c PnL':>8} {'Sized$':>9}")
    print(f"  {'-'*50}")

    for threshold in [5, 8, 10, 12, 15, 20]:
        row = conn.execute(f"""
            SELECT COUNT(*) AS n,
              SUM(CASE WHEN market_result IN ('yes','all_yes') THEN 1 ELSE 0 END) AS w,
              SUM(CASE WHEN market_result IN ('no','all_no') THEN 1 ELSE 0 END) AS l
            FROM evaluated_opportunities
            WHERE product_type='weather' {W}
              AND filter_stage='weather_observation' AND market_result IS NOT NULL
              AND market_price >= ?
        """, (threshold,)).fetchone()
        w = row["w"] or 0
        l = row["l"] or 0
        n = w + l
        wr = pct(w, n) if n > 0 else "—"

        # Compute PnL for this threshold
        pnl_rows = conn.execute(f"""
            SELECT market_price, market_result, position_size FROM evaluated_opportunities
            WHERE product_type='weather' {W}
              AND filter_stage='weather_observation' AND market_result IS NOT NULL
              AND market_price >= ?
        """, (threshold,)).fetchall()
        pnl = sum(cf_pnl(int(r["market_price"]), r["market_result"]) for r in pnl_rows)
        pnl_s = sum(sized_pnl(int(r["market_price"]), r["market_result"], r["position_size"])
                     for r in pnl_rows if r["position_size"] and r["position_size"] > 0)
        current = " <<<" if threshold == 10 else ""
        print(f"  {threshold:>7}c {n:>5} {w:>4} {l:>4} {wr:>6} {pnl:>+7d}c ${pnl_s:>+8.2f}{current}")

    # Edge threshold sweep
    subheader("WEATHER_MIN_EDGE_PCT sweep")
    rows = conn.execute(f"""
        SELECT fee_adjusted_edge, market_price, market_result, position_size
        FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND filter_stage IN ('weather_observation', 'insufficient_edge')
          AND market_result IS NOT NULL AND fee_adjusted_edge IS NOT NULL
    """).fetchall()

    if rows:
        print(f"  {'MinEdge':>8} {'Sigs':>5} {'W':>4} {'L':>4} {'WR':>6} {'1c PnL':>8} {'Sized$':>9}")
        print(f"  {'-'*50}")
        for edge_thresh in [0.001, 0.003, 0.005, 0.01, 0.02, 0.05]:
            passed = [r for r in rows if r["fee_adjusted_edge"] >= edge_thresh]
            w = sum(1 for r in passed if r["market_result"] in ("yes", "all_yes"))
            l = len(passed) - w
            pnl = sum(cf_pnl(int(r["market_price"]), r["market_result"]) for r in passed)
            pnl_s = sum(sized_pnl(int(r["market_price"]), r["market_result"], r["position_size"])
                         for r in passed if r["position_size"] and r["position_size"] > 0)
            wr = pct(w, len(passed)) if passed else "—"
            current = " <<<" if edge_thresh == 0.001 else ""
            print(f"  {edge_thresh:>7.3f} {len(passed):>5} {w:>4} {l:>4} {wr:>6} {pnl:>+7d}c ${pnl_s:>+8.2f}{current}")


# ─── Section 10: Bias Correction ─────────────────────────────────────────────

def section_bias(conn, since):
    header("10. BIAS CORRECTION STATUS")
    W = where_clause(since)

    rows = conn.execute(f"""
        SELECT asset, wx_bias_correction, wx_ensemble_mean, wx_actual_high_temp,
               DATE(evaluation_time) AS day
        FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND wx_bias_correction IS NOT NULL
        GROUP BY asset, DATE(evaluation_time)
        ORDER BY asset, day
    """).fetchall()

    if not rows:
        print("  No bias data")
        return

    # Aggregate per city
    city_bias = defaultdict(list)
    for r in rows:
        city_bias[r["asset"]].append(r["wx_bias_correction"])

    print(f"  {'City':<12} {'N':>4} {'Mean':>7} {'Min':>7} {'Max':>7} {'NonZero':>8}")
    print(f"  {'-'*50}")
    for city in sorted(city_bias.keys()):
        vals = city_bias[city]
        nonzero = sum(1 for v in vals if abs(v) > 0.01)
        print(f"  {city:<12} {len(vals):>4} {sum(vals)/len(vals):>+7.2f} {min(vals):>+7.2f} {max(vals):>+7.2f} {nonzero:>8}")

    total_nonzero = sum(1 for r in rows if abs(r["wx_bias_correction"]) > 0.01)
    total = len(rows)
    print(f"\n  Bias active: {total_nonzero}/{total} city-days ({pct(total_nonzero, total)})")
    if total_nonzero == 0:
        print(f"  >>> Bias correction is INACTIVE — needs more settlement cycles to accumulate errors")


# ─── Section 11: Market Type × Price Cross-Tab ───────────────────────────────

def section_cross_tab(conn, since):
    header("11. MARKET TYPE x PRICE CROSS-TAB")
    W = where_clause(since)

    rows = conn.execute(f"""
        SELECT wx_market_type, market_price, market_result, position_size
        FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND filter_stage='weather_observation' AND market_result IS NOT NULL
    """).fetchall()

    if not rows:
        print("  No settled data")
        return

    grid = defaultdict(lambda: {"w": 0, "l": 0, "pnl_1c": 0, "pnl_sized": 0.0})
    for r in rows:
        mt = r["wx_market_type"] or "unknown"
        price = int(r["market_price"])
        if price <= 15: pb = "1-15c"
        elif price <= 30: pb = "16-30c"
        elif price <= 60: pb = "31-60c"
        else: pb = "61c+"

        key = (mt, pb)
        is_win = r["market_result"] in ("yes", "all_yes")
        grid[key]["w" if is_win else "l"] += 1
        grid[key]["pnl_1c"] += cf_pnl(price, r["market_result"])
        ps = r["position_size"]
        if ps and ps > 0:
            grid[key]["pnl_sized"] += sized_pnl(price, r["market_result"], ps)

    mtypes = sorted(set(k[0] for k in grid.keys()))
    pbuckets = ["1-15c", "16-30c", "31-60c", "61c+"]

    # Header
    hdr = f"  {'':15s}"
    for pb in pbuckets:
        hdr += f"  {pb:>28s}"
    print(hdr)
    print(f"  {'-'*100}")

    for mt in mtypes:
        row_str = f"  {mt:15s}"
        for pb in pbuckets:
            key = (mt, pb)
            if key in grid:
                s = grid[key]
                n = s["w"] + s["l"]
                wr = s["w"] / n * 100 if n > 0 else 0
                row_str += f"  {s['w']}W/{s['l']}L {wr:.0f}% {s['pnl_1c']:+d}c ${s['pnl_sized']:+.2f}"
            else:
                row_str += f"  {'—':>28s}"
        print(row_str)


# ─── Section 12: Data Sufficiency ─────────────────────────────────────────────

def section_sufficiency(conn, since, stats, pipeline):
    header("12. READINESS ASSESSMENT")
    W = where_clause(since)

    settled_sigs = conn.execute(f"""
        SELECT COUNT(*) FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND filter_stage='weather_observation' AND market_result IS NOT NULL
    """).fetchone()[0] or 0

    sig_cities = conn.execute(f"""
        SELECT COUNT(DISTINCT asset) FROM evaluated_opportunities
        WHERE product_type='weather' {W} AND filter_stage='weather_observation'
    """).fetchone()[0] or 0

    ens_cov = pipeline.get("coverage", 0)

    # Check if any config produces positive PnL
    pnl_rows = conn.execute(f"""
        SELECT market_price, market_result, position_size FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND filter_stage='weather_observation' AND market_result IS NOT NULL
    """).fetchall()
    total_pnl = sum(cf_pnl(int(r["market_price"]), r["market_result"]) for r in pnl_rows) if pnl_rows else 0
    total_sized = sum(sized_pnl(int(r["market_price"]), r["market_result"], r["position_size"])
                      for r in pnl_rows if r["position_size"] and r["position_size"] > 0) if pnl_rows else 0.0

    checks = [
        ("Total evaluations >= 500", stats["total"] >= 500, f"{stats['total']}/500"),
        ("Signals >= 100", stats["signals"] >= 100, f"{stats['signals']}/100"),
        ("Settled signals >= 50", settled_sigs >= 50, f"{settled_sigs}/50"),
        ("Days of data >= 14", stats["span_hrs"] / 24 >= 14, f"{stats['span_hrs']/24:.1f}/14 days"),
        ("Cities signaling >= 5", sig_cities >= 5, f"{sig_cities}/5"),
        ("Ensemble coverage > 90%", ens_cov > 90, f"{ens_cov:.1f}%"),
        ("Signal PnL positive", total_pnl > 0, f"{total_pnl:+d}c (1-contract), ${total_sized:+.2f} (sized)"),
    ]

    for desc, passed, detail in checks:
        status = "[+]" if passed else "[ ]"
        print(f"  {status} {desc}: {detail}")

    n_pass = sum(1 for _, p, _ in checks if p)
    print(f"\n  {n_pass}/{len(checks)} checks passing", end="")
    if n_pass == len(checks):
        print(" — READY for promotion evaluation")
    else:
        print(" — continue data collection")

    if stats["total"] > 0 and stats["span_hrs"] > 0:
        rate = stats["total"] / stats["span_hrs"]
        if stats["total"] < 500 and rate > 0:
            hrs_to_500 = (500 - stats["total"]) / rate
            print(f"\n  Eval rate: {rate:.1f}/hr | Est. time to 500 evals: {hrs_to_500/24:.1f} days")


# ─── Section 13: CalEngine Pipeline ──────────────────────────────────────────

def section_cal_engine(conn, since):
    header("CALENGINE OBSERVATION PIPELINE")
    W = where_clause(since)

    row = conn.execute(f"""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN raw_prob IS NOT NULL THEN 1 ELSE 0 END) AS with_rp,
               SUM(CASE WHEN market_result IS NOT NULL AND raw_prob IS NOT NULL THEN 1 ELSE 0 END) AS cal_eligible
        FROM evaluated_opportunities WHERE product_type='weather' {W}
    """).fetchone()

    print(f"  Total weather evals:      {row['total']}")
    print(f"  With raw_prob:            {row['with_rp']}")
    print(f"  Settled + raw_prob (cal):  {row['cal_eligible']}")

    # Per market_type
    mt_rows = conn.execute(f"""
        SELECT COALESCE(wx_market_type, 'unknown') AS mtype,
               SUM(CASE WHEN market_result IS NOT NULL AND raw_prob IS NOT NULL THEN 1 ELSE 0 END) AS cal
        FROM evaluated_opportunities WHERE product_type='weather' {W}
        GROUP BY mtype ORDER BY cal DESC
    """).fetchall()
    if mt_rows:
        print()
        for r in mt_rows:
            cal = r["cal"] or 0
            if cal > 0:
                print(f"    {r['mtype']:<15} {cal} cal obs")

    cal = row["cal_eligible"] or 0
    if cal > 0:
        print(f"\n  >>> {cal} observations feeding weather CalEngines")
    else:
        print("\n  >>> No CalEngine observations yet")


def detect_regime_start() -> str:
    """Auto-detect regime start by finding the last git commit that changed
    weather trading constants in bot/_impl.py."""
    import subprocess

    REGIME_CONSTANTS = [
        "WEATHER_OBSERVATION_ONLY", "WEATHER_MIN_ENTRY_PRICE",
        "WEATHER_MAX_ENTRY_PRICE", "WEATHER_MARKET_BLEND_W",
        "WEATHER_MIN_EDGE_PCT", "WEATHER_MAX_RISK_PER_TRADE",
        "WEATHER_KELLY_FRACTION", "WEATHER_MIN_SECONDS_BEFORE_CLOSE",
        "WEATHER_MAX_SECONDS_BEFORE_CLOSE",
    ]

    # Bit 11.2 (2026-05-12): relocated to scripts/audit/; 3-level dirname.
    repo_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        result = subprocess.run(
            ["git", "log", "--format=%H %aI", "--since=180 days ago",
             "--", "bot.py", "bot/_impl.py", "bot/constants.py"],
            capture_output=True, text=True, timeout=10, cwd=repo_dir,
        )
        if result.returncode != 0:
            return "2026-02-28T00:00:00"
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split(" ", 1)
            commit_hash, timestamp = parts[0], parts[1] if len(parts) > 1 else ""
            diff_result = subprocess.run(
                ["git", "diff", f"{commit_hash}^..{commit_hash}", "--", "bot.py", "bot/_impl.py", "bot/constants.py"],
                capture_output=True, text=True, timeout=10, cwd=repo_dir,
            )
            if diff_result.returncode != 0:
                continue
            found = False
            for ln in diff_result.stdout.split("\n"):
                if not (ln.startswith("+") or ln.startswith("-")):
                    continue
                if any(f"{c} =" in ln or f"{c}=" in ln
                       for c in REGIME_CONSTANTS):
                    found = True
                    break
            if found:
                dt = datetime.fromisoformat(timestamp)
                return dt.strftime("%Y-%m-%dT%H:%M:%S")
        return "2026-02-28T00:00:00"
    except Exception:
        return "2026-02-28T00:00:00"


def section_no_side(conn, since):
    """NO-side shadow analysis from evaluated_opportunities where side='no'."""
    header("NO-SIDE SHADOW ANALYSIS")

    if not has_column(conn, "evaluated_opportunities", "side"):
        print("  side column not found on evaluated_opportunities — skipping")
        return

    W = where_clause(since)
    no_count = conn.execute(f"""
        SELECT COUNT(*) FROM evaluated_opportunities
        WHERE product_type='weather' AND side='no' {W}
    """).fetchone()[0]

    if no_count == 0:
        print("  No NO-side shadow entries found.")
        return

    settled_count = conn.execute(f"""
        SELECT COUNT(*) FROM evaluated_opportunities
        WHERE product_type='weather' AND side='no'
          AND market_result IS NOT NULL {W}
    """).fetchone()[0]

    print(f"  NO-side signals: {no_count} total, {settled_count} settled")

    if settled_count == 0:
        print("  No settled NO-side data yet.")
        return

    # Win/loss — NO wins when market_result IN ('no','all_no')
    rows = conn.execute(f"""
        SELECT market_price, COALESCE(position_size, 1) AS cnt, market_result,
               asset, edge, fee_adjusted_edge
        FROM evaluated_opportunities
        WHERE product_type='weather' AND side='no'
          AND market_result IS NOT NULL {W}
    """).fetchall()

    wins = sum(1 for r in rows if r["market_result"] in ("no", "all_no"))
    losses = sum(1 for r in rows if r["market_result"] in ("yes", "all_yes"))
    wr = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0

    sim_pnl = 0
    for r in rows:
        p = r["market_price"] or 0
        c = r["cnt"]
        fee = taker_fee(p)
        if r["market_result"] in ("no", "all_no"):
            sim_pnl += (100 - p) * c - fee * c
        elif r["market_result"] in ("yes", "all_yes"):
            sim_pnl -= p * c + fee * c

    print(f"  Win rate:     {wins}W/{losses}L ({wr:.1f}%)")
    print(f"  Sim PnL:      {sim_pnl}c (${sim_pnl/100:.2f})")

    # Per-asset
    subheader("NO-side per-asset")
    asset_data = defaultdict(lambda: {"w": 0, "l": 0})
    for r in rows:
        a = r["asset"]
        if r["market_result"] in ("no", "all_no"):
            asset_data[a]["w"] += 1
        elif r["market_result"] in ("yes", "all_yes"):
            asset_data[a]["l"] += 1

    print(f"  {'City/Asset':<12} {'W':>4} {'L':>4} {'WR':>7}")
    print("  " + "-" * 30)
    for a in sorted(asset_data.keys()):
        d = asset_data[a]
        n = d["w"] + d["l"]
        wr_a = d["w"] / n * 100 if n > 0 else 0
        print(f"  {a:<12} {d['w']:>4} {d['l']:>4} {wr_a:>6.1f}%")

    # Edge distribution
    subheader("NO-side edge distribution")
    edge_data = defaultdict(lambda: {"w": 0, "l": 0, "n": 0})
    for r in rows:
        fe = r["fee_adjusted_edge"]
        if fe is None:
            bucket = "N/A"
        elif fe < 0:
            bucket = "<0%"
        elif fe < 0.005:
            bucket = "0-0.5%"
        elif fe < 0.01:
            bucket = "0.5-1%"
        elif fe < 0.02:
            bucket = "1-2%"
        elif fe < 0.05:
            bucket = "2-5%"
        else:
            bucket = "5%+"
        edge_data[bucket]["n"] += 1
        if r["market_result"] in ("no", "all_no"):
            edge_data[bucket]["w"] += 1
        elif r["market_result"] in ("yes", "all_yes"):
            edge_data[bucket]["l"] += 1

    print(f"  {'Bucket':<10} {'N':>4} {'W':>4} {'L':>4} {'WR':>7}")
    print("  " + "-" * 33)
    for b in ["<0%", "0-0.5%", "0.5-1%", "1-2%", "2-5%", "5%+", "N/A"]:
        if b in edge_data:
            d = edge_data[b]
            n = d["w"] + d["l"]
            wr_b = d["w"] / n * 100 if n > 0 else 0
            print(f"  {b:<10} {d['n']:>4} {d['w']:>4} {d['l']:>4} {wr_b:>6.1f}%")

    # YES vs NO comparison
    subheader("YES-side vs NO-side comparison")
    yes_rows = conn.execute(f"""
        SELECT
            SUM(CASE WHEN market_result IN ('yes','all_yes') THEN 1 ELSE 0 END) AS w,
            SUM(CASE WHEN market_result IN ('no','all_no') THEN 1 ELSE 0 END) AS l
        FROM evaluated_opportunities
        WHERE product_type='weather' AND (side IS NULL OR side='yes')
          AND filter_stage='weather_observation'
          AND market_result IS NOT NULL {W}
    """).fetchone()
    yes_w = yes_rows["w"] or 0
    yes_l = yes_rows["l"] or 0
    yes_n = yes_w + yes_l
    yes_wr = yes_w / yes_n * 100 if yes_n > 0 else 0
    print(f"  YES-side: {yes_w}W/{yes_l}L ({yes_wr:.1f}% WR, n={yes_n})")
    print(f"  NO-side:  {wins}W/{losses}L ({wr:.1f}% WR, n={wins + losses})")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Weather shadow mode audit")
    parser.add_argument("--db", default="state.db")
    parser.add_argument("--since", default=None)
    parser.add_argument("--regime", choices=["auto"],
                        help="Auto-detect regime start from git history")
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    conn = connect_db(args.db)

    if args.regime == "auto":
        since = detect_regime_start()
        print(f"[Auto-detected regime start: {since}]")
    else:
        since = args.since

    W = where_clause(since)
    n = conn.execute(f"SELECT COUNT(*) FROM evaluated_opportunities WHERE product_type='weather' {W}").fetchone()[0]
    if not n:
        print(f"ERROR: No weather evaluations since {since}")
        sys.exit(1)

    print(f"Weather Shadow Audit — since {since or 'all time'}")
    print(f"DB: {args.db} ({n} weather evaluations)")

    stats = section_overview(conn, args.since)
    section_per_city(conn, args.since)
    section_daily_pnl(conn, args.since)
    section_market_type_pnl(conn, args.since)
    section_price_buckets(conn, args.since)
    section_calibration(conn, args.since)
    section_forecast_accuracy(conn, args.since)
    pipeline = section_ensemble_health(conn, args.since)
    section_blend_sim(conn, args.since)
    section_stc(conn, args.since)
    section_leaks(conn, args.since)
    section_cross_tab(conn, args.since)
    section_config(conn, args.since)
    section_bias(conn, args.since)
    section_sufficiency(conn, args.since, stats, pipeline or {})
    section_cal_engine(conn, args.since)
    section_no_side(conn, args.since)

    conn.close()

    print(f"\n{'=' * 72}")
    print(f"  Audit complete.")
    print(f"{'=' * 72}")


if __name__ == "__main__":
    main()
