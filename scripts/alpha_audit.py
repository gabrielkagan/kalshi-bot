#!/usr/bin/env python3
"""Comprehensive opportunity analysis script for 15M crypto trading.

Analyzes the filter funnel, rejection quality, counterfactual PnL,
win rates by STC/z-score/asset, capital utilization, shadow strategies,
and weekend/weekday splits.

Usage:
    scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
    python3 scripts/alpha_audit.py --db /tmp/state.db
    python3 scripts/alpha_audit.py --db /tmp/state.db --days 7 --asset BTC
"""

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone


# ── Constants ────────────────────────────────────────────────────────

# 15M filter: exclude hourly/weather/sports/spx tickers
EVAL_15M_FILTER = (
    "AND (product_type IS NULL OR product_type NOT IN "
    "('hourly', 'weather', 'sports', 'spx_hourly'))"
)
SETTLED_15M_FILTER = "AND event_ticker NOT LIKE '%D-%'"

SHADOW_STAGES = [
    "decided_contract_t1",
    "decided_contract_t2",
    "relaxed_edge_shadow",
    "weekend_discount_shadow",
    "overnight_discount_shadow",
]

STC_BUCKETS = [
    (0, 100, "0-100s (risk zone)"),
    (100, 200, "100-200s"),
    (200, 300, "200-300s"),
    (300, 500, "300-500s"),
    (500, 900, "500-900s (shadow)"),
]


# ── Helpers ──────────────────────────────────────────────────────────

def connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def section(num: int, title: str) -> None:
    print(f"\n{'=' * 72}")
    print(f"  Section {num}: {title}")
    print(f"{'=' * 72}\n")


def taker_fee_cents(contracts: int, price_cents: int) -> int:
    """Taker fee: ceil(0.07 * C * P/100 * (1-P/100)) in cents."""
    p = price_cents / 100.0
    return math.ceil(0.07 * contracts * p * (1 - p))


def breakeven_wr(price_cents: int) -> float:
    """Breakeven win rate at price P with 1-contract taker fee."""
    fee = taker_fee_cents(1, price_cents)
    return (price_cents + fee) / 100.0


def validate_columns(conn: sqlite3.Connection, table: str, required: list[str]) -> list[str]:
    """Check which required columns exist in the table. Returns missing columns."""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    existing = {row["name"] for row in cursor.fetchall()}
    return [c for c in required if c not in existing]


def fmt_pnl(cents: float) -> str:
    """Format PnL in dollars with sign."""
    return f"${cents / 100:+.2f}" if cents else "$0.00"


def fmt_pct(val: float) -> str:
    """Format as percentage."""
    return f"{val:.1%}"


def fmt_wr(wins: int, total: int) -> str:
    """Format win rate with fraction."""
    if total == 0:
        return "N/A (0)"
    return f"{wins/total:.1%} ({wins}/{total})"


# ── Section Implementations ──────────────────────────────────────────

def section_1_filter_funnel(conn: sqlite3.Connection, since: str, asset_filter: str):
    """Section 1: Filter Funnel — evaluated opportunity flow."""
    section(1, "Filter Funnel")

    rows = conn.execute(f"""
        SELECT filter_stage, COUNT(*) as cnt
        FROM evaluated_opportunities
        WHERE evaluation_time >= ?
        {EVAL_15M_FILTER}
        {asset_filter}
        GROUP BY filter_stage
        ORDER BY cnt DESC
    """, (since,)).fetchall()

    if not rows:
        print("  No evaluated opportunities found in lookback window.")
        return

    total = sum(r["cnt"] for r in rows)
    candidate_count = 0

    print(f"  Total evaluated opportunities: {total}")
    print()
    print(f"  {'Filter Stage':<35} {'Count':>8} {'Pct':>8}")
    print(f"  {'-'*35} {'-'*8} {'-'*8}")
    for r in rows:
        pct = r["cnt"] / total if total else 0
        label = r["filter_stage"]
        if label == "candidate":
            candidate_count = r["cnt"]
            label = "candidate ***"
        print(f"  {label:<35} {r['cnt']:>8} {pct:>7.1%}")

    print()
    if total > 0:
        print(f"  Candidate conversion rate: {candidate_count}/{total} = "
              f"{candidate_count/total:.2%}")


