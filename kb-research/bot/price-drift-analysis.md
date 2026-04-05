---
status: active
updated: 2026-04-05
tags: [research, execution, drift, price, maker, taker, stc-gate]
---
# Price Drift Analysis: Scan-Time vs Fill-Time Price Mismatch

Date: April 5, 2026
Status: Investigation complete, implementation plan needed

## Summary

68.8% of all trades (788/1,146) have a fill price different from scan-time price. This causes STC gate violations, per-asset floor violations, and edge thresholds applied at wrong tiers.

## Scope

| Metric | Value |
|--------|-------|
| Trades with price drift | 788/1,146 (68.8%) |
| Avg drift | +1.26c (bimodal: maker UP, taker DOWN) |
| STC gate violations (scan 90c+ → fill <90c at STC>600) | 6 confirmed |
| Per-asset floor violations | 30 trades |
| Sub-global-floor fills (below 75c) | 7 trades |
| Edge threshold boundary crossings | 537 trades |

## Direction

| Direction | n | Avg Drift | WR | Risk |
|-----------|---|-----------|-----|------|
| UP (fill > scan) | 458 (58.1%) | +3.79c | 95.4% | Benign — more conservative |
| DOWN (fill < scan) | 330 (41.9%) | -2.24c, min -47c | 88.2% | **Adversarial — fills below gates** |

## Maker vs Taker

| Type | Mismatch Rate | Avg Drift | Direction |
|------|---------------|-----------|-----------|
| Maker (escalation_wait/none) | **91.0%** | +2.03c | UP (benign — price improvement) |
| Taker (direct_taker/sol_override) | **33.4%** | -2.08c | DOWN (adversarial — below gates) |

Maker drift is benign (fills at higher price = more certain market). Taker drift is the problem — IOC fills at lower prices than scan-time ask.

## Execution Paths and Fresh-Ask Status

| Path | Lines | Fresh Ask? | Drift Risk |
|------|-------|-----------|------------|
| Maker submission | 13866-13874 | **NO** | Posts at scan-time price, fills wherever |
| Maker fill detection | 14239-14240 | N/A | Records actual fill price (correct) |
| Taker escalation | 13109-13182 | YES | Re-fetches orderbook before IOC |
| Direct taker (<180s) | 12566-12604 | YES | Fresh ask before IOC |
| TM executor | 13432-13454 | YES | Fresh ask + size re-derivation |
| LPNE executor | 13532-13572 | YES | Fresh ask before IOC |
| SOL taker-first | 12390-12435 | YES | Fresh ask + 1c IOC offset |

**The maker submission path is the only one WITHOUT fresh-ask re-validation.** But maker fills drift UP (benign). The real issue is that the SCAN-TIME gates (STC threshold, price floor, edge tier) are evaluated against a price that may not match the fill.

## Specific Bug: STC Gate at 90c Boundary

The STC gate uses price-dependent thresholds: 90c+ allows 700s STC, <90c allows 600s. When scan is at 90c (700s allowed) but fill is at 89c (600s required), trades with STC 600-700s slip through.

Example: BTC scanned at 90c, STC 665s → passes 700s gate. Maker fills at 89c → should have been blocked at 600s. Result: -$16.91 loss.

6 confirmed cases. This is a narrow but real bug.

## Post-Fill Re-Validation Options

1. **After maker fill**: re-check entry_price against STC gate, asset floor, edge tier. If violated, could close position immediately (sell back). Complex and risky.

2. **At execution time for taker escalation**: already done (fresh ask re-derivation at line 13109). The taker escalation path is safe.

3. **At maker submission time**: add fresh-ask check before posting. Would catch the ~9% of maker orders where the market has already moved. But maker drift is benign — this is low priority.

4. **Widen the STC gate boundary**: instead of hard 90c threshold, use 91c or 92c to create a buffer. Trades at 90c that drift to 89c would still be within the gate. Simple, no execution changes.

## Profit-Maximizing Analysis (CORRECTED — Initial "Fix" Was Wrong)

The initial recommendation to widen the STC gate to 92c was wrong. The data shows:

### Price Drift Is NET PROFITABLE

| Group | n | WR | Total PnL | $/trade |
|-------|---|-----|-----------|---------|
| No drift | 366 | 96.4% | $361.26 | $0.99 |
| UP drift (fill > scan) | 449 | 95.3% | $95.94 | **$0.21** |
| **DOWN drift (fill < scan)** | **331** | **88.2%** | **$413.29** | **$1.25** |

**DOWN-drift fills are the most profitable group** — $1.25/trade, 47.5% of total PnL. Getting cheaper fills (entry below scan) produces wider margins on wins that more than compensate for lower WR.

### Sub-Floor Fills Are Profitable

135 trades filled below asset MIN_ENTRY_PRICE. Net PnL: **+$108.12** (12.4% of total PnL). Blocking them would LOSE money.

### The 6 STC Gate Violations Are Negligible

5 wins, 1 loss. Net PnL: -$11.46. Not worth a code change.

### Tightening the STC Gate to 92c Would Cost Money

9 trades at 90-91c with STC 600-700s: 8W/1L, -$4.48. The "fix" costs more than the bug.

### Why the Initial Recommendation Was Wrong

At 93.5% WR, every gate has asymmetric costs: blocking a loser saves ~91c/contract but blocking a winner costs ~7c/contract. At 93.5% WR, for every 1 loser blocked, 14 winners are also blocked. Net: 14 × 7c = 98c cost vs 1 × 91c benefit. Gates barely break even when perfectly calibrated; any imprecision (like drift-induced boundary crossings) tips them negative.

## Actual Recommendation: Do Nothing (For Now)

Price drift is a FEATURE, not a bug. The bot's current execution mechanics produce a profitable mix of fills. The "violations" (sub-floor fills, STC gate crossings) are net profitable because they let through trades the model liked but the gates rejected.

### If We Must Act: Post-Fill Validation (Future)

The one defensible improvement: after a fill, check if the fill price falls in a genuinely dangerous zone (e.g., fill at 47c when scan was 94c — the 7 extreme sub-75c fills). This catches only the truly adversarial fills while preserving the beneficial down-drift fills. But at n=7 extreme cases out of 1,146 trades, the expected savings are <$30 total. Not a priority.

## Related

- [[../kb/concepts/edge-thresholds.md]] — Edge threshold schedule
- [[../kb/concepts/execution-layer.md]] — Execution paths
- [[../kb/failures/ioc-subfloor-fill.md]] — IOC sub-floor fill bug (related)
