"""Async Supabase syncer — background thread that mirrors SQLite → Supabase.

Architecture mirrors firebase_push.py exactly:
  - Daemon thread, never blocks trading
  - Own SQLite connection (WAL mode, read-only queries)
  - Kill switch file: touch .supabase_kill_switch to disable
  - All operations idempotent (UPSERT on conflict keys)
  - Failures logged, never crash bot
"""

import os
import time
import json
import sqlite3
import logging
import threading
from typing import Dict, Any, Optional

import requests

ASSETS = ["BTC", "ETH", "SOL", "XRP"]
KILL_SWITCH_FILE = os.path.join(os.path.dirname(__file__) or ".", ".supabase_kill_switch")

# Sync intervals
DASHBOARD_INTERVAL = 10      # seconds — dashboard_state UPSERT
INCREMENTAL_INTERVAL = 30    # seconds — evaluations, rejections, vol_params
SNAPSHOT_INTERVAL = 900       # seconds — volatility snapshots, scan summaries (15 min)
STORAGE_CHECK_INTERVAL = 1800 # seconds — pg_database_size check (30 min)

# Storage guardrails (bytes)
STORAGE_WARN_BYTES = 400 * 1024 * 1024   # 400MB — critical-only sync
STORAGE_STOP_BYTES = 450 * 1024 * 1024   # 450MB — all sync stops

# Error backoff
MAX_CONSECUTIVE_ERRORS = 10
BACKOFF_INTERVAL = 300  # 5 minutes

# Session refresh
SESSION_REFRESH_REQUESTS = 1000


