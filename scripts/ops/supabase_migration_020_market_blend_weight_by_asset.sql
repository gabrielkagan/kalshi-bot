-- Migration 020: P2.1.d (2026-05-13) per-asset market blend weights —
-- adds `market_blend_weight_by_asset jsonb` column to calibration_snapshots.
--
-- Background:
--   P2.1.d (ClickUp 86b9xfwkg, ship 2026-05-13) replaces the scalar
--   MARKET_BLEND_W=0.40 with a per-asset map MARKET_BLEND_W_BY_ASSET
--   at the 15M consumer sites. P2.3 (ClickUp 86b9xv66a, ship 2026-05-14)
--   extends to 6 keys: {BTC:0.10, DOGE:0.60, ETH:0.20, HYPE:0.80,
--   SOL:0.80, XRP:0.90}.
--   The 14-day Brier-monitored soak depends on dashboard + Supabase
--   reporting the per-asset weight that actually fired at decision
--   time — the scalar `market_blend_weight` column reports 0.40 for all
--   assets post-deploy and no longer reflects production reality.
--
--   bot/snapshots/supabase_sync.py:829 (calibration_snapshots row build)
--   gains a `market_blend_weight_by_asset` field carrying the per-asset
--   map as JSON. This migration ships the matching jsonb column on the
--   remote so the POSTs don't HTTP-400 (per supabase_sync._validate_
--   schema_parity contract — see migration 019 docstring for the lock-
--   step rule).
--
-- Types: jsonb on Postgres; the Python side serializes the dict via
--   the standard supabase REST POST path which auto-jsonifies dict
--   values. jsonb (vs json) gives indexable query semantics if a
--   future audit script wants to filter rows by per-asset value.
--
-- Apply:
--   Supabase dashboard → SQL Editor → paste this block → Run
--   OR via MCP: mcp__claude_ai_Supabase__apply_migration
--
-- Idempotent: ADD COLUMN IF NOT EXISTS.
--
-- ClickUp: 86b9xfwkg
-- Bit doc: kb/decisions/p2-1-d-pickup-prompt-may13.md

ALTER TABLE public.calibration_snapshots
  -- P2.1.d + P2.3 per-asset 15M blend weights (all 6 production 15M
  -- assets: BTC 0.10, DOGE 0.60, ETH 0.20, HYPE 0.80, SOL 0.80, XRP 0.90).
  -- jsonb so future asset additions don't require schema migrations.
  ADD COLUMN IF NOT EXISTS market_blend_weight_by_asset jsonb;

-- Reload PostgREST schema cache so the new column is visible to POSTs.
NOTIFY pgrst, 'reload schema';
