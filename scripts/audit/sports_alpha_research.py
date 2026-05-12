#!/usr/bin/env python3
"""
Sports Comeback Alpha Analyzer
Systematic alpha research on sports comeback prediction market data.
Discovers profitable configurations, validates robustness, and identifies
which sport groups / leagues / game situations offer real edge.

Designed to be rerun on updated data — outputs standardized alpha discovery report.

Usage:
    # Run locally against copied DB:
    scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
    python3 scripts/audit/sports_alpha_research.py --db /tmp/state.db

    # Filter to current regime:
    python3 scripts/audit/sports_alpha_research.py --db /tmp/state.db --regime auto

    # Focus on a sport group:
    python3 scripts/audit/sports_alpha_research.py --db /tmp/state.db --sport-group basketball
"""

import argparse
import math
import os
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


# ── Fee Model ─────────────────────────────────────────────────────────────────

def maker_fee_cents(price_cents: float, contracts: int = 1) -> float:
    """Kalshi charges $0 on maker fills."""
    return 0


def taker_fee_cents(price_cents: float, contracts: int = 1) -> float:
    p = price_cents / 100.0
    return math.ceil(0.07 * contracts * p * (1 - p))


def sim_pnl_1lot(price_cents: float, won: bool, is_maker: bool = True) -> float:
    """PnL in dollars for 1-lot at given price."""
    fee = maker_fee_cents(price_cents) if is_maker else taker_fee_cents(price_cents)
    if won:
        return (100 - price_cents - fee) / 100.0
    else:
        return -(price_cents + fee) / 100.0


def breakeven_wr(price_cents: float, is_maker: bool = True) -> float:
    fee = maker_fee_cents(price_cents) if is_maker else taker_fee_cents(price_cents)
    return (price_cents + fee) / 100.0


# ── Statistical Tests ─────────────────────────────────────────────────────────

def wilson_ci(wins: int, total: int, z: float = 1.96) -> Tuple[float, float]:
    if total == 0:
        return 0.0, 0.0
    p = wins / total
    denom = 1 + z ** 2 / total
    center = (p + z ** 2 / (2 * total)) / denom
    spread = z * math.sqrt((p * (1 - p) + z ** 2 / (4 * total)) / total) / denom
    return max(0, center - spread), min(1, center + spread)


def fisher_exact_p(a: int, b: int, c: int, d: int) -> float:
    """One-sided Fisher exact test p-value."""
    from math import comb
    n = a + b + c + d
    r1, c1 = a + b, a + c

    def _hyper(x):
        return comb(r1, x) * comb(n - r1, c1 - x) / comb(n, c1)

    p = sum(_hyper(x) for x in range(a, min(r1, c1) + 1))
    return min(p, 1.0)


def brier_score(probs: List[float], outcomes: List[int]) -> Optional[float]:
    if not probs:
        return None
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / len(probs)


def profit_factor(wins_pnl: float, losses_pnl: float) -> float:
    if losses_pnl == 0:
        return float('inf') if wins_pnl > 0 else 0.0
    return abs(wins_pnl / losses_pnl)


def significance_tag(n: int, observed_rate: float, null_rate: float = 0.5) -> str:
    if n < 5:
        return "[n<5]"
    if n < 15:
        return "[n<15]"
    se = math.sqrt(null_rate * (1 - null_rate) / n)
    if se == 0:
        return "[NOT SIG]"
    z = (observed_rate - null_rate) / se
    if abs(z) >= 2.576:
        return "[SIG p<.01]"
    if abs(z) >= 1.960:
        return "[SIG p<.05]"
    if abs(z) >= 1.645:
        return "[MARGINAL p<.10]"
    return "[NOT SIG]"


def sprt_test(outcomes: List[int], p0: float = 0.50, p1: float = 0.55,
              alpha: float = 0.05, beta: float = 0.10) -> Dict:
    """Wald Sequential Probability Ratio Test."""
    A = math.log((1 - beta) / alpha)
    B = math.log(beta / (1 - alpha))
    llr = 0.0
    n = 0
    decision = "CONTINUE_COLLECTING"
    trajectory = []

    for outcome in outcomes:
        n += 1
        if outcome == 1:
            llr += math.log(p1 / p0)
        else:
            llr += math.log((1 - p1) / (1 - p0))
        trajectory.append(llr)

        if llr >= A:
            decision = "REJECT_H0_EDGE_EXISTS"
            break
        elif llr <= B:
            decision = "ACCEPT_H0_NO_EDGE"
            break

    wins = sum(outcomes[:n])
    return {
        "n": n, "wins": wins, "llr": llr, "decision": decision,
        "upper_boundary": A, "lower_boundary": B,
        "trajectory": trajectory,
    }


# ── Data Loading ──────────────────────────────────────────────────────────────

def connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def load_sports_data(db_path: str, since: Optional[str] = None,
                     sport_group: Optional[str] = None) -> List[Dict]:
    """Load sports_shadow_log data with parsed fields."""
    conn = connect_db(db_path)

    parts = []
    if since:
        parts.append(f"evaluation_time >= '{since}'")
    if sport_group:
        parts.append(f"sport_group = '{sport_group}'")
    where = "WHERE " + " AND ".join(parts) if parts else ""

    rows = conn.execute(f"""
        SELECT * FROM sports_shadow_log {where}
        ORDER BY evaluation_time
    """).fetchall()

    data = []
    for r in rows:
        d = dict(r)
        d['won'] = d.get('fav_won') == 1
        d['settled'] = d.get('fav_won') is not None
        d['is_signal'] = d.get('signal_fired') == 1
        d['ask'] = d.get('yes_ask') or 0
        d['bid'] = d.get('yes_bid') or 0
        d['sprd'] = d.get('spread') or 0
        d['trp'] = d.get('time_remaining_pct') or 0
        d['deficit_val'] = d.get('deficit') or 0
        d['lr'] = d.get('likelihood_ratio') or 0
        d['lr_scale'] = d.get('sport_lr_scale') or 0.2
        d['cp'] = d.get('comeback_prob') or 0
        d['mip'] = d.get('market_implied_prob') or 0
        d['pregame_p'] = d.get('pregame_fav_prob') or 0
        d['edge_val'] = d.get('fee_adjusted_edge') or 0
        d['close_p'] = d.get('closing_price') or 0
        d['sc'] = d.get('score_changed') == 1 if d.get('score_changed') is not None else None
        d['capture'] = d.get('pregame_capture_method') or 'unknown'
        d['sg'] = d.get('sport_group') or 'unknown'
        try:
            d['eval_dt'] = datetime.fromisoformat(
                d['evaluation_time'].replace('Z', '+00:00')
            ) if d.get('evaluation_time') else None
        except Exception:
            d['eval_dt'] = None
        d['date'] = d['eval_dt'].strftime('%Y-%m-%d') if d['eval_dt'] else None
        data.append(d)

    conn.close()
    return data


