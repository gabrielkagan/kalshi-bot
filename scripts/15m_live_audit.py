#!/usr/bin/env python3
"""15-minute crypto LIVE trading audit script.

Runs locally against a copy of state.db from VPS.
Evaluates live performance, execution quality, profit leakage,
config sensitivity, and data sufficiency.

Usage:
    scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
    python scripts/15m_live_audit.py [--db /tmp/state.db] [--since 2026-02-28]
    python scripts/15m_live_audit.py --db /tmp/state.db --regime auto
    python scripts/15m_live_audit.py --db /tmp/state.db --asset XRP
"""

import argparse
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple


# ── Helpers ──────────────────────────────────────────────────────

def connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def section(title: str) -> None:
    print(f"\n{'=' * 72}")
    print(f"  {title}")
    print(f"{'=' * 72}\n")


def subsection(title: str) -> None:
    print(f"\n--- {title} ---")


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


def is_15m_trade(row) -> bool:
    """Check if a settled_trade row is a 15M trade (not hourly).
    settled_trades has no product_type column; use event_ticker pattern."""
    et = row["event_ticker"] or ""
    # Hourly tickers contain 'D-' (e.g., KXBTCD-26MAR0118-T69249.99)
    return "D-" not in et


def is_15m_eval(row) -> bool:
    """Check if an evaluated_opportunity is 15M."""
    pt = row["product_type"] if "product_type" in row.keys() else None
    return pt is None or pt not in ("hourly", "weather", "sports",
                                     "spx_hourly")


SETTLED_15M_FILTER = "AND (product_type IS NULL OR product_type = '15m')"
EVAL_15M_FILTER = ("AND (product_type IS NULL OR product_type NOT IN "
                    "('hourly', 'weather', 'sports', 'spx_hourly'))")


