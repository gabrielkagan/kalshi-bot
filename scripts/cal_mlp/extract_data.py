#!/usr/bin/env python3
"""
P2 Phase 2: per-asset data extraction → train_id-namespaced parquet bundle.

Implements `kb-research/bot/p2-phase2-data-extraction.md` (converged at R5,
95 substantive critiques addressed across 5 rounds).

Usage:
    python -m scripts.cal_mlp.extract_data --asset SOL [options]

Outputs:
    data/cal_mlp/<asset>/
    ├── CURRENT                         # text file: train_id of latest valid bundle
    ├── <train_id>/
    │   ├── extract_bundle.json         # the manifest (gate)
    │   ├── extract_audit.json          # diagnostic counters
    │   ├── ticker_vocab.json
    │   ├── fold0.parquet, fold1.parquet, fold2.parquet
    │   └── normstats_fold0.json, normstats_fold1.json, normstats_fold2.json
    └── .extract.lock
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).parent))
# A.7 (86b9vejrj): shared snapshot helper. Bit 11.2 (2026-05-12) moved
# `_state_db_snapshot.py` from `scripts/` → `scripts/ops/`; both paths are
# inserted defensively so the lazy import inside run() never misses.
# P2.1.a-3 (2026-05-13, ticket 86b9wuhhr) surfaced the broken import.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ops'))

import features  # noqa: E402  (R3-H1: import the module so mutations to
                  # features.SIGMA_WINSOR_ABS_CAP are observed at call time;
                  # `from features import SIGMA_WINSOR_ABS_CAP` would bake
                  # in the import-time value and silently diverge from
                  # cfg_fp under monkey-patch / hot-reload).
from features import (  # noqa: E402
    ASSET_FLOORS,
    BLEED_CELL,
    CONT_FEATURE_COLS,
    CONT_FEATURE_TRANSFORMS,
    DROP_PREDICATES_ORDER,
    GLOBAL_MIN_ENTRY_PRICE,
    MISSING_INDICATOR_COLS,
    MISSING_INDICATOR_SOURCE_MAP,
    PRICE_BIN_CUTOFFS,
    PROVENANCE_FILTER_CHOICES,
    RAW_PROB_CLIP_EPS,
    SETTLEMENT_WHITELIST,
    SETTLEMENT_YES_VALUES,
    STC_BIN_CUTOFFS,
    asset_min_price,
    compute_cfg_fp,
    compute_hour_features,
)
from normalize import fit_normstats, transform


# ---------------------------------------------------------------------------
# Exit-code hierarchy (R-p2-spec-r1#R3-OPS#C12)
# ---------------------------------------------------------------------------

class Phase2Error(RuntimeError):
    exit_code: int = 1


class Phase2DBError(Phase2Error):
    exit_code = 2


class Phase2ContractError(Phase2Error):
    exit_code = 3


class Phase2LockError(Phase2Error):
    exit_code = 4


class Phase2WriteError(Phase2Error):
    exit_code = 5


class Phase2SchemaError(Phase2Error):
    exit_code = 6


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------

def _normalize_cutoff_end(s: Optional[str]) -> str:
    """Parse and canonicalize cutoff_end to bot.py's microsecond Z format
    (`%Y-%m-%dT%H:%M:%S.%fZ`)."""
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
    ap = argparse.ArgumentParser(description="Phase 2 cal_mlp data extraction")
    # Bit B (86ba0jn0w, 2026-05-19) — widened from 4 assets to add
    # HYPE/DOGE. Allows HYPE/DOGE LIVE rows in `evaluated_opportunities`
    # to flow through the production recipe. The HYPE/DOGE REPLAY corpus
    # is consumed by the parallel `scripts/cal_mlp/extract_data_replay.py`
    # extractor (P2.1.a-3 ticket `86b9wuhhr`) — no UNION here.
    ap.add_argument(
        '--asset',
        required=True,
        choices=['BTC', 'ETH', 'SOL', 'XRP', 'HYPE', 'DOGE'],
    )
    ap.add_argument('--folds', type=int, default=3)
    ap.add_argument('--train-days', type=int, default=60)
    ap.add_argument('--cal-days', type=int, default=15)
    ap.add_argument('--test-days', type=int, default=15)
    ap.add_argument('--fold-offset-days', type=int, default=30)
    ap.add_argument('--out-dir', default=None,
                    help='default = data/cal_mlp/<asset>')
    ap.add_argument('--cutoff-end', default=None,
                    help='ISO timestamp (default: now - 24h)')
    ap.add_argument('--db', default='state.db')
    ap.add_argument('--include-sub-floor', action='store_true')
    # v2 ablation: SQL-side filter on `data_provenance`. Per
    # `kb/decisions/v2-cal-mlp-deploy-runbook-may03.md`:
    #   live_only      → WHERE data_provenance = 'live_ws'
    #   full_dataset   → WHERE data_provenance IN ('live_ws','backfill_60s_inputs')
    #   all (default)  → no SQL filter (backwards-compat with v1 reproduction)
    # Filter is baked into cfg_fp so live_only and full_dataset bundles have
    # distinct identities.
    ap.add_argument(
        '--provenance-filter',
        choices=list(PROVENANCE_FILTER_CHOICES),
        default='all',
    )
    ap.add_argument('--n-train-min', type=int, default=2000)
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--verbose', action='store_true')
    # A.7 (Sprint A Bit 7, ticket 86b9vejrj) — immutable snapshot binding.
    # If --snapshot-sha256 is given, the extract reads from the
    # decompressed snapshot at data/cal_mlp/_snapshots/<sha8>/state.db.*
    # instead of --db. If --auto-snapshot is given without --snapshot-sha256,
    # the extract takes a fresh snapshot of --db first. Without either flag,
    # the extract reads --db directly (legacy behavior; bundle records null
    # for the snapshot fields).
    snap_grp = ap.add_mutually_exclusive_group()
    snap_grp.add_argument(
        '--snapshot-sha256', default=None,
        help='SHA-256 hex of an existing snapshot in --snap-root; A.7',
    )
    snap_grp.add_argument(
        '--auto-snapshot', action='store_true',
        help='Take a fresh snapshot of --db before extracting; A.7',
    )
    ap.add_argument(
        '--snap-root', type=str, default=None,
        help='Snapshot root (default: data/cal_mlp/_snapshots); A.7',
    )
    return ap.parse_args()


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(quiet: bool, verbose: bool) -> None:
    """R2#C10: force=True so re-config works if a parent process pre-configured logging."""
    level = logging.WARN if quiet else (logging.DEBUG if verbose else logging.INFO)
    logging.basicConfig(
        level=level,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S',
        stream=sys.stderr,
        force=True,
    )


