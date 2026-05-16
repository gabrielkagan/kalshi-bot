-- Supabase Migration 002: Backfills, new views, retention, reconciliation support
-- Run in Supabase SQL Editor. Safe to re-run.

-- ============================================================================
-- #2: Add UNIQUE constraint on volatility_snapshots for data integrity
-- ============================================================================
DO $$ BEGIN
  ALTER TABLE volatility_snapshots ADD CONSTRAINT uq_vol_snap_asset_time
    UNIQUE (asset, snapshot_time);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ============================================================================
-- #3: Backfill product_type on historical evaluations and rejections
-- ============================================================================

-- Evaluations: hourly tickers
UPDATE evaluations SET product_type = 'hourly'
WHERE product_type IS NULL
  AND (event_ticker LIKE 'KXBTCD%' OR event_ticker LIKE 'KXETHD%'
       OR event_ticker LIKE 'KXSOLD%' OR event_ticker LIKE 'KXXRPD%');

-- Evaluations: everything else is 15m
UPDATE evaluations SET product_type = '15m'
WHERE product_type IS NULL;

-- Rejections: hourly tickers
UPDATE rejections SET product_type = 'hourly'
WHERE product_type IS NULL
  AND (event_ticker LIKE 'KXBTCD%' OR event_ticker LIKE 'KXETHD%'
       OR event_ticker LIKE 'KXSOLD%' OR event_ticker LIKE 'KXXRPD%');

-- Rejections: everything else is 15m
UPDATE rejections SET product_type = '15m'
WHERE product_type IS NULL;

-- Trades: hourly tickers
UPDATE trades SET product_type = 'hourly'
WHERE product_type IS NULL
  AND (event_ticker LIKE 'KXBTCD%' OR event_ticker LIKE 'KXETHD%'
       OR event_ticker LIKE 'KXSOLD%' OR event_ticker LIKE 'KXXRPD%');

-- Trades: everything else is 15m
UPDATE trades SET product_type = '15m'
WHERE product_type IS NULL;

-- Reset rejections watermark to trigger full re-sync with new rowid-based code
UPDATE sync_watermarks SET last_synced_id = 0
WHERE source_table = 'rejected_opportunities';

-- ============================================================================
-- #5: Hourly calibration drift alert view
-- ============================================================================

DROP MATERIALIZED VIEW IF EXISTS mv_hourly_calibration_drift;
CREATE MATERIALIZED VIEW mv_hourly_calibration_drift AS
SELECT
    DATE(evaluation_time AT TIME ZONE 'UTC') AS eval_date,
    CASE
        WHEN calibrated_prob < 0.80 THEN '<80%'
        WHEN calibrated_prob < 0.85 THEN '80-85%'
        WHEN calibrated_prob < 0.90 THEN '85-90%'
        WHEN calibrated_prob < 0.95 THEN '90-95%'
        ELSE '95%+'
    END AS prob_bucket,
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE market_result IS NOT NULL) AS settled,
    ROUND(AVG(calibrated_prob)::NUMERIC, 4) AS avg_predicted,
    ROUND(AVG(CASE WHEN market_result IN ('yes', 'all_yes') THEN 1.0
                   WHEN market_result IN ('no', 'all_no') THEN 0.0
                   ELSE NULL END)::NUMERIC, 4) AS avg_actual,
    ROUND(AVG(CASE WHEN market_result IS NOT NULL THEN
        (calibrated_prob - CASE WHEN market_result IN ('yes', 'all_yes') THEN 1.0 ELSE 0.0 END)^2
    END)::NUMERIC, 6) AS brier_score
FROM evaluations
WHERE product_type = 'hourly'
  AND filter_stage IN ('candidate', 'observation_trade')
  AND calibrated_prob IS NOT NULL
GROUP BY 1, 2
ORDER BY 1 DESC, 2;

