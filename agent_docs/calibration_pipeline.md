# Calibration Pipeline

1. Raw statistical probability (from volatility model)
2. Beta calibration (`CalibrationEngine` — trained on 15M data only, hourly excluded)
3. **Hourly temperature scaling** (Layer 1): T=1.45 softens overconfident probs (95%→88.4%). Applied before OFA/dynamic cap. 15M unaffected.
4. Dynamic cap: **bypassed** when learned calibration is active (`is_learned_method_active()` → uses 0.999 safety ceiling). Cap schedule only applies during startup before training.
5. Market blend: per-asset 15M weights `MARKET_BLEND_W_BY_ASSET = {BTC:0.10,DOGE:0.60,ETH:0.20,HYPE:0.80,SOL:0.80,XRP:0.90}` (P2.1.d 2026-05-13 + P2.3 HYPE/DOGE live promotion 2026-05-14); unknown assets + non-15M paths fall back to the scalar MARKET_BLEND_W = 0.40 (legacy 60/40 blend)
6. Fee-adjusted edge check: price-dependent minimum (0.25% at 80-90c up to 1.0% at 97c+)

## Hourly Three-Layer Optimization

All run under `HOURLY_OBSERVATION_ONLY = not HOURLY_LIVE_ENABLED` — observation gate is the shadow mechanism. Filters placed LATE in pipeline so all upstream data is still logged for counterfactual analysis.

| Layer | Filter | Purpose |
|---|---|---|
| 1 | Temperature scaling (T=1.45) | Softens 15M calibration that doesn't transfer to hourly |
| 2 | STC timing (120-3600s) | Expanded for observation data collection |
| 3a | Asset exclusion (disabled) | Disabled in observation mode — collecting all asset data |
| 3b | Per-window position limit (2) | ENB ~1.3 independent bets per window |
| 3c | Per-window risk cap (15%) | Prevents correlated multi-asset blowups |
| 3d | Quarter-Kelly sizing | 44% of growth rate, ~3% halving probability |

## Per-engine CalEngines

- **15M** — Beta-calibrated, 15M data only
- **Hourly** — disabled (passthrough + T=1.45)
- **SPX-D** — separate engine for SPX hourly
- **Weather** — per-city CalEngines (shadow learning)
- **Sports** — per-sport-group CalEngines (shadow learning)

## P2 cal_mlp pipeline (R-p7-deploy-r11, 2026-04-29)

Per-asset M=5 ensemble residual calibrator + Mondrian conformal. v1 LIVE shadow-only since `69c1ddc`. See `kb/decisions/p2-cal-mlp-v1v2v3-retraining-plan.md` for the v1/v2/v3 phased plan.

### Sigma winsorize (R-p7-deploy-r11)

`spot_distance_to_strike_sigma` blows up to ±3,000+ at terminal STC (T→0 in denominator). Without clipping, z-scoring across the column inflates std 100×+ and collapses real signal. Fix: `features.SIGMA_WINSOR_ABS_CAP = 25.0` applied via `features.apply_sigma_winsor(sd)`. The canonical enumeration of all surfaces (including sister-script drift sites surfaced in 2026-05-12 R2 adv review) is below in **"cal_mlp feature transforms (lock-step)"**. Cross-reference that section as the source of truth.

Constant lives in `features.py`. cfg_fp captures `sigma_winsor_abs_cap`. Regression tests in `tests/integration/test_calmlp_sigma_winsorize.py` lock that all serve paths see ≤25; cross-site AST + runtime parity guard in `tests/contracts/test_calmlp_lockstep.py` (Sprint A.1a 2026-05-12).

### Train/serve consistency invariants

The three pipelines use the SAME formulas. Any drift = silent training-distribution mismatch.
- `prob_breakeven_gap = calibrated_prob - market_price/100` (post-CalEngine; `bot/helpers/derived_features.py::compute_derived_features`). Locked by `tests/integration/test_calmlp_tm96_gate.py`.
- `hour_sin/cos` from INTEGER `dt.hour` (NOT minute-fractional). Locked.
- `spot_distance_to_strike_sigma` clipped to ±25. Locked.

### Sub-floor data inclusion

`scripts/cal_mlp/run_pipeline.sh` defaults `INCLUDE_SUB_FLOOR=1` so `low_price_shadow` + `floor_raise_shadow` + `eth_low_floor_shadow` rows (75¢ to MIN_ENTRY-1¢) feed v2/v3 training. v1 was trained without this. Override `INCLUDE_SUB_FLOOR=0` for legacy v1-style cfg_fp.

