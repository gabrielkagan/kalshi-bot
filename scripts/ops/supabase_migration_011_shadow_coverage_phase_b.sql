-- Migration 011: Shadow coverage expansion Phase B — 18 nullable columns on evaluations
--
-- Background:
--   bot.py StateManager._create_tables migration loop adds 18 new ALTER TABLE
--   ADD COLUMN entries to local SQLite `evaluated_opportunities` (commit
--   shipping with this file). Column families:
--     state-at-decision (3): n_open_positions, recent_n_outcome_streak,
--                             time_since_last_fill_s
--     maker counterfactual (3): maker_price_cents, maker_depth_at_post,
--                                maker_would_fill_within_30s
--     path-of-rejection (1): next_blocking_gate
--     resolution metadata (5): final_spot_price, knockout_time_relative,
--                               max_excursion_from_strike,
--                               time_above_strike_seconds,
--                               time_below_strike_seconds
--     cross-asset (4): btc/eth/sol/xrp_spot_at_decision (absolute prices —
--                       existing btc_spot_change_*_bps are RELATIVE)
--     funding/basis (2): okx/deribit_funding_rate_at_decision
--
--   To mirror those columns to Supabase, supabase_sync._EVAL_COLUMNS gained
--   the 18 fields. Per supabase_sync._check_schema_parity (and the
--   2026-04-04 incident postmortem in kb/failures/dashboard-drift.md),
--   adding columns to that whitelist WITHOUT the remote columns existing
--   silently HTTP-400s every batch and freezes sync. Ship this migration
--   FIRST, then the bot/snapshots/supabase_sync.py edit.
--
--   Phase B is schema-only. Population ships in phases D (cal_mlp annotation),
--   E (state-at-decision-time helper), and F (resolution / cross-asset /
--   funding enrichment). All columns are nullable to avoid backfill.
--
-- Types: integer columns mirror the SQLite INTEGER declarations; real-valued
--   columns mirror REAL. Postgres `real` is single-precision (~7 digits) which
--   matches the existing convention on this table (calibrated_prob, edge,
--   etc.). Higher-precision data — e.g. spot prices for high-priced assets
--   — uses `double precision` to avoid rounding to whole-cent BTC/ETH levels.
--
-- Apply:
--   Supabase dashboard → SQL Editor → paste this block → Run
--   OR via MCP: mcp__claude_ai_Supabase__apply_migration
--
-- Idempotent: ADD COLUMN IF NOT EXISTS.
--
-- Master plan: kb/decisions/shadow-coverage-expansion-may01.md

ALTER TABLE public.evaluations
  -- State-at-decision-time
  ADD COLUMN IF NOT EXISTS n_open_positions             integer,
  ADD COLUMN IF NOT EXISTS recent_n_outcome_streak      integer,
  ADD COLUMN IF NOT EXISTS time_since_last_fill_s       real,
  -- Maker counterfactual
  ADD COLUMN IF NOT EXISTS maker_price_cents            integer,
  ADD COLUMN IF NOT EXISTS maker_depth_at_post          integer,
  ADD COLUMN IF NOT EXISTS maker_would_fill_within_30s  integer,
  -- Path-of-rejection
  ADD COLUMN IF NOT EXISTS next_blocking_gate           text,
  -- Resolution metadata
  ADD COLUMN IF NOT EXISTS final_spot_price             double precision,
  ADD COLUMN IF NOT EXISTS knockout_time_relative       real,
  ADD COLUMN IF NOT EXISTS max_excursion_from_strike    double precision,
  ADD COLUMN IF NOT EXISTS time_above_strike_seconds    real,
  ADD COLUMN IF NOT EXISTS time_below_strike_seconds    real,
  -- Cross-asset (absolute spot prices at decision tick)
  ADD COLUMN IF NOT EXISTS btc_spot_at_decision         double precision,
  ADD COLUMN IF NOT EXISTS eth_spot_at_decision         double precision,
  ADD COLUMN IF NOT EXISTS sol_spot_at_decision         double precision,
  ADD COLUMN IF NOT EXISTS xrp_spot_at_decision         double precision,
  -- Funding / basis
  ADD COLUMN IF NOT EXISTS okx_funding_rate_at_decision     real,
  ADD COLUMN IF NOT EXISTS deribit_funding_rate_at_decision real;

-- Reload PostgREST schema cache so the new columns are visible to POSTs.
NOTIFY pgrst, 'reload schema';