def section_2_rejection_by_price(conn: sqlite3.Connection, since: str, asset_filter: str):
    """Section 2: Rejection Analysis by Price Band (insufficient_edge)."""
    section(2, "Rejection Analysis by Price Band (insufficient_edge)")

    rows = conn.execute(f"""
        SELECT
            market_price,
            COUNT(*) as cnt,
            SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled,
            SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes')
                THEN 1 ELSE 0 END) as wins
        FROM evaluated_opportunities
        WHERE evaluation_time >= ?
        AND filter_stage = 'insufficient_edge'
        AND market_price BETWEEN 86 AND 99
        {EVAL_15M_FILTER}
        {asset_filter}
        GROUP BY market_price
        ORDER BY market_price
    """, (since,)).fetchall()

    if not rows:
        print("  No insufficient_edge rejections at 86-99c found.")
        return

    print(f"  {'Price':>5} {'Count':>7} {'Settled':>8} {'Wins':>6} {'WR%':>8} "
          f"{'BE WR':>8} {'Delta':>8} {'Flag':>12}")
    print(f"  {'-'*5} {'-'*7} {'-'*8} {'-'*6} {'-'*8} {'-'*8} {'-'*8} {'-'*12}")

    for r in rows:
        p = r["market_price"]
        settled = r["settled"]
        wins = r["wins"] or 0
        wr = wins / settled if settled else 0
        be = breakeven_wr(p)
        delta = wr - be if settled else 0
        flag = "OPPORTUNITY" if settled > 0 and delta > 0.02 else ""

        wr_str = fmt_pct(wr) if settled else "N/A"
        delta_str = f"{delta:+.1%}" if settled else "N/A"

        print(f"  {p:>5}c {r['cnt']:>7} {settled:>8} {wins:>6} "
              f"{wr_str:>8} {fmt_pct(be):>8} {delta_str:>8} {flag:>12}")


def section_3_counterfactual_pnl(conn: sqlite3.Connection, since: str,
                                  asset_filter: str, days: int):
    """Section 3: Counterfactual PnL on Rejected Trades."""
    section(3, "Counterfactual PnL on Rejected Trades (insufficient_edge, 86-99c)")

    rows = conn.execute(f"""
        SELECT
            market_price,
            SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes')
                THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN status='settled' AND market_result NOT IN ('yes','all_yes')
                THEN 1 ELSE 0 END) as losses,
            AVG(CASE WHEN position_size IS NOT NULL AND position_size > 0
                THEN position_size ELSE 1 END) as avg_size
        FROM evaluated_opportunities
        WHERE evaluation_time >= ?
        AND filter_stage = 'insufficient_edge'
        AND market_price BETWEEN 86 AND 99
        AND status = 'settled'
        {EVAL_15M_FILTER}
        {asset_filter}
        GROUP BY market_price
        ORDER BY market_price
    """, (since,)).fetchall()

    if not rows:
        print("  No settled insufficient_edge rejections at 86-99c.")
        return

    total_pnl = 0
    print(f"  {'Price':>5} {'Wins':>6} {'Losses':>7} {'PnL/contract':>14} "
          f"{'AvgSize':>8} {'Total PnL':>11} {'Daily Rate':>11}")
    print(f"  {'-'*5} {'-'*6} {'-'*7} {'-'*14} {'-'*8} {'-'*11} {'-'*11}")

    for r in rows:
        p = r["market_price"]
        wins = r["wins"] or 0
        losses = r["losses"] or 0
        avg_size = r["avg_size"] or 1
        fee_per = taker_fee_cents(1, p)

        # PnL per contract: wins get (100 - price - fee), losses pay price
        pnl_per_contract = wins * (100 - p - fee_per) - losses * p
        total_pnl_cents = pnl_per_contract * avg_size
        daily = total_pnl_cents / days if days > 0 else 0
        total_pnl += total_pnl_cents

        print(f"  {p:>5}c {wins:>6} {losses:>7} {pnl_per_contract:>13}c "
              f"{avg_size:>8.1f} {fmt_pnl(total_pnl_cents):>11} {fmt_pnl(daily):>11}/d")

    print(f"\n  Total counterfactual PnL: {fmt_pnl(total_pnl)} "
          f"({fmt_pnl(total_pnl / days)}/day)" if days > 0 else "")


