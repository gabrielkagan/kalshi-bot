#!/usr/bin/env python3
"""Comprehensive sports shadow audit script.

Runs against state.db to evaluate the sports comeback shadow engine.
Designed to be run frequently (daily or after each game night) to track
progress toward live trading readiness.

Output is organized per-sport so each league can be evaluated independently.

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
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


# ── Helpers ────────────────────────────────────────────────────────────────────

def connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def _where(since: Optional[str] = None, league: Optional[str] = None,
           prefix: str = "WHERE", sport_group: Optional[str] = None) -> str:
    """Composable WHERE/AND clause builder."""
    parts = []
    if since:
        parts.append(f"evaluation_time >= '{since}'")
    if league:
        parts.append(f"league = '{league}'")
    if sport_group:
        parts.append(f"sport_group = '{sport_group}'")
    if not parts:
        return ""
    return f" {prefix} " + " AND ".join(parts)


def pct(num: int, denom: int) -> str:
    return f"{num/denom*100:.1f}%" if denom > 0 else "n/a"


def safe_div(a, b, default=0.0):
    return a / b if b and b > 0 else default


def maker_fee(price_cents: float, contracts: int = 1) -> float:
    """Kalshi charges $0 on maker fills."""
    return 0


def taker_fee(price_cents: float, contracts: int = 1) -> float:
    """Taker fee in cents."""
    p = price_cents / 100.0
    return math.ceil(0.07 * contracts * p * (1 - p))


def breakeven_wr(price_cents: float, fee_type: str = "taker") -> float:
    """Breakeven win rate at a given price accounting for fees."""
    if not price_cents or price_cents <= 0 or price_cents >= 100:
        return 0.0
    fee_fn = maker_fee if fee_type == "maker" else taker_fee
    fee = fee_fn(price_cents)
    cost = price_cents + fee
    win_payout = 100 - price_cents - fee
    if win_payout + cost == 0:
        return 0.0
    return cost / (win_payout + cost)


def sized_pnl(price_cents: float, won: bool, contracts: int = 1,
              fee_type: str = "taker") -> float:
    """Compute PnL in cents for a sized trade."""
    fee_fn = maker_fee if fee_type == "maker" else taker_fee
    fee = fee_fn(price_cents, contracts)
    if won:
        return (100 - price_cents) * contracts - fee
    else:
        return -(price_cents * contracts + fee)


def significance_tag(n: int, p: float = 0.5, threshold: float = 0.55) -> str:
    """Return significance tag based on sample size and effect.
    Uses normal approximation for binomial test."""
    if n < 5:
        return "[INSUFFICIENT]"
    if n < 15:
        return "[NOT SIG, n<15]"
    se = math.sqrt(p * (1 - p) / n)
    if se == 0:
        return "[NOT SIG]"
    z = (threshold - p) / se  # z for observed rate vs null
    # Quick lookup: z=1.28 → p=0.10, z=1.645 → p=0.05
    if abs(z) >= 1.645:
        return "[SIG p<.05]"
    elif abs(z) >= 1.28:
        return "[MARGINAL p<.10]"
    return "[NOT SIG]"


def _dedup_game_wr(conn, since=None, league=None, sport_group=None,
                   extra_cond=""):
    """De-dup to one outcome per game_id for signal rows.
    Returns (n_games_with_signals, settled_games, game_wins).
    """
    parts = ["signal_fired=1"]
    if since:
        parts.append(f"evaluation_time >= '{since}'")
    if league:
        parts.append(f"league = '{league}'")
    if sport_group:
        parts.append(f"sport_group = '{sport_group}'")
    if extra_cond:
        parts.append(extra_cond)
    where = "WHERE " + " AND ".join(parts)

    row = conn.execute(f"""
        SELECT COUNT(*) AS games,
               SUM(CASE WHEN fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled,
               SUM(CASE WHEN fav_won = 1 THEN 1 ELSE 0 END) AS wins
        FROM (
            SELECT game_id,
                   (SELECT s2.fav_won FROM sports_shadow_log s2
                    WHERE s2.game_id = s.game_id AND s2.signal_fired=1
                    ORDER BY s2.evaluation_time ASC LIMIT 1) AS fav_won
            FROM sports_shadow_log s {where}
            GROUP BY game_id
        )
    """).fetchone()
    return row['games'] or 0, row['settled'] or 0, row['wins'] or 0


def _sql_taker_fee(price_col: str = "yes_ask") -> str:
    """SQL expression for taker fee in cents: ceil(0.07 * P * (100-P) / 10000).
    Uses CAST(x + 0.9999 AS INTEGER) to simulate ceil for positive values."""
    return (f"CAST(0.07 * {price_col} * (100 - {price_col}) / 10000.0 + 0.9999 AS INTEGER)")


def header(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def big_header(title: str) -> None:
    print()
    print()
    print("#" * 72)
    print(f"##  {title}")
    print("#" * 72)


def subheader(title: str) -> None:
    print(f"\n--- {title} ---")


def get_leagues_with_signals(conn: sqlite3.Connection,
                             since: Optional[str] = None) -> List[str]:
    """Return league names that have at least 1 signal."""
    w = _where(since, prefix="AND")
    rows = conn.execute(f"""
        SELECT DISTINCT league FROM sports_shadow_log
        WHERE signal_fired=1 {w}
        ORDER BY league
    """).fetchall()
    return [r['league'] for r in rows]


# ── Section: Overview ────────────────────────────────────────────────────────

def section_overview(conn: sqlite3.Connection, since: Optional[str] = None,
                     league: Optional[str] = None) -> Dict:
    w = _where(since, league)

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
               SUM(CASE WHEN signal_fired=1 THEN COALESCE(pnl_cents, 0) END) AS sim_pnl,
               SUM(CASE WHEN signal_fired=1 AND fav_won IS NOT NULL THEN
                   CASE WHEN fav_won=1 THEN (100 - yes_ask) - {_sql_taker_fee()} ELSE -(yes_ask + {_sql_taker_fee()}) END
               END) AS sim_pnl_1c
        FROM sports_shadow_log {w}
    """).fetchone()

    result = {k: row[k] for k in row.keys()}
    result["since_filter"] = since
    result["league_filter"] = league

    header(f"OVERVIEW{f' ({league})' if league else ''}")
    print(f"  Date range:         {result['first_eval'] or 'none'} → {result['last_eval'] or 'none'}")
    if since:
        print(f"  Regime filter:      since {since}")
    if league:
        print(f"  League filter:      {league}")
    print(f"  Total evaluations:  {result['total']}")
    print(f"  Signals fired:      {result['signals']} ({pct(result['signals'], result['total'])} signal rate)")
    print(f"  Settled signals:    {result['settled_sigs']}")
    print(f"  Signal wins (raw):  {result['sig_wins']} (raw WR: {pct(result['sig_wins'], result['settled_sigs'])} — NOT de-duped)")
    g, gs, gw = _dedup_game_wr(conn, since=since, league=league)
    result['dedup_games'] = g
    result['dedup_settled'] = gs
    result['dedup_wins'] = gw
    dedup_wr = safe_div(gw, gs)
    tag = significance_tag(gs, p=0.5, threshold=dedup_wr) if gs > 0 else ""
    print(f"  Game wins (dedup):  {gw}/{gs} games (WR: {pct(gw, gs)}) {tag}")
    print(f"  Unique games:       {result['games']}")
    if not league:
        print(f"  Leagues active:     {result['leagues']}")
    print(f"  Sim PnL (1c):       ${(result['sim_pnl_1c'] or 0)/100:.2f}")
    print(f"  Sim PnL (sized):    ${(result['sim_pnl'] or 0)/100:.2f}")

    if result['settled_sigs'] == 0 and result['settled'] == 0:
        print("\n  *** WARNING: Zero settlements recorded. Settlement backfill is broken. ***")
        print("  *** fav_won, closing_price, pnl_cents are ALL NULL.                    ***")
        print("  *** No WR, PnL, or CLV analysis possible until this is fixed.          ***")

    return result


# ── Section: Per-League Summary Table ────────────────────────────────────────

def section_per_league(conn: sqlite3.Connection,
                       since: Optional[str] = None) -> List[Dict]:
    w = _where(since)

    # Check if sport_group column exists
    has_sport_group = False
    try:
        conn.execute("SELECT sport_group FROM sports_shadow_log LIMIT 1")
        has_sport_group = True
    except Exception:
        pass

    sg_col = ", MAX(COALESCE(sport_group, 'unknown')) AS sg" if has_sport_group else ", 'unknown' AS sg"

    rows = conn.execute(f"""
        SELECT league, MAX(sport) AS sport, MAX(outcome_type) AS outcome_type {sg_col},
               COUNT(*) AS evals,
               SUM(CASE WHEN signal_fired=1 THEN 1 ELSE 0 END) AS signals,
               SUM(CASE WHEN fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled,
               SUM(CASE WHEN signal_fired=1 AND fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled_sigs,
               SUM(CASE WHEN signal_fired=1 AND fav_won=1 THEN 1 ELSE 0 END) AS sig_wins,
               AVG(CASE WHEN signal_fired=1 THEN fee_adjusted_edge END) AS avg_edge,
               AVG(CASE WHEN signal_fired=1 THEN yes_ask END) AS avg_ask,
               AVG(CASE WHEN signal_fired=1 THEN spread END) AS avg_spread,
               COUNT(DISTINCT game_id) AS games
        FROM sports_shadow_log {w}
        GROUP BY league
        ORDER BY signals DESC, evals DESC
    """).fetchall()

    result = [dict(r) for r in rows]

    subheader("PER-LEAGUE SUMMARY")
    print(f"  {'League':<15} {'Group':<12} {'Type':<10} {'Evals':>6} {'Sigs':>5} "
          f"{'RawWR':>7} {'GameWR':>8} {'AvgEdge':>8} {'Games':>5}")
    print("  " + "-" * 95)
    for r in result:
        raw_wr = pct(r['sig_wins'] or 0, r['settled_sigs'] or 0)
        g, gs, gw = _dedup_game_wr(conn, since=since, league=r['league'])
        game_wr = pct(gw, gs)
        r['dedup_settled'] = gs
        r['dedup_wins'] = gw
        avg_e = f"{(r['avg_edge'] or 0)*100:.1f}%" if r['avg_edge'] else "n/a"
        print(f"  {r['league']:<15} {r['sg']:<12} {r['outcome_type']:<10} {r['evals']:>6} "
              f"{r['signals']:>5} {raw_wr:>7} {f'{gw}/{gs}':>8} "
              f"{avg_e:>8} {r['games']:>5}")

    return result


