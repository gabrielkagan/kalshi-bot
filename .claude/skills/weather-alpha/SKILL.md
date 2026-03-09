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

## How to Run

```bash
# 1. Copy DB from VPS (if not already local)
scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db

# 2. Run the alpha research script
python3 scripts/weather_alpha_research.py --db /tmp/state.db
```

## What It Covers (18 sections)

1. **City-by-city analysis** -- WR, PnL, Wilson CI, city exclusion analysis
2. **Ensemble quality** -- coverage, spread, bias, forecast MAE/RMSE per city
3. **HRRR vs ensemble** -- head-to-head accuracy comparison where both exist
4. **Temperature regime** -- performance by actual temp (cold/cool/mild/warm/hot)
5. **Lead time (STC)** -- hours-before-settlement impact on accuracy and WR
6. **Bias correction** -- raw vs corrected ensemble effectiveness
7. **Price tier** -- WR by price band vs breakeven
8. **Calibration diagnostics** -- predicted vs actual probability, Brier scores
9. **Seasonal patterns** -- day-of-week, time-of-day, daily P&L timeline
10. **Edge analysis** -- monotonicity test, optimal thresholds per city
11. **Market blend simulation** -- sweep WEATHER_MARKET_BLEND_W 0-50%
12. **Exhaustive config search** -- city x price x edge x STC x ensemble-std grid
13. **Alpha discovery** -- top 20 profitable configurations ranked
14. **Robustness validation** -- Wilson CI, Fisher tests, time stability on top 5
15. **NO-side opportunity** -- counterfactual analysis of NO-side trades
16. **Leak / counterfactual** -- rejected stage PnL analysis
17. **Data sufficiency** -- readiness checklist for promotion
18. **Final verdict** -- alpha exists / marginal / none

## Key DB Details
- Table: `evaluated_opportunities` where `product_type='weather'`
- Signal stage: `filter_stage='weather_observation'`
- Weather columns: `wx_ensemble_mean`, `wx_ensemble_std`, `wx_bias_correction`,
  `wx_n_members`, `wx_market_type`, `wx_actual_high_temp`, `wx_no_side_edge`,
  `wx_hrrr_temp`, `wx_corrected_mean`
- Fee model: `ceil(0.0175 * count * price * (100-price) / 100)` (maker)

## Dependencies
- Python 3 standard library only (sqlite3, math, collections, datetime, itertools)
- No external packages required

## Output
- Prints standardized text report to stdout
- Self-contained, rerunnable on any state.db copy
