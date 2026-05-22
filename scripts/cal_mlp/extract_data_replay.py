#!/usr/bin/env python3
"""P2.1.a-3 (2026-05-13, ticket 86b9wuhhr) + Bit F (2026-05-21, ticket
86ba1wpck) — HYPE/DOGE/BNB replay-corpus extraction for v1.1 cal_mlp
retrain. Parallel pull path to extract_data.py.

Why a separate module
=====================

`scripts/cal_mlp/extract_data.py` reads from `evaluated_opportunities` and
demands 32 source columns (REQUIRED_SOURCE_COLS) — most are bot-state
features (market_price, NBBO, vol_regime, momentum, balance, depth, etc.).
The Phase 2 replay backfill table `historical_replay_calmlp` (Mac-only,
written by `scripts/backfill/crypto_replay_backfill.py`, ticket
`86b9wy7v3`, harness shipped at `fe75cf0`; renamed + BNB-widened in Bit F
`86ba1wpck` 2026-05-21) has 21 columns post-Bit-F (19 pre-fu2 + `threshold
REAL` ticket `86b9xtam7` 2026-05-13 + `spot_staleness_seconds REAL` ticket
`86ba1wpck` 2026-05-21) by design because the harness explicitly notes:

  > Bot-state features cannot be replayed accurately (market_price/NBBO,
  > depth, OFT, queue position, recent_bot_pnl, drawdown_scaler). These
  > are honest-NULL on replay rows.

A UNION view across the two tables would fail extract_data.py's NULL
contract (>30% NULL on any continuous feature → Phase2ContractError). So
HYPE/DOGE need a parallel pull path with a strict-subset recipe.

Recipe divergence (vs v1.1 production cfg_fp `345978797274721f`)
================================================================

DROPPED FROM RECIPE (cannot derive from replay corpus):
  - market_price       — replay's `replay_market(predictor, ...)` uses
                         entry_price_cents=0 sentinel; not stored
  - prob_breakeven_gap — 100% NULL in replay (no historical Kalshi
                         orderbook → can't derive `breakeven - market`)

KEPT (derived at extract time from replay's source cols):
  - seconds_to_close           = (close_time - evaluation_time).total_seconds()
  - spot_distance_to_strike_sigma = (spot - strike_$) / (sigma × √(stc/5) × 100),
                                   then `apply_sigma_winsor` — same formula
                                   as the bot's compute_derived_features
  - abs_spot_distance_to_strike_sigma = |sd|
  - time_decayed_proximity     = sd × (1 - stc / 900)
  - hour_sin / hour_cos        = pre-computed in replay table (canonical
                                 helper run at backfill time per harness
                                 docstring); we re-derive via
                                 `compute_hour_sin_cos` for lock-step
                                 verification

Identity:
  recipe fingerprint via `features.compute_cfg_fp_replay()` namespaced
  separately from `compute_cfg_fp()` so Phase 6 A/B refuses to compare
  replay-recipe bundles against production-recipe bundles. Pinned in
  `tests/contracts/test_p2_1_a_3_corpus_snapshots.py` anchor 7.

Source DB
=========

Reads from `data/replay/state.db` by default — the Mac-only DB written by
the Phase 2 backfill harness. **DO NOT** point at production state.db; it
has no `historical_replay_calmlp` table (verified: `mcp__kalshi-vps__query_db`
returns `[]` for `name LIKE '%replay%'` on VPS).

Bundle shape
============

Same JSON schema as `extract_data.py`'s output bundle (extract_bundle.json),
with these specific differences:

  - `state_db_snapshot_sha256` is NULL (replay corpus is not a sqlite
    snapshot; the source-of-truth is `data/replay/state.db` which is
    operator-curated, not a content-addressed snapshot)
  - `replay_db_path`            = relative path to `data/replay/state.db`
  - `replay_db_size_bytes`      = file size at extract time (audit field)
  - `replay_table_name`         = 'historical_replay_calmlp' (constant)
  - `replay_table_row_count_at_extract` = audit field
  - `provenance_filter`         = 'replay_phase2_v1' (single value;
                                  REPLAY_PROVENANCE_FILTER_CHOICES singleton)
  - bundle dirs land at the standard `data/cal_mlp/<asset>/<train_id>/`
    so train.py / validate.py find them under the established convention

Train.py asset-list extension (P2.1.b, SHIPPED)
================================================

This module produces extract bundles for HYPE/DOGE/BNB. train.py's
`--asset` choices was widened to 7 assets (`{BTC,ETH,SOL,XRP,HYPE,DOGE,BNB}`)
via P2.1.b + Bit C + Bit F. The reduced CONT_FEATURE_COLS_REPLAY recipe
is consumed via `compute_cfg_fp_replay()` namespace routing — bundles
whose cfg_fp matches `ea9c30477f844afa` route through the replay path.

Lock-step rule
==============

Per `bot/CLAUDE.md` "cal_mlp feature transforms (lock-step)" + Sprint
A.1b (ticket `86b9veppa`): hour_sin/cos AND sigma_winsor MUST route
through canonical helpers. This module imports `compute_hour_sin_cos`
(scalar) from `bot.helpers.derived_features` for verification of replay
table's pre-computed values, plus `apply_sigma_winsor` from
`features` for the derived sigma_distance clipping.

`tests/contracts/test_p2_1_a_3_corpus_snapshots.py` anchors 10a/b/c are
AST guards that catch inline drift in this module.

CLI
===

    python -m scripts.cal_mlp.extract_data_replay --asset HYPE
    python -m scripts.cal_mlp.extract_data_replay --asset DOGE \
        --replay-db data/replay/state.db \
        --folds 2 --train-days 30 --cal-days 7 --test-days 7 \
        --fold-offset-days 14
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

# Re-use canonical helpers + sister-module infrastructure. Lock-step
# imports per `tests/contracts/test_p2_1_a_3_corpus_snapshots.py`
# anchors 10a/b/c.
sys.path.insert(0, str(Path(__file__).parent))
import features  # noqa: E402  (read .SIGMA_WINSOR_ABS_CAP via module attr —
                  # mirrors extract_data.py R3-H1 pattern so monkey-patches
                  # in tests propagate through cfg_fp consistently)
from features import (  # noqa: E402
    ASSET_FLOORS_REPLAY,
    CONT_FEATURE_COLS_REPLAY,
    CONT_FEATURE_TRANSFORMS_REPLAY,
    DROP_PREDICATES_ORDER_REPLAY,
    RAW_PROB_CLIP_EPS,
    REPLAY_PROVENANCE_FILTER_CHOICES,
    REPLAY_RECIPE_NAMESPACE,
    SETTLEMENT_WHITELIST,
    SETTLEMENT_YES_VALUES,
    STC_BIN_CUTOFFS,
    apply_sigma_winsor,
    compute_cfg_fp_replay,
    compute_hour_features,
)
from normalize import fit_normstats, transform

# Canonical scalar hour helper for lock-step verification of replay table's
# pre-computed hour_sin/cos. compute_hour_features (DataFrame branch) is
# used during build_feature_frame; compute_hour_sin_cos (scalar) is the
# byte-identical sibling.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from bot.helpers.derived_features import compute_hour_sin_cos  # noqa: E402

# Reuse fold construction + atomic-IO + Phase2 exception hierarchy
# from extract_data.py — single source of truth for split semantics.
from extract_data import (  # noqa: E402
    Phase2ContractError,
    Phase2DBError,
    Phase2Error,
    Phase2LockError,
    Phase2SchemaError,
    Phase2WriteError,
    assert_walk_forward_temporal,
    assign_split,
    atomic_write_json,
    atomic_write_parquet,
    compute_fold_windows,
    enforce_ticker_disjoint,
    fsync_directory,
)


REPLAY_TABLE = "historical_replay_calmlp"

REPLAY_REQUIRED_SOURCE_COLS = (
    'ticker', 'evaluation_time', 'asset',
    'strike_cents', 'close_time', 'open_time',
    'raw_prob', 'calibrated_prob', 'blended_prob',
    'spot_at_evaluation', 'sigma_at_evaluation',
    'hour_sin', 'hour_cos',
    'prob_breakeven_gap', 'sigma_winsorize',
    'result', 'settlement_value',
    'data_provenance', 'replay_run_ts',
)

# P2.3.b-fu2 (2026-05-13, ticket 86b9xtam7): OPTIONAL columns — present in
# post-fix DBs (REAL `threshold` preserves sub-cent precision for DOGE),
# absent from pre-fix legacy bundles (HYPE 4847-row corpus has no column).
# `_check_schema` MUST NOT raise on a missing optional column; `pull_and_classify`
# probes the actual table at query time and only SELECTs columns that exist.
# `build_feature_frame` falls back to `strike_cents / 100.0` when `threshold`
# is NULL (HYPE legacy path stays intact).
REPLAY_OPTIONAL_SOURCE_COLS = ('threshold',)

REPLAY_ASSET_CHOICES = ('HYPE', 'DOGE', 'BNB')


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------

def _normalize_cutoff_end(s: Optional[str]) -> str:
    """Same canonical microsecond Z format as extract_data.py."""
    if s is None:
        dt = datetime.now(timezone.utc) - timedelta(days=1)
        return dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    formats = (
        '%Y-%m-%dT%H:%M:%S.%fZ',
        '%Y-%m-%dT%H:%M:%SZ',
        '%Y-%m-%dT%H:%M:%S.%f',
        '%Y-%m-%dT%H:%M:%S',
        '%Y-%m-%dT%H:%M',
        '%Y-%m-%d',
    )
    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        except ValueError:
            continue
    raise SystemExit(f"--cutoff-end could not parse: {s!r}")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="P2.1.a-3 cal_mlp replay-corpus extraction (HYPE/DOGE)")
    ap.add_argument('--asset', required=True, choices=list(REPLAY_ASSET_CHOICES))
    ap.add_argument('--folds', type=int, default=2,
                    help='default 2 — replay corpus is 53d per asset; v1 used 2 folds')
    ap.add_argument('--train-days', type=int, default=30)
    ap.add_argument('--cal-days', type=int, default=7)
    ap.add_argument('--test-days', type=int, default=7)
    ap.add_argument('--fold-offset-days', type=int, default=14)
    ap.add_argument('--out-dir', default=None,
                    help='default = data/cal_mlp/<asset>')
    ap.add_argument('--cutoff-end', default=None,
                    help='ISO timestamp (default: now - 24h)')
    ap.add_argument(
        '--replay-db', default='data/replay/state.db',
        help="path to the Mac-only Phase 2 replay DB (NOT production state.db)",
    )
    ap.add_argument(
        '--provenance-filter',
        choices=list(REPLAY_PROVENANCE_FILTER_CHOICES),
        default='replay_phase2_v1',
    )
    ap.add_argument('--n-train-min', type=int, default=500,
                    help='lower than production (2000) — replay corpus is ~5K rows '
                         'per asset and the recipe is 4 features (vs 8 in v1.1), '
                         'so per-fold n_train ≈ 1500 is acceptable; 500 is the '
                         'absolute floor below which conformal calibration becomes '
                         'unstable. (R1 MN1)')
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--verbose', action='store_true')
    return ap.parse_args()


def _setup_logging(quiet: bool, verbose: bool) -> None:
    level = logging.WARN if quiet else (logging.DEBUG if verbose else logging.INFO)
    logging.basicConfig(
        level=level,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S',
        stream=sys.stderr,
        force=True,
    )


# ---------------------------------------------------------------------------
# Lock acquisition (same pattern as extract_data.py)
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def acquire_extract_lock(out_dir: Path, asset: str):
    lock_path = out_dir / '.extract.lock'
    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        raise Phase2LockError(f"failed to open lock file {lock_path}: {e}") from e
    handle = os.fdopen(fd, 'r+')
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise Phase2LockError(
                f"extract lock {lock_path} is held — another extract process is running. "
                f"`fuser {lock_path}` to identify holder; do NOT delete the lock file."
            )
        try:
            yield handle
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        handle.close()


# ---------------------------------------------------------------------------
# DB connection + schema check (replay table)
# ---------------------------------------------------------------------------

def _open_ro_conn(db_path: str) -> sqlite3.Connection:
    """RO file-URI open — same form as extract_data.py for consistency."""
    from urllib.parse import quote
    abs_path = str(Path(db_path).resolve())
    if not Path(abs_path).exists():
        raise Phase2DBError(
            f"replay DB not found at {abs_path}. The Phase 2 replay corpus "
            f"lives at `data/replay/state.db` (Mac-only); production state.db "
            f"has no historical_replay_calmlp table."
        )
    conn = sqlite3.connect(f'file:{quote(abs_path, safe="/")}?mode=ro', uri=True)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def _check_schema(conn: sqlite3.Connection, db_path: str) -> set[str]:
    """Verify required schema cols are present; return the set of OPTIONAL
    cols that DO exist in this DB (for `pull_and_classify` to SELECT).

    REQUIRED cols must all be present — missing → Phase2SchemaError.
    OPTIONAL cols (P2.3.b-fu2 `threshold`) are absent from pre-fix legacy
    HYPE bundles; we report which ones exist so the SELECT only asks for
    columns the DB actually has.
    """
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({REPLAY_TABLE})").fetchall()}
    if not cols:
        raise Phase2SchemaError(
            f"{REPLAY_TABLE} table missing in {db_path}. Was Phase 2 backfill "
            f"(scripts/backfill/crypto_replay_backfill.py) run against this DB?"
        )
    missing = [c for c in REPLAY_REQUIRED_SOURCE_COLS if c not in cols]
    if missing:
        raise Phase2SchemaError(
            f"P2.1.a-3 replay schema mismatch — columns expected but not in {REPLAY_TABLE}: {missing}. "
            f"Re-run backfill or update REPLAY_REQUIRED_SOURCE_COLS + cfg_fp_replay."
        )
    return {c for c in REPLAY_OPTIONAL_SOURCE_COLS if c in cols}


# ---------------------------------------------------------------------------
# DROP_PREDICATES — replay-recipe sequential exclusive bucketing
# ---------------------------------------------------------------------------

def _parse_iso_z(s: Optional[str]) -> Optional[datetime]:
    """Parse the two ISO-8601 forms used in `historical_replay_calmlp`:
        - `'%Y-%m-%dT%H:%M:%SZ'`         (the harness writes this for
                                          15-min-aligned open/close times)
        - `'%Y-%m-%dT%H:%M:%S.%fZ'`      (microsecond form for completeness)
    Returns None if `s` is None or unparseable. Centralized so both the
    drop-predicate path and the cutoff comparison use byte-identical
    parsing — avoiding the ISO-string-compare bug where lexicographic
    ordering puts `'Z' (0x5A) > '.' (0x2E)`, dropping rows whose timestamp
    has no microseconds when the cutoff has microseconds (R1 M1)."""
    if s is None:
        return None
    for fmt in ('%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S.%fZ'):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _classify_drop(row: sqlite3.Row, asset: str, cutoff_end_dt: datetime) -> Optional[str]:
    """Return the FIRST predicate the row fails, or None if it passes.
    Order matches `DROP_PREDICATES_ORDER_REPLAY` in features.py.

    `cutoff_end_dt` is a `datetime` (NOT an ISO string) — pass the parsed
    cutoff once at the call-site to avoid per-row ISO parsing of the
    cutoff side. R1 M1 fix: replace lexicographic ISO compare with
    `datetime` numeric compare so rows with no-microsecond timestamps
    don't get spuriously dropped on same-second collisions with the
    microsecond-formatted cutoff."""
    if row['evaluation_time'] is None:
        return 'null_evaluation_time'
    if row['close_time'] is None:
        return 'null_close_time'
    if row['spot_at_evaluation'] is None:
        return 'null_spot_at_evaluation'
    if row['sigma_at_evaluation'] is None:
        return 'null_sigma_at_evaluation'
    if row['strike_cents'] is None:
        return 'null_strike_cents'
    if (row['result'] or '') not in SETTLEMENT_WHITELIST:
        return 'non_yes_no_result'
    # Datetime-numeric compare (R1 M1 fix). `replay_market` writes settlement
    # before storing; settled_after_cutoff uses close_time as a proxy (no
    # explicit settled_time on replay rows).
    close_dt = _parse_iso_z(row['close_time'])
    if close_dt is None:
        return 'null_close_time'
    if close_dt >= cutoff_end_dt:
        return 'settled_after_cutoff'
    # Derived stc — defensive against close_time<=evaluation_time corruption
    eval_dt = _parse_iso_z(row['evaluation_time'])
    if eval_dt is None:
        return 'null_evaluation_time'
    stc = (close_dt - eval_dt).total_seconds()
    if stc <= 0:
        return 'non_positive_seconds_to_close'
    return None


def pull_and_classify(
    conn: sqlite3.Connection,
    asset: str,
    cutoff_end: str,
    *,
    provenance_filter: str,
    optional_cols_present: Optional[set[str]] = None,
) -> tuple[list[dict], dict[str, int], int]:
    """Single-pass pull of `WHERE asset=? AND data_provenance=?` rows. Returns
    (kept_rows, drops_dict, source_total).

    `optional_cols_present` — set of OPTIONAL columns the DB actually has,
    returned by `_check_schema`. Pre-fix DBs lack `threshold`; `kept` dicts
    omit the key entirely so downstream `build_feature_frame` falls back to
    `strike_cents / 100.0` (HYPE legacy path)."""
    if provenance_filter not in REPLAY_PROVENANCE_FILTER_CHOICES:
        raise ValueError(
            f"provenance_filter must be one of {REPLAY_PROVENANCE_FILTER_CHOICES}; "
            f"got {provenance_filter!r}"
        )
    cutoff_end_dt = _parse_iso_z(cutoff_end)
    if cutoff_end_dt is None:
        raise ValueError(
            f"cutoff_end must be ISO-8601 UTC ('%Y-%m-%dT%H:%M:%SZ' or "
            f"'%Y-%m-%dT%H:%M:%S.%fZ'); got {cutoff_end!r}"
        )
    optional_cols_present = optional_cols_present or set()
    select_col_tuple = tuple(REPLAY_REQUIRED_SOURCE_COLS) + tuple(
        c for c in REPLAY_OPTIONAL_SOURCE_COLS if c in optional_cols_present
    )
    select_cols = ', '.join(select_col_tuple)
    sql = (
        f"SELECT {select_cols} FROM {REPLAY_TABLE} "
        f"WHERE asset = ? AND data_provenance = ? "
        f"ORDER BY evaluation_time, ticker"
    )
    drops: dict[str, int] = {k: 0 for k in DROP_PREDICATES_ORDER_REPLAY}
    kept: list[dict] = []
    source_total = 0
    try:
        cur = conn.execute(sql, (asset, provenance_filter))
        for row in cur:
            source_total += 1
            bucket = _classify_drop(row, asset, cutoff_end_dt)
            if bucket is None:
                kept.append(dict(row))
            else:
                drops[bucket] += 1
    except sqlite3.OperationalError as e:
        raise Phase2DBError(
            f"replay DB read failed (busy_timeout? schema drift?): {e}"
        ) from e
    return kept, drops, source_total


# ---------------------------------------------------------------------------
# Bucketization + feature engineering
# ---------------------------------------------------------------------------

def _digitize(values: np.ndarray, cutoffs: list[int]) -> np.ndarray:
    return np.digitize(values, cutoffs, right=True).astype(np.int8)


def build_feature_frame(rows: list[dict]) -> pd.DataFrame:
    """Build the post-bucketization, post-feature-engineering DataFrame for
    replay rows. Differences vs extract_data.build_feature_frame:
      - No price_tier (no market_price column in replay schema)
      - Derives `seconds_to_close` from (close_time - evaluation_time)
      - Derives `spot_distance_to_strike_sigma` from spot/sigma/strike/stc
        using the same formula as bot.helpers.derived_features
      - hour_sin/cos: re-derive via canonical `compute_hour_features`
        (DataFrame branch) AND verify equality with stored values for
        lock-step (raise on mismatch beyond float tolerance)
    """
    df = pd.DataFrame(rows)

    # ── Derived: seconds_to_close ────────────────────────────────────────
    eval_dt = pd.to_datetime(df['evaluation_time'], utc=True, format='ISO8601')
    close_dt = pd.to_datetime(df['close_time'], utc=True, format='ISO8601')
    stc = (close_dt - eval_dt).dt.total_seconds().astype(np.float32)
    df['seconds_to_close'] = stc

    # ── Derived: spot_distance_to_strike_sigma + winsor + symmetric/proximity ──
    # Formula mirrors bot.helpers.derived_features.compute_derived_features
    # (raw form pre-winsor): (spot - strike_$) / (sigma * sqrt(stc/5) * 100)
    # where sigma is per-5s vol stdev of log returns.
    spot = df['spot_at_evaluation'].astype(np.float64).to_numpy()
    sigma = df['sigma_at_evaluation'].astype(np.float64).to_numpy()
    # P2.3.b-fu2 (2026-05-13, ticket 86b9xtam7): prefer REAL `threshold`,
    # fall back to `strike_cents / 100.0` for legacy HYPE bundles whose
    # rows pre-date the schema migration. Per
    # `kb/findings/replay-backfill-strike-precision-bug-may13.md`, HYPE
    # corpus is correct as-is (asset price magnitude makes integer cents
    # adequate); DOGE corpus needs full regen against the post-fix schema.
    legacy_strike_dollars = df['strike_cents'].astype(np.float64).to_numpy() / 100.0
    if 'threshold' in df.columns:
        threshold_real = df['threshold'].astype(np.float64).to_numpy()
        # NaN → fall back to legacy. `np.where` handles the per-row choice.
        strike_dollars = np.where(np.isnan(threshold_real), legacy_strike_dollars, threshold_real)
    else:
        strike_dollars = legacy_strike_dollars
    stc_arr = stc.to_numpy().astype(np.float64)
    # Defensive: clamp denominators, accept honest-NULL on degenerate.
    sigma_safe = np.where(sigma > 0, sigma, np.nan)
    stc_safe = np.where(stc_arr > 0, stc_arr, np.nan)
    sigma_term = sigma_safe * np.sqrt(stc_safe / 5.0) * 100.0
    sd_raw = (spot - strike_dollars) / sigma_term
    # Apply canonical winsor element-wise (NaN-safe; mirrors apply_sigma_winsor
    # scalar branch). Use vector clip with the SAME cap exposed by the helper
    # to maintain serve/extract lock-step. The cap-constant SoT is enforced by
    # `tests/contracts/test_calmlp_lockstep.py` anchor 1; redundant runtime
    # probes were removed per R1 MN2.
    cap = features.SIGMA_WINSOR_ABS_CAP
    sd = np.clip(sd_raw, -cap, cap).astype(np.float32)
    df['spot_distance_to_strike_sigma'] = sd
    df['abs_spot_distance_to_strike_sigma'] = np.abs(sd)
    # time_decayed_proximity: sd × (1 - stc/900). 900 = 15-min window seconds.
    df['time_decayed_proximity'] = (sd * (1.0 - stc.to_numpy() / 900.0)).astype(np.float32)

    # ── Hour features: re-derive via canonical helper, verify against
    # backfill-stored values for lock-step ──
    # Replay rows store hour_sin/cos computed by `compute_hour_sin_cos` at
    # backfill time. Re-derive here and verify byte-equality (modulo float
    # tolerance) — catches any future drift between backfill helper and
    # extract helper.
    hour_int = eval_dt.dt.hour.astype(np.float32) % 24.0
    h_sin_recomputed, h_cos_recomputed = compute_hour_features(hour_int)
    h_sin_stored = df['hour_sin'].astype(np.float64).to_numpy()
    h_cos_stored = df['hour_cos'].astype(np.float64).to_numpy()
    # Per-row max-abs-diff. NaN-safe via np.nanmax + ignore where stored is NaN.
    sin_diff = np.nanmax(np.abs(np.asarray(h_sin_recomputed) - h_sin_stored)) if len(h_sin_stored) else 0.0
    cos_diff = np.nanmax(np.abs(np.asarray(h_cos_recomputed) - h_cos_stored)) if len(h_cos_stored) else 0.0
    if sin_diff > 1e-6 or cos_diff > 1e-6:
        raise Phase2ContractError(
            f"hour_sin/cos drift between backfill-stored and canonical helper: "
            f"sin_diff={sin_diff:.2e}, cos_diff={cos_diff:.2e}. Backfill harness "
            f"may have been run against an outdated compute_hour_sin_cos."
        )
    # Use the canonically-recomputed values in the parquet (single source of truth)
    df['hour_sin'] = np.asarray(h_sin_recomputed, dtype=np.float32)
    df['hour_cos'] = np.asarray(h_cos_recomputed, dtype=np.float32)

    # ── STC bucket (no price_tier — no market_price in replay) ──
    df['stc_bucket'] = _digitize(stc.to_numpy().astype(np.float64), STC_BIN_CUTOFFS)

    # ── Outcome label ──
    # Replay's `replay_market` always evaluates YES side (per harness docstring).
    # Outcome = 1 iff result=='yes' (settlement was YES).
    result_yes = df['result'].astype(str).isin(SETTLEMENT_YES_VALUES)
    df['outcome'] = result_yes.astype(np.int8)
    df['result_yes_int'] = df['outcome']  # alias for downstream consistency
    df['side_int'] = np.int8(1)           # always YES side in replay
    df['method_output_raw'] = df['raw_prob'].astype(np.float32)
    rp = df['raw_prob'].astype(np.float64).to_numpy()
    rp_c = np.clip(rp, RAW_PROB_CLIP_EPS, 1.0 - RAW_PROB_CLIP_EPS)
    df['logit_raw_prob_clipped'] = np.log(rp_c / (1.0 - rp_c)).astype(np.float32)
    df['calibrated_prob_audit'] = df['calibrated_prob'].astype(np.float32)
    return df


# ---------------------------------------------------------------------------
# Per-cell audit stats (1D STC-bucket; no price_tier in replay)
# ---------------------------------------------------------------------------

def compute_per_stc_bucket_stats(
    train_df: pd.DataFrame,
    cal_df: pd.DataFrame,
    test_df: pd.DataFrame,
) -> dict:
    """1D per-stc_bucket stats. Replay has no market_price → no price_tier."""
    cells: dict = {}
    all_buckets = set()
    for d in (train_df, cal_df, test_df):
        if not d.empty:
            for sb in d['stc_bucket'].unique():
                all_buckets.add(int(sb))
    for sb in sorted(all_buckets):
        sub_train = train_df[train_df['stc_bucket'] == sb]
        sub_cal = cal_df[cal_df['stc_bucket'] == sb]
        sub_test = test_df[test_df['stc_bucket'] == sb]
        cells[f"stc_bucket={sb}"] = {
            'n_train': int(len(sub_train)),
            'n_cal': int(len(sub_cal)),
            'n_test': int(len(sub_test)),
            'train_positive_rate': float(sub_train['outcome'].mean()) if len(sub_train) else None,
            'cal_positive_rate': float(sub_cal['outcome'].mean()) if len(sub_cal) else None,
            'test_positive_rate': float(sub_test['outcome'].mean()) if len(sub_test) else None,
            'train_mean_method_output': float(sub_train['method_output_raw'].mean()) if len(sub_train) else None,
        }
    return cells


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> dict:
    asset = args.asset
    cutoff_end = _normalize_cutoff_end(args.cutoff_end)
    cutoff_end_dt = datetime.strptime(cutoff_end, '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc)
    cfg_fp = compute_cfg_fp_replay(provenance_filter=args.provenance_filter)

    project_root = Path(__file__).resolve().parents[2]
    if args.out_dir:
        out_dir = (Path(args.out_dir) if Path(args.out_dir).is_absolute()
                   else (project_root / args.out_dir)).resolve()
    else:
        out_dir = (project_root / 'data' / 'cal_mlp' / asset).resolve()
    if not out_dir.is_relative_to(project_root):
        raise SystemExit(
            f"--out-dir must be inside project root {project_root}; got {out_dir}"
        )
    out_dir.mkdir(parents=True, exist_ok=True)

    replay_db_path = (Path(args.replay_db) if Path(args.replay_db).is_absolute()
                      else (project_root / args.replay_db)).resolve()
    replay_db_size = replay_db_path.stat().st_size if replay_db_path.exists() else 0

    with acquire_extract_lock(out_dir, asset):
        conn = _open_ro_conn(str(replay_db_path))
        data_version_at_open: int = 0
        data_version_at_close: int = 0
        try:
            try:
                data_version_at_open = int(conn.execute("PRAGMA data_version").fetchone()[0])
            except sqlite3.OperationalError as e:
                raise Phase2DBError(f"PRAGMA data_version failed: {e}") from e
            data_version_at_close = data_version_at_open
            optional_cols_present = _check_schema(conn, str(replay_db_path))
            replay_table_total = int(conn.execute(f"SELECT COUNT(*) FROM {REPLAY_TABLE}").fetchone()[0])
            logging.info("[extract_replay] pulling rows for asset=%s ...", asset)
            kept, drops, source_total = pull_and_classify(
                conn, asset, cutoff_end,
                provenance_filter=args.provenance_filter,
                optional_cols_present=optional_cols_present,
            )
            try:
                data_version_at_close = int(conn.execute("PRAGMA data_version").fetchone()[0])
            except sqlite3.OperationalError:
                pass
        finally:
            conn.close()

        n_kept = len(kept)
        n_dropped = sum(drops.values())
        if source_total != n_kept + n_dropped:
            raise Phase2ContractError(
                f"drops invariant violation: source_total={source_total} != "
                f"n_kept={n_kept} + sum(drops)={n_dropped}"
            )
        if n_kept == 0:
            raise Phase2ContractError(
                f"0 rows after filter — likely wrong asset or empty replay corpus. "
                f"drops={drops}"
            )
        logging.info("[extract_replay] kept=%d dropped=%d source_total=%d", n_kept, n_dropped, source_total)

        # train_id sha8 = recipe-hash of (asset|cfg_fp|cutoff|fold params)
        sha8_input = (
            f"{asset}|{cfg_fp}|{cutoff_end}|{args.folds}|"
            f"{args.train_days}|{args.cal_days}|{args.test_days}|{args.fold_offset_days}"
        )
        sha8 = hashlib.sha256(sha8_input.encode()).hexdigest()[:8]
        train_id = f"{cutoff_end}-{sha8}"
        train_dir = out_dir / train_id
        train_dir.mkdir(parents=True, exist_ok=True)
        for stale in list(train_dir.glob('*.tmp-*')) + list(out_dir.glob('CURRENT.tmp-*')):
            try:
                stale.unlink()
                logging.info("[extract_replay] cleaned stale tmp: %s", stale)
            except OSError:
                pass

        df = build_feature_frame(kept)

        oldest_ts = pd.to_datetime(df['evaluation_time'], utc=True, format='ISO8601').min()
        min_required = args.train_days + args.cal_days + args.test_days + args.fold_offset_days * (args.folds - 1)
        available_days = (cutoff_end_dt - oldest_ts).days
        if available_days < min_required:
            raise Phase2ContractError(
                f"replay corpus has {available_days}d, need ≥{min_required}d for "
                f"folds={args.folds} train={args.train_days} cal={args.cal_days} "
                f"test={args.test_days} offset={args.fold_offset_days}."
            )

        fold_windows = compute_fold_windows(
            cutoff_end_dt, args.folds, args.train_days, args.cal_days,
            args.test_days, args.fold_offset_days,
        )

        unique_tickers = sorted(df['ticker'].astype(str).unique())
        ticker_vocab = {'<UNK>': 0}
        ticker_vocab.update({t: i + 1 for i, t in enumerate(unique_tickers)})
        df['ticker_id'] = df['ticker'].astype(str).map(ticker_vocab).astype(np.int32)
        df['is_unk_ticker'] = np.int8(0)

        per_fold_audit: list[dict] = []
        fold_artifacts: list[dict] = []
        tmps_to_rename: list[tuple[Path, Path]] = []
        all_per_cell: dict = {}

        try:
            for fw in fold_windows:
                k = fw['fold']
                logging.info(
                    "[extract_replay] fold %d: train=[%s,%s) cal=[%s,%s) test=[%s,%s)",
                    k, fw['train_start'].date(), fw['train_end'].date(),
                    fw['cal_start'].date(), fw['cal_end'].date(),
                    fw['test_start'].date(), fw['test_end'].date(),
                )
                fold_df = df.copy()
                fold_df['split'] = assign_split(fold_df, fw)
                fold_df, n_boundary_reassigned = enforce_ticker_disjoint(fold_df)
                assert_walk_forward_temporal(fold_df, fw)
                fold_df = fold_df[fold_df['split'].notna()].reset_index(drop=True)
                fold_df['fold'] = np.int8(k)
                tr = fold_df[fold_df['split'] == 'train'].reset_index(drop=True)
                ca = fold_df[fold_df['split'] == 'cal'].reset_index(drop=True)
                te = fold_df[fold_df['split'] == 'test'].reset_index(drop=True)
                if len(te) < 50:
                    raise Phase2ContractError(
                        f"fold {k}: n_test={len(te)} < 50 (conformal quantile unstable)"
                    )
                if len(tr) < args.n_train_min:
                    raise Phase2ContractError(
                        f"fold {k}: n_train={len(tr)} < {args.n_train_min}"
                    )
                # NULL contract per CONT_FEATURE_COLS_REPLAY (>30% NULL on train)
                for col in CONT_FEATURE_COLS_REPLAY:
                    n_null_train = int(tr[col].isna().sum())
                    if n_null_train / max(1, len(tr)) > 0.30:
                        if n_null_train == len(tr):
                            raise Phase2ContractError(
                                f"fold {k}: column {col!r} 100% NULL on train — "
                                f"recently added without backfill"
                            )
                        raise Phase2ContractError(
                            f"fold {k}: column {col!r} {n_null_train/len(tr):.1%} NULL on train (>30%)"
                        )
                normstats = fit_normstats(tr, CONT_FEATURE_COLS_REPLAY, CONT_FEATURE_TRANSFORMS_REPLAY)
                normstats_payload = {
                    'fold': k, 'asset': asset, 'cutoff_end': cutoff_end,
                    'n_train': int(len(tr)), 'ddof': 1,
                    'transforms': dict(CONT_FEATURE_TRANSFORMS_REPLAY),
                    'cont_feature_cols': list(CONT_FEATURE_COLS_REPLAY),
                    'stats': normstats,
                }
                imputed_pct = {'train': {}, 'cal': {}, 'test': {}}
                for col in CONT_FEATURE_COLS_REPLAY:
                    if normstats[col].get('_no_zscore'):
                        for label, sp in (('train', tr), ('cal', ca), ('test', te)):
                            imputed_pct[label][col] = float(sp[col].isna().sum()) / max(1, len(sp))
                        continue
                    tname = CONT_FEATURE_TRANSFORMS_REPLAY.get(col, 'identity')
                    for label, sp in (('train', tr), ('cal', ca), ('test', te)):
                        tx = transform(sp[col], tname)
                        imputed_pct[label][col] = float(tx.isna().sum()) / max(1, len(sp))
                per_cell = compute_per_stc_bucket_stats(tr, ca, te)
                small_cell_warnings = [
                    f"{cell_key} n_test={cell['n_test']} <50"
                    for cell_key, cell in per_cell.items()
                    if 0 < cell['n_test'] < 50
                ]
                fold_parquet_path = train_dir / f"fold{k}.parquet"
                table = pa.Table.from_pandas(fold_df, preserve_index=False)
                fold_tmp, fold_sha = atomic_write_parquet(table, fold_parquet_path)
                tmps_to_rename.append((fold_tmp, fold_parquet_path))
                normstats_path = train_dir / f"normstats_fold{k}.json"
                ns_tmp, ns_sha = atomic_write_json(normstats_payload, normstats_path)
                tmps_to_rename.append((ns_tmp, normstats_path))
                fold_artifacts.append({
                    'fold': k,
                    'parquet_path': fold_parquet_path.name,
                    'parquet_sha256': fold_sha,
                    'normstats_path': normstats_path.name,
                    'normstats_sha256': ns_sha,
                    'n_train': int(len(tr)),
                    'n_cal': int(len(ca)),
                    'n_test': int(len(te)),
                    'test_window_start': fw['test_start'].isoformat(),
                    'test_window_end': fw['test_end'].isoformat(),
                })
                per_fold_audit.append({
                    'fold': k,
                    'test_window_start': fw['test_start'].isoformat(),
                    'test_window_end': fw['test_end'].isoformat(),
                    'n_train': int(len(tr)),
                    'n_cal': int(len(ca)),
                    'n_test': int(len(te)),
                    'imputed_pct': imputed_pct,
                    'small_cell_warnings': small_cell_warnings,
                    'per_cell': per_cell,
                    'n_rows_reassigned_at_boundary': n_boundary_reassigned,
                })
                all_per_cell[f"fold{k}"] = per_cell

            ticker_stats = {
                'n_unique_tickers': len(unique_tickers),
                'mean_rows_per_ticker': float(len(df) / max(1, len(unique_tickers))),
                'pct_tickers_with_only_one_row': float(
                    (df.groupby('ticker').size() == 1).sum() / max(1, len(unique_tickers))
                ),
            }

            vocab_path = train_dir / 'ticker_vocab.json'
            vocab_payload = {
                'asset': asset,
                'vocab': ticker_vocab,
                'n_unique': len(unique_tickers),
            }
            vocab_tmp, vocab_sha = atomic_write_json(vocab_payload, vocab_path)
            tmps_to_rename.append((vocab_tmp, vocab_path))

            audit_path = train_dir / 'extract_audit.json'
            audit_payload = {
                'asset': asset,
                'train_id': train_id,
                'cfg_fp': cfg_fp,
                'recipe_namespace': REPLAY_RECIPE_NAMESPACE,
                'cutoff_end': cutoff_end,
                'data_version_at_open': int(data_version_at_open),
                'data_version_at_close': int(data_version_at_close),
                'replay_db_path': str(replay_db_path.relative_to(project_root))
                                  if replay_db_path.is_relative_to(project_root)
                                  else str(replay_db_path),
                'replay_db_size_bytes': int(replay_db_size),
                'replay_table_name': REPLAY_TABLE,
                'replay_table_row_count_at_extract': replay_table_total,
                'source_total_rows_for_asset': source_total,
                'source_total_rows_post_filter': n_kept,
                'drops': drops,
                'per_fold': per_fold_audit,
                'ticker_stats': ticker_stats,
                'asset_floor_applied': ASSET_FLOORS_REPLAY[asset],
                'provenance_filter': args.provenance_filter,
                'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            }
            audit_tmp, audit_sha = atomic_write_json(audit_payload, audit_path)
            tmps_to_rename.append((audit_tmp, audit_path))

            bundle_path = train_dir / 'extract_bundle.json'
            bundle_payload = {
                'phase': 2,
                'schema_version': 2,
                'asset': asset,
                'train_id': train_id,
                'cfg_fp': cfg_fp,
                'recipe_namespace': REPLAY_RECIPE_NAMESPACE,
                'cutoff_end': cutoff_end,
                'provenance_filter': args.provenance_filter,
                'data_version_at_open': int(data_version_at_open),
                'data_version_at_close': int(data_version_at_close),
                '_path_resolution': 'basenames_relative_to_bundle_dir',
                'ticker_vocab_path': vocab_path.name,
                'ticker_vocab_sha256': vocab_sha,
                'audit_path': audit_path.name,
                'audit_sha256': audit_sha,
                'eval_fold_artifacts': fold_artifacts,
                'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
                'extract_data_replay_py_sha256': hashlib.sha256(
                    Path(__file__).read_bytes()
                ).hexdigest(),
                # Replay corpus is NOT a sqlite snapshot — see module docstring.
                # Audit fields below pin the source DB by path + size (not hash).
                'state_db_snapshot_sha256': None,
                'replay_db_path': audit_payload['replay_db_path'],
                'replay_db_size_bytes': int(replay_db_size),
                'replay_table_name': REPLAY_TABLE,
            }
            bundle_tmp, bundle_sha = atomic_write_json(bundle_payload, bundle_path)
            tmps_to_rename.append((bundle_tmp, bundle_path))

            renamed: list[Path] = []
            try:
                for tmp, final in tmps_to_rename:
                    os.replace(tmp, final)
                    renamed.append(final)
                fsync_directory(train_dir)
                # NOTE: deliberately NOT writing CURRENT here — the production
                # `extract_data.py` writes CURRENT to point at v1.1 production
                # bundles, and replay bundles must NOT silently rotate it. The
                # caller (P2.1.b train.py) reads the replay bundle via explicit
                # train_id, not via CURRENT.
            except Exception as e:
                cleanup_errors = []
                for final in reversed(renamed):
                    try:
                        if final.exists():
                            final.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError as ce:
                        cleanup_errors.append((final, ce))
                raise Phase2WriteError(
                    f"rename phase failed: {e}; cleanup_errors={cleanup_errors}"
                ) from e

            return bundle_payload
        except Exception:
            for tmp, _ in tmps_to_rename:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except (FileNotFoundError, OSError):
                    pass
            raise


def main() -> None:
    args = parse_args()
    _setup_logging(args.quiet, args.verbose)
    try:
        bundle = run(args)
    except Phase2Error as e:
        logging.error("Phase2Error: %s", e)
        sys.exit(e.exit_code)
    except Exception:
        logging.exception("unexpected error")
        sys.exit(1)
    summary = {
        'train_id': bundle['train_id'],
        'cfg_fp': bundle['cfg_fp'],
        'asset': bundle['asset'],
        'cutoff_end': bundle['cutoff_end'],
        'n_folds': len(bundle['eval_fold_artifacts']),
        'replay_db_path': bundle['replay_db_path'],
    }
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
