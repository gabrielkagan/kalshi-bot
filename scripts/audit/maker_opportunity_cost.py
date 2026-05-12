#!/usr/bin/env python3
"""Maker Opportunity Cost Tracker.

Computes the hypothetical P&L if every unfilled maker order had been
submitted as a taker instead.  Uses evaluated_opportunities (candidates
with order_outcome='unfilled') joined to settled_trades for settlement
outcomes.

Usage:
    python3 scripts/maker_opportunity_cost.py --db /tmp/state.db
    python3 scripts/maker_opportunity_cost.py --db /tmp/state.db --since 2026-03-03
    python3 scripts/maker_opportunity_cost.py --db /tmp/state.db --asset SOL
"""

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone


TAKER_FEE_MULT = 0.07


def run(db_path: str, since: str = None, asset_filter: str = None):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=10000")

    since_clause = f"AND e.evaluation_time >= '{since}'" if since else ""
    asset_clause = f"AND e.asset = '{asset_filter}'" if asset_filter else ""

    # ── 1. Fill rate ─────────────────────────────────────────────────────
    stats = c.execute(f"""
        SELECT
            SUM(CASE WHEN order_outcome='filled' THEN 1 ELSE 0 END) as filled,
            SUM(CASE WHEN order_outcome='unfilled' THEN 1 ELSE 0 END) as unfilled,
            SUM(CASE WHEN order_outcome IS NULL THEN 1 ELSE 0 END) as pending,
            COUNT(*) as total
        FROM evaluated_opportunities e
        WHERE e.filter_stage = 'candidate'
          AND e.side = 'yes'
          {since_clause} {asset_clause}
    """).fetchone()

    filled = stats["filled"] or 0
    unfilled = stats["unfilled"] or 0
    submitted = filled + unfilled

    print("=" * 70)
    print("  MAKER OPPORTUNITY COST REPORT")
    print("=" * 70)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"  Report time: {now}")
    print(f"  DB: {db_path}")
    if since:
        print(f"  Since: {since}")
    if asset_filter:
        print(f"  Asset: {asset_filter}")
    print()

    print("=" * 70)
    print("  1. FILL RATE")
    print("=" * 70)
    fill_rate = 100 * filled / submitted if submitted > 0 else 0
    print(f"  Submitted: {submitted}  (filled: {filled}, unfilled: {unfilled})")
    print(f"  Fill rate: {fill_rate:.0f}%")
    print(f"  Pending/no-outcome: {stats['pending'] or 0}")
    print()

    # ── 2. Unfilled opportunity cost ─────────────────────────────────────
    rows = c.execute(f"""
        SELECT e.ticker, e.event_ticker, e.asset, e.market_price,
               e.position_size, e.calibrated_prob, e.fee_adjusted_edge,
               e.evaluation_time, e.taker_ask_at_submit
        FROM evaluated_opportunities e
        WHERE e.filter_stage = 'candidate'
          AND e.order_outcome = 'unfilled'
          AND e.side = 'yes'
          {since_clause} {asset_clause}
        ORDER BY e.evaluation_time DESC
    """).fetchall()

    shadow_count = 0  # orders with actual taker_ask_at_submit data
    won = lost = pending_settle = 0
    pnl_total = 0
    by_asset = defaultdict(lambda: {"n": 0, "w": 0, "l": 0, "pnl": 0, "sz": 0})
    details = []

    for r in rows:
        ticker = r["ticker"]
        # Use shadow taker price (best ask at maker submit) when available,
        # fall back to market_price (best ask at evaluation time)
        taker_ask = r["taker_ask_at_submit"]
        has_shadow = taker_ask is not None
        if has_shadow:
            shadow_count += 1
        mp = taker_ask if has_shadow else r["market_price"]
        sz = r["position_size"] or 1
        a = r["asset"]

        # Find settlement
        settle = c.execute(
            "SELECT market_result FROM settled_trades WHERE ticker=? LIMIT 1",
            (ticker,),
        ).fetchone()
        if not settle and r["event_ticker"]:
            settle = c.execute(
                "SELECT market_result FROM settled_trades WHERE event_ticker=? LIMIT 1",
                (r["event_ticker"],),
            ).fetchone()

        result = settle["market_result"] if settle else None

        fee = math.ceil(TAKER_FEE_MULT * sz * (mp / 100.0) * (1 - mp / 100.0) * 100)
        if result in ("yes", "all_yes"):
            hyp = (100 - mp) * sz - fee
            won += 1
        elif result in ("no", "all_no"):
            hyp = -(mp * sz) - fee
            lost += 1
        else:
            hyp = None
            pending_settle += 1

        if hyp is not None:
            pnl_total += hyp
            d = by_asset[a]
            d["n"] += 1
            d["sz"] += sz
            if result in ("yes", "all_yes"):
                d["w"] += 1
            else:
                d["l"] += 1
            d["pnl"] += hyp
        else:
            by_asset[a]["n"] += 1
            by_asset[a]["sz"] += sz

        details.append(
            {
                "ticker": ticker,
                "asset": a,
                "mp": mp,
                "size": sz,
                "result": result or "pending",
                "hyp_pnl": hyp,
                "fee_edge": r["fee_adjusted_edge"],
                "cal_prob": r["calibrated_prob"],
                "time": r["evaluation_time"],
                "shadow": has_shadow,
            }
        )

    settled = won + lost

    print("=" * 70)
    print("  2. UNFILLED MAKER OPPORTUNITY COST")
    print("=" * 70)
    if settled > 0:
        wr = 100 * won / settled
        print(f"  Settled: {settled} ({won}W/{lost}L, {wr:.0f}% WR)")
    print(f"  Pending settlement: {pending_settle}")
    print(f"  Hypothetical taker PnL: ${pnl_total / 100:+.2f}")
    total_unfilled = len(rows)
    pct_shadow = 100 * shadow_count / total_unfilled if total_unfilled > 0 else 0
    print(f"  Shadow taker data: {shadow_count}/{total_unfilled} ({pct_shadow:.0f}%)")
    if shadow_count < total_unfilled:
        print(f"  ({total_unfilled - shadow_count} orders use eval-time ask as fallback)")
    print()

    # ── 3. Per-asset breakdown ───────────────────────────────────────────
    print("=" * 70)
    print("  3. PER-ASSET BREAKDOWN")
    print("=" * 70)
    print(
        f"  {'Asset':<6} {'Unfilled':>8} {'W':>3} {'L':>3} {'WR':>6}"
        f" {'Hyp PnL':>10} {'Avg Sz':>7}"
    )
    print("  " + "-" * 50)
    for a in sorted(by_asset):
        d = by_asset[a]
        s = d["w"] + d["l"]
        wr_s = f"{100 * d['w'] / s:.0f}%" if s > 0 else "-"
        avg_sz = d["sz"] / d["n"] if d["n"] > 0 else 0
        print(
            f"  {a:<6} {d['n']:>8} {d['w']:>3} {d['l']:>3} {wr_s:>6}"
            f" ${d['pnl'] / 100:>+9.2f} {avg_sz:>6.0f}"
        )
    print()

    # ── 4. Comparison to actual PnL ──────────────────────────────────────
    actual_pnl = (
        c.execute(
            f"""SELECT SUM(pnl_cents - COALESCE(fee_cents, 0)) FROM settled_trades
            WHERE event_ticker NOT LIKE '%D-%'
            {'AND settled_at >= ?' if since else ''}""",
            (since,) if since else (),
        ).fetchone()[0]
        or 0
    )
    actual_fees = (
        c.execute(
            f"""SELECT SUM(COALESCE(fee_cents, 0)) FROM settled_trades
            WHERE event_ticker NOT LIKE '%D-%'
            {'AND settled_at >= ?' if since else ''}""",
            (since,) if since else (),
        ).fetchone()[0]
        or 0
    )

    # Extra fees if current maker fills had been taker
    maker_fills = c.execute(
        f"""SELECT entry_price_cents, count FROM settled_trades
        WHERE event_ticker NOT LIKE '%D-%'
          AND fee_cents = 0
          {'AND settled_at >= ?' if since else ''}""",
        (since,) if since else (),
    ).fetchall()
    extra_taker_fee = 0
    for mf in maker_fills:
        p, ct = mf[0], mf[1]
        extra_taker_fee += math.ceil(
            TAKER_FEE_MULT * ct * (p / 100.0) * (1 - p / 100.0) * 100
        )

    print("=" * 70)
    print("  4. CAPTURE RATE & ALL-TAKER COMPARISON")
    print("=" * 70)
    theoretical = actual_pnl + pnl_total
    capture = 100 * actual_pnl / theoretical if theoretical > 0 else 0
    net_all_taker = actual_pnl + pnl_total - extra_taker_fee

    print(f"  Actual PnL (filled trades):   ${actual_pnl / 100:>+9.2f}")
    print(f"  Missed PnL (unfilled makers): ${pnl_total / 100:>+9.2f}")
    print(f"  Theoretical max:              ${theoretical / 100:>+9.2f}")
    print(f"  Capture rate:                 {capture:.0f}%")
    print()
    print(f"  Fee adjustment (maker→taker): -${extra_taker_fee / 100:.2f}")
    print(f"  Net all-taker PnL:            ${net_all_taker / 100:>+9.2f}")
    opp_cost = (pnl_total - extra_taker_fee) / 100.0
    pct = 100 * opp_cost / (actual_pnl / 100.0) if actual_pnl > 0 else 0
    print(f"  Opportunity cost of maker:    ${opp_cost:>+9.2f} ({pct:.0f}% of PnL)")
    print()

    # ── 5. Individual trades ─────────────────────────────────────────────
    print("=" * 70)
    print("  5. INDIVIDUAL UNFILLED ORDERS (most recent 30)")
    print("=" * 70)
    print(
        f"  {'Ticker':<36} {'Asset':>4} {'MP':>3} {'Sz':>4}"
        f" {'Result':>7} {'Hyp PnL':>8} {'FeeEdge':>8} {'Src':>3}"
    )
    print("  " + "-" * 80)
    for d in details[:30]:
        fe = f"{d['fee_edge']:.4f}" if d["fee_edge"] else "-"
        if d["hyp_pnl"] is not None:
            pnl_s = f"${d['hyp_pnl'] / 100:>+7.2f}"
        else:
            pnl_s = "   pend"
        src = " S" if d["shadow"] else " F"
        print(
            f"  {d['ticker']:<36} {d['asset']:>4} {d['mp']:>3}c"
            f" {d['size']:>4} {d['result']:>7} {pnl_s} {fe:>8} {src:>3}"
        )

    print()
    print("=" * 70)
    print("  VERDICT")
    print("=" * 70)
    if opp_cost > 0:
        print(
            f"  Maker-first is leaving ${opp_cost:.2f} on the table"
            f" ({pct:.0f}% of actual PnL)."
        )
        print(f"  Fill rate: {fill_rate:.0f}%. Unfilled WR: {100*won/settled:.0f}%.")
        if pct > 50:
            print("  CONSIDER: All-taker would substantially outperform maker-first.")
        elif pct > 20:
            print("  MONITOR: Material opportunity cost. Worth testing taker-first.")
        else:
            print("  OK: Maker savings outweigh opportunity cost.")
    else:
        print("  Maker-first is net positive — unfilled orders were losers.")

    c.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Maker opportunity cost tracker")
    parser.add_argument("--db", required=True, help="Path to state.db")
    parser.add_argument("--since", default=None, help="Filter since date")
    parser.add_argument("--asset", default=None, help="Filter to one asset")
    args = parser.parse_args()
    run(args.db, args.since, args.asset)
