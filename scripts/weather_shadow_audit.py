#!/usr/bin/env python3
"""Weather shadow mode audit script — comprehensive analysis of weather observation data.

Reads from state.db evaluated_opportunities (product_type='weather').
Follows SPX audit pattern: --since, --json, --db flags, PRAGMA busy_timeout.

Usage:
    scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
    python3 scripts/weather_shadow_audit.py [--db /tmp/state.db] [--since 2026-03-02T16:54:00] [--json weather_audit.json]
"""

import argparse
import json
import math
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


def cf_pnl(price_cents: int, result: str, is_maker: bool = True) -> int:
    """Counterfactual PnL per contract in cents. YES-side only (weather observation buys YES).

    Maker fee: ceil(0.0175 * 100 * p * (1-p))
    Taker fee: ceil(0.07 * 100 * p * (1-p))
    Win: 100 - price - fee, Loss: -(price + fee)
    """
    p = price_cents / 100.0
    if is_maker:
        fee = math.ceil(0.0175 * 100 * p * (1 - p))
    else:
        fee = math.ceil(0.07 * 100 * p * (1 - p))
    if result in ("yes", "all_yes"):
        return 100 - price_cents - fee
    else:
        return -(price_cents + fee)


def wilson_ci(wins: int, total: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson score confidence interval for a proportion."""
    if total == 0:
        return (0.0, 0.0)
    p_hat = wins / total
    denom = 1 + z * z / total
    center = (p_hat + z * z / (2 * total)) / denom
    spread = z * math.sqrt((p_hat * (1 - p_hat) + z * z / (4 * total)) / total) / denom
    return (max(0, center - spread), min(1, center + spread))


def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Check if a column exists in a table."""
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(c["name"] == column for c in cols)


# ─── Section 1: Overview ──────────────────────────────────────────────────────

def section_overview(conn: sqlite3.Connection, since: Optional[str]) -> dict:
    """Overall shadow data summary."""
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
    du = row["du"] or 0
    zs = row["zs"] or 0

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
        ("price_out_of_range", por), ("strategy_wait", sw), ("insufficient_edge", ie),
        ("weather_observation", signals), ("data_unavailable", du), ("zero_sizing", zs),
        ("low_probability", row["lp"] or 0),
    ]
    for name, count in stages:
        if count > 0:
            print(f"  {name:<25} {count:>4} ({pct(count, total)})")

    # Per-city breakdown
    subheader("Per-City Breakdown")
    rows = conn.execute(f"""
        SELECT asset, COUNT(*) AS n,
          SUM(CASE WHEN filter_stage='weather_observation' THEN 1 ELSE 0 END) AS sigs,
          SUM(CASE WHEN filter_stage='insufficient_edge' THEN 1 ELSE 0 END) AS ie,
          SUM(CASE WHEN filter_stage='price_out_of_range' THEN 1 ELSE 0 END) AS por,
          AVG(CASE WHEN wx_n_members IS NOT NULL THEN wx_n_members END) AS avg_mem
        FROM evaluated_opportunities WHERE product_type='weather' {W}
        GROUP BY asset ORDER BY n DESC
    """).fetchall()

    print(f"  {'City':<12} {'Total':>6} {'Signals':>8} {'IE':>5} {'POR':>5} {'AvgMem':>7}")
    print(f"  {'-'*50}")
    for r in rows:
        avg_mem = f"{r['avg_mem']:.0f}" if r["avg_mem"] else "—"
        print(f"  {r['asset']:<12} {r['n']:>6} {r['sigs']:>8} {r['ie']:>5} {r['por']:>5} {avg_mem:>7}")

    return {
        "total": total, "signals": signals, "span_hrs": span_hrs,
        "por": por, "sw": sw, "ie": ie, "du": du, "zs": zs,
    }


# ─── Section 2: Settlement Outcomes ──────────────────────────────────────────

