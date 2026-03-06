# Hourly Crypto Market Research Brief: ETH, SOL, XRP

**Generated**: 2026-03-06
**Data source**: state.db (VPS, WAL-checkpointed)
**Scope**: All settled hourly_observation signals for ETH, SOL, XRP
**Purpose**: Self-contained brief for AI/analyst to immediately begin calibration and strategy research

---

## 1. Executive Summary

### Key Failures
1. **XRP is catastrophically miscalibrated**: 34.6% WR with 90.6% avg predicted prob (+56.0pp overconfidence). No configuration produces positive PnL. The EGARCH volatility model fundamentally overestimates XRP's directional predictability.
2. **SOL has massive overconfidence at 90-94c**: 38% WR in the 90-94c band (n=93, $-51.48 PnL). The model is confident at exactly the price range where it fails most.
3. **ETH has a bizarre 80-84c dead zone**: 0W/24L (0% WR!) in the 80-84c price band. This is statistically impossible under the model's predictions and suggests a structural bias.
4. **All three assets show severe edge inversion**: higher model edge = worse actual WR. The model's confidence signal is anti-predictive.

### Profitable Regimes Identified
- **ETH edge<=3.0%**: n=64, 89% WR, $0.96 PnL (only positive config)
- **ETH 95-99c**: n=13, 100% WR, $0.45 PnL (tiny sample)
- **BTC (comparison)**: n=229, 84.7% WR, $4.50 PnL — the model works for BTC

### Top 3 Actionable Insights
1. **CalEngine (beta_cal) was disabled — passthrough+T=1.45 is now live**: The hourly CalEngine's beta calibration was +44pp overconfident (93.2% predicted vs 49.2% actual, n=455). The `shadow_cal_prob` column (which is just passthrough+T=1.45, NOT a separate CalEngine) had far better Brier scores. CalEngine was disabled Mar 6 2026 — passthrough+T=1.45 is now the live pipeline.
2. **Per-asset temperature scaling needed**: T=2.76 for ETH, T~8 for SOL, T~8 for XRP (per hourly alpha research). Current T=1.45 is optimal for BTC only. SOL/XRP need T so high it flattens to ~50/50, confirming the model has no predictive power for those assets.
3. **XRP should be excluded entirely** until the volatility model is fundamentally reworked. It contributes $-95.63 in simulated losses (53% of total ETH/SOL/XRP losses).

---

## 2. Per-Asset Deep Dive

### 2.1 ETH (Ethereum)

| Metric | Value |
|--------|-------|
| Total signals | 133 |
| W/L | 80W/53L |
| Win Rate | 60.2% |
| Simulated PnL | $-32.02 |
| Avg Price | 83c |
| Avg Breakeven WR | 84.2% |
| WR vs Breakeven | **-24.1pp** (far below breakeven) |
| Avg Calibrated Prob | 88.1% |
| Overconfidence | **+27.9pp** |
| Avg Edge | 4.82% |
| Avg STC | 1210s |

**Price Band Performance:**

| Band | N | W/L | WR | BE | PnL | Assessment |
|------|---|-----|----|----|-----|------------|
| 50-69c | 18 | 12/6 | 67% | 62% | $0.79 | Marginal profit |
| 70-79c | 20 | 7/13 | 35% | 78% | $-8.52 | Bad — below BE |
| **80-84c** | **24** | **0/24** | **0%** | **83%** | **$-19.94** | **CATASTROPHIC** |
| 85-89c | 27 | 21/6 | 78% | 88% | $-2.84 | Close but unprofitable |
| 90-94c | 31 | 27/4 | 87% | 93% | $-1.96 | Close but unprofitable |
| 95-99c | 13 | 13/0 | 100% | 97% | $0.45 | Profitable (small n) |

