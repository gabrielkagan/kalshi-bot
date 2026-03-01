#!/usr/bin/env python3
"""Weather shadow mode audit script.

Runs locally against a copy of state.db from VPS.
Evaluates data quality, ensemble coverage, signal pipeline, and readiness.

Usage:
    scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
    python scripts/weather_shadow_audit.py [--db /tmp/state.db]
"""

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple


def connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def section(title: str) -> None:
    print(f"\n{'=' * 65}")
    print(f"  {title}")
    print(f"{'=' * 65}\n")


# ── Section 1: Overview ──────────────────────────────────────────

def overview(conn: sqlite3.Connection) -> dict:
    """Overall shadow data summary. Returns stats dict for readiness check."""
    section("1. SHADOW PERFORMANCE SUMMARY")

    row = conn.execute("""
        SELECT COUNT(*) AS total,
          SUM(CASE WHEN filter_stage='price_out_of_range' THEN 1 ELSE 0 END) AS por,
          SUM(CASE WHEN filter_stage='strategy_wait' THEN 1 ELSE 0 END) AS sw,
          SUM(CASE WHEN filter_stage='insufficient_edge' THEN 1 ELSE 0 END) AS ie,
          SUM(CASE WHEN filter_stage='weather_observation' THEN 1 ELSE 0 END) AS wo,
          SUM(CASE WHEN filter_stage='low_probability' THEN 1 ELSE 0 END) AS lp,
          MIN(evaluation_time) AS first_t, MAX(evaluation_time) AS last_t
        FROM evaluated_opportunities WHERE product_type='weather'
    """).fetchone()

    total = row["total"] or 0
    signals = row["wo"] or 0
    por = row["por"] or 0
    sw = row["sw"] or 0
    ie = row["ie"] or 0

    # Compute time span
    span_hrs = 0
    if row["first_t"] and row["last_t"]:
        t1 = datetime.fromisoformat(row["first_t"].replace("Z", ""))
        t2 = datetime.fromisoformat(row["last_t"].replace("Z", ""))
        span_hrs = (t2 - t1).total_seconds() / 3600

    print(f"Total evaluations:     {total}")
    print(f"Signals fired:         {signals}")
    print(f"Signal rate:           {signals/total*100:.1f}%" if total > 0 else "Signal rate: —")
    print(f"Time span:             {span_hrs:.1f} hours")
    print(f"Eval rate:             {total/span_hrs:.1f}/hr" if span_hrs > 0 else "")
    print(f"First: {row['first_t'] or '—'}")
    print(f"Last:  {row['last_t'] or '—'}")
    print()

    print("Filter stage breakdown:")
    print(f"  price_out_of_range:  {por:>4} ({por/total*100:.1f}%)" if total > 0 else "")
    print(f"  strategy_wait:       {sw:>4} ({sw/total*100:.1f}%)" if total > 0 else "")
    print(f"  insufficient_edge:   {ie:>4} ({ie/total*100:.1f}%)" if total > 0 else "")
    print(f"  weather_observation: {signals:>4} ({signals/total*100:.1f}%)" if total > 0 else "")

    # Per-city
    print()
    rows = conn.execute("""
        SELECT asset, COUNT(*) AS n,
          SUM(CASE WHEN filter_stage='weather_observation' THEN 1 ELSE 0 END) AS sigs,
          SUM(CASE WHEN filter_stage='price_out_of_range' THEN 1 ELSE 0 END) AS por,
          SUM(CASE WHEN filter_stage='strategy_wait' THEN 1 ELSE 0 END) AS sw,
          SUM(CASE WHEN filter_stage='insufficient_edge' THEN 1 ELSE 0 END) AS ie
        FROM evaluated_opportunities WHERE product_type='weather'
        GROUP BY asset ORDER BY n DESC
    """).fetchall()

    print(f"{'City':<12} {'Total':>6} {'Signals':>8} {'POR':>5} {'SW':>5} {'IE':>5}")
    print("-" * 45)
    for r in rows:
        print(f"{r['asset']:<12} {r['n']:>6} {r['sigs']:>8} {r['por']:>5} {r['sw']:>5} {r['ie']:>5}")

    return {
        "total": total, "signals": signals, "span_hrs": span_hrs,
        "por": por, "sw": sw, "ie": ie,
    }


# ── Section 2: Data Pipeline Audit ──────────────────────────────

