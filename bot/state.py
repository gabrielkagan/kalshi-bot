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
    ENGINE_OWNED_CLIENT_OID_PREFIXES,
    ENGINE_OWNED_OID_PREFIX_TO_STRATEGY,
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


# ── Known SQLite-contention signatures (db-contention log-noise, 2026-06-13) ──
# Three hot-path writers (insert_rejection / insert_evaluated_opportunity /
# insert_bot_order) log a RICH structured WARNING (begin_immediate timing +
# retry count + active/recent-writer envelope) when the chronic single-writer
# + cursor-race contention against state.db trips their BEGIN IMMEDIATE retry
# loop. (The fourth guarded site, mark_rejection_settled, has only a
# commit-race except with NO retry loop and NO log of its own — out of scope
# here; see bot/CLAUDE.md.) For THIS known, handled,
# already-accounted class the additional `exc_info` traceback is pure
# journalctl noise — it points only at the conn.execute line the envelope
# already names. At current universe scale this fires ~100/hr and the
# traceback flood buries genuine ERROR lines. An UNEXPECTED exception (schema
# bug, TypeError, …) keeps its traceback — there the stack is the signal.
# Pinned by tests/integration/test_db_contention_lognoise_regression.py.
_DB_CONTENTION_MARKERS = (
    "database is locked",
    "database table is locked",
    "database is busy",
    "another row available",
    "no more rows available",
    "cannot commit - no transaction is active",
)