def detect_regime_start(conn: sqlite3.Connection) -> str:
    """Auto-detect regime start by finding the last git commit that changed
    actual 15M live trading constants in bot/_impl.py.

    Checks git diff of each commit for changes to known constant names.
    Falls back to 2026-02-28 if git is unavailable."""
    import subprocess

    # Constants whose changes define a new regime for 15M live trading
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
        # Get recent bot/_impl.py-changing commit hashes
        result = subprocess.run(
            ["git", "log", "--format=%H %aI", "--since=180 days ago",
             "--", "bot.py", "bot/_impl.py"],
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

            # Check if this commit's diff touches any regime constant
            diff_result = subprocess.run(
                ["git", "diff", f"{commit_hash}^..{commit_hash}",
                 "--", "bot.py", "bot/_impl.py"],
                capture_output=True, text=True, timeout=10, cwd=repo_dir,
            )
            if diff_result.returncode != 0:
                continue

            # Only look at added/removed lines that are constant definitions
            # Match "CONST_NAME =" or "CONST_NAME=" to avoid matching
            # code that merely references the constant
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


# ── Section 1: Performance Summary ──────────────────────────────

def performance_summary(conn: sqlite3.Connection, since: str,
                        asset_filter: Optional[str] = None) -> dict:
    section("1. PERFORMANCE SUMMARY")

    asset_clause = f"AND asset = '{asset_filter}'" if asset_filter else ""

    row = conn.execute(f"""
        SELECT COUNT(*) AS trades,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) AS losses,
          SUM(pnl_cents - COALESCE(fee_cents, 0)) AS pnl,
          SUM(COALESCE(fee_cents, 0)) AS fees,
          SUM(count) AS contracts,
          SUM(count * entry_price_cents) AS risk_cents,
          ROUND(AVG(entry_price_cents), 1) AS avg_price,
          ROUND(AVG(seconds_to_close), 1) AS avg_stc,
          ROUND(AVG(fill_latency_seconds), 2) AS avg_latency,
          MIN(settled_at) AS first_t,
          MAX(settled_at) AS last_t
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {asset_clause}
    """, (since,)).fetchone()

    trades = row["trades"] or 0
    wins = row["wins"] or 0
    losses = row["losses"] or 0
    pnl = row["pnl"] or 0
    fees = row["fees"] or 0
    risk = row["risk_cents"] or 0

    if trades == 0:
        print("No trades in regime window.")
        return {"trades": 0}

    wr = wins / trades * 100
    gross = pnl + fees
    print(f"Period:         {(row['first_t'] or '')[:16]} to {(row['last_t'] or '')[:16]}")
    print(f"Trades:         {trades} ({wins}W / {losses}L)")
    print(f"Win rate:       {wr:.1f}%")
    print(f"Net PnL:        ${pnl/100:.2f}")
    print(f"Gross PnL:      ${gross/100:.2f}")
    print(f"Total fees:     ${fees/100:.2f} ({fees/gross*100:.1f}% of gross)"
          if gross > 0 else f"Total fees:     ${fees/100:.2f}")
    print(f"PnL/trade:      ${pnl/100/trades:.2f}")
    print(f"Contracts:      {row['contracts'] or 0} total, "
          f"{(row['contracts'] or 0)/trades:.1f} avg")
    print(f"Capital risked: ${risk/100:.2f} (return: "
          f"{pnl/risk*100:.1f}%)" if risk > 0 else "")
    print(f"Avg entry:      {row['avg_price']}c")
    print(f"Avg STC:        {row['avg_stc']}s ({(row['avg_stc'] or 0)/60:.1f}m)")
    print(f"Avg fill lat:   {row['avg_latency']}s")

    # Daily breakdown
    subsection("Daily breakdown")
    days = conn.execute(f"""
        SELECT date(settled_at) AS day,
          COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
          SUM(pnl_cents - COALESCE(fee_cents, 0)) AS pnl,
          SUM(COALESCE(fee_cents, 0)) AS fees,
          SUM(count * entry_price_cents) AS risk
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {asset_clause}
        GROUP BY day ORDER BY day
    """, (since,)).fetchall()
    print(f"  {'Day':<12} {'N':>4} {'W/L':>7} {'PnL':>10} {'Fees':>8} "
          f"{'Risk':>10} {'Return':>8}")
    print("  " + "-" * 62)
    for d in days:
        w = d["w"] or 0
        l = (d["n"] or 0) - w
        r_val = d["risk"] or 1
        print(f"  {d['day']:<12} {d['n']:>4} {w:>3}/{l:<3} "
              f"${d['pnl']/100:>9.2f} ${d['fees']/100:>7.2f} "
              f"${r_val/100:>9.2f} {d['pnl']/r_val*100:>7.1f}%")

    # By asset
    subsection("By asset")
    assets = conn.execute(f"""
        SELECT asset,
          COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
          SUM(pnl_cents - COALESCE(fee_cents, 0)) AS pnl,
          SUM(COALESCE(fee_cents, 0)) AS fees,
          ROUND(AVG(entry_price_cents), 1) AS avg_p,
          ROUND(AVG(seconds_to_close), 1) AS avg_stc
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {asset_clause}
        GROUP BY asset ORDER BY pnl DESC
    """, (since,)).fetchall()
    print(f"  {'Asset':<6} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} "
          f"{'PnL':>10} {'Fees':>7} {'Avg P':>6} {'Avg STC':>8} {'Share':>6}")
    print("  " + "-" * 68)
    for a in assets:
        l = (a["n"] or 0) - (a["w"] or 0)
        wr_a = (a["w"] or 0) / a["n"] * 100
        share = (a["pnl"] or 0) / pnl * 100 if pnl != 0 else 0
        print(f"  {a['asset']:<6} {a['n']:>4} {a['w']:>3} {l:>3} {wr_a:>5.1f}% "
              f"${a['pnl']/100:>9.2f} ${a['fees']/100:>6.2f} "
              f"{a['avg_p']:>5.0f}c {a['avg_stc']:>7.0f}s {share:>5.1f}%")

    return {"trades": trades, "wins": wins, "losses": losses,
            "pnl": pnl, "fees": fees, "wr": wr}


# ── Section 2: Execution Quality ────────────────────────────────

def execution_quality(conn: sqlite3.Connection, since: str,
                      asset_filter: Optional[str] = None) -> None:
    section("2. EXECUTION QUALITY")
    asset_clause = f"AND asset = '{asset_filter}'" if asset_filter else ""

    # Strategy breakdown
    subsection("By execution strategy")
    strats = conn.execute(f"""
        SELECT strategy,
          COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
          SUM(pnl_cents - COALESCE(fee_cents, 0)) AS pnl,
          SUM(COALESCE(fee_cents, 0)) AS fees,
          ROUND(AVG(fill_latency_seconds), 2) AS avg_lat,
          ROUND(AVG(seconds_to_close), 1) AS avg_stc
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {asset_clause}
        GROUP BY strategy ORDER BY n DESC
    """, (since,)).fetchall()
    print(f"  {'Strategy':<18} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} "
          f"{'PnL':>10} {'Fees':>7} {'Lat':>6} {'STC':>7}")
    print("  " + "-" * 70)
    for s in strats:
        l = (s["n"] or 0) - (s["w"] or 0)
        wr_s = (s["w"] or 0) / s["n"] * 100
        print(f"  {s['strategy'] or 'NULL':<18} {s['n']:>4} {s['w']:>3} {l:>3} "
              f"{wr_s:>5.1f}% ${s['pnl']/100:>9.2f} ${s['fees']/100:>6.2f} "
              f"{s['avg_lat']:>5.1f}s {s['avg_stc']:>6.0f}s")

    # Fisher: maker vs taker WR
    maker = conn.execute(f"""
        SELECT SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
               SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) AS l
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {asset_clause}
          AND strategy = 'MAKER_PATIENT'
    """, (since,)).fetchone()
    taker = conn.execute(f"""
        SELECT SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
               SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) AS l
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {asset_clause}
          AND strategy = 'TAKER_NOW'
    """, (since,)).fetchone()
    mw, ml = (maker["w"] or 0), (maker["l"] or 0)
    tw, tl = (taker["w"] or 0), (taker["l"] or 0)
    if mw + ml > 0 and tw + tl > 0:
        p_val = fisher_exact_2x2(mw, ml, tw, tl)
        print(f"\n  Fisher (MAKER vs TAKER WR): p={p_val:.4f} "
              f"({'significant' if p_val < 0.05 else 'NOT significant'})")
        print(f"    MAKER: {mw}W/{ml}L = "
              f"{mw/(mw+ml)*100:.0f}%  |  TAKER: {tw}W/{tl}L = "
              f"{tw/(tw+tl)*100:.0f}%")

    # Escalation breakdown (normalize None/none/NULL → 'none')
    subsection("By escalation type")
    escs = conn.execute(f"""
        SELECT COALESCE(LOWER(escalation_type), 'none') AS esc_type,
          COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
          SUM(pnl_cents - COALESCE(fee_cents, 0)) AS pnl,
          SUM(COALESCE(fee_cents, 0)) AS fees,
          ROUND(AVG(maker_wait_seconds), 1) AS avg_wait,
          ROUND(AVG(fill_latency_seconds), 2) AS avg_lat
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {asset_clause}
        GROUP BY esc_type ORDER BY n DESC
    """, (since,)).fetchall()
    print(f"  {'Escalation':<18} {'N':>4} {'W':>3} {'L':>3} "
          f"{'PnL':>10} {'Fees':>7} {'Wait':>6} {'Lat':>6}")
    print("  " + "-" * 62)
    for e in escs:
        l = (e["n"] or 0) - (e["w"] or 0)
        print(f"  {e['esc_type']:<18} {e['n']:>4} "
              f"{e['w']:>3} {l:>3} ${e['pnl']/100:>9.2f} "
              f"${e['fees']/100:>6.2f} "
              f"{e['avg_wait'] or 0:>5.1f}s {e['avg_lat']:>5.1f}s")

    # Fee distribution — classify maker vs taker by computing expected fees
    subsection("Fee distribution")
    trade_rows = conn.execute(f"""
        SELECT entry_price_cents, count, fee_cents, market_result, pnl_cents
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {asset_clause}
        ORDER BY fee_cents
    """, (since,)).fetchall()
    maker_count = 0
    taker_count = 0
    maker_fees = 0
    taker_fees = 0
    for tr in trade_rows:
        p = tr["entry_price_cents"]
        c = tr["count"]
        expected_maker = 0  # Kalshi charges $0 on maker fills
        expected_taker = math.ceil(0.07 * c * p * (100 - p) / 100)
        actual_fee = tr["fee_cents"]
        # Classify: if actual fee is closer to expected maker fee, it's maker
        if abs(actual_fee - expected_maker) <= abs(actual_fee - expected_taker):
            maker_count += 1
            maker_fees += actual_fee
        else:
            taker_count += 1
            taker_fees += actual_fee
    total_n = maker_count + taker_count
    if total_n > 0:
        print(f"  Maker fills: {maker_count}/{total_n} "
              f"({maker_count/total_n*100:.0f}%), total fees: ${maker_fees/100:.2f}")
        print(f"  Taker fills: {taker_count}/{total_n} "
              f"({taker_count/total_n*100:.0f}%), total fees: ${taker_fees/100:.2f}")
        maker_counterfactual = 0  # Kalshi charges $0 on maker fills
        print(f"  Taker fee premium:     "
              f"${(maker_fees + taker_fees - maker_counterfactual)/100:.2f} "
              f"extra vs all-maker")
    else:
        print("  No trades in period")

    # Contract size distribution
    subsection("Contract size distribution")
    size_rows = conn.execute(f"""
        SELECT
          CASE
            WHEN count <= 5 THEN '1-5'
            WHEN count <= 15 THEN '6-15'
            WHEN count <= 30 THEN '16-30'
            WHEN count <= 50 THEN '31-50'
            ELSE '51+'
          END AS bucket,
          COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
          SUM(pnl_cents - COALESCE(fee_cents, 0)) AS pnl,
          ROUND(AVG(count), 1) AS avg_ct,
          ROUND(AVG(entry_price_cents), 1) AS avg_p
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {asset_clause}
        GROUP BY bucket ORDER BY MIN(count)
    """, (since,)).fetchall()
    print(f"  {'Size':>8} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} "
          f"{'PnL':>10} {'Avg Ct':>7} {'Avg P':>6}")
    print("  " + "-" * 55)
    for s in size_rows:
        l = (s["n"] or 0) - (s["w"] or 0)
        wr_s = (s["w"] or 0) / s["n"] * 100
        print(f"  {s['bucket']:>8} {s['n']:>4} {s['w']:>3} {l:>3} "
              f"{wr_s:>5.1f}% ${s['pnl']/100:>9.2f} "
              f"{s['avg_ct']:>6.1f} {s['avg_p']:>5.0f}c")


# ── Section 3: Bucket Analysis ──────────────────────────────────

def bucket_analysis(conn: sqlite3.Connection, since: str,
                    asset_filter: Optional[str] = None) -> None:
    section("3. BUCKET ANALYSIS")
    asset_clause = f"AND asset = '{asset_filter}'" if asset_filter else ""

    # Entry price buckets
    subsection("By entry price")
    price_rows = conn.execute(f"""
        SELECT
          CASE
            WHEN entry_price_cents BETWEEN 87 AND 88 THEN '87-88c'
            WHEN entry_price_cents BETWEEN 89 AND 90 THEN '89-90c'
            WHEN entry_price_cents BETWEEN 91 AND 92 THEN '91-92c'
            WHEN entry_price_cents BETWEEN 93 AND 94 THEN '93-94c'
            WHEN entry_price_cents >= 95 THEN '95c+'
            ELSE 'other'
          END AS bucket,
          COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
          SUM(pnl_cents - COALESCE(fee_cents, 0)) AS pnl,
          SUM(COALESCE(fee_cents, 0)) AS fees,
          ROUND(AVG(entry_price_cents), 1) AS avg_p
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {asset_clause}
        GROUP BY bucket ORDER BY MIN(entry_price_cents)
    """, (since,)).fetchall()
    print(f"  {'Bucket':>8} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} "
          f"{'PnL':>10} {'PnL/T':>8} {'Fees':>7} {'BE WR':>6}")
    print("  " + "-" * 62)
    for p in price_rows:
        l = (p["n"] or 0) - (p["w"] or 0)
        wr_p = (p["w"] or 0) / p["n"] * 100
        be = p["avg_p"] or 0
        print(f"  {p['bucket']:>8} {p['n']:>4} {p['w']:>3} {l:>3} "
              f"{wr_p:>5.1f}% ${p['pnl']/100:>9.2f} "
              f"${p['pnl']/100/p['n']:>7.2f} ${p['fees']/100:>6.2f} "
              f"{be:>5.0f}%")

    # Edge buckets (raw edge from settled_trades)
    subsection("By edge bucket")
    edge_rows = conn.execute(f"""
        SELECT
          CASE
            WHEN edge < 0.015 THEN '<1.5%'
            WHEN edge < 0.020 THEN '1.5-2%'
            WHEN edge < 0.025 THEN '2-2.5%'
            WHEN edge < 0.030 THEN '2.5-3%'
            WHEN edge < 0.040 THEN '3-4%'
            ELSE '4%+'
          END AS bucket,
          COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
          SUM(pnl_cents - COALESCE(fee_cents, 0)) AS pnl,
          ROUND(AVG(edge), 4) AS avg_edge,
          ROUND(AVG(entry_price_cents), 1) AS avg_p
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {asset_clause}
        GROUP BY bucket ORDER BY MIN(edge)
    """, (since,)).fetchall()
    print(f"  {'Edge':>8} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} "
          f"{'PnL':>10} {'Avg Edge':>9} {'Avg P':>6}")
    print("  " + "-" * 55)
    for e in edge_rows:
        l = (e["n"] or 0) - (e["w"] or 0)
        wr_e = (e["w"] or 0) / e["n"] * 100
        print(f"  {e['bucket']:>8} {e['n']:>4} {e['w']:>3} {l:>3} "
              f"{wr_e:>5.1f}% ${e['pnl']/100:>9.2f} "
              f"{e['avg_edge']:>+8.4f} {e['avg_p']:>5.0f}c")

    # STC buckets
    subsection("By seconds-to-close")
    stc_rows = conn.execute(f"""
        SELECT
          CASE
            WHEN seconds_to_close < 60 THEN '<60s'
            WHEN seconds_to_close < 120 THEN '60-120s'
            WHEN seconds_to_close < 180 THEN '120-180s'
            WHEN seconds_to_close < 240 THEN '180-240s'
            WHEN seconds_to_close < 300 THEN '240-300s'
            ELSE '300s+'
          END AS bucket,
          COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
          SUM(pnl_cents - COALESCE(fee_cents, 0)) AS pnl,
          SUM(COALESCE(fee_cents, 0)) AS fees,
          ROUND(AVG(entry_price_cents), 1) AS avg_p,
          ROUND(AVG(seconds_to_close), 1) AS avg_stc
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {asset_clause}
        GROUP BY bucket ORDER BY MIN(seconds_to_close)
    """, (since,)).fetchall()
    print(f"  {'STC':>8} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} "
          f"{'PnL':>10} {'Fees':>7} {'Avg P':>6} {'Avg STC':>8}")
    print("  " + "-" * 62)
    for s in stc_rows:
        l = (s["n"] or 0) - (s["w"] or 0)
        wr_s = (s["w"] or 0) / s["n"] * 100
        print(f"  {s['bucket']:>8} {s['n']:>4} {s['w']:>3} {l:>3} "
              f"{wr_s:>5.1f}% ${s['pnl']/100:>9.2f} "
              f"${s['fees']/100:>6.2f} {s['avg_p']:>5.0f}c {s['avg_stc']:>7.0f}s")

    # Fisher: STC < 180 vs >= 180 (split at loss cluster)
    stc_lo = conn.execute(f"""
        SELECT SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
               SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) AS l
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {asset_clause}
          AND seconds_to_close < 180
    """, (since,)).fetchone()
    stc_hi = conn.execute(f"""
        SELECT SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
               SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) AS l
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {asset_clause}
          AND seconds_to_close >= 180
    """, (since,)).fetchone()
    a, b = (stc_lo["w"] or 0), (stc_lo["l"] or 0)
    c_, d_ = (stc_hi["w"] or 0), (stc_hi["l"] or 0)
    if a + b > 0 and c_ + d_ > 0:
        p_val = fisher_exact_2x2(a, b, c_, d_)
        print(f"\n  Fisher (STC<180 vs >=180): p={p_val:.4f} "
              f"({'significant' if p_val < 0.05 else 'NOT significant'})")
        print(f"    <180s: {a}W/{b}L = {a/(a+b)*100:.0f}%  |  "
              f">=180s: {c_}W/{d_}L = {c_/(c_+d_)*100:.0f}%")

    # Calibration accuracy
    subsection("Calibration accuracy")
    cal_rows = conn.execute(f"""
        SELECT
          CASE
            WHEN calibrated_prob < 0.90 THEN '<90%'
            WHEN calibrated_prob < 0.92 THEN '90-92%'
            WHEN calibrated_prob < 0.94 THEN '92-94%'
            WHEN calibrated_prob < 0.96 THEN '94-96%'
            ELSE '96%+'
          END AS bucket,
          COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
          ROUND(AVG(calibrated_prob), 4) AS avg_pred
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {asset_clause}
          AND calibrated_prob IS NOT NULL
        GROUP BY bucket ORDER BY MIN(calibrated_prob)
    """, (since,)).fetchall()
    print(f"  {'Predicted':>10} {'N':>4} {'W':>3} {'L':>3} "
          f"{'Predicted':>10} {'Actual':>8} {'Delta':>8}")
    print("  " + "-" * 50)
    for c in cal_rows:
        l = (c["n"] or 0) - (c["w"] or 0)
        actual = (c["w"] or 0) / c["n"] * 100
        pred = (c["avg_pred"] or 0) * 100
        print(f"  {c['bucket']:>10} {c['n']:>4} {c['w']:>3} {l:>3} "
              f"{pred:>9.1f}% {actual:>7.1f}% {actual - pred:>+7.1f}pp")

    # Time-of-day
    subsection("Time-of-day (UTC)")
    tod_rows = conn.execute(f"""
        SELECT CAST(strftime('%H', settled_at) AS INTEGER) AS hr,
          COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
          SUM(pnl_cents - COALESCE(fee_cents, 0)) AS pnl
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {asset_clause}
        GROUP BY hr ORDER BY hr
    """, (since,)).fetchall()
    print(f"  {'Hour':>6} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} {'PnL':>10}")
    print("  " + "-" * 38)
    for t in tod_rows:
        l = (t["n"] or 0) - (t["w"] or 0)
        wr_t = (t["w"] or 0) / t["n"] * 100
        print(f"  {t['hr']:>4}:00 {t['n']:>4} {t['w']:>3} {l:>3} "
              f"{wr_t:>5.0f}% ${t['pnl']/100:>9.2f}")

    # Vol regime
    subsection("Volatility regime")
    vol_rows = conn.execute(f"""
        SELECT vol_regime,
          COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
          SUM(pnl_cents - COALESCE(fee_cents, 0)) AS pnl
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {asset_clause}
        GROUP BY vol_regime
    """, (since,)).fetchall()
    for v in vol_rows:
        l = (v["n"] or 0) - (v["w"] or 0)
        wr_v = (v["w"] or 0) / v["n"] * 100 if v["n"] else 0
        print(f"  {v['vol_regime'] or 'NULL':<12} {v['n']:>4} trades, "
              f"{v['w']}W/{l}L ({wr_v:.0f}%), PnL ${v['pnl']/100:.2f}")


# ── Section 4: Profit Leakage ───────────────────────────────────

def profit_leakage(conn: sqlite3.Connection, since: str,
                   asset_filter: Optional[str] = None) -> None:
    section("4. PROFIT LEAKAGE ANALYSIS")
    asset_clause_eval = (f"AND asset = '{asset_filter}'"
                         if asset_filter else "")
    asset_clause_settled = (f"AND asset = '{asset_filter}'"
                            if asset_filter else "")

    # Pipeline completeness
    subsection("Pipeline completeness")
    pipe = conn.execute(f"""
        SELECT
          SUM(CASE WHEN filter_stage = 'candidate' THEN 1 ELSE 0 END) AS candidates,
          SUM(CASE WHEN filter_stage = 'candidate' AND status = 'settled'
              THEN 1 ELSE 0 END) AS cand_settled,
          SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
          COUNT(*) AS total_evals
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER} {asset_clause_eval}
    """, (since,)).fetchone()
    traded = conn.execute(f"""
        SELECT COUNT(*) AS n FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {asset_clause_settled}
    """, (since,)).fetchone()
    rej_n = conn.execute(f"""
        SELECT COUNT(*) AS n FROM rejected_opportunities
        WHERE rejection_time >= ?
          AND (product_type IS NULL OR product_type NOT IN
               ('hourly', 'weather', 'sports', 'spx_hourly'))
    """, (since,)).fetchone()
    cand = pipe["candidates"] or 0
    traded_n = traded["n"] or 0
    pending_n = pipe["pending"] or 0
    total_eval = pipe["total_evals"] or 0
    rej = rej_n["n"] or 0
    non_cand = total_eval - cand
    print(f"  Total evaluated:       {total_eval}")
    print(f"  Passed all filters:    {cand} ({cand/total_eval*100:.1f}%)"
          if total_eval > 0 else f"  Passed all filters:    {cand}")
    print(f"  Filtered out:          {non_cand}")
    print(f"  Actually traded:       {traded_n}")
    if cand > 0:
        gap = cand - traded_n
        print(f"  Candidate→trade gap:   {gap} "
              f"({gap/cand*100:.0f}% of candidates not traded)"
              if gap > 0 else f"  Candidate→trade gap:   0 (all candidates traded)")
    print(f"  Pending (unsettled):   {pending_n}")
    print(f"  Rejected opportunities:{rej}")

    # Filter funnel
    subsection("Evaluated opportunities funnel")
    funnel = conn.execute(f"""
        SELECT filter_stage, COUNT(*) AS n,
          ROUND(AVG(market_price), 1) AS avg_p,
          ROUND(AVG(fee_adjusted_edge), 4) AS avg_edge,
          ROUND(AVG(seconds_to_close), 1) AS avg_stc,
          SUM(CASE WHEN status='settled' AND market_result='yes'
              THEN 1 ELSE 0 END) AS cf_w,
          SUM(CASE WHEN status='settled' AND market_result='no'
              THEN 1 ELSE 0 END) AS cf_l,
          SUM(CASE WHEN status='settled'
              THEN COALESCE(counterfactual_pnl, 0) ELSE 0 END) AS cf_pnl
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER} {asset_clause_eval}
        GROUP BY filter_stage ORDER BY n DESC
    """, (since,)).fetchall()
    print(f"  {'Stage':<24} {'N':>5} {'Avg P':>6} {'Avg Edge':>9} "
          f"{'CF W':>4} {'CF L':>4} {'CF PnL':>10}")
    print("  " + "-" * 70)
    for f in funnel:
        print(f"  {f['filter_stage']:<24} {f['n']:>5} "
              f"{f['avg_p'] or 0:>5.0f}c {f['avg_edge'] or 0:>+8.4f} "
              f"{f['cf_w'] or 0:>4} {f['cf_l'] or 0:>4} "
              f"${(f['cf_pnl'] or 0)/100:>9.2f}")

    # Loss detail
    subsection("Loss detail")
    losses = conn.execute(f"""
        SELECT ticker, asset, entry_price_cents, count, pnl_cents,
          fee_cents, seconds_to_close, strategy, escalation_type,
          fill_latency_seconds, calibrated_prob, edge, vol_regime,
          settled_at
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {asset_clause_settled}
          AND market_result = 'no'
        ORDER BY pnl_cents ASC
    """, (since,)).fetchall()
    if losses:
        for lo in losses:
            print(f"  {lo['ticker']}")
            print(f"    Asset: {lo['asset']}, {lo['count']}ct @ "
                  f"{lo['entry_price_cents']}c, PnL: "
                  f"${lo['pnl_cents']/100:.2f}")
            print(f"    Strategy: {lo['strategy']}, "
                  f"Escalation: {lo['escalation_type']}, "
                  f"STC: {lo['seconds_to_close']:.0f}s")
            print(f"    CalProb: {lo['calibrated_prob']:.4f}, "
                  f"Edge: {lo['edge']:.4f}, "
                  f"VolRegime: {lo['vol_regime']}")
            print(f"    Fee: {lo['fee_cents']}c, "
                  f"Latency: {lo['fill_latency_seconds']:.1f}s")
    else:
        print("  No losses in regime window!")

    # Loss clustering analysis
    if losses:
        subsection("Loss clustering")
        loss_times = []
        loss_assets = []
        for lo in losses:
            try:
                t = datetime.fromisoformat(
                    (lo["settled_at"] if "settled_at" in lo.keys() else "")
                    .replace("Z", ""))
                loss_times.append(t)
            except Exception:
                loss_times.append(None)
            loss_assets.append(lo["asset"])

        # Same-hour clustering
        hour_buckets: Dict[str, int] = defaultdict(int)
        for t in loss_times:
            if t:
                hour_buckets[t.strftime("%Y-%m-%d %H:00")] += 1
        clusters = {k: v for k, v in hour_buckets.items() if v > 1}
        if clusters:
            print("  ALERT: Loss clustering detected (>1 loss in same hour):")
            for hr, cnt in sorted(clusters.items()):
                print(f"    {hr} — {cnt} losses")
        else:
            print("  No same-hour clustering (losses spread across hours)")

        # Consecutive same-asset losses
        all_trades = conn.execute(f"""
            SELECT asset, market_result, settled_at
            FROM settled_trades
            WHERE settled_at >= ? {SETTLED_15M_FILTER}
            ORDER BY settled_at
        """, (since,)).fetchall()
        streaks = []
        cur_asset = None
        cur_count = 0
        for t in all_trades:
            if t["market_result"] == "no":
                if t["asset"] == cur_asset:
                    cur_count += 1
                else:
                    if cur_count >= 2:
                        streaks.append((cur_asset, cur_count))
                    cur_asset = t["asset"]
                    cur_count = 1
            else:
                if cur_count >= 2:
                    streaks.append((cur_asset, cur_count))
                cur_asset = None
                cur_count = 0
        if cur_count >= 2:
            streaks.append((cur_asset, cur_count))
        if streaks:
            print("  ALERT: Consecutive same-asset losses:")
            for asset, cnt in streaks:
                print(f"    {asset}: {cnt} consecutive losses")
        else:
            print("  No consecutive same-asset loss streaks")

        # Asset loss concentration
        asset_loss_ct: Dict[str, int] = defaultdict(int)
        for a in loss_assets:
            asset_loss_ct[a] += 1
        total_losses = len(losses)
        for asset, cnt in sorted(asset_loss_ct.items(),
                                  key=lambda x: -x[1]):
            pct = cnt / total_losses * 100
            flag = " *** CONCENTRATED" if pct > 60 else ""
            print(f"  {asset}: {cnt}/{total_losses} losses "
                  f"({pct:.0f}%){flag}")

    # Counterfactual: stc_shadow (500-900s zone per STC_SHADOW_THRESHOLD=500)
    subsection("STC shadow zone counterfactual (500-900s)")
    shadow = conn.execute(f"""
        SELECT
          CASE
            WHEN seconds_to_close BETWEEN 500 AND 600 THEN '500-600s'
            WHEN seconds_to_close BETWEEN 600 AND 700 THEN '600-700s'
            WHEN seconds_to_close BETWEEN 700 AND 800 THEN '700-800s'
            WHEN seconds_to_close BETWEEN 800 AND 900 THEN '800-900s'
            ELSE 'other'
          END AS bucket,
          COUNT(*) AS n,
          SUM(CASE WHEN status='settled' AND market_result='yes'
              THEN 1 ELSE 0 END) AS w,
          SUM(CASE WHEN status='settled' AND market_result='no'
              THEN 1 ELSE 0 END) AS l,
          SUM(CASE WHEN status='settled'
              THEN COALESCE(counterfactual_pnl, 0) ELSE 0 END) AS cf_pnl,
          ROUND(AVG(market_price), 1) AS avg_p,
          SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER} {asset_clause_eval}
          AND filter_stage IN ('stc_shadow', 'stc_shadow_xrp')
        GROUP BY bucket ORDER BY MIN(seconds_to_close)
    """, (since,)).fetchall()
    if shadow:
        total_n = sum(s["n"] for s in shadow)
        total_w = sum(s["w"] or 0 for s in shadow)
        total_l = sum(s["l"] or 0 for s in shadow)
        total_cf = sum(s["cf_pnl"] or 0 for s in shadow)
        print(f"  {'Bucket':>10} {'N':>4} {'W':>3} {'L':>3} {'Pend':>5} "
              f"{'CF PnL':>10} {'Avg P':>6}")
        print("  " + "-" * 48)
        for s in shadow:
            print(f"  {s['bucket']:>10} {s['n']:>4} {s['w'] or 0:>3} "
                  f"{s['l'] or 0:>3} {s['pending'] or 0:>5} "
                  f"${(s['cf_pnl'] or 0)/100:>9.2f} {s['avg_p']:>5.0f}c")
        settled_n = total_w + total_l
        wr = total_w / settled_n * 100 if settled_n > 0 else 0
        print(f"\n  Total: {total_n} obs, {total_w}W/{total_l}L "
              f"({wr:.0f}% WR), CF PnL ${total_cf/100:.2f}")
        print(f"  Data sufficiency: {'SUFFICIENT' if settled_n >= 30 else 'INSUFFICIENT'} "
              f"(need 30, have {settled_n})")

        # Promotion candidate: combined 500-700s bucket (next zone to consider)
        promo = conn.execute(f"""
            SELECT COUNT(*) AS n,
              SUM(CASE WHEN status='settled' AND market_result='yes'
                  THEN 1 ELSE 0 END) AS w,
              SUM(CASE WHEN status='settled' AND market_result='no'
                  THEN 1 ELSE 0 END) AS l,
              SUM(CASE WHEN status='settled'
                  THEN COALESCE(counterfactual_pnl, 0) ELSE 0 END) AS cf_pnl,
              SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
              ROUND(AVG(market_price), 1) AS avg_p
            FROM evaluated_opportunities
            WHERE evaluation_time >= ? {EVAL_15M_FILTER} {asset_clause_eval}
              AND filter_stage IN ('stc_shadow', 'stc_shadow_xrp')
              AND seconds_to_close BETWEEN 500 AND 700
        """, (since,)).fetchone()
        pw, pl = (promo["w"] or 0), (promo["l"] or 0)
        pn_settled = pw + pl
        if pn_settled > 0:
            p_wr = pw / pn_settled * 100
            print(f"\n  >>> PROMOTION CANDIDATE (500-700s combined):")
            print(f"      {pw}W/{pl}L ({p_wr:.0f}% WR), "
                  f"CF PnL ${(promo['cf_pnl'] or 0)/100:.2f}, "
                  f"pending {promo['pending'] or 0}")
            if pn_settled >= 25 and p_wr >= 90:
                print(f"      STATUS: READY for promotion consideration "
                      f"({pn_settled} settled obs)")
            else:
                print(f"      STATUS: Need more data "
                      f"({pn_settled}/25 settled, {p_wr:.0f}%/90% WR)")
    else:
        print("  No stc_shadow entries")

    # Counterfactual: XRP shadow (15M only)
    subsection("XRP shadow counterfactual")
    _counterfactual_stage(conn, since, "xrp_shadow", asset_clause_eval)

    # XRP live vs shadow comparison
    xrp_live = conn.execute(f"""
        SELECT COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
          SUM(pnl_cents - COALESCE(fee_cents, 0)) AS pnl
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} AND asset='XRP'
    """, (since,)).fetchone()
    xrp_n = xrp_live["n"] or 0
    if xrp_n > 0:
        xrp_w = xrp_live["w"] or 0
        xrp_pnl = xrp_live["pnl"] or 0
        print(f"\n  XRP live (same period): {xrp_w}W/{xrp_n - xrp_w}L, "
              f"PnL ${xrp_pnl/100:.2f}")

    # Counterfactual: insufficient_edge by price bucket
    subsection("Insufficient edge rejections — by price bucket")
    ie_price = conn.execute(f"""
        SELECT
          CASE
            WHEN market_price BETWEEN 87 AND 88 THEN '87-88c'
            WHEN market_price BETWEEN 89 AND 90 THEN '89-90c'
            WHEN market_price BETWEEN 91 AND 92 THEN '91-92c'
            WHEN market_price BETWEEN 93 AND 94 THEN '93-94c'
            WHEN market_price >= 95 THEN '95c+'
            ELSE 'other'
          END AS bucket,
          COUNT(*) AS n,
          SUM(CASE WHEN status='settled' AND market_result='yes'
              THEN 1 ELSE 0 END) AS w,
          SUM(CASE WHEN status='settled' AND market_result='no'
              THEN 1 ELSE 0 END) AS l,
          SUM(CASE WHEN status='settled'
              THEN COALESCE(counterfactual_pnl, 0) ELSE 0 END) AS cf_pnl,
          ROUND(AVG(fee_adjusted_edge), 4) AS avg_fa_edge
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER} {asset_clause_eval}
          AND filter_stage = 'insufficient_edge'
        GROUP BY bucket ORDER BY MIN(market_price)
    """, (since,)).fetchall()
    if ie_price:
        print(f"  {'Bucket':>8} {'N':>5} {'W':>4} {'L':>4} "
              f"{'CF PnL':>10} {'Avg FA Edge':>12} {'Verdict':<16}")
        print("  " + "-" * 65)
        for ip in ie_price:
            verdict = ("CORRECT" if (ip["cf_pnl"] or 0) < 0
                       else "MAY BE TOO STRICT")
            print(f"  {ip['bucket']:>8} {ip['n']:>5} {ip['w'] or 0:>4} "
                  f"{ip['l'] or 0:>4} ${(ip['cf_pnl'] or 0)/100:>9.2f} "
                  f"{ip['avg_fa_edge'] or 0:>+11.4f} {verdict:<16}")

    # Counterfactual: insufficient_edge by edge bucket
    subsection("Insufficient edge rejections — by fee-adjusted edge")
    ie_edge = conn.execute(f"""
        SELECT
          CASE
            WHEN fee_adjusted_edge < -0.02 THEN '<-2%'
            WHEN fee_adjusted_edge < -0.01 THEN '-2% to -1%'
            WHEN fee_adjusted_edge < 0 THEN '-1% to 0%'
            WHEN fee_adjusted_edge < 0.005 THEN '0% to 0.5%'
            WHEN fee_adjusted_edge < 0.009 THEN '0.5-0.9%'
            ELSE '0.9%+'
          END AS bucket,
          COUNT(*) AS n,
          SUM(CASE WHEN status='settled' AND market_result='yes'
              THEN 1 ELSE 0 END) AS w,
          SUM(CASE WHEN status='settled' AND market_result='no'
              THEN 1 ELSE 0 END) AS l,
          SUM(CASE WHEN status='settled'
              THEN COALESCE(counterfactual_pnl, 0) ELSE 0 END) AS cf_pnl
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER} {asset_clause_eval}
          AND filter_stage = 'insufficient_edge'
          AND fee_adjusted_edge IS NOT NULL
        GROUP BY bucket ORDER BY MIN(fee_adjusted_edge)
    """, (since,)).fetchall()
    if ie_edge:
        print(f"  {'Edge':>12} {'N':>5} {'W':>4} {'L':>4} "
              f"{'CF PnL':>10} {'Verdict':<16}")
        print("  " + "-" * 55)
        for ie in ie_edge:
            verdict = ("CORRECT" if (ie["cf_pnl"] or 0) < 0
                       else "MAY BE TOO STRICT")
            print(f"  {ie['bucket']:>12} {ie['n']:>5} {ie['w'] or 0:>4} "
                  f"{ie['l'] or 0:>4} ${(ie['cf_pnl'] or 0)/100:>9.2f} "
                  f"{verdict:<16}")

    # MIN_ENTRY sensitivity: 80-86c
    subsection("MIN_ENTRY_PRICE sensitivity (80-86c)")
    por = conn.execute(f"""
        SELECT market_price,
          COUNT(*) AS n,
          SUM(CASE WHEN status='settled' AND market_result='yes'
              THEN 1 ELSE 0 END) AS w,
          SUM(CASE WHEN status='settled' AND market_result='no'
              THEN 1 ELSE 0 END) AS l,
          SUM(CASE WHEN status='settled'
              THEN COALESCE(counterfactual_pnl, 0) ELSE 0 END) AS cf_pnl,
          SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER} {asset_clause_eval}
          AND filter_stage = 'price_out_of_range'
          AND market_price BETWEEN 80 AND 86
        GROUP BY market_price ORDER BY market_price
    """, (since,)).fetchall()
    if por:
        print(f"  {'Price':>6} {'N':>5} {'W':>4} {'L':>4} {'Pend':>5} "
              f"{'CF PnL':>10} {'WR':>6} {'Verdict':<14}")
        print("  " + "-" * 58)
        for p in por:
            settled_n = (p["w"] or 0) + (p["l"] or 0)
            wr = (p["w"] or 0) / settled_n * 100 if settled_n > 0 else 0
            be = p["market_price"]
            verdict = "PROFITABLE" if wr > be else "UNPROFITABLE"
            print(f"  {p['market_price']:>5}c {p['n']:>5} {p['w'] or 0:>4} "
                  f"{p['l'] or 0:>4} {p['pending'] or 0:>5} "
                  f"${(p['cf_pnl'] or 0)/100:>9.2f} {wr:>5.1f}% "
                  f"{verdict:<14}")
    else:
        print("  No price_out_of_range entries in 80-86c range")

    # Counterfactual: zero_sizing
    subsection("Counterfactual — zero_sizing (drawdown blocked)")
    _counterfactual_stage(conn, since, "zero_sizing", asset_clause_eval)

    # Counterfactual: strategy_wait
    subsection("Counterfactual — strategy_wait")
    _counterfactual_stage(conn, since, "strategy_wait", asset_clause_eval)

    # Counterfactual: dip_addon_shadow
    subsection("Counterfactual — dip_addon_shadow")
    dip_rows = conn.execute(f"""
        SELECT asset, market_price, market_result, seconds_to_close,
          COALESCE(counterfactual_pnl, 0) AS cf_pnl, status
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER} {asset_clause_eval}
          AND filter_stage = 'dip_addon_shadow'
          AND status = 'settled'
    """, (since,)).fetchall()
    if dip_rows:
        w = sum(1 for r in dip_rows if r["market_result"] == "yes")
        l = len(dip_rows) - w
        cf_total = sum(r["cf_pnl"] for r in dip_rows)
        avg_p = sum(r["market_price"] for r in dip_rows) / len(dip_rows)
        avg_stc = sum(r["seconds_to_close"] or 0
                      for r in dip_rows) / len(dip_rows)
        wr = w / len(dip_rows) * 100
        print(f"  N={len(dip_rows)}, {w}W/{l}L, WR={wr:.1f}%, "
              f"avg_price={avg_p:.1f}c, avg_stc={avg_stc:.0f}s")
        print(f"  CF PnL: ${cf_total/100:.2f}")
        verdict = "FILTER CORRECT" if cf_total < 0 else "FILTER MAY BE TOO STRICT"
        print(f"  >>> {verdict}")
        # By asset detail
        dip_by_asset: Dict[str, List] = defaultdict(list)
        for r in dip_rows:
            dip_by_asset[r["asset"]].append(r)
        if len(dip_by_asset) > 1:
            print(f"\n  {'Asset':<6} {'N':>4} {'W':>3} {'L':>3} "
                  f"{'CF PnL':>10} {'WR':>6}")
            print("  " + "-" * 38)
            for asset in sorted(dip_by_asset):
                rr = dip_by_asset[asset]
                aw = sum(1 for r in rr if r["market_result"] == "yes")
                al = len(rr) - aw
                acf = sum(r["cf_pnl"] for r in rr)
                awr = aw / len(rr) * 100
                print(f"  {asset:<6} {len(rr):>4} {aw:>3} {al:>3} "
                      f"${acf/100:>9.2f} {awr:>5.1f}%")
    else:
        print("  No settled dip_addon entries")


def _counterfactual_stage(conn: sqlite3.Connection, since: str,
                          stage: str, asset_clause: str) -> None:
    rows = conn.execute(f"""
        SELECT market_price, market_result, seconds_to_close, asset,
          COALESCE(counterfactual_pnl, 0) AS cf_pnl,
          position_size
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER} {asset_clause}
          AND filter_stage = ?
          AND status = 'settled'
    """, (since, stage)).fetchall()
    if rows:
        w = sum(1 for r in rows if r["market_result"] == "yes")
        l = len(rows) - w
        cf_total = sum(r["cf_pnl"] for r in rows)
        avg_p = sum(r["market_price"] for r in rows) / len(rows)
        avg_stc = sum(r["seconds_to_close"] or 0 for r in rows) / len(rows)
        wr = w / len(rows) * 100
        print(f"  N={len(rows)}, {w}W/{l}L, WR={wr:.1f}%, "
              f"avg_price={avg_p:.1f}c, avg_stc={avg_stc:.0f}s")
        print(f"  CF PnL: ${cf_total/100:.2f}")
        # Flag if CF PnL uses default sizing (position_size NULL → 1 contract)
        null_sizing = sum(1 for r in rows
                          if r["position_size"] is None or r["position_size"] == 0)
        if null_sizing > 0:
            print(f"  NOTE: {null_sizing}/{len(rows)} entries use 1ct default "
                  f"sizing (position_size NULL) — CF PnL is understated")
        verdict = "FILTER CORRECT" if cf_total < 0 else "FILTER MAY BE TOO STRICT"
        print(f"  >>> {verdict}")
    else:
        print(f"  No settled {stage} entries")


# ── Section 5: Config Sensitivity ───────────────────────────────

def config_sensitivity(conn: sqlite3.Connection, since: str,
                       asset_filter: Optional[str] = None) -> None:
    section("5. CONFIG SENSITIVITY")
    asset_clause = f"AND asset = '{asset_filter}'" if asset_filter else ""
    asset_clause_eval = (f"AND asset = '{asset_filter}'"
                         if asset_filter else "")

    # P1: STC threshold sweep (live trades)
    subsection("P1: STC threshold sweep (live trades)")
    print(f"  {'Max STC':>9} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} "
          f"{'PnL':>10} {'Avg P':>6}")
    print("  " + "-" * 48)
    for max_stc in [120, 180, 240, 300]:
        rows = conn.execute(f"""
            SELECT COUNT(*) AS n,
              SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
              SUM(pnl_cents - COALESCE(fee_cents, 0)) AS pnl,
              ROUND(AVG(entry_price_cents), 1) AS avg_p
            FROM settled_trades
            WHERE settled_at >= ? {SETTLED_15M_FILTER} {asset_clause}
              AND seconds_to_close <= ?
        """, (since, max_stc)).fetchone()
        n = rows["n"] or 0
        if n > 0:
            w = rows["w"] or 0
            wr = w / n * 100
            print(f"  {max_stc:>8}s {n:>4} {w:>3} {n-w:>3} "
                  f"{wr:>5.1f}% ${rows['pnl']/100:>9.2f} "
                  f"{rows['avg_p']:>5.0f}c")

    # P2: Per-price-tier edge analysis (matches MIN_EDGE_BY_PRICE schedule)
    # The bot uses price-dependent edge thresholds, NOT a flat minimum.
    # This analysis evaluates each tier independently.
    EDGE_TIERS = [
        ("86-88c", 86, 88, 0.0025),   # 0.25% threshold
        ("89-90c", 89, 90, 0.0025),   # 0.25% threshold
        ("91-92c", 91, 92, 0.0020),   # 0.20% threshold
        ("93-94c", 93, 94, 0.005),    # 0.50% threshold
        ("95-96c", 95, 96, 0.0075),   # 0.75% threshold
        ("97-99c", 97, 99, 0.010),    # 1.00% threshold
    ]
    subsection("P2: Per-price-tier edge performance (MIN_EDGE_BY_PRICE)")
    all_edge = conn.execute(f"""
        SELECT fee_adjusted_edge, market_price, market_result,
          position_size, status, filter_stage,
          COALESCE(counterfactual_pnl, 0) AS cf_pnl
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER} {asset_clause_eval}
          AND filter_stage IN ('candidate', 'insufficient_edge')
          AND status = 'settled' AND fee_adjusted_edge IS NOT NULL
    """, (since,)).fetchall()
    if all_edge:
        print(f"  {'Tier':>8} {'Thresh':>7} {'N':>4} {'W':>3} {'L':>3} "
              f"{'WR':>6} {'Avg Edge':>9} {'Margin':>9} {'CF PnL':>10} {'Verdict':<16}")
        print("  " + "-" * 90)
        for label, lo, hi, thresh in EDGE_TIERS:
            tier_rows = [r for r in all_edge
                         if lo <= (r["market_price"] or 0) <= hi]
            if not tier_rows:
                continue
            traded = [r for r in tier_rows if r["filter_stage"] == "candidate"]
            rejected = [r for r in tier_rows if r["filter_stage"] == "insufficient_edge"]
            w = sum(1 for r in traded if r["market_result"] == "yes")
            l_ = len(traded) - w
            wr = w / len(traded) * 100 if traded else 0
            avg_edge = sum(r["fee_adjusted_edge"] for r in traded) / len(traded) if traded else 0
            avg_margin = avg_edge - thresh  # how far above threshold
            cf_pnl = sum(r["cf_pnl"] for r in traded) / 100
            rej_w = sum(1 for r in rejected if r["market_result"] == "yes")
            rej_l = len(rejected) - rej_w
            # Verdict based on loss clustering near threshold
            losses_near_thresh = [r for r in traded
                                  if r["market_result"] != "yes"
                                  and (r["fee_adjusted_edge"] or 0) < thresh + 0.01]
            if l_ > 0 and len(losses_near_thresh) == l_:
                verdict = "TIGHTEN?"
            elif len(rejected) > 0 and rej_w > rej_l:
                verdict = "LOOSEN?"
            elif l_ == 0 and len(traded) >= 3:
                verdict = "OK"
            else:
                verdict = "OK" if wr >= 75 or len(traded) < 3 else "MONITOR"
            print(f"  {label:>8} {thresh*100:>6.2f}% {len(traded):>4} {w:>3} {l_:>3} "
                  f"{wr:>5.1f}% {avg_edge:>+8.4f} {avg_margin:>+8.4f} "
                  f"${cf_pnl:>9.2f} {verdict:<16}")
            if rejected:
                rej_cf = sum(r["cf_pnl"] for r in rejected) / 100
                print(f"           rejected: {len(rejected)} ({rej_w}W/{rej_l}L), "
                      f"CF PnL ${rej_cf:.2f}")

        # Edge margin analysis: how far above threshold were wins vs losses?
        print()
        subsection("P2b: Edge margin analysis (distance from tier threshold)")
        print(f"  {'Tier':>8} {'Outcome':>8} {'N':>3} {'Avg Margin':>11} "
              f"{'Min Margin':>11} {'Max Margin':>11}")
        print("  " + "-" * 60)
        for label, lo, hi, thresh in EDGE_TIERS:
            traded = [r for r in all_edge
                      if lo <= (r["market_price"] or 0) <= hi
                      and r["filter_stage"] == "candidate"]
            if not traded:
                continue
            wins = [r for r in traded if r["market_result"] == "yes"]
            losses = [r for r in traded if r["market_result"] != "yes"]
            for outcome, group in [("WIN", wins), ("LOSS", losses)]:
                if not group:
                    continue
                margins = [(r["fee_adjusted_edge"] or 0) - thresh for r in group]
                print(f"  {label:>8} {outcome:>8} {len(group):>3} "
                      f"{sum(margins)/len(margins):>+10.4f} "
                      f"{min(margins):>+10.4f} {max(margins):>+10.4f}")

        # Per-tier threshold sensitivity: what if each tier's threshold changed?
        print()
        subsection("P2c: Per-tier threshold tuning (would trades change?)")
        for label, lo, hi, current_thresh in EDGE_TIERS:
            tier_all = [r for r in all_edge
                        if lo <= (r["market_price"] or 0) <= hi]
            if len(tier_all) < 3:
                continue
            print(f"\n  {label} (current threshold: {current_thresh*100:.2f}%):")
            test_thresholds = sorted(set([
                current_thresh * 0.5,
                current_thresh * 0.75,
                current_thresh,
                current_thresh * 1.5,
                current_thresh * 2.0,
                current_thresh * 3.0,
            ]))
            print(f"    {'Threshold':>10} {'N':>4} {'W':>3} {'L':>3} "
                  f"{'WR':>6} {'CF PnL':>10}")
            print("    " + "-" * 42)
            for t in test_thresholds:
                sub = [r for r in tier_all if (r["fee_adjusted_edge"] or 0) >= t]
                if not sub:
                    continue
                sw = sum(1 for r in sub if r["market_result"] == "yes")
                sl = len(sub) - sw
                swr = sw / len(sub) * 100
                scf = sum(r["cf_pnl"] for r in sub) / 100
                marker = " ◄ current" if abs(t - current_thresh) < 0.0001 else ""
                print(f"    {t*100:>9.2f}% {len(sub):>4} {sw:>3} {sl:>3} "
                      f"{swr:>5.1f}% ${scf:>9.2f}{marker}")
        # P2d: Dollar-impact analysis — what $ would each threshold change cost/save?
        print()
        subsection("P2d: Dollar impact of threshold changes (wins$ cut vs losses$ avoided)")
        print("  For each tier, shows the MARGINAL impact of raising the threshold:")
        print("  trades that would be REMOVED and their actual $ outcome.\n")
        for label, lo, hi, current_thresh in EDGE_TIERS:
            tier_traded = [r for r in all_edge
                           if lo <= (r["market_price"] or 0) <= hi
                           and r["filter_stage"] == "candidate"]
            if len(tier_traded) < 2:
                continue
            # Test thresholds above current
            test_thresholds = sorted(set([
                current_thresh * 1.5,
                current_thresh * 2.0,
                current_thresh * 3.0,
                current_thresh * 4.0,
            ]))
            # Only keep thresholds that would actually cut trades
            test_thresholds = [t for t in test_thresholds
                               if any((r["fee_adjusted_edge"] or 0) < t
                                      for r in tier_traded)]
            if not test_thresholds:
                continue
            print(f"  {label} (current: {current_thresh*100:.2f}%):")
            print(f"    {'New Thresh':>10} {'Wins Cut':>9} {'Win$ Cut':>10} "
                  f"{'Losses Avoided':>15} {'Loss$ Saved':>12} {'Net $':>10}")
            print("    " + "-" * 70)
            for t in test_thresholds:
                # Trades that pass current but fail new threshold
                cut = [r for r in tier_traded
                       if (r["fee_adjusted_edge"] or 0) >= current_thresh
                       and (r["fee_adjusted_edge"] or 0) < t]
                if not cut:
                    continue
                cut_wins = [r for r in cut if r["market_result"] == "yes"]
                cut_losses = [r for r in cut if r["market_result"] != "yes"]
                win_dollars = sum(r["cf_pnl"] for r in cut_wins) / 100
                loss_dollars = sum(r["cf_pnl"] for r in cut_losses) / 100
                # loss_dollars is negative, so saving = -loss_dollars
                loss_saved = abs(loss_dollars)
                net = loss_saved - win_dollars  # positive = net benefit
                print(f"    {t*100:>9.2f}% {len(cut_wins):>9} "
                      f"${win_dollars:>9.2f} {len(cut_losses):>15} "
                      f"${loss_saved:>11.2f} ${net:>+9.2f}")
            # Also show the specific losses in this tier with their edge values
            tier_losses = [r for r in tier_traded
                           if r["market_result"] != "yes"]
            if tier_losses:
                print(f"    Losses in tier: ", end="")
                for r in tier_losses:
                    edge_pct = (r["fee_adjusted_edge"] or 0) * 100
                    loss_usd = r["cf_pnl"] / 100
                    print(f"edge={edge_pct:.2f}% ${loss_usd:.2f}", end="  ")
                print()
            print()
    else:
        print("  No settled edge data in period")

    # P3: MIN_ENTRY sweep (trades + POR)
    subsection("P3: MIN_ENTRY_PRICE sweep")
    all_entry = conn.execute(f"""
        SELECT market_price, market_result, status,
          COALESCE(counterfactual_pnl, 0) AS cf_pnl
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER} {asset_clause_eval}
          AND filter_stage IN ('candidate', 'price_out_of_range')
          AND status = 'settled'
    """, (since,)).fetchall()
    if all_entry:
        print(f"  {'Min Price':>10} {'Would Trade':>12} {'W':>3} {'L':>3} "
              f"{'WR':>6} {'CF PnL':>10}")
        print("  " + "-" * 50)
        for min_p in [80, 82, 84, 85, 86, 87, 88, 90]:
            subset = [r for r in all_entry
                      if (r["market_price"] or 0) >= min_p]
            if subset:
                w = sum(1 for r in subset
                        if r["market_result"] == "yes")
                l = len(subset) - w
                wr = w / len(subset) * 100
                cf = sum(r["cf_pnl"] for r in subset)
                print(f"  {min_p:>9}c {len(subset):>12} {w:>3} {l:>3} "
                      f"{wr:>5.1f}% ${cf/100:>9.2f}")

    # P4: MAX_RISK_PER_TRADE impact
    subsection("P4: Position sizing analysis")
    sizing = conn.execute(f"""
        SELECT count, entry_price_cents, pnl_cents, market_result
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER} {asset_clause}
    """, (since,)).fetchall()
    if sizing:
        risks = [r["count"] * r["entry_price_cents"] / 100 for r in sizing]
        pnls = [r["pnl_cents"] / 100 for r in sizing]
        avg_risk = sum(risks) / len(risks)
        max_risk = max(risks)
        max_loss = min(pnls)
        max_win = max(pnls)
        print(f"  Avg risk/trade:  ${avg_risk:.2f}")
        print(f"  Max risk/trade:  ${max_risk:.2f}")
        print(f"  Max single loss: ${max_loss:.2f}")
        print(f"  Max single win:  ${max_win:.2f}")
        # Concentration: what % of PnL comes from top 5 trades?
        sorted_pnl = sorted(pnls, reverse=True)
        total_pnl = sum(pnls)
        top5 = sum(sorted_pnl[:5])
        print(f"  Top 5 trades:    ${top5:.2f} "
              f"({top5/total_pnl*100:.0f}% of total)" if total_pnl > 0
              else "")

    # P5: MAKER_ONLY_THRESHOLD sensitivity
    subsection("P5: MAKER_ONLY_THRESHOLD sensitivity")
    for threshold in [60, 90, 120, 150, 180]:
        below = conn.execute(f"""
            SELECT COUNT(*) AS n,
              SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
              SUM(pnl_cents - COALESCE(fee_cents, 0)) AS pnl,
              SUM(CASE WHEN strategy='TAKER_NOW' THEN 1 ELSE 0 END) AS takers
            FROM settled_trades
            WHERE settled_at >= ? {SETTLED_15M_FILTER} {asset_clause}
              AND seconds_to_close < ?
        """, (since, threshold)).fetchone()
        n = below["n"] or 0
        if n > 0:
            w = below["w"] or 0
            takers = below["takers"] or 0
            print(f"  <{threshold:>3}s: {n} trades, {w}W/{n-w}L, "
                  f"${(below['pnl'] or 0)/100:.2f}, "
                  f"{takers} taker ({takers/n*100:.0f}%)")


# ── Section 6: Calibration Grid Search ────────────────────────────

def calibration_grid_search(conn: sqlite3.Connection, since: str,
                            asset_filter: Optional[str] = None) -> dict:
    """Grid search over edge floor x min price to find optimal filter config.

    Uses the full evaluation universe (candidate + insufficient_edge) to
    simulate what-if scenarios for different filter thresholds. Returns
    dict with best config info for downstream use.
    """
    section("6. CALIBRATION GRID SEARCH")
    asset_clause = f"AND asset = '{asset_filter}'" if asset_filter else ""

    # Full universe: candidates (traded) + insufficient_edge (rejected by edge)
    # Both have fee_adjusted_edge and market_result once settled
    rows = conn.execute(f"""
        SELECT market_price, fee_adjusted_edge, market_result, asset,
               seconds_to_close, filter_stage, position_size,
               COALESCE(counterfactual_pnl, 0) AS cf_pnl
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER} {asset_clause}
          AND filter_stage IN ('candidate', 'insufficient_edge')
          AND status = 'settled'
          AND fee_adjusted_edge IS NOT NULL
          AND market_price IS NOT NULL
          AND market_result IS NOT NULL
    """, (since,)).fetchall()

    n_total = len(rows)
    if n_total < 10:
        print(f"  Only {n_total} settled evaluations — need 10+ for grid search.")
        return {"n_total": n_total, "best_config": None}

    # Date range for daily rate
    ts = conn.execute(f"""
        SELECT MIN(evaluation_time), MAX(evaluation_time)
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER}
          AND filter_stage IN ('candidate', 'insufficient_edge')
    """, (since,)).fetchone()
    if ts[0] and ts[1]:
        t1 = datetime.fromisoformat(ts[0].replace("Z", ""))
        t2 = datetime.fromisoformat(ts[1].replace("Z", ""))
        n_days = max((t2 - t1).total_seconds() / 86400, 0.5)
    else:
        n_days = 1.0

    # Separate positive-edge universe (would actually be tradeable)
    pos_rows = [r for r in rows if r["fee_adjusted_edge"] >= 0]
    total_w = sum(1 for r in pos_rows if r["market_result"] == "yes")
    total_l = len(pos_rows) - total_w
    total_wr = total_w / len(pos_rows) * 100 if pos_rows else 0

    # Current config baseline (candidates only)
    cands = [r for r in rows if r["filter_stage"] == "candidate"]
    cand_w = sum(1 for r in cands if r["market_result"] == "yes")
    cand_l = len(cands) - cand_w

    def sim_pnl_maker_unit(price: int, won: bool) -> float:
        fee = 0  # Kalshi charges $0 on maker fills
        return ((100 - price) - fee) if won else (-price - fee)

    cand_pnl = sum(sim_pnl_maker_unit(r["market_price"],
                   r["market_result"] == "yes") for r in cands)
    cand_pnl_sz = sum(sim_pnl_maker_unit(r["market_price"],
                      r["market_result"] == "yes")
                      * (r["position_size"] or 1) for r in cands)

    print(f"  Full universe: {n_total} evaluations over {n_days:.1f} days")
    print(f"  Positive-edge universe: {len(pos_rows)} "
          f"({total_w}W/{total_l}L, {total_wr:.1f}%)")
    print(f"  Current config (candidates): {len(cands)} trades "
          f"({cand_w}W/{cand_l}L, "
          f"{cand_w/len(cands)*100:.1f}%), "
          f"1c PnL=${cand_pnl/100:.2f}, "
          f"Sized PnL=${cand_pnl_sz/100:.2f}")

    # ── Per-tier edge threshold grid search ──
    # The bot uses MIN_EDGE_BY_PRICE — each price tier has its own threshold.
    # This grid evaluates each tier independently rather than sweeping a flat minimum.
    GRID_TIERS = [
        ("86-88c", 86, 88, 0.0025),
        ("89-90c", 89, 90, 0.0025),
        ("91-92c", 91, 92, 0.0020),
        ("93-94c", 93, 94, 0.005),
        ("95-96c", 95, 96, 0.0075),
        ("97-99c", 97, 99, 0.010),
    ]
    subsection("Per-tier threshold grid (matches MIN_EDGE_BY_PRICE)")
    tier_best = []
    for label, lo, hi, current in GRID_TIERS:
        tier = [r for r in pos_rows if lo <= r["market_price"] <= hi]
        if len(tier) < 3:
            continue
        print(f"\n  {label} (current: {current*100:.2f}%, n={len(tier)}):")
        multipliers = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0]
        print(f"    {'Threshold':>10} {'N':>4} {'W':>3} {'L':>3} "
              f"{'WR':>6} {'1c PnL':>8} {'Sized PnL':>10} {'$/day sz':>9}")
        print("    " + "-" * 62)
        best_pnl, best_t = -999, current
        for mult in multipliers:
            t = current * mult
            sub = [r for r in tier if r["fee_adjusted_edge"] >= t]
            if not sub:
                continue
            sw = sum(1 for r in sub if r["market_result"] == "yes")
            sl = len(sub) - sw
            swr = sw / len(sub) * 100
            spnl = sum(sim_pnl_maker_unit(r["market_price"],
                       r["market_result"] == "yes") for r in sub)
            spnl_sz = sum(sim_pnl_maker_unit(r["market_price"],
                          r["market_result"] == "yes")
                          * (r["position_size"] or 1) for r in sub)
            daily_sz = spnl_sz / 100 / n_days
            marker = " ◄ current" if abs(mult - 1.0) < 0.01 else ""
            print(f"    {t*100:>9.3f}% {len(sub):>4} {sw:>3} {sl:>3} "
                  f"{swr:>5.1f}% ${spnl/100:>7.2f} "
                  f"${spnl_sz/100:>9.2f} ${daily_sz:>8.2f}{marker}")
            if spnl_sz > best_pnl:
                best_pnl, best_t = spnl_sz, t
        if abs(best_t - current) > 0.0001:
            tier_best.append((label, current, best_t, best_pnl))

    if tier_best:
        print(f"\n  Tier threshold suggestions (data-limited, NOT recommendations):")
        for label, cur, best, pnl in tier_best:
            direction = "TIGHTEN" if best > cur else "LOOSEN"
            print(f"    {label}: {cur*100:.2f}% -> {best*100:.3f}% "
                  f"({direction}, +${pnl/100:.2f} flat PnL)")

    # ── Min price sweep (still useful — independent of per-tier edges) ──
    subsection("Min price sweep")
    prices = [80, 82, 84, 85, 86, 87, 88, 89, 90, 91, 92]
    print(f"  {'MinPrice':>9} {'N':>4} {'W':>4} {'L':>3} {'WR':>6} "
          f"{'1c PnL':>8} {'Sized PnL':>10} {'$/day sz':>9}")
    print("  " + "-" * 60)
    for mp in prices:
        # Apply per-tier edge threshold for each trade (matches live behavior)
        def _passes_tier(r, min_p=mp):
            p = r["market_price"]
            if p < min_p:
                return False
            for _, tlo, thi, thresh in GRID_TIERS:
                if tlo <= p <= thi:
                    return r["fee_adjusted_edge"] >= thresh
            return r["fee_adjusted_edge"] >= 0.0025
        sub = [r for r in pos_rows if _passes_tier(r)]
        if not sub:
            continue
        w = sum(1 for r in sub if r["market_result"] == "yes")
        l_ = len(sub) - w
        wr = w / len(sub) * 100
        pnl = sum(sim_pnl_maker_unit(r["market_price"],
                  r["market_result"] == "yes") for r in sub)
        pnl_sz = sum(sim_pnl_maker_unit(r["market_price"],
                     r["market_result"] == "yes")
                     * (r["position_size"] or 1) for r in sub)
        daily_sz = pnl_sz / 100 / n_days
        asset_floors = {88: "BTC", 90: "ETH", 80: "SOL", 92: "XRP"}
        marker = f" ◄ {asset_floors[mp]}" if mp in asset_floors else ""
        print(f"  {mp:>8}c {len(sub):>4} {w:>4} {l_:>3} {wr:>5.1f}% "
              f"${pnl/100:>7.2f} ${pnl_sz/100:>9.2f} "
              f"${daily_sz:>8.2f}{marker}")

    # ── Fisher exact tests ──
    subsection("Statistical significance (Fisher exact test)")

    # Key comparisons — test current tier-based config vs alternatives
    def _current_config(r):
        """Would this trade pass the current MIN_EDGE_BY_PRICE schedule?"""
        p = r["market_price"]
        e = r["fee_adjusted_edge"]
        if p < 86:
            return False
        for _, tlo, thi, thresh in GRID_TIERS:
            if tlo <= p <= thi:
                return e >= thresh
        return e >= 0.0025

    comparisons = [
        ("Current tier-based config (P>=86c) vs rejected",
         _current_config),
    ]
    # Add per-tier tightened alternatives from tier_best
    for label, cur, best_t, _ in tier_best[:3]:
        comparisons.append((
            f"{label}: tighten to {best_t*100:.2f}% (from {cur*100:.2f}%)",
            lambda r, lo_=int(label.split('-')[0]),
                   hi_=int(label.split('-')[1].rstrip('c')),
                   t_=best_t: (
                lo_ <= r["market_price"] <= hi_
                and r["fee_adjusted_edge"] >= t_)
            if lo_ <= r["market_price"] <= hi_
            else _current_config(r)
        ))

    best_result = None
    for label, pred in comparisons:
        inside = [r for r in pos_rows if pred(r)]
        outside = [r for r in pos_rows if not pred(r)]

        if not inside or not outside:
            continue

        a = sum(1 for r in inside if r["market_result"] == "yes")
        b_ = len(inside) - a
        c_ = sum(1 for r in outside if r["market_result"] == "yes")
        d_ = len(outside) - c_

        wr_in = a / (a + b_) * 100 if (a + b_) > 0 else 0
        wr_out = c_ / (c_ + d_) * 100 if (c_ + d_) > 0 else 0
        p_val = fisher_exact_2x2(a, b_, c_, d_)
        sig = ("***" if p_val < 0.001 else "**" if p_val < 0.01
               else "*" if p_val < 0.05 else "NS")
        daily_n = (a + b_) / n_days

        flat_in = sum(sim_pnl_maker_unit(r["market_price"],
                      r["market_result"] == "yes") for r in inside)
        sized_in = sum(sim_pnl_maker_unit(r["market_price"],
                       r["market_result"] == "yes")
                       * (r["position_size"] or 1) for r in inside)

        print(f"  {label}")
        print(f"    In:  {a}W/{b_}L ({wr_in:.1f}%) "
              f"1c=${flat_in/100:.2f} Sized=${sized_in/100:.2f}")
        print(f"    Out: {c_}W/{d_}L ({wr_out:.1f}%)")
        print(f"    Fisher p={p_val:.4f} {sig}  |  "
              f"{a+b_} signals = {daily_n:.1f}/day")
        print()

        if best_result is None or flat_in > best_result.get("flat_pnl", 0):
            best_result = {
                "n": a + b_,
                "wins": a,
                "losses": b_,
                "win_rate": round(wr_in, 1),
                "flat_pnl": flat_in,
                "fisher_p": round(p_val, 6),
                "signals_per_day": round(daily_n, 1),
            }

    # ── Rejected opportunity analysis (per-tier aware) ──
    subsection("Currently rejected opportunities (below tier threshold)")
    def _below_tier_threshold(r):
        """Would this trade fail its price tier's edge threshold?"""
        p = r["market_price"]
        e = r["fee_adjusted_edge"]
        for _, tlo, thi, thresh in GRID_TIERS:
            if tlo <= p <= thi:
                return e < thresh and e >= 0
        return e < 0.0025 and e >= 0
    rejected = [r for r in pos_rows if _below_tier_threshold(r)]
    if rejected:
        rej_w = sum(1 for r in rejected if r["market_result"] == "yes")
        rej_l = len(rejected) - rej_w
        rej_pnl = sum(sim_pnl_maker_unit(r["market_price"],
                      r["market_result"] == "yes") for r in rejected)
        rej_wr = rej_w / len(rejected) * 100

        print(f"  Rejected (0-0.7% edge): {len(rejected)} trades, "
              f"{rej_w}W/{rej_l}L ({rej_wr:.1f}%)")
        print(f"  Missed flat PnL: ${rej_pnl/100:.2f} "
              f"(${rej_pnl/100/n_days:.2f}/day)")
        print(f"  Missed signals/day: {len(rejected)/n_days:.1f}")

        # By asset
        from collections import defaultdict
        by_asset = defaultdict(lambda: {"w": 0, "l": 0})
        for r in rejected:
            a_key = r["asset"]
            if r["market_result"] == "yes":
                by_asset[a_key]["w"] += 1
            else:
                by_asset[a_key]["l"] += 1
        print(f"\n  Per asset (rejected 0-0.7% edge):")
        for asset in sorted(by_asset):
            d = by_asset[asset]
            n = d["w"] + d["l"]
            wr = d["w"] / n * 100
            print(f"    {asset:4s}: {d['w']}W/{d['l']}L ({wr:.0f}%)")
    else:
        print("  No rejected opportunities with positive edge < 0.7%")

    return {
        "n_total": n_total,
        "n_days": round(n_days, 1),
        "best_config": best_result,
    }


