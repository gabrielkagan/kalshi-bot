#!/usr/bin/env python3
"""
Pre-compute audit summary metrics and store in state.db.

Runs as a systemd timer every 30 min on VPS. Each run computes summary
metrics for all 5 systems (15m, hourly, spx, weather, sports) and inserts
one row per system into the audit_snapshots table. Prunes rows older than
7 days.

Usage:
    python3 scripts/audit/audit_cron.py                   # default: ~/kalshi-bot-repo/state.db
    python3 scripts/audit/audit_cron.py --db /tmp/state.db # custom path
"""

import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Regime timestamps — keep in sync with SKILL.md / CLAUDE.md
# ---------------------------------------------------------------------------
REGIME_SINCE = {
    "15m": "2026-02-28T00:00:00",
    "15m_shadow": "2026-02-28T00:00:00",
    "hourly": "2026-02-28T18:30:00",
    "spx": "2026-03-02T00:00:00",
    "weather": "2026-03-02T16:54:00",
    "sports": "2026-03-01T00:00:00",
}


def wilson_ci(wins, total, z=1.96):
    """Wilson score 95% confidence interval for win rate."""
    if total == 0:
        return (0.0, 0.0)
    p = wins / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    adj = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return (round((centre - adj) / denom, 4), round((centre + adj) / denom, 4))


def _sql_ceil(expr):
    """SQL expression for ceil(expr) using SQLite integer truncation.

    ceil(x) = CAST(x AS INTEGER) + (x > CAST(x AS INTEGER))
    Works for positive values (fees are always >= 0).
    """
    return (f"(CAST({expr} AS INTEGER) + ({expr} > CAST({expr} AS INTEGER)))")


def _sim_pnl_sql(fee_mult, size_expr, price_expr, result_expr):
    """SQL CASE expression for simulated PnL with correct maker/taker fees.

    Fees are subtracted on BOTH wins and losses (fee is paid at order time).
    Uses ceil() to match bot's math.ceil() fee calculation.
    """
    fee_raw = f"({fee_mult} * {size_expr} * {price_expr} * (100 - {price_expr}) / 100.0)"
    fee = _sql_ceil(fee_raw)
    return f"""CASE
                WHEN {result_expr} IN ('yes', 'all_yes') THEN
                    (100 - {price_expr}) * {size_expr} - {fee}
                WHEN {result_expr} IN ('no', 'all_no') THEN
                    -({price_expr} * {size_expr} + {fee})
                ELSE 0 END"""


def _sim_pnl_sql_no_side(fee_mult, size_expr, price_expr, result_expr):
    """SQL CASE for NO-side simulated PnL — win when result is 'no'/'all_no'.

    Same math as YES-side but win/loss conditions are flipped.
    Price stored is the NO price (what you'd pay for a NO contract).
    """
    fee_raw = f"({fee_mult} * {size_expr} * {price_expr} * (100 - {price_expr}) / 100.0)"
    fee = _sql_ceil(fee_raw)
    return f"""CASE
                WHEN {result_expr} IN ('no', 'all_no') THEN
                    (100 - {price_expr}) * {size_expr} - {fee}
                WHEN {result_expr} IN ('yes', 'all_yes') THEN
                    -({price_expr} * {size_expr} + {fee})
                ELSE 0 END"""


# YES-only filter for existing queries (handles old NULL rows and new 'yes' rows)
YES_SIDE_FILTER = "AND (side IS NULL OR side = 'yes')"
NO_SIDE_FILTER = "AND side = 'no'"


