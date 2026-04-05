---
status: resolved
updated: 2026-03-06
tags: [research, infrastructure, migration]
---
# Firebase to Supabase Migration — Complete Record

Source: Mar 2026
Chat link: https://claude.ai/chat/30bec92a-f790-454a-8a5e-eb9e3f88948b

---

## Decision
Consolidate from Firebase + Supabase to Supabase-only. Both were being used simultaneously — Firebase for dashboard real-time data, Supabase for structured trade/evaluation storage. Supabase can do everything Firebase does for this use case.

## Why It Was Dangerous
- Dashboard at gabekagan.io read from Firebase
- Real-time data pipeline from VPS wrote to Firebase
- Historical data stored in Firebase could be lost
- Latency/reliability differences between Firebase and Supabase real-time

## Migration Architecture: 7-Agent Sequential Plan

### Agent 1: Audit Current State
Complete inventory of both systems before any changes:
- Every Firebase collection/document/path, data structure, field names/types
- Every Supabase table/view/function
- What writes to each (VPS bot? Dashboard? Scripts? Manual?)
- What reads from each (Dashboard? Scripts? Alerts?)
- Write frequency (real-time push? Batch? On settlement?)
- Firebase-specific features in use (real-time listeners, auth, cloud functions, hosting, storage)
- Overlap analysis: what's in both, what's only in Firebase, what's only in Supabase
- Dependency map: complete data flow VPS → [Firebase/Supabase] → Dashboard

### Agent 2: Design Supabase Target Schema
- Accommodate everything currently in Firebase
- Extend existing Supabase tables rather than creating duplicates
- Real-time requirements: if dashboard uses Firebase real-time listeners, Supabase real-time subscriptions must replicate
- Query patterns: Postgres may need indexes for queries Firebase handled differently
- Data types: Firebase schemaless JSON → Postgres typed columns
- Historical data partitioning/retention

### Agent 3: Implement Dual-Write
- VPS writes to BOTH Firebase AND Supabase simultaneously
- Firebase remains PRIMARY — if Supabase write fails, must not affect Firebase or bot
- Log Supabase write failures separately
- **TEST:** Deploy, verify both receive identical data for 24+ hours
- **TEST:** Compare data in both systems — any discrepancies?
- **TEST:** Simulate Supabase write failure — verify Firebase unaffected
- **TEST:** Verify live trading completely unaffected (latency, resources)

### Agent 4: Migrate Historical Data
- Export all historical data from Firebase
- Transform to Supabase target schema
- Import to Supabase
- **TEST:** Row counts match, spot-check records, timestamps/types survived

### Agent 5: Switch Dashboard to Supabase
- Incrementally, one section at a time
- If Firebase used real-time listeners → implement Supabase real-time subscriptions
- **TEST:** Each section shows same data as before, update frequency matches

### Agent 6: Switch Scripts & Tooling
- Every script reading/writing Firebase → update to Supabase
- Verify no code references Firebase after migration

### Agent 7: Decommission Firebase (LAST, after all verified)
Pre-decommission checklist (ALL must pass):
- [ ] Dual-write running 48+ hours successfully
- [ ] Dashboard fully on Supabase with no Firebase fallbacks
- [ ] All scripts/tooling on Supabase
- [ ] Historical data verified in Supabase
- [ ] Firebase export backup stored safely
- [ ] No code references Firebase (grep entire codebase)

Decommission sequence:
1. Stop dual-writing to Firebase
2. Verify dashboard/scripts still work
3. Wait 48 hours, monitor
4. Remove Firebase SDK, config, dependencies
5. Keep Firebase project as cold backup 30 days, then delete

## Migration Sequence (Non-Negotiable Order)
```
Audit → Schema Design → Dual-Write (24hr+) → Historical Data → Switch Dashboard (24hr+) → Switch Scripts → Decommission (48hr wait) → Cleanup
```

Each step verified before next begins. Dual-write phase is the safety net.

## Supabase Configuration Decisions
- **Region:** East US (North Virginia, us-east-1) — closest to Kalshi (NYC) and DigitalOcean VPS
- **Connection:** Pooling URL (Transaction mode, port 6543) for bot's async nature
- **RLS:** Off — single trusted service account, not multi-user
- **bot_version column:** On trades table for cross-version performance comparison

## Outcome
Firebase removed March 6, 2026. Supabase sole data layer.
- Project ID: srbdajecmkjxinmcozxl
- Key tables: trades, evaluations, dashboard_state (JSONB data column), spx_harrv_shadow_signals
- 28-day evaluations sync failure discovered post-migration: 32 missing columns, FK constraint, NaN/Inf in kelly_f — all fixed

## Related (KB operational articles)
- [[kb/failures/evaluations-sync.md]]
