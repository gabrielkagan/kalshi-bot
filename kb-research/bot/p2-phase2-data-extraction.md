# Phase 2: Data Extraction (extract_data.py)

**Status:** Round 2 (R1 closed 40 critiques across 3 reviewers).
**Anchor:** `kb-research/bot/p2-phases-4-to-8-design.md`.
**Output:** per-asset, per-fold parquet files + normstats + audit + bundle metadata for Phase 3/4 training and Phase 5/6 calibration/validation.

## Goal

Pull rows from `evaluated_opportunities`, build train/cal/test splits using K-fold walk-forward (3 folds × 30-day non-overlapping test windows), bin into categorical buckets, compute the MLP target and feature vector, write atomically with `train_id`-namespaced output paths.

## Inputs

- `state.db` (read-only via `mode=ro`)
- CLI flags:
  - `--asset {BTC,ETH,SOL,XRP}` (required)
  - `--folds 3` (locked; > 3 needs spec amendment)
  - `--cal-days 15`, `--test-days 15`, `--train-days 60`
  - `--fold-offset-days 30` (test slices march forward 30d/fold; non-overlapping)
  - `--out-dir data/cal_mlp/<asset>` (default)
  - `--cutoff-end <ISO>` (default = `now - 24h`, normalized to `%Y-%m-%dT%H:%M:%S.%fZ`)
  - `--include-sub-floor` (default OFF; when ON, drops the per-asset floor filter for OOD coverage)
  - `--quiet` (only structured JSON to stdout; suppresses stderr below WARN)

## Source query (single connection)

All reads use **one** sqlite3 connection opened with `f'file:{db_path}?mode=ro&cache=shared'`. SQLite WAL gives a consistent read snapshot at first query; we capture `PRAGMA data_version` at open and at close to fingerprint the snapshot.

Per-asset entry-floor filter is on by default; disable with `--include-sub-floor` (logged + recorded in audit).

```sql
-- Asset floor = bot.py:219-225 mirrored: BTC=88, ETH=90, SOL=86, XRP=92.
-- (Skip the floor when --include-sub-floor is set.)

SELECT ticker, evaluation_time, asset, side, strategy,
       market_price, seconds_to_close, vol_regime, z_score,
       yes_spread_cents, calibrated_prob, calibration_method,
       raw_prob, breakeven_wr, fee_adjusted_edge, kelly_f,
       is_weekend, hour_of_day_utc, day_of_week,
       market_result, settled_time, available_balance_cents,
       spot_momentum_60s_bps, spot_momentum_5m_bps,
       spot_realized_range_15m_bps,
       btc_spot_change_5m_bps, btc_realized_vol_15m,
       window_max_buf_pct, window_min_buf_pct, minutes_above_strike,
       spot_distance_to_strike_sigma, prob_breakeven_gap,
       spot_coinbase_kraken_gap_bps, kalshi_flow_depth_velocity,
       rowid
FROM evaluated_opportunities
WHERE asset = ?
  AND product_type = '15m'
  AND ticker NOT LIKE 'SPORTS-%'
  AND market_price IS NOT NULL
  AND market_price > 0
  AND market_price >= ?              -- per-asset floor (skipped if --include-sub-floor)
  AND raw_prob IS NOT NULL
  AND evaluation_time IS NOT NULL
  AND evaluation_time < ?            -- cutoff_end_evaluation
  AND market_result IN ('yes','all_yes','no','all_no')
  AND settled_time IS NOT NULL
  AND settled_time < ?               -- cutoff_end (settlement watermark)
ORDER BY evaluation_time, ticker, rowid
```

**R1#C1**: Settlement results include `'all_yes'` and `'all_no'` (bot.py:4351 `_OK_RESULTS`). Filtering to only `('yes','no')` silently drops fully-filled markets.

**R1#C3**: `cutoff_end` is a SETTLEMENT watermark. We compute `cutoff_end_evaluation = cutoff_end - 1h` to allow for batched-settlement lag (`_SETTLEMENT_BATCH_SIZE=50` in bot.py). Both watermarks are persisted in `bundle.json`.

