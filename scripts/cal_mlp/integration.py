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
import hashlib
import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger('cal_mlp')

# R-p7-r2#M1: one-time module-level sys.path setup so we can import
# cal_mlp/* without per-call mutation. cal_mlp/* names don't shadow any
# bot.py top-level names (verified by grep), so the addition is safe.
_CAL_MLP_DIR = Path(__file__).resolve().parent
if str(_CAL_MLP_DIR) not in sys.path:
    sys.path.insert(0, str(_CAL_MLP_DIR))


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
    from sizing import (
        SIZING_TIERS, ASSET_MAX_RISK_PER_TRADE, MAX_RISK_PER_TRADE,
        DRAWDOWN_HALF_THRESHOLD, DRAWDOWN_QUARTER_THRESHOLD,
        DRAWDOWN_HALT_THRESHOLD, DRAWDOWN_HALT_FLOOR,
        STC_SIZING_SCALER_KNEE, STC_SIZING_SCALER_ENABLED,
    )
    from sim_pnl import (
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
    anti-deadlock rules. Both are required — busy_timeout governs whether
    the connection waits for a lock or fails immediately."""
    mode = conn.execute("PRAGMA journal_mode").fetchone()
    if mode is None or str(mode[0]).lower() != 'wal':
        raise CalMLPSchemaError(
            f"connection journal_mode={mode}; CLAUDE.md requires WAL"
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
        AND mean-impute the value via normstats."""
        # R-p7-impl#C6: take the lock for the duration so reads of self.*
        # are correctly synchronized under PEP 703 (free-threaded CPython).
        with self._lock:
            if not self._loaded:
                self._load()
        # R-p7-r2#M1: imports moved to module level.
        from features import (RAW_PROB_CLIP_EPS, MISSING_INDICATOR_COLS,
                                CONT_FEATURE_COLS, PRICE_BIN_CUTOFFS,
                                STC_BIN_CUTOFFS, MISSING_INDICATOR_SOURCE_MAP)
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
        row['ticker_id'] = self.vocab.get(str(ticker), 0)
        # R-p7-r2#H2: NaN/None values in CONT_FEATURE_COLS must flip the
        # matching *_missing indicator. Build an inverse map src→indicator.
        # R-p7-r4#M-INV: assert no source-column collisions (silent dropping
        # of a flag would break the missing-indicator contract).
        _src_to_ind = {v: k for k, v in MISSING_INDICATOR_SOURCE_MAP.items()}
        if len(_src_to_ind) != len(MISSING_INDICATOR_SOURCE_MAP):
            raise CalMLPSchemaError(
                "MISSING_INDICATOR_SOURCE_MAP inverse has fewer keys than the "
                "forward map — a source column maps to multiple indicators"
            )
        # Initialize all indicator cols to 0 (truly-present default).
        for col in MISSING_INDICATOR_COLS:
            row.setdefault(col, 0)
        # R-p7-r3#C1: previously this branch eagerly imputed with the
        # POST-transform mean and let apply_norm re-transform — wrong for
        # log/log1p columns. Fix: leave NaN, let apply_norm.fillna(mean)
        # run AFTER its transform step (normalize.py:171 path is correct).
        # We still flip the *_missing companion at this layer because that's
        # a Phase-7 feature engineering decision, not a normalize concern.
        normstats_map = self.normstats.get('stats', {})
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
                # Set to NaN so apply_norm's post-transform fillna runs.
                row[col] = float('nan')
                ind = _src_to_ind.get(col)
                if ind is not None:
                    row[ind] = 1
        df = pd.DataFrame([row])
        df_norm = apply_norm(df, self.normstats['stats'], CONT_FEATURE_COLS,
                              transforms=self.normstats.get('transforms', {}))

        # Forward through ensemble.
        try:
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
                for m in self.models:
                    _, p = m(**{k: batch[k] for k in FORWARD_KEYS})
                    preds.append(p.detach().cpu().numpy())
                stacked = np.stack(preds)  # [M, 1]
                p_mean = float(stacked.mean(axis=0)[0])
                p_std = float(stacked.std(axis=0, ddof=0)[0])
        except MemoryError as e:
            raise CalMLPError('predict_oom', str(e)) from e
        except RuntimeError as e:
            raise CalMLPError('predict_runtime', str(e)) from e

        # Apply conformal interval.
        result = predict_with_interval(
            p_mean, p_std, self.conformal,
            row_features={'price_tier': int(row['price_tier']),
                          'stc_bucket': int(row['stc_bucket']),
                          'vol_regime': int(row['vol_regime_int'])},
            entry_price_cents=entry_price_cents, side=side,
            market_blend_w=self.market_blend_w, mode='inference',
        )
        cal_prob, ens_std, final_lo, final_hi = result
        return cal_prob, ens_std, final_lo, final_hi


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
    if raw_prob is None:
        kwargs['cal_mlp_skipped_reason'] = 'raw_prob_null'
        return None
    # R-p7-impl#C9: explicit truthy set; '' / 'no' / '0' all disable.
    if os.environ.get('CALMLP_ENABLED', '1').strip().lower() not in ('1', 'true', 'yes'):
        kwargs['cal_mlp_skipped_reason'] = 'env_disabled'
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
