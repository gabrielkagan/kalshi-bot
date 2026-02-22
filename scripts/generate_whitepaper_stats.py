#!/usr/bin/env python3
"""Query state.db on the VPS and output whitepaper stats as JSON."""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state.db")


def main():
    if not os.path.exists(DB_PATH):
        print(json.dumps({"error": f"state.db not found at {DB_PATH}"}))
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    stats = {}

    # Total evaluated opportunities
    row = conn.execute("SELECT COUNT(*) AS n FROM evaluated_opportunities").fetchone()
    stats["total_evaluated"] = row["n"] if row else 0

    # Filter breakdown
    rows = conn.execute(
        "SELECT filter_stage, COUNT(*) AS n FROM evaluated_opportunities GROUP BY filter_stage"
    ).fetchall()
    stats["filter_breakdown"] = {r["filter_stage"]: r["n"] for r in rows}

    # Settled trades
    row = conn.execute("SELECT COUNT(*) AS n FROM settled_trades").fetchone()
    stats["total_settled"] = row["n"] if row else 0

    # Win/loss from settled trades
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM settled_trades WHERE pnl_cents > 0"
    ).fetchone()
    stats["total_wins"] = row["n"] if row else 0

    row = conn.execute(
        "SELECT COALESCE(SUM(pnl_cents), 0) AS total FROM settled_trades"
    ).fetchone()
    stats["observation_pnl"] = row["total"] if row else 0

    # Total trades (hypothetical candidates that passed all filters)
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM evaluated_opportunities WHERE filter_stage = 'candidate'"
    ).fetchone()
    stats["total_trades"] = row["n"] if row else 0

    # Win rate by price bucket from settled trades
    buckets = {"80-84": (80, 84), "85-89": (85, 89), "90-94": (90, 94), "95-99": (95, 99)}
    win_rate_by_price = {}
    for label, (lo, hi) in buckets.items():
        row = conn.execute(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) AS wins "
            "FROM settled_trades WHERE entry_price_cents >= ? AND entry_price_cents <= ?",
            (lo, hi),
        ).fetchone()
        win_rate_by_price[label] = {
            "n": row["n"] if row else 0,
            "wins": row["wins"] if row and row["wins"] else 0,
        }
    stats["win_rate_by_price"] = win_rate_by_price

    # Assets tracked
    rows = conn.execute(
        "SELECT DISTINCT asset FROM evaluated_opportunities WHERE asset IS NOT NULL"
    ).fetchall()
    stats["assets_tracked"] = sorted([r["asset"] for r in rows]) if rows else ["BTC", "ETH", "SOL", "XRP"]

    # Observation period
    row = conn.execute(
        "SELECT MIN(evaluation_time) AS first, MAX(evaluation_time) AS last FROM evaluated_opportunities"
    ).fetchone()
    if row and row["first"] and row["last"]:
        stats["observation_period"] = f"{row['first'][:10]} to {row['last'][:10]}"
    else:
        stats["observation_period"] = "N/A"

    stats["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn.close()

    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
