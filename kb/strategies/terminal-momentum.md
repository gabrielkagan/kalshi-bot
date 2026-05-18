---
status: active
updated: 2026-05-18
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
| `TM_MAX_CONCURRENT` | 8 | Safety cap — raised for stacking (multiple price levels per ticker) |
| `TM_NEGATIVE_EV_TIERS` | {95} | Minimum sizing only — 88.9% WR vs 95.3% breakeven on 27 trades |
| `TM_ASSET_RISK_CAPS` | per-asset | BTC/SOL/XRP 15%, ETH 20% — matches main pipeline (was 25% flat) |

## Sizing: `tm_compute_contracts(price, stc, bankroll, asset)`

**Formula:** `TM_BASE_CONTRACTS × (100 - price) × stc_multiplier`, capped by per-asset risk.
Negative-EV tiers (95c) get TM_MIN_CONTRACTS (25ct) until WR proves above breakeven.

| Parameter | Value | Notes |
|-----------|-------|-------|
| `TM_BASE_CONTRACTS` | 100 | Base multiplier |
| `TM_STC_SAFE_MULT` | 1.5 | STC < 180s (100% WR on 77 trades) |
| `TM_STC_DANGER_MULT` | 0.5 | STC 180-240s (94.7% WR, all 4 losses here) |
| `TM_STC_NORMAL_MULT` | 1.0 | STC 240+s (99.3% WR, fattest buffers) |
| `TM_ASSET_RISK_CAPS` | per-asset | BTC/SOL/XRP 15%, ETH 20% (replaced flat 25%) |
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
4. If eligible AND no per-(ticker, side) Kelly-sized entry already
   exists (see "Overlap Prevention & Stacking" below), add as TM candidate
5. Set `_tm_intercepted = True` to skip the rejection

## Overlap Prevention & Stacking
- **Same-tick decided_* overlap:** Skips if a `decided_*` candidate emits in the same scan tick (`_tm_dc_overlap` — candidate-list scan).
- **Cross-tick decided_* overlap (B5, 86b9zudg2, 2026-05-18):** Skips if any `decided_*` IOC is in flight via the executor's `_dc_retry_queue` from a prior tick (`_tm_dc_retry_overlap`). Closes the production stack class that fired TM_98 22s after `decided_t1`'s first IOC entered retry on KXHYPE15M-26MAY180530-30.
- **Per-(ticker, side='yes') non-TM open-position lock (B5, 86b9zudg2, 2026-05-18):** Skips if any non-TM strategy (`decided_*`, `weekend_discount`, `overnight_discount`, `low_price_near_expiry`, etc.) holds an open YES-side position on the ticker (`_tm_non_tm_position`). NO-side strategies (e.g., `bracket_no`) on the same ticker do NOT block YES-side TM (side filter).
- **Price-level stacking (TM-on-TM):** Multiple TM positions remain allowed on same ticker at DIFFERENT prices (e.g., 95c + 98c). Strategy encoded as `terminal_momentum_{price}`, each gets its own composite PK slot. Only blocks duplicate at same price (`_tm_has_position`).
- **Concurrent cap:** Max 8 simultaneous TM positions (raised from 4 for stacking headroom)
- **Data (TM-on-TM, pre-B5):** 40/40 stackable tickers settled YES, 0/4 TM losses had stacking opportunities. Rising price across TM levels = strong confirmation signal. B5's new gates do NOT touch TM-on-TM behavior — they close the orthogonal "TM-on-Kelly-sized-non-TM" stack class.

## Execution
TM candidates route through `_execute_tm_taker` (direct taker IOC). Sizing via `tm_compute_contracts()` — no Kelly, no drawdown scaler, kelly_f logged as 0.0. Execution-time re-derivation if price drifts between scan and execution. Strategy routing uses `.startswith("terminal_momentum")` to match all price-encoded variants.

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
