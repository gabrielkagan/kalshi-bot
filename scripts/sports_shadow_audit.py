#!/usr/bin/env python3
"""Comprehensive sports shadow audit script.

Runs against state.db to evaluate the sports comeback shadow engine.
Designed to be run frequently (daily or after each game night) to track
progress toward live trading readiness.

Usage:
    # Run on VPS directly:
    python3 scripts/sports_shadow_audit.py

    # Run locally against copied DB:
    scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
    python3 scripts/sports_shadow_audit.py --db /tmp/state.db

    # Filter to current regime (since a specific date):
    python3 scripts/sports_shadow_audit.py --since 2026-03-01

    # Output JSON artifact for dashboard/analyst consumption:
    python3 scripts/sports_shadow_audit.py --json sports_audit_results.json
"""

import argparse
import json
import math
import sqlite3
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


# ── Helpers ────────────────────────────────────────────────────────────────────

def connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def pct(num: int, denom: int) -> str:
    return f"{num/denom*100:.1f}%" if denom > 0 else "n/a"


def safe_div(a, b, default=0.0):
    return a / b if b and b > 0 else default


def header(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def subheader(title: str) -> None:
    print(f"\n--- {title} ---")


# ── Section 1: Overview ───────────────────────────────────────────────────────

def section_overview(conn: sqlite3.Connection, since: Optional[str] = None) -> Dict:
    where = f"WHERE evaluation_time >= '{since}'" if since else ""

    row = conn.execute(f"""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN signal_fired=1 THEN 1 ELSE 0 END) AS signals,
               SUM(CASE WHEN fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled,
               SUM(CASE WHEN signal_fired=1 AND fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled_sigs,
               SUM(CASE WHEN signal_fired=1 AND fav_won=1 THEN 1 ELSE 0 END) AS sig_wins,
               COUNT(DISTINCT game_id) AS games,
               COUNT(DISTINCT league) AS leagues,
               MIN(evaluation_time) AS first_eval,
               MAX(evaluation_time) AS last_eval,
               SUM(CASE WHEN signal_fired=1 THEN COALESCE(pnl_cents, 0) END) AS sim_pnl
        FROM sports_shadow_log {where}
    """).fetchone()

    result = {k: row[k] for k in row.keys()}
    result["since_filter"] = since

    header("1. OVERVIEW")
    print(f"  Date range:         {result['first_eval'] or 'none'} → {result['last_eval'] or 'none'}")
    if since:
        print(f"  Regime filter:      since {since}")
    print(f"  Total evaluations:  {result['total']}")
    print(f"  Signals fired:      {result['signals']} ({pct(result['signals'], result['total'])} signal rate)")
    print(f"  Settled signals:    {result['settled_sigs']}")
    print(f"  Signal wins:        {result['sig_wins']} (WR: {pct(result['sig_wins'], result['settled_sigs'])})")
    print(f"  Unique games:       {result['games']}")
    print(f"  Leagues active:     {result['leagues']}")
    print(f"  Sim PnL (settled):  ${(result['sim_pnl'] or 0)/100:.2f}")

    if result['settled_sigs'] == 0 and result['settled'] == 0:
        print("\n  *** WARNING: Zero settlements recorded. Settlement backfill is broken. ***")
        print("  *** fav_won, closing_price, pnl_cents are ALL NULL.                    ***")
        print("  *** No WR, PnL, or CLV analysis possible until this is fixed.          ***")

    return result


# ── Section 2: Per-League Breakdown ───────────────────────────────────────────

def section_per_league(conn: sqlite3.Connection, since: Optional[str] = None) -> List[Dict]:
    where = f"WHERE evaluation_time >= '{since}'" if since else ""

    rows = conn.execute(f"""
        SELECT league, sport, outcome_type,
               COUNT(*) AS evals,
               SUM(CASE WHEN signal_fired=1 THEN 1 ELSE 0 END) AS signals,
               SUM(CASE WHEN fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled,
               SUM(CASE WHEN signal_fired=1 AND fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled_sigs,
               SUM(CASE WHEN signal_fired=1 AND fav_won=1 THEN 1 ELSE 0 END) AS sig_wins,
               AVG(CASE WHEN signal_fired=1 THEN fee_adjusted_edge END) AS avg_edge,
               AVG(CASE WHEN signal_fired=1 THEN yes_ask END) AS avg_ask,
               AVG(CASE WHEN signal_fired=1 THEN spread END) AS avg_spread,
               COUNT(DISTINCT game_id) AS games
        FROM sports_shadow_log {where}
        GROUP BY league
        ORDER BY signals DESC, evals DESC
    """).fetchall()

    result = [dict(r) for r in rows]

    header("2. PER-LEAGUE BREAKDOWN")
    print(f"  {'League':<15} {'Type':<10} {'Evals':>6} {'Sigs':>5} {'Settled':>7} "
          f"{'WR':>6} {'AvgEdge':>8} {'AvgAsk':>7} {'AvgSpread':>9} {'Games':>5}")
    print("  " + "-" * 90)
    for r in result:
        wr = pct(r['sig_wins'] or 0, r['settled_sigs'] or 0)
        avg_e = f"{(r['avg_edge'] or 0)*100:.1f}%" if r['avg_edge'] else "n/a"
        avg_a = f"{r['avg_ask']:.0f}c" if r['avg_ask'] else "n/a"
        avg_s = f"{r['avg_spread']:.1f}c" if r['avg_spread'] else "n/a"
        print(f"  {r['league']:<15} {r['outcome_type']:<10} {r['evals']:>6} "
              f"{r['signals']:>5} {r['settled_sigs'] or 0:>7} {wr:>6} "
              f"{avg_e:>8} {avg_a:>7} {avg_s:>9} {r['games']:>5}")

    return result


# ── Section 3: Per-Game Drill-Down ────────────────────────────────────────────

def section_per_game(conn: sqlite3.Connection, since: Optional[str] = None) -> List[Dict]:
    where = f"WHERE evaluation_time >= '{since}'" if since else ""

    rows = conn.execute(f"""
        SELECT game_id, league, home_team, away_team,
               pregame_fav_code, pregame_fav_prob,
               MIN(home_score) AS min_home, MAX(home_score) AS max_home,
               MIN(away_score) AS min_away, MAX(away_score) AS max_away,
               COUNT(*) AS evals,
               SUM(CASE WHEN signal_fired=1 THEN 1 ELSE 0 END) AS signals,
               MIN(CASE WHEN signal_fired=1 THEN deficit END) AS min_deficit,
               MAX(CASE WHEN signal_fired=1 THEN deficit END) AS max_deficit,
               MIN(CASE WHEN signal_fired=1 THEN yes_ask END) AS min_ask,
               MAX(CASE WHEN signal_fired=1 THEN yes_ask END) AS max_ask,
               AVG(CASE WHEN signal_fired=1 THEN spread END) AS avg_spread,
               MAX(fav_won) AS fav_won,
               MIN(evaluation_time) AS first_eval,
               MAX(evaluation_time) AS last_eval
        FROM sports_shadow_log {where}
        GROUP BY game_id
        ORDER BY first_eval
    """).fetchall()

    result = [dict(r) for r in rows]

    header("3. PER-GAME DRILL-DOWN")
    for r in result:
        settled = "WIN" if r['fav_won'] == 1 else ("LOSS" if r['fav_won'] == 0 else "UNSETTLED")
        signals_info = ""
        if r['signals'] and r['signals'] > 0:
            min_def = r['min_deficit'] if r['min_deficit'] is not None else '?'
            max_def = r['max_deficit'] if r['max_deficit'] is not None else '?'
            min_ask = r['min_ask'] if r['min_ask'] is not None else '?'
            max_ask = r['max_ask'] if r['max_ask'] is not None else '?'
            avg_spread = f"{r['avg_spread']:.0f}" if r['avg_spread'] is not None else '?'
            signals_info = (f" | {r['signals']} signals, deficit={min_def}-{max_def}, "
                           f"ask={min_ask}-{max_ask}c, spread~{avg_spread}c")
        print(f"  {r['league']:6s} | {r['home_team']:22s} vs {r['away_team']:22s}")
        print(f"         fav={r['pregame_fav_code']} ({r['pregame_fav_prob']:.0%}) | "
              f"score={r['max_home']}-{r['max_away']} | {settled}{signals_info}")

    # Multi-entry warning
    multi = [r for r in result if (r['signals'] or 0) > 1]
    if multi:
        subheader("MULTI-ENTRY WARNING")
        for r in multi:
            print(f"  {r['league']:6s} {r['home_team']} vs {r['away_team']}: "
                  f"{r['signals']} signals in same game (correlated risk!)")

    return result


# ── Section 4: Filter Stage Distribution ──────────────────────────────────────

def section_filter_stages(conn: sqlite3.Connection, since: Optional[str] = None) -> List[Dict]:
    where = f"WHERE evaluation_time >= '{since}'" if since else ""

    rows = conn.execute(f"""
        SELECT filter_stage, COUNT(*) AS cnt,
               AVG(fee_adjusted_edge) AS avg_edge,
               AVG(yes_ask) AS avg_ask
        FROM sports_shadow_log {where}
        GROUP BY filter_stage
        ORDER BY cnt DESC
    """).fetchall()

    result = [dict(r) for r in rows]

    header("4. FILTER STAGE DISTRIBUTION")
    print(f"  {'Stage':<35} {'Count':>6} {'AvgEdge':>10} {'AvgAsk':>8}")
    print("  " + "-" * 65)
    for r in result:
        avg_e = f"{(r['avg_edge'] or 0)*100:.1f}%" if r['avg_edge'] else "n/a"
        avg_a = f"{r['avg_ask']:.0f}c" if r['avg_ask'] else "n/a"
        print(f"  {r['filter_stage']:<35} {r['cnt']:>6} {avg_e:>10} {avg_a:>8}")

    return result


# ── Section 5: Signal Quality Analysis ────────────────────────────────────────

def section_signal_quality(conn: sqlite3.Connection, since: Optional[str] = None) -> Dict:
    where = f"AND evaluation_time >= '{since}'" if since else ""

    rows = conn.execute(f"""
        SELECT fee_adjusted_edge, edge, comeback_prob, prior,
               likelihood_ratio, deficit, time_remaining_pct,
               fav_won, yes_ask, yes_bid, spread, closing_price,
               league, game_id, home_team, away_team
        FROM sports_shadow_log
        WHERE signal_fired=1 {where}
        ORDER BY evaluation_time
    """).fetchall()

    result = {"total_signals": len(rows), "settled": 0, "wins": 0}

    header("5. SIGNAL QUALITY ANALYSIS")

    if not rows:
        print("  No signals to analyze.")
        return result

    # Basic stats
    edges = [r['fee_adjusted_edge'] for r in rows if r['fee_adjusted_edge'] is not None]
    asks = [r['yes_ask'] for r in rows if r['yes_ask'] is not None]
    spreads = [r['spread'] for r in rows if r['spread'] is not None]
    posteriors = [r['comeback_prob'] for r in rows if r['comeback_prob'] is not None]

    print(f"  Total signals:      {len(rows)}")
    if edges:
        print(f"  Fee-adj edge:       min={min(edges)*100:.1f}% avg={sum(edges)/len(edges)*100:.1f}% max={max(edges)*100:.1f}%")
    if asks:
        print(f"  Entry price (ask):  min={min(asks)}c avg={sum(asks)/len(asks):.0f}c max={max(asks)}c")
    if spreads:
        print(f"  Spread:             min={min(spreads)}c avg={sum(spreads)/len(spreads):.1f}c max={max(spreads)}c")
    if posteriors:
        print(f"  Model posterior:    min={min(posteriors):.1%} avg={sum(posteriors)/len(posteriors):.1%} max={max(posteriors):.1%}")

    # Model overconfidence check
    subheader("MODEL vs MARKET (overconfidence check)")
    for r in rows:
        model_pct = (r['comeback_prob'] or 0) * 100
        market_pct = r['yes_ask'] or 0
        gap = model_pct - market_pct
        flag = " *** EXTREME" if gap > 40 else (" ** HIGH" if gap > 25 else "")
        print(f"  {r['league']:6s} {r['home_team']:15s} vs {r['away_team']:15s} | "
              f"model={model_pct:.0f}% market={market_pct}c gap={gap:+.0f}pp{flag}")

    avg_gap = sum((r['comeback_prob'] or 0) * 100 - (r['yes_ask'] or 0) for r in rows) / len(rows)
    print(f"\n  Average model-market gap: {avg_gap:+.1f}pp")
    if avg_gap > 30:
        print("  *** MODEL IS SEVERELY OVERCONFIDENT — LR table needs recalibration ***")

    # Settled signal analysis
    settled_rows = [r for r in rows if r['fav_won'] is not None]
    result['settled'] = len(settled_rows)
    result['wins'] = sum(1 for r in settled_rows if r['fav_won'] == 1)
    if settled_rows:
        subheader("SETTLED SIGNAL OUTCOMES")
        wr = safe_div(result['wins'], result['settled'])
        print(f"  Settled: {result['settled']} | Wins: {result['wins']} | WR: {wr:.1%}")

    # CLV analysis
    clv_rows = [r for r in rows if r['closing_price'] is not None and r['yes_ask']]
    if clv_rows:
        subheader("CLOSING LINE VALUE (CLV)")
        clvs = [r['closing_price'] - r['yes_ask'] for r in clv_rows]
        print(f"  Signals with CLV:   {len(clv_rows)}")
        print(f"  CLV positive:       {sum(1 for c in clvs if c > 0)}/{len(clvs)}")
        print(f"  Average CLV:        {sum(clvs)/len(clvs):.1f}c")
    else:
        subheader("CLOSING LINE VALUE (CLV)")
        print("  No CLV data available (closing_price not populated).")

    return result


# ── Section 6: Orderbook & Liquidity ──────────────────────────────────────────

def section_liquidity(conn: sqlite3.Connection, since: Optional[str] = None) -> Dict:
    header("6. ORDERBOOK & LIQUIDITY")

    # Spread distribution
    spread_where = f"WHERE spread IS NOT NULL AND evaluation_time >= '{since}'" if since else "WHERE spread IS NOT NULL"
    rows = conn.execute(f"""
        SELECT spread, COUNT(*) AS cnt,
               AVG(ask_depth) AS avg_ask_depth,
               AVG(bid_depth) AS avg_bid_depth,
               SUM(CASE WHEN signal_fired=1 THEN 1 ELSE 0 END) AS signals
        FROM sports_shadow_log {spread_where}
        GROUP BY spread ORDER BY spread
    """).fetchall()

    subheader("SPREAD DISTRIBUTION")
    print(f"  {'Spread':>7} {'Count':>6} {'Signals':>8} {'AvgAskDepth':>12} {'AvgBidDepth':>12}")
    print("  " + "-" * 50)
    for r in rows:
        print(f"  {r['spread']:>6}c {r['cnt']:>6} {r['signals']:>8} "
              f"{r['avg_ask_depth']:>12,.0f} {r['avg_bid_depth']:>12,.0f}")

    # Maker fill feasibility
    subheader("MAKER EXECUTION FEASIBILITY")
    sig_rows = conn.execute(f"""
        SELECT yes_ask, yes_bid, spread, ask_depth, bid_depth, league
        FROM sports_shadow_log
        WHERE signal_fired=1 {f"AND evaluation_time >= '{since}'" if since else ""}
    """).fetchall()

    tight_spread = sum(1 for r in sig_rows if r['spread'] and r['spread'] <= 3)
    total_sigs = len(sig_rows)
    print(f"  Signals with spread <= 3c: {tight_spread}/{total_sigs} ({pct(tight_spread, total_sigs)})")
    print(f"  Signals with spread <= 5c: {sum(1 for r in sig_rows if r['spread'] and r['spread'] <= 5)}/{total_sigs}")
    if total_sigs > 0:
        mid_entry_possible = sum(1 for r in sig_rows
                                 if r['yes_bid'] and r['yes_ask']
                                 and (r['yes_ask'] - r['yes_bid']) >= 2)
        print(f"  Signals where mid-price entry possible (spread>=2c): {mid_entry_possible}/{total_sigs}")

    return {"spread_distribution": [dict(r) for r in rows]}


# ── Section 7: Deficit & Time Heatmap ─────────────────────────────────────────

def section_deficit_time(conn: sqlite3.Connection, since: Optional[str] = None) -> Dict:
    where = f"WHERE evaluation_time >= '{since}'" if since else ""

    header("7. DEFICIT x TIME HEATMAP")

    rows = conn.execute(f"""
        SELECT
            CASE
                WHEN deficit <= 5 THEN 'small(1-5)'
                WHEN deficit <= 10 THEN 'medium(6-10)'
                WHEN deficit <= 15 THEN 'large(11-15)'
                ELSE 'blowout(16+)'
            END AS def_bucket,
            CASE
                WHEN time_remaining_pct >= 0.75 THEN '>75%'
                WHEN time_remaining_pct >= 0.50 THEN '50-75%'
                WHEN time_remaining_pct >= 0.25 THEN '25-50%'
                ELSE '<25%'
            END AS time_bucket,
            COUNT(*) AS evals,
            SUM(CASE WHEN signal_fired=1 THEN 1 ELSE 0 END) AS signals,
            SUM(CASE WHEN fav_won=1 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN fav_won=0 THEN 1 ELSE 0 END) AS losses,
            AVG(yes_ask) AS avg_ask
        FROM sports_shadow_log {where}
        GROUP BY def_bucket, time_bucket
        ORDER BY def_bucket, time_bucket
    """).fetchall()

    result = [dict(r) for r in rows]

    print(f"  {'Deficit':<16} {'Time':>8} {'Evals':>6} {'Sigs':>5} {'W':>4} {'L':>4} {'AvgAsk':>7}")
    print("  " + "-" * 55)
    for r in result:
        print(f"  {r['def_bucket']:<16} {r['time_bucket']:>8} {r['evals']:>6} "
              f"{r['signals']:>5} {r['wins'] or 0:>4} {r['losses'] or 0:>4} "
              f"{r['avg_ask']:.0f}c" if r['avg_ask'] else "n/a")

    return result


# ── Section 8: Favorite ID Audit ──────────────────────────────────────────────

def section_fav_id_audit(conn: sqlite3.Connection, since: Optional[str] = None) -> Dict:
    where = f"WHERE evaluation_time >= '{since}'" if since else ""

    header("8. FAVORITE IDENTIFICATION AUDIT")

    rows = conn.execute(f"""
        SELECT DISTINCT game_id, league, home_team, away_team,
               home_code, away_code, pregame_fav_code, pregame_fav_prob,
               pregame_price_home, pregame_price_away,
               MAX(home_score) AS last_home, MAX(away_score) AS last_away
        FROM sports_shadow_log {where}
        GROUP BY game_id
    """).fetchall()

    issues = []
    for r in rows:
        fav_code = r['pregame_fav_code']
        fav_prob = r['pregame_fav_prob']
        home_price = r['pregame_price_home']
        away_price = r['pregame_price_away']

        # Check: if fav_code is home, pregame_price_home should be high
        is_home_fav = fav_code == r['home_code']
        if is_home_fav and home_price and away_price:
            if away_price > home_price:
                issues.append({
                    "game_id": r['game_id'],
                    "league": r['league'],
                    "home": r['home_team'],
                    "away": r['away_team'],
                    "assigned_fav": fav_code,
                    "fav_prob": fav_prob,
                    "home_price": home_price,
                    "away_price": away_price,
                    "issue": f"Assigned {fav_code} (home) as fav at {fav_prob:.0%}, "
                             f"but away_price ({away_price:.0f}c) > home_price ({home_price:.0f}c)"
                })

        if not is_home_fav and home_price and away_price:
            if home_price > away_price:
                issues.append({
                    "game_id": r['game_id'],
                    "league": r['league'],
                    "home": r['home_team'],
                    "away": r['away_team'],
                    "assigned_fav": fav_code,
                    "fav_prob": fav_prob,
                    "home_price": home_price,
                    "away_price": away_price,
                    "issue": f"Assigned {fav_code} (away) as fav at {fav_prob:.0%}, "
                             f"but home_price ({home_price:.0f}c) > away_price ({away_price:.0f}c)"
                })

        print(f"  {r['league']:6s} | {r['home_team']:22s} vs {r['away_team']:22s}")
        print(f"         fav={fav_code} ({fav_prob:.0%}) | "
              f"home_price={home_price or '?'}c away_price={away_price or '?'}c")

    if issues:
        subheader("POTENTIAL MISIDENTIFICATIONS")
        for i in issues:
            print(f"  *** {i['issue']}")
    else:
        print("\n  No obvious favorite misidentifications detected from price data.")
        print("  (Note: price-based check may miss cases where both prices are similar)")

    return {"games": [dict(r) for r in rows], "issues": issues}


# ── Section 9: Settlement Gap Check ───────────────────────────────────────────

def section_settlement_gaps(conn: sqlite3.Connection, since: Optional[str] = None) -> Dict:
    where = f"AND evaluation_time >= '{since}'" if since else ""

    header("9. SETTLEMENT GAP CHECK")

    total = conn.execute(f"""
        SELECT COUNT(DISTINCT game_id) FROM sports_shadow_log WHERE 1=1 {where}
    """).fetchone()[0]

    settled = conn.execute(f"""
        SELECT COUNT(DISTINCT game_id) FROM sports_shadow_log
        WHERE fav_won IS NOT NULL {where}
    """).fetchone()[0]

    with_closing = conn.execute(f"""
        SELECT COUNT(DISTINCT game_id) FROM sports_shadow_log
        WHERE closing_price IS NOT NULL {where}
    """).fetchone()[0]

    print(f"  Total unique games:           {total}")
    print(f"  Games with fav_won populated: {settled} ({pct(settled, total)})")
    print(f"  Games with closing_price:     {with_closing} ({pct(with_closing, total)})")

    if settled < total:
        gap = total - settled
        print(f"\n  *** {gap} games missing settlement data ***")
        print("  Settlement backfill mechanism is needed in sports_engine.py:")
        print("  1. When ESPN reports game_status='final', UPDATE sports_shadow_log")
        print("     with final_home_score, final_away_score, fav_won")
        print("  2. Poll Kalshi for market settlement result")
        print("  3. Capture closing_price from last orderbook snapshot")

    return {"total_games": total, "settled_games": settled, "gap": total - settled}


# ── Section 10: Wald Sequential Test ──────────────────────────────────────────

def section_wald_sprt(conn: sqlite3.Connection, since: Optional[str] = None) -> Dict:
    where = f"AND evaluation_time >= '{since}'" if since else ""

    header("10. WALD SEQUENTIAL PROBABILITY RATIO TEST")

    rows = conn.execute(f"""
        SELECT fav_won FROM sports_shadow_log
        WHERE signal_fired=1 AND fav_won IS NOT NULL {where}
        ORDER BY evaluation_time
    """).fetchall()

    if not rows:
        print("  No settled signals for sequential test.")
        print("  Need settlement backfill before this analysis is possible.")
        return {"n": 0, "decision": "INSUFFICIENT_DATA"}

    p0 = 0.50  # H0: no edge
    p1 = 0.55  # H1: 5% edge
    alpha = 0.05
    beta = 0.10

    A = math.log((1 - beta) / alpha)
    B = math.log(beta / (1 - alpha))

    llr = 0.0
    n = 0
    decision = "CONTINUE_COLLECTING"

    for r in rows:
        n += 1
        if r["fav_won"] == 1:
            llr += math.log(p1 / p0)
        else:
            llr += math.log((1 - p1) / (1 - p0))

        if llr >= A:
            decision = "REJECT_H0_EDGE_EXISTS"
            break
        elif llr <= B:
            decision = "ACCEPT_H0_NO_EDGE"
            break

    wins = sum(1 for r in rows if r["fav_won"] == 1)
    print(f"  Signals tested: {n}")
    print(f"  Wins:           {wins} ({pct(wins, n)})")
    print(f"  Log LR:         {llr:.3f}")
    print(f"  Boundaries:     reject H0 at {A:.3f}, accept H0 at {B:.3f}")
    print(f"  Decision:       {decision}")

    return {"n": n, "wins": wins, "llr": llr, "decision": decision}


# ── Section 11: Readiness Scorecard ───────────────────────────────────────────

def section_readiness(conn: sqlite3.Connection, since: Optional[str] = None) -> Dict:
    where = f"AND evaluation_time >= '{since}'" if since else ""

    header("11. READINESS SCORECARD")

    checks = []

    # 1. Settlement backfill working
    settled = conn.execute(f"""
        SELECT COUNT(*) FROM sports_shadow_log
        WHERE fav_won IS NOT NULL {where}
    """).fetchone()[0]
    total = conn.execute(f"""
        SELECT COUNT(DISTINCT game_id) FROM sports_shadow_log WHERE 1=1 {where}
    """).fetchone()[0]
    c1 = settled > 0
    checks.append(("Settlement backfill working", c1, f"{settled}/{total} games settled"))

    # 2. 100+ settled signals
    settled_sigs = conn.execute(f"""
        SELECT COUNT(*) FROM sports_shadow_log
        WHERE signal_fired=1 AND fav_won IS NOT NULL {where}
    """).fetchone()[0]
    c2 = settled_sigs >= 100
    checks.append(("100+ settled signals", c2, f"{settled_sigs}/100"))

    # 3. Win rate > 55%
    sig_wins = conn.execute(f"""
        SELECT COUNT(*) FROM sports_shadow_log
        WHERE signal_fired=1 AND fav_won=1 {where}
    """).fetchone()[0]
    wr = safe_div(sig_wins, settled_sigs)
    c3 = wr > 0.55 and settled_sigs >= 50
    checks.append(("Win rate > 55% (50+ settled)", c3, f"{wr:.1%} ({settled_sigs} settled)"))

    # 4. Positive CLV
    clv_row = conn.execute(f"""
        SELECT AVG(closing_price - yes_ask) AS avg_clv
        FROM sports_shadow_log
        WHERE signal_fired=1 AND closing_price IS NOT NULL AND yes_ask IS NOT NULL {where}
    """).fetchone()
    avg_clv = clv_row["avg_clv"] if clv_row and clv_row["avg_clv"] else None
    c4 = avg_clv is not None and avg_clv > 0
    checks.append(("Positive CLV", c4, f"{avg_clv:.1f}c" if avg_clv else "no data"))

    # 5. Multiple leagues
    n_leagues = conn.execute(f"""
        SELECT COUNT(DISTINCT league) FROM sports_shadow_log
        WHERE signal_fired=1 {where}
    """).fetchone()[0]
    c5 = n_leagues >= 3
    checks.append(("3+ leagues with signals", c5, f"{n_leagues} leagues"))

    # 6. Model calibration reasonable
    avg_gap = conn.execute(f"""
        SELECT AVG(comeback_prob * 100 - yes_ask) AS gap
        FROM sports_shadow_log
        WHERE signal_fired=1 AND yes_ask > 0 {where}
    """).fetchone()
    gap_val = avg_gap["gap"] if avg_gap and avg_gap["gap"] else None
    c6 = gap_val is not None and abs(gap_val) < 20
    checks.append(("Model-market gap < 20pp", c6,
                    f"{gap_val:.1f}pp avg" if gap_val else "no data"))

    # 7. Per-game dedup implemented
    multi = conn.execute(f"""
        SELECT COUNT(*) FROM (
            SELECT game_id, COUNT(*) as n
            FROM sports_shadow_log
            WHERE signal_fired=1 {where}
            GROUP BY game_id HAVING n > 1
        )
    """).fetchone()[0]
    c7 = multi == 0
    checks.append(("No multi-entry per game", c7,
                    f"{multi} games with multiple signals"))

    print()
    all_pass = True
    result_checks = []
    for desc, passed, detail in checks:
        status = "PASS" if passed else "FAIL"
        symbol = "+" if passed else "X"
        all_pass = all_pass and passed
        print(f"  [{symbol}] {desc}: {detail}")
        result_checks.append({"check": desc, "passed": passed, "detail": detail})

    print()
    if all_pass:
        print("  >>> ALL CHECKS PASSED — Ready for live promotion consideration")
    else:
        failing = sum(1 for _, p, _ in checks if not p)
        print(f"  >>> NOT READY — {failing} checks failing. Continue shadow collection.")

    return {"all_pass": all_pass, "checks": result_checks}


# ── Section 12: Config Recommendations ────────────────────────────────────────

def section_recommendations(overview_data: Dict, signal_data: Dict,
                           settlement_data: Dict) -> None:
    header("12. RECOMMENDATIONS (prioritized)")

    recs = []

    # Always recommend settlement backfill if missing
    if settlement_data.get("gap", 0) > 0:
        recs.append((
            "CRITICAL",
            "Fix settlement backfill",
            "Add game-over detection in _tick(). When ESPN reports 'final', "
            "UPDATE sports_shadow_log with final scores, fav_won, closing_price. "
            "Without this, ALL performance analysis is impossible.",
            "IMMEDIATE"
        ))

    # Favorite ID audit
    recs.append((
        "CRITICAL",
        "Validate favorite identification",
        "Log both home/away Kalshi prices explicitly with team names. "
        "Cross-check against ESPN win probability. The home/away parsing "
        "heuristic in KalshiSportsDiscovery may be swapping teams.",
        "IMMEDIATE"
    ))

    # Model calibration
    if signal_data.get("total_signals", 0) > 0:
        recs.append((
            "HIGH",
            "Recalibrate LR table",
            "Current LR values produce posteriors 40-70pp above market prices. "
            "Use historical comeback rate data (Basketball Reference, FBref) "
            "to derive empirical LR values. Target: model-market gap < 15pp.",
            "WEEK 2"
        ))

    # Per-game dedup
    recs.append((
        "MEDIUM",
        "Add per-game signal dedup",
        "Currently fires signal on every score change. Add max 1 signal per "
        "game_id (enter on first qualifying signal only). Reduces correlated "
        "risk from multiple entries in same game.",
        "WEEK 1"
    ))

    # Max model-market disagreement filter
    recs.append((
        "MEDIUM",
        "Add model-market disagreement cap",
        "Reject signals where model posterior exceeds market price by >30pp. "
        "At >30pp disagreement, the model is almost certainly wrong, not the market. "
        "This is a safety valve until LR table is recalibrated.",
        "WEEK 1"
    ))

    for priority, title, detail, timeline in recs:
        print(f"\n  [{priority}] {title} ({timeline})")
        print(f"    {detail}")


# ── Section 13: Counterfactual Analysis ──────────────────────────────────

def section_counterfactual(conn: sqlite3.Connection, since: Optional[str] = None) -> Dict:
    where = f"WHERE evaluation_time >= '{since}'" if since else ""

    header("13. COUNTERFACTUAL ANALYSIS (multi-threshold)")

    # Check if columns exist
    try:
        conn.execute("SELECT would_signal_50c FROM sports_shadow_log LIMIT 1")
    except Exception:
        print("  Counterfactual columns not yet populated (need engine restart).")
        return {}

    row = conn.execute(f"""
        SELECT COUNT(*) AS total,
               SUM(would_signal_50c) AS at_50c,
               SUM(would_signal_60c) AS at_60c,
               SUM(would_signal_70c) AS at_70c,
               SUM(would_signal_80c) AS at_80c,
               SUM(would_signal_pregame_55) AS pregame_55,
               SUM(would_signal_pregame_65) AS pregame_65,
               SUM(signal_fired) AS live_signal
        FROM sports_shadow_log {where}
    """).fetchone()

    result = {k: row[k] for k in row.keys()}

    print(f"  Total evaluations:  {result['total']}")
    print()
    print(f"  {'Threshold':<25} {'Signals':>8} {'Rate':>8}")
    print("  " + "-" * 45)
    for label, key in [
        ("Live (current config)", "live_signal"),
        ("Price <= 50c", "at_50c"),
        ("Price <= 60c", "at_60c"),
        ("Price <= 70c", "at_70c"),
        ("Price <= 80c", "at_80c"),
        ("Pregame >= 55%", "pregame_55"),
        ("Pregame >= 65%", "pregame_65"),
    ]:
        val = result.get(key) or 0
        print(f"  {label:<25} {val:>8} {pct(val, result['total']):>8}")

    # Per-threshold settled outcomes if available
    subheader("SETTLED COUNTERFACTUAL OUTCOMES")
    settled_row = conn.execute(f"""
        SELECT
            SUM(CASE WHEN would_signal_50c=1 AND fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled_50,
            SUM(CASE WHEN would_signal_50c=1 AND fav_won=1 THEN 1 ELSE 0 END) AS wins_50,
            SUM(CASE WHEN would_signal_60c=1 AND fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled_60,
            SUM(CASE WHEN would_signal_60c=1 AND fav_won=1 THEN 1 ELSE 0 END) AS wins_60,
            SUM(CASE WHEN would_signal_70c=1 AND fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled_70,
            SUM(CASE WHEN would_signal_70c=1 AND fav_won=1 THEN 1 ELSE 0 END) AS wins_70,
            SUM(CASE WHEN would_signal_80c=1 AND fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled_80,
            SUM(CASE WHEN would_signal_80c=1 AND fav_won=1 THEN 1 ELSE 0 END) AS wins_80
        FROM sports_shadow_log {where}
    """).fetchone()

    for label, s_key, w_key in [
        ("Price <= 50c", "settled_50", "wins_50"),
        ("Price <= 60c", "settled_60", "wins_60"),
        ("Price <= 70c", "settled_70", "wins_70"),
        ("Price <= 80c", "settled_80", "wins_80"),
    ]:
        s = settled_row[s_key] or 0
        w = settled_row[w_key] or 0
        print(f"  {label:<25} {w}W/{s-w}L  WR={pct(w, s)}" if s > 0
              else f"  {label:<25} no settled data")

    return result


# ── Section 14: Pregame Capture Rate ─────────────────────────────────────

def section_pregame_capture(conn: sqlite3.Connection, since: Optional[str] = None) -> Dict:
    where = f"WHERE evaluation_time >= '{since}'" if since else ""

    header("14. PREGAME CAPTURE RATE")

    # Check if column exists
    try:
        conn.execute("SELECT pregame_capture_method FROM sports_shadow_log LIMIT 1")
    except Exception:
        print("  pregame_capture_method column not yet populated (need engine restart).")
        return {}

    rows = conn.execute(f"""
        SELECT
            COALESCE(pregame_capture_method, 'unknown') AS method,
            COUNT(*) AS cnt,
            SUM(CASE WHEN pregame_price_home IS NOT NULL THEN 1 ELSE 0 END) AS has_pregame_price,
            SUM(CASE WHEN signal_fired=1 THEN 1 ELSE 0 END) AS signals
        FROM sports_shadow_log {where}
        GROUP BY method
        ORDER BY cnt DESC
    """).fetchall()

    result = [dict(r) for r in rows]

    total = sum(r['cnt'] for r in result)
    print(f"  Total evaluations: {total}")
    print()
    print(f"  {'Method':<20} {'Count':>6} {'%':>7} {'HasPrice':>9} {'Signals':>8}")
    print("  " + "-" * 55)
    for r in result:
        print(f"  {r['method']:<20} {r['cnt']:>6} {pct(r['cnt'], total):>7} "
              f"{r['has_pregame_price']:>9} {r['signals']:>8}")

    # Overall pregame price availability
    with_price = conn.execute(f"""
        SELECT COUNT(*) FROM sports_shadow_log
        {where + ' AND' if where else 'WHERE'} pregame_price_home IS NOT NULL
    """).fetchone()[0]
    print(f"\n  Evaluations with pregame_price_home: {with_price}/{total} ({pct(with_price, total)})")

    return {"methods": result, "total": total, "with_pregame_price": with_price}


# ── Section 15: LR Scale A/B ────────────────────────────────────────────

def section_lr_scale_ab(conn: sqlite3.Connection, since: Optional[str] = None) -> Dict:
    where = f"WHERE evaluation_time >= '{since}'" if since else ""

    header("15. LR SCALE A/B (live=0.2 vs shadow=0.5)")

    # Check if column exists
    try:
        conn.execute("SELECT shadow_lr_scale_50_signal FROM sports_shadow_log LIMIT 1")
    except Exception:
        print("  Shadow LR scale columns not yet populated (need engine restart).")
        return {}

    row = conn.execute(f"""
        SELECT
            SUM(signal_fired) AS live_signals,
            SUM(shadow_lr_scale_50_signal) AS shadow_signals,
            SUM(CASE WHEN signal_fired=1 AND fav_won IS NOT NULL THEN 1 ELSE 0 END) AS live_settled,
            SUM(CASE WHEN signal_fired=1 AND fav_won=1 THEN 1 ELSE 0 END) AS live_wins,
            SUM(CASE WHEN shadow_lr_scale_50_signal=1 AND fav_won IS NOT NULL THEN 1 ELSE 0 END) AS shadow_settled,
            SUM(CASE WHEN shadow_lr_scale_50_signal=1 AND fav_won=1 THEN 1 ELSE 0 END) AS shadow_wins,
            AVG(CASE WHEN signal_fired=1 THEN comeback_prob END) AS live_avg_posterior,
            AVG(CASE WHEN shadow_lr_scale_50_signal=1 THEN shadow_lr_scale_50_posterior END) AS shadow_avg_posterior
        FROM sports_shadow_log {where}
    """).fetchone()

    result = {k: row[k] for k in row.keys()}

    live_s = result['live_signals'] or 0
    shadow_s = result['shadow_signals'] or 0
    live_settled = result['live_settled'] or 0
    live_wins = result['live_wins'] or 0
    shadow_settled = result['shadow_settled'] or 0
    shadow_wins = result['shadow_wins'] or 0

    print(f"  {'Metric':<30} {'Live (scale=0.2)':>18} {'Shadow (scale=0.5)':>20}")
    print("  " + "-" * 70)
    print(f"  {'Signals fired':<30} {live_s:>18} {shadow_s:>20}")
    print(f"  {'Settled':<30} {live_settled:>18} {shadow_settled:>20}")
    print(f"  {'Wins':<30} {live_wins:>18} {shadow_wins:>20}")
    print(f"  {'Win rate':<30} {pct(live_wins, live_settled):>18} {pct(shadow_wins, shadow_settled):>20}")
    live_post = result['live_avg_posterior']
    shadow_post = result['shadow_avg_posterior']
    print(f"  {'Avg posterior':<30} {f'{live_post:.1%}' if live_post else 'n/a':>18} "
          f"{f'{shadow_post:.1%}' if shadow_post else 'n/a':>20}")

    if live_s > shadow_s:
        print(f"\n  Scale=0.2 generates MORE signals ({live_s} vs {shadow_s}) — "
              "lower LR produces lower posterior → lower edge → fewer signals??")
        print("  (Check: scale=0.2 compresses LR toward 1.0 MORE, reducing model confidence)")
    elif shadow_s > live_s:
        print(f"\n  Scale=0.5 would generate MORE signals ({shadow_s} vs {live_s})")
    else:
        print(f"\n  Both scales produce same number of signals ({live_s})")

    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Comprehensive sports shadow audit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("--db", default="state.db", help="Path to state.db")
    parser.add_argument("--since", default=None,
                        help="Filter to data since this date (YYYY-MM-DD)")
    parser.add_argument("--json", default=None,
                        help="Write JSON artifact to this path")
    args = parser.parse_args()

    try:
        conn = connect_db(args.db)
    except Exception as e:
        print(f"ERROR: Cannot open DB at {args.db}: {e}")
        sys.exit(1)

    # Check table exists
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sports_shadow_log'"
    ).fetchone()
    if not table:
        print("ERROR: sports_shadow_log table not found. Has the sports engine run?")
        sys.exit(1)

    print()
    print("=" * 72)
    print("  KALSHI SPORTS SHADOW AUDIT")
    print(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    if args.since:
        print(f"  Regime filter: since {args.since}")
    print("=" * 72)

    # Run all sections
    overview_data = section_overview(conn, args.since)
    league_data = section_per_league(conn, args.since)
    game_data = section_per_game(conn, args.since)
    filter_data = section_filter_stages(conn, args.since)
    signal_data = section_signal_quality(conn, args.since)
    liquidity_data = section_liquidity(conn, args.since)
    deficit_data = section_deficit_time(conn, args.since)
    fav_audit = section_fav_id_audit(conn, args.since)
    settlement_data = section_settlement_gaps(conn, args.since)
    sprt_data = section_wald_sprt(conn, args.since)
    readiness_data = section_readiness(conn, args.since)
    section_recommendations(overview_data, signal_data, settlement_data)
    counterfactual_data = section_counterfactual(conn, args.since)
    pregame_data = section_pregame_capture(conn, args.since)
    lr_ab_data = section_lr_scale_ab(conn, args.since)

    # JSON output
    if args.json:
        artifact = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "since": args.since,
            "overview": overview_data,
            "leagues": league_data,
            "games": game_data,
            "filter_stages": filter_data,
            "signal_quality": signal_data,
            "settlement": settlement_data,
            "sprt": sprt_data,
            "readiness": readiness_data,
            "fav_audit_issues": fav_audit.get("issues", []),
            "counterfactual": counterfactual_data,
            "pregame_capture": pregame_data,
            "lr_scale_ab": lr_ab_data,
        }
        with open(args.json, "w") as f:
            json.dump(artifact, f, indent=2, default=str)
        print(f"\n  JSON artifact written to: {args.json}")

    print()
    conn.close()


if __name__ == "__main__":
    main()
