#!/usr/bin/env python3
"""Hourly crypto shadow mode audit script.

Runs locally against a copy of state.db from VPS.
Evaluates shadow performance, calibration, config sensitivity, and readiness.

Usage:
    scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
    python scripts/hourly_shadow_audit.py [--db /tmp/state.db] [--since 2026-02-28]
    python scripts/hourly_shadow_audit.py --db /tmp/state.db --regime auto
"""

import argparse
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple


# ── Helpers ──────────────────────────────────────────────────────

def connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Check if a column exists in a table."""
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(c["name"] == column for c in cols)


def wilson_ci(wins: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson score confidence interval for a proportion."""
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    lo = max(0.0, (centre - spread) / denom)
    hi = min(1.0, (centre + spread) / denom)
    return (lo, hi)


def section(title: str) -> None:
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}\n")


def subsection(title: str) -> None:
    print(f"\n--- {title} ---")


def taker_fee(price_cents: int, contracts: int = 1) -> int:
    """Kalshi taker fee: ceil(0.07 * C * P * (1-P)/100)"""
    return math.ceil(0.07 * contracts * price_cents * (100 - price_cents) / 10000)


def sim_pnl_taker(price: int, size: int, won: bool) -> float:
    """Simulate PnL in cents for a taker trade (hourly is HOURLY_TAKER_ONLY=True)."""
    fee = taker_fee(price, size)
    if won:
        return size * (100 - price) - fee
    else:
        return -(size * price) - fee


def fisher_exact_2x2(a: int, b: int, c: int, d: int) -> float:
    """One-sided Fisher exact test p-value for [[a,b],[c,d]].
    Tests if group 1 (a wins, b losses) has significantly higher WR
    than group 2 (c wins, d losses). Uses log-space for large factorials."""
    n = a + b + c + d
    if n == 0:
        return 1.0

    def log_fact(x: int) -> float:
        return sum(math.log(i) for i in range(1, x + 1)) if x > 0 else 0.0

    def log_hyper(aa: int) -> float:
        """Log of hypergeometric probability for given aa."""
        bb = a + b - aa
        cc = a + c - aa
        dd = d - (a - aa) + (c - (a + c - aa))
        # recalculate properly
        r1 = a + b
        r2 = c + d
        c1 = a + c
        c2 = b + d
        bb2 = r1 - aa
        cc2 = c1 - aa
        dd2 = r2 - cc2
        if bb2 < 0 or cc2 < 0 or dd2 < 0:
            return float('-inf')
        return (log_fact(r1) + log_fact(r2) + log_fact(c1) + log_fact(c2)
                - log_fact(n) - log_fact(aa) - log_fact(bb2)
                - log_fact(cc2) - log_fact(dd2))

    # Sum probabilities for all tables as extreme or more extreme
    r1 = a + b
    c1 = a + c
    r2 = c + d
    c2 = b + d
    p_obs = log_hyper(a)
    p_sum = 0.0
    lo = max(0, c1 - r2)
    hi = min(r1, c1)
    for aa in range(lo, hi + 1):
        lp = log_hyper(aa)
        if lp <= p_obs + 1e-10:  # as extreme or more extreme
            p_sum += math.exp(lp)
    return min(1.0, p_sum)


def detect_regime_start(conn: sqlite3.Connection) -> str:
    """Auto-detect regime start by finding the last git commit that changed
    hourly trading constants in bot/_impl.py.

    Falls back to 2026-02-28 if git is unavailable."""
    import subprocess

    # Constants whose changes define a new regime for hourly trading
    REGIME_CONSTANTS = [
        "HOURLY_OBSERVATION_ONLY", "HOURLY_MIN_ENTRY_PRICE",
        "HOURLY_MAX_ENTRY_PRICE", "HOURLY_MARKET_BLEND_W",
        "HOURLY_MIN_EDGE_PCT", "HOURLY_MAX_RISK_PER_TRADE",
        "HOURLY_KELLY_FRACTION", "HOURLY_TEMPERATURE_T",
        "HOURLY_CALIBRATION_ENABLED", "HOURLY_MIN_STC_ENTRY",
        "HOURLY_MAX_STC_ENTRY", "HOURLY_EXCLUDED_ASSETS",
        "HOURLY_MAX_POSITIONS_PER_WINDOW", "HOURLY_MAX_WINDOW_RISK",
    ]

    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
                ["git", "diff", f"{commit_hash}^..{commit_hash}",
                 "--", "bot.py", "bot/_impl.py", "bot/constants.py"],
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


# ── Section 1: Shadow Performance Summary ─────────────────────────

def performance_summary(conn: sqlite3.Connection, since: str) -> dict:
    section("1. SHADOW PERFORMANCE SUMMARY")

    row = conn.execute("""
        SELECT COUNT(*) AS total,
          SUM(CASE WHEN filter_stage='hourly_observation' THEN 1 ELSE 0 END) AS signals,
          MIN(evaluation_time) AS first_t, MAX(evaluation_time) AS last_t
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND evaluation_time >= ?
    """, (since,)).fetchone()

    total = row["total"] or 0
    signals = row["signals"] or 0
    print(f"Total evaluations:  {total}")
    if total > 0:
        print(f"Signals:            {signals} ({signals/total*100:.1f}%)")
    print(f"Period:             {row['first_t'] or '—'} to {row['last_t'] or '—'}")

    # Observation outcomes
    rows = conn.execute("""
        SELECT evaluation_time, ticker, asset, market_price, calibrated_prob,
          fee_adjusted_edge, seconds_to_close, position_size, market_result,
          hourly_pre_temp_prob, hourly_applied_temp_t
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND evaluation_time >= ?
        ORDER BY evaluation_time
    """, (since,)).fetchall()

    settled = [r for r in rows if r["market_result"] is not None]
    wins = [r for r in settled if r["market_result"] == "yes"]
    losses = [r for r in settled if r["market_result"] == "no"]
    pending = [r for r in rows if r["market_result"] is None]

    print(f"\nSettled: {len(settled)} | Pending: {len(pending)}")
    print(f"Wins: {len(wins)} | Losses: {len(losses)}")

    total_pnl = 0
    if settled:
        wr = len(wins) / len(settled) * 100
        ci_lo, ci_hi = wilson_ci(len(wins), len(settled))
        print(f"Win rate: {wr:.1f}% [Wilson 95% CI: {ci_lo*100:.1f}-{ci_hi*100:.1f}%]")

        # Simulated PnL
        for r in settled:
            size = r["position_size"] or 25
            p = r["market_price"]
            total_pnl += sim_pnl_taker(p, size, r["market_result"] == "yes")

        avg_p = sum(r["market_price"] for r in settled) / len(settled)
        # Breakeven WR accounts for taker fees: WR = (price + avg_fee_per_contract) / 100
        avg_taker_fee = sum(taker_fee(r["market_price"], 1) for r in settled) / len(settled)
        be_wr = (avg_p + avg_taker_fee) / 100.0 * 100  # breakeven WR in %
        avg_stc = sum(r["seconds_to_close"] or 0 for r in settled) / len(settled)
        print(f"Simulated PnL (taker): ${total_pnl/100:.2f}")
        print(f"Avg PnL/trade: ${total_pnl/100/len(settled):.2f}")
        print(f"Avg entry price: {avg_p:.1f}c (breakeven WR w/fees: {be_wr:.1f}%)")
        print(f"WR vs breakeven: {wr:.1f}% vs {be_wr:.1f}% ({wr - be_wr:+.1f}pp)")
        print(f"Avg STC at entry: {avg_stc:.0f}s ({avg_stc/60:.1f}m)")
        print(f"Fee assumption: taker (ceil(0.07*C*P*(1-P)/100), HOURLY_TAKER_ONLY=True)")

    # Per-asset
    subsection("Per-asset breakdown")
    print(f"{'Asset':<8} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} {'Sim PnL':>10} {'Avg P':>7} {'Avg Edge':>10}")
    print("-" * 60)
    by_asset = defaultdict(list)
    for r in settled:
        by_asset[r["asset"]].append(r)

    for asset in sorted(by_asset):
        asset_rows = by_asset[asset]
        w = sum(1 for r in asset_rows if r["market_result"] == "yes")
        l_count = len(asset_rows) - w
        pnl = sum(sim_pnl_taker(r["market_price"], r["position_size"] or 25,
                                 r["market_result"] == "yes") for r in asset_rows)
        avg_price = sum(r["market_price"] for r in asset_rows) / len(asset_rows)
        avg_edge = sum(r["fee_adjusted_edge"] or 0 for r in asset_rows) / len(asset_rows)
        wr_a = w / len(asset_rows) * 100 if asset_rows else 0
        print(f"{asset:<8} {len(asset_rows):>4} {w:>3} {l_count:>3} {wr_a:>5.1f}% "
              f"${pnl/100:>9.2f} {avg_price:>6.1f}c {avg_edge:>+9.4f}")

    # Edge bucket analysis
    subsection("Edge bucket analysis")
    print(f"{'Edge':>10} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} {'Sim PnL':>10} {'Verdict':<14}")
    print("-" * 55)
    edge_buckets = [(0, 0.01, "0-1%"), (0.01, 0.02, "1-2%"), (0.02, 0.03, "2-3%"),
                    (0.03, 0.05, "3-5%"), (0.05, 1.0, "5%+")]
    for lo, hi, label in edge_buckets:
        subset = [r for r in settled if r["fee_adjusted_edge"] is not None
                  and lo <= r["fee_adjusted_edge"] < hi]
        if subset:
            w = sum(1 for r in subset if r["market_result"] == "yes")
            pnl = sum(sim_pnl_taker(r["market_price"], r["position_size"] or 25,
                                     r["market_result"] == "yes") for r in subset)
            wr_b = w / len(subset) * 100
            avg_p_b = sum(r["market_price"] for r in subset) / len(subset)
            avg_fee_b = sum(taker_fee(r["market_price"], 1) for r in subset) / len(subset)
            be_wr_b = (avg_p_b + avg_fee_b) / 100.0 * 100
            verdict = "PROFITABLE" if wr_b > be_wr_b else "UNPROFITABLE"
            print(f"{label:>10} {len(subset):>4} {w:>3} {len(subset)-w:>3} "
                  f"{wr_b:>5.0f}% ${pnl/100:>9.2f} {verdict:<14}")

    # Price bucket analysis
    subsection("Entry price bucket analysis")
    print(f"{'Price':>10} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} {'BE WR':>7} {'Gap':>8}")
    print("-" * 50)
    price_buckets = [(70, 80, "70-79c"), (80, 85, "80-84c"), (85, 90, "85-89c"),
                     (90, 95, "90-94c"), (95, 100, "95-99c")]
    for lo, hi, label in price_buckets:
        subset = [r for r in settled if lo <= r["market_price"] < hi]
        if subset:
            w = sum(1 for r in subset if r["market_result"] == "yes")
            wr_b = w / len(subset) * 100
            be = (lo + hi) / 2
            print(f"{label:>10} {len(subset):>4} {w:>3} {len(subset)-w:>3} "
                  f"{wr_b:>5.0f}% {be:>6.0f}% {wr_b - be:>+7.1f}pp")

    # Day-over-day trend
    subsection("Day-over-day performance")
    by_day: Dict[str, Dict] = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0})
    for r in settled:
        day = (r["evaluation_time"] or "")[:10]
        if not day:
            continue
        won = r["market_result"] == "yes"
        if won:
            by_day[day]["w"] += 1
        else:
            by_day[day]["l"] += 1
        by_day[day]["pnl"] += sim_pnl_taker(
            r["market_price"], r["position_size"] or 25, won)
    if by_day:
        print(f"  {'Date':<12} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} {'Sim PnL':>10}")
        print("  " + "-" * 42)
        for day in sorted(by_day):
            d = by_day[day]
            n = d["w"] + d["l"]
            wr = d["w"] / n * 100 if n > 0 else 0
            print(f"  {day:<12} {n:>4} {d['w']:>3} {d['l']:>3} "
                  f"{wr:>5.0f}% ${d['pnl']/100:>9.2f}")

    return {
        "total": total, "signals": signals, "settled": len(settled),
        "wins": len(wins), "losses": len(losses), "pending": len(pending),
        "total_pnl": total_pnl,
    }


# ── Section 2: Data Pipeline + Quality Audit ──────────────────────

