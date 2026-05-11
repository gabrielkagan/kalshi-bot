#!/usr/bin/env python3
"""Layer C: retroactive reconciliation of IOC-path losses against Kalshi's
authoritative fills API.

Read-only. Never writes to state.db. Never modifies Kalshi state.

For each IOC-path 15m loss in the lookback window, fetches the fills Kalshi
actually recorded for that ticker, matches by order_id, sums real count, and
compares against positions.count / settled_trades.pnl_cents.

Output: one row per divergence, plus a summary of restated PnL.

Usage:
  python3 scripts/reconcile_ioc_losses.py [--days 30] [--product 15m] [--limit N]

Requires the same env vars as bot.py:
  KALSHI_API_KEY (or KALSHI_API_KEY_ID)
  KALSHI_PRIVATE_KEY_PATH
  STATE_DB_PATH (optional, defaults to ./state.db)
"""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bot.helpers.strings import fp_str_to_int, dollars_str_to_cents  # type: ignore
from bot.kalshi_client import KalshiClient


def load_client() -> KalshiClient:
    api_key = os.environ.get("KALSHI_API_KEY") or os.environ.get("KALSHI_API_KEY_ID", "")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    if not api_key or not key_path:
        print("ERROR: KALSHI_API_KEY (or KALSHI_API_KEY_ID) and KALSHI_PRIVATE_KEY_PATH must be set",
              file=sys.stderr)
        sys.exit(1)
    return KalshiClient(api_key, key_path)


def connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def fetch_losses(conn: sqlite3.Connection, days: int, product: str,
                 limit: int) -> list[dict]:
    """Pull IOC-path losses along with any known order_ids for each ticker."""
    cutoff = f"{days} days"
    sql = """
        SELECT s.ticker,
               s.asset,
               s.product_type,
               s.count       AS settled_count,
               s.entry_price_cents,
               s.revenue_cents,
               s.fee_cents,
               s.pnl_cents,
               s.settled_at,
               p.count       AS pos_count,
               p.total_cost_cents,
               p.avg_price_cents,
               p.opened_at,
               p.strategy,
               p.is_taker,
               p.fill_source,
               p.execution_method
        FROM settled_trades s
        JOIN positions p ON p.ticker = s.ticker
        WHERE s.settled_at >= datetime('now', ?)
          AND s.pnl_cents < 0
          AND s.revenue_cents = 0
          AND p.is_taker = 1
          AND p.fill_source = 'rest_poll'
          AND p.execution_method = 'ioc'
          AND s.product_type = ?
        ORDER BY s.pnl_cents ASC
        LIMIT ?
    """
    rows = conn.execute(sql, (f"-{cutoff}", product, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        order_rows = conn.execute(
            "SELECT order_id, client_order_id, count, price_cents, status, created_at "
            "FROM pending_orders WHERE ticker=? ORDER BY created_at",
            (d["ticker"],)
        ).fetchall()
        d["local_orders"] = [dict(o) for o in order_rows]
        out.append(d)
    return out


def fetch_kalshi_fills(client: KalshiClient, ticker: str) -> list[dict]:
    """Fetch all Kalshi fills for a ticker, following pagination if present."""
    all_fills: list[dict] = []
    resp = client.get_fills(ticker=ticker, limit=200)
    if not resp:
        return all_fills
    all_fills.extend(resp.get("fills", []) or [])
    # Some Kalshi endpoints paginate via `cursor`. The fills endpoint in this
    # client doesn't accept cursor, but if the API returns one we retry via
    # direct request.
    cursor = resp.get("cursor")
    guard = 0
    while cursor and guard < 20:
        guard += 1
        resp = client._request(
            "GET", f"/trade-api/v2/portfolio/fills",
            params={"ticker": ticker, "limit": 200, "cursor": cursor},
        )
        if not resp:
            break
        all_fills.extend(resp.get("fills", []) or [])
        cursor = resp.get("cursor")
        if not cursor:
            break
    return all_fills


def count_from_fill(fill: dict) -> int:
    """Extract integer fill count with the same fallbacks as bot._on_fill."""
    c_fp = fp_str_to_int(fill.get("count_fp")) if fill.get("count_fp") is not None else 0
    if c_fp:
        return c_fp
    return int(fill.get("count") or 0)


def price_from_fill(fill: dict, side: str) -> int:
    if side == "no":
        pd = fill.get("no_price_dollars")
        if pd:
            return dollars_str_to_cents(pd)
        return int(fill.get("no_price") or 0)
    pd = fill.get("yes_price_dollars")
    if pd:
        return dollars_str_to_cents(pd)
    return int(fill.get("yes_price") or 0)


def reconcile_one(row: dict, kalshi_fills: list[dict]) -> dict:
    """Match Kalshi fills against local order_ids, sum the authoritative count."""
    local_order_ids = {o["order_id"] for o in row["local_orders"] if o.get("order_id")}
    kalshi_matched: list[dict] = []
    kalshi_unmatched: list[dict] = []
    for f in kalshi_fills:
        if f.get("order_id") in local_order_ids:
            kalshi_matched.append(f)
        else:
            kalshi_unmatched.append(f)

    true_count = sum(count_from_fill(f) for f in kalshi_matched)
    true_cost = sum(count_from_fill(f) * price_from_fill(f, "yes") for f in kalshi_matched)

    local_count = row["pos_count"]
    avg_price = row["avg_price_cents"]

    # True PnL: for a LOSS with revenue=0, pnl = -true_cost
    true_pnl = -true_cost
    reported_pnl = row["pnl_cents"]
    pnl_delta = true_pnl - reported_pnl  # positive = we over-reported the loss

    return {
        "ticker": row["ticker"],
        "asset": row["asset"],
        "settled_at": row["settled_at"],
        "reported_count": local_count,
        "true_count": true_count,
        "count_delta": local_count - true_count,
        "reported_cost": row["total_cost_cents"],
        "true_cost": true_cost,
        "avg_price": avg_price,
        "reported_pnl_cents": reported_pnl,
        "true_pnl_cents": true_pnl,
        "pnl_delta_cents": pnl_delta,
        "n_kalshi_matched": len(kalshi_matched),
        "n_kalshi_unmatched": len(kalshi_unmatched),
        "n_local_orders": len(row["local_orders"]),
        "local_order_ids": list(local_order_ids),
    }


def print_report(results: list[dict]) -> None:
    print()
    print("=" * 100)
    print(f"{'TICKER':<32} {'ASSET':<5} {'LOC_CT':>6} {'KAL_CT':>6} "
          f"{'Δ_CT':>5} {'LOC_$':>8} {'KAL_$':>8} {'Δ_$':>8}")
    print("-" * 100)
    divergent = [r for r in results if r["count_delta"] != 0]
    matched = [r for r in results if r["count_delta"] == 0]
    for r in divergent:
        print(f"{r['ticker']:<32} {r['asset']:<5} "
              f"{r['reported_count']:>6} {r['true_count']:>6} "
              f"{r['count_delta']:>+5} "
              f"{r['reported_pnl_cents']/100:>+8.2f} "
              f"{r['true_pnl_cents']/100:>+8.2f} "
              f"{r['pnl_delta_cents']/100:>+8.2f}")
    print("-" * 100)
    print(f"Divergent rows:     {len(divergent):>4}")
    print(f"Matching rows:      {len(matched):>4}")
    total_reported = sum(r["reported_pnl_cents"] for r in results) / 100
    total_true = sum(r["true_pnl_cents"] for r in results) / 100
    total_delta = sum(r["pnl_delta_cents"] for r in results) / 100
    print(f"Reported PnL (sum): ${total_reported:>+10.2f}")
    print(f"True PnL (sum):     ${total_true:>+10.2f}")
    print(f"Restatement delta:  ${total_delta:>+10.2f}  "
          f"(positive = reported loss larger than real)")
    print("=" * 100)

    missing_from_kalshi = [r for r in results if r["n_kalshi_matched"] == 0]
    if missing_from_kalshi:
        print()
        print(f"WARNING: {len(missing_from_kalshi)} ticker(s) had zero Kalshi fills "
              f"matching any local order_id — they may be outside Kalshi's "
              f"retention window, or the order_ids never filled on Kalshi's side. "
              f"Listing first 10:")
        for r in missing_from_kalshi[:10]:
            print(f"  {r['ticker']}  local_orders={r['n_local_orders']}  "
                  f"reported_count={r['reported_count']}  "
                  f"unmatched_kalshi_fills={r['n_kalshi_unmatched']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30,
                    help="Lookback window in days (default 30)")
    ap.add_argument("--product", default="15m",
                    help="product_type to reconcile (default 15m)")
    ap.add_argument("--limit", type=int, default=200,
                    help="Max losses to examine (default 200)")
    ap.add_argument("--db", default=os.environ.get("STATE_DB_PATH", "state.db"),
                    help="Path to state.db")
    ap.add_argument("--only", default=None,
                    help="Single ticker to reconcile (debug one trade)")
    args = ap.parse_args()

    client = load_client()
    conn = connect_db(args.db)

    if args.only:
        rows = conn.execute(
            """SELECT s.ticker, s.asset, s.product_type, s.count AS settled_count,
                      s.entry_price_cents, s.revenue_cents, s.fee_cents, s.pnl_cents,
                      s.settled_at, p.count AS pos_count, p.total_cost_cents,
                      p.avg_price_cents, p.opened_at, p.strategy, p.is_taker,
                      p.fill_source, p.execution_method
               FROM settled_trades s JOIN positions p ON p.ticker=s.ticker
               WHERE s.ticker=?""",
            (args.only,)
        ).fetchall()
        losses = []
        for r in rows:
            d = dict(r)
            order_rows = conn.execute(
                "SELECT order_id, client_order_id, count, price_cents, status, created_at "
                "FROM pending_orders WHERE ticker=? ORDER BY created_at",
                (d["ticker"],)).fetchall()
            d["local_orders"] = [dict(o) for o in order_rows]
            losses.append(d)
    else:
        losses = fetch_losses(conn, args.days, args.product, args.limit)

    if not losses:
        print("No matching losses found.")
        return

    print(f"Reconciling {len(losses)} IOC-path losses against Kalshi fills API...")
    print(f"Lookback: {args.days} days,  product: {args.product}")

    results = []
    for i, row in enumerate(losses, 1):
        print(f"  [{i}/{len(losses)}] {row['ticker']} "
              f"(reported_count={row['pos_count']}, pnl=${row['pnl_cents']/100:.2f})",
              end="", flush=True)
        try:
            fills = fetch_kalshi_fills(client, row["ticker"])
            result = reconcile_one(row, fills)
            results.append(result)
            if result["count_delta"] != 0:
                print(f"  → DIVERGENT (kalshi={result['true_count']}, "
                      f"local={result['reported_count']})")
            else:
                print("  ✓")
        except Exception as e:
            print(f"  ERROR: {e}")
        time.sleep(0.1)  # gentle on rate limit

    print_report(results)


if __name__ == "__main__":
    main()
