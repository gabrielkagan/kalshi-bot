#!/usr/bin/env python3
"""
Buy-Low-Sell-Higher Shadow Analysis

Reads evaluated_opportunities and computes a scorecard for the buy-low strategy:
"if YES was buyable at price X with real depth, did the bid later reach price Y?"

This is offline analysis only. No trading decisions are made.

Usage:
    python3 scripts/buy_low_analysis.py [--db state.db] [--days 7] [--min-depth 10]

Validation criteria for going live:
    - n >= 50 entries at chosen entry/exit pair
    - Hit rate >= 60% with Wilson 95% CI lower bound > 45%
    - Average per-trade EV > 5c after fees
    - Same signal works on out-of-sample data (split by date)
"""

import argparse
import math
import sqlite3
import sys
from collections import defaultdict


def wilson_ci(wins, n, z=1.96):
    """Wilson score interval for binomial proportion."""
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (center - margin, center + margin)


def find_entry_candidates(conn, days, min_depth):
    """Find scan-time observations that match buy-low entry criteria.

    YES-side filter: raw_prob > 0.5 and filter_stage NOT in NO-side stages.
    """
    cur = conn.execute(
        """
        SELECT id, ticker, asset, evaluation_time, market_price as ask, yes_bid_cents as bid,
               ask_depth, raw_prob, calibrated_prob, fee_adjusted_edge,
               seconds_to_close, spot_price, threshold, market_result, filter_stage
        FROM evaluated_opportunities
        WHERE product_type='15m'
            AND market_price IS NOT NULL
            AND market_price < 60
            AND best_ask_source = 'orderbook'
            AND ask_depth IS NOT NULL
            AND ask_depth >= ?
            AND raw_prob IS NOT NULL
            AND raw_prob > 0.55
            AND seconds_to_close >= 60
            AND seconds_to_close <= 700
            AND filter_stage NOT LIKE '%no_side%'
            AND filter_stage NOT LIKE '%hourly%'
            AND market_result IS NOT NULL
            AND evaluation_time > datetime('now', ?)
        ORDER BY ticker, evaluation_time
        """,
        (min_depth, f"-{days} days"),
    )
    return [dict(r) for r in cur]


def find_subsequent_observations(conn, ticker, after_time):
    """Get all later observations for the same ticker (for tracking bid evolution)."""
    cur = conn.execute(
        """
        SELECT evaluation_time, market_price as ask, yes_bid_cents as bid, ask_depth
        FROM evaluated_opportunities
        WHERE ticker = ?
            AND evaluation_time > ?
            AND raw_prob > 0.5
            AND product_type='15m'
        ORDER BY evaluation_time
        """,
        (ticker, after_time),
    )
    return [dict(r) for r in cur]


def analyze_entries(entries, conn):
    """For each entry, find the max bid and ask reached afterward.

    Returns a list of result dicts with entry + best subsequent bid/ask.
    """
    # Dedup: keep only the FIRST entry per ticker (avoid double-counting same setup)
    seen_tickers = set()
    deduped = []
    for e in entries:
        if e["ticker"] in seen_tickers:
            continue
        seen_tickers.add(e["ticker"])
        deduped.append(e)

    results = []
    for e in deduped:
        subsequent = find_subsequent_observations(conn, e["ticker"], e["evaluation_time"])
        bids = [s["bid"] for s in subsequent if s["bid"] is not None]
        asks = [s["ask"] for s in subsequent if s["ask"] is not None]
        result = dict(e)
        result["max_bid_after"] = max(bids) if bids else None
        result["max_ask_after"] = max(asks) if asks else None
        result["n_subsequent"] = len(subsequent)
        results.append(result)
    return results


