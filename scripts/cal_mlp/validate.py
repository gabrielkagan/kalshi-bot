#!/usr/bin/env python3
"""
P2 Phase 6: validation harness. Implements `kb-research/bot/p2-phase6-validation.md`
(30 critiques addressed across 3 spec rounds, then 4 rounds of impl review).

Reads a Phase 5 bundle, runs:
  - Per-band Brier comparison (production vs MLP) with paired bootstrap CIs
  - Empirical coverage on `[final_lo, final_hi]` per cell with Wilson CIs
  - Sim PnL counterfactual replay through full live gate (dual block_on/off)
  - Drawdown scaler walk-forward replay
  - A/B mode if --challenger-bundle-sha provided

Emits:
  models/cal_mlp_<asset>_*.lock (SHARED — Phase 6 doesn't block Phase 5)
  data/cal_mlp/<asset>/validation_audit_v<art_tag>_<conformal_sha[:8]>.json
  reports/p2_validation_<asset>_<art_tag>_<conformal_sha[:8]>.md

Usage:
  validate.py --asset SOL --bundle-sha <hex> [--challenger-bundle-sha <hex>]
              [--alpha 0.20] [--bootstrap-n 2000] [--allow-shipblocker-fail]
              [--override-market-blend-w <float>] [--allow-alpha-mismatch]
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import torch
from torch.utils.data import DataLoader

sys.path.insert(0, str(Path(__file__).parent))

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

from train import (  # noqa: E402
    CalibrationMLP,
    CalibrationDataset,
    CONT_FEATURE_COLS,
    N_CONT,
    DEFAULT_DROPOUT,
    apply_norm,
    collate_dict,
)  # R3#C5: dropped unused predict_p_out import
from conformal import (  # noqa: E402
    SinglePredictor,
    EnsemblePredictor,
    load_predictor,
    _load_normstats,
    _verify_artifact_sha,
)
from _helpers import (  # noqa: E402
    market_implied_prob_yes,
    lookup_cell_quantile,
    predict_with_interval,
    fsync_directory,
    wilson_ci,
)
from stats import (  # noqa: E402
    cluster_bootstrap_ci,
    day_bootstrap_ci,
    escalate_n_if_close,
)
# R-p2-r12-lint: sizing imports were unused in validate.py — they're consumed
# by sim_pnl.py via _replay_one_path. validate.py only invokes run_sim_pnl.


# ---------------------------------------------------------------------------
# Locked constants
# ---------------------------------------------------------------------------

DEFAULT_ALPHA = 0.20
DEFAULT_BOOTSTRAP_N = 2000
SHIP_BLOCKER_BAND_N_FLOOR = 150
UNSETTLED_DROP_SOFT = 0.05
UNSETTLED_DROP_HARD = 0.20
COVERAGE_TOL_NORMAL = 0.05
COVERAGE_TOL_SMALL_N = 0.10
RSS_HARD_CEILING_BYTES = 1024 * 1024 * 1024  # 1GB per R-p6-2#C6


def _peak_rss_mb() -> Optional[float]:
    """R-p6-impl-2#C4: return None when psutil missing — soft_flag surfaces
    the disabled RSS check separately."""
    if not _HAS_PSUTIL:
        return None
    return psutil.Process().memory_info().rss / (1024 * 1024)


def _check_rss_ceiling(label: str) -> None:
    if not _HAS_PSUTIL:
        return
    rss = psutil.Process().memory_info().rss
    if rss > RSS_HARD_CEILING_BYTES:
        raise SystemExit(
            f"RSS ceiling exceeded ({rss / 1024 / 1024:.0f} MB) at {label}; "
            f"R-p6-2#C6 hard cap = 1GB"
        )


# ---------------------------------------------------------------------------
# Acquire SHARED flock (R-p6-2#C2 — readers, blocked by EXCLUSIVE writers)
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def acquire_shared_lock(lock_path: Path, asset: str):
    """Phase 6 reader takes LOCK_SH on the existing .cal_mlp_<asset>.lock.
    R-p6-impl-2#C9 + #C3: open with O_RDWR|O_CREAT (no truncation) so readers
    never destroy a writer's lock-file content."""
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    handle = os.fdopen(fd, 'r+')
    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            handle.close()
            raise SystemExit(
                f"Phase 4/5 writer holds {asset} lock; aborting Phase 6 "
                f"(retry after writer completes)"
            )
        yield handle
    finally:
        if acquired:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


# ---------------------------------------------------------------------------
# Bundle locator (find by SHA)
# ---------------------------------------------------------------------------

# R-impl-r2#C9: shared find_bundle_by_sha in _helpers.py uses rglob (recursive).
from _helpers import find_bundle_by_sha as _find_bundle_helper  # noqa: E402
from _helpers import load_bundle_with_dir  # noqa: E402


def find_bundle_by_sha(models_dir: Path, asset: str, bundle_sha: str) -> Path:
    try:
        return _find_bundle_helper(models_dir, asset, bundle_sha)
    except RuntimeError as e:
        raise SystemExit(
            f"{e}; verify the SHA from train.py / conformal.py output."
        )


# ---------------------------------------------------------------------------
# Per-band Brier comparison (R-p6-1#C1 + R-p6-1#C4)
# ---------------------------------------------------------------------------

GRAD_BAND_BOUNDS = [(0.0, 0.85), (0.85, 0.92), (0.92, 0.96), (0.96, 1.0)]
GRAD_BAND_NAMES = ['<0.85', '0.85-0.92', '0.92-0.96', '0.96+']


