# P2 Calibration Project — Design Overview

**Status:** active rebuild after working-tree loss on 2026-04-28.
**Driving incident:** `kb/failures/calibration-pocket-sol-apr27.md` — SOL -$210 single-day loss attributed to ~22pp raw_prob overconfidence bias at moderate z-score, traced in `raw-prob-22pp-bias-rca.md` to two compounding biases: vol blend (RV given 88% weight by MZ; optimal w_eg=0.564, current 0.61× true) and NIG distribution shape (KS p=4.5e-15) adding 5-7pp at moderate z.

## Problem statement

Production raw_prob is a closed-form CDF complement on a chosen distribution (NIG/EGARCH-blended). Postmortem analysis shows it is structurally miscalibrated in identifiable cells — most acutely the bleed cell `(price_tier=3 ≥96¢, stc_bucket=2 300-600s)` where realized win rate diverges from raw_prob by single-digit percentage points. BLR (Bayesian Logistic Regression) was tested as a calibrator but underperforms raw passthrough on Brier (0.0648 vs 0.0634); production runs with `FIFTEEN_M_CALIBRATION_ENABLED=False`.

We want a residual calibrator that:
1. **Reduces error** on the bleed cell without degrading well-calibrated bands.
2. **Quantifies its own uncertainty** so position sizing can pull back when the model is unsure.
3. **Audits cleanly** so we can ship/block/manual-review per band and per cell.

## Architecture

**Per-asset MLP residual calibrator** that takes raw_prob (and supporting features) and produces a calibrated p̂ + standard deviation, wrapped by **Mondrian conformal prediction** to give per-cell `[final_lo, final_hi]` intervals at α=0.20 (80% nominal coverage).

**Ensembling:** M=5 seed-ensemble. Each member is the same architecture trained from a different seed; final p̂ is the ensemble mean and the per-row σ is the ensemble std (with `unbiased=False` since the M members ARE the population for inference).

**Sized via** the bot's existing Kelly tier ladder + drawdown scaler + STC scaler + per-asset MAX_RISK_PER_TRADE caps. The calibrator changes p̂; sizing logic is unchanged.

## Locked architectural decisions

| Decision | Value | Rationale |
|---|---|---|
| Calibrator scope | per-asset (BTC/ETH/SOL/XRP independent) | per-asset CalEngines already in production; preserves data partitioning |
| Architecture | small MLP (residual on raw_prob) | linear/BLR underperforms; deeper risks overfit on n~10k cohorts |
| Ensemble size | M=5 seed-ensemble | replaces single model + MC dropout; std subsumes Phase 6 MC need |
| Conformal | Mondrian, absolute residual `s = |p̂ - y|` | per-cell quantile is the natural unit for ship-blockers |
| α (coverage target) | 0.20 → 80% intervals | tight enough to gate sizing, wide enough to stay finite on small cells |
| Bins (price) | cutoffs `[80, 90, 96]` → 4 tiers | matches MIN_ENTRY_PRICE_BY_PRICE schedule kink points |
| Bins (STC) | cutoffs `[120, 300, 600]` → 4 buckets | matches A1/A2/decided_t1/decided_t2 phase boundaries |
| Vol regime | binary `elevated` vs other | bot.py's existing regime classifier output |
| Bleed cell | `price_tier=3 AND stc_bucket=2` | (≥96¢, 300-600s) — the documented underperformance hotspot |
| Folds | K=3 walk-forward, 30-day windows | three months of post-WS-fix data; rolling origin |
| Doc-drift rule | mirror constants in cal_mlp + assert at bot.py startup | A53-locked sizing tiers + A28-locked edge schedule |

## Phase numbering

The original session's KB content used phases 2-8. After this rebuild:

- **Phase 2 — Data extraction** (`extract_data.py`): pulls per-asset rows from `evaluated_opportunities`, applies regime filter, builds K-fold parquet outputs with normstats.
- **Phase 3 — Architecture spec** (`p2-phase3-mlp-architecture.md`): no impl file; declares CalibrationMLP shape, loss, optimizer.
- **Phase 4 — Training** (`train.py`): M=5 seed-ensemble training loop, atomic 3-step bundle write protocol, eval_fold_artifacts.
- **Phase 5 — Conformal wrapper** (`conformal.py` + `_helpers.py`): per-cell residual quantiles, bleed-cell collapse-by-merge, market_blend_w precedence chain, predict_with_interval.
- **Phase 6 — Validation harness** (`validate.py` + `sim_pnl.py` + `sizing.py` + `stats.py`): per-band Brier with cluster-bootstrap CI, per-cell coverage with Wilson CI, sim PnL replay (dual block_off/block_on) with full live gate replication, ship-blocker decision tree.
- **Phase 7 — Deploy preconditions** (bot.py amendments + `market_config.py` helpers + schema migration): startup parity assert, single-source-of-truth helpers, DB column adds.

## Design history (what changed mid-project)

These are amendments to the original spec that emerged during adversarial review and external feedback (2026-04-27):

