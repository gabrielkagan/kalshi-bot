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

## Three-commit rule

When wiring any engine to CalEngine pipeline, all three must ship in the SAME commit:
1. Engine's INSERT includes `raw_prob`
2. Settlement code routes to the correct CalEngine
3. Audit script checks for CalEngine observations

Splitting these creates silent data gaps. (See `kb/failures/sports-raw-prob-null.md`.)
