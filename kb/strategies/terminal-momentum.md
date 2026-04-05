---
status: active
updated: 2026-04-02
tags: [strategy, tm, 95-99c, live]
---
# Terminal Momentum (TM) Strategy

## Summary
Terminal momentum trades contracts at 95-99c in the final 1-5 minutes before expiry, intercepting what would otherwise be insufficient_edge rejections. Per-price sizing (98-99c: 100 contracts, 95-97c: 50 contracts) bypasses Kelly. Near-100% WR on standalone due to extreme price convergence at expiry.

## Mechanism
At very high prices (95-99c) near expiry, the model's probability is extremely high but edge relative to price is thin. The standard edge filter rejects these. TM intercepts the rejection and trades them anyway, because at 95-99c with 1-5 minutes left, the underlying price has already moved well past the strike threshold.

## Entry Criteria
| Parameter | Value | Notes |
|-----------|-------|-------|
| `TM_PRICE_SET` | {95, 96, 97, 98, 99} | Valid entry prices |
| `TM_MIN_PROB` | 0.93 | Model confirmation threshold |
| `TM_MIN_STC` | 61s | Not in final minute (settlement noise) |
| `TM_MAX_STC` | 300s | 5 minutes maximum |
| `TM_FIXED_CONTRACTS` | 50 | Default sizing for 95-97c, bypasses Kelly |
| `TM_CONTRACTS_BY_PRICE` | {98: 100, 99: 100} | Per-price overrides (scaled tiers) |
| `TM_MAX_CONCURRENT` | 4 | Safety cap on simultaneous TM positions |

## Pipeline Position
TM sits inside the `insufficient_edge` rejection path in `scan()` (bot.py ~7585-7698):
1. Scanner computes probability and edge for a contract
2. Edge falls below `MIN_EDGE_BY_PRICE` threshold
3. Before rejecting, check TM eligibility (price, prob, STC)
4. If eligible and no DC overlap on same ticker, add as TM candidate
5. Set `_tm_intercepted = True` to skip the rejection

## Overlap Prevention
- **DC overlap:** Skips if ticker already claimed by a decided contract strategy
- **Position overlap:** Checks if ticker already has a TM position (stacking-aware when `STACKING_ENABLED`)
- **Concurrent cap:** Max 4 simultaneous TM positions

## Execution
TM candidates route through normal execution (taker for DC/TM strategies). Per-price sizing (98-99c: 100, others: 50) — no Kelly, no drawdown scaler, kelly_f logged as 0.0. Execution-time re-derivation: if price drifts between scan and execution, `_execute_tm_taker` re-derives contract count from `TM_CONTRACTS_BY_PRICE` using `fresh_ask`, preventing oversized fills on lower-price tiers.

## Strategy Scores
All set to maximum (1.0 certainty, 1.0 urgency, 0.5 orderbook, 1.0 composite). These are diagnostic only.

## Product Type
15M only (`_pt in (None, "15m")`). Not applied to hourly, SPX, or weather markets.

## Kill Switch
`TERMINAL_MOMENTUM_ENABLED` env var (default "1" = enabled).

## Data
97c entry: 98.2% WR on 55 observations, above 97% breakeven.
98c entry: 100% WR on 33 live trades (scaled to 100 contracts Apr 5 2026).
99c entry: 100% WR on 62 live trades (scaled to 100 contracts Apr 5 2026).

## Related
- [[concepts/dc-strategy.md]]
- [[concepts/execution-layer.md]]
- [[concepts/stacking-infrastructure.md]]
