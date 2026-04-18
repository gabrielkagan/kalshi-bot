-- Migration 008: Sync spx_harrv_shadow_signals Supabase schema with local SQLite
--
-- Background:
--   The harrv shadow sync has been silently 400'ing for weeks (PGRST204).
--   Remote Supabase table has 49 columns; local SQLite has 52. After fixing
--   bankroll_cents, 22 more missing columns surfaced. Adding all of them here.
--
--   Column types mapped from SQLite → Postgres:
--     INTEGER → INTEGER, REAL → DOUBLE PRECISION, TEXT → TEXT
--
-- Apply:
--   1. Supabase dashboard → SQL Editor → paste this block → Run
--   2. Watch VPS logs for PGRST204 to stop
--   3. Confirm _wm_harrv watermark advances (incremental sync resumes)
--
-- Idempotent (ADD COLUMN IF NOT EXISTS + NOTIFY).

ALTER TABLE spx_harrv_shadow_signals
    ADD COLUMN IF NOT EXISTS bankroll_cents INTEGER,
    ADD COLUMN IF NOT EXISTS best_ask INTEGER,
    ADD COLUMN IF NOT EXISTS best_bid INTEGER,
    ADD COLUMN IF NOT EXISTS egarch_edge DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS egarch_prob DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS est_fee_cents INTEGER,
    ADD COLUMN IF NOT EXISTS final_prob DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS mkt_only_prob DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS n_returns_1d INTEGER,
    ADD COLUMN IF NOT EXISTS n_returns_1h INTEGER,
    ADD COLUMN IF NOT EXISTS n_returns_1w INTEGER,
    ADD COLUMN IF NOT EXISTS rv_1d DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS rv_1d_imputed INTEGER,
    ADD COLUMN IF NOT EXISTS rv_1h DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS rv_1w DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS rv_1w_imputed INTEGER,
    ADD COLUMN IF NOT EXISTS rv_forecast DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS scaled_prob DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS settled_time TEXT,
    ADD COLUMN IF NOT EXISTS shadow_contracts INTEGER,
    ADD COLUMN IF NOT EXISTS shadow_pnl_cents INTEGER,
    ADD COLUMN IF NOT EXISTS sigma_forecast DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS status TEXT,
    ADD COLUMN IF NOT EXISTS threshold DOUBLE PRECISION;

NOTIFY pgrst, 'reload schema';
