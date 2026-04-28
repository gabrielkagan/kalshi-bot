# Phase 4: Training (train.py)

**Status:** Round 1 spec.
**Anchor:** `kb-research/bot/p2-phases-4-to-8-design.md`.
**Prerequisite specs:** Phase 2 (data, converged R5), Phase 3 (architecture, R1).
**Output:** `models/cal_mlp_<asset>_<train_id>_bundle.json` + per-member checkpoints + per-fold cal/test predictions parquet.

## Goal

For each asset, for each fold (default K=3), for each ensemble member (M=5):
- Load Phase 2 parquet + normstats + ticker vocab via the bundle's CURRENT pointer
- Apply normalization (`apply_norm` from `cal_mlp/normalize.py`)
- Construct the architecture per Phase 3
- Train with the locked optimizer + loss + schedule
- Emit per-member predictions on cal + test splits
- Aggregate ensemble (mean + std with ddof=0)
- Atomically write the model bundle that Phase 5 consumes

## Inputs (CLI)

```
train.py --asset {BTC,ETH,SOL,XRP}
         [--extract-train-id <hex>]      # default: read CURRENT
         [--folds-to-train 0,1,2]        # default: all folds
         [--ensemble-size 5]             # M=5 locked; flag for ablation only
         [--base-seed 42]                # deterministic seed root
         [--device cpu|cuda]             # default: cpu
         [--n-train-min 2000]            # mirrored from Phase 2
         [--quiet|--verbose]
         [--allow-resume]                # if a prior partial run exists, resume per-member
```

## Bundle discovery (CURRENT pointer)

```python
project_root = Path(__file__).resolve().parents[2]
extract_dir = project_root / 'data' / 'cal_mlp' / asset
if args.extract_train_id:
    train_dir = extract_dir / args.extract_train_id
else:
    current_path = extract_dir / 'CURRENT'
    if not current_path.exists():
        raise Phase4Error(f"CURRENT pointer absent at {current_path} — run extract_data.py first")
    train_id = current_path.read_text().strip()
    train_dir = extract_dir / train_id

with acquire_shared_lock(extract_dir / '.extract.lock'):
    bundle = json.load(open(train_dir / 'extract_bundle.json'))
    assert bundle['schema_version'] == 2  # phase-local SchemaError if mismatch
    # ... read normstats, vocab, parquets while holding LOCK_SH
```

**R-p2-spec-r5#R1#C11 (reader contract):** Phase 4 holds `LOCK_SH` on `data/cal_mlp/<asset>/.extract.lock` from before opening `extract_bundle.json` until ALL parquet/normstats/vocab reads are complete and loaded into memory.

## Per-fold per-member training loop

