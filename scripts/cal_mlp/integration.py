"""
P2 Phase 7: bot.py integration module.

Encapsulates all cal_mlp deploy logic in ONE module so bot.py edits are
minimal (import + 4 call sites). Per CLAUDE.md, bot.py is sacred —
small, targeted insertions only.

Public surface (what bot.py imports):
    parity_assert(bot_globals, conn) → 'passed'/'failed' (logs to bot_startup_log)
    sizing_parity_assert(bot_globals, conn) → 'passed'/'failed' (UPDATEs the
        same row parity_assert seeded; MUST run after parity_assert)
    migrate_schema(conn) → list of added columns
    CalMLPPredictor(asset, project_root) → lazy-loaded per-asset predictor.
        Call .warmup() at bot startup to avoid first-prediction latency spike
        (~1-2s wall clock per asset for model load).
    annotate_evaluation_kwargs(kwargs, raw_prob, ticker, side, entry_price_cents,
                                row_features, predictor) → mutates kwargs in place
    CalMLPError, CalMLPParityError, CalMLPSchemaError

Locked enum:
    SKIPPED_REASONS — cal_mlp_skipped_reason values

Phase 7 contract: invoked from bot.py at:
- startup (after _create_tables): parity_assert + sizing_parity_assert + migrate_schema
- per-evaluation (15M scan path): annotate_evaluation_kwargs

CLAUDE.md compliance: no signal handlers, no logging.basicConfig (we use a
named child logger), no thread spawn, no sqlite write outside migrate_schema +
bot_startup_log inserts (which the caller's connection commits).
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger('cal_mlp')

# R-p7-r2#M1: one-time module-level sys.path setup so we can import
# cal_mlp/* without per-call mutation. cal_mlp/* names don't shadow any
# bot.py top-level names (verified by grep), so the addition is safe.
_CAL_MLP_DIR = Path(__file__).resolve().parent
if str(_CAL_MLP_DIR) not in sys.path:
    sys.path.insert(0, str(_CAL_MLP_DIR))


def _build_missing_indicator_inverse() -> dict:
    """R-p7-cleanroom-r6#H1 + LOW#2: compute inverse map ONCE at module
    import time so the collision check can't fire from inside predict()
    (where it would be demoted from hard-fail to a per-call soft skip via
    the outer Exception wrapper). Programming-class bugs in features.py
    surface loudly at import."""
    from features import MISSING_INDICATOR_SOURCE_MAP
    inv = {v: k for k, v in MISSING_INDICATOR_SOURCE_MAP.items()}
    if len(inv) != len(MISSING_INDICATOR_SOURCE_MAP):
        # Module-load-time hard fail: a source column maps to multiple
        # indicators. The forward map is dict (unique keys), so this can
        # only happen if two distinct indicator names point at the same
        # source column — a features.py drift.
        raise RuntimeError(
            "MISSING_INDICATOR_SOURCE_MAP inverse has fewer keys than the "
            "forward map — a source column maps to multiple indicators. "
            "Fix features.MISSING_INDICATOR_SOURCE_MAP before import."
        )
    return inv


_MISSING_INDICATOR_SRC_TO_IND = _build_missing_indicator_inverse()


def _build_identity_no_zscore_set() -> frozenset:
    """R-p7-deploy-r5#L2: hoist the identity_no_zscore lookup out of the
    per-call _predict_inner path. Used by the safety net that decides
    whether a missing CONT col can be mean-imputed (companion or analytical)
    or must raise missing_features (no signal to convey "missing")."""
    from features import CONT_FEATURE_TRANSFORMS
    return frozenset(c for c, t in CONT_FEATURE_TRANSFORMS.items()
                     if t == 'identity_no_zscore')


_IDENTITY_NO_Z = _build_identity_no_zscore_set()


# ---------------------------------------------------------------------------
# Torch thread constraint (R-p7-deploy-r7)
#
# Pin torch + OMP/MKL/OpenBLAS to 1 thread each, at module-import time.
#
# Why: the bot's scan thread shares CPU with WS feeds, calibration workers,
# supabase_sync, sports thread, etc. By default torch uses num_cpus intra-op
# threads (= 2 on the VPS), so a single predict() can grab both cores and
# starve the scan thread via GIL contention.
#
# Empirical benchmark on prod VPS (2 vCPU / 2 GB):
#     default 2 threads, no contention      → 22ms median
#     1 thread,           no contention     → 15ms median
#     default 2 threads, 3 busy threads     → 992ms median   ← outage mode
#     1 thread,           3 busy threads    → 372ms median
#
# IMPORTANT: `torch.set_num_interop_threads()` raises RuntimeError if torch's
# parallel pool has already initialized — which happens at *first parallel
# op* in the process. To beat that race we must run BEFORE any other module
# imports torch (e.g. train.py at integration:_load() does
# `torch.use_deterministic_algorithms` which can lock the dispatcher).
# Hence: module-import time, before any sub-imports.
#
# Belt-and-suspenders: OMP/MKL/OPENBLAS env vars are also set in the systemd
# EnvironmentFile (VPS ~/kalshi-bot-repo/.env) so the OpenMP/MKL backends are
# constrained at first `import torch` regardless of who imports it first.
# ---------------------------------------------------------------------------

# Single source of truth for the OMP/MKL setdefaults: bot/_thread_env.py.
# bot/_impl.py imports bot._thread_env as its first non-stdlib import (BEFORE numpy);
# integration.py imports it here as belt-and-suspenders for tests/scripts that bypass
# bot/_impl.py. Either path guarantees the env vars are set before any C extension that
# reads them.
import bot._thread_env  # noqa: F401 — side-effect import: sets OMP_NUM_THREADS=1 etc.

# Sprint A Bit 1b (86b9veppa) — canonical derived-feature helper. Replaces
# the inline `buf_pct / sigma_denom` + `cb_prob - market_price/100` formulas
# previously duplicated in `should_block_tm96`. Lock-step with the same
# helper called by bot/state.py:1713 + 2010 (pre-DB-write). Safe at
# module-top: derived_features.py imports only stdlib (math + typing).
from bot.helpers.derived_features import compute_derived_features  # noqa: E402

_TORCH_THREADS_INTRA = None       # post-call observed value (or None on failure)
_TORCH_THREADS_INTEROP = None
_TORCH_THREADS_INTEROP_RACE = False  # True if interop call lost race to default-2


def _constrain_torch_threads_at_import() -> None:
    """Run at module import. Imports torch eagerly and pins thread counts.

    DO NOT call importlib.reload(integration) at runtime: torch's interop
    pool is initialized after the first call here, so a second invocation
    will set _TORCH_THREADS_INTEROP_RACE=True spuriously. Hot-reloading the
    bundle should be done via re-instantiating CalMLPPredictor (which only
    re-loads the model files), not by reloading this module.
    """
    global _TORCH_THREADS_INTRA, _TORCH_THREADS_INTEROP, _TORCH_THREADS_INTEROP_RACE
    try:
        import torch
    except ImportError:
        # torch not installed (e.g. during static analysis / lint): skip
        # silently. The bot can't run without torch anyway, so this only
        # matters for tests that don't touch torch.
        return
    # set_num_interop_threads MUST be called before any parallel work, OR it
    # raises RuntimeError. Call it FIRST so we get the explicit signal.
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError as e:
        # Pool already initialized — interop stuck at default. Surface as a
        # WARNING (not info): under contention this re-introduces the issue.
        _TORCH_THREADS_INTEROP_RACE = True
        logger.warning(
            "[CALMLP_THREADS] torch.set_num_interop_threads(1) lost race "
            "(another module imported torch first): %s — interop=%d",
            e, torch.get_num_interop_threads(),
        )
    # set_num_threads (intra-op) can be called any time and is the bigger
    # lever (matrix mult uses these). Failure is fatal — we don't silently
    # flip the constrained flag if this fails.
    torch.set_num_threads(1)
    _TORCH_THREADS_INTRA = torch.get_num_threads()
    _TORCH_THREADS_INTEROP = torch.get_num_interop_threads()
    # R-p7-deploy-r7-r3: NOT setting torch.set_grad_enabled(False) globally;
    # predict() uses `with torch.no_grad():` locally, and a process-wide flip
    # is a footgun for any future audit/REPL code path that imports
    # integration.py and expects autograd. (Round-3 review #M5.)
    # R-p7-deploy-r7-r3: NOT calling torch.set_flush_denormal(True); our
    # post-clip logit values are bounded in [-13.8, +13.8] (RAW_PROB_CLIP_EPS
    # = 1e-6), nowhere near float32 denormal range (~1.18e-38). The flag
    # would be perf-neutral for our pipeline, and the comment had a wrong
    # rationale claim. (Round-3 review #L6.)
    if _TORCH_THREADS_INTRA != 1:
        # set_num_threads(1) didn't take. Loud.
        logger.error(
            "[CALMLP_THREADS] torch threads NOT constrained — get_num_threads()=%d "
            "after set_num_threads(1). Cal_mlp will likely cause scan "
            "loop slowdowns under contention.", _TORCH_THREADS_INTRA,
        )
    else:
        logger.info(
            "[CALMLP_THREADS] constrained: intra=%d interop=%d "
            "(interop_race_lost=%s)",
            _TORCH_THREADS_INTRA, _TORCH_THREADS_INTEROP,
            _TORCH_THREADS_INTEROP_RACE,
        )


_constrain_torch_threads_at_import()


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

class CalMLPError(RuntimeError):
    """Soft error — logged + raw_prob fallback at runtime."""
    def __init__(self, code: str, detail: str = ''):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class CalMLPParityError(RuntimeError):
    """Hard error at startup; deploy gate."""


class CalMLPSchemaError(RuntimeError):
    """Hard error at startup; bundle/schema mismatch."""


# ---------------------------------------------------------------------------
# Locked enum (Phase 7 R1#C11)
# ---------------------------------------------------------------------------

SKIPPED_REASONS = frozenset({
    'no_current',           # CURRENT pointer absent for this asset
    'phase_mismatch',       # bundle phase != 5
    'sha_chain_fail',       # bundle_sha chain verification failed
    'marker_drift',         # member marker cfg_fp != bundle cfg_fp
    'load_failed',          # generic load error (file IO, JSON, torch.load)
    'predict_oom',          # MemoryError during predict
    'predict_runtime',      # other RuntimeError during predict
    'env_disabled',         # CALMLP_ENABLED=0
    'raw_prob_null',        # ProbabilityEngine returned None
    'market_blend_w_drift', # bundle vs market_config divergence
    'no_predictor',         # no predictor cached for asset (impl-added; spec amended)
    'missing_features',     # row_features dict missing required keys (impl-added)
})


# ---------------------------------------------------------------------------
# Constants imported from cal_mlp/* — single source of truth
# ---------------------------------------------------------------------------

def _import_cal_mlp_constants() -> dict:
    """Import cal_mlp constants under aliases (R-p7-r2#C2 to prevent shadowing).

    R-p7-r2#M1: relies on module-level sys.path setup (no per-call mutation)."""
    from features import (
        ASSET_FLOORS, GLOBAL_MIN_ENTRY_PRICE, RAW_PROB_CLIP_EPS,
        SETTLEMENT_WHITELIST, PRICE_BIN_CUTOFFS, STC_BIN_CUTOFFS,
    )
    # R-p7-deploy-r3: all parity-checked constants now live in sizing.py
    # (torch-free), decoupling parity_assert from sim_pnl's torch+pandas
    # import chain. This fixes test_db_signatures.py on local-only-no-pandas
    # environments and reduces parity_assert's import surface.
    from sizing import (
        SIZING_TIERS, ASSET_MAX_RISK_PER_TRADE, MAX_RISK_PER_TRADE,
        DRAWDOWN_HALF_THRESHOLD, DRAWDOWN_QUARTER_THRESHOLD,
        DRAWDOWN_HALT_THRESHOLD, DRAWDOWN_HALT_FLOOR,
        STC_SIZING_SCALER_KNEE, STC_SIZING_SCALER_ENABLED,
        MIN_EDGE_BY_PRICE_SCHEDULE, WEEKEND_EDGE_DISCOUNT,
        WEEKEND_EDGE_FLOOR, OVERNIGHT_EDGE_DISCOUNT,
        HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES,
        STC_EXTENDED_PER_ASSET_FLOOR, STC_EXTENDED_BUFFER_RESCUE,
    )
    return {
        'ASSET_FLOORS': ASSET_FLOORS, 'GLOBAL_MIN_ENTRY_PRICE': GLOBAL_MIN_ENTRY_PRICE,
        'RAW_PROB_CLIP_EPS': RAW_PROB_CLIP_EPS, 'SETTLEMENT_WHITELIST': SETTLEMENT_WHITELIST,
        'PRICE_BIN_CUTOFFS': PRICE_BIN_CUTOFFS, 'STC_BIN_CUTOFFS': STC_BIN_CUTOFFS,
        'SIZING_TIERS': SIZING_TIERS, 'ASSET_MAX_RISK_PER_TRADE': ASSET_MAX_RISK_PER_TRADE,
        'MAX_RISK_PER_TRADE': MAX_RISK_PER_TRADE,
        'DRAWDOWN_HALF_THRESHOLD': DRAWDOWN_HALF_THRESHOLD,
        'DRAWDOWN_QUARTER_THRESHOLD': DRAWDOWN_QUARTER_THRESHOLD,
        'DRAWDOWN_HALT_THRESHOLD': DRAWDOWN_HALT_THRESHOLD,
        'DRAWDOWN_HALT_FLOOR': DRAWDOWN_HALT_FLOOR,
        'STC_SIZING_SCALER_KNEE': STC_SIZING_SCALER_KNEE,
        'STC_SIZING_SCALER_ENABLED': STC_SIZING_SCALER_ENABLED,
        'MIN_EDGE_BY_PRICE_SCHEDULE': MIN_EDGE_BY_PRICE_SCHEDULE,
        'WEEKEND_EDGE_DISCOUNT': WEEKEND_EDGE_DISCOUNT,
        'WEEKEND_EDGE_FLOOR': WEEKEND_EDGE_FLOOR,
        'OVERNIGHT_EDGE_DISCOUNT': OVERNIGHT_EDGE_DISCOUNT,
        'HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES': HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES,
        'STC_EXTENDED_PER_ASSET_FLOOR': STC_EXTENDED_PER_ASSET_FLOOR,
        'STC_EXTENDED_BUFFER_RESCUE': STC_EXTENDED_BUFFER_RESCUE,
    }


# ---------------------------------------------------------------------------
# Schema migration (R-p7-r2#C13 — PRAGMA-based)
# ---------------------------------------------------------------------------

CAL_MLP_COLUMNS = [
    ('cal_mlp_p_mean',          'REAL'),
    ('cal_mlp_p_std',           'REAL'),
    ('cal_mlp_final_lo',        'REAL'),
    ('cal_mlp_final_hi',        'REAL'),
    ('cal_mlp_train_id',        'TEXT'),
    ('cal_mlp_skipped_reason',  'TEXT'),
    # R-p7-deploy-r8 async-predict: uuid generated at scan-tick, written at
    # INSERT, used by the async worker to UPDATE the SAME ROW after predict()
    # completes. UPSERT semantics on (ticker, filter_stage, side) make the row
    # PK 'id' stable across re-evaluations BUT the row contents are mutated
    # by newer ticks; using a uuid avoids the worker overwriting a newer
    # tick's audit data with stale predictions.
    ('cal_mlp_request_id',      'TEXT'),
]


def migrate_schema(conn) -> list:
    """Idempotent schema migration: PRAGMA table_info + explicit ADD COLUMN
    only for missing columns. Also creates bot_startup_log if absent.
    Returns list of newly-added column names.
    R-p7-r3#M7: WAL pre-check is also required here (third write site)."""
    _verify_wal(conn)
    existing = {row[1] for row in conn.execute(
        "PRAGMA table_info(evaluated_opportunities)"
    ).fetchall()}
    added = []
    for col, typ in CAL_MLP_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE evaluated_opportunities ADD COLUMN {col} {typ}")
            added.append(col)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_startup_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            parity_check_status TEXT,
            sizing_parity_status TEXT,
            pid INTEGER
        )
    """)
    # R-p7-deploy-r9 Round-1#2: partial index for the post-hoc processor's
    # SELECT. Without it, every poll (every 10s) does a full scan over recent
    # rows. Index only covers the unannotated 15M rows the processor cares
    # about; insert overhead is negligible because most rows are non-15M
    # or already annotated.
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_eval_opp_calmlp_pending
        ON evaluated_opportunities(evaluation_time)
        WHERE product_type='15m'
          AND cal_mlp_request_id IS NOT NULL
          AND cal_mlp_p_mean IS NULL
          AND cal_mlp_skipped_reason IS NULL
    """)
    conn.commit()
    if added:
        logger.info("[CALMLP_MIGRATE] added columns: %s", added)
    return added


