#!/usr/bin/env python3
"""B4 (ticket 86b9zudcc, 2026-05-18): retroactive Kalshi-truth audit of
``settled_trades`` rows.

Read-only against ``state.db`` by default. Use ``--apply`` to write
``phantom_corrections`` rows to ``state.db``; the table is created on first
run. Never modifies Kalshi state and never mutates ``settled_trades`` rows
(those stay as the historical write-time record; ``phantom_corrections`` is
an append-only ledger of authoritative restatements).

For each unique ticker with a ``settled_trades`` row in the lookback window:

1. Aggregate the local ledger across all strategy_groups for that ticker:
   ``local_count = SUM(count)``, ``local_pnl_cents = SUM(pnl_cents)``.  # noqa: gross-vs-gross comparison vs settled_trades.pnl_cents (which is itself gross per scripts/CLAUDE.md); fee_cents intentionally excluded
2. Query Kalshi for ground truth:
   - ``GET /portfolio/settlements?ticker=<ticker>`` → settled revenue in cents.
   - ``GET /portfolio/fills?ticker=<ticker>`` → authoritative filled count
     (matched on side, paginated via the same cursor pattern as
     ``scripts/audit/reconcile_ioc_losses.py``).
3. ``kalshi_count`` = sum of side-matched fill counts (the only signal
   that works for both WIN and LOSS — the settlements endpoint can only
   imply count on WINs via ``revenue // 100``).
4. When ``kalshi_count != local_count``: compute the corrected pnl
   (``kalshi_revenue_cents - kalshi_count * avg_price_cents``) and write a
   ``phantom_corrections`` row capturing the delta.

The audit is restartable: rows are inserted with ``INSERT OR REPLACE`` on
``(audit_run_id, ticker, side)`` — re-running with a fresh ``--run-id``
will append a new batch without colliding with prior batches. The
``side`` column is part of the UNIQUE clause because weather/hourly
observation modes can carry both YES and NO rows on a single ticker
(see R1-M5 + ``fetch_settled_tickers`` `GROUP BY ticker, side`).

Usage:
  python3 scripts/audit/phantom_pnl_audit.py --days 14 --dry-run
  python3 scripts/audit/phantom_pnl_audit.py --days 14 --apply --run-id may18

Requires the same env vars as the bot:
  KALSHI_API_KEY (or KALSHI_API_KEY_ID)
  KALSHI_PRIVATE_KEY_PATH
  STATE_DB_PATH (optional, defaults to ./state.db)
"""
from __future__ import annotations

import argparse
import datetime
import logging
import os
import sqlite3
import sys
from datetime import timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bot.helpers.strings import dollars_str_to_cents, fp_str_to_int  # noqa: E402
from bot.kalshi_client import KalshiClient  # noqa: E402


PHANTOM_CORRECTIONS_DDL = """
-- B4 (86b9zudcc, 2026-05-18): retroactive append-only ledger of
-- settled_trades rows whose count diverged from Kalshi truth. corrected_*
-- columns are GROSS pnl (revenue − count × avg_price), excluding fees,
-- mirroring settled_trades.pnl_cents semantics (scripts/CLAUDE.md). Net
-- consumers re-join settled_trades.fee_cents downstream.
-- UNIQUE(audit_run_id, ticker, side) keeps re-runs of the same run-id
-- idempotent (INSERT OR REPLACE updates the existing row in place).
CREATE TABLE IF NOT EXISTS phantom_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_run_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT,
    detected_at TEXT NOT NULL,
    local_count INTEGER NOT NULL,
    kalshi_count INTEGER NOT NULL,
    delta_count INTEGER NOT NULL,           -- local − kalshi (positive = over-count)
    local_pnl_cents INTEGER NOT NULL,       -- GROSS (excludes fees)
    corrected_pnl_cents INTEGER NOT NULL,   -- GROSS (excludes fees) — rebuilt from Kalshi truth
    delta_pnl_cents INTEGER NOT NULL,       -- corrected − local (positive = local was MORE NEGATIVE / overstated loss)
    kalshi_revenue_cents INTEGER,
    avg_price_cents INTEGER,
    market_result TEXT,
    audit_window_days INTEGER,
    notes TEXT,
    UNIQUE(audit_run_id, ticker, side)
);
"""