# ---------------------------------------------------------------------------
# Lock acquisition (R-p2-spec-r1#R3-OPS#C4 + R2-OPS#C16)
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def acquire_extract_lock(out_dir: Path, asset: str):
    """LOCK_EX | LOCK_NB on data/cal_mlp/<asset>/.extract.lock. Auto-released
    on process death by POSIX flock semantics."""
    lock_path = out_dir / '.extract.lock'
    # R2#C4: catch OSError from os.open and re-raise as Phase2LockError.
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
# Schema check + data SELECT (single connection)
# ---------------------------------------------------------------------------

REQUIRED_SOURCE_COLS = (
    'ticker', 'evaluation_time', 'asset', 'side', 'strategy', 'product_type',
    'market_price', 'seconds_to_close', 'vol_regime', 'z_score',
    'yes_spread_cents', 'calibrated_prob',
    'raw_prob', 'breakeven_wr', 'fee_adjusted_edge', 'kelly_f',
    'is_weekend', 'hour_of_day_utc', 'day_of_week',
    'market_result', 'settled_time', 'available_balance_cents',
    'spot_momentum_60s_bps', 'spot_momentum_5m_bps',
    'spot_realized_range_15m_bps',
    'btc_spot_change_5m_bps', 'btc_realized_vol_15m',
    'window_max_buf_pct', 'window_min_buf_pct', 'minutes_above_strike',
    'spot_distance_to_strike_sigma', 'prob_breakeven_gap',
    'spot_coinbase_kraken_gap_bps', 'kalshi_flow_depth_velocity',
    # G-6 (shipped 2026-05-03) — required so v2 ablation can SQL-filter
    # by training cohort and downstream Phase 6 can scope held-out to
    # `data_provenance='live_ws'`. Pre-G6 state.db will fail _check_schema.
    'data_provenance',
)


