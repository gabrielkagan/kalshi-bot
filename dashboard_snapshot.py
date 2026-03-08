"""Build dashboard state snapshots for Supabase sync."""

import math
import os
import time
import logging
import datetime
import collections
from typing import Dict, Any, List, Optional

ASSETS = ["BTC", "ETH", "SOL", "XRP"]

# Current config regime boundary — performance metrics filtered to this era
# Mar 3 2026: MIN_EDGE_BY_PRICE halved, DIRECT_TAKER_THRESHOLD 60→75
CONFIG_REGIME_SINCE = "2026-03-03T00:00:00"

# Sim fee rate for observation products: maker = $0, taker ~30% @ 0.07 → blended ~0.021
# But since most shadow trades would enter as maker (fee=$0), use taker-only rate for conservative sim
SIM_FEE_RATE = 0.021


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

    def __init__(self, main_loop):
        self._ml = main_loop
        # Position health tracking (dashboard enrichment)
        self._mid_history: Dict[str, collections.deque] = {}
        self._health_state: Dict[str, str] = {}
        self._health_streak: Dict[str, int] = {}

    def _build_snapshot(self, db_conn) -> Dict[str, Any]:
        """Build dashboard snapshot. db_conn is a sqlite3 connection."""
        snap: Dict[str, Any] = {}
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

        # Starting balance
        try:
            snap["starting_balance"] = round(self._ml.sizer.starting_balance_cents / 100, 2)
        except Exception:
            snap["starting_balance"] = 0.0

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
            if len(hist) > 360:
                snap["balance_history_4h"] = hist[::max(1, len(hist) // 360)]
        except Exception:
            logging.debug("Snapshot: balance_history build failed", exc_info=True)
            snap["balance_history"] = []

        _conn = db_conn

        # Active positions
        try:
            rows = _conn.execute(
                "SELECT * FROM positions WHERE status='open'"
            ).fetchall()
            snap["active_positions"] = [dict(r) for r in rows]
        except Exception:
            snap["active_positions"] = []

        # Resting orders
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

        # Section 2: Win/loss counts (15M + all products)
        try:
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

            # 15M only (primary display)
            settled_15m = conn.execute(
                "SELECT side, market_result FROM settled_trades WHERE product_type='15m'"
            ).fetchall()
            win, loss = _count_wins_losses(settled_15m)
            snap["win_count"] = win
            snap["loss_count"] = loss
            snap["win_rate"] = round(win / (win + loss), 4) if (win + loss) > 0 else 0.0

            # All products (for toggle)
            all_settled = conn.execute(
                "SELECT side, market_result FROM settled_trades"
            ).fetchall()
            all_win, all_loss = _count_wins_losses(all_settled)
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

        # Section 3: Daily P&L (15M only)
        try:
            today_midnight = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl_cents - fee_cents), 0) AS daily FROM settled_trades "
                "WHERE settled_at >= ? AND product_type='15m'",
                (today_midnight,),
            ).fetchone()
            snap["daily_pnl_cents"] = row["daily"] if row else 0
            start_cents = self._ml.sizer.starting_balance_cents
            if start_cents > 0:
                snap["daily_pnl_pct"] = round(snap["daily_pnl_cents"] / start_cents * 100, 2)
            else:
                snap["daily_pnl_pct"] = 0.0
        except Exception:
            snap["daily_pnl_cents"] = 0
            snap["daily_pnl_pct"] = 0.0
            snap["_snapshot_errors"].append("daily_pnl")

        # Section 4: Current streak (15M only) — wins or losses
        try:
            recent_settled = conn.execute(
                "SELECT side, market_result FROM settled_trades "
                "WHERE product_type='15m' ORDER BY settled_at DESC LIMIT 50"
            ).fetchall()
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

            def _compute_risk_stats(pnl_rows, span_query_filter):
                nets = [r["net"] for r in pnl_rows]
                n = len(nets)
                stats = {}
                if n > 0:
                    total = sum(nets)
                    # Daily-aggregated Sharpe: group by date, compute daily mean/std, annualize sqrt(365) (crypto trades 24/7)
                    daily_rows = conn.execute(
                        "SELECT DATE(settled_at) AS d, SUM(pnl_cents - fee_cents) AS daily_net "
                        f"FROM settled_trades{span_query_filter} GROUP BY DATE(settled_at) ORDER BY d"
                    ).fetchall()
                    daily_nets = [r["daily_net"] for r in daily_rows if r["daily_net"] is not None]
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

            # 15M only
            pnl_15m = conn.execute(
                "SELECT (pnl_cents - fee_cents) AS net FROM settled_trades WHERE product_type='15m' ORDER BY settled_at"
            ).fetchall()
            risk.update(_compute_risk_stats(pnl_15m, " WHERE product_type='15m'"))
            # True max drawdown from equity curve (peak-to-trough, not just peak-to-now)
            try:
                start_cents = self._ml.sizer.starting_balance_cents
                risk["true_max_drawdown_pct"], risk["true_max_drawdown_cents"] = _true_max_drawdown(pnl_15m, start_cents)
            except Exception:
                pass
            snap["risk_metrics"] = risk

            # All products (for toggle)
            all_pnl = conn.execute(
                "SELECT (pnl_cents - fee_cents) AS net FROM settled_trades"
            ).fetchall()
            all_risk = {}
            all_risk["max_drawdown_pct"] = risk["max_drawdown_pct"]
            all_risk["max_drawdown_dollars"] = risk["max_drawdown_dollars"]
            all_risk.update(_compute_risk_stats(all_pnl, ""))
            snap["all_products_risk_metrics"] = all_risk

            # Regime-filtered metrics (current config only)
            try:
                settled_15m_regime = conn.execute(
                    "SELECT side, market_result FROM settled_trades WHERE product_type='15m' AND settled_at >= ?",
                    (CONFIG_REGIME_SINCE,)
                ).fetchall()
                r_win, r_loss = _count_wins_losses(settled_15m_regime)
                pnl_15m_regime = conn.execute(
                    "SELECT (pnl_cents - fee_cents) AS net FROM settled_trades "
                    "WHERE product_type='15m' AND settled_at >= ? ORDER BY settled_at",
                    (CONFIG_REGIME_SINCE,)
                ).fetchall()
                regime_risk = {}
                regime_risk.update(_compute_risk_stats(
                    pnl_15m_regime,
                    f" WHERE product_type='15m' AND settled_at >= '{CONFIG_REGIME_SINCE}'"
                ))
                regime_risk["win_count"] = r_win
                regime_risk["loss_count"] = r_loss
                regime_risk["win_rate"] = round(r_win / (r_win + r_loss), 4) if (r_win + r_loss) > 0 else 0.0
                try:
                    regime_risk["true_max_drawdown_pct"], regime_risk["true_max_drawdown_cents"] = _true_max_drawdown(
                        pnl_15m_regime, self._ml.sizer.starting_balance_cents
                    )
                except Exception:
                    pass
                snap["regime_risk_metrics"] = regime_risk
                snap["config_regime_since"] = CONFIG_REGIME_SINCE
            except Exception:
                logging.debug("Snapshot: regime_risk_metrics failed", exc_info=True)
                snap["regime_risk_metrics"] = None
        except Exception:
            logging.debug("Snapshot: risk_metrics build failed", exc_info=True)
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
            logging.debug("Snapshot: execution_quality build failed", exc_info=True)
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
            logging.debug("Snapshot: convergence_velocity build failed", exc_info=True)
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
            pnl_series = conn.execute(
                f"SELECT settled_at, (pnl_cents - fee_cents) AS net "
                f"FROM settled_trades{_pt_filter} ORDER BY settled_at"
            ).fetchall()
            cumulative = []
            running = 0
            for r in pnl_series:
                running += r["net"]
                cumulative.append({"ts": r["settled_at"], "cum_pnl": running})
            rta["cumulative_pnl"] = cumulative

            # All-products cumulative P&L (for toggle)
            all_pnl_series = conn.execute(
                "SELECT settled_at, (pnl_cents - fee_cents) AS net, product_type "
                "FROM settled_trades ORDER BY settled_at"
            ).fetchall()
            all_cumulative = []
            all_running = 0
            for r in all_pnl_series:
                all_running += r["net"]
                all_cumulative.append({"ts": r["settled_at"], "cum_pnl": all_running, "pt": r["product_type"]})
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
                logging.debug("Snapshot: regime_trade_analytics failed", exc_info=True)
                snap["regime_trade_analytics"] = None
        except Exception:
            logging.debug("Snapshot: real_trade_analytics build failed", exc_info=True)
            snap["real_trade_analytics"] = {}


        # ── observation_mode flag ─────────────────────────────────────
        try:
            snap["observation_mode"] = getattr(self._ml, '_observation_mode', True)
        except Exception:
            snap["observation_mode"] = True

        # ── trading config values ──────────────────────────────────────
        try:
            import bot as _bot_mod
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
            logging.debug("Snapshot: cal_registry build failed", exc_info=True)

        # ── NIG distribution parameters ────────────────────────────────
        try:
            import json as _json
            dist_path = os.path.join(os.path.dirname(__file__), "dist_config.json")
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
                import bot as _bot_mod
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
            logging.debug("Snapshot: egarch_blend build failed", exc_info=True)

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
            logging.debug("Snapshot: shadow_variants build failed", exc_info=True)

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
                import bot as _bot_mod
                koft_data = {
                    "shadow_mode": getattr(_bot_mod, "KALSHI_OFT_SHADOW_MODE", True),
                    "tracked_tickers": koft.get_tracked_count(),
                }
                # Per-ticker signals (skip hourly — too many strikes)
                _hourly_pfx = ("KXBTCD", "KXETHD", "KXSOLD", "KXXRPD")
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
            logging.debug("Snapshot: kalshi_oft build failed", exc_info=True)

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
                    import bot as _bot_mod3
                    if getattr(_bot_mod3, "HOURLY_OBSERVATION_ENABLED", False) and \
                       getattr(_bot_mod3, "HOURLY_OBSERVATION_ONLY", True):
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
            logging.debug("Snapshot: execution_engine build failed", exc_info=True)
            snap["execution_engine"] = {}

        # ── Shadow calibration pipeline ─────────────────────────────────
        try:
            import bot as _bot_mod
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
            logging.debug("Snapshot: shadow_cal_pipeline build failed", exc_info=True)

        # ── Hourly observation mode ──────────────────────────────────────
        try:
            import bot as _bot_mod
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
            logging.debug("Snapshot: hourly_observation build failed", exc_info=True)

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
            logging.debug("Snapshot: spx_observation build failed", exc_info=True)

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
                snap["weather_observation"] = wx_data
        except Exception:
            logging.debug("Snapshot: weather_observation build failed", exc_info=True)

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
            logging.debug("Snapshot: sports_observation build failed", exc_info=True)

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
            logging.debug("Snapshot: data_collection build failed", exc_info=True)
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
            logging.debug("Snapshot: capital_allocation build failed", exc_info=True)

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
                        for a in ["BTC", "ETH", "SOL", "XRP"]:
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
                        logging.debug(f"Snapshot: ob summary failed for {ticker}", exc_info=True)

                snap["orderbooks"] = ob_summary
                logging.debug(f"Snapshot: orderbooks built for {sum(len(v) for v in ob_summary.values())} tickers")
            else:
                snap["orderbooks"] = {}
        except Exception:
            logging.debug("Snapshot: orderbooks build failed", exc_info=True)
            snap["orderbooks"] = {}

        # ── Position health (read-only enrichment for dashboard) ─────────
        try:
            kf = getattr(self._ml, "kalshi_feed", None)
            raw_obs = kf.get_all_orderbooks() if (kf and kf.is_connected) else {}
            windows = getattr(self._ml, "_active_windows", []) or []
            snap["position_health"] = self._compute_position_health(
                snap.get("active_positions", []), raw_obs, windows
            )
        except Exception:
            logging.debug("Snapshot: position_health build failed", exc_info=True)
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
            logging.debug("Snapshot: fifteenm_shadow build failed", exc_info=True)
            snap["fifteenm_shadow"] = None

        # ── Hourly Alt Shadow Strategies Panel ───────────────────────────
        try:
            _alt_eng = getattr(self._ml, "hourly_alt_shadow", None) if self._ml else None
            if _alt_eng:
                snap["hourly_alt_shadow"] = _alt_eng.get_dashboard_data()
            else:
                snap["hourly_alt_shadow"] = None
        except Exception:
            logging.debug("Snapshot: hourly_alt_shadow build failed", exc_info=True)
            snap["hourly_alt_shadow"] = None

        # ── SPX HAR-RV Shadow Panel ──────────────────────────────────────
        try:
            _harv_eng = getattr(self._ml, "spx_harrv_shadow", None) if self._ml else None
            if _harv_eng:
                snap["spx_harrv_shadow"] = _harv_eng.get_dashboard_data()
            else:
                snap["spx_harrv_shadow"] = None
        except Exception:
            logging.debug("Snapshot: spx_harrv_shadow build failed", exc_info=True)
            snap["spx_harrv_shadow"] = None

        # ── STC Performance (15M live trades by STC bucket) ────────────
        try:
            conn = _conn
            stc_buckets = [
                ("0-180", 0, 180),
                ("180-500", 180, 500),
                ("500-900", 500, 900),
            ]
            stc_perf = {}
            for label, lo, hi in stc_buckets:
                row = conn.execute(
                    "SELECT COUNT(*) AS n, "
                    "SUM(CASE WHEN (side='yes' AND market_result='yes') OR (side='no' AND market_result IN ('no','all_no')) THEN 1 ELSE 0 END) AS w, "
                    "SUM(pnl_cents - fee_cents) AS pnl "
                    "FROM settled_trades WHERE product_type='15m' AND seconds_to_close >= ? AND seconds_to_close < ? "
                    "AND settled_at >= ?",
                    (lo, hi, CONFIG_REGIME_SINCE)
                ).fetchone()
                stc_perf[label] = {
                    "trades": row["n"] if row else 0,
                    "wins": row["w"] if row and row["w"] else 0,
                    "wr": round(row["w"] / row["n"], 4) if row and row["n"] and row["w"] else 0,
                    "pnl_cents": row["pnl"] if row and row["pnl"] else 0,
                }
            snap["stc_performance"] = stc_perf
        except Exception:
            logging.debug("Snapshot: stc_performance build failed", exc_info=True)

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
            logging.debug("Snapshot: calibration_health build failed", exc_info=True)

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
            logging.debug("Snapshot: edge_integrity build failed", exc_info=True)

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
                    "settled": spo.get("settled_signals", 0),
                    "wins": spo.get("settled_wins", 0),
                    "wr": spo.get("settled_wr", 0),
                    "sim_pnl_cents": spo.get("settled_sim_pnl_cents", 0),
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

        # ── Weekend Edge Discount Shadow ─────────────────────────────
        try:
            _wknd_row = _conn.execute(
                "SELECT COUNT(*) as n, "
                "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes') "
                "  THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl "
                "FROM evaluated_opportunities WHERE filter_stage='weekend_discount_shadow'"
            ).fetchone()
            # Per-asset breakdown
            _wknd_by_asset = {}
            for _wa in _conn.execute(
                "SELECT asset, COUNT(*) as n, "
                "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes') "
                "  THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl "
                "FROM evaluated_opportunities WHERE filter_stage='weekend_discount_shadow' "
                "GROUP BY asset"
            ).fetchall():
                _wknd_by_asset[_wa["asset"]] = {
                    "n": _wa["n"], "settled": _wa["settled"],
                    "wins": _wa["wins"] or 0,
                    "wr": round(_wa["wins"] / _wa["settled"], 4) if _wa["settled"] else 0,
                    "sim_pnl_cents": _wa["sim_pnl"] or 0,
                }
            # Per-price-tier breakdown
            _wknd_by_tier = {}
            for _wt in _conn.execute(
                "SELECT CASE "
                "  WHEN market_price >= 95 THEN '95+' "
                "  WHEN market_price >= 93 THEN '93-94' "
                "  WHEN market_price >= 91 THEN '91-92' "
                "  WHEN market_price >= 89 THEN '89-90' "
                "  ELSE '86-88' END as tier, "
                "COUNT(*) as n, "
                "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes') "
                "  THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl "
                "FROM evaluated_opportunities WHERE filter_stage='weekend_discount_shadow' "
                "GROUP BY tier ORDER BY tier"
            ).fetchall():
                _wknd_by_tier[_wt["tier"]] = {
                    "n": _wt["n"], "settled": _wt["settled"],
                    "wins": _wt["wins"] or 0,
                    "wr": round(_wt["wins"] / _wt["settled"], 4) if _wt["settled"] else 0,
                    "sim_pnl_cents": _wt["sim_pnl"] or 0,
                }
            snap["weekend_discount_shadow"] = {
                "total_signals": _wknd_row["n"] if _wknd_row else 0,
                "settled": _wknd_row["settled"] if _wknd_row else 0,
                "wins": _wknd_row["wins"] if _wknd_row else 0,
                "wr": round(_wknd_row["wins"] / _wknd_row["settled"], 4) if _wknd_row and _wknd_row["settled"] else 0,
                "sim_pnl_cents": _wknd_row["sim_pnl"] if _wknd_row else 0,
                "by_asset": _wknd_by_asset,
                "by_price_tier": _wknd_by_tier,
                "discount_factor": 0.60,
            }
        except Exception:
            snap["weekend_discount_shadow"] = {"total_signals": 0, "settled": 0, "wins": 0, "wr": 0,
                                               "sim_pnl_cents": 0, "by_asset": {}, "by_price_tier": {},
                                               "discount_factor": 0.60}

        # ── Overnight Edge Discount Shadow ─────────────────────────────
        try:
            _ovn_row = _conn.execute(
                "SELECT COUNT(*) as n, "
                "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes') "
                "  THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl "
                "FROM evaluated_opportunities WHERE filter_stage='overnight_discount_shadow'"
            ).fetchone()
            _ovn_by_asset = {}
            for _oa in _conn.execute(
                "SELECT asset, COUNT(*) as n, "
                "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes') "
                "  THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl "
                "FROM evaluated_opportunities WHERE filter_stage='overnight_discount_shadow' "
                "GROUP BY asset"
            ).fetchall():
                _ovn_by_asset[_oa["asset"]] = {
                    "n": _oa["n"], "settled": _oa["settled"],
                    "wins": _oa["wins"] or 0,
                    "wr": round(_oa["wins"] / _oa["settled"], 4) if _oa["settled"] else 0,
                    "sim_pnl_cents": _oa["sim_pnl"] or 0,
                }
            _ovn_by_tier = {}
            for _ot in _conn.execute(
                "SELECT CASE "
                "  WHEN market_price >= 95 THEN '95+' "
                "  WHEN market_price >= 93 THEN '93-94' "
                "  WHEN market_price >= 91 THEN '91-92' "
                "  WHEN market_price >= 89 THEN '89-90' "
                "  ELSE '86-88' END as tier, "
                "COUNT(*) as n, "
                "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes') "
                "  THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl "
                "FROM evaluated_opportunities WHERE filter_stage='overnight_discount_shadow' "
                "GROUP BY tier ORDER BY tier"
            ).fetchall():
                _ovn_by_tier[_ot["tier"]] = {
                    "n": _ot["n"], "settled": _ot["settled"],
                    "wins": _ot["wins"] or 0,
                    "wr": round(_ot["wins"] / _ot["settled"], 4) if _ot["settled"] else 0,
                    "sim_pnl_cents": _ot["sim_pnl"] or 0,
                }
            snap["overnight_discount_shadow"] = {
                "total_signals": _ovn_row["n"] if _ovn_row else 0,
                "settled": _ovn_row["settled"] if _ovn_row else 0,
                "wins": _ovn_row["wins"] if _ovn_row else 0,
                "wr": round(_ovn_row["wins"] / _ovn_row["settled"], 4) if _ovn_row and _ovn_row["settled"] else 0,
                "sim_pnl_cents": _ovn_row["sim_pnl"] if _ovn_row else 0,
                "by_asset": _ovn_by_asset,
                "by_price_tier": _ovn_by_tier,
                "discount_factor": 0.60,
            }
        except Exception:
            snap["overnight_discount_shadow"] = {"total_signals": 0, "settled": 0, "wins": 0, "wr": 0,
                                                  "sim_pnl_cents": 0, "by_asset": {}, "by_price_tier": {},
                                                  "discount_factor": 0.60}

        # ── Decided Contract Shadow ─────────────────────────────
        try:
            _dc_row = _conn.execute(
                "SELECT COUNT(*) as n, "
                "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes') "
                "  THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl "
                "FROM evaluated_opportunities WHERE filter_stage IN ('decided_contract_t1','decided_contract_t2')"
            ).fetchone()
            # Per-asset breakdown
            _dc_by_asset = {}
            for _da in _conn.execute(
                "SELECT asset, COUNT(*) as n, "
                "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes') "
                "  THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl "
                "FROM evaluated_opportunities WHERE filter_stage IN ('decided_contract_t1','decided_contract_t2') "
                "GROUP BY asset"
            ).fetchall():
                _dc_by_asset[_da["asset"]] = {
                    "n": _da["n"], "settled": _da["settled"],
                    "wins": _da["wins"] or 0,
                    "wr": round(_da["wins"] / _da["settled"], 4) if _da["settled"] else 0,
                    "sim_pnl_cents": _da["sim_pnl"] or 0,
                }
            # Per-tier breakdown (t1 vs t2)
            _dc_by_tier = {}
            for _dt in _conn.execute(
                "SELECT filter_stage as tier, COUNT(*) as n, "
                "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes') "
                "  THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl "
                "FROM evaluated_opportunities WHERE filter_stage IN ('decided_contract_t1','decided_contract_t2') "
                "GROUP BY filter_stage ORDER BY filter_stage"
            ).fetchall():
                _dc_by_tier[_dt["tier"]] = {
                    "n": _dt["n"], "settled": _dt["settled"],
                    "wins": _dt["wins"] or 0,
                    "wr": round(_dt["wins"] / _dt["settled"], 4) if _dt["settled"] else 0,
                    "sim_pnl_cents": _dt["sim_pnl"] or 0,
                }
            snap["decided_contract_shadow"] = {
                "total_signals": _dc_row["n"] if _dc_row else 0,
                "settled": _dc_row["settled"] if _dc_row else 0,
                "wins": _dc_row["wins"] if _dc_row else 0,
                "wr": round(_dc_row["wins"] / _dc_row["settled"], 4) if _dc_row and _dc_row["settled"] else 0,
                "sim_pnl_cents": _dc_row["sim_pnl"] if _dc_row else 0,
                "by_asset": _dc_by_asset,
                "by_tier": _dc_by_tier,
            }
        except Exception:
            logging.debug("decided_contract_shadow snapshot failed", exc_info=True)
            snap["decided_contract_shadow"] = {"total_signals": 0, "settled": 0, "wins": 0, "wr": 0,
                                                "sim_pnl_cents": 0, "by_asset": {}, "by_tier": {}}

        # ── Relaxed Edge Shadow ─────────────────────────────
        try:
            _re_row = _conn.execute(
                "SELECT COUNT(*) as n, "
                "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes') "
                "  THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl "
                "FROM evaluated_opportunities WHERE filter_stage='relaxed_edge_shadow'"
            ).fetchone()
            # Per-asset breakdown
            _re_by_asset = {}
            for _ra in _conn.execute(
                "SELECT asset, COUNT(*) as n, "
                "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes') "
                "  THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl "
                "FROM evaluated_opportunities WHERE filter_stage='relaxed_edge_shadow' "
                "GROUP BY asset"
            ).fetchall():
                _re_by_asset[_ra["asset"]] = {
                    "n": _ra["n"], "settled": _ra["settled"],
                    "wins": _ra["wins"] or 0,
                    "wr": round(_ra["wins"] / _ra["settled"], 4) if _ra["settled"] else 0,
                    "sim_pnl_cents": _ra["sim_pnl"] or 0,
                }
            # Per-price-tier breakdown
            _re_by_tier = {}
            for _rt in _conn.execute(
                "SELECT CASE "
                "  WHEN market_price >= 92 THEN '92' "
                "  WHEN market_price >= 90 THEN '90-91' "
                "  ELSE '88-89' END as tier, "
                "COUNT(*) as n, "
                "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "SUM(CASE WHEN status='settled' AND market_result IN ('yes','all_yes') "
                "  THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as sim_pnl "
                "FROM evaluated_opportunities WHERE filter_stage='relaxed_edge_shadow' "
                "GROUP BY tier ORDER BY tier"
            ).fetchall():
                _re_by_tier[_rt["tier"]] = {
                    "n": _rt["n"], "settled": _rt["settled"],
                    "wins": _rt["wins"] or 0,
                    "wr": round(_rt["wins"] / _rt["settled"], 4) if _rt["settled"] else 0,
                    "sim_pnl_cents": _rt["sim_pnl"] or 0,
                }
            snap["relaxed_edge_shadow"] = {
                "total_signals": _re_row["n"] if _re_row else 0,
                "settled": _re_row["settled"] if _re_row else 0,
                "wins": _re_row["wins"] if _re_row else 0,
                "wr": round(_re_row["wins"] / _re_row["settled"], 4) if _re_row and _re_row["settled"] else 0,
                "sim_pnl_cents": _re_row["sim_pnl"] if _re_row else 0,
                "by_asset": _re_by_asset,
                "by_price_tier": _re_by_tier,
            }
        except Exception:
            logging.debug("relaxed_edge_shadow snapshot failed", exc_info=True)
            snap["relaxed_edge_shadow"] = {"total_signals": 0, "settled": 0, "wins": 0, "wr": 0,
                                            "sim_pnl_cents": 0, "by_asset": {}, "by_price_tier": {}}

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
                "  AND created_at >= datetime('now', '-14 days') "
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
            logging.debug("calibration_gap snapshot failed", exc_info=True)
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
            logging.debug("capital_utilization snapshot failed", exc_info=True)
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
        try:
            conn = _conn
            completeness = {}
            for pt, key_cols in [
                ("hourly", ["calibrated_prob", "market_price", "fee_adjusted_edge", "raw_prob"]),
                ("spx_hourly", ["calibrated_prob", "market_price", "fee_adjusted_edge"]),
                ("weather", ["calibrated_prob", "market_price", "fee_adjusted_edge", "raw_prob"]),
                ("sports", ["calibrated_prob", "market_price", "fee_adjusted_edge", "raw_prob"]),
            ]:
                total_row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM evaluated_opportunities WHERE product_type=?", (pt,)
                ).fetchone()
                total = total_row["cnt"] if total_row else 0
                if total == 0:
                    completeness[pt] = {"total": 0, "columns": {}}
                    continue
                col_fills = {}
                for col in key_cols:
                    try:
                        filled_row = conn.execute(
                            f"SELECT COUNT({col}) AS cnt FROM evaluated_opportunities WHERE product_type=?", (pt,)
                        ).fetchone()
                        col_fills[col] = round(filled_row["cnt"] / total * 100, 1) if filled_row else 0
                    except Exception:
                        col_fills[col] = None
                completeness[pt] = {"total": total, "columns": col_fills}
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
            logging.debug("sol_pathc_shadow snapshot failed", exc_info=True)
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
            logging.debug("eth_filter_shadow snapshot failed", exc_info=True)
            snap["eth_filter_shadow"] = {"total_trades": 0, "wins": 0, "losses": 0,
                                          "wr": 0, "total_pnl_cents": 0, "filters": {}}

        return snap

    def _compute_position_health(
        self,
        positions: List[Dict],
        orderbooks: Dict,
        active_windows: List[Dict],
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
            if not ob:
                continue

            try:
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

                if best_bid is None or best_ask is None:
                    continue

                spread = best_ask - best_bid
                mid_price = (best_bid + best_ask) / 2.0
                bid_depth = sum(q for _, q in yes_bid_levels)
                ask_depth = sum(q for _, q in yes_ask_levels)

                entry_price = pos.get("avg_price_cents", 0)
                count = pos.get("count", 0)
                side = (pos.get("side") or "").lower()

                # Unrealized P&L (conservative: bid-based exit for YES, ask-based for NO)
                if side == "yes":
                    unrealized_cents = (best_bid - entry_price) * count
                    unrealized_pct = round(
                        (best_bid - entry_price) / entry_price * 100, 2
                    ) if entry_price else 0
                else:
                    # NO position: profit if ask drops
                    unrealized_cents = (entry_price - best_ask) * count
                    unrealized_pct = round(
                        (entry_price - best_ask) / entry_price * 100, 2
                    ) if entry_price else 0

                # Seconds to close
                event_ticker = pos.get("event_ticker")
                stc_seconds = stc_lookup.get(event_ticker)

                # Mid-price history
                if ticker not in self._mid_history:
                    self._mid_history[ticker] = collections.deque(maxlen=30)
                self._mid_history[ticker].append(mid_price)
                mid_hist = list(self._mid_history[ticker])[-5:]

                # Health classification
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
                # STC-based danger: immediate (no debounce)
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

                result[ticker] = {
                    "entry_price": entry_price,
                    "mid_price": round(mid_price, 1),
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "spread": spread,
                    "bid_depth": bid_depth,
                    "ask_depth": ask_depth,
                    "unrealized_cents": round(unrealized_cents),
                    "unrealized_pct": round(unrealized_pct, 1),
                    "health": health,
                    "ob_age_s": ob_age_s,
                    "mid_history": mid_hist,
                    "stc_seconds": round(stc_seconds, 1) if stc_seconds is not None else None,
                }
            except Exception:
                logging.debug(f"Snapshot: position health failed for {ticker}", exc_info=True)

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

