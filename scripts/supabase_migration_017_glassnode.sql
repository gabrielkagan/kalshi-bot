-- Migration 017: Phase H-4b — Glassnode on-chain features on evaluations
--
-- Background:
--   Phase H-4b backfills three Glassnode-derived features per 15M
--   evaluation row, all expressed as 24h-rolling z-scores against a
--   trailing 30-day mean+std of the same metric (so the value is
--   distribution-free across asset/regime):
--     - btc_active_addresses_24h_zscore  (REAL): BTC daily active address count
--     - eth_active_addresses_24h_zscore  (REAL): ETH daily active address count
--     - btc_exchange_inflow_24h_zscore   (REAL): BTC exchange inflow native units
--       (free tier may not cover this — column is nullable; backfill writes
--       NULL with a per-day skip warning when source is unavailable, NEVER
--       silently for a single row).
--
--   Source: https://api.glassnode.com/v1/metrics/...
--   Free tier: addresses/active_count, transactions/count, market/price_*.
--   Premium tier: distribution/exchange_*. We default to free; if an
--   API key is present (env GLASSNODE_API_KEY) the premium endpoints
--   are attempted, falling back to NULL on 401/403.
--
--   Daily metrics — 24h cadence, sufficient because the features are
--   shaped at z-score scale (the bot's decisions are on 15M markets;
--   on-chain dynamics evolve much slower than that).
--
--   Backfill: scripts/glassnode_backfill.py (date-bucketed cache: one
--   API call per asset per day, then one z-score per row from the
--   trailing-30d window in memory).
--
--   Design doc: kb/decisions/phase-h4b-glassnode-may02.md.
--   Master plan: kb/decisions/shadow-coverage-phase-h-data-recovery-may02.md.
--
-- Apply:
--   Supabase dashboard → SQL Editor → paste this block → Run
--   OR via MCP: mcp__claude_ai_Supabase__apply_migration
--
-- Idempotent: ADD COLUMN IF NOT EXISTS.

ALTER TABLE public.evaluations
  ADD COLUMN IF NOT EXISTS btc_active_addresses_24h_zscore real,
  ADD COLUMN IF NOT EXISTS eth_active_addresses_24h_zscore real,
  ADD COLUMN IF NOT EXISTS btc_exchange_inflow_24h_zscore  real;

-- Reload PostgREST schema cache so the new columns are visible to POSTs.
NOTIFY pgrst, 'reload schema';