def _open_ro_conn(db_path: str) -> sqlite3.Connection:
    """R2#C1: URL-encode the path to prevent ?/&/# from being interpreted as
    URI parameters. R2#C12: drop journal_mode=WAL (no-op on RO connection)."""
    from urllib.parse import quote
    abs_path = str(Path(db_path).resolve())
    conn = sqlite3.connect(f'file:{quote(abs_path, safe="/")}?mode=ro', uri=True)
    # WAL is set by the writer (bot.py); RO connections don't need it. busy_timeout
    # DOES apply to RO connections.
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def _check_schema(conn: sqlite3.Connection, db_path: str) -> None:
    """R2#C2: take db_path explicitly so the error message doesn't rely on
    a fragile second connection query."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(evaluated_opportunities)").fetchall()}
    if not cols:
        raise Phase2SchemaError("evaluated_opportunities table missing or empty schema")
    missing = [c for c in REQUIRED_SOURCE_COLS if c not in cols]
    if missing:
        raise Phase2SchemaError(
            f"Phase 2 schema mismatch — columns expected by extract but not in state.db: {missing}. "
            f"Either bot.py removed them (update extract spec + bump cfg_fp) or backfill incomplete. "
            f"Run `sqlite3 {db_path} 'PRAGMA table_info(evaluated_opportunities)'`."
        )


# ---------------------------------------------------------------------------
# DROP_PREDICATES — sequential exclusive bucketing (R-p2-spec-r4#C2)
# ---------------------------------------------------------------------------

def _classify_drop(row: sqlite3.Row, asset: str, asset_floor: int, cutoff_end: str) -> Optional[str]:
    """Return the FIRST predicate the row fails, or None if it passes.

    R1#C7 + R2#C17: extends the spec's drop predicates with side / stc
    NULL-checks because those columns must be non-null for outcome and
    bucketization. Adding them here changes cfg_fp via DROP_PREDICATES_ORDER.
    """
    if (row['product_type'] or '') != '15m':
        return 'non_15m_product_type'
    if (row['ticker'] or '').startswith('SPORTS-'):
        return 'sports_ticker'
    if row['market_price'] is None:
        return 'null_market_price'
    if row['market_price'] <= 0:
        return 'non_positive_market_price'
    if row['market_price'] < asset_floor:
        return 'below_asset_floor'
    if row['raw_prob'] is None:
        return 'null_raw_prob'
    if row['evaluation_time'] is None:
        return 'null_evaluation_time'
    if (row['market_result'] or '') not in SETTLEMENT_WHITELIST:
        return 'non_yes_no_result'
    if row['settled_time'] is None:
        return 'null_settled_time'
    if row['settled_time'] >= cutoff_end:
        return 'settled_after_cutoff'
    if row['side'] is None or row['side'] not in ('yes', 'no'):
        return 'null_or_invalid_side'
    if row['seconds_to_close'] is None:
        return 'null_seconds_to_close'
    return None


def pull_and_classify(
    conn: sqlite3.Connection,
    asset: str,
    cutoff_end: str,
    asset_floor: int,
    *,
    provenance_filter: str = 'all',
) -> tuple[list[dict], dict[str, int], int]:
    """Single-pass pull of `WHERE asset=?` rows. Bucket-classifies each row
    via DROP_PREDICATES; returns (kept_rows, drops_dict, source_total).

    `provenance_filter` adds an SQL-side `data_provenance` clause:
        live_only     → AND data_provenance = 'live_ws'
        full_dataset  → AND data_provenance IN ('live_ws','backfill_60s_inputs')
        all (default) → no provenance clause (backwards-compat)
    Unknown values raise ValueError. `source_total` reflects only rows the
    SQL pull returned — it does NOT count rows excluded by the provenance
    filter (those never reached the bucketing stage).

    Bit B (86ba0jn0w, 2026-05-19): NO behavior change here. The widened
    `--asset` choices in `parse_args` let HYPE/DOGE *live* rows in
    `evaluated_opportunities` flow through this function unchanged. The
    HYPE/DOGE *replay* corpus (`historical_replay_calmlp`) is consumed by
    the parallel extractor `scripts/cal_mlp/extract_data_replay.py`
    (P2.1.a-3, ticket `86b9wuhhr`, shipped commit `3a9d690a`) with its
    own `compute_cfg_fp_replay` namespace — a UNION here would regress
    on that two-extractor architecture and trip
    `build_feature_frame`'s NULL contract once `market_price` lands.
    """
    if provenance_filter not in PROVENANCE_FILTER_CHOICES:
        raise ValueError(
            f"provenance_filter must be one of {PROVENANCE_FILTER_CHOICES}; "
            f"got {provenance_filter!r}"
        )
    select_cols = ', '.join(REQUIRED_SOURCE_COLS) + ', rowid'
    where_clauses = ['asset = ?']
    params: list = [asset]
    if provenance_filter == 'live_only':
        where_clauses.append('data_provenance = ?')
        params.append('live_ws')
    elif provenance_filter == 'full_dataset':
        where_clauses.append('data_provenance IN (?, ?)')
        params.extend(['live_ws', 'backfill_60s_inputs'])
    sql = (
        f"SELECT {select_cols} FROM evaluated_opportunities "
        f"WHERE {' AND '.join(where_clauses)} "
        f"ORDER BY evaluation_time, ticker, rowid"
    )
    drops: dict[str, int] = {k: 0 for k in DROP_PREDICATES_ORDER}
    kept: list[dict] = []
    source_total = 0
    try:
        cur = conn.execute(sql, tuple(params))
        for row in cur:
            source_total += 1
            bucket = _classify_drop(row, asset, asset_floor, cutoff_end)
            if bucket is None:
                kept.append(dict(row))
            else:
                drops[bucket] += 1
    except sqlite3.OperationalError as e:
        # R2#C3: preserve documented exit-code contract for DB issues.
        raise Phase2DBError(
            f"state.db read failed (busy_timeout exceeded? schema drift?): {e}"
        ) from e
    return kept, drops, source_total


# ---------------------------------------------------------------------------
# Bucketization + feature engineering
# ---------------------------------------------------------------------------

def _digitize(values: np.ndarray, cutoffs: list[int]) -> np.ndarray:
    # right=True per features.py; boundary values land in the LOWER bin.
    return np.digitize(values, cutoffs, right=True).astype(np.int8)


def build_feature_frame(rows: list[dict]) -> pd.DataFrame:
    """Build the post-bucketization, post-feature-engineering DataFrame.
    Continuous features in CONT_FEATURE_COLS still in raw value space —
    apply_norm runs later (per-fold)."""
    df = pd.DataFrame(rows)
    # Buckets
    df['price_tier'] = _digitize(df['market_price'].astype(float).to_numpy(), PRICE_BIN_CUTOFFS)
    df['stc_bucket'] = _digitize(df['seconds_to_close'].astype(float).to_numpy(), STC_BIN_CUTOFFS)
    df['vol_regime_int'] = (df['vol_regime'].astype(str) == 'elevated').astype(np.int8)
    df['side_int'] = (df['side'].astype(str) == 'yes').astype(np.int8)
    # Outcome label (R-p2-spec-r1#R1-C1 + R1#C15)
    result_yes = df['market_result'].isin(SETTLEMENT_YES_VALUES)
    df['result_yes_int'] = result_yes.astype(np.int8)
    df['outcome'] = (result_yes == (df['side'] == 'yes')).astype(np.int8)
    # method_output_raw + logit clipped skip term
    df['method_output_raw'] = df['raw_prob'].astype(np.float32)
    rp = df['raw_prob'].astype(np.float64).to_numpy()
    rp_c = np.clip(rp, RAW_PROB_CLIP_EPS, 1.0 - RAW_PROB_CLIP_EPS)
    df['logit_raw_prob_clipped'] = np.log(rp_c / (1.0 - rp_c)).astype(np.float32)
    df['calibrated_prob_audit'] = df['calibrated_prob'].astype(np.float32)
    # Engineered features
    # R-p7-deploy-r11: winsorize sigma at ±SIGMA_WINSOR_ABS_CAP BEFORE
    # deriving abs() and time_decayed_proximity. At terminal STC (T→0) the
    # raw sigma denominator collapses, producing ±3000+ outliers in prod
    # data. R3 (CRITICAL): centralized in features.apply_sigma_winsor so
    # post_hoc_processor + should_block_tm96 apply the same clip at serve
    # time. R3-H1: read constant via module attr so monkey-patches in tests
    # propagate through cfg_fp consistently.
    cap = features.SIGMA_WINSOR_ABS_CAP
    sd_raw = df['spot_distance_to_strike_sigma'].astype(np.float32)
    sd = sd_raw.clip(lower=-cap, upper=cap)
    df['spot_distance_to_strike_sigma'] = sd
    df['abs_spot_distance_to_strike_sigma'] = sd.abs()
    stc = df['seconds_to_close'].astype(np.float32)
    df['time_decayed_proximity'] = sd * (1.0 - stc / 900.0)
    # Cyclic hour (sin/cos) — canonical helper, lock-step with serve path
    h = df['hour_of_day_utc'].astype(np.float32) % 24.0
    df['hour_sin'], df['hour_cos'] = compute_hour_features(h)
    # Log-balance — column NAME is the post-transform identity; the VALUE
    # written here is raw cents. The `log_cents_to_dollars` transform
    # (`log1p(x/100)`) is applied later by `normalize.apply_norm` via
    # `CONT_FEATURE_TRANSFORMS`. Two contract concerns (R-p2-r1):
    #   C1: the misleading name. Renaming cascades through normstats keys
    #       and the parquet schema, so we keep the name and document here.
    #   C2: log1p(x/100) is NaN for x <= -100. Settlement-race edges can
    #       produce transiently negative balances in `state.db` (see memory
    #       on Settlement Watermark Race). Clamp at 0 so the train mean
    #       imputation captures it as "near-empty" rather than NaN-imputed.
    # R-p2-r12#H1: log_balance_dollars value is clipped here; the source
    # column `available_balance_cents` in the parquet remains UNCLIPPED.
    # Auditors reading raw balance see negatives (Settlement-race edges);
    # only the feature pipeline sees the clipped form.
    bal = df['available_balance_cents'].astype(np.float32).clip(lower=0.0)
    df['log_balance_dollars'] = bal
    # Audit columns (kept untransformed for Phase 6)
    df['breakeven_wr_audit'] = df['breakeven_wr'].astype(np.float32)
    df['fee_adjusted_edge_audit'] = df['fee_adjusted_edge'].astype(np.float32)
    df['kelly_f_audit'] = df['kelly_f'].astype(np.float32)
    # Strategy NULL → 'unknown'
    df['strategy'] = df['strategy'].fillna('unknown').astype(str)
    # Missing indicators (BEFORE imputation; on raw source values)
    for indicator_col, source_col in MISSING_INDICATOR_SOURCE_MAP.items():
        df[indicator_col] = df[source_col].isna().astype(np.int8)
    return df


# ---------------------------------------------------------------------------
# Walk-forward fold construction (R-p2-spec-r4 redrawn)
# ---------------------------------------------------------------------------

def compute_fold_windows(
    cutoff_end_dt: datetime,
    folds: int,
    train_days: int,
    cal_days: int,
    test_days: int,
    fold_offset_days: int,
) -> list[dict]:
    """Returns a list of {fold, train_start, train_end, cal_start, cal_end,
    test_start, test_end} dicts. Test slices march FORWARD; fold 0 is OLDEST."""
    out = []
    T = cutoff_end_dt
    for k in range(folds):
        test_end = T - timedelta(days=(folds - 1 - k) * fold_offset_days)
        test_start = test_end - timedelta(days=test_days)
        cal_end = test_start
        cal_start = cal_end - timedelta(days=cal_days)
        train_end = cal_start
        train_start = train_end - timedelta(days=train_days)
        out.append({
            'fold': k,
            'train_start': train_start, 'train_end': train_end,
            'cal_start': cal_start, 'cal_end': cal_end,
            'test_start': test_start, 'test_end': test_end,
        })
    return out


def assign_split(df: pd.DataFrame, fold_window: dict) -> pd.Series:
    """Returns a Series of {'train','cal','test', None} for each row of df.
    None = row falls outside this fold's spans.
    R-p2-r1#H2: assert masks are non-overlapping. By construction (cal_start
    == train_end, test_start == cal_end, all half-open) they should be —
    but the assert turns any future window-math bug into a loud failure
    instead of a silent last-write-wins."""
    et = pd.to_datetime(df['evaluation_time'], utc=True)
    out = pd.Series(np.full(len(df), None, dtype=object), index=df.index)
    train_mask = (et >= fold_window['train_start']) & (et < fold_window['train_end'])
    cal_mask = (et >= fold_window['cal_start']) & (et < fold_window['cal_end'])
    test_mask = (et >= fold_window['test_start']) & (et < fold_window['test_end'])
    overlap = (train_mask.astype(int) + cal_mask.astype(int) + test_mask.astype(int)).max()
    if overlap > 1:
        raise Phase2ContractError(
            f"split masks overlap ({overlap} matches) for fold "
            f"train=[{fold_window['train_start']}, {fold_window['train_end']}) "
            f"cal=[{fold_window['cal_start']}, {fold_window['cal_end']}) "
            f"test=[{fold_window['test_start']}, {fold_window['test_end']})"
        )
    out.loc[train_mask] = 'train'
    out.loc[cal_mask] = 'cal'
    out.loc[test_mask] = 'test'
    return out


def enforce_ticker_disjoint(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Per ticker, assign all in-span rows of that ticker to the split where
    that ticker's LATEST in-span row falls. Out-of-span rows keep `split=None`.

    R1#C4 + R1#C5 + R2#C9: prior version mass-promoted out-of-span rows into
    splits unconditionally — restricted to in-span rows.
    R2-impl#C1: returns `n_reassigned` = count of in-span rows whose split
    actually changed (e.g., a train row that got moved to test because the
    same ticker had a later test row). The previous "n_dropped to None"
    metric was structurally always-zero by construction.
    """
    if df.empty:
        return df, 0
    pre_split = df['split'].copy()
    in_span_mask = pre_split.notna()
    if not in_span_mask.any():
        return df, 0
    et = pd.to_datetime(df['evaluation_time'], utc=True)
    sorted_df = df.assign(_et=et).sort_values(['ticker', '_et'])
    # `.last()` after sort returns the latest-in-span split per ticker.
    last_split = sorted_df[sorted_df['split'].notna()].groupby('ticker')['split'].last()
    new_split = pre_split.copy()
    new_split.loc[in_span_mask] = df.loc[in_span_mask, 'ticker'].map(last_split)
    # Count rows whose split CHANGED — those are the boundary-cost reassignments.
    reassigned_mask = in_span_mask & (new_split != pre_split)
    df = df.copy()
    df['split'] = new_split
    return df, int(reassigned_mask.sum())


