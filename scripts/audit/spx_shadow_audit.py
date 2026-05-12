#!/usr/bin/env python3
"""SPX Shadow Engine Audit — comprehensive analysis of SPX hourly observation data.

Reads from state.db evaluated_opportunities (product_type='spx_hourly') and
rejected_opportunities (product_type='spx_hourly').

Usage:
    python3 scripts/audit/spx_shadow_audit.py [--db state.db] [--since 2026-03-02] [--json spx_audit.json]
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
    """Kalshi charges $0 on maker fills."""
    return 0


def taker_fee(price_cents: int) -> int:
    """Taker fee for SPX: ceil(0.035 * 100 * p * (1-p))."""
    p = price_cents / 100.0
    return math.ceil(0.035 * 100 * p * (1 - p))


def breakeven_wr(price_cents: int) -> float:
    """Breakeven win rate at given price (taker fee)."""
    fee = taker_fee(price_cents)
    return (price_cents + fee) / 100.0


def compute_pnl(price: int, result: str, n_contracts: int = 1) -> int:
    """Compute PnL in cents. Win: (100 - price - fee) * contracts.
    Loss: -(price + fee) * contracts."""
    fee = taker_fee(price)
    if result == "yes":
        per_contract = 100 - price - fee
    else:
        per_contract = -(price + fee)
    return per_contract * n_contracts


def significance_tag(n: int) -> str:
    """Return significance warning based on sample size."""
    if n < 10:
        return " *** VERY SMALL SAMPLE"
    if n < 20:
        return " ** NOT SIGNIFICANT"
    if n < 30:
        return " * SMALL SAMPLE"
    return ""


# ─── Section 1: Performance Summary ──────────────────────────────────────────

def section_performance(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    wc = where_clause(since)

    obs = conn.execute(f"""
        SELECT ticker, market_price, calibrated_prob, edge, fee_adjusted_edge,
               kelly_f, position_size, seconds_to_close, market_result,
               spot_price, threshold, evaluation_time, event_ticker
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='spx_observation'
              AND market_result IS NOT NULL {wc}
        ORDER BY evaluation_time
    """).fetchall()

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

    obs_pnl_1c = 0
    obs_pnl_sized = 0
    obs_wins = 0
    for r in obs:
        pnl_1 = compute_pnl(r["market_price"], r["market_result"])
        pos = min(r["position_size"] or 1, 50)  # Cap at 50: SPX liquidity constraint
        pnl_s = compute_pnl(r["market_price"], r["market_result"], pos)
        obs_pnl_1c += pnl_1
        obs_pnl_sized += pnl_s
        if pnl_1 > 0:
            obs_wins += 1

    wait_pnl_1c = 0
    wait_wins = 0
    for r in wait:
        pnl = compute_pnl(r["market_price"], r["market_result"])
        wait_pnl_1c += pnl
        if pnl > 0:
            wait_wins += 1

    total = len(obs) + len(wait)
    total_wins = obs_wins + wait_wins

    print(f"\n  {'Metric':<30} {'Observations':>14} {'Strategy_wait':>14} {'Combined':>14}")
    print(f"  {'-' * 72}")
    print(f"  {'Count':<30} {len(obs):>14} {len(wait):>14} {total:>14}")
    print(f"  {'Win Rate':<30} {pct(obs_wins, len(obs)):>14} {pct(wait_wins, len(wait)):>14} {pct(total_wins, total):>14}")
    print(f"  {'1-contract PnL':<30} {f'{obs_pnl_1c:+d}c':>14} {f'{wait_pnl_1c:+d}c':>14} {f'{obs_pnl_1c + wait_pnl_1c:+d}c':>14}")
    print(f"  {'Sized PnL (Kelly)':<30} {f'${obs_pnl_sized / 100:.2f}':>14} {'n/a':>14} {'':>14}")

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


# ─── Section 1a: Per-Price Bucket Performance ─────────────────────────────────

def section_price_buckets(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    wc = where_clause(since)

    header("1a. PER-PRICE BUCKET PERFORMANCE")

    rows = conn.execute(f"""
        SELECT market_price, calibrated_prob, market_result, seconds_to_close,
               position_size, kelly_f, fee_adjusted_edge, edge
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='spx_observation'
              AND market_result IS NOT NULL {wc}
        ORDER BY market_price
    """).fetchall()

    if not rows:
        print("  No data.")
        return {}

    BUCKETS = [
        ("1-9c", 1, 9),
        ("10-19c", 10, 19),
        ("20-29c", 20, 29),
        ("30-49c", 30, 49),
        ("50-69c", 50, 69),
        ("70-79c", 70, 79),
        ("80-84c", 80, 84),
        ("85-89c", 85, 89),
        ("90-94c", 90, 94),
        ("95-99c", 95, 99),
    ]

    bucket_data = {}
    for label, lo, hi in BUCKETS:
        filtered = [r for r in rows if lo <= r["market_price"] <= hi]
        if not filtered:
            bucket_data[label] = None
            continue
        wins = sum(1 for r in filtered if r["market_result"] == "yes")
        losses = len(filtered) - wins
        pnl_1c = sum(compute_pnl(r["market_price"], r["market_result"]) for r in filtered)
        pnl_sz = sum(compute_pnl(r["market_price"], r["market_result"], min(r["position_size"] or 1, 50)) for r in filtered)
        avg_price = sum(r["market_price"] for r in filtered) / len(filtered)
        avg_edge = sum((r["fee_adjusted_edge"] or 0) for r in filtered) / len(filtered)
        avg_pos = sum(min(r["position_size"] or 1, 50) for r in filtered) / len(filtered)
        avg_stc = sum((r["seconds_to_close"] or 0) for r in filtered) / len(filtered)
        be_wr = breakeven_wr(int(avg_price))
        actual_wr = wins / len(filtered)

        bucket_data[label] = {
            "n": len(filtered), "wins": wins, "losses": losses,
            "wr": actual_wr, "be_wr": be_wr,
            "pnl_1c": pnl_1c, "pnl_sized": pnl_sz,
            "avg_edge": avg_edge, "avg_pos": avg_pos, "avg_stc": avg_stc,
        }

    print(f"\n  {'Bucket':>8s} {'N':>4s} {'W':>4s} {'L':>3s} {'WR':>6s} {'BE_WR':>6s} {'Gap':>6s} "
          f"{'1c_PnL':>8s} {'Sz_PnL':>10s} {'AvgEdge':>8s} {'AvgPos':>7s} {'Verdict':>10s}")
    print(f"  {'-' * 98}")

    for label, lo, hi in BUCKETS:
        b = bucket_data[label]
        if b is None:
            print(f"  {label:>8s}  {'--- no data ---':>50s}")
            continue
        gap = b["wr"] - b["be_wr"]
        if gap < -0.05 and b["n"] >= 5:
            verdict = "LOSING"
        elif gap < 0 and b["n"] >= 5:
            verdict = "MARGINAL"
        elif b["n"] < 10:
            verdict = "LOW N"
        else:
            verdict = "OK"
        sig = significance_tag(b["n"])
        print(f"  {label:>8s} {b['n']:>4d} {b['wins']:>4d} {b['losses']:>3d} {b['wr']:>5.1%} {b['be_wr']:>5.1%} {gap:>+5.1%} "
              f"{b['pnl_1c']:>+7d}c {b['pnl_sized'] / 100:>+9.2f}$ {b['avg_edge']:>7.3%} {b['avg_pos']:>6.1f} {verdict:>10s}{sig}")

    # Highlight key finding
    losing = [(label, b) for label, b in bucket_data.items() if b and b["wr"] < b["be_wr"] and b["n"] >= 5]
    if losing:
        print(f"\n  *** BELOW-BREAKEVEN BUCKETS:")
        for label, b in losing:
            gap = b["wr"] - b["be_wr"]
            print(f"      {label}: WR {b['wr']:.1%} vs BE {b['be_wr']:.1%} ({gap:+.1%}pp), sized PnL ${b['pnl_sized']/100:+.2f}")

    return bucket_data


# ─── Section 1b: Daily P&L Timeline ──────────────────────────────────────────

def section_daily_pnl(conn: sqlite3.Connection, since: Optional[str]) -> List:
    wc = where_clause(since)

    header("1b. DAILY P&L TIMELINE")

    rows = conn.execute(f"""
        SELECT DATE(evaluation_time) as dt, market_price, market_result, position_size
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='spx_observation'
              AND market_result IS NOT NULL {wc}
        ORDER BY evaluation_time
    """).fetchall()

    if not rows:
        print("  No data.")
        return []

    daily = {}
    for r in rows:
        dt = r["dt"]
        if dt not in daily:
            daily[dt] = {"w": 0, "l": 0, "pnl_1c": 0, "pnl_sz": 0, "n": 0}
        d = daily[dt]
        d["n"] += 1
        is_win = r["market_result"] == "yes"
        if is_win:
            d["w"] += 1
        else:
            d["l"] += 1
        d["pnl_1c"] += compute_pnl(r["market_price"], r["market_result"])
        d["pnl_sz"] += compute_pnl(r["market_price"], r["market_result"], min(r["position_size"] or 1, 50))  # Cap at 50: SPX liquidity constraint

    print(f"\n  {'Date':<12s} {'N':>4s} {'W':>3s} {'L':>3s} {'WR':>6s} {'1c PnL':>8s} {'Sized PnL':>10s} {'Cum 1c':>8s} {'Cum Sized':>10s}")
    print(f"  {'-' * 72}")

    cum_1c = 0
    cum_sz = 0
    result = []
    for dt in sorted(daily.keys()):
        d = daily[dt]
        wr = d["w"] / d["n"]
        cum_1c += d["pnl_1c"]
        cum_sz += d["pnl_sz"]
        print(f"  {dt:<12s} {d['n']:>4d} {d['w']:>3d} {d['l']:>3d} {wr:>5.0%} {d['pnl_1c']:>+7d}c {d['pnl_sz']/100:>+9.2f}$ {cum_1c:>+7d}c {cum_sz/100:>+9.2f}$")
        result.append({"date": dt, **d, "cum_1c": cum_1c, "cum_sz": cum_sz})

    # Trend
    if len(result) >= 3:
        first_half = result[:len(result)//2]
        second_half = result[len(result)//2:]
        fh_wr = sum(d["w"] for d in first_half) / max(sum(d["n"] for d in first_half), 1)
        sh_wr = sum(d["w"] for d in second_half) / max(sum(d["n"] for d in second_half), 1)
        if sh_wr > fh_wr + 0.05:
            print(f"\n  Trend: IMPROVING (first half {fh_wr:.0%} → second half {sh_wr:.0%})")
        elif sh_wr < fh_wr - 0.05:
            print(f"\n  Trend: DETERIORATING (first half {fh_wr:.0%} → second half {sh_wr:.0%})")
        else:
            print(f"\n  Trend: STABLE (first half {fh_wr:.0%} → second half {sh_wr:.0%})")

    return result


# ─── Section 1c: Loss Deep Dive ──────────────────────────────────────────────

def section_loss_analysis(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    wc = where_clause(since)

    header("1c. LOSS DEEP DIVE")

    losses = conn.execute(f"""
        SELECT ticker, market_price, calibrated_prob, fee_adjusted_edge, edge,
               seconds_to_close, position_size, kelly_f, spot_price, threshold,
               evaluation_time, egarch_blend_weight, mz_r_squared, vol_regime,
               event_ticker
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='spx_observation'
              AND market_result='no' {wc}
        ORDER BY evaluation_time
    """).fetchall()

    wins = conn.execute(f"""
        SELECT market_price, seconds_to_close, fee_adjusted_edge, position_size
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='spx_observation'
              AND market_result='yes' {wc}
    """).fetchall()

    if not losses:
        print("  No losses.")
        return {}

    n_loss = len(losses)
    n_win = len(wins)

    # Aggregate loss stats
    avg_price_l = sum(r["market_price"] for r in losses) / n_loss
    avg_stc_l = sum(r["seconds_to_close"] for r in losses) / n_loss
    avg_edge_l = sum((r["fee_adjusted_edge"] or 0) for r in losses) / n_loss
    avg_pos_l = sum(min(r["position_size"] or 1, 50) for r in losses) / n_loss
    avg_kelly_l = sum((r["kelly_f"] or 0) for r in losses) / n_loss

    avg_price_w = sum(r["market_price"] for r in wins) / n_win if n_win else 0
    avg_stc_w = sum(r["seconds_to_close"] for r in wins) / n_win if n_win else 0
    avg_edge_w = sum((r["fee_adjusted_edge"] or 0) for r in wins) / n_win if n_win else 0
    avg_pos_w = sum(min(r["position_size"] or 1, 50) for r in wins) / n_win if n_win else 0

    print(f"\n  Total losses: {n_loss} out of {n_loss + n_win} ({pct(n_loss, n_loss + n_win)} loss rate)")
    print(f"\n  {'Metric':<25s} {'Losses':>12s} {'Wins':>12s} {'Delta':>12s}")
    print(f"  {'-' * 63}")
    print(f"  {'Avg price':<25s} {avg_price_l:>11.1f}c {avg_price_w:>11.1f}c {avg_price_l - avg_price_w:>+11.1f}c")
    print(f"  {'Avg STC':<25s} {avg_stc_l:>10.0f}s {avg_stc_w:>10.0f}s {avg_stc_l - avg_stc_w:>+10.0f}s")
    print(f"  {'Avg fee-adj edge':<25s} {avg_edge_l:>11.3%} {avg_edge_w:>11.3%} {avg_edge_l - avg_edge_w:>+11.3%}")
    print(f"  {'Avg position size':<25s} {avg_pos_l:>11.1f} {avg_pos_w:>11.1f} {avg_pos_l - avg_pos_w:>+11.1f}")
    print(f"  {'Avg Kelly fraction':<25s} {avg_kelly_l:>11.4f} {'':>12s} {'':>12s}")

    # Top 10 worst losses
    subheader("Top 10 Worst Losses (by sized PnL)")
    loss_list = []
    for r in losses:
        pos = min(r["position_size"] or 1, 50)  # Cap at 50: SPX liquidity constraint
        pnl = compute_pnl(r["market_price"], "no", pos)
        loss_list.append((pnl, r))
    loss_list.sort(key=lambda x: x[0])

    print(f"  {'Sized PnL':>10s} {'Price':>5s} {'Pos':>4s} {'Edge':>7s} {'STC':>6s} {'Kelly':>6s} {'Window':>20s}")
    print(f"  {'-' * 65}")
    for pnl, r in loss_list[:10]:
        pos = min(r["position_size"] or 1, 50)  # Cap at 50: SPX liquidity constraint
        print(f"  ${pnl / 100:>+8.2f} {r['market_price']:>4d}c {pos:>4d} {(r['fee_adjusted_edge'] or 0):>6.2%} "
              f"{r['seconds_to_close']:>5.0f}s {(r['kelly_f'] or 0):>5.3f} {r['event_ticker'][-20:]}")

    # Loss clustering by window
    subheader("Loss Clustering by Window")
    window_losses = {}
    for r in losses:
        et = r["event_ticker"]
        if et not in window_losses:
            window_losses[et] = 0
        window_losses[et] += 1

    multi_loss = {k: v for k, v in window_losses.items() if v > 1}
    if multi_loss:
        for et, cnt in sorted(multi_loss.items(), key=lambda x: -x[1]):
            print(f"    {et}: {cnt} losses in same window")
        print(f"\n    {len(multi_loss)}/{len(window_losses)} windows had multiple losses — correlated blowup risk")
    else:
        print(f"    All losses in separate windows — no clustering detected")

    return {
        "n_losses": n_loss,
        "avg_price_loss": avg_price_l,
        "avg_stc_loss": avg_stc_l,
        "worst_losses": [(pnl, dict(r)) for pnl, r in loss_list[:5]],
    }


# ─── Section 2: Calibration Analysis ─────────────────────────────────────────

def section_calibration(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    wc = where_clause(since)

    rows = conn.execute(f"""
        SELECT calibrated_prob, market_result, market_price
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
    brier_sum = sum((r["calibrated_prob"] - (1.0 if r["market_result"] == "yes" else 0.0)) ** 2 for r in rows)
    brier = brier_sum / len(rows)

    # Calibration by predicted probability bucket
    subheader("By Predicted Probability")
    prob_buckets = {}
    for r in rows:
        cp = r["calibrated_prob"]
        win = r["market_result"] == "yes"
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
        if key not in prob_buckets:
            prob_buckets[key] = {"n": 0, "wins": 0, "prob_sum": 0.0}
        prob_buckets[key]["n"] += 1
        prob_buckets[key]["wins"] += int(win)
        prob_buckets[key]["prob_sum"] += cp

    print(f"\n  Brier Score: {brier:.4f} (n={len(rows)})")
    print(f"\n  {'Bucket':<12} {'N':>4} {'Predicted':>10} {'Actual WR':>10} {'Gap':>8} {'Verdict':>12}")
    print(f"  {'-' * 60}")
    bucket_list = []
    for key in sorted(prob_buckets.keys()):
        b = prob_buckets[key]
        pred = b["prob_sum"] / b["n"]
        actual_wr = b["wins"] / b["n"]
        gap = pred - actual_wr
        verdict = ""
        if gap > 0.10 and b["n"] >= 5:
            verdict = "OVERCONFIDENT"
        elif gap > 0.05 and b["n"] >= 5:
            verdict = "WARM"
        elif gap < -0.05 and b["n"] >= 5:
            verdict = "UNDERCONFIDENT"
        print(f"  {key:<12} {b['n']:>4} {pred:>9.3f} {actual_wr:>9.3f} {gap:>+7.3f} {verdict:>12}{significance_tag(b['n'])}")
        bucket_list.append({
            "bucket": key, "n": b["n"], "predicted": round(pred, 4),
            "actual": round(actual_wr, 4), "gap": round(gap, 4),
        })

    # Calibration by market price (critical for SPX!)
    subheader("By Market Price (Actual WR vs Breakeven WR)")
    price_buckets = {}
    for r in rows:
        p = r["market_price"]
        if p < 10:
            key = "1-9c"
        elif p < 20:
            key = "10-19c"
        elif p < 30:
            key = "20-29c"
        elif p < 50:
            key = "30-49c"
        elif p < 70:
            key = "50-69c"
        elif p < 80:
            key = "70-79c"
        elif p < 85:
            key = "80-84c"
        elif p < 90:
            key = "85-89c"
        elif p < 95:
            key = "90-94c"
        else:
            key = "95-99c"
        if key not in price_buckets:
            price_buckets[key] = {"n": 0, "wins": 0, "price_sum": 0}
        price_buckets[key]["n"] += 1
        price_buckets[key]["wins"] += int(r["market_result"] == "yes")
        price_buckets[key]["price_sum"] += p

    print(f"\n  {'Price Bucket':<12} {'N':>4} {'Actual WR':>10} {'BE WR':>8} {'Margin':>8} {'Verdict':>12}")
    print(f"  {'-' * 58}")
    for key in sorted(price_buckets.keys()):
        b = price_buckets[key]
        avg_p = b["price_sum"] / b["n"]
        actual = b["wins"] / b["n"]
        be = breakeven_wr(int(avg_p))
        margin = actual - be
        if margin < -0.10 and b["n"] >= 5:
            verdict = "LOSING"
        elif margin < 0 and b["n"] >= 5:
            verdict = "BELOW BE"
        elif b["n"] < 10:
            verdict = "LOW N"
        else:
            verdict = "PROFITABLE"
        print(f"  {key:<12} {b['n']:>4} {actual:>9.1%} {be:>7.1%} {margin:>+7.1%} {verdict:>12}{significance_tag(b['n'])}")

    # Market-only baseline: does market_price/100 beat EGARCH?
    subheader("Market-Only Baseline (fair_prob = market_price / 100)")
    mkt_rows = [r for r in rows if r["market_price"]]
    if mkt_rows:
        mkt_brier_sum = sum(
            (r["market_price"] / 100.0 - (1.0 if r["market_result"] == "yes" else 0.0)) ** 2
            for r in mkt_rows
        )
        mkt_brier = mkt_brier_sum / len(mkt_rows)
        print(f"  Market-only Brier:  {mkt_brier:.4f}  (n={len(mkt_rows)})")
        print(f"  EGARCH model Brier: {brier:.4f}")
        delta = brier - mkt_brier
        if delta > 0:
            print(f"  Delta: {delta:+.4f} — MARKET BEATS MODEL")
            print(f"  *** Edge inversion signal: market better calibrated than EGARCH")
        else:
            print(f"  Delta: {delta:+.4f} — MODEL BEATS MARKET")

        # Per-tier breakdown
        print(f"\n  {'Tier':>8} {'N':>4} {'MktBrier':>9} {'ModelBrier':>11} {'Winner':>10}")
        print(f"  {'-'*48}")
        for t_label, t_lo, t_hi in [("<80c", 0, 80), ("80-89c", 80, 90), ("90c+", 90, 100)]:
            t_data = [r for r in mkt_rows if t_lo <= r["market_price"] < t_hi]
            if len(t_data) < 5:
                continue
            t_mkt_b = sum((r["market_price"]/100.0 - (1.0 if r["market_result"]=="yes" else 0.0))**2
                         for r in t_data) / len(t_data)
            t_cal_b = sum((r["calibrated_prob"] - (1.0 if r["market_result"]=="yes" else 0.0))**2
                         for r in t_data) / len(t_data)
            winner = "Market" if t_cal_b > t_mkt_b else "Model"
            print(f"  {t_label:>8} {len(t_data):>4} {t_mkt_b:>8.4f} {t_cal_b:>10.4f} {winner:>10}")

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
               seconds_to_close, market_result, event_ticker
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='strategy_wait'
              AND market_result IS NOT NULL {wc}
        ORDER BY evaluation_time
    """).fetchall()

    wait_pnl = 0
    wait_wins = 0
    for r in wait_rows:
        pnl = compute_pnl(r["market_price"], r["market_result"])
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
        SELECT market_price, market_result
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='insufficient_edge'
              AND fee_adjusted_edge > -0.005
              AND market_result IS NOT NULL {wc}
    """).fetchall()

    if near_rows:
        near_pnl = sum(compute_pnl(r["market_price"], r["market_result"]) for r in near_rows)
        near_wins = sum(1 for r in near_rows if compute_pnl(r["market_price"], r["market_result"]) > 0)
        print(f"    {len(near_rows)} trades within 0.5% of edge threshold")
        print(f"    {near_wins}W/{len(near_rows) - near_wins}L ({pct(near_wins, len(near_rows))}) | PnL: {near_pnl:+d}c")
    else:
        print("    No near-threshold trades found.")

    # Window limit rejections
    subheader("spx_hourly_window_limit: Position Limit Rejections")
    wlim_rows = conn.execute(f"""
        SELECT market_price, market_result
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly'
              AND filter_stage IN ('spx_hourly_window_limit', 'spx_hourly_window_risk_cap')
              AND market_result IS NOT NULL {wc}
    """).fetchall()

    if wlim_rows:
        bl_pnl = sum(compute_pnl(r["market_price"], r["market_result"]) for r in wlim_rows)
        bl_wins = sum(1 for r in wlim_rows if compute_pnl(r["market_price"], r["market_result"]) > 0)
        print(f"    {len(wlim_rows)} blocked by window limits")
        print(f"    {bl_wins}W/{len(wlim_rows) - bl_wins}L | PnL if traded: {bl_pnl:+d}c")
    else:
        print("    No window-limit rejections yet.")

    return {
        "strategy_wait": {"count": len(wait_rows), "wins": wait_wins, "pnl_1c": wait_pnl},
        "near_edge": {"count": len(near_rows) if near_rows else 0},
        "window_limit": {"count": len(wlim_rows) if wlim_rows else 0},
    }