def section_settlement(conn: sqlite3.Connection, since: Optional[str]) -> dict:
    """Settlement outcomes and simulated PnL."""
    header("2. SETTLEMENT OUTCOMES + SIMULATED PnL")
    W = where_clause(since)

    # Overall settled counts
    rows = conn.execute(f"""
        SELECT filter_stage,
          COUNT(*) AS n,
          SUM(CASE WHEN market_result IN ('yes', 'all_yes') THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN market_result IN ('no', 'all_no') THEN 1 ELSE 0 END) AS losses,
          SUM(CASE WHEN market_result IS NULL THEN 1 ELSE 0 END) AS unsettled
        FROM evaluated_opportunities
        WHERE product_type='weather' {W}
        GROUP BY filter_stage
        ORDER BY n DESC
    """).fetchall()

    total_settled = 0
    total_pnl = 0
    signal_pnl = 0
    signal_wins = 0
    signal_total = 0
    ie_pnl = 0
    ie_wins = 0
    ie_total = 0

    print(f"  {'Stage':<25} {'Total':>6} {'Settled':>8} {'Wins':>5} {'Losses':>7} {'WR':>8}")
    print(f"  {'-'*65}")
    for r in rows:
        settled = (r["wins"] or 0) + (r["losses"] or 0)
        total_settled += settled
        wr = pct(r["wins"] or 0, settled)
        print(f"  {r['filter_stage']:<25} {r['n']:>6} {settled:>8} {r['wins'] or 0:>5} {r['losses'] or 0:>7} {wr:>8}")

    # Compute PnL for signals and IE near-misses
    settled_rows = conn.execute(f"""
        SELECT filter_stage, market_price, market_result
        FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND market_result IS NOT NULL
          AND market_price IS NOT NULL
    """).fetchall()

    for r in settled_rows:
        price = int(r["market_price"])
        pnl_val = cf_pnl(price, r["market_result"])
        total_pnl += pnl_val
        if r["filter_stage"] == "weather_observation":
            signal_pnl += pnl_val
            signal_total += 1
            if r["market_result"] in ("yes", "all_yes"):
                signal_wins += 1
        elif r["filter_stage"] == "insufficient_edge":
            ie_pnl += pnl_val
            ie_total += 1
            if r["market_result"] in ("yes", "all_yes"):
                ie_wins += 1

    subheader("Simulated PnL (Maker Fees)")
    print(f"  Signals:     {signal_total} settled, PnL={signal_pnl:+d}¢", end="")
    if signal_total > 0:
        lo, hi = wilson_ci(signal_wins, signal_total)
        print(f"  WR={pct(signal_wins, signal_total)} CI=[{lo:.1%},{hi:.1%}]")
    else:
        print()
    print(f"  IE near-miss: {ie_total} settled, PnL={ie_pnl:+d}¢", end="")
    if ie_total > 0:
        lo, hi = wilson_ci(ie_wins, ie_total)
        print(f"  WR={pct(ie_wins, ie_total)} CI=[{lo:.1%},{hi:.1%}]")
    else:
        print()
    print(f"  Total:       PnL={total_pnl:+d}¢ ({total_pnl/100:+.2f}$)")

    return {
        "total_settled": total_settled,
        "signal_pnl": signal_pnl, "signal_wins": signal_wins, "signal_total": signal_total,
        "ie_pnl": ie_pnl, "ie_wins": ie_wins, "ie_total": ie_total,
        "total_pnl": total_pnl,
    }


# ─── Section 3: wx_market_type Breakdown ─────────────────────────────────────

def section_market_type(conn: sqlite3.Connection, since: Optional[str]) -> dict:
    """GROUP BY wx_market_type breakdowns."""
    header("3. MARKET TYPE BREAKDOWN (wx_market_type)")
    W = where_clause(since)

    rows = conn.execute(f"""
        SELECT COALESCE(wx_market_type, 'NULL') AS mtype,
          COUNT(*) AS n,
          SUM(CASE WHEN filter_stage='weather_observation' THEN 1 ELSE 0 END) AS sigs,
          SUM(CASE WHEN filter_stage='insufficient_edge' THEN 1 ELSE 0 END) AS ie,
          AVG(CASE WHEN fee_adjusted_edge IS NOT NULL THEN fee_adjusted_edge END) AS avg_edge,
          AVG(CASE WHEN wx_n_members IS NOT NULL THEN wx_n_members END) AS avg_mem,
          AVG(CASE WHEN wx_ensemble_std IS NOT NULL THEN wx_ensemble_std END) AS avg_std
        FROM evaluated_opportunities WHERE product_type='weather' {W}
        GROUP BY mtype ORDER BY n DESC
    """).fetchall()

    result = {}
    print(f"  {'Type':<15} {'Total':>6} {'Signals':>8} {'IE':>5} {'AvgEdge':>10} {'AvgMem':>7} {'AvgStd':>8}")
    print(f"  {'-'*65}")
    for r in rows:
        avg_edge = f"{r['avg_edge']:.4f}" if r["avg_edge"] is not None else "—"
        avg_mem = f"{r['avg_mem']:.0f}" if r["avg_mem"] is not None else "—"
        avg_std = f"{r['avg_std']:.2f}" if r["avg_std"] is not None else "—"
        print(f"  {r['mtype']:<15} {r['n']:>6} {r['sigs']:>8} {r['ie']:>5} {avg_edge:>10} {avg_mem:>7} {avg_std:>8}")
        result[r["mtype"]] = {"n": r["n"], "sigs": r["sigs"] or 0, "ie": r["ie"] or 0}

    return result


# ─── Section 4: Data Pipeline + Quality Audit ────────────────────────────────