```
for fold in args.folds_to_train:
    fold_artifact = next(a for a in bundle['eval_fold_artifacts'] if a['fold'] == fold)
    fold_df = pq.read_table(train_dir / fold_artifact['parquet_path']).to_pandas()
    normstats = json.load(open(train_dir / fold_artifact['normstats_path']))
    vocab = json.load(open(train_dir / bundle['ticker_vocab_path']))['vocab']
    n_vocab = len(vocab)

    # Apply normalization (transform → impute → z-score) per Phase 2 lock
    fold_df_norm = apply_norm(fold_df, normstats, CONT_FEATURE_COLS)

    # Per-cell weights (loss form locked in Phase 2)
    per_cell = read_per_cell(audit_json, fold)
    w_cell_lookup = build_w_cell(per_cell)   # 16-cell lookup, w_cell = 1 + 4×|p_cell - prior_cell|

    # Split into train/cal/test
    tr = fold_df_norm[fold_df_norm['split'] == 'train']
    ca = fold_df_norm[fold_df_norm['split'] == 'cal']
    te = fold_df_norm[fold_df_norm['split'] == 'test']

    # Train M ensemble members
    for member in range(args.ensemble_size):
        member_seed = args.base_seed * 1000 + member
        torch.manual_seed(member_seed)
        np.random.seed(member_seed)
        random.seed(member_seed)

        model = build_model_from_definition(MODEL_DEF, n_vocab=n_vocab,
                                             unk_init_seed=member_seed)
        opt = AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)
        scheduler = build_warmup_then_cosine(opt, warmup_steps=200,
                                              total_steps=len(tr) * 30 // 256,
                                              eta_min=1e-4)

        best_cal_brier_w = float('inf')
        epochs_since_improve = 0
        best_state_dict = None

        for epoch in range(30):
            model.train()
            for batch in DataLoader(tr, batch_size=256, shuffle=True,
                                     worker_init_fn=lambda i: np.random.seed(member_seed + i)):
                loss = compute_weighted_bce(model, batch, w_cell_lookup)
                opt.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
                opt.step()
                scheduler.step()

            # Cal-side weighted Brier for early stopping
            cal_brier_w = compute_cal_brier_weighted(model, ca, w_cell_lookup)
            if cal_brier_w < best_cal_brier_w - 1e-5:
                best_cal_brier_w = cal_brier_w
                best_state_dict = {k: v.cpu().clone() for k, v in model.state_dict().items()}
                epochs_since_improve = 0
            else:
                epochs_since_improve += 1
                if epochs_since_improve >= 5:
                    break  # early stop

        # Save best member checkpoint
        member_path = train_dir_phase4 / f"fold{fold}_member{member}.pt"
        torch.save(best_state_dict, member_path)

        # Compute per-row predictions on cal + test for ensembling
        model.load_state_dict(best_state_dict)
        model.eval()
        with torch.no_grad():
            cal_preds[member] = predict_p(model, ca)
            test_preds[member] = predict_p(model, te)

    # Ensemble aggregate
    cal_p_mean = cal_preds.mean(axis=0)        # [N_cal]
    cal_p_std  = cal_preds.std(axis=0, ddof=0) # ddof=0: members ARE the population
    test_p_mean = test_preds.mean(axis=0)
    test_p_std  = test_preds.std(axis=0, ddof=0)

    # Persist fold predictions (Phase 5 reads cal predictions for conformal calibration;
    # Phase 6 reads test predictions for ship-blocker evaluation)
    save_fold_predictions(fold, cal_p_mean, cal_p_std, test_p_mean, test_p_std,
                           ca['outcome'], te['outcome'], ca['ticker'], te['ticker'], ...)
```

## Output structure

```
models/
├── CURRENT_<asset>                                # text: latest train_id
└── cal_mlp_<asset>_<train_id>/
    ├── cal_mlp_<asset>_<train_id>_bundle.json     # the manifest (Phase 5 reads)
    ├── model_definition.json                       # architecture spec for Phase 7
    ├── fold0_member0.pt, ..., fold0_member4.pt     # per-member best-state checkpoints
    ├── fold1_member0.pt, ..., fold1_member4.pt
    ├── fold2_member0.pt, ..., fold2_member4.pt
    ├── fold0_predictions.parquet                   # cal + test predictions, per-member + ensemble
    ├── fold1_predictions.parquet
    ├── fold2_predictions.parquet
    └── train_audit.json                            # diagnostic counters, early-stop epoch per member
```

## Bundle JSON

