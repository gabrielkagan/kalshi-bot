# Phase 2: Data Extraction (extract_data.py)

**Status:** spec — to converge through adversarial review.
**Anchor:** `kb-research/bot/p2-phases-4-to-8-design.md`.
**Output:** per-asset, per-fold parquet files + normstats + bundle metadata for Phase 3/4 training and Phase 5/6 calibration/validation.

## Goal

Pull rows from `evaluated_opportunities`, build train/cal/test splits using K-fold walk-forward (3 folds × 30-day window offsets), bin into categorical buckets, compute the MLP target and method-output, write atomically.

## Inputs

- `state.db` (read-only via `mode=ro`)
- CLI: `--asset {BTC,ETH,SOL,XRP}`, `--folds 3`, `--cal-days 15`, `--test-days 15`, `--train-days 60`, `--fold-offset-days 30`, `--out-dir data/cal_mlp/<asset>`, optional `--cutoff-end <ISO>` (default = now-1d to avoid in-flight rows)

## Source query

```sql
SELECT ticker, evaluation_time, asset, side, strategy,
       market_price, seconds_to_close, vol_regime, z_score,
       yes_spread_cents, calibrated_prob, calibration_method,
       raw_prob, breakeven_wr, fee_adjusted_edge, kelly_f,
       is_weekend, hour_of_day_utc, day_of_week,
       market_result, available_balance_cents,
       spot_momentum_60s_bps, spot_momentum_5m_bps,
       spot_realized_range_15m_bps,
       btc_spot_change_5m_bps, btc_realized_vol_15m,
       window_max_buf_pct, window_min_buf_pct, minutes_above_strike,
       spot_distance_to_strike_sigma, prob_breakeven_gap,
       spot_coinbase_kraken_gap_bps, kalshi_flow_depth_velocity
FROM evaluated_opportunities
WHERE asset = ?
  AND product_type = '15m'
  AND ticker NOT LIKE 'SPORTS-%'
  AND market_price IS NOT NULL
  AND market_price > 0
  AND raw_prob IS NOT NULL
  AND evaluation_time IS NOT NULL
  AND evaluation_time < ?  -- cutoff_end
  AND market_result IN ('yes', 'no')  -- exclude unsettled
ORDER BY evaluation_time, ticker
```

`evaluation_time < cutoff_end` excludes in-flight rows. **Locked:** rows with `market_result NOT IN ('yes','no')` are dropped — they cannot contribute to a labeled fold.

## Bucketization

```python
PRICE_BIN_CUTOFFS = [80, 90, 96]   # → 4 tiers (0..3)
STC_BIN_CUTOFFS = [120, 300, 600]  # → 4 buckets (0..3)
price_tier = np.digitize(market_price, PRICE_BIN_CUTOFFS, right=False)
stc_bucket = np.digitize(seconds_to_close, STC_BIN_CUTOFFS, right=False)
vol_regime_int = (vol_regime == 'elevated').astype(int)
side_int = (side == 'yes').astype(int)
```

Bleed cell = `(price_tier=3, stc_bucket=2)` = (≥96¢, 300-600s) — Phase 5/6 ship-blockers reference this cell explicitly.

## Method output (the calibrator's input prior)

```python
method_output = COALESCE(calibrated_prob, raw_prob)
```

When `FIFTEEN_M_CALIBRATION_ENABLED=False`, calibrated_prob is the passthrough of raw_prob; older rows may have NULL calibrated_prob — fall back to raw_prob. Both are in [0,1].

## Outcome (target)

```python
outcome = (market_result == side).astype(int)
```

Binary 0/1. `market_result` is the settlement side ('yes' or 'no'); the trade wins when the bot's side matches.

## Continuous feature set (CONT_FEATURE_COLS)

Locked list, in stable order (used for normstats indexing):

```python
CONT_FEATURE_COLS = [
    'method_output',       # the input prior — calibrator predicts residual on this
    'raw_prob',            # also include uncalibrated for redundancy
    'breakeven_wr',
    'market_price',        # entry price, cents
    'seconds_to_close',
    'z_score',
    'yes_spread_cents',
    'fee_adjusted_edge',   # bot.py's own edge — gives the MLP information about
                           # how the bot already "feels" about this candidate
    'kelly_f',
    'spot_momentum_60s_bps',
    'spot_momentum_5m_bps',
    'spot_realized_range_15m_bps',
    'btc_spot_change_5m_bps',
    'btc_realized_vol_15m',
    'window_max_buf_pct',
    'window_min_buf_pct',
    'minutes_above_strike',
    'spot_distance_to_strike_sigma',
    'prob_breakeven_gap',
    'spot_coinbase_kraken_gap_bps',
    'kalshi_flow_depth_velocity',
    'hour_of_day_utc',     # 0-23 — keep as continuous; MLP can learn cyclic
    'available_balance_cents',  # used by Kelly tier; redundant but informative
]
```

