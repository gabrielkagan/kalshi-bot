---
status: fixed
updated: 2026-04-07
tags: [failure, settlement, positions, dashboard, pnl]
---
# Settlement Watermark Race (Apr 7, 2026)

## Bug

`SettlementTracker._poll()` advances `_last_check_ts` unconditionally after each poll cycle, even when `_process_settlement()` returns early (unknown market_result, revenue=0 on WIN, stacked count mismatch). Once the watermark moves past a settlement's timestamp, `get_settlements(min_ts=...)` never returns it again. The position is stuck as "open" forever.

## Impact

- Two terminal_momentum positions (SOL 200ct@99c, BTC 200ct@99c) stuck as "open" for 4-12 hours after settlement
- Dashboard "Active Positions" showed phantom positions
- Lifetime PnL inflated by ~$396 — `actual_pnl_cents` formula adds back open position cost, but Kalshi already settled them (balance reflects outcome). Double-counted cost.
- 7 stale resting orders accumulated — `cleanup_expired_resting_orders()` was defined but never wired into the main loop

## Root Cause

Three early-return paths in `_process_settlement()` (lines ~15486, 15506, 15520) leave the position as "open" and DON'T add the ticker to `_processed_tickers`. But the watermark at line 15443 advances regardless. The settlement is permanently skipped.

## Fix (d1631b3)

1. **`_sweep_stuck_positions()`** — runs every 5 min, finds 15M positions whose market closed >5 min ago but are still unsettled. Queries individual market result via `get_market(ticker)` API and processes through normal settlement path. `_from_sweep=True` flag bypasses the revenue=0/WIN guard (sweep computes PnL from first principles).
2. **`cleanup_expired_resting_orders()`** — wired into settlement `tick()` every 60s (was defined at line 2699 but never called).

## Collateral Damage

- Stuck SOL position accumulated 23,181 PPO observations over 8 hours (65% of all PPO data)
- Dashboard showed wrong PnL for ~12 hours until fix deployed

## Prevention

The watermark-based settlement design is inherently fragile — any early return permanently loses that settlement. The sweep is a robust fallback. Consider also adding a `_failed_tickers` set that retries on each poll (without relying on API min_ts window).
