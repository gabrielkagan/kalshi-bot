"""Build dashboard state snapshots for Supabase sync."""

import math
import os
import time
import logging
import datetime
import collections
from typing import Dict, Any, List, Optional

ASSETS = ["BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE"]

# Configs C–G filter metadata for dashboard rendering
_HOURLY_VARIANT_DEFS = [
    ("hourly_config_c", {"included_assets": ["BTC", "ETH"], "min_stc": 600, "max_stc": 1800}),
    ("hourly_config_d", {"excluded_assets": ["XRP"], "max_edge": 0.05}),
    ("hourly_config_e", {"included_assets": ["BTC", "ETH"], "min_stc": 1200, "max_stc": 1800}),
    ("hourly_config_f", {"max_edge": 0.012}),
    ("hourly_config_g", {"included_assets": ["BTC"], "min_stc": 900, "max_stc": 1800}),
    # Killed configs h, j, k — 55% WR, deeply negative PnL
    ("hourly_config_i", {"included_assets": ["BTC", "ETH"], "min_stc": 600, "max_stc": 1800, "temperature": 2.0, "blend_w": 0.0}),
    ("hourly_config_l", {"excluded_assets": ["XRP"], "temperature": 2.0, "blend_w": 0.0}),
    ("hourly_config_m", {"included_assets": ["BTC", "ETH"], "min_stc": 600, "max_stc": 1800, "temperature": 2.5, "blend_w": 0.0}),
]
_HOURLY_VARIANT_GRADUATION = {
    "days_required": 7, "min_wr": 0.72, "min_wilson_lower": 0.65,
    "max_brier": 0.30, "min_day_wr": 0.50, "pnl_positive": True,
}


def _build_hourly_variant_snap(conn, filter_stage, filters_dict, graduation_dict):
    """Build snapshot dict for a single hourly shadow variant."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) as n, "
            "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
            "SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes') "
            "  THEN 1 ELSE 0 END) as wins, "
            "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl, "
            "AVG(CASE WHEN status='settled' AND calibrated_prob IS NOT NULL "
            "  THEN (calibrated_prob - CASE WHEN market_result IN ('yes','all_yes') "
            "    THEN 1.0 ELSE 0.0 END) * (calibrated_prob - CASE WHEN market_result IN ('yes','all_yes') "
            "    THEN 1.0 ELSE 0.0 END) END) as brier "
            "FROM evaluated_opportunities "
            "WHERE product_type='hourly' AND filter_stage=?",
            (filter_stage,)
        ).fetchone()
        by_day = conn.execute(
            "SELECT date(evaluation_time) as day, COUNT(*) as n, "
            "SUM(CASE WHEN market_result IN ('yes','all_yes') THEN 1 ELSE 0 END) as wins "
            "FROM evaluated_opportunities "
            "WHERE product_type='hourly' AND filter_stage=? AND status='settled' "
            "GROUP BY day ORDER BY day",
            (filter_stage,)
        ).fetchall()
        min_day_wr = None
        days = 0
        for d in by_day:
            days += 1
            dwr = (d["wins"] or 0) / d["n"] if d["n"] else 0
            if min_day_wr is None or dwr < min_day_wr:
                min_day_wr = dwr
        settled = (row["settled"] or 0) if row else 0
        wins = (row["wins"] or 0) if row else 0
        wr = round(wins / settled, 4) if settled else 0
        wlo = 0
        if settled > 0:
            p = wins / settled
            z = 1.96
            denom = 1 + z**2 / settled
            wlo = round((p + z**2 / (2 * settled) - z * ((p * (1 - p) / settled + z**2 / (4 * settled**2)) ** 0.5)) / denom, 4)
        return {
            "total_signals": row["n"] if row else 0,
            "settled": settled, "wins": wins, "wr": wr,
            "wilson_lower": wlo,
            "brier": round(row["brier"], 4) if row and row["brier"] else None,
            "sim_pnl_cents": row["sim_pnl"] if row else 0,
            "days": days,
            "min_day_wr": round(min_day_wr, 4) if min_day_wr is not None else None,
            "filters": filters_dict, "graduation": graduation_dict,
        }
    except Exception:
        logging.warning("%s snapshot failed", filter_stage, exc_info=True)
        return {
            "total_signals": 0, "settled": 0, "wins": 0, "wr": 0,
            "wilson_lower": 0, "brier": None, "sim_pnl_cents": 0,
            "days": 0, "min_day_wr": None,
            "filters": filters_dict, "graduation": graduation_dict,
        }

# Current config regime boundary — performance metrics filtered to this era
# TODO: auto-detect from git log like audit scripts
CONFIG_REGIME_SINCE = "2026-04-02T00:00:00"

# Sim fee rate for observation products: maker = $0, taker ~30% @ 0.07 → blended ~0.021
# But since most shadow trades would enter as maker (fee=$0), use taker-only rate for conservative sim
SIM_FEE_RATE = 0.021

# ── Cohort attribution panel (P1.2, Money Printer Roadmap Phase 1) ───────────

# Cap on the per-list payload — keeps the Supabase row bounded and matches
# the design § Dashboard mock pagination intent.
COHORT_PANEL_LIMIT = 20

# Schema version for the cohort_attribution snapshot key — bump on breaking
# shape changes; gh-pages renderer warns on mismatch.
COHORT_PANEL_SCHEMA_VERSION = 1

# Stale threshold — if the latest cohort_date is older than this many hours
# the nightly 13:07 UTC cron missed at least one fire; surface `stale=True`.
# 36h gives ~12h of NTP/cron jitter slack on the 24h nightly cadence.
COHORT_PANEL_STALE_HOURS = 36


def _empty_cohort_panel(now_iso):
    """Single source of truth for the cohort_attribution panel shape on the
    empty / stale / error path. Returned by the inner helper when the table
    is missing or empty AND used by the outer wire-in's `except` branch so
    the two stay locked in shape (R2 MN1 — guards against silent shape drift
    between the helper's no-data path and the wire-in's fallback)."""
    return {
        "as_of": now_iso,
        "schema_version": COHORT_PANEL_SCHEMA_VERSION,
        "summary": {
            "latest_cohort_date": None,
            "stale": True,
            "n_cohorts_total": 0,
            "n_cohorts_eligible_n50": 0,
            "n_cohorts_firing_bleed": 0,
            "n_cohorts_firing_cal": 0,
        },
        "top_bleeders_30d": [],
        "top_cal_drift_30d": [],
    }


def _price_band_label(band_5c):
    """Display label for a 5¢ price band — 17 → '85-89'."""
    lo = band_5c * 5
    return f"{lo}-{lo + 4}"


def _stc_band_label(band_60s):
    """Display label for a 60s STC band — 4 → '240-299', 11 → '660+'."""
    if band_60s >= 11:
        return "660+"
    lo = band_60s * 60
    return f"{lo}-{lo + 59}"


def _build_cohort_attribution_snap(db_conn):
    """Build the `cohort_attribution` top-level snap key.

    Reads `cohort_attribution_daily` (P1.1 schema) for the LATEST
    cohort_date and produces a JSON-serializable dict matching design
    § Dashboard mock:

        {
          "as_of": "...",
          "schema_version": 1,
          "summary": {"latest_cohort_date", "stale",
                      "n_cohorts_total", "n_cohorts_eligible_n50",
                      "n_cohorts_firing_bleed", "n_cohorts_firing_cal"},
          "top_bleeders_30d": [...],  # cf_pnl_30d ASC, capped at 20
          "top_cal_drift_30d": [...], # |cal_gap_30d| DESC, capped at 20
        }

    Defensive: returns the empty-but-shape-complete payload with
    `stale=True` when (a) the table is empty, (b) `evaluated_opportunities`
    hasn't been aggregated yet, or (c) the latest cohort_date is older than
    `COHORT_PANEL_STALE_HOURS`.
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    panel = _empty_cohort_panel(now_utc.isoformat())

    try:
        latest_row = db_conn.execute(
            "SELECT MAX(cohort_date) AS d FROM cohort_attribution_daily"
        ).fetchone()
    except Exception:
        logging.warning("cohort_attribution_daily missing or unreadable", exc_info=True)
        return panel

    latest_date = None
    if latest_row is not None:
        # sqlite3.Row supports indexed AND named access; fall back to index
        # for dict-cursor variants used in some test paths.
        try:
            latest_date = latest_row["d"]
        except (IndexError, KeyError, TypeError):
            latest_date = latest_row[0] if latest_row else None

    panel["summary"]["latest_cohort_date"] = latest_date
    if latest_date is None:
        return panel

    # Staleness — latest cohort_date older than the nightly cadence + jitter.
    try:
        latest_dt = datetime.datetime.fromisoformat(latest_date).replace(
            tzinfo=datetime.timezone.utc
        )
        age_hours = (now_utc - latest_dt).total_seconds() / 3600.0
        panel["summary"]["stale"] = age_hours > COHORT_PANEL_STALE_HOURS
    except (ValueError, TypeError):
        # Unparseable cohort_date — treat as stale defensively.
        panel["summary"]["stale"] = True

    summary_row = db_conn.execute(
        """
        SELECT
          COUNT(*)                                                    AS n_total,
          SUM(CASE WHEN n_30d >= 50 THEN 1 ELSE 0 END)                AS n_eligible_50,
          SUM(CASE WHEN alert_state = 'firing_bleed' THEN 1 ELSE 0 END) AS n_firing_bleed,
          SUM(CASE WHEN alert_state = 'firing_cal'   THEN 1 ELSE 0 END) AS n_firing_cal
        FROM cohort_attribution_daily
        WHERE cohort_date = ?
        """,
        (latest_date,),
    ).fetchone()
    if summary_row is not None:
        try:
            panel["summary"]["n_cohorts_total"] = int(summary_row["n_total"] or 0)
            panel["summary"]["n_cohorts_eligible_n50"] = int(summary_row["n_eligible_50"] or 0)
            panel["summary"]["n_cohorts_firing_bleed"] = int(summary_row["n_firing_bleed"] or 0)
            panel["summary"]["n_cohorts_firing_cal"] = int(summary_row["n_firing_cal"] or 0)
        except (IndexError, KeyError, TypeError):
            n_total, n_elig, n_fb, n_fc = (
                summary_row[0] or 0, summary_row[1] or 0,
                summary_row[2] or 0, summary_row[3] or 0,
            )
            panel["summary"]["n_cohorts_total"] = int(n_total)
            panel["summary"]["n_cohorts_eligible_n50"] = int(n_elig)
            panel["summary"]["n_cohorts_firing_bleed"] = int(n_fb)
            panel["summary"]["n_cohorts_firing_cal"] = int(n_fc)

    bleeder_rows = db_conn.execute(
        """
        SELECT asset, strategy, price_band_5c, stc_band_60s, cell_block_stage,
               n_30d, wr_30d, wilson95_hi_30d,
               cf_pnl_30d_dollars, cal_gap_30d,
               alert_state, last_alert_time
        FROM cohort_attribution_daily
        WHERE cohort_date = ?
          AND cf_pnl_30d_dollars IS NOT NULL
          AND cf_pnl_30d_dollars < 0
        ORDER BY cf_pnl_30d_dollars ASC
        LIMIT ?
        """,
        (latest_date, COHORT_PANEL_LIMIT),
    ).fetchall()
    panel["top_bleeders_30d"] = [_cohort_row_to_panel_entry(r) for r in bleeder_rows]

    cal_drift_rows = db_conn.execute(
        """
        SELECT asset, strategy, price_band_5c, stc_band_60s, cell_block_stage,
               n_30d, wr_30d, wilson95_hi_30d,
               cf_pnl_30d_dollars,
               mean_cal_prob_30d, cal_gap_30d,
               alert_state, last_alert_time
        FROM cohort_attribution_daily
        WHERE cohort_date = ?
          AND cal_gap_30d IS NOT NULL
        ORDER BY ABS(cal_gap_30d) DESC
        LIMIT ?
        """,
        (latest_date, COHORT_PANEL_LIMIT),
    ).fetchall()
    panel["top_cal_drift_30d"] = [
        _cohort_row_to_panel_entry(r, include_mean_cal_prob=True) for r in cal_drift_rows
    ]

    return panel


def _row_get(row, key, default=None):
    """Tolerant accessor — sqlite3.Row supports both indexed and named, but
    name lookup raises on missing keys; sqlite3.Connection.row_factory may
    also be None (raw tuple) in some test paths."""
    try:
        val = row[key]
        return default if val is None else val
    except (IndexError, KeyError, TypeError):
        return default


def _cohort_row_to_panel_entry(row, *, include_mean_cal_prob=False):
    """Convert a `cohort_attribution_daily` SELECT row to the panel-entry
    dict shape per design § Dashboard mock."""
    entry = {
        "asset": _row_get(row, "asset"),
        "strategy": _row_get(row, "strategy"),
        "price_band": _price_band_label(int(_row_get(row, "price_band_5c", 0))),
        "stc_band": _stc_band_label(int(_row_get(row, "stc_band_60s", 0))),
        "stage": _row_get(row, "cell_block_stage"),
        "n": int(_row_get(row, "n_30d", 0)),
        "wr": _row_get(row, "wr_30d"),
        "wilson95_hi": _row_get(row, "wilson95_hi_30d"),
        "cf_pnl_30d": _row_get(row, "cf_pnl_30d_dollars"),
        "cal_gap": _row_get(row, "cal_gap_30d"),
        "alert": _row_get(row, "alert_state"),
        "last_alert_time": _row_get(row, "last_alert_time"),
    }
    if include_mean_cal_prob:
        entry["mean_cal_prob"] = _row_get(row, "mean_cal_prob_30d")
    return entry


def _parse_ob_levels(entries):
    """Parse orderbook level entries into (price_cents, quantity) tuples."""
    result = []
    for e in entries:
        if isinstance(e, (list, tuple)) and len(e) >= 2:
            p, q = e[0], int(e[1])
        elif isinstance(e, dict):
            p, q = e.get("price", 0), int(e.get("quantity", 0))
        else:
            continue
        if isinstance(p, float) and p < 1.0:
            p = round(p * 100)
        else:
            p = int(p)
        if q > 0:
            result.append((p, q))
    return result


