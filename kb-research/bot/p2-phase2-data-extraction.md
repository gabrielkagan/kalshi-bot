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

All reads use **one** sqlite3 connection opened with `f'file:{db_path}?mode=ro'` (R2-OPS#C2: dropped `cache=shared` — intra-process only, irrelevant here). SQLite WAL gives a consistent read snapshot at first query.

`PRAGMA data_version` is captured at open and at close, persisted in audit JSON. **R2-OPS#C3:** `data_version_at_close > data_version_at_open` is EXPECTED — the live bot commits ~10/sec. WAL gives a stable snapshot at first read regardless. The two values bracket the extraction window in commit-counter space; **DO NOT** treat inequality as a contract violation.

Per-asset entry-floor filter is on by default; disable with `--include-sub-floor` (logged + recorded in audit).

```sql
-- Asset floor = bot.py:219-225 mirrored: BTC=88, ETH=90, SOL=86, XRP=92.
-- (Skip the floor when --include-sub-floor is set.)

SELECT ticker, evaluation_time, asset, side, strategy,
       market_price, seconds_to_close, vol_regime, z_score,
       yes_spread_cents, calibrated_prob,
       raw_prob, fee_adjusted_edge, kelly_f,
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
  AND market_result IN ('yes','all_yes','no','all_no')
  AND settled_time IS NOT NULL
  AND settled_time < ?               -- cutoff_end (settlement watermark)
  -- R2-OPS#C4: rows already settled before cutoff are exempt from the
  -- evaluation-watermark gap (they're caught up). Only unsettled rows need
  -- the 1h buffer to allow batched-settlement to finish — but those have
  -- already been excluded by `settled_time IS NOT NULL`.
ORDER BY evaluation_time, ticker, rowid
```

**R1#C1**: Settlement results include `'all_yes'` and `'all_no'` (bot.py:4351 `_OK_RESULTS`). Filtering to only `('yes','no')` silently drops fully-filled markets.

**R1#C3 + R2-OPS#C4**: `cutoff_end` is a SETTLEMENT watermark. The evaluation-watermark gap was removed — rows with `settled_time IS NOT NULL AND settled_time < cutoff_end` are unambiguously caught up regardless of when they were evaluated. `cutoff_end` alone is persisted in `bundle.json`.

**R1#C4**: Per-asset floor mirrored from bot.py:219-225, vendored into `cal_mlp/features.py` as the constant `ASSET_FLOORS` (single source of truth — see Module layout section). Phase 7 startup parity-asserts.

**R1#C18 / R3#C5**: `rowid` tiebreaker for total order; `data_version` audited at open/close.

## Drop counting algorithm (R4#C2 — locked)

The single SELECT applies all WHERE clauses simultaneously, so a row failing multiple predicates cannot be unambiguously assigned to one drop bucket. To make the invariant `source_total_rows_for_asset == source_total_rows_post_filter + sum(drops)` mechanically achievable, drops are computed via a SECOND single-pass query that pulls `COUNT(*) WHERE asset=?` and bucket-classifies each excluded row by the FIRST failing predicate in this fixed order:

```python
DROP_PREDICATES = [   # order is part of cfg_fp; do not reorder without bumping schema_version
    ('non_15m_product_type',     "product_type != '15m'"),
    ('sports_ticker',            "ticker LIKE 'SPORTS-%'"),
    ('null_market_price',        "market_price IS NULL"),
    ('non_positive_market_price',"market_price IS NOT NULL AND market_price <= 0"),
    ('below_asset_floor',        "market_price > 0 AND market_price < ?"),  # asset floor
    ('null_raw_prob',            "raw_prob IS NULL"),
    ('null_evaluation_time',     "evaluation_time IS NULL"),
    ('non_yes_no_result',        "market_result NOT IN ('yes','all_yes','no','all_no')"),
    ('null_settled_time',        "settled_time IS NULL"),
    ('settled_after_cutoff',     "settled_time >= ?"),                       # cutoff
    # R-p2-impl-r1#C7 + R2#C17: two predicates added post-spec.
    ('null_or_invalid_side',     "side NOT IN ('yes','no')"),                # R-p2-spec-r2#H1
    ('null_seconds_to_close',    "seconds_to_close IS NULL"),
]
```

**R-p2-spec-r2#H1:** the canonical list lives in `features.DROP_PREDICATES_ORDER` (12 entries). Spec body kept as historical reference; reorderings or new entries MUST update both `features.py` AND `compute_cfg_fp` (which embeds the order in `cfg_fp`). A spec-only change without bumping schema_version + updating impl will produce a different `cfg_fp` than existing bundles → all bundles refuse to load.

Implementation: `SELECT * FROM evaluated_opportunities WHERE asset=?` (no other clauses), iterate rows, for each row determine which (first) predicate it fails — increment that bucket. After iteration, `sum(drops) + n_kept` MUST equal `source_total_rows_for_asset` or raise Phase2ContractError. This second pass is single-process, in-Python (no separate SQL), so cost is one full scan vs. the data SELECT's filtered scan — acceptable.

Phase 2 unit test: inject a row failing TWO predicates (e.g., `null_market_price` AND `null_raw_prob`); assert it counts in the EARLIER bucket (`null_market_price`). Reordering DROP_PREDICATES is a schema change.

## Schema check at extract time (R3#C14)

Before the SELECT, run `PRAGMA table_info(evaluated_opportunities)` on the SAME connection used for the data SELECT. Assert every column referenced exists.

**R3-cnv#C7 (PRAGMA + SELECT consistency):** PRAGMA queries do not take their own read snapshot, but on a single connection any DDL applied between the PRAGMA and the SELECT would have to acquire SQLite's write lock, blocked by the running connection. Schema observed by PRAGMA == schema used by SELECT, by single-connection serialization. Spec contract: never split schema-check and data-pull across connections.

On mismatch:

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
method_output_raw = raw_prob                          # unclipped, audit only
RAW_PROB_CLIP_EPS = 1e-6                              # logit ≈ ±13.8
logit_raw_prob_clipped = logit(clip(raw_prob, EPS, 1-EPS))   # what Phase 4 reads
```

**R2-ML#C1 (clip site lock):** Phase 2 writes BOTH `method_output_raw` (audit) AND `logit_raw_prob_clipped` (the value Phase 4 uses as the skip term). The clip happens once at extract time; Phase 4 never recomputes. `RAW_PROB_CLIP_EPS` is part of `cfg_fp`. This mechanically prevents inf/NaN even if a buggy upstream change produces 1.0.

`calibrated_prob` is bot.py's `min(raw_prob, dynamic_cap)` post-cap value (bot.py:8453+). The cap is downstream sizing concern. Phase 7 deploys cleanly: bot.py computes `raw_prob`, MLP predicts `Δ`, bot writes `final_prob = sigmoid(logit_raw_prob_clipped + Δ)`, then sizing applies the existing dynamic_cap.

**Architecture lock for Phase 3** (binding for this spec's parquet schema):

```
final_prob = sigmoid(logit_raw_prob_clipped + Δ)
where Δ = MLP(features_excluding_logit_raw_prob)   # the residual head
```

**R2-ML#C2 (honest framing):** raw_prob is excluded as a literal feature so the SKIP TERM dominates the prior path. The MLP can still recover information about it via correlated features (`market_price`, `time_decayed_proximity`, etc.), which is intentional — Δ should be allowed to depend on the prior, just not short-circuit through a literal copy.

**R2-ML#C3 + R3-ML#C1 + R4#C3 (loss formulation lock — REVISED):** The earlier `1/sqrt(p_cell+ε)` form had the wrong direction — it weighted high-WR cells DOWN (so the bleed cell at p~0.88 got LESS training emphasis than thin cells at p~0.50, which is the opposite of intent). Locked form:

```python
# Per-cell calibration-residual weighting (fold-train only):
n_train_per_cell = count of train rows in cell (price_tier, stc_bucket)
p_cell           = train_positive_rate per cell                         # NaN if n_train_per_cell == 0
prior_cell       = train_mean_method_output per cell                    # NaN if n_train_per_cell == 0
# R5#C1: nan_to_num pre-fill avoids np.where's eager-branch RuntimeWarning when both inputs are NaN
p_safe           = np.nan_to_num(p_cell, nan=0.0)
prior_safe       = np.nan_to_num(prior_cell, nan=0.0)
miscal_cell      = np.where(n_train_per_cell > 0, abs(p_safe - prior_safe), 0.0)
w_cell           = 1.0 + 4.0 * miscal_cell                              # in [1, 5]; floor=1 for empty cells
loss = BCE(sigmoid(logit_raw_prob_clipped + Δ), outcome) * w_cell[row.cell]
```

This explicitly up-weights cells where the production prior `method_output` diverges from the empirical positive rate — exactly the bleed cells we want to fix. **Both `p_cell` and `prior_cell` are computed on the FOLD'S TRAIN SPLIT ONLY** — never on cal or test (no leakage).

**Empty-cell handling (R4#C3):** Cells with `n_train_per_cell == 0` get `w_cell = 1.0` (the floor). Since no train row falls in such a cell, the weight is never actually applied during training, but materializing the lookup with `np.where(..., 0.0)` avoids NaN propagation in vectorized implementations.

**Audit field naming (R4#C3):** the audit JSON field `mean_method_output` is renamed to `train_mean_method_output` to match the loss-formula source unambiguously.

The `4.0` multiplier and `1.0` floor are part of `cfg_fp`. Phase 3 ablation may tune them; the form is locked here.

**R2-ML#C4 (Phase 7 inference contract):** when bot.py's ProbabilityEngine returns `raw_prob = None` (null_result fallback at bot.py:8400), the MLP is bypassed — no calibrated_prob written, opportunity skipped just as today. The MLP is only invoked when `raw_prob ∈ [EPS, 1-EPS]`. Phase 7 must enforce this guard at the call site.

**R3-cnv#C4 (Phase 4 scale handoff lock):** `logit_raw_prob_clipped` is in raw logit units (range `[-13.8156, +13.8156]`). Phase 4 MUST consume it WITHOUT normalization (it's not in CONT_FEATURE_COLS, not z-scored), and the MLP's Δ output MUST be unbounded logit-space (no final sigmoid/tanh on Δ). The forward computation `final_prob = sigmoid(logit_raw_prob_clipped + Δ)` is the locked architecture; Phase 4 review may not relax this. Phase 7 startup parity-asserts by computing one inference forward pass with `raw_prob=0.94, Δ=0` and asserting `final_prob == 0.94 ± 1e-6`.

**R3-cnv#C5 (UNK ticker honest framing):** `is_unk_ticker` is always 0 in Phase 2 training data BY CONSTRUCTION (vocab built from this extract). Phase 4 cannot learn an "unk → high uncertainty" pathway from training data; the MLP's response to `is_unk_ticker=1` at Phase 7 inference is undefined extrapolation. Phase 5/6 MUST handle UNK uncertainty via conformal-side σ inflation (forced max-uncertainty cell quantile), NOT MLP-learned behavior. The `is_unk_ticker` column is a routing flag for downstream code, NOT a learned feature.

**R2-ML#C15 (architecture lock phase boundary):** the parquet is a feature-superset. `method_output_raw` (raw_prob), `logit_raw_prob_clipped`, `prob_breakeven_gap`, `breakeven_wr_audit` (audit-only), etc. are all written. `cfg_fp` commits the SUBSET that Phase 4 consumes. If Phase 3 review wants to revise (e.g., temperature-scaled raw_prob input), the parquet does NOT need re-extraction — only `cfg_fp` and the Phase 4 model definition change. Re-extraction is the fallback if Phase 3 wants different bucketization or different pre-filter rules.

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
# Continuous features — z-score normalized after per-column transform unless
# marked 'identity_no_zscore'. Lives in cal_mlp/features.py (R2-OPS#C13).
# R2-OPS#C5: removed `breakeven_wr` (linearly redundant with market_price).
CONT_FEATURE_COLS = [
    'market_price',                            # log1p(market_price/100) → z
    'seconds_to_close',                        # identity → z
    'z_score',                                 # identity → z
    'yes_spread_cents',                        # identity → z
    'spot_momentum_60s_bps',                   # identity → z
    'spot_momentum_5m_bps',                    # identity → z
    'spot_realized_range_15m_bps',             # log1p_signed → z
    'btc_spot_change_5m_bps',                  # identity → z
    'btc_realized_vol_15m',                    # log1p → z
    'window_max_buf_pct',                      # identity → z
    'window_min_buf_pct',                      # identity → z
    'minutes_above_strike',                    # identity → z
    'spot_distance_to_strike_sigma',           # identity → z
    'abs_spot_distance_to_strike_sigma',       # |spot_distance| — symmetry prior (R2-ML#C6)
    'time_decayed_proximity',                  # spot_distance × (1 - seconds_to_close/900) — R2-ML#C5 inverted formula
    'prob_breakeven_gap',                      # identity → z (kept; collinear with raw_prob/market_price but small)
    'spot_coinbase_kraken_gap_bps',            # identity → z
    'kalshi_flow_depth_velocity',              # identity → z
    'log_balance_dollars',                     # log1p(available_balance_cents/100) → z
    'hour_sin', 'hour_cos',                    # identity_no_zscore (R2-ML#C14)
]

CONT_FEATURE_TRANSFORMS = {
    'market_price': 'log_cents_to_dollars',
    'spot_realized_range_15m_bps': 'log1p_signed',
    'btc_realized_vol_15m': 'log1p',
    'log_balance_dollars': 'log_cents_to_dollars',
    'hour_sin': 'identity_no_zscore',           # bounded [-1,1], analytical mean=0
    'hour_cos': 'identity_no_zscore',
    # all others: 'identity' (z-scored)
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
3. **WS-fed features** (in `MISSING_INDICATOR_COLS`): impute with FOLD-TRAIN MEAN AND emit indicator column = 1.
4. **Other continuous features** NULL → impute with FOLD-TRAIN MEAN (no indicator).
5. **`available_balance_cents` NULL** (R1#C10): impute with FOLD-TRAIN MEAN (do NOT drop the row).
6. **`strategy` NULL** (R1#C16): coalesce to `'unknown'` at read; metadata-only column.

**R-p2-spec-r2#M1 IMPORTANT:** The pseudocode below describes the LOGICAL pipeline. In the impl, Phase 2 writes RAW post-feature-engineering values to the parquet (`extract_data.build_feature_frame`), and `apply_norm` is invoked LAZILY by Phase 4 (training), Phase 5 (conformal fit), Phase 6 (sim_pnl/validate), and Phase 7 (predict-time). Reading the parquet directly returns un-normalized values; only the model batch tensor sees post-z-score floats. This split lets Phase 4/5/6/7 use a single `apply_norm` implementation (`normalize.py`) and lets Phase 2 stay agnostic to the consumer's normstats version.

**R3-stitch#C3 + R4#C1 (imputation order — LOCKED, fixed in R4):**

```python
for fold in folds:
    for col in CONT_FEATURE_COLS:
        # Step 1: transform train/cal/test in place (raw value space → transformed space)
        for split in (fold.train, fold.cal, fold.test):
            split[col] = transform(split[col], CONT_FEATURE_TRANSFORMS[col])
        # Step 2: compute mean/std on POST-TRANSFORM, NON-NULL train values only
        train_non_null = fold.train[col].dropna()    # post-transform
        mean = train_non_null.mean()
        std  = train_non_null.std(ddof=1)
        # Step 3: impute with the post-transform train mean and z-score (skipping no_zscore)
        for split in (fold.train, fold.cal, fold.test):
            n_imputed = split[col].isna().sum()      # per-split, audited
            split[col] = split[col].fillna(mean)
            if CONT_FEATURE_TRANSFORMS[col] != 'identity_no_zscore':
                split[col] = (split[col] - mean) / std
        # Persist {mean, std, p1, p99, median, mad} per fold from train_non_null
```

**Critical:** mean and std are computed on the POST-TRANSFORM train values. Imputing the pre-transform mean (e.g., raw `market_price ≈ 90¢`) into a post-transform column (`log_cents_to_dollars(market_price/100) ≈ 0.64`) would produce ~140× outliers — this was a bug in R3 caught in R4#C1.

Train, cal, AND test rows are all imputed with the FOLD-TRAIN MEAN (not per-split, not zero, not median). Per-split imputation would leak distributional information from cal/test back into the feature.

Phase 2 unit tests:
- Inject NULL into a known-value test row → assert imputed value equals post-transform train mean.
- Construct a column with raw mean=90 and `log_cents_to_dollars` transform → assert post-transform mean is ~log1p(0.9) ≈ 0.64, NOT 90.

**Hard contract violations** (SystemExit):

- Any continuous feature has > 30% NULL on a fold's train split.
- Any column referenced by SELECT is missing from the source schema (R3#C14).

## Fold construction (walk-forward, rolling origin) — REWRITTEN per R2#C1

**Rule:** test slices march FORWARD in time, are DISJOINT across folds, each fold's `train_end ≤ cal_start ≤ cal_end ≤ test_start ≤ test_end`. Across folds, `test_end_k < test_start_{k+1}` (gap is exactly `fold_offset_days - test_days = 15d` with default params).

Mathematically, fold k (0-indexed, K=3):

```
test_end_k    = T - (K-1-k) × 30d              = T - 60d, T - 30d, T
test_start_k  = test_end_k - 15d
cal_end_k     = test_start_k
cal_start_k   = cal_end_k - 15d
train_end_k   = cal_start_k
train_start_k = train_end_k - 60d
```

Concrete spans with `T = cutoff_end`:

| fold | train       | cal           | test          |
|------|-------------|---------------|---------------|
| 0    | [T-150,T-90)| [T-90,T-75)  | [T-75,T-60)  |
| 1    | [T-120,T-60)| [T-60,T-45)  | [T-45,T-30)  |
| 2    | [T-90,T-30) | [T-30,T-15)  | [T-15,T)     |

```
T-150d   T-120d  T-90d   T-75d   T-60d   T-45d   T-30d   T-15d    T
   │        │       │       │       │       │       │       │      │
fold 0  ├── train (60d) ────┤  ├cal┤  ├test─┤
fold 1               ├── train (60d) ────┤  ├cal┤  ├test─┤
fold 2                            ├── train (60d) ──┤  ├cal┤  ├test┤
```

**Locked:** fold 0 is OLDEST test, fold K-1 is NEWEST test. Test slices are disjoint with 15d gaps between them. Train spans overlap across folds (this is normal walk-forward — fold 1's train [T-120,T-60) contains fold 0's TEST [T-75,T-60), which is fine because fold 1 trains on data that fold 0 reported test metrics for).

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
- `n_rows_reassigned_at_boundary / n_in_span > 0.30` → soft flag `high_boundary_reassign_rate` (R2-OPS#C15 + R-impl-r3#C1: threshold raised from 0.20 to 0.30 to match the renamed metric — reassignments naturally include same-fold cross-split moves and are higher than dropped-only counts)

**R2-OPS#C12 (namespace clarification):** the `n_test < 50` threshold applies to the FOLD's total test rows. Per-cell counts may be smaller and are surfaced in `audit.per_fold[k].small_cell_warnings` for Phase 5's `n_test < 20 → use global quantile` fallback. Phase 2 does NOT abort on small per-cell counts — small bleed-cell counts are expected.

**Ticker-disjoint splits enforcement (R2#C15):** after the time-based split, assign each ticker entirely to the split its LATEST IN-SPAN row belongs to. This eliminates correlated leakage at fold boundaries (15-min market straddling cal/test). Costs ~5-10% of rows at boundaries. Audit reports `n_rows_reassigned_at_boundary` — count of rows whose split CHANGED (e.g., a train row reassigned to test because the same ticker had a later test row); this is the boundary-cost metric, not "rows dropped to None" (that metric is structurally always 0 by construction).

## Normstats (per-fold)

For each fold, compute on the **train split only**:
- For each col in `CONT_FEATURE_COLS`:
  - Apply transform (`logit` / `log_cents_to_dollars` / `log1p` / `log1p_signed` / `identity` / `identity_no_zscore`)
  - For `identity_no_zscore` columns (e.g., `hour_sin`, `hour_cos`): persist `{mean: 0.0, std: 1.0, _no_zscore: true}` analytical stats; do NOT fit from data. `apply_norm` short-circuits these columns (no z-score).
  - For all other transforms: `mean = train_non_null[col].mean()`, `std = train_non_null[col].std(ddof=1)` (R2#C17 — sample std, locked).
  - `p1 = quantile(0.01)`, `p99 = quantile(0.99)`, `median`, `mad` (R2#C9 — surface robust stats for Phase 3)
  - **R3-stitch#C8/C9:** `std == 0` on a non-constant transform is a contract violation (Phase2ContractError); on `identity_no_zscore` it's a no-op.
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
  "transforms": { "market_price": "log_cents_to_dollars", "btc_realized_vol_15m": "log1p", ... },
  "stats": {
    "market_price": {"mean": -0.054, "std": 0.075, "n_nan": 0, "n_imputed": 0,
                      "p1": -0.13, "p99": 0.00, "median": -0.05, "mad": 0.04},
    "spot_momentum_60s_bps": {"mean": 0.012, "std": 4.7, "n_nan": 0, "n_imputed": 12,
                                "p1": -14.2, "p99": 14.5, "median": 0.0, "mad": 2.8},
    "hour_sin": {"mean": 0.0, "std": 1.0, "n_nan": 0, "n_imputed": 0, "_no_zscore": true},
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

**R2-OPS#C9:** when `--include-sub-floor` is set, `canonical_inputs['asset_floors']` is replaced with the sentinel string `'__sub_floor_included__'` BEFORE `cfg_fp` is computed. The two modes therefore have distinct `cfg_fp` and distinct `train_id`. Also adds `'include_sub_floor': bool` as a top-level key in `canonical_inputs` for redundant clarity.

Re-running with identical inputs produces identical `train_id`. **R2-OPS#C17 — idempotency is at LOGICAL CONTENT level, not bit level.** On-disk parquet bytes may differ across pyarrow versions; re-extract overwrites the parquet inode atomically. Bundle JSON's `parquet_sha256` may differ between two re-extracts of the same `train_id` — by design (logical, not bit, idempotence). Re-running with different inputs creates a new `train_id` directory; old directories coexist until a separate retention/GC job (out of scope).

## Atomic write protocol — REWRITTEN per R3#C1, C2, C3, C16

Order:

1. **mkdir** `data/cal_mlp/<asset>/<train_id>/` (audit JSON lives at the train_id directory root, not in a subdir; R3-stitch#C10)
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

**R2-OPS#C16 (POSIX flock semantics):** `fcntl.flock` is auto-released by the kernel when the holding process exits, including via SIGKILL or OOM-kill. There is NO stale-lock cleanup needed. Operators MUST NOT `rm .extract.lock` to "unstick" a perceived stuck lock — unlinking the file does not release any lock currently held against the inode; a concurrent process re-creating the path can then double-acquire. If a lock appears stuck, identify and kill the holding process (`fuser <path>` or `lsof`), do not delete the file.

**R2-OPS#C11 (writer-starvation alert throttling):** Phase 2 with `LOCK_NB` exits cleanly (Phase2LockError, exit code 4) when a Phase 4 reader is mid-load. Cron should treat exit 4 as "try again next tick", not an alert. Per-cron throttling: if Phase 2 sees code 4 three ticks in a row, emit one Telegram alert (slow reader detected), not three.

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

**Cross-fold ticker constraint:** assert no ticker appears in more than one fold's split (after ticker-disjoint enforcement above). Audit reports `n_tickers_train_only, n_tickers_cal_only, n_tickers_test_only, n_rows_reassigned_at_boundary`.

## Output parquet schema

```
ticker          string                                  -- human-readable (audit/dashboards)
ticker_id       int32                                   -- vocab index (Phase 4 embedding)
is_unk_ticker   int8                                    -- R2-ML#C8: 1 if ticker∉vocab; Phase 4 forces high uncertainty
evaluation_time timestamp[us, UTC]
asset           string
side            string                                  -- 'yes'/'no'
side_int        int8
strategy        string                                  -- 'unknown' if NULL
market_result   string                                  -- 'yes' | 'all_yes' | 'no' | 'all_no'
result_yes_int  int8                                    -- 1 if market_result IN ('yes','all_yes')
outcome         int8                                    -- the target
method_output_raw      float32                          -- raw_prob unclipped (audit only; NOT in CONT_FEATURE_COLS)
logit_raw_prob_clipped float32                          -- the skip-term value Phase 4 reads (R2-ML#C1)
calibrated_prob_audit  float32                          -- bot.py's post-cap value; metadata only
price_tier      int8
stc_bucket      int8
vol_regime_int  int8
split           string                                  -- 'train' | 'cal' | 'test'
fold            int8
[CONT_FEATURE_COLS as float32, post-transform]
[MISSING_INDICATOR_COLS as int8, 0/1]
breakeven_wr_audit     float32                           -- audit only; not in CONT_FEATURE_COLS (R3-stitch#C1)
fee_adjusted_edge_audit float32                          -- audit only; Phase 6 sim_pnl uses for tier replay
kelly_f_audit          float32                           -- audit only
available_balance_cents int64                            -- audit only; Phase 6 uses for sizing
settled_time            timestamp[us, UTC]               -- audit
rowid                   int64                            -- source-table rowid (audit/repro)
```

`is_unk_ticker` is always 0 in Phase 2 outputs (vocab is built from all rows in this extract — every ticker is in vocab by construction). The column exists for Phase 7 inference: bot.py at decision time may encounter a ticker not in the vocab; `is_unk_ticker=1` instructs Phase 4's predict path to inject ensemble-disagreeing init OR force high σ at the calibrator output.

## Bundle JSON (extract_bundle.json)

```json
{
  "phase": 2,
  "schema_version": 2,
  "asset": "SOL",
  "train_id": "2026-04-27T00:00:00.000000Z-abcd1234",
  "cfg_fp": "f3b201e8a7c1d0e9",
  "cutoff_end": "2026-04-27T00:00:00.000000Z",
  "include_sub_floor": false,
  "data_version_at_open": 184321,
  "data_version_at_close": 184321,
  "ticker_vocab_path": "ticker_vocab.json",
  "ticker_vocab_sha256": "...",
  "audit_path": "extract_audit.json",
  "audit_sha256": "...",
  "eval_fold_artifacts": [
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
  "source_total_rows_for_asset": 92334,    // SELECT COUNT(*) WHERE asset=? — full scan, ms-fast at current scale (R2-OPS#C12b: bot.py has no asset index; if scale grows past 1M rows, add index in Phase 7)
  "source_total_rows_post_filter": 92110,
  "drops": {
    "non_15m_product_type": 0,
    "sports_ticker": 0,
    "null_market_price": 0,
    "non_positive_market_price": 0,
    "below_asset_floor": 0,
    "null_raw_prob": 142,
    "null_evaluation_time": 0,
    "non_yes_no_result": 0,
    "null_settled_time": 12,
    "settled_after_cutoff": 8932
  },
  "_drops_invariant": "source_total_rows_for_asset == source_total_rows_post_filter + sum(drops); enforced via Phase2ContractError. Each excluded row is bucketed to the FIRST predicate it fails (sequential pass — see DROP_PREDICATES below).",
  "per_fold": [
    {
      "fold": 0,
      "test_window_start": "...",
      "test_window_end": "...",
      "n_train": 8421, "n_cal": 2103, "n_test": 2087,
      "imputed_pct": {
        "train": {"spot_momentum_60s_bps": 0.03, ...},
        "cal":   {"spot_momentum_60s_bps": 0.04, ...},
        "test":  {"spot_momentum_60s_bps": 0.05, ...}
      },
      "small_cell_warnings": ["cell_(3,2)_n_test=12 <50 floor"],
      "n_rows_reassigned_at_boundary": 487,
      "_per_cell_key_format": "f'({price_tier},{stc_bucket})' — exactly two integers, no whitespace; parser locked to re.fullmatch(r'\\((\\d+),(\\d+)\\)', key) (R3-cnv#C6)",
      "per_cell": {
        "(3,2)": {"n_train": 821, "n_cal": 198, "n_test": 187,
                   "train_positive_rate": 0.94, "cal_positive_rate": 0.93,
                   "test_positive_rate": 0.92,
                   "train_mean_method_output": 0.97}    // R4#C3: train-only; loss-formula source
        ...
      }
    },
    ...
  ],
  "ticker_stats": {
    "n_unique_tickers": 1281,
    "mean_rows_per_ticker": 1.6,
    "pct_tickers_with_only_one_row": 0.72
  },
  "data_version_at_close": 184321,
  "include_sub_floor": false,
  "asset_floor_applied": 88,
  "generated_at": "..."
  // R-p2-spec-r2#H2: dropped from this example (impl does not write):
  //   - top-level `void_rate` (deferred — no Phase-5 ship-blocker actually
  //     reads it; if/when one does, also implement the pre-filter pass that
  //     would source `void_count` and `n_pre_settle_filter` per-cell)
  //   - per_fold.missing_pct_test (subsumed by imputed_pct.test)
  //   - ticker_stats.n_tickers_train_only/cal_only/test_only (informational,
  //     can be reconstructed from per-fold splits)
  // n_rows_reassigned_at_boundary moved INTO per_fold[k] where it was always
  // computed (impl matches the new placement).
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

## Module layout (R2-OPS#C13)

To break the import direction cleanly:

```
cal_mlp/
├── features.py          # constants ONLY: CONT_FEATURE_COLS, CONT_FEATURE_TRANSFORMS,
│                          MISSING_INDICATOR_COLS, PRICE_BIN_CUTOFFS, STC_BIN_CUTOFFS,
│                          ASSET_FLOORS, RAW_PROB_CLIP_EPS. No I/O, no torch.
├── normalize.py         # apply_norm, fit_normstats, transform helpers (logit, log_cents_to_dollars, ...)
│                          Imports features.py only.
├── extract_data.py      # CLI tool. Imports features.py, normalize.py, sqlite3, pyarrow.
├── train.py             # CLI tool. Imports features.py, normalize.py, torch.
├── conformal.py         # Imports features.py, normalize.py, torch.
└── ...
```

No module imports `extract_data.py`. Phase 4/5/6 import `features.py` and `normalize.py` (lightweight) but never the I/O-heavy extract module.

## Schema version contract (R2-OPS#C10)

`schema_version: 2` in bundle. Phase 4/5/6 readers MUST refuse to load bundles where `schema_version != 2` and emit a phase-local `*SchemaError` (defined in each phase's spec), with exit code mirroring Phase 2's exit-6 contract.

Version 1 was the pre-R1 single-file layout (now obsolete). Future v3 will require a same-commit migration script that upgrades v2 bundles in place OR re-extracts from source. No automatic forward/backward compatibility.

## Closed open issues (carried from R1)

- Q1 (NULL `available_balance_cents`): keep rows, mean-impute. Closed (above).
- Q2/Q3 (kelly_f / fee_adjusted_edge leakage): dropped from CONT_FEATURE_COLS, kept as audit columns. Closed.
- Q4 (cutoff_end default): `now - 24h`. Closed.
- Q5 (SOL proximity-to-strike features): added `abs_spot_distance_to_strike_sigma` (regularization prior on symmetry per R2-ML#C6) + `time_decayed_proximity` (with the FIXED 1 - stc/900 weighting per R2-ML#C5). Closed.

## Open issues remaining for Round 3

1. Per-asset feature lists (R2-OPS#C14): currently uniform across BTC/ETH/SOL/XRP. Phase 3 ablation will show whether `time_decayed_proximity` and `abs_spot_distance_to_strike_sigma` degrade non-SOL assets. If yes, add per-asset feature mask in Phase 4. Defer the decision to Phase 3 review; Phase 2 writes the columns regardless (parquet superset).
2. Per-cell historical positive rate as a feature (R2-ML#C7): NOT included in this spec. Revisit in Phase 8 if residual analysis shows per-cell signal the MLP isn't capturing.
