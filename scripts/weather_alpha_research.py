#!/usr/bin/env python3
"""
Weather Temperature Market Alpha Analyzer
Systematic research script to discover profitable configurations in weather data.
Rerunnable on updated data -- outputs standardized alpha discovery report.

Usage:
    scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
    python3 scripts/weather_alpha_research.py [--db /tmp/state.db]
"""
import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import combinations
from typing import Dict, List, Optional, Tuple


# ── Fee model ──────────────────────────────────────────────────────────
def maker_fee(count: int, price_cents: int) -> int:
    return math.ceil(0.0175 * count * price_cents * (100 - price_cents) / 100)


def taker_fee(count: int, price_cents: int) -> int:
    return math.ceil(0.07 * count * price_cents * (100 - price_cents) / 100)


def sim_pnl_1lot(price_cents: int, won: bool, is_maker: bool = True) -> float:
    fee = maker_fee(1, price_cents) if is_maker else taker_fee(1, price_cents)
    if won:
        return (100 - price_cents - fee) / 100.0
    else:
        return -(price_cents + fee) / 100.0


def breakeven_wr(price_cents: int, is_maker: bool = True) -> float:
    fee = maker_fee(1, price_cents) if is_maker else taker_fee(1, price_cents)
    return (price_cents + fee) / 100.0


# ── Statistical tests ──────────────────────────────────────────────────
def fisher_exact_p(a, b, c, d):
    """One-sided Fisher exact test p-value (a,b wins/losses group1; c,d group2)."""
    from math import comb
    n = a + b + c + d
    if n == 0 or n > 300:
        return 1.0
    def _hyper(x):
        try:
            return comb(a + b, x) * comb(c + d, a + c - x) / comb(n, a + c)
        except (ValueError, ZeroDivisionError):
            return 0.0
    p = sum(_hyper(x) for x in range(a, min(a + b, a + c) + 1))
    return min(p, 1.0)


def wilson_ci(wins, total, z=1.96):
    if total == 0:
        return 0.0, 0.0
    p = wins / total
    denom = 1 + z ** 2 / total
    center = (p + z ** 2 / (2 * total)) / denom
    spread = z * math.sqrt((p * (1 - p) + z ** 2 / (4 * total)) / total) / denom
    return max(0, center - spread), min(1, center + spread)


def brier_score(probs, outcomes):
    if not probs:
        return None
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / len(probs)


def profit_factor(wins_pnl, losses_pnl):
    if losses_pnl == 0:
        return float('inf') if wins_pnl > 0 else 0
    return abs(wins_pnl / losses_pnl)


def significance_tag(n: int) -> str:
    if n < 5:
        return "*** VERY SMALL SAMPLE"
    if n < 15:
        return "** NOT SIGNIFICANT"
    if n < 30:
        return "* SMALL SAMPLE"
    return ""


def safe_div(a, b, default=0.0):
    return a / b if b else default


def pct(num, denom) -> str:
    if denom == 0:
        return "n/a"
    return f"{num / denom * 100:.1f}%"


# ── Data loading ───────────────────────────────────────────────────────
def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(c[1] == column for c in cols)


def load_weather_data(db_path: str) -> List[Dict]:
    """Load all weather evaluated_opportunities."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT * FROM evaluated_opportunities
        WHERE product_type = 'weather'
        ORDER BY evaluation_time
    """).fetchall()

    # Check available columns
    has_hrrr = has_column(conn, "evaluated_opportunities", "wx_hrrr_temp")
    has_corrected = has_column(conn, "evaluated_opportunities", "wx_corrected_mean")

    data = []
    for r in rows:
        d = dict(r)
        # Parse key fields
        d['won'] = d.get('market_result') in ('yes', 'all_yes')
        d['lost'] = d.get('market_result') in ('no', 'all_no')
        d['settled'] = d.get('market_result') is not None

        # Price handling
        mp = d.get('market_price') or 0
        if 0 < mp < 1.5:
            d['price'] = max(1, min(99, int(round(mp * 100))))
        else:
            d['price'] = max(1, min(99, int(mp)))

        # Time fields
        d['eval_dt'] = None
        d['hour'] = None
        d['date'] = None
        d['weekday'] = None
        if d.get('evaluation_time'):
            try:
                d['eval_dt'] = datetime.fromisoformat(
                    d['evaluation_time'].replace('Z', '+00:00').replace('+00:00+00:00', '+00:00')
                )
                d['hour'] = d['eval_dt'].hour
                d['date'] = d['eval_dt'].strftime('%Y-%m-%d')
                d['weekday'] = d['eval_dt'].weekday()  # 0=Mon
            except (ValueError, TypeError):
                pass

        # Derived fields
        d['stc'] = d.get('seconds_to_close') or 0
        d['stc_hours'] = d['stc'] / 3600.0
        d['edge_val'] = d.get('edge') or 0
        d['fee_edge'] = d.get('fee_adjusted_edge') or d['edge_val']
        d['cal_prob'] = d.get('calibrated_prob') or 0
        d['raw_p'] = d.get('raw_prob') or 0
        d['is_signal'] = d.get('filter_stage') == 'weather_observation'
        d['city'] = d.get('asset') or 'unknown'

        # Weather-specific fields
        d['ens_mean'] = d.get('wx_ensemble_mean')
        d['ens_std'] = d.get('wx_ensemble_std')
        d['bias_corr'] = d.get('wx_bias_correction')
        d['n_members'] = d.get('wx_n_members')
        d['market_type'] = d.get('wx_market_type') or 'unknown'
        d['actual_temp'] = d.get('wx_actual_high_temp')
        d['no_side_edge'] = d.get('wx_no_side_edge')
        d['hrrr_temp'] = d.get('wx_hrrr_temp') if has_hrrr else None
        d['corrected_mean'] = d.get('wx_corrected_mean') if has_corrected else None

        # Temperature regime classification
        actual = d['actual_temp']
        if actual is not None:
            if actual < 40:
                d['temp_regime'] = 'cold'
            elif actual < 60:
                d['temp_regime'] = 'cool'
            elif actual < 75:
                d['temp_regime'] = 'mild'
            elif actual < 90:
                d['temp_regime'] = 'warm'
            else:
                d['temp_regime'] = 'hot'
        else:
            d['temp_regime'] = None

        data.append(d)

    conn.close()
    return data