def section_pipeline(conn: sqlite3.Connection, since: Optional[str]) -> dict:
    """Data pipeline and quality audit."""
    header("4. DATA PIPELINE + QUALITY AUDIT")
    W = where_clause(since)

    # Ensemble coverage
    ens_row = conn.execute(f"""
        SELECT
          SUM(CASE WHEN wx_ensemble_mean IS NOT NULL THEN 1 ELSE 0 END) AS has_ens,
          SUM(CASE WHEN wx_ensemble_mean IS NULL THEN 1 ELSE 0 END) AS null_ens,
          COUNT(*) AS total,
          AVG(CASE WHEN wx_n_members IS NOT NULL THEN wx_n_members END) AS avg_members,
          MIN(CASE WHEN wx_n_members IS NOT NULL THEN wx_n_members END) AS min_members,
          MAX(CASE WHEN wx_n_members IS NOT NULL THEN wx_n_members END) AS max_members,
          AVG(CASE WHEN wx_ensemble_std IS NOT NULL THEN wx_ensemble_std END) AS avg_std
        FROM evaluated_opportunities WHERE product_type='weather' {W}
    """).fetchone()

    total = ens_row["total"] or 0
    has_ens = ens_row["has_ens"] or 0
    null_ens = ens_row["null_ens"] or 0
    coverage = safe_div(has_ens, total) * 100

    subheader("Ensemble Data Coverage")
    print(f"  With ensemble:    {has_ens}/{total} ({coverage:.1f}%)")
    print(f"  NULL ensemble:    {null_ens}/{total} ({100-coverage:.1f}%)")
    if ens_row["avg_members"]:
        print(f"  Avg members:      {ens_row['avg_members']:.0f} (expected 82: 31 GFS + 51 ECMWF)")
        print(f"  Member range:     {ens_row['min_members']}-{ens_row['max_members']}")
    if ens_row["avg_std"]:
        print(f"  Avg ensemble std: {ens_row['avg_std']:.2f}F")

    ecmwf_present = (ens_row["max_members"] or 0) > 31
    print(f"\n  ECMWF status:     {'PRESENT' if ecmwf_present else 'ABSENT (only GFS)'}")

    # raw_prob availability check (E4)
    subheader("raw_prob Availability")
    rp_row = conn.execute(f"""
        SELECT
          SUM(CASE WHEN raw_prob IS NULL THEN 1 ELSE 0 END) AS null_rp,
          SUM(CASE WHEN raw_prob IS NOT NULL THEN 1 ELSE 0 END) AS has_rp,
          COUNT(*) AS total,
          AVG(CASE WHEN raw_prob IS NOT NULL THEN raw_prob END) AS avg_rp,
          MIN(CASE WHEN raw_prob IS NOT NULL THEN raw_prob END) AS min_rp,
          MAX(CASE WHEN raw_prob IS NOT NULL THEN raw_prob END) AS max_rp
        FROM evaluated_opportunities WHERE product_type='weather' {W}
    """).fetchone()
    null_rp = rp_row["null_rp"] or 0
    has_rp = rp_row["has_rp"] or 0
    rp_total = rp_row["total"] or 1
    null_pct = safe_div(null_rp, rp_total) * 100
    print(f"  raw_prob populated: {has_rp}/{rp_total} ({100-null_pct:.1f}%)")
    print(f"  raw_prob NULL:      {null_rp}/{rp_total} ({null_pct:.1f}%)")
    if null_pct > 20:
        print(f"  >>> WARNING: {null_pct:.0f}% NULL raw_prob — blocks blend weight simulation")
    if rp_row["avg_rp"] is not None:
        print(f"  raw_prob range:     [{rp_row['min_rp']:.3f}, {rp_row['max_rp']:.3f}], mean={rp_row['avg_rp']:.3f}")

    # Market price distribution
    subheader("Market Price Distribution")
    rows = conn.execute(f"""
        SELECT
          CASE
            WHEN market_price <= 5 THEN '0-5c'
            WHEN market_price <= 15 THEN '6-15c'
            WHEN market_price <= 30 THEN '16-30c'
            WHEN market_price <= 50 THEN '31-50c'
            WHEN market_price <= 70 THEN '51-70c'
            WHEN market_price <= 85 THEN '71-85c'
            WHEN market_price <= 95 THEN '86-95c'
            ELSE '96-100c'
          END AS bucket,
          COUNT(*) AS n,
          SUM(CASE WHEN filter_stage='weather_observation' THEN 1 ELSE 0 END) AS sigs
        FROM evaluated_opportunities WHERE product_type='weather' {W}
        GROUP BY bucket ORDER BY MIN(market_price)
    """).fetchall()

    tradeable = 0
    for r in rows:
        flag = " [TRADEABLE]" if r["bucket"] in ("16-30c", "31-50c", "51-70c", "71-85c", "86-95c") else ""
        if flag:
            tradeable += r["n"]
        print(f"  {r['bucket']:<10} {r['n']:>4} ({pct(r['n'], total)}) sigs={r['sigs']}{flag}")

    if total > 0:
        print(f"\n  Tradeable range (15-95c): {tradeable}/{total} ({pct(tradeable, total)})")

    # Coverage
    cov = conn.execute(f"""
        SELECT COUNT(DISTINCT ticker) AS tickers,
          COUNT(DISTINCT event_ticker) AS events,
          COUNT(DISTINCT asset) AS cities
        FROM evaluated_opportunities WHERE product_type='weather' {W}
    """).fetchone()
    print(f"\n  Distinct tickers: {cov['tickers']}")
    print(f"  Distinct events:  {cov['events']}")
    print(f"  Distinct cities:  {cov['cities']}/5")

    return {
        "ensemble_coverage": coverage,
        "ecmwf_present": ecmwf_present,
        "avg_members": ens_row["avg_members"],
        "tradeable_pct": safe_div(tradeable, total) * 100,
        "raw_prob_null_pct": null_pct,
    }


# ─── Section 5: NO-Side Edge Analysis ────────────────────────────────────────

