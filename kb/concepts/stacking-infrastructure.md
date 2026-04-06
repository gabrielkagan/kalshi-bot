---
status: active
updated: 2026-04-02
tags: [stacking, composite-pk, strategy-group]
---
# Stacking Infrastructure

## Summary
Stacking allows multiple strategies (main, DC, terminal momentum, bracket NO) to hold simultaneous positions on the same ticker. Uses a composite primary key of (ticker, strategy_group), with per-ticker and per-window caps to limit correlated exposure. Gated by `STACKING_ENABLED` env var.

## Problem Stacking Solves
Without stacking, the bot's per-ticker dedup prevents a main candidate and a DC overlay (or TM intercept) from both trading the same ticker. This leaves money on the table when multiple independent strategies identify edge on the same contract.

## Strategy Groups
`strategy_to_group()` in `models.py` maps strategy names to groups:
- **main** — standard 15M candidates, hourly, overnight/weekend discount
- **decided** — all DC tiers (T1, T1B, T2, T2_Z25, T2_Z2)
- **terminal_momentum** — TM strategy
- **bracket_no** — weather bracket NO

Different groups can coexist on the same ticker. Same group cannot stack (a ticker can only have one `main` position).

## Schema Changes
The `positions` and `settled_trades` tables gained:
- `strategy_group TEXT DEFAULT 'main'` — identifies which group owns the position
- `is_stacked BOOLEAN DEFAULT 0` — marks positions created via stacking

Primary key for positions changed from `(ticker)` to `(ticker, strategy_group)`.

## Position Checks
When evaluating a candidate:
1. Look up existing positions for that ticker
2. If `STACKING_ENABLED`, check if the candidate's strategy_group already has an open position
3. If same group exists, skip (no intra-group stacking)
4. If different group, allow (inter-group stacking)

When `STACKING_ENABLED = False`, legacy behavior: reject any ticker with an existing position regardless of group.

## Safety Caps
- Per-ticker aggregate risk limit prevents total exposure from exceeding safe levels even with stacked positions
- Per-window position limits apply to each strategy group independently
- `MAX_CONCURRENT_TAKER_PER_ASSET` still applies as a global safety cap

## Settlement
Settlement processes each (ticker, strategy_group) position independently. PnL computed per position, not aggregated at ticker level. The settlement refactor ensures each strategy group's position gets its own settlement record in `settled_trades`.

**Revenue override (Apr 5, 2026):** `_process_settlement()` passes `revenue_override=row_count*100` to `record_settlement()`. Without this, the Kalshi API's aggregate revenue (for ALL contracts on the ticker) was attributed to EACH position row, double-counting PnL. See [[failures/pnl-reporting-bugs.md]].

**Lost addon positions:** If reconciliation deletes an addon position before settlement, the surviving position's revenue may still reflect the full ticker revenue from the API. The revenue_override fix handles this for positions that exist at settlement time, but lost addons cannot be recovered.

## Kill Switch
`STACKING_ENABLED = os.environ.get("STACKING_ENABLED", "0") == "1"` — defaults OFF. When disabled, logs a warning if any tickers have multiple positions (shouldn't happen, but safety check).

## Related
- [[concepts/dc-strategy.md]]
- [[strategies/terminal-momentum.md]]
- [[strategies/bracket-no.md]]
- [[concepts/position-reconciliation.md]]
- [[decisions/stacking-enabled.md]]
