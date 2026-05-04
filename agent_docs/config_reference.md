# Config Reference

All values mirror constants in `bot.py`. `market_config.py` asserts they match at startup.
On change, run `python3 scripts/doc_drift_check.py` and update this file in the same commit.

## Global / 15M

| Config | Value | Notes |
|--------|-------|-------|
| OBSERVATION_MODE | False | LIVE trading |
| MIN_ENTRY_PRICE | 75 | Cents (global floor — lowered from 80 for ETH 75-79c) |
| BTC_MIN_ENTRY_PRICE | 88 | Cents (88c = 96.2% WR on n=53 shadow, 96.3% on n=27 recent) |
| ETH_MIN_ENTRY_PRICE | 90 | Cents (raised from 85 — ETH 85-89c 86.2% WR, -$23.76 PnL; 90c+ 95.2% WR) |
| SOL_MIN_ENTRY_PRICE | 86 | Cents (raised from 80: SOL@85c 68.2% WR -$496/22 vs 86c 94.4% WR +$375/36) |
| ETH_SUB80_POSITION_CAP | 50 | Max contracts for ETH 75-79c (half-Kelly clamp [20,50]) |
| XRP_MIN_ENTRY_PRICE | 92 | Cents (PnL negative at every floor <90c, PF=1.68 at ≥92c) |
| MAX_ENTRY_PRICE | 99 | Cents |
| MIN_EDGE_PCT | 0.25 | Flat fallback for execution paths (was 0.7) |
| MIN_EDGE_BY_PRICE | 0.20%-1.0% | 80-88c→0.25%, 89-90c→0.25%, 91-92c→0.20%, 93-94c→0.50%, 95-96c→0.75%, 97-99c→1.0% |
| MARKET_BLEND_W | 0.40 | 60% model, 40% market (model underconfident 0.8-2.1pp at 90%+) |
| MAX_RISK_PER_TRADE | 0.25 | Max 25% bankroll per trade |
| MAX_SECONDS_BEFORE_CLOSE | 900 | 15 min before close (600-900s shadow, 0-600s live) |

## STC zones