def pipeline_audit(conn: sqlite3.Connection, since: str) -> None:
    section("2. DATA PIPELINE + QUALITY AUDIT")

    # Filter funnel
    subsection("Filter funnel")
    rows = conn.execute("""
        SELECT filter_stage, COUNT(*) AS n,
          AVG(market_price) AS avg_price,
          AVG(fee_adjusted_edge) AS avg_edge,
          AVG(seconds_to_close) AS avg_stc,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) AS losses,
          SUM(CASE WHEN market_result IS NULL THEN 1 ELSE 0 END) AS pending
        FROM evaluated_opportunities WHERE product_type='hourly'
          AND evaluation_time >= ?
        GROUP BY filter_stage ORDER BY n DESC
    """, (since,)).fetchall()

    print(f"  {'Stage':<28} {'N':>5} {'Avg P':>7} {'Avg Edge':>10} {'Avg STC':>8} "
          f"{'W':>4} {'L':>4} {'Pend':>5} {'WR':>6}")
    print("  " + "-" * 85)
    for r in rows:
        total_s = (r["wins"] or 0) + (r["losses"] or 0)
        wr = f"{r['wins']/total_s*100:.1f}%" if total_s > 0 else "—"
        print(f"  {r['filter_stage']:<28} {r['n']:>5} {r['avg_price'] or 0:>6.1f}c "
              f"{r['avg_edge'] or 0:>+9.4f} {r['avg_stc'] or 0:>7.0f}s "
              f"{r['wins'] or 0:>4} {r['losses'] or 0:>4} {r['pending'] or 0:>5} {wr:>6}")

    # Per-series coverage
    subsection("Coverage by market series")
    series_rows = conn.execute("""
        SELECT
          CASE
            WHEN ticker LIKE 'KXBTCD%%' THEN 'KXBTCD'
            WHEN ticker LIKE 'KXETHD%%' THEN 'KXETHD'
            WHEN ticker LIKE 'KXSOLD%%' THEN 'KXSOLD'
            WHEN ticker LIKE 'KXXRPD%%' THEN 'KXXRPD'
            ELSE 'OTHER'
          END AS series,
          COUNT(*) AS n,
          SUM(CASE WHEN filter_stage='hourly_observation' THEN 1 ELSE 0 END) AS signals,
          MIN(evaluation_time) AS first_t,
          MAX(evaluation_time) AS last_t
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND evaluation_time >= ?
        GROUP BY series ORDER BY n DESC
    """, (since,)).fetchall()
    print(f"  {'Series':<10} {'Evals':>6} {'Signals':>8} {'First':>18} {'Last':>18}")
    print("  " + "-" * 65)
    for r in series_rows:
        first = (r["first_t"] or "")[:16]
        last = (r["last_t"] or "")[:16]
        print(f"  {r['series']:<10} {r['n']:>6} {r['signals']:>8} {first:>18} {last:>18}")

    # Duplicate detection
    subsection("Duplicate entry check")
    dup_row = conn.execute("""
        SELECT COUNT(*) AS total,
          COUNT(DISTINCT ticker || '|' || evaluation_time) AS distinct_keys
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND evaluation_time >= ?
    """, (since,)).fetchone()
    dup_total = dup_row["total"] or 0
    dup_distinct = dup_row["distinct_keys"] or 0
    if dup_total > dup_distinct:
        print(f"  WARNING: {dup_total - dup_distinct} potential duplicates "
              f"({dup_total} total, {dup_distinct} distinct)")
    else:
        print(f"  No duplicates ({dup_total} entries, all distinct)")

    # Gap / outage detection
    subsection("Evaluation gap detection (>60 min)")
    times = conn.execute("""
        SELECT evaluation_time FROM evaluated_opportunities
        WHERE product_type='hourly' AND evaluation_time >= ?
        ORDER BY evaluation_time
    """, (since,)).fetchall()
    t_list = []
    for r in times:
        try:
            t_list.append(datetime.fromisoformat(r["evaluation_time"].replace("Z", "")))
        except Exception:
            pass
    big_gaps = []
    if len(t_list) > 1:
        for i in range(1, len(t_list)):
            gap_s = (t_list[i] - t_list[i - 1]).total_seconds()
            if gap_s > 3600:
                big_gaps.append((t_list[i - 1], t_list[i], gap_s / 60))
        gaps_all = [(t_list[i] - t_list[i - 1]).total_seconds()
                    for i in range(1, len(t_list))]
        span_hrs = (t_list[-1] - t_list[0]).total_seconds() / 3600
        print(f"  Eval frequency: {len(times)} over {span_hrs:.1f} hrs "
              f"({len(times)/max(span_hrs,0.1):.1f}/hr)")
        print(f"  Gap avg: {sum(gaps_all)/len(gaps_all)/60:.1f}min | "
              f"max: {max(gaps_all)/60:.1f}min")
    if big_gaps:
        for start, end, mins in big_gaps:
            print(f"  GAP: {start.strftime('%m/%d %H:%M')} to "
                  f"{end.strftime('%m/%d %H:%M')} ({mins:.0f} min)")
    else:
        print("  No gaps > 60 min")

    # Stale quote detection
    subsection("Stale quote detection")
    stale_rows = conn.execute("""
        SELECT market_price, calibrated_prob
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND calibrated_prob IS NOT NULL AND evaluation_time >= ?
    """, (since,)).fetchall()
    if stale_rows:
        deltas = [abs(r["market_price"] / 100 - r["calibrated_prob"]) for r in stale_rows]
        avg_delta = sum(deltas) / len(deltas)
        exact_matches = sum(1 for d in deltas if d < 0.01)
        print(f"  Avg |market_price - calibrated_prob|: {avg_delta:.4f} ({avg_delta*100:.1f}pp)")
        print(f"  Entries where market == model (within 1c): {exact_matches}/{len(deltas)}")
        if exact_matches > len(deltas) * 0.5:
            print("  >>> WARNING: High market==model rate suggests stale quotes")
        else:
            print("  Stale quote risk: LOW")

    # Temperature data coverage
    subsection("Temperature data coverage")
    temp_row = conn.execute("""
        SELECT COUNT(*) AS total,
          SUM(CASE WHEN hourly_applied_temp_t IS NOT NULL THEN 1 ELSE 0 END) AS has_t,
          SUM(CASE WHEN hourly_pre_temp_prob IS NOT NULL THEN 1 ELSE 0 END) AS has_pre
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND evaluation_time >= ?
    """, (since,)).fetchone()
    t_total = temp_row["total"] or 1
    has_t = temp_row["has_t"] or 0
    print(f"  Observations with temp T: {has_t}/{t_total} ({has_t/t_total*100:.1f}%)")
    print(f"  Observations with pre-temp prob: {temp_row['has_pre'] or 0}/{t_total}")
    if has_t < t_total:
        print(f"  >>> WARNING: {t_total - has_t} entries missing temperature data")

    # Temperature coverage by filter stage
    temp_stages = conn.execute("""
        SELECT filter_stage, COUNT(*) AS n,
          SUM(CASE WHEN hourly_applied_temp_t IS NOT NULL THEN 1 ELSE 0 END) AS has_t
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND evaluation_time >= ?
          AND filter_stage != 'price_out_of_range'
        GROUP BY filter_stage ORDER BY n DESC
    """, (since,)).fetchall()
    if temp_stages:
        print(f"\n  Temperature coverage by post-POR stage:")
        for r in temp_stages:
            pct = (r["has_t"] or 0) / r["n"] * 100 if r["n"] > 0 else 0
            print(f"    {r['filter_stage']:<28} {r['has_t'] or 0}/{r['n']} ({pct:.0f}%)")

    # Sizing data coverage
    subsection("Sizing data coverage")
    size_row = conn.execute("""
        SELECT COUNT(*) AS total,
          SUM(CASE WHEN position_size > 0 THEN 1 ELSE 0 END) AS has_size
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND evaluation_time >= ?
    """, (since,)).fetchone()
    has_size = size_row["has_size"] or 0
    print(f"  Observations with size > 0: {has_size}/{t_total} ({has_size/t_total*100:.1f}%)")

    # Time-of-day analysis
    subsection("Time-of-day breakdown (signals only)")
    tod_rows = conn.execute("""
        SELECT evaluation_time, market_result, market_price
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND market_result IS NOT NULL AND evaluation_time >= ?
    """, (since,)).fetchall()
    by_hour: Dict[int, Dict] = defaultdict(lambda: {"w": 0, "l": 0, "prices": []})
    for r in tod_rows:
        try:
            h = int(r["evaluation_time"][11:13])
            if r["market_result"] == "yes":
                by_hour[h]["w"] += 1
            else:
                by_hour[h]["l"] += 1
            by_hour[h]["prices"].append(r["market_price"])
        except Exception:
            pass
    if by_hour:
        print(f"  {'Hour':>6} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} {'Avg P':>7}")
        print("  " + "-" * 35)
        for h in sorted(by_hour):
            d = by_hour[h]
            n = d["w"] + d["l"]
            wr = d["w"] / n * 100 if n > 0 else 0
            avg_p = sum(d["prices"]) / len(d["prices"]) if d["prices"] else 0
            print(f"  {h:02d}:00  {n:>4} {d['w']:>3} {d['l']:>3} {wr:>5.0f}% {avg_p:>6.0f}c")

    # Ask depth / liquidity proxy
    subsection("Liquidity proxy (ask_depth)")
    depth_row = conn.execute("""
        SELECT COUNT(*) AS total,
          SUM(CASE WHEN ask_depth IS NOT NULL AND ask_depth > 0 THEN 1 ELSE 0 END)
            AS has_depth,
          AVG(CASE WHEN ask_depth > 0 THEN ask_depth END) AS avg_depth,
          MIN(CASE WHEN ask_depth > 0 THEN ask_depth END) AS min_depth,
          MAX(CASE WHEN ask_depth > 0 THEN ask_depth END) AS max_depth
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND evaluation_time >= ?
    """, (since,)).fetchone()
    print(f"  ask_depth populated: {depth_row['has_depth'] or 0}/{depth_row['total']}")
    if depth_row["avg_depth"]:
        print(f"  avg: {depth_row['avg_depth']:.0f} contracts | "
              f"min: {depth_row['min_depth']:.0f} | "
              f"max: {depth_row['max_depth']:.0f}")
    print(f"  NOTE: Hourly is observation-only — no actual fills to analyze")

    # Per-window analysis
    subsection("Per-window analysis (signals per event)")
    window_rows = conn.execute("""
        SELECT event_ticker, COUNT(*) AS n,
          GROUP_CONCAT(DISTINCT asset) AS assets,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) AS losses
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND evaluation_time >= ?
        GROUP BY event_ticker ORDER BY n DESC LIMIT 15
    """, (since,)).fetchall()
    if window_rows:
        print(f"  {'Event':<35} {'N':>3} {'Assets':<20} {'W':>3} {'L':>3}")
        print("  " + "-" * 65)
        for r in window_rows:
            print(f"  {r['event_ticker']:<35} {r['n']:>3} "
                  f"{r['assets'] or '?':<20} {r['wins'] or 0:>3} {r['losses'] or 0:>3}")
        multi = [r for r in window_rows if ',' in (r['assets'] or '')]
        print(f"\n  Multi-asset windows: {len(multi)}/{len(window_rows)}")


# ── Section 3: Leak Analysis ─────────────────────────────────────

def leak_analysis(conn: sqlite3.Connection, since: str,
                  total_pnl_cache: int = 0) -> None:
    section("3. LEAK ANALYSIS")

    # Calibration overconfidence
    subsection("Leak 1: Calibration overconfidence")
    rows = conn.execute("""
        SELECT calibrated_prob, market_price, market_result
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND market_result IS NOT NULL AND evaluation_time >= ?
    """, (since,)).fetchall()

    if rows:
        avg_prob = sum(r["calibrated_prob"] for r in rows) / len(rows)
        actual_wr = sum(1 for r in rows if r["market_result"] == "yes") / len(rows)
        overconf = (avg_prob - actual_wr) * 100
        brier = sum((r["calibrated_prob"] - (1 if r["market_result"] == "yes" else 0)) ** 2
                     for r in rows) / len(rows)

        print(f"  Avg model prob:  {avg_prob:.4f} ({avg_prob*100:.1f}%)")
        print(f"  Actual win rate: {actual_wr:.4f} ({actual_wr*100:.1f}%)")
        print(f"  Overconfidence:  {overconf:+.1f}pp")
        print(f"  Brier score:     {brier:.4f}")
        if overconf > 5:
            print(f"  >>> MODEL IS OVERCONFIDENT")
        else:
            print(f"  >>> Calibration within range")

    # STC bucket leak
    subsection("Leak 2: STC bucket performance")
    stc_rows = conn.execute("""
        SELECT
          CASE
            WHEN seconds_to_close < 600 THEN '<10m'
            WHEN seconds_to_close < 900 THEN '10-15m'
            WHEN seconds_to_close < 1200 THEN '15-20m'
            WHEN seconds_to_close < 1800 THEN '20-30m'
            ELSE '30m+'
          END AS bucket,
          COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) AS losses,
          AVG(market_price) AS avg_price
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND market_result IS NOT NULL AND evaluation_time >= ?
        GROUP BY bucket ORDER BY MIN(seconds_to_close)
    """, (since,)).fetchall()
    print(f"  {'Bucket':<8} {'N':>4} {'WR':>6} {'BE':>5} {'Gap':>8} {'Verdict':<14}")
    print("  " + "-" * 50)
    for r in stc_rows:
        total_s = (r["wins"] or 0) + (r["losses"] or 0)
        wr = r["wins"] / total_s * 100 if total_s > 0 else 0
        avg_px = r["avg_price"] or 0
        avg_fee_est = taker_fee(int(round(avg_px)), 1)
        be = (avg_px + avg_fee_est) / 100.0 * 100
        gap = wr - be
        verdict = "PROFITABLE" if gap > 0 else "UNPROFITABLE"
        print(f"  {r['bucket']:<8} {total_s:>4} {wr:>5.1f}% {be:>4.0f}% "
              f"{gap:>+7.1f}pp {verdict:<14}")

    # Counterfactual: strategy_wait
    subsection("Leak 3: Counterfactual — strategy_wait")
    _counterfactual_stage(conn, since, "strategy_wait",
                          "Passed filters but rejected by strategy timing")

    # Counterfactual: zero_sizing
    subsection("Leak 4: Counterfactual — zero_sizing")
    _counterfactual_stage(conn, since, "zero_sizing",
                          "Passed filters but drawdown scaler killed sizing")

    # Counterfactual: insufficient_edge (near-miss vs far)
    subsection("Leak 5: Counterfactual — insufficient_edge")
    ie_rows = conn.execute("""
        SELECT market_price, market_result, fee_adjusted_edge, position_size
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='insufficient_edge'
          AND market_result IS NOT NULL AND evaluation_time >= ?
    """, (since,)).fetchall()
    if ie_rows:
        near = [r for r in ie_rows if r["fee_adjusted_edge"] is not None
                and r["fee_adjusted_edge"] > -0.02]
        far = [r for r in ie_rows if r["fee_adjusted_edge"] is not None
               and r["fee_adjusted_edge"] <= -0.02]
        for label, subset in [("Near-miss (edge > -2%)", near),
                              ("Far (edge <= -2%)", far)]:
            if subset:
                w = sum(1 for r in subset if r["market_result"] == "yes")
                avg_p = sum(r["market_price"] for r in subset) / len(subset)
                wr_s = w / len(subset) * 100
                pnl = sum(sim_pnl_taker(r["market_price"], 1,
                                         r["market_result"] == "yes") for r in subset)
                sized_pnl = sum(sim_pnl_taker(r["market_price"], r["position_size"] or 25,
                                               r["market_result"] == "yes") for r in subset)
                print(f"  {label}: N={len(subset)}, {w}W/{len(subset)-w}L, "
                      f"WR={wr_s:.1f}%, avg_price={avg_p:.1f}c, "
                      f"1c_pnl=${pnl/100:.2f}, sized_pnl=${sized_pnl/100:.2f}")
    else:
        print("  No settled insufficient_edge entries")

    # Counterfactual: hourly_timing_restricted
    subsection("Leak 6: Counterfactual — hourly_timing_restricted")
    _counterfactual_stage(conn, since, "hourly_timing_restricted",
                          "STC outside configured range")

    # Counterfactual: hourly_asset_excluded
    subsection("Leak 7: Counterfactual — hourly_asset_excluded")
    _counterfactual_stage(conn, since, "hourly_asset_excluded",
                          "Asset in excluded set")

    # Shadow calibration comparison
    subsection("Leak 8: Shadow calibration (no-blend) comparison")
    shadow_rows = conn.execute("""
        SELECT shadow_cal_prob, calibrated_prob, market_price, market_result
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND shadow_cal_prob > 0 AND market_result IS NOT NULL
          AND evaluation_time >= ?
    """, (since,)).fetchall()
    if shadow_rows:
        shadow_wr = sum(1 for r in shadow_rows if r["market_result"] == "yes") / len(shadow_rows)
        avg_shadow = sum(r["shadow_cal_prob"] for r in shadow_rows) / len(shadow_rows)
        avg_live = sum(r["calibrated_prob"] for r in shadow_rows) / len(shadow_rows)
        print(f"  Entries with shadow_cal data: {len(shadow_rows)}")
        print(f"  Avg shadow_cal_prob: {avg_shadow:.4f} ({avg_shadow*100:.1f}%)")
        print(f"  Avg live cal_prob:   {avg_live:.4f} ({avg_live*100:.1f}%)")
        print(f"  Actual WR (subset):  {shadow_wr:.4f} ({shadow_wr*100:.1f}%)")
        print(f"  Shadow overconf:     {(avg_shadow - shadow_wr)*100:+.1f}pp")
        print(f"  Live overconf:       {(avg_live - shadow_wr)*100:+.1f}pp")
        if avg_shadow > avg_live + 0.05:
            print("  >>> Shadow (no-blend) is MORE overconfident — blend is helping")
    else:
        print("  No shadow calibration data available")

    # Kelly sizing detail
    subsection("Leak 9: Kelly sizing detail (entries with size > 0)")
    sized_rows = conn.execute("""
        SELECT market_price, position_size, fee_adjusted_edge, market_result, asset
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND position_size > 0 AND market_result IS NOT NULL
          AND evaluation_time >= ?
    """, (since,)).fetchall()
    if sized_rows:
        print(f"  {'Asset':<5} {'Price':>5} {'Size':>5} {'Edge':>8} {'Result':>7} {'PnL':>9}")
        print("  " + "-" * 45)
        total_sized_pnl = 0
        for r in sized_rows:
            won = r["market_result"] == "yes"
            pnl = sim_pnl_taker(r["market_price"], r["position_size"], won)
            total_sized_pnl += pnl
            print(f"  {r['asset']:<5} {r['market_price']:>4}c {r['position_size']:>5} "
                  f"{r['fee_adjusted_edge']:>+7.4f} {r['market_result']:>7} "
                  f"${pnl/100:>8.2f}")
        print(f"\n  Total sized PnL: ${total_sized_pnl/100:.2f}")
    else:
        print("  No entries with position_size > 0 and settlement")

    # Calibration by probability bucket
    subsection("Leak 10: Calibration by probability bucket")
    cal_rows = conn.execute("""
        SELECT calibrated_prob, market_result
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND market_result IS NOT NULL AND evaluation_time >= ?
    """, (since,)).fetchall()
    if cal_rows:
        buckets = [(0.70, 0.75), (0.75, 0.80), (0.80, 0.85), (0.85, 0.90),
                   (0.90, 0.95), (0.95, 1.001)]
        print(f"  {'Bucket':<12} {'N':>4} {'W':>3} {'L':>3} {'WR':>7} "
              f"{'Predicted':>10} {'Gap':>8} {'Wilson 95%':>14}")
        print("  " + "-" * 65)
        for lo, hi in buckets:
            sub = [r for r in cal_rows if lo <= r["calibrated_prob"] < hi]
            if sub:
                w = sum(1 for r in sub if r["market_result"] == "yes")
                wr = w / len(sub) * 100
                avg_pred = sum(r["calibrated_prob"] for r in sub) / len(sub) * 100
                ci_lo, ci_hi = wilson_ci(w, len(sub))
                print(f"  {lo:.2f}-{hi:.2f}  {len(sub):>4} {w:>3} {len(sub)-w:>3} "
                      f"{wr:>6.1f}% {avg_pred:>9.1f}% {wr - avg_pred:>+7.1f}pp "
                      f"[{ci_lo*100:.0f}-{ci_hi*100:.0f}%]")
    else:
        print("  No settled observation data")

    # Market-only baseline comparison
    subsection("Leak 10b: Market-only baseline (fair_prob = market_price / 100)")
    mkt_rows = conn.execute("""
        SELECT calibrated_prob, market_price, market_result
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND market_result IS NOT NULL AND market_price IS NOT NULL
          AND evaluation_time >= ?
    """, (since,)).fetchall()
    if mkt_rows:
        mkt_brier = sum(
            (r["market_price"] / 100.0 - (1.0 if r["market_result"] == "yes" else 0.0)) ** 2
            for r in mkt_rows
        ) / len(mkt_rows)
        model_brier = sum(
            (r["calibrated_prob"] - (1.0 if r["market_result"] == "yes" else 0.0)) ** 2
            for r in mkt_rows
        ) / len(mkt_rows)
        delta = model_brier - mkt_brier
        print(f"  Market-only Brier:  {mkt_brier:.4f}  (n={len(mkt_rows)})")
        print(f"  EGARCH model Brier: {model_brier:.4f}")
        if delta > 0:
            print(f"  Delta: {delta:+.4f} — MARKET BEATS MODEL")
            print(f"  *** Market price alone is better calibrated than EGARCH pipeline")
        else:
            print(f"  Delta: {delta:+.4f} — MODEL BEATS MARKET")
    else:
        print("  No data for market-only comparison")

    # Correlated multi-loss windows
    subsection("Leak 11: Correlated multi-loss windows")
    window_loss_rows = conn.execute("""
        SELECT event_ticker,
          GROUP_CONCAT(asset) AS assets,
          GROUP_CONCAT(market_result) AS results,
          GROUP_CONCAT(CAST(position_size AS TEXT)) AS sizes,
          GROUP_CONCAT(CAST(market_price AS TEXT)) AS prices,
          COUNT(*) AS n,
          SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) AS losses,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS wins
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND market_result IS NOT NULL AND evaluation_time >= ?
        GROUP BY event_ticker
        HAVING losses > 1
        ORDER BY losses DESC
    """, (since,)).fetchall()
    if window_loss_rows:
        total_corr_loss = 0
        print(f"  {'Event':<30} {'N':>3} {'W':>2} {'L':>2} {'Assets':>7} {'Win PnL':>9}")
        print("  " + "-" * 58)
        for r in window_loss_rows:
            assets = r["assets"].split(",")
            results = r["results"].split(",")
            prices = [int(p) for p in r["prices"].split(",")]
            sizes = [int(s) if s != "None" else 25 for s in r["sizes"].split(",")]
            wpnl = 0
            for res, price, size in zip(results, prices, sizes):
                fee = taker_fee(price, size)
                if res == "yes":
                    wpnl += size * (100 - price) - fee
                else:
                    wpnl -= size * price + fee
            total_corr_loss += wpnl
            unique_assets = len(set(assets))
            print(f"  {r['event_ticker']:<30} {r['n']:>3} {r['wins'] or 0:>2} "
                  f"{r['losses']:>2} {unique_assets:>7} ${wpnl/100:>8.2f}")
        print(f"\n  Total correlated window loss: ${total_corr_loss/100:.2f}")
        if total_pnl_cache != 0:
            print(f"  As % of total loss: "
                  f"{abs(total_corr_loss)/abs(total_pnl_cache)*100:.0f}%"
                  if total_pnl_cache < 0 else "  (total PnL positive)")
    else:
        print("  No windows with 2+ losses")

    # Worst-asset deep dive (per-asset x STC cross-tab)
    subsection("Leak 12: Per-asset x STC cross-tabulation")
    all_obs = conn.execute("""
        SELECT asset, seconds_to_close, market_price, market_result,
               position_size, fee_adjusted_edge
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND market_result IS NOT NULL AND evaluation_time >= ?
    """, (since,)).fetchall()
    if all_obs:
        stc_bins = [("<10m", 0, 600), ("10-20m", 600, 1200), ("20-30m", 1200, 1800)]
        assets_seen = sorted(set(r["asset"] for r in all_obs))
        header = f"  {'Asset':<5}"
        for label, _, _ in stc_bins:
            header += f" | {label:^14}"
        header += " | {'Total':^14}"
        print(f"  {'Asset':<5}", end="")
        for label, _, _ in stc_bins:
            print(f"  | {label:^14}", end="")
        print(f"  | {'Total':^14}")
        print("  " + "-" * (7 + 18 * (len(stc_bins) + 1)))
        for asset in assets_seen:
            asset_rows = [r for r in all_obs if r["asset"] == asset]
            line = f"  {asset:<5}"
            for _, lo, hi in stc_bins:
                sub = [r for r in asset_rows
                       if lo <= (r["seconds_to_close"] or 0) < hi]
                if sub:
                    w = sum(1 for r in sub if r["market_result"] == "yes")
                    pnl = sum(sim_pnl_taker(r["market_price"],
                              r["position_size"] or 25,
                              r["market_result"] == "yes") for r in sub)
                    line += f"  | {w}W/{len(sub)-w}L ${pnl/100:>6.1f}"
                else:
                    line += f"  | {'—':^14}"
            # Total for asset
            w_total = sum(1 for r in asset_rows if r["market_result"] == "yes")
            pnl_total = sum(sim_pnl_taker(r["market_price"],
                            r["position_size"] or 25,
                            r["market_result"] == "yes") for r in asset_rows)
            line += f"  | {w_total}W/{len(asset_rows)-w_total}L ${pnl_total/100:>6.1f}"
            print(line)

        # Identify worst asset and deep dive
        worst_asset = None
        worst_pnl = 0
        for asset in assets_seen:
            asset_rows = [r for r in all_obs if r["asset"] == asset]
            pnl = sum(sim_pnl_taker(r["market_price"], r["position_size"] or 25,
                                     r["market_result"] == "yes") for r in asset_rows)
            if pnl < worst_pnl:
                worst_pnl = pnl
                worst_asset = asset
        if worst_asset:
            print(f"\n  Worst asset: {worst_asset} (${worst_pnl/100:.2f})")
            wa_rows = [r for r in all_obs if r["asset"] == worst_asset]
            wa_wins = [r for r in wa_rows if r["market_result"] == "yes"]
            wa_losses = [r for r in wa_rows if r["market_result"] == "no"]
            if wa_wins:
                avg_stc_w = sum(r["seconds_to_close"] or 0
                                for r in wa_wins) / len(wa_wins)
                avg_edge_w = sum(r["fee_adjusted_edge"] or 0
                                 for r in wa_wins) / len(wa_wins)
                print(f"  Wins ({len(wa_wins)}):  avg_stc={avg_stc_w:.0f}s "
                      f"avg_edge={avg_edge_w:.4f}")
            if wa_losses:
                avg_stc_l = sum(r["seconds_to_close"] or 0
                                 for r in wa_losses) / len(wa_losses)
                avg_edge_l = sum(r["fee_adjusted_edge"] or 0
                                  for r in wa_losses) / len(wa_losses)
                avg_size_l = sum(r["position_size"] or 25
                                  for r in wa_losses) / len(wa_losses)
                print(f"  Losses ({len(wa_losses)}): avg_stc={avg_stc_l:.0f}s "
                      f"avg_edge={avg_edge_l:.4f} avg_size={avg_size_l:.0f}")
    else:
        print("  No settled observation data")