- **Noise injection dropped → audit-only.** Adding noise to raw_prob during training was proposed as regularization; one-hot+continuous redundancy makes feature perturbation inconsistent (perturbing one but not the other breaks invariants). Kept as an audit knob, not a training mode. (`p2-feedback-2026-04-27.md`)
- **MC dropout dropped → ensemble std subsumes.** Originally Phase 6 was going to use MC dropout sampling for uncertainty; M=5 ensemble already provides per-row σ; the second sampling layer is redundant.
- **VAE for OOD detection → deferred.** Variational autoencoder on input features was considered for OOD flagging; doesn't address the dominant 96¢ × 2-5min miss (which is a missing feature, not OOD). Revisit if OOD becomes load-bearing.
- **Phase 5 must wrap a `Predictor` interface.** SinglePredictor and EnsemblePredictor share a `.predict(batch) → (p, p_std)` contract so Phase 6 can A/B M=1 vs M=5 without touching downstream code.

## Doc-drift safety

Constants that exist in both bot.py and cal_mlp/* MUST be updated in the same commit; bot.py runs a startup parity assert against vendored copies. Specifically:
- `SIZING_TIERS` (config.py:134 ↔ sizing.py)
- `MIN_EDGE_BY_PRICE` (bot.py:1180 ↔ sim_pnl.py)
- `WEEKEND_EDGE_DISCOUNT`, `OVERNIGHT_EDGE_DISCOUNT`, `WEEKEND_EDGE_FLOOR`, hour bounds (bot.py:853-864 ↔ sim_pnl.py)
- `HIGH_PRICE_STC_BLOCK_*` (bot.py:1213-1226 ↔ sim_pnl.py)
- `STC_EXTENDED_*` per-asset floors (bot.py:238-244 ↔ sim_pnl.py)
- `*_MIN_ENTRY_PRICE` per-asset (bot.py:219-225 ↔ sim_pnl.py)
- `*_MAX_RISK_PER_TRADE` per-asset (bot.py:226-229 ↔ sizing.py)
- `STC_SIZING_SCALER_*` (bot.py:897-898 ↔ sizing.py)

## Reconstruction status (2026-04-28 EOD)

| Phase | Spec | Impl | Status |
|---|---|---|---|
| 2 | `p2-phase2-data-extraction.md` (R16 doc-aligned) | `extract_data.py` + `features.py` + `normalize.py` | CONVERGED |
| 3 | `p2-phase3-mlp-architecture.md` (R16 doc-aligned) | (model lives in `train.py`) | CONVERGED |
| 4 | (in this design doc) | `train.py` (R9 + walk-forward assert wired) | CONVERGED |
| 5 | `p2-phase5-conformal.md` (R2 spec-aligned) | `conformal.py` + `_helpers.py` (R9 + DRY chain home) | CONVERGED |
| 6 | (in this design doc) | `validate.py` + `sim_pnl.py` + `sizing.py` + `stats.py` (R11 + normstats unwrap + vol_regime_int + lock domain) | CONVERGED |
| 7 | `p2-phase7-deploy.md` (R8 cross-spec) + `p2-phase7-bot-py-diff.md` (R12 operator-deploy) | `integration.py` (R8 final hardening — predict() exception wrapping, snapshot pattern, env-first ordering, force-overwrite indicators, module-load inverse-map check, Edit 3b always-construct + warmup-gated for hot env flip) | CONVERGED — bot.py edits documented but NOT applied (gated on operator authorization per CLAUDE.md "bot.py is sacred") |
| 8 | none yet | none | DEFERRED (May A/B bake-off — see MEMORY.md `project_p2_design_amendment_apr27.md`) |

**Key load-bearing facts (locked):**
- bundle_sha chain producer/consumer byte-equivalent at R8: `phase4 = sha256(model_identity_sha256 : normstats_concat_sha256 : 'phase4')` where `model_identity_sha256` aggregates ONLY the deploy fold's member SHAs (sorted asc, joined by ':') and `normstats_concat_sha256` hashes the per-file SHA hex strings from `eval_fold_artifacts[].normstats_sha256`. `phase5 = sha256(phase4_bundle_sha : conformal_sha256)`. Single source of truth: `_helpers.verify_bundle_sha_chain`.
- 12 SKIPPED_REASONS frozenset, 17 `_check()` parity calls (DRAWDOWN_HALT_FLOOR + STC_SIZING_SCALER_ENABLED added at R12).
- Kill switch contract (R-p7-coldboot#C-S2): predictor INSTANCES always constructed at boot; `.warmup()` gated on `CALMLP_ENABLED`. Hot env flip activates calibration on first scan tick after the flip.
- Bundle CURRENT swap requires `systemctl restart kalshi-bot` (R-p7-coldboot#C-S3) — no in-process reload mechanism by design.

**Operator next step:** apply the 4 bot.py edits per `kb-research/bot/p2-phase7-bot-py-diff.md` and deploy with `CALMLP_ENABLED=0` for shadow soak.