def cal_engine_obs_count(conn, product_type, since):
    """Count settled evaluated_opportunities with raw_prob for CalEngine training."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM evaluated_opportunities "
            "WHERE product_type=? AND raw_prob IS NOT NULL "
            "AND status='settled' AND evaluation_time>=?",
            (product_type, since)
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def compute_no_side_metrics(conn, product_type_filter, since, fee_mult=0.0):  # Kalshi charges $0 on maker fills
    """Compute NO-side shadow metrics for any product type.

    Returns dict with NO-side signal count, settled, WR, sim PnL, per-asset breakdown.
    Returns None if no NO-side data exists.
    """
    c = conn.cursor()
    try:
        # Check if side column exists
        cols = [r[1] for r in c.execute("PRAGMA table_info(evaluated_opportunities)").fetchall()]
        if "side" not in cols:
            return None
    except Exception:
        return None

    # Count NO-side entries
    count_row = c.execute(f"""
        SELECT COUNT(*) FROM evaluated_opportunities
        WHERE {product_type_filter} AND side = 'no' AND evaluation_time >= ?
    """, (since,)).fetchone()
    total = count_row[0] if count_row else 0
    if total == 0:
        return None

    # Settled NO-side W/L and sim PnL
    _pnl = _sim_pnl_sql_no_side(fee_mult, "COALESCE(position_size, 1)", "market_price", "market_result")
    sig_row = c.execute(f"""
        SELECT
            SUM(CASE WHEN market_result IN ('no', 'all_no') THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN market_result IN ('yes', 'all_yes') THEN 1 ELSE 0 END) as losses,
            SUM({_pnl}) as sim_pnl,
            AVG(market_price) as avg_price
        FROM evaluated_opportunities
        WHERE {product_type_filter} AND side = 'no'
          AND evaluation_time >= ?
          AND market_result IS NOT NULL
    """, (since,)).fetchone()

    wins = sig_row[0] or 0
    losses = sig_row[1] or 0
    sim_pnl = sig_row[2] or 0
    avg_price = round(sig_row[3], 1) if sig_row[3] else 0
    settled = wins + losses
    wr = round(wins / settled, 4) if settled > 0 else 0

    # Per-asset
    asset_rows = c.execute(f"""
        SELECT asset,
            SUM(CASE WHEN market_result IN ('no', 'all_no') THEN 1 ELSE 0 END) as w,
            SUM(CASE WHEN market_result IN ('yes', 'all_yes') THEN 1 ELSE 0 END) as l,
            SUM({_pnl}) as pnl
        FROM evaluated_opportunities
        WHERE {product_type_filter} AND side = 'no'
          AND evaluation_time >= ?
          AND market_result IS NOT NULL
        GROUP BY asset ORDER BY pnl DESC
    """, (since,)).fetchall()
    by_asset = {r[0]: {"wins": r[1] or 0, "losses": r[2] or 0, "sim_pnl_cents": r[3] or 0} for r in asset_rows}

    return {
        "total": total,
        "settled": settled,
        "wins": wins,
        "losses": losses,
        "win_rate": wr,
        "sim_pnl_cents": sim_pnl,
        "avg_price": avg_price,
        "by_asset": by_asset,
    }


def ensure_table(conn):
    """Create audit_snapshots table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            audit_type TEXT NOT NULL,
            computed_at TEXT NOT NULL,
            regime_since TEXT,
            metrics_json TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_latest
        ON audit_snapshots(audit_type, computed_at DESC)
    """)
    conn.commit()


def prune_old(conn, days=7):
    """Delete snapshots older than N days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn.execute("DELETE FROM audit_snapshots WHERE computed_at < ?", (cutoff,))
    conn.commit()


# ---------------------------------------------------------------------------
# 15M Live
# ---------------------------------------------------------------------------
def compute_15m(conn, since):
    """Compute 15M live trading summary metrics."""
    c = conn.cursor()

    # Core performance — filter to product_type='15m' to exclude hourly/spx/etc
    # Side-aware win counting: win = (side matches market_result)
    row = c.execute(f"""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN COALESCE(side,'yes') = market_result THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN COALESCE(side,'yes') != market_result THEN 1 ELSE 0 END) as losses,
            SUM(pnl_cents - COALESCE(fee_cents, 0)) as total_pnl,
            SUM(COALESCE(fee_cents, 0)) as total_fees,
            AVG(entry_price_cents) as avg_entry,
            AVG(fill_latency_seconds) as avg_fill_latency,
            SUM(count) as total_contracts
        FROM settled_trades
        WHERE settled_at >= ? AND product_type = '15m'
    """, (since,)).fetchone()

    total, wins, losses = row[0], row[1] or 0, row[2] or 0
    total_pnl = row[3] or 0
    total_fees = row[4] or 0
    avg_entry = round(row[5], 1) if row[5] else 0
    avg_fill_latency = round(row[6], 2) if row[6] else 0
    wr = round(wins / total, 4) if total else 0
    ci_lo, ci_hi = wilson_ci(wins, total)

    # Today's PnL
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_row = c.execute("""
        SELECT SUM(pnl_cents - COALESCE(fee_cents, 0)) FROM settled_trades
        WHERE settled_at >= ? AND product_type = '15m' AND date(settled_at) = ?
    """, (since, today)).fetchone()
    daily_pnl = daily_row[0] or 0 if daily_row else 0

    # Per-asset breakdown (side-aware win counting)
    asset_rows = c.execute(f"""
        SELECT asset,
            COUNT(*) as n,
            SUM(CASE WHEN COALESCE(side,'yes') = market_result THEN 1 ELSE 0 END) as w,
            SUM(pnl_cents - COALESCE(fee_cents, 0)) as pnl
        FROM settled_trades
        WHERE settled_at >= ? AND product_type = '15m'
        GROUP BY asset ORDER BY pnl DESC
    """, (since,)).fetchall()
    by_asset = {r[0]: {"n": r[1], "wins": r[2], "pnl_cents": r[3] or 0} for r in asset_rows}

    # Execution mix
    exec_row = c.execute("""
        SELECT
            SUM(CASE WHEN escalation_type IS NULL OR escalation_type = 'none' THEN 1 ELSE 0 END) as maker,
            SUM(CASE WHEN escalation_type IS NOT NULL AND escalation_type != 'none' THEN 1 ELSE 0 END) as taker
        FROM settled_trades
        WHERE settled_at >= ? AND product_type = '15m'
    """, (since,)).fetchone()

    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": wr,
        "win_rate_ci": [ci_lo, ci_hi],
        "total_pnl_cents": total_pnl,
        "total_fees_cents": total_fees,
        "avg_entry_price": avg_entry,
        "avg_fill_latency": avg_fill_latency,
        "daily_pnl_today": daily_pnl,
        "by_asset": by_asset,
        "maker_fills": exec_row[0] or 0 if exec_row else 0,
        "taker_fills": exec_row[1] or 0 if exec_row else 0,
        "regime_start": since,
    }


