-- Migration 001: Add missing columns + new analytics views
-- Run in Supabase SQL Editor: https://supabase.com/dashboard/project/srbdajecmkjxinmcozxl/sql
-- Safe to re-run (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS throughout)

-- ============================================================================
-- 1. Add product_type to evaluations and rejections
-- ============================================================================

ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS product_type TEXT;
CREATE INDEX IF NOT EXISTS idx_eval_product_type ON evaluations(product_type);

ALTER TABLE rejections ADD COLUMN IF NOT EXISTS product_type TEXT;
CREATE INDEX IF NOT EXISTS idx_rej_product_type ON rejections(product_type);

-- ============================================================================
-- 2. Add escalation tracking columns to trades
-- ============================================================================

ALTER TABLE trades ADD COLUMN IF NOT EXISTS escalation_type TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS maker_price_cents INTEGER;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS maker_wait_seconds REAL;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS product_type TEXT;
CREATE INDEX IF NOT EXISTS idx_trades_product_type ON trades(product_type);

-- ============================================================================
-- 3. New materialized view: Rolling calibration monitoring
-- ============================================================================

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_rolling_calibration AS
SELECT
    prob_bucket,
    product_type,
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE market_result = 'yes') AS wins,
    ROUND(AVG(calibrated_prob)::NUMERIC, 4) AS avg_predicted,
    ROUND(COUNT(*) FILTER (WHERE market_result = 'yes')::NUMERIC / NULLIF(COUNT(*), 0), 4) AS actual_win_rate,
    ROUND(AVG(calibrated_prob)::NUMERIC - COUNT(*) FILTER (WHERE market_result = 'yes')::NUMERIC / NULLIF(COUNT(*), 0), 4) AS calibration_gap,
    ROUND(AVG(fee_adjusted_edge)::NUMERIC, 4) AS avg_fee_adjusted_edge
FROM (
    SELECT
        *,
        CASE
            WHEN calibrated_prob < 0.80 THEN '<80%'
            WHEN calibrated_prob < 0.85 THEN '80-85%'
            WHEN calibrated_prob < 0.90 THEN '85-90%'
            WHEN calibrated_prob < 0.95 THEN '90-95%'
            ELSE '95%+'
        END AS prob_bucket
    FROM evaluations
    WHERE calibrated_prob IS NOT NULL
      AND market_result IS NOT NULL
      AND filter_stage IN ('candidate', 'observation_trade', 'hourly_observation')
) sub
GROUP BY prob_bucket, product_type
ORDER BY prob_bucket, product_type;

-- ============================================================================
-- 4. New materialized view: Hourly readiness scorecard
-- ============================================================================

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_hourly_readiness AS
SELECT
    COALESCE(product_type, '15m') AS product_type,
    filter_stage,
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE market_result = 'yes') AS wins,
    COUNT(*) FILTER (WHERE market_result = 'no') AS losses,
    ROUND(COUNT(*) FILTER (WHERE market_result = 'yes')::NUMERIC /
          NULLIF(COUNT(*) FILTER (WHERE market_result IS NOT NULL), 0), 4) AS win_rate,
    ROUND(AVG(calibrated_prob)::NUMERIC, 4) AS avg_predicted_prob,
    ROUND(AVG(fee_adjusted_edge)::NUMERIC, 4) AS avg_fee_adjusted_edge,
    ROUND(AVG(CASE WHEN market_result = 'yes' THEN (100 - market_price)
                   WHEN market_result = 'no' THEN -market_price
                   ELSE NULL END)::NUMERIC, 1) AS avg_cf_pnl_cents,
    SUM(CASE WHEN market_result = 'yes' THEN (100 - market_price)
             WHEN market_result = 'no' THEN -market_price
             ELSE 0 END) AS total_cf_pnl_cents
FROM evaluations
WHERE market_result IS NOT NULL
GROUP BY COALESCE(product_type, '15m'), filter_stage
ORDER BY product_type, filter_stage;

-- ============================================================================
-- 5. New materialized view: STC performance analysis
-- ============================================================================

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_stc_performance AS
SELECT
    COALESCE(product_type, '15m') AS product_type,
    CASE
        WHEN seconds_to_close < 120 THEN '<2min'
        WHEN seconds_to_close < 180 THEN '2-3min'
        WHEN seconds_to_close < 240 THEN '3-4min'
        WHEN seconds_to_close < 300 THEN '4-5min'
        WHEN seconds_to_close < 600 THEN '5-10min'
        WHEN seconds_to_close < 900 THEN '10-15min'
        ELSE '15min+'
    END AS stc_bucket,
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE market_result = 'yes') AS wins,
    COUNT(*) FILTER (WHERE market_result = 'no') AS losses,
    ROUND(COUNT(*) FILTER (WHERE market_result = 'yes')::NUMERIC /
          NULLIF(COUNT(*) FILTER (WHERE market_result IS NOT NULL), 0), 4) AS win_rate,
    ROUND(AVG(calibrated_prob)::NUMERIC, 4) AS avg_predicted,
    ROUND(AVG(fee_adjusted_edge)::NUMERIC, 4) AS avg_edge
FROM evaluations
WHERE seconds_to_close IS NOT NULL
  AND market_result IS NOT NULL
  AND filter_stage IN ('candidate', 'observation_trade', 'hourly_observation')
