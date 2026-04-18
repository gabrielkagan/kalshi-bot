---
status: open
updated: 2026-04-18
tags: [dashboard, supabase, frontend, technical-debt, data-integrity]
severity: high
---
# Dashboard Drift — Messy, Stale, Partial

## Summary
The dashboard at https://gabekagan.io/dashboard/ is degraded in multiple independent ways:
1. **Data gaps** — live sync is missing ~412 trades and ~2,000 rejections behind local SQLite
2. **Silent schema drift** — `spx_harrv_shadow_signals` 400s every sync (missing `bankroll_cents` column in Supabase). Exact recurrence of the 2026-04-04 postmortem pattern, different table
3. **Snapshot bloat** — `dashboard_snapshot.py` has grown to 4,261 lines producing 155+ top-level keys; many are dead or duplicated
4. **No schema contract** — frontend has 30+ renderers, each reading untyped JSONB keys directly. Any backend rename silently kills a panel
5. **Frontend monolith** — 10,322-line single HTML file, 129 functions, jQuery-era pattern with no components, no types, no build step

## Timeline / Evidence
- **2026-04-18 ~10:54 UTC** reconciliation logs:
  - `rejected_opportunities local=34317 remote=32317 gap=2000`
  - `settled_trades local=2331 remote=1919 gap=412`
  - `5 days with PnL mismatch: ['2026-04-13', '2026-04-14', '2026-04-15', '2026-04-16', '2026-04-17']`
  - Re-synced 83/70/69/89/101 trades across Apr 13–17 (412 total — matches the gap)
- **2026-04-18 throughout day** recurring log:
  ```
  Supabase spx_harrv_shadow_signals: HTTP 400 —
  {"code":"PGRST204","message":"Could not find the 'bankroll_cents' column
   of 'spx_harrv_shadow_signals' in the schema cache"}
  ```

## Root causes

### 1. Schema drift with no startup check
Every new shadow table is `ALTER TABLE`'d in SQLite, but the corresponding Supabase column has to be added manually. When it's forgotten, PostgREST returns HTTP 400 on every sync and data is silently dropped. This is the *exact same failure class* as `kb/failures/supabase-sync-silent-failure.md` (2026-04-04) — we fixed the two worst offenders (evaluations, rejections) with explicit column lists but left `spx_harrv_shadow_signals` using `SELECT *`.

**Evidence:** `supabase_sync.py:479` — `mapped = [{col: self._clean(r[col]) for col in r.keys()} for r in rows]` — passes whatever columns exist in local SQLite, including `bankroll_cents` which Supabase doesn't know about.

### 2. Snapshot accretion without cleanup
155+ top-level keys, no lifecycle management. Dead features still emit keys (`dip_addon_shadow`, `sol_pathc_shadow`, `eth_filter_shadow`). Duplicate flavors for the same semantic: `win_count` vs `all_products_win_count`, `risk_metrics` vs `all_products_risk_metrics` vs `regime_risk_metrics`, `recent_trades` vs `all_products_recent_trades`. The frontend picks one; if it picks the wrong one, the dashboard silently shows partial data.

### 3. Ad-hoc shadow blocks
30+ shadow-specific top-level keys (`hourly_config_a..m`, `weekend_discount_shadow`, `decided_contract_shadow`, etc.), each a bespoke shape. Adding a new shadow = add a key to `dashboard_snapshot.py` + add a renderer to `dashboard/index.html` + hope nothing drifts. No shared schema for `{n, wins, losses, wr, wilson_lo, pnl_cents, status}`.

### 4. Frontend monolith
Single 10,322-line HTML file. No build step, no types, no component framework. Supabase anon key hardcoded twice. Every renderer does defensive optional-chaining because there's no contract. jQuery + vanilla JS + inline Chart.js.

### 5. Reconciliation masks the problem
Daily PnL reconciliation runs every 15 min and re-syncs mismatched day-totals — so the *top-line* PnL looks fine. But underlying rows behind the watermark aren't backfilled, so drill-down views (trades list, per-asset, per-strategy) lag multiple days.

## Impact
- **Multi-day lag in trade-level data on dashboard** — trades from Apr 13+ were not in Supabase until the 10:54 UTC reconciliation today (Apr 18)
- **Empty/broken `spx_harrv_shadow` panel** — silent 400 since the table schema was first created
- **Risk of hidden future drift** — any new shadow that goes live without adding matching Supabase columns will repeat this bug class
- **Dashboard feels unreliable** — users don't trust numbers they can't correlate, so dashboard becomes aesthetic noise

## Status (as of 2026-04-18 end-of-day)

