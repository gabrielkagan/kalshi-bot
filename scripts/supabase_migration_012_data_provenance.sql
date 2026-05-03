-- Migration 012: data_provenance column on public.evaluations
--
-- Phase G-6 — BLOCKING prerequisite for v2 calibrator retraining.
--
-- Adds a `data_provenance` TEXT column to mark each row as either
-- 'live_ws' (captured live, sub-second precision) or 'backfill_60s_inputs'
-- (G-2/G-4 inputs derived from 1-min Coinbase candles). v2 training
-- MUST stratify / filter by this column to avoid silent train/serve skew.
-- See kb/decisions/v2-train-must-account-for-backfill-skew-may02.md for
-- the BLOCKING rule.
--
-- NOTE: SQLite-side table is `evaluated_opportunities`; the Supabase
-- mirror table is `public.evaluations` (per supabase_sync._TABLE_MAP).
--
-- Operator: apply via Supabase SQL editor or:
--   PGPASSWORD="$SUPABASE_DB_PASSWORD" psql "$SUPABASE_DB_URL" \
--     -f scripts/supabase_migration_012_data_provenance.sql
--
-- Idempotent — IF NOT EXISTS guards + UPDATE filters on
-- `data_provenance IS NULL`, so re-running the file re-stamps zero rows.
--
-- Why this migration ALSO does the historical UPDATE (round 2 #1):
-- supabase_sync.py is incremental on `id` (WHERE id > watermark).
-- An UPDATE on existing rows by stamp_data_provenance.py NEVER moves
-- the watermark, so without this Postgres-side UPDATE the Supabase
-- mirror would keep `data_provenance = NULL` on all 81K+ historical
-- rows forever. v2 / dashboards / external researchers reading from
-- Supabase would silently mis-categorize all historical rows.

ALTER TABLE public.evaluations
    ADD COLUMN IF NOT EXISTS data_provenance TEXT;

-- Bulk backfill historical rows so Supabase mirror matches SQLite after
-- stamp_data_provenance.py runs. Predicate logic mirrors the SQLite
-- stamp script (scripts/stamp_data_provenance.py):
--   - product_type='15m' AND evaluation_time < F-3-deploy
--     AND (btc_spot OR time_above NOT NULL) → 'backfill_60s_inputs'
--   - product_type='15m' AND evaluation_time >= F-3-deploy → 'live_ws'
--   - product_type='15m' pre-cutoff with neither input → leave NULL
--   - non-15m rows → leave NULL
--
-- Cutoff = Phase F-3 deploy time (commit 4897846, 2026-05-02T19:53:19Z).
-- Postgres comparison uses native timestamp ordering; we cast the
-- string literal to timestamptz to be explicit about timezone handling
-- (avoids implicit-cast surprises if `evaluation_time` is text vs ts).
UPDATE public.evaluations
   SET data_provenance = 'backfill_60s_inputs'
 WHERE product_type = '15m'
   AND data_provenance IS NULL
   AND evaluation_time < TIMESTAMPTZ '2026-05-02T19:53:19+00'
   AND (btc_spot_at_decision IS NOT NULL
        OR time_above_strike_seconds IS NOT NULL);

UPDATE public.evaluations
   SET data_provenance = 'live_ws'
 WHERE product_type = '15m'
   AND data_provenance IS NULL
   AND evaluation_time >= TIMESTAMPTZ '2026-05-02T19:53:19+00';

-- v2 training queries filter on data_provenance as their primary axis
-- (held-out validation: WHERE data_provenance = 'live_ws'). The column
-- is low-cardinality but the filter runs over the full table, so a
-- btree index pays for itself once we accumulate >100K live rows.
-- Not CONCURRENTLY because (a) the table is small enough that the
-- ACCESS EXCLUSIVE lock is sub-second and (b) CONCURRENTLY can't run
-- inside an implicit transaction block from psql -f.
CREATE INDEX IF NOT EXISTS evaluations_data_provenance_idx
    ON public.evaluations (data_provenance);

-- Tell PostgREST to reload its schema cache so supabase_sync.py can
-- begin sending data_provenance in INSERT bodies immediately. Without
-- this, the cache may take up to a minute to refresh and the first
-- few batches HTTP-400 with PGRST204 (column not in schema cache).
NOTIFY pgrst, 'reload schema';
