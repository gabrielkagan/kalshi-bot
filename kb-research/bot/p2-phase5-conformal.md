# Phase 5: Conformal Wrapper (`conformal.py` + `_helpers.py`)

**Status:** Round 1 spec.
**Anchor:** `kb-research/bot/p2-phases-4-to-8-design.md`.
**Prerequisite specs:** Phase 2 (data, converged), Phase 3 (architecture, converged), Phase 4 (training, R1 in progress).
**Output:** Phase 5 bundle wrapping Phase 4 + per-cell conformal quantile artifact + `_helpers.py` runtime API consumed by Phase 6/7.

## Goal

Wrap the M=5 ensemble predictor with **Mondrian conformal prediction** at α=0.20 (80% nominal coverage). Produce per-cell residual quantiles from fold K-1's CAL split (the locked deploy fold per Phase 3 R1#C8). Emit `[final_lo, final_hi]` intervals at inference time via `predict_with_interval`.

## Inputs (CLI)

```
conformal.py --asset {BTC,ETH,SOL,XRP}
             --bundle-sha <hex>            # Phase 4 bundle to wrap
             [--alpha 0.20]                # locked default
             [--bleed-collapse]            # default ON; off for ablation
             [--n-cell-floor 20]           # below: fall back to global quantile
             [--device cpu|cuda]
             [--quiet|--verbose]
```

## Conformal scoring (LOCKED)

**Nonconformity score:** absolute residual on the calibrator's predicted probability.

```python
def score(p_hat: float, outcome: int) -> float:
    return abs(p_hat - float(outcome))    # ∈ [0, 1]
```

Computed on fold K-1's CAL split rows. Per row:
- `p_hat` = ensemble mean from Phase 4: `cal_p_mean[i]`
- `outcome` = target ∈ {0, 1}

## Mondrian cells (per-cell quantiles)

The conformal partition matches Phase 2's bucketization:

```python
cells = {(price_tier, stc_bucket): residuals_in_this_cell}    # 4 × 4 = 16 cells max
```

For each cell, compute the empirical `(1 - α)`-quantile:

```python
q_alpha[cell] = quantile(residuals_in_this_cell, 1 - alpha)
# At α=0.20, q_alpha is the 80th percentile of |p_hat - y| within the cell.
```

**Edge cases:**
- `n_cell < N_CELL_FLOOR` (default 20): cell falls back to GLOBAL quantile (computed on union of all cal residuals across cells).
- `n_cell == 0`: cell uses GLOBAL quantile, flagged as `cell_kind: 'fallback_empty'`.

## Bleed-cell collapse-by-merge (`--bleed-collapse`, default ON)

The bleed cell `(price_tier=3, stc_bucket=2)` is the project KPI. If its `n_cell < N_CELL_FLOOR` (always plausible for SOL/XRP given ≥96¢ × 300-600s is rare), the global-quantile fallback under-represents the bleed's higher residual variance.

**Mitigation:** when bleed cell is below floor AND `--bleed-collapse` is ON, collapse the bleed quantile by merging across the `key_axes` dimensions documented in the conformal artifact:

```python
bleed_fallback_quantiles = {
    'key_axes': ['vol_regime'],   # merge across vol_regime, keep price_tier=3 + stc_bucket=2
    'quantiles': {
        # Sub-key per remaining axis combo
        'vol_regime=0': 0.18,
        'vol_regime=1': 0.22,
    }
}
```

If `key_axes` ends up empty (all dimensions merged), the bleed fallback is the merged-cells quantile across price_tier=3, stc_bucket=2 regardless of vol_regime.

`bleed_collapsed_by_merge: bool` flag in the artifact records whether collapse fired.

## Predictor interface (`SinglePredictor` and `EnsemblePredictor`)

Phase 4 may produce M=1 or M=5 model bundles. Phase 5 supports both via a shared `Predictor` interface:

