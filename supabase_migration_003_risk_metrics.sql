-- Supabase Migration 003: Risk Metrics Framework
-- Run in Supabase SQL Editor. Safe to re-run.

-- ============================================================================
-- 1. Fix trades sync: add missing columns to trades table (already in schema,
--    but bot/snapshots/supabase_sync.py wasn't sending them — after code fix, backfill needed)
-- ============================================================================

-- Columns already exist from migration_001. This is just a reminder to run
-- the backfill after deploying the bot/snapshots/supabase_sync.py fix.

-- ============================================================================
-- 2. Materialized view: Daily risk rollup
-- ============================================================================

DROP MATERIALIZED VIEW IF EXISTS mv_daily_risk;
CREATE MATERIALIZED VIEW mv_daily_risk AS
WITH daily AS (
    SELECT
        DATE(settled_at AT TIME ZONE 'UTC') AS trade_date,
        COUNT(*) AS trades,
        COUNT(*) FILTER (WHERE is_win) AS wins,
        COUNT(*) FILTER (WHERE NOT is_win) AS losses,
        SUM(pnl_cents - fee_cents) AS net_pnl_cents,
        SUM(CASE WHEN NOT is_win THEN ABS(pnl_cents - fee_cents) ELSE 0 END) AS gross_loss_cents,
        SUM(CASE WHEN is_win THEN (pnl_cents - fee_cents) ELSE 0 END) AS gross_win_cents,
        MIN(pnl_cents - fee_cents) AS worst_trade_cents,
        MAX(pnl_cents - fee_cents) AS best_trade_cents,
        SUM(fee_cents) AS total_fees_cents,
        ROUND(AVG(fill_latency_seconds)::NUMERIC, 2) AS avg_fill_latency,
        ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY fill_latency_seconds)::NUMERIC, 2) AS median_fill_latency,
        ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY fill_latency_seconds)::NUMERIC, 2) AS p95_fill_latency,
        COUNT(*) FILTER (WHERE escalation_type IS NOT NULL) AS escalated_trades,
        COUNT(*) FILTER (WHERE strategy ILIKE '%taker%' OR escalation_type ILIKE '%taker%') AS taker_trades,
        COALESCE(product_type, '15m') AS product_type
    FROM trades
    GROUP BY DATE(settled_at AT TIME ZONE 'UTC'), COALESCE(product_type, '15m')
),
cumulative AS (
    SELECT
        *,
        SUM(net_pnl_cents) OVER (PARTITION BY product_type ORDER BY trade_date) AS cum_pnl_cents
    FROM daily
),
with_peak AS (
    SELECT
        *,
        MAX(cum_pnl_cents) OVER (PARTITION BY product_type ORDER BY trade_date) AS peak_cum_pnl_cents
    FROM cumulative
)
SELECT
    c.*,
    c.peak_cum_pnl_cents - c.cum_pnl_cents AS daily_drawdown_from_peak_cents,
    CASE WHEN c.peak_cum_pnl_cents > 0
         THEN ROUND(((c.peak_cum_pnl_cents - c.cum_pnl_cents)::NUMERIC / c.peak_cum_pnl_cents * 100), 2)
         ELSE 0 END AS daily_drawdown_from_peak_pct
FROM with_peak c
ORDER BY c.product_type, c.trade_date;

-- ============================================================================
-- 3. Materialized view: Tail risk metrics
-- ============================================================================

DROP MATERIALIZED VIEW IF EXISTS mv_tail_risk;
CREATE MATERIALIZED VIEW mv_tail_risk AS
WITH ranked AS (
    SELECT
        pnl_cents - fee_cents AS net_pnl,
        COALESCE(product_type, '15m') AS product_type,
        ROW_NUMBER() OVER (PARTITION BY COALESCE(product_type, '15m') ORDER BY (pnl_cents - fee_cents) ASC) AS rn,
        COUNT(*) OVER (PARTITION BY COALESCE(product_type, '15m')) AS total
    FROM trades
),
streaks AS (
    SELECT
        COALESCE(product_type, '15m') AS product_type,
        is_win,
        settled_at,
        ROW_NUMBER() OVER (PARTITION BY COALESCE(product_type, '15m') ORDER BY settled_at)
          - ROW_NUMBER() OVER (PARTITION BY COALESCE(product_type, '15m'), is_win ORDER BY settled_at) AS grp
    FROM trades
),
loss_streaks AS (
    SELECT
        product_type,
        COUNT(*) AS streak_length
    FROM streaks
    WHERE NOT is_win
    GROUP BY product_type, grp
)
SELECT
    r.product_type,
    MIN(r.net_pnl) AS worst_trade_cents,
    MAX(r.net_pnl) AS best_trade_cents,
    -- CVaR 5%: average of worst 5% of trades
    ROUND(AVG(CASE WHEN r.rn <= GREATEST(1, r.total * 0.05) THEN r.net_pnl END)::NUMERIC, 1) AS cvar_5pct_cents,
    -- CVaR 10%: average of worst 10%
    ROUND(AVG(CASE WHEN r.rn <= GREATEST(1, r.total * 0.10) THEN r.net_pnl END)::NUMERIC, 1) AS cvar_10pct_cents,
    r.total AS total_trades,
    -- Max consecutive losses
    COALESCE((SELECT MAX(streak_length) FROM loss_streaks ls WHERE ls.product_type = r.product_type), 0) AS max_consecutive_losses