def _counterfactual_stage(conn: sqlite3.Connection, since: str,
                          stage: str, description: str) -> None:
    """Print counterfactual analysis for a filter stage."""
    # Match stage with possible prefixes
    stage_filter = f"'hourly_{stage}'" if not stage.startswith("hourly_") else f"'{stage}'"
    stage_variants = [stage, f"hourly_{stage}"]
    placeholders = ",".join("?" for _ in stage_variants)
    params = stage_variants + [since]

    rows = conn.execute(f"""
        SELECT market_price, market_result, seconds_to_close, asset, position_size
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage IN ({placeholders})
          AND market_result IS NOT NULL AND evaluation_time >= ?
    """, params).fetchall()
    if rows:
        w = sum(1 for r in rows if r["market_result"] == "yes")
        l_count = len(rows) - w
        avg_p = sum(r["market_price"] for r in rows) / len(rows)
        avg_stc = sum(r["seconds_to_close"] or 0 for r in rows) / len(rows)
        pnl = sum(sim_pnl_taker(r["market_price"], 1,
                                 r["market_result"] == "yes") for r in rows)
        sized_pnl = sum(sim_pnl_taker(r["market_price"], r["position_size"] or 25,
                                       r["market_result"] == "yes") for r in rows)
        wr = w / len(rows) * 100
        print(f"  {description}")
        print(f"  N={len(rows)}, {w}W/{l_count}L, WR={wr:.1f}%, "
              f"avg_price={avg_p:.1f}c, avg_stc={avg_stc:.0f}s")
        print(f"  Simulated PnL (1-lot taker): ${pnl/100:.2f}")
        print(f"  Simulated PnL (sized taker): ${sized_pnl/100:.2f}")
        avg_cf_fee = sum(taker_fee(r["market_price"], 1) for r in rows) / len(rows)
        cf_be_wr = (avg_p + avg_cf_fee) / 100.0 * 100
        print(f"  Breakeven WR: {cf_be_wr:.1f}%, gap={wr - cf_be_wr:+.1f}pp")
        # Per-asset detail if multiple
        by_asset = defaultdict(lambda: {"w": 0, "l": 0})
        for r in rows:
            if r["market_result"] == "yes":
                by_asset[r["asset"]]["w"] += 1
            else:
                by_asset[r["asset"]]["l"] += 1
        if len(by_asset) > 1:
            for a in sorted(by_asset):
                d = by_asset[a]
                print(f"    {a}: {d['w']}W/{d['l']}L")
        verdict = "FILTER CORRECT" if pnl < 0 else "FILTER MAY BE TOO STRICT"
        print(f"  >>> {verdict} (counterfactual PnL {'negative' if pnl < 0 else 'positive'})")
    else:
        print(f"  No settled {stage} entries")


# ── Section 4: Config Sensitivity ─────────────────────────────────

