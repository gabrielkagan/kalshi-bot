---
status: active
updated: 2026-04-01
tags: [failure, ioc, nbbo, unfixed]
severity: minor
---
# IOC Sub-Floor Fill Bug

## Summary
IOC limit orders filling below per-asset MIN_ENTRY_PRICE floors. The floor check uses scan-time NBBO which may be stale by the time the IOC executes, resulting in fills at prices lower than intended. 27 sub-floor fills in one week when discovered. Status: KNOWN UNFIXED.

## Symptom
Settled trades appearing in state.db with `entry_price_cents` below the asset's configured minimum. For example, a BTC trade filling at 85c when BTC_MIN_ENTRY_PRICE is 89c.

## Root Cause
The floor check happens during `scan()` using the NBBO at evaluation time. When an IOC order is submitted, it fills at the best available price on the Kalshi orderbook, which may be at or below the limit price. The sequence:

1. `scan()` sees NBBO yes_ask = 90c, passes BTC floor check (90 >= 89)
2. IOC submitted with limit price = 90c
3. Between scan and execution, orderbook shifts -- best available is 87c
4. IOC fills at 87c (valid -- it is <= limit price of 90c)
5. Result: trade entered at 87c, below BTC_MIN_ENTRY_PRICE of 89c

The core issue is that IOC limit orders guarantee a maximum price, not a minimum. The floor check at scan time does not protect against fills at lower prices when the book moves.

## Scale
- 27 sub-floor fills in one week (initial discovery, Mar 27)
- 170 total sub-floor fills identified historically
- Net PnL on sub-floor fills: +$54.59 (profitable overall, not a money-losing bug)
- Only 2 sub-floor fills since BLR passthrough switch (dormant under current calibration)

## Why Not Fixed
1. Net profitable -- the fills that happen to be sub-floor are not systematically losers
2. Dormant under current passthrough calibration regime (only 2 post-BLR fills)
3. Fix options are all suboptimal:
   - Post-fill price check + cancel: IOC fills are immediate, no cancel window
   - Tighter limit price: reduces fill rate, costs more than sub-floor fills lose
   - Real-time NBBO refresh before IOC: adds latency, NBBO can still shift during submission

## Current Per-Asset Floors (for reference)
| Asset | MIN_ENTRY_PRICE | Notes |
|-------|----------------|-------|
| BTC | 89c | Data: 86-88c below taker BE |
| ETH | 90c | Raised from 85c -- 85-89c was -$23.76 PnL |
| SOL | 80c | Explicit floor, prevents 75-79c |
| XRP | 92c | PnL negative at every floor <90c |

## Risk Assessment
Structurally wrong but practically harmless under current config. Could become material if:
- Calibration changes cause more aggressive pricing near floors
- A high-volatility regime produces frequent orderbook gaps
- Per-asset floors are tightened to where sub-floor = truly bad prices
- SOL taker-first mode (SOL_TAKER_FIRST=True) increases IOC volume, increasing sub-floor exposure

## NBBO Fallback Warning
The `_nbbo_fallback_price()` function echoes scan-time price, NOT a fresh REST call. This compounds the staleness problem -- when the WS orderbook is empty and NBBO fallback activates, the price used for floor checks can be arbitrarily stale.

## Monitoring
Query to find sub-floor fills:
```sql
SELECT asset, entry_price_cents, settled_at
FROM settled_trades
WHERE (asset='BTC' AND entry_price_cents < 89)
   OR (asset='ETH' AND entry_price_cents < 90)
   OR (asset='SOL' AND entry_price_cents < 80)
   OR (asset='XRP' AND entry_price_cents < 92)
ORDER BY settled_at DESC;
```

## Related
- [[concepts/execution-layer.md]] (IOC execution, maker-first escalation)
- [[concepts/per-asset-rules.md]] (per-asset MIN_ENTRY_PRICE floors)