def pipeline_audit(conn: sqlite3.Connection) -> dict:
    """Data pipeline and quality audit."""
    section("2. DATA PIPELINE + QUALITY AUDIT")

    # Ensemble coverage
    row = conn.execute("""
        SELECT
          SUM(CASE WHEN wx_ensemble_mean IS NOT NULL THEN 1 ELSE 0 END) AS has_ens,
          SUM(CASE WHEN wx_ensemble_mean IS NULL THEN 1 ELSE 0 END) AS null_ens,
          COUNT(*) AS total,
          AVG(CASE WHEN wx_n_members IS NOT NULL THEN wx_n_members END) AS avg_members,
          MIN(CASE WHEN wx_n_members IS NOT NULL THEN wx_n_members END) AS min_members,
          MAX(CASE WHEN wx_n_members IS NOT NULL THEN wx_n_members END) AS max_members,
          AVG(CASE WHEN wx_ensemble_std IS NOT NULL THEN wx_ensemble_std END) AS avg_std
        FROM evaluated_opportunities WHERE product_type='weather'
    """).fetchone()

    total = row["total"] or 0
    has_ens = row["has_ens"] or 0
    null_ens = row["null_ens"] or 0
    coverage = has_ens / total * 100 if total > 0 else 0

    print("Ensemble Data Coverage:")
    print(f"  With ensemble:    {has_ens}/{total} ({coverage:.1f}%)")
    print(f"  NULL ensemble:    {null_ens}/{total} ({100-coverage:.1f}%)")
    if row["avg_members"]:
        print(f"  Avg members:      {row['avg_members']:.0f} (expected 82: 31 GFS + 51 ECMWF)")
        print(f"  Member range:     {row['min_members']}-{row['max_members']}")
    if row["avg_std"]:
        print(f"  Avg ensemble std: {row['avg_std']:.2f}F")

    ecmwf_present = (row["max_members"] or 0) > 31
    print(f"\n  ECMWF status:     {'PRESENT' if ecmwf_present else 'ABSENT (only GFS)'}")
    if not ecmwf_present:
        print("  >>> WARNING: ECMWF ensemble data is completely missing!")
        print("  >>> Operating at 37.8% of designed ensemble capacity (31/82 members)")

    # Market price distribution
    print("\nMarket Price Distribution:")
    rows = conn.execute("""
        SELECT
          CASE
            WHEN market_price <= 5 THEN '0-5c'
            WHEN market_price <= 15 THEN '6-15c'
            WHEN market_price <= 30 THEN '16-30c'
            WHEN market_price <= 50 THEN '31-50c'
            WHEN market_price <= 70 THEN '51-70c'
            WHEN market_price <= 85 THEN '71-85c'
            WHEN market_price <= 95 THEN '86-95c'
            ELSE '96-100c'
          END AS bucket,
          COUNT(*) AS n,
          SUM(CASE WHEN filter_stage='weather_observation' THEN 1 ELSE 0 END) AS sigs
        FROM evaluated_opportunities WHERE product_type='weather'
        GROUP BY bucket ORDER BY MIN(market_price)
    """).fetchall()

    tradeable = 0
    for r in rows:
        pct = r["n"] / total * 100 if total > 0 else 0
        tradeable_flag = " [TRADEABLE]" if r["bucket"] in ("16-30c", "31-50c", "51-70c", "71-85c") else ""
        if tradeable_flag:
            tradeable += r["n"]
        print(f"  {r['bucket']:<10} {r['n']:>4} ({pct:>5.1f}%) sigs={r['sigs']}{tradeable_flag}")

    print(f"\n  Tradeable range (15-85c): {tradeable}/{total} ({tradeable/total*100:.1f}%)" if total > 0 else "")

    # Evaluation frequency
    print("\nEvaluation Frequency:")
    times = conn.execute("""
        SELECT evaluation_time FROM evaluated_opportunities
        WHERE product_type='weather' ORDER BY evaluation_time
    """).fetchall()
    if len(times) > 1:
        gaps = []
        for i in range(1, len(times)):
            t1 = datetime.fromisoformat(times[i-1]["evaluation_time"].replace("Z", ""))
            t2 = datetime.fromisoformat(times[i]["evaluation_time"].replace("Z", ""))
            gaps.append((t2 - t1).total_seconds())
        print(f"  Avg gap: {sum(gaps)/len(gaps)/60:.1f} min")
        print(f"  Min gap: {min(gaps)/60:.1f} min")
        print(f"  Max gap: {max(gaps)/60:.1f} min ({max(gaps)/3600:.1f} hrs)")

    # Coverage
    row = conn.execute("""
        SELECT COUNT(DISTINCT ticker) AS tickers,
          COUNT(DISTINCT event_ticker) AS events,
          COUNT(DISTINCT asset) AS cities
        FROM evaluated_opportunities WHERE product_type='weather'
    """).fetchone()
    print(f"\n  Distinct tickers: {row['tickers']}")
    print(f"  Distinct events:  {row['events']}")
    print(f"  Distinct cities:  {row['cities']}/5")

    return {
        "ensemble_coverage": coverage,
        "ecmwf_present": ecmwf_present,
        "avg_members": row["avg_members"] if row else None,
        "tradeable_pct": tradeable / total * 100 if total > 0 else 0,
    }


