#!/usr/bin/env python3
"""Nightly cohort attribution aggregation — Money Printer Roadmap P1.1.

Runs at 13:07 UTC via systemd timer (intentionally 6 minutes before the
P1.4 Monday 13:13 weekly cron so the weekly report reads a post-nightly
commit). Materializes one row per (asset × product_type × strategy ×
price_band_5c × stc_band_60s × cell_block_stage) for the current
cohort_date.

Idempotent on `cohort_date` (composite-PK upsert via INSERT OR REPLACE),
so a rerun within the same UTC day overwrites the existing rows with
freshly-computed values. A one-shot 73d backfill should be performed
once by the operator using `--backfill-days N` (default 0 = today only).

`bot.helpers.cohort_alerts` import is optional — graceful fallback per
the P1.1 design § Alert state write-back protocol option (a). When the
sister Bit P1.3 ships, the alert triggers activate without any change to
this script.

Ticket: ClickUp 86b9x3kgd (P1.1).
Design: kb/decisions/cohort-measurement-design-may12.md.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bot.helpers.cohort_attribution import (  # noqa: E402
    ensure_schema,
    run_aggregation,
)

DEFAULT_DB_PATH = os.environ.get("KALSHI_STATE_DB", str(REPO_ROOT / "state.db"))


def _open_conn(db_path: str) -> sqlite3.Connection:
    """Open a sqlite3 connection with WAL + busy_timeout pragmas per
    scripts/CLAUDE.md."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"Path to state.db (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=0,
        help=(
            "One-shot historical backfill. If N>0, runs aggregation for each "
            "of the last N+1 UTC dates (today and N days back). Default 0 = "
            "today only."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [COHORT_NIGHTLY] %(levelname)s %(message)s",
    )

    alerts_module = None
    try:
        import bot.helpers.cohort_alerts as _alerts_mod  # noqa: F401
        alerts_module = _alerts_mod
        logging.info("cohort_alerts module imported; alert state will be evaluated")
    except ImportError:
        logging.info(
            "cohort_alerts module not yet shipped (sister Bit P1.3 pending); "
            "running in graceful-fallback mode — alert_state will be 'quiet' for all rows"
        )

    conn = _open_conn(args.db)
    try:
        ensure_schema(conn)

        today = _dt.datetime.now(_dt.timezone.utc).date()
        dates = [today - _dt.timedelta(days=i) for i in range(args.backfill_days + 1)]
        dates.reverse()  # oldest first so persistence_days lookups have history

        for d in dates:
            date_str = d.isoformat()
            logging.info("aggregating cohorts for cohort_date=%s", date_str)
            run_aggregation(
                conn,
                alerts_module=alerts_module,
                cohort_date=date_str,
                now=_dt.datetime.combine(d, _dt.time(13, 7), tzinfo=_dt.timezone.utc),
            )
            n = conn.execute(
                "SELECT COUNT(*) FROM cohort_attribution_daily WHERE cohort_date=?",
                (date_str,),
            ).fetchone()[0]
            logging.info("  -> %d cohort rows materialized for %s", n, date_str)

        return 0
    except Exception:
        logging.exception("cohort_attribution_nightly failed")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
