---
status: active
updated: 2026-04-03
tags: [methodology, analysis, checklist]
---
# Data Analysis Rules

## Summary
A mandatory 10-point checklist for all data analysis performed on the bot's databases. Established after multiple trust-destroying analysis errors in Feb-Mar 2026 where wrong assumptions about column values, schema shapes, and data distributions led to incorrect recommendations. Every analysis must follow this protocol — no exceptions.

## The 10-Point Checklist

### 1. Schema First
Before ANY SQL query, run `PRAGMA table_info(table)` and `SELECT DISTINCT column FROM table LIMIT 10` to verify column names and actual values. Never assume column values (e.g., `settled_trades.market_result` is 'yes'/'no', NOT 'win'/'loss').

### 2. Verify filter_stage vs rejection_reason
These columns have different semantics across tables:
- `evaluated_opportunities.filter_stage`: short labels ('candidate', 'observation_trade', 'shadow', 'edge_too_low')
- `rejected_opportunities.rejection_reason`: full descriptive strings (NOT short labels)
Always check actual values with `SELECT DISTINCT` before building queries.

### 3. Check What Was Actually Traded
`filter_stage = 'candidate'` means it passed all filters and was sent for execution. `zero_sizing` means drawdown killed it. Always verify filter_stage when claiming trades would be "added" or "skipped".

### 4. Statistical Significance Before Recommendations
Never recommend a config change based on a subsample without computing p-values or Wilson confidence intervals. State sample size and significance explicitly. If p > 0.10, say "not significant, could be noise."

### 5. One Careful Query, Not Four Rushed Ones
Run schema checks first, then ONE comprehensive query. Do not present preliminary results and iterate — each wrong iteration destroys trust. (Origin: Feb 28 session where three successive wrong analyses compounded into a completely incorrect recommendation.)

### 6. Flag Confidence Level
Every number gets a tag:
- **[VERIFIED]**: Checked against raw data
- **[ESTIMATED]**: Derived with assumptions stated
- **[ROUGH]**: Back-of-envelope
Never present [ROUGH] numbers as if they are [VERIFIED].

### 7. No Compounding Errors
If an earlier number was wrong, do NOT build further analysis on top of it. Go back to raw data and start over.

### 8. Compute Before Hardcoding
Never hardcode derived values (breakeven WR, fee thresholds) by mental math. Write a Python computation, run it, verify the output, THEN hardcode. (Origin: breakeven WR formula bug Mar 5 — used `p/(100-fee)` instead of `(p+fee)/100`, shipped wrong values to dashboard.)

### 9. Match the System You Are Analyzing
The bot uses price-dependent edge thresholds (`MIN_EDGE_BY_PRICE`), not flat minimums. Any edge analysis must be per-price-tier. Flat sweeps like "raise edge to 0.9%" are misleading when the bot has 6 different thresholds. Always check how the actual config works before designing analysis.

### 10. Verify Assumptions Against Real Data Before Writing Code
Never write detection/analysis logic based on assumptions about data shape. Always query actual data first. (Origin: regime detection assumed >4h gap in settled trades = config restart. Reality: every overnight has a 4-11h gap — detection fired daily, producing 0.6-day window instead of true 2.3-day regime.)

## Key Schema Gotchas
| Table | Column | Actual Values | Common Mistake |
|-------|--------|--------------|----------------|
| settled_trades | market_result | 'yes' / 'no' | Assuming 'win' / 'loss' |
| settled_trades | entry_price_cents | integer cents | Using market_price (that is on evaluated_opportunities) |
| evaluated_opportunities | filter_stage | short labels | Confusing with rejection_reason |
| rejected_opportunities | rejection_reason | full descriptive strings | Using short labels |
| both | product_type | '15m', 'hourly', 'spx_hourly', 'weather', 'sports' | Assuming NULL or other values |

## When These Rules Apply
- Any time Claude Code runs SQL against state.db
- Any time performance numbers are presented to the user
- Any time a config change is being evaluated with data
- Any time counterfactual or simulation analysis is performed

## Related
- [[concepts/edge-thresholds.md]]
- [[concepts/guard-aware-protocol.md]]
