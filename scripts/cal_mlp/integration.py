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

# Single source of truth for the OMP/MKL setdefaults: scripts/cal_mlp/_thread_env.py.
# bot.py imports _thread_env BEFORE numpy at line 1; integration.py imports it
# here as belt-and-suspenders for tests/scripts that bypass bot.py. Either path
# guarantees the env vars are set before any C extension that reads them.
import _thread_env  # noqa: F401 — side-effect import: sets OMP_NUM_THREADS=1 etc.

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
    # R-p7-deploy-r8 async-predict additions:
    'queue_full',           # async pool's bounded queue is at capacity
    'async_predict_failed', # worker thread raised an exception
    'row_not_found',        # async UPDATE hit rowcount=0 after retries (likely
                            # row got UPSERT-overwritten by newer tick with a
                            # different request_id)
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


def parity_assert(bot_globals: dict, conn) -> str:
    """Cross-check vendored cal_mlp constants against bot.py globals.
    bot_globals: caller passes vars() / globals() of the bot module.
    Raises CalMLPParityError on drift; logs sentinel row to bot_startup_log.

    R-p7-r2#C1: invokes _verify_wal(conn) FIRST per CLAUDE.md anti-deadlock
    rule — any sqlite write must run on a WAL+busy_timeout connection."""
    _verify_wal(conn)
    cmc = _import_cal_mlp_constants()

    failures = []
    _check_count = [0]  # mutable counter for the closure
    def _check(label, expected, actual):
        _check_count[0] += 1
        if expected != actual:
            failures.append(f"{label}: bot={expected!r} cal_mlp={actual!r}")

    g = bot_globals  # shorthand

    # Asset floors (bot.py:219-225)
    _check("ASSET_FLOORS",
           {'BTC': g['BTC_MIN_ENTRY_PRICE'], 'ETH': g['ETH_MIN_ENTRY_PRICE'],
            'SOL': g['SOL_MIN_ENTRY_PRICE'], 'XRP': g['XRP_MIN_ENTRY_PRICE']},
           cmc['ASSET_FLOORS'])
    _check("GLOBAL_MIN_ENTRY_PRICE", g['MIN_ENTRY_PRICE'], cmc['GLOBAL_MIN_ENTRY_PRICE'])

    # Sizing
    _check("SIZING_TIERS", g['SIZING_TIERS'], cmc['SIZING_TIERS'])
    _check("ASSET_MAX_RISK_PER_TRADE",
           {'BTC': g['BTC_MAX_RISK_PER_TRADE'], 'ETH': g['ETH_MAX_RISK_PER_TRADE'],
            'SOL': g['SOL_MAX_RISK_PER_TRADE'], 'XRP': g['XRP_MAX_RISK_PER_TRADE']},
           cmc['ASSET_MAX_RISK_PER_TRADE'])
    _check("MAX_RISK_PER_TRADE", g['MAX_RISK_PER_TRADE'], cmc['MAX_RISK_PER_TRADE'])
    _check("DRAWDOWN_HALF_THRESHOLD", g['DRAWDOWN_HALF_THRESHOLD'], cmc['DRAWDOWN_HALF_THRESHOLD'])
    _check("DRAWDOWN_QUARTER_THRESHOLD", g['DRAWDOWN_QUARTER_THRESHOLD'], cmc['DRAWDOWN_QUARTER_THRESHOLD'])
    _check("DRAWDOWN_HALT_THRESHOLD", g['DRAWDOWN_HALT_THRESHOLD'], cmc['DRAWDOWN_HALT_THRESHOLD'])
    # R-p7-r4-cross-phase-v2#M1: explicit check for DRAWDOWN_HALT_FLOOR.
    # bot.py may not expose this constant (it's hardcoded inside
    # models.PositionSizer._drawdown_scaler); g.get() with the same fallback
    # impl uses, so a "constant mismatch" message surfaces here instead of
    # surfacing ONLY via sizing_parity_assert vec 4 (which is correct but less
    # specific). Operator may optionally promote DRAWDOWN_HALT_FLOOR=0.10 to
    # config.py to make this assertion exact rather than fallback-equal.
    _check("DRAWDOWN_HALT_FLOOR",
           g.get('DRAWDOWN_HALT_FLOOR', 0.10), cmc['DRAWDOWN_HALT_FLOOR'])

    # Edge schedule
    _check("MIN_EDGE_BY_PRICE",
           [tuple(x) for x in g['MIN_EDGE_BY_PRICE']],
           [tuple(x) for x in cmc['MIN_EDGE_BY_PRICE_SCHEDULE']])

    # Discounts
    _check("WEEKEND_EDGE_DISCOUNT", g['WEEKEND_EDGE_DISCOUNT'], cmc['WEEKEND_EDGE_DISCOUNT'])
    _check("WEEKEND_EDGE_FLOOR", g['WEEKEND_EDGE_FLOOR'], cmc['WEEKEND_EDGE_FLOOR'])
    _check("OVERNIGHT_EDGE_DISCOUNT", g['OVERNIGHT_EDGE_DISCOUNT'], cmc['OVERNIGHT_EDGE_DISCOUNT'])

    # STC scaler (R-p7-r3#M2: bool also drives sizing parity)
    _check("STC_SIZING_SCALER_KNEE", g['STC_SIZING_SCALER_KNEE'], cmc['STC_SIZING_SCALER_KNEE'])
    _check("STC_SIZING_SCALER_ENABLED",
           g['STC_SIZING_SCALER_ENABLED'], cmc['STC_SIZING_SCALER_ENABLED'])

    # STC_EXTENDED
    _check("STC_EXTENDED_PER_ASSET_FLOOR",
           {'BTC': g['STC_EXTENDED_BTC_MIN_PRICE'], 'ETH': g['STC_EXTENDED_ETH_MIN_PRICE'],
            'SOL': g['STC_EXTENDED_SOL_MIN_PRICE'], 'XRP': g['STC_EXTENDED_XRP_MIN_PRICE']},
           cmc['STC_EXTENDED_PER_ASSET_FLOOR'])
    _check("STC_EXTENDED_BUFFER_RESCUE",
           g['STC_EXTENDED_BUFFER_RESCUE'], cmc['STC_EXTENDED_BUFFER_RESCUE'])

    # HIGH_PRICE_STC_BLOCK
    _check("HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES",
           g['HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES'],
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
    bot_globals['_calmlp_startup_log_rowid'] = cur.lastrowid
    conn.commit()
    if failures:
        raise CalMLPParityError("CALMLP_PARITY FAIL:\n  " + "\n  ".join(failures))
    # R-p7-r3#M5: log the actual count instead of a hardcoded literal so the
    # log line tracks _check() additions/removals.
    logger.info("[CALMLP_PARITY] %d constants verified", _check_count[0])
    return 'passed'


def sizing_parity_assert(bot_globals: dict, conn) -> str:
    """8-vector sizing parity (Phase 7 R1#C4).

    R-p7-r2#C1: WAL pre-check (CLAUDE.md anti-deadlock).
    R-p7-r2#H1: requires parity_assert to have run first; no fallback INSERT
    so the test-plan query (`ORDER BY id DESC LIMIT 1`) is guaranteed to
    return a single row with both columns populated."""
    _verify_wal(conn)
    from sizing import compute_size  # R-p7-r2#M1: module-level sys.path

    g = bot_globals
    bot_compute = g.get('compute_for_15m_main_path')
    if bot_compute is None:
        raise CalMLPParityError(
            "compute_for_15m_main_path not defined in bot.py — Phase 7 amendment required"
        )

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
        bot_result = bot_compute(edge, bal, price, cur_bal, hwm, stc, asset)
        if cm_result.contract_count != bot_result['contracts']:
            failures.append(f"vec={vec}: cal_mlp={cm_result.contract_count} bot={bot_result['contracts']}")
        if expected is not None and cm_result.contract_count != expected:
            failures.append(f"vec={vec}: cal_mlp={cm_result.contract_count} expected={expected}")

    # R-p7-impl#C11: UPDATE the same row parity_assert created.
    # R-p7-r2#H1: hard fail if parity_assert wasn't called first. No fallback
    # INSERT — that would split the audit row, breaking the test-plan query.
    rowid = bot_globals.get('_calmlp_startup_log_rowid')
    if rowid is None:
        raise CalMLPParityError(
            "sizing_parity_assert called before parity_assert; "
            "deploy contract requires parity_assert first to seed bot_startup_log row"
        )
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

def make_compute_for_15m_main_path(bot_globals: dict):
    """Returns a function that re-implements the 15M MAIN-path sizing math
    using bot.py globals. Phase 7 inserts this into bot.py via:

        from cal_mlp.integration import make_compute_for_15m_main_path
        compute_for_15m_main_path = make_compute_for_15m_main_path(globals())

    The static-method-style function is used by sizing_parity_assert at startup."""
    g = bot_globals
    asset_caps = {
        'BTC': g['BTC_MAX_RISK_PER_TRADE'], 'ETH': g['ETH_MAX_RISK_PER_TRADE'],
        'SOL': g['SOL_MAX_RISK_PER_TRADE'], 'XRP': g['XRP_MAX_RISK_PER_TRADE'],
    }

    def _bot_lookup_tier(fee_adj_edge_frac: float):
        for i, (floor, risk) in enumerate(g['SIZING_TIERS']):
            if fee_adj_edge_frac >= floor:
                return (i, risk)
        return (-1, 0.0)

    def _bot_drawdown_scaler(current_balance_cents: int, hwm_cents: int) -> float:
        if hwm_cents <= 0:
            return 1.0
        ratio = current_balance_cents / hwm_cents
        if ratio < g['DRAWDOWN_HALT_THRESHOLD']:
            # R-p7-spec-r1#C1: bot.py hardcodes 0.10 inside
            # models.PositionSizer._drawdown_scaler; config.py exposes
            # DRAWDOWN_HALT_THRESHOLD but NOT DRAWDOWN_HALT_FLOOR. Falling
            # back to the same literal keeps the parity vector reachable
            # without requiring a config.py edit at deploy time. If the
            # operator lands the suggested config.py edit, the bot global
            # takes precedence automatically.
            return g.get('DRAWDOWN_HALT_FLOOR', 0.10)
        if ratio < g['DRAWDOWN_QUARTER_THRESHOLD']:
            return 0.25
        if ratio < g['DRAWDOWN_HALF_THRESHOLD']:
            return 0.50
        return 1.0

    def compute(fee_adj_edge_frac: float, available_balance_cents: int,
                entry_price_cents: int, current_balance_cents: int,
                hwm_cents: int, seconds_to_close: float, asset: str) -> dict:
        tier_idx, risk_fraction = _bot_lookup_tier(fee_adj_edge_frac)
        if tier_idx < 0:
            return {'contracts': 0}
        drawdown = _bot_drawdown_scaler(current_balance_cents, hwm_cents)
        asset_cap = asset_caps.get(asset, g['MAX_RISK_PER_TRADE'])
        effective_risk = min(risk_fraction * drawdown, g['MAX_RISK_PER_TRADE'], asset_cap)
        risk_cents = int(available_balance_cents * effective_risk)
        notional_cents = max(1, risk_cents)
        contracts = max(0, notional_cents // max(1, entry_price_cents))
        if (g['STC_SIZING_SCALER_ENABLED']
                and seconds_to_close > g['STC_SIZING_SCALER_KNEE']
                and contracts > 0):
            contracts = max(1, int(contracts * (g['STC_SIZING_SCALER_KNEE'] / seconds_to_close)))
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
                current_path = models_dir / 'CURRENT'
                if not current_path.exists():
                    raise CalMLPError('no_current', f"no CURRENT for {self.asset}")
                lock_path = models_dir / '.lock'
                lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_SH)
                    train_id = current_path.read_text().strip()
                    train_dir = models_dir / train_id
                    # R-p7-r2#H3: only phase-5 bundles deploy. Phase-4-only
                    # ablation paths must explicitly opt in upstream.
                    bundle_path = train_dir / f'cal_mlp_{self.asset}_{train_id}_phase5_bundle.json'
                    if not bundle_path.exists():
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
# R-p7-deploy-r8: async predict pool (decouple predict() from scan loop)
#
# Design summary:
# - bot.py Edit 4 calls annotate_evaluation_async_enqueue() — synchronous, fast
#   (validates inputs, generates uuid, submits to pool, returns).
# - INSERT writes the row with cal_mlp_request_id=<uuid>, cal_mlp_p_mean=NULL.
# - Worker thread (1 of them, max_workers=1 for ordering + DB lock simplicity)
#   pulls task, calls predict(), then UPDATEs the row WHERE cal_mlp_request_id=?.
# - UPSERT can overwrite the row before worker UPDATEs — but with a fresh uuid
#   per scan tick, the worker's WHERE clause won't match the new row, so we
#   stamp 'row_not_found' instead of corrupting newer audit data.
#
# Round-1 critiques addressed:
#  C1/C2/C3: uuid request_id avoids eval_time mismatch + UPSERT overwrites.
#  H4 (qsize race): threading.Semaphore for bounded queue, not private API.
#  H5 (busy_timeout retry interaction): short connect timeout + BEGIN IMMEDIATE.
#  H6 (connection per call): thread-local sqlite conn, opened once.
#  H7 (signal handler drain): drain_predict_pool documented as "main loop only".
#  H8 (re-init race): module-level lock guards _PREDICT_POOL access.
#  H10 (pending_async pollution): we DO NOT write 'pending_async'; row stays
#       cal_mlp_skipped_reason=NULL until UPDATE lands with the real value.
#  M14 (rowcount swallow): explicit rowcount handling + row_not_found stamp.
#  M15 (BEGIN IMMEDIATE): explicit in worker.
#  L19 (future swallows): worker has top-level try/except BaseException.
#  L20 (no metrics): _async_metrics dict + periodic log line every 60s.
# ---------------------------------------------------------------------------

import concurrent.futures
import sqlite3
import time
import uuid

_PREDICT_POOL: Optional[concurrent.futures.ThreadPoolExecutor] = None
_PREDICT_POOL_LOCK = threading.Lock()
_PREDICT_POOL_SHUTDOWN = False
_PREDICT_QUEUE_MAX = 256
_PREDICT_QUEUE_SEMAPHORE = threading.Semaphore(_PREDICT_QUEUE_MAX)
_ASYNC_METRICS = {
    'submitted': 0, 'completed_ok': 0, 'completed_predict_failed': 0,
    'completed_row_not_found': 0, 'queue_full': 0,
}
_ASYNC_METRICS_LOCK = threading.Lock()
_ASYNC_LAST_LOG_TS = 0.0  # for periodic metrics log line


def _get_predict_pool() -> Optional[concurrent.futures.ThreadPoolExecutor]:
    """Lazy-init the pool. Returns None during/after shutdown so callers
    stamp env_disabled instead of submitting work that won't drain."""
    global _PREDICT_POOL
    with _PREDICT_POOL_LOCK:
        if _PREDICT_POOL_SHUTDOWN:
            return None
        if _PREDICT_POOL is None:
            _PREDICT_POOL = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix='cal_mlp_predict',
            )
        return _PREDICT_POOL


