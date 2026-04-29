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
    bundle_path = train_dir / 'extract_bundle.json'
    bundle = json.load(open(bundle_path))
    if bundle.get('schema_version') != 2:
        raise Phase4SchemaError(
            f"extract_bundle schema_version={bundle.get('schema_version')}, expected 2"
        )
    # Resolve audit + verify
    audit_path = train_dir / bundle['audit_path']
    if compute_sha256(audit_path) != bundle['audit_sha256']:
        raise Phase4SchemaError(f"audit_sha256 mismatch")
    audit = json.load(open(audit_path))
    # ... read normstats, vocab, parquets, audit while holding LOCK_SH
```

**R-p2-spec-r5#R1#C11 + R1-train#C12 (reader contract):** Phase 4 holds `LOCK_SH` on `data/cal_mlp/<asset>/.extract.lock` from before opening `extract_bundle.json` until ALL parquet/normstats/vocab/AUDIT reads are complete and loaded into memory.

## Lock-ordering invariant (R1-ops#C1)

Phase 4 MUST acquire locks in this order: `extract_lock` (SHARED on `data/cal_mlp/<asset>/.extract.lock`) → `models_lock` (EXCLUSIVE on `models/cal_mlp_<asset>/.lock`). Both held until end of run. No code path may take models_lock first; future writers MUST honor this.

## Cross-phase migration: lock-path move (R2#C3)

R1-ops#C9 moves the model lock from `models/.cal_mlp_<asset>.lock` (sibling of models/) to `models/cal_mlp_<asset>/.lock` (inside per-asset dir). Phase 6's already-rebuilt `validate.py:367` currently reads the OLD path:

```python
lock_path = models_dir / f".cal_mlp_{args.asset}.lock"
```

**Required in the SAME commit as Phase 4 cutover:**
1. Update `validate.py:367` to read `models_dir / f"cal_mlp_{args.asset}" / ".lock"`.
2. Add a CI grep test: `grep -r "\.cal_mlp_.*\.lock" scripts/cal_mlp/` returns no matches outside the per-asset directory pattern.
3. Block Phase 4 deploy until validate.py is patched (parity-assert at startup verifies the lock file is at the new location).

If split across commits, Phase 4 (writer) and legacy Phase 6 (reader) hold non-overlapping locks → no mutual exclusion → readers observe partially-renamed train_dirs.

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

    # Pre-allocate ensemble prediction arrays (R1-ops#C12)
    cal_preds = np.zeros((args.ensemble_size, len(ca)), dtype=np.float32)
    test_preds = np.zeros((args.ensemble_size, len(te)), dtype=np.float32)

    # Train M ensemble members
    for member in range(args.ensemble_size):
        # R1-train#C2: locked seed formula, used identically by Phase 3 (UNK init).
        # Spec asserts BASE_SEED >= 1 to prevent collision with member offset.
        assert args.base_seed >= 1, "BASE_SEED must be >= 1"
        member_seed = args.base_seed * 1000 + member
        torch.manual_seed(member_seed)
        np.random.seed(member_seed)
        random.seed(member_seed)

        model = build_model_from_definition(MODEL_DEF, n_vocab=n_vocab,
                                             unk_init_seed=member_seed)
        opt = AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)
        scheduler = build_warmup_then_cosine(opt, warmup_steps=50,
                                              total_steps=len(tr) * 30 // 256,
                                              eta_min=1e-4)

        best_cal_brier_w = float('inf')
        epochs_since_improve = 0
        best_state_dict = None

        for epoch in range(30):
            model.train()
            # R1-train#C3: lock num_workers=0 (deterministic, removes worker_init_fn ambiguity).
            for batch in DataLoader(tr, batch_size=256, shuffle=True, num_workers=0,
                                     generator=torch.Generator().manual_seed(member_seed)):
                # R1-train#C1: forward contract:
                #   model(x_cont, x_missing, price_tier, stc_bucket, vol_regime_int,
                #         side_int, ticker_id, logit_raw_prob_clipped) -> (final_logit, final_prob)
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

        # Save best member checkpoint AS TMP (R1-ops#C2: rename batch happens later).
        # R2#C1: marker is also a tmp; renamed in step 7 BEFORE the checkpoint
        # so the invariant "checkpoint final ⇒ marker final" holds.
        member_final = train_dir_phase4 / f"fold{fold}_member{member}.pt"
        member_tmp = member_final.with_suffix(
            f".pt.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        )
        with open(member_tmp, 'wb') as f:
            torch.save(best_state_dict, f)
            f.flush()
            os.fsync(f.fileno())
        # R1-train#C5: per-checkpoint resume marker.
        marker_payload = {
            'cfg_fp': cfg_fp, 'extract_bundle_logical_sha256': extract_bundle_logical_sha256,
            'base_seed': args.base_seed, 'model_definition_sha256': MODEL_DEF_SHA,
            'train_id': train_id, 'fold': fold, 'member': member,
            'best_cal_brier_w': best_cal_brier_w,
            'early_stop_epoch': epoch + 1,
        }
        marker_final = train_dir_phase4 / f"fold{fold}_member{member}.marker.json"
        marker_tmp = write_json_tmp(marker_payload, marker_final)  # writes .tmp-..., NO rename
        # Marker renamed BEFORE checkpoint (step 7 ordering); resume invariant holds.
        pending_renames.append((marker_tmp, marker_final))
        pending_renames.append((member_tmp, member_final))

        # Compute per-row predictions on cal + test for ensembling
        model.load_state_dict(best_state_dict)
        model.eval()
        with torch.no_grad():
            cal_preds[member] = predict_p(model, ca)
            test_preds[member] = predict_p(model, te)

    # Ensemble aggregate — R1-train#C10: compute std in float64, store float32.
    cal_preds_f64 = cal_preds.astype(np.float64)
    test_preds_f64 = test_preds.astype(np.float64)
    cal_p_mean = cal_preds_f64.mean(axis=0).astype(np.float32)
    cal_p_std  = cal_preds_f64.std(axis=0, ddof=0).astype(np.float32)
    test_p_mean = test_preds_f64.mean(axis=0).astype(np.float32)
    test_p_std  = test_preds_f64.std(axis=0, ddof=0).astype(np.float32)
    assert (cal_p_std >= 0).all() and (test_p_std >= 0).all()

    # Persist fold predictions (Phase 5 reads cal predictions for conformal calibration;
    # Phase 6 reads test predictions for ship-blocker evaluation)
    save_fold_predictions(fold, cal_p_mean, cal_p_std, test_p_mean, test_p_std,
                           ca['outcome'], te['outcome'], ca['ticker'], te['ticker'], ...)
```

