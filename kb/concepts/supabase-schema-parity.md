---
status: current
updated: 2026-04-18
tags: [supabase, sync, schema, validation, drift-prevention]
---
# Supabase Schema Parity Validator

## Purpose
Detect and loudly log every case where the bot's local SQLite schema has a column that the corresponding Supabase table doesn't — before that column causes silent HTTP 400s on every sync attempt.

This is the structural fix for the recurring "schema drift" bug class documented in `kb/failures/supabase-sync-silent-failure.md` (2026-04-04, evaluations/rejections) and `kb/failures/dashboard-drift.md` (2026-04-18, spx_harrv_shadow_signals). Previous fixes were key-by-key; this catches future drift automatically.

## How it works
Added to `SupabaseSyncer.start()` — runs once on bot startup, after watermark load, before the sync thread starts.

1. **One GET to PostgREST's OpenAPI endpoint** (`GET /rest/v1/`) — returns a JSON doc with `definitions.<table>.properties.<col>` for every remotely-exposed table. This is the stable, cheap schema-introspection path — no DB credentials needed, just the service key we already have.
2. **For each synced table**, pull local columns via `PRAGMA table_info(<table>)`.
3. **Diff**: columns in local (or in the explicit sync column list for evaluations/rejections) minus columns in remote.
4. **Log each missing column** with a ready-to-paste `ALTER TABLE` suggestion.

Tables checked:
- `evaluated_opportunities` → `evaluations` (against explicit `_EVAL_COLUMNS` list)
- `rejected_opportunities` → `rejections` (against explicit `_REJ_COLUMNS` list)
- `settled_trades` → `trades` (all local columns — no explicit list)
- `spx_harrv_shadow_signals` → `spx_harrv_shadow_signals` (all local columns — `SELECT *` sync)

## Output example (startup log)
If `bankroll_cents` exists locally but not remotely (the 2026-04-18 case):
```
[WARNING] Supabase schema parity: spx_harrv_shadow_signals -> spx_harrv_shadow_signals missing 1 column(s): ['bankroll_cents']
[WARNING]   SUGGEST: ALTER TABLE spx_harrv_shadow_signals ADD COLUMN IF NOT EXISTS bankroll_cents <TYPE>;  -- then NOTIFY pgrst, 'reload schema';
[WARNING] Supabase schema parity: 1 total column(s) missing remotely — inserts will silently 400
```

When clean:
```
[INFO] Supabase schema parity: OK (all synced columns present remotely)
```

## What the validator does NOT do
- **Does not auto-ALTER.** Schema changes must be explicit, reviewed, and applied in Supabase UI or via DB password. Auto-DDL risks DB lock under load (see PM-001, `wal_checkpoint(TRUNCATE)` deadlock family).
- **Does not block sync startup.** Logs WARNING; sync proceeds. PostgREST 400s still happen, but now you see the root cause at startup.
- **Does not check type compatibility** — only column presence. Type mismatch (e.g., INTEGER vs BIGINT) could still 400 at insert time.
- **Does not check reverse drift** (remote columns missing locally). That's benign — PostgREST ignores extra remote columns on insert.

## Why OpenAPI, not a custom RPC
- No DDL access needed from the bot side
- Single HTTP GET, cheap, idempotent
- Stable PostgREST contract — documented, not internal
- We already use `requests` and have the service key

Alternatives considered:
- `information_schema.columns` via an `exec_sql` RPC — requires defining a Postgres function with DDL execution, security risk
- Direct Postgres connection via pooler with service key — **does not work** (pooler requires actual DB password, not JWT)
- Scraping pg_meta — internal, undocumented, fragile

## Operational
- Runs once per `SupabaseSyncer.start()` — i.e., every bot restart
- Fails silent (logs WARNING, continues) if OpenAPI fetch itself fails
- Should be surfaced to Telegram via auditor.py if any drift is detected — TODO for a future commit

## Related
- [[failures/supabase-sync-silent-failure.md]] — the precedent that motivated explicit column lists (2026-04-04)
- [[failures/dashboard-drift.md]] — recurrence with spx_harrv (2026-04-18)
- [[concepts/dashboard-architecture.md]] — where this fits in the sync pipeline
- [[decisions/dashboard-overhaul-plan.md]] — A8 deliverable in Phase A
