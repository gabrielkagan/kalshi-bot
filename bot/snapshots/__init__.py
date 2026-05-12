"""bot.snapshots — JSON snapshot writers + Supabase syncer (Sprint 10 Bit 10.4).

Producer-side modules that read in-memory bot state and SQLite tables and
emit snapshots to Supabase (dashboard JSON, evaluated-opportunity row
microstate, continuous NBBO observations) plus the background syncer
that mirrors SQLite → Supabase. Consumers (the bot scan loop, settlement,
executor) never import this subpackage; main_loop.py owns the late-binding
wiring at startup.

Modules:
  - dashboard_snapshot.py — DashboardSnapshotBuilder (4500 LOC)
  - bot_state_snapshot.py — compute_bot_state_snapshot helper (491 LOC)
  - market_observations_snapshotter.py — MarketObservationsSnapshotter
    + extract_active_15m_tickers helper (575 LOC)
  - supabase_sync.py — SupabaseSyncer background thread (1049 LOC)

Each module anchors `__file__`-derived paths (dist_config.json, state.db,
.supabase_kill_switch) to the repo root via a 3-level dirname chain
matching the bot/engines/weather_engine.py:806-807 precedent. The kill
switch file location is preserved at <repo>/.supabase_kill_switch.

Lock-step: dashboard_snapshot.py output is consumed by dashboard/index.html
on the gh-pages branch. JSON schema changes (key names) ship in the same
commit as the rendering update per kb/decisions/dashboard-overhaul-plan.md.
"""