## Output structure (R1-ops#C9 — restructured for symmetry with Phase 2)

```
models/
└── cal_mlp_<asset>/
    ├── CURRENT                                       # text: latest train_id
    ├── .lock                                         # models_lock per asset
    └── <train_id>/
        ├── cal_mlp_<asset>_<train_id>_bundle.json    # the manifest (Phase 5 reads)
        ├── model_definition.json                      # architecture spec for Phase 7
        ├── fold0_member0.pt, ..., fold0_member4.pt    # per-member best-state checkpoints
        ├── fold0_member0.marker.json, ...             # resume markers (R1-train#C5)
        ├── fold1_member0.pt, ..., fold1_member4.pt
        ├── fold1_member0.marker.json, ...
        ├── fold2_member0.pt, ..., fold2_member4.pt
        ├── fold2_member0.marker.json, ...
        ├── fold0_predictions.parquet                  # cal + test predictions, per-member + ensemble
        ├── fold1_predictions.parquet
        ├── fold2_predictions.parquet
        └── train_audit.json                           # diagnostic counters
```

## Bundle JSON

```json
{
  "phase": 4,
  "schema_version": 2,
  "asset": "SOL",
  "train_id": "2026-04-27T00:00:00.000000Z-abcd1234",   // SAME train_id as Phase 2
  "cfg_fp": "f3b201e8a7c1d0e9",                          // SAME cfg_fp as Phase 2
  "model_definition_path": "model_definition.json",      // basename
  "model_definition_sha256": "...",
  "ensemble_size": 5,
  "base_seed": 42,

  // R1-train#C7: pin to logical content, not parquet bytes.
  "extract_bundle_path": "data/cal_mlp/SOL/<extract_train_id>/extract_bundle.json",  // relative to project_root
  "extract_bundle_sha256": "...",                        // bit-level (informational)
  "extract_bundle_logical_sha256": "...",                // sha over canonical normstats + per-cell counts + n_train/cal/test
  "ticker_vocab_path": "ticker_vocab.json",              // basename, resolved against extract bundle's parent
  "ticker_vocab_sha256": "...",

  // R1-train#C4: explicit deploy fold (= K-1 by Phase 3 lock).
  "deploy_fold_idx": 2,

  "_path_resolution": "basenames_relative_to_bundle_dir; cross-bundle refs (extract_bundle_path) relative to project_root",

  "eval_fold_artifacts": [
    {
      "fold": 0,
      "n_train": 8421, "n_cal": 2103, "n_test": 2087,
      "test_window_start": "...", "test_window_end": "...",
      "predictions_path": "fold0_predictions.parquet",   // basename
      "predictions_sha256": "...",
      "members": [
        {"member": 0, "seed": 42000, "checkpoint_path": "fold0_member0.pt",
         "checkpoint_sha256": "...", "marker_path": "fold0_member0.marker.json",
         "marker_sha256": "...", "best_cal_brier_w": 0.0612,
         "early_stop_epoch": 18},
        ...
      ]
    },
    ...
  ],
  "model_identity_sha256": "...",                        // hash of all member checkpoints concatenated
  "normstats_concat_sha256": "...",                      // hash of all per-fold normstats concatenated
  "bundle_sha": "...",                                   // sha256(model_id:normstats_concat:phase4)
  "generated_at": "...",
  "train_py_sha256": "...",
  "torch_version": "...",
  "numpy_version": "...",
  "pandas_version": "...",
  "pyarrow_version": "..."
}
```

