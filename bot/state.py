"""bot/state.py — StateManager extracted from bot/_impl.py (Bit 7.1, 2026-05-10).

Sprint 7 closes here (per kb/decisions/repo-modularization-plan-may05.md).
StateManager is the largest single leaf-class extraction in the modularization
track at ~2,607 lines (38 methods, 2 staticmethods).

This is **path-A++ extraction**, not byte-for-byte path-A. Two `globals()`
call sites inside StateManager.__init__ pre-extraction (bot/_impl.py:615-616)
resolved to bot._impl's namespace, which laundered the constants surface via
`from bot.config import *` (historical bot/_impl.py line 47, retargeted in Bit 12.1) + `from bot.constants import *` (historical bot/_impl.py line 83).
Path-A would have preserved the smell with a `_bot_impl_globals()` wrapper.
Path-A++ (user-authorized 2026-05-10 mid-Bit-7.1, per the modularization
strategic goal of reducing code smells) refactored
`scripts/cal_mlp/integration.py::parity_assert` and `sizing_parity_assert`
in-Bit to drop their `bot_globals` parameter:

  - `parity_assert(conn) -> tuple[str, int]` — imports the 21 names from
    bot.constants and 5 from config directly inside its function body.
    DRAWDOWN_HALT_FLOOR=0.10 fallback semantics preserved (literal).
    Returns (status, rowid) tuple instead of mutating caller globals.
  - `sizing_parity_assert(conn, *, rowid, compute_for_15m_main_path) -> str` —
    explicit keyword-only deps. Caller passes the rowid from parity_assert's
    return tuple and the `compute_for_15m_main_path` callable.
  - `make_compute_for_15m_main_path()` — Bit 7.1 fu (Smell 4, ticket
    86b9vhccw, 2026-05-10): dropped `bot_globals: dict` parameter. The
    closure now imports its 11 dependent names directly from
    `bot.constants` + `config` inside the function body, plus a literal
    `DRAWDOWN_HALT_FLOOR = 0.10` fallback (mirrors path-A++ pattern).

Post-Bit-9.3-iii.a (2026-05-11): this module has ZERO bot._impl edges.
`compute_for_15m_main_path` is top-imported from clean-leaf bot/boot.py
(relocated there in Bit 9.3-iii.a). The Bit 7.1 `_get_compute_for_15m_main_path()`
late-binding helper is GONE — no longer needed because bot.boot has zero
bot.state edges (no load-order cycle to avoid). The .importlinter
`state-no-impl-toplevel` carve-out + its 3 anti-regression tests were
retired in the same atomic commit.

Sister Bit 7.2 (`agent_docs/db_schema.md` refresh) ships in the same atomic
commit — the schema doc's source-of-truth for the 17 tables is now
StateManager._create_tables in this file.

Imports (5/3/1/1/5/2 partition):
  - bot.constants: DB_PATH, OB_CACHE_EVICT_AGE_SECONDS,
    OB_CACHE_FRESHNESS_SECONDS, SOL_RESCUE_CONTRACT_CAP, STACKING_ENABLED
  - bot.db_writer_registry: tracked_write, snapshot_active, recent_writes
    (cf34b5c db-locked instrumentation surface; load-bearing for
    SLOW_BATCH_BREAKDOWN failure-path logging)
  - bot.engines.calibration as _cal_state (Bit 6.3 path-B alias; used
    by failure-path logger to read _cal_state._CALIBRATION_ENGINE +
    _cal_state._resolve_cal_engine)
  - bot.kalshi_client.KalshiClient (type annotation only on
    reconcile_with_api / _reconcile_positions / _reconcile_orders)
  - integration (cal_mlp): CalMLPParityError, CalMLPSchemaError,
    migrate_schema, parity_assert (path-A++), sizing_parity_assert (path-A++)
  - models: calculate_fee, strategy_to_group

Forbidden-imports (per tests/integration/test_state_extraction.py::STATE_FORBIDDEN_IMPORTS):
  numpy, scipy, torch, sklearn, pandas — strict ban. StateManager is pure
  stdlib + sqlite3.

The `discover_active_windows()` module-level function did not move with
StateManager (Bit 7.1). Post-Bit-9.2 (2026-05-10) it lives in
`bot/settlement.py` (search anchor: `def discover_active_windows`),
bundled with SettlementTracker per master plan Phase Z+AA bundle decision.
"""
from __future__ import annotations

import os
import sys

# scripts/cal_mlp/ on sys.path for the bare `from integration import ...`
# call below. Mirrors bot/_impl.py:15. Idempotent — sys.path.insert(0, X)
# when X is already in sys.path just moves it to the front, no harm.
# Required when bot/state.py is imported in isolation (e.g., by test_state_extraction.py
# doing `import bot.state` before bot._impl has loaded).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, 'scripts', 'cal_mlp'))
del _REPO_ROOT

import datetime
import json
import logging
import random
import re
import sqlite3
import threading
import time
from datetime import timezone
from typing import Any, Dict, List, Optional, Set, Tuple

# bot.* leaf imports
from bot.constants import (
    DB_PATH,
    OB_CACHE_EVICT_AGE_SECONDS,
    OB_CACHE_FRESHNESS_SECONDS,
    SOL_RESCUE_CONTRACT_CAP,
    STACKING_ENABLED,
)
from bot.db_writer_registry import recent_writes, snapshot_active, tracked_write
from bot.engines import calibration as _cal_state  # Bit 6.3 path-B alias
# Bit 7.1 R1 follow-up: explicit imports from bot.helpers replace the
# star-import laundering that was load-bearing in bot/_impl.py
# (`from bot.helpers import *` at line 84 pre-extraction). Per L40 lesson
# (Bit 6.2 DIST_CONFIG analogue) — bare-name references inside the class
# body that came through star-imports must become explicit per-leaf imports
# at extraction time. Pre-flight free-variable scan missed these because
# the scan only checked module-level *definitions* in bot/_impl.py, not
# *imported* names. Found via post-extraction NameError fallout in
# tests/integration/test_tm_sweep_shadow.py + tests/integration/test_orderbook_logging_schema.py.
from bot.helpers import (
    compute_derived_features,
    compute_time_regime_features,
    dollars_str_to_cents,
    fp_str_to_int,
    tm_sweep_counterfactual_pnl,
)
# Sprint B Bit B.1a (2026-05-12): rejected_opportunities feature enrichment
# routes through these helpers. Mirrors the cal_mlp lock-step surface
# (bot/CLAUDE.md "cal_mlp feature transforms (lock-step)"). See ticket
# 86b9vfzjp + kb/decisions/sprint-b-bit-1a-shipped-may12.md.
from bot.helpers.derived_features import apply_sigma_winsor, compute_hour_sin_cos
from bot.kalshi_client import KalshiClient

# top-level modules — `scripts/cal_mlp/` is on sys.path (see top of file)
from integration import (
    CalMLPParityError,
    CalMLPSchemaError,
    migrate_schema as _calmlp_migrate_schema,
    parity_assert as _calmlp_parity_assert_impl,
    sizing_parity_assert as _calmlp_sizing_parity_assert_impl,
)
from bot.models import calculate_fee, strategy_to_group

# Bit 9.3-iii.a (2026-05-11): `compute_for_15m_main_path` closure relocated from
# bot/_impl.py to clean-leaf bot/boot.py. The Bit 7.1 `_get_compute_for_15m_main_path()`
# late-binding helper is GONE — bot.boot has zero bot.state edges, so the load-order
# cycle (bot._impl → bot.state → bot._impl) that originally required lazy access no
# longer exists. State now has ZERO bot._impl edges. The .importlinter
# `state-no-impl-toplevel` carve-out (Bit 7.1) was retired in the same atomic commit.
from bot.boot import compute_for_15m_main_path


