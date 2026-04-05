---
status: decided
updated: 2026-04-02
tags: [decision, stacking, composite-pk]
date: 2026-04-02
---
# Decision: Enable Multi-Strategy Stacking

Date: 2026-04-01 to 2026-04-02
Status: Decided

## Context
Multiple strategies (main 15M, DC overlay, terminal momentum, bracket NO) can identify edge on the same ticker simultaneously. Without stacking, the per-ticker dedup rejects the second strategy, leaving money on the table. For example, a 96c contract at 180s STC could qualify as both a main candidate and a DC T2 overlay — but only one could trade.

## Options Considered
1. **Priority-based selection** — If multiple strategies want the same ticker, pick the best one
   - Pro: Simple, no schema changes
   - Con: Loses the second strategy's edge entirely
2. **Full stacking with composite PK** — Allow multiple strategies on same ticker via (ticker, strategy_group) composite key
   - Pro: Captures edge from all qualifying strategies
   - Con: Schema migration, settlement refactor, increased correlated exposure
   - Con: Safety caps needed to prevent runaway position accumulation
3. **Delayed stacking** — Wait for more data on strategy independence
   - Con: Continues leaving money on the table

## Decision
Enable stacking with `STACKING_ENABLED` env var (default "0" for safety). Schema migration adds `strategy_group` and `is_stacked` columns to positions and settled_trades tables.

## Implementation
1. **Schema migration:** `strategy_group TEXT DEFAULT 'main'` and `is_stacked BOOLEAN DEFAULT 0` added to positions and settled_trades
2. **`strategy_to_group()` in models.py:** Maps strategy names to groups (main, decided, terminal_momentum, bracket_no)
3. **Position checks:** Stacking allowed across groups, not within same group
4. **Settlement:** Each (ticker, strategy_group) position settled independently
5. **Safety caps:** Per-ticker aggregate risk limit, per-window position limits per group, MAX_CONCURRENT_TAKER_PER_ASSET global cap

## Consequences
- DC and main can coexist on same ticker — captures both edges
- TM and DC can coexist (though TM already checks for DC overlap)
- Slightly higher correlated exposure per ticker — mitigated by caps
- Settlement logic more complex — each position tracked independently
- Kill switch allows instant revert to non-stacked behavior

## Related
- [[concepts/stacking-infrastructure.md]]
- [[concepts/dc-strategy.md]]
- [[strategies/terminal-momentum.md]]
- [[strategies/bracket-no.md]]