def section_no_side_edge(conn: sqlite3.Connection, since: Optional[str]) -> dict:
    """Analyze NO-side shadow edge distribution."""
    header("5. NO-SIDE EDGE ANALYSIS")
    W = where_clause(since)

    has_col = has_column(conn, "evaluated_opportunities", "wx_no_side_edge")
    if not has_col:
        print("  DATA GAP: wx_no_side_edge column not yet present")
        return {"status": "column_missing"}

    row = conn.execute(f"""
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN wx_no_side_edge IS NOT NULL THEN 1 ELSE 0 END) AS populated,
          AVG(CASE WHEN wx_no_side_edge IS NOT NULL THEN wx_no_side_edge END) AS avg_edge,
          MIN(CASE WHEN wx_no_side_edge IS NOT NULL THEN wx_no_side_edge END) AS min_edge,
          MAX(CASE WHEN wx_no_side_edge IS NOT NULL THEN wx_no_side_edge END) AS max_edge,
          SUM(CASE WHEN wx_no_side_edge > 0 THEN 1 ELSE 0 END) AS positive_edge
        FROM evaluated_opportunities WHERE product_type='weather' {W}
    """).fetchone()

    populated = row["populated"] or 0
    total = row["total"] or 0
    pos = row["positive_edge"] or 0
    print(f"  Populated:     {populated}/{total} ({pct(populated, total)})")
    if populated > 0:
        print(f"  Avg NO edge:   {row['avg_edge']:.4f}")
        print(f"  Range:         [{row['min_edge']:.4f}, {row['max_edge']:.4f}]")
        print(f"  Positive edge: {pos}/{populated} ({pct(pos, populated)})")

    # Per market_type
    rows = conn.execute(f"""
        SELECT COALESCE(wx_market_type, 'NULL') AS mtype,
          AVG(wx_no_side_edge) AS avg_edge,
          SUM(CASE WHEN wx_no_side_edge > 0 THEN 1 ELSE 0 END) AS pos,
          COUNT(*) AS n
        FROM evaluated_opportunities
        WHERE product_type='weather' {W} AND wx_no_side_edge IS NOT NULL
        GROUP BY mtype ORDER BY n DESC
    """).fetchall()

    if rows:
        subheader("By Market Type")
        for r in rows:
            print(f"  {r['mtype']:<15} avg={r['avg_edge']:.4f}  pos={r['pos']}/{r['n']} ({pct(r['pos'], r['n'])})")

    return {
        "populated": populated,
        "avg_edge": row["avg_edge"],
        "positive_pct": safe_div(pos, populated) * 100 if populated > 0 else 0,
    }


# ─── Section 6: Blend Weight Simulation ──────────────────────────────────────

def section_blend_sim(conn: sqlite3.Connection, since: Optional[str]) -> dict:
    """Simulate different BLEND_W values on settled data with raw_prob."""
    header("6. BLEND WEIGHT SIMULATION")
    W = where_clause(since)

    rows = conn.execute(f"""
        SELECT raw_prob, market_price, market_result
        FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND raw_prob IS NOT NULL
          AND market_price IS NOT NULL
          AND market_result IS NOT NULL
    """).fetchall()

    if len(rows) < 5:
        print(f"  INSUFFICIENT DATA: {len(rows)} settled rows with raw_prob (need >= 5)")
        return {"status": "insufficient_data", "n": len(rows)}

    blend_weights = [0.0, 0.10, 0.20, 0.30, 0.40, 0.50]
    results = {}

    print(f"  {'BLEND_W':>8} {'Brier':>8} {'PnL¢':>8} {'Signals':>8} {'WR':>8}")
    print(f"  {'-'*50}")

    for bw in blend_weights:
        total_brier = 0
        total_pnl = 0
        signal_count = 0
        signal_wins = 0
        min_edge_pct = 0.001  # current WEATHER_MIN_EDGE_PCT

        for r in rows:
            rp = r["raw_prob"]
            mp = int(r["market_price"])
            market_p = mp / 100.0

            # Blended prob
            blended = (1.0 - bw) * rp + bw * market_p

            # Brier score
            outcome = 1 if r["market_result"] in ("yes", "all_yes") else 0
            total_brier += (blended - outcome) ** 2

            # Would this be a signal? (taker fee — bot uses taker for weather)
            edge = blended - market_p
            fee = math.ceil(0.07 * 100 * market_p * (1 - market_p))
            fee_edge = edge - fee / 100.0
            if fee_edge >= min_edge_pct:
                signal_count += 1
                pnl_val = cf_pnl(mp, r["market_result"])
                total_pnl += pnl_val
                if r["market_result"] in ("yes", "all_yes"):
                    signal_wins += 1

        brier = total_brier / len(rows)
        wr = pct(signal_wins, signal_count) if signal_count > 0 else "—"
        print(f"  {bw:>8.2f} {brier:>8.4f} {total_pnl:>+8d} {signal_count:>8} {wr:>8}")
        results[str(bw)] = {
            "brier": round(brier, 4), "pnl": total_pnl,
            "signals": signal_count, "wins": signal_wins,
        }

    # Find best
    best_brier_bw = min(results.items(), key=lambda x: x[1]["brier"])
    best_pnl_bw = max(results.items(), key=lambda x: x[1]["pnl"])
    print(f"\n  Best Brier:  BLEND_W={best_brier_bw[0]} ({best_brier_bw[1]['brier']:.4f})")
    print(f"  Best PnL:    BLEND_W={best_pnl_bw[0]} ({best_pnl_bw[1]['pnl']:+d}¢)")

    return results


# ─── Section 7: Forecast vs Observed Accuracy ────────────────────────────────

