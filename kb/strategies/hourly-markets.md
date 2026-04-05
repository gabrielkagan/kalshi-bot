---
status: active
updated: 2026-04-01
tags: [strategy, hourly, sub-60c, live]
---
# Hourly Markets Strategy

## Summary
Hourly crypto markets (KXBTCD, KXETHD) traded live since March 21, 2026. Sub-60c BTC+ETH only, taker-only IOC, fixed 10-contract sizing, temperature-scaled probabilities (T=1.45), STC 600-1800s. Promoted after 66.3% WR vs 46.5% breakeven on 1,474 unique tickers over 14 days.

## Key Differences from 15M
| Aspect | 15M | Hourly |
|--------|-----|--------|
| Price range | 75-99c | 50-59c |
| Sizing | Kelly | Fixed 10 contracts |
| Execution | Maker-first | Taker-only IOC |
| Calibration | Passthrough | Disabled + T=1.45 |
| Bankroll fraction | 100% | 10% |
| Assets | BTC, ETH, SOL, XRP | BTC, ETH only |

## Three-Layer Optimization
| Layer | Purpose | Config |
|-------|---------|--------|
| Temperature scaling (T=1.45) | Softens overconfident 15M calibration that doesn't transfer | 95% raw becomes 88.4% |
| STC timing (600-1800s) | Sweet spot: 10-30 min (25-30m zone at 69.4% WR) | `HOURLY_MIN_STC_ENTRY=600`, `HOURLY_MAX_STC_ENTRY=1800` |
| Asset exclusion | XRP 42.9% WR = toxic, SOL marginal | `HOURLY_EXCLUDED_ASSETS = {SOL, XRP}` |

Additional filters:
- Per-window position limit: 2 (ENB ~1.3 independent bets per window)
- Per-window risk cap: 15% (`HOURLY_MAX_WINDOW_RISK`)
- Max edge cap: 5% (`HOURLY_MAX_EDGE`) — 10%+ zone has 24.2% WR (edge inversion)

## Kill Switch
`HOURLY_LIVE_ENABLED` env var on VPS must be "1". Default "0" = observation mode. `HOURLY_OBSERVATION_ONLY = not HOURLY_LIVE_ENABLED`.

## Why Fixed Sizing
Kelly at sub-60c prices with noisy probability estimates produces erratic position sizes. Fixed 10-contract sizing provides stable risk exposure. Quarter-Kelly (`HOURLY_KELLY_FRACTION = 0.25`) exists in code but is unused.

## Why Taker-Only
`HOURLY_TAKER_ONLY = True` — no maker orders. Avoids per-asset lock contention with the 15M system, which runs simultaneously. Maker fill rates at hourly price levels (sub-60c) are poor.

## Calibration
`HOURLY_CALIBRATION_ENABLED = False` — the CalibrationEngine is disabled for hourly. Instead, raw probabilities are softened by temperature scaling (T=1.45), which is simpler and more predictable than learned calibration on a product type with different characteristics.

Hourly data is excluded from 15M CalEngine training (`load_training_data_from_db()` filters by product_type).

## Bankroll Isolation
`HOURLY_BANKROLL_FRACTION = 0.10` — hourly sizes off 10% of total balance. This prevents hourly trading from affecting 15M sizing and limits total hourly exposure.

## Shadow Configs Killed
Hourly shadow configs h/j/k killed — all showed ~55% WR, no edge.

## Hourly DC Shadow Variants
- 97c+ z <= -3 STC <= 600s (a116b77) — shadow
- 93-96c z <= -3 STC <= 300s (5125316) — shadow

## Related
- [[concepts/execution-layer.md]]
- [[concepts/per-asset-rules.md]]
