# Calibration Pipeline

1. Raw statistical probability (from volatility model)
2. Beta calibration (`CalibrationEngine` — trained on 15M data only, hourly excluded)
3. **Hourly temperature scaling** (Layer 1): T=1.45 softens overconfident probs (95%→88.4%). Applied before OFA/dynamic cap. 15M unaffected.
4. Dynamic cap: **bypassed** when learned calibration is active (`is_learned_method_active()` → uses 0.999 safety ceiling). Cap schedule only applies during startup before training.
5. Market blend: 40% weight toward market price (60% model)
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

`spot_distance_to_strike_sigma` blows up to ±3,000+ at terminal STC (T→0 in denominator). Without clipping, z-scoring across the column inflates std 100×+ and collapses real signal. Fix: `features.SIGMA_WINSOR_ABS_CAP = 25.0`, applied via `features.apply_sigma_winsor(sd)` at THREE sites that must stay in lock-step:

1. **Train** — `extract_data.build_feature_frame` (clips before deriving abs/tdp)
2. **Serve post-hoc** — `post_hoc_processor._process_row` (clips DB-loaded value before predict)
3. **Serve sync gate** — `integration.should_block_tm96` (clips inline-computed value)

Constant lives in `features.py`. cfg_fp captures `sigma_winsor_abs_cap`. Regression tests in `tests/test_calmlp_sigma_winsorize.py` lock that all three serve paths see ≤25.

### Train/serve consistency invariants

The three pipelines use the SAME formulas. Any drift = silent training-distribution mismatch.
- `prob_breakeven_gap = calibrated_prob - market_price/100` (post-CalEngine; bot/_impl.py:1513). Locked by `tests/test_calmlp_tm96_gate.py`.
- `hour_sin/cos` from INTEGER `dt.hour` (NOT minute-fractional). Locked.
- `spot_distance_to_strike_sigma` clipped to ±25. Locked.

### Sub-floor data inclusion

`scripts/cal_mlp/run_pipeline.sh` defaults `INCLUDE_SUB_FLOOR=1` so `low_price_shadow` + `floor_raise_shadow` + `eth_low_floor_shadow` rows (75¢ to MIN_ENTRY-1¢) feed v2/v3 training. v1 was trained without this. Override `INCLUDE_SUB_FLOOR=0` for legacy v1-style cfg_fp.

### Feature-cohort timeline

| Cohort | Activated | Used by |
|---|---|---|
| v1 (8 features) | 2026-02-22 | LIVE bundle, cfg_fp `178d14020bd21beb` |
| v2 (+8 features: momentum, buffer, BTC RV) | 2026-04-19 | K=1 train target 2026-05-19 |
| v3 (+3 features: spread, flow, CB-Kraken gap) | 2026-04-23 | K=2 train target 2026-06-22 |
| External market data (OKX funding+OI, Deribit DVOL) | 2026-04-29 (this commit) | Earliest v3 use 2026-06-22 |

Health monitoring: `scripts/calibrator_feature_health.py` (cron 6h) alerts via Telegram when any feature drops below 99% (or 95% for known-WS-flaky 5m momentum). Schema drift surfaced as `SCHEMA_DRIFT` alert.

External polling: `scripts/external_market_poller.py --once` (cron 1m) writes OKX funding+OI for BTC/ETH/SOL/XRP perpetuals + Deribit BTC/ETH DVOL to `external_market_data` table.

### Spot price buffer persistence (`bot/_impl.py:CoinbaseFeed`)

30-min buffer persists to `state/spot_buffer.json` every 30s; reloads on bot startup. Drops entries >30min old AND >60s in the future. Without this, every bot restart creates a 5-min hole in `spot_momentum_5m_bps` and 30-min hole in `btc_spot_change_30m_bps`. Multiple deploys/day → 25-50% NULL on bad days.

## Commit-bundling rules

### CalEngine wiring (three-commit rule)

When wiring any engine to CalEngine pipeline, all three must ship in the SAME commit:
1. Engine's INSERT includes `raw_prob`
2. Settlement code routes to the correct CalEngine
3. Audit script checks for CalEngine observations

Splitting these creates silent data gaps. (See `kb/failures/sports-raw-prob-null.md`.)

### cal_mlp feature transforms (four-site lock-step)

R-p7-deploy-r11: any change to a feature transform (winsorize cap, hour_sin/cos derivation, prob_breakeven_gap formula, sigma derivation) must ship in ONE commit touching ALL four sites:

1. **Train** — `scripts/cal_mlp/extract_data.py:build_feature_frame`
2. **Serve post-hoc** — `scripts/cal_mlp/post_hoc_processor.py:_process_row`
3. **Serve sync gate** — `scripts/cal_mlp/integration.py:should_block_tm96`
4. **Fingerprint** — `scripts/cal_mlp/features.py:compute_cfg_fp` canonical dict

Splitting any of these creates train/serve skew — model trained on one distribution, served from another. The R3 review caught this exact regression after R2 winsorize landed in extract but not the serve paths.