def section_forecast_accuracy(conn: sqlite3.Connection, since: Optional[str]) -> dict:
    """Evaluate forecast accuracy if wx_actual_high_temp is available."""
    header("7. FORECAST vs OBSERVED ACCURACY")
    W = where_clause(since)

    has_col = has_column(conn, "evaluated_opportunities", "wx_actual_high_temp")
    if not has_col:
        print("  DATA GAP: wx_actual_high_temp column not yet present in DB")
        return {"status": "column_missing"}

    # Dedup by city + date: same forecast repeated across strikes
    rows = conn.execute(f"""
        SELECT asset, wx_ensemble_mean, wx_actual_high_temp, wx_bias_correction,
               DATE(evaluation_time) AS eval_date
        FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND wx_actual_high_temp IS NOT NULL
          AND wx_ensemble_mean IS NOT NULL
        GROUP BY asset, DATE(evaluation_time)
    """).fetchall()

    if not rows:
        print("  DATA GAP: wx_actual_high_temp not yet populated (archive API needs ~24h)")
        return {"status": "no_data"}

    # Per-city MAE/RMSE (one observation per city/date)
    city_errors: Dict[str, List[float]] = defaultdict(list)
    for r in rows:
        city = r["asset"]
        error = r["wx_actual_high_temp"] - r["wx_ensemble_mean"]
        city_errors[city].append(error)

    print(f"  {'City':<12} {'N':>4} {'MAE':>7} {'RMSE':>7} {'MeanErr':>8} {'Direction':>10}")
    print(f"  {'-'*55}")

    all_errors = []
    result = {}
    for city, errors in sorted(city_errors.items()):
        mae = sum(abs(e) for e in errors) / len(errors)
        rmse = math.sqrt(sum(e**2 for e in errors) / len(errors))
        mean_err = sum(errors) / len(errors)
        # Direction: positive = forecast too low, negative = forecast too high
        direction = "low" if mean_err > 0.5 else "high" if mean_err < -0.5 else "neutral"
        print(f"  {city:<12} {len(errors):>4} {mae:>7.1f}F {rmse:>7.1f}F {mean_err:>+8.1f}F {direction:>10}")
        all_errors.extend(errors)
        result[city] = {"n": len(errors), "mae": round(mae, 1), "rmse": round(rmse, 1), "bias": round(mean_err, 1)}

    if all_errors:
        overall_mae = sum(abs(e) for e in all_errors) / len(all_errors)
        overall_rmse = math.sqrt(sum(e**2 for e in all_errors) / len(all_errors))
        print(f"\n  Overall:     {len(all_errors):>4} {overall_mae:>7.1f}F {overall_rmse:>7.1f}F")
        result["overall"] = {"n": len(all_errors), "mae": round(overall_mae, 1), "rmse": round(overall_rmse, 1)}

    return result


# ─── Section 8: Leak Analysis ────────────────────────────────────────────────

def section_leaks(conn: sqlite3.Connection, since: Optional[str]) -> dict:
    """Quantify data leaks and pipeline inefficiencies."""
    header("8. LEAK ANALYSIS")
    W = where_clause(since)

    row = conn.execute(f"""
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN market_price <= 5 THEN 1 ELSE 0 END) AS sub_5,
          SUM(CASE WHEN market_price <= 5 AND filter_stage NOT IN ('price_out_of_range','low_probability') THEN 1 ELSE 0 END) AS sub_5_passed,
          SUM(CASE WHEN wx_ensemble_mean IS NULL THEN 1 ELSE 0 END) AS null_ens,
          MAX(wx_n_members) AS max_mem,
          SUM(CASE WHEN filter_stage='weather_observation' THEN 1 ELSE 0 END) AS sigs,
          SUM(CASE WHEN filter_stage='insufficient_edge' AND market_price BETWEEN 15 AND 85 THEN 1 ELSE 0 END) AS ie_near
        FROM evaluated_opportunities WHERE product_type='weather' {W}
    """).fetchone()

    total = row["total"] or 1
    sub5 = row["sub_5"] or 0
    sub5_pass = row["sub_5_passed"] or 0
    null_ens = row["null_ens"] or 0
    max_mem = row["max_mem"] or 0
    sigs = row["sigs"] or 0
    ie_near = row["ie_near"] or 0

    print(f"  Leak 1: Low-Price Noise (<=5c)")
    print(f"    Total <=5c:       {sub5}/{total} ({pct(sub5, total)})")
    print(f"    Past POR filter:  {sub5_pass}")

    print(f"\n  Leak 2: NULL Ensemble")
    print(f"    Missing ensemble: {null_ens}/{total} ({pct(null_ens, total)})")

    print(f"\n  Leak 3: ECMWF Coverage")
    print(f"    Max members:      {max_mem} (expected 82)")
    print(f"    Capacity:         {pct(max_mem, 82)}")

    print(f"\n  Leak 4: Signal Conversion")
    print(f"    Signals:          {sigs}")
    print(f"    IE near-misses:   {ie_near}")

    return {
        "sub5_pct": safe_div(sub5, total) * 100,
        "null_ens_pct": safe_div(null_ens, total) * 100,
        "max_members": max_mem,
        "signal_count": sigs,
        "ie_near_miss": ie_near,
    }


# ─── Section 9: Config Sensitivity ───────────────────────────────────────────

