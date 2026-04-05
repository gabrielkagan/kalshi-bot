---
status: decided
updated: 2026-03-29
tags: [decision, blr, calibration]
date: 2026-03-29
---
# Decision: Disable BLR Calibrator

Date: 2026-03-29
Status: Decided

## Context
The Bayesian Logistic Regression (BLR) calibrator was discovered to have collapsed weights (w=0.1145) on March 25, 2026. It was outputting ~95% probability regardless of input, destroying edge discrimination. The bot was still profitable because high-price crypto contracts settle YES frequently, but the calibrator was adding noise rather than signal.

## Options Considered
1. **Fix and retrain BLR** — Reset weights, improve training pipeline, add monitoring
   - Risk: BLR could silently degrade again
   - Complexity: Need to understand why weights collapsed in the first place
2. **Switch to a different calibrator** — Use isotonic regression, Platt scaling, or A2 LightGBM
   - Risk: Any learned calibrator can fail silently
   - The A2 LightGBM evaluation showed it was a price proxy with no independent signal
3. **Disable calibration entirely (passthrough)** — Use raw EGARCH probabilities directly
   - Simple, transparent, no hidden state to collapse
   - Raw probabilities are well-calibrated at high prices where the bot trades

## Decision
Disable BLR calibration with `FIFTEEN_M_CALIBRATION_ENABLED = False`. Raw EGARCH probabilities used directly (passthrough mode).

## Consequences
- **Positive:** Bot compounded from ~$660 to ~$1,400 over five weeks at 92%+ WR after the switch
- **Positive:** Edge discrimination restored — the bot can now distinguish between 90% and 95% probability contracts
- **Positive:** Simpler system with fewer failure modes
- **Negative:** No learned correction for any systematic bias in raw probabilities. April 2026 analysis confirmed overnight (04-11 UTC) underconfidence of 7pp at 91-92c — BLR would fix this but hurts global Brier marginally. See kb-research/bot/overnight-miscalibration-analysis.md.
- **Monitoring:** BLR diagnostic logging kept (`BLR_BYPASS: raw=X passthrough=Y blr_would=Z`) to track what BLR would have produced

The CalibrationEngine infrastructure remains in code. Per-product CalEngines (hourly, SPX, weather cities, sports groups) continue learning in shadow. If a future calibrator demonstrates genuine value, it can be activated via the feature flag.

## Related
- [[failures/blr-calibrator.md]]
- [[../kb-research/bot/overnight-miscalibration-analysis.md]] — Overnight underconfidence traced to BLR being disabled
