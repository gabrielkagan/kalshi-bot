---
status: resolved
updated: 2026-03-07
tags: [failure, dedup, crash, tuple]
severity: critical
---
# Dedup Set Tuple Size Crash

## Summary
Mixed tuple sizes in `_eval_opp_seen` dedup set caused a ValueError crash loop on every tick. YES-side code added 2-tuples `(ticker, stage)`, NO-side code added 3-tuples `(ticker, stage, side)`. Cleanup iteration used `for tk, stage in _eval_opp_seen` which cannot destructure 3-tuples. Commit abd47c8, Mar 7, 2026.

## Symptom
Bot crash loop: `ValueError: too many values to unpack` on every scan tick. No trades executing. Error appeared immediately after NO-side shadow code was merged.

## Root Cause
`_eval_opp_seen` is a set used to deduplicate evaluated opportunity inserts within a scan cycle. The cleanup code at the end of each cycle iterated the set to expire old entries:

```python
# Cleanup code expected 2-tuples
for tk, stage in _eval_opp_seen:
    ...
```

When NO-side shadow code was added, it inserted 3-tuples to distinguish YES vs NO evaluations of the same ticker:

```python
# NO-side added 3-tuples
_eval_opp_seen.add((ticker, stage, "no"))
```

Python destructuring `for tk, stage in ...` fails on any element that is not exactly a 2-tuple. Since the set contained a mix of 2-tuples and 3-tuples, the first 3-tuple encountered raised ValueError, crashing the entire scan loop.

## Why This Was Not Caught Before Deploy
1. **ast.parse() passed** -- the code is syntactically valid Python. Tuple size mismatches are runtime errors.
2. **Unit tests did not cover mixed-shape iteration** -- tests exercised YES-side and NO-side insertion separately, never a mixed set.
3. **NO-side was developed in isolation** -- the developer adding NO-side shadow did not grep for existing iteration patterns on `_eval_opp_seen`.

## Impact
- Total downtime: less than 1 hour (crash loop detected quickly via Telegram alerts)
- No financial loss (crash prevented all trades, not just bad ones)
- Revealed a class of bug: **collection shape drift** where different code paths add incompatible entry shapes to the same collection

## Fix
Safe iteration that handles variable tuple sizes. The cleanup code no longer assumes a fixed tuple shape -- it indexes by position (`entry[0]`, `entry[1]`) rather than destructuring.

## Timeline
- NO-side shadow code merged (abd47c8)
- Immediate crash loop on next tick
- Fix deployed same day (Mar 7)

## Lesson
**Any change to the shape of entries in ANY set or dict used as a dedup cache must grep ALL iteration and comprehension sites for that collection.** The add-site and the iteration-site may be in completely different parts of the codebase. The type system (Python's dynamic typing) provides no protection -- mixed shapes are silently accepted at insert time and only crash at iteration time.

This is a broader instance of the "two code paths, one data structure" problem. Other examples in this codebase:
- `_shadow_diag` dict: keys added in one place, splatted in another (see [[failures/shadow-callsite-variable.md]])
- CalEngine registry: engines registered in init, resolved in settlement -- shape must match

## Prevention
- Regression test: `TestDedupSetTupleSafety` verifies all tuple shapes in dedup sets are consistent
- CLAUDE.md mandatory pre-commit check #8: "Does this change the shape of entries in ANY set/dict used as a dedup cache?"
- grep pattern: `grep -n "_eval_opp_seen" bot.py` to find all add/iteration sites before any change

## Related
- [[failures/shadow-callsite-variable.md]] (same week, another silent failure from adding NO-side code)
- [[failures/database-contention.md]] (another class of multi-component integration bug)
- [[concepts/execution-layer.md]] (scan loop lifecycle, dedup mechanics)
