#!/usr/bin/env python3
"""Backfill Tier 4 (time/regime) + Tier 5 (derived) features on existing
evaluated_opportunities rows.

Usage:
    python3 scripts/backfill_extended_features.py [--dry-run] [--db state.db] [--limit N]

Tier 4 fields populated from `evaluation_time` column (pure timestamp derivation):
    hour_of_day_utc, day_of_week, is_weekend, minutes_since_us_open,
    is_fomc_day, is_cpi_day

Tier 5 fields populated from existing columns:
    spot_distance_to_strike_sigma  = buf_pct / (vol × sqrt(STC/5) × 100)
    prob_breakeven_gap             = calibrated_prob − market_price/100
    kelly_vs_cap_ratio             = position_size / SOL_RESCUE_CONTRACT_CAP
    (calibration_confidence left NULL — requires CalEngine state history)

Tier 1/2/3/6 are NOT backfilled (require forward instrumentation).

UPDATE batches of ≤50 rows per commit (DB contention rule).

--force-sigma-recompute recomputes spot_distance_to_strike_sigma for rows
that already have a value (used to fix the Apr 21 formula scale bug that
stored values ~240× too small).
"""

import argparse
import math
import sqlite3
import sys
import os
from pathlib import Path

# Ensure we can import bot.compute_* helpers regardless of CWD.
# Bit 11.2 (2026-05-12): relocated to scripts/backfill/; 3-level dirname
# (backfill/ → scripts/ → repo/).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from bot.constants import SOL_RESCUE_CONTRACT_CAP
from bot.helpers.derived_features import compute_derived_features
BATCH_SIZE = 50


def open_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def fetch_candidates(conn: sqlite3.Connection, limit: int = None,
                     force_sigma_recompute: bool = False) -> list:
    """Fetch rows where ANY of the Tier 4/5 columns is NULL.

    With --force-sigma-recompute, also re-fetch rows where
    spot_distance_to_strike_sigma is already populated (to recompute
    under the corrected formula).
    """
    if force_sigma_recompute:
        where_clause = """
             WHERE hour_of_day_utc IS NULL
                OR spot_distance_to_strike_sigma IS NULL
                OR prob_breakeven_gap IS NULL
                OR spot_distance_to_strike_sigma IS NOT NULL
        """
    else:
        where_clause = """
             WHERE hour_of_day_utc IS NULL
                OR spot_distance_to_strike_sigma IS NULL
                OR prob_breakeven_gap IS NULL
        """
    sql = f"""
        SELECT id, evaluation_time, spot_price, threshold, volatility,
               seconds_to_close, calibrated_prob, market_price, position_size
          FROM evaluated_opportunities
        {where_clause}
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql))


def compute_features_for_row(row: tuple) -> dict:
    (row_id, eval_time, spot, thresh, vol, stc, cal_prob, mkt_price, pos_size) = row
    t4 = compute_time_regime_features(eval_time)
    t5 = compute_derived_features(
        spot_price=spot, threshold=thresh, volatility=vol,
        seconds_to_close=stc, calibrated_prob=cal_prob,
        market_price_cents=mkt_price, kelly_contracts=pos_size,
        sol_rescue_cap=SOL_RESCUE_CONTRACT_CAP, n_recent_cal_trades=None,
    )
    return {**t4, **t5, "id": row_id}


def apply_batch(conn: sqlite3.Connection, features: list, dry_run: bool = False) -> int:
    """Apply UPDATE for up to BATCH_SIZE rows, single commit at end."""
    if not features:
        return 0
    update_sql = """
        UPDATE evaluated_opportunities
           SET hour_of_day_utc = ?, day_of_week = ?, is_weekend = ?,
               minutes_since_us_open = ?, is_fomc_day = ?, is_cpi_day = ?,
               spot_distance_to_strike_sigma = ?, prob_breakeven_gap = ?,
               kelly_vs_cap_ratio = ?, calibration_confidence = ?
         WHERE id = ?
    """
    if dry_run:
        return len(features)
    params = [
        (f["hour_of_day_utc"], f["day_of_week"], f["is_weekend"],
         f["minutes_since_us_open"], f["is_fomc_day"], f["is_cpi_day"],
         f["spot_distance_to_strike_sigma"], f["prob_breakeven_gap"],
         f["kelly_vs_cap_ratio"], f["calibration_confidence"], f["id"])
        for f in features
    ]
    conn.executemany(update_sql, params)
    conn.commit()
    return len(features)


def run(db_path: str, dry_run: bool, limit: int = None,
        force_sigma_recompute: bool = False) -> None:
    conn = open_conn(db_path)
    print(f"Opened {db_path} (dry_run={dry_run}, limit={limit}, "
          f"force_sigma_recompute={force_sigma_recompute})")

    rows = fetch_candidates(conn, limit=limit,
                            force_sigma_recompute=force_sigma_recompute)
    print(f"Found {len(rows)} rows needing backfill")
    if not rows:
        print("Nothing to do")
        return

    total_updated = 0
    batch = []
    for i, row in enumerate(rows, 1):
        feats = compute_features_for_row(row)
        batch.append(feats)
        if len(batch) >= BATCH_SIZE:
            n = apply_batch(conn, batch, dry_run=dry_run)
            total_updated += n
            batch = []
            if i % 500 == 0:
                print(f"  progress: {i}/{len(rows)} ({100*i/len(rows):.1f}%)")

    if batch:
        total_updated += apply_batch(conn, batch, dry_run=dry_run)

    verb = "would update" if dry_run else "updated"
    print(f"Done — {verb} {total_updated} rows")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="state.db", help="Path to state.db")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute but don't UPDATE")
    ap.add_argument("--limit", type=int, default=None,
                    help="Limit rows processed (for testing)")
    ap.add_argument("--force-sigma-recompute", action="store_true",
                    help="Recompute spot_distance_to_strike_sigma on rows "
                         "that already have a value (fixes Apr 21 scale bug)")
    args = ap.parse_args()
    if not os.path.exists(args.db):
        print(f"ERROR: db not found: {args.db}", file=sys.stderr)
        return 1
    run(args.db, dry_run=args.dry_run, limit=args.limit,
        force_sigma_recompute=args.force_sigma_recompute)
    return 0


if __name__ == "__main__":
    sys.exit(main())