```python
class Predictor(Protocol):
    def predict(self, batch: dict) -> tuple[Tensor, Tensor]:
        """Returns (p_mean[B], p_std[B]). For SinglePredictor, p_std=0.
        For EnsemblePredictor, p_std is across M members with ddof=0."""

class SinglePredictor:
    """Wraps a single MLP for the M=1 ablation case."""
    def __init__(self, model: nn.Module, device: torch.device): ...
    def predict(self, batch): ...      # p_std = zeros

class EnsemblePredictor:
    """Wraps M=5 MLPs."""
    def __init__(self, models: list[nn.Module], device: torch.device): ...
    def predict(self, batch):
        all_preds = torch.stack([m(batch) for m in self.models])  # [M, B]
        p_mean = all_preds.mean(dim=0)
        p_std = all_preds.std(dim=0, unbiased=False)              # ddof=0
        return p_mean, p_std

def load_predictor(bundle: dict, device: torch.device) -> Predictor:
    """Inspects bundle['ensemble_size']; loads M=1 → SinglePredictor or
    M=5 → EnsemblePredictor by reading checkpoint paths from bundle's
    eval_fold_artifacts[K-1].members."""
```

Phase 6 A/B comparisons use this interface to swap base/challenger without code change. The `cfg_fp` gate in Phase 6 ensures the two bundles share the same feature schema.

## `predict_with_interval` (the runtime API)

```python
def predict_with_interval(
    p_pred: float,                # ensemble mean from predictor
    p_std: float,                 # ensemble std
    conformal_artifact: dict,     # loaded from Phase 5 bundle
    row_features: dict,           # {'price_tier': int, 'stc_bucket': int, 'vol_regime': int}
    entry_price_cents: int,
    side: str,                    # 'yes' or 'no'
    market_blend_w: float,        # 0..1, blends prior with raw market_implied_prob
    mode: str,                    # 'inference' or 'audit'
) -> tuple[float, float, Optional[float], Optional[float]]:
    """Returns (p_mean, p_std, final_lo, final_hi) where:
      - p_mean is the input ensemble mean, possibly blended with market prior.
      - final_lo, final_hi are conformal interval bounds in [0, 1].
      - final_lo = None if cell dispatch failed (Phase 6 ship-blocker #7).
    """
```

### Lookup chain (`lookup_cell_quantile`)

```python
def lookup_cell_quantile(
    artifact: dict,
    row_features: dict,
    mode: str,
) -> tuple[Optional[float], list[str]]:
    """Returns (q_alpha, dispatch_chain) — q_alpha=None means dispatch missed.
    The chain is the audit trail of which lookup paths fired."""
    chain = []
    pt = row_features['price_tier']
    sb = row_features['stc_bucket']
    vr = row_features['vol_regime']

    # 1. Bleed cell + collapse-by-merge
    if (pt, sb) == (3, 2) and artifact.get('bleed_collapsed_by_merge'):
        bleed = artifact['bleed_fallback_quantiles']
        key = ','.join(f"{a}={row_features[a]}" for a in bleed['key_axes']) or '_all'
        q = bleed['quantiles'].get(key)
        if q is not None:
            chain.append(f"bleed[{key}]")
            return q, chain

    # 2. Direct (price_tier, stc_bucket, vol_regime) lookup
    direct_key = f"({pt},{sb},{vr})"
    cell = next((c for c in artifact['cells'] if c['key'] == direct_key), None)
    if cell and cell.get('n_cal') >= N_CELL_FLOOR_INFER:
        chain.append(f"mondrian[{direct_key}]")
        return cell['q_alpha'], chain

    # 3. Merged-axes fallback (collapse vol_regime if its axis was merged at fit)
    merged = artifact.get('merged_axes', [])
    fallback_pt = 0 if 'price_tier' in merged else pt
    fallback_sb = 0 if 'stc' in merged else sb
    fallback_vr = 0 if 'vol_regime' in merged else vr
    fallback_key = f"({fallback_pt},{fallback_sb},{fallback_vr})"
    fb_cell = next((c for c in artifact['cells'] if c['key'] == fallback_key), None)
    if fb_cell:
        chain.append(f"merged[{fallback_key}]")
        return fb_cell['q_alpha'], chain

    # 4. Global fallback
    if 'global_q_alpha' in artifact:
        chain.append("global")
        return artifact['global_q_alpha'], chain

    chain.append("dispatch_miss")
    return None, chain
```

