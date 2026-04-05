---
status: resolved
updated: 2026-03-30
tags: [failure, hwm, drawdown, recurring]
severity: critical
---
# HWM / Drawdown Scaler Bugs

## Summary
Five variants of high-water mark and drawdown scaler bugs. Most persistent bug family in the bot's history.

## Variant 1: Balance API Inflated First Reading
**Symptom:** First balance reading on restart anomalously high, inflating HWM.
**Root cause:** Kalshi API returns stale/inflated value on first call after connection.
**Fix:** 5-reading warmup + median initialization.

## Variant 2: Spike Rejection Guard Freeze
**Symptom:** After crash, balance history frozen at crash-time values.
**Root cause:** Spike rejection threshold too tight; new legitimate readings rejected.
**Fix:** Relaxed parameters + stale history detector that resets guard.

## Variant 3: Portfolio Value vs Cash-Only
**Symptom:** HWM calculated on portfolio value (cash + open exposure) instead of cash.
**Root cause:** Wrong balance field — total portfolio fluctuates with open positions.
**Fix:** Switched to cash-only balance tracking.

## Variants 4-5
Additional edge cases found during adversarial testing. Documented in CLAUDE.md defensive engineering section.

## Detection Pattern
All five share: **drawdown scaler in unexpected state relative to actual performance.** Canonical check: compare scaler value to manual calculation from known balance.

## Prevention
- `auditor.py` hourly HWM consistency check
- Adversarial test scenarios cover all five variants
- CLAUDE.md documents each as known failure mode

## Related
- [[failures/blr-calibrator.md]] (shared "silent failure" pattern)