def config_sensitivity(conn: sqlite3.Connection, since: str) -> None:
    section("4. CONFIG SENSITIVITY")

    # Temperature simulation
    subsection("P1: Temperature T simulation")
    temp_rows = conn.execute("""
        SELECT hourly_pre_temp_prob, market_price, market_result, position_size
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND hourly_pre_temp_prob IS NOT NULL AND market_result IS NOT NULL
          AND evaluation_time >= ?
    """, (since,)).fetchall()

    if len(temp_rows) >= 5:
        print(f"  {'T':>6} {'Trades':>7} {'W':>3} {'L':>3} {'WR':>6} {'1c PnL':>10} {'Sized PnL':>11} {'Brier':>7}")
        print("  " + "-" * 58)
        for t in [1.0, 1.2, 1.45, 1.8, 2.0, 2.5]:
            n_trades = 0
            n_wins = 0
            pnl_1c = 0
            pnl_sized = 0
            brier_sum = 0
            for r in temp_rows:
                pre = r["hourly_pre_temp_prob"]
                if 0 < pre < 1:
                    logit_p = math.log(pre / (1 - pre))
                    scaled = 1 / (1 + math.exp(-logit_p / t))
                else:
                    scaled = pre
                p = r["market_price"]
                fee_pct = taker_fee(p, 1) / 100.0  # taker fee as fraction
                edge = scaled - (p / 100) - fee_pct
                outcome = 1 if r["market_result"] == "yes" else 0
                brier_sum += (scaled - outcome) ** 2
                if edge > 0.0025:  # current MIN_EDGE_PCT
                    n_trades += 1
                    size = r["position_size"] or 25
                    won = r["market_result"] == "yes"
                    if won:
                        n_wins += 1
                    pnl_1c += sim_pnl_taker(p, 1, won)
                    pnl_sized += sim_pnl_taker(p, size, won)
            wr = n_wins / n_trades * 100 if n_trades > 0 else 0
            brier = brier_sum / len(temp_rows)
            print(f"  {t:>5.2f} {n_trades:>7} {n_wins:>3} {n_trades - n_wins:>3} "
                  f"{wr:>5.0f}% ${pnl_1c/100:>9.2f} ${pnl_sized/100:>10.2f} {brier:>6.4f}")
    else:
        print(f"  Only {len(temp_rows)} entries with pre-temp data — "
              f"insufficient for simulation")
        print(f"  >>> Need 50+ entries. Accumulating...")

    # STC range sensitivity
    subsection("P2: STC range sensitivity")
    print(f"  {'Max STC':>9} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} {'1c PnL':>10} {'Sized PnL':>11}")
    print("  " + "-" * 52)
    for max_stc in [300, 600, 900, 1200, 1500, 1800]:
        rows2 = conn.execute("""
            SELECT market_price, market_result, position_size
            FROM evaluated_opportunities
            WHERE product_type='hourly' AND filter_stage='hourly_observation'
              AND market_result IS NOT NULL AND seconds_to_close <= ?
              AND evaluation_time >= ?
        """, (max_stc, since)).fetchall()
        if rows2:
            w = sum(1 for r in rows2 if r["market_result"] == "yes")
            pnl_1c = sum(sim_pnl_taker(r["market_price"], 1,
                                        r["market_result"] == "yes") for r in rows2)
            pnl_sized = sum(sim_pnl_taker(r["market_price"], r["position_size"] or 25,
                                           r["market_result"] == "yes") for r in rows2)
            print(f"  {max_stc:>8}s {len(rows2):>4} {w:>3} {len(rows2)-w:>3} "
                  f"{w/len(rows2)*100:>5.0f}% ${pnl_1c/100:>9.2f} ${pnl_sized/100:>10.2f}")
        else:
            print(f"  {max_stc:>8}s    0   —")

    # STC Fisher test: <600 vs >=600
    stc_lo = conn.execute("""
        SELECT SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
               SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) AS l
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND market_result IS NOT NULL AND seconds_to_close < 600
          AND evaluation_time >= ?
    """, (since,)).fetchone()
    stc_hi = conn.execute("""
        SELECT SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
               SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) AS l
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND market_result IS NOT NULL AND seconds_to_close >= 600
          AND evaluation_time >= ?
    """, (since,)).fetchone()
    a, b = (stc_lo["w"] or 0), (stc_lo["l"] or 0)
    c_, d_ = (stc_hi["w"] or 0), (stc_hi["l"] or 0)
    if a + b > 0 and c_ + d_ > 0:
        p_val = fisher_exact_2x2(a, b, c_, d_)
        print(f"\n  Fisher exact (STC<600 vs >=600): p={p_val:.4f} "
              f"({'significant' if p_val < 0.05 else 'NOT significant'} at 0.05)")
        print(f"    <600s: {a}W/{b}L = {a/(a+b)*100:.0f}%  |  "
              f">=600s: {c_}W/{d_}L = {c_/(c_+d_)*100:.0f}%")

    # Min entry price sensitivity
    subsection("P3: Min entry price sensitivity")
    print(f"  {'Min Price':>10} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} {'1c PnL':>10} {'Sized PnL':>11} {'Avg P':>7}")
    print("  " + "-" * 60)
    for min_p in [70, 75, 80, 85, 87, 90, 92]:
        rows3 = conn.execute("""
            SELECT market_price, market_result, position_size
            FROM evaluated_opportunities
            WHERE product_type='hourly' AND filter_stage='hourly_observation'
              AND market_result IS NOT NULL AND market_price >= ?
              AND evaluation_time >= ?
        """, (min_p, since)).fetchall()
        if rows3:
            w = sum(1 for r in rows3 if r["market_result"] == "yes")
            pnl_1c = sum(sim_pnl_taker(r["market_price"], 1,
                                        r["market_result"] == "yes") for r in rows3)
            pnl_sized = sum(sim_pnl_taker(r["market_price"], r["position_size"] or 25,
                                           r["market_result"] == "yes") for r in rows3)
            avg_p = sum(r["market_price"] for r in rows3) / len(rows3)
            print(f"  {min_p:>9}c {len(rows3):>4} {w:>3} {len(rows3)-w:>3} "
                  f"{w/len(rows3)*100:>5.0f}% ${pnl_1c/100:>9.2f} ${pnl_sized/100:>10.2f} {avg_p:>6.0f}c")

    # Edge threshold sensitivity
    subsection("P4: Edge threshold sensitivity")
    all_edge_rows = conn.execute("""
        SELECT fee_adjusted_edge, market_price, market_result, position_size
        FROM evaluated_opportunities
        WHERE product_type='hourly'
          AND filter_stage IN ('hourly_observation', 'insufficient_edge')
          AND market_result IS NOT NULL AND fee_adjusted_edge IS NOT NULL
          AND evaluation_time >= ?
    """, (since,)).fetchall()
    print(f"  {'Min Edge':>10} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} {'1c PnL':>10} {'Sized PnL':>11} {'Avg P':>7}")
    print("  " + "-" * 60)
    for min_edge in [0.0, 0.005, 0.007, 0.010, 0.015, 0.020, 0.030]:
        subset = [r for r in all_edge_rows if r["fee_adjusted_edge"] >= min_edge]
        if subset:
            w = sum(1 for r in subset if r["market_result"] == "yes")
            pnl_1c = sum(sim_pnl_taker(r["market_price"], 1,
                                        r["market_result"] == "yes") for r in subset)
            pnl_sized = sum(sim_pnl_taker(r["market_price"], r["position_size"] or 25,
                                           r["market_result"] == "yes") for r in subset)
            avg_p = sum(r["market_price"] for r in subset) / len(subset)
            print(f"  {min_edge:>9.3f} {len(subset):>4} {w:>3} {len(subset)-w:>3} "
                  f"{w/len(subset)*100:>5.0f}% ${pnl_1c/100:>9.2f} ${pnl_sized/100:>10.2f} {avg_p:>6.0f}c")

    # Edge Fisher test: >=1.5% vs <1.5%
    edge_hi_rows = [r for r in all_edge_rows if r["fee_adjusted_edge"] >= 0.015]
    edge_lo_rows = [r for r in all_edge_rows if r["fee_adjusted_edge"] < 0.015]
    if edge_hi_rows and edge_lo_rows:
        a = sum(1 for r in edge_hi_rows if r["market_result"] == "yes")
        b = len(edge_hi_rows) - a
        c_ = sum(1 for r in edge_lo_rows if r["market_result"] == "yes")
        d_ = len(edge_lo_rows) - c_
        p_val = fisher_exact_2x2(a, b, c_, d_)
        print(f"\n  Fisher exact (edge>=1.5% vs <1.5%): p={p_val:.4f} "
              f"({'significant' if p_val < 0.05 else 'NOT significant'} at 0.05)")
        print(f"    >=1.5%: {a}W/{b}L = {a/(a+b)*100:.0f}%  |  "
              f"<1.5%: {c_}W/{d_}L = {c_/(c_+d_)*100:.0f}%")

    # Market blend (P5) — check shadow data availability
    subsection("P5: Market blend sensitivity (data availability)")
    blend_row = conn.execute("""
        SELECT COUNT(*) AS total,
          SUM(CASE WHEN shadow_cal_prob > 0 THEN 1 ELSE 0 END) AS has_shadow
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND evaluation_time >= ?
    """, (since,)).fetchone()
    print(f"  Entries with shadow_cal (no-blend) data: "
          f"{blend_row['has_shadow'] or 0}/{blend_row['total']}")

    # Shadow blend=0.50 comparison if column exists
    has_b50 = has_column(conn, "evaluated_opportunities", "hourly_shadow_blend_50")
    if has_b50:
        b50_rows = conn.execute("""
            SELECT calibrated_prob, hourly_shadow_blend_50, market_result
            FROM evaluated_opportunities
            WHERE product_type='hourly' AND filter_stage='hourly_observation'
              AND hourly_shadow_blend_50 IS NOT NULL AND market_result IS NOT NULL
              AND evaluation_time >= ?
        """, (since,)).fetchall()
        if len(b50_rows) >= 5:
            brier_live = sum((r["calibrated_prob"] -
                              (1 if r["market_result"] == "yes" else 0)) ** 2
                             for r in b50_rows) / len(b50_rows)
            brier_b50 = sum((r["hourly_shadow_blend_50"] -
                             (1 if r["market_result"] == "yes" else 0)) ** 2
                            for r in b50_rows) / len(b50_rows)
            print(f"  Shadow blend=0.50 data: {len(b50_rows)} settled entries")
            print(f"    Brier (live blend=0.40): {brier_live:.4f}")
            print(f"    Brier (shadow blend=0.50): {brier_b50:.4f}")
            better = "blend=0.50" if brier_b50 < brier_live else "blend=0.40"
            print(f"    >>> {better} better by {abs(brier_b50 - brier_live):.4f}")
        else:
            print(f"  Shadow blend=0.50 data: {len(b50_rows)} entries "
                  f"(need 5+ settled)")
    else:
        print(f"  >>> Need hourly_shadow_blend_50 column to compare 0.40 vs 0.50 blend")

    # Shadow T=2.0 Brier comparison if column exists
    subsection("P6: Shadow temperature T=2.0 Brier comparison")
    has_t2 = has_column(conn, "evaluated_opportunities", "hourly_shadow_temp_2_0")
    if has_t2:
        t2_rows = conn.execute("""
            SELECT calibrated_prob, hourly_shadow_temp_2_0, market_result
            FROM evaluated_opportunities
            WHERE product_type='hourly' AND filter_stage='hourly_observation'
              AND hourly_shadow_temp_2_0 IS NOT NULL AND market_result IS NOT NULL
              AND evaluation_time >= ?
        """, (since,)).fetchall()
        if len(t2_rows) >= 5:
            brier_live = sum((r["calibrated_prob"] -
                              (1 if r["market_result"] == "yes" else 0)) ** 2
                             for r in t2_rows) / len(t2_rows)
            brier_t2 = sum((r["hourly_shadow_temp_2_0"] -
                            (1 if r["market_result"] == "yes" else 0)) ** 2
                           for r in t2_rows) / len(t2_rows)
            print(f"  Shadow T=2.0 data: {len(t2_rows)} settled entries")
            print(f"    Brier (live T=1.45): {brier_live:.4f}")
            print(f"    Brier (shadow T=2.0): {brier_t2:.4f}")
            better = "T=2.0" if brier_t2 < brier_live else "T=1.45"
            print(f"    >>> {better} better by {abs(brier_t2 - brier_live):.4f}")
        else:
            print(f"  Shadow T=2.0 data: {len(t2_rows)} entries (need 5+ settled)")
    else:
        print(f"  >>> hourly_shadow_temp_2_0 column not found — "
              f"needs bot instrumentation")

    # ── Temperature × Blend Shadow Grid ──
    subsection("P7: Temperature × Blend Shadow Grid")
    grid_cols = {
        "hourly_shadow_temp_1_75": "T=1.75",
        "hourly_shadow_temp_3_0": "T=3.0",
        "hourly_shadow_blend_20": "W=0.20",
        "hourly_shadow_blend_30": "W=0.30",
        "hourly_shadow_blend_60": "W=0.60",
        "hourly_post_temp_prob": "post_temp",
    }
    available_grid = {col: label for col, label in grid_cols.items()
                      if has_column(conn, "evaluated_opportunities", col)}
    if not available_grid:
        print("  >>> No grid shadow columns found — needs bot v2 instrumentation")
    else:
        print(f"  Grid columns available: {list(available_grid.values())}")

        # Temperature grid: pre-blend comparison using post_temp_prob + market_price
        if "hourly_post_temp_prob" in available_grid:
            temp_blend_rows = conn.execute(f"""
                SELECT calibrated_prob, market_price, market_result,
                       hourly_shadow_temp_2_0, hourly_shadow_temp_1_0,
                       hourly_shadow_temp_2_5,
                       hourly_shadow_temp_1_75, hourly_shadow_temp_3_0,
                       hourly_shadow_blend_20, hourly_shadow_blend_30,
                       hourly_shadow_blend_50, hourly_shadow_blend_60,
                       hourly_post_temp_prob, hourly_pre_temp_prob
                FROM evaluated_opportunities
                WHERE product_type='hourly' AND filter_stage='hourly_observation'
                  AND hourly_post_temp_prob IS NOT NULL AND market_result IS NOT NULL
                  AND evaluation_time >= ?
            """, (since,)).fetchall()

            if len(temp_blend_rows) >= 10:
                n = len(temp_blend_rows)
                outcome = [(1 if r["market_result"] == "yes" else 0) for r in temp_blend_rows]

                # Brier for live system
                brier_live = sum((r["calibrated_prob"] - o) ** 2
                                 for r, o in zip(temp_blend_rows, outcome)) / n

                # Pre-blend temps (apply live W=0.40 blend offline)
                temp_labels = [
                    ("T=1.0", "hourly_shadow_temp_1_0"),
                    ("T=1.45", None),  # live — use hourly_post_temp_prob
                    ("T=1.75", "hourly_shadow_temp_1_75"),
                    ("T=2.0", "hourly_shadow_temp_2_0"),
                    ("T=2.5", "hourly_shadow_temp_2_5"),
                    ("T=3.0", "hourly_shadow_temp_3_0"),
                ]
                blend_labels = [
                    ("W=0.20", "hourly_shadow_blend_20"),
                    ("W=0.30", "hourly_shadow_blend_30"),
                    ("W=0.40", None),  # live — use calibrated_prob
                    ("W=0.50", "hourly_shadow_blend_50"),
                    ("W=0.60", "hourly_shadow_blend_60"),
                ]

                print(f"\n  Temperature Brier (pre-blend, then W=0.40 live blend applied offline):")
                print(f"  {'Temp':<8} {'Brier':>8}  {'vs live':>8}  n={n}")
                print(f"  {'-'*30}")
                for t_label, t_col in temp_labels:
                    brier_vals = []
                    for r, o in zip(temp_blend_rows, outcome):
                        if t_col is None:
                            # Live T: use post_temp_prob with live blend
                            pre_blend = r["hourly_post_temp_prob"]
                        else:
                            pre_blend = r[t_col]
                        if pre_blend is None:
                            continue
                        # Apply live W=0.40 blend
                        mkt = r["market_price"] / 100.0 if r["market_price"] else 0.5
                        blended = 0.60 * pre_blend + 0.40 * mkt
                        brier_vals.append((blended - o) ** 2)
                    if brier_vals:
                        b = sum(brier_vals) / len(brier_vals)
                        diff = b - brier_live
                        marker = " <<<" if b == min(b, brier_live) and diff < -0.001 else ""
                        print(f"  {t_label:<8} {b:>8.4f}  {diff:>+8.4f}{marker}")

                print(f"\n  Blend weight Brier (live T applied, shadow W):")
                print(f"  {'Blend':<8} {'Brier':>8}  {'vs live':>8}  n={n}")
                print(f"  {'-'*30}")
                for w_label, w_col in blend_labels:
                    brier_vals = []
                    for r, o in zip(temp_blend_rows, outcome):
                        if w_col is None:
                            prob = r["calibrated_prob"]
                        else:
                            prob = r[w_col]
                        if prob is None:
                            continue
                        brier_vals.append((prob - o) ** 2)
                    if brier_vals:
                        b = sum(brier_vals) / len(brier_vals)
                        diff = b - brier_live
                        marker = " <<<" if b == min(b, brier_live) and diff < -0.001 else ""
                        print(f"  {w_label:<8} {b:>8.4f}  {diff:>+8.4f}{marker}")

                # Full T×W grid (offline computation using post_temp_prob + market_price)
                print(f"\n  Full T×W Brier Grid (n={n}):")
                w_values = [0.20, 0.30, 0.40, 0.50, 0.60]
                print(f"  {'':>8}", end="")
                for w in w_values:
                    print(f"  W={w:.2f}", end="")
                print()
                print(f"  {'-'*50}")

                best_brier = 999.0
                best_combo = ""
                for t_label, t_col in temp_labels:
                    print(f"  {t_label:<8}", end="")
                    for w in w_values:
                        brier_vals = []
                        for r, o in zip(temp_blend_rows, outcome):
                            if t_col is None:
                                pre_blend = r["hourly_post_temp_prob"]
                            else:
                                pre_blend = r[t_col]
                            if pre_blend is None:
                                continue
                            mkt = r["market_price"] / 100.0 if r["market_price"] else 0.5
                            blended = (1 - w) * pre_blend + w * mkt
                            brier_vals.append((blended - o) ** 2)
                        if brier_vals:
                            b = sum(brier_vals) / len(brier_vals)
                            if b < best_brier:
                                best_brier = b
                                best_combo = f"{t_label}, W={w:.2f}"
                            print(f"  {b:.4f}", end="")
                        else:
                            print(f"     N/A", end="")
                    print()

                print(f"\n  >>> Best: {best_combo} (Brier={best_brier:.4f}) "
                      f"vs live {brier_live:.4f} "
                      f"(diff={best_brier - brier_live:+.4f})")
            else:
                print(f"  Grid data: {len(temp_blend_rows)} entries (need 10+ settled)")
        else:
            print("  >>> hourly_post_temp_prob column needed for T×W grid")


# ── Section 5: Calibration Grid Search ────────────────────────────

