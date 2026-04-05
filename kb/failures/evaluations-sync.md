---
status: resolved
updated: 2026-04-01
tags: [failure, supabase, sync, schema]
severity: major
---
# 28-Day Supabase Evaluations Sync Failure

## Summary
For approximately 28 days, the `supabase_sync.py` incremental sync for evaluations silently failed due to schema mismatch between SQLite and Supabase tables. Evaluations, rejections, and other incremental data were not reaching the Supabase dashboard backend.

## Symptom
- Dashboard showing stale evaluation data
- Supabase `evaluations` table not receiving new rows
- No errors visible in bot logs (failures silently caught)
- Watermark IDs not advancing

## Root Cause
Three compounding issues:

1. **32 missing columns:** The Supabase `evaluations` table schema lagged behind the SQLite `evaluated_opportunities` table. Over time, 32 new columns were added to SQLite (shadow diagnostics, strategy fields, etc.) that were never added to the Supabase table. PostgREST rejects inserts with unknown columns.

2. **FK constraint:** Supabase had a foreign key constraint on the evaluations table that SQLite doesn't enforce. Some evaluation rows referenced entities that didn't exist in the Supabase parent table.

3. **NaN/Inf values:** Some float columns contained `NaN` or `Inf` values from edge cases in the volatility engine. PostgreSQL rejects these — SQLite stores them silently.

## Fix
1. Added all 32 missing columns to the Supabase evaluations table
2. Removed or relaxed the FK constraint
3. Added NaN/Inf sanitization in the sync layer before UPSERT
4. Backfilled missing evaluation data from SQLite

## Timeline
- **~Early March 2026:** Sync starts failing silently
- **~Late March 2026:** Failure discovered during dashboard investigation
- **Fix deployed:** Schema migration + sanitization

## Lessons
1. **Schema drift between SQLite and Supabase is inevitable** — every bot.py column addition must also update Supabase schema
2. **Silent failure in sync layers is dangerous** — the bot appeared healthy because trading was unaffected
3. **PostgREST is strict** — unlike SQLite, it rejects unknown columns, NaN, Inf, FK violations
4. **Monitoring watermark advancement** would have caught this immediately — if `_wm_evaluations` never increases, sync is broken
5. **Sanitize floats before external sync** — always check for NaN/Inf

## Related
- See `kb-research/infrastructure/firebase-supabase-migration.md` for the migration playbook that preceded this failure
