---
status: active
updated: 2026-03-22
tags: [research, weather, nwp, ensemble]
---
# Weather NWP Ensemble Analysis — Complete Research

Source: Multiple sessions Mar 2026
Chat links: https://claude.ai/chat/932a113a-5dd3-4041-92e9-88d7a78ff5f6, https://claude.ai/chat/2e28531a-07ee-42d6-88c3-50192c210a59, https://claude.ai/chat/baf8c081-c27f-4d84-8c29-6455f26b5e67

---

## Verdict: NO ALPHA (YES-side). NO-side structurally interesting but unviable at current pricing.

## The Warm Bias Problem
NWP ensemble models (GFS, HRRR, ECMWF) systematically over-predict daytime high temperatures, especially at rural/suburban stations. Well-documented in meteorological literature. Practical effect: YES-side temperature contracts ("will temp exceed X?") are systematically overpriced because the forecast models feeding market prices are biased warm.

## Implementation Built
- 82 NWP ensemble members (31 GFS + 51 ECMWF via Open-Meteo)
- 19 US cities tracked
- Per-city CalEngines learning in shadow
- Data source: Open-Meteo ensemble API (free, no API key, 10K requests/day)

## Empirical Results (581 shadow observations)

### YES-Side
- Win rate: 29.8% (terrible)
- Settlement rate: 30.1%
- Model overconfidence: +32pp (predicted ~62% → actual ~30%)
- Brier score: 0.3498

The +32pp overconfidence is the whole story — NWP ensemble probabilities are essentially meaningless as market-calibrated signals in their current form.

### NO-Side
- Win rate: 73.1% (strong signal)
- But pricing doesn't work: at extreme z-scores, `no_ask` field is empty — no one posts sell orders for NO at 99c. The ≤93c ceiling rejects anything that does come through.
- Fix evaluated: relax to z≥3 where NO is 80-90c (where liquidity actually exists)
- Two compounding bugs: empty `no_ask` at extreme z-scores AND ≤93c ceiling blocking valid signals

### Short STC (1-8h) Signal
- n=34 only (far too small)
- Intuition: forecast error grows with lead time, so short windows compress outcomes closer to NWP prediction
- Marginal promise but insufficient data to validate
- Would need 2+ more months of shadow data

### Temperature Scaling Analysis
- T≥3.0 needed to avoid catastrophic overconfidence
- But at T≥3.0, you're barely using the NWP signal — essentially flattening everything toward base rates
- At that point might as well trade city/season base rates directly (probably not an edge either)

## Bias Correction Approach Researched
- **Simple:** Rolling 7-day mean bias correction per city (observed_temp - forecast_temp, averaged over last 7 days). Adjusts forecast downward by recent systematic error.
- **Sophisticated:** Per-city, per-season, per-hour-of-day bias correction if enough data exists
- **EMOS (Ensemble Model Output Statistics):** From ECMWF research, improves calibration equivalent to "10-20 years of model development." Training requires 1-2 years of historical reforecasts (available free via Open-Meteo Historical Forecast API).
- None deployed — deprioritized before implementation

## Three Threads Evaluated

**1. NO-side reframing:** 73.1% WR is real signal even if pricing structure eats PnL. Question: is there a price tier within NO-side where WR holds AND math works after fees? If NO-side at 20-40c (YES at 60-80c) has decent WR, could be a narrow viable window.

**2. STC 1-8h filter:** Keep as shadow-only filter (STC markets, ≤8h to settlement). Would know in 1-2 months if real or noise.

**3. Temperature scaling beyond T=3.0:** Admits model confidence is garbage. Diminishing returns.

## Conclusion
Weather is structurally harder than crypto or sports. The data source (NWP ensembles) isn't calibrated to the resolution Kalshi markets require. Unless NO-side price-tier slice reveals something, deprioritize behind crypto improvements and sports (basketball), which both have clearer paths to live.

## Key Weather-Specific Risks (from original expansion research)
- NWS station microclimate effects (O'Hare airport ≠ downtown Chicago)
- DST/LST settlement timing nuances
- CLI report delays tying up capital
- Thin orderbooks limiting position sizes to ~$25K per contract

## Related (KB operational articles)
- [[kb/concepts/weather-system.md]]
- [[kb/strategies/bracket-no.md]]
