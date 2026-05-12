#!/usr/bin/env python3
"""Weekend Edge Discount Shadow — Audit & Graduation Report.

Reads from evaluated_opportunities table (source of truth after settlement).
Shows performance of the 0.6x weekend edge discount shadow signals.

Usage:
    python3 scripts/weekend_discount_audit.py --db /tmp/state.db
    python3 scripts/weekend_discount_audit.py --db /tmp/state.db --asset SOL
    python3 scripts/weekend_discount_audit.py --db /tmp/state.db --discount 0.7
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
    """Maker fee: ceil(0.0175 * C * P * (100-P) / 100)."""
    return math.ceil(0.0175 * contracts * price_cents * (100 - price_cents) / 100)


def audit_discount_shadow(conn, filter_stage, label, schedule_desc, rows, args):
    """Shared audit logic for weekend and overnight discount shadows."""
    if not rows:
        print("=" * 70)
        print(f"{label} — NO DATA")
        print("=" * 70)
        print(f"\nNo {filter_stage} signals found in DB.")
        print(f"This feature activates {schedule_desc}.")
        print("If recently deployed, wait for the next qualifying period.")
        return

    # ── Categorize ────────────────────────────────────────────────────
    total = len(rows)
    settled = [r for r in rows if r["status"] == "settled" and r["won"] is not None]
    pending = [r for r in rows if r["status"] != "settled" or r["won"] is None]
    wins = sum(1 for r in settled if r["won"] == 1)
    losses = len(settled) - wins

    sim_pnl_cents = sum(r["counterfactual_pnl"] or 0 for r in settled)

    print("=" * 70)
    print(f"{label} — AUDIT REPORT")
    print(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    if args.asset:
        print(f"Filter: asset={args.asset.upper()}")
    if args.discount:
        print(f"Override discount: {args.discount}")
    print("=" * 70)

    # ── Section 1: Summary ────────────────────────────────────────────
    print(f"\n{'─'*50}")
    print("1. SUMMARY")
    print(f"{'─'*50}")
    print(f"  Total signals:  {total}")
    print(f"  Settled:        {len(settled)}")
    print(f"  Pending:        {len(pending)}")
    if settled:
        wr = wins / len(settled)
        lo, hi = wilson_ci(wins, len(settled))
        print(f"  Wins / Losses:  {wins}W / {losses}L")
        print(f"  Win Rate:       {wr:.1%}  (95% CI: [{lo:.1%}, {hi:.1%}])")
        print(f"  Sim PnL:        ${sim_pnl_cents / 100:.2f}")
    else:
        print("  (no settled signals yet)")

    # ── Section 2: By Asset ───────────────────────────────────────────
    print(f"\n{'─'*50}")
    print("2. BY ASSET")
    print(f"{'─'*50}")
    by_asset = defaultdict(lambda: {"n": 0, "settled": 0, "wins": 0, "pnl": 0})
    for r in rows:
        a = r["asset"]
        by_asset[a]["n"] += 1
        if r["status"] == "settled" and r["won"] is not None:
            by_asset[a]["settled"] += 1
            if r["won"]:
                by_asset[a]["wins"] += 1
            by_asset[a]["pnl"] += r["counterfactual_pnl"] or 0

    print(f"  {'Asset':<8} {'Sig':>5} {'Settled':>8} {'W':>4} {'L':>4} {'WR':>8} {'95% CI':>16} {'PnL':>10}")
    for a in sorted(by_asset):
        d = by_asset[a]
        s = d["settled"]
        w = d["wins"]
        l = s - w
        wr_str = f"{w/s:.1%}" if s else "—"
        lo, hi = wilson_ci(w, s) if s else (0, 0)
        ci_str = f"[{lo:.1%}, {hi:.1%}]" if s else "—"
        pnl_str = f"${d['pnl']/100:.2f}" if s else "—"
        print(f"  {a:<8} {d['n']:>5} {s:>8} {w:>4} {l:>4} {wr_str:>8} {ci_str:>16} {pnl_str:>10}")

    # ── Section 3: By Price Tier ──────────────────────────────────────
    print(f"\n{'─'*50}")
    print("3. BY PRICE TIER")
    print(f"{'─'*50}")

    def tier(price):
        if price >= 95: return "95+"
        if price >= 93: return "93-94"
        if price >= 91: return "91-92"
        if price >= 89: return "89-90"
        return "86-88"

    tier_thresholds = {"86-88": 0.25, "89-90": 0.25, "91-92": 0.35, "93-94": 0.90, "95+": 1.25}

    by_tier = defaultdict(lambda: {"n": 0, "settled": 0, "wins": 0, "pnl": 0, "edges": []})
    for r in rows:
        t = tier(r["market_price"])
        by_tier[t]["n"] += 1
        if r["fee_adjusted_edge"]:
            by_tier[t]["edges"].append(r["fee_adjusted_edge"])
        if r["status"] == "settled" and r["won"] is not None:
            by_tier[t]["settled"] += 1
            if r["won"]:
                by_tier[t]["wins"] += 1
            by_tier[t]["pnl"] += r["counterfactual_pnl"] or 0

    print(f"  {'Tier':<8} {'Thr%':>6} {'Sig':>5} {'Settled':>8} {'WR':>8} {'Avg Edge':>10} {'PnL':>10}")
    for t in ["86-88", "89-90", "91-92", "93-94", "95+"]:
        if t not in by_tier:
            continue
        d = by_tier[t]
        s = d["settled"]
        w = d["wins"]
        wr_str = f"{w/s:.1%}" if s else "—"
        avg_e = f"{sum(d['edges'])/len(d['edges'])*100:.2f}%" if d["edges"] else "—"
        pnl_str = f"${d['pnl']/100:.2f}" if s else "—"
        thr = tier_thresholds.get(t, 0)
        print(f"  {t:<8} {thr:>5.2f}% {d['n']:>5} {s:>8} {wr_str:>8} {avg_e:>10} {pnl_str:>10}")

    # ── Section 4: Per-Period Breakdown ───────────────────────────────
    print(f"\n{'─'*50}")
    print("4. PER-PERIOD BREAKDOWN")
    print(f"{'─'*50}")
    by_period = defaultdict(lambda: {"n": 0, "settled": 0, "wins": 0, "pnl": 0})
    for r in rows:
        wk = r["week_num"]
        dt = r["eval_date"]
        key = f"W{wk} ({dt[:10]})"
        by_period[key]["n"] += 1
        if r["status"] == "settled" and r["won"] is not None:
            by_period[key]["settled"] += 1
            if r["won"]:
                by_period[key]["wins"] += 1
            by_period[key]["pnl"] += r["counterfactual_pnl"] or 0

    print(f"  {'Period':<25} {'Sig':>5} {'Settled':>8} {'W/L':>8} {'WR':>8} {'PnL':>10}")
    for wk in sorted(by_period):
        d = by_period[wk]
        s = d["settled"]
        w = d["wins"]
        l = s - w
        wr_str = f"{w/s:.1%}" if s else "—"
        pnl_str = f"${d['pnl']/100:.2f}" if s else "—"
        wl_str = f"{w}W/{l}L" if s else "—"
        print(f"  {wk:<25} {d['n']:>5} {s:>8} {wl_str:>8} {wr_str:>8} {pnl_str:>10}")

    # ── Section 5: Edge Distribution ──────────────────────────────────
    print(f"\n{'─'*50}")
    print("5. EDGE DISTRIBUTION (fee-adjusted)")
    print(f"{'─'*50}")
    edges = [r["fee_adjusted_edge"] for r in rows if r["fee_adjusted_edge"] is not None]
    if edges:
        edges_sorted = sorted(edges)
        n = len(edges_sorted)
        print(f"  Count:    {n}")
        print(f"  Min:      {edges_sorted[0]*100:.3f}%")
        print(f"  P25:      {edges_sorted[n//4]*100:.3f}%")
        print(f"  Median:   {edges_sorted[n//2]*100:.3f}%")
        print(f"  P75:      {edges_sorted[3*n//4]*100:.3f}%")
        print(f"  Max:      {edges_sorted[-1]*100:.3f}%")
        print(f"  Mean:     {sum(edges)/n*100:.3f}%")

    # ── Section 6: Graduation Assessment ──────────────────────────────
    print(f"\n{'─'*50}")
    print("6. GRADUATION ASSESSMENT")
    print(f"{'─'*50}")

    GRAD_MIN_SETTLED = 60
    GRAD_MIN_WR = 0.85
    GRAD_MIN_ASSET_WR = 0.75

    n_settled = len(settled)
    checks = []

    if n_settled >= GRAD_MIN_SETTLED:
        checks.append(("Sample size >= 60", True, f"{n_settled} settled"))
    else:
        checks.append(("Sample size >= 60", False, f"{n_settled}/{GRAD_MIN_SETTLED} ({GRAD_MIN_SETTLED - n_settled} more needed)"))

    if n_settled > 0:
        overall_wr = wins / n_settled
        lo, _ = wilson_ci(wins, n_settled)
        if overall_wr >= GRAD_MIN_WR:
            checks.append(("Overall WR >= 85%", True, f"{overall_wr:.1%} (CI lower: {lo:.1%})"))
        else:
            checks.append(("Overall WR >= 85%", False, f"{overall_wr:.1%} (need {GRAD_MIN_WR:.0%})"))
    else:
        checks.append(("Overall WR >= 85%", False, "No settled data"))

    asset_issues = []
    for a in sorted(by_asset):
        d = by_asset[a]
        if d["settled"] >= 5:
            a_wr = d["wins"] / d["settled"]
            if a_wr < GRAD_MIN_ASSET_WR:
                asset_issues.append(f"{a}: {a_wr:.1%}")
    if not asset_issues:
        checks.append(("No asset WR < 75%", True, "All assets above threshold"))
    else:
        checks.append(("No asset WR < 75%", False, ", ".join(asset_issues)))

    tier_order = ["86-88", "89-90", "91-92", "93-94", "95+"]
    tier_wrs = {}
    for t in tier_order:
        if t in by_tier and by_tier[t]["settled"] >= 3:
            tier_wrs[t] = by_tier[t]["wins"] / by_tier[t]["settled"]
    if tier_wrs:
        worst_tier = min(tier_wrs, key=tier_wrs.get)
        if tier_wrs[worst_tier] >= 0.70:
            checks.append(("No edge inversion", True, f"Worst tier: {worst_tier} at {tier_wrs[worst_tier]:.1%}"))
        else:
            checks.append(("No edge inversion", False, f"{worst_tier} at {tier_wrs[worst_tier]:.1%} — dragging"))
    else:
        checks.append(("No edge inversion", None, "Insufficient tier data"))

    passing = sum(1 for _, ok, _ in checks if ok is True)
    total_checks = sum(1 for _, ok, _ in checks if ok is not None)

    for name, ok, detail in checks:
        icon = "PASS" if ok else ("FAIL" if ok is False else "N/A ")
        print(f"  [{icon}] {name}: {detail}")

    print()
    if n_settled < GRAD_MIN_SETTLED:
        est_signals_per_period = total / max(1, len(by_period))
        remaining = GRAD_MIN_SETTLED - n_settled
        est_periods = math.ceil(remaining / max(1, est_signals_per_period))
        print(f"  VERDICT: NEED MORE DATA")
        print(f"  Estimated {est_periods} more period(s) needed at ~{est_signals_per_period:.0f} signals/period")
    elif passing == total_checks:
        print(f"  VERDICT: YES — READY TO PROMOTE")
        print(f"  All {passing}/{total_checks} checks pass.")
    else:
        print(f"  VERDICT: NO — NOT READY ({passing}/{total_checks} checks pass)")
        for name, ok, detail in checks:
            if ok is False:
                print(f"    - Fix needed: {name} — {detail}")

    # ── Section 7: Recent Signals (last 10) ───────────────────────────
    print(f"\n{'─'*50}")
    print("7. RECENT SIGNALS (last 10)")
    print(f"{'─'*50}")
    recent = rows[-10:]
    print(f"  {'Time':<20} {'Asset':<6} {'Price':>6} {'Edge%':>7} {'Result':>8} {'PnL':>8}")
    for r in recent:
        t = r["evaluation_time"][:19] if r["evaluation_time"] else "?"
        edge_str = f"{r['fee_adjusted_edge']*100:.2f}%" if r["fee_adjusted_edge"] else "?"
        if r["won"] == 1:
            res = "WIN"
            pnl = f"${(r['counterfactual_pnl'] or 0)/100:.2f}"
        elif r["won"] == 0:
            res = "LOSS"
            pnl = f"${(r['counterfactual_pnl'] or 0)/100:.2f}"
        else:
            res = "pending"
            pnl = "—"
        print(f"  {t:<20} {r['asset']:<6} {r['market_price']:>5}c {edge_str:>7} {res:>8} {pnl:>8}")

    print(f"\n{'='*70}")


def _fetch_shadow_rows(conn, filter_stage, asset=None):
    """Fetch shadow signal rows for a given filter_stage."""
    query = f"""
        SELECT e.*,
            CASE WHEN status='settled' AND market_result IN ('yes','all_yes') THEN 1
                 WHEN status='settled' AND market_result IN ('no','all_no') THEN 0
                 ELSE NULL END as won,
            DATE(evaluation_time) as eval_date,
            strftime('%W', evaluation_time) as week_num
        FROM evaluated_opportunities e
        WHERE filter_stage = ?
    """
    params = [filter_stage]
    if asset:
        query += " AND asset = ?"
        params.append(asset.upper())
    query += " ORDER BY evaluation_time"
    return conn.execute(query, params).fetchall()


def main():
    parser = argparse.ArgumentParser(description="Quiet-Market Edge Discount Shadow Audit")
    parser.add_argument("--db", required=True, help="Path to state.db")
    parser.add_argument("--asset", default=None, help="Filter to specific asset")
    parser.add_argument("--discount", type=float, default=None,
                        help="Override discount factor for what-if analysis")
    parser.add_argument("--weekend-only", action="store_true", help="Only show weekend section")
    parser.add_argument("--overnight-only", action="store_true", help="Only show overnight section")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")

    show_weekend = not args.overnight_only
    show_overnight = not args.weekend_only

    if show_weekend:
        rows = _fetch_shadow_rows(conn, "weekend_discount_shadow", args.asset)
        audit_discount_shadow(
            conn, "weekend_discount_shadow",
            "WEEKEND EDGE DISCOUNT SHADOW",
            "on Saturdays and Sundays (UTC) only",
            rows, args,
        )

    if show_weekend and show_overnight:
        print("\n\n")

    if show_overnight:
        rows = _fetch_shadow_rows(conn, "overnight_discount_shadow", args.asset)
        audit_discount_shadow(
            conn, "overnight_discount_shadow",
            "OVERNIGHT EDGE DISCOUNT SHADOW",
            "on weekday quiet hours (04:00-11:00 UTC / 23:00-06:00 ET)",
            rows, args,
        )

    conn.close()


if __name__ == "__main__":
    main()