**R1#C4**: Per-asset floor mirrored from bot.py:219-225, vendored into `cal_mlp/asset_floors.py` (single source of truth). Phase 7 startup parity-asserts.

**R1#C18 / R3#C5**: `rowid` tiebreaker for total order; `data_version` audited at open/close.

## Schema check at extract time (R3#C14)

Before the SELECT, run `PRAGMA table_info(evaluated_opportunities)`. Assert every column referenced exists. On mismatch:

```
SystemExit: Phase 2 schema mismatch — column <X> expected by extract but not in state.db.
Either bot.py removed it (update extract spec + bump cfg_fp) or backfill incomplete.
Run `sqlite3 state.db 'PRAGMA table_info(evaluated_opportunities)'` to inspect.
```

If a column exists but is 100% NULL on the train fold, emit a distinct error from the `>30% NULL` contract violation: `"column <X> exists but is 100% NULL — recently added without backfill"`.

## Bucketization

```python
PRICE_BIN_CUTOFFS = [80, 90, 96]   # → 4 tiers (0..3)
STC_BIN_CUTOFFS = [120, 300, 600]  # → 4 buckets (0..3)
price_tier = np.digitize(market_price, PRICE_BIN_CUTOFFS, right=True)
stc_bucket = np.digitize(seconds_to_close, STC_BIN_CUTOFFS, right=True)
vol_regime_int = (vol_regime == 'elevated').astype(int)
side_int = (side == 'yes').astype(int)
```

**R1#C5**: `right=True` puts boundary values in the LOWER bin. So:
- `market_price == 96` → tier 3 (≥96¢ — bleed cell upper edge)
- `seconds_to_close == 600` → bucket 2 (300-600s — bleed cell upper edge inclusive)
- `seconds_to_close == 300` → bucket 1 (120-300s)

**Bleed cell:** `(price_tier=3, stc_bucket=2)` ≡ `market_price ∈ [96, 100], seconds_to_close ∈ (300, 600]`. Locked in `cal_mlp/buckets.py`; Phase 5/6 import from there. Unit test: edge cases `(96, 300), (96, 600), (99, 300), (99, 600)` → membership matrix.

## Method output (the calibrator's input prior)

**R1#C7 / R2#C3 (residual parameterization):**

```python
method_output = raw_prob   # locked. NOT a COALESCE on calibrated_prob.
```

`calibrated_prob` is bot.py's `min(raw_prob, dynamic_cap)` post-cap value (bot.py:8453+). The calibrator predicts a residual on the raw prior; the cap is downstream sizing concern. Phase 7 deploys cleanly: bot.py computes `raw_prob`, MLP predicts `Δlogit`, bot writes `final_prob = sigmoid(logit(raw_prob) + Δlogit)`, then sizing applies the existing dynamic_cap.