### Feature-cohort timeline

| Cohort | Activated | Used by |
|---|---|---|
| v1 (8 features) | features active since 2026-02-22; bundles trained 2026-04-28 | Prior LIVE bundle, cfg_fp `178d14020bd21beb` (commit `7122693`). Superseded by v1.1 on 2026-05-13 via P2.1.d. |
| v1.1 (same 8 features; adds `sigma_winsor_abs_cap` to canonical dict) | Recipe shipped 2026-04-29 commit `7ad2464`; retrained 2026-05-12 (P2.1.b); LIVE 2026-05-13 (P2.1.d) | **CURRENT LIVE** bundle for BTC/ETH/SOL/XRP, cfg_fp `345978797274721f`. Shipped atomically with per-asset `MARKET_BLEND_W_BY_ASSET = {BTC:0.10,DOGE:0.60,ETH:0.20,HYPE:0.80,SOL:0.80,XRP:0.90}` (canonical doc-drift form) from the P2.1.c-fu1 4×6 sim-PnL sweep + P2.3 2026-05-14 (`86b9xv66a`) HYPE/DOGE B.1 Brier sweep extension. Money Printer Roadmap Phase 2 (`86b9wuhhr` / `86b9xfwkg` / `86b9xv66a`). |
| v2 (+8 features: momentum, buffer, BTC RV) | 2026-04-19 | K=1 train target 2026-05-19 |
| v3 (+3 features: spread, flow, CB-Kraken gap) | 2026-04-23 | K=2 train target 2026-06-22 |
| External market data (OKX funding+OI, Deribit DVOL) | 2026-04-29 (commit `7ad2464`) | Earliest v3 use 2026-06-22 |

The v1 → v1.1 cfg_fp delta is documented in `scripts/cal_mlp/features.py::compute_cfg_fp` docstring (the "Known cfg_fp values" section) and pinned in `tests/contracts/test_calmlp_lockstep.py` anchor 5. The 2026-05-03 unpromoted candidates on VPS (cfg_fp `1969b12c6c0c39bf`) are the `--include-sub-floor --provenance-filter=full_dataset` ablation arm of the same recipe per `kb/decisions/v2-cal-mlp-deploy-runbook-may03.md` — not a distinct feature cohort.

Health monitoring: `scripts/audit/calibrator_feature_health.py` (cron 6h) alerts via Telegram when any feature drops below 99% (or 95% for known-WS-flaky 5m momentum). Schema drift surfaced as `SCHEMA_DRIFT` alert.

External polling: `scripts/backfill/external_market_poller.py --once` is a CRON-NEVER-INSTALLED script (greenfield at `7ad2464` 2026-04-29; ClickUp 86b9vrr8q RCA 2026-05-11) — `external_market_data` table does NOT exist on VPS. The script targets OKX funding+OI for all 7 assets in `config.ASSETS` (T1.5 bf8b9a3 added HYPE+DOGE 2026-05-10; T1.5 ticket 86b9zmj15 added BNB 2026-05-17 to its `FUNDING_SYMBOLS`/`OI_SYMBOLS` lists) + Deribit BTC/ETH DVOL, but is operationally dormant. The load-bearing writer for `evaluated_opportunities.okx_funding_rate_at_decision` + `deribit_funding_rate_at_decision` is the Phase G-5 backfill in `scripts/backfill/shadow_coverage_backfill.py` — also currently uninstalled-on-VPS (separate P1 G-5 ticket to install daily cron; population dropped 80% → 0% on 2026-05-03, 17.5K NULL rows accumulated).

### Spot price buffer persistence (`bot/feeds/coinbase.py::CoinbaseFeed`)

30-min buffer persists to `state/spot_buffer.json` every 30s; reloads on bot startup. Drops entries >30min old AND >60s in the future. Without this, every bot restart creates a 5-min hole in `spot_momentum_5m_bps` and 30-min hole in `btc_spot_change_30m_bps`. Multiple deploys/day → 25-50% NULL on bad days.

## Commit-bundling rules

### CalEngine wiring (three-commit rule)

When wiring any engine to CalEngine pipeline, all three must ship in the SAME commit:
1. Engine's INSERT includes `raw_prob`
2. Settlement code routes to the correct CalEngine
3. Audit script checks for CalEngine observations

Splitting these creates silent data gaps.

### cal_mlp feature transforms (lock-step)

