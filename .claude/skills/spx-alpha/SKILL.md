---
name: spx-alpha
description: "Deep SPX hourly alpha research — EGARCH blend, VIX regimes, intraday patterns, 7-point readiness checklist. Use when: \"SPX deep dive\", \"SPX alpha research\", \"is SPX ready for live?\", \"SPX hourly analysis\", \"check SPX readiness\""
---

# SPX Hourly Alpha Research

Systematic alpha research on SPX hourly observation data. Analyzes EGARCH blend performance, VIX regime effects, intraday patterns, calibration quality, edge integrity, multi-position correlation, and counterfactual config optimization.

## Usage
```
/spx-alpha
/spx-alpha fresh    # Force fresh DB copy from VPS
```

## Steps

1. **Sync the database.** Follow `.claude/skills/references/db-sync.md` to sync the database. If user says "fresh", always re-sync regardless of cache age.

2. **Run the alpha research script**:
   ```bash
   python3 scripts/audit/spx_alpha_research.py --db /tmp/state.db 2>&1
   ```

3. **Present findings** with focus on:
   - Is SPX hourly profitable under any configuration?
   - EGARCH blend weight: adapting or stuck?
   - VIX regime impact on accuracy
   - Intraday patterns (open, lunch, close)
   - Optimal temperature, min price, STC range, blend weight
   - Multi-position window correlation risk
   - Readiness assessment for live trading promotion

4. **Track evolution** — note whether:
   - Observation count is growing toward readiness thresholds
   - Calibration is improving or degrading
   - EGARCH blend is adapting to SPX-specific dynamics
   - Edge signal is monotonic and informative
   - Any intraday period shows consistent alpha

## Report Sections

| Section | Purpose |
|---------|---------|
| 1. Performance Overview | WR, PnL, profit factor, Brier, Wilson CI, filter funnel |
| 2. EGARCH Blend Analysis | Blend weight distribution, WR by weight, sigma distribution |
| 3. VIX Regime Analysis | VIX implied RV terciles, seasonal factor, vol_regime impact |
| 4. Intraday Pattern | Market period WR (open/lunch/close), hour-by-hour, day-of-week |
| 5. Price Tier Analysis | WR by price band vs breakeven, profit factor per tier |
| 6. Multi-Position Window | Correlation, all-win/all-loss clustering, ENB, position limit sim |
| 7. Edge Integrity | Quintile monotonicity, edge threshold sweep, $/day |
| 8. Calibration Diagnostics | Probability bucket gaps, Brier, raw vs calibrated, overconfidence |
| 9. Trading Hours | Regular vs extended hours WR, volatility by session |
| 10. Counterfactual Configs | Temperature sweep, min price sweep, STC range sweep, blend sweep |
| 11. Robustness & Stability | Half-split stability, daily PnL, drawdown, streaks, concentration |
| 12. Price x STC Cross-Tab | Best/worst zones for targeting |
| 13. Temperature Tournament | Shadow column Brier comparison (if populated) |
| 14. Data Quality Audit | Column fill rates, CalEngine pipeline status |
| 15. Readiness Assessment | 7-point checklist for live trading promotion |

## Key Differences from Crypto Hourly