PHANTOM_CORRECTIONS_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_phantom_corrections_ticker "
    "ON phantom_corrections(ticker);"
)


def connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def load_client() -> KalshiClient:
    api_key = (os.environ.get("KALSHI_API_KEY")
               or os.environ.get("KALSHI_API_KEY_ID", ""))
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    if not api_key or not key_path:
        print(
            "ERROR: KALSHI_API_KEY (or KALSHI_API_KEY_ID) and "
            "KALSHI_PRIVATE_KEY_PATH must be set",
            file=sys.stderr,
        )
        sys.exit(1)
    return KalshiClient(api_key, key_path)


def ensure_phantom_table(conn: sqlite3.Connection) -> None:
    conn.execute(PHANTOM_CORRECTIONS_DDL)
    conn.execute(PHANTOM_CORRECTIONS_INDEX_DDL)
    conn.commit()


def fetch_settled_tickers(conn: sqlite3.Connection,
                          days: int) -> List[Dict]:
    """Aggregate settled_trades across strategy_groups, per ``(ticker, side)``.

    R1-M5 fix: weather/hourly observation modes can stack BOTH YES and NO
    rows on a single ticker. Aggregating by ticker only would compare an
    all-rows local sum against side-filtered Kalshi fills → false
    divergence. The audit's natural unit is therefore the
    ``(ticker, side)`` pair, mirroring the bot's per-side accounting.
    """
    sql = """
        SELECT ticker,
               side,
               MAX(event_ticker) AS event_ticker,
               MAX(asset) AS asset,
               MAX(market_result) AS market_result,
               SUM(count) AS local_count,
               -- entry_price_cents is per-row; weighted average gives the
               -- best single number for cost-basis reconstruction.
               CAST(ROUND(SUM(count * entry_price_cents) * 1.0
                          / NULLIF(SUM(count), 0)) AS INTEGER)
                   AS avg_price_cents,
               SUM(revenue_cents) AS local_revenue_cents,
               SUM(pnl_cents) AS local_pnl_cents,  -- noqa: gross-vs-gross comparison; the corrected counterpart (kalshi_revenue - kalshi_count*avg_price) is also gross — net subtraction would mis-pair the two surfaces. Net consumers re-join fee_cents downstream.
               SUM(fee_cents) AS local_fee_cents,
               MIN(settled_at) AS settled_at
        FROM settled_trades
        WHERE settled_at >= datetime('now', ?)
        GROUP BY ticker, side
        ORDER BY settled_at
    """
    rows = conn.execute(sql, (f"-{days} days",)).fetchall()
    return [dict(r) for r in rows]


def fetch_kalshi_truth(client: KalshiClient, ticker: str,
                       side: str) -> Tuple[int, int, bool]:
    """Return ``(kalshi_count, kalshi_revenue_cents, pagination_complete)``.

    Count comes from ``/portfolio/fills`` filtered by side (sum of fill
    counts). Revenue comes from ``/portfolio/settlements?ticker=<ticker>``
    using the bot's existing dollars-or-cents fallback shape. The third
    return is ``False`` when fills pagination was interrupted by a
    transient API failure (R1-M7) — callers MUST treat partial data as
    "unverified" rather than emit a phantom-correction row that would
    encode incomplete fill state as ground truth.
    """
    revenue_cents = 0
    settle_resp = client.get_settlements(ticker=ticker, limit=10)
    if settle_resp:
        for s in settle_resp.get("settlements") or []:
            if s.get("ticker") != ticker:
                continue
            rev_d = s.get("revenue_dollars")
            if rev_d:
                revenue_cents = dollars_str_to_cents(rev_d)
            else:
                revenue_cents = int(s.get("revenue") or 0)
            break

    fills_count = 0
    pagination_complete = True
    fills_resp = client.get_fills(ticker=ticker, limit=200)
    cursor: Optional[str] = None
    guard = 0
    while True:
        if fills_resp is None:
            # Transient failure mid-pagination — partial fills_count is not
            # safe to compare against local. R1-M7: distinguish "no more
            # pages" (cursor=None) from "page fetch failed" (None response).
            pagination_complete = False
            break
        for f in fills_resp.get("fills") or []:
            if f.get("side") != side:
                continue
            c = fp_str_to_int(f.get("count_fp")) or int(f.get("count") or 0)
            fills_count += c
        cursor = fills_resp.get("cursor")
        if not cursor or guard >= 20:
            break
        guard += 1
        fills_resp = client._request(
            "GET", "/trade-api/v2/portfolio/fills",
            params={"ticker": ticker, "limit": 200, "cursor": cursor},
        )

    return fills_count, revenue_cents, pagination_complete


