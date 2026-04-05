---
status: resolved
updated: 2026-03-07
tags: [failure, shadow, nameerror, silent]
severity: major
---
# Shadow Engine Dead Code (NameError Swallowed)

## Summary
Shadow engine calls used bare variable names that only existed as keys in the `_shadow_diag` dict, not as local variables. `ast.parse()` cannot catch NameError (it is a runtime error). The exception was silently swallowed by `except Exception: logging.debug(...)`. Shadow engine was dead code for weeks until discovered in commit dffdb05, Mar 7, 2026.

## Symptom
Zero rows in `fifteenm_shadow_signals` table for weeks. No errors in logs (debug-level logging was not visible at default log level). Shadow engine appeared to be running but producing nothing.

## Root Cause
The shadow engine call site passed keyword arguments using bare variable names:

```python
try:
    shadow_engine.evaluate(
        egarch_blend_weight=egarch_blend_weight,  # NameError: not a local var
        ...
    )
except Exception:
    logging.debug("shadow eval failed")  # silently swallowed
```

`egarch_blend_weight` existed as a key in the `_shadow_diag` dictionary (`_shadow_diag["egarch_blend_weight"]`), but was never assigned as a standalone local variable. Every call raised NameError on the first such argument, jumped to the except block, logged at debug level (invisible in production), and continued. The shadow engine never executed a single evaluation.

## Why ast.parse() Missed It
`ast.parse()` only checks syntax -- it verifies the code is grammatically valid Python. NameError is a runtime error that occurs when the interpreter cannot find a variable binding. The code `egarch_blend_weight=egarch_blend_weight` is syntactically valid -- ast.parse has no way to know whether `egarch_blend_weight` will be bound at runtime.

## The Anti-Pattern
```python
except Exception:
    logging.debug(...)
```

This is the most dangerous pattern in the codebase. It:
1. Catches ALL exceptions including NameError, TypeError, AttributeError
2. Logs at debug level which is invisible in production
3. Continues execution silently as if nothing happened
4. Makes the failure completely invisible -- no alerts, no errors, no symptoms

This same anti-pattern caused the `check_same_thread=False` bug in fifteenm_shadow.py (Mar 6) -- two separate bugs from the exact same exception-swallowing pattern in one week.

## Impact
- Shadow engine A1/A2/A3 produced zero data for weeks
- No shadow promotion decisions could be made during that period
- Data collection delay pushed back shadow evaluation timeline
- No financial loss (shadow-only code), but significant opportunity cost in delayed research

## Fix
1. Changed call site to use `_shadow_diag["egarch_blend_weight"]` (dict access instead of bare variable)
2. Changed `except Exception: logging.debug(...)` to `logging.warning(..., exc_info=True)` for all shadow engine calls and DB operations
3. Audited all other `except Exception` blocks in shadow code paths for the same anti-pattern

## Broader Lesson: The check_same_thread Twin
The exact same `except Exception: logging.debug(...)` pattern caused a separate bug in fifteenm_shadow.py one day earlier (Mar 6, commit 53c953b). That bug: `sqlite3.connect()` without `check_same_thread=False` raised `ProgrammingError` when called from supabase_sync thread, silently swallowed by the debug handler, zero rows written. Two distinct bugs from the same anti-pattern in 48 hours.

## Prevention
- Regression test: `TestShadowCallsiteVariables` verifies all variables passed to shadow calls are bound as locals
- Regression test: `TestShadowCallsiteVariables.test_no_logging_debug_in_shadow_except` scans for the anti-pattern
- CLAUDE.md rule: "After ANY function signature change, grep ALL call sites and verify every caller passes the new parameter"
- CLAUDE.md rule: "Never add keys to `_shadow_diag` without also adding them to insert signatures + SQL"

## Related
- [[failures/dedup-tuple-crash.md]] (same week, another NO-side integration bug)
- [[failures/blr-calibrator.md]] (shared "silent failure" pattern -- broken output undetected)
- [[concepts/fifteenm-shadow-variants.md]] (shadow engine architecture)
