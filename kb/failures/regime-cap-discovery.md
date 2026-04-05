---
status: resolved
updated: 2026-04-02
tags: [failure, capital-allocator, regime, sizing]
severity: major
---
# Capital Allocator Regime Cap Discovery

## Summary
The capital allocator's regime cap was permanently stuck at GREEN with a static $400 cap, throttling all trades to approximately 38% of available balance. Discovered April 2026 during sizing analysis. The allocator was effectively dead code producing a constant output regardless of market conditions.

## Symptom
- Position sizes consistently smaller than expected given balance and Kelly fraction
- Capital allocator always returning same budget ceiling
- `get_budget_cents()` output capped at ~$400 regardless of balance growth
- No regime transitions observed in logs — always GREEN

## Root Cause
The capital allocator (`capital_allocator.py`) maintained a regime state (GREEN/YELLOW/RED) intended to scale position sizing based on recent performance. However:

1. **Regime transitions never triggered:** The thresholds for moving from GREEN to YELLOW/RED were set at levels that never occurred during normal operation
2. **$400 static cap:** The GREEN regime had a fixed dollar cap rather than a percentage-based cap, so as the balance grew from ~$660 to ~$1400+, the cap became an increasingly tight constraint
3. **No dynamic adjustment:** The cap was hardcoded at initialization and never updated

The result: every trade was sized as if the bot had ~$400 to work with, even when the actual balance was 2-3x that.

## Impact
- All trades sized at ~38% of what they should have been (at $1050 balance: $400/$1050)
- Lost compounding: smaller positions mean less PnL per winning trade
- The bot was still profitable, but significantly underperforming its potential

## Fix
See [[decisions/regime-cap-removal.md]] — the capital allocator regime cap was removed entirely. Per-asset risk caps (`MAX_RISK_PER_TRADE`, `BTC_MAX_RISK_PER_TRADE`, `XRP_MAX_RISK_PER_TRADE`) already provide sufficient position-level risk control.

## Lessons
1. **Audit sizing end-to-end periodically** — compare actual position sizes to what Kelly recommends
2. **Fixed dollar caps become stale** — always use percentage-based caps for a growing bankroll
3. **Dead code that returns constants is invisible** — the allocator "worked" (no errors) but added no value
4. **Multiple sizing layers can conflict** — Kelly, drawdown scaler, AND capital allocator all adjusted sizing, making it hard to trace which one was binding

## Related
- [[decisions/regime-cap-removal.md]]
- [[failures/hwm-bugs.md]]
