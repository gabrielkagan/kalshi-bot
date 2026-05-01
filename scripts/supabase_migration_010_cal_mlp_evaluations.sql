-- Migration 010: cal_mlp_* annotation columns on evaluations
--
-- Background:
--   bot.py commit f7d2f47 fixed the propagation of cal_mlp_* fields from
--   _shadow_diag to evaluated_opportunities INSERTs at the 4 trade-endpoint
--   call sites (candidate, observation_trade, TM98/SOL_TAKER/HPSB bleed
--   blocks). Local DB now reaches 100% annotation on every 15M trade.
--
--   To mirror those annotations to Supabase, supabase_sync._EVAL_COLUMNS
--   gained the 7 fields below. Per supabase_sync._check_schema_parity (and
--   the 2026-04-04 incident postmortem in kb/failures/dashboard-drift.md),
--   adding columns to that whitelist WITHOUT the remote columns existing
--   silently HTTP-400s every batch and freezes sync. Ship this migration
--   FIRST, then the supabase_sync.py edit.
--
-- Types: every existing probability/edge column on this table is `real`
--   (calibrated_prob, raw_prob, breakeven_wr, edge, etc.) — keep the
--   convention. Python-side precision loss is ~10^-7, well below trading
--   meaningful resolution (we round to 6 digits in bot.py).
--
-- Apply:
--   Supabase dashboard → SQL Editor → paste this block → Run
--   OR via MCP: mcp__claude_ai_Supabase__apply_migration
--
-- Idempotent: ADD COLUMN IF NOT EXISTS.

ALTER TABLE public.evaluations
  ADD COLUMN IF NOT EXISTS cal_mlp_request_id     text,
  ADD COLUMN IF NOT EXISTS cal_mlp_skipped_reason text,
  ADD COLUMN IF NOT EXISTS cal_mlp_p_mean         real,
  ADD COLUMN IF NOT EXISTS cal_mlp_p_std          real,
  ADD COLUMN IF NOT EXISTS cal_mlp_final_lo       real,
  ADD COLUMN IF NOT EXISTS cal_mlp_final_hi       real,
  ADD COLUMN IF NOT EXISTS cal_mlp_train_id       text;

-- Reload PostgREST schema cache so the new columns are visible to POSTs.
NOTIFY pgrst, 'reload schema';