# ── Section 7: Data Sufficiency ─────────────────────────────────

def data_sufficiency(conn: sqlite3.Connection, since: str) -> None:
    section("7. DATA SUFFICIENCY AUDIT")

    # Total regime window
    window = conn.execute(f"""
        SELECT MIN(settled_at) AS first_t, MAX(settled_at) AS last_t,
          COUNT(*) AS n
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER}
    """, (since,)).fetchone()
    n = window["n"] or 0
    if n > 0:
        try:
            t1 = datetime.fromisoformat(
                (window["first_t"] or "").replace("Z", ""))
            t2 = datetime.fromisoformat(
                (window["last_t"] or "").replace("Z", ""))
            days = (t2 - t1).total_seconds() / 86400
        except Exception:
            days = 0
        print(f"  Regime window:  {days:.1f} days")
        print(f"  Total trades:   {n}")
        print(f"  Trades/day:     {n/max(days, 0.1):.1f}")
    else:
        print("  No trades in regime window")
        return

    # Per-config data sufficiency
    configs = [
        ("STC shadow (500-900s)", "stc_shadow", 30),
        ("XRP shadow (counterfactual)", "xrp_shadow", 20),
        ("MIN_ENTRY at 86c", "price_out_of_range", 30),
        ("Edge threshold marginal", "insufficient_edge", 50),
        ("zero_sizing", "zero_sizing", 20),
    ]
    subsection("Per-config data points")
    print(f"  {'Config':<28} {'Have':>5} {'Need':>5} {'Gap':>5} {'Status':<14}")
    print("  " + "-" * 60)
    for label, stage, needed in configs:
        if stage == "price_out_of_range":
            row = conn.execute(f"""
                SELECT COUNT(*) AS n FROM evaluated_opportunities
                WHERE evaluation_time >= ? {EVAL_15M_FILTER}
                  AND filter_stage = ? AND market_price = 86
                  AND status = 'settled'
            """, (since, stage)).fetchone()
        elif stage == "insufficient_edge":
            row = conn.execute(f"""
                SELECT COUNT(*) AS n FROM evaluated_opportunities
                WHERE evaluation_time >= ? {EVAL_15M_FILTER}
                  AND filter_stage = ?
                  AND fee_adjusted_edge > -0.02
                  AND status = 'settled'
            """, (since, stage)).fetchone()
        else:
            # Match both old names (stc_shadow, price_shadow) and new (_xrp variants)
            row = conn.execute(f"""
                SELECT COUNT(*) AS n FROM evaluated_opportunities
                WHERE evaluation_time >= ? {EVAL_15M_FILTER}
                  AND (filter_stage = ? OR filter_stage = ? || '_xrp'
                       OR filter_stage = ? || '_no_xrp')
                  AND status = 'settled'
            """, (since, stage, stage, stage)).fetchone()
        have = row["n"] or 0
        gap = max(0, needed - have)
        status = "SUFFICIENT" if have >= needed else "INSUFFICIENT"
        print(f"  {label:<28} {have:>5} {needed:>5} {gap:>5} {status:<14}")

    # Fill microstructure data
    subsection("Fill microstructure data")
    fill_data = conn.execute(f"""
        SELECT COUNT(*) AS total,
          SUM(CASE WHEN ask_depth IS NOT NULL AND ask_depth > 0 THEN 1 ELSE 0 END) AS with_depth
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER}
          AND filter_stage = 'candidate'
    """, (since,)).fetchone()
    fd_total = fill_data["total"] or 0
    fd_depth = fill_data["with_depth"] or 0
    print(f"  Candidates with orderbook depth: {fd_depth}/{fd_total}")
    if fd_total > 0:
        print(f"  Coverage: {fd_depth/fd_total*100:.0f}%")

    # Per-asset N
    subsection("Per-asset trade count")
    asset_n = conn.execute(f"""
        SELECT asset, COUNT(*) AS n
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER}
        GROUP BY asset ORDER BY n DESC
    """, (since,)).fetchall()
    for a in asset_n:
        status = "OK" if a["n"] >= 15 else "LOW"
        print(f"  {a['asset']:<6} {a['n']:>4} trades  [{status}]")

    # Taker vs maker sample size
    subsection("Execution path sample sizes")
    strat_n = conn.execute(f"""
        SELECT strategy, COUNT(*) AS n
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER}
        GROUP BY strategy ORDER BY n DESC
    """, (since,)).fetchall()
    for s in strat_n:
        status = "OK" if s["n"] >= 20 else "LOW"
        print(f"  {s['strategy'] or 'NULL':<18} {s['n']:>4} trades  [{status}]")

    # Vol regime coverage
    subsection("Vol regime coverage")
    vol_n = conn.execute(f"""
        SELECT vol_regime, COUNT(*) AS n
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER}
        GROUP BY vol_regime
    """, (since,)).fetchall()
    for v in vol_n:
        status = "OK" if v["n"] >= 10 else "LOW"
        print(f"  {v['vol_regime'] or 'NULL':<12} {v['n']:>4} trades  [{status}]")


