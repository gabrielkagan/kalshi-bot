---
status: active
updated: 2026-04-05
tags: [edge, thresholds, price-dependent, probability]
---
# Edge Thresholds

## Summary

Edge thresholds are price-dependent minimum required edges that a signal must clear before becoming a trade candidate. Defined in `MIN_EDGE_BY_PRICE` (bot.py:616) and accessed via `get_min_edge()` (bot.py:625). Higher prices require higher edges because the asymmetric payoff at high prices means one loss wipes many wins. This is the primary quality gate in the scan pipeline.

## The Price-Dependent Schedule

As of Apr 2026 (bot.py:616-623):

| Price Range | Min Fee-Adjusted Edge | Rationale |
|---|---|---|
| 97-99c | 1.0% | One loss at 97c = ~33 wins needed to recover |
| 95-96c | 0.75% | One loss at 95c = ~19 wins |
| 93-94c | 0.5% | Near-misses at 93-95c: 95.5% WR historically |
| 91-92c | 0.20% | 193 settled at 94.3% WR, Wilson LB 90.1% |
| 89-90c | 0.25% | Halved from 0.5%: 2 rejected winners at 0.31-0.48% |
| 0-88c | 0.25% | Floor for ETH/SOL low-price zone |

The `MIN_EDGE_PCT = 0.25%` constant is a flat fallback only, used in execution paths where per-price lookup is not available. The real config is `MIN_EDGE_BY_PRICE`.

## Why Higher Prices Need Higher Edge

At 95c, the payoff is 5c per win but the loss is 95c. Breakeven win rate = ~95%. One loss erases ~19 wins of profit. A small edge miscalculation at high prices has catastrophic PnL impact. The tiered schedule ensures the required edge scales with this asymmetry.

At 89c, the payoff is 11c per win vs 89c loss. Breakeven ~89%. The asymmetry is less extreme, so a smaller edge threshold (0.25%) is justified.

## SOL-Specific Override

`SOL_MIN_EDGE = 0.010` (1.0%) applies as a floor across ALL SOL price tiers (bot.py:59). Data justification: SOL signals with <1.0% fee-adjusted edge have 82% WR vs >=1.0% at 94.2% WR on 258 trades. The SOL override is applied via `max(get_min_edge(price), SOL_MIN_EDGE)` in scan().

## Fee-Adjusted Edge Computation

The edge check operates on fee-adjusted edge, not raw edge:

```
edge = final_prob - best_ask / 100.0
est_fee_1c = ceil(0.07 * 1 * P * (1 - P))  # taker fee for 1 contract
fee_adjusted_edge = edge - est_fee_1c / 100.0
```

At typical prices (90c), the taker fee is ~1c per contract, reducing edge by ~0.01 (1pp). At 95c, fee is ~0.33c. Maker fills have $0 fee, but the edge check assumes taker (conservative).

## Weekend/Overnight Discount

During quiet hours, the threshold is reduced by a multiplier:

- **Weekend** (Sat/Sun): `MIN_EDGE_BY_PRICE * WEEKEND_EDGE_DISCOUNT (0.60)` = 40% reduction
- **Overnight** (weekday 04-11 UTC): `MIN_EDGE_BY_PRICE * OVERNIGHT_EDGE_DISCOUNT (0.60)` = 40% reduction

Discount is only live for 89c+, STC<=600s, no DC overlap. Below 89c and STC>600s, discount trades are shadow-only.

## Relaxed Edge Shadow

A secondary shadow captures signals at `0.5x MIN_EDGE_BY_PRICE` (the `relaxed_edge_shadow`). These are logged to `evaluated_opportunities` for counterfactual analysis but never traded. Purpose: determine if current thresholds are too strict by measuring settlement outcomes of near-miss signals.

## Historical Tuning

The thresholds have been lowered multiple times based on live data:

- **Original**: 2.0% at 97c, 1.25% at 95c, 0.9% at 93c, 0.5% at 89c
- **Multiple audits**: Found "MAY BE TOO STRICT" verdicts -- rejected signals that would have been profitable
- **Current**: Roughly halved at each tier, validated by settlement data (e.g., 93-94c incremental signals: 97.2% WR on 72 signals)
- **91-92c tier added**: Was grouped with 93c at 0.5%; split out to 0.20% based on 193 trades at 94.3% WR

## Interaction with MIN_ENTRY_PRICE

Per-asset price floors (`BTC_MIN_ENTRY_PRICE=88`, `ETH_MIN_ENTRY_PRICE=90`, etc.) are checked BEFORE the edge threshold. A signal at 88c for BTC never reaches the edge check. The two systems are complementary:

- Price floor: "is this price tier profitable for this asset at all?"
- Edge threshold: "given this price tier, is this specific signal strong enough?"

## STC Sizing Scaler (Apr 5, 2026)

`STC_SIZING_SCALER_ENABLED = True`, `STC_SIZING_SCALER_KNEE = 300`

Independent of edge thresholds, position sizing scales down with time-to-close:

```
if STC > 300s: contracts *= 300 / STC
```

| STC | Scaler | Effect |
|-----|--------|--------|
| 300s (5m) | 1.0x | No change |
| 420s (7m) | 0.71x | |
| 600s (10m) | 0.50x | |
| 900s (15m) | 0.33x | |

**Data:** 3-5 min is the sweet spot (94.6% WR, +$667). 7m+ is net negative (-$251). The scaler doesn't cut trades — it sizes them proportionally to time exposure. Applied in main 15M pipeline, overnight discount, and weekend discount. kelly_f stays pure.

**Interaction with LOW_STC_SIZING_CAP:** Mutually exclusive ranges — LOW_STC halves at <100s, STC scaler activates at >300s. The 100-300s range (including the sweet spot) is unscaled.

## Anti-Pattern: Flat Edge Analysis

Audit scripts that sweep a single flat edge threshold ("raise edge to 0.9%") produce misleading results because the bot already has 6 different thresholds. Any edge analysis must be **per-price-tier** to be actionable.

## Probability Parameters Feeding Edge

The edge check operates on `final_prob` which passes through these stages with our specific parameters:

| Parameter | Value | Purpose |
|---|---|---|
| MARKET_BLEND_W (15M) | 0.40 | 60% model, 40% market price |
| MARKET_BLEND_W (hourly) | 0.40 | Same as 15M |
| MARKET_BLEND_W (SPX) | 0.00 | No blend — CalEngine only |
| MARKET_BLEND_W (weather) | 0.20 | 80% model (ensemble is primary signal) |
| HOURLY_TEMPERATURE_T | 1.45 | Softens overconfident probs (95%→88.4%) |
| OFA_MAX_ADJUSTMENT | ±3pp | Order flow adjustment cap |
| Dynamic cap (>600s) | 0.97 | Prevents unreasonably high probs far from settlement |
| Dynamic cap (>300s) | 0.98 | Relaxes closer to expiry |
| Dynamic cap (<120s) | 0.999 | Near-expiry safety ceiling only |

## Related

- [[concepts/fee-optimization.md]] - Fee structure affecting fee_adjusted_edge
- [[concepts/per-asset-rules.md]] - Per-asset price floors and SOL_MIN_EDGE
- [[strategies/overnight-discount.md]] - Weekend/overnight edge discount details
- [[concepts/cal-engine-registry.md]] - Per-product CalEngine routing