def _is_known_db_contention(exc: BaseException) -> bool:
    """True for the chronic, by-design-swallowed SQLite contention class
    whose structured WARNING envelope already carries full diagnostics."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _DB_CONTENTION_MARKERS)


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
        # Bit S.1 (86ba1wrcg, 2026-05-21): per-asset Coinbase WS spot
        # staleness in seconds, computed once per scan tick from
        # CoinbaseFeed.get_price_with_ts. Read by
        # insert_evaluated_opportunity to auto-fill spot_staleness_seconds
        # across ALL 115+ call sites in bot/scanner/__init__.py — mirrors
        # _scan_cx_gap_cache pattern. Missing entry → NULL (asset wasn't
        # processed via the Coinbase scan-path `else` branch this tick;
        # reach: `_pt in (None, "15m", "hourly")` — SPX/weather/sports
        # route through other engines and skip the cache write).
        self._scan_spot_staleness_cache: Dict[str, float] = {}
        # B2b-1 (86ba64h2w, 2026-05-28): per-asset multi-venue synthetic RTI,
        # staged once per scan tick from SyntheticRTIFeed.get_cached_synthetic
        # (an O(1) read of the feed's off-hot-path sampler cache). Read by
        # insert_evaluated_opportunity to auto-fill rti_synthetic /
        # rti_constituent_count / rti_confidence across all Coinbase scan-path
        # insert sites — mirrors _scan_cx_gap_cache. Value is
        # (rti, n_constituents, confidence); missing entry → NULL (asset not
        # in the Coinbase scan path this tick, feed disabled, or sampler
        # stale). SHADOW by default; post-RTI-6 read by the scanner's
        # _effective_decision_spot ONLY for assets in SYNTHETIC_RTI_LIVE_ASSETS
        # (default empty ⇒ feeds no decision).
        self._scan_rti_cache: Dict[str, Tuple[float, int, Optional[float]]] = {}
        # Bit V.1 (2026-06-12): per-asset trailing-300s tape realized vol
        # (bot.helpers.tape_rv.trailing_rv300 — exact parity with the
        # research scripts' rv_5s; see kb/failures/vol-engine-beta-dvol-
        # deflation-jun12.md L-VOL-1). Staged once per asset per scan tick
        # by the scanner's Coinbase spot/vol seam from
        # CoinbaseFeed.get_buffer; consumed in the same tick by the
        # longshot/twaplock overlays as max(blended_rv, rv300). Honest-NULL:
        # the scanner POPS the slot when rv300 is None — mirrors
        # _scan_spot_staleness_cache. TWO-LAYER staleness (R1-M1 fix
        # round): the helper's per-grid-point guard covers BUFFER-shape
        # gaps only (warmup, short buffer, stalled sampler); a frozen WS
        # feed is invisible to it because CoinbaseFeed's sampler re-stamps
        # the last-known price with fresh timestamps every 1s, so the
        # scanner seam ADDITIONALLY treats rv300 as None whenever the
        # Bit-S.1 EVENT-time reading (_scan_spot_staleness_cache above) is
        # missing or > bot.helpers.tape_rv.TAPE_RV_MAX_STALENESS_S (30.0
        # = the validated backtest's abstention horizon).
        # Bit V.3 + V.3-R1-M1 fix round (2026-06-12): the single ATOMIC
        # vol-honesty pair stash — `_scan_vol_pair_cache[asset] =
        # (raw_blended_rv, tape_rv300)`, where raw_blended_rv is the RAW
        # engine estimate BEFORE the V.1 max(blended_rv, rv300) selection
        # and tape_rv300 is the SAME-TICK independent tape RV. Written
        # AND popped by the scanner at the 15M _strategy_vol seam (write
        # when rv300 is present; pop when it is None — plus pops on
        # every rv300-None vol pass and in the 15M loss-cooldown branch,
        # which skips the seam entirely), so both halves come from the
        # same tick BY CONSTRUCTION. The cache only ever holds COMPLETE
        # pairs (persist-both-or-neither: the soak ratio raw/tape needs
        # both halves; a raw without its same-tick tape is most honestly
        # represented as both-NULL). Replaces the two independently-
        # lifecycled V.1 caches (`_scan_tape_rv_cache` honest-NULL-pop +
        # `_scan_raw_blended_rv_cache` overwrite-only) whose
        # "tape-present" proxy freshness gate broke on (1) the cooldown
        # branch (both frozen ≤2h yet tape-present) and (2) hourly
        # passes (fresh tape certified a frozen raw). Consumers: (a)
        # insert_evaluated_opportunity auto-fills `tape_rv300` +
        # `raw_blended_rv` from here, atomically, gated to product_type
        # in (None, '15m') — hourly/SPX/weather rows are both-NULL by
        # construction; (b) the eval rows' volatility column carries the
        # max — the honest input the DECISION used — so the V.3 re-arm
        # deflation ratio (per-asset median raw_blended_rv/tape_rv300,
        # gate [0.8, 1.25]) MUST source both halves here; computed off
        # the rows it would be max(b, rv300)/rv300 >= 1 always and could
        # never detect deflation. The scanner's VolHonestyMonitor feed
        # reads the seam LOCALS (same-tick by definition), not this
        # cache.
        self._scan_vol_pair_cache: Dict[str, Tuple[float, float]] = {}
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

            -- Per-decision config snapshot (ticket 86b9zkp8p, 2026-05-17).
            -- Captures EXACTLY which config produced each evaluated/rejected
            -- decision via sha256 of bot/constants.py + bot/config.py +
            -- market_config.py + sorted-key JSON of tracked env-var flags +
            -- git HEAD. Phase-1 captures once at MainLoop.__init__; mid-day
            -- mutation re-capture is Phase-2. See bot/CLAUDE.md
            -- "config_snapshot_id schema chain" + bot/helpers/config_snapshot.py.
            CREATE TABLE IF NOT EXISTS config_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                config_hash TEXT NOT NULL UNIQUE,
                captured_at TEXT NOT NULL,
                git_head_sha TEXT,
                constants_sha TEXT NOT NULL,
                config_sha TEXT NOT NULL,
                market_config_sha TEXT NOT NULL,
                env_flags_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_config_snapshots_hash
                ON config_snapshots(config_hash);

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
            -- Bit 86ba0jvka-fu / hotfix 2026-05-19: the unique index
            -- on evaluated_opportunities was REMOVED from this
            -- executescript block. It MUST include `side` (per the
            -- dual-side YES+NO evaluation pattern from
            -- bot/scanner/__init__.py NO-side queue +
            -- bot/shadows/hourly_alt_shadow.py::_evaluate_no_side),
            -- but the `side` column doesn't exist at this point —
            -- it's added by the ALTER TABLE loop further down in
            -- _create_tables. The DROP+CREATE migration at the same
            -- function (search anchor: "update unique index to
            -- include side") creates the 3-col UNIQUE INDEX AFTER
            -- the ALTER TABLE runs, which is the correct ordering.
            -- The pre-hotfix 2-col form `(ticker, filter_stage)`
            -- crashed _create_tables on any DB seeded with dual-side
            -- data (incident 2026-05-19 10:46 UTC: bot crash-looped
            -- because legitimate YES+NO complement rows violated
            -- the 2-col uniqueness — see post-incident operator
            -- recovery via manual `CREATE UNIQUE INDEX ...
            -- (ticker, filter_stage, side)` on production state.db
            -- at 10:55:25 UTC).
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
            # BNB T1.5 followup (2026-05-17, ticket 86b9zn5pq). Supabase
            # migration 021 mirrors this to the remote evaluations table.
            # Scanner producer at bot/scanner/__init__.py:1011 is
            # ASSETS-driven (BNB T1 2026-05-17) and emits all 7 keys; the
            # consumer block below was the silent-drop site this Bit closes.
            ("bnb_spot_at_decision", "REAL"),
            # ADA/BCH T1 15M shadow (2026-05-30, ada-bch-15m-shadow-t1).
            # Wired end-to-end here proactively to avoid the BNB-T1 silent-drop
            # gap (followup 86b9zn5pq): the scanner producer at
            # bot/scanner/__init__.py:1056 is ASSETS-driven and emits these keys
            # the moment ADA/BCH enter config.ASSETS.
            ("ada_spot_at_decision", "REAL"),
            ("bch_spot_at_decision", "REAL"),
            # NEAR/ZEC T1 15M shadow (2026-09-05, 86bbvdc8y) — same proactive wiring.
            ("near_spot_at_decision", "REAL"),
            ("zec_spot_at_decision", "REAL"),
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
            # Per-decision config snapshot advisory-pointer (ticket 86b9zkp8p, 2026-05-17).
            # NULLABLE so legacy rows (pre-snapshot ship) and any caller that
            # forgets to pass the kwarg remain insertable. New scanner call
            # sites pass `config_snapshot_id=self._ml.config_snapshot_id`.
            # See bot/helpers/config_snapshot.py + bot/CLAUDE.md
            # "config_snapshot_id schema chain".
            ("config_snapshot_id", "INTEGER"),
            # TM half-Kelly cal_mlp shadow (Sim C, ticket 86ba0v7fc, 2026-05-19).
            # 4-column lockstep with insert_evaluated_opportunity + the
            # tm_shadow_kelly_contracts_with_bound helper. SHADOW-ONLY — these
            # cols log the counterfactual Kelly size + which constraint bound
            # it; NEVER consumed by production sizing. See
            # kb/decisions/tm-half-kelly-shadow-plan.md + bot/CLAUDE.md
            # "_shadow_diag schema chain".
            ("tm_shadow_kelly_ct", "INTEGER"),
            ("tm_shadow_kelly_prob", "REAL"),
            ("tm_shadow_kelly_fraction", "REAL"),
            ("tm_shadow_kelly_bound_hit", "TEXT"),
            # Bit S.1 (86ba1wrcg, 2026-05-21): Coinbase WS spot staleness
            # at evaluation time. Seconds since the last WS ticker frame
            # populated CoinbaseFeed._prices[asset]. Populated on every
            # Coinbase scan-path insert (candidate, decided_contract*,
            # insufficient_edge, price_out_of_range, silent_spot_none,
            # all 115+ sites) via the per-asset
            # `_scan_spot_staleness_cache` auto-fill — same pattern as
            # `_scan_cx_gap_cache`. NULL on (a) non-Coinbase scan paths
            # (SPX/weather/sports route through other engines + don't
            # populate the cache; hourly DOES populate because it falls
            # through the same Coinbase `else` branch as 15M),
            # (b) the warmup case where CoinbaseFeed has never seen a
            # tick for the asset (cache slot popped),
            # (c) backfill / test callers that pass neither the kwarg
            # nor the cache. Observability-only — S.3 (ticket
            # 86ba1wrka under umbrella 86ba1wrad) is the production gate.
            ("spot_staleness_seconds", "REAL"),
            # B2b-1 (86ba64h2w, 2026-05-28): in-bot multi-venue synthetic RTI,
            # SHADOW-ONLY. Decision-time CFB-shape reconstruction from 4-venue
            # L2 (Coinbase/Kraken/Bitstamp/Gemini) paired with the live
            # single-venue Coinbase signal + outcome → the dataset Bit 3
            # retrains on. WRITE-ONLY: auto-filled on every Coinbase scan-path
            # insert via the per-asset `_scan_rti_cache` (mirrors
            # `_scan_cx_gap_cache` → spot_coinbase_kraken_gap_bps); read by a
            # decision path (scanner _effective_decision_spot) ONLY for assets
            # in SYNTHETIC_RTI_LIVE_ASSETS (RTI-6; default empty ⇒ the
            # zero-live-decision-change invariant holds for every asset).
            # rti_synthetic = the index; rti_constituent_count = venues that
            # contributed; rti_confidence = contributed / expected (the CFB
            # constituent set the feed can source for the asset). NULL on
            # non-Coinbase scan paths, sampler stall (cache stale), the
            # disabled kill-switch (SYNTHETIC_RTI_ENABLED=False default), or
            # backfill/test callers. See kb/decisions/b2b-1-core-shadow-plan.md.
            ("rti_synthetic", "REAL"),
            ("rti_constituent_count", "INTEGER"),
            ("rti_confidence", "REAL"),
            # Bit V.3 (2026-06-12): vol-honesty pair — the soak's
            # measurement layer for the L-VOL-2 lesson (kb/failures/
            # vol-engine-beta-dvol-deflation-jun12.md). tape_rv300 =
            # the independent trailing-300s tape realized vol
            # (bot.helpers.tape_rv.trailing_rv300, exact backtest
            # parity); raw_blended_rv = the RAW engine estimate BEFORE
            # the V.1 max(blended_rv, rv300) selection (V.1-R1-M2: the
            # rows' `volatility` column carries the max, so the
            # deflation ratio computed off it would be >= 1 always and
            # could never detect deflation). Auto-filled ATOMICALLY from
            # the single per-asset pair cache `_scan_vol_pair_cache`
            # (V.3-R1-M1: both halves same-tick by construction;
            # persist-both-or-neither; gated to product_type in
            # (None, '15m') so hourly/SPX/weather rows are both-NULL).
            # Re-arm soak gate reads these: per-asset median
            # raw_blended_rv/tape_rv300 ∈ [0.8, 1.25]
            # (kb/decisions/longshot-twap-live-small-plan.md,
            # pre-registered; /live-small soak section).
            ("tape_rv300", "REAL"),
            ("raw_blended_rv", "REAL"),
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

        # Migration: index on config_snapshot_id advisory pointer for fast replay lookups.
        # Ticket 86b9zkp8p (2026-05-17). Idempotent: re-runs are no-ops.
        try:
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_eval_opp_config_snapshot "
                "ON evaluated_opportunities(config_snapshot_id)")
            self.conn.commit()
        except sqlite3.OperationalError:
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
            # Per-decision config snapshot advisory-pointer (ticket 86b9zkp8p, 2026-05-17).
            # See bot/helpers/config_snapshot.py + the matching
            # evaluated_opportunities ALTER above for the schema chain.
            ("config_snapshot_id", "INTEGER"),
        ]:
            try:
                self.conn.execute(f"ALTER TABLE rejected_opportunities ADD COLUMN {col_def[0]} {col_def[1]}")
            except sqlite3.OperationalError:
                pass  # column already exists
        self.conn.commit()

        # Migration: index on config_snapshot_id advisory pointer for fast replay lookups
        # (ticket 86b9zkp8p, 2026-05-17). Idempotent.
        try:
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rejected_opp_config_snapshot "
                "ON rejected_opportunities(config_snapshot_id)")
            self.conn.commit()
        except sqlite3.OperationalError:
            pass

        # Scan-productive watchdog (2026-09-07): EXISTS on
        # product_type + evaluation_time / rejection_time was a full
        # table scan (622k eval + 2.4M reject). IF NOT EXISTS — first
        # boot after deploy may take tens of seconds; later boots no-op.
        try:
            logging.info(
                "ensuring idx_eval_opp_pt_time / idx_rejected_opp_pt_time")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_eval_opp_pt_time "
                "ON evaluated_opportunities(product_type, evaluation_time)")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rejected_opp_pt_time "
                "ON rejected_opportunities(product_type, rejection_time)")
            self.conn.commit()
        except sqlite3.Error:
            logging.warning(
                "idx_eval_opp_pt_time / idx_rejected_opp_pt_time failed",
                exc_info=True)
        try:
            _have = {
                row[0]
                for row in self.conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND name IN "
                    "('idx_eval_opp_pt_time','idx_rejected_opp_pt_time')"
                )
            }
            _need = {"idx_eval_opp_pt_time", "idx_rejected_opp_pt_time"}
            if _need - _have:
                logging.error(
                    "idx_eval_opp_pt_time / idx_rejected_opp_pt_time missing "
                    "after boot: have=%s — miss-path EXISTS reverts to full "
                    "scan (~1.5s/tick on 2.4M reject rows)",
                    sorted(_have),
                )
        except sqlite3.Error:
            logging.error(
                "could not verify idx_eval_opp_pt_time / "
                "idx_rejected_opp_pt_time",
                exc_info=True)

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
                "OR ticker LIKE 'KXHYPE15M%' OR ticker LIKE 'KXDOGE15M%' "
                "OR ticker LIKE 'KXBNB15M%' "
                "OR ticker LIKE 'KXADA15M%' OR ticker LIKE 'KXBCH15M%' "
                "OR ticker LIKE 'KXNEAR15M%' OR ticker LIKE 'KXZEC15M%')")
            self.conn.execute(
                "UPDATE settled_trades SET product_type='hourly' WHERE product_type IS NULL "
                "AND (ticker LIKE 'KXBTCD%' OR ticker LIKE 'KXETHD%' "
                "OR ticker LIKE 'KXSOLD%' OR ticker LIKE 'KXXRPD%' "
                "OR ticker LIKE 'KXHYPED%' OR ticker LIKE 'KXDOGED%' "
                "OR ticker LIKE 'KXBNBD%' "
                "OR ticker LIKE 'KXADAD%' OR ticker LIKE 'KXBCHD%' "
                "OR ticker LIKE 'KXNEARD%' OR ticker LIKE 'KXZECD%')")
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
                "OR ticker LIKE 'KXHYPE15M%' OR ticker LIKE 'KXDOGE15M%' "
                "OR ticker LIKE 'KXBNB15M%' "
                "OR ticker LIKE 'KXADA15M%' OR ticker LIKE 'KXBCH15M%' "
                "OR ticker LIKE 'KXNEAR15M%' OR ticker LIKE 'KXZEC15M%')")
            self.conn.execute(
                "UPDATE evaluated_opportunities SET product_type='hourly' WHERE product_type IS NULL "
                "AND (ticker LIKE 'KXBTCD%' OR ticker LIKE 'KXETHD%' "
                "OR ticker LIKE 'KXSOLD%' OR ticker LIKE 'KXXRPD%' "
                "OR ticker LIKE 'KXHYPED%' OR ticker LIKE 'KXDOGED%' "
                "OR ticker LIKE 'KXBNBD%' "
                "OR ticker LIKE 'KXADAD%' OR ticker LIKE 'KXBCHD%' "
                "OR ticker LIKE 'KXNEARD%' OR ticker LIKE 'KXZECD%')")
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

        # Migration: per-order recorded-fill counter (Bit L-1 R4-M2,
        # 2026-06-11). LongshotEngine._apply_fills bumps it after each
        # successful record_position_from_fill; boot reconciliation seeds
        # each order's boot_skip_remaining from ITS OWN row (NULL = legacy
        # pre-R4 row -> (ticker, side) aggregate fallback). NO DEFAULT on
        # the ALTER: SQLite reports an ADD-COLUMN default for pre-existing
        # rows too, which would erase the NULL-legacy distinction —
        # insert_bot_order writes the explicit 0 for new rows instead.
        # Lives in this ALTER loop, NOT the _create_tables executescript,
        # and no index may reference it (schema-bootstrap DDL ordering:
        # the executescript runs BEFORE the ALTER loops — PR #107 lesson).
        for col_def in [
            ("recorded_fill_count", "INTEGER"),
        ]:
            try:
                self.conn.execute(
                    f"ALTER TABLE pending_orders ADD COLUMN {col_def[0]} {col_def[1]}")
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
            try:
                position_count = fp_str_to_int(pos.get("position_fp"))
                if not position_count:
                    position_count = pos.get("position") or 0
                position_count = int(position_count)
            except (TypeError, ValueError, OverflowError):
                logging.warning(
                    "RECONCILE_POSITION_PARSE_MALFORMED ticker=%s "
                    "position_fp=%r position=%r — skipping this row",
                    ticker, pos.get("position_fp"), pos.get("position"))
                continue

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
            try:
                cost = dollars_str_to_cents(cost_d) if cost_d else (
                    pos.get("market_exposure") or 0)
            except (TypeError, ValueError, OverflowError):
                logging.warning(
                    "RECONCILE_POSITION_COST_PARSE_MALFORMED ticker=%s "
                    "market_exposure_dollars=%r — skipping this row",
                    ticker, cost_d)
                continue
            avg_price = cost // count if count else 0

            local_rows = self.conn.execute(
                "SELECT strategy_group, count, total_cost_cents "
                "FROM positions WHERE ticker=? AND status='open'",
                (ticker,)
            ).fetchall()

            if len(local_rows) == 0:
                # Settled-trades guard: if we already processed a
                # settlement event for this ticker, settled_trades has
                # a row. Kalshi's positions API can lag in dropping
                # just-settled positions — trust our authoritative
                # ledger and skip. Prevents -fu2 phantom-row class.
                settled_row = self.conn.execute(
                    "SELECT 1 FROM settled_trades WHERE ticker=? LIMIT 1",
                    (ticker,)
                ).fetchone()
                if settled_row:
                    logging.warning(
                        "RECONCILE_TRUSTED_LOCAL_SETTLE: ticker=%s "
                        "settled_trades row exists, ignoring Kalshi positions-API lag",
                        ticker)
                    continue

                # No settled_trades record AND no open local row — orphan
                # settle or new position. INSERT from API; if conflict
                # (pre-existing non-open row at (ticker, 'main')), heal
                # in place to restore local visibility.
                asset = self._asset_from_ticker(ticker)
                event_ticker = self._event_ticker_from_ticker(ticker)
                # R3-M1 + R4-MN2 (generalized at Bit T-1): stamp an
                # ENGINE-OWNED strategy ONLY when the MOST RECENT
                # pending_orders row on the ticker (ANY prefix, any status)
                # carries that engine's client_oid prefix — i.e. the engine
                # was the last strategy to trade it. Pre-R4 this was an
                # existence check on ls- history, so a weeks-old longshot
                # quote claimed an import the MAIN pipeline most recently
                # traded. When the recency test passes, the position lands
                # inside that engine's rails (caps, marks, streaks) instead
                # of the DDL default 'main'. The prefix->strategy map is
                # single-sourced in
                # bot.constants.ENGINE_OWNED_OID_PREFIX_TO_STRATEGY
                # ('ls-'->'longshot', 'tw-'->'twaplock'; both literals pass
                # through strategy_to_group unchanged).
                _last_order = self.conn.execute(
                    "SELECT order_id, client_order_id FROM pending_orders "
                    "WHERE ticker=? ORDER BY created_at DESC LIMIT 1",
                    (ticker,)).fetchone()
                _eng_strategy = None
                if _last_order is not None:
                    _last_coid = _last_order["client_order_id"] or ""
                    for _pfx, _strat in (
                            ENGINE_OWNED_OID_PREFIX_TO_STRATEGY.items()):
                        if _last_coid.startswith(_pfx):
                            _eng_strategy = _strat
                            break
                try:
                    if _eng_strategy:
                        self.conn.execute("""
                            INSERT INTO positions (ticker, event_ticker, asset,
                                side, count, avg_price_cents, total_cost_cents,
                                opened_at, updated_at, status,
                                strategy, strategy_group)
                            VALUES (?,?,?,?,?,?,?,?,?,'open', ?, ?)
                        """, (ticker, event_ticker, asset, side, count,
                              avg_price, cost, now, now,
                              _eng_strategy, _eng_strategy))
                        # R4-M2: the imported contracts are "already
                        # embodied" truth that the engine never
                        # counter-attributed (longshot's per-order
                        # recorded_fill_count only counts its OWN
                        # record_position_from_fill calls; harmless no-op
                        # for twaplock, which never reads the column).
                        # Attribute the whole import to the MOST RECENT
                        # engine-owned order so its own-row boot skip seed
                        # absorbs the fill refetch. Single-row attribution
                        # is a best-guess when multiple engine orders
                        # contributed; the residual multi-order ambiguity
                        # rides with ticket 86badbf9t's durable rebuild.
                        self.conn.execute(
                            "UPDATE pending_orders SET recorded_fill_count="
                            "COALESCE(recorded_fill_count, 0) + ? "
                            "WHERE order_id=?",
                            (count, _last_order["order_id"]))
                        logging.warning(
                            "RECONCILE_IMPORT_%s: ticker=%s side=%s "
                            "count=%d — unknown position imported with "
                            "strategy_group='%s' (engine-owned pending "
                            "history)", _eng_strategy.upper(), ticker, side,
                            count, _eng_strategy)
                    else:
                        self.conn.execute("""
                            INSERT INTO positions (ticker, event_ticker, asset, side,
                                count, avg_price_cents, total_cost_cents,
                                opened_at, updated_at, status)
                            VALUES (?,?,?,?,?,?,?,?,?,'open')
                        """, (ticker, event_ticker, asset, side, count,
                              avg_price, cost, now, now))
                except sqlite3.IntegrityError as e:
                    conflict = self.conn.execute(
                        "SELECT status FROM positions "
                        "WHERE ticker=? AND strategy_group='main'",
                        (ticker,)
                    ).fetchone()
                    if conflict and dict(conflict)["status"] != "open":
                        self.conn.execute("""
                            UPDATE positions SET side=?, count=?,
                                avg_price_cents=?, total_cost_cents=?,
                                updated_at=?, status='open'
                            WHERE ticker=? AND strategy_group='main'
                        """, (side, count, avg_price, cost, now, ticker))
                        logging.warning(
                            "RECONCILE_REOPEN_SETTLED: ticker=%s prev_status=%s",
                            ticker, dict(conflict)["status"])
                    else:
                        logging.warning(
                            "RECONCILE_INSERT_CONFLICT: ticker=%s err=%s — skipping",
                            ticker, e)
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
                    self._reconcile_multi_mismatch(
                        ticker, side, count, now)

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

    def _reconcile_multi_mismatch(self, ticker: str, side: str,
                                  api_total: int, now: str) -> None:
        """B2 (86b9zud1p): handle a multi-row local-vs-API divergence.

        Phase 1 — always emit a per-row evidence log (strategy_group, count,
        fill_source, delta = side_local_total - api_total) so retroactive
        cleanup can reconstruct the ambiguity from logs alone.

        Phase 2 — when delta > 0 AND ≥1 same-side open row has
        `fill_source` starting with 'ghost_fill', deflate the ghost-fill
        row(s) until sum(count) == api_total. Non-ghost-fill rows are
        left untouched. With multiple ghost-fill rows, deflate
        proportionally by count share (largest-remainder rounding so the
        sum lands exactly on the target). When a ghost-fill row reaches
        count=0 it is DELETED (mirrors the existing reconcile DELETE at
        the ticker-level zero-position path; avoids polluting
        `settled_trades` with a zero-row entry when settlement later
        iterates `WHERE ticker=?` without a status filter — the per-row
        AUTOFIXED log line is the audit trail).

        **Narrow fill_source gate (L100 caveat)**: `record_position_from_fill`
        sets `fill_source` only on the FIRST INSERT for a
        (ticker, strategy_group) pair, NOT on subsequent UPDATEs. If a
        ghost-fill landed via UPDATE on a row that an earlier IOC fill
        stamped 'ioc', this auto-fix CANNOT identify the contaminated row;
        the ticker falls through to the no-fix warning branch. B1
        (`b3fe7546`, 2026-05-18) prevents the upstream class in
        `bot/executor.py`; B2 is defense-in-depth for the narrower subset
        where the ghost-fill row was the FIRST INSERT on its
        (ticker, strategy_group). See
        `kb/failures/ghost-fill-retry-overcount-may18.md` for the HYPE
        incident shape.

        **Side-filter rationale**: helper SELECTs `WHERE side=?` because
        per-side is the only sensible auto-fix scope (a side flip is
        itself a bug class). `_reconcile_positions`'s mismatch-detection
        sum is unfiltered by side; if those values diverge, both will
        appear in the side_local_total vs the caller's pre-call logging.

        **Cost-preservation limitation**: when deflating, `total_cost_cents`
        is rescaled as `new_count * avg_price_cents` — the row's original
        avg may itself be distorted (it was computed when the row was
        inflated). Settlement will see the rescaled cost. For full fidelity,
        operator should manually verify post-incident via the per-row
        evidence log (pre_count/pre_avg/post_count/post_cost in the
        AUTOFIXED log line).

        Under-count (delta < 0) is a separate bug class (possibly missed
        maker fill); keep the warning, no mutation.
        """
        rows = self.conn.execute(
            "SELECT strategy_group, count, avg_price_cents, "
            "total_cost_cents, fill_source FROM positions "
            "WHERE ticker=? AND side=? AND status='open'",
            (ticker, side),
        ).fetchall()
        side_local_total = sum(dict(r)["count"] for r in rows)
        delta = side_local_total - api_total
        if delta == 0:
            # Caller's mismatch was a cross-side artefact (caller sums all
            # sides; helper is side-scoped per Kalshi's one-row-per-ticker
            # positions API). On the side that matters, local matches API
            # — return silently. Mixed-side rows are a separate bug class
            # outside B2 scope.
            return
        evidence = ", ".join(
            f"{dict(r)['strategy_group']}=(count={dict(r)['count']},"
            f"avg={dict(r)['avg_price_cents']},"
            f"fill_source={dict(r)['fill_source']})"
            for r in rows
        )
        ghost_rows = [dict(r) for r in rows
                      if (dict(r).get("fill_source") or "").startswith("ghost_fill")]

        if delta > 0 and ghost_rows:
            excess = delta
            ghost_sum = sum(g["count"] for g in ghost_rows)
            if ghost_sum < excess:
                logging.warning(
                    "RECONCILE_MULTI_MISMATCH: %s local=%d api=%d delta=%d "
                    "ghost_sum=%d — NOT auto-fixing (excess > ghost-fill capacity); "
                    "rows=[%s]",
                    ticker, side_local_total, api_total, delta,
                    ghost_sum, evidence)
                return

            if len(ghost_rows) == 1:
                takes = {ghost_rows[0]["strategy_group"]: excess}
            else:
                # Largest-remainder rounding so the sum lands exactly on excess.
                raw = [(g["strategy_group"],
                        excess * g["count"] / ghost_sum,
                        g["count"]) for g in ghost_rows]
                floors = [(sg, int(r), cap) for sg, r, cap in raw]
                assigned = sum(f for _, f, _ in floors)
                remainder = excess - assigned
                # Distribute leftover units to the largest fractional parts.
                ranked = sorted(
                    range(len(raw)),
                    key=lambda i: (raw[i][1] - floors[i][1]),
                    reverse=True,
                )
                takes_list = [list(f) for f in floors]
                for i in ranked:
                    if remainder <= 0:
                        break
                    if takes_list[i][1] < takes_list[i][2]:
                        takes_list[i][1] += 1
                        remainder -= 1
                takes = {sg: take for sg, take, _ in takes_list}

            per_row_post = []
            for g in ghost_rows:
                take = takes.get(g["strategy_group"], 0)
                if take <= 0:
                    continue
                new_count = g["count"] - take
                # See "Cost-preservation limitation" in docstring — rescale
                # at the row's stored avg_price; operator audit is the
                # backstop for distorted-avg fidelity.
                new_cost = new_count * g["avg_price_cents"]
                if new_count <= 0:
                    # DELETE (not status='closed') so settlement's
                    # status-unfiltered `WHERE ticker=?` does NOT iterate a
                    # zero-count phantom row into `settled_trades`.
                    self.conn.execute(
                        "DELETE FROM positions WHERE ticker=? AND strategy_group=?",
                        (ticker, g["strategy_group"]))
                    per_row_post.append(
                        f"{g['strategy_group']}=(pre_count={g['count']},"
                        f"pre_avg={g['avg_price_cents']},post=DELETED)")
                else:
                    self.conn.execute(
                        "UPDATE positions SET count=?, total_cost_cents=?, "
                        "updated_at=? WHERE ticker=? AND strategy_group=?",
                        (new_count, new_cost, now, ticker, g["strategy_group"]))
                    per_row_post.append(
                        f"{g['strategy_group']}=(pre_count={g['count']},"
                        f"pre_avg={g['avg_price_cents']},"
                        f"post_count={new_count},post_cost={new_cost},"
                        f"post_avg={g['avg_price_cents']})")
            # Distinct event tag so operator alerts can grep auto-fixed
            # vs un-fixed separately (L100 — don't share a wire-protocol
            # string for two semantics).
            logging.warning(
                "RECONCILE_MULTI_MISMATCH_AUTOFIXED: %s local=%d api=%d delta=%d "
                "— deflated ghost-fill rows; pre=[%s] post=[%s]; "
                "note: remaining open rows may still trigger the post-reconcile "
                "STACKING_DISABLED check by design",
                ticker, side_local_total, api_total, delta,
                evidence, ", ".join(per_row_post))
            return

        logging.warning(
            "RECONCILE_MULTI_MISMATCH: %s local=%d api=%d delta=%d — "
            "NOT auto-fixing (no ghost-fill row); rows=[%s]",
            ticker, side_local_total, api_total, delta, evidence)

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
        # R3-M1 carve-out (generalized at Bit T-1): engine-owned orders
        # (client_oid prefixes in ENGINE_OWNED_CLIENT_OID_PREFIXES — ls-
        # longshot, tw- twaplock) are ENGINE-OWNED flow.
        # LongshotEngine._boot_reconcile_orphans adopts/reconciles ls-
        # orders at its first tick (which runs AFTER this startup
        # reconcile); TwaplockEngine's first-tick boot sweep owns stranded
        # tw- rows (an IOC never legitimately rests). Cancelling them here
        # neutralized that machinery and flipping their rows off 'resting'
        # hid them from the engines' boot queries. Skip them in BOTH the
        # cancel sweep and the local row-flip loops below.
        _stale_canceled = 0
        for order in resting_orders:
            oid = order["order_id"]
            api_order_ids.add(oid)
            coid = order.get("client_order_id") or ""
            if coid.startswith(ENGINE_OWNED_CLIENT_OID_PREFIXES):
                _eng = next(
                    (s for p, s in
                     ENGINE_OWNED_OID_PREFIX_TO_STRATEGY.items()
                     if coid.startswith(p)), "engine")
                logging.info(
                    "RECONCILE_SKIP_%s: %s ticker=%s — engine owns this "
                    "order lifecycle (boot step adopts/sweeps at first "
                    "tick)", _eng.upper(), oid, order.get("ticker"))
                continue
            ticker = order["ticker"]
            try:
                client.cancel_order(oid, ticker=ticker)
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
            # R3-M1 (generalized at Bit T-1): engine-owned (ls-/tw-) orders
            # were NOT canceled above — don't flip their local rows to
            # 'canceled' (a lie that hides them from the engines' boot
            # steps) and don't INSERT a synthetic 'canceled' history row
            # (the engine adopts/sweeps straight from its own boot pass).
            if (order.get("client_order_id") or "").startswith(
                    ENGINE_OWNED_CLIENT_OID_PREFIXES):
                continue
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
            try:
                if ypd:
                    price = dollars_str_to_cents(ypd)
                elif npd:
                    price = dollars_str_to_cents(npd)
                else:
                    price = order.get("yes_price", 0) or order.get("no_price", 0)
                price = int(price)
            except (TypeError, ValueError, OverflowError):
                logging.warning(
                    "RECONCILE_ORDER_PRICE_PARSE_MALFORMED oid=%s — "
                    "falling back to integer cents", oid)
                try:
                    price = int(order.get("yes_price", 0) or order.get("no_price", 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    price = 0

            try:
                remaining = fp_str_to_int(order.get("remaining_count_fp"))
                if not remaining:
                    remaining = order.get("remaining_count") or 0
                remaining = int(remaining)
            except (TypeError, ValueError, OverflowError):
                logging.warning(
                    "RECONCILE_ORDER_REMAINING_PARSE_MALFORMED oid=%s "
                    "remaining_count_fp=%r — falling back to integer remaining",
                    oid, order.get("remaining_count_fp"))
                try:
                    remaining = int(fp_str_to_int(
                        order.get("remaining_count") or 0))
                except (TypeError, ValueError, OverflowError):
                    remaining = 0

            self.conn.execute("""
                INSERT INTO pending_orders (order_id, client_order_id, ticker,
                    event_ticker, asset, side, action, count, price_cents,
                    status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,'canceled',?,?)
            """, (oid, order.get("client_order_id", ""), ticker,
                  event_ticker, asset, order["side"], order["action"],
                  remaining, price,
                  order.get("created_time", now), now))

        # Mark local resting orders not on API as canceled.
        # R3-M1 (generalized at Bit T-1): engine-owned (ls-/tw-) rows are
        # skipped — a longshot order absent from the API list (fully
        # filled / expired pre-restart) is reconciled by
        # LongshotEngine._boot_reconcile_orphans step 2, which NEEDS the
        # row still 'resting' to find it (fills recorded, row then marked
        # filled/canceled by the engine); a stranded tw- row is flipped to
        # 'canceled' by TwaplockEngine's first-tick boot sweep, the single
        # owner of that transition.
        local_rows = self.conn.execute(
            "SELECT order_id, client_order_id FROM pending_orders "
            "WHERE status='resting'"
        ).fetchall()
        for row in local_rows:
            if (row["client_order_id"] or "").startswith(
                    ENGINE_OWNED_CLIENT_OID_PREFIXES):
                continue
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

    def get_local_position_count_for_ticker(self, ticker: str, side: str) -> int:
        """Sum open-position count for (ticker, side) across ALL strategy_groups.

        Truth source for the ghost-fill delta-COUNT calculation in
        `bot/executor.py::OrderExecutor._submit_taker` (ticket 86b9zuczz).
        Kalshi's positions API returns one row per ticker (cumulative across
        strategy_groups), so to compute the **new contracts** delivered by a
        single IOC attempt we subtract this local cumulative from the API count.
        """
        row = self.conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM positions "
            "WHERE ticker=? AND side=? AND status='open'",
            (ticker, side),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def get_local_position_cost_for_ticker(self, ticker: str, side: str) -> int:
        """Sum open-position total_cost_cents for (ticker, side) across ALL
        strategy_groups.

        Pair to `get_local_position_count_for_ticker` for the ghost-fill
        delta-COST calculation (ticket 86b9zuczz, R1-M2). Used by Layer B to
        attribute the new-contracts cost to the actual delta price rather
        than to the cumulative weighted-average across sibling strategies.
        """
        row = self.conn.execute(
            "SELECT COALESCE(SUM(total_cost_cents), 0) FROM positions "
            "WHERE ticker=? AND side=? AND status='open'",
            (ticker, side),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def get_open_position_exposure_cents(self) -> int:
        # Cost-basis sum (count * avg_price_cents) across open positions.
        # Used by main_loop + settlement Telegram alerts so "Balance:"
        # approximates Kalshi UI Portfolio total (cash + positions)
        # rather than cash only. NOT bit-exact with Kalshi's market-mark
        # figure — diverges as mark moves away from fill price (e.g.
        # observed ~8% gap on 2026-05-13 user screenshot).
        return sum(
            int(p.get("count", 0) or 0) * int(p.get("avg_price_cents", 0) or 0)
            for p in self.get_open_positions()
        )

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
        if any(ticker.startswith(p) for p in ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXHYPE15M", "KXDOGE15M", "KXBNB15M", "KXADA15M", "KXBCH15M", "KXNEAR15M", "KXZEC15M")):
            product_type = "15m"
        elif any(ticker.startswith(p) for p in ("KXBTCD", "KXETHD", "KXSOLD", "KXXRPD", "KXHYPED", "KXDOGED", "KXBNBD", "KXADAD", "KXBCHD", "KXNEARD", "KXZECD")):
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
                         prob_breakeven_gap: Optional[float] = None,
                         # Per-decision config snapshot advisory-pointer (ticket 86b9zkp8p,
                         # 2026-05-17). NULLABLE for backward compat with
                         # callers that pre-date the schema chain (e.g.
                         # backfill scripts, integration tests). Production
                         # scanner call sites pass
                         # `config_snapshot_id=self._ml.config_snapshot_id`.
                         # See bot/helpers/config_snapshot.py.
                         config_snapshot_id: Optional[int] = None):
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

        # Ticket 86ba0jvgw (2026-05-19) — defensive guard mirroring the
        # sister `StateManager.insert_evaluated_opportunity` (its BEGIN
        # IMMEDIATE retry loop + outer-try WARNING envelope are the
        # structural sister) and `StateManager.insert_bot_order` (its
        # `_log_insert_bot_order_failure` envelope mirrors the
        # diagnostic envelope here). 4-site coverage of the StateManager
        # hot-path writers completes here:
        #
        #   insert_evaluated_opportunity → SWALLOW (telemetry)
        #   mark_rejection_settled       → B3-fu1 swallow (telemetry)
        #   insert_bot_order             → RAISE (crash safety)
        #   insert_rejection             → SWALLOW (telemetry) ◀ this site
        #
        # Pattern: BEGIN IMMEDIATE retry-on-busy (3 attempts, 25-75ms
        # jittered backoff — same SCAN_BODY_SLOW budget as the
        # `insert_evaluated_opportunity` retry loop) + B3-fu1
        # commit-race swallow on the COMMIT step + broad telemetry
        # swallow with structured WARNING on any other OperationalError
        # (including the 2026-05-19 incident's `another row available`
        # signature from disk-full cursor corruption). Rejection rows
        # are telemetry; losing one is preferable to crashing the scan
        # tick (same divergence as `insert_evaluated_opportunity`).
        #
        # Pinned by tests/integration/test_insert_rejection_defensive_guard_regression.py.
        # NOTE: this comment uses SYMBOLIC anchors (function names) for
        # cross-references rather than line numbers — line refs drift
        # with every shift in the file, per R1-M1 ratchet of this Bit.
        _be_err_repr: Optional[str] = None
        _be_duration_ms: Optional[float] = None
        _be_retries: int = 0
        _began_explicitly = False
        _t0_lock = time.perf_counter()
        try:
            # 2026-05-22 R1: parent-class catch — see lockstep note in
            # insert_evaluated_opportunity retry loop. Production
            # histogram 2026-05-22 attributed 9 `DatabaseError: another
            # row available` traceback frames to this function alone.
            for _attempt in range(3):
                try:
                    self.conn.execute("BEGIN IMMEDIATE")
                    _be_duration_ms = (time.perf_counter() - _t0_lock) * 1000.0
                    _began_explicitly = True
                    _be_retries = _attempt
                    break
                except sqlite3.DatabaseError as _be_err:
                    _be_duration_ms = (time.perf_counter() - _t0_lock) * 1000.0
                    _be_err_repr = f"{type(_be_err).__name__}: {_be_err}"
                    _be_retries = _attempt + 1
                    _err_msg = str(_be_err).lower()
                    _is_transient = ("locked" in _err_msg) or ("busy" in _err_msg)
                    if not _is_transient:
                        break
                    if _attempt < 2:
                        time.sleep(0.025 + random.random() * 0.050)
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
                     vol_regime, data_provenance, orderbook_levels_json,
                     config_snapshot_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (ticker, event_ticker, asset, rejection_reason, now,
                  z_score, spot_price, threshold, volatility, market_price,
                  seconds_to_close, calibrated_prob, raw_prob, "pending",
                  egarch_sigma, egarch_blend_sigma, egarch_blend_weight, mz_r_squared,
                  shadow_tv_blend_rv, mz_shadow_sigmoid_w, mz_baseline_qlike, mz_qlike,
                  counterfactual, product_type,
                  oft_prob_adjustment, oft_imbalance_ratio, oft_n_snapshots,
                  no_ask_cents,
                  sigma_winsorize, hour_sin, hour_cos, prob_breakeven_gap,
                  vol_regime, data_provenance, orderbook_levels_json,
                  config_snapshot_id))
            # B3-fu1 cross-thread commit-race tolerance: if
            # settlement_tracker's mark_rejection_settled issues a
            # commit() on the shared conn between our BEGIN IMMEDIATE
            # and our COMMIT here, SQLite reports "cannot commit - no
            # transaction is active" — the racer's commit captured
            # our INSERT, so the row is preserved. Swallow that
            # specific signature; propagate other errors to the outer
            # broad-telemetry catch below.
            try:
                if _began_explicitly:
                    self.conn.execute("COMMIT")
                else:
                    self.conn.commit()
            except sqlite3.OperationalError as _ce:
                if "no transaction is active" not in str(_ce).lower():
                    raise
        except Exception as e:
            try:
                self.conn.rollback()
            except Exception:
                pass
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
                f"insert_rejection failed: {e} "
                f"ticker={ticker!r} "
                f"begin_immediate={_diag_begin!r} "
                f"begin_immediate_duration_ms={_diag_be_dur} "
                f"begin_immediate_retries={_be_retries} "
                f"thread={_diag_thread!r} "
                f"in_tx={_diag_in_tx!s} "
                f"active_writers={_diag_active!r} "
                f"recent_writes={_diag_recent!r}",
                exc_info=not _is_known_db_contention(e),
            )

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
        try:
            self.conn.commit()
        except sqlite3.OperationalError as _ce:
            # B3-fu1 cross-thread commit-race tolerance (2026-05-18):
            # `state.conn` is shared with MainThread; if MainThread
            # commits between our UPDATE and our commit, our commit
            # finds no active tx and raises "cannot commit - no
            # transaction is active". The race window is narrow —
            # between Python's autocommit check inside conn.commit()
            # and SQLite's actual COMMIT step — so synthetic single-
            # threaded repros do NOT trigger it; the production
            # traceback at journalctl 2026-05-18 12:03:56 UTC
            # confirms `self.conn.commit()` is the raising frame.
            # Data is USUALLY preserved (the racer's commit captured
            # our UPDATE) — but a cross-thread racer rollback() on the
            # shared state.conn would silently lose our UPDATE. The
            # only candidate cross-thread rollback site for this
            # method (which itself runs on settlement_tracker thread)
            # is MainThread's rollback at bot/state.py:2939 inside
            # `insert_evaluated_opportunity`'s outer try/except —
            # rare, only fires when that path itself errors out.
            # (`bot/settlement.py:1015` is sequentially same-thread
            # — _poll_rejections completes BEFORE
            # _poll_evaluated_opportunities in the worker function
            # — so it cannot race with us.) Tolerated because (a)
            # the common case preserves the row via the racer's commit
            # and (b) the rare racer-rollback loss is single-row-
            # bounded: the settlement-status flag is rebuildable on
            # the next pending-rejection sweep after the in-memory
            # `_settled_rejection_tickers` dedup set resets at process
            # restart, vs. an exception-storm at this site (no
            # surrounding try/except in state.py — propagates to
            # settlement.py:715-717 outer catch). Re-raise anything
            # else (disk-full, corruption) so the caller-side WARNING
            # still fires.
            if "no transaction is active" not in str(_ce).lower():
                raise

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
                                     bnb_spot_at_decision: Optional[float] = None,
                                     ada_spot_at_decision: Optional[float] = None,
                                     bch_spot_at_decision: Optional[float] = None,
                                     near_spot_at_decision: Optional[float] = None,
                                     zec_spot_at_decision: Optional[float] = None,
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
                                     bot_state_snapshot_json: Optional[str] = None,
                                     # Per-decision config snapshot advisory-pointer
                                     # (ticket 86b9zkp8p, 2026-05-17). NULLABLE
                                     # for backward compat. Production scanner
                                     # call sites pass
                                     # `config_snapshot_id=self._ml.config_snapshot_id`.
                                     # See bot/helpers/config_snapshot.py +
                                     # bot/CLAUDE.md "config_snapshot_id
                                     # schema chain".
                                     config_snapshot_id: Optional[int] = None,
                                     # TM half-Kelly cal_mlp shadow (Sim C,
                                     # ticket 86ba0v7fc, 2026-05-19). Logged
                                     # counterfactual Kelly contracts + the
                                     # constraint that bound the size. SHADOW-
                                     # ONLY — NEVER consumed by production
                                     # sizing. bound_hit ∈ {kelly, abs_loss,
                                     # asset_cap, null_prob, raw_fallback}. See
                                     # bot/helpers/tm_sweep.py +
                                     # kb/decisions/tm-half-kelly-shadow-plan.md.
                                     tm_shadow_kelly_ct: Optional[int] = None,
                                     tm_shadow_kelly_prob: Optional[float] = None,
                                     tm_shadow_kelly_fraction: Optional[float] = None,
                                     tm_shadow_kelly_bound_hit: Optional[str] = None,
                                     # Bit S.1 (86ba1wrcg, 2026-05-21): Coinbase
                                     # WS spot staleness at evaluation. Seconds
                                     # since the last WS tick populated
                                     # CoinbaseFeed._prices[asset], measured via
                                     # `time.monotonic()`. Auto-filled from
                                     # `_scan_spot_staleness_cache[asset]` so
                                     # every Coinbase-path insert site
                                     # (candidate / decided_contract* /
                                     # insufficient_edge / price_out_of_range /
                                     # silent_spot_none / all 115+ sites) gets
                                     # a value without per-call threading.
                                     # Explicit caller kwarg wins.
                                     # NULL only for non-Coinbase scan paths
                                     # (SPX/weather/sports use other engines;
                                     # hourly shares the Coinbase branch with
                                     # 15M so hourly rows also carry staleness),
                                     # warmup (cache slot popped), or backfill
                                     # /test callers that supply neither input.
                                     spot_staleness_seconds: Optional[float] = None,
                                     # B2b-1 (86ba64h2w, 2026-05-28): multi-venue
                                     # synthetic RTI — SHADOW by default. Auto-filled
                                     # from `_scan_rti_cache[asset]` so every
                                     # Coinbase scan-path insert carries the
                                     # decision-time synthetic without per-call
                                     # threading. Explicit caller kwargs win.
                                     # Read by a decision path only for
                                     # SYNTHETIC_RTI_LIVE_ASSETS (RTI-6; default
                                     # empty ⇒ shadow for all).
                                     rti_synthetic: Optional[float] = None,
                                     rti_constituent_count: Optional[int] = None,
                                     rti_confidence: Optional[float] = None,
                                     # Bit V.3 (2026-06-12): vol-honesty
                                     # pair. tape_rv300 = independent
                                     # trailing-300s tape RV at decision
                                     # time; raw_blended_rv = the RAW
                                     # engine estimate BEFORE the V.1
                                     # max() selection. Auto-filled
                                     # ATOMICALLY from the single
                                     # `_scan_vol_pair_cache` (V.3-R1-M1
                                     # — same-tick pair, both-or-
                                     # neither, 15M-only). Explicit
                                     # caller kwargs win.
                                     tape_rv300: Optional[float] = None,
                                     raw_blended_rv: Optional[float] = None):
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
        # Bit S.1 (86ba1wrcg, 2026-05-21): auto-fill spot staleness from
        # per-asset scanner cache. Mirrors the _scan_cx_gap_cache pattern
        # above — populated once per tick at the Coinbase scan-path site
        # in bot/scanner/__init__.py (covers `_pt in (None, "15m",
        # "hourly")`); missing entry → NULL (asset wasn't in the
        # Coinbase scan path this tick; SPX/weather/sports use other
        # engines and have no staleness equivalent here). Explicit kwarg
        # from a caller wins.
        if spot_staleness_seconds is None and asset is not None:
            spot_staleness_seconds = self._scan_spot_staleness_cache.get(asset)
        # B2b-1 (86ba64h2w, 2026-05-28): auto-fill the multi-venue synthetic
        # RTI from the per-asset scanner cache. Mirrors _scan_cx_gap_cache /
        # _scan_spot_staleness_cache above — staged once per tick at the
        # Coinbase scan-path site; missing entry → NULL (asset not in the
        # Coinbase scan path this tick, feed disabled, or sampler stale).
        # Per-field guard so an explicit caller kwarg (e.g. a backfill) for
        # any one field still wins. SHADOW-ONLY.
        if (rti_synthetic is None and rti_constituent_count is None
                and rti_confidence is None and asset is not None):
            _rti = self._scan_rti_cache.get(asset)
            if _rti is not None:
                rti_synthetic, rti_constituent_count, rti_confidence = _rti
        # Bit V.3 + V.3-R1-M1 fix round (2026-06-12): auto-fill the
        # vol-honesty pair ATOMICALLY from the single per-asset pair cache
        # (mirrors _scan_spot_staleness_cache above for the no-threading
        # contract; the pair shape is the durable fix for the stale-leak
        # class). The cache only ever holds COMPLETE same-tick pairs —
        # the scanner writes it solely at the 15M _strategy_vol seam and
        # pops it on every rv300-None vol pass + in the loss-cooldown
        # branch — so a bare .get is safe: missing → both NULL (honest).
        # PERSIST-BOTH-OR-NEITHER: (a) the only consumer — the soak's
        # honesty ratio raw/tape — is undefined unless BOTH halves exist
        # from the SAME tick, so the fill triggers only when the caller
        # supplied NEITHER kwarg (a caller passing exactly one side gets
        # no cache fill for the other — that would fabricate a mixed-tick
        # pair); (b) the fill is gated to product_type in (None, '15m'):
        # hourly shares the Coinbase scan branch but never seams the
        # pair, so pre-fix a frozen raw could ride a fresh tape onto
        # hourly rows — post-fix hourly/SPX/weather rows are both-NULL
        # by construction. Residual (documented, accepted): the rare
        # post-warmup silent_vol_none branch can persist a ≤1-tick-old
        # (but internally same-tick) pair; the /live-small soak query F
        # excludes filter_stage LIKE 'silent_%' as defense-in-depth.
        # Pairing invariant on auto-filled rows: raw_blended_rv non-NULL
        # ⇔ tape_rv300 non-NULL. Explicit kwargs win.
        if (tape_rv300 is None and raw_blended_rv is None
                and asset is not None
                and product_type in (None, "15m")):
            _vol_pair = self._scan_vol_pair_cache.get(asset)
            if _vol_pair is not None:
                raw_blended_rv, tape_rv300 = _vol_pair
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
                # BNB T1.5 followup (2026-05-17, ticket 86b9zn5pq):
                # producer at bot/scanner/__init__.py:1011 is ASSETS-driven
                # and emits 7 keys post-BNB-T1. Pre-fix the bnb key was
                # silently dropped on the floor here.
                if bnb_spot_at_decision is None:
                    bnb_spot_at_decision = _ext.get("bnb_spot_at_decision")
                if ada_spot_at_decision is None:
                    ada_spot_at_decision = _ext.get("ada_spot_at_decision")
                if bch_spot_at_decision is None:
                    bch_spot_at_decision = _ext.get("bch_spot_at_decision")
                if near_spot_at_decision is None:
                    near_spot_at_decision = _ext.get("near_spot_at_decision")
                if zec_spot_at_decision is None:
                    zec_spot_at_decision = _ext.get("zec_spot_at_decision")
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
        # 2026-05-22 (responding to 2026-05-21 Tick error escalation):
        # broadened to `sqlite3.DatabaseError` (parent of OperationalError)
        # because Python's sqlite3 module surfaces stale-cursor-class
        # raises ("another row available", "no more rows available")
        # at the bare `DatabaseError` class level, which the narrow
        # `OperationalError` catch did NOT match. Past-48h production
        # histogram on the VPS journal: 21× `DatabaseError: another row
        # available` + 2× `DatabaseError: no more rows available`
        # escaped the retry block across the four StateManager
        # hot-path sites that share this conn. Broadening lets the
        # transient/non-transient string-match dispatch route them
        # uniformly; behavior for `OperationalError` cases is
        # unchanged (subclass still matches). Lockstep applied to
        # insert_rejection + insert_bot_order BEGIN retry loops in
        # the same Bit; the commit-race "no transaction is active"
        # except sites stay narrow (different class, only OperationalError
        # observed). See bot/CLAUDE.md SQLite section for the full
        # 4-site coverage chain.
        for _attempt in range(3):
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                _lock_wait_ms = (time.perf_counter() - _t0_lock) * 1000.0
                _be_duration_ms = _lock_wait_ms
                _began_explicitly = True
                _be_retries = _attempt
                break
            except sqlite3.DatabaseError as _be_err:
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
                     bnb_spot_at_decision,
                     ada_spot_at_decision, bch_spot_at_decision,
                     near_spot_at_decision, zec_spot_at_decision,
                     okx_funding_rate_at_decision, deribit_funding_rate_at_decision,
                     data_provenance, bot_state_snapshot_json,
                     config_snapshot_id,
                     tm_shadow_kelly_ct, tm_shadow_kelly_prob,
                     tm_shadow_kelly_fraction, tm_shadow_kelly_bound_hit,
                     spot_staleness_seconds,
                     rti_synthetic, rti_constituent_count, rti_confidence,
                     tape_rv300, raw_blended_rv)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    bnb_spot_at_decision=excluded.bnb_spot_at_decision,
                    ada_spot_at_decision=excluded.ada_spot_at_decision,
                    bch_spot_at_decision=excluded.bch_spot_at_decision,
                    near_spot_at_decision=excluded.near_spot_at_decision,
                    zec_spot_at_decision=excluded.zec_spot_at_decision,
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
                    bot_state_snapshot_json=excluded.bot_state_snapshot_json,
                    -- Per-decision config snapshot advisory-pointer (ticket 86b9zkp8p,
                    -- 2026-05-17). COALESCE so the FIRST snapshot stamp on
                    -- a row survives subsequent UPSERTs (e.g. mid-day
                    -- mutation that re-captures via a future Phase-2
                    -- followup — the original decision-time snapshot is
                    -- what replay must consult, not whichever snapshot
                    -- happens to be current at the next tick). Mirrors the
                    -- data_provenance COALESCE pattern immediately above
                    -- (round-2 review of the H-2 Phase G-6 ship).
                    config_snapshot_id=COALESCE(evaluated_opportunities.config_snapshot_id, excluded.config_snapshot_id),
                    -- TM half-Kelly cal_mlp shadow (Sim C, ticket 86ba0v7fc,
                    -- 2026-05-19). COALESCE so the FIRST shadow stamp on a
                    -- re-emitted row survives subsequent UPSERTs — Sim C
                    -- analysis joins on decision-time prob signal, not on
                    -- whichever cal_mlp prediction completes last. Mirrors
                    -- the config_snapshot_id COALESCE pattern immediately
                    -- above. The 4 keys ship as a lockstep group; partial
                    -- updates (e.g. only ct present) preserve their other
                    -- non-NULL siblings via COALESCE per-column.
                    tm_shadow_kelly_ct=COALESCE(evaluated_opportunities.tm_shadow_kelly_ct, excluded.tm_shadow_kelly_ct),
                    tm_shadow_kelly_prob=COALESCE(evaluated_opportunities.tm_shadow_kelly_prob, excluded.tm_shadow_kelly_prob),
                    tm_shadow_kelly_fraction=COALESCE(evaluated_opportunities.tm_shadow_kelly_fraction, excluded.tm_shadow_kelly_fraction),
                    tm_shadow_kelly_bound_hit=COALESCE(evaluated_opportunities.tm_shadow_kelly_bound_hit, excluded.tm_shadow_kelly_bound_hit),
                    -- Bit S.1 (86ba1wrcg, 2026-05-21): COALESCE preserves
                    -- the FIRST staleness reading. The scan tick that
                    -- emits the candidate row captures the freshest
                    -- decision-time value; any subsequent rejection/
                    -- shadow UPSERT for the same (ticker, filter_stage,
                    -- side) tuple should NOT overwrite with a later
                    -- read. (Mirrors config_snapshot_id pattern.)
                    spot_staleness_seconds=COALESCE(evaluated_opportunities.spot_staleness_seconds, excluded.spot_staleness_seconds),
                    -- B2b-1 (86ba64h2w): COALESCE preserves the FIRST synthetic
                    -- reading for a (ticker, filter_stage, side) tuple — the
                    -- candidate-emitting tick captures the freshest
                    -- decision-time value; later rejection/shadow UPSERTs must
                    -- not overwrite it (mirrors spot_staleness_seconds).
                    rti_synthetic=COALESCE(evaluated_opportunities.rti_synthetic, excluded.rti_synthetic),
                    rti_constituent_count=COALESCE(evaluated_opportunities.rti_constituent_count, excluded.rti_constituent_count),
                    rti_confidence=COALESCE(evaluated_opportunities.rti_confidence, excluded.rti_confidence),
                    -- Bit V.3 (2026-06-12): COALESCE preserves the FIRST
                    -- vol-honesty reading for a (ticker, filter_stage,
                    -- side) tuple — the candidate-emitting tick captures
                    -- the decision-time estimate pair; later rejection/
                    -- shadow UPSERTs must not overwrite it (mirrors
                    -- spot_staleness_seconds / rti_* immediately above).
                    tape_rv300=COALESCE(evaluated_opportunities.tape_rv300, excluded.tape_rv300),
                    raw_blended_rv=COALESCE(evaluated_opportunities.raw_blended_rv, excluded.raw_blended_rv)
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
                  bnb_spot_at_decision,
                  ada_spot_at_decision, bch_spot_at_decision,
                  near_spot_at_decision, zec_spot_at_decision,
                  okx_funding_rate_at_decision, deribit_funding_rate_at_decision,
                  data_provenance, bot_state_snapshot_json,
                  config_snapshot_id,
                  tm_shadow_kelly_ct, tm_shadow_kelly_prob,
                  tm_shadow_kelly_fraction, tm_shadow_kelly_bound_hit,
                  spot_staleness_seconds,
                  rti_synthetic, rti_constituent_count, rti_confidence,
                  tape_rv300, raw_blended_rv))
            # Phase H-2: explicit COMMIT only if we BEGAN IMMEDIATE explicitly.
            # Otherwise fall back to the implicit-tx commit() that paired
            # with the implicit BEGIN that fired on the INSERT above.
            #
            # B3-fu1 cross-thread commit-race tolerance (2026-05-18):
            # `state.conn` is shared with the settlement_tracker daemon
            # thread (bot/settlement.py:212). If settlement_tracker
            # issues `conn.commit()` between our BEGIN IMMEDIATE/INSERT
            # and the COMMIT here, it commits our still-open tx for
            # us — our INSERT is usually captured and persisted — and
            # SQLite then reports "cannot commit - no transaction is
            # active" on our COMMIT. Swallow that specific error;
            # re-raise any other OperationalError (disk-full,
            # corruption, etc.) so the outer try/except still WARNs
            # and rolls back. Data is USUALLY preserved (racer's
            # commit captured our INSERT before our COMMIT fired);
            # a racer rollback() at bot/settlement.py:1015 — rare,
            # only fires inside the chunked-batch commit-failure
            # handler — would silently lose our INSERT row. That row
            # is the load-bearing candidate-audit / cohort-rollup
            # source (filter_stage='candidate' joins downstream), but
            # the single-row-bounded rare loss is preferable to an
            # exception-storm at 10-20 inserts/sec masking real
            # failures from the outer-try WARNING channel. See
            # tests/integration/test_cannot_commit_no_transaction_regression.py
            # for the persistence pin.
            try:
                if _began_explicitly:
                    self.conn.execute("COMMIT")
                else:
                    self.conn.commit()
            except sqlite3.OperationalError as _ce:
                if "no transaction is active" not in str(_ce).lower():
                    raise
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
                exc_info=not _is_known_db_contention(e),
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
        """Insert a new bot-initiated order with status='pending'.

        Order-ledger crash-safety contract (ticket 86ba0jb1g,
        2026-05-19): if the DB persist fails, place_order MUST NOT
        fire — pinned by tests/integration/test_execution.py
        ::test_maker_persists_to_db_before_api. Retry transient
        SQLITE_BUSY up to 3 times via explicit BEGIN IMMEDIATE
        (same intra-process contention pattern as
        insert_evaluated_opportunity, bot/state.py:2563-2594
        May-9 instrumentation). Same 3-retry × 25-75ms jittered
        backoff (worst-case ~225ms per call) as the sister site
        for the same SCAN_BODY_SLOW 1.5s budget reasons (see
        :2565-2570). On retry exhaustion, RE-RAISE — unlike
        insert_evaluated_opportunity which swallows (telemetry
        rows: losing data > tick crash; order rows: tick crash >
        lost record). Stale-tx ("cannot start a transaction
        within a transaction") ALSO re-raises immediately for
        the same crash-safety reason (sister site falls through
        to the implicit-tx INSERT; we cannot — losing the order
        row is unacceptable).

        B3-fu1 commit-race swallow: settlement_tracker writes
        to pending_orders via cleanup_expired_resting_orders
        (bot/settlement.py:201) on shared state.conn; if its
        commit lands between our BEGIN IMMEDIATE and COMMIT,
        our COMMIT finds no active tx but our INSERT was
        captured by the racer's commit. Swallow ONLY that
        signature; propagate everything else. Documented
        residual hazard (mirrors sister at :2917-2926): a rare
        cross-thread settlement_tracker.conn.rollback() at
        bot/settlement.py:1015 — only fires inside its
        chunked-batch commit-failure handler — would silently
        lose our INSERT row. Single-row-bounded rare loss is
        preferable to an exception storm masking real failures;
        accept the same tradeoff the sister site made.

        On either raise path (retry exhaustion + non-race COMMIT
        failure) we emit a structured WARNING with retry count,
        BEGIN duration, and recent_writes() ring-buffer evidence
        — mirrors sister diagnostic at :2974-2984 so the
        operator can correlate order-ledger failures to the
        broader contention storm class.
        """
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        _be_err_repr: Optional[str] = None
        _be_duration_ms: Optional[float] = None
        _be_retries: int = 0
        _t0_lock = time.perf_counter()
        # 2026-05-22 R1: parent-class catch — see lockstep note in
        # insert_evaluated_opportunity retry loop. Crash-safety divergence
        # vs telemetry siblings preserved: non-transient OR retry-exhausted
        # paths still `raise` (data-integrity contract — losing an order
        # row is worse than crashing the tick).
        for _attempt in range(3):
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                _be_duration_ms = (time.perf_counter() - _t0_lock) * 1000.0
                _be_retries = _attempt
                break
            except sqlite3.DatabaseError as _be_err:
                _be_duration_ms = (time.perf_counter() - _t0_lock) * 1000.0
                _be_err_repr = f"{type(_be_err).__name__}: {_be_err}"
                _be_retries = _attempt + 1
                _err_msg = str(_be_err).lower()
                _is_transient = ("locked" in _err_msg) or ("busy" in _err_msg)
                if not _is_transient or _attempt == 2:
                    self._log_insert_bot_order_failure(
                        client_order_id, ticker, _be_err,
                        _be_err_repr, _be_duration_ms, _be_retries,
                        phase="begin_immediate")
                    raise
                time.sleep(0.025 + random.random() * 0.050)
        # recorded_fill_count starts at an EXPLICIT 0 (Bit L-1 R4-M2):
        # NULL is reserved for legacy pre-R4 rows so longshot boot
        # reconciliation can tell "this order recorded nothing yet" (0)
        # from "this row predates the counter" (NULL -> aggregate seed).
        self.conn.execute("""
            INSERT INTO pending_orders (order_id, client_order_id, ticker,
                event_ticker, asset, side, action, count, price_cents,
                status, created_at, updated_at, recorded_fill_count)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)
        """, (client_order_id, client_order_id, ticker,
              event_ticker, asset, side, "buy", count, price_cents,
              "pending", now, now))
        try:
            self.conn.execute("COMMIT")
        except sqlite3.OperationalError as _ce:
            if "no transaction is active" not in str(_ce).lower():
                self._log_insert_bot_order_failure(
                    client_order_id, ticker, _ce,
                    _be_err_repr, _be_duration_ms, _be_retries,
                    phase="commit")
                raise

    def _log_insert_bot_order_failure(
            self, client_order_id: str, ticker: str,
            err: BaseException, be_err_repr: Optional[str],
            be_duration_ms: Optional[float], be_retries: int,
            phase: str) -> None:
        """Diagnostic envelope for insert_bot_order raise paths
        (ticket 86ba0jb1g, 2026-05-19). Mirrors the sister
        insert_evaluated_opportunity envelope at state.py:2974-2984
        so operators can correlate order-ledger contention to the
        broader storm. `phase` distinguishes the BEGIN-retry-
        exhaustion path from the COMMIT non-race re-raise path.
        """
        try:
            _diag_active = [
                f"{tok.split('#', 1)[0]}/{kind}/{th}"
                f"@{(time.time() - started) * 1000:.0f}ms"
                for (tok, started, kind, th) in snapshot_active()
            ]
        except Exception:
            _diag_active = ["<snapshot_failed>"]
        try:
            _now = time.time()
            _diag_recent = [
                f"{name}/{kind}/{th}"
                f"@{(_now - finished_ts) * 1000:.0f}ms_ago/{dur:.1f}ms"
                for (name, kind, dur, finished_ts, th) in recent_writes(2.0)
            ]
        except Exception:
            _diag_recent = ["<recent_failed>"]
        _diag_be_dur = (
            f"{be_duration_ms:.1f}" if be_duration_ms is not None else "?"
        )
        logging.warning(
            f"insert_bot_order failed: {err} "
            f"phase={phase!r} "
            f"client_order_id={client_order_id!r} "
            f"ticker={ticker!r} "
            f"begin_immediate={be_err_repr!r} "
            f"begin_immediate_duration_ms={_diag_be_dur} "
            f"begin_immediate_retries={be_retries} "
            f"thread={threading.current_thread().name!r} "
            f"active_writers={_diag_active!r} "
            f"recent_writes={_diag_recent!r}",
            exc_info=not _is_known_db_contention(err),
        )

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
            "SELECT order_id, client_order_id, ticker FROM pending_orders "
            "WHERE status='resting'"
        ).fetchall()
        cleaned = 0
        for row in rows:
            # R4-MN1 (generalized at Bit T-1): engine-owned (ls-/tw-) rows
            # — same carve-out as _reconcile_orders (R3-M1). The settlement
            # daemon calls this method concurrently (bot/settlement.py) and
            # flipping a past-close ls- row to 'expired' would hide it from
            # LongshotEngine._boot_reconcile_orphans step 2, which finds
            # unreconciled orders via status='resting'; an 'expired' tw-
            # row would likewise hide from TwaplockEngine's boot sweep
            # (status IN ('pending','resting')). The engines' own paths
            # mark their rows off 'resting'.
            if (row["client_order_id"] or "").startswith(
                    ENGINE_OWNED_CLIENT_OID_PREFIXES):
                continue
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
