# cal_mlp v1 baseline corpus — design

Phase 2 P2.1.a-1 TDD anchor. Pins the v1 (2026-04-28 CURRENT) predictor's exact
output for each of BTC/ETH/SOL/XRP against a fixed 6-row stratified synthetic
corpus.

## Corpus design

6 rows per asset, deterministic synthetic features stratified by price-tier ×
STC-tier:

| Row | price_cents | seconds_to_close | sigma | hour | rationale |
|---|---|---|---|---|---|
| 1 | 60 | 720 | 0.5 | 12 | low-price (<80c, tier 0), 12min, near-strike, noon |
| 2 | 75 | 360 | 1.2 | 0 | mid-low (<80c, tier 0), 6min, mid-sigma, midnight UTC |
| 3 | 85 | 600 | 0.8 | 18 | mid-high (80-90c, tier 1), 10min, near-strike, evening |
| 4 | 92 | 180 | 2.0 | 6 | high (90-96c, tier 2), 3min, far-sigma, morning |
| 5 | 97 | 120 | 0.3 | 14 | very-high (96c+, tier 3), 2min, near-strike, afternoon |
| 6 | 99 | 60 | 0.1 | 9 | terminal (96c+, tier 3), 1min, very-near, morning |

Price tiers per `features.PRICE_BIN_CUTOFFS = [80, 90, 96]` (right=True
digitize).
STC tiers per `features.STC_BIN_CUTOFFS = [120, 300, 600]` (right=True
digitize).

raw_prob per row is a fixed value selected to be realistic given the price
(roughly tracking market with a small edge — see `_synthesize_corpus` in the
capture script).

Derived features. Canonical helper homes per `bot/CLAUDE.md` § cal_mlp
feature transforms (lock-step) and `agent_docs/calibration_pipeline.md`
§ "cal_mlp feature transforms (lock-step)":

- `spot_distance_to_strike_sigma` + `prob_breakeven_gap`:
  `bot/helpers/derived_features.py::compute_derived_features`
- `hour_sin` / `hour_cos`:
  `bot/helpers/derived_features.py::compute_hour_sin_cos`
  (mirrored DataFrame-side by `scripts/cal_mlp/features.compute_hour_features`)

For self-containment of the pinned corpus, `capture_baseline.py::_build_row_features`
inlines the formulas rather than importing the canonical helpers. **This is a
deliberate isolation pattern**: any future drift in the canonical helpers
would change the v1.1 predict path while the pinned corpus stays fixed —
the predict-output snapshot test catches the drift loudly. Inverse drift
(corpus-script formulas drifting from helpers) is caught by the lock-step
contract test `tests/contracts/test_calmlp_lockstep.py`.

Formulas (inline mirrors of the canonical helpers):

- `abs_spot_distance_to_strike_sigma = abs(spot_distance_to_strike_sigma)`
- `time_decayed_proximity = sigma * (1 - stc/900)` (engineered, ~v1 spec; the
  serve-path `scripts/cal_mlp/integration.py:1424` additionally clamps the
  decay factor to `[0, 1]` for `stc > 900`. This corpus uses `stc ≤ 720`, so
  the clamp is inert and pinned values are bit-identical.)
- `prob_breakeven_gap = raw_prob - price_cents/100`
- `hour_sin = sin(2π·hour/24)`, `hour_cos = cos(2π·hour/24)`

## Snapshot files

Per asset: `cal_mlp_v1_baseline_<asset>.json`. Schema:

```json
{
  "asset": "BTC",
  "train_id": "2026-04-28T11:50:29.671752Z-8acc233e",
  "cfg_fp": "178d14020bd21beb",
  "captured_at": "2026-05-13T...Z",
  "rows": [
    {
      "row_id": 1,
      "inputs": {"raw_prob": ..., "price_cents": ..., "side": "yes", "row_features": {...}},
      "outputs": {"cal_prob": ..., "ens_std": ..., "final_lo": ..., "final_hi": ...}
    },
    ...
  ]
}
```

## REGEN PROTOCOL — human-only

Same discipline as `tests/equivalence/REGEN.md`: snapshots are NEVER
auto-regenerated.

If the test fails because v1 predict output drifted, the answer is
**investigate the divergence**, not regenerate:

- Did torch / numpy / scipy minor versions drift on the local machine?
- Did `scripts/cal_mlp/features.py` change a feature transform without a v1.1
  KB doc?
- Did `scripts/cal_mlp/integration.py` change the predict path?
- Did `scripts/cal_mlp/conformal.py` change conformal arithmetic?

To legitimately regenerate (e.g., after a deliberate engine-math change that
ships with a KB decision doc):

```bash
python3 tests/integration/cal_mlp_v1_baseline/capture_baseline.py --regen
git diff tests/integration/cal_mlp_v1_baseline/
# Diff every changed value by hand. If the magnitude of the drift cannot be
# explained from the KB doc, STOP — the regen is capturing unintended drift.
git add tests/integration/cal_mlp_v1_baseline/
git commit  # include the KB-doc link in the commit message
```

The capture script refuses to overwrite existing snapshots without `--regen`.

## Out-of-scope (next P2 sub-Bits will address)

- HYPE / DOGE baseline (no v1 bundle exists for these assets; v1.1 ships the
  first calibrator)
- Real-traffic Brier / ECE / regime-stratified WR baseline against state.db
  rows (this corpus is synthetic; the validation gate also needs a corpus
  drawn from production data)
- Cell-block sunset audit baseline (a separate Bit will pin the per-cell-block
  cal_prob × edge distribution in historically-blocked rows)
