---
status: fixed
updated: 2026-04-07
tags: [failure, ppo, position-monitoring, orderbook, timezone]
---
# PPO Monitor Bugs — Three Simultaneous Failures (Apr 7, 2026)

## Bugs

PPO (Position Price Observations) monitor launched Apr 5 with three bugs, all present from day one:

### 1. STC Timezone Error
Ticker time is ET (UTC-4 during EDT), but the code tagged it as UTC. Every STC value was exactly 14,400 seconds too low. Correctable retroactively by adding 14,400 to all stored values.

**Code**: Line ~17316 created `_ppo_close` with `tzinfo=timezone.utc` instead of adding 4-hour EDT offset.

### 2. REST Orderbook Field Names (Primary cause of 0% orderbook data)
After the Feb 26 FP transition, Kalshi API returns `yes_ask_dollars`/`yes_bid_dollars` (strings like "0.99"). PPO code used the deprecated `yes_ask`/`yes_bid` fields → always None. The scanner at line 6992 correctly uses `mkt.get("yes_ask_dollars") or mkt.get("yes_ask")` but PPO was never updated.

### 3. WS Stale Threshold Too Aggressive
`POSITION_PRICE_MONITOR_WS_STALE_SEC = 10.0` — thin 15M books near settlement often don't change for >10s. All WS data discarded as stale, falling through to the broken REST path.

### 4. WS Subscription Gap
`_subscribe_discovery_orderbooks()` subscribed active-window tickers but the cleanup cycle unsubscribed expired tickers, including held positions past close. No explicit subscription for position tickers.

## Impact on Data

| Field | Status | Notes |
|-------|--------|-------|
| spot_price | ✓ Valid | 100% non-null |
| threshold | ✓ Valid | 100% non-null |
| spot_buffer_pct | ✓ Valid | 100% non-null — primary analysis signal |
| seconds_to_close | ✓ Correctable | Add 14,400 to all stored values |
| yes_ask_cents | ✗ Missing | 0% — irrecoverable |
| yes_bid_cents | ✗ Missing | 0% — irrecoverable |

**65% of observations (26,195/40,146) are from two stuck positions** (settlement watermark race bug). Excluding those, 13,951 clean observations across 62 tickers covering corrected STC range of -1032s to +511s (last ~8 min of position life).

## Early Findings (from spot-only data)

Despite missing orderbook data, the spot buffer analysis revealed a strong loss predictor:
- **Losses**: 63% of observations show negative buffer (spot below threshold)
- **Wins**: 0.7% of observations show negative buffer
- Simple rule ("exit if >50% of last 30 obs have neg buffer") would have saved 2 losses ($258.90) while clipping 2 near-zero wins ($3.50)
- **n=2 losses is far too small for statistical significance** — need 10-20+ before acting

## Fix (3a6fe53)

1. STC: Added +4h EDT offset to ticker time parsing
2. REST: Uses `yes_ask_dollars`/`yes_bid_dollars` with `dollars_str_to_cents()`
3. WS stale: 10s → 120s
4. Subscription: PPO explicitly subscribes held-position tickers; discovery cleanup protects them

## Lesson

Any code path that touches Kalshi API response fields must use `*_dollars` fields post-FP transition. The scanner was updated but auxiliary systems (PPO) were not. Grep for bare `yes_ask`/`yes_bid` field access after any API format change.
