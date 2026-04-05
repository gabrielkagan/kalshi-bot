---
status: active
updated: 2026-04-03
tags: [sports, bayesian, espn, observation]
---
# Sports Comeback Engine

## Summary
The sports engine (`sports_engine.py`) is a fully independent daemon thread that monitors live games across 8 sport groups and 26 Kalshi league series. It polls ESPN for live scores, computes Bayesian comeback probabilities for trailing favorites, and logs shadow signals. Permanently observation-only (`SPORTS_OBSERVATION_ONLY = True`, hardcoded). Basketball is the best-performing group at 69.2% WR (n=39). SPRT sequential test status: CONTINUE_COLLECTING.

## Architecture
- Runs as a daemon thread launched by `MainLoop` — no access to `OrderExecutor` or `KalshiClient`
- Polls ESPN scoreboard API every ~30 seconds per league for live game states
- Discovers Kalshi game-level markets and captures pregame prices before tip-off
- Computes comeback signals for favorites that fall behind
- Logs to both `sports_shadow_log` (detailed) and `evaluated_opportunities` (unified pipeline)
- Settlement handled by bot.py's existing `SettlementTracker`

## Signal Generation Pipeline
1. **ESPN poll** -- fetch live scores, parse into `GameState` (period, clock, time_remaining_pct, scores)
2. **Favorite identification** -- pregame Kalshi prices determine the favorite (higher-priced side)
3. **Deficit classification** -- `classify_deficit_binary()` or `classify_deficit_three_way()` buckets the deficit size (small/medium/large)
4. **Time remaining classification** -- `classify_time_remaining()` buckets game clock into early/mid/late
5. **Strength classification** -- `classify_strength()` buckets pregame price into tiers
6. **Likelihood ratio lookup** -- `lookup_lr()` from pre-computed tables (`BINARY_LR_TABLE` or `THREE_WAY_LR_TABLE`) with conservative scaling (`CONSERVATIVE_LR_SCALE = 0.2`)
7. **Bayesian posterior** -- `comeback_prob = prior * LR / (prior * LR + (1 - prior))` where prior = pregame price
8. **Edge computation** -- `edge = comeback_prob - current_kalshi_price`, fee-adjusted

## Sport Groups (8)
| Group | LR Scale | Type | Example Leagues |
|-------|----------|------|-----------------|
| basketball | 0.2 | binary | NBA (KXNBAGAME) |
| hockey | 0.2 | binary | NHL (KXNHLGAME) |
| baseball | 0.2 | binary | MLB (KXMLBGAME) |
| football | 0.2 | binary | NFL (KXNFLGAME) |
| soccer | 0.2 | three_way | EPL, Bundesliga, La Liga, Serie A, UCL, MLS, Turkish Super Lig |
| tennis | 0.2 | binary | (configured but seasonal) |
| mma | 0.2 | binary | UFC (KXUFCFIGHT) |
| esports | 0.2 | binary | CS:GO, LoL, Valorant (no ESPN -- Kalshi price monitoring only) |

## Leagues (26 series)
Binary leagues (2 markets/game: Team A wins, Team B wins) and three-way leagues (3 markets/game: Home, Away, Draw for soccer). Leagues without ESPN endpoints (esports) skip live score polling and only monitor Kalshi prices.

## Performance Data
- **Basketball**: 69.2% WR on 39 settled signals -- best group by both WR and sample size
- **SPRT status**: CONTINUE_COLLECTING -- no group has reached statistical significance for promotion
- All groups use conservative LR scaling (0.2x) to avoid overconfidence on small samples

## Data Storage
- **`sports_shadow_log`** table: game_id, league, teams, comeback_prob, edge, market_result, pnl_cents, fav_won, filter_stage, evaluation_time, pregame prices, orderbook data
- **`evaluated_opportunities`**: unified pipeline entry with `product_type='sports'`, `filter_stage='sports_observation'`
- Per-sport-group `SportsCalEngine` fits on settled `sports_shadow_log` rows with H1/H2 train-test split

## CalEngine Integration
Per-sport-group CalEngines registered in `_CAL_REGISTRY` as `"sports_{group}"` (8 engines total). Each group has its own state file (e.g., `cal_sports_basketball.json`). Settlement routing in bot.py sends observations to the correct per-group engine via `_resolve_cal_engine("sports", asset)` where asset maps to sport group.

## Key Constraints
- **Hardcoded observation-only**: `SPORTS_OBSERVATION_ONLY = True` is not an env var -- requires code change to promote
- **No order placement code**: `sports_engine.py` has zero references to `OrderExecutor` or order submission
- **Off-season handling**: Leagues with 0 ESPN games skip silently (no errors, no signals)
- **Esports limitation**: No ESPN endpoint -- can only monitor Kalshi prices, cannot compute comeback signals

## Configuration
| Parameter | Value | Notes |
|-----------|-------|-------|
| `SPORTS_OBSERVATION_ONLY` | True | Hardcoded, never live without explicit promotion |
| `SPORTS_SHADOW_MIN_PRICE` | (from sports_data) | Minimum Kalshi price for signal logging |
| `SPORTS_SHADOW_MIN_LR` | (from sports_data) | Minimum likelihood ratio to fire signal |
| `CONSERVATIVE_LR_SCALE` | 0.2 | Global conservative scaling on all LR lookups |
| `MAX_MODEL_MARKET_GAP` | (from sports_data) | Max divergence between model and market price |

## Related
- [[concepts/cal-engine-registry.md]]
- See `kb-research/bot/sports-comeback-model.md` for complete Bayesian model derivation and per-sport analysis