# ---------------------------------------------------------------------------
# Parity asserts (R-p7-r2#C1 — bot-globals direct compare; aliased imports)
# ---------------------------------------------------------------------------

def _verify_wal(conn) -> None:
    """R-p7-impl#C12 + R-p7-r3#C2: assert WAL AND busy_timeout per CLAUDE.md
    anti-deadlock rules. Both are required for FILE-BACKED dbs.

    R-p7-deploy-r3: in-memory dbs (`:memory:`) silently fall back to
    journal_mode='memory' — they have no inter-process/thread contention
    by definition (no shared file), so the anti-deadlock rule doesn't
    apply. We accept 'memory' mode (test-only path) without checking
    busy_timeout. Production bot.py uses state.db (file-backed) and
    sets WAL+busy_timeout=30000 at StateManager.__init__ — the file
    case still gates correctly."""
    mode = conn.execute("PRAGMA journal_mode").fetchone()
    mode_str = str(mode[0]).lower() if mode else ''
    if mode_str == 'memory':
        # In-memory db — no contention possible, anti-deadlock rule N/A.
        return
    if mode_str != 'wal':
        raise CalMLPSchemaError(
            f"connection journal_mode={mode_str!r}; CLAUDE.md requires WAL "
            f"for file-backed dbs (:memory: dbs are exempted)"
        )
    bt = conn.execute("PRAGMA busy_timeout").fetchone()
    bt_ms = int(bt[0]) if bt else 0
    if bt_ms < 10000:
        raise CalMLPSchemaError(
            f"connection busy_timeout={bt_ms}ms; CLAUDE.md requires ≥10000ms"
        )