**R1-ops#C10 path resolution:** Phase 4's bundle uses BASENAMES (relative to bundle's directory) for in-train_id artifacts and `project_root`-relative paths for cross-bundle references (Phase 2 extract bundle). Phase 5/6 readers resolve via `Path(__file__).resolve().parents[2]` (already applied in validate.py per R-p2-impl-r3#C2). Worktree-move robust: re-running from a relocated checkout works without bundle rewriting.

**R1-train#C7 logical sha — locked computation (R2#C2):** `extract_bundle_logical_sha256` is computed by Phase 4 at bundle-load time (under `LOCK_SH` on extract_lock). Phase 2 does NOT emit it. The function is locked here so Phase 5/7 can recompute byte-identically:

```python
def compute_extract_logical_sha(audit_json: dict, normstats_per_fold: list[dict]) -> str:
    """Stable across pyarrow upgrades. Operates on logical content only:
    sorted normstats values per fold + per_cell counts + n_train/cal/test.

    R3#C1: Phase 2's normstats schema puts `transforms` at top level (a dict
    {col: transform_name}), NOT inside individual stat dicts. Read from
    ns['transforms'][col], not ns['stats'][col].
    """
    canonical = {
        'normstats': [
            {col: {'mean': stats['mean'], 'std': stats['std'],
                    'transform': ns.get('transforms', {}).get(col, 'identity'),
                    '_no_zscore': stats.get('_no_zscore', False)}
             for col, stats in sorted(ns['stats'].items())}
            for ns in normstats_per_fold
        ],
        'per_fold': [
            {'fold': pf['fold'],
             'n_train': pf['n_train'], 'n_cal': pf['n_cal'], 'n_test': pf['n_test'],
             'per_cell': {k: {'n_train': v['n_train'], 'n_cal': v['n_cal'], 'n_test': v['n_test'],
                              'train_positive_rate': v['train_positive_rate'],
                              'train_mean_method_output': v['train_mean_method_output']}
                          for k, v in sorted(pf['per_cell'].items())}}
            for pf in audit_json['per_fold']
        ],
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest()
```