def calibration_grid_search(conn: sqlite3.Connection, since: str) -> dict:
    """Grid search over edge cap x min price to find optimal filter config.

    Returns dict with best config info for use by recommendations section.
    Uses both flat PnL (equal sizing) and Kelly-weighted PnL (position sizing)
    to evaluate configs. Fisher exact test for statistical significance.
    """
    section("5. CALIBRATION GRID SEARCH")

    # Load all observation signals with edge + outcome
    rows = conn.execute("""
        SELECT market_price, fee_adjusted_edge, market_result, calibrated_prob,
               asset, seconds_to_close, position_size
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND market_result IS NOT NULL AND fee_adjusted_edge IS NOT NULL
          AND market_price IS NOT NULL AND evaluation_time >= ?
    """, (since,)).fetchall()

    n_total = len(rows)
    if n_total < 10:
        print(f"  Only {n_total} settled signals — need 10+ for grid search.")
        return {"n_total": n_total, "best_config": None}

    total_wins = sum(1 for r in rows if r["market_result"] == "yes")
    total_losses = n_total - total_wins
    total_wr = total_wins / n_total * 100
    total_flat = sum(
        (100 - r["market_price"]) if r["market_result"] == "yes"
        else -r["market_price"] for r in rows
    )

    # Date range for daily rate
    ts = conn.execute("""
        SELECT MIN(evaluation_time), MAX(evaluation_time)
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND evaluation_time >= ?
    """, (since,)).fetchone()
    if ts[0] and ts[1]:
        t1 = datetime.fromisoformat(ts[0].replace("Z", ""))
        t2 = datetime.fromisoformat(ts[1].replace("Z", ""))
        n_days = max((t2 - t1).total_seconds() / 86400, 0.5)
    else:
        n_days = 1.0

    print(f"  Total signals: {n_total} ({total_wins}W/{total_losses}L, "
          f"{total_wr:.1f}%) over {n_days:.1f} days")
    print(f"  Baseline flat PnL: ${total_flat/100:.2f} "
          f"(${total_flat/100/n_days:.2f}/day)")

    # ── Edge cap sweep ──
    subsection("Edge cap sweep (max fee-adjusted edge)")
    edge_caps = [0.005, 0.007, 0.008, 0.010, 0.012, 0.015, 0.020, 0.030,
                 0.050, 1.0]
    print(f"  {'MaxEdge':>8} {'N':>4} {'W':>4} {'L':>3} {'WR':>6} "
          f"{'FlatPnL':>9} {'SizedPnL':>10} {'$/day':>7}")
    print("  " + "-" * 58)
    for me in edge_caps:
        sub = [r for r in rows if r["fee_adjusted_edge"] <= me]
        if not sub:
            continue
        w = sum(1 for r in sub if r["market_result"] == "yes")
        l_ = len(sub) - w
        wr = w / len(sub) * 100
        pnl = sum(
            (100 - r["market_price"]) if r["market_result"] == "yes"
            else -r["market_price"] for r in sub
        )
        sized_pnl = sum(sim_pnl_taker(r["market_price"], r["position_size"] or 25,
                                       r["market_result"] == "yes") for r in sub)
        daily = pnl / 100 / n_days
        label = "all" if me >= 1.0 else f"{me*100:.2f}%"
        print(f"  {label:>8} {len(sub):>4} {w:>4} {l_:>3} {wr:>5.1f}% "
              f"${pnl/100:>8.2f} ${sized_pnl/100:>9.2f} ${daily:>6.2f}")

    # ── Min price sweep ──
    subsection("Min price sweep")
    prices = [50, 60, 65, 70, 75, 80, 85, 88, 90, 92]
    print(f"  {'MinPrice':>9} {'N':>4} {'W':>4} {'L':>3} {'WR':>6} "
          f"{'FlatPnL':>9} {'SizedPnL':>10} {'$/day':>7}")
    print("  " + "-" * 58)
    for mp in prices:
        sub = [r for r in rows if r["market_price"] >= mp]
        if not sub:
            continue
        w = sum(1 for r in sub if r["market_result"] == "yes")
        l_ = len(sub) - w
        wr = w / len(sub) * 100
        pnl = sum(
            (100 - r["market_price"]) if r["market_result"] == "yes"
            else -r["market_price"] for r in sub
        )
        sized_pnl = sum(sim_pnl_taker(r["market_price"], r["position_size"] or 25,
                                       r["market_result"] == "yes") for r in sub)
        daily = pnl / 100 / n_days
        print(f"  {mp:>8}c {len(sub):>4} {w:>4} {l_:>3} {wr:>5.1f}% "
              f"${pnl/100:>8.2f} ${sized_pnl/100:>9.2f} ${daily:>6.2f}")

    # ── Combined grid: edge cap x min price ──
    subsection("Combined grid: edge cap x min price")
    grid_results = []  # (max_edge, min_price, n, w, l, wr, flat_pnl, kelly_pnl, fixed_pnl)
    for me in edge_caps:
        for mp in prices:
            sub = [r for r in rows
                   if r["fee_adjusted_edge"] <= me and r["market_price"] >= mp]
            if len(sub) < 5:
                continue

            flat_pnl = 0
            fixed_pnl = 0  # Fixed 25-contract sizing (actual HOURLY_FIXED_CONTRACTS)
            wins = 0
            bankroll = 10000  # $100.00 in cents
            for r in sub:
                won = r["market_result"] == "yes"
                if won:
                    wins += 1
                flat_pnl += sim_pnl_taker(r["market_price"], 1, won)
                fixed_pnl += sim_pnl_taker(r["market_price"], 25, won)
                # Hypothetical Kelly-weighted simulation (NOT matching actual
                # fixed-sizing; included for comparison only)
                price = r["market_price"] / 100.0
                edge = r["fee_adjusted_edge"]
                b = (1 - price) / price  # payout ratio
                if b > 0:
                    kelly_f = min(max(0, edge / (1 - price)) * 0.25, 0.15)
                    bet = bankroll * kelly_f
                    if won:
                        bankroll += bet * b
                    else:
                        bankroll -= bet

            n = len(sub)
            wr = wins / n * 100
            kelly_net = bankroll - 10000
            grid_results.append((me, mp, n, wins, n - wins, wr,
                                 flat_pnl, kelly_net, fixed_pnl))

    # Sort by fixed-sizing PnL (matches actual HOURLY_FIXED_CONTRACTS=25)
    grid_results.sort(key=lambda x: -x[8])
    print(f"\n  Top 15 configs by fixed-sizing PnL (25 contracts, actual sizing):")
    print(f"  {'MaxEdge':>8} {'MinP':>5} {'N':>4} {'W':>4} {'L':>3} "
          f"{'WR':>6} {'Fixed25':>10} {'KellyHyp':>10}")
    print("  " + "-" * 65)
    for r in grid_results[:15]:
        me_label = "all" if r[0] >= 1.0 else f"{r[0]*100:.2f}%"
        print(f"  {me_label:>8} {r[1]:>4}c {r[2]:>4} {r[3]:>4} {r[4]:>3} "
              f"{r[5]:>5.1f}% ${r[8]/100:>9.2f} ${r[7]/100:>9.2f}")

    # Sort by flat PnL
    grid_results.sort(key=lambda x: -x[6])
    print(f"\n  Top 10 configs by flat PnL (equal 1-contract sizing):")
    print(f"  {'MaxEdge':>8} {'MinP':>5} {'N':>4} {'W':>4} {'L':>3} "
          f"{'WR':>6} {'FlatPnL':>9} {'$/day':>7}")
    print("  " + "-" * 55)
    for r in grid_results[:10]:
        me_label = "all" if r[0] >= 1.0 else f"{r[0]*100:.2f}%"
        daily = r[6] / 100 / n_days
        print(f"  {me_label:>8} {r[1]:>4}c {r[2]:>4} {r[3]:>4} {r[4]:>3} "
              f"{r[5]:>5.1f}% ${r[6]/100:>8.2f} ${daily:>6.2f}")

    # ── Fisher exact tests for top configs ──
    subsection("Statistical significance (Fisher exact test)")

    # Pick top 3 distinct configs by Kelly PnL
    grid_results.sort(key=lambda x: -x[7])
    seen = set()
    top_configs = []
    for r in grid_results:
        key = (r[0], r[1])
        if key not in seen and len(top_configs) < 5:
            seen.add(key)
            top_configs.append(r)

    best_result = None
    for r in top_configs:
        me, mp = r[0], r[1]
        me_label = "all" if me >= 1.0 else f"{me*100:.2f}%"
        label = f"edge<={me_label} P>={mp}c"

        inside = [x for x in rows
                  if x["fee_adjusted_edge"] <= me and x["market_price"] >= mp]
        outside = [x for x in rows
                   if not (x["fee_adjusted_edge"] <= me
                           and x["market_price"] >= mp)]

        a = sum(1 for x in inside if x["market_result"] == "yes")
        b_ = len(inside) - a
        c_ = sum(1 for x in outside if x["market_result"] == "yes")
        d_ = len(outside) - c_

        if a + b_ == 0 or c_ + d_ == 0:
            continue

        wr_in = a / (a + b_) * 100
        wr_out = c_ / (c_ + d_) * 100
        p_val = fisher_exact_2x2(a, b_, c_, d_)
        sig = ("***" if p_val < 0.001 else "**" if p_val < 0.01
               else "*" if p_val < 0.05 else "NS")
        daily_n = (a + b_) / n_days

        print(f"  {label}")
        print(f"    In:  {a}W/{b_}L ({wr_in:.1f}%)  "
              f"Out: {c_}W/{d_}L ({wr_out:.1f}%)")
        print(f"    Fisher p={p_val:.6f} {sig}  |  "
              f"{a+b_} signals = {daily_n:.1f}/day")
        print()

        if best_result is None or r[7] > best_result["kelly_pnl"]:
            best_result = {
                "max_edge": me,
                "min_price": mp,
                "n": a + b_,
                "wins": a,
                "losses": b_,
                "win_rate": round(wr_in, 1),
                "flat_pnl": r[6],
                "kelly_pnl": r[7],
                "fisher_p": round(p_val, 6),
                "signals_per_day": round(daily_n, 1),
            }

    # ── Loss analysis ──
    subsection("Loss pattern analysis")
    losses = [r for r in rows if r["market_result"] == "no"]
    if losses:
        loss_edges = [r["fee_adjusted_edge"] * 100 for r in losses]
        loss_prices = [r["market_price"] for r in losses]
        print(f"  Total losses: {len(losses)}")
        print(f"  Loss price range: {min(loss_prices)}-{max(loss_prices)}c "
              f"(avg {sum(loss_prices)/len(loss_prices):.0f}c)")
        print(f"  Loss edge range: {min(loss_edges):.2f}%-{max(loss_edges):.2f}% "
              f"(avg {sum(loss_edges)/len(loss_edges):.2f}%)")
        print(f"  Total loss cost: ${sum(loss_prices)/100:.2f}")

        # Where do losses cluster?
        hi_edge_losses = sum(1 for r in losses
                             if r["fee_adjusted_edge"] > 0.008)
        lo_price_losses = sum(1 for r in losses if r["market_price"] < 70)
        both = sum(1 for r in losses
                   if r["fee_adjusted_edge"] > 0.008
                   or r["market_price"] < 70)
        print(f"\n  Losses with edge>0.80%: {hi_edge_losses}/{len(losses)}")
        print(f"  Losses with price<70c: {lo_price_losses}/{len(losses)}")
        print(f"  Losses with either: {both}/{len(losses)}")

        if best_result:
            me_best = best_result["max_edge"]
            mp_best = best_result["min_price"]
            inside_losses = sum(
                1 for r in losses
                if r["fee_adjusted_edge"] <= me_best
                and r["market_price"] >= mp_best
            )
            me_label = (f"{me_best*100:.2f}%"
                        if me_best < 1.0 else "all")
            print(f"  Losses inside best filter "
                  f"(edge<={me_label}, P>={mp_best}c): "
                  f"{inside_losses}/{len(losses)}")

    return {
        "n_total": n_total,
        "n_days": round(n_days, 1),
        "best_config": best_result,
    }


# ── Section 6: Data Sufficiency ───────────────────────────────────

def data_sufficiency(conn: sqlite3.Connection, since: str, stats: dict) -> None:
    section("6. DATA SUFFICIENCY AUDIT")

    subsection("Global readiness checks")
    checks = [
        ("Total observations >= 100", stats["signals"] >= 100,
         f"{stats['signals']}/100"),
        ("Settled observations >= 50", stats["settled"] >= 50,
         f"{stats['settled']}/50"),
        ("Temperature data on >= 90% of obs", False, "—"),
        ("Sizing data on >= 90% of obs", False, "—"),
        ("Per-asset: >= 20 settled each", False, "—"),
        ("Days of data >= 14", False, "—"),
        ("Positive simulated PnL", stats.get("total_pnl", 0) > 0,
         f"${stats.get('total_pnl', 0)/100:.2f}"),
    ]

    # Temperature coverage
    row = conn.execute("""
        SELECT COUNT(*) AS total,
          SUM(CASE WHEN hourly_applied_temp_t IS NOT NULL THEN 1 ELSE 0 END) AS has_t
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND evaluation_time >= ?
    """, (since,)).fetchone()
    total_obs = row["total"] or 1
    has_t = row["has_t"] or 0
    checks[2] = ("Temperature data on >= 90% of obs", has_t / total_obs >= 0.9,
                 f"{has_t}/{total_obs} ({has_t/total_obs*100:.0f}%)")

    # Sizing coverage
    row = conn.execute("""
        SELECT SUM(CASE WHEN position_size > 0 THEN 1 ELSE 0 END) AS has_size
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND evaluation_time >= ?
    """, (since,)).fetchone()
    has_size = row["has_size"] or 0
    checks[3] = ("Sizing data on >= 90% of obs", has_size / total_obs >= 0.9,
                 f"{has_size}/{total_obs} ({has_size/total_obs*100:.0f}%)")

    # Per-asset check
    asset_rows = conn.execute("""
        SELECT asset, COUNT(*) AS n FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND market_result IS NOT NULL AND evaluation_time >= ?
        GROUP BY asset
    """, (since,)).fetchall()
    min_n = min((r["n"] for r in asset_rows), default=0)
    asset_detail = ", ".join(f"{r['asset']}={r['n']}" for r in asset_rows)
    checks[4] = ("Per-asset: >= 20 settled each", min_n >= 20,
                 f"min={min_n} ({asset_detail})")

    # Timespan
    row = conn.execute("""
        SELECT MIN(evaluation_time) AS first_t, MAX(evaluation_time) AS last_t
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND evaluation_time >= ?
    """, (since,)).fetchone()
    if row["first_t"] and row["last_t"]:
        t1 = datetime.fromisoformat(row["first_t"].replace("Z", ""))
        t2 = datetime.fromisoformat(row["last_t"].replace("Z", ""))
        days = (t2 - t1).total_seconds() / 86400
    else:
        days = 0
    checks[5] = ("Days of data >= 14", days >= 14, f"{days:.1f} days")

    for desc, passed, detail in checks:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {desc}: {detail}")

    n_pass = sum(1 for _, p, _ in checks if p)
    print(f"\n  >>> {n_pass}/{len(checks)} checks passing")

    # Per-config data sufficiency
    subsection("Per-config data sufficiency")

    # Check shadow column availability for blend sufficiency
    blend_sample = "Column not found — cannot evaluate"
    if has_column(conn, "evaluated_opportunities", "hourly_shadow_blend_50"):
        b50_row = conn.execute("""
            SELECT COUNT(*) AS n FROM evaluated_opportunities
            WHERE product_type='hourly' AND filter_stage='hourly_observation'
              AND hourly_shadow_blend_50 IS NOT NULL
              AND market_result IS NOT NULL AND evaluation_time >= ?
        """, (since,)).fetchone()
        b50_n = b50_row["n"] or 0
        blend_sample = (f"{b50_n} settled entries with shadow data (need 50+)"
                        if b50_n < 50 else
                        f"{b50_n} settled entries with shadow data — SUFFICIENT")

    # Check shadow T=2.0 column for temp sufficiency
    t2_sample = "Column not found — cannot evaluate"
    if has_column(conn, "evaluated_opportunities", "hourly_shadow_temp_2_0"):
        t2_row = conn.execute("""
            SELECT COUNT(*) AS n FROM evaluated_opportunities
            WHERE product_type='hourly' AND filter_stage='hourly_observation'
              AND hourly_shadow_temp_2_0 IS NOT NULL
              AND market_result IS NOT NULL AND evaluation_time >= ?
        """, (since,)).fetchone()
        t2_n = t2_row["n"] or 0
        t2_sample = (f"{has_t} pre-temp + {t2_n} shadow T=2.0 entries (need 50+)"
                     if t2_n < 50 else
                     f"{has_t} pre-temp + {t2_n} shadow T=2.0 — SUFFICIENT")
    else:
        t2_sample = f"{has_t} pre-temp entries only (need 50+, shadow column missing)"

    configs = [
        {
            "name": "HOURLY_MAX_STC_ENTRY (600 vs 1800)",
            "sample": f"<600s: {stats.get('signals', 0)} total signals, see STC buckets",
            "confounders": "Time-of-day, asset mix, volatility regime",
        },
        {
            "name": "Hourly min edge floor (1.5% threshold)",
            "sample": "See edge threshold table in S4",
            "confounders": "Higher-edge entries have lower prices (correlated)",
        },
        {
            "name": "HOURLY_TEMPERATURE_T",
            "sample": t2_sample,
            "confounders": "Temperature interacts with blend and edge filter",
        },
        {
            "name": "HOURLY_MARKET_BLEND_W (0.40 vs 0.50)",
            "sample": blend_sample,
            "confounders": "Blend interacts with temperature and calibration",
        },
        {
            "name": "HOURLY_KELLY_FRACTION",
            "sample": f"{has_size} entries with sizing data",
            "confounders": "Sizing magnitude doesn't affect WR, only PnL scale",
        },
    ]

    for cfg in configs:
        print(f"\n  {cfg['name']}:")
        print(f"    Sample: {cfg['sample']}")
        print(f"    Confounders: {cfg['confounders']}")

    # Compute STC Fisher test for sufficiency assessment
    stc_lo = conn.execute("""
        SELECT SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
               SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) AS l
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND market_result IS NOT NULL AND seconds_to_close < 600
          AND evaluation_time >= ?
    """, (since,)).fetchone()
    stc_hi = conn.execute("""
        SELECT SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
               SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) AS l
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND market_result IS NOT NULL AND seconds_to_close >= 600
          AND evaluation_time >= ?
    """, (since,)).fetchone()
    a, b = (stc_lo["w"] or 0), (stc_lo["l"] or 0)
    c_, d_ = (stc_hi["w"] or 0), (stc_hi["l"] or 0)
    if a + b > 0 and c_ + d_ > 0:
        p_stc = fisher_exact_2x2(a, b, c_, d_)
        print(f"\n  Statistical tests:")
        print(f"    STC<600 vs >=600 Fisher p={p_stc:.4f} "
              f"({'sufficient' if p_stc < 0.10 else 'INSUFFICIENT'} at p<0.10)")

    # Edge threshold Fisher
    all_edge_rows = conn.execute("""
        SELECT fee_adjusted_edge, market_result
        FROM evaluated_opportunities
        WHERE product_type='hourly'
          AND filter_stage IN ('hourly_observation', 'insufficient_edge')
          AND market_result IS NOT NULL AND fee_adjusted_edge IS NOT NULL
          AND evaluation_time >= ?
    """, (since,)).fetchall()
    edge_hi = [r for r in all_edge_rows if r["fee_adjusted_edge"] >= 0.015]
    edge_lo = [r for r in all_edge_rows if r["fee_adjusted_edge"] < 0.015]
    if edge_hi and edge_lo:
        a = sum(1 for r in edge_hi if r["market_result"] == "yes")
        b = len(edge_hi) - a
        c_ = sum(1 for r in edge_lo if r["market_result"] == "yes")
        d_ = len(edge_lo) - c_
        p_edge = fisher_exact_2x2(a, b, c_, d_)
        print(f"    Edge>=1.5% vs <1.5% Fisher p={p_edge:.4f} "
              f"({'sufficient' if p_edge < 0.10 else 'INSUFFICIENT'} at p<0.10)")

    subsection("Missing instrumentation")
    missing = []
    if has_t < total_obs * 0.9:
        missing.append(("hourly_pre_temp_prob / hourly_applied_temp_t",
                        "Temperature data on observation entries",
                        "Accumulating post-deploy. ETA: 3-5 days for 50+ entries."))
    if not has_column(conn, "evaluated_opportunities", "hourly_shadow_temp_2_0"):
        missing.append(("hourly_shadow_temp_2_0",
                        "Shadow probability at T=2.0 for offline Brier comparison",
                        "Add to insert_evaluated_opportunity + scan loop. ~10 lines."))
    if not has_column(conn, "evaluated_opportunities", "hourly_shadow_blend_50"):
        missing.append(("hourly_shadow_blend_50",
                        "Shadow probability at MARKET_BLEND_W=0.50",
                        "Add to insert_evaluated_opportunity + scan loop. ~5 lines."))

    if missing:
        for col, purpose, action in missing:
            print(f"  Column: {col}")
            print(f"    Purpose: {purpose}")
            print(f"    Action: {action}")
            print()
    else:
        print("  All instrumentation columns present. Accumulating data.")


