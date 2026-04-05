---
status: active
updated: 2026-04-05
tags: [strategy, lpne, btc, near-expiry, low-price]
---
# Low-Price Near-Expiry (LPNE) Strategy

## Summary
LPNE trades BTC 15M contracts at 80-87c in the final 10-120 seconds before expiry. These are contracts the price floor rejects (BTC_MIN_ENTRY_PRICE=88c) but that settle YES at 97.6% WR (42 observations, STC<=120s). Fixed 50-contract sizing, direct taker IOC. BTC ONLY.

## Mechanism
At 80-87c with <2 minutes left, the underlying price is above the strike but the market prices in residual downside risk. The model's cal_prob (~83%) underestimates reality (~98%) because the vol model overestimates short-horizon vol. Near expiry, there isn't enough time for the required reversal.

## Entry Criteria
| Parameter | Value | Notes |
|-----------|-------|-------|
| `LPNE_ENABLED` | True (env var kill switch) | Default enabled |
| `LPNE_ASSETS` | {BTC} | BTC only — ETH 80% WR, XRP 88.5%, SOL marginal |
| `LPNE_MIN_PRICE` | 80 | Lowest eligible price |
| `LPNE_MAX_PRICE` | 87 | Highest (88c+ is main pipeline) |
| `LPNE_MIN_STC` | 10 | Avoid settlement noise |
| `LPNE_MAX_STC` | 120 | Data: STC<=120s is the validated zone |
| `LPNE_FIXED_CONTRACTS` | 50 | Fixed sizing, bypasses Kelly |
| `LPNE_MAX_CONCURRENT` | 2 | Conservative cap |
| Probability gate | `final_prob >= best_ask / 100.0` | Model must believe at least break-even |

## Pipeline Position
LPNE intercepts at the **price floor check** (bot.py ~line 7148), BEFORE the floor rejection. This is different from TM which intercepts at insufficient_edge. The signal passes all quality gates (probability, orderbook, etc.) but fails the BTC_MIN_ENTRY_PRICE floor — LPNE catches it before it's rejected.

BTC_MIN_ENTRY_PRICE stays at 88c — LPNE is a separate intercept, not a floor change.

## NBBO Fallback Gate
BTC NBBO gate lowered from (86, 99, 300) to (80, 99, 300) to let the bot SEE prices at 80-85c when WS orderbook is empty. No effect on main pipeline (floor still blocks at 88c).

## Execution
Separate `_execute_lpne_taker()` method — mirrors TM executor. Direct taker IOC. Fresh ask validation: must still be in LPNE_MIN_PRICE..LPNE_MAX_PRICE range. No retry queue.

## Sizing
Fixed 50 contracts. LOW_STC_SIZING_CAP (0.5x at STC<100s) applies — 83% of signals have STC<100s, so most get 25 contracts. Risk at 83c: $41.50 (50ct) or $20.75 (25ct after cap) = 1.5-2.9% of balance. Less risky than TM at 97c.

## Statistical Basis
- 42 signals, 41W/1L = 97.6% WR
- Binomial test (H0: WR = breakeven): p=0.031
- Near-expiry advantage vs far: p=0.006
- Wilson CI lower bound: 87.7% (above 84% breakeven for 80-84c, marginal at 87c)
- The one loss: BTC 86c, STC=82s, cal_prob=0.81 (below 86% breakeven — would be filtered by prob gate)
- Z-scores 92% have z > -1 — NOT decided contracts, DC won't catch these

## Data
- ~1.2 signals/day
- Fill rate unknown (new strategy, fire-and-forget IOC)
- Near-expiry books have 24-40% WS depth visibility at 80-87c

## Related
- [[terminal-momentum.md]] — TM is the same concept at 95-99c
- [[../concepts/sol-dynamics.md]] — SOL has similar sub-floor gate but blocking, not trading
- [[../concepts/edge-thresholds.md]] — STC scaler also applies to LPNE timing zone
- [[../../kb-research/bot/stc-sizing-research.md]] — Source research for near-expiry patterns