R-p7-deploy-r11: any change to a feature transform (winsorize cap, hour_sin/cos derivation, prob_breakeven_gap formula, sigma derivation) must ship in ONE commit keeping all surfaces in lock-step. RCA refresh 2026-05-12 (Sprint A.1a, ticket `86b9vejnq`) revised the surface from the historical "four-site" framing; **Sprint A.1b (ticket `86b9veppa`, 2026-05-12) closed the inline-drift surface** by routing all tracked cal_mlp sites through canonical helpers. bot/CLAUDE.md "cal_mlp feature transforms (lock-step)" is the source of truth — this section mirrors it.

**Drift surface — 4 tracked drift sites + 1 helper home (post-A.1b: all 4 call canonical helpers):**

Drift sites (pinned by `HOUR_SINCOS_DRIFT_SITES` in `tests/contracts/test_calmlp_lockstep.py`):
1. **Train** — `scripts/cal_mlp/extract_data.py:build_feature_frame` (`hour_sin/cos` via `features.compute_hour_features`)
2. **Serve post-hoc** — `scripts/cal_mlp/post_hoc_processor.py:_process_row` (`hour_sin/cos` via `features.compute_hour_features`)
3. **Serve sync gate** — `scripts/cal_mlp/integration.py:should_block_tm96` (`hour_sin/cos` via `features.compute_hour_features`; sigma + breakeven via `bot.helpers.derived_features.compute_derived_features` + `features.apply_sigma_winsor`)
4. **Train sim-PnL** — `scripts/cal_mlp/sim_pnl.py` (`hour_sin/cos` via `features.compute_hour_features`; sister site added during A.1a R2 adv-review 2026-05-12)

Helper home (not a drift site — owning the formula IS the canonical change vehicle):
- **Fingerprint** — `scripts/cal_mlp/features.py:compute_cfg_fp` canonical dict + `SIGMA_WINSOR_ABS_CAP` constant + `apply_sigma_winsor` + `compute_hour_features` helper home

Untracked dev artifacts (NOT in the AST-guard surface; ticket `86b9wjd3e` pending track-or-delete):
- `scripts/cal_mlp/backfill_offline.py`
- `scripts/cal_mlp/mac_diagnostics/v2_live_audit/score_live_ws.py`

**Canonical helper homes:**
- `bot/helpers/derived_features.py::compute_derived_features` owns `spot_distance_to_strike_sigma` + `prob_breakeven_gap`. Extracted in Bit 3.2 (2026-05-08); allowed by `.importlinter` Contract 4 (helpers-leaf). A.1b (2026-05-12) routed `scripts/cal_mlp/integration.py` inline formulas through this helper.
- `bot/helpers/derived_features.py::compute_hour_sin_cos` — scalar hour-of-day cyclic encoding (Bit B.1a, 2026-05-12). Mirrored by `scripts/cal_mlp/features.compute_hour_features` (A.1b) which additionally accepts a numpy/pandas Series for DataFrame-side extract paths.

**Helper-call sites (preserve when editing):** `bot/state.py:1713` + `:2010` (pre-DB-write `compute_derived_features` calls + `:1723` `apply_sigma_winsor` on the returned sigma) + `bot/engines/sports_engine.py` (2 sites) + `scripts/backfill/backfill_extended_features.py` (evaluated_opportunities pre-B.1a Tier 4/5 backfill) + `scripts/backfill/wave1_derived_cols.py` (B.1a-fu2 2026-05-12: rejected_opportunities Wave 1 + evaluated_opportunities prob_breakeven_gap backfill) + `scripts/backfill/hype_doge_replay_backfill.py` (Phase 2 replay backfill, 2026-05-12, ticket `86b9wy7v3`: per-market `replay_market()` calls helpers for `hour_sin`/`hour_cos`/`sigma_winsorize` on `historical_replay_calmlp` rows. `prob_breakeven_gap` honest-NULL in v1 — no historical Kalshi orderbook — but the helper IS called with `market_price_cents=None` to preserve the lock-step call shape) + `scripts/cal_mlp/integration.py` (serve-path `should_block_tm96`, post-A.1b).

Splitting any of these creates train/serve skew — model trained on one distribution, served from another. The R3 review of A.1a caught this exact regression after R2 winsorize landed in extract but not the serve paths. Cross-site AST + runtime parity guard: `tests/contracts/test_calmlp_lockstep.py` (Sprint A.1a 2026-05-12 + A.1b 2026-05-12).

## Recipe namespace dispatch (P2.1.a-3-fu1, 2026-05-13, ticket `86b9xbd2u`)

