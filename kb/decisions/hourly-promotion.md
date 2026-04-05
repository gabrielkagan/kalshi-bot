---
status: decided
updated: 2026-03-21
tags: [decision, hourly, promotion]
date: 2026-03-21
---
# Decision: Promote Hourly Crypto Markets to Live

Date: 2026-03-21
Status: Decided

## Context

Hourly crypto markets (KXBTCD, KXETHD, KXSOLD, KXXRPD) had been running in
observation mode for 14 days, collecting settlement data across all assets
and price ranges. The observation period produced a large dataset of 1,474
unique tickers with full settlement outcomes.

Key observation findings:

- **66.3% win rate** vs **46.5% breakeven** -- a 20pp edge
- Sub-60c price range showed the strongest signal; 70-79c was a "death zone"
- BTC and ETH were profitable; SOL was marginal; XRP was toxic (42.9% WR)
- STC sweet spot at 600-1800s (10-30 min before close)
- Edge inverted above 5% -- the 10%+ edge zone had only 24.2% WR
- Temperature scaling T=1.45 was needed to correct overconfident 15M calibration

## Options Considered

1. **Continue observation** -- more data but delay monetization of a clear edge.
2. **Full promotion** -- all assets, all prices. Risk: XRP and high-price
   zones would destroy PnL.
3. **Constrained promotion** -- live with tight guardrails on assets, prices,
   sizing, and timing. Accept smaller upside for controlled risk.

## Decision

Constrained promotion (option 3). Deployed in commit 7cabfe6 with these
guardrails:

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Assets | BTC + ETH only | SOL marginal, XRP 42.9% WR = toxic |
| Price range | Sub-60c (50-59c) | Edge lives at low prices |
| Execution | Taker-only IOC | No maker orders, no lock contention with 15M |
| Sizing | Fixed 10 contracts | Bypass Kelly -- too early for model-based sizing |
| Bankroll fraction | 10% | Isolated from 15M bankroll |
| Max edge cap | 5% | >5% edge zone inverts to 24.2% WR |
| STC window | 600-1800s | 10-30 min sweet spot |
| CalEngine | Disabled | Passthrough + T=1.45 (hourly beta_cal +44pp overconfident) |
| Kill switch | HOURLY_LIVE_ENABLED env var | Must be "1" to trade; default "0" |

SOL and XRP excluded via HOURLY_EXCLUDED_ASSETS = {SOL, XRP}.

Hourly data is excluded from 15M CalibrationEngine training to prevent
contamination (`load_training_data_from_db()` filters by product_type).

## Consequences

- Hourly markets trade live on VPS when HOURLY_LIVE_ENABLED="1"
- Fixed 10-contract sizing limits both upside and downside during early live
- Taker-only avoids the per-asset lock contention that 15M maker orders use
- HOURLY_OBSERVATION_ONLY derived: `not HOURLY_LIVE_ENABLED`
- Kill switch allows instant revert without code deploy
- Shadow configs h/j/k killed (55% WR) during cleanup

## Related

- [[concepts/fee-optimization.md]] - Taker-only fee impact at sub-60c prices
- [[strategies/hourly-markets.md]] - Full strategy documentation