```json
{
  "phase": 4,
  "schema_version": 2,
  "asset": "SOL",
  "train_id": "2026-04-27T00:00:00.000000Z-abcd1234",  // SAME train_id as Phase 2
  "cfg_fp": "f3b201e8a7c1d0e9",                         // SAME cfg_fp as Phase 2
  "model_definition_sha256": "...",                     // sha of model_definition.json
  "model_definition_path": "model_definition.json",
  "ensemble_size": 5,
  "base_seed": 42,
  "extract_bundle_sha256": "...",                       // pin to specific Phase 2 bundle
  "extract_bundle_path": "/abs/path/to/extract_bundle.json",  // absolute (R-p2-impl-r1#C2)
  "ticker_vocab_path": "/abs/path/to/ticker_vocab.json",
  "ticker_vocab_sha256": "...",
  "eval_fold_artifacts": [
    {
      "fold": 0,
      "n_train": 8421, "n_cal": 2103, "n_test": 2087,
      "test_window_start": "...", "test_window_end": "...",
      "parquet_path": "/abs/path/to/fold0.parquet",                  // ABSOLUTE (Phase 6 reads)
      "parquet_sha256": "...",
      "normstats_path": "/abs/path/to/normstats_fold0.json",
      "normstats_sha256": "...",
      "predictions_path": "/abs/path/to/fold0_predictions.parquet",
      "predictions_sha256": "...",
      "members": [
        {"member": 0, "seed": 42000, "checkpoint_path": ".../fold0_member0.pt",
         "checkpoint_sha256": "...", "best_cal_brier_w": 0.0612,
         "early_stop_epoch": 18},
        ...
      ]
    },
    ...
  ],
  "model_identity_sha256": "...",                       // hash of all member checkpoints concatenated
  "normstats_sha256": "...",                            // hash of all per-fold normstats concatenated
  "bundle_sha": "...",                                  // bundle_sha_v1 = sha256(model_id:normstats:NULL_at_phase4)
  "generated_at": "...",
  "train_py_sha256": "...",
  "torch_version": "...",
  "pandas_version": "..."
}
```

**R-p2-impl-r1#C2 path resolution:** Phase 4's bundle ABSOLUTIZES paths via `Path(...).resolve()` so Phase 6 (the eventual reader, possibly running from a different cwd) can open them without further resolution. Phase 2's bundle uses basenames + `_path_resolution` contract; Phase 4 reads Phase 2 by joining basenames against `bundle_path.parent`, then writes its own bundle with absolute paths.

## bundle_sha_v1 (canonical chain)

```python
# Phase 4 emits bundle_sha that Phase 5 will EXTEND with conformal_sha
bundle_sha_v1_inputs = (
    f"{model_identity_sha256}:{normstats_concat_sha256}:phase4"
)
bundle_sha = sha256(bundle_sha_v1_inputs.encode()).hexdigest()
```

Phase 5 wraps and re-emits with conformal_sha appended:
```python
phase5_bundle_sha = sha256(f"{phase4_bundle_sha}:{conformal_sha256}".encode()).hexdigest()
```

This chain lets Phase 6 verify the full provenance via a single `bundle_sha`.

## Atomic write protocol

Same as Phase 2:

1. mkdir `models/cal_mlp_<asset>_<train_id>/`
2. Acquire `models/.cal_mlp_<asset>.lock` with `LOCK_EX | LOCK_NB`. **Lock domain split (R-p2-spec-r1#R3-OPS#C4):** Phase 4's lock is the model lock; Phase 2's lock is the extract lock. Phase 4 takes BOTH for the duration of training: SHARED on extract_lock (so Phase 2 can't re-extract under us), EXCLUSIVE on models_lock.
3. Clean stale `*.tmp-*` from prior crashed runs.
4. For each fold k for each member m: write `fold{k}_member{m}.pt.tmp-...` with `torch.save` + fsync; later renamed.
5. Build per-fold predictions parquet via `atomic_write_parquet`.
6. Build `model_definition.json`, `train_audit.json`, `bundle.json`.
7. Rename order: per-fold member checkpoints → per-fold predictions → model_definition → audit → bundle (LAST).
8. Update `models/CURRENT_<asset>` pointer (atomic via tmp+rename).
9. fsync directory after each rename batch.

On failure: roll back already-renamed final paths in REVERSE; bundle is the gate.

## Resume semantics (`--allow-resume`)

If a prior partial run left checkpoints `fold0_member0.pt, fold0_member1.pt` but no bundle:
- With `--allow-resume`: skip per-member training where checkpoint exists AND `checkpoint_sha256` matches the bundle's expected sha (impossible if no prior bundle — so fall back to "checkpoint exists" only).
- Without `--allow-resume`: clean stale tmps + retrain from scratch.

Resume is a developer convenience; production cron does NOT pass `--allow-resume`.

## Determinism + reproducibility

| Source of nondeterminism | Mitigation |
|---|---|
| Python RNG | `random.seed(member_seed)` |
| numpy RNG | `np.random.seed(member_seed)` |
| torch RNG | `torch.manual_seed(member_seed)` |
| CUDA non-determinism | `torch.use_deterministic_algorithms(True); CUBLAS_WORKSPACE_CONFIG=:4096:8` |
| DataLoader worker | `worker_init_fn = lambda i: np.random.seed(member_seed + i)` |
| Cuda dropout | uses torch RNG; covered by manual_seed |
| Embedding init | per-member fixed seed → deterministic |

Two runs with identical args + identical Phase 2 train_id produce identical `model_identity_sha256` (assuming no torch / numpy / pandas version drift). Train_id is derived from Phase 2 train_id + `(base_seed, ensemble_size)` — same inputs → same train_id.

## Memory / runtime budgets

| Budget | Target | Hard ceiling |
|---|---|---|
| Peak RSS per asset | 4 GB | 6 GB → SystemExit |
| Wall time per fold per member (CPU) | ≤ 90 s | 5 min → SystemExit |
| Wall time per asset (3 folds × 5 members) | ≤ 25 min | 60 min → SystemExit |

`psutil` REQUIRED. Missing → SystemExit (Phase 4 ContractError, exit code 3).

## Failure modes (locked exit codes)

```python
class Phase4Error(RuntimeError): exit_code = 1
class Phase4DBError(Phase4Error): exit_code = 2          # n/a but reserved
class Phase4ContractError(Phase4Error): exit_code = 3
class Phase4LockError(Phase4Error): exit_code = 4
class Phase4WriteError(Phase4Error): exit_code = 5
class Phase4SchemaError(Phase4Error): exit_code = 6
class Phase4ResourceError(Phase4Error): exit_code = 7    # OOM / wall-clock
```

## What this phase does NOT do

- No conformal calibration (Phase 5 owns).
- No ship-blocker evaluation (Phase 6 owns).
- No bot.py amendments (Phase 7 owns).
- Does not retrain bot.py's existing CalEngine — that's a separate stack; this calibrator wraps `raw_prob` AFTER the existing CalEngine output.

## Open questions for adversarial review

1. `--ensemble-size 5` — is M=5 enough? With small per-fold n_train (~8k for SOL), M=5 may be too few for stable std. Spec defaults to 5 per Phase 4-design lock; verify.
2. Per-fold per-member training is independent — should we parallelize across members on multi-core? Or keep serial for determinism + memory bounding?
3. Resume semantics — should we hash the (post-fix) Phase 2 train_id into the model identity to detect Phase 2 re-extracts that invalidate prior model checkpoints?
4. Early stopping on cal_brier_weighted — but the cal weights use Phase 2's `train_positive_rate`, not cal's. Is this right? (Phase 3 had this open question; Phase 4 carries it.)
5. AdamW ε? Default 1e-8. Specify in spec.
6. Should the bundle include the Phase 2 audit JSON's per_cell stats verbatim (so Phase 6 doesn't have to re-read Phase 2's audit)? Or is the cross-bundle reference sufficient?
7. Embedding init UNK row uses `rng.normal(0, EMB_DIM ** -0.5, size=EMB_DIM)` per-member. Should we also INIT the embedding for all OTHER rows per-member? Currently only UNK is per-member; the rest use torch's default Xavier/uniform via `nn.Embedding`. With M=5 and 1500 vocab × 8 dims = 12k embedding params, the per-asset variance contribution to ensemble std is dominated by these. Verify it's acceptable.