# ── Section 8: Price Shadow Analysis ────────────────────────────

def price_shadow_analysis(conn: sqlite3.Connection, since: str,
                          asset_filter: Optional[str] = None) -> dict:
    """Analyze price_shadow data to evaluate lowering MIN_ENTRY_PRICE."""
    section("8. PRICE SHADOW ANALYSIS (70-85c)")
    asset_clause = f"AND asset = '{asset_filter}'" if asset_filter else ""

    # Total observations
    totals = conn.execute(f"""
        SELECT COUNT(*) AS n,
               ROUND(AVG(market_price), 1) AS avg_price,
               MIN(market_price) AS min_price,
               MAX(market_price) AS max_price,
               ROUND(AVG(edge), 4) AS avg_edge,
               ROUND(AVG(fee_adjusted_edge), 4) AS avg_fee_edge
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER} {asset_clause}
          AND filter_stage IN ('price_shadow', 'price_shadow_xrp')
    """, (since,)).fetchone()

    n = totals["n"] or 0
    if n == 0:
        print("  No price_shadow observations yet. Data collection just started.")
        return {"n": 0}

    print(f"  Total observations:  {n}")
    print(f"  Price range:         {totals['min_price']}-{totals['max_price']}c "
          f"(avg {totals['avg_price']}c)")
    print(f"  Avg edge:            {totals['avg_edge']*100:.2f}%")
    print(f"  Avg fee-adj edge:    {totals['avg_fee_edge']*100:.2f}%")

    # Settled subset
    settled = conn.execute(f"""
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
               SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) AS l,
               ROUND(AVG(edge), 4) AS avg_edge,
               ROUND(AVG(fee_adjusted_edge), 4) AS avg_fee_edge
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER} {asset_clause}
          AND filter_stage IN ('price_shadow', 'price_shadow_xrp')
          AND status = 'settled'
          AND market_result IS NOT NULL
    """, (since,)).fetchone()

    sn = settled["n"] or 0
    sw = settled["w"] or 0
    sl = settled["l"] or 0
    print(f"\n  Settled:             {sn} / {n} ({sn/n*100:.0f}%)" if n > 0 else "")

    if sn > 0:
        wr = sw / sn * 100
        print(f"  Win rate:            {sw}W/{sl}L ({wr:.1f}%)")
        print(f"  Avg edge (settled):  {settled['avg_edge']*100:.2f}%")
        print(f"  Avg fee-adj (sett):  {settled['avg_fee_edge']*100:.2f}%")
    else:
        print("  [Awaiting settlements — edge data available, WR data pending]")

    # ── By price bucket ──
    subsection("Price bucket breakdown")
    buckets = conn.execute(f"""
        SELECT
            CASE
                WHEN market_price BETWEEN 70 AND 74 THEN '70-74c'
                WHEN market_price BETWEEN 75 AND 79 THEN '75-79c'
                WHEN market_price BETWEEN 80 AND 82 THEN '80-82c'
                WHEN market_price BETWEEN 83 AND 85 THEN '83-85c'
                ELSE 'other'
            END AS bucket,
            COUNT(*) AS n,
            ROUND(AVG(edge), 4) AS avg_edge,
            ROUND(AVG(fee_adjusted_edge), 4) AS avg_fee_edge,
            SUM(CASE WHEN status='settled' AND market_result='yes'
                THEN 1 ELSE 0 END) AS w,
            SUM(CASE WHEN status='settled' AND market_result='no'
                THEN 1 ELSE 0 END) AS l
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER} {asset_clause}
          AND filter_stage IN ('price_shadow', 'price_shadow_xrp')
        GROUP BY bucket ORDER BY bucket
    """, (since,)).fetchall()

    print(f"  {'Bucket':>8} {'N':>5} {'Edge':>7} {'FeeEdge':>8} "
          f"{'W':>4} {'L':>3} {'WR':>6}")
    print("  " + "-" * 50)
    for b in buckets:
        bw = b["w"] or 0
        bl = b["l"] or 0
        bn = bw + bl
        wr_str = f"{bw/bn*100:.1f}%" if bn > 0 else "n/a"
        print(f"  {b['bucket']:>8} {b['n']:>5} "
              f"{b['avg_edge']*100:>6.2f}% {b['avg_fee_edge']*100:>7.2f}% "
              f"{bw:>4} {bl:>3} {wr_str:>6}")

    # ── By asset ──
    subsection("Per-asset breakdown")
    by_asset = conn.execute(f"""
        SELECT asset, COUNT(*) AS n,
               ROUND(AVG(edge), 4) AS avg_edge,
               ROUND(AVG(fee_adjusted_edge), 4) AS avg_fee_edge,
               SUM(CASE WHEN status='settled' AND market_result='yes'
                   THEN 1 ELSE 0 END) AS w,
               SUM(CASE WHEN status='settled' AND market_result='no'
                   THEN 1 ELSE 0 END) AS l
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER} {asset_clause}
          AND filter_stage IN ('price_shadow', 'price_shadow_xrp')
        GROUP BY asset ORDER BY n DESC
    """, (since,)).fetchall()

    print(f"  {'Asset':>6} {'N':>5} {'Edge':>7} {'FeeEdge':>8} "
          f"{'W':>4} {'L':>3} {'WR':>6}")
    print("  " + "-" * 48)
    for a in by_asset:
        aw = a["w"] or 0
        al = a["l"] or 0
        an = aw + al
        wr_str = f"{aw/an*100:.1f}%" if an > 0 else "n/a"
        print(f"  {a['asset']:>6} {a['n']:>5} "
              f"{a['avg_edge']*100:>6.2f}% {a['avg_fee_edge']*100:>7.2f}% "
              f"{aw:>4} {al:>3} {wr_str:>6}")

    # ── Simulated PnL (settled only) ──
    if sn >= 5:
        subsection("Simulated PnL (flat $1 per contract, maker fees)")
        sim_rows = conn.execute(f"""
            SELECT market_price, fee_adjusted_edge, market_result, asset,
                   seconds_to_close
            FROM evaluated_opportunities
            WHERE evaluation_time >= ? {EVAL_15M_FILTER} {asset_clause}
              AND filter_stage IN ('price_shadow', 'price_shadow_xrp')
              AND status = 'settled'
              AND market_result IS NOT NULL
              AND fee_adjusted_edge IS NOT NULL
        """, (since,)).fetchall()

        def sim_pnl_maker(price: int, won: bool) -> float:
            fee = 0  # Kalshi charges $0 on maker fills
            return ((100 - price) - fee) if won else (-price - fee)

        total_pnl = 0
        pos_edge_pnl = 0
        for r in sim_rows:
            won = r["market_result"] == "yes"
            pnl = sim_pnl_maker(r["market_price"], won)
            total_pnl += pnl
            if r["fee_adjusted_edge"] >= 0:
                pos_edge_pnl += pnl

        # Date range for daily rate
        ts_range = conn.execute(f"""
            SELECT MIN(evaluation_time), MAX(evaluation_time)
            FROM evaluated_opportunities
            WHERE evaluation_time >= ? {EVAL_15M_FILTER}
              AND filter_stage IN ('price_shadow', 'price_shadow_xrp')
        """, (since,)).fetchone()
        if ts_range[0] and ts_range[1]:
            t1 = datetime.fromisoformat(ts_range[0].replace("Z", ""))
            t2 = datetime.fromisoformat(ts_range[1].replace("Z", ""))
            ps_days = max((t2 - t1).total_seconds() / 86400, 0.5)
        else:
            ps_days = 1.0

        print(f"  All settled:         ${total_pnl/100:.2f} "
              f"(${total_pnl/100/ps_days:.2f}/day)")
        print(f"  Positive-edge only:  ${pos_edge_pnl/100:.2f} "
              f"(${pos_edge_pnl/100/ps_days:.2f}/day)")
        print(f"  Data window:         {ps_days:.1f} days")

        # Min price sweep within shadow data
        subsection("Min price sweep (price_shadow settled data)")
        prices = [70, 72, 74, 76, 78, 80, 82, 84]
        print(f"  {'MinPrice':>9} {'N':>4} {'W':>4} {'L':>3} {'WR':>6} "
              f"{'FlatPnL':>9} {'$/day':>7}")
        print("  " + "-" * 48)
        for mp in prices:
            sub = [r for r in sim_rows
                   if (r["fee_adjusted_edge"] or 0) >= 0
                   and r["market_price"] >= mp]
            if not sub:
                continue
            w = sum(1 for r in sub if r["market_result"] == "yes")
            l_ = len(sub) - w
            wr = w / len(sub) * 100
            pnl = sum(sim_pnl_maker(r["market_price"],
                      r["market_result"] == "yes") for r in sub)
            daily = pnl / 100 / ps_days
            print(f"  {mp:>8}c {len(sub):>4} {w:>4} {l_:>3} {wr:>5.1f}% "
                  f"${pnl/100:>8.2f} ${daily:>6.2f}")

    # ── Promotion readiness ──
    subsection("Promotion readiness")
    if sn >= 30:
        wr = sw / sn * 100
        if wr >= 85:
            print(f"  READY: {sw}W/{sl}L ({wr:.1f}% WR, n={sn}) — "
                  f"consider lowering MIN_ENTRY_PRICE")
            # Fisher exact vs live trades
            live = conn.execute(f"""
                SELECT SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
                       SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) AS l
                FROM settled_trades
                WHERE settled_at >= ? {SETTLED_15M_FILTER}
            """, (since,)).fetchone()
            lw, ll = (live["w"] or 0), (live["l"] or 0)
            if lw + ll > 0:
                p = fisher_exact_2x2(lw, ll, sw, sl)
                print(f"  Fisher test (live vs shadow): p={p:.4f} "
                      f"{'(not sig worse)' if p > 0.05 else '(significantly worse)'}")
        else:
            print(f"  NOT READY: {sw}W/{sl}L ({wr:.1f}% WR) — "
                  f"below 85% threshold")
    elif sn > 0:
        wr = sw / sn * 100
        print(f"  COLLECTING: {sw}W/{sl}L ({wr:.1f}% WR, n={sn}) — "
              f"need 30 settled for promotion decision")
    else:
        print(f"  COLLECTING: {n} observations, 0 settled — "
              f"awaiting market settlements")

    return {"n": n, "settled": sn, "wins": sw, "losses": sl}


