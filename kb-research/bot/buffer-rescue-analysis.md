---
status: in-progress
updated: 2026-04-07
tags: [research, ppo, buffer, sizing, opportunity, shadow-promotion]
---
# Buffer-Gated Trade Rescue Analysis (Apr 7, 2026)

## Question
Can we safely take MORE trades by using spot buffer (distance from threshold) as a second-level gate to rescue borderline rejections?

## Data
13,099 PPO observations, 55 tickers (12 hours clean data since PPO fix). Plus 7 days of evaluated_opportunities with buffer derivable from spot_price/threshold.

## Findings

### Buffer strongly differentiates wins from losses
| Group | n | Avg buffer |
|-------|---|-----------|
| Taken wins | 77 | 0.391% |
| Rejected wins | 143 | 0.253% |
| Taken losses | 2 | 0.144% |
| Rejected losses | 45 | 0.140% |

Taken wins have 2.7x the buffer of losses. Gates correctly keep the fattest-buffer trades.

### Two candidate promotions (100% WR, n≥16)
| Gate | Buffer threshold | Trades | WR | Counterfactual PnL |
|------|-----------------|--------|-----|-------------------|
| `stc_extended_floor_shadow` | ≥ 0.25% | 17 | 100% (17/17) | +$173 |
| `dead_hour_passed` | ≥ 0.30% | 16 | 100% (16/16) | +$124 |
| **Combined** | | **33** | **100%** | **+$297 ($42/day)** |

### Challenges identified (DO NOT PROMOTE YET)

**1. All 33 are NBBO-sourced.** Zero orderbook depth. Promoting these re-introduces the exact trade type that caused -$328 in SOL NBBO losses. Buffer is computed from spot/threshold, not from actual market depth. IOC drift could still degrade fill quality.

**2. Statistical confidence is insufficient.**
- Extended 17/17: Wilson 90% CI lower bound = 86.3%. Breakeven at avg 86c = ~86%. Lower bound OVERLAPS with breakeven.
- Dead hour 16/16: Wilson 90% CI lower bound = 85.5%. Breakeven at avg 90c = ~90%. Lower bound is BELOW breakeven.

**3. Minimum sizing barely helps.**
At 25ct fixed:
- Extended 100% WR: +$58/week. At 85% WR: +$4/week (noise).
- Dead hour 100% WR: +$40/week. At 85% WR: -$13/week (negative).

**4. Main pipeline is already doing well.**
Post-guardrails: ~$330/day with 39% conversion rate. Buffer rescue adds 13% at full sizing but re-introduces NBBO risk. Not worth contaminating a clean run.

### Gates that should STAY blocked
| Gate | WR | Buffer differentiates? | Verdict |
|------|-----|----------------------|---------|
| `low_price_shadow` | 55% | No (wins=0.082%, losses=0.094%) | Correctly blocked |
| `price_shadow_no_xrp` | 50% | No | Correctly blocked |
| `floor_raise_shadow` | 76% | Weakly (wins=0.221%, losses=0.147%) | Correctly blocked |

### Gate worth monitoring for promotion
| Gate | WR | Buffer signal? | Action |
|------|-----|---------------|--------|
| `dead_hour_passed` | 100% (11/11 today) | Fat buffers (0.374% avg) | Monitor 2 more weeks |
| `stc_extended_floor_shadow` | 91% (29/32) | Yes — 0.25% threshold separates W/L | Shadow-track buffer for 2 weeks |

## Recommendation
**Wait.** Continue shadow collection with buffer data. Re-analyze at:
- Apr 14 (1 week clean PPO data): ~100+ observations per gate
- Apr 21 (2 weeks): statistical power for 95% CI to clear breakeven

If buf ≥ 0.25% maintains 95%+ WR on extended zone across varying conditions → promote to live at minimum sizing (25ct).

If dead_hour maintains 100% at buf ≥ 0.30% → promote to live at minimum sizing.

## Key Insight
Buffer works as a safety signal for the main pipeline (PPO proved it), but it does NOT rescue NBBO-sourced trades — the pricing uncertainty from stale NBBO is orthogonal to the buffer signal. The safe path is to wait for orderbook-sourced versions of these trades, where buffer + real depth = high confidence.

## Related
- [[ppo-research-questions.md]] — PPO Q1-Q15 findings
- [[stc-sizing-research.md]] — STC extended zone original research
- kb/failures/ppo-monitor-bugs.md — PPO fix that enabled clean data collection
- kb/concepts/sol-dynamics.md — SOL NBBO problem
