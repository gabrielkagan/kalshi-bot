#!/usr/bin/env python3
"""15-minute crypto alpha research script.

Deep-dive alpha analysis for the live 15M prediction market trading system.
Goes beyond the live audit with regime detection, edge inversion checks,
loss clustering, calibration diagnostics, execution analysis, and
counterfactual simulations with statistical robustness checks.

Usage:
    scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
    python scripts/15m_alpha_research.py [--db /tmp/state.db] [--regime auto]
    python scripts/15m_alpha_research.py --db /tmp/state.db --since 2026-03-03
    python scripts/15m_alpha_research.py --db /tmp/state.db --asset BTC
    python scripts/15m_alpha_research.py --db /tmp/state.db --section calibration
"""

import argparse
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple


# ── Constants ────────────────────────────────────────────────────

MIN_EDGE_BY_PRICE = [
    (86, 88, 0.0025),
    (89, 90, 0.0025),
    (91, 92, 0.0035),
    (93, 94, 0.009),
    (95, 96, 0.0125),
    (97, 99, 0.020),
]

SETTLED_15M_FILTER = "AND event_ticker NOT LIKE '%D-%'"
EVAL_15M_FILTER = ("AND (product_type IS NULL OR product_type NOT IN "
                    "('hourly', 'weather', 'sports', 'spx_hourly'))")


# ── Helpers ──────────────────────────────────────────────────────

def connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def section(title: str) -> None:
    print(f"\n{'=' * 76}")
    print(f"  {title}")
    print(f"{'=' * 76}\n")


def subsection(title: str) -> None:
    print(f"\n--- {title} ---")


