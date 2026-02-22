"""Push bot state snapshots to Firebase Realtime Database every 10 seconds."""

import os
import time
import logging
import datetime
import threading
from typing import Dict, Any, Optional

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


class FirebasePusher:
    """Daemon thread that pushes bot status to Firebase REST API."""

    def __init__(self, main_loop):
        self._ml = main_loop
        self._db_url = os.environ.get("FIREBASE_DB_URL", "").rstrip("/")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

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
        while not self._stop.is_set():
            try:
                snapshot = self._build_snapshot()
                self._push(snapshot)
            except Exception:
                logging.warning("Firebase push failed", exc_info=True)
            self._stop.wait(timeout=PUSH_INTERVAL)

    def _build_snapshot(self) -> Dict[str, Any]:
        snap: Dict[str, Any] = {}
        now_utc = datetime.datetime.utcnow()
        snap["timestamp"] = now_utc.isoformat() + "Z"

        # Uptime
        snap["uptime_seconds"] = round(time.time() - self._ml._start_time, 1)

        # Balance
        try:
            bal = self._ml.client.get_balance()
            snap["current_balance"] = round(bal["balance"] / 100, 2) if bal else 0.0
            if snap["current_balance"] > self._ml._peak_balance:
                self._ml._peak_balance = snap["current_balance"]
            snap["peak_balance"] = self._ml._peak_balance
        except Exception:
            snap["current_balance"] = 0.0
            snap["peak_balance"] = getattr(self._ml, "_peak_balance", 0.0)

        # Starting balance
        try:
            snap["starting_balance"] = round(self._ml.sizer.starting_balance_cents / 100, 2)
        except Exception:
            snap["starting_balance"] = 0.0

        # Drawdown Kelly multiplier
        try:
            bal_cents = int(snap["current_balance"] * 100)
            snap["drawdown_kelly_mult"] = self._ml.sizer._drawdown_scaler(bal_cents)
        except Exception:
            snap["drawdown_kelly_mult"] = 1.0

        # Active positions
        try:
            positions = self._ml.state.get_open_positions()
            snap["active_positions"] = positions
        except Exception:
            snap["active_positions"] = []

        # Resting orders
        try:
            snap["resting_orders"] = self._ml.state.get_resting_orders()
        except Exception:
            snap["resting_orders"] = []

        # Recent trades + win/loss from settled_trades
        try:
            conn = self._ml.state.conn
            rows = conn.execute(
                "SELECT * FROM settled_trades ORDER BY settled_at DESC LIMIT 10"
            ).fetchall()
            snap["recent_trades"] = [dict(r) for r in rows]

            # Win/loss counts (same logic as SettlementTracker)
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

            # Daily P&L
            today_midnight = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat() + "Z"
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl_cents - fee_cents), 0) AS daily FROM settled_trades WHERE settled_at >= ?",
                (today_midnight,),
            ).fetchone()
            snap["daily_pnl_cents"] = row["daily"] if row else 0

            # Daily P&L percentage
            start_cents = self._ml.sizer.starting_balance_cents
            if start_cents > 0:
                snap["daily_pnl_pct"] = round(snap["daily_pnl_cents"] / start_cents * 100, 2)
            else:
                snap["daily_pnl_pct"] = 0.0

            # Consecutive losses
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
            snap["recent_trades"] = []
            snap["win_count"] = 0
            snap["loss_count"] = 0
            snap["win_rate"] = 0.0
            snap["daily_pnl_cents"] = 0
            snap["daily_pnl_pct"] = 0.0
            snap["consecutive_losses"] = 0

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
                for w in windows:
                    a = w["asset"]
                    by_asset[a] = by_asset.get(a, 0) + 1
                snap["active_windows"] = {
                    "total": len(windows),
                    "by_asset": by_asset,
                }
            else:
                snap["seconds_to_next_close"] = -1
                snap["active_windows"] = {"total": 0, "by_asset": {}}
        except Exception:
            snap["seconds_to_next_close"] = -1
            snap["active_windows"] = {"total": 0, "by_asset": {}}

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

        # ── strategy_breakdown (session counts by strategy) ──────────────
        try:
            snap["strategy_breakdown"] = dict(self._ml.scanner._session_strategy_counts)
        except Exception:
            snap["strategy_breakdown"] = {}

        # ── asset_performance (per-asset selection stats) ────────────────
        try:
            perf = {}
            for asset in ASSETS:
                ap = self._ml.scanner._session_asset_perf[asset]
                found = ap["opportunities_found"]
                selected = ap["times_selected"]
                rejected = ap["times_rejected"]
                total = selected + rejected
                perf[asset] = {
                    "opportunities_found": found,
                    "times_selected": selected,
                    "times_rejected": rejected,
                    "selection_rate": round(selected / total, 4) if total > 0 else 0.0,
                }
            snap["asset_performance"] = perf
        except Exception:
            snap["asset_performance"] = {}

        # ── session_stats ────────────────────────────────────────────────
        try:
            uptime_min = round((time.time() - self._ml._start_time) / 60, 1)
            total_scanned = self._ml.scanner._session_total_scanned
            snap["session_stats"] = {
                "total_opportunities_found": self._ml.scanner._session_total_candidates,
                "total_markets_scanned": total_scanned,
                "evaluation_rate": round(total_scanned / max(uptime_min, 0.1), 1),
                "uptime_minutes": uptime_min,
                "last_opportunity_timestamp": self._ml.scanner._last_opportunity_ts,
            }
        except Exception:
            snap["session_stats"] = {}

        # ── simulated_performance (observation mode counterfactuals) ─────
        try:
            conn = self._ml.state.conn
            sim_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM evaluated_opportunities "
                "WHERE filter_stage = 'observation_trade'"
            ).fetchone()["cnt"]

            row = conn.execute(
                "SELECT "
                "  COUNT(CASE WHEN counterfactual_pnl > 0 THEN 1 END) AS wins, "
                "  COUNT(CASE WHEN counterfactual_pnl <= 0 THEN 1 END) AS losses, "
                "  COALESCE(SUM(counterfactual_pnl), 0) AS pnl "
                "FROM evaluated_opportunities "
                "WHERE filter_stage = 'observation_trade' AND status = 'settled'"
            ).fetchone()

            snap["simulated_performance"] = {
                "simulated_trades_count": sim_count,
                "simulated_wins": row["wins"],
                "simulated_losses": row["losses"],
                "simulated_pnl_cents": row["pnl"],
            }
        except Exception:
            snap["simulated_performance"] = {
                "simulated_trades_count": 0,
                "simulated_wins": 0,
                "simulated_losses": 0,
                "simulated_pnl_cents": 0,
            }

        # ── rejection_summary (counts by reason) ────────────────────────
        try:
            conn = self._ml.state.conn
            rej_rows = conn.execute(
                "SELECT rejection_reason, COUNT(*) AS cnt "
                "FROM rejected_opportunities GROUP BY rejection_reason"
            ).fetchall()
            snap["rejection_summary"] = {r["rejection_reason"]: r["cnt"] for r in rej_rows}
        except Exception:
            snap["rejection_summary"] = {}

        return snap

    def _push(self, snapshot: Dict[str, Any]):
        url = f"{self._db_url}/bot_status.json"
        requests.put(url, json=_sanitize_keys(snapshot), timeout=5)
