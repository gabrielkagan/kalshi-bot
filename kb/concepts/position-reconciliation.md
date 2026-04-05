---
status: active
updated: 2026-04-01
tags: [reconciliation, startup, api]
---
# Position Reconciliation

## Summary
On every startup, `reconcile_with_api()` syncs the local `positions` and `pending_orders` tables against the Kalshi API. The API is treated as the source of truth ("API always wins"). This runs once at startup only — there is no periodic reconciliation during the main loop.

## Flow
`reconcile_with_api()` (bot.py ~1900) calls two sub-methods in sequence, then commits:
1. `_reconcile_positions(client, now)` — syncs positions table with `/portfolio/positions`
2. `_reconcile_orders(client, now)` — cleans up resting maker orders
3. `conn.commit()` — single atomic commit for all reconciliation changes
4. Post-reconciliation stacking check — warns if any ticker has multiple positions while `STACKING_ENABLED = False`

## Position Reconciliation (_reconcile_positions)
Fetches all positions from the Kalshi API and walks through each one:

### Case 1: API position count = 0
The ticker is fully settled or closed. Deletes ALL local positions for that ticker regardless of strategy group. Logs `RECONCILE_DELETE_UNSETTLED` if any open positions existed locally — this indicates a settlement was missed or a position was closed externally.

### Case 2: No local position exists
Creates a new position row from API data. Derives `asset` and `event_ticker` from the ticker string using helper methods (`_asset_from_ticker`, `_event_ticker_from_ticker`). Strategy group defaults to `'main'` since the API has no concept of strategy groups.

### Case 3: Exactly one local position
Straightforward UPDATE — overwrites side, count, avg_price_cents, total_cost_cents from API data. Preserves the existing `strategy_group` value.

### Case 4: Multiple local positions (stacking)
This is the multi-strategy case. When stacking is enabled, a single ticker can have positions from multiple strategy groups (e.g., `main` + `decided`). The API returns ONE aggregate count per ticker — it has no awareness of strategy groups.

Reconciliation performs a **sum-check only**: compares the local total count (summed across all strategy groups) against the API count. If they disagree, logs `RECONCILE_MULTI_MISMATCH` but does NOT auto-fix. This is deliberate — there is no safe way to redistribute the API aggregate back across strategy groups without potentially corrupting individual position records.

### Cleanup: Local positions not on API
After processing all API positions, scans local positions for any ticker not present in the API response. These are marked `status='closed'` — they represent positions that were settled or closed while the bot was offline.

## Key Gotcha: API Returns ONE Aggregate Per Ticker
The Kalshi `/portfolio/positions` endpoint returns a single `position` count per ticker. It does not know about the bot's internal strategy groups. This means:
- A ticker with 10 contracts from `main` + 5 from `decided` shows as 15 total on the API
- Reconciliation cannot know how to split 15 back into 10+5
- The `RECONCILE_MULTI_MISMATCH` warning fires when the split goes wrong, requiring manual investigation

## Order Reconciliation (_reconcile_orders)
Fetches all resting (unfilled maker) orders from the API and cancels every one of them. These are stale leftovers from before the restart — leaving them resting would consume capital and interfere with fresh orders.

After canceling, any orders found in `pending_orders` are marked `status='canceled'`. Orders not in `pending_orders` are imported as new rows with `status='canceled'` for history. Logs `STALE_ORDER_CLEANUP` with order details (ticker, price, count, creation time).

## Cost Calculation
Average price is derived from market exposure: `avg_price = cost // count`. The code prefers the newer fixed-point `market_exposure_dollars` field (from Kalshi's FP API migration), falling back to the legacy `market_exposure` integer field.

## When This Runs
- **Startup only** — called once during `MainLoop` initialization
- **Not periodic** — there is no reconciliation during normal operation
- The bot trusts its own tracking (fills via WS + REST fallback) during live operation
- If the bot crashes and restarts, reconciliation catches up with any fills or settlements that happened while offline

## Key Code Locations
- `reconcile_with_api()`: bot.py ~1900
- `_reconcile_positions()`: bot.py ~1918
- `_reconcile_orders()`: bot.py ~1989
- Stacking post-check: bot.py ~1908-1916

## Related
- [[concepts/stacking-infrastructure.md]]
- [[concepts/execution-layer.md]]
