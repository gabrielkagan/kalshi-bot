---
status: decided
decided: 2026-04-18
tags: [dashboard, supabase, frontend, architecture, overhaul]
---
# Dashboard Overhaul Plan — Option A → B, defer C

## Context
On 2026-04-18 the operator flagged the dashboard as "messy, doesn't show the right data, not super useful." A six-agent swarm audit found:
- `dashboard_snapshot.py` produces 216 `snap[...]` assignments (155+ top-level keys); frontend consumes only 101
- **20 DEAD (backend-only)** keys — produced, never read (~2.4–3.6 MB/hour wasted)
- **8 GHOST (frontend-only)** keys — `hourly_config_c..m` rendered but never produced → silent blank panels
- **3 duplicate families** — `win_count` / `risk_metrics` / `recent_trades` each have `base + all_products_* + regime_*` triplets
- `spx_harrv_shadow_signals` HTTP 400 every sync (missing `bankroll_cents` column in Supabase — same failure class as 2026-04-04 supabase-sync-silent-failure postmortem)
- Trades sync lagged ~412 rows (5 days) until reconciliation caught up
- Frontend: 10,322-line single-file HTML on `gh-pages` branch of `gabrielkagan/gabekagan`, served at https://gabekagan.io/dashboard/
- 91 renderer functions, ~10 dead-candidate panels, jQuery-era pattern with no types, no build step

## Options evaluated

### A — Surgical (chosen, ship first)
1 week of bounded, reversible changes:
- Fix `spx_harrv` 400 by adding `bankroll_cents` to Supabase schema
- Audit/confirm 412-trade sync gap is fully recovered
- Delete the 5 Chesterton-safe dead keys (`dip_addon_shadow`, `eth_filter_shadow`, `nig_distribution`, `ask_distribution`, `hourly_config_h/j/k`)
- Delete the 8 ghost frontend panels (`hourly_config_c..m`)
- Delete the additional 10–15 agent-identified DEAD backend keys (conservative; only ones both agents agreed on)
- Collapse `all_products_*` / `regime_*` duplicates into one shape
- Modularize `dashboard_snapshot.py` into 5 files (output byte-identical)
- Add a JSON-schema runtime assertion in `supabase_sync._sync_dashboard()` that logs on shape regression
- Add `?view=public` CSS toggle on existing dashboard (aesthetic only — real sanitization comes in B)

**Captures ~80% of the pain with zero deploy risk.**

### B — Phased contract (chosen, ship after A)
2–3 weeks, parallel-not-replacement:
- Define `DashboardState v2` contract: ≤20 top-level sections, uniform `shadows[]` array with `{id, status, n, wins, wr, wilson_lo, pnl_cents, stage, last_updated}`, `schema_version: 2`
- Dual-write: backend emits v1 (current blob) AND v2 (contract-compliant) to two Supabase rows
- Build `dashboard/v2/index.html` — **still single-file static HTML, no SPA** — against the contract; ~2k lines expected
- Dogfood for 1 week. When proven, flip default at `/dashboard/`; keep v1 at `/dashboard/legacy/`
- Public view = `?view=public` reading from a separately-written sanitized Supabase row (server-side strip of positions, balance, shadow signals). Requires RLS audit first.
- Retire v1 after 2 weeks clean.

**This is the Netscape-safe rewrite: parallel, reversible, no build step added.**

### C — Full SPA (DEFERRED, revisit only if specific conditions met)
TypeScript + Vite + React/Svelte + component library + GitHub Actions build. Rejected for now because:
- Only 1 operator, 0 external investors today (YAGNI)
- Breaks the one-file-deploy guarantee that mirrors `bot.py is sacred`
- Supply-chain surface, lockfile rot, build pipeline risk, React state-sync with Realtime is famously bug-prone
- Both skeptical agents flagged it as highest-regret path
- **Revisit only if** (a) multiple dashboard consumers emerge, (b) v2 contract-based HTML genuinely breaks under feature growth, or (c) the operator acquires sustained TS/React bandwidth

## Why incremental beats rewrite
- `kb/concepts/dashboard-architecture.md` captures current surface — accumulates 14 months of quiet patches that a rewrite would re-discover
- Critical Rule parallel: "Don't refactor bot.py into multiple files" applies equally to the single HTML
- Two-dashboard splits double maintenance for one user
- A rewrite-during-live-trading window is a blind-operator window

## Failure modes to avoid during A execution
- Do not break reconciliation during sync-gap audit
- Do not delete any key without confirming no active writer (bot.py grep + Supabase RPC check)
- Do not change output JSON shape during modularization (byte-compare before/after)
- Do not promote `?view=public` as "private" — it is not. Real privacy comes in B4 via separate sanitized row + RLS

## Failure modes to avoid during B execution
- Dual-write must not spike SQLite write lock (respect the "≤50 rows per commit" rule, PM-001)
- RLS must be audited and verified before public row is exposed — Supabase anon key is public by design; RLS is the only fence
- v2 must have a `schema_version` field; frontend must warn loudly on mismatch (kills the recurring silent-panel-drift bug class)
- Public row must strip: `active_positions`, `resting_orders`, `current_balance` (raw), shadow signals, per-trade entry prices, raw_prob / calibrated_prob, edge, kelly_f
- Do not launch public surface without: financial disclaimers, methodology notes for Sharpe/DD, Kalshi-not-securities framing

## Work tracking
Tasks #1–#15 in the session task list. Execution order:
- **This week:** #1 (docs), #2 (spx_harrv fix), #3 (sync gap), #4 (Chesterton deletes), #5 (ghost panels), #6 (agent-2 deletes), #7 (duplicate collapse), #8 (modularize), #9 (schema validator), #10 (?view=public)
- **Following weeks:** #11 (v2 contract), #12 (dual-write), #13 (v2 HTML), #14 (sanitized public row), #15 (retire v1)

## Related
- [[concepts/dashboard-architecture.md]] — current state
- [[failures/dashboard-drift.md]] — swarm findings in detail
- [[failures/supabase-sync-silent-failure.md]] — 2026-04-04 precedent for `SELECT *` schema drift
- [[failures/pnl-reporting-bugs.md]] — `rta.cumulative_pnl` scope bug (precedent for duplicate-flavor confusion)