| Config | Value | Notes |
|--------|-------|-------|
| STC_SHADOW_THRESHOLD | 600 | 15M trades above this STC are shadow-only |
| STC_EXTENDED_LIVE_FLOOR | 300 | 300-600s zone: per-asset higher floors apply |
| STC_EXTENDED_BTC_MIN_PRICE | 93 | BTC floor for 300-600s (93c+ = 98.1% WR, n=52) |
| STC_EXTENDED_ETH_MIN_PRICE | 90 | ETH floor for 300-600s (same as main floor) |
| STC_EXTENDED_SOL_MIN_PRICE | 95 | SOL floor for 300-600s (95c+ = 100% WR, n=14) |
| STC_EXTENDED_XRP_MIN_PRICE | 92 | XRP floor for 300-600s (same as main floor) |
| SOL_LOW_ENTRY_STC_GATE | True | Block SOL ≤85c at STC≥300s (78.3% WR -$289; <300s is 100% WR +$228) |
| HIGH_PRICE_STC_BLOCK_ENABLED | env-default `0` | 96¢ × {SOL,XRP} × 2-5min STC strategy-aware filter; saves $895/30d |
| HIGH_PRICE_STC_BLOCK_ASSETS | {SOL, XRP} | Cell scope; BTC/ETH 96¢ profitable, untouched |
| HIGH_PRICE_STC_BLOCK_PRICE_CENTS | 96 | Exact match — DO NOT widen, see KB |
| HIGH_PRICE_STC_BLOCK_STC_LO_S | 121 | STC inclusive lower bound |
| HIGH_PRICE_STC_BLOCK_STC_HI_S | 300 | STC inclusive upper bound |
| HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES | {decided_t2, decided_t2_z2, decided_t2_z25, MAKER_PATIENT} | Strategies dropped within cell; wins (TM-96, TAKER_NOW, decided_t1*) preserved |
| TM98_HIGHPRICE_BLEED_BLOCK_ENABLED | env-default `0` | {BTC,ETH,XRP} × TM-98 × 97-98¢ × 121-300s STC; -$980/30d projected (R-bleed-1) |
| TM98_HIGHPRICE_BLEED_BLOCK_ASSETS | {BTC, ETH, XRP} | SOL TM-98 NOT catastrophic, untouched |
| TM98_HIGHPRICE_BLEED_BLOCK_PRICE_LO | 97 | Covers maker fill 1c below TM trigger |
| TM98_HIGHPRICE_BLEED_BLOCK_PRICE_HI | 98 | TM-99 NOT blocked (profitable per 14d) |
| TM98_HIGHPRICE_BLEED_BLOCK_STC_LO_S | 121 | Lower bound of 2-5min danger zone (inclusive) |
| TM98_HIGHPRICE_BLEED_BLOCK_STC_HI_S | 300 | Upper bound of 2-5min danger zone (inclusive) |
| TM98_HIGHPRICE_BLEED_BLOCK_STRATEGIES | {terminal_momentum_98} | Strategy-aware; TM-99 in same range = profitable, untouched |
| SOL_TAKER_LOWPRICE_BLEED_BLOCK_ENABLED | env-default `0` | SOL × TAKER_NOW × 85-89¢ × 121-300s STC; -$782/30d projected (R-bleed-1) |
| SOL_TAKER_LOWPRICE_BLEED_BLOCK_ASSETS | {SOL} | Other assets do not have this bleed pattern |
| SOL_TAKER_LOWPRICE_BLEED_BLOCK_PRICE_LO | 85 | Near-asset-floor thin-buffer disaster zone |
| SOL_TAKER_LOWPRICE_BLEED_BLOCK_PRICE_HI | 89 | 90+¢ TAKER profitable, untouched |
| SOL_TAKER_LOWPRICE_BLEED_BLOCK_STC_LO_S | 121 | Lower bound of 2-5min danger zone (inclusive) |
| SOL_TAKER_LOWPRICE_BLEED_BLOCK_STC_HI_S | 300 | Upper bound of 2-5min danger zone (inclusive) |
| SOL_TAKER_LOWPRICE_BLEED_BLOCK_STRATEGIES | {TAKER_NOW} | MAKER cohorts not blocked |
| BINANCE_FEED_ENABLED | env-default `0` | US-VPS HTTP-451 geoblocked; CROSS_EXCHANGE_CONSENSUS_MIN auto-lowers to 2 (Kraken+Bybit) when off |
| STC_SIZING_SCALER_KNEE | 300 | Seconds — start scaling contracts by 300/STC above this |
| STC_SIZING_SCALER_ENABLED | True | Universal STC scaler: contracts *= 300/STC for 15M at STC>300s |

## Per-asset risk

| Config | Value | Notes |
|--------|-------|-------|
| XRP_MAX_RISK_PER_TRADE | 0.15 | XRP: 15% per-trade (was 12%) |
| BTC_MAX_RISK_PER_TRADE | 0.15 | BTC: 15% per-trade (was 12%) |
| SOL_MIN_EDGE | 0.010 | SOL-specific edge floor (>=1.0% = 94.2% WR n=258; <1.0% drops to 82%) |
| SOL_TAKER_FIRST | True | SOL bypasses maker entirely, direct IOC at all STC |
| SOL_RESCUE_CONTRACT_CAP | 25 | SOL rescue sizing clamp |

## Loss cooldown / LPNE / monitor

| Config | Value | Notes |
|--------|-------|-------|
| LOSS_COOLDOWN_ENABLED | True | Per-asset 2h 15M lockout after any loss (+$441/30d counterfactual) |
| LOSS_COOLDOWN_SECONDS | 7200 | 2-hour cooldown — first-ship per-asset (conservative) |
| LPNE_ENABLED | True (env var) | BTC 80-87c near-expiry overlay (STC 10-120s, prob >= price/100) |
| LPNE_FIXED_CONTRACTS | 50 | Fixed sizing for LPNE |
| LPNE_MAX_CONCURRENT | 2 | Max simultaneous LPNE positions |
| POSITION_PRICE_MONITOR_ENABLED | True | Post-entry price logging via WS (change-only dedup) |