def section_4_wr_by_stc(conn: sqlite3.Connection, since: str, asset_filter: str):
    """Section 4: Win Rate by STC Bucket."""
    section(4, "Win Rate by STC Bucket (settled_trades)")

    # Build CASE expression for buckets
    rows = conn.execute(f"""
        SELECT
            CASE
                WHEN seconds_to_close < 100 THEN '0-100'
                WHEN seconds_to_close < 200 THEN '100-200'
                WHEN seconds_to_close < 300 THEN '200-300'
                WHEN seconds_to_close < 500 THEN '300-500'
                WHEN seconds_to_close < 900 THEN '500-900'
                ELSE '900+'
            END as stc_bucket,
            COUNT(*) as trades,
            SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) as wins,
            SUM(pnl_cents) as pnl
        FROM settled_trades
        WHERE settled_at >= ?
        AND seconds_to_close IS NOT NULL
        {SETTLED_15M_FILTER}
        {asset_filter}
        GROUP BY stc_bucket
        ORDER BY MIN(seconds_to_close)
    """, (since,)).fetchall()

    if not rows:
        print("  No settled trades with STC data found.")
        return

    print(f"  {'STC Bucket':<22} {'Trades':>7} {'Wins':>6} {'WR%':>8} {'PnL':>12}")
    print(f"  {'-'*22} {'-'*7} {'-'*6} {'-'*8} {'-'*12}")

    for r in rows:
        trades = r["trades"]
        wins = r["wins"] or 0
        pnl = r["pnl"] or 0
        label = r["stc_bucket"]
        if label == "0-100":
            label = "0-100s ** RISK **"
        elif label == "500-900":
            label = "500-900s (shadow)"

        print(f"  {label:<22} {trades:>7} {wins:>6} {fmt_pct(wins/trades):>8} "
              f"{fmt_pnl(pnl):>12}")


def section_5_wr_by_zscore(conn: sqlite3.Connection, since: str, asset_filter: str):
    """Section 5: Win Rate by Z-Score (insufficient_edge rejections)."""
    section(5, "Win Rate by Z-Score (insufficient_edge, settled)")

    rows = conn.execute(f"""
        SELECT
            CAST(ROUND(z_score) AS INTEGER) as z_bucket,
            COUNT(*) as cnt,
            SUM(CASE WHEN market_result IN ('yes','all_yes') THEN 1 ELSE 0 END) as wins,
            AVG(market_price) as avg_price
        FROM evaluated_opportunities
        WHERE evaluation_time >= ?
        AND filter_stage = 'insufficient_edge'
        AND status = 'settled'
        AND z_score IS NOT NULL
        {EVAL_15M_FILTER}
        {asset_filter}
        GROUP BY z_bucket
        ORDER BY z_bucket
    """, (since,)).fetchall()

    if not rows:
        print("  No settled insufficient_edge rejections with z_score data.")
        return

    print(f"  {'Z-Score':>8} {'Count':>7} {'Wins':>6} {'WR%':>8} {'Avg Price':>10} {'Note':>20}")
    print(f"  {'-'*8} {'-'*7} {'-'*6} {'-'*8} {'-'*10} {'-'*20}")

    for r in rows:
        z = r["z_bucket"]
        cnt = r["cnt"]
        wins = r["wins"] or 0
        avg_p = r["avg_price"] or 0
        note = "decided contract" if z is not None and z <= -5 else ""

        print(f"  {z:>8} {cnt:>7} {wins:>6} {fmt_pct(wins/cnt):>8} "
              f"{avg_p:>9.0f}c {note:>20}")