- **Finance fee category**: taker 0.035 (half of crypto's 0.07), maker 0.0175. This means breakeven WR is lower at every price point, making SPX intrinsically easier to profit from than crypto hourly.
- **Single asset**: SPX only (no multi-asset exclusion needed)
- **Market hours**: 9:30 AM - 4:00 PM ET cash session (strong intraday patterns — open and close tend to be more volatile)
- **VIX integration**: Uses implied volatility from VIX for vol estimation (crypto has no equivalent)
- **EGARCH+RK blend**: Same architecture as crypto but SPX-specific calibration needed
- **T=1.0**: No temperature correction yet (needs data to determine optimal)

## Readiness Checklist — with WHY for each threshold

| # | Check | Threshold | WHY |
|---|-------|-----------|-----|
| 1 | Trading days | ≥ 10 | SPX has strong day-of-week effects (Monday sell-off, Friday pinning). 10 days covers 2 full weeks, giving at least 1-2 observations per weekday. Below 10, you might have zero Friday data and miss a systematic pattern. |
| 2 | Observations | ≥ 100 | At 100 observations with 75% WR, Wilson 95% CI is (65%-83%). At 50, it's (61%-86%) — too wide to distinguish a 75% strategy from a 65% one. 100 is the minimum where the CI is narrow enough to compare against breakeven. |
| 3 | Min calibration bucket | ≥ 20 per bucket | Calibration maps predicted probability → actual outcome. With <20 per bucket, a single outlier can shift the calibration curve by 5+ pp. 20 gives standard error of ~10pp at 75% WR — still noisy but usable. |
| 4 | Blend weight adapting | Not stuck at prior | The EGARCH blend starts at a prior weight and should adapt as SPX data arrives. If it's stuck at the prior after 10+ days, the Mincer-Zarnowitz tracker may not be receiving data or the SPX vol dynamics are too different from the prior. Either way, the model isn't learning. |
| 5 | 80c+ WR > 85% | Breakeven+margin | At 80c with finance fees (taker 0.035), breakeven WR is ~81.4%. The +3.6pp margin covers estimation error and ensures profitability survives out-of-sample degradation. Without margin, a strategy that's exactly at breakeven in-sample will lose money live (regression to mean). |
| 6 | Overall WR > breakeven | Positive edge | The weighted-average WR across all price tiers must exceed the weighted-average breakeven. This catches the scenario where high-price trades win but low-price trades lose badly enough to offset. |
| 7 | Positive flat PnL | > $0 | Even with Kelly sizing, check flat 1-contract PnL as a sanity check. If flat PnL is negative but Kelly PnL is positive, the strategy is only profitable because Kelly over-sizes the wins — fragile. Both should be positive. |

**A system must pass ALL 7 checks to be considered for promotion.** Passing 6/7 is NOT enough — each check catches a different failure mode.

## Interpretation Guide

- **Brier Score**: < 0.10 excellent, 0.10-0.15 good, > 0.15 poor. WHY these ranges? Brier = 0 is perfect, Brier = 0.25 is coin-flip calibration. Below 0.10 means the model is well-calibrated; above 0.15 means it's making confident predictions that are often wrong.
- **Profit Factor**: > 1.5 strong, > 1.3 credible, < 1.2 noise. PF = gross_wins / gross_losses. Below 1.2, a single bad day can flip cumulative PnL negative.
- **Edge monotonicity**: Higher edge must produce higher WR or signal is noise. If 5% edge has lower WR than 2% edge, the edge signal is not informative — the model can't rank opportunities correctly.
- **Wilson CI lower bound > BE**: Required for statistical confidence in profitability. See alpha-audit skill for full explanation.
- **ENB (Effective Number of Bets)**: < 1.5 means positions are highly correlated within windows. Two positions that always win/lose together is really one bet with double the risk. ENB near 1.0 means the per-window position limit isn't helping.
- **Temperature**: > 1.2 suggests overconfidence (model probabilities are too extreme), < 0.9 suggests underconfidence. Optimal T makes calibration buckets match observed WR.

## Error Handling

| Situation | Action |
|-----------|--------|
| Script not found | Check: `ls scripts/audit/spx*`. The script may not exist yet — offer to run the hourly audit script instead (`scripts/audit/spx_shadow_audit.py`). |
| Very few observations (n < 30) | Report what exists but add **"INSUFFICIENT DATA"** on every finding. Don't run the readiness checklist — it's meaningless at n<30. Say: "Need N more observations before meaningful analysis. Current rate: ~X/day, est. Y days." |
| No SPX data at all | SPX engine may not be running during market hours, or it may be a weekend. Check if `spx_engine.py` is active: `ssh botuser@45.55.181.30 "journalctl -u kalshi-bot --no-pager -n 50 \| grep -i spx"`. |
| EGARCH blend weight is stuck at prior | Flag this prominently: "EGARCH blend not adapting — model is running on crypto priors, not SPX-calibrated. Readiness check #4 FAILS." |
| Script output shows negative PnL at all price tiers | The model may not work for SPX. Report honestly: "No profitable price tier found. SPX hourly is not ready for promotion. Consider: is the vol model appropriate for equity index dynamics?" |
| Readiness checklist shows 5/7 or 6/7 passing | Don't say "almost ready." Say which checks fail, what data/changes are needed to pass them, and estimate timeline. "Checks #2 and #3 fail — need 52 more observations (~6 trading days at current rate)." |
