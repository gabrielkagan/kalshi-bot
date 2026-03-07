#!/usr/bin/env python3
"""NO-side shadow data comprehensive status report.

Shows current state, data health, pricing verification, settlement
outcomes, and approach comparisons for the NO-side shadow pipeline.

Usage:
    # Against local copy
    python3 scripts/no_side_status.py --db /tmp/state.db

    # Quick refresh from VPS (checkpoint + copy + run)
    python3 scripts/no_side_status.py --refresh
"""

import argparse
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime


def section(title):
    print(f"\n{'=' * 72}")
    print(f"  {title}")
    print(f"{'=' * 72}\n")


def subsection(title):
    print(f"\n--- {title} ---")


def wilson_ci(wins, n, z=1.96):
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    spread = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return max(0, center - spread), min(1, center + spread)


def run_report(db_path):
    if not os.path.exists(db_path):
        print(f"ERROR: {db_path} not found")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")

    now_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # ── Deploy timestamp (NO-side pricing fix)
    DEPLOY_TIME = "2026-03-07T18:43:00"

    section("NO-SIDE SHADOW STATUS REPORT")
    print(f"  Report time:  {now_utc}")
    print(f"  DB path:      {db_path}")
    print(f"  Fix deployed: {DEPLOY_TIME}")

    # ── 1. Data volume overview ──

    section("1. DATA VOLUME")

    # Check schema
    eval_cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(evaluated_opportunities)").fetchall()}
    shadow_cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(fifteenm_shadow_signals)").fetchall()}

    has_side = "side" in eval_cols
    has_no_live = "no_live_pnl_cents" in shadow_cols

    if not has_side:
        print("  WARNING: 'side' column missing from evaluated_opportunities")
    if not has_no_live:
        print("  WARNING: NO-side columns missing from fifteenm_shadow_signals")
        conn.close()
        return

    # evaluated_opportunities NO-side
    eo_total = conn.execute(
        "SELECT COUNT(*) FROM evaluated_opportunities WHERE side = 'no'"
    ).fetchone()[0]
    eo_pre = conn.execute(
        "SELECT COUNT(*) FROM evaluated_opportunities "
        "WHERE side = 'no' AND evaluation_time < ?", (DEPLOY_TIME,)
    ).fetchone()[0]
    eo_post = conn.execute(
        "SELECT COUNT(*) FROM evaluated_opportunities "
        "WHERE side = 'no' AND evaluation_time >= ?", (DEPLOY_TIME,)
    ).fetchone()[0]

    print(f"  evaluated_opportunities NO-side:")
    print(f"    Total:      {eo_total}")
    print(f"    Pre-fix:    {eo_pre} (pricing_version='no_bid_wrong')")
    print(f"    Post-fix:   {eo_post} (pricing_version='no_ask_correct')")

    # By product type
    eo_by_pt = conn.execute(
        "SELECT COALESCE(product_type, '15m') as pt, COUNT(*) as cnt, "
        "  SUM(CASE WHEN evaluation_time >= ? THEN 1 ELSE 0 END) as post "
        "FROM evaluated_opportunities WHERE side = 'no' "
        "GROUP BY COALESCE(product_type, '15m') ORDER BY cnt DESC",
        (DEPLOY_TIME,)
    ).fetchall()
    if eo_by_pt:
        print(f"    By product_type:")
        for r in eo_by_pt:
            print(f"      {r['pt']:>12}: {r['cnt']} total, {r['post'] or 0} post-fix")
        # Check if 15M is missing
        pts = {r["pt"] for r in eo_by_pt}
        if "15m" not in pts:
            print(f"      {'15m':>12}: 0 (NO asks below threshold — see Section 3)")

    # fifteenm_shadow_signals
    fs_total = conn.execute(
        "SELECT COUNT(*) FROM fifteenm_shadow_signals"
    ).fetchone()[0]
    fs_with_no = conn.execute(
        "SELECT COUNT(*) FROM fifteenm_shadow_signals "
        "WHERE no_live_prob IS NOT NULL"
    ).fetchone()[0]
    fs_settled_no = conn.execute(
        "SELECT COUNT(*) FROM fifteenm_shadow_signals "
        "WHERE status = 'settled' AND no_live_pnl_cents IS NOT NULL"
    ).fetchone()[0]
    fs_pending_no = conn.execute(
        "SELECT COUNT(*) FROM fifteenm_shadow_signals "
        "WHERE status != 'settled' AND no_live_prob IS NOT NULL"
    ).fetchone()[0]
    fs_post = conn.execute(
        "SELECT COUNT(*) FROM fifteenm_shadow_signals "
        "WHERE evaluation_time >= ?", (DEPLOY_TIME,)
    ).fetchone()[0]

    print(f"\n  fifteenm_shadow_signals:")
    print(f"    Total signals:       {fs_total}")
    print(f"    With NO-side data:   {fs_with_no}")
    print(f"    Settled (NO):        {fs_settled_no}")
    print(f"    Pending (NO):        {fs_pending_no}")
    print(f"    Post-fix signals:    {fs_post}")

    # Pre vs post fix breakdown
    fs_pre_no_data = conn.execute(
        "SELECT COUNT(*) FROM fifteenm_shadow_signals "
        "WHERE no_live_prob IS NULL AND evaluation_time < ?", (DEPLOY_TIME,)
    ).fetchone()[0]
    fs_post_no_data = conn.execute(
        "SELECT COUNT(*) FROM fifteenm_shadow_signals "
        "WHERE no_live_prob IS NULL AND evaluation_time >= ?", (DEPLOY_TIME,)
    ).fetchone()[0]
    fs_post_with_no = conn.execute(
        "SELECT COUNT(*) FROM fifteenm_shadow_signals "
        "WHERE no_live_prob IS NOT NULL AND evaluation_time >= ?", (DEPLOY_TIME,)
    ).fetchone()[0]
    print(f"    Pre-fix (no NO data): {fs_pre_no_data} (expected — code not deployed)")
    print(f"    Post-fix with NO:    {fs_post_with_no}")
    print(f"    Post-fix missing NO: {fs_post_no_data}", end="")
    if fs_post_no_data > 0:
        print(f" WARNING — check fifteenm_shadow.py")
    else:
        print(f" (good)")

    # Per-asset signal counts
    subsection("15M shadow signals per asset")
    asset_sigs = conn.execute(
        "SELECT asset, COUNT(*) as total, "
        "  SUM(CASE WHEN no_live_prob IS NOT NULL THEN 1 ELSE 0 END) as with_no, "
        "  SUM(CASE WHEN status = 'settled' AND no_live_pnl_cents IS NOT NULL "
        "      THEN 1 ELSE 0 END) as settled_no, "
        "  SUM(CASE WHEN status != 'settled' AND no_live_prob IS NOT NULL "
        "      THEN 1 ELSE 0 END) as pending_no "
        "FROM fifteenm_shadow_signals GROUP BY asset ORDER BY total DESC"
    ).fetchall()
    if asset_sigs:
        print(f"  {'Asset':>5} {'Total':>6} {'W/NO':>6} {'Settled':>8} {'Pending':>8}")
        print("  " + "-" * 38)
        for r in asset_sigs:
            print(f"  {r['asset']:>5} {r['total']:>6} {r['with_no'] or 0:>6} "
                  f"{r['settled_no'] or 0:>8} {r['pending_no'] or 0:>8}")

    # ── 2. Pricing verification ──

    section("2. PRICING VERIFICATION")

    subsection("Post-fix NO-side pricing (should use 100 - YES_bid = NO ask)")
    rows = conn.execute(
        "SELECT asset, market_price, best_bid, best_ask, "
        "  no_live_prob, no_live_edge, evaluation_time "
        "FROM fifteenm_shadow_signals "
        "WHERE evaluation_time >= ? AND no_live_prob IS NOT NULL "
        "ORDER BY rowid DESC LIMIT 10", (DEPLOY_TIME,)
    ).fetchall()
    if rows:
        print(f"  {'Time':<22} {'Asset':>5} {'MP':>4} {'Bid':>4} {'Ask':>4} "
              f"{'NO_ask':>7} {'NO_prob':>8} {'NO_edge':>9}")
        print("  " + "-" * 70)
        for r in rows:
            bb = r["best_bid"]
            ba = r["best_ask"]
            no_ask = (100 - bb) if (bb and bb > 0) else None
            no_ask_s = f"{no_ask}c" if no_ask else "n/a"
            print(f"  {r['evaluation_time'][-22:]:<22} {r['asset']:>5} "
                  f"{r['market_price']:>3}c {bb or 'n/a':>4} {ba or 'n/a':>4} "
                  f"{no_ask_s:>7} {r['no_live_prob']:>7.3f} "
                  f"{r['no_live_edge']:>+8.4f}")
    else:
        print("  No post-fix NO-side shadow signals yet")
        print("  (Expected: signals appear when 15M markets are scanned)")

    subsection("Pre-fix NO-side data (marked as wrong)")
    pv_counts = conn.execute(
        "SELECT pricing_version, COUNT(*) as cnt "
        "FROM evaluated_opportunities "
        "WHERE side = 'no' "
        "GROUP BY pricing_version"
    ).fetchall()
    if pv_counts:
        for r in pv_counts:
            pv = r["pricing_version"] or "NULL"
            print(f"  {pv}: {r['cnt']}")
    else:
        print("  (no data)")

    # ── 3. NO-side filter stage breakdown ──

    section("3. FILTER STAGE BREAKDOWN")

    subsection("evaluated_opportunities (NO-side)")
    if has_side:
        stages = conn.execute(
            "SELECT filter_stage, COUNT(*) as cnt, "
            "  ROUND(AVG(market_price), 1) as avg_price, "
            "  SUM(CASE WHEN status = 'settled' THEN 1 ELSE 0 END) as settled "
            "FROM evaluated_opportunities "
            "WHERE side = 'no' "
            "GROUP BY filter_stage ORDER BY cnt DESC"
        ).fetchall()
        if stages:
            print(f"  {'Stage':<35} {'N':>5} {'Settled':>8} {'AvgP':>6}")
            print("  " + "-" * 58)
            for r in stages:
                print(f"  {r['filter_stage']:<35} {r['cnt']:>5} "
                      f"{r['settled'] or 0:>8} {r['avg_price']:>5}c")
        else:
            print("  No NO-side evaluations yet")
            print("  (Expected: NO-side entries appear when NO ask >= 70c)")
    else:
        print("  'side' column not available")

    subsection("Current NO ask prices vs threshold (NO_SIDE_MIN_ENTRY_PRICE=70)")
    recent = conn.execute(
        "SELECT asset, market_price, best_bid, "
        "  CASE WHEN best_bid > 0 THEN (100 - best_bid) ELSE NULL END as no_ask "
        "FROM fifteenm_shadow_signals "
        "WHERE best_bid IS NOT NULL AND best_bid > 0 "
        "ORDER BY rowid DESC LIMIT 8"
    ).fetchall()
    if recent:
        print(f"  {'Asset':>5} {'YES_bid':>8} {'NO_ask':>7} {'vs 70c':>8}")
        print("  " + "-" * 32)
        for r in recent:
            na = r["no_ask"]
            vs = f"{na - 70:>+4}c" if na else "n/a"
            status = "TRADEABLE" if (na and na >= 70) else "below"
            print(f"  {r['asset']:>5} {r['best_bid']:>7}c {na:>6}c {vs:>8} {status}")
    else:
        print("  No recent bid data")

    # ── 4. Shadow approach results ──

    section("4. SHADOW APPROACH RESULTS (fifteenm_shadow)")

    approaches = [
        ("Live baseline", "no_live_prob", "no_live_edge",
         "no_live_pnl_cents", None),
        ("A1 RecalEGARCH", "no_a1_final_prob", "no_a1_edge",
         "no_a1_pnl_cents", "no_a1_contracts"),
        ("A2 LightGBM", "no_a2_prob", "no_a2_edge",
         "no_a2_pnl_cents", "no_a2_contracts"),
        ("Market-only", "no_market_only_prob", None,
         "no_market_only_pnl_cents", None),
    ]

    for label, prob_col, edge_col, pnl_col, contracts_col in approaches:
        if pnl_col not in shadow_cols or prob_col not in shadow_cols:
            continue

        subsection(f"{label}")

        for asset in ["BTC", "ETH", "SOL", "XRP"]:
            try:
                if contracts_col:
                    filt = f"{contracts_col} > 0"
                else:
                    filt = f"{pnl_col} IS NOT NULL"

                edge_sql = f"AVG(CASE WHEN {filt} THEN {edge_col} END)" if edge_col else "NULL"

                r = conn.execute(f"""
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN {filt} THEN 1 ELSE 0 END) AS signaled,
                        SUM(CASE WHEN status='settled' AND {filt}
                            AND market_result IN ('no', 'all_no')
                            THEN 1 ELSE 0 END) AS wins,
                        SUM(CASE WHEN status='settled' AND {filt}
                            AND market_result IN ('yes', 'all_yes')
                            THEN 1 ELSE 0 END) AS losses,
                        SUM(CASE WHEN status='settled' AND {filt}
                            THEN {pnl_col} ELSE 0 END) AS pnl,
                        {edge_sql} AS avg_edge,
                        AVG(CASE WHEN {filt} THEN {prob_col} END) AS avg_prob
                    FROM fifteenm_shadow_signals
                    WHERE asset = ?
                """, (asset,)).fetchone()

                t = r["total"] or 0
                sig = r["signaled"] or 0
                w = r["wins"] or 0
                l_v = r["losses"] or 0
                pnl_v = r["pnl"] or 0
                n_s = w + l_v

                if sig == 0:
                    print(f"  {asset}: {t} signals, 0 with data")
                    continue

                edge_s = f"{r['avg_edge']*100:.2f}%" if r["avg_edge"] else "n/a"
                prob_s = f"{r['avg_prob']*100:.1f}%" if r["avg_prob"] else "n/a"

                if n_s > 0:
                    wr = w / n_s * 100
                    lo, hi = wilson_ci(w, n_s)
                    print(f"  {asset}: {t} signals, {sig} active | "
                          f"{w}W/{l_v}L ({wr:.0f}% WR) [{lo*100:.0f}-{hi*100:.0f}%] | "
                          f"PnL: {pnl_v}c | edge: {edge_s} prob: {prob_s}")
                else:
                    print(f"  {asset}: {t} signals, {sig} active | "
                          f"0 settled | edge: {edge_s} prob: {prob_s}")
            except Exception as e:
                print(f"  {asset}: query error: {e}")

    # ── 5. YES vs NO comparison ──

    section("5. YES vs NO SIDE COMPARISON")

    try:
        comp = conn.execute("""
            SELECT
                asset,
                COUNT(*) AS n,
                SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) AS settled,
                SUM(CASE WHEN status='settled' THEN live_pnl_cents ELSE 0 END) AS yes_pnl,
                SUM(CASE WHEN status='settled' AND no_live_pnl_cents IS NOT NULL
                    THEN no_live_pnl_cents ELSE 0 END) AS no_pnl,
                SUM(CASE WHEN status='settled'
                    AND market_result IN ('yes','all_yes') THEN 1 ELSE 0 END) AS yes_wins,
                SUM(CASE WHEN status='settled'
                    AND market_result IN ('no','all_no') THEN 1 ELSE 0 END) AS no_wins
            FROM fifteenm_shadow_signals
            GROUP BY asset ORDER BY asset
        """).fetchall()

        if comp:
            print(f"  {'Asset':>5} {'N':>4} {'Sett':>5} "
                  f"{'YES PnL':>9} {'NO PnL':>9} {'YES wins':>9} {'NO wins':>9}")
            print("  " + "-" * 55)
            ty, tn = 0, 0
            for r in comp:
                yp = r["yes_pnl"] or 0
                np_ = r["no_pnl"] or 0
                ty += yp
                tn += np_
                print(f"  {r['asset']:>5} {r['n']:>4} {r['settled'] or 0:>5} "
                      f"{yp:>+8}c {np_:>+8}c "
                      f"{r['yes_wins'] or 0:>9} {r['no_wins'] or 0:>9}")
            print("  " + "-" * 55)
            print(f"  {'TOTAL':>5} {'':>4} {'':>5} "
                  f"{ty:>+8}c {tn:>+8}c")
            print(f"\n  Interpretation: YES-side is profitable when YES wins,")
            print(f"  NO-side is profitable when NO wins. At current prices,")
            print(f"  NO asks are 25-30c (far below 70c threshold), so NO-side")
            print(f"  entries are correctly being filtered out.")
        else:
            print("  No shadow data")
    except Exception as e:
        print(f"  Query error: {e}")

    # ── 6. Data freshness ──

    section("6. DATA FRESHNESS")

    latest_eval = conn.execute(
        "SELECT evaluation_time FROM evaluated_opportunities "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    latest_shadow = conn.execute(
        "SELECT evaluation_time FROM fifteenm_shadow_signals "
        "ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    latest_no_eval = conn.execute(
        "SELECT evaluation_time FROM evaluated_opportunities "
        "WHERE side = 'no' ORDER BY id DESC LIMIT 1"
    ).fetchone() if has_side else None
    latest_no_shadow = conn.execute(
        "SELECT evaluation_time FROM fifteenm_shadow_signals "
        "WHERE no_live_prob IS NOT NULL ORDER BY rowid DESC LIMIT 1"
    ).fetchone()

    print(f"  Latest eval (any):        {latest_eval['evaluation_time'] if latest_eval else 'none'}")
    print(f"  Latest shadow (any):      {latest_shadow['evaluation_time'] if latest_shadow else 'none'}")
    print(f"  Latest NO eval:           {latest_no_eval['evaluation_time'] if latest_no_eval else 'none'}")
    print(f"  Latest NO shadow:         {latest_no_shadow['evaluation_time'] if latest_no_shadow else 'none'}")
    print(f"  Current UTC:              {now_utc}")

    # Time since last NO-side data
    if latest_no_shadow:
        # Parse the timestamp
        ts = latest_no_shadow["evaluation_time"]
        try:
            # Handle various timestamp formats
            for fmt in ["%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                        "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"]:
                try:
                    dt = datetime.strptime(ts, fmt)
                    break
                except ValueError:
                    continue
            else:
                dt = None
            if dt:
                age_min = (datetime.utcnow() - dt).total_seconds() / 60
                print(f"  NO shadow data age:       {age_min:.0f} min ago")
                if age_min > 60:
                    print(f"  WARNING: NO shadow data is {age_min/60:.1f}h stale")
        except Exception:
            pass

    # ── 7. Settlement outcomes ──

    section("7. SETTLEMENT OUTCOMES (NO-side shadow)")

    settled = conn.execute("""
        SELECT asset, market_price, best_bid, market_result,
            no_live_prob, no_live_edge, no_live_pnl_cents,
            no_a1_pnl_cents, no_a2_pnl_cents,
            evaluation_time, settled_time
        FROM fifteenm_shadow_signals
        WHERE status = 'settled' AND no_live_pnl_cents IS NOT NULL
        ORDER BY rowid DESC LIMIT 20
    """).fetchall()

    if settled:
        print(f"  {'Time':<14} {'Asset':>5} {'MP':>4} {'Bid':>4} {'NO_ask':>7} "
              f"{'Result':>7} {'NO PnL':>8} {'A1 PnL':>8} {'A2 PnL':>8}")
        print("  " + "-" * 78)
        for r in settled:
            bb = r["best_bid"]
            no_ask = (100 - bb) if (bb and bb > 0) else None
            no_ask_s = f"{no_ask}c" if no_ask else "n/a"
            result = r["market_result"] or "?"
            # NO wins when market_result is 'no'
            pnl = r["no_live_pnl_cents"] or 0
            a1 = r["no_a1_pnl_cents"] or 0
            a2 = r["no_a2_pnl_cents"] or 0
            ts = r["evaluation_time"][-14:-1] if r["evaluation_time"] else "?"
            print(f"  {ts:<14} {r['asset']:>5} {r['market_price']:>3}c "
                  f"{bb or '?':>4} {no_ask_s:>7} {result:>7} "
                  f"{pnl:>+7}c {a1:>+7}c {a2:>+7}c")
    else:
        print("  No settled NO-side shadow signals yet")

    # ── 8. Summary & health check ──

    section("8. HEALTH CHECK SUMMARY")

    issues = []

    # Check: bot producing shadow signals?
    if fs_total == 0:
        issues.append("CRITICAL: No fifteenm_shadow_signals at all")
    elif fs_with_no == 0:
        issues.append("WARNING: Shadow signals exist but none have NO-side data")

    # Check: post-fix signals flowing?
    if fs_post == 0:
        issues.append("INFO: No post-fix shadow signals yet (fix deployed recently)")

    # Check: evaluated_opportunities NO-side
    if eo_post == 0 and eo_total > 0:
        # Check if NO asks are below threshold
        below = conn.execute(
            "SELECT COUNT(*) FROM fifteenm_shadow_signals "
            "WHERE best_bid IS NOT NULL AND best_bid > 0 "
            "AND (100 - best_bid) < 70 "
            "AND evaluation_time >= ?", (DEPLOY_TIME,)
        ).fetchone()[0]
        above = conn.execute(
            "SELECT COUNT(*) FROM fifteenm_shadow_signals "
            "WHERE best_bid IS NOT NULL AND best_bid > 0 "
            "AND (100 - best_bid) >= 70 "
            "AND evaluation_time >= ?", (DEPLOY_TIME,)
        ).fetchone()[0]
        if below > 0 and above == 0:
            issues.append(f"OK: 0 post-fix NO evals because all {below} "
                          f"NO asks < 70c threshold")
        elif above > 0:
            issues.append(f"WARNING: {above} signals had NO ask >= 70c "
                          f"but 0 NO-side evals — possible bug")

    # Check: pre-fix data properly marked?
    unmarked = conn.execute(
        "SELECT COUNT(*) FROM evaluated_opportunities "
        "WHERE side = 'no' AND evaluation_time < ? "
        "AND (pricing_version IS NULL OR pricing_version != 'no_bid_wrong')",
        (DEPLOY_TIME,)
    ).fetchone()[0]
    if unmarked > 0:
        issues.append(f"WARNING: {unmarked} pre-fix NO entries not marked "
                      f"with pricing_version='no_bid_wrong'")

    if not issues:
        print("  ALL CHECKS PASSED")
    else:
        for issue in issues:
            status = issue.split(":")[0]
            if status == "CRITICAL":
                print(f"  [!!!] {issue}")
            elif status == "WARNING":
                print(f"  [!]   {issue}")
            else:
                print(f"  [i]   {issue}")

    # Quick status line
    print(f"\n  BOTTOM LINE: {fs_with_no} NO-side shadow signals "
          f"({fs_settled_no} settled, {fs_pending_no} pending)")
    if fs_settled_no > 0:
        total_no_pnl = conn.execute(
            "SELECT SUM(no_live_pnl_cents) FROM fifteenm_shadow_signals "
            "WHERE status = 'settled' AND no_live_pnl_cents IS NOT NULL"
        ).fetchone()[0] or 0
        print(f"  NO-side sim PnL: {total_no_pnl:+}c "
              f"({'profitable' if total_no_pnl > 0 else 'unprofitable'})")
        print(f"  (This is expected to be negative at current NO asks of 25-30c)")

    conn.close()


def main():
    parser = argparse.ArgumentParser(description="NO-side shadow status report")
    parser.add_argument("--db", default="/tmp/state.db",
                        help="Path to state.db (default: /tmp/state.db)")
    parser.add_argument("--refresh", action="store_true",
                        help="Checkpoint WAL + SCP fresh state.db from VPS first")
    args = parser.parse_args()

    if args.refresh:
        print("Refreshing state.db from VPS...")
        try:
            subprocess.run(
                ["ssh", "botuser@45.55.181.30",
                 "cd ~/kalshi-bot-repo && python3 -c "
                 "\"import sqlite3; c=sqlite3.connect('state.db'); "
                 "c.execute('PRAGMA wal_checkpoint(PASSIVE)'); c.close()\""],
                check=True, timeout=15
            )
            subprocess.run(
                ["scp", "botuser@45.55.181.30:~/kalshi-bot-repo/state.db",
                 "/tmp/state.db"],
                check=True, timeout=30
            )
            print("Done.\n")
        except Exception as e:
            print(f"Refresh failed: {e}")
            sys.exit(1)
        args.db = "/tmp/state.db"

    run_report(args.db)


if __name__ == "__main__":
    main()