def audit_ticker(client: KalshiClient,
                 ticker_row: Dict) -> Tuple[Optional[Dict], str]:
    """Audit one ``(ticker, side)`` row.

    Returns ``(correction_dict_or_None, status)`` where ``status`` is one
    of: ``"divergent"`` (correction returned), ``"matched"`` (counts agree,
    no correction), ``"unverified"`` (Kalshi returned no usable truth —
    pagination interrupted, zero count + zero revenue, etc.). The
    tri-state status is what ``run_audit`` uses to populate the summary
    counters honestly (R1-N1: previously `n_unverified` was always 0).
    """
    ticker = ticker_row["ticker"]
    side = ticker_row["side"] or "yes"
    local_count = int(ticker_row["local_count"] or 0)
    local_pnl_cents = int(ticker_row["local_pnl_cents"] or 0)
    avg_price_cents = int(ticker_row["avg_price_cents"] or 0)
    market_result = ticker_row["market_result"] or ""

    kalshi_count, kalshi_revenue_cents, pagination_complete = fetch_kalshi_truth(
        client, ticker, side)

    # Pagination interrupted → fills_count is partial; not safe to compare.
    if not pagination_complete:
        return None, "unverified"

    # If Kalshi returned zero count AND zero revenue, the audit could not
    # verify (transient API failure, fills purged, etc.). Skip rather than
    # invent a divergence.
    if kalshi_count == 0 and kalshi_revenue_cents == 0:
        return None, "unverified"

    delta_count = local_count - kalshi_count
    if delta_count == 0:
        return None, "matched"

    # Corrected pnl_cents from Kalshi truth: revenue minus cost basis at
    # the locally-recorded avg price (the price WAS recorded correctly at
    # fill time — the count was the lie). Fees are intentionally NOT
    # subtracted: this is a gross-pnl restatement that mirrors
    # ``settled_trades.pnl_cents`` semantics (the column is gross per
    # scripts/CLAUDE.md "settled_trades.pnl_cents is GROSS, not net").
    corrected_pnl_cents = kalshi_revenue_cents - (kalshi_count * avg_price_cents)
    delta_pnl_cents = corrected_pnl_cents - local_pnl_cents

    return {
        "ticker": ticker,
        "local_count": local_count,
        "kalshi_count": kalshi_count,
        "delta_count": delta_count,
        "local_pnl_cents": local_pnl_cents,
        "corrected_pnl_cents": corrected_pnl_cents,
        "delta_pnl_cents": delta_pnl_cents,
        "kalshi_revenue_cents": kalshi_revenue_cents,
        "avg_price_cents": avg_price_cents,
        "market_result": market_result,
        "side": side,
    }, "divergent"


def write_correction(conn: sqlite3.Connection, audit_run_id: str,
                     days: int, correction: Dict, notes: str = "") -> None:
    now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT OR REPLACE INTO phantom_corrections "
        "(audit_run_id, ticker, side, detected_at, local_count, kalshi_count, "
        "delta_count, local_pnl_cents, corrected_pnl_cents, delta_pnl_cents, "
        "kalshi_revenue_cents, avg_price_cents, market_result, "
        "audit_window_days, notes) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (audit_run_id, correction["ticker"], correction["side"], now,
         correction["local_count"], correction["kalshi_count"],
         correction["delta_count"], correction["local_pnl_cents"],
         correction["corrected_pnl_cents"], correction["delta_pnl_cents"],
         correction["kalshi_revenue_cents"], correction["avg_price_cents"],
         correction["market_result"], days, notes),
    )
    conn.commit()


