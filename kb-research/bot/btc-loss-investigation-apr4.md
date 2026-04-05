---
status: active
updated: 2026-04-04
tags: [research, btc, loss, investigation]
---
# BTC Loss Investigation — April 4, 2026

Source: Claude Code session, 2026-04-04
Status: Active — monitoring direct-fill WR, cap at 30ct implemented

**Trade:** `KXBTC15M-26APR041130-30`
**Date:** 2026-04-04 15:30 UTC
**Result:** Loss — 84 contracts YES at 89c, BTC settled NO
**PnL:** -$74.76
**Trigger:** Gabriel asked whether this was normal variance or structural

---

## 1. The Trade

| Field | Value |
|---|---|
| Ticker | KXBTC15M-26APR041130-30 |
| Strategy | TAKER_NOW |
| Escalation | none (direct fill) |
| Contracts | 84 |
| Entry Price | 89c |
| Calibrated Prob | 91.75% |
| Edge | 1.75% (0.75% after fee) |
| Kelly_f | 0.0836 (59% of full Kelly used) |
| Vol Regime | normal |
| Seconds to Close | 456.2 |
| Spot at Eval | $67,470.20 |
| Threshold | $67,368.60 |
| Cushion | $101.60 (0.15%) |
| EGARCH Vol | 0.000130 (lowest of passthrough era) |
| Balance at Trade | $1,514.12 |
| Ask Depth | 0 |

The bot evaluated at 15:22 UTC, found 1.75% edge, took 84 contracts TAKER_NOW at 89c. BTC dropped below the $67,368.60 threshold within the remaining ~8 minutes and the contract settled NO.

---

## 2. Verdict: Probably Normal Variance

**t-test on 41 passthrough BTC trades:** mean = -$1.66/trade, SD = $12.17, t = -0.876, **p = 0.39**. Cannot reject null that true edge is zero.

**Binomial test:** 4 losses in 41 trades at 7.8% expected loss rate → P(≥4) ≈ 40%. Completely unremarkable.

**Median PnL is positive** at +$0.42/trade. Negative mean driven entirely by this one loss.

**Power analysis:** Cohen's d = 0.14 (very small effect). Need **419 trades (~62 more days)** for 80% power to detect whether BTC edge is genuinely negative. At 41 trades we're at ~10% power — statistically blind.

**Position sizing was correct.** Kelly recommended 84 contracts (position_size field matches). 59% of full Kelly wager. Drawdown scaler = 1. Nothing misconfigured.

---

## 3. Data Cleaning Issues Discovered

### 3a. BLR Contamination
Initial "post-BLR" window (March 25+) was wrong. BTC continued using BLR calibration through March 27. True passthrough started March 30 for BTC.

| Period | Calibration | BTC 15M Trades |
|---|---|---|
| March 25–27 | BLR | 40 trades |
| March 30+ | Passthrough | 41 trades |

All subsequent analysis uses passthrough-only (March 30+).

### 3b. Daily Contract Mixing
10 of the 26 all-time BTC losses were from KXBTCD (daily) contracts — a different product. Must filter to `KXBTC15M%` for 15M analysis.

---

## 4. BTC Passthrough Performance Summary

### 4a. Overall
| Metric | Value |
|---|---|
| Trades | 41 |
| Wins | 37 |
| Losses | 4 |
| Win Rate | 90.2% |
| Total PnL | -$68.25 |
| Avg PnL/Trade | -$1.66 |
| Median PnL/Trade | +$0.42 |
| Avg Edge | 1.69% |
| Avg After-Fee Edge | 0.69% |
| Avg Entry Price | 94.5c |
| Avg Contracts | 20.0 |

### 4b. Without Today's Outlier
| Metric | Value |
|---|---|
| Trades | 40 |
| Win Rate | 92.5% |
| Total PnL | +$6.51 |
| Avg PnL/Trade | +$0.16 |
| t-stat | 0.305 (p = 0.76) |

### 4c. Win/Loss Asymmetry (Passthrough)
| | Wins | Losses |
|---|---|---|
| Count | 37 | 4 |
| Avg PnL | +$1.03 | -$26.55 |
| Avg Contracts | 19.0 | 29.5 |
| Avg Entry | 94.8c | 91.3c |
| Loss/Win Ratio | — | 25.8x |
| Breakeven WR | — | 96.3% |

---

## 5. Cross-Asset Comparison (Passthrough Only)

| Asset | n | WR | Total PnL | Avg PnL | Avg Edge | Return on Capital |
|---|---|---|---|---|---|---|
| SOL | 120 | 92.5% | +$270.87 | +$2.26 | 3.25% | +4.23% |
| XRP | 35 | 94.3% | +$21.71 | +$0.62 | 1.93% | +2.61% |
| ETH | 20 | 100% | +$17.70 | +$0.89 | 1.64% | +6.29% |
| **BTC** | **41** | **90.2%** | **-$68.25** | **-$1.66** | **1.69%** | **-8.83%** |

