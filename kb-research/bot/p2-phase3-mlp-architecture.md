# Phase 3: MLP Architecture Specification

**Status:** Round 1 spec.
**Anchor:** `kb-research/bot/p2-phases-4-to-8-design.md`.
**Prerequisite spec:** `kb-research/bot/p2-phase2-data-extraction.md` (converged at R5).
**Output:** binding architecture for Phase 4 training. No impl file in this phase — just the spec.

## Goal

Lock the MLP residual calibrator's architecture: layer dimensions, activations, dropout, normalization, output head, optimizer, loss, training loop semantics. Phase 2 already committed the SKIP-TERM (`final_prob = sigmoid(logit_raw_prob_clipped + Δ)`) and the LOSS FORM (`BCE × w_cell` where `w_cell = 1 + 4 × |p_cell - prior_cell|`). Phase 3 fleshes out the rest.

## Inputs (read from Phase 2 outputs)

For each fold:
- `parquet` with `CONT_FEATURE_COLS` (~21 continuous features, post-bucketization), 4 categorical buckets (price_tier, stc_bucket, vol_regime_int, side_int), `ticker_id`, `is_unk_ticker`, `logit_raw_prob_clipped`, `outcome`.
- `normstats_fold{F}.json` with per-column transform + mean/std/p1/p99/median/mad.
- `ticker_vocab.json` with asset-wide vocab (size `n_unique + 1` including UNK at index 0).

`apply_norm` (in `cal_mlp/normalize.py`) is called BY PHASE 4 at training time (not eagerly in parquet). The MLP input is the post-z-score continuous block + categorical embeddings + skip term.

## Architecture (LOCKED)

### Forward pass

```
inputs:
  x_cont: float32[B, N_CONT]                    # post-apply_norm continuous features (N_CONT=21)
  x_missing: int8[B, N_MISSING]                 # WS-feed missing indicators (N_MISSING=7), 0/1
  price_tier: int8[B]                           # 0..3
  stc_bucket: int8[B]                           # 0..3
  vol_regime_int: int8[B]                       # 0..1
  side_int: int8[B]                             # 0..1
  ticker_id: int32[B]                           # 0..n_vocab; 0 = UNK
  logit_raw_prob_clipped: float32[B]            # the SKIP TERM

# R1#C3 + R-p3-spec-r2#M2: is_unk_ticker is NOT a model input. Phase 4
# reads it from parquet at FOLD CONSTRUCTION TIME (train.py:693-704) and
# raises Phase4ContractError if (a) the column is missing OR (b) any
# train-split row has is_unk_ticker != 0. This is the same logical layer
# as the DataLoader (the `tr` DataFrame is what feeds Phase4Dataset),
# just earlier in the pipeline — earlier failure surfaces a cleaner error.
# Inference-time UNK routing is Phase 5's job (σ-inflation via conformal).

# Categorical one-hot
oh_price = one_hot(price_tier, 4)               # [B, 4]
oh_stc   = one_hot(stc_bucket, 4)               # [B, 4]
oh_vol   = one_hot(vol_regime_int, 2)           # [B, 2]
oh_side  = one_hot(side_int, 2)                 # [B, 2]

# Ticker embedding
emb_ticker = TickerEmbedding(ticker_id)         # [B, EMB_DIM]
# UNK index 0 is initialized PER-MEMBER with high-variance random vector
# (R2-ML#C8: forces ensemble disagreement on unseen tickers — forensic only)

# R1#C10: x_cont and x_missing both flow into the continuous block. Effective
# continuous dim = N_CONT + N_MISSING = 28.
x_cont_full = concat([x_cont, x_missing.float()], dim=-1)   # [B, 28]

# Concat
h = concat([x_cont_full, oh_price, oh_stc, oh_vol, oh_side, emb_ticker], dim=-1)
# h shape: [B, 28 + 4 + 4 + 2 + 2 + EMB_DIM] = [B, 44]

# MLP residual head
h = LayerNorm(INPUT_DIM)(h)
h = GELU(Linear(INPUT_DIM, HIDDEN_1))(h)        # HIDDEN_1 = 64
h = Dropout(0.1)(h)
h = LayerNorm(HIDDEN_1)(h)
h = GELU(Linear(HIDDEN_1, HIDDEN_2))(h)         # HIDDEN_2 = 32
h = Dropout(0.1)(h)
delta_raw = Linear(HIDDEN_2, 1)(h)              # [B, 1]
delta = clamp(delta_raw, -DELTA_LOGIT_CLAMP, +DELTA_LOGIT_CLAMP)  # ±2.5

# Skip-term combine
final_logit = logit_raw_prob_clipped + delta.squeeze(-1)
final_prob = sigmoid(final_logit)               # [B]
```

