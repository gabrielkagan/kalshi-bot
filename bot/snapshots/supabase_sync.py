"""Async Supabase syncer — background thread that mirrors SQLite → Supabase.

Architecture mirrors dashboard_snapshot.py:
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
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

import requests

ASSETS = ["BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB"]

# Sprint 10 Bit 10.4 (2026-05-12): anchor sentinel files to REPO ROOT regardless
# of this module's filesystem location. Pre-move this file lived at repo root
# so the naive single-level dirname-of-__file__ happened to be the repo root;
# post-move it resolves to `bot/snapshots/` which would silently break the
# operator-touched `.supabase_kill_switch` workflow + the `state.db` connect
# target. The 3-level dirname chain mirrors bot/engines/weather_engine.py:806-807.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KILL_SWITCH_FILE = os.path.join(_REPO_ROOT, ".supabase_kill_switch")

# Sync intervals
DASHBOARD_INTERVAL = 30      # seconds — dashboard_state UPSERT
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

        # Watermarks for incremental sync (all rowid-based — monotonic, drift-immune)
        self._wm_evaluations = 0
        self._wm_rejections = 0
        self._wm_trades_rowid = 0  # was _wm_trades_count — renamed for clarity; value stored in sync_watermarks.last_synced_id
        self._wm_harrv = 0

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
            os.path.join(_REPO_ROOT, "state.db"),
            check_same_thread=False,
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=30000")

        # Load watermarks from Supabase (best-effort)
        self._load_watermarks()

        # Validate schema parity — catches spx_harrv-style silent 400 drift at startup.
        # See kb/failures/dashboard-drift.md and kb/failures/supabase-sync-silent-failure.md.
        try:
            self._validate_schema_parity()
        except Exception:
            logging.warning("Supabase: schema parity check failed", exc_info=True)

        # Register all asset codes the bot can trade into the FK'd assets table.
        # Missing rows here silently drop every trade that references an unknown
        # asset (the whole batch 409s). Lost 486 trades Apr 12-18 2026 to this.
        try:
            self._register_assets()
        except Exception:
            logging.warning("Supabase: asset registry sync failed", exc_info=True)

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

                # Dashboard push (every 30s)
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
        """UPSERT rows into a Supabase table. Returns True on success.

        Returns False on ANY non-2xx so the caller does not advance its watermark.
        A 409 Conflict with `resolution=merge-duplicates` means the header was NOT
        honored (PK ambiguity) or the error is actually a constraint violation
        (FK 23503, check 23514, etc.) returned as 409 — both are real failures,
        not upsert success. Previously treating 409 as success silently dropped
        486 trades Apr 12-18 2026 when weather assets were missing from the
        FK'd `assets` table.
        """
        if not rows:
            return True
        try:
            self._maybe_refresh_session()
            url = f"{self._url}/rest/v1/{table}"
            headers = {"Prefer": "resolution=merge-duplicates,return=minimal"}
            resp = self._session.post(url, json=rows, headers=headers, timeout=10)
            self._request_count += 1
            if resp.status_code in (200, 201, 204):
                self._consecutive_errors = 0
                return True
            logging.warning("Supabase %s: HTTP %d — %s", table, resp.status_code, resp.text[:400])
            self._consecutive_errors += 1
            return False
        except Exception as e:
            logging.warning("Supabase POST failed: %s %s", table, e, exc_info=True)
            self._consecutive_errors += 1
            return False

    def _insert(self, table: str, rows: list) -> bool:
        """INSERT rows (append-only, no UPSERT). For tables like vol/cal snapshots."""
        if not rows:
            return True
        try:
            self._maybe_refresh_session()
            url = f"{self._url}/rest/v1/{table}"
            headers = {"Prefer": "return=minimal"}
            resp = self._session.post(url, json=rows, headers=headers, timeout=10)
            self._request_count += 1
            if resp.status_code in (200, 201):
                self._consecutive_errors = 0
                return True
            logging.warning("Supabase INSERT %s: HTTP %d — %s", table, resp.status_code, resp.text[:200])
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
                        self._wm_rejections = wm
                    elif src == "settled_trades":
                        self._wm_trades_rowid = wm
                    elif src == "spx_harrv_shadow_signals":
                        self._wm_harrv = wm
                logging.info("Supabase watermarks loaded: evals=%d rej=%d trades=%d harrv=%d",
                             self._wm_evaluations, self._wm_rejections, self._wm_trades_rowid, self._wm_harrv)
        except Exception:
            logging.debug("Supabase: could not load watermarks, starting from 0")

    # ── Asset registry ──────────────────────────────────────────────────

    _WEATHER_ASSETS = [
        ("ATL_TEMP", "Atlanta", "KXHIGHTATL"),
        ("AUS_TEMP", "Austin", "KXHIGHAUS"),
        ("BOS_TEMP", "Boston", "KXHIGHTBOS"),
        ("CHI_TEMP", "Chicago", "KXHIGHCHI"),
        ("DAL_TEMP", "Dallas", "KXHIGHTDAL"),
        ("DCA_TEMP", "Washington DC", "KXHIGHTDC"),
        ("DEN_TEMP", "Denver", "KXHIGHDEN"),
        ("HOU_TEMP", "Houston", "KXHIGHTHOU"),
        ("LAS_TEMP", "Las Vegas", "KXHIGHTLV"),
        ("LAX_TEMP", "Los Angeles", "KXHIGHLAX"),
        ("MIA_TEMP", "Miami", "KXHIGHMIA"),
        ("MIN_TEMP", "Minneapolis", "KXHIGHTMIN"),
        ("MSY_TEMP", "New Orleans", "KXHIGHTNOLA"),
        ("NYC_TEMP", "New York", "KXHIGHNY"),
        ("OKC_TEMP", "Oklahoma City", "KXHIGHTOKC"),
        ("PHI_TEMP", "Philadelphia", "KXHIGHPHIL"),
        ("PHX_TEMP", "Phoenix", "KXHIGHTPHX"),
        ("SEA_TEMP", "Seattle", "KXHIGHTSEA"),
        ("SFO_TEMP", "San Francisco", "KXHIGHTSFO"),
    ]

    def _register_assets(self):
        """Upsert every asset code the bot might emit into the FK'd assets table.

        `trades.asset` has a FK to `assets.symbol`. Any row referencing an
        unknown symbol fails the whole batch with HTTP 409 (code 23503). This
        registers crypto + weather cities up front so new assets do not silently
        break sync. Safe to call repeatedly — uses UPSERT.
        """
        rows = [{"symbol": s, "name": n, "series_ticker": t}
                for s, n, t in self._WEATHER_ASSETS]
        # Crypto are already in the table from initial schema but upsert is cheap
        for sym, name, ticker in (("BTC", "Bitcoin", "KXBTC15M"),
                                   ("ETH", "Ethereum", "KXETH15M"),
                                   ("SOL", "Solana", "KXSOL15M"),
                                   ("XRP", "Ripple", "KXXRP15M"),
                                   ("HYPE", "Hyperliquid", "KXHYPE15M"),
                                   ("DOGE", "Dogecoin", "KXDOGE15M"),
                                   ("BNB", "BNB", "KXBNB15M")):
            rows.append({"symbol": sym, "name": name, "series_ticker": ticker})
        if self._post("assets", rows):
            logging.info("Supabase assets: registered %d symbols", len(rows))
        else:
            logging.warning("Supabase assets: registration failed — trades with unknown assets will 409")

    # ── Schema parity ───────────────────────────────────────────────────

    def _validate_schema_parity(self):
        """Compare local SQLite columns to remote Supabase columns for each synced table.

        Logs WARNING with an ALTER TABLE suggestion for every column present locally
        (in the sync column list, or all cols for SELECT *-synced tables) but missing
        remotely. Prevents silent HTTP 400s on every insert attempt — the failure mode
        that lost weeks of evaluations/rejections data (2026-04-04) and kept
        spx_harrv_shadow_signals empty (2026-04-18).

        Non-fatal. Logs only, never blocks sync startup.
        """
        # Fetch PostgREST OpenAPI spec — single GET, returns all table schemas.
        try:
            resp = self._session.get(f"{self._url}/rest/v1/", timeout=10)
            if resp.status_code != 200:
                logging.warning("Supabase schema parity: OpenAPI fetch returned HTTP %d", resp.status_code)
                return
            spec = resp.json()
        except Exception:
            logging.warning("Supabase schema parity: OpenAPI fetch failed", exc_info=True)
            return
        remote_defs = spec.get("definitions", {}) or {}

        def _remote_cols(table: str) -> set:
            props = (remote_defs.get(table) or {}).get("properties") or {}
            return set(props.keys())

        def _local_cols(table: str) -> set:
            try:
                rows = self._db.execute(f"PRAGMA table_info({table})").fetchall()
                return {r["name"] for r in rows}
            except Exception:
                return set()

        # (local_table, remote_table, explicit_cols) — explicit_cols=None means "all local"
        checks = [
            ("evaluated_opportunities", "evaluations", set(c.strip() for c in self._EVAL_COLUMNS.split(","))),
            ("rejected_opportunities", "rejections", set(c.strip() for c in self._REJ_COLUMNS.split(","))),
            ("settled_trades", "trades", None),
            ("spx_harrv_shadow_signals", "spx_harrv_shadow_signals", None),
        ]

        total_drift = 0
        for local_tbl, remote_tbl, synced_cols in checks:
            local = _local_cols(local_tbl)
            if not local:
                continue  # local table doesn't exist; skip
            remote = _remote_cols(remote_tbl)
            if not remote:
                logging.warning(
                    "Supabase schema parity: remote table '%s' not found in OpenAPI (404 likely on every sync)",
                    remote_tbl,
                )
                total_drift += 1
                continue
            # What do we attempt to send?
            effective = synced_cols if synced_cols is not None else local
            missing = sorted(effective - remote)
            if missing:
                total_drift += len(missing)
                logging.warning(
                    "Supabase schema parity: %s -> %s missing %d column(s): %s",
                    local_tbl, remote_tbl, len(missing), missing,
                )
                for col in missing:
                    logging.warning(
                        "  SUGGEST: ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s <TYPE>;  -- then NOTIFY pgrst, 'reload schema';",
                        remote_tbl, col,
                    )
        if total_drift == 0:
            logging.info("Supabase schema parity: OK (all synced columns present remotely)")
        else:
            logging.warning("Supabase schema parity: %d total column(s) missing remotely — inserts will silently 400",
                            total_drift)

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
        """Push dashboard_state — operator row (id=1) + public row (id=2).

        Two rows, same table. Operator row has the full snapshot (155+ keys);
        public row has a strictly-whitelisted subset built by _build_public_snapshot().
        Frontend /dashboard/ reads id=1; /performance/ reads id=2. Public page can
        poll id=2 at its own cadence without touching operator data at all.

        See kb/concepts/public-dashboard-schema.md for the public-row contract.
        """
        try:
            sb = getattr(self._ml, "snapshot_builder", None)
            if sb and hasattr(sb, "_build_snapshot"):
                snapshot = sb._build_snapshot(db_conn=self._db)
            else:
                snapshot = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

            now_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            rows = [{
                "id": 1,
                "data": self._sanitize_for_json(snapshot),
                "updated_at": now_ts,
            }]

            # Public snapshot — whitelisted subset. Failure here must NOT block the
            # operator sync (operator is the critical path; public is observational).
            if sb and hasattr(sb, "_build_public_snapshot"):
                try:
                    public = sb._build_public_snapshot(db_conn=self._db)
                    rows.append({
                        "id": 2,
                        "data": self._sanitize_for_json(public),
                        "updated_at": now_ts,
                    })
                except Exception:
                    logging.warning("Supabase: public snapshot build failed", exc_info=True)

            self._post("dashboard_state", rows)
        except Exception:
            logging.warning("Supabase: dashboard sync failed", exc_info=True)

    # ── Incremental sync ────────────────────────────────────────────────

    def _sync_incremental(self):
        """Sync new rows from SQLite → Supabase using watermarks."""
        self._sync_evaluations()
        self._sync_rejections()
        self._sync_trades()
        self._sync_vol_params()
        self._sync_harrv()

    # Columns that map to Supabase `evaluations` table
    # cal_mlp_* fields require remote columns to exist (migration
    # `add_cal_mlp_columns_to_evaluations`, applied 2026-05-01) — adding to
    # this list without the remote columns silently 400s every batch (see
    # _check_schema_parity for the failure mode that lost data 2026-04-04).
    # Regression test: tests/integration/test_supabase_eval_columns_calmlp.py.
    _EVAL_COLUMNS = (
        "id, ticker, event_ticker, asset, filter_stage, rejection_reason, evaluation_time, "
        "spot_price, threshold, volatility, market_price, seconds_to_close, calibrated_prob, "
        "edge, ofa_adjustment, status, market_result, counterfactual_pnl, strategy, "
        "position_size, kelly_f, z_score, vol_regime, calibrated_prob_raw, settled_time, "
        "breakeven_wr, expected_value, drawdown_scaler, ask_depth, best_ask_source, "
        "ofa_confidence, raw_prob, calibration_method, old_system_prob, fee_adjusted_edge, "
        "egarch_sigma, egarch_blend_sigma, egarch_blend_weight, mz_r_squared, "
        "shadow_tv_blend_rv, mz_shadow_sigmoid_w, mz_baseline_qlike, mz_qlike, "
        "counterfactual, shadow_cal_prob, shadow_cal_fee_edge, shadow_cal_temperature, "
        "product_type, "
        "cal_mlp_request_id, cal_mlp_skipped_reason, cal_mlp_p_mean, cal_mlp_p_std, "
        "cal_mlp_final_lo, cal_mlp_final_hi, cal_mlp_train_id, "
        # Shadow coverage expansion Phase B (2026-05-02). Whitelist parity with
        # supabase migration 011 — adding here without the remote columns
        # silently HTTP-400s every batch and freezes sync (per migration 010
        # header + dashboard-drift postmortem). Migration 011 ships first.
        "n_open_positions, recent_n_outcome_streak, time_since_last_fill_s, "
        "maker_price_cents, maker_depth_at_post, maker_would_fill_within_30s, "
        "next_blocking_gate, "
        "final_spot_price, knockout_time_relative, max_excursion_from_strike, "
        "time_above_strike_seconds, time_below_strike_seconds, "
        "btc_spot_at_decision, eth_spot_at_decision, "
        "sol_spot_at_decision, xrp_spot_at_decision, "
        # Bit 2 / T1 cross-asset expansion (2026-05-11). Whitelist parity
        # with supabase migration 019 — adding here without the remote
        # columns silently HTTP-400s every batch and freezes sync (per
        # _validate_schema_parity at line 338, 2026-04-04 postmortem).
        # Migration 019 ships first; the operator applies it to the remote
        # out-of-band BEFORE this commit merges. ClickUp 86b9vrjf2.
        "hype_spot_at_decision, doge_spot_at_decision, "
        # BNB T1.5 followup (2026-05-17, ClickUp 86b9zn5pq). Whitelist parity
        # with supabase migration 021 — same HTTP-400-wedge risk as Bit 2
        # if added without the remote column. Migration 021 ships first;
        # the operator applies it to the remote out-of-band BEFORE merge.
        "bnb_spot_at_decision, "
        "okx_funding_rate_at_decision, deribit_funding_rate_at_decision, "
        # Phase G-6 (2026-05-03). Whitelist parity with supabase migration 012
        # — adding here without the remote column silently HTTP-400s every
        # batch. Migration 012 ships first.
        "data_provenance, "
        # Phase H-2 (2026-05-03). Whitelist parity with supabase migration
        # 013. Same HTTP-400 risk if added without remote column.
        "bot_state_snapshot_json"
    )

    # Columns that map to Supabase `rejections` table
    _REJ_COLUMNS = (
        "ticker, event_ticker, asset, rejection_reason, rejection_time, z_score, "
        "spot_price, threshold, volatility, market_price, seconds_to_close, "
        "calibrated_prob, status, raw_prob, market_result, egarch_sigma, "
        "egarch_blend_sigma, egarch_blend_weight, mz_r_squared, shadow_tv_blend_rv, "
        "mz_shadow_sigmoid_w, mz_baseline_qlike, mz_qlike, counterfactual, product_type"
    )

    # Columns that are integer-typed in Postgres but loose-typed (REAL-acceptable)
    # in SQLite. Any float value here causes 22P02 ("invalid input syntax for
    # type integer") and freezes the watermark on that batch. Defensive
    # round-to-int at sync time so a single upstream bug (like sports_engine
    # commit b6436f3 emitting float qty into ask_depth) can't wedge the mirror
    # for days. Source-side fixes are still required — this is belt-and-
    # suspenders.
    #
    # Authoritative union across all sync targets — derived from
    #   SELECT table_name, column_name FROM information_schema.columns
    #   WHERE table_schema='public' AND table_name IN
    #     ('evaluations','rejections','trades','spx_harrv_shadow_signals')
    #   AND data_type IN ('integer','bigint','smallint');
    # Re-run that query and update this set when adding new int columns.
    _INT_COLUMNS = frozenset({
        # evaluations
        "ask_depth", "available_balance_cents", "counterfactual_pnl", "id",
        "market_price", "no_ask_cents", "oft_n_snapshots", "position_size",
        "taker_ask_at_submit", "wx_n_members",
        # spx_harrv_shadow_signals (delta vs evaluations)
        "bankroll_cents", "best_ask", "best_bid", "est_fee_cents",
        "gates_passed", "n_ols_obs", "n_returns_1d", "n_returns_1h",
        "n_returns_1w", "no_contracts", "no_counterfactual_pnl",
        "no_gates_passed", "no_pnl_cents", "no_price", "rv_1d_imputed",
        "rv_1w_imputed", "settled_pnl", "shadow_contracts", "shadow_pnl_cents",
        # trades (delta)
        "count", "entry_price_cents", "fee_cents", "maker_price_cents",
        "pnl_cents", "revenue_cents",
        # bid_depth: not yet in remote evaluations schema but is the symmetric
        # twin of ask_depth (same source bug class — bot/engines/sports_engine.py — search anchor: `int(round(sum(`).
        # Including is a no-op if absent from payload; future-proofs a remote
        # migration that adds the column.
        "bid_depth",
        # Shadow coverage expansion Phase B (2026-05-02): integer-typed cols
        # in supabase migration 011. Defensive against future float upstream
        # writes (memory: project_may01_calmlp_dashboard_chain — 12d 22P02
        # wedge from a single float ask_depth).
        "n_open_positions", "recent_n_outcome_streak",
        "maker_price_cents", "maker_depth_at_post", "maker_would_fill_within_30s",
    })

    @classmethod
    def _coerce_int_columns(cls, row: dict) -> dict:
        """Return a NEW dict with known-integer columns rounded to int.
        None passes through; non-numeric values pass through unchanged
        (let Postgres raise the real error rather than masking type bugs).
        Mutating in place would make the sync non-idempotent on retry, so
        we copy. Cheap — dict is ~50 keys."""
        out = dict(row)
        for col in cls._INT_COLUMNS:
            v = out.get(col)
            if v is None or isinstance(v, bool):
                continue
            if isinstance(v, float):
                out[col] = int(round(v))
            # int passes through; str / other left alone (will surface as 22P02
            # if genuinely bad — that's the right escalation path)
        return out

    def _sync_evaluations(self):
        """Incremental sync of evaluated_opportunities by rowid."""
        try:
            rows = self._db.execute(
                f"SELECT {self._EVAL_COLUMNS} FROM evaluated_opportunities WHERE id > ? ORDER BY id LIMIT 500",
                (self._wm_evaluations,)
            ).fetchall()
            if not rows:
                return
            mapped = [
                self._coerce_int_columns(
                    {col: self._clean(r[col]) for col in r.keys()}
                )
                for r in rows
            ]
            if self._post("evaluations", mapped):
                new_wm = max(r["id"] for r in rows)
                self._wm_evaluations = new_wm
                self._save_watermark("evaluated_opportunities", new_wm, len(rows))
                logging.debug("Supabase: synced %d evaluations (wm=%d)", len(rows), new_wm)
        except Exception:
            logging.warning("Supabase: evaluations sync failed", exc_info=True)

    def _sync_rejections(self):
        """Incremental sync of rejected_opportunities by rowid."""
        try:
            rows = self._db.execute(
                f"SELECT rowid, {self._REJ_COLUMNS} FROM rejected_opportunities WHERE rowid > ? ORDER BY rowid LIMIT 500",
                (self._wm_rejections,)
            ).fetchall()
            if not rows:
                return
            mapped = []
            for r in rows:
                row_dict = {col: self._clean(r[col]) for col in r.keys() if col != "rowid"}
                mapped.append(self._coerce_int_columns(row_dict))
            if self._post("rejections", mapped):
                new_wm = max(r["rowid"] for r in rows)
                self._wm_rejections = new_wm
                self._save_watermark("rejected_opportunities", new_wm, len(rows))
                logging.debug("Supabase: synced %d rejections (wm=%d)", len(rows), new_wm)
        except Exception:
            logging.warning("Supabase: rejections sync failed", exc_info=True)

    def _sync_trades(self):
        """Sync settled_trades incrementally by rowid (monotonic, drift-immune).

        Previously keyed on settled_at string timestamps — VPS clock drift (13.6s observed)
        caused later-inserted rows to appear with earlier settled_at than the watermark,
        silently skipping them. Reconciliation (_reconcile_daily_pnl) caught gaps after
        the fact, but by then multiple days of trade-level drill-down data was missing
        from the dashboard. Rowid is stable insertion order, immune to clock issues.
        See kb/failures/dashboard-drift.md.
        """
        try:
            rows = self._db.execute("""
                SELECT rowid, ticker, event_ticker, asset, market_result, side, count,
                       entry_price_cents, revenue_cents, fee_cents, pnl_cents,
                       settled_at, strategy, seconds_to_close, fill_latency_seconds,
                       vol_regime, calibrated_prob, edge, kelly_f,
                       escalation_type, maker_price_cents, maker_wait_seconds,
                       product_type, strategy_group, is_stacked
                FROM settled_trades
                WHERE rowid > ?
                ORDER BY rowid
                LIMIT 500
            """, (self._wm_trades_rowid,)).fetchall()
            if not rows:
                return
            mapped = [
                self._coerce_int_columns(
                    {col: self._clean(r[col]) for col in r.keys() if col != "rowid"}
                )
                for r in rows
            ]
            if self._post("trades", mapped):
                new_wm = max(r["rowid"] for r in rows)
                self._wm_trades_rowid = new_wm
                self._save_watermark("settled_trades", new_wm, len(rows))
                logging.debug("Supabase: synced %d trades (rowid wm=%d)", len(rows), new_wm)
        except Exception:
            logging.warning("Supabase: trades sync failed", exc_info=True)

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

    def _sync_harrv(self):
        """Incremental sync of spx_harrv_shadow_signals by id."""
        try:
            # Check if table exists
            tables = [r[0] for r in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='spx_harrv_shadow_signals'"
            ).fetchall()]
            if not tables:
                return
            rows = self._db.execute(
                "SELECT * FROM spx_harrv_shadow_signals WHERE id > ? ORDER BY id LIMIT 100",
                (self._wm_harrv,)
            ).fetchall()
            if not rows:
                return
            mapped = [
                self._coerce_int_columns(
                    {col: self._clean(r[col]) for col in r.keys()}
                )
                for r in rows
            ]
            if self._post("spx_harrv_shadow_signals", mapped):
                new_wm = max(r["id"] for r in rows)
                self._wm_harrv = new_wm
                self._save_watermark("spx_harrv_shadow_signals", new_wm, len(rows))
                logging.debug("Supabase: synced %d harrv signals (wm=%d)", len(rows), new_wm)
        except Exception:
            logging.warning("Supabase: harrv sync failed", exc_info=True)

    # ── Periodic snapshots ──────────────────────────────────────────────

    def _sync_snapshots(self):
        """Push volatility snapshots, calibration state, and run reconciliation."""
        self._sync_vol_snapshots()
        self._sync_cal_snapshot()
        self._refresh_analytics_views()
        self._reconciliation_check()

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
                self._insert("volatility_snapshots", rows)
        except Exception:
            logging.debug("Supabase: vol snapshots failed", exc_info=True)

    def _sync_cal_snapshot(self):
        """Push calibration engine state."""
        try:
            cal = getattr(self._ml, "calibration", None)
            if not cal:
                return
            # Skip if calibration has no observations yet
            obs_count = len(getattr(cal, "_observations", []))
            if obs_count == 0:
                return
            import bot.runtime_config as _bot_mod  # Bit 9.3-iii.c (2026-05-11): replaces the bot._impl shim with the PEP 562 dual-probe view. See bot/runtime_config.py.
            brier = cal.rolling_brier_score()
            row = {
                "snapshot_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "active_method": cal.active_method,
                "beta_cal_brier": brier if brier < 1.0 else None,
                "temperature_brier": getattr(cal, "_temperature_brier", None),
                "beta_cal_a": getattr(cal, "_beta_a", None),
                "beta_cal_b": getattr(cal, "_beta_b", None),
                "temperature": getattr(cal, "_temperature", None),
                "sample_count": obs_count,
                "market_blend_weight": getattr(_bot_mod, "MARKET_BLEND_W", None),
                # P2.1.d (2026-05-13) + P2.3 (2026-05-14, 86b9xv66a): per-asset
                # 15M blend weights for all 6 production assets (BTC 0.10,
                # DOGE 0.60, ETH 0.20, HYPE 0.80, SOL 0.80, XRP 0.90).
                # Unknown assets + non-15M paths fall back to the scalar
                # above. Schema column added in
                # scripts/ops/supabase_migration_020_market_blend_weight_by_asset.sql
                # (jsonb). Soak monitor reads this for per-asset Brier
                # comparison against the per-asset baseline.
                "market_blend_weight_by_asset": getattr(_bot_mod, "MARKET_BLEND_W_BY_ASSET", {}),
            }
            self._insert("calibration_snapshots", [row])
        except Exception:
            logging.debug("Supabase: cal snapshot failed", exc_info=True)

    # ── Reconciliation ─────────────────────────────────────────────────

    def _reconciliation_check(self):
        """Compare SQLite vs Supabase row counts AND daily PnL, fix discrepancies.

        Row count check: logs warnings when gap > 10 rows.
        Daily PnL check: compares per-day PnL sums between SQLite and Supabase.
            If any day has a mismatch, does a full re-sync of trades for that day
            (delete + re-insert). This catches corrections/deletions on VPS side
            (e.g., ghost fill removals) that watermark-based sync misses.
        """
        self._reconcile_row_counts()
        self._reconcile_daily_pnl()

    def _reconcile_row_counts(self):
        """Compare row counts between SQLite and Supabase, log discrepancies."""
        try:
            for sqlite_tbl, sb_tbl in [
                ("evaluated_opportunities", "evaluations"),
                ("rejected_opportunities", "rejections"),
                ("settled_trades", "trades"),
            ]:
                local = self._db.execute(f"SELECT COUNT(*) FROM {sqlite_tbl}").fetchone()[0]
                resp = self._session.head(
                    f"{self._url}/rest/v1/{sb_tbl}?select=*",
                    headers={"Prefer": "count=exact", "Range-Unit": "items", "Range": "0-0"},
                    timeout=10,
                )
                remote_str = resp.headers.get("content-range", "*/0").split("/")[-1]
                remote = int(remote_str) if remote_str.isdigit() else 0
                gap = local - remote
                if gap > 10:
                    logging.warning("Supabase reconciliation: %s local=%d remote=%d gap=%d",
                                    sqlite_tbl, local, remote, gap)
        except Exception:
            logging.debug("Supabase: row count reconciliation failed", exc_info=True)

    def _reconcile_daily_pnl(self):
        """Compare daily PnL totals between SQLite and Supabase. Fix mismatches.

        Catches: ghost fill corrections, manual trade deletions, any VPS-side
        data fixes that the forward-only watermark sync misses.
        """
        try:
            # Get per-day PnL from SQLite. Stays GROSS for parity with the
            # Supabase remote `daily_pnl_summary()` RPC (which also sums gross).
            # Audit-PnL-fee-omission v3 (R-p7-deploy-r9): switching local to
            # net would mismatch every historical day with non-zero fees,
            # triggering the reconciliation's destructive DELETE+INSERT path
            # 4×/hour. Migrate the remote RPC FIRST, then revisit.
            local_rows = self._db.execute("""
                SELECT DATE(settled_at) AS day,
                       SUM(pnl_cents) AS pnl,  -- noqa: keep gross until Supabase migration 006
                       COUNT(*) AS cnt
                FROM settled_trades
                WHERE settled_at IS NOT NULL
                GROUP BY DATE(settled_at)
                ORDER BY day
            """).fetchall()
            if not local_rows:
                return
            local_daily = {r["day"]: (r["pnl"], r["cnt"]) for r in local_rows}

            # Get per-day PnL from Supabase
            resp = self._session.get(
                f"{self._url}/rest/v1/rpc/daily_pnl_summary",
                timeout=10,
            )
            if resp.status_code != 200:
                # RPC may not exist yet — skip silently
                logging.debug("Supabase: daily_pnl_summary RPC not available (HTTP %d)", resp.status_code)
                return

            remote_daily = {}
            for row in resp.json():
                day = row.get("day")
                if day:
                    remote_daily[day] = (row.get("pnl", 0), row.get("cnt", 0))

            # Find mismatches
            days_to_fix = []
            for day, (local_pnl, local_cnt) in local_daily.items():
                remote_pnl, remote_cnt = remote_daily.get(day, (None, None))
                if remote_pnl is None or local_pnl != remote_pnl or local_cnt != remote_cnt:
                    days_to_fix.append(day)

            # Also check for days in Supabase that don't exist locally (deleted trades)
            for day in remote_daily:
                if day not in local_daily:
                    days_to_fix.append(day)

            if not days_to_fix:
                return

            logging.warning("Supabase reconciliation: %d days with PnL mismatch: %s",
                            len(days_to_fix), days_to_fix[:5])

            # Fix each mismatched day: delete remote rows, re-insert from local
            for day in days_to_fix:
                self._fix_trades_for_day(day, day in local_daily)

            # Refresh materialized views after corrections
            if days_to_fix:
                self._rpc("refresh_analytics_views")
                logging.info("Supabase reconciliation: fixed %d days, refreshed views", len(days_to_fix))

        except Exception:
            logging.debug("Supabase: daily PnL reconciliation failed", exc_info=True)

    def _fix_trades_for_day(self, day: str, exists_locally: bool):
        """Delete and re-insert trades for a specific day."""
        try:
            # Delete remote trades for this day
            next_day = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            resp = self._session.delete(
                f"{self._url}/rest/v1/trades?settled_at=gte.{day}T00:00:00&settled_at=lt.{next_day}T00:00:00",
                headers={"Prefer": "return=minimal"},
                timeout=10,
            )
            self._request_count += 1
            if resp.status_code not in (200, 204):
                logging.debug("Supabase: failed to delete trades for %s (HTTP %d)", day, resp.status_code)
                return

            if not exists_locally:
                # Day was deleted on VPS — just the delete is enough
                logging.info("Supabase reconciliation: removed orphaned trades for %s", day)
                return

            # Re-insert from SQLite
            rows = self._db.execute("""
                SELECT ticker, event_ticker, asset, market_result, side, count,
                       entry_price_cents, revenue_cents, fee_cents, pnl_cents,
                       settled_at, strategy, seconds_to_close, fill_latency_seconds,
                       vol_regime, calibrated_prob, edge, kelly_f,
                       escalation_type, maker_price_cents, maker_wait_seconds,
                       product_type, strategy_group, is_stacked
                FROM settled_trades
                WHERE DATE(settled_at) = ?
            """, (day,)).fetchall()
            if rows:
                mapped = [{col: self._clean(r[col]) for col in r.keys()} for r in rows]
                self._post("trades", mapped)
                logging.info("Supabase reconciliation: re-synced %d trades for %s", len(rows), day)
        except Exception:
            logging.debug("Supabase: fix_trades_for_day(%s) failed", day, exc_info=True)

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
        if isinstance(val, float) and (val != val or val == float('inf') or val == float('-inf')):
            return None  # NaN/Inf are not JSON-compliant
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
