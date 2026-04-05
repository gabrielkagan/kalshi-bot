---
status: active
updated: 2026-03-25
tags: [research, ml, calibration, lgbm]
---
# ML/AI Probability Improvements — Complete Research

Source: Deep research sessions Feb-Mar 2026
Chat links: https://claude.ai/chat/043b9eeb-150d-43b4-8b25-5802e385c863, https://claude.ai/chat/45e95e02-117d-46ed-bcd5-68041646cf45

---

## Context
Bot trades YES-side 15-minute crypto contracts using EGARCH(1,1) volatility → z-score → NIG CDF → calibration → 60/40 model/market blend → price-dependent edge threshold → Kelly sizing. 47-column feature vector available per evaluation. ~3,142 labeled calibration observations and ~1,797 evaluated opportunities at time of research.

## A. Probability Estimation: Can ML Beat z-score → CDF?

### Approaches Evaluated

**Gradient-boosted trees (XGBoost/LightGBM):**
- Could use full 47-column feature vector directly
- Risk: overfits at small n. With ~3,142 observations, aggressive hyperparameter tuning will find spurious patterns
- A2 LightGBM was built and deployed as shadow
- **Finding:** Confirmed as price proxy with no independent predictive value beyond EGARCH. Literally just re-derived the current market price from the features. Kept as passive dashboard metric only.

**Small neural nets (LSTM/GRU on 5-second return series):**
- 10,800 return buffer (~15 hours of 5-second data)
- Evaluated — no clear advantage over EGARCH at 15-minute horizon for crypto
- Computational cost too high for $6-15/month VPS constraint

**Logistic regression variants:**
- Platt scaling, Beta calibration already in pipeline
- BLR (Bayesian Logistic Regression) was the calibration layer — collapsed silently, outputting ~95% constant probability regardless of input. Single largest source of lost alpha.

### Key Finding
At current data scale (~3K observations), sophisticated ML doesn't beat well-calibrated parametric models for binary crypto predictions. The bottleneck is calibration quality, not model complexity. Passthrough calibration (raw EGARCH probabilities) dramatically outperformed the broken BLR.

## B. Volatility Forecasting Improvements over EGARCH(1,1)

### HAR (Heterogeneous Autoregressive)
- Captures long-memory in realized volatility that EGARCH misses
- Uses three components: daily, weekly, monthly realized vol
- Better at multi-horizon forecasting
- **Deployed as HAR-RV shadow for SPX** — natural fit for equity intraday vol
- For crypto 15M: less clear advantage since EGARCH already captures short-term vol dynamics well

### Rough Volatility Models
- Theoretically superior — fractional Brownian motion captures the rough nature of volatility paths
- Computationally heavy — impractical for $6-15/mo VPS
- Parked for future exploration if compute budget increases

### HEAVY Models
- Combine high-frequency and low-frequency vol estimates
- Interesting conceptually but implementation complexity high relative to marginal improvement
- Not implemented

### Realized Kernel Improvements
- Already implemented in the bot — data-adaptive bandwidth with Parzen kernel
- MZ R²-weighted blending with EGARCH
- This is a known strong point of the current system

### Ensemble Approaches
- Combining EGARCH + HAR-RV where both available (SPX has both)
- MZ R² weighted blending already used for RK + EGARCH
- Natural extension: add HAR-RV as third input to blending

## C. Regime Detection / Overconfidence Detection

### BOCPD (Bayesian Online Changepoint Detection)
- Promising for detecting vol regime shifts in real-time
- Could dynamically adjust edge thresholds when regime changes detected
- Not implemented — remains actionable

### Hidden Markov Models
- Identify high/low vol regimes
- Computational cost manageable
- Could gate strategies differently in different regimes
- Not implemented — remains actionable

### Empirical Overconfidence Finding
- 3-5% edge bucket: only 82% WR vs 98% for 2-3% edge
- Confirms model overconfidence at extremes
- Suggests need for either better calibration at high-edge values or hard cap on edge claims

### Regime Cap Discovery
- A regime-dependent position cap was discovered in the codebase
- Was artificially limiting positions in certain conditions
- Removed after analysis showed it wasn't helping

## D. Calibration Deep Dive

### BLR Collapse (The Big Failure)
- BLR weights converged to degenerate state producing ~95% constant output
- Undetected for weeks because crypto 15M contracts settle YES at high rates
- No monitoring on calibrator output distribution
- Discovery March 25: examining output distributions revealed collapsed weights
- Fix: removed entirely, switched to passthrough
- Impact: $660 → $1,400 over five weeks at 92%+ WR

### Temperature Scaling
- Single parameter T dividing logits — simplest post-hoc calibration
- Most practical for very small samples
- Hourly pipeline uses T=1.45 (CalEngine disabled at +44pp overconfident)
- Weather pipeline needs T≥3.0 (essentially flattening to base rates)
- Sports uses LR_scale parameter for similar purpose

### CalEngine Progression
- Fixed Logistic → Platt → Beta → BLR (when BLR worked)
- 15M: Active, progressing through stages
- Hourly: DISABLED (+44pp overconfident)
- SPX: Has CalEngine (SPX-D variant), currently observation
- Weather: Per-city CalEngines learning in shadow
- Sports: Per-sport-group CalEngines learning in shadow

## E. RL Feasibility Assessment

### Full RL (learning to trade from scratch): Impractical
- Too few observations for exploration
- Long feedback loops (15 min per outcome minimum)
- Reward signal is sparse and noisy
- Would need millions of interactions to converge

### Narrow RL for Execution Optimization: Feasible but not prioritized
- When to escalate maker → taker
- Optimal retry timing for DC queue
- Action space is small, feedback is quick (fill or no fill within seconds)
- Haven't built this

### Autoresearch Concept (Karpathy)
- Autonomous parameter optimization loop
- Blocked by evaluation cycle time — real markets take days, not 5-minute training runs
- Requires fast backtesting harness: replay historical JSONL through candidate config, compute counterfactual metrics in seconds
- Then agent iterates at Karpathy speed on historical data, graduates promising configs to live shadow
- **Not started — blocked on backtesting harness**

## What Was Actually Implemented
1. LightGBM A2 → passive dashboard metric (no trading signal)
2. HAR-RV → shadow for SPX only
3. Temperature scaling → hourly, weather, and sports pipelines
4. Passthrough calibration → replaced BLR, dramatically improved 15M
5. Per-asset EGARCH recalibration with skewed-t → shadowed for all 4 crypto assets (A1-A4 variants)
6. Realized Kernel with adaptive bandwidth → live
7. MZ R²-weighted blending → live

## What Remains Actionable
1. Regime detection (BOCPD/HMM) for dynamic edge threshold adjustment
2. Fast backtesting harness to enable autoresearch-style parameter optimization
3. Ensemble vol forecasting combining EGARCH + HAR-RV where both available
4. Better calibration monitoring (output distribution checks, not just downstream metrics)

## Related (KB operational articles)
- [[kb/failures/blr-calibrator.md]]
- [[kb/decisions/blr-removal.md]]
- [[kb/concepts/fifteenm-shadow-variants.md]]
- [[kb/concepts/spx-engine.md]]