GROUP BY COALESCE(product_type, '15m'), 2
ORDER BY product_type, stc_bucket;

-- ============================================================================
-- 6. New materialized view: Edge-realized analysis
-- ============================================================================

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_edge_realized AS
SELECT
    COALESCE(product_type, '15m') AS product_type,
    CASE
        WHEN fee_adjusted_edge < 0.005 THEN '<0.5%'
        WHEN fee_adjusted_edge < 0.01 THEN '0.5-1%'
        WHEN fee_adjusted_edge < 0.02 THEN '1-2%'
        WHEN fee_adjusted_edge < 0.04 THEN '2-4%'
        WHEN fee_adjusted_edge < 0.06 THEN '4-6%'
        ELSE '6%+'
    END AS edge_bucket,
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE market_result = 'yes') AS wins,
    COUNT(*) FILTER (WHERE market_result = 'no') AS losses,
    ROUND(COUNT(*) FILTER (WHERE market_result = 'yes')::NUMERIC /
          NULLIF(COUNT(*) FILTER (WHERE market_result IS NOT NULL), 0), 4) AS win_rate,
    ROUND(AVG(fee_adjusted_edge)::NUMERIC, 4) AS avg_edge,
    SUM(CASE WHEN market_result = 'yes' THEN (100 - market_price)
             WHEN market_result = 'no' THEN -market_price
             ELSE 0 END) AS cf_pnl_cents
FROM evaluations
WHERE fee_adjusted_edge IS NOT NULL
  AND market_result IS NOT NULL
  AND filter_stage IN ('candidate', 'observation_trade', 'hourly_observation')
GROUP BY COALESCE(product_type, '15m'), 2
ORDER BY product_type, edge_bucket;

-- ============================================================================
-- 7. New materialized view: Counterfactual P&L by filter (with product_type)
-- ============================================================================

DROP MATERIALIZED VIEW IF EXISTS mv_counterfactual_by_stage;
CREATE MATERIALIZED VIEW mv_counterfactual_by_stage AS
SELECT
    filter_stage,
    COALESCE(product_type, '15m') AS product_type,
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE market_result IS NOT NULL) AS settled,
    COUNT(*) FILTER (WHERE market_result = 'yes') AS would_have_won,
    COUNT(*) FILTER (WHERE market_result = 'no') AS would_have_lost,
    SUM(CASE WHEN market_result = 'yes' THEN (100 - market_price) ELSE 0 END) AS money_left_cents,
    SUM(CASE WHEN market_result = 'no' THEN market_price ELSE 0 END) AS bullets_dodged_cents
FROM evaluations
WHERE filter_stage NOT IN ('candidate', 'observation_trade')
  AND market_result IS NOT NULL
GROUP BY filter_stage, COALESCE(product_type, '15m')
ORDER BY money_left_cents DESC;

-- ============================================================================
-- 8. Update refresh function to include new views
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
    REFRESH MATERIALIZED VIEW mv_rolling_calibration;
    REFRESH MATERIALIZED VIEW mv_hourly_readiness;
    REFRESH MATERIALIZED VIEW mv_stc_performance;
    REFRESH MATERIALIZED VIEW mv_edge_realized;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- 9. New RPC functions for analytics
-- ============================================================================

CREATE OR REPLACE FUNCTION get_analytics_rolling_calibration()
RETURNS TABLE(prob_bucket text, product_type text, total bigint, wins bigint,
              avg_predicted numeric, actual_win_rate numeric, calibration_gap numeric,
              avg_fee_adjusted_edge numeric)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT * FROM mv_rolling_calibration ORDER BY product_type, prob_bucket
$$;

CREATE OR REPLACE FUNCTION get_analytics_hourly_readiness()
RETURNS TABLE(product_type text, filter_stage text, total bigint, wins bigint, losses bigint,
              win_rate numeric, avg_predicted_prob numeric, avg_fee_adjusted_edge numeric,
              avg_cf_pnl_cents numeric, total_cf_pnl_cents bigint)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT * FROM mv_hourly_readiness ORDER BY product_type, filter_stage
$$;

CREATE OR REPLACE FUNCTION get_analytics_stc_performance()
RETURNS TABLE(product_type text, stc_bucket text, total bigint, wins bigint, losses bigint,
              win_rate numeric, avg_predicted numeric, avg_edge numeric)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT * FROM mv_stc_performance ORDER BY product_type, stc_bucket
$$;

CREATE OR REPLACE FUNCTION get_analytics_edge_realized_v2()
RETURNS TABLE(product_type text, edge_bucket text, total bigint, wins bigint, losses bigint,
              win_rate numeric, avg_edge numeric, cf_pnl_cents bigint)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT * FROM mv_edge_realized ORDER BY product_type, edge_bucket
$$;

-- Grant execute to anon role
GRANT EXECUTE ON FUNCTION get_analytics_rolling_calibration TO anon;
GRANT EXECUTE ON FUNCTION get_analytics_hourly_readiness TO anon;
GRANT EXECUTE ON FUNCTION get_analytics_stc_performance TO anon;
GRANT EXECUTE ON FUNCTION get_analytics_edge_realized_v2 TO anon;

-- ============================================================================
-- 10. Initial refresh of all views
-- ============================================================================

SELECT refresh_analytics_views();
