---
status: active
updated: 2026-04-01
tags: [execution, addon, confirmation]
---
# Addon Strategies

## Summary
Addon strategies add contracts to existing positions after the initial fill. Two variants exist: CONFIRMATION_ADDON (live, adds on price improvement) and DIP_ADDON (killed, added on price dips). Addons execute as taker IOC, are capped at 50% of balance, and merge into the parent position via the stacking composite PK.

## Confirmation Addon (Live)
When the ask price rises after an initial fill, the thesis is confirmed -- the market agrees the contract should settle YES. The addon buys more contracts at the higher (but still profitable) price.

**Constants (bot.py ~688-694):**
| Constant | Value | Purpose |
|----------|-------|---------|
| ADDON_ENABLED | True | Master switch |
| ADDON_MIN_PRICE_IMPROVEMENT | 3 | Cents above entry price to trigger |
| ADDON_MIN_SECONDS_SINCE_FILL | 10.0 | Wait after fill before eligible |
| ADDON_MIN_STC_REMAINING | 45.0 | Minimum STC at addon time |
| ADDON_SIZE_FRACTION | 0.50 | Addon = 50% of original contract count |
| ADDON_MAX_ENTRY_PRICE | 98 | Price cap (still profitable after fees) |

## Addon Lifecycle
1. **`_on_fill()`** detects a fill via WS or REST poll
2. **`_register_addon_eligible()`** (line 14091) creates metadata entry in `_addon_eligible[ticker]` with actual fill price, corrected STC, and candidate context. Skips addon fills, TM fills, and bracket_no fills to prevent recursive registration.
3. **`_check_addon_opportunities()`** runs each `_tick()` -- iterates `_addon_eligible`, checks timing, price improvement, edge, and balance constraints
4. **`_execute_addon()`** (line 14281) submits taker IOC with strategy="CONFIRMATION_ADDON"
5. On fill, ticker is added to `_addon_completed` set (max one addon per position)

## Checks Before Execution
- Hourly fills excluded (addon would bypass hourly constraints like fixed sizing and asset exclusion)
- Entries expire after 300 seconds (5 minutes)
- Fresh probability recalculated with current spot and vol
- Net edge must exceed `MIN_EDGE_PCT / 100.0` after taker fees
- Addon cost capped at 50% of current balance (count reduced to fit if needed)

## Dip Addon (Killed)
Added contracts when the ask price dipped below entry. Data: 55.2% WR on 29 settled trades (16W/13L) -- no edge. `DIP_ADDON_ENABLED = False` since conclusive data showed it was unprofitable.

**Key parameters (for reference, all inactive):**
- `DIP_ADDON_MIN_DROP_CENTS = 3` -- ask must drop 3+ cents below entry
- `DIP_ADDON_MAX_TOTAL_RISK = 0.35` -- original + addon capped at 35% of bankroll
- `DIP_ADDON_SHADOW_FLOOR = 50` -- shadow logged all dips to 50 cents for data collection
- `DIP_ADDON_SHADOW_MODE = False` -- shadow data collection also stopped

## Stacking Interaction
With the composite PK system, addon fills map to `strategy_group = "main"` and merge into the parent position. The position table tracks the combined count, and settlement computes PnL on the full stacked position. This means addon contracts share the same ticker entry -- no separate position row.

The addon's `entry_path` is set to `"confirmation_addon"` or `"dip_addon"`, which prevents recursive registration -- `_register_addon_eligible()` explicitly skips these paths along with `tm_taker` and `bracket_no_taker`.

## Related
- [[concepts/execution-layer.md]]
- [[concepts/stacking-infrastructure.md]]
- [[concepts/fee-optimization.md]]