class SupabaseSyncer:
    """Daemon thread that syncs SQLite data to Supabase PostgREST API."""

    def __init__(self, main_loop):
        self._ml = main_loop
        self._url = os.environ.get("SUPABASE_URL", "").rstrip("/")
        self._key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._session: Optional[requests.Session] = None
        self._request_count = 0
        self._consecutive_errors = 0
        self._storage_ok = True        # False when >400MB
        self._storage_stopped = False   # True when >450MB

        # Watermarks for incremental sync
        self._wm_evaluations = 0
        self._wm_rejections_count = 0
        self._wm_trades_count = 0

        # Timing
        self._last_dashboard = 0
        self._last_incremental = 0
        self._last_snapshot = 0
        self._last_storage_check = 0

    def start(self):
        if not self._url or not self._key:
            logging.info("SUPABASE_URL/SUPABASE_SERVICE_KEY not set — Supabase sync disabled")
            return
        self._session = self._new_session()
        self._db = sqlite3.connect(
            os.path.join(os.path.dirname(__file__) or ".", "state.db"),
            check_same_thread=False,
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")

        # Load watermarks from Supabase (best-effort)
        self._load_watermarks()

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logging.info("Supabase syncer started")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if hasattr(self, "_db"):
            try:
                self._db.close()
            except Exception:
                pass

    # ── Main loop ───────────────────────────────────────────────────────

    def _run(self):
        while not self._stop.is_set():
            try:
                # Kill switch check
                if os.path.exists(KILL_SWITCH_FILE):
                    self._stop.wait(timeout=DASHBOARD_INTERVAL)
                    continue

                # Error backoff
                if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    logging.debug("Supabase syncer: backing off after %d errors", self._consecutive_errors)
                    self._stop.wait(timeout=BACKOFF_INTERVAL)
                    self._consecutive_errors = 0
                    continue

                # Storage stopped
                if self._storage_stopped:
                    self._stop.wait(timeout=STORAGE_CHECK_INTERVAL)
                    self._check_storage()
                    continue

                now = time.time()

                # Dashboard push (every 10s)
                if now - self._last_dashboard >= DASHBOARD_INTERVAL:
                    self._sync_dashboard()
                    self._last_dashboard = now

                # Incremental sync (every 30s) — skip if storage warning
                if now - self._last_incremental >= INCREMENTAL_INTERVAL and self._storage_ok:
                    self._sync_incremental()
                    self._last_incremental = now

                # Periodic snapshots (every 15min) — skip if storage warning
                if now - self._last_snapshot >= SNAPSHOT_INTERVAL and self._storage_ok:
                    self._sync_snapshots()
                    self._last_snapshot = now

                # Storage check (every 30min)
                if now - self._last_storage_check >= STORAGE_CHECK_INTERVAL:
                    self._check_storage()
                    self._last_storage_check = now

            except Exception:
                logging.warning("Supabase syncer cycle failed", exc_info=True)
                self._consecutive_errors += 1

            self._stop.wait(timeout=DASHBOARD_INTERVAL)

    # ── HTTP helpers ────────────────────────────────────────────────────

    def _new_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        })
        return s

    def _post(self, table: str, rows: list, on_conflict: str = "") -> bool:
        """UPSERT rows into a Supabase table. Returns True on success."""
        if not rows:
            return True
        try:
            self._maybe_refresh_session()
            url = f"{self._url}/rest/v1/{table}"
            headers = {"Prefer": "resolution=merge-duplicates"}
            if on_conflict:
                headers["Prefer"] = f"resolution=merge-duplicates,return=minimal"
            resp = self._session.post(url, json=rows, headers=headers, timeout=10)
            self._request_count += 1
            if resp.status_code in (200, 201):
                self._consecutive_errors = 0
                return True
            if resp.status_code == 409:
                self._consecutive_errors = 0
                return True
            logging.debug("Supabase %s: HTTP %d — %s", table, resp.status_code, resp.text[:200])
            self._consecutive_errors += 1
            return False
        except Exception:
            self._consecutive_errors += 1
            return False

    def _patch(self, table: str, data: dict, query: str = "") -> bool:
        """PATCH (update) rows matching query."""
        try:
            self._maybe_refresh_session()
            url = f"{self._url}/rest/v1/{table}?{query}" if query else f"{self._url}/rest/v1/{table}"
            headers = {"Prefer": "return=minimal"}
            resp = self._session.patch(url, json=data, headers=headers, timeout=10)
            self._request_count += 1
            if resp.status_code in (200, 204):
                self._consecutive_errors = 0
                return True
            logging.debug("Supabase PATCH %s: HTTP %d — %s", table, resp.status_code, resp.text[:200])
            self._consecutive_errors += 1
            return False
        except Exception:
            self._consecutive_errors += 1
            return False

    def _rpc(self, fn: str) -> Any:
        """Call a Supabase RPC function."""
        try:
            self._maybe_refresh_session()
            url = f"{self._url}/rest/v1/rpc/{fn}"
            resp = self._session.post(url, json={}, timeout=10)
            self._request_count += 1
            if resp.status_code in (200, 204):
                return resp.json() if resp.text else None
            return None
        except Exception:
            return None

    def _maybe_refresh_session(self):
        if self._request_count >= SESSION_REFRESH_REQUESTS:
            if self._session:
                self._session.close()
            self._session = self._new_session()
            self._request_count = 0

    # ── Watermarks ──────────────────────────────────────────────────────

    def _load_watermarks(self):
        """Load sync watermarks from Supabase (best-effort)."""
        try:
            url = f"{self._url}/rest/v1/sync_watermarks?select=source_table,last_synced_id"
            resp = self._session.get(url, timeout=10)
            if resp.status_code == 200:
                for row in resp.json():
                    src = row.get("source_table")
                    wm = row.get("last_synced_id", 0) or 0
                    if src == "evaluated_opportunities":
                        self._wm_evaluations = wm
                    elif src == "rejected_opportunities":
                        self._wm_rejections_count = wm
                    elif src == "settled_trades":
                        self._wm_trades_count = wm
                logging.info("Supabase watermarks loaded: evals=%d rej=%d trades=%d",
                             self._wm_evaluations, self._wm_rejections_count, self._wm_trades_count)
        except Exception:
            logging.debug("Supabase: could not load watermarks, starting from 0")

    def _save_watermark(self, source: str, last_id: int, count: int):
        """Update watermark in Supabase."""
        self._post("sync_watermarks", [{
            "source_table": source,
            "last_synced_id": last_id,
            "row_count": count,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }])

    # ── Dashboard sync ──────────────────────────────────────────────────

    def _sync_dashboard(self):
        """Push dashboard_state — single-row UPSERT, reuses firebase_push snapshot logic."""
        try:
            fb = getattr(self._ml, "firebase", None)
            if fb and hasattr(fb, "_build_snapshot"):
                snapshot = fb._build_snapshot()
            else:
                snapshot = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

            row = {
                "id": 1,
                "data": self._sanitize_for_json(snapshot),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            self._post("dashboard_state", [row])
        except Exception:
            logging.debug("Supabase: dashboard sync failed", exc_info=True)

    # ── Incremental sync ────────────────────────────────────────────────

    def _sync_incremental(self):
        """Sync new rows from SQLite → Supabase using watermarks."""
        self._sync_evaluations()
        self._sync_rejections()
        self._sync_trades()
        self._sync_vol_params()

    def _sync_evaluations(self):
        """Incremental sync of evaluated_opportunities by rowid."""
        try:
            rows = self._db.execute(
                "SELECT * FROM evaluated_opportunities WHERE id > ? ORDER BY id LIMIT 500",
                (self._wm_evaluations,)
            ).fetchall()
            if not rows:
                return
            mapped = [{col: self._clean(r[col]) for col in r.keys()} for r in rows]
            if self._post("evaluations", mapped):
                new_wm = max(r["id"] for r in rows)
                self._wm_evaluations = new_wm
                self._save_watermark("evaluated_opportunities", new_wm, len(rows))
                logging.debug("Supabase: synced %d evaluations (wm=%d)", len(rows), new_wm)
        except Exception:
            logging.debug("Supabase: evaluations sync failed", exc_info=True)

    def _sync_rejections(self):
        """Sync rejections — use COUNT as watermark since no autoincrement id."""
        try:
            count_row = self._db.execute("SELECT COUNT(*) AS cnt FROM rejected_opportunities").fetchone()
            current_count = count_row["cnt"] if count_row else 0
            if current_count <= self._wm_rejections_count:
                return

            # Fetch all and upsert (idempotent on ticker PK)
            rows = self._db.execute(
                "SELECT * FROM rejected_opportunities ORDER BY rejection_time DESC LIMIT 500"
            ).fetchall()
            if not rows:
                return
            mapped = [{col: self._clean(r[col]) for col in r.keys()} for r in rows]
            if self._post("rejections", mapped):
                self._wm_rejections_count = current_count
                self._save_watermark("rejected_opportunities", current_count, len(rows))
                logging.debug("Supabase: synced %d rejections", len(rows))
        except Exception:
            logging.debug("Supabase: rejections sync failed", exc_info=True)

    def _sync_trades(self):
        """Sync settled_trades — use COUNT as watermark."""
        try:
            count_row = self._db.execute("SELECT COUNT(*) AS cnt FROM settled_trades").fetchone()
            current_count = count_row["cnt"] if count_row else 0
            if current_count <= self._wm_trades_count:
                return

            rows = self._db.execute("""
                SELECT ticker, event_ticker, asset, market_result, side, count,
                       entry_price_cents, revenue_cents, fee_cents, pnl_cents,
                       settled_at, strategy, seconds_to_close, fill_latency_seconds,
                       vol_regime, calibrated_prob, edge, kelly_f
                FROM settled_trades
            """).fetchall()
            if not rows:
                return
            mapped = [{col: self._clean(r[col]) for col in r.keys()} for r in rows]
            if self._post("trades", mapped):
                self._wm_trades_count = current_count
                self._save_watermark("settled_trades", current_count, len(rows))
                logging.debug("Supabase: synced %d trades", len(rows))
        except Exception:
            logging.debug("Supabase: trades sync failed", exc_info=True)

    def _sync_vol_params(self):
        """Sync current GARCH/EGARCH parameters."""
        try:
            garch = {}
            try:
                for r in self._db.execute("SELECT * FROM garch_params").fetchall():
                    garch[r["asset"]] = dict(r)
            except Exception:
                pass

            egarch = {}
            try:
                for r in self._db.execute("SELECT * FROM egarch_params").fetchall():
                    egarch[r["asset"]] = dict(r)
            except Exception:
                pass

            if not garch and not egarch:
                return

            mapped = []
            for asset in ASSETS:
                g = garch.get(asset, {})
                e = egarch.get(asset, {})
                if not g and not e:
                    continue
                mapped.append({
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
                    "updated_at": e.get("updated_at") or g.get("updated_at")
                                  or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
            if mapped:
                self._post("volatility_params", mapped)
        except Exception:
            logging.debug("Supabase: vol_params sync failed", exc_info=True)

    # ── Periodic snapshots ──────────────────────────────────────────────

    def _sync_snapshots(self):
        """Push volatility snapshots and calibration state."""
        self._sync_vol_snapshots()
        self._sync_cal_snapshot()
        self._refresh_analytics_views()

    def _refresh_analytics_views(self):
        """Refresh materialized views for dashboard analytics."""
        try:
            self._rpc("refresh_analytics_views")
        except Exception:
            logging.debug("Supabase: view refresh failed", exc_info=True)

    def _sync_vol_snapshots(self):
        """Sample current volatility state for each asset."""
        try:
            vol = getattr(self._ml, "vol", None)
            mz = getattr(self._ml, "mz_tracker", None)
            if not vol:
                return
            now_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            rows = []
            for asset in ASSETS:
                cached = vol._cache.get(asset)
                if not cached:
                    continue
                row = {
                    "asset": asset,
                    "snapshot_time": now_ts,
                    "garch_sigma": cached.get("garch_sigma"),
                    "egarch_sigma": cached.get("egarch_sigma"),
                    "egarch_blend_sigma": cached.get("egarch_blend_sigma"),
                    "egarch_blend_weight": mz.get_weight(asset) if mz else None,
                    "rk_variance": cached.get("blended_rv"),
                    "rk_tv_weight": (cached.get("shadow_tv_weights") or {}).get("w_5"),
                    "jump_count": cached.get("jump_event_count", 0),
                    "jump_adaptive_threshold": cached.get("adaptive_ewma_sigma"),
                    "mz_r_squared": mz._r_squared.get(asset) if mz else None,
                    "mz_qlike": mz._qlike.get(asset) if mz else None,
                    "mz_baseline_qlike": mz._baseline_qlike.get(asset) if mz else None,
                    "mz_shadow_sigmoid_w": mz._shadow_sigmoid_w.get(asset) if mz else None,
                }
                rows.append(row)
            if rows:
                self._post("volatility_snapshots", rows)
        except Exception:
            logging.debug("Supabase: vol snapshots failed", exc_info=True)

    def _sync_cal_snapshot(self):
        """Push calibration engine state."""
        try:
            cal = getattr(self._ml, "calibration", None)
            if not cal:
                return
            import bot as _bot_mod
            row = {
                "snapshot_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "active_method": cal.active_method,
                "beta_cal_brier": cal.rolling_brier_score(),
                "temperature_brier": getattr(cal, "_temperature_brier", None),
                "beta_cal_a": getattr(cal, "_beta_a", None),
                "beta_cal_b": getattr(cal, "_beta_b", None),
                "temperature": getattr(cal, "_temperature", None),
                "sample_count": getattr(cal, "_sample_count", None),
                "market_blend_weight": getattr(_bot_mod, "MARKET_BLEND_W", None),
            }
            self._post("calibration_snapshots", [row])
        except Exception:
            logging.debug("Supabase: cal snapshot failed", exc_info=True)

    # ── Storage check ───────────────────────────────────────────────────

    def _check_storage(self):
        """Query pg_database_size and apply guardrails."""
        try:
            url = f"{self._url}/rest/v1/rpc/pg_database_size"
            # Use raw SQL via RPC if available, otherwise estimate
            resp = self._session.post(
                f"{self._url}/rest/v1/rpc/pg_database_size",
                json={"name": "postgres"},
                headers={"Prefer": "return=representation"},
                timeout=10,
            )
            if resp.status_code == 200:
                size_bytes = resp.json()
                if isinstance(size_bytes, list) and size_bytes:
                    size_bytes = size_bytes[0]
                if isinstance(size_bytes, dict):
                    size_bytes = list(size_bytes.values())[0]
                size_bytes = int(size_bytes) if size_bytes else 0

                if size_bytes > STORAGE_STOP_BYTES:
                    logging.warning("Supabase storage %dMB > 450MB — ALL sync stopped",
                                    size_bytes // (1024 * 1024))
                    self._storage_stopped = True
                    self._storage_ok = False
                elif size_bytes > STORAGE_WARN_BYTES:
                    logging.warning("Supabase storage %dMB > 400MB — critical-only sync",
                                    size_bytes // (1024 * 1024))
                    self._storage_ok = False
                    self._storage_stopped = False
                else:
                    self._storage_ok = True
                    self._storage_stopped = False
            else:
                # Can't check — assume OK
                logging.debug("Supabase: pg_database_size RPC not available (HTTP %d)", resp.status_code)
        except Exception:
            logging.debug("Supabase: storage check failed", exc_info=True)

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _clean(val):
        """Convert SQLite values for JSON serialization."""
        if val is None:
            return None
        if isinstance(val, bytes):
            return val.decode("utf-8", errors="replace")
        return val

    @staticmethod
    def _sanitize_for_json(obj):
        """Recursively convert non-JSON-serializable types to strings.

        Keeps the structure as a dict/list so PostgREST's json= parameter
        handles final serialization (avoids double-encoding into a JSONB string literal).
        """
        if isinstance(obj, dict):
            return {k: SupabaseSyncer._sanitize_for_json(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [SupabaseSyncer._sanitize_for_json(v) for v in obj]
        if isinstance(obj, (str, int, float, bool)) or obj is None:
            return obj
        # datetime, bytes, Decimal, etc. → string
        return str(obj)
