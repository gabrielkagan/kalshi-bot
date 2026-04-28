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
  x_cont: float32[B, N_CONT]                    # post-apply_norm continuous features
  price_tier: int8[B]                           # 0..3
  stc_bucket: int8[B]                           # 0..3
  vol_regime_int: int8[B]                       # 0..1
  side_int: int8[B]                             # 0..1
  ticker_id: int32[B]                           # 0..n_vocab; 0 = UNK
  is_unk_ticker: int8[B]                        # 0 in train; may be 1 at inference
  logit_raw_prob_clipped: float32[B]            # the SKIP TERM

# Categorical one-hot
oh_price = one_hot(price_tier, 4)               # [B, 4]
oh_stc   = one_hot(stc_bucket, 4)               # [B, 4]
oh_vol   = one_hot(vol_regime_int, 2)           # [B, 2]
oh_side  = one_hot(side_int, 2)                 # [B, 2]

# Ticker embedding
emb_ticker = TickerEmbedding(ticker_id)         # [B, EMB_DIM]
# UNK index 0 is initialized PER-MEMBER with high-variance random vector
# (R2-ML#C8: forces ensemble disagreement on unseen tickers)

# Concat
h = concat([x_cont, oh_price, oh_stc, oh_vol, oh_side, emb_ticker], dim=-1)
# h shape: [B, N_CONT + 4 + 4 + 2 + 2 + EMB_DIM] = [B, INPUT_DIM]

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
| `EMB_DIM` | 8 | small per-ticker capacity; vocab is ~1500/asset |
| `HIDDEN_1` | 64 | conservative for ~10k-row train cohorts |
| `HIDDEN_2` | 32 | bottleneck → encourages residual signal |
| `DROPOUT` | 0.1 | mild regularization; ensemble dominates uncertainty |
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
# Phase 4 init (per-member):
for member in range(M):
    rng = np.random.default_rng(seed=BASE_SEED + member)
    embedding_table[0] = rng.normal(0, EMB_DIM ** -0.5, size=EMB_DIM)
```

## Loss (carried forward from Phase 2 lock)

```python
# w_cell precomputed at Phase 4 fit-time from train fold's per-cell stats
# (read from extract_audit.json's per_fold[k].per_cell):
n_train_per_cell, p_cell, prior_cell = read_per_cell_stats(audit, fold=k)
p_safe     = np.nan_to_num(p_cell, nan=0.0)
prior_safe = np.nan_to_num(prior_cell, nan=0.0)
miscal_cell = np.where(n_train_per_cell > 0, np.abs(p_safe - prior_safe), 0.0)
w_cell = 1.0 + 4.0 * miscal_cell                    # shape [n_cells]; one entry per (price_tier, stc_bucket)

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

`binary_cross_entropy_with_logits` is numerically stable (uses log-sum-exp internally), so we feed `final_logit` directly without computing `sigmoid` in the forward pass before the loss. The forward pass returns `final_prob` for downstream metrics; training uses `final_logit` for the loss.

## Optimizer + schedule

| Component | Choice | Notes |
|---|---|---|
| Optimizer | AdamW | weight_decay=1e-4 — light regularization on the residual MLP |
| LR | 1e-3 (constant) | with cosine decay to 1e-4 over 80% of training |
| Batch size | 256 | balances per-step variance with gradient quality |
| Epochs | 30 | per-fold; early stopping on cal Brier stop-improvement for 5 epochs |
| Gradient clipping | max_norm=1.0 | prevents the rare large-batch gradient spike |
| LR warmup | 200 steps linear from 0 → 1e-3 | helps with the small clamped Δ output head |

The early-stopping signal is **per-cell weighted Brier** on the cal split (using the same `w_cell` weights as training loss). This aligns the stopping criterion with the loss objective.

## Per-fold model output (per ensemble member)

Each member of the M=5 ensemble produces:
- `delta_logit_per_row: float32[N_test]` — the unbounded Δ before the skip combine.
- `final_prob_per_row: float32[N_test]` — sigmoid(skip + clamp(Δ, ±2.5)).
- `cal_brier: float`, `cal_brier_weighted: float` — for early stopping audit.

