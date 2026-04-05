---
status: active
updated: 2026-03-15
tags: [research, spx, weather, expansion]
---
# Market Expansion Research — Complete Analysis

Source: Deep research session Feb-Mar 2026
Chat link: https://claude.ai/chat/baf8c081-c27f-4d84-8c29-6455f26b5e67

---

## Markets Evaluated

### 1. S&P 500 Hourly (KXINXU) — RECOMMENDED FIRST
- **Volume:** $167M cumulative series volume
- **Settlement:** Google Finance, hourly
- **Development estimate:** 3-5 days for MVP (existing execution stack is market-agnostic)
- **Taker fee coefficient:** 0.035 (half of crypto's 0.07)

**Volatility modeling for SPX:**
The existing EGARCH paradigm transfers but needs modifications. Key differences from crypto:
- VIX provides options-implied vol anchor not available in crypto
- Intraday vol has strong seasonality (U-shape: high at open/close, low at lunch)
- HAR-RV (Heterogeneous Autoregressive Realized Volatility) may outperform EGARCH for equities — better at capturing long-memory vol patterns
- Dual vol model recommended: EGARCH for short-term + HAR-RV for longer-horizon baseline

**Configuration recommendations from research:**
- MIN/MAX_ENTRY_PRICE: Same 85-95c range as crypto (high-probability contracts)
- Edge thresholds: Price-dependent (same structure as crypto)
- Market blend weight: May need higher model weight initially (less efficient market for hourly)
- Entry window timing: Avoid first 15 min after market open (high volatility, wide spreads)
- Time-of-day effects: Lunch hour (12-1pm ET) lowest vol → most predictable
- Position limits: $50 initially, scale to $150-200 if profitable

**Risk analysis:**
- Flash crashes, circuit breakers, trading halts
- FOMC / macro event vol spikes
- Google Finance settlement source — any known issues?
- More sophisticated counterparties than crypto (institutional traders)
- Correlation with crypto: moderate during macro events, low otherwise

### 2. Weather Markets — RECOMMENDED SECOND
- **Series:** KXHIGHNY, KXHIGHCHI, KXHIGHLAX, etc. (19 US cities)
- **Settlement:** NWS CLI temperature reports
- **Estimated edge:** 5-15% per trade (higher than SPX 1-5%)
- **Development estimate:** 4-6 weeks MVP, 8-12 weeks production with EMOS calibration
- **Correlation with crypto:** Zero (pure portfolio diversification)

**Edge thesis:** Kalshi's own blog acknowledges "traders tend to value certainty before they should" — systematic overpricing of high-probability brackets. Market is dominated by retail traders using consumer weather apps (AccuWeather, Apple Weather), while ensemble weather models provide quantifiably better probability distributions.

**Weather forecast accuracy by lead time:**

| Lead time | Best model MAE | Implication for 2°F brackets |
|-----------|---------------|-------------------------------|
| 0-6 hours | ~1.0-1.4°F | Strong predictability; brackets somewhat efficient |
| 6-12 hours | ~1.4°F | Good edge potential |
| 12-24 hours | ~2.2°F | Matches bracket width; maximum uncertainty and edge |
| Day 2 | ~2.7-3.2°F | Brackets poorly constrained; high edge but high risk |

**The HRRR model** (3 km resolution, updated every hour) excels at short-range US temperature forecasting. Combined with ECMWF IFS (9 km, runs 4× daily) and GFS ensembles, you build a probability model that substantially outperforms retail intuition.

**Bracket probability from ensembles:** Fit a Gaussian or logistic distribution to ensemble members, then integrate over each bracket range. Raw ensembles are under-dispersive (overconfident) — post-processing via Ensemble Model Output Statistics (EMOS) improves calibration by the equivalent of "10-20 years of model development" per ECMWF research. Training requires 1-2 years of historical reforecasts (available free via Open-Meteo Historical Forecast API).

**Open-Meteo ensemble API** (free, no API key):
```
https://api.open-meteo.com/v1/ensemble?latitude=40.71&longitude=-74.01&hourly=temperature_2m&models=gfs_seamless
```
Returns 31 GFS ensemble members with hourly resolution. ECMWF (51 members), ICON (40 members), and others also available. Rate limit: 10,000 requests/day.

**Key weather-specific risks:** NWS station microclimate effects (O'Hare airport ≠ downtown Chicago); DST/LST settlement timing nuances; CLI report delays tying up capital; thin orderbooks limiting position sizes to ~$25K per contract.

### 3. Nasdaq 100 Hourly (KXNASDAQ100U) — SKIP
- $86M volume
- ρ > 0.95 correlation with S&P 500
- No diversification benefit if already trading SPX
- Skip entirely as recommended

### 4. EUR/USD, USD/JPY Hourly — LOW PRIORITY
- $2.8M and $1.9M volume respectively
- Low liquidity limits position sizes
- FX vol modeling well-understood but different paradigm from equity/crypto

## Implementation Roadmap (as originally designed)

| Week | SPX Track | Weather Track |
|------|-----------|---------------|
| 1 | Build data pipeline + vol model | — |
| 2 | Deploy observation mode | Begin weather data pipeline (Open-Meteo ensemble API) |
| 3 | Continue observation (~100 windows) | Build naive ensemble → bracket probability model |
| 4 | **Go live** ($50 allocation) | Backtest against historical Kalshi weather prices |
| 5-6 | Scale to $100 if profitable | Implement EMOS calibration layer |
| 7-8 | Scale to $150; evaluate | Weather observation mode (3-5 cities) |
| 9-10 | Steady state | Weather paper trading validation |
| 11-12 | Mature ($150-200 allocation) | **Weather go-live** ($50, 3-5 cities) |

## What Actually Happened

**SPX:**
- Built with dual vol model (EGARCH + HAR-RV)
- Polygon price feed as primary data source
- Briefly promoted live March 17
- Reverted same day: Polygon 403 errors caused zero SPX evaluations for two days
- Circuit breaker + Finnhub fallback deployed
- Now observation-only with HAR-RV shadow running in parallel
- SPX YES-side confirmed no edge above 70c at any tier
- SPX NO-side at 70-79c YES price shows +13.5pp margin on 239 observations (market-level inefficiency, not model selection)

**Weather:**
- Built with 82 NWP ensemble members, 19 US cities
- Per-city CalEngines deployed for learning
- Verdict: NO ALPHA on YES-side (29.8% WR, +32pp overconfidence, Brier 0.3498)
- NO-side shows 73.1% WR but pricing structure doesn't work
- Deprioritized behind crypto improvements and sports

**Nasdaq:**
- Skipped entirely as recommended

## Related (KB operational articles)
- [[kb/concepts/spx-engine.md]]
- [[kb/concepts/weather-system.md]]
- [[kb/failures/polygon-403.md]]
