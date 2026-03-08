#!/usr/bin/env python3
"""Overnight Pattern Analysis — Pre-build data exploration.

Analyzes hourly trading patterns to identify quiet overnight periods,
counterfactual win rates on rejected signals, vol compression, and
orderbook quality changes.

Usage:
    python3 scripts/overnight_analysis.py --db /tmp/state.db
    python3 scripts/overnight_analysis.py --db /tmp/state.db --days 30
"""

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone


# ── Wilson score interval ─────────────────────────────────────────────
def wilson_ci(wins: int, n: int, z: float = 1.96):
    """95% Wilson score confidence interval."""
    if n == 0:
        return 0.0, 0.0
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return max(0, center - spread), min(1, center + spread)


# ── Fee model (maker) ────────────────────────────────────────────────
def maker_fee(contracts: int, price_cents: int) -> int:
    return math.ceil(0.0175 * contracts * price_cents * (100 - price_cents) / 100)


def utc_to_et(utc_hour: int) -> str:
    """Convert UTC hour to ET string (EST = UTC-5, EDT = UTC-4). Approximate as EST."""
    et = (utc_hour - 5) % 24
    return f"{et:02d}"


def main():
    parser = argparse.ArgumentParser(description="Overnight Pattern Analysis")
    parser.add_argument("--db", required=True, help="Path to state.db")
    parser.add_argument("--days", type=int, default=21, help="Days of history (default 21)")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")

    lookback = f"-{args.days} days"

    print("=" * 80)
    print("OVERNIGHT PATTERN ANALYSIS")
    print(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Lookback: {args.days} days")
    print("=" * 80)

    # =====================================================================
    # SECTION 1: Trade Frequency by Hour (UTC and ET)
    # =====================================================================
    print(f"\n{'━' * 80}")
    print("1. TRADE FREQUENCY BY HOUR")
    print(f"{'━' * 80}")

    # 1a: Settled trades by hour — weekday vs weekend
    trades_q = """
        SELECT CAST(strftime('%H', settled_at) AS INTEGER) as hour_utc,
               CASE WHEN strftime('%w', settled_at) IN ('0','6') THEN 'weekend' ELSE 'weekday' END as day_type,
               COUNT(*) as trades,
               SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN pnl_cents <= 0 THEN 1 ELSE 0 END) as losses,
               ROUND(AVG(pnl_cents) / 100.0, 2) as avg_pnl,
               product_type
        FROM settled_trades
        WHERE settled_at >= datetime('now', ?)
        GROUP BY hour_utc, day_type, product_type
        ORDER BY hour_utc, day_type
    """
    trades_rows = conn.execute(trades_q, (lookback,)).fetchall()

    # Aggregate into per-hour buckets
    hourly_trades = defaultdict(lambda: {"weekday": {"trades": 0, "wins": 0, "losses": 0},
                                          "weekend": {"trades": 0, "wins": 0, "losses": 0}})
    for r in trades_rows:
        h = r["hour_utc"]
        dt = r["day_type"]
        hourly_trades[h][dt]["trades"] += r["trades"]
        hourly_trades[h][dt]["wins"] += r["wins"]
        hourly_trades[h][dt]["losses"] += r["losses"]

    print(f"\n  {'Hour':>4} {'ET':>4} │ {'──── WEEKDAY ────':^20} │ {'──── WEEKEND ────':^20} │ {'TOTAL':^6}")
    print(f"  {'UTC':>4} {'':>4} │ {'Trades':>7} {'W':>4} {'L':>4} {'WR':>7} │ {'Trades':>7} {'W':>4} {'L':>4} {'WR':>7} │ {'':>6}")
    print(f"  {'─'*4} {'─'*4} ┼ {'─'*20} ┼ {'─'*20} ┼ {'─'*6}")

    total_wd_trades = 0
    total_we_trades = 0
    for h in range(24):
        wd = hourly_trades[h]["weekday"]
        we = hourly_trades[h]["weekend"]
        et = utc_to_et(h)

        wd_wr = f"{wd['wins']/wd['trades']:.0%}" if wd["trades"] else "—"
        we_wr = f"{we['wins']/we['trades']:.0%}" if we["trades"] else "—"
        total = wd["trades"] + we["trades"]
        total_wd_trades += wd["trades"]
        total_we_trades += we["trades"]

        # Histogram bar
        bar = "█" * min(total, 50)
        print(f"  {h:>4} {et:>4} │ {wd['trades']:>7} {wd['wins']:>4} {wd['losses']:>4} {wd_wr:>7} │ "
              f"{we['trades']:>7} {we['wins']:>4} {we['losses']:>4} {we_wr:>7} │ {total:>5} {bar}")

    print(f"\n  Totals: Weekday={total_wd_trades}, Weekend={total_we_trades}, "
          f"All={total_wd_trades + total_we_trades}")

    # 1b: Evaluated opportunities (candidates) by hour
    print(f"\n  ── Candidates (evaluated_opportunities) by hour ──")
    cand_q = """
        SELECT CAST(strftime('%H', evaluation_time) AS INTEGER) as hour_utc,
               CASE WHEN strftime('%w', evaluation_time) IN ('0','6') THEN 'weekend' ELSE 'weekday' END as day_type,
               filter_stage,
               COUNT(*) as n
        FROM evaluated_opportunities
        WHERE evaluation_time >= datetime('now', ?)
        GROUP BY hour_utc, day_type, filter_stage
        ORDER BY hour_utc
    """
    cand_rows = conn.execute(cand_q, (lookback,)).fetchall()

    hourly_cands = defaultdict(lambda: {"weekday": defaultdict(int), "weekend": defaultdict(int)})
    for r in cand_rows:
        hourly_cands[r["hour_utc"]][r["day_type"]][r["filter_stage"]] += r["n"]

    # Show the main filter stages
    print(f"\n  {'Hour':>4} {'ET':>4} │ {'WD Total':>9} {'WD trade':>9} {'WD obs':>7} │ {'WE Total':>9} {'WE trade':>9} {'WE obs':>7}")
    print(f"  {'─'*4} {'─'*4} ┼ {'─'*27} ┼ {'─'*27}")
    for h in range(24):
        et = utc_to_et(h)
        wd = hourly_cands[h]["weekday"]
        we = hourly_cands[h]["weekend"]
        wd_total = sum(wd.values())
        we_total = sum(we.values())
        wd_trade = wd.get("trade", 0) + wd.get("observation_trade", 0)
        we_trade = we.get("trade", 0) + we.get("observation_trade", 0)
        wd_obs = wd.get("observation_trade", 0)
        we_obs = we.get("observation_trade", 0)
        print(f"  {h:>4} {et:>4} │ {wd_total:>9} {wd_trade:>9} {wd_obs:>7} │ {we_total:>9} {we_trade:>9} {we_obs:>7}")

    # 1c: Rejections by hour
    print(f"\n  ── Rejections by hour (weekdays only, top reasons) ──")
    rej_q = """
        SELECT CAST(strftime('%H', rejection_time) AS INTEGER) as hour_utc,
               rejection_reason,
               COUNT(*) as n
        FROM rejected_opportunities
        WHERE rejection_time >= datetime('now', ?)
          AND strftime('%w', rejection_time) NOT IN ('0','6')
        GROUP BY hour_utc, rejection_reason
        ORDER BY hour_utc, n DESC
    """
    rej_rows = conn.execute(rej_q, (lookback,)).fetchall()

    hourly_rej = defaultdict(lambda: defaultdict(int))
    for r in rej_rows:
        hourly_rej[r["hour_utc"]][r["rejection_reason"]] += r["n"]

    # Get top rejection reasons overall
    all_reasons = defaultdict(int)
    for h in hourly_rej:
        for reason, n in hourly_rej[h].items():
            all_reasons[reason] += n
    top_reasons = sorted(all_reasons, key=all_reasons.get, reverse=True)[:5]

    header = f"  {'Hour':>4} {'ET':>4} │ {'Total':>6}"
    for reason in top_reasons:
        short = reason[:18]
        header += f" │ {short:>18}"
    print(header)
    print(f"  {'─'*4} {'─'*4} ┼ {'─'*6}" + "".join(f" ┼ {'─'*18}" for _ in top_reasons))

    for h in range(24):
        et = utc_to_et(h)
        total = sum(hourly_rej[h].values())
        line = f"  {h:>4} {et:>4} │ {total:>6}"
        for reason in top_reasons:
            line += f" │ {hourly_rej[h].get(reason, 0):>18}"
        print(line)

    # 1d: Identify quiet hours automatically
    print(f"\n  ── Quiet Hour Detection (weekdays) ──")
    wd_hourly_total = {}
    for h in range(24):
        wd_hourly_total[h] = hourly_trades[h]["weekday"]["trades"]

    if any(wd_hourly_total.values()):
        avg_trades = sum(wd_hourly_total.values()) / 24
        quiet_threshold = avg_trades * 0.25  # hours with < 25% of average
        quiet_hours = [h for h in range(24) if wd_hourly_total[h] <= quiet_threshold]
        active_hours = [h for h in range(24) if wd_hourly_total[h] > quiet_threshold]

        print(f"  Average trades/hour (weekday): {avg_trades:.1f}")
        print(f"  Quiet threshold (<25% of avg): {quiet_threshold:.1f} trades")
        if quiet_hours:
            # Find contiguous ranges
            ranges = []
            start = quiet_hours[0]
            prev = quiet_hours[0]
            for h in quiet_hours[1:]:
                if h == prev + 1 or (prev == 23 and h == 0):
                    prev = h
                else:
                    ranges.append((start, prev))
                    start = h
                    prev = h
            ranges.append((start, prev))

            for s, e in ranges:
                s_et = utc_to_et(s)
                e_et = utc_to_et((e + 1) % 24)
                print(f"  QUIET ZONE: {s:02d}:00-{(e+1)%24:02d}:00 UTC "
                      f"({s_et}:00-{e_et}:00 ET)")
            print(f"  All quiet hours (UTC): {sorted(quiet_hours)}")
        else:
            print("  No quiet hours detected — trades distributed evenly")
    else:
        print("  No weekday trades in lookback period")

    # =====================================================================
    # SECTION 2: Overnight Rejection Analysis (Counterfactual)
    # =====================================================================
    print(f"\n{'━' * 80}")
    print("2. OVERNIGHT REJECTION ANALYSIS — COUNTERFACTUAL")
    print(f"{'━' * 80}")

    # Determine quiet hours from data (use detected or fallback to 4-12 UTC)
    if quiet_hours:
        overnight_hours = set(quiet_hours)
    else:
        overnight_hours = set(range(4, 12))  # default guess

    overnight_str = f"UTC hours {sorted(overnight_hours)}"
    print(f"\n  Using overnight window: {overnight_str}")

    # Build the hour filter for SQL
    hour_placeholders = ",".join(str(h) for h in sorted(overnight_hours))

    # 2a: insufficient_edge rejections during overnight, weekdays
    rej_overnight_q = f"""
        SELECT r.ticker, r.asset, r.market_price, r.calibrated_prob,
               r.volatility, r.seconds_to_close, r.rejection_reason,
               r.rejection_time, r.product_type, r.raw_prob,
               r.threshold, r.spot_price,
               CAST(strftime('%H', r.rejection_time) AS INTEGER) as hour_utc
        FROM rejected_opportunities r
        WHERE r.rejection_time >= datetime('now', ?)
          AND strftime('%w', r.rejection_time) NOT IN ('0','6')
          AND CAST(strftime('%H', r.rejection_time) AS INTEGER) IN ({hour_placeholders})
          AND r.rejection_reason = 'insufficient_edge'
          AND r.product_type IN ('15m', '15M') OR (r.product_type IS NULL
              AND r.rejection_reason = 'insufficient_edge'
              AND r.rejection_time >= datetime('now', ?)
              AND strftime('%w', r.rejection_time) NOT IN ('0','6')
              AND CAST(strftime('%H', r.rejection_time) AS INTEGER) IN ({hour_placeholders}))
        ORDER BY r.rejection_time
    """
    # Simpler query - just get all insufficient_edge in overnight hours on weekdays
    rej_overnight_q2 = f"""
        SELECT r.*,
               CAST(strftime('%H', r.rejection_time) AS INTEGER) as hour_utc
        FROM rejected_opportunities r
        WHERE r.rejection_time >= datetime('now', ?)
          AND strftime('%w', r.rejection_time) NOT IN ('0','6')
          AND CAST(strftime('%H', r.rejection_time) AS INTEGER) IN ({hour_placeholders})
          AND r.rejection_reason = 'insufficient_edge'
        ORDER BY r.rejection_time
    """
    rej_overnight = conn.execute(rej_overnight_q2, (lookback,)).fetchall()
    print(f"  Overnight insufficient_edge rejections (weekdays): {len(rej_overnight)}")

    # Match against settlements via event_ticker
    # Check market_result column on rejected_opportunities
    settled_count = 0
    won_count = 0
    by_asset_rej = defaultdict(lambda: {"n": 0, "settled": 0, "wins": 0, "prices": [], "probs": []})
    by_tier_rej = defaultdict(lambda: {"n": 0, "settled": 0, "wins": 0, "prices": []})

    def price_tier(p):
        if p >= 95: return "95+"
        if p >= 93: return "93-94"
        if p >= 91: return "91-92"
        if p >= 89: return "89-90"
        return "86-88"

    for r in rej_overnight:
        asset = r["asset"]
        price = r["market_price"]
        tier = price_tier(price) if price else "unknown"
        prob = r["calibrated_prob"]

        by_asset_rej[asset]["n"] += 1
        by_asset_rej[asset]["prices"].append(price or 0)
        if prob:
            by_asset_rej[asset]["probs"].append(prob)

        by_tier_rej[tier]["n"] += 1
        by_tier_rej[tier]["prices"].append(price or 0)

        # Check if this rejection has settlement info
        result = r["market_result"]
        if result:
            settled_count += 1
            by_asset_rej[asset]["settled"] += 1
            by_tier_rej[tier]["settled"] += 1
            if result in ("yes", "all_yes"):
                won_count += 1
                by_asset_rej[asset]["wins"] += 1
                by_tier_rej[tier]["wins"] += 1

    print(f"  Settled: {settled_count} / {len(rej_overnight)}")
    if settled_count > 0:
        wr = won_count / settled_count
        lo, hi = wilson_ci(won_count, settled_count)
        print(f"  Counterfactual WR: {wr:.1%} ({won_count}W/{settled_count - won_count}L)")
        print(f"  95% CI: [{lo:.1%}, {hi:.1%}]")

    # 2b: By asset
    print(f"\n  ── By Asset ──")
    print(f"  {'Asset':<8} {'Rej':>5} {'Settled':>8} {'W':>4} {'L':>4} {'WR':>8} {'Avg Price':>10} {'Avg Prob':>10}")
    for asset in sorted(by_asset_rej):
        d = by_asset_rej[asset]
        s = d["settled"]
        w = d["wins"]
        l = s - w
        wr_str = f"{w/s:.1%}" if s else "—"
        avg_p = f"{sum(d['prices'])/len(d['prices']):.0f}c" if d["prices"] else "—"
        avg_prob = f"{sum(d['probs'])/len(d['probs']):.3f}" if d["probs"] else "—"
        print(f"  {asset:<8} {d['n']:>5} {s:>8} {w:>4} {l:>4} {wr_str:>8} {avg_p:>10} {avg_prob:>10}")

    # 2c: By price tier
    print(f"\n  ── By Price Tier ──")
    print(f"  {'Tier':<8} {'Rej':>5} {'Settled':>8} {'W':>4} {'L':>4} {'WR':>8}")
    for tier in ["86-88", "89-90", "91-92", "93-94", "95+"]:
        if tier not in by_tier_rej:
            continue
        d = by_tier_rej[tier]
        s = d["settled"]
        w = d["wins"]
        l = s - w
        wr_str = f"{w/s:.1%}" if s else "—"
        print(f"  {tier:<8} {d['n']:>5} {s:>8} {w:>4} {l:>4} {wr_str:>8}")

    # 2d: Compare overnight vs daytime WR on same tiers
    print(f"\n  ── Overnight vs Daytime Counterfactual WR (weekdays) ──")
    daytime_hours_str = ",".join(str(h) for h in range(24) if h not in overnight_hours)
    daytime_q = f"""
        SELECT r.market_price, r.market_result,
               CAST(strftime('%H', r.rejection_time) AS INTEGER) as hour_utc
        FROM rejected_opportunities r
        WHERE r.rejection_time >= datetime('now', ?)
          AND strftime('%w', r.rejection_time) NOT IN ('0','6')
          AND CAST(strftime('%H', r.rejection_time) AS INTEGER) IN ({daytime_hours_str})
          AND r.rejection_reason = 'insufficient_edge'
          AND r.market_result IS NOT NULL
        ORDER BY r.rejection_time
    """
    daytime_rej = conn.execute(daytime_q, (lookback,)).fetchall()

    day_by_tier = defaultdict(lambda: {"settled": 0, "wins": 0})
    for r in daytime_rej:
        tier = price_tier(r["market_price"]) if r["market_price"] else "unknown"
        day_by_tier[tier]["settled"] += 1
        if r["market_result"] in ("yes", "all_yes"):
            day_by_tier[tier]["wins"] += 1

    print(f"  {'Tier':<8} │ {'── OVERNIGHT ──':^18} │ {'── DAYTIME ──':^18} │ {'Δ WR':>6}")
    print(f"  {'─'*8} ┼ {'─'*18} ┼ {'─'*18} ┼ {'─'*6}")
    for tier in ["86-88", "89-90", "91-92", "93-94", "95+"]:
        o = by_tier_rej.get(tier, {"settled": 0, "wins": 0})
        d = day_by_tier.get(tier, {"settled": 0, "wins": 0})
        o_wr = f"{o['wins']/o['settled']:.0%}" if o["settled"] else "—"
        d_wr = f"{d['wins']/d['settled']:.0%}" if d["settled"] else "—"
        o_str = f"{o.get('wins',0)}W/{o['settled']-o.get('wins',0)}L {o_wr}"
        d_str = f"{d['wins']}W/{d['settled']-d['wins']}L {d_wr}"
        delta = ""
        if o["settled"] and d["settled"]:
            delta = f"{(o['wins']/o['settled'] - d['wins']/d['settled'])*100:+.0f}pp"
        print(f"  {tier:<8} │ {o_str:>18} │ {d_str:>18} │ {delta:>6}")

    # =====================================================================
    # SECTION 3: Overnight Vol Profile
    # =====================================================================
    print(f"\n{'━' * 80}")
    print("3. OVERNIGHT VOLATILITY PROFILE")
    print(f"{'━' * 80}")

    vol_q = """
        SELECT asset,
               CAST(strftime('%H', evaluation_time) AS INTEGER) as hour_utc,
               COUNT(*) as n,
               AVG(volatility) as avg_vol,
               MIN(volatility) as min_vol,
               MAX(volatility) as max_vol,
               AVG(volatility * volatility) as avg_vol_sq
        FROM evaluated_opportunities
        WHERE evaluation_time >= datetime('now', ?)
          AND volatility IS NOT NULL
          AND volatility > 0
          AND strftime('%w', evaluation_time) NOT IN ('0','6')
        GROUP BY asset, hour_utc
        ORDER BY asset, hour_utc
    """
    vol_rows = conn.execute(vol_q, (lookback,)).fetchall()

    assets_vol = defaultdict(dict)
    for r in vol_rows:
        assets_vol[r["asset"]][r["hour_utc"]] = {
            "n": r["n"], "avg": r["avg_vol"], "min": r["min_vol"],
            "max": r["max_vol"], "std": math.sqrt(max(0, r["avg_vol_sq"] - r["avg_vol"]**2))
        }

    for asset in sorted(assets_vol):
        print(f"\n  ── {asset} ──")
        hours_data = assets_vol[asset]
        if not hours_data:
            print("  No data")
            continue

        all_vols = [hours_data[h]["avg"] for h in hours_data if hours_data[h]["avg"]]
        overall_avg = sum(all_vols) / len(all_vols) if all_vols else 0

        print(f"  {'Hour':>4} {'ET':>4} │ {'Avg Vol':>10} {'vs Avg':>8} {'N':>6} {'Min':>10} {'Max':>10}")
        for h in range(24):
            et = utc_to_et(h)
            if h in hours_data and hours_data[h]["avg"]:
                d = hours_data[h]
                pct_vs_avg = ((d["avg"] / overall_avg) - 1) * 100 if overall_avg else 0
                is_overnight = "◀" if h in overnight_hours else ""
                print(f"  {h:>4} {et:>4} │ {d['avg']:>10.6f} {pct_vs_avg:>+7.1f}% {d['n']:>6} "
                      f"{d['min']:>10.6f} {d['max']:>10.6f} {is_overnight}")
            else:
                print(f"  {h:>4} {et:>4} │ {'—':>10}")

        # Compute overnight vs daytime avg
        overnight_vols = [hours_data[h]["avg"] for h in overnight_hours
                         if h in hours_data and hours_data[h]["avg"]]
        daytime_vols = [hours_data[h]["avg"] for h in range(24)
                       if h not in overnight_hours and h in hours_data and hours_data[h]["avg"]]

        if overnight_vols and daytime_vols:
            on_avg = sum(overnight_vols) / len(overnight_vols)
            dt_avg = sum(daytime_vols) / len(daytime_vols)
            compression = ((on_avg / dt_avg) - 1) * 100
            print(f"\n  Overnight avg: {on_avg:.6f}")
            print(f"  Daytime avg:   {dt_avg:.6f}")
            print(f"  Compression:   {compression:+.1f}%")

    # 3b: Night-to-night consistency
    print(f"\n  ── Night-to-Night Volatility Consistency ──")
    nightly_vol_q = f"""
        SELECT DATE(evaluation_time) as eval_date,
               asset,
               AVG(volatility) as avg_vol,
               COUNT(*) as n
        FROM evaluated_opportunities
        WHERE evaluation_time >= datetime('now', ?)
          AND volatility IS NOT NULL AND volatility > 0
          AND strftime('%w', evaluation_time) NOT IN ('0','6')
          AND CAST(strftime('%H', evaluation_time) AS INTEGER) IN ({hour_placeholders})
        GROUP BY eval_date, asset
        ORDER BY eval_date, asset
    """
    nightly_rows = conn.execute(nightly_vol_q, (lookback,)).fetchall()

    by_asset_nightly = defaultdict(list)
    for r in nightly_rows:
        by_asset_nightly[r["asset"]].append({"date": r["eval_date"], "vol": r["avg_vol"], "n": r["n"]})

    for asset in sorted(by_asset_nightly):
        nights = by_asset_nightly[asset]
        if len(nights) < 2:
            continue
        vols = [n["vol"] for n in nights]
        avg = sum(vols) / len(vols)
        std = math.sqrt(sum((v - avg)**2 for v in vols) / len(vols))
        cv = std / avg if avg else 0
        print(f"  {asset}: {len(nights)} nights, avg vol={avg:.6f}, std={std:.6f}, "
              f"CV={cv:.2f} ({'consistent' if cv < 0.3 else 'variable' if cv < 0.6 else 'highly variable'})")

    # =====================================================================
    # SECTION 4: Orderbook Quality Overnight
    # =====================================================================
    print(f"\n{'━' * 80}")
    print("4. ORDERBOOK QUALITY OVERNIGHT")
    print(f"{'━' * 80}")

    # Use evaluated_opportunities which has market_price (best_ask), ask_depth, best_ask_source
    ob_q = """
        SELECT CAST(strftime('%H', evaluation_time) AS INTEGER) as hour_utc,
               CASE WHEN strftime('%w', evaluation_time) IN ('0','6') THEN 'weekend' ELSE 'weekday' END as day_type,
               COUNT(*) as n,
               AVG(ask_depth) as avg_depth,
               AVG(CASE WHEN best_ask_source = 'maker' THEN 1.0 ELSE 0.0 END) as maker_fill_pct,
               COUNT(DISTINCT asset) as assets_active
        FROM evaluated_opportunities
        WHERE evaluation_time >= datetime('now', ?)
          AND filter_stage IN ('trade', 'observation_trade', 'candidate')
        GROUP BY hour_utc, day_type
        ORDER BY hour_utc
    """
    ob_rows = conn.execute(ob_q, (lookback,)).fetchall()

    hourly_ob = defaultdict(lambda: {"weekday": {}, "weekend": {}})
    for r in ob_rows:
        hourly_ob[r["hour_utc"]][r["day_type"]] = {
            "n": r["n"], "avg_depth": r["avg_depth"],
            "maker_pct": r["maker_fill_pct"], "assets": r["assets_active"]
        }

    print(f"\n  {'Hour':>4} {'ET':>4} │ {'WD N':>6} {'Depth':>7} {'Maker%':>8} │ {'WE N':>6} {'Depth':>7} {'Maker%':>8}")
    print(f"  {'─'*4} {'─'*4} ┼ {'─'*23} ┼ {'─'*23}")
    for h in range(24):
        et = utc_to_et(h)
        wd = hourly_ob[h].get("weekday", {})
        we = hourly_ob[h].get("weekend", {})
        wd_n = wd.get("n", 0)
        we_n = we.get("n", 0)
        wd_depth = f"{wd['avg_depth']:.0f}" if wd.get("avg_depth") else "—"
        we_depth = f"{we['avg_depth']:.0f}" if we.get("avg_depth") else "—"
        wd_maker = f"{wd['maker_pct']:.0%}" if wd.get("maker_pct") is not None else "—"
        we_maker = f"{we['maker_pct']:.0%}" if we.get("maker_pct") is not None else "—"
        is_on = "◀" if h in overnight_hours else ""
        print(f"  {h:>4} {et:>4} │ {wd_n:>6} {wd_depth:>7} {wd_maker:>8} │ "
              f"{we_n:>6} {we_depth:>7} {we_maker:>8} {is_on}")

    # 4b: Escalation type breakdown by hour (maker vs taker)
    print(f"\n  ── Fill Type by Hour (settled trades) ──")
    esc_q = """
        SELECT CAST(strftime('%H', settled_at) AS INTEGER) as hour_utc,
               escalation_type,
               COUNT(*) as n,
               SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as wins
        FROM settled_trades
        WHERE settled_at >= datetime('now', ?)
          AND strftime('%w', settled_at) NOT IN ('0','6')
        GROUP BY hour_utc, escalation_type
        ORDER BY hour_utc
    """
    esc_rows = conn.execute(esc_q, (lookback,)).fetchall()

    hourly_esc = defaultdict(lambda: defaultdict(lambda: {"n": 0, "wins": 0}))
    for r in esc_rows:
        etype = r["escalation_type"] or "unknown"
        hourly_esc[r["hour_utc"]][etype]["n"] += r["n"]
        hourly_esc[r["hour_utc"]][etype]["wins"] += r["wins"]

    all_etypes = set()
    for h in hourly_esc:
        all_etypes.update(hourly_esc[h].keys())
    etypes_sorted = sorted(all_etypes)

    if etypes_sorted:
        header = f"  {'Hour':>4} {'ET':>4}"
        for et in etypes_sorted:
            header += f" │ {et[:12]:>12}"
        print(header)
        for h in range(24):
            et = utc_to_et(h)
            line = f"  {h:>4} {et:>4}"
            for etype in etypes_sorted:
                d = hourly_esc[h].get(etype, {"n": 0, "wins": 0})
                if d["n"]:
                    line += f" │ {d['n']:>5} ({d['wins']}W)"
                else:
                    line += f" │ {'—':>12}"
            print(line)

    # =====================================================================
    # SECTION 5: Summary & Recommendations
    # =====================================================================
    print(f"\n{'━' * 80}")
    print("5. SUMMARY & OVERNIGHT SHADOW READINESS")
    print(f"{'━' * 80}")

    # Summarize findings
    if quiet_hours:
        et_quiet = [utc_to_et(h) for h in sorted(quiet_hours)]
        print(f"\n  Quiet hours (UTC): {sorted(quiet_hours)}")
        print(f"  Quiet hours (ET):  {et_quiet}")
    else:
        print("\n  No clear quiet period detected")

    if settled_count > 0:
        wr = won_count / settled_count
        lo, hi = wilson_ci(won_count, settled_count)
        print(f"\n  Overnight insufficient_edge counterfactual:")
        print(f"    WR: {wr:.1%} ({won_count}W/{settled_count - won_count}L, n={settled_count})")
        print(f"    95% CI: [{lo:.1%}, {hi:.1%}]")
        print(f"    Threshold for shadow: >= 85%: {'PASS' if wr >= 0.85 else 'FAIL'}")
    else:
        print("\n  No settled counterfactual data available")

    # Vol compression summary
    print(f"\n  Volatility compression (overnight vs daytime):")
    for asset in sorted(assets_vol):
        hours_data = assets_vol[asset]
        overnight_vols = [hours_data[h]["avg"] for h in overnight_hours
                         if h in hours_data and hours_data[h]["avg"]]
        daytime_vols = [hours_data[h]["avg"] for h in range(24)
                       if h not in overnight_hours and h in hours_data and hours_data[h]["avg"]]
        if overnight_vols and daytime_vols:
            on_avg = sum(overnight_vols) / len(overnight_vols)
            dt_avg = sum(daytime_vols) / len(daytime_vols)
            compression = ((on_avg / dt_avg) - 1) * 100
            print(f"    {asset}: {compression:+.1f}%")

    # Go/No-Go checklist
    print(f"\n  ── OVERNIGHT SHADOW GO/NO-GO ──")
    checks = []

    # Check 1: Clear quiet period
    if quiet_hours and len(quiet_hours) >= 3:
        checks.append(("Clear quiet period (3+ hours)", True,
                       f"{len(quiet_hours)} hours identified"))
    else:
        checks.append(("Clear quiet period (3+ hours)", False,
                       f"{'No quiet hours' if not quiet_hours else f'Only {len(quiet_hours)} hours'}"))

    # Check 2: Counterfactual WR >= 85%
    if settled_count >= 10:
        wr = won_count / settled_count
        if wr >= 0.85:
            checks.append(("Counterfactual WR >= 85%", True, f"{wr:.1%} (n={settled_count})"))
        else:
            checks.append(("Counterfactual WR >= 85%", False, f"{wr:.1%} (n={settled_count})"))
    else:
        checks.append(("Counterfactual WR >= 85%", None,
                       f"Insufficient data (n={settled_count}, need 10+)"))

    # Check 3: Vol compression is the cause (not orderbook issues)
    # If vol drops but orderbook quality stays reasonable → yes
    checks.append(("Vol compression identified as primary cause", None,
                   "Review Section 3 manually"))

    for name, ok, detail in checks:
        icon = "PASS" if ok else ("FAIL" if ok is False else "????")
        print(f"  [{icon}] {name}: {detail}")

    print(f"\n{'=' * 80}")
    conn.close()


if __name__ == "__main__":
    main()
