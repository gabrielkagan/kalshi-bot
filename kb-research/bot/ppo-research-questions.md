---
status: pending
updated: 2026-04-05
tags: [research, ppo, position-monitoring, questions, data-collection]
---
# PPO Research Questions (Data Collecting Since Apr 5, 2026)

Position price monitor v2 is live, logging spot_price, threshold, spot_buffer_pct, and Kalshi quotes (when available) every tick for held 15M positions. These questions need 3-7 days of data to answer.

## Entry Timing

1. **How tight is the buffer at entry?** Distribution of spot_buffer_pct at the first observation per position. If most entries have 0.1% buffer vs 0.5%, tells us about timing quality.
2. **Are we entering at local peaks or mid-move?** Compare spot at entry to min/max spot during hold. If entry is consistently near the max, we're buying at peaks.
3. **Does entry buffer predict win/loss?** Correlate initial buffer_pct with settlement outcome. Is there a buffer threshold below which WR drops sharply?

## Post-Entry Dynamics

4. **How fast does the buffer erode on losses?** Sudden cliff vs gradual decline. If losses show a sharp buffer collapse in the last 30 seconds, early exit wouldn't help. If it's gradual over 2-3 minutes, maybe it would.
5. **Is there a buffer threshold that predicts losses?** If buffer dips below 0.05% at ANY point during hold, what's the WR? Maybe a "buffer alert" signal exists.
6. **Do positions that eventually win ever dip into danger?** How often does a winning position's buffer go negative temporarily before recovering?

## Asset-Specific Patterns

7. **Do different assets have different buffer volatility?** SOL might have more volatile trajectories than BTC. If SOL buffers oscillate wildly, it explains the higher loss rate.
8. **Is there an asset where entries are systematically better/worse timed?** Maybe BTC entries always have fat buffers while XRP entries are razor-thin.

## STC Interaction

9. **How does buffer trajectory differ by entry STC?** Positions entered at STC=500s have more time for the buffer to erode vs STC=100s. Do high-STC entries show more buffer volatility?
10. **Does the STC scaler (300/STC) correctly calibrate risk?** Compare buffer volatility at different STC levels. If 400s positions have 2x the buffer swing of 200s positions, the scaler should reflect that.

## Kalshi Quote Availability

11. **When ARE Kalshi quotes available?** At what STC do orderbooks dry up? Is it gradual or sudden?
12. **When quotes exist, do they track spot faithfully?** Or is there a lag/divergence between spot-implied probability and Kalshi market price?

## Actionable Signals (Future)

13. **Could a "buffer deterioration alert" improve PnL?** If buffer drops below X% after entry, reduce position (sell partial). Requires Kalshi bid liquidity data.
14. **Can we predict losses from the first 30 seconds of trajectory?** If the buffer is shrinking in the first 30s, does that predict loss?
15. **Should entry timing be a signal for sizing?** Enter at fat buffer = size up, enter at thin buffer = size down.

## Data Availability Timeline

- **Apr 5-6**: First 24h of data. Enough for basic distributions (Q1, Q7, Q11).
- **Apr 7-8**: 3 days. Enough for per-asset comparisons (Q7, Q8) and buffer/outcome correlations (Q3, Q5).
- **Apr 10-12**: 1 week. Enough for statistically meaningful answers to all questions. Time to analyze.

## Related

- [[same-ticker-reentry-analysis.md]] — Original motivation (debunked, but led to discovering 97.6% data blind spot)
- [[price-drift-analysis.md]] — Price drift is net profitable; PPO data will help understand why
- [[goldmine-hunt-apr5.md]] — Broader alpha search context
