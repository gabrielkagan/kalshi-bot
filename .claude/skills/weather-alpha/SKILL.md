---
name: weather-alpha
description: "Deep weather alpha research — ensemble quality, HRRR comparison, bias correction, per-city analysis, config grid search. Use when: \"weather deep dive\", \"weather alpha research\", \"is weather ready?\", \"how's the ensemble doing?\", \"weather shadow analysis\""
---

# Weather Alpha Research Skill

## Purpose
Run comprehensive alpha research on weather temperature prediction market data.
Analyzes the NWP ensemble model (GFS+ECMWF, 82 members) performance across 5 cities
(NY, CHI, MIA, DEN, LAX) to discover profitable trading configurations.

## When to Use
- When the user asks about weather market performance, alpha, or readiness
- When investigating weather ensemble accuracy or bias
- When optimizing weather trading config (blend weight, edge thresholds, city selection)
- When checking if weather markets are ready for promotion from observation mode

## Usage
```
/weather-alpha
/weather-alpha fresh    # Force fresh DB copy from VPS
```

## Steps

1. **Sync the database.** Follow `.claude/skills/references/db-sync.md` to sync the database. If user says "fresh", always re-sync regardless of cache age.

2. **Run the alpha research script**:
   ```bash
   python3 scripts/audit/weather_alpha_research.py --db /tmp/state.db 2>&1
   ```

3. **Present findings** with example summary:

   ```
   ## Weather Alpha Research

   ### Verdict: MARGINAL ALPHA
   MIA and LAX show signal; NYC and CHI are noise; DEN too few observations.

   ### Baseline (all cities, current config)
   - 156 settled: 96W/60L (61.5%, Wilson 95% CI: 53.4-69.1%)
   - Sim PnL: -$203 (Kelly-sized)
   - Brier: 0.221 (poor — ensemble is overconfident in tails)
   - Ensemble coverage: 78% (22% of evals missing ensemble data)

   ### Per-City Breakdown
   | City | Settled | WR%  | CI (95%)    | Sim PnL | Brier | Status      |
   |------|---------|------|-------------|---------|-------|-------------|
   | MIA  | 42      | 71.4%| (55.4-84.3) | +$18    | 0.182 | PROMISING   |
   | LAX  | 38      | 68.4%| (51.3-82.5) | +$8     | 0.195 | PROMISING   |
   | NYC  | 34      | 55.9%| (38.0-72.6) | -$92    | 0.248 | NO ALPHA    |
   | CHI  | 28      | 50.0%| (30.7-69.4) | -$118   | 0.262 | NO ALPHA    |
   | DEN  | 14      | 64.3%| (35.1-87.2) | -$19    | 0.210 | LOW SAMPLE  |

   ### Key Issues
   1. Ensemble coverage 78% — 22% of evals missing wx_ensemble_mean
   2. NYC/CHI overconfident: model says 80%+ but observed WR is 50-56%
   3. Bias correction helps MIA (+4pp) but hurts CHI (-2pp)

   ### Readiness: NOT READY (3/8 checks pass)
   Need: more observations, better ensemble coverage, city-specific calibration.
   ```

4. **Track evolution** — note whether:
   - Ensemble coverage is improving (should approach 100%)
   - Per-city Brier scores are converging
   - Bias correction is helping consistently
   - Any city has crossed readiness thresholds
   - HRRR comparison shows it beating or matching the ensemble

## Sections (18 total)

| # | Section | Purpose |
|---|---------|---------|
| 1 | City-by-city analysis | WR, PnL, Wilson CI, city exclusion analysis |
| 2 | Ensemble quality | Coverage, spread, bias, forecast MAE/RMSE per city |
| 3 | HRRR vs ensemble | Head-to-head accuracy comparison where both exist |
| 4 | Temperature regime | Performance by actual temp (cold/cool/mild/warm/hot) |
| 5 | Lead time (STC) | Hours-before-settlement impact on accuracy and WR |
| 6 | Bias correction | Raw vs corrected ensemble effectiveness |
| 7 | Price tier | WR by price band vs breakeven |
| 8 | Calibration diagnostics | Predicted vs actual probability, Brier scores |
| 9 | Seasonal patterns | Day-of-week, time-of-day, daily PnL timeline |
| 10 | Edge analysis | Monotonicity test, optimal thresholds per city |
| 11 | Market blend simulation | Sweep WEATHER_MARKET_BLEND_W 0-50% |
| 12 | Exhaustive config search | City x price x edge x STC x ensemble-std grid |
| 13 | Alpha discovery | Top 20 profitable configurations ranked |
| 14 | Robustness validation | Wilson CI, Fisher tests, time stability on top 5 |
| 15 | NO-side opportunity | Counterfactual analysis of NO-side trades |
| 16 | Leak / counterfactual | Rejected stage PnL analysis |
| 17 | Data sufficiency | Readiness checklist for promotion |
| 18 | Final verdict | Alpha exists / marginal / none |

## Readiness Checklist — with WHY for each threshold