def assert_walk_forward_temporal(df: pd.DataFrame, fold_window: dict) -> None:
    """R-p2-r1#H1: ticker-disjoint enforcement can promote a train-window row
    into the test split (because the same ticker had a later test row).
    Walk-forward semantics require test_min_ts >= cal_max_ts >= train_max_ts.
    Reassignment keeps tickers disjoint but can violate the temporal invariant.
    Raise Phase2ContractError if the post-disjoint splits violate that order."""
    if df.empty:
        return
    et = pd.to_datetime(df['evaluation_time'], utc=True)
    df_t = df.assign(_et=et)
    train_rows = df_t[df_t['split'] == 'train']
    cal_rows = df_t[df_t['split'] == 'cal']
    test_rows = df_t[df_t['split'] == 'test']
    train_max = train_rows['_et'].max() if not train_rows.empty else None
    cal_min = cal_rows['_et'].min() if not cal_rows.empty else None
    cal_max = cal_rows['_et'].max() if not cal_rows.empty else None
    test_min = test_rows['_et'].min() if not test_rows.empty else None
    # Reassignment reorders ticker-rows; allow up to a per-ticker rescue
    # window equal to the cal_days span. Beyond that flag a hard violation.
    rescue_window = (fold_window['cal_end'] - fold_window['cal_start'])
    if train_max is not None and cal_min is not None:
        if cal_min < train_max - rescue_window:
            raise Phase2ContractError(
                f"post-disjoint train_max={train_max} > cal_min={cal_min} "
                f"by more than rescue_window={rescue_window}; ticker-disjoint "
                f"enforcement violated walk-forward temporal invariant"
            )
    if cal_max is not None and test_min is not None:
        if test_min < cal_max - rescue_window:
            raise Phase2ContractError(
                f"post-disjoint cal_max={cal_max} > test_min={test_min} "
                f"by more than rescue_window={rescue_window}; ticker-disjoint "
                f"enforcement violated walk-forward temporal invariant"
            )