# ── Configuration evaluator ────────────────────────────────────────────
def evaluate_config(signals: List[Dict],
                    cities: Optional[set] = None,
                    min_price: int = 1, max_price: int = 99,
                    min_edge: float = -1.0, max_edge: float = 1.0,
                    min_stc_hours: float = 0, max_stc_hours: float = 999,
                    min_ens_std: float = 0, max_ens_std: float = 999,
                    max_signals_per_event: int = 999,
                    blend_w: Optional[float] = None,
                    label: str = "") -> Dict:
    """Evaluate a configuration against the signal dataset."""
    filtered = []
    for s in signals:
        if not s['settled']:
            continue
        if cities and s['city'] not in cities:
            continue
        if s['price'] < min_price or s['price'] > max_price:
            continue
        if s['fee_edge'] < min_edge or s['fee_edge'] > max_edge:
            continue
        if s['stc_hours'] < min_stc_hours or s['stc_hours'] > max_stc_hours:
            continue
        if s['ens_std'] is not None:
            if s['ens_std'] < min_ens_std or s['ens_std'] > max_ens_std:
                continue
        filtered.append(s)

    if not filtered:
        return {'label': label, 'n': 0}

    # Apply per-event limit
    if max_signals_per_event < 999:
        by_event = defaultdict(list)
        for s in filtered:
            by_event[s.get('event_ticker', '')].append(s)
        limited = []
        for evt, sigs in by_event.items():
            sigs.sort(key=lambda x: x['price'], reverse=True)
            limited.extend(sigs[:max_signals_per_event])
        limited.sort(key=lambda x: x.get('evaluation_time', ''))
        filtered = limited

    wins = sum(1 for s in filtered if s['won'])
    losses = len(filtered) - wins
    wr = wins / len(filtered) if filtered else 0

    # PnL calculations
    flat_pnl = sum(sim_pnl_1lot(s['price'], s['won']) for s in filtered)
    sized_pnl_val = sum(
        sim_pnl_1lot(s['price'], s['won']) * (s.get('position_size') or 1)
        for s in filtered
    )

    # Win/loss PnL split
    win_pnl = sum(sim_pnl_1lot(s['price'], True) for s in filtered if s['won'])
    loss_pnl = sum(sim_pnl_1lot(s['price'], False) for s in filtered if not s['won'])
    pf = profit_factor(win_pnl, loss_pnl)

    # Brier
    probs = [s['cal_prob'] for s in filtered if s['cal_prob']]
    outcomes = [1.0 if s['won'] else 0.0 for s in filtered if s['cal_prob']]
    bs = brier_score(probs, outcomes)

    # Time span
    dated = [s for s in filtered if s['eval_dt']]
    if dated:
        first = dated[0]['eval_dt']
        last = dated[-1]['eval_dt']
        days = max((last - first).total_seconds() / 86400, 0.01)
    else:
        days = 0.01

    per_day = len(filtered) / days if days > 0 else 0
    pnl_per_day = flat_pnl / days if days > 0 else 0

    # Wilson CI
    lo, hi = wilson_ci(wins, len(filtered))

    # Avg breakeven
    avg_be = sum(breakeven_wr(s['price']) for s in filtered) / len(filtered)

    # Time stability: split into halves
    mid = len(filtered) // 2
    h1 = filtered[:mid]
    h2 = filtered[mid:]
    h1_wr = sum(1 for s in h1 if s['won']) / len(h1) if h1 else 0
    h2_wr = sum(1 for s in h2 if s['won']) / len(h2) if h2 else 0
    h1_pnl = sum(sim_pnl_1lot(s['price'], s['won']) for s in h1)
    h2_pnl = sum(sim_pnl_1lot(s['price'], s['won']) for s in h2)

    # Concentration: top 5 trades as % of total
    trade_pnls = sorted([sim_pnl_1lot(s['price'], s['won']) for s in filtered], reverse=True)
    top5_pnl = sum(trade_pnls[:5])
    concentration = top5_pnl / flat_pnl if flat_pnl > 0 else 0

    # Per-city breakdown
    city_stats = {}
    for s in filtered:
        c = s['city']
        if c not in city_stats:
            city_stats[c] = {'n': 0, 'w': 0, 'pnl': 0}
        city_stats[c]['n'] += 1
        if s['won']:
            city_stats[c]['w'] += 1
        city_stats[c]['pnl'] += sim_pnl_1lot(s['price'], s['won'])

    return {
        'label': label,
        'n': len(filtered),
        'wins': wins,
        'losses': losses,
        'wr': wr,
        'wilson_lo': lo,
        'wilson_hi': hi,
        'flat_pnl': flat_pnl,
        'sized_pnl': sized_pnl_val,
        'pnl_per_day': pnl_per_day,
        'profit_factor': pf,
        'brier': bs,
        'avg_price': sum(s['price'] for s in filtered) / len(filtered),
        'avg_edge': sum(s['edge_val'] for s in filtered) / len(filtered),
        'avg_be': avg_be,
        'wr_vs_be': wr - avg_be,
        'days': days,
        'per_day': per_day,
        'h1_wr': h1_wr,
        'h2_wr': h2_wr,
        'h1_pnl': h1_pnl,
        'h2_pnl': h2_pnl,
        'time_stable': abs(h1_wr - h2_wr) < 0.15,
        'concentration': concentration,
        'city_stats': city_stats,
    }


# ── Blend weight simulator ────────────────────────────────────────────
def simulate_blend(signals: List[Dict], blend_w: float) -> Dict:
    """Simulate a different market blend weight on settled data that has raw_prob."""
    eligible = [s for s in signals if s['settled'] and s['raw_p'] and s['raw_p'] > 0]
    if not eligible:
        return {'n': 0, 'blend_w': blend_w}

    total_brier = 0
    total_pnl = 0
    sigs = 0
    wins = 0
    for s in eligible:
        market_p = s['price'] / 100.0
        blended = (1.0 - blend_w) * s['raw_p'] + blend_w * market_p
        outcome = 1 if s['won'] else 0
        total_brier += (blended - outcome) ** 2

        edge = blended - market_p
        fee = maker_fee(1, s['price']) / 100.0
        fee_edge = edge - fee
        if fee_edge >= 0.001:
            sigs += 1
            total_pnl += sim_pnl_1lot(s['price'], s['won'])
            if outcome:
                wins += 1

    return {
        'blend_w': blend_w,
        'n': len(eligible),
        'brier': total_brier / len(eligible) if eligible else 0,
        'sigs': sigs,
        'wins': wins,
        'losses': sigs - wins,
        'pnl': total_pnl,
        'wr': wins / sigs if sigs > 0 else 0,
    }


