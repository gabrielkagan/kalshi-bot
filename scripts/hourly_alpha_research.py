#!/usr/bin/env python3
"""
Hourly Strategy Alpha Analyzer — Enhanced
Systematic research script to discover profitable configurations in hourly data.
Rerunnable on updated data — outputs standardized alpha discovery report.

Enhancements over v1:
  - Per-price-tier breakeven analysis matching MIN_EDGE_BY_PRICE schedule
  - Data-driven regime detection (volatility clusters, WR rolling shifts, density)
  - Exhaustive multi-dimensional grid search (asset x price x edge x STC x hour)
  - Loss concentration analysis (asset, price, hour, correlated multi-loss windows)
  - Calibration diagnostics (probability buckets, shadow cal, edge monotonicity)
  - Robustness validation (Wilson CI, Fisher exact, time stability H1/H2, profit factor,
    concentration risk, max drawdown)
  - Position sizing recommendations per price tier
  - Multi-position correlation analysis per event window

Usage:
    scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
    python3 scripts/hourly_alpha_research.py --db /tmp/state.db
"""
import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import combinations
from typing import Dict, List, Optional, Tuple

# ── Bot's live MIN_EDGE_BY_PRICE schedule (for reference) ─────────
BOT_EDGE_SCHEDULE = {
    86: 0.0025, 89: 0.0025, 91: 0.0035, 93: 0.009, 95: 0.0125, 97: 0.02
}

# ── Fee model ──────────────────────────────────────────────────────
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

# ── Statistical tests ──────────────────────────────────────────────
def fisher_exact_p(a, b, c, d):
    """One-sided Fisher exact test p-value (a,b wins/losses group1; c,d group2).
    Uses log-space to avoid factorial overflow."""
    n = a + b + c + d
    if n == 0:
        return 1.0

    def log_fact(x):
        return sum(math.log(i) for i in range(1, x + 1)) if x > 0 else 0.0

    r1 = a + b
    r2 = c + d
    c1 = a + c
    c2 = b + d

    def log_hyper(aa):
        bb = r1 - aa
        cc = c1 - aa
        dd = r2 - cc
        if bb < 0 or cc < 0 or dd < 0:
            return float('-inf')
        return (log_fact(r1) + log_fact(r2) + log_fact(c1) + log_fact(c2)
                - log_fact(n) - log_fact(aa) - log_fact(bb)
                - log_fact(cc) - log_fact(dd))

    p_obs = log_hyper(a)
    p_sum = 0.0
    lo = max(0, c1 - r2)
    hi = min(r1, c1)
    for aa in range(lo, hi + 1):
        lp = log_hyper(aa)
        if lp <= p_obs + 1e-10:
            p_sum += math.exp(lp)
    return min(1.0, p_sum)

def wilson_ci(wins, total, z=1.96):
    if total == 0:
        return 0, 0
    p = wins / total
    denom = 1 + z**2 / total
    center = (p + z**2 / (2 * total)) / denom
    spread = z * math.sqrt((p * (1 - p) + z**2 / (4 * total)) / total) / denom
    return max(0, center - spread), min(1, center + spread)

def brier_score(probs, outcomes):
    if not probs:
        return None
    return sum((p - o)**2 for p, o in zip(probs, outcomes)) / len(probs)

def profit_factor(wins_pnl, losses_pnl):
    if losses_pnl == 0:
        return float('inf') if wins_pnl > 0 else 0
    return abs(wins_pnl / losses_pnl)

