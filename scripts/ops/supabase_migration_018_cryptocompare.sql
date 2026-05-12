-- Migration 018: Phase H-4c — CryptoCompare news sentiment feature on evaluations
--
-- Background:
--   Phase H-4c backfills one CryptoCompare-news-derived feature per 15M
--   evaluation row:
--     - news_sentiment_score_1h_pre_decision (REAL): mean of mapped
--       sentiment over CryptoCompare news articles tagged with the row's
--       underlying asset, in the 1-hour window ending at evaluation_time.
--
--       Sentiment mapping: CryptoCompare returns a categorical label per
--       article ("POSITIVE" / "NEGATIVE" / "NEUTRAL"). We map to:
--         POSITIVE → +1.0
--         NEUTRAL  →  0.0
--         NEGATIVE → -1.0
--       Average is in [-1.0, +1.0]. NULL when the 1h window has no
--       tagged articles (don't synthesize a 0.0 — that conflates "no
--       news" with "balanced news"; see design doc for rationale).
--
--   Source: https://min-api.cryptocompare.com/data/v2/news/?lang=EN&categories=...
--   Free tier covers 250K calls/month — well above what the backfill
--   needs. No API key required for the news endpoint.
--
--   Backfill: scripts/cryptocompare_news_backfill.py (date+hour+asset
--   bucket cache, like H-4a; many rows share buckets).
--
--   Design doc: kb/decisions/phase-h4c-cryptocompare-may02.md.
--   Master plan: kb/decisions/shadow-coverage-phase-h-data-recovery-may02.md.
--
-- Apply:
--   Supabase dashboard → SQL Editor → paste this block → Run
--   OR via MCP: mcp__claude_ai_Supabase__apply_migration
--
-- Idempotent: ADD COLUMN IF NOT EXISTS.

ALTER TABLE public.evaluations
  ADD COLUMN IF NOT EXISTS news_sentiment_score_1h_pre_decision real;

-- Reload PostgREST schema cache so the new columns are visible to POSTs.
NOTIFY pgrst, 'reload schema';
