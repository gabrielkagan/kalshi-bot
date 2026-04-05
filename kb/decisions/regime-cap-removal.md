---
status: decided
updated: 2026-04-02
tags: [decision, capital-allocator, regime, sizing]
date: 2026-04-02
---
# Decision: Remove Capital Allocator Regime Cap

Date: 2026-04-02
Status: Decided

## Context
The capital allocator's regime cap was discovered to be permanently GREEN with a static $400 cap (see [[failures/regime-cap-discovery.md]]). This throttled all trades to ~38% of available balance as the bankroll grew. The allocator was effectively dead code — it consumed CPU cycles and added a code path but always returned the same constant ceiling.

## Options Considered
1. **Fix the allocator** — Implement dynamic regime transitions, percentage-based caps
   - Pro: Adaptive risk management based on recent performance
   - Con: Complex, another layer that can silently fail (like BLR did)
   - Con: Already have drawdown scaler + per-asset risk caps serving this function
2. **Remove the regime cap entirely** — Let Kelly + drawdown scaler + per-asset caps control sizing
   - Pro: Removes dead code and a layer of complexity
   - Pro: Existing controls (MAX_RISK_PER_TRADE=25%, BTC/XRP at 12%) already cap per-trade risk
   - Con: Loses a theoretical safety net (but it wasn't actually providing one)
3. **Convert to percentage-based cap** — Replace $400 with e.g., 50% of balance
   - Pro: Scales with balance
   - Con: Still redundant with existing risk controls

## Decision
Remove the capital allocator regime cap. Per-asset risk caps and the drawdown scaler provide sufficient position-level and portfolio-level risk control.

The `capital_allocator.py` module remains in the codebase but `get_budget_cents()` no longer applies the regime cap ceiling. The allocator's observation-mode strategy gating (preventing observation-only strategies from trading) is still active.

## Consequences
- **Immediate:** Position sizes increase to their Kelly-optimal levels (constrained by per-asset risk caps)
- **Expected PnL impact:** Positive — winning trades now capture full edge instead of being throttled
- **Risk:** Larger positions mean larger losses when trades lose — but this is the Kelly-optimal tradeoff
- **Monitoring:** Track position sizes post-change to verify they're reasonable

## Related
- [[failures/regime-cap-discovery.md]]
- [[failures/hwm-bugs.md]]