def section_6_asset_performance(conn: sqlite3.Connection, since: str, asset_filter: str):
    """Section 6: Asset-Level Performance."""
    section(6, "Asset-Level Performance (settled_trades)")

    rows = conn.execute(f"""
        SELECT
            asset,
            COUNT(*) as trades,
            SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) as wins,
            SUM(pnl_cents) as pnl,
            AVG(entry_price_cents) as avg_price,
            AVG(count) as avg_contracts
        FROM settled_trades
        WHERE settled_at >= ?
        {SETTLED_15M_FILTER}
        {asset_filter}
        GROUP BY asset
        ORDER BY SUM(pnl_cents) DESC
    """, (since,)).fetchall()

    if not rows:
        print("  No settled trades found.")
        return

    print(f"  {'Asset':<6} {'Trades':>7} {'Wins':>6} {'WR%':>8} {'PnL':>12} "
          f"{'Avg Price':>10} {'Avg Ctrs':>9}")
    print(f"  {'-'*6} {'-'*7} {'-'*6} {'-'*8} {'-'*12} {'-'*10} {'-'*9}")

    total_trades = 0
    total_wins = 0
    total_pnl = 0

    for r in rows:
        trades = r["trades"]
        wins = r["wins"] or 0
        pnl = r["pnl"] or 0
        total_trades += trades
        total_wins += wins
        total_pnl += pnl

        print(f"  {r['asset']:<6} {trades:>7} {wins:>6} {fmt_pct(wins/trades):>8} "
              f"{fmt_pnl(pnl):>12} {r['avg_price'] or 0:>9.0f}c {r['avg_contracts'] or 0:>9.1f}")

    print(f"  {'-'*6} {'-'*7} {'-'*6} {'-'*8} {'-'*12}")
    print(f"  {'TOTAL':<6} {total_trades:>7} {total_wins:>6} "
          f"{fmt_pct(total_wins/total_trades) if total_trades else 'N/A':>8} "
          f"{fmt_pnl(total_pnl):>12}")


def section_7_capital_utilization(conn: sqlite3.Connection, since: str,
                                   asset_filter: str, days: int):
    """Section 7: Capital Utilization."""
    section(7, "Capital Utilization")

    row = conn.execute(f"""
        SELECT
            COUNT(*) as trades,
            AVG(count) as avg_contracts,
            AVG(count * entry_price_cents) as avg_deployed_cents
        FROM settled_trades
        WHERE settled_at >= ?
        {SETTLED_15M_FILTER}
        {asset_filter}
    """, (since,)).fetchone()

    if not row or not row["trades"]:
        print("  No settled trades found.")
        return

    trades = row["trades"]
    avg_contracts = row["avg_contracts"] or 0
    avg_deployed = row["avg_deployed_cents"] or 0

    # Get latest balance from evaluated_opportunities
    bal_row = conn.execute(f"""
        SELECT available_balance_cents
        FROM evaluated_opportunities
        WHERE evaluation_time >= ?
        AND available_balance_cents IS NOT NULL
        AND available_balance_cents > 0
        {EVAL_15M_FILTER}
        ORDER BY evaluation_time DESC LIMIT 1
    """, (since,)).fetchone()

    balance = bal_row["available_balance_cents"] if bal_row else None
    trades_per_day = trades / days if days > 0 else 0

    print(f"  Trades in period:         {trades}")
    print(f"  Trades per day:           {trades_per_day:.1f}")
    print(f"  Avg contracts per trade:  {avg_contracts:.1f}")
    print(f"  Avg capital per trade:    {fmt_pnl(avg_deployed)}")
    if balance:
        utilization = avg_deployed / balance if balance else 0
        print(f"  Latest balance:           {fmt_pnl(balance)}")
        print(f"  Utilization per trade:    {utilization:.1%}")
    else:
        print("  (balance data not available for utilization estimate)")


