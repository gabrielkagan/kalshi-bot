---
status: pending
updated: 2026-03-18
tags: [research, dota, esports, design]
---
# Dota Draft Arbitrage — Complete Design

Source: Mar 2026
Chat link: https://claude.ai/chat/686d6681-c526-4219-99f4-ebec1d081fee

---

## Concept
Predict Dota 2 pro match outcomes during the drafting phase. Hero composition (picks + bans) creates asymmetric win probability that prediction markets may misprice. A model trained on historical draft-to-outcome data could identify when the market undervalues a draft advantage.

## Architecture Designed

### 12-Table Supabase Schema
Designed to integrate with existing Supabase infrastructure (project srbdajecmkjxinmcozxl). Tables cover: matches, drafts, heroes, hero synergies, counter-picks, team compositions, model predictions, market prices, signals, trades, evaluations.

### Data Sources (both free)
- **Steam Web API:** Free, 2-minute setup at steamcommunity.com/dev/apikey. Match data, player data, hero data.
- **STRATZ API:** Free, 2-minute setup at stratz.com/api. Richer pro match data, draft sequences, detailed match stats.

### Implementation Plan
1. **Backfill:** Pull 2,000+ historical pro matches from STRATZ into Supabase
2. **Feature engineering:** Hero synergies, counter-pick statistics, team composition metrics, draft-order effects
3. **Model training:** Draft composition → win probability. Initial model likely gradient-boosted trees on composition features.
4. **Validation:** Backtest against historical data — does the model identify exploitable mispricings?
5. **Live pipeline:** Subscribe to pro match draft phase, compare model probability vs market price, generate signals
6. **Shadow mode:** Log signals without trading, evaluate against shadow framework promotion criteria

### Relationship to Main Bot
Shares existing infrastructure:
- Supabase storage (same project)
- Shadow evaluation framework (Wilson CI, min-n threshold, forward-tested)
- Kelly sizing
- Execution layer (if Kalshi has esports markets)
- Same Bayesian probability → edge detection → sizing pipeline as other verticals

## Status
Design document completed. Implementation not started. Blocking dependency: validate the core thesis via backtest against 2,000+ historical pro matches before writing any live-tracking code. If backtested edge is insufficient or unstable, no point building the live system.

## Open Questions
- Does Kalshi offer Dota/esports markets? (Market availability is a prerequisite)
- How stable are draft-based win probability models across patches/meta shifts?
- Is the esports prediction market liquid enough for meaningful position sizes?
- How quickly does the market price draft information? If pros and analysts update prices within minutes of picks, the window may be too short.

## Related (KB operational articles)
- [[kb/concepts/sports-engine.md]]
