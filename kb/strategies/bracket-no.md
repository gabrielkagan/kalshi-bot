---
status: active
updated: 2026-04-01
tags: [strategy, weather, no-side, live]
---
# Weather Bracket NO Strategy

## Summary
Buys NO on weather bracket markets where YES is priced at 88-96c, exploiting the structural 91.7% NO settlement rate in this price range. Fixed 5-contract sizing, 8+ hour STC minimum, kill switch at -$20 cumulative PnL.

## Edge Source
Weather bracket markets (e.g., "Will temperature be 75-80F?") at 88-96c YES price have a 91.7% NO settlement rate. The market overprices YES because casual bettors anchor on the bracket seeming "likely" without considering the full distribution of outcomes. A bracket that looks 90% likely often settles NO because the actual temperature lands in an adjacent bracket.

The 97-99c zone is excluded — only 47.8% NO rate there (dead zone).

## Configuration
| Parameter | Value | Notes |
|-----------|-------|-------|
| `BRACKET_NO_ENABLED` | Env var | Kill switch |
| `BRACKET_NO_YES_MIN` | 88c | Min YES price to trigger |
| `BRACKET_NO_YES_MAX` | 96c | Max YES price (97-99c excluded) |
| `BRACKET_NO_FIXED_CONTRACTS` | 5 | Fixed sizing — start small |
| `BRACKET_NO_ASSUMED_PROB` | 0.92 | Conservative (actual 91.7%) |
| `BRACKET_NO_MIN_STC` | 28800 (8h) | Data: 84.8% NO at 8-16h, 92.6% at 16h+ |
| `BRACKET_NO_MAX_CONCURRENT` | 6 | Max simultaneous positions |
| `BRACKET_NO_KILL_THRESHOLD` | -2000 (-$20) | Auto-disables at cumulative loss |

## Auto-Kill Circuit Breaker
At each scan, the bot checks cumulative bracket NO PnL from settled_trades. If below -$20, `BRACKET_NO_ENABLED` is set to False for the remainder of the session. This is a safety net for a strategy with limited live data.

## NO Ask Price Bug
Kalshi's `no_ask_cents` field was corrupted/unreliable for weather markets. Workaround: compute NO ask as `100 - YES_bid` to get the effective NO-side price. This is mathematically equivalent but avoids the corrupted field.

## Execution
NO-side orders use the same execution infrastructure as YES-side. The bot buys NO contracts at the computed NO ask price.

## Scaling Plan
Starting at 5 contracts. Intended to scale to 25 after verifying execution quality and settlement accuracy.

## Related
- [[concepts/weather-system.md]]
- [[concepts/stacking-infrastructure.md]]