## Execution

| Config | Value | Notes |
|--------|-------|-------|
| IOC_TICKER_COOLDOWN | 15 | Seconds cooldown per ticker after IOC attempt (was 60) |
| IOC_RETRY_OFFSET | 1 | Cents above ask for taker-first IOC + retry offset |
| DIP_ADDON_ENABLED | False | Killed — 55.2% WR, no edge |

## Decided contracts

| Config | Value | Notes |
|--------|-------|-------|
| DECIDED_T1_ENABLED | True | Overlay: z≤-5, any price (env var) |
| DECIDED_CONTRACT_Z_T1B | -4.0 | T1B z-score threshold |
| DECIDED_CONTRACT_T1B_MIN_PRICE | 95 | T1B minimum price in cents |
| DECIDED_T1B_ENABLED | True | Overlay: z≤-4, 95c+ (env var) |
| DECIDED_T2_ENABLED | True | Overlay: z≤-3, 93-96c (env var) |
| DECIDED_T2_Z25_ENABLED | True | Overlay: z≤-2.5, 93-96c (7/7 WR) |
| DECIDED_T2_Z2_ENABLED | False | SHADOWED Apr 1 — z∈[-2.5,-1.75], -$313 on 47 trades |
| DECIDED_CONTRACT_Z_T2_Z2 | -1.75 | Expanded from -2.0 pre-shadow |
| DECIDED_CONTRACT_RISK | 0.20 | Fixed 20% bankroll for T1/T1B/T2 |
| DECIDED_CONTRACT_T2_Z25_RISK | 0.10 | T2-Z25 cut from 0.20 Apr 21 (14d bleed -$95) |
| DECIDED_CONTRACT_T2_Z2_RISK | 0.20 | Moot — shadowed |
| DECIDED_CONTRACT_MAX_WINDOW_RISK | 0.35 | Hard cap across all DC signals in one window |
| SOL_DC_RISK_TIERS | [(97, 0.05), (95, 0.10)] | SOL price-tiered DC risk: ≥97c→5%, 95-96c→10%, <95c→20% |

Canonical reference: `kb/concepts/dc-strategy.md`

## Discount strategies

| Config | Value | Notes |
|--------|-------|-------|
| OVERNIGHT_DISCOUNT_LIVE | True | Weekday 04-11 UTC live (kill switch) |
| OVERNIGHT_DISCOUNT_MIN_PRICE | 89 | Cents — 89c+ floor for live |
| OVERNIGHT_DISCOUNT_MAX_STC | 600 | STC gate for live |
| WEEKEND_DISCOUNT_LIVE | True | Sat/Sun live |
| WEEKEND_DISCOUNT_MIN_PRICE | 90 | Cents (raised from 89 to match ETH floor) |
| WEEKEND_DISCOUNT_MAX_STC | 600 | STC gate for live |
| WEEKEND_EDGE_DISCOUNT | 0.60 | 40% edge reduction applied on weekends |

## Low-Price Shadow (observation-only; Phase C of shadow coverage expansion 2026-05-02)

`low_price_shadow` is shadow-only data collection — never affects live trades. `MIN_ENTRY_PRICE` (live floor) is unchanged. See `kb/decisions/shadow-coverage-expansion-may01.md`.

| Config | Value | Notes |
|--------|-------|-------|
| LOW_PRICE_SHADOW_ENABLED | True | Master kill switch |
| LOW_PRICE_SHADOW_MIN_PRICE | 20 | Floor (was 70 pre-Phase-C; 20 leaves room for far-from-BE training data) |
| LOW_PRICE_SHADOW_MAX_PRICE | 79 | Ceiling (80c+ already live for some assets) |
| LOW_PRICE_SHADOW_MAX_STC | 900 | Full scan-window (was 600; captures entire decision life) |
| LP_MAX_RISK_PER_TRADE | 0.10 | Capped sizing — 10% bankroll cap |
| LP_KELLY_FRACTION | 0.25 | Capped sizing — quarter-Kelly |
| LP_WINDOW_CAP | 2 | Max signals per 15M window (correlation cap) |
| LP_HOUR_CAP | 4 | Max signals per hour (correlation cap) |

