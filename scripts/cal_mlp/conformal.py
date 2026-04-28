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
import fcntl
import hashlib
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

import numpy as np
import pandas as pd
import torch

sys.path.insert(0, str(Path(__file__).parent))

from features import BLEED_CELL  # noqa: E402
from _helpers import (  # noqa: E402
    N_CELL_FLOOR,
    fsync_directory,
    sha256_file,
    verify_artifact_sha,
    find_bundle_by_sha,
    load_bundle_with_dir,
    format_bleed_key,
)
from train import (  # noqa: E402
    CalibrationMLP,
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
    """Wrapper around _helpers.verify_artifact_sha that re-raises as
    Phase5SchemaError for the local exit-code contract."""
    try:
        verify_artifact_sha(path, expected)
    except RuntimeError as e:
        raise Phase5SchemaError(str(e)) from e


def _load_normstats(path: Path, expected_sha: Optional[str] = None) -> dict:
    if expected_sha:
        _verify_artifact_sha(path, expected_sha)
    return json.load(open(path))


def load_predictor(bundle: dict, device: torch.device) -> Predictor:
    """Read deploy fold K-1's per-member checkpoints, build SinglePredictor
    or EnsemblePredictor based on ensemble_size.

    R1#C1: requires `bundle['_bundle_dir']` to be populated (use
    `load_bundle_with_dir` from _helpers, NOT `json.load`)."""
    if '_bundle_dir' not in bundle:
        raise Phase5SchemaError(
            "load_predictor: bundle missing '_bundle_dir'. Use "
            "_helpers.load_bundle_with_dir(bundle_path) instead of json.load."
        )
    ensemble_size = bundle.get('ensemble_size', 1)
    deploy_idx = bundle.get('deploy_fold_idx',
                              max(r['fold'] for r in bundle['eval_fold_artifacts']))
    fold = next(r for r in bundle['eval_fold_artifacts'] if r['fold'] == deploy_idx)
    bundle_dir = Path(bundle['_bundle_dir'])
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

    Score = `|p_mean - outcome|`. Cells = (price_tier, stc_bucket, vol_regime),
    32 max. Cells with n >= floor get an emitted entry; cells with n < floor
    fall through to merged/global at lookup time. R1#C6: also emits
    cell_kind='fallback_empty' for cells with n_cal=0 so audit doesn't
    confuse "dispatch_miss" with "empty cell".
    """
    # Score = |p_mean - outcome|
    residuals = (cal_df['p_mean'].astype(np.float64) -
                  cal_df['outcome'].astype(np.float64)).abs().to_numpy()
    cal_df = cal_df.assign(_residual=residuals)
    n_cal_total = int(len(cal_df))
    global_q_alpha = float(np.quantile(residuals, 1 - alpha)) if n_cal_total > 0 else 1.0

    cells_out: list[dict] = []
    bleed_quantiles: dict[str, float] = {}
    bleed_key_axes: list[str] = ['vol_regime']
    seen_cells: set[tuple[int, int, int]] = set()
    bleed_above_floor: dict[int, bool] = {}  # vr → True if cell has n >= floor

    for (pt, sb, vr), sub in cal_df.groupby(['price_tier', 'stc_bucket', 'vol_regime']):
        pt, sb, vr = int(pt), int(sb), int(vr)
        seen_cells.add((pt, sb, vr))
        n = int(len(sub))
        is_bleed_cell_pt_sb = (pt, sb) == BLEED_CELL
        if n < n_cell_floor:
            if is_bleed_cell_pt_sb:
                bleed_above_floor.setdefault(vr, False)
            continue
        if is_bleed_cell_pt_sb:
            bleed_above_floor[vr] = True
        q_alpha = float(np.quantile(sub['_residual'].to_numpy(), 1 - alpha))
        ticker_counts = sub['ticker'].value_counts()
        top_share = float(ticker_counts.iloc[0] / n) if len(ticker_counts) else 0.0
        cells_out.append({
            'price_tier': pt, 'stc_bucket': sb, 'vol_regime': vr,
            'n_cal': n, 'q_alpha': q_alpha,
            'concentration_warning': bool(top_share > 0.5),
            'top_ticker_share': top_share,
            'cell_kind': 'mondrian',
        })

    # R1#C6: emit fallback_empty entries for the 32-cell space minus seen cells.
    for pt in range(4):
        for sb in range(4):
            for vr in range(2):
                if (pt, sb, vr) not in seen_cells:
                    cells_out.append({
                        'price_tier': pt, 'stc_bucket': sb, 'vol_regime': vr,
                        'n_cal': 0, 'q_alpha': global_q_alpha,
                        'concentration_warning': False,
                        'top_ticker_share': 0.0,
                        'cell_kind': 'fallback_empty',
                    })

    # R1#C3: bleed_per_vr is deterministic — set False if cell has n>=floor,
    # True if bleed cell exists and is below floor (or absent and bleed_collapse).
    bleed_per_vr: dict[str, bool] = {}
    for vr_i in (0, 1):
        if vr_i in bleed_above_floor and bleed_above_floor[vr_i]:
            bleed_per_vr[str(vr_i)] = False
        else:
            # Either explicitly below floor, or never appeared. Both cases
            # warrant bleed-fallback collapse if --bleed-collapse is on.
            bleed_per_vr[str(vr_i)] = bleed_collapse

    bleed_collapsed_overall = any(bleed_per_vr.values())
    if bleed_collapsed_overall:
        bleed_sub = cal_df[(cal_df['price_tier'] == BLEED_CELL[0]) &
                            (cal_df['stc_bucket'] == BLEED_CELL[1])]
        for vr_i in (0, 1):
            if not bleed_per_vr[str(vr_i)]:
                continue  # this vr has its own mondrian quantile
            sub_vr = bleed_sub[bleed_sub['vol_regime'] == vr_i]
            if len(sub_vr) >= 1:
                bleed_quantiles[f"vol_regime={vr_i}"] = float(
                    np.quantile(sub_vr['_residual'].to_numpy(), 1 - alpha)
                )
            else:
                bleed_quantiles[f"vol_regime={vr_i}"] = global_q_alpha

    return {
        'alpha': alpha,
        'n_cal_total': n_cal_total,
        'global_q_alpha': global_q_alpha,
        'merged_axes': [],
        'bleed_collapsed_by_merge': bleed_collapsed_overall,
        'bleed_collapsed_by_merge_per_vr': bleed_per_vr,
        'bleed_fallback_quantiles': {
            'key_axes': bleed_key_axes,
            'quantiles': bleed_quantiles,
        },
        'cells': cells_out,
    }


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


# R1#C8: sha256_file moved to _helpers.py; imported above.


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


# R1#C9: find_bundle_by_sha lives in _helpers.py; imported above.


def run(args: argparse.Namespace) -> dict:
    project_root = Path(__file__).resolve().parents[2]
    models_root = project_root / 'models' / f'cal_mlp_{args.asset}'
    if not models_root.exists():
        raise Phase5ContractError(f"no models dir for {args.asset}: {models_root}")

    try:
        bundle_path = find_bundle_by_sha(models_root, args.asset, args.bundle_sha)
    except RuntimeError as e:
        raise Phase5SchemaError(str(e)) from e
    bundle = load_bundle_with_dir(bundle_path)  # populates _bundle_dir
    if bundle.get('phase') != 4:
        raise Phase5SchemaError(
            f"input bundle phase={bundle.get('phase')}; Phase 5 requires Phase 4"
        )
    # R1#C14: cfg_fp cross-check between Phase 4 bundle and model_definition.
    train_dir = bundle_path.parent
    model_def = json.load(open(train_dir / bundle['model_definition_path']))
    if (bundle.get('cfg_fp')
            and model_def.get('cfg_fp')
            and bundle['cfg_fp'] != model_def['cfg_fp']):
        raise Phase5SchemaError(
            f"cfg_fp mismatch: bundle={bundle['cfg_fp']} model_def={model_def['cfg_fp']}"
        )

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
        # R1#C10/C12: narrow exception scope; sys.path entry cleaned up in finally.
        sys_path_added = False
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))
            sys_path_added = True
        try:
            try:
                from market_config import MARKET_CONFIGS
                market_blend_w = float(MARKET_CONFIGS['15m'].market_blend_w)
                market_blend_w_source = 'market_config.py'
            except (ImportError, KeyError, AttributeError) as e:
                raise Phase5ContractError(
                    f"failed to load market_config.MARKET_CONFIGS['15m'].market_blend_w: {e}"
                ) from e
        finally:
            if sys_path_added:
                try:
                    sys.path.remove(str(project_root))
                except ValueError:
                    pass

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
        # R1#C11: fsync between renames to limit stranded-artifact window.
        try:
            os.replace(artifact_tmp, artifact_final)
            fsync_directory(train_dir)
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