def section_config(conn: sqlite3.Connection, since: Optional[str]) -> dict:
    """Analyze sensitivity to key config parameters."""
    header("9. CONFIG SENSITIVITY")
    W = where_clause(since)

    # MIN_ENTRY_PRICE sensitivity
    print("  Parameter: WEATHER_MIN_ENTRY_PRICE (current = 10)")
    for threshold in [5, 10, 15, 20]:
        row = conn.execute(f"""
            SELECT COUNT(*) AS kept,
              SUM(CASE WHEN filter_stage='weather_observation' THEN 1 ELSE 0 END) AS sigs,
              SUM(CASE WHEN filter_stage='insufficient_edge' THEN 1 ELSE 0 END) AS ie
            FROM evaluated_opportunities
            WHERE product_type='weather' {W} AND market_price >= ?
        """, (threshold,)).fetchone()
        print(f"    If {threshold}c: {row['kept']} evals, {row['ie']} IE, {row['sigs']} sigs")

    # BLEND_W
    print(f"\n  Parameter: WEATHER_MARKET_BLEND_W (current = 0.20)")
    print(f"    0.00: Pure model — maximum signal divergence from market")
    print(f"    0.20: Current — 80% model, 20% market")
    print(f"    0.40: More anchoring — reduces false signals in thin markets")
    print(f"    See Section 6 for simulation results")

    # MIN_EDGE_PCT
    print(f"\n  Parameter: WEATHER_MIN_EDGE_PCT (current = 0.001)")
    for edge_thresh in [0.001, 0.003, 0.005, 0.010]:
        row = conn.execute(f"""
            SELECT COUNT(*) AS n
            FROM evaluated_opportunities
            WHERE product_type='weather' {W}
              AND fee_adjusted_edge >= ?
              AND filter_stage IN ('weather_observation', 'insufficient_edge')
        """, (edge_thresh,)).fetchone()
        print(f"    At {edge_thresh:.3f}: {row['n']} entries pass edge filter")

    # Edge distribution for IE near-misses
    subheader("Insufficient_edge Near-Misses (top 10)")
    rows = conn.execute(f"""
        SELECT ticker, market_price, fee_adjusted_edge, calibrated_prob,
          wx_ensemble_mean, wx_market_type
        FROM evaluated_opportunities
        WHERE product_type='weather' {W} AND filter_stage='insufficient_edge'
        ORDER BY fee_adjusted_edge DESC
        LIMIT 10
    """).fetchall()
    for r in rows:
        ens = f"mean={r['wx_ensemble_mean']:.1f}" if r["wx_ensemble_mean"] else "no_ens"
        mtype = r["wx_market_type"] or "—"
        print(f"    {r['ticker']:<40} p={r['market_price']:>3}c edge={r['fee_adjusted_edge']:>+.4f} type={mtype} {ens}")

    return {}


# ─── Section 10: Pre/Post Regime Comparison ──────────────────────────────────

def section_pre_post(conn: sqlite3.Connection, since: Optional[str]) -> dict:
    """Side-by-side comparison of pre vs post regime."""
    header("10. PRE/POST REGIME COMPARISON")

    if not since:
        print("  No --since provided, skipping pre/post comparison")
        return {}

    def get_stats(where_extra: str) -> dict:
        row = conn.execute(f"""
            SELECT COUNT(*) AS total,
              SUM(CASE WHEN filter_stage='weather_observation' THEN 1 ELSE 0 END) AS sigs,
              AVG(CASE WHEN wx_n_members IS NOT NULL THEN wx_n_members END) AS avg_mem,
              SUM(CASE WHEN wx_ensemble_mean IS NOT NULL THEN 1 ELSE 0 END) AS has_ens,
              SUM(CASE WHEN wx_market_type IS NOT NULL THEN 1 ELSE 0 END) AS has_mtype
            FROM evaluated_opportunities
            WHERE product_type='weather' {where_extra}
        """).fetchone()
        total = row["total"] or 0
        return {
            "total": total,
            "signals": row["sigs"] or 0,
            "avg_members": row["avg_mem"],
            "ensemble_pct": safe_div(row["has_ens"] or 0, total) * 100,
            "mtype_pct": safe_div(row["has_mtype"] or 0, total) * 100,
        }

    pre = get_stats(f"AND evaluation_time < '{since}'")
    post = get_stats(f"AND evaluation_time >= '{since}'")

    print(f"  {'Metric':<25} {'Pre':>12} {'Post':>12}")
    print(f"  {'-'*52}")
    print(f"  {'Total evals':<25} {pre['total']:>12} {post['total']:>12}")
    print(f"  {'Signals':<25} {pre['signals']:>12} {post['signals']:>12}")
    print(f"  {'Ensemble coverage':<25} {pre['ensemble_pct']:>11.1f}% {post['ensemble_pct']:>11.1f}%")
    avg_pre = f"{pre['avg_members']:.0f}" if pre["avg_members"] else "—"
    avg_post = f"{post['avg_members']:.0f}" if post["avg_members"] else "—"
    print(f"  {'Avg members':<25} {avg_pre:>12} {avg_post:>12}")
    print(f"  {'wx_market_type fill':<25} {pre['mtype_pct']:>11.1f}% {post['mtype_pct']:>11.1f}%")

    return {"pre": pre, "post": post}


# ─── Section 11: Bias Correction Tracking ────────────────────────────────────