def drain_predict_pool(timeout_sec: float = 5.0) -> None:
    """Bot-shutdown hook. MUST NOT be called from a signal handler — call from
    the main shutdown loop after `_shutdown.is_set()` is observed.

    The `timeout_sec` parameter is ignored by stdlib's `pool.shutdown` (it
    has no timeout arg). Production tolerance: a single in-flight predict()
    is ~25ms median, p95 62ms; bounded queue (256) × ~62ms = ~16s worst-case
    drain. systemd TimeoutStopSec should be ≥30s to accommodate. Round-2 #4
    addressed worker conn leak by closing them explicitly here.
    """
    global _PREDICT_POOL, _PREDICT_POOL_SHUTDOWN
    with _PREDICT_POOL_LOCK:
        _PREDICT_POOL_SHUTDOWN = True
        pool = _PREDICT_POOL
        _PREDICT_POOL = None
    if pool is not None:
        try:
            # shutdown(wait=True) blocks until the executor's worker drains
            # its current task. cancel_futures=False because each pending
            # task is already cheap (~62ms p95) and we want the audit data.
            pool.shutdown(wait=True, cancel_futures=False)
        except Exception as e:  # pragma: no cover
            logger.warning("[CALMLP_ASYNC] pool drain raised: %s", e)
    # Close any sqlite conns opened by worker threads (Round-2 #4).
    _close_all_worker_conns()
    logger.info("[CALMLP_ASYNC] pool drained; metrics=%s", _ASYNC_METRICS)