def dedup_games(rows: List[Dict]) -> List[Dict]:
    """De-duplicate to one outcome per game_id. Uses first signal row per game."""
    seen = {}
    for r in rows:
        gid = r.get('game_id')
        if gid not in seen:
            seen[gid] = r
        else:
            # Keep the one with fav_won populated if possible
            if r['settled'] and not seen[gid]['settled']:
                seen[gid] = r
    return list(seen.values())


# ── Formatting ────────────────────────────────────────────────────────────────

def pct(num: int, denom: int) -> str:
    return f"{num / denom * 100:.1f}%" if denom > 0 else "n/a"


def safe_div(a, b, default=0.0):
    return a / b if b and b > 0 else default


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def subheader(title: str) -> None:
    print(f"\n--- {title} ---")


# ── Analysis Sections ─────────────────────────────────────────────────────────

def section_overview(all_data: List[Dict]) -> None:
    """High-level data summary."""
    header("1. DATA OVERVIEW")

    signals = [r for r in all_data if r['is_signal']]
    settled_sigs = [r for r in signals if r['settled']]
    games = dedup_games(signals)
    settled_games = [g for g in games if g['settled']]
    wins = [g for g in settled_games if g['won']]

    n_leagues = len(set(r.get('league', '') for r in all_data))
    n_groups = len(set(r['sg'] for r in all_data))
    dates = [r['date'] for r in all_data if r['date']]

    print(f"  Total evaluations:     {len(all_data)}")
    print(f"  Signals fired:         {len(signals)} ({pct(len(signals), len(all_data))} rate)")
    print(f"  Settled signals:       {len(settled_sigs)}")
    print(f"  Unique games w/signal: {len(games)}")
    print(f"  Settled games (dedup): {len(settled_games)}")
    print(f"  Game wins:             {len(wins)}/{len(settled_games)} ({pct(len(wins), len(settled_games))})")
    print(f"  Sport groups:          {n_groups}")
    print(f"  Leagues:               {n_leagues}")
    if dates:
        print(f"  Date range:            {min(dates)} to {max(dates)}")

    if settled_games:
        wr = len(wins) / len(settled_games)
        lo, hi = wilson_ci(len(wins), len(settled_games))
        tag = significance_tag(len(settled_games), wr)
        print(f"\n  Overall game WR:       {wr:.1%} (95% CI: [{lo:.1%}, {hi:.1%}]) {tag}")

        # Sim PnL
        total_pnl = 0.0
        for g in settled_games:
            if g['ask'] > 0:
                total_pnl += sim_pnl_1lot(g['ask'], g['won'], is_maker=False)
        print(f"  Sim PnL (1-lot taker): ${total_pnl:.2f}")

    # Verdict
    if len(settled_games) < 20:
        verdict = "INSUFFICIENT DATA"
    elif len(wins) / max(len(settled_games), 1) > 0.55 and total_pnl > 0:
        verdict = "ALPHA SIGNAL DETECTED"
    elif total_pnl > 0:
        verdict = "MARGINAL ALPHA"
    else:
        verdict = "NO ALPHA DETECTED"
    print(f"\n  >>> VERDICT: {verdict}")


def section_sport_group(all_data: List[Dict]) -> None:
    """Performance breakdown by sport_group."""
    header("2. SPORT GROUP ANALYSIS")

    signals = [r for r in all_data if r['is_signal']]
    groups = sorted(set(r['sg'] for r in signals))

    print(f"  {'Group':<14} {'Sigs':>5} {'Games':>5} {'W':>4} {'L':>4} "
          f"{'WR':>7} {'95%CI':>14} {'PnL$':>8} {'PF':>6} {'Brier':>7} {'Tag':<16}")
    print("  " + "-" * 110)

    for g in groups:
        g_sigs = [r for r in signals if r['sg'] == g]
        games = dedup_games(g_sigs)
        settled = [x for x in games if x['settled']]
        wins = [x for x in settled if x['won']]
        losses = [x for x in settled if not x['won']]
        n_w, n_l, n_s = len(wins), len(losses), len(settled)

        wr = safe_div(n_w, n_s)
        lo, hi = wilson_ci(n_w, n_s)
        tag = significance_tag(n_s, wr)

        # PnL
        win_pnl = sum(sim_pnl_1lot(x['ask'], True, False) for x in wins if x['ask'] > 0)
        loss_pnl = sum(sim_pnl_1lot(x['ask'], False, False) for x in losses if x['ask'] > 0)
        total_pnl = win_pnl + loss_pnl
        pf = profit_factor(win_pnl, abs(loss_pnl))
        pf_str = f"{pf:.2f}" if pf < 100 else "inf"

        # Brier
        probs = [x['cp'] for x in settled if x['cp'] > 0]
        outs = [int(x['won']) for x in settled if x['cp'] > 0]
        bs = brier_score(probs, outs)
        bs_str = f"{bs:.4f}" if bs is not None else "n/a"

        ci_str = f"[{lo:.0%},{hi:.0%}]"
        print(f"  {g:<14} {len(g_sigs):>5} {n_s:>5} {n_w:>4} {n_l:>4} "
              f"{pct(n_w, n_s):>7} {ci_str:>14} ${total_pnl:>7.2f} {pf_str:>6} "
              f"{bs_str:>7} {tag:<16}")


def section_league(all_data: List[Dict]) -> None:
    """Performance breakdown by league."""
    header("3. LEAGUE ANALYSIS")

    signals = [r for r in all_data if r['is_signal']]
    leagues = sorted(set(r.get('league', 'unknown') for r in signals))

    print(f"  {'League':<16} {'Group':<12} {'Sigs':>5} {'Games':>5} {'W':>4} {'L':>4} "
          f"{'WR':>7} {'PnL$':>8} {'AvgAsk':>7} {'AvgEdge':>8}")
    print("  " + "-" * 100)

    for lg in leagues:
        lg_sigs = [r for r in signals if r.get('league') == lg]
        sg = lg_sigs[0]['sg'] if lg_sigs else 'unknown'
        games = dedup_games(lg_sigs)
        settled = [x for x in games if x['settled']]
        wins = [x for x in settled if x['won']]
        losses = [x for x in settled if not x['won']]
        n_w, n_l, n_s = len(wins), len(losses), len(settled)

        total_pnl = sum(sim_pnl_1lot(x['ask'], x['won'], False)
                        for x in settled if x['ask'] > 0)

        asks = [x['ask'] for x in lg_sigs if x['ask'] > 0]
        avg_ask = sum(asks) / len(asks) if asks else 0
        edges = [x['edge_val'] for x in lg_sigs if x['edge_val']]
        avg_edge = sum(edges) / len(edges) if edges else 0

        print(f"  {lg:<16} {sg:<12} {len(lg_sigs):>5} {n_s:>5} {n_w:>4} {n_l:>4} "
              f"{pct(n_w, n_s):>7} ${total_pnl:>7.2f} {avg_ask:>6.0f}c "
              f"{avg_edge * 100:>7.1f}%")


