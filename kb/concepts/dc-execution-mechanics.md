---
status: active
updated: 2026-04-01
tags: [execution, dc, retry-queue, ioc]
---
# Decided Contract Execution Mechanics

## Summary
DC orders always execute as direct taker IOC -- never maker. On unfilled or partial fills, a non-blocking retry queue re-attempts the order at the top of each `_tick()` cycle, with price widening on later retries. Fill rate is approximately 22%, but 24% of DC tickers recover within 8-32 seconds, making retries net positive.

## Execution Flow (`_execute_dc_taker`, bot.py ~13013)
1. Validate `count > 0` and `net_edge >= -0.01` (fee-adjusted)
2. Fresh ask check via `_dc_get_ask_with_depth()` -- verifies price, depth, and source
3. Price floor gate: abort if `fresh_ask < DECIDED_CONTRACT_MIN_PRICE`
4. If fresh ask differs from scan price, recalculate edge with updated price
5. Submit IOC via `_submit_taker()`
6. On full fill: done. On partial fill: queue retry for remaining contracts. On zero fill: queue retry for full count.

## Non-Blocking Retry Queue
The `_dc_retry_queue` (list on `OrderExecutor`, line 11617) holds pending retries. `process_dc_retries()` runs at the top of each `_tick()` -- each retry is a single IOC submission (<1s), so it never blocks the main loop.

**Retry parameters (bot.py ~578-581):**
| Constant | Value | Purpose |
|----------|-------|---------|
| DC_IOC_RETRY_DELAY | 8s | Default delay between attempts (fallback) |
| DC_IOC_MAX_RETRIES | 10 | Max retries per ticker (11 total attempts) |
| DC_PRICE_TOLERANCE_START_RETRY | 3 | Retry index where price widening begins |
| DC_PRICE_TOLERANCE_MAX | 3 | Max cents above original target price |

**Lifecycle:** Initial attempt -> if unfilled/partial, append to queue with `next_retry_ts = now + 8s` -> on next tick after delay expires, attempt retry -> repeat until filled or 11 attempts exhausted.

## Price Widening
Retries 0-2 (attempts 2-4) use exact fresh ask price. Starting at retry 3 (attempt 5), the IOC price widens by 1 cent per retry up to DC_PRICE_TOLERANCE_MAX (3 cents). Formula at line 13436:
```
offset = min(retry_num - DC_PRICE_TOLERANCE_START_RETRY + 1, DC_PRICE_TOLERANCE_MAX)
price = min(fresh_ask + offset, MAX_ENTRY_PRICE)
```

## Safety Gates on Each Retry
Each retry re-checks before submitting:
- **Fresh ask available** -- if no asks, re-queue with adaptive delay
- **Price floor** -- abort if `fresh_ask < DECIDED_CONTRACT_MIN_PRICE`
- **Price drift** -- abort if ask dropped 3+ cents from original signal price
- **Edge check** -- skip if `net_edge < -0.01` after fee recalculation
- **Max attempts** -- drop from queue after 11 total attempts
- **Phantom depth flag** -- logged but never blocks (depth=0 + NBBO source)

## Window Risk Cap
`DECIDED_CONTRACT_MAX_WINDOW_RISK = 0.35` (35% of bankroll per window) acts as a scan-time pre-filter. Before a DC candidate enters execution, the scanner checks aggregate DC exposure across all strategies in the same window. Existing positions reduce new DC sizing -- the position subtraction ensures additive exposure stays within the 35% cap.

## Fill Rate Data
- Overall DC fill rate: approximately 22%
- 24% of DC tickers that initially fail recover asks within 8-32 seconds
- Retry queue captures this recovery window across 11 attempts over ~80 seconds
- Partial fills are tracked cumulatively (`total_filled` / `original_count`)

## Related
- [[concepts/dc-strategy.md]]
- [[concepts/execution-layer.md]]
- [[failures/t2-z2-losses.md]]