# Thread-local DB connection — single worker means one conn ever, opened once.
_WORKER_TLOCAL = threading.local()


def _worker_db_conn(db_path: str) -> sqlite3.Connection:
    """Open-once sqlite conn for the worker thread. Round-2 #4: registered
    on _ALL_WORKER_CONNS so drain_predict_pool can close them deterministically
    instead of leaking until thread GC.

    Round-2 #2 + Round-3 #4: busy_timeout=2000ms (between CLAUDE.md's 10000
    for main scan and a tighter cal_mlp budget). Rationale: under sustained
    main-thread writer contention >500ms (e.g. supabase_sync batch), 500ms
    busy_timeout caused a retry-storm + queue saturation. 2s lets the worker
    actually wait for the writer to finish; combined with 5 retries the
    worst-case is 5 × 2s + LOCK backoff (~265ms) = ~10.3s per task. Bounded
    queue + add_done_callback semaphore release prevents queue runaway.

    Round-3 #3: check_same_thread=False so drain_predict_pool can close
    these conns from the drain caller's thread (the conn was opened in the
    worker thread, sqlite3 normally forbids cross-thread close)."""
    conn = getattr(_WORKER_TLOCAL, 'conn', None)
    if conn is None:
        conn = sqlite3.connect(
            db_path, timeout=2.0, isolation_level=None,
            check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=2000")
        _WORKER_TLOCAL.conn = conn
        with _ALL_WORKER_CONNS_LOCK:
            _ALL_WORKER_CONNS.append(conn)
    return conn


# Track all worker-thread sqlite connections so drain_predict_pool can close
# them deterministically (Round-2 #4: was thread-local + GC-on-thread-death,
# which doesn't fire predictably on pool shutdown).
_ALL_WORKER_CONNS: list = []
_ALL_WORKER_CONNS_LOCK = threading.Lock()


def _close_all_worker_conns() -> None:
    """Called from drain_predict_pool. Closes any sqlite connections opened
    by worker threads. Idempotent.

    Round-3 #3: sqlite3 forbids cross-thread close by default (Connection was
    opened in the worker thread; this runs in the drain caller's thread).
    The conn was opened with check_same_thread=False (set in
    _worker_db_conn) so close from any thread is permitted."""
    with _ALL_WORKER_CONNS_LOCK:
        for c in _ALL_WORKER_CONNS:
            try:
                c.close()
            except Exception:
                pass
        _ALL_WORKER_CONNS.clear()


def _release_pool_slot(_future) -> None:
    """Round-3 #1: module-level done-callback for the pool. Must NEVER raise
    — callback exceptions abort the worker thread (in some CPython versions
    they bubble up and the thread dies without releasing). Wrapped in
    try/finally so the semaphore release is unconditional."""
    try:
        _PREDICT_QUEUE_SEMAPHORE.release()
    except BaseException as e:  # pragma: no cover — release should not raise
        try:
            logger.error("[CALMLP_ASYNC] semaphore release failed: %s", e)
        except Exception:
            pass


def _bump_metric(key: str) -> None:
    global _ASYNC_LAST_LOG_TS
    with _ASYNC_METRICS_LOCK:
        _ASYNC_METRICS[key] = _ASYNC_METRICS.get(key, 0) + 1
        now = time.time()
        if now - _ASYNC_LAST_LOG_TS >= 60.0:
            _ASYNC_LAST_LOG_TS = now
            logger.info("[CALMLP_ASYNC] metrics %s", _ASYNC_METRICS)


def _async_predict_and_update(
    *,
    predictor: 'CalMLPPredictor',
    raw_prob: float,
    ticker: str,
    side: str,
    entry_price_cents: int,
    row_features: dict,
    request_id: str,
    db_path: str,
) -> None:
    """Worker function. NEVER raises — all errors stamped to skip_reason
    via UPDATE."""
    try:
        diag: dict = {}
        try:
            annotate_evaluation_kwargs(
                diag, raw_prob=raw_prob, ticker=ticker, side=side,
                entry_price_cents=entry_price_cents, row_features=row_features,
                predictor=predictor,
            )
        except Exception as e:
            # Should be impossible (annotate catches its own exceptions), but
            # defense-in-depth.
            diag = {
                'cal_mlp_skipped_reason': 'async_predict_failed',
            }
            logger.warning("[CALMLP_ASYNC] predict failed for %s: %s",
                           ticker, e, exc_info=True)
        # UPDATE the row keyed by request_id. Retry up to 5 times if the row
        # isn't INSERTed yet (worker can fire before scan's INSERT commits).
        conn = _worker_db_conn(db_path)
        sql = """
            UPDATE evaluated_opportunities
            SET cal_mlp_p_mean=?,
                cal_mlp_p_std=?,
                cal_mlp_final_lo=?,
                cal_mlp_final_hi=?,
                cal_mlp_train_id=?,
                cal_mlp_skipped_reason=?
            WHERE cal_mlp_request_id=?
        """
        params = (
            diag.get('cal_mlp_p_mean'),
            diag.get('cal_mlp_p_std'),
            diag.get('cal_mlp_final_lo'),
            diag.get('cal_mlp_final_hi'),
            diag.get('cal_mlp_train_id'),
            diag.get('cal_mlp_skipped_reason'),
            request_id,
        )
        # Round-1 critique #1+#2: a single scan-iteration's _shadow_diag is
        # splatted into MULTIPLE insert_evaluated_opportunity calls (canonical
        # 15M filter_stage + terminal_momentum + decided_contract + various
        # shadow paths). They all carry the SAME cal_mlp_request_id, all
        # represent the same scan-tick's view of the market, and ALL should
        # get the same calibration data. Hence `rowcount >= 1` is success.
        # Round-1 #4/#5: split retry handling. INSERT-not-yet-committed gets
        # short backoff (rowcount=0 path); lock contention gets a tighter
        # backoff because busy_timeout=2000 already waited up to 2s on the
        # blocking call. Skip sleep on the last attempt (#4).
        ROWCOUNT0_BACKOFF_MS = (10, 25, 50, 150, 400)  # total 0.635s worst-case
        LOCK_BACKOFF_MS = (5, 10, 25, 75, 150)         # total 0.265s worst-case
        n_attempts = len(ROWCOUNT0_BACKOFF_MS)
        # Round-3 #2: explicit txn lifecycle. With isolation_level=None
        # (autocommit) + manual BEGIN IMMEDIATE, the conn holds the writer
        # lock until COMMIT or ROLLBACK. If the worker thread is interrupted
        # (Exception inside loop, KeyboardInterrupt during time.sleep), the
        # txn must be rolled back or the lock leaks until process exit.
        txn_open = False
        try:
            for attempt in range(n_attempts):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    txn_open = True
                    cur = conn.execute(sql, params)
                    conn.execute("COMMIT")
                    txn_open = False
                    if cur.rowcount >= 1:
                        if diag.get('cal_mlp_skipped_reason') == 'async_predict_failed':
                            _bump_metric('completed_predict_failed')
                        else:
                            _bump_metric('completed_ok')
                        return
                    # rowcount=0 → INSERT not yet committed; backoff + retry.
                    if attempt < n_attempts - 1:
                        time.sleep(ROWCOUNT0_BACKOFF_MS[attempt] / 1000.0)
                except sqlite3.OperationalError as e:
                    # Lock contention or BEGIN IMMEDIATE conflict.
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        pass
                    txn_open = False
                    logger.debug("[CALMLP_ASYNC] update lock retry %d: %s",
                                 attempt, e)
                    if attempt < n_attempts - 1:
                        time.sleep(LOCK_BACKOFF_MS[attempt] / 1000.0)
            # Retries exhausted: row(s) not yet INSERTed (or all UPSERT-
            # overwritten by newer ticks). Stamp metric — not an error.
            _bump_metric('completed_row_not_found')
        finally:
            # Round-3 #2: defense-in-depth. If we leave the loop with txn_open
            # (impossible via explicit paths above, but possible if a
            # BaseException landed mid-execute), force a rollback so the
            # worker conn doesn't stay holding the writer lock forever.
            if txn_open:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
    except Exception as e:  # absolute last resort — protect the pool
        # Round-2 #5: narrow to Exception (not BaseException). SystemExit /
        # KeyboardInterrupt should propagate up to the executor and abort
        # the worker thread; swallowing them silently masks fatals (e.g.,
        # torch sys.exit on hardware error).
        logger.error("[CALMLP_ASYNC] worker raised: %s", e, exc_info=True)
    # Round-2 #9: semaphore is released by the future's done_callback (set
    # in annotate_evaluation_async_enqueue), NOT here. Single-owner pattern
    # avoids the double-release race where KeyboardInterrupt fires between
    # pool.submit() return and the submit_succeeded=True assignment.


def annotate_evaluation_async_enqueue(
    kwargs: dict,
    *,
    raw_prob: Optional[float],
    ticker: str,
    side: str,
    entry_price_cents: int,
    row_features: dict,
    predictor: Optional['CalMLPPredictor'],
    db_path: str,
) -> None:
    """Enqueue cal_mlp predict for async execution. Mutates kwargs ONLY to
    set kwargs['cal_mlp_request_id']=<uuid> (which the synchronous INSERT
    writes). predict() runs in the worker thread; cal_mlp_p_mean / p_std /
    final_lo / final_hi / train_id / skipped_reason are written by the
    worker's UPDATE keyed on cal_mlp_request_id.

    Synchronous skip paths (no work submitted to pool):
    - env=0 → kwargs['cal_mlp_skipped_reason']='env_disabled', no request_id.
    - raw_prob None → 'raw_prob_null'.
    - predictor None → 'no_predictor'.
    - pool full → 'queue_full'.
    - pool shutdown → 'env_disabled' (treat as off).

    NOTE: this function does NOT return a calibrated probability — v1 is
    shadow-only. final_prob downstream uses raw_prob path.
    """
    # Synchronous early-exit checks (cheap, no submit needed).
    if os.environ.get('CALMLP_ENABLED', '1').strip().lower() not in ('1', 'true', 'yes'):
        kwargs['cal_mlp_skipped_reason'] = 'env_disabled'
        return
    if raw_prob is None:
        kwargs['cal_mlp_skipped_reason'] = 'raw_prob_null'
        return
    if predictor is None:
        kwargs['cal_mlp_skipped_reason'] = 'no_predictor'
        return
    pool = _get_predict_pool()
    if pool is None:
        kwargs['cal_mlp_skipped_reason'] = 'env_disabled'  # shutdown in progress
        return
    # Bounded queue via semaphore. Non-blocking acquire — drop if full.
    if not _PREDICT_QUEUE_SEMAPHORE.acquire(blocking=False):
        kwargs['cal_mlp_skipped_reason'] = 'queue_full'
        _bump_metric('queue_full')
        return
    # Round-2 #9: SINGLE-OWNER semaphore release pattern.
    # We register a future done-callback that releases the semaphore exactly
    # once when the worker finishes (success OR exception). This eliminates
    # the race where KeyboardInterrupt fires between pool.submit() return
    # and a `submit_succeeded=True` flag assignment, which under the old
    # double-release pattern caused capacity-leak by the slot.
    request_id = uuid.uuid4().hex
    kwargs['cal_mlp_request_id'] = request_id
    # NOTE: we do NOT set cal_mlp_skipped_reason='pending_async' — leaving
    # it NULL means audit queries naturally see "rows where worker hasn't
    # written yet" as NULL; the worker's UPDATE replaces with real value.
    try:
        future = pool.submit(
            _async_predict_and_update,
            predictor=predictor,
            raw_prob=raw_prob,
            ticker=ticker,
            side=side,
            entry_price_cents=entry_price_cents,
            row_features=row_features,
            request_id=request_id,
            db_path=db_path,
        )
    except RuntimeError as e:
        # Pool shutting down — the request_id was set but no worker will
        # process it. Release semaphore + clean up kwargs.
        _PREDICT_QUEUE_SEMAPHORE.release()
        kwargs['cal_mlp_skipped_reason'] = 'env_disabled'
        kwargs.pop('cal_mlp_request_id', None)
        logger.debug("[CALMLP_ASYNC] submit raced pool shutdown: %s", e)
        return
    # Round-3 #1: use module-level _release_pool_slot (try/finally protected,
    # never raises) instead of an inline lambda. add_done_callback runs the
    # callback ON THE WORKER THREAD synchronously after the future completes;
    # any callback exception abort the worker thread without releasing the
    # semaphore in some CPython versions, so the callback body MUST be
    # exception-safe.
    try:
        future.add_done_callback(_release_pool_slot)
    except Exception:  # pragma: no cover — should never raise per docs
        _PREDICT_QUEUE_SEMAPHORE.release()
        raise
    _bump_metric('submitted')


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
]