# ---------------------------------------------------------------------------
# 15M Shadow (STC + XRP + fifteenm_shadow_signals)
# ---------------------------------------------------------------------------
def compute_15m_shadow(conn, since):
    """Compute 15M shadow metrics from evaluated_opportunities + fifteenm_shadow_signals."""
    c = conn.cursor()

    # STC and XRP shadow counts from evaluated_opportunities
    row = c.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN filter_stage = 'stc_shadow_no_xrp' THEN 1 ELSE 0 END) as stc_non_xrp,
            SUM(CASE WHEN filter_stage IN ('stc_shadow', 'stc_shadow_xrp') THEN 1 ELSE 0 END) as stc_xrp,
            SUM(CASE WHEN filter_stage = 'xrp_shadow' THEN 1 ELSE 0 END) as xrp_shadow,
            SUM(CASE WHEN market_result IS NOT NULL THEN 1 ELSE 0 END) as settled
        FROM evaluated_opportunities
        WHERE (product_type IS NULL OR product_type = '15m')
          AND filter_stage IN ('stc_shadow', 'stc_shadow_xrp', 'stc_shadow_no_xrp', 'xrp_shadow')
          AND evaluation_time >= ?
    """, (since,)).fetchone()

    total = row[0]
    stc_non_xrp = row[1] or 0
    stc_xrp = row[2] or 0
    xrp_shadow = row[3] or 0
    settled = row[4] or 0

    # STC shadow (non-XRP) W/L and sim PnL — YES-side only
    _pnl = _sim_pnl_sql(0.0, "COALESCE(position_size, 1)", "market_price", "market_result")  # Kalshi charges $0 on maker fills
    stc_row = c.execute(f"""
        SELECT
            SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN market_result = 'no' THEN 1 ELSE 0 END) as losses,
            SUM({_pnl}) as sim_pnl,
            AVG(market_price) as avg_price
        FROM evaluated_opportunities
        WHERE (product_type IS NULL OR product_type = '15m')
          AND filter_stage = 'stc_shadow_no_xrp'
          AND evaluation_time >= ?
          AND market_result IS NOT NULL
          {YES_SIDE_FILTER}
    """, (since,)).fetchone()

    stc_wins = stc_row[0] or 0
    stc_losses = stc_row[1] or 0
    stc_pnl = stc_row[2] or 0
    stc_wr = round(stc_wins / (stc_wins + stc_losses), 4) if (stc_wins + stc_losses) > 0 else 0

    # XRP shadow W/L and sim PnL — YES-side only
    xrp_row = c.execute(f"""
        SELECT
            SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN market_result = 'no' THEN 1 ELSE 0 END) as losses,
            SUM({_pnl}) as sim_pnl
        FROM evaluated_opportunities
        WHERE (product_type IS NULL OR product_type = '15m')
          AND filter_stage = 'xrp_shadow'
          AND evaluation_time >= ?
          AND market_result IS NOT NULL
          {YES_SIDE_FILTER}
    """, (since,)).fetchone()

    xrp_wins = xrp_row[0] or 0
    xrp_losses = xrp_row[1] or 0
    xrp_pnl = xrp_row[2] or 0

    # fifteenm_shadow_signals approach metrics (if table exists)
    a1_stats = {}
    a2_stats = {}
    a3_stats = {}
    try:
        # Check if table exists
        tbl = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='fifteenm_shadow_signals'"
        ).fetchone()
        if tbl:
            for asset in ("BTC", "ETH", "SOL", "XRP"):
                for prefix, stats in [("a1", a1_stats), ("a2", a2_stats)]:
                    # fifteenm_shadow_signals has no side column — YES/NO stored in separate column prefixes
                    # a1_/a2_ columns are inherently YES-side; market_result='yes' = win for YES-side
                    r = c.execute(f"""
                        SELECT
                            COUNT(*) as total,
                            SUM(CASE WHEN {prefix}_gates_passed = 1 AND market_result IN ('yes', 'all_yes') THEN 1 ELSE 0 END) as wins,
                            SUM(CASE WHEN {prefix}_gates_passed = 1 AND market_result IN ('no', 'all_no') THEN 1 ELSE 0 END) as losses,
                            SUM(CASE WHEN {prefix}_gates_passed = 1 THEN {prefix}_pnl_cents ELSE 0 END) as pnl
                        FROM fifteenm_shadow_signals
                        WHERE asset = ? AND status = 'settled'
                    """, (asset,)).fetchone()
                    if r and r[0] > 0:
                        w, l = r[1] or 0, r[2] or 0
                        stats[asset] = {
                            "total": r[0],
                            "wins": w,
                            "losses": l,
                            "wr": round(w / (w + l), 4) if (w + l) > 0 else 0,
                            "pnl_cents": r[3] or 0,
                        }
            # A3 gating metrics
            shadow_cols = [r[1] for r in c.execute(
                "PRAGMA table_info(fifteenm_shadow_signals)").fetchall()]
            if "a3_gate_prob" in shadow_cols:
                for asset in ("BTC", "ETH", "SOL", "XRP"):
                    r = c.execute("""
                        SELECT
                            COUNT(*) as total,
                            SUM(a3_pnl_gate10_cents) as pnl_g10,
                            SUM(a3_pnl_gate20_cents) as pnl_g20,
                            SUM(a3_pnl_gate30_cents) as pnl_g30,
                            SUM(live_pnl_cents) as baseline_pnl,
                            AVG(a3_gate_prob) as avg_gate_prob
                        FROM fifteenm_shadow_signals
                        WHERE asset = ? AND status = 'settled'
                            AND a3_gate_prob IS NOT NULL
                    """, (asset,)).fetchone()
                    if r and r[0] > 0:
                        a3_stats[asset] = {
                            "total": r[0],
                            "baseline_pnl": r[4] or 0,
                            "pnl_gate10": r[1] or 0,
                            "pnl_gate20": r[2] or 0,
                            "pnl_gate30": r[3] or 0,
                            "avg_gate_prob": round(r[5] or 0, 4),
                        }
    except Exception:
        pass

    # NO-side shadow metrics
    no_side = compute_no_side_metrics(
        conn, "(product_type IS NULL OR product_type = '15m')", since)

    result = {
        "total_shadow_evals": total,
        "stc_non_xrp": stc_non_xrp,
        "stc_xrp": stc_xrp,
        "xrp_shadow": xrp_shadow,
        "settled": settled,
        "stc_wins": stc_wins,
        "stc_losses": stc_losses,
        "stc_wr": stc_wr,
        "stc_pnl_cents": stc_pnl,
        "xrp_wins": xrp_wins,
        "xrp_losses": xrp_losses,
        "xrp_pnl_cents": xrp_pnl,
        "approach1_by_asset": a1_stats,
        "approach2_by_asset": a2_stats,
        "approach3_gating_by_asset": a3_stats,
    }
    if no_side:
        result["no_side"] = no_side
    return result


# ---------------------------------------------------------------------------
# Hourly Shadow
# ---------------------------------------------------------------------------
def compute_hourly(conn, since):
    """Compute hourly observation summary metrics."""
    c = conn.cursor()

    # Total evals and signal count
    row = c.execute("""
        SELECT
            COUNT(*) as total_evals,
            SUM(CASE WHEN filter_stage = 'hourly_observation' THEN 1 ELSE 0 END) as signals,
            SUM(CASE WHEN market_result IS NOT NULL THEN 1 ELSE 0 END) as settled,
            SUM(CASE WHEN market_result IS NOT NULL AND filter_stage = 'hourly_observation' THEN 1 ELSE 0 END) as signals_settled
        FROM evaluated_opportunities
        WHERE product_type = 'hourly' AND evaluation_time >= ?
    """, (since,)).fetchone()

    total_evals = row[0]
    signals = row[1] or 0
    settled = row[2] or 0
    signals_settled = row[3] or 0

    # Signal W/L and simulated PnL — YES-side only (NO-side handled by compute_no_side_metrics)
    _pnl = _sim_pnl_sql(0.0, "COALESCE(position_size, 1)", "market_price", "market_result")  # Kalshi charges $0 on maker fills
    sig_row = c.execute(f"""
        SELECT
            SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN market_result = 'no' THEN 1 ELSE 0 END) as losses,
            SUM({_pnl}) as sim_pnl,
            AVG(market_price) as avg_price
        FROM evaluated_opportunities
        WHERE product_type = 'hourly'
          AND filter_stage = 'hourly_observation'
          AND evaluation_time >= ?
          AND market_result IS NOT NULL
          {YES_SIDE_FILTER}
    """, (since,)).fetchone()

    wins = sig_row[0] or 0
    losses = sig_row[1] or 0
    sim_pnl = sig_row[2] or 0
    avg_price = round(sig_row[3], 1) if sig_row[3] else 0
    wr = round(wins / (wins + losses), 4) if (wins + losses) > 0 else 0
    ci_lo, ci_hi = wilson_ci(wins, wins + losses)

    # Brier score on signals — YES-side only (calibrated_prob predicts YES outcome)
    brier_row = c.execute(f"""
        SELECT AVG((calibrated_prob - CASE WHEN market_result = 'yes' THEN 1.0 ELSE 0.0 END)
                    * (calibrated_prob - CASE WHEN market_result = 'yes' THEN 1.0 ELSE 0.0 END))
        FROM evaluated_opportunities
        WHERE product_type = 'hourly'
          AND filter_stage = 'hourly_observation'
          AND evaluation_time >= ?
          AND market_result IS NOT NULL
          AND calibrated_prob IS NOT NULL
          {YES_SIDE_FILTER}
    """, (since,)).fetchone()
    brier = round(brier_row[0], 4) if brier_row and brier_row[0] is not None else None

    # Overconfidence: avg predicted - avg actual — YES-side only
    oc_row = c.execute(f"""
        SELECT
            AVG(calibrated_prob) as avg_pred,
            AVG(CASE WHEN market_result = 'yes' THEN 1.0 ELSE 0.0 END) as avg_actual
        FROM evaluated_opportunities
        WHERE product_type = 'hourly'
          AND filter_stage = 'hourly_observation'
          AND evaluation_time >= ?
          AND market_result IS NOT NULL
          AND calibrated_prob IS NOT NULL
          {YES_SIDE_FILTER}
    """, (since,)).fetchone()
    overconfidence_pp = None
    if oc_row and oc_row[0] is not None and oc_row[1] is not None:
        overconfidence_pp = round((oc_row[0] - oc_row[1]) * 100, 2)

    # Temperature instrumentation coverage (signals only — pre-temp stages always have NULL)
    temp_row = c.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN hourly_pre_temp_prob IS NOT NULL THEN 1 ELSE 0 END) as has_temp,
            SUM(CASE WHEN hourly_applied_temp_t IS NOT NULL THEN 1 ELSE 0 END) as has_temp_t
        FROM evaluated_opportunities
        WHERE product_type = 'hourly'
          AND filter_stage = 'hourly_observation'
          AND evaluation_time >= ?
    """, (since,)).fetchone()
    temp_coverage = round(temp_row[1] / temp_row[0] * 100, 1) if temp_row[0] else 0

    # Worst asset by simulated PnL — YES-side only
    worst_row = c.execute(f"""
        SELECT asset, SUM({_pnl}) as pnl
        FROM evaluated_opportunities
        WHERE product_type = 'hourly'
          AND filter_stage = 'hourly_observation'
          AND evaluation_time >= ?
          AND market_result IS NOT NULL
          {YES_SIDE_FILTER}
        GROUP BY asset
        ORDER BY pnl ASC
        LIMIT 1
    """, (since,)).fetchone()
    worst_asset = worst_row[0] if worst_row else None
    worst_asset_pnl = worst_row[1] or 0 if worst_row else 0

    # Per-asset summary — YES-side only
    asset_rows = c.execute(f"""
        SELECT asset,
            SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as w,
            SUM(CASE WHEN market_result = 'no' THEN 1 ELSE 0 END) as l,
            SUM({_pnl}) as pnl
        FROM evaluated_opportunities
        WHERE product_type = 'hourly'
          AND filter_stage = 'hourly_observation'
          AND evaluation_time >= ?
          AND market_result IS NOT NULL
          {YES_SIDE_FILTER}
        GROUP BY asset ORDER BY pnl DESC
    """, (since,)).fetchall()
    by_asset = {r[0]: {"wins": r[1], "losses": r[2], "sim_pnl_cents": r[3] or 0} for r in asset_rows}

    result = {
        "total_evals": total_evals,
        "signals": signals,
        "settled": signals_settled,
        "wins": wins,
        "losses": losses,
        "pending": signals - signals_settled,
        "win_rate": wr,
        "win_rate_ci": [ci_lo, ci_hi],
        "sim_pnl_cents": sim_pnl,
        "avg_entry_price": avg_price,
        "brier": brier,
        "overconfidence_pp": overconfidence_pp,
        "temp_coverage_pct": temp_coverage,
        "worst_asset": worst_asset,
        "worst_asset_pnl": worst_asset_pnl,
        "by_asset": by_asset,
        "cal_engine_obs": cal_engine_obs_count(conn, "hourly", since),
    }
    # NO-side metrics
    no_side = compute_no_side_metrics(conn, "product_type = 'hourly'", since)
    if no_side:
        result["no_side"] = no_side
    return result


