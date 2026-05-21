"""Bit F (86ba1wpck, 2026-05-21) — `historical_replay_calmlp` schema migration.

Widens the asset CHECK constraint from `('HYPE','DOGE')` → `('HYPE','DOGE','BNB')`
and adds `spot_staleness_seconds REAL` column. SQLite's `ALTER TABLE` does NOT
support modifying CHECK constraints in-place, so this migration uses the
standard CREATE-COPY-DROP-RENAME pattern.

Idempotent — detects post-migration schema and no-ops; safe to re-run.

Mac-only by convention (per `feedback_vps_compute_isolation.md`). Refuses paths
under `/home/botuser/` as a defensive guard.

Usage:
    python -m scripts.ops.migrate_replay_table_bit_f --db data/replay/state.db

Acceptance:
    1. Asset CHECK includes 'BNB' post-run.
    2. `spot_staleness_seconds REAL` column exists post-run.
    3. Existing HYPE+DOGE row counts preserved across the rebuild.
    4. PK `(ticker, evaluation_time)` preserved.
    5. Pre-existing data_provenance, result, and other CHECKs preserved.
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from typing import Optional


REPLAY_TABLE = "historical_replay_calmlp"
TMP_TABLE = "historical_replay_calmlp_bit_f_new"


# Post-Bit-F target schema. Keep in lock-step with
# `scripts/backfill/crypto_replay_backfill.py::ensure_schema`. Column order
# matches the production schema (threshold + spot_staleness_seconds slotted
# in the same positions as the ensure_schema CREATE).
_TARGET_SCHEMA_DDL = f"""
CREATE TABLE {TMP_TABLE} (
    ticker TEXT NOT NULL,
    evaluation_time TEXT NOT NULL,
    asset TEXT NOT NULL CHECK (asset IN ('HYPE','DOGE','BNB')),
    strike_cents INTEGER,
    threshold REAL,
    close_time TEXT,
    open_time TEXT,
    raw_prob REAL,
    calibrated_prob REAL,
    blended_prob REAL,
    spot_at_evaluation REAL,
    sigma_at_evaluation REAL,
    hour_sin REAL,
    hour_cos REAL,
    prob_breakeven_gap REAL,
    sigma_winsorize REAL,
    spot_staleness_seconds REAL,
    result TEXT NOT NULL CHECK (result IN ('yes','no')),
    settlement_value INTEGER,
    data_provenance TEXT NOT NULL,
    replay_run_ts INTEGER NOT NULL,
    PRIMARY KEY (ticker, evaluation_time)
)
""".strip()


def _refuse_vps_path(db_path: str) -> None:
    """Mac-only defensive guard. Refuses paths under /home/botuser/."""
    abs_path = os.path.abspath(db_path)
    if abs_path.startswith("/home/botuser/"):
        raise SystemExit(
            f"Refusing to operate on VPS path {abs_path}. "
            "This migration is Mac-only per feedback_vps_compute_isolation.md."
        )


def _schema_state(conn: sqlite3.Connection) -> str:
    """Probe the live schema. Returns one of:
       - 'absent' (no replay table)
       - 'pre_bit_f' (CHECK does NOT include BNB OR missing spot_staleness)
       - 'post_bit_f' (CHECK includes BNB AND spot_staleness exists)
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (REPLAY_TABLE,),
    ).fetchone()
    if row is None:
        return "absent"
    sql_text = row[0] or ""
    cols = {
        r[1]
        for r in conn.execute(f"PRAGMA table_info({REPLAY_TABLE})").fetchall()
    }
    has_bnb = "'BNB'" in sql_text
    has_staleness = "spot_staleness_seconds" in cols
    if has_bnb and has_staleness:
        return "post_bit_f"
    return "pre_bit_f"


def _column_overlap(conn: sqlite3.Connection) -> list[str]:
    """Columns that exist in BOTH the existing replay table AND target schema.

    Source-of-truth for the INSERT...SELECT migration column list. Built from
    PRAGMA so the operator's older schema (pre-fu2 / pre-Bit-F) doesn't trip
    a hard reference to columns that don't exist yet.
    """
    target_cols = [
        "ticker", "evaluation_time", "asset", "strike_cents", "threshold",
        "close_time", "open_time", "raw_prob", "calibrated_prob",
        "blended_prob", "spot_at_evaluation", "sigma_at_evaluation",
        "hour_sin", "hour_cos", "prob_breakeven_gap", "sigma_winsorize",
        "spot_staleness_seconds",
        "result", "settlement_value", "data_provenance", "replay_run_ts",
    ]
    existing = {
        r[1]
        for r in conn.execute(f"PRAGMA table_info({REPLAY_TABLE})").fetchall()
    }
    return [c for c in target_cols if c in existing]


def migrate(db_path: str, *, dry_run: bool = False) -> str:
    """Run the migration. Returns one of: 'noop' | 'migrated' | 'created'."""
    _refuse_vps_path(db_path)

    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")

        state = _schema_state(conn)
        logging.info("Pre-migration schema state: %s", state)

        if state == "post_bit_f":
            logging.info("Schema already at Bit F target; no-op.")
            return "noop"

        if state == "absent":
            if dry_run:
                logging.info("Dry-run: would CREATE %s", REPLAY_TABLE)
                return "noop"
            # Create the target schema directly (no migration needed).
            conn.execute(_TARGET_SCHEMA_DDL.replace(TMP_TABLE, REPLAY_TABLE))
            conn.commit()
            return "created"

        # state == 'pre_bit_f': CREATE-COPY-DROP-RENAME.
        n_existing = conn.execute(
            f"SELECT COUNT(*) FROM {REPLAY_TABLE}"
        ).fetchone()[0]
        cols = _column_overlap(conn)
        col_list = ", ".join(cols)
        logging.info(
            "pre_bit_f migration: %d existing rows; columns to copy: %s",
            n_existing, col_list,
        )

        if dry_run:
            logging.info(
                "Dry-run: would CREATE %s + INSERT...SELECT %d rows + DROP + RENAME",
                TMP_TABLE, n_existing,
            )
            return "noop"

        # Drop any stale TMP_TABLE from a previous failed run (idempotent).
        conn.execute(f"DROP TABLE IF EXISTS {TMP_TABLE}")
        conn.execute(_TARGET_SCHEMA_DDL)
        conn.execute(
            f"INSERT INTO {TMP_TABLE} ({col_list}) "
            f"SELECT {col_list} FROM {REPLAY_TABLE}"
        )
        n_copied = conn.execute(
            f"SELECT COUNT(*) FROM {TMP_TABLE}"
        ).fetchone()[0]
        if n_copied != n_existing:
            raise RuntimeError(
                f"Row-count mismatch post-COPY: {n_existing} → {n_copied}; "
                "rolling back."
            )
        conn.execute(f"DROP TABLE {REPLAY_TABLE}")
        conn.execute(f"ALTER TABLE {TMP_TABLE} RENAME TO {REPLAY_TABLE}")
        conn.commit()
        logging.info(
            "Migration complete: rebuilt %s with %d rows preserved.",
            REPLAY_TABLE, n_copied,
        )
        return "migrated"
    finally:
        conn.close()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Bit F migration — widen asset CHECK + add spot_staleness_seconds."
    )
    ap.add_argument("--db", required=True, help="path to replay state.db")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would happen; no writes",
    )
    ap.add_argument(
        "--log-level",
        default="INFO",
        help="logging level (DEBUG/INFO/WARNING/ERROR)",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    outcome = migrate(args.db, dry_run=args.dry_run)
    logging.info("Outcome: %s", outcome)
    return 0


if __name__ == "__main__":
    sys.exit(main())