## Hourly (DISABLED Apr 18 — both kill switches 0)

To re-enable: set `HOURLY_LIVE_ENABLED=1` (YES) and/or `HOURLY_NO_SIDE_LIVE=1` (NO) in VPS .env + restart.

| Config | Value | Notes |
|--------|-------|-------|
| HOURLY_LIVE_ENABLED | env var (0 on VPS) | Kill switch for YES-side |
| HOURLY_OBSERVATION_ONLY | not HOURLY_LIVE_ENABLED | Derived from kill switch |
| HOURLY_MAX_ENTRY_PRICE | 59 | Sub-60c only — edge lives at low prices, 70-79c death zone |
| HOURLY_BANKROLL_FRACTION | 0.10 | Hourly sizes off 10% of balance |
| HOURLY_FIXED_CONTRACTS | 25 | Fixed sizing — bypass Kelly entirely |
| HOURLY_MAX_EDGE | 0.05 | Reject >5% edge (10%+ zone has 24.2% WR — edge inversion) |
| HOURLY_NO_SIDE_LIVE | env var (0 on VPS) | Kill switch for NO-side |
| HOURLY_NO_MIN_PRICE | 40 | Minimum NO entry price (cents) |
| HOURLY_NO_MAX_PRICE | 54 | Maximum NO entry price (cents) |
| HOURLY_NO_FIXED_CONTRACTS | 1 | Flat 1-contract — verification mode |
| HOURLY_NO_KILL_THRESHOLD | -2000 | Auto-disable if cumulative hourly NO PnL < -$20 |
| HOURLY_TAKER_ONLY | True | IOC only — no maker orders |
| HOURLY_MARKET_BLEND_W | 0.40 | Optimal Brier per 134K simulation |
| HOURLY_MIN_ENTRY_PRICE | 50 | Floor for data collection |
| HOURLY_MAX_RISK_PER_TRADE | 0.15 | 60% of 15M's 0.25 |
| HOURLY_TEMPERATURE_T | 1.45 | Softens overconfident probs: 95%→88.4% |
| HOURLY_KELLY_FRACTION | 0.25 | Quarter-Kelly (unused — fixed sizing active) |
| HOURLY_CALIBRATION_ENABLED | False | Engine disabled — passthrough + T=1.45 |
| HOURLY_MIN_STC_ENTRY | 600 | 10 min minimum (5-10m zone 56.5% WR — too thin) |
| HOURLY_MAX_STC_ENTRY | 1800 | 30 min maximum (25-30m sweet spot at 69.4% WR) |
| HOURLY_EXCLUDED_ASSETS | {SOL, XRP} | BTC+ETH only — XRP 42.9% WR (toxic), SOL marginal |
| HOURLY_MAX_POSITIONS_PER_WINDOW | 2 | Limit correlated exposure |
| HOURLY_MAX_WINDOW_RISK | 0.15 | Max aggregate risk per hourly window |

## SPX Hourly (observation)

| Config | Value | Notes |
|--------|-------|-------|
| SPX_HOURLY_OBSERVATION_ONLY | True | Reverted Mar 17 — Polygon 403 broke vol engine |
| SPX_HOURLY_MIN_ENTRY_PRICE | 90 | Cents (SPX-C: 90.9% WR at 90c+) |
| SPX_HOURLY_MAX_ENTRY_PRICE | 99 | Cents |
| SPX_HOURLY_MARKET_BLEND_W | 0.00 | No blend — CalEngine calibration only (SPX-D) |
| SPX_HOURLY_TEMPERATURE_T | 1.0 | No temperature correction yet |
| SPX_HOURLY_KELLY_FRACTION | 0.125 | Eighth-Kelly: ultra-conservative |
| SPX_HOURLY_MAX_RISK_PER_TRADE | 0.10 | Conservative (down from 0.15) |
| SPX_HOURLY_FEE_MULTIPLIER_TAKER | 0.035 | Finance category — half of crypto's 0.07 |
| SPX_HOURLY_FEE_MULTIPLIER_MAKER | 0.0 | Kalshi charges $0 on maker fills |
| SPX_HOURLY_BANKROLL_FRACTION | 0.15 | SPX sizes off 15% of total balance |
| SPX_HOURLY_MAX_POSITIONS_PER_WINDOW | 2 | Prevent correlated multi-strike blowups |
| SPX_HOURLY_MAX_WINDOW_RISK | 0.15 | Max aggregate risk per SPX window |