Phase 5/7 import this function from `cal_mlp/_helpers.py` and recompute. Mismatch → `Phase{5,7}SchemaError`.

**R1-train#C6 Phase 7 verification contract:** at boot, bot.py MUST recompute and verify (a) `phase4_bundle_sha = sha256(model_identity:normstats_concat:phase4)`, (b) `phase5_bundle_sha = sha256(phase4_bundle_sha:conformal_sha)`. Both assertions are hard ship-blockers (refuse to start; alert).

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

1. mkdir `models/cal_mlp_<asset>/<train_id>/`
2. Acquire SHARED on `data/cal_mlp/<asset>/.extract.lock`, then EXCLUSIVE on `models/cal_mlp_<asset>/.lock` (lock-ordering invariant per above).
3. **Stale-tmp cleanup (R1-ops#C3):** scope = `train_dir_phase4.glob('*.tmp-*')` (current train_id only). Orphan train_id sibling dirs are NOT auto-cleaned (operator concern; harmless because CURRENT doesn't point to them).
4. For each fold k for each member m: training-loop writes `fold{k}_member{m}.pt.tmp-...` with `torch.save` + fsync, plus a sibling `fold{k}_member{m}.marker.json.tmp-...`. All accumulated in `pending_renames` list — none renamed yet.
5. After all folds × members complete: build per-fold predictions parquet tmps via `atomic_write_parquet`; append to `pending_renames`.
6. Build `model_definition.json`, `train_audit.json`, `bundle.json` tmps; append.
7. **Rename phase** — order: per-fold MARKERS → per-fold member checkpoints → per-fold predictions → model_definition → audit → bundle (LAST). **R3#C2:** marker-before-checkpoint preserves the invariant `checkpoint_final ⇒ marker_final` — a crash between rename ops can leave an orphan marker (harmless; resume deletes it) but never an orphan checkpoint.
8. Update `models/cal_mlp_<asset>/CURRENT` pointer atomically via tmp+rename. **Reader contract (R1-ops#C11):** Phase 5/6 read CURRENT AFTER acquiring models_lock SH, so they see a stable train_id throughout their run.
9. fsync directory after each rename batch.

**On failure** during steps 4-7 (training crash, OOM, wall-clock kill, rename error):
- Outer except catches `Phase4Error` or `Phase4ResourceError`.
- Cleanup unlinks ALL `pending_renames` tmps + ANY already-renamed final paths in REVERSE order.
- ENOENT swallowed silently (file already gone).
- Bundle is never written on failure → gate intact.
- Re-raise as Phase4WriteError (or pass through Phase4ResourceError) with exit code per hierarchy.

Without `--allow-resume`, prior-run final checkpoints (renamed but bundle missing) are OVERWRITTEN by the new training pass via marker mismatch (R1-train#C5 contract). Concurrent readers are blocked by LOCK_EX.

## Resume semantics (`--allow-resume`) — REWRITTEN per R1-train#C5

The unsafe "checkpoint exists" fallback is replaced with marker-based verification.

**Marker file** (written sibling to each checkpoint at `torch.save` time):

```json
{
  "cfg_fp": "...",
  "extract_bundle_logical_sha256": "...",
  "base_seed": 42,
  "model_definition_sha256": "...",
  "train_id": "...",
  "fold": 0,
  "member": 0,
  "best_cal_brier_w": 0.0612,
  "early_stop_epoch": 18
}
```

**Resume logic:** for each `(fold, member)` pair:
- If `fold{k}_member{m}.pt` AND `fold{k}_member{m}.marker.json` both exist
- AND every marker field matches the current run's values (cfg_fp, logical sha, base_seed, model_def_sha, train_id, fold, member)
- THEN skip training; load the checkpoint as-is.
- ELSE delete the stale checkpoint + marker; train this member from scratch.

This is the RESUME CONTRACT regardless of whether `--allow-resume` is set. Without `--allow-resume`, the spec's earlier `clean stale tmps + retrain from scratch` policy is REPLACED by per-member resume — so the "from scratch" wording is misleading.

Production cron may safely pass `--allow-resume` because the marker check guards against silent corruption. Documented as production-safe.

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

## Memory / runtime budgets (R1-ops#C5/C6)

| Budget | Target | Hard ceiling |
|---|---|---|
| Peak RSS per asset | 1 GB (per Phase 3) | 1.5 GB → Phase4ResourceError exit 7 |
| Wall time per fold per member (CPU) | ≤ 90 s | 5 min → Phase4ResourceError |
| Wall time per asset (3 folds × 5 members) | ≤ 25 min | 60 min → Phase4ResourceError |

**`psutil` check:** Module load wraps `try: import psutil; _HAS_PSUTIL=True except: _HAS_PSUTIL=False`. First line of `main()` (after args parse + banner) raises `Phase4ContractError` if `not _HAS_PSUTIL`. Phase 4 enforces because OOM during training is unrecoverable; Phase 6 only reads and is allowed to soft-flag.

**Wall-clock mechanism:** `signal.alarm(WALL_CEILING_S)` registered at start of `main()` after lock acquisition; SIGALRM handler raises `Phase4ResourceError(exit_code=7)`. Belt-and-suspenders: per-batch `if time.monotonic() - start > WALL_CEILING_S: raise` (signal.alarm can be masked by torch C++ code). POSIX-only; documented.

**Resource breach handler (R1-train#C8):**
1. Raise `Phase4ResourceError` from training thread.
2. Outer handler catches: deletes ALL `*.tmp-*` files under `train_dir_phase4/`; deletes any final-path checkpoint whose marker file is missing or stale.
3. Does NOT touch other folds' members (different fold's checkpoints stay).
4. Bundle is never written on resource error (gate intact).
5. Exits with code 7. Resume-after-resource-error is supported and safe via marker check.

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

## Phase4Dataset + forward batch protocol (R3#C3 — locked)

`FORWARD_KEYS` is a module-level tuple in `cal_mlp/_helpers.py`:

```python
FORWARD_KEYS = (
    'x_cont', 'x_missing',
    'price_tier', 'stc_bucket', 'vol_regime_int', 'side_int',
    'ticker_id', 'logit_raw_prob_clipped',
)
```

```python
class Phase4Dataset(torch.utils.data.Dataset):
    """Wraps a normalized fold DataFrame + ticker vocab + per-cell weights.
    __getitem__ returns a dict whose keys exactly match FORWARD_KEYS plus
    'outcome' and 'w_cell'. Default torch collate stacks each key into a
    batched tensor."""

    def __init__(self, df: pd.DataFrame, vocab: dict, w_cell_lookup: np.ndarray):
        self.df = df.reset_index(drop=True)
        self.vocab = vocab
        self.w_cell_lookup = torch.from_numpy(w_cell_lookup.astype(np.float32))
        self._cont_arr = self.df[CONT_FEATURE_COLS].to_numpy(np.float32)
        self._missing_arr = self.df[MISSING_INDICATOR_COLS].to_numpy(np.float32)
        self._price = self.df['price_tier'].to_numpy(np.int64)
        self._stc   = self.df['stc_bucket'].to_numpy(np.int64)
        self._vol   = self.df['vol_regime_int'].to_numpy(np.int64)
        self._side  = self.df['side_int'].to_numpy(np.int64)
        self._tid   = self.df['ticker_id'].to_numpy(np.int64)
        self._logit_raw = self.df['logit_raw_prob_clipped'].to_numpy(np.float32)
        self._outcome = self.df['outcome'].to_numpy(np.float32)

    def __len__(self): return len(self.df)

    def __getitem__(self, i: int) -> dict:
        return {
            'x_cont': torch.from_numpy(self._cont_arr[i]),
            'x_missing': torch.from_numpy(self._missing_arr[i]),
            'price_tier': torch.tensor(self._price[i], dtype=torch.long),
            'stc_bucket': torch.tensor(self._stc[i], dtype=torch.long),
            'vol_regime_int': torch.tensor(self._vol[i], dtype=torch.long),
            'side_int': torch.tensor(self._side[i], dtype=torch.long),
            'ticker_id': torch.tensor(self._tid[i], dtype=torch.long),
            'logit_raw_prob_clipped': torch.tensor(self._logit_raw[i], dtype=torch.float32),
            'outcome': torch.tensor(self._outcome[i], dtype=torch.float32),
            'w_cell': self.w_cell_lookup[self._price[i] * 4 + self._stc[i]],
        }
```

`compute_weighted_bce(model, batch_dict, w_cell_lookup) → loss`:

```python
def compute_weighted_bce(model: nn.Module, batch: dict, _unused_w_lookup) -> torch.Tensor:
    """Forward + weighted BCE. The dataset already attached per-row w_cell."""
    final_logit, _final_prob = model(**{k: batch[k] for k in FORWARD_KEYS})
    bce = F.binary_cross_entropy_with_logits(final_logit, batch['outcome'], reduction='none')
    return (bce * batch['w_cell']).mean()
```

DataLoader call site:

```python
ds = Phase4Dataset(tr, vocab, w_cell_lookup)
loader = DataLoader(ds, batch_size=256, shuffle=True, num_workers=0,
                    generator=torch.Generator().manual_seed(member_seed))
for batch in loader:
    loss = compute_weighted_bce(model, batch, w_cell_lookup)
    ...
```

Phase 7's bot.py forward path uses the SAME `FORWARD_KEYS` ordering — drift = startup parity-assert fail.

## train_audit.json schema (R1-train#C11 — locked v1)

```json
{
  "schema_version": 1,
  "train_id": "...",
  "asset": "SOL",
  "wall_time_total_s": 1247.3,
  "peak_rss_mb": 873.2,
  "rss_samples": [{"step": 100, "rss_mb": 412.5}, ...],
  "torch_version": "...",
  "numpy_version": "...",
  "pandas_version": "...",
  "pyarrow_version": "...",
  "folds": [
    {
      "fold": 0,
      "wall_time_s": 380.5,
      "members": [
        {
          "member": 0,
          "seed": 42000,
          "early_stop_epoch": 18,
          "epochs_trained": 23,
          "best_cal_brier_raw": 0.0589,
          "best_cal_brier_weighted": 0.0612,
          "final_train_loss": 0.234,
          "resumed_from_marker": false
        }, ...
      ],
      "ensemble_std_distribution": {"p10": 0.012, "p50": 0.035, "p90": 0.067, "max": 0.142},
      "n_zero_std_rows": 3,
      "n_zero_std_pct": 0.0014
    }, ...
  ]
}
```

Phase 6 reads `ensemble_std_distribution` and `n_zero_std_rows` for ship-blocker checks (collapse detection: all members converge to same minimum → std artificially low).

## Concurrency model (R1-ops#C7 + R1-train#C9)

Each asset trains in a SEPARATE OS process (`subprocess.Popen` from a top-level cron driver), never in shared-process threads. Determinism env vars (`CUBLAS_WORKSPACE_CONFIG`) are set per-process at import time before `import torch`.

Aggregate memory: 4 assets × 1 GB target = 4 GB; 4 × 1.5 GB ceiling = 6 GB. VPS must have ≥ 8 GB to run all 4 in parallel; otherwise, serialize via a wrapping `flock(/tmp/cal_mlp_train.global.lock)` in the cron entrypoint.

Cron driver itself is out of scope for this spec (a small wrapper script in scripts/cron/).

## Runtime version pinning (R1-ops#C8)

`requirements_calmlp.txt` pins torch, numpy, pandas, pyarrow. Phase 4 records runtime versions in bundle JSON AND audit JSON. Phase 4 startup compares `torch.__version__` etc. against `requirements_calmlp.txt`; mismatch raises `Phase4ContractError`. Phase 5/6/7 mirror the check.

This catches `pip auto-upgrade` regressions that would silently change `model_identity_sha256`.

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