### Locked hyperparameters

| Name | Value | Justification |
|---|---|---|
| `EMB_DIM` | 4 | R1#C1: dropped from 8 → 4 because vocab ~1283 with `pct_tickers_with_only_one_row=0.72` means most tickers see 1 gradient step; 8-dim per-ticker would memorize. 4-dim halves embedding params (1284×4≈5.1k including UNK row) and leaves room for residual signal. Frequency-floor (collapse rare tickers to UNK at extract time) is a future Phase 2 amendment.|
| `HIDDEN_1` | 64 | conservative for ~10k-row train cohorts |
| `HIDDEN_2` | 32 | bottleneck → encourages residual signal |
| `DROPOUT` | 0.1 | regularization only (training mode); inference uses `model.eval()` so dropout is OFF. Ensemble std is the sole uncertainty signal (MC-dropout was DROPPED per anchor doc).|
| `DELTA_LOGIT_CLAMP` | 2.5 | sigmoid(2.5)/sigmoid(-2.5) ≈ 0.92/0.08 — bounds the calibrator's adjustment to ±~10pp from the prior even at extreme prior values; prevents pathological divergence on small folds |
| Activation | GELU | smooth alternative to ReLU, helps with the residual head's small-magnitude outputs |
| Normalization | LayerNorm | per-row stats; consistent with M=5 ensemble training (no batch-cross-ensemble interference) |
| Output activation | identity (with clamp) | logit-space output added to skip term; sigmoid is applied AFTER skip combine |

### UNK ticker handling (R2-ML#C8)

At training time, `is_unk_ticker` is always 0 (vocab is built from training data). Embedding row 0 (UNK) has zero gradient signal during training.

At inference time (Phase 7), an unseen ticker arrives. The deploy contract:
- `ticker_id = 0`, `is_unk_ticker = 1`.
- Phase 5 conformal wrapper detects `is_unk_ticker == 1` and forces the per-cell quantile to the GLOBAL quantile (not the cell-specific one), inflating σ.
- Phase 4's embedding INIT for row 0 is RANDOM PER-MEMBER (different across the M=5 ensemble seeds) so the ensemble std on UNK rows reflects honest disagreement, not zero. This is purely a forensic / sanity signal — Phase 5's σ-inflation is the load-bearing mechanism.

```python
# Phase 4 init (per-member) — R-p2-impl-r1#C2 reconcile: same formula across phases.
for member in range(M):
    member_seed = BASE_SEED * 1000 + member
    rng = np.random.default_rng(seed=member_seed)
    embedding_table[0] = rng.normal(0, EMB_DIM ** -0.5, size=EMB_DIM)
```

## Loss (carried forward from Phase 2 lock + R1#C2 precision tweak)