Phase 4 ensembles the M=5 per-row predictions:
- `p_mean = mean(final_prob_per_row, axis=members)` — the calibrator's point estimate.
- `p_std = std(final_prob_per_row, axis=members, ddof=0)` — ensemble uncertainty (population std; M=5 ARE the population).

Phase 5 reads `p_mean` and `p_std`, then applies the conformal wrapper to produce `[final_lo, final_hi]`.

## Determinism

- Each ensemble member uses a deterministic seed: `seed_member_m = BASE_SEED * 1000 + m`.
- `BASE_SEED` is recorded in the bundle and set on torch + numpy + Python `random`.
- DataLoader uses `worker_init_fn` to set per-worker seeds.
- CUDA non-determinism: training is CPU-default (datasets are small); if GPU is enabled, torch's `deterministic=True` is set even at the cost of throughput.

## Memory and runtime budgets

| Budget | Target | Rationale |
|---|---|---|
| Peak RSS per asset training | 4 GB | M=5 × ~5 MB per model state + batches + DataLoader buffers |
| Wall time per fold per member (CPU) | ≤ 90 s | 30 epochs × 12k batches × O(B × INPUT_DIM × HIDDEN_1) |
| Total per-asset training | ≤ 25 minutes (3 folds × 5 members) | fits comfortably in a cron window |

`psutil` is required at training; missing → SystemExit (Phase 4's contract; Phase 6 already has the same rule). Phase 7 startup parity-asserts.

## Architecture parity with Phase 7 deploy

bot.py at Phase 7 deploy must instantiate the same architecture for inference. Phase 4 emits a `model_definition.json` in the bundle with:

```json
{
  "model_kind": "ResidualMLPV1",
  "n_cont": 21,
  "n_price_tiers": 4,
  "n_stc_buckets": 4,
  "n_vol_regimes": 2,
  "n_sides": 2,
  "n_vocab": 1283,
  "emb_dim": 8,
  "hidden_1": 64,
  "hidden_2": 32,
  "dropout": 0.1,
  "delta_logit_clamp": 2.5,
  "raw_prob_clip_eps": 1e-6
}
```

Phase 7's bot.py amendment loads this JSON and constructs the model. Architecture drift between training and inference is a hard ship-blocker (Phase 6 verifies via a model_definition_sha that's part of `bundle_sha_v1`).

## Open questions for adversarial review

1. `EMB_DIM=8` for vocab ~1500 is small. Is 8-dimensional per-ticker representation enough to learn ticker-level idiosyncrasy? Vs 16 or 32 — at what point does overfit dominate sample efficiency?
2. `HIDDEN_1=64, HIDDEN_2=32` — the bottleneck-decreasing layout encourages residual capacity. Should we try equal layers (`64-64`)?
3. `DELTA_LOGIT_CLAMP=2.5` — this hard-clamps the calibrator's adjustment. At raw_prob=0.5, Δ=±2.5 → final_prob ∈ [0.076, 0.924]. At raw_prob=0.95 (logit≈2.94), Δ=±2.5 → final_prob ∈ [sigmoid(0.44), sigmoid(5.44)] ≈ [0.61, 0.996]. So the clamp is asymmetric around the prior — bigger upward room when prior is low, bigger downward room when prior is high. Defensible? Or should the clamp be on `final_prob` directly instead?
4. AdamW vs SGD with momentum — AdamW handles the small-data residual case better in our experience but worth checking on this dataset.
5. Cosine LR decay — 80% of training for the decay phase. On 30 epochs that's 24 epochs decaying. Is this too slow? Step decay alternative?
6. Early stopping on cal_brier_weighted — but per-cell weighting at cal time uses Phase 2 `train_positive_rate`, not `cal_positive_rate`. That introduces a bias toward training-cell-distribution. Should cal weighting use cal_positive_rate or train_positive_rate? (Spec is silent.)
7. Does the architecture need explicit handling of `is_unk_ticker=1` rows during training? At training time always 0, so the model never sees the case. At inference, Phase 5's σ-inflation handles it. Phase 4 just initializes the UNK embedding row randomly per-member; no other change. Confirm.
