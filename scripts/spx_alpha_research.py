#!/usr/bin/env python3
"""
SPX Hourly Strategy Alpha Analyzer
Systematic research script to discover profitable configurations in SPX hourly
observation data. Rerunnable on updated data -- outputs standardized alpha report.

Reads from evaluated_opportunities (product_type='spx_hourly') and
rejected_opportunities (product_type='spx_hourly') in state.db.

Usage:
    python3 scripts/spx_alpha_research.py [--db /tmp/state.db] [--since 2026-03-02] [--json spx_alpha.json]
"""

import argparse
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple


# ============================================================================
#  Helpers & Fee Model (FINANCE category -- half of crypto)
# ============================================================================

def connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def maker_fee(price_cents: int, count: int = 1) -> int:
    """Maker fee for SPX (finance category): ceil(0.0175 * count * p * (1-p))."""
    p = price_cents / 100.0
    return math.ceil(0.0175 * count * p * (1 - p) * 100)


def taker_fee(price_cents: int, count: int = 1) -> int:
    """Taker fee for SPX: ceil(0.035 * count * p * (1-p))."""
    p = price_cents / 100.0
    return math.ceil(0.035 * count * p * (1 - p) * 100)


def breakeven_wr(price_cents: int) -> float:
    """Breakeven win rate at given price (maker fee)."""
    fee = maker_fee(price_cents)
    return (price_cents + fee) / 100.0


def sim_pnl_1lot(price_cents: int, won: bool, is_maker: bool = True) -> float:
    """Simulated PnL for 1 contract in dollars."""
    fee = maker_fee(price_cents) if is_maker else taker_fee(price_cents)
    if won:
        return (100 - price_cents - fee) / 100.0
    else:
        return -(price_cents + fee) / 100.0


def compute_pnl_cents(price: int, result: str, n_contracts: int = 1) -> int:
    """PnL in cents. result='yes' is a win."""
    fee = maker_fee(price)
    if result == "yes":
        per_contract = 100 - price - fee
    else:
        per_contract = -(price + fee)
    return per_contract * n_contracts


