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

**R1#C1 LOCKED 3D partition** — Phase 6 already-rebuilt code keys cells by `(price_tier, stc_bucket, vol_regime)`:

```python
cells = {(price_tier, stc_bucket, vol_regime): residuals_in_this_cell}   # 4 × 4 × 2 = 32 cells max
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

    # 2. Direct (price_tier, stc_bucket, vol_regime) lookup.
    # R1#C11: drop the redundant string-key field; lookup uses structured fields only.
    cell = next(
        (c for c in artifact['cells']
         if c['price_tier'] == pt and c['stc_bucket'] == sb and c['vol_regime'] == vr),
        None,
    )
    # R1#C4: cells with n_cal < N_CELL_FLOOR are NOT emitted at fit time;
    # if a cell entry exists, it has a valid q_alpha by construction.
    if cell:
        chain.append(f"mondrian[({pt},{sb},{vr})]")
        return cell['q_alpha'], chain

    # 3. Merged-axes fallback. R1#C6 LOCKED axis-name vocabulary:
    # merged_axes ⊆ {'price_tier', 'stc', 'vol_regime'} (note 'stc' not 'stc_bucket').
    merged = artifact.get('merged_axes', [])
    fallback_pt = 0 if 'price_tier' in merged else pt
    fallback_sb = 0 if 'stc' in merged else sb
    fallback_vr = 0 if 'vol_regime' in merged else vr
    fb_cell = next(
        (c for c in artifact['cells']
         if c['price_tier'] == fallback_pt
            and c['stc_bucket'] == fallback_sb
            and c['vol_regime'] == fallback_vr),
        None,
    )
    if fb_cell:
        chain.append(f"merged[({fallback_pt},{fallback_sb},{fallback_vr})]")
        return fb_cell['q_alpha'], chain

    # 4. Global fallback
    if 'global_q_alpha' in artifact:
        chain.append("global")
        return artifact['global_q_alpha'], chain

    chain.append("dispatch_miss")
    return None, chain
```

### Final interval (R1#C3 + R1#C10 — REVISED)

The earlier `q_alpha + ENSEMBLE_STD_MULTIPLIER * p_std` form sacrificed validity. Locked form:

```python
breakeven = market_implied_prob_for_side(entry_price_cents, side)   # R1#C10 rename
# market_blend_w default = 0 (production); >0 invalidates the conformal interval per R1#C10.
p_center = market_blend_w * breakeven + (1 - market_blend_w) * p_pred

q_alpha, chain = lookup_cell_quantile(artifact, row_features, mode)
if q_alpha is None:
    return p_center, p_std, None, None    # Phase 6 ship-blocker #7

# Pure conformal width (locked validity). Additive σ inflation is dropped
# from the runtime path — was breaking marginal coverage. Audit-mode kept
# as an option for ablation only.
half_width = q_alpha
final_lo_raw = p_center - half_width
final_hi_raw = p_center + half_width
clipped_lo = final_lo_raw < 0
clipped_hi = final_hi_raw > 1
final_lo = max(0.0, final_lo_raw)
final_hi = min(1.0, final_hi_raw)

if mode == 'audit':
    return p_center, p_std, final_lo, final_hi, {
        'q_alpha': q_alpha, 'half_width': half_width,
        'clipped_lo': clipped_lo, 'clipped_hi': clipped_hi,
        'chain': chain, 'p_pred_raw': p_pred,
    }
return p_center, p_std, final_lo, final_hi
```

**R1#C3:** the additive σ form is REMOVED from production path (sacrificed validity). Phase 6 verifies coverage empirically. If we want σ-aware widths in a future amendment, refit on normalized scores `score = |p̂-y| / max(σ̂, σ_floor)` — that preserves validity. Phase 5 fits on raw scores only for now.

**R1#C7:** `mode='audit'` returns a 5-tuple with the audit dict; Phase 6 reads `clipped_lo/clipped_hi/q_alpha` from there instead of duplicating the math.

**R1#C8 (market_blend_w source-of-truth):** the bundle's `market_blend_w` is INFORMATIONAL — recorded for drift detection. Production inference re-reads `market_config.MARKET_CONFIGS['15m'].market_blend_w` LIVE. Bundle's value is never used at inference except for the drift check Phase 6 emits as a ship-blocker.