def parity_assert(conn) -> tuple:
    """Cross-check vendored cal_mlp constants against bot.constants + config.

    Bit 7.1 path-A++ (2026-05-10): refactored to drop the `bot_globals` param.
    Constants are imported directly from `bot.constants` and `config` inside
    the function body — no caller-passed globals dict, no laundered-namespace
    coupling. Cross-call state (rowid for sizing_parity_assert's UPDATE) is
    returned as a tuple instead of mutating the caller's namespace.

    Returns: (status: 'passed'|'failed', rowid: int)
    Raises: CalMLPParityError on drift; logs sentinel row to bot_startup_log.

    R-p7-r2#C1: invokes _verify_wal(conn) FIRST per CLAUDE.md anti-deadlock
    rule — any sqlite write must run on a WAL+busy_timeout connection."""
    # Function-scoped imports per Bit 7.1 path-A++ (lifts the bot_globals
    # parameter dependency). Ordering: bot.constants first (most names),
    # config second (drawdown thresholds + sizing tiers + MAX_RISK_PER_TRADE).
    from bot.constants import (
        BTC_MIN_ENTRY_PRICE, ETH_MIN_ENTRY_PRICE, SOL_MIN_ENTRY_PRICE, XRP_MIN_ENTRY_PRICE,
        MIN_ENTRY_PRICE,
        BTC_MAX_RISK_PER_TRADE, ETH_MAX_RISK_PER_TRADE, SOL_MAX_RISK_PER_TRADE, XRP_MAX_RISK_PER_TRADE,
        MIN_EDGE_BY_PRICE,
        WEEKEND_EDGE_DISCOUNT, WEEKEND_EDGE_FLOOR, OVERNIGHT_EDGE_DISCOUNT,
        STC_SIZING_SCALER_KNEE, STC_SIZING_SCALER_ENABLED,
        STC_EXTENDED_BTC_MIN_PRICE, STC_EXTENDED_ETH_MIN_PRICE,
        STC_EXTENDED_SOL_MIN_PRICE, STC_EXTENDED_XRP_MIN_PRICE,
        STC_EXTENDED_BUFFER_RESCUE,
        HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES,
    )
    from bot.config import (
        SIZING_TIERS, MAX_RISK_PER_TRADE,
        DRAWDOWN_HALF_THRESHOLD, DRAWDOWN_QUARTER_THRESHOLD, DRAWDOWN_HALT_THRESHOLD,
    )
    # DRAWDOWN_HALT_FLOOR is in NEITHER bot.constants nor bot.config at the
    # time of Bit 7.1 ship. The literal 0.10 mirrors the pre-refactor
    # `bot_globals.get('DRAWDOWN_HALT_FLOOR', 0.10)` fallback semantics.
    # (Operator may promote DRAWDOWN_HALT_FLOOR=0.10 to bot.config in a
    # future bit; this fallback then becomes a no-op redundancy.)
    DRAWDOWN_HALT_FLOOR = 0.10

    _verify_wal(conn)
    cmc = _import_cal_mlp_constants()

    failures = []
    _check_count = [0]  # mutable counter for the closure
    def _check(label, expected, actual):
        _check_count[0] += 1
        if expected != actual:
            failures.append(f"{label}: bot={expected!r} cal_mlp={actual!r}")

    # Asset floors (bot.constants origin)
    _check("ASSET_FLOORS",
           {'BTC': BTC_MIN_ENTRY_PRICE, 'ETH': ETH_MIN_ENTRY_PRICE,
            'SOL': SOL_MIN_ENTRY_PRICE, 'XRP': XRP_MIN_ENTRY_PRICE},
           cmc['ASSET_FLOORS'])
    _check("GLOBAL_MIN_ENTRY_PRICE", MIN_ENTRY_PRICE, cmc['GLOBAL_MIN_ENTRY_PRICE'])

    # Sizing
    _check("SIZING_TIERS", SIZING_TIERS, cmc['SIZING_TIERS'])
    _check("ASSET_MAX_RISK_PER_TRADE",
           {'BTC': BTC_MAX_RISK_PER_TRADE, 'ETH': ETH_MAX_RISK_PER_TRADE,
            'SOL': SOL_MAX_RISK_PER_TRADE, 'XRP': XRP_MAX_RISK_PER_TRADE},
           cmc['ASSET_MAX_RISK_PER_TRADE'])
    _check("MAX_RISK_PER_TRADE", MAX_RISK_PER_TRADE, cmc['MAX_RISK_PER_TRADE'])
    _check("DRAWDOWN_HALF_THRESHOLD", DRAWDOWN_HALF_THRESHOLD, cmc['DRAWDOWN_HALF_THRESHOLD'])
    _check("DRAWDOWN_QUARTER_THRESHOLD", DRAWDOWN_QUARTER_THRESHOLD, cmc['DRAWDOWN_QUARTER_THRESHOLD'])
    _check("DRAWDOWN_HALT_THRESHOLD", DRAWDOWN_HALT_THRESHOLD, cmc['DRAWDOWN_HALT_THRESHOLD'])
    _check("DRAWDOWN_HALT_FLOOR", DRAWDOWN_HALT_FLOOR, cmc['DRAWDOWN_HALT_FLOOR'])

    # Edge schedule
    _check("MIN_EDGE_BY_PRICE",
           [tuple(x) for x in MIN_EDGE_BY_PRICE],
           [tuple(x) for x in cmc['MIN_EDGE_BY_PRICE_SCHEDULE']])

    # Discounts
    _check("WEEKEND_EDGE_DISCOUNT", WEEKEND_EDGE_DISCOUNT, cmc['WEEKEND_EDGE_DISCOUNT'])
    _check("WEEKEND_EDGE_FLOOR", WEEKEND_EDGE_FLOOR, cmc['WEEKEND_EDGE_FLOOR'])
    _check("OVERNIGHT_EDGE_DISCOUNT", OVERNIGHT_EDGE_DISCOUNT, cmc['OVERNIGHT_EDGE_DISCOUNT'])

    # STC scaler (R-p7-r3#M2: bool also drives sizing parity)
    _check("STC_SIZING_SCALER_KNEE", STC_SIZING_SCALER_KNEE, cmc['STC_SIZING_SCALER_KNEE'])
    _check("STC_SIZING_SCALER_ENABLED",
           STC_SIZING_SCALER_ENABLED, cmc['STC_SIZING_SCALER_ENABLED'])

    # STC_EXTENDED
    _check("STC_EXTENDED_PER_ASSET_FLOOR",
           {'BTC': STC_EXTENDED_BTC_MIN_PRICE, 'ETH': STC_EXTENDED_ETH_MIN_PRICE,
            'SOL': STC_EXTENDED_SOL_MIN_PRICE, 'XRP': STC_EXTENDED_XRP_MIN_PRICE},
           cmc['STC_EXTENDED_PER_ASSET_FLOOR'])
    _check("STC_EXTENDED_BUFFER_RESCUE",
           STC_EXTENDED_BUFFER_RESCUE, cmc['STC_EXTENDED_BUFFER_RESCUE'])

    # HIGH_PRICE_STC_BLOCK
    _check("HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES",
           HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES,
           cmc['HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES'])

    ts = datetime.now(timezone.utc).isoformat()
    pid = os.getpid()
    # R-p7-impl#C11: insert single sentinel row up front; sizing_parity_assert
    # UPDATEs it with sizing_parity_status. Then the test-plan query
    # `SELECT ... ORDER BY id DESC LIMIT 1` returns BOTH columns populated.
    cur = conn.execute(
        "INSERT INTO bot_startup_log (parity_check_status, ts, pid) VALUES (?, ?, ?)",
        ('failed' if failures else 'passed', ts, pid),
    )
    rowid = cur.lastrowid
    conn.commit()
    if failures:
        raise CalMLPParityError("CALMLP_PARITY FAIL:\n  " + "\n  ".join(failures))
    # R-p7-r3#M5: log the actual count instead of a hardcoded literal so the
    # log line tracks _check() additions/removals.
    logger.info("[CALMLP_PARITY] %d constants verified", _check_count[0])
    return ('passed', rowid)


def sizing_parity_assert(conn, *, rowid: int, compute_for_15m_main_path) -> str:
    """8-vector sizing parity (Phase 7 R1#C4).

    Bit 7.1 path-A++ (2026-05-10): refactored to take `rowid` and
    `compute_for_15m_main_path` as explicit keyword-only parameters, dropping
    the `bot_globals` dict. Caller (StateManager.__init__ in bot/state.py)
    captures the rowid from parity_assert's return tuple and passes it here
    along with the compute_for_15m_main_path callable from bot._impl.

    R-p7-r2#C1: WAL pre-check (CLAUDE.md anti-deadlock).
    R-p7-r2#H1: requires parity_assert to have run first to seed the
    bot_startup_log row that this function UPDATEs. Path-A++ enforces this
    contract at the signature level (rowid is required keyword-only)
    rather than via a runtime check on a globals dict."""
    _verify_wal(conn)
    from sizing import compute_size  # R-p7-r2#M1: module-level sys.path

    test_vectors = [
        # (edge_frac, balance, price, cur_bal, hwm, stc, asset, expected)
        (0.04,    100000, 95, 100000, 100000,  60,  'BTC',  None),
        (0.025,   100000, 90,  80000, 100000, 300, 'ETH',  None),
        (0.012,   100000, 96,  50000, 100000, 600, 'SOL',  None),
        (0.04,    100000, 95,  60000, 100000,  60,  'XRP',  None),
        (0.04,    100000, 95, 100000, 100000, 300, 'BTC',  None),
        (0.04,    100000, 95, 100000, 100000, 301, 'BTC',  None),
        (0.001,   100000, 95, 100000, 100000,  60,  'BTC',  0),
        (0.04,    100000, 50, 100000, 100000,  60,  'BTC',  None),
    ]

    failures = []
    for vec in test_vectors:
        edge, bal, price, cur_bal, hwm, stc, asset, expected = vec
        cm_result = compute_size(edge, bal, price, cur_bal, hwm,
                                   seconds_to_close=stc, asset=asset)
        bot_result = compute_for_15m_main_path(edge, bal, price, cur_bal, hwm, stc, asset)
        if cm_result.contract_count != bot_result['contracts']:
            failures.append(f"vec={vec}: cal_mlp={cm_result.contract_count} bot={bot_result['contracts']}")
        if expected is not None and cm_result.contract_count != expected:
            failures.append(f"vec={vec}: cal_mlp={cm_result.contract_count} expected={expected}")

    # R-p7-impl#C11: UPDATE the same row parity_assert created.
    status = 'failed' if failures else 'passed'
    conn.execute(
        "UPDATE bot_startup_log SET sizing_parity_status = ? WHERE id = ?",
        (status, rowid),
    )
    conn.commit()
    if failures:
        raise CalMLPParityError("CALMLP_SIZING_PARITY FAIL:\n  " + "\n  ".join(failures))
    logger.info("[CALMLP_PARITY] sizing parity verified across 8 vectors")
    return 'passed'


