-- Migration 004: Add product_type='15m' filter to materialized views
-- All 5 views were mixing hourly/weather/sports data into 15M analytics
-- Run manually via Supabase SQL Editor

-- 1. mv_daily_pnl — was including hourly trades (Feb 28 showed 62 trades when 15M was only 29)
DROP MATERIALIZED VIEW IF EXISTS mv_daily_pnl;
CREATE MATERIALIZED VIEW mv_daily_pnl AS
SELECT
    DATE(settled_at AT TIME ZONE 'UTC') AS trade_date,
    COUNT(*) AS trades,
    COUNT(*) FILTER (WHERE is_win) AS wins,
    SUM(pnl_cents - fee_cents) AS net_pnl_cents,
    SUM(revenue_cents) AS revenue_cents,
    SUM(fee_cents) AS fee_cents
FROM trades
WHERE product_type = '15m'
GROUP BY DATE(settled_at AT TIME ZONE 'UTC')
ORDER BY trade_date;

-- 2. mv_win_rate_by_price
DROP MATERIALIZED VIEW IF EXISTS mv_win_rate_by_price;
CREATE MATERIALIZED VIEW mv_win_rate_by_price AS
SELECT
    CASE
        WHEN entry_price_cents BETWEEN 86 AND 89 THEN '86-89c'
        WHEN entry_price_cents BETWEEN 90 AND 93 THEN '90-93c'
        WHEN entry_price_cents BETWEEN 94 AND 96 THEN '94-96c'
        WHEN entry_price_cents BETWEEN 97 AND 99 THEN '97-99c'
        ELSE 'other'
    END AS price_bucket,
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE is_win) AS wins,
    ROUND(COUNT(*) FILTER (WHERE is_win)::NUMERIC / NULLIF(COUNT(*), 0), 4) AS win_rate,
    SUM(pnl_cents - fee_cents) AS net_pnl_cents,
    ROUND(AVG(pnl_cents - fee_cents)::NUMERIC, 1) AS avg_net_pnl
FROM trades
WHERE product_type = '15m'
GROUP BY 1
ORDER BY 1;

-- 3. mv_calibration_accuracy
DROP MATERIALIZED VIEW IF EXISTS mv_calibration_accuracy;
CREATE MATERIALIZED VIEW mv_calibration_accuracy AS
SELECT
    CASE
        WHEN calibrated_prob < 0.80 THEN '<80%'
        WHEN calibrated_prob < 0.85 THEN '80-85%'
        WHEN calibrated_prob < 0.90 THEN '85-90%'
        WHEN calibrated_prob < 0.95 THEN '90-95%'
        ELSE '95%+'
    END AS prob_bucket,
    COUNT(*) AS total,
    ROUND(AVG(calibrated_prob)::NUMERIC, 4) AS avg_predicted,
    ROUND(AVG(CASE WHEN market_result IS NOT NULL THEN
        CASE WHEN (market_result IN ('yes','all_yes') AND filter_stage = 'candidate') THEN 1.0 ELSE 0.0 END
    END)::NUMERIC, 4) AS avg_actual,
    COUNT(*) FILTER (WHERE market_result IS NOT NULL) AS settled_count
FROM evaluations
WHERE calibrated_prob IS NOT NULL
  AND filter_stage IN ('candidate', 'observation_trade')
  AND product_type = '15m'
GROUP BY 1
ORDER BY 1;

-- 4. mv_counterfactual_by_stage
DROP MATERIALIZED VIEW IF EXISTS mv_counterfactual_by_stage;
CREATE MATERIALIZED VIEW mv_counterfactual_by_stage AS
SELECT
    filter_stage,
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE status = 'settled') AS settled,
    SUM(CASE WHEN counterfactual_pnl > 0 THEN counterfactual_pnl ELSE 0 END) AS money_left_cents,
    SUM(CASE WHEN counterfactual_pnl < 0 THEN ABS(counterfactual_pnl) ELSE 0 END) AS bullets_dodged_cents,
    COUNT(*) FILTER (WHERE counterfactual_pnl > 0) AS would_have_won,
    COUNT(*) FILTER (WHERE counterfactual_pnl < 0) AS would_have_lost
FROM evaluations
WHERE filter_stage NOT IN ('candidate', 'observation_trade')
  AND product_type = '15m'
GROUP BY filter_stage
ORDER BY money_left_cents DESC;

-- 5. mv_fill_rates
DROP MATERIALIZED VIEW IF EXISTS mv_fill_rates;
CREATE MATERIALIZED VIEW mv_fill_rates AS
SELECT
    COALESCE(strategy, 'unknown') AS execution_method,
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE fill_latency_seconds IS NOT NULL) AS filled,
    ROUND(AVG(fill_latency_seconds)::NUMERIC, 2) AS avg_fill_latency,
    ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY fill_latency_seconds)::NUMERIC, 2) AS median_fill_latency
FROM trades
WHERE product_type = '15m'
GROUP BY 1;

-- Refresh all views
REFRESH MATERIALIZED VIEW mv_daily_pnl;
REFRESH MATERIALIZED VIEW mv_win_rate_by_price;
REFRESH MATERIALIZED VIEW mv_calibration_accuracy;
REFRESH MATERIALIZED VIEW mv_counterfactual_by_stage;
REFRESH MATERIALIZED VIEW mv_fill_rates;