def section_deficit(all_data: List[Dict]) -> None:
    """WR by deficit size."""
    header("4. DEFICIT ANALYSIS")

    signals = [r for r in all_data if r['is_signal']]

    buckets = [
        ("1 point", lambda d: d == 1),
        ("2 points", lambda d: d == 2),
        ("3 points", lambda d: d == 3),
        ("4-5 points", lambda d: 4 <= d <= 5),
        ("6-10 points", lambda d: 6 <= d <= 10),
        ("11-15 points", lambda d: 11 <= d <= 15),
        ("16+ points", lambda d: d >= 16),
    ]

    print(f"  {'Deficit':<14} {'Sigs':>5} {'Games':>5} {'W':>4} {'L':>4} "
          f"{'WR':>7} {'PnL$':>8} {'AvgAsk':>7} {'Tag':<16}")
    print("  " + "-" * 90)

    for label, pred in buckets:
        bk = [r for r in signals if pred(r['deficit_val'])]
        if not bk:
            continue
        games = dedup_games(bk)
        settled = [x for x in games if x['settled']]
        wins = [x for x in settled if x['won']]
        n_w, n_s = len(wins), len(settled)
        wr = safe_div(n_w, n_s)
        tag = significance_tag(n_s, wr)

        total_pnl = sum(sim_pnl_1lot(x['ask'], x['won'], False)
                        for x in settled if x['ask'] > 0)
        asks = [x['ask'] for x in bk if x['ask'] > 0]
        avg_ask = sum(asks) / len(asks) if asks else 0

        print(f"  {label:<14} {len(bk):>5} {n_s:>5} {n_w:>4} {n_s - n_w:>4} "
              f"{pct(n_w, n_s):>7} ${total_pnl:>7.2f} {avg_ask:>6.0f}c {tag:<16}")


def section_time_remaining(all_data: List[Dict]) -> None:
    """WR by time_remaining_pct."""
    header("5. TIME REMAINING ANALYSIS")

    signals = [r for r in all_data if r['is_signal']]

    buckets = [
        ("<25%", 0, 0.25),
        ("25-40%", 0.25, 0.40),
        ("40-55%", 0.40, 0.55),
        ("55-70%", 0.55, 0.70),
        ("70-85%", 0.70, 0.85),
        (">85%", 0.85, 1.01),
    ]

    print(f"  {'TimeRemain':<12} {'Sigs':>5} {'Games':>5} {'W':>4} {'L':>4} "
          f"{'WR':>7} {'PnL$':>8} {'AvgDef':>7} {'AvgAsk':>7} {'Tag':<16}")
    print("  " + "-" * 100)

    for label, lo, hi in buckets:
        bk = [r for r in signals if lo <= r['trp'] < hi]
        if not bk:
            continue
        games = dedup_games(bk)
        settled = [x for x in games if x['settled']]
        wins = [x for x in settled if x['won']]
        n_w, n_s = len(wins), len(settled)
        wr = safe_div(n_w, n_s)
        tag = significance_tag(n_s, wr)

        total_pnl = sum(sim_pnl_1lot(x['ask'], x['won'], False)
                        for x in settled if x['ask'] > 0)
        avg_def = sum(x['deficit_val'] for x in bk) / len(bk)
        asks = [x['ask'] for x in bk if x['ask'] > 0]
        avg_ask = sum(asks) / len(asks) if asks else 0

        print(f"  {label:<12} {len(bk):>5} {n_s:>5} {n_w:>4} {n_s - n_w:>4} "
              f"{pct(n_w, n_s):>7} ${total_pnl:>7.2f} {avg_def:>6.1f} "
              f"{avg_ask:>6.0f}c {tag:<16}")


def section_pregame_strength(all_data: List[Dict]) -> None:
    """Does pregame_fav_prob predict comeback success?"""
    header("6. PREGAME FAVORITE STRENGTH")

    signals = [r for r in all_data if r['is_signal']]

    buckets = [
        ("50-55%", 0.50, 0.55),
        ("55-60%", 0.55, 0.60),
        ("60-65%", 0.60, 0.65),
        ("65-70%", 0.65, 0.70),
        ("70-80%", 0.70, 0.80),
        ("80%+", 0.80, 1.01),
    ]

    print(f"  {'PregameProb':<12} {'Sigs':>5} {'Games':>5} {'W':>4} {'L':>4} "
          f"{'WR':>7} {'PnL$':>8} {'AvgAsk':>7} {'Tag':<16}")
    print("  " + "-" * 90)

    for label, lo, hi in buckets:
        bk = [r for r in signals if lo <= r['pregame_p'] < hi]
        if not bk:
            continue
        games = dedup_games(bk)
        settled = [x for x in games if x['settled']]
        wins = [x for x in settled if x['won']]
        n_w, n_s = len(wins), len(settled)
        wr = safe_div(n_w, n_s)
        tag = significance_tag(n_s, wr)

        total_pnl = sum(sim_pnl_1lot(x['ask'], x['won'], False)
                        for x in settled if x['ask'] > 0)
        asks = [x['ask'] for x in bk if x['ask'] > 0]
        avg_ask = sum(asks) / len(asks) if asks else 0

        print(f"  {label:<12} {len(bk):>5} {n_s:>5} {n_w:>4} {n_s - n_w:>4} "
              f"{pct(n_w, n_s):>7} ${total_pnl:>7.2f} {avg_ask:>6.0f}c {tag:<16}")

    # Correlation check
    settled_sigs = [r for r in signals if r['settled'] and r['pregame_p'] > 0]
    if len(settled_sigs) >= 10:
        games = dedup_games(settled_sigs)
        settled_games = [g for g in games if g['settled']]
        xs = [g['pregame_p'] for g in settled_games]
        ys = [int(g['won']) for g in settled_games]
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n
        sx = math.sqrt(sum((x - mx) ** 2 for x in xs) / n)
        sy = math.sqrt(sum((y - my) ** 2 for y in ys) / n)
        r_val = cov / (sx * sy) if sx > 0 and sy > 0 else 0
        print(f"\n  Correlation(pregame_prob, fav_won): r = {r_val:.3f} (n={n})")
        if abs(r_val) < 0.1:
            print("  --> Weak: pregame strength does NOT predict comeback success")
        elif r_val > 0.2:
            print("  --> Positive: stronger favorites DO come back more often")
        elif r_val < -0.2:
            print("  --> Negative: stronger favorites come back LESS (surprising)")


