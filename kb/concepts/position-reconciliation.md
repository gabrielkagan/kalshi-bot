---
status: active
updated: 2026-05-18
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

Reconciliation performs a sum-check: compares the local total count (summed across all strategy groups, side-filtered) against the API count. Behavior on disagreement is split (B2 ticket `86b9zud1p`, 2026-05-18):

- **`local_total > api_total` AND ≥1 open row has `fill_source` starting with `'ghost_fill'`** → auto-fix path. Deflate the ghost-fill row(s) by exactly `(local_total - api_total)` contracts (largest-remainder rounding across multiple ghost-fill rows so the sum lands exactly on the target). Non-ghost-fill rows are untouched. When a ghost-fill row reaches `count=0`, it is DELETED (mirrors the existing reconcile DELETE on the ticker-level zero-position path at the case-1 branch; avoids polluting `settled_trades` with a zero-row entry when settlement later iterates `WHERE ticker=?` without a status filter). Logs `RECONCILE_MULTI_MISMATCH_AUTOFIXED` with per-row evidence (audit trail).
- **All other cases** (no ghost-fill row present; or under-count `local_total < api_total`; or `excess > sum(ghost_row.count)`) → logs `RECONCILE_MULTI_MISMATCH ... NOT auto-fixing` with per-row evidence (strategy_group, count, fill_source, delta). No state mutation.

**Narrow caveat — fill_source is INSERT-only**: `record_position_from_fill` sets `fill_source` only on first INSERT, not on subsequent UPDATEs that accumulate into the same `(ticker, strategy_group)` row. If a ghost-fill landed via UPDATE on a row that an earlier IOC fill stamped `fill_source='ioc'`, the auto-fix gate (`fill_source LIKE 'ghost_fill%'`) will NOT see it as a ghost-fill row, and that ticker falls through to the warning-only branch. The 2026-05-18 HYPE incident (postmortem: `kb/failures/ghost-fill-retry-overcount-may18.md`) is exactly this shape — B1 fixed the upstream class in `bot/executor.py`; B2 is defense-in-depth for the narrower subset where the ghost-fill row was the FIRST INSERT on its `(ticker, strategy_group)`.

### Cleanup: Local positions not on API
After processing all API positions, scans local positions for any ticker not present in the API response. These are marked `status='closed'` — they represent positions that were settled or closed while the bot was offline.

## Key Gotcha: API Returns ONE Aggregate Per Ticker
The Kalshi `/portfolio/positions` endpoint returns a single `position` count per ticker. It does not know about the bot's internal strategy groups. This means:
- A ticker with 10 contracts from `main` + 5 from `decided` shows as 15 total on the API
- Reconciliation cannot know how to split 15 back into 10+5 in the general case
- For the over-count direction, the `fill_source='ghost_fill%'` heuristic identifies rows likely to be the phantom contributor (B2, 2026-05-18) and auto-fixes them. For other shapes (under-count, no ghost-fill row, or excess > ghost capacity), the `RECONCILE_MULTI_MISMATCH ... NOT auto-fixing` warning fires with per-row evidence, requiring manual investigation.

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
- `reconcile_with_api()`: `bot/state.py::StateManager.reconcile_with_api` (~1180)
- `_reconcile_positions()`: `bot/state.py::StateManager._reconcile_positions` (~1198)
- `_reconcile_multi_mismatch()`: `bot/state.py::StateManager._reconcile_multi_mismatch` (~1308; B2 auto-fix helper, 2026-05-18)
- `_reconcile_orders()`: `bot/state.py::StateManager._reconcile_orders` (~1466)
- Stacking post-check: `bot/state.py::StateManager.reconcile_with_api` (~1188-1196)

## Related
- [[concepts/stacking-infrastructure.md]]
- [[concepts/execution-layer.md]]