### Final interval

```python
breakeven = market_implied_prob_yes(entry_price_cents, side)
# market_blend_w blends p_pred with breakeven for ensemble-noisy rows
p_blend = market_blend_w * breakeven + (1 - market_blend_w) * p_pred

q_alpha, chain = lookup_cell_quantile(artifact, row_features, mode)
if q_alpha is None:
    return p_blend, p_std, None, None    # Phase 6 ship-blocker #7

# Conformal interval; expand by ensemble std (Phase 6 R-p6-impl-2 lock)
sigma_term = ENSEMBLE_STD_MULTIPLIER * p_std    # default multiplier = 0.5
half_width = q_alpha + sigma_term
final_lo = max(0.0, p_blend - half_width)
final_hi = min(1.0, p_blend + half_width)
return p_blend, p_std, final_lo, final_hi
```

`ENSEMBLE_STD_MULTIPLIER = 0.5` is locked (Phase 5 audit-mode optionally allows tuning to study width trade-off vs coverage).

## `_helpers.py` API surface

```python
# Functions Phase 6 (and Phase 7) imports from cal_mlp/_helpers.py:

def market_implied_prob_yes(entry_price_cents: int, side: str) -> float:
    """For YES side: breakeven = price/100. For NO side: breakeven = 1 - price/100.
    The 'price' is the YES ask in cents; trades on YES win at $1 - $price."""

def predict_with_interval(...) -> tuple: ...   # see above

def lookup_cell_quantile(...) -> tuple: ...    # see above

def wilson_ci(n_success: int, n_total: int, z: float = 1.96) -> tuple[float, float]:
    """80% CI on a binomial proportion; lower/upper bounds. n_total=0 → (0, 1)."""

def fsync_directory(path: Path) -> None:
    """fsync a directory file descriptor for atomicity guarantees."""
```

These functions are pure (no I/O except fsync_directory) and have no torch dependency. Phase 7 imports them at bot startup.

## `conformal.py` — fit + bundle write

```python
def fit_conformal(
    asset: str,
    phase4_bundle: dict,
    alpha: float,
    bleed_collapse: bool,
    n_cell_floor: int,
    device: torch.device,
) -> dict:
    """Returns the conformal_artifact dict to be saved.

    1. Load fold K-1's predictions parquet (cal split).
    2. For each row, compute residual = |p_mean - outcome|.
    3. Group by (price_tier, stc_bucket, vol_regime) → cells.
    4. Compute (1-α)-quantile per cell where n_cell ≥ floor.
    5. Compute global quantile across all cal residuals.
    6. If bleed cell is below floor and bleed_collapse: compute fallback.
    7. Build cells list + bleed_fallback_quantiles + merged_axes meta.
    """

def _verify_artifact_sha(path: Path, expected_sha: str) -> None:
    """Read file, compute sha256, raise on mismatch."""

def _load_normstats(path: Path, expected_sha: Optional[str] = None) -> dict:
    """Load normstats JSON; verify sha256 if given."""
```

## Bundle JSON (Phase 5 wraps Phase 4)

```json
{
  "phase": 5,
  "schema_version": 2,
  "asset": "SOL",
  "train_id": "...",
  "cfg_fp": "...",
  "alpha": 0.20,
  "bleed_collapse_enabled": true,
  "n_cell_floor": 20,
  "ensemble_std_multiplier": 0.5,

  "phase4_bundle_path": "/abs/path/cal_mlp_SOL_<train_id>_bundle.json",
  "phase4_bundle_sha256": "...",
  "model_definition_path": "...",
  "model_definition_sha256": "...",

  "conformal_path": "/abs/path/conformal_artifact.json",
  "conformal_sha256": "...",
  "normstats_path": "/abs/path/normstats_fold2.json",   // fold K-1
  "normstats_sha256": "...",

  "market_blend_w": 0.0,                     // resolved at fit time
  "market_blend_w_source": "market_config.py",

  "eval_fold_artifacts": [...],              // copied from Phase 4 bundle, paths absolutized
  "deploy_fold_idx": 2,                       // K-1; Phase 6/7 read this

  "bundle_sha": "<sha256 of phase4_bundle_sha256:conformal_sha256>",
  "generated_at": "...",
  "conformal_py_sha256": "..."
}
```