def section_lr_scale(all_data: List[Dict]) -> None:
    """Impact of sport_lr_scale on signal quality."""
    header("7. LR SCALE ANALYSIS")

    signals = [r for r in all_data if r['is_signal']]
    scales = sorted(set(r['lr_scale'] for r in signals if r['lr_scale'] > 0))

    if not scales:
        print("  No sport_lr_scale data available.")
        return

    print(f"  {'LR_Scale':<10} {'Sigs':>5} {'Games':>5} {'W':>4} {'L':>4} "
          f"{'WR':>7} {'AvgPosterior':>13} {'AvgEdge':>8} {'PnL$':>8}")
    print("  " + "-" * 85)

    for s in scales:
        bk = [r for r in signals if r['lr_scale'] == s]
        games = dedup_games(bk)
        settled = [x for x in games if x['settled']]
        wins = [x for x in settled if x['won']]
        n_w, n_s = len(wins), len(settled)

        total_pnl = sum(sim_pnl_1lot(x['ask'], x['won'], False)
                        for x in settled if x['ask'] > 0)
        avg_post = sum(x['cp'] for x in bk if x['cp'] > 0) / max(1, sum(1 for x in bk if x['cp'] > 0))
        avg_edge = sum(x['edge_val'] for x in bk if x['edge_val']) / max(1, sum(1 for x in bk if x['edge_val']))

        print(f"  {s:<10.3f} {len(bk):>5} {n_s:>5} {n_w:>4} {n_s - n_w:>4} "
              f"{pct(n_w, n_s):>7} {avg_post:>12.1%} {avg_edge * 100:>7.1f}% "
              f"${total_pnl:>7.2f}")


def section_price_analysis(all_data: List[Dict]) -> None:
    """WR by yes_ask price band with breakeven comparison."""
    header("8. PRICE ANALYSIS")

    signals = [r for r in all_data if r['is_signal'] and r['ask'] > 0]

    buckets = [
        ("1-19c", 1, 20),
        ("20-34c", 20, 35),
        ("35-49c", 35, 50),
        ("50-64c", 50, 65),
        ("65-79c", 65, 80),
        ("80-99c", 80, 100),
    ]

    print(f"  {'Bucket':<10} {'Sigs':>5} {'Games':>5} {'W':>4} {'L':>4} "
          f"{'WR':>7} {'BEwr':>7} {'Edge?':>6} {'PnL$':>8} {'PF':>6} {'Tag':<16}")
    print("  " + "-" * 100)

    for label, lo, hi in buckets:
        bk = [r for r in signals if lo <= r['ask'] < hi]
        if not bk:
            continue
        games = dedup_games(bk)
        settled = [x for x in games if x['settled']]
        wins = [x for x in settled if x['won']]
        losses = [x for x in settled if not x['won']]
        n_w, n_s = len(wins), len(settled)
        wr = safe_div(n_w, n_s)
        tag = significance_tag(n_s, wr)

        avg_ask = sum(x['ask'] for x in bk) / len(bk)
        be = breakeven_wr(avg_ask, is_maker=False)
        has_edge = "YES" if wr > be and n_s >= 5 else ("---" if n_s < 5 else "NO")

        win_pnl = sum(sim_pnl_1lot(x['ask'], True, False) for x in wins if x['ask'] > 0)
        loss_pnl = sum(sim_pnl_1lot(x['ask'], False, False) for x in losses if x['ask'] > 0)
        total_pnl = win_pnl + loss_pnl
        pf = profit_factor(win_pnl, abs(loss_pnl))
        pf_str = f"{pf:.2f}" if pf < 100 else "inf"

        print(f"  {label:<10} {len(bk):>5} {n_s:>5} {n_w:>4} {n_s - n_w:>4} "
              f"{pct(n_w, n_s):>7} {be:>6.1%} {has_edge:>6} ${total_pnl:>7.2f} "
              f"{pf_str:>6} {tag:<16}")


def section_signal_counterfactual(all_data: List[Dict]) -> None:
    """Compare signal_fired vs would_signal counterfactuals."""
    header("9. SIGNAL QUALITY / COUNTERFACTUAL")

    thresholds = [
        ("Live (current)", lambda r: r['is_signal']),
        ("would_signal_50c", lambda r: r.get('would_signal_50c') == 1),
        ("would_signal_60c", lambda r: r.get('would_signal_60c') == 1),
        ("would_signal_70c", lambda r: r.get('would_signal_70c') == 1),
        ("would_signal_80c", lambda r: r.get('would_signal_80c') == 1),
        ("would_signal_pregame_55", lambda r: r.get('would_signal_pregame_55') == 1),
        ("would_signal_pregame_65", lambda r: r.get('would_signal_pregame_65') == 1),
    ]

    print(f"  {'Threshold':<28} {'Sigs':>6} {'Games':>5} {'W':>4} {'L':>4} "
          f"{'WR':>7} {'PnL$':>8} {'Tag':<16}")
    print("  " + "-" * 95)

    for label, pred in thresholds:
        sigs = [r for r in all_data if pred(r)]
        if not sigs:
            continue
        games = dedup_games(sigs)
        settled = [x for x in games if x['settled']]
        wins = [x for x in settled if x['won']]
        n_w, n_s = len(wins), len(settled)
        wr = safe_div(n_w, n_s)
        tag = significance_tag(n_s, wr)

        total_pnl = sum(sim_pnl_1lot(x['ask'], x['won'], False)
                        for x in settled if x['ask'] > 0)

        print(f"  {label:<28} {len(sigs):>6} {n_s:>5} {n_w:>4} {n_s - n_w:>4} "
              f"{pct(n_w, n_s):>7} ${total_pnl:>7.2f} {tag:<16}")


def section_pregame_capture(all_data: List[Dict]) -> None:
    """Impact of pregame capture method on accuracy."""
    header("10. PREGAME CAPTURE METHOD")

    signals = [r for r in all_data if r['is_signal']]
    methods = sorted(set(r['capture'] for r in signals))

    print(f"  {'Method':<20} {'Sigs':>5} {'Games':>5} {'W':>4} {'L':>4} "
          f"{'WR':>7} {'AvgPregame':>11} {'PnL$':>8}")
    print("  " + "-" * 80)

    for m in methods:
        bk = [r for r in signals if r['capture'] == m]
        games = dedup_games(bk)
        settled = [x for x in games if x['settled']]
        wins = [x for x in settled if x['won']]
        n_w, n_s = len(wins), len(settled)

        total_pnl = sum(sim_pnl_1lot(x['ask'], x['won'], False)
                        for x in settled if x['ask'] > 0)
        avg_p = sum(x['pregame_p'] for x in bk if x['pregame_p'] > 0) / max(
            1, sum(1 for x in bk if x['pregame_p'] > 0))

        print(f"  {m:<20} {len(bk):>5} {n_s:>5} {n_w:>4} {n_s - n_w:>4} "
              f"{pct(n_w, n_s):>7} {avg_p:>10.1%} ${total_pnl:>7.2f}")