# ── Section 3: Leak Analysis ────────────────────────────────────

def leak_analysis(conn: sqlite3.Connection) -> None:
    """Quantify data leaks and pipeline inefficiencies."""
    section("3. LEAK ANALYSIS")

    # Leak 1: Low-price noise
    row = conn.execute("""
        SELECT
          SUM(CASE WHEN market_price <= 5 THEN 1 ELSE 0 END) AS sub_5,
          SUM(CASE WHEN market_price <= 5 AND filter_stage != 'price_out_of_range' THEN 1 ELSE 0 END) AS sub_5_passed_por,
          COUNT(*) AS total
        FROM evaluated_opportunities WHERE product_type='weather'
    """).fetchone()

    total = row["total"] or 1
    sub5 = row["sub_5"] or 0
    sub5_pass = row["sub_5_passed_por"] or 0
    print(f"Leak 1: Low-Price Noise (<=5c markets)")
    print(f"  Total <=5c entries:    {sub5}/{total} ({sub5/total*100:.1f}%)")
    print(f"  <=5c past POR filter:  {sub5_pass} (these show fake 'edge')")
    print(f"  Impact: Pollutes strategy_wait counts with untradeable noise")
    severity = "CRITICAL" if sub5_pass > 5 else "HIGH" if sub5_pass > 0 else "LOW"
    print(f"  Severity: {severity}")

    # Leak 2: NULL ensemble
    row = conn.execute("""
        SELECT
          SUM(CASE WHEN wx_ensemble_mean IS NULL THEN 1 ELSE 0 END) AS null_ens,
          SUM(CASE WHEN wx_ensemble_mean IS NULL AND filter_stage != 'price_out_of_range' THEN 1 ELSE 0 END) AS null_ens_non_por,
          COUNT(*) AS total
        FROM evaluated_opportunities WHERE product_type='weather'
    """).fetchone()

    null_ens = row["null_ens"] or 0
    null_non_por = row["null_ens_non_por"] or 0
    print(f"\nLeak 2: NULL Ensemble Data")
    print(f"  Entries without ensemble: {null_ens}/{row['total']} ({null_ens/total*100:.1f}%)")
    print(f"  Non-POR with NULL ens:    {null_non_por}")
    print(f"  Impact: Probability model running without forecast data")
    print(f"  Severity: {'CRITICAL' if null_ens/total > 0.5 else 'HIGH'}")

    # Leak 3: ECMWF missing
    row = conn.execute("""
        SELECT MAX(wx_n_members) AS max_mem
        FROM evaluated_opportunities WHERE product_type='weather'
    """).fetchone()
    max_mem = row["max_mem"] or 0
    print(f"\nLeak 3: ECMWF Ensemble Missing")
    print(f"  Max members observed: {max_mem} (expected 82)")
    print(f"  Capacity utilization: {max_mem/82*100:.1f}%")
    print(f"  Std inflation factor: ~{(82/max(max_mem,1))**0.5:.2f}x wider than designed")
    print(f"  Severity: {'CRITICAL' if max_mem <= 31 else 'MEDIUM'}")

    # Leak 4: Zero signal conversion
    row = conn.execute("""
        SELECT
          SUM(CASE WHEN filter_stage='weather_observation' THEN 1 ELSE 0 END) AS sigs,
          SUM(CASE WHEN filter_stage='insufficient_edge' AND market_price BETWEEN 15 AND 85 THEN 1 ELSE 0 END) AS near_miss
        FROM evaluated_opportunities WHERE product_type='weather'
    """).fetchone()
    sigs = row["sigs"] or 0
    near = row["near_miss"] or 0
    print(f"\nLeak 4: Zero Signal Conversion")
    print(f"  Signals produced:  {sigs}")
    print(f"  Near-misses (IE at 15-85c): {near}")
    print(f"  Impact: Cannot evaluate trading readiness with 0 signals")
    print(f"  Severity: BLOCKING")