# ---------------------------------------------------------------------------
# SPX Shadow
# ---------------------------------------------------------------------------
def compute_spx(conn, since):
    """Compute SPX hourly observation summary metrics."""
    c = conn.cursor()

    row = c.execute("""
        SELECT
            COUNT(*) as total_evals,
            SUM(CASE WHEN filter_stage = 'spx_observation' THEN 1 ELSE 0 END) as signals,
            SUM(CASE WHEN market_result IS NOT NULL THEN 1 ELSE 0 END) as settled,
            SUM(CASE WHEN market_result IS NOT NULL AND filter_stage = 'spx_observation' THEN 1 ELSE 0 END) as signals_settled
        FROM evaluated_opportunities
        WHERE product_type = 'spx_hourly' AND evaluation_time >= ?
    """, (since,)).fetchone()

    total_evals = row[0]
    signals = row[1] or 0
    signals_settled = row[3] or 0

    # Signal W/L and sim PnL — YES-side only (NO-side handled by compute_no_side_metrics)
    _spx_pnl = _sim_pnl_sql(0.0, "COALESCE(position_size, 1)", "market_price", "market_result")  # Kalshi charges $0 on maker fills
    sig_row = c.execute(f"""
        SELECT
            SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN market_result = 'no' THEN 1 ELSE 0 END) as losses,
            SUM({_spx_pnl}) as sim_pnl,
            AVG(market_price) as avg_price
        FROM evaluated_opportunities
        WHERE product_type = 'spx_hourly'
          AND filter_stage = 'spx_observation'
          AND evaluation_time >= ?
          AND market_result IS NOT NULL
          {YES_SIDE_FILTER}
    """, (since,)).fetchone()

    wins = sig_row[0] or 0
    losses = sig_row[1] or 0
    sim_pnl = sig_row[2] or 0
    avg_price = round(sig_row[3], 1) if sig_row[3] else 0
    wr = round(wins / (wins + losses), 4) if (wins + losses) > 0 else 0
    ci_lo, ci_hi = wilson_ci(wins, wins + losses)

    # Brier score — YES-side only
    brier_row = c.execute(f"""
        SELECT AVG((calibrated_prob - CASE WHEN market_result = 'yes' THEN 1.0 ELSE 0.0 END)
                    * (calibrated_prob - CASE WHEN market_result = 'yes' THEN 1.0 ELSE 0.0 END))
        FROM evaluated_opportunities
        WHERE product_type = 'spx_hourly'
          AND filter_stage = 'spx_observation'
          AND evaluation_time >= ?
          AND market_result IS NOT NULL
          AND calibrated_prob IS NOT NULL
          {YES_SIDE_FILTER}
    """, (since,)).fetchone()
    brier = round(brier_row[0], 4) if brier_row and brier_row[0] is not None else None

    # Trading days covered
    days_row = c.execute("""
        SELECT COUNT(DISTINCT date(evaluation_time))
        FROM evaluated_opportunities
        WHERE product_type = 'spx_hourly' AND evaluation_time >= ?
    """, (since,)).fetchone()
    trading_days = days_row[0] if days_row else 0

    # EGARCH blend weight distribution
    blend_row = c.execute("""
        SELECT
            AVG(egarch_blend_weight) as avg_w,
            MIN(egarch_blend_weight) as min_w,
            MAX(egarch_blend_weight) as max_w,
            SUM(CASE WHEN egarch_blend_weight IS NOT NULL THEN 1 ELSE 0 END) as populated
        FROM evaluated_opportunities
        WHERE product_type = 'spx_hourly' AND evaluation_time >= ?
    """, (since,)).fetchone()
    blend_adapting = False
    if blend_row and blend_row[0] is not None and blend_row[2] is not None:
        blend_adapting = abs(blend_row[2] - blend_row[1]) > 0.01

    # Data quality checks
    checks = []
    checks_pass = 0
    for col in ["egarch_blend_weight", "egarch_blend_sigma", "spot_price", "threshold"]:
        dq_row = c.execute(f"""
            SELECT SUM(CASE WHEN {col} IS NOT NULL THEN 1 ELSE 0 END), COUNT(*)
            FROM evaluated_opportunities
            WHERE product_type = 'spx_hourly' AND evaluation_time >= ?
        """, (since,)).fetchone()
        pct = round(dq_row[0] / dq_row[1] * 100, 1) if dq_row[1] else 0
        ok = pct > 50
        if ok:
            checks_pass += 1
        checks.append({"col": col, "pct": pct, "ok": ok})

    result = {
        "total_evals": total_evals,
        "signals": signals,
        "settled": signals_settled,
        "wins": wins,
        "losses": losses,
        "pending": signals - signals_settled,
        "win_rate": wr,
        "win_rate_ci": [ci_lo, ci_hi],
        "sim_pnl_cents": sim_pnl,
        "avg_entry_price": avg_price,
        "brier": brier,
        "trading_days": trading_days,
        "blend_adapting": blend_adapting,
        "data_checks": checks,
        "data_checks_pass": checks_pass,
        "data_checks_total": len(checks),
        "cal_engine_obs": cal_engine_obs_count(conn, "spx_hourly", since),
    }
    # NO-side metrics
    no_side = compute_no_side_metrics(conn, "product_type = 'spx_hourly'", since)
    if no_side:
        result["no_side"] = no_side
    return result