def section_sprt(all_data: List[Dict]) -> None:
    """Sequential Probability Ratio Test for convergence."""
    header("11. SPRT SEQUENTIAL TEST")

    signals = [r for r in all_data if r['is_signal']]
    games = dedup_games(signals)
    settled = sorted([g for g in games if g['settled']],
                     key=lambda x: x.get('evaluation_time', ''))

    if not settled:
        print("  No settled games for sequential test.")
        return

    outcomes = [int(g['won']) for g in settled]

    # Overall test
    result = sprt_test(outcomes)
    print(f"  Games tested:     {result['n']}")
    print(f"  Wins:             {result['wins']} ({pct(result['wins'], result['n'])})")
    print(f"  Log LR:           {result['llr']:.3f}")
    print(f"  Boundaries:       reject H0 at {result['upper_boundary']:.3f}, "
          f"accept H0 at {result['lower_boundary']:.3f}")
    print(f"  Decision:         {result['decision']}")

    # Show trajectory milestones
    if result['trajectory']:
        subheader("TRAJECTORY MILESTONES")
        milestones = [5, 10, 20, 30, 50, 75, 100]
        for m in milestones:
            if m <= len(result['trajectory']):
                llr_at = result['trajectory'][m - 1]
                wins_at = sum(outcomes[:m])
                print(f"  After {m:>3} games: LLR={llr_at:+.3f}  WR={pct(wins_at, m)}")

    # Per sport group SPRT
    subheader("PER SPORT GROUP SPRT")
    groups = sorted(set(r['sg'] for r in signals))
    for g in groups:
        g_sigs = [r for r in signals if r['sg'] == g]
        g_games = dedup_games(g_sigs)
        g_settled = sorted([x for x in g_games if x['settled']],
                           key=lambda x: x.get('evaluation_time', ''))
        if len(g_settled) < 3:
            continue
        g_outcomes = [int(x['won']) for x in g_settled]
        g_result = sprt_test(g_outcomes)
        print(f"  {g:<14} n={g_result['n']:>3} wins={g_result['wins']:>3} "
              f"LLR={g_result['llr']:+.3f} --> {g_result['decision']}")


def section_calibration(all_data: List[Dict]) -> None:
    """Calibration: comeback_prob vs actual fav_won rate."""
    header("12. CALIBRATION ANALYSIS")

    signals = [r for r in all_data if r['is_signal']]
    games = dedup_games(signals)
    settled = [g for g in games if g['settled'] and g['cp'] > 0]

    if not settled:
        print("  No settled games with model probability.")
        return

    # Calibration buckets
    buckets = [
        ("30-40%", 0.30, 0.40),
        ("40-50%", 0.40, 0.50),
        ("50-60%", 0.50, 0.60),
        ("60-70%", 0.60, 0.70),
        ("70-80%", 0.70, 0.80),
        ("80-90%", 0.80, 0.90),
        ("90%+", 0.90, 1.01),
    ]

    print(f"  {'Predicted':<12} {'Games':>5} {'W':>4} {'L':>4} "
          f"{'ActualWR':>9} {'AvgPred':>9} {'Gap':>8} {'Status':<16}")
    print("  " + "-" * 80)

    all_probs, all_outs = [], []
    for label, lo, hi in buckets:
        bk = [g for g in settled if lo <= g['cp'] < hi]
        if not bk:
            continue
        n_w = sum(1 for g in bk if g['won'])
        n_s = len(bk)
        wr = n_w / n_s
        avg_pred = sum(g['cp'] for g in bk) / n_s
        gap = (avg_pred - wr) * 100

        all_probs.extend([g['cp'] for g in bk])
        all_outs.extend([int(g['won']) for g in bk])

        if n_s >= 5:
            if gap > 15:
                status = "OVERCONFIDENT"
            elif gap < -15:
                status = "UNDERCONFIDENT"
            elif abs(gap) <= 5:
                status = "well-calibrated"
            else:
                status = "slight bias"
        else:
            status = f"n<5"

        print(f"  {label:<12} {n_s:>5} {n_w:>4} {n_s - n_w:>4} "
              f"{wr:>8.1%} {avg_pred:>8.1%} {gap:>+7.1f}pp {status:<16}")

    # Overall Brier
    if all_probs:
        model_brier = brier_score(all_probs, all_outs)
        print(f"\n  Model Brier score: {model_brier:.4f}")

        # Market Brier for comparison
        mkt_settled = [g for g in settled if g['ask'] > 0]
        if mkt_settled:
            mkt_probs = [g['ask'] / 100.0 for g in mkt_settled]
            mkt_outs = [int(g['won']) for g in mkt_settled]
            mkt_brier = brier_score(mkt_probs, mkt_outs)
            print(f"  Market Brier score: {mkt_brier:.4f}")
            if model_brier < mkt_brier:
                print(f"  --> Model beats market by {mkt_brier - model_brier:.4f}")
            else:
                print(f"  --> Market beats model by {model_brier - mkt_brier:.4f}")

        # Naive baseline (always predict 50%)
        naive_brier = sum((0.5 - o) ** 2 for o in all_outs) / len(all_outs)
        print(f"  Naive (50%) Brier: {naive_brier:.4f}")