## Conformal artifact JSON (separate file)

```json
{
  "alpha": 0.20,
  "n_cal_total": 2103,
  "global_q_alpha": 0.142,
  "ensemble_std_multiplier": 0.5,
  "merged_axes": [],                         // axes that collapsed for low-n
  "bleed_collapsed_by_merge": true,
  "bleed_fallback_quantiles": {
    "key_axes": ["vol_regime"],
    "quantiles": {"vol_regime=0": 0.18, "vol_regime=1": 0.22}
  },
  "cells": [
    {
      "key": "(0,0,0)",
      "price_tier": 0, "stc_bucket": 0, "vol_regime": 0,
      "n_cal": 184, "q_alpha": 0.121,
      "concentration_warning": false,
      "top_ticker_share": 0.04
    },
    ...
  ]
}
```

`concentration_warning`: fired when a single ticker dominates >50% of a cell's cal rows. Phase 6 surfaces as soft-flag.

## Atomic write protocol

1. mkdir `models/cal_mlp_<asset>_<train_id>/`
2. Write conformal_artifact.json tmp + fsync
3. Write phase5_bundle.json tmp + fsync
4. Rename in order: conformal_artifact → phase5 bundle (LAST)
5. fsync directory
6. Update `models/CURRENT_<asset>` (text file, atomic via tmp)

Same lock domain split as Phase 4: SHARED on extract_lock + EXCLUSIVE on models_lock.

## bundle_sha_v1 chain

```python
phase5_bundle_sha = sha256(f"{phase4_bundle_sha}:{conformal_sha256}".encode()).hexdigest()
```

This extends Phase 4's `phase4_bundle_sha` (which itself was `sha256(model_id:normstats:phase4)`). Phase 6 verifies via a single sha256 by recomputing the chain.

## Determinism

- Conformal fit is deterministic given fixed predictions parquet + locked alpha.
- No RNG used.

## Failure modes (locked exit codes)

```python
class Phase5Error(RuntimeError): exit_code = 1
class Phase5DBError(Phase5Error): exit_code = 2          # n/a; reserved
class Phase5ContractError(Phase5Error): exit_code = 3
class Phase5LockError(Phase5Error): exit_code = 4
class Phase5WriteError(Phase5Error): exit_code = 5
class Phase5SchemaError(Phase5Error): exit_code = 6
```

## What this phase does NOT do

- No re-training (Phase 4 owns).
- No ship-blocker evaluation (Phase 6 owns).
- No direct DB access — operates entirely on Phase 4 bundle artifacts.

## Open questions for adversarial review

1. `ENSEMBLE_STD_MULTIPLIER = 0.5` — heuristic. Should it be data-derived (e.g., calibrated on cal residuals so coverage hits exactly 1-α)?
2. `N_CELL_FLOOR = 20` — below this, fall back to global. Is 20 enough for the conformal quantile to be stable? Theoretically, 80th percentile on n=20 has ±2 quantile-rank uncertainty.
3. Bleed collapse-by-merge — currently only `key_axes=['vol_regime']` is implemented. What about price_tier or stc collapse if bleed cell is empty even after vol_regime merge?
4. `market_blend_w` precedence — Phase 6 already implements CLI > ENV > market_config > bundle. Phase 5 just reads market_config.py at fit time and records source. Phase 6 may override at validation time. Verify cross-phase consistency.
5. `cfg_fp` cross-check between Phase 4 bundle and conformal fit — Phase 5 assert that the loaded predictions came from a Phase 4 bundle whose `cfg_fp` matches the model_definition.json. If it doesn't, raise Phase5SchemaError.
6. The `cells` list in artifact — should keys be string `"(p,s,v)"` or structured `{'price_tier': p, 'stc_bucket': s, 'vol_regime': v}`? Phase 6 already uses `cell['price_tier']` access, suggesting structured. Lock here.
7. Are there per-cell `concentration_warning` thresholds beyond top-ticker-share>0.5? E.g., top-3-tickers > 0.7?
