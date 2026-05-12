#!/usr/bin/env python3
"""B.1a-fu2 (2026-05-12, ticket 86b9wuh8r) — Wave 1 derivable-column backfill.

Backfills the 4 derivable Wave 1 columns onto historical rows using the
canonical helpers in bot.helpers (cal_mlp feature-transform lock-step, see
bot/CLAUDE.md). Idempotent: only updates rows where the target column is
already NULL — re-runs are no-ops.

Scope (per the verified schema as of B.1a 2026-05-12 ship):

  rejected_opportunities:
    hour_sin              derived from rejection_time (compute_time_regime_features → compute_hour_sin_cos)
    hour_cos              "
    sigma_winsorize       derived from spot_price + threshold + volatility + seconds_to_close
                          (compute_derived_features → apply_sigma_winsor)
    prob_breakeven_gap    derived from calibrated_prob + market_price
                          (compute_derived_features)
    data_provenance       stamped 'backfill_b1a_fu2' on rows we touched (only when NULL)

  evaluated_opportunities:
    prob_breakeven_gap    derived from calibrated_prob + market_price
                          (sigma_winsorize/hour_sin/hour_cos cols don't exist
                           on this table — schema asymmetry; out of scope.)

Honest-NULL semantics (mirrors bot/state.py::insert_rejection): rows where
any required input is NULL keep their NULL target. Never fabricate.

Usage:
    python3 scripts/backfill/wave1_derived_cols.py [--dry-run] [--db state.db] [--limit N]

Lock-step partner: scripts/backfill/backfill_extended_features.py (Tier 4/5
backfill on evaluated_opportunities pre-B.1a). Both call canonical helpers.

Plan + RCA: kb/decisions/wave1-backfill-derivable-cols-may12.md
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Optional

# Bit 11.2 (2026-05-12): scripts/backfill/ subdir; 3-level dirname.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from bot.helpers.derived_features import (  # noqa: E402
    apply_sigma_winsor,
    compute_derived_features,
    compute_hour_sin_cos,
)
from bot.helpers.time_features import compute_time_regime_features  # noqa: E402

BATCH_SIZE = 50
PROVENANCE_TAG = "backfill_b1a_fu2"


def open_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _column_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == col for r in rows)


# ── rejected_opportunities backfill ──


def backfill_rejected(
    conn: sqlite3.Connection,
    *,
    dry_run: bool,
    limit: Optional[int],
) -> dict:
    """Replay B.1a auto-fill against rejected_opportunities rows where any
    Wave 1 derivable col is NULL."""
    stats = {
        "hour_sin": 0, "hour_cos": 0,
        "sigma_winsorize": 0, "prob_breakeven_gap": 0,
        "data_provenance": 0, "rows_seen": 0, "rows_updated": 0,
    }
    where = """
        WHERE (hour_sin IS NULL OR hour_cos IS NULL
            OR sigma_winsorize IS NULL OR prob_breakeven_gap IS NULL)
    """
    sql = f"""
        SELECT ticker, rejection_time, spot_price, threshold, volatility,
               seconds_to_close, calibrated_prob, market_price,
               hour_sin, hour_cos, sigma_winsorize, prob_breakeven_gap,
               data_provenance
          FROM rejected_opportunities
        {where}
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    # R1 C2 fix: materialize cursor before iterating so we don't commit on a
    # connection that holds an open SELECT (mirrors
    # scripts/backfill/backfill_extended_features.py and avoids the
    # "another row available" cursor race documented in MEMORY.md).
    rows = list(conn.execute(sql))
    batch: list[tuple] = []
    for row in rows:
        stats["rows_seen"] += 1
        (ticker, rejection_time, spot_price, threshold, volatility,
         seconds_to_close, calibrated_prob, market_price,
         existing_hs, existing_hc, existing_sigma, existing_gap,
         existing_prov) = row

        # ── derive each col, but only when target is NULL (explicit-wins) ──
        new_hs, new_hc = existing_hs, existing_hc
        new_sigma = existing_sigma
        new_gap = existing_gap
        new_prov = existing_prov

        # R2 M1 fix: explicit guard. compute_time_regime_features(None)
        # returns features for the *current* UTC hour (a live-write
        # convenience), which would be semantically wrong for a backfill
        # — we must reflect the row's historical rejection_time, not "now".
        # In production rejection_time is NOT NULL by schema constraint,
        # but defensive against future schema relaxations or empty-string
        # rows that bypass the constraint.
        if (existing_hs is None or existing_hc is None) and rejection_time:
            _t4 = compute_time_regime_features(rejection_time)
            _hs, _hc = compute_hour_sin_cos(_t4.get("hour_of_day_utc"))
            if existing_hs is None and _hs is not None:
                new_hs = _hs
                stats["hour_sin"] += 1
            if existing_hc is None and _hc is not None:
                new_hc = _hc
                stats["hour_cos"] += 1

        if existing_sigma is None or existing_gap is None:
            _t5 = compute_derived_features(
                spot_price=spot_price, threshold=threshold,
                volatility=volatility, seconds_to_close=seconds_to_close,
                calibrated_prob=calibrated_prob,
                market_price_cents=market_price,
            )
            if existing_sigma is None:
                derived = apply_sigma_winsor(_t5["spot_distance_to_strike_sigma"])
                if derived is not None:
                    new_sigma = derived
                    stats["sigma_winsorize"] += 1
            if existing_gap is None:
                derived = _t5["prob_breakeven_gap"]
                if derived is not None:
                    new_gap = derived
                    stats["prob_breakeven_gap"] += 1

        # R1 C1 fix: only stamp data_provenance on rows where backfill
        # actually computed at least one Wave 1 cell. Otherwise the stamp
        # would incorrectly attribute live-written values to the backfill,
        # which would re-route them to recomputation in any future
        # formula-change correction job.
        cells_changed = (
            new_hs is not existing_hs or new_hc is not existing_hc
            or new_sigma is not existing_sigma or new_gap is not existing_gap
        )
        if existing_prov is None and cells_changed:
            new_prov = PROVENANCE_TAG
            stats["data_provenance"] += 1

        if cells_changed or new_prov is not existing_prov:
            batch.append((new_hs, new_hc, new_sigma, new_gap, new_prov, ticker))
            stats["rows_updated"] += 1

        if not dry_run and len(batch) >= BATCH_SIZE:
            _flush_rejected(conn, batch)
            batch.clear()

    if not dry_run and batch:
        _flush_rejected(conn, batch)

    return stats


