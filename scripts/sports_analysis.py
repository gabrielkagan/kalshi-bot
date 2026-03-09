#!/usr/bin/env python3
"""Analysis script for sports comeback shadow data.

Runs locally against a copy of state.db from VPS.
Evaluates shadow signal quality, CLV, and readiness for live trading.

Usage:
    scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
    python scripts/sports_analysis.py [--db /tmp/state.db]
"""

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple


def connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def overview(conn: sqlite3.Connection) -> None:
    """Print overall shadow data summary."""
    print("=" * 60)
    print("SPORTS COMEBACK SHADOW — OVERVIEW")
    print("=" * 60)

    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN signal_fired=1 THEN 1 ELSE 0 END) AS signals, "
        "MIN(evaluation_time) AS first, MAX(evaluation_time) AS last "
        "FROM sports_shadow_log"
    ).fetchone()

    total = row["total"] or 0
    signals = row["signals"] or 0
    print(f"Total evaluations: {total}")
    print(f"Total signals:     {signals}")
    print(f"Signal rate:       {signals/total*100:.1f}%" if total > 0 else "Signal rate: —")
    print(f"First entry:       {row['first'] or '—'}")
    print(f"Last entry:        {row['last'] or '—'}")
    print()


def per_league_breakdown(conn: sqlite3.Connection) -> None:
    """Per-league signal and performance breakdown."""
    print("=" * 60)
    print("PER-LEAGUE BREAKDOWN")
    print("=" * 60)

    rows = conn.execute(
        "SELECT league, outcome_type, "
        "COUNT(*) AS evals, "
        "SUM(CASE WHEN signal_fired=1 THEN 1 ELSE 0 END) AS sigs, "
        "SUM(CASE WHEN signal_fired=1 AND fav_won=1 THEN 1 ELSE 0 END) AS wins, "
        "SUM(CASE WHEN signal_fired=1 AND fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled, "
        "AVG(CASE WHEN signal_fired=1 THEN fee_adjusted_edge END) AS avg_edge, "
        "SUM(CASE WHEN signal_fired=1 THEN COALESCE(pnl_cents,0) END) AS sim_pnl "
        "FROM sports_shadow_log GROUP BY league ORDER BY sigs DESC"
    ).fetchall()

    if not rows:
        print("No data yet.\n")
        return

    print(f"{'League':<20} {'Type':<10} {'Evals':>6} {'Sigs':>5} {'Settled':>7} "
          f"{'WR':>6} {'Avg Edge':>9} {'Sim PnL':>8}")
    print("-" * 80)
    for r in rows:
        settled = r["settled"] or 0
        wins = r["wins"] or 0
        wr = f"{wins/settled*100:.1f}%" if settled > 0 else "—"
        avg_e = f"{r['avg_edge']*100:.2f}%" if r["avg_edge"] else "—"
        pnl = f"${(r['sim_pnl'] or 0)/100:.2f}"
        print(f"{r['league']:<20} {r['outcome_type']:<10} {r['evals']:>6} "
              f"{r['sigs']:>5} {settled:>7} {wr:>6} {avg_e:>9} {pnl:>8}")
    print()


def signal_quality(conn: sqlite3.Connection) -> None:
    """Analyze signal quality — edge at signal time vs outcome."""
    print("=" * 60)
    print("SIGNAL QUALITY ANALYSIS")
    print("=" * 60)

    rows = conn.execute(
        "SELECT fee_adjusted_edge, comeback_prob, prior, "
        "likelihood_ratio, deficit, time_remaining_pct, "
        "fav_won, yes_ask, closing_price "
        "FROM sports_shadow_log "
        "WHERE signal_fired=1 AND fav_won IS NOT NULL"
    ).fetchall()

    if not rows:
        print("No settled signals yet.\n")
        return

    total = len(rows)
    wins = sum(1 for r in rows if r["fav_won"] == 1)
    losses = total - wins
    wr = wins / total if total > 0 else 0

    print(f"Settled signals: {total} ({wins}W / {losses}L)")
    print(f"Win rate:        {wr*100:.1f}%")

    # Edge buckets
    edge_buckets = defaultdict(lambda: {"total": 0, "wins": 0})
    for r in rows:
        e = r["fee_adjusted_edge"] or 0
        if e < 0.02:
            bucket = "0-2%"
        elif e < 0.05:
            bucket = "2-5%"
        elif e < 0.10:
            bucket = "5-10%"
        else:
            bucket = "10%+"
        edge_buckets[bucket]["total"] += 1
        if r["fav_won"] == 1:
            edge_buckets[bucket]["wins"] += 1

    print(f"\n{'Edge Bucket':<12} {'Total':>6} {'Wins':>5} {'WR':>6}")
    print("-" * 35)
    for bucket in ["0-2%", "2-5%", "5-10%", "10%+"]:
        if bucket in edge_buckets:
            b = edge_buckets[bucket]
            bwr = f"{b['wins']/b['total']*100:.1f}%" if b["total"] > 0 else "—"
            print(f"{bucket:<12} {b['total']:>6} {b['wins']:>5} {bwr:>6}")
    print()


