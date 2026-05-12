-- Supabase Migration 005: Daily PnL Reconciliation Support
-- Run in Supabase SQL Editor. Safe to re-run.
--
-- Creates an RPC function that returns per-day PnL summaries from the trades
-- table. Used by bot/snapshots/supabase_sync.py's reconciliation check to detect and auto-fix
-- data drift between VPS SQLite and Supabase (e.g., ghost fill corrections,
-- manual trade deletions).

-- ============================================================================
-- 1. RPC function: daily_pnl_summary
-- ============================================================================

CREATE OR REPLACE FUNCTION daily_pnl_summary()
RETURNS TABLE (day TEXT, pnl BIGINT, cnt BIGINT)
LANGUAGE sql
STABLE
AS $$
    SELECT
        TO_CHAR(DATE(settled_at AT TIME ZONE 'UTC'), 'YYYY-MM-DD') AS day,
        SUM(pnl_cents)::BIGINT AS pnl,
        COUNT(*)::BIGINT AS cnt
    FROM trades
    WHERE settled_at IS NOT NULL
    GROUP BY DATE(settled_at AT TIME ZONE 'UTC')
    ORDER BY day;
$$;

-- Grant access to the service role (used by bot/snapshots/supabase_sync.py)
GRANT EXECUTE ON FUNCTION daily_pnl_summary() TO service_role;