```python
# R1#C2: precision-weight by per-cell n to prevent thin small-n cells from
# dominating gradients. Lock N_TRAIN_PER_CELL_FLOOR = 50.
N_TRAIN_PER_CELL_FLOOR = 50

n_train_per_cell, p_cell, prior_cell = read_per_cell_stats(audit, fold=k)
p_safe     = np.nan_to_num(p_cell, nan=0.0)
prior_safe = np.nan_to_num(prior_cell, nan=0.0)
miscal_cell = np.where(n_train_per_cell > 0, np.abs(p_safe - prior_safe), 0.0)
precision_factor = np.minimum(n_train_per_cell / N_TRAIN_PER_CELL_FLOOR, 1.0)
w_cell = 1.0 + 4.0 * miscal_cell * precision_factor   # shape [n_cells]

# At bleed cell (n~821, miscal~0.06): w_cell ≈ 1.24
# At thin cell (n~30, miscal~0.40): w_cell ≈ 1 + 4 × 0.40 × 0.6 = 1.96 (down from 2.6)
# At empty cell: w_cell = 1.0 (unchanged)

# Per-row weight
def cell_idx(pt: int, sb: int) -> int:
    return pt * 4 + sb                              # 16 cells (4 × 4)

w_row = w_cell[cell_idx(price_tier, stc_bucket)]    # [B]

# Loss
final_logit = logit_raw_prob_clipped + delta
loss_per_row = F.binary_cross_entropy_with_logits(
    final_logit, outcome.float(), reduction='none'
)
loss = (loss_per_row * w_row).mean()
```

Audit JSON gains `per_cell_effective_weight` (the materialized `w_cell` table) and `per_cell_expected_gradient_mass = w_cell × n_train_per_cell` so reviewers can see where loss attention lands.

`binary_cross_entropy_with_logits` is numerically stable (uses log-sum-exp internally), so we feed `final_logit` directly without computing `sigmoid` in the forward pass before the loss. The forward pass returns `final_prob` for downstream metrics; training uses `final_logit` for the loss.

## Optimizer + schedule

