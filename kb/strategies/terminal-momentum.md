---
status: active
updated: 2026-04-07
tags: [strategy, tm, 95-99c, live]
---
# Terminal Momentum (TM) Strategy

## Summary
Terminal momentum trades contracts at 95-99c in the final 1-5 minutes before expiry, intercepting what would otherwise be insufficient_edge rejections. Margin × STC-aware sizing replaces fixed sizing (Apr 7 2026). Near-100% WR due to extreme price convergence at expiry.

## Mechanism
At very high prices (95-99c) near expiry, the model's probability is extremely high but edge relative to price is thin. The standard edge filter rejects these. TM intercepts the rejection and trades them anyway, because at 95-99c with 1-5 minutes left, the underlying price has already moved well past the strike threshold.

## Entry Criteria
| Parameter | Value | Notes |
|-----------|-------|-------|
| `TM_PRICE_SET` | {95, 96, 97, 98, 99} | Valid entry prices |
| `TM_MIN_PROB` | 0.93 | Model confirmation threshold |
| `TM_MIN_STC` | 61s | Not in final minute (settlement noise) |
| `TM_MAX_STC` | 300s | 5 minutes maximum |
| `TM_MAX_CONCURRENT` | 4 | Safety cap on simultaneous TM positions |

## Sizing: `tm_compute_contracts(price, stc, bankroll)`

**Formula:** `TM_BASE_CONTRACTS × (100 - price) × stc_multiplier`, capped by risk.

| Parameter | Value | Notes |
|-----------|-------|-------|
| `TM_BASE_CONTRACTS` | 100 | Base multiplier |
| `TM_STC_SAFE_MULT` | 1.5 | STC < 180s (100% WR on 77 trades) |
| `TM_STC_DANGER_MULT` | 0.5 | STC 180-240s (94.7% WR, all 4 losses here) |
| `TM_STC_NORMAL_MULT` | 1.0 | STC 240+s (99.3% WR, fattest buffers) |
| `TM_MAX_RISK_FRAC` | 0.25 | Max fraction of bankroll per TM trade |
| `TM_MIN_CONTRACTS` | 25 | Floor |
| `TM_MAX_CONTRACTS` | 500 | Hard cap |

**Example sizing on $1000 bankroll:**

| Price | STC < 3min | STC 3-4min | STC 4-5min |
|-------|-----------|-----------|-----------|
| 96c | 260ct | 200ct | 260ct |
| 98c | 255ct | 100ct | 200ct |
| 99c | 150ct | 50ct | 100ct |

### Why STC 240-300s is NOT reduced
Data shows 240-300s is the safest zone by risk-adjusted metrics:
- Entry buffer averages 0.25% (vs 0.10% at <120s) — fattest buffers
- Risk ratio (expected_move/buffer) is 1.23 (lowest of any zone)
- 135/136 = 99.3% WR
- Contracts reach 95-99c at high STC only when spot is well above threshold

## STC Danger Zone: 180-240s
All 4 TM losses (as of Apr 7) occurred at STC 210-240s:
- XRP@95c ×2 (77.8% WR at 95c+180-240s)
- XRP@99c ×1 (94.4% WR at 99c+180-240s)
- ETH@97c ×1 (at STC 270-300s, the lone exception)

The 0.5× multiplier reduces exposure here.

## Pipeline Position
TM sits inside the `insufficient_edge` rejection path in `scan()`:
1. Scanner computes probability and edge for a contract
2. Edge falls below `MIN_EDGE_BY_PRICE` threshold
3. Before rejecting, check TM eligibility (price, prob, STC)
4. If eligible and no DC overlap on same ticker, add as TM candidate
5. Set `_tm_intercepted = True` to skip the rejection

## Overlap Prevention
- **DC overlap:** Skips if ticker already claimed by a decided contract strategy
- **Position overlap:** Checks if ticker already has a TM position (stacking-aware)
- **Concurrent cap:** Max 4 simultaneous TM positions

## Execution
TM candidates route through `_execute_tm_taker` (direct taker IOC). Sizing via `tm_compute_contracts()` — no Kelly, no drawdown scaler, kelly_f logged as 0.0. Execution-time re-derivation if price drifts between scan and execution.

## Data (Apr 7, 2026 — 270 trades)
- Overall: 266W/4L (98.5% WR), +$31.29 net (under old fixed sizing)
- 98c: 63W/0L (100%), +$79.97 — highest EV tier
- 99c: 107W/1L (99.1%), -$128.36 — marginal under old 200ct fixed sizing
- 96c: 26W/0L (100%), +$40.37
- Backtest of new sizing: +$583 vs +$56 actual (+$83/day vs +$8/day)

## Related
- [[concepts/dc-strategy.md]]
- [[concepts/execution-layer.md]]
- [[concepts/stacking-infrastructure.md]]
- [[failures/settlement-watermark-race.md]] — Two TM positions stuck by settlement bug (Apr 7)
