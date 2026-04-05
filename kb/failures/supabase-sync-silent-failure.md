---
status: resolved
updated: 2026-04-04
tags: [supabase, sync, silent-failure, data-loss]
severity: critical
---
# Supabase Sync Silent Data Loss

## Summary
`supabase_sync.py` used `SELECT *` to sync `evaluated_opportunities` and `rejected_opportunities` to Supabase. As new columns were added to SQLite via ALTER TABLE (weather fields, hourly temperature shadows, order tracking, NO-side columns — ~30 extra columns), the payloads included columns that didn't exist in the Supabase `evaluations` and `rejections` tables. PostgREST returned HTTP 400 on every sync attempt. The watermark never advanced. All new evaluations and rejections were silently lost from Supabase.

## Timeline
- **Unknown date (likely Feb-Mar 2026)**: First extra columns added to SQLite `evaluated_opportunities` (OFT fields, weather fields, hourly temperature shadows). Supabase sync starts silently failing.
- **2026-04-04**: Discovered during deep dashboard audit. The `_post()` method's `except Exception` block incremented `_consecutive_errors` but did NOT log the exception — failures were completely invisible even at WARNING level.
- **2026-04-04**: Fixed by replacing `SELECT *` with explicit column lists matching the Supabase schema (47 columns for evaluations, 25 for rejections).

## Root Cause
1. `SELECT *` is fragile — any ALTER TABLE on the source breaks the sync without any code change.
2. The `_post()` error handler swallowed exceptions silently (no logging).
3. The trades sync had a separate but related issue: full-table re-send every 30 seconds (no incremental watermark).

## Impact
- Weeks of evaluation and rejection data missing from Supabase
- Materialized views (calibration accuracy, counterfactual analysis, edge accuracy) were stale
- Dashboard analytics panels showing old data with no indication of staleness

## Fix
- `_sync_evaluations()`: `SELECT *` → explicit 47-column list (`_EVAL_COLUMNS`)
- `_sync_rejections()`: `SELECT rowid, *` → explicit 25-column list (`_REJ_COLUMNS`)
- `_sync_trades()`: Added incremental watermark on `settled_at`
- `_post()`: Added `logging.warning` with `exc_info=True` on all failures
- Dashboard/trades sync: `logging.debug` → `logging.warning`

## Lessons
1. **Never use `SELECT *` across system boundaries.** When two systems (SQLite + Supabase) share a table schema, use explicit column lists. `SELECT *` creates an invisible coupling that breaks silently when either side changes.
2. **Error handlers must log.** `except Exception: self._consecutive_errors += 1` with no log message made weeks of data loss invisible. Every except block in sync/pipeline code must use `logging.warning` with `exc_info=True`.
3. **Sync watermarks must advance only on success.** The evaluations sync correctly gated watermark advancement on `_post()` returning True. But the dashboard sync and trades sync had no such gating.

## Related
- [[failures/database-contention.md]] — SQLite busy_timeout issues from concurrent access
- [[concepts/balance-tracking.md]] — Dashboard data flow architecture
