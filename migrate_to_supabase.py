#!/usr/bin/env python3
"""One-time migration: load existing SQLite state.db data into Supabase.

Run from VPS after:
  1. Schema applied via Supabase SQL Editor
  2. SUPABASE_URL and SUPABASE_SERVICE_KEY set in environment

Usage:
  source ~/.env
  python3 migrate_to_supabase.py [--dry-run]
"""

import os
import sys
import json
import sqlite3
import logging
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
BATCH_SIZE = 100
TIMEOUT = 15

DRY_RUN = "--dry-run" in sys.argv


def _headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }


def _post(table: str, rows: list) -> int:
    """UPSERT a batch of rows into a Supabase table. Returns count inserted."""
    if not rows:
        return 0
    if DRY_RUN:
        logging.info(f"  [DRY RUN] Would upsert {len(rows)} rows into {table}")
        return len(rows)
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    resp = requests.post(url, headers=_headers(), json=rows, timeout=TIMEOUT)
    if resp.status_code in (200, 201):
        return len(rows)
    # 409 = conflict (already exists) — fine for idempotent upserts
    if resp.status_code == 409:
        logging.warning(f"  {table}: conflict on {len(rows)} rows (already migrated?)")
        return len(rows)
    logging.error(f"  {table}: HTTP {resp.status_code} — {resp.text[:500]}")
    return 0


def _batched_upsert(table: str, rows: list) -> int:
    """Upsert rows in batches. Returns total count."""
    total = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        total += _post(table, batch)
    return total


def _clean(val):
    """Convert SQLite values for JSON serialization."""
    if val is None:
        return None
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return val


def migrate_trades(conn):
    """settled_trades → trades"""
    logging.info("Migrating settled_trades → trades...")
    rows = conn.execute("""
        SELECT st.ticker, st.event_ticker, st.asset, st.market_result,
               st.side, st.count, st.entry_price_cents, st.revenue_cents,
               st.fee_cents, st.pnl_cents, st.settled_at,
               st.strategy, st.seconds_to_close, st.fill_latency_seconds,
               st.vol_regime, st.calibrated_prob, st.edge, st.kelly_f
        FROM settled_trades st
    """).fetchall()

    mapped = []
    for r in rows:
        mapped.append({
            "ticker": r["ticker"],
            "event_ticker": r["event_ticker"],
            "asset": r["asset"],
            "market_result": r["market_result"],
            "side": r["side"],
            "count": r["count"],
            "entry_price_cents": r["entry_price_cents"],
            "revenue_cents": r["revenue_cents"],
            "fee_cents": r["fee_cents"],
            "pnl_cents": r["pnl_cents"],
            "settled_at": r["settled_at"],
            "strategy": _clean(r["strategy"]),
            "seconds_to_close": r["seconds_to_close"],
            "fill_latency_seconds": r["fill_latency_seconds"],
            "vol_regime": _clean(r["vol_regime"]),
            "calibrated_prob": r["calibrated_prob"],
            "edge": r["edge"],
            "kelly_f": r["kelly_f"],
        })

    count = _batched_upsert("trades", mapped)
    logging.info(f"  trades: {count}/{len(rows)} rows migrated")
    return count, len(rows)


def migrate_evaluations(conn):
    """evaluated_opportunities → evaluations"""
    logging.info("Migrating evaluated_opportunities → evaluations...")
    rows = conn.execute("""
        SELECT id, ticker, event_ticker, asset, filter_stage, rejection_reason,
               evaluation_time, spot_price, threshold, volatility, market_price,
               seconds_to_close, calibrated_prob, edge, ofa_adjustment,
               status, market_result, counterfactual_pnl,
               strategy, position_size, kelly_f, z_score, vol_regime,
               calibrated_prob_raw, settled_time, breakeven_wr, expected_value,
               drawdown_scaler, ask_depth, best_ask_source, ofa_confidence,
               raw_prob, calibration_method, old_system_prob, fee_adjusted_edge,
               egarch_sigma, egarch_blend_sigma, egarch_blend_weight,
               mz_r_squared, shadow_tv_blend_rv, mz_shadow_sigmoid_w,
               mz_baseline_qlike, mz_qlike, counterfactual,
               shadow_cal_prob, shadow_cal_fee_edge, shadow_cal_temperature
        FROM evaluated_opportunities
    """).fetchall()

    mapped = []
    for r in rows:
        row_dict = {col: _clean(r[col]) for col in r.keys()}
        # Rename evaluation_time for Supabase schema
        mapped.append(row_dict)

    count = _batched_upsert("evaluations", mapped)
    logging.info(f"  evaluations: {count}/{len(rows)} rows migrated")
    return count, len(rows)