# ── Section: Per-Game Drill-Down ─────────────────────────────────────────────

def section_per_game(conn: sqlite3.Connection, since: Optional[str] = None,
                     league: Optional[str] = None) -> List[Dict]:
    w = _where(since, league)

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
               (SELECT s2.fav_won FROM sports_shadow_log s2
                WHERE s2.game_id = s.game_id
                ORDER BY s2.evaluation_time ASC LIMIT 1) AS fav_won,
               MIN(evaluation_time) AS first_eval,
               MAX(evaluation_time) AS last_eval
        FROM sports_shadow_log s {w}
        GROUP BY game_id
        ORDER BY first_eval
    """).fetchall()

    result = [dict(r) for r in rows]

    subheader(f"PER-GAME DRILL-DOWN{f' ({league})' if league else ''}")
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


# ── Section: Filter Stage Distribution ───────────────────────────────────────

def section_filter_stages(conn: sqlite3.Connection, since: Optional[str] = None,
                          league: Optional[str] = None) -> List[Dict]:
    w = _where(since, league)

    rows = conn.execute(f"""
        SELECT filter_stage, COUNT(*) AS cnt,
               AVG(fee_adjusted_edge) AS avg_edge,
               AVG(yes_ask) AS avg_ask
        FROM sports_shadow_log {w}
        GROUP BY filter_stage
        ORDER BY cnt DESC
    """).fetchall()

    result = [dict(r) for r in rows]

    subheader(f"FILTER STAGE DISTRIBUTION{f' ({league})' if league else ''}")
    print(f"  {'Stage':<35} {'Count':>6} {'AvgEdge':>10} {'AvgAsk':>8}")
    print("  " + "-" * 65)
    for r in result:
        avg_e = f"{(r['avg_edge'] or 0)*100:.1f}%" if r['avg_edge'] else "n/a"
        avg_a = f"{r['avg_ask']:.0f}c" if r['avg_ask'] else "n/a"
        print(f"  {r['filter_stage']:<35} {r['cnt']:>6} {avg_e:>10} {avg_a:>8}")

    return result


# ── Section: Signal Quality Analysis ─────────────────────────────────────────

def section_signal_quality(conn: sqlite3.Connection, since: Optional[str] = None,
                           league: Optional[str] = None) -> Dict:
    extra = _where(since, league, prefix="AND")

    rows = conn.execute(f"""
        SELECT fee_adjusted_edge, edge, comeback_prob, prior,
               likelihood_ratio, deficit, time_remaining_pct,
               fav_won, yes_ask, yes_bid, spread, closing_price,
               league, game_id, home_team, away_team
        FROM sports_shadow_log
        WHERE signal_fired=1 {extra}
        ORDER BY evaluation_time
    """).fetchall()

    result = {"total_signals": len(rows), "settled": 0, "wins": 0}

    subheader(f"SIGNAL QUALITY{f' ({league})' if league else ''}")

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

    # Model overconfidence — aggregate to ONE LINE PER GAME
    subheader(f"MODEL vs MARKET — per game{f' ({league})' if league else ''}")

    game_agg = defaultdict(lambda: {
        "model_probs": [], "market_prices": [], "fav_won": None,
        "league": "", "home_team": "", "away_team": "", "n_signals": 0
    })
    for r in rows:
        g = game_agg[r['game_id']]
        g["league"] = r['league']
        g["home_team"] = r['home_team']
        g["away_team"] = r['away_team']
        g["n_signals"] += 1
        if r['comeback_prob'] is not None:
            g["model_probs"].append(r['comeback_prob'] * 100)
        if r['yes_ask'] is not None:
            g["market_prices"].append(r['yes_ask'])
        if r['fav_won'] is not None:
            g["fav_won"] = r['fav_won']

    zero_market_count = 0
    valid_gaps = []
    print(f"  {'League':6s} {'Home':15s} {'Away':15s} {'Sigs':>4} "
          f"{'AvgModel':>9} {'AvgMkt':>7} {'Gap':>7} {'Result':>8}")
    print("  " + "-" * 75)
    for gid, g in sorted(game_agg.items(), key=lambda x: x[1]['home_team']):
        avg_model = sum(g["model_probs"]) / len(g["model_probs"]) if g["model_probs"] else 0
        market_with_data = [p for p in g["market_prices"] if p > 0]
        if not market_with_data:
            zero_market_count += 1
            continue
        avg_market = sum(market_with_data) / len(market_with_data)
        gap = avg_model - avg_market
        valid_gaps.append(gap)
        outcome = ("WIN" if g["fav_won"] == 1
                    else ("LOSS" if g["fav_won"] == 0 else "UNSETTLED"))
        flag = " ***" if gap > 40 else (" **" if gap > 25 else "")
        print(f"  {g['league']:6s} {g['home_team']:15s} {g['away_team']:15s} "
              f"{g['n_signals']:>4} {avg_model:>8.0f}% {avg_market:>6.0f}c "
              f"{gap:>+6.0f}pp {outcome:>8}{flag}")

    if zero_market_count:
        print(f"\n  {zero_market_count} game(s) had market=0c (ticker matching failure, excluded from gap calc)")
    if valid_gaps:
        avg_gap = sum(valid_gaps) / len(valid_gaps)
        print(f"  Average model-market gap: {avg_gap:+.1f}pp ({len(valid_gaps)} games)")
        if avg_gap > 30:
            print("  *** MODEL IS SEVERELY OVERCONFIDENT — LR table needs recalibration ***")
        result["avg_model_market_gap"] = avg_gap
    result["zero_market_games"] = zero_market_count

    # Settled signal analysis
    settled_rows = [r for r in rows if r['fav_won'] is not None]
    result['settled'] = len(settled_rows)
    result['wins'] = sum(1 for r in settled_rows if r['fav_won'] == 1)
    if settled_rows:
        subheader(f"SETTLED SIGNAL OUTCOMES{f' ({league})' if league else ''}")
        wr = safe_div(result['wins'], result['settled'])
        print(f"  Raw signals: {result['settled']} settled | {result['wins']} wins | WR: {wr:.1%} (NOT de-duped)")
        g, gs, gw = _dedup_game_wr(conn, since=since, league=league)
        dedup_wr = safe_div(gw, gs)
        tag = significance_tag(gs, p=0.5, threshold=dedup_wr) if gs > 0 else ""
        print(f"  Per game:    {gs} settled | {gw} wins | WR: {dedup_wr:.1%} (DE-DUPED) {tag}")

    # CLV analysis
    clv_rows = [r for r in rows if r['closing_price'] is not None and r['yes_ask']]
    if clv_rows:
        subheader(f"CLOSING LINE VALUE (CLV){f' ({league})' if league else ''}")
        clvs = [r['closing_price'] - r['yes_ask'] for r in clv_rows]
        avg_clv = sum(clvs) / len(clvs)
        pos_clv = sum(1 for c in clvs if c > 0)
        print(f"  Signals with CLV:   {len(clv_rows)}")
        print(f"  CLV positive:       {pos_clv}/{len(clvs)}")
        print(f"  Average CLV:        {avg_clv:.1f}c")
        if avg_clv < -10:
            print(f"  *** NEGATIVE CLV: market moves {abs(avg_clv):.0f}c against entry — model overconfident ***")

    return result


# ── Section: Price Bucket Performance ────────────────────────────────────────

def section_price_buckets(conn: sqlite3.Connection, since: Optional[str] = None,
                          league: Optional[str] = None) -> List[Dict]:
    """Win rate and PnL by entry price tier with breakeven comparison."""
    extra = _where(since, league, prefix="AND")

    subheader(f"PRICE BUCKET PERFORMANCE{f' ({league})' if league else ''}")

    rows = conn.execute(f"""
        SELECT
            CASE
                WHEN yes_ask < 20 THEN '1-19c'
                WHEN yes_ask < 35 THEN '20-34c'
                WHEN yes_ask < 50 THEN '35-49c'
                WHEN yes_ask < 65 THEN '50-64c'
                WHEN yes_ask < 80 THEN '65-79c'
                ELSE '80-99c'
            END AS bucket,
            MIN(yes_ask) AS min_ask,
            MAX(yes_ask) AS max_ask,
            COUNT(*) AS signals,
            SUM(CASE WHEN fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled,
            SUM(CASE WHEN fav_won=1 THEN 1 ELSE 0 END) AS wins,
            AVG(yes_ask) AS avg_ask,
            AVG(fee_adjusted_edge) AS avg_edge,
            SUM(COALESCE(pnl_cents, 0)) AS raw_pnl,
            SUM(CASE WHEN fav_won IS NOT NULL THEN
                CASE WHEN fav_won=1 THEN (100 - yes_ask) - {_sql_taker_fee()} ELSE -(yes_ask + {_sql_taker_fee()}) END
            END) AS raw_pnl_1c
        FROM sports_shadow_log
        WHERE signal_fired=1 AND yes_ask IS NOT NULL AND yes_ask > 0 {extra}
        GROUP BY bucket
        ORDER BY min_ask
    """).fetchall()

    result = [dict(r) for r in rows]

    if not result:
        print("  No signal data with prices.")
        return result

    print(f"  {'Bucket':<8} {'Sigs':>5} {'Settled':>8} {'W':>4} {'L':>4} "
          f"{'WR':>7} {'BkEvenWR':>9} {'Edge?':>6} {'AvgEdge':>8} "
          f"{'1c PnL':>8} {'SizedPnL':>9} {'Tag':>16}")
    print("  " + "-" * 110)
    for r in result:
        s = r['settled'] or 0
        w = r['wins'] or 0
        wr = safe_div(w, s)
        avg_ask = r['avg_ask'] or 50
        be_wr = breakeven_wr(avg_ask)
        has_edge = "YES" if wr > be_wr and s >= 5 else ("---" if s < 5 else "NO")
        avg_e = f"{(r['avg_edge'] or 0)*100:.1f}%" if r['avg_edge'] else "n/a"
        raw_pnl = r['raw_pnl'] or 0
        raw_pnl_1c = r['raw_pnl_1c'] or 0
        tag = significance_tag(s, p=0.5, threshold=wr) if s >= 5 else "[n<5]"
        print(f"  {r['bucket']:<8} {r['signals']:>5} {s:>8} {w:>4} {s-w:>4} "
              f"{pct(w, s):>7} {be_wr:>8.1%} {has_edge:>6} {avg_e:>8} "
              f"${raw_pnl_1c/100:>7.2f} ${raw_pnl/100:>8.2f} {tag:>16}")

    return result


# ── Section: Daily P&L Timeline ─────────────────────────────────────────────

def section_daily_pnl(conn: sqlite3.Connection, since: Optional[str] = None,
                      league: Optional[str] = None) -> List[Dict]:
    """Day-by-day PnL with cumulative tracking."""
    extra = _where(since, league, prefix="AND")

    subheader(f"DAILY P&L TIMELINE{f' ({league})' if league else ''}")

    rows = conn.execute(f"""
        SELECT DATE(evaluation_time) AS day,
            COUNT(*) AS signals,
            SUM(CASE WHEN fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled,
            SUM(CASE WHEN fav_won=1 THEN 1 ELSE 0 END) AS wins,
            SUM(COALESCE(pnl_cents, 0)) AS pnl_cents,
            SUM(CASE WHEN fav_won IS NOT NULL THEN
                CASE WHEN fav_won=1 THEN (100 - yes_ask) - {_sql_taker_fee()} ELSE -(yes_ask + {_sql_taker_fee()}) END
            END) AS pnl_cents_1c,
            AVG(yes_ask) AS avg_ask,
            COUNT(DISTINCT game_id) AS games
        FROM sports_shadow_log
        WHERE signal_fired=1 {extra}
        GROUP BY day
        ORDER BY day
    """).fetchall()

    result = [dict(r) for r in rows]

    if not result:
        print("  No signal data.")
        return result

    cumulative = 0
    cumulative_1c = 0
    print(f"  {'Date':<12} {'Sigs':>5} {'Games':>5} {'W':>3} {'L':>3} "
          f"{'WR':>7} {'1cPnL':>8} {'Cum1c':>8} {'SizedPnL':>9} {'CumSized':>9} {'AvgAsk':>8}")
    print("  " + "-" * 95)
    for r in result:
        s = r['settled'] or 0
        w = r['wins'] or 0
        pnl = r['pnl_cents'] or 0
        pnl_1c = r['pnl_cents_1c'] or 0
        cumulative += pnl
        cumulative_1c += pnl_1c
        avg_a = f"{r['avg_ask']:.0f}c" if r['avg_ask'] else "n/a"
        print(f"  {r['day']:<12} {r['signals']:>5} {r['games']:>5} {w:>3} {s-w:>3} "
              f"{pct(w, s):>7} ${pnl_1c/100:>7.2f} ${cumulative_1c/100:>7.2f} "
              f"${pnl/100:>8.2f} ${cumulative/100:>8.2f} {avg_a:>8}")

    return result


# ── Section: Data Quality Report ────────────────────────────────────────────

def section_data_quality(conn: sqlite3.Connection,
                         since: Optional[str] = None) -> Dict:
    """Column fill rates, ticker match rates, NULL analysis."""
    w = _where(since)

    header("DATA QUALITY REPORT")

    # Overall column fill rates
    row = conn.execute(f"""
        SELECT COUNT(*) AS total,
            SUM(CASE WHEN yes_ask IS NOT NULL AND yes_ask > 0 THEN 1 ELSE 0 END) AS has_ask,
            SUM(CASE WHEN yes_bid IS NOT NULL AND yes_bid > 0 THEN 1 ELSE 0 END) AS has_bid,
            SUM(CASE WHEN spread IS NOT NULL THEN 1 ELSE 0 END) AS has_spread,
            SUM(CASE WHEN ticker IS NOT NULL AND ticker != '' THEN 1 ELSE 0 END) AS has_ticker,
            SUM(CASE WHEN pregame_price_home IS NOT NULL THEN 1 ELSE 0 END) AS has_pregame_h,
            SUM(CASE WHEN pregame_price_away IS NOT NULL THEN 1 ELSE 0 END) AS has_pregame_a,
            SUM(CASE WHEN sport_group IS NOT NULL THEN 1 ELSE 0 END) AS has_sport_group,
            SUM(CASE WHEN sport_lr_scale IS NOT NULL THEN 1 ELSE 0 END) AS has_lr_scale,
            SUM(CASE WHEN market_implied_prob IS NOT NULL THEN 1 ELSE 0 END) AS has_mkt_prob,
            SUM(CASE WHEN pregame_capture_method IS NOT NULL THEN 1 ELSE 0 END) AS has_capture_method
        FROM sports_shadow_log {w}
    """).fetchone()

    total = row['total']
    result = {"total": total}

    print(f"  Total rows: {total}")
    print()
    print(f"  {'Column':<30} {'Filled':>8} {'Rate':>8} {'Status':>10}")
    print("  " + "-" * 60)
    for col, key in [
        ("yes_ask (orderbook)", "has_ask"),
        ("yes_bid (orderbook)", "has_bid"),
        ("spread", "has_spread"),
        ("ticker (Kalshi match)", "has_ticker"),
        ("pregame_price_home", "has_pregame_h"),
        ("pregame_price_away", "has_pregame_a"),
        ("sport_group", "has_sport_group"),
        ("sport_lr_scale", "has_lr_scale"),
        ("market_implied_prob", "has_mkt_prob"),
        ("pregame_capture_method", "has_capture_method"),
    ]:
        filled = row[key] or 0
        rate = filled / total if total > 0 else 0
        status = "OK" if rate > 0.95 else ("WARN" if rate > 0.80 else "GAP")
        result[key] = {"filled": filled, "rate": rate}
        print(f"  {col:<30} {filled:>8} {rate:>7.1%} {status:>10}")

    # Signal rows specifically
    print()
    sig_row = conn.execute(f"""
        SELECT COUNT(*) AS total,
            SUM(CASE WHEN yes_ask IS NULL OR yes_ask = 0 THEN 1 ELSE 0 END) AS missing_ask,
            SUM(CASE WHEN closing_price IS NULL AND fav_won IS NOT NULL THEN 1 ELSE 0 END) AS missing_closing,
            SUM(CASE WHEN sport_lr_scale IS NULL THEN 1 ELSE 0 END) AS missing_lr,
            SUM(CASE WHEN pregame_price_home IS NULL THEN 1 ELSE 0 END) AS missing_pregame
        FROM sports_shadow_log
        WHERE signal_fired=1 {_where(since, prefix="AND")}
    """).fetchone()

    sig_total = sig_row['total']
    print(f"  Signal rows ({sig_total} total):")
    for col, key in [
        ("Missing yes_ask", "missing_ask"),
        ("Missing closing_price (settled)", "missing_closing"),
        ("Missing sport_lr_scale", "missing_lr"),
        ("Missing pregame_price", "missing_pregame"),
    ]:
        val = sig_row[key] or 0
        flag = " ***" if val > 0 else ""
        print(f"    {col:<35} {val}/{sig_total}{flag}")

    # Ticker match rate by sport
    print()
    print("  Kalshi ticker match rate by league:")
    league_rows = conn.execute(f"""
        SELECT league,
            COUNT(*) AS total,
            SUM(CASE WHEN ticker IS NOT NULL AND ticker != '' THEN 1 ELSE 0 END) AS matched
        FROM sports_shadow_log {w}
        GROUP BY league
        ORDER BY total DESC
    """).fetchall()
    print(f"  {'League':<15} {'Total':>6} {'Matched':>8} {'Rate':>8}")
    print("  " + "-" * 42)
    for r in league_rows:
        matched = r['matched'] or 0
        rate = safe_div(matched, r['total'])
        flag = " ***" if rate < 0.80 else ""
        print(f"  {r['league']:<15} {r['total']:>6} {matched:>8} {rate:>7.1%}{flag}")

    # Settlement gap analysis — distinguish live vs truly stale
    print()
    print("  Settlement gap analysis:")
    gap_row = conn.execute(f"""
        SELECT
            COUNT(DISTINCT game_id) AS total_games,
            COUNT(DISTINCT CASE WHEN fav_won IS NOT NULL THEN game_id END) AS settled_games,
            COUNT(DISTINCT CASE WHEN fav_won IS NULL THEN game_id END) AS unsettled_games
        FROM sports_shadow_log {w}
    """).fetchone()
    total_g = gap_row['total_games']
    settled_g = gap_row['settled_games']
    unsettled_g = gap_row['unsettled_games']
    print(f"    Total games: {total_g}, Settled: {settled_g}, Unsettled: {unsettled_g}")

    # Check which unsettled games are truly stale (last eval > 6 hours ago)
    if unsettled_g > 0:
        stale_rows = conn.execute(f"""
            SELECT game_id, league, home_team, away_team,
                MAX(evaluation_time) AS last_eval
            FROM sports_shadow_log
            WHERE fav_won IS NULL {_where(since, prefix="AND")}
            GROUP BY game_id
            HAVING MAX(evaluation_time) < datetime('now', '-6 hours')
        """).fetchall()
        live_count = unsettled_g - len(stale_rows)
        print(f"    Still live (eval < 6h ago): {live_count}")
        print(f"    Stale/missing settlement: {len(stale_rows)}")
        if stale_rows:
            print()
            for r in stale_rows:
                print(f"      {r['league']:15s} {r['home_team']:25s} vs {r['away_team']:25s} last_eval={r['last_eval']}")
    result["unsettled"] = unsettled_g

    # Closing price coverage by date
    print()
    print("  Closing price coverage by date:")
    cp_rows = conn.execute(f"""
        SELECT d, total_games, has_closing FROM (
            SELECT DATE(last_eval) AS d,
                COUNT(*) AS total_games,
                SUM(CASE WHEN has_c > 0 THEN 1 ELSE 0 END) AS has_closing
            FROM (
                SELECT game_id, MAX(evaluation_time) AS last_eval,
                    COUNT(closing_price) AS has_c
                FROM sports_shadow_log
                WHERE fav_won IS NOT NULL {_where(since, prefix="AND")}
                GROUP BY game_id
            )
            GROUP BY DATE(last_eval)
        )
        ORDER BY d
    """).fetchall()
    for r in cp_rows:
        flag = " ***" if r['has_closing'] < r['total_games'] else ""
        print(f"    {r['d']}: {r['has_closing']}/{r['total_games']} games have closing_price{flag}")

    return result


# ── Section: Orderbook & Liquidity ───────────────────────────────────────────

def section_liquidity(conn: sqlite3.Connection, since: Optional[str] = None,
                      league: Optional[str] = None) -> Dict:
    # Build WHERE for spread queries
    parts = ["spread IS NOT NULL"]
    if since:
        parts.append(f"evaluation_time >= '{since}'")
    if league:
        parts.append(f"league = '{league}'")
    spread_where = "WHERE " + " AND ".join(parts)

    extra = _where(since, league, prefix="AND")

    subheader(f"ORDERBOOK & LIQUIDITY{f' ({league})' if league else ''}")

    rows = conn.execute(f"""
        SELECT spread, COUNT(*) AS cnt,
               AVG(ask_depth) AS avg_ask_depth,
               AVG(bid_depth) AS avg_bid_depth,
               SUM(CASE WHEN signal_fired=1 THEN 1 ELSE 0 END) AS signals
        FROM sports_shadow_log {spread_where}
        GROUP BY spread ORDER BY spread
    """).fetchall()

    print(f"  {'Spread':>7} {'Count':>6} {'Signals':>8} {'AvgAskDepth':>12} {'AvgBidDepth':>12}")
    print("  " + "-" * 50)
    for r in rows:
        print(f"  {r['spread']:>6}c {r['cnt']:>6} {r['signals']:>8} "
              f"{r['avg_ask_depth']:>12,.0f} {r['avg_bid_depth']:>12,.0f}")

    # Maker fill feasibility
    print()
    sig_rows = conn.execute(f"""
        SELECT yes_ask, yes_bid, spread, ask_depth, bid_depth, league
        FROM sports_shadow_log
        WHERE signal_fired=1 {extra}
    """).fetchall()

    tight_spread = sum(1 for r in sig_rows if r['spread'] and r['spread'] <= 3)
    total_sigs = len(sig_rows)
    print(f"  Signals with spread <= 3c: {tight_spread}/{total_sigs} ({pct(tight_spread, total_sigs)})")
    print(f"  Signals with spread <= 5c: {sum(1 for r in sig_rows if r['spread'] and r['spread'] <= 5)}/{total_sigs}")
    if total_sigs > 0:
        mid_entry_possible = sum(1 for r in sig_rows
                                 if r['yes_bid'] and r['yes_ask']
                                 and (r['yes_ask'] - r['yes_bid']) >= 2)
        print(f"  Mid-price entry possible (spread>=2c): {mid_entry_possible}/{total_sigs}")

    return {"spread_distribution": [dict(r) for r in rows]}


# ── Section: Deficit & Time Heatmap ──────────────────────────────────────────

def section_deficit_time(conn: sqlite3.Connection, since: Optional[str] = None,
                         league: Optional[str] = None) -> Dict:
    w = _where(since, league)

    subheader(f"DEFICIT x TIME HEATMAP{f' ({league})' if league else ''}")

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
            COUNT(DISTINCT CASE WHEN fav_won=1 THEN game_id END) AS wins,
            COUNT(DISTINCT CASE WHEN fav_won=0 THEN game_id END) AS losses,
            AVG(yes_ask) AS avg_ask
        FROM sports_shadow_log {w}
        GROUP BY def_bucket, time_bucket
        ORDER BY def_bucket, time_bucket
    """).fetchall()

    result = [dict(r) for r in rows]

    print(f"  {'Deficit':<16} {'Time':>8} {'Evals':>6} {'Sigs':>5} {'W':>4} {'L':>4} {'AvgAsk':>7}")
    print("  " + "-" * 55)
    for r in result:
        avg_ask_str = f"{r['avg_ask']:.0f}c" if r['avg_ask'] else "n/a"
        print(f"  {r['def_bucket']:<16} {r['time_bucket']:>8} {r['evals']:>6} "
              f"{r['signals']:>5} {r['wins'] or 0:>4} {r['losses'] or 0:>4} "
              f"{avg_ask_str:>7}")

    return result


# ── Section: Favorite ID Audit (global only) ─────────────────────────────────

def section_fav_id_audit(conn: sqlite3.Connection,
                         since: Optional[str] = None) -> Dict:
    w = _where(since)

    header("FAVORITE IDENTIFICATION AUDIT")

    rows = conn.execute(f"""
        SELECT DISTINCT game_id, league, home_team, away_team,
               home_code, away_code, pregame_fav_code, pregame_fav_prob,
               pregame_price_home, pregame_price_away,
               MAX(home_score) AS last_home, MAX(away_score) AS last_away
        FROM sports_shadow_log {w}
        GROUP BY game_id
    """).fetchall()

    issues = []
    for r in rows:
        fav_code = r['pregame_fav_code']
        fav_prob = r['pregame_fav_prob']
        home_price = r['pregame_price_home']
        away_price = r['pregame_price_away']

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


# ── Section: Settlement Gap Check ────────────────────────────────────────────

def section_settlement_gaps(conn: sqlite3.Connection, since: Optional[str] = None,
                            league: Optional[str] = None) -> Dict:
    extra = _where(since, league, prefix="AND")

    subheader(f"SETTLEMENT GAPS{f' ({league})' if league else ''}")

    total = conn.execute(f"""
        SELECT COUNT(DISTINCT game_id) FROM sports_shadow_log WHERE 1=1 {extra}
    """).fetchone()[0]

    settled = conn.execute(f"""
        SELECT COUNT(DISTINCT game_id) FROM sports_shadow_log
        WHERE fav_won IS NOT NULL {extra}
    """).fetchone()[0]

    with_closing = conn.execute(f"""
        SELECT COUNT(DISTINCT game_id) FROM sports_shadow_log
        WHERE closing_price IS NOT NULL {extra}
    """).fetchone()[0]

    # Distinguish live games from stale/missing settlement
    stale_count = 0
    live_count = 0
    if settled < total:
        stale = conn.execute(f"""
            SELECT COUNT(DISTINCT game_id) FROM sports_shadow_log
            WHERE fav_won IS NULL {extra}
              AND game_id NOT IN (
                SELECT DISTINCT game_id FROM sports_shadow_log
                WHERE evaluation_time > datetime('now', '-6 hours') AND fav_won IS NULL {extra}
              )
        """).fetchone()[0]
        stale_count = stale
        live_count = total - settled - stale

    print(f"  Total unique games:           {total}")
    print(f"  Games with fav_won populated: {settled} ({pct(settled, total)})")
    print(f"  Games with closing_price:     {with_closing} ({pct(with_closing, total)})")
    if settled < total:
        gap = total - settled
        print(f"\n  Unsettled games:              {gap}")
        print(f"    Still live (< 6h ago):      {live_count}")
        print(f"    Stale (> 6h, no settle):    {stale_count}")
        if stale_count > 0:
            print(f"  *** {stale_count} games are STALE — settlement may have failed ***")

    return {"total_games": total, "settled_games": settled,
            "gap": total - settled, "stale": stale_count, "live": live_count}


# ── Section: Wald Sequential Test ────────────────────────────────────────────

def section_wald_sprt(conn: sqlite3.Connection, since: Optional[str] = None,
                      league: Optional[str] = None) -> Dict:
    extra = _where(since, league, prefix="AND")

    subheader(f"WALD SPRT{f' ({league})' if league else ''}")

    # De-dup: one outcome per game_id (multiple signals per game are NOT independent)
    rows = conn.execute(f"""
        SELECT
            (SELECT s2.fav_won FROM sports_shadow_log s2
             WHERE s2.game_id = s.game_id AND s2.signal_fired=1
             ORDER BY s2.evaluation_time ASC LIMIT 1) AS fav_won,
            MIN(evaluation_time) AS first_eval
        FROM sports_shadow_log s
        WHERE signal_fired=1 AND fav_won IS NOT NULL {extra}
        GROUP BY game_id
        ORDER BY first_eval
    """).fetchall()

    if not rows:
        print("  No settled games for sequential test.")
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
    print(f"  Games tested (de-duped): {n}")
    print(f"  Wins:           {wins} ({pct(wins, n)})")
    print(f"  Log LR:         {llr:.3f}")
    print(f"  Boundaries:     reject H0 at {A:.3f}, accept H0 at {B:.3f}")
    print(f"  Decision:       {decision}")

    return {"n": n, "wins": wins, "llr": llr, "decision": decision}


# ── Section: Readiness Scorecard ─────────────────────────────────────────────

def section_readiness(conn: sqlite3.Connection, since: Optional[str] = None,
                      league: Optional[str] = None) -> Dict:
    extra = _where(since, league, prefix="AND")

    subheader(f"READINESS SCORECARD{f' ({league})' if league else ''}")

    checks = []

    # 1. Settlement backfill working
    settled = conn.execute(f"""
        SELECT COUNT(*) FROM sports_shadow_log
        WHERE fav_won IS NOT NULL {extra}
    """).fetchone()[0]
    total = conn.execute(f"""
        SELECT COUNT(DISTINCT game_id) FROM sports_shadow_log WHERE 1=1 {extra}
    """).fetchone()[0]
    c1 = settled > 0
    checks.append(("Settlement backfill working", c1, f"{settled}/{total} games settled"))

    # 2. 50+ settled games (25+ for per-sport) — de-duped
    min_games = 25 if league else 50
    g, gs, gw = _dedup_game_wr(conn, since=since, league=league)
    c2 = gs >= min_games
    checks.append((f"{min_games}+ settled games (de-duped)", c2, f"{gs}/{min_games}"))

    # 3. Win rate > 55% (de-duped per game)
    wr = safe_div(gw, gs)
    c3 = wr > 0.55 and gs >= 20
    checks.append(("Win rate > 55% (20+ games, de-duped)", c3, f"{wr:.1%} ({gw}/{gs} games)"))

    # 4. Positive CLV
    clv_row = conn.execute(f"""
        SELECT AVG(closing_price - yes_ask) AS avg_clv
        FROM sports_shadow_log
        WHERE signal_fired=1 AND closing_price IS NOT NULL AND yes_ask IS NOT NULL {extra}
    """).fetchone()
    avg_clv = clv_row["avg_clv"] if clv_row and clv_row["avg_clv"] else None
    c4 = avg_clv is not None and avg_clv > 0
    checks.append(("Positive CLV", c4, f"{avg_clv:.1f}c" if avg_clv else "no data"))

    # 5. Model calibration reasonable (gap < 20pp, excluding 0c market)
    avg_gap = conn.execute(f"""
        SELECT AVG(comeback_prob * 100 - yes_ask) AS gap
        FROM sports_shadow_log
        WHERE signal_fired=1 AND yes_ask > 0 {extra}
    """).fetchone()
    gap_val = avg_gap["gap"] if avg_gap and avg_gap["gap"] else None
    c5 = gap_val is not None and abs(gap_val) < 20
    checks.append(("Model-market gap < 20pp", c5,
                    f"{gap_val:.1f}pp avg" if gap_val else "no data"))

    # 6. Per-game dedup implemented
    multi = conn.execute(f"""
        SELECT COUNT(*) FROM (
            SELECT game_id, COUNT(*) as n
            FROM sports_shadow_log
            WHERE signal_fired=1 {extra}
            GROUP BY game_id HAVING n > 1
        )
    """).fetchone()[0]
    c6 = multi == 0
    checks.append(("No multi-entry per game", c6,
                    f"{multi} games with duplicate signals (restart dedup gap)" if multi else "clean"))

    # 7. Positive sim PnL (deduped: one pnl per game)
    sim_pnl = conn.execute(f"""
        SELECT SUM(game_pnl) AS pnl,
               SUM(game_pnl_1c) AS pnl_1c
        FROM (
            SELECT game_id,
                   (SELECT COALESCE(s2.pnl_cents, 0) FROM sports_shadow_log s2
                    WHERE s2.game_id = s.game_id AND s2.signal_fired=1
                    ORDER BY s2.evaluation_time ASC LIMIT 1) AS game_pnl,
                   (SELECT CASE WHEN s2.fav_won IS NOT NULL THEN
                       CASE WHEN s2.fav_won=1 THEN (100 - s2.yes_ask) - {_sql_taker_fee('s2.yes_ask')}
                       ELSE -(s2.yes_ask + {_sql_taker_fee('s2.yes_ask')}) END
                   END FROM sports_shadow_log s2
                    WHERE s2.game_id = s.game_id AND s2.signal_fired=1
                    ORDER BY s2.evaluation_time ASC LIMIT 1) AS game_pnl_1c
            FROM sports_shadow_log s
            WHERE signal_fired=1 {extra}
            GROUP BY game_id
        )
    """).fetchone()
    pnl_val = sim_pnl["pnl"] if sim_pnl and sim_pnl["pnl"] else 0
    pnl_val_1c = sim_pnl["pnl_1c"] if sim_pnl and sim_pnl["pnl_1c"] else 0
    c7 = pnl_val > 0
    checks.append(("Positive sim PnL", c7,
                    f"1c=${pnl_val_1c/100:.2f}, sized=${pnl_val/100:.2f}"))

    print()
    all_pass = True
    result_checks = []
    for desc, passed, detail in checks:
        symbol = "+" if passed else "X"
        all_pass = all_pass and passed
        print(f"  [{symbol}] {desc}: {detail}")
        result_checks.append({"check": desc, "passed": passed, "detail": detail})

    passed_count = sum(1 for _, p, _ in checks if p)
    total_checks = len(checks)
    print()
    if all_pass:
        print(f"  >>> ALL {total_checks} CHECKS PASSED — Ready for live promotion consideration")
    else:
        failing = total_checks - passed_count
        print(f"  >>> {passed_count}/{total_checks} checks pass — "
              f"{'CONTINUE COLLECTING' if passed_count >= 3 else 'NOT READY'}")

    return {"all_pass": all_pass, "checks": result_checks,
            "passed": passed_count, "total": total_checks}


# ── Section: Counterfactual Analysis ─────────────────────────────────────────

def section_counterfactual(conn: sqlite3.Connection, since: Optional[str] = None,
                           league: Optional[str] = None) -> Dict:
    w = _where(since, league)

    subheader(f"COUNTERFACTUAL ANALYSIS{f' ({league})' if league else ''}")

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
        FROM sports_shadow_log {w}
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

    # Per-threshold settled outcomes — de-duped per game
    extra_w = _where(since, league)
    # Game-level: a game "would signal" at threshold X if ANY eval in that game would
    settled_row = conn.execute(f"""
        SELECT
            SUM(CASE WHEN fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled_50,
            SUM(CASE WHEN fav_won = 1 THEN 1 ELSE 0 END) AS wins_50
        FROM (
            SELECT game_id,
                   (SELECT s2.fav_won FROM sports_shadow_log s2
                    WHERE s2.game_id = s.game_id
                    ORDER BY s2.evaluation_time ASC LIMIT 1) AS fav_won
            FROM sports_shadow_log s {extra_w + (' AND' if extra_w else 'WHERE')} would_signal_50c=1
            GROUP BY game_id
        )
    """).fetchone()
    settled_60 = conn.execute(f"""
        SELECT
            SUM(CASE WHEN fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled,
            SUM(CASE WHEN fav_won = 1 THEN 1 ELSE 0 END) AS wins
        FROM (
            SELECT game_id,
                   (SELECT s2.fav_won FROM sports_shadow_log s2
                    WHERE s2.game_id = s.game_id
                    ORDER BY s2.evaluation_time ASC LIMIT 1) AS fav_won
            FROM sports_shadow_log s {extra_w + (' AND' if extra_w else 'WHERE')} would_signal_60c=1
            GROUP BY game_id
        )
    """).fetchone()
    settled_70 = conn.execute(f"""
        SELECT
            SUM(CASE WHEN fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled,
            SUM(CASE WHEN fav_won = 1 THEN 1 ELSE 0 END) AS wins
        FROM (
            SELECT game_id,
                   (SELECT s2.fav_won FROM sports_shadow_log s2
                    WHERE s2.game_id = s.game_id
                    ORDER BY s2.evaluation_time ASC LIMIT 1) AS fav_won
            FROM sports_shadow_log s {extra_w + (' AND' if extra_w else 'WHERE')} would_signal_70c=1
            GROUP BY game_id
        )
    """).fetchone()
    settled_80 = conn.execute(f"""
        SELECT
            SUM(CASE WHEN fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled,
            SUM(CASE WHEN fav_won = 1 THEN 1 ELSE 0 END) AS wins
        FROM (
            SELECT game_id,
                   (SELECT s2.fav_won FROM sports_shadow_log s2
                    WHERE s2.game_id = s.game_id
                    ORDER BY s2.evaluation_time ASC LIMIT 1) AS fav_won
            FROM sports_shadow_log s {extra_w + (' AND' if extra_w else 'WHERE')} would_signal_80c=1
            GROUP BY game_id
        )
    """).fetchone()

    print("\n  Per-threshold WR (de-duped per game):")
    for label, sr in [
        ("Price <= 50c", settled_row),
        ("Price <= 60c", settled_60),
        ("Price <= 70c", settled_70),
        ("Price <= 80c", settled_80),
    ]:
        s = (sr['settled'] if 'settled' in sr.keys() else sr['settled_50']) or 0
        w = (sr['wins'] if 'wins' in sr.keys() else sr['wins_50']) or 0
        if s > 0:
            print(f"  {label:<25} {w}W/{s-w}L  WR={pct(w, s)} (n={s} games)")
        else:
            print(f"  {label:<25} no settled data")

    return result


# ── Section: Pregame Capture Rate (global only) ─────────────────────────────

def section_pregame_capture(conn: sqlite3.Connection,
                            since: Optional[str] = None) -> Dict:
    w = _where(since)

    header("PREGAME CAPTURE RATE")

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
        FROM sports_shadow_log {w}
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
    where_clause = w + (" AND" if w else " WHERE") + " pregame_price_home IS NOT NULL"
    with_price = conn.execute(f"""
        SELECT COUNT(*) FROM sports_shadow_log {where_clause}
    """).fetchone()[0]
    print(f"\n  Evaluations with pregame_price_home: {with_price}/{total} ({pct(with_price, total)})")

    return {"methods": result, "total": total, "with_pregame_price": with_price}


# ── Section: LR Scale A/B ───────────────────────────────────────────────────

def section_lr_scale_ab(conn: sqlite3.Connection, since: Optional[str] = None,
                        league: Optional[str] = None) -> Dict:
    w = _where(since, league)

    subheader(f"LR SCALE A/B (live=0.2 vs shadow=0.5){f' ({league})' if league else ''}")

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
        FROM sports_shadow_log {w}
    """).fetchone()

    result = {k: row[k] for k in row.keys()}

    live_s = result['live_signals'] or 0
    shadow_s = result['shadow_signals'] or 0
    live_settled = result['live_settled'] or 0
    live_wins = result['live_wins'] or 0
    shadow_settled = result['shadow_settled'] or 0
    shadow_wins = result['shadow_wins'] or 0

    # De-duped game-level WR for live signals
    g_live, gs_live, gw_live = _dedup_game_wr(conn, since=since, league=league)

    print(f"  {'Metric':<30} {'Live (scale=0.2)':>18} {'Shadow (scale=0.5)':>20}")
    print("  " + "-" * 70)
    print(f"  {'Signals fired':<30} {live_s:>18} {shadow_s:>20}")
    print(f"  {'Settled (raw signals)':<30} {live_settled:>18} {shadow_settled:>20}")
    print(f"  {'Raw WR':<30} {pct(live_wins, live_settled):>18} {pct(shadow_wins, shadow_settled):>20}")
    print(f"  {'Game WR (de-duped)':<30} {f'{gw_live}/{gs_live}':>18} {'n/a':>20}")
    live_post = result['live_avg_posterior']
    shadow_post = result['shadow_avg_posterior']
    print(f"  {'Avg posterior':<30} {f'{live_post:.1%}' if live_post else 'n/a':>18} "
          f"{f'{shadow_post:.1%}' if shadow_post else 'n/a':>20}")

    if live_s > shadow_s:
        print(f"\n  Scale=0.2 generates MORE signals ({live_s} vs {shadow_s})")
    elif shadow_s > live_s:
        print(f"\n  Scale=0.5 would generate MORE signals ({shadow_s} vs {live_s})")
    else:
        print(f"\n  Both scales produce same number of signals ({live_s})")

    return result


# ── Section: Per Sport Group Summary ─────────────────────────────────────

def section_per_sport_group(conn: sqlite3.Connection,
                            since: Optional[str] = None) -> List[Dict]:
    """Aggregate metrics by sport_group (basketball, hockey, etc.)."""
    # Check if sport_group column exists
    try:
        conn.execute("SELECT sport_group FROM sports_shadow_log LIMIT 1")
    except Exception:
        print("  sport_group column not yet populated (need engine restart).")
        return []

    w = _where(since)

    rows = conn.execute(f"""
        SELECT COALESCE(sport_group, 'unknown') AS sport_group,
               COUNT(*) AS evals,
               COUNT(DISTINCT league) AS leagues,
               COUNT(DISTINCT game_id) AS games,
               SUM(CASE WHEN signal_fired=1 THEN 1 ELSE 0 END) AS signals,
               SUM(CASE WHEN signal_fired=1 AND fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled_sigs,
               SUM(CASE WHEN signal_fired=1 AND fav_won=1 THEN 1 ELSE 0 END) AS sig_wins,
               AVG(CASE WHEN signal_fired=1 THEN fee_adjusted_edge END) AS avg_edge,
               AVG(CASE WHEN signal_fired=1 THEN comeback_prob END) AS avg_model,
               AVG(CASE WHEN signal_fired=1 AND yes_ask > 0 THEN yes_ask END) AS avg_ask,
               AVG(sport_lr_scale) AS lr_scale
        FROM sports_shadow_log {w}
        GROUP BY sport_group
        ORDER BY signals DESC, evals DESC
    """).fetchall()

    result = [dict(r) for r in rows]

    header("PER SPORT GROUP SUMMARY")
    print(f"  {'Group':<12} {'Lgues':>5} {'Games':>5} {'Evals':>6} {'Sigs':>5} "
          f"{'RawWR':>7} {'GameWR':>8} {'AvgEdge':>8} {'LRscale':>8}")
    print("  " + "-" * 80)
    for r in result:
        raw_wr = pct(r['sig_wins'] or 0, r['settled_sigs'] or 0)
        g, gs, gw = _dedup_game_wr(conn, since=since, sport_group=r['sport_group'])
        game_wr = f"{gw}/{gs}" if gs > 0 else "n/a"
        r['dedup_settled'] = gs
        r['dedup_wins'] = gw
        avg_e = f"{(r['avg_edge'] or 0)*100:.1f}%" if r['avg_edge'] else "n/a"
        lr_s = f"{r['lr_scale']:.2f}" if r['lr_scale'] else "n/a"
        print(f"  {r['sport_group']:<12} {r['leagues']:>5} {r['games']:>5} {r['evals']:>6} "
              f"{r['signals']:>5} {raw_wr:>7} {game_wr:>8} "
              f"{avg_e:>8} {lr_s:>8}")

    return result


# ── Section: Sport Group Calibration Analysis ────────────────────────────

def section_sport_group_calibration(conn: sqlite3.Connection,
                                    since: Optional[str] = None) -> Dict:
    """Per-sport-group calibration: model predicted vs actual WR.

    Key diagnostic for Phase 2 per-sport LR scale tuning.
    """
    # Check if sport_group column exists
    try:
        conn.execute("SELECT sport_group FROM sports_shadow_log LIMIT 1")
    except Exception:
        print("  sport_group column not yet populated.")
        return {}

    w = _where(since)

    header("SPORT GROUP CALIBRATION")
    print("  Per-group model accuracy: predicted comeback % vs actual win rate")
    print("  Overconfident = predicted >> actual. Underconfident = predicted << actual.")
    print()

    rows = conn.execute(f"""
        SELECT COALESCE(sport_group, 'unknown') AS sport_group,
               COUNT(*) AS n_signals,
               AVG(comeback_prob) AS avg_predicted,
               SUM(CASE WHEN fav_won=1 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled,
               AVG(CASE WHEN yes_ask > 0 THEN yes_ask END) AS avg_market,
               AVG(sport_lr_scale) AS lr_scale,
               AVG(likelihood_ratio) AS avg_lr
        FROM sports_shadow_log
        WHERE signal_fired=1 {_where(since, prefix="AND")}
        GROUP BY sport_group
        ORDER BY n_signals DESC
    """).fetchall()

    result = {}

    if not rows:
        print("  No signals to analyze.")
        return result

    print(f"  {'Group':<12} {'Sigs':>5} {'Games':>5} {'Predicted':>10} {'ActualWR':>9} "
          f"{'Gap':>8} {'AvgMkt':>7} {'AvgLR':>6} {'LRscale':>8} {'Status':<20}")
    print("  " + "-" * 110)

    for r in rows:
        rdict = dict(r)
        avg_pred = rdict['avg_predicted'] or 0

        # De-duped game-level actual WR
        g, gs, gw = _dedup_game_wr(conn, since=since, sport_group=rdict['sport_group'])
        actual_wr = safe_div(gw, gs) if gs > 0 else None
        gap = (avg_pred - actual_wr) * 100 if actual_wr is not None else None
        avg_mkt = f"{rdict['avg_market']:.0f}c" if rdict['avg_market'] else "n/a"
        avg_lr_str = f"{rdict['avg_lr']:.2f}" if rdict['avg_lr'] else "n/a"
        lr_s = f"{rdict['lr_scale']:.2f}" if rdict['lr_scale'] else "n/a"

        if actual_wr is not None and gs >= 5:
            if gap > 15:
                status = "OVERCONFIDENT"
            elif gap < -15:
                status = "UNDERCONFIDENT"
            elif abs(gap) <= 5:
                status = "well-calibrated"
            else:
                status = "slight bias"
        elif gs > 0:
            status = f"too few ({gs} games)"
        else:
            status = "no settlements"

        pred_str = f"{avg_pred:.1%}" if avg_pred else "n/a"
        actual_str = f"{actual_wr:.1%} ({gw}/{gs})" if actual_wr is not None else "n/a"
        gap_str = f"{gap:+.1f}pp" if gap is not None else "n/a"

        print(f"  {rdict['sport_group']:<12} {rdict['n_signals']:>5} {gs:>5} "
              f"{pred_str:>10} {actual_str:>9} {gap_str:>8} {avg_mkt:>7} "
              f"{avg_lr_str:>6} {lr_s:>8} {status:<20}")

        rdict['actual_wr'] = actual_wr
        rdict['dedup_settled'] = gs
        rdict['dedup_wins'] = gw
        rdict['calibration_gap'] = gap
        rdict['status'] = status
        result[rdict['sport_group']] = rdict

    # Brier scores per group
    print()
    subheader("BRIER SCORES BY SPORT GROUP")
    print("  Lower Brier = better calibration. Random baseline = 0.25 at 50/50.")
    print()
    for group, data in result.items():
        gs = data.get('dedup_settled', 0)
        if gs < 5:
            print(f"  {group:<12} n={gs} — insufficient data")
            continue
        # Compute Brier from signal rows
        brier_rows = conn.execute(f"""
            SELECT comeback_prob, fav_won
            FROM (
                SELECT game_id, AVG(comeback_prob) AS comeback_prob,
                       (SELECT s2.fav_won FROM sports_shadow_log s2
                        WHERE s2.game_id = s.game_id AND s2.signal_fired=1
                        ORDER BY s2.evaluation_time ASC LIMIT 1) AS fav_won
                FROM sports_shadow_log s
                WHERE signal_fired=1 AND fav_won IS NOT NULL
                  AND sport_group = '{group}' {_where(since, prefix="AND")}
                GROUP BY game_id
            )
        """).fetchall()
        if brier_rows:
            brier = sum((r['comeback_prob'] - r['fav_won'])**2 for r in brier_rows) / len(brier_rows)
            # Market Brier for comparison
            mkt_brier_rows = conn.execute(f"""
                SELECT yes_ask / 100.0 AS mkt_prob, fav_won
                FROM (
                    SELECT game_id, AVG(yes_ask) AS yes_ask,
                           (SELECT s2.fav_won FROM sports_shadow_log s2
                            WHERE s2.game_id = s.game_id AND s2.signal_fired=1
                            ORDER BY s2.evaluation_time ASC LIMIT 1) AS fav_won
                    FROM sports_shadow_log s
                    WHERE signal_fired=1 AND fav_won IS NOT NULL AND yes_ask > 0
                      AND sport_group = '{group}' {_where(since, prefix="AND")}
                    GROUP BY game_id
                )
            """).fetchall()
            mkt_brier = (sum((r['mkt_prob'] - r['fav_won'])**2 for r in mkt_brier_rows) / len(mkt_brier_rows)
                         if mkt_brier_rows else None)
            mkt_str = f"{mkt_brier:.4f}" if mkt_brier is not None else "n/a"
            edge = "BETTER" if mkt_brier and brier < mkt_brier else "WORSE"
            print(f"  {group:<12} Model Brier={brier:.4f}  Market Brier={mkt_str}  "
                  f"n={len(brier_rows)} games  [{edge} than market]")
            data['brier_model'] = brier
            data['brier_market'] = mkt_brier

    # LR scale recommendations
    print()
    recs = []
    for group, data in result.items():
        gap = data.get('calibration_gap')
        settled = data.get('settled', 0)
        lr_scale = data.get('lr_scale', 0.2)

        lr_display = lr_scale if lr_scale is not None else 0.2

        dedup_settled = data.get('dedup_settled', 0)
        if gap is None or dedup_settled < 10:
            recs.append((group, "COLLECT MORE DATA",
                         f"Only {dedup_settled} settled games. Need 50+ for reliable calibration."))
        elif gap > 20:
            recs.append((group, "DECREASE lr_scale",
                         f"Model {gap:+.1f}pp overconfident. "
                         f"Current scale={lr_display:.2f}, try {max(0.05, lr_display * 0.5):.2f}"))
        elif gap < -20:
            recs.append((group, "INCREASE lr_scale",
                         f"Model {gap:+.1f}pp underconfident. "
                         f"Current scale={lr_display:.2f}, try {min(1.0, lr_display * 2.0):.2f}"))
        elif abs(gap) <= 10:
            recs.append((group, "HOLD",
                         f"Gap={gap:+.1f}pp — calibration is reasonable at scale={lr_display:.2f}"))
        else:
            direction = "decrease" if gap > 0 else "increase"
            recs.append((group, f"MONITOR ({direction})",
                         f"Gap={gap:+.1f}pp — borderline. Watch with more data."))

    if recs:
        subheader("LR SCALE TUNING RECOMMENDATIONS (Phase 2)")
        for group, action, detail in recs:
            print(f"  {group:<12} [{action}] {detail}")

    return result


# ── Section: Data-Driven Recommendations ─────────────────────────────────────

def section_recommendations(conn: sqlite3.Connection, since: Optional[str] = None,
                            per_sport_data: Optional[Dict] = None,
                            settlement_data: Optional[Dict] = None) -> None:
    header("RECOMMENDATIONS (data-driven, per-sport)")

    extra = _where(since, prefix="AND")

    # Global recs
    recs_global = []

    # Settlement backfill — check actual data (only flag stale, not live)
    if settlement_data:
        total_stale = sum(v.get("stale", 0) for v in settlement_data.values())
        total_gap = sum(v.get("gap", 0) for v in settlement_data.values())
        total_live = sum(v.get("live", 0) for v in settlement_data.values())
        if total_stale > 0:
            recs_global.append((
                "MEDIUM",
                "Stale unsettled games",
                f"{total_stale} games finished >6h ago but missing settlement data "
                f"(fav_won IS NULL). Likely ESPN status never returned 'final'. "
                f"({total_live} games still live — normal.)",
                "INVESTIGATE"
            ))

    # Ticker match failures — check for leagues with 0% match
    try:
        tm_rows = conn.execute(f"""
            SELECT league, COUNT(*) AS total,
                SUM(CASE WHEN ticker IS NOT NULL AND ticker != '' THEN 1 ELSE 0 END) AS matched
            FROM sports_shadow_log WHERE 1=1 {extra}
            GROUP BY league
            HAVING matched = 0 AND total > 5
        """).fetchall()
        if tm_rows:
            leagues_str = ", ".join(r['league'] for r in tm_rows)
            recs_global.append((
                "LOW",
                "No Kalshi ticker match for some leagues",
                f"Leagues with 0% ticker match: {leagues_str}. "
                "These have ESPN data but no Kalshi market match — "
                "no orderbook data, CLV, or closing price possible.",
                "INFORMATIONAL"
            ))
    except Exception:
        pass

    if recs_global:
        subheader("GLOBAL")
        for priority, title, detail, timeline in recs_global:
            print(f"\n  [{priority}] {title} ({timeline})")
            print(f"    {detail}")

    # Per-sport recs
    if per_sport_data:
        for sport, data in sorted(per_sport_data.items()):
            sport_recs = []

            # Signal dedup: only if multi-entry games still exist
            # Note: bot already deduplicates via in-memory _signaled_games set,
            # but restarts clear it. If this triggers, run retroactive dedup cleanup.
            multi = conn.execute(f"""
                SELECT COUNT(*) FROM (
                    SELECT game_id, COUNT(*) as n
                    FROM sports_shadow_log
                    WHERE signal_fired=1 AND league='{sport}' {extra}
                    GROUP BY game_id HAVING n > 1
                )
            """).fetchone()[0]
            if multi > 0:
                sport_recs.append((
                    "LOW",
                    "Retroactive dedup cleanup needed",
                    f"{multi} games have multiple signals (bot restart lost dedup state). "
                    "Clean via: UPDATE sports_shadow_log SET signal_fired=0, "
                    "filter_stage='sports_signal_dup' WHERE game_id=X AND id != (first).",
                    "WEEK 1"
                ))

            # Model calibration: only if avg gap > 15pp (excluding 0c market)
            signal_data = data.get("signal_quality", {})
            avg_gap = signal_data.get("avg_model_market_gap")
            if avg_gap is not None and avg_gap > 15:
                sport_recs.append((
                    "HIGH",
                    "Recalibrate LR table",
                    f"Average model-market gap is {avg_gap:+.1f}pp (>{'+15pp'} threshold). "
                    "Model is overconfident — use historical comeback rate data to derive empirical LR values.",
                    "WEEK 2"
                ))

            # LR scale rec based on WR
            readiness = data.get("readiness", {})
            settled_check = next((c for c in readiness.get("checks", [])
                                  if "settled signals" in c.get("check", "")), None)
            wr_check = next((c for c in readiness.get("checks", [])
                             if "Win rate" in c.get("check", "")), None)
            if wr_check and not wr_check.get("passed"):
                sport_recs.append((
                    "HIGH",
                    "WR below 55% — do not promote",
                    f"Current: {wr_check['detail']}. Need more data or model improvements before live trading.",
                    "ONGOING"
                ))

            if sport_recs:
                subheader(f"{sport}")
                for priority, title, detail, timeline in sport_recs:
                    print(f"\n  [{priority}] {title} ({timeline})")
                    print(f"    {detail}")

    # Check if no recs at all
    if not recs_global and (not per_sport_data or
            all(not _sport_has_recs(conn, sport, since, data)
                for sport, data in per_sport_data.items())):
        print("\n  No data-driven recommendations at this time.")


def _sport_has_recs(conn: sqlite3.Connection, sport: str,
                    since: Optional[str], data: Dict) -> bool:
    """Quick check if a sport would generate any recommendations."""
    extra = _where(since, prefix="AND")
    multi = conn.execute(f"""
        SELECT COUNT(*) FROM (
            SELECT game_id, COUNT(*) as n
            FROM sports_shadow_log
            WHERE signal_fired=1 AND league='{sport}' {extra}
            GROUP BY game_id HAVING n > 1
        )
    """).fetchone()[0]
    if multi > 0:
        return True
    signal_data = data.get("signal_quality", {})
    if (signal_data.get("avg_model_market_gap") or 0) > 15:
        return True
    readiness = data.get("readiness", {})
    wr_check = next((c for c in readiness.get("checks", [])
                      if "Win rate" in c.get("check", "")), None)
    if wr_check and not wr_check.get("passed"):
        return True
    return False


def section_cal_engine_obs(conn: sqlite3.Connection,
                           since: Optional[str] = None) -> Dict:
    """CalEngine observation pipeline: settled evals with raw_prob per sport group."""
    header("CALENGINE OBSERVATION PIPELINE")
    print("  Settled evaluated_opportunities with raw_prob (feeds per-sport CalEngines)")
    print()

    try:
        w = f"AND evaluation_time >= '{since}'" if since else ""
        rows = conn.execute(f"""
            SELECT asset,
                   COUNT(*) AS total,
                   SUM(CASE WHEN raw_prob IS NOT NULL THEN 1 ELSE 0 END) AS with_raw_prob,
                   SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) AS settled,
                   SUM(CASE WHEN status='settled' AND raw_prob IS NOT NULL THEN 1 ELSE 0 END) AS cal_eligible
            FROM evaluated_opportunities
            WHERE product_type='sports' {w}
            GROUP BY asset
            ORDER BY total DESC
        """).fetchall()

        if not rows:
            print("  No sports evaluated_opportunities found.")
            return {"total": 0, "cal_eligible": 0}

        print(f"  {'Asset':<15} {'Total':>7} {'HasRawProb':>11} {'Settled':>8} {'CalEligible':>12}")
        print("  " + "-" * 55)

        grand_total = 0
        grand_eligible = 0
        result = {}
        for r in rows:
            rdict = dict(r)
            asset = rdict['asset'] or 'unknown'
            total = rdict['total']
            with_rp = rdict['with_raw_prob'] or 0
            settled = rdict['settled'] or 0
            eligible = rdict['cal_eligible'] or 0
            grand_total += total
            grand_eligible += eligible
            print(f"  {asset:<15} {total:>7} {with_rp:>11} {settled:>8} {eligible:>12}")
            result[asset] = {"total": total, "with_raw_prob": with_rp,
                             "settled": settled, "cal_eligible": eligible}

        print("  " + "-" * 55)
        print(f"  {'TOTAL':<15} {grand_total:>7} {'':>11} {'':>8} {grand_eligible:>12}")
        print()
        if grand_eligible == 0:
            print("  >>> No CalEngine observations yet — raw_prob was NULL on pre-deploy rows")
            print("  >>> New rows after deploy will have raw_prob populated")
        else:
            print(f"  >>> {grand_eligible} observations available for CalEngine training")

        return {"total": grand_total, "cal_eligible": grand_eligible, "by_asset": result}
    except Exception as e:
        print(f"  ERROR: {e}")
        return {"error": str(e)}


def detect_regime_start() -> str:
    """Auto-detect regime start by finding the last git commit that changed
    sports engine constants in sports_data.py or sports_engine.py."""
    import subprocess

    REGIME_CONSTANTS = [
        "CONSERVATIVE_LR_SCALE", "MAX_MODEL_MARKET_GAP",
        "BINARY_ENTRY_CRITERIA", "THREE_WAY_ENTRY_CRITERIA",
        "BINARY_LR_TABLE", "THREE_WAY_LR_TABLE",
        "SPORT_GROUPS", "LEAGUES",
    ]

    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        result = subprocess.run(
            ["git", "log", "--format=%H %aI", "--since=180 days ago",
             "--", "sports_data.py", "sports_engine.py"],
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
                 "--", "sports_data.py", "sports_engine.py"],
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


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Comprehensive sports shadow audit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("--db", default="state.db", help="Path to state.db")
    parser.add_argument("--since", default=None,
                        help="Filter to data since this date (YYYY-MM-DD)")
    parser.add_argument("--regime", choices=["auto"],
                        help="Auto-detect regime start from git history")
    parser.add_argument("--sport-group", default=None,
                        help="Filter to a sport group (basketball, hockey, soccer, etc.)")
    parser.add_argument("--json", default=None,
                        help="Write JSON artifact to this path")
    args = parser.parse_args()

    if args.regime == "auto":
        args.since = detect_regime_start()
        print(f"[Auto-detected regime start: {args.since}]")

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

    sport_group_filter = getattr(args, 'sport_group', None)

    print()
    print("#" * 72)
    print("##  KALSHI SPORTS SHADOW AUDIT")
    print(f"##  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    if args.since:
        print(f"##  Regime filter: since {args.since}")
    if sport_group_filter:
        print(f"##  Sport group filter: {sport_group_filter}")
    print("#" * 72)

    # ── GLOBAL OVERVIEW ──────────────────────────────────────────────────
    big_header("GLOBAL OVERVIEW")
    overview_data = section_overview(conn, args.since)
    league_data = section_per_league(conn, args.since)

    # ── SPORT GROUP OVERVIEW ─────────────────────────────────────────────
    sport_group_data = section_per_sport_group(conn, args.since)
    sport_group_cal = section_sport_group_calibration(conn, args.since)
    cal_engine_obs = section_cal_engine_obs(conn, args.since)

    # ── PER-SPORT DETAILED ANALYSIS ─────────────────────────────────────
    leagues = get_leagues_with_signals(conn, args.since)

    # If --sport-group filter, only show leagues in that group
    if sport_group_filter:
        # Query which leagues belong to this sport group
        try:
            sg_leagues = conn.execute("""
                SELECT DISTINCT league FROM sports_shadow_log
                WHERE sport_group = ? AND signal_fired=1
            """, (sport_group_filter,)).fetchall()
            sg_league_names = {r['league'] for r in sg_leagues}
            leagues = [l for l in leagues if l in sg_league_names]
        except Exception:
            pass  # sport_group column may not exist yet

    per_sport_data = {}
    settlement_data_per_sport = {}

    for league_name in leagues:
        big_header(f"{league_name} DETAILED ANALYSIS")

        sport_data = {}
        section_per_game(conn, args.since, league=league_name)
        sport_data["filter_stages"] = section_filter_stages(conn, args.since, league=league_name)
        sport_data["signal_quality"] = section_signal_quality(conn, args.since, league=league_name)
        sport_data["price_buckets"] = section_price_buckets(conn, args.since, league=league_name)
        sport_data["daily_pnl"] = section_daily_pnl(conn, args.since, league=league_name)
        sport_data["liquidity"] = section_liquidity(conn, args.since, league=league_name)
        sport_data["deficit_time"] = section_deficit_time(conn, args.since, league=league_name)
        sport_data["sprt"] = section_wald_sprt(conn, args.since, league=league_name)
        sport_data["readiness"] = section_readiness(conn, args.since, league=league_name)
        sport_data["counterfactual"] = section_counterfactual(conn, args.since, league=league_name)
        sport_data["lr_scale_ab"] = section_lr_scale_ab(conn, args.since, league=league_name)

        settlement = section_settlement_gaps(conn, args.since, league=league_name)
        sport_data["settlement"] = settlement
        settlement_data_per_sport[league_name] = settlement

        per_sport_data[league_name] = sport_data

    # ── CROSS-SPORT ─────────────────────────────────────────────────────
    big_header("CROSS-SPORT")
    data_quality = section_data_quality(conn, args.since)
    fav_audit = section_fav_id_audit(conn, args.since)
    pregame_data = section_pregame_capture(conn, args.since)

    # Global readiness summary with sig tags
    header("READINESS SUMMARY")
    any_ready = False
    for league_name in leagues:
        rd = per_sport_data[league_name].get("readiness", {})
        passed = rd.get("passed", 0)
        total_chk = rd.get("total", 0)
        all_pass = rd.get("all_pass", False)
        status = "READY" if all_pass else ("CONTINUE COLLECTING" if passed >= 3 else "NOT READY")
        if all_pass:
            any_ready = True
        # Add WR significance
        g, gs, gw = _dedup_game_wr(conn, since=args.since, league=league_name)
        wr = safe_div(gw, gs)
        tag = significance_tag(gs, p=0.5, threshold=wr) if gs > 0 else ""
        print(f"  {league_name:14s}: {passed}/{total_chk} checks pass — {status} "
              f"(WR={pct(gw, gs)} n={gs} {tag})")

    print()
    if any_ready:
        print("  >>> At least one sport is ready for live promotion consideration")
    else:
        print("  >>> NOT READY (requires all checks to pass for at least one sport)")

    # Recommendations
    section_recommendations(conn, args.since,
                            per_sport_data=per_sport_data,
                            settlement_data=settlement_data_per_sport)

    # ── JSON output ─────────────────────────────────────────────────────
    if args.json:
        artifact = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "since": args.since,
            "sport_group_filter": sport_group_filter,
            "overview": overview_data,
            "leagues": league_data,
            "sport_groups": sport_group_data,
            "sport_group_calibration": sport_group_cal,
            "cal_engine_obs": cal_engine_obs,
            "per_sport": {},
            "cross_sport": {
                "data_quality": data_quality,
                "fav_audit_issues": fav_audit.get("issues", []),
                "pregame_capture": pregame_data,
            },
        }
        for league_name, sport_data in per_sport_data.items():
            artifact["per_sport"][league_name] = sport_data

        with open(args.json, "w") as f:
            json.dump(artifact, f, indent=2, default=str)
        print(f"\n  JSON artifact written to: {args.json}")

    print()
    conn.close()


if __name__ == "__main__":
    main()
