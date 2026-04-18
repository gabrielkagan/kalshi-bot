---
status: fixed
updated: 2026-04-18
tags: [supabase, sync, watermark, clock-drift, silent-failure]
severity: medium
---
# Supabase Trades Sync — Clock-Drift Watermark Gap

## Summary
`supabase_sync._sync_trades()` used `WHERE settled_at > ?` to fetch rows newer than the last watermark. The watermark `self._wm_trades_last_settled` was an ISO string timestamp sourced from the bot's local clock. When the VPS clock drifted forward then corrected (observed: `clock_drift_detected: 13.6s vs server` in live logs), *newer* rows could land with *earlier* `settled_at` than the watermark — the `>` comparison silently skipped them. They never re-synced until the daily-PnL reconciliation RPC caught the gap ~15 minutes to multiple days later.

## Symptoms
- `settled_trades local=2331 remote=1919 gap=412` (2026-04-18 10:54 UTC)
- `Supabase reconciliation: 5 days with PnL mismatch: ['2026-04-13', '2026-04-14', '2026-04-15', '2026-04-16', '2026-04-17']`
- Reconciliation re-synced 83/70/69/89/101 trades (412 total) across those 5 days
- Dashboard trade-level drill-down was multi-day-stale the entire time, even though top-line daily PnL looked correct (reconciliation fixed day-totals without backfilling the individual rows until forced)

## Root cause
Three compounding issues in the watermark:

1. **String timestamp watermark** — sortable but not monotonic under clock adjustment
2. **No persistence of `_wm_trades_last_settled`** across bot restarts — set only in-memory; after every restart, the condition branched to "full sync" (line 391) which sent ALL rows at once; worked for small counts but fragile at scale
3. **Strict `>` comparison** — equal timestamps on simultaneously-settled rows could also drop rows if interleaved with failed posts

The belt-and-suspenders reconciliation masked the visibility of the bug; without that catch, we'd have lost trade-level data permanently.

Compare to `_sync_rejections()` / `_sync_evaluations()` which both use `rowid > ?` / `id > ?` — monotonic by SQLite insertion order, drift-immune, unaffected by timestamp equality.

## Fix (2026-04-18)
Rewrote `_sync_trades()` to use `rowid > ?` watermark matching the rejections pattern:

```sql
SELECT rowid, ... FROM settled_trades WHERE rowid > ? ORDER BY rowid LIMIT 500
```

- Renamed instance field `_wm_trades_count` → `_wm_trades_rowid` for semantic clarity (value stored in `sync_watermarks.last_synced_id` unchanged)
- Deleted `_wm_trades_last_settled` — no longer needed
- Removed the "full sync on None" branch — incremental works from the first cycle regardless
- Added `LIMIT 500` matching the rejections/evaluations pattern — prevents megabatch on post-restart catchup
- On first run post-deploy: rowid watermark is loaded from `sync_watermarks` (was count-based, coincidentally matches `max(rowid)` when there are no deletes in `settled_trades`)

## Lesson — patterns to preserve
- **Sync watermarks must be monotonic.** String timestamps are not monotonic under clock adjustment. Use stable rowid or a monotonic `id SERIAL`.
- **Belt-and-suspenders fixes are a honeypot.** Reconciliation hid this bug for weeks because day-totals looked fine. Add a structured log when a reconciler has to *fix* a gap — that line is the smoke alarm for the underlying bug.
- **Watermark semantics must be consistent with what's persisted.** Our `_wm_trades_count` (in memory, count of rows) and `sync_watermarks.last_synced_id` (row count) shared a name but the in-memory `_wm_trades_last_settled` was never persisted, creating hidden restart behavior.

## Related
- [[failures/supabase-sync-silent-failure.md]] — 2026-04-04, `SELECT *` schema drift (same family)
- [[failures/dashboard-drift.md]] — 2026-04-18 comprehensive dashboard audit that surfaced this
- [[decisions/dashboard-overhaul-plan.md]] — A2 deliverable in Phase A