def _flush_rejected(conn: sqlite3.Connection, batch: list[tuple]) -> None:
    """Single-statement batched UPDATE — uses IS NULL guards in WHERE so a
    concurrent live-write that filled a cell between our SELECT and our
    UPDATE wins (preserves honest-NULL + explicit-wins)."""
    conn.executemany(
        """
        UPDATE rejected_opportunities
           SET hour_sin           = COALESCE(hour_sin, ?),
               hour_cos           = COALESCE(hour_cos, ?),
               sigma_winsorize    = COALESCE(sigma_winsorize, ?),
               prob_breakeven_gap = COALESCE(prob_breakeven_gap, ?),
               data_provenance    = COALESCE(data_provenance, ?)
         WHERE ticker = ?
        """,
        batch,
    )
    conn.commit()


# ── evaluated_opportunities backfill (prob_breakeven_gap only) ──


def backfill_evaluated(
    conn: sqlite3.Connection,
    *,
    dry_run: bool,
    limit: Optional[int],
) -> dict:
    # R1 M3 (corrected at R2 M2): evaluated_opportunities.data_provenance is
    # owned by scripts/backfill/stamp_data_provenance.py (Phase G-6, ~15m-only
    # rows under a cutoff + inputs predicate; non-15m rows + pre-cutoff rows
    # without inputs are NULL by design). This backfill stays out of that
    # column entirely to avoid corrupting G-6's classification — we only fill
    # the prob_breakeven_gap NULLs.
    stats = {"prob_breakeven_gap": 0, "rows_seen": 0, "rows_updated": 0}
    sql = """
        SELECT id, calibrated_prob, market_price
          FROM evaluated_opportunities
         WHERE prob_breakeven_gap IS NULL
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    # R1 C2 fix: materialize cursor — see backfill_rejected.
    rows = list(conn.execute(sql))
    batch: list[tuple] = []
    for row in rows:
        stats["rows_seen"] += 1
        rowid, calibrated_prob, market_price = row
        _t5 = compute_derived_features(
            calibrated_prob=calibrated_prob,
            market_price_cents=market_price,
        )
        gap = _t5["prob_breakeven_gap"]
        if gap is None:
            continue
        batch.append((gap, rowid))
        stats["prob_breakeven_gap"] += 1
        stats["rows_updated"] += 1
        if not dry_run and len(batch) >= BATCH_SIZE:
            _flush_evaluated(conn, batch)
            batch.clear()

    if not dry_run and batch:
        _flush_evaluated(conn, batch)
    return stats


def _flush_evaluated(conn: sqlite3.Connection, batch: list[tuple]) -> None:
    conn.executemany(
        """
        UPDATE evaluated_opportunities
           SET prob_breakeven_gap = COALESCE(prob_breakeven_gap, ?)
         WHERE id = ?
        """,
        batch,
    )
    conn.commit()


# ── CLI ──


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="B.1a-fu2: backfill Wave 1 derivable columns."
    )
    parser.add_argument("--db", default=str(_REPO_ROOT / "state.db"),
                        help="path to state.db (default: <repo>/state.db)")
    parser.add_argument("--dry-run", action="store_true",
                        help="count would-update without writing")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap rows per table (for testing/incremental)")
    parser.add_argument("--table", choices=("rejected", "evaluated", "both"),
                        default="both")
    args = parser.parse_args(argv)

    db_path = args.db
    if not Path(db_path).is_file():
        print(f"[ERROR] db not found: {db_path}", file=sys.stderr)
        return 2

    conn = open_conn(db_path)

    # Schema-asymmetry guard: refuse if the required cols don't exist
    if args.table in ("rejected", "both"):
        for col in ("hour_sin", "hour_cos", "sigma_winsorize",
                    "prob_breakeven_gap", "data_provenance"):
            if not _column_exists(conn, "rejected_opportunities", col):
                print(f"[ERROR] rejected_opportunities is missing column '{col}' "
                      "— refusing to backfill. Run the B.1a schema migration first.",
                      file=sys.stderr)
                return 3
    if args.table in ("evaluated", "both"):
        if not _column_exists(conn, "evaluated_opportunities", "prob_breakeven_gap"):
            print("[ERROR] evaluated_opportunities is missing 'prob_breakeven_gap' "
                  "— refusing to backfill.", file=sys.stderr)
            return 3

    print(f"[wave1_backfill] db={db_path} dry_run={args.dry_run} table={args.table}")

    if args.table in ("rejected", "both"):
        s = backfill_rejected(conn, dry_run=args.dry_run, limit=args.limit)
        print(f"[wave1_backfill] rejected_opportunities: "
              f"seen={s['rows_seen']} updated={s['rows_updated']} | "
              f"hour_sin={s['hour_sin']} hour_cos={s['hour_cos']} "
              f"sigma_winsorize={s['sigma_winsorize']} "
              f"prob_breakeven_gap={s['prob_breakeven_gap']} "
              f"data_provenance={s['data_provenance']}")

    if args.table in ("evaluated", "both"):
        s = backfill_evaluated(conn, dry_run=args.dry_run, limit=args.limit)
        print(f"[wave1_backfill] evaluated_opportunities: "
              f"seen={s['rows_seen']} updated={s['rows_updated']} | "
              f"prob_breakeven_gap={s['prob_breakeven_gap']}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