**Architecture lock for Phase 3** (binding for this spec's parquet schema):

```
final_prob = sigmoid(logit(raw_prob) + Δ)
where Δ = MLP(features_excluding_raw_prob)   # the residual head
```

The MLP **does not see `raw_prob` as a continuous input** — including it would let the MLP collapse to `Δ ≈ 0` (predict the prior). Instead, `raw_prob` enters via the `logit(raw_prob)` skip term added to `Δ`. The parquet still stores `raw_prob` (as `method_output_raw`) for Phase 5/6 reference, but it's NOT in `CONT_FEATURE_COLS`.

## Outcome (target)

**R1#C1 / R1#C15:**

```python
result_yes = market_result IN ('yes', 'all_yes')
outcome = int(result_yes == (side == 'yes'))
```

Unit test: all 4 cells of `(side, market_result) ∈ {yes,no} × {yes,all_yes,no,all_no}` produce the correct outcome.

## Continuous feature set (CONT_FEATURE_COLS) — locked

**R1#C2 / R2#C4:** removed `fee_adjusted_edge` and `kelly_f` (linear-redundant with raw_prob and market_price).
**R1#C13:** hour-of-day → sin/cos cyclic; balance → log1p.
**R2#C5:** WS-fed features get `_missing` indicator.
**R2#C8:** bounded-support features get `logit` transform before z-score.

```python
# Continuous, z-score normalized after per-column transform.
CONT_FEATURE_COLS = [
    'breakeven_wr',                # logit transform → z-score
    'market_price',                # log1p(market_price/100) → z-score (cents → log-dollars)
    'seconds_to_close',            # identity → z-score
    'z_score',                     # identity → z-score
    'yes_spread_cents',            # identity → z-score
    'spot_momentum_60s_bps',       # identity → z-score
    'spot_momentum_5m_bps',        # identity → z-score
    'spot_realized_range_15m_bps', # log1p → z-score
    'btc_spot_change_5m_bps',      # identity → z-score
    'btc_realized_vol_15m',        # log1p → z-score
    'window_max_buf_pct',          # identity → z-score
    'window_min_buf_pct',          # identity → z-score
    'minutes_above_strike',        # identity → z-score
    'spot_distance_to_strike_sigma',           # identity → z-score
    'abs_spot_distance_to_strike_sigma',       # NEW (R2#C14): |spot_distance|, addresses SOL pocket
    'time_decayed_proximity',                  # NEW: spot_distance_to_strike_sigma × (seconds_to_close/900)
    'prob_breakeven_gap',          # identity → z-score
    'spot_coinbase_kraken_gap_bps',# identity → z-score
    'kalshi_flow_depth_velocity',  # identity → z-score
    'log_balance_dollars',         # = log1p(available_balance_cents/100); identity → z-score
    'hour_sin', 'hour_cos',        # = sin/cos(2π·hour_of_day_utc/24); identity → z-score
]

CONT_FEATURE_TRANSFORMS = {
    'breakeven_wr': 'logit',
    'market_price': 'log_cents_to_dollars',
    'spot_realized_range_15m_bps': 'log1p_signed',
    'btc_realized_vol_15m': 'log1p',
    # all others: 'identity'
}

# WS-fed columns get a parallel _missing int8 indicator.
MISSING_INDICATOR_COLS = [
    'spot_momentum_60s_bps_missing',
    'spot_momentum_5m_bps_missing',
    'spot_realized_range_15m_bps_missing',
    'btc_spot_change_5m_bps_missing',
    'btc_realized_vol_15m_missing',
    'spot_coinbase_kraken_gap_bps_missing',
    'kalshi_flow_depth_velocity_missing',
]
```

Categorical (one-hot in dataset, NOT normalized):
- `price_tier` (0-3)
- `stc_bucket` (0-3)
- `vol_regime_int` (0-1)
- `side_int` (0-1)
- `ticker_id` (asset-wide vocabulary, embedded by Phase 4)

`N_CONT = len(CONT_FEATURE_COLS)`. The architecture-lock skip term `logit(raw_prob)` is NOT in `CONT_FEATURE_COLS` (it bypasses normstats and is added directly in Phase 4's forward pass).

`cfg_fp = sha256(canonical_inputs)[:16]` (16 chars = 64 bits per R1#C14):

```python
canonical_inputs = json.dumps({
    'CONT_FEATURE_COLS': CONT_FEATURE_COLS,
    'CONT_FEATURE_TRANSFORMS': CONT_FEATURE_TRANSFORMS,
    'MISSING_INDICATOR_COLS': MISSING_INDICATOR_COLS,
    'PRICE_BIN_CUTOFFS': PRICE_BIN_CUTOFFS,
    'STC_BIN_CUTOFFS': STC_BIN_CUTOFFS,
    'digitize_right': True,
    'method_output_policy': 'raw_prob_only',
    'asset_floors': ASSET_FLOORS,
    'settlement_whitelist': ['yes','all_yes','no','all_no'],
    'null_drop_threshold': 0.30,
    'null_imputation_policy': 'fold_train_mean_with_missing_indicator',
    'normstats_ddof': 1,
}, sort_keys=True).encode()
```

## NULL handling

Per-feature policy:

1. **`raw_prob` NULL** → row dropped at SQL (`raw_prob IS NOT NULL`).
2. **`market_result` NULL** → dropped at SQL.
3. **WS-fed features** (in `MISSING_INDICATOR_COLS`): impute with fold-train mean AND emit indicator column = 1.
4. **Other continuous features** NULL → impute with fold-train mean (no indicator).
5. **`available_balance_cents` NULL** (R1#C10): impute with fold-train mean (do NOT drop the row).
6. **`strategy` NULL** (R1#C16): coalesce to `'unknown'` at read; metadata-only column.

**Hard contract violations** (SystemExit):

- Any continuous feature has > 30% NULL on a fold's train split.
- Any column referenced by SELECT is missing from the source schema (R3#C14).

## Fold construction (walk-forward, rolling origin) — REWRITTEN per R2#C1

**Rule:** test slices march FORWARD in time, are DISJOINT across folds, and each fold's `train_end ≤ cal_start ≤ cal_end ≤ test_start ≤ test_end ≤ next_fold.test_start`.

Concretely, with `cutoff_end = T`, `train=60d, cal=15d, test=15d, fold_offset=30d, K=3`:

```
oldest         T-150d        T-120d        T-105d   T-90d        T-60d   T-45d   T-30d   T-15d    T (cutoff)
                │             │             │       │             │       │       │       │       │
fold 0:         ├──── train (60d)  ─────────┤      ├── cal(15d) ──┤      ├── test (15d) ──┤
                                                   │              │
fold 1:                       ├──── train (60d) ──────────────────┤      ├── cal(15d)─────┤      ├── test ───┤
                                                                                          │              │
fold 2:                                                  ├──── train (60d) ──────────────────┤      ├── cal──┤      ├── test ──┤
```

Mathematically, fold k:
- `test_end_k    = T - (K-1-k) × 30d                          = T - 60d, T - 30d, T`
- `test_start_k  = test_end_k - 15d`
- `cal_end_k     = test_start_k`
- `cal_start_k   = cal_end_k - 15d`
- `train_end_k   = cal_start_k`
- `train_start_k = train_end_k - 60d`

**Locked:** fold 0 is OLDEST test, fold K-1 is NEWEST test. Test slices are disjoint by construction.

**Minimum data check (R1#C6):**

```python
min_required_days = train_days + cal_days + test_days + fold_offset_days * (folds - 1)
                  = 60 + 15 + 15 + 30 * 2 = 150 days
oldest_row_ts = SELECT MIN(evaluation_time) post-filter
if (cutoff_end - oldest_row_ts).days < min_required_days:
    SystemExit(f"Phase 2: source has {available}d, need ≥{min_required}d ...")
```

**Per-fold abort thresholds:**

- `n_test < 50` → SystemExit (R1#C8 — conformal quantile is unstable)
- `n_test < 200` → soft warning in audit
- `n_train < N_TRAIN_MIN` (default 2000; per-asset CLI overridable) → SystemExit (R2#C13)

**Ticker-disjoint splits enforcement (R2#C15):** after the time-based split, assign each ticker entirely to the split its LATEST row belongs to. This eliminates correlated leakage at fold boundaries (15-min market straddling cal/test). Costs ~5-10% of rows at boundaries. Audit reports `n_tickers_dropped_at_boundary`.

## Normstats (per-fold)

For each fold, compute on the **train split only**:
- For each col in `CONT_FEATURE_COLS`:
  - Apply transform (`logit` / `log_cents_to_dollars` / `log1p` / `log1p_signed` / `identity`)
  - `mean = train[col].mean(skipna=True)`
  - `std = train[col].std(skipna=True, ddof=1)` (R2#C17 — sample std, locked)
  - `p1 = quantile(0.01)`, `p99 = quantile(0.99)`, `median`, `mad` (R2#C9 — surface robust stats)
  - if `std == 0`: emit error (constant column on train is a contract violation, not a benign edge case)
- For each col in `MISSING_INDICATOR_COLS`:
  - No transform; mean = fold_train_pct_missing; std = sqrt(p*(1-p)) clipped to ≥ 1e-3.

Persist `normstats_fold{F}.json`:

```json
{
  "fold": 0,
  "asset": "SOL",
  "cutoff_end": "...",
  "n_train": 8421,
  "ddof": 1,
  "transforms": { "breakeven_wr": "logit", ... },
  "stats": {
    "breakeven_wr": {"mean": 1.27, "std": 0.43, "n_nan": 0, "n_imputed": 12,
                      "p1": 0.5, "p99": 2.4, "median": 1.30, "mad": 0.32},
    ...
  },
  "sha256_self": "<sha of canonical-json>"
}
```

`apply_norm` lives in `cal_mlp/normalize.py` (R2#C10 — NOT train.py). Phase 4/5/6 import from there. Phase 7 startup parity-asserts.

## Output structure (train_id namespaced) — R3#C9 / R3#C18

```
data/cal_mlp/<asset>/
├── CURRENT                                  # one-line file: train_id of latest valid bundle
├── <train_id>/
│   ├── extract_bundle.json                  # the manifest
│   ├── extract_audit.json                   # diagnostic counters + per-cell stats
│   ├── ticker_vocab.json                    # asset-wide ticker→int mapping
│   ├── fold0.parquet
│   ├── fold1.parquet
│   ├── fold2.parquet
│   ├── normstats_fold0.json
│   ├── normstats_fold1.json
│   └── normstats_fold2.json
└── .extract.lock
```

`train_id` is content-derived (R3#C8):

```python
train_id = f"{cutoff_end_iso}-{sha8}"
sha8 = sha256(f"{asset}|{cfg_fp}|{cutoff_end}|{data_version_at_open}|{folds}|"
              f"{train_days}|{cal_days}|{test_days}|{fold_offset_days}").hexdigest()[:8]
```

Re-running with identical inputs produces identical `train_id` (idempotent — overwrites in place; previous artifacts unchanged).

Re-running with different inputs creates a new `train_id` directory; old directories coexist until a separate retention/GC job (out of scope).

## Atomic write protocol — REWRITTEN per R3#C1, C2, C3, C16

Order:

1. **mkdir** `data/cal_mlp/<asset>/<train_id>/` and `data/cal_mlp/<asset>/<train_id>/audit/`
2. **Acquire** `.extract.lock` with `LOCK_EX | LOCK_NB` via `os.open(..., O_RDWR | O_CREAT)` (no truncate).
3. **Write tmps** for each artifact in dependency order. For parquet: explicit fsync on file descriptor.

```python
def atomic_parquet_write(table: pa.Table, final_path: Path) -> None:
    tmp = final_path.with_suffix(f".parquet.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    with open(tmp, 'wb') as f:
        pq.write_table(table, f)
        f.flush()
        os.fsync(f.fileno())
    return tmp  # caller does the os.replace later
```

4. **Rename tmps → final paths** in dependency order (parquet folds → normstats → ticker_vocab → audit JSON → bundle JSON LAST). Bundle's existence is the gate (R3#C16); audit being written before bundle means bundle implies audit exists.
5. **fsync directory** after each `os.replace`.
6. **Update `CURRENT`** as the very last step: write tmp `CURRENT.tmp-...` containing the new train_id, fsync, `os.replace`. Phase 4/5/6 readers read CURRENT to discover the active bundle.

**On any failure** during steps 3-5 (R3#C3): unlink ALL tmps AND any already-renamed final paths IN REVERSE ORDER, then unlink bundle.json LAST (likely already absent in this case). Wrap each unlink in try/except and accumulate cleanup errors; raise `Phase2WriteError` chaining the original error and any cleanup failures.

**On failure during step 6** (CURRENT update): the new bundle exists on disk but readers don't see it; they continue to use the prior CURRENT. Self-healing: re-run extraction lands the same train_id (idempotent) and tries CURRENT update again.

## Lock domain (R3#C4)

**Phase 2's lock** (`data/cal_mlp/<asset>/.extract.lock`) guards the **artifact directory**: parquet, normstats, vocab, audit, bundle, CURRENT.

**Phase 4/5/6's lock** (`models/.cal_mlp_<asset>.lock`) guards the **model bundle** in `models/`. Different domain.

**Reader contract** (R1#C11): Phase 4 takes `LOCK_SH` on `data/cal_mlp/<asset>/.extract.lock` BEFORE opening CURRENT, and HOLDS it until all parquet/normstats/vocab reads are complete and loaded into memory. Releasing earlier voids atomicity (`os.replace` of a parquet under an open mmap returns stale data without error on macOS/Linux).

## Ticker vocabulary (R1#C9)

Build asset-wide vocab from the FULL post-filter source (all 3 folds combined):

```json
{
  "asset": "SOL",
  "vocab": {"<UNK>": 0, "SOLUSD-26APR2812": 1, "SOLUSD-26APR2813": 2, ...},
  "n_unique": 1281,
  "sha256_self": "..."
}
```

`ticker_id (int32)` is added to the parquet schema. UNK index 0 reserved for inference-time tickers not in vocab (Phase 4 embedding layer initializes UNK to zero vector).

**Cross-fold ticker constraint:** assert no ticker appears in more than one fold's split (after ticker-disjoint enforcement above). Audit reports `n_tickers_train_only, n_tickers_cal_only, n_tickers_test_only, n_tickers_dropped_at_boundary`.

## Output parquet schema

```
ticker          string                                  -- human-readable (audit/dashboards)
ticker_id       int32                                   -- vocab index (Phase 4 embedding)
evaluation_time timestamp[us, UTC]
asset           string
side            string                                  -- 'yes'/'no'
side_int        int8
strategy        string                                  -- 'unknown' if NULL
market_result   string                                  -- 'yes' | 'all_yes' | 'no' | 'all_no'
result_yes_int  int8                                    -- 1 if market_result IN ('yes','all_yes')
outcome         int8                                    -- the target
method_output_raw float32                               -- raw_prob (the input prior; NOT in CONT_FEATURE_COLS)
calibrated_prob_audit float32                           -- bot.py's calibrated_prob; metadata only
price_tier      int8
stc_bucket      int8
vol_regime_int  int8
split           string                                  -- 'train' | 'cal' | 'test'
fold            int8
[CONT_FEATURE_COLS as float32, post-transform]
[MISSING_INDICATOR_COLS as int8, 0/1]
fee_adjusted_edge_audit float32                          -- audit only; Phase 6 sim_pnl uses for tier replay
kelly_f_audit          float32                           -- audit only
available_balance_cents int64                            -- audit only; Phase 6 uses for sizing
settled_time            timestamp[us, UTC]               -- audit
rowid                   int64                            -- source-table rowid (audit/repro)
```

## Bundle JSON (extract_bundle.json)

```json
{
  "phase": 2,
  "schema_version": 2,
  "asset": "SOL",
  "train_id": "2026-04-27T00:00:00.000000Z-abcd1234",
  "cfg_fp": "f3b201e8a7c1d0e9",
  "cutoff_end": "2026-04-27T00:00:00.000000Z",
  "cutoff_end_evaluation": "2026-04-26T23:00:00.000000Z",
  "include_sub_floor": false,
  "data_version_at_open": 184321,
  "data_version_at_close": 184321,
  "ticker_vocab_path": "ticker_vocab.json",
  "ticker_vocab_sha256": "...",
  "audit_path": "extract_audit.json",
  "audit_sha256": "...",
  "fold_artifacts": [
    {"fold": 0, "parquet_path": "fold0.parquet", "parquet_sha256": "...",
     "normstats_path": "normstats_fold0.json", "normstats_sha256": "...",
     "n_train": 8421, "n_cal": 2103, "n_test": 2087,
     "test_window_start": "...", "test_window_end": "..."},
    ...
  ],
  "generated_at": "2026-04-28T15:30:00.000000Z",
  "extract_data_py_sha256": "<sha of extract_data.py at run time>"
}
```

## Audit JSON (extract_audit.json)

```json
{
  "asset": "SOL",
  "train_id": "...",
  "cfg_fp": "...",
  "cutoff_end": "...",
  "data_version_at_open": 184321,
  "source_total_rows_for_asset": 92334,    // R3#C7: indexed COUNT, fast
  "source_total_rows_post_filter": 92110,
  "drops": {
    "below_asset_floor": 0,
    "null_market_price": 0,
    "null_raw_prob": 142,
    "unsettled": 8932,
    "non_yes_no_result": 0,
    "null_settled_time": 12,
    "null_evaluation_time": 0
  },
  "per_fold": [
    {
      "fold": 0,
      "test_window_start": "...",
      "test_window_end": "...",
      "n_train": 8421, "n_cal": 2103, "n_test": 2087,
      "imputed_pct": {"breakeven_wr": 0.0, "spot_momentum_60s_bps": 0.03, ...},
      "missing_pct_test": {"spot_momentum_60s_bps_missing": 0.04, ...},
      "small_cell_warnings": ["cell_(3,2)_n_test=12 <50 floor"],
      "per_cell": {
        "(3,2)": {"n_train": 821, "n_cal": 198, "n_test": 187,
                   "train_positive_rate": 0.94, "test_positive_rate": 0.92,
                   "mean_method_output": 0.97, "void_rate": 0.005},
        ...
      }
    },
    ...
  ],
  "ticker_stats": {
    "n_unique_tickers": 1281,
    "mean_rows_per_ticker": 1.6,
    "pct_tickers_with_only_one_row": 0.72,
    "n_tickers_train_only": 854, "n_tickers_cal_only": 211, "n_tickers_test_only": 197,
    "n_tickers_dropped_at_boundary": 19
  },
  "void_rate": 0.003,
  "data_version_at_close": 184321,
  "generated_at": "..."
}
```

`per_cell` (R2#C2, R2#C6, R2#C16, R1#C8) gives Phase 3 the per-cell positive rates needed to choose a loss strategy and Phase 5/6 the per-cell counts needed for ship-blockers.

`ticker_stats.pct_tickers_with_only_one_row` (R2#C7) tells Phase 6 whether to honestly call its bootstrap "cluster-by-ticker" or just "ticker-resampled."

## CutoffEnd parsing (R3#C6)

```python
def normalize_cutoff_end(s: str | None) -> str:
    if s is None:
        s = (datetime.now(timezone.utc) - timedelta(days=1))
        return s.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    # Accept date-only ('2026-04-27'), date+T ('2026-04-27T15:30'),
    # full ISO with/without microseconds, with/without Z suffix.
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M",
                "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError:
            continue
    raise SystemExit(f"--cutoff-end could not parse: {s!r}")
```

## Determinism (scoped per R3#C13)

The DATA extracted is deterministic given `(state.db data_version, cutoff_end, cfg_fp, fold parameters)`. The on-disk parquet bytes are NOT guaranteed identical across pyarrow versions; regression tests compare logical content (load both, assert frames equal). Audit JSON's per-feature `n_imputed`, `n_train/cal/test`, and per-cell counts ARE byte-deterministic across runs with the same inputs.

## Failure modes (locked exit codes per R3#C12)

```python
class Phase2Error(RuntimeError): pass
class Phase2DBError(Phase2Error): pass         # exit 2
class Phase2ContractError(Phase2Error): pass    # exit 3
class Phase2LockError(Phase2Error): pass        # exit 4
class Phase2WriteError(Phase2Error): pass       # exit 5
class Phase2SchemaError(Phase2Error): pass      # exit 6
```

The CLI catches each and raises `SystemExit(code, message)`.

## Logging (R3#C17)

- Default: structured-INFO to stderr, structured-JSON summary to stdout on success.
- `--quiet`: stderr suppressed below WARN.
- On failure: stdout empty, stderr has the error.

`extract_data.py --quiet --asset SOL | jq .train_id` works in cron pipelines.

## What this phase does NOT do

- No outlier capping in parquet values (R2#C9 — but normstats DO record p1/p99/median/mad for Phase 3 to use).
- No SMOTE / class balancing (per-cell positive rates surfaced in audit; Phase 3 chooses).
- No model training, no normalization-eager parquet (transforms applied to a derived column; raw `market_price` etc. also kept in parquet for audit).

## Open issues for Round 2 review

1. The architecture lock (skip term `logit(raw_prob)` + MLP residual) is a Phase 3 commitment made in Phase 2. Confirm via review that this is the right phase boundary for that decision.
2. The `time_decayed_proximity` feature is heuristic. Justify or drop.
3. Ticker-disjoint splits drop ~5-10% of rows. Verify on real SOL data that the drop rate is acceptable.
4. SQLite WAL snapshot consistency vs `--cache=shared` — confirm whether this connection-string variant changes snapshot semantics vs the default.
5. `apply_norm` location in `cal_mlp/normalize.py` — verify that Phase 4/5/6 can import from there without a circular dep on Phase 2's main module.