def clv_analysis(conn: sqlite3.Connection) -> None:
    """Closing line value analysis — compare signal price to closing price."""
    print("=" * 60)
    print("CLOSING LINE VALUE (CLV) ANALYSIS")
    print("=" * 60)

    rows = conn.execute(
        "SELECT yes_ask, closing_price, fav_won, fee_adjusted_edge "
        "FROM sports_shadow_log "
        "WHERE signal_fired=1 AND closing_price IS NOT NULL"
    ).fetchall()

    if not rows:
        print("No CLV data yet (need closing prices).\n")
        return

    clv_positive = 0
    clv_total = 0
    clv_sum = 0.0
    for r in rows:
        entry = r["yes_ask"]
        closing = r["closing_price"]
        if entry and closing:
            clv = closing - entry  # Positive = we got a better price
            clv_sum += clv
            clv_total += 1
            if clv > 0:
                clv_positive += 1

    if clv_total > 0:
        print(f"Signals with CLV data: {clv_total}")
        print(f"CLV positive:          {clv_positive}/{clv_total} ({clv_positive/clv_total*100:.1f}%)")
        print(f"Average CLV:           {clv_sum/clv_total:.1f}c")
        print(f"{'→ Consistently positive CLV = real edge' if clv_sum > 0 else '→ Negative CLV = possible market efficiency'}")
    print()


def sequential_test(conn: sqlite3.Connection) -> None:
    """Wald sequential probability ratio test for edge significance."""
    print("=" * 60)
    print("SEQUENTIAL EDGE TEST (Wald SPRT)")
    print("=" * 60)

    rows = conn.execute(
        "SELECT fav_won FROM sports_shadow_log "
        "WHERE signal_fired=1 AND fav_won IS NOT NULL "
        "ORDER BY evaluation_time"
    ).fetchall()

    if not rows:
        print("No settled signals for sequential test.\n")
        return

    # H0: p = 0.50 (no edge), H1: p = 0.55 (5% edge)
    p0 = 0.50
    p1 = 0.55
    alpha = 0.05  # Type I error
    beta = 0.10   # Type II error

    A = math.log((1 - beta) / alpha)
    B = math.log(beta / (1 - alpha))

    llr = 0.0
    n = 0
    decision = "CONTINUE COLLECTING"

    for r in rows:
        n += 1
        outcome = r["fav_won"]
        if outcome == 1:
            llr += math.log(p1 / p0)
        else:
            llr += math.log((1 - p1) / (1 - p0))

        if llr >= A:
            decision = "REJECT H0 — edge is real (p > 0.50)"
            break
        elif llr <= B:
            decision = "ACCEPT H0 — no significant edge"
            break

    wins = sum(1 for r in rows if r["fav_won"] == 1)
    print(f"Signals tested:  {n}")
    print(f"Wins:            {wins} ({wins/n*100:.1f}%)")
    print(f"Log LR:          {llr:.3f} (reject H0 at {A:.3f}, accept H0 at {B:.3f})")
    print(f"Decision:        {decision}")
    print(f"\nThresholds: H0 (p=50%), H1 (p=55%), alpha=5%, beta=10%")
    print()


def readiness_assessment(conn: sqlite3.Connection) -> None:
    """Overall readiness assessment for live trading."""
    print("=" * 60)
    print("READINESS ASSESSMENT")
    print("=" * 60)

    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN fav_won=1 THEN 1 ELSE 0 END) AS wins, "
        "SUM(CASE WHEN fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled "
        "FROM sports_shadow_log WHERE signal_fired=1"
    ).fetchone()

    total_signals = row["total"] or 0
    settled = row["settled"] or 0
    wins = row["wins"] or 0

    checks = []

    # Check 1: Minimum 300 signals
    check1 = total_signals >= 300
    checks.append(("300+ signals collected", check1, f"{total_signals}/300"))

    # Check 2: Win rate > 55%
    wr = wins / settled if settled > 0 else 0
    check2 = wr > 0.55 and settled >= 50
    checks.append(("Win rate > 55% (50+ settled)", check2,
                    f"{wr*100:.1f}% ({settled} settled)"))

    # Check 3: Positive CLV
    clv_row = conn.execute(
        "SELECT AVG(closing_price - yes_ask) AS avg_clv "
        "FROM sports_shadow_log "
        "WHERE signal_fired=1 AND closing_price IS NOT NULL AND yes_ask IS NOT NULL"
    ).fetchone()
    avg_clv = clv_row["avg_clv"] if clv_row and clv_row["avg_clv"] else 0
    check3 = avg_clv > 0
    checks.append(("Positive CLV", check3, f"{avg_clv:.1f}c avg"))

    # Check 4: Multiple leagues contributing
    league_row = conn.execute(
        "SELECT COUNT(DISTINCT league) AS cnt FROM sports_shadow_log WHERE signal_fired=1"
    ).fetchone()
    n_leagues = league_row["cnt"] if league_row else 0
    check4 = n_leagues >= 3
    checks.append(("3+ leagues with signals", check4, f"{n_leagues} leagues"))

    print()
    all_pass = True
    for desc, passed, detail in checks:
        status = "PASS" if passed else "FAIL"
        all_pass = all_pass and passed
        print(f"  [{status}] {desc}: {detail}")

    print()
    if all_pass:
        print("  >>> ALL CHECKS PASSED — Ready for live promotion consideration")
    else:
        print("  >>> NOT READY — Continue collecting shadow data")
    print()


def main():
    parser = argparse.ArgumentParser(description="Analyze sports comeback shadow data")
    parser.add_argument("--db", default="state.db", help="Path to state.db")
    args = parser.parse_args()

    try:
        conn = connect_db(args.db)
    except Exception as e:
        print(f"ERROR: Cannot open DB at {args.db}: {e}")
        sys.exit(1)

    # Check if table exists
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sports_shadow_log'"
    ).fetchone()
    if not tables:
        print("ERROR: sports_shadow_log table not found. Has the sports engine run?")
        sys.exit(1)

    overview(conn)
    per_league_breakdown(conn)
    signal_quality(conn)
    clv_analysis(conn)
    sequential_test(conn)
    readiness_assessment(conn)

    conn.close()


if __name__ == "__main__":
    main()
