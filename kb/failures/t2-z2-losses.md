---
status: resolved
updated: 2026-03-31
tags: [failure, dc, t2-z2, loss]
severity: critical
---
# T2_Z2 Losses

## Summary
Two losses on DC T2_Z2 (~$310 total) led to T2_Z2 being shadowed.

## Loss 1: Retry Bug
**Root cause:** Pre-existing retry bug in execution layer. Not a strategy failure.

## Loss 2: Pre-Tiered-Risk SOL Position
**Root cause:** SOL position taken before tiered risk caps existed. Would have been sized smaller or rejected under current caps.

## Decision
Conservatively shadowed despite both losses having non-strategy explanations:
- 2% zone has less margin of safety
- Two losses on small sample is concerning regardless of cause
- Cost of shadowing < cost of another loss
- Can re-promote after collecting shadow data

## Status
Running as shadow, collecting observations for potential re-promotion.

## Related
- [[concepts/dc-strategy.md]]
- [[concepts/sol-dynamics.md]]