def wilson_ci(wins: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson score confidence interval for a binomial proportion."""
    if n == 0:
        return (0.0, 1.0)
    p_hat = wins / n
    denom = 1 + z * z / n
    centre = (p_hat + z * z / (2 * n)) / denom
    half = z * math.sqrt((p_hat * (1 - p_hat) + z * z / (4 * n)) / n) / denom
    return (max(0, centre - half), min(1, centre + half))


def fisher_exact_2x2(a: int, b: int, c: int, d: int) -> float:
    """One-sided Fisher exact test p-value for [[a,b],[c,d]].
    Tests if group 1 (a wins, b losses) has significantly higher WR
    than group 2 (c wins, d losses)."""
    n = a + b + c + d
    if n == 0:
        return 1.0

    def log_fact(x: int) -> float:
        return sum(math.log(i) for i in range(1, x + 1)) if x > 0 else 0.0

    def log_hyper(aa: int) -> float:
        r1 = a + b
        r2 = c + d
        c1 = a + c
        bb2 = r1 - aa
        cc2 = c1 - aa
        dd2 = r2 - cc2
        if bb2 < 0 or cc2 < 0 or dd2 < 0:
            return float('-inf')
        return (log_fact(r1) + log_fact(r2) + log_fact(c1) + log_fact(b + d)
                - log_fact(n) - log_fact(aa) - log_fact(bb2)
                - log_fact(cc2) - log_fact(dd2))

    r1 = a + b
    c1 = a + c
    r2 = c + d
    p_obs = log_hyper(a)
    p_sum = 0.0
    lo = max(0, c1 - r2)
    hi = min(r1, c1)
    for aa in range(lo, hi + 1):
        lp = log_hyper(aa)
        if lp <= p_obs + 1e-10:
            p_sum += math.exp(lp)
    return min(1.0, p_sum)


def sim_pnl_maker_unit(price: int, won: bool) -> float:
    """Simulate per-contract PnL for a 1-contract maker trade at given price."""
    fee = math.ceil(0.0175 * price * (100 - price) / 100)
    return ((100 - price) - fee) if won else (-price - fee)


def sim_pnl_taker_unit(price: int, won: bool) -> float:
    """Simulate per-contract PnL for a 1-contract taker trade at given price."""
    fee = math.ceil(0.07 * price * (100 - price) / 100)
    return ((100 - price) - fee) if won else (-price - fee)


def edge_tier_threshold(price: int) -> float:
    """Get the MIN_EDGE_BY_PRICE threshold for a given price."""
    for lo, hi, thresh in MIN_EDGE_BY_PRICE:
        if lo <= price <= hi:
            return thresh
    return 0.0025  # default


def breakeven_wr(price: int, maker: bool = True) -> float:
    """Compute breakeven win rate at a given price with fees."""
    fee = math.ceil((0.0175 if maker else 0.07) * price * (100 - price) / 100)
    return (price + fee) / 100.0


def sig_str(p: float) -> str:
    if p < 0.001:
        return "***"
    elif p < 0.01:
        return "**"
    elif p < 0.05:
        return "*"
    return "NS"


def detect_regime_start(conn: sqlite3.Connection) -> str:
    """Auto-detect regime start via git diff of config constants.
    Falls back to 2026-02-28 if git is unavailable."""
    import subprocess

    REGIME_CONSTANTS = [
        "MIN_EDGE_BY_PRICE", "MIN_EDGE_PCT", "MIN_ENTRY_PRICE",
        "MAX_ENTRY_PRICE", "MAX_SECONDS_BEFORE_CLOSE",
        "MARKET_BLEND_W", "MAX_RISK_PER_TRADE", "KELLY_FRACTION",
        "DIRECT_TAKER_THRESHOLD", "MAKER_ONLY_THRESHOLD",
        "STC_SHADOW_THRESHOLD", "OBSERVATION_MODE",
        "XRP_MAX_RISK_PER_TRADE", "SIZING_TIERS",
    ]

    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    try:
        result = subprocess.run(
            ["git", "log", "--format=%H %aI", "--since=30 days ago",
             "--", "bot.py"],
            capture_output=True, text=True, timeout=10, cwd=repo_dir,
        )
        if result.returncode != 0:
            return "2026-02-28T00:00:00"

        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split(" ", 1)
            commit_hash = parts[0]
            timestamp = parts[1] if len(parts) > 1 else ""

            diff_result = subprocess.run(
                ["git", "diff", f"{commit_hash}^..{commit_hash}",
                 "--", "bot.py"],
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


# ── Section 1: Regime Detection & Performance ────────────────────

def regime_performance(conn: sqlite3.Connection, since: str,
                       asset_filter: Optional[str] = None) -> dict:
    section("1. REGIME DETECTION & PERFORMANCE")
    ac = f"AND asset = '{asset_filter}'" if asset_filter else ""

    # Regime info
    print(f"  Regime start: {since}")

    row = conn.execute(f"""
        SELECT COUNT(*) AS trades,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) AS losses,
          SUM(pnl_cents) AS pnl,
          SUM(fee_cents) AS fees,
          SUM(count) AS contracts,
          SUM(count * entry_price_cents) AS risk_cents,
          ROUND(AVG(entry_price_cents), 1) AS avg_price,
          ROUND(AVG(seconds_to_close), 1) AS avg_stc,
          ROUND(AVG(fill_latency_seconds), 2) AS avg_latency,
          MIN(settled_at) AS first_t,
          MAX(settled_at) AS last_t
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {ac}
    """, (since,)).fetchone()

    trades = row["trades"] or 0
    wins = row["wins"] or 0
    losses = row["losses"] or 0
    pnl = row["pnl"] or 0
    fees = row["fees"] or 0
    risk = row["risk_cents"] or 0

    if trades == 0:
        print("  No trades in regime window.")
        return {"trades": 0}

    wr = wins / trades * 100
    lo, hi = wilson_ci(wins, trades)

    # Compute days
    try:
        t1 = datetime.fromisoformat((row["first_t"] or "").replace("Z", ""))
        t2 = datetime.fromisoformat((row["last_t"] or "").replace("Z", ""))
        n_days = max((t2 - t1).total_seconds() / 86400, 0.5)
    except Exception:
        n_days = 1.0

    print(f"  Period:         {(row['first_t'] or '')[:16]} to "
          f"{(row['last_t'] or '')[:16]} ({n_days:.1f} days)")
    print(f"  Trades:         {trades} ({wins}W / {losses}L)")
    print(f"  Win rate:       {wr:.1f}% [Wilson 95% CI: "
          f"{lo*100:.1f}%-{hi*100:.1f}%]")
    print(f"  Net PnL:        ${pnl/100:.2f} (${pnl/100/n_days:.2f}/day)")
    print(f"  Total fees:     ${fees/100:.2f}")
    if risk > 0:
        print(f"  Capital risked: ${risk/100:.2f} (return: {pnl/risk*100:.1f}%)")
    print(f"  Avg entry:      {row['avg_price']}c, Avg STC: "
          f"{row['avg_stc']}s ({(row['avg_stc'] or 0)/60:.1f}m)")
    print(f"  Avg fill lat:   {row['avg_latency']}s")

    # Rolling performance (3-day windows)
    subsection("Rolling 3-day performance")
    days = conn.execute(f"""
        SELECT date(settled_at) AS day,
          COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
          SUM(pnl_cents) AS pnl
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {ac}
        GROUP BY day ORDER BY day
    """, (since,)).fetchall()

    if len(days) >= 3:
        print(f"  {'Window':<25} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} "
              f"{'PnL':>10}")
        print("  " + "-" * 55)
        for i in range(len(days) - 2):
            d0, d1, d2 = days[i], days[i+1], days[i+2]
            tn = (d0["n"] or 0) + (d1["n"] or 0) + (d2["n"] or 0)
            tw = (d0["w"] or 0) + (d1["w"] or 0) + (d2["w"] or 0)
            tl = tn - tw
            tp = (d0["pnl"] or 0) + (d1["pnl"] or 0) + (d2["pnl"] or 0)
            twr = tw / tn * 100 if tn > 0 else 0
            print(f"  {d0['day']} to {d2['day']}  {tn:>4} {tw:>3} {tl:>3} "
                  f"{twr:>5.1f}% ${tp/100:>9.2f}")

    return {"trades": trades, "wins": wins, "losses": losses,
            "pnl": pnl, "fees": fees, "wr": wr, "n_days": n_days}


# ── Section 2: Per-Asset Alpha ───────────────────────────────────

def per_asset_alpha(conn: sqlite3.Connection, since: str,
                    asset_filter: Optional[str] = None) -> None:
    section("2. PER-ASSET ALPHA ANALYSIS")
    ac = f"AND asset = '{asset_filter}'" if asset_filter else ""

    assets = conn.execute(f"""
        SELECT asset,
          COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
          SUM(pnl_cents) AS pnl,
          SUM(fee_cents) AS fees,
          ROUND(AVG(entry_price_cents), 1) AS avg_p,
          ROUND(AVG(seconds_to_close), 1) AS avg_stc,
          ROUND(AVG(edge), 4) AS avg_edge,
          ROUND(AVG(calibrated_prob), 4) AS avg_cal,
          SUM(count * entry_price_cents) AS risk_cents,
          MIN(settled_at) AS first_t,
          MAX(settled_at) AS last_t
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {ac}
        GROUP BY asset ORDER BY pnl DESC
    """, (since,)).fetchall()

    if not assets:
        print("  No trades.")
        return

    print(f"  {'Asset':<6} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} "
          f"{'PnL':>10} {'$/trade':>8} {'Fees':>7} {'Avg P':>6} "
          f"{'Avg STC':>8} {'ROC':>6}")
    print("  " + "-" * 80)
    all_wins = sum(a["w"] or 0 for a in assets)
    all_trades = sum(a["n"] or 0 for a in assets)

    for a in assets:
        n = a["n"]
        w = a["w"] or 0
        l_ = n - w
        wr_a = w / n * 100
        roc = (a["pnl"] or 0) / (a["risk_cents"] or 1) * 100
        lo, hi = wilson_ci(w, n)
        print(f"  {a['asset']:<6} {n:>4} {w:>3} {l_:>3} {wr_a:>5.1f}% "
              f"${(a['pnl'] or 0)/100:>9.2f} "
              f"${(a['pnl'] or 0)/100/n:>7.2f} "
              f"${(a['fees'] or 0)/100:>6.2f} "
              f"{a['avg_p']:>5.0f}c {a['avg_stc']:>7.0f}s {roc:>5.1f}%")
        print(f"         Wilson CI: [{lo*100:.1f}%-{hi*100:.1f}%], "
              f"avg edge: {(a['avg_edge'] or 0)*100:.2f}%, "
              f"avg cal: {(a['avg_cal'] or 0)*100:.1f}%")

    # Fisher: pairwise asset WR comparison
    if len(assets) >= 2:
        subsection("Pairwise Fisher tests (asset WR)")
        asset_data = {a["asset"]: (a["w"] or 0, a["n"] - (a["w"] or 0))
                      for a in assets}
        tested = set()
        for a1 in asset_data:
            for a2 in asset_data:
                if a1 >= a2:
                    continue
                key = (a1, a2)
                if key in tested:
                    continue
                tested.add(key)
                w1, l1 = asset_data[a1]
                w2, l2 = asset_data[a2]
                if w1 + l1 < 5 or w2 + l2 < 5:
                    continue
                # Test if a1 > a2
                p_val = fisher_exact_2x2(w1, l1, w2, l2)
                wr1 = w1 / (w1 + l1) * 100
                wr2 = w2 / (w2 + l2) * 100
                print(f"  {a1} ({wr1:.0f}%) vs {a2} ({wr2:.0f}%): "
                      f"p={p_val:.4f} {sig_str(p_val)}")

    # Asset contribution analysis
    subsection("Asset contribution to total alpha")
    total_pnl = sum((a["pnl"] or 0) for a in assets)
    if total_pnl != 0:
        for a in assets:
            share = (a["pnl"] or 0) / total_pnl * 100
            bar = "#" * max(1, int(share / 5))
            print(f"  {a['asset']:<6} {share:>6.1f}% {bar}")


# ── Section 3: Price Tier Analysis ───────────────────────────────

def price_tier_analysis(conn: sqlite3.Connection, since: str,
                        asset_filter: Optional[str] = None) -> None:
    section("3. PRICE TIER ANALYSIS (matching MIN_EDGE_BY_PRICE)")
    ac = f"AND asset = '{asset_filter}'" if asset_filter else ""

    TIERS = [
        ("86-88c", 86, 88, 0.0025),
        ("89-90c", 89, 90, 0.0025),
        ("91-92c", 91, 92, 0.0035),
        ("93-94c", 93, 94, 0.009),
        ("95-96c", 95, 96, 0.0125),
        ("97-99c", 97, 99, 0.020),
    ]

    # Live trades by tier
    subsection("Live trades by price tier")
    print(f"  {'Tier':>8} {'Thresh':>7} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} "
          f"{'PnL':>10} {'BE WR':>6} {'Margin':>7} {'Wilson CI':>18}")
    print("  " + "-" * 90)

    for label, lo, hi, thresh in TIERS:
        row = conn.execute(f"""
            SELECT COUNT(*) AS n,
              SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
              SUM(pnl_cents) AS pnl,
              SUM(fee_cents) AS fees,
              ROUND(AVG(entry_price_cents), 1) AS avg_p
            FROM settled_trades
            WHERE settled_at >= ? {SETTLED_15M_FILTER} {ac}
              AND entry_price_cents BETWEEN ? AND ?
        """, (since, lo, hi)).fetchone()
        n = row["n"] or 0
        if n == 0:
            continue
        w = row["w"] or 0
        l_ = n - w
        wr = w / n * 100
        avg_p = int(row["avg_p"] or lo)
        be = breakeven_wr(avg_p) * 100
        margin = wr - be
        wl, wh = wilson_ci(w, n)
        print(f"  {label:>8} {thresh*100:>6.2f}% {n:>4} {w:>3} {l_:>3} "
              f"{wr:>5.1f}% ${(row['pnl'] or 0)/100:>9.2f} "
              f"{be:>5.1f}% {margin:>+6.1f}pp "
              f"[{wl*100:.1f}-{wh*100:.1f}%]")

    # Evaluated opportunities by tier (traded + rejected)
    subsection("Full eval universe by price tier (candidate + insufficient_edge)")
    ac_eval = f"AND asset = '{asset_filter}'" if asset_filter else ""
    all_evals = conn.execute(f"""
        SELECT market_price, fee_adjusted_edge, market_result, filter_stage,
               position_size, COALESCE(counterfactual_pnl, 0) AS cf_pnl
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER} {ac_eval}
          AND filter_stage IN ('candidate', 'insufficient_edge')
          AND status = 'settled' AND fee_adjusted_edge IS NOT NULL
    """, (since,)).fetchall()

    if all_evals:
        print(f"  {'Tier':>8} {'Traded':>7} {'Rej':>5} {'T WR':>6} {'R WR':>6} "
              f"{'T 1c':>7} {'T Sized':>9} {'R CF':>9} {'Verdict':<16}")
        print("  " + "-" * 85)
        for label, lo, hi, thresh in TIERS:
            traded = [r for r in all_evals
                      if lo <= (r["market_price"] or 0) <= hi
                      and r["filter_stage"] == "candidate"]
            rejected = [r for r in all_evals
                        if lo <= (r["market_price"] or 0) <= hi
                        and r["filter_stage"] == "insufficient_edge"]
            if not traded and not rejected:
                continue
            tw = sum(1 for r in traded if r["market_result"] == "yes")
            tl = len(traded) - tw
            twr = tw / len(traded) * 100 if traded else 0
            tpnl_1c = sum(sim_pnl_maker_unit(r["market_price"],
                          r["market_result"] == "yes") for r in traded) / 100
            tpnl_sz = sum(sim_pnl_maker_unit(r["market_price"],
                          r["market_result"] == "yes")
                          * (r["position_size"] or 1)
                          for r in traded) / 100
            rw = sum(1 for r in rejected if r["market_result"] == "yes")
            rl = len(rejected) - rw
            rwr = rw / len(rejected) * 100 if rejected else 0
            rcf = sum(r["cf_pnl"] for r in rejected) / 100
            verdict = ""
            if rejected and rcf > 0 and rwr > 80:
                verdict = "LOOSEN?"
            elif rejected and rcf < 0:
                verdict = "CORRECT"
            elif not rejected:
                verdict = "NO REJECTIONS"
            else:
                verdict = "MONITOR"
            print(f"  {label:>8} {len(traded):>4}({tw}W) {len(rejected):>5} "
                  f"{twr:>5.1f}% {rwr:>5.1f}% "
                  f"${tpnl_1c:>6.2f} ${tpnl_sz:>8.2f} "
                  f"${rcf:>8.2f} {verdict:<16}")


# ── Section 4: STC Analysis ─────────────────────────────────────

def stc_analysis(conn: sqlite3.Connection, since: str,
                 asset_filter: Optional[str] = None) -> None:
    section("4. SECONDS-TO-CLOSE ANALYSIS")
    ac = f"AND asset = '{asset_filter}'" if asset_filter else ""
    ac_eval = f"AND asset = '{asset_filter}'" if asset_filter else ""

    # Granular STC buckets (live trades)
    subsection("Live trades by STC bucket")
    stc_buckets = [
        ("<60s", 0, 60), ("60-120s", 60, 120), ("120-180s", 120, 180),
        ("180-240s", 180, 240), ("240-300s", 240, 300),
        ("300-400s", 300, 400), ("400-500s", 400, 500),
    ]
    print(f"  {'STC':>10} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} "
          f"{'PnL':>10} {'$/trade':>8} {'Avg P':>6} {'BE':>5} "
          f"{'Wilson CI':>18}")
    print("  " + "-" * 90)
    for label, lo, hi in stc_buckets:
        row = conn.execute(f"""
            SELECT COUNT(*) AS n,
              SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
              SUM(pnl_cents) AS pnl,
              ROUND(AVG(entry_price_cents), 1) AS avg_p
            FROM settled_trades
            WHERE settled_at >= ? {SETTLED_15M_FILTER} {ac}
              AND seconds_to_close >= ? AND seconds_to_close < ?
        """, (since, lo, hi)).fetchone()
        n = row["n"] or 0
        if n == 0:
            continue
        w = row["w"] or 0
        wr = w / n * 100
        avg_p = int(row["avg_p"] or 90)
        be = breakeven_wr(avg_p) * 100
        wl, wh = wilson_ci(w, n)
        print(f"  {label:>10} {n:>4} {w:>3} {n-w:>3} {wr:>5.1f}% "
              f"${(row['pnl'] or 0)/100:>9.2f} "
              f"${(row['pnl'] or 0)/100/n:>7.2f} "
              f"{avg_p:>5}c {be:>4.0f}% [{wl*100:.1f}-{wh*100:.1f}%]")

    # Shadow zone (500-900s)
    subsection("Shadow zone (500-900s) counterfactual")
    shadow_buckets = [
        ("500-600s", 500, 600), ("600-700s", 600, 700),
        ("700-800s", 700, 800), ("800-900s", 800, 900),
    ]
    shadow_rows = conn.execute(f"""
        SELECT market_price, market_result, status, seconds_to_close,
               position_size
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER} {ac_eval}
          AND filter_stage = 'stc_shadow'
    """, (since,)).fetchall()

    print(f"  {'STC':>10} {'N':>4} {'W':>3} {'L':>3} {'Pend':>5} "
          f"{'1c PnL':>8} {'Sized PnL':>10} {'AvgSz':>6} {'Avg P':>6}")
    print("  " + "-" * 65)
    total_w, total_l, total_1c, total_sz = 0, 0, 0.0, 0.0
    for label, lo, hi in shadow_buckets:
        bucket = [r for r in shadow_rows
                  if lo <= (r["seconds_to_close"] or 0) < hi]
        if not bucket:
            continue
        settled = [r for r in bucket if r["status"] == "settled"
                   and r["market_result"] is not None]
        pending = len(bucket) - len(settled)
        w = sum(1 for r in settled if r["market_result"] == "yes")
        l_ = len(settled) - w
        total_w += w
        total_l += l_
        pnl_1c = sum(sim_pnl_maker_unit(r["market_price"],
                     r["market_result"] == "yes") for r in settled) / 100
        pnl_sz = sum(sim_pnl_maker_unit(r["market_price"],
                     r["market_result"] == "yes")
                     * (r["position_size"] or 1) for r in settled) / 100
        total_1c += pnl_1c
        total_sz += pnl_sz
        avg_sz = (sum((r["position_size"] or 1) for r in settled)
                  / len(settled)) if settled else 0
        avg_p = (sum(r["market_price"] for r in bucket)
                 / len(bucket)) if bucket else 0
        print(f"  {label:>10} {len(settled):>4} {w:>3} {l_:>3} {pending:>5} "
              f"${pnl_1c:>7.2f} ${pnl_sz:>9.2f} {avg_sz:>5.1f} {avg_p:>5.0f}c")

    settled_shadow = total_w + total_l
    if settled_shadow > 0:
        swr = total_w / settled_shadow * 100
        print(f"\n  Shadow total: {total_w}W/{total_l}L ({swr:.0f}% WR), "
              f"1c PnL ${total_1c:.2f}, Sized PnL ${total_sz:.2f}")
        print(f"  Data sufficiency: "
              f"{'SUFFICIENT' if settled_shadow >= 30 else 'INSUFFICIENT'} "
              f"(need 30, have {settled_shadow})")

    # STC vs WR regression (is later entry = better WR?)
    subsection("STC correlation with outcomes")
    trades = conn.execute(f"""
        SELECT seconds_to_close,
          CASE WHEN market_result='yes' THEN 1 ELSE 0 END AS won
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {ac}
          AND seconds_to_close IS NOT NULL
    """, (since,)).fetchall()
    if len(trades) >= 10:
        stc_vals = [t["seconds_to_close"] for t in trades]
        won_vals = [t["won"] for t in trades]
        n_t = len(trades)
        mean_stc = sum(stc_vals) / n_t
        mean_won = sum(won_vals) / n_t
        cov = sum((s - mean_stc) * (w - mean_won)
                   for s, w in zip(stc_vals, won_vals)) / n_t
        var_stc = sum((s - mean_stc) ** 2 for s in stc_vals) / n_t
        var_won = sum((w - mean_won) ** 2 for w in won_vals) / n_t
        if var_stc > 0 and var_won > 0:
            corr = cov / math.sqrt(var_stc * var_won)
            print(f"  Pearson r(STC, win): {corr:+.4f} "
                  f"({'later=better' if corr > 0 else 'earlier=better'})")
            # T-test for significance
            if abs(corr) < 1:
                t_stat = corr * math.sqrt((n_t - 2) / (1 - corr ** 2))
                print(f"  t-statistic: {t_stat:.2f} (n={n_t})")
        else:
            print("  Insufficient variance for correlation")


# ── Section 5: Execution Analysis ────────────────────────────────

def execution_analysis(conn: sqlite3.Connection, since: str,
                       asset_filter: Optional[str] = None) -> None:
    section("5. EXECUTION ANALYSIS")
    ac = f"AND asset = '{asset_filter}'" if asset_filter else ""

    # Strategy breakdown
    subsection("By execution strategy")
    strats = conn.execute(f"""
        SELECT strategy,
          COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
          SUM(pnl_cents) AS pnl,
          SUM(fee_cents) AS fees,
          ROUND(AVG(fill_latency_seconds), 2) AS avg_lat,
          ROUND(AVG(seconds_to_close), 1) AS avg_stc,
          ROUND(AVG(entry_price_cents), 1) AS avg_p
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {ac}
        GROUP BY strategy ORDER BY n DESC
    """, (since,)).fetchall()
    print(f"  {'Strategy':<18} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} "
          f"{'PnL':>10} {'Fees':>7} {'Lat':>6} {'STC':>7} {'Avg P':>6}")
    print("  " + "-" * 78)
    for s in strats:
        l_ = (s["n"] or 0) - (s["w"] or 0)
        wr = (s["w"] or 0) / s["n"] * 100
        print(f"  {s['strategy'] or 'NULL':<18} {s['n']:>4} {s['w']:>3} {l_:>3} "
              f"{wr:>5.1f}% ${(s['pnl'] or 0)/100:>9.2f} "
              f"${(s['fees'] or 0)/100:>6.2f} "
              f"{s['avg_lat']:>5.1f}s {s['avg_stc']:>6.0f}s "
              f"{s['avg_p']:>5.0f}c")

    # Fee classification: maker vs taker
    subsection("Fee classification (maker vs taker fills)")
    trade_rows = conn.execute(f"""
        SELECT entry_price_cents, count, fee_cents, market_result, pnl_cents
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {ac}
    """, (since,)).fetchall()
    maker_n = taker_n = 0
    maker_fees = taker_fees = 0
    maker_w = taker_w = 0
    maker_pnl = taker_pnl = 0
    for tr in trade_rows:
        p = tr["entry_price_cents"]
        c = tr["count"]
        expected_maker = math.ceil(0.0175 * c * p * (100 - p) / 100)
        expected_taker = math.ceil(0.07 * c * p * (100 - p) / 100)
        is_maker = abs(tr["fee_cents"] - expected_maker) <= abs(
            tr["fee_cents"] - expected_taker)
        won = tr["market_result"] == "yes"
        if is_maker:
            maker_n += 1
            maker_fees += tr["fee_cents"]
            maker_w += won
            maker_pnl += tr["pnl_cents"]
        else:
            taker_n += 1
            taker_fees += tr["fee_cents"]
            taker_w += won
            taker_pnl += tr["pnl_cents"]

    total_n = maker_n + taker_n
    if total_n > 0:
        print(f"  Maker: {maker_n}/{total_n} ({maker_n/total_n*100:.0f}%), "
              f"{maker_w}W/{maker_n-maker_w}L "
              f"({maker_w/maker_n*100:.0f}% WR), "
              f"PnL ${maker_pnl/100:.2f}, fees ${maker_fees/100:.2f}")
        if taker_n > 0:
            print(f"  Taker: {taker_n}/{total_n} ({taker_n/total_n*100:.0f}%), "
                  f"{taker_w}W/{taker_n-taker_w}L "
                  f"({taker_w/taker_n*100:.0f}% WR), "
                  f"PnL ${taker_pnl/100:.2f}, fees ${taker_fees/100:.2f}")
            # Fisher test
            p_val = fisher_exact_2x2(maker_w, maker_n - maker_w,
                                     taker_w, taker_n - taker_w)
            print(f"  Fisher (maker vs taker WR): p={p_val:.4f} {sig_str(p_val)}")
            # Fee savings
            all_maker_fee = sum(
                math.ceil(0.0175 * tr["count"] * tr["entry_price_cents"]
                          * (100 - tr["entry_price_cents"]) / 100)
                for tr in trade_rows)
            actual_fees = sum(tr["fee_cents"] for tr in trade_rows)
            print(f"  Taker fee premium: ${(actual_fees - all_maker_fee)/100:.2f} "
                  f"extra vs all-maker scenario")

    # Escalation analysis
    subsection("By escalation type")
    escs = conn.execute(f"""
        SELECT COALESCE(LOWER(escalation_type), 'none') AS esc,
          COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
          SUM(pnl_cents) AS pnl
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {ac}
        GROUP BY esc ORDER BY n DESC
    """, (since,)).fetchall()
    for e in escs:
        l_ = (e["n"] or 0) - (e["w"] or 0)
        wr = (e["w"] or 0) / e["n"] * 100
        print(f"  {e['esc']:<18} {e['n']:>4} {e['w']:>3}W/{l_}L "
              f"({wr:.0f}%) ${(e['pnl'] or 0)/100:.2f}")

    # Fill latency impact
    subsection("Fill latency vs outcome")
    lat_buckets = [
        ("<1s", 0, 1), ("1-3s", 1, 3), ("3-10s", 3, 10),
        ("10-30s", 10, 30), ("30s+", 30, 9999),
    ]
    print(f"  {'Latency':>8} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} {'PnL':>10}")
    print("  " + "-" * 42)
    for label, lo, hi in lat_buckets:
        row = conn.execute(f"""
            SELECT COUNT(*) AS n,
              SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
              SUM(pnl_cents) AS pnl
            FROM settled_trades
            WHERE settled_at >= ? {SETTLED_15M_FILTER} {ac}
              AND fill_latency_seconds >= ? AND fill_latency_seconds < ?
        """, (since, lo, hi)).fetchone()
        n = row["n"] or 0
        if n == 0:
            continue
        w = row["w"] or 0
        print(f"  {label:>8} {n:>4} {w:>3} {n-w:>3} "
              f"{w/n*100:>5.1f}% ${(row['pnl'] or 0)/100:>9.2f}")


# ── Section 6: Calibration Diagnostics ───────────────────────────

def calibration_diagnostics(conn: sqlite3.Connection, since: str,
                            asset_filter: Optional[str] = None) -> None:
    section("6. CALIBRATION DIAGNOSTICS")
    ac = f"AND asset = '{asset_filter}'" if asset_filter else ""

    # Predicted vs actual by probability bucket
    subsection("Predicted vs actual WR by calibrated_prob bucket")
    cal_buckets = [
        ("<88%", 0, 0.88), ("88-90%", 0.88, 0.90), ("90-92%", 0.90, 0.92),
        ("92-94%", 0.92, 0.94), ("94-96%", 0.94, 0.96), ("96%+", 0.96, 1.01),
    ]
    print(f"  {'Bucket':>10} {'N':>4} {'W':>3} {'L':>3} "
          f"{'Predicted':>10} {'Actual':>8} {'Delta':>8} {'Brier':>7}")
    print("  " + "-" * 62)
    total_brier = 0.0
    total_n = 0
    for label, lo, hi in cal_buckets:
        rows = conn.execute(f"""
            SELECT calibrated_prob,
              CASE WHEN market_result='yes' THEN 1 ELSE 0 END AS won
            FROM settled_trades
            WHERE settled_at >= ? {SETTLED_15M_FILTER} {ac}
              AND calibrated_prob >= ? AND calibrated_prob < ?
              AND calibrated_prob IS NOT NULL
        """, (since, lo, hi)).fetchall()
        n = len(rows)
        if n == 0:
            continue
        w = sum(r["won"] for r in rows)
        l_ = n - w
        pred = sum(r["calibrated_prob"] for r in rows) / n * 100
        actual = w / n * 100
        brier = sum((r["calibrated_prob"] - r["won"]) ** 2 for r in rows) / n
        total_brier += sum((r["calibrated_prob"] - r["won"]) ** 2 for r in rows)
        total_n += n
        print(f"  {label:>10} {n:>4} {w:>3} {l_:>3} "
              f"{pred:>9.1f}% {actual:>7.1f}% {actual - pred:>+7.1f}pp "
              f"{brier:>.4f}")

    if total_n > 0:
        overall_brier = total_brier / total_n
        print(f"\n  Overall Brier score: {overall_brier:.4f}")
        print(f"  (Lower is better; perfect=0, random@90%=0.09)")

    # Overconfidence check: model says 95% but actual is 85%?
    subsection("Overconfidence/underconfidence summary")
    all_cal = conn.execute(f"""
        SELECT calibrated_prob,
          CASE WHEN market_result='yes' THEN 1 ELSE 0 END AS won
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {ac}
          AND calibrated_prob IS NOT NULL
    """, (since,)).fetchall()
    if all_cal:
        avg_pred = sum(r["calibrated_prob"] for r in all_cal) / len(all_cal)
        avg_actual = sum(r["won"] for r in all_cal) / len(all_cal)
        delta = (avg_actual - avg_pred) * 100
        direction = "UNDERCONFIDENT" if delta > 0 else "OVERCONFIDENT"
        print(f"  Avg predicted: {avg_pred*100:.1f}%")
        print(f"  Avg actual:    {avg_actual*100:.1f}%")
        print(f"  Delta:         {delta:+.1f}pp ({direction})")
        if abs(delta) > 3:
            print(f"  WARNING: {abs(delta):.1f}pp miscalibration is significant")

    # Per-asset calibration
    subsection("Per-asset calibration")
    asset_cal = conn.execute(f"""
        SELECT asset,
          COUNT(*) AS n,
          AVG(calibrated_prob) AS avg_pred,
          AVG(CASE WHEN market_result='yes' THEN 1.0 ELSE 0.0 END) AS avg_actual
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {ac}
          AND calibrated_prob IS NOT NULL
        GROUP BY asset ORDER BY asset
    """, (since,)).fetchall()
    print(f"  {'Asset':<6} {'N':>4} {'Predicted':>10} {'Actual':>8} {'Delta':>8}")
    print("  " + "-" * 40)
    for a in asset_cal:
        pred = (a["avg_pred"] or 0) * 100
        actual = (a["avg_actual"] or 0) * 100
        print(f"  {a['asset']:<6} {a['n']:>4} {pred:>9.1f}% "
              f"{actual:>7.1f}% {actual - pred:>+7.1f}pp")


# ── Section 7: Edge Inversion Check ──────────────────────────────

def edge_inversion_check(conn: sqlite3.Connection, since: str,
                         asset_filter: Optional[str] = None) -> None:
    section("7. EDGE INVERSION CHECK")
    ac = f"AND asset = '{asset_filter}'" if asset_filter else ""

    print("  (Higher edge should = higher WR. An inversion is a red flag.)\n")

    # Edge quintiles
    trades = conn.execute(f"""
        SELECT edge,
          CASE WHEN market_result='yes' THEN 1 ELSE 0 END AS won,
          entry_price_cents, pnl_cents
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {ac}
          AND edge IS NOT NULL
        ORDER BY edge
    """, (since,)).fetchall()

    n = len(trades)
    if n < 10:
        print(f"  Only {n} trades with edge data -- need 10+")
        return

    # Split into quintiles
    q_size = n // 5
    quintiles = []
    for i in range(5):
        start = i * q_size
        end = start + q_size if i < 4 else n
        q = trades[start:end]
        w = sum(r["won"] for r in q)
        l_ = len(q) - w
        wr = w / len(q) * 100
        avg_edge = sum(r["edge"] for r in q) / len(q)
        pnl = sum(r["pnl_cents"] for r in q)
        quintiles.append((i + 1, len(q), w, l_, wr, avg_edge, pnl))

    print(f"  {'Q':>4} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} "
          f"{'Avg Edge':>9} {'PnL':>10}")
    print("  " + "-" * 46)
    for q, qn, w, l_, wr, ae, pnl in quintiles:
        print(f"  Q{q:>3} {qn:>4} {w:>3} {l_:>3} {wr:>5.1f}% "
              f"{ae*100:>+8.2f}% ${pnl/100:>9.2f}")

    # Check monotonicity
    wrs = [q[4] for q in quintiles]
    inversions = sum(1 for i in range(len(wrs) - 1) if wrs[i] > wrs[i+1])
    if inversions == 0:
        print("\n  MONOTONIC: Higher edge = higher WR (no inversions)")
    else:
        print(f"\n  WARNING: {inversions} inversion(s) detected "
              f"(higher edge != higher WR)")

    # Rank correlation (Spearman-like on quintile medians)
    edges = [r["edge"] for r in trades]
    wons = [r["won"] for r in trades]
    # Point-biserial correlation
    mean_e = sum(edges) / n
    mean_w = sum(wons) / n
    cov = sum((e - mean_e) * (w - mean_w) for e, w in zip(edges, wons)) / n
    var_e = sum((e - mean_e) ** 2 for e in edges) / n
    var_w = sum((w - mean_w) ** 2 for w in wons) / n
    if var_e > 0 and var_w > 0:
        r_pb = cov / math.sqrt(var_e * var_w)
        print(f"  Point-biserial r(edge, win): {r_pb:+.4f}")
        if r_pb < 0:
            print("  ALERT: Negative correlation -- edge signal may be broken!")


# ── Section 8: Counterfactual Simulations ────────────────────────

def counterfactual_simulations(conn: sqlite3.Connection, since: str,
                               asset_filter: Optional[str] = None) -> None:
    section("8. COUNTERFACTUAL SIMULATIONS")
    ac = f"AND asset = '{asset_filter}'" if asset_filter else ""
    ac_eval = f"AND asset = '{asset_filter}'" if asset_filter else ""

    # Full universe
    rows = conn.execute(f"""
        SELECT market_price, fee_adjusted_edge, market_result, asset,
               seconds_to_close, filter_stage, position_size,
               COALESCE(counterfactual_pnl, 0) AS cf_pnl
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER} {ac_eval}
          AND filter_stage IN ('candidate', 'insufficient_edge',
                               'stc_shadow', 'price_out_of_range')
          AND status = 'settled'
          AND fee_adjusted_edge IS NOT NULL
          AND market_price IS NOT NULL
          AND market_result IS NOT NULL
    """, (since,)).fetchall()

    if len(rows) < 5:
        print(f"  Only {len(rows)} settled evaluations -- need 5+")
        return

    # Compute days
    ts = conn.execute(f"""
        SELECT MIN(evaluation_time), MAX(evaluation_time)
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER}
          AND filter_stage IN ('candidate', 'insufficient_edge')
    """, (since,)).fetchone()
    try:
        t1 = datetime.fromisoformat((ts[0] or "").replace("Z", ""))
        t2 = datetime.fromisoformat((ts[1] or "").replace("Z", ""))
        n_days = max((t2 - t1).total_seconds() / 86400, 0.5)
    except Exception:
        n_days = 1.0

    def sim_config(label: str, pred_fn):
        """Simulate a filter config and report results."""
        sub = [r for r in rows if pred_fn(r)]
        if not sub:
            return None
        w = sum(1 for r in sub if r["market_result"] == "yes")
        l_ = len(sub) - w
        wr = w / len(sub) * 100
        pnl_1c = sum(sim_pnl_maker_unit(r["market_price"],
                     r["market_result"] == "yes") for r in sub) / 100
        pnl_sz = sum(sim_pnl_maker_unit(r["market_price"],
                     r["market_result"] == "yes")
                     * (r["position_size"] or 1) for r in sub) / 100
        daily_1c = pnl_1c / n_days
        daily_sz = pnl_sz / n_days
        return {
            "label": label, "n": len(sub), "w": w, "l": l_,
            "wr": wr, "pnl": pnl_1c, "daily": daily_1c,
            "pnl_sz": pnl_sz, "daily_sz": daily_sz,
        }

    # Current config baseline
    def _current(r):
        p = r["market_price"]
        e = r["fee_adjusted_edge"]
        if p < 86 or p > 99:
            return False
        stc = r["seconds_to_close"] or 0
        if stc > 500:
            return False
        return e >= edge_tier_threshold(p)

    # Config variants
    configs = [
        ("Current config (baseline)", _current),
        ("Lower MIN_ENTRY to 84c",
         lambda r: (r["market_price"] or 0) >= 84
                   and (r["market_price"] or 0) <= 99
                   and (r["seconds_to_close"] or 0) <= 500
                   and (r["fee_adjusted_edge"] or 0) >= edge_tier_threshold(
                       r["market_price"])),
        ("Expand STC to 600s",
         lambda r: (r["market_price"] or 0) >= 86
                   and (r["market_price"] or 0) <= 99
                   and (r["seconds_to_close"] or 0) <= 600
                   and (r["fee_adjusted_edge"] or 0) >= edge_tier_threshold(
                       r["market_price"])),
        ("Expand STC to 700s",
         lambda r: (r["market_price"] or 0) >= 86
                   and (r["market_price"] or 0) <= 99
                   and (r["seconds_to_close"] or 0) <= 700
                   and (r["fee_adjusted_edge"] or 0) >= edge_tier_threshold(
                       r["market_price"])),
        ("Tighten: STC <= 300s only",
         lambda r: (r["market_price"] or 0) >= 86
                   and (r["market_price"] or 0) <= 99
                   and (r["seconds_to_close"] or 0) <= 300
                   and (r["fee_adjusted_edge"] or 0) >= edge_tier_threshold(
                       r["market_price"])),
        ("Tighten: STC <= 180s only",
         lambda r: (r["market_price"] or 0) >= 86
                   and (r["market_price"] or 0) <= 99
                   and (r["seconds_to_close"] or 0) <= 180
                   and (r["fee_adjusted_edge"] or 0) >= edge_tier_threshold(
                       r["market_price"])),
        ("Halve all edge thresholds",
         lambda r: (r["market_price"] or 0) >= 86
                   and (r["market_price"] or 0) <= 99
                   and (r["seconds_to_close"] or 0) <= 500
                   and (r["fee_adjusted_edge"] or 0) >= edge_tier_threshold(
                       r["market_price"]) * 0.5),
        ("Double all edge thresholds",
         lambda r: (r["market_price"] or 0) >= 86
                   and (r["market_price"] or 0) <= 99
                   and (r["seconds_to_close"] or 0) <= 500
                   and (r["fee_adjusted_edge"] or 0) >= edge_tier_threshold(
                       r["market_price"]) * 2.0),
        ("Exclude XRP",
         lambda r: _current(r) and r["asset"] != "XRP"),
        ("Exclude SOL",
         lambda r: _current(r) and r["asset"] != "SOL"),
        ("BTC only",
         lambda r: _current(r) and r["asset"] == "BTC"),
    ]

    print(f"  Universe: {len(rows)} settled evals over {n_days:.1f} days")
    n_with_sz = sum(1 for r in rows
                    if r["position_size"] is not None and r["position_size"] > 0)
    print(f"  Position sizing available: {n_with_sz}/{len(rows)} evals")
    print(f"  (candidate + stc_shadow have sizing; "
          f"insufficient_edge/price_out_of_range use 1 contract)\n")
    print(f"  {'Config':<32} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} "
          f"{'1c PnL':>8} {'Sized PnL':>10} {'$/day sz':>9}")
    print("  " + "-" * 88)

    results = []
    for label, pred in configs:
        r = sim_config(label, pred)
        if r:
            results.append(r)
            marker = " <<" if label.startswith("Current") else ""
            print(f"  {r['label']:<32} {r['n']:>4} {r['w']:>3} {r['l']:>3} "
                  f"{r['wr']:>5.1f}% ${r['pnl']:>7.2f} "
                  f"${r['pnl_sz']:>9.2f} "
                  f"${r['daily_sz']:>8.2f}{marker}")

    # Highlight best
    if results:
        best = max(results, key=lambda x: x["daily_sz"])
        baseline = results[0] if results else None
        if baseline and best["label"] != baseline["label"]:
            delta = best["daily_sz"] - baseline["daily_sz"]
            print(f"\n  BEST (sized): {best['label']} "
                  f"(+${delta:.2f}/day vs current)")


# ── Section 9: Loss Pattern Analysis ─────────────────────────────

def loss_pattern_analysis(conn: sqlite3.Connection, since: str,
                          asset_filter: Optional[str] = None) -> None:
    section("9. LOSS PATTERN ANALYSIS")
    ac = f"AND asset = '{asset_filter}'" if asset_filter else ""

    losses = conn.execute(f"""
        SELECT ticker, asset, entry_price_cents, count, pnl_cents,
          fee_cents, seconds_to_close, strategy, escalation_type,
          fill_latency_seconds, calibrated_prob, edge, vol_regime,
          settled_at
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {ac}
          AND market_result = 'no'
        ORDER BY settled_at
    """, (since,)).fetchall()

    if not losses:
        print("  No losses in regime window!")
        return

    print(f"  Total losses: {len(losses)}")
    total_loss_pnl = sum(r["pnl_cents"] for r in losses)
    print(f"  Total loss PnL: ${total_loss_pnl/100:.2f}")
    avg_loss = total_loss_pnl / len(losses)
    print(f"  Avg loss: ${avg_loss/100:.2f}")

    # Loss detail table
    subsection("Individual losses (sorted by PnL)")
    sorted_losses = sorted(losses, key=lambda r: r["pnl_cents"])
    print(f"  {'Ticker':<32} {'Asset':<5} {'P':>3} {'Ct':>3} "
          f"{'PnL':>8} {'STC':>5} {'Edge':>6} {'Cal':>6} {'Vol':>8}")
    print("  " + "-" * 95)
    for lo in sorted_losses:
        print(f"  {(lo['ticker'] or '')[:32]:<32} {lo['asset']:<5} "
              f"{lo['entry_price_cents']:>3} {lo['count']:>3} "
              f"${lo['pnl_cents']/100:>7.2f} "
              f"{lo['seconds_to_close']:>4.0f}s "
              f"{(lo['edge'] or 0)*100:>5.2f}% "
              f"{(lo['calibrated_prob'] or 0)*100:>5.1f}% "
              f"{(lo['vol_regime'] or 'N/A'):>8}")

    # Same-hour clustering
    subsection("Loss clustering")
    loss_times = []
    for lo in losses:
        try:
            t = datetime.fromisoformat(
                (lo["settled_at"] or "").replace("Z", ""))
            loss_times.append(t)
        except Exception:
            pass

    hour_buckets: Dict[str, int] = defaultdict(int)
    for t in loss_times:
        hour_buckets[t.strftime("%Y-%m-%d %H:00")] += 1
    clusters = {k: v for k, v in hour_buckets.items() if v > 1}
    if clusters:
        print("  ALERT: Same-hour loss clusters:")
        for hr, cnt in sorted(clusters.items()):
            print(f"    {hr}: {cnt} losses")
    else:
        print("  No same-hour clustering")

    # Consecutive loss streaks
    all_trades = conn.execute(f"""
        SELECT asset, market_result, settled_at, pnl_cents
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {ac}
        ORDER BY settled_at
    """, (since,)).fetchall()

    max_streak = 0
    cur_streak = 0
    streak_pnl = 0
    max_streak_pnl = 0
    for t in all_trades:
        if t["market_result"] == "no":
            cur_streak += 1
            streak_pnl += t["pnl_cents"]
            if cur_streak > max_streak:
                max_streak = cur_streak
                max_streak_pnl = streak_pnl
        else:
            cur_streak = 0
            streak_pnl = 0
    print(f"  Max consecutive losses: {max_streak} "
          f"(PnL: ${max_streak_pnl/100:.2f})")

    # Loss concentration by asset
    subsection("Loss concentration by asset")
    asset_loss: Dict[str, int] = defaultdict(int)
    asset_total: Dict[str, int] = defaultdict(int)
    for t in all_trades:
        asset_total[t["asset"]] += 1
        if t["market_result"] == "no":
            asset_loss[t["asset"]] += 1
    for asset in sorted(asset_loss, key=lambda a: -asset_loss[a]):
        n_a = asset_total.get(asset, 0)
        l_a = asset_loss[asset]
        lr = l_a / n_a * 100 if n_a > 0 else 0
        share = l_a / len(losses) * 100
        flag = " *** CONCENTRATED" if share > 50 else ""
        print(f"  {asset:<6} {l_a}/{n_a} losses ({lr:.0f}% loss rate), "
              f"{share:.0f}% of all losses{flag}")

    # Loss by time of day
    subsection("Losses by time of day (UTC)")
    loss_hours: Dict[int, int] = defaultdict(int)
    for t in loss_times:
        loss_hours[t.hour] += 1
    if loss_hours:
        peak_hr = max(loss_hours, key=loss_hours.get)
        print(f"  Peak loss hour: {peak_hr:02d}:00 UTC "
              f"({loss_hours[peak_hr]} losses)")
        for hr in sorted(loss_hours):
            bar = "#" * loss_hours[hr]
            print(f"    {hr:02d}:00 {loss_hours[hr]:>2} {bar}")

    # Loss by STC
    subsection("Loss by STC bucket")
    loss_stc: Dict[str, int] = defaultdict(int)
    stc_labels = [
        ("<120s", 0, 120), ("120-240s", 120, 240),
        ("240-360s", 240, 360), ("360-500s", 360, 500),
    ]
    for lo in losses:
        stc = lo["seconds_to_close"] or 0
        for label, slo, shi in stc_labels:
            if slo <= stc < shi:
                loss_stc[label] += 1
                break
    for label, _, _ in stc_labels:
        if label in loss_stc:
            print(f"  {label:<10} {loss_stc[label]} losses")

    # Common characteristics of losses
    subsection("Loss fingerprint")
    avg_price = sum(lo["entry_price_cents"] for lo in losses) / len(losses)
    avg_stc = sum(lo["seconds_to_close"] or 0 for lo in losses) / len(losses)
    avg_edge = sum(lo["edge"] or 0 for lo in losses) / len(losses)
    avg_cal = sum(lo["calibrated_prob"] or 0 for lo in losses) / len(losses)
    print(f"  Avg loss price:      {avg_price:.0f}c")
    print(f"  Avg loss STC:        {avg_stc:.0f}s")
    print(f"  Avg loss edge:       {avg_edge*100:.2f}%")
    print(f"  Avg loss cal_prob:   {avg_cal*100:.1f}%")


# ── Section 10: Robustness & Statistical Tests ───────────────────

def robustness_analysis(conn: sqlite3.Connection, since: str,
                        asset_filter: Optional[str] = None) -> None:
    section("10. ROBUSTNESS & STATISTICAL TESTS")
    ac = f"AND asset = '{asset_filter}'" if asset_filter else ""

    trades = conn.execute(f"""
        SELECT entry_price_cents, pnl_cents, fee_cents,
          CASE WHEN market_result='yes' THEN 1 ELSE 0 END AS won,
          seconds_to_close, edge, calibrated_prob, asset, settled_at
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {ac}
        ORDER BY settled_at
    """, (since,)).fetchall()

    n = len(trades)
    if n < 10:
        print(f"  Only {n} trades -- need 10+ for robustness analysis")
        return

    wins = sum(t["won"] for t in trades)
    wr = wins / n

    # Wilson CI
    lo, hi = wilson_ci(wins, n)
    subsection("Wilson confidence interval")
    print(f"  n={n}, wins={wins}, WR={wr*100:.1f}%")
    print(f"  95% Wilson CI: [{lo*100:.1f}%, {hi*100:.1f}%]")
    print(f"  CI width: {(hi-lo)*100:.1f}pp")

    # Is WR significantly above breakeven?
    avg_price = sum(t["entry_price_cents"] for t in trades) / n
    be = breakeven_wr(int(avg_price))
    if lo > be:
        print(f"  WR lower bound ({lo*100:.1f}%) > breakeven ({be*100:.1f}%): "
              f"PROFITABLE WITH 95% CONFIDENCE")
    else:
        print(f"  WR lower bound ({lo*100:.1f}%) vs breakeven ({be*100:.1f}%): "
              f"NOT YET STATISTICALLY PROFITABLE")

    # Time stability: first half vs second half
    subsection("Time stability (first half vs second half)")
    mid = n // 2
    first_half = trades[:mid]
    second_half = trades[mid:]
    w1 = sum(t["won"] for t in first_half)
    w2 = sum(t["won"] for t in second_half)
    n1 = len(first_half)
    n2 = len(second_half)
    wr1 = w1 / n1 * 100
    wr2 = w2 / n2 * 100
    p_val = fisher_exact_2x2(w1, n1 - w1, w2, n2 - w2)
    print(f"  First half:  {w1}W/{n1-w1}L ({wr1:.1f}%)")
    print(f"  Second half: {w2}W/{n2-w2}L ({wr2:.1f}%)")
    print(f"  Fisher p={p_val:.4f} {sig_str(p_val)}")
    if wr2 < wr1 - 5:
        print(f"  WARNING: WR declining ({wr1:.1f}% -> {wr2:.1f}%)")
    elif wr2 > wr1 + 5:
        print(f"  IMPROVING: WR rising ({wr1:.1f}% -> {wr2:.1f}%)")
    else:
        print(f"  STABLE: WR consistent across halves")

    # Drawdown analysis
    subsection("Drawdown analysis")
    cumulative = 0
    peak = 0
    max_dd = 0
    dd_start = dd_end = None
    for t in trades:
        cumulative += t["pnl_cents"]
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
            dd_end = t["settled_at"]

    print(f"  Max drawdown: ${max_dd/100:.2f}")
    if cumulative > 0:
        print(f"  DD/PnL ratio: {max_dd/cumulative:.2f}x")
    print(f"  Final cumulative PnL: ${cumulative/100:.2f}")

    # Sharpe-like ratio (daily returns)
    subsection("Daily return analysis")
    daily_pnl: Dict[str, int] = defaultdict(int)
    for t in trades:
        day = (t["settled_at"] or "")[:10]
        if day:
            daily_pnl[day] += t["pnl_cents"]

    if len(daily_pnl) >= 3:
        vals = list(daily_pnl.values())
        mean_daily = sum(vals) / len(vals)
        var_daily = sum((v - mean_daily) ** 2 for v in vals) / len(vals)
        std_daily = math.sqrt(var_daily) if var_daily > 0 else 0
        if std_daily > 0:
            sharpe_like = mean_daily / std_daily
            print(f"  Mean daily PnL:  ${mean_daily/100:.2f}")
            print(f"  Std daily PnL:   ${std_daily/100:.2f}")
            print(f"  Daily Sharpe:    {sharpe_like:.2f}")
            # Winning days
            winning_days = sum(1 for v in vals if v > 0)
            print(f"  Winning days:    {winning_days}/{len(vals)} "
                  f"({winning_days/len(vals)*100:.0f}%)")
        else:
            print(f"  Zero variance in daily PnL")

    # Expected Kelly growth rate
    subsection("Kelly analysis")
    # Use actual WR and avg payoff ratios
    wins_data = [t for t in trades if t["won"]]
    losses_data = [t for t in trades if not t["won"]]
    if wins_data and losses_data:
        avg_win = sum(t["pnl_cents"] for t in wins_data) / len(wins_data)
        avg_loss = abs(sum(t["pnl_cents"] for t in losses_data) / len(losses_data))
        if avg_loss > 0:
            win_loss_ratio = avg_win / avg_loss
            kelly_f = wr - (1 - wr) / win_loss_ratio
            print(f"  Avg win:         ${avg_win/100:.2f}")
            print(f"  Avg loss:        ${avg_loss/100:.2f}")
            print(f"  Win/loss ratio:  {win_loss_ratio:.2f}")
            print(f"  Full Kelly f*:   {kelly_f:.3f}")
            print(f"  Quarter Kelly:   {kelly_f/4:.3f}")
            if kelly_f < 0:
                print(f"  WARNING: Negative Kelly -- system not profitable "
                      f"on risk-adjusted basis!")


# ── Section 11: Volatility Regime Analysis ───────────────────────

def vol_regime_analysis(conn: sqlite3.Connection, since: str,
                        asset_filter: Optional[str] = None) -> None:
    section("11. VOLATILITY REGIME ANALYSIS")
    ac = f"AND asset = '{asset_filter}'" if asset_filter else ""

    vol_rows = conn.execute(f"""
        SELECT vol_regime,
          COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
          SUM(pnl_cents) AS pnl,
          ROUND(AVG(entry_price_cents), 1) AS avg_p,
          ROUND(AVG(edge), 4) AS avg_edge,
          ROUND(AVG(seconds_to_close), 1) AS avg_stc
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {ac}
        GROUP BY vol_regime ORDER BY n DESC
    """, (since,)).fetchall()

    if not vol_rows:
        print("  No vol regime data.")
        return

    print(f"  {'Regime':<12} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} "
          f"{'PnL':>10} {'Avg P':>6} {'Avg Edge':>9} {'Avg STC':>8}")
    print("  " + "-" * 72)
    for v in vol_rows:
        l_ = (v["n"] or 0) - (v["w"] or 0)
        wr = (v["w"] or 0) / v["n"] * 100
        wl, wh = wilson_ci(v["w"] or 0, v["n"])
        print(f"  {v['vol_regime'] or 'NULL':<12} {v['n']:>4} {v['w']:>3} "
              f"{l_:>3} {wr:>5.1f}% ${(v['pnl'] or 0)/100:>9.2f} "
              f"{v['avg_p']:>5.0f}c {(v['avg_edge'] or 0)*100:>+8.2f}% "
              f"{v['avg_stc']:>7.0f}s")
        print(f"             Wilson CI: [{wl*100:.1f}-{wh*100:.1f}%]")


# ── Section 12: Time-of-Day Analysis ────────────────────────────

def time_of_day_analysis(conn: sqlite3.Connection, since: str,
                         asset_filter: Optional[str] = None) -> None:
    section("12. TIME-OF-DAY ANALYSIS")
    ac = f"AND asset = '{asset_filter}'" if asset_filter else ""

    tod = conn.execute(f"""
        SELECT CAST(strftime('%H', settled_at) AS INTEGER) AS hr,
          COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
          SUM(pnl_cents) AS pnl,
          ROUND(AVG(entry_price_cents), 1) AS avg_p
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {ac}
        GROUP BY hr ORDER BY hr
    """, (since,)).fetchall()

    if not tod:
        print("  No time-of-day data.")
        return

    print(f"  {'Hour':>6} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} "
          f"{'PnL':>10} {'Avg P':>6}")
    print("  " + "-" * 46)
    for t in tod:
        l_ = (t["n"] or 0) - (t["w"] or 0)
        wr = (t["w"] or 0) / t["n"] * 100
        print(f"  {t['hr']:>4}:00 {t['n']:>4} {t['w']:>3} {l_:>3} "
              f"{wr:>5.0f}% ${(t['pnl'] or 0)/100:>9.2f} "
              f"{t['avg_p'] or 0:>5.0f}c")

    # Identify best/worst hours
    if len(tod) >= 3:
        best_hr = max(tod, key=lambda t: (t["pnl"] or 0))
        worst_hr = min(tod, key=lambda t: (t["pnl"] or 0))
        print(f"\n  Best hour:  {best_hr['hr']:02d}:00 UTC "
              f"(${(best_hr['pnl'] or 0)/100:.2f})")
        print(f"  Worst hour: {worst_hr['hr']:02d}:00 UTC "
              f"(${(worst_hr['pnl'] or 0)/100:.2f})")


# ── Section 13: Shadow Approaches Alpha ──────────────────────────

def shadow_approaches_alpha(conn: sqlite3.Connection, since: str,
                            asset_filter: Optional[str] = None) -> None:
    section("13. SHADOW APPROACHES ALPHA (RecalibratedEGARCH + LightGBM)")

    tbl = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='fifteenm_shadow_signals'"
    ).fetchone()
    if not tbl:
        print("  fifteenm_shadow_signals table not found")
        return

    asset_clause = f"AND asset = '{asset_filter}'" if asset_filter else ""

    # Data health check
    subsection("Data health")
    health = conn.execute(f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) AS settled,
            MIN(evaluation_time) AS first,
            MAX(evaluation_time) AS last,
            COUNT(DISTINCT asset) AS assets
        FROM fifteenm_shadow_signals
        WHERE evaluation_time >= ? {asset_clause}
    """, (since,)).fetchone()

    total = health["total"] or 0
    settled = health["settled"] or 0
    print(f"  Total signals: {total}, Settled: {settled}")
    if total > 0:
        print(f"  Period: {health['first']} → {health['last']}")
        print(f"  Assets covered: {health['assets']}")
    if total == 0:
        print("  ⚠ No data — shadow engine may not be running")
        return

    # Per-asset A1 deep dive
    subsection("A1 (RecalibratedEGARCH) per-asset alpha")
    for asset in (["BTC", "ETH", "SOL", "XRP"] if not asset_filter else [asset_filter]):
        rows = conn.execute(f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN a1_gates_passed = 1 THEN 1 ELSE 0 END) AS passed,
                SUM(CASE WHEN status='settled' AND a1_gates_passed = 1
                    AND market_result IN ('yes', 'all_yes') THEN 1 ELSE 0 END) AS w,
                SUM(CASE WHEN status='settled' AND a1_gates_passed = 1
                    AND market_result IN ('no', 'all_no') THEN 1 ELSE 0 END) AS l,
                SUM(CASE WHEN status='settled' AND a1_gates_passed = 1
                    THEN a1_pnl_cents ELSE 0 END) AS pnl,
                AVG(CASE WHEN a1_gates_passed = 1 THEN a1_fee_edge END) AS avg_edge,
                AVG(a1_temperature) AS avg_t,
                AVG(a1_debiased_prob) AS avg_debias,
                SUM(CASE WHEN a1_edge_band_blocked = 1 THEN 1 ELSE 0 END) AS band_blocked
            FROM fifteenm_shadow_signals
            WHERE evaluation_time >= ? AND asset = ?
        """, (since, asset)).fetchone()

        t = rows["total"] or 0
        p = rows["passed"] or 0
        w = rows["w"] or 0
        l = rows["l"] or 0
        n = w + l

        print(f"\n  {asset}:")
        print(f"    Signals: {t}, Gates passed: {p} ({p/t*100:.1f}%)" if t > 0 else f"    Signals: 0")
        if n > 0:
            wr = w / n * 100
            pnl = rows["pnl"] or 0
            print(f"    Settled: {w}W/{l}L ({wr:.1f}% WR), PnL: {pnl} cents")
            print(f"    Avg edge: {(rows['avg_edge'] or 0)*100:.2f}%, "
                  f"Avg T: {rows['avg_t'] or 0:.3f}, "
                  f"Avg debiased_p: {(rows['avg_debias'] or 0)*100:.1f}%")
        bb = rows["band_blocked"] or 0
        if bb > 0:
            print(f"    Edge band blocked: {bb}")

    # A1 gate failure analysis
    subsection("A1 gate failure breakdown")
    failures = conn.execute(f"""
        SELECT asset, a1_gate_failures,
               COUNT(*) AS n
        FROM fifteenm_shadow_signals
        WHERE evaluation_time >= ? AND a1_gates_passed = 0
            {asset_clause}
        GROUP BY asset, a1_gate_failures
        ORDER BY n DESC
        LIMIT 20
    """, (since,)).fetchall()
    if failures:
        print(f"  {'Asset':<6} {'Failure Reason':<40} {'N':>5}")
        print("  " + "-" * 53)
        for f in failures:
            reason = f["a1_gate_failures"] or "unknown"
            print(f"  {f['asset']:<6} {reason:<40} {f['n']:>5}")
    else:
        print("  No gate failures recorded (all signals passing?)")

    # Per-asset A2 deep dive
    subsection("A2 (LightGBM) per-asset alpha")
    for asset in (["BTC", "ETH", "SOL", "XRP"] if not asset_filter else [asset_filter]):
        rows = conn.execute(f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN a2_gates_passed = 1 THEN 1 ELSE 0 END) AS passed,
                SUM(CASE WHEN status='settled' AND a2_gates_passed = 1
                    AND market_result IN ('yes', 'all_yes') THEN 1 ELSE 0 END) AS w,
                SUM(CASE WHEN status='settled' AND a2_gates_passed = 1
                    AND market_result IN ('no', 'all_no') THEN 1 ELSE 0 END) AS l,
                SUM(CASE WHEN status='settled' AND a2_gates_passed = 1
                    THEN a2_pnl_cents ELSE 0 END) AS pnl,
                AVG(CASE WHEN a2_gates_passed = 1 THEN a2_fee_edge END) AS avg_edge,
                MAX(a2_model_version) AS model_ver
            FROM fifteenm_shadow_signals
            WHERE evaluation_time >= ? AND asset = ?
        """, (since, asset)).fetchone()

        t = rows["total"] or 0
        p = rows["passed"] or 0
        w = rows["w"] or 0
        l = rows["l"] or 0
        n = w + l

        print(f"\n  {asset}:")
        print(f"    Signals: {t}, Gates passed: {p} ({p/t*100:.1f}%)" if t > 0 else f"    Signals: 0")
        if n > 0:
            wr = w / n * 100
            pnl = rows["pnl"] or 0
            print(f"    Settled: {w}W/{l}L ({wr:.1f}% WR), PnL: {pnl} cents")
            print(f"    Avg edge: {(rows['avg_edge'] or 0)*100:.2f}%")
        model = rows["model_ver"]
        print(f"    Model: {model if model else 'NOT TRAINED'}")

    # Comparative alpha: live vs A1 vs A2 vs market-only
    subsection("Comparative alpha (settled signals)")
    comp = conn.execute(f"""
        SELECT
            asset,
            COUNT(*) AS n,
            SUM(live_pnl_cents) AS live_pnl,
            SUM(CASE WHEN a1_gates_passed = 1 THEN a1_pnl_cents ELSE 0 END) AS a1_pnl,
            SUM(CASE WHEN a2_gates_passed = 1 THEN a2_pnl_cents ELSE 0 END) AS a2_pnl,
            SUM(market_only_pnl_cents) AS mkt_pnl
        FROM fifteenm_shadow_signals
        WHERE status = 'settled' AND evaluation_time >= ? {asset_clause}
        GROUP BY asset
    """, (since,)).fetchall()

    if comp:
        print(f"  {'Asset':<6} {'N':>4} {'Live PnL':>10} {'A1 PnL':>10} "
              f"{'A2 PnL':>10} {'Mkt PnL':>10}")
        print("  " + "-" * 54)
        totals = [0, 0, 0, 0, 0]
        for r in comp:
            n = r["n"] or 0
            live = r["live_pnl"] or 0
            a1 = r["a1_pnl"] or 0
            a2 = r["a2_pnl"] or 0
            mkt = r["mkt_pnl"] or 0
            print(f"  {r['asset']:<6} {n:>4} {live:>9}c {a1:>9}c "
                  f"{a2:>9}c {mkt:>9}c")
            totals[0] += n
            totals[1] += live
            totals[2] += a1
            totals[3] += a2
            totals[4] += mkt
        print("  " + "-" * 54)
        print(f"  {'TOTAL':<6} {totals[0]:>4} {totals[1]:>9}c {totals[2]:>9}c "
              f"{totals[3]:>9}c {totals[4]:>9}c")

        # Alpha over market
        if totals[4] != 0:
            a1_alpha = totals[2] - totals[4]
            a2_alpha = totals[3] - totals[4]
            live_alpha = totals[1] - totals[4]
            print(f"\n  Alpha vs market-only:")
            print(f"    Live:  {live_alpha:>+8}c")
            print(f"    A1:    {a1_alpha:>+8}c")
            print(f"    A2:    {a2_alpha:>+8}c")
    else:
        print("  No settled shadow signals yet")

    # LightGBM readiness
    subsection("LightGBM training readiness")
    for asset in (["BTC", "ETH", "SOL", "XRP"] if not asset_filter else [asset_filter]):
        n = conn.execute(
            "SELECT COUNT(*) FROM fifteenm_shadow_signals "
            "WHERE status='settled' AND asset=?", (asset,)
        ).fetchone()[0]
        status = "READY" if n >= 200 else f"need {200 - n} more"
        print(f"  {asset}: {n}/200 settled ({status})")