def section_8_shadow_strategies(conn: sqlite3.Connection, since: str, asset_filter: str):
    """Section 8: Shadow Strategy Status."""
    section(8, "Shadow Strategy Status")

    print(f"  {'Stage':<30} {'Count':>6} {'Settled':>8} {'Wins':>6} "
          f"{'WR%':>8} {'Sim PnL':>10} {'Ready?':>10}")
    print(f"  {'-'*30} {'-'*6} {'-'*8} {'-'*6} {'-'*8} {'-'*10} {'-'*10}")

    any_data = False
    for stage in SHADOW_STAGES:
        row = conn.execute(f"""
            SELECT
                COUNT(*) as cnt,
                SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled,
                SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes')
                    THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl
            FROM evaluated_opportunities
            WHERE evaluation_time >= ?
            AND filter_stage = ?
            {EVAL_15M_FILTER}
            {asset_filter}
        """, (since, stage)).fetchone()

        cnt = row["cnt"] or 0
        if cnt == 0:
            print(f"  {stage:<30} {0:>6} {0:>8} {0:>6} {'N/A':>8} {'N/A':>10} {'NO DATA':>10}")
            continue

        any_data = True
        settled = row["settled"] or 0
        wins = row["wins"] or 0
        sim_pnl = row["sim_pnl"] or 0

        wr = wins / settled if settled else 0

        # Promotion readiness: WR > breakeven+2pp AND settled > 50 AND PnL > 0
        # Use avg price for breakeven estimate
        avg_price_row = conn.execute(f"""
            SELECT AVG(market_price) as avg_p
            FROM evaluated_opportunities
            WHERE evaluation_time >= ?
            AND filter_stage = ?
            AND status = 'settled'
            {EVAL_15M_FILTER}
            {asset_filter}
        """, (since, stage)).fetchone()
        avg_p = avg_price_row["avg_p"] if avg_price_row and avg_price_row["avg_p"] else 90
        be = breakeven_wr(int(avg_p))

        ready = "YES" if (settled > 50 and wr > be + 0.02 and sim_pnl > 0) else "NO"

        print(f"  {stage:<30} {cnt:>6} {settled:>8} {wins:>6} "
              f"{fmt_pct(wr):>8} {fmt_pnl(sim_pnl):>10} {ready:>10}")

    if not any_data:
        print("\n  No shadow strategy data found. Shadow features may not be active yet.")


def section_9_weekend_weekday(conn: sqlite3.Connection, since: str, asset_filter: str):
    """Section 9: Weekend vs Weekday Performance."""
    section(9, "Weekend vs Weekday Performance (settled_trades)")

    rows = conn.execute(f"""
        SELECT
            CASE WHEN CAST(strftime('%w', settled_at) AS INTEGER) IN (0, 6)
                THEN 'Weekend' ELSE 'Weekday' END as period,
            COUNT(*) as trades,
            SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) as wins,
            SUM(pnl_cents) as pnl
        FROM settled_trades
        WHERE settled_at >= ?
        {SETTLED_15M_FILTER}
        {asset_filter}
        GROUP BY period
        ORDER BY period
    """, (since,)).fetchall()

    if not rows:
        print("  No settled trades found.")
        return

    print(f"  {'Period':<10} {'Trades':>7} {'Wins':>6} {'WR%':>8} "
          f"{'PnL':>12} {'PnL/Trade':>12}")
    print(f"  {'-'*10} {'-'*7} {'-'*6} {'-'*8} {'-'*12} {'-'*12}")

    for r in rows:
        trades = r["trades"]
        wins = r["wins"] or 0
        pnl = r["pnl"] or 0
        per_trade = pnl / trades if trades else 0

        print(f"  {r['period']:<10} {trades:>7} {wins:>6} {fmt_pct(wins/trades):>8} "
              f"{fmt_pnl(pnl):>12} {fmt_pnl(per_trade):>12}")


