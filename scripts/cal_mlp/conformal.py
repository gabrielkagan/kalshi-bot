#!/usr/bin/env python3
"""
P2 Phase 5: Mondrian conformal wrapper around the M=5 ensemble Phase 4 bundle.

Implements `kb-research/bot/p2-phase5-conformal.md` (converged at R5):
- α=0.20 (80% nominal coverage)
- 3D cells (price_tier × stc_bucket × vol_regime) — 32 max
- Score = |p̂ - y|, fitted on fold K-1 CAL split
- Bleed-cell collapse-by-merge per vol_regime (R1#C5)
- N_CELL_FLOOR=20: cells below floor NOT emitted; lookup falls through
- Predictor protocol: SinglePredictor (M=1) and EnsemblePredictor (M=5)
- bundle_sha_v1 = sha256(phase4_bundle_sha:conformal_sha)
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
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

import numpy as np
import pandas as pd
import torch
from torch.utils.data import DataLoader

sys.path.insert(0, str(Path(__file__).parent))

from features import (  # noqa: E402
    BLEED_CELL,
    CONT_FEATURE_COLS,
    CONT_FEATURE_TRANSFORMS,
    MISSING_INDICATOR_COLS,
    RAW_PROB_CLIP_EPS,
)
from _helpers import (  # noqa: E402
    N_CELL_FLOOR,
    fsync_directory,
)
from train import (  # noqa: E402
    CalibrationMLP,
    Phase4Dataset,
    build_model_from_definition,
)

DEFAULT_ALPHA = 0.20


# ---------------------------------------------------------------------------
# Exit-code hierarchy
# ---------------------------------------------------------------------------

class Phase5Error(RuntimeError):
    exit_code: int = 1


class Phase5ContractError(Phase5Error):
    exit_code = 3


class Phase5LockError(Phase5Error):
    exit_code = 4


class Phase5WriteError(Phase5Error):
    exit_code = 5


class Phase5SchemaError(Phase5Error):
    exit_code = 6


# ---------------------------------------------------------------------------
# Predictor protocol — Single (M=1) + Ensemble (M=5)
# ---------------------------------------------------------------------------

class Predictor(Protocol):
    def predict(self, batch: dict) -> tuple:
        """Returns (p_mean[B], p_std[B]). For SinglePredictor, p_std=zeros."""
        ...


class SinglePredictor:
    """Wraps a single MLP (for M=1 ablation)."""
    def __init__(self, model: CalibrationMLP, device: torch.device):
        self.model = model.to(device).eval()
        self.device = device

    @torch.no_grad()
    def predict(self, batch: dict) -> tuple:
        from _helpers import FORWARD_KEYS
        kwargs = {k: batch[k].to(self.device) for k in FORWARD_KEYS}
        _, p = self.model(**kwargs)
        return p.detach().cpu(), torch.zeros_like(p.detach().cpu())


class EnsemblePredictor:
    """Wraps M ensemble members; ddof=0 std (members ARE the population)."""
    def __init__(self, models: list, device: torch.device):
        self.models = [m.to(device).eval() for m in models]
        self.device = device

    @torch.no_grad()
    def predict(self, batch: dict) -> tuple:
        from _helpers import FORWARD_KEYS
        kwargs = {k: batch[k].to(self.device) for k in FORWARD_KEYS}
        all_preds = []
        for m in self.models:
            _, p = m(**kwargs)
            all_preds.append(p.detach())
        stacked = torch.stack(all_preds)  # [M, B]
        p_mean = stacked.mean(dim=0)
        p_std = stacked.std(dim=0, unbiased=False)
        return p_mean.cpu(), p_std.cpu()


def _verify_artifact_sha(path: Path, expected: str) -> None:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    if h.hexdigest() != expected:
        raise Phase5SchemaError(f"sha256 mismatch on {path}")


def _load_normstats(path: Path, expected_sha: Optional[str] = None) -> dict:
    if expected_sha:
        _verify_artifact_sha(path, expected_sha)
    return json.load(open(path))


def load_predictor(bundle: dict, device: torch.device) -> Predictor:
    """Read deploy fold K-1's per-member checkpoints, build SinglePredictor
    or EnsemblePredictor based on ensemble_size."""
    ensemble_size = bundle.get('ensemble_size', 1)
    deploy_idx = bundle.get('deploy_fold_idx',
                              max(r['fold'] for r in bundle['eval_fold_artifacts']))
    fold = next(r for r in bundle['eval_fold_artifacts'] if r['fold'] == deploy_idx)
    bundle_dir = Path(bundle.get('_bundle_dir', '.'))
    model_def = json.load(open(bundle_dir / bundle['model_definition_path']))
    project_root = Path(__file__).resolve().parents[2]
    extract_bundle_path = project_root / bundle['extract_bundle_path']
    ext_bundle = json.load(open(extract_bundle_path))
    vocab_payload = json.load(open(extract_bundle_path.parent / ext_bundle['ticker_vocab_path']))
    n_vocab = len(vocab_payload['vocab'])

    models = []
    for m in fold['members']:
        model = build_model_from_definition(model_def, n_vocab=n_vocab)
        ckpt_path = bundle_dir / m['checkpoint_path']
        _verify_artifact_sha(ckpt_path, m['checkpoint_sha256'])
        state_dict = torch.load(ckpt_path, map_location='cpu')
        model.load_state_dict(state_dict)
        models.append(model)

    if ensemble_size == 1 or len(models) == 1:
        return SinglePredictor(models[0], device)
    return EnsemblePredictor(models, device)


# ---------------------------------------------------------------------------
# Conformal fit
# ---------------------------------------------------------------------------

def fit_conformal(
    cal_df: pd.DataFrame,
    alpha: float = DEFAULT_ALPHA,
    bleed_collapse: bool = True,
    n_cell_floor: int = N_CELL_FLOOR,
) -> dict:
    """Returns the conformal_artifact dict.

    cal_df has per-row p_mean (ensemble mean) and outcome columns; group by
    (price_tier, stc_bucket, vol_regime) — 4×4×2=32 cells max. Per cell with
    n >= floor, compute (1-α)-quantile of |p_mean - outcome|.
    """
    # Score = |p_mean - outcome|
    residuals = (cal_df['p_mean'].astype(np.float64) -
                  cal_df['outcome'].astype(np.float64)).abs().to_numpy()
    cal_df = cal_df.assign(_residual=residuals)

    cells_out: list[dict] = []
    bleed_per_vr: dict[str, bool] = {}
    bleed_quantiles: dict[str, float] = {}
    bleed_key_axes: list[str] = ['vol_regime']  # default merge axis
    n_cal_total = int(len(cal_df))
    global_q_alpha = float(np.quantile(residuals, 1 - alpha)) if n_cal_total > 0 else 1.0

    # Concentration warning: top ticker share within a cell.
    cal_df = cal_df.assign(_residual=residuals)
    for (pt, sb, vr), sub in cal_df.groupby(['price_tier', 'stc_bucket', 'vol_regime']):
        n = int(len(sub))
        is_bleed_cell_pt_sb = (int(pt), int(sb)) == BLEED_CELL
        if n < n_cell_floor:
            # Cell falls through; for the bleed cell we may compute a fallback below.
            if is_bleed_cell_pt_sb and bleed_collapse:
                bleed_per_vr[str(int(vr))] = True
            continue
        bleed_per_vr.setdefault(str(int(vr)), False)
        q_alpha = float(np.quantile(sub['_residual'].to_numpy(), 1 - alpha))
        # Top-ticker concentration (Phase 6 reads soft-flag).
        ticker_counts = sub['ticker'].value_counts()
        top_share = float(ticker_counts.iloc[0] / n) if len(ticker_counts) else 0.0
        cells_out.append({
            'price_tier': int(pt),
            'stc_bucket': int(sb),
            'vol_regime': int(vr),
            'n_cal': n,
            'q_alpha': q_alpha,
            'concentration_warning': bool(top_share > 0.5),
            'top_ticker_share': top_share,
        })

    # Compute bleed fallback if any vol_regime needs it.
    if bleed_collapse and any(bleed_per_vr.get(str(vr_i), False) for vr_i in (0, 1)):
        bleed_sub = cal_df[(cal_df['price_tier'] == BLEED_CELL[0]) &
                            (cal_df['stc_bucket'] == BLEED_CELL[1])]
        # Group by remaining key_axes (just vol_regime by default).
        for vr_i in (0, 1):
            sub_vr = bleed_sub[bleed_sub['vol_regime'] == vr_i]
            if len(sub_vr) >= 1:
                # Use whatever quantile we can; if too few for tight quantile,
                # the merged-cells fallback is conservative.
                bleed_quantiles[f"vol_regime={vr_i}"] = float(
                    np.quantile(sub_vr['_residual'].to_numpy(), 1 - alpha)
                )
            else:
                # If empty: use global q_alpha.
                bleed_quantiles[f"vol_regime={vr_i}"] = global_q_alpha
        bleed_collapsed_overall = True
    else:
        bleed_collapsed_overall = False
        # Ensure all vol_regimes default to False when not collapsed.
        for vr_i in (0, 1):
            bleed_per_vr.setdefault(str(vr_i), False)

    artifact = {
        'alpha': alpha,
        'n_cal_total': n_cal_total,
        'global_q_alpha': global_q_alpha,
        'merged_axes': [],   # population-level merging not used; bleed handled separately
        'bleed_collapsed_by_merge': bleed_collapsed_overall,
        'bleed_collapsed_by_merge_per_vr': bleed_per_vr,
        'bleed_fallback_quantiles': {
            'key_axes': bleed_key_axes,
            'quantiles': bleed_quantiles,
        },
        'cells': cells_out,
    }
    return artifact


# ---------------------------------------------------------------------------
# Lock helpers
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def acquire_lock_ex(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(fd, 'r+')
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise Phase5LockError(f"models lock {lock_path} held")
        yield handle
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
    finally:
        handle.close()


# ---------------------------------------------------------------------------
# Atomic write helpers
# ---------------------------------------------------------------------------

def write_json_tmp(payload: dict, final_path: Path) -> tuple[Path, str]:
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


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Phase 5 conformal wrapper")
    ap.add_argument('--asset', required=True, choices=['BTC', 'ETH', 'SOL', 'XRP'])
    ap.add_argument('--bundle-sha', required=True,
                    help='Phase 4 bundle_sha to wrap')
    ap.add_argument('--alpha', type=float, default=DEFAULT_ALPHA)
    ap.add_argument('--bleed-collapse', action='store_true', default=True)
    ap.add_argument('--no-bleed-collapse', dest='bleed_collapse', action='store_false')
    ap.add_argument('--n-cell-floor', type=int, default=N_CELL_FLOOR)
    ap.add_argument('--device', default='cpu')
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


def find_bundle_by_sha(models_dir: Path, asset: str, bundle_sha: str) -> Path:
    pattern = f"cal_mlp_{asset}_*_bundle.json"
    for path in models_dir.rglob(pattern):
        try:
            with open(path) as f:
                bundle = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if bundle.get('bundle_sha') == bundle_sha:
            return path
    raise Phase5SchemaError(
        f"bundle with sha={bundle_sha[:12]} not found in {models_dir}"
    )


def run(args: argparse.Namespace) -> dict:
    project_root = Path(__file__).resolve().parents[2]
    models_root = project_root / 'models' / f'cal_mlp_{args.asset}'
    if not models_root.exists():
        raise Phase5ContractError(f"no models dir for {args.asset}: {models_root}")

    bundle_path = find_bundle_by_sha(models_root, args.asset, args.bundle_sha)
    bundle = json.load(open(bundle_path))
    if bundle.get('phase') != 4:
        raise Phase5SchemaError(
            f"input bundle phase={bundle.get('phase')}; Phase 5 requires Phase 4"
        )
    train_dir = bundle_path.parent

    # Acquire models_lock EX (Phase 5 takes EX only; no extract_lock needed
    # per spec R1#C12 — Phase 4 bundle SHA chain is the integrity guarantee).
    models_lock = models_root / '.lock'
    with acquire_lock_ex(models_lock):
        # Stale-tmp cleanup
        for stale in train_dir.glob('*.tmp-*'):
            try:
                stale.unlink()
            except OSError:
                pass

        deploy_idx = bundle['deploy_fold_idx']
        deploy_fold = next(r for r in bundle['eval_fold_artifacts'] if r['fold'] == deploy_idx)

        # Load deploy fold's predictions parquet (cal split residuals).
        preds_path = train_dir / deploy_fold['predictions_path']
        if sha256_file(preds_path) != deploy_fold['predictions_sha256']:
            raise Phase5SchemaError("predictions_sha256 mismatch")
        preds_df = pd.read_parquet(preds_path, engine='pyarrow', dtype_backend='numpy_nullable')
        cal_df = preds_df[preds_df['split'] == 'cal'].reset_index(drop=True)
        if len(cal_df) < args.n_cell_floor:
            raise Phase5ContractError(
                f"cal split has {len(cal_df)} rows < N_CELL_FLOOR={args.n_cell_floor}"
            )

        artifact = fit_conformal(
            cal_df, alpha=args.alpha,
            bleed_collapse=args.bleed_collapse,
            n_cell_floor=args.n_cell_floor,
        )

        # market_blend_w from market_config.py (live read at fit time; bundle
        # records it for drift detection only).
        try:
            sys.path.insert(0, str(project_root))
            from market_config import MARKET_CONFIGS
            market_blend_w = float(MARKET_CONFIGS['15m'].market_blend_w)
            market_blend_w_source = 'market_config.py'
        except Exception:
            market_blend_w = 0.0
            market_blend_w_source = 'fallback_zero'

        # Write artifact tmp.
        artifact_final = train_dir / f"conformal_artifact_alpha{int(args.alpha * 100)}.json"
        artifact_tmp, conformal_sha = write_json_tmp(artifact, artifact_final)

        # Build phase 5 bundle.
        phase4_bundle_sha = bundle['bundle_sha']
        phase5_bundle_sha = hashlib.sha256(
            f"{phase4_bundle_sha}:{conformal_sha}".encode()
        ).hexdigest()

        phase5_bundle = dict(bundle)
        phase5_bundle.update({
            'phase': 5,
            'alpha': args.alpha,
            'n_cell_floor': args.n_cell_floor,
            'bleed_collapse_enabled': bool(args.bleed_collapse),
            'conformal_path': artifact_final.name,
            'conformal_sha256': conformal_sha,
            'phase4_bundle_sha': phase4_bundle_sha,
            'bundle_sha': phase5_bundle_sha,
            'market_blend_w': market_blend_w,
            'market_blend_w_source': market_blend_w_source,
            'phase5_generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
        })

        # Phase 5 writes its own bundle file (NEW filename) — does not
        # overwrite the Phase 4 bundle.
        phase5_bundle_final = train_dir / f"cal_mlp_{args.asset}_{bundle['train_id']}_phase5_bundle.json"
        phase5_tmp, phase5_sha = write_json_tmp(phase5_bundle, phase5_bundle_final)

        # Atomic rename: artifact first, then phase 5 bundle (gate).
        try:
            os.replace(artifact_tmp, artifact_final)
            os.replace(phase5_tmp, phase5_bundle_final)
            fsync_directory(train_dir)
            # Update CURRENT to point to this train_id (already pointed; idempotent).
            current_path = models_root / 'CURRENT'
            current_tmp = current_path.with_suffix(
                current_path.suffix + f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            )
            with open(current_tmp, 'wb') as f:
                f.write(bundle['train_id'].encode('utf-8'))
                f.flush()
                os.fsync(f.fileno())
            os.replace(current_tmp, current_path)
            fsync_directory(models_root)
            return phase5_bundle
        except Exception:
            for tmp in (artifact_tmp, phase5_tmp):
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
            raise


def main() -> None:
    args = parse_args()
    _setup_logging(args.quiet, args.verbose)
    try:
        bundle = run(args)
    except Phase5Error as e:
        logging.error("Phase5Error: %s", e)
        sys.exit(e.exit_code)
    except Exception:
        logging.exception("unexpected error")
        sys.exit(1)
    summary = {
        'train_id': bundle['train_id'],
        'cfg_fp': bundle['cfg_fp'],
        'asset': bundle['asset'],
        'alpha': bundle['alpha'],
        'bundle_sha': bundle['bundle_sha'],
    }
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
