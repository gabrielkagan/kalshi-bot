"""Push bot state snapshots to Firebase Realtime Database every 10 seconds."""

import os
import time
import logging
import datetime
import threading
import collections
from typing import Dict, Any, List, Optional

import requests

PUSH_INTERVAL = 10  # seconds
ASSETS = ["BTC", "ETH", "SOL", "XRP"]

# Firebase key names cannot contain . $ # [ ] /
_FB_KEY_BAD = str.maketrans({".": "_", "$": "_", "#": "_", "[": "(", "]": ")", "/": "|"})


def _sanitize_keys(obj):
    """Recursively sanitize dict keys for Firebase compatibility."""
    if isinstance(obj, dict):
        return {str(k).translate(_FB_KEY_BAD): _sanitize_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_keys(v) for v in obj]
    return obj


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


class FirebasePusher:
    """Daemon thread that pushes bot status to Firebase REST API."""

    def __init__(self, main_loop, db_path: str = "state.db"):
        self._ml = main_loop
        self._db_url = os.environ.get("FIREBASE_DB_URL", "").rstrip("/")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._db_path = db_path
        self._db_conn = None  # opened on daemon thread
        # Position health tracking (dashboard enrichment)
        self._mid_history: Dict[str, collections.deque] = {}
        self._health_state: Dict[str, str] = {}
        self._health_streak: Dict[str, int] = {}

    def start(self):
        if not self._db_url:
            logging.info("FIREBASE_DB_URL not set — Firebase push disabled")
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logging.info("Firebase pusher started")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        import sqlite3
        self._db_conn = sqlite3.connect(self._db_path)
        self._db_conn.execute("PRAGMA journal_mode=WAL")
        self._db_conn.execute("PRAGMA busy_timeout=10000")
        self._db_conn.row_factory = sqlite3.Row
        while not self._stop.is_set():
            try:
                snapshot = self._build_snapshot()
                self._push(snapshot)
            except Exception:
                logging.warning("Firebase push failed", exc_info=True)
            self._stop.wait(timeout=PUSH_INTERVAL)

    def _build_snapshot(self) -> Dict[str, Any]:
        snap: Dict[str, Any] = {}
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        snap["timestamp"] = now_utc.isoformat()

        # Uptime
        snap["uptime_seconds"] = round(time.time() - self._ml._start_time, 1)

        # Balance
        try:
            bal = self._ml.client.get_balance()
            snap["current_balance"] = round(bal["balance"] / 100, 2) if bal else 0.0
            # Read-only: peak tracking moved to main loop to avoid cross-thread mutation
            snap["peak_balance"] = getattr(self._ml, "_peak_balance", snap["current_balance"])
            self._last_good_balance = snap["current_balance"]
            snap["balance_stale"] = False
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
            logging.debug("Firebase: balance_history build failed", exc_info=True)
            snap["balance_history"] = []

        # Active positions (use Firebase thread's own DB connection to avoid threading issues)
        try:
            rows = self._db_conn.execute(
                "SELECT * FROM positions WHERE status='open'"
            ).fetchall()
            snap["active_positions"] = [dict(r) for r in rows]
        except Exception:
            snap["active_positions"] = []

        # Resting orders (use Firebase thread's own DB connection)
        try:
            rows = self._db_conn.execute(
                "SELECT * FROM pending_orders WHERE status='resting'"
            ).fetchall()
            snap["resting_orders"] = [dict(r) for r in rows]
        except Exception:
            snap["resting_orders"] = []

        # Track which sections failed for diagnostics
        snap["_snapshot_errors"] = []
        conn = self._db_conn

        # Section 1: Recent trades
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
                           COALESCE(st.calibrated_prob, eo.calibrated_prob) AS calibrated_prob
                    FROM settled_trades st
                    LEFT JOIN evaluated_opportunities eo
                        ON st.ticker = eo.ticker AND eo.filter_stage IN ('candidate', 'observation_trade')
                    ORDER BY st.settled_at DESC LIMIT 10
                """).fetchall()
            except Exception:
                rows = conn.execute(
                    "SELECT * FROM settled_trades ORDER BY settled_at DESC LIMIT 10"
                ).fetchall()
            snap["recent_trades"] = [dict(r) for r in rows]
        except Exception:
            snap["recent_trades"] = []
            snap["_snapshot_errors"].append("recent_trades")

        # Section 2: Win/loss counts
        try:
            all_settled = conn.execute(
                "SELECT side, market_result FROM settled_trades"
            ).fetchall()
            win = 0
            loss = 0
            for r in all_settled:
                side = r["side"]
                result = r["market_result"]
                if result in ("yes", "all_yes"):
                    if side == "yes":
                        win += 1
                    else:
                        loss += 1
                elif result in ("no", "all_no"):
                    if side == "no":
                        win += 1
                    else:
                        loss += 1
            snap["win_count"] = win
            snap["loss_count"] = loss
            snap["win_rate"] = round(win / (win + loss), 4) if (win + loss) > 0 else 0.0
        except Exception:
            snap["win_count"] = 0
            snap["loss_count"] = 0
            snap["win_rate"] = 0.0
            snap["_snapshot_errors"].append("win_loss_counts")

        # Section 3: Daily P&L
        try:
            today_midnight = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl_cents - fee_cents), 0) AS daily FROM settled_trades WHERE settled_at >= ?",
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

        # Section 4: Consecutive losses
        try:
            recent_settled = conn.execute(
                "SELECT side, market_result FROM settled_trades ORDER BY settled_at DESC LIMIT 20"
            ).fetchall()
            streak = 0
            for r in recent_settled:
                side, result = r["side"], r["market_result"]
                is_win = (result in ("yes", "all_yes") and side == "yes") or \
                         (result in ("no", "all_no") and side == "no")
                if is_win:
                    break
                streak += 1
            snap["consecutive_losses"] = streak
        except Exception:
            snap["consecutive_losses"] = 0
            snap["_snapshot_errors"].append("consecutive_losses")

        # Risk metrics
        try:
            risk = {}
            peak = snap.get("peak_balance", 0)
            cur = snap.get("current_balance", 0)
            risk["max_drawdown_pct"] = round((peak - cur) / peak * 100, 2) if peak > 0 else 0.0
            risk["max_drawdown_dollars"] = round(peak - cur, 2)

            all_pnl = conn.execute(
                "SELECT (pnl_cents - fee_cents) AS net, settled_at FROM settled_trades"
            ).fetchall()
            nets = [r["net"] for r in all_pnl]
            n = len(nets)
            if n > 0:
                total = sum(nets)
                mean = total / n
                variance = sum((x - mean) ** 2 for x in nets) / n if n > 1 else 0
                std = variance ** 0.5
                # Use actual trade span for Sharpe annualization
                span_row = conn.execute(
                    "SELECT MIN(settled_at) AS first_t, MAX(settled_at) AS last_t FROM settled_trades"
                ).fetchone()
                if span_row and span_row["first_t"] and span_row["last_t"]:
                    from datetime import datetime as _dt
                    try:
                        t0 = _dt.fromisoformat(span_row["first_t"].replace("Z", "+00:00"))
                        t1 = _dt.fromisoformat(span_row["last_t"].replace("Z", "+00:00"))
                        span_days = max((t1 - t0).total_seconds() / 86400, 0.01)
                    except Exception:
                        span_days = max(snap.get("uptime_seconds", 1) / 86400, 0.01)
                else:
                    span_days = max(snap.get("uptime_seconds", 1) / 86400, 0.01)
                trades_per_day = n / span_days
                risk["sharpe_ratio"] = round(mean / std * (trades_per_day ** 0.5), 2) if std > 0 else 0.0
                risk["total_pnl_cents"] = total
                risk["avg_pnl_per_trade"] = round(total / n, 1)
                gross_wins = sum(x for x in nets if x > 0)
                gross_losses = abs(sum(x for x in nets if x < 0))
                risk["profit_factor"] = round(gross_wins / gross_losses, 2) if gross_losses > 0 else 999.0
                risk["total_trades"] = n
            else:
                risk.update({"sharpe_ratio": 0, "total_pnl_cents": 0, "avg_pnl_per_trade": 0,
                              "profit_factor": 0, "total_trades": 0})
            snap["risk_metrics"] = risk
        except Exception:
            logging.debug("Firebase: risk_metrics build failed", exc_info=True)
            snap["risk_metrics"] = None

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
            logging.debug("Firebase: execution_quality build failed", exc_info=True)
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
            logging.debug("Firebase: convergence_velocity build failed", exc_info=True)
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
            snap["rate_limits"] = {
                "reads_last_second": len(self._ml.client._read_timestamps),
                "writes_last_second": len(self._ml.client._write_timestamps),
                "read_limit": 30,
                "write_limit": 30,
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
                "events_evaluated": total_scanned,
                "unique_markets_seen": total_scanned,
            }
        except Exception:
            snap["session_stats"] = {}

        # ── real_trade_analytics (from settled_trades) ─────────────────
        try:
            conn = self._db_conn
            rta = {}

            # P&L by asset
            asset_rows = conn.execute(
                "SELECT asset, COUNT(*) AS cnt, "
                "  COUNT(CASE WHEN (market_result IN ('yes','all_yes') AND side='yes') "
                "    OR (market_result IN ('no','all_no') AND side='no') THEN 1 END) AS wins, "
                "  COALESCE(SUM(pnl_cents - fee_cents), 0) AS net_pnl "
                "FROM settled_trades GROUP BY asset"
            ).fetchall()
            rta["by_asset"] = {r["asset"]: {"count": r["cnt"], "wins": r["wins"], "net_pnl": r["net_pnl"]} for r in asset_rows}

            # P&L by price bucket
            bucket_rows = conn.execute(
                "SELECT CASE "
                "  WHEN entry_price_cents BETWEEN 86 AND 89 THEN '86-89' "
                "  WHEN entry_price_cents BETWEEN 90 AND 94 THEN '90-94' "
                "  WHEN entry_price_cents BETWEEN 95 AND 99 THEN '95-99' "
                "  ELSE 'other' END AS bucket, "
                "COUNT(*) AS cnt, "
                "COUNT(CASE WHEN (market_result IN ('yes','all_yes') AND side='yes') "
                "  OR (market_result IN ('no','all_no') AND side='no') THEN 1 END) AS wins, "
                "COALESCE(SUM(pnl_cents - fee_cents), 0) AS net_pnl "
                "FROM settled_trades GROUP BY bucket"
            ).fetchall()
            rta["by_bucket"] = {r["bucket"]: {"count": r["cnt"], "wins": r["wins"], "net_pnl": r["net_pnl"]} for r in bucket_rows}

            # P&L by strategy
            strat_rows = conn.execute(
                "SELECT COALESCE(strategy, 'unknown') AS strat, COUNT(*) AS cnt, "
                "  COUNT(CASE WHEN (market_result IN ('yes','all_yes') AND side='yes') "
                "    OR (market_result IN ('no','all_no') AND side='no') THEN 1 END) AS wins, "
                "  COALESCE(SUM(pnl_cents - fee_cents), 0) AS net_pnl "
                "FROM settled_trades GROUP BY strat"
            ).fetchall()
            rta["by_strategy"] = {r["strat"]: {"count": r["cnt"], "wins": r["wins"], "net_pnl": r["net_pnl"]} for r in strat_rows}

            # P&L by hour (UTC)
            hour_rows = conn.execute(
                "SELECT CAST(SUBSTR(settled_at, 12, 2) AS INTEGER) AS hour, "
                "  COUNT(*) AS cnt, "
                "  COUNT(CASE WHEN (market_result IN ('yes','all_yes') AND side='yes') "
                "    OR (market_result IN ('no','all_no') AND side='no') THEN 1 END) AS wins, "
                "  COALESCE(SUM(pnl_cents - fee_cents), 0) AS net_pnl "
                "FROM settled_trades WHERE settled_at IS NOT NULL GROUP BY hour"
            ).fetchall()
            rta["by_hour"] = {str(r["hour"]): {"count": r["cnt"], "wins": r["wins"], "net_pnl": r["net_pnl"]} for r in hour_rows}

            # Best and worst trade
            best = conn.execute(
                "SELECT ticker, asset, entry_price_cents, (pnl_cents - fee_cents) AS net, settled_at "
                "FROM settled_trades ORDER BY net DESC LIMIT 1"
            ).fetchone()
            worst = conn.execute(
                "SELECT ticker, asset, entry_price_cents, (pnl_cents - fee_cents) AS net, settled_at "
                "FROM settled_trades ORDER BY net ASC LIMIT 1"
            ).fetchone()
            if best:
                rta["best_trade"] = dict(best)
            if worst:
                rta["worst_trade"] = dict(worst)

            # Cumulative P&L time series (for chart)
            pnl_series = conn.execute(
                "SELECT settled_at, (pnl_cents - fee_cents) AS net "
                "FROM settled_trades ORDER BY settled_at"
            ).fetchall()
            cumulative = []
            running = 0
            for r in pnl_series:
                running += r["net"]
                cumulative.append({"ts": r["settled_at"], "cum_pnl": running})
            rta["cumulative_pnl"] = cumulative

            snap["real_trade_analytics"] = rta
        except Exception:
            logging.debug("Firebase: real_trade_analytics build failed", exc_info=True)
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
            logging.debug("Firebase: egarch_blend build failed", exc_info=True)

        # ── counterfactual analysis ───────────────────────────────────
        try:
            conn = self._db_conn

            # By filter stage
            stage_rows = conn.execute(
                "SELECT filter_stage, COUNT(*) AS total, "
                "  COUNT(CASE WHEN counterfactual_pnl > 0 THEN 1 END) AS wins, "
                "  COUNT(CASE WHEN counterfactual_pnl <= 0 THEN 1 END) AS losses, "
                "  COALESCE(SUM(counterfactual_pnl), 0) AS net_pnl_cents "
                "FROM evaluated_opportunities "
                "WHERE status = 'settled' AND counterfactual_pnl IS NOT NULL "
                "GROUP BY filter_stage"
            ).fetchall()
            by_stage = []
            for r in stage_rows:
                total = r["total"]
                by_stage.append({
                    "stage": r["filter_stage"],
                    "total": total,
                    "wins": r["wins"],
                    "losses": r["losses"],
                    "net_pnl_cents": r["net_pnl_cents"],
                    "win_rate": round(r["wins"] / total, 4) if total > 0 else 0.0,
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
                "WHERE status = 'settled' AND counterfactual_pnl IS NOT NULL "
                "  AND market_price BETWEEN 80 AND 99 "
                "GROUP BY bucket"
            ).fetchall()
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
                "WHERE status = 'settled' AND counterfactual_pnl IS NOT NULL"
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
            logging.debug("Firebase: kalshi_oft build failed", exc_info=True)

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

            # IOC taker path may be broken
            if ioc_u >= 3 and ioc_f == 0:
                alerts.append(f"IOC taker path failing ({ioc_u} unfilled, 0 fills)")

            # Post-only rejection storm
            if po_rej >= 10 and po_fill == 0:
                alerts.append(f"Post-only rejection storm ({po_rej} rejects, 0 taker fills)")

            # Direct taker path may be broken
            dt_att = exec_eng.get("session_direct_taker_attempts", 0)
            dt_fill = exec_eng.get("session_direct_taker_fills", 0)
            if dt_att >= 3 and dt_fill == 0:
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
            logging.debug("Firebase: execution_engine build failed", exc_info=True)
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
            logging.debug("Firebase: shadow_cal_pipeline build failed", exc_info=True)

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
                conn = self._db_conn
                # Settled hourly observations
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM evaluated_opportunities "
                        "WHERE product_type='hourly' AND status='settled'"
                    ).fetchone()
                    hourly_data["settled_count"] = row["cnt"] if row else 0
                    # NOTE: market_result='yes' = win because bot ONLY takes YES side.
                    # If bot ever takes NO side, this query must check side column too.
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM evaluated_opportunities "
                        "WHERE product_type='hourly' AND status='settled' AND market_result='yes'"
                    ).fetchone()
                    hourly_data["settled_wins"] = row["cnt"] if row else 0
                    row = conn.execute(
                        "SELECT AVG(fee_adjusted_edge) AS avg_e FROM evaluated_opportunities "
                        "WHERE product_type='hourly' AND market_price IS NOT NULL"
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
                # Change 10: Simulated P&L from hourly observation trades
                # NOTE: Assumes YES side only. market_result='yes' = win, payout = 100 - price.
                # If bot ever takes NO side, sim P&L logic must account for side.
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt, "
                        "SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS wins, "
                        "SUM(CASE WHEN market_result='yes' "
                        "    THEN (100 - market_price) - CAST(CEIL(0.07 * 1 * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER) "
                        "    ELSE -market_price - CAST(CEIL(0.07 * 1 * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER) "
                        "END) AS sim_pnl "
                        "FROM evaluated_opportunities "
                        "WHERE product_type='hourly' AND filter_stage='hourly_observation' "
                        "AND status='settled' AND market_result IS NOT NULL"
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
            logging.debug("Firebase: hourly_observation build failed", exc_info=True)

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

                conn = self._db_conn
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM evaluated_opportunities "
                        "WHERE product_type='spx_hourly' AND status='settled'"
                    ).fetchone()
                    spx_data["settled_count"] = row["cnt"] if row else 0
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM evaluated_opportunities "
                        "WHERE product_type='spx_hourly' AND status='settled' AND market_result='yes'"
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

                # Simulated P&L
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt, "
                        "SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS wins, "
                        "SUM(CASE WHEN market_result='yes' "
                        "    THEN (100 - market_price) - CAST(CEIL(0.035 * 1 * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER) "
                        "    ELSE -market_price - CAST(CEIL(0.035 * 1 * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER) "
                        "END) AS sim_pnl "
                        "FROM evaluated_opportunities "
                        "WHERE product_type='spx_hourly' AND filter_stage='spx_observation' "
                        "AND status='settled' AND market_result IS NOT NULL"
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
            logging.debug("Firebase: spx_observation build failed", exc_info=True)

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
                conn = self._db_conn
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM evaluated_opportunities "
                        "WHERE product_type='weather' AND status='settled'"
                    ).fetchone()
                    wx_data["settled_count"] = row["cnt"] if row else 0
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM evaluated_opportunities "
                        "WHERE product_type='weather' AND status='settled' AND market_result='yes'"
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

                # Simulated P&L
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt, "
                        "SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS wins, "
                        "SUM(CASE WHEN market_result='yes' "
                        "    THEN (100 - market_price) - CAST(CEIL(0.07 * 1 * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER) "
                        "    ELSE -market_price - CAST(CEIL(0.07 * 1 * (market_price / 100.0) * (1 - market_price / 100.0)) AS INTEGER) "
                        "END) AS sim_pnl "
                        "FROM evaluated_opportunities "
                        "WHERE product_type='weather' AND filter_stage='weather_observation' "
                        "AND status='settled' AND market_result IS NOT NULL"
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
            logging.debug("Firebase: weather_observation build failed", exc_info=True)

        # ── Sports Observation Panel ─────────────────────────────────────
        try:
            if getattr(_bot_mod, "SPORTS_ENABLED", False):
                sp_data = {
                    "enabled": True,
                    "observation_only": getattr(_bot_mod, "SPORTS_OBSERVATION_ONLY", True),
                }
                conn = self._db_conn
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
            logging.debug("Firebase: sports_observation build failed", exc_info=True)

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
            logging.debug("Firebase: capital_allocation build failed", exc_info=True)

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
                        logging.debug(f"Firebase: ob summary failed for {ticker}", exc_info=True)

                snap["orderbooks"] = ob_summary
                logging.debug(f"Firebase: orderbooks built for {sum(len(v) for v in ob_summary.values())} tickers")
            else:
                snap["orderbooks"] = {}
        except Exception:
            logging.debug("Firebase: orderbooks build failed", exc_info=True)
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
            logging.debug("Firebase: position_health build failed", exc_info=True)
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
                logging.debug(f"Firebase: position health failed for {ticker}", exc_info=True)

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

    def _push(self, snapshot: Dict[str, Any]):
        url = f"{self._db_url}/bot_status.json"
        payload = _sanitize_keys(snapshot)
        for attempt in range(2):
            try:
                requests.put(url, json=payload, timeout=5)
                self._consecutive_push_failures = 0
                return
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                else:
                    self._consecutive_push_failures = getattr(self, '_consecutive_push_failures', 0) + 1
                    logging.warning(
                        f"Firebase PUT failed after retry: {e} "
                        f"(consecutive={self._consecutive_push_failures})"
                    )