# ── Section 7: Recommendations ────────────────────────────────────

def recommendations(conn: sqlite3.Connection, since: str) -> None:
    section("7. RECOMMENDATIONS")

    # Gather data for data-driven recommendations
    # STC data
    stc_lo = conn.execute("""
        SELECT COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND market_result IS NOT NULL AND seconds_to_close < 600
          AND evaluation_time >= ?
    """, (since,)).fetchone()
    stc_all = conn.execute("""
        SELECT COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND market_result IS NOT NULL AND evaluation_time >= ?
    """, (since,)).fetchone()

    stc_lo_n = stc_lo["n"] or 0
    stc_lo_w = stc_lo["w"] or 0
    stc_all_n = stc_all["n"] or 0
    stc_all_w = stc_all["w"] or 0
    stc_hi_n = stc_all_n - stc_lo_n
    stc_hi_w = stc_all_w - stc_lo_w
    stc_lo_wr = stc_lo_w / stc_lo_n * 100 if stc_lo_n > 0 else 0
    stc_hi_wr = stc_hi_w / stc_hi_n * 100 if stc_hi_n > 0 else 0

    # Edge data
    edge_hi = conn.execute("""
        SELECT COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w
        FROM evaluated_opportunities
        WHERE product_type='hourly'
          AND filter_stage IN ('hourly_observation', 'insufficient_edge')
          AND market_result IS NOT NULL AND fee_adjusted_edge >= 0.015
          AND evaluation_time >= ?
    """, (since,)).fetchone()
    edge_lo = conn.execute("""
        SELECT COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w
        FROM evaluated_opportunities
        WHERE product_type='hourly'
          AND filter_stage IN ('hourly_observation', 'insufficient_edge')
          AND market_result IS NOT NULL AND fee_adjusted_edge < 0.015
          AND fee_adjusted_edge IS NOT NULL
          AND evaluation_time >= ?
    """, (since,)).fetchone()

    edge_hi_n = edge_hi["n"] or 0
    edge_hi_w = edge_hi["w"] or 0
    edge_lo_n = edge_lo["n"] or 0
    edge_lo_w = edge_lo["w"] or 0
    edge_hi_wr = edge_hi_w / edge_hi_n * 100 if edge_hi_n > 0 else 0
    edge_lo_wr = edge_lo_w / edge_lo_n * 100 if edge_lo_n > 0 else 0

    # Temp coverage (all-time since --since)
    temp_row = conn.execute("""
        SELECT COUNT(*) AS total,
          SUM(CASE WHEN hourly_applied_temp_t IS NOT NULL THEN 1 ELSE 0 END) AS has_t
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND evaluation_time >= ?
    """, (since,)).fetchone()
    temp_pct = (temp_row["has_t"] or 0) / max(temp_row["total"] or 1, 1) * 100

    # Recent instrumentation coverage (last 48h) — detects whether columns are actively populated
    recent_cov = conn.execute("""
        SELECT COUNT(*) AS total,
          SUM(CASE WHEN hourly_pre_temp_prob IS NOT NULL THEN 1 ELSE 0 END) AS has_temp,
          SUM(CASE WHEN hourly_shadow_temp_2_0 IS NOT NULL THEN 1 ELSE 0 END) AS has_shadow_t,
          SUM(CASE WHEN hourly_shadow_blend_50 IS NOT NULL THEN 1 ELSE 0 END) AS has_blend_50
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND evaluation_time >= datetime('now', '-48 hours')
    """).fetchone()
    recent_total = recent_cov["total"] or 0
    recent_temp_pct = (recent_cov["has_temp"] or 0) / max(recent_total, 1) * 100
    recent_shadow_pct = (recent_cov["has_shadow_t"] or 0) / max(recent_total, 1) * 100
    recent_blend_pct = (recent_cov["has_blend_50"] or 0) / max(recent_total, 1) * 100

    # Overconfidence
    cal_row = conn.execute("""
        SELECT AVG(calibrated_prob) AS avg_prob,
          AVG(CASE WHEN market_result='yes' THEN 1.0 ELSE 0.0 END) AS wr
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND market_result IS NOT NULL AND evaluation_time >= ?
    """, (since,)).fetchone()
    overconf = ((cal_row["avg_prob"] or 0) - (cal_row["wr"] or 0)) * 100

    recs = [
        ("R1", "CRITICAL",
         f"Tighten HOURLY_MAX_STC_ENTRY 1800 -> 600",
         f"<600s: {stc_lo_w}W/{stc_lo_n - stc_lo_w}L ({stc_lo_wr:.0f}% WR). "
         f">=600s: {stc_hi_w}W/{stc_hi_n - stc_hi_w}L ({stc_hi_wr:.0f}% WR).\n"
         f"   Only <600s bucket is profitable. EGARCH degrades at longer horizons.\n"
         f"   Risk: Signal volume drops ~{stc_lo_n}/{stc_all_n} "
         f"({stc_lo_n/max(stc_all_n,1)*100:.0f}% retained)."),

        ("R2", "HIGH",
         f"Set hourly min edge floor to 1.5%",
         f"Edge>=1.5%: {edge_hi_w}W/{edge_hi_n - edge_hi_w}L ({edge_hi_wr:.0f}% WR). "
         f"Edge<1.5%: {edge_lo_w}W/{edge_lo_n - edge_lo_w}L ({edge_lo_wr:.0f}% WR).\n"
         f"   Profitability crossover at ~1.5% fee-adjusted edge.\n"
         f"   Implementation: Add HOURLY_MIN_EDGE_PCT constant."),

    ]

    # R3: Shadow temperature columns — dynamic based on recent coverage
    if recent_shadow_pct < 90:
        recs.append(("R3", "HIGH",
         "Add multi-temperature shadow columns [instrumentation]",
         f"Temperature data: {temp_pct:.0f}% all-time, {recent_shadow_pct:.0f}% last 48h.\n"
         f"   Model is {overconf:+.1f}pp overconfident.\n"
         f"   Add shadow columns for T=1.0/2.0/2.5 — enables offline Brier comparison.\n"
         f"   Zero behavior change. Purely data collection."))
    else:
        recs.append(("R3", "RESOLVED",
         "Multi-temperature shadow columns — instrumented",
         f"Shadow temp columns: {recent_shadow_pct:.0f}% coverage last 48h (n={recent_total}).\n"
         f"   All-time coverage: {temp_pct:.0f}%. Model overconfidence: {overconf:+.1f}pp."))

    # R4: Temperature on candidate-dict insert paths — dynamic based on recent coverage
    if recent_temp_pct < 90:
        recs.append(("R4", "MEDIUM",
         "Fix candidate-dict temperature instrumentation",
         f"Recent hourly_pre_temp_prob coverage: {recent_temp_pct:.0f}% last 48h (n={recent_total}).\n"
         f"   Insert call sites may be missing hourly temperature columns.\n"
         f"   CRITICAL before promotion — needed for post-hoc calibration analysis."))
    else:
        recs.append(("R4", "RESOLVED",
         "Candidate-dict temperature instrumentation — complete",
         f"hourly_pre_temp_prob: {recent_temp_pct:.0f}% coverage last 48h (n={recent_total}).\n"
         f"   All insert paths are populating temperature data correctly."))

    # R5: Shadow blend column — dynamic based on recent coverage
    if recent_blend_pct < 90:
        recs.append(("R5", "MEDIUM",
         "Add hourly_shadow_blend_50 column [instrumentation]",
         f"Recent blend_50 coverage: {recent_blend_pct:.0f}% last 48h (n={recent_total}).\n"
         f"   Cannot compare MARKET_BLEND_W=0.40 vs 0.50 without shadow data.\n"
         f"   Zero behavior change. Purely data collection."))
    else:
        recs.append(("R5", "RESOLVED",
         "Shadow blend column — instrumented",
         f"hourly_shadow_blend_50: {recent_blend_pct:.0f}% coverage last 48h (n={recent_total})."))

    for label, severity, title, detail in recs:
        print(f"[{label}] [{severity}] {title}")
        print(f"   {detail}")
        print()


# ── Section 8: Validation Plan ────────────────────────────────────

def validation_plan(conn: sqlite3.Connection) -> None:
    section("8. VALIDATION PLAN")

    subsection("R1: STC 1800 -> 600")
    print("  Deploy: Change HOURLY_MAX_STC_ENTRY + market_config.py sync")
    print("  Monitor: 1 week minimum")
    print("  Query:")
    print("    SELECT COUNT(*),")
    print("      SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS wins,")
    print("      AVG(market_price) AS avg_price")
    print("    FROM evaluated_opportunities")
    print("    WHERE product_type='hourly' AND filter_stage='hourly_observation'")
    print("      AND market_result IS NOT NULL AND evaluation_time >= '[deploy_time]';")
    print("  PASS: WR > 85% AND n >= 30 AND simulated PnL > $0")
    print("  FAIL: WR < 75% OR n < 10/week -> widen to 900s")
    print("  Guardrail: Keep logging timing_restricted for counterfactual")

    subsection("R2: Edge floor 1.5%")
    print("  Deploy: Add HOURLY_MIN_EDGE_PCT=0.015, override get_min_edge() for hourly")
    print("  Monitor: 1 week minimum")
    print("  Query:")
    print("    SELECT filter_stage, COUNT(*),")
    print("      SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS wins")
    print("    FROM evaluated_opportunities")
    print("    WHERE product_type='hourly'")
    print("      AND filter_stage IN ('hourly_observation', 'insufficient_edge')")
    print("      AND fee_adjusted_edge BETWEEN 0.007 AND 0.015")
    print("      AND market_result IS NOT NULL AND evaluation_time >= '[deploy_time]';")
    print("  PASS: Newly-rejected (0.7-1.5%) entries WR < breakeven at avg price")
    print("  FAIL: Newly-rejected entries WR > breakeven -> lower floor to 1.0%")

    # R3/R4/R5: Dynamic based on actual DB coverage
    recent_cov = conn.execute("""
        SELECT COUNT(*) AS total,
          SUM(CASE WHEN hourly_pre_temp_prob IS NOT NULL THEN 1 ELSE 0 END) AS has_temp,
          SUM(CASE WHEN hourly_shadow_temp_2_0 IS NOT NULL THEN 1 ELSE 0 END) AS has_shadow_t,
          SUM(CASE WHEN hourly_shadow_blend_50 IS NOT NULL THEN 1 ELSE 0 END) AS has_blend_50
        FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation'
          AND evaluation_time >= datetime('now', '-48 hours')
    """).fetchone()
    _rt = recent_cov["total"] or 0
    _r_temp = (recent_cov["has_temp"] or 0) / max(_rt, 1) * 100
    _r_shadow = (recent_cov["has_shadow_t"] or 0) / max(_rt, 1) * 100
    _r_blend = (recent_cov["has_blend_50"] or 0) / max(_rt, 1) * 100

    subsection("R3: Multi-temperature shadow")
    if _r_shadow < 90:
        print(f"  Status: OPEN — {_r_shadow:.0f}% coverage last 48h (n={_rt})")
        print("  Deploy: Add 3 columns + compute in scan loop. Zero behavior change.")
        print("  Monitor: Until 50+ settled entries have temp data")
    else:
        print(f"  Status: RESOLVED — {_r_shadow:.0f}% coverage last 48h (n={_rt})")
    print("  Query:")
    print("    SELECT 'T=1.45' AS label,")
    print("      AVG((calibrated_prob - (CASE WHEN market_result='yes'")
    print("        THEN 1.0 ELSE 0.0 END))^2) AS brier")
    print("    FROM evaluated_opportunities")
    print("    WHERE product_type='hourly' AND filter_stage='hourly_observation'")
    print("      AND hourly_applied_temp_t IS NOT NULL AND market_result IS NOT NULL;")
    print("  PASS: One T achieves Brier < 0.10 AND positive simulated PnL")
    print("  FAIL: No T under 0.10 -> need fundamentally different calibration")

    subsection("R4: Candidate-dict temperature")
    if _r_temp < 90:
        print(f"  Status: OPEN — {_r_temp:.0f}% coverage last 48h (n={_rt})")
        print("  Deploy: Add hourly_pre_temp_prob + hourly_applied_temp_t to insert call sites")
        print("  Verify: grep hourly_pre_temp_prob bot/_impl.py | wc -l  (should be ~12+)")
    else:
        print(f"  Status: RESOLVED — {_r_temp:.0f}% coverage last 48h (n={_rt})")
    print("  No monitoring needed — it's a correctness fix")

    subsection("R5: Shadow blend column")
    if _r_blend < 90:
        print(f"  Status: OPEN — {_r_blend:.0f}% coverage last 48h (n={_rt})")
        print("  Deploy: Add column + compute inline. Zero behavior change.")
        print("  Monitor: Until 100+ settled entries have data")
    else:
        print(f"  Status: RESOLVED — {_r_blend:.0f}% coverage last 48h (n={_rt})")
    print("  Compare Brier score of blend=0.40 vs shadow_blend=0.50")

    subsection("Promotion criteria (ALL must pass)")
    print("  1. 200+ settled hourly_observation with temperature data")
    print("  2. Simulated PnL positive over full post-change dataset")
    print("  3. Brier score < 0.10")
    print("  4. WR > breakeven at avg entry price")
    print("  5. No single asset dragging aggregate PnL negative")
    _r4_status = "RESOLVED" if _r_temp >= 90 else "OPEN"
    print(f"  6. Temperature instrumentation complete ({_r4_status})")
    print("  7. Sequential testing: Wald SPRT confirms edge > 0 at 95% confidence")
    print("  8. Max drawdown < 20% of bankroll in simulation")