def section_bias(conn: sqlite3.Connection, since: Optional[str]) -> dict:
    """Track bias correction values by city."""
    header("11. BIAS CORRECTION TRACKING")
    W = where_clause(since)

    rows = conn.execute(f"""
        SELECT asset,
          COUNT(*) AS n,
          AVG(wx_bias_correction) AS avg_bias,
          MIN(wx_bias_correction) AS min_bias,
          MAX(wx_bias_correction) AS max_bias
        FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND wx_bias_correction IS NOT NULL
        GROUP BY asset ORDER BY asset
    """).fetchall()

    if not rows:
        print("  No bias correction data available yet")
        return {"status": "no_data"}

    result = {}
    print(f"  {'City':<12} {'N':>5} {'Mean':>7} {'Min':>7} {'Max':>7} {'Alert':>8}")
    print(f"  {'-'*50}")
    for r in rows:
        alert = "HIGH" if abs(r["avg_bias"] or 0) > 2.0 else ""
        print(f"  {r['asset']:<12} {r['n']:>5} {r['avg_bias']:>+7.2f} {r['min_bias']:>+7.2f} {r['max_bias']:>+7.2f} {alert:>8}")
        result[r["asset"]] = {
            "n": r["n"], "mean": round(r["avg_bias"], 2),
            "min": round(r["min_bias"], 2), "max": round(r["max_bias"], 2),
        }

    # Time trend (by day)
    subheader("Bias by Day")
    day_rows = conn.execute(f"""
        SELECT SUBSTR(evaluation_time, 1, 10) AS day,
          AVG(wx_bias_correction) AS avg_bias,
          COUNT(*) AS n
        FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND wx_bias_correction IS NOT NULL
        GROUP BY day ORDER BY day
    """).fetchall()

    if day_rows:
        for r in day_rows:
            print(f"    {r['day']}  n={r['n']:>4}  bias={r['avg_bias']:>+.2f}F")

    return result


# ─── Section 12: Data Sufficiency ────────────────────────────────────────────

def section_sufficiency(conn: sqlite3.Connection, since: Optional[str], stats: dict, pipeline: dict) -> dict:
    """Evaluate data sufficiency for trading decisions."""
    header("12. DATA SUFFICIENCY AUDIT")
    W = where_clause(since)

    # Settled signal count
    settled_sigs = conn.execute(f"""
        SELECT COUNT(*) AS n FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND filter_stage='weather_observation'
          AND market_result IS NOT NULL
    """).fetchone()["n"] or 0

    # Cities signaling
    sig_cities = conn.execute(f"""
        SELECT COUNT(DISTINCT asset) AS n FROM evaluated_opportunities
        WHERE product_type='weather' {W}
          AND filter_stage='weather_observation'
    """).fetchone()["n"] or 0

    ens_cov = pipeline.get("ensemble_coverage", 0)

    checks = [
        ("Total evaluations >= 500", stats["total"] >= 500, f"{stats['total']}/500"),
        ("Signals >= 100", stats["signals"] >= 100, f"{stats['signals']}/100"),
        ("Settled signals >= 50", settled_sigs >= 50, f"{settled_sigs}/50"),
        ("Days of data >= 14", stats["span_hrs"] / 24 >= 14,
         f"{stats['span_hrs']/24:.1f}/14 days"),
        ("All 5 cities signaling", sig_cities >= 5, f"{sig_cities}/5 cities"),
        ("Ensemble coverage > 90%", ens_cov > 90, f"{ens_cov:.1f}%"),
        ("raw_prob NULL < 20%", pipeline.get("raw_prob_null_pct", 100) < 20,
         f"{pipeline.get('raw_prob_null_pct', 100):.1f}%"),
    ]

    all_pass = True
    for desc, passed, detail in checks:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  [{status}] {desc}: {detail}")

    print()
    if all_pass:
        print("  >>> ALL CHECKS PASSED — ready for promotion evaluation")
    else:
        n_pass = sum(1 for _, p, _ in checks if p)
        print(f"  >>> {n_pass}/{len(checks)} checks passing — NOT READY")

    # Time estimates
    if stats["total"] > 0 and stats["span_hrs"] > 0:
        rate = stats["total"] / stats["span_hrs"]
        if stats["total"] < 500 and rate > 0:
            hrs_to_500 = (500 - stats["total"]) / rate
            print(f"\n  Current eval rate: {rate:.1f}/hr")
            print(f"  Est. time to 500 evals: {hrs_to_500/24:.1f} days")

    return {"checks_passing": sum(1 for _, p, _ in checks if p), "total_checks": len(checks)}


# ─── Section 13: Recommendations ─────────────────────────────────────────────

def section_recommendations(stats: dict, pipeline: dict, settlement: dict) -> None:
    header("13. RECOMMENDATIONS")

    recs = []

    # Dynamic recommendations based on data
    if pipeline.get("ensemble_coverage", 0) < 90:
        recs.append(("R1", "HIGH", "Improve ensemble coverage",
                     f"Currently {pipeline.get('ensemble_coverage', 0):.0f}%. Need >90% for reliable forecasts."))

    if stats["signals"] == 0:
        recs.append(("R2", "HIGH", "Lower WEATHER_MIN_EDGE_PCT further or reduce BLEND_W",
                     "Zero signals prevents evaluation. Consider BLEND_W=0.0 temporarily."))

    if pipeline.get("raw_prob_null_pct", 0) > 20:
        recs.append(("R3", "MEDIUM", "Investigate raw_prob NULL rate",
                     f"{pipeline.get('raw_prob_null_pct', 0):.0f}% NULL — blocks blend simulation."))

    if settlement.get("signal_total", 0) > 10 and settlement.get("signal_pnl", 0) < 0:
        recs.append(("R4", "HIGH", "Investigate signal PnL loss",
                     f"Signals: {settlement['signal_pnl']:+d}¢ on {settlement['signal_total']} trades"))

    recs.append(("R5", "MEDIUM", "Collect 14+ days of cleaned data",
                 "After instrumentation: monitor 2 weeks. Evaluate edge quality, bias correction."))

    recs.append(("R6", "LOW", "Run weekly audit",
                 "python3 scripts/weather_shadow_audit.py --db /tmp/state.db --since <regime_start>"))

    for label, severity, title, detail in recs:
        print(f"  [{label}] [{severity}] {title}")
        print(f"      {detail}")
        print()