Categorical features (one-hot in the dataset, not normalized):
- `price_tier` (0-3)
- `stc_bucket` (0-3)
- `vol_regime_int` (0-1)
- `side_int` (0-1)
- ticker (passed as `ticker_id`; embedded by training, NOT one-hot — high cardinality)

`N_CONT = len(CONT_FEATURE_COLS)`. `cfg_fp = sha256(",".join(CONT_FEATURE_COLS))[:12]` — the feature-schema fingerprint that Phase 6 A/B requires to match between base and challenger bundles.

## NULL handling

All continuous features may be NULL in source rows (older data, backfill gaps). Policy:

1. If `method_output` is NULL after the COALESCE, **drop the row** (we can't train a residual without an input prior).
2. Other continuous features: NULL → fold-train mean (computed in normstats step).
3. **Locked contract:** if any continuous feature has > 30% NULL on a fold's train split, abort the fold with `SystemExit('Phase 2 contract violation: <col> > 30% NULL on fold N train split')`. Indicates a backfill regression that would silently bias normstats.

## Fold construction (walk-forward, rolling origin)

```
oldest_row_ts ────── offset_0 ──────────── offset_1 ──── offset_2 ──── cutoff_end
                       │                                                    │
                       ├──── train (60d) ──── cal (15d) ──── test (15d) ────┘  fold 0
                                ├──── train (60d) ──── cal (15d) ──── test (15d)  fold 1
                                          ├──── train (60d) ──── cal (15d) ──── test (15d)  fold 2
```

Folds shift forward by `--fold-offset-days 30` each. Concretely, with cutoff_end = today and 90 days of data:

- fold 0: train [0, 60) cal [60, 75) test [75, 90)
- fold 1: train [-30, 30) cal [30, 45) test [45, 60)  ← shift origin earlier
- fold 2: train [-60, 0) cal [0, 15) test [15, 30)

**Locked:** fold 0 is the most recent (largest offset toward cutoff_end). Phase 5/6 default to fold 0 for the conformal calibration set + test reporting.

If a fold's test or cal split has 0 settled rows (e.g., low-volume day window), abort that fold with `SystemExit('Phase 2 contract violation: fold N has empty cal/test')`.

## Normstats (per-fold)

For each fold, compute on the **train split only**:
- For each col in `CONT_FEATURE_COLS`:
  - `mean = train_split[col].mean(skipna=True)`
  - `std = train_split[col].std(skipna=True)` (use `np.std(..., ddof=0)` — population std for stable batch norm; Phase 4 may convert to ddof=1 if needed but lock the convention here)
  - if `std == 0`: set `std = 1.0` (constant column — pass through unchanged)

Persist as `normstats_fold{F}.json`:
```json
{
  "fold": 0,
  "asset": "SOL",
  "cutoff_end": "2026-04-27T00:00:00Z",
  "n_train": 8421,
  "stats": {
    "method_output": {"mean": 0.78, "std": 0.14, "n_nan": 0, "n_imputed": 0},
    "raw_prob": {"mean": 0.79, "std": 0.13, "n_nan": 0, "n_imputed": 0},
    ...
  },
  "sha256_self": "<sha of canonical-json without this field>"
}
```

`apply_norm(df, normstats, CONT_FEATURE_COLS)` (in `train.py`) does `(x - mean) / std` after NULL-imputing with mean. Sentinel `n_imputed > 0` is an audit signal — Phase 4 logs it, Phase 6 surfaces it as a soft-flag if any fold's test split has > 5% imputed.

## Output parquet

One parquet per fold: `data/cal_mlp/<asset>/fold{F}.parquet`. Schema (pyarrow):

```
ticker (string)
evaluation_time (timestamp[us, UTC])
asset (string)
side (string)            # 'yes'/'no' literal — UI-friendly
side_int (int8)
strategy (string)
market_result (string)
outcome (int8)           # the target
method_output (float32)  # the input prior
price_tier (int8)
stc_bucket (int8)
vol_regime_int (int8)
split (string)           # 'train' | 'cal' | 'test'
fold (int8)
[CONT_FEATURE_COLS as float32]
[also keep raw originals: market_price, seconds_to_close, vol_regime, etc.]
```

`split` is INSIDE the parquet (not separate files) so Phase 4 can `df[df['split'] == 'train']` cleanly. `fold` and `asset` are constant columns but included for safety (catches accidental cross-fold concat).

## Atomic write

3-step protocol per fold:

1. Write all rows to `fold{F}.parquet.tmp-<pid>-<uuid>` with explicit fsync.
2. fsync the parent directory.
3. `os.replace` the tmp into place.

Same for `normstats_fold{F}.json`. After all folds + normstats land successfully, write `extract_bundle.json` (which lists each fold's parquet path + sha256, normstats path + sha256, cfg_fp, cutoff_end, asset, source_db_path, source_db_sha_at_extract, n_train/cal/test per fold). Phase 4's training reads ONLY `extract_bundle.json` to discover the fold artifacts.

If any fold fails, **delete all partials** and raise. Bundle file is the single source of truth — no half-extracted state.

## Lock file

Acquire `data/cal_mlp/<asset>/.extract.lock` with `fcntl.LOCK_EX` (exclusive write). Phase 4/5/6 readers take `LOCK_SH`. If lock cannot be acquired immediately, abort with operator message — never block (CI/cron-friendly).

Lock file is opened with `os.open(... O_RDWR|O_CREAT)` so neither writer nor reader truncates it (R-p6-impl-2#C9 lesson).

## Determinism

- Source query `ORDER BY evaluation_time, ticker` is deterministic.
- Bucketization is deterministic.
- Normstats use `train_split` only — fold split → normstats is deterministic.
- No RNG used in extraction (RNG enters in Phase 4 training).

## Schema fingerprint (cfg_fp)

```python
cfg_fp = hashlib.sha256(
    ",".join(CONT_FEATURE_COLS).encode()
    + b"|" + ",".join([str(x) for x in PRICE_BIN_CUTOFFS]).encode()
    + b"|" + ",".join([str(x) for x in STC_BIN_CUTOFFS]).encode()
).hexdigest()[:12]
```

If `CONT_FEATURE_COLS` or bucketization changes, `cfg_fp` changes. Phase 6 A/B refuses to compare bundles with different `cfg_fp` (different feature schemas).

## Audit JSON

Per-fold parquet and the bundle JSON are the durable outputs. Additionally write `extract_audit_<asset>_<cfg_fp>_<train_id>.json` to `data/cal_mlp/<asset>/audit/`:

```json
{
  "asset": "SOL",
  "train_id": "2026-04-28T15:30:00Z-abcd1234",
  "cfg_fp": "f3b201e8a7c1",
  "cutoff_end": "...",
  "source_db_sha": "<sha of state.db at extract time>",
  "source_total_rows_pre_filter": 184321,
  "source_total_rows_post_filter": 92110,
  "drops": {
    "null_market_price": 0,
    "null_raw_prob": 142,
    "unsettled": 8932,
    "non_yes_no_result": 12,
    "null_method_output_after_coalesce": 0
  },
  "per_fold": [
    {"fold": 0, "n_train": 8421, "n_cal": 2103, "n_test": 2087, "imputed_pct": {...}},
    ...
  ],
  "n_unique_tickers": 1281,
  "generated_at": "..."
}
```

## Failure modes (locked SystemExit raises)

1. `state.db` not found / not readable → SystemExit
2. `evaluated_opportunities` table missing → SystemExit
3. Source query returns 0 rows → SystemExit (likely wrong asset or bad cutoff_end)
4. Any fold has empty cal/test → SystemExit
5. Any continuous col >30% NULL on train → SystemExit
6. Atomic write failure → SystemExit (and clean up partial tmps)
7. Lock acquisition fails → SystemExit (operator must investigate stale writer)

## What this phase does NOT do

- No feature engineering beyond what's in the source schema (Tier 1-6 columns are already populated by the bot).
- No outlier capping (Phase 4 may add this; out of scope for extraction).
- No SMOTE / class balancing (we want raw distribution; Phase 4 may class-weight).
- No ticker filtering by frequency (low-frequency tickers are still informative; Phase 4 ensemble handles).

## Open questions for adversarial review

1. Should we drop rows with `available_balance_cents IS NULL`? They can't replay sizing in Phase 6 — but excluding them biases the train set toward live-trade rows.
2. Is `kelly_f` a feature, a target, or an audit-only column? It's derived from `raw_prob` so including it as a feature risks leakage.
3. `fee_adjusted_edge` is also derived from `raw_prob - market_price/100 - fee/100`. Same leakage concern.
4. The cutoff_end default of "now - 1d" — should it be tighter (e.g., 1h) or looser (e.g., 7d for settled-trade stability)?
5. For SOL specifically (the calibration-pocket asset), should we include extra features about proximity-to-strike that the post-mortem identified as missing? `spot_distance_to_strike_sigma` is the closest existing column.

These are the questions adversarial review will close on.