def section_game_flow(all_data: List[Dict]) -> None:
    """Score change impact, period/clock patterns."""
    header("13. GAME FLOW ANALYSIS")

    signals = [r for r in all_data if r['is_signal']]

    # Score changed impact
    subheader("SCORE CHANGE IMPACT")
    sc_yes = [r for r in signals if r['sc'] is True]
    sc_no = [r for r in signals if r['sc'] is False]

    print(f"  {'Score Changed?':<16} {'Sigs':>5} {'Games':>5} {'W':>4} {'L':>4} "
          f"{'WR':>7} {'PnL$':>8}")
    print("  " + "-" * 65)

    for label, subset in [("Yes", sc_yes), ("No", sc_no)]:
        if not subset:
            continue
        games = dedup_games(subset)
        settled = [x for x in games if x['settled']]
        wins = [x for x in settled if x['won']]
        n_w, n_s = len(wins), len(settled)
        total_pnl = sum(sim_pnl_1lot(x['ask'], x['won'], False)
                        for x in settled if x['ask'] > 0)
        print(f"  {label:<16} {len(subset):>5} {n_s:>5} {n_w:>4} {n_s - n_w:>4} "
              f"{pct(n_w, n_s):>7} ${total_pnl:>7.2f}")

    # Fisher test if both have data
    if sc_yes and sc_no:
        g_yes = dedup_games(sc_yes)
        g_no = dedup_games(sc_no)
        s_yes = [x for x in g_yes if x['settled']]
        s_no = [x for x in g_no if x['settled']]
        w_yes = sum(1 for x in s_yes if x['won'])
        w_no = sum(1 for x in s_no if x['won'])
        l_yes = len(s_yes) - w_yes
        l_no = len(s_no) - w_no
        if len(s_yes) >= 3 and len(s_no) >= 3:
            p = fisher_exact_p(w_yes, l_yes, w_no, l_no)
            print(f"\n  Fisher test (score_changed YES vs NO): p = {p:.4f}")

    # Period analysis
    subheader("PERIOD DISTRIBUTION")
    periods = sorted(set(str(r.get('period', 'unknown')) for r in signals))

    print(f"  {'Period':<12} {'Sigs':>5} {'Games':>5} {'W':>4} {'L':>4} "
          f"{'WR':>7} {'PnL$':>8}")
    print("  " + "-" * 60)

    for p in periods:
        bk = [r for r in signals if str(r.get('period', 'unknown')) == p]
        games = dedup_games(bk)
        settled = [x for x in games if x['settled']]
        wins = [x for x in settled if x['won']]
        n_w, n_s = len(wins), len(settled)
        total_pnl = sum(sim_pnl_1lot(x['ask'], x['won'], False)
                        for x in settled if x['ask'] > 0)
        print(f"  {p:<12} {len(bk):>5} {n_s:>5} {n_w:>4} {n_s - n_w:>4} "
              f"{pct(n_w, n_s):>7} ${total_pnl:>7.2f}")


def section_robustness(all_data: List[Dict]) -> None:
    """Wilson CI, per-sport Fisher tests, time stability."""
    header("14. ROBUSTNESS ANALYSIS")

    signals = [r for r in all_data if r['is_signal']]
    games = dedup_games(signals)
    settled = sorted([g for g in games if g['settled']],
                     key=lambda x: x.get('evaluation_time', ''))

    if len(settled) < 6:
        print("  Insufficient data for robustness analysis (need 6+ settled games).")
        return

    # Wilson CIs per sport group
    subheader("WILSON CONFIDENCE INTERVALS BY SPORT GROUP")
    groups = sorted(set(r['sg'] for r in signals))

    print(f"  {'Group':<14} {'Games':>5} {'W':>4} {'L':>4} "
          f"{'WR':>7} {'95%CI':>14} {'CI Width':>8} {'Robust?':<10}")
    print("  " + "-" * 75)

    for g in groups:
        g_sigs = [r for r in signals if r['sg'] == g]
        g_games = dedup_games(g_sigs)
        g_settled = [x for x in g_games if x['settled']]
        g_wins = [x for x in g_settled if x['won']]
        n_w, n_s = len(g_wins), len(g_settled)
        if n_s < 3:
            continue
        wr = n_w / n_s
        lo, hi = wilson_ci(n_w, n_s)
        width = hi - lo
        # Robust if lower CI > 0.50 (better than coin flip)
        robust = "YES" if lo > 0.50 else ("MAYBE" if lo > 0.40 else "NO")

        print(f"  {g:<14} {n_s:>5} {n_w:>4} {n_s - n_w:>4} "
              f"{wr:>6.1%} [{lo:.0%},{hi:.0%}]{' ' * (14 - 10)} "
              f"{width:>7.0%} {robust:<10}")

    # Time stability: first half vs second half
    subheader("TIME STABILITY (first half vs second half)")

    mid = len(settled) // 2
    h1 = settled[:mid]
    h2 = settled[mid:]
    w1 = sum(1 for g in h1 if g['won'])
    w2 = sum(1 for g in h2 if g['won'])
    n1, n2 = len(h1), len(h2)

    wr1 = safe_div(w1, n1)
    wr2 = safe_div(w2, n2)
    drift = abs(wr1 - wr2)

    print(f"  First half:  {w1}W/{n1 - w1}L  WR={pct(w1, n1)} (n={n1})")
    print(f"  Second half: {w2}W/{n2 - w2}L  WR={pct(w2, n2)} (n={n2})")
    print(f"  Drift:       {drift:.1%}")
    if drift < 0.10:
        print(f"  --> STABLE (drift < 10pp)")
    elif drift < 0.20:
        print(f"  --> MODERATE drift ({drift:.0%})")
    else:
        print(f"  --> UNSTABLE (drift > 20pp) -- regime shift likely")

    # Fisher H1 vs H2
    if n1 >= 3 and n2 >= 3:
        p = fisher_exact_p(w1, n1 - w1, w2, n2 - w2)
        print(f"  Fisher H1 vs H2: p = {p:.4f}")

    # Per-sport Fisher tests vs overall
    subheader("PER-SPORT FISHER TESTS (each sport vs rest)")

    total_w = sum(1 for g in settled if g['won'])
    total_n = len(settled)

    for g in groups:
        g_sigs = [r for r in signals if r['sg'] == g]
        g_games = dedup_games(g_sigs)
        g_settled = [x for x in g_games if x['settled']]
        if len(g_settled) < 3:
            continue
        g_w = sum(1 for x in g_settled if x['won'])
        g_n = len(g_settled)
        rest_w = total_w - g_w
        rest_n = total_n - g_n
        if rest_n < 3:
            continue
        p = fisher_exact_p(g_w, g_n - g_w, rest_w, rest_n - rest_w)
        wr_g = g_w / g_n
        wr_r = rest_w / rest_n
        label = "BETTER" if wr_g > wr_r else "WORSE"
        print(f"  {g:<14} WR={wr_g:.1%} vs rest={wr_r:.1%}  Fisher p={p:.4f}  [{label}]")

    # Daily PnL consistency
    subheader("DAILY PNL CONSISTENCY")
    daily = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
    for g in settled:
        d = g.get('date', 'unknown')
        pnl = sim_pnl_1lot(g['ask'], g['won'], False) if g['ask'] > 0 else 0
        daily[d]["pnl"] += pnl
        daily[d]["trades"] += 1
        daily[d]["wins"] += int(g['won'])

    pos_days = sum(1 for d in daily.values() if d["pnl"] > 0)
    neg_days = sum(1 for d in daily.values() if d["pnl"] < 0)
    zero_days = sum(1 for d in daily.values() if d["pnl"] == 0)
    total_days = len(daily)

    print(f"  Trading days:  {total_days}")
    print(f"  Positive days: {pos_days} ({pct(pos_days, total_days)})")
    print(f"  Negative days: {neg_days} ({pct(neg_days, total_days)})")
    if zero_days:
        print(f"  Zero days:     {zero_days}")

    if total_days >= 3:
        pnls = sorted(daily.values(), key=lambda x: x["pnl"])
        worst = pnls[0]
        best = pnls[-1]
        worst_date = [d for d, v in daily.items() if v is worst][0]
        best_date = [d for d, v in daily.items() if v is best][0]
        print(f"  Worst day:     ${worst['pnl']:.2f} ({worst_date})")
        print(f"  Best day:      ${best['pnl']:.2f} ({best_date})")