-- RPC wrapper
CREATE OR REPLACE FUNCTION get_analytics_hourly_cal_drift()
RETURNS TABLE(eval_date date, prob_bucket text, total bigint, settled bigint,
              avg_predicted numeric, avg_actual numeric, brier_score numeric)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT * FROM mv_hourly_calibration_drift ORDER BY eval_date DESC, prob_bucket
$$;

GRANT EXECUTE ON FUNCTION get_analytics_hourly_cal_drift TO anon;

-- ============================================================================
-- #6: Per-window correlation analysis view
-- ============================================================================

DROP MATERIALIZED VIEW IF EXISTS mv_hourly_window_correlation;
CREATE MATERIALIZED VIEW mv_hourly_window_correlation AS
SELECT
    event_ticker,
    COUNT(*) AS positions_in_window,
    COUNT(DISTINCT asset) AS distinct_assets,
    COUNT(*) FILTER (WHERE market_result IS NOT NULL) AS settled,
    COUNT(*) FILTER (WHERE market_result IN ('yes', 'all_yes')) AS wins,
    COUNT(*) FILTER (WHERE market_result IN ('no', 'all_no')) AS losses,
    -- All-or-nothing: did ALL positions in this window win or ALL lose?
    CASE
        WHEN COUNT(*) FILTER (WHERE market_result IS NOT NULL) = 0 THEN 'pending'
        WHEN COUNT(*) FILTER (WHERE market_result IN ('no', 'all_no')) = 0 THEN 'all_won'
        WHEN COUNT(*) FILTER (WHERE market_result IN ('yes', 'all_yes')) = 0 THEN 'all_lost'
        ELSE 'mixed'
    END AS window_outcome,
    SUM(COALESCE(counterfactual_pnl, 0)) AS window_pnl_cents
FROM evaluations
WHERE product_type = 'hourly'
  AND filter_stage IN ('candidate', 'observation_trade')
GROUP BY event_ticker
HAVING COUNT(*) > 1
ORDER BY event_ticker DESC;

CREATE OR REPLACE FUNCTION get_analytics_window_correlation()
RETURNS TABLE(event_ticker text, positions_in_window bigint, distinct_assets bigint,
              settled bigint, wins bigint, losses bigint,
              window_outcome text, window_pnl_cents bigint)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT * FROM mv_hourly_window_correlation ORDER BY event_ticker DESC
$$;

GRANT EXECUTE ON FUNCTION get_analytics_window_correlation TO anon;

-- ============================================================================
-- #8: Data retention / pruning function
-- ============================================================================

CREATE OR REPLACE FUNCTION prune_old_data(days_to_keep INTEGER DEFAULT 90)
RETURNS TABLE(table_name text, rows_deleted bigint) AS $$
DECLARE
    cutoff TIMESTAMPTZ := NOW() - (days_to_keep || ' days')::INTERVAL;
    del_count BIGINT;
BEGIN
    -- Volatility snapshots (keep last N days)
    DELETE FROM volatility_snapshots WHERE snapshot_time < cutoff;
    GET DIAGNOSTICS del_count = ROW_COUNT;
    table_name := 'volatility_snapshots'; rows_deleted := del_count; RETURN NEXT;

    -- Calibration snapshots (keep last N days)
    DELETE FROM calibration_snapshots WHERE snapshot_time < cutoff;
    GET DIAGNOSTICS del_count = ROW_COUNT;
    table_name := 'calibration_snapshots'; rows_deleted := del_count; RETURN NEXT;

    -- Scan summaries (keep last N days)
    DELETE FROM scan_summaries WHERE window_start < cutoff;
    GET DIAGNOSTICS del_count = ROW_COUNT;
    table_name := 'scan_summaries'; rows_deleted := del_count; RETURN NEXT;

    -- Order events (keep last N days)
    DELETE FROM order_events WHERE event_time < cutoff;
    GET DIAGNOSTICS del_count = ROW_COUNT;
    table_name := 'order_events'; rows_deleted := del_count; RETURN NEXT;

    -- Fill model samples (keep last N days)
    DELETE FROM fill_model_samples WHERE sample_time < cutoff;
    GET DIAGNOSTICS del_count = ROW_COUNT;
    table_name := 'fill_model_samples'; rows_deleted := del_count; RETURN NEXT;

    -- Note: trades, evaluations, rejections are NEVER pruned (core analytics data)
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