class StateManager:
    """SQLite-backed persistent state. WAL mode for crash resilience."""

    def __init__(self, db_path: str = DB_PATH):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.row_factory = sqlite3.Row
        self._last_balance_cents: Optional[int] = None
        # Per-ticker yes_bid cache populated by scanner each tick.
        # Used by insert_evaluated_opportunity when caller doesn't pass yes_bid_cents explicitly.
        # Bounded by number of unique tickers seen — bot only sees ~10K tickers/day, ~1MB max.
        self._scan_bid_cache: Dict[str, int] = {}
        # Phase 1 feature caches (Apr 23): populated by scanner each tick, read by
        # insert_evaluated_opportunity. Same pattern as _scan_bid_cache.
        # _scan_ms_cache: per-ticker microstructure + Kalshi flow dict.
        # _scan_cx_gap_cache: per-asset Coinbase-vs-Kraken gap in bps (computed once per tick).
        # See kb/concepts/feature-engineering-phase1.md.
        self._scan_ms_cache: Dict[str, Dict[str, Any]] = {}
        self._scan_cx_gap_cache: Dict[str, float] = {}
        # Per-ticker top-N orderbook ladder JSON populated by scanner each
        # tick from current ob_data. Stored as (monotonic_ts, json) tuples
        # so reads can enforce a freshness gate — auto-filling a 15-minute
        # old ladder labeled as "now" is forensic poisoning, worse than NULL.
        # Same pattern as caches above but with TTL for both safety
        # (no stale data) and bounded memory (eviction on stale-write).
        # See kb/concepts/orderbook-depth-logging.md.
        self._scan_ob_cache: Dict[str, Tuple[float, str]] = {}
        # Lifecycle-snapshot failure counter (Phase 4). Exposed so auditor /
        # monitoring can detect "snapshots dropping silently" — the exact
        # failure mode the verify-new-features rule warns against.
        self._lifecycle_snapshot_failures: int = 0
        # Extended feature provider callback (Phase 2). Scanner attaches this
        # on construction to enrich insert_evaluated_opportunity rows with
        # Tier 1/2/3/6 features without threading kwargs through 96 call sites.
        # Signature: (ticker, asset, spot_price, threshold, product_type) -> Dict[str, Any]
        # Returns empty dict for non-15M or if no state. Must be fast (called on every insert).
        self._extended_feature_provider: Optional[Any] = None
        # Phase H-2 (2026-05-03): bot microstate snapshot provider callback.
        # MainLoop wires this AFTER both StateManager and MainLoop are
        # constructed (avoids circular ref at __init__). Signature:
        # `() -> Optional[Dict[str, Any]]` — returns the snapshot dict
        # (or None on failure). insert_evaluated_opportunity patches
        # lock_wait_ms after BEGIN IMMEDIATE and serializes inside the
        # lock; this decouples the heavy field-extraction from the
        # writer lock (round-1 wiring review #3 lock-window inflation).
        # Called on every 15M insert when the kwarg isn't explicitly
        # passed.
        self._bot_state_provider: Optional[Any] = None
        self._create_tables()
        # Phase 7 Edit 3a: cal_mlp deploy preconditions.
        # R-p7-cleanroom#M3: migrate_schema is in the same try/except as
        # parity_assert so a CalMLPSchemaError from _verify_wal surfaces
        # as a clean SystemExit(2) with a bot_startup_log row.
        try:
            _calmlp_migrate_schema(self.conn)
            _calmlp_parity_status, _calmlp_rowid = _calmlp_parity_assert_impl(self.conn)
            _calmlp_sizing_parity_assert_impl(
                self.conn,
                rowid=_calmlp_rowid,
                compute_for_15m_main_path=compute_for_15m_main_path,
            )
        except (CalMLPParityError, CalMLPSchemaError) as _calmlp_e:
            logging.error("[CALMLP_PARITY] FATAL: %s", _calmlp_e)
            raise SystemExit(2)
        # One-time backfill of cf_pnl_cents_with_97 for legacy tm_sweep_shadow
        # rows. Idempotent: SQL guard on `cf_pnl_cents_with_97 IS NULL` makes
        # subsequent restarts no-ops once all rows are populated.
        try:
            self.backfill_tm_sweep_with_97()
        except Exception:
            logging.warning("tm_sweep_shadow backfill at init failed", exc_info=True)
        # Seed balance cache from most recent DB value to avoid NULL gap after restart
        try:
            row = self.conn.execute(
                "SELECT available_balance_cents FROM evaluated_opportunities "
                "WHERE available_balance_cents IS NOT NULL ORDER BY evaluation_time DESC LIMIT 1"
            ).fetchone()
            if row:
                self._last_balance_cents = row[0]
        except Exception:
            pass

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                ticker TEXT PRIMARY KEY,
                event_ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                side TEXT NOT NULL,
                count INTEGER NOT NULL,
                avg_price_cents INTEGER NOT NULL,
                total_cost_cents INTEGER NOT NULL,
                opened_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
            );

            CREATE TABLE IF NOT EXISTS pending_orders (
                order_id TEXT PRIMARY KEY,
                client_order_id TEXT,
                ticker TEXT NOT NULL,
                event_ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                side TEXT NOT NULL,
                action TEXT NOT NULL,
                count INTEGER NOT NULL,
                price_cents INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'resting',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settled_trades (
                ticker TEXT PRIMARY KEY,
                event_ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                market_result TEXT NOT NULL,
                side TEXT NOT NULL,
                count INTEGER NOT NULL,
                entry_price_cents INTEGER NOT NULL,
                revenue_cents INTEGER NOT NULL,
                fee_cents INTEGER NOT NULL,
                pnl_cents INTEGER NOT NULL,
                settled_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS garch_params (
                asset TEXT PRIMARY KEY,
                omega REAL,
                alpha REAL,
                beta REAL,
                last_variance REAL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS egarch_params (
                asset TEXT PRIMARY KEY,
                omega REAL NOT NULL,
                alpha REAL NOT NULL,
                gamma REAL NOT NULL,
                beta REAL NOT NULL,
                last_log_variance REAL,
                mle_loglik REAL,
                mle_converged INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sports_platt_params (
                id INTEGER PRIMARY KEY DEFAULT 1,
                a REAL NOT NULL DEFAULT 1.0,
                b REAL NOT NULL DEFAULT 0.0,
                n_train INTEGER NOT NULL DEFAULT 0,
                h1_brier REAL,
                h2_brier_raw REAL,
                h2_brier_cal REAL,
                fitted INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_positions_asset
                ON positions(asset);
            CREATE INDEX IF NOT EXISTS idx_positions_status
                ON positions(status);
            CREATE INDEX IF NOT EXISTS idx_pending_orders_ticker
                ON pending_orders(ticker);
            CREATE INDEX IF NOT EXISTS idx_settled_trades_asset
                ON settled_trades(asset);

            CREATE TABLE IF NOT EXISTS rejected_opportunities (
                ticker TEXT PRIMARY KEY,
                event_ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                rejection_reason TEXT NOT NULL,
                rejection_time TEXT NOT NULL,
                z_score REAL,
                spot_price REAL,
                threshold REAL,
                volatility REAL,
                market_price INTEGER,
                seconds_to_close REAL,
                calibrated_prob REAL,
                status TEXT NOT NULL DEFAULT 'pending'
            );

            CREATE INDEX IF NOT EXISTS idx_rejected_status
                ON rejected_opportunities(status);

            CREATE TABLE IF NOT EXISTS evaluated_opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                event_ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                filter_stage TEXT NOT NULL,
                rejection_reason TEXT,
                evaluation_time TEXT NOT NULL,
                spot_price REAL,
                threshold REAL,
                volatility REAL,
                market_price INTEGER,
                seconds_to_close REAL,
                calibrated_prob REAL,
                edge REAL,
                ofa_adjustment REAL,
                status TEXT NOT NULL DEFAULT 'pending',
                market_result TEXT,
                counterfactual_pnl INTEGER
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_eval_opp_ticker_stage
                ON evaluated_opportunities(ticker, filter_stage);
            CREATE INDEX IF NOT EXISTS idx_eval_opp_status
                ON evaluated_opportunities(status);
            CREATE INDEX IF NOT EXISTS idx_eval_opp_ticker
                ON evaluated_opportunities(ticker);
        """)
        self.conn.commit()

        # SOL Path C shadow table: compares live taker override vs hypothetical maker-with-escalation
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sol_pathc_shadow (
                ticker TEXT PRIMARY KEY,
                evaluation_time TEXT,
                asset TEXT DEFAULT 'SOL',
                -- Live taker state (what actually happened)
                live_ask INTEGER,
                live_depth INTEGER,
                live_edge REAL,
                live_stc REAL,
                live_contracts INTEGER,
                live_entry_price INTEGER,
                live_cal_prob REAL,
                -- Path C maker hypothetical
                pathc_maker_price INTEGER,
                pathc_maker_offset INTEGER,
                pathc_depth_at_maker INTEGER,
                position_size INTEGER,
                -- Deferred observation (continuous monitoring during escalation window)
                obs_time TEXT,
                obs_elapsed_seconds REAL,
                obs_best_ask INTEGER,
                obs_depth INTEGER,
                obs_maker_would_fill INTEGER DEFAULT 0,
                obs_maker_price_touched INTEGER DEFAULT 0,
                -- Path C escalation taker hypothetical (if maker wouldn't fill)
                pathc_esc_ask INTEGER,
                pathc_esc_depth INTEGER,
                pathc_esc_edge REAL,
                -- Settlement
                status TEXT DEFAULT 'pending',
                market_result TEXT,
                settled_time TEXT,
                -- Counterfactual PnL
                live_pnl_cents INTEGER,
                pathc_maker_pnl_cents INTEGER,
                pathc_maker_contracts INTEGER,
                pathc_esc_pnl_cents INTEGER,
                pathc_esc_contracts INTEGER,
                pathc_best_pnl_cents INTEGER
            );
        """)
        self.conn.commit()

        # Sports shadow log table (independent from crypto)
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sports_shadow_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id TEXT, sport TEXT, league TEXT,
                home_team TEXT, away_team TEXT, home_code TEXT, away_code TEXT,
                pregame_fav_code TEXT, pregame_fav_prob REAL,
                pregame_price_home REAL, pregame_price_away REAL, pregame_price_draw REAL,
                scheduled_start TEXT, outcome_type TEXT,
                home_score INTEGER, away_score INTEGER, fav_score INTEGER, underdog_score INTEGER,
                deficit INTEGER, period INTEGER, clock TEXT, time_remaining_pct REAL,
                game_status TEXT, red_cards_fav INTEGER, red_cards_underdog INTEGER,
                ticker TEXT, event_ticker TEXT, yes_bid INTEGER, yes_ask INTEGER,
                mid_price REAL, spread INTEGER, ask_depth INTEGER, bid_depth INTEGER,
                comeback_prob REAL, prior REAL, likelihood_ratio REAL,
                edge REAL, fee_adjusted_edge REAL,
                signal_fired INTEGER DEFAULT 0, filter_stage TEXT, rejection_reason TEXT,
                simulated_contracts INTEGER, simulated_risk REAL,
                evaluation_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                espn_latency_ms REAL, kalshi_latency_ms REAL,
                closing_price REAL,
                final_home_score INTEGER, final_away_score INTEGER,
                fav_won INTEGER, market_result TEXT, pnl_cents INTEGER,
                would_signal_50c INTEGER DEFAULT 0,
                would_signal_60c INTEGER DEFAULT 0,
                would_signal_70c INTEGER DEFAULT 0,
                would_signal_80c INTEGER DEFAULT 0,
                would_signal_pregame_55 INTEGER DEFAULT 0,
                would_signal_pregame_65 INTEGER DEFAULT 0,
                market_implied_prob REAL,
                pregame_capture_method TEXT,
                shadow_lr_scale_50_posterior REAL,
                shadow_lr_scale_50_signal INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_sports_shadow_game
                ON sports_shadow_log(game_id);
            CREATE INDEX IF NOT EXISTS idx_sports_shadow_league
                ON sports_shadow_log(league);
            CREATE INDEX IF NOT EXISTS idx_sports_shadow_signal
                ON sports_shadow_log(signal_fired);
        """)
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS low_price_shadow_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                event_ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                window_id TEXT,
                market_price INTEGER,
                seconds_to_close REAL,
                calibrated_prob REAL, raw_prob REAL,
                edge REAL, fee_adjusted_edge REAL,
                z_score REAL, vol_regime TEXT, volatility REAL,
                spot_price REAL, threshold REAL,
                full_kelly_risk_fraction REAL, full_kelly_contracts INTEGER,
                capped_risk_fraction REAL, capped_contracts INTEGER,
                window_signal_count INTEGER, hour_signal_count INTEGER,
                evaluation_time TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                market_result TEXT,
                counterfactual_pnl_full REAL,
                counterfactual_pnl_capped REAL,
                settled_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_lps_ticker ON low_price_shadow_signals(ticker);
            CREATE INDEX IF NOT EXISTS idx_lps_status ON low_price_shadow_signals(status);
            CREATE INDEX IF NOT EXISTS idx_lps_asset ON low_price_shadow_signals(asset);
        """)
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS tm_sweep_shadow (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                event_ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                entry_time TEXT NOT NULL,
                entry_price_cents INTEGER NOT NULL,
                requested_count INTEGER NOT NULL,
                filled_count INTEGER NOT NULL,
                unfilled_count INTEGER NOT NULL,
                depth_at_entry_pre_fill INTEGER,
                depth_96c_pre INTEGER, depth_97c_pre INTEGER,
                depth_98c_pre INTEGER, depth_99c_pre INTEGER,
                depth_96c_post INTEGER, depth_97c_post INTEGER,
                depth_98c_post INTEGER, depth_99c_post INTEGER,
                seconds_to_close REAL,
                calibrated_prob REAL,
                buf_pct REAL,
                best_ask_source TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                market_result TEXT,
                settled_at TEXT,
                cf_pnl_cents INTEGER,
                cf_breakdown_json TEXT,
                cf_pnl_cents_with_97 INTEGER,
                direct_bump_applied INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_tmss_ticker ON tm_sweep_shadow(ticker);
            CREATE INDEX IF NOT EXISTS idx_tmss_status ON tm_sweep_shadow(status);
        """)
        # Idempotent migration for pre-existing DBs that lack cf_pnl_cents_with_97.
        # Kalshi IOCs cannot skip 97c — limit=98 fills 97 first. cf_pnl_cents
        # excludes 97 (analytical convenience, not implementable in production).
        # cf_pnl_cents_with_97 reflects what live execution would actually capture.
        # Adversary A4: silent ALTER failure produces "no such column" downstream.
        # We commit the ALTER, then re-read PRAGMA and assert the column exists.
        # If the assertion fails we raise — startup crash > silent shadow corruption.
        _existing_cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(tm_sweep_shadow)").fetchall()}
        if "cf_pnl_cents_with_97" not in _existing_cols:
            self.conn.execute(
                "ALTER TABLE tm_sweep_shadow ADD COLUMN cf_pnl_cents_with_97 INTEGER")
        self.conn.commit()
        # Post-migration verification (loud failure on silent ALTER drop).
        _post_cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(tm_sweep_shadow)").fetchall()}
        if "cf_pnl_cents_with_97" not in _post_cols:
            raise RuntimeError(
                "tm_sweep_shadow.cf_pnl_cents_with_97 column missing after "
                "migration — refusing to start with broken shadow schema")
        # Same idempotent ALTER for direct_bump_applied (Apr 28 2026 follow-up
        # to RCA on no-op deploy). cf_pnl interpretation differs for direct-
        # bumped rows (entry was effectively 99c, not scan-time price), so
        # analysts must filter on this column when aggregating cf_pnl.
        if "direct_bump_applied" not in _post_cols:
            self.conn.execute(
                "ALTER TABLE tm_sweep_shadow ADD COLUMN direct_bump_applied INTEGER")
            self.conn.commit()
            _post_cols2 = {r[1] for r in self.conn.execute(
                "PRAGMA table_info(tm_sweep_shadow)").fetchall()}
            if "direct_bump_applied" not in _post_cols2:
                raise RuntimeError(
                    "tm_sweep_shadow.direct_bump_applied column missing "
                    "after migration — refusing to start")
            _post_cols = _post_cols2

        # Unified shadow view: combines 15M shadow engines + hourly alt shadows
        # Safe to re-run; depends on fifteenm_shadow_signals + hourly_alt_shadow_signals
        try:
            self.conn.executescript("""
                DROP VIEW IF EXISTS unified_shadow_signals;
                CREATE VIEW unified_shadow_signals AS
                SELECT 'fifteenm' AS source, 'A1_recal_egarch' AS approach,
                       asset, evaluation_time, market_price,
                       a1_final_prob AS prob, a1_fee_edge AS fee_edge,
                       a1_kelly_f AS kelly_f, a1_contracts AS contracts,
                       a1_gates_passed AS gates_passed, a1_pnl_cents AS pnl_cents,
                       status, market_result, settled_time
                FROM fifteenm_shadow_signals WHERE a1_final_prob IS NOT NULL
                UNION ALL
                SELECT 'fifteenm', 'A2_lightgbm',
                       asset, evaluation_time, market_price,
                       a2_calibrated_prob, a2_fee_edge,
                       a2_kelly_f, a2_contracts,
                       a2_gates_passed, a2_pnl_cents,
                       status, market_result, settled_time
                FROM fifteenm_shadow_signals WHERE a2_raw_prob IS NOT NULL
                UNION ALL
                SELECT 'fifteenm', 'A3_gating',
                       asset, evaluation_time, market_price,
                       a3_gate_prob, NULL, NULL, NULL,
                       a3_gate_10, a3_pnl_gate10_cents,
                       status, market_result, settled_time
                FROM fifteenm_shadow_signals WHERE a3_gate_prob IS NOT NULL
                UNION ALL
                SELECT 'fifteenm', 'A4_late_window',
                       asset, evaluation_time, market_price,
                       live_prob, a4_edge, NULL, NULL,
                       a4_gates_passed, a4_pnl_cents,
                       status, market_result, settled_time
                FROM fifteenm_shadow_signals WHERE a4_gates_passed = 1
                UNION ALL
                SELECT 'hourly_alt', strategy,
                       asset, evaluation_time, market_price,
                       final_prob, fee_adjusted_edge,
                       kelly_f, shadow_contracts,
                       gates_passed, shadow_pnl_cents,
                       status, market_result, settled_time
                FROM hourly_alt_shadow_signals;
            """)
            self.conn.commit()
        except Exception:
            logging.debug("unified_shadow_signals view creation skipped (tables may not exist yet)")

        # Migration: add new columns to sports_shadow_log (safe to re-run)
        for col_def in [
            ("would_signal_50c", "INTEGER DEFAULT 0"),
            ("would_signal_60c", "INTEGER DEFAULT 0"),
            ("would_signal_70c", "INTEGER DEFAULT 0"),
            ("would_signal_80c", "INTEGER DEFAULT 0"),
            ("would_signal_pregame_55", "INTEGER DEFAULT 0"),
            ("would_signal_pregame_65", "INTEGER DEFAULT 0"),
            ("market_implied_prob", "REAL"),
            ("pregame_capture_method", "TEXT"),
            ("shadow_lr_scale_50_posterior", "REAL"),
            ("shadow_lr_scale_50_signal", "INTEGER DEFAULT 0"),
            ("score_changed", "INTEGER"),
            ("sport_group", "TEXT"),
            ("sport_lr_scale", "REAL"),
            ("is_strong_config", "INTEGER DEFAULT 0"),
            ("platt_prob", "REAL"),
            ("platt_edge", "REAL"),
            ("platt_fee_adj_edge", "REAL"),
            ("nba_variant", "TEXT"),
        ]:
            try:
                self.conn.execute(
                    f"ALTER TABLE sports_shadow_log ADD COLUMN {col_def[0]} {col_def[1]}")
            except Exception:
                pass  # Column already exists
        self.conn.commit()

        # One-time backfill: classify existing basketball signals into variants
        try:
            self.conn.execute("""
                UPDATE sports_shadow_log SET nba_variant = 'core'
                WHERE sport_group = 'basketball' AND nba_variant IS NULL
                AND pregame_fav_prob >= 0.65 AND deficit <= 1
                AND period = 1 AND time_remaining_pct > 0.75
                AND yes_ask <= 70
            """)
            self.conn.execute("""
                UPDATE sports_shadow_log SET nba_variant = 'wide'
                WHERE sport_group = 'basketball' AND nba_variant IS NULL
                AND pregame_fav_prob >= 0.65 AND deficit <= 3
                AND time_remaining_pct > 0.25
            """)
            self.conn.commit()
        except Exception:
            pass  # Safe to fail — backfill is best-effort

        # Migration: add new columns to evaluated_opportunities (safe to re-run)
        for col_def in [
            ("strategy", "TEXT"),
            ("position_size", "INTEGER"),
            ("kelly_f", "REAL"),
            ("z_score", "REAL"),
            ("vol_regime", "TEXT"),
            ("calibrated_prob_raw", "REAL"),
            ("settled_time", "TEXT"),
            ("breakeven_wr", "REAL"),
            ("expected_value", "REAL"),
            ("drawdown_scaler", "REAL"),
            ("ask_depth", "INTEGER"),
            ("best_ask_source", "TEXT"),
            ("ofa_confidence", "TEXT"),
            ("raw_prob", "REAL"),
            ("calibration_method", "TEXT"),
            ("old_system_prob", "REAL"),
            ("fee_adjusted_edge", "REAL"),
            ("egarch_sigma", "REAL"),
            ("egarch_blend_sigma", "REAL"),
            ("egarch_blend_weight", "REAL"),
            ("mz_r_squared", "REAL"),
            ("shadow_tv_blend_rv", "REAL"),
            ("mz_shadow_sigmoid_w", "REAL"),
            ("mz_baseline_qlike", "REAL"),
            ("mz_qlike", "REAL"),
            ("counterfactual", "TEXT"),
            ("shadow_cal_prob", "REAL"),
            ("shadow_cal_fee_edge", "REAL"),
            ("shadow_cal_temperature", "REAL"),
            ("product_type", "TEXT"),
            # OFT signal columns
            ("oft_prob_adjustment", "REAL"),
            ("oft_imbalance_ratio", "REAL"),
            ("oft_n_snapshots", "INTEGER"),
            # Weather ensemble columns
            ("wx_ensemble_mean", "REAL"),
            ("wx_ensemble_std", "REAL"),
            ("wx_bias_correction", "REAL"),
            ("wx_n_members", "INTEGER"),
            ("wx_market_type", "TEXT"),
            ("wx_actual_high_temp", "REAL"),
            ("wx_no_side_edge", "REAL"),
            ("wx_hrrr_temp", "REAL"),
            ("wx_corrected_mean", "REAL"),
            # Hourly temperature scaling columns
            ("hourly_pre_temp_prob", "REAL"),
            ("hourly_applied_temp_t", "REAL"),
            # Hourly shadow instrumentation columns
            ("hourly_shadow_temp_2_0", "REAL"),
            ("hourly_shadow_temp_1_0", "REAL"),
            ("hourly_shadow_temp_2_5", "REAL"),
            ("hourly_shadow_blend_50", "REAL"),
            ("hourly_shadow_temp_1_75", "REAL"),
            ("hourly_shadow_temp_3_0", "REAL"),
            ("hourly_shadow_blend_20", "REAL"),
            ("hourly_shadow_blend_30", "REAL"),
            ("hourly_shadow_blend_60", "REAL"),
            ("hourly_post_temp_prob", "REAL"),
            # Balance at evaluation time
            ("available_balance_cents", "INTEGER"),
            # Order tracking columns
            ("order_id", "TEXT"),
            ("order_submitted_at", "TEXT"),
            ("order_outcome", "TEXT"),
            # NO-side shadow: trade direction (yes=buy YES contract, no=buy NO contract)
            ("side", "TEXT DEFAULT 'yes'"),
            # Shadow taker tracking: best ask at maker order submission time
            ("taker_ask_at_submit", "INTEGER"),
            # NO-side pricing: actual NO ask from Kalshi NBBO (for DC-NO analysis)
            ("no_ask_cents", "INTEGER"),
            # YES bid at scan time — for buy-low-sell-higher and exit price analysis
            ("yes_bid_cents", "INTEGER"),
            # ── Extended feature instrumentation (Apr 19, Phase 1+2) ──
            # Tier 1: window/spot-path state (populated in Phase 2)
            ("minutes_above_strike", "REAL"),
            ("window_max_buf_pct", "REAL"),
            ("window_min_buf_pct", "REAL"),
            ("recent_crossings_5m", "INTEGER"),
            ("spot_at_window_open", "REAL"),
            # Tier 2: spot momentum (populated in Phase 2)
            ("spot_momentum_60s_bps", "REAL"),
            ("spot_momentum_5m_bps", "REAL"),
            ("spot_realized_range_15m_bps", "REAL"),
            # Tier 3: cross-asset (populated in Phase 2)
            ("btc_spot_change_30m_bps", "REAL"),
            ("btc_spot_change_5m_bps", "REAL"),
            ("btc_realized_vol_15m", "REAL"),
            ("sol_btc_relative_return_30m_bps", "REAL"),
            # Tier 4: time/regime (populated in Phase 1, fully backfillable)
            ("hour_of_day_utc", "INTEGER"),
            ("day_of_week", "INTEGER"),
            ("is_weekend", "INTEGER"),
            ("minutes_since_us_open", "INTEGER"),
            ("is_fomc_day", "INTEGER"),
            ("is_cpi_day", "INTEGER"),
            # Tier 5: derived (populated in Phase 1, backfillable from existing cols)
            ("spot_distance_to_strike_sigma", "REAL"),
            ("prob_breakeven_gap", "REAL"),
            ("kelly_vs_cap_ratio", "REAL"),
            ("calibration_confidence", "REAL"),
            # Tier 6: bot state (populated in Phase 2)
            ("active_positions_same_asset", "INTEGER"),
            ("recent_bot_pnl_30m_cents", "INTEGER"),
            ("current_drawdown_pct", "REAL"),
            ("recent_ioc_fill_success_rate_1h", "REAL"),
            # Feature-engineering Phase 1 (Apr 23): microstructure + cross-exchange + Kalshi flow.
            # Schema-lift of values already computed elsewhere — scanner populates caches
            # during tick, insert_evaluated_opportunity auto-fills from cache.
            # See kb/concepts/feature-engineering-phase1.md.
            ("yes_spread_cents", "INTEGER"),
            ("bid_depth", "INTEGER"),
            ("spot_coinbase_kraken_gap_bps", "REAL"),
            ("kalshi_flow_imbalance_level", "TEXT"),
            ("kalshi_flow_depth_velocity", "REAL"),
            ("kalshi_flow_depth_drain", "INTEGER"),
            # Per-level orderbook snapshot at evaluation time. Compact JSON
            # of top-N YES ladder via OrderExecutor._extract_book_levels.
            # See kb/concepts/orderbook-depth-logging.md.
            ("orderbook_levels_json", "TEXT"),
            # Shadow coverage expansion Phase B (2026-05-02). 18 nullable
            # columns spanning state-at-decision, maker counterfactual,
            # path-of-rejection, resolution metadata, cross-asset, funding.
            # Schema-only here; population ships in phases D/E/F.
            # See kb/decisions/shadow-coverage-expansion-may01.md.
            ("n_open_positions", "INTEGER"),
            ("recent_n_outcome_streak", "INTEGER"),
            ("time_since_last_fill_s", "REAL"),
            ("maker_price_cents", "INTEGER"),
            ("maker_depth_at_post", "INTEGER"),
            ("maker_would_fill_within_30s", "INTEGER"),
            ("next_blocking_gate", "TEXT"),
            ("final_spot_price", "REAL"),
            ("knockout_time_relative", "REAL"),
            ("max_excursion_from_strike", "REAL"),
            ("time_above_strike_seconds", "REAL"),
            ("time_below_strike_seconds", "REAL"),
            ("btc_spot_at_decision", "REAL"),
            ("eth_spot_at_decision", "REAL"),
            ("sol_spot_at_decision", "REAL"),
            ("xrp_spot_at_decision", "REAL"),
            # Bit 2 / T1 cross-asset expansion (2026-05-11). Supabase
            # migration 019 mirrors these to the remote evaluations table.
            # Scanner producer at bot/scanner/__init__.py:990 is already
            # ASSETS-driven (T1 5dca85a) and emits all 6 keys.
            ("hype_spot_at_decision", "REAL"),
            ("doge_spot_at_decision", "REAL"),
            ("okx_funding_rate_at_decision", "REAL"),
            ("deribit_funding_rate_at_decision", "REAL"),
            # Phase G-6 (2026-05-02): provenance flag for v2 calibrator
            # train/serve skew control. Live-bot inserts default 'live_ws'.
            # Backfilled rows stamped 'backfill_60s_inputs' via
            # scripts/backfill/stamp_data_provenance.py (one-time post-migration).
            # See kb/decisions/v2-train-must-account-for-backfill-skew-may02.md.
            ("data_provenance", "TEXT"),
            # Phase H-2 (2026-05-03): bot microstate forward capture.
            # JSON blob with scan_iter, scan_dt_ms, active_cooldowns,
            # api_error_counts, ws_cache_age_ms, open_positions_count,
            # lock_wait_ms. Default NULL until step 2 wires the
            # snapshot computation at the call site.
            # See kb/decisions/phase-h2-bot-microstate-fwd-may02.md.
            ("bot_state_snapshot_json", "TEXT"),
        ]:
            try:
                self.conn.execute(f"ALTER TABLE evaluated_opportunities ADD COLUMN {col_def[0]} {col_def[1]}")
            except sqlite3.OperationalError:
                pass  # column already exists
        self.conn.commit()

        # Migration: update unique index to include side (enables YES + NO rows per ticker/stage)
        # Check if index already includes side by trying to create the 3-column version;
        # if it succeeds the old 2-column index is replaced.
        try:
            self.conn.execute("DROP INDEX IF EXISTS idx_eval_opp_ticker_stage")
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_eval_opp_ticker_stage "
                "ON evaluated_opportunities(ticker, filter_stage, side)")
            self.conn.commit()
        except Exception:
            pass

        # Migration: add new columns to rejected_opportunities (safe to re-run)
        for col_def in [
            ("raw_prob", "REAL"),
            ("market_result", "TEXT"),
            ("egarch_sigma", "REAL"),
            ("egarch_blend_sigma", "REAL"),
            ("egarch_blend_weight", "REAL"),
            ("mz_r_squared", "REAL"),
            ("shadow_tv_blend_rv", "REAL"),
            ("mz_shadow_sigmoid_w", "REAL"),
            ("mz_baseline_qlike", "REAL"),
            ("mz_qlike", "REAL"),
            ("counterfactual", "TEXT"),
            ("product_type", "TEXT"),
            # OFT signal columns (for rejected markets with OFT data)
            ("oft_prob_adjustment", "REAL"),
            ("oft_imbalance_ratio", "REAL"),
            ("oft_n_snapshots", "INTEGER"),
            # NO-side pricing (for DC-NO analysis)
            ("no_ask_cents", "INTEGER"),
            # ── Sprint B Bit B.1a (2026-05-12, ticket 86b9vfzjp): training-data
            # feature enrichment so a future gate-policy learner can be
            # trained on rejected rows. Auto-populated by insert_rejection()
            # via the existing _scan_ob_cache + helper-based derivation
            # pattern. The cal_mlp anchors for sigma_winsorize / hour_sin /
            # hour_cos / prob_breakeven_gap are mirrored through
            # bot.helpers.derived_features.{apply_sigma_winsor,
            # compute_hour_sin_cos, compute_derived_features} — DO NOT
            # inline duplicate formulas (lock-step rule, bot/CLAUDE.md).
            # See kb/decisions/sprint-b-bit-1a-shipped-may12.md.
            ("sigma_winsorize", "REAL"),
            ("hour_sin", "REAL"),
            ("hour_cos", "REAL"),
            ("prob_breakeven_gap", "REAL"),
            ("vol_regime", "TEXT"),
            ("data_provenance", "TEXT"),
            ("orderbook_levels_json", "TEXT"),
        ]:
            try:
                self.conn.execute(f"ALTER TABLE rejected_opportunities ADD COLUMN {col_def[0]} {col_def[1]}")
            except sqlite3.OperationalError:
                pass  # column already exists
        self.conn.commit()

        # Migration: add enrichment columns to settled_trades
        for col_def in [
            ("strategy", "TEXT"),
            ("seconds_to_close", "REAL"),
            ("fill_latency_seconds", "REAL"),
            ("vol_regime", "TEXT"),
            ("calibrated_prob", "REAL"),
            ("edge", "REAL"),
            ("kelly_f", "REAL"),
            ("escalation_type", "TEXT"),
            ("maker_price_cents", "INTEGER"),
            ("maker_wait_seconds", "REAL"),
            ("product_type", "TEXT"),
            ("strategy_group", "TEXT DEFAULT 'main'"),
            ("is_stacked", "INTEGER DEFAULT 0"),
        ]:
            try:
                self.conn.execute(f"ALTER TABLE settled_trades ADD COLUMN {col_def[0]} {col_def[1]}")
            except sqlite3.OperationalError:
                pass  # column already exists
        self.conn.commit()

        # One-time backfill: derive product_type from ticker prefix
        null_count = self.conn.execute(
            "SELECT COUNT(*) FROM settled_trades WHERE product_type IS NULL"
        ).fetchone()[0]
        if null_count > 0:
            self.conn.execute(
                "UPDATE settled_trades SET product_type='15m' WHERE product_type IS NULL "
                "AND (ticker LIKE 'KXBTC15M%' OR ticker LIKE 'KXETH15M%' "
                "OR ticker LIKE 'KXSOL15M%' OR ticker LIKE 'KXXRP15M%' "
                "OR ticker LIKE 'KXHYPE15M%' OR ticker LIKE 'KXDOGE15M%')")
            self.conn.execute(
                "UPDATE settled_trades SET product_type='hourly' WHERE product_type IS NULL "
                "AND (ticker LIKE 'KXBTCD%' OR ticker LIKE 'KXETHD%' "
                "OR ticker LIKE 'KXSOLD%' OR ticker LIKE 'KXXRPD%' "
                "OR ticker LIKE 'KXHYPED%' OR ticker LIKE 'KXDOGED%')")
            self.conn.execute(
                "UPDATE settled_trades SET product_type='spx_hourly' WHERE product_type IS NULL "
                "AND ticker LIKE 'KXSPX%'")
            self.conn.execute(
                "UPDATE settled_trades SET product_type='weather' WHERE product_type IS NULL "
                "AND ticker LIKE 'KXHIGH%'")
            self.conn.commit()
            logging.info(f"Backfilled product_type for {null_count} settled_trades rows")

        # Same backfill for evaluated_opportunities (needed for per-asset CalEngine training)
        eo_null = self.conn.execute(
            "SELECT COUNT(*) FROM evaluated_opportunities WHERE product_type IS NULL"
        ).fetchone()[0]
        if eo_null > 0:
            self.conn.execute(
                "UPDATE evaluated_opportunities SET product_type='15m' WHERE product_type IS NULL "
                "AND (ticker LIKE 'KXBTC15M%' OR ticker LIKE 'KXETH15M%' "
                "OR ticker LIKE 'KXSOL15M%' OR ticker LIKE 'KXXRP15M%' "
                "OR ticker LIKE 'KXHYPE15M%' OR ticker LIKE 'KXDOGE15M%')")
            self.conn.execute(
                "UPDATE evaluated_opportunities SET product_type='hourly' WHERE product_type IS NULL "
                "AND (ticker LIKE 'KXBTCD%' OR ticker LIKE 'KXETHD%' "
                "OR ticker LIKE 'KXSOLD%' OR ticker LIKE 'KXXRPD%' "
                "OR ticker LIKE 'KXHYPED%' OR ticker LIKE 'KXDOGED%')")
            self.conn.execute(
                "UPDATE evaluated_opportunities SET product_type='spx_hourly' WHERE product_type IS NULL "
                "AND ticker LIKE 'KXSPX%'")
            self.conn.execute(
                "UPDATE evaluated_opportunities SET product_type='weather' WHERE product_type IS NULL "
                "AND ticker LIKE 'KXHIGH%'")
            self.conn.commit()
            logging.info(f"Backfilled product_type for {eo_null} evaluated_opportunities rows")

        # Migration: add enrichment columns to positions
        for col_def in [
            ("strategy", "TEXT"),
            ("seconds_to_close", "REAL"),
            ("fill_latency_seconds", "REAL"),
            ("vol_regime", "TEXT"),
            ("calibrated_prob", "REAL"),
            ("edge", "REAL"),
            ("kelly_f", "REAL"),
            ("is_taker", "INTEGER"),
            ("fill_source", "TEXT"),
            ("execution_method", "TEXT"),
            ("escalation_type", "TEXT"),
            ("maker_price_cents", "INTEGER"),
            ("maker_wait_seconds", "REAL"),
            ("strategy_group", "TEXT DEFAULT 'main'"),
            ("is_stacked", "INTEGER DEFAULT 0"),
            ("accumulated_fee_cents", "INTEGER DEFAULT 0"),
        ]:
            try:
                self.conn.execute(f"ALTER TABLE positions ADD COLUMN {col_def[0]} {col_def[1]}")
            except sqlite3.OperationalError:
                pass  # column already exists
        self.conn.commit()

        # Position price observations (post-entry monitoring) — v2: spot-price primary
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS position_price_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                observation_time TEXT NOT NULL,
                seconds_to_close REAL,
                spot_price REAL,
                threshold REAL,
                spot_buffer_pct REAL,
                yes_ask_cents INTEGER,
                yes_bid_cents INTEGER,
                entry_price_cents INTEGER NOT NULL,
                position_count INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'spot_only'
            )""")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ppo_ticker ON position_price_observations(ticker)")
        # Migration: per-level orderbook snapshot column on position observations
        for col_def in [
            ("orderbook_levels_json", "TEXT"),
        ]:
            try:
                self.conn.execute(
                    f"ALTER TABLE position_price_observations "
                    f"ADD COLUMN {col_def[0]} {col_def[1]}")
            except sqlite3.OperationalError:
                pass
        self.conn.commit()

        # Order lifecycle orderbook snapshots — captures book state at
        # IOC submit, fill (incl. partial), and cancel. Shares the parent
        # StateManager connection (WAL + busy_timeout=30000 already set
        # in __init__). DO NOT open a separate sqlite3.connect for this
        # table — adds contention without setting required PRAGMAs.
        # event_type is enum-constrained to catch typo writes (FILL/filled/etc).
        # POPULATED IN PHASE 4 — column NULL until OrderExecutor wiring lands.
        # TODO: retention policy — add daily prune of rows older than N days
        # once volume confirms (~50-200 rows/day expected from Phase 4).
        # See kb/concepts/orderbook-depth-logging.md.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS order_lifecycle_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                event_type TEXT NOT NULL
                    CHECK (event_type IN ('submit','fill','partial_fill','cancel')),
                observation_time TEXT NOT NULL,
                orderbook_levels_json TEXT,
                source TEXT
            )""")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ols_order_id "
            "ON order_lifecycle_snapshots(order_id)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ols_ticker_time "
            "ON order_lifecycle_snapshots(ticker, observation_time)")
        self.conn.commit()

        # Order-decision snapshots — captures the maker-vs-taker route
        # decision moment for every execute() / escalation call site, plus
        # an opportunistic 30s post-decision orderbook tick stream as a
        # JSON-blob column (approach (c) from Sprint B Bit B.2b ticket).
        # Sister table to order_lifecycle_snapshots:
        #   - order_lifecycle_snapshots = per-event (submit/fill/cancel)
        #   - order_decision_snapshots  = per-DECISION (route choice itself)
        # The two join on (ticker, time-proximity) for forensic replay.
        #
        # decision_type enum-constrained so typo writes ('MAKER'/'taker')
        # fail loudly instead of silently polluting forensic GROUP BY.
        # followup_ticks_json: appended to opportunistically by
        # StateManager.append_decision_followup_tick() during the 30s
        # post-decision window — gives the future execution-policy
        # learner the microstructure data to learn "maker vs taker at
        # this state — which was right?"
        #
        # Retention: 90 days, pruned daily via
        # StateManager.prune_old_decision_snapshots() invoked from
        # MainLoop._log_daily_summary (mirrors the audit_cron.prune_old
        # pattern). ~500-1000 rows/day × ~3KB = ~270 MB / 90d.
        # See kb/decisions/sprint-b-bit-2b-shipped-may12.md.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS order_decision_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                decision_time TEXT NOT NULL,
                decision_type TEXT NOT NULL
                    CHECK (decision_type IN ('maker_first','taker_first','escalate','shadow')),
                orderbook_levels_json TEXT,
                spot_price REAL,
                seconds_to_close REAL,
                vol_regime TEXT,
                source TEXT,
                followup_ticks_json TEXT
            )""")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ods_decision_id "
            "ON order_decision_snapshots(decision_id)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ods_ticker_time "
            "ON order_decision_snapshots(ticker, decision_time)")
        self.conn.commit()

        # Shadow exit signal table — tracks what early-exit would recommend
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS exit_signal_shadow (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                signal_time TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                buffer_at_signal REAL,
                pct_negative_30 REAL,
                entry_price_cents INTEGER,
                yes_bid_at_signal INTEGER,
                position_count INTEGER,
                seconds_to_close REAL,
                counterfactual_exit_pnl_cents INTEGER
            )""")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ess_ticker ON exit_signal_shadow(ticker)")
        self.conn.commit()

        # Cohort attribution daily aggregates — Money Printer Roadmap Phase 1
        # P1.1 (ticket 86b9x3kgd, 2026-05-12). Materialized nightly at 13:07
        # UTC by `scripts/audit/cohort_attribution_nightly.py` calling
        # `bot.helpers.cohort_attribution.run_aggregation(self.conn)`. The
        # canonical DDL lives there as the single source of truth — both
        # this bootstrap and the nightly script call `ensure_schema(conn)`
        # so the schema is owned in exactly one place.
        # Design doc: kb/decisions/cohort-measurement-design-may12.md.
        from bot.helpers.cohort_attribution import ensure_schema as _cohort_ensure_schema
        _cohort_ensure_schema(self.conn)
        self.conn.commit()

    # ── Ticker Parsing ────────────────────────────────────────────────────

    @staticmethod
    def _asset_from_ticker(ticker: str) -> str:
        """'KXBTC15M-26FEB211545-45' -> 'BTC'  (also handles 'KXBTC-...' legacy)"""
        prefix = ticker.split("-")[0]  # e.g. "KXBTC15M" or "KXBTC"
        if prefix.startswith("KX"):
            asset = prefix[2:]         # "BTC15M" or "BTC"
            # Strip known product suffixes
            for suffix in ("15M", "1H", "1D", "D"):
                if asset.endswith(suffix):
                    asset = asset[:-len(suffix)]
            return asset
        return prefix

    @staticmethod
    def _event_ticker_from_ticker(ticker: str) -> str:
        """'KXBTC15M-26FEB211545-45' -> 'KXBTC15M-26FEB211545'"""
        parts = ticker.split("-")
        if len(parts) >= 2:
            return f"{parts[0]}-{parts[1]}"
        return ticker

    # ── Reconciliation ────────────────────────────────────────────────────

    def reconcile_with_api(self, client: KalshiClient):
        """Sync local state with Kalshi API on startup. API always wins."""
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self._reconcile_positions(client, now)
        self._reconcile_orders(client, now)
        self.conn.commit()
        logging.info("State reconciliation complete")

        stacked = self.conn.execute(
            "SELECT ticker, COUNT(*) as n FROM positions "
            "WHERE status='open' GROUP BY ticker HAVING n > 1"
        ).fetchall()
        if stacked:
            if not STACKING_ENABLED:
                logging.warning(
                    "STACKING_DISABLED but %d tickers have multiple positions",
                    len(stacked))

    def _reconcile_positions(self, client: KalshiClient, now: str):
        api_resp = client.get_positions()
        if not api_resp or not api_resp.get("market_positions"):
            logging.warning("Could not fetch positions for reconciliation")
            return

        api_tickers: Set[str] = set()
        for pos in api_resp["market_positions"]:
            ticker = pos["ticker"]
            api_tickers.add(ticker)
            position_count = fp_str_to_int(pos.get("position_fp")) or (pos.get("position") or 0)

            if position_count == 0:
                _unsettled = self.conn.execute(
                    "SELECT COUNT(*) FROM positions WHERE ticker=? AND status='open'",
                    (ticker,)
                ).fetchone()[0]
                if _unsettled > 0:
                    logging.warning("RECONCILE_DELETE_UNSETTLED: %s has %d unsettled positions", ticker, _unsettled)
                self.conn.execute(
                    "DELETE FROM positions WHERE ticker = ?", (ticker,))
                continue

            side = "yes" if position_count > 0 else "no"
            count = abs(position_count)
            cost_d = pos.get("market_exposure_dollars")
            cost = dollars_str_to_cents(cost_d) if cost_d else (pos.get("market_exposure") or 0)
            avg_price = cost // count if count else 0

            local_rows = self.conn.execute(
                "SELECT strategy_group, count, total_cost_cents "
                "FROM positions WHERE ticker=? AND status='open'",
                (ticker,)
            ).fetchall()

            if len(local_rows) == 0:
                # No local position — INSERT from API
                asset = self._asset_from_ticker(ticker)
                event_ticker = self._event_ticker_from_ticker(ticker)
                self.conn.execute("""
                    INSERT INTO positions (ticker, event_ticker, asset, side,
                        count, avg_price_cents, total_cost_cents,
                        opened_at, updated_at, status)
                    VALUES (?,?,?,?,?,?,?,?,?,'open')
                """, (ticker, event_ticker, asset, side, count,
                      avg_price, cost, now, now))
            elif len(local_rows) == 1:
                sg = dict(local_rows[0])["strategy_group"]
                self.conn.execute("""
                    UPDATE positions SET side=?, count=?, avg_price_cents=?,
                        total_cost_cents=?, updated_at=?, status='open'
                    WHERE ticker=? AND strategy_group=?
                """, (side, count, avg_price, cost, now, ticker, sg))
            else:
                local_total = sum(dict(r)["count"] for r in local_rows)
                if local_total != count:
                    logging.warning(
                        "RECONCILE_MULTI_MISMATCH: %s local=%d api=%d — NOT auto-fixing",
                        ticker, local_total, count)

        # Remove local positions not on API
        local_rows = self.conn.execute(
            "SELECT ticker FROM positions WHERE status='open'"
        ).fetchall()
        for row in local_rows:
            if row["ticker"] not in api_tickers:
                self.conn.execute("""
                    UPDATE positions SET status='closed', updated_at=?
                    WHERE ticker=?
                """, (now, row["ticker"]))

    def _reconcile_orders(self, client: KalshiClient, now: str):
        api_resp = client.get_orders(status="resting")
        if api_resp is None:
            logging.warning("Could not fetch orders for reconciliation")
            return

        api_order_ids: Set[str] = set()
        resting_orders = api_resp.get("orders") or []

        # Cancel all stale resting orders on Kalshi — clean slate on startup.
        # These are maker orders from pre-restart that were never filled or canceled.
        # Leaving them resting consumes capital and can interfere with new orders.
        _stale_canceled = 0
        for order in resting_orders:
            oid = order["order_id"]
            api_order_ids.add(oid)
            ticker = order["ticker"]
            try:
                client.cancel_order(oid)
                _stale_canceled += 1
                logging.warning("STALE_ORDER_CLEANUP: canceled %s ticker=%s price=%s count=%s (resting since %s)",
                                oid, ticker,
                                order.get("yes_price_dollars") or order.get("yes_price", "?"),
                                order.get("remaining_count", "?"),
                                order.get("created_time", "?"))
            except Exception as e:
                logging.warning("STALE_ORDER_CLEANUP: failed to cancel %s: %s", oid, e)

        if _stale_canceled > 0:
            logging.info("STALE_ORDER_CLEANUP: canceled %d resting orders on startup", _stale_canceled)

        # Import any orders from API that we don't have locally (for history)
        for order in resting_orders:
            oid = order["order_id"]
            existing = self.conn.execute(
                "SELECT 1 FROM pending_orders WHERE order_id=?", (oid,)
            ).fetchone()
            if existing:
                # Mark as canceled (we just canceled it above)
                self.conn.execute("""
                    UPDATE pending_orders SET status='canceled', updated_at=?
                    WHERE order_id=?
                """, (now, oid))
                continue

            ticker = order["ticker"]
            asset = self._asset_from_ticker(ticker)
            event_ticker = self._event_ticker_from_ticker(ticker)
            # Prefer *_dollars fields (new FP API), fall back to legacy
            ypd = order.get("yes_price_dollars")
            npd = order.get("no_price_dollars")
            if ypd:
                price = dollars_str_to_cents(ypd)
            elif npd:
                price = dollars_str_to_cents(npd)
            else:
                price = order.get("yes_price", 0) or order.get("no_price", 0)

            remaining = fp_str_to_int(order.get("remaining_count_fp")) or (order.get("remaining_count") or 0)

            self.conn.execute("""
                INSERT INTO pending_orders (order_id, client_order_id, ticker,
                    event_ticker, asset, side, action, count, price_cents,
                    status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,'canceled',?,?)
            """, (oid, order.get("client_order_id", ""), ticker,
                  event_ticker, asset, order["side"], order["action"],
                  remaining, price,
                  order.get("created_time", now), now))

        # Mark local resting orders not on API as canceled
        local_rows = self.conn.execute(
            "SELECT order_id FROM pending_orders WHERE status='resting'"
        ).fetchall()
        for row in local_rows:
            if row["order_id"] not in api_order_ids:
                self.conn.execute("""
                    UPDATE pending_orders SET status='canceled', updated_at=?
                    WHERE order_id=?
                """, (now, row["order_id"]))

    # ── Phase H-2 provider hook ───────────────────────────────────────────

    def set_bot_state_provider(self, provider) -> None:
        """Wire a callable that returns the bot microstate snapshot dict
        for `bot_state_snapshot_json` on 15M inserts.

        Signature: `provider() -> Optional[Dict[str, Any]]`.
        Returns the snapshot dict OR None if the provider cannot build
        one. insert_evaluated_opportunity calls this per-insert on 15M
        rows when the kwarg isn't explicitly passed; it patches
        `lock_wait_ms` (measured AFTER BEGIN IMMEDIATE) into the dict
        and serializes inside the writer lock.

        Why dict (not pre-serialized JSON): the heavy field-extraction
        (api_error_counts walk, ws_cache_age_ms walk, open_positions
        cache read) happens OUTSIDE the writer lock; only the
        json.dumps + dict-patch run inside the lock.

        Why a setter (not constructor injection): MainLoop holds the
        StateManager AND the references the snapshot reads from
        (kalshi_feed, executor, _scan_iter, etc.). Setting the provider
        AFTER both are constructed avoids a circular import / partial-
        init problem at __init__ time.

        See bot_state_snapshot.compute_bot_state_snapshot for the helper
        that callers typically wrap in this provider.
        """
        self._bot_state_provider = provider

    # ── CRUD ──────────────────────────────────────────────────────────────

    def get_open_positions(self, asset: Optional[str] = None) -> List[Dict]:
        if asset:
            rows = self.conn.execute(
                "SELECT * FROM positions WHERE status='open' AND asset=?",
                (asset,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM positions WHERE status='open'"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_unsettled_positions(self) -> List[Dict]:
        """Return positions that are open or closed but not yet settled."""
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE status IN ('open', 'closed')"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_resting_orders(self, ticker: Optional[str] = None) -> List[Dict]:
        if ticker:
            rows = self.conn.execute(
                "SELECT * FROM pending_orders WHERE status='resting' AND ticker=?",
                (ticker,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM pending_orders WHERE status='resting'"
            ).fetchall()
        return [dict(r) for r in rows]

    def record_settlement(self, settlement: Dict,
                          revenue_override: Optional[int] = None,
                          pnl_override: Optional[int] = None,
                          fee_override: Optional[int] = None,
                          pos: Optional[Dict] = None):
        ticker = settlement["ticker"]
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        if pos is None:
            pos_row = self.conn.execute(
                "SELECT * FROM positions WHERE ticker=?", (ticker,)
            ).fetchone()
            if not pos_row:
                return
            pos = dict(pos_row)

        # Derive product_type from ticker prefix
        product_type = None
        if any(ticker.startswith(p) for p in ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXHYPE15M", "KXDOGE15M")):
            product_type = "15m"
        elif any(ticker.startswith(p) for p in ("KXBTCD", "KXETHD", "KXSOLD", "KXXRPD", "KXHYPED", "KXDOGED")):
            product_type = "hourly"
        elif ticker.startswith("KXSPX"):
            product_type = "spx_hourly"
        elif ticker.startswith("KXHIGH"):
            product_type = "weather"

        result = settlement.get("market_result", "")
        rev_d = settlement.get("revenue_dollars")
        revenue = dollars_str_to_cents(rev_d) if rev_d else (settlement.get("revenue") or 0)
        # For stacked positions, use per-row revenue (not full API aggregate)
        if revenue_override is not None:
            revenue = revenue_override
        total_cost = pos["total_cost_cents"]
        pnl = revenue - total_cost
        is_taker = bool(pos.get("is_taker"))
        recomputed_fee = calculate_fee(pos["count"], pos["avg_price_cents"], is_taker=is_taker)

        # Prefer per-fill accumulated fee (accurate for mixed maker/taker positions)
        # Fall back to recomputed fee for legacy positions without accumulated data
        accumulated = pos.get("accumulated_fee_cents")
        if accumulated and accumulated > 0:
            fee = accumulated
            if abs(fee - recomputed_fee) > 2:
                logging.info(
                    f"FEE_CORRECTION {ticker}: accumulated={accumulated}¢ "
                    f"recomputed={recomputed_fee}¢ delta={recomputed_fee - accumulated}¢")
        else:
            fee = recomputed_fee

        # Cross-check P&L/fee consistency with caller (canary for divergence)
        if pnl_override is not None and abs(pnl - pnl_override) > 2:
            logging.error(
                f"PNL_MISMATCH {ticker}: record_settlement computed={pnl}, "
                f"tracker passed={pnl_override}, delta={pnl - pnl_override}")
        if fee_override is not None and abs(fee - fee_override) > 2:
            logging.error(
                f"FEE_MISMATCH {ticker}: record_settlement computed={fee}, "
                f"tracker passed={fee_override}, delta={fee - fee_override}")

        _sg = pos.get("strategy_group", "main")
        _is_stacked = pos.get("is_stacked", 0)

        self.conn.execute("""
            INSERT OR REPLACE INTO settled_trades
                (ticker, event_ticker, asset, market_result, side, count,
                 entry_price_cents, revenue_cents, fee_cents, pnl_cents,
                 settled_at, strategy, seconds_to_close, fill_latency_seconds,
                 vol_regime, calibrated_prob, edge, kelly_f,
                 escalation_type, maker_price_cents, maker_wait_seconds,
                 product_type, strategy_group, is_stacked)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (ticker, pos["event_ticker"], pos["asset"], result,
              pos["side"], pos["count"], pos["avg_price_cents"],
              revenue, fee, pnl, now,
              pos.get("strategy"), pos.get("seconds_to_close"),
              pos.get("fill_latency_seconds"), pos.get("vol_regime"),
              pos.get("calibrated_prob"), pos.get("edge"), pos.get("kelly_f"),
              pos.get("escalation_type"), pos.get("maker_price_cents"),
              pos.get("maker_wait_seconds"),
              product_type, _sg, _is_stacked))

    def _get_fresh_ob_ladder(self, ticker: str) -> Optional[str]:
        """Return cached orderbook ladder JSON for ticker if fresh, else None.

        Single source of truth for the freshness gate used by all three
        auto-fill call sites (insert_evaluated_opportunity,
        insert_order_lifecycle_snapshot, position_price_observations).

        Stale entry → returns None → caller writes NULL — honest.
        Returning the stale entry would be forensic poisoning.
        """
        entry = self._scan_ob_cache.get(ticker)
        if entry is None:
            return None
        ts, json_str = entry
        if time.monotonic() - ts < OB_CACHE_FRESHNESS_SECONDS:
            return json_str
        return None

    def insert_order_lifecycle_snapshot(self, order_id: str, ticker: str,
                                         event_type: str,
                                         orderbook_levels_json: Optional[str] = None,
                                         source: Optional[str] = None) -> None:
        """Record a lifecycle event (submit / fill / partial_fill / cancel)
        for an order, with the prevailing book state.

        Auto-fills observation_time (now, UTC ISO8601) and
        orderbook_levels_json (from _scan_ob_cache, freshness-gated to
        OB_CACHE_FRESHNESS_SECONDS — stale → NULL, never lie).

        SOURCE VOCABULARY: caller must pass `source` as the strategy name
        (e.g. "terminal_momentum_96", "decided_t2") on EVERY event_type.
        Mixing strategy with execution-tier ('taker'/'maker') in the same
        column makes GROUP BY source meaningless. Tier is recoverable via
        order_id join with pending_orders / positions when needed.

        CHECK on event_type and NOT NULL on order_id are enforced by the
        Phase 2 schema; failures increment _lifecycle_snapshot_failures
        and re-raise so callers can decide (OrderExecutor wraps in try/
        except so a snapshot failure never breaks order flow).

        COMMIT PATTERN: per-call commit, consistent with other StateManager
        helpers. Volume estimate ~150 commits/day (50 trades × ~2 fills +
        50 submits) — well below PM-001 threshold of 91 commits in tight
        loop. If contention metrics later show this is hot, refactor to
        deferred-flush. Shares self.conn (WAL + busy_timeout=30000).
        """
        if orderbook_levels_json is None:
            orderbook_levels_json = self._get_fresh_ob_ladder(ticker)
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        try:
            self.conn.execute(
                "INSERT INTO order_lifecycle_snapshots "
                "(order_id, ticker, event_type, observation_time, "
                "orderbook_levels_json, source) VALUES (?, ?, ?, ?, ?, ?)",
                (order_id, ticker, event_type, now, orderbook_levels_json, source))
            self.conn.commit()
        except Exception:
            self._lifecycle_snapshot_failures += 1
            raise

    # ── Sprint B Bit B.2b — order-decision snapshots ─────────────────
    # Decision-data capture for future execution-policy training. ONE
    # row per maker-vs-taker decision point (route choice itself);
    # NOT one row per orderbook tick (regime-classifier territory,
    # explicitly out of scope per Bit B.0 design spike). Escalations
    # write a second row sharing the original decision_id so a learner
    # can reconstruct the "maker_first@t0 → escalate@t15s" sequence.

    # Hard cap on the 30s follow-up tick stream — defensive against
    # callers who forget to gate by elapsed time. 30s @ 5s tick cadence
    # gives ≤ 6 ticks; 8 is safe headroom for tick-period jitter.
    DECISION_FOLLOWUP_WINDOW_S: float = 30.0
    DECISION_FOLLOWUP_MAX_TICKS: int = 8

    def insert_decision_snapshot(self,
                                 decision_id: str,
                                 ticker: str,
                                 asset: str,
                                 decision_type: str,
                                 orderbook_levels_json: Optional[str] = None,
                                 spot_price: Optional[float] = None,
                                 seconds_to_close: Optional[float] = None,
                                 vol_regime: Optional[str] = None,
                                 source: Optional[str] = None) -> None:
        """Record a route decision (maker_first / taker_first / escalate /
        shadow) with the prevailing book state + spot context.

        Auto-fills decision_time (now, UTC ISO8601) and
        orderbook_levels_json (from _scan_ob_cache via the freshness-
        gated _get_fresh_ob_ladder — stale → NULL, never lie). Mirrors
        the existing insert_order_lifecycle_snapshot contract.

        decision_id MUST be the same string across the route call AND
        any subsequent escalation row, so a learner can join the two
        events into a single sequence. The candidate dict carries it
        as candidate['decision_id'] from execute() onward.

        CHECK on decision_type and NOT NULL on (decision_id, ticker,
        asset, decision_time, decision_type) are enforced by the schema.

        COMMIT PATTERN: per-call commit, consistent with the existing
        insert_order_lifecycle_snapshot helper. Volume estimate
        ~500-1000 commits/day — well below PM-001 tight-loop threshold.
        Shares self.conn (WAL + busy_timeout=30000).
        """
        if orderbook_levels_json is None:
            orderbook_levels_json = self._get_fresh_ob_ladder(ticker)
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        try:
            self.conn.execute(
                "INSERT INTO order_decision_snapshots "
                "(decision_id, ticker, asset, decision_time, decision_type, "
                " orderbook_levels_json, spot_price, seconds_to_close, "
                " vol_regime, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (decision_id, ticker, asset, now, decision_type,
                 orderbook_levels_json, spot_price, seconds_to_close,
                 vol_regime, source))
            self.conn.commit()
        except Exception:
            logging.warning(
                "insert_decision_snapshot failed for %s decision_id=%s",
                ticker, decision_id, exc_info=True)
            raise

    def append_decision_followup_tick(self,
                                      decision_id: str,
                                      t_offset_s: float,
                                      best_yes_ask: Optional[int] = None,
                                      best_yes_bid: Optional[int] = None,
                                      ask_depth: Optional[int] = None,
                                      bid_depth: Optional[int] = None) -> None:
        """Append a single orderbook tick to the 30s post-decision
        followup stream for decision_id. Stored as a JSON list on the
        snapshot row's followup_ticks_json column (option (c) — no
        new table; single-row queryability).

        Silently drops ticks beyond DECISION_FOLLOWUP_WINDOW_S or beyond
        DECISION_FOLLOWUP_MAX_TICKS — defensive against caller bugs
        (the executor tick loop SHOULD gate by elapsed time, this is
        belt-and-braces).

        No-ops if decision_id isn't found (e.g., snapshot insert failed
        upstream — we never resurrect rows).
        """
        if t_offset_s > self.DECISION_FOLLOWUP_WINDOW_S:
            return
        try:
            row = self.conn.execute(
                "SELECT followup_ticks_json FROM order_decision_snapshots "
                "WHERE decision_id=? ORDER BY id DESC LIMIT 1",
                (decision_id,)).fetchone()
            if row is None:
                return
            existing_raw = row["followup_ticks_json"]
            ticks = json.loads(existing_raw) if existing_raw else []
            if len(ticks) >= self.DECISION_FOLLOWUP_MAX_TICKS:
                return
            ticks.append({
                "t_offset_s": float(t_offset_s),
                "best_yes_ask": best_yes_ask,
                "best_yes_bid": best_yes_bid,
                "ask_depth": ask_depth,
                "bid_depth": bid_depth,
            })
            self.conn.execute(
                "UPDATE order_decision_snapshots SET followup_ticks_json=? "
                "WHERE decision_id=? AND id=("
                "  SELECT id FROM order_decision_snapshots "
                "  WHERE decision_id=? ORDER BY id DESC LIMIT 1)",
                (json.dumps(ticks), decision_id, decision_id))
            self.conn.commit()
        except Exception:
            logging.warning(
                "append_decision_followup_tick failed decision_id=%s "
                "t=%.1f", decision_id, t_offset_s, exc_info=True)
            # Don't re-raise — tick capture is best-effort.

    def prune_old_decision_snapshots(self, days: int = 90) -> int:
        """Delete order_decision_snapshots rows older than `days` days.
        Returns the number of rows deleted. Idempotent — running twice
        in the same day prunes 0 the second time. Invoked from
        MainLoop._log_daily_summary (daily housekeeping hook).

        Mirrors scripts/audit/audit_cron.prune_old() pattern. Volume estimate
        ~500-1000 rows/day → ~45-90K rows steady-state at 90d.
        """
        cutoff = (datetime.datetime.now(timezone.utc)
                  - datetime.timedelta(days=days)
                  ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        try:
            cur = self.conn.execute(
                "DELETE FROM order_decision_snapshots WHERE decision_time < ?",
                (cutoff,))
            deleted = cur.rowcount or 0
            self.conn.commit()
            if deleted:
                logging.info(
                    "prune_old_decision_snapshots: deleted %d rows older "
                    "than %dd (cutoff=%s)", deleted, days, cutoff)
            return deleted
        except Exception:
            logging.warning(
                "prune_old_decision_snapshots failed", exc_info=True)
            return 0

    def _evict_stale_ob_cache(self) -> None:
        """Drop _scan_ob_cache entries older than OB_CACHE_EVICT_AGE_SECONDS.

        Called opportunistically by the scanner each tick. Bounds memory
        from quiet/closed-market tickers — failure_15m_silence_apr24_second
        was a similar 'WS cache phantom state' growth pattern.

        O(n) over current cache; n is bounded by active-ticker count
        (~30-50 in practice), so cost is trivial.
        """
        _now = time.monotonic()
        # Materialize the expired keys before mutating the dict
        _stale = [k for k, (ts, _) in self._scan_ob_cache.items()
                  if _now - ts >= OB_CACHE_EVICT_AGE_SECONDS]
        for k in _stale:
            self._scan_ob_cache.pop(k, None)

    # ── Rejected Opportunities ─────────────────────────────────────────

    def insert_rejection(self, ticker: str, event_ticker: str, asset: str,
                         rejection_reason: str, z_score: Optional[float],
                         spot_price: Optional[float], threshold: Optional[float],
                         volatility: Optional[float], market_price: Optional[int],
                         seconds_to_close: Optional[float],
                         calibrated_prob: Optional[float],
                         raw_prob: Optional[float] = None,
                         egarch_sigma: Optional[float] = None,
                         egarch_blend_sigma: Optional[float] = None,
                         egarch_blend_weight: Optional[float] = None,
                         mz_r_squared: Optional[float] = None,
                         shadow_tv_blend_rv: Optional[float] = None,
                         mz_shadow_sigmoid_w: Optional[float] = None,
                         mz_baseline_qlike: Optional[float] = None,
                         mz_qlike: Optional[float] = None,
                         counterfactual: Optional[str] = None,
                         product_type: Optional[str] = None,
                         oft_prob_adjustment: Optional[float] = None,
                         oft_imbalance_ratio: Optional[float] = None,
                         oft_n_snapshots: Optional[int] = None,
                         no_ask_cents: Optional[int] = None,
                         # ── Sprint B Bit B.1a (2026-05-12, ticket 86b9vfzjp) ──
                         # Feature enrichment so a future gate-policy learner
                         # can be trained on rejected rows. Most are auto-filled
                         # from the existing inputs + caches (see body below).
                         # Caller may override by passing an explicit non-None
                         # value (mirrors insert_evaluated_opportunity semantics).
                         vol_regime: Optional[str] = None,
                         data_provenance: str = 'live_ws',
                         orderbook_levels_json: Optional[str] = None,
                         sigma_winsorize: Optional[float] = None,
                         hour_sin: Optional[float] = None,
                         hour_cos: Optional[float] = None,
                         prob_breakeven_gap: Optional[float] = None):
        """Insert a rejected opportunity. INSERT OR IGNORE keeps the first rejection reason.

        Sprint B Bit B.1a (2026-05-12) added auto-fill for the 7 new
        training-data columns. Auto-fill skips when the caller passed
        a non-None value (explicit-wins, same as insert_evaluated_opportunity).
        Honest-NULL rule: when a derivation has insufficient inputs (e.g.
        prob_breakeven_gap with calibrated_prob=None at price_out_of_range_early)
        we write NULL rather than fabricating a value.
        """
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        # ── Auto-compute orderbook ladder snapshot from scanner cache ──
        # Same freshness-gated path used by insert_evaluated_opportunity
        # (10s gate; stale entries → None — honest, never lie).
        if orderbook_levels_json is None:
            orderbook_levels_json = self._get_fresh_ob_ladder(ticker)

        # ── Auto-compute Tier 4 hour_sin/hour_cos (cyclic 24h embedding) ──
        # Cal_mlp lock-step routes through bot.helpers.derived_features.
        # Derived from rejection-time (already in scope as `now`) when the
        # caller did not pass them explicitly.
        if hour_sin is None or hour_cos is None:
            _t4 = compute_time_regime_features(now)
            _hour = _t4.get("hour_of_day_utc")
            _hs, _hc = compute_hour_sin_cos(_hour)
            if hour_sin is None:
                hour_sin = _hs
            if hour_cos is None:
                hour_cos = _hc

        # ── Auto-compute Tier 5 derived features (prob_breakeven_gap +
        # spot_distance_to_strike_sigma → winsorized) ──
        if prob_breakeven_gap is None or sigma_winsorize is None:
            _t5 = compute_derived_features(
                spot_price=spot_price, threshold=threshold, volatility=volatility,
                seconds_to_close=seconds_to_close, calibrated_prob=calibrated_prob,
                market_price_cents=market_price,
            )
            if prob_breakeven_gap is None:
                prob_breakeven_gap = _t5["prob_breakeven_gap"]
            if sigma_winsorize is None:
                # Winsorize at ±SIGMA_WINSOR_ABS_CAP=25.0 to match
                # train-time clipping in scripts/cal_mlp/features.py.
                sigma_winsorize = apply_sigma_winsor(
                    _t5["spot_distance_to_strike_sigma"]
                )

        self.conn.execute("""
            INSERT OR IGNORE INTO rejected_opportunities
                (ticker, event_ticker, asset, rejection_reason, rejection_time,
                 z_score, spot_price, threshold, volatility, market_price,
                 seconds_to_close, calibrated_prob, raw_prob, status,
                 egarch_sigma, egarch_blend_sigma, egarch_blend_weight, mz_r_squared,
                 shadow_tv_blend_rv, mz_shadow_sigmoid_w, mz_baseline_qlike, mz_qlike,
                 counterfactual, product_type,
                 oft_prob_adjustment, oft_imbalance_ratio, oft_n_snapshots,
                 no_ask_cents,
                 sigma_winsorize, hour_sin, hour_cos, prob_breakeven_gap,
                 vol_regime, data_provenance, orderbook_levels_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (ticker, event_ticker, asset, rejection_reason, now,
              z_score, spot_price, threshold, volatility, market_price,
              seconds_to_close, calibrated_prob, raw_prob, "pending",
              egarch_sigma, egarch_blend_sigma, egarch_blend_weight, mz_r_squared,
              shadow_tv_blend_rv, mz_shadow_sigmoid_w, mz_baseline_qlike, mz_qlike,
              counterfactual, product_type,
              oft_prob_adjustment, oft_imbalance_ratio, oft_n_snapshots,
              no_ask_cents,
              sigma_winsorize, hour_sin, hour_cos, prob_breakeven_gap,
              vol_regime, data_provenance, orderbook_levels_json))
        self.conn.commit()

    def get_unsettled_rejections(self) -> List[Dict]:
        """Return all rejected opportunities with status='pending'."""
        rows = self.conn.execute(
            "SELECT * FROM rejected_opportunities WHERE status='pending'"
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_rejection_settled(self, ticker: str,
                              market_result: Optional[str] = None,
                              counterfactual: Optional[str] = None):
        """Set status='settled' and update result/counterfactual for a rejected opportunity."""
        self.conn.execute(
            """UPDATE rejected_opportunities
               SET status='settled', market_result=?, counterfactual=?
               WHERE ticker=?""",
            (market_result, counterfactual, ticker)
        )
        self.conn.commit()

    # ── Evaluated Opportunities ────────────────────────────────────────

    def insert_evaluated_opportunity(self, ticker: str, event_ticker: str,
                                     asset: str, filter_stage: str,
                                     rejection_reason: Optional[str] = None,
                                     spot_price: Optional[float] = None,
                                     threshold: Optional[float] = None,
                                     volatility: Optional[float] = None,
                                     market_price: Optional[int] = None,
                                     seconds_to_close: Optional[float] = None,
                                     calibrated_prob: Optional[float] = None,
                                     edge: Optional[float] = None,
                                     ofa_adjustment: Optional[float] = None,
                                     strategy: Optional[str] = None,
                                     position_size: Optional[int] = None,
                                     kelly_f: Optional[float] = None,
                                     z_score: Optional[float] = None,
                                     vol_regime: Optional[str] = None,
                                     calibrated_prob_raw: Optional[float] = None,
                                     breakeven_wr: Optional[float] = None,
                                     expected_value: Optional[float] = None,
                                     drawdown_scaler: Optional[float] = None,
                                     ask_depth: Optional[int] = None,
                                     best_ask_source: Optional[str] = None,
                                     ofa_confidence: Optional[str] = None,
                                     raw_prob: Optional[float] = None,
                                     calibration_method: Optional[str] = None,
                                     old_system_prob: Optional[float] = None,
                                     fee_adjusted_edge: Optional[float] = None,
                                     egarch_sigma: Optional[float] = None,
                                     egarch_blend_sigma: Optional[float] = None,
                                     egarch_blend_weight: Optional[float] = None,
                                     mz_r_squared: Optional[float] = None,
                                     shadow_tv_blend_rv: Optional[float] = None,
                                     mz_shadow_sigmoid_w: Optional[float] = None,
                                     mz_baseline_qlike: Optional[float] = None,
                                     mz_qlike: Optional[float] = None,
                                     counterfactual: Optional[str] = None,
                                     shadow_cal_prob: Optional[float] = None,
                                     shadow_cal_fee_edge: Optional[float] = None,
                                     shadow_cal_temperature: Optional[float] = None,
                                     product_type: Optional[str] = None,
                                     oft_prob_adjustment: Optional[float] = None,
                                     oft_imbalance_ratio: Optional[float] = None,
                                     oft_n_snapshots: Optional[int] = None,
                                     wx_ensemble_mean: Optional[float] = None,
                                     wx_ensemble_std: Optional[float] = None,
                                     wx_bias_correction: Optional[float] = None,
                                     wx_n_members: Optional[int] = None,
                                     wx_market_type: Optional[str] = None,
                                     wx_actual_high_temp: Optional[float] = None,
                                     wx_no_side_edge: Optional[float] = None,
                                     wx_hrrr_temp: Optional[float] = None,
                                     wx_corrected_mean: Optional[float] = None,
                                     hourly_pre_temp_prob: Optional[float] = None,
                                     hourly_applied_temp_t: Optional[float] = None,
                                     hourly_shadow_temp_2_0: Optional[float] = None,
                                     hourly_shadow_temp_1_0: Optional[float] = None,
                                     hourly_shadow_temp_2_5: Optional[float] = None,
                                     hourly_shadow_blend_50: Optional[float] = None,
                                     hourly_shadow_temp_1_75: Optional[float] = None,
                                     hourly_shadow_temp_3_0: Optional[float] = None,
                                     hourly_shadow_blend_20: Optional[float] = None,
                                     hourly_shadow_blend_30: Optional[float] = None,
                                     hourly_shadow_blend_60: Optional[float] = None,
                                     hourly_post_temp_prob: Optional[float] = None,
                                     available_balance_cents: Optional[int] = None,
                                     order_id: Optional[str] = None,
                                     order_submitted_at: Optional[str] = None,
                                     order_outcome: Optional[str] = None,
                                     side: str = "yes",
                                     no_ask_cents: Optional[int] = None,
                                     yes_bid_cents: Optional[int] = None,
                                     # ── Extended feature instrumentation (Apr 19) ──
                                     # Tier 1: window/spot-path (populated in Phase 2)
                                     minutes_above_strike: Optional[float] = None,
                                     window_max_buf_pct: Optional[float] = None,
                                     window_min_buf_pct: Optional[float] = None,
                                     recent_crossings_5m: Optional[int] = None,
                                     spot_at_window_open: Optional[float] = None,
                                     # Tier 2: spot momentum (populated in Phase 2)
                                     spot_momentum_60s_bps: Optional[float] = None,
                                     spot_momentum_5m_bps: Optional[float] = None,
                                     spot_realized_range_15m_bps: Optional[float] = None,
                                     # Tier 3: cross-asset (populated in Phase 2)
                                     btc_spot_change_30m_bps: Optional[float] = None,
                                     btc_spot_change_5m_bps: Optional[float] = None,
                                     btc_realized_vol_15m: Optional[float] = None,
                                     sol_btc_relative_return_30m_bps: Optional[float] = None,
                                     # Tier 4: time/regime (Phase 1)
                                     hour_of_day_utc: Optional[int] = None,
                                     day_of_week: Optional[int] = None,
                                     is_weekend: Optional[int] = None,
                                     minutes_since_us_open: Optional[int] = None,
                                     is_fomc_day: Optional[int] = None,
                                     is_cpi_day: Optional[int] = None,
                                     # Tier 5: derived (Phase 1)
                                     spot_distance_to_strike_sigma: Optional[float] = None,
                                     prob_breakeven_gap: Optional[float] = None,
                                     kelly_vs_cap_ratio: Optional[float] = None,
                                     calibration_confidence: Optional[float] = None,
                                     # Tier 6: bot state (populated in Phase 2)
                                     active_positions_same_asset: Optional[int] = None,
                                     recent_bot_pnl_30m_cents: Optional[int] = None,
                                     current_drawdown_pct: Optional[float] = None,
                                     recent_ioc_fill_success_rate_1h: Optional[float] = None,
                                     # Feature-engineering Phase 1 (Apr 23): orthogonal axes.
                                     # Auto-filled from scanner caches (self._scan_ms_cache,
                                     # self._scan_cx_gap_cache) when None — no need to thread
                                     # through 57 call sites.
                                     yes_spread_cents: Optional[int] = None,
                                     bid_depth: Optional[int] = None,
                                     spot_coinbase_kraken_gap_bps: Optional[float] = None,
                                     kalshi_flow_imbalance_level: Optional[str] = None,
                                     kalshi_flow_depth_velocity: Optional[float] = None,
                                     kalshi_flow_depth_drain: Optional[int] = None,
                                     # Per-level orderbook snapshot at evaluation time
                                     # (compact JSON via OrderExecutor._extract_book_levels).
                                     # Populated at trade-creation call sites only — not on
                                     # high-volume rejection rows (volume control).
                                     orderbook_levels_json: Optional[str] = None,
                                     # Phase 7 cal_mlp audit columns (R-p7-deploy)
                                     cal_mlp_p_mean: Optional[float] = None,
                                     cal_mlp_p_std: Optional[float] = None,
                                     cal_mlp_final_lo: Optional[float] = None,
                                     cal_mlp_final_hi: Optional[float] = None,
                                     cal_mlp_train_id: Optional[str] = None,
                                     cal_mlp_skipped_reason: Optional[str] = None,
                                     # R-p7-deploy-r8 async-predict: uuid set by
                                     # annotate_evaluation_async_enqueue at scan-tick;
                                     # async worker UPDATEs the row WHERE this matches.
                                     cal_mlp_request_id: Optional[str] = None,
                                     # Shadow coverage expansion Phase B (2026-05-02).
                                     # 18 nullable kwargs for future-phase population
                                     # (D/E/F). Schema lands here so older rows can be
                                     # backfilled and so insert call sites that already
                                     # have a value (e.g. next_blocking_gate at
                                     # rejection-decision time) can stamp it now.
                                     # See kb/decisions/shadow-coverage-expansion-may01.md.
                                     n_open_positions: Optional[int] = None,
                                     recent_n_outcome_streak: Optional[int] = None,
                                     time_since_last_fill_s: Optional[float] = None,
                                     maker_price_cents: Optional[int] = None,
                                     maker_depth_at_post: Optional[int] = None,
                                     maker_would_fill_within_30s: Optional[int] = None,
                                     next_blocking_gate: Optional[str] = None,
                                     final_spot_price: Optional[float] = None,
                                     knockout_time_relative: Optional[float] = None,
                                     max_excursion_from_strike: Optional[float] = None,
                                     time_above_strike_seconds: Optional[float] = None,
                                     time_below_strike_seconds: Optional[float] = None,
                                     btc_spot_at_decision: Optional[float] = None,
                                     eth_spot_at_decision: Optional[float] = None,
                                     sol_spot_at_decision: Optional[float] = None,
                                     xrp_spot_at_decision: Optional[float] = None,
                                     hype_spot_at_decision: Optional[float] = None,
                                     doge_spot_at_decision: Optional[float] = None,
                                     okx_funding_rate_at_decision: Optional[float] = None,
                                     deribit_funding_rate_at_decision: Optional[float] = None,
                                     # Phase G-6 (2026-05-02): provenance flag.
                                     # Live-bot inserts always default 'live_ws'. Existing
                                     # backfilled rows are stamped retroactively by
                                     # scripts/backfill/stamp_data_provenance.py (no caller passes this
                                     # kwarg today — backfill scripts use raw UPDATE SQL with
                                     # COALESCE(data_provenance, '<source>') instead).
                                     # See kb/decisions/v2-train-must-account-for-backfill-skew-may02.md.
                                     data_provenance: str = 'live_ws',
                                     # Phase H-2 (2026-05-03): bot microstate JSON blob.
                                     # Default None — Step 1 ships only the plumbing; Step 2
                                     # wires the snapshot computation at the call site so
                                     # callers actually pass the JSON string.
                                     # See kb/decisions/phase-h2-bot-microstate-fwd-may02.md.
                                     bot_state_snapshot_json: Optional[str] = None):
        """Insert an evaluated opportunity for settlement tracking."""
        # Auto-fill balance from cache so ALL filter stages have a recent value
        if available_balance_cents is not None:
            self._last_balance_cents = available_balance_cents
        elif self._last_balance_cents is not None:
            available_balance_cents = self._last_balance_cents
        # Auto-fill yes_bid from scanner cache if not explicitly provided
        # This lets ALL insert call sites benefit from bid logging without needing
        # to thread ob_data through 50+ call sites.
        if yes_bid_cents is None:
            yes_bid_cents = self._scan_bid_cache.get(ticker)
        # Phase 1 feature auto-fill: microstructure + Kalshi flow (per-ticker).
        _ms = self._scan_ms_cache.get(ticker)
        if _ms:
            if yes_spread_cents is None:
                yes_spread_cents = _ms.get("yes_spread_cents")
            if bid_depth is None:
                bid_depth = _ms.get("bid_depth")
            if kalshi_flow_imbalance_level is None:
                kalshi_flow_imbalance_level = _ms.get("kalshi_flow_imbalance_level")
            if kalshi_flow_depth_velocity is None:
                kalshi_flow_depth_velocity = _ms.get("kalshi_flow_depth_velocity")
            if kalshi_flow_depth_drain is None:
                kalshi_flow_depth_drain = _ms.get("kalshi_flow_depth_drain")
            # Phase F-2 (shadow coverage expansion 2026-05-02): maker
            # counterfactual snapshot. Cache populated in scan tick at
            # _scan_ms_cache write site (bot/_impl.py around line 12085).
            if maker_price_cents is None:
                maker_price_cents = _ms.get("maker_price_cents")
            if maker_depth_at_post is None:
                maker_depth_at_post = _ms.get("maker_depth_at_post")
        # Phase 1 cross-exchange gap (per-asset).
        if spot_coinbase_kraken_gap_bps is None and asset is not None:
            spot_coinbase_kraken_gap_bps = self._scan_cx_gap_cache.get(asset)
        # Per-level orderbook ladder (Apr 25): auto-fill from cache via
        # _get_fresh_ob_ladder (returns None on stale entries — honest).
        if orderbook_levels_json is None:
            orderbook_levels_json = self._get_fresh_ob_ladder(ticker)
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        # ── Auto-compute Tier 4 (time/regime) + Tier 5 (derived) features ──
        # Computed once per row at insert time from the caller's kwargs + 'now'.
        # Avoids threading through 56 call sites. Caller can still override by
        # passing any feature explicitly (non-None value wins).
        if any(v is None for v in (hour_of_day_utc, day_of_week, is_weekend,
                                    minutes_since_us_open, is_fomc_day, is_cpi_day)):
            _t4 = compute_time_regime_features(now)
            if hour_of_day_utc is None: hour_of_day_utc = _t4["hour_of_day_utc"]
            if day_of_week is None: day_of_week = _t4["day_of_week"]
            if is_weekend is None: is_weekend = _t4["is_weekend"]
            if minutes_since_us_open is None: minutes_since_us_open = _t4["minutes_since_us_open"]
            if is_fomc_day is None: is_fomc_day = _t4["is_fomc_day"]
            if is_cpi_day is None: is_cpi_day = _t4["is_cpi_day"]
        if any(v is None for v in (spot_distance_to_strike_sigma, prob_breakeven_gap,
                                    kelly_vs_cap_ratio, calibration_confidence)):
            _n_cal_obs: Optional[int] = None
            try:
                if product_type == "15m" or product_type is None:
                    if _cal_state._CALIBRATION_ENGINE is not None:
                        _n_cal_obs = len(_cal_state._CALIBRATION_ENGINE._observations)
                else:
                    _eng = _cal_state._resolve_cal_engine(product_type, asset, require_enabled=False)
                    if _eng is not None:
                        _n_cal_obs = len(_eng._observations)
            except Exception:
                _n_cal_obs = None
            _t5 = compute_derived_features(
                spot_price=spot_price, threshold=threshold, volatility=volatility,
                seconds_to_close=seconds_to_close, calibrated_prob=calibrated_prob,
                market_price_cents=market_price, kelly_contracts=position_size,
                sol_rescue_cap=SOL_RESCUE_CONTRACT_CAP, n_recent_cal_trades=_n_cal_obs,
            )
            if spot_distance_to_strike_sigma is None: spot_distance_to_strike_sigma = _t5["spot_distance_to_strike_sigma"]
            if prob_breakeven_gap is None: prob_breakeven_gap = _t5["prob_breakeven_gap"]
            if kelly_vs_cap_ratio is None: kelly_vs_cap_ratio = _t5["kelly_vs_cap_ratio"]
            if calibration_confidence is None: calibration_confidence = _t5["calibration_confidence"]

        # ── Auto-compute Tier 1 (window), 2 (momentum), 3 (cross-asset), 6 (bot state) ──
        # Delegates to scanner-provided callback if set. Only fires for 15M rows;
        # the provider returns empty dict otherwise. Safe to call with unset provider.
        if self._extended_feature_provider is not None:
            try:
                _ext = self._extended_feature_provider(
                    ticker, asset, spot_price, threshold, product_type)
            except Exception as _exc:
                logging.debug("extended_feature_provider failed for %s: %s", ticker, _exc)
                _ext = {}
            if _ext:
                if minutes_above_strike is None:
                    minutes_above_strike = _ext.get("minutes_above_strike")
                if window_max_buf_pct is None:
                    window_max_buf_pct = _ext.get("window_max_buf_pct")
                if window_min_buf_pct is None:
                    window_min_buf_pct = _ext.get("window_min_buf_pct")
                if recent_crossings_5m is None:
                    recent_crossings_5m = _ext.get("recent_crossings_5m")
                if spot_at_window_open is None:
                    spot_at_window_open = _ext.get("spot_at_window_open")
                if spot_momentum_60s_bps is None:
                    spot_momentum_60s_bps = _ext.get("spot_momentum_60s_bps")
                if spot_momentum_5m_bps is None:
                    spot_momentum_5m_bps = _ext.get("spot_momentum_5m_bps")
                if spot_realized_range_15m_bps is None:
                    spot_realized_range_15m_bps = _ext.get("spot_realized_range_15m_bps")
                if btc_spot_change_30m_bps is None:
                    btc_spot_change_30m_bps = _ext.get("btc_spot_change_30m_bps")
                if btc_spot_change_5m_bps is None:
                    btc_spot_change_5m_bps = _ext.get("btc_spot_change_5m_bps")
                if btc_realized_vol_15m is None:
                    btc_realized_vol_15m = _ext.get("btc_realized_vol_15m")
                if sol_btc_relative_return_30m_bps is None:
                    sol_btc_relative_return_30m_bps = _ext.get("sol_btc_relative_return_30m_bps")
                if active_positions_same_asset is None:
                    active_positions_same_asset = _ext.get("active_positions_same_asset")
                if recent_bot_pnl_30m_cents is None:
                    recent_bot_pnl_30m_cents = _ext.get("recent_bot_pnl_30m_cents")
                if current_drawdown_pct is None:
                    current_drawdown_pct = _ext.get("current_drawdown_pct")
                if recent_ioc_fill_success_rate_1h is None:
                    recent_ioc_fill_success_rate_1h = _ext.get("recent_ioc_fill_success_rate_1h")
                # Phase E (shadow coverage expansion 2026-05-02): state-at-
                # decision-time fields. Computed once per 60s in
                # _compute_bot_state_features and propagated here uniformly.
                # See kb/decisions/shadow-coverage-expansion-may01.md.
                if n_open_positions is None:
                    n_open_positions = _ext.get("n_open_positions")
                if time_since_last_fill_s is None:
                    time_since_last_fill_s = _ext.get("time_since_last_fill_s")
                if recent_n_outcome_streak is None:
                    recent_n_outcome_streak = _ext.get("recent_n_outcome_streak")
                # Phase F: cross-asset spot snapshot + resolution metadata.
                # Cross-asset (6 post-Bit-2 2026-05-11): absolute spot levels
                # at decision tick. Bit 2 added hype/doge — see consumer
                # block below + bot/scanner/__init__.py:990 producer.
                # Resolution (4 of 5): max_excursion_from_strike +
                # time_above/below_strike_seconds derived from
                # _window_states; final_spot_price uses current spot
                # (each ON-CONFLICT-UPDATE overwrites — last decision-tick
                # value approximates spot-at-settlement). Phase F-3 added
                # decision-tick approximate `knockout_time_relative` from
                # `_window_states.crossings + window_open_ts`. OKX/Deribit
                # funding rates remain DEFERRED — no exchange-specific
                # funding feed in current bot (CoinGlass returns AVG only).
                if btc_spot_at_decision is None:
                    btc_spot_at_decision = _ext.get("btc_spot_at_decision")
                if eth_spot_at_decision is None:
                    eth_spot_at_decision = _ext.get("eth_spot_at_decision")
                if sol_spot_at_decision is None:
                    sol_spot_at_decision = _ext.get("sol_spot_at_decision")
                if xrp_spot_at_decision is None:
                    xrp_spot_at_decision = _ext.get("xrp_spot_at_decision")
                # Bit 2 / T1 cross-asset expansion: producer at
                # bot/scanner/__init__.py:990 is ASSETS-driven and emits
                # 6 keys. Pre-Bit-2 the hype/doge keys were silently
                # dropped on the floor here.
                if hype_spot_at_decision is None:
                    hype_spot_at_decision = _ext.get("hype_spot_at_decision")
                if doge_spot_at_decision is None:
                    doge_spot_at_decision = _ext.get("doge_spot_at_decision")
                if max_excursion_from_strike is None:
                    max_excursion_from_strike = _ext.get("max_excursion_from_strike")
                if time_above_strike_seconds is None:
                    time_above_strike_seconds = _ext.get("time_above_strike_seconds")
                if time_below_strike_seconds is None:
                    time_below_strike_seconds = _ext.get("time_below_strike_seconds")
                # Phase F-3: knockout_time_relative approximation.
                if knockout_time_relative is None:
                    knockout_time_relative = _ext.get("knockout_time_relative")
        # final_spot_price: derive from caller-provided spot_price (current
        # decision-tick spot). Independent of `_extended_feature_provider`
        # since it depends only on the caller's `spot_price` kwarg, not on
        # asset state. Placed OUTSIDE the `if _ext:` block (Phase F adversarial
        # round 2 MEDIUM-1) so the column populates on EVERY row that has a
        # spot_price, including non-15M rows and provider-failure paths.
        # Per-stage semantic:
        #   - Stages re-emitted on every tick (candidate, etc.) converge to
        #     spot-at-final-tick ≈ spot-at-settlement (ON CONFLICT UPDATE
        #     overwrites).
        #   - One-shot stages dedup'd via `_eval_opp_seen` (e.g.
        #     floor_raise_shadow, low_price_shadow, sol_low_entry_high_stc)
        #     freeze at the FIRST-tick spot — this is spot-at-DECISION, not
        #     at settlement.
        # Downstream analysts must consider stage type when interpreting.
        # A future Phase F-2 may add explicit settlement-time backfill via
        # SettlementTracker. For product_type='weather' / 'spx_hourly',
        # `spot_price` is overloaded (temperature in °F / SPX index value
        # respectively); `final_spot_price` inherits that overload — same
        # convention as the existing `spot_price` column. See
        # kb/decisions/shadow-coverage-expansion-may01.md.
        if final_spot_price is None and spot_price is not None:
            final_spot_price = spot_price

        # Phase H-2: compute snapshot DICT before BEGIN IMMEDIATE so the
        # heavy field-extraction work (api_error_counts walk, ws_cache_age_ms
        # walk, open_positions cache read) happens OUTSIDE the writer
        # lock. lock_wait_ms is unknown at this point; it gets patched
        # into the dict after BEGIN succeeds, and json.dumps runs INSIDE
        # the lock (sub-ms). Round-1 wiring review #3 — keeps lock-held
        # time minimal under contention with cal_mlp post-hoc + settlement.
        # Scoped to product_type='15m' per the design doc.
        # Provider contract: returns dict (not JSON string) so the insert
        # site can patch lock_wait_ms in-place.
        _h2_snap_dict: Optional[Dict[str, Any]] = None
        if (bot_state_snapshot_json is None
                and product_type == '15m'
                and self._bot_state_provider is not None):
            try:
                _h2_snap_dict = self._bot_state_provider()
            except Exception:
                logging.warning(
                    "bot_state_provider raised — bot_state_snapshot_json NULL",
                    exc_info=True,
                )
                _h2_snap_dict = None

        # Phase H-2: BEGIN IMMEDIATE timing pattern for lock_wait_ms.
        # Mandated per bot/snapshots/bot_state_snapshot.py "lock_wait_ms semantics —
        # MANDATED PATTERN": measure the wall-clock delay between issuing
        # BEGIN IMMEDIATE and the lock being granted, in milliseconds.
        # Falls through to the implicit-tx path under two conditions:
        #   1. Another thread already has an implicit tx open on the
        #      shared connection (Python-side "cannot start a transaction
        #      within a transaction" error).
        #   2. busy_timeout exceeded (SQLite-side "database is locked").
        # In both cases lock_wait_ms remains None — best-effort under
        # thread contention. v2 training should treat NULL as "unmeasured".
        _lock_wait_ms: Optional[float] = None
        _began_explicitly: bool = False
        # RCA instrumentation (2026-05-09): capture BEGIN IMMEDIATE error
        # message so the failure-path warning log can distinguish
        # "cannot start a transaction within a transaction" (broken-stale-tx
        # cascade) from "database is locked" (busy_timeout-driven contention).
        # 2026-05-09 followup: capture begin_immediate_duration_ms even on
        # failure (sub-millisecond = fast-fail, NOT busy_timeout-driven).
        # 2026-05-09 RCA-resolution: confirmed FAST-fail mechanism. SQLite
        # returns SQLITE_BUSY immediately for INTRA-process lock contention
        # (busy_handler bypassed to avoid deadlock). recent_writes() ring
        # buffer identified `market_obs_snapshotter` as the culprit (its
        # 4-row executemany periodically takes 7-8 SECONDS — likely WAL
        # checkpoint or fsync stall). This retry loop catches the row when
        # the holder finishes between our attempts.
        _be_err_repr: Optional[str] = None
        _be_duration_ms: Optional[float] = None
        _be_retries: int = 0
        _t0_lock = time.perf_counter()
        # Retry-on-busy loop: 3 attempts with 25-75ms jittered backoff.
        # Worst-case latency: ~225ms per call (kept under SCAN_BODY_SLOW
        # 1.5s budget even if multiple inserts contend in same scan tick).
        # Adversarial review (2026-05-09 R1): reduced from 5×50-200ms
        # because cumulative MainThread blocking risked scan-loop overrun
        # — the same failure mode as cal-mlp-torch-thread-contention-apr29.
        # Only retry on transient "is locked"/"is busy"; "cannot start a
        # transaction within a transaction" is Python-side stale-tx and
        # not transient — sleep won't help.
        for _attempt in range(3):
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                _lock_wait_ms = (time.perf_counter() - _t0_lock) * 1000.0
                _be_duration_ms = _lock_wait_ms
                _began_explicitly = True
                _be_retries = _attempt
                break
            except sqlite3.OperationalError as _be_err:
                _be_duration_ms = (time.perf_counter() - _t0_lock) * 1000.0
                _be_err_repr = f"{type(_be_err).__name__}: {_be_err}"
                _be_retries = _attempt + 1
                _err_msg = str(_be_err).lower()
                _is_transient = ("locked" in _err_msg) or ("busy" in _err_msg)
                if not _is_transient:
                    # Stale-tx case ("cannot start a transaction within a
                    # transaction") — retry won't help. Fall through.
                    break
                if _attempt < 2:
                    # Jittered backoff: 25-75ms uniform random.
                    time.sleep(0.025 + random.random() * 0.050)

        # Phase H-2: patch lock_wait_ms into the pre-computed snapshot
        # and serialize. Done INSIDE the lock but it's just a dict write
        # + json.dumps — sub-millisecond.
        if _h2_snap_dict is not None:
            try:
                _h2_snap_dict["lock_wait_ms"] = (
                    round(_lock_wait_ms, 3)
                    if _lock_wait_ms is not None else None
                )
                bot_state_snapshot_json = json.dumps(_h2_snap_dict, allow_nan=False)
            except Exception:
                logging.warning(
                    "bot_state snapshot serialization failed; column NULL",
                    exc_info=True,
                )
                bot_state_snapshot_json = None

        try:
          with tracked_write("state_manager", "insert_evaluated_opportunity"):
            self.conn.execute("""
                INSERT INTO evaluated_opportunities
                    (ticker, event_ticker, asset, filter_stage, rejection_reason,
                     evaluation_time, spot_price, threshold, volatility,
                     market_price, seconds_to_close, calibrated_prob,
                     edge, ofa_adjustment, status,
                     strategy, position_size, kelly_f, z_score,
                     vol_regime, calibrated_prob_raw,
                     breakeven_wr, expected_value, drawdown_scaler,
                     ask_depth, best_ask_source, ofa_confidence,
                     raw_prob, calibration_method, old_system_prob,
                     fee_adjusted_edge,
                     egarch_sigma, egarch_blend_sigma, egarch_blend_weight, mz_r_squared,
                     shadow_tv_blend_rv, mz_shadow_sigmoid_w, mz_baseline_qlike, mz_qlike,
                     counterfactual,
                     shadow_cal_prob, shadow_cal_fee_edge, shadow_cal_temperature,
                     product_type,
                     oft_prob_adjustment, oft_imbalance_ratio, oft_n_snapshots,
                     wx_ensemble_mean, wx_ensemble_std, wx_bias_correction, wx_n_members,
                     wx_market_type, wx_actual_high_temp, wx_no_side_edge,
                     wx_hrrr_temp, wx_corrected_mean,
                     hourly_pre_temp_prob, hourly_applied_temp_t,
                     hourly_shadow_temp_2_0, hourly_shadow_temp_1_0, hourly_shadow_temp_2_5,
                     hourly_shadow_blend_50,
                     hourly_shadow_temp_1_75, hourly_shadow_temp_3_0,
                     hourly_shadow_blend_20, hourly_shadow_blend_30, hourly_shadow_blend_60,
                     hourly_post_temp_prob,
                     available_balance_cents,
                     order_id, order_submitted_at, order_outcome,
                     side, no_ask_cents, yes_bid_cents,
                     minutes_above_strike, window_max_buf_pct, window_min_buf_pct,
                     recent_crossings_5m, spot_at_window_open,
                     spot_momentum_60s_bps, spot_momentum_5m_bps, spot_realized_range_15m_bps,
                     btc_spot_change_30m_bps, btc_spot_change_5m_bps, btc_realized_vol_15m,
                     sol_btc_relative_return_30m_bps,
                     hour_of_day_utc, day_of_week, is_weekend, minutes_since_us_open,
                     is_fomc_day, is_cpi_day,
                     spot_distance_to_strike_sigma, prob_breakeven_gap,
                     kelly_vs_cap_ratio, calibration_confidence,
                     active_positions_same_asset, recent_bot_pnl_30m_cents,
                     current_drawdown_pct, recent_ioc_fill_success_rate_1h,
                     yes_spread_cents, bid_depth, spot_coinbase_kraken_gap_bps,
                     kalshi_flow_imbalance_level, kalshi_flow_depth_velocity,
                     kalshi_flow_depth_drain,
                     orderbook_levels_json,
                     cal_mlp_p_mean, cal_mlp_p_std, cal_mlp_final_lo,
                     cal_mlp_final_hi, cal_mlp_train_id, cal_mlp_skipped_reason,
                     cal_mlp_request_id,
                     n_open_positions, recent_n_outcome_streak,
                     time_since_last_fill_s,
                     maker_price_cents, maker_depth_at_post,
                     maker_would_fill_within_30s,
                     next_blocking_gate,
                     final_spot_price, knockout_time_relative,
                     max_excursion_from_strike,
                     time_above_strike_seconds, time_below_strike_seconds,
                     btc_spot_at_decision, eth_spot_at_decision,
                     sol_spot_at_decision, xrp_spot_at_decision,
                     hype_spot_at_decision, doge_spot_at_decision,
                     okx_funding_rate_at_decision, deribit_funding_rate_at_decision,
                     data_provenance, bot_state_snapshot_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(ticker, filter_stage, side) DO UPDATE SET
                    event_ticker=excluded.event_ticker, asset=excluded.asset,
                    rejection_reason=excluded.rejection_reason,
                    evaluation_time=excluded.evaluation_time,
                    spot_price=excluded.spot_price, threshold=excluded.threshold,
                    volatility=excluded.volatility, market_price=excluded.market_price,
                    seconds_to_close=excluded.seconds_to_close,
                    calibrated_prob=excluded.calibrated_prob,
                    edge=excluded.edge, ofa_adjustment=excluded.ofa_adjustment,
                    strategy=excluded.strategy, position_size=excluded.position_size,
                    kelly_f=excluded.kelly_f, z_score=excluded.z_score,
                    vol_regime=excluded.vol_regime,
                    calibrated_prob_raw=excluded.calibrated_prob_raw,
                    breakeven_wr=excluded.breakeven_wr,
                    expected_value=excluded.expected_value,
                    drawdown_scaler=excluded.drawdown_scaler,
                    ask_depth=excluded.ask_depth,
                    best_ask_source=excluded.best_ask_source,
                    ofa_confidence=excluded.ofa_confidence,
                    raw_prob=excluded.raw_prob,
                    calibration_method=excluded.calibration_method,
                    old_system_prob=excluded.old_system_prob,
                    fee_adjusted_edge=excluded.fee_adjusted_edge,
                    egarch_sigma=excluded.egarch_sigma,
                    egarch_blend_sigma=excluded.egarch_blend_sigma,
                    egarch_blend_weight=excluded.egarch_blend_weight,
                    mz_r_squared=excluded.mz_r_squared,
                    shadow_tv_blend_rv=excluded.shadow_tv_blend_rv,
                    mz_shadow_sigmoid_w=excluded.mz_shadow_sigmoid_w,
                    mz_baseline_qlike=excluded.mz_baseline_qlike,
                    mz_qlike=excluded.mz_qlike,
                    counterfactual=excluded.counterfactual,
                    shadow_cal_prob=excluded.shadow_cal_prob,
                    shadow_cal_fee_edge=excluded.shadow_cal_fee_edge,
                    shadow_cal_temperature=excluded.shadow_cal_temperature,
                    product_type=excluded.product_type,
                    oft_prob_adjustment=excluded.oft_prob_adjustment,
                    oft_imbalance_ratio=excluded.oft_imbalance_ratio,
                    oft_n_snapshots=excluded.oft_n_snapshots,
                    wx_ensemble_mean=excluded.wx_ensemble_mean,
                    wx_ensemble_std=excluded.wx_ensemble_std,
                    wx_bias_correction=excluded.wx_bias_correction,
                    wx_n_members=excluded.wx_n_members,
                    wx_market_type=excluded.wx_market_type,
                    wx_actual_high_temp=excluded.wx_actual_high_temp,
                    wx_no_side_edge=excluded.wx_no_side_edge,
                    wx_hrrr_temp=excluded.wx_hrrr_temp,
                    wx_corrected_mean=excluded.wx_corrected_mean,
                    hourly_pre_temp_prob=excluded.hourly_pre_temp_prob,
                    hourly_applied_temp_t=excluded.hourly_applied_temp_t,
                    hourly_shadow_temp_2_0=excluded.hourly_shadow_temp_2_0,
                    hourly_shadow_temp_1_0=excluded.hourly_shadow_temp_1_0,
                    hourly_shadow_temp_2_5=excluded.hourly_shadow_temp_2_5,
                    hourly_shadow_blend_50=excluded.hourly_shadow_blend_50,
                    hourly_shadow_temp_1_75=excluded.hourly_shadow_temp_1_75,
                    hourly_shadow_temp_3_0=excluded.hourly_shadow_temp_3_0,
                    hourly_shadow_blend_20=excluded.hourly_shadow_blend_20,
                    hourly_shadow_blend_30=excluded.hourly_shadow_blend_30,
                    hourly_shadow_blend_60=excluded.hourly_shadow_blend_60,
                    hourly_post_temp_prob=excluded.hourly_post_temp_prob,
                    available_balance_cents=excluded.available_balance_cents,
                    no_ask_cents=excluded.no_ask_cents,
                    yes_bid_cents=excluded.yes_bid_cents,
                    minutes_above_strike=excluded.minutes_above_strike,
                    window_max_buf_pct=excluded.window_max_buf_pct,
                    window_min_buf_pct=excluded.window_min_buf_pct,
                    recent_crossings_5m=excluded.recent_crossings_5m,
                    spot_at_window_open=excluded.spot_at_window_open,
                    spot_momentum_60s_bps=excluded.spot_momentum_60s_bps,
                    spot_momentum_5m_bps=excluded.spot_momentum_5m_bps,
                    spot_realized_range_15m_bps=excluded.spot_realized_range_15m_bps,
                    btc_spot_change_30m_bps=excluded.btc_spot_change_30m_bps,
                    btc_spot_change_5m_bps=excluded.btc_spot_change_5m_bps,
                    btc_realized_vol_15m=excluded.btc_realized_vol_15m,
                    sol_btc_relative_return_30m_bps=excluded.sol_btc_relative_return_30m_bps,
                    hour_of_day_utc=excluded.hour_of_day_utc,
                    day_of_week=excluded.day_of_week,
                    is_weekend=excluded.is_weekend,
                    minutes_since_us_open=excluded.minutes_since_us_open,
                    is_fomc_day=excluded.is_fomc_day,
                    is_cpi_day=excluded.is_cpi_day,
                    spot_distance_to_strike_sigma=excluded.spot_distance_to_strike_sigma,
                    prob_breakeven_gap=excluded.prob_breakeven_gap,
                    kelly_vs_cap_ratio=excluded.kelly_vs_cap_ratio,
                    calibration_confidence=excluded.calibration_confidence,
                    active_positions_same_asset=excluded.active_positions_same_asset,
                    recent_bot_pnl_30m_cents=excluded.recent_bot_pnl_30m_cents,
                    current_drawdown_pct=excluded.current_drawdown_pct,
                    recent_ioc_fill_success_rate_1h=excluded.recent_ioc_fill_success_rate_1h,
                    yes_spread_cents=excluded.yes_spread_cents,
                    bid_depth=excluded.bid_depth,
                    spot_coinbase_kraken_gap_bps=excluded.spot_coinbase_kraken_gap_bps,
                    kalshi_flow_imbalance_level=excluded.kalshi_flow_imbalance_level,
                    kalshi_flow_depth_velocity=excluded.kalshi_flow_depth_velocity,
                    kalshi_flow_depth_drain=excluded.kalshi_flow_depth_drain,
                    orderbook_levels_json=excluded.orderbook_levels_json,
                    cal_mlp_p_mean=excluded.cal_mlp_p_mean,
                    cal_mlp_p_std=excluded.cal_mlp_p_std,
                    cal_mlp_final_lo=excluded.cal_mlp_final_lo,
                    cal_mlp_final_hi=excluded.cal_mlp_final_hi,
                    cal_mlp_train_id=excluded.cal_mlp_train_id,
                    cal_mlp_skipped_reason=excluded.cal_mlp_skipped_reason,
                    cal_mlp_request_id=excluded.cal_mlp_request_id,
                    n_open_positions=excluded.n_open_positions,
                    recent_n_outcome_streak=excluded.recent_n_outcome_streak,
                    time_since_last_fill_s=excluded.time_since_last_fill_s,
                    maker_price_cents=excluded.maker_price_cents,
                    maker_depth_at_post=excluded.maker_depth_at_post,
                    maker_would_fill_within_30s=excluded.maker_would_fill_within_30s,
                    next_blocking_gate=excluded.next_blocking_gate,
                    final_spot_price=excluded.final_spot_price,
                    knockout_time_relative=excluded.knockout_time_relative,
                    max_excursion_from_strike=excluded.max_excursion_from_strike,
                    time_above_strike_seconds=excluded.time_above_strike_seconds,
                    time_below_strike_seconds=excluded.time_below_strike_seconds,
                    btc_spot_at_decision=excluded.btc_spot_at_decision,
                    eth_spot_at_decision=excluded.eth_spot_at_decision,
                    sol_spot_at_decision=excluded.sol_spot_at_decision,
                    xrp_spot_at_decision=excluded.xrp_spot_at_decision,
                    hype_spot_at_decision=excluded.hype_spot_at_decision,
                    doge_spot_at_decision=excluded.doge_spot_at_decision,
                    okx_funding_rate_at_decision=excluded.okx_funding_rate_at_decision,
                    deribit_funding_rate_at_decision=excluded.deribit_funding_rate_at_decision,
                    -- Phase G-6: COALESCE so an existing non-default value
                    -- (e.g. a backfill-source marker stamped by H-4 scripts
                    -- or by stamp_data_provenance.py) survives an UPSERT
                    -- whose caller relies on the 'live_ws' default. Round-2
                    -- adversarial review #11. Without COALESCE every UPSERT
                    -- of an existing row resets data_provenance to the
                    -- default kwarg, silently re-labeling backfilled rows
                    -- as live.
                    data_provenance=COALESCE(evaluated_opportunities.data_provenance, excluded.data_provenance),
                    -- Phase H-2: bot microstate snapshot. Overwrite on UPSERT
                    -- (no COALESCE) — matches the dynamic-column pattern
                    -- (spot_price, seconds_to_close, market_price, etc.)
                    -- Re-emitted stages must reflect latest-tick state;
                    -- COALESCEing here would freeze the snapshot at
                    -- first-tick while every other column tracks last-tick,
                    -- creating within-row temporal inconsistency for v2
                    -- training joins.
                    --
                    -- Step-2 design eliminates the "split-caller" risk by
                    -- having insert_evaluated_opportunity AUTO-POPULATE
                    -- bot_state_snapshot_json from a provider callback
                    -- (see StateManager.set_bot_state_provider). Callers
                    -- never need to pass the kwarg. The only paths that
                    -- write NULL to this column are: (a) provider import
                    -- failed at MainLoop init (logged once at startup),
                    -- (b) BEGIN IMMEDIATE raised + provider raised, or
                    -- (c) product_type != '15m' (by design — non-15M
                    -- product_types leave the column NULL).
                    bot_state_snapshot_json=excluded.bot_state_snapshot_json
            """, (ticker, event_ticker, asset, filter_stage, rejection_reason,
                  now, spot_price, threshold, volatility, market_price,
                  seconds_to_close, calibrated_prob, edge, ofa_adjustment,
                  "pending",
                  strategy, position_size, kelly_f, z_score,
                  vol_regime, calibrated_prob_raw,
                  breakeven_wr, expected_value, drawdown_scaler,
                  ask_depth, best_ask_source, ofa_confidence,
                  raw_prob, calibration_method, old_system_prob,
                  fee_adjusted_edge,
                  egarch_sigma, egarch_blend_sigma, egarch_blend_weight, mz_r_squared,
                  shadow_tv_blend_rv, mz_shadow_sigmoid_w, mz_baseline_qlike, mz_qlike,
                  counterfactual,
                  shadow_cal_prob, shadow_cal_fee_edge, shadow_cal_temperature,
                  product_type,
                  oft_prob_adjustment, oft_imbalance_ratio, oft_n_snapshots,
                  wx_ensemble_mean, wx_ensemble_std, wx_bias_correction, wx_n_members,
                  wx_market_type, wx_actual_high_temp, wx_no_side_edge,
                  wx_hrrr_temp, wx_corrected_mean,
                  hourly_pre_temp_prob, hourly_applied_temp_t,
                  hourly_shadow_temp_2_0, hourly_shadow_temp_1_0, hourly_shadow_temp_2_5,
                  hourly_shadow_blend_50,
                  hourly_shadow_temp_1_75, hourly_shadow_temp_3_0,
                  hourly_shadow_blend_20, hourly_shadow_blend_30, hourly_shadow_blend_60,
                  hourly_post_temp_prob,
                  available_balance_cents,
                  order_id, order_submitted_at, order_outcome,
                  side, no_ask_cents, yes_bid_cents,
                  minutes_above_strike, window_max_buf_pct, window_min_buf_pct,
                  recent_crossings_5m, spot_at_window_open,
                  spot_momentum_60s_bps, spot_momentum_5m_bps, spot_realized_range_15m_bps,
                  btc_spot_change_30m_bps, btc_spot_change_5m_bps, btc_realized_vol_15m,
                  sol_btc_relative_return_30m_bps,
                  hour_of_day_utc, day_of_week, is_weekend, minutes_since_us_open,
                  is_fomc_day, is_cpi_day,
                  spot_distance_to_strike_sigma, prob_breakeven_gap,
                  kelly_vs_cap_ratio, calibration_confidence,
                  active_positions_same_asset, recent_bot_pnl_30m_cents,
                  current_drawdown_pct, recent_ioc_fill_success_rate_1h,
                  yes_spread_cents, bid_depth, spot_coinbase_kraken_gap_bps,
                  kalshi_flow_imbalance_level, kalshi_flow_depth_velocity,
                  kalshi_flow_depth_drain,
                  orderbook_levels_json,
                  cal_mlp_p_mean, cal_mlp_p_std, cal_mlp_final_lo,
                  cal_mlp_final_hi, cal_mlp_train_id, cal_mlp_skipped_reason,
                  cal_mlp_request_id,
                  n_open_positions, recent_n_outcome_streak,
                  time_since_last_fill_s,
                  maker_price_cents, maker_depth_at_post,
                  maker_would_fill_within_30s,
                  next_blocking_gate,
                  final_spot_price, knockout_time_relative,
                  max_excursion_from_strike,
                  time_above_strike_seconds, time_below_strike_seconds,
                  btc_spot_at_decision, eth_spot_at_decision,
                  sol_spot_at_decision, xrp_spot_at_decision,
                  hype_spot_at_decision, doge_spot_at_decision,
                  okx_funding_rate_at_decision, deribit_funding_rate_at_decision,
                  data_provenance, bot_state_snapshot_json))
            # Phase H-2: explicit COMMIT only if we BEGAN IMMEDIATE explicitly.
            # Otherwise fall back to the implicit-tx commit() that paired
            # with the implicit BEGIN that fired on the INSERT above.
            if _began_explicitly:
                self.conn.execute("COMMIT")
            else:
                self.conn.commit()
        except Exception as e:
            try:
                self.conn.rollback()
            except Exception:
                pass
            # RCA instrumentation (2026-05-09): structured failure context.
            # See `_be_err_repr` capture above + tests/integration/test_db_locked_instrumentation.py.
            # 2026-05-08 follow-up: includes active_writers=... snapshot from
            # bot.db_writer_registry so the operator can see which OTHER
            # connection was holding the writer lock when this insert failed.
            _diag_thread = threading.current_thread().name
            _diag_in_tx = getattr(self.conn, "in_transaction", "?")
            _diag_begin = "OK" if _began_explicitly else _be_err_repr
            try:
                _diag_active = [
                    f"{tok.split('#', 1)[0]}/{kind}/{th}"
                    f"@{(time.time() - started) * 1000:.0f}ms"
                    for (tok, started, kind, th) in snapshot_active()
                ]
            except Exception:
                _diag_active = ["<snapshot_failed>"]
            try:
                # FAST-fail RCA (2026-05-09): snapshot_active() shows [] when
                # BEGIN IMMEDIATE returns SQLITE_BUSY in <1ms (intra-process
                # holder released JUST before). recent_writes(2.0) captures
                # the lock-holder via the ring buffer of last-finished writes.
                _now = time.time()
                _diag_recent = [
                    f"{name}/{kind}/{th}"
                    f"@{(_now - finished_ts) * 1000:.0f}ms_ago/{dur:.1f}ms"
                    for (name, kind, dur, finished_ts, th) in recent_writes(2.0)
                ]
            except Exception:
                _diag_recent = ["<recent_failed>"]
            _diag_be_dur = (
                f"{_be_duration_ms:.1f}" if _be_duration_ms is not None else "?"
            )
            logging.warning(
                f"insert_evaluated_opportunity failed: {e} "
                f"begin_immediate={_diag_begin!r} "
                f"begin_immediate_duration_ms={_diag_be_dur} "
                f"begin_immediate_retries={_be_retries} "
                f"thread={_diag_thread!r} "
                f"in_tx={_diag_in_tx!s} "
                f"active_writers={_diag_active!r} "
                f"recent_writes={_diag_recent!r}",
                exc_info=True,
            )

    def update_evaluated_opportunity_order(self, ticker: str,
                                            order_id: Optional[str] = None,
                                            order_submitted_at: Optional[str] = None,
                                            order_outcome: Optional[str] = None,
                                            taker_ask_at_submit: Optional[int] = None):
        """Update order tracking fields on the candidate row for a ticker."""
        try:
            parts = []
            vals = []
            if order_id is not None:
                parts.append("order_id=?")
                vals.append(order_id)
            if order_submitted_at is not None:
                parts.append("order_submitted_at=?")
                vals.append(order_submitted_at)
            if order_outcome is not None:
                parts.append("order_outcome=?")
                vals.append(order_outcome)
            if taker_ask_at_submit is not None:
                parts.append("taker_ask_at_submit=?")
                vals.append(taker_ask_at_submit)
            if not parts:
                return
            vals.append(ticker)
            vals.append("candidate")
            self.conn.execute(
                f"UPDATE evaluated_opportunities SET {', '.join(parts)} "
                f"WHERE ticker=? AND filter_stage=?",
                tuple(vals)
            )
            self.conn.commit()
        except Exception as e:
            logging.warning(f"update_evaluated_opportunity_order failed: {e}", exc_info=True)

    def insert_tm_sweep_shadow_row(self, *, ticker: str, event_ticker: str,
                                    asset: str, entry_time: str,
                                    entry_price_cents: int,
                                    requested_count: int, filled_count: int,
                                    unfilled_count: int,
                                    depth_at_entry_pre_fill: Optional[int] = None,
                                    depth_96c_pre: Optional[int] = None,
                                    depth_97c_pre: Optional[int] = None,
                                    depth_98c_pre: Optional[int] = None,
                                    depth_99c_pre: Optional[int] = None,
                                    depth_96c_post: Optional[int] = None,
                                    depth_97c_post: Optional[int] = None,
                                    depth_98c_post: Optional[int] = None,
                                    depth_99c_post: Optional[int] = None,
                                    seconds_to_close: Optional[float] = None,
                                    calibrated_prob: Optional[float] = None,
                                    buf_pct: Optional[float] = None,
                                    best_ask_source: Optional[str] = None,
                                    direct_bump_applied: Optional[int] = None) -> None:
        """Insert one tm_sweep_shadow row capturing a TM execution snapshot.
        Caller is responsible for ensuring this is only called from TM paths
        (DC/LPNE/maker do NOT capture). Idempotent at the row level only by
        (ticker, entry_time) — duplicates would create separate rows.

        direct_bump_applied semantics (R3 A1): 1 when the direct-bump POLICY
        was active at IOC submit time (TM_SWEEP_LIVE_ENABLED + TM strategy +
        not retry + not no_side + price < MAX). It is NOT a confirmation
        that an IOC fill occurred — rows with direct_bump_applied=1 AND
        filled_count=0 mean the IOC was submitted at limit=MAX_ENTRY_PRICE
        but no liquidity at-or-below filled (or place_order returned None).
        cf_pnl interpretation differs by this column: bumped rows entered at
        IOC limit=MAX_ENTRY_PRICE, NOT the scan-time price stored in
        entry_price_cents. Compute effective entry as
        `CASE WHEN direct_bump_applied=1 THEN 99 ELSE entry_price_cents END`.
        For realized cost basis on filled bumped rows, join settled_trades
        — the limit was 99 but actual fills sweep 96/97/98."""
        try:
            self.conn.execute(
                "INSERT INTO tm_sweep_shadow ("
                "ticker, event_ticker, asset, entry_time, entry_price_cents, "
                "requested_count, filled_count, unfilled_count, "
                "depth_at_entry_pre_fill, "
                "depth_96c_pre, depth_97c_pre, depth_98c_pre, depth_99c_pre, "
                "depth_96c_post, depth_97c_post, depth_98c_post, depth_99c_post, "
                "seconds_to_close, calibrated_prob, buf_pct, best_ask_source, "
                "direct_bump_applied"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ticker, event_ticker, asset, entry_time, entry_price_cents,
                 requested_count, filled_count, unfilled_count,
                 depth_at_entry_pre_fill,
                 depth_96c_pre, depth_97c_pre, depth_98c_pre, depth_99c_pre,
                 depth_96c_post, depth_97c_post, depth_98c_post, depth_99c_post,
                 seconds_to_close, calibrated_prob, buf_pct, best_ask_source,
                 direct_bump_applied))
            self.conn.commit()
        except Exception:
            logging.warning("tm_sweep_shadow insert failed for %s", ticker, exc_info=True)

    def update_tm_sweep_shadow_on_settlement(self, ticker: str, market_result: str) -> None:
        """For all open tm_sweep_shadow rows on `ticker`, compute counterfactual
        sweep PnL via tm_sweep_counterfactual_pnl() and mark as settled.

        Idempotent: only acts on rows with status='open'. Subsequent calls on
        the same ticker are no-ops. Unrecognized market_result strings (e.g.
        'void') leave rows in 'open' state."""
        if market_result not in ("yes", "all_yes", "no", "all_no"):
            return
        try:
            rows = self.conn.execute(
                "SELECT id, entry_price_cents, unfilled_count, "
                "depth_96c_post, depth_97c_post, depth_98c_post, depth_99c_post "
                "FROM tm_sweep_shadow WHERE ticker=? AND status='open'",
                (ticker,)).fetchall()
            if not rows:
                return
            now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for r in rows:
                depths = {
                    96: r["depth_96c_post"] or 0,
                    97: r["depth_97c_post"] or 0,
                    98: r["depth_98c_post"] or 0,
                    99: r["depth_99c_post"] or 0,
                }
                cf_pnl, legs = tm_sweep_counterfactual_pnl(
                    unfilled=r["unfilled_count"],
                    entry_tier=r["entry_price_cents"],
                    depths=depths,
                    market_result=market_result)
                # Production realism: Kalshi IOCs cannot skip 97c. Compute
                # cf_pnl_cents_with_97 with sweep_tiers=(97,98,99) so we have
                # an apples-to-apples number for "what would a real sweep
                # actually have netted?" alongside the 97-skipped academic version.
                cf_pnl_w97, _legs_w97 = tm_sweep_counterfactual_pnl(
                    unfilled=r["unfilled_count"],
                    entry_tier=r["entry_price_cents"],
                    depths=depths,
                    market_result=market_result,
                    sweep_tiers=(97, 98, 99))
                # Belt-and-suspenders int cast on cf_pnl values. Both columns
                # (cf_pnl_cents, cf_pnl_cents_with_97) are INTEGER; the cf
                # function returns int today via calculate_taker_fee→math.ceil
                # but cast defensively in case that contract changes.
                self.conn.execute(
                    "UPDATE tm_sweep_shadow SET status='settled', "
                    "market_result=?, cf_pnl_cents=?, cf_breakdown_json=?, "
                    "cf_pnl_cents_with_97=?, settled_at=? WHERE id=?",
                    (market_result, int(cf_pnl), json.dumps(legs, separators=(",", ":")),
                     int(cf_pnl_w97), now, r["id"]))
            self.conn.commit()
        except Exception:
            logging.warning("tm_sweep_shadow settle failed for %s", ticker, exc_info=True)

    def backfill_tm_sweep_with_97(self) -> int:
        """Backfill cf_pnl_cents_with_97 for settled rows that have NULL in
        that column (legacy rows from before the with-97 instrumentation).
        Idempotent: skips rows where cf_pnl_cents_with_97 IS NOT NULL.
        Open rows are skipped (no market_result yet).

        Adversary A3: skips rows whose market_result is not in the
        recognized set ('yes','all_yes','no','all_no'). Writing 0 for
        a 'void'/NULL row would be confidently wrong shadow data.

        Returns: count of rows updated."""
        _OK_RESULTS = ("yes", "all_yes", "no", "all_no")
        try:
            rows = self.conn.execute(
                "SELECT id, entry_price_cents, unfilled_count, market_result, "
                "depth_96c_post, depth_97c_post, depth_98c_post, depth_99c_post "
                "FROM tm_sweep_shadow "
                "WHERE status='settled' AND cf_pnl_cents_with_97 IS NULL"
            ).fetchall()
            if not rows:
                return 0
            updated = 0
            skipped = 0
            for r in rows:
                if r["market_result"] not in _OK_RESULTS:
                    skipped += 1
                    continue
                depths = {
                    96: r["depth_96c_post"] or 0,
                    97: r["depth_97c_post"] or 0,
                    98: r["depth_98c_post"] or 0,
                    99: r["depth_99c_post"] or 0,
                }
                cf_pnl_w97, _legs = tm_sweep_counterfactual_pnl(
                    unfilled=r["unfilled_count"],
                    entry_tier=r["entry_price_cents"],
                    depths=depths,
                    market_result=r["market_result"],
                    sweep_tiers=(97, 98, 99))
                self.conn.execute(
                    "UPDATE tm_sweep_shadow SET cf_pnl_cents_with_97=? WHERE id=?",
                    (int(cf_pnl_w97), r["id"]))
                updated += 1
            self.conn.commit()
            if updated or skipped:
                logging.info("tm_sweep_shadow backfill: populated %d row(s), "
                             "skipped %d row(s) with non-whitelisted market_result",
                             updated, skipped)
            return updated
        except Exception:
            logging.warning("tm_sweep_shadow backfill failed", exc_info=True)
            return 0

    def get_unsettled_evaluated_opportunities(self) -> List[Dict]:
        """Return evaluated opportunities with status='pending' and a market_price.
        Excludes synthetic sports tickers (SPORTS-*) that don't resolve via
        get_market().  Real Kalshi tickers (KXNBAGAME-*, etc.) are allowed."""
        rows = self.conn.execute(
            "SELECT * FROM evaluated_opportunities WHERE status='pending'"
            " AND market_price IS NOT NULL"
            " AND ticker NOT LIKE 'SPORTS-%'"
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_evaluated_opportunity_settled(self, opp_id: int,
                                             market_result: Optional[str] = None,
                                             counterfactual_pnl: Optional[int] = None,
                                             commit: bool = True):
        """Set status='settled' for an evaluated opportunity by id.
        Set commit=False to batch multiple updates in a single transaction."""
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.conn.execute(
            "UPDATE evaluated_opportunities SET status='settled', "
            "market_result=?, counterfactual_pnl=?, settled_time=? WHERE id=?",
            (market_result, counterfactual_pnl, now, opp_id)
        )
        if commit:
            self.conn.commit()

    # ── SOL Path C Shadow ──────────────────────────────────────────────

    def insert_sol_pathc_shadow(self, ticker, evaluation_time, live_ask,
                                live_depth, live_edge, live_stc,
                                live_contracts, live_entry_price, live_cal_prob,
                                pathc_maker_price, pathc_maker_offset,
                                pathc_depth_at_maker, position_size):
        """Insert initial SOL Path C shadow row at taker submission time."""
        try:
            self.conn.execute("""
                INSERT OR REPLACE INTO sol_pathc_shadow
                    (ticker, evaluation_time, live_ask, live_depth,
                     live_edge, live_stc, live_contracts, live_entry_price,
                     live_cal_prob, pathc_maker_price, pathc_maker_offset,
                     pathc_depth_at_maker, position_size, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (ticker, evaluation_time, live_ask, live_depth,
                  live_edge, live_stc, live_contracts, live_entry_price,
                  live_cal_prob, pathc_maker_price, pathc_maker_offset,
                  pathc_depth_at_maker, position_size, "pending"))
            self.conn.commit()
        except Exception as e:
            logging.warning(f"insert_sol_pathc_shadow failed: {e}", exc_info=True)

    def update_sol_pathc_observation(self, ticker, obs_time, obs_elapsed,
                                     obs_best_ask, obs_depth,
                                     obs_maker_would_fill, obs_maker_price_touched,
                                     pathc_esc_ask, pathc_esc_depth, pathc_esc_edge):
        """Update deferred observation columns after escalation wait."""
        try:
            self.conn.execute("""
                UPDATE sol_pathc_shadow SET
                    obs_time=?, obs_elapsed_seconds=?, obs_best_ask=?,
                    obs_depth=?, obs_maker_would_fill=?,
                    obs_maker_price_touched=?,
                    pathc_esc_ask=?, pathc_esc_depth=?, pathc_esc_edge=?
                WHERE ticker=?
            """, (obs_time, obs_elapsed, obs_best_ask, obs_depth,
                  obs_maker_would_fill, obs_maker_price_touched,
                  pathc_esc_ask, pathc_esc_depth, pathc_esc_edge, ticker))
            self.conn.commit()
        except Exception as e:
            logging.warning(f"update_sol_pathc_observation failed: {e}", exc_info=True)

    def update_sol_pathc_touch(self, ticker):
        """Set obs_maker_price_touched=1 when ask drops to/below maker price."""
        try:
            self.conn.execute(
                "UPDATE sol_pathc_shadow SET obs_maker_price_touched=1 WHERE ticker=?",
                (ticker,))
            self.conn.commit()
        except Exception as e:
            logging.warning(f"update_sol_pathc_touch failed: {e}", exc_info=True)

    def settle_sol_pathc_shadow(self, ticker, market_result, live_pnl,
                                pathc_maker_pnl, pathc_maker_contracts,
                                pathc_esc_pnl, pathc_esc_contracts,
                                pathc_best_pnl):
        """Settle a SOL Path C shadow row with counterfactual PnL."""
        try:
            now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            self.conn.execute("""
                UPDATE sol_pathc_shadow SET
                    status='settled', market_result=?, settled_time=?,
                    live_pnl_cents=?, pathc_maker_pnl_cents=?,
                    pathc_maker_contracts=?,
                    pathc_esc_pnl_cents=?, pathc_esc_contracts=?,
                    pathc_best_pnl_cents=?
                WHERE ticker=?
            """, (market_result, now, live_pnl, pathc_maker_pnl,
                  pathc_maker_contracts, pathc_esc_pnl, pathc_esc_contracts,
                  pathc_best_pnl, ticker))
            self.conn.commit()
        except Exception as e:
            logging.warning(f"settle_sol_pathc_shadow failed: {e}", exc_info=True)

    def get_pending_sol_pathc_shadows(self):
        """Get all pending sol_pathc_shadow rows for settlement."""
        try:
            rows = self.conn.execute(
                "SELECT * FROM sol_pathc_shadow WHERE status='pending'"
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    # ── Bot Order Lifecycle ─────────────────────────────────────────────

    def insert_bot_order(self, client_order_id: str, ticker: str,
                         event_ticker: str, asset: str, side: str,
                         count: int, price_cents: int, is_taker: bool):
        """Insert a new bot-initiated order with status='pending'."""
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.conn.execute("""
            INSERT INTO pending_orders (order_id, client_order_id, ticker,
                event_ticker, asset, side, action, count, price_cents,
                status, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (client_order_id, client_order_id, ticker,
              event_ticker, asset, side, "buy", count, price_cents,
              "pending", now, now))
        self.conn.commit()

    def confirm_order_submitted(self, client_order_id: str, order_id: str):
        """Update with server-assigned order_id, set status='resting'."""
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.conn.execute("""
            UPDATE pending_orders SET order_id=?, status='resting', updated_at=?
            WHERE client_order_id=? AND status='pending'
        """, (order_id, now, client_order_id))
        self.conn.commit()

    def cleanup_expired_resting_orders(self):
        """Cancel resting orders whose contract has expired.

        15M tickers encode close time: KXSOL15M-26APR021145-45 → Apr 2 11:45 ET.
        Any resting order past its close time was auto-canceled by Kalshi but the
        local DB row was never updated. This runs periodically (called from snapshot
        builder) to prevent stale orders accumulating in dashboard_state sync.
        """
        import re
        _MONTH_MAP = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
                       "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
        now_utc = datetime.datetime.now(timezone.utc)
        now_str = now_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        rows = self.conn.execute(
            "SELECT order_id, ticker FROM pending_orders WHERE status='resting'"
        ).fetchall()
        cleaned = 0
        for row in rows:
            ticker = row["ticker"]
            m = re.match(r'KX\w+15M-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})-', ticker)
            if not m:
                continue  # Non-15M ticker, skip (hourly/weather have different lifecycle)
            yy, mon, dd, hh, mm = m.groups()
            mon_num = _MONTH_MAP.get(mon)
            if not mon_num:
                continue
            try:
                # Close time is in ET (UTC-4 during EDT)
                close_et = datetime.datetime(2000 + int(yy), mon_num, int(dd), int(hh), int(mm))
                close_utc = close_et.replace(tzinfo=None) + datetime.timedelta(hours=4)
                if now_utc.replace(tzinfo=None) > close_utc:
                    self.conn.execute(
                        "UPDATE pending_orders SET status='expired', updated_at=? WHERE order_id=?",
                        (now_str, row["order_id"]))
                    cleaned += 1
            except (ValueError, OverflowError):
                continue
        if cleaned:
            self.conn.commit()
            logging.info("cleanup_expired_resting: marked %d expired orders", cleaned)

    def mark_order_status(self, order_id: str, status: str):
        """Update order status (filled, canceled, api_error)."""
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.conn.execute("""
            UPDATE pending_orders SET status=?, updated_at=?
            WHERE order_id=? OR client_order_id=?
        """, (status, now, order_id, order_id))
        self.conn.commit()

    def record_position_from_fill(self, ticker: str, event_ticker: str,
                                  asset: str, side: str, count: int,
                                  price_cents: int, strategy=None,
                                  seconds_to_close=None, fill_latency=None,
                                  vol_regime=None, calibrated_prob=None,
                                  edge=None, kelly_f=None,
                                  is_taker=None, fill_source=None,
                                  execution_method=None,
                                  escalation_type=None, maker_price_cents=None,
                                  maker_wait_seconds=None):
        """Record or accumulate a position from a fill.

        If a position already exists for this ticker, accumulate:
        weighted-average price and sum of contracts/cost.
        """
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        fill_cost = count * price_cents
        _sg = strategy_to_group(strategy)

        # Compute fee for THIS fill (per-fill is_taker is accurate)
        _fill_fee = calculate_fee(count, price_cents, is_taker=bool(is_taker))

        existing = self.conn.execute(
            "SELECT count, avg_price_cents, total_cost_cents, opened_at, accumulated_fee_cents "
            "FROM positions WHERE ticker=? AND strategy_group=? AND status='open'",
            (ticker, _sg)
        ).fetchone()

        if existing:
            old_count = existing[0]
            old_cost = existing[2]
            old_fee = existing[4] or 0
            new_count = old_count + count
            new_cost = old_cost + fill_cost
            new_avg = round(new_cost / new_count) if new_count else price_cents
            opened_at = existing[3]
            self.conn.execute("""
                UPDATE positions
                SET count=?, avg_price_cents=?, total_cost_cents=?,
                    is_taker=MAX(is_taker, ?), accumulated_fee_cents=?,
                    updated_at=?
                WHERE ticker=? AND strategy_group=? AND status='open'
            """, (new_count, new_avg, new_cost, 1 if is_taker else 0,
                  old_fee + _fill_fee, now, ticker, _sg))
        else:
            opened_at = now
            _other = self.conn.execute(
                "SELECT 1 FROM positions WHERE ticker=? AND status='open'",
                (ticker,)).fetchone()
            _is_stacked = 1 if _other else 0
            self.conn.execute("""
                INSERT OR REPLACE INTO positions
                    (ticker, event_ticker, asset, side, count,
                     avg_price_cents, total_cost_cents, opened_at, updated_at, status,
                     strategy, seconds_to_close, fill_latency_seconds,
                     vol_regime, calibrated_prob, edge, kelly_f,
                     is_taker, fill_source, execution_method,
                     escalation_type, maker_price_cents, maker_wait_seconds,
                     strategy_group, is_stacked, accumulated_fee_cents)
                VALUES (?,?,?,?,?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (ticker, event_ticker, asset, side, count,
                  price_cents, fill_cost, now, now,
                  strategy, seconds_to_close, fill_latency,
                  vol_regime, calibrated_prob, edge, kelly_f,
                  1 if is_taker else 0, fill_source, execution_method,
                  escalation_type, maker_price_cents, maker_wait_seconds,
                  _sg, _is_stacked, _fill_fee))
        self.conn.commit()

    def update_garch_params(self, asset: str, omega: float, alpha: float,
                            beta: float, last_variance: float):
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.conn.execute("""
            INSERT OR REPLACE INTO garch_params
                (asset, omega, alpha, beta, last_variance, updated_at)
            VALUES (?,?,?,?,?,?)
        """, (asset, omega, alpha, beta, last_variance, now))
        self.conn.commit()

    def update_egarch_params(self, asset: str, omega: float, alpha: float,
                             gamma: float, beta: float,
                             last_log_variance: Optional[float] = None,
                             mle_loglik: Optional[float] = None,
                             mle_converged: bool = False):
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.conn.execute("""
            INSERT OR REPLACE INTO egarch_params
                (asset, omega, alpha, gamma, beta, last_log_variance,
                 mle_loglik, mle_converged, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (asset, omega, alpha, gamma, beta, last_log_variance,
              mle_loglik, 1 if mle_converged else 0, now))
        self.conn.commit()

    def close(self):
        self.conn.close()