# ── Main research pipeline ────────────────────────────────────────────
def run_research(db_path: str):
    print("=" * 80)
    print("  WEATHER TEMPERATURE MARKET ALPHA ANALYZER")
    print("=" * 80)

    all_data = load_weather_data(db_path)
    signals = [d for d in all_data if d['is_signal']]
    all_settled = [d for d in all_data if d['settled']]
    settled_signals = [d for d in signals if d['settled']]

    print(f"\nDataset: {len(all_data)} total evals, {len(signals)} signals, "
          f"{len(settled_signals)} settled signals, {len(all_settled)} total settled")
    if signals:
        times = [s['evaluation_time'] for s in signals if s.get('evaluation_time')]
        if times:
            print(f"Period: {times[0][:16]} to {times[-1][:16]}")
        dated = [s for s in signals if s['eval_dt']]
        if len(dated) >= 2:
            days = (dated[-1]['eval_dt'] - dated[0]['eval_dt']).total_seconds() / 86400
            print(f"Duration: {days:.1f} days")

    cities_seen = sorted(set(s['city'] for s in all_data))
    print(f"Cities: {', '.join(cities_seen)}")

    # ================================================================
    #  SECTION 1: CITY-BY-CITY ANALYSIS
    # ================================================================
    print("\n" + "=" * 80)
    print("  1. CITY-BY-CITY ANALYSIS")
    print("=" * 80)

    baseline = evaluate_config(settled_signals, label="ALL")
    print(f"\n  Baseline (all settled signals):")
    if baseline['n'] > 0:
        print(f"    N={baseline['n']}, {baseline['wins']}W/{baseline['losses']}L, "
              f"WR={baseline['wr']:.1%} [{baseline['wilson_lo']:.1%}-{baseline['wilson_hi']:.1%}]")
        print(f"    Flat PnL: ${baseline['flat_pnl']:.2f} (${baseline['pnl_per_day']:.2f}/day)")
        print(f"    Profit Factor: {baseline['profit_factor']:.2f}")
        print(f"    Avg Price: {baseline['avg_price']:.0f}c, Avg BE WR: {baseline['avg_be']:.1%}")
        print(f"    WR vs BE: {baseline['wr_vs_be']:+.1%}")
        print(f"    Brier: {baseline['brier']:.4f}" if baseline.get('brier') else "    Brier: N/A")
        print(f"    Time stability: H1 WR={baseline['h1_wr']:.1%}, H2 WR={baseline['h2_wr']:.1%}")
    else:
        print(f"    No settled signal data")

    print(f"\n  --- Per-city contribution ---")
    print(f"  {'City':>8} {'N':>5} {'W':>4} {'L':>4} {'WR':>7} {'CI95':>15} "
          f"{'FlatPnL':>10} {'BE WR':>7} {'Gap':>8} {'Sig':>20}")
    print(f"  {'-'*100}")

    city_results = {}
    for city in cities_seen:
        r = evaluate_config(settled_signals, cities={city}, label=city)
        city_results[city] = r
        if r['n'] > 0:
            lo, hi = wilson_ci(r['wins'], r['n'])
            tag = significance_tag(r['n'])
            print(f"  {city:>8} {r['n']:>5} {r['wins']:>4} {r['losses']:>4} "
                  f"{r['wr']:>6.1%} [{lo:.0%}-{hi:.0%}] "
                  f"${r['flat_pnl']:>9.2f} {r['avg_be']:>6.1%} {r['wr'] - r['avg_be']:>+7.1%} {tag}")

    # City exclusion analysis
    if len(cities_seen) >= 2:
        print(f"\n  --- City exclusion analysis ---")
        print(f"  {'Excluded':>12} {'N':>5} {'W':>4} {'L':>4} {'WR':>7} {'FlatPnL':>10} {'$/day':>8} {'PF':>6} {'Stable':>7}")
        print(f"  {'-'*70}")

        all_cities = set(cities_seen)
        for exclude_n in range(0, min(4, len(cities_seen))):
            for excluded in combinations(all_cities, exclude_n):
                remaining = all_cities - set(excluded)
                if not remaining:
                    continue
                label = f"no_{'_'.join(sorted(excluded))}" if excluded else "ALL"
                r = evaluate_config(settled_signals, cities=remaining, label=label)
                if r['n'] >= 5:
                    excl_str = ','.join(sorted(excluded)) if excluded else 'none'
                    print(f"  {excl_str:>12} {r['n']:>5} {r['wins']:>4} {r['losses']:>4} "
                          f"{r['wr']:>6.1%} ${r['flat_pnl']:>9.2f} ${r['pnl_per_day']:>7.2f} "
                          f"{r['profit_factor']:>5.2f} {'YES' if r['time_stable'] else 'NO':>7}")

    # ================================================================
    #  SECTION 2: ENSEMBLE QUALITY & FORECAST ACCURACY
    # ================================================================
    print("\n" + "=" * 80)
    print("  2. ENSEMBLE QUALITY & FORECAST ACCURACY")
    print("=" * 80)

    # Ensemble coverage
    has_ens = sum(1 for d in all_data if d['ens_mean'] is not None)
    has_std = sum(1 for d in all_data if d['ens_std'] is not None)
    has_members = sum(1 for d in all_data if d['n_members'] is not None)
    print(f"\n  Ensemble coverage:")
    print(f"    Mean:    {has_ens}/{len(all_data)} ({pct(has_ens, len(all_data))})")
    print(f"    Std:     {has_std}/{len(all_data)} ({pct(has_std, len(all_data))})")
    print(f"    Members: {has_members}/{len(all_data)} ({pct(has_members, len(all_data))})")

    member_data = [d for d in all_data if d['n_members'] is not None]
    if member_data:
        avg_mem = sum(d['n_members'] for d in member_data) / len(member_data)
        min_mem = min(d['n_members'] for d in member_data)
        max_mem = max(d['n_members'] for d in member_data)
        print(f"    Members stats: avg={avg_mem:.0f} min={min_mem} max={max_mem} (expected 82: 31 GFS + 51 ECMWF)")
        ecmwf_present = max_mem > 31
        print(f"    ECMWF status: {'PRESENT' if ecmwf_present else 'ABSENT (only GFS)'}")

    # Spread analysis per city
    std_by_city = defaultdict(list)
    for d in all_data:
        if d['ens_std'] is not None:
            std_by_city[d['city']].append(d['ens_std'])

    if std_by_city:
        print(f"\n  Ensemble spread by city:")
        print(f"  {'City':>8} {'N':>5} {'Mean':>7} {'Min':>6} {'Max':>6} {'Median':>7}")
        print(f"  {'-'*45}")
        for city in sorted(std_by_city.keys()):
            vals = sorted(std_by_city[city])
            n = len(vals)
            avg = sum(vals) / n
            med = vals[n // 2]
            print(f"  {city:>8} {n:>5} {avg:>6.2f}F {vals[0]:>5.2f}F {vals[-1]:>5.2f}F {med:>6.2f}F")

    # Forecast accuracy: ensemble vs actual
    forecast_data = [d for d in all_data if d['actual_temp'] is not None and d['ens_mean'] is not None]
    # Deduplicate by (city, date) — take first eval per city-day
    seen_citydays = set()
    forecast_dedup = []
    for d in forecast_data:
        key = (d['city'], d['date'])
        if key not in seen_citydays:
            seen_citydays.add(key)
            forecast_dedup.append(d)

    if forecast_dedup:
        print(f"\n  Forecast accuracy (ensemble vs actual, deduplicated by city-day):")
        print(f"  {'City':>8} {'N':>4} {'MAE':>7} {'RMSE':>7} {'Bias':>7} {'Direction':>10} {'Sig':>20}")
        print(f"  {'-'*75}")

        city_errors = defaultdict(list)
        for d in forecast_dedup:
            error = d['actual_temp'] - d['ens_mean']
            city_errors[d['city']].append(error)

        all_errors = []
        for city in sorted(city_errors.keys()):
            errors = city_errors[city]
            n = len(errors)
            mae = sum(abs(e) for e in errors) / n
            rmse = math.sqrt(sum(e ** 2 for e in errors) / n)
            bias = sum(errors) / n
            direction = "low" if bias > 0.5 else "high" if bias < -0.5 else "neutral"
            tag = significance_tag(n)
            print(f"  {city:>8} {n:>4} {mae:>6.1f}F {rmse:>6.1f}F {bias:>+6.1f}F {direction:>10} {tag}")
            all_errors.extend(errors)

        if all_errors:
            mae = sum(abs(e) for e in all_errors) / len(all_errors)
            rmse = math.sqrt(sum(e ** 2 for e in all_errors) / len(all_errors))
            bias = sum(all_errors) / len(all_errors)
            print(f"\n  Overall: {len(all_errors):>4} {mae:>6.1f}F {rmse:>6.1f}F {bias:>+6.1f}F")

        # Flag high-bias cities
        biased = [(c, sum(e for e in errs) / len(errs))
                  for c, errs in city_errors.items()
                  if len(errs) >= 3 and abs(sum(e for e in errs) / len(errs)) > 2.0]
        if biased:
            print(f"\n  *** HIGH-BIAS CITIES (|bias| > 2F, n>=3):")
            for city, bias in biased:
                direction = "forecasts too HIGH" if bias < 0 else "forecasts too LOW"
                print(f"      {city}: {bias:+.1f}F ({direction})")
    else:
        print(f"\n  No observed high-temp data (settlement archive not yet populated)")

    # ================================================================
    #  SECTION 3: HRRR vs ENSEMBLE COMPARISON
    # ================================================================
    print("\n" + "=" * 80)
    print("  3. HRRR vs ENSEMBLE COMPARISON")
    print("=" * 80)

    hrrr_data = [d for d in forecast_dedup if d['hrrr_temp'] is not None]
    if hrrr_data:
        hrrr_errors = [d['actual_temp'] - d['hrrr_temp'] for d in hrrr_data]
        ens_errors = [d['actual_temp'] - d['ens_mean'] for d in hrrr_data]

        hrrr_mae = sum(abs(e) for e in hrrr_errors) / len(hrrr_errors)
        ens_mae = sum(abs(e) for e in ens_errors) / len(ens_errors)
        hrrr_rmse = math.sqrt(sum(e ** 2 for e in hrrr_errors) / len(hrrr_errors))
        ens_rmse = math.sqrt(sum(e ** 2 for e in ens_errors) / len(ens_errors))
        hrrr_bias = sum(hrrr_errors) / len(hrrr_errors)
        ens_bias = sum(ens_errors) / len(ens_errors)

        print(f"\n  Overall comparison (n={len(hrrr_data)} city-days):")
        print(f"  {'Model':>12} {'MAE':>7} {'RMSE':>7} {'Bias':>7}")
        print(f"  {'-'*35}")
        print(f"  {'HRRR':>12} {hrrr_mae:>6.1f}F {hrrr_rmse:>6.1f}F {hrrr_bias:>+6.1f}F")
        print(f"  {'Ensemble':>12} {ens_mae:>6.1f}F {ens_rmse:>6.1f}F {ens_bias:>+6.1f}F")
        winner = "HRRR" if hrrr_mae < ens_mae else "Ensemble"
        print(f"\n  >>> {winner} is {abs(hrrr_mae - ens_mae):.1f}F more accurate (MAE)")

        # Corrected ensemble comparison
        corr_data = [d for d in hrrr_data if d['corrected_mean'] is not None]
        if corr_data:
            corr_errors = [d['actual_temp'] - d['corrected_mean'] for d in corr_data]
            corr_mae = sum(abs(e) for e in corr_errors) / len(corr_errors)
            corr_rmse = math.sqrt(sum(e ** 2 for e in corr_errors) / len(corr_errors))
            corr_bias = sum(corr_errors) / len(corr_errors)
            raw_ens_errors = [d['actual_temp'] - d['ens_mean'] for d in corr_data]
            raw_mae = sum(abs(e) for e in raw_ens_errors) / len(raw_ens_errors)
            print(f"\n  Corrected vs Raw Ensemble (n={len(corr_data)}):")
            print(f"  {'Model':>15} {'MAE':>7} {'RMSE':>7} {'Bias':>7}")
            print(f"  {'-'*38}")
            print(f"  {'Raw Ensemble':>15} {raw_mae:>6.1f}F {math.sqrt(sum(e**2 for e in raw_ens_errors)/len(raw_ens_errors)):>6.1f}F {sum(raw_ens_errors)/len(raw_ens_errors):>+6.1f}F")
            print(f"  {'Corrected':>15} {corr_mae:>6.1f}F {corr_rmse:>6.1f}F {corr_bias:>+6.1f}F")
            improvement = raw_mae - corr_mae
            print(f"  Correction {'helps' if improvement > 0 else 'hurts'}: {abs(improvement):.2f}F MAE {'reduction' if improvement > 0 else 'increase'}")

        # Per-city HRRR comparison
        print(f"\n  Per-city HRRR vs Ensemble:")
        print(f"  {'City':>8} {'N':>4} {'HRRR MAE':>9} {'Ens MAE':>9} {'Winner':>8}")
        print(f"  {'-'*42}")
        city_hrrr = defaultdict(list)
        for d in hrrr_data:
            city_hrrr[d['city']].append(d)
        for city in sorted(city_hrrr.keys()):
            cd = city_hrrr[city]
            h_mae = sum(abs(d['actual_temp'] - d['hrrr_temp']) for d in cd) / len(cd)
            e_mae = sum(abs(d['actual_temp'] - d['ens_mean']) for d in cd) / len(cd)
            w = "HRRR" if h_mae < e_mae else "Ens"
            print(f"  {city:>8} {len(cd):>4} {h_mae:>8.1f}F {e_mae:>8.1f}F {w:>8}")
    else:
        print(f"\n  No HRRR data available yet (wx_hrrr_temp not populated)")

    # ================================================================
    #  SECTION 4: TEMPERATURE REGIME ANALYSIS
    # ================================================================
    print("\n" + "=" * 80)
    print("  4. TEMPERATURE REGIME ANALYSIS")
    print("=" * 80)

    regime_data = [s for s in settled_signals if s['temp_regime'] is not None]
    if regime_data:
        regimes = defaultdict(lambda: {'w': 0, 'l': 0, 'pnl': 0, 'prices': [], 'errors': []})
        for s in regime_data:
            r = s['temp_regime']
            regimes[r]['w' if s['won'] else 'l'] += 1
            regimes[r]['pnl'] += sim_pnl_1lot(s['price'], s['won'])
            regimes[r]['prices'].append(s['price'])
            if s['ens_mean'] is not None and s['actual_temp'] is not None:
                regimes[r]['errors'].append(abs(s['actual_temp'] - s['ens_mean']))

        regime_order = ['cold', 'cool', 'mild', 'warm', 'hot']
        temp_ranges = {'cold': '<40F', 'cool': '40-59F', 'mild': '60-74F', 'warm': '75-89F', 'hot': '90F+'}

        print(f"\n  {'Regime':>8} {'Range':>8} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} "
              f"{'1-lot$':>9} {'AvgP':>6} {'MAE':>6} {'Sig':>20}")
        print(f"  {'-'*95}")
        for reg in regime_order:
            if reg not in regimes:
                continue
            s = regimes[reg]
            n = s['w'] + s['l']
            wr = s['w'] / n if n > 0 else 0
            avg_p = sum(s['prices']) / len(s['prices']) if s['prices'] else 0
            mae = sum(s['errors']) / len(s['errors']) if s['errors'] else 0
            tag = significance_tag(n)
            mae_str = f"{mae:.1f}F" if s['errors'] else "  n/a"
            print(f"  {reg:>8} {temp_ranges[reg]:>8} {n:>4} {s['w']:>3} {s['l']:>3} {wr:>5.1%} "
                  f"${s['pnl']:>8.2f} {avg_p:>5.0f}c {mae_str:>6} {tag}")

        # Forecast accuracy degrades in which regimes?
        if any(regimes[r]['errors'] for r in regime_order if r in regimes):
            worst = max((r for r in regime_order if r in regimes and regimes[r]['errors']),
                        key=lambda r: sum(regimes[r]['errors']) / len(regimes[r]['errors']),
                        default=None)
            best = min((r for r in regime_order if r in regimes and regimes[r]['errors']),
                       key=lambda r: sum(regimes[r]['errors']) / len(regimes[r]['errors']),
                       default=None)
            if worst and best and worst != best:
                w_mae = sum(regimes[worst]['errors']) / len(regimes[worst]['errors'])
                b_mae = sum(regimes[best]['errors']) / len(regimes[best]['errors'])
                print(f"\n  >>> Worst forecast regime: {worst} (MAE={w_mae:.1f}F)")
                print(f"  >>> Best forecast regime:  {best} (MAE={b_mae:.1f}F)")
    else:
        print(f"\n  No settled signals with actual temperature data")

    # ================================================================
    #  SECTION 5: LEAD TIME (STC) ANALYSIS
    # ================================================================
    print("\n" + "=" * 80)
    print("  5. LEAD TIME (STC) ANALYSIS")
    print("=" * 80)

    stc_data = [s for s in settled_signals if s['stc'] > 0]
    if stc_data:
        stc_buckets = defaultdict(lambda: {'w': 0, 'l': 0, 'pnl': 0, 'prices': [], 'errors': []})
        bucket_labels = ['<1h', '1-2h', '2-4h', '4-8h', '8-12h', '12-16h', '16-24h', '24h+']
        for s in stc_data:
            h = s['stc_hours']
            if h < 1: b = '<1h'
            elif h < 2: b = '1-2h'
            elif h < 4: b = '2-4h'
            elif h < 8: b = '4-8h'
            elif h < 12: b = '8-12h'
            elif h < 16: b = '12-16h'
            elif h < 24: b = '16-24h'
            else: b = '24h+'
            stc_buckets[b]['w' if s['won'] else 'l'] += 1
            stc_buckets[b]['pnl'] += sim_pnl_1lot(s['price'], s['won'])
            stc_buckets[b]['prices'].append(s['price'])
            if s['ens_mean'] is not None and s['actual_temp'] is not None:
                stc_buckets[b]['errors'].append(abs(s['actual_temp'] - s['ens_mean']))

        print(f"\n  {'STC':>8} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} {'1-lot$':>9} "
              f"{'AvgP':>6} {'MAE':>6} {'Sig':>20}")
        print(f"  {'-'*75}")
        for b in bucket_labels:
            if b not in stc_buckets:
                continue
            s = stc_buckets[b]
            n = s['w'] + s['l']
            wr = s['w'] / n if n > 0 else 0
            avg_p = sum(s['prices']) / len(s['prices']) if s['prices'] else 0
            mae = sum(s['errors']) / len(s['errors']) if s['errors'] else 0
            tag = significance_tag(n)
            mae_str = f"{mae:.1f}F" if s['errors'] else "  n/a"
            print(f"  {b:>8} {n:>4} {s['w']:>3} {s['l']:>3} {wr:>5.1%} "
                  f"${s['pnl']:>8.2f} {avg_p:>5.0f}c {mae_str:>6} {tag}")

        # Optimal STC range
        best_stc = None
        best_pnl = -999
        for lo_h in [1, 2, 4, 6, 8, 12]:
            for hi_h in [4, 8, 12, 16, 24]:
                if hi_h <= lo_h:
                    continue
                subset = [s for s in stc_data if lo_h <= s['stc_hours'] < hi_h]
                if len(subset) >= 5:
                    pnl = sum(sim_pnl_1lot(s['price'], s['won']) for s in subset)
                    wr = sum(1 for s in subset if s['won']) / len(subset)
                    if pnl > best_pnl:
                        best_pnl = pnl
                        best_stc = (lo_h, hi_h, len(subset), wr, pnl)
        if best_stc:
            lo_h, hi_h, n, wr, pnl = best_stc
            tag = significance_tag(n)
            print(f"\n  >>> Optimal STC range: {lo_h}-{hi_h}h (n={n}, WR={wr:.1%}, PnL=${pnl:.2f}) {tag}")
    else:
        print(f"\n  No settled signals with STC data")

    # ================================================================
    #  SECTION 6: BIAS CORRECTION EFFECTIVENESS
    # ================================================================
    print("\n" + "=" * 80)
    print("  6. BIAS CORRECTION EFFECTIVENESS")
    print("=" * 80)

    bias_data = [d for d in forecast_dedup if d['bias_corr'] is not None]
    if bias_data:
        city_bias = defaultdict(list)
        for d in bias_data:
            city_bias[d['city']].append(d['bias_corr'])

        print(f"\n  Bias correction values by city:")
        print(f"  {'City':>8} {'N':>4} {'Mean':>7} {'Min':>7} {'Max':>7} {'Active':>8}")
        print(f"  {'-'*45}")
        for city in sorted(city_bias.keys()):
            vals = city_bias[city]
            nonzero = sum(1 for v in vals if abs(v) > 0.01)
            print(f"  {city:>8} {len(vals):>4} {sum(vals) / len(vals):>+6.2f}F "
                  f"{min(vals):>+6.2f}F {max(vals):>+6.2f}F {nonzero:>4}/{len(vals)}")

        total_nonzero = sum(1 for d in bias_data if abs(d['bias_corr']) > 0.01)
        print(f"\n  Bias active: {total_nonzero}/{len(bias_data)} ({pct(total_nonzero, len(bias_data))})")
        if total_nonzero == 0:
            print(f"  >>> Bias correction is INACTIVE -- needs more settlement cycles")

        # Compare corrected vs raw where both available
        corr_vs_raw = [d for d in forecast_dedup
                       if d['corrected_mean'] is not None
                       and d['actual_temp'] is not None
                       and d['ens_mean'] is not None]
        if corr_vs_raw:
            raw_errors = [abs(d['actual_temp'] - d['ens_mean']) for d in corr_vs_raw]
            corr_errors = [abs(d['actual_temp'] - d['corrected_mean']) for d in corr_vs_raw]
            raw_mae = sum(raw_errors) / len(raw_errors)
            corr_mae = sum(corr_errors) / len(corr_errors)
            print(f"\n  Raw ensemble MAE:       {raw_mae:.2f}F (n={len(corr_vs_raw)})")
            print(f"  Corrected ensemble MAE: {corr_mae:.2f}F")
            diff = raw_mae - corr_mae
            print(f"  Correction effect: {diff:+.2f}F {'(HELPS)' if diff > 0 else '(HURTS)' if diff < 0 else '(NEUTRAL)'}")
    else:
        print(f"\n  No bias correction data available")

    # ================================================================
    #  SECTION 7: PRICE TIER ANALYSIS
    # ================================================================
    print("\n" + "=" * 80)
    print("  7. PRICE TIER ANALYSIS")
    print("=" * 80)

    if settled_signals:
        price_bands = [(1, 10, '1-10c'), (11, 20, '11-20c'), (21, 30, '21-30c'),
                       (31, 50, '31-50c'), (51, 70, '51-70c'), (71, 85, '71-85c'),
                       (86, 99, '86-99c')]

        print(f"\n  {'Band':>8} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} {'BE':>6} {'Gap':>7} "
              f"{'1-lot$':>9} {'AvgEdge':>8} {'Sig':>20}")
        print(f"  {'-'*90}")
        for lo, hi, label in price_bands:
            band = [s for s in settled_signals if lo <= s['price'] <= hi]
            if not band:
                continue
            w = sum(1 for s in band if s['won'])
            l = len(band) - w
            n = w + l
            wr = w / n if n > 0 else 0
            mid_price = (lo + hi) // 2
            be = breakeven_wr(mid_price)
            gap = wr - be
            pnl = sum(sim_pnl_1lot(s['price'], s['won']) for s in band)
            avg_edge = sum(s['edge_val'] for s in band) / len(band) if band else 0
            tag = significance_tag(n)
            print(f"  {label:>8} {n:>4} {w:>3} {l:>3} {wr:>5.1%} {be:>5.1%} {gap:>+6.1%} "
                  f"${pnl:>8.2f} {avg_edge:>7.3f} {tag}")

        # Flag losing bands
        print()
        for lo, hi, label in price_bands:
            band = [s for s in settled_signals if lo <= s['price'] <= hi]
            if not band:
                continue
            w = sum(1 for s in band if s['won'])
            n = len(band)
            wr = w / n
            mid_price = (lo + hi) // 2
            be = breakeven_wr(mid_price)
            if wr < be and n >= 3:
                print(f"  *** BELOW-BREAKEVEN: {label} ({w}W/{n - w}L = {wr:.1%} < BE {be:.1%})")

    # ================================================================
    #  SECTION 8: CALIBRATION DIAGNOSTICS
    # ================================================================
    print("\n" + "=" * 80)
    print("  8. CALIBRATION DIAGNOSTICS")
    print("=" * 80)

    cal_data = [s for s in settled_signals if s['cal_prob'] and s['cal_prob'] > 0]
    if cal_data:
        probs = [s['cal_prob'] for s in cal_data]
        outcomes = [1.0 if s['won'] else 0.0 for s in cal_data]
        overall_brier = brier_score(probs, outcomes)
        avg_pred = sum(probs) / len(probs)
        avg_actual = sum(outcomes) / len(outcomes)

        print(f"\n  Overall Brier Score: {overall_brier:.4f} (n={len(cal_data)})")
        print(f"  Avg predicted: {avg_pred:.3f}, Avg actual: {avg_actual:.3f}")
        print(f"  Overconfidence: {(avg_pred - avg_actual) * 100:+.1f}pp")

        # By probability bucket
        prob_bands = [(0.0, 0.15), (0.15, 0.25), (0.25, 0.40), (0.40, 0.60),
                      (0.60, 0.75), (0.75, 0.90), (0.90, 1.01)]
        print(f"\n  {'Bucket':>10} {'N':>4} {'Predicted':>10} {'Actual':>8} {'Gap':>8} {'Verdict':>15} {'Sig':>20}")
        print(f"  {'-'*85}")
        for lo, hi in prob_bands:
            band = [s for s in cal_data if lo <= s['cal_prob'] < hi]
            if not band:
                continue
            pred = sum(s['cal_prob'] for s in band) / len(band)
            act = sum(1 for s in band if s['won']) / len(band)
            gap = pred - act
            verdict = "OVERCONFIDENT" if gap > 0.05 else "UNDERCONFIDENT" if gap < -0.05 else "WELL-CAL"
            tag = significance_tag(len(band))
            label = f"{lo:.0%}-{hi:.0%}" if hi <= 1.0 else f"{lo:.0%}+"
            print(f"  {label:>10} {len(band):>4} {pred:>9.1%} {act:>7.1%} {gap:>+7.1%} {verdict:>15} {tag}")

        # Brier by city
        print(f"\n  Brier by city:")
        for city in sorted(set(s['city'] for s in cal_data)):
            cd = [s for s in cal_data if s['city'] == city]
            if not cd:
                continue
            bp = [s['cal_prob'] for s in cd]
            bo = [1.0 if s['won'] else 0.0 for s in cd]
            bs = brier_score(bp, bo)
            oc = sum(bp) / len(bp) - sum(bo) / len(bo)
            tag = significance_tag(len(cd))
            print(f"    {city:>8}: Brier={bs:.4f}, overconfidence={oc * 100:+.1f}pp (n={len(cd)}) {tag}")

        # raw_prob vs calibrated_prob comparison
        raw_data = [s for s in cal_data if s['raw_p'] and s['raw_p'] > 0]
        if raw_data:
            raw_probs = [s['raw_p'] for s in raw_data]
            cal_probs = [s['cal_prob'] for s in raw_data]
            raw_outcomes = [1.0 if s['won'] else 0.0 for s in raw_data]
            raw_brier = brier_score(raw_probs, raw_outcomes)
            cal_brier = brier_score(cal_probs, raw_outcomes)
            print(f"\n  Raw prob Brier:        {raw_brier:.4f} (n={len(raw_data)})")
            print(f"  Calibrated prob Brier: {cal_brier:.4f}")
            diff = raw_brier - cal_brier
            print(f"  Calibration effect: {diff:+.4f} {'(HELPS)' if diff > 0 else '(HURTS)' if diff < 0 else '(NEUTRAL)'}")
    else:
        print(f"\n  No calibrated probability data available")

    # ================================================================
    #  SECTION 9: SEASONAL / TEMPORAL PATTERNS
    # ================================================================
    print("\n" + "=" * 80)
    print("  9. SEASONAL / TEMPORAL PATTERNS")
    print("=" * 80)

    # Day of week
    dow_data = [s for s in settled_signals if s['weekday'] is not None]
    if dow_data:
        dow_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        dow_stats = defaultdict(lambda: {'w': 0, 'l': 0, 'pnl': 0})
        for s in dow_data:
            dow_stats[s['weekday']]['w' if s['won'] else 'l'] += 1
            dow_stats[s['weekday']]['pnl'] += sim_pnl_1lot(s['price'], s['won'])

        print(f"\n  Day-of-week breakdown:")
        print(f"  {'Day':>5} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} {'1-lot$':>9} {'Sig':>20}")
        print(f"  {'-'*55}")
        for i in range(7):
            if i not in dow_stats:
                continue
            s = dow_stats[i]
            n = s['w'] + s['l']
            wr = s['w'] / n if n > 0 else 0
            tag = significance_tag(n)
            print(f"  {dow_names[i]:>5} {n:>4} {s['w']:>3} {s['l']:>3} {wr:>5.1%} ${s['pnl']:>8.2f} {tag}")

    # Hour of eval
    hour_data = [s for s in settled_signals if s['hour'] is not None]
    if hour_data:
        hour_stats = defaultdict(lambda: {'w': 0, 'l': 0, 'pnl': 0})
        for s in hour_data:
            hour_stats[s['hour']]['w' if s['won'] else 'l'] += 1
            hour_stats[s['hour']]['pnl'] += sim_pnl_1lot(s['price'], s['won'])

        # Group into sessions
        session_map = {
            'Night (0-5)': range(0, 6),
            'Morning (6-11)': range(6, 12),
            'Afternoon (12-17)': range(12, 18),
            'Evening (18-23)': range(18, 24),
        }
        print(f"\n  Evaluation time session:")
        print(f"  {'Session':>20} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} {'1-lot$':>9} {'Sig':>20}")
        print(f"  {'-'*70}")
        for sname, hours in session_map.items():
            w = sum(hour_stats[h]['w'] for h in hours if h in hour_stats)
            l = sum(hour_stats[h]['l'] for h in hours if h in hour_stats)
            pnl = sum(hour_stats[h]['pnl'] for h in hours if h in hour_stats)
            n = w + l
            if n == 0:
                continue
            wr = w / n
            tag = significance_tag(n)
            print(f"  {sname:>20} {n:>4} {w:>3} {l:>3} {wr:>5.1%} ${pnl:>8.2f} {tag}")

    # Daily timeline
    daily_data = [s for s in settled_signals if s['date'] is not None]
    if daily_data:
        daily_stats = defaultdict(lambda: {'w': 0, 'l': 0, 'pnl': 0})
        for s in daily_data:
            daily_stats[s['date']]['w' if s['won'] else 'l'] += 1
            daily_stats[s['date']]['pnl'] += sim_pnl_1lot(s['price'], s['won'])

        print(f"\n  Daily P&L timeline:")
        print(f"  {'Date':>12} {'N':>3} {'W':>3} {'L':>3} {'WR':>6} {'1-lot$':>9} {'Cum$':>9}")
        print(f"  {'-'*50}")
        cum = 0
        for day in sorted(daily_stats.keys()):
            s = daily_stats[day]
            n = s['w'] + s['l']
            wr = s['w'] / n if n > 0 else 0
            cum += s['pnl']
            print(f"  {day:>12} {n:>3} {s['w']:>3} {s['l']:>3} {wr:>5.0f}% ${s['pnl']:>8.2f} ${cum:>8.2f}")

    # ================================================================
    #  SECTION 10: EDGE ANALYSIS & OPTIMAL THRESHOLDS
    # ================================================================
    print("\n" + "=" * 80)
    print("  10. EDGE ANALYSIS & OPTIMAL THRESHOLDS")
    print("=" * 80)

    edge_data = [s for s in settled_signals if s['fee_edge'] is not None]
    if edge_data:
        # Edge monotonicity test
        edge_bands = [(0, 0.005), (0.005, 0.01), (0.01, 0.02), (0.02, 0.05),
                      (0.05, 0.10), (0.10, 0.20), (0.20, 1.0)]
        prev_wr = None
        monotonic = True

        print(f"\n  Edge vs WR monotonicity:")
        print(f"  {'Edge Band':>12} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} {'1-lot$':>9} {'Mono':>6}")
        print(f"  {'-'*50}")
        for lo, hi in edge_bands:
            band = [s for s in edge_data if lo <= s['fee_edge'] < hi]
            if not band:
                continue
            w = sum(1 for s in band if s['won'])
            n = len(band)
            wr = w / n
            pnl = sum(sim_pnl_1lot(s['price'], s['won']) for s in band)
            is_mono = '  OK' if prev_wr is None or wr >= prev_wr - 0.05 else 'INVERT'
            if is_mono == 'INVERT':
                monotonic = False
            print(f"  {lo * 100:.1f}-{hi * 100:.1f}% {n:>4} {w:>3} {n - w:>3} {wr:>5.1%} "
                  f"${pnl:>8.2f} {is_mono:>6}")
            prev_wr = wr

        print(f"\n  Edge monotonicity: {'PASS' if monotonic else 'FAIL -- EDGE INVERSION DETECTED'}")

        # Minimum edge sweep (include insufficient_edge for counterfactual)
        ie_data = [d for d in all_data if d['settled']
                   and d.get('filter_stage') in ('weather_observation', 'insufficient_edge')
                   and d.get('fee_adjusted_edge') is not None]
        if ie_data:
            print(f"\n  Minimum edge sweep (incl. insufficient_edge counterfactual):")
            print(f"  {'MinEdge':>8} {'N':>5} {'W':>4} {'L':>4} {'WR':>6} {'1-lot$':>9} {'$/day':>8}")
            print(f"  {'-'*50}")
            for thresh in [0.001, 0.003, 0.005, 0.01, 0.02, 0.03, 0.05, 0.10]:
                passed = [s for s in ie_data if s.get('fee_adjusted_edge', 0) >= thresh]
                if not passed:
                    continue
                w = sum(1 for s in passed if s.get('market_result') in ('yes', 'all_yes'))
                n = len(passed)
                wr = w / n if n > 0 else 0
                pnl = sum(sim_pnl_1lot(
                    max(1, min(99, int(s.get('market_price', 50)))),
                    s.get('market_result') in ('yes', 'all_yes')
                ) for s in passed)
                dated_p = [s for s in passed if s.get('evaluation_time')]
                if len(dated_p) >= 2:
                    try:
                        t1 = datetime.fromisoformat(dated_p[0]['evaluation_time'].replace('Z', '+00:00').replace('+00:00+00:00', '+00:00'))
                        t2 = datetime.fromisoformat(dated_p[-1]['evaluation_time'].replace('Z', '+00:00').replace('+00:00+00:00', '+00:00'))
                        d = max((t2 - t1).total_seconds() / 86400, 0.01)
                    except (ValueError, TypeError):
                        d = 1
                else:
                    d = 1
                current = " <<<" if thresh == 0.001 else ""
                print(f"  {thresh:>7.3f} {n:>5} {w:>4} {n - w:>4} {wr:>5.1%} "
                      f"${pnl:>8.2f} ${pnl / d:>7.2f}{current}")

        # Per-city optimal edge
        print(f"\n  Per-city edge analysis:")
        print(f"  {'City':>8} {'N':>4} {'AvgEdge':>8} {'WR':>6} {'1-lot$':>9}")
        print(f"  {'-'*40}")
        for city in sorted(set(s['city'] for s in edge_data)):
            cd = [s for s in edge_data if s['city'] == city]
            if not cd:
                continue
            w = sum(1 for s in cd if s['won'])
            n = len(cd)
            avg_e = sum(s['fee_edge'] for s in cd) / n
            pnl = sum(sim_pnl_1lot(s['price'], s['won']) for s in cd)
            print(f"  {city:>8} {n:>4} {avg_e:>7.3f} {w / n:>5.1%} ${pnl:>8.2f}")

    # ================================================================
    #  SECTION 11: MARKET BLEND WEIGHT SIMULATION
    # ================================================================
    print("\n" + "=" * 80)
    print("  11. MARKET BLEND WEIGHT SIMULATION")
    print("=" * 80)

    blend_eligible = [s for s in settled_signals if s['raw_p'] and s['raw_p'] > 0]
    if len(blend_eligible) >= 5:
        print(f"\n  Simulating alternative WEATHER_MARKET_BLEND_W values (n={len(blend_eligible)}):")
        print(f"  {'BLEND_W':>8} {'Brier':>8} {'Sigs':>5} {'W':>4} {'L':>4} {'WR':>6} {'1-lot$':>9}")
        print(f"  {'-'*50}")

        best_blend = None
        best_brier = 999
        for bw in [0.00, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]:
            result = simulate_blend(blend_eligible, bw)
            current = " <<<" if bw == 0.20 else ""
            wr_str = f"{result['wr']:.1%}" if result['sigs'] > 0 else "  --"
            print(f"  {bw:>8.2f} {result['brier']:>8.4f} {result['sigs']:>5} "
                  f"{result['wins']:>4} {result['losses']:>4} {wr_str:>6} "
                  f"${result['pnl']:>8.2f}{current}")
            if result['brier'] < best_brier:
                best_brier = result['brier']
                best_blend = bw

        if best_blend is not None:
            print(f"\n  >>> Optimal blend weight (Brier): {best_blend:.2f} "
                  f"(current: 0.20, {'CHANGE WARRANTED' if best_blend != 0.20 else 'CURRENT IS OPTIMAL'})")
    else:
        print(f"\n  Insufficient data for blend simulation ({len(blend_eligible)} rows, need >= 5)")

    # ================================================================
    #  SECTION 12: EXHAUSTIVE CONFIGURATION SEARCH
    # ================================================================
    print("\n" + "=" * 80)
    print("  12. EXHAUSTIVE CONFIGURATION SEARCH")
    print("=" * 80)

    configs = []

    # 12a: City portfolios x price floors
    city_combos = []
    all_city_set = set(cities_seen)
    for city in cities_seen:
        city_combos.append(({city}, city))
    if len(cities_seen) >= 2:
        for n_excl in range(1, min(3, len(cities_seen))):
            for excluded in combinations(all_city_set, n_excl):
                remaining = all_city_set - set(excluded)
                if remaining:
                    label = 'no_' + '_'.join(sorted(excluded))
                    city_combos.append((remaining, label))
    city_combos.append((all_city_set, 'ALL'))

    price_floors = [5, 10, 15, 20, 30, 40, 50]
    for cities, cname in city_combos:
        for pf in price_floors:
            r = evaluate_config(settled_signals, cities=cities, min_price=pf,
                                label=f"{cname}_P>={pf}")
            if r['n'] >= 5:
                configs.append(r)

    # 12b: Edge thresholds
    edge_thresholds = [0.001, 0.005, 0.01, 0.02, 0.03, 0.05, 0.10]
    for cities, cname in [(all_city_set, 'ALL')] + [(set([c]), c) for c in cities_seen]:
        for et in edge_thresholds:
            r = evaluate_config(settled_signals, cities=cities, min_edge=et,
                                label=f"{cname}_edge>={et * 100:.1f}%")
            if r['n'] >= 5:
                configs.append(r)

    # 12c: STC ranges (hours)
    stc_ranges = [(1, 4), (1, 8), (2, 8), (2, 12), (4, 12), (4, 16),
                  (4, 24), (6, 12), (6, 24), (8, 24), (12, 24)]
    for stc_lo, stc_hi in stc_ranges:
        r = evaluate_config(settled_signals, min_stc_hours=stc_lo, max_stc_hours=stc_hi,
                            label=f"ALL_STC_{stc_lo}-{stc_hi}h")
        if r['n'] >= 5:
            configs.append(r)

    # 12d: Ensemble std filters
    std_thresholds = [(0, 1.0), (0, 1.5), (0, 2.0), (1.0, 2.0), (1.0, 3.0), (1.5, 3.0)]
    for std_lo, std_hi in std_thresholds:
        r = evaluate_config(settled_signals, min_ens_std=std_lo, max_ens_std=std_hi,
                            label=f"ALL_std_{std_lo:.1f}-{std_hi:.1f}")
        if r['n'] >= 5:
            configs.append(r)

    # 12e: Combined filters (city x price x edge)
    for cities, cname in [(all_city_set, 'ALL')] + [(set([c]), c) for c in cities_seen if city_results.get(c, {}).get('n', 0) >= 3]:
        for pf in [10, 20, 30]:
            for et in [0.005, 0.01, 0.03]:
                r = evaluate_config(settled_signals, cities=cities, min_price=pf, min_edge=et,
                                    label=f"{cname}_P>={pf}_E>={et * 100:.1f}%")
                if r['n'] >= 5:
                    configs.append(r)

    # 12f: Per-event position limits
    for limit in [1, 2, 3, 5]:
        r = evaluate_config(settled_signals, max_signals_per_event=limit,
                            label=f"ALL_evtlim={limit}")
        if r['n'] >= 5:
            configs.append(r)

    print(f"\n  Evaluated {len(configs)} configurations")

    # ================================================================
    #  SECTION 13: ALPHA DISCOVERY -- TOP CONFIGURATIONS
    # ================================================================
    print("\n" + "=" * 80)
    print("  13. ALPHA DISCOVERY -- TOP CONFIGURATIONS")
    print("=" * 80)

    # Filter to profitable configs
    profitable = [c for c in configs if c['flat_pnl'] > 0 and c['n'] >= 10]
    profitable.sort(key=lambda x: x['flat_pnl'], reverse=True)

    if not profitable:
        profitable = [c for c in configs if c['flat_pnl'] > 0 and c['n'] >= 5]
        profitable.sort(key=lambda x: x['flat_pnl'], reverse=True)
        if profitable:
            print(f"\n  WARNING: No configs with n>=10 are profitable. Showing n>=5:")

    if not profitable:
        print(f"\n  >>> NO PROFITABLE CONFIGURATIONS FOUND")
        print(f"\n  Least-negative configs:")
        least_bad = [c for c in configs if c['n'] >= 5]
        least_bad.sort(key=lambda x: x['flat_pnl'], reverse=True)
        profitable = least_bad[:15]

    print(f"\n  {'#':>3} {'Config':>40} {'N':>4} {'W/L':>8} {'WR':>6} "
          f"{'FlatPnL':>9} {'$/day':>8} {'PF':>5} {'BE':>5} {'Gap':>7} {'Stab':>5}")
    print(f"  {'-'*110}")

    for i, c in enumerate(profitable[:20], 1):
        wl = f"{c['wins']}W/{c['losses']}L"
        stable = 'YES' if c.get('time_stable') else 'NO'
        pf_val = c.get('profit_factor', 0)
        pf_str = f"{pf_val:>4.2f}" if pf_val < 100 else " inf"
        print(f"  {i:>3} {c['label']:>40} {c['n']:>4} {wl:>8} {c['wr']:>5.1%} "
              f"${c['flat_pnl']:>8.2f} ${c['pnl_per_day']:>7.2f} "
              f"{pf_str} {c['avg_be']:>4.0%} {c['wr_vs_be']:>+6.1%} {stable:>5}")

    # ================================================================
    #  SECTION 14: ROBUSTNESS VALIDATION (top 5)
    # ================================================================
    print("\n" + "=" * 80)
    print("  14. ROBUSTNESS VALIDATION -- TOP 5 CANDIDATES")
    print("=" * 80)

    top5 = profitable[:5]
    for i, c in enumerate(top5, 1):
        if c['n'] == 0:
            continue
        print(f"\n  --- Candidate {i}: {c['label']} ---")
        print(f"    N={c['n']}, {c['wins']}W/{c['losses']}L, WR={c['wr']:.1%}")
        print(f"    Wilson 95% CI: [{c['wilson_lo']:.1%} - {c['wilson_hi']:.1%}]")
        print(f"    Flat PnL: ${c['flat_pnl']:.2f} (${c['pnl_per_day']:.2f}/day)")
        pf_val = c.get('profit_factor', 0)
        print(f"    Profit Factor: {pf_val:.2f}" if pf_val < 100 else "    Profit Factor: inf")
        print(f"    Avg Price: {c['avg_price']:.0f}c, Avg BE: {c['avg_be']:.0%}")
        print(f"    WR vs BE: {c['wr_vs_be']:+.1%}")
        print(f"    Time stability: H1={c['h1_wr']:.1%} H2={c['h2_wr']:.1%} "
              f"{'STABLE' if c.get('time_stable') else 'UNSTABLE'}")
        if c.get('h1_pnl') is not None:
            print(f"    Half PnL: H1=${c['h1_pnl']:.2f}, H2=${c['h2_pnl']:.2f}")

        # Fisher test vs excluded trades
        if baseline['n'] > 0 and c['n'] < baseline['n']:
            excluded_w = baseline['wins'] - c['wins']
            excluded_l = baseline['losses'] - c['losses']
            if excluded_w + excluded_l > 0:
                p = fisher_exact_p(c['wins'], c['losses'], excluded_w, excluded_l)
                excl_wr = excluded_w / (excluded_w + excluded_l) if (excluded_w + excluded_l) > 0 else 0
                print(f"    Fisher vs excluded: p={p:.6f} "
                      f"({'***' if p < 0.001 else '**' if p < 0.01 else '*' if p < 0.05 else 'NS'})")
                print(f"    In: {c['wr']:.1%} ({c['n']}), Out: {excl_wr:.1%} ({excluded_w + excluded_l})")

        # City breakdown
        if c.get('city_stats'):
            print(f"    Cities:")
            for city, st in sorted(c['city_stats'].items()):
                cwr = st['w'] / st['n'] * 100 if st['n'] else 0
                print(f"      {city}: {st['n']}t, {st['w']}W, WR={cwr:.0f}%, PnL=${st['pnl']:.2f}")

    # ================================================================
    #  SECTION 15: NO-SIDE OPPORTUNITY
    # ================================================================
    print("\n" + "=" * 80)
    print("  15. NO-SIDE OPPORTUNITY ANALYSIS")
    print("=" * 80)

    no_side_data = [s for s in settled_signals if s['no_side_edge'] is not None]
    if no_side_data:
        pos_edge = [s for s in no_side_data if s['no_side_edge'] > 0]
        print(f"\n  Total with NO edge data: {len(no_side_data)}")
        print(f"  Positive NO edge: {len(pos_edge)}/{len(no_side_data)} ({pct(len(pos_edge), len(no_side_data))})")
        if pos_edge:
            no_wins = sum(1 for s in pos_edge if s['lost'])  # YES loses = NO wins
            no_pnl = sum(sim_pnl_1lot(100 - s['price'], s['lost']) for s in pos_edge)
            print(f"  If traded NO side: {no_wins}W/{len(pos_edge) - no_wins}L, PnL=${no_pnl:.2f}")
            avg_no_edge = sum(s['no_side_edge'] for s in pos_edge) / len(pos_edge)
            print(f"  Avg NO edge: {avg_no_edge:.3f}")

            # Per-city NO side
            if len(pos_edge) >= 3:
                print(f"\n  NO-side by city:")
                for city in sorted(set(s['city'] for s in pos_edge)):
                    cd = [s for s in pos_edge if s['city'] == city]
                    nw = sum(1 for s in cd if s['lost'])
                    np = sum(sim_pnl_1lot(100 - s['price'], s['lost']) for s in cd)
                    print(f"    {city}: {len(cd)} trades, {nw}W, PnL=${np:.2f}")
    else:
        print(f"\n  No NO-side edge data available")

    # ================================================================
    #  SECTION 16: LEAK / COUNTERFACTUAL
    # ================================================================
    print("\n" + "=" * 80)
    print("  16. LEAK / COUNTERFACTUAL ANALYSIS")
    print("=" * 80)

    for stage, label in [('insufficient_edge', 'Insufficient Edge'),
                         ('price_out_of_range', 'Price Out of Range'),
                         ('strategy_wait', 'Strategy Wait'),
                         ('low_probability', 'Low Probability')]:
        stage_data = [d for d in all_data if d['settled']
                      and d.get('filter_stage') == stage]
        if stage_data:
            w = sum(1 for d in stage_data if d['won'])
            n = len(stage_data)
            pnl = sum(sim_pnl_1lot(d['price'], d['won']) for d in stage_data)
            verdict = 'FILTER CORRECT' if pnl <= 0 else 'FILTER TOO STRICT'
            print(f"\n  {label}: {w}W/{n - w}L ({pct(w, n)}), PnL=${pnl:.2f} >>> {verdict}")

    # ================================================================
    #  SECTION 17: DATA SUFFICIENCY & READINESS
    # ================================================================
    print("\n" + "=" * 80)
    print("  17. DATA SUFFICIENCY & READINESS ASSESSMENT")
    print("=" * 80)

    n_total = len(all_data)
    n_signals = len(signals)
    n_settled = len(settled_signals)
    dated = [s for s in signals if s['eval_dt']]
    span_days = 0
    if len(dated) >= 2:
        span_days = (dated[-1]['eval_dt'] - dated[0]['eval_dt']).total_seconds() / 86400
    n_cities = len(set(s['city'] for s in signals))
    total_pnl = baseline.get('flat_pnl', 0) if baseline['n'] > 0 else 0

    ens_coverage = safe_div(has_ens, len(all_data)) * 100

    checks = [
        ("Total evaluations >= 500", n_total >= 500, f"{n_total}/500"),
        ("Signals >= 100", n_signals >= 100, f"{n_signals}/100"),
        ("Settled signals >= 50", n_settled >= 50, f"{n_settled}/50"),
        ("Days of data >= 14", span_days >= 14, f"{span_days:.1f}/14 days"),
        ("Cities signaling >= 5", n_cities >= 5, f"{n_cities}/5"),
        ("Ensemble coverage > 90%", ens_coverage > 90, f"{ens_coverage:.1f}%"),
        ("Signal PnL positive", total_pnl > 0, f"${total_pnl:.2f}"),
    ]

    print()
    for desc, passed, detail in checks:
        status = "[+]" if passed else "[ ]"
        print(f"  {status} {desc}: {detail}")

    n_pass = sum(1 for _, p, _ in checks if p)
    print(f"\n  {n_pass}/{len(checks)} checks passing", end="")
    if n_pass == len(checks):
        print(" -- READY for promotion evaluation")
    else:
        print(" -- continue data collection")

    if n_total > 0 and span_days > 0:
        rate = n_total / (span_days * 24)
        if n_total < 500 and rate > 0:
            hrs_to_500 = (500 - n_total) / rate
            print(f"\n  Eval rate: {rate:.1f}/hr | Est. time to 500 evals: {hrs_to_500 / 24:.1f} days")

    # ================================================================
    #  SECTION 18: FINAL VERDICT
    # ================================================================
    print("\n" + "=" * 80)
    print("  18. FINAL VERDICT: DOES LATENT ALPHA EXIST?")
    print("=" * 80)

    has_alpha = any(c['flat_pnl'] > 0 and c['n'] >= 20 and c.get('time_stable')
                    and c.get('profit_factor', 0) > 1.3 for c in configs)

    marginal_alpha = any(c['flat_pnl'] > 0 and c['n'] >= 10 for c in configs)

    if has_alpha:
        strong = [c for c in configs if c['flat_pnl'] > 0 and c['n'] >= 20
                  and c.get('time_stable') and c.get('profit_factor', 0) > 1.3]
        print(f"\n  VERDICT: ALPHA EXISTS ({len(strong)} robust configurations)")
        for c in strong[:5]:
            pf_val = c.get('profit_factor', 0)
            pf_str = f"{pf_val:.2f}" if pf_val < 100 else "inf"
            print(f"    - {c['label']}: n={c['n']}, WR={c['wr']:.1%}, "
                  f"PnL=${c['flat_pnl']:.2f}, PF={pf_str}")
    elif marginal_alpha:
        marginal = [c for c in configs if c['flat_pnl'] > 0 and c['n'] >= 10]
        print(f"\n  VERDICT: MARGINAL ALPHA ({len(marginal)} configs with positive PnL, "
              f"none fully robust)")
        for c in marginal[:5]:
            pf_val = c.get('profit_factor', 0)
            pf_str = f"{pf_val:.2f}" if pf_val < 100 else "inf"
            print(f"    - {c['label']}: n={c['n']}, WR={c['wr']:.1%}, "
                  f"PnL=${c['flat_pnl']:.2f}, PF={pf_str}, "
                  f"stable={'Y' if c.get('time_stable') else 'N'}")
    else:
        print(f"\n  VERDICT: NO CREDIBLE ALPHA DETECTED")
        print(f"  The weather strategy does not produce positive PnL under any tested")
        print(f"  configuration with n>=10 trades. Ensemble model may need improvement")
        print(f"  or market is too efficient at weather pricing.")

    # Key structural observations
    if settled_signals:
        print(f"\n  --- Structural observations ---")
        avg_cal = sum(s['cal_prob'] for s in settled_signals if s['cal_prob']) / max(1, sum(1 for s in settled_signals if s['cal_prob']))
        actual_wr = sum(1 for s in settled_signals if s['won']) / len(settled_signals) if settled_signals else 0
        if avg_cal > 0:
            oc = avg_cal - actual_wr
            level = 'CRITICAL' if abs(oc) > 0.2 else 'WARNING' if abs(oc) > 0.1 else 'INFO'
            direction = 'Overconfident' if oc > 0 else 'Underconfident'
            print(f"  [{level}] {direction}: predicted {avg_cal:.1%} vs actual {actual_wr:.1%} "
                  f"({oc * 100:+.1f}pp)")

    print(f"\n{'=' * 80}")
    print(f"  Analysis complete. {len(configs)} configurations evaluated.")
    print(f"{'=' * 80}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Weather Temperature Market Alpha Analyzer')
    parser.add_argument('--db', default='/tmp/state.db', help='Path to state.db')
    args = parser.parse_args()
    run_research(args.db)