FROM ranked r
GROUP BY r.product_type, r.total;

-- ============================================================================
-- 4. Materialized view: Execution quality
-- ============================================================================

DROP MATERIALIZED VIEW IF EXISTS mv_execution_quality;
CREATE MATERIALIZED VIEW mv_execution_quality AS
SELECT
    COALESCE(product_type, '15m') AS product_type,
    COUNT(*) AS total_trades,
    ROUND(AVG(fee_cents)::NUMERIC, 1) AS avg_fee_cents,
    ROUND(AVG(fill_latency_seconds)::NUMERIC, 2) AS avg_fill_latency_s,
    ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY fill_latency_seconds)::NUMERIC, 2) AS p50_fill_latency_s,
    ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY fill_latency_seconds)::NUMERIC, 2) AS p95_fill_latency_s,
    ROUND(PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY fill_latency_seconds)::NUMERIC, 2) AS p99_fill_latency_s,
    COUNT(*) FILTER (WHERE escalation_type IS NOT NULL) AS escalated_count,
    ROUND(COUNT(*) FILTER (WHERE escalation_type IS NOT NULL)::NUMERIC / NULLIF(COUNT(*), 0), 4) AS escalation_rate,
    -- Spread paid on escalated trades (maker_price_cents → entry_price_cents)
    ROUND(AVG(CASE WHEN maker_price_cents IS NOT NULL AND entry_price_cents != maker_price_cents
                   THEN ABS(entry_price_cents - maker_price_cents) END)::NUMERIC, 1) AS avg_spread_paid_cents,
    SUM(fee_cents) AS total_fees_cents,
    ROUND(SUM(fee_cents)::NUMERIC / NULLIF(SUM(ABS(pnl_cents - fee_cents)), 0), 4) AS fee_to_pnl_ratio
FROM trades
GROUP BY COALESCE(product_type, '15m');

-- ============================================================================
-- 5. Materialized view: Asset concentration
-- ============================================================================

DROP MATERIALIZED VIEW IF EXISTS mv_asset_concentration;
CREATE MATERIALIZED VIEW mv_asset_concentration AS
WITH totals AS (
    SELECT
        COALESCE(product_type, '15m') AS product_type,
        SUM(pnl_cents - fee_cents) AS total_net_pnl,
        COUNT(*) AS total_trades
    FROM trades
    GROUP BY COALESCE(product_type, '15m')
)
SELECT
    t.asset,
    COALESCE(t.product_type, '15m') AS product_type,
    COUNT(*) AS trades,
    ROUND(COUNT(*)::NUMERIC / NULLIF(tot.total_trades, 0), 4) AS trade_share,
    SUM(t.pnl_cents - t.fee_cents) AS net_pnl_cents,
    ROUND(SUM(t.pnl_cents - t.fee_cents)::NUMERIC / NULLIF(ABS(tot.total_net_pnl), 0), 4) AS pnl_share,
    COUNT(*) FILTER (WHERE t.is_win) AS wins,
    COUNT(*) FILTER (WHERE NOT t.is_win) AS losses,
    ROUND(COUNT(*) FILTER (WHERE t.is_win)::NUMERIC / NULLIF(COUNT(*), 0), 4) AS win_rate,
    MAX(t.count * (100 - t.entry_price_cents)) AS max_single_risk_cents
FROM trades t
JOIN totals tot ON COALESCE(t.product_type, '15m') = tot.product_type
GROUP BY t.asset, COALESCE(t.product_type, '15m'), tot.total_trades, tot.total_net_pnl
ORDER BY net_pnl_cents DESC;

-- ============================================================================
-- 6. Materialized view: Rolling 7d risk window
-- ============================================================================

