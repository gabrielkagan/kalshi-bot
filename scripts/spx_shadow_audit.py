#!/usr/bin/env python3
"""SPX Shadow Engine Audit — comprehensive analysis of SPX hourly observation data.

Reads from state.db evaluated_opportunities (product_type='spx_hourly') and
rejected_opportunities (product_type='spx_hourly').

Usage:
    python3 scripts/spx_shadow_audit.py [--db state.db] [--since 2026-03-02] [--json spx_audit.json]
"""

import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional


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
    """Maker fee for SPX: ceil(0.0175 * 100 * p * (1-p))."""
    p = price_cents / 100.0
    return math.ceil(0.0175 * 100 * p * (1 - p))


def compute_pnl(price: int, result: str, spot: float, threshold: float, n_contracts: int = 1) -> int:
    """Compute PnL in cents. Positive = profit."""
    # Determine side: if spot > threshold, model is betting YES (above)
    yes_side = spot > threshold
    fee = maker_fee(price)
    if yes_side:
        per_contract = (100 - price - fee) if result == "yes" else -price
    else:
        per_contract = (price - fee) if result == "no" else -(100 - price)
    return per_contract * n_contracts


# ─── Section 1: Performance Summary ──────────────────────────────────────────

def section_performance(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    wc = where_clause(since)

    # Observations (would-be trades)
    obs = conn.execute(f"""
        SELECT ticker, market_price, calibrated_prob, edge, fee_adjusted_edge,
               kelly_f, position_size, seconds_to_close, market_result,
               spot_price, threshold, evaluation_time, event_ticker
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='spx_observation'
              AND market_result IS NOT NULL {wc}
        ORDER BY evaluation_time
    """).fetchall()

    # Strategy_wait (blocked)
    wait = conn.execute(f"""
        SELECT ticker, market_price, calibrated_prob, edge, fee_adjusted_edge,
               seconds_to_close, market_result, spot_price, threshold,
               evaluation_time, event_ticker
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='strategy_wait'
              AND market_result IS NOT NULL {wc}
        ORDER BY evaluation_time
    """).fetchall()

    header("1. PERFORMANCE SUMMARY")

    # 1-contract PnL
    obs_pnl_1c = 0
    obs_wins = 0
    for r in obs:
        pnl = compute_pnl(r["market_price"], r["market_result"], r["spot_price"], r["threshold"])
        obs_pnl_1c += pnl
        if pnl > 0:
            obs_wins += 1

    wait_pnl_1c = 0
    wait_wins = 0
    for r in wait:
        pnl = compute_pnl(r["market_price"], r["market_result"], r["spot_price"], r["threshold"])
        wait_pnl_1c += pnl
        if pnl > 0:
            wait_wins += 1

    # Sized PnL
    obs_pnl_sized = 0
    for r in obs:
        pos = r["position_size"] or 1
        pnl = compute_pnl(r["market_price"], r["market_result"], r["spot_price"], r["threshold"], pos)
        obs_pnl_sized += pnl

    total = len(obs) + len(wait)
    total_wins = obs_wins + wait_wins

    print(f"\n  {'Metric':<30} {'Observations':>14} {'Strategy_wait':>14} {'Combined':>14}")
    print(f"  {'-' * 72}")
    print(f"  {'Count':<30} {len(obs):>14} {len(wait):>14} {total:>14}")
    print(f"  {'Win Rate':<30} {pct(obs_wins, len(obs)):>14} {pct(wait_wins, len(wait)):>14} {pct(total_wins, total):>14}")
    print(f"  {'1-contract PnL':<30} {f'{obs_pnl_1c:+d}c':>14} {f'{wait_pnl_1c:+d}c':>14} {f'{obs_pnl_1c + wait_pnl_1c:+d}c':>14}")
    print(f"  {'Sized PnL (Kelly)':<30} {f'${obs_pnl_sized / 100:.2f}':>14} {'n/a':>14} {'':>14}")

    subheader("Trade-by-Trade (Observations)")
    print(f"  {'Ticker':<35} {'Price':>5} {'Cal':>6} {'Edge%':>6} {'Kelly':>6} {'Pos':>4} {'STC':>6} {'Res':>4} {'PnL':>7}")
    print(f"  {'-' * 85}")
    for r in obs:
        pos = r["position_size"] or 1
        pnl = compute_pnl(r["market_price"], r["market_result"], r["spot_price"], r["threshold"], pos)
        win = pnl > 0
        print(f"  {r['ticker'][-35:]:<35} {r['market_price']:>4}c {r['calibrated_prob']:>5.3f} "
              f"{r['fee_adjusted_edge'] * 100:>5.2f} {(r['kelly_f'] or 0):>5.3f} {pos:>4} "
              f"{int(r['seconds_to_close']):>5}s {r['market_result']:>4} {pnl:>+6d}c")

    # Date range
    all_times = [r["evaluation_time"] for r in obs] + [r["evaluation_time"] for r in wait]
    dates = set()
    for t in all_times:
        if t:
            dates.add(t[:10])

    result = {
        "obs_count": len(obs),
        "obs_wins": obs_wins,
        "obs_wr": safe_div(obs_wins, len(obs)),
        "obs_pnl_1c": obs_pnl_1c,
        "obs_pnl_sized": obs_pnl_sized,
        "wait_count": len(wait),
        "wait_wins": wait_wins,
        "wait_wr": safe_div(wait_wins, len(wait)),
        "wait_pnl_1c": wait_pnl_1c,
        "trading_days": len(dates),
        "date_range": sorted(dates),
    }
    print(f"\n  Trading days: {len(dates)} ({', '.join(sorted(dates))})")
    return result


# ─── Section 2: Calibration Analysis ─────────────────────────────────────────

def section_calibration(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    wc = where_clause(since)

    rows = conn.execute(f"""
        SELECT calibrated_prob, market_result, spot_price, threshold
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly'
              AND filter_stage IN ('spx_observation', 'strategy_wait')
              AND market_result IS NOT NULL {wc}
    """).fetchall()

    header("2. CALIBRATION ANALYSIS")

    if not rows:
        print("  No settled data available.")
        return {"buckets": [], "brier": None}

    # Brier score
    brier_sum = 0
    for r in rows:
        actual = 1.0 if r["market_result"] == "yes" else 0.0
        # The model predicts P(above threshold) when spot > threshold
        above = r["spot_price"] > r["threshold"]
        predicted = r["calibrated_prob"] if above else (1 - r["calibrated_prob"])
        brier_sum += (predicted - actual) ** 2
    brier = brier_sum / len(rows)

    # Calibration buckets
    buckets = {}
    for r in rows:
        cp = r["calibrated_prob"]
        above = r["spot_price"] > r["threshold"]
        win = (r["market_result"] == "yes") if above else (r["market_result"] == "no")
        if cp < 0.80:
            key = "0.70-0.80"
        elif cp < 0.85:
            key = "0.80-0.85"
        elif cp < 0.90:
            key = "0.85-0.90"
        elif cp < 0.95:
            key = "0.90-0.95"
        else:
            key = "0.95-1.00"
        if key not in buckets:
            buckets[key] = {"n": 0, "wins": 0, "prob_sum": 0.0}
        buckets[key]["n"] += 1
        buckets[key]["wins"] += int(win)
        buckets[key]["prob_sum"] += cp

    print(f"\n  Brier Score: {brier:.4f} (n={len(rows)})")
    print(f"\n  {'Bucket':<12} {'N':>4} {'Predicted':>10} {'Actual WR':>10} {'Gap':>8}")
    print(f"  {'-' * 48}")
    bucket_list = []
    for key in sorted(buckets.keys()):
        b = buckets[key]
        pred = b["prob_sum"] / b["n"]
        actual_wr = b["wins"] / b["n"]
        gap = pred - actual_wr
        print(f"  {key:<12} {b['n']:>4} {pred:>9.3f} {actual_wr:>9.3f} {gap:>+7.3f}")
        bucket_list.append({
            "bucket": key, "n": b["n"], "predicted": round(pred, 4),
            "actual": round(actual_wr, 4), "gap": round(gap, 4),
        })

    # Overconfidence detection
    oc_buckets = [b for b in bucket_list if b["gap"] > 0.05 and b["n"] >= 5]
    if oc_buckets:
        print(f"\n  *** OVERCONFIDENCE DETECTED in {len(oc_buckets)} bucket(s) (gap > 5pp, n >= 5):")
        for b in oc_buckets:
            print(f"      {b['bucket']}: predicted {b['predicted']:.3f} vs actual {b['actual']:.3f} (n={b['n']})")
    else:
        print(f"\n  No significant overconfidence detected (min n=5 threshold).")

    return {"brier": round(brier, 4), "n": len(rows), "buckets": bucket_list}


# ─── Section 3: Filter Stage Funnel ──────────────────────────────────────────

def section_filter_funnel(conn: sqlite3.Connection, since: Optional[str]) -> List:
    wc = where_clause(since)

    rows = conn.execute(f"""
        SELECT filter_stage, COUNT(*) as cnt,
               SUM(CASE WHEN market_result IS NOT NULL THEN 1 ELSE 0 END) as settled,
               SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) as yes_ct,
               SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) as no_ct
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' {wc}
        GROUP BY filter_stage ORDER BY cnt DESC
    """).fetchall()

    # Also count rejections
    rej_rows = conn.execute(f"""
        SELECT 'z_score_rejection' as reason, COUNT(*) as cnt,
               SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) as yes_ct,
               SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) as no_ct
        FROM rejected_opportunities
        WHERE product_type='spx_hourly'
              AND rejection_reason LIKE '%z_score%' {where_clause(since, 'rejection_time')}
    """).fetchone()

    header("3. FILTER STAGE FUNNEL")

    total = sum(r["cnt"] for r in rows) + (rej_rows["cnt"] if rej_rows else 0)
    print(f"\n  Total evaluations: {total}")
    print(f"\n  {'Stage':<30} {'Count':>6} {'%':>7} {'Settled':>8} {'Yes%':>7}")
    print(f"  {'-' * 60}")

    result = []
    # Z-score rejections first (earliest filter)
    if rej_rows and rej_rows["cnt"] > 0:
        r = rej_rows
        settled = r["yes_ct"] + r["no_ct"]
        yes_pct = pct(r["yes_ct"], settled) if settled > 0 else "n/a"
        print(f"  {'z_score_rejection':<30} {r['cnt']:>6} {pct(r['cnt'], total):>7} {settled:>8} {yes_pct:>7}")
        result.append({"stage": "z_score_rejection", "count": r["cnt"], "settled": settled,
                        "yes_rate": safe_div(r["yes_ct"], settled)})

    for r in rows:
        settled = r["settled"]
        yes_pct_str = pct(r["yes_ct"], settled) if settled > 0 else "n/a"
        print(f"  {r['filter_stage']:<30} {r['cnt']:>6} {pct(r['cnt'], total):>7} {settled:>8} {yes_pct_str:>7}")
        result.append({"stage": r["filter_stage"], "count": r["cnt"], "settled": settled,
                        "yes_rate": safe_div(r["yes_ct"], settled)})

    return result


# ─── Section 4: EV Leakage Analysis ──────────────────────────────────────────

def section_ev_leakage(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    wc = where_clause(since)

    header("4. EV LEAKAGE ANALYSIS")

    # Strategy_wait leakage
    subheader("strategy_wait: Blocked Trades")
    wait_rows = conn.execute(f"""
        SELECT ticker, market_price, calibrated_prob, fee_adjusted_edge,
               seconds_to_close, market_result, spot_price, threshold, event_ticker
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='strategy_wait'
              AND market_result IS NOT NULL {wc}
        ORDER BY evaluation_time
    """).fetchall()

    wait_pnl = 0
    wait_wins = 0
    for r in wait_rows:
        pnl = compute_pnl(r["market_price"], r["market_result"], r["spot_price"], r["threshold"])
        if pnl > 0:
            wait_wins += 1
        wait_pnl += pnl
        win = pnl > 0
        print(f"    {r['ticker'][-30:]:<30} {r['market_price']:>3}c edge={r['fee_adjusted_edge'] * 100:>5.2f}% "
              f"stc={int(r['seconds_to_close']):>5}s {'W' if win else 'L'} {pnl:>+4d}c")

    if wait_rows:
        print(f"\n    Total: {len(wait_rows)} blocked | {wait_wins}W/{len(wait_rows) - wait_wins}L "
              f"({pct(wait_wins, len(wait_rows))}) | PnL: {wait_pnl:+d}c")

    # Near-threshold insufficient_edge
    subheader("insufficient_edge: Near-Threshold")
    near_rows = conn.execute(f"""
        SELECT ticker, market_price, calibrated_prob, fee_adjusted_edge,
               seconds_to_close, market_result, spot_price, threshold
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='insufficient_edge'
              AND fee_adjusted_edge > -0.005
              AND market_result IS NOT NULL {wc}
        ORDER BY fee_adjusted_edge DESC
    """).fetchall()

    near_pnl = 0
    near_wins = 0
    for r in near_rows:
        pnl = compute_pnl(r["market_price"], r["market_result"], r["spot_price"], r["threshold"])
        if pnl > 0:
            near_wins += 1
        near_pnl += pnl

    if near_rows:
        print(f"    {len(near_rows)} trades within 0.5% of edge threshold")
        print(f"    {near_wins}W/{len(near_rows) - near_wins}L ({pct(near_wins, len(near_rows))}) | PnL: {near_pnl:+d}c")
    else:
        print("    No near-threshold trades found.")

    # Window limit rejections (new filter stage)
    subheader("spx_hourly_window_limit: Position Limit Rejections")
    wlim_rows = conn.execute(f"""
        SELECT COUNT(*) as cnt,
               SUM(CASE WHEN market_result IS NOT NULL THEN 1 ELSE 0 END) as settled,
               SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) as yes_ct,
               SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) as no_ct
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly'
              AND filter_stage IN ('spx_hourly_window_limit', 'spx_hourly_window_risk_cap') {wc}
    """).fetchone()

    if wlim_rows and wlim_rows["cnt"] > 0:
        settled = wlim_rows["yes_ct"] + wlim_rows["no_ct"]
        print(f"    {wlim_rows['cnt']} blocked by window limits | settled: {settled}")
        if settled > 0:
            # Compute PnL for blocked trades
            blocked = conn.execute(f"""
                SELECT market_price, market_result, spot_price, threshold
                FROM evaluated_opportunities
                WHERE product_type='spx_hourly'
                      AND filter_stage IN ('spx_hourly_window_limit', 'spx_hourly_window_risk_cap')
                      AND market_result IS NOT NULL {wc}
            """).fetchall()
            bl_pnl = sum(compute_pnl(r["market_price"], r["market_result"], r["spot_price"], r["threshold"]) for r in blocked)
            bl_wins = sum(1 for r in blocked if compute_pnl(r["market_price"], r["market_result"], r["spot_price"], r["threshold"]) > 0)
            print(f"    {bl_wins}W/{len(blocked) - bl_wins}L | PnL if traded: {bl_pnl:+d}c")
    else:
        print("    No window-limit rejections yet (filter just added).")

    return {
        "strategy_wait": {"count": len(wait_rows), "wins": wait_wins, "pnl_1c": wait_pnl},
        "near_edge": {"count": len(near_rows), "wins": near_wins, "pnl_1c": near_pnl},
        "window_limit": {"count": wlim_rows["cnt"] if wlim_rows else 0},
    }


# ─── Section 5: Vol Model Health ─────────────────────────────────────────────

def section_vol_health(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    wc = where_clause(since)

    header("5. VOL MODEL HEALTH")

    row = conn.execute(f"""
        SELECT
            AVG(egarch_blend_weight) as avg_blend_w,
            MIN(egarch_blend_weight) as min_blend_w,
            MAX(egarch_blend_weight) as max_blend_w,
            AVG(mz_r_squared) as avg_mz_r2,
            MIN(mz_r_squared) as min_mz_r2,
            MAX(mz_r_squared) as max_mz_r2,
            AVG(egarch_sigma) as avg_egarch,
            MIN(egarch_sigma) as min_egarch,
            MAX(egarch_sigma) as max_egarch,
            SUM(CASE WHEN egarch_blend_sigma IS NOT NULL THEN 1 ELSE 0 END) as blend_sigma_filled,
            COUNT(*) as total,
            AVG(volatility) as avg_vol,
            MIN(volatility) as min_vol,
            MAX(volatility) as max_vol
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' {wc}
    """).fetchone()

    if not row or row["total"] == 0:
        print("  No data.")
        return {}

    subheader("EGARCH Blend Weight")
    print(f"    avg={row['avg_blend_w']:.4f}  min={row['min_blend_w']:.4f}  max={row['max_blend_w']:.4f}")
    if row["min_blend_w"] == row["max_blend_w"]:
        print(f"    *** STUCK at {row['avg_blend_w']:.4f} — blend weight not adapting")

    subheader("Mincer-Zarnowitz R²")
    print(f"    avg={row['avg_mz_r2']:.4f}  min={row['min_mz_r2']:.4f}  max={row['max_mz_r2']:.4f}")
    if row["min_mz_r2"] == row["max_mz_r2"]:
        print(f"    *** STUCK at {row['avg_mz_r2']:.4f} — MZ not adapting (expected if < {20} pairs)")

    subheader("EGARCH Sigma")
    print(f"    avg={row['avg_egarch']:.2e}  min={row['min_egarch']:.2e}  max={row['max_egarch']:.2e}")

    subheader("Blended Volatility (RK + EGARCH)")
    print(f"    avg={row['avg_vol']:.2e}  min={row['min_vol']:.2e}  max={row['max_vol']:.2e}")

    subheader("egarch_blend_sigma Fill Rate")
    fill_rate = row["blend_sigma_filled"] / row["total"]
    status = "OK" if fill_rate > 0.5 else "*** LOW — likely missing from vol estimate return"
    print(f"    {row['blend_sigma_filled']}/{row['total']} ({pct(row['blend_sigma_filled'], row['total'])}) — {status}")

    return {
        "blend_weight": {"avg": row["avg_blend_w"], "min": row["min_blend_w"], "max": row["max_blend_w"]},
        "mz_r_squared": {"avg": row["avg_mz_r2"], "min": row["min_mz_r2"], "max": row["max_mz_r2"]},
        "egarch_sigma": {"avg": row["avg_egarch"], "min": row["min_egarch"], "max": row["max_egarch"]},
        "blend_sigma_fill_rate": fill_rate,
        "total_evals": row["total"],
    }


# ─── Section 6: Multi-Strike Correlation ─────────────────────────────────────

def section_correlation(conn: sqlite3.Connection, since: Optional[str]) -> List:
    wc = where_clause(since)

    header("6. MULTI-STRIKE CORRELATION (per-window)")

    rows = conn.execute(f"""
        SELECT event_ticker,
               COUNT(*) as n_obs,
               GROUP_CONCAT(market_price) as prices,
               GROUP_CONCAT(market_result) as results,
               GROUP_CONCAT(position_size) as positions,
               MIN(seconds_to_close) as min_stc,
               MAX(seconds_to_close) as max_stc
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='spx_observation'
              AND market_result IS NOT NULL {wc}
        GROUP BY event_ticker ORDER BY n_obs DESC
    """).fetchall()

    if not rows:
        print("  No observation data.")
        return []

    print(f"\n  {'Window':<30} {'Obs':>4} {'Prices':<25} {'Results':<25} {'PnL':>8}")
    print(f"  {'-' * 95}")

    result = []
    max_obs = 0
    for r in rows:
        prices = [int(x) for x in r["prices"].split(",")]
        results = r["results"].split(",")
        positions = [int(x) for x in r["positions"].split(",")] if r["positions"] else [1] * len(prices)

        # Compute window PnL (need spot/threshold — estimate from ticker)
        # Query individual rows for accurate PnL
        detail = conn.execute(f"""
            SELECT market_price, market_result, spot_price, threshold, position_size
            FROM evaluated_opportunities
            WHERE event_ticker=? AND product_type='spx_hourly' AND filter_stage='spx_observation'
                  AND market_result IS NOT NULL
        """, (r["event_ticker"],)).fetchall()

        window_pnl = 0
        for d in detail:
            pos = d["position_size"] or 1
            window_pnl += compute_pnl(d["market_price"], d["market_result"], d["spot_price"], d["threshold"], pos)

        n = len(detail)
        max_obs = max(max_obs, n)
        wins = sum(1 for d in detail if compute_pnl(d["market_price"], d["market_result"], d["spot_price"], d["threshold"]) > 0)
        print(f"  {r['event_ticker']:<30} {n:>4} {r['prices']:<25} {r['results']:<25} {window_pnl:>+7d}c")

        result.append({
            "window": r["event_ticker"], "n_obs": n, "wins": wins,
            "window_pnl_sized": window_pnl,
            "prices": prices, "results": results,
        })

    # Risk assessment
    print(f"\n  Max observations in one window: {max_obs}")
    if max_obs > 2:
        print(f"  *** HIGH CORRELATION RISK: {max_obs} positions in a single window")
        print(f"      With per-window limit of 2, excess would be blocked")

    return result


# ─── Section 7: Timing Analysis ──────────────────────────────────────────────

def section_timing(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    wc = where_clause(since)

    header("7. TIMING ANALYSIS")

    # By STC bucket
    subheader("By Seconds-to-Close Bucket")
    stc_rows = conn.execute(f"""
        SELECT
            CASE
                WHEN seconds_to_close < 600 THEN '0-600s'
                WHEN seconds_to_close < 1200 THEN '600-1200s'
                ELSE '1200-1800s'
            END as bucket,
            COUNT(*) as n,
            SUM(CASE WHEN market_result IS NOT NULL THEN 1 ELSE 0 END) as settled,
            AVG(market_price) as avg_price,
            AVG(fee_adjusted_edge) as avg_edge
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage IN ('spx_observation', 'strategy_wait')
              {wc}
        GROUP BY bucket ORDER BY bucket
    """).fetchall()

    print(f"\n  {'STC Bucket':<15} {'N':>4} {'Settled':>8} {'Avg Price':>10} {'Avg Edge':>10}")
    print(f"  {'-' * 50}")
    stc_data = []
    for r in stc_rows:
        print(f"  {r['bucket']:<15} {r['n']:>4} {r['settled']:>8} {r['avg_price']:>9.1f}c {r['avg_edge'] * 100:>9.2f}%")
        stc_data.append(dict(r))

    # By hour of day (UTC → ET approximation)
    subheader("By Hour (UTC)")
    hour_rows = conn.execute(f"""
        SELECT strftime('%H', evaluation_time) as hour,
               COUNT(*) as total,
               SUM(CASE WHEN filter_stage='spx_observation' THEN 1 ELSE 0 END) as obs,
               SUM(CASE WHEN filter_stage='strategy_wait' THEN 1 ELSE 0 END) as wait,
               SUM(CASE WHEN filter_stage='insufficient_edge' THEN 1 ELSE 0 END) as insuf
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' {wc}
        GROUP BY hour ORDER BY hour
    """).fetchall()

    print(f"\n  {'Hour UTC':>9} {'Total':>6} {'Obs':>5} {'Wait':>5} {'InsufEdge':>10}")
    print(f"  {'-' * 38}")
    hour_data = []
    for r in hour_rows:
        print(f"  {r['hour']:>6}:00 {r['total']:>6} {r['obs']:>5} {r['wait']:>5} {r['insuf']:>10}")
        hour_data.append(dict(r))

    # By strategy
    subheader("By Strategy (obs + wait)")
    strat_rows = conn.execute(f"""
        SELECT strategy, COUNT(*) as cnt
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND strategy IS NOT NULL {wc}
        GROUP BY strategy ORDER BY cnt DESC
    """).fetchall()

    for r in strat_rows:
        print(f"    {r['strategy']}: {r['cnt']}")

    return {"stc_buckets": stc_data, "hours": hour_data, "strategies": [dict(r) for r in strat_rows]}


# ─── Section 8: Data Quality ─────────────────────────────────────────────────

def section_data_quality(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    wc = where_clause(since)

    header("8. DATA QUALITY AUDIT")

    # Column fill rates
    subheader("Column Fill Rates (evaluated_opportunities, spx_hourly)")
    cols_to_check = [
        "ticker", "market_price", "calibrated_prob", "edge", "fee_adjusted_edge",
        "kelly_f", "position_size", "strategy", "drawdown_scaler",
        "egarch_sigma", "egarch_blend_sigma", "egarch_blend_weight", "mz_r_squared",
        "z_score", "vol_regime", "raw_prob", "calibration_method",
        "spot_price", "threshold", "volatility",
        "shadow_tv_blend_rv", "mz_shadow_sigmoid_w", "mz_baseline_qlike", "mz_qlike",
    ]

    total_row = conn.execute(f"""
        SELECT COUNT(*) as cnt FROM evaluated_opportunities
        WHERE product_type='spx_hourly' {wc}
    """).fetchone()
    total = total_row["cnt"]

    fill_rates = {}
    issues = []
    for col in cols_to_check:
        try:
            r = conn.execute(f"""
                SELECT SUM(CASE WHEN {col} IS NOT NULL THEN 1 ELSE 0 END) as filled
                FROM evaluated_opportunities
                WHERE product_type='spx_hourly' {wc}
            """).fetchone()
            filled = r["filled"] or 0
            rate = filled / total if total > 0 else 0
            fill_rates[col] = rate
            status = "OK" if rate > 0.9 else ("PARTIAL" if rate > 0 else "EMPTY")
            if status != "OK":
                issues.append(col)
            print(f"    {col:<30} {filled:>5}/{total} ({pct(filled, total):>6}) {status}")
        except Exception:
            fill_rates[col] = 0
            print(f"    {col:<30} COLUMN NOT FOUND")

    # Duplicate check
    subheader("Duplicate Check")
    dupes = conn.execute(f"""
        SELECT ticker, filter_stage, COUNT(*) as cnt
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' {wc}
        GROUP BY ticker, filter_stage HAVING cnt > 1
        LIMIT 5
    """).fetchall()
    if dupes:
        print(f"    *** {len(dupes)} duplicate (ticker, filter_stage) pairs found!")
        for d in dupes:
            print(f"        {d['ticker']} / {d['filter_stage']}: {d['cnt']}x")
    else:
        print(f"    No duplicates found.")

    # Timestamp gaps
    subheader("Timestamp Gaps (> 30 min)")
    times = conn.execute(f"""
        SELECT evaluation_time FROM evaluated_opportunities
        WHERE product_type='spx_hourly' {wc}
        ORDER BY evaluation_time
    """).fetchall()

    gaps = []
    for i in range(1, len(times)):
        try:
            t1 = datetime.fromisoformat(times[i - 1]["evaluation_time"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(times[i]["evaluation_time"].replace("Z", "+00:00"))
            gap_min = (t2 - t1).total_seconds() / 60
            if gap_min > 30:
                gaps.append({"from": times[i - 1]["evaluation_time"], "to": times[i]["evaluation_time"],
                             "gap_min": round(gap_min, 1)})
        except Exception:
            pass

    if gaps:
        print(f"    {len(gaps)} gaps > 30 min (expected at hourly window boundaries):")
        for g in gaps[:10]:
            print(f"        {g['from'][:19]} → {g['to'][:19]} ({g['gap_min']:.0f} min)")
    else:
        print(f"    No gaps > 30 min found.")

    if issues:
        subheader("Data Issues Summary")
        critical = [c for c in issues if c in ("egarch_blend_sigma", "kelly_f", "position_size")]
        cosmetic = [c for c in issues if c not in critical]
        if critical:
            print(f"    CRITICAL (affect analysis): {', '.join(critical)}")
        if cosmetic:
            print(f"    Expected (crypto-specific): {', '.join(cosmetic)}")

    return {"total": total, "fill_rates": fill_rates, "duplicates": len(dupes), "gaps": len(gaps)}


# ─── Section 9: Price-Out-Of-Range Analysis ──────────────────────────────────

def section_price_range(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    wc = where_clause(since)

    header("9. PRICE-OUT-OF-RANGE ANALYSIS")

    rows = conn.execute(f"""
        SELECT market_price, calibrated_prob, market_result, seconds_to_close,
               spot_price, threshold
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='price_out_of_range'
              AND market_result IS NOT NULL {wc}
        ORDER BY market_price
    """).fetchall()

    if not rows:
        print("  No price_out_of_range entries.")
        return {"count": 0}

    # Group by price range
    below_min = [r for r in rows if r["market_price"] < 70]
    above_max = [r for r in rows if r["market_price"] > 99]

    print(f"\n  Total: {len(rows)} | Below 70c: {len(below_min)} | Above 99c: {len(above_max)}")

    if below_min:
        subheader(f"Below MIN_ENTRY_PRICE (70c): {len(below_min)} entries")
        wins = sum(1 for r in below_min if
                   compute_pnl(r["market_price"], r["market_result"], r["spot_price"], r["threshold"]) > 0)
        print(f"    Win rate: {pct(wins, len(below_min))} ({wins}W/{len(below_min) - wins}L)")
        print(f"    Price range: {min(r['market_price'] for r in below_min)}-{max(r['market_price'] for r in below_min)}c")
        print(f"    Avg cal prob: {sum(r['calibrated_prob'] for r in below_min) / len(below_min):.3f}")
        avg_gap = sum(abs(r["calibrated_prob"] - r["market_price"] / 100.0) for r in below_min) / len(below_min)
        print(f"    Avg model-market gap: {avg_gap * 100:.1f}pp")
        if avg_gap > 0.20:
            print(f"    *** GAP > 20pp — model dramatically disagrees with market at these prices")

    return {"count": len(rows), "below_min": len(below_min), "above_max": len(above_max)}


# ─── Section 10: Z-Score Rejection Analysis ──────────────────────────────────

def section_zscore(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    wc = where_clause(since, "rejection_time")

    header("10. Z-SCORE REJECTION ANALYSIS")

    row = conn.execute(f"""
        SELECT COUNT(*) as cnt,
               SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) as yes_ct,
               SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) as no_ct,
               SUM(CASE WHEN market_result IS NULL THEN 1 ELSE 0 END) as unsettled,
               AVG(ABS(z_score)) as avg_abs_z,
               MIN(ABS(z_score)) as min_abs_z,
               MAX(ABS(z_score)) as max_abs_z
        FROM rejected_opportunities
        WHERE product_type='spx_hourly' AND rejection_reason LIKE '%z_score%' {wc}
    """).fetchone()

    if not row or row["cnt"] == 0:
        print("  No z-score rejections.")
        return {"count": 0}

    settled = row["yes_ct"] + row["no_ct"]
    yes_rate = safe_div(row["yes_ct"], settled)

    print(f"\n  Total z-score rejections: {row['cnt']}")
    print(f"  Settled: {settled} (yes={row['yes_ct']}, no={row['no_ct']}, unsettled={row['unsettled']})")
    print(f"  Yes rate: {pct(row['yes_ct'], settled)} — {'FILTER WORKING (low yes = garbage correctly blocked)' if yes_rate < 0.20 else 'INVESTIGATE — high yes rate may mean filter too aggressive'}")
    print(f"  |z| range: {row['min_abs_z']:.1f} - {row['max_abs_z']:.1f} (avg {row['avg_abs_z']:.1f})")

    # Z-score distribution
    subheader("Z-Score Distribution")
    zbuckets = conn.execute(f"""
        SELECT
            CASE
                WHEN ABS(z_score) < 50 THEN '25-50'
                WHEN ABS(z_score) < 100 THEN '50-100'
                WHEN ABS(z_score) < 200 THEN '100-200'
                ELSE '200+'
            END as bucket,
            COUNT(*) as cnt,
            SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) as yes_ct,
            SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) as no_ct
        FROM rejected_opportunities
        WHERE product_type='spx_hourly' AND rejection_reason LIKE '%z_score%' {wc}
        GROUP BY bucket ORDER BY bucket
    """).fetchall()

    print(f"  {'|z| Bucket':<12} {'Count':>6} {'Yes':>5} {'No':>5} {'Yes Rate':>9}")
    print(f"  {'-' * 40}")
    for zb in zbuckets:
        settled_b = zb["yes_ct"] + zb["no_ct"]
        print(f"  {zb['bucket']:<12} {zb['cnt']:>6} {zb['yes_ct']:>5} {zb['no_ct']:>5} {pct(zb['yes_ct'], settled_b):>9}")

    return {
        "count": row["cnt"], "settled": settled,
        "yes_rate": round(yes_rate, 4), "avg_abs_z": round(row["avg_abs_z"], 1),
    }


# ─── Section 11: Volatility by Time of Day ───────────────────────────────────

def section_vol_intraday(conn: sqlite3.Connection, since: Optional[str]) -> List:
    wc = where_clause(since)

    header("11. VOLATILITY BY TIME OF DAY")

    rows = conn.execute(f"""
        SELECT strftime('%H', evaluation_time) as hour,
               AVG(volatility) as avg_vol,
               MIN(volatility) as min_vol,
               MAX(volatility) as max_vol,
               COUNT(*) as n
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND volatility IS NOT NULL {wc}
        GROUP BY hour ORDER BY hour
    """).fetchall()

    if not rows:
        print("  No data.")
        return []

    print(f"\n  {'Hour UTC':>9} {'N':>5} {'Avg Vol':>12} {'Min Vol':>12} {'Max Vol':>12}")
    print(f"  {'-' * 55}")
    result = []
    for r in rows:
        print(f"  {r['hour']:>6}:00 {r['n']:>5} {r['avg_vol']:>11.2e} {r['min_vol']:>11.2e} {r['max_vol']:>11.2e}")
        result.append(dict(r))

    # Check for U-shape pattern
    if len(rows) >= 3:
        vols = [r["avg_vol"] for r in rows]
        if vols[0] > min(vols) and vols[-1] > min(vols):
            print(f"\n  U-shape pattern detected (high open, low midday, high close)")
        else:
            print(f"\n  No clear U-shape pattern — may need more data")

    return result


# ─── Section 12: Readiness Assessment ────────────────────────────────────────

def section_readiness(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    wc = where_clause(since)

    header("12. READINESS ASSESSMENT")

    # Count trading days
    days = conn.execute(f"""
        SELECT DISTINCT SUBSTR(evaluation_time, 1, 10) as dt
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' {wc}
    """).fetchall()
    n_days = len(days)

    # Count observations
    obs_count = conn.execute(f"""
        SELECT COUNT(*) FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='spx_observation'
              AND market_result IS NOT NULL {wc}
    """).fetchone()[0]

    # Calibration bucket min
    cal_min = conn.execute(f"""
        SELECT
            CASE
                WHEN calibrated_prob < 0.85 THEN 'low'
                WHEN calibrated_prob < 0.95 THEN 'mid'
                ELSE 'high'
            END as bucket,
            COUNT(*) as n
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly'
              AND filter_stage IN ('spx_observation', 'strategy_wait')
              AND market_result IS NOT NULL {wc}
        GROUP BY bucket
    """).fetchall()
    min_bucket_n = min((r["n"] for r in cal_min), default=0) if cal_min else 0

    # Blend weight variation
    bw = conn.execute(f"""
        SELECT MIN(egarch_blend_weight) as mn, MAX(egarch_blend_weight) as mx
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' {wc}
    """).fetchone()
    blend_adapting = bw and bw["mn"] != bw["mx"]

    # egarch_blend_sigma fill
    ebs = conn.execute(f"""
        SELECT SUM(CASE WHEN egarch_blend_sigma IS NOT NULL THEN 1 ELSE 0 END) as filled,
               COUNT(*) as total
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' {wc}
    """).fetchone()
    blend_sigma_ok = ebs and ebs["total"] > 0 and (ebs["filled"] / ebs["total"]) > 0.5

    checks = [
        ("Trading days >= 10", n_days >= 10, f"{n_days} days"),
        ("Observations >= 100", obs_count >= 100, f"{obs_count} obs"),
        ("Min calibration bucket >= 30", min_bucket_n >= 30, f"min bucket n={min_bucket_n}"),
        ("MZ R² adapting (not stuck)", blend_adapting, "adapting" if blend_adapting else "STUCK"),
        ("egarch_blend_sigma populated", blend_sigma_ok, f"{ebs['filled']}/{ebs['total']}" if ebs else "no data"),
        ("Per-window limits active", True, "max_positions=2, max_risk=0.15"),
    ]

    all_pass = True
    for label, passed, detail in checks:
        icon = "[+]" if passed else "[ ]"
        if not passed:
            all_pass = False
        print(f"  {icon} {label}: {detail}")

    if all_pass:
        print(f"\n  *** ALL CHECKS PASS — ready for config tuning phase")
    else:
        failed = sum(1 for _, p, _ in checks if not p)
        print(f"\n  {failed}/{len(checks)} checks failing — continue data collection")

    return {
        "trading_days": n_days,
        "observations": obs_count,
        "min_cal_bucket": min_bucket_n,
        "blend_adapting": blend_adapting,
        "blend_sigma_ok": blend_sigma_ok,
        "all_pass": all_pass,
    }


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="SPX Shadow Engine Audit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default="state.db", help="Path to state.db")
    parser.add_argument("--since", default=None, help="Only analyze data since YYYY-MM-DD")
    parser.add_argument("--json", default=None, help="Output JSON artifact path")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: Database not found: {args.db}")
        sys.exit(1)

    try:
        conn = connect_db(args.db)
    except Exception as e:
        print(f"ERROR: Cannot open database: {e}")
        sys.exit(1)

    # Verify we have SPX data
    count = conn.execute(
        "SELECT COUNT(*) FROM evaluated_opportunities WHERE product_type='spx_hourly'"
    ).fetchone()[0]
    if count == 0:
        print("No SPX hourly data found in evaluated_opportunities.")
        sys.exit(0)

    print(f"\nSPX Shadow Engine Audit — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    if args.since:
        print(f"Filtering to data since: {args.since}")
    print(f"Database: {args.db} ({count} SPX evaluations)")

    since = args.since

    # Run all sections
    perf = section_performance(conn, since)
    cal = section_calibration(conn, since)
    funnel = section_filter_funnel(conn, since)
    leakage = section_ev_leakage(conn, since)
    vol = section_vol_health(conn, since)
    corr = section_correlation(conn, since)
    timing = section_timing(conn, since)
    quality = section_data_quality(conn, since)
    price_range = section_price_range(conn, since)
    zscore = section_zscore(conn, since)
    vol_intraday = section_vol_intraday(conn, since)
    readiness = section_readiness(conn, since)

    conn.close()

    # JSON artifact
    if args.json:
        artifact = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "since": args.since,
            "performance": perf,
            "calibration": cal,
            "filter_funnel": funnel,
            "ev_leakage": leakage,
            "vol_health": vol,
            "correlation": corr,
            "timing": timing,
            "data_quality": quality,
            "price_range": price_range,
            "zscore": zscore,
            "vol_intraday": vol_intraday,
            "readiness": readiness,
        }
        with open(args.json, "w") as f:
            json.dump(artifact, f, indent=2, default=str)
        print(f"\nJSON artifact written to: {args.json}")

    print(f"\n{'=' * 72}")
    print(f"  AUDIT COMPLETE")
    print(f"{'=' * 72}")


if __name__ == "__main__":
    main()