# ── Main ─────────────────────────────────────────────────────────

SECTIONS = {
    "regime": regime_performance,
    "asset": per_asset_alpha,
    "price": price_tier_analysis,
    "stc": stc_analysis,
    "execution": execution_analysis,
    "calibration": calibration_diagnostics,
    "edge": edge_inversion_check,
    "counterfactual": counterfactual_simulations,
    "loss": loss_pattern_analysis,
    "robustness": robustness_analysis,
    "vol": vol_regime_analysis,
    "time": time_of_day_analysis,
    "shadow": shadow_approaches_alpha,
}


def main():
    parser = argparse.ArgumentParser(
        description="15M crypto alpha research script")
    parser.add_argument("--db", default="/tmp/state.db",
                        help="Path to state.db")
    parser.add_argument("--since", default=None,
                        help="Start date (ISO format)")
    parser.add_argument("--regime", default=None,
                        help="'auto' to detect regime start from git")
    parser.add_argument("--asset", default=None,
                        help="Filter to single asset (BTC/ETH/SOL/XRP)")
    parser.add_argument("--section", default=None,
                        help=f"Run single section: {', '.join(SECTIONS.keys())}")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: Database not found at {args.db}")
        print("  scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db")
        sys.exit(1)

    conn = connect_db(args.db)

    # Determine since
    if args.regime == "auto":
        since = detect_regime_start(conn)
        print(f"[Auto-detected regime start: {since}]")
    elif args.since:
        since = args.since
    else:
        since = "2026-02-28T00:00:00"

    print(f"[DB: {args.db}]")
    print(f"[Since: {since}]")
    if args.asset:
        print(f"[Asset filter: {args.asset}]")

    # Run sections
    if args.section:
        if args.section not in SECTIONS:
            print(f"ERROR: Unknown section '{args.section}'. "
                  f"Available: {', '.join(SECTIONS.keys())}")
            sys.exit(1)
        SECTIONS[args.section](conn, since, args.asset)
    else:
        for name, fn in SECTIONS.items():
            try:
                fn(conn, since, args.asset)
            except Exception as e:
                print(f"\n  ERROR in {name}: {e}")

    conn.close()
    print(f"\n{'=' * 76}")
    print(f"  Alpha research complete. {len(SECTIONS)} sections analyzed.")
    print(f"{'=' * 76}")


if __name__ == "__main__":
    main()