## Weather

| Config | Value | Notes |
|--------|-------|-------|
| WEATHER_OBSERVATION_ONLY | True | Observation-only — collecting ensemble data |
| WEATHER_MIN_ENTRY_PRICE | 10 | Cents |
| WEATHER_MAX_ENTRY_PRICE | 99 | Cents |
| WEATHER_MARKET_BLEND_W | 0.20 | 80% model, 20% market |
| WEATHER_MIN_EDGE_PCT | 0.001 | 0.1% — observation-only |
| WEATHER_MAX_RISK_PER_TRADE | 0.10 | Conservative sizing |
| WEATHER_KELLY_FRACTION | 0.25 | Quarter-Kelly |
| WEATHER_MIN_SECONDS_BEFORE_CLOSE | 3600 | At least 1 hour before settlement |
| WEATHER_MAX_SECONDS_BEFORE_CLOSE | 86400 | Weather settles daily — always eligible |
| WEATHER_NO_SIDE_LIVE | True | LIVE — NO 39-40c, STC ≥ 16h, 1-contract |
| WEATHER_NO_MIN_PRICE | 39 | Tightened May 2 from 37 — 37c/38c sub-bands bleeding (25%/14.3% WR), 39-40c band profitable |

## Calibrator (P2 cal_mlp)

| Config | Value | Source | Notes |
|--------|-------|--------|-------|
| `SIGMA_WINSOR_ABS_CAP` | 25.0 | `scripts/cal_mlp/features.py` | Clip for `spot_distance_to_strike_sigma` at extract + post-hoc + sync-gate. Below empirical max benign (~19); above the 30+ outlier tail. Captured in cfg_fp; changing it changes the bundle fingerprint. |
| `GLOBAL_MIN_ENTRY_PRICE` | 75 | `features.py` | Floor when `--include-sub-floor` is set. Per-asset floors (88/90/86/92) used otherwise. |
| `INCLUDE_SUB_FLOOR` (env) | 1 | `scripts/cal_mlp/run_pipeline.sh` | Default ON. Pulls 75¢-MIN_ENTRY-1¢ shadow rows into v2/v3 training. |
| `RAW_PROB_CLIP_EPS` | 1e-6 | `features.py` | Logit clipping for the skip term. |
| `SPOT_BUFFER_PERSIST_PATH` | `state/spot_buffer.json` | `bot.py` | 30-min spot price buffer persisted to disk every 30s. |
| `SPOT_BUFFER_PERSIST_INTERVAL_S` | 30 | `bot.py` | Flush cadence. Async via `asyncio.to_thread` so disk I/O doesn't block WS event loop. |
| `PRICE_BUFFER_SIZE` | 1800 | `bot.py` | 30 min @ 1s sampling. |

## External market data poller

| Config | Source | Notes |
|--------|--------|-------|
| OKX funding + OI | `https://www.okx.com/api/v5/public/{funding-rate,open-interest}` | Switched from Binance.com (HTTP 451 from US). 4 symbols × 2 endpoints. |
| Deribit DVOL | `https://www.deribit.com/api/v2/public/get_index_price?index_name={btcdvol_usdc,ethdvol_usdc}` | BTC + ETH only. |
| Poll interval | 60 s | `scripts/external_market_poller.py --once` cron. |
| Stale threshold | 900 s (15 min) | Above Deribit's typical weekly maintenance window (~10 min). |
| Process-start grace | 900 s | Suppresses NEVER-stale alerts in cron mode for the first 15 min. |