| # | Check | Threshold | WHY |
|---|-------|-----------|-----|
| 1 | Observation days | ≥ 14 | Weather has strong day-of-week patterns (weekend forecasts use older model runs). 14 days covers 2 full weeks and all 7 weekdays at least twice. |
| 2 | Total settled | ≥ 100 | At 100 with 65% WR, Wilson CI is (55%-74%). At 50, it's (50%-78%) — can't distinguish from coin flip. 100 is the minimum for meaningful analysis. |
| 3 | Per-city settled | ≥ 20 each | Cities have different forecast difficulty (coastal vs inland, stable vs volatile). A strategy that works in MIA but fails in CHI isn't ready. Need ≥20 per city to detect city-specific failure modes. |
| 4 | Ensemble coverage | ≥ 90% | If >10% of evals are missing ensemble data (`wx_ensemble_mean` is NULL), the model is running blind on those trades. Either the weather API is failing or the fetch timing is wrong. Must fix before going live. |
| 5 | Bias correction helps | ≥ 3 cities improved | Raw NWP ensembles have systematic biases (GFS runs warm in summer, cold in winter). Bias correction should improve at least 3/5 cities' Brier scores. If it hurts most cities, the correction is overfit or has too little training data. |
| 6 | No city WR < 50% | Floor for live | A city with WR below coin-flip is actively losing money. Even if other cities profit, one bad city can consume the edge. Either exclude it or fix the model. |
| 7 | Overall Brier < 0.20 | Calibration quality | Brier = 0.25 is uninformed (always guess 50%). Brier < 0.20 means the model has real predictive power. Below 0.15 is good; below 0.10 is excellent. Weather is harder to calibrate than crypto (longer horizons, fewer data points), so 0.20 is the floor. |
| 8 | Positive sim PnL (Kelly) | Net profitable | Must be positive using actual Kelly sizing, not flat contracts. Weather markets have different price distributions than crypto (more mid-range 40-60c prices), so Kelly sizing is less aggressive — PnL per trade is smaller. |

**A system must pass ALL 8 checks to be considered for promotion.** Weather is the newest and most complex system — be conservative.

## Interpretation Guide

- **ALPHA EXISTS**: ≥6/8 checks pass, overall PnL positive, at least 2 cities profitable with CI lower > 50%
- **MARGINAL ALPHA**: 4-5/8 checks pass, some cities profitable but overall PnL may be negative
- **WEAK ALPHA**: 2-3/8 checks pass, only 1 city looks promising
- **NO ALPHA**: <2 checks pass, overall PnL negative, no city consistently above breakeven
- **Ensemble spread**: Wide spread (high `wx_ensemble_std`) = model uncertainty. WR should be lower when spread is high. If WR is HIGHER when spread is high, the model may be overconfident when members agree.
- **HRRR vs Ensemble**: HRRR is a single high-res model updated hourly. If HRRR beats the 82-member ensemble on MAE, the ensemble may be stale (using older model runs). HRRR advantage suggests lead-time sensitivity.
- **Bias correction**: Compare `wx_ensemble_mean` vs `wx_corrected_mean` against `wx_actual_high_temp`. If corrected is consistently closer, bias correction is working. Per-city analysis is critical — correction may help coastal cities but hurt inland.
- **Market blend (WEATHER_MARKET_BLEND_W=0.20)**: Weather uses 80% model / 20% market. This is much less market weight than crypto (40%) because weather ensemble forecasts are more reliable than EGARCH for their domain. The blend sweep should confirm 0.15-0.25 is optimal.
- **Profit Factor**: >1.3 credible, >1.5 strong (same as other systems)
- **Wilson CI lower bound > breakeven**: Required for statistical confidence — same as alpha-audit thresholds

## Key DB Details
- Table: `evaluated_opportunities` where `product_type='weather'`
- Signal stage: `filter_stage='weather_observation'`
- Weather columns: `wx_ensemble_mean`, `wx_ensemble_std`, `wx_bias_correction`,
  `wx_n_members`, `wx_market_type`, `wx_actual_high_temp`, `wx_no_side_edge`,
  `wx_hrrr_temp`, `wx_corrected_mean`
- Fee model: maker = `ceil(0.0175 * count * price * (100-price) / 100)`, taker = `ceil(0.035 * count * price * (100-price) / 100)`
- Cities: KXHIGHNY (New York), KXHIGHCHI (Chicago), KXHIGHMIA (Miami), KXHIGHDEN (Denver), KXHIGHLAX (Los Angeles)

## Error Handling

| Situation | Action |
|-----------|--------|
| Script not found | Check: `ls scripts/weather*`. |
| 0 weather observations | Weather engine may not be running. Check: `ssh botuser@45.55.181.30 "journalctl -u kalshi-bot --no-pager -n 50 \| grep -i weather"`. Weather markets are daily settlement — fewer signals than 15M/hourly. |
| Very few settled (n < 20) | Report what exists but add **"INSUFFICIENT DATA"** on every finding. Don't run readiness checklist at n<20. Say: "Need N more observations. Weather settles once daily per city → max 5 new data points/day." |
| Ensemble coverage < 50% | Something is wrong with the weather fetch. Check if Open-Meteo API is responding. Report: "Ensemble data missing for >50% of evals — weather_engine.py may be failing to fetch forecasts." |
| All cities show WR near 50% | Model has no edge. Be honest: "No city shows meaningful edge. Weather prediction at these horizons may not beat the market." Don't search for cherry-picked configs. |
| Script output is very long | Show the verdict, readiness checklist, and per-city table. Offer specific sections on request. |
| `wx_actual_high_temp` is NULL for settled rows | Settlement data isn't being backfilled. This means Brier scores and calibration can't be computed. Report: "Actual temperature missing for N settled rows — settlement pipeline may not be filling weather actuals." |
| HRRR data shows 0 rows | HRRR fetch may not be active. HRRR is optional (bonus signal, not primary). Note it and continue with ensemble-only analysis. |

## Dependencies
- Python 3 standard library only (sqlite3, math, collections, datetime, itertools)
- No external packages required