def section_10_summary(conn: sqlite3.Connection, since: str, asset_filter: str, days: int):
    """Section 10: Summary & Recommendations."""
    section(10, "Summary & Recommendations")

    opportunities = []

    # Check 1: Rejected trades with positive counterfactual PnL
    rej_row = conn.execute(f"""
        SELECT
            SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes')
                THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN status='settled' AND market_result NOT IN ('yes','all_yes')
                THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl,
            COUNT(CASE WHEN status='settled' THEN 1 END) as settled
        FROM evaluated_opportunities
        WHERE evaluation_time >= ?
        AND filter_stage = 'insufficient_edge'
        AND market_price BETWEEN 86 AND 99
        {EVAL_15M_FILTER}
        {asset_filter}
    """, (since,)).fetchone()

    if rej_row and rej_row["settled"] and rej_row["settled"] > 10:
        sim_pnl = rej_row["sim_pnl"] or 0
        settled = rej_row["settled"]
        wins = rej_row["wins"] or 0
        wr = wins / settled if settled else 0
        daily_pnl = sim_pnl / days if days > 0 else 0
        if sim_pnl > 0:
            opportunities.append((
                abs(daily_pnl),
                f"Edge threshold relaxation: {settled} rejected trades at 86-99c "
                f"show {fmt_pct(wr)} WR, {fmt_pnl(sim_pnl)} sim PnL "
                f"({fmt_pnl(daily_pnl)}/day). Consider selective threshold loosening."
            ))

    # Check 2: Shadow strategies ready for promotion
    for stage in SHADOW_STAGES:
        srow = conn.execute(f"""
            SELECT
                SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled,
                SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes')
                    THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl
            FROM evaluated_opportunities
            WHERE evaluation_time >= ?
            AND filter_stage = ?
            {EVAL_15M_FILTER}
            {asset_filter}
        """, (since, stage)).fetchone()

        settled = srow["settled"] or 0
        if settled > 50:
            wins = srow["wins"] or 0
            sim_pnl = srow["sim_pnl"] or 0
            wr = wins / settled
            daily_pnl = sim_pnl / days if days > 0 else 0
            if sim_pnl > 0 and wr > 0.85:
                opportunities.append((
                    abs(daily_pnl),
                    f"Shadow promotion: {stage} has {settled} settled, "
                    f"{fmt_pct(wr)} WR, {fmt_pnl(sim_pnl)} sim PnL "
                    f"({fmt_pnl(daily_pnl)}/day). Meets graduation criteria."
                ))

    # Check 3: WR regression detection
    # Compare first half vs second half of the period
    first_trades = first_wins = second_trades = second_wins = 0
    mid_dt = (datetime.fromisoformat(since) +
              timedelta(days=days / 2)).strftime("%Y-%m-%d")
    for label, time_filter in [("first half", f"settled_at >= '{since}' AND settled_at < '{mid_dt}'"),
                                ("second half", f"settled_at >= '{mid_dt}'")]:
        rrow = conn.execute(f"""
            SELECT
                COUNT(*) as trades,
                SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) as wins
            FROM settled_trades
            WHERE {time_filter}
            {SETTLED_15M_FILTER}
            {asset_filter}
        """).fetchone()
        if rrow:
            if label == "first half":
                first_trades = rrow["trades"] or 0
                first_wins = rrow["wins"] or 0
            else:
                second_trades = rrow["trades"] or 0
                second_wins = rrow["wins"] or 0

    if first_trades > 5 and second_trades > 5:
        first_wr = first_wins / first_trades
        second_wr = second_wins / second_trades
        if second_wr < first_wr - 0.05:
            opportunities.append((
                0,  # regression, not positive opportunity
                f"REGRESSION: WR declined from {fmt_pct(first_wr)} (first half, n={first_trades}) "
                f"to {fmt_pct(second_wr)} (second half, n={second_trades}). "
                f"Delta: {second_wr - first_wr:+.1%}. Investigate loss patterns."
            ))

    # Sort by daily impact (descending)
    opportunities.sort(key=lambda x: x[0], reverse=True)

    if opportunities:
        print("  Top Opportunities (ranked by estimated daily impact):\n")
        for i, (daily, desc) in enumerate(opportunities[:5], 1):
            print(f"  {i}. {desc}")
            print()
    else:
        print("  No significant opportunities or regressions detected.")
        print("  Current configuration appears well-tuned for the lookback period.")


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Comprehensive 15M opportunity analysis")
    parser.add_argument("--db", default="../state.db",
                        help="Path to state.db (default: ../state.db)")
    parser.add_argument("--days", type=int, default=14,
                        help="Lookback window in days (default: 14)")
    parser.add_argument("--asset", type=str, default=None,
                        help="Filter to specific asset (e.g. BTC, SOL)")
    args = parser.parse_args()

    # Connect
    try:
        conn = connect_db(args.db)
    except Exception as e:
        print(f"ERROR: Cannot open database at {args.db}: {e}", file=sys.stderr)
        sys.exit(1)

    # Schema validation
    missing_eval = validate_columns(conn, "evaluated_opportunities", [
        "ticker", "asset", "filter_stage", "market_price", "seconds_to_close",
        "calibrated_prob", "edge", "fee_adjusted_edge", "z_score",
        "market_result", "status", "counterfactual_pnl", "position_size",
        "evaluation_time", "product_type",
    ])
    missing_settled = validate_columns(conn, "settled_trades", [
        "ticker", "asset", "market_result", "count", "entry_price_cents",
        "pnl_cents", "fee_cents", "settled_at", "seconds_to_close",
        "product_type", "event_ticker",
    ])

    if missing_eval:
        print(f"WARNING: evaluated_opportunities missing columns: {missing_eval}",
              file=sys.stderr)
    if missing_settled:
        print(f"WARNING: settled_trades missing columns: {missing_settled}",
              file=sys.stderr)

    # Compute date range
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime(
        "%Y-%m-%dT%H:%M:%S")

    # Asset filter clause (for both tables)
    asset_filter = ""
    if args.asset:
        asset_filter = f"AND asset = '{args.asset.upper()}'"

    # Header
    print("=" * 72)
    print("  ALPHA AUDIT — Comprehensive Opportunity Analysis")
    print(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Lookback:  {args.days} days (since {since[:10]})")
    if args.asset:
        print(f"  Asset:     {args.asset.upper()}")
    print(f"  Database:  {args.db}")
    print("=" * 72)

    # Run all sections
    section_1_filter_funnel(conn, since, asset_filter)
    section_2_rejection_by_price(conn, since, asset_filter)
    section_3_counterfactual_pnl(conn, since, asset_filter, args.days)
    section_4_wr_by_stc(conn, since, asset_filter)
    section_5_wr_by_zscore(conn, since, asset_filter)
    section_6_asset_performance(conn, since, asset_filter)
    section_7_capital_utilization(conn, since, asset_filter, args.days)
    section_8_shadow_strategies(conn, since, asset_filter)
    section_9_weekend_weekday(conn, since, asset_filter)
    section_10_summary(conn, since, asset_filter, args.days)

    conn.close()
    print(f"\n{'=' * 72}")
    print("  Audit complete.")
    print(f"{'=' * 72}")


if __name__ == "__main__":
    main()
