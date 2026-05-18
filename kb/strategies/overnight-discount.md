---
status: active
updated: 2026-05-18
tags: [strategy, overnight, weekend, live]
---
# Overnight and Weekend Edge Discount

## Summary
Applies a 40% edge reduction (`WEEKEND_EDGE_DISCOUNT = 0.60` multiplier) during low-liquidity periods — weekday overnight (04-11 UTC) and weekends (Sat/Sun). Promoted to live at 89c+, STC <= 600s, with no DC overlap. Sub-89c and STC > 600s remain shadow.

## Rationale
During overnight and weekend hours, crypto market microstructure changes:
- Lower liquidity on underlying exchanges
- Wider spreads on Kalshi
- Potentially less reliable price signals

The edge discount conservatively reduces the bot's probability estimate to account for this uncertainty. A 40% reduction means a 5% edge becomes 3% effective edge.

## Overnight Discount (Weekday 04-11 UTC)
| Parameter | Value |
|-----------|-------|
| `OVERNIGHT_DISCOUNT_LIVE` | True |
| Hours | 04:00-11:00 UTC (weekdays only) |
| `OVERNIGHT_DISCOUNT_MIN_PRICE` | 89c |
| `OVERNIGHT_DISCOUNT_MAX_STC` | 600s |
| DC overlap | Excluded — same-tick `_ovn_dc_overlap` (z/price/STC heuristic) + cross-tick `_ovn_dc_retry_overlap` (executor `_dc_retry_queue` scan; B5-fu4, 2026-05-18) |
| Non-OVN same-side position | Blocked — `_ovn_non_ovn_position` per-(ticker, side='yes') entry-lock (B5-fu4) |

## Weekend Discount (Saturday + Sunday)
| Parameter | Value |
|-----------|-------|
| `WEEKEND_DISCOUNT_LIVE` | True |
| Days | Saturday and Sunday (all hours) |
| `WEEKEND_DISCOUNT_MIN_PRICE` | 89c |
| `WEEKEND_DISCOUNT_MAX_STC` | 600s |
| DC overlap | Excluded — same-tick `_wknd_dc_overlap` + cross-tick `_wknd_dc_retry_overlap` (B5-fu4, 2026-05-18) |
| Non-WKND same-side position | Blocked — `_wknd_non_wknd_position` per-(ticker, side='yes') entry-lock (B5-fu4) |

## Shadow Zones
Trades that meet discount criteria but fall outside the live gates are logged as shadow:
- **Sub-89c:** Price too low for live discount trades (higher risk of loss at lower prices)
- **STC > 600s:** Time too far from expiry for live discount trades
- Both zones continue collecting data for potential promotion

## Promotion History
- Deployed as shadow initially
- Weekend discount promoted based on 75 settled trades at 89c+, 93.8% WR
- Overnight discount promoted based on similar data (weekday overnight has comparable liquidity profile to weekends)

## Interaction with Other Strategies
- No overlap with DC strategies — if a contract qualifies as DC, it trades under DC rules (no discount applied). Post-B5-fu4 (2026-05-18, ticket `86ba05k5q`), the DC overlap check is two-sided: same-tick `_*_dc_overlap` AND cross-tick `_*_dc_retry_overlap` against `executor._dc_retry_queue` close the prior-tick decided_* in-flight class that the z-score heuristic missed.
- TM cross-stacking is now mutually blocked. B5 (TM side) refuses TM entry when WKND/OVN has an open same-side position; B5-fu4 (WKND/OVN side) refuses WKND/OVN entry when TM has an open same-side position via the `_*_non_*_position` per-(ticker, side='yes') entry-lock. NO-side strategies (e.g., `bracket_no`) do NOT participate in this lock — they remain free to coexist on the same ticker.
- Hourly markets are not affected (separate product type).
- See [[concepts/stacking-infrastructure.md]] § "Weekend Discount + Overnight Discount — B5-fu4 gates" for the full predicate enumeration and the L104 ratchet promotion.

## Related
- [[concepts/per-asset-rules.md]]
- [[concepts/execution-layer.md]]
- See `kb-research/bot/profitability-acceleration.md` for weekend/overnight discount graduation data
