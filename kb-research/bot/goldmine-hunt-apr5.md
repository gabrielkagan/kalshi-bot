---
status: active
updated: 2026-04-05
tags: [research, alpha, comprehensive, counterfactual, verification]
---
# Goldmine Hunt: Comprehensive Alpha Search (Apr 5, 2026)

12-agent deep dive searching for untapped alpha. Most "opportunities" collapsed under rigorous verification. Key methodology: find signal → challenge it → verify with Wilson CIs and counterfactual PnL → accept or reject.

## Executive Summary

The bot is well-optimized. No single goldmine exists that 10x's profitability. The biggest opportunities are in execution (fill rate), model improvement (vol seasonality), and expanding to new strategy types (market making).

---

## Opportunities That COLLAPSED Under Verification

### Relaxed Edge Threshold (originally "+$682, 93.4% WR")

**DEAD.** Of 542 signals:
- 30% (164) already traded — zero incremental
- 41% (221) passed all filters but went unfilled — execution failures, not edge issue
- 29% (157) truly incremental — at Kelly sizing produce **-$103 PnL**
- No Wilson CI clears breakeven for any price tier
- **The real finding:** 221 execution failures are a fill rate problem, not an edge threshold problem

### DC z≤-1.5 at 93-94c (originally "+$93.5K CF PnL")

**MOSTLY BASE RATE.** The $93.5K was in cents ($935). Under scrutiny:
- z-score is NOT statistically predictive at 93-94c (z-stat=0.87, not significant)
- Base rate at 93-94c is 95.0% regardless of z — the z filter adds nothing
- 52.5% of signals from one anomalously good week (W13: 98.6% WR)
- Without W13: WR drops to 93.3% — below breakeven
- SOL at 93c: 88.1% WR — toxic
- Only 94c excluding SOL is narrowly viable (~$4.50/day)
- Realistic incremental at DC sizing: $168.90 over 5 weeks

### Post-Loss Sizing Reduction (originally "+$3-5/day")

**REAL SIGNAL, TINY IMPACT.** Post-loss WR = 82.4% is verified (p=0.008). But:
- 77% of post-loss losses are same-window correlation (two positions lose in same reversal)
- Different-window post-loss trades have 92.3% WR — essentially baseline
- Counterfactual from halving: **+$23-33 total across 883 trades**
- Drawdown scaler already reduces 60% of post-loss trades (mean scaler=0.62)
- Not worth adding complexity for $0.03/trade

### Maker Adverse Selection (originally "-$83 PnL for maker fills")

**OVERSTATED.** Pure maker PnL is +$86.61, not -$83. But the nuanced finding is real:
- At 90-94c with STC 5-10m: maker 90% WR vs taker 98.6% WR — adverse selection
- At 85-89c: maker 94.4% WR vs taker 83.0% — maker WINS
- Going all-taker-first is NOT justified — effects cancel across price tiers
- SOL (already taker-first) improved modestly post-switch (+1.6pp WR)

---

## Opportunities That SURVIVED Verification

### 1. Fill Rate Improvement (Largest Real Pool)

221 candidates passed ALL filters but went unfilled. 450 total unfilled candidates at 97.3% WR, $967 CF PnL. SOL alone = $568.

- The fill rate is 69% for tracked candidates — 31% go unfilled
- Fill rate is best at 120-300s STC (94.5%) and worst at 0-60s (84.6%)
- Larger orders (31-60 contracts) fill at 95.7% — no size penalty
- **This is the single largest actionable opportunity**

### 2. Hourly STC Tightening (600s → 1200s)

- 600-1200s zone: 46-53% WR (losing money)
- 1200-1800s zone: 56-58% WR, +$377 sim PnL
- Simple config change: HOURLY_MIN_STC_ENTRY from 600 to 1200
- But only 10 actual live settled trades — mostly observation data

### 3. Intraday Vol Seasonality (Model-Level Fix)

Academic research confirms crypto has strong 24h vol pattern (double U-shape). The model overestimates vol overnight by 6-11%. A 24-element hourly multiplier array would:
- Replace ad-hoc overnight/weekend discount flags
- Fix the 7pp model gap at 91-92c overnight
- Be continuously data-driven vs binary on/off discounts

### 4. Weather NO-Side Pipeline Fix

348 observations at YES 85-96c, 94.5% NO settlement rate. But WEATHER_OBSERVATION_ONLY=True blocks upstream, so the NO-side live feature produces zero fills despite being "enabled."