Extract bundles produced under different recipes route through one
dispatch helper: `scripts/cal_mlp/features.py::resolve_recipe(namespace) ->
RecipeSpec`. Pinned by `tests/contracts/test_p2_1_a_3_fu1_recipe_dispatch.py`.

**Recipe namespaces** (canonical labels — adding a new one bumps cfg_fp
and requires a sister anchor in the dispatch test):

| Namespace | CONT_FEATURE_COLS | Assets | cfg_fp |
|---|---|---|---|
| `v1.1_production` | 8 (incl. market_price, prob_breakeven_gap) | CORE: BTC/ETH/SOL/XRP (baked into cfg_fp) + EXT: HYPE/DOGE/... (NOT in cfg_fp; extensible per Bit C 86ba0jn2b 2026-05-19) | `345978797274721f` (default flags) / `1969b12c6c0c39bf` (ablation) |
| `replay_v1` | 4 (no market_price, no prob_breakeven_gap, no seconds_to_close, no time_decayed_proximity) | HYPE/DOGE | `9347942aaba71146` |

**CORE vs EXT split in `v1.1_production`** (Bit C, `86ba0jn2b`, 2026-05-19):
- `features.ASSET_FLOORS` (CORE) = `{BTC: 88, ETH: 90, SOL: 86, XRP: 92}`.
  Frozen. Membership baked into `compute_cfg_fp()` canonical dict. Changing this rotates cfg_fp and invalidates all production bundles.
- `features.ASSET_FLOORS_EXT` (EXTENSION) = `{HYPE: 75, DOGE: 75, ...}`.
  Extensible. Membership NOT in cfg_fp — adding a new Kalshi crypto rollout (BNB next; future SHIB/ADA/etc.) is a 1-line edit with no cfg_fp rotation, no production-bundle invalidation.
- `resolve_recipe('v1.1_production').asset_floors` returns the UNION
  (CORE ∪ EXT). `train.py:693`'s membership guard reads the union;
  `compute_cfg_fp()` reads CORE only.
- Lifecycle: EXT → CORE migration is triggered by the asset's cal_mlp
  v1.1 bundle SHIPPING to production (the `CURRENT` pointer at
  `models/cal_mlp_<ASSET>/` flips to a v1.1_production-recipe bundle
  that the bot consumes at serve time). T4-promotion in the BOT (live
  trading via `raw_prob × MARKET_BLEND_W`) is INDEPENDENT — HYPE and
  DOGE are already T4-promoted in the bot (2026-05-14) but have no
  live cal_mlp v1.1 bundle, so they stay in EXT. When the asset's
  cal_mlp v1.1 bundle ships (umbrella `86ba0jmyq` Bit D gate for
  HYPE/DOGE), MOVE the entry `ASSET_FLOORS_EXT[ASSET] →
  ASSET_FLOORS[ASSET]` in the SAME commit (this IS the cfg_fp rotation
  event; ship a re-extract+re-train of all 4+ production bundles).
- Pinned by `tests/contracts/test_asset_floors_ext_extensibility.py`
  (11 tests including a monkeypatched-BNB-addition no-rotation case).

**Bundle stamping convention:**
- `extract_data_replay.py` ALWAYS stamps `recipe_namespace='replay_v1'`
  in the produced `extract_bundle.json`.
- `extract_data.py` (production) currently does NOT stamp the field;
  consumers default-to-`v1.1_production` on absent key for back-compat
  with already-shipped production bundles. `resolve_recipe(None)` and
  `resolve_recipe('v1.1_production')` resolve identically.