BTC deployed $773 in capital at -8.83% return. SOL deployed $6,407 at +4.23%.

---

## 6. Entry Price Analysis

**Key finding: entry price CAP is the WRONG intervention.** Lower entries perform worse, not better.

| Entry Bucket | n | WR | PnL | Avg Edge |
|---|---|---|---|---|
| ≤89c | 8 | 75.0% | -$69.66 | 1.78% |
| 90–92c | 5 | 80.0% | -$0.62 | 1.94% |
| 93–95c | 7 | 100% | +$7.22 | 1.63% |
| 96–97c | 8 | 87.5% | -$10.34 | 1.53% |
| 98–99c | 13 | 100% | +$5.15 | 1.68% |

BTC loses at LOW entries (where spot is near threshold) and wins at HIGH entries (where the contract is practically decided). The model finds "more edge" at low entries but is wrong more often.

---

## 7. Escalation Pattern

| Channel | n | WR | PnL | Avg Slippage | Notes |
|---|---|---|---|---|---|
| Direct fill | 11 | 72.7% | -$77.64 | -0.8c | |
| Escalation wait | 30 | 96.7% | +$9.39 | +5.7c | Terminal-momentum-by-another-name |

73% of BTC trades escalate (vs 52% SOL). BTC is a clear outlier in direct/escalated WR gap (24pp vs SOL 2.1pp).

**Critical caveat:** Without today's single trade, direct fills = 10 trades, 80% WR, -$2.88. P(≥2 losses in 10 at 8%) = 18.8% — unremarkable. The "model failure" narrative is 96% driven by one trade.

---

## 8. The Four Passthrough Losses

| # | Date | Strategy | Escalation | Contracts | Entry | PnL | Cal Prob | Vol |
|---|---|---|---|---|---|---|---|---|
| 1 | Mar 31 04:30 | MAKER_PATIENT | escalation_wait | 13 | 97c | -$12.61 | 92.6% | 0.000218 |
| 2 | Mar 31 15:45 | MAKER_PATIENT | none | 13 | 90c | -$11.70 | 92.6% | 0.000215 |
| 3 | Apr 1 17:15 | MAKER_PATIENT | none | 8 | 89c | -$7.12 | 91.4% | 0.000215 |
| 4 | Apr 4 15:30 | TAKER_NOW | none | 84 | 89c | -$74.76 | 91.8% | 0.000130 |

Loss #4 is 7x the magnitude of all other losses combined. Entirely a position size effect — 84 contracts vs 8–13.

---

## 9. Interventions Simulated

### Position Size Caps (All Trades)
| Cap | Simulated PnL |
|---|---|
| 15 contracts | -$22.47 |
| 20 contracts | -$22.60 |
| 30 contracts | -$26.50 |
| No cap | -$69.05 |

All caps still negative. Caps reduce magnitude but don't create edge.

### Edge Floors
| Floor | n | WR | PnL |
|---|---|---|---|
| ≥1.0% (current) | 41 | 90.2% | -$68.25 |
| ≥2.0% | 7 | 100% | +$11.32 |

Edge ≥2.0% shows 100% WR but only 7 trades — useless sample size.

---

## 10. Recommendations

### Immediate: Cap BTC at 30 contracts
Not because sizing was wrong, but because BTC's edge-to-variance ratio doesn't justify larger positions. Max BTC loss drops from ~$75 to ~$27.

### Short-term: Shadow-tag escalation type
After 30+ more direct-fill observations (~2–3 weeks), evaluate:
- If direct-fill WR stays below 85% → model miscalibrated for BTC
- If direct-fill WR recovers to 90%+ → today was variance

### Do NOT do yet
- Gate BTC entirely — evidence for "no edge" is directional but insufficient (p=0.39)
- Change edge floors — no filter produces meaningful positive returns

### Open Questions
1. Why does EGARCH produce declining vol for BTC? Lagging a regime shift?
2. Is the 73% escalation rate indicative of systematic BTC mispricing?
3. At what sample size can we confidently decide? (Power analysis: ~419 trades / 62 days)

---

## Appendix: Analysis Methodology Errors

1. **Era contamination**: Mixed pre-BLR data with post-BLR. BLR active for BTC through March 27.
2. **Product mixing**: Included KXBTCD (daily) in 15M analysis.
3. **Wrong intervention direction**: Entry price caps make BTC worse, not better.
4. **Narrative escalation on small samples**: Built dramatic conclusions by slicing 41 trades into tiny subgroups.
5. **Missing statistical rigor**: Didn't run t-tests or power analysis until late rounds.

## Related (KB operational articles)
- [[kb/concepts/per-asset-rules.md]]
- [[kb/concepts/edge-thresholds.md]]
- [[kb/concepts/execution-layer.md]]
- [[kb/concepts/drawdown-scaler.md]]