class DashboardSnapshotBuilder:
    """Builds dashboard state snapshots. Used by SupabaseSyncer."""

    # TTL cache for slow-changing sections (settled trades, shadows, counterfactual)
    _SLOW_CACHE_TTL = 60  # seconds

    def __init__(self, main_loop):
        self._ml = main_loop
        # Position health tracking (dashboard enrichment)
        self._mid_history: Dict[str, collections.deque] = {}
        self._health_state: Dict[str, str] = {}
        self._health_streak: Dict[str, int] = {}
        # TTL cache for slow-changing snapshot sections
        self._slow_cache: Dict[str, Any] = {}
        self._slow_cache_ts: float = 0

    def _build_snapshot(self, db_conn) -> Dict[str, Any]:
        """Build dashboard snapshot. db_conn is a sqlite3 connection."""
        snap: Dict[str, Any] = {}
        _query_count = 0  # track actual queries this cycle
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        snap["timestamp"] = now_utc.isoformat()

        # Uptime
        snap["uptime_seconds"] = round(time.time() - self._ml._start_time, 1)

        # Balance
        try:
            bal = self._ml.client.get_balance()
            bal_val = round(bal["balance"] / 100, 2) if bal else 0.0
            # Only update last_good_balance if we got a real value (not 0 from transient API state)
            if bal_val > 0:
                self._last_good_balance = bal_val
            snap["current_balance"] = bal_val if bal_val > 0 else getattr(self, "_last_good_balance", 0.0)
            # Read-only: peak tracking moved to main loop to avoid cross-thread mutation
            snap["peak_balance"] = getattr(self._ml, "_peak_balance", snap["current_balance"])
            snap["balance_stale"] = bal_val == 0
        except Exception:
            snap["current_balance"] = getattr(self, "_last_good_balance", 0.0)
            snap["peak_balance"] = getattr(self._ml, "_peak_balance", 0.0)
            snap["balance_stale"] = True

        # Starting balance (rolling HWM — used for drawdown display and session PnL)
        try:
            snap["starting_balance"] = round(self._ml.sizer.starting_balance_cents / 100, 2)
        except Exception:
            snap["starting_balance"] = 0.0

        # Initial deposit (fixed constant — used for true return % on dashboard)
        from bot.constants import INITIAL_DEPOSIT_CENTS
        snap["initial_deposit"] = round(INITIAL_DEPOSIT_CENTS / 100, 2)

        # Drawdown Kelly multiplier
        try:
            bal_cents = int(snap["current_balance"] * 100)
            snap["drawdown_kelly_mult"] = self._ml.sizer._drawdown_scaler_readonly(bal_cents)
        except Exception:
            snap["drawdown_kelly_mult"] = 1.0

        # Balance history (append current, push last hour)
        try:
            self._ml._balance_history.append({
                "ts": snap["timestamp"],
                "bal": snap["current_balance"],
            })
            hist = list(self._ml._balance_history)
            snap["balance_history"] = hist[-360:]  # last hour
            snap["balance_history_4h"] = hist[::max(1, len(hist) // 360)] if len(hist) > 1 else list(hist)
        except Exception:
            logging.warning("Snapshot: balance_history build failed", exc_info=True)
            snap["balance_history"] = []

        _conn = db_conn

        # Single read transaction — consistent snapshot, doesn't block WAL checkpointing
        try:
            _conn.execute("BEGIN DEFERRED")
        except Exception:
            pass  # may already be in a transaction

        # Check if slow-changing sections need refresh
        _now_mono = time.time()
        _run_slow = (_now_mono - self._slow_cache_ts) >= self._SLOW_CACHE_TTL
        if not _run_slow:
            # Merge cached slow sections into snap
            snap.update(self._slow_cache)

        # Active positions
        try:
            rows = _conn.execute(
                "SELECT * FROM positions WHERE status='open'"
            ).fetchall()
            snap["active_positions"] = [dict(r) for r in rows]
        except Exception:
            snap["active_positions"] = []

        # Actual PnL from Kalshi balance (avoids fee overcounting in per-trade data)
        try:
            _bal_cents = int(round(snap["current_balance"] * 100))
            _init_cents = int(round(snap["initial_deposit"] * 100))
            _open_cost = sum(p.get("total_cost_cents", 0) for p in snap.get("active_positions", []))
            _open_fee = sum(p.get("accumulated_fee_cents", 0) or 0 for p in snap.get("active_positions", []))
            snap["actual_pnl_cents"] = _bal_cents - _init_cents + _open_cost + _open_fee
        except Exception:
            snap["actual_pnl_cents"] = None

        # Resting orders
        # cleanup_expired_resting_orders moved to main loop — snapshot is read-only
        try:
            rows = _conn.execute(
                "SELECT * FROM pending_orders WHERE status='resting'"
            ).fetchall()
            snap["resting_orders"] = [dict(r) for r in rows]
        except Exception:
            snap["resting_orders"] = []

        # Track which sections failed for diagnostics
        snap["_snapshot_errors"] = []
        conn = _conn

        # Section 1: Recent trades (15M only for main view, all for toggle)
        try:
            try:
                rows = conn.execute("""
                    SELECT st.ticker, st.event_ticker, st.asset, st.market_result,
                           st.side, st.count, st.entry_price_cents, st.revenue_cents,
                           st.fee_cents, st.pnl_cents, st.settled_at,
                           COALESCE(st.strategy, eo.strategy) AS strategy,
                           COALESCE(st.vol_regime, eo.vol_regime) AS vol_regime,
                           COALESCE(st.seconds_to_close, eo.seconds_to_close) AS ttc,
                           COALESCE(st.edge, eo.edge) AS edge,
                           COALESCE(st.kelly_f, eo.kelly_f) AS kelly_f,
                           st.fill_latency_seconds,
                           COALESCE(st.calibrated_prob, eo.calibrated_prob) AS calibrated_prob,
                           st.product_type
                    FROM settled_trades st
                    LEFT JOIN evaluated_opportunities eo
                        ON eo.rowid = (
                            SELECT eo2.rowid FROM evaluated_opportunities eo2
                            WHERE eo2.ticker = st.ticker
                              AND eo2.filter_stage IN ('candidate', 'observation_trade')
                            ORDER BY eo2.evaluation_time DESC LIMIT 1
                        )
                    WHERE st.product_type = '15m'
                    ORDER BY st.settled_at DESC LIMIT 10
                """).fetchall()
            except Exception:
                rows = conn.execute(
                    "SELECT * FROM settled_trades WHERE product_type='15m' ORDER BY settled_at DESC LIMIT 10"
                ).fetchall()
            snap["recent_trades"] = [dict(r) for r in rows]
            # All-products recent trades for toggle
            try:
                all_rows = conn.execute("""
                    SELECT st.ticker, st.event_ticker, st.asset, st.market_result,
                           st.side, st.count, st.entry_price_cents, st.revenue_cents,
                           st.fee_cents, st.pnl_cents, st.settled_at,
                           COALESCE(st.strategy, eo.strategy) AS strategy,
                           COALESCE(st.vol_regime, eo.vol_regime) AS vol_regime,
                           COALESCE(st.seconds_to_close, eo.seconds_to_close) AS ttc,
                           COALESCE(st.edge, eo.edge) AS edge,
                           COALESCE(st.kelly_f, eo.kelly_f) AS kelly_f,
                           st.fill_latency_seconds,
                           COALESCE(st.calibrated_prob, eo.calibrated_prob) AS calibrated_prob,
                           st.product_type
                    FROM settled_trades st
                    LEFT JOIN evaluated_opportunities eo
                        ON eo.rowid = (
                            SELECT eo2.rowid FROM evaluated_opportunities eo2
                            WHERE eo2.ticker = st.ticker
                              AND eo2.filter_stage IN ('candidate', 'observation_trade')
                            ORDER BY eo2.evaluation_time DESC LIMIT 1
                        )
                    ORDER BY st.settled_at DESC LIMIT 10
                """).fetchall()
                snap["all_products_recent_trades"] = [dict(r) for r in all_rows]
            except Exception:
                snap["all_products_recent_trades"] = []
        except Exception:
            snap["recent_trades"] = []
            snap["_snapshot_errors"].append("recent_trades")

        # Section 2: Win/loss counts + risk metrics — shared single-scan.
        # Previously: 5 separate SELECTs over settled_trades (15m wins, all wins,
        # 15m pnl, all pnl, 15m-regime pnl) + 3 daily-aggregate subqueries = ~8 scans
        # per 30s snapshot. Collapsed to ONE scan; all three scopes bucketed in memory.
        # Wire format unchanged — frontend reads identical keys.
        def _count_wins_losses(rows):
            w, l = 0, 0
            for r in rows:
                side, result = r["side"], r["market_result"]
                if result in ("yes", "all_yes"):
                    if side == "yes": w += 1
                    else: l += 1
                elif result in ("no", "all_no"):
                    if side == "no": w += 1
                    else: l += 1
            return w, l

        try:
            all_settled_rows = conn.execute("""
                SELECT side, market_result, product_type, settled_at,
                       DATE(settled_at) AS day,
                       (pnl_cents - fee_cents) AS net
                FROM settled_trades
                ORDER BY settled_at
            """).fetchall()
        except Exception:
            logging.warning("Snapshot: settled_trades single-scan failed", exc_info=True)
            all_settled_rows = []

        # Bucket once — three scopes read from the same in-memory list
        settled_15m = [r for r in all_settled_rows if r["product_type"] == "15m"]
        settled_15m_regime = [r for r in settled_15m
                              if r["settled_at"] and r["settled_at"] >= CONFIG_REGIME_SINCE]

        try:
            win, loss = _count_wins_losses(settled_15m)
            snap["win_count"] = win
            snap["loss_count"] = loss
            snap["win_rate"] = round(win / (win + loss), 4) if (win + loss) > 0 else 0.0

            all_win, all_loss = _count_wins_losses(all_settled_rows)
            snap["all_products_win_count"] = all_win
            snap["all_products_loss_count"] = all_loss
            snap["all_products_win_rate"] = round(all_win / (all_win + all_loss), 4) if (all_win + all_loss) > 0 else 0.0
        except Exception:
            snap["win_count"] = 0
            snap["loss_count"] = 0
            snap["win_rate"] = 0.0
            snap["all_products_win_count"] = 0
            snap["all_products_loss_count"] = 0
            snap["all_products_win_rate"] = 0.0
            snap["_snapshot_errors"].append("win_loss_counts")

        # Section 3: Daily P&L (15M only) — sum from in-memory bucket
        try:
            today_midnight = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            daily_total = sum(r["net"] for r in settled_15m
                              if r["settled_at"] and r["settled_at"] >= today_midnight
                              and r["net"] is not None)
            snap["daily_pnl_cents"] = daily_total
            start_cents = self._ml.sizer.starting_balance_cents
            if start_cents > 0:
                snap["daily_pnl_pct"] = round(snap["daily_pnl_cents"] / start_cents * 100, 2)
            else:
                snap["daily_pnl_pct"] = 0.0
        except Exception:
            snap["daily_pnl_cents"] = 0
            snap["daily_pnl_pct"] = 0.0
            snap["_snapshot_errors"].append("daily_pnl")

        # Section 4: Current streak (15M only) — wins or losses, from in-memory bucket
        try:
            # Last 50 settled trades, reversed (most recent first)
            recent_settled = settled_15m[-50:][::-1]
            loss_streak = 0
            win_streak = 0
            for r in recent_settled:
                side, result = r["side"], r["market_result"]
                is_win = (result in ("yes", "all_yes") and side == "yes") or \
                         (result in ("no", "all_no") and side == "no")
                if is_win:
                    break
                loss_streak += 1
            for r in recent_settled:
                side, result = r["side"], r["market_result"]
                is_win = (result in ("yes", "all_yes") and side == "yes") or \
                         (result in ("no", "all_no") and side == "no")
                if not is_win:
                    break
                win_streak += 1
            snap["consecutive_losses"] = loss_streak
            snap["consecutive_wins"] = win_streak
        except Exception:
            snap["consecutive_losses"] = 0
            snap["consecutive_wins"] = 0
            snap["_snapshot_errors"].append("consecutive_losses")

        # Risk metrics (15M only + all products for toggle)
        try:
            risk = {}
            peak = snap.get("peak_balance", 0)
            cur = snap.get("current_balance", 0)
            risk["max_drawdown_pct"] = round((peak - cur) / peak * 100, 2) if peak > 0 else 0.0
            risk["max_drawdown_dollars"] = round(peak - cur, 2)

            def _true_max_drawdown(pnl_rows, starting_cents):
                """Compute true peak-to-trough max drawdown from equity curve."""
                if not pnl_rows or starting_cents <= 0:
                    return 0.0, 0
                cumulative = 0
                peak_bal = starting_cents
                max_dd_cents = 0
                for r in pnl_rows:
                    cumulative += r["net"]
                    cur_bal = starting_cents + cumulative
                    if cur_bal > peak_bal:
                        peak_bal = cur_bal
                    dd = peak_bal - cur_bal
                    if dd > max_dd_cents:
                        max_dd_cents = dd
                return round(max_dd_cents / peak_bal * 100, 2) if peak_bal > 0 else 0.0, max_dd_cents

            def _compute_risk_stats(rows):
                """Compute risk stats from a pre-filtered row list. Row must expose
                'net' (cents) and 'day' (YYYY-MM-DD). Previously issued a second SQL
                query per call for daily aggregation; now fully in-memory."""
                nets = [r["net"] for r in rows if r["net"] is not None]
                n = len(nets)
                stats = {}
                if n > 0:
                    total = sum(nets)
                    # Daily-aggregated Sharpe — group by day in memory, annualize √365 (crypto trades 24/7)
                    daily_map = {}
                    for r in rows:
                        net = r["net"]
                        if net is None:
                            continue
                        daily_map[r["day"]] = daily_map.get(r["day"], 0) + net
                    daily_nets = list(daily_map.values())
                    n_days = len(daily_nets)
                    if n_days > 1:
                        d_mean = sum(daily_nets) / n_days
                        d_var = sum((x - d_mean) ** 2 for x in daily_nets) / (n_days - 1)
                        d_std = d_var ** 0.5
                        stats["sharpe_ratio"] = round(d_mean / d_std * (365 ** 0.5), 2) if d_std > 0 else 0.0
                    else:
                        stats["sharpe_ratio"] = 0.0
                    stats["total_pnl_cents"] = total
                    stats["avg_pnl_per_trade"] = round(total / n, 1)
                    gross_wins = sum(x for x in nets if x > 0)
                    gross_losses = abs(sum(x for x in nets if x < 0))
                    stats["profit_factor"] = round(gross_wins / gross_losses, 2) if gross_losses > 0 else 999.0
                    stats["total_trades"] = n
                else:
                    stats.update({"sharpe_ratio": 0, "total_pnl_cents": 0, "avg_pnl_per_trade": 0,
                                  "profit_factor": 0, "total_trades": 0})
                return stats

            # 15M only — reuse the in-memory bucket from the single-scan above
            risk.update(_compute_risk_stats(settled_15m))
            try:
                start_cents = self._ml.sizer.starting_balance_cents
                risk["true_max_drawdown_pct"], risk["true_max_drawdown_cents"] = _true_max_drawdown(
                    settled_15m, start_cents
                )
            except Exception:
                pass
            snap["risk_metrics"] = risk

            # All products — bucket already includes everything
            all_risk = {}
            all_risk["max_drawdown_pct"] = risk["max_drawdown_pct"]
            all_risk["max_drawdown_dollars"] = risk["max_drawdown_dollars"]
            all_risk.update(_compute_risk_stats(all_settled_rows))
            snap["all_products_risk_metrics"] = all_risk

            # Regime-filtered (current config only) — 15m bucket filtered by CONFIG_REGIME_SINCE
            try:
                r_win, r_loss = _count_wins_losses(settled_15m_regime)
                regime_risk = {}
                regime_risk.update(_compute_risk_stats(settled_15m_regime))
                regime_risk["win_count"] = r_win
                regime_risk["loss_count"] = r_loss
                regime_risk["win_rate"] = round(r_win / (r_win + r_loss), 4) if (r_win + r_loss) > 0 else 0.0
                try:
                    regime_risk["true_max_drawdown_pct"], regime_risk["true_max_drawdown_cents"] = _true_max_drawdown(
                        settled_15m_regime, self._ml.sizer.starting_balance_cents
                    )
                except Exception:
                    pass
                snap["regime_risk_metrics"] = regime_risk
                snap["config_regime_since"] = CONFIG_REGIME_SINCE
            except Exception:
                logging.warning("Snapshot: regime_risk_metrics failed", exc_info=True)
                snap["regime_risk_metrics"] = None
        except Exception:
            logging.warning("Snapshot: risk_metrics build failed", exc_info=True)
            snap["risk_metrics"] = None
            snap["all_products_risk_metrics"] = None

        # Execution quality
        try:
            lats = list(self._ml._recent_fill_latencies)
            eq = {}
            if lats:
                lats_sorted = sorted(lats)
                eq["avg_fill_latency_ms"] = round(sum(lats) / len(lats) * 1000, 1)
                eq["median_fill_latency_ms"] = round(lats_sorted[len(lats_sorted) // 2] * 1000, 1)
                eq["min_fill_latency_ms"] = round(lats_sorted[0] * 1000, 1)
                eq["max_fill_latency_ms"] = round(lats_sorted[-1] * 1000, 1)
                eq["recent_count"] = len(lats)
            else:
                eq = {"avg_fill_latency_ms": 0, "median_fill_latency_ms": 0,
                      "min_fill_latency_ms": 0, "max_fill_latency_ms": 0, "recent_count": 0}
            ex = self._ml.executor
            eq["session_fills"] = getattr(ex, "_session_ws_fills", 0) + getattr(ex, "_session_rest_fills", 0)
            maker_subs = getattr(self._ml, "_session_maker_submissions", 0)
            maker_fills = getattr(self._ml, "_session_maker_fills", 0)
            eq["maker_fill_rate"] = round(maker_fills / maker_subs, 3) if maker_subs > 0 else 0.0
            snap["execution_quality"] = eq
        except Exception:
            logging.warning("Snapshot: execution_quality build failed", exc_info=True)
            snap["execution_quality"] = None

        # Volatility from cache (read-only)
        try:
            vol_data = {}
            for asset in ASSETS:
                cached = self._ml.vol._cache.get(asset)
                if cached:
                    vol_data[asset] = {
                        "blended_rv": cached["blended_rv"],
                        "regime": cached["regime"],
                        "dvol_5s": cached.get("dvol_5s"),
                        "iv_rv_blend_method": cached.get("iv_rv_blend_method"),
                        "jump_component": cached.get("jump_component", 0),
                        "rv_1min": cached.get("rv_1min"),
                        "rv_5min": cached.get("rv_5min"),
                        "rv_15min": cached.get("rv_15min"),
                        "bv_5min": cached.get("bv_5min"),
                        "bv_15min": cached.get("bv_15min"),
                        "iv_rv_spread": cached.get("iv_rv_spread"),
                        "num_returns": cached.get("num_returns", 0),
                        "jump_seconds_remaining": cached.get("jump_seconds_remaining", 0),
                        "fixed_blend_rv": cached.get("fixed_blend_rv"),
                        "jump_multiplier": cached.get("jump_multiplier", 1.0),
                        "jump_event_count": cached.get("jump_event_count", 0),
                        "adaptive_jump_multiplier": cached.get("adaptive_jump_multiplier", 1.0),
                        "adaptive_jump_regime": cached.get("adaptive_jump_regime", "normal"),
                        "adaptive_jump_event_count": cached.get("adaptive_jump_event_count", 0),
                        "adaptive_ewma_sigma": cached.get("adaptive_ewma_sigma"),
                        "adaptive_n_obs_15s": cached.get("adaptive_n_obs_15s", 0),
                        "egarch_sigma": cached.get("egarch_sigma"),
                        "egarch_n_updates": cached.get("egarch_n_updates", 0),
                        "egarch_log_var": cached.get("egarch_log_var"),
                        # Adaptive RK bandwidth
                        "omega_sq": cached.get("omega_sq"),
                        "rk_H_adaptive_5": cached.get("rk_H_adaptive_5"),
                        "rk_H_adaptive_15": cached.get("rk_H_adaptive_15"),
                        "ark_5min": cached.get("ark_5min"),
                        "ark_15min": cached.get("ark_15min"),
                        # DVOL diagnostics
                        "dvol_sq_hourly": cached.get("dvol_sq_hourly"),
                        "vrp": cached.get("vrp"),
                        # Shadow time-varying RK weights
                        "shadow_tv_blend_rv": cached.get("shadow_tv_blend_rv"),
                        "shadow_tv_weights": cached.get("shadow_tv_weights"),
                        # Shadow sigmoid QLIKE
                        "mz_shadow_sigmoid_w": cached.get("mz_shadow_sigmoid_w"),
                        "mz_baseline_qlike": cached.get("mz_baseline_qlike"),
                    }
                else:
                    vol_data[asset] = None
            snap["current_volatility"] = vol_data
        except Exception:
            snap["current_volatility"] = {}

        # Spot prices
        try:
            snap["spot_prices"] = self._ml.feed.get_all_prices()
        except Exception:
            snap["spot_prices"] = {}

        # Funding rates (numeric)
        try:
            funding = {}
            for asset in ASSETS:
                rate = self._ml.coinglass.get_funding_rate(asset)
                funding[asset] = rate  # float or None
            snap["funding_rates"] = funding
        except Exception:
            snap["funding_rates"] = {}

        # Cross-exchange prices and premia
        snap["cross_exchange"] = {}
        try:
            if hasattr(self._ml, 'cross_feed') and self._ml.cross_feed:
                cx_data = {}
                for asset in ASSETS:
                    cx_data[asset] = {
                        "prices": self._ml.cross_feed.get_prices(asset),
                        "lead_lag": self._ml.cross_feed.get_lead_lag(asset),
                    }
                snap["cross_exchange"] = cx_data
        except Exception:
            snap["cross_exchange"] = {}

        # Cross-exchange feed health
        try:
            if hasattr(self._ml, 'cross_feed') and self._ml.cross_feed:
                snap["feed_health"] = dict(self._ml.cross_feed._connected)
            else:
                snap["feed_health"] = {}
        except Exception:
            snap["feed_health"] = {}

        # Order flow signals
        try:
            if hasattr(self._ml, 'order_flow') and self._ml.order_flow:
                ofa_data = {}
                for asset in ASSETS:
                    signals = self._ml.order_flow.get_signals(asset)
                    ofa_data[asset] = {
                        "prob_adjustment": signals["prob_adjustment"],
                        "confidence": signals["confidence"],
                        "consensus": signals["signals"].get("cross_exchange", {}).get("consensus_direction"),
                        "funding_level": signals["signals"].get("funding", {}).get("level"),
                    }
                snap["order_flow"] = ofa_data
        except Exception:
            snap["order_flow"] = {}

        # Active windows breakdown
        try:
            windows = self._ml._active_windows
            if windows:
                snap["seconds_to_next_close"] = round(
                    min(w["seconds_to_close"] for w in windows), 1
                )
                by_asset = {}
                hourly_count = 0
                fifteenm_count = 0
                for w in windows:
                    a = w["asset"]
                    by_asset[a] = by_asset.get(a, 0) + 1
                    if w.get("product_type") == "hourly":
                        hourly_count += 1
                    else:
                        fifteenm_count += 1
                snap["active_windows"] = {
                    "total": len(windows),
                    "by_asset": by_asset,
                    "fifteenm": fifteenm_count,
                    "hourly": hourly_count,
                }
            else:
                snap["seconds_to_next_close"] = -1
                snap["active_windows"] = {"total": 0, "by_asset": {}, "fifteenm": 0, "hourly": 0}
        except Exception:
            snap["seconds_to_next_close"] = -1
            snap["active_windows"] = {"total": 0, "by_asset": {}, "fifteenm": 0, "hourly": 0}

        # Convergence velocity per asset
        try:
            conv = {}
            scanner = self._ml.scanner
            # Snapshot keys to avoid RuntimeError from dict mutation during iteration
            ticker_keys = list(scanner._ticker_ask_history.keys())
            for asset in ASSETS:
                velocities = []
                for ticker in ticker_keys:
                    history = scanner._ticker_ask_history.get(ticker)
                    if history and asset.lower() in ticker.lower() and len(history) >= 2:
                        velocities.append(scanner._scanner_convergence_velocity(ticker))
                conv[asset] = round(max(velocities), 2) if velocities else 0.0
            snap["convergence_velocity"] = conv
        except Exception:
            logging.warning("Snapshot: convergence_velocity build failed", exc_info=True)
            snap["convergence_velocity"] = None

        # Bot status
        try:
            if self._ml._last_error and (time.time() - self._ml._last_error_time < 120):
                snap["bot_status"] = "ERROR"
            elif self._ml.executor.has_active_order:
                snap["bot_status"] = "TRADING"
            elif self._ml._active_windows:
                snap["bot_status"] = "SCANNING"
            else:
                snap["bot_status"] = "IDLE"
        except Exception:
            snap["bot_status"] = "UNKNOWN"

        snap["last_error_message"] = getattr(self._ml, "_last_error", None) or ""

        # Active order detail (makes TRADING status informative)
        try:
            order = self._ml.executor._active_order
            if order:
                snap["active_order"] = {
                    "ticker": order["ticker"],
                    "asset": order["asset"],
                    "price_cents": order["price_cents"],
                    "count": order["count"],
                    "is_taker": order.get("is_taker", False),
                    "is_panic": order.get("is_panic", False),
                    "elapsed_seconds": round(time.time() - order["submit_time"], 1),
                    "seconds_to_close": round(
                        order["seconds_to_close_at_submit"] - (time.time() - order["submit_time"]), 1
                    ),
                    "edge": order.get("candidate", {}).get("edge"),
                    "kelly_fraction": order.get("candidate", {}).get("kelly_fraction"),
                    "strategy": order.get("candidate", {}).get("strategy"),
                    "cal_prob": order.get("candidate", {}).get("calibrated_prob"),
                    "execution_method": order.get("execution_method", "legacy"),
                    "fill_source": order.get("fill_source"),
                    "escalated": order.get("escalated", False),
                    "queue_position": order.get("queue_position"),
                }
            else:
                snap["active_order"] = None
        except Exception:
            snap["active_order"] = None

        # Filter funnel (per-asset rejection breakdown from last scan)
        try:
            stats = self._ml.scanner._last_scan_stats
            snap["filter_funnel"] = stats if stats else {}
        except Exception:
            snap["filter_funnel"] = {}

        # Rate limit pressure
        try:
            reads = len(self._ml.client._read_timestamps)
            writes = len(self._ml.client._write_timestamps)
            snap["rate_limits"] = {
                "reads_last_second": reads,
                "writes_last_second": writes,
                "read_limit": 30,
                "write_limit": 30,
                # Dashboard-compatible fields
                "exchange_requests": {
                    "used": reads,
                    "limit": 30,
                    "remaining": max(0, 30 - reads),
                },
                "order_requests": {
                    "used": writes,
                    "limit": 30,
                    "remaining": max(0, 30 - writes),
                },
            }
        except Exception:
            snap["rate_limits"] = {}

        # Settlement queue
        try:
            snap["pending_settlements"] = len(self._ml.tracker._pending_rejection_tickers)
        except Exception:
            snap["pending_settlements"] = 0

        # ── recent_opportunities (last 20 evaluated with market data) ────
        try:
            scanner = self._ml.scanner
            snap["recent_opportunities"] = list(scanner._recent_opportunities)
        except Exception:
            snap["recent_opportunities"] = []

        # ── session_stats ────────────────────────────────────────────────
        try:
            start_time = self._ml._start_time
            total_scanned = self._ml.scanner._session_total_scanned
            ex = self._ml.executor
            total_fills = getattr(ex, "_session_ws_fills", 0) + getattr(ex, "_session_rest_fills", 0)
            snap["session_stats"] = {
                "session_start_time": start_time,
                "total_evaluations": total_scanned,
                "total_orders_placed": self._ml.scanner._session_total_candidates,
                "total_fills": total_fills,
                "total_cancels": getattr(ex, "_session_ioc_unfilled", 0),
                "active_event_tickers": len(self._ml._active_windows),
            }
        except Exception:
            snap["session_stats"] = {}

        # ── real_trade_analytics (from settled_trades, 15M only) ─────────
        try:
            conn = _conn
            rta = {}
            _pt_filter = " WHERE product_type='15m'"
            _win_case = ("COUNT(CASE WHEN (market_result IN ('yes','all_yes') AND side='yes') "
                         "OR (market_result IN ('no','all_no') AND side='no') THEN 1 END)")

            # P&L by asset
            asset_rows = conn.execute(
                f"SELECT asset, COUNT(*) AS cnt, {_win_case} AS wins, "
                f"  COALESCE(SUM(pnl_cents - fee_cents), 0) AS net_pnl "
                f"FROM settled_trades{_pt_filter} GROUP BY asset"
            ).fetchall()
            rta["by_asset"] = {r["asset"]: {"count": r["cnt"], "wins": r["wins"], "net_pnl": r["net_pnl"]} for r in asset_rows}

            # P&L by price bucket
            bucket_rows = conn.execute(
                f"SELECT CASE "
                f"  WHEN entry_price_cents BETWEEN 86 AND 89 THEN '86-89' "
                f"  WHEN entry_price_cents BETWEEN 90 AND 94 THEN '90-94' "
                f"  WHEN entry_price_cents BETWEEN 95 AND 99 THEN '95-99' "
                f"  ELSE 'other' END AS bucket, "
                f"COUNT(*) AS cnt, {_win_case} AS wins, "
                f"COALESCE(SUM(pnl_cents - fee_cents), 0) AS net_pnl "
                f"FROM settled_trades{_pt_filter} GROUP BY bucket"
            ).fetchall()
            _BUCKET_MIDPOINTS = {"80-84": 82, "85-89": 87, "90-94": 92, "95-99": 97, "86-89": 87.5, "90-92": 91, "93-95": 94, "96-99": 97.5}
            by_bucket_dict = {}
            for r in bucket_rows:
                entry = {"count": r["cnt"], "wins": r["wins"], "net_pnl": r["net_pnl"]}
                mid_p = _BUCKET_MIDPOINTS.get(r["bucket"])
                if mid_p is not None:
                    fee = math.ceil(SIM_FEE_RATE * (mid_p / 100.0) * (1 - mid_p / 100.0))
                    entry["breakeven_wr"] = round((mid_p + fee) / 100.0, 4)
                by_bucket_dict[r["bucket"]] = entry
            rta["by_bucket"] = by_bucket_dict

            # P&L by strategy
            strat_rows = conn.execute(
                f"SELECT COALESCE(strategy, 'unknown') AS strat, COUNT(*) AS cnt, {_win_case} AS wins, "
                f"  COALESCE(SUM(pnl_cents - fee_cents), 0) AS net_pnl "
                f"FROM settled_trades{_pt_filter} GROUP BY strat"
            ).fetchall()
            rta["by_strategy"] = {r["strat"]: {"count": r["cnt"], "wins": r["wins"], "net_pnl": r["net_pnl"]} for r in strat_rows}

            # P&L by hour (UTC)
            hour_rows = conn.execute(
                f"SELECT CAST(SUBSTR(settled_at, 12, 2) AS INTEGER) AS hour, "
                f"  COUNT(*) AS cnt, {_win_case} AS wins, "
                f"  COALESCE(SUM(pnl_cents - fee_cents), 0) AS net_pnl "
                f"FROM settled_trades WHERE product_type='15m' AND settled_at IS NOT NULL GROUP BY hour"
            ).fetchall()
            rta["by_hour"] = {str(r["hour"]): {"count": r["cnt"], "wins": r["wins"], "net_pnl": r["net_pnl"]} for r in hour_rows}

            # Best and worst trade (15M only)
            best = conn.execute(
                f"SELECT ticker, asset, entry_price_cents, (pnl_cents - fee_cents) AS net, settled_at "
                f"FROM settled_trades{_pt_filter} ORDER BY net DESC LIMIT 1"
            ).fetchone()
            worst = conn.execute(
                f"SELECT ticker, asset, entry_price_cents, (pnl_cents - fee_cents) AS net, settled_at "
                f"FROM settled_trades{_pt_filter} ORDER BY net ASC LIMIT 1"
            ).fetchone()
            if best:
                rta["best_trade"] = dict(best)
            if worst:
                rta["worst_trade"] = dict(worst)

            # Cumulative P&L time series (15M for chart)
            # Thin to ~100 points max to reduce Supabase Realtime egress
            # (~50KB savings per push × 2880/day = ~145MB/day saved)
            pnl_series = conn.execute(
                f"SELECT settled_at, (pnl_cents - fee_cents) AS net "
                f"FROM settled_trades{_pt_filter} ORDER BY settled_at"
            ).fetchall()
            cumulative = []
            running = 0
            for r in pnl_series:
                running += r["net"]
                cumulative.append({"ts": r["settled_at"], "cum_pnl": running})
            _MAX_CHART_POINTS = 100
            if len(cumulative) > _MAX_CHART_POINTS:
                _step = len(cumulative) / _MAX_CHART_POINTS
                _thinned = [cumulative[int(i * _step)] for i in range(_MAX_CHART_POINTS - 1)]
                _thinned.append(cumulative[-1])  # always include latest
                cumulative = _thinned
            rta["cumulative_pnl"] = cumulative

            # All-products cumulative P&L (for toggle)
            all_pnl_series = conn.execute(
                "SELECT settled_at, (pnl_cents - fee_cents) AS net, product_type, strategy "
                "FROM settled_trades ORDER BY settled_at"
            ).fetchall()
            all_cumulative = []
            all_running = 0
            for r in all_pnl_series:
                all_running += r["net"]
                all_cumulative.append({"ts": r["settled_at"], "cum_pnl": all_running,
                                       "pt": r["product_type"], "strat": r["strategy"]})
            if len(all_cumulative) > _MAX_CHART_POINTS:
                _step = len(all_cumulative) / _MAX_CHART_POINTS
                _thinned = [all_cumulative[int(i * _step)] for i in range(_MAX_CHART_POINTS - 1)]
                _thinned.append(all_cumulative[-1])
                all_cumulative = _thinned
            rta["all_products_cumulative_pnl"] = all_cumulative

            snap["real_trade_analytics"] = rta

            # Regime-filtered trade analytics (current config only)
            try:
                _regime_filter = f" WHERE product_type='15m' AND settled_at >= '{CONFIG_REGIME_SINCE}'"
                r_asset = conn.execute(
                    f"SELECT asset, COUNT(*) AS cnt, {_win_case} AS wins, "
                    f"  COALESCE(SUM(pnl_cents - fee_cents), 0) AS net_pnl "
                    f"FROM settled_trades{_regime_filter} GROUP BY asset"
                ).fetchall()
                r_bucket = conn.execute(
                    f"SELECT CASE "
                    f"  WHEN entry_price_cents BETWEEN 86 AND 89 THEN '86-89' "
                    f"  WHEN entry_price_cents BETWEEN 90 AND 94 THEN '90-94' "
                    f"  WHEN entry_price_cents BETWEEN 95 AND 99 THEN '95-99' "
                    f"  ELSE 'other' END AS bucket, "
                    f"COUNT(*) AS cnt, {_win_case} AS wins, "
                    f"COALESCE(SUM(pnl_cents - fee_cents), 0) AS net_pnl "
                    f"FROM settled_trades{_regime_filter} GROUP BY bucket"
                ).fetchall()
                snap["regime_trade_analytics"] = {
                    "since": CONFIG_REGIME_SINCE,
                    "by_asset": {r["asset"]: {"count": r["cnt"], "wins": r["wins"], "net_pnl": r["net_pnl"]} for r in r_asset},
                    "by_bucket": {r["bucket"]: {"count": r["cnt"], "wins": r["wins"], "net_pnl": r["net_pnl"]} for r in r_bucket},
                }
            except Exception:
                logging.warning("Snapshot: regime_trade_analytics failed", exc_info=True)
                snap["regime_trade_analytics"] = None
        except Exception:
            logging.warning("Snapshot: real_trade_analytics build failed", exc_info=True)
            snap["real_trade_analytics"] = {}


        # ── observation_mode flag ─────────────────────────────────────
        try:
            snap["observation_mode"] = getattr(self._ml, '_observation_mode', True)
        except Exception:
            snap["observation_mode"] = True

        # ── trading config values ──────────────────────────────────────
        try:
            import bot.runtime_config as _bot_mod  # Bit 9.3-iii.c (2026-05-11): bot.runtime_config is a PEP 562 __getattr__ dual-probe of bot.constants then bot.config — preserves getattr-fallback semantics + mutation freshness for runtime-mutable kill-switch flags. See bot/runtime_config.py.
            snap["trading_config"] = {
                "min_edge_by_price": getattr(_bot_mod, "MIN_EDGE_BY_PRICE", []),
                "market_blend_w": getattr(_bot_mod, "MARKET_BLEND_W", None),
                "max_risk_per_trade": getattr(_bot_mod, "MAX_RISK_PER_TRADE", None),
                "sizing_tiers": getattr(_bot_mod, "SIZING_TIERS", []),
                "min_entry_price": getattr(_bot_mod, "MIN_ENTRY_PRICE", None),
                "max_entry_price": getattr(_bot_mod, "MAX_ENTRY_PRICE", None),
                "max_seconds_before_close": getattr(_bot_mod, "MAX_SECONDS_BEFORE_CLOSE", None),
                "maker_only_threshold": getattr(_bot_mod, "MAKER_ONLY_THRESHOLD", None),
                "drawdown_half": getattr(_bot_mod, "DRAWDOWN_HALF_THRESHOLD", None),
                "drawdown_quarter": getattr(_bot_mod, "DRAWDOWN_QUARTER_THRESHOLD", None),
                "drawdown_halt": getattr(_bot_mod, "DRAWDOWN_HALT_THRESHOLD", None),
                "hourly_observation_only": getattr(_bot_mod, "HOURLY_OBSERVATION_ONLY", True),
                "spx_hourly_enabled": getattr(_bot_mod, "SPX_HOURLY_ENABLED", False),
                "spx_hourly_observation_only": getattr(_bot_mod, "SPX_HOURLY_OBSERVATION_ONLY", True),
                "weather_enabled": getattr(_bot_mod, "WEATHER_ENABLED", False),
                "weather_observation_only": getattr(_bot_mod, "WEATHER_OBSERVATION_ONLY", True),
                "sports_enabled": getattr(_bot_mod, "SPORTS_ENABLED", False),
                "sports_observation_only": getattr(_bot_mod, "SPORTS_OBSERVATION_ONLY", True),
                "config_regime_since": CONFIG_REGIME_SINCE,
                "sim_fee_rate": SIM_FEE_RATE,
            }
        except Exception:
            snap["trading_config"] = {}

        # ── calibration diagnostics ───────────────────────────────────
        try:
            diag = self._ml.calibration.get_diagnostics()
            diag["min_platt"] = 200
            diag["min_beta"] = 350
            diag["min_blr"] = 50
            snap["calibration"] = diag
        except Exception:
            snap["calibration"] = None

        # ── hourly calibration diagnostics ──────────────────────────────
        hourly_cal = getattr(self._ml, "hourly_calibration", None)
        if hourly_cal:
            try:
                snap["hourly_calibration"] = hourly_cal.get_diagnostics()
            except Exception:
                pass

        # ── Per-engine CalEngine registry diagnostics ─────────────────
        try:
            _cal_engines = getattr(self._ml, "_cal_engines", {})
            _cal_meta = getattr(self._ml, "_cal_engine_meta", {})
            if _cal_engines:
                _reg = {}
                for _key, _eng in _cal_engines.items():
                    try:
                        _d = _eng.get_diagnostics()
                        _pt, _sub = _cal_meta.get(_key, (_key, None))
                        _reg[_key] = {
                            "product_type": _pt,
                            "subtype": _sub,
                            "n_observations": _d.get("n_observations", 0),
                            "active_method": _d.get("active_method", "raw"),
                            "rolling_brier": _d.get("rolling_brier"),
                            "learned_method_active": _d.get("learned_method_active", False),
                        }
                    except Exception:
                        _reg[_key] = {"n_observations": 0, "error": True}
                snap["cal_registry"] = _reg
        except Exception:
            logging.warning("Snapshot: cal_registry build failed", exc_info=True)

        # ── NIG distribution parameters ────────────────────────────────
        try:
            import json as _json
            # Sprint 10 Bit 10.4 (2026-05-12): anchor to REPO ROOT so the file
            # resolves correctly post-relocation to bot/snapshots/. 3-level
            # dirname chain mirrors bot/engines/weather_engine.py:806-807.
            _dash_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            dist_path = os.path.join(_dash_repo_root, "dist_config.json")
            if os.path.exists(dist_path):
                with open(dist_path) as f:
                    snap["nig_distribution"] = _json.load(f)
            else:
                snap["nig_distribution"] = None
        except Exception:
            snap["nig_distribution"] = None

        # ── EGARCH estimation diagnostics ─────────────────────────────
        try:
            snap["egarch_estimation"] = self._ml.egarch_estimator.get_diagnostics()
        except Exception:
            snap["egarch_estimation"] = None

        # ── EGARCH blend diagnostics ──────────────────────────────────
        try:
            mz = getattr(self._ml, "mz_tracker", None)
            if mz:
                import bot.runtime_config as _bot_mod  # Bit 9.3-iii.c (2026-05-11): bot.runtime_config is a PEP 562 __getattr__ dual-probe of bot.constants then bot.config — preserves getattr-fallback semantics + mutation freshness for runtime-mutable kill-switch flags. See bot/runtime_config.py.
                snap["egarch_blend"] = {
                    "shadow_mode": getattr(_bot_mod, "EGARCH_BLEND_SHADOW_MODE", True),
                    "weights": {a: mz.get_weight(a) for a in ASSETS},
                    "r_squared": dict(mz._r_squared),
                    "qlike": dict(mz._qlike),
                    "obs_count": {a: len(mz._pairs[a]) for a in ASSETS},
                    "prev_weights": {a: mz._prev_weight.get(a) for a in ASSETS},
                    "ema_lambda": getattr(_bot_mod, "MZ_EMA_LAMBDA", None),
                    "equal_weight_threshold": getattr(_bot_mod, "MZ_EQUAL_WEIGHT_R2_THRESHOLD", None),
                    "mz_window": getattr(_bot_mod, "MZ_WINDOW", None),
                    # Shadow sigmoid QLIKE weight mapping
                    "sigmoid_shadow_mode": getattr(_bot_mod, "MZ_SIGMOID_SHADOW_MODE", True),
                    "baseline_qlike": dict(mz._baseline_qlike),
                    "shadow_sigmoid_w": dict(mz._shadow_sigmoid_w),
                }
        except Exception:
            logging.warning("Snapshot: egarch_blend build failed", exc_info=True)

        # ── counterfactual analysis (15M only, current regime) ──────────
        try:
            conn = _conn
            _cf_pt_filter = "AND product_type='15m'"
            _cf_regime_filter = f"AND evaluation_time >= '{CONFIG_REGIME_SINCE}'"

            # By filter stage
            stage_rows = conn.execute(
                "SELECT filter_stage, COUNT(*) AS total, "
                "  COUNT(CASE WHEN counterfactual_pnl > 0 THEN 1 END) AS wins, "
                "  COUNT(CASE WHEN counterfactual_pnl <= 0 THEN 1 END) AS losses, "
                "  COALESCE(SUM(counterfactual_pnl), 0) AS net_pnl_cents "
                "FROM evaluated_opportunities "
                f"WHERE status = 'settled' AND counterfactual_pnl IS NOT NULL {_cf_pt_filter} {_cf_regime_filter} "
                "GROUP BY filter_stage"
            ).fetchall()
            by_stage = []
            # Merge shadow variants into clear display rows:
            #   "price_shadow" = all assets combined (old pre-split + xrp + no_xrp)
            #   "price_shadow (no XRP)" = just the no-XRP variant
            #   Same pattern for stc_shadow.
            # DB stages: price_shadow (old), price_shadow_xrp, price_shadow_no_xrp,
            #            stc_shadow (old), stc_shadow_xrp, stc_shadow_no_xrp, xrp_shadow
            _merge_rules = {
                # stage_in_db -> list of display buckets to add to
                "price_shadow":        ["price_shadow"],
                "price_shadow_xrp":    ["price_shadow"],
                "price_shadow_no_xrp": ["price_shadow", "price_shadow (no XRP)"],
                "stc_shadow":          ["stc_shadow"],
                "stc_shadow_xrp":      ["stc_shadow"],
                "stc_shadow_no_xrp":   ["stc_shadow", "stc_shadow (no XRP)"],
                # Per-asset low-price floor shadow variants
                "eth_low_floor_shadow": ["eth_low_floor_shadow"],
                "sol_low_floor_shadow": ["sol_low_floor_shadow"],
                # NO-side shadow variants (mirror YES-side taxonomy)
                "no_side_price_shadow_xrp":    ["no_side_price_shadow"],
                "no_side_price_shadow_no_xrp": ["no_side_price_shadow", "no_side_price_shadow (no XRP)"],
                "no_side_stc_shadow_xrp":      ["no_side_stc_shadow"],
                "no_side_stc_shadow_no_xrp":   ["no_side_stc_shadow", "no_side_stc_shadow (no XRP)"],
            }
            _merged = {}  # display_name -> {total, wins, losses, net_pnl_cents}
            for r in stage_rows:
                stage = r["filter_stage"]
                buckets = _merge_rules.get(stage)
                if buckets:
                    for bucket in buckets:
                        if bucket not in _merged:
                            _merged[bucket] = {"total": 0, "wins": 0, "losses": 0, "net_pnl_cents": 0}
                        _merged[bucket]["total"] += r["total"]
                        _merged[bucket]["wins"] += r["wins"]
                        _merged[bucket]["losses"] += r["losses"]
                        _merged[bucket]["net_pnl_cents"] += r["net_pnl_cents"]
                else:
                    # Normal stage — pass through
                    total = r["total"]
                    by_stage.append({
                        "stage": stage,
                        "total": total,
                        "wins": r["wins"],
                        "losses": r["losses"],
                        "net_pnl_cents": r["net_pnl_cents"],
                        "win_rate": round(r["wins"] / total, 4) if total > 0 else 0.0,
                    })
            # Append merged stages
            for stage, m in _merged.items():
                total = m["total"]
                if total > 0:
                    by_stage.append({
                        "stage": stage,
                        "total": total,
                        "wins": m["wins"],
                        "losses": m["losses"],
                        "net_pnl_cents": m["net_pnl_cents"],
                        "win_rate": round(m["wins"] / total, 4),
                    })

            # By price bucket
            bucket_rows = conn.execute(
                "SELECT "
                "  CASE "
                "    WHEN market_price BETWEEN 80 AND 84 THEN '80-84' "
                "    WHEN market_price BETWEEN 85 AND 89 THEN '85-89' "
                "    WHEN market_price BETWEEN 90 AND 94 THEN '90-94' "
                "    WHEN market_price BETWEEN 95 AND 99 THEN '95-99' "
                "    ELSE 'other' "
                "  END AS bucket, "
                "  COUNT(*) AS total, "
                "  COUNT(CASE WHEN counterfactual_pnl > 0 THEN 1 END) AS wins, "
                "  COALESCE(SUM(counterfactual_pnl), 0) AS net_pnl_cents "
                "FROM evaluated_opportunities "
                f"WHERE status = 'settled' AND counterfactual_pnl IS NOT NULL {_cf_pt_filter} {_cf_regime_filter} "
                "  AND market_price BETWEEN 80 AND 99 "
                "GROUP BY bucket"
            ).fetchall()
            # Breakeven WR = price / 100 (maker fee = $0)
            # Midpoints: 82c→82%, 87c→87%, 92c→92%, 97c→97%
            breakeven_map = {"80-84": 82, "85-89": 87, "90-94": 92, "95-99": 97}
            by_bucket = []
            for r in bucket_rows:
                total = r["total"]
                wr = round(r["wins"] / total, 4) if total > 0 else 0.0
                be = breakeven_map.get(r["bucket"], 0)
                by_bucket.append({
                    "bucket": r["bucket"],
                    "total": total,
                    "wins": r["wins"],
                    "net_pnl_cents": r["net_pnl_cents"],
                    "win_rate": wr,
                    "breakeven_wr": be,
                    "above_breakeven": wr * 100 >= be,
                })

            # Summary: money left on table vs bullets dodged
            summary_row = conn.execute(
                "SELECT "
                "  COALESCE(SUM(CASE WHEN counterfactual_pnl > 0 AND filter_stage != 'observation_trade' "
                "    THEN counterfactual_pnl ELSE 0 END), 0) AS money_left, "
                "  COALESCE(SUM(CASE WHEN counterfactual_pnl < 0 AND filter_stage != 'observation_trade' "
                "    THEN ABS(counterfactual_pnl) ELSE 0 END), 0) AS bullets_dodged "
                "FROM evaluated_opportunities "
                f"WHERE status = 'settled' AND counterfactual_pnl IS NOT NULL {_cf_pt_filter} {_cf_regime_filter}"
            ).fetchone()

            snap["counterfactual_analysis"] = {
                "by_stage": by_stage,
                "by_bucket": by_bucket,
                "money_left_on_table_cents": summary_row["money_left"],
                "bullets_dodged_cents": summary_row["bullets_dodged"],
                "net_filter_value_cents": summary_row["bullets_dodged"] - summary_row["money_left"],
            }
        except Exception:
            snap["counterfactual_analysis"] = None

        # ── shadow variants (hourly counterfactual configs) ───────────
        try:
            conn = _conn
            _sv_fee = 0.0  # maker fee = $0 (Kalshi charges nothing on maker fills)
            # BTC_P>=70_wl2: BTC only, price >= 70c, max 2 positions per window
            # Uses SQL window function to apply per-window position limit
            sv_rows = conn.execute("""
                WITH ranked AS (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY event_ticker ORDER BY market_price DESC
                    ) AS rn
                    FROM evaluated_opportunities
                    WHERE product_type = 'hourly'
                      AND filter_stage = 'hourly_observation'
                      AND asset = 'BTC'
                      AND market_price >= 70
                      AND market_result IS NOT NULL
                )
                SELECT market_price, market_result, evaluation_time
                FROM ranked WHERE rn <= 2
                ORDER BY evaluation_time
            """).fetchall()
            if sv_rows:
                sv_wins = sum(1 for r in sv_rows if r["market_result"] == "yes")
                sv_n = len(sv_rows)
                sv_losses = sv_n - sv_wins
                sv_wr = round(sv_wins / sv_n, 4) if sv_n > 0 else 0
                # Simulated PnL (maker fees, 1 lot)
                sv_pnl = 0
                for r in sv_rows:
                    p = int(r["market_price"])
                    fee = math.ceil(_sv_fee * p * (100 - p) / 100)
                    if r["market_result"] == "yes":
                        sv_pnl += (100 - p - fee)
                    else:
                        sv_pnl += -(p + fee)
                snap["shadow_variants"] = {
                    "btc_p70_wl2": {
                        "label": "BTC P>=70 wl2",
                        "description": "BTC only, price >= 70c, max 2 per window",
                        "n": sv_n,
                        "wins": sv_wins,
                        "losses": sv_losses,
                        "win_rate": sv_wr,
                        "sim_pnl_cents": sv_pnl,
                    },
                }
            else:
                snap["shadow_variants"] = {}
        except Exception:
            snap["shadow_variants"] = None
            logging.warning("Snapshot: shadow_variants build failed", exc_info=True)

        # ── ask price distribution ────────────────────────────────────
        try:
            scanner = self._ml.scanner
            opps = list(scanner._recent_opportunities)
            buckets = {"<80": 0, "80-84": 0, "85-89": 0, "90-92": 0, "93-96": 0, "97-99": 0, "100+": 0}
            sweet_spot = 0
            for opp in opps:
                ask = opp.get("best_ask")
                if ask is None:
                    continue
                if ask < 80:
                    buckets["<80"] += 1
                elif ask <= 84:
                    buckets["80-84"] += 1
                    sweet_spot += 1
                elif ask <= 89:
                    buckets["85-89"] += 1
                    sweet_spot += 1
                elif ask <= 92:
                    buckets["90-92"] += 1
                    sweet_spot += 1
                elif ask <= 96:
                    buckets["93-96"] += 1
                elif ask <= 99:
                    buckets["97-99"] += 1
                else:
                    buckets["100+"] += 1
            sample = len(opps)
            snap["ask_distribution"] = {
                "sample_size": sample,
                "buckets": buckets,
                "sweet_spot_count": sweet_spot,
                "sweet_spot_pct": round(sweet_spot / sample * 100, 1) if sample > 0 else 0.0,
            }
        except Exception:
            snap["ask_distribution"] = None

        # ── Kalshi order flow tracking ────────────────────────────────
        try:
            koft = getattr(self._ml, "kalshi_oft", None)
            if koft:
                import bot.runtime_config as _bot_mod  # Bit 9.3-iii.c (2026-05-11): bot.runtime_config is a PEP 562 __getattr__ dual-probe of bot.constants then bot.config — preserves getattr-fallback semantics + mutation freshness for runtime-mutable kill-switch flags. See bot/runtime_config.py.
                koft_data = {
                    "shadow_mode": getattr(_bot_mod, "KALSHI_OFT_SHADOW_MODE", True),
                    "tracked_tickers": koft.get_tracked_count(),
                }
                # Per-ticker signals (skip hourly — too many strikes)
                _hourly_pfx = ("KXBTCD", "KXETHD", "KXSOLD", "KXXRPD", "KXHYPED", "KXDOGED")
                per_ticker = {}
                for ticker in list(koft._snapshots.keys()):
                    if ticker.startswith(_hourly_pfx):
                        continue
                    try:
                        sigs = koft.get_signals(ticker)
                        if sigs:
                            per_ticker[ticker] = sigs
                    except Exception:
                        pass
                if per_ticker:
                    koft_data["signals"] = per_ticker
                snap["kalshi_order_flow"] = koft_data
        except Exception:
            logging.warning("Snapshot: kalshi_oft build failed", exc_info=True)

        # ── Execution engine capabilities ────────────────────────────────
        try:
            exec_eng = {}

            # WebSocket status
            kf = getattr(self._ml, "kalshi_feed", None)
            if kf:
                exec_eng["kalshi_ws_connected"] = kf.is_connected
                exec_eng["kalshi_ws_subscribed_tickers"] = (
                    kf.get_subscribed_count() if hasattr(kf, 'get_subscribed_count') else 0)
                exec_eng["kalshi_ws_orderbooks_cached"] = (
                    kf.get_cached_ob_count() if hasattr(kf, 'get_cached_ob_count') else 0)
            else:
                exec_eng["kalshi_ws_connected"] = False

            # Active order execution details
            order = self._ml.executor._active_order
            if order:
                exec_eng["active_order_queue_position"] = order.get("queue_position")
                exec_eng["active_order_execution_method"] = order.get("execution_method", "legacy")

            # Execution stats from OrderExecutor
            ex = self._ml.executor
            exec_eng["session_amend_attempts"] = getattr(ex, "_session_amend_attempts", 0)
            exec_eng["session_amend_successes"] = getattr(ex, "_session_amend_successes", 0)
            exec_eng["session_ioc_fills"] = getattr(ex, "_session_ioc_fills", 0)
            exec_eng["session_ioc_unfilled"] = getattr(ex, "_session_ioc_unfilled", 0)
            exec_eng["session_ws_fills"] = getattr(ex, "_session_ws_fills", 0)
            exec_eng["session_rest_fills"] = getattr(ex, "_session_rest_fills", 0)
            exec_eng["session_post_only_rejections"] = getattr(ex, "_session_post_only_rejections", 0)
            exec_eng["session_post_only_degraded"] = getattr(ex, "_session_post_only_degraded_attempts", 0)
            exec_eng["session_post_only_taker_escalations"] = getattr(ex, "_session_post_only_taker_escalations", 0)
            exec_eng["session_post_only_taker_fills"] = getattr(ex, "_session_post_only_taker_fills", 0)
            # Direct taker counters
            exec_eng["session_direct_taker_attempts"] = getattr(ex, "_session_direct_taker_attempts", 0)
            exec_eng["session_direct_taker_fills"] = getattr(ex, "_session_direct_taker_fills", 0)
            exec_eng["session_direct_taker_unfilled"] = getattr(ex, "_session_direct_taker_unfilled", 0)
            exec_eng["session_direct_taker_skipped"] = getattr(ex, "_session_direct_taker_skipped", 0)
            # Confirmation addon counters
            exec_eng["session_addon_attempts"] = getattr(ex, "_session_addon_attempts", 0)
            exec_eng["session_addon_fills"] = getattr(ex, "_session_addon_fills", 0)
            exec_eng["session_addon_unfilled"] = getattr(ex, "_session_addon_unfilled", 0)
            exec_eng["session_addon_skipped"] = getattr(ex, "_session_addon_skipped", 0)
            # Dip addon counters
            exec_eng["dip_addon_shadow"] = getattr(ex, "_session_dip_addon_shadow", 0)
            exec_eng["dip_addon_attempts"] = getattr(ex, "_session_dip_addon_attempts", 0)
            exec_eng["dip_addon_fills"] = getattr(ex, "_session_dip_addon_fills", 0)
            exec_eng["dip_addon_skipped"] = getattr(ex, "_session_dip_addon_skipped", 0)
            # Order suppression tracking
            exec_eng["order_suppressions"] = {
                "asset_lock": getattr(ex, "_session_suppressed_asset_lock", 0),
                "ticker_cooldown": getattr(ex, "_session_suppressed_ticker_cooldown", 0),
                "no_asks": getattr(ex, "_session_suppressed_no_asks", 0),
                "edge_recalc": getattr(ex, "_session_suppressed_edge_recalc", 0),
                "zero_size": getattr(ex, "_session_suppressed_zero_size", 0),
            }
            exec_eng["session_ioc_retries"] = getattr(ex, "_session_ioc_retries", 0)
            exec_eng["session_ioc_retry_fills"] = getattr(ex, "_session_ioc_retry_fills", 0)
            exec_eng["nbbo_fallback_attempts"] = getattr(ex, "_session_nbbo_fallback_attempts", 0)
            exec_eng["nbbo_fallback_blocked"] = getattr(ex, "_session_nbbo_fallback_blocked", 0)

            # Escalation funnel
            po_rej = exec_eng.get("session_post_only_rejections", 0)
            po_deg = exec_eng.get("session_post_only_degraded", 0)
            po_esc = exec_eng.get("session_post_only_taker_escalations", 0)
            po_fill = exec_eng.get("session_post_only_taker_fills", 0)
            exec_eng["escalation_funnel"] = {
                "rejections": po_rej,
                "degraded_attempts": po_deg,
                "taker_escalations": po_esc,
                "taker_fills": po_fill,
                "taker_fill_rate": round(po_fill / po_esc, 3) if po_esc > 0 else None,
            }

            # Entry path distribution
            entry_path_dist = {}
            ml = self._ml
            entry_path_dist["maker"] = getattr(ml, "_session_maker_fills", 0)
            entry_path_dist["direct_taker"] = getattr(ex, "_session_direct_taker_fills", 0)
            entry_path_dist["post_only_taker"] = getattr(ex, "_session_post_only_taker_fills", 0)
            entry_path_dist["confirmation_addon"] = getattr(ex, "_session_addon_fills", 0)
            entry_path_dist["dip_addon"] = getattr(ex, "_session_dip_addon_fills", 0)
            esc_ioc = max(0, getattr(ex, "_session_ioc_fills", 0)
                          - entry_path_dist["direct_taker"]
                          - entry_path_dist["post_only_taker"])
            entry_path_dist["escalation_ioc"] = esc_ioc
            exec_eng["entry_path_distribution"] = entry_path_dist

            # Derived rates
            dt_att = exec_eng.get("session_direct_taker_attempts", 0)
            exec_eng["direct_taker_fill_rate"] = round(
                exec_eng.get("session_direct_taker_fills", 0) / dt_att, 3
            ) if dt_att > 0 else None
            total_fills = exec_eng["session_ws_fills"] + exec_eng["session_rest_fills"]
            exec_eng["ws_fill_ratio"] = round(
                exec_eng["session_ws_fills"] / total_fills, 3
            ) if total_fills > 0 else None

            # Strategy distribution from scanner
            scanner = getattr(self._ml, "scanner", None)
            strat_counts = getattr(scanner, "_session_strategy_counts", None)
            if strat_counts:
                exec_eng["strategy_distribution"] = dict(strat_counts)

            # ── Health alerts ──────────────────────────────────────────
            alerts = []
            ws_f = exec_eng.get("session_ws_fills", 0)
            rest_f = exec_eng.get("session_rest_fills", 0)
            ioc_f = exec_eng.get("session_ioc_fills", 0)
            ioc_u = exec_eng.get("session_ioc_unfilled", 0)

            # WS fill detection may be broken
            if rest_f >= 3 and ws_f == 0:
                alerts.append("WS fill detection may be broken (0 WS fills, REST taking over)")

            # IOC taker path may be broken — only alert if no overall fills
            # (IOC unfills are normal market friction when liquidity is thin)
            total_fills_so_far = ws_f + rest_f
            if ioc_u >= 3 and ioc_f == 0 and total_fills_so_far == 0:
                alerts.append(f"IOC taker path failing ({ioc_u} unfilled, 0 fills)")

            # Post-only rejection storm
            if po_rej >= 10 and po_fill == 0:
                alerts.append(f"Post-only rejection storm ({po_rej} rejects, 0 taker fills)")

            # Direct taker path may be broken — only alert if no overall fills
            # (Direct taker unfills are normal when spread widens before fill)
            dt_att = exec_eng.get("session_direct_taker_attempts", 0)
            dt_fill = exec_eng.get("session_direct_taker_fills", 0)
            if dt_att >= 3 and dt_fill == 0 and total_fills_so_far == 0:
                alerts.append(f"Direct taker path failing ({dt_att} attempts, 0 fills)")

            # No fills at all despite orders (Change 14: exclude hourly obs candidates)
            total_orders = getattr(scanner, "_session_total_candidates", 0)
            hourly_obs_cand = getattr(scanner, "_session_hourly_obs_candidates", 0) or 0
            candidates_15m = max(0, total_orders - hourly_obs_cand)
            exec_eng["candidates_15m"] = candidates_15m
            exec_eng["candidates_hourly"] = hourly_obs_cand
            if hourly_obs_cand == 0 and total_orders > 0:
                # Scanner doesn't track hourly separately; check if hourly obs is active
                try:
                    import bot.runtime_config as _bot_mod  # Bit 9.3-iii.c (2026-05-11): bot.runtime_config is a PEP 562 __getattr__ dual-probe of bot.constants then bot.config — preserves getattr-fallback semantics + mutation freshness for runtime-mutable kill-switch flags. See bot/runtime_config.py.
                    if getattr(_bot_mod, "HOURLY_OBSERVATION_ENABLED", False) and \
                       getattr(_bot_mod, "HOURLY_OBSERVATION_ONLY", True):
                        # Can't distinguish 15m vs hourly candidates, suppress this alert
                        candidates_15m = 0
                        exec_eng["candidates_15m"] = 0
                        exec_eng["candidates_hourly"] = total_orders
                except Exception:
                    pass
            if candidates_15m >= 3 and (ws_f + rest_f) == 0:
                alerts.append(f"No fills despite {candidates_15m} 15M candidates — check execution")

            exec_eng["health_alerts"] = alerts
            exec_eng["health_ok"] = len(alerts) == 0

            snap["execution_engine"] = exec_eng
        except Exception:
            logging.warning("Snapshot: execution_engine build failed", exc_info=True)
            snap["execution_engine"] = {}

        # ── Shadow calibration pipeline ─────────────────────────────────
        try:
            import bot.runtime_config as _bot_mod  # Bit 9.3-iii.c (2026-05-11): bot.runtime_config is a PEP 562 __getattr__ dual-probe of bot.constants then bot.config — preserves getattr-fallback semantics + mutation freshness for runtime-mutable kill-switch flags. See bot/runtime_config.py.
            cal_engine = getattr(self._ml, "calibration", None)
            if cal_engine:
                snap["shadow_cal_pipeline"] = {
                    "shadow_mode": getattr(_bot_mod, "SHADOW_CAL_PIPELINE", False),
                    "temperature": getattr(cal_engine, '_temperature', None),
                    "temperature_brier": getattr(cal_engine, '_temperature_brier', None),
                    "production_brier": cal_engine.rolling_brier_score(),
                    "production_method": cal_engine.active_method,
                    "blend_w_shadow": getattr(_bot_mod, 'SHADOW_BLEND_W', None),
                    "blend_w_production": getattr(_bot_mod, 'MARKET_BLEND_W', None),
                }
        except Exception:
            logging.warning("Snapshot: shadow_cal_pipeline build failed", exc_info=True)

        # ── Hourly observation mode ──────────────────────────────────────
        try:
            import bot.runtime_config as _bot_mod  # Bit 9.3-iii.c (2026-05-11): bot.runtime_config is a PEP 562 __getattr__ dual-probe of bot.constants then bot.config — preserves getattr-fallback semantics + mutation freshness for runtime-mutable kill-switch flags. See bot/runtime_config.py.
            hourly_enabled = getattr(_bot_mod, "HOURLY_OBSERVATION_ENABLED", False)
            if hourly_enabled:
                hourly_data = {
                    "enabled": True,
                    "observation_only": getattr(_bot_mod, "HOURLY_OBSERVATION_ONLY", True),
                    "blend_w": getattr(_bot_mod, "HOURLY_MARKET_BLEND_W", 0.70),
                    "max_stc": getattr(_bot_mod, "HOURLY_MAX_SECONDS_BEFORE_CLOSE", 900),
                    "temperature_t": getattr(_bot_mod, "HOURLY_TEMPERATURE_T", None),
                    "kelly_fraction": getattr(_bot_mod, "HOURLY_KELLY_FRACTION", None),
                    "min_stc_entry": getattr(_bot_mod, "HOURLY_MIN_STC_ENTRY", None),
                    "max_stc_entry": getattr(_bot_mod, "HOURLY_MAX_STC_ENTRY", None),
                    "excluded_assets": list(getattr(_bot_mod, "HOURLY_EXCLUDED_ASSETS", set())),
                    "max_positions_per_window": getattr(_bot_mod, "HOURLY_MAX_POSITIONS_PER_WINDOW", None),
                    "max_window_risk": getattr(_bot_mod, "HOURLY_MAX_WINDOW_RISK", None),
                }
                conn = _conn
                # Settled hourly observations
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM evaluated_opportunities "
                        "WHERE product_type='hourly' AND status='settled'"
                    ).fetchone()
                    hourly_data["settled_count"] = row["cnt"] if row else 0
                    # Directional win: infer side from calibrated_prob vs market_price
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM evaluated_opportunities "
                        "WHERE product_type='hourly' AND status='settled' AND ("
                        "  (calibrated_prob > market_price/100.0 AND market_result='yes') OR "
                        "  (calibrated_prob <= market_price/100.0 AND market_result IN ('no','all_no'))"
                        ") AND calibrated_prob IS NOT NULL AND market_price IS NOT NULL"
                    ).fetchone()
                    hourly_data["settled_wins"] = row["cnt"] if row else 0
                    row = conn.execute(
                        "SELECT AVG(fee_adjusted_edge) AS avg_e FROM evaluated_opportunities "
                        "WHERE product_type='hourly' AND filter_stage='hourly_observation' AND market_price IS NOT NULL"
                    ).fetchone()
                    hourly_data["avg_edge"] = round(row["avg_e"], 6) if row and row["avg_e"] else None
                except Exception:
                    hourly_data["settled_count"] = 0
                    hourly_data["settled_wins"] = 0
                    hourly_data["avg_edge"] = None
                # Pending (unsettled) hourly observations
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM evaluated_opportunities "
                        "WHERE product_type='hourly' AND status='pending'"
                    ).fetchone()
                    hourly_data["pending_count"] = row["cnt"] if row else 0
                except Exception:
                    hourly_data["pending_count"] = 0
                # Filter stage breakdown for hourly
                try:
                    rows = conn.execute(
                        "SELECT filter_stage, COUNT(*) AS cnt FROM evaluated_opportunities "
                        "WHERE product_type='hourly' GROUP BY filter_stage"
                    ).fetchall()
                    hourly_data["filter_stages"] = {
                        r["filter_stage"]: r["cnt"] for r in rows
                    } if rows else {}
                except Exception:
                    hourly_data["filter_stages"] = {}
                # Simulated P&L from hourly observation trades
                # Direction inferred from calibrated_prob vs market_price:
                #   calibrated_prob > market_price/100 → YES side, else NO side
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt, "
                        "SUM(CASE "
                        "  WHEN calibrated_prob > market_price/100.0 AND market_result='yes' THEN 1 "
                        "  WHEN calibrated_prob <= market_price/100.0 AND market_result IN ('no','all_no') THEN 1 "
                        "  ELSE 0 END) AS wins, "
                        "SUM(CASE "
                        "  WHEN calibrated_prob > market_price/100.0 AND market_result='yes' "
                        f"    THEN (100 - market_price) - CAST(CEIL({SIM_FEE_RATE} * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER) "
                        "  WHEN calibrated_prob > market_price/100.0 AND market_result IN ('no','all_no') "
                        f"    THEN -(market_price + CAST(CEIL({SIM_FEE_RATE} * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER)) "
                        "  WHEN calibrated_prob <= market_price/100.0 AND market_result IN ('no','all_no') "
                        f"    THEN market_price - CAST(CEIL({SIM_FEE_RATE} * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER) "
                        "  WHEN calibrated_prob <= market_price/100.0 AND market_result='yes' "
                        f"    THEN -((100 - market_price) + CAST(CEIL({SIM_FEE_RATE} * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER)) "
                        "  ELSE 0 END) AS sim_pnl "
                        "FROM evaluated_opportunities "
                        "WHERE product_type='hourly' AND filter_stage='hourly_observation' "
                        "AND status='settled' AND market_result IS NOT NULL "
                        "AND calibrated_prob IS NOT NULL AND market_price IS NOT NULL"
                    ).fetchone()
                    if row and row["cnt"] > 0:
                        hourly_data["sim_trade_count"] = row["cnt"]
                        hourly_data["sim_win_rate"] = round(row["wins"] / row["cnt"], 4) if row["cnt"] > 0 else 0
                        hourly_data["sim_pnl_cents"] = row["sim_pnl"] or 0
                    else:
                        hourly_data["sim_trade_count"] = 0
                        hourly_data["sim_win_rate"] = 0
                        hourly_data["sim_pnl_cents"] = 0
                except Exception:
                    hourly_data["sim_trade_count"] = 0
                    hourly_data["sim_win_rate"] = 0
                    hourly_data["sim_pnl_cents"] = 0
                snap["hourly_observation"] = hourly_data
        except Exception:
            logging.warning("Snapshot: hourly_observation build failed", exc_info=True)

        # ── Hourly Live (BTC+ETH, sub-60c, taker-only) ──
        # Split: new sub-60c strategy (since Mar 23) vs legacy Feb 28 disaster
        _HOURLY_NEW_SINCE = "2026-03-23T18:00:00"
        try:
            # New strategy only
            _hl = _conn.execute(
                "SELECT COUNT(*) as n, "
                "SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as wins, "
                "SUM(pnl_cents) as total_pnl, "
                "SUM(fee_cents) as total_fees "
                "FROM settled_trades WHERE product_type='hourly' AND settled_at >= ?",
                (_HOURLY_NEW_SINCE,)
            ).fetchone()
            _hl_n = (_hl["n"] or 0) if _hl else 0
            _hl_wins = (_hl["wins"] or 0) if _hl else 0
            _hl_pnl = (_hl["total_pnl"] or 0) if _hl else 0
            _hl_fees = (_hl["total_fees"] or 0) if _hl else 0
            # Legacy
            _hl_leg = _conn.execute(
                "SELECT COUNT(*) as n, SUM(pnl_cents) as pnl "
                "FROM settled_trades WHERE product_type='hourly' AND settled_at < ?",
                (_HOURLY_NEW_SINCE,)
            ).fetchone()
            # Per-asset (new strategy only)
            _hl_assets = {}
            for _hla_row in _conn.execute(
                "SELECT asset, COUNT(*) as n, "
                "SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as wins, "
                "SUM(pnl_cents) as pnl "
                "FROM settled_trades WHERE product_type='hourly' AND settled_at >= ? GROUP BY asset",
                (_HOURLY_NEW_SINCE,)
            ).fetchall():
                _hla = _hla_row["asset"]
                _hla_n = _hla_row["n"] or 0
                _hla_w = _hla_row["wins"] or 0
                _hl_assets[_hla] = {
                    "trades": _hla_n, "wins": _hla_w,
                    "wr": round(_hla_w / _hla_n, 4) if _hla_n else 0,
                    "pnl_cents": _hla_row["pnl"] or 0,
                }
            snap["hourly_live"] = {
                "enabled": os.environ.get("HOURLY_LIVE_ENABLED", "0") == "1",
                "trades": _hl_n,
                "wins": _hl_wins,
                "losses": _hl_n - _hl_wins,
                "wr": round(_hl_wins / _hl_n, 4) if _hl_n else 0,
                "pnl_cents": _hl_pnl,
                "fees_cents": _hl_fees,
                "legacy_trades": (_hl_leg["n"] or 0) if _hl_leg else 0,
                "legacy_pnl_cents": (_hl_leg["pnl"] or 0) if _hl_leg else 0,
                "per_asset": _hl_assets,
                "config": {
                    "max_entry_price": 59,
                    "excluded_assets": ["SOL", "XRP"],
                    "fixed_contracts": 25,
                    "bankroll_fraction": 0.10,
                    "max_edge": 0.05,
                    "stc_range": [600, 1800],
                    "taker_only": True,
                },
            }
        except Exception:
            logging.warning("Snapshot: hourly_live build failed", exc_info=True)

        # ── Daily PnL History (last 30 days, all products) ──
        try:
            _daily_rows = _conn.execute(
                "SELECT DATE(settled_at) as day, "
                "SUM(pnl_cents - fee_cents) as net_pnl, "
                "COUNT(*) as trades, "
                "SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN pnl_cents <= 0 THEN 1 ELSE 0 END) as losses "
                "FROM settled_trades GROUP BY day ORDER BY day DESC LIMIT 30"
            ).fetchall()
            snap["daily_pnl_history"] = [
                {"date": r["day"], "pnl_cents": r["net_pnl"] or 0,
                 "trades": r["trades"], "wins": r["wins"] or 0, "losses": r["losses"] or 0}
                for r in reversed(_daily_rows)
            ]
        except Exception:
            snap["daily_pnl_history"] = []
            logging.warning("Snapshot: daily_pnl_history failed", exc_info=True)

        # ── DC By Tier ──
        try:
            _dc_tiers = {}
            for _dc_row in _conn.execute(
                "SELECT strategy, COUNT(*) as n, "
                "SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as wins, "
                "SUM(pnl_cents - fee_cents) as net_pnl "
                "FROM settled_trades WHERE strategy LIKE 'decided_%' GROUP BY strategy"
            ).fetchall():
                _dc_tiers[_dc_row["strategy"]] = {
                    "fills": _dc_row["n"],
                    "wins": _dc_row["wins"] or 0,
                    "wr": round((_dc_row["wins"] or 0) / _dc_row["n"], 4) if _dc_row["n"] else 0,
                    "pnl_cents": _dc_row["net_pnl"] or 0,
                }
            snap["decided_contracts_by_tier"] = _dc_tiers
        except Exception:
            snap["decided_contracts_by_tier"] = {}
            logging.warning("Snapshot: dc_by_tier failed", exc_info=True)

        # ── Hourly Config A (no_XRP + edge ≤ 0.7%) ──────────────────────
        try:
            _ca = _conn.execute(
                "SELECT COUNT(*) as n, "
                "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes') "
                "  THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl, "
                "AVG(CASE WHEN status='settled' AND calibrated_prob IS NOT NULL "
                "  THEN (calibrated_prob - CASE WHEN market_result IN ('yes','all_yes') "
                "    THEN 1.0 ELSE 0.0 END) * (calibrated_prob - CASE WHEN market_result IN ('yes','all_yes') "
                "    THEN 1.0 ELSE 0.0 END) END) as brier "
                "FROM evaluated_opportunities "
                "WHERE product_type='hourly' AND filter_stage='hourly_config_a'"
            ).fetchone()
            _ca_by_day = _conn.execute(
                "SELECT date(evaluation_time) as day, COUNT(*) as n, "
                "SUM(CASE WHEN market_result IN ('yes','all_yes') THEN 1 ELSE 0 END) as wins "
                "FROM evaluated_opportunities "
                "WHERE product_type='hourly' AND filter_stage='hourly_config_a' AND status='settled' "
                "GROUP BY day ORDER BY day"
            ).fetchall()
            _ca_min_day_wr = None
            _ca_days = 0
            for _d in _ca_by_day:
                _ca_days += 1
                _dwr = (_d["wins"] or 0) / _d["n"] if _d["n"] else 0
                if _ca_min_day_wr is None or _dwr < _ca_min_day_wr:
                    _ca_min_day_wr = _dwr
            _ca_settled = (_ca["settled"] or 0) if _ca else 0
            _ca_wins = (_ca["wins"] or 0) if _ca else 0
            _ca_wr = round(_ca_wins / _ca_settled, 4) if _ca_settled else 0
            # Wilson lower bound
            _ca_wlo = 0
            if _ca_settled > 0:
                _p = _ca_wins / _ca_settled
                _z = 1.96
                _d = 1 + _z**2 / _ca_settled
                _ca_wlo = round((_p + _z**2 / (2 * _ca_settled) - _z * ((_p * (1 - _p) / _ca_settled + _z**2 / (4 * _ca_settled**2)) ** 0.5)) / _d, 4)
            snap["hourly_config_a"] = {
                "total_signals": _ca["n"] if _ca else 0,
                "settled": _ca_settled,
                "wins": _ca_wins,
                "wr": _ca_wr,
                "wilson_lower": _ca_wlo,
                "brier": round(_ca["brier"], 4) if _ca and _ca["brier"] else None,
                "sim_pnl_cents": _ca["sim_pnl"] if _ca else 0,
                "days": _ca_days,
                "min_day_wr": round(_ca_min_day_wr, 4) if _ca_min_day_wr is not None else None,
                "filters": {"excluded_assets": ["XRP"], "max_edge": 0.007},
                "graduation": {
                    "days_required": 7,
                    "min_wr": 0.78,
                    "min_wilson_lower": 0.72,
                    "max_brier": 0.25,
                    "min_day_wr": 0.60,
                    "pnl_positive": True,
                },
            }
        except Exception:
            logging.warning("hourly_config_a snapshot failed", exc_info=True)
            snap["hourly_config_a"] = {"total_signals": 0, "settled": 0, "wins": 0, "wr": 0,
                                        "wilson_lower": 0, "brier": None, "sim_pnl_cents": 0,
                                        "days": 0, "min_day_wr": None,
                                        "filters": {"excluded_assets": ["XRP"], "max_edge": 0.007},
                                        "graduation": {"days_required": 7, "min_wr": 0.78,
                                                       "min_wilson_lower": 0.72, "max_brier": 0.25,
                                                       "min_day_wr": 0.60, "pnl_positive": True}}

        # ── Config B: BTC 70-89c wl2 (promotion candidate) ────────────────
        try:
            _cb = _conn.execute(
                "SELECT COUNT(*) as n, "
                "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "SUM(CASE WHEN status='settled' AND "
                "  ((side='yes' AND market_result IN ('yes','all_yes')) OR "
                "   (side='no' AND market_result IN ('no','all_no'))) THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl, "
                "AVG(CASE WHEN status='settled' AND calibrated_prob IS NOT NULL "
                "  THEN (calibrated_prob - CASE WHEN market_result IN ('yes','all_yes') "
                "    THEN 1.0 ELSE 0.0 END) * (calibrated_prob - CASE WHEN market_result IN ('yes','all_yes') "
                "    THEN 1.0 ELSE 0.0 END) END) as brier "
                "FROM evaluated_opportunities "
                "WHERE product_type='hourly' AND filter_stage='hourly_config_b'"
            ).fetchone()
            _cb_by_day = _conn.execute(
                "SELECT date(evaluation_time) as day, COUNT(*) as n, "
                "SUM(CASE WHEN (side='yes' AND market_result IN ('yes','all_yes')) OR "
                "  (side='no' AND market_result IN ('no','all_no')) THEN 1 ELSE 0 END) as wins "
                "FROM evaluated_opportunities "
                "WHERE product_type='hourly' AND filter_stage='hourly_config_b' AND status='settled' "
                "GROUP BY day ORDER BY day"
            ).fetchall()
            _cb_min_day_wr = None
            _cb_days = 0
            for _d in _cb_by_day:
                _cb_days += 1
                _dwr = (_d["wins"] or 0) / _d["n"] if _d["n"] else 0
                if _cb_min_day_wr is None or _dwr < _cb_min_day_wr:
                    _cb_min_day_wr = _dwr
            _cb_settled = (_cb["settled"] or 0) if _cb else 0
            _cb_wins = (_cb["wins"] or 0) if _cb else 0
            _cb_wr = round(_cb_wins / _cb_settled, 4) if _cb_settled else 0
            _cb_wlo = 0
            if _cb_settled > 0:
                _p = _cb_wins / _cb_settled
                _z = 1.96
                _d = 1 + _z**2 / _cb_settled
                _cb_wlo = round((_p + _z**2 / (2 * _cb_settled) - _z * ((_p * (1 - _p) / _cb_settled + _z**2 / (4 * _cb_settled**2)) ** 0.5)) / _d, 4)
            # Breakeven WR for avg price in 70-89c range (~81c)
            _cb_avg_price = 81
            _cb_be_wr = round((_cb_avg_price + 2) / 100, 4)  # ~83% for avg 81c + ~2c fee
            snap["hourly_config_b"] = {
                "total_signals": _cb["n"] if _cb else 0,
                "settled": _cb_settled,
                "wins": _cb_wins,
                "wr": _cb_wr,
                "wilson_lower": _cb_wlo,
                "brier": round(_cb["brier"], 4) if _cb and _cb["brier"] else None,
                "sim_pnl_cents": _cb["sim_pnl"] if _cb else 0,
                "days": _cb_days,
                "min_day_wr": round(_cb_min_day_wr, 4) if _cb_min_day_wr is not None else None,
                "breakeven_wr": _cb_be_wr,
                "wilson_margin_over_be": round(_cb_wlo - _cb_be_wr, 4) if _cb_wlo else 0,
                "filters": {"asset": "BTC", "min_price": 70, "max_price": 89, "max_per_window": 2},
                "graduation": {
                    "min_n": 100,
                    "min_wr": 0.88,
                    "min_wilson_margin_over_be": 0.03,
                    "pnl_positive": True,
                },
            }
        except Exception:
            logging.warning("hourly_config_b snapshot failed", exc_info=True)
            snap["hourly_config_b"] = {"total_signals": 0, "settled": 0, "wins": 0, "wr": 0,
                                        "wilson_lower": 0, "brier": None, "sim_pnl_cents": 0,
                                        "days": 0, "min_day_wr": None, "breakeven_wr": 0.83,
                                        "wilson_margin_over_be": 0,
                                        "filters": {"asset": "BTC", "min_price": 70, "max_price": 89, "max_per_window": 2},
                                        "graduation": {"min_n": 100, "min_wr": 0.88,
                                                       "min_wilson_margin_over_be": 0.03, "pnl_positive": True}}

        # ── Hourly Configs C–G (shadow promotion candidates) ─────────────
        for _vfs, _vfilters in _HOURLY_VARIANT_DEFS:
            snap[_vfs] = _build_hourly_variant_snap(_conn, _vfs, _vfilters, _HOURLY_VARIANT_GRADUATION)

        # ── Weather NO-side Shadow ────────────────────────────────────────
        try:
            _wn = _conn.execute(
                "SELECT COUNT(*) as n, "
                "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "SUM(CASE WHEN status='settled' AND market_result IN ('no','all_no') "
                "  THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl, "
                "AVG(CASE WHEN status='settled' AND calibrated_prob IS NOT NULL "
                "  THEN (calibrated_prob - CASE WHEN market_result IN ('no','all_no') "
                "    THEN 1.0 ELSE 0.0 END) * (calibrated_prob - CASE WHEN market_result IN ('no','all_no') "
                "    THEN 1.0 ELSE 0.0 END) END) as brier "
                "FROM evaluated_opportunities "
                "WHERE filter_stage='weather_no_shadow' AND side='no'",
            ).fetchone()
            _wn_settled = (_wn["settled"] or 0) if _wn else 0
            _wn_wins = (_wn["wins"] or 0) if _wn else 0
            _wn_wr = round(_wn_wins / _wn_settled, 4) if _wn_settled else 0
            _wn_wlo = 0
            if _wn_settled > 0:
                _p = _wn_wins / _wn_settled
                _z = 1.96
                _d = 1 + _z**2 / _wn_settled
                _wn_wlo = round((_p + _z**2 / (2 * _wn_settled) - _z * ((_p * (1 - _p) / _wn_settled + _z**2 / (4 * _wn_settled**2)) ** 0.5)) / _d, 4)
            # Per-city breakdown
            _wn_by_city = {}
            for _cr in _conn.execute(
                "SELECT asset, COUNT(*) as n, "
                "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "SUM(CASE WHEN status='settled' AND market_result IN ('no','all_no') THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl "
                "FROM evaluated_opportunities "
                "WHERE filter_stage='weather_no_shadow' AND side='no' "
                "GROUP BY asset ORDER BY asset",
            ).fetchall():
                _wn_by_city[_cr["asset"]] = {
                    "n": _cr["n"], "settled": _cr["settled"] or 0,
                    "wins": _cr["wins"] or 0,
                    "wr": round((_cr["wins"] or 0) / _cr["settled"], 4) if _cr["settled"] else 0,
                    "sim_pnl_cents": _cr["sim_pnl"] or 0,
                }
            # Per YES-probability bucket (model's YES confidence → NO opportunity)
            _wn_by_yes_bucket = {}
            for _br in _conn.execute(
                "SELECT CASE "
                "  WHEN (1.0 - calibrated_prob) < 0.70 THEN '55-70pct' "
                "  WHEN (1.0 - calibrated_prob) < 0.85 THEN '70-85pct' "
                "  ELSE '85pct_plus' END as bucket, "
                "COUNT(*) as n, "
                "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "SUM(CASE WHEN status='settled' AND market_result IN ('no','all_no') THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl "
                "FROM evaluated_opportunities "
                "WHERE filter_stage='weather_no_shadow' AND side='no' "
                "GROUP BY bucket ORDER BY bucket",
            ).fetchall():
                _wn_by_yes_bucket[_br["bucket"]] = {
                    "n": _br["n"], "settled": _br["settled"] or 0,
                    "wins": _br["wins"] or 0,
                    "wr": round((_br["wins"] or 0) / _br["settled"], 4) if _br["settled"] else 0,
                    "sim_pnl_cents": _br["sim_pnl"] or 0,
                }
            snap["weather_no_shadow"] = {
                "total_signals": _wn["n"] if _wn else 0,
                "settled": _wn_settled, "wins": _wn_wins, "wr": _wn_wr,
                "wilson_lower": _wn_wlo,
                "brier": round(_wn["brier"], 4) if _wn and _wn["brier"] else None,
                "sim_pnl_cents": _wn["sim_pnl"] if _wn else 0,
                "by_city": _wn_by_city,
                "by_yes_bucket": _wn_by_yes_bucket,
            }
        except Exception:
            snap["weather_no_shadow"] = {
                "total_signals": 0, "settled": 0, "wins": 0, "wr": 0,
                "wilson_lower": 0, "brier": None, "sim_pnl_cents": 0,
                "by_city": {}, "by_yes_bucket": {},
            }

        # ── Weather NO-side Live Performance ─────────────────────────────
        try:
            _wnl = _conn.execute(
                "SELECT COUNT(*) as n, "
                "SUM(CASE WHEN "
                "  (side='no' AND market_result IN ('no','all_no')) OR "
                "  (side='yes' AND market_result IN ('yes','all_yes')) "
                "  THEN 1 ELSE 0 END) as wins, "
                "SUM(pnl_cents - fee_cents) as pnl "
                "FROM settled_trades "
                "WHERE product_type='weather' AND side='no'",
            ).fetchone()
            _wnl_n = (_wnl["n"] or 0) if _wnl else 0
            _wnl_wins = (_wnl["wins"] or 0) if _wnl else 0
            _wnl_wr = round(_wnl_wins / _wnl_n, 4) if _wnl_n else 0
            _wnl_pnl = (_wnl["pnl"] or 0) if _wnl else 0

            # Per-city breakdown
            _wnl_by_city = {}
            for _cr in _conn.execute(
                "SELECT asset, COUNT(*) as n, "
                "SUM(CASE WHEN "
                "  (side='no' AND market_result IN ('no','all_no')) OR "
                "  (side='yes' AND market_result IN ('yes','all_yes')) "
                "  THEN 1 ELSE 0 END) as wins, "
                "SUM(pnl_cents - fee_cents) as pnl "
                "FROM settled_trades "
                "WHERE product_type='weather' AND side='no' "
                "GROUP BY asset ORDER BY asset",
            ).fetchall():
                _wnl_by_city[_cr["asset"]] = {
                    "n": _cr["n"],
                    "wins": _cr["wins"] or 0,
                    "wr": round((_cr["wins"] or 0) / _cr["n"], 4) if _cr["n"] else 0,
                    "pnl_cents": _cr["pnl"] or 0,
                }

            # Per YES-probability bucket (calibrated_prob is NO-prob; YES = 1 - calibrated_prob)
            _wnl_by_bucket = {}
            for _br in _conn.execute(
                "SELECT CASE "
                "  WHEN (1.0 - calibrated_prob) < 0.70 THEN '55-70pct' "
                "  WHEN (1.0 - calibrated_prob) < 0.85 THEN '70-85pct' "
                "  ELSE '85pct_plus' END as bucket, "
                "COUNT(*) as n, "
                "SUM(CASE WHEN "
                "  (side='no' AND market_result IN ('no','all_no')) OR "
                "  (side='yes' AND market_result IN ('yes','all_yes')) "
                "  THEN 1 ELSE 0 END) as wins, "
                "SUM(pnl_cents - fee_cents) as pnl "
                "FROM settled_trades "
                "WHERE product_type='weather' AND side='no' "
                "  AND calibrated_prob IS NOT NULL "
                "GROUP BY bucket ORDER BY bucket",
            ).fetchall():
                _wnl_by_bucket[_br["bucket"]] = {
                    "n": _br["n"],
                    "wins": _br["wins"] or 0,
                    "wr": round((_br["wins"] or 0) / _br["n"], 4) if _br["n"] else 0,
                    "pnl_cents": _br["pnl"] or 0,
                }

            snap["weather_no_live"] = {
                "trades": _wnl_n, "wins": _wnl_wins, "wr": _wnl_wr,
                "pnl_cents": _wnl_pnl,
                "by_city": _wnl_by_city,
                "by_yes_bucket": _wnl_by_bucket,
                "live": getattr(_bot_mod, "WEATHER_NO_SIDE_LIVE", False),
            }
        except Exception:
            snap["weather_no_live"] = {
                "trades": 0, "wins": 0, "wr": 0, "pnl_cents": 0,
                "by_city": {}, "by_yes_bucket": {},
                "live": getattr(_bot_mod, "WEATHER_NO_SIDE_LIVE", False),
            }

        # ── SPX Observation Panel ─────────────────────────────────────────
        try:
            if getattr(_bot_mod, "SPX_HOURLY_ENABLED", False):
                spx_data = {
                    "enabled": True,
                    "observation_only": getattr(_bot_mod, "SPX_HOURLY_OBSERVATION_ONLY", True),
                    "min_entry_price": getattr(_bot_mod, "SPX_HOURLY_MIN_ENTRY_PRICE", None),
                    "market_blend_w": getattr(_bot_mod, "SPX_HOURLY_MARKET_BLEND_W", None),
                    "max_risk_per_trade": getattr(_bot_mod, "SPX_HOURLY_MAX_RISK_PER_TRADE", None),
                    "kelly_fraction": getattr(_bot_mod, "SPX_HOURLY_KELLY_FRACTION", None),
                    "fee_multiplier_taker": getattr(_bot_mod, "SPX_HOURLY_FEE_MULTIPLIER_TAKER", None),
                    "bankroll_fraction": getattr(_bot_mod, "SPX_HOURLY_BANKROLL_FRACTION", None),
                }
                spx_eng = getattr(self._ml, "spx_engine", None)
                spx_data["market_open"] = spx_eng.is_market_open() if spx_eng else False
                if spx_eng:
                    spx_data["spx_price"] = spx_eng.get_spot_price("SPX")
                    spx_data["vix_level"] = spx_eng.get_vix()

                conn = _conn
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM evaluated_opportunities "
                        "WHERE product_type='spx_hourly' AND status='settled'"
                    ).fetchone()
                    spx_data["settled_count"] = row["cnt"] if row else 0
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM evaluated_opportunities "
                        "WHERE product_type='spx_hourly' AND status='settled' AND ("
                        "  (calibrated_prob > market_price/100.0 AND market_result='yes') OR "
                        "  (calibrated_prob <= market_price/100.0 AND market_result IN ('no','all_no'))"
                        ") AND calibrated_prob IS NOT NULL AND market_price IS NOT NULL"
                    ).fetchone()
                    spx_data["settled_wins"] = row["cnt"] if row else 0
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM evaluated_opportunities "
                        "WHERE product_type='spx_hourly' AND status='pending'"
                    ).fetchone()
                    spx_data["pending_count"] = row["cnt"] if row else 0
                except Exception:
                    spx_data["settled_count"] = 0
                    spx_data["settled_wins"] = 0
                    spx_data["pending_count"] = 0

                # Filter stage breakdown
                try:
                    rows = conn.execute(
                        "SELECT filter_stage, COUNT(*) AS cnt FROM evaluated_opportunities "
                        "WHERE product_type='spx_hourly' GROUP BY filter_stage"
                    ).fetchall()
                    spx_data["filter_stages"] = {
                        r["filter_stage"]: r["cnt"] for r in rows
                    } if rows else {}
                except Exception:
                    spx_data["filter_stages"] = {}

                # Simulated P&L (directional: infer side from calibrated_prob vs market_price)
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt, "
                        "SUM(CASE "
                        "  WHEN calibrated_prob > market_price/100.0 AND market_result='yes' THEN 1 "
                        "  WHEN calibrated_prob <= market_price/100.0 AND market_result IN ('no','all_no') THEN 1 "
                        "  ELSE 0 END) AS wins, "
                        "SUM(CASE "
                        "  WHEN calibrated_prob > market_price/100.0 AND market_result='yes' "
                        f"    THEN (100 - market_price) - CAST(CEIL({SIM_FEE_RATE} * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER) "
                        "  WHEN calibrated_prob > market_price/100.0 AND market_result IN ('no','all_no') "
                        f"    THEN -(market_price + CAST(CEIL({SIM_FEE_RATE} * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER)) "
                        "  WHEN calibrated_prob <= market_price/100.0 AND market_result IN ('no','all_no') "
                        f"    THEN market_price - CAST(CEIL({SIM_FEE_RATE} * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER) "
                        "  WHEN calibrated_prob <= market_price/100.0 AND market_result='yes' "
                        f"    THEN -((100 - market_price) + CAST(CEIL({SIM_FEE_RATE} * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER)) "
                        "  ELSE 0 END) AS sim_pnl "
                        "FROM evaluated_opportunities "
                        "WHERE product_type='spx_hourly' AND filter_stage='spx_observation' "
                        "AND status='settled' AND market_result IS NOT NULL "
                        "AND calibrated_prob IS NOT NULL AND market_price IS NOT NULL"
                    ).fetchone()
                    if row and row["cnt"] > 0:
                        spx_data["sim_trade_count"] = row["cnt"]
                        spx_data["sim_win_rate"] = round(row["wins"] / row["cnt"], 4)
                        spx_data["sim_pnl_cents"] = row["sim_pnl"] or 0
                    else:
                        spx_data["sim_trade_count"] = 0
                        spx_data["sim_win_rate"] = 0
                        spx_data["sim_pnl_cents"] = 0
                except Exception:
                    spx_data["sim_trade_count"] = 0
                    spx_data["sim_win_rate"] = 0
                    spx_data["sim_pnl_cents"] = 0
                snap["spx_observation"] = spx_data
        except Exception:
            logging.warning("Snapshot: spx_observation build failed", exc_info=True)

        # ── SPX Shadow Calibration Variants ──────────────────────────────
        # Five variants exploring temperature + blend + filter combinations
        # using pre-computed shadow columns on spx_observation rows.
        _SPX_VARIANT_DEFS = [
            ("spx_a", "T=2.0 no blend", "hourly_shadow_temp_2_0", None, None),
            ("spx_b", "T=2.5 no blend", "hourly_shadow_temp_2_5", None, None),
            ("spx_c", "T=2.0 90c+", "hourly_shadow_temp_2_0", "market_price >= 90", None),
            ("spx_d", "post_temp no blend", "hourly_post_temp_prob", None, None),
            ("spx_e", "T=2.0 high vol", "hourly_shadow_temp_2_0", "vol_regime = 'high'", None),
        ]
        # SPX finance fee rate: taker 0.035 (half crypto)
        _SPX_FEE = 0.035
        try:
            spx_variants = {}
            for _vid, _vlabel, _prob_col, _extra_filter, _ in _SPX_VARIANT_DEFS:
                try:
                    _where = (
                        f"product_type='spx_hourly' AND filter_stage='spx_observation' "
                        f"AND status='settled' AND market_result IS NOT NULL "
                        f"AND {_prob_col} IS NOT NULL"
                    )
                    if _extra_filter:
                        _where += f" AND {_extra_filter}"
                    _sql = (
                        f"SELECT COUNT(*) as n, "
                        f"SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) as wins, "
                        f"AVG(CASE WHEN market_result='yes' "
                        f"  THEN (1-{_prob_col})*(1-{_prob_col}) "
                        f"  ELSE {_prob_col}*{_prob_col} END) as brier, "
                        f"SUM(CASE WHEN {_prob_col} > market_price/100.0 THEN "
                        f"  CASE WHEN market_result='yes' "
                        f"    THEN (100 - market_price) - CAST(CEIL({_SPX_FEE} * (market_price/100.0) * (1.0 - market_price/100.0) * 100) AS INTEGER) "
                        f"  ELSE -(market_price + CAST(CEIL({_SPX_FEE} * (market_price/100.0) * (1.0 - market_price/100.0) * 100) AS INTEGER)) "
                        f"  END ELSE 0 END) as sim_pnl "
                        f"FROM evaluated_opportunities WHERE {_where}"
                    )
                    _r = _conn.execute(_sql).fetchone()
                    _n = _r["n"] if _r else 0
                    _w = (_r["wins"] or 0) if _r else 0
                    _wr = round(_w / _n, 4) if _n > 0 else 0
                    _wlo = 0.0
                    if _n > 0:
                        _p = _w / _n
                        _z = 1.96
                        _denom = 1 + _z ** 2 / _n
                        _wlo = round((_p + _z ** 2 / (2 * _n) - _z * ((_p * (1 - _p) / _n + _z ** 2 / (4 * _n ** 2)) ** 0.5)) / _denom, 4)
                    # Pending count
                    _pw = (
                        f"product_type='spx_hourly' AND filter_stage='spx_observation' "
                        f"AND status != 'settled' AND {_prob_col} IS NOT NULL"
                    )
                    if _extra_filter:
                        _pw += f" AND {_extra_filter}"
                    _pend = _conn.execute(f"SELECT COUNT(*) as c FROM evaluated_opportunities WHERE {_pw}").fetchone()
                    spx_variants[_vid] = {
                        "label": _vlabel,
                        "settled": _n, "wins": _w, "losses": _n - _w,
                        "wr": _wr, "wilson_lower": _wlo,
                        "brier": round(_r["brier"], 4) if _r and _r["brier"] else None,
                        "sim_pnl_cents": (_r["sim_pnl"] or 0) if _r else 0,
                        "pending": (_pend["c"] or 0) if _pend else 0,
                    }
                except Exception:
                    logging.warning("SPX variant %s failed", _vid, exc_info=True)
                    spx_variants[_vid] = {
                        "label": _vlabel, "settled": 0, "wins": 0, "losses": 0,
                        "wr": 0, "wilson_lower": 0, "brier": None,
                        "sim_pnl_cents": 0, "pending": 0,
                    }
            snap["spx_variants"] = spx_variants
        except Exception:
            logging.warning("Snapshot: spx_variants build failed", exc_info=True)

        # ── SPX Live Trading Performance ───────────────────────────────
        # Real trades from settled_trades WHERE product_type='spx_hourly'
        _SPX_FEE_LIVE = getattr(_bot_mod, "SPX_HOURLY_FEE_MULTIPLIER_TAKER", 0.035)
        try:
            spx_live = {"trades": 0, "wins": 0, "losses": 0, "wr": 0, "pnl_cents": 0, "fee_cents": 0}
            _sl = _conn.execute(
                "SELECT COUNT(*) AS cnt, "
                "SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) AS wins, "
                "SUM(CASE WHEN pnl_cents <= 0 THEN 1 ELSE 0 END) AS losses, "
                "SUM(pnl_cents) AS total_pnl, "
                "SUM(fee_cents) AS total_fees, "
                "AVG(entry_price_cents) AS avg_entry "
                "FROM settled_trades WHERE product_type='spx_hourly'"
            ).fetchone()
            if _sl and _sl["cnt"] > 0:
                spx_live["trades"] = _sl["cnt"]
                spx_live["wins"] = _sl["wins"] or 0
                spx_live["losses"] = _sl["losses"] or 0
                spx_live["wr"] = round(spx_live["wins"] / _sl["cnt"], 4)
                spx_live["pnl_cents"] = _sl["total_pnl"] or 0
                spx_live["fee_cents"] = _sl["total_fees"] or 0
                spx_live["avg_entry_price"] = round(_sl["avg_entry"]) if _sl["avg_entry"] else None

            # Per-price-tier breakdown
            _tiers = _conn.execute(
                "SELECT "
                "CASE WHEN entry_price_cents >= 95 THEN '95-99' "
                "     WHEN entry_price_cents >= 93 THEN '93-94' "
                "     ELSE '90-92' END AS tier, "
                "COUNT(*) AS cnt, "
                "SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) AS wins, "
                "SUM(pnl_cents) AS pnl "
                "FROM settled_trades WHERE product_type='spx_hourly' "
                "GROUP BY tier ORDER BY tier"
            ).fetchall()
            spx_live["by_tier"] = {
                r["tier"]: {"trades": r["cnt"], "wins": r["wins"] or 0,
                            "wr": round((r["wins"] or 0) / r["cnt"], 4),
                            "pnl_cents": r["pnl"] or 0}
                for r in _tiers
            } if _tiers else {}

            # Recent SPX trades
            _recent = _conn.execute(
                "SELECT ticker, entry_price_cents, count, pnl_cents, fee_cents, "
                "settled_at, market_result, side "
                "FROM settled_trades WHERE product_type='spx_hourly' "
                "ORDER BY settled_at DESC LIMIT 5"
            ).fetchall()
            spx_live["recent"] = [dict(r) for r in _recent] if _recent else []

            # Effective bankroll
            _bfrac = getattr(_bot_mod, "SPX_HOURLY_BANKROLL_FRACTION", 0.15)
            spx_live["bankroll_fraction"] = _bfrac

            snap["spx_live"] = spx_live
        except Exception:
            logging.warning("Snapshot: spx_live build failed", exc_info=True)
            snap["spx_live"] = {"trades": 0, "wins": 0, "losses": 0, "wr": 0, "pnl_cents": 0, "fee_cents": 0}

        # ── Hourly NO-Side Overconfidence Tracker ────────────────────────
        # The hourly model is massively overconfident on YES → actual NO rate
        # far exceeds breakeven at cheap NO prices.  Data comes from YES-side
        # evals (side='yes') where we check how often market_result='no'.
        # NO-side shadow (side='no') can't capture this because the model's
        # low NO prob means it sees negative edge on exactly the best signals.
        _HNO_ASSETS = ["XRP", "SOL"]
        _HNO_MIN_YES_PRED = {"XRP": 0.87, "SOL": 0.90}
        _HNO_MAX_YES_PRICE = 97  # NO price >= 3c — below this, NO contracts don't exist
        _HNO_BUCKETS = [
            ("87-90%", 0.87, 0.90),
            ("90-93%", 0.90, 0.93),
            ("93-95%", 0.93, 0.95),
            ("95-97%", 0.95, 0.97),
            ("97%+",   0.97, 1.01),
        ]
        _HNO_FEE = 0.07  # crypto taker
        try:
            hno = {}
            for _ha in _HNO_ASSETS:
                _min_pred = _HNO_MIN_YES_PRED[_ha]
                # Overall asset summary
                _sql_all = (
                    "SELECT COUNT(*) as n, "
                    "SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) as no_wins, "
                    "AVG(market_price) as avg_yes_price "
                    "FROM evaluated_opportunities "
                    "WHERE product_type='hourly' AND side='yes' "
                    "AND market_result IS NOT NULL "
                    f"AND calibrated_prob >= {_min_pred} "
                    f"AND market_price <= {_HNO_MAX_YES_PRICE} AND asset=?"
                )
                _ra = _conn.execute(_sql_all, (_ha,)).fetchone()
                _n = _ra["n"] if _ra else 0
                _nw = (_ra["no_wins"] or 0) if _ra else 0
                _avg_yes = (_ra["avg_yes_price"] or 90) if _ra else 90
                _avg_no = 100 - _avg_yes
                _wr = round(_nw / _n, 4) if _n > 0 else 0
                # Wilson CI lower
                _wlo = 0.0
                if _n > 0:
                    _p = _nw / _n
                    _z = 1.96
                    _d = 1 + _z ** 2 / _n
                    _wlo = round((_p + _z ** 2 / (2 * _n) - _z * ((_p * (1 - _p) / _n + _z ** 2 / (4 * _n ** 2)) ** 0.5)) / _d, 4)
                # Sim PnL: flat 1-contract, buy NO at (100 - market_price)
                _sql_pnl = (
                    "SELECT SUM(CASE WHEN market_result='no' "
                    "  THEN (market_price - CAST(CEIL(0.07 * (market_price/100.0) * (1.0 - market_price/100.0) * 100) AS INTEGER)) "
                    "  ELSE -((100 - market_price) + CAST(CEIL(0.07 * ((100-market_price)/100.0) * (market_price/100.0) * 100) AS INTEGER)) "
                    "END) as pnl "
                    "FROM evaluated_opportunities "
                    "WHERE product_type='hourly' AND side='yes' "
                    "AND market_result IS NOT NULL "
                    f"AND calibrated_prob >= {_min_pred} "
                    f"AND market_price <= {_HNO_MAX_YES_PRICE} AND asset=?"
                )
                _rp = _conn.execute(_sql_pnl, (_ha,)).fetchone()
                _sim_pnl = (_rp["pnl"] or 0) if _rp else 0
                # Pending
                _pend_r = _conn.execute(
                    "SELECT COUNT(*) as c FROM evaluated_opportunities "
                    "WHERE product_type='hourly' AND side='yes' "
                    "AND market_result IS NULL "
                    f"AND calibrated_prob >= {_min_pred} "
                    f"AND market_price <= {_HNO_MAX_YES_PRICE} AND asset=?", (_ha,)
                ).fetchone()
                _pend = (_pend_r["c"] or 0) if _pend_r else 0
                # Breakeven WR at avg NO price
                _no_p = _avg_no / 100.0
                _fee_c = math.ceil(_HNO_FEE * 100 * _no_p * (1 - _no_p))
                _be_wr = round((_avg_no + _fee_c) / 100.0, 4)

                # Per-bucket breakdown
                buckets = []
                for _blabel, _blo, _bhi in _HNO_BUCKETS:
                    if _blo < _min_pred:
                        continue
                    _bsql = (
                        "SELECT COUNT(*) as n, "
                        "SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) as nw, "
                        "AVG(market_price) as avg_yp "
                        "FROM evaluated_opportunities "
                        "WHERE product_type='hourly' AND side='yes' "
                        "AND market_result IS NOT NULL "
                        f"AND calibrated_prob >= {_blo} AND calibrated_prob < {_bhi} "
                        f"AND market_price <= {_HNO_MAX_YES_PRICE} "
                        "AND asset=?"
                    )
                    _br = _conn.execute(_bsql, (_ha,)).fetchone()
                    _bn = _br["n"] if _br else 0
                    _bnw = (_br["nw"] or 0) if _br else 0
                    if _bn > 0:
                        buckets.append({
                            "label": _blabel,
                            "n": _bn,
                            "no_wins": _bnw,
                            "wr": round(_bnw / _bn, 4),
                            "avg_no_price": round(100 - (_br["avg_yp"] or 90), 1),
                        })

                hno[_ha] = {
                    "settled": _n, "no_wins": _nw,
                    "wr": _wr, "wilson_lower": _wlo,
                    "sim_pnl_cents": _sim_pnl, "pending": _pend,
                    "avg_no_price": round(_avg_no, 1),
                    "be_wr": _be_wr,
                    "min_yes_pred": _min_pred,
                    "buckets": buckets,
                }
            snap["hourly_no_side"] = hno
        except Exception:
            logging.warning("Snapshot: hourly_no_side build failed", exc_info=True)

        # ── Weather Observation Panel ─────────────────────────────────────
        try:
            if getattr(_bot_mod, "WEATHER_ENABLED", False):
                wx_data = {
                    "enabled": True,
                    "observation_only": getattr(_bot_mod, "WEATHER_OBSERVATION_ONLY", True),
                    "min_entry_price": getattr(_bot_mod, "WEATHER_MIN_ENTRY_PRICE", None),
                    "market_blend_w": getattr(_bot_mod, "WEATHER_MARKET_BLEND_W", None),
                    "max_risk_per_trade": getattr(_bot_mod, "WEATHER_MAX_RISK_PER_TRADE", None),
                }
                conn = _conn
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM evaluated_opportunities "
                        "WHERE product_type='weather' AND status='settled'"
                    ).fetchone()
                    wx_data["settled_count"] = row["cnt"] if row else 0
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM evaluated_opportunities "
                        "WHERE product_type='weather' AND status='settled' AND ("
                        "  (calibrated_prob > market_price/100.0 AND market_result='yes') OR "
                        "  (calibrated_prob <= market_price/100.0 AND market_result IN ('no','all_no'))"
                        ") AND calibrated_prob IS NOT NULL AND market_price IS NOT NULL"
                    ).fetchone()
                    wx_data["settled_wins"] = row["cnt"] if row else 0
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM evaluated_opportunities "
                        "WHERE product_type='weather' AND status='pending'"
                    ).fetchone()
                    wx_data["pending_count"] = row["cnt"] if row else 0
                except Exception:
                    wx_data["settled_count"] = 0
                    wx_data["settled_wins"] = 0
                    wx_data["pending_count"] = 0

                # Filter stage breakdown
                try:
                    rows = conn.execute(
                        "SELECT filter_stage, COUNT(*) AS cnt FROM evaluated_opportunities "
                        "WHERE product_type='weather' GROUP BY filter_stage"
                    ).fetchall()
                    wx_data["filter_stages"] = {
                        r["filter_stage"]: r["cnt"] for r in rows
                    } if rows else {}
                except Exception:
                    wx_data["filter_stages"] = {}

                # Simulated P&L (directional: infer side from calibrated_prob vs market_price)
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt, "
                        "SUM(CASE "
                        "  WHEN calibrated_prob > market_price/100.0 AND market_result='yes' THEN 1 "
                        "  WHEN calibrated_prob <= market_price/100.0 AND market_result IN ('no','all_no') THEN 1 "
                        "  ELSE 0 END) AS wins, "
                        "SUM(CASE "
                        "  WHEN calibrated_prob > market_price/100.0 AND market_result='yes' "
                        f"    THEN (100 - market_price) - CAST(CEIL({SIM_FEE_RATE} * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER) "
                        "  WHEN calibrated_prob > market_price/100.0 AND market_result IN ('no','all_no') "
                        f"    THEN -(market_price + CAST(CEIL({SIM_FEE_RATE} * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER)) "
                        "  WHEN calibrated_prob <= market_price/100.0 AND market_result IN ('no','all_no') "
                        f"    THEN market_price - CAST(CEIL({SIM_FEE_RATE} * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER) "
                        "  WHEN calibrated_prob <= market_price/100.0 AND market_result='yes' "
                        f"    THEN -((100 - market_price) + CAST(CEIL({SIM_FEE_RATE} * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER)) "
                        "  ELSE 0 END) AS sim_pnl "
                        "FROM evaluated_opportunities "
                        "WHERE product_type='weather' AND filter_stage='weather_observation' "
                        "AND status='settled' AND market_result IS NOT NULL "
                        "AND calibrated_prob IS NOT NULL AND market_price IS NOT NULL"
                    ).fetchone()
                    if row and row["cnt"] > 0:
                        wx_data["sim_trade_count"] = row["cnt"]
                        wx_data["sim_win_rate"] = round(row["wins"] / row["cnt"], 4)
                        wx_data["sim_pnl_cents"] = row["sim_pnl"] or 0
                    else:
                        wx_data["sim_trade_count"] = 0
                        wx_data["sim_win_rate"] = 0
                        wx_data["sim_pnl_cents"] = 0
                except Exception:
                    wx_data["sim_trade_count"] = 0
                    wx_data["sim_win_rate"] = 0
                    wx_data["sim_pnl_cents"] = 0
                # Weather shadow variants sim PnL
                wx_variants = {}
                for _wv_stage in ("weather_shadow_capped30", "weather_shadow_short_stc",
                                  "weather_shadow_capped30_short_stc"):
                    try:
                        row = conn.execute(
                            "SELECT COUNT(*) AS cnt, "
                            "SUM(CASE "
                            "  WHEN calibrated_prob > market_price/100.0 AND market_result='yes' THEN 1 "
                            "  WHEN calibrated_prob <= market_price/100.0 AND market_result IN ('no','all_no') THEN 1 "
                            "  ELSE 0 END) AS wins, "
                            "SUM(CASE "
                            "  WHEN calibrated_prob > market_price/100.0 AND market_result='yes' "
                            f"    THEN (100 - market_price) - CAST(CEIL({SIM_FEE_RATE} * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER) "
                            "  WHEN calibrated_prob > market_price/100.0 AND market_result IN ('no','all_no') "
                            f"    THEN -(market_price + CAST(CEIL({SIM_FEE_RATE} * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER)) "
                            "  WHEN calibrated_prob <= market_price/100.0 AND market_result IN ('no','all_no') "
                            f"    THEN market_price - CAST(CEIL({SIM_FEE_RATE} * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER) "
                            "  WHEN calibrated_prob <= market_price/100.0 AND market_result='yes' "
                            f"    THEN -((100 - market_price) + CAST(CEIL({SIM_FEE_RATE} * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER)) "
                            "  ELSE 0 END) AS sim_pnl "
                            "FROM evaluated_opportunities "
                            "WHERE product_type='weather' AND filter_stage=? "
                            "AND status='settled' AND market_result IS NOT NULL "
                            "AND calibrated_prob IS NOT NULL AND market_price IS NOT NULL",
                            (_wv_stage,)
                        ).fetchone()
                        if row and row["cnt"] > 0:
                            wx_variants[_wv_stage] = {
                                "count": row["cnt"],
                                "wins": row["wins"] or 0,
                                "win_rate": round((row["wins"] or 0) / row["cnt"], 4),
                                "sim_pnl_cents": row["sim_pnl"] or 0,
                            }
                    except Exception:
                        pass
                wx_data["shadow_variants"] = wx_variants

                snap["weather_observation"] = wx_data
        except Exception:
            logging.warning("Snapshot: weather_observation build failed", exc_info=True)

        # ── Sports Observation Panel ─────────────────────────────────────
        try:
            if getattr(_bot_mod, "SPORTS_ENABLED", False):
                sp_data = {
                    "enabled": True,
                    "observation_only": getattr(_bot_mod, "SPORTS_OBSERVATION_ONLY", True),
                }
                conn = _conn
                # Total shadow log entries + signals
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt, "
                        "SUM(CASE WHEN signal_fired=1 THEN 1 ELSE 0 END) AS signals "
                        "FROM sports_shadow_log"
                    ).fetchone()
                    sp_data["total_evaluations"] = row["cnt"] if row else 0
                    sp_data["total_signals"] = row["signals"] if row else 0
                except Exception:
                    sp_data["total_evaluations"] = 0
                    sp_data["total_signals"] = 0

                # Per-league breakdown
                try:
                    rows = conn.execute(
                        "SELECT league, COUNT(*) AS cnt, "
                        "SUM(CASE WHEN signal_fired=1 THEN 1 ELSE 0 END) AS signals "
                        "FROM sports_shadow_log GROUP BY league ORDER BY cnt DESC"
                    ).fetchall()
                    sp_data["leagues"] = {
                        r["league"]: {"evaluations": r["cnt"], "signals": r["signals"]}
                        for r in rows
                    } if rows else {}
                except Exception:
                    sp_data["leagues"] = {}

                # Settled signals (where we have outcome data)
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt, "
                        "SUM(CASE WHEN fav_won=1 THEN 1 ELSE 0 END) AS wins, "
                        "SUM(COALESCE(pnl_cents, 0)) AS sim_pnl "
                        "FROM sports_shadow_log "
                        "WHERE signal_fired=1 AND fav_won IS NOT NULL"
                    ).fetchone()
                    if row and row["cnt"] > 0:
                        sp_data["sim_trade_count"] = row["cnt"]
                        sp_data["sim_win_rate"] = round(row["wins"] / row["cnt"], 4)
                        sp_data["sim_pnl_cents"] = row["sim_pnl"] or 0
                    else:
                        sp_data["sim_trade_count"] = 0
                        sp_data["sim_win_rate"] = 0
                        sp_data["sim_pnl_cents"] = 0
                except Exception:
                    sp_data["sim_trade_count"] = 0
                    sp_data["sim_win_rate"] = 0
                    sp_data["sim_pnl_cents"] = 0

                # Edge distribution for signals
                try:
                    row = conn.execute(
                        "SELECT AVG(fee_adjusted_edge) AS avg_edge, "
                        "MIN(fee_adjusted_edge) AS min_edge, "
                        "MAX(fee_adjusted_edge) AS max_edge "
                        "FROM sports_shadow_log WHERE signal_fired=1"
                    ).fetchone()
                    if row and row["avg_edge"] is not None:
                        sp_data["avg_edge"] = round(row["avg_edge"], 4)
                        sp_data["min_edge"] = round(row["min_edge"], 4)
                        sp_data["max_edge"] = round(row["max_edge"], 4)
                except Exception:
                    pass

                snap["sports_observation"] = sp_data
        except Exception:
            logging.warning("Snapshot: sports_observation build failed", exc_info=True)

        # ── Sports Strong Config Analysis ─────────────────────────────────
        try:
            conn = _conn
            sc_data = {}

            def _wilson_ci(wins, total, z=1.96):
                """Wilson score interval (lower, upper)."""
                if total == 0:
                    return (0.0, 0.0)
                p = wins / total
                denom = 1 + z * z / total
                centre = (p + z * z / (2 * total)) / denom
                spread = z * ((p * (1 - p) + z * z / (4 * total)) / total) ** 0.5 / denom
                return (max(0.0, centre - spread), min(1.0, centre + spread))

            def _build_sports_subset(where_clause, params=()):
                """Build stats for a subset of sports_shadow_log."""
                row = conn.execute(
                    "SELECT COUNT(*) AS total, "
                    "SUM(CASE WHEN fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled, "
                    "SUM(CASE WHEN fav_won=1 THEN 1 ELSE 0 END) AS wins, "
                    "SUM(CASE WHEN fav_won=0 THEN 1 ELSE 0 END) AS losses, "
                    "SUM(COALESCE(pnl_cents, 0)) AS sim_pnl, "
                    "AVG(CASE WHEN fav_won IS NOT NULL THEN "
                    "  (comeback_prob - fav_won) * (comeback_prob - fav_won) END) AS brier_raw, "
                    "AVG(CASE WHEN fav_won IS NOT NULL AND platt_prob IS NOT NULL THEN "
                    "  (platt_prob - fav_won) * (platt_prob - fav_won) END) AS brier_platt "
                    f"FROM sports_shadow_log WHERE {where_clause}",
                    params
                ).fetchone()
                if not row or not row["total"]:
                    return {}
                settled = row["settled"] or 0
                wins = row["wins"] or 0
                losses = row["losses"] or 0
                wr = round(wins / settled, 4) if settled > 0 else 0
                ci_lo, ci_hi = _wilson_ci(wins, settled)
                return {
                    "total": row["total"],
                    "settled": settled,
                    "wins": wins,
                    "losses": losses,
                    "win_rate": wr,
                    "wilson_ci_lo": round(ci_lo, 4),
                    "wilson_ci_hi": round(ci_hi, 4),
                    "sim_pnl_cents": row["sim_pnl"] or 0,
                    "brier_raw": round(row["brier_raw"], 4) if row["brier_raw"] else None,
                    "brier_platt": round(row["brier_platt"], 4) if row["brier_platt"] else None,
                }

            # All sports — strong config (trailing + signal-like rows)
            sc_data["all"] = _build_sports_subset(
                "is_strong_config=1 AND filter_stage NOT IN ('sports_fav_leading')")
            # NBA only — strong config
            sc_data["nba"] = _build_sports_subset(
                "is_strong_config=1 AND sport_group='basketball' "
                "AND filter_stage NOT IN ('sports_fav_leading')")
            # Platt calibrator diagnostics (if available)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS n, "
                    "AVG(platt_prob) AS avg_platt, AVG(comeback_prob) AS avg_raw "
                    "FROM sports_shadow_log WHERE platt_prob IS NOT NULL"
                ).fetchone()
                platt_diag = {}
                if row and row["n"]:
                    platt_diag["n_rows"] = row["n"]
                    platt_diag["avg_platt"] = round(row["avg_platt"], 4) if row["avg_platt"] else None
                    platt_diag["avg_raw"] = round(row["avg_raw"], 4) if row["avg_raw"] else None
                # Read fitted A/B params from sports_platt_params
                try:
                    prow = conn.execute(
                        "SELECT a, b, n_train, h1_brier, h2_brier_raw, "
                        "h2_brier_cal, fitted, updated_at "
                        "FROM sports_platt_params WHERE id=1"
                    ).fetchone()
                    if prow and prow["fitted"]:
                        platt_diag["a"] = round(prow["a"], 4)
                        platt_diag["b"] = round(prow["b"], 4)
                        platt_diag["n_train"] = prow["n_train"]
                        platt_diag["h1_brier"] = round(prow["h1_brier"], 4) if prow["h1_brier"] else None
                        platt_diag["h2_brier_raw"] = round(prow["h2_brier_raw"], 4) if prow["h2_brier_raw"] else None
                        platt_diag["h2_brier_cal"] = round(prow["h2_brier_cal"], 4) if prow["h2_brier_cal"] else None
                        platt_diag["updated_at"] = prow["updated_at"]
                except Exception:
                    pass  # Table may not exist yet
                if platt_diag:
                    sc_data["platt"] = platt_diag
            except Exception:
                pass

            snap["sports_strong_config"] = sc_data
        except Exception:
            logging.warning("Snapshot: sports_strong_config build failed", exc_info=True)

        # ── Sports NBA Variants (Core + Wide) ────────────────────────────
        try:
            conn = _conn
            sv_data = {}

            def _wilson_ci_v(wins, total, z=1.96):
                """Wilson score interval (lower, upper)."""
                if total == 0:
                    return (0.0, 0.0)
                p = wins / total
                denom = 1 + z * z / total
                centre = (p + z * z / (2 * total)) / denom
                spread = z * ((p * (1 - p) + z * z / (4 * total)) / total) ** 0.5 / denom
                return (max(0.0, centre - spread), min(1.0, centre + spread))

            def _build_variant_stats(variant_name):
                """Build stats for an NBA variant from sports_shadow_log."""
                # nba_variant is set on all basketball evals (not just signals)
                # For settled signal stats, filter to signal_fired=1
                row = conn.execute(
                    "SELECT COUNT(*) AS total, "
                    "SUM(CASE WHEN fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled, "
                    "SUM(CASE WHEN fav_won=1 THEN 1 ELSE 0 END) AS wins, "
                    "SUM(CASE WHEN fav_won=0 THEN 1 ELSE 0 END) AS losses, "
                    "SUM(COALESCE(pnl_cents, 0)) AS sim_pnl "
                    "FROM sports_shadow_log "
                    "WHERE signal_fired=1 AND nba_variant=? "
                    "AND fav_won IS NOT NULL AND pnl_cents IS NOT NULL",
                    (variant_name,)
                ).fetchone()
                if not row or not row["settled"]:
                    return {"games": 0, "wins": 0, "losses": 0,
                            "win_rate": 0, "wilson_ci_lo": 0, "wilson_ci_hi": 0,
                            "sim_pnl_cents": 0, "game_log": []}
                settled = row["settled"] or 0
                wins = row["wins"] or 0
                losses = row["losses"] or 0
                wr = round(wins / settled, 4) if settled > 0 else 0
                ci_lo, ci_hi = _wilson_ci_v(wins, settled)

                # Game log: one row per game (deduped by game_id, take first signal)
                game_rows = conn.execute(
                    "SELECT DATE(evaluation_time) AS dt, "
                    "home_team, away_team, "
                    "ROUND(100*pregame_fav_prob,0) AS pregame, "
                    "deficit, period, "
                    "ROUND(100*time_remaining_pct,0) AS trp, "
                    "yes_ask, fav_won, pnl_cents "
                    "FROM sports_shadow_log "
                    "WHERE signal_fired=1 AND nba_variant=? "
                    "AND fav_won IS NOT NULL AND pnl_cents IS NOT NULL "
                    "ORDER BY evaluation_time",
                    (variant_name,)
                ).fetchall()
                game_log = []
                for gr in game_rows:
                    game_log.append({
                        "date": gr["dt"] or "",
                        "teams": f"{gr['home_team'] or '?'} v {gr['away_team'] or '?'}",
                        "pregame": int(gr["pregame"] or 0),
                        "deficit": gr["deficit"] or 0,
                        "period": gr["period"] or 0,
                        "trp": int(gr["trp"] or 0),
                        "ask": gr["yes_ask"] or 0,
                        "won": bool(gr["fav_won"]),
                        "pnl": gr["pnl_cents"] or 0,
                    })

                return {
                    "games": settled,
                    "wins": wins,
                    "losses": losses,
                    "win_rate": wr,
                    "wilson_ci_lo": round(ci_lo, 4),
                    "wilson_ci_hi": round(ci_hi, 4),
                    "sim_pnl_cents": row["sim_pnl"] or 0,
                    "game_log": game_log,
                }

            sv_data["core"] = _build_variant_stats("core")
            sv_data["wide"] = _build_variant_stats("wide")

            # Also count unsettled signals for each variant (pending games)
            for vname in ("core", "wide"):
                try:
                    prow = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM sports_shadow_log "
                        "WHERE signal_fired=1 AND nba_variant=? AND fav_won IS NULL",
                        (vname,)
                    ).fetchone()
                    sv_data[vname]["pending"] = prow["cnt"] if prow else 0
                except Exception:
                    sv_data[vname]["pending"] = 0

            snap["sports_variants"] = sv_data
        except Exception:
            logging.warning("Snapshot: sports_variants build failed", exc_info=True)

        # ── Data Collection Progress ───────────────────────────────────────
        try:
            conn = _conn
            dc = {}
            for pt, obs_stage, target in [
                ("hourly", "hourly_observation", 200),
                ("spx_hourly", "spx_observation", 200),
                ("weather", "weather_observation", 100),
            ]:
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) AS total, "
                        "  SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) AS settled, "
                        "  MIN(evaluation_time) AS first_ts, MAX(evaluation_time) AS last_ts "
                        "FROM evaluated_opportunities "
                        "WHERE product_type=? AND filter_stage=?", (pt, obs_stage)
                    ).fetchone()
                    settled = (row["settled"] or 0) if row else 0
                    total = (row["total"] or 0) if row else 0
                    rate_per_day = None
                    eta_days = None
                    if row and row["first_ts"] and row["last_ts"] and settled > 1:
                        from datetime import datetime as _dt
                        try:
                            t0 = _dt.fromisoformat(row["first_ts"].replace("Z", "+00:00"))
                            t1 = _dt.fromisoformat(row["last_ts"].replace("Z", "+00:00"))
                            days_elapsed = max((t1 - t0).total_seconds() / 86400, 0.01)
                            rate_per_day = round(settled / days_elapsed, 1)
                            remaining = max(0, target - settled)
                            eta_days = round(remaining / rate_per_day, 1) if rate_per_day > 0 else None
                        except Exception:
                            pass
                    dc[pt] = {
                        "signals": total, "settled": settled, "target": target,
                        "rate_per_day": rate_per_day, "eta_days": eta_days,
                    }
                except Exception:
                    dc[pt] = {"signals": 0, "settled": 0, "target": target,
                              "rate_per_day": None, "eta_days": None}
            # Sports uses sports_shadow_log table
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS total, "
                    "  SUM(CASE WHEN fav_won IS NOT NULL THEN 1 ELSE 0 END) AS settled, "
                    "  MIN(timestamp) AS first_ts, MAX(timestamp) AS last_ts "
                    "FROM sports_shadow_log WHERE signal_fired=1"
                ).fetchone()
                settled = (row["settled"] or 0) if row else 0
                total = (row["total"] or 0) if row else 0
                target = 300
                rate_per_day = None
                eta_days = None
                if row and row["first_ts"] and row["last_ts"] and settled > 1:
                    from datetime import datetime as _dt
                    try:
                        t0 = _dt.fromisoformat(str(row["first_ts"]).replace("Z", "+00:00"))
                        t1 = _dt.fromisoformat(str(row["last_ts"]).replace("Z", "+00:00"))
                        days_elapsed = max((t1 - t0).total_seconds() / 86400, 0.01)
                        rate_per_day = round(settled / days_elapsed, 1)
                        remaining = max(0, target - settled)
                        eta_days = round(remaining / rate_per_day, 1) if rate_per_day > 0 else None
                    except Exception:
                        pass
                dc["sports"] = {
                    "signals": total, "settled": settled, "target": target,
                    "rate_per_day": rate_per_day, "eta_days": eta_days,
                }
            except Exception:
                dc["sports"] = {"signals": 0, "settled": 0, "target": 300,
                                "rate_per_day": None, "eta_days": None}
            snap["data_collection"] = dc
        except Exception:
            logging.warning("Snapshot: data_collection build failed", exc_info=True)
            snap["data_collection"] = {}

        # ── Capital Allocation Panel ──────────────────────────────────────
        try:
            cap_alloc = getattr(self._ml, "capital_allocator", None)
            if cap_alloc:
                snap["capital_allocation"] = {
                    "regime": cap_alloc.get_regime(),
                    "ewma_corr": cap_alloc.get_ewma_correlation(),
                    "vix_level": cap_alloc.get_vix(),
                    "composite_score": cap_alloc.get_composite_score(),
                }
        except Exception:
            logging.warning("Snapshot: capital_allocation build failed", exc_info=True)

        # ── Orderbook visibility (dashboard only) ─────────────────────────
        try:
            kf = getattr(self._ml, "kalshi_feed", None)
            if kf and kf.is_connected:
                all_obs = kf.get_all_orderbooks()
                now_ts = time.time()
                ob_summary = {}

                for ticker, ob in all_obs.items():
                    try:
                        ob_ts = ob.get("ts", 0)
                        age_s = round(now_ts - ob_ts, 1)
                        if age_s > 60:
                            continue  # Don't push stale orderbooks to dashboard

                        no_bids = ob.get("no", [])
                        yes_bids = ob.get("yes", [])

                        parsed_no = _parse_ob_levels(no_bids)
                        yes_ask_levels = [{"p": 100 - p, "q": q} for p, q in parsed_no]
                        yes_ask_levels.sort(key=lambda x: x["p"])

                        parsed_yes = _parse_ob_levels(yes_bids)
                        yes_bid_levels = [{"p": p, "q": q} for p, q in parsed_yes]
                        yes_bid_levels.sort(key=lambda x: -x["p"])

                        best_ask = yes_ask_levels[0]["p"] if yes_ask_levels else None
                        best_bid = yes_bid_levels[0]["p"] if yes_bid_levels else None
                        spread = (best_ask - best_bid) if (best_ask is not None and best_bid is not None) else None

                        stale = age_s > 30
                        total_ask_depth = sum(l["q"] for l in yes_ask_levels)
                        total_bid_depth = sum(l["q"] for l in yes_bid_levels)

                        asset = "UNK"
                        for a in ["BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE"]:
                            if a in ticker.upper():
                                asset = a
                                break

                        if asset not in ob_summary:
                            ob_summary[asset] = {}

                        ob_summary[asset][ticker] = {
                            "best_ask": best_ask,
                            "best_bid": best_bid,
                            "spread": spread,
                            "ask_depth": total_ask_depth,
                            "bid_depth": total_bid_depth,
                            "asks": yes_ask_levels[:3],
                            "bids": yes_bid_levels[:3],
                            "age_s": age_s,
                            "stale": stale,
                        }
                    except Exception:
                        logging.warning(f"Snapshot: ob summary failed for {ticker}", exc_info=True)

                snap["orderbooks"] = ob_summary
                logging.debug(f"Snapshot: orderbooks built for {sum(len(v) for v in ob_summary.values())} tickers")
            else:
                snap["orderbooks"] = {}
        except Exception:
            logging.warning("Snapshot: orderbooks build failed", exc_info=True)
            snap["orderbooks"] = {}

        # ── Position health (read-only enrichment for dashboard) ─────────
        try:
            kf = getattr(self._ml, "kalshi_feed", None)
            raw_obs = kf.get_all_orderbooks() if (kf and kf.is_connected) else {}
            windows = getattr(self._ml, "_active_windows", []) or []
            snap["position_health"] = self._compute_position_health(
                snap.get("active_positions", []), raw_obs, windows, db_conn=_conn
            )
        except Exception:
            logging.warning("Snapshot: position_health build failed", exc_info=True)
            snap["position_health"] = {
                "summary": {"lock": 0, "watch": 0, "danger": 0, "total": 0},
                "positions": {},
            }

        # Disk usage check
        try:
            import shutil
            disk = shutil.disk_usage("/home/botuser")
            snap["disk_free_gb"] = round(disk.free / (1024**3), 1)
            if snap["disk_free_gb"] < 2:
                logging.critical(f"LOW DISK: {snap['disk_free_gb']}GB free")
        except Exception:
            snap["disk_free_gb"] = None

        # ── 15M Shadow Panel ────────────────────────────────────────────
        try:
            _15m_eng = getattr(self._ml, "fifteenm_shadow", None) if self._ml else None
            if _15m_eng:
                snap["fifteenm_shadow"] = _15m_eng.get_dashboard_data()
            else:
                snap["fifteenm_shadow"] = None
        except Exception:
            logging.warning("Snapshot: fifteenm_shadow build failed", exc_info=True)
            snap["fifteenm_shadow"] = None

        # ── Hourly Alt Shadow Strategies Panel ───────────────────────────
        try:
            _alt_eng = getattr(self._ml, "hourly_alt_shadow", None) if self._ml else None
            if _alt_eng:
                snap["hourly_alt_shadow"] = _alt_eng.get_dashboard_data()
            else:
                snap["hourly_alt_shadow"] = None
        except Exception:
            logging.warning("Snapshot: hourly_alt_shadow build failed", exc_info=True)
            snap["hourly_alt_shadow"] = None

        # ── SPX HAR-RV Shadow Panel ──────────────────────────────────────
        try:
            _harv_eng = getattr(self._ml, "spx_harrv_shadow", None) if self._ml else None
            if _harv_eng:
                snap["spx_harrv_shadow"] = _harv_eng.get_dashboard_data()
            else:
                snap["spx_harrv_shadow"] = None
        except Exception:
            logging.warning("Snapshot: spx_harrv_shadow build failed", exc_info=True)
            snap["spx_harrv_shadow"] = None

        # ── STC Performance (15M live trades by STC bucket) ────────────
        # Combined into single query (was 3 queries in loop)
        try:
            _stc_rows = _conn.execute(
                "SELECT CASE "
                "  WHEN seconds_to_close < 180 THEN '0-180' "
                "  WHEN seconds_to_close < 500 THEN '180-500' "
                "  ELSE '500-900' END AS bucket, "
                "COUNT(*) AS n, "
                "SUM(CASE WHEN (side='yes' AND market_result='yes') OR "
                "  (side='no' AND market_result IN ('no','all_no')) THEN 1 ELSE 0 END) AS w, "
                "SUM(pnl_cents - fee_cents) AS pnl "
                "FROM settled_trades WHERE product_type='15m' "
                "AND seconds_to_close >= 0 AND seconds_to_close < 900 "
                "AND settled_at >= ? "
                "GROUP BY bucket",
                (CONFIG_REGIME_SINCE,)
            ).fetchall()
            _query_count += 1
            stc_perf = {}
            for row in _stc_rows:
                stc_perf[row["bucket"]] = {
                    "trades": row["n"] if row else 0,
                    "wins": row["w"] if row and row["w"] else 0,
                    "wr": round(row["w"] / row["n"], 4) if row and row["n"] and row["w"] else 0,
                    "pnl_cents": row["pnl"] if row and row["pnl"] else 0,
                }
            # Ensure all buckets present even if empty
            for b in ("0-180", "180-500", "500-900"):
                if b not in stc_perf:
                    stc_perf[b] = {"trades": 0, "wins": 0, "wr": 0, "pnl_cents": 0}
            snap["stc_performance"] = stc_perf
        except Exception:
            logging.warning("Snapshot: stc_performance build failed", exc_info=True)

        # ── Calibration Health (overconfidence + Brier by bucket) ──────
        try:
            conn = _conn
            rows = conn.execute(
                "SELECT calibrated_prob, market_result, market_price FROM evaluated_opportunities "
                "WHERE product_type='15m' AND filter_stage='candidate' AND status='settled' "
                "AND calibrated_prob IS NOT NULL AND market_result IS NOT NULL "
                "AND evaluation_time >= ?",
                (CONFIG_REGIME_SINCE,)
            ).fetchall()
            if rows and len(rows) > 0:
                outcomes = []
                preds = []
                for r in rows:
                    pred = r["calibrated_prob"]
                    mp = r["market_price"]
                    if pred > mp / 100.0:
                        actual = 1.0 if r["market_result"] == "yes" else 0.0
                    else:
                        actual = 1.0 if r["market_result"] in ("no", "all_no") else 0.0
                        pred = 1.0 - pred
                    preds.append(pred)
                    outcomes.append(actual)
                overconf = sum(preds) / len(preds) - sum(outcomes) / len(outcomes)
                brier = sum((p - o) ** 2 for p, o in zip(preds, outcomes)) / len(preds)
                market_preds = []
                for r in rows:
                    mp = r["market_price"] / 100.0
                    if r["calibrated_prob"] > mp:
                        market_preds.append(mp)
                    else:
                        market_preds.append(1.0 - mp)
                market_brier = sum((p - o) ** 2 for p, o in zip(market_preds, outcomes)) / len(market_preds)
                buckets = {}
                for pred_val, outcome_val in zip(preds, outcomes):
                    bucket_key = f"{int(pred_val * 100 // 5) * 5}-{int(pred_val * 100 // 5) * 5 + 4}"
                    if bucket_key not in buckets:
                        buckets[bucket_key] = {"preds": [], "outcomes": []}
                    buckets[bucket_key]["preds"].append(pred_val)
                    buckets[bucket_key]["outcomes"].append(outcome_val)
                brier_by_bucket = {}
                for bk, bv in sorted(buckets.items()):
                    n = len(bv["preds"])
                    avg_pred = sum(bv["preds"]) / n
                    avg_outcome = sum(bv["outcomes"]) / n
                    brier_by_bucket[bk] = {
                        "n": n,
                        "avg_predicted": round(avg_pred, 4),
                        "avg_actual": round(avg_outcome, 4),
                        "gap_pp": round((avg_pred - avg_outcome) * 100, 2),
                    }
                snap["calibration_health"] = {
                    "n": len(preds),
                    "overconfidence_pp": round(overconf * 100, 2),
                    "model_brier": round(brier, 4),
                    "market_brier": round(market_brier, 4),
                    "brier_improvement_pct": round((1 - brier / market_brier) * 100, 1) if market_brier > 0 else 0,
                    "brier_by_bucket": brier_by_bucket,
                }
            else:
                snap["calibration_health"] = {"n": 0}
        except Exception:
            logging.warning("Snapshot: calibration_health build failed", exc_info=True)

        # ── Edge Integrity (monotonicity check) ───────────────────────
        try:
            conn = _conn
            rows = conn.execute(
                "SELECT fee_adjusted_edge, market_result, calibrated_prob, market_price "
                "FROM evaluated_opportunities "
                "WHERE product_type='15m' AND filter_stage='candidate' AND status='settled' "
                "AND fee_adjusted_edge IS NOT NULL AND calibrated_prob IS NOT NULL "
                "AND market_price IS NOT NULL AND evaluation_time >= ?",
                (CONFIG_REGIME_SINCE,)
            ).fetchall()
            if rows and len(rows) >= 10:
                data = []
                for r in rows:
                    # Infer side from calibrated_prob vs market_price
                    yes_side = r["calibrated_prob"] > r["market_price"] / 100.0
                    won = (yes_side and r["market_result"] == "yes") or \
                          (not yes_side and r["market_result"] in ("no", "all_no"))
                    data.append((r["fee_adjusted_edge"], 1 if won else 0))
                data.sort(key=lambda x: x[0])
                n = len(data)
                q_size = n // 5
                quintiles = []
                for i in range(5):
                    start = i * q_size
                    end = (i + 1) * q_size if i < 4 else n
                    chunk = data[start:end]
                    wins = sum(c[1] for c in chunk)
                    avg_edge = sum(c[0] for c in chunk) / len(chunk)
                    wr = wins / len(chunk) if len(chunk) > 0 else 0
                    quintiles.append({
                        "label": f"Q{i+1}",
                        "n": len(chunk),
                        "avg_edge": round(avg_edge, 4),
                        "wr": round(wr, 4),
                    })
                inversions = sum(1 for i in range(4) if quintiles[i]["wr"] > quintiles[i+1]["wr"])
                edges = [d[0] for d in data]
                wins_arr = [d[1] for d in data]
                mean_e = sum(edges) / n
                mean_w = sum(wins_arr) / n
                num = sum((e - mean_e) * (w - mean_w) for e, w in zip(edges, wins_arr))
                den_e = sum((e - mean_e) ** 2 for e in edges) ** 0.5
                den_w = sum((w - mean_w) ** 2 for w in wins_arr) ** 0.5
                corr = num / (den_e * den_w) if den_e > 0 and den_w > 0 else 0
                snap["edge_integrity"] = {
                    "n": n,
                    "quintiles": quintiles,
                    "inversions": inversions,
                    "monotonic": inversions == 0,
                    "correlation": round(corr, 4),
                }
            else:
                snap["edge_integrity"] = {"n": len(rows) if rows else 0}
        except Exception:
            logging.warning("Snapshot: edge_integrity build failed", exc_info=True)

        # ── System Health (consolidated health signals) ────────────────
        try:
            health_issues = []
            exec_eng = snap.get("execution_engine", {})
            health_alerts = exec_eng.get("health_alerts", [])
            for alert in health_alerts:
                health_issues.append({"source": "execution", "message": alert})
            feed = snap.get("feed_health", {})
            for exch, status in (feed.items() if isinstance(feed, dict) else []):
                if not status:
                    health_issues.append({"source": "feed", "message": f"{exch} disconnected"})
            cal = snap.get("calibration", {})
            if cal.get("rolling_brier") and cal["rolling_brier"] > 0.20:
                health_issues.append({"source": "calibration", "message": f"15M Brier elevated: {cal['rolling_brier']:.3f}"})
            if snap.get("balance_stale"):
                health_issues.append({"source": "balance", "message": "Balance API returning stale data"})
            err = snap.get("last_error_message")
            if err:
                health_issues.append({"source": "error", "message": str(err)[:200]})
            snap["system_health"] = {
                "status": "healthy" if len(health_issues) == 0 else ("degraded" if len(health_issues) <= 2 else "unhealthy"),
                "issue_count": len(health_issues),
                "issues": health_issues[:10],
            }
        except Exception:
            snap["system_health"] = {"status": "unknown", "issue_count": 0, "issues": []}

        # ── Shadow Comparison (unified shadow strategy table) ──────────
        try:
            comparison = []
            ho = snap.get("hourly_observation", {})
            if ho.get("enabled"):
                comparison.append({
                    "product": "Crypto Hourly",
                    "status": "SHADOW" if ho.get("observation_only") else "LIVE",
                    "settled": ho.get("settled_count", 0),
                    "wins": ho.get("settled_wins", 0),
                    "wr": ho.get("sim_win_rate", 0),
                    "sim_pnl_cents": ho.get("sim_pnl_cents", 0),
                    "avg_edge": ho.get("avg_edge"),
                })
            so = snap.get("spx_observation", {})
            if so.get("enabled"):
                comparison.append({
                    "product": "SPX Hourly",
                    "status": "SHADOW" if so.get("observation_only") else "LIVE",
                    "settled": so.get("settled_count", 0),
                    "wins": so.get("settled_wins", 0),
                    "wr": so.get("sim_win_rate", 0),
                    "sim_pnl_cents": so.get("sim_pnl_cents", 0),
                    "avg_edge": None,
                })
            wo = snap.get("weather_observation", {})
            if wo.get("enabled"):
                comparison.append({
                    "product": "Weather",
                    "status": "SHADOW" if wo.get("observation_only") else "LIVE",
                    "settled": wo.get("settled_count", 0),
                    "wins": wo.get("settled_wins", 0),
                    "wr": wo.get("sim_win_rate", 0),
                    "sim_pnl_cents": wo.get("sim_pnl_cents", 0),
                    "avg_edge": wo.get("avg_edge"),
                })
            spo = snap.get("sports_observation", {})
            if spo:
                comparison.append({
                    "product": "Sports Comeback",
                    "status": "SHADOW",
                    "settled": spo.get("sim_trade_count", 0),
                    "wins": round(spo.get("sim_win_rate", 0) * spo.get("sim_trade_count", 0)),
                    "wr": spo.get("sim_win_rate", 0),
                    "sim_pnl_cents": spo.get("sim_pnl_cents", 0),
                    "avg_edge": spo.get("avg_edge"),
                })
            snap["shadow_comparison"] = comparison
        except Exception:
            snap["shadow_comparison"] = []

        # ── STC Shadow Counterfactual (500-900s shadow zone) ───────────
        try:
            conn = _conn
            row = conn.execute(
                "SELECT COUNT(*) AS n, "
                "SUM(CASE "
                "  WHEN (calibrated_prob > market_price/100.0 AND market_result='yes') "
                "    OR (calibrated_prob <= market_price/100.0 AND market_result IN ('no','all_no')) "
                "  THEN 1 ELSE 0 END) AS w, "
                "SUM(CASE "
                "  WHEN calibrated_prob > market_price/100.0 AND market_result='yes' "
                f"    THEN (100 - market_price) - CAST(CEIL({SIM_FEE_RATE} * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER) "
                "  WHEN calibrated_prob > market_price/100.0 AND market_result IN ('no','all_no') "
                f"    THEN -(market_price + CAST(CEIL({SIM_FEE_RATE} * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER)) "
                "  WHEN calibrated_prob <= market_price/100.0 AND market_result IN ('no','all_no') "
                f"    THEN market_price - CAST(CEIL({SIM_FEE_RATE} * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER) "
                "  WHEN calibrated_prob <= market_price/100.0 AND market_result='yes' "
                f"    THEN -((100 - market_price) + CAST(CEIL({SIM_FEE_RATE} * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER)) "
                "  ELSE 0 END) AS sim_pnl "
                "FROM evaluated_opportunities "
                "WHERE product_type='15m' AND filter_stage IN ('stc_shadow','stc_shadow_xrp','stc_shadow_no_xrp') AND status='settled' "
                "AND market_result IS NOT NULL AND calibrated_prob IS NOT NULL AND market_price IS NOT NULL "
                "AND evaluation_time >= ?",
                (CONFIG_REGIME_SINCE,)
            ).fetchone()
            snap["stc_shadow_counterfactual"] = {
                "n": row["n"] if row else 0,
                "wins": row["w"] if row and row["w"] else 0,
                "wr": round(row["w"] / row["n"], 4) if row and row["n"] and row["w"] else 0,
                "sim_pnl_cents": row["sim_pnl"] if row and row["sim_pnl"] else 0,
            }
        except Exception:
            snap["stc_shadow_counterfactual"] = {"n": 0, "wins": 0, "wr": 0, "sim_pnl_cents": 0}

        # ── NO-side Shadow Summary ────────────────────────────────────
        try:
            _no_row = _conn.execute(
                "SELECT COUNT(*) as n, "
                "SUM(CASE WHEN status='settled' AND "
                "  ((side='no' AND market_result IN ('no','all_no')) OR "
                "   (side='yes' AND market_result IN ('yes','all_yes'))) "
                "  THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl "
                "FROM evaluated_opportunities WHERE side='no' AND evaluation_time >= ?",
                (CONFIG_REGIME_SINCE,)
            ).fetchone()
            # Per-product breakdown
            _no_by_pt = {}
            for _pt_row in _conn.execute(
                "SELECT product_type, COUNT(*) as n, "
                "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "SUM(CASE WHEN status='settled' AND market_result IN ('no','all_no') "
                "  THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl "
                "FROM evaluated_opportunities WHERE side='no' AND evaluation_time >= ? "
                "GROUP BY product_type",
                (CONFIG_REGIME_SINCE,)
            ).fetchall():
                _no_by_pt[_pt_row["product_type"] or "15m"] = {
                    "n": _pt_row["n"], "settled": _pt_row["settled"],
                    "wins": _pt_row["wins"] or 0,
                    "wr": round(_pt_row["wins"] / _pt_row["settled"], 4) if _pt_row["settled"] else 0,
                    "sim_pnl_cents": _pt_row["sim_pnl"] or 0,
                }
            snap["no_side_shadow"] = {
                "total_signals": _no_row["n"] if _no_row else 0,
                "settled": _no_row["settled"] if _no_row else 0,
                "wins": _no_row["wins"] if _no_row else 0,
                "wr": round(_no_row["wins"] / _no_row["settled"], 4) if _no_row and _no_row["settled"] else 0,
                "sim_pnl_cents": _no_row["sim_pnl"] if _no_row else 0,
                "by_product_type": _no_by_pt,
            }
        except Exception:
            snap["no_side_shadow"] = {"total_signals": 0, "settled": 0, "wins": 0, "wr": 0,
                                      "sim_pnl_cents": 0, "by_product_type": {}}

        # ── Shadow Panels (combined: 17 queries → 3) ─────────────────
        _SHADOW_STAGES = (
            'weekend_discount_shadow', 'overnight_discount_shadow',
            'overnight_lp_shadow', 'decided_contract_t1',
            'decided_contract_t1b', 'decided_contract_t2',
            'relaxed_edge_shadow', 'low_price_shadow',
        )
        _shadow_defaults = {
            "weekend_discount_shadow": {"total_signals": 0, "settled": 0, "wins": 0, "wr": 0,
                                        "sim_pnl_cents": 0, "by_asset": {}, "by_price_tier": {},
                                        "discount_factor": 0.60},
            "overnight_discount_shadow": {"total_signals": 0, "settled": 0, "wins": 0, "wr": 0,
                                          "sim_pnl_cents": 0, "by_asset": {}, "by_price_tier": {},
                                          "discount_factor": 0.60},
            "overnight_lp_shadow": {
                "total_signals": 0, "settled": 0, "wins": 0, "wr": 0,
                "sim_pnl_cents": 0, "by_asset": {}, "by_price_tier": {},
                "edge_distribution": {"min": None, "avg": None, "max": None},
                "config": {"price_range": "50-85c", "hours": "00-12 UTC",
                           "min_cal_prob": 0.82, "min_edge": 0.10,
                           "kelly_fraction": 0.125, "max_risk": 0.10,
                           "stc_range": "120-600s", "vol_spike_mult": 2.0},
            },
            "decided_contract_shadow": {"total_signals": 0, "settled": 0, "wins": 0, "wr": 0,
                                        "sim_pnl_cents": 0, "by_asset": {}, "by_tier": {}},
            "relaxed_edge_shadow": {"total_signals": 0, "settled": 0, "wins": 0, "wr": 0,
                                    "sim_pnl_cents": 0, "by_asset": {}, "by_price_tier": {}},
            "low_price_shadow": {"total_signals": 0, "settled": 0, "wins": 0, "wr": 0,
                                  "sim_pnl_cents": 0, "by_asset": {}, "by_price_tier": {},
                                  "config": {"price_range": "20-79c", "max_stc": 900,
                                             "lp_kelly_fraction": 0.25, "lp_max_risk": 0.10,
                                             "window_cap": 2, "hour_cap": 4}},
        }
        try:
            # Query 1: per filter_stage × asset (covers totals + by_asset for all 5 panels)
            _sh_by_asset = _conn.execute(
                "SELECT filter_stage, asset, COUNT(*) as n, "
                "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes') "
                "  THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl "
                "FROM evaluated_opportunities "
                "WHERE filter_stage IN ('weekend_discount_shadow','overnight_discount_shadow',"
                "'overnight_lp_shadow','decided_contract_t1','decided_contract_t1b','decided_contract_t2','relaxed_edge_shadow','low_price_shadow') "
                "GROUP BY filter_stage, asset"
            ).fetchall()
            _query_count += 1

            # Query 2: per filter_stage × market_price (for price tier bucketing in Python)
            _sh_by_price = _conn.execute(
                "SELECT filter_stage, market_price, COUNT(*) as n, "
                "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes') "
                "  THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl "
                "FROM evaluated_opportunities "
                "WHERE filter_stage IN ('weekend_discount_shadow','overnight_discount_shadow',"
                "'overnight_lp_shadow','decided_contract_t1','decided_contract_t1b','decided_contract_t2','relaxed_edge_shadow','low_price_shadow') "
                "GROUP BY filter_stage, market_price"
            ).fetchall()
            _query_count += 1

            # Query 3: edge distribution for overnight LP only
            _olp_edge = _conn.execute(
                "SELECT ROUND(MIN(fee_adjusted_edge), 4) as min_edge, "
                "ROUND(AVG(fee_adjusted_edge), 4) as avg_edge, "
                "ROUND(MAX(fee_adjusted_edge), 4) as max_edge "
                "FROM evaluated_opportunities WHERE filter_stage='overnight_lp_shadow'"
            ).fetchone()
            _query_count += 1

            # --- Disaggregate by_asset data ---
            # Build {filter_stage: {asset: {n, settled, wins, sim_pnl}}}
            _sh_asset_map = {}
            for r in _sh_by_asset:
                fs = r["filter_stage"]
                _sh_asset_map.setdefault(fs, {})[r["asset"]] = {
                    "n": r["n"], "settled": r["settled"],
                    "wins": r["wins"] or 0,
                    "wr": round(r["wins"] / r["settled"], 4) if r["settled"] else 0,
                    "sim_pnl_cents": r["sim_pnl"] or 0,
                }

            # Compute totals per filter_stage by summing across assets
            _sh_totals = {}
            for fs, assets in _sh_asset_map.items():
                _sh_totals[fs] = {
                    "n": sum(a["n"] for a in assets.values()),
                    "settled": sum(a["settled"] for a in assets.values()),
                    "wins": sum(a["wins"] for a in assets.values()),
                    "sim_pnl": sum(a["sim_pnl_cents"] for a in assets.values()),
                }

            # --- Disaggregate by_price data with per-panel tier bucketing ---
            def _bucket_standard(mp):
                """86-88, 89-90, 91-92, 93-94, 95+"""
                if mp is None:
                    return "unknown"
                if mp >= 95: return "95+"
                if mp >= 93: return "93-94"
                if mp >= 91: return "91-92"
                if mp >= 89: return "89-90"
                return "86-88"

            def _bucket_lp(mp):
                """50-64, 65-74, 75-85"""
                if mp is None:
                    return "unknown"
                if mp >= 75: return "75-85"
                if mp >= 65: return "65-74"
                return "50-64"

            def _bucket_relaxed(mp):
                """88-89, 90-91, 92"""
                if mp is None:
                    return "unknown"
                if mp >= 92: return "92"
                if mp >= 90: return "90-91"
                return "88-89"

            def _bucket_low_price(mp):
                """20-39, 40-54, 55-69, 70-74, 75-79 (Phase C of shadow
                coverage expansion 2026-05-02 widened the band from 70-79
                to 20-79; tiers added so the dashboard rollup doesn't
                silently bucket 20-69¢ rows into 70-74)."""
                if mp is None:
                    return "unknown"
                if mp >= 75: return "75-79"
                if mp >= 70: return "70-74"
                if mp >= 55: return "55-69"
                if mp >= 40: return "40-54"
                return "20-39"

            _tier_bucketers = {
                "weekend_discount_shadow": _bucket_standard,
                "overnight_discount_shadow": _bucket_standard,
                "overnight_lp_shadow": _bucket_lp,
                "relaxed_edge_shadow": _bucket_relaxed,
                "low_price_shadow": _bucket_low_price,
            }

            # Build {filter_stage: {tier: {n, settled, wins, sim_pnl}}}
            _sh_tier_map = {}
            for r in _sh_by_price:
                fs = r["filter_stage"]
                bucketer = _tier_bucketers.get(fs)
                if bucketer:
                    tier = bucketer(r["market_price"])
                else:
                    # decided_contract uses filter_stage as tier — skip price bucketing
                    continue
                bucket = _sh_tier_map.setdefault(fs, {}).setdefault(tier, {"n": 0, "settled": 0, "wins": 0, "sim_pnl": 0})
                bucket["n"] += r["n"]
                bucket["settled"] += (r["settled"] or 0)
                bucket["wins"] += (r["wins"] or 0)
                bucket["sim_pnl"] += (r["sim_pnl"] or 0)

            # Finalize tier dicts with wr
            for fs, tiers in _sh_tier_map.items():
                for tier, vals in tiers.items():
                    vals["sim_pnl_cents"] = vals.pop("sim_pnl")
                    vals["wr"] = round(vals["wins"] / vals["settled"], 4) if vals["settled"] else 0

            # --- Helper to build a panel snap dict ---
            def _panel_snap(fs, totals_dict, by_asset_dict, by_tier_dict):
                t = totals_dict.get(fs, {"n": 0, "settled": 0, "wins": 0, "sim_pnl": 0})
                return {
                    "total_signals": t["n"],
                    "settled": t["settled"],
                    "wins": t["wins"],
                    "wr": round(t["wins"] / t["settled"], 4) if t["settled"] else 0,
                    "sim_pnl_cents": t["sim_pnl"],
                    "by_asset": by_asset_dict.get(fs, {}),
                    "by_price_tier": by_tier_dict.get(fs, {}),
                }

            # Weekend discount shadow
            _wknd = _panel_snap("weekend_discount_shadow", _sh_totals, _sh_asset_map, _sh_tier_map)
            _wknd["discount_factor"] = 0.60
            snap["weekend_discount_shadow"] = _wknd

            # Weekend discount live
            _wknd_live = {"trades": 0, "wins": 0, "losses": 0, "pnl_cents": 0, "wr": 0, "by_asset": {}}
            try:
                for _wl_row in _conn.execute(
                    "SELECT asset, COUNT(*) as n, "
                    "SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as wins, "
                    "SUM(CASE WHEN pnl_cents <= 0 THEN 1 ELSE 0 END) as losses, "
                    "SUM(pnl_cents) as pnl "
                    "FROM settled_trades WHERE strategy = 'weekend_discount' "
                    "GROUP BY asset"
                ).fetchall():
                    a, n, w, l, p = _wl_row
                    _wknd_live["trades"] += n
                    _wknd_live["wins"] += w
                    _wknd_live["losses"] += l
                    _wknd_live["pnl_cents"] += p
                    _wknd_live["by_asset"][a] = {"trades": n, "wins": w, "losses": l,
                                                  "pnl_cents": p, "wr": round(w / n, 4) if n else 0}
                _wknd_live["wr"] = round(_wknd_live["wins"] / _wknd_live["trades"], 4) if _wknd_live["trades"] else 0
                # Shadow signal count (evaluated_opportunities with live filter_stage)
                _wl_sig = _conn.execute(
                    "SELECT COUNT(*) FROM evaluated_opportunities "
                    "WHERE filter_stage = 'weekend_discount'"
                ).fetchone()
                _wknd_live["signals"] = _wl_sig[0] if _wl_sig else 0
                _query_count += 2
            except Exception:
                logging.warning("weekend_discount_live snapshot failed", exc_info=True)
            snap["weekend_discount_live"] = _wknd_live

            # Overnight discount
            _ovn = _panel_snap("overnight_discount_shadow", _sh_totals, _sh_asset_map, _sh_tier_map)
            _ovn["discount_factor"] = 0.60
            snap["overnight_discount_shadow"] = _ovn

            # Overnight LP (extra: edge distribution + config)
            _olp = _panel_snap("overnight_lp_shadow", _sh_totals, _sh_asset_map, _sh_tier_map)
            _olp["edge_distribution"] = {
                "min": _olp_edge["min_edge"] if _olp_edge else None,
                "avg": _olp_edge["avg_edge"] if _olp_edge else None,
                "max": _olp_edge["max_edge"] if _olp_edge else None,
            }
            _olp["config"] = {
                "price_range": "50-85c", "hours": "00-12 UTC",
                "min_cal_prob": 0.82, "min_edge": 0.10,
                "kelly_fraction": 0.125, "max_risk": 0.10,
                "stc_range": "120-600s", "vol_spike_mult": 2.0,
            }
            snap["overnight_lp_shadow"] = _olp

            # Low-price shadow (20-79c dual-sizing sim + correlation; Phase C 2026-05-02)
            _lps = _panel_snap("low_price_shadow", _sh_totals, _sh_asset_map, _sh_tier_map)
            _lps["config"] = {
                "price_range": "20-79c", "max_stc": 900,
                "lp_kelly_fraction": 0.25, "lp_max_risk": 0.10,
                "window_cap": 2, "hour_cap": 4,
            }
            # Pull capped-sizing PnL and correlation stats from dedicated table
            try:
                _lps_stats = _conn.execute(
                    "SELECT COUNT(*) as n, "
                    "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                    "SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes') "
                    "  THEN 1 ELSE 0 END) as wins, "
                    "SUM(CASE WHEN status='settled' THEN counterfactual_pnl_full ELSE 0 END) as pnl_full, "
                    "SUM(CASE WHEN status='settled' THEN counterfactual_pnl_capped ELSE 0 END) as pnl_capped, "
                    "ROUND(AVG(window_signal_count), 2) as avg_window_ct, "
                    "MAX(window_signal_count) as max_window_ct, "
                    "ROUND(AVG(hour_signal_count), 2) as avg_hour_ct, "
                    "MAX(hour_signal_count) as max_hour_ct "
                    "FROM low_price_shadow_signals"
                ).fetchone()
                _query_count += 1
                if _lps_stats and _lps_stats["n"]:
                    _lps["capped_pnl_cents"] = _lps_stats["pnl_capped"] or 0
                    _lps["full_pnl_cents"] = _lps_stats["pnl_full"] or 0
                    _lps["correlation"] = {
                        "avg_window_ct": _lps_stats["avg_window_ct"],
                        "max_window_ct": _lps_stats["max_window_ct"],
                        "avg_hour_ct": _lps_stats["avg_hour_ct"],
                        "max_hour_ct": _lps_stats["max_hour_ct"],
                    }
                else:
                    _lps["capped_pnl_cents"] = 0
                    _lps["full_pnl_cents"] = 0
                    _lps["correlation"] = {"avg_window_ct": 0, "max_window_ct": 0,
                                            "avg_hour_ct": 0, "max_hour_ct": 0}
            except Exception:
                logging.warning("low_price_shadow dedicated table query failed", exc_info=True)
                _lps["capped_pnl_cents"] = 0
                _lps["full_pnl_cents"] = 0
                _lps["correlation"] = {"avg_window_ct": 0, "max_window_ct": 0,
                                        "avg_hour_ct": 0, "max_hour_ct": 0}
            snap["low_price_shadow"] = _lps

            # Decided contract (tier = filter_stage t1/t1b/t2, not price bucket)
            _dc_tiers_live = ("decided_contract_t1", "decided_contract_t1b", "decided_contract_t2")
            _dc_total_n = _dc_total_s = _dc_total_w = _dc_total_pnl = 0
            for _dct in _dc_tiers_live:
                _dctv = _sh_totals.get(_dct, {"n": 0, "settled": 0, "wins": 0, "sim_pnl": 0})
                _dc_total_n += _dctv["n"]
                _dc_total_s += _dctv["settled"]
                _dc_total_w += _dctv["wins"]
                _dc_total_pnl += _dctv["sim_pnl"]
            # Merge by_asset across t1/t1b/t2
            _dc_by_asset = {}
            for fs in _dc_tiers_live:
                for asset, vals in _sh_asset_map.get(fs, {}).items():
                    if asset not in _dc_by_asset:
                        _dc_by_asset[asset] = {"n": 0, "settled": 0, "wins": 0, "sim_pnl_cents": 0}
                    _dc_by_asset[asset]["n"] += vals["n"]
                    _dc_by_asset[asset]["settled"] += vals["settled"]
                    _dc_by_asset[asset]["wins"] += vals["wins"]
                    _dc_by_asset[asset]["sim_pnl_cents"] += vals["sim_pnl_cents"]
            for vals in _dc_by_asset.values():
                vals["wr"] = round(vals["wins"] / vals["settled"], 4) if vals["settled"] else 0
            # by_tier: t1/t1b/t2 from totals
            _dc_by_tier = {}
            for fs in _dc_tiers_live:
                t = _sh_totals.get(fs)
                if t and t["n"] > 0:
                    _dc_by_tier[fs] = {
                        "n": t["n"], "settled": t["settled"], "wins": t["wins"],
                        "wr": round(t["wins"] / t["settled"], 4) if t["settled"] else 0,
                        "sim_pnl_cents": t["sim_pnl"],
                    }
            snap["decided_contract_shadow"] = {
                "total_signals": _dc_total_n, "settled": _dc_total_s,
                "wins": _dc_total_w,
                "wr": round(_dc_total_w / _dc_total_s, 4) if _dc_total_s else 0,
                "sim_pnl_cents": _dc_total_pnl,
                "by_asset": _dc_by_asset, "by_tier": _dc_by_tier,
            }
            # ── Decided Contract LIVE performance ──
            _dc_live = {"trades": 0, "wins": 0, "losses": 0, "pnl_cents": 0,
                        "by_tier": {}, "by_asset": {}, "window_cap_skips": 0}
            try:
                for _row in _conn.execute(
                    "SELECT strategy, asset, COUNT(*) as n, "
                    "SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as wins, "
                    "SUM(CASE WHEN pnl_cents <= 0 THEN 1 ELSE 0 END) as losses, "
                    "SUM(pnl_cents) as pnl "
                    "FROM settled_trades WHERE strategy IN ('decided_t1','decided_t1b','decided_t2','decided_t2_z25','decided_t2_z2') "
                    "GROUP BY strategy, asset"
                ).fetchall():
                    strat, asset_name, n, w, l, pnl = _row
                    _dc_live["trades"] += n
                    _dc_live["wins"] += w
                    _dc_live["losses"] += l
                    _dc_live["pnl_cents"] += pnl
                    _dc_live["by_tier"].setdefault(strat, {"trades": 0, "wins": 0, "losses": 0, "pnl_cents": 0})
                    _dc_live["by_tier"][strat]["trades"] += n
                    _dc_live["by_tier"][strat]["wins"] += w
                    _dc_live["by_tier"][strat]["losses"] += l
                    _dc_live["by_tier"][strat]["pnl_cents"] += pnl
                    _dc_live["by_asset"].setdefault(asset_name, {"trades": 0, "wins": 0, "losses": 0, "pnl_cents": 0})
                    _dc_live["by_asset"][asset_name]["trades"] += n
                    _dc_live["by_asset"][asset_name]["wins"] += w
                    _dc_live["by_asset"][asset_name]["losses"] += l
                    _dc_live["by_asset"][asset_name]["pnl_cents"] += pnl
                _dc_live["wr"] = round(_dc_live["wins"] / _dc_live["trades"], 4) if _dc_live["trades"] else 0
                for v in _dc_live["by_tier"].values():
                    v["wr"] = round(v["wins"] / v["trades"], 4) if v["trades"] else 0
                for v in _dc_live["by_asset"].values():
                    v["wr"] = round(v["wins"] / v["trades"], 4) if v["trades"] else 0
                # Count window cap skips
                _skip_count = _conn.execute(
                    "SELECT COUNT(*) FROM evaluated_opportunities "
                    "WHERE filter_stage='decided_window_cap_skip'"
                ).fetchone()
                _dc_live["window_cap_skips"] = _skip_count[0] if _skip_count else 0
                _query_count += 2
            except Exception:
                logging.warning("decided_contract_live snapshot failed", exc_info=True)
            snap["decided_contract_live"] = _dc_live

            # ── Terminal Momentum LIVE performance ──
            _tm_live = {"trades": 0, "wins": 0, "losses": 0, "pnl_cents": 0,
                        "by_price": {}, "by_asset": {}, "signals": 0,
                        "fill_rate": 0, "avg_fill_size": 0}
            try:
                _tm_total_contracts = 0
                for _tm_row in _conn.execute(
                    "SELECT entry_price_cents, asset, COUNT(*) as n, "
                    "SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as wins, "
                    "SUM(CASE WHEN pnl_cents <= 0 THEN 1 ELSE 0 END) as losses, "
                    "SUM(pnl_cents) as pnl, SUM(count) as total_cts "
                    "FROM settled_trades WHERE strategy LIKE 'terminal_momentum%' "
                    "GROUP BY entry_price_cents, asset"
                ).fetchall():
                    price, asset_name, n, w, l, pnl, cts = _tm_row
                    _tm_live["trades"] += n
                    _tm_live["wins"] += w
                    _tm_live["losses"] += l
                    _tm_live["pnl_cents"] += pnl
                    _tm_total_contracts += (cts or 0)
                    # by_price
                    pk = str(price)
                    _tm_live["by_price"].setdefault(pk, {"trades": 0, "wins": 0, "losses": 0, "pnl_cents": 0})
                    _tm_live["by_price"][pk]["trades"] += n
                    _tm_live["by_price"][pk]["wins"] += w
                    _tm_live["by_price"][pk]["losses"] += l
                    _tm_live["by_price"][pk]["pnl_cents"] += pnl
                    # by_asset
                    _tm_live["by_asset"].setdefault(asset_name, {"trades": 0, "wins": 0, "losses": 0, "pnl_cents": 0})
                    _tm_live["by_asset"][asset_name]["trades"] += n
                    _tm_live["by_asset"][asset_name]["wins"] += w
                    _tm_live["by_asset"][asset_name]["losses"] += l
                    _tm_live["by_asset"][asset_name]["pnl_cents"] += pnl
                _tm_live["wr"] = round(_tm_live["wins"] / _tm_live["trades"], 4) if _tm_live["trades"] else 0
                for v in _tm_live["by_price"].values():
                    v["wr"] = round(v["wins"] / v["trades"], 4) if v["trades"] else 0
                for v in _tm_live["by_asset"].values():
                    v["wr"] = round(v["wins"] / v["trades"], 4) if v["trades"] else 0
                _tm_live["avg_fill_size"] = round(_tm_total_contracts / _tm_live["trades"], 1) if _tm_live["trades"] else 0
                # Signal count + fill rate
                _tm_sig = _conn.execute(
                    "SELECT COUNT(*) FROM evaluated_opportunities "
                    "WHERE filter_stage LIKE 'terminal_momentum%'"
                ).fetchone()
                _tm_live["signals"] = _tm_sig[0] if _tm_sig else 0
                _tm_live["fill_rate"] = round(_tm_live["trades"] / _tm_live["signals"], 4) if _tm_live["signals"] else 0
                _query_count += 2
            except Exception:
                logging.warning("terminal_momentum_live snapshot failed", exc_info=True)
            snap["terminal_momentum_live"] = _tm_live

            # ── Bracket NO LIVE performance ──
            _bn_live = {"trades": 0, "wins": 0, "losses": 0, "pnl_cents": 0,
                        "by_price": {}, "by_asset": {}, "signals": 0,
                        "fill_rate": 0, "avg_fill_size": 0, "avg_no_cost": 0}
            try:
                _bn_total_contracts = 0
                _bn_total_cost = 0
                for _bn_row in _conn.execute(
                    "SELECT entry_price_cents, asset, COUNT(*) as n, "
                    "SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as wins, "
                    "SUM(CASE WHEN pnl_cents <= 0 THEN 1 ELSE 0 END) as losses, "
                    "SUM(pnl_cents) as pnl, SUM(count) as total_cts "
                    "FROM settled_trades WHERE strategy = 'bracket_no' "
                    "GROUP BY entry_price_cents, asset"
                ).fetchall():
                    no_cost, asset_name, n, w, l, pnl, cts = _bn_row
                    _bn_live["trades"] += n
                    _bn_live["wins"] += w
                    _bn_live["losses"] += l
                    _bn_live["pnl_cents"] += pnl
                    _bn_total_contracts += (cts or 0)
                    _bn_total_cost += (no_cost or 0) * n
                    # by_price: key on YES price (100 - NO cost) for consistency with analysis
                    yes_price = 100 - no_cost if no_cost else 0
                    pk = str(yes_price)
                    _bn_live["by_price"].setdefault(pk, {"trades": 0, "wins": 0, "losses": 0, "pnl_cents": 0})
                    _bn_live["by_price"][pk]["trades"] += n
                    _bn_live["by_price"][pk]["wins"] += w
                    _bn_live["by_price"][pk]["losses"] += l
                    _bn_live["by_price"][pk]["pnl_cents"] += pnl
                    # by_asset (city identifier for weather)
                    _bn_live["by_asset"].setdefault(asset_name, {"trades": 0, "wins": 0, "losses": 0, "pnl_cents": 0})
                    _bn_live["by_asset"][asset_name]["trades"] += n
                    _bn_live["by_asset"][asset_name]["wins"] += w
                    _bn_live["by_asset"][asset_name]["losses"] += l
                    _bn_live["by_asset"][asset_name]["pnl_cents"] += pnl
                _bn_live["wr"] = round(_bn_live["wins"] / _bn_live["trades"], 4) if _bn_live["trades"] else 0
                for v in _bn_live["by_price"].values():
                    v["wr"] = round(v["wins"] / v["trades"], 4) if v["trades"] else 0
                for v in _bn_live["by_asset"].values():
                    v["wr"] = round(v["wins"] / v["trades"], 4) if v["trades"] else 0
                _bn_live["avg_fill_size"] = round(_bn_total_contracts / _bn_live["trades"], 1) if _bn_live["trades"] else 0
                _bn_live["avg_no_cost"] = round(_bn_total_cost / _bn_live["trades"], 1) if _bn_live["trades"] else 0
                # Signal count + fill rate
                _bn_sig = _conn.execute(
                    "SELECT COUNT(*) FROM evaluated_opportunities "
                    "WHERE filter_stage = 'bracket_no'"
                ).fetchone()
                _bn_live["signals"] = _bn_sig[0] if _bn_sig else 0
                _bn_live["fill_rate"] = round(_bn_live["trades"] / _bn_live["signals"], 4) if _bn_live["signals"] else 0
                _query_count += 2
            except Exception:
                logging.warning("bracket_no_live snapshot failed", exc_info=True)
            snap["bracket_no_live"] = _bn_live

            # ── Stacking stats ──
            _stack_stats = {"stacked_trades": 0, "stacked_pnl": 0, "stacked_wr": 0}
            try:
                _ss = _conn.execute(
                    "SELECT COUNT(*) as n, "
                    "SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as wins, "
                    "SUM(pnl_cents) as pnl "
                    "FROM settled_trades WHERE is_stacked = 1"
                ).fetchone()
                if _ss and _ss[0]:
                    _stack_stats["stacked_trades"] = _ss[0]
                    _stack_stats["stacked_pnl"] = _ss[2] or 0
                    _stack_stats["stacked_wr"] = round(_ss[1] / _ss[0], 4) if _ss[0] else 0
                _query_count += 1
            except Exception:
                logging.warning("stacking_stats snapshot failed", exc_info=True)
            snap["stacking_stats"] = _stack_stats

            # ── Decided Contract Expansion Shadow ──
            _DC_EXPANSION_STAGES = (
                'dc_shadow_t1b_93c', 'dc_shadow_t2_z25', 'dc_shadow_t2_90c',
                'dc_shadow_t2_90c_xrp', 'dc_shadow_t2_z2', 'dc_shadow_no_side',
            )
            try:
                _dc_exp = {}
                for _dce_row in _conn.execute(
                    "SELECT filter_stage, COUNT(*) as n, "
                    "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                    "SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes') "
                    "  THEN 1 ELSE 0 END) as wins, "
                    "SUM(CASE WHEN status='settled' AND market_result NOT IN ('yes','all_yes') "
                    "  THEN 1 ELSE 0 END) as losses, "
                    "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl, "
                    "MAX(evaluation_time) as last_signal "
                    "FROM evaluated_opportunities "
                    "WHERE filter_stage IN ('dc_shadow_t1b_93c','dc_shadow_t2_z25',"
                    "'dc_shadow_t2_90c','dc_shadow_t2_90c_xrp','dc_shadow_t2_z2','dc_shadow_no_side') "
                    "GROUP BY filter_stage"
                ).fetchall():
                    fs = _dce_row["filter_stage"]
                    n = _dce_row["n"]
                    s = _dce_row["settled"] or 0
                    w = _dce_row["wins"] or 0
                    _dc_exp[fs] = {
                        "signals": n, "settled": s, "wins": w,
                        "losses": (_dce_row["losses"] or 0),
                        "wr": round(w / s, 4) if s else 0,
                        "sim_pnl_cents": _dce_row["sim_pnl"] or 0,
                        "last_signal": _dce_row["last_signal"],
                    }
                _query_count += 1
                # Ensure all stages have an entry (even if no data yet)
                for _dce_s in _DC_EXPANSION_STAGES:
                    _dc_exp.setdefault(_dce_s, {
                        "signals": 0, "settled": 0, "wins": 0, "losses": 0,
                        "wr": 0, "sim_pnl_cents": 0, "last_signal": None,
                    })
            except Exception:
                logging.warning("dc_expansion_shadow snapshot failed", exc_info=True)
                _dc_exp = {s: {"signals": 0, "settled": 0, "wins": 0, "losses": 0,
                               "wr": 0, "sim_pnl_cents": 0, "last_signal": None}
                           for s in _DC_EXPANSION_STAGES}
            snap["dc_expansion_shadow"] = _dc_exp

            # Relaxed edge
            _re = _panel_snap("relaxed_edge_shadow", _sh_totals, _sh_asset_map, _sh_tier_map)
            snap["relaxed_edge_shadow"] = _re

        except Exception:
            logging.warning("shadow_panels combined snapshot failed", exc_info=True)
            for _sk, _sd in _shadow_defaults.items():
                snap.setdefault(_sk, _sd)

        # ── Calibration Gap (15M model vs realized by price) ──────────
        try:
            _cal_gap_by_price = {}
            for _cg in _conn.execute(
                "SELECT market_price, COUNT(*) as n, "
                "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes') "
                "  THEN 1 ELSE 0 END) as wins, "
                "AVG(calibrated_prob) as avg_model_prob "
                "FROM evaluated_opportunities "
                "WHERE product_type='15m' "
                "  AND filter_stage IN ('candidate','insufficient_edge') "
                "  AND market_price BETWEEN 86 AND 99 "
                "  AND evaluation_time >= datetime('now', '-14 days') "
                "GROUP BY market_price ORDER BY market_price"
            ).fetchall():
                _price = _cg["market_price"]
                _settled = _cg["settled"] or 0
                _wins = _cg["wins"] or 0
                _wr = round(_wins / _settled, 4) if _settled else 0
                _avg_prob = round(_cg["avg_model_prob"], 4) if _cg["avg_model_prob"] else 0
                _cal_gap_by_price[str(_price)] = {
                    "n": _cg["n"],
                    "settled": _settled,
                    "wins": _wins,
                    "wr": _wr,
                    "avg_model_prob": _avg_prob,
                    "gap": round(_wr - _avg_prob, 4) if _settled else None,
                }
            snap["calibration_gap"] = {"by_price": _cal_gap_by_price}
        except Exception:
            logging.warning("calibration_gap snapshot failed", exc_info=True)
            snap["calibration_gap"] = {"by_price": {}}

        # ── Capital Utilization ──────────────────────────────
        try:
            # Deployed capital: sum of total_cost_cents for open positions
            _deployed_row = _conn.execute(
                "SELECT COALESCE(SUM(total_cost_cents), 0) as deployed "
                "FROM positions WHERE status='open'"
            ).fetchone()
            _deployed_cents = _deployed_row["deployed"] if _deployed_row else 0
            # Available capital from balance (already in snap as dollars)
            _available_cents = int(snap.get("current_balance", 0) * 100)
            _total_cents = _deployed_cents + _available_cents
            _utilization_pct = round(_deployed_cents / _total_cents, 4) if _total_cents > 0 else 0
            # Average trades per day (last 7 days)
            _trades_7d_row = _conn.execute(
                "SELECT COUNT(*) as cnt FROM settled_trades "
                "WHERE settled_at >= datetime('now', '-7 days')"
            ).fetchone()
            _trades_7d = _trades_7d_row["cnt"] if _trades_7d_row else 0
            _avg_trades_per_day = round(_trades_7d / 7.0, 2)
            _avg_idle_hours = round(24.0 / _avg_trades_per_day, 2) if _avg_trades_per_day > 0 else 24.0
            snap["capital_utilization"] = {
                "deployed_cents": _deployed_cents,
                "available_cents": _available_cents,
                "utilization_pct": _utilization_pct,
                "avg_trades_per_day": _avg_trades_per_day,
                "avg_idle_hours": _avg_idle_hours,
            }
        except Exception:
            logging.warning("capital_utilization snapshot failed", exc_info=True)
            snap["capital_utilization"] = {"deployed_cents": 0, "available_cents": 0, "utilization_pct": 0,
                                            "avg_trades_per_day": 0, "avg_idle_hours": 24.0}

        # ── Loss Clustering (detect loss clusters within 1hr) ──────────
        try:
            conn = _conn
            rows = conn.execute(
                "SELECT settled_at, pnl_cents - fee_cents AS net_pnl FROM settled_trades "
                "WHERE product_type='15m' AND NOT ("
                "  (side='yes' AND market_result='yes') OR "
                "  (side='no' AND market_result IN ('no','all_no'))"
                ") AND settled_at >= ? ORDER BY settled_at",
                (CONFIG_REGIME_SINCE,)
            ).fetchall()
            clusters = []
            if rows and len(rows) >= 2:
                import datetime as _dt
                current_cluster = [rows[0]]
                for i in range(1, len(rows)):
                    try:
                        t_prev = _dt.datetime.fromisoformat(rows[i-1]["settled_at"].replace("Z", "+00:00"))
                        t_curr = _dt.datetime.fromisoformat(rows[i]["settled_at"].replace("Z", "+00:00"))
                        if (t_curr - t_prev).total_seconds() < 3600:
                            current_cluster.append(rows[i])
                        else:
                            if len(current_cluster) >= 2:
                                clusters.append({
                                    "size": len(current_cluster),
                                    "total_loss_cents": sum(r["net_pnl"] for r in current_cluster),
                                })
                            current_cluster = [rows[i]]
                    except Exception:
                        current_cluster = [rows[i]]
                if len(current_cluster) >= 2:
                    clusters.append({
                        "size": len(current_cluster),
                        "total_loss_cents": sum(r["net_pnl"] for r in current_cluster),
                    })
            snap["loss_clustering"] = {
                "total_losses": len(rows) if rows else 0,
                "cluster_count": len(clusters),
                "max_cluster_size": max((c["size"] for c in clusters), default=0),
                "worst_cluster_loss_cents": min((c["total_loss_cents"] for c in clusters), default=0),
                "clusters": clusters[:5],
            }
        except Exception:
            snap["loss_clustering"] = {"total_losses": 0, "cluster_count": 0, "max_cluster_size": 0}

        # ── Pipeline Completeness (data quality for observation modules)
        # Combined into single query (was 19 queries in nested loop)
        try:
            _pc_rows = _conn.execute(
                "SELECT product_type, COUNT(*) AS total, "
                "COUNT(calibrated_prob) AS cp, COUNT(market_price) AS mp, "
                "COUNT(fee_adjusted_edge) AS fae, COUNT(raw_prob) AS rp "
                "FROM evaluated_opportunities "
                "WHERE product_type IN ('hourly','spx_hourly','weather','sports') "
                "GROUP BY product_type"
            ).fetchall()
            _query_count += 1
            _pc_col_map = {
                "hourly": ["calibrated_prob", "market_price", "fee_adjusted_edge", "raw_prob"],
                "spx_hourly": ["calibrated_prob", "market_price", "fee_adjusted_edge"],
                "weather": ["calibrated_prob", "market_price", "fee_adjusted_edge", "raw_prob"],
                "sports": ["calibrated_prob", "market_price", "fee_adjusted_edge", "raw_prob"],
            }
            _col_alias = {"calibrated_prob": "cp", "market_price": "mp",
                          "fee_adjusted_edge": "fae", "raw_prob": "rp"}
            completeness = {}
            _pc_by_pt = {r["product_type"]: r for r in _pc_rows}
            for pt, key_cols in _pc_col_map.items():
                r = _pc_by_pt.get(pt)
                if not r or r["total"] == 0:
                    completeness[pt] = {"total": r["total"] if r else 0, "columns": {}}
                    continue
                col_fills = {}
                for col in key_cols:
                    alias = _col_alias.get(col, col)
                    col_fills[col] = round(r[alias] / r["total"] * 100, 1) if r[alias] else 0
                completeness[pt] = {"total": r["total"], "columns": col_fills}
            snap["pipeline_completeness"] = completeness
        except Exception:
            snap["pipeline_completeness"] = {}

        # ── SOL Path C Shadow ──────────────────────────────────────
        try:
            _pc_total = _conn.execute(
                "SELECT COUNT(*) as n FROM sol_pathc_shadow"
            ).fetchone()
            _pc_settled_rows = _conn.execute(
                "SELECT COUNT(*) as n, "
                "SUM(CASE WHEN market_result IN ('yes','all_yes') THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN market_result IN ('no','all_no') THEN 1 ELSE 0 END) as losses, "
                "SUM(live_pnl_cents) as live_pnl, "
                "SUM(pathc_maker_pnl_cents) as maker_pnl, "
                "SUM(pathc_esc_pnl_cents) as esc_pnl, "
                "SUM(pathc_best_pnl_cents) as best_pnl, "
                "SUM(CASE WHEN obs_maker_price_touched=1 THEN 1 ELSE 0 END) as touch_count, "
                "SUM(CASE WHEN obs_maker_would_fill=1 THEN 1 ELSE 0 END) as fill_count, "
                "AVG(live_stc) as avg_stc "
                "FROM sol_pathc_shadow WHERE status='settled'"
            ).fetchone()
            _pc_n = _pc_total["n"] if _pc_total else 0
            _pc_s = _pc_settled_rows["n"] if _pc_settled_rows and _pc_settled_rows["n"] else 0
            snap["sol_pathc_shadow"] = {
                "total_signals": _pc_n,
                "settled": _pc_s,
                "wins": _pc_settled_rows["wins"] or 0 if _pc_settled_rows else 0,
                "losses": _pc_settled_rows["losses"] or 0 if _pc_settled_rows else 0,
                "live_pnl_cents": _pc_settled_rows["live_pnl"] or 0 if _pc_settled_rows else 0,
                "pathc_maker_pnl_cents": _pc_settled_rows["maker_pnl"] or 0 if _pc_settled_rows else 0,
                "pathc_esc_pnl_cents": _pc_settled_rows["esc_pnl"] or 0 if _pc_settled_rows else 0,
                "pathc_best_pnl_cents": _pc_settled_rows["best_pnl"] or 0 if _pc_settled_rows else 0,
                "maker_touch_rate": round((_pc_settled_rows["touch_count"] or 0) / _pc_s, 4) if _pc_s else 0,
                "maker_fill_rate": round((_pc_settled_rows["fill_count"] or 0) / _pc_s, 4) if _pc_s else 0,
                "avg_stc": round(_pc_settled_rows["avg_stc"] or 0, 1) if _pc_settled_rows else 0,
                "pnl_delta_cents": ((_pc_settled_rows["best_pnl"] or 0) - (_pc_settled_rows["live_pnl"] or 0)) if _pc_settled_rows else 0,
            }
        except Exception:
            logging.warning("sol_pathc_shadow snapshot failed", exc_info=True)
            snap["sol_pathc_shadow"] = {"total_signals": 0, "settled": 0, "wins": 0, "losses": 0,
                                         "live_pnl_cents": 0, "pathc_best_pnl_cents": 0, "pnl_delta_cents": 0}

        # ── ETH Filter Shadow (query-only, no bot.py table) ──────────
        try:
            _eth_rows = _conn.execute(
                "SELECT st.ticker, st.entry_price_cents, st.pnl_cents, st.fee_cents, "
                "st.market_result, st.count, "
                "eo.calibrated_prob, eo.edge, eo.seconds_to_close "
                "FROM settled_trades st "
                "LEFT JOIN evaluated_opportunities eo ON st.ticker=eo.ticker AND eo.filter_stage='candidate' "
                "WHERE st.asset='ETH' AND st.product_type='15m' "
                "AND st.settled_at >= datetime('now', '-14 days')"
            ).fetchall()
            _eth_total = len(_eth_rows)
            _eth_wins = sum(1 for r in _eth_rows if r["market_result"] in ("yes", "all_yes"))
            _eth_losses = _eth_total - _eth_wins
            _eth_pnl = sum((r["pnl_cents"] or 0) - (r["fee_cents"] or 0) for r in _eth_rows)

            # Simulate filters: price floors and edge thresholds
            _filters = {}
            for _fname, _fn in [
                ("price_89", lambda r: (r["entry_price_cents"] or 0) >= 89),
                ("price_90", lambda r: (r["entry_price_cents"] or 0) >= 90),
                ("price_91", lambda r: (r["entry_price_cents"] or 0) >= 91),
                ("price_92", lambda r: (r["entry_price_cents"] or 0) >= 92),
                ("price_93", lambda r: (r["entry_price_cents"] or 0) >= 93),
                ("edge_1pct", lambda r: (r["edge"] or 0) >= 0.01),
                ("edge_3pct", lambda r: (r["edge"] or 0) >= 0.03),
                ("edge_4pct", lambda r: (r["edge"] or 0) >= 0.04),
                ("edge_5pct", lambda r: (r["edge"] or 0) >= 0.05),
                ("price_90_and_edge_1pct", lambda r: (r["entry_price_cents"] or 0) >= 90 and (r["edge"] or 0) >= 0.01),
                ("price_92_and_edge_1pct", lambda r: (r["entry_price_cents"] or 0) >= 92 and (r["edge"] or 0) >= 0.01),
                ("price_90_and_edge_3pct", lambda r: (r["entry_price_cents"] or 0) >= 90 and (r["edge"] or 0) >= 0.03),
            ]:
                _kept = [r for r in _eth_rows if _fn(r)]
                _blocked = [r for r in _eth_rows if not _fn(r)]
                _kept_wins = sum(1 for r in _kept if r["market_result"] in ("yes", "all_yes"))
                _kept_pnl = sum((r["pnl_cents"] or 0) - (r["fee_cents"] or 0) for r in _kept)
                _blocked_pnl = sum((r["pnl_cents"] or 0) - (r["fee_cents"] or 0) for r in _blocked)
                _filters[_fname] = {
                    "kept": len(_kept),
                    "blocked": len(_blocked),
                    "kept_wr": round(_kept_wins / len(_kept), 4) if _kept else 0,
                    "kept_pnl_cents": _kept_pnl,
                    "blocked_pnl_cents": _blocked_pnl,
                    "net_impact_cents": _kept_pnl - _eth_pnl,
                }
            snap["eth_filter_shadow"] = {
                "total_trades": _eth_total,
                "wins": _eth_wins,
                "losses": _eth_losses,
                "wr": round(_eth_wins / _eth_total, 4) if _eth_total else 0,
                "total_pnl_cents": _eth_pnl,
                "filters": _filters,
            }
        except Exception:
            logging.warning("eth_filter_shadow snapshot failed", exc_info=True)
            snap["eth_filter_shadow"] = {"total_trades": 0, "wins": 0, "losses": 0,
                                          "wr": 0, "total_pnl_cents": 0, "filters": {}}

        # Cohort attribution panel (P1.2, Money Printer Roadmap Phase 1) —
        # surfaces the nightly-materialized cohort_attribution_daily rows
        # (top bleeders + top cal-drift + summary) so the operator can see
        # bleeders the moment they cross n≥50 instead of waiting for the
        # weekly bleed report. Read-only against the P1.1 aggregate table;
        # graceful empty-panel fallback when table missing or cron stale.
        try:
            snap["cohort_attribution"] = _build_cohort_attribution_snap(_conn)
        except Exception:
            logging.warning("cohort_attribution snapshot failed", exc_info=True)
            snap["cohort_attribution"] = _empty_cohort_panel(
                datetime.datetime.now(datetime.timezone.utc).isoformat()
            )
            snap["_snapshot_errors"].append("cohort_attribution")

        # ── Save slow-changing sections to TTL cache ──────────────────
        if _run_slow:
            _SLOW_SNAP_KEYS = {
                "weekend_discount_live", "weekend_discount_shadow",
                "overnight_discount_shadow",
                "overnight_lp_shadow", "decided_contract_shadow", "decided_contract_live",
                "terminal_momentum_live", "bracket_no_live", "stacking_stats",
                "dc_expansion_shadow", "relaxed_edge_shadow",
                "calibration_gap", "capital_utilization", "loss_clustering",
                "pipeline_completeness", "sol_pathc_shadow", "eth_filter_shadow",
                "low_price_shadow",
                # P1.2: nightly-aggregated table; 60s TTL is generous over-refresh
                "cohort_attribution",
            }
            self._slow_cache = {k: v for k, v in snap.items() if k in _SLOW_SNAP_KEYS}
            self._slow_cache_ts = _now_mono

        # End read transaction
        try:
            _conn.execute("COMMIT")
        except Exception:
            pass  # may not have started a transaction

        logging.debug("dashboard snapshot: %d tracked queries this cycle (run_slow=%s)",
                      _query_count, _run_slow)

        return snap

    def _build_public_snapshot(self, db_conn) -> Dict[str, Any]:
        """Build a sanitized public-facing snapshot for /performance/ page.

        Strictly whitelisted — ONLY these fields are ever written. If you add
        a new operator metric, it does not leak here by accident. To expose
        something publicly, add it to this method deliberately.

        Data scope: ALL product_types (15m + hourly + weather). The bot trades
        multiple products; showing only 15m would be a misleading subset.

        Truth source for cumulative return: live Kalshi balance, not summed
        settled_trades (which can diverge from reality by ~$200 due to pre-DB
        history / ghost settlements). Matches the operator dashboard's
        actual_pnl_cents computation at line ~221. Frontend shows one number
        that reconciles with the real bankroll.

        Schema (v1):
          - schema_version: int — bump on breaking changes; frontend warns on mismatch
          - updated_at: ISO timestamp
          - since: inception date (first settled trade)
          - days_active: int
          - total_trades / wins / losses: ints (all product types)
          - win_rate / win_rate_ci_lo / win_rate_ci_hi: Wilson 95% CI
          - cumulative_return_pct: % return on initial deposit, balance-derived
          - sharpe_ratio: daily-annualized Sharpe (√365, crypto trades 24/7)
          - max_drawdown_pct: true peak-to-trough DD on the equity curve, %
          - profit_factor: gross wins / gross losses
          - daily_return_series: [{day, cumulative_pct, daily_pct}] for chart
          - calibration_reliability: [{bucket_lo, bucket_hi, n, predicted, actual}] — 15m only

        NOT EXPOSED (stripped): balance in $, positions, shadow signals,
        per-trade entry/exit, model probs, edge, kelly_f, error messages,
        feed state. Bankroll stays private; only % return escapes.

        See kb/decisions/dashboard-overhaul-plan.md (Phase P) and
        kb/concepts/public-dashboard-schema.md for rationale.
        """
        from bot.constants import INITIAL_DEPOSIT_CENTS
        import math as _math

        pub: Dict[str, Any] = {
            "schema_version": 1,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

        # One scan — ALL product types (not just 15m)
        try:
            rows = db_conn.execute("""
                SELECT side, market_result, product_type,
                       DATE(settled_at) AS day, settled_at,
                       (pnl_cents - fee_cents) AS net,
                       calibrated_prob
                FROM settled_trades
                ORDER BY settled_at
            """).fetchall()
        except Exception:
            logging.warning("Public snapshot: settled_trades scan failed", exc_info=True)
            rows = []

        def _is_win(r):
            side, result = r["side"], r["market_result"]
            return ((result in ("yes", "all_yes") and side == "yes") or
                    (result in ("no", "all_no") and side == "no"))

        total = len(rows)
        wins = sum(1 for r in rows if _is_win(r))
        losses = total - wins
        pub["total_trades"] = total
        pub["total_wins"] = wins
        pub["total_losses"] = losses

        # Inception + days active
        if rows:
            first_ts = rows[0]["settled_at"]
            pub["since"] = first_ts[:10] if first_ts else None
            try:
                first_day = datetime.datetime.fromisoformat(first_ts.replace("Z", "+00:00")).date()
                pub["days_active"] = (datetime.datetime.now(datetime.timezone.utc).date() - first_day).days
            except Exception:
                pub["days_active"] = 0
        else:
            pub["since"] = None
            pub["days_active"] = 0

        # Win rate + Wilson 95% CI
        if total > 0:
            p = wins / total
            z = 1.96
            denom = 1 + z * z / total
            center = (p + z * z / (2 * total)) / denom
            halfwidth = z * _math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
            pub["win_rate"] = round(p, 4)
            pub["win_rate_ci_lo"] = round(max(0.0, center - halfwidth), 4)
            pub["win_rate_ci_hi"] = round(min(1.0, center + halfwidth), 4)
        else:
            pub["win_rate"] = 0.0
            pub["win_rate_ci_lo"] = 0.0
            pub["win_rate_ci_hi"] = 0.0

        # Per-trade equity curve (all product types). We do NOT daily-bucket —
        # daily closes hide intraday peaks (observed 2026-04-05: ledger hit
        # +119.65% intraday but daily close reported +92% after same-day losses
        # took back gains). Per-trade resolution tells the true story.
        # Thinning: if > MAX_POINTS, sample evenly. Chart stays light.
        MAX_CURVE_POINTS = 500
        equity_curve: List[Dict[str, Any]] = []
        peak_pct = 0.0
        peak_cents = 0
        trough_after_peak_cents = 0
        max_dd_cents = 0
        daily_map: Dict[str, int] = {}  # for Sharpe

        if rows and INITIAL_DEPOSIT_CENTS > 0:
            cum_cents = 0
            raw_points = []
            for r in rows:
                net = r["net"] or 0
                cum_cents += net
                raw_points.append({"t": r["settled_at"], "cum": cum_cents})
                if r["day"]:
                    daily_map[r["day"]] = daily_map.get(r["day"], 0) + net
                # Track peak-to-trough drawdown on per-trade equity
                if cum_cents > peak_cents:
                    peak_cents = cum_cents
                    trough_after_peak_cents = cum_cents  # reset trough
                else:
                    if cum_cents < trough_after_peak_cents:
                        trough_after_peak_cents = cum_cents
                    dd = peak_cents - cum_cents
                    if dd > max_dd_cents:
                        max_dd_cents = dd

            # Thin to at most MAX_CURVE_POINTS (sample evenly, always include first/last)
            if len(raw_points) > MAX_CURVE_POINTS:
                step = len(raw_points) / MAX_CURVE_POINTS
                thinned = [raw_points[int(i * step)] for i in range(MAX_CURVE_POINTS - 1)]
                thinned.append(raw_points[-1])
            else:
                thinned = raw_points
            for p in thinned:
                equity_curve.append({
                    "t": p["t"],
                    "cumulative_pct": round(p["cum"] / INITIAL_DEPOSIT_CENTS * 100, 2),
                })
            peak_pct = round(peak_cents / INITIAL_DEPOSIT_CENTS * 100, 2)

        pub["equity_curve"] = equity_curve
        pub["peak_return_pct"] = peak_pct

        # Cumulative return % = last point of ledger equity curve
        if equity_curve:
            pub["cumulative_return_pct"] = equity_curve[-1]["cumulative_pct"]
        else:
            pub["cumulative_return_pct"] = 0.0

        # Also expose balance-derived cumulative for audit (frontend may show
        # the reconciliation gap transparently — the ~$200 delta is a known
        # pre-schema data artifact, not hidden).
        try:
            bal_cents = None
            try:
                bal = self._ml.client.get_balance()
                bal_cents = int(round(bal["balance"])) if bal else None
            except Exception:
                bal_cents = None
            if bal_cents is None:
                bal_cents = int(round(getattr(self, "_last_good_balance", 0) * 100))
            try:
                open_rows = db_conn.execute(
                    "SELECT total_cost_cents, accumulated_fee_cents FROM positions WHERE status='open'"
                ).fetchall()
                open_cost = sum((r["total_cost_cents"] or 0) for r in open_rows)
                open_fee = sum((r["accumulated_fee_cents"] or 0) for r in open_rows)
            except Exception:
                open_cost = 0
                open_fee = 0
            balance_pnl_cents = bal_cents - INITIAL_DEPOSIT_CENTS + open_cost + open_fee
            if INITIAL_DEPOSIT_CENTS > 0 and bal_cents > 0:
                pub["balance_cumulative_return_pct"] = round(balance_pnl_cents / INITIAL_DEPOSIT_CENTS * 100, 2)
            else:
                pub["balance_cumulative_return_pct"] = None
        except Exception:
            pub["balance_cumulative_return_pct"] = None

        # Sharpe ratio — daily-annualized from daily ledger buckets (separate
        # aggregation from the chart; chart is per-trade)
        daily_nets_pct = [daily_map[d] / INITIAL_DEPOSIT_CENTS * 100 for d in sorted(daily_map.keys())]
        if len(daily_nets_pct) > 1:
            d_mean = sum(daily_nets_pct) / len(daily_nets_pct)
            d_var = sum((x - d_mean) ** 2 for x in daily_nets_pct) / (len(daily_nets_pct) - 1)
            d_std = d_var ** 0.5
            pub["sharpe_ratio"] = round(d_mean / d_std * (365 ** 0.5), 2) if d_std > 0 else 0.0
        else:
            pub["sharpe_ratio"] = 0.0

        # Max drawdown % — already computed above from per-trade equity loop.
        # Expressed as % of peak equity (the standard definition), negative.
        if peak_cents > 0 and max_dd_cents > 0:
            peak_equity = INITIAL_DEPOSIT_CENTS + peak_cents
            pub["max_drawdown_pct"] = round(-max_dd_cents / peak_equity * 100, 2)
        else:
            pub["max_drawdown_pct"] = 0.0

        # Profit factor = gross wins / gross losses (from raw ledger, not rescaled —
        # this measures trade quality, not balance reconciliation)
        gross_wins = sum(r["net"] for r in rows if r["net"] and r["net"] > 0)
        gross_losses = abs(sum(r["net"] for r in rows if r["net"] and r["net"] < 0))
        if gross_losses > 0:
            pub["profit_factor"] = round(gross_wins / gross_losses, 2)
        else:
            pub["profit_factor"] = 999.0

        # Calibration reliability — 15m only (primary strategy; hourly/weather too few trades)
        buckets = [(0.50, 0.70), (0.70, 0.80), (0.80, 0.85),
                   (0.85, 0.90), (0.90, 0.95), (0.95, 1.01)]
        reliability = []
        rows_15m = [r for r in rows if r["product_type"] == "15m"]
        for lo, hi in buckets:
            bucket_rows = [r for r in rows_15m
                           if r["calibrated_prob"] is not None
                           and lo <= r["calibrated_prob"] < hi]
            n = len(bucket_rows)
            if n >= 10:
                pred = sum(r["calibrated_prob"] for r in bucket_rows) / n
                actual = sum(1 for r in bucket_rows if _is_win(r)) / n
                reliability.append({
                    "bucket_lo": lo,
                    "bucket_hi": min(hi, 1.0),
                    "n": n,
                    "predicted": round(pred, 4),
                    "actual": round(actual, 4),
                })
        pub["calibration_reliability"] = reliability

        return pub

    def _compute_position_health(
        self,
        positions: List[Dict],
        orderbooks: Dict,
        active_windows: List[Dict],
        db_conn=None,
    ) -> Dict[str, Any]:
        """Compute mark-to-market health data for each open position."""
        now_ts = time.time()
        result: Dict[str, Any] = {}
        counts = {"lock": 0, "watch": 0, "danger": 0}
        active_tickers = set()

        # Build event_ticker → seconds_to_close lookup
        stc_lookup: Dict[str, float] = {}
        for w in active_windows:
            et = w.get("event_ticker")
            if et:
                stc_lookup[et] = w.get("seconds_to_close", 9999)

        for pos in positions:
            ticker = pos.get("ticker")
            if not ticker:
                continue
            active_tickers.add(ticker)

            ob = orderbooks.get(ticker)

            try:
                # Parse orderbook (may be missing — health still computed from buffer)
                best_bid = best_ask = mid_price = spread = None
                bid_depth = ask_depth = 0
                ob_age_s = 999
                has_ob = False

                if ob:
                    ob_ts = ob.get("ts", 0)
                    ob_age_s = round(now_ts - ob_ts, 1) if ob_ts else 999

                    no_bids = ob.get("no", [])
                    yes_bids = ob.get("yes", [])

                    parsed_no = _parse_ob_levels(no_bids)
                    yes_ask_levels = sorted(
                        [(100 - p, q) for p, q in parsed_no], key=lambda x: x[0]
                    )
                    parsed_yes = _parse_ob_levels(yes_bids)
                    yes_bid_levels = sorted(parsed_yes, key=lambda x: -x[0])

                    best_ask = yes_ask_levels[0][0] if yes_ask_levels else None
                    best_bid = yes_bid_levels[0][0] if yes_bid_levels else None

                    if best_bid is not None and best_ask is not None:
                        has_ob = True
                        spread = best_ask - best_bid
                        mid_price = (best_bid + best_ask) / 2.0
                        bid_depth = sum(q for _, q in yes_bid_levels)
                        ask_depth = sum(q for _, q in yes_ask_levels)

                entry_price = pos.get("avg_price_cents", 0)
                count = pos.get("count", 0)
                side = (pos.get("side") or "").lower()

                # Unrealized P&L (conservative: bid-based exit for YES, ask-based for NO)
                unrealized_cents = 0
                unrealized_pct = 0.0
                if has_ob:
                    if side == "yes":
                        unrealized_cents = (best_bid - entry_price) * count
                        unrealized_pct = round(
                            (best_bid - entry_price) / entry_price * 100, 2
                        ) if entry_price else 0
                    else:
                        unrealized_cents = (entry_price - best_ask) * count
                        unrealized_pct = round(
                            (entry_price - best_ask) / entry_price * 100, 2
                        ) if entry_price else 0

                # Seconds to close
                event_ticker = pos.get("event_ticker")
                stc_seconds = stc_lookup.get(event_ticker)

                # Mid-price history (only when orderbook available)
                mid_hist = []
                if has_ob:
                    if ticker not in self._mid_history:
                        self._mid_history[ticker] = collections.deque(maxlen=30)
                    self._mid_history[ticker].append(mid_price)
                    mid_hist = list(self._mid_history[ticker])[-5:]

                # Health classification
                if has_ob:
                    if side == "yes":
                        price_warn = entry_price - 3 <= mid_price < entry_price
                        price_danger = mid_price < entry_price - 3
                    else:
                        price_warn = entry_price < mid_price <= entry_price + 3
                        price_danger = mid_price > entry_price + 3

                    danger_conditions = (
                        price_danger
                        or spread > 6
                        or bid_depth < 3
                        or ob_age_s > 30
                    )
                    stc_danger = (
                        stc_seconds is not None
                        and stc_seconds < 30
                        and mid_price < 95
                    )
                    watch_conditions = (
                        price_warn
                        or 4 < spread <= 6
                        or 3 <= bid_depth < 5
                        or ob_age_s >= 30
                    )

                    if stc_danger or danger_conditions:
                        raw_state = "DANGER"
                    elif watch_conditions:
                        raw_state = "WATCH"
                    else:
                        raw_state = "LOCK"
                else:
                    # No orderbook — classify from STC only
                    raw_state = "WATCH"
                    stc_danger = False

                # Debounce: require 2 consecutive ticks before changing state
                # Exception: STC danger is immediate
                prev_state = self._health_state.get(ticker, raw_state)
                if raw_state != prev_state:
                    streak = self._health_streak.get(ticker, 0) + 1
                    self._health_streak[ticker] = streak
                    if stc_danger or streak >= 2:
                        self._health_state[ticker] = raw_state
                        self._health_streak[ticker] = 0
                    # else keep previous state
                else:
                    self._health_streak[ticker] = 0
                    self._health_state[ticker] = raw_state

                health = self._health_state[ticker]
                counts[health.lower()] += 1

                # Spot buffer data from PPO table
                buffer_data = None
                if db_conn is not None:
                    try:
                        ppo_rows = db_conn.execute(
                            "SELECT spot_buffer_pct, spot_price, threshold "
                            "FROM position_price_observations "
                            "WHERE ticker = ? ORDER BY id DESC LIMIT 30",
                            (ticker,)
                        ).fetchall()
                        if ppo_rows:
                            bufs = [r["spot_buffer_pct"] for r in ppo_rows if r["spot_buffer_pct"] is not None]
                            if bufs:
                                # All-time min/max for this position
                                all_bufs = db_conn.execute(
                                    "SELECT MIN(spot_buffer_pct) AS mn, MAX(spot_buffer_pct) AS mx, "
                                    "COUNT(*) AS n FROM position_price_observations WHERE ticker = ?",
                                    (ticker,)
                                ).fetchone()
                                # Trend: compare recent 10 avg vs older 10 avg
                                trend = "stable"
                                if len(bufs) >= 6:
                                    recent = sum(bufs[:3]) / 3   # newest 3
                                    older = sum(bufs[-3:]) / 3   # oldest 3 of last 30
                                    delta = recent - older
                                    if delta > 0.5:
                                        trend = "rising"
                                    elif delta < -0.5:
                                        trend = "falling"
                                buffer_data = {
                                    "current": round(bufs[0], 2),
                                    "trend": trend,
                                    "min": round(all_bufs["mn"], 2) if all_bufs else None,
                                    "max": round(all_bufs["mx"], 2) if all_bufs else None,
                                    "spot": round(ppo_rows[0]["spot_price"], 2) if ppo_rows[0]["spot_price"] else None,
                                    "threshold": round(ppo_rows[0]["threshold"], 2) if ppo_rows[0]["threshold"] else None,
                                    "n_obs": all_bufs["n"] if all_bufs else len(ppo_rows),
                                }
                    except Exception:
                        logging.debug("Snapshot: PPO buffer query failed for %s", ticker, exc_info=True)

                result[ticker] = {
                    "entry_price": entry_price,
                    "mid_price": round(mid_price, 1) if mid_price is not None else None,
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "spread": spread,
                    "bid_depth": bid_depth,
                    "ask_depth": ask_depth,
                    "unrealized_cents": round(unrealized_cents),
                    "unrealized_pct": round(unrealized_pct, 1),
                    "health": health,
                    "ob_age_s": ob_age_s if has_ob else None,
                    "mid_history": mid_hist,
                    "stc_seconds": round(stc_seconds, 1) if stc_seconds is not None else None,
                    "buffer": buffer_data,
                    "has_ob": has_ob,
                }
            except Exception:
                logging.warning(f"Snapshot: position health failed for {ticker}", exc_info=True)

        # Cleanup stale tickers
        stale = [t for t in self._mid_history if t not in active_tickers]
        for t in stale:
            self._mid_history.pop(t, None)
            self._health_state.pop(t, None)
            self._health_streak.pop(t, None)

        return {
            "summary": {
                "lock": counts["lock"],
                "watch": counts["watch"],
                "danger": counts["danger"],
                "total": sum(counts.values()),
            },
            "positions": result,
        }