# ---------------------------------------------------------------------------
# bot.py addition: compute_for_15m_main_path (mirrors cal_mlp/sizing.py)
# ---------------------------------------------------------------------------

def make_compute_for_15m_main_path():
    """Returns a function that re-implements the 15M MAIN-path sizing math
    using values from `bot.constants` + `config`. Inserted into bot._impl
    via:

        from integration import make_compute_for_15m_main_path
        compute_for_15m_main_path = make_compute_for_15m_main_path()

    The returned `compute` callable is used by `sizing_parity_assert` at
    startup (StateManager.__init__).

    Smell 4 refactor (Bit 7.1 follow-up, ticket 86b9vhca0 closeout +
    86b9vhccw): dropped the `bot_globals: dict` parameter. Pre-refactor,
    the closure read 11 names lazily via `g['NAME']` from a caller-passed
    dict (typically `globals()` of bot._impl). All 11 names are immutable
    at runtime per the bot/__init__.py proxy docstring (only
    WEATHER_NO_SIDE_LIVE / HOURLY_NO_SIDE_LIVE / BRACKET_NO_ENABLED
    mutate, none of which are in this set), so capture-once-at-import-time
    via direct imports preserves semantics. Mirrors Bit 7.1 path-A++
    `parity_assert` / `sizing_parity_assert` (which dropped their
    `bot_globals` params for the same reason).

    `parity_assert` separately enforces that these `bot.constants` /
    `config` values match cal_mlp's vendored constants — drift here would
    fail boot at the StateManager.__init__ parity gate."""
    # Function-scoped imports: resolved when make_compute_for_15m_main_path()
    # is called from bot/_impl.py (well after both modules are loaded). Same
    # pattern as parity_assert/sizing_parity_assert above.
    from bot.constants import (
        BTC_MAX_RISK_PER_TRADE, ETH_MAX_RISK_PER_TRADE,
        SOL_MAX_RISK_PER_TRADE, XRP_MAX_RISK_PER_TRADE,
        STC_SIZING_SCALER_KNEE, STC_SIZING_SCALER_ENABLED,
    )
    from bot.config import (
        SIZING_TIERS, MAX_RISK_PER_TRADE,
        DRAWDOWN_HALF_THRESHOLD, DRAWDOWN_QUARTER_THRESHOLD, DRAWDOWN_HALT_THRESHOLD,
    )
    # DRAWDOWN_HALT_FLOOR is in NEITHER bot.constants nor bot.config. The
    # literal 0.10 mirrors the pre-refactor `bot_globals.get('DRAWDOWN_HALT_FLOOR',
    # 0.10)` fallback semantics AND the matching literal inside
    # `parity_assert` (above). If an operator promotes DRAWDOWN_HALT_FLOOR
    # to bot.config in a future bit, this literal becomes a redundancy to
    # remove (parity_assert's _check on this name will start failing if
    # the literal drifts from the promoted config value).
    DRAWDOWN_HALT_FLOOR = 0.10

    asset_caps = {
        'BTC': BTC_MAX_RISK_PER_TRADE, 'ETH': ETH_MAX_RISK_PER_TRADE,
        'SOL': SOL_MAX_RISK_PER_TRADE, 'XRP': XRP_MAX_RISK_PER_TRADE,
    }

    def _bot_lookup_tier(fee_adj_edge_frac: float):
        for i, (floor, risk) in enumerate(SIZING_TIERS):
            if fee_adj_edge_frac >= floor:
                return (i, risk)
        return (-1, 0.0)

    def _bot_drawdown_scaler(current_balance_cents: int, hwm_cents: int) -> float:
        if hwm_cents <= 0:
            return 1.0
        ratio = current_balance_cents / hwm_cents
        if ratio < DRAWDOWN_HALT_THRESHOLD:
            return DRAWDOWN_HALT_FLOOR
        if ratio < DRAWDOWN_QUARTER_THRESHOLD:
            return 0.25
        if ratio < DRAWDOWN_HALF_THRESHOLD:
            return 0.50
        return 1.0

    def compute(fee_adj_edge_frac: float, available_balance_cents: int,
                entry_price_cents: int, current_balance_cents: int,
                hwm_cents: int, seconds_to_close: float, asset: str) -> dict:
        tier_idx, risk_fraction = _bot_lookup_tier(fee_adj_edge_frac)
        if tier_idx < 0:
            return {'contracts': 0}
        drawdown = _bot_drawdown_scaler(current_balance_cents, hwm_cents)
        asset_cap = asset_caps.get(asset, MAX_RISK_PER_TRADE)
        effective_risk = min(risk_fraction * drawdown, MAX_RISK_PER_TRADE, asset_cap)
        risk_cents = int(available_balance_cents * effective_risk)
        notional_cents = max(1, risk_cents)
        contracts = max(0, notional_cents // max(1, entry_price_cents))
        if (STC_SIZING_SCALER_ENABLED
                and seconds_to_close > STC_SIZING_SCALER_KNEE
                and contracts > 0):
            contracts = max(1, int(contracts * (STC_SIZING_SCALER_KNEE / seconds_to_close)))
        return {'contracts': contracts}

    return compute


# ---------------------------------------------------------------------------
# CalMLPPredictor (R-p7-r2#C5/C6/C9/C10/C12)
# ---------------------------------------------------------------------------

_SHA_CHAIN_CACHE: dict = {}
# R-p7-r2#H5: dict.__setitem__ is NOT atomic under PEP 703 free-threaded
# Python; lock the cache. (CPython 3.13 default still has GIL but we lock
# defensively for forward-compat.)
_SHA_CHAIN_CACHE_LOCK = threading.Lock()


def _resolve_bundle_dir(asset: str) -> str:
    """Resolve CALMLP_BUNDLE_DIR override path with per-asset precedence.

    Phase 1a of v2 asymmetric rollout
    (kb/decisions/v2-asymmetric-rollout-and-xrp-rca-may05.md). Operator
    can pin a v2 bundle for ETH in isolation by setting
    CALMLP_BUNDLE_DIR_ETH while leaving CALMLP_BUNDLE_DIR (global, with
    <ASSET> placeholder) at v1 — or unset.

    Precedence:
      1. CALMLP_BUNDLE_DIR_<ASSET_UPPER> (per-asset)
      2. CALMLP_BUNDLE_DIR                (global; <ASSET> substituted by caller)
      3. ''                               (no override; CURRENT file is read)

    Returns the RAW string. Caller (CalMLPPredictor._load) continues to
    do <ASSET> placeholder substitution and relative-path resolution
    against project_root — keeping ONE substitution site avoids the
    double-substitute foot-gun.

    Whitespace-only values are treated as unset (mirrors the
    .strip()-then-check pattern shared with CALMLP_ENABLED + the
    pre-Phase-1a global override).

    AST guard: tests/integration/test_calmlp_bundle_dir_per_asset.py asserts that
    the CALMLP_BUNDLE_DIR* env vars are ONLY read inside this function.
    """
    asset_key = asset.strip().upper() if asset else ''
    if asset_key:
        per_asset = os.environ.get(
            f'CALMLP_BUNDLE_DIR_{asset_key}', '',
        ).strip()
        if per_asset:
            return per_asset
    return os.environ.get('CALMLP_BUNDLE_DIR', '').strip()


class CalMLPPredictor:
    """Lazy-loaded per-asset predictor. Thread-safe via _lock; uses LOCK_SH
    on models/cal_mlp_<asset>/.lock during _load."""

    def __init__(self, asset: str, project_root: Optional[Path] = None):
        self.asset = asset
        # R-p7-impl#C6: RLock so predict() can re-enter while holding the
        # lock when calling _load() (which also takes self._lock).
        self._lock = threading.RLock()
        self._loaded = False
        if project_root is None:
            project_root = Path(os.environ.get(
                'KALSHI_PROJECT_ROOT',
                str(Path(__file__).resolve().parents[2]),
            ))
        self.project_root = Path(project_root)
        # Late-bound state (set on _load).
        self.conformal: Optional[dict] = None
        self.models: Optional[list] = None
        self.vocab: Optional[dict] = None
        self.normstats: Optional[dict] = None
        self.market_blend_w: Optional[float] = None
        self.train_id: Optional[str] = None

    def _verify_bundle_sha_chain(self, bundle: dict, train_dir: Path) -> None:
        """R-p7-r3#H3-DRY-1: delegates to _helpers.verify_bundle_sha_chain
        so phases 4/5/7 share ONE source of truth for the chain formula.
        R-p7-r4#MED-CACHE: cache key includes project_root so test fixtures
        with the same train_id under a fake root can't poison the prod cache."""
        cache_key = (bundle.get('train_id'), self.asset, str(self.project_root))
        with _SHA_CHAIN_CACHE_LOCK:
            if cache_key in _SHA_CHAIN_CACHE:
                return
        from _helpers import verify_bundle_sha_chain
        try:
            verify_bundle_sha_chain(bundle)
        except RuntimeError as e:
            raise CalMLPError('sha_chain_fail', str(e)) from e
        with _SHA_CHAIN_CACHE_LOCK:
            _SHA_CHAIN_CACHE[cache_key] = True

    def _load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            try:
                models_dir = self.project_root / 'models' / f'cal_mlp_{self.asset}'
                # R-p7-impl#C5: check CURRENT BEFORE creating lock file. Avoids
                # polluting models/cal_mlp_<asset>/.lock on a fresh install
                # with no Phase 4 bundle deployed yet.
                if not models_dir.exists():
                    raise CalMLPError('no_current', f"no models dir for {self.asset}")
                # CALMLP_BUNDLE_DIR per-version kill-switch
                # (kb/decisions/v2-cal-mlp-deploy-runbook-may03.md +
                # kb/decisions/calmlp-bundle-dir-per-asset-may06.md).
                # Resolution lives in module-level `_resolve_bundle_dir`
                # (per-asset CALMLP_BUNDLE_DIR_<ASSET> beats global
                # CALMLP_BUNDLE_DIR; both stripped, empty=unset).
                # <ASSET> placeholder is substituted with self.asset
                # (uppercase by bot.py callers' convention) HERE so one
                # env var still rolls back all 4 assets at once when the
                # global form is used. Substitution is intentionally a
                # raw string replace (no normalization) — bot.py is the
                # only production caller and uses uppercase asset names.
                # Conventions (NOT enforced beyond the error messages below):
                #   1. Override path must point to a DIRECTORY (not a file).
                #   2. The directory NAME must equal the bundle's train_id —
                #      the phase5 bundle filename pattern depends on it.
                #      Symlinks / renamed dirs are not supported.
                #   3. Override paths SHOULD live under models/cal_mlp_<asset>/
                #      so the per-asset .lock still coordinates with the
                #      CURRENT publisher. Adding a new asset via override-only
                #      is NOT supported (models_dir.exists() pre-check fires
                #      first, by design — see test_calmlp_bundle_dir_requires_
                #      models_dir_to_exist).
                override_raw = _resolve_bundle_dir(self.asset)
                override_train_dir: Optional[Path] = None
                if override_raw:
                    substituted = override_raw.replace('<ASSET>', self.asset)
                    override_train_dir = Path(substituted)
                    if not override_train_dir.is_absolute():
                        override_train_dir = self.project_root / override_train_dir
                    if not override_train_dir.exists():
                        raise CalMLPError(
                            'no_current',
                            f"CALMLP_BUNDLE_DIR override directory not found: "
                            f"{override_train_dir}",
                        )
                    # Convention 1: must be a directory, not a regular file.
                    if not override_train_dir.is_dir():
                        raise CalMLPError(
                            'no_current',
                            f"CALMLP_BUNDLE_DIR override path is not a directory: "
                            f"{override_train_dir}",
                        )
                else:
                    current_path = models_dir / 'CURRENT'
                    if not current_path.exists():
                        raise CalMLPError('no_current', f"no CURRENT for {self.asset}")
                lock_path = models_dir / '.lock'
                lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_SH)
                    if override_train_dir is not None:
                        train_dir = override_train_dir
                        # Convention 2: directory name == train_id. If operator
                        # used a symlink/renamed dir, bundle filename below
                        # won't match — we surface the convention in the error.
                        train_id = train_dir.name
                    else:
                        train_id = current_path.read_text().strip()
                        train_dir = models_dir / train_id
                    # R-p7-r2#H3: only phase-5 bundles deploy. Phase-4-only
                    # ablation paths must explicitly opt in upstream.
                    bundle_path = train_dir / f'cal_mlp_{self.asset}_{train_id}_phase5_bundle.json'
                    if not bundle_path.exists():
                        if override_train_dir is not None:
                            raise CalMLPError(
                                'no_current',
                                f"phase5 bundle not found at {bundle_path} "
                                f"(CALMLP_BUNDLE_DIR convention: directory NAME "
                                f"must equal train_id; symlinks/renames are not "
                                f"supported)",
                            )
                        raise CalMLPError('no_current', f"phase5 bundle not found at {bundle_path}")
                    with open(bundle_path) as f:
                        bundle = json.load(f)
                    if bundle.get('phase') != 5:
                        raise CalMLPError(
                            'phase_mismatch',
                            f"expected Phase 5, got {bundle.get('phase')}",
                        )
                    self._verify_bundle_sha_chain(bundle, train_dir)

                    deploy_idx = bundle['deploy_fold_idx']
                    extract_bundle_rel = bundle.get('extract_bundle_path', '')
                    if not extract_bundle_rel:
                        raise CalMLPError('phase_mismatch', 'extract_bundle_path missing')
                    extract_bundle_path = Path(extract_bundle_rel)
                    if not extract_bundle_path.is_absolute():
                        extract_bundle_path = self.project_root / extract_bundle_path
                    # R-p7-r2#M5: use `with open(...)` consistently.
                    with open(extract_bundle_path) as f:
                        ext_bundle = json.load(f)
                    vocab_path = extract_bundle_path.parent / ext_bundle['ticker_vocab_path']
                    with open(vocab_path) as f:
                        _vocab = json.load(f)['vocab']
                    deploy_fold = next(
                        r for r in ext_bundle['eval_fold_artifacts'] if r['fold'] == deploy_idx
                    )
                    ns_path = extract_bundle_path.parent / deploy_fold['normstats_path']
                    with open(ns_path) as f:
                        _normstats = json.load(f)
                    with open(train_dir / bundle['conformal_path']) as f:
                        _conformal = json.load(f)

                    # Build per-member models (R-p7-r2#M1: module-level path).
                    # R-p7-deploy-r7: torch threads already constrained to 1
                    # at integration.py module-import time (see
                    # _constrain_torch_threads_at_import above), BEFORE
                    # train.py is imported — so the interop-pool race is won.
                    from train import build_model_from_definition
                    import torch
                    fold_p4 = next(
                        r for r in bundle['eval_fold_artifacts'] if r['fold'] == deploy_idx
                    )
                    with open(train_dir / bundle['model_definition_path']) as f:
                        model_def = json.load(f)
                    _models = []
                    for m in fold_p4['members']:
                        marker_path = train_dir / m['marker_path']
                        with open(marker_path) as f:
                            marker = json.load(f)
                        if marker['cfg_fp'] != bundle['cfg_fp']:
                            raise CalMLPError(
                                'marker_drift',
                                f"member {m['member']} marker mismatch",
                            )
                        model = build_model_from_definition(
                            model_def, n_vocab=len(_vocab),
                        )
                        state_dict = torch.load(
                            train_dir / m['checkpoint_path'], map_location='cpu',
                        )
                        model.load_state_dict(state_dict)
                        model.eval()
                        _models.append(model)

                    # market_blend_w drift check (R-p7-r2#C9).
                    # project_root is added/removed per-call since it's external.
                    _proj_added = False
                    if str(self.project_root) not in sys.path:
                        sys.path.insert(0, str(self.project_root))
                        _proj_added = True
                    try:
                        from market_config import MARKET_CONFIGS
                        live_w = float(MARKET_CONFIGS['15m'].market_blend_w)
                    finally:
                        if _proj_added:
                            try:
                                sys.path.remove(str(self.project_root))
                            except ValueError:
                                pass
                    bundle_w = bundle.get('market_blend_w')
                    if bundle_w is not None and abs(bundle_w - live_w) > 1e-9:
                        raise CalMLPError(
                            'market_blend_w_drift',
                            f"bundle={bundle_w} live={live_w}; retrain required",
                        )

                    # Atomic publish.
                    self.conformal = _conformal
                    self.models = _models
                    # R-p7-r4#LOW-1: M=1 collapses ensemble std to 0 → conformal
                    # interval degenerates. Bundle was likely misconfigured.
                    if len(_models) < 2:
                        logger.warning(
                            "[CALMLP_LOAD] asset=%s loaded %d members (expected ≥2); "
                            "ensemble std will collapse to 0 and conformal "
                            "interval will degenerate.",
                            self.asset, len(_models),
                        )
                    self.vocab = _vocab
                    self.normstats = _normstats
                    self.market_blend_w = live_w
                    self.train_id = train_id
                    self._loaded = True
                finally:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    finally:
                        os.close(lock_fd)
            except CalMLPError:
                raise
            except Exception as e:
                raise CalMLPError('load_failed', str(e)) from e

    def warmup(self) -> None:
        """R-p7-r2#C2: optional eager-load to avoid latency spike on first
        predict() per asset. bot.py should call this at startup (after the
        predictor cache is built) to amortize the ~1-2s model load off the
        scan path. Soft-fails — exceptions are logged but don't abort
        startup, since calibration is a graceful-skip subsystem.
        R-p7-r3#H4: widened to Exception so non-CalMLPError (torch import,
        flock OSError on NFS) also defer rather than abort the bot."""
        try:
            with self._lock:
                if not self._loaded:
                    self._load()
        except Exception as e:
            # R-p7-r4#MED-EXC: log full traceback so AttributeError-class
            # programming bugs in _load aren't silently indistinguishable from
            # benign flock OSError on NFS.
            logger.warning("[CALMLP_WARMUP] %s asset=%s; deferring to first predict()",
                           e, self.asset, exc_info=True)

    def predict(self, raw_prob: float, ticker: str, side: str,
                entry_price_cents: int, row_features: dict) -> tuple:
        """Returns (cal_prob, ens_std, final_lo, final_hi). Raises CalMLPError
        on failure. R-p7-impl#C2: derives price_tier/stc_bucket from
        entry_price_cents/seconds_to_close to keep the API minimal.
        R-p7-r2#H2: NaN/None values in CONT_FEATURE_COLS flip the matching
        *_missing indicator to 1 (preserving the missing-indicator contract)
        AND mean-impute the value via normstats.
        R-p7-cleanroom#H1: ALL exceptions in the body are wrapped to
        CalMLPError so the scan thread never sees an uncaught TypeError /
        ImportError / KeyError / ValueError. annotate_evaluation_kwargs
        catches CalMLPError and falls back to raw_prob."""
        try:
            return self._predict_inner(raw_prob, ticker, side,
                                        entry_price_cents, row_features)
        except CalMLPError:
            raise
        except (MemoryError,) as e:
            raise CalMLPError('predict_oom', str(e)) from e
        except Exception as e:
            raise CalMLPError('predict_runtime', repr(e)) from e

    def _predict_inner(self, raw_prob, ticker, side, entry_price_cents, row_features):
        # R-p7-cleanroom#M1: lock held only for state-publish synchronization.
        # Free-threaded Python: torch.eval forward path is self-contained on
        # local references built outside the lock, so re-acquiring the lock
        # for every attribute read isn't required for correctness.
        with self._lock:
            if not self._loaded:
                self._load()
            # Snapshot all self.* state into locals UNDER the lock so a
            # concurrent reload (future hot-reload) can't tear our reads.
            _vocab = self.vocab
            _normstats = self.normstats
            _models = self.models
            _conformal = self.conformal
            _market_blend_w = self.market_blend_w
            _train_id = self.train_id
        # R-p7-r2#M1: imports moved to module level.
        from features import (RAW_PROB_CLIP_EPS, MISSING_INDICATOR_COLS,
                                CONT_FEATURE_COLS, PRICE_BIN_CUTOFFS,
                                STC_BIN_CUTOFFS)
        from normalize import apply_norm
        from _helpers import predict_with_interval, FORWARD_KEYS

        import torch
        import numpy as np
        import pandas as pd

        # R-p7-impl#C2: derive price_tier/stc_bucket if not provided.
        # R-p7-r2#H4: accept either seconds_to_close (canonical) or
        # seconds_remaining (bot.py local var name) so callers don't need to
        # rename their scope variables.
        row_features = dict(row_features)
        if 'price_tier' not in row_features:
            row_features['price_tier'] = int(
                np.digitize(entry_price_cents, PRICE_BIN_CUTOFFS, right=True)
            )
        if 'stc_bucket' not in row_features:
            stc = row_features.get('seconds_to_close')
            if stc is None:
                stc = row_features.get('seconds_remaining')
            if stc is None:
                raise CalMLPError(
                    'missing_features',
                    'predict() requires seconds_to_close or seconds_remaining or stc_bucket',
                )
            row_features['stc_bucket'] = int(
                np.digitize(stc, STC_BIN_CUTOFFS, right=True)
            )
            # R-p7-r3#H1: seed seconds_to_close in row so apply_norm finds it.
            # Use explicit None check (NOT setdefault) so a stale None value
            # in row_features is replaced with the resolved stc.
            if row_features.get('seconds_to_close') is None:
                row_features['seconds_to_close'] = stc

        # Build a 1-row DataFrame.
        row = dict(row_features)
        row.setdefault('ticker', ticker)
        row.setdefault('raw_prob', raw_prob)
        row.setdefault('side', side)
        row.setdefault('market_price', entry_price_cents)
        rp_c = float(np.clip(raw_prob, RAW_PROB_CLIP_EPS, 1.0 - RAW_PROB_CLIP_EPS))
        row['logit_raw_prob_clipped'] = float(np.log(rp_c / (1.0 - rp_c)))
        row.setdefault('side_int', 1 if side == 'yes' else 0)
        row.setdefault('vol_regime_int', int(row.get('vol_regime', 0) == 'elevated'))
        row['ticker_id'] = _vocab.get(str(ticker), 0)
        # R-p7-cleanroom-r6#H1: inverse map + collision check ran at module
        # import time. _MISSING_INDICATOR_SRC_TO_IND is module-frozen.
        _src_to_ind = _MISSING_INDICATOR_SRC_TO_IND
        # R-p7-cleanroom#M2: indicator cols are AUTHORITATIVELY set by this
        # layer based on source-col missingness. Pre-set values from the
        # caller would silently violate the contract (indicator=1 with
        # source present → model sees inconsistent input). Force-overwrite
        # to 0; the source-missing loop below sets to 1 where applicable.
        for col in MISSING_INDICATOR_COLS:
            row[col] = 0
        # R-p7-r3#C1: previously this branch eagerly imputed with the
        # POST-transform mean and let apply_norm re-transform — wrong for
        # log/log1p columns. Fix: leave NaN, let apply_norm.fillna(mean)
        # run AFTER its transform step (normalize.py:171 path is correct).
        # We still flip the *_missing companion at this layer because that's
        # a Phase-7 feature engineering decision, not a normalize concern.
        # R-p7-deploy-r4#C1 SAFETY NET: silently mean-imputing features that
        # have NO missing-indicator companion is a SHIP-BLOCKER bug — the
        # model loses ~13 of ~20 conditioning features and predictions
        # collapse toward the training prior. Raise missing_features for
        # any non-companion column that wasn't explicitly seeded by the
        # caller. This forces Edit 4 (or any future caller) to populate
        # the full row, OR triggers a graceful skip with audit trail.
        # Exception: identity_no_zscore columns (hour_sin/hour_cos) are
        # analytical, deterministic from datetime, and never legitimately
        # missing — they get fillna(0.0) inside apply_norm.
        # R-p7-deploy-r5#L2: _IDENTITY_NO_Z is module-level (hoisted).
        normstats_map = _normstats.get('stats', {})
        for col in CONT_FEATURE_COLS:
            v = row.get(col)
            is_missing = (v is None) or (
                isinstance(v, float) and (v != v)  # NaN
            )
            if col not in row or is_missing:
                col_stats = normstats_map.get(col, {})
                if 'mean' not in col_stats:
                    raise CalMLPError(
                        'missing_features',
                        f"col {col!r} missing and no normstats mean to impute",
                    )
                ind = _src_to_ind.get(col)
                if ind is None and col not in _IDENTITY_NO_Z:
                    # NO missing-indicator companion AND not analytical.
                    # The model was trained with this feature ALWAYS PRESENT;
                    # mean-imputing would silently degrade the prediction.
                    raise CalMLPError(
                        'missing_features',
                        f"col {col!r} not provided by caller and has no "
                        f"*_missing indicator. Edit 4 must populate this "
                        f"feature in row_features (or wire the scan loop to "
                        f"compute it). Calibrator falling back to raw_prob.",
                    )
                # Set to NaN so apply_norm's post-transform fillna runs.
                row[col] = float('nan')
                if ind is not None:
                    row[ind] = 1
        df = pd.DataFrame([row])
        df_norm = apply_norm(df, _normstats['stats'], CONT_FEATURE_COLS,
                              transforms=_normstats.get('transforms', {}))

        # Forward through ensemble. R-p7-cleanroom#H1: outer predict()
        # wraps all exceptions; the redundant inner try/except is removed.
        with torch.no_grad():
            batch = {
                'x_cont': torch.tensor(df_norm[CONT_FEATURE_COLS].to_numpy(np.float32)),
                'x_missing': torch.tensor(df_norm[MISSING_INDICATOR_COLS].to_numpy(np.float32)),
                'price_tier': torch.tensor(df_norm['price_tier'].to_numpy(np.int64)),
                'stc_bucket': torch.tensor(df_norm['stc_bucket'].to_numpy(np.int64)),
                'vol_regime_int': torch.tensor(df_norm['vol_regime_int'].to_numpy(np.int64)),
                'side_int': torch.tensor(df_norm['side_int'].to_numpy(np.int64)),
                'ticker_id': torch.tensor(df_norm['ticker_id'].to_numpy(np.int64)),
                'logit_raw_prob_clipped': torch.tensor(
                    df_norm['logit_raw_prob_clipped'].to_numpy(np.float32)
                ),
            }
            preds = []
            for m in _models:
                _, p = m(**{k: batch[k] for k in FORWARD_KEYS})
                preds.append(p.detach().cpu().numpy())
            stacked = np.stack(preds)  # [M, 1]
            p_mean = float(stacked.mean(axis=0)[0])
            p_std = float(stacked.std(axis=0, ddof=0)[0])

        # Apply conformal interval.
        result = predict_with_interval(
            p_mean, p_std, _conformal,
            row_features={'price_tier': int(row['price_tier']),
                          'stc_bucket': int(row['stc_bucket']),
                          'vol_regime': int(row['vol_regime_int'])},
            entry_price_cents=entry_price_cents, side=side,
            market_blend_w=_market_blend_w, mode='inference',
        )
        cal_prob, ens_std, final_lo, final_hi = result
        return cal_prob, ens_std, final_lo, final_hi