def wilson_ci(wins: int, total: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson score confidence interval."""
    if total == 0:
        return 0.0, 0.0
    p = wins / total
    denom = 1 + z**2 / total
    center = (p + z**2 / (2 * total)) / denom
    spread = z * math.sqrt((p * (1 - p) + z**2 / (4 * total)) / total) / denom
    return max(0, center - spread), min(1, center + spread)


def brier_score(probs: List[float], outcomes: List[float]) -> Optional[float]:
    if not probs:
        return None
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / len(probs)


def profit_factor(wins_pnl: float, losses_pnl: float) -> float:
    if losses_pnl == 0:
        return float("inf") if wins_pnl > 0 else 0
    return abs(wins_pnl / losses_pnl)


def significance_tag(n: int) -> str:
    if n < 10:
        return " *** VERY SMALL SAMPLE"
    if n < 20:
        return " ** NOT SIGNIFICANT"
    if n < 30:
        return " * SMALL SAMPLE"
    return ""


def safe_div(a, b, default=0.0):
    return a / b if b else default


def pct(num, denom) -> str:
    if denom == 0:
        return "n/a"
    return f"{num / denom * 100:.1f}%"


def header(title: str):
    print(f"\n{'=' * 78}")
    print(f"  {title}")
    print(f"{'=' * 78}")


def subheader(title: str):
    print(f"\n  -- {title} --")


def where_clause(since: Optional[str], time_col: str = "evaluation_time") -> str:
    if since:
        return f"AND {time_col} >= '{since}'"
    return ""


# ============================================================================
#  Data Loader
# ============================================================================

def load_spx_data(conn: sqlite3.Connection, since: Optional[str] = None) -> List[Dict]:
    """Load all settled SPX hourly observations."""
    wc = where_clause(since)

    rows = conn.execute(f"""
        SELECT * FROM evaluated_opportunities
        WHERE product_type='spx_hourly'
              AND market_result IS NOT NULL {wc}
        ORDER BY evaluation_time
    """).fetchall()

    data = []
    for r in rows:
        d = dict(r)
        d["won"] = d["market_result"] == "yes"
        d["price"] = int(d["market_price"]) if d["market_price"] else 0
        d["stc"] = d.get("seconds_to_close") or 0
        d["fee_edge"] = d.get("fee_adjusted_edge") or 0
        d["cal_prob"] = d.get("calibrated_prob") or 0
        d["raw_p"] = d.get("raw_prob") or 0
        d["is_signal"] = d["filter_stage"] == "spx_observation"
        try:
            dt = datetime.fromisoformat(d["evaluation_time"].replace("Z", "+00:00"))
            d["eval_dt"] = dt
            d["hour_utc"] = dt.hour
            d["date"] = dt.strftime("%Y-%m-%d")
            d["weekday"] = dt.weekday()  # 0=Mon
            # ET approximation (UTC-5 for EST, UTC-4 for EDT)
            # March 2026: EDT starts Mar 8, so data before that is EST
            et_offset = 4  # EDT
            d["hour_et"] = (dt.hour - et_offset) % 24
        except (ValueError, AttributeError):
            d["eval_dt"] = None
            d["hour_utc"] = None
            d["date"] = None
            d["weekday"] = None
            d["hour_et"] = None
        data.append(d)

    return data


# ============================================================================
#  Section 1: Performance Overview
# ============================================================================

def section_performance(data: List[Dict]) -> Dict:
    header("1. PERFORMANCE OVERVIEW")

    signals = [d for d in data if d["is_signal"]]
    all_settled = data

    if not signals:
        print("  No spx_observation signals with settlement data.")
        return {}

    wins = sum(1 for d in signals if d["won"])
    losses = len(signals) - wins
    wr = wins / len(signals)
    lo, hi = wilson_ci(wins, len(signals))

    # PnL
    flat_pnl = sum(sim_pnl_1lot(d["price"], d["won"]) for d in signals)
    sized_pnl = sum(
        sim_pnl_1lot(d["price"], d["won"]) * (d.get("position_size") or 1)
        for d in signals
    )

    # Win/loss split
    win_pnl = sum(sim_pnl_1lot(d["price"], True) for d in signals if d["won"])
    loss_pnl_total = sum(sim_pnl_1lot(d["price"], False) for d in signals if not d["won"])
    pf = profit_factor(win_pnl, loss_pnl_total)

    # Brier
    probs = [d["cal_prob"] for d in signals if d["cal_prob"]]
    outcomes = [1.0 if d["won"] else 0.0 for d in signals if d["cal_prob"]]
    bs = brier_score(probs, outcomes)

    # Average breakeven
    avg_be = sum(breakeven_wr(d["price"]) for d in signals) / len(signals)

    # Date range
    dates = sorted(set(d["date"] for d in signals if d["date"]))

    print(f"\n  Settled signals: {len(signals)} ({wins}W/{losses}L)")
    print(f"  Win Rate:        {wr:.1%}  [95% CI: {lo:.1%} - {hi:.1%}]")
    print(f"  Avg Breakeven:   {avg_be:.1%}")
    print(f"  WR vs Breakeven: {wr - avg_be:+.1%}pp {'PROFITABLE' if wr > avg_be else 'LOSING'}")
    print(f"  Flat PnL:        ${flat_pnl:+.2f}  (1 contract per signal)")
    print(f"  Sized PnL:       ${sized_pnl:+.2f}  (Kelly-sized)")
    print(f"  Profit Factor:   {pf:.2f}")
    print(f"  Brier Score:     {bs:.4f}" if bs else "  Brier Score:     n/a")
    print(f"  Trading Days:    {len(dates)}")
    if dates:
        print(f"  Date Range:      {dates[0]} to {dates[-1]}")

    # Filter stage breakdown
    stages = defaultdict(int)
    for d in data:
        stages[d["filter_stage"]] += 1
    subheader("Filter Stage Funnel (all settled)")
    for stage, cnt in sorted(stages.items(), key=lambda x: -x[1]):
        stage_data = [d for d in data if d["filter_stage"] == stage]
        s_wins = sum(1 for d in stage_data if d["won"])
        s_wr = pct(s_wins, cnt)
        print(f"    {stage:<30s} {cnt:>5d}  WR: {s_wr}")

    return {
        "n": len(signals), "wins": wins, "losses": losses,
        "wr": wr, "wilson_lo": lo, "wilson_hi": hi,
        "flat_pnl": flat_pnl, "sized_pnl": sized_pnl,
        "profit_factor": pf, "brier": bs,
        "trading_days": len(dates),
    }


# ============================================================================
#  Section 2: EGARCH Blend Analysis
# ============================================================================

def section_egarch_blend(data: List[Dict]) -> Dict:
    header("2. EGARCH BLEND ANALYSIS")

    signals = [d for d in data if d["is_signal"]]
    blend_data = [d for d in signals if d.get("egarch_blend_weight") is not None]

    if not blend_data:
        print("  No EGARCH blend weight data available.")
        return {}

    weights = [d["egarch_blend_weight"] for d in blend_data]
    avg_w = sum(weights) / len(weights)
    min_w = min(weights)
    max_w = max(weights)

    print(f"\n  EGARCH blend weight: avg={avg_w:.4f}  min={min_w:.4f}  max={max_w:.4f}  n={len(blend_data)}")
    if min_w == max_w:
        print(f"  *** STUCK at {avg_w:.4f} -- blend weight not adapting")

    # By blend weight bucket
    BUCKETS = [
        ("0.00-0.20", 0.0, 0.20),
        ("0.20-0.40", 0.20, 0.40),
        ("0.40-0.60", 0.40, 0.60),
        ("0.60-0.80", 0.60, 0.80),
        ("0.80-1.00", 0.80, 1.01),
    ]

    subheader("WR by EGARCH Blend Weight")
    print(f"  {'Blend W':>10s} {'N':>4s} {'W':>3s} {'L':>3s} {'WR':>6s} {'Flat $':>8s} {'Verdict':>10s}")
    print(f"  {'-' * 52}")

    for label, lo, hi in BUCKETS:
        subset = [d for d in blend_data if lo <= d["egarch_blend_weight"] < hi]
        if not subset:
            continue
        w = sum(1 for d in subset if d["won"])
        n = len(subset)
        fpnl = sum(sim_pnl_1lot(d["price"], d["won"]) for d in subset)
        wr_val = w / n
        avg_be = sum(breakeven_wr(d["price"]) for d in subset) / n
        verdict = "OK" if wr_val > avg_be else "BELOW BE"
        print(f"  {label:>10s} {n:>4d} {w:>3d} {n-w:>3d} {wr_val:>5.1%} {fpnl:>+7.2f} {verdict:>10s}{significance_tag(n)}")

    # EGARCH sigma distribution
    sigma_data = [d for d in signals if d.get("egarch_sigma") is not None]
    if sigma_data:
        sigmas = [d["egarch_sigma"] for d in sigma_data]
        subheader("EGARCH Sigma Distribution")
        print(f"  avg={sum(sigmas)/len(sigmas):.2e}  "
              f"min={min(sigmas):.2e}  max={max(sigmas):.2e}  n={len(sigma_data)}")

    # Blend sigma
    bsig_data = [d for d in signals if d.get("egarch_blend_sigma") is not None]
    if bsig_data:
        bsigs = [d["egarch_blend_sigma"] for d in bsig_data]
        subheader("Blended Sigma Distribution")
        print(f"  avg={sum(bsigs)/len(bsigs):.2e}  "
              f"min={min(bsigs):.2e}  max={max(bsigs):.2e}  n={len(bsig_data)}")
        # WR by blend sigma tercile
        bsigs_sorted = sorted(bsig_data, key=lambda d: d["egarch_blend_sigma"])
        n = len(bsigs_sorted)
        terciles = [bsigs_sorted[:n//3], bsigs_sorted[n//3:2*n//3], bsigs_sorted[2*n//3:]]
        labels = ["Low vol", "Mid vol", "High vol"]
        subheader("WR by Blend Sigma Tercile")
        for lbl, t in zip(labels, terciles):
            if not t:
                continue
            tw = sum(1 for d in t if d["won"])
            fpnl = sum(sim_pnl_1lot(d["price"], d["won"]) for d in t)
            avg_sig = sum(d["egarch_blend_sigma"] for d in t) / len(t)
            print(f"    {lbl:<10s} n={len(t):>3d}  WR={tw/len(t):.1%}  "
                  f"sigma={avg_sig:.2e}  flat=${fpnl:+.2f}{significance_tag(len(t))}")

    return {
        "avg_blend_weight": avg_w, "min_blend_weight": min_w, "max_blend_weight": max_w,
        "n_with_blend": len(blend_data),
    }


# ============================================================================
#  Section 3: VIX Regime Analysis
# ============================================================================

def section_vix_regime(data: List[Dict], conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    header("3. VIX REGIME ANALYSIS")

    wc = where_clause(since)
    signals = [d for d in data if d["is_signal"]]

    # Check counterfactual column for VIX data
    vix_rows = conn.execute(f"""
        SELECT counterfactual, market_price, market_result, position_size,
               fee_adjusted_edge, seconds_to_close, calibrated_prob
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='spx_observation'
              AND market_result IS NOT NULL AND counterfactual IS NOT NULL {wc}
    """).fetchall()

    vix_data = []
    for r in vix_rows:
        try:
            cf = json.loads(r["counterfactual"])
            if cf.get("vix_implied_rv") is not None:
                vix_data.append({
                    "vix_rv": cf["vix_implied_rv"],
                    "seasonal_factor": cf.get("seasonal_factor"),
                    "n_returns": cf.get("n_returns"),
                    "price": r["market_price"],
                    "won": r["market_result"] == "yes",
                    "pos": r["position_size"] or 1,
                    "edge": r["fee_adjusted_edge"] or 0,
                    "stc": r["seconds_to_close"] or 0,
                    "cal_prob": r["calibrated_prob"] or 0,
                })
        except (json.JSONDecodeError, TypeError):
            pass

    if not vix_data:
        print("  No VIX data found in counterfactual column.")
        print("  VIX integration data may not be instrumented yet.")

        # Fall back to vol_regime column
        vol_regime_data = [d for d in signals if d.get("vol_regime")]
        if vol_regime_data:
            subheader("Volatility Regime (from vol_regime column)")
            regimes = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
            for d in vol_regime_data:
                r = regimes[d["vol_regime"]]
                r["n"] += 1
                if d["won"]:
                    r["w"] += 1
                r["pnl"] += sim_pnl_1lot(d["price"], d["won"])

            print(f"  {'Regime':<15s} {'N':>4s} {'WR':>6s} {'Flat $':>8s}")
            print(f"  {'-' * 38}")
            for regime, stats in sorted(regimes.items()):
                print(f"  {regime:<15s} {stats['n']:>4d} {stats['w']/stats['n']:>5.1%} "
                      f"{stats['pnl']:>+7.2f}{significance_tag(stats['n'])}")
        return {"vix_available": False}

    # VIX tercile analysis
    vix_data.sort(key=lambda x: x["vix_rv"])
    n = len(vix_data)
    terciles = [vix_data[:n//3], vix_data[n//3:2*n//3], vix_data[2*n//3:]]
    labels = ["Low VIX", "Mid VIX", "High VIX"]

    subheader("WR by VIX Implied RV Tercile")
    print(f"  {'Tercile':<10s} {'N':>4s} {'WR':>6s} {'Avg VIX RV':>12s} {'Flat $':>8s} {'AvgEdge':>8s}")
    print(f"  {'-' * 54}")

    for lbl, t in zip(labels, terciles):
        if not t:
            continue
        tw = sum(1 for d in t if d["won"])
        fpnl = sum(sim_pnl_1lot(d["price"], d["won"]) for d in t)
        avg_vix = sum(d["vix_rv"] for d in t) / len(t)
        avg_edge = sum(d["edge"] for d in t) / len(t)
        print(f"  {lbl:<10s} {len(t):>4d} {tw/len(t):>5.1%} {avg_vix:>11.2e} "
              f"{fpnl:>+7.2f} {avg_edge:>7.3%}{significance_tag(len(t))}")

    # Seasonal factor analysis
    seasonal_data = [d for d in vix_data if d["seasonal_factor"] is not None]
    if seasonal_data:
        subheader("Seasonal Factor Distribution")
        sfs = [d["seasonal_factor"] for d in seasonal_data]
        print(f"  avg={sum(sfs)/len(sfs):.4f}  min={min(sfs):.4f}  max={max(sfs):.4f}  n={len(sfs)}")

        # High vs low seasonal factor
        median_sf = sorted(sfs)[len(sfs) // 2]
        low_sf = [d for d in seasonal_data if d["seasonal_factor"] <= median_sf]
        high_sf = [d for d in seasonal_data if d["seasonal_factor"] > median_sf]
        if low_sf and high_sf:
            lw = sum(1 for d in low_sf if d["won"])
            hw = sum(1 for d in high_sf if d["won"])
            print(f"  Low seasonal (<=median):  n={len(low_sf)} WR={lw/len(low_sf):.1%}")
            print(f"  High seasonal (>median):  n={len(high_sf)} WR={hw/len(high_sf):.1%}")

    return {"vix_available": True, "n_vix_obs": len(vix_data)}


# ============================================================================
#  Section 4: Intraday Pattern Analysis
# ============================================================================

def section_intraday(data: List[Dict]) -> Dict:
    header("4. INTRADAY PATTERN ANALYSIS (Eastern Time)")

    signals = [d for d in data if d["is_signal"] and d["hour_et"] is not None]
    if not signals:
        print("  No time-of-day data.")
        return {}

    # SPX market hours: 9:30 AM - 4:00 PM ET
    # Key periods: Open (9-10), Mid-morning (10-12), Lunch (12-14), Afternoon (14-16), After-hours
    PERIODS = [
        ("Pre-market (4-9 ET)", range(4, 10)),
        ("Open rush (10-11 ET)", range(10, 11)),
        ("Mid-morning (11-12 ET)", range(11, 12)),
        ("Lunch (12-14 ET)", range(12, 14)),
        ("Afternoon (14-16 ET)", range(14, 16)),
        ("Close (16-17 ET)", range(16, 17)),
        ("After-hours (17+ ET)", range(17, 24)),
        ("Overnight (0-4 ET)", range(0, 4)),
    ]

    subheader("By Market Period")
    print(f"  {'Period':<28s} {'N':>4s} {'W':>3s} {'L':>3s} {'WR':>6s} {'BE WR':>6s} "
          f"{'Flat $':>8s} {'AvgEdge':>8s} {'Verdict':>10s}")
    print(f"  {'-' * 92}")

    period_results = {}
    for label, hours in PERIODS:
        subset = [d for d in signals if d["hour_et"] in hours]
        if not subset:
            continue
        w = sum(1 for d in subset if d["won"])
        n = len(subset)
        fpnl = sum(sim_pnl_1lot(d["price"], d["won"]) for d in subset)
        wr_val = w / n
        avg_be = sum(breakeven_wr(d["price"]) for d in subset) / n
        avg_edge = sum(d["fee_edge"] for d in subset) / n
        verdict = "PROFITABLE" if wr_val > avg_be and n >= 5 else ("LOSING" if wr_val < avg_be and n >= 5 else "LOW N")
        print(f"  {label:<28s} {n:>4d} {w:>3d} {n-w:>3d} {wr_val:>5.1%} {avg_be:>5.1%} "
              f"{fpnl:>+7.2f} {avg_edge:>7.3%} {verdict:>10s}{significance_tag(n)}")
        period_results[label] = {"n": n, "wins": w, "wr": wr_val, "pnl": fpnl}

    # Hour-by-hour for granularity
    subheader("Hour-by-Hour (ET)")
    hourly = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0, "vol_sum": 0.0})
    for d in signals:
        h = d["hour_et"]
        hourly[h]["n"] += 1
        if d["won"]:
            hourly[h]["w"] += 1
        hourly[h]["pnl"] += sim_pnl_1lot(d["price"], d["won"])
        hourly[h]["vol_sum"] += d.get("volatility") or 0

    print(f"  {'Hour ET':>8s} {'N':>4s} {'WR':>6s} {'Flat $':>8s} {'Avg Vol':>12s}")
    print(f"  {'-' * 44}")
    for h in sorted(hourly.keys()):
        hd = hourly[h]
        avg_vol = hd["vol_sum"] / hd["n"] if hd["n"] else 0
        vol_str = f"{avg_vol:.2e}" if avg_vol > 0 else "n/a"
        print(f"  {h:>5d}:00 {hd['n']:>4d} {hd['w']/hd['n']:>5.1%} {hd['pnl']:>+7.2f} "
              f"{vol_str:>12s}{significance_tag(hd['n'])}")

    # Day of week
    subheader("Day of Week")
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    dow = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
    for d in signals:
        if d["weekday"] is not None:
            dow[d["weekday"]]["n"] += 1
            if d["won"]:
                dow[d["weekday"]]["w"] += 1
            dow[d["weekday"]]["pnl"] += sim_pnl_1lot(d["price"], d["won"])

    print(f"  {'Day':<5s} {'N':>4s} {'WR':>6s} {'Flat $':>8s}")
    print(f"  {'-' * 28}")
    for day in sorted(dow.keys()):
        dd = dow[day]
        print(f"  {dow_names[day]:<5s} {dd['n']:>4d} {dd['w']/dd['n']:>5.1%} "
              f"{dd['pnl']:>+7.2f}{significance_tag(dd['n'])}")

    return {"periods": period_results}


# ============================================================================
#  Section 5: Price Tier Analysis
# ============================================================================

def section_price_tiers(data: List[Dict]) -> Dict:
    header("5. PRICE TIER ANALYSIS")

    signals = [d for d in data if d["is_signal"]]
    if not signals:
        print("  No data.")
        return {}

    TIERS = [
        ("70-74c", 70, 74),
        ("75-79c", 75, 79),
        ("80-84c", 80, 84),
        ("85-89c", 85, 89),
        ("90-94c", 90, 94),
        ("95-99c", 95, 99),
    ]

    print(f"\n  {'Tier':>8s} {'N':>4s} {'W':>3s} {'L':>3s} {'WR':>6s} {'BE WR':>6s} {'Gap':>6s} "
          f"{'Flat $':>8s} {'Sz $':>9s} {'AvgEdge':>8s} {'PF':>6s} {'Verdict':>10s}")
    print(f"  {'-' * 104}")

    tier_data = {}
    for label, lo, hi in TIERS:
        subset = [d for d in signals if lo <= d["price"] <= hi]
        if not subset:
            continue
        n = len(subset)
        w = sum(1 for d in subset if d["won"])
        wr_val = w / n
        avg_p = sum(d["price"] for d in subset) / n
        be = breakeven_wr(int(avg_p))
        gap = wr_val - be
        fpnl = sum(sim_pnl_1lot(d["price"], d["won"]) for d in subset)
        spnl = sum(sim_pnl_1lot(d["price"], d["won"]) * (d.get("position_size") or 1) for d in subset)
        avg_edge = sum(d["fee_edge"] for d in subset) / n
        w_pnl = sum(sim_pnl_1lot(d["price"], True) for d in subset if d["won"])
        l_pnl = sum(sim_pnl_1lot(d["price"], False) for d in subset if not d["won"])
        pf_val = profit_factor(w_pnl, l_pnl)
        pf_str = f"{pf_val:.1f}" if pf_val < 100 else "inf"

        if gap < -0.05 and n >= 5:
            verdict = "LOSING"
        elif gap < 0 and n >= 5:
            verdict = "MARGINAL"
        elif n < 10:
            verdict = "LOW N"
        else:
            verdict = "PROFITABLE"

        print(f"  {label:>8s} {n:>4d} {w:>3d} {n-w:>3d} {wr_val:>5.1%} {be:>5.1%} {gap:>+5.1%} "
              f"{fpnl:>+7.2f} {spnl:>+8.2f} {avg_edge:>7.3%} {pf_str:>6s} {verdict:>10s}{significance_tag(n)}")
        tier_data[label] = {"n": n, "wins": w, "wr": wr_val, "be": be, "pnl": fpnl}

    # Highlight losing tiers
    losing = [(l, t) for l, t in tier_data.items() if t["wr"] < t["be"] and t["n"] >= 5]
    if losing:
        print(f"\n  *** BELOW-BREAKEVEN TIERS:")
        for label, t in losing:
            print(f"      {label}: WR {t['wr']:.1%} vs BE {t['be']:.1%} ({t['wr']-t['be']:+.1%}pp)")

    return tier_data


# ============================================================================
#  Section 6: Multi-Position Window Analysis
# ============================================================================

def section_window_correlation(data: List[Dict]) -> Dict:
    header("6. MULTI-POSITION WINDOW ANALYSIS")

    signals = [d for d in data if d["is_signal"]]
    if not signals:
        print("  No data.")
        return {}

    # Group by event_ticker
    by_window = defaultdict(list)
    for d in signals:
        et = d.get("event_ticker", "")
        by_window[et].append(d)

    # Distribution of positions per window
    pos_counts = defaultdict(int)
    for et, entries in by_window.items():
        pos_counts[len(entries)] += 1

    subheader("Positions per Window Distribution")
    print(f"  {'Positions':>10s} {'Windows':>8s} {'%':>6s}")
    print(f"  {'-' * 28}")
    total_windows = len(by_window)
    for cnt in sorted(pos_counts.keys()):
        print(f"  {cnt:>10d} {pos_counts[cnt]:>8d} {pos_counts[cnt]/total_windows:>5.1%}")

    # Correlation: all-win vs all-loss vs mixed
    subheader("Window Outcome Patterns")
    all_win = 0
    all_loss = 0
    mixed = 0
    multi_windows = [(et, entries) for et, entries in by_window.items() if len(entries) > 1]

    for et, entries in multi_windows:
        w = sum(1 for d in entries if d["won"])
        if w == len(entries):
            all_win += 1
        elif w == 0:
            all_loss += 1
        else:
            mixed += 1

    if multi_windows:
        n_multi = len(multi_windows)
        print(f"  Multi-position windows: {n_multi}")
        print(f"  All-win:  {all_win} ({all_win/n_multi:.0%})")
        print(f"  All-loss: {all_loss} ({all_loss/n_multi:.0%})")
        print(f"  Mixed:    {mixed} ({mixed/n_multi:.0%})")

        if all_loss > 0:
            print(f"\n  *** {all_loss} windows where ALL positions lost -- correlated blowup!")
            # Show details
            for et, entries in multi_windows:
                w = sum(1 for d in entries if d["won"])
                if w == 0:
                    pnl = sum(sim_pnl_1lot(d["price"], d["won"]) * (d.get("position_size") or 1) for d in entries)
                    print(f"      {et}: {len(entries)} positions, sized PnL ${pnl:.2f}")

        # Effective number of bets (ENB)
        # If correlation=1, ENB=1; if independent, ENB=n
        if n_multi >= 3:
            corr_count = 0
            total_pairs = 0
            for et, entries in multi_windows:
                for i in range(len(entries)):
                    for j in range(i + 1, len(entries)):
                        total_pairs += 1
                        if entries[i]["won"] == entries[j]["won"]:
                            corr_count += 1
            if total_pairs > 0:
                agreement_rate = corr_count / total_pairs
                # Simple ENB estimate
                avg_per_window = sum(len(e) for _, e in multi_windows) / n_multi
                estimated_corr = (agreement_rate - 0.5) * 2  # scale to [-1, 1]
                estimated_corr = max(0, min(1, estimated_corr))
                enb = avg_per_window / (1 + (avg_per_window - 1) * estimated_corr) if estimated_corr < 1 else 1
                print(f"\n  Agreement rate (same outcome): {agreement_rate:.1%}")
                print(f"  Estimated correlation: {estimated_corr:.2f}")
                print(f"  Avg positions/window: {avg_per_window:.1f}")
                print(f"  Effective number of bets (ENB): {enb:.1f}")
    else:
        print("  No multi-position windows found.")

    # Position limit simulation
    subheader("Position Limit Simulation")
    print(f"  {'Max/Window':>12s} {'N':>4s} {'W':>3s} {'L':>3s} {'WR':>6s} {'Flat $':>8s} {'Sized $':>10s}")
    print(f"  {'-' * 55}")

    for limit in [1, 2, 3, 5, "all"]:
        total_pnl = 0.0
        total_spnl = 0.0
        total_n = 0
        total_w = 0
        for et, entries in by_window.items():
            # Sort by edge descending, take top N
            sorted_entries = sorted(entries, key=lambda x: x["fee_edge"], reverse=True)
            selected = sorted_entries if limit == "all" else sorted_entries[:limit]
            for d in selected:
                total_n += 1
                if d["won"]:
                    total_w += 1
                total_pnl += sim_pnl_1lot(d["price"], d["won"])
                total_spnl += sim_pnl_1lot(d["price"], d["won"]) * (d.get("position_size") or 1)

        label = str(limit) if limit != "all" else "unlimited"
        wr_val = total_w / total_n if total_n else 0
        marker = " *** BEST" if limit == 2 else ""  # current config
        print(f"  {label:>12s} {total_n:>4d} {total_w:>3d} {total_n-total_w:>3d} "
              f"{wr_val:>5.1%} {total_pnl:>+7.2f} {total_spnl:>+9.2f}{marker}")

    return {
        "total_windows": total_windows,
        "multi_position_windows": len(multi_windows),
        "all_loss_windows": all_loss,
    }


# ============================================================================
#  Section 7: Edge Integrity Analysis
# ============================================================================

def section_edge_integrity(data: List[Dict]) -> Dict:
    header("7. EDGE INTEGRITY ANALYSIS")

    signals = [d for d in data if d["is_signal"]]
    if not signals:
        print("  No data.")
        return {}

    # Edge quintile analysis
    sorted_by_edge = sorted(signals, key=lambda d: d["fee_edge"])
    n = len(sorted_by_edge)

    subheader("WR by Fee-Adjusted Edge Quintile")
    print(f"  {'Quintile':<12s} {'N':>4s} {'WR':>6s} {'AvgEdge':>9s} {'Flat $':>8s} {'Verdict':>10s}")
    print(f"  {'-' * 55}")

    quintile_results = []
    prev_wr = None
    monotonic = True
    for i, label in enumerate(["Q1 (low)", "Q2", "Q3", "Q4", "Q5 (high)"]):
        start = i * n // 5
        end = (i + 1) * n // 5
        subset = sorted_by_edge[start:end]
        if not subset:
            continue
        w = sum(1 for d in subset if d["won"])
        wr_val = w / len(subset)
        avg_edge = sum(d["fee_edge"] for d in subset) / len(subset)
        fpnl = sum(sim_pnl_1lot(d["price"], d["won"]) for d in subset)
        verdict = "OK" if fpnl > 0 else "NEGATIVE"
        if prev_wr is not None and wr_val < prev_wr - 0.05:
            monotonic = False
            verdict = "NON-MONO"
        prev_wr = wr_val
        print(f"  {label:<12s} {len(subset):>4d} {wr_val:>5.1%} {avg_edge:>8.3%} "
              f"{fpnl:>+7.2f} {verdict:>10s}{significance_tag(len(subset))}")
        quintile_results.append({"label": label, "n": len(subset), "wr": wr_val, "edge": avg_edge})

    if monotonic:
        print(f"\n  Edge monotonicity: PASSED (higher edge -> higher WR)")
    else:
        print(f"\n  *** Edge monotonicity VIOLATED -- edge signal may not be informative")

    # Edge threshold sweep
    subheader("Edge Threshold Sweep")
    print(f"  {'Min Edge':>10s} {'N':>4s} {'W':>3s} {'L':>3s} {'WR':>6s} {'Flat $':>8s} {'$/day':>8s} {'Verdict':>10s}")
    print(f"  {'-' * 62}")

    # Compute trading days for $/day
    dates = set(d["date"] for d in signals if d["date"])
    n_days = max(len(dates), 1)

    for min_edge in [0.0, 0.005, 0.01, 0.015, 0.02, 0.03, 0.05, 0.08, 0.10]:
        subset = [d for d in signals if d["fee_edge"] >= min_edge]
        if not subset:
            continue
        w = sum(1 for d in subset if d["won"])
        fpnl = sum(sim_pnl_1lot(d["price"], d["won"]) for d in subset)
        per_day = fpnl / n_days
        wr_val = w / len(subset) if subset else 0
        avg_be = sum(breakeven_wr(d["price"]) for d in subset) / len(subset)
        verdict = "PROFITABLE" if wr_val > avg_be and len(subset) >= 5 else "LOSING"
        marker = " <<<" if min_edge == 0.0 else ""
        if fpnl > 0 and len(subset) >= 10:
            marker = " ***"
        print(f"  {min_edge:>9.1%} {len(subset):>4d} {w:>3d} {len(subset)-w:>3d} "
              f"{wr_val:>5.1%} {fpnl:>+7.2f} {per_day:>+7.2f} {verdict:>10s}{marker}")

    return {"monotonic": monotonic, "quintiles": quintile_results}


# ============================================================================
#  Section 8: Calibration Diagnostics
# ============================================================================

def section_calibration(data: List[Dict]) -> Dict:
    header("8. CALIBRATION DIAGNOSTICS")

    # Include all settled data, not just signals
    settled = [d for d in data if d["cal_prob"] > 0]
    if not settled:
        print("  No calibration data.")
        return {}

    # Overall Brier
    probs = [d["cal_prob"] for d in settled]
    outcomes = [1.0 if d["won"] else 0.0 for d in settled]
    bs = brier_score(probs, outcomes)

    print(f"\n  Overall Brier Score: {bs:.4f} (n={len(settled)})")

    # Calibration buckets
    BUCKETS = [
        ("0.50-0.70", 0.50, 0.70),
        ("0.70-0.80", 0.70, 0.80),
        ("0.80-0.85", 0.80, 0.85),
        ("0.85-0.90", 0.85, 0.90),
        ("0.90-0.95", 0.90, 0.95),
        ("0.95-1.00", 0.95, 1.01),
    ]

    subheader("Predicted vs Actual by Probability Bucket")
    print(f"  {'Bucket':>10s} {'N':>4s} {'Predicted':>10s} {'Actual':>8s} {'Gap':>8s} "
          f"{'Brier':>7s} {'Verdict':>14s}")
    print(f"  {'-' * 68}")

    total_overconf = 0.0
    total_overconf_n = 0
    for label, lo, hi in BUCKETS:
        subset = [d for d in settled if lo <= d["cal_prob"] < hi]
        if not subset:
            continue
        avg_pred = sum(d["cal_prob"] for d in subset) / len(subset)
        actual_wr = sum(1 for d in subset if d["won"]) / len(subset)
        gap = avg_pred - actual_wr
        bucket_probs = [d["cal_prob"] for d in subset]
        bucket_outs = [1.0 if d["won"] else 0.0 for d in subset]
        bucket_bs = brier_score(bucket_probs, bucket_outs)

        if gap > 0.10 and len(subset) >= 5:
            verdict = "OVERCONFIDENT"
        elif gap > 0.05 and len(subset) >= 5:
            verdict = "WARM"
        elif gap < -0.05 and len(subset) >= 5:
            verdict = "UNDERCONFIDENT"
        elif len(subset) < 10:
            verdict = "LOW N"
        else:
            verdict = "CALIBRATED"

        total_overconf += gap * len(subset)
        total_overconf_n += len(subset)

        print(f"  {label:>10s} {len(subset):>4d} {avg_pred:>9.3f} {actual_wr:>7.1%} {gap:>+7.3f} "
              f"{bucket_bs:>6.4f} {verdict:>14s}{significance_tag(len(subset))}")

    if total_overconf_n > 0:
        avg_overconf = total_overconf / total_overconf_n
        print(f"\n  Avg overconfidence: {avg_overconf:+.3f}pp")
        if avg_overconf > 0.05:
            print(f"  *** Systematic overconfidence detected -- temperature correction recommended")
        elif avg_overconf < -0.05:
            print(f"  *** Systematic underconfidence -- model leaving edge on table")

    # raw_prob vs calibrated_prob comparison
    raw_data = [d for d in settled if d["raw_p"] > 0]
    if raw_data:
        subheader("Raw vs Calibrated Probability")
        raw_probs = [d["raw_p"] for d in raw_data]
        raw_outs = [1.0 if d["won"] else 0.0 for d in raw_data]
        raw_bs = brier_score(raw_probs, raw_outs)
        cal_probs = [d["cal_prob"] for d in raw_data]
        cal_bs = brier_score(cal_probs, raw_outs)
        print(f"  Raw prob Brier:        {raw_bs:.4f}")
        print(f"  Calibrated prob Brier: {cal_bs:.4f}")
        if raw_bs and cal_bs:
            improvement = raw_bs - cal_bs
            print(f"  Calibration improvement: {improvement:+.4f} ({'HELPING' if improvement > 0 else 'HURTING'})")

    # Market-only baseline: does market_price/100 beat EGARCH?
    mkt_data = [d for d in settled if d.get("market_price")]
    if mkt_data:
        subheader("Market-Only Baseline (fair_prob = market_price / 100)")
        mkt_probs = [d["market_price"] / 100.0 if d["market_price"] > 1.5
                     else d["market_price"] for d in mkt_data]
        mkt_outs = [1.0 if d["won"] else 0.0 for d in mkt_data]
        mkt_bs = brier_score(mkt_probs, mkt_outs)
        cal_probs_m = [d["cal_prob"] for d in mkt_data]
        cal_bs_m = brier_score(cal_probs_m, mkt_outs)
        print(f"  Market-only Brier:  {mkt_bs:.4f}  (fair_prob = market_price/100)")
        print(f"  EGARCH model Brier: {cal_bs_m:.4f}  (calibrated_prob)")
        if mkt_bs is not None and cal_bs_m is not None:
            delta = cal_bs_m - mkt_bs
            if delta > 0:
                print(f"  Delta: {delta:+.4f} — MARKET BEATS MODEL")
                print(f"  *** Edge inversion signal: market is better calibrated than EGARCH")
                print(f"  *** Consider increasing MARKET_BLEND_W toward 0.60+")
            else:
                print(f"  Delta: {delta:+.4f} — MODEL BEATS MARKET")
                print(f"  *** EGARCH adds real signal beyond market price")

        # Per-price-tier market vs model comparison
        print(f"\n  {'Tier':>8} {'N':>4} {'MktBrier':>9} {'ModelBrier':>11} {'Winner':>10}")
        print(f"  {'-'*48}")
        for tier_label, tier_lo, tier_hi in [("<80c", 0, 80), ("80-89c", 80, 90), ("90c+", 90, 100)]:
            tier_d = [d for d in mkt_data
                      if tier_lo <= (d["market_price"] if d["market_price"] > 1.5
                                     else d["market_price"] * 100) < tier_hi]
            if len(tier_d) < 5:
                continue
            t_mkt = [d["market_price"] / 100.0 if d["market_price"] > 1.5
                     else d["market_price"] for d in tier_d]
            t_out = [1.0 if d["won"] else 0.0 for d in tier_d]
            t_cal = [d["cal_prob"] for d in tier_d]
            t_mkt_bs = brier_score(t_mkt, t_out)
            t_cal_bs = brier_score(t_cal, t_out)
            if t_mkt_bs is not None and t_cal_bs is not None:
                winner = "Market" if t_cal_bs > t_mkt_bs else "Model"
                print(f"  {tier_label:>8} {len(tier_d):>4} {t_mkt_bs:>8.4f} {t_cal_bs:>10.4f} {winner:>10}")

    return {"brier": bs, "n": len(settled)}


# ============================================================================
#  Section 9: Trading Hours Analysis
# ============================================================================

def section_trading_hours(data: List[Dict]) -> Dict:
    header("9. MARKET HOURS vs EXTENDED HOURS")

    signals = [d for d in data if d["is_signal"] and d["hour_et"] is not None]
    if not signals:
        print("  No data.")
        return {}

    # SPX regular hours: 9:30 AM - 4:00 PM ET (roughly hour 10-16)
    # Pre-market: 4-9:30 ET, After-hours: 4-8 PM ET
    # SPX futures trade nearly 24h but cash market is 9:30-4

    regular = [d for d in signals if 10 <= d["hour_et"] < 16]
    extended = [d for d in signals if d["hour_et"] < 10 or d["hour_et"] >= 16]

    print(f"\n  {'Session':<20s} {'N':>4s} {'W':>3s} {'L':>3s} {'WR':>6s} {'BE WR':>6s} "
          f"{'Flat $':>8s} {'AvgEdge':>8s} {'AvgSTC':>7s}")
    print(f"  {'-' * 72}")

    for label, subset in [("Regular (10-16 ET)", regular), ("Extended", extended)]:
        if not subset:
            print(f"  {label:<20s} {'--- no data ---':>30s}")
            continue
        w = sum(1 for d in subset if d["won"])
        n = len(subset)
        fpnl = sum(sim_pnl_1lot(d["price"], d["won"]) for d in subset)
        avg_be = sum(breakeven_wr(d["price"]) for d in subset) / n
        avg_edge = sum(d["fee_edge"] for d in subset) / n
        avg_stc = sum(d["stc"] for d in subset) / n
        wr_val = w / n
        print(f"  {label:<20s} {n:>4d} {w:>3d} {n-w:>3d} {wr_val:>5.1%} {avg_be:>5.1%} "
              f"{fpnl:>+7.2f} {avg_edge:>7.3%} {avg_stc:>6.0f}{significance_tag(n)}")

    # Volatility by session
    subheader("Volatility by Session")
    for label, subset in [("Regular", regular), ("Extended", extended)]:
        vol_data = [d for d in subset if d.get("volatility") is not None]
        if vol_data:
            vols = [d["volatility"] for d in vol_data]
            print(f"  {label}: avg={sum(vols)/len(vols):.2e}  "
                  f"min={min(vols):.2e}  max={max(vols):.2e}  n={len(vols)}")

    return {"regular_n": len(regular), "extended_n": len(extended)}


# ============================================================================
#  Section 10: Counterfactual Config Optimizer
# ============================================================================

def section_counterfactual(data: List[Dict]) -> Dict:
    header("10. COUNTERFACTUAL CONFIG OPTIMIZER")

    signals = [d for d in data if d["is_signal"]]
    if not signals:
        print("  No data.")
        return {}

    dates = set(d["date"] for d in signals if d["date"])
    n_days = max(len(dates), 1)

    # Temperature sweep
    subheader("Temperature Sweep (simulated)")
    print(f"  {'Temp':>6s} {'Brier':>8s} {'AvgProb':>9s} {'AvgOverconf':>13s}")
    print(f"  {'-' * 40}")

    best_temp = 1.0
    best_brier = float("inf")

    for temp in [0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.45, 1.6, 1.8, 2.0, 2.5]:
        # Apply temperature to raw_prob or cal_prob
        adjusted_probs = []
        outcomes = []
        for d in signals:
            p = d["cal_prob"]
            if p <= 0 or p >= 1:
                continue
            # Temperature scaling: logit -> scale -> sigmoid
            logit = math.log(p / (1 - p))
            scaled_logit = logit / temp
            adjusted_p = 1 / (1 + math.exp(-scaled_logit))
            adjusted_probs.append(adjusted_p)
            outcomes.append(1.0 if d["won"] else 0.0)

        if not adjusted_probs:
            continue
        bs = brier_score(adjusted_probs, outcomes)
        avg_p = sum(adjusted_probs) / len(adjusted_probs)
        avg_oc = sum(adjusted_probs) / len(adjusted_probs) - sum(outcomes) / len(outcomes)

        marker = ""
        if bs < best_brier:
            best_brier = bs
            best_temp = temp
            marker = " <-- best"
        print(f"  {temp:>5.2f} {bs:>7.4f} {avg_p:>8.4f} {avg_oc:>+12.4f}{marker}")

    print(f"\n  Optimal temperature: T={best_temp:.2f} (Brier={best_brier:.4f})")
    if best_temp > 1.2:
        print(f"  *** Model is overconfident -- temperature correction would help")
    elif best_temp < 0.9:
        print(f"  *** Model is underconfident -- consider lower temperature")

    # Min price sweep
    subheader("Min Entry Price Sweep")
    print(f"  {'Min P':>6s} {'N':>4s} {'W':>3s} {'L':>3s} {'WR':>6s} {'Flat $':>8s} "
          f"{'$/day':>8s} {'Excluded':>9s} {'Verdict':>10s}")
    print(f"  {'-' * 70}")

    best_price = 70
    best_daily_pnl = -999
    for min_p in [70, 75, 78, 80, 82, 84, 85, 87, 90, 92, 95]:
        subset = [d for d in signals if d["price"] >= min_p]
        if not subset:
            continue
        w = sum(1 for d in subset if d["won"])
        fpnl = sum(sim_pnl_1lot(d["price"], d["won"]) for d in subset)
        daily = fpnl / n_days
        excluded = len(signals) - len(subset)
        avg_be = sum(breakeven_wr(d["price"]) for d in subset) / len(subset)
        verdict = "PROFITABLE" if w / len(subset) > avg_be else "LOSING"
        marker = ""
        if daily > best_daily_pnl and len(subset) >= 10:
            best_daily_pnl = daily
            best_price = min_p
            marker = " *** BEST $/day"
        print(f"  {min_p:>5d}c {len(subset):>4d} {w:>3d} {len(subset)-w:>3d} "
              f"{w/len(subset):>5.1%} {fpnl:>+7.2f} {daily:>+7.2f} {excluded:>8d} {verdict:>10s}{marker}")

    # STC range sweep
    subheader("STC Range Sweep")
    print(f"  {'STC Range':>14s} {'N':>4s} {'W':>3s} {'L':>3s} {'WR':>6s} {'Flat $':>8s} {'$/day':>8s}")
    print(f"  {'-' * 54}")

    for min_stc, max_stc in [(300, 600), (300, 900), (300, 1200), (300, 1800),
                              (600, 1200), (600, 1800), (900, 1800),
                              (0, 600), (0, 1800), (0, 3600)]:
        subset = [d for d in signals if min_stc <= d["stc"] <= max_stc]
        if not subset:
            continue
        w = sum(1 for d in subset if d["won"])
        fpnl = sum(sim_pnl_1lot(d["price"], d["won"]) for d in subset)
        daily = fpnl / n_days
        wr_val = w / len(subset) if subset else 0
        print(f"  {min_stc:>5d}-{max_stc:<5d}s {len(subset):>4d} {w:>3d} {len(subset)-w:>3d} "
              f"{wr_val:>5.1%} {fpnl:>+7.2f} {daily:>+7.2f}{significance_tag(len(subset))}")

    # Market blend weight sweep
    subheader("Market Blend Weight Sweep")
    print(f"  {'Blend W':>8s} {'Brier':>8s} {'AvgProb':>9s}")
    print(f"  {'-' * 28}")

    raw_data = [d for d in signals if d["raw_p"] > 0]
    if raw_data:
        best_blend = 0.40
        best_blend_brier = float("inf")
        for blend_w in [0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60]:
            blended_probs = []
            outs = []
            for d in raw_data:
                model_p = d["cal_prob"]
                market_p = d["price"] / 100.0
                blended = model_p * (1 - blend_w) + market_p * blend_w
                blended_probs.append(blended)
                outs.append(1.0 if d["won"] else 0.0)
            bs = brier_score(blended_probs, outs)
            avg_p = sum(blended_probs) / len(blended_probs)
            marker = ""
            if bs < best_blend_brier:
                best_blend_brier = bs
                best_blend = blend_w
                marker = " <-- best"
            print(f"  {blend_w:>7.2f} {bs:>7.4f} {avg_p:>8.4f}{marker}")
        print(f"\n  Optimal blend: W={best_blend:.2f} (Brier={best_blend_brier:.4f})")
    else:
        print("  No raw_prob data available for blend analysis.")

    return {
        "best_temperature": best_temp,
        "best_min_price": best_price,
        "best_daily_pnl": best_daily_pnl,
    }


# ============================================================================
#  Section 11: Robustness & Stability
# ============================================================================

def section_robustness(data: List[Dict]) -> Dict:
    header("11. ROBUSTNESS & STABILITY")

    signals = [d for d in data if d["is_signal"]]
    if not signals:
        print("  No data.")
        return {}

    # Time stability: split into halves
    mid = len(signals) // 2
    h1 = signals[:mid]
    h2 = signals[mid:]

    subheader("Half-Split Stability")
    for label, half in [("First half", h1), ("Second half", h2)]:
        if not half:
            continue
        w = sum(1 for d in half if d["won"])
        fpnl = sum(sim_pnl_1lot(d["price"], d["won"]) for d in half)
        wr_val = w / len(half)
        lo, hi = wilson_ci(w, len(half))
        print(f"  {label}: n={len(half)} WR={wr_val:.1%} [{lo:.1%}-{hi:.1%}] PnL=${fpnl:+.2f}")

    if h1 and h2:
        h1_wr = sum(1 for d in h1 if d["won"]) / len(h1)
        h2_wr = sum(1 for d in h2 if d["won"]) / len(h2)
        drift = abs(h2_wr - h1_wr)
        if drift > 0.15:
            print(f"  *** WR DRIFT of {drift:.1%}pp between halves -- model may be unstable")
        else:
            print(f"  WR drift: {drift:.1%}pp -- {'STABLE' if drift < 0.05 else 'MODERATE'}")

    # Daily PnL timeline
    subheader("Daily PnL Timeline")
    daily = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0, "n": 0})
    for d in signals:
        if d["date"]:
            daily[d["date"]]["n"] += 1
            if d["won"]:
                daily[d["date"]]["w"] += 1
            else:
                daily[d["date"]]["l"] += 1
            daily[d["date"]]["pnl"] += sim_pnl_1lot(d["price"], d["won"])

    print(f"  {'Date':<12s} {'N':>4s} {'W':>3s} {'L':>3s} {'WR':>6s} {'PnL':>8s} {'Cum $':>8s}")
    print(f"  {'-' * 50}")

    cum_pnl = 0
    losing_days = 0
    winning_days = 0
    max_drawdown = 0
    peak = 0
    for dt in sorted(daily.keys()):
        dd = daily[dt]
        wr_val = dd["w"] / dd["n"] if dd["n"] else 0
        cum_pnl += dd["pnl"]
        peak = max(peak, cum_pnl)
        dd_val = peak - cum_pnl
        max_drawdown = max(max_drawdown, dd_val)
        if dd["pnl"] >= 0:
            winning_days += 1
        else:
            losing_days += 1
        print(f"  {dt:<12s} {dd['n']:>4d} {dd['w']:>3d} {dd['l']:>3d} {wr_val:>5.0%} "
              f"{dd['pnl']:>+7.2f} {cum_pnl:>+7.2f}")

    n_days = winning_days + losing_days
    if n_days:
        print(f"\n  Winning days: {winning_days}/{n_days} ({winning_days/n_days:.0%})")
        print(f"  Max drawdown: ${max_drawdown:.2f}")

    # Profit factor
    win_pnl = sum(sim_pnl_1lot(d["price"], True) for d in signals if d["won"])
    loss_pnl_total = sum(sim_pnl_1lot(d["price"], False) for d in signals if not d["won"])
    pf = profit_factor(win_pnl, loss_pnl_total)

    subheader("Risk Metrics")
    print(f"  Profit Factor: {pf:.2f}")
    print(f"  Max Drawdown:  ${max_drawdown:.2f}")

    # Streak analysis
    current_streak = 0
    max_win_streak = 0
    max_loss_streak = 0
    for d in signals:
        if d["won"]:
            if current_streak > 0:
                current_streak += 1
            else:
                current_streak = 1
            max_win_streak = max(max_win_streak, current_streak)
        else:
            if current_streak < 0:
                current_streak -= 1
            else:
                current_streak = -1
            max_loss_streak = max(max_loss_streak, abs(current_streak))

    print(f"  Max win streak:  {max_win_streak}")
    print(f"  Max loss streak: {max_loss_streak}")

    # Concentration: what % of PnL comes from top 5 trades
    trade_pnls = sorted([sim_pnl_1lot(d["price"], d["won"]) for d in signals], reverse=True)
    total_pnl = sum(trade_pnls)
    if total_pnl > 0:
        top5_pnl = sum(trade_pnls[:5])
        concentration = top5_pnl / total_pnl
        print(f"  Top 5 trade concentration: {concentration:.0%} of total PnL")
        if concentration > 0.80:
            print(f"  *** HIGH CONCENTRATION -- profits depend on a few trades")

    return {
        "profit_factor": pf,
        "max_drawdown": max_drawdown,
        "winning_days": winning_days,
        "losing_days": losing_days,
    }


# ============================================================================
#  Section 12: Price x STC Cross-Tab
# ============================================================================

def section_cross_tab(data: List[Dict]) -> Dict:
    header("12. PRICE x STC CROSS-TAB")

    signals = [d for d in data if d["is_signal"]]
    if not signals:
        print("  No data.")
        return {}

    price_labels = ["<75c", "75-79c", "80-84c", "85-89c", "90c+"]
    stc_labels = ["<600s", "600-1200s", "1200s+"]

    def price_key(p):
        if p < 75: return "<75c"
        if p < 80: return "75-79c"
        if p < 85: return "80-84c"
        if p < 90: return "85-89c"
        return "90c+"

    def stc_key(s):
        if s < 600: return "<600s"
        if s < 1200: return "600-1200s"
        return "1200s+"

    grid = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0})
    for d in signals:
        pk = price_key(d["price"])
        sk = stc_key(d["stc"])
        if d["won"]:
            grid[(pk, sk)]["w"] += 1
        else:
            grid[(pk, sk)]["l"] += 1
        grid[(pk, sk)]["pnl"] += sim_pnl_1lot(d["price"], d["won"]) * (d.get("position_size") or 1)

    print(f"\n  Format: W/L WR $PnL")
    print(f"\n  {'':>10s}", end="")
    for sk in stc_labels:
        print(f"  {sk:>20s}", end="")
    print()
    print(f"  {'-' * 72}")

    for pk in price_labels:
        print(f"  {pk:>10s}", end="")
        for sk in stc_labels:
            cell = grid[(pk, sk)]
            n = cell["w"] + cell["l"]
            if n == 0:
                print(f"  {'---':>20s}", end="")
            else:
                wr = cell["w"] / n
                print(f"  {cell['w']}W/{cell['l']}L {wr:.0%} ${cell['pnl']:+.0f}", end="")
        print()

    # Find best zone
    cells = [(k, v) for k, v in grid.items() if v["w"] + v["l"] >= 3]
    if cells:
        best = max(cells, key=lambda x: x[1]["pnl"])
        worst = min(cells, key=lambda x: x[1]["pnl"])
        print(f"\n  Best zone:  {best[0][0]} x {best[0][1]} -- ${best[1]['pnl']:+.2f} "
              f"(n={best[1]['w']+best[1]['l']}){significance_tag(best[1]['w']+best[1]['l'])}")
        print(f"  Worst zone: {worst[0][0]} x {worst[0][1]} -- ${worst[1]['pnl']:+.2f} "
              f"(n={worst[1]['w']+worst[1]['l']}){significance_tag(worst[1]['w']+worst[1]['l'])}")

    return {}


# ============================================================================
#  Section 13: Temperature Tournament (shadow columns)
# ============================================================================

def section_temp_tournament(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    header("13. TEMPERATURE TOURNAMENT (Shadow Columns)")

    wc = where_clause(since)
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
        print("  (SPX currently uses T=1.0 -- shadow columns may not be populated)")
        return {}

    variants = [
        ("pre_temp (raw)", "hourly_pre_temp_prob"),
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
    print(f"  {'-' * 62}")

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
            bs = brier_sum / count
            avg_pred = pred_sum / count
            if name == "current (final)":
                current_brier = bs
            diff = ""
            if current_brier is not None and name != "current (final)":
                d = bs - current_brier
                diff = f"{d:>+10.4f}"
            results.append((bs, name, count, avg_pred))
            print(f"  {name:<25s} {bs:>7.4f} {count:>5d} {avg_pred:>7.4f} {diff}")

    if results:
        best = min(results, key=lambda x: x[0])
        worst = max(results, key=lambda x: x[0])
        print(f"\n  Best:  {best[1]} (Brier={best[0]:.4f}, n={best[2]})")
        print(f"  Worst: {worst[1]} (Brier={worst[0]:.4f}, n={worst[2]})")
        if best[2] < 50:
            print(f"  *** n={best[2]} -- NOT SIGNIFICANT for Brier comparison")

    return {"variants": [{"name": n, "brier": b, "n": c} for b, n, c, _ in results]}


# ============================================================================
#  Section 14: Data Quality Audit
# ============================================================================

def section_data_quality(conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    header("14. DATA QUALITY AUDIT")

    wc = where_clause(since)
    total = conn.execute(f"""
        SELECT COUNT(*) FROM evaluated_opportunities
        WHERE product_type='spx_hourly' AND filter_stage='spx_observation' {wc}
    """).fetchone()[0]

    if total == 0:
        print("  No spx_observation entries.")
        return {}

    cols = [
        "market_price", "calibrated_prob", "edge", "fee_adjusted_edge",
        "kelly_f", "position_size", "seconds_to_close", "market_result",
        "egarch_sigma", "egarch_blend_sigma", "egarch_blend_weight",
        "mz_r_squared", "z_score", "vol_regime", "raw_prob",
        "calibration_method", "counterfactual", "volatility",
        "hourly_pre_temp_prob", "hourly_applied_temp_t",
    ]

    subheader(f"Column Fill Rates (n={total} spx_observation rows)")
    print(f"  {'Column':<30s} {'Filled':>12s} {'Status':>10s}")
    print(f"  {'-' * 54}")

    issues = 0
    for col in cols:
        try:
            r = conn.execute(f"""
                SELECT SUM(CASE WHEN {col} IS NOT NULL THEN 1 ELSE 0 END) as filled
                FROM evaluated_opportunities
                WHERE product_type='spx_hourly' AND filter_stage='spx_observation' {wc}
            """).fetchone()
            filled = r["filled"] or 0
            rate = filled / total
            status = "OK" if rate > 0.9 else ("PARTIAL" if rate > 0 else "EMPTY")
            if status != "OK":
                issues += 1
            print(f"  {col:<30s} {filled:>5d}/{total} ({rate:>5.1%}) {status:>8s}")
        except Exception:
            print(f"  {col:<30s} {'NOT FOUND':>12s}")
            issues += 1

    # CalEngine observation pipeline
    subheader("CalEngine Observation Pipeline")
    cal_row = conn.execute(f"""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN raw_prob IS NOT NULL THEN 1 ELSE 0 END) AS with_raw_prob,
               SUM(CASE WHEN market_result IS NOT NULL AND raw_prob IS NOT NULL THEN 1 ELSE 0 END) AS cal_eligible
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' {wc}
    """).fetchone()
    print(f"  Total SPX evals:          {cal_row[0]}")
    print(f"  With raw_prob:            {cal_row[1] or 0}")
    print(f"  Settled + raw_prob (cal):  {cal_row[2] or 0}")

    return {"total": total, "issues": issues}


# ============================================================================
#  Section 15: Readiness Assessment
# ============================================================================

def section_readiness(data: List[Dict], conn: sqlite3.Connection, since: Optional[str]) -> Dict:
    header("15. READINESS ASSESSMENT FOR LIVE TRADING")

    wc = where_clause(since)
    signals = [d for d in data if d["is_signal"]]

    n_days = len(set(d["date"] for d in signals if d["date"]))
    n_obs = len(signals)

    # Calibration bucket minimums
    cal_buckets = defaultdict(int)
    for d in signals:
        if d["cal_prob"] < 0.85:
            cal_buckets["low"] += 1
        elif d["cal_prob"] < 0.95:
            cal_buckets["mid"] += 1
        else:
            cal_buckets["high"] += 1
    min_bucket = min(cal_buckets.values()) if cal_buckets else 0

    # Win rate at 80c+
    p80 = [d for d in signals if d["price"] >= 80]
    p80_wr = sum(1 for d in p80 if d["won"]) / len(p80) if p80 else 0

    # Overall profitability
    flat_pnl = sum(sim_pnl_1lot(d["price"], d["won"]) for d in signals)

    # Blend weight adapting
    bw = conn.execute(f"""
        SELECT MIN(egarch_blend_weight) as mn, MAX(egarch_blend_weight) as mx
        FROM evaluated_opportunities
        WHERE product_type='spx_hourly' {wc}
    """).fetchone()
    blend_adapting = bw and bw["mn"] is not None and bw["mn"] != bw["mx"]

    # Overall WR vs breakeven
    if signals:
        overall_wr = sum(1 for d in signals if d["won"]) / len(signals)
        avg_be = sum(breakeven_wr(d["price"]) for d in signals) / len(signals)
        wr_above_be = overall_wr > avg_be
    else:
        wr_above_be = False
        overall_wr = 0
        avg_be = 0

    checks = [
        ("Trading days >= 10", n_days >= 10, f"{n_days} days"),
        ("Observations >= 100", n_obs >= 100, f"{n_obs} obs"),
        ("Min calibration bucket >= 20", min_bucket >= 20, f"min bucket n={min_bucket}"),
        ("Blend weight adapting", blend_adapting, "adapting" if blend_adapting else "STUCK"),
        ("80c+ WR > 85%", p80_wr > 0.85, f"{p80_wr:.1%} (n={len(p80)})"),
        ("Overall WR > breakeven", wr_above_be, f"WR={overall_wr:.1%} vs BE={avg_be:.1%}"),
        ("Positive flat PnL", flat_pnl > 0, f"${flat_pnl:+.2f}"),
    ]

    all_pass = True
    for label, passed, detail in checks:
        icon = "[+]" if passed else "[ ]"
        if not passed:
            all_pass = False
        print(f"  {icon} {label}: {detail}")

    if all_pass:
        print(f"\n  *** ALL CHECKS PASS -- ready for config tuning phase")
    else:
        failed = sum(1 for _, p, _ in checks if not p)
        print(f"\n  {failed}/{len(checks)} checks failing -- continue data collection")

    return {
        "all_pass": all_pass,
        "trading_days": n_days,
        "observations": n_obs,
        "overall_wr": overall_wr,
        "flat_pnl": flat_pnl,
    }


# ============================================================================
#  Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="SPX Hourly Alpha Research -- comprehensive profitability analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python3 scripts/spx_alpha_research.py --db /tmp/state.db
    python3 scripts/spx_alpha_research.py --db /tmp/state.db --since 2026-03-04
    python3 scripts/spx_alpha_research.py --db /tmp/state.db --json spx_alpha.json
        """,
    )
    parser.add_argument("--db", default="state.db", help="Path to state.db")
    parser.add_argument("--since", default=None, help="Only analyze data since YYYY-MM-DD")
    parser.add_argument("--json", default=None, help="Output JSON artifact path")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: Database not found: {args.db}")
        sys.exit(1)

    conn = connect_db(args.db)

    # Verify data exists
    count = conn.execute(
        "SELECT COUNT(*) FROM evaluated_opportunities WHERE product_type='spx_hourly'"
    ).fetchone()[0]
    if count == 0:
        print("No SPX hourly data found in evaluated_opportunities.")
        sys.exit(0)

    since = args.since

    print(f"\nSPX Hourly Alpha Research -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    if since:
        print(f"Filtering to data since: {since}")
    print(f"Database: {args.db} ({count} SPX evaluations)")

    # Load data
    data = load_spx_data(conn, since)
    signals = [d for d in data if d["is_signal"]]
    print(f"Settled evaluations: {len(data)} (signals: {len(signals)})")

    # Run all sections
    results = {}
    results["performance"] = section_performance(data)
    results["egarch_blend"] = section_egarch_blend(data)
    results["vix_regime"] = section_vix_regime(data, conn, since)
    results["intraday"] = section_intraday(data)
    results["price_tiers"] = section_price_tiers(data)
    results["window_correlation"] = section_window_correlation(data)
    results["edge_integrity"] = section_edge_integrity(data)
    results["calibration"] = section_calibration(data)
    results["trading_hours"] = section_trading_hours(data)
    results["counterfactual"] = section_counterfactual(data)
    results["robustness"] = section_robustness(data)
    results["cross_tab"] = section_cross_tab(data)
    results["temp_tournament"] = section_temp_tournament(conn, since)
    results["data_quality"] = section_data_quality(conn, since)
    results["readiness"] = section_readiness(data, conn, since)

    conn.close()

    # JSON artifact
    if args.json:
        artifact = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "since": since,
            **results,
        }
        with open(args.json, "w") as f:
            json.dump(artifact, f, indent=2, default=str)
        print(f"\nJSON artifact written to: {args.json}")

    print(f"\n{'=' * 78}")
    print(f"  SPX ALPHA RESEARCH COMPLETE")
    print(f"{'=' * 78}")


if __name__ == "__main__":
    main()