def run_audit(conn: sqlite3.Connection, client: KalshiClient, *,
              audit_run_id: str, days: int,
              apply: bool, limit: Optional[int] = None) -> Dict:
    """Iterate settled tickers in window; write phantom_corrections rows."""
    if apply:
        ensure_phantom_table(conn)
    tickers = fetch_settled_tickers(conn, days)
    if limit:
        tickers = tickers[:limit]

    n_audited = 0
    n_divergent = 0
    n_unverified = 0
    n_matched = 0
    sum_delta_count = 0
    sum_delta_pnl_cents = 0
    findings: List[Dict] = []

    for row in tickers:
        n_audited += 1
        try:
            correction, status = audit_ticker(client, row)
        except Exception as e:
            logging.warning("phantom_pnl_audit: %s failed: %s",
                            row.get("ticker"), e)
            n_unverified += 1
            continue

        if status == "unverified":
            n_unverified += 1
            continue
        if status == "matched":
            n_matched += 1
            continue
        # status == "divergent"
        n_divergent += 1
        sum_delta_count += correction["delta_count"]
        sum_delta_pnl_cents += correction["delta_pnl_cents"]
        findings.append(correction)
        if apply:
            write_correction(conn, audit_run_id, days, correction)

    return {
        "n_audited": n_audited,
        "n_divergent": n_divergent,
        "n_unverified": n_unverified,
        "n_matched": n_matched,
        "sum_delta_count": sum_delta_count,
        "sum_delta_pnl_cents": sum_delta_pnl_cents,
        "findings": findings,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("STATE_DB_PATH",
                                                        "./state.db"),
                        help="Path to state.db")
    parser.add_argument("--days", type=int, default=14,
                        help="Lookback window in days (default: 14)")
    parser.add_argument("--apply", action="store_true",
                        help="Write phantom_corrections rows (default: dry-run)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Explicit dry-run (no DB writes)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap audited tickers (debugging)")
    parser.add_argument("--run-id", default=None,
                        help="audit_run_id stamp (default: ISO date)")
    args = parser.parse_args()

    apply_writes = args.apply and not args.dry_run
    audit_run_id = args.run_id or datetime.datetime.now(
        timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    client = load_client()
    conn = connect_db(args.db)
    try:
        summary = run_audit(
            conn, client, audit_run_id=audit_run_id, days=args.days,
            apply=apply_writes, limit=args.limit)
    finally:
        conn.close()

    mode = "APPLY" if apply_writes else "DRY-RUN"
    print(f"\nphantom_pnl_audit [{mode}] window={args.days}d "
          f"run_id={audit_run_id}")
    print(f"  pairs audited     : {summary['n_audited']}")
    print(f"  divergent         : {summary['n_divergent']}")
    print(f"  matched           : {summary['n_matched']}")
    print(f"  unverified        : {summary['n_unverified']}")
    print(f"  delta_count_total : {summary['sum_delta_count']:+d}")
    print(f"  delta_pnl_total   : ${summary['sum_delta_pnl_cents'] / 100:+.2f}")
    if summary["findings"]:
        print("\nTop divergent rows (by |delta_pnl_cents| desc):")
        ranked = sorted(summary["findings"],
                        key=lambda d: abs(d["delta_pnl_cents"]),
                        reverse=True)
        for c in ranked[:20]:
            print(f"  {c['ticker']:40s} local={c['local_count']:>4d} "
                  f"kalshi={c['kalshi_count']:>4d} "
                  f"delta={c['delta_count']:+d}ct "
                  f"pnl_local=${c['local_pnl_cents']/100:+.2f} -> "
                  f"corrected=${c['corrected_pnl_cents']/100:+.2f} "
                  f"(delta=${c['delta_pnl_cents']/100:+.2f})")


if __name__ == "__main__":
    main()