def scorecard(results):
    """Compute hit rates for various entry/exit price combinations."""
    print("\n=== ENTRY/EXIT SCORECARD ===")
    print("Entry: YES ask < X, exit: max bid >= Y reached during hold")
    print()

    entry_buckets = [(20, 30), (30, 40), (40, 50), (50, 60)]
    exit_targets = [40, 50, 60, 70, 80, 90]

    for ent_lo, ent_hi in entry_buckets:
        bucket = [r for r in results if ent_lo <= r["ask"] < ent_hi]
        if not bucket:
            print(f"\nEntry {ent_lo}-{ent_hi}c: NO DATA")
            continue
        # Drop entries where we have no subsequent bid data at all
        with_bid_data = [r for r in bucket if r["max_bid_after"] is not None]
        n_total = len(bucket)
        n_with_bid = len(with_bid_data)

        print(f"\nEntry {ent_lo}-{ent_hi}c: n={n_total} (n_with_bid={n_with_bid})")
        print(f"  Settled YES: {sum(1 for r in bucket if r['market_result']=='yes')}/{n_total}")

        for target in exit_targets:
            if target <= ent_hi:
                continue
            hits = sum(1 for r in with_bid_data if r["max_bid_after"] >= target)
            if n_with_bid > 0:
                rate = hits / n_with_bid * 100
                lo, hi = wilson_ci(hits, n_with_bid)
                # Estimated profit per contract (assume entry at midpoint)
                entry_mid = (ent_lo + ent_hi) // 2
                profit = target - entry_mid
                # Approximate fee (taker on both sides, ~1c each)
                fee = 2
                net = profit - fee
                ev = (hits / n_with_bid) * net + (1 - hits / n_with_bid) * 0  # ignoring downside for now
                print(
                    f"  Bid >= {target}c: {hits}/{n_with_bid} ({rate:.1f}%)  "
                    f"CI [{lo*100:.1f}%, {hi*100:.1f}%]  "
                    f"profit={net}c  EV~{ev:.1f}c"
                )


def daily_volume(results):
    """How many entries per day?"""
    print("\n=== DAILY VOLUME ===")
    by_day = defaultdict(int)
    for r in results:
        day = r["evaluation_time"][:10]
        by_day[day] += 1
    for day in sorted(by_day):
        print(f"  {day}: {by_day[day]} entries")


def asset_breakdown(results):
    """Per-asset entry counts."""
    print("\n=== PER-ASSET BREAKDOWN ===")
    by_asset = defaultdict(int)
    for r in results:
        by_asset[r["asset"]] += 1
    for asset in sorted(by_asset, key=lambda a: -by_asset[a]):
        print(f"  {asset}: {by_asset[asset]} entries")


def data_health(conn, days):
    """How much bid data has been captured since the column was added?"""
    print("\n=== DATA HEALTH ===")
    cur = conn.execute(
        """
        SELECT COUNT(*) as total,
               SUM(CASE WHEN yes_bid_cents IS NOT NULL THEN 1 ELSE 0 END) as with_bid
        FROM evaluated_opportunities
        WHERE product_type='15m'
          AND evaluation_time > datetime('now', ?)
        """,
        (f"-{days} days",),
    )
    r = cur.fetchone()
    total, with_bid = r["total"], r["with_bid"]
    pct = with_bid / total * 100 if total else 0
    print(f"  Total 15M evals: {total}")
    print(f"  With yes_bid_cents: {with_bid} ({pct:.1f}%)")
    if pct < 5:
        print("  WARNING: bid data is sparse — may not have been populated yet, or scanner has no orderbook access")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="state.db", help="Path to state.db")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days")
    parser.add_argument("--min-depth", type=int, default=10, help="Minimum ask depth")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row

    print(f"Analyzing last {args.days} days, min ask depth {args.min_depth}")
    data_health(conn, args.days)

    entries = find_entry_candidates(conn, args.days, args.min_depth)
    print(f"\nFound {len(entries)} raw entry observations")

    if not entries:
        print("\nNo entries match criteria. Either:")
        print("  1. yes_bid_cents column was added recently — wait for data to accumulate")
        print("  2. The bot's scanner isn't seeing orderbook data at low prices")
        print("  3. The buy-low setup is rare (this is the suspected reality)")
        return

    results = analyze_entries(entries, conn)
    print(f"After dedup (1 per ticker): {len(results)} entries")

    daily_volume(results)
    asset_breakdown(results)
    scorecard(results)


if __name__ == "__main__":
    main()
