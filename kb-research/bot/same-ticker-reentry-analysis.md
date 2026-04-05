---
status: resolved
updated: 2026-04-05
tags: [research, stacking, reentry, addon, exposure, dedup, debunked]
---
# Same-Ticker Re-Entry Opportunity Analysis

Date: April 5, 2026
Status: **DEBUNKED** — re-entry is worse than base rate. Real finding: post-entry blindness.

## Summary

The bot blocks same-ticker re-entry via two mechanisms: (1) `_eval_opp_seen` dedup prevents re-evaluation as `candidate`, and (2) exposure deduction at line 8941 subtracts existing position contracts from new sizing. This means when a position is held at 97c and the price drops to 92c (creating a 2.7x better per-contract entry), the bot cannot add contracts.

## The Opportunity (VERIFIED — Apr 5 Deep Dive)

Full cross-reference of settled_trades × evaluated_opportunities reveals:

**136 re-entry evaluations across 81 unique tickers (6.5% of all traded tickers)**

### By Price Drop
| Drop | Evals | Tickers | WR | Key |
|------|-------|---------|-----|-----|
| 1c | 34 | 20 | 97.1% (33W/1L) | |
| 2c | 24 | 17 | 91.7% (22W/2L) | |
| **3c** | **36** | **20** | **100% (36W/0L)** | **Zero losses at 3c+** |
| 4c | 11 | 9 | 100% (11W/0L) | |
| 5c | 23 | 15 | 100% (23W/0L) | |
| 6-8c | 8 | 5 | 100% (8W/0L) | |

**At 3c+ drop: 78/78 = 100% WR. ZERO losses.**

### By Asset
| Asset | n | WR | Avg Drop | Notes |
|-------|---|-----|----------|-------|
| XRP | 66 | 100% (66W/0L) | 2.9c | Largest volume, zero losses |
| SOL | 54 | 94.4% (51W/3L) | 3.2c | All 3 losses are here |
| BTC | 15 | 100% (15W/0L) | 2.5c | |
| ETH | 1 | 100% (1W/0L) | 2.0c | Very rare |

### By STC
| STC | n | WR | CF PnL (50ct) |
|-----|---|-----|---------------|
| 0-120s | 4 | 100% | $12.46 |
| 120-180s | 9 | 100% | $23.91 |
| **180-300s** | **101** | **99.0%** | **$208.99** |
| 300-600s | 22 | 90.9% | -$9.22 |

Sweet spot: 180-300s STC (74% of all re-entries, 99% WR).

### What the Bot Did When It Saw the Lower Price
- `candidate`: **62 evals (96.8% WR)** — bot wanted to trade but exposure deduction blocked it
- `dc_shadow_t2_z2`: 22 (100% WR)
- `relaxed_edge_shadow`: 21 (95.2% WR)
- DC overlays: 14 combined (100% WR)

**62 of 136 were already `candidate` — the signal passed ALL filters but was blocked by existing position.**

### Counterfactual PnL
- All re-entries at 50ct: **$236.14** over ~40 days = ~$5.90/day
- 3c+ drops only: **$247.22** (no losses to drag it down)
- Per-ticker deduped (best price only): $139.69 on 81 tickers (79W/2L = 97.5% WR)

### Frequency
- ~4.0 unique tickers/day with re-entry ops (range: 2-12)
- Spiking on volatile days (Apr 4: 12, Apr 5: 8)

### The Typical Pattern
Enter at 97-99c → price dips to 92-96c → settles YES. Top combos: 97c→94c (16 evals, 100%), 99c→94c (14, 100%), 99c→96c (12, 100%). Nearly all re-entries are at 90c+ (135/136).

### The 3 Losses (All SOL)
All 3 losses occurred at 1-2c price drops in the 300-600s STC zone. At 3c+ drop AND STC < 300s, there are ZERO losses in the dataset.

## Why the Downward Case Matters

| Metric | First Entry (97c) | Re-Entry (92c) |
|--------|-------------------|----------------|
| Win per contract | $0.03 | $0.08 |
| Loss per contract | $0.97 | $0.92 |
| Upside ratio | 1x | 2.7x |
| Breakeven WR | 97% | 92% |

The second entry at a lower price has dramatically better economics — more profit per win, less loss per loss, lower breakeven WR. Same settlement outcome (both YES or both NO).

## Current Blocking Mechanisms

1. **`_eval_opp_seen` dedup (line ~6097):** Tracks `(ticker, filter_stage)` tuples. Once a ticker is tagged as `candidate`, it cannot be re-tagged in the same scan cycle. The set IS cleaned of expired tickers but retains active ones.

2. **Exposure deduction (line 8941-8951):** Subtracts existing position contracts from new Kelly sizing. If existing 50 contracts and Kelly says 40, net = -10 → zero_sizing → blocked.

3. **STACKING_ENABLED = False (line 232):** All position checks (DC, TM, main) see ALL positions regardless of strategy group. No multi-group stacking occurs.

## Architecture for Re-Entry

The stacking infrastructure already exists:
- Composite PK: (ticker, strategy_group, status)
- `strategy_to_group()` maps strategies to groups
- Settlement handles multi-group positions (fetchall, per-position PnL)
- DC and TM already use strategy groups
- `is_stacked` flag tracks multi-position tickers

## Risk Analysis

Combined exposure at 50+50 contracts (97c + 92c):
- Total loss if NO: $48.50 + $46.00 = $94.50 = 6.6% of $1,431 balance
- Within BTC_MAX_RISK_PER_TRADE (15% = $214.65)
- Within MAX_TICKER_RISK (25% = $357.75)
- With STC scaler: first at STC=400s (~37ct) + second at STC=360s (~42ct) = 79ct combined

## CORRECTION: Stress Test Results (Apr 5, later session)

### The 78/78 = 100% Claim Was Wrong

Full dataset analysis: **1,304/1,404 = 92.9% WR with 100 losses** at 3c+ drop. The original 78/78 was from a narrow DC-overlay subset, not the full picture.

### Re-Entry Is Worse Than Base Rate

| Group | WR | n |
|-------|-----|---|
| Base rate (90-96c, 180-300s STC) | **94.0%** | 1,021 |
| Re-entry (held + 3c+ drop) | **90.0%** | 10 |
| Fisher exact test | p=0.46 | Not significant |

The price drop is a slight NEGATIVE signal, not positive. When market drops 97c→94c, z-score averages -1.0 (spot genuinely near strike). 21% of the time price keeps falling by avg 29c more — catching a falling knife.

### Additional Problems
- 86% of re-entry signals have zero ask depth (unfillable)
- Occupied timeslot check (line 6484) means 97.6% of held tickers get ZERO post-entry evaluation
- Wilson CI [91.4%, 94.1%] does not clear breakeven at 90c+

### What IS Actionable: Post-Entry Blindness

The REAL finding: once the bot trades a ticker, it stops scanning entirely (occupied timeslot at line 6484). We're 97.6% blind to post-entry price movement. A lightweight held-position price monitor would give us:
- Post-entry price trajectories
- Whether entries are well-timed or systematically early
- Input for potential early-exit signals on deteriorating positions

### Verdict
**Re-entry stacking: DEAD. Do NOT implement.**
**Post-entry monitoring: WORTH implementing for observability.**

## Related

- [[../kb/concepts/stacking-infrastructure.md]] — Stacking architecture
- [[../kb/concepts/execution-layer.md]] — Execution paths
- [[goldmine-hunt-apr5.md]] — Broader alpha search context