# ---------------------------------------------------------------------------
# Per-cell audit stats
# ---------------------------------------------------------------------------

def compute_per_cell_stats(
    train_df: pd.DataFrame,
    cal_df: pd.DataFrame,
    test_df: pd.DataFrame,
) -> dict:
    """Per-cell (price_tier, stc_bucket) stats for audit JSON."""
    cells = {}
    all_cells = set()
    for d in (train_df, cal_df, test_df):
        if not d.empty:
            for (pt, sb), _ in d.groupby(['price_tier', 'stc_bucket']):
                all_cells.add((int(pt), int(sb)))
    for (pt, sb) in sorted(all_cells):
        sub_train = train_df[(train_df['price_tier'] == pt) & (train_df['stc_bucket'] == sb)]
        sub_cal = cal_df[(cal_df['price_tier'] == pt) & (cal_df['stc_bucket'] == sb)]
        sub_test = test_df[(test_df['price_tier'] == pt) & (test_df['stc_bucket'] == sb)]
        cell = {
            'n_train': int(len(sub_train)),
            'n_cal': int(len(sub_cal)),
            'n_test': int(len(sub_test)),
            'train_positive_rate': float(sub_train['outcome'].mean()) if len(sub_train) else None,
            'cal_positive_rate': float(sub_cal['outcome'].mean()) if len(sub_cal) else None,
            'test_positive_rate': float(sub_test['outcome'].mean()) if len(sub_test) else None,
            'train_mean_method_output': float(sub_train['method_output_raw'].mean()) if len(sub_train) else None,
        }
        cells[f"({pt},{sb})"] = cell
    return cells


# ---------------------------------------------------------------------------
# Atomic write helpers
# ---------------------------------------------------------------------------

def atomic_write_parquet(table: pa.Table, final_path: Path) -> tuple[Path, str]:
    """Write to tmp + fsync + return tmp path and sha256 of written bytes."""
    tmp = final_path.with_suffix(
        final_path.suffix + f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )
    with open(tmp, 'wb') as f:
        pq.write_table(table, f)
        f.flush()
        os.fsync(f.fileno())
    sha = hashlib.sha256(tmp.read_bytes()).hexdigest()
    return tmp, sha


def atomic_write_json(payload: dict, final_path: Path) -> tuple[Path, str]:
    tmp = final_path.with_suffix(
        final_path.suffix + f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )
    raw = json.dumps(payload, indent=2, sort_keys=True, default=str).encode()
    with open(tmp, 'wb') as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    sha = hashlib.sha256(raw).hexdigest()
    return tmp, sha