Phase A progress:
- ✅ **A2 — Rowid watermark for trades** (`supabase_sync.py` commit pending push). Root cause: clock drift + string-timestamp watermark silently skipped rows. Full postmortem: [[failures/sync-watermark-clock-drift.md]].
- ✅ **A3 — Dead-key deletes** — effectively DONE after verification (zero safe deletes found; agent swarm false-positives documented in [[concepts/agent-audit-verification.md]]).
- ✅ **A4 — Ghost panel deletes** — DONE (no ghosts; backend loop at `dashboard_snapshot.py:1826` produces all `hourly_config_c..m`).
- ✅ **A8 — Schema parity validator** (`supabase_sync.py` commit pending push). OpenAPI-based column diff at startup. Full writeup: [[concepts/supabase-schema-parity.md]].
- ✅ **A9 — `?view=public` toggle** (`gabekagan/dashboard/index.html` commit pending push). CSS-hides `.shadow-panel`, `.research-panel`, `#orderCell`, `#calRegistryCell`, `[id^="exec"]`, `[id$="ShadowBody"]`. **Not actually private** — raw JSONB still flows to client. True privacy comes in Phase B4 via separate sanitized row.
- ⏳ **A1 — spx_harrv bankroll_cents** — migration SQL ready at `scripts/supabase_migration_008_harrv_bankroll.sql`. Blocked: Supabase DDL requires UI paste or DB password (service key alone is insufficient for PostgREST DDL).
- ⏳ **A5 — additional dead-key deletes** — deferred pending per-key verification via the protocol in [[concepts/agent-audit-verification.md]].
- ⏳ **A6 — duplicate collapse** (`all_products_*` / `regime_*`) — design work.
- ⏳ **A7 — modularize** — final step once shape is stable.

## Chosen path (decided 2026-04-18)

Six-agent swarm audit led to a two-phase incremental plan. Full details in [[decisions/dashboard-overhaul-plan.md]].

**Phase A (this week) — surgical fixes:**
1. Add `bankroll_cents` column to Supabase `spx_harrv_shadow_signals`; confirm 400s stop
2. Audit 412-trade sync gap (reconciliation already caught up; verify watermark health)
3. Delete 5 Chesterton-safe keys (`dip_addon_shadow`, `eth_filter_shadow`, `nig_distribution`, `ask_distribution`, `hourly_config_h/j/k`)
4. Delete 8 ghost frontend panels (`hourly_config_c..m` — frontend reads, backend never writes)
5. Delete additional DEAD-only backend keys (conservative intersection of both audit agents)
6. Collapse `all_products_*` / `regime_*` duplicate triplets
7. Modularize `dashboard_snapshot.py` into 5 files (byte-identical output)
8. Add JSON-schema runtime assertion in `_sync_dashboard()` — logs on shape regression to prevent future drift
9. Add `?view=public` CSS toggle on existing dashboard (not actually private — aesthetic only)

**Phase B (2–3 weeks later) — contract rebuild, parallel not replacement:**
1. Define `DashboardState v2`: ≤20 top-level sections, uniform `shadows[]` array, `schema_version` field
2. Backend dual-writes v1 + v2 to two Supabase rows
3. Build `dashboard/v2/index.html` — still single-file static HTML, no SPA — against the contract (~2k lines)
4. Dogfood 1 week; when proven, flip default; keep v1 at `/dashboard/legacy/`
5. Server-side-sanitized public row + RLS audit before exposing `?view=public` as actually-private

**Phase C (DEFERRED):** full TS SPA rejected for now. Revisit only if multiple consumers emerge or v2 HTML breaks under feature growth.

## Rejected approaches (with reasons)
- **Auto-generated `ALTER TABLE` on schema drift** — footgun; schema changes must be explicit commits, not side-effects of sync. Risks DB lock under settlement bursts (see PM-001).
- **Complete TS SPA rewrite now** — Netscape 2.0 / Joel Spolsky trap. Breaks single-file deploy ethos. YAGNI for one operator.
- **Two fully separate dashboards** — doubles maintenance for one user; history says the lower-traffic one will decay silently within 6 months.

## Operational
- [[failures/supabase-sync-silent-failure.md]] needs updating — the April fix was incomplete. `spx_harrv` was left using `SELECT *`, which caused this recurrence. Update recommends "every new Supabase-synced table gets explicit column list AND a startup schema-diff check."

## Related
- [[concepts/dashboard-architecture.md]]
- [[failures/supabase-sync-silent-failure.md]]
- [[failures/pnl-reporting-bugs.md]]