# ---------------------------------------------------------------------------
# Predictor cache (relocated from bot/_impl.py per Smell 3 fu, 86b9vhcat,
# 2026-05-10). Plan: kb/decisions/smell-3-calmlp-predictors-relocation-plan-may10.md.
#
# Kill-switch contract (R-p7-cleanroom#H2 + R-p7-coldboot#C-S2):
# Predictor INSTANCES are always constructed at module-import time below
# (CalMLPPredictor.__init__ is pure attr-set; no IO — pinned by
# tests/integration/test_cal_mlp_invariants.py::test_cal_mlp_predictor_init_zero_io).
# .warmup() is gated on CALMLP_ENABLED. This ordering is required so hot-
# flipping CALMLP_ENABLED=0→1 mid-process actually activates calibration on
# the first scan tick — without always-construct, an env=0 boot would leave
# the cache permanently unwarmed AND missing instances. The per-call
# CALMLP_ENABLED check inside annotate_evaluation_kwargs and
# annotate_evaluation_async_enqueue (below) ensures predict() never runs
# when env=0 even if the cache IS warmed.
#
# Pre-Smell-3 this lived in bot/_impl.py:589-606. The cache + warmup
# orchestration belong with CalMLPPredictor (locality of reference);
# bot/_impl.py now imports both names and emits the boot log in its own
# bot._impl logger namespace using the (enabled, warmed_count) tuple this
# helper returns (M3 — operator-runbook grep contract).
# ---------------------------------------------------------------------------
_calmlp_predictors: dict = {a: CalMLPPredictor(a) for a in ('BTC', 'ETH', 'SOL', 'XRP')}