**Consumer wiring** (post-fu1):
- `train.py`, `validate.py`, `conformal.py`: argparse `--asset` choices
  widened to `['BTC','ETH','SOL','XRP','HYPE','DOGE','BNB']` (BNB added
  2026-05-17 with T1 shadow onboarding, ticket 86b9zmj0c — kept in lock-step
  with `bot.config.ASSETS` so cal_mlp scripts don't drift behind activation).
- `train.py`, `validate.py`: load `ext_bundle['recipe_namespace']`,
  resolve recipe, hard-fail if `--asset` ∉ `recipe.asset_floors`
  (catches `--asset HYPE` paired with production bundle and vice versa).
- `train.py`: `Phase4Dataset(...)` + `apply_norm(...)` + `model_def`
  size receive recipe-derived `cont_feature_cols` /
  `cont_feature_transforms` / `missing_indicator_cols` /
  input_continuous_dim — not module-globals.
- `train.py`: `model_def` stamps `recipe_namespace`. `phase1b_verify_eth_v2.py::EXPECTED_MODEL_DEF_FIELDS`
  pins `recipe_namespace: "v1.1_production"` as the 17th field (added
  P2.1.d, 2026-05-13, ClickUp `86b9xd9pp`, atomically with the v1.1 ETH
  bundle retarget — `V2_ETH_TRAIN_ID` bumped to
  `2026-05-12T11:59:31.654442Z-b6eb2704`, `V2_ETH_EXPECTED_CFG_FP` bumped to
  `345978797274721f`, `EXPECTED_N_VOCAB` bumped to 2876). The variable
  names retain the `V2_*` prefix as legacy but encode v1.1 values per
  the in-place-bump path.
- `validate.py`: replay-namespace bundles SKIP the sim_pnl
  counterfactual replay block. Replay corpus rows live in
  `data/replay/state.db::historical_replay_calmlp`, not the production
  `state.db::evaluated_opportunities` table that `run_sim_pnl` reads.
  The skip emits a documented `sim_pnl_skipped` marker into the audit
  JSON AND the operator-facing markdown report (sections §3 + §4
  render `_skipped_` rather than misleading `$0.00` defaults). Brier
  (§1) + coverage (§2) remain authoritative.
- `train.py::Phase4Dataset` + `train.py::run` preds_df concat +
  `validate.py::CalibrationDataset` (P2.1.a-3-fu2, 2026-05-13, ticket
  `86b9xd9hn`): receive recipe-derived `categorical_feature_cols` —
  production lists the full 4-tuple `('price_tier','stc_bucket',
  'vol_regime_int','side_int')`; replay lists only the 2 columns the
  parquet structurally has `('stc_bucket','side_int')`. Absent
  categoricals default to int64 zeros at construction → degenerate
  one-hots `[1,0,0,0]` and `[1,0]` on those axes (model trains on the
  collapsed signal; ticker embedding + cont features are the
  non-degenerate trainable surface).
- `validate.py::empirical_coverage` (P2.1.a-3-fu3, 2026-05-13, ticket
  `86b9xe3ku`): accepts keyword-only `recipe=` and short-circuits the
  per-row reads of `price_tier`/`vol_regime_int`/`market_price`/`side`
  to neutral defaults (`0`/`0`/`50¢`/`'yes'`) when
  `recipe.namespace == REPLAY_RECIPE_NAMESPACE`. Production-recipe and
  `recipe=None` preserve the legacy literal-lookup behavior — absent
  columns on a BTC/ETH/SOL/XRP run still surface as `KeyError` rather
  than silently defaulting. Replay-mode with `market_blend_w != 0`
  raises `SystemExit` at function entry — the neutral-50¢ breakeven
  only cancels the blend term when `w=0` and `validate.main`'s blend
  resolution stack can otherwise pick up a non-zero scalar for
  HYPE/DOGE silently. Pinned by
  `tests/contracts/test_p2_1_a_3_fu3_validate_replay_tolerance.py`
  (8 anchors: functional regression + nonzero-blend hard-fail +
  zero-blend success + keyword-only kwarg + production no-regression +
  production-still-raises-on-missing-market_price + AST kwarg-presence
  guard + AST Name-identifier value guard).

**P2.1.b enablement gap** (CLOSED by fu2+fu3, 2026-05-13): at fu1 ship
time, `Phase4Dataset.__getitem__` unconditionally read `df['price_tier']`
+ `df['vol_regime_int']` categorical columns, and
`validate.py::empirical_coverage` made a parallel set of per-row reads
including `df['market_price']` + `df['side']`. Replay parquets lack all
four columns (no market_price → no price_tier digitization; no vol
regime feed for HYPE/DOGE in replay backfill; no orderbook for
market_price; only `side_int=1` hardcoded YES). fu2 (`86b9xd9hn`,
SHIPPED `b566ae9`) closed the train.py categorical-FE side via
`RecipeSpec.categorical_feature_cols`. fu3 (`86b9xe3ku`, this commit)
closed the validate.py empirical_coverage row-iter side via the
keyword-only `recipe=` kwarg. The validate.py row-iter dispatch surface
for HYPE/DOGE replay bundles is now closed; P2.3.b (HYPE/DOGE validation)
becomes runnable once P2.3.a's HYPE/DOGE bundle exists (P2.3.a remains
blocked on `86b9xednb` — HYPE/DOGE replay corpus proxy).