def section_optimal_config(all_data: List[Dict]) -> None:
    """Grid search for optimal entry criteria."""
    header("15. OPTIMAL CONFIGURATION SEARCH")

    signals = [r for r in all_data if r['is_signal']]
    games = dedup_games(signals)
    settled = [g for g in games if g['settled']]

    if len(settled) < 10:
        print("  Insufficient data for config optimization (need 10+ settled games).")
        return

    # Grid dimensions
    price_thresholds = [20, 30, 40, 50, 60, 70]
    pregame_thresholds = [0.50, 0.55, 0.60, 0.65, 0.70]
    trp_ranges = [(0, 1.0), (0.25, 1.0), (0.40, 1.0), (0, 0.60), (0.25, 0.75)]

    results = []

    for max_price in price_thresholds:
        for min_pregame in pregame_thresholds:
            for trp_lo, trp_hi in trp_ranges:
                filtered = [g for g in settled
                            if g['ask'] > 0 and g['ask'] <= max_price
                            and g['pregame_p'] >= min_pregame
                            and trp_lo <= g['trp'] <= trp_hi]
                if len(filtered) < 5:
                    continue
                n_w = sum(1 for g in filtered if g['won'])
                n_s = len(filtered)
                wr = n_w / n_s
                total_pnl = sum(sim_pnl_1lot(g['ask'], g['won'], False) for g in filtered)
                win_pnl = sum(sim_pnl_1lot(g['ask'], True, False) for g in filtered if g['won'])
                loss_pnl = sum(sim_pnl_1lot(g['ask'], False, False) for g in filtered if not g['won'])
                pf = profit_factor(win_pnl, abs(loss_pnl))
                lo_ci, _ = wilson_ci(n_w, n_s)

                results.append({
                    "max_price": max_price,
                    "min_pregame": min_pregame,
                    "trp_range": f"{trp_lo:.0%}-{trp_hi:.0%}",
                    "n": n_s, "wins": n_w, "wr": wr,
                    "pnl": total_pnl, "pf": pf, "ci_lo": lo_ci,
                })

    if not results:
        print("  No configurations had 5+ games. Need more data.")
        return

    # Sort by PnL, show top 15
    results.sort(key=lambda x: x['pnl'], reverse=True)

    subheader("TOP 15 CONFIGURATIONS BY PNL")
    print(f"  {'MaxPrice':>8} {'MinPreg':>8} {'TRP':>10} {'N':>4} {'W':>3} {'L':>3} "
          f"{'WR':>7} {'PnL$':>8} {'PF':>6} {'CI_lo':>6}")
    print("  " + "-" * 80)

    for r in results[:15]:
        pf_str = f"{r['pf']:.2f}" if r['pf'] < 100 else "inf"
        print(f"  {r['max_price']:>7}c {r['min_pregame']:>7.0%} {r['trp_range']:>10} "
              f"{r['n']:>4} {r['wins']:>3} {r['n'] - r['wins']:>3} "
              f"{r['wr']:>6.1%} ${r['pnl']:>7.2f} {pf_str:>6} {r['ci_lo']:>5.0%}")

    # Robust configs: PnL > 0, CI_lo > 0.50, PF > 1.2
    robust = [r for r in results if r['pnl'] > 0 and r['ci_lo'] > 0.50 and r['pf'] > 1.2]
    if robust:
        subheader(f"ROBUST CONFIGS ({len(robust)} found: PnL>0, CI_lo>50%, PF>1.2)")
        for r in robust[:10]:
            pf_str = f"{r['pf']:.2f}" if r['pf'] < 100 else "inf"
            print(f"  P<={r['max_price']}c  Preg>={r['min_pregame']:.0%}  "
                  f"TRP={r['trp_range']}  n={r['n']}  "
                  f"WR={r['wr']:.1%}  PnL=${r['pnl']:.2f}  PF={pf_str}")
    else:
        print("\n  >>> NO ROBUST CONFIGS FOUND (need PnL>0, CI_lo>50%, PF>1.2)")


def section_clv(all_data: List[Dict]) -> None:
    """Closing Line Value analysis."""
    header("16. CLOSING LINE VALUE (CLV)")

    signals = [r for r in all_data if r['is_signal'] and r['ask'] > 0 and r['close_p'] > 0]

    if not signals:
        print("  No signals with both yes_ask and closing_price data.")
        return

    games = dedup_games(signals)
    settled = [g for g in games if g['settled']]

    clvs = [g['close_p'] - g['ask'] for g in settled]
    pos_clv = sum(1 for c in clvs if c > 0)

    print(f"  Signals with CLV data: {len(settled)}")
    print(f"  CLV positive:          {pos_clv}/{len(clvs)} ({pct(pos_clv, len(clvs))})")
    if clvs:
        avg = sum(clvs) / len(clvs)
        print(f"  Average CLV:           {avg:+.1f}c")
        print(f"  Median CLV:            {sorted(clvs)[len(clvs) // 2]:+.1f}c")

        # CLV by sport group
        subheader("CLV BY SPORT GROUP")
        groups = sorted(set(g['sg'] for g in settled))
        for sg in groups:
            sg_games = [g for g in settled if g['sg'] == sg]
            sg_clvs = [g['close_p'] - g['ask'] for g in sg_games]
            avg_clv = sum(sg_clvs) / len(sg_clvs)
            pos = sum(1 for c in sg_clvs if c > 0)
            print(f"  {sg:<14} avg CLV={avg_clv:+.1f}c  pos={pos}/{len(sg_clvs)}  "
                  f"n={len(sg_games)}")