# ─── Section 4a: Position-Limited Simulation ─────────────────────────────────

def section_position_limit_sim(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    wc = where_clause(since)

    header("4a. POSITION-LIMITED SIMULATION")

    rows = conn.execute(f"""
        SELECT event_ticker, market_price, market_result, position_size, fee_adjusted_edge
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='spx_observation'
              AND market_result IS NOT NULL {wc}
        ORDER BY event_ticker, fee_adjusted_edge DESC
    """).fetchall()

    if not rows:
        print("  No data.")
        return {}

    window_groups = {}
    for r in rows:
        et = r["event_ticker"]
        if et not in window_groups:
            window_groups[et] = []
        window_groups[et].append(dict(r))

    print(f"  Selecting top N entries per window by highest fee-adjusted edge.")
    print(f"\n  {'Max/Window':>12s} {'N':>4s} {'W':>4s} {'L':>3s} {'WR':>6s} {'1c PnL':>8s} {'Sized PnL':>10s}")
    print(f"  {'-' * 52}")

    result = {}
    for limit in [1, 2, 3, 5, "all"]:
        pnl_1c = 0
        pnl_sz = 0
        n = 0
        wins = 0
        for et, entries in window_groups.items():
            selected = entries if limit == "all" else entries[:limit]
            for r in selected:
                pos = min(r["position_size"] or 1, 50)  # Cap at 50: SPX liquidity constraint
                n += 1
                pnl = compute_pnl(r["market_price"], r["market_result"])
                pnl_s = compute_pnl(r["market_price"], r["market_result"], pos)
                pnl_1c += pnl
                pnl_sz += pnl_s
                if pnl > 0:
                    wins += 1

        label = str(limit) if limit != "all" else "unlimited"
        wr = wins / n if n > 0 else 0
        print(f"  {label:>12s} {n:>4d} {wins:>4d} {n - wins:>3d} {wr:>5.1%} {pnl_1c:>+7d}c {pnl_sz / 100:>+9.2f}$")
        result[label] = {"n": n, "wins": wins, "pnl_1c": pnl_1c, "pnl_sz": pnl_sz}

    print(f"\n  Note: Position limits don't change which trades win/lose — they reduce")
    print(f"  exposure to correlated multi-strike blowups within a single window.")

    return result


# ─── Section 4b: Min Price Sweep ──────────────────────────────────────────────

def section_min_price_sweep(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    wc = where_clause(since)

    header("4b. MIN ENTRY PRICE SWEEP")

    rows = conn.execute(f"""
        SELECT market_price, market_result, position_size
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='spx_observation'
              AND market_result IS NOT NULL {wc}
    """).fetchall()

    if not rows:
        print("  No data.")
        return {}

    print(f"  What if we raised MIN_ENTRY_PRICE? (currently 90c)")
    print(f"\n  {'Min Price':>10s} {'N':>4s} {'W':>4s} {'L':>3s} {'WR':>6s} {'1c PnL':>8s} {'Sized PnL':>10s} {'Excluded':>9s}")
    print(f"  {'-' * 60}")

    result = {}
    total = len(rows)
    for mp in [5, 10, 15, 20, 25, 30, 40, 50, 60, 70, 80, 90]:
        filtered = [r for r in rows if r["market_price"] >= mp]
        n = len(filtered)
        w = sum(1 for r in filtered if r["market_result"] == "yes")
        pnl_1c = sum(compute_pnl(r["market_price"], r["market_result"]) for r in filtered)
        pnl_sz = sum(compute_pnl(r["market_price"], r["market_result"], min(r["position_size"] or 1, 50)) for r in filtered)
        wr = w / n if n > 0 else 0
        excluded = total - n
        marker = " <<<" if mp == 90 else ""
        if pnl_sz > 0 and pnl_1c > 0:
            marker = " *** PROFITABLE"
        print(f"  {mp:>9d}c {n:>4d} {w:>4d} {n - w:>3d} {wr:>5.1%} {pnl_1c:>+7d}c {pnl_sz / 100:>+9.2f}$ {excluded:>8d}{marker}")
        result[mp] = {"n": n, "wins": w, "pnl_1c": pnl_1c, "pnl_sz": pnl_sz}

    return result


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

    subheader("EGARCH Sigma")
    print(f"    avg={row['avg_egarch']:.2e}  min={row['min_egarch']:.2e}  max={row['max_egarch']:.2e}")

    subheader("Blended Volatility (RK + EGARCH)")
    print(f"    avg={row['avg_vol']:.2e}  min={row['min_vol']:.2e}  max={row['max_vol']:.2e}")

    subheader("egarch_blend_sigma Fill Rate")
    fill_rate = row["blend_sigma_filled"] / row["total"]
    status = "OK" if fill_rate > 0.5 else "*** LOW"
    print(f"    {row['blend_sigma_filled']}/{row['total']} ({pct(row['blend_sigma_filled'], row['total'])}) — {status}")

    # VIX data from counterfactual JSON
    subheader("VIX Integration (from counterfactual column)")
    vix_rows = conn.execute(f"""
        SELECT counterfactual
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND counterfactual IS NOT NULL
              AND filter_stage='spx_observation' {wc}
    """).fetchall()

    if vix_rows:
        vix_vals = []
        seasonal_vals = []
        n_returns_vals = []
        for r in vix_rows:
            try:
                d = json.loads(r["counterfactual"])
                if d.get("vix_implied_rv") is not None:
                    vix_vals.append(d["vix_implied_rv"])
                if d.get("seasonal_factor") is not None:
                    seasonal_vals.append(d["seasonal_factor"])
                if d.get("n_returns") is not None:
                    n_returns_vals.append(d["n_returns"])
            except (json.JSONDecodeError, TypeError):
                pass
        if vix_vals:
            print(f"    VIX implied RV: avg={sum(vix_vals)/len(vix_vals):.2e} "
                  f"min={min(vix_vals):.2e} max={max(vix_vals):.2e} (n={len(vix_vals)})")
        if seasonal_vals:
            print(f"    Seasonal factor: avg={sum(seasonal_vals)/len(seasonal_vals):.4f} "
                  f"min={min(seasonal_vals):.4f} max={max(seasonal_vals):.4f} (n={len(seasonal_vals)})")
        if n_returns_vals:
            print(f"    N returns: avg={sum(n_returns_vals)/len(n_returns_vals):.0f} "
                  f"min={min(n_returns_vals)} max={max(n_returns_vals)} (n={len(n_returns_vals)})")
    else:
        print(f"    No VIX data in DB yet (counterfactual column empty).")
        print(f"    Deploy instrumentation fix to start collecting VIX diagnostics.")

    return {
        "blend_weight": {"avg": row["avg_blend_w"], "min": row["min_blend_w"], "max": row["max_blend_w"]},
        "mz_r_squared": {"avg": row["avg_mz_r2"], "min": row["min_mz_r2"], "max": row["max_mz_r2"]},
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
               GROUP_CONCAT(market_result) as results
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='spx_observation'
              AND market_result IS NOT NULL {wc}
        GROUP BY event_ticker ORDER BY n_obs DESC
    """).fetchall()

    if not rows:
        print("  No observation data.")
        return []

    print(f"\n  {'Window':<30} {'Obs':>4} {'W':>3} {'L':>3} {'WR':>5} {'1c PnL':>8} {'Sz PnL':>9}")
    print(f"  {'-' * 70}")

    result = []
    max_obs = 0
    total_sized = 0
    for r in rows:
        detail = conn.execute(f"""
            SELECT market_price, market_result, position_size
            FROM evaluated_opportunities
            WHERE event_ticker=? AND product_type='spx_hourly' AND filter_stage='spx_observation'
                  AND market_result IS NOT NULL
        """, (r["event_ticker"],)).fetchall()

        n = len(detail)
        max_obs = max(max_obs, n)
        w = sum(1 for d in detail if d["market_result"] == "yes")
        l = n - w
        pnl_1c = sum(compute_pnl(d["market_price"], d["market_result"]) for d in detail)
        pnl_sz = sum(compute_pnl(d["market_price"], d["market_result"], min(d["position_size"] or 1, 50)) for d in detail)
        total_sized += pnl_sz

        wr_str = f"{w / n:.0%}" if n > 0 else "n/a"
        print(f"  {r['event_ticker']:<30} {n:>4} {w:>3} {l:>3} {wr_str:>5} {pnl_1c:>+7d}c {pnl_sz / 100:>+8.2f}$")

        result.append({
            "window": r["event_ticker"], "n_obs": n, "wins": w,
            "pnl_1c": pnl_1c, "pnl_sized": pnl_sz,
        })

    print(f"\n  Max observations in one window: {max_obs}")
    if max_obs > 2:
        print(f"  *** HIGH CORRELATION RISK: {max_obs} positions in a single window")

    # Count windows that lost money
    losing_windows = sum(1 for r in result if r["pnl_sized"] < 0)
    print(f"  Windows losing money: {losing_windows}/{len(result)} ({pct(losing_windows, len(result))})")

    return result


# ─── Section 7: Timing & Temperature Analysis ────────────────────────────────

def section_timing(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    wc = where_clause(since)

    header("7. TIMING ANALYSIS")

    # By STC bucket with PnL
    subheader("By Seconds-to-Close (with P&L)")
    stc_rows = conn.execute(f"""
        SELECT seconds_to_close, market_price, market_result, position_size,
               fee_adjusted_edge, calibrated_prob
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='spx_observation'
              AND market_result IS NOT NULL {wc}
    """).fetchall()

    stc_buckets = {"300-600s": [], "600-1200s": [], "1200-1800s": []}
    for r in stc_rows:
        stc = r["seconds_to_close"]
        if stc < 600:
            bk = "300-600s"
        elif stc < 1200:
            bk = "600-1200s"
        else:
            bk = "1200-1800s"
        stc_buckets[bk].append(dict(r))

    print(f"\n  {'STC Bucket':<14} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} {'BE WR':>6} {'1c PnL':>8} {'Sz PnL':>9} {'AvgEdge':>8}")
    print(f"  {'-' * 68}")
    for bk in ["300-600s", "600-1200s", "1200-1800s"]:
        entries = stc_buckets[bk]
        if not entries:
            print(f"  {bk:<14} {'--- no data ---':>30}")
            continue
        n = len(entries)
        w = sum(1 for r in entries if r["market_result"] == "yes")
        l = n - w
        wr = w / n
        avg_p = sum(r["market_price"] for r in entries) / n
        be = breakeven_wr(int(avg_p))
        pnl_1c = sum(compute_pnl(r["market_price"], r["market_result"]) for r in entries)
        pnl_sz = sum(compute_pnl(r["market_price"], r["market_result"], min(r["position_size"] or 1, 50)) for r in entries)
        avg_edge = sum((r["fee_adjusted_edge"] or 0) for r in entries) / n
        print(f"  {bk:<14} {n:>4} {w:>3} {l:>3} {wr:>5.1%} {be:>5.1%} {pnl_1c:>+7d}c {pnl_sz/100:>+8.2f}$ {avg_edge:>7.3%}")

    return {}


# ─── Section 7a: Temperature Tournament ──────────────────────────────────────

def section_temp_tournament(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    wc = where_clause(since)

    header("7a. TEMPERATURE TOURNAMENT")

    rows = conn.execute(f"""
        SELECT calibrated_prob, hourly_pre_temp_prob, hourly_applied_temp_t,
               hourly_shadow_temp_1_0, hourly_shadow_temp_2_0, hourly_shadow_temp_2_5,
               hourly_shadow_temp_1_75, hourly_shadow_temp_3_0,
               hourly_shadow_blend_50, hourly_shadow_blend_20, hourly_shadow_blend_30,
               hourly_shadow_blend_60, hourly_post_temp_prob, market_result, market_price
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='spx_observation'
              AND market_result IS NOT NULL AND hourly_pre_temp_prob IS NOT NULL {wc}
    """).fetchall()

    if not rows:
        print("  No temperature shadow data available.")
        return {}

    variants = [
        ("pre_temp (no T)", "hourly_pre_temp_prob"),
        ("T=1.0 (identity)", "hourly_shadow_temp_1_0"),
        ("T=1.75", "hourly_shadow_temp_1_75"),
        ("T=2.0", "hourly_shadow_temp_2_0"),
        ("T=2.5", "hourly_shadow_temp_2_5"),
        ("T=3.0", "hourly_shadow_temp_3_0"),
        ("blend_20 (80m/20mkt)", "hourly_shadow_blend_20"),
        ("blend_30 (70m/30mkt)", "hourly_shadow_blend_30"),
        ("blend_50 (50m/50mkt)", "hourly_shadow_blend_50"),
        ("blend_60 (40m/60mkt)", "hourly_shadow_blend_60"),
        ("post_temp", "hourly_post_temp_prob"),
        ("current (final)", "calibrated_prob"),
    ]

    print(f"\n  {'Variant':<25s} {'Brier':>8s} {'N':>5s} {'AvgPred':>8s} {'vs Current':>11s}")
    print(f"  {'-' * 60}")

    results = []
    current_brier = None
    for name, col in variants:
        brier_sum = 0
        pred_sum = 0
        count = 0
        for r in rows:
            val = r[col]
            if val is None:
                continue
            actual = 1.0 if r["market_result"] == "yes" else 0.0
            brier_sum += (val - actual) ** 2
            pred_sum += val
            count += 1
        if count > 0:
            brier = brier_sum / count
            avg_pred = pred_sum / count
            if name == "current (final)":
                current_brier = brier
            diff = ""
            if current_brier is not None and name != "current (final)":
                d = brier - current_brier
                diff = f"{d:>+10.4f}"
            results.append((brier, name, count, avg_pred))
            print(f"  {name:<25s} {brier:>7.4f} {count:>5d} {avg_pred:>7.4f} {diff}")

    if results:
        best = min(results, key=lambda x: x[0])
        worst = max(results, key=lambda x: x[0])
        print(f"\n  Best:  {best[1]} (Brier={best[0]:.4f}, n={best[2]})")
        print(f"  Worst: {worst[1]} (Brier={worst[0]:.4f}, n={worst[2]})")

        # Significance caveat
        if best[2] < 50:
            print(f"  *** Best variant has n={best[2]} — NOT SIGNIFICANT for Brier comparison")

    return {"variants": [{"name": n, "brier": b, "n": c, "avg_pred": p} for b, n, c, p in results]}


# ─── Section 7b: Price x STC Cross-Tab ───────────────────────────────────────

def section_cross_tab(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    wc = where_clause(since)

    header("7b. PRICE x STC CROSS-TAB")

    rows = conn.execute(f"""
        SELECT market_price, seconds_to_close, market_result, position_size
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='spx_observation'
              AND market_result IS NOT NULL {wc}
    """).fetchall()

    if not rows:
        print("  No data.")
        return {}

    price_labels = ["<30c", "30-49c", "50-69c", "70-84c", "85-89c", "90c+"]
    stc_labels = ["<600s", "600-1200s", "1200s+"]

    grid = {}
    for r in rows:
        p = r["market_price"]
        stc = r["seconds_to_close"]
        if p < 30:
            pk = "<30c"
        elif p < 50:
            pk = "30-49c"
        elif p < 70:
            pk = "50-69c"
        elif p < 85:
            pk = "70-84c"
        elif p < 90:
            pk = "85-89c"
        else:
            pk = "90c+"
        if stc < 600:
            sk = "<600s"
        elif stc < 1200:
            sk = "600-1200s"
        else:
            sk = "1200s+"
        key = (pk, sk)
        if key not in grid:
            grid[key] = {"w": 0, "l": 0, "pnl": 0}
        is_win = r["market_result"] == "yes"
        pos = min(r["position_size"] or 1, 50)  # Cap at 50: SPX liquidity constraint
        grid[key]["w" if is_win else "l"] += 1
        grid[key]["pnl"] += compute_pnl(r["market_price"], r["market_result"], pos)

    print(f"\n  Format: WinW/LossL WR Sized$PnL")
    print(f"\n  {'':>10s}", end="")
    for sk in stc_labels:
        print(f"  {sk:>18s}", end="")
    print()
    print(f"  {'-' * 66}")

    for pk in price_labels:
        print(f"  {pk:>10s}", end="")
        for sk in stc_labels:
            d = grid.get((pk, sk), {"w": 0, "l": 0, "pnl": 0})
            n = d["w"] + d["l"]
            if n == 0:
                print(f"  {'---':>18s}", end="")
            else:
                wr = d["w"] / n
                print(f"  {d['w']}W/{d['l']}L {wr:.0%} ${d['pnl']/100:+.0f}", end="")
        print()

    # Find best and worst cells
    cells = [(k, v) for k, v in grid.items() if v["w"] + v["l"] >= 3]
    if cells:
        best = max(cells, key=lambda x: x[1]["pnl"])
        worst = min(cells, key=lambda x: x[1]["pnl"])
        n_best = best[1]["w"] + best[1]["l"]
        n_worst = worst[1]["w"] + worst[1]["l"]
        print(f"\n  Best zone:  {best[0][0]} x {best[0][1]} — ${best[1]['pnl']/100:+.2f} (n={n_best}){significance_tag(n_best)}")
        print(f"  Worst zone: {worst[0][0]} x {worst[0][1]} — ${worst[1]['pnl']/100:+.2f} (n={n_worst}){significance_tag(n_worst)}")

    return {}


# ─── Section 8: Data Quality ─────────────────────────────────────────────────

def section_data_quality(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    wc = where_clause(since)

    header("8. DATA QUALITY AUDIT")

    # Column fill rates for observations only (not rejections which naturally miss sizing)
    subheader("Column Fill Rates (spx_observation entries only)")
    total = conn.execute(f"""
        SELECT COUNT(*) FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='spx_observation' {wc}
    """).fetchone()[0]

    critical_cols = [
        "market_price", "calibrated_prob", "edge", "fee_adjusted_edge",
        "kelly_f", "position_size", "strategy", "drawdown_scaler",
        "egarch_sigma", "egarch_blend_sigma", "egarch_blend_weight", "mz_r_squared",
        "z_score", "vol_regime", "raw_prob", "calibration_method",
        "expected_value", "breakeven_wr",
    ]

    spx_specific = [
        "counterfactual",
        "hourly_pre_temp_prob", "hourly_applied_temp_t",
        "hourly_shadow_temp_2_0", "hourly_shadow_temp_1_0",
        "hourly_shadow_temp_1_75", "hourly_shadow_temp_3_0",
        "hourly_shadow_blend_20", "hourly_shadow_blend_30",
        "hourly_shadow_blend_60", "hourly_post_temp_prob",
    ]

    issues = []
    print(f"\n  Total spx_observation entries: {total}")

    # Critical columns
    print(f"\n  {'Column':<30} {'Filled':>12} {'Status':>10}")
    print(f"  {'-' * 54}")
    for col in critical_cols:
        try:
            r = conn.execute(f"""
                SELECT SUM(CASE WHEN {col} IS NOT NULL THEN 1 ELSE 0 END) as filled
                FROM evaluated_opportunities
                WHERE product_type='spx_hourly' AND filter_stage='spx_observation' {wc}
            """).fetchone()
            filled = r["filled"] or 0
            rate = filled / total if total > 0 else 0
            status = "OK" if rate > 0.9 else ("PARTIAL" if rate > 0 else "EMPTY")
            if status != "OK":
                issues.append((col, status, filled, total))
            print(f"  {col:<30} {filled:>5}/{total} ({pct(filled, total):>6}) {status:>8}")
        except Exception:
            print(f"  {col:<30} {'NOT FOUND':>12}")

    # SPX-specific columns
    print(f"\n  SPX-specific columns:")
    for col in spx_specific:
        try:
            r = conn.execute(f"""
                SELECT SUM(CASE WHEN {col} IS NOT NULL THEN 1 ELSE 0 END) as filled
                FROM evaluated_opportunities
                WHERE product_type='spx_hourly' AND filter_stage='spx_observation' {wc}
            """).fetchone()
            filled = r["filled"] or 0
            rate = filled / total if total > 0 else 0
            status = "OK" if rate > 0.9 else ("PARTIAL" if rate > 0 else "EMPTY")
            print(f"  {col:<30} {filled:>5}/{total} ({pct(filled, total):>6}) {status:>8}")
        except Exception:
            pass

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

    if issues:
        subheader("Issues Summary")
        for col, status, filled, total in issues:
            print(f"    {status}: {col} ({filled}/{total})")

    return {"total": total, "issues": len(issues)}


# ─── Section 9: Price-Out-Of-Range Analysis ──────────────────────────────────

def section_price_range(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    wc = where_clause(since)

    header("9. PRICE-OUT-OF-RANGE ANALYSIS")

    rows = conn.execute(f"""
        SELECT market_price, calibrated_prob, market_result
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='price_out_of_range'
              AND market_result IS NOT NULL {wc}
        ORDER BY market_price
    """).fetchall()

    if not rows:
        print("  No price_out_of_range entries.")
        return {"count": 0}

    below_min = [r for r in rows if r["market_price"] < 70]
    above_max = [r for r in rows if r["market_price"] > 99]

    print(f"\n  Total: {len(rows)} | Below 70c: {len(below_min)} | Above 99c: {len(above_max)}")

    if below_min:
        subheader(f"Below MIN_ENTRY_PRICE (70c): {len(below_min)} entries")
        wins = sum(1 for r in below_min if r["market_result"] == "yes")
        print(f"    Win rate: {pct(wins, len(below_min))} ({wins}W/{len(below_min) - wins}L)")
        print(f"    Price range: {min(r['market_price'] for r in below_min)}-{max(r['market_price'] for r in below_min)}c")
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
    verdict = "FILTER WORKING (low yes = garbage correctly blocked)" if yes_rate < 0.20 else "INVESTIGATE — high yes rate may mean filter too aggressive"
    print(f"  Yes rate: {pct(row['yes_ct'], settled)} — {verdict}")
    print(f"  |z| range: {row['min_abs_z']:.1f} - {row['max_abs_z']:.1f} (avg {row['avg_abs_z']:.1f})")

    return {"count": row["cnt"], "yes_rate": round(yes_rate, 4)}


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
    for r in rows:
        print(f"  {r['hour']:>6}:00 {r['n']:>5} {r['avg_vol']:>11.2e} {r['min_vol']:>11.2e} {r['max_vol']:>11.2e}")

    if len(rows) >= 3:
        vols = [r["avg_vol"] for r in rows]
        if vols[0] > min(vols) and vols[-1] > min(vols):
            print(f"\n  U-shape pattern detected (high open, low midday, high close)")

    return [dict(r) for r in rows]


# ─── Section 12: Readiness Assessment ────────────────────────────────────────

def section_readiness(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    wc = where_clause(since)

    header("12. READINESS ASSESSMENT")

    days = conn.execute(f"""
        SELECT DISTINCT SUBSTR(evaluation_time, 1, 10) as dt
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' {wc}
    """).fetchall()
    n_days = len(days)

    obs_count = conn.execute(f"""
        SELECT COUNT(*) FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='spx_observation'
              AND market_result IS NOT NULL {wc}
    """).fetchone()[0]

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

    bw = conn.execute(f"""
        SELECT MIN(egarch_blend_weight) as mn, MAX(egarch_blend_weight) as mx
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' {wc}
    """).fetchone()
    blend_adapting = bw and bw["mn"] != bw["mx"]

    ebs = conn.execute(f"""
        SELECT SUM(CASE WHEN egarch_blend_sigma IS NOT NULL THEN 1 ELSE 0 END) as filled,
               COUNT(*) as total
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' {wc}
    """).fetchone()
    blend_sigma_ok = ebs and ebs["total"] > 0 and (ebs["filled"] / ebs["total"]) > 0.5

    # Check if calibration is profitable at any price range
    profitable = conn.execute(f"""
        SELECT COUNT(*) as n,
               SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) as wins
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='spx_observation'
              AND market_result IS NOT NULL AND market_price >= 80 {wc}
    """).fetchone()
    price_80_wr = profitable["wins"] / profitable["n"] if profitable["n"] > 0 else 0
    calibration_viable = price_80_wr > 0.85

    checks = [
        ("Trading days >= 10", n_days >= 10, f"{n_days} days"),
        ("Observations >= 100", obs_count >= 100, f"{obs_count} obs"),
        ("Min calibration bucket >= 30", min_bucket_n >= 30, f"min bucket n={min_bucket_n}"),
        ("MZ R² adapting (not stuck)", blend_adapting, "adapting" if blend_adapting else "STUCK"),
        ("egarch_blend_sigma populated", blend_sigma_ok, f"{ebs['filled']}/{ebs['total']}" if ebs else "no data"),
        ("Calibration viable (80c+ WR>85%)", calibration_viable, f"{price_80_wr:.1%} at >=80c (n={profitable['n']})"),
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
        "calibration_viable": calibration_viable,
        "all_pass": all_pass,
    }


# ─── Section 13: Promotion Config Test ───────────────────────────────────────

def section_promotion_config(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    wc = where_clause(since)

    header("13. PROMOTION CONFIG TEST")

    rows = conn.execute(f"""
        SELECT market_price, market_result, position_size, fee_adjusted_edge,
               seconds_to_close, calibrated_prob, event_ticker
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='spx_observation'
              AND market_result IS NOT NULL {wc}
    """).fetchall()

    if not rows:
        print("  No data.")
        return {}

    print(f"  Simulating different config combinations on {len(rows)} observations.")

    configs = [
        {"name": "Current (90c, no limit)", "min_p": 90, "max_per_window": 999},
        {"name": "Min 5c, no limit", "min_p": 5, "max_per_window": 999},
        {"name": "Min 10c, no limit", "min_p": 10, "max_per_window": 999},
        {"name": "Min 10c, max 2/window", "min_p": 10, "max_per_window": 2},
        {"name": "Min 20c, no limit", "min_p": 20, "max_per_window": 999},
        {"name": "Min 20c, max 2/window", "min_p": 20, "max_per_window": 2},
        {"name": "Min 30c, no limit", "min_p": 30, "max_per_window": 999},
        {"name": "Min 30c, max 3/window", "min_p": 30, "max_per_window": 3},
        {"name": "Min 50c, max 2/window", "min_p": 50, "max_per_window": 2},
    ]

    print(f"\n  {'Config':<30s} {'N':>4s} {'W':>3s} {'L':>3s} {'WR':>6s} {'1c PnL':>8s} {'Sz PnL':>10s}")
    print(f"  {'-' * 68}")

    results = []
    for cfg in configs:
        # Filter by min price
        filtered = [r for r in rows if r["market_price"] >= cfg["min_p"]]

        # Apply per-window limit (select top entries by edge)
        if cfg["max_per_window"] < 999:
            window_groups = {}
            for r in filtered:
                et = r["event_ticker"]
                if et not in window_groups:
                    window_groups[et] = []
                window_groups[et].append(r)
            limited = []
            for et, entries in window_groups.items():
                sorted_entries = sorted(entries, key=lambda x: x["fee_adjusted_edge"] or 0, reverse=True)
                limited.extend(sorted_entries[:cfg["max_per_window"]])
            filtered = limited

        n = len(filtered)
        w = sum(1 for r in filtered if r["market_result"] == "yes")
        l = n - w
        wr = w / n if n > 0 else 0
        pnl_1c = sum(compute_pnl(r["market_price"], r["market_result"]) for r in filtered)
        pnl_sz = sum(compute_pnl(r["market_price"], r["market_result"], min(r["position_size"] or 1, 50)) for r in filtered)

        marker = ""
        if pnl_1c > 0 and pnl_sz > 0:
            marker = " *** PROFITABLE"
        elif pnl_1c > 0:
            marker = " * 1c profitable"

        print(f"  {cfg['name']:<30s} {n:>4d} {w:>3d} {l:>3d} {wr:>5.1%} {pnl_1c:>+7d}c {pnl_sz/100:>+9.2f}${marker}")
        results.append({"config": cfg["name"], "n": n, "wins": w, "pnl_1c": pnl_1c, "pnl_sz": pnl_sz})

    # Significance warning
    print(f"\n  *** All results based on n={len(rows)} observations over limited trading days.")
    print(f"  *** NOT statistically significant for promotion decisions. Continue collecting data.")

    return {"configs": results}


# ─── CalEngine Observation Pipeline ───────────────────────────────────────────

def section_cal_engine_obs(conn, since=None):
    """CalEngine observation pipeline: settled evals with raw_prob for SPX."""
    header("CALENGINE OBSERVATION PIPELINE")
    try:
        w = f"AND evaluation_time >= '{since}'" if since else ""
        row = conn.execute(f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN raw_prob IS NOT NULL THEN 1 ELSE 0 END) AS with_raw_prob,
                   SUM(CASE WHEN status='settled' AND raw_prob IS NOT NULL THEN 1 ELSE 0 END) AS cal_eligible
            FROM evaluated_opportunities
            WHERE product_type='spx_hourly' {w}
        """).fetchone()
        total = row[0] or 0
        with_rp = row[1] or 0
        eligible = row[2] or 0
        print(f"  Total SPX evals:          {total}")
        print(f"  With raw_prob:            {with_rp}")
        print(f"  Settled + raw_prob (cal):  {eligible}")
        if eligible > 0:
            print(f"\n  >>> {eligible} observations feeding SPX CalEngine")
        else:
            print("\n  >>> No CalEngine observations yet")
        return {"total": total, "with_raw_prob": with_rp, "cal_eligible": eligible}
    except Exception as e:
        print(f"  ERROR: {e}")
        return {"error": str(e)}


# ─── Section 14: HAR-RV Shadow Engine ─────────────────────────────────────

def section_harrv_shadow(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    """Analyze SPX HAR-RV shadow signals — performance, gates, model diagnostics."""
    header("14. SPX HAR-RV SHADOW ENGINE")

    # Check if table exists
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='spx_harrv_shadow_signals'"
    ).fetchone()
    if not table_check:
        print("  spx_harrv_shadow_signals table not found — engine not yet deployed or no data.")
        return {"status": "no_table"}

    # Overview counts
    counts = conn.execute("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled,
               SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending,
               SUM(CASE WHEN gates_passed=1 THEN 1 ELSE 0 END) as passed_gates,
               MIN(evaluation_time) as first_eval,
               MAX(evaluation_time) as last_eval
        FROM spx_harrv_shadow_signals
    """).fetchone()

    total = counts["total"] or 0
    settled = counts["settled"] or 0
    pending = counts["pending"] or 0
    passed = counts["passed_gates"] or 0

    if total == 0:
        print("  No HAR-RV signals recorded yet.")
        return {"status": "no_data"}

    print(f"\n  Total signals:  {total}")
    print(f"  Settled:        {settled}")
    print(f"  Pending:        {pending}")
    print(f"  Passed gates:   {passed} ({pct(passed, total)})")
    print(f"  Date range:     {(counts['first_eval'] or '')[:19]} → {(counts['last_eval'] or '')[:19]}")

    # ── Settled performance ──
    subheader("Settled Performance")
    settled_rows = conn.execute("""
        SELECT market_price, market_result, shadow_contracts, shadow_pnl_cents,
               final_prob, edge, fee_adjusted_edge, gates_passed, seconds_to_close,
               egarch_prob, egarch_edge, raw_prob, scaled_prob, mkt_only_prob,
               har_method, rv_1h, sigma_forecast
        FROM spx_harrv_shadow_signals
        WHERE status='settled'
    """).fetchall()

    if not settled_rows:
        print("  No settled signals yet.")
    else:
        wins = sum(1 for r in settled_rows if r["market_result"] in ("yes", "all_yes"))
        losses = len(settled_rows) - wins
        wr = wins / len(settled_rows)
        total_pnl = sum((r["shadow_pnl_cents"] or 0) for r in settled_rows)
        # Counterfactual 1-contract PnL (uses market_price when contracts=0)
        cf_pnl_1c = 0
        for r in settled_rows:
            if (r["shadow_contracts"] or 0) > 0:
                cf_pnl_1c += r["shadow_pnl_cents"] or 0
            elif r["market_result"] in ("yes", "all_yes"):
                cf_pnl_1c += 100 - (r["market_price"] or 0)
            elif r["market_result"] in ("no", "all_no"):
                cf_pnl_1c -= r["market_price"] or 0

        # Only signals that passed gates
        gated = [r for r in settled_rows if r["gates_passed"]]
        gated_wins = sum(1 for r in gated if r["market_result"] in ("yes", "all_yes"))

        print(f"\n  All settled:    {len(settled_rows)} signals, {wins}W/{losses}L ({wr:.1%} WR)")
        print(f"  Shadow PnL:     {total_pnl:+d}c (${total_pnl/100:+.2f}){significance_tag(len(settled_rows))}")
        print(f"  CF 1c PnL:      {cf_pnl_1c:+d}c (${cf_pnl_1c/100:+.2f}) — counterfactual at 1 contract")
        if gated:
            gated_wr = gated_wins / len(gated)
            gated_pnl = sum((r["shadow_pnl_cents"] or 0) for r in gated)
            print(f"  Gates-passed:   {len(gated)} signals, {gated_wins}W/{len(gated)-gated_wins}L "
                  f"({gated_wr:.1%} WR), PnL={gated_pnl:+d}c{significance_tag(len(gated))}")
        else:
            print(f"  Gates-passed:   0 signals (all gated out)")

    # ── Brier score comparison: HAR-RV vs EGARCH vs Market ──
    subheader("Brier Score: HAR-RV vs EGARCH vs Market")
    brier_rows = [r for r in settled_rows if r["final_prob"] is not None] if settled_rows else []
    if brier_rows:
        harrv_brier_sum = 0
        egarch_brier_sum = 0
        mkt_brier_sum = 0
        egarch_n = 0
        mkt_n = 0
        for r in brier_rows:
            actual = 1.0 if r["market_result"] in ("yes", "all_yes") else 0.0
            harrv_brier_sum += (r["final_prob"] - actual) ** 2
            if r["egarch_prob"] is not None:
                egarch_brier_sum += (r["egarch_prob"] - actual) ** 2
                egarch_n += 1
            if r["mkt_only_prob"] is not None:
                mkt_brier_sum += (r["mkt_only_prob"] - actual) ** 2
                mkt_n += 1

        harrv_bs = harrv_brier_sum / len(brier_rows)
        print(f"\n  {'Model':<25s} {'Brier':>8s} {'N':>5s}")
        print(f"  {'-' * 40}")
        print(f"  {'HAR-RV (final_prob)':<25s} {harrv_bs:>7.4f} {len(brier_rows):>5d}")
        if egarch_n > 0:
            egarch_bs = egarch_brier_sum / egarch_n
            delta = harrv_bs - egarch_bs
            winner = "EGARCH" if delta > 0 else "HAR-RV"
            print(f"  {'EGARCH (egarch_prob)':<25s} {egarch_bs:>7.4f} {egarch_n:>5d}  (delta={delta:+.4f} → {winner})")
        if mkt_n > 0:
            mkt_bs = mkt_brier_sum / mkt_n
            delta = harrv_bs - mkt_bs
            winner = "Market" if delta > 0 else "HAR-RV"
            print(f"  {'Market-only':<25s} {mkt_bs:>7.4f} {mkt_n:>5d}  (delta={delta:+.4f} → {winner})")

        # raw_prob (pre-temperature, pre-blend) Brier
        raw_rows = [r for r in brier_rows if r["raw_prob"] is not None]
        if raw_rows:
            raw_bs = sum((r["raw_prob"] - (1.0 if r["market_result"] in ("yes", "all_yes") else 0.0)) ** 2
                         for r in raw_rows) / len(raw_rows)
            print(f"  {'HAR-RV raw (no T, no mkt)':<25s} {raw_bs:>7.4f} {len(raw_rows):>5d}")
    else:
        print("  No settled signals with probability data.")

    # ── Gate failure analysis ──
    subheader("Gate Failure Analysis")
    gate_rows = conn.execute("""
        SELECT gate_failures FROM spx_harrv_shadow_signals
        WHERE gate_failures IS NOT NULL AND gate_failures != ''
    """).fetchall()

    if gate_rows:
        gate_counts: Dict[str, int] = {}
        for r in gate_rows:
            for failure in r["gate_failures"].split("; "):
                gate_name = failure.split(":")[0].strip()
                if gate_name:
                    gate_counts[gate_name] = gate_counts.get(gate_name, 0) + 1

        print(f"\n  {'Gate':<20s} {'Failures':>9s} {'% of signals':>14s}")
        print(f"  {'-' * 45}")
        for gate, cnt in sorted(gate_counts.items(), key=lambda x: -x[1]):
            print(f"  {gate:<20s} {cnt:>9d} {pct(cnt, total):>14s}")
    else:
        print("  No gate failures recorded.")

    # ── Model diagnostics ──
    subheader("Model Diagnostics")
    diag = conn.execute("""
        SELECT har_method,
               AVG(rv_1h) as avg_rv_1h,
               AVG(sigma_forecast) as avg_sigma,
               MIN(sigma_forecast) as min_sigma,
               MAX(sigma_forecast) as max_sigma,
               AVG(n_ols_obs) as avg_ols_obs,
               MAX(n_ols_obs) as max_ols_obs,
               COUNT(*) as n
        FROM spx_harrv_shadow_signals
        GROUP BY har_method
    """).fetchall()

    if diag:
        print(f"\n  {'Method':<10s} {'N':>5s} {'AvgSigma':>12s} {'MinSigma':>12s} {'MaxSigma':>12s} {'OLS obs':>8s}")
        print(f"  {'-' * 60}")
        for r in diag:
            print(f"  {r['har_method'] or 'unknown':<10s} {r['n']:>5d} "
                  f"{r['avg_sigma']:>11.2e} {r['min_sigma']:>11.2e} {r['max_sigma']:>11.2e} "
                  f"{int(r['max_ols_obs'] or 0):>8d}")

    # ── By price tier ──
    subheader("Performance by Price Tier")
    if settled_rows:
        tiers = {"<80c": [], "80-89c": [], "90c+": []}
        for r in settled_rows:
            p = r["market_price"] or 0
            if p < 80:
                tiers["<80c"].append(r)
            elif p < 90:
                tiers["80-89c"].append(r)
            else:
                tiers["90c+"].append(r)

        print(f"\n  {'Tier':<10s} {'N':>4s} {'W':>3s} {'L':>3s} {'WR':>6s} {'PnL':>8s}")
        print(f"  {'-' * 40}")
        for label in ["<80c", "80-89c", "90c+"]:
            subset = tiers[label]
            if not subset:
                print(f"  {label:<10s} {'---':>4s}")
                continue
            w = sum(1 for r in subset if r["market_result"] in ("yes", "all_yes"))
            pnl = sum((r["shadow_pnl_cents"] or 0) for r in subset)
            wr_val = w / len(subset)
            print(f"  {label:<10s} {len(subset):>4d} {w:>3d} {len(subset)-w:>3d} "
                  f"{wr_val:>5.1%} {pnl:>+7d}c{significance_tag(len(subset))}")

    return {
        "total_signals": total,
        "settled": settled,
        "pending": pending,
        "passed_gates": passed,
    }


# ─── Section 15: HAR-RV NO-Side Shadow Analysis ───────────────────────────

def section_harrv_no_side(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    """Analyze HAR-RV NO-side shadow signals — performance, gates, comparison with YES-side."""
    header("15. HAR-RV NO-SIDE SHADOW ANALYSIS")

    # Check if spx_harrv_shadow_signals table exists
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='spx_harrv_shadow_signals'"
    ).fetchone()
    if not table_check:
        print("  spx_harrv_shadow_signals table not found — engine not yet deployed.")
        return {"status": "no_table"}

    # Check if NO-side columns exist (graceful degradation)
    cols_info = conn.execute("PRAGMA table_info(spx_harrv_shadow_signals)").fetchall()
    col_names = {c["name"] for c in cols_info}
    no_cols_needed = {"no_prob", "no_edge", "no_fee_edge", "no_kelly_f",
                      "no_contracts", "no_gates_passed", "no_gate_failures", "no_pnl_cents"}
    missing = no_cols_needed - col_names
    if missing:
        print(f"  NO-side columns not yet present: {', '.join(sorted(missing))}")
        print("  Engine needs update to record NO-side data.")
        return {"status": "missing_columns", "missing": sorted(missing)}

    wc = where_clause(since)

    # ── Overview: NO-side signals with contracts > 0 ──
    no_overview = conn.execute(f"""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN no_contracts > 0 THEN 1 ELSE 0 END) as with_contracts,
               SUM(CASE WHEN status='settled' AND no_contracts > 0 THEN 1 ELSE 0 END) as settled_with_contracts,
               SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled_total,
               SUM(CASE WHEN no_gates_passed=1 THEN 1 ELSE 0 END) as no_gates_passed,
               SUM(CASE WHEN gates_passed=1 THEN 1 ELSE 0 END) as yes_gates_passed
        FROM spx_harrv_shadow_signals
        WHERE 1=1 {wc}
    """).fetchone()

    total = no_overview["total"] or 0
    no_with_contracts = no_overview["with_contracts"] or 0
    no_settled = no_overview["settled_with_contracts"] or 0
    settled_total = no_overview["settled_total"] or 0
    no_gp = no_overview["no_gates_passed"] or 0
    yes_gp = no_overview["yes_gates_passed"] or 0

    if total == 0:
        print("  No HAR-RV signals recorded yet.")
        return {"status": "no_data"}

    print(f"\n  Total HAR-RV signals:           {total}")
    print(f"  NO-side with contracts > 0:     {no_with_contracts} ({pct(no_with_contracts, total)})")
    print(f"  NO-side settled (contracts>0):  {no_settled}")

    # ── Gates comparison ──
    subheader("Gates Pass Rate: YES vs NO")
    print(f"\n  {'Side':<8s} {'Passed':>8s} {'Total':>7s} {'Rate':>8s}")
    print(f"  {'-' * 35}")
    print(f"  {'YES':<8s} {yes_gp:>8d} {total:>7d} {pct(yes_gp, total):>8s}")
    print(f"  {'NO':<8s} {no_gp:>8d} {total:>7d} {pct(no_gp, total):>8s}")

    # ── Settled NO-side performance ──
    subheader("Settled NO-Side Performance")
    no_settled_rows = conn.execute(f"""
        SELECT market_price, market_result, no_contracts, no_pnl_cents,
               no_prob, no_edge, no_fee_edge, no_gates_passed, no_gate_failures,
               seconds_to_close, har_method
        FROM spx_harrv_shadow_signals
        WHERE status='settled' AND no_contracts > 0 {wc}
    """).fetchall()

    no_side_result = {"total_signals": total, "no_with_contracts": no_with_contracts,
                      "no_settled": no_settled, "gates_yes": yes_gp, "gates_no": no_gp}

    if not no_settled_rows:
        print("  No settled NO-side signals with contracts > 0.")
    else:
        # NO wins when market_result IN ('no', 'all_no')
        no_wins = sum(1 for r in no_settled_rows if r["market_result"] in ("no", "all_no"))
        no_losses = len(no_settled_rows) - no_wins
        no_wr = no_wins / len(no_settled_rows)
        no_total_pnl = sum((r["no_pnl_cents"] or 0) for r in no_settled_rows)

        print(f"\n  Settled NO signals:  {len(no_settled_rows)}")
        print(f"  Win rate:            {no_wr:.1%} ({no_wins}W/{no_losses}L)"
              f"{significance_tag(len(no_settled_rows))}")
        print(f"  NO-side sim PnL:     {no_total_pnl:+d}c (${no_total_pnl/100:+.2f})")

        # Gated NO-side subset
        no_gated = [r for r in no_settled_rows if r["no_gates_passed"]]
        if no_gated:
            ng_wins = sum(1 for r in no_gated if r["market_result"] in ("no", "all_no"))
            ng_pnl = sum((r["no_pnl_cents"] or 0) for r in no_gated)
            ng_wr = ng_wins / len(no_gated)
            print(f"  Gates-passed NO:     {len(no_gated)} signals, {ng_wins}W/{len(no_gated)-ng_wins}L "
                  f"({ng_wr:.1%} WR), PnL={ng_pnl:+d}c{significance_tag(len(no_gated))}")
        else:
            print(f"  Gates-passed NO:     0 signals (all gated out)")

        no_side_result.update({
            "no_wins": no_wins, "no_losses": no_losses,
            "no_wr": round(no_wr, 4), "no_pnl_cents": no_total_pnl,
        })

    # ── YES vs NO side-by-side on same settled signals ──
    subheader("YES vs NO Side-by-Side (settled signals)")
    both_rows = conn.execute(f"""
        SELECT market_price, market_result, shadow_pnl_cents, no_pnl_cents,
               shadow_contracts, no_contracts, gates_passed, no_gates_passed,
               edge, no_edge, fee_adjusted_edge, no_fee_edge
        FROM spx_harrv_shadow_signals
        WHERE status='settled' {wc}
    """).fetchall()

    if both_rows:
        yes_pnl = sum((r["shadow_pnl_cents"] or 0) for r in both_rows if (r["shadow_contracts"] or 0) > 0)
        no_pnl = sum((r["no_pnl_cents"] or 0) for r in both_rows if (r["no_contracts"] or 0) > 0)
        combined = yes_pnl + no_pnl
        yes_ct = sum(1 for r in both_rows if (r["shadow_contracts"] or 0) > 0)
        no_ct = sum(1 for r in both_rows if (r["no_contracts"] or 0) > 0)

        print(f"\n  {'Side':<8s} {'Signals':>9s} {'PnL':>10s}")
        print(f"  {'-' * 30}")
        print(f"  {'YES':<8s} {yes_ct:>9d} {yes_pnl:>+9d}c")
        print(f"  {'NO':<8s} {no_ct:>9d} {no_pnl:>+9d}c")
        print(f"  {'COMBINED':<8s} {'':>9s} {combined:>+9d}c")

        no_side_result["combined_pnl_cents"] = combined

    # ── Check evaluated_opportunities for NO-side entries ──
    subheader("NO-Side in evaluated_opportunities")
    eo_cols = conn.execute("PRAGMA table_info(evaluated_opportunities)").fetchall()
    eo_col_names = {c["name"] for c in eo_cols}
    if "side" not in eo_col_names:
        print("  'side' column not present in evaluated_opportunities — no NO-side evals tracked there.")
    else:
        eo_wc = where_clause(since)
        eo_rows = conn.execute(f"""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled,
                   SUM(CASE WHEN status='settled' AND market_result IN ('no', 'all_no') THEN 1 ELSE 0 END) as no_wins
            FROM evaluated_opportunities
            WHERE side='no' AND product_type IN ('spx_hourly') {eo_wc}
        """).fetchone()
        eo_total = eo_rows["total"] or 0
        eo_settled = eo_rows["settled"] or 0
        eo_no_wins = eo_rows["no_wins"] or 0
        print(f"  NO-side evals (product_type=spx_hourly):  {eo_total}")
        print(f"  Settled:                                   {eo_settled}")
        if eo_settled > 0:
            print(f"  NO wins (result in no/all_no):             {eo_no_wins} ({pct(eo_no_wins, eo_settled)})")
        no_side_result["eo_total"] = eo_total
        no_side_result["eo_settled"] = eo_settled

    # ── NO-side gate failure breakdown ──
    subheader("NO-Side Gate Failures")
    no_gate_rows = conn.execute(f"""
        SELECT no_gate_failures FROM spx_harrv_shadow_signals
        WHERE no_gate_failures IS NOT NULL AND no_gate_failures != '' {wc}
    """).fetchall()

    if no_gate_rows:
        gate_counts: Dict[str, int] = {}
        for r in no_gate_rows:
            for failure in r["no_gate_failures"].split("; "):
                gate_name = failure.split(":")[0].strip()
                if gate_name:
                    gate_counts[gate_name] = gate_counts.get(gate_name, 0) + 1

        print(f"\n  {'Gate':<20s} {'Failures':>9s} {'% of signals':>14s}")
        print(f"  {'-' * 45}")
        for gate, cnt in sorted(gate_counts.items(), key=lambda x: -x[1]):
            print(f"  {gate:<20s} {cnt:>9d} {pct(cnt, total):>14s}")
    else:
        print("  No NO-side gate failures recorded.")

    return no_side_result


def detect_regime_start() -> str:
    """Auto-detect regime start by finding the last git commit that changed
    SPX hourly trading constants in bot/_impl.py."""
    import subprocess

    REGIME_CONSTANTS = [
        "SPX_HOURLY_OBSERVATION_ONLY", "SPX_HOURLY_MIN_ENTRY_PRICE",
        "SPX_HOURLY_MAX_ENTRY_PRICE", "SPX_HOURLY_MARKET_BLEND_W",
        "SPX_HOURLY_TEMPERATURE_T", "SPX_HOURLY_KELLY_FRACTION",
        "SPX_HOURLY_MAX_RISK_PER_TRADE", "SPX_HOURLY_FEE_MULTIPLIER",
        "SPX_HOURLY_MAX_POSITIONS_PER_WINDOW", "SPX_HOURLY_MAX_WINDOW_RISK",
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


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="SPX Shadow Engine Audit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default="state.db", help="Path to state.db")
    parser.add_argument("--since", default=None, help="Only analyze data since YYYY-MM-DD")
    parser.add_argument("--regime", choices=["auto"],
                        help="Auto-detect regime start from git history")
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

    count = conn.execute(
        "SELECT COUNT(*) FROM evaluated_opportunities WHERE product_type='spx_hourly'"
    ).fetchone()[0]
    if count == 0:
        print("No SPX hourly data found in evaluated_opportunities.")
        sys.exit(0)

    if args.regime == "auto":
        since = detect_regime_start()
        print(f"[Auto-detected regime start: {since}]")
    else:
        since = args.since

    print(f"\nSPX Shadow Engine Audit — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    if since:
        print(f"Filtering to data since: {since}")
    print(f"Database: {args.db} ({count} SPX evaluations)")

    # Run all sections
    perf = section_performance(conn, since)
    price_bk = section_price_buckets(conn, since)
    daily = section_daily_pnl(conn, since)
    loss = section_loss_analysis(conn, since)
    cal = section_calibration(conn, since)
    funnel = section_filter_funnel(conn, since)
    leakage = section_ev_leakage(conn, since)
    pos_sim = section_position_limit_sim(conn, since)
    min_price = section_min_price_sweep(conn, since)
    vol = section_vol_health(conn, since)
    corr = section_correlation(conn, since)
    timing = section_timing(conn, since)
    temp = section_temp_tournament(conn, since)
    cross = section_cross_tab(conn, since)
    quality = section_data_quality(conn, since)
    price_range = section_price_range(conn, since)
    zscore = section_zscore(conn, since)
    vol_intraday = section_vol_intraday(conn, since)
    readiness = section_readiness(conn, since)
    promotion = section_promotion_config(conn, since)
    cal_obs = section_cal_engine_obs(conn, since)
    harrv = section_harrv_shadow(conn, since)
    harrv_no = section_harrv_no_side(conn, since)

    conn.close()

    # JSON artifact
    if args.json:
        artifact = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "since": args.since,
            "performance": perf,
            "price_buckets": price_bk,
            "daily_pnl": daily,
            "loss_analysis": loss,
            "calibration": cal,
            "filter_funnel": funnel,
            "ev_leakage": leakage,
            "position_sim": pos_sim,
            "min_price_sweep": min_price,
            "vol_health": vol,
            "correlation": corr,
            "timing": timing,
            "temp_tournament": temp,
            "cross_tab": cross,
            "data_quality": quality,
            "price_range": price_range,
            "zscore": zscore,
            "vol_intraday": vol_intraday,
            "readiness": readiness,
            "promotion_config": promotion,
            "cal_engine_obs": cal_obs,
            "harrv_shadow": harrv,
            "harrv_no_side": harrv_no,
        }
        with open(args.json, "w") as f:
            json.dump(artifact, f, indent=2, default=str)
        print(f"\nJSON artifact written to: {args.json}")

    print(f"\n{'=' * 72}")
    print(f"  AUDIT COMPLETE")
    print(f"{'=' * 72}")


if __name__ == "__main__":
    main()
