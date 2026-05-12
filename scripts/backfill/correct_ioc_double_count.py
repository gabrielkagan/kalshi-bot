#!/usr/bin/env python3
"""One-time correction for IOC-path losses where local count diverged from
Kalshi's authoritative fill count (the "double-count" bug).

Dry-run by default. Pass --apply to write changes to state.db.

Before writing anything, this script RE-VERIFIES against Kalshi's fills API
so it's safe to re-run — if counts already match, it becomes a no-op.

Usage:
  python3 scripts/correct_ioc_double_count.py            # dry-run
  python3 scripts/correct_ioc_double_count.py --apply    # write changes
"""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys
from pathlib import Path

# Bit 11.2 (2026-05-12): relocated to scripts/backfill/; 3-level dirname.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bot.helpers.strings import fp_str_to_int, dollars_str_to_cents  # type: ignore
from bot.kalshi_client import KalshiClient
from bot.models import calculate_fee
from scripts.reconcile_ioc_losses import (  # type: ignore
    fetch_kalshi_fills, count_from_fill, price_from_fill,
)

# Tickers confirmed divergent by reconcile_ioc_losses.py --days 30.
# Script re-verifies against Kalshi before writing, so this list is just a
# safety allowlist — we never touch rows outside it.
CANDIDATES = [
    "KXXRP15M-26APR190615-15",
    "KXSOL15M-26MAR291315-15",
]


def load_client() -> KalshiClient:
    api_key = os.environ.get("KALSHI_API_KEY") or os.environ.get("KALSHI_API_KEY_ID", "")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    if not api_key or not key_path:
        print("ERROR: KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH must be set", file=sys.stderr)
        sys.exit(1)
    return KalshiClient(api_key, key_path)


def connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def true_count_from_kalshi(client: KalshiClient, conn: sqlite3.Connection,
                           ticker: str) -> tuple[int, int]:
    """Return (true_count, true_cost_cents) summing Kalshi fills that match
    any local order_id recorded for this ticker."""
    order_rows = conn.execute(
        "SELECT order_id FROM pending_orders WHERE ticker=?", (ticker,)
    ).fetchall()
    local_ids = {r["order_id"] for r in order_rows if r["order_id"]}
    fills = fetch_kalshi_fills(client, ticker)
    true_count = 0
    true_cost = 0
    for f in fills:
        if f.get("order_id") in local_ids:
            c = count_from_fill(f)
            p = price_from_fill(f, "yes")  # caller should restrict to YES-side losses for now
            true_count += c
            true_cost += c * p
    return true_count, true_cost


def correct_ticker(conn: sqlite3.Connection, client: KalshiClient,
                   ticker: str, apply: bool) -> dict:
    """Re-verify and optionally correct one ticker. Returns a summary dict."""
    pos = conn.execute(
        "SELECT ticker, count, total_cost_cents, avg_price_cents, "
        "accumulated_fee_cents, strategy_group, is_taker "
        "FROM positions WHERE ticker=?",
        (ticker,)
    ).fetchone()
    if not pos:
        return {"ticker": ticker, "status": "no_position_row"}

    st = conn.execute(
        "SELECT ticker, count, entry_price_cents, revenue_cents, fee_cents, "
        "pnl_cents FROM settled_trades WHERE ticker=?",
        (ticker,)
    ).fetchone()
    if not st:
        return {"ticker": ticker, "status": "no_settled_row"}

    true_count, true_cost = true_count_from_kalshi(client, conn, ticker)
    if true_count == 0:
        return {"ticker": ticker, "status": "no_kalshi_fills_matched"}

    local_count = pos["count"]
    if local_count == true_count:
        return {"ticker": ticker, "status": "already_matches",
                "local_count": local_count, "true_count": true_count}

    # Losses: revenue=0, pnl = -true_cost
    avg_price = pos["avg_price_cents"]
    # Recompute fees with calculate_fee at the corrected count (approximation —
    # Kalshi's per-fill fees sum slightly differently, but off by pennies).
    new_fee = calculate_fee(true_count, avg_price, is_taker=bool(pos["is_taker"]))
    new_cost = true_cost if true_cost else true_count * avg_price
    new_pnl = 0 - new_cost  # LOSS: revenue = 0

    before = {
        "pos_count": pos["count"], "pos_cost": pos["total_cost_cents"],
        "pos_fee": pos["accumulated_fee_cents"],
        "st_count": st["count"], "st_fee": st["fee_cents"],
        "st_pnl": st["pnl_cents"],
    }
    after = {
        "pos_count": true_count, "pos_cost": new_cost, "pos_fee": new_fee,
        "st_count": true_count, "st_fee": new_fee, "st_pnl": new_pnl,
    }

    if apply:
        conn.execute(
            "UPDATE positions SET count=?, total_cost_cents=?, accumulated_fee_cents=? "
            "WHERE ticker=? AND strategy_group=?",
            (after["pos_count"], after["pos_cost"], after["pos_fee"],
             ticker, pos["strategy_group"])
        )
        conn.execute(
            "UPDATE settled_trades SET count=?, fee_cents=?, pnl_cents=? WHERE ticker=?",
            (after["st_count"], after["st_fee"], after["st_pnl"], ticker)
        )
        conn.commit()

    return {"ticker": ticker, "status": "corrected" if apply else "would_correct",
            "before": before, "after": after,
            "pnl_delta_cents": after["st_pnl"] - before["st_pnl"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually write changes (default: dry-run)")
    ap.add_argument("--db", default=os.environ.get("STATE_DB_PATH", "state.db"))
    args = ap.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"Running in {mode} mode against {args.db}")
    print(f"Verifying {len(CANDIDATES)} candidate tickers against Kalshi...")
    print()

    client = load_client()
    conn = connect_db(args.db)

    total_pnl_delta = 0
    for ticker in CANDIDATES:
        result = correct_ticker(conn, client, ticker, apply=args.apply)
        print(f"--- {ticker} ---")
        print(f"  status: {result['status']}")
        if result["status"] in ("corrected", "would_correct"):
            b, a = result["before"], result["after"]
            print(f"  positions.count:        {b['pos_count']:>6} -> {a['pos_count']:>6}")
            print(f"  positions.total_cost:   {b['pos_cost']:>6}c -> {a['pos_cost']:>6}c")
            print(f"  positions.fee:          {b['pos_fee']:>6}c -> {a['pos_fee']:>6}c")
            print(f"  settled_trades.count:   {b['st_count']:>6} -> {a['st_count']:>6}")
            print(f"  settled_trades.fee:     {b['st_fee']:>6}c -> {a['st_fee']:>6}c")
            print(f"  settled_trades.pnl:     {b['st_pnl']:>6}c -> {a['st_pnl']:>6}c  "
                  f"(Δ ${result['pnl_delta_cents']/100:+.2f})")
            total_pnl_delta += result["pnl_delta_cents"]
        elif result["status"] == "already_matches":
            print(f"  local={result['local_count']} kalshi={result['true_count']} — no-op")
        print()

    print(f"Total PnL restatement: ${total_pnl_delta/100:+.2f}")
    if not args.apply:
        print("(dry-run — pass --apply to write changes)")


if __name__ == "__main__":
    main()