def _band_for_p(p: float) -> str:
    for (lo, hi), name in zip(GRAD_BAND_BOUNDS, GRAD_BAND_NAMES):
        if hi == 1.0 and lo <= p <= 1.0:
            return name
        if lo <= p < hi:
            return name
    return '<0.85'


def per_band_brier(
    df: pd.DataFrame,
    bootstrap_n: int,
    seed: int,
) -> dict:
    """Per-band paired-bootstrap on Brier delta = mlp - prod (signed,
    more-negative = MLP improves more). Cluster by ticker per E3."""
    df = df.copy()
    df['band'] = df['method_output'].apply(_band_for_p)
    out = {}
    for band in GRAD_BAND_NAMES:
        sub = df[df['band'] == band]
        if len(sub) == 0:
            out[band] = {'n': 0, 'point': None, 'ci_lo': None, 'ci_hi': None,
                         'ship_blocker_active': False, 'ship_blocker_fires': False}
            continue

        def stat(d: pd.DataFrame) -> float:
            d = d.copy()
            d['err_prod'] = (d['method_output'] - d['outcome'].astype(float)) ** 2
            d['err_mlp'] = (d['p_pred'] - d['outcome'].astype(float)) ** 2
            return float(d['err_mlp'].mean() - d['err_prod'].mean())

        point, lo, hi, audit = cluster_bootstrap_ci(
            sub, stat, cluster_col='ticker', n_bootstrap=bootstrap_n, seed=seed,
        )
        n = len(sub)
        ship_blocker_active = (n >= SHIP_BLOCKER_BAND_N_FLOOR)
        ship_blocker_fires = ship_blocker_active and (hi > 0.005)
        out[band] = {
            'n': int(n),
            'point': float(point),
            'ci_lo': float(lo),
            'ci_hi': float(hi),
            'mc_se_mean': audit['mc_se_mean'],
            'ship_blocker_active': ship_blocker_active,
            'ship_blocker_fires': ship_blocker_fires,
        }
    return out


# ---------------------------------------------------------------------------
# Empirical coverage on [final_lo, final_hi] (R-p6-1#C5/C8)
# ---------------------------------------------------------------------------

def empirical_coverage(
    df: pd.DataFrame,
    conformal_artifact: dict,
    market_blend_w: float,
) -> tuple[list[dict], dict]:
    """Per-cell coverage + directional miss rates + clip rate."""
    cell_stats = defaultdict(lambda: {
        'n': 0, 'n_covered': 0, 'n_below_lo': 0, 'n_above_hi': 0,
        'n_clipped': 0, 'n_dispatch_miss': 0,
    })
    bleed_collapsed = conformal_artifact.get('bleed_collapsed_by_merge', False)
    bleed_key_axes = (conformal_artifact.get('bleed_fallback_quantiles', {}) or {}).get('key_axes', [])
    ax_to_col = {'vol_regime': 'vol_regime', 'price_tier': 'price_tier',
                 'stc': 'stc_bucket'}
    n_total = 0
    for _, row in df.iterrows():
        n_total += 1
        # R-p6-impl-r5#C2: parquet has BOTH `vol_regime` (string: 'normal'/'elevated')
        # and `vol_regime_int` (int 0/1). Conformal lookup uses the int. Reading
        # `int(row['vol_regime'])` on the string raises ValueError on first
        # 'elevated' row.
        row_features = {
            'price_tier': int(row['price_tier']),
            'stc_bucket': int(row['stc_bucket']),
            'vol_regime': int(row['vol_regime_int']),
        }
        is_bleed = (row_features['price_tier'] == 3 and row_features['stc_bucket'] == 2)
        if bleed_collapsed and is_bleed:
            sub = ",".join(f"{a}={row_features[ax_to_col[a]]}"
                           for a in bleed_key_axes) or '_all'
            key = ('bleed', sub)
        else:
            merged = conformal_artifact['merged_axes']
            pt = 0 if 'price_tier' in merged else int(row['price_tier'])
            sb = 0 if 'stc' in merged else int(row['stc_bucket'])
            vr = 0 if 'vol_regime' in merged else int(row['vol_regime_int'])
            key = (pt, sb, vr)
        s = cell_stats[key]
        result = predict_with_interval(
            float(row['p_pred']), float(row.get('p_std', 0.0)),
            conformal_artifact, row_features,
            int(row['entry_price_cents']), str(row['side']),
            market_blend_w, mode='inference',
        )
        p_mean, p_std, final_lo, final_hi = result
        if final_lo is None:
            s['n_dispatch_miss'] += 1
            continue
        q_alpha, _chain = lookup_cell_quantile(conformal_artifact, row_features, mode='inference')
        if q_alpha is None:
            s['n_dispatch_miss'] += 1
            continue
        p_lo_raw = p_mean - q_alpha
        p_hi_raw = p_mean + q_alpha
        clipped = (p_lo_raw < 0 or p_hi_raw > 1)
        outcome_p = float(row['outcome'])
        s['n'] += 1
        if final_lo <= outcome_p <= final_hi:
            s['n_covered'] += 1
        if outcome_p < final_lo:
            s['n_below_lo'] += 1
        if outcome_p > final_hi:
            s['n_above_hi'] += 1
        if clipped:
            s['n_clipped'] += 1
    cell_audit = []
    n_total_dispatch_miss = 0
    for key, s in cell_stats.items():
        n_total_dispatch_miss += s['n_dispatch_miss']
        if s['n'] == 0:
            continue
        cov = s['n_covered'] / s['n']
        lo, hi = wilson_ci(s['n_covered'], s['n'])
        _, clip_hi = wilson_ci(s['n_clipped'], s['n'])
        if isinstance(key, tuple) and len(key) == 2 and key[0] == 'bleed':
            cell_id = {'cell_kind': 'bleed', 'sub_key': key[1]}
        else:
            pt, sb, vr = key
            cell_id = {'cell_kind': 'mondrian',
                       'price_tier': pt, 'stc_bucket': sb, 'vol_regime': vr}
        cell_audit.append({
            **cell_id,
            'n': s['n'], 'n_covered': s['n_covered'],
            'n_below_lo': s['n_below_lo'], 'n_above_hi': s['n_above_hi'],
            'n_clipped': s['n_clipped'], 'n_dispatch_miss': s['n_dispatch_miss'],
            'coverage': cov,
            'coverage_wilson_lo': lo, 'coverage_wilson_hi': hi,
            'clip_rate': s['n_clipped'] / s['n'],
            'clip_wilson_hi': clip_hi,
            'frac_below_lo': s['n_below_lo'] / s['n'],
            'frac_above_hi': s['n_above_hi'] / s['n'],
        })
    return cell_audit, {
        'n_total_test_rows': n_total,
        'n_eval_cells': len(cell_audit),
        'n_total_dispatch_miss': n_total_dispatch_miss,
    }