def warmup_predictor_cache() -> tuple:
    """Conditionally warm the predictor cache based on CALMLP_ENABLED env.

    Returns (enabled, warmed_count) — the caller (bot/_impl.py module-load)
    uses these counters to emit the boot log in the bot._impl logger
    namespace, preserving the operator-runbook grep contract.

    Reads CALMLP_ENABLED from os.environ at call time (not at module-import
    time), so an env=0 boot followed by an env=1 mid-process flip is
    honored on the next call — pin in
    tests/integration/test_calmlp_predictor_cache_relocation.py::test_hot_env_flip_zero_to_one_honored.

    Idempotent: subsequent calls re-check env and re-iterate.
    CalMLPPredictor.warmup() short-circuits via the already-loaded guard,
    so warmed_count settles across calls.
    """
    enabled = (
        os.environ.get('CALMLP_ENABLED', '1').strip().lower() in ('1', 'true', 'yes')
    )
    if not enabled:
        return False, 0
    for p in _calmlp_predictors.values():
        p.warmup()
    return True, sum(1 for p in _calmlp_predictors.values() if p._loaded)


# ---------------------------------------------------------------------------
# R-p7-deploy-r9: post-hoc cal_mlp processor (replaces R-p7-deploy-r8 inline pool)
#
# Edit 4 in bot.py reduces to a single uuid stamp on _shadow_diag — zero
# scan-thread overhead beyond ~1µs per 15M market. The actual feature
# reconstruction + predict + UPDATE happens in a separate daemon thread
# (CalMLPPostHocProcessor) that polls evaluated_opportunities every N
# seconds for unannotated rows.
#
# All v1 features are derivable from DB columns (market_price,
# seconds_to_close, spot_distance_to_strike_sigma, prob_breakeven_gap,
# vol_regime, evaluation_time, raw_prob, ticker, side). No bot state
# access required.
#
# Lifecycle: bot.py startup creates and start()s the processor AFTER
# predictors are warmed; bot.py shutdown stop()s it BEFORE state.close().
# ---------------------------------------------------------------------------