GRANT EXECUTE ON FUNCTION prune_old_data TO anon;

-- ============================================================================
-- #10: Edge decay analysis view (predicted edge vs actual outcome by STC bucket)
-- ============================================================================

DROP MATERIALIZED VIEW IF EXISTS mv_edge_decay;
CREATE MATERIALIZED VIEW mv_edge_decay AS
SELECT
    CASE
        WHEN seconds_to_close < 60 THEN '<1min'
        WHEN seconds_to_close < 120 THEN '1-2min'
        WHEN seconds_to_close < 180 THEN '2-3min'
        WHEN seconds_to_close < 240 THEN '3-4min'
        WHEN seconds_to_close < 300 THEN '4-5min'
        ELSE '5min+'
    END AS stc_bucket,
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE is_win) AS wins,
    ROUND(COUNT(*) FILTER (WHERE is_win)::NUMERIC / NULLIF(COUNT(*), 0), 4) AS win_rate,
    ROUND(AVG(edge)::NUMERIC, 4) AS avg_edge,
    ROUND(AVG(pnl_cents - fee_cents)::NUMERIC, 1) AS avg_net_pnl,
    SUM(pnl_cents - fee_cents) AS total_net_pnl,
    product_type
FROM trades
WHERE edge IS NOT NULL AND seconds_to_close IS NOT NULL
GROUP BY 1, product_type
ORDER BY 1;

CREATE OR REPLACE FUNCTION get_analytics_edge_decay()
RETURNS TABLE(stc_bucket text, total bigint, wins bigint, win_rate numeric,
              avg_edge numeric, avg_net_pnl numeric, total_net_pnl bigint,
              product_type text)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT * FROM mv_edge_decay ORDER BY stc_bucket
$$;

GRANT EXECUTE ON FUNCTION get_analytics_edge_decay TO anon;

-- ============================================================================
-- Update refresh_analytics_views to include new materialized views
-- ============================================================================

CREATE OR REPLACE FUNCTION refresh_analytics_views()
RETURNS VOID AS $$
BEGIN
    REFRESH MATERIALIZED VIEW mv_win_rate_by_asset;
    REFRESH MATERIALIZED VIEW mv_daily_pnl;
    REFRESH MATERIALIZED VIEW mv_win_rate_by_price;
    REFRESH MATERIALIZED VIEW mv_calibration_accuracy;
    REFRESH MATERIALIZED VIEW mv_counterfactual_by_stage;
    REFRESH MATERIALIZED VIEW mv_fill_rates;
    REFRESH MATERIALIZED VIEW mv_hourly_calibration_drift;
    REFRESH MATERIALIZED VIEW mv_hourly_window_correlation;
    REFRESH MATERIALIZED VIEW mv_edge_decay;
    -- Also refresh views from migration 001 if they exist
    BEGIN REFRESH MATERIALIZED VIEW mv_rolling_calibration; EXCEPTION WHEN undefined_table THEN NULL; END;
    BEGIN REFRESH MATERIALIZED VIEW mv_hourly_readiness; EXCEPTION WHEN undefined_table THEN NULL; END;
    BEGIN REFRESH MATERIALIZED VIEW mv_stc_performance; EXCEPTION WHEN undefined_table THEN NULL; END;
    BEGIN REFRESH MATERIALIZED VIEW mv_edge_realized; EXCEPTION WHEN undefined_table THEN NULL; END;
END;
$$ LANGUAGE plpgsql;
