"""Shared helpers consumed by Phase 4/5/6/7.

Pure-math + I/O atomicity. NO torch dependency in this module — Phase 7's
bot.py imports these without bringing in the model machinery.

Locked exports (per Phase 5 R5 + Phase 4 R3):
- FORWARD_KEYS: tuple of model forward-pass keys
- compute_extract_logical_sha: stable across pyarrow versions
- predict_with_interval: conformal interval at inference / audit mode
- lookup_cell_quantile: per-cell dispatch chain (Mondrian → merged → global)
- market_implied_prob_yes: side-aware breakeven helper
- wilson_ci: 80% binomial CI
- fsync_directory: directory fd fsync
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional, TypedDict, Union

# R-p7-r2#H2: import BLEED_CELL from features so this module and conformal.py
# stay aligned through any future rebinning. features.py has no torch dep so
# this is safe even though _helpers.py is intentionally torch-free.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from features import BLEED_CELL  # noqa: E402


# ---------------------------------------------------------------------------
# FORWARD_KEYS (Phase 4 R3#C3 lock)
# ---------------------------------------------------------------------------

FORWARD_KEYS: tuple[str, ...] = (
    'x_cont', 'x_missing',
    'price_tier', 'stc_bucket', 'vol_regime_int', 'side_int',
    'ticker_id', 'logit_raw_prob_clipped',
)


# ---------------------------------------------------------------------------
# Bundle SHA chain (R-p7-r2#H3 + R-p7-r3#H3-DRY-1)
# ---------------------------------------------------------------------------
# DRY home for the bundle_sha_v1 chain formula. Phase 5 (conformal.load_predictor),
# Phase 7 (integration.CalMLPPredictor._verify_bundle_sha_chain), and any
# future phase MUST go through this function so a chain-format change updates
# both producers and consumers in lockstep.

def verify_bundle_sha_chain(bundle: dict) -> None:
    """Recompute phase4_bundle_sha + bundle_sha (phase5 chain hash) and
    assert match. Caller-side wrapper translates RuntimeError into the
    phase-specific exception type."""
    deploy_idx = bundle.get('deploy_fold_idx',
                              max(r['fold'] for r in bundle['eval_fold_artifacts']))
    fold = next(r for r in bundle['eval_fold_artifacts'] if r['fold'] == deploy_idx)
    ckpt_shas = sorted(m['checkpoint_sha256'] for m in fold['members'])
    model_id_sha = hashlib.sha256(':'.join(ckpt_shas).encode()).hexdigest()
    ns_concat = hashlib.sha256()
    for fold_art in bundle['eval_fold_artifacts']:
        ns_concat.update(fold_art['normstats_sha256'].encode())
    ns_sha = ns_concat.hexdigest()
    expected_p4 = hashlib.sha256(
        f"{model_id_sha}:{ns_sha}:phase4".encode()
    ).hexdigest()
    if expected_p4 != bundle.get('phase4_bundle_sha'):
        raise RuntimeError(
            f"phase4 sha mismatch (expected={expected_p4} "
            f"bundle={bundle.get('phase4_bundle_sha')})"
        )
    # Phase 5 chain only applies if the bundle records a conformal_sha256.
    conformal_sha = bundle.get('conformal_sha256')
    if conformal_sha is None:
        return
    expected_p5 = hashlib.sha256(
        f"{expected_p4}:{conformal_sha}".encode()
    ).hexdigest()
    if expected_p5 != bundle.get('bundle_sha'):
        raise RuntimeError(
            f"phase5 sha mismatch (expected={expected_p5} "
            f"bundle={bundle.get('bundle_sha')})"
        )


# ---------------------------------------------------------------------------
# compute_extract_logical_sha (Phase 4 R3#C1)
# ---------------------------------------------------------------------------

def compute_extract_logical_sha(audit_json: dict, normstats_per_fold: list[dict]) -> str:
    """Stable across pyarrow upgrades. Reads transforms from top-level
    `normstats['transforms']` (NOT inside individual stat dicts)."""
    canonical = {
        'normstats': [
            {col: {
                'mean': stats['mean'],
                'std': stats['std'],
                'transform': ns.get('transforms', {}).get(col, 'identity'),
                '_no_zscore': stats.get('_no_zscore', False),
             }
             for col, stats in sorted(ns['stats'].items())}
            for ns in normstats_per_fold
        ],
        'per_fold': [
            {'fold': pf['fold'],
             'n_train': pf['n_train'], 'n_cal': pf['n_cal'], 'n_test': pf['n_test'],
             'per_cell': {k: {'n_train': v['n_train'], 'n_cal': v['n_cal'], 'n_test': v['n_test'],
                              'train_positive_rate': v.get('train_positive_rate'),
                              'train_mean_method_output': v.get('train_mean_method_output')}
                          for k, v in sorted(pf.get('per_cell', {}).items())}}
            for pf in audit_json.get('per_fold', [])
        ],
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# market_implied_prob_yes (side-aware breakeven)
# ---------------------------------------------------------------------------

def market_implied_prob_yes(entry_price_cents: int, side: str) -> float:
    """For YES side: breakeven = price/100. For NO side: breakeven = 1 - price/100.
    The 'price' is the YES ask in cents (or NO ask if you're on the NO side)."""
    p = max(0.0, min(1.0, entry_price_cents / 100.0))
    return p if side == 'yes' else 1.0 - p


# ---------------------------------------------------------------------------
# wilson_ci (80% / 95% binomial CI)
# ---------------------------------------------------------------------------

def wilson_ci(n_success: int, n_total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% CI on a binomial proportion. n_total=0 → (0, 1)."""
    if n_total <= 0:
        return (0.0, 1.0)
    p = n_success / n_total
    denom = 1 + z * z / n_total
    centre = (p + z * z / (2 * n_total)) / denom
    half = (z * math.sqrt(p * (1 - p) / n_total + z * z / (4 * n_total * n_total))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


# ---------------------------------------------------------------------------
# fsync_directory
# ---------------------------------------------------------------------------

def fsync_directory(path: Path) -> None:
    """fsync a directory file descriptor for atomicity guarantees."""
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# AuditDict (Phase 5 R2#C3) + return-type aliases
# ---------------------------------------------------------------------------

class AuditDict(TypedDict):
    """R-p7-r2#M1: q_alpha and half_width are Optional because dispatch_miss
    cells return None. Audit consumers must None-check before float()."""
    q_alpha: Optional[float]
    half_width: Optional[float]
    clipped_lo: bool
    clipped_hi: bool
    chain: list
    p_pred_raw: float


InferenceReturn = tuple  # (p_center, p_std, final_lo, final_hi)
AuditReturn = tuple      # (p_center, p_std, final_lo, final_hi, AuditDict)


# ---------------------------------------------------------------------------
# lookup_cell_quantile (Phase 5 R5)
# ---------------------------------------------------------------------------

N_CELL_FLOOR = 20  # cells with n_cal < 20 are NOT emitted at fit; fall through


def lookup_cell_quantile(
    artifact: dict,
    row_features: dict,
    mode: str = 'inference',
) -> tuple[Optional[float], list]:
    """Returns (q_alpha, chain). q_alpha=None means dispatch missed.

    Lookup chain:
      1. Bleed cell (3, 2, *) per-vol_regime collapse-by-merge if active
      2. Mondrian direct (price_tier, stc_bucket, vol_regime)
      3. Merged-axes fallback (collapse axes listed in artifact['merged_axes'])
      4. Global quantile fallback
    """
    chain: list = []
    pt = int(row_features['price_tier'])
    sb = int(row_features['stc_bucket'])
    vr = int(row_features['vol_regime'])

    # 1. Bleed cell — per-vol_regime granularity (Phase 5 R1#C5)
    bleed_per_vr = artifact.get('bleed_collapsed_by_merge_per_vr', {})
    if (pt, sb) == BLEED_CELL and bleed_per_vr.get(str(vr)):
        bleed = artifact.get('bleed_fallback_quantiles', {}) or {}
        key_axes = bleed.get('key_axes', [])
        sub_key = format_bleed_key(key_axes, row_features)
        q = (bleed.get('quantiles') or {}).get(sub_key)
        if q is not None:
            chain.append(f"bleed[{sub_key}]")
            return float(q), chain

    # 2. Direct mondrian lookup — structured fields only.
    cell = next(
        (c for c in artifact.get('cells', [])
         if c.get('price_tier') == pt
            and c.get('stc_bucket') == sb
            and c.get('vol_regime') == vr),
        None,
    )
    if cell is not None:
        chain.append(f"mondrian[({pt},{sb},{vr})]")
        return float(cell['q_alpha']), chain

    # 3. Merged-axes fallback. Phase 5 R1#C6 vocabulary:
    # merged_axes ⊆ {'price_tier', 'stc', 'vol_regime'}.
    merged = artifact.get('merged_axes', [])
    fb_pt = 0 if 'price_tier' in merged else pt
    fb_sb = 0 if 'stc' in merged else sb
    fb_vr = 0 if 'vol_regime' in merged else vr
    fb_cell = next(
        (c for c in artifact.get('cells', [])
         if c.get('price_tier') == fb_pt
            and c.get('stc_bucket') == fb_sb
            and c.get('vol_regime') == fb_vr),
        None,
    )
    if fb_cell is not None:
        chain.append(f"merged[({fb_pt},{fb_sb},{fb_vr})]")
        return float(fb_cell['q_alpha']), chain

    # 4. Global fallback
    if 'global_q_alpha' in artifact:
        chain.append("global")
        return float(artifact['global_q_alpha']), chain

    chain.append("dispatch_miss")
    return None, chain


# ---------------------------------------------------------------------------
# predict_with_interval (Phase 5 R5)
# ---------------------------------------------------------------------------

def predict_with_interval(
    p_pred: float,
    p_std: float,
    conformal_artifact: dict,
    row_features: dict,
    entry_price_cents: int,
    side: str,
    market_blend_w: float,
    mode: str = 'inference',
) -> Union[tuple, tuple]:
    """Returns (p_center, p_std, final_lo, final_hi) at inference; or
    (..., AuditDict) at audit. final_lo=None on dispatch miss.

    R1#C3: pure conformal width q_alpha (no σ inflation in production).
    R1#C10: market_blend_w > 0 invalidates conformal validity guarantee
            (soft-flag emitted by Phase 6; production default = 0).
    """
    breakeven = market_implied_prob_yes(int(entry_price_cents), str(side))
    p_center = market_blend_w * breakeven + (1 - market_blend_w) * p_pred

    q_alpha, chain = lookup_cell_quantile(conformal_artifact, row_features, mode)
    if q_alpha is None:
        if mode == 'audit':
            return p_center, p_std, None, None, {
                'q_alpha': None, 'half_width': None,
                'clipped_lo': False, 'clipped_hi': False,
                'chain': chain, 'p_pred_raw': float(p_pred),
            }
        return p_center, p_std, None, None

    half_width = q_alpha
    final_lo_raw = p_center - half_width
    final_hi_raw = p_center + half_width
    clipped_lo = final_lo_raw < 0
    clipped_hi = final_hi_raw > 1
    final_lo = max(0.0, final_lo_raw)
    final_hi = min(1.0, final_hi_raw)

    if mode == 'audit':
        return p_center, p_std, final_lo, final_hi, {
            'q_alpha': float(q_alpha),
            'half_width': float(half_width),
            'clipped_lo': bool(clipped_lo),
            'clipped_hi': bool(clipped_hi),
            'chain': chain,
            'p_pred_raw': float(p_pred),
        }
    return p_center, p_std, final_lo, final_hi


# ---------------------------------------------------------------------------
# Hash + bundle discovery (R1#C8 + C9 — single source for cross-phase use)
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def verify_artifact_sha(path: Path, expected: str) -> None:
    """Raise RuntimeError on mismatch; common helper for Phase 4/5/6/7."""
    actual = sha256_file(path)
    if actual != expected:
        raise RuntimeError(f"sha256 mismatch on {path}: expected={expected} actual={actual}")


def find_bundle_by_sha(models_dir: Path, asset: str, bundle_sha: str) -> Path:
    """Find Phase 4/5 bundle JSON whose `bundle_sha` matches.
    Uses rglob to recurse into per-train_id subdirectories."""
    pattern = f"cal_mlp_{asset}_*_bundle.json"
    for path in models_dir.rglob(pattern):
        try:
            with open(path) as f:
                bundle = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if bundle.get('bundle_sha') == bundle_sha:
            return path
    raise RuntimeError(f"bundle with sha={bundle_sha[:12]} not found in {models_dir}")


def load_bundle_with_dir(bundle_path: Path) -> dict:
    """Load a bundle JSON and inject `_bundle_dir` for downstream path
    resolution (R1#C1 fix — Phase 5 load_predictor needs this)."""
    with open(bundle_path) as f:
        bundle = json.load(f)
    bundle['_bundle_dir'] = str(bundle_path.parent)
    return bundle


# ---------------------------------------------------------------------------
# Bleed key formatter (Phase 5 impl R1#C7)
# ---------------------------------------------------------------------------

_AXIS_TO_FEATURE_COL = {'price_tier': 'price_tier', 'stc': 'stc_bucket',
                          'vol_regime': 'vol_regime'}


def format_bleed_key(key_axes: list, row_features: dict) -> str:
    """Build a sub-key string like 'vol_regime=0' (single axis) or
    'vol_regime=0,price_tier=3' (multi-axis). Used by both fit_conformal
    (write side) and lookup_cell_quantile (read side)."""
    if not key_axes:
        return '_all'
    parts = []
    for a in key_axes:
        col = _AXIS_TO_FEATURE_COL.get(a, a)
        parts.append(f"{a}={row_features[col]}")
    return ','.join(parts)


__all__ = [
    'FORWARD_KEYS',
    'compute_extract_logical_sha',
    'market_implied_prob_yes',
    'wilson_ci',
    'fsync_directory',
    'lookup_cell_quantile',
    'predict_with_interval',
    'AuditDict',
    'N_CELL_FLOOR',
    'sha256_file',
    'verify_artifact_sha',
    'find_bundle_by_sha',
    'load_bundle_with_dir',
    'format_bleed_key',
]
