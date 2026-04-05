---
status: active
updated: 2026-04-03
tags: [methodology, guards, protocol, hwm]
---
# Guard-Aware Development Protocol

## Summary

Mandatory investigation-before-implementation protocol for any change touching balance, sizing, HWM, guards, or caches. Originated from five silent sizing bugs caused by guard false-positives between Mar 25-30, 2026. The drawdown scaler cascade (d823366 through e5d73d2) proved that naive fixes to balance-dependent code create secondary failures in guards, caches, and warmup logic that only manifest under specific market conditions.

## When It Applies

Any change touching: portfolio balance reads, position sizing, HWM (high-water mark), drawdown scaler, spike rejection, deque-based windows, balance caches, or warmup logic. If the change modifies a value that flows into a guard or filter, this protocol is mandatory.

## The 5-Step Protocol

### Step 1: Interaction Graph

Trace the changed value through every guard, filter, and cache it touches. Draw the dependency chain. For example, a balance change flows through: HWM comparison, drawdown ratio, drawdown scaler, sizing clamp, and the spike rejection guard. Missing any node means a silent bug.

### Step 2: Adversarial Analysis

Identify the top 3 ways the change could break something. Focus on edge cases: bot startup with no history, recovery from drawdown, balance near zero, balance spikes after a large win. Each scenario must be checked against the interaction graph from Step 1.

### Step 3: Settlement Gauntlet Test

Run the standard gauntlet: balance goes $1,400 to $1,050 (drawdown) to $1,420 (recovery). Verify that sizing remains sane at every step. The drawdown scaler should engage during the dip and release during recovery. HWM should update correctly. No guard should permanently lock out trading.

### Step 4: Runtime Invariants

Add assertions near the consumer of the changed value. These catch bugs at runtime before they cause bad trades. Example: assert that sized contracts > 0 when edge > 0 and balance > 0. Invariants belong near the point of use, not the point of mutation.

### Step 5: Meta-Rule for New Guards

Before writing any new guard or filter, write a false-positive test FIRST. The test should prove the guard does not block legitimate trades under normal conditions. Guards that only have true-positive tests are the ones that silently kill volume for weeks.

## Known Landmines

| Component | Risk | What Goes Wrong |
|-----------|------|----------------|
| Spike rejection | Rejects legitimate balance jumps | Large wins trigger rejection, HWM stalls, drawdown scaler locks |
| 7-day deque | Warmup period produces bad median | Median of 3 values can be wildly wrong, scaler miscalculates |
| Drawdown thresholds | Zero-balance denominator | Division by zero or near-zero produces extreme scaler values |
| Balance cache | Stale reads | Cache returns old balance, sizing uses wrong base, guards misfire |
| Warmup median | Inflated by initial deposit | First few days have no losses, median is artificially high, scaler is too aggressive |

## Origin: The Drawdown Scaler Cascade (Mar 25-30, 2026)

Five bugs in five days, all from the same root cause pattern:
1. HWM warmup used raw balance including unrealized PnL -- inflated HWM
2. Fractional bankroll (hourly/SPX) leaked into portfolio-level HWM
3. Zero-balance halt when drawdown ratio hit exactly 1.0
4. Spike rejection blocked legitimate recovery, locking scaler at max drawdown
5. Drawdown scaler used product-level sizing balance instead of portfolio balance

Each fix created a new edge case in a downstream guard. The protocol exists to break this cycle.

## Related

- [[failures/hwm-bugs.md]] - The five HWM/drawdown scaler bugs that motivated this protocol.
- [[concepts/drawdown-scaler.md]] - The drawdown scaler component itself.