**Critical finding**: The 80-84c band has 0% WR across 24 signals. This is a structural failure — the model predicts ~83% probability for events that literally never happen in this price range for ETH. Investigate whether ETH hourly markets in this price range have a systematic bias (e.g., mean reversion pattern that the above/below model doesn't capture).

**Edge Inversion (SEVERE):**

| Edge Band | N | WR | Assessment |
|-----------|---|----|----|
| 1.0-2.0% | 31 | 97% | Excellent |
| 2.0-5.0% | 53 | 77% | Good |
| 5.0-15.0% | 49 | **18%** | **CATASTROPHIC** |

Higher model edge = dramatically worse WR. The model's confidence is anti-predictive beyond ~3% edge.

**Best Config**: edge<=3.0% produces n=64, 89% WR, $0.96 PnL.

**STC Performance**: All STC ranges unprofitable. No clear timing advantage.

**Hour-of-day**: US afternoon (13-19h UTC) is strong (WR 73-100%). European/Asian hours (03-08h) are weak (17-50% WR).

**Calibration Buckets:**

| Predicted | N | Actual WR | Gap |
|-----------|---|-----------|-----|
| 0.85-0.90 | 22 | 41% | +47.9pp |
| 0.90-0.95 | 53 | 45% | +47.0pp |
| 0.95-1.00 | 32 | 91% | +5.4pp |

The model is well-calibrated at 95%+ but massively overconfident at 85-95%.

**Passthrough+T=1.45 vs beta_cal**: Brier 0.094 vs 0.334 — **passthrough wins by 0.241** (the `shadow_cal_prob` column IS passthrough, not a separate engine)

**Temperature sensitivity**: T=1.75 achieves Brier 0.144 with near-zero overconfidence (n=24). T=2.76 is optimal per hourly alpha grid search. Current T=1.45 is insufficient for ETH.

---

### 2.2 SOL (Solana)

| Metric | Value |
|--------|-------|
| Total signals | 188 |
| W/L | 110W/78L |
| Win Rate | 58.5% |
| Simulated PnL | $-54.47 |
| Avg Price | 86c |
| Avg Breakeven WR | 87.5% |
| WR vs Breakeven | **-29.0pp** |
| Avg Calibrated Prob | 90.6% |
| Overconfidence | **+32.1pp** |
| Avg Edge | 4.13% |
| Avg STC | 1365s |

**Price Band Performance:**

| Band | N | W/L | WR | BE | PnL | Assessment |
|------|---|-----|----|----|-----|------------|
| 50-69c | 19 | 14/5 | 74% | 62% | $2.16 | Profitable |
| 70-79c | 12 | 10/2 | 83% | 76% | $0.85 | Profitable |
| 80-84c | 14 | 11/3 | 79% | 83% | $-0.60 | Near-breakeven |
| 85-89c | 35 | 26/9 | 74% | 88% | $-4.87 | Below BE |
| **90-94c** | **93** | **35/58** | **38%** | **93%** | **$-51.48** | **CATASTROPHIC** |
| 95-99c | 15 | 14/1 | 93% | 97% | $-0.53 | Near-breakeven |

**Critical finding**: The 90-94c band contains 49% of all SOL signals and produces 94% of SOL losses. The model concentrates its trading in exactly the worst price range.

**Edge Pattern (NON-MONOTONIC):**

| Edge Band | N | WR |
|-----------|---|-----|
| 1.0-2.0% | 37 | 84% |
| 2.0-5.0% | 102 | 41% |
| 5.0-15.0% | 49 | 76% |

SOL shows a U-shaped pattern: low and high edge are OK, but medium edge (2-5%) is terrible. This suggests the model's edge calculation is unreliable in the mid-range.

**STC Performance**: STC 900-1200s is the only profitable window (86% WR, $0.09). Long STC (1200-1800s) is where 78% of losses occur ($-42.54).

**Calibration Buckets:**

| Predicted | N | Actual WR | Gap |
|-----------|---|-----------|-----|
| 0.90-0.95 | 103 | 46% | +47.6pp |
| 0.95-1.00 | 44 | 68% | +28.0pp |

**Passthrough+T=1.45 vs beta_cal**: Brier 0.073 vs 0.382 — **passthrough wins by 0.309**

**Temperature sensitivity**: Even T=3.0 only gets to Brier 0.224 with +6.1pp OC (n=41). Optimal T~8 per grid search — so high it flattens to ~50/50, meaning the model has no real signal for SOL.

**Hour patterns**: US afternoon (14-15h UTC) strong (85-91% WR). Overnight (00-05h, 22-23h) weak (30-42% WR).

---

### 2.3 XRP (Ripple)

| Metric | Value |
|--------|-------|
| Total signals | 188 |
| W/L | 65W/123L |
| Win Rate | **34.6%** |
| Simulated PnL | **$-95.63** |
| Avg Price | 84c |
| Avg Breakeven WR | 85.4% |
| WR vs Breakeven | **-50.9pp** |
| Avg Calibrated Prob | 90.6% |
| Overconfidence | **+56.0pp** |
| Avg Edge | 6.18% |
| Avg STC | 1432s |

**Price Band Performance:**

| Band | N | W/L | WR | BE | PnL |
|------|---|-----|----|----|-----|
| 50-69c | 9 | 5/4 | 56% | 63% | $-0.68 |
| **70-79c** | **54** | **8/46** | **15%** | **79%** | **$-34.83** |
| 80-84c | 23 | 8/15 | 35% | 84% | $-11.27 |
| 85-89c | 51 | 23/28 | 45% | 89% | $-22.25 |
| 90-94c | 45 | 19/26 | 42% | 93% | $-22.81 |
| 95-99c | 6 | 2/4 | 33% | 96% | $-3.79 |

**Every single price band is unprofitable for XRP.** Even the 50-69c range (which should be easy to predict) loses money. The model is fundamentally wrong about XRP price dynamics.

**Edge Inversion (CATASTROPHIC):**

| Edge Band | N | WR |
|-----------|---|-----|
| 1.0-2.0% | 13 | 54% |
| 2.0-5.0% | 73 | 48% |
| 5.0-15.0% | 102 | **23%** |

Higher model confidence = worse outcomes. At >5% edge, XRP wins only 23% of the time. The model's edge signal is strongly anti-predictive.

**70-79c disaster**: 15% WR (8W/46L) at 70-79c is worse than random coin flips. The model predicts ~75% probability for events that happen 15% of the time. This is the single worst sub-population in the entire system.

**STC Performance**: All ranges unprofitable. STC 1200-1800s is worst (28% WR, $-78.59).

**Calibration (COMPLETE FAILURE):**

| Predicted | N | Actual WR | Gap |
|-----------|---|-----------|-----|
| 0.85-0.90 | 53 | 15% | **+74.1pp** |
| 0.90-0.95 | 94 | 38% | +54.5pp |
| 0.95-1.00 | 26 | 42% | +53.6pp |

The 0.85-0.90 bucket has **74pp overconfidence** — predicting 89% but achieving 15%.

**Passthrough+T=1.45 vs beta_cal**: Brier 0.066 vs 0.559 — **passthrough wins by 0.493**. This is the largest improvement of any asset — beta_cal was catastrophically wrong for XRP.

**Temperature**: Even T=3.0 has Brier 0.427 with +46.4pp OC. Optimal T~8 flattens to ~50/50. No temperature value can fix XRP — the underlying probability estimates are structurally wrong.

**Hour patterns**: Universally bad. 0% WR at hours 05, 11, 20. Even the "best" hours (07-10h UTC) have tiny samples.

---

## 3. Correlated Loss Analysis

### Multi-Loss Windows (Top 15)

| Event | Signals | Losses | PnL | Asset |
|-------|---------|--------|-----|-------|
| KXXRPD-26MAR0418 | 7 | 7 | $-6.09 | XRP |
| KXXRPD-26MAR0421 | 7 | 7 | $-5.87 | XRP |
| KXETHD-26MAR0323 | 8 | 7 | $-5.60 | ETH |
| KXXRPD-26MAR0508 | 7 | 6 | $-5.08 | XRP |
| KXXRPD-26MAR0420 | 6 | 6 | $-5.00 | XRP |
| KXXRPD-26MAR0512 | 7 | 6 | $-4.97 | XRP |
| KXXRPD-26MAR0419 | 7 | 6 | $-4.88 | XRP |
| KXSOLD-26MAR0419 | 6 | 5 | $-4.50 | SOL |
| KXXRPD-26MAR0513 | 5 | 5 | $-4.21 | XRP |
| KXXRPD-26MAR0423 | 6 | 5 | $-4.13 | XRP |
| KXXRPD-26MAR0510 | 6 | 5 | $-4.12 | XRP |
| KXETHD-26MAR0402 | 6 | 5 | $-4.07 | ETH |
| KXETHD-26MAR0403 | 7 | 5 | $-3.93 | ETH |
| KXSOLD-26MAR0422 | 5 | 4 | $-3.56 | SOL |
| KXSOLD-26MAR0400 | 5 | 4 | $-3.55 | SOL |

**Total multi-loss window PnL: $-168.17**

XRP dominates correlated losses (8 of top 15 windows). Multiple XRP windows have 100% loss rate (7/7, 6/6). This is not random — when the model is wrong about XRP direction, ALL strikes within that window fail simultaneously.

---

## 4. Calibration Diagnostics

### 4.1 Passthrough+T=1.45 vs CalEngine beta_cal (per asset)

| Asset | N | beta_cal Brier | Passthrough Brier | Improvement | Winner |
|-------|---|-----------|-------------|-------------|--------|
| ETH | 127 | 0.3344 | **0.0936** | 3.6x | Passthrough |
| SOL | 181 | 0.3815 | **0.0730** | 5.2x | Passthrough |
| XRP | 183 | 0.5588 | **0.0660** | 8.5x | Passthrough |

**Passthrough+T=1.45 is dramatically better for all three assets.** The CalEngine's beta calibration was actively harmful — it transformed reasonable raw probabilities into overconfident nonsense. **Fixed Mar 6 2026**: `HOURLY_CALIBRATION_ENABLED = False`, passthrough+T=1.45 is now the live pipeline.

Note: The `shadow_cal_prob` column in the DB stores the passthrough output. Despite the column name, it is NOT a separate CalEngine — it's the raw probability with temperature scaling only.

### 4.2 Temperature Sensitivity

| Asset | Current (T=1.45) Brier | Optimal T | Best T Brier | OC at Best T |
|-------|--------------|--------|-------------|-------------|
| BTC | ~0.08 | 1.45 | ~0.08 | ~0pp |
| ETH | 0.326 | 2.76 | ~0.14 | ~0pp |
| SOL | 0.368 | ~8 | ~0.25 | flattens to 50/50 |
| XRP | 0.549 | ~8 | ~0.43 | flattens to 50/50 |

T=1.45 is optimal for BTC but insufficient for ETH. SOL and XRP need T so high that it pushes all probabilities toward 50/50 — meaning the model has no predictive signal for these assets at hourly timescales. Temperature cannot fix a model that doesn't work.

### 4.3 Market Blend Sensitivity

| Asset | Current (40%) Brier | Best Blend | Best Brier |
|-------|-------------------|-----------|-----------|
| ETH | 0.326 | 60% | 0.166 (n=21) |
| SOL | 0.368 | 60% | 0.344 (n=34) |
| XRP | 0.549 | 20% | 0.639 (n=55) |

For ETH, higher market blend helps (the market is more accurate than the model). For XRP, NOTHING helps — all blend weights produce terrible Brier scores.

### 4.4 Edge Monotonicity

**Expected**: Higher edge = higher WR (model confidence predicts outcomes)
**Actual**: Edge inversion across all three assets

| Asset | Low Edge (1-2%) WR | High Edge (5-15%) WR | Inverted? |
|-------|-------------------|---------------------|-----------|
| ETH | 97% | 18% | **YES (catastrophic)** |
| SOL | 84% | 76% | YES (mild) |
| XRP | 54% | 23% | **YES (catastrophic)** |
| BTC | 92% | 80% | YES (mild, expected) |

BTC's mild inversion is expected (higher edge = more volatile = more uncertain). ETH and XRP's catastrophic inversion means the model's edge calculation is actively misleading for these assets.

---

## 5. System Architecture Context

### 5.1 Pipeline (for researcher)

```
Raw price data (Coinbase WebSocket)
    |
    v
EGARCH + Realized Kernel volatility model
    |
    v
Statistical probability (raw_prob)
    |  - P(price stays above/below threshold in remaining window)
    |  - Uses EGARCH blend: w * EGARCH_sigma + (1-w) * RK_sigma
    |  - Blend weight is adaptive (QLIKE-based)
    |
    v
CalibrationEngine (beta calibration) — **DISABLED Mar 6 2026**
    |  - Was +44pp overconfident (93.2% predicted vs 49.2% actual)
    |  - Dedicated hourly engine exists but is bypassed
    |  - Passthrough mode: raw_prob passes through unchanged
    |
    v
Temperature scaling: T=1.45
    |  - prob_adjusted = prob^(1/T) / (prob^(1/T) + (1-prob)^(1/T))
    |  - Softens overconfident probs (95% -> 88.4%)
    |  - Optimal for BTC only; ETH needs T=2.76, SOL/XRP need T~8
    |
    v
Market blend: final = 0.60 * model + 0.40 * market_price/100
    |  - Market price from Kalshi orderbook
    |
    v
Fee-adjusted edge check
    |  - edge = final_prob - breakeven_wr
    |  - breakeven_wr = (price + maker_fee) / 100
    |
    v
Position sizing (Quarter-Kelly)
    |  - kelly_f = edge / (1 - breakeven_wr)
    |  - position = 0.25 * kelly_f * bankroll
    |
    v
Per-window limits
    |  - Max 2 positions per event_ticker
    |  - Max 15% aggregate risk per window
    |
    v
[OBSERVATION MODE] -> Log to evaluated_opportunities
```

### 5.2 Data Availability

| Column | Description | Coverage |
|--------|-------------|----------|
| market_price | Kalshi orderbook price (cents) | 100% |
| calibrated_prob | Final calibrated probability | ~100% |
| raw_prob | Pre-calibration statistical prob | Partial |
| edge | Raw edge (prob - price/100) | ~100% |
| fee_adjusted_edge | Edge minus maker fee impact | ~100% |
| seconds_to_close | Time until window settlement | ~100% |
| spot_price | Current crypto price (USD) | ~100% |
| threshold | Strike price boundary | ~100% |
| volatility | RK estimate | ~100% |
| egarch_blend_weight | EGARCH vs RK blend | ~100% |
| egarch_blend_sigma | Blended vol estimate | ~100% |
| shadow_cal_prob | Shadow CalEngine output | ~90% |
| hourly_pre_temp_prob | Pre-temperature prob | ~50% |
| hourly_applied_temp_t | Temperature used | ~50% |
| hourly_shadow_temp_* | Shadow temperature variants | ~30-50% |
| hourly_shadow_blend_* | Shadow blend variants | ~15-25% |
| position_size | Kelly-sized position | ~100% |
| z_score | Volatility z-score | ~100% |

### 5.3 Market Structure

- **75 strikes per hourly event** (vs 1 per 15M event)
- Strikes are price thresholds: "Will BTC be above $X at settlement?"
- Multiple strikes can fire in the same window → correlated positions
- Settlement is binary: YES if price >= threshold, NO otherwise
- Hourly windows settle every hour, 24/7 for crypto

### 5.4 Key Differences from 15M (which works)

| Aspect | 15M | Hourly |
|--------|-----|--------|
| Window duration | 15 minutes | 60 minutes |
| Strikes per event | 1 | 75 |
| Correlation risk | None | High |
| EGARCH accuracy | Good | Degrades with time |
| Calibration | Dedicated engine, works | CalEngine disabled, passthrough+T=1.45 |
| WR | 88.2% | 60-85% by asset |
| Overconfidence | <2pp | 1-56pp by asset |

---

## 6. Structural Failures

### 6.1 ETH: Model fails in the 80-84c "dead zone"
- 0W/24L at 80-84c is statistically impossible under the model
- Hypothesis: ETH has a mean-reversion tendency that the above/below model doesn't capture. When the model predicts ~83% probability, it's in a regime where ETH tends to revert to the other side
- The EGARCH blend weight may be miscalibrated for ETH's volatility dynamics at this price range
- **Research needed**: Analyze the spot_price vs threshold relationship at 80-84c. Are these near-the-money strikes that revert?

### 6.2 SOL: Catastrophic concentration at 90-94c
- 93 of 188 signals (49%) are at 90-94c, with 38% WR
- The model generates TOO MANY signals at a price range where it's systematically wrong
- The edge calculation shows 2-5% edge for these signals, but actual WR is far below breakeven
- **Research needed**: Why does the model generate so many 90-94c signals? Is the volatility estimate systematically too low, making distant strikes appear "safe"?

### 6.3 XRP: Fundamental model failure
- No price band, no STC range, no edge filter, no hour filter produces positive PnL
- 56pp overconfidence — the model thinks it knows XRP's direction when it doesn't
- Even Shadow CalEngine (Brier 0.066 — excellent!) can't overcome the underlying signal quality
- **Root cause hypothesis**: XRP has fundamentally different microstructure — higher kurtosis, more frequent regime switches, manipulation events. The EGARCH model, calibrated primarily for BTC dynamics, doesn't capture XRP's behavior
- **Research needed**: Compare RK estimates for XRP vs actual realized vol. Is the model systematically underestimating XRP volatility?

### 6.4 Cross-Asset: Edge inversion is universal
- The edge metric (model_prob - market_price) SHOULD be positively correlated with WR
- Instead, it's negatively correlated for ETH and XRP
- This means: when the model disagrees with the market the most, it's the model that's wrong
- **Implication**: The market price is a better predictor than the model for these assets. Higher market blend weight would help.

---

## 7. Research Guidance

### Priority 1: Per-Asset Temperature Scaling (DONE for BTC, needed for ETH)
CalEngine beta_cal was disabled Mar 6 — passthrough+T=1.45 is now live. This is optimal for BTC but not for ETH/SOL/XRP:
- ETH needs T=2.76 (brings OC to ~0pp)
- SOL needs T~8 (flattens to 50/50 — model has no signal)
- XRP needs T~8 (same — model has no signal)

Implementing per-asset temperature would help ETH. SOL/XRP are unfixable by temperature — the underlying EGARCH model doesn't predict these assets at hourly timescales.

### Priority 2: Per-Asset CalEngines (longer term)
Once enough per-asset observation data is collected, dedicated per-asset CalEngines could learn asset-specific calibration curves. This is a longer-term project requiring hundreds of settled observations per asset.

### Priority 3: Edge Cap at 3% for ETH
ETH with edge<=3.0% has 89% WR and positive PnL. The edge inversion means high-edge signals should be REJECTED, not traded. Implement a MAX_EDGE filter.

### Priority 4: Exclude XRP from hourly
XRP contributes $-95.63 (53% of total ETH/SOL/XRP losses). No configuration is profitable. Exclude until the volatility model is fundamentally reworked for XRP's microstructure.

### Priority 5: SOL price band filter
SOL at 90-94c is responsible for 94% of SOL losses. Either:
- Cap SOL max price at 89c, OR
- Implement per-asset price-dependent edge thresholds (require much higher edge at 90c+ for SOL)

### Priority 6: Investigate ETH 80-84c anomaly
24 consecutive losses at 80-84c for ETH is not random. Research the relationship between:
- spot_price distance from threshold
- ETH mean-reversion tendency at hourly timescales
- Whether these are near-the-money strikes that tend to cross back

### Priority 7: Correlation model
Multi-position windows lose $168 total. The per-window limit (2) helps but doesn't eliminate correlated losses. Research:
- Per-asset window limits (XRP should be 0-1)
- Correlation-adjusted Kelly sizing
- Window-level rather than strike-level probability estimation

### Research Questions (Prioritized)
1. Can per-asset EGARCH parameters fix the volatility misestimation? The model was tuned on BTC — altcoin dynamics may differ fundamentally.
2. Why does the model produce confident predictions for assets where it has no signal? Is the EGARCH systematically underestimating altcoin volatility at hourly timescales?
3. Is XRP's excess kurtosis / regime-switching captured by any model variant?
4. Does ETH show mean-reversion at hourly timescales that the model misses? (Explains 0W/24L at 80-84c)
5. Can the edge calculation be restructured to eliminate inversion? (e.g., use market-adjusted edge instead of raw model edge)
6. Would a separate volatility model per asset improve calibration?
7. Can intraday seasonal patterns (US afternoon is better) be exploited with time-of-day filters?

---

## Appendix: Comparison with BTC (Reference)

BTC works because:
- Passthrough+T=1.45 calibration is nearly perfect (+0.8pp OC) — T was optimized for BTC
- Edge is mildly inverted but WR exceeds breakeven at all edge levels
- All price bands except 50-69c are profitable or near-breakeven
- The EGARCH volatility model fits BTC well (it was tuned on BTC data)

The key question is: **what makes BTC different?** Likely answers:
- BTC has higher liquidity → more efficient price discovery → model predictions track market better
- BTC's EGARCH parameters were tuned on BTC data → better fit
- BTC has lower kurtosis / fewer regime switches than altcoins
- T=1.45 happens to be the right temperature for BTC but not for altcoins

**End of Brief**
