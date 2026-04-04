---
status: active
updated: 2026-04-04
tags: [btc, eth, sol, xrp, risk-caps]
---
# Per-Asset Configuration

## Summary
Each asset (BTC, ETH, SOL, XRP) has individualized trading rules based on empirical performance data. Rules cover entry price floors, risk caps, execution modes, and edge thresholds. All values are data-driven with specific trade counts and win rates backing each decision.

## BTC
| Parameter | Value | Justification |
|-----------|-------|---------------|
| Min entry price | 88c | Data: 88c = 96.2% WR on n=53 shadow, 96.3% on n=27 recent |
| Max risk per trade | 0.12 (12%) | BTC oversizing causes outsized losses — cap exposure |
| Escalation wait | 7s (vs 15s default) | Data: ask_confirmed avg 2.7s, escalation_wait avg 19.5s, slip 3.4c |
| NBBO fallback | 86-99c, STC <= 300s | 97.9% WR |

BTC is the most liquid asset. The tighter escalation wait (7s vs 15s) reflects fast price confirmation. Risk cap at 12% prevents single-trade blowups.

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
| Min edge | 1.0% (`SOL_MIN_EDGE`) | Data: <1.0% = 82% WR, >= 1.0% = 94.2% WR on 258 trades |
| Max risk per trade | 0.12 (12%) | Tightest cap — contains loss magnitude on high-volume asset |
| Execution mode | Taker-first | Data: 44.7% maker fill rate, $101/wk missed |
| NBBO fallback | 86-99c, STC <= 300s | 93.3% WR; 80-85c is 50-73% WR trap |

SOL drives the majority of bot trade volume and is profitable at current settings (backtester validated: +$71 on 77 trades, 90.9% WR over Mar 30–Apr 2). Taker-first is critical — maker adverse selection was significant (see [[failures/sol-maker-adverse-selection.md]]). The 1.0% edge floor is the single most important SOL-specific rule. SOL cap 30ct reduces drawdown from 17% to 9% at a cost of ~$55 PnL — a risk/reward tradeoff, not a bug fix. See `kb-research/bot/backtesting-harness.md` for full analysis.

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
