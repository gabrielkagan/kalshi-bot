#!/usr/bin/env python3
"""Capture v1 cal_mlp predictor baseline outputs for the Phase 2 TDD anchor.

Runs the production CURRENT bundle for each of BTC/ETH/SOL/XRP against a
fixed 6-row stratified synthetic corpus (see CORPUS.md) and writes the
exact (cal_prob, ens_std, final_lo, final_hi) outputs to
`cal_mlp_v1_baseline_<asset>.json` in this directory.

Usage:
    # Initial capture (refuses to overwrite existing snapshots):
    python3 tests/integration/cal_mlp_v1_baseline/capture_baseline.py

    # Deliberate regen (e.g., after a KB-documented engine-math change):
    python3 tests/integration/cal_mlp_v1_baseline/capture_baseline.py --regen

The companion test `test_calmlp_v1_baseline_predict.py` re-runs the predictor
against this corpus and asserts byte-stable outputs.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make this script runnable from anywhere — anchor to repo root via __file__.
_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parents[2]
_CAL_MLP_DIR = _REPO_ROOT / 'scripts' / 'cal_mlp'

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_CAL_MLP_DIR) not in sys.path:
    sys.path.insert(0, str(_CAL_MLP_DIR))

ASSETS = ('BTC', 'ETH', 'SOL', 'XRP')

# Corpus row definitions. See CORPUS.md for rationale.
# Each row is independent of asset; ticker is asset-templated below.
_CORPUS_ROWS = [
    # (row_id, price_cents, seconds_to_close, sigma, hour, raw_prob)
    (1, 60, 720, 0.5, 12, 0.62),
    (2, 75, 360, 1.2, 0, 0.78),
    (3, 85, 600, 0.8, 18, 0.87),
    (4, 92, 180, 2.0, 6, 0.93),
    (5, 97, 120, 0.3, 14, 0.97),
    (6, 99, 60, 0.1, 9, 0.99),
]


def _build_row_features(price_cents: int, stc: int, sigma: float, hour: int,
                        raw_prob: float) -> dict:
    """Derive the 8 v1 CONT_FEATURE_COLS values deterministically.

    Formulas mirror `bot/helpers/derived_features.compute_derived_features`
    + `scripts/cal_mlp/features.compute_hour_features`. We intentionally
    inline them here rather than importing the helpers, so the capture
    corpus is a self-contained pinned artifact: a future drift in the
    helpers will be caught by the predict-output snapshot test, NOT by
    a hidden re-derivation in this capture script."""
    abs_sigma = abs(sigma)
    time_decayed_proximity = sigma * (1.0 - stc / 900.0)
    prob_breakeven_gap = raw_prob - price_cents / 100.0
    angle = 2.0 * math.pi * hour / 24.0
    hour_sin = math.sin(angle)
    hour_cos = math.cos(angle)
    return {
        'seconds_to_close': stc,
        'spot_distance_to_strike_sigma': sigma,
        'abs_spot_distance_to_strike_sigma': abs_sigma,
        'time_decayed_proximity': time_decayed_proximity,
        'prob_breakeven_gap': prob_breakeven_gap,
        'hour_sin': hour_sin,
        'hour_cos': hour_cos,
        # market_price not in row_features dict — passed via
        # entry_price_cents arg; predictor sets it on the internal row.
    }


def _read_bundle_cfg_fp(asset: str, train_id: str) -> str:
    """Read cfg_fp directly from the phase5 bundle JSON. The predictor
    instance does not expose `cfg_fp` as an attribute (it lives in the
    bundle dict, discarded after `_load`). We pin the load-bearing
    fingerprint as it actually lives in the bundle, not as a hardcoded
    string — that way a future regen against a different bundle records
    the bundle's TRUE cfg_fp (and downstream invariant tests catch the
    drift via the train_id check)."""
    bundle_path = (_REPO_ROOT / 'models' / f'cal_mlp_{asset}'
                   / train_id / f'cal_mlp_{asset}_{train_id}_phase5_bundle.json')
    bundle = json.loads(bundle_path.read_text())
    return bundle['cfg_fp']


def _capture_asset(asset: str) -> dict:
    import integration  # noqa: E402 — heavy bot.* + torch chain
    predictor = integration.CalMLPPredictor(asset, project_root=_REPO_ROOT)
    predictor.warmup()
    if predictor.train_id is None:
        raise RuntimeError(
            f"{asset}: warmup did not load a bundle (CURRENT pointer missing?). "
            f"Check models/cal_mlp_{asset}/CURRENT."
        )
    cfg_fp = _read_bundle_cfg_fp(asset, predictor.train_id)

    rows = []
    for row_id, price_cents, stc, sigma, hour, raw_prob in _CORPUS_ROWS:
        ticker = f'KX{asset}15M-26APR28-T{price_cents}'
        row_features = _build_row_features(price_cents, stc, sigma, hour, raw_prob)
        cal_prob, ens_std, final_lo, final_hi = predictor.predict(
            raw_prob=raw_prob,
            ticker=ticker,
            side='yes',
            entry_price_cents=price_cents,
            row_features=row_features,
        )
        rows.append({
            'row_id': row_id,
            'inputs': {
                'raw_prob': raw_prob,
                'price_cents': price_cents,
                'side': 'yes',
                'ticker': ticker,
                'row_features': row_features,
            },
            'outputs': {
                'cal_prob': cal_prob,
                'ens_std': ens_std,
                'final_lo': final_lo,
                'final_hi': final_hi,
            },
        })

    return {
        'asset': asset,
        'train_id': predictor.train_id,
        'cfg_fp': cfg_fp,  # read from bundle JSON, not hardcoded
        'market_blend_w': predictor.market_blend_w,
        'captured_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'rows': rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--regen', action='store_true',
                        help='Overwrite existing snapshot JSONs. Human-only operation.')
    args = parser.parse_args()

    for asset in ASSETS:
        snap_path = _THIS_DIR / f'cal_mlp_v1_baseline_{asset}.json'
        if snap_path.exists() and not args.regen:
            print(f'{asset}: snapshot exists at {snap_path.name} — pass --regen to overwrite. SKIPPED.')
            continue
        print(f'{asset}: capturing baseline...')
        snap = _capture_asset(asset)
        snap_path.write_text(json.dumps(snap, indent=2, sort_keys=False) + '\n')
        print(f'{asset}: wrote {snap_path.name} (train_id={snap["train_id"][:30]}..., '
              f'{len(snap["rows"])} rows)')

    return 0


if __name__ == '__main__':
    sys.exit(main())