import uuid


def annotate_evaluation_async_enqueue(
    kwargs: dict,
    *,
    raw_prob,
    ticker: str,
    side: str,
    entry_price_cents: int,
    row_features: dict,
    predictor,
    db_path: str,
) -> None:
    """R-p7-deploy-r9: stamp cal_mlp_request_id on the row and return.

    Edit 4 in bot.py calls this once per 15M scan iteration. The actual
    predict() runs in CalMLPPostHocProcessor's daemon thread, which polls
    the DB for rows where cal_mlp_request_id IS NOT NULL AND
    cal_mlp_p_mean IS NULL.

    Synchronous skip paths (no uuid stamped):
    - env=0 → kwargs['cal_mlp_skipped_reason']='env_disabled'
    - raw_prob None → 'raw_prob_null'
    - predictor None → 'no_predictor'

    NOTE: this function does NOT return a calibrated probability — v1 is
    shadow-only. final_prob downstream uses raw_prob path. The
    `row_features`, `entry_price_cents`, `db_path` parameters are kept
    for API stability with the v1.5 signature; the post-hoc processor
    re-derives features from DB columns and ignores them. They are
    accepted to avoid bot.py call-site churn.
    """
    if os.environ.get('CALMLP_ENABLED', '1').strip().lower() not in ('1', 'true', 'yes'):
        kwargs['cal_mlp_skipped_reason'] = 'env_disabled'
        return
    if raw_prob is None:
        kwargs['cal_mlp_skipped_reason'] = 'raw_prob_null'
        return
    if predictor is None:
        kwargs['cal_mlp_skipped_reason'] = 'no_predictor'
        return
    # All cheap checks passed — stamp uuid; processor will pick it up.
    kwargs['cal_mlp_request_id'] = uuid.uuid4().hex


# Module-level slot for the bot's post-hoc processor instance. bot.py
# startup constructs and start()s it; shutdown stop()s it.
_POSTHOC_PROCESSOR = None


def start_post_hoc_processor(
    db_path: str, predictors: dict,
    poll_interval_sec: float = 10.0, batch_size: int = 50,
):
    """Construct + start the CalMLPPostHocProcessor. Idempotent: subsequent
    calls log a warning and return the existing instance."""
    global _POSTHOC_PROCESSOR
    if _POSTHOC_PROCESSOR is not None:
        logger.warning('[CALMLP_POSTHOC] start called but processor already exists')
        return _POSTHOC_PROCESSOR
    from post_hoc_processor import CalMLPPostHocProcessor
    _POSTHOC_PROCESSOR = CalMLPPostHocProcessor(
        db_path=db_path, predictors=predictors,
        poll_interval_sec=poll_interval_sec, batch_size=batch_size,
    )
    _POSTHOC_PROCESSOR.start()
    return _POSTHOC_PROCESSOR


def stop_post_hoc_processor(timeout_sec: float = 10.0) -> None:
    """Stop the post-hoc processor. Bot's _cleanup() should call this BEFORE
    state.close() so any in-flight UPDATE can land."""
    global _POSTHOC_PROCESSOR
    if _POSTHOC_PROCESSOR is None:
        return
    _POSTHOC_PROCESSOR.stop(timeout_sec=timeout_sec)
    _POSTHOC_PROCESSOR = None


# Backwards-compat shim: bot.py imported drain_predict_pool from v1.5. Keep
# the name so the import doesn't break, but route to the new lifecycle.
def drain_predict_pool(timeout_sec: float = 10.0) -> None:
    """Compat shim → stop_post_hoc_processor."""
    stop_post_hoc_processor(timeout_sec=timeout_sec)


# ---------------------------------------------------------------------------
# R-p7-deploy-r10: TM-96 cal_mlp lower-bound gate
# ---------------------------------------------------------------------------

# Round-1 #5: fail-open metric. Operators need a counter to distinguish
# "gate is doing its job" from "predictor is silently broken." Logged
# periodically to operator-facing channel.
_TM96_GATE_METRICS = {
    'calls': 0,
    'blocks': 0,             # would_block=True (regardless of env-flag)
    'allows': 0,             # would_block=False with valid prediction
    'fail_open_no_predictor': 0,
    'fail_open_env_disabled': 0,
    'fail_open_raw_prob_null': 0,
    'fail_open_not_loaded': 0,
    'fail_open_feature_error': 0,
    'fail_open_predict_error': 0,
    'fail_open_dispatch_miss': 0,   # final_lo is None
}
_TM96_GATE_METRICS_LOCK = threading.Lock()


def _tm96_gate_bump(key: str) -> None:
    with _TM96_GATE_METRICS_LOCK:
        _TM96_GATE_METRICS[key] = _TM96_GATE_METRICS.get(key, 0) + 1


def get_tm96_gate_metrics() -> dict:
    """Snapshot for dashboard / health checks."""
    with _TM96_GATE_METRICS_LOCK:
        return dict(_TM96_GATE_METRICS)

