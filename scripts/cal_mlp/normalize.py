"""Per-fold transforms + normstats fit/apply.

Phase 2 calls `fit_normstats(df, CONT_FEATURE_COLS)` on the train split per
fold; persists the result. Phase 4/5/6 call `apply_norm(df, normstats,
CONT_FEATURE_COLS)` to materialize z-scored features at training/inference
time.

Locked transform → impute → z-score order per R-p2-spec-r5 (ALL splits use
the FOLD-TRAIN MEAN computed POST-TRANSFORM; never per-split mean, never
zero, never raw-space mean).
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from features import (  # noqa: E402
    CONT_FEATURE_COLS,
    CONT_FEATURE_TRANSFORMS,
    RAW_PROB_CLIP_EPS,
)


# ---------------------------------------------------------------------------
# Per-column transforms
# ---------------------------------------------------------------------------

def _logit(s: pd.Series) -> pd.Series:
    """logit(p) = log(p/(1-p)). Clamps to [EPS, 1-EPS] to avoid ±inf."""
    arr = s.astype(np.float64).to_numpy(copy=True)
    mask = ~np.isnan(arr)
    arr[mask] = np.clip(arr[mask], RAW_PROB_CLIP_EPS, 1.0 - RAW_PROB_CLIP_EPS)
    arr[mask] = np.log(arr[mask] / (1.0 - arr[mask]))
    return pd.Series(arr, index=s.index, dtype=np.float64)


def _log_cents_to_dollars(s: pd.Series) -> pd.Series:
    """log1p(x/100) — cents → log-dollars. Preserves sign by going through
    log1p (which is well-defined on negatives if input/100 > -1)."""
    return np.log1p(s.astype(np.float64) / 100.0)


def _log1p_signed(s: pd.Series) -> pd.Series:
    """sign(x) * log1p(|x|). Handles negative ranges (e.g. realized range
    deltas) without losing direction."""
    arr = s.astype(np.float64).to_numpy(copy=True)
    out = np.sign(arr) * np.log1p(np.abs(arr))
    return pd.Series(out, index=s.index, dtype=np.float64)


def _log1p(s: pd.Series) -> pd.Series:
    """log1p(x) — for non-negative columns (realized vol, etc.)."""
    return np.log1p(s.astype(np.float64))


_TRANSFORMS = {
    'identity': lambda s: s.astype(np.float64),
    'identity_no_zscore': lambda s: s.astype(np.float64),
    'logit': _logit,
    'log_cents_to_dollars': _log_cents_to_dollars,
    'log1p': _log1p,
    'log1p_signed': _log1p_signed,
}


def transform(s: pd.Series, name: str) -> pd.Series:
    """Apply the named transform to a Series (returns a new Series)."""
    if name not in _TRANSFORMS:
        raise ValueError(f"unknown transform: {name!r}")
    return _TRANSFORMS[name](s)


# ---------------------------------------------------------------------------
# fit_normstats — POST-TRANSFORM mean/std + robust statistics
# ---------------------------------------------------------------------------

def fit_normstats(
    train_df: pd.DataFrame,
    cont_feature_cols: list[str] = CONT_FEATURE_COLS,
    transforms: dict[str, str] = CONT_FEATURE_TRANSFORMS,
) -> dict:
    """Compute per-column normstats on the train split.

    Returns a dict {col: {mean, std, n_nan, p1, p99, median, mad, _no_zscore}}.

    Order: transform train values → drop NaN → compute mean/std/quantiles.
    `train_df` is the train split AFTER the bucketization step but BEFORE
    apply_norm — i.e. raw values for the CONT_FEATURE_COLS columns.
    """
    out = {}
    for col in cont_feature_cols:
        tname = transforms.get(col, 'identity')
        if tname == 'identity_no_zscore':
            # Analytical: fixed mean=0, std=1 — no fitting from data.
            out[col] = {
                'mean': 0.0,
                'std': 1.0,
                '_no_zscore': True,
                'n_nan': int(train_df[col].isna().sum()),
                'transform': tname,
            }
            continue
        # Step 1: transform train values
        tx = transform(train_df[col], tname)
        # Step 2: post-transform non-null
        nn = tx.dropna()
        n_nan = int(tx.isna().sum())
        if len(nn) == 0:
            raise RuntimeError(
                f"fit_normstats: column {col!r} has 0 non-null train rows after "
                f"transform={tname!r} — Phase 2 contract violation."
            )
        mean = float(nn.mean())
        std = float(nn.std(ddof=1))
        if std == 0.0:
            raise RuntimeError(
                f"fit_normstats: column {col!r} has std=0 after transform={tname!r} "
                f"— constant column on train; Phase 2 contract violation."
            )
        out[col] = {
            'mean': mean,
            'std': std,
            'n_nan': n_nan,
            'p1': float(nn.quantile(0.01)),
            'p99': float(nn.quantile(0.99)),
            'median': float(nn.median()),
            'mad': float((nn - nn.median()).abs().median()),
            'transform': tname,
        }
    return out


# ---------------------------------------------------------------------------
# apply_norm — transform → impute → z-score
# ---------------------------------------------------------------------------

def apply_norm(
    df: pd.DataFrame,
    normstats: dict,
    cont_feature_cols: list[str] = CONT_FEATURE_COLS,
    transforms: dict[str, str] = CONT_FEATURE_TRANSFORMS,
) -> pd.DataFrame:
    """Materialize post-transform, post-impute, post-z-score values.

    Operates on a COPY of df. Imputation uses the train-fold mean stored in
    normstats. For 'identity_no_zscore' columns, only transform is applied
    (no impute — those columns shouldn't have NaN by construction; if they
    do, fillna(0) since the analytical mean is 0).
    """
    out = df.copy()
    for col in cont_feature_cols:
        if col not in normstats:
            raise RuntimeError(f"apply_norm: column {col!r} missing from normstats")
        stats = normstats[col]
        tname = stats.get('transform') or transforms.get(col, 'identity')
        # Step 1: transform
        out[col] = transform(out[col], tname)
        if stats.get('_no_zscore'):
            out[col] = out[col].fillna(0.0)
            continue
        # Step 2: impute with FOLD-TRAIN POST-TRANSFORM MEAN
        out[col] = out[col].fillna(stats['mean'])
        # Step 3: z-score
        out[col] = (out[col] - stats['mean']) / stats['std']
    return out


def n_imputed_per_split(df: pd.DataFrame, cont_feature_cols: list[str] = CONT_FEATURE_COLS) -> dict:
    """Count NaN values per column in `df` BEFORE imputation. Caller must
    invoke this on the post-transform but pre-fillna DataFrame to record
    the audit field `imputed_pct.<split>.<col>`."""
    return {col: int(df[col].isna().sum()) for col in cont_feature_cols}
