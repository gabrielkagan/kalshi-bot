-- Migration 016: Phase H-4a — GDELT news event cluster features on evaluations
--
-- Background:
--   Phase H-4a backfills two GDELT-derived features per 15M evaluation row:
--     - gdelt_event_count_1h_pre_decision  (INTEGER): count of news articles
--       indexed by GDELT mentioning the row's underlying asset in the
--       1-hour window ending at evaluation_time.
--     - gdelt_avg_tone_1h_pre_decision     (REAL): mean of GDELT's `tone`
--       field over those same articles. GDELT's tone scale is roughly
--       [-10, +10] (negative = more negative-sentiment language).
--
--   Source: https://api.gdeltproject.org/api/v2/doc/doc?query=...&mode=ArtList
--   (free, no auth, indexed since 2015 — covers our entire backfill window).
--
--   Backfill: scripts/gdelt_backfill.py (date+hour+asset bucket cache —
--   many rows share the same hour bucket, so cache amortizes API cost).
--
--   Design doc: kb/decisions/phase-h4a-gdelt-may02.md.
--   Master plan: kb/decisions/shadow-coverage-phase-h-data-recovery-may02.md.
--
-- Apply:
--   Supabase dashboard → SQL Editor → paste this block → Run
--   OR via MCP: mcp__claude_ai_Supabase__apply_migration
--
-- Idempotent: ADD COLUMN IF NOT EXISTS.

ALTER TABLE public.evaluations
  ADD COLUMN IF NOT EXISTS gdelt_event_count_1h_pre_decision integer,
  ADD COLUMN IF NOT EXISTS gdelt_avg_tone_1h_pre_decision    real;

-- Reload PostgREST schema cache so the new columns are visible to POSTs.
NOTIFY pgrst, 'reload schema';