def alt_shadow_strategies(conn: sqlite3.Connection, since: str) -> None:
    """Report on hourly alt shadow strategies (MM + HAR-RV) from hourly_alt_shadow_signals."""
    section("ALT SHADOW STRATEGIES (MM + HAR-RV)")

    # Check if the table exists
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='hourly_alt_shadow_signals'"
    ).fetchall()]
    if not tables:
        print("  Table hourly_alt_shadow_signals not found — engine not yet deployed or no data.")
        return

    # ── Overall counts ──
    subsection("Signal counts")
    overview = conn.execute("""
        SELECT strategy,
               COUNT(*) AS total,
               SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) AS settled,
               SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN market_result IN ('yes','all_yes') THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN market_result IN ('no','all_no') THEN 1 ELSE 0 END) AS losses,
               SUM(shadow_pnl_cents) AS total_pnl,
               SUM(CASE
                   WHEN shadow_contracts > 0 THEN shadow_pnl_cents
                   WHEN market_result IN ('yes','all_yes') THEN (100 - market_price)
                   WHEN market_result IN ('no','all_no') THEN -market_price
                   ELSE 0
               END) AS cf_pnl_1c
        FROM hourly_alt_shadow_signals
        WHERE evaluation_time >= ?
        GROUP BY strategy
    """, (since,)).fetchall()
    if not overview:
        print("  No alt shadow signals found since", since)
        return

    print(f"  {'Strategy':<16} {'Total':>6} {'Settled':>8} {'Pending':>8} {'W':>5} {'L':>5} {'WR':>7} {'Sized PnL':>10} {'CF 1c PnL':>10}")
    print(f"  {'-'*86}")
    for r in overview:
        settled = r["settled"] or 0
        wins = r["wins"] or 0
        losses = r["losses"] or 0
        wr = wins / settled * 100 if settled > 0 else 0
        pnl = (r["total_pnl"] or 0) / 100
        cf_pnl = (r["cf_pnl_1c"] or 0) / 100
        print(f"  {r['strategy']:<16} {r['total']:>6} {settled:>8} {r['pending'] or 0:>8} "
              f"{wins:>5} {losses:>5} {wr:>6.1f}% ${pnl:>8.2f} ${cf_pnl:>8.2f}")

    # ── Per-asset breakdown ──
    subsection("Per-asset x strategy breakdown")
    asset_rows = conn.execute("""
        SELECT strategy, asset,
               COUNT(*) AS n,
               SUM(CASE WHEN market_result IN ('yes','all_yes') THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN market_result IN ('no','all_no') THEN 1 ELSE 0 END) AS losses,
               SUM(shadow_pnl_cents) AS pnl,
               SUM(CASE
                   WHEN shadow_contracts > 0 THEN shadow_pnl_cents
                   WHEN market_result IN ('yes','all_yes') THEN (100 - market_price)
                   WHEN market_result IN ('no','all_no') THEN -market_price
                   ELSE 0
               END) AS cf_pnl_1c,
               AVG(market_price) AS avg_price,
               AVG(seconds_to_close) AS avg_stc
        FROM hourly_alt_shadow_signals
        WHERE status='settled' AND evaluation_time >= ?
        GROUP BY strategy, asset ORDER BY strategy, asset
    """, (since,)).fetchall()
    if asset_rows:
        print(f"  {'Strategy':<16} {'Asset':>5} {'N':>5} {'W':>4} {'L':>4} {'WR':>7} {'Sized PnL':>10} {'CF 1c':>7} {'AvgPx':>6} {'AvgSTC':>7}")
        print(f"  {'-'*80}")
        for r in asset_rows:
            settled = (r["wins"] or 0) + (r["losses"] or 0)
            wr = (r["wins"] or 0) / settled * 100 if settled > 0 else 0
            pnl = (r["pnl"] or 0) / 100
            cf = (r["cf_pnl_1c"] or 0) / 100
            print(f"  {r['strategy']:<16} {r['asset']:>5} {r['n']:>5} {r['wins'] or 0:>4} "
                  f"{r['losses'] or 0:>4} {wr:>6.1f}% ${pnl:>8.2f} ${cf:>5.2f} {r['avg_price'] or 0:>5.0f} "
                  f"{r['avg_stc'] or 0:>6.0f}s")
    else:
        print("  No settled alt shadow signals yet.")

    # ── HAR-RV gate failure analysis ──
    subsection("HAR-RV gate failures (top reasons for abstention)")
    gate_rows = conn.execute("""
        SELECT gate_failures, COUNT(*) AS n
        FROM hourly_alt_shadow_signals
        WHERE strategy='harrv_shadow' AND gate_failures IS NOT NULL
          AND gate_failures != '' AND evaluation_time >= ?
        GROUP BY gate_failures ORDER BY n DESC LIMIT 10
    """, (since,)).fetchall()
    if gate_rows:
        for r in gate_rows:
            print(f"  {r['n']:>5}x  {r['gate_failures']}")
    else:
        print("  No HAR-RV gate failure data yet (needs return accumulation).")

    # ── HAR-RV return buffer status ──
    subsection("HAR-RV diagnostics")
    harrv_rows = conn.execute("""
        SELECT asset,
               MAX(n_returns_1h) AS max_n_returns,
               AVG(rv_1h) AS avg_rv1h,
               AVG(rv_forecast) AS avg_rv_forecast,
               AVG(temperature) AS avg_temp,
               COUNT(*) AS n_signals
        FROM hourly_alt_shadow_signals
        WHERE strategy='harrv_shadow' AND evaluation_time >= ?
        GROUP BY asset
    """, (since,)).fetchall()
    if harrv_rows:
        print(f"  {'Asset':>5} {'Signals':>8} {'MaxRets':>8} {'AvgRV1h':>10} {'AvgRVf':>10} {'AvgT':>6}")
        print(f"  {'-'*52}")
        for r in harrv_rows:
            print(f"  {r['asset']:>5} {r['n_signals']:>8} {r['max_n_returns'] or 0:>8} "
                  f"{r['avg_rv1h'] or 0:>10.6f} {r['avg_rv_forecast'] or 0:>10.6f} "
                  f"{r['avg_temp'] or 0:>5.2f}")
    else:
        print("  No HAR-RV signals yet — return buffer still accumulating.")

    # ── MM spread analysis ──
    subsection("Market-Making spread analysis")
    mm_rows = conn.execute("""
        SELECT asset,
               COUNT(*) AS n,
               AVG(spread) AS avg_spread,
               AVG(spread_buffer) AS avg_buffer,
               AVG(midpoint) AS avg_mid,
               SUM(mm_buy_filled) AS buy_fills,
               SUM(mm_sell_filled) AS sell_fills
        FROM hourly_alt_shadow_signals
        WHERE strategy='mm_shadow' AND evaluation_time >= ?
        GROUP BY asset
    """, (since,)).fetchall()
    if mm_rows:
        print(f"  {'Asset':>5} {'N':>5} {'AvgSprd':>8} {'AvgBuf':>7} {'AvgMid':>7} {'BuyFills':>9} {'SellFills':>10}")
        print(f"  {'-'*58}")
        for r in mm_rows:
            print(f"  {r['asset']:>5} {r['n']:>5} {r['avg_spread'] or 0:>7.1f} "
                  f"{r['avg_buffer'] or 0:>6.0f} {r['avg_mid'] or 0:>6.1f} "
                  f"{r['buy_fills'] or 0:>9} {r['sell_fills'] or 0:>10}")
    else:
        print("  No MM signals yet.")

    # ── EGARCH comparison (where both have data for same ticker) ──
    subsection("EGARCH vs alt shadow comparison (same tickers)")
    comparison = conn.execute("""
        SELECT s.strategy, s.asset,
               COUNT(*) AS n,
               AVG(s.final_prob) AS alt_prob,
               AVG(s.egarch_prob) AS egarch_prob,
               AVG(s.edge) AS alt_edge,
               AVG(s.egarch_edge) AS egarch_edge,
               SUM(CASE WHEN s.market_result IN ('yes','all_yes') THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN s.market_result IN ('no','all_no') THEN 1 ELSE 0 END) AS losses
        FROM hourly_alt_shadow_signals s
        WHERE s.status='settled' AND s.egarch_prob IS NOT NULL
          AND s.evaluation_time >= ?
        GROUP BY s.strategy, s.asset ORDER BY s.strategy, s.asset
    """, (since,)).fetchall()
    if comparison:
        print(f"  {'Strategy':<16} {'Asset':>5} {'N':>4} {'AltProb':>8} {'EGProb':>8} "
              f"{'AltEdge':>8} {'EGEdge':>8} {'WR':>7}")
        print(f"  {'-'*72}")
        for r in comparison:
            settled = (r["wins"] or 0) + (r["losses"] or 0)
            wr = (r["wins"] or 0) / settled * 100 if settled > 0 else 0
            print(f"  {r['strategy']:<16} {r['asset']:>5} {r['n']:>4} "
                  f"{r['alt_prob'] or 0:>7.3f} {r['egarch_prob'] or 0:>7.3f} "
                  f"{(r['alt_edge'] or 0)*100:>7.2f}% {(r['egarch_edge'] or 0)*100:>7.2f}% "
                  f"{wr:>6.1f}%")
    else:
        print("  No settled signals with EGARCH comparison data yet.")

    # ── Data sufficiency for alt shadow ──
    subsection("Alt shadow data sufficiency")
    total_row = conn.execute("""
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) AS settled,
               MIN(evaluation_time) AS first_eval,
               MAX(evaluation_time) AS last_eval
        FROM hourly_alt_shadow_signals
        WHERE evaluation_time >= ?
    """, (since,)).fetchone()
    total_n = total_row["n"] or 0
    settled_n = total_row["settled"] or 0
    if total_row["first_eval"] and total_row["last_eval"]:
        t1 = datetime.fromisoformat(total_row["first_eval"].replace("Z", ""))
        t2 = datetime.fromisoformat(total_row["last_eval"].replace("Z", ""))
        days = (t2 - t1).total_seconds() / 86400
    else:
        days = 0
    checks = [
        ("Total signals >= 50", total_n >= 50, f"{total_n}/50"),
        ("Settled signals >= 30", settled_n >= 30, f"{settled_n}/30"),
        ("Days of data >= 5", days >= 5, f"{days:.1f}/5"),
        ("HAR-RV producing signals", any(r["strategy"] == "harrv_shadow" for r in overview),
         "check above"),
        ("MM producing signals", any(r["strategy"] == "mm_shadow" for r in overview),
         "check above"),
    ]
    for desc, passed, detail in checks:
        status = "PASS" if passed else "WAIT"
        print(f"  [{status}] {desc}: {detail}")
    n_pass = sum(1 for _, p, _ in checks if p)
    print(f"\n  >>> {n_pass}/{len(checks)} checks passing")
    if n_pass < len(checks):
        print("  >>> Alt shadow still collecting data — check back in a few days")


def no_side_shadow_analysis(conn: sqlite3.Connection, since: str) -> None:
    """Report NO-side shadow analysis from hourly_alt_shadow_signals and evaluated_opportunities."""
    section("NO-SIDE SHADOW ANALYSIS")

    # ── Check column existence in hourly_alt_shadow_signals ──
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='hourly_alt_shadow_signals'"
    ).fetchall()]
    if not tables:
        print("  Table hourly_alt_shadow_signals not found — skipping NO-side analysis.")
        return

    has_no_contracts = has_column(conn, "hourly_alt_shadow_signals", "no_harrv_contracts")
    has_no_pnl = has_column(conn, "hourly_alt_shadow_signals", "no_harrv_pnl_cents")
    has_no_prob = has_column(conn, "hourly_alt_shadow_signals", "no_harrv_prob")
    has_no_edge = has_column(conn, "hourly_alt_shadow_signals", "no_harrv_fee_edge")
    has_no_gates = has_column(conn, "hourly_alt_shadow_signals", "no_harrv_gates_passed")
    has_no_gate_fail = has_column(conn, "hourly_alt_shadow_signals", "no_harrv_gate_failures")

    if not has_no_contracts:
        print("  NO-side columns not found in hourly_alt_shadow_signals — engine not yet deployed.")
        return

    # ── NO-side signal counts ──
    subsection("NO-side HAR-RV signal counts")
    total_row = conn.execute("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN no_harrv_contracts > 0 THEN 1 ELSE 0 END) AS sized,
               SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) AS settled,
               SUM(CASE WHEN status='settled' AND no_harrv_contracts > 0 THEN 1 ELSE 0 END) AS sized_settled
        FROM hourly_alt_shadow_signals
        WHERE strategy='harrv_shadow' AND evaluation_time >= ?
    """, (since,)).fetchone()
    total_n = total_row["total"] or 0
    sized_n = total_row["sized"] or 0
    settled_n = total_row["settled"] or 0
    sized_settled = total_row["sized_settled"] or 0
    print(f"  Total HAR-RV signals:           {total_n}")
    print(f"  NO-side sized (contracts > 0):  {sized_n}")
    print(f"  Settled:                        {settled_n}")
    print(f"  NO-side sized & settled:        {sized_settled}")

    # ── Settled NO-side performance ──
    subsection("Settled NO-side performance")
    if sized_settled > 0:
        perf = conn.execute("""
            SELECT
                SUM(CASE WHEN market_result IN ('no','all_no') THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN market_result IN ('yes','all_yes') THEN 1 ELSE 0 END) AS losses,
                SUM(no_harrv_pnl_cents) AS total_pnl
            FROM hourly_alt_shadow_signals
            WHERE strategy='harrv_shadow' AND status='settled'
              AND no_harrv_contracts > 0 AND evaluation_time >= ?
        """, (since,)).fetchone()
        wins = perf["wins"] or 0
        losses = perf["losses"] or 0
        pnl = (perf["total_pnl"] or 0) / 100.0
        wr = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
        lo, hi = wilson_ci(wins, wins + losses)
        print(f"  Settled NO-side: {wins}W / {losses}L  ({wr:.1f}% WR)")
        print(f"  Wilson 95% CI:   [{lo*100:.1f}%, {hi*100:.1f}%]")
        print(f"  Sim PnL:         ${pnl:.2f}")
    else:
        print("  No settled NO-side signals with contracts > 0 yet.")

    # ── Per-asset NO-side breakdown ──
    subsection("Per-asset NO-side breakdown")
    asset_rows = conn.execute("""
        SELECT asset,
               COUNT(*) AS n,
               SUM(CASE WHEN market_result IN ('no','all_no') THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN market_result IN ('yes','all_yes') THEN 1 ELSE 0 END) AS losses,
               SUM(no_harrv_pnl_cents) AS pnl,
               AVG(market_price) AS avg_price
        FROM hourly_alt_shadow_signals
        WHERE strategy='harrv_shadow' AND status='settled'
          AND no_harrv_contracts > 0 AND evaluation_time >= ?
        GROUP BY asset ORDER BY asset
    """, (since,)).fetchall()
    if asset_rows:
        print(f"  {'Asset':>5} {'N':>5} {'W':>4} {'L':>4} {'WR':>7} {'PnL':>10} {'AvgPx':>6}")
        print(f"  {'-'*48}")
        for r in asset_rows:
            wins = r["wins"] or 0
            losses = r["losses"] or 0
            n = wins + losses
            wr = wins / n * 100 if n > 0 else 0
            pnl = (r["pnl"] or 0) / 100.0
            print(f"  {r['asset']:>5} {r['n']:>5} {wins:>4} {losses:>4} "
                  f"{wr:>6.1f}% ${pnl:>8.2f} {r['avg_price'] or 0:>5.0f}")
    else:
        print("  No settled per-asset NO-side data yet.")

    # ── Gates passed: YES vs NO comparison ──
    if has_no_gates:
        subsection("Gates passed: YES-side vs NO-side")
        gate_cmp = conn.execute("""
            SELECT
                AVG(CASE WHEN gates_passed IS NOT NULL THEN gates_passed END) AS yes_avg_gates,
                AVG(CASE WHEN no_harrv_gates_passed IS NOT NULL THEN no_harrv_gates_passed END) AS no_avg_gates,
                SUM(CASE WHEN gates_passed > 0 THEN 1 ELSE 0 END) AS yes_gated,
                SUM(CASE WHEN no_harrv_gates_passed > 0 THEN 1 ELSE 0 END) AS no_gated,
                COUNT(*) AS total
            FROM hourly_alt_shadow_signals
            WHERE strategy='harrv_shadow' AND evaluation_time >= ?
        """, (since,)).fetchone()
        yes_avg = gate_cmp["yes_avg_gates"] or 0
        no_avg = gate_cmp["no_avg_gates"] or 0
        yes_gated = gate_cmp["yes_gated"] or 0
        no_gated = gate_cmp["no_gated"] or 0
        total = gate_cmp["total"] or 0
        print(f"  YES-side: avg gates passed = {yes_avg:.1f}, signals passing any gate = {yes_gated}/{total}")
        print(f"  NO-side:  avg gates passed = {no_avg:.1f}, signals passing any gate = {no_gated}/{total}")

    # ── NO-side gate failures ──
    if has_no_gate_fail:
        subsection("NO-side gate failures (top reasons)")
        gate_fail_rows = conn.execute("""
            SELECT no_harrv_gate_failures AS reason, COUNT(*) AS n
            FROM hourly_alt_shadow_signals
            WHERE strategy='harrv_shadow'
              AND no_harrv_gate_failures IS NOT NULL
              AND no_harrv_gate_failures != ''
              AND evaluation_time >= ?
            GROUP BY no_harrv_gate_failures ORDER BY n DESC LIMIT 10
        """, (since,)).fetchall()
        if gate_fail_rows:
            for r in gate_fail_rows:
                print(f"  {r['n']:>5}x  {r['reason']}")
        else:
            print("  No NO-side gate failure data yet.")

    # ── evaluated_opportunities NO-side check ──
    subsection("evaluated_opportunities NO-side entries")
    has_side_col = has_column(conn, "evaluated_opportunities", "side")
    if has_side_col:
        eo_rows = conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) AS settled,
                   SUM(CASE WHEN status='settled' AND market_result IN ('no','all_no') THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes') THEN 1 ELSE 0 END) AS losses
            FROM evaluated_opportunities
            WHERE side='no' AND product_type IN ('hourly') AND evaluation_time >= ?
        """, (since,)).fetchone()
        total_eo = eo_rows["total"] or 0
        settled_eo = eo_rows["settled"] or 0
        wins_eo = eo_rows["wins"] or 0
        losses_eo = eo_rows["losses"] or 0
        wr_eo = wins_eo / (wins_eo + losses_eo) * 100 if (wins_eo + losses_eo) > 0 else 0
        print(f"  Total NO-side evals (hourly):   {total_eo}")
        print(f"  Settled:                        {settled_eo}")
        print(f"  Wins (market=no):               {wins_eo}")
        print(f"  Losses (market=yes):            {losses_eo}")
        print(f"  Win rate:                       {wr_eo:.1f}%")

        # Per-asset from evaluated_opportunities
        eo_asset = conn.execute("""
            SELECT asset,
                   COUNT(*) AS n,
                   SUM(CASE WHEN market_result IN ('no','all_no') THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN market_result IN ('yes','all_yes') THEN 1 ELSE 0 END) AS losses
            FROM evaluated_opportunities
            WHERE side='no' AND product_type IN ('hourly')
              AND status='settled' AND evaluation_time >= ?
            GROUP BY asset ORDER BY asset
        """, (since,)).fetchall()
        if eo_asset:
            print(f"\n  {'Asset':>5} {'N':>5} {'W':>4} {'L':>4} {'WR':>7}")
            print(f"  {'-'*30}")
            for r in eo_asset:
                w = r["wins"] or 0
                l = r["losses"] or 0
                n = w + l
                wr = w / n * 100 if n > 0 else 0
                print(f"  {r['asset']:>5} {r['n']:>5} {w:>4} {l:>4} {wr:>6.1f}%")

        # Per filter_stage NO-side breakdown
        eo_stage = conn.execute("""
            SELECT filter_stage,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) AS settled,
                   SUM(CASE WHEN status='settled' AND market_result IN ('no','all_no') THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes') THEN 1 ELSE 0 END) AS losses,
                   COALESCE(SUM(CASE WHEN status='settled' THEN counterfactual_pnl END), 0) AS pnl_cents,
                   ROUND(AVG(market_price), 1) AS avg_price
            FROM evaluated_opportunities
            WHERE side='no' AND product_type IN ('hourly') AND evaluation_time >= ?
            GROUP BY filter_stage ORDER BY total DESC
        """, (since,)).fetchall()
        if eo_stage:
            print(f"\n  NO-side by filter stage:")
            print(f"  {'Stage':<35} {'N':>4} {'Sett':>5} {'W':>4} {'L':>4} {'WR':>6} {'PnL':>10} {'AvgP':>5}")
            print(f"  {'-'*75}")
            for r in eo_stage:
                s = r["settled"] or 0
                w = r["wins"] or 0
                l = r["losses"] or 0
                n = w + l
                wr_s = f"{w/n*100:.1f}%" if n > 0 else "n/a"
                pnl = r["pnl_cents"] or 0
                print(f"  {r['filter_stage']:<35} {r['total']:>4} {s:>5} "
                      f"{w:>4} {l:>4} {wr_s:>6} ${pnl/100:>8.2f} {r['avg_price']:>5}c")
    else:
        print("  Column 'side' not found in evaluated_opportunities — skipping.")


