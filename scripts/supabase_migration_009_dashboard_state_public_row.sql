-- Migration 009: Allow id=2 on dashboard_state (public/sanitized row)
--
-- Background:
--   Phase P (Public dashboard) writes two rows: id=1 (operator, existing) and
--   id=2 (sanitized, consumed by /performance/ page). The original schema
--   declared `CHECK (id = 1)` to enforce a single-row pattern. Relax to (1, 2).
--
--   Constraint name is dashboard_state_id_check (verified via PGRST 23514 error).
--
-- Apply:
--   Supabase dashboard → SQL Editor → paste this block → Run
--
-- Idempotent: DROP IF EXISTS + ADD.

ALTER TABLE dashboard_state DROP CONSTRAINT IF EXISTS dashboard_state_id_check;
ALTER TABLE dashboard_state ADD CONSTRAINT dashboard_state_id_check CHECK (id IN (1, 2));

-- Seed the public row (empty; bot will overwrite on next sync cycle)
INSERT INTO dashboard_state (id, data) VALUES (2, '{}')
ON CONFLICT (id) DO NOTHING;

-- Reload PostgREST schema cache
NOTIFY pgrst, 'reload schema';