# ── Data loading ───────────────────────────────────────────────────
def load_hourly_data(db_path: str) -> List[Dict]:
    """Load all settled hourly evaluated_opportunities."""
    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA busy_timeout=5000')
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT * FROM evaluated_opportunities
        WHERE product_type = 'hourly'
          AND market_result IS NOT NULL
        ORDER BY evaluation_time
    """).fetchall()

    data = []
    for r in rows:
        d = dict(r)
        d['won'] = d['market_result'] == 'yes'
        d['price'] = int(round(d['market_price'] * 100)) if d['market_price'] and d['market_price'] < 1.5 else int(d['market_price'] or 0)
        if d['price'] > 100:
            d['price'] = int(d['market_price'] * 100) if d['market_price'] < 1.5 else int(d['market_price'])
        d['price'] = max(1, min(99, d['price']))
        d['eval_dt'] = datetime.fromisoformat(d['evaluation_time'].replace('Z', '+00:00')) if d['evaluation_time'] else None
        d['hour'] = d['eval_dt'].hour if d['eval_dt'] else None
        d['date'] = d['eval_dt'].strftime('%Y-%m-%d') if d['eval_dt'] else None
        d['weekday'] = d['eval_dt'].weekday() if d['eval_dt'] else None  # 0=Mon
        d['stc'] = d.get('seconds_to_close') or 0
        d['edge_val'] = d.get('edge') or 0
        d['fee_edge'] = d.get('fee_adjusted_edge') or d['edge_val']
        d['cal_prob'] = d.get('calibrated_prob') or 0
        d['raw_p'] = d.get('raw_prob') or 0
        d['shadow_cal'] = d.get('shadow_cal_prob')
        d['is_signal'] = d['filter_stage'] == 'hourly_observation'
        d['volatility_val'] = d.get('volatility') or 0
        d['z_score_val'] = d.get('z_score') or 0
        d['kelly_val'] = d.get('kelly_f') or 0
        d['pre_temp'] = d.get('hourly_pre_temp_prob')
        d['temp_t'] = d.get('hourly_applied_temp_t')
        data.append(d)

    conn.close()
    return data


# ── Regime detection (data-driven) ────────────────────────────────
def detect_regimes(signals: List[Dict]) -> List[Dict]:
    """Detect regimes from data patterns using a sliding window approach.
    Identifies: volatility clusters, WR shifts, signal density changes,
    and asset dominance shifts. Returns regime boundaries where the
    character of trading materially changes."""
    if len(signals) < 30:
        return [{'start': 0, 'end': len(signals) - 1, 'label': 'all',
                 'signals': signals}]

    window = min(50, len(signals) // 3)
    step = max(1, window // 2)

    # Compute rolling statistics
    windows = []
    for i in range(0, len(signals) - window + 1, step):
        chunk = signals[i:i + window]
        wr = sum(1 for s in chunk if s['won']) / len(chunk)
        avg_price = sum(s['price'] for s in chunk) / len(chunk)
        avg_edge = sum(s['edge_val'] for s in chunk) / len(chunk)
        avg_vol = sum(s['volatility_val'] for s in chunk) / len(chunk)
        asset_counts = defaultdict(int)
        for s in chunk:
            asset_counts[s['asset']] += 1
        dominant = max(asset_counts, key=asset_counts.get)

        # Signal density (signals per hour)
        dt_span = (chunk[-1]['eval_dt'] - chunk[0]['eval_dt']).total_seconds()
        density = len(chunk) / (dt_span / 3600) if dt_span > 0 else 0

        windows.append({
            'start_idx': i,
            'end_idx': i + window - 1,
            'start_time': chunk[0]['evaluation_time'][:16],
            'end_time': chunk[-1]['evaluation_time'][:16],
            'n': len(chunk),
            'wr': wr,
            'avg_price': avg_price,
            'avg_edge': avg_edge,
            'avg_vol': avg_vol,
            'dominant_asset': dominant,
            'asset_mix': dict(asset_counts),
            'density': density,
            'signals': chunk,
        })

    # Detect regime boundaries: significant WR shift (>15pp) or vol shift (>50%)
    regimes = []
    current_regime_start = 0
    prev_wr = windows[0]['wr'] if windows else 0
    prev_vol = windows[0]['avg_vol'] if windows else 0

    for i, w in enumerate(windows):
        wr_shift = abs(w['wr'] - prev_wr) > 0.15
        vol_shift = abs(w['avg_vol'] - prev_vol) / max(prev_vol, 1e-8) > 0.5 if prev_vol > 0 else False

        if (wr_shift or vol_shift) and i > 0:
            # Close previous regime
            regime_signals = []
            for s in signals:
                if s['eval_dt'] and windows[current_regime_start]['start_time'][:16] <= s['evaluation_time'][:16] <= windows[i - 1]['end_time'][:16]:
                    regime_signals.append(s)
            if regime_signals:
                regime_wr = sum(1 for s in regime_signals if s['won']) / len(regime_signals)
                regimes.append({
                    'start_time': windows[current_regime_start]['start_time'],
                    'end_time': windows[i - 1]['end_time'],
                    'n': len(regime_signals),
                    'wr': regime_wr,
                    'trigger': 'WR_SHIFT' if wr_shift else 'VOL_SHIFT',
                    'signals': regime_signals,
                })
            current_regime_start = i
        prev_wr = w['wr']
        prev_vol = w['avg_vol']

    # Close final regime
    final_signals = []
    for s in signals:
        if s['eval_dt'] and s['evaluation_time'][:16] >= windows[current_regime_start]['start_time'][:16]:
            final_signals.append(s)
    if final_signals:
        final_wr = sum(1 for s in final_signals if s['won']) / len(final_signals)
        regimes.append({
            'start_time': windows[current_regime_start]['start_time'],
            'end_time': windows[-1]['end_time'] if windows else '',
            'n': len(final_signals),
            'wr': final_wr,
            'trigger': 'CURRENT',
            'signals': final_signals,
        })

    return regimes if regimes else [{'start_time': signals[0]['evaluation_time'][:16],
                                      'end_time': signals[-1]['evaluation_time'][:16],
                                      'n': len(signals),
                                      'wr': sum(1 for s in signals if s['won']) / len(signals),
                                      'trigger': 'SINGLE',
                                      'signals': signals}]


# ── Configuration evaluator ────────────────────────────────────────
def evaluate_config(signals: List[Dict],
                    assets: Optional[set] = None,
                    min_price: int = 1, max_price: int = 99,
                    min_edge: float = -1.0, max_edge: float = 1.0,
                    min_stc: int = 0, max_stc: int = 99999,
                    hours: Optional[set] = None,
                    max_signals_per_window: int = 999,
                    use_shadow_cal: bool = False,
                    label: str = "") -> Dict:
    """Evaluate a configuration against the signal dataset."""
    filtered = []
    for s in signals:
        if assets and s['asset'] not in assets:
            continue
        if s['price'] < min_price or s['price'] > max_price:
            continue
        edge = s['fee_edge'] if not use_shadow_cal else (s.get('shadow_cal_fee_edge') or s['fee_edge'])
        if edge < min_edge or edge > max_edge:
            continue
        if s['stc'] < min_stc or s['stc'] > max_stc:
            continue
        if hours and s['hour'] not in hours:
            continue
        filtered.append(s)

    if not filtered:
        return {'label': label, 'n': 0}

    # Apply per-window limit
    if max_signals_per_window < 999:
        by_event = defaultdict(list)
        for s in filtered:
            by_event[s.get('event_ticker', '')].append(s)
        limited = []
        for evt, sigs in by_event.items():
            sigs.sort(key=lambda x: x['price'], reverse=True)
            limited.extend(sigs[:max_signals_per_window])
        limited.sort(key=lambda x: x['evaluation_time'])
        filtered = limited

    wins = sum(1 for s in filtered if s['won'])
    losses = len(filtered) - wins
    wr = wins / len(filtered) if filtered else 0

    # PnL calculations
    flat_pnl = sum(sim_pnl_1lot(s['price'], s['won']) for s in filtered)
    sized_pnl = sum(
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
    first = filtered[0]['eval_dt']
    last = filtered[-1]['eval_dt']
    days = max((last - first).total_seconds() / 86400, 0.01)

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

    # Max drawdown
    cum_pnl = 0
    peak = 0
    max_dd = 0
    for s in filtered:
        cum_pnl += sim_pnl_1lot(s['price'], s['won'])
        if cum_pnl > peak:
            peak = cum_pnl
        dd = peak - cum_pnl
        if dd > max_dd:
            max_dd = dd

    # Concentration: top 5 trades as % of total
    trade_pnls = sorted([sim_pnl_1lot(s['price'], s['won']) for s in filtered], reverse=True)
    top5_pnl = sum(trade_pnls[:5])
    concentration = top5_pnl / flat_pnl if flat_pnl > 0 else 0

    # Per-asset breakdown
    asset_stats = {}
    for s in filtered:
        a = s['asset']
        if a not in asset_stats:
            asset_stats[a] = {'n': 0, 'w': 0, 'pnl': 0}
        asset_stats[a]['n'] += 1
        if s['won']:
            asset_stats[a]['w'] += 1
        asset_stats[a]['pnl'] += sim_pnl_1lot(s['price'], s['won'])

    return {
        'label': label,
        'n': len(filtered),
        'wins': wins,
        'losses': losses,
        'wr': wr,
        'wilson_lo': lo,
        'wilson_hi': hi,
        'flat_pnl': flat_pnl,
        'sized_pnl': sized_pnl,
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
        'max_drawdown': max_dd,
        'asset_stats': asset_stats,
    }


# ── Price tier utilities ───────────────────────────────────────────
def get_price_tier(price_cents: int) -> str:
    """Return the price tier label matching the bot's MIN_EDGE_BY_PRICE schedule."""
    if price_cents < 86:
        return '<86c'
    elif price_cents < 89:
        return '86-88c'
    elif price_cents < 91:
        return '89-90c'
    elif price_cents < 93:
        return '91-92c'
    elif price_cents < 95:
        return '93-94c'
    elif price_cents < 97:
        return '95-96c'
    else:
        return '97c+'

def get_tier_min_edge(price_cents: int) -> float:
    """Return the bot's minimum edge for this price tier."""
    if price_cents < 89:
        return 0.0025
    elif price_cents < 91:
        return 0.0025
    elif price_cents < 93:
        return 0.0035
    elif price_cents < 95:
        return 0.009
    elif price_cents < 97:
        return 0.0125
    else:
        return 0.02


