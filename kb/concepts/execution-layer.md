---
status: active
updated: 2026-04-02
tags: [execution, maker, taker, escalation]
---
# Execution Layer

## Summary
The execution architecture uses a maker-first strategy with taker escalation, per-asset overrides, NBBO fallback for empty orderbooks, and per-ticker locking to prevent duplicate positions. The `evaluate_execution_strategy()` function is diagnostic only and never controls actual execution flow.

## Default Execution Path: Maker-First
1. **Post-only maker order** at `best_ask - MAKER_PRICE_OFFSET` (1 cent below)
2. **Poll for fill** every `MAKER_POLL_INTERVAL` (2s) via WS fill detection + REST fallback
3. **Escalate to taker** via cancel-replace IOC if unfilled after timeout
4. Maker fee: $0 (Kalshi charges nothing on maker fills)
5. Taker fee: `ceil(0.07 * C * P * (1-P))` per contract

## Direct Taker Threshold
When `seconds_to_close < DIRECT_TAKER_THRESHOLD` (180s), skip maker entirely and submit IOC directly. Data: 0% maker fill rate at short STC (26/26 escalated to taker), 9 missed candidates/day from the delay.

## SOL Taker-First Override
`SOL_TAKER_FIRST = True` — SOL bypasses maker entirely at all STC. Data: 44.7% maker fill rate for SOL, $101/week missed opportunity cost, 95% unfilled WR. Taker fee delta ~$2/week vs $101 missed.

**SOL empty-book maker fallback:** When SOL orderbook is empty (depth=0), uses maker at 87c+ with STC >= 60s. Data: 400 unfilled at depth=0, 95% WR.

## NBBO Fallback
When WS orderbook is empty, falls back to market endpoint `yes_ask` with per-asset gates:
- BTC: 86-99c, STC <= 300s (97.9% WR)
- ETH: 90-99c, STC <= 300s
- SOL: 86-99c, STC <= 300s (93.3% WR; 80-85c is 50-73% WR trap)
- XRP: 92-99c, STC <= 300s

**Warning:** `_nbbo_fallback_price()` echoes scan-time price, not a fresh REST call.

## Adaptive Escalation Timeouts
| STC Range | Wait Before Escalation |
|-----------|----------------------|
| >= 180s | 15s (`ESCALATION_WAIT_LONG`) |
| >= 180s (BTC) | 7s (`BTC_ESCALATION_WAIT_OVERRIDE`) |
| 120-180s | 7s (`ESCALATION_WAIT_MEDIUM`) |
| 60-120s | 5s (`ESCALATION_WAIT_SHORT`) |

Early escalation trigger: ask moves >= 5 cents above maker price (`EARLY_ESCALATION_MIN_MOVE`).

## Post-Only Rejection Handling
When maker order rejected as post-only (would cross spread):
1. Tier 1: retry up to `POST_ONLY_MAX_SAME_PRICE` (2) times at same price
2. Tier 2: offset by `POST_ONLY_DEGRADED_EXTRA_OFFSET` (1 cent)
3. Then escalate to taker IOC

## Per-Ticker Execution Lock
Prevents simultaneous trades on same ticker. Originally was per-asset (too broad — blocked 86% of SOL candidates). Now per-ticker with `IOC_TICKER_COOLDOWN` (15s) after IOC attempt.

## Confirmation Addon
After a fill, if price improves by >= 3 cents within the same window, an addon order for 50% of original size is placed. Max 1 addon per position, 98c cap.

## evaluate_execution_strategy() — Diagnostic Only
Located at bot.py ~620-815. Computes execution strategy scores (certainty, orderbook, urgency, composite) but the result is ONLY logged, never controls execution. The actual execution path is determined by the constants above.

## Key Code Locations
- `OrderExecutor`: bot.py ~9848-12220 (execute() at ~9945)
- Execution constants: bot.py ~638-698
- NBBO fallback gates: bot.py ~667
- Per-ticker lock: within OrderExecutor

## Related
- [[concepts/per-asset-rules.md]]
- [[concepts/sol-dynamics.md]]
- [[failures/sol-maker-adverse-selection.md]]