| Component | Choice | Notes |
|---|---|---|
| Optimizer | AdamW | weight_decay=1e-4 — light regularization on the residual MLP |
| LR | 1e-3 (constant) | with cosine decay to 1e-4 over 80% of training |
| Batch size | 256 | balances per-step variance with gradient quality |
| Epochs | 30 | per-fold; early stopping on cal Brier stop-improvement for 5 epochs |
| Gradient clipping | max_norm=1.0 | prevents the rare large-batch gradient spike |
| LR warmup | 50 steps linear from 0 → 1e-3 (R1#C4: dropped from 200) | embedding's first few updates not dominating |
| Cosine decay window | 80% of POST-WARMUP steps | locks decay start = warmup_steps; ~880 of 930 steps decay |

The early-stopping signal is **per-cell weighted Brier** on the cal split using the **train-fold w_cell** weights (R1#C5: locked — close open question 6). Train-fold w_cell is a deterministic, locked quantity; using cal stats would give a moving early-stop target across folds with their own variance. Bias acknowledgment: the metric is biased toward training-cell distribution; this matches the loss objective by construction.

## Per-fold model output (per ensemble member)

Each member of the M=5 ensemble produces:
- `delta_logit_per_row: float32[N_test]` — the unbounded Δ before the skip combine.
- `final_prob_per_row: float32[N_test]` — sigmoid(skip + clamp(Δ, ±2.5)).
- `cal_brier: float`, `cal_brier_weighted: float` — for early stopping audit.

Phase 4 ensembles the M=5 per-row predictions:
- `p_mean = mean(final_prob_per_row, axis=members)` — the calibrator's point estimate.
- `p_std = std(final_prob_per_row, axis=members, ddof=0)` — ensemble uncertainty (population std; M=5 ARE the population).

Phase 5 reads `p_mean` and `p_std`, then applies the conformal wrapper to produce `[final_lo, final_hi]`.

## Fold selection for deploy (R1#C8 — locked)

The bundle's deployable predictions come from **fold K-1 (newest test)** ONLY. Earlier folds (0..K-2) are kept for stability auditing — Phase 6 reports fold-to-fold Brier delta as a soft-flag — but Phase 7 deploys **only fold K-1's per-member checkpoints**. Phase 5 reads `eval_fold_artifacts[K-1]` for conformal calibration (uses fold K-1's CAL split residuals). Phase 6 ship-blocker checks per-fold consistency; the production weights are fold K-1's.

Locking this here (in Phase 3) so Phase 4/5/6 don't re-derive.

## Determinism (R1#C6 — concrete CUDA spec)

- Each ensemble member uses a deterministic seed: `seed_member_m = BASE_SEED * 1000 + m`.
- `BASE_SEED` is recorded in the bundle and set on torch + numpy + Python `random`.
- DataLoader uses `worker_init_fn` to set per-worker seeds.
- CUDA setup (locked, applied BEFORE `import torch`):
  ```python
  os.environ['CUBLAS_WORKSPACE_CONFIG'] = ':4096:8'   # required by torch.use_deterministic_algorithms
  import torch
  torch.use_deterministic_algorithms(True, warn_only=False)
  torch.backends.cudnn.deterministic = True
  torch.backends.cudnn.benchmark = False
  ```
- CPU-only is the default; if `CUDA_VISIBLE_DEVICES` is non-empty AND `torch.cuda.is_available()`, log a startup banner and proceed. Any operator missing a deterministic CUDA implementation is a hard fail (the warn_only=False above guarantees).

## Memory and runtime budgets (R1#C7 — tightened)

| Budget | Target | Hard ceiling (SystemExit) |
|---|---|---|
| Peak RSS per asset training | 1 GB | 1.5 GB |
| Wall time per fold per member (CPU) | ≤ 90 s | 5 min |
| Total per-asset training | ≤ 25 min (3 folds × 5 members) | 60 min |

Tighter peak-RSS catches DataLoader leaks, accidental `pin_memory=True` with workers>1, and full-parquet-in-RAM bugs. RSS sampled every 100 steps via psutil; threshold breach → Phase4ResourceError.

`psutil` is required at training; missing → SystemExit (Phase 4's contract; Phase 6 already has the same rule). Phase 7 startup parity-asserts.

## Architecture parity with Phase 7 deploy

bot.py at Phase 7 deploy must instantiate the same architecture for inference. Phase 4 emits a `model_definition.json` in the bundle with:

```json
{
  "model_kind": "ResidualMLPV1",
  "n_cont": 21,
  "n_missing_indicator_cols": 7,
  "input_continuous_dim": 28,
  "n_price_tiers": 4,
  "n_stc_buckets": 4,
  "n_vol_regimes": 2,
  "n_sides": 2,
  "n_vocab": 1283,
  "emb_dim": 4,
  "hidden_1": 64,
  "hidden_2": 32,
  "dropout": 0.1,
  "delta_logit_clamp": 2.5,
  "raw_prob_clip_eps": 1e-6
}
```

Phase 7's bot.py amendment loads this JSON and constructs the model. Architecture drift between training and inference is a hard ship-blocker (Phase 6 verifies via a model_definition_sha that's part of `bundle_sha_v1`).

## Closed open questions (R1)

- Q1 EMB_DIM (R1#C1): locked to 4. Revisit if ensemble disagreement on ticker-OOD is implausibly low after Phase 6.
- Q5 (cosine decay window) (R1#C4): locked to 80% of post-warmup steps.
- Q6 (cal_brier_weighted weighting) (R1#C5): locked to train-fold w_cell.
- Q7 (is_unk_ticker training-time semantics) (R1#C3): NOT a model input; Phase 4 reads from parquet for asserts only; Phase 5 σ-inflation is the inference-time mechanism.

## Open questions for adversarial review (R2+)

1. `HIDDEN_1=64, HIDDEN_2=32` — the bottleneck-decreasing layout encourages residual capacity. Should we try equal layers (`64-64`)?
2. `DELTA_LOGIT_CLAMP=2.5` — at raw_prob=0.5, Δ=±2.5 → final_prob ∈ [0.076, 0.924]. At raw_prob=0.95 (logit≈2.94), Δ=±2.5 → final_prob ∈ [sigmoid(0.44), sigmoid(5.44)] ≈ [0.61, 0.996]. The clamp is asymmetric in probability space. Defensible or move clamp to `final_prob`?
3. AdamW vs SGD-with-momentum — AdamW handles small-data residual case better in our experience but worth ablating.