DROP MATERIALIZED VIEW IF EXISTS mv_rolling_7d_risk;
CREATE MATERIALIZED VIEW mv_rolling_7d_risk AS
WITH daily AS (
    SELECT
        DATE(settled_at AT TIME ZONE 'UTC') AS trade_date,
        COALESCE(product_type, '15m') AS product_type,
        COUNT(*) AS trades,
        COUNT(*) FILTER (WHERE is_win) AS wins,
        SUM(pnl_cents - fee_cents) AS net_pnl_cents,
        MIN(pnl_cents - fee_cents) AS worst_trade_cents
    FROM trades
    GROUP BY 1, 2
),
rolling AS (
    SELECT
        trade_date,
        product_type,
        trades,
        wins,
        net_pnl_cents,
        worst_trade_cents,
        SUM(net_pnl_cents) OVER w AS rolling_7d_pnl,
        SUM(trades) OVER w AS rolling_7d_trades,
        SUM(wins) OVER w AS rolling_7d_wins,
        MIN(net_pnl_cents) OVER w AS rolling_7d_worst_day,
        MIN(worst_trade_cents) OVER w AS rolling_7d_worst_trade
    FROM daily
    WINDOW w AS (PARTITION BY product_type ORDER BY trade_date
                 ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)
)
SELECT * FROM rolling ORDER BY product_type, trade_date;

-- ============================================================================
-- 7. RPC functions for risk metrics
-- ============================================================================

-- Daily risk rollup
CREATE OR REPLACE FUNCTION get_risk_daily(p_product_type TEXT DEFAULT '15m')
RETURNS TABLE(
    trade_date date, trades bigint, wins bigint, losses bigint,
    net_pnl_cents bigint, gross_loss_cents bigint, gross_win_cents bigint,
    worst_trade_cents bigint, best_trade_cents bigint,
    total_fees_cents bigint, avg_fill_latency numeric,
    escalated_trades bigint, taker_trades bigint,
    cum_pnl_cents bigint, peak_cum_pnl_cents bigint,
    daily_drawdown_from_peak_cents bigint, daily_drawdown_from_peak_pct numeric
)
LANGUAGE sql SECURITY DEFINER AS $$
    SELECT trade_date, trades, wins, losses, net_pnl_cents,
           gross_loss_cents, gross_win_cents, worst_trade_cents, best_trade_cents,
           total_fees_cents, avg_fill_latency,
           escalated_trades, taker_trades,
           cum_pnl_cents, peak_cum_pnl_cents,
           daily_drawdown_from_peak_cents, daily_drawdown_from_peak_pct
    FROM mv_daily_risk
    WHERE product_type = p_product_type
    ORDER BY trade_date
$$;

-- Tail risk summary
CREATE OR REPLACE FUNCTION get_risk_tail(p_product_type TEXT DEFAULT '15m')
RETURNS TABLE(
    product_type text, worst_trade_cents bigint, best_trade_cents bigint,
    cvar_5pct_cents numeric, cvar_10pct_cents numeric,
    total_trades bigint, max_consecutive_losses bigint
)
LANGUAGE sql SECURITY DEFINER AS $$
    SELECT * FROM mv_tail_risk WHERE product_type = p_product_type
$$;

-- Execution quality
CREATE OR REPLACE FUNCTION get_risk_execution(p_product_type TEXT DEFAULT '15m')
RETURNS TABLE(
    product_type text, total_trades bigint, avg_fee_cents numeric,
    avg_fill_latency_s numeric, p50_fill_latency_s numeric,
    p95_fill_latency_s numeric, p99_fill_latency_s numeric,
    escalated_count bigint, escalation_rate numeric,
    avg_spread_paid_cents numeric, total_fees_cents bigint,
    fee_to_pnl_ratio numeric
)
LANGUAGE sql SECURITY DEFINER AS $$
    SELECT * FROM mv_execution_quality WHERE product_type = p_product_type
$$;

-- Asset concentration
CREATE OR REPLACE FUNCTION get_risk_concentration(p_product_type TEXT DEFAULT '15m')
RETURNS TABLE(
    asset text, product_type text, trades bigint, trade_share numeric,
    net_pnl_cents bigint, pnl_share numeric, wins bigint, losses bigint,
    win_rate numeric, max_single_risk_cents integer
)
LANGUAGE sql SECURITY DEFINER AS $$
    SELECT * FROM mv_asset_concentration
    WHERE product_type = p_product_type
    ORDER BY net_pnl_cents DESC
$$;