# ── Main research pipeline ─────────────────────────────────────────
def run_research(db_path: str):
    print("=" * 80)
    print("  HOURLY STRATEGY ALPHA ANALYZER (Enhanced)")
    print("=" * 80)

    all_data = load_hourly_data(db_path)
    signals = [d for d in all_data if d['is_signal']]
    all_settled = [d for d in all_data if d['market_result'] is not None]

    print(f"\nDataset: {len(all_data)} total evals, {len(signals)} signals, "
          f"{len(all_settled)} settled")
    if not signals:
        print("  >>> NO SIGNALS FOUND. Nothing to analyze.")
        return

    print(f"Period: {signals[0]['evaluation_time'][:16]} to {signals[-1]['evaluation_time'][:16]}")
    days = (signals[-1]['eval_dt'] - signals[0]['eval_dt']).total_seconds() / 86400
    print(f"Duration: {days:.1f} days")
    print(f"Signal rate: {len(signals) / max(days, 0.01):.1f}/day")

    # ================================================================
    #  SECTION 1: BASELINE & ASSET CONTRIBUTION
    # ================================================================
    print("\n" + "=" * 80)
    print("  1. BASELINE & ASSET CONTRIBUTION")
    print("=" * 80)

    baseline = evaluate_config(signals, label="ALL")
    print(f"\n  Baseline (all signals):")
    print(f"    N={baseline['n']}, {baseline['wins']}W/{baseline['losses']}L, "
          f"WR={baseline['wr']:.1%} [{baseline['wilson_lo']:.1%}-{baseline['wilson_hi']:.1%}]")
    print(f"    Flat PnL: ${baseline['flat_pnl']:.2f} (${baseline['pnl_per_day']:.2f}/day)")
    print(f"    Profit Factor: {baseline['profit_factor']:.2f}")
    print(f"    Avg Price: {baseline['avg_price']:.0f}c, Avg BE WR: {baseline['avg_be']:.1%}")
    print(f"    WR vs BE: {baseline['wr_vs_be']:+.1%}")
    print(f"    Brier: {baseline['brier']:.4f}" if baseline['brier'] else "    Brier: N/A")
    print(f"    Time stability: H1 WR={baseline['h1_wr']:.1%}, H2 WR={baseline['h2_wr']:.1%}")
    print(f"    Max drawdown: ${baseline['max_drawdown']:.2f}")
    if baseline['concentration'] < 50:
        print(f"    Top-5 concentration: {baseline['concentration']:.0%} of total PnL")

    print(f"\n  --- Per-asset contribution ---")
    print(f"  {'Asset':>6} {'N':>5} {'W':>4} {'L':>4} {'WR':>7} {'FlatPnL':>10} {'BE WR':>7} {'Gap':>8} {'PF':>6}")
    print(f"  {'-'*66}")

    asset_results = {}
    for asset in ['BTC', 'ETH', 'SOL', 'XRP']:
        r = evaluate_config(signals, assets={asset}, label=asset)
        asset_results[asset] = r
        if r['n'] > 0:
            avg_be = r['avg_be']
            print(f"  {asset:>6} {r['n']:>5} {r['wins']:>4} {r['losses']:>4} "
                  f"{r['wr']:>6.1%} ${r['flat_pnl']:>9.2f} {avg_be:>6.1%} "
                  f"{r['wr'] - avg_be:>+7.1%} {r['profit_factor']:>5.2f}")

    # Asset exclusion analysis
    print(f"\n  --- Asset exclusion analysis ---")
    print(f"  {'Excluded':>10} {'N':>5} {'W':>4} {'L':>4} {'WR':>7} {'FlatPnL':>10} "
          f"{'$/day':>8} {'PF':>6} {'Stable':>7}")
    print(f"  {'-'*70}")

    all_assets = {'BTC', 'ETH', 'SOL', 'XRP'}
    exclusion_results = []
    for exclude_n in range(0, 4):
        for excluded in combinations(all_assets, exclude_n):
            remaining = all_assets - set(excluded)
            if not remaining:
                continue
            label = f"no_{'_'.join(sorted(excluded))}" if excluded else "ALL"
            r = evaluate_config(signals, assets=remaining, label=label)
            if r['n'] >= 20:
                exclusion_results.append(r)
                excl_str = ','.join(sorted(excluded)) if excluded else 'none'
                print(f"  {excl_str:>10} {r['n']:>5} {r['wins']:>4} {r['losses']:>4} "
                      f"{r['wr']:>6.1%} ${r['flat_pnl']:>9.2f} ${r['pnl_per_day']:>7.2f} "
                      f"{r['profit_factor']:>5.2f} {'YES' if r['time_stable'] else 'NO':>7}")

    # ================================================================
    #  SECTION 2: PER-PRICE-TIER BREAKEVEN ANALYSIS
    # ================================================================
    print("\n" + "=" * 80)
    print("  2. PER-PRICE-TIER BREAKEVEN ANALYSIS (matches MIN_EDGE_BY_PRICE)")
    print("=" * 80)

    tiers = defaultdict(list)
    for s in signals:
        tiers[get_price_tier(s['price'])].append(s)

    tier_order = ['<86c', '86-88c', '89-90c', '91-92c', '93-94c', '95-96c', '97c+']
    print(f"\n  {'Tier':>10} {'N':>4} {'W':>3} {'L':>3} {'WR':>7} {'BE WR':>7} {'Gap':>8} "
          f"{'BotEdge':>8} {'AvgEdge':>9} {'PnL':>9} {'Sizing':>8}")
    print(f"  {'-'*88}")

    for tier_name in tier_order:
        tier_sigs = tiers.get(tier_name, [])
        if not tier_sigs:
            continue
        wins = sum(1 for s in tier_sigs if s['won'])
        losses = len(tier_sigs) - wins
        wr = wins / len(tier_sigs)
        avg_be = sum(breakeven_wr(s['price']) for s in tier_sigs) / len(tier_sigs)
        avg_edge = sum(s['fee_edge'] for s in tier_sigs) / len(tier_sigs)
        flat_pnl = sum(sim_pnl_1lot(s['price'], s['won']) for s in tier_sigs)
        representative_price = int(sum(s['price'] for s in tier_sigs) / len(tier_sigs))
        bot_edge = get_tier_min_edge(representative_price) * 100

        # Sizing recommendation: based on gap vs breakeven and sample size
        gap = wr - avg_be
        lo_ci, _ = wilson_ci(wins, len(tier_sigs))
        if gap > 0.05 and lo_ci > avg_be and len(tier_sigs) >= 30:
            sizing = "FULL"
        elif gap > 0.02 and lo_ci > avg_be - 0.02:
            sizing = "HALF"
        elif gap > 0 and len(tier_sigs) >= 20:
            sizing = "1/4"
        else:
            sizing = "SKIP"

        print(f"  {tier_name:>10} {len(tier_sigs):>4} {wins:>3} {losses:>3} {wr:>6.1%} "
              f"{avg_be:>6.1%} {gap * 100:>+7.1f}pp {bot_edge:>7.2f}% "
              f"{avg_edge * 100:>+8.2f}% ${flat_pnl:>8.2f} {sizing:>8}")

    # ================================================================
    #  SECTION 3: LOSS CONCENTRATION ANALYSIS
    # ================================================================
    print("\n" + "=" * 80)
    print("  3. LOSS CONCENTRATION ANALYSIS")
    print("=" * 80)

    losses_list = [s for s in signals if not s['won']]

    # By asset
    print(f"\n  --- Loss concentration by asset ---")
    asset_losses = defaultdict(list)
    for s in losses_list:
        asset_losses[s['asset']].append(s)

    total_loss_pnl = sum(sim_pnl_1lot(s['price'], False) for s in losses_list)
    for asset in ['BTC', 'ETH', 'SOL', 'XRP']:
        al = asset_losses.get(asset, [])
        loss_pnl = sum(sim_pnl_1lot(s['price'], False) for s in al)
        pct = loss_pnl / total_loss_pnl * 100 if total_loss_pnl else 0
        avg_p = sum(s['price'] for s in al) / len(al) if al else 0
        print(f"    {asset}: {len(al)} losses (${loss_pnl:.2f}, {pct:.1f}% of total), "
              f"avg price={avg_p:.0f}c")

    # By price band
    print(f"\n  --- Loss concentration by price band ---")
    price_bands = [(50, 69, '50-69c'), (70, 79, '70-79c'), (80, 84, '80-84c'),
                   (85, 89, '85-89c'), (90, 94, '90-94c'), (95, 99, '95-99c')]
    for lo, hi, label in price_bands:
        band_sigs = [s for s in signals if lo <= s['price'] <= hi]
        band_w = sum(1 for s in band_sigs if s['won'])
        band_l = len(band_sigs) - band_w
        band_pnl = sum(sim_pnl_1lot(s['price'], s['won']) for s in band_sigs)
        wr = band_w / len(band_sigs) * 100 if band_sigs else 0
        be = sum(breakeven_wr(s['price']) for s in band_sigs) / len(band_sigs) * 100 if band_sigs else 0
        print(f"    {label}: n={len(band_sigs):>3}, {band_w}W/{band_l}L, "
              f"WR={wr:.0f}%, BE={be:.0f}%, PnL=${band_pnl:.2f}")

    # By hour of day
    print(f"\n  --- Loss concentration by hour (UTC) ---")
    hour_stats = defaultdict(lambda: {'w': 0, 'l': 0, 'pnl': 0})
    for s in signals:
        h = s['hour']
        if h is not None:
            hour_stats[h]['w' if s['won'] else 'l'] += 1
            hour_stats[h]['pnl'] += sim_pnl_1lot(s['price'], s['won'])

    print(f"  {'Hour':>6} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} {'PnL':>9} {'$/sig':>7}")
    for h in sorted(hour_stats.keys()):
        st = hour_stats[h]
        n = st['w'] + st['l']
        wr = st['w'] / n * 100 if n else 0
        per_sig = st['pnl'] / n if n else 0
        marker = ' <<<' if st['pnl'] < -1.0 else (' +++' if st['pnl'] > 1.0 else '')
        print(f"  {h:>5}h {n:>4} {st['w']:>3} {st['l']:>3} {wr:>5.0f}% "
              f"${st['pnl']:>8.2f} ${per_sig:>6.3f}{marker}")

    # Correlated multi-loss windows
    print(f"\n  --- Correlated multi-loss windows (top 10) ---")
    by_event = defaultdict(list)
    for s in signals:
        by_event[s.get('event_ticker', 'unknown')].append(s)

    window_losses = []
    for evt, sigs in by_event.items():
        l = sum(1 for s in sigs if not s['won'])
        if l >= 2:
            pnl = sum(sim_pnl_1lot(s['price'], s['won']) for s in sigs)
            assets_in = set(s['asset'] for s in sigs)
            window_losses.append((evt, len(sigs), l, pnl, assets_in))

    window_losses.sort(key=lambda x: x[3])
    for evt, n, l, pnl, assets_in in window_losses[:10]:
        print(f"    {evt}: {n} signals, {l} losses, PnL=${pnl:.2f} "
              f"({','.join(sorted(assets_in))})")

    corr_loss = sum(pnl for _, _, _, pnl, _ in window_losses if pnl < 0)
    print(f"    Total correlated window loss: ${corr_loss:.2f}")
    if baseline['flat_pnl'] < 0 and corr_loss < 0:
        print(f"    Correlated losses as % of total: "
              f"{corr_loss / baseline['flat_pnl'] * 100:.0f}%")

    # Multi-position correlation analysis
    print(f"\n  --- Multi-position correlation analysis ---")
    multi_pos_events = [(evt, sigs) for evt, sigs in by_event.items() if len(sigs) >= 2]
    if multi_pos_events:
        all_win_pct = []
        all_lose_pct = []
        mixed_n = 0
        for evt, sigs in multi_pos_events:
            outcomes = [s['won'] for s in sigs]
            if all(outcomes):
                all_win_pct.append(1)
            elif not any(outcomes):
                all_lose_pct.append(1)
            else:
                mixed_n += 1
        total_multi = len(multi_pos_events)
        print(f"    Windows with 2+ positions: {total_multi}")
        print(f"    All-win: {len(all_win_pct)} ({len(all_win_pct) / total_multi * 100:.0f}%)")
        print(f"    All-loss: {len(all_lose_pct)} ({len(all_lose_pct) / total_multi * 100:.0f}%)")
        print(f"    Mixed: {mixed_n} ({mixed_n / total_multi * 100:.0f}%)")

        # ENB calculation
        avg_sigs = sum(len(sigs) for _, sigs in multi_pos_events) / total_multi
        if all_lose_pct and total_multi > 5:
            corr_ratio = len(all_lose_pct) / total_multi
            # If all independent, P(all lose) = (1-WR)^n. ENB is the n that fits.
            base_wr = baseline['wr']
            if base_wr < 1 and corr_ratio > 0:
                import math as m
                try:
                    enb = m.log(corr_ratio) / m.log(1 - base_wr) if (1 - base_wr) > 0 else avg_sigs
                except (ValueError, ZeroDivisionError):
                    enb = avg_sigs
                print(f"    Effective # independent bets (ENB): {enb:.1f} "
                      f"(of {avg_sigs:.1f} avg positions)")
                print(f"    Correlation level: {'HIGH' if enb < avg_sigs * 0.5 else 'MODERATE' if enb < avg_sigs * 0.8 else 'LOW'}")

    # ================================================================
    #  SECTION 4: EDGE INTEGRITY & CALIBRATION DIAGNOSTICS
    # ================================================================
    print("\n" + "=" * 80)
    print("  4. EDGE INTEGRITY & CALIBRATION DIAGNOSTICS")
    print("=" * 80)

    # Edge monotonicity test
    print(f"\n  --- Edge vs WR monotonicity ---")
    edge_bands = [(0, 0.005), (0.005, 0.01), (0.01, 0.02), (0.02, 0.03),
                  (0.03, 0.05), (0.05, 0.08), (0.08, 0.15), (0.15, 1.0)]
    prev_wr = None
    monotonic = True
    print(f"  {'Edge Band':>14} {'N':>4} {'WR':>6} {'BE':>6} {'Gap':>8} {'Mono':>6}")
    for lo, hi in edge_bands:
        band = [s for s in signals if lo <= s['edge_val'] < hi]
        if not band:
            continue
        wr = sum(1 for s in band if s['won']) / len(band)
        be = sum(breakeven_wr(s['price']) for s in band) / len(band)
        is_mono = '  OK' if prev_wr is None or wr >= prev_wr - 0.05 else 'INVERT'
        if is_mono == 'INVERT':
            monotonic = False
        print(f"  {lo * 100:.1f}-{hi * 100:.1f}% {len(band):>4} {wr:>5.1%} "
              f"{be:>5.1%} {(wr - be) * 100:>+7.1f}pp {is_mono:>6}")
        prev_wr = wr

    print(f"\n  Edge monotonicity: {'PASS' if monotonic else 'FAIL -- EDGE INVERSION DETECTED'}")

    # Calibration by probability bucket
    print(f"\n  --- Calibration by probability bucket ---")
    prob_bands = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.85),
                  (0.85, 0.9), (0.9, 0.95), (0.95, 1.0)]
    print(f"  {'Bucket':>10} {'N':>4} {'WR':>6} {'Pred':>6} {'Gap':>8} {'Wilson95':>14}")
    for lo, hi in prob_bands:
        band = [s for s in signals if lo <= s['cal_prob'] < hi]
        if not band:
            continue
        wins = sum(1 for s in band if s['won'])
        wr = wins / len(band)
        pred = sum(s['cal_prob'] for s in band) / len(band)
        wlo, whi = wilson_ci(wins, len(band))
        print(f"  {lo:.2f}-{hi:.2f} {len(band):>4} {wr:>5.1%} {pred:>5.1%} "
              f"{(wr - pred) * 100:>+7.1f}pp [{wlo:.1%}-{whi:.1%}]")

    # Overall overconfidence
    avg_cal = sum(s['cal_prob'] for s in signals) / len(signals)
    actual_wr = sum(1 for s in signals if s['won']) / len(signals)
    print(f"\n  Overall: predicted {avg_cal:.1%} vs actual {actual_wr:.1%} "
          f"({(avg_cal - actual_wr) * 100:+.1f}pp overconfidence)")

    # Shadow cal comparison (where available)
    shadow_sigs = [s for s in signals if s['shadow_cal'] is not None and s['shadow_cal'] > 0]
    if shadow_sigs:
        print(f"\n  --- Shadow CalEngine vs Live calibration (n={len(shadow_sigs)}) ---")
        live_probs = [s['cal_prob'] for s in shadow_sigs]
        shadow_probs = [s['shadow_cal'] for s in shadow_sigs]
        outcomes = [1.0 if s['won'] else 0.0 for s in shadow_sigs]
        live_brier = brier_score(live_probs, outcomes)
        shadow_brier = brier_score(shadow_probs, outcomes)
        live_oc = sum(live_probs) / len(live_probs) - sum(outcomes) / len(outcomes)
        shadow_oc = sum(shadow_probs) / len(shadow_probs) - sum(outcomes) / len(outcomes)
        print(f"    Live Brier:   {live_brier:.4f}, overconfidence: {live_oc * 100:+.1f}pp")
        print(f"    Shadow Brier: {shadow_brier:.4f}, overconfidence: {shadow_oc * 100:+.1f}pp")
        winner = 'Shadow' if shadow_brier < live_brier else 'Live'
        print(f"    >>> {winner} wins by {abs(live_brier - shadow_brier):.4f}")

    # Temperature scaling analysis (where available)
    temp_sigs = [s for s in signals if s['pre_temp'] is not None and s['pre_temp'] > 0]
    if temp_sigs:
        print(f"\n  --- Temperature scaling impact (n={len(temp_sigs)}) ---")
        pre_probs = [s['pre_temp'] for s in temp_sigs]
        post_probs = [s['cal_prob'] for s in temp_sigs]
        outcomes = [1.0 if s['won'] else 0.0 for s in temp_sigs]
        pre_brier = brier_score(pre_probs, outcomes)
        post_brier = brier_score(post_probs, outcomes)
        pre_oc = sum(pre_probs) / len(pre_probs) - sum(outcomes) / len(outcomes)
        post_oc = sum(post_probs) / len(post_probs) - sum(outcomes) / len(outcomes)
        avg_t = sum(s['temp_t'] for s in temp_sigs if s['temp_t']) / max(1, sum(1 for s in temp_sigs if s['temp_t']))
        print(f"    Pre-temp Brier:  {pre_brier:.4f}, overconfidence: {pre_oc * 100:+.1f}pp")
        print(f"    Post-temp Brier: {post_brier:.4f}, overconfidence: {post_oc * 100:+.1f}pp")
        print(f"    Avg T applied:   {avg_t:.3f}")

        # Sweep T values
        print(f"\n    T sweep (on pre-temp probs):")
        print(f"    {'T':>6} {'Brier':>7} {'OC':>8} {'Trades':>7} {'WR':>6} {'PnL':>9}")
        for t in [1.0, 1.1, 1.2, 1.3, 1.45, 1.6, 1.8, 2.0, 2.5]:
            n_trades = 0
            n_wins = 0
            t_pnl = 0
            t_brier = 0
            t_oc_sum = 0
            for s in temp_sigs:
                pre = s['pre_temp']
                if 0 < pre < 1:
                    logit_p = math.log(pre / (1 - pre))
                    scaled = 1 / (1 + math.exp(-logit_p / t))
                else:
                    scaled = pre
                p = s['price']
                fee_pct = maker_fee(1, p) / 100.0
                edge = scaled - (p / 100) - fee_pct
                outcome = 1.0 if s['won'] else 0.0
                t_brier += (scaled - outcome) ** 2
                t_oc_sum += scaled - outcome
                if edge > 0.001:
                    n_trades += 1
                    if s['won']:
                        n_wins += 1
                    t_pnl += sim_pnl_1lot(p, s['won'])
            brier_avg = t_brier / len(temp_sigs)
            oc_avg = t_oc_sum / len(temp_sigs) * 100
            wr_t = n_wins / n_trades * 100 if n_trades > 0 else 0
            print(f"    {t:>5.2f} {brier_avg:>6.4f} {oc_avg:>+7.1f}pp {n_trades:>7} "
                  f"{wr_t:>5.0f}% ${t_pnl:>8.2f}")

    # ================================================================
    #  SECTION 5: EXHAUSTIVE CONFIGURATION SEARCH
    # ================================================================
    print("\n" + "=" * 80)
    print("  5. EXHAUSTIVE CONFIGURATION SEARCH")
    print("=" * 80)

    configs = []

    # 5a: Asset portfolios x price floors
    asset_combos = [
        ({'BTC'}, 'BTC_only'),
        ({'ETH'}, 'ETH_only'),
        ({'SOL'}, 'SOL_only'),
        ({'BTC', 'ETH'}, 'BTC_ETH'),
        ({'BTC', 'SOL'}, 'BTC_SOL'),
        ({'BTC', 'ETH', 'SOL'}, 'no_XRP'),
        ({'ETH', 'SOL', 'XRP'}, 'no_BTC'),
        ({'BTC', 'ETH', 'SOL', 'XRP'}, 'ALL'),
    ]
    price_floors = [50, 70, 80, 85, 88, 90, 92, 95]

    for assets, aname in asset_combos:
        for pf in price_floors:
            r = evaluate_config(signals, assets=assets, min_price=pf,
                                label=f"{aname}_P>={pf}")
            if r['n'] >= 20:
                configs.append(r)

    # 5b: Edge caps
    edge_caps = [0.003, 0.005, 0.007, 0.008, 0.01, 0.012, 0.015, 0.02, 0.03, 0.05]
    for assets, aname in asset_combos:
        for ec in edge_caps:
            r = evaluate_config(signals, assets=assets, max_edge=ec,
                                label=f"{aname}_edge<={ec * 100:.1f}%")
            if r['n'] >= 20:
                configs.append(r)

    # 5c: STC ranges
    stc_ranges = [(0, 300), (0, 600), (0, 900), (300, 600), (300, 900), (300, 1200),
                  (600, 900), (600, 1200), (600, 1800), (900, 1800), (1200, 1800)]
    for assets, aname in [({'BTC'}, 'BTC'), ({'BTC', 'ETH'}, 'BTC_ETH'),
                          ({'BTC', 'ETH', 'SOL', 'XRP'}, 'ALL')]:
        for stc_lo, stc_hi in stc_ranges:
            r = evaluate_config(signals, assets=assets, min_stc=stc_lo, max_stc=stc_hi,
                                label=f"{aname}_STC_{stc_lo}-{stc_hi}")
            if r['n'] >= 20:
                configs.append(r)

    # 5d: Hour-of-day sessions
    sessions = {
        'US_AM': set(range(13, 18)),
        'US_PM': set(range(18, 22)),
        'US_full': set(range(13, 22)),
        'Asia': set(range(0, 8)),
        'EU': set(range(7, 13)),
        'Night': set(range(22, 24)) | set(range(0, 6)),
        'Active': set(range(13, 22)),   # US trading hours
    }
    for sname, hours in sessions.items():
        for assets, aname in [({'BTC'}, 'BTC'), ({'BTC', 'ETH', 'SOL', 'XRP'}, 'ALL')]:
            r = evaluate_config(signals, assets=assets, hours=hours,
                                label=f"{aname}_{sname}")
            if r['n'] >= 20:
                configs.append(r)

    # 5e: Combined multi-dimensional grid
    for assets, aname in asset_combos:
        for pf in [80, 85, 88, 90, 92, 95]:
            for ec in [0.005, 0.007, 0.008, 0.01, 0.012, 0.015, 0.02]:
                r = evaluate_config(signals, assets=assets, min_price=pf, max_edge=ec,
                                    label=f"{aname}_P>={pf}_E<={ec * 100:.1f}%")
                if r['n'] >= 20:
                    configs.append(r)
            # Combined with STC
            for stc_lo, stc_hi in [(300, 900), (300, 1200), (600, 1800)]:
                r = evaluate_config(signals, assets=assets, min_price=pf,
                                    min_stc=stc_lo, max_stc=stc_hi,
                                    label=f"{aname}_P>={pf}_STC_{stc_lo}-{stc_hi}")
                if r['n'] >= 20:
                    configs.append(r)

    # 5f: Per-window position limits
    for wlimit in [1, 2, 3]:
        for assets, aname in [({'BTC'}, 'BTC'), ({'BTC', 'ETH'}, 'BTC_ETH'),
                               ({'BTC', 'ETH', 'SOL', 'XRP'}, 'ALL')]:
            r = evaluate_config(signals, assets=assets, max_signals_per_window=wlimit,
                                label=f"{aname}_wlim={wlimit}")
            if r['n'] >= 20:
                configs.append(r)

    # 5g: Shadow CalEngine probabilities
    for assets, aname in [({'BTC'}, 'BTC'), ({'BTC', 'ETH', 'SOL', 'XRP'}, 'ALL')]:
        r = evaluate_config(signals, assets=assets, use_shadow_cal=True,
                            label=f"{aname}_shadow_cal")
        if r['n'] >= 20:
            configs.append(r)

    # 5h: Combined best: asset x price x edge x STC
    for assets, aname in [({'BTC'}, 'BTC'), ({'BTC', 'ETH'}, 'BTC_ETH'),
                           ({'BTC', 'ETH', 'SOL', 'XRP'}, 'ALL')]:
        for pf in [80, 85, 90]:
            for ec in [0.008, 0.01, 0.015]:
                for stc_lo, stc_hi in [(300, 900), (300, 1200)]:
                    r = evaluate_config(signals, assets=assets, min_price=pf,
                                        max_edge=ec, min_stc=stc_lo, max_stc=stc_hi,
                                        label=f"{aname}_P>={pf}_E<={ec*100:.1f}%_STC_{stc_lo}-{stc_hi}")
                    if r['n'] >= 20:
                        configs.append(r)

    # ================================================================
    #  SECTION 6: ALPHA DISCOVERY -- RANK AND VALIDATE
    # ================================================================
    print("\n" + "=" * 80)
    print("  6. ALPHA DISCOVERY -- TOP CONFIGURATIONS")
    print("=" * 80)

    # Sort by PnL/day (more useful than raw PnL which favors larger samples)
    profitable = [c for c in configs if c['flat_pnl'] > 0 and c['n'] >= 50]
    profitable.sort(key=lambda x: x['pnl_per_day'], reverse=True)

    if not profitable:
        profitable = [c for c in configs if c['flat_pnl'] > 0 and c['n'] >= 30]
        profitable.sort(key=lambda x: x['pnl_per_day'], reverse=True)
        if profitable:
            print(f"\n  NOTE: No configs with n>=50 profitable. Showing n>=30:")

    if not profitable:
        profitable = [c for c in configs if c['flat_pnl'] > 0 and c['n'] >= 20]
        profitable.sort(key=lambda x: x['pnl_per_day'], reverse=True)
        if profitable:
            print(f"\n  NOTE: Relaxed to n>=20:")

    if not profitable:
        print(f"\n  >>> NO PROFITABLE CONFIGURATIONS FOUND")
        print(f"\n  Least-negative configs (n>=30):")
        least_bad = [c for c in configs if c['n'] >= 30]
        least_bad.sort(key=lambda x: x['flat_pnl'], reverse=True)
        profitable = least_bad[:20]

    print(f"\n  {'#':>3} {'Config':>45} {'N':>4} {'W/L':>8} {'WR':>6} "
          f"{'PnL':>8} {'$/day':>7} {'PF':>5} {'BE':>5} {'Gap':>7} "
          f"{'DD':>6} {'Stab':>5}")
    print(f"  {'-'*120}")

    for i, c in enumerate(profitable[:25], 1):
        wl = f"{c['wins']}W/{c['losses']}L"
        stable = 'YES' if c['time_stable'] else 'NO'
        dd = c.get('max_drawdown', 0)
        print(f"  {i:>3} {c['label']:>45} {c['n']:>4} {wl:>8} {c['wr']:>5.1%} "
              f"${c['flat_pnl']:>7.2f} ${c['pnl_per_day']:>6.2f} "
              f"{c['profit_factor']:>4.2f} {c['avg_be']:>4.0%} {c['wr_vs_be']:>+6.1%} "
              f"${dd:>5.2f} {stable:>5}")

    # ================================================================
    #  SECTION 7: ROBUSTNESS VALIDATION (top 5)
    # ================================================================
    print("\n" + "=" * 80)
    print("  7. ROBUSTNESS VALIDATION -- TOP 5 CANDIDATES")
    print("=" * 80)

    top5 = profitable[:5]
    for i, c in enumerate(top5, 1):
        print(f"\n  --- Candidate {i}: {c['label']} ---")
        print(f"    N={c['n']}, {c['wins']}W/{c['losses']}L, WR={c['wr']:.1%}")
        print(f"    Wilson 95% CI: [{c['wilson_lo']:.1%} - {c['wilson_hi']:.1%}]")
        print(f"    Flat PnL: ${c['flat_pnl']:.2f} (${c['pnl_per_day']:.2f}/day)")
        print(f"    Profit Factor: {c['profit_factor']:.2f}")
        print(f"    Avg Price: {c['avg_price']:.0f}c, Avg BE: {c['avg_be']:.0%}")
        print(f"    WR vs BE: {c['wr_vs_be']:+.1%}")
        print(f"    Max drawdown: ${c.get('max_drawdown', 0):.2f}")
        print(f"    Time stability: H1={c['h1_wr']:.1%} H2={c['h2_wr']:.1%} "
              f"{'STABLE' if c['time_stable'] else 'UNSTABLE'}")
        if c['h1_pnl'] is not None:
            print(f"    Half PnL: H1=${c['h1_pnl']:.2f}, H2=${c['h2_pnl']:.2f}")
        if c.get('concentration') and c['flat_pnl'] > 0:
            print(f"    Top-5 concentration: {c['concentration']:.0%} of total PnL")

        # Fisher test vs excluded trades
        excluded_w = baseline['wins'] - c['wins']
        excluded_l = baseline['losses'] - c['losses']
        if excluded_w + excluded_l > 0:
            p = fisher_exact_p(c['wins'], c['losses'], excluded_w, excluded_l)
            excl_wr = excluded_w / (excluded_w + excluded_l)
            sig_stars = '***' if p < 0.001 else '**' if p < 0.01 else '*' if p < 0.05 else 'NS'
            print(f"    Fisher vs excluded: p={p:.6f} ({sig_stars})")
            print(f"    In: {c['wr']:.1%} ({c['n']}), Out: {excl_wr:.1%} ({excluded_w + excluded_l})")

        # Wilson lower bound vs breakeven
        if c['wilson_lo'] > c['avg_be']:
            print(f"    >>> Wilson lower bound ({c['wilson_lo']:.1%}) > BE ({c['avg_be']:.0%}) = REAL EDGE")
        else:
            print(f"    >>> Wilson lower bound ({c['wilson_lo']:.1%}) < BE ({c['avg_be']:.0%}) = UNCERTAIN")

        # Asset breakdown
        if c.get('asset_stats'):
            print(f"    Assets:")
            for a, st in sorted(c['asset_stats'].items()):
                awr = st['w'] / st['n'] * 100 if st['n'] else 0
                print(f"      {a}: {st['n']}t, {st['w']}W, WR={awr:.0f}%, PnL=${st['pnl']:.2f}")

        # Robustness grade
        checks = []
        if c['n'] >= 50:
            checks.append('SAMPLE_OK')
        if c['time_stable']:
            checks.append('TIME_STABLE')
        if c['profit_factor'] > 1.3:
            checks.append('PF_STRONG')
        if c['wilson_lo'] > c['avg_be']:
            checks.append('WILSON_CLEAR')
        if c['h1_pnl'] > 0 and c['h2_pnl'] > 0:
            checks.append('BOTH_HALVES_PROFITABLE')

        grade = len(checks)
        grade_label = ('A' if grade >= 5 else 'B' if grade >= 4 else
                       'C' if grade >= 3 else 'D' if grade >= 2 else 'F')
        print(f"    Robustness grade: {grade_label} ({grade}/5 checks passed: "
              f"{', '.join(checks)})")

    # ================================================================
    #  SECTION 8: REGIME MAP
    # ================================================================
    print("\n" + "=" * 80)
    print("  8. REGIME MAP (data-driven detection)")
    print("=" * 80)

    regimes = detect_regimes(signals)
    if regimes:
        print(f"\n  {'#':>3} {'Period':>35} {'N':>4} {'WR':>6} {'Trigger':>12}")
        print(f"  {'-'*65}")
        for i, r in enumerate(regimes, 1):
            period = f"{r['start_time']} to {r['end_time']}"
            print(f"  {i:>3} {period:>35} {r['n']:>4} {r['wr']:>5.1%} {r['trigger']:>12}")

        # Compare regime performance
        if len(regimes) >= 2:
            print(f"\n  --- Regime comparison ---")
            for i, r in enumerate(regimes, 1):
                rsigs = r.get('signals', [])
                if not rsigs:
                    continue
                wins = sum(1 for s in rsigs if s['won'])
                losses = len(rsigs) - wins
                pnl = sum(sim_pnl_1lot(s['price'], s['won']) for s in rsigs)
                avg_p = sum(s['price'] for s in rsigs) / len(rsigs)
                asset_counts = defaultdict(int)
                for s in rsigs:
                    asset_counts[s['asset']] += 1
                top_asset = max(asset_counts, key=asset_counts.get)
                print(f"    Regime {i}: {wins}W/{losses}L ({wins / len(rsigs):.0%}), "
                      f"PnL=${pnl:.2f}, avg_p={avg_p:.0f}c, dominant={top_asset} "
                      f"({asset_counts[top_asset]}/{len(rsigs)})")

    # ================================================================
    #  SECTION 9: POSITION SIZING RECOMMENDATIONS
    # ================================================================
    print("\n" + "=" * 80)
    print("  9. POSITION SIZING RECOMMENDATIONS BY PRICE TIER")
    print("=" * 80)

    print(f"\n  {'Tier':>10} {'N':>4} {'WR':>6} {'BE':>6} {'Edge':>8} "
          f"{'Kelly_f':>8} {'Rec Frac':>9} {'MaxRisk':>8}")
    print(f"  {'-'*70}")

    for tier_name in tier_order:
        tier_sigs = tiers.get(tier_name, [])
        if not tier_sigs:
            continue
        wins = sum(1 for s in tier_sigs if s['won'])
        n = len(tier_sigs)
        wr = wins / n
        avg_be = sum(breakeven_wr(s['price']) for s in tier_sigs) / n
        avg_price = int(sum(s['price'] for s in tier_sigs) / n)

        # Kelly fraction computation
        if wr > avg_be:
            p = wr
            b = (100 - avg_price) / avg_price  # payout ratio
            kelly_full = (p * b - (1 - p)) / b if b > 0 else 0
            kelly_full = max(0, kelly_full)
        else:
            kelly_full = 0

        # Recommended fraction based on sample size confidence
        lo_ci, _ = wilson_ci(wins, n)
        if lo_ci > avg_be and n >= 50:
            rec_frac = min(0.25, kelly_full * 0.5)  # half-Kelly capped at 25%
            max_risk = min(0.15, rec_frac * 2)
        elif lo_ci > avg_be and n >= 30:
            rec_frac = min(0.15, kelly_full * 0.25)  # quarter-Kelly
            max_risk = 0.10
        elif wr > avg_be:
            rec_frac = min(0.10, kelly_full * 0.15)  # eighth-Kelly
            max_risk = 0.05
        else:
            rec_frac = 0
            max_risk = 0

        edge = (wr - avg_be) * 100
        print(f"  {tier_name:>10} {n:>4} {wr:>5.1%} {avg_be:>5.1%} {edge:>+7.1f}pp "
              f"{kelly_full:>7.1%} {rec_frac:>8.1%} {max_risk:>7.1%}")

    # ================================================================
    #  SECTION 10: RECOMMENDED EXPERIMENTAL CONFIGURATION
    # ================================================================
    print("\n" + "=" * 80)
    print("  10. RECOMMENDED EXPERIMENTAL CONFIGURATION")
    print("=" * 80)

    # Find best config that passes robustness checks
    best = None
    for c in profitable:
        if (c['n'] >= 50 and c['flat_pnl'] > 0 and c['time_stable']
                and c['profit_factor'] > 1.2 and c['wilson_lo'] > c['avg_be']):
            best = c
            break

    if not best:
        for c in profitable:
            if c['n'] >= 50 and c['flat_pnl'] > 0 and c['time_stable']:
                best = c
                break

    if not best:
        for c in profitable:
            if c['n'] >= 30 and c['flat_pnl'] > 0:
                best = c
                break

    if best:
        print(f"\n  RECOMMENDED: {best['label']}")
        print(f"    Expected: {best['n']} trades over {best['days']:.1f} days, "
              f"WR={best['wr']:.1%}, PnL=${best['flat_pnl']:.2f}/period")
        print(f"    $/day: ${best['pnl_per_day']:.2f}, PF={best['profit_factor']:.2f}")
        print(f"    Max drawdown: ${best.get('max_drawdown', 0):.2f}")

        # Translate label to bot config
        print(f"\n    Suggested bot config changes:")
        label = best['label']
        if 'BTC_only' in label:
            print(f"      HOURLY_EXCLUDED_ASSETS = {{'ETH', 'SOL', 'XRP'}}")
        elif 'no_XRP' in label:
            print(f"      HOURLY_EXCLUDED_ASSETS = {{'XRP'}}")
        elif 'BTC_ETH' in label:
            print(f"      HOURLY_EXCLUDED_ASSETS = {{'SOL', 'XRP'}}")

        if 'P>=' in label:
            pf_match = label.split('P>=')[1].split('_')[0]
            print(f"      HOURLY_MIN_ENTRY_PRICE = {pf_match}")

        if 'E<=' in label:
            ec_match = label.split('E<=')[1].split('%')[0]
            print(f"      # Max edge cap: {ec_match}% (requires new config)")

        if 'STC_' in label:
            parts = label.split('STC_')[1].split('_')[0]
            stc_lo, stc_hi = parts.split('-')
            print(f"      HOURLY_MIN_STC_ENTRY = {stc_lo}")
            print(f"      HOURLY_MAX_STC_ENTRY = {stc_hi}")

        validation = 'ROBUST' if (best['time_stable'] and best['n'] >= 50
                                   and best['wilson_lo'] > best['avg_be']) else 'PRELIMINARY'
        print(f"\n    Validation: {validation}")
    else:
        print(f"\n  >>> NO CONFIGURATION MEETS PROMOTION CRITERIA")
        print(f"  Closest candidates shown in Section 6.")

    # ================================================================
    #  SECTION 11: FINAL VERDICT & RECOMMENDATIONS
    # ================================================================
    print("\n" + "=" * 80)
    print("  11. FINAL VERDICT & RECOMMENDATIONS")
    print("=" * 80)

    has_alpha = any(c['flat_pnl'] > 0 and c['n'] >= 50 and c['time_stable']
                    and c['profit_factor'] > 1.3 and c['wilson_lo'] > c['avg_be']
                    for c in configs)

    marginal_alpha = any(c['flat_pnl'] > 0 and c['n'] >= 30 for c in configs)

    weak_alpha = any(c['flat_pnl'] > 0 and c['n'] >= 20 for c in configs)

    if has_alpha:
        strong = [c for c in configs if c['flat_pnl'] > 0 and c['n'] >= 50
                  and c['time_stable'] and c['profit_factor'] > 1.3
                  and c['wilson_lo'] > c['avg_be']]
        print(f"\n  VERDICT: ALPHA EXISTS ({len(strong)} robust configurations)")
        for c in strong[:5]:
            print(f"    + {c['label']}: n={c['n']}, WR={c['wr']:.1%}, "
                  f"PnL=${c['flat_pnl']:.2f}, PF={c['profit_factor']:.2f}")
    elif marginal_alpha:
        marginal = [c for c in configs if c['flat_pnl'] > 0 and c['n'] >= 30]
        print(f"\n  VERDICT: MARGINAL ALPHA ({len(marginal)} configs with positive PnL, "
              f"none fully robust)")
        for c in marginal[:5]:
            print(f"    ~ {c['label']}: n={c['n']}, WR={c['wr']:.1%}, "
                  f"PnL=${c['flat_pnl']:.2f}, PF={c['profit_factor']:.2f}, "
                  f"stable={'Y' if c['time_stable'] else 'N'}")
    elif weak_alpha:
        weak = [c for c in configs if c['flat_pnl'] > 0 and c['n'] >= 20]
        print(f"\n  VERDICT: WEAK ALPHA SIGNAL ({len(weak)} configs with n>=20 positive PnL)")
        for c in weak[:5]:
            print(f"    ? {c['label']}: n={c['n']}, WR={c['wr']:.1%}, "
                  f"PnL=${c['flat_pnl']:.2f}")
    else:
        print(f"\n  VERDICT: NO CREDIBLE ALPHA DETECTED")
        print(f"  The hourly strategy does not produce positive PnL under any tested")
        print(f"  configuration with n>=20 trades.")

    # Key structural problems
    print(f"\n  --- Structural diagnostics ---")

    # Edge inversion check
    low_edge = [s for s in signals if s['edge_val'] < 0.01]
    high_edge = [s for s in signals if s['edge_val'] >= 0.03]
    if low_edge and high_edge:
        low_wr = sum(1 for s in low_edge if s['won']) / len(low_edge)
        high_wr = sum(1 for s in high_edge if s['won']) / len(high_edge)
        if high_wr < low_wr:
            print(f"  [CRITICAL] Edge inversion: low-edge WR={low_wr:.1%} (n={len(low_edge)}) "
                  f"> high-edge WR={high_wr:.1%} (n={len(high_edge)})")
        else:
            print(f"  [OK] Edge monotonic: low={low_wr:.1%}, high={high_wr:.1%}")

    # Overconfidence
    print(f"  [{'CRITICAL' if avg_cal - actual_wr > 0.15 else 'WARNING' if avg_cal - actual_wr > 0.05 else 'OK'}] "
          f"Overconfidence: predicted {avg_cal:.1%} vs actual {actual_wr:.1%} "
          f"({(avg_cal - actual_wr) * 100:+.1f}pp)")

    # Asset-specific issues
    for asset in ['BTC', 'ETH', 'SOL', 'XRP']:
        r = asset_results.get(asset, {})
        if r and r.get('n', 0) > 0:
            if r['flat_pnl'] < -2.0:
                print(f"  [DRAG] {asset}: {r['wins']}W/{r['losses']}L ({r['wr']:.1%}), "
                      f"PnL=${r['flat_pnl']:.2f}")

    # Time stability
    if baseline['time_stable']:
        print(f"  [OK] Time stability: H1={baseline['h1_wr']:.1%}, H2={baseline['h2_wr']:.1%}")
    else:
        print(f"  [WARNING] Time instability: H1={baseline['h1_wr']:.1%}, "
              f"H2={baseline['h2_wr']:.1%} ({abs(baseline['h1_wr'] - baseline['h2_wr']) * 100:.0f}pp gap)")

    # Recommendations
    print(f"\n  --- Actionable recommendations ---")
    recommendations = []

    # 1. Asset exclusion
    worst_asset = min(asset_results.items(),
                       key=lambda x: x[1].get('flat_pnl', 0) if x[1].get('n', 0) > 5 else 0)
    if worst_asset[1].get('flat_pnl', 0) < -1.0:
        no_worst = evaluate_config(signals,
                                    assets=all_assets - {worst_asset[0]},
                                    label=f"no_{worst_asset[0]}")
        if no_worst['n'] >= 20 and no_worst['flat_pnl'] > baseline['flat_pnl']:
            delta = no_worst['flat_pnl'] - baseline['flat_pnl']
            recommendations.append(
                f"Exclude {worst_asset[0]}: saves ${abs(worst_asset[1]['flat_pnl']):.2f}, "
                f"net +${delta:.2f} PnL"
            )

    # 2. Price floor
    for pf in [85, 88, 90, 92]:
        pf_result = evaluate_config(signals, min_price=pf, label=f"P>={pf}")
        if pf_result['n'] >= 20 and pf_result['pnl_per_day'] > baseline.get('pnl_per_day', 0):
            recommendations.append(
                f"Raise MIN_ENTRY_PRICE to {pf}: {pf_result['n']}t, "
                f"WR={pf_result['wr']:.1%}, ${pf_result['pnl_per_day']:.2f}/day "
                f"(vs baseline ${baseline.get('pnl_per_day', 0):.2f}/day)"
            )
            break

    # 3. Edge cap
    best_cap = None
    best_cap_daily = -999
    for ec in [0.005, 0.008, 0.01, 0.012, 0.015]:
        ec_result = evaluate_config(signals, max_edge=ec, label=f"edge<={ec*100:.1f}%")
        if ec_result['n'] >= 20 and ec_result['pnl_per_day'] > best_cap_daily:
            best_cap = ec
            best_cap_daily = ec_result['pnl_per_day']
            best_cap_result = ec_result
    if best_cap and best_cap_daily > baseline.get('pnl_per_day', 0):
        recommendations.append(
            f"Add edge cap at {best_cap*100:.1f}%: {best_cap_result['n']}t, "
            f"WR={best_cap_result['wr']:.1%}, ${best_cap_daily:.2f}/day"
        )

    # 4. STC optimization
    best_stc = None
    best_stc_daily = -999
    for stc_lo, stc_hi in [(300, 900), (300, 1200), (600, 1200)]:
        stc_result = evaluate_config(signals, min_stc=stc_lo, max_stc=stc_hi,
                                      label=f"STC_{stc_lo}-{stc_hi}")
        if stc_result['n'] >= 20 and stc_result['pnl_per_day'] > best_stc_daily:
            best_stc = (stc_lo, stc_hi)
            best_stc_daily = stc_result['pnl_per_day']
            best_stc_result = stc_result
    if best_stc and best_stc_daily > baseline.get('pnl_per_day', 0):
        recommendations.append(
            f"STC range {best_stc[0]}-{best_stc[1]}s: {best_stc_result['n']}t, "
            f"WR={best_stc_result['wr']:.1%}, ${best_stc_daily:.2f}/day"
        )

    # 5. Position limit
    for wlim in [1, 2]:
        wlim_result = evaluate_config(signals, max_signals_per_window=wlim,
                                       label=f"wlim={wlim}")
        if wlim_result['n'] >= 20 and wlim_result['pnl_per_day'] > baseline.get('pnl_per_day', 0):
            recommendations.append(
                f"Per-window limit {wlim}: {wlim_result['n']}t, "
                f"WR={wlim_result['wr']:.1%}, ${wlim_result['pnl_per_day']:.2f}/day"
            )
            break

    if recommendations:
        for i, rec in enumerate(recommendations, 1):
            print(f"  {i}. {rec}")
    else:
        print(f"  No actionable improvements found over baseline.")
        print(f"  Continue observation data collection.")

    # Overall status
    print(f"\n  --- Promotion readiness ---")
    min_n_for_promotion = 200
    if baseline['n'] < min_n_for_promotion:
        print(f"  Status: CONTINUE OBSERVATION (n={baseline['n']}, need {min_n_for_promotion}+)")
        remaining_days = (min_n_for_promotion - baseline['n']) / max(baseline.get('per_day', 1), 0.1)
        print(f"  ETA to {min_n_for_promotion} signals: ~{remaining_days:.0f} days at current rate")
    elif not has_alpha and not marginal_alpha:
        print(f"  Status: STAY IN OBSERVATION (no alpha detected with {baseline['n']} signals)")
    elif has_alpha:
        print(f"  Status: CANDIDATE FOR PROMOTION (alpha detected, validate config)")
    else:
        print(f"  Status: MARGINAL -- collect more data before promoting")

    print(f"\n{'=' * 80}")
    print(f"  Analysis complete. {len(configs)} configurations evaluated.")
    print(f"{'=' * 80}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Hourly Strategy Alpha Analyzer (Enhanced)')
    parser.add_argument('--db', default='/tmp/state.db', help='Path to state.db')
    args = parser.parse_args()
    run_research(args.db)