def should_block_tm96(
    *,
    predictor,
    raw_prob: float,
    calibrated_prob: float,      # final_prob (post-CalEngine + post-temp + post-OFA + post-blend);
                                 # MUST be the same value passed to insert_evaluated_opportunity
                                 # so the gate's prob_breakeven_gap matches the DB column used
                                 # for training. NOT raw_prob, NOT pre-blend cal_prob.
                                 # See R-p7-deploy-r11 R2 critique log.
    ticker: str,
    market_price: int,           # entry_price_cents
    seconds_to_close: float,
    spot: float,
    threshold: float,
    blended_rv: float,
    vol_regime: str,
):
    """Synchronous cal_mlp gate for TM-96 trades.

    Returns (should_block: bool, diag: dict). Gate fires (returns True)
    when cal_mlp's conformal LOWER bound `final_lo` is below the market
    price — i.e., the prob could plausibly be below break-even given v1's
    uncertainty. Fails OPEN: any error → returns False (don't block).

    Why a synchronous predict here: TM-96 fires a few times per day, so
    the inline ~25ms cost doesn't accumulate the way it did when we ran
    cal_mlp on every scan-tick window. The post-hoc processor still
    handles the bulk audit annotation; this gate is a targeted decision-
    time check for the one strategy that doesn't gate on edge.

    The gate ALWAYS computes (so we can shadow-log) — bot.py is
    responsible for honoring `TM96_CALMLP_GATE_ENABLED` to decide whether
    a True return blocks the trade or just gets logged.
    """
    diag: dict = {}
    _tm96_gate_bump('calls')
    if raw_prob is None:
        diag['cal_mlp_skipped_reason'] = 'raw_prob_null'
        _tm96_gate_bump('fail_open_raw_prob_null')
        return False, diag
    if predictor is None:
        diag['cal_mlp_skipped_reason'] = 'no_predictor'
        _tm96_gate_bump('fail_open_no_predictor')
        return False, diag
    if os.environ.get('CALMLP_ENABLED', '1').strip().lower() not in ('1', 'true', 'yes'):
        diag['cal_mlp_skipped_reason'] = 'env_disabled'
        _tm96_gate_bump('fail_open_env_disabled')
        return False, diag
    if not getattr(predictor, '_loaded', True):
        diag['cal_mlp_skipped_reason'] = 'predictor_not_loaded'
        _tm96_gate_bump('fail_open_not_loaded')
        return False, diag

    # Build v1's row_features dict from raw inputs. Same schema as
    # post_hoc_processor._process_row + bot.py Edit-4 (pre-trim) but
    # operating on values from the live scan iteration rather than DB
    # columns. Keep the formula EXACTLY matching training:
    # - hour: integer dt.hour (R-p7-deploy-r9 Round-1#1: training used
    #   `hour_of_day_utc % 24` which is an integer; minute-fractional
    #   would be out-of-distribution for hour_sin/hour_cos which are
    #   identity_no_zscore raw passthrough).
    # - tdp: distance × clip(1 - stc/900, 0, 1).
    import math as _math
    import numpy as _np
    from datetime import datetime as _dt, timezone as _tz

    # Tier 5 derived features (pure math, ~100µs). Sprint A Bit 1b
    # (86b9veppa) routes through bot.helpers.derived_features.compute_derived_features
    # — the canonical helper also called from bot/state.py:1713 + :2010
    # (pre-DB-write). Lock-step contract: train (DB write) and serve (this
    # path) MUST use the same formula. apply_sigma_winsor is applied here
    # on the helper's return value, mirroring bot/state.py:1723.
    spot_dist_sigma = None
    breakeven_gap = None
    try:
        # Round-1 #1 train/serve skew fix: training extracted
        # `prob_breakeven_gap` from the DB column populated via the same
        # helper in insert_evaluated_opportunity. Use calibrated_prob
        # (post-CalEngine) to match training distribution; fall back to
        # raw_prob only when no CalEngine has fired yet.
        cb_prob = calibrated_prob if calibrated_prob is not None else raw_prob
        _t5 = compute_derived_features(
            spot_price=spot,
            threshold=threshold,
            volatility=blended_rv,
            seconds_to_close=seconds_to_close,
            calibrated_prob=cb_prob,
            market_price_cents=market_price,
        )
        # R-p7-deploy-r11 R3 CRITICAL: clip to match train-time winsorize.
        # At terminal STC (T→0) raw sigma blows up to ±3,000+; train sees
        # ±25 max. Without this clip the gate's row_features distribution
        # diverges from training.
        from features import apply_sigma_winsor
        spot_dist_sigma = apply_sigma_winsor(_t5['spot_distance_to_strike_sigma'])
        breakeven_gap = _t5['prob_breakeven_gap']
    except Exception as e:
        diag['cal_mlp_skipped_reason'] = f'feature_build_error:{type(e).__name__}'
        _tm96_gate_bump('fail_open_feature_error')
        return False, diag

    if spot_dist_sigma is not None:
        decay = max(0.0, min(1.0, 1.0 - seconds_to_close / 900.0))
        tdp = spot_dist_sigma * decay
        abs_dist = abs(spot_dist_sigma)
    else:
        tdp = None
        abs_dist = None

    now_dt = _dt.now(_tz.utc)
    int_hour = float(now_dt.hour)
    from features import compute_hour_features
    _hsin, _hcos = compute_hour_features(int_hour)
    row_features = {
        'price_tier': int(_np.digitize(market_price, [80, 90, 96], right=True)),
        'stc_bucket': int(_np.digitize(seconds_to_close, [120, 300, 600], right=True)),
        'vol_regime_int': 1 if vol_regime == 'elevated' else 0,
        'vol_regime': vol_regime or 'normal',
        'spot_distance_to_strike_sigma': spot_dist_sigma,
        'abs_spot_distance_to_strike_sigma': abs_dist,
        'time_decayed_proximity': tdp,
        'prob_breakeven_gap': breakeven_gap,
        'hour_sin': _hsin,
        'hour_cos': _hcos,
        'seconds_to_close': seconds_to_close,
    }

    # Synchronous predict.
    try:
        cal_prob, ens_std, final_lo, final_hi = predictor.predict(
            raw_prob=raw_prob, ticker=ticker, side='yes',
            entry_price_cents=market_price, row_features=row_features,
        )
    except Exception as e:
        diag['cal_mlp_skipped_reason'] = f'predict_error:{type(e).__name__}'
        _tm96_gate_bump('fail_open_predict_error')
        logger.debug("[CALMLP_TM96_GATE] predict raised for %s: %s", ticker, e)
        return False, diag

    diag['cal_mlp_p_mean'] = cal_prob
    diag['cal_mlp_p_std'] = ens_std
    diag['cal_mlp_final_lo'] = final_lo
    diag['cal_mlp_final_hi'] = final_hi
    diag['cal_mlp_train_id'] = getattr(predictor, 'train_id', None)

    # Round-1 #7: dispatch miss observability. final_lo=None means the
    # conformal-cell dispatch failed (cell key not in conformal table).
    if final_lo is None or market_price is None:
        diag['cal_mlp_skipped_reason'] = 'dispatch_miss'
        _tm96_gate_bump('fail_open_dispatch_miss')
        return False, diag
    market_p = market_price / 100.0
    blocked = (final_lo < market_p)
    if blocked:
        _tm96_gate_bump('blocks')
    else:
        _tm96_gate_bump('allows')
    return blocked, diag


# ---------------------------------------------------------------------------
# Per-evaluation hook (R-p7-r2#C11 + R-p7-r3#C3)
# ---------------------------------------------------------------------------

def annotate_evaluation_kwargs(
    kwargs: dict,
    raw_prob: Optional[float],
    ticker: str,
    side: str,
    entry_price_cents: int,
    row_features: dict,
    predictor: Optional[CalMLPPredictor],
) -> Optional[float]:
    """In-place mutate kwargs with cal_mlp fields. Returns the calibrated
    final_prob to use downstream, or None if calibration was skipped.

    Usage in bot.py scan loop:

        new_prob = annotate_evaluation_kwargs(kwargs, raw_prob, ticker, side,
                                                entry_price_cents, row_features,
                                                _calmlp_predictors.get(asset))
        if new_prob is not None:
            final_prob = new_prob   # otherwise existing raw_prob path runs
    """
    # R-p7-cleanroom#M4: env check FIRST so kill-switch dashboards don't
    # under-count when raw_prob is also None.
    # R-p7-impl#C9: explicit truthy set; '' / 'no' / '0' all disable.
    if os.environ.get('CALMLP_ENABLED', '1').strip().lower() not in ('1', 'true', 'yes'):
        kwargs['cal_mlp_skipped_reason'] = 'env_disabled'
        return None
    if raw_prob is None:
        kwargs['cal_mlp_skipped_reason'] = 'raw_prob_null'
        return None
    if predictor is None:
        kwargs['cal_mlp_skipped_reason'] = 'no_predictor'
        return None

    try:
        cal_prob, ens_std, final_lo, final_hi = predictor.predict(
            raw_prob=raw_prob, ticker=ticker, side=side,
            entry_price_cents=entry_price_cents, row_features=row_features,
        )
        kwargs['cal_mlp_p_mean'] = float(cal_prob)
        kwargs['cal_mlp_p_std'] = float(ens_std)
        kwargs['cal_mlp_final_lo'] = float(final_lo) if final_lo is not None else None
        kwargs['cal_mlp_final_hi'] = float(final_hi) if final_hi is not None else None
        kwargs['cal_mlp_train_id'] = predictor.train_id
        return float(cal_prob)
    except CalMLPError as e:
        if e.code in SKIPPED_REASONS:
            kwargs['cal_mlp_skipped_reason'] = e.code
        else:
            kwargs['cal_mlp_skipped_reason'] = 'load_failed'
        logger.warning("[CALMLP] %s asset=%s ticker=%s; falling back to raw_prob",
                        e, predictor.asset, ticker)
        return None


__all__ = [
    'CalMLPError', 'CalMLPParityError', 'CalMLPSchemaError',
    'CAL_MLP_COLUMNS', 'SKIPPED_REASONS',
    'migrate_schema', 'parity_assert', 'sizing_parity_assert',
    'make_compute_for_15m_main_path',
    'CalMLPPredictor', 'annotate_evaluation_kwargs',
    'warmup_predictor_cache',
]