def cal_engine_pipeline(conn, since: str) -> None:
    """CalEngine observation pipeline: settled evals with raw_prob for hourly."""
    section("CALENGINE OBSERVATION PIPELINE")
    try:
        row = conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN raw_prob IS NOT NULL THEN 1 ELSE 0 END) AS with_raw_prob,
                   SUM(CASE WHEN status='settled' AND raw_prob IS NOT NULL THEN 1 ELSE 0 END) AS cal_eligible
            FROM evaluated_opportunities
            WHERE product_type='hourly' AND evaluation_time >= ?
        """, (since,)).fetchone()
        total = row["total"] or 0
        with_rp = row["with_raw_prob"] or 0
        eligible = row["cal_eligible"] or 0
        print(f"  Total hourly evals:       {total}")
        print(f"  With raw_prob:            {with_rp}")
        print(f"  Settled + raw_prob (cal):  {eligible}")
        print()
        if eligible > 0:
            print(f"  >>> {eligible} observations feeding hourly CalEngine")
        else:
            print("  >>> No CalEngine observations yet (settlement routing change may be recent)")
    except Exception as e:
        print(f"  ERROR: {e}")


# ── V2 Variant Comparison ────────────────────────────────────────

def v2_variant_comparison(conn: sqlite3.Connection, since: str):
    """Compare V1 (beta cal + 40% blend) vs V2 (temperature + no blend) performance."""
    section("V2 VARIANT COMPARISON (shadow_cal pipeline)")

    # Check if V2 data exists
    v2_count = conn.execute("""
        SELECT COUNT(*) AS n FROM evaluated_opportunities
        WHERE product_type='hourly' AND filter_stage='hourly_observation_v2'
          AND evaluation_time >= ?
    """, (since,)).fetchone()["n"] or 0

    if v2_count == 0:
        print("  No V2 variant data yet. V2 rows will appear after next deploy.")
        print("  V2 = shadow cal pipeline (temperature scaling + no market blend)")
        return

    # V1 and V2 side-by-side
    for label, stage in [("V1 (beta_cal + 40% blend)", "hourly_observation"),
                         ("V2 (temp_scale + no blend)", "hourly_observation_v2")]:
        rows = conn.execute("""
            SELECT evaluation_time, ticker, asset, market_price, calibrated_prob,
              fee_adjusted_edge, seconds_to_close, position_size, market_result
            FROM evaluated_opportunities
            WHERE product_type='hourly' AND filter_stage=?
              AND market_result IS NOT NULL AND evaluation_time >= ?
            ORDER BY evaluation_time
        """, (stage, since)).fetchall()

        settled = list(rows)
        wins = [r for r in settled if r["market_result"] == "yes"]
        losses = [r for r in settled if r["market_result"] == "no"]
        total_pnl = sum(sim_pnl_taker(r["market_price"], r["position_size"] or 25,
                                       r["market_result"] == "yes") for r in settled)
        avg_p = sum(r["market_price"] for r in settled) / len(settled) if settled else 0
        avg_prob = sum(r["calibrated_prob"] or 0 for r in settled) / len(settled) if settled else 0
        wr = len(wins) / len(settled) * 100 if settled else 0
        avg_tfee = sum(taker_fee(r["market_price"], 1) for r in settled) / len(settled) if settled else 0
        be_wr = (avg_p + avg_tfee) / 100.0 * 100  # taker fee breakeven WR
        brier = sum((r["calibrated_prob"] - (1 if r["market_result"] == "yes" else 0))**2
                     for r in settled) / len(settled) if settled else 0

        print(f"\n  --- {label} ---")
        print(f"  Settled: {len(settled)}, {len(wins)}W/{len(losses)}L, WR={wr:.1f}%")
        print(f"  Sim PnL (taker): ${total_pnl/100:.2f}")
        print(f"  Avg price: {avg_p:.1f}c, BE WR: {be_wr:.0f}%, Gap: {wr - be_wr:+.1f}pp")
        print(f"  Avg model prob: {avg_prob*100:.1f}%, Overconfidence: {avg_prob*100 - wr:+.1f}pp")
        print(f"  Brier: {brier:.4f}")

        # Per-asset
        by_asset = defaultdict(list)
        for r in settled:
            by_asset[r["asset"]].append(r)
        if by_asset:
            print(f"  {'Asset':<8} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} {'Sim PnL':>10} {'Avg P':>7}")
            print(f"  {'-'*50}")
            for asset in sorted(by_asset):
                ar = by_asset[asset]
                w = sum(1 for r in ar if r["market_result"] == "yes")
                pnl = sum(sim_pnl_taker(r["market_price"], r["position_size"] or 25,
                                         r["market_result"] == "yes") for r in ar)
                ap = sum(r["market_price"] for r in ar) / len(ar)
                print(f"  {asset:<8} {len(ar):>4} {w:>3} {len(ar)-w:>3} "
                      f"{w/len(ar)*100:>5.1f}% ${pnl/100:>9.2f} {ap:>6.1f}c")

    # Matched ticker comparison (V1 vs V2 on same tickers)
    matched = conn.execute("""
        SELECT v1.ticker, v1.asset, v1.market_price, v1.market_result,
               v1.calibrated_prob AS v1_prob, v1.fee_adjusted_edge AS v1_edge,
               v1.position_size AS v1_size,
               v2.calibrated_prob AS v2_prob, v2.fee_adjusted_edge AS v2_edge,
               v2.position_size AS v2_size
        FROM evaluated_opportunities v1
        JOIN evaluated_opportunities v2
          ON v1.ticker = v2.ticker
        WHERE v1.filter_stage='hourly_observation'
          AND v2.filter_stage='hourly_observation_v2'
          AND v1.product_type='hourly' AND v2.product_type='hourly'
          AND v1.market_result IS NOT NULL
          AND v1.evaluation_time >= ?
    """, (since,)).fetchall()

    if matched:
        subsection("Matched ticker comparison")
        v1_brier = sum((r["v1_prob"] - (1 if r["market_result"] == "yes" else 0))**2
                       for r in matched) / len(matched)
        v2_brier = sum((r["v2_prob"] - (1 if r["market_result"] == "yes" else 0))**2
                       for r in matched) / len(matched)
        v1_pnl = sum(sim_pnl_taker(r["market_price"], r["v1_size"] or 1,
                                    r["market_result"] == "yes") for r in matched)
        v2_pnl = sum(sim_pnl_taker(r["market_price"], r["v2_size"] or 1,
                                    r["market_result"] == "yes") for r in matched)
        v1_oc = sum(r["v1_prob"] for r in matched) / len(matched) * 100 - \
                sum(1 for r in matched if r["market_result"] == "yes") / len(matched) * 100
        v2_oc = sum(r["v2_prob"] for r in matched) / len(matched) * 100 - \
                sum(1 for r in matched if r["market_result"] == "yes") / len(matched) * 100
        print(f"  Matched tickers: {len(matched)}")
        print(f"  V1 Brier: {v1_brier:.4f}, Overconfidence: {v1_oc:+.1f}pp")
        print(f"  V2 Brier: {v2_brier:.4f}, Overconfidence: {v2_oc:+.1f}pp")
        delta = v1_brier - v2_brier
        winner = "V2" if delta > 0 else "V1"
        print(f"  Delta: {delta:+.4f} — {winner} wins by {abs(delta):.4f}")
        print(f"  V1 sized PnL: ${v1_pnl/100:.2f}, V2 sized PnL: ${v2_pnl/100:.2f}")

    # V2-only signals (V2 passed edge but V1 didn't — signals from insufficient_edge)
    v2_only = conn.execute("""
        SELECT COUNT(*) AS n,
          SUM(CASE WHEN v2.market_result='yes' THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN v2.market_result='no' THEN 1 ELSE 0 END) AS losses
        FROM evaluated_opportunities v2
        LEFT JOIN evaluated_opportunities v1
          ON v1.ticker = v2.ticker AND v1.filter_stage='hourly_observation'
          AND v1.product_type='hourly'
        WHERE v2.filter_stage='hourly_observation_v2'
          AND v2.product_type='hourly'
          AND v2.market_result IS NOT NULL
          AND v2.evaluation_time >= ?
          AND v1.id IS NULL
    """, (since,)).fetchone()

    v2_only_n = v2_only["n"] or 0
    if v2_only_n > 0:
        subsection("V2-only signals (V1 rejected as insufficient_edge)")
        v2w = v2_only["wins"] or 0
        v2l = v2_only["losses"] or 0
        print(f"  N={v2_only_n}, {v2w}W/{v2l}L, WR={v2w/v2_only_n*100:.1f}%")
        print(f"  These are signals where V2 found edge but V1 didn't")


# ── Main ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Hourly shadow mode audit")
    parser.add_argument("--db", default="state.db", help="Path to state.db")
    parser.add_argument("--since", default="2026-02-28T00:00:00",
                        help="Only analyze entries after this timestamp")
    parser.add_argument("--regime", default=None,
                        help="Set to 'auto' to auto-detect regime start from gaps")
    parser.add_argument("--json", default=None,
                        help="Write JSON artifact summary to this path")
    args = parser.parse_args()

    try:
        conn = connect_db(args.db)
    except Exception as e:
        print(f"ERROR: Cannot open DB at {args.db}: {e}")
        sys.exit(1)

    since = args.since
    if args.regime == "auto":
        since = detect_regime_start(conn)
        print(f"Auto-detected regime start: {since}")

    row = conn.execute("""
        SELECT COUNT(*) AS n FROM evaluated_opportunities
        WHERE product_type='hourly' AND evaluation_time >= ?
    """, (since,)).fetchone()
    if (row["n"] or 0) == 0:
        print(f"ERROR: No hourly evaluations found after {since}")
        sys.exit(1)

    stats = performance_summary(conn, since)
    pipeline_audit(conn, since)
    leak_analysis(conn, since, total_pnl_cache=stats.get("total_pnl", 0))
    config_sensitivity(conn, since)
    cal_grid = calibration_grid_search(conn, since)
    data_sufficiency(conn, since, stats)
    recommendations(conn, since)
    alt_shadow_strategies(conn, since)
    cal_engine_pipeline(conn, since)
    no_side_shadow_analysis(conn, since)
    v2_variant_comparison(conn, since)
    validation_plan(conn)

    # JSON artifact output
    if args.json:
        artifact = {
            "since": since,
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "total_evals": stats["total"],
            "signals": stats["signals"],
            "settled": stats["settled"],
            "wins": stats["wins"],
            "losses": stats["losses"],
            "pending": stats["pending"],
            "simulated_pnl_cents": stats["total_pnl"],
            "win_rate": round(stats["wins"] / max(stats["settled"], 1), 4),
            "avg_entry_price": None,
        }
        # Compute avg entry price from signals
        avg_row = conn.execute("""
            SELECT AVG(market_price) AS avg_p
            FROM evaluated_opportunities
            WHERE product_type='hourly' AND filter_stage='hourly_observation'
              AND market_result IS NOT NULL AND evaluation_time >= ?
        """, (since,)).fetchone()
        if avg_row["avg_p"]:
            artifact["avg_entry_price"] = round(avg_row["avg_p"], 1)

        # Per-asset summary
        asset_rows = conn.execute("""
            SELECT asset, COUNT(*) AS n,
              SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS wins,
              SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) AS losses
            FROM evaluated_opportunities
            WHERE product_type='hourly' AND filter_stage='hourly_observation'
              AND market_result IS NOT NULL AND evaluation_time >= ?
            GROUP BY asset
        """, (since,)).fetchall()
        artifact["by_asset"] = {
            r["asset"]: {"n": r["n"], "wins": r["wins"] or 0,
                         "losses": r["losses"] or 0}
            for r in asset_rows
        }

        if cal_grid.get("best_config"):
            artifact["calibration_grid"] = cal_grid

        try:
            with open(args.json, "w") as f:
                json.dump(artifact, f, indent=2)
            print(f"\nJSON artifact written to {args.json}")
        except Exception as e:
            print(f"\nERROR writing JSON: {e}")

    conn.close()
    print(f"\n{'=' * 70}")
    print(f"  Audit complete. Re-run weekly with fresh state.db.")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
