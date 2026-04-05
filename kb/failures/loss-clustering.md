---
status: resolved
updated: 2026-04-01
tags: [failure, loss, multi-asset, window-cap]
severity: critical
---
# Mar 31 Triple-Loss Window ($352)

## Summary
On Mar 31, 2026 at ~04:00 UTC (midnight ET), three trades in the same 15-minute window all lost, producing a $352 combined loss -- the worst single-window loss in bot history. The trades spanned three different assets and three different strategies, exposing the lack of a per-window aggregate risk cap.

## Symptom
Three simultaneous losses settled in one 15-minute window:

| Asset | Contracts | Entry Price | Strategy | Loss |
|-------|-----------|-------------|----------|------|
| BTC | 13 | 97c | MAKER_PATIENT | ~$39 |
| XRP | 279 | 96c | decided_t2_z2 | ~$223 |
| SOL | 76 | 94c | overnight_discount | ~$90 |

**Total: ~$352 in a single window.**

## Root Cause
No single trade was individually unreasonable -- each passed its own filters and sizing caps. The problem was structural: the bot had no mechanism to limit aggregate risk across concurrent positions in the same settlement window.

**Contributing factors:**
1. **Cross-asset correlation ignored.** BTC, SOL, and XRP moved against the bot simultaneously. The per-asset risk caps (BTC 15%, XRP 15%, SOL 15%) are independent -- they don't account for correlated crypto moves.
2. **Time of day.** 04:00 UTC (midnight ET) falls in the overnight discount window. Lower liquidity and wider spreads increase the chance of adverse moves.
3. **Elevated vol regime.** The window coincided with a period of above-average realized volatility across all three assets.
4. **Strategy stacking.** Three different strategies (MAKER_PATIENT, decided_t2_z2, overnight_discount) each independently sized their position. No cross-strategy cap existed.

## Impact
- Largest single-window loss in bot history
- Wiped approximately 2 days of profit
- Triggered re-evaluation of T2_Z2 decided contract strategy (subsequently re-shadowed)

## Fix (Deployed Apr 1, 2026)
Per-window aggregate risk cap implemented in `scan()`:

- **MAX_WINDOW_RISK:** Caps total risk across all strategies in a single settlement window
- **MAX_POSITIONS_PER_WINDOW:** Limits concurrent positions per window (already existed for hourly/SPX at 2, extended to 15M)
- Cross-asset positions in the same window now count toward a shared budget

The cap would have prevented the third trade from entering (SOL overnight_discount) once BTC + XRP already consumed the window budget, limiting the loss to ~$262 instead of $352.

## Consequences
1. **T2_Z2 re-shadowed:** The XRP 279ct@96c T2_Z2 loss was the largest single-trade component. Combined with the earlier T2_Z2 loss history (see [[failures/t2-z2-losses.md]]), T2_Z2 was moved back to shadow for further data collection.
2. **Per-window caps deployed:** First time 15M markets have an aggregate window cap. Hourly and SPX already had `MAX_POSITIONS_PER_WINDOW=2` and `MAX_WINDOW_RISK=0.15`.
3. **Overnight discount scrutiny:** The SOL loss at 94c in the overnight window raised questions about whether the 89c floor is sufficient for overnight trades.

## Lessons
- **Per-trade sizing is necessary but not sufficient.** Even correctly-sized individual trades can produce catastrophic aggregate losses when correlated.
- **Crypto assets are highly correlated in tail moves.** Treating BTC, ETH, SOL, XRP as independent for risk purposes is dangerous during vol spikes.
- **Midnight ET is a high-risk period.** Lower liquidity + overnight discount eligibility + multiple strategies active = concentration risk.
- **Strategy stacking without window caps = unbounded exposure.** The stacking infrastructure ([[concepts/stacking-infrastructure.md]]) handles composite PKs but didn't originally cap aggregate window risk.

## Related
- [[failures/t2-z2-losses.md]]
- [[concepts/dc-strategy.md]]
- [[strategies/overnight-discount.md]]
- [[concepts/stacking-infrastructure.md]]