def migrate_rejections(conn):
    """rejected_opportunities → rejections"""
    logging.info("Migrating rejected_opportunities → rejections...")
    rows = conn.execute("""
        SELECT ticker, event_ticker, asset, rejection_reason, rejection_time,
               z_score, spot_price, threshold, volatility, market_price,
               seconds_to_close, calibrated_prob, status, raw_prob,
               market_result, egarch_sigma, egarch_blend_sigma,
               egarch_blend_weight, mz_r_squared, shadow_tv_blend_rv,
               mz_shadow_sigmoid_w, mz_baseline_qlike, mz_qlike,
               counterfactual
        FROM rejected_opportunities
    """).fetchall()

    mapped = []
    for r in rows:
        row_dict = {col: _clean(r[col]) for col in r.keys()}
        mapped.append(row_dict)

    count = _batched_upsert("rejections", mapped)
    logging.info(f"  rejections: {count}/{len(rows)} rows migrated")
    return count, len(rows)


def migrate_vol_params(conn):
    """garch_params + egarch_params → volatility_params"""
    logging.info("Migrating GARCH/EGARCH params → volatility_params...")

    # Read GARCH params
    garch = {}
    try:
        for r in conn.execute("SELECT * FROM garch_params").fetchall():
            garch[r["asset"]] = dict(r)
    except Exception:
        logging.warning("  No garch_params table found")

    # Read EGARCH params
    egarch = {}
    try:
        for r in conn.execute("SELECT * FROM egarch_params").fetchall():
            egarch[r["asset"]] = dict(r)
    except Exception:
        logging.warning("  No egarch_params table found")

    # Merge into single rows
    mapped = []
    for asset in ["BTC", "ETH", "SOL", "XRP"]:
        g = garch.get(asset, {})
        e = egarch.get(asset, {})
        row = {
            "asset": asset,
            "garch_omega": g.get("omega"),
            "garch_alpha": g.get("alpha"),
            "garch_beta": g.get("beta"),
            "garch_last_variance": g.get("last_variance"),
            "egarch_omega": e.get("omega"),
            "egarch_alpha": e.get("alpha"),
            "egarch_gamma": e.get("gamma"),
            "egarch_beta": e.get("beta"),
            "egarch_last_log_variance": e.get("last_log_variance"),
            "egarch_mle_loglik": e.get("mle_loglik"),
            "egarch_mle_converged": bool(e.get("mle_converged", False)),
            "updated_at": e.get("updated_at") or g.get("updated_at") or "2026-01-01T00:00:00Z",
        }
        if g or e:
            mapped.append(row)

    count = _batched_upsert("volatility_params", mapped)
    logging.info(f"  volatility_params: {count}/{len(mapped)} rows migrated")
    return count, len(mapped)


def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        logging.error("Set SUPABASE_URL and SUPABASE_SERVICE_KEY environment variables")
        sys.exit(1)

    logging.info(f"Supabase URL: {SUPABASE_URL}")
    logging.info(f"Dry run: {DRY_RUN}")

    db_path = os.path.join(os.path.dirname(__file__) or ".", "state.db")
    if not os.path.exists(db_path):
        logging.error(f"state.db not found at {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    results = {}
    results["trades"] = migrate_trades(conn)
    results["evaluations"] = migrate_evaluations(conn)
    results["rejections"] = migrate_rejections(conn)
    results["volatility_params"] = migrate_vol_params(conn)

    conn.close()

    logging.info("\n=== Migration Summary ===")
    all_ok = True
    for table, (migrated, total) in results.items():
        status = "OK" if migrated == total else "PARTIAL"
        if migrated != total:
            all_ok = False
        logging.info(f"  {table}: {migrated}/{total} ({status})")

    if all_ok:
        logging.info("\nMigration complete. Verify with:")
        logging.info("  SELECT 'trades' AS tbl, COUNT(*) FROM trades")
        logging.info("  UNION ALL SELECT 'evaluations', COUNT(*) FROM evaluations")
        logging.info("  UNION ALL SELECT 'rejections', COUNT(*) FROM rejections")
        logging.info("  UNION ALL SELECT 'volatility_params', COUNT(*) FROM volatility_params;")
    else:
        logging.warning("\nSome tables had partial migration — re-run is safe (idempotent)")


if __name__ == "__main__":
    main()