-- Rolling 7d risk
CREATE OR REPLACE FUNCTION get_risk_rolling_7d(p_product_type TEXT DEFAULT '15m')
RETURNS TABLE(
    trade_date date, trades bigint, wins bigint, net_pnl_cents bigint,
    worst_trade_cents bigint, rolling_7d_pnl bigint, rolling_7d_trades bigint,
    rolling_7d_wins bigint, rolling_7d_worst_day bigint, rolling_7d_worst_trade bigint
)
LANGUAGE sql SECURITY DEFINER AS $$
    SELECT trade_date, trades, wins, net_pnl_cents, worst_trade_cents,
           rolling_7d_pnl, rolling_7d_trades, rolling_7d_wins,
           rolling_7d_worst_day, rolling_7d_worst_trade
    FROM mv_rolling_7d_risk
    WHERE product_type = p_product_type
    ORDER BY trade_date
$$;

-- Current loss streak (computed live, not from MV)
CREATE OR REPLACE FUNCTION get_risk_current_streak()
RETURNS TABLE(product_type text, current_streak_type text, current_streak_length bigint)
LANGUAGE sql SECURITY DEFINER AS $$
    WITH ordered AS (
        SELECT
            COALESCE(product_type, '15m') AS product_type,
            is_win,
            ROW_NUMBER() OVER (PARTITION BY COALESCE(product_type, '15m') ORDER BY settled_at DESC) AS rn
        FROM trades
    ),
    streak AS (
        SELECT
            product_type,
            is_win,
            rn
        FROM ordered
        WHERE rn = 1
          OR (SELECT o2.is_win FROM ordered o2 WHERE o2.product_type = ordered.product_type AND o2.rn = 1) = ordered.is_win
    )
    SELECT
        product_type,
        CASE WHEN bool_and(is_win) THEN 'win' ELSE 'loss' END AS current_streak_type,
        COUNT(*) AS current_streak_length
    FROM streak
    GROUP BY product_type
$$;

-- Grant execute to anon role
GRANT EXECUTE ON FUNCTION get_risk_daily TO anon;
GRANT EXECUTE ON FUNCTION get_risk_tail TO anon;
GRANT EXECUTE ON FUNCTION get_risk_execution TO anon;
GRANT EXECUTE ON FUNCTION get_risk_concentration TO anon;
GRANT EXECUTE ON FUNCTION get_risk_rolling_7d TO anon;
GRANT EXECUTE ON FUNCTION get_risk_current_streak TO anon;

-- ============================================================================
-- 8. Update refresh function to include new risk views
-- ============================================================================

CREATE OR REPLACE FUNCTION refresh_analytics_views()
RETURNS VOID AS $$
BEGIN
    -- Existing analytics views
    REFRESH MATERIALIZED VIEW mv_win_rate_by_asset;
    REFRESH MATERIALIZED VIEW mv_daily_pnl;
    REFRESH MATERIALIZED VIEW mv_win_rate_by_price;
    REFRESH MATERIALIZED VIEW mv_calibration_accuracy;
    REFRESH MATERIALIZED VIEW mv_counterfactual_by_stage;
    REFRESH MATERIALIZED VIEW mv_fill_rates;
    BEGIN REFRESH MATERIALIZED VIEW mv_hourly_calibration_drift; EXCEPTION WHEN undefined_table THEN NULL; END;
    BEGIN REFRESH MATERIALIZED VIEW mv_hourly_window_correlation; EXCEPTION WHEN undefined_table THEN NULL; END;
    BEGIN REFRESH MATERIALIZED VIEW mv_edge_decay; EXCEPTION WHEN undefined_table THEN NULL; END;
    BEGIN REFRESH MATERIALIZED VIEW mv_rolling_calibration; EXCEPTION WHEN undefined_table THEN NULL; END;
    BEGIN REFRESH MATERIALIZED VIEW mv_hourly_readiness; EXCEPTION WHEN undefined_table THEN NULL; END;
    BEGIN REFRESH MATERIALIZED VIEW mv_stc_performance; EXCEPTION WHEN undefined_table THEN NULL; END;
    BEGIN REFRESH MATERIALIZED VIEW mv_edge_realized; EXCEPTION WHEN undefined_table THEN NULL; END;
    -- New risk views
    BEGIN REFRESH MATERIALIZED VIEW mv_daily_risk; EXCEPTION WHEN undefined_table THEN NULL; END;
    BEGIN REFRESH MATERIALIZED VIEW mv_tail_risk; EXCEPTION WHEN undefined_table THEN NULL; END;
    BEGIN REFRESH MATERIALIZED VIEW mv_execution_quality; EXCEPTION WHEN undefined_table THEN NULL; END;
    BEGIN REFRESH MATERIALIZED VIEW mv_asset_concentration; EXCEPTION WHEN undefined_table THEN NULL; END;
    BEGIN REFRESH MATERIALIZED VIEW mv_rolling_7d_risk; EXCEPTION WHEN undefined_table THEN NULL; END;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- 9. Initial refresh
-- ============================================================================

SELECT refresh_analytics_views();
