-- Migration 019: Bit 2 / T1 cross-asset expansion — hype/doge_spot_at_decision
--
-- Background:
--   T1 (5dca85a, 2026-05-10) added HYPE/DOGE to ASSETS as shadow observation
--   assets. The scanner producer at bot/scanner/__init__.py:990
--   (_compute_cross_asset_spot_snapshot) became ASSETS-driven in T1 and
--   ALREADY emits all six cross-asset spot keys today — but the consumer
--   chain in bot/state.py + supabase_sync.py + the G-2 backfill harness in
--   scripts/shadow_coverage_backfill.py still only knew about
--   btc/eth/sol/xrp. The producer's hype/doge keys were silently dropped
--   on the floor (consumer block at bot/state.py:1833-1840).
--
--   Bit 2 closes the gap: bot/state.py adds 2 ALTER TABLE ADD COLUMN
--   entries + extends signature, consumer, INSERT, VALUES, ON CONFLICT,
--   and parameter tuple. supabase_sync._EVAL_COLUMNS gains the 2 fields.
--   scripts/shadow_coverage_backfill.py UPDATE statement extends to 6.
--   This migration ships the matching 2 columns on the remote.
--
--   Per supabase_sync._validate_schema_parity (and the 2026-04-04 incident
--   postmortem in kb/failures/dashboard-drift.md), adding columns to the
--   whitelist WITHOUT the remote columns existing silently HTTP-400s every
--   batch and freezes sync. Ship this migration FIRST, then the merge.
--
--   T1+T1.5+Bit 2 schema lockstep:
--     T1 5dca85a (2026-05-10): ASSETS = [BTC, ETH, SOL, XRP, HYPE, DOGE]
--     T1.5 bf8b9a3 (2026-05-10): external feeds + COINBASE_PRODUCTS extended
--     Bit 2 (2026-05-11): schema gap closed (this migration)
--
-- Types: REAL on SQLite side, `double precision` on Postgres — matches
--   the existing btc/eth/sol/xrp_spot_at_decision precedent in migration
--   011. High-precision spot prices need >= 7 significant digits (BTC at
--   $67432.50 = 7 sig figs; DOGE at 0.183722 = 6 sig figs but on small
--   absolute scale where rounding to single-precision could lose
--   meaningful relative magnitude).
--
-- Apply:
--   Supabase dashboard → SQL Editor → paste this block → Run
--   OR via MCP: mcp__claude_ai_Supabase__apply_migration
--
-- Idempotent: ADD COLUMN IF NOT EXISTS.
--
-- ClickUp: 86b9vrjf2
-- Bit doc: kb/decisions/asset-onboarding-doge-hype-bit-2-shipped-may10.md

ALTER TABLE public.evaluations
  -- Bit 2 / T1 cross-asset expansion (absolute spot prices at decision tick)
  ADD COLUMN IF NOT EXISTS hype_spot_at_decision         double precision,
  ADD COLUMN IF NOT EXISTS doge_spot_at_decision         double precision;

-- Reload PostgREST schema cache so the new columns are visible to POSTs.
NOTIFY pgrst, 'reload schema';
