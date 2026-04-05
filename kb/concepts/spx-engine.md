---
status: active
updated: 2026-04-03
tags: [spx, egarch, har-rv, observation]
---
# SPX Hourly Market Engine

## Summary
The SPX engine (`spx_engine.py`) trades S&P 500 hourly above/below markets on Kalshi (series KXINXU). Uses a dual volatility model: EGARCH from the main engine and an experimental HAR-RV shadow (`spx_harrv_shadow.py`). Currently observation-only (`SPX_HOURLY_OBSERVATION_ONLY = True`) after a Polygon.io 403 failure broke the vol engine during a brief live period on Mar 17, 2026. Finnhub WebSocket is now the primary price feed with REST fallback.

## Architecture
- **`spx_engine.py`**: Main engine -- price feed, EGARCH volatility, market hours guard, VIX integration
- **`spx_harrv_shadow.py`**: Shadow strategy -- HAR-RV (Heterogeneous Autoregressive Realized Volatility) model
- Both run as components within the bot's main loop, not as separate daemon threads
- NYSE market hours enforced via `MarketHoursGuard` with holiday calendar and FOMC date awareness
- 10-minute warmup after market open (`WARMUP_MINUTES = 10`) to avoid opening auction noise

## Price Feed
- **Primary**: Finnhub WebSocket pushing tick-by-tick SPY trades, converted via `SPY_TO_SPX_RATIO = 10.03`
- **Fallback**: Finnhub REST polling at 3-second intervals when WebSocket is down
- **Polygon.io removed as primary**: Was the original source but persistent 403 errors made it unreliable (see [[failures/polygon-403.md]])
- **VIX**: Polled via Finnhub REST every 60 seconds (`VIX_POLL_INTERVAL = 60`)
- **Stale detection**: Price considered stale after 180 seconds (`STALE_PRICE_THRESHOLD`)
- **Zero-return guard**: When on REST fallback, 3-second polls produce duplicate prices that collapse EGARCH sigma. Engine freezes EGARCH at last persisted sigma and skips RK during REST-only mode.

## EGARCH Volatility Model
- SPX-specific EGARCH with stronger leverage asymmetry: `SPX_EGARCH_GAMMA_BOUNDS = (-0.30, -0.05)` (4x stronger than crypto)
- Refit interval: 4 hours (`SPX_EGARCH_REFIT_INTERVAL = 14400`) -- less frequent than crypto due to lower volatility
- State persisted to `spx_egarch_state.json`
- Minimum 100 observations before first estimate (`SPX_EGARCH_MIN_OBSERVATIONS`)
- RK (Realized Kernel) with Parzen bandwidth of 10 (`SPX_RK_PARZEN_BANDWIDTH`)
- Mincer-Zarnowitz window of 50 for EGARCH blend weight (`SPX_MZ_WINDOW`)

## HAR-RV Shadow Strategy
The HAR-RV model (`spx_harrv_shadow.py`) runs in parallel as a 2-week data collection experiment (created Mar 6, 2026):
- Accumulates its own return buffer (5-second returns, separate from EGARCH)
- Computes RV at three frequencies: 1h, 1d (trading day), 1w (5 trading days)
- Forecasts next-hour RV: `RV_f = b0 + b1*RV_1h + b2*RV_1d + b3*RV_1w` (Corsi 2009 priors)
- Converts forecast to probability via lognormal CDF
- Applies temperature scaling + heavy market blend
- Multi-gate abstention system with per-gate logging
- Logs to `spx_harrv_shadow_signals` table
- **Zero-return filtering**: Returns with `abs(r) < 1e-12` are excluded from RV computation to prevent collapsed estimates from stale prices

## VIX Integration
VIX divergence threshold at 30% (`VIX_DIVERGENCE_THRESHOLD = 0.30`). When realized vol diverges significantly from VIX-implied vol, the engine shifts toward implied vol. This helps during regime changes when realized vol lags.

## Intraday Seasonal Filter
EWMA-based seasonal adjustment (`SEASONAL_EWMA_LAMBDA = 0.97`, ~23-day half-life) across 13 half-hour buckets (9:30-16:00 ET). Captures intraday volatility patterns (e.g., higher vol at open and close).

## Polygon 403 Failure (Mar 17, 2026)
SPX hourly was briefly promoted to live trading on Mar 17. Polygon.io 403 errors broke the price feed, producing zero evaluations. Reverted to observation-only the same day. Finnhub is now the sole price source. See [[failures/polygon-403.md]] for full post-mortem.

## Configuration
| Parameter | Value | Notes |
|-----------|-------|-------|
| `SPX_HOURLY_OBSERVATION_ONLY` | True | Reverted from live after Polygon 403 |
| `SPX_HOURLY_MIN_ENTRY_PRICE` | 90c | 90.9% WR at 90c+ in observation data |
| `SPX_HOURLY_MAX_ENTRY_PRICE` | 99c | |
| `SPX_HOURLY_MARKET_BLEND_W` | 0.00 | No market blend -- CalEngine calibration only |
| `SPX_HOURLY_TEMPERATURE_T` | 1.0 | No temperature correction yet |
| `SPX_HOURLY_KELLY_FRACTION` | 0.125 | Eighth-Kelly: ultra-conservative |
| `SPX_HOURLY_MAX_RISK_PER_TRADE` | 0.10 | Conservative risk cap |
| `SPX_HOURLY_BANKROLL_FRACTION` | 0.15 | Sizes off 15% of total balance |
| `SPX_HOURLY_FEE_MULTIPLIER_TAKER` | 0.035 | Finance category -- half of crypto's 0.07 |
| `SPX_HOURLY_FEE_MULTIPLIER_MAKER` | 0.0 | $0 maker fee |
| `SPX_HOURLY_MAX_POSITIONS_PER_WINDOW` | 2 | Prevent correlated multi-strike exposure |
| `SPX_HOURLY_MAX_WINDOW_RISK` | 0.15 | Max aggregate risk per window |

## CalEngine Integration
SPX hourly uses the SPX-D CalEngine, registered in `_CAL_REGISTRY` as `"spx_hourly"`. State persisted to `spx_hourly_calibration_state.json`. CalEngine is enabled (`cal_engine_enabled = True`) and uses learned temperature rather than a fixed T parameter. Market blend weight is 0.00 because the CalEngine handles all calibration.

## Related
- [[failures/polygon-403.md]]
- [[concepts/cal-engine-registry.md]]
- See `kb-research/bot/market-expansion-analysis.md` for SPX market ranking and expansion analysis
