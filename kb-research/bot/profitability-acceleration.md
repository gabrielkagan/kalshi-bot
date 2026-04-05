---
status: active
updated: 2026-03-20
tags: [research, profitability, per-asset, execution]
---
# Profitability Acceleration Research — Complete Analysis

Source: Deep research session Mar 2026
Chat link: https://claude.ai/chat/2e28531a-07ee-42d6-88c3-50192c210a59

---

## Context
604 settled trades, $211.92 lifetime PnL on ~$900 bankroll (~23.5% ROI), 29 days live. 7 ranked research questions to identify highest-leverage PnL improvements per day.

## Pipeline at Time of Research
15M pipeline: EGARCH → z-score → NIG probability → Beta/Platt/BLR calibration → 60/40 model/market blend → price-dependent edge threshold → Kelly sizing → maker-first execution

DC overlay: z-score depth signals at 93-99c, direct IOC taker, retry queue with 8s spacing, up to 5 retries.

Fee structure: Taker fee = ceil(0.07 × contracts × P × (1-P)), Maker = $0, SPX taker = 0.035 coefficient.

## Research Question 1: MAKER_PATIENT at sub-84c — Fix or Kill?

**Data:** Worst strategy at 75.6% WR, -$54.01 in 7 days, avg entry price 81c.

**Analysis:** Low-price contracts have unfavorable risk/reward asymmetry. Winning a 81c contract pays 19c. Losing costs 81c. You need >81% WR just to break even. At 75.6% WR, you're losing money every trade on average.

**Resolution:** Per-asset MIN_ENTRY_PRICE raises:
- BTC: 89c (can't generate sub-89c signals at this floor)
- ETH: 88c (raised from 80c)
- SOL: 86c (kept lower due to higher volume/edge at these levels)
- XRP: 92c (highest floor due to microstructure issues)

Estimated savings: ~$2.30/day from avoided bad trades.

## Research Question 2: DC Overlay Optimization

**DC was already the highest alpha strategy:**
- Near-100% WR on clean tiers (T1/T1B/T2/T2_Z25)
- z-score depth signals at 93-99c identify contracts extremely likely to settle YES
- Direct IOC taker execution (no maker attempt — speed matters)

**Improvements researched:**
- **Multi-layer fill capture:** 5 layers from shadow orderbook monitoring → WS event trigger → expanded retry queue → price tolerance → adaptive timing
- **Retry expansion:** 10 retries instead of 5, spaced to hit different points in liquidity cycle
- **WebSocket trigger:** If a sell appears on a ticker being watched, snipe instantly
- **Price tolerance:** Retries 3+ accept 94c, 95c, 96c (liquidity may exist 1-2c above original target)

**Key insight:** Nothing removed, nothing gated, nothing blocks existing path. Worst case: additional layers produce zero fills but you still have exactly the fills you had before.

## Research Question 3: SOL Maker Adverse Selection

**Finding:** SOL maker fill rate was 7.7% at low STC. When maker orders DO fill on SOL, it's because the price moved against you (adverse selection).

**Resolution:** SOL_TAKER_FIRST = True. SOL never attempts maker — goes directly to taker. Eliminates adverse selection at cost of taker fees.

## Research Question 4: Fee Optimization

**Fee math:**
- Taker: ceil(0.07 × contracts × P × (1-P))
- Maker: $0
- Fee is maximized at P=0.50 and decreases toward P=0 or P=1
- At high prices (90c+), taker fees are small: ~0.63c per contract at 90c

**Implication:** High-price contracts (89c+) are naturally fee-efficient. The per-asset floor raises (Q1) simultaneously solve the WR problem AND the fee problem.

## Research Question 5: Weekend/Overnight Discount

**Weekend data:** 4x weekday $/trade during weekend hours.

**Graduated from shadow:** 75 settled at 89c+, 93.8% WR. Promoted live.

**Overnight discount:** Also promoted after similar evaluation. Edge discount factor applied during overnight hours when markets are less efficient.

**Subtlety discovered:** At 89-90c, the edge gap between normal (0.25%) and discounted (0.15%) thresholds was only 0.10pp. Most of weekend/overnight value concentrated at 93c+ where the gap is more meaningful. Same pattern as regular edge thresholds — high-price contracts drive the alpha.

## Research Question 6: XRP Status

**XRP at time of research:** Primary profit engine with 100% WR and +$82.54 in 7 days.

**XRP evolution:**
- 15M: Eventually gated from live (microstructure issues, kurtosis, regime-switching)
- Hourly: Fundamentally broken
- Later: Re-evaluated and promoted with tiered risk (12% max risk cap, 92c floor)

## Research Question 7: NBBO Fill Rate

**Before NBBO:** DC fill rate was the primary bottleneck — strategy had near-100% WR but couldn't fill every opportunity.

**After NBBO deployment:** Fill rate approximately doubled.

**Per-ticker execution lock:** Deployed to prevent simultaneous trades on same asset. Before fix, 86% SOL candidate blocking from overly broad per-asset lock.

## What Was Actually Implemented (from this research)
1. Per-asset MIN_ENTRY_PRICE raises (BTC 89c, ETH 88c, SOL 86c, XRP 92c)
2. DC overlay with expanded retry queue (10 retries, adaptive timing, price tolerance)
3. SOL_TAKER_FIRST = True
4. Weekend discount promoted live (93.8% WR on 75 settled)
5. Overnight discount promoted live
6. NBBO deployment (doubled DC fill rate)
7. Per-ticker execution lock (fixed 86% SOL blocking)

## Related (KB operational articles)
- [[kb/concepts/per-asset-rules.md]]
- [[kb/decisions/sol-taker-first.md]]
- [[kb/strategies/overnight-discount.md]]
- [[kb/concepts/execution-layer.md]]
- [[kb/concepts/dc-execution-mechanics.md]]