def section_summary(all_data: List[Dict]) -> None:
    """Executive summary with verdict and key findings."""
    header("EXECUTIVE SUMMARY")

    signals = [r for r in all_data if r['is_signal']]
    games = dedup_games(signals)
    settled = [g for g in games if g['settled']]
    wins = [g for g in settled if g['won']]

    if not settled:
        print("  No settled data. Cannot produce summary.")
        return

    n_w, n_s = len(wins), len(settled)
    wr = n_w / n_s
    lo_ci, hi_ci = wilson_ci(n_w, n_s)

    total_pnl = sum(sim_pnl_1lot(g['ask'], g['won'], False)
                    for g in settled if g['ask'] > 0)

    outcomes = [int(g['won']) for g in sorted(settled, key=lambda x: x.get('evaluation_time', ''))]
    sprt = sprt_test(outcomes)

    # Determine overall verdict
    checks = {
        "sample_size": n_s >= 30,
        "wr_above_50": wr > 0.50,
        "wr_above_55": wr > 0.55,
        "positive_pnl": total_pnl > 0,
        "ci_lo_above_50": lo_ci > 0.50,
        "sprt_edge": sprt['decision'] == "REJECT_H0_EDGE_EXISTS",
    }

    passed = sum(checks.values())
    total = len(checks)

    if passed >= 5:
        verdict = "STRONG ALPHA"
    elif passed >= 4:
        verdict = "ALPHA DETECTED"
    elif passed >= 3 and total_pnl > 0:
        verdict = "MARGINAL ALPHA"
    elif n_s < 30:
        verdict = "INSUFFICIENT DATA"
    else:
        verdict = "NO ALPHA"

    print(f"\n  VERDICT: {verdict}")
    print()
    print(f"  Overall WR:          {wr:.1%} ({n_w}W/{n_s - n_w}L, n={n_s})")
    print(f"  95% Wilson CI:       [{lo_ci:.1%}, {hi_ci:.1%}]")
    print(f"  Sim PnL (1-lot):     ${total_pnl:.2f}")
    print(f"  SPRT decision:       {sprt['decision']}")
    print()

    print("  Checklist:")
    for name, passed_val in checks.items():
        sym = "+" if passed_val else "X"
        print(f"    [{sym}] {name}")

    print(f"\n  Score: {passed}/{total}")

    # Sport group ranking
    subheader("SPORT GROUP RANKING (by PnL)")
    groups = sorted(set(r['sg'] for r in signals))
    group_results = []
    for g in groups:
        g_sigs = [r for r in signals if r['sg'] == g]
        g_games = dedup_games(g_sigs)
        g_settled = [x for x in g_games if x['settled']]
        g_wins = [x for x in g_settled if x['won']]
        g_pnl = sum(sim_pnl_1lot(x['ask'], x['won'], False)
                     for x in g_settled if x['ask'] > 0)
        group_results.append((g, len(g_settled), len(g_wins), g_pnl))

    group_results.sort(key=lambda x: x[3], reverse=True)
    for g, n, w, pnl in group_results:
        status = "PROFITABLE" if pnl > 0 and n >= 5 else ("MARGINAL" if pnl > -0.50 else "LOSING")
        print(f"  {g:<14} {w}W/{n - w}L  WR={pct(w, n)}  PnL=${pnl:.2f}  [{status}]")

    # Key findings
    subheader("KEY FINDINGS")
    if total_pnl > 0:
        print("  [+] Overall positive simulated PnL")
    else:
        print("  [-] Overall negative simulated PnL")

    if lo_ci > 0.50:
        print("  [+] Lower CI bound above 50% -- statistically significant edge")
    else:
        print("  [-] Lower CI bound below 50% -- edge not yet statistically significant")

    profitable_groups = [g for g, n, w, pnl in group_results if pnl > 0 and n >= 5]
    if profitable_groups:
        print(f"  [+] Profitable sport groups: {', '.join(profitable_groups)}")

    losing_groups = [g for g, n, w, pnl in group_results if pnl < -1.0 and n >= 5]
    if losing_groups:
        print(f"  [-] Losing sport groups: {', '.join(losing_groups)}")


# ── Regime Detection ──────────────────────────────────────────────────────────

def detect_regime_start() -> str:
    """Auto-detect regime start from git history."""
    REGIME_CONSTANTS = [
        "CONSERVATIVE_LR_SCALE", "MAX_MODEL_MARKET_GAP",
        "BINARY_ENTRY_CRITERIA", "THREE_WAY_ENTRY_CRITERIA",
        "BINARY_LR_TABLE", "THREE_WAY_LR_TABLE",
        "SPORT_GROUPS", "LEAGUES",
    ]

    # Bit 11.2 (2026-05-12): relocated to scripts/audit/; 3-level dirname.
    repo_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        result = subprocess.run(
            ["git", "log", "--format=%H %aI", "--since=30 days ago",
             "--", "bot/engines/sports_data.py", "sports_data.py", "bot/engines/sports_engine.py", "sports_engine.py"],  # Sprint 10.1a/d (2026-05-11): include both pre+post-move paths for both files
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
                 "--", "bot/engines/sports_data.py", "sports_data.py", "bot/engines/sports_engine.py", "sports_engine.py"],  # Sprint 10.1a/d (2026-05-11): include both pre+post-move paths for both files
                capture_output=True, text=True, timeout=10, cwd=repo_dir,
            )
            if diff_result.returncode != 0:
                continue
            for ln in diff_result.stdout.split("\n"):
                if not (ln.startswith("+") or ln.startswith("-")):
                    continue
                if any(f"{c} =" in ln or f"{c}=" in ln for c in REGIME_CONSTANTS):
                    dt = datetime.fromisoformat(timestamp)
                    return dt.strftime("%Y-%m-%dT%H:%M:%S")
        return "2026-02-28T00:00:00"
    except Exception:
        return "2026-02-28T00:00:00"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sports Comeback Alpha Analyzer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--db", default="state.db", help="Path to state.db")
    parser.add_argument("--since", default=None,
                        help="Filter to data since this date (YYYY-MM-DD)")
    parser.add_argument("--regime", choices=["auto"],
                        help="Auto-detect regime start from git history")
    parser.add_argument("--sport-group", default=None,
                        help="Filter to a sport group (basketball, hockey, etc.)")
    args = parser.parse_args()

    if args.regime == "auto":
        args.since = detect_regime_start()
        print(f"[Auto-detected regime start: {args.since}]")

    print()
    print("#" * 78)
    print("##  SPORTS COMEBACK ALPHA RESEARCH")
    print(f"##  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    if args.since:
        print(f"##  Regime filter: since {args.since}")
    if args.sport_group:
        print(f"##  Sport group: {args.sport_group}")
    print("#" * 78)

    # Load data
    try:
        data = load_sports_data(args.db, since=args.since,
                                sport_group=args.sport_group)
    except Exception as e:
        print(f"ERROR: Failed to load data: {e}")
        sys.exit(1)

    if not data:
        print("\nNo data found. Check DB path and filters.")
        sys.exit(1)

    print(f"\nLoaded {len(data)} evaluations.")

    # Run all analysis sections
    section_overview(data)
    section_sport_group(data)
    section_league(data)
    section_deficit(data)
    section_time_remaining(data)
    section_pregame_strength(data)
    section_lr_scale(data)
    section_price_analysis(data)
    section_signal_counterfactual(data)
    section_pregame_capture(data)
    section_sprt(data)
    section_calibration(data)
    section_game_flow(data)
    section_robustness(data)
    section_optimal_config(data)
    section_clv(data)
    section_summary(data)

    print()


if __name__ == "__main__":
    main()
