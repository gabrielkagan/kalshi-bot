---
status: decided
updated: 2026-03-20
tags: [decision, xrp, promotion]
date: 2026-03-20
---
# Decision: XRP Promoted from Shadow to Live

Date: 2026-03-20 (approximate)
Status: Decided

## Context
XRP 15M trading was in shadow mode (`XRP_15M_SHADOW = True`) due to historically negative PnL (-$32.97 all-time at the time of shadowing). The bot logged XRP signals to `evaluated_opportunities` with `filter_stage='xrp_shadow'` for counterfactual analysis.

After accumulating shadow data with the current configuration (post-BLR passthrough, post-EGARCH blend, post-TV-RK), the XRP shadow results showed strong performance at high entry prices.

## Options Considered
1. **Keep XRP in shadow** — Continue collecting data
   - Con: Missing profitable trades while waiting for more data
   - Pro: More statistical confidence
2. **Promote XRP at 88c+ floor** — Standard floor like BTC
   - Risk: XRP's thinner liquidity and wider spreads make sub-92c entries riskier
   - Data showed PnL negative at every floor below 90c
3. **Promote XRP at 92c+ floor** — Conservative high floor
   - Data: 41W/2L at >= 92c, 95.3% WR
   - Profit factor 1.68 at >= 92c

## Decision
Promote XRP to live with `XRP_MIN_ENTRY_PRICE = 92` and `XRP_MAX_RISK_PER_TRADE = 0.12` (12% cap).

Set `XRP_15M_SHADOW = False` to remove the shadow gate.

## Data
| Floor | WR | PnL | Notes |
|-------|-----|-----|-------|
| All prices | Negative | -$32.97 | All-time pre-shadow |
| >= 88c | Marginal | ~breakeven | |
| >= 90c | Positive | Small | |
| >= 92c | 95.3% (41W/2L) | Positive, PF 1.68 | Selected |

## Consequences
- XRP contributes live trades at 92c+ (approximately 5-10 trades/day)
- 12% risk cap mitigates XRP-specific risks (RK vol underestimates, thin liquidity)
- XRP NBBO fallback gates set at 92-99c, STC <= 300s (matching the floor)
- Counterfactual tracking continues for sub-92c via price-dependent filtering

## Related
- [[concepts/per-asset-rules.md]]