def section_cal_engine_obs(conn, since=None):
    """CalEngine observation pipeline: settled evals with raw_prob for weather."""
    header("CALENGINE OBSERVATION PIPELINE")
    W = where_clause(since)
    try:
        row = conn.execute(f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN raw_prob IS NOT NULL THEN 1 ELSE 0 END) AS with_raw_prob,
                   SUM(CASE WHEN status='settled' AND raw_prob IS NOT NULL THEN 1 ELSE 0 END) AS cal_eligible
            FROM evaluated_opportunities
            WHERE product_type='weather' {W}
        """).fetchone()
        total = row["total"] or 0
        with_rp = row["with_raw_prob"] or 0
        eligible = row["cal_eligible"] or 0
        print(f"  Total weather evals:      {total}")
        print(f"  With raw_prob:            {with_rp}")
        print(f"  Settled + raw_prob (cal):  {eligible}")

        # Per-city breakdown
        city_rows = conn.execute(f"""
            SELECT COALESCE(wx_market_type, 'unknown') AS city,
                   SUM(CASE WHEN status='settled' AND raw_prob IS NOT NULL THEN 1 ELSE 0 END) AS cal_eligible
            FROM evaluated_opportunities
            WHERE product_type='weather' {W}
            GROUP BY wx_market_type
            ORDER BY cal_eligible DESC
        """).fetchall()
        if city_rows:
            print()
            for r in city_rows:
                city_name = r["city"] if isinstance(r, sqlite3.Row) else r[0]
                city_eligible = (r["cal_eligible"] if isinstance(r, sqlite3.Row) else r[1]) or 0
                if city_eligible > 0:
                    print(f"    {city_name:<12} {city_eligible} cal obs")

        if eligible > 0:
            print(f"\n  >>> {eligible} observations feeding per-city weather CalEngines")
        else:
            print("\n  >>> No CalEngine observations yet")
        return {"total": total, "with_raw_prob": with_rp, "cal_eligible": eligible}
    except Exception as e:
        print(f"  ERROR: {e}")
        return {"error": str(e)}


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Weather shadow mode audit")
    parser.add_argument("--db", default="state.db", help="Path to state.db")
    parser.add_argument("--since", default="2026-03-02T16:54:00",
                        help="Only analyze data since (default: current regime)")
    parser.add_argument("--json", default=None, help="Output JSON artifact path")
    args = parser.parse_args()

    try:
        conn = connect_db(args.db)
    except Exception as e:
        print(f"ERROR: Cannot open DB at {args.db}: {e}")
        sys.exit(1)

    W = where_clause(args.since)
    row = conn.execute(f"""
        SELECT COUNT(*) AS n FROM evaluated_opportunities
        WHERE product_type='weather' {W}
    """).fetchone()
    if (row["n"] or 0) == 0:
        print(f"ERROR: No weather evaluations found since {args.since}")
        print("Try --since with an earlier date, or check product_type='weather' entries")
        sys.exit(1)

    print(f"Weather Shadow Audit — since {args.since or 'all time'}")
    print(f"DB: {args.db}")

    stats = section_overview(conn, args.since)
    settlement = section_settlement(conn, args.since)
    mtype = section_market_type(conn, args.since)
    pipeline = section_pipeline(conn, args.since)
    no_side = section_no_side_edge(conn, args.since)
    blend = section_blend_sim(conn, args.since)
    forecast = section_forecast_accuracy(conn, args.since)
    leaks = section_leaks(conn, args.since)
    config = section_config(conn, args.since)
    pre_post = section_pre_post(conn, args.since)
    bias = section_bias(conn, args.since)
    sufficiency = section_sufficiency(conn, args.since, stats, pipeline)
    cal_obs = section_cal_engine_obs(conn, args.since)
    section_recommendations(stats, pipeline, settlement)

    conn.close()

    if args.json:
        artifact = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "since": args.since,
            "overview": stats,
            "settlement": settlement,
            "market_type": mtype,
            "pipeline": pipeline,
            "no_side_edge": no_side,
            "blend_simulation": blend,
            "forecast_accuracy": forecast,
            "leaks": leaks,
            "pre_post": pre_post,
            "bias": bias,
            "sufficiency": sufficiency,
            "cal_engine_obs": cal_obs,
        }
        with open(args.json, "w") as f:
            json.dump(artifact, f, indent=2, default=str)
        print(f"\nJSON artifact written to: {args.json}")

    print(f"\n{'=' * 72}")
    print(f"  Audit complete. Next run: 7 days.")
    print(f"{'=' * 72}")


if __name__ == "__main__":
    main()