def fsync_directory(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


_HEX = set('0123456789abcdef')


def _resolve_db_and_snapshot_meta(args: argparse.Namespace, project_root: Path) -> tuple[str, dict]:
    """A.7: resolve the DB path the extract should read from + snapshot metadata.

    Returns (db_path, snapshot_meta_for_bundle). snapshot_meta has 4 keys
    (all None on legacy runs without --snapshot-sha256/--auto-snapshot):
        state_db_snapshot_sha256
        state_db_snapshot_path                  (relative to project_root, or
                                                 absolute when --snap-root is
                                                 outside project_root)
        state_db_snapshot_size_bytes_uncompressed
        state_db_snapshot_compression           ('zstd' | 'gzip' | null)
    """
    import snapshot_state_db as _snap_cli  # resolved by module-top sys.path

    snap_root = Path(args.snap_root) if args.snap_root else (
        project_root / 'data' / 'cal_mlp' / '_snapshots'
    )

    meta_for_bundle: dict = {
        'state_db_snapshot_sha256': None,
        'state_db_snapshot_path': None,
        'state_db_snapshot_size_bytes_uncompressed': None,
        'state_db_snapshot_compression': None,
    }

    if args.auto_snapshot:
        sha = _snap_cli.take(src=Path(args.db), snap_root=snap_root)
    elif args.snapshot_sha256:
        sha = args.snapshot_sha256
        if not (isinstance(sha, str) and len(sha) == 64 and all(c in _HEX for c in sha)):
            raise SystemExit(
                f"--snapshot-sha256 must be 64 lowercase hex chars; got {sha!r}"
            )
    else:
        return args.db, meta_for_bundle

    sha8_dir = snap_root / sha[:8]
    meta_path = sha8_dir / 'snapshot_meta.json'
    if not meta_path.exists():
        raise Phase2DBError(
            f"snapshot {sha[:8]} not found in {snap_root}. "
            f"Either take a fresh snapshot with `python -m scripts.cal_mlp.snapshot_state_db take` "
            f"and pass its sha256, or use --auto-snapshot to take and extract in one step."
        )
    snap_meta = json.loads(meta_path.read_text())
    if snap_meta.get('sha256') != sha:
        raise Phase2DBError(
            f"snapshot_meta.json sha256 mismatch in {sha8_dir}: "
            f"expected {sha}, got {snap_meta.get('sha256')}"
        )

    scratch_dir = snap_root / '_scratch'
    decompressed = _snap_cli.decompress_for_extract(snap_root, sha, scratch_dir)

    compressed_file = _snap_cli.snapshot_path_for(snap_root, sha)
    if compressed_file is None:
        raise Phase2DBError(
            f"snapshot compressed file missing for sha={sha[:8]} under {snap_root}"
        )
    try:
        rel_path = str(compressed_file.relative_to(project_root))
    except ValueError:
        # --snap-root is outside project_root; record absolute path.
        rel_path = str(compressed_file.resolve())

    meta_for_bundle.update({
        'state_db_snapshot_sha256': sha,
        'state_db_snapshot_path': rel_path,
        'state_db_snapshot_size_bytes_uncompressed': int(snap_meta.get('size_bytes_uncompressed', 0)),
        'state_db_snapshot_compression': snap_meta.get('compression'),
    })
    return str(decompressed), meta_for_bundle


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> dict:
    """Returns the bundle JSON dict on success."""
    asset = args.asset
    cutoff_end = _normalize_cutoff_end(args.cutoff_end)
    cutoff_end_dt = datetime.strptime(cutoff_end, '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc)
    asset_floor = asset_min_price(asset, include_sub_floor=args.include_sub_floor)
    cfg_fp = compute_cfg_fp(
        include_sub_floor=args.include_sub_floor,
        provenance_filter=args.provenance_filter,
    )

    # R2#C8 + R2-impl#C2: anchor project_root to script location (not cwd —
    # cron and worktree invocations may run from any cwd). Resolve --out-dir
    # against project_root and use is_relative_to() to avoid prefix-collision.
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

    # A.7: bind the extract to a hashed snapshot of state.db. Without
    # --snapshot-sha256 / --auto-snapshot, falls through to reading args.db
    # directly (legacy; bundle records null for snapshot fields).
    db_path, snapshot_meta_for_bundle = _resolve_db_and_snapshot_meta(args, project_root)

    with acquire_extract_lock(out_dir, asset):
        conn = _open_ro_conn(db_path)
        # R1#C14: pre-bind data_version_at_close so an early DB error
        # doesn't cause UnboundLocalError when audit JSON is built.
        data_version_at_open: int = 0
        data_version_at_close: int = 0
        try:
            try:
                data_version_at_open = int(conn.execute("PRAGMA data_version").fetchone()[0])
            except sqlite3.OperationalError as e:
                raise Phase2DBError(f"PRAGMA data_version failed: {e}") from e
            data_version_at_close = data_version_at_open  # default if pull fails
            _check_schema(conn, db_path)
            # A.7: pin the schema we read from so re-extract-from-snapshot
            # surfaces drift loudly. Only meaningful when a snapshot is bound
            # — on legacy runs (live state.db) the schema can change between
            # extract and re-extract, so the recorded sha would be a false
            # invariant. Use null for legacy runs.
            schema_cols_sha = None
            if snapshot_meta_for_bundle['state_db_snapshot_sha256'] is not None:
                from _state_db_snapshot import schema_columns_sha256 as _schema_sha
                schema_cols_sha = _schema_sha(Path(db_path), 'evaluated_opportunities')
            logging.info("[extract] pulling rows for asset=%s ...", asset)
            kept, drops, source_total = pull_and_classify(
                conn, asset, cutoff_end, asset_floor,
                provenance_filter=args.provenance_filter,
            )
            try:
                data_version_at_close = int(conn.execute("PRAGMA data_version").fetchone()[0])
            except sqlite3.OperationalError:
                pass  # non-fatal; audit just records the open value
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
                f"0 rows after filter — likely wrong asset or stale state.db. "
                f"drops={drops}"
            )
        logging.info("[extract] kept=%d dropped=%d source_total=%d", n_kept, n_dropped, source_total)

        # Compute train_id (deterministic content-derived).
        # R1#C23: data_version_at_open dropped from sha8 input — it's an audit
        # field, not content. Identical (asset, cfg_fp, cutoff_end, fold params)
        # → identical train_id even across data_version progressions.
        sha8_input = (
            f"{asset}|{cfg_fp}|{cutoff_end}|{args.folds}|"
            f"{args.train_days}|{args.cal_days}|{args.test_days}|{args.fold_offset_days}"
        )
        sha8 = hashlib.sha256(sha8_input.encode()).hexdigest()[:8]
        train_id = f"{cutoff_end}-{sha8}"
        train_dir = out_dir / train_id
        train_dir.mkdir(parents=True, exist_ok=True)
        # R1#C24 + R2#C5 + R2-impl#C3: clean any stale tmp files from a prior
        # crashed extract — both train_dir and out_dir's CURRENT.tmp-*.
        # Lock guarantees no concurrent writer; safe to glob+unlink.
        for stale in list(train_dir.glob('*.tmp-*')) + list(out_dir.glob('CURRENT.tmp-*')):
            try:
                stale.unlink()
                logging.info("[extract] cleaned stale tmp: %s", stale)
            except OSError:
                pass

        # Build feature frame
        df = build_feature_frame(kept)

        # Min-data check (need at least train+cal+test+fold_offset×(folds-1) days
        oldest_ts = pd.to_datetime(df['evaluation_time'], utc=True).min()
        min_required = args.train_days + args.cal_days + args.test_days + args.fold_offset_days * (args.folds - 1)
        available_days = (cutoff_end_dt - oldest_ts).days
        if available_days < min_required:
            raise Phase2ContractError(
                f"source has {available_days}d, need ≥{min_required}d for "
                f"folds={args.folds} train={args.train_days} cal={args.cal_days} "
                f"test={args.test_days} offset={args.fold_offset_days}. "
                f"Reduce --folds/--train-days or wait for more data."
            )

        # Compute fold windows
        fold_windows = compute_fold_windows(
            cutoff_end_dt, args.folds, args.train_days, args.cal_days,
            args.test_days, args.fold_offset_days,
        )

        # Build asset-wide ticker vocab from all kept rows
        unique_tickers = sorted(df['ticker'].astype(str).unique())
        ticker_vocab = {'<UNK>': 0}
        ticker_vocab.update({t: i + 1 for i, t in enumerate(unique_tickers)})
        df['ticker_id'] = df['ticker'].astype(str).map(ticker_vocab).astype(np.int32)
        df['is_unk_ticker'] = np.int8(0)  # always 0 in extract by construction

        # Per-fold processing
        per_fold_audit: list[dict] = []
        fold_artifacts: list[dict] = []
        tmps_to_rename: list[tuple[Path, Path]] = []
        all_per_cell: dict = {}

        try:
            for fw in fold_windows:
                k = fw['fold']
                logging.info("[extract] fold %d: train=[%s,%s) cal=[%s,%s) test=[%s,%s)",
                             k, fw['train_start'].date(), fw['train_end'].date(),
                             fw['cal_start'].date(), fw['cal_end'].date(),
                             fw['test_start'].date(), fw['test_end'].date())
                fold_df = df.copy()
                fold_df['split'] = assign_split(fold_df, fw)
                fold_df, n_boundary_reassigned = enforce_ticker_disjoint(fold_df)
                # R-p2-r12#C1: walk-forward temporal contract must hold POST-disjoint.
                assert_walk_forward_temporal(fold_df, fw)
                fold_df = fold_df[fold_df['split'].notna()].reset_index(drop=True)
                fold_df['fold'] = np.int8(k)
                # Split frames
                tr = fold_df[fold_df['split'] == 'train'].reset_index(drop=True)
                ca = fold_df[fold_df['split'] == 'cal'].reset_index(drop=True)
                te = fold_df[fold_df['split'] == 'test'].reset_index(drop=True)
                # Aborts
                if len(te) < 50:
                    raise Phase2ContractError(
                        f"fold {k}: n_test={len(te)} < 50 (conformal quantile unstable)"
                    )
                if len(tr) < args.n_train_min:
                    raise Phase2ContractError(
                        f"fold {k}: n_train={len(tr)} < {args.n_train_min}"
                    )
                # NULL contract: any continuous feature >30% NULL on train
                for col in CONT_FEATURE_COLS:
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
                # Normstats (post-transform; train only)
                normstats = fit_normstats(tr, CONT_FEATURE_COLS, CONT_FEATURE_TRANSFORMS)
                normstats_payload = {
                    'fold': k, 'asset': asset, 'cutoff_end': cutoff_end,
                    'n_train': int(len(tr)), 'ddof': 1,
                    'transforms': dict(CONT_FEATURE_TRANSFORMS),
                    'stats': normstats,
                }
                # Imputed-pct per split (count NaN BEFORE impute, ON post-transform values)
                imputed_pct = {'train': {}, 'cal': {}, 'test': {}}
                for col in CONT_FEATURE_COLS:
                    if normstats[col].get('_no_zscore'):
                        for label, sp in (('train', tr), ('cal', ca), ('test', te)):
                            imputed_pct[label][col] = float(sp[col].isna().sum()) / max(1, len(sp))
                        continue
                    # Apply transform on each split (in-memory, for counting)
                    tname = CONT_FEATURE_TRANSFORMS.get(col, 'identity')
                    for label, sp in (('train', tr), ('cal', ca), ('test', te)):
                        tx = transform(sp[col], tname)
                        imputed_pct[label][col] = float(tx.isna().sum()) / max(1, len(sp))
                # Per-cell — Phase 5 owns per-cell aborts; Phase 2 just surfaces.
                per_cell = compute_per_cell_stats(tr, ca, te)
                small_cell_warnings = [
                    f"{cell_key} n_test={cell['n_test']} <50"
                    for cell_key, cell in per_cell.items()
                    if 0 < cell['n_test'] < 50
                ]
                # Assemble fold parquet
                fold_parquet_path = train_dir / f"fold{k}.parquet"
                # All columns (CONT in raw value space; Phase 4 applies normstats lazily)
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

            # Ticker stats
            ticker_stats = {
                'n_unique_tickers': len(unique_tickers),
                'mean_rows_per_ticker': float(len(df) / max(1, len(unique_tickers))),
                'pct_tickers_with_only_one_row': float(
                    (df.groupby('ticker').size() == 1).sum() / max(1, len(unique_tickers))
                ),
            }

            # Ticker vocab tmp
            vocab_path = train_dir / 'ticker_vocab.json'
            vocab_payload = {
                'asset': asset,
                'vocab': ticker_vocab,
                'n_unique': len(unique_tickers),
            }
            vocab_tmp, vocab_sha = atomic_write_json(vocab_payload, vocab_path)
            tmps_to_rename.append((vocab_tmp, vocab_path))

            # Audit JSON tmp
            audit_path = train_dir / 'extract_audit.json'
            audit_payload = {
                'asset': asset,
                'train_id': train_id,
                'cfg_fp': cfg_fp,
                'cutoff_end': cutoff_end,
                'data_version_at_open': int(data_version_at_open),
                'data_version_at_close': int(data_version_at_close),
                'source_total_rows_for_asset': source_total,
                'source_total_rows_post_filter': n_kept,
                'drops': drops,
                'per_fold': per_fold_audit,
                'ticker_stats': ticker_stats,
                'include_sub_floor': bool(args.include_sub_floor),
                'asset_floor_applied': asset_floor,
                'provenance_filter': args.provenance_filter,
                'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            }
            audit_tmp, audit_sha = atomic_write_json(audit_payload, audit_path)
            tmps_to_rename.append((audit_tmp, audit_path))

            # Bundle JSON tmp (LAST)
            bundle_path = train_dir / 'extract_bundle.json'
            bundle_payload = {
                'phase': 2,
                'schema_version': 2,
                'asset': asset,
                'train_id': train_id,
                'cfg_fp': cfg_fp,
                'cutoff_end': cutoff_end,
                'include_sub_floor': bool(args.include_sub_floor),
                'provenance_filter': args.provenance_filter,
                'data_version_at_open': int(data_version_at_open),
                'data_version_at_close': int(data_version_at_close),
                # R1#C2: paths are basenames; resolve against the bundle's
                # directory `Path(bundle_path).parent` per locked Phase 2 spec.
                # Phase 4 must rewrite to absolute or pass through the parent.
                '_path_resolution': 'basenames_relative_to_bundle_dir',
                'ticker_vocab_path': vocab_path.name,
                'ticker_vocab_sha256': vocab_sha,
                'audit_path': audit_path.name,
                'audit_sha256': audit_sha,
                'eval_fold_artifacts': fold_artifacts,
                'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
                'extract_data_py_sha256': hashlib.sha256(
                    Path(__file__).read_bytes()
                ).hexdigest(),
                # A.7: pin the source DB bytes + schema. NULL on legacy runs
                # without --snapshot-sha256/--auto-snapshot.
                'state_db_snapshot_sha256': snapshot_meta_for_bundle['state_db_snapshot_sha256'],
                'state_db_snapshot_path': snapshot_meta_for_bundle['state_db_snapshot_path'],
                'state_db_snapshot_size_bytes_uncompressed': snapshot_meta_for_bundle['state_db_snapshot_size_bytes_uncompressed'],
                'state_db_snapshot_compression': snapshot_meta_for_bundle['state_db_snapshot_compression'],
                'state_db_schema_columns_sha256': schema_cols_sha,
            }
            bundle_tmp, bundle_sha = atomic_write_json(bundle_payload, bundle_path)
            # Bundle is renamed LAST in the loop below
            tmps_to_rename.append((bundle_tmp, bundle_path))

            # Now rename in dependency order: parquet/normstats per-fold,
            # then vocab, then audit, then bundle.
            renamed: list[Path] = []
            try:
                for tmp, final in tmps_to_rename:
                    os.replace(tmp, final)
                    renamed.append(final)
                fsync_directory(train_dir)

                # CURRENT pointer (the very last step)
                current_path = out_dir / 'CURRENT'
                current_tmp = current_path.with_suffix(
                    current_path.suffix + f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
                )
                # R2#C19: write in binary mode for byte-deterministic content.
                with open(current_tmp, 'wb') as f:
                    f.write(train_id.encode('utf-8'))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(current_tmp, current_path)
                fsync_directory(out_dir)
            except Exception as e:
                # Rename failure: roll back already-renamed final paths in REVERSE order
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
            # Unlink any tmps still on disk (renamed ones are handled above)
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
    except Exception as e:
        logging.exception("unexpected error")
        sys.exit(1)
    summary = {
        'train_id': bundle['train_id'],
        'cfg_fp': bundle['cfg_fp'],
        'asset': bundle['asset'],
        'cutoff_end': bundle['cutoff_end'],
        'n_folds': len(bundle['eval_fold_artifacts']),
    }
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
