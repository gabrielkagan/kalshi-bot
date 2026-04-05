---
status: active
updated: 2026-04-04
tags: [weather, ensemble, nwp, bracket-no]
---
# Weather Market System

## Summary
The weather engine (`weather_engine.py`) runs an NWP ensemble model combining GFS (31 members) and ECMWF (51 members) for 82 total ensemble members across 19 US cities. Currently observation-only (`WEATHER_OBSERVATION_ONLY = True`). Evaluates daily high temperature markets on Kalshi.

## Ensemble Model
- **Data source:** Open-Meteo ensemble API (free, no key)
- **GFS:** 31 ensemble members from NOAA's Global Forecast System
- **ECMWF:** 51 ensemble members from European Centre for Medium-Range Weather Forecasts
- **Total:** 82 members — probability estimated as fraction of members exceeding threshold
- **Poll interval:** 15 minutes (`WEATHER_POLL_INTERVAL = 900`)

## Per-City Std Correction
Ensemble spread systematically underestimates true forecast uncertainty. Per-city correction factors multiply ensemble_std before computing probabilities:
- **Underdispersive** (correction > 1.0): SFO (3.3x), PHI (1.6x), MIA (1.4x), BOS (1.3x), MSY (1.3x)
- **Calibrated** (~1.0): DCA, DAL, LAX
- **Overdispersive** (correction < 1.0): PHX (0.4x), MIN (0.4x), DEN (0.4x), OKC (0.3x), ATL (0.4x)

Default correction for unverified cities: 1.5x.

## Bias Correction
EWMA bias correction per city (`BIAS_EWMA_LAMBDA = 0.90`, ~7-day half-life). Requires `BIAS_MIN_SIGNALS = 30` settled signals before applying — fewer than 30 is just fitting noise.

## 19 Cities
NYC, CHI, MIA, DEN, LAX, AUS, ATL, SFO, DAL, PHX, PHI, MIN, SEA, HOU, BOS, LAS, OKC, DCA, MSY

Kalshi series tickers: KXHIGHNY, KXHIGHCHI, KXHIGHMIA, etc.

## Market Types
- **Tail markets:** Will temperature be above/below X? (binary YES/NO on extreme outcomes)
- **Bracket markets:** Will temperature fall in range [X, Y]? (bracket structure)
- **Lower tail focus:** Shadow evaluation focused on `{"lower_tail"}` for cities `{"DCA", "MIN", "PHI", "BOS", "NYC"}`

## NO-Side Opportunity
Weather markets have structural NO-side edge because YES prices at 88-96c reflect bracket markets that settle NO 91.7% of the time. See [[strategies/bracket-no.md]].
- `WEATHER_NO_SIDE_LIVE = False` — kill-switched off
- Execution pipeline wired but not activated

## Configuration
| Parameter | Value | Notes |
|-----------|-------|-------|
| `WEATHER_OBSERVATION_ONLY` | True | Observation mode |
| `WEATHER_MIN_ENTRY_PRICE` | 10c | Low floor for data collection |
| `WEATHER_MAX_ENTRY_PRICE` | 99c | |
| `WEATHER_MARKET_BLEND_W` | 0.20 | 80% model, 20% market (ensemble is primary) |
| `WEATHER_MIN_EDGE_PCT` | 0.001 | Very low for max signal collection |
| `WEATHER_MAX_RISK_PER_TRADE` | 0.10 | Conservative |
| `WEATHER_KELLY_FRACTION` | 0.25 | Quarter-Kelly |
| `WEATHER_MIN_SECONDS_BEFORE_CLOSE` | 3600 | 1 hour minimum |
| `WEATHER_MAX_SECONDS_BEFORE_CLOSE` | 86400 | Weather settles daily |

## CalEngine Integration
Per-city CalEngines learn in shadow. Each city gets its own CalibrationEngine instance registered in `_CAL_REGISTRY` as `"weather_{city}"`. Settlement routing sends observations to the correct per-city engine.

## Research Verdict: NO ALPHA on YES-Side

Analysis of 581 observations found the weather ensemble is structurally overconfident: +32pp average overconfidence (predicted ~62%, actual ~30%), Brier score 0.3498, YES-side WR 29.8%. Root cause: NWP warm bias — models systematically over-predict daytime highs. Temperature scaling T≥3.0 would be needed to compensate, essentially flattening to base rates. EMOS calibration (equivalent to "10-20 years of model development") requires 1-2 years of historical reforecasts — not practical short-term.

NO-side shows 73.1% WR but `no_ask` is empty at extreme z-scores and the ≤93c ceiling blocks valid signals. The bracket NO strategy ([[strategies/bracket-no.md]]) works around this by computing NO cost from YES price.

See `kb-research/bot/weather-nwp-analysis.md` for complete analysis with per-city breakdowns and calibration research.

## Related
- [[strategies/bracket-no.md]]
- [[concepts/cal-engine-registry.md]]