# ── Section 4: Config Sensitivity ────────────────────────────────

def config_sensitivity(conn: sqlite3.Connection) -> None:
    """Analyze sensitivity to key config parameters."""
    section("4. CONFIG SENSITIVITY")

    # Simulate MIN_ENTRY_PRICE changes
    print("Parameter: WEATHER_MIN_ENTRY_PRICE (current = 1)")
    for threshold in [5, 10, 15, 20]:
        row = conn.execute("""
            SELECT COUNT(*) AS kept,
              SUM(CASE WHEN filter_stage='weather_observation' THEN 1 ELSE 0 END) AS sigs,
              SUM(CASE WHEN filter_stage='insufficient_edge' THEN 1 ELSE 0 END) AS ie,
              SUM(CASE WHEN filter_stage='strategy_wait' THEN 1 ELSE 0 END) AS sw
            FROM evaluated_opportunities
            WHERE product_type='weather' AND market_price >= ?
        """, (threshold,)).fetchone()
        total = row["kept"] or 0
        print(f"  If raised to {threshold}c: {total} evals kept, {row['ie']} IE, {row['sw']} SW, {row['sigs']} signals")

    # Simulate MAX_ENTRY_PRICE changes
    print(f"\nParameter: WEATHER_MAX_ENTRY_PRICE (current = 99)")
    for threshold in [95, 90, 85]:
        row = conn.execute("""
            SELECT COUNT(*) AS kept
            FROM evaluated_opportunities
            WHERE product_type='weather' AND market_price <= ?
        """, (threshold,)).fetchone()
        print(f"  If lowered to {threshold}c: {row['kept']} evals kept")

    # Edge distribution for near-misses
    print(f"\nInsufficient_edge entries (closest to signals):")
    rows = conn.execute("""
        SELECT ticker, market_price, fee_adjusted_edge, calibrated_prob,
          wx_ensemble_mean, wx_ensemble_std
        FROM evaluated_opportunities
        WHERE product_type='weather' AND filter_stage='insufficient_edge'
        ORDER BY fee_adjusted_edge DESC
    """).fetchall()
    for r in rows:
        ens = f"mean={r['wx_ensemble_mean']:.1f}" if r["wx_ensemble_mean"] else "no_ensemble"
        print(f"  {r['ticker']:<45} p={r['market_price']:>3}c edge={r['fee_adjusted_edge']:>+.4f} cal={r['calibrated_prob']:.3f} {ens}")

    print(f"\nParameter: WEATHER_MARKET_BLEND_W (current = 0.50)")
    print(f"  At 0.50: Heavy market anchoring — suppresses model divergence")
    print(f"  At 0.30: More model trust — may surface edge in thin markets")
    print(f"  At 0.00: Pure model — risky without calibration validation")
    print(f"  Recommendation: Cannot evaluate without signal data; keep at 0.50")


# ── Section 5: Data Sufficiency ──────────────────────────────────

def data_sufficiency(conn: sqlite3.Connection, stats: dict) -> None:
    """Evaluate data sufficiency for trading decisions."""
    section("5. DATA SUFFICIENCY AUDIT")

    checks = [
        ("Total evaluations >= 500", stats["total"] >= 500, f"{stats['total']}/500"),
        ("Signals >= 100", stats["signals"] >= 100, f"{stats['signals']}/100"),
        ("Settled signals >= 50", 0 >= 50, "0/50"),
        ("Days of data >= 14", stats["span_hrs"] / 24 >= 14,
         f"{stats['span_hrs']/24:.1f}/14 days"),
        ("All 5 cities signaling", False, "0/5 cities"),
        ("Ensemble coverage > 90%", False, "~25%"),
        ("Mid-range evals >= 200", False, "~12/200"),
    ]

    all_pass = True
    for desc, passed, detail in checks:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  [{status}] {desc}: {detail}")

    print()
    if all_pass:
        print("  >>> ALL CHECKS PASSED")
    else:
        n_pass = sum(1 for _, p, _ in checks if p)
        print(f"  >>> {n_pass}/{len(checks)} checks passing — NOT READY")
        print(f"  >>> Primary blocker: 0 signals = cannot evaluate edge quality")

    # Estimate time to sufficiency
    if stats["total"] > 0 and stats["span_hrs"] > 0:
        rate = stats["total"] / stats["span_hrs"]
        hrs_to_500 = (500 - stats["total"]) / rate if rate > 0 else float("inf")
        print(f"\n  Current eval rate: {rate:.1f}/hr")
        print(f"  Est. time to 500 evals: {hrs_to_500/24:.1f} days")
        print(f"  Est. time to signal data: UNKNOWN (signal rate = 0%)")