# ---------------------------------------------------------------------------
# Weather Shadow
# ---------------------------------------------------------------------------
def compute_weather(conn, since):
    """Compute weather observation summary metrics."""
    c = conn.cursor()

    row = c.execute("""
        SELECT
            COUNT(*) as total_evals,
            SUM(CASE WHEN filter_stage = 'weather_observation' THEN 1 ELSE 0 END) as signals,
            SUM(CASE WHEN market_result IS NOT NULL THEN 1 ELSE 0 END) as settled,
            SUM(CASE WHEN market_result IS NOT NULL AND filter_stage = 'weather_observation' THEN 1 ELSE 0 END) as signals_settled
        FROM evaluated_opportunities
        WHERE product_type = 'weather' AND evaluation_time >= ?
    """, (since,)).fetchone()

    total_evals = row[0]
    signals = row[1] or 0
    settled_total = row[2] or 0
    signals_settled = row[3] or 0

    # Signal W/L and simulated PnL — YES-side only (NO-side handled by compute_no_side_metrics)
    _pnl = _sim_pnl_sql(0.0, "COALESCE(position_size, 1)", "market_price", "market_result")  # Kalshi charges $0 on maker fills
    sig_row = c.execute(f"""
        SELECT
            SUM(CASE WHEN market_result IN ('yes', 'all_yes') THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN market_result IN ('no', 'all_no') THEN 1 ELSE 0 END) as losses,
            SUM({_pnl}) as sim_pnl,
            AVG(market_price) as avg_price
        FROM evaluated_opportunities
        WHERE product_type = 'weather'
          AND filter_stage = 'weather_observation'
          AND evaluation_time >= ?
          AND market_result IS NOT NULL
          {YES_SIDE_FILTER}
    """, (since,)).fetchone()

    wins = sig_row[0] or 0
    losses = sig_row[1] or 0
    sim_pnl = sig_row[2] or 0
    avg_price = round(sig_row[3], 1) if sig_row[3] else 0
    wr = round(wins / (wins + losses), 4) if (wins + losses) > 0 else 0
    ci_lo, ci_hi = wilson_ci(wins, wins + losses)

    # Ensemble coverage
    ens_row = c.execute("""
        SELECT
            SUM(CASE WHEN wx_ensemble_mean IS NOT NULL THEN 1 ELSE 0 END) as has_ens,
            COUNT(*) as total
        FROM evaluated_opportunities
        WHERE product_type = 'weather' AND evaluation_time >= ?
    """, (since,)).fetchone()
    ensemble_coverage = round(ens_row[0] / ens_row[1] * 100, 1) if ens_row[1] else 0

    # No-side edge: check if fee_adjusted_edge is populated
    noside_row = c.execute("""
        SELECT
            SUM(CASE WHEN fee_adjusted_edge IS NOT NULL THEN 1 ELSE 0 END),
            COUNT(*)
        FROM evaluated_opportunities
        WHERE product_type = 'weather' AND evaluation_time >= ?
    """, (since,)).fetchone()
    noside_edge_pct = round(noside_row[0] / noside_row[1] * 100, 1) if noside_row[1] else 0

    # By wx_market_type (city) — YES-side only
    city_rows = c.execute(f"""
        SELECT wx_market_type,
            COUNT(*) as n,
            SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as w,
            SUM(CASE WHEN market_result = 'no' THEN 1 ELSE 0 END) as l
        FROM evaluated_opportunities
        WHERE product_type = 'weather'
          AND evaluation_time >= ?
          AND market_result IS NOT NULL
          {YES_SIDE_FILTER}
        GROUP BY wx_market_type
    """, (since,)).fetchall()
    by_city = {r[0] or "unknown": {"n": r[1], "wins": r[2] or 0, "losses": r[3] or 0} for r in city_rows}

    result = {
        "total_evals": total_evals,
        "signals": signals,
        "settled": signals_settled,
        "wins": wins,
        "losses": losses,
        "pending": signals - signals_settled,
        "win_rate": wr,
        "win_rate_ci": [ci_lo, ci_hi],
        "sim_pnl_cents": sim_pnl,
        "avg_entry_price": avg_price,
        "ensemble_coverage_pct": ensemble_coverage,
        "no_side_edge_populated": noside_edge_pct,
        "by_city": by_city,
        "cal_engine_obs": cal_engine_obs_count(conn, "weather", since),
    }
    # NO-side metrics
    no_side = compute_no_side_metrics(conn, "product_type = 'weather'", since)
    if no_side:
        result["no_side"] = no_side
    return result