# ---------------------------------------------------------------------------
# Sim PnL (delegated to sim_pnl module)
# ---------------------------------------------------------------------------

from sim_pnl import run_sim_pnl  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_cfg_fp_compat(
    base_cfg_fp: str,
    challenger_cfg_fp: str,
    allow_mismatch: bool,
) -> None:
    """Enforce or escape the A/B cfg_fp identity guard.

    Default contract: bundles must share `cfg_fp` (= same canonical-dict
    feature schema + extraction policy). Cross-cfg_fp comparison is
    refused because predictions across schema-incompatible bundles are
    nonsense.

    `allow_mismatch=True` mirrors the `--allow-alpha-mismatch` escape:
    operator explicitly opts into the comparison. Used for the v2
    ablation where live_only and full_dataset bundles differ in cfg_fp
    only by `provenance_filter` value (canonical-dict feature-schema
    keys are identical). Logs a loud warning so the override is visible
    in audit trails.
    """
    if base_cfg_fp == challenger_cfg_fp:
        return
    if not allow_mismatch:
        raise SystemExit(
            "A/B cfg_fp mismatch — different feature schemas; refusing. "
            "Use --allow-cfg-fp-mismatch to force (manual_review; only "
            "valid when canonical-dict drift is provenance_filter-only)."
        )
    logging.warning(
        "A/B cfg_fp mismatch ALLOWED via --allow-cfg-fp-mismatch: "
        "base=%s challenger=%s — operator owns verifying that the "
        "canonical-dict drift is benign (provenance_filter only, not "
        "feature-schema keys).",
        base_cfg_fp, challenger_cfg_fp,
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--asset', required=True, choices=['BTC', 'ETH', 'SOL', 'XRP'])
    ap.add_argument('--bundle-sha', required=True)
    ap.add_argument('--challenger-bundle-sha', default=None)
    ap.add_argument('--alpha', type=float, default=DEFAULT_ALPHA)
    ap.add_argument('--bootstrap-n', type=int, default=DEFAULT_BOOTSTRAP_N)
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--models-dir', default='models')
    ap.add_argument('--data-dir', default='data/cal_mlp')
    ap.add_argument('--reports-dir', default='reports')
    ap.add_argument('--device', default='cpu')
    ap.add_argument('--db', default='state.db')
    ap.add_argument('--allow-shipblocker-fail', action='store_true')
    ap.add_argument('--override-market-blend-w', type=float, default=None)
    ap.add_argument('--allow-alpha-mismatch', action='store_true')
    # v2 ablation escape hatch: live_only vs full_dataset bundles share
    # CONT_FEATURE_COLS but differ in cfg_fp by `provenance_filter` only
    # (per kb/decisions/v2-cal-mlp-deploy-runbook-may03.md). The default
    # cfg_fp guard refuses the comparison; this flag mirrors the
    # `--allow-alpha-mismatch` precedent and emits a loud warning rather
    # than silently bypassing identity. Operator owns ensuring the
    # canonical-dict drift is benign (i.e., only provenance_filter, not
    # CONT_FEATURE_COLS or other feature-schema keys).
    ap.add_argument('--allow-cfg-fp-mismatch', action='store_true')
    args = ap.parse_args()

    device = torch.device(args.device)
    # R-impl-r3#C2: anchor relative paths to project_root (script location),
    # NOT cwd — matches extract_data.py's anchoring so Phase 2 writer and
    # Phase 6 reader agree on locations regardless of invocation cwd.
    project_root = Path(__file__).resolve().parents[2]
    def _resolve(p: str) -> Path:
        pp = Path(p)
        return (pp if pp.is_absolute() else (project_root / pp)).resolve()
    models_dir = _resolve(args.models_dir)
    data_dir = _resolve(args.data_dir)
    reports_dir = _resolve(args.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    asset_data_dir = data_dir / args.asset
    asset_data_dir.mkdir(parents=True, exist_ok=True)

    # R-p4-spec-r2#C3: lock at per-asset directory (parallel to Phase 2's
    # data/cal_mlp/<asset>/.extract.lock). Old path was models/.cal_mlp_<asset>.lock.
    # R-p6-impl-r5#H2: also acquire extract_lock SH (outer, taken FIRST per
    # train.py:562-564 ordering invariant) so the deploy-fold parquet and
    # normstats files can't be replaced by a concurrent extract_data.py run
    # mid-read. Lock domain split: extract (SH) wraps models (SH) for readers;
    # writers take EX in the inverse order.
    asset_models_dir = models_dir / f"cal_mlp_{args.asset}"
    asset_models_dir.mkdir(parents=True, exist_ok=True)
    extract_lock_path = asset_data_dir / ".extract.lock"
    lock_path = asset_models_dir / ".lock"
    with acquire_shared_lock(extract_lock_path, args.asset), \
         acquire_shared_lock(lock_path, args.asset):
        bundle_path = find_bundle_by_sha(models_dir, args.asset, args.bundle_sha)
        # R-impl-r2#C2: load_bundle_with_dir injects _bundle_dir for load_predictor.
        bundle = load_bundle_with_dir(bundle_path)
        if bundle.get('phase') != 5:
            raise SystemExit(
                f"input bundle phase={bundle.get('phase')}; Phase 6 requires "
                f"Phase 5 bundle (one with conformal_path populated)"
            )
        # R-p6-impl-2#C9 + R3#C3: top-level keys actually present in
        # Phase 5 bundles. normstats_path/sha lives per-fold under
        # eval_fold_artifacts (NOT top level).
        _required_keys = (
            'phase', 'cfg_fp', 'train_id', 'conformal_path', 'conformal_sha256',
            'eval_fold_artifacts', 'extract_bundle_path', 'deploy_fold_idx',
        )
        _missing = [k for k in _required_keys if k not in bundle]
        if _missing:
            raise SystemExit(
                f"bundle malformed (sha={args.bundle_sha[:12]}): missing keys {_missing}"
            )
        # R2#C5: conformal_path is basename relative to bundle_dir.
        bundle_dir = Path(bundle['_bundle_dir'])
        conformal_path = Path(bundle['conformal_path'])
        if not conformal_path.is_absolute():
            conformal_path = bundle_dir / conformal_path
        if not conformal_path.exists():
            raise SystemExit(f"conformal_path {conformal_path} does not exist")
        _verify_artifact_sha(conformal_path, bundle['conformal_sha256'])
        with open(conformal_path) as f:
            conformal_artifact = json.load(f)

        challenger_bundle = None
        challenger_artifact = None
        if args.challenger_bundle_sha:
            ch_path = find_bundle_by_sha(models_dir, args.asset, args.challenger_bundle_sha)
            challenger_bundle = load_bundle_with_dir(ch_path)
            if challenger_bundle.get('phase') != 5:
                raise SystemExit("challenger bundle is not Phase 5")
            ch_bundle_dir = Path(challenger_bundle['_bundle_dir'])
            ch_conf_path = Path(challenger_bundle['conformal_path'])
            if not ch_conf_path.is_absolute():
                ch_conf_path = ch_bundle_dir / ch_conf_path
            _verify_artifact_sha(ch_conf_path, challenger_bundle['conformal_sha256'])
            with open(ch_conf_path) as f:
                challenger_artifact = json.load(f)
            if challenger_artifact['alpha'] != conformal_artifact['alpha']:
                if not args.allow_alpha_mismatch:
                    raise SystemExit(
                        f"A/B alpha mismatch: base={conformal_artifact['alpha']} "
                        f"challenger={challenger_artifact['alpha']}; use "
                        f"--allow-alpha-mismatch to force (manual_review)"
                    )
            _check_cfg_fp_compat(
                base_cfg_fp=bundle['cfg_fp'],
                challenger_cfg_fp=challenger_bundle['cfg_fp'],
                allow_mismatch=args.allow_cfg_fp_mismatch,
            )

        # MARKET_BLEND_W resolution: CLI > ENV > market_config.py > bundle.
        # R-p6-impl-2#C5: strip env, treat blank as missing, surface bad floats.
        if args.override_market_blend_w is not None:
            market_blend_w = args.override_market_blend_w
            mb_source = 'cli_override'
        elif (_env := os.environ.get('MARKET_BLEND_W', '').strip()):
            try:
                market_blend_w = float(_env)
            except ValueError:
                raise SystemExit(
                    f"MARKET_BLEND_W env var is not a valid float: {_env!r}"
                )
            mb_source = 'env'
        else:
            try:
                from market_config import MARKET_CONFIGS as _MC  # type: ignore
                mc_w = _MC['15m'].market_blend_w
                market_blend_w = mc_w
                mb_source = 'market_config.py'
            except (ImportError, KeyError, AttributeError):
                market_blend_w = conformal_artifact['market_blend_w']
                mb_source = conformal_artifact['market_blend_w_source']
        # Drift check.
        try:
            sys.path.insert(0, str(Path.cwd()))
            from market_config import MARKET_CONFIGS  # type: ignore
            current_w = MARKET_CONFIGS['15m'].market_blend_w
        except (ImportError, KeyError, AttributeError):
            current_w = None
        market_blend_w_drift = (
            current_w is not None
            and abs(market_blend_w - current_w) > 1e-9
            and conformal_artifact.get('market_blend_w_source') != 'cli'
        )

        predictor = load_predictor(bundle, device)

        # R-p5-spec-r1#C2: read deploy_fold_idx (= K-1) per Phase 3/5 lock —
        # NOT fold==0. fold==0 is the OLDEST test window in walk-forward; the
        # deployable model is fit on K-1's CAL split.
        deploy_fold_idx = bundle.get('deploy_fold_idx')
        if deploy_fold_idx is None:
            # Fallback for Phase 4 bundles missing the field: use last fold.
            deploy_fold_idx = max(r['fold'] for r in bundle['eval_fold_artifacts'])
        deploy_fold = next(
            (r for r in bundle['eval_fold_artifacts'] if r['fold'] == deploy_fold_idx),
            None,
        )
        if deploy_fold is None:
            raise SystemExit(f"bundle has no fold-{deploy_fold_idx} record")
        # R2#C3 + R3#C4: parquet + normstats are extract-dir-relative.
        # Fail fast if extract_bundle_path is missing — silent fallback
        # masks malformed bundles.
        extract_bundle_rel = bundle.get('extract_bundle_path', '')
        if not extract_bundle_rel:
            raise SystemExit(
                f"bundle missing extract_bundle_path (sha={args.bundle_sha[:12]})"
            )
        ext_bundle_path = Path(extract_bundle_rel)
        if not ext_bundle_path.is_absolute():
            ext_bundle_path = project_root / ext_bundle_path
        extract_dir = ext_bundle_path.parent
        deploy_fold_path = Path(deploy_fold['parquet_path'])
        if not deploy_fold_path.is_absolute():
            deploy_fold_path = extract_dir / deploy_fold_path
        if not deploy_fold_path.exists():
            raise SystemExit(f"deploy fold {deploy_fold_idx} parquet missing at {deploy_fold_path}")
        fold_df = pd.read_parquet(deploy_fold_path, engine='pyarrow', dtype_backend='pyarrow')
        test_df = fold_df[fold_df['split'] == 'test'].copy()
        if test_df['outcome'].isna().any():
            raise SystemExit("test split has NaN outcomes — Phase 2 contract violation")

        # R2#C2: normstats is per-fold; read from eval_fold_artifacts[deploy_fold_idx].
        ns_rel = deploy_fold['normstats_path']
        ns_path = Path(ns_rel)
        if not ns_path.is_absolute():
            ns_path = extract_dir / ns_path
        normstats = _load_normstats(
            ns_path,
            expected_sha=deploy_fold.get('normstats_sha256'),
        )
        # R-p6-impl-r5#CRIT: _load_normstats returns full payload; unwrap.
        test_normed = apply_norm(
            test_df, normstats['stats'], CONT_FEATURE_COLS,
            transforms=normstats.get('transforms', {}),
        )
        ticker_to_id = {t: i for i, t in enumerate(sorted(test_normed['ticker'].unique()))}
        ds = CalibrationDataset(test_normed, CONT_FEATURE_COLS, ticker_to_id)
        loader = DataLoader(ds, batch_size=2048, shuffle=False, collate_fn=collate_dict)
        p_means, p_stds = [], []
        with torch.no_grad():
            for batch in loader:
                p, p_std = predictor.predict(batch)
                p_means.extend(p.detach().cpu().tolist())
                p_stds.extend(p_std.detach().cpu().tolist())
        test_df = test_df.reset_index(drop=True)
        # R-p6-impl-2#C11: defensive shape assert.
        assert len(p_means) == len(test_df), (
            f"prediction count drift: {len(p_means)} vs {len(test_df)}"
        )
        test_df['p_pred'] = np.asarray(p_means, dtype=np.float64)
        test_df['p_std'] = np.asarray(p_stds, dtype=np.float64)

        _check_rss_ceiling('after_predict')

        # Per-band Brier — tiered N escalation driven by escalate_n_if_close.
        print("[brier] per-band paired bootstrap (tiered N)...")
        current_n = args.bootstrap_n
        brier_per_band = per_band_brier(test_df, current_n, args.seed)
        bootstrap_inconclusive = False
        for _attempt in range(3):
            target_n = current_n
            for band, m in brier_per_band.items():
                if m.get('ci_hi') is None or not m.get('ship_blocker_active'):
                    continue
                margin = abs(m['ci_hi'] - 0.005)
                new_n, abstain = escalate_n_if_close(margin, current_n)
                if abstain:
                    bootstrap_inconclusive = True
                    break
                if new_n > target_n:
                    target_n = new_n
            if bootstrap_inconclusive:
                break
            if target_n > current_n:
                current_n = target_n
                print(f"[brier] escalating to N={current_n}...")
                brier_per_band = per_band_brier(test_df, current_n, args.seed)
            else:
                break

        print("[coverage] per-cell empirical...")
        cell_audit, cov_summary = empirical_coverage(
            test_df, conformal_artifact, market_blend_w,
        )
        _check_rss_ceiling('after_coverage')

        print("[sim_pnl] counterfactual replay...")
        sim_pnl_result = run_sim_pnl(
            asset=args.asset, bundle=bundle, conformal_artifact=conformal_artifact,
            predictor=predictor, market_blend_w=market_blend_w,
            test_window=(test_df['evaluation_time'].min(),
                         test_df['evaluation_time'].max()),
            normstats=normstats, db_path=args.db, device=device,
            challenger_bundle=challenger_bundle,
            challenger_artifact=challenger_artifact,
        )
        _check_rss_ceiling('after_sim_pnl')

        blockers = []
        soft_flags = []

        # Per-band ship-blocker #1 (degradation, n≥150)
        for band, m in brier_per_band.items():
            if m['ship_blocker_active'] and m['ship_blocker_fires']:
                blockers.append(
                    f"#1 brier_band[{band}]: ci_hi={m['ci_hi']:.4f} > 0.005 "
                    f"(n={m['n']}, n_floor={SHIP_BLOCKER_BAND_N_FLOOR})"
                )
            elif m['ship_blocker_active'] is False and m['point'] is not None and m['point'] > 0.005:
                soft_flags.append(
                    f"per_band[{band}]: degradation point={m['point']:.4f} but n={m['n']} < {SHIP_BLOCKER_BAND_N_FLOOR}"
                )

        # Ship-blocker #2: A50 LOCKED inequality
        d96 = brier_per_band['0.96+'].get('point')
        d_body = brier_per_band['0.85-0.92'].get('point')
        n96 = brier_per_band['0.96+'].get('n', 0)
        if d96 is not None and d_body is not None and d96 > d_body:
            blockers.append(
                f"#2 A50 inequality: d_[0.96+]={d96:.4f} > d_[0.85,0.92)={d_body:.4f} "
                f"(more-negative on bleed band required)"
            )
        if n96 < 20:
            soft_flags.append(
                f"a50_inequality_unverifiable: 0.96+ band n={n96} < 20 "
                f"(insufficient data to verify d_[0.96+] ≤ d_[0.85,0.92))"
            )

        # Ship-blockers #4 + #5 + #6 + #7: per-cell coverage / directional / clip.
        def _cell_lbl(c: dict) -> str:
            if c.get('cell_kind') == 'bleed':
                return f"bleed[{c.get('sub_key', '_all')}]"
            return f"({c.get('price_tier')},{c.get('stc_bucket')},{c.get('vol_regime')})"
        for cell in cell_audit:
            n = cell['n']
            is_bleed = (cell.get('cell_kind') == 'bleed')
            tol = COVERAGE_TOL_SMALL_N if (is_bleed or 20 <= n < 40) else COVERAGE_TOL_NORMAL
            lbl = _cell_lbl(cell)
            if cell['coverage_wilson_lo'] < (1 - args.alpha) - tol and n >= 20:
                blockers.append(
                    f"#4 cov[{lbl}]: wilson_lo={cell['coverage_wilson_lo']:.3f} < "
                    f"target={(1 - args.alpha) - tol:.3f} (n={n})"
                )
            elif (0 < n < 20
                  and cell['coverage'] < (1 - args.alpha) - COVERAGE_TOL_SMALL_N):
                soft_flags.append(
                    f"per_cell_low_n[{lbl}]: coverage={cell['coverage']:.3f} "
                    f"< target={(1 - args.alpha) - COVERAGE_TOL_SMALL_N:.3f} (n={n}<20)"
                )
            below_wilson_lo, _ = wilson_ci(cell['n_below_lo'], cell['n'])
            if below_wilson_lo > args.alpha / 2 + 0.05:
                blockers.append(
                    f"#5 lower-miscov[{lbl}]: wilson_lo={below_wilson_lo:.3f} > "
                    f"{args.alpha/2 + 0.05:.3f} (n_below_lo={cell['n_below_lo']}/{n})"
                )
            above_wilson_lo, _ = wilson_ci(cell['n_above_hi'], cell['n'])
            if above_wilson_lo > args.alpha / 2 + 0.05:
                blockers.append(
                    f"#5 upper-miscov[{lbl}]: wilson_lo={above_wilson_lo:.3f} > "
                    f"{args.alpha/2 + 0.05:.3f} (n_above_hi={cell['n_above_hi']}/{n})"
                )
            if cell['clip_wilson_hi'] > 0.05:
                blockers.append(
                    f"#6 clip[{lbl}]: wilson_hi={cell['clip_wilson_hi']:.3f} > 0.05 "
                    f"(n_clipped={cell['n_clipped']}/{n})"
                )
        if cov_summary['n_total_dispatch_miss'] > 0:
            blockers.append(f"#7 dispatch_miss: {cov_summary['n_total_dispatch_miss']} test rows")

        # Sim PnL ship-blockers (#8, #9, #10).
        sim_pnl_off = sim_pnl_result.get('block_off', {})
        sim_pnl_on = sim_pnl_result.get('block_on', {})
        if sim_pnl_off.get('total_pessimistic_30d', 0.0) <= 0:
            blockers.append(f"#8 sim_pnl pessimistic ≤ 0: ${sim_pnl_off.get('total_pessimistic_30d', 0):.2f}")
        modeled = sim_pnl_off.get('total_modeled_30d', 0.0)
        pessim = sim_pnl_off.get('total_pessimistic_30d', 0.0)
        if pessim != 0 and abs(modeled - pessim) / abs(pessim) > 0.5:
            blockers.append(f"#9 sim_pnl modeled vs pessimistic > 50% diverge")
        if modeled == pessim:
            soft_flags.append(
                "#9_dead: pnl_modeled == pnl_pessimistic (per-cell fill-rate "
                "model deferred — divergence ship-blocker is structurally "
                "inactive in Phase 6)"
            )
        if (sim_pnl_off.get('worst_7d_drawdown_cents')
                == sim_pnl_off.get('worst_7d_drawdown_prod_cents')):
            soft_flags.append(
                "drawdown_prod_stub: worst_7d_drawdown_prod == worst_7d_drawdown_mlp "
                "(production-path replay deferred — ratio always 1.0)"
            )
        weighted_drop = sim_pnl_result.get('weighted_avg_risk_drop', 0.0)
        if weighted_drop > 0.15:
            blockers.append(f"#10 tier migration weighted_avg_risk_drop={weighted_drop:.3f} > 0.15")

        if market_blend_w_drift:
            blockers.append(
                f"market_blend_w drift: bundle={market_blend_w} current={current_w}"
            )

        if not _HAS_PSUTIL:
            soft_flags.append(
                "psutil_missing: RSS ceiling check disabled (R-p6-2#C6 1GB cap not enforced)"
            )
        if (sim_pnl_result.get('challenger_error')
                and args.challenger_bundle_sha):
            soft_flags.append(
                f"challenger_replay_error: {sim_pnl_result['challenger_error']}"
            )

        unsettled_drop_rate = sim_pnl_result.get('unsettled_drop_rate', 0.0)
        if unsettled_drop_rate > UNSETTLED_DROP_HARD:
            blockers.append(
                f"unsettled_drop_rate={unsettled_drop_rate:.3f} > {UNSETTLED_DROP_HARD} "
                f"(catastrophic backfill failure)"
            )
        elif unsettled_drop_rate > UNSETTLED_DROP_SOFT:
            soft_flags.append(f"unsettled_drop_rate={unsettled_drop_rate:.3f}")
        worst_ratio = sim_pnl_result.get('worst_7d_drawdown_ratio', 1.0)
        if worst_ratio > 1.5:
            soft_flags.append(f"worst_7d_drawdown_ratio={worst_ratio:.2f} > 1.5")
        if sim_pnl_result.get('hwm_init_source') == 'forward_only_from_now':
            soft_flags.append("hwm_init_source=forward_only_from_now (drawdown replay structural-only)")
        # A27 deprecation candidate — R-p6-impl-3#C6 drops `> 0` lower bound.
        block_marginal = (
            sim_pnl_on.get('total_pessimistic_30d', 0.0)
            - sim_pnl_off.get('total_pessimistic_30d', 0.0)
        )
        if block_marginal < 100:
            soft_flags.append(
                f"a27_block_marginal_pnl_30d=${block_marginal:.0f} < $100 "
                f"(deprecation CANDIDATE — confounded; manual MIN_EDGE_BY_PRICE "
                f"re-validation required; negative = block hurting PnL)"
            )

        for cell in conformal_artifact.get('cells', []):
            if cell.get('concentration_warning'):
                soft_flags.append(
                    f"concentration_warning cell({cell['price_tier']},"
                    f"{cell['stc_bucket']},{cell['vol_regime']}) "
                    f"top_share={cell.get('top_ticker_share', 0):.2f}"
                )

        if bootstrap_inconclusive:
            ship_rec = 'manual_review'
            soft_flags.append("bootstrap_inconclusive: tiered escalation hit max_n=50000 with margin<0.001")
        elif args.override_market_blend_w is not None:
            ship_rec = 'manual_review'
        elif blockers and not args.allow_shipblocker_fail:
            ship_rec = 'block'
        elif blockers and args.allow_shipblocker_fail:
            ship_rec = 'manual_review'
        elif soft_flags:
            ship_rec = 'manual_review'
        else:
            ship_rec = 'ship'

        audit = {
            'phase': 6,
            'schema_version': 1,
            'asset': args.asset,
            'bundle_sha': args.bundle_sha,
            'conformal_sha': bundle.get('conformal_sha256'),
            'challenger_bundle_sha': args.challenger_bundle_sha,
            'alpha': args.alpha,
            'bootstrap_n_initial': args.bootstrap_n,
            'bootstrap_n_final': current_n,
            'bootstrap_inconclusive': bootstrap_inconclusive,
            'bootstrap_seed': args.seed,
            'market_blend_w_used': market_blend_w,
            'market_blend_w_source': mb_source,
            'current_market_blend_w': current_w,
            'market_blend_w_drift': market_blend_w_drift,
            'brier_per_band': brier_per_band,
            'coverage_per_cell': cell_audit,
            'coverage_summary': cov_summary,
            'sim_pnl': sim_pnl_result,
            'blockers_fired': blockers,
            'soft_flags': soft_flags,
            'shipblocker_overrides': blockers if (blockers and args.allow_shipblocker_fail) else [],
            'allow_shipblocker_fail': bool(args.allow_shipblocker_fail),
            'allow_alpha_mismatch': bool(args.allow_alpha_mismatch),
            'allow_cfg_fp_mismatch': bool(args.allow_cfg_fp_mismatch),
            'override_market_blend_w': args.override_market_blend_w,
            'ship_recommendation': ship_rec,
            'peak_rss_mb': _peak_rss_mb(),
            'torch_version': torch.__version__,
            'pandas_version': pd.__version__,
            'generated_at': datetime.now(timezone.utc).isoformat(),
        }
        art_tag = f"{bundle['cfg_fp']}_{bundle['train_id']}"
        conformal_sha = bundle['conformal_sha256']
        audit_path = asset_data_dir / f"validation_audit_v{art_tag}_{conformal_sha[:8]}.json"
        report_path = reports_dir / f"p2_validation_{args.asset}_{art_tag}_{conformal_sha[:8]}.md"
        # R-p6-impl-2#C1 (R2-wiring): atomic bundle — write report tmp first;
        # on failure, audit_tmp is unlinked and audit JSON never appears.
        audit_tmp = audit_path.with_suffix(
            audit_path.suffix + f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        )
        report_tmp = report_path.with_suffix(
            report_path.suffix + f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        )
        with open(audit_tmp, 'w') as f:
            json.dump(audit, f, indent=2, sort_keys=True, default=str)
            f.flush()
            os.fsync(f.fileno())
        try:
            _write_report_md(report_tmp, audit)
        except Exception:
            try:
                os.unlink(audit_tmp)
            except OSError:
                pass
            raise
        os.replace(audit_tmp, audit_path)
        fsync_directory(asset_data_dir)
        os.replace(report_tmp, report_path)
        fsync_directory(report_path.parent)

        print(f"[OK] {args.asset}: ship_recommendation={ship_rec} "
              f"blockers={len(blockers)} soft_flags={len(soft_flags)} "
              f"audit={audit_path} report={report_path}")


def _write_report_md(path: Path, audit: dict) -> None:
    """R-p6-3#C4 LOCKED 10-section layout. Caller manages atomic-bundle replace."""
    lines = []
    lines.append(f"# Phase 6 Validation Report — {audit['asset']}\n")
    lines.append(f"**ship_recommendation: `{audit['ship_recommendation']}`**\n")
    lines.append(f"\nbundle_sha: `{audit['bundle_sha'][:12]}` | "
                 f"conformal_sha: `{audit['conformal_sha'][:12]}` | "
                 f"alpha: {audit['alpha']}\n")

    lines.append("\n## 1. Per-band Brier delta (mlp − prod)\n")
    lines.append("| band | n | point | ci_lo | ci_hi | n≥150 | blocker |")
    lines.append("|---|---|---|---|---|---|---|")
    for band, m in audit['brier_per_band'].items():
        if m.get('point') is None:
            lines.append(f"| {band} | 0 | — | — | — | — | — |")
        else:
            lines.append(f"| {band} | {m['n']} | {m['point']:.4f} | "
                         f"{m['ci_lo']:.4f} | {m['ci_hi']:.4f} | "
                         f"{m['ship_blocker_active']} | {m['ship_blocker_fires']} |")

    lines.append("\n## 2. Per-cell coverage\n")
    lines.append("| cell | n | coverage | wilson_lo | wilson_hi | below_lo | above_hi | clip_rate |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for c in audit['coverage_per_cell']:
        cell_lbl = (f"bleed[{c.get('sub_key', '')}]" if c.get('cell_kind') == 'bleed'
                    else f"({c['price_tier']},{c['stc_bucket']},{c['vol_regime']})")
        lines.append(
            f"| {cell_lbl} | {c['n']} | {c['coverage']:.3f} | "
            f"{c['coverage_wilson_lo']:.3f} | {c['coverage_wilson_hi']:.3f} | "
            f"{c['frac_below_lo']:.3f} | {c['frac_above_hi']:.3f} | "
            f"{c['clip_rate']:.3f} |"
        )

    lines.append("\n## 3. Sim PnL summary\n")
    sp = audit['sim_pnl']
    lines.append(f"- Pessimistic 30d (block_off): ${sp.get('block_off', {}).get('total_pessimistic_30d', 0):.2f}")
    lines.append(f"- Modeled 30d (block_off): ${sp.get('block_off', {}).get('total_modeled_30d', 0):.2f}")
    lines.append(f"- Pessimistic 30d (block_on): ${sp.get('block_on', {}).get('total_pessimistic_30d', 0):.2f}")
    lines.append(f"- A27 block marginal: ${(sp.get('block_on', {}).get('total_pessimistic_30d', 0) - sp.get('block_off', {}).get('total_pessimistic_30d', 0)):.2f}/30d")
    lines.append(f"- Tier migration weighted_avg_risk_drop: {sp.get('weighted_avg_risk_drop', 0):.3f}")

    lines.append("\n## 4. Drawdown\n")
    lines.append(f"- worst_7d_drawdown_ratio (mlp/prod): {sp.get('worst_7d_drawdown_ratio', 1.0):.2f}")
    lines.append(f"- hwm_init_source: {sp.get('hwm_init_source', 'unknown')}")

    lines.append("\n## 5. HARD blockers fired\n")
    if audit['blockers_fired']:
        for b in audit['blockers_fired']:
            lines.append(f"- {b}")
    else:
        lines.append("- none")

    lines.append("\n## 6. Soft flags\n")
    if audit['soft_flags']:
        for s in audit['soft_flags']:
            lines.append(f"- {s}")
    else:
        lines.append("- none")

    lines.append("\n## 7. Provenance\n")
    lines.append(
        f"- bootstrap_n: initial={audit['bootstrap_n_initial']} "
        f"final={audit['bootstrap_n_final']} "
        f"inconclusive={audit['bootstrap_inconclusive']}, "
        f"seed: {audit['bootstrap_seed']}"
    )
    lines.append(f"- market_blend_w_used: {audit['market_blend_w_used']} (source: {audit['market_blend_w_source']})")
    lines.append(f"- current_market_blend_w: {audit['current_market_blend_w']} (drift: {audit['market_blend_w_drift']})")
    rss = audit.get('peak_rss_mb')
    rss_str = f"{rss:.0f}" if isinstance(rss, (int, float)) else "null (psutil missing)"
    lines.append(f"- peak_rss_mb: {rss_str}")
    lines.append(f"- generated_at: {audit['generated_at']}")

    raw = "\n".join(lines) + "\n"
    with open(path, 'w') as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())


if __name__ == '__main__':
    main()
