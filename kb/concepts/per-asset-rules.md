---
status: active
updated: 2026-04-07
tags: [btc, eth, sol, xrp, risk-caps]
---
# Per-Asset Configuration

## Summary
Each asset (BTC, ETH, SOL, XRP) has individualized trading rules based on empirical performance data. Rules cover entry price floors, risk caps, execution modes, and edge thresholds. All values are data-driven with specific trade counts and win rates backing each decision.

## BTC
| Parameter | Value | Justification |
|-----------|-------|---------------|
| Min entry price | 88c (main pipeline) | Data: 88c = 96.2% WR on n=53 shadow, 96.3% on n=27 recent |
| LPNE | 80-87c, STC<=120s | Intercepts below floor. 97.6% WR on 42 obs, 50ct fixed |
| Max risk per trade | 0.15 (15%) | Per-trade cap |
| Escalation wait | 7s (vs 15s default) | Data: ask_confirmed avg 2.7s, escalation_wait avg 19.5s, slip 3.4c |
| NBBO fallback | 80-99c, STC <= 300s | Lowered from 86c for LPNE. 97.9% WR at 86c+ |

BTC is the most liquid asset. The tighter escalation wait (7s vs 15s) reflects fast price confirmation. LPNE extends BTC trading to 80-87c near-expiry only (STC<=120s) — the floor stays at 88c for the main pipeline.

## ETH
| Parameter | Value | Justification |
|-----------|-------|---------------|
| Min entry price | 90c | Data: 85-89c has 86.2% WR on 65 trades, -$23.76 PnL; 90c+ is 95.2% WR |
| Sub-80c position cap | 50 contracts | Half-Kelly clamp [20, 50] for 75-79c zone |
| Max risk per trade | 0.25 (default) | No override needed |
| NBBO fallback | 90-99c, STC <= 300s | Raised from 85c based on PnL data |

ETH floor was raised from 85c to 90c after discovering negative PnL in the 85-89c zone. The sub-80c cap exists because the global `MIN_ENTRY_PRICE` was lowered to 75c specifically for ETH data collection, but full Kelly sizing at those prices is too aggressive.

## SOL
| Parameter | Value | Justification |
|-----------|-------|---------------|
| Min entry price | 80c | Explicit floor — prevents SOL trading at 75-79c |
| Sub-86c time gate | Block ≤85c at STC≥300s | Data: 78.3% WR -$289; <300s is 100% WR +$228 |
| Min edge | 1.0% (`SOL_MIN_EDGE`) | Data: <1.0% = 82% WR, >= 1.0% = 94.2% WR on 258 trades |
| Max risk per trade | 0.15 (15%) | Raised from 12% — 43.9% of trades were capped, +$26 PnL. DC path now enforces this too. |
| Execution mode | Taker-first | Data: 44.7% maker fill rate, $101/wk missed |
| NBBO fallback | 90-99c, STC <= 300s | Raised 86→90 Apr 7: NBBO sub-90c = 85.7% WR -$319; orderbook unaffected (+$470) |

SOL drives the majority of bot trade volume but is only marginally profitable (+$102 on 474 trades, $0.21/trade as of Apr 7). NBBO fallback was the primary PnL drag: -$328 on 226 trades vs +$470 on 116 orderbook trades. Gate raised 86→90c (Apr 7) to block the losing NBBO path. Taker-first is critical — maker adverse selection was significant (see [[failures/sol-maker-adverse-selection.md]]). The 1.0% edge floor is the single most important SOL-specific rule.

**IOC drift is a systematic problem at low prices:** All 20 SOL 85c trades were phantom fills — scanned at 87-92c, filled at 85c via IOC drift. The 85c tier is 70% WR, -$456 net. See [[failures/ioc-subfloor-fill.md]].

**SOL DC tiered risk:** <= 94c: 20% of standard, 95-96c: 10%, >= 97c: 5%.

## XRP
| Parameter | Value | Justification |
|-----------|-------|---------------|
| Min entry price | 92c | Data: PnL negative at every floor <90c, PF=1.68 at >= 92c |
| Max risk per trade | 0.12 (12%) | XRP RK vol underestimates realized risk |
| Status | Live (was shadow) | Promoted: 41W/2L, 95.3% WR |
| NBBO fallback | 92-99c, STC <= 300s | Conservative — XRP less liquid |

XRP was promoted from shadow (see [[decisions/xrp-promotion.md]]) at a high floor. The 12% risk cap reflects that XRP's Realized Kernel volatility systematically underestimates actual risk — likely due to thinner liquidity and wider spreads on underlying exchanges.

## Price-Dependent Edge Thresholds (All Assets)
`MIN_EDGE_BY_PRICE` applies universally:
| Price | Min Edge | Notes |
|-------|----------|-------|
| 97-99c | 1.00% | High price = thin margin, need more edge |
| 95-96c | 0.75% | |
| 93-94c | 0.50% | |
| 91-92c | 0.20% | Data: 193 settled at 94.3% WR, Wilson LB 90.1% |
| 89-90c | 0.25% | |
| 0-88c | 0.25% | Floor |

SOL additionally requires `SOL_MIN_EDGE` (1.0%) regardless of price tier.

## Related
- [[concepts/sol-dynamics.md]]
- [[concepts/execution-layer.md]]
- [[decisions/xrp-promotion.md]]
- [[decisions/sol-edge-floor.md]]
- [[failures/sol-maker-adverse-selection.md]]
- See `kb-research/bot/profitability-acceleration.md` for per-asset MIN_ENTRY_PRICE research