# ---------------------------------------------------------------------------
# Sports Shadow
# ---------------------------------------------------------------------------
def compute_sports(conn, since):
    """Compute sports comeback detection summary metrics."""
    c = conn.cursor()

    # Check if sports_shadow_log table exists
    tbl = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sports_shadow_log'"
    ).fetchone()
    if not tbl:
        return {"error": "sports_shadow_log table not found"}

    row = c.execute("""
        SELECT
            COUNT(*) as total,
            SUM(signal_fired) as signals,
            SUM(CASE WHEN signal_fired = 1 AND fav_won IS NOT NULL THEN 1 ELSE 0 END) as signals_settled,
            COUNT(DISTINCT game_id) as games,
            COUNT(DISTINCT league) as leagues_n
        FROM sports_shadow_log
        WHERE evaluation_time >= ?
    """, (since,)).fetchone()

    total = row[0]
    signals = row[1] or 0
    settled_signals = row[2] or 0
    games = row[3] or 0

    # Distinct leagues
    league_rows = c.execute("""
        SELECT DISTINCT league FROM sports_shadow_log WHERE evaluation_time >= ?
    """, (since,)).fetchall()
    leagues = [r[0] for r in league_rows if r[0]]

    # Signal W/L and simulated PnL.
    # NOTE: sports_shadow_log has NO fee_cents column — this table tracks
    # simulated/observation PnL pre-fees by design. Bare SUM(pnl_cents) is
    # CORRECT here, not a bug; do not mechanically rewrite to subtract fees.
    sig_row = c.execute("""
        SELECT
            SUM(CASE WHEN fav_won = 1 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN fav_won = 0 THEN 1 ELSE 0 END) as losses,
            SUM(pnl_cents) as pnl  -- noqa: sports_shadow_log has no fee_cents
        FROM sports_shadow_log
        WHERE signal_fired = 1
          AND evaluation_time >= ?
          AND fav_won IS NOT NULL
    """, (since,)).fetchone()

    wins = sig_row[0] or 0
    losses = sig_row[1] or 0
    pnl = sig_row[2] or 0
    wr = round(wins / (wins + losses), 4) if (wins + losses) > 0 else 0
    ci_lo, ci_hi = wilson_ci(wins, wins + losses)

    # Avg entry price for signals
    price_row = c.execute("""
        SELECT AVG(yes_ask) FROM sports_shadow_log
        WHERE signal_fired = 1 AND evaluation_time >= ?
    """, (since,)).fetchone()
    avg_price = round(price_row[0], 1) if price_row and price_row[0] else 0

    # SPRT (Sequential Probability Ratio Test)
    # H0: p <= 0.50 (unprofitable), H1: p >= 0.55
    p0, p1 = 0.50, 0.55
    llr = 0.0
    sprt_n = 0
    # Deduplicate by game_id — multiple signals per game are correlated
    signal_rows = c.execute("""
        SELECT fav_won FROM sports_shadow_log
        WHERE signal_fired = 1 AND fav_won IS NOT NULL AND evaluation_time >= ?
        GROUP BY game_id
        ORDER BY MIN(evaluation_time)
    """, (since,)).fetchall()
    for (outcome,) in signal_rows:
        if outcome == 1:
            llr += math.log(p1 / p0) if p0 > 0 and p1 > 0 else 0
        else:
            llr += math.log((1 - p1) / (1 - p0)) if p0 < 1 and p1 < 1 else 0
        sprt_n += 1

    # Wald boundaries: alpha=0.05, beta=0.10
    A = math.log(0.90 / 0.05)   # ~2.89 (reject H0 → profitable)
    B = math.log(0.10 / 0.95)   # ~-2.25 (accept H0 → not profitable)
    if llr >= A:
        sprt_decision = "REJECT_H0_PROFITABLE"
    elif llr <= B:
        sprt_decision = "ACCEPT_H0_NOT_PROFITABLE"
    else:
        sprt_decision = "CONTINUE"

    # Pregame capture method
    cap_row = c.execute("""
        SELECT pregame_capture_method, COUNT(*)
        FROM sports_shadow_log
        WHERE evaluation_time >= ? AND pregame_capture_method IS NOT NULL
        GROUP BY pregame_capture_method
    """, (since,)).fetchall()
    pregame_methods = {r[0]: r[1] for r in cap_row}
    total_with_method = sum(pregame_methods.values())
    pregame_capture_pct = round(total_with_method / total * 100, 1) if total else 0

    # Per sport group breakdown (if column exists)
    sport_groups = {}
    try:
        sg_rows = c.execute("""
            SELECT COALESCE(sport_group, 'unknown') AS sg,
                   COUNT(*) AS evals,
                   SUM(signal_fired) AS signals,
                   SUM(CASE WHEN signal_fired=1 AND fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled,
                   SUM(CASE WHEN signal_fired=1 AND fav_won=1 THEN 1 ELSE 0 END) AS wins,
                   COUNT(DISTINCT game_id) AS games,
                   AVG(CASE WHEN signal_fired=1 THEN comeback_prob END) AS avg_predicted,
                   AVG(sport_lr_scale) AS lr_scale
            FROM sports_shadow_log
            WHERE evaluation_time >= ?
            GROUP BY sg
            ORDER BY signals DESC
        """, (since,)).fetchall()
        for r in sg_rows:
            sg_settled = r[3] or 0
            sg_wins = r[4] or 0
            sg_wr = round(sg_wins / sg_settled, 4) if sg_settled > 0 else 0
            avg_pred = round(r[6], 4) if r[6] else 0
            cal_gap = round((avg_pred - sg_wr) * 100, 1) if sg_settled > 0 and avg_pred else None
            sport_groups[r[0]] = {
                "evals": r[1], "signals": r[2] or 0,
                "settled": sg_settled, "wins": sg_wins,
                "win_rate": sg_wr, "games": r[5] or 0,
                "avg_predicted": avg_pred,
                "lr_scale": round(r[7], 2) if r[7] else None,
                "calibration_gap_pp": cal_gap,
            }
    except Exception:
        pass  # sport_group column may not exist yet

    return {
        "total_evals": total,
        "signals": signals,
        "settled": settled_signals,
        "wins": wins,
        "losses": losses,
        "pending": signals - settled_signals,
        "win_rate": wr,
        "win_rate_ci": [ci_lo, ci_hi],
        "sim_pnl_cents": pnl,
        "avg_entry_price": avg_price,
        "games_covered": games,
        "leagues": leagues,
        "sprt_llr": round(llr, 3),
        "sprt_decision": sprt_decision,
        "sprt_n": sprt_n,
        "pregame_capture_pct": pregame_capture_pct,
        "sport_groups": sport_groups,
        "cal_engine_obs": cal_engine_obs_count(conn, "sports", since),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Pre-compute audit summary metrics")
    parser.add_argument(
        "--db",
        default=os.path.expanduser("~/kalshi-bot-repo/state.db"),
        help="Path to state.db (default: ~/kalshi-bot-repo/state.db)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: Database not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")

    ensure_table(conn)

    now = datetime.now(timezone.utc).isoformat()
    computed = []

    systems = {
        "15m": compute_15m,
        "15m_shadow": compute_15m_shadow,
        "hourly": compute_hourly,
        "spx": compute_spx,
        "weather": compute_weather,
        "sports": compute_sports,
    }

    for system, func in systems.items():
        since = REGIME_SINCE[system]
        try:
            metrics = func(conn, since)
            conn.execute(
                "INSERT INTO audit_snapshots (audit_type, computed_at, regime_since, metrics_json) VALUES (?, ?, ?, ?)",
                (system, now, since, json.dumps(metrics)),
            )
            computed.append(system)
            print(f"  [{system}] OK — {json.dumps(metrics, indent=None)[:120]}...")
        except Exception as e:
            print(f"  [{system}] ERROR: {e}", file=sys.stderr)

    conn.commit()

    prune_old(conn)
    conn.close()

    print(f"\nDone. Computed: {', '.join(computed)} at {now}")


if __name__ == "__main__":
    main()