### 5. Cross-Asset Confirmation

When BTC is at 95c+ and ETH/SOL at 85-90c in the same window: 90% YES settlement. 111 insufficient_edge rejections could be rescued. Dual z≤-3 across assets: 100% WR on 92 samples. New signal type needed.

### 6. Market Making + Liquidity Incentives

Kalshi pays up to $1K/day for resting orders. $0 maker fees. Different strategy from directional trading. Would need inventory management and both-side quoting. Significant development effort but potentially large return.

---

## Other Findings (Reference)

### Hourly Pipeline Status
- Only 10 live settled trades post-promotion (all BTC, 50% WR, -$22.87)
- 600-900s STC zone is consistently losing
- ETH at sub-60c is 41.2% WR — consider dropping
- 40-44c shows strong edge (49.5% vs 42% breakeven) but below current floor

### 15M Shadow Strategies (A1-A4)
- A1 RecalibratedEGARCH: 81.0% WR — not beating live
- A2 LightGBM: 83.6% WR — not beating live
- A3 EGARCH Gating: anti-correlated, needs retraining
- A4 LateWindow: 85.0% WR, $36 total PnL — not worth it
- **None ready for promotion**

### Sports Engine
- NBA: 66.7% WR on 42 trades — best group but still CONTINUE_COLLECTING
- Tennis: 51.5% on 101 trades — kill candidate
- All groups remain far from SPRT thresholds

### Weather YES-Side
- 28% WR at 78% model confidence — **model is catastrophically broken for YES-side**
- NO-side is the correct approach

### Volatility Regime Analysis
- Low vol = higher WR but WORSE PnL (oversize at high prices, catastrophic losses)
- High vol + high edge = best PnL cell (+$407 on 159 trades)
- Vol-of-vol as a Kelly discount could replace drawdown scaler (more principled)

### Cross-Asset Correlation
- Same-window correlation: 81-91% agreement across assets
- Wins are highly correlated; losses are less correlated
- 4-asset stacking: 100% WR (84/0) but joint losses are 3-5x independent expectation
- BTC leads ETH by +9.9pp lift; SOL/XRP are followers

### Fee Optimization
- Total fees: $204.52 = 19.4% of gross profit
- 416 maker fills (33%) contribute only 10% of PnL
- Fill model journal: 3,043 entries with 8+ features — enough for fill prediction model
- Realistic fee savings from better routing: $15-30

---

## External Research Findings (Web)

Top implementable ideas from academic/industry research:

1. **Intraday vol seasonality** — confirmed by Eross et al. (2019), Baur & Dimpfl (2019). 30-50% lower overnight vol for BTC. 24-element hourly multiplier.
2. **STC-dependent vol blend weight** — Hansen & Lunde (2005), Andersen et al. (2003). RK dominates at short horizons, EGARCH at long. Make blend weight a function of STC.
3. **Real-time jump detector (BN-S test)** — Scaillet et al. (2020). Jumps = 15-30% of BTC variance. Compare RV to bipower variation. Jump clustering predicts elevated vol.
4. **Vol-of-vol Kelly discount** — Bouri et al. (2021). When vol-of-vol is high, model is unreliable. Principled replacement for drawdown scaler.
5. **Cross-exchange OBI** — arxiv (2025). Order book imbalance at 40 levels = 71.5% binary accuracy. Your CrossExchangeFeed already collects this data.
6. **Deribit gamma pinning** — Near options expiry, BTC pins near max-pain. Suppresses realized vol.
7. **KXFED/KXCPI macro signals** — arxiv (Apr 2026). Kalshi macro contract repricing predicts crypto vol.

---

## What to KILL

| Shadow/Feature | Why |
|----------------|-----|
| Weather YES-side | 28% WR, model catastrophically wrong |
| 15M A1-A4 shadows | None beat main pipeline |
| DC NO-side | Terrible risk/reward at 3-7c |
| Hourly 60-69c expansion | Net negative |
| XRP floor relaxation | 88.5% vs 92% breakeven |
| Tennis sports group | 51.5% on 101 trades |

## Related

- [[overnight-miscalibration-analysis.md]] — Overnight model gap (7pp at 91-92c)
- [[stc-sizing-research.md]] — STC vulnerability and SOL gate
- [[../kb/concepts/edge-thresholds.md]] — Price-dependent edge schedule
- [[../kb/strategies/terminal-momentum.md]] — TM strategy (similar overlay pattern)
