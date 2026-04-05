---
status: active
updated: 2026-04-01
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
| DC overlap | Excluded (DCs have their own entry logic) |

## Weekend Discount (Saturday + Sunday)
| Parameter | Value |
|-----------|-------|
| `WEEKEND_DISCOUNT_LIVE` | True |
| Days | Saturday and Sunday (all hours) |
| `WEEKEND_DISCOUNT_MIN_PRICE` | 89c |
| `WEEKEND_DISCOUNT_MAX_STC` | 600s |
| DC overlap | Excluded |

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
- No overlap with DC strategies — if a contract qualifies as DC, it trades under DC rules (no discount applied)
- TM is not affected (TM has its own criteria)
- Hourly markets are not affected (separate product type)

## Related
- [[concepts/per-asset-rules.md]]
- [[concepts/execution-layer.md]]
- See `kb-research/bot/profitability-acceleration.md` for weekend/overnight discount graduation data
