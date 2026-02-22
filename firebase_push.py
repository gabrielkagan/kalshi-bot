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
        except Exception:
            snap["current_balance"] = 0.0

        # Active positions
        try:
            positions = self._ml.state.get_open_positions()
            snap["active_positions"] = positions
        except Exception:
            snap["active_positions"] = []

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
        except Exception:
            snap["recent_trades"] = []
            snap["win_count"] = 0
            snap["loss_count"] = 0
            snap["win_rate"] = 0.0
            snap["daily_pnl_cents"] = 0

        # Volatility from cache (read-only)
        try:
            vol_data = {}
            for asset in ASSETS:
                cached = self._ml.vol._cache.get(asset)
                if cached:
                    vol_data[asset] = {
                        "blended_rv": cached["blended_rv"],
                        "regime": cached["regime"],
                    }
                else:
                    vol_data[asset] = None
            snap["current_volatility"] = vol_data
        except Exception:
            snap["current_volatility"] = {}

        # Seconds to next close
        try:
            windows = self._ml._active_windows
            if windows:
                snap["seconds_to_next_close"] = round(
                    min(w["seconds_to_close"] for w in windows), 1
                )
            else:
                snap["seconds_to_next_close"] = -1
        except Exception:
            snap["seconds_to_next_close"] = -1

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

        return snap

    def _push(self, snapshot: Dict[str, Any]):
        url = f"{self._db_url}/bot_status.json"
        requests.put(url, json=snapshot, timeout=5)
