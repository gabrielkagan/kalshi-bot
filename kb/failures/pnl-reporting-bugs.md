---
status: fixed
updated: 2026-04-06
tags: [fees, revenue, pnl, stacking, reporting]
---
# P&L Reporting Bugs (Apr 5-6, 2026)

## Summary
Three data integrity bugs caused the P&L chart to understate actual profits by ~$128. Combined effect: chart showed +$605 when actual Kalshi balance implied +$734 PnL. All three fixed in a single session.

## Bug 1: Fee Overcounting (~$120 impact)

**Root cause:** `record_settlement()` recomputed the fee on the FULL position count using `is_taker=MAX(all_fills)`. For escalated orders (maker→taker), the maker-filled portion should have $0 fee, but the entire position was charged at taker rate.

**Mechanism:** `is_taker = MAX(is_taker, ?)` in position updates meant ANY taker fill on a position marked it permanently as taker. At settlement, `calculate_fee(total_count, avg_price, is_taker=True)` charged taker fee on ALL contracts.

**Fix:** Added `accumulated_fee_cents` column to positions table. Each fill in `record_position_from_fill()` computes its own fee (using the correct per-fill `is_taker`) and accumulates. At settlement, `record_settlement()` uses the accumulated fee instead of recomputing. Falls back to recomputed fee for legacy positions without accumulated data.

**Historical data:** Not retroactively fixable (no per-fill data). Dashboard uses `actual_pnl_cents` (derived from Kalshi balance) for the "Total" display to show correct numbers.

## Bug 2: Revenue Inflation (34 trades, $20.48 impact)

**Root cause:** Addon positions (terminal momentum, bracket NO) on the same ticker were lost before settlement — either deleted by reconciliation or overwritten. At settlement, the Kalshi API returned revenue for ALL contracts on the ticker, but only the surviving position row existed. Full revenue was attributed to the surviving row.

**Example:** 1 contract at 89c showed `revenue_cents=193` (should be 100). The extra 93c came from a lost TM addon position.

**Fix:** Corrected 34 rows: `revenue_cents = count * 100`, adjusted `pnl_cents`. The `revenue_override` fix (Bug 3) prevents this going forward for stacked positions.

## Bug 3: Stacked Trade Double-Revenue ($13.20 impact)

**Root cause:** When multiple positions existed on the same ticker (stacking), `record_settlement()` extracted the FULL API aggregate revenue and used it for EACH position row independently. Both the base (88 contracts) and addon (44 contracts) got 13200c revenue instead of 8800c and 4400c respectively.

**Fix:** Added `revenue_override` parameter to `record_settlement()`. `_process_settlement()` computes per-row revenue (`row_count * 100`) and passes it as override. The one historical stacked trade was manually corrected.

## Bug 4: P&L Chart Scope Bug

**Root cause:** The Lifetime P&L chart used `rta.cumulative_pnl` (15M-only data) when `_dashboardScope='15m'` (the default). The filter buttons (ALL/15M/DC/HOURLY/DISC) couldn't work because 15M data lacked `pt`/`strat` fields.

**Fix:** Chart always uses `rta.all_products_cumulative_pnl` which includes strategy metadata for filtering.

## Bug 5: Trade Count Display

**Root cause:** `elTrades.textContent = data.length` showed thinned data point count (100) instead of actual trade count (1300+).

**Fix:** Uses `all_products_win_count + all_products_loss_count` for ALL view.

## Actual PnL Display

Added `snap["actual_pnl_cents"]` computed from Kalshi balance:
```
actual_pnl = kalshi_balance - initial_deposit + open_position_costs + open_position_fees
```

This is always correct regardless of per-trade fee/revenue errors. Dashboard uses this for the "Total" display in ALL view. Filtered views still use per-trade data.

## Prevention
- Per-fill fee accumulation prevents future fee overcounting
- `revenue_override` prevents future stacked revenue double-counting
- `actual_pnl_cents` from balance provides a cross-check against trade-level PnL

## Related
- [[concepts/fee-optimization.md]]
- [[concepts/stacking-infrastructure.md]]
- [[failures/hwm-bugs.md]]