# ── Section 10: 15M Shadow Approaches ──────────────────────────

def shadow_approaches(conn: sqlite3.Connection, since: str,
                      asset_filter: Optional[str] = None) -> None:
    section("10. 15M SHADOW APPROACHES (RecalibratedEGARCH + LightGBM + Gating)")

    # Check if table exists
    tbl = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='fifteenm_shadow_signals'"
    ).fetchone()
    if not tbl:
        print("  fifteenm_shadow_signals table not found — shadow engine not initialized")
        return

    asset_clause = f"AND asset = '{asset_filter}'" if asset_filter else ""

    # Overview
    overview = conn.execute(f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) AS settled,
            SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
            MIN(evaluation_time) AS first_eval,
            MAX(evaluation_time) AS last_eval
        FROM fifteenm_shadow_signals
        WHERE evaluation_time >= ? {asset_clause}
    """, (since,)).fetchone()

    total = overview["total"] or 0
    settled = overview["settled"] or 0
    pending = overview["pending"] or 0

    print(f"  Total signals:   {total}")
    print(f"  Settled:         {settled}")
    print(f"  Pending:         {pending}")
    if total > 0:
        print(f"  First eval:      {overview['first_eval']}")
        print(f"  Last eval:       {overview['last_eval']}")

    if total == 0:
        print("\n  ⚠ No shadow signals recorded. Check fifteenm_shadow.py is running.")
        print("    Common causes: check_same_thread missing, import error, "
              "exception in evaluate_strike()")
        return

    # Approach 1: RecalibratedEGARCH
    subsection("Approach 1: RecalibratedEGARCH")
    for asset in (["BTC", "ETH", "SOL", "XRP"] if not asset_filter else [asset_filter]):
        a1 = conn.execute(f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN a1_gates_passed = 1 THEN 1 ELSE 0 END) AS passed,
                SUM(CASE WHEN status='settled' AND a1_gates_passed = 1
                    AND market_result IN ('yes', 'all_yes') THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN status='settled' AND a1_gates_passed = 1
                    AND market_result IN ('no', 'all_no') THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN status='settled' AND a1_gates_passed = 1
                    THEN a1_pnl_cents ELSE 0 END) AS pnl,
                AVG(CASE WHEN a1_gates_passed = 1 THEN a1_fee_edge END) AS avg_edge,
                AVG(a1_temperature) AS avg_temp,
                AVG(a1_blend_w) AS avg_blend
            FROM fifteenm_shadow_signals
            WHERE evaluation_time >= ? AND asset = ?
        """, (since, asset)).fetchone()

        t = a1["total"] or 0
        p = a1["passed"] or 0
        w = a1["wins"] or 0
        l = a1["losses"] or 0
        pnl = a1["pnl"] or 0
        n = w + l
        wr = w / n * 100 if n > 0 else 0

        gate_rate = p / t * 100 if t > 0 else 0
        print(f"\n  {asset}: {t} evals, {p} passed gates ({gate_rate:.1f}%)")
        if n > 0:
            print(f"    Settled: {w}W/{l}L ({wr:.1f}% WR), PnL: {pnl} cents")
        if a1["avg_edge"] is not None:
            print(f"    Avg fee-adj edge: {a1['avg_edge']*100:.2f}%")
        if a1["avg_temp"] is not None:
            print(f"    Avg temperature: {a1['avg_temp']:.3f}, "
                  f"blend_w: {a1['avg_blend']:.3f}")

    # Approach 2: LightGBM
    subsection("Approach 2: LightGBM")
    for asset in (["BTC", "ETH", "SOL", "XRP"] if not asset_filter else [asset_filter]):
        a2 = conn.execute(f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN a2_gates_passed = 1 THEN 1 ELSE 0 END) AS passed,
                SUM(CASE WHEN status='settled' AND a2_gates_passed = 1
                    AND market_result IN ('yes', 'all_yes') THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN status='settled' AND a2_gates_passed = 1
                    AND market_result IN ('no', 'all_no') THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN status='settled' AND a2_gates_passed = 1
                    THEN a2_pnl_cents ELSE 0 END) AS pnl,
                AVG(CASE WHEN a2_gates_passed = 1 THEN a2_fee_edge END) AS avg_edge,
                MAX(a2_model_version) AS latest_model
            FROM fifteenm_shadow_signals
            WHERE evaluation_time >= ? AND asset = ?
        """, (since, asset)).fetchone()

        t = a2["total"] or 0
        p = a2["passed"] or 0
        w = a2["wins"] or 0
        l = a2["losses"] or 0
        pnl = a2["pnl"] or 0
        n = w + l
        wr = w / n * 100 if n > 0 else 0

        gate_rate = p / t * 100 if t > 0 else 0
        print(f"\n  {asset}: {t} evals, {p} passed gates ({gate_rate:.1f}%)")
        if n > 0:
            print(f"    Settled: {w}W/{l}L ({wr:.1f}% WR), PnL: {pnl} cents")
        if a2["avg_edge"] is not None:
            print(f"    Avg fee-adj edge: {a2['avg_edge']*100:.2f}%")
        model = a2["latest_model"]
        if model:
            print(f"    Model version: {model}")
        else:
            print(f"    Model: NOT TRAINED (need {200} settled rows)")

    # Approach 3: EGARCH Gating Model
    shadow_cols = [r[1] for r in conn.execute(
        "PRAGMA table_info(fifteenm_shadow_signals)").fetchall()]
    if "a3_gate_prob" in shadow_cols:
        subsection("Approach 3: EGARCH Gating Model")
        for side_label, gate_col_pfx, pnl_pfx, live_pnl_col, win_result in [
            ("YES-side", "a3", "a3", "live_pnl_cents", ("yes", "all_yes")),
            ("NO-side", "no_a3", "no_a3", "no_live_pnl_cents", ("no", "all_no")),
        ]:
            print(f"\n  {side_label}:")
            for asset in (["BTC", "ETH", "SOL", "XRP"] if not asset_filter
                          else [asset_filter]):
                try:
                    a3 = conn.execute(f"""
                        SELECT
                            COUNT(*) AS total,
                            SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) AS settled,
                            AVG({gate_col_pfx}_gate_prob) AS avg_gate_prob,
                            MAX({gate_col_pfx}_model_version) AS model_ver,
                            -- Gate rates
                            SUM({gate_col_pfx}_gate_10) AS gated_10,
                            SUM({gate_col_pfx}_gate_20) AS gated_20,
                            SUM({gate_col_pfx}_gate_30) AS gated_30,
                            -- Counterfactual PnL at each threshold
                            SUM(CASE WHEN status='settled'
                                THEN {pnl_pfx}_pnl_gate10_cents END) AS pnl_g10,
                            SUM(CASE WHEN status='settled'
                                THEN {pnl_pfx}_pnl_gate20_cents END) AS pnl_g20,
                            SUM(CASE WHEN status='settled'
                                THEN {pnl_pfx}_pnl_gate30_cents END) AS pnl_g30,
                            -- Baseline PnL (ungated)
                            SUM(CASE WHEN status='settled'
                                THEN {live_pnl_col} END) AS baseline_pnl,
                            -- Losses avoided at each threshold
                            SUM(CASE WHEN status='settled' AND {gate_col_pfx}_gate_10 = 1
                                AND {live_pnl_col} < 0 THEN 1 ELSE 0 END) AS losses_avoided_10,
                            SUM(CASE WHEN status='settled' AND {gate_col_pfx}_gate_20 = 1
                                AND {live_pnl_col} < 0 THEN 1 ELSE 0 END) AS losses_avoided_20,
                            SUM(CASE WHEN status='settled' AND {gate_col_pfx}_gate_30 = 1
                                AND {live_pnl_col} < 0 THEN 1 ELSE 0 END) AS losses_avoided_30,
                            -- Wins missed at each threshold
                            SUM(CASE WHEN status='settled' AND {gate_col_pfx}_gate_10 = 1
                                AND {live_pnl_col} > 0 THEN 1 ELSE 0 END) AS wins_missed_10,
                            SUM(CASE WHEN status='settled' AND {gate_col_pfx}_gate_20 = 1
                                AND {live_pnl_col} > 0 THEN 1 ELSE 0 END) AS wins_missed_20,
                            SUM(CASE WHEN status='settled' AND {gate_col_pfx}_gate_30 = 1
                                AND {live_pnl_col} > 0 THEN 1 ELSE 0 END) AS wins_missed_30
                        FROM fifteenm_shadow_signals
                        WHERE evaluation_time >= ? AND asset = ?
                            AND {gate_col_pfx}_gate_prob IS NOT NULL
                    """, (since, asset)).fetchone()

                    t = a3["total"] or 0
                    if t == 0:
                        print(f"    {asset}: no gating data")
                        continue
                    settled = a3["settled"] or 0
                    avg_gp = a3["avg_gate_prob"]
                    model_ver = a3["model_ver"] or "NOT TRAINED"
                    baseline_pnl = a3["baseline_pnl"] or 0

                    print(f"    {asset}: {t} evals ({settled} settled), "
                          f"avg gate_prob={avg_gp:.3f}, model={model_ver}")
                    if settled > 0:
                        print(f"      Baseline PnL: {baseline_pnl} cents")
                        print(f"      {'Threshold':<12} {'Gated':>6} {'LossAvd':>8} "
                              f"{'WinMiss':>8} {'Net PnL':>9} {'vs Base':>8}")
                        print(f"      {'-'*54}")
                        for thr, g_key, pnl_key, la_key, wm_key in [
                            ("10%", "gated_10", "pnl_g10",
                             "losses_avoided_10", "wins_missed_10"),
                            ("20%", "gated_20", "pnl_g20",
                             "losses_avoided_20", "wins_missed_20"),
                            ("30%", "gated_30", "pnl_g30",
                             "losses_avoided_30", "wins_missed_30"),
                        ]:
                            g = a3[g_key] or 0
                            pnl_v = a3[pnl_key] or 0
                            la = a3[la_key] or 0
                            wm = a3[wm_key] or 0
                            diff = pnl_v - baseline_pnl
                            print(f"      {thr:<12} {g:>6} {la:>8} "
                                  f"{wm:>8} {pnl_v:>8}c {diff:>+7}c")
                except Exception:
                    print(f"    {asset}: query error")
                    break

    # Training data availability for LightGBM
    subsection("LightGBM / Gating training data")
    training_rows = conn.execute(f"""
        SELECT COUNT(*) AS n FROM fifteenm_shadow_signals
        WHERE status = 'settled' {asset_clause}
    """).fetchone()["n"] or 0
    print(f"  Settled rows available for training: {training_rows}")
    print(f"    A2 (LightGBM outcome): needs 200 — "
          + ("✓ sufficient" if training_rows >= 200 else f"need {200 - training_rows} more"))
    print(f"    A3 (EGARCH gating):    needs 100 — "
          + ("✓ sufficient" if training_rows >= 100 else f"need {100 - training_rows} more"))

    # Comparison: shadow vs live baseline
    subsection("Shadow vs live baseline (settled signals)")
    comp = conn.execute(f"""
        SELECT
            COUNT(*) AS n,
            SUM(CASE WHEN market_result IN ('yes', 'all_yes') THEN 1 ELSE 0 END) AS live_wins,
            SUM(live_pnl_cents) AS live_pnl,
            SUM(CASE WHEN a1_gates_passed = 1 THEN a1_pnl_cents ELSE 0 END) AS a1_pnl,
            SUM(CASE WHEN a2_gates_passed = 1 THEN a2_pnl_cents ELSE 0 END) AS a2_pnl,
            SUM(market_only_pnl_cents) AS mkt_pnl
        FROM fifteenm_shadow_signals
        WHERE status = 'settled' AND evaluation_time >= ? {asset_clause}
    """, (since,)).fetchone()

    n = comp["n"] or 0
    if n > 0:
        print(f"  Settled signals: {n}")
        print(f"  Live baseline PnL:     {comp['live_pnl'] or 0:>8} cents")
        print(f"  A1 (RecalEGARCH) PnL:  {comp['a1_pnl'] or 0:>8} cents")
        print(f"  A2 (LightGBM) PnL:     {comp['a2_pnl'] or 0:>8} cents")
        print(f"  Market-only PnL:       {comp['mkt_pnl'] or 0:>8} cents")
        # A3 gating comparison
        if "a3_gate_prob" in shadow_cols:
            a3_comp = conn.execute(f"""
                SELECT
                    SUM(a3_pnl_gate10_cents) AS g10,
                    SUM(a3_pnl_gate20_cents) AS g20,
                    SUM(a3_pnl_gate30_cents) AS g30
                FROM fifteenm_shadow_signals
                WHERE status = 'settled' AND evaluation_time >= ?
                    AND a3_gate_prob IS NOT NULL {asset_clause}
            """, (since,)).fetchone()
            if a3_comp and a3_comp["g10"] is not None:
                print(f"  A3 gated@10% PnL:      {a3_comp['g10'] or 0:>8} cents")
                print(f"  A3 gated@20% PnL:      {a3_comp['g20'] or 0:>8} cents")
                print(f"  A3 gated@30% PnL:      {a3_comp['g30'] or 0:>8} cents")
    else:
        print("  No settled signals yet for comparison")


# ── Section 12: NO-Side Shadow Analysis ──────────────────────────

def no_side_analysis(conn: sqlite3.Connection, since: str,
                     asset_filter: Optional[str] = None) -> None:
    section("12. NO-SIDE SHADOW ANALYSIS")

    # Check if side column exists on evaluated_opportunities
    cols = [r[1] for r in conn.execute(
        "PRAGMA table_info(evaluated_opportunities)").fetchall()]
    if "side" not in cols:
        print("  side column not found on evaluated_opportunities — skipping")
        return

    asset_clause = f"AND asset = '{asset_filter}'" if asset_filter else ""

    # Count NO-side entries from evaluated_opportunities (15M only)
    no_count = conn.execute(f"""
        SELECT COUNT(*) AS n FROM evaluated_opportunities
        WHERE (product_type IS NULL OR product_type = '15m')
          AND side = 'no' AND evaluation_time >= ? {asset_clause}
    """, (since,)).fetchone()["n"]

    if no_count == 0:
        print("  No NO-side shadow entries found.")
        return

    settled_count = conn.execute(f"""
        SELECT COUNT(*) AS n FROM evaluated_opportunities
        WHERE (product_type IS NULL OR product_type = '15m')
          AND side = 'no' AND market_result IS NOT NULL
          AND evaluation_time >= ? {asset_clause}
    """, (since,)).fetchone()["n"]

    print(f"  NO-side signals: {no_count} total, {settled_count} settled")

    if settled_count == 0:
        print("  No settled NO-side data yet.")
        return

    # Win/loss — NO wins when market_result IN ('no','all_no')
    wl = conn.execute(f"""
        SELECT
            SUM(CASE WHEN market_result IN ('no', 'all_no') THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN market_result IN ('yes', 'all_yes') THEN 1 ELSE 0 END) AS losses,
            AVG(market_price) AS avg_price,
            AVG(edge) AS avg_edge,
            AVG(seconds_to_close) AS avg_stc
        FROM evaluated_opportunities
        WHERE (product_type IS NULL OR product_type = '15m')
          AND side = 'no' AND market_result IS NOT NULL
          AND evaluation_time >= ? {asset_clause}
    """, (since,)).fetchone()

    wins = wl["wins"] or 0
    losses = wl["losses"] or 0
    wr = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0

    # Sim PnL (NO-side: win when result='no')
    sim_rows = conn.execute(f"""
        SELECT market_price, COALESCE(position_size, 1) AS cnt, market_result
        FROM evaluated_opportunities
        WHERE (product_type IS NULL OR product_type = '15m')
          AND side = 'no' AND market_result IS NOT NULL
          AND evaluation_time >= ? {asset_clause}
    """, (since,)).fetchall()

    sim_pnl = 0
    for r in sim_rows:
        p = r["market_price"] or 0
        c = r["cnt"]
        fee = 0  # Kalshi charges $0 on maker fills
        if r["market_result"] in ("no", "all_no"):
            sim_pnl += (100 - p) * c - fee
        elif r["market_result"] in ("yes", "all_yes"):
            sim_pnl -= p * c + fee

    print(f"  Win rate:     {wins}W/{losses}L ({wr:.1f}%)")
    print(f"  Sim PnL:      {sim_pnl} cents (${sim_pnl/100:.2f})")
    print(f"  Avg NO price: {wl['avg_price']:.1f}c")
    if wl["avg_edge"] is not None:
        print(f"  Avg edge:     {wl['avg_edge']*100:.2f}%")
    if wl["avg_stc"] is not None:
        print(f"  Avg STC:      {wl['avg_stc']:.0f}s ({wl['avg_stc']/60:.1f}m)")

    # Per-asset breakdown
    subsection("NO-side per-asset")
    assets = conn.execute(f"""
        SELECT asset,
            SUM(CASE WHEN market_result IN ('no', 'all_no') THEN 1 ELSE 0 END) AS w,
            SUM(CASE WHEN market_result IN ('yes', 'all_yes') THEN 1 ELSE 0 END) AS l,
            AVG(market_price) AS avg_p,
            AVG(edge) AS avg_e
        FROM evaluated_opportunities
        WHERE (product_type IS NULL OR product_type = '15m')
          AND side = 'no' AND market_result IS NOT NULL
          AND evaluation_time >= ? {asset_clause}
        GROUP BY asset ORDER BY (w - l) DESC
    """, (since,)).fetchall()

    print(f"  {'Asset':<6} {'W':>4} {'L':>4} {'WR':>7} {'Avg P':>7} {'Avg Edge':>9}")
    print("  " + "-" * 42)
    for a in assets:
        w = a["w"] or 0
        l = a["l"] or 0
        n = w + l
        wr_a = w / n * 100 if n > 0 else 0
        avg_e = f"{a['avg_e']*100:.2f}%" if a["avg_e"] is not None else "N/A"
        print(f"  {a['asset']:<6} {w:>4} {l:>4} {wr_a:>6.1f}% {a['avg_p']:>6.1f}c {avg_e:>9}")

    # Per filter_stage NO-side breakdown
    subsection("NO-side by filter stage")
    stage_rows = conn.execute(f"""
        SELECT filter_stage,
            COUNT(*) AS total,
            SUM(CASE WHEN market_result IN ('no', 'all_no') THEN 1 ELSE 0 END) AS w,
            SUM(CASE WHEN market_result IN ('yes', 'all_yes') THEN 1 ELSE 0 END) AS l,
            COALESCE(SUM(counterfactual_pnl), 0) AS pnl_cents,
            AVG(market_price) AS avg_p
        FROM evaluated_opportunities
        WHERE (product_type IS NULL OR product_type = '15m')
          AND side = 'no' AND market_result IS NOT NULL
          AND evaluation_time >= ? {asset_clause}
        GROUP BY filter_stage ORDER BY total DESC
    """, (since,)).fetchall()

    if stage_rows:
        print(f"  {'Stage':<35} {'N':>4} {'W':>4} {'L':>4} {'WR':>6} {'PnL':>10} {'AvgP':>5}")
        print("  " + "-" * 70)
        for r in stage_rows:
            w = r["w"] or 0
            l = r["l"] or 0
            n = w + l
            wr_s = f"{w/n*100:.1f}%" if n > 0 else "n/a"
            pnl = r["pnl_cents"] or 0
            print(f"  {r['filter_stage']:<35} {r['total']:>4} {w:>4} {l:>4} "
                  f"{wr_s:>6} ${pnl/100:>8.2f} {r['avg_p']:>5.1f}c")
    else:
        print("  No settled NO-side data by stage")

    # Edge distribution for NO-side
    subsection("NO-side edge distribution")
    edge_buckets = conn.execute(f"""
        SELECT
            CASE
                WHEN fee_adjusted_edge < 0 THEN '<0%'
                WHEN fee_adjusted_edge < 0.005 THEN '0-0.5%'
                WHEN fee_adjusted_edge < 0.01 THEN '0.5-1%'
                WHEN fee_adjusted_edge < 0.02 THEN '1-2%'
                WHEN fee_adjusted_edge < 0.05 THEN '2-5%'
                ELSE '5%+'
            END AS bucket,
            COUNT(*) AS n,
            SUM(CASE WHEN market_result IN ('no', 'all_no') THEN 1 ELSE 0 END) AS w,
            SUM(CASE WHEN market_result IN ('yes', 'all_yes') THEN 1 ELSE 0 END) AS l
        FROM evaluated_opportunities
        WHERE (product_type IS NULL OR product_type = '15m')
          AND side = 'no' AND market_result IS NOT NULL
          AND fee_adjusted_edge IS NOT NULL
          AND evaluation_time >= ? {asset_clause}
        GROUP BY bucket ORDER BY MIN(fee_adjusted_edge)
    """, (since,)).fetchall()

    if edge_buckets:
        print(f"  {'Bucket':<10} {'N':>4} {'W':>4} {'L':>4} {'WR':>7}")
        print("  " + "-" * 33)
        for b in edge_buckets:
            w = b["w"] or 0
            l = b["l"] or 0
            n = b["n"]
            wr_b = w / n * 100 if n > 0 else 0
            print(f"  {b['bucket']:<10} {n:>4} {w:>4} {l:>4} {wr_b:>6.1f}%")

    # Comparison: YES vs NO WR
    subsection("YES-side vs NO-side comparison")
    yes_wl = conn.execute(f"""
        SELECT
            SUM(CASE WHEN market_result IN ('yes', 'all_yes') THEN 1 ELSE 0 END) AS w,
            SUM(CASE WHEN market_result IN ('no', 'all_no') THEN 1 ELSE 0 END) AS l
        FROM evaluated_opportunities
        WHERE (product_type IS NULL OR product_type = '15m')
          AND (side IS NULL OR side = 'yes')
          AND filter_stage IN ('candidate', 'stc_shadow_no_xrp', 'stc_shadow_xrp',
                               'xrp_shadow', 'observation_trade')
          AND market_result IS NOT NULL
          AND evaluation_time >= ? {asset_clause}
    """, (since,)).fetchone()

    yes_w = yes_wl["w"] or 0
    yes_l = yes_wl["l"] or 0
    yes_n = yes_w + yes_l
    yes_wr = yes_w / yes_n * 100 if yes_n > 0 else 0

    print(f"  YES-side: {yes_w}W/{yes_l}L ({yes_wr:.1f}% WR, n={yes_n})")
    print(f"  NO-side:  {wins}W/{losses}L ({wr:.1f}% WR, n={wins + losses})")

    # fifteenm_shadow_signals NO-side approaches
    tbl = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='fifteenm_shadow_signals'"
    ).fetchone()
    if tbl:
        # Check if NO-side columns exist
        shadow_cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(fifteenm_shadow_signals)").fetchall()]
        has_no_cols = "no_live_pnl_cents" in shadow_cols

        if not has_no_cols:
            subsection("NO-side shadow approaches (fifteenm_shadow_signals)")
            print("  NO-side columns not found in fifteenm_shadow_signals — "
                  "schema not yet updated")
        else:
            subsection("NO-side shadow approaches (fifteenm_shadow_signals)")
            for prefix, label in [("no_a1", "NO A1 (RecalEGARCH)"),
                                   ("no_a2", "NO A2 (LightGBM)")]:
                contracts_col = f"{prefix}_contracts"
                pnl_col = f"{prefix}_pnl_cents"
                edge_col = f"{prefix}_fee_edge"
                if pnl_col not in shadow_cols or contracts_col not in shadow_cols:
                    continue
                print(f"\n  {label}:")
                for asset in (["BTC", "ETH", "SOL", "XRP"] if not asset_filter
                              else [asset_filter]):
                    try:
                        r = conn.execute(f"""
                            SELECT
                                COUNT(*) AS total,
                                SUM(CASE WHEN {contracts_col} > 0
                                    THEN 1 ELSE 0 END) AS signaled,
                                SUM(CASE WHEN status='settled'
                                    AND {contracts_col} > 0
                                    AND market_result IN ('no', 'all_no')
                                    THEN 1 ELSE 0 END) AS wins,
                                SUM(CASE WHEN status='settled'
                                    AND {contracts_col} > 0
                                    AND market_result IN ('yes', 'all_yes')
                                    THEN 1 ELSE 0 END) AS losses,
                                SUM(CASE WHEN status='settled'
                                    AND {contracts_col} > 0
                                    THEN {pnl_col} ELSE 0 END) AS pnl,
                                AVG(CASE WHEN {contracts_col} > 0
                                    THEN {edge_col} END) AS avg_edge
                            FROM fifteenm_shadow_signals
                            WHERE evaluation_time >= ? AND asset = ?
                        """, (since, asset)).fetchone()

                        t = r["total"] or 0
                        sig = r["signaled"] or 0
                        w = r["wins"] or 0
                        l_v = r["losses"] or 0
                        pnl_v = r["pnl"] or 0
                        n = w + l_v
                        wr_v = w / n * 100 if n > 0 else 0
                        sig_rate = sig / t * 100 if t > 0 else 0
                        avg_e = r["avg_edge"]
                        edge_str = f", edge {avg_e*100:.2f}%" if avg_e else ""

                        print(f"    {asset}: {t} evals, {sig} signaled "
                              f"({sig_rate:.1f}%)"
                              + (f", {w}W/{l_v}L ({wr_v:.1f}%), "
                                 f"PnL {pnl_v}c{edge_str}" if n > 0 else ""))
                    except Exception:
                        print(f"    {asset}: query error (column mismatch?)")
                        break

            # NO-side comparison: live baseline vs shadow approaches
            try:
                comp = conn.execute(f"""
                    SELECT
                        COUNT(*) AS n,
                        SUM(no_live_pnl_cents) AS live_pnl,
                        SUM(CASE WHEN no_a1_contracts > 0
                            THEN no_a1_pnl_cents ELSE 0 END) AS a1_pnl,
                        SUM(CASE WHEN no_a2_contracts > 0
                            THEN no_a2_pnl_cents ELSE 0 END) AS a2_pnl,
                        SUM(no_market_only_pnl_cents) AS mkt_pnl
                    FROM fifteenm_shadow_signals
                    WHERE status = 'settled' AND evaluation_time >= ?
                        {asset_clause}
                """, (since,)).fetchone()

                n = comp["n"] or 0
                if n > 0:
                    subsection("NO-side shadow PnL comparison")
                    print(f"  Settled signals: {n}")
                    print(f"  NO live baseline PnL:     "
                          f"{comp['live_pnl'] or 0:>8} cents")
                    print(f"  NO A1 (RecalEGARCH) PnL:  "
                          f"{comp['a1_pnl'] or 0:>8} cents")
                    print(f"  NO A2 (LightGBM) PnL:     "
                          f"{comp['a2_pnl'] or 0:>8} cents")
                    print(f"  NO Market-only PnL:       "
                          f"{comp['mkt_pnl'] or 0:>8} cents")

                    # YES vs NO side PnL comparison
                    yes_comp = conn.execute(f"""
                        SELECT
                            SUM(live_pnl_cents) AS live_pnl,
                            SUM(CASE WHEN a1_gates_passed = 1
                                THEN a1_pnl_cents ELSE 0 END) AS a1_pnl,
                            SUM(CASE WHEN a2_gates_passed = 1
                                THEN a2_pnl_cents ELSE 0 END) AS a2_pnl,
                            SUM(market_only_pnl_cents) AS mkt_pnl
                        FROM fifteenm_shadow_signals
                        WHERE status = 'settled' AND evaluation_time >= ?
                            {asset_clause}
                    """, (since,)).fetchone()

                    print(f"\n  YES vs NO comparison:")
                    print(f"  {'Approach':<22} {'YES PnL':>10} {'NO PnL':>10} "
                          f"{'Combined':>10}")
                    print("  " + "-" * 55)
                    for lbl, y_key, n_key in [
                        ("Live baseline", "live_pnl", "live_pnl"),
                        ("A1 (RecalEGARCH)", "a1_pnl", "a1_pnl"),
                        ("A2 (LightGBM)", "a2_pnl", "a2_pnl"),
                        ("Market-only", "mkt_pnl", "mkt_pnl"),
                    ]:
                        y_v = yes_comp[y_key] or 0
                        n_v = comp[n_key] or 0
                        print(f"  {lbl:<22} {y_v:>9}c {n_v:>9}c "
                              f"{y_v + n_v:>9}c")
            except Exception:
                pass  # NO-side PnL columns may not exist yet


# ── Section 13: Recommendations ──────────────────────────────────

def recommendations(conn: sqlite3.Connection, since: str,
                    perf: dict) -> None:
    section("13. DATA-DRIVEN RECOMMENDATIONS")

    trades = perf.get("trades", 0)
    if trades == 0:
        print("  No trades — cannot generate recommendations")
        return

    wr = perf.get("wr", 0)
    pnl = perf.get("pnl", 0)

    print(f"  Regime: {trades} trades, {wr:.1f}% WR, "
          f"PnL ${pnl/100:.2f}\n")

    # Auto-recommendations based on data
    recs = []

    # R1: STC shadow expansion
    shadow_row = conn.execute(f"""
        SELECT COUNT(*) AS n,
          SUM(CASE WHEN status='settled' AND market_result='yes'
              THEN 1 ELSE 0 END) AS w,
          SUM(CASE WHEN status='settled' AND market_result='no'
              THEN 1 ELSE 0 END) AS l
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER}
          AND filter_stage IN ('stc_shadow', 'stc_shadow_xrp')
    """, (since,)).fetchone()
    sw = shadow_row["w"] or 0
    sl = shadow_row["l"] or 0
    sn = sw + sl
    if sn >= 30 and sw / sn > 0.90:
        recs.append(("HIGH", "Promote STC shadow to live (500→600s)",
                      f"{sw}W/{sl}L ({sw/sn*100:.0f}% WR) — statistically sufficient"))
    elif sn > 0:
        recs.append(("WAIT", f"STC shadow: {sw}W/{sl}L ({sn} obs, need 30)",
                      "Keep collecting shadow data"))
    else:
        recs.append(("WAIT", "STC shadow: 0 observations",
                      "Shadow zone collecting — revisit in 2 weeks"))

    # R2: MIN_ENTRY at 86c
    por86 = conn.execute(f"""
        SELECT COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
          SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) AS l
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER}
          AND filter_stage = 'price_out_of_range'
          AND market_price = 86 AND status = 'settled'
    """, (since,)).fetchone()
    pw, pl = (por86["w"] or 0), (por86["l"] or 0)
    pn = pw + pl
    if pn >= 30 and pw / pn > 0.91:
        recs.append(("INFO", f"MIN_ENTRY=86c performing well",
                      f"{pw}W/{pl}L ({pw/pn*100:.0f}% WR) — already active"))
    elif pn > 0:
        recs.append(("WAIT", f"MIN_ENTRY 86c: {pw}W/{pl}L ({pn} obs, need 30)",
                      "Keep collecting counterfactual data"))

    # R3: Edge threshold
    ie_marginal = conn.execute(f"""
        SELECT COUNT(*) AS n,
          SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
          SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) AS l
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER}
          AND filter_stage = 'insufficient_edge'
          AND fee_adjusted_edge BETWEEN 0.001 AND 0.0025
          AND status = 'settled'
    """, (since,)).fetchone()
    ew, el = (ie_marginal["w"] or 0), (ie_marginal["l"] or 0)
    en = ew + el
    if en >= 30 and ew / en > 0.92:
        recs.append(("MEDIUM", "Edge below current 0.25% threshold performing well",
                      f"{ew}W/{el}L — sub-threshold candidates are winning"))
    elif en > 0:
        recs.append(("WAIT", f"Sub-threshold edge (0.1-0.25%): {ew}W/{el}L "
                      f"({en} obs, need 30)", "Accumulating data"))

    # R4: Taker WR monitoring
    taker_row = conn.execute(f"""
        SELECT SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS w,
               SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) AS l
        FROM settled_trades
        WHERE settled_at >= ? {SETTLED_15M_FILTER}
          AND strategy = 'TAKER_NOW'
    """, (since,)).fetchone()
    tw, tl = (taker_row["w"] or 0), (taker_row["l"] or 0)
    if tw + tl >= 20 and tw / (tw + tl) < 0.85:
        recs.append(("MEDIUM", f"Investigate taker quality: "
                      f"{tw}W/{tl}L ({tw/(tw+tl)*100:.0f}% WR)",
                      "Consider raising MAKER_ONLY_THRESHOLD"))
    elif tw + tl > 0:
        taker_wr = tw / (tw + tl) * 100
        recs.append(("MONITOR", f"Taker WR: {tw}W/{tl}L ({taker_wr:.0f}%)",
                      f"{'Concerning' if taker_wr < 90 else 'Acceptable'} "
                      f"— need more data"))

    # R5: Price shadow (70-85c) readiness
    ps_row = conn.execute(f"""
        SELECT COUNT(*) AS n,
          SUM(CASE WHEN status='settled' AND market_result='yes'
              THEN 1 ELSE 0 END) AS w,
          SUM(CASE WHEN status='settled' AND market_result='no'
              THEN 1 ELSE 0 END) AS l
        FROM evaluated_opportunities
        WHERE evaluation_time >= ? {EVAL_15M_FILTER}
          AND filter_stage IN ('price_shadow', 'price_shadow_xrp')
    """, (since,)).fetchone()
    ps_n = ps_row["n"] or 0
    ps_w = ps_row["w"] or 0
    ps_l = ps_row["l"] or 0
    ps_settled = ps_w + ps_l
    if ps_settled >= 30:
        ps_wr = ps_w / ps_settled * 100
        if ps_wr >= 85:
            recs.append(("HIGH", f"Price shadow 70-85c: {ps_w}W/{ps_l}L ({ps_wr:.0f}%)",
                          "Consider lowering MIN_ENTRY_PRICE"))
        else:
            recs.append(("INFO", f"Price shadow 70-85c: {ps_w}W/{ps_l}L ({ps_wr:.0f}%)",
                          "WR below 85% — keep current MIN_ENTRY=86"))
    elif ps_n > 0:
        recs.append(("WAIT", f"Price shadow: {ps_n} obs, {ps_settled} settled",
                      "Collecting 70-85c edge data — need 30 settlements"))

    print(f"  {'#':>3} {'Priority':<10} {'Recommendation':<45} {'Rationale'}")
    print("  " + "-" * 90)
    for i, (pri, rec, rat) in enumerate(recs, 1):
        print(f"  {i:>3} {pri:<10} {rec:<45} {rat}")

    if not recs:
        print("  No recommendations — all filters correctly calibrated")


# ── Main ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="15M live trading audit")
    parser.add_argument("--db", default="/tmp/state.db",
                        help="Path to state.db")
    parser.add_argument("--since", default=None,
                        help="Start date (YYYY-MM-DD)")
    parser.add_argument("--regime", choices=["auto"],
                        help="Auto-detect regime start")
    parser.add_argument("--asset", default=None,
                        help="Filter to single asset (BTC/ETH/SOL/XRP)")
    parser.add_argument("--json", default=None,
                        help="Write JSON artifact summary to this path")
    args = parser.parse_args()

    conn = connect_db(args.db)

    # Determine since timestamp
    if args.regime == "auto":
        since = detect_regime_start(conn)
        print(f"[Auto-detected regime start: {since}]")
    elif args.since:
        since = args.since
    else:
        since = "2026-02-28T00:00:00"
    print(f"[Analyzing from: {since}]")
    if args.asset:
        print(f"[Filtered to asset: {args.asset}]")

    perf = performance_summary(conn, since, args.asset)
    if perf["trades"] == 0:
        print("\nNo trades found. Check --since date and --db path.")
        conn.close()
        return

    execution_quality(conn, since, args.asset)
    bucket_analysis(conn, since, args.asset)
    profit_leakage(conn, since, args.asset)
    config_sensitivity(conn, since, args.asset)
    cal_grid = calibration_grid_search(conn, since, args.asset)
    data_sufficiency(conn, since)
    price_shadow_analysis(conn, since, args.asset)
    shadow_approaches(conn, since, args.asset)
    no_side_analysis(conn, since, args.asset)
    recommendations(conn, since, perf)

    # JSON artifact output
    if args.json:
        artifact = {
            "since": since,
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "trades": perf["trades"],
            "wins": perf["wins"],
            "losses": perf["losses"],
            "pnl_cents": perf["pnl"],
            "fees_cents": perf["fees"],
            "win_rate": round(perf["wr"] / 100, 4) if perf["wr"] else 0,
        }
        # Per-asset breakdown
        asset_rows = conn.execute(f"""
            SELECT asset, COUNT(*) AS n,
              SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS wins,
              SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) AS losses,
              SUM(pnl_cents - COALESCE(fee_cents, 0)) AS pnl
            FROM settled_trades
            WHERE settled_at >= ? {SETTLED_15M_FILTER}
            GROUP BY asset
        """, (since,)).fetchall()
        artifact["by_asset"] = {
            r["asset"]: {"n": r["n"], "wins": r["wins"] or 0,
                         "losses": r["losses"] or 0, "pnl": r["pnl"] or 0}
            for r in asset_rows
        }
        if cal_grid and cal_grid.get("best_config"):
            artifact["calibration_grid"] = cal_grid
        try:
            with open(args.json, "w") as f:
                json.dump(artifact, f, indent=2)
            print(f"\nJSON artifact written to {args.json}")
        except Exception as e:
            print(f"\nERROR writing JSON: {e}")

    conn.close()
    print(f"\n{'=' * 72}")
    print("  Audit complete.")
    print(f"{'=' * 72}")


if __name__ == "__main__":
    main()