**R1#C10 (blend invalidates conformal):** when `market_blend_w > 0`, the conformal interval `[p_center ± q_alpha]` is centered on a blended value but `q_alpha` was fit on `|p_pred - y|` residuals, NOT `|p_center - y|`. Validity claim breaks. Production default = 0; runtime override emits a soft-flag. Future amendment: refit conformal on `|p_blend - y|` residuals if `market_blend_w > 0` is desired.

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

  "conformal_path": "conformal_artifact.json",          // basename relative to bundle dir
  "conformal_sha256": "...",
  // R1#C17: normstats_path is NOT duplicated at top level. Phase 6 reads
  // bundle['eval_fold_artifacts'][bundle['deploy_fold_idx']]['normstats_path'].

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
      // R1#C11: structured fields only; no redundant string `key`.
      "price_tier": 0, "stc_bucket": 0, "vol_regime": 0,
      "n_cal": 184, "q_alpha": 0.121,
      "concentration_warning": false,
      "top_ticker_share": 0.04
    },
    ...
  ],
  // R1#C5: bleed_collapsed_by_merge is per-vol_regime when partial.
  "bleed_collapsed_by_merge_per_vr": {"0": false, "1": true}
}
```

`concentration_warning`: fired when a single ticker dominates >50% of a cell's cal rows. Phase 6 surfaces as soft-flag.

## Atomic write protocol

1. mkdir `models/cal_mlp_<asset>/<train_id>/` (Phase 4 already created; idempotent).
2. Write `conformal_artifact.json` tmp + fsync.
3. Write `phase5_bundle.json` tmp + fsync.
4. Rename in order: conformal_artifact → phase5 bundle (LAST).
5. fsync directory.
6. Update `models/cal_mlp_<asset>/CURRENT` (text file, atomic via tmp).

**R1#C12 (lock domain):** Phase 5 takes EXCLUSIVE on `models/cal_mlp_<asset>/.lock` ONLY. Does NOT take extract_lock SH — Phase 4's bundle SHA chain (`phase4_bundle_sha` includes `extract_bundle_logical_sha256`) is the integrity guarantee. If Phase 2 re-runs concurrently, Phase 5's loaded bundle still pins the correct artifact paths via SHA verification.

## Validity assumptions (R1#C9)

Mondrian split-conformal validity within each cell requires that cal residuals and test residuals be EXCHANGEABLE within that cell. With walk-forward folds (cal precedes test in time) and known regime drift (96¢ × 2-5min SOL bleed first detected Apr 26, 2026), within-cell stationarity is NOT guaranteed.

**Compensating controls:**
1. Phase 6 empirical-coverage check (cell-by-cell Wilson CI at α=0.20) is the post-hoc verification.
2. When a cell's empirical Wilson_lo drops below `(1-α) - tol`, retrigger conformal fit on a more-recent CAL window (operator action).
3. Phase 6 ship-blocker #4 fires on per-cell coverage shortfall; this is the load-bearing safety net.

The exchangeability assumption is acknowledged here so Phase 6 reviewers know the conformal interval's validity is empirical (not theoretical) on this dataset.

## bundle_sha_v1 chain

```python
phase5_bundle_sha = sha256(f"{phase4_bundle_sha}:{conformal_sha256}".encode()).hexdigest()
```

This extends Phase 4's `phase4_bundle_sha` (which itself was `sha256(model_id:normstats:phase4)`).

**R1#C13 (verification ownership):** Phase 6 verifies the conformal artifact sha (`conformal_sha256`) against the file via `_verify_artifact_sha`, but does NOT recompute the chained `phase5_bundle_sha`. Chain verification is reserved for Phase 7 (bot.py boot — unattended); Phase 6 is operator-driven and the operator-supplied `--bundle-sha` is the trust anchor.

Phase 7's contract (per Phase 4 R1#C6): at boot, recompute `phase4_bundle_sha = sha256(model_id:normstats:phase4)`, recompute `phase5_bundle_sha = sha256(phase4_bundle_sha:conformal_sha256)`, and assert match against `bundle['bundle_sha']`. Both are hard ship-blockers.

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