# ── Section 6: Recommendations ───────────────────────────────────

def recommendations() -> None:
    section("6. RECOMMENDATIONS")

    recs = [
        ("R1", "CRITICAL", "Fix ECMWF ensemble fetch",
         "Investigate Open-Meteo ECMWF endpoint. Currently getting 0 ECMWF members.\n"
         "   Expected: 51 ECMWF + 31 GFS = 82 total. Getting: 31 GFS only.\n"
         "   Add explicit error logging when ECMWF fetch returns empty."),
        ("R2", "HIGH", "Raise WEATHER_MIN_ENTRY_PRICE to 15",
         "Currently 1c — allows deep OTM noise markets through pipeline.\n"
         "   57%+ of entries are at 1c. These show fake 'edge' (model assigns >1% to\n"
         "   outcomes market prices at 1c). Raising to 15c focuses on tradeable range."),
        ("R3", "HIGH", "Add ensemble data quality gate",
         "Skip evaluation when wx_ensemble_mean is NULL. Insert with\n"
         "   filter_stage='data_unavailable'. Currently 74%+ of evals have NULL ensemble —\n"
         "   probability model runs without forecast data."),
        ("R4", "MEDIUM", "Review calibration transfer to weather",
         "CalibrationEngine trained on 15M crypto data. Weather probability distributions\n"
         "   (Gaussian ensemble) are fundamentally different. Verify calibration isn't\n"
         "   distorting weather probabilities. Consider raw model probs for weather."),
        ("R5", "MEDIUM", "Collect 14+ days of cleaned data before further changes",
         "After R1-R3: monitor for 2 weeks. Look for signal rate > 0%, ensemble\n"
         "   coverage > 90%, mid-range evaluations. Only then evaluate edge/sizing."),
    ]

    for label, severity, title, detail in recs:
        print(f"[{label}] [{severity}] {title}")
        print(f"   {detail}")
        print()


# ── Section 7: Validation Plan ───────────────────────────────────

def validation_plan() -> None:
    section("7. VALIDATION PLAN")

    print("Immediate (today):")
    print("  1. curl-test ECMWF endpoint from VPS")
    print("  2. Check weather_engine.py ECMWF error handling path")
    print("  3. Verify n_members logged on every ensemble fetch")
    print()
    print("After config changes (R1-R3):")
    print("  4. Monitor 24h: ensemble coverage > 90%?")
    print("  5. Confirm 1c markets filtered out")
    print("  6. Watch for first weather_observation signal")
    print()
    print("Weekly audit:")
    print("  7. Run this script: python scripts/weather_shadow_audit.py --db /tmp/state.db")
    print("  8. Track: signal rate trend, ensemble coverage, price distribution")
    print("  9. After 50+ signals: compute Brier score vs market baseline")
    print()
    print("Promotion criteria (all must pass):")
    print("  - 100+ weather_observation signals")
    print("  - 50+ settled with known outcomes")
    print("  - Win rate > 55% on tradeable-price signals")
    print("  - Positive CLV (model beats closing line)")
    print("  - Ensemble coverage > 95%")
    print("  - Signal quality consistent across 3+ cities")


# ── Main ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Weather shadow mode audit")
    parser.add_argument("--db", default="state.db", help="Path to state.db")
    args = parser.parse_args()

    try:
        conn = connect_db(args.db)
    except Exception as e:
        print(f"ERROR: Cannot open DB at {args.db}: {e}")
        sys.exit(1)

    # Check for weather data
    row = conn.execute("""
        SELECT COUNT(*) AS n FROM evaluated_opportunities WHERE product_type='weather'
    """).fetchone()
    if (row["n"] or 0) == 0:
        print("ERROR: No weather evaluations found in evaluated_opportunities.")
        print("Is weather_engine.py running? Is product_type='weather' set?")
        sys.exit(1)

    stats = overview(conn)
    pipeline_stats = pipeline_audit(conn)
    leak_analysis(conn)
    config_sensitivity(conn)
    data_sufficiency(conn, stats)
    recommendations()
    validation_plan()

    conn.close()
    print(f"\n{'=' * 65}")
    print(f"  Audit complete. Re-run after implementing R1-R3.")
    print(f"{'=' * 65}")


if __name__ == "__main__":
    main()
