---
status: resolved
updated: 2026-03-29
tags: [failure, blr, calibration]
severity: critical
---
# BLR Calibrator Failure

## Summary
BLR silently failed, outputting ~95% probability regardless of input. Largest source of lost alpha, undetected for weeks.

## Timeline
- **Pre-March 25:** BLR active as calibration layer on EGARCH
- **March 25:** Failure discovered via output distribution analysis
- **March 25:** Removed, passthrough deployed
- **Post-March 25:** $660 → $1,400 over five weeks, 92%+ WR

## Why Undetected
1. Crypto 15M settles YES at high rates — "always 95%" still profitable
2. No calibrator output distribution monitoring
3. Downstream metrics (WR, PnL) positive enough to not trigger investigation
4. Gradual failure — weights collapsed over time

## Root Cause
BLR weights converged to degenerate state. Training data and update mechanism failed to maintain discrimination.

## Lessons
1. **Monitor intermediate outputs**, not just final results
2. **Silent failures are most dangerous** — bot was "working" by every visible metric
3. **Simpler systems fail more visibly** — passthrough has no hidden state to collapse

## Post-Mortem Actions
- BLR bypass shadowed then deployed live
- Calibrator output distribution monitoring added
- IOC sub-floor bug (dormant post-BLR) has alert in place

## Related
- [[failures/hwm-bugs.md]] (shared "silent failure" pattern)
- See `kb-research/bot/ml-probability-improvements.md` for complete ML investigation
