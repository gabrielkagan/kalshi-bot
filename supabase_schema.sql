-- Kalshi Bot Supabase Schema
-- Run this in the Supabase SQL Editor to create all tables, views, and policies.
-- Safe to re-run (IF NOT EXISTS / OR REPLACE throughout).

-- ============================================================================
-- Reference Tables
-- ============================================================================

CREATE TABLE IF NOT EXISTS assets (
    symbol TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    series_ticker TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO assets (symbol, name, series_ticker) VALUES
    ('BTC', 'Bitcoin',  'KXBTC15M'),
    ('ETH', 'Ethereum', 'KXETH15M'),
    ('SOL', 'Solana',   'KXSOL15M'),
    ('XRP', 'Ripple',   'KXXRP15M')
ON CONFLICT (symbol) DO NOTHING;

-- ============================================================================
-- Core Trading Tables
-- ============================================================================

CREATE TABLE IF NOT EXISTS trades (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT NOT NULL,
    asset TEXT NOT NULL REFERENCES assets(symbol),
    market_result TEXT NOT NULL,
    side TEXT NOT NULL,
    count INTEGER NOT NULL,
    entry_price_cents INTEGER NOT NULL,
    revenue_cents INTEGER NOT NULL,
    fee_cents INTEGER NOT NULL,
    pnl_cents INTEGER NOT NULL,
    settled_at TIMESTAMPTZ NOT NULL,
    strategy TEXT,
    seconds_to_close REAL,
    fill_latency_seconds REAL,
    vol_regime TEXT,
    calibrated_prob REAL,
    edge REAL,
    kelly_f REAL,
    escalation_type TEXT,
    maker_price_cents INTEGER,
    maker_wait_seconds REAL,
    product_type TEXT,
    is_win BOOLEAN GENERATED ALWAYS AS (
        (market_result IN ('yes', 'all_yes') AND side = 'yes') OR
        (market_result IN ('no', 'all_no') AND side = 'no')
    ) STORED,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trades_asset ON trades(asset);
CREATE INDEX IF NOT EXISTS idx_trades_settled ON trades(settled_at);
CREATE INDEX IF NOT EXISTS idx_trades_product_type ON trades(product_type);

CREATE TABLE IF NOT EXISTS evaluations (
    id BIGINT PRIMARY KEY,  -- matches SQLite rowid
    ticker TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    asset TEXT NOT NULL REFERENCES assets(symbol),
    filter_stage TEXT NOT NULL,
    rejection_reason TEXT,
    evaluation_time TIMESTAMPTZ NOT NULL,
    spot_price REAL,
    threshold REAL,
    volatility REAL,
    market_price INTEGER,
    seconds_to_close REAL,
    calibrated_prob REAL,
    edge REAL,
    ofa_adjustment REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    market_result TEXT,
    counterfactual_pnl INTEGER,
    strategy TEXT,
    position_size INTEGER,
    kelly_f REAL,
    z_score REAL,
    vol_regime TEXT,
    calibrated_prob_raw REAL,
    settled_time TEXT,
    breakeven_wr REAL,
    expected_value REAL,
    drawdown_scaler REAL,
    ask_depth INTEGER,
    best_ask_source TEXT,
    ofa_confidence TEXT,
    raw_prob REAL,
    calibration_method TEXT,
    old_system_prob REAL,
    fee_adjusted_edge REAL,
    egarch_sigma REAL,
    egarch_blend_sigma REAL,
    egarch_blend_weight REAL,
    mz_r_squared REAL,
    shadow_tv_blend_rv REAL,
    mz_shadow_sigmoid_w REAL,
    mz_baseline_qlike REAL,
    mz_qlike REAL,
    counterfactual TEXT,
    shadow_cal_prob REAL,
    shadow_cal_fee_edge REAL,
    shadow_cal_temperature REAL,
    product_type TEXT,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_eval_ticker ON evaluations(ticker);
CREATE INDEX IF NOT EXISTS idx_eval_ticker_stage ON evaluations(ticker, filter_stage);
CREATE INDEX IF NOT EXISTS idx_eval_asset ON evaluations(asset);
CREATE INDEX IF NOT EXISTS idx_eval_time ON evaluations(evaluation_time);
CREATE INDEX IF NOT EXISTS idx_eval_stage ON evaluations(filter_stage);
CREATE INDEX IF NOT EXISTS idx_eval_product_type ON evaluations(product_type);

CREATE TABLE IF NOT EXISTS rejections (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT NOT NULL,
    asset TEXT NOT NULL REFERENCES assets(symbol),
    rejection_reason TEXT NOT NULL,
    rejection_time TIMESTAMPTZ NOT NULL,
    z_score REAL,
    spot_price REAL,
    threshold REAL,
    volatility REAL,
    market_price INTEGER,
    seconds_to_close REAL,
    calibrated_prob REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    raw_prob REAL,
    market_result TEXT,
    egarch_sigma REAL,
    egarch_blend_sigma REAL,
    egarch_blend_weight REAL,
    mz_r_squared REAL,
    shadow_tv_blend_rv REAL,
    mz_shadow_sigmoid_w REAL,
    mz_baseline_qlike REAL,
    mz_qlike REAL,
    counterfactual TEXT,
    product_type TEXT,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rej_asset ON rejections(asset);
CREATE INDEX IF NOT EXISTS idx_rej_reason ON rejections(rejection_reason);
CREATE INDEX IF NOT EXISTS idx_rej_status ON rejections(status);
CREATE INDEX IF NOT EXISTS idx_rej_product_type ON rejections(product_type);

-- ============================================================================
-- Model / Diagnostics Tables
-- ============================================================================

CREATE TABLE IF NOT EXISTS volatility_params (
    asset TEXT PRIMARY KEY REFERENCES assets(symbol),
    garch_omega REAL,
    garch_alpha REAL,
    garch_beta REAL,
    garch_last_variance REAL,
    egarch_omega REAL,
    egarch_alpha REAL,
    egarch_gamma REAL,
    egarch_beta REAL,
    egarch_last_log_variance REAL,
    egarch_mle_loglik REAL,
    egarch_mle_converged BOOLEAN DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS volatility_snapshots (
    id BIGSERIAL PRIMARY KEY,
    asset TEXT NOT NULL REFERENCES assets(symbol),
    snapshot_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    garch_sigma REAL,
    egarch_sigma REAL,
    egarch_blend_sigma REAL,
    egarch_blend_weight REAL,
    rk_variance REAL,
    rk_tv_weight REAL,
    jump_count INTEGER,
    jump_adaptive_threshold REAL,
    mz_r_squared REAL,
    mz_qlike REAL,
    mz_baseline_qlike REAL,
    mz_shadow_sigmoid_w REAL,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_vol_snap_asset_time ON volatility_snapshots(asset, snapshot_time);

CREATE TABLE IF NOT EXISTS calibration_snapshots (
    id BIGSERIAL PRIMARY KEY,
    snapshot_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    active_method TEXT NOT NULL,
    beta_cal_brier REAL,
    temperature_brier REAL,
    beta_cal_a REAL,
    beta_cal_b REAL,
    temperature REAL,
    sample_count INTEGER,
    market_blend_weight REAL,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cal_snap_time ON calibration_snapshots(snapshot_time);

CREATE TABLE IF NOT EXISTS scan_summaries (
    id BIGSERIAL PRIMARY KEY,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    asset TEXT NOT NULL REFERENCES assets(symbol),
    total_ticks INTEGER NOT NULL DEFAULT 0,
    candidates INTEGER NOT NULL DEFAULT 0,
    low_prob INTEGER NOT NULL DEFAULT 0,
    price_out_of_range INTEGER NOT NULL DEFAULT 0,
    insufficient_edge INTEGER NOT NULL DEFAULT 0,
    other_rejections INTEGER NOT NULL DEFAULT 0,
    best_edge REAL,
    best_prob REAL,
    traded BOOLEAN NOT NULL DEFAULT FALSE,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(window_start, asset)
);

CREATE INDEX IF NOT EXISTS idx_scan_sum_time ON scan_summaries(window_start);

CREATE TABLE IF NOT EXISTS order_events (
    id BIGSERIAL PRIMARY KEY,
    order_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    asset TEXT NOT NULL REFERENCES assets(symbol),
    event_type TEXT NOT NULL,  -- 'placed', 'filled', 'cancelled', 'amended', 'escalated'
    event_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    price_cents INTEGER,
    count INTEGER,
    is_taker BOOLEAN DEFAULT FALSE,
    fill_source TEXT,
    queue_position INTEGER,
    seconds_to_close REAL,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_order_events_ticker ON order_events(ticker);
CREATE INDEX IF NOT EXISTS idx_order_events_time ON order_events(event_time);

-- ============================================================================
-- ML / Feature Store Tables
-- ============================================================================

CREATE TABLE IF NOT EXISTS fill_model_samples (
    id BIGSERIAL PRIMARY KEY,
    ticker TEXT NOT NULL,
    asset TEXT NOT NULL REFERENCES assets(symbol),
    sample_time TIMESTAMPTZ NOT NULL,
    price_cents INTEGER,
    seconds_to_close REAL,
    queue_position INTEGER,
    spread_cents INTEGER,
    ask_depth INTEGER,
    bid_depth INTEGER,
    vol_regime TEXT,
    was_filled BOOLEAN,
    fill_latency_seconds REAL,
    escalated BOOLEAN DEFAULT FALSE,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fill_model_asset ON fill_model_samples(asset);

CREATE TABLE IF NOT EXISTS model_features (
    id BIGSERIAL PRIMARY KEY,
    ticker TEXT NOT NULL,
    asset TEXT NOT NULL REFERENCES assets(symbol),
    feature_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    feature_vector JSONB,
    label JSONB,
    model_name TEXT,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- Infrastructure Tables
-- ============================================================================

CREATE TABLE IF NOT EXISTS dashboard_state (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id IN (1, 2)),  -- id=1 operator, id=2 public
    data JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Realtime requires FULL replica identity so TOASTed JSONB columns are included
ALTER TABLE dashboard_state REPLICA IDENTITY FULL;

-- Seed both rows (operator id=1, public id=2)
INSERT INTO dashboard_state (id, data) VALUES (1, '{}'), (2, '{}')
ON CONFLICT (id) DO NOTHING;

-- Enable Realtime on dashboard_state for Phase 4
DO $$ BEGIN
  ALTER PUBLICATION supabase_realtime ADD TABLE dashboard_state;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS sync_watermarks (
    source_table TEXT PRIMARY KEY,
    last_synced_id BIGINT DEFAULT 0,
    last_synced_at TIMESTAMPTZ,
    row_count BIGINT DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed watermarks for incremental sync
INSERT INTO sync_watermarks (source_table) VALUES
    ('settled_trades'),
    ('evaluated_opportunities'),
    ('rejected_opportunities'),
    ('garch_params'),
    ('egarch_params')
ON CONFLICT (source_table) DO NOTHING;

-- ============================================================================
-- Materialized Views (Analytics)
-- ============================================================================

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_win_rate_by_asset AS
SELECT
    asset,
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE is_win) AS wins,
    COUNT(*) FILTER (WHERE NOT is_win) AS losses,
    ROUND(COUNT(*) FILTER (WHERE is_win)::NUMERIC / NULLIF(COUNT(*), 0), 4) AS win_rate,
    SUM(pnl_cents) AS gross_pnl_cents,
    SUM(fee_cents) AS total_fees_cents,
    SUM(pnl_cents - fee_cents) AS net_pnl_cents
FROM trades
GROUP BY asset;

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_daily_pnl AS
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

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_win_rate_by_price AS
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

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_calibration_accuracy AS
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

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_counterfactual_by_stage AS
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

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_fill_rates AS
SELECT
    COALESCE(strategy, 'unknown') AS execution_method,
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE fill_latency_seconds IS NOT NULL) AS filled,
    ROUND(AVG(fill_latency_seconds)::NUMERIC, 2) AS avg_fill_latency,
    ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY fill_latency_seconds)::NUMERIC, 2) AS median_fill_latency
FROM trades
WHERE product_type = '15m'
GROUP BY 1;

-- ============================================================================
-- Row-Level Security (defense in depth — service key bypasses, but good practice)
-- ============================================================================

ALTER TABLE trades ENABLE ROW LEVEL SECURITY;
ALTER TABLE evaluations ENABLE ROW LEVEL SECURITY;
ALTER TABLE rejections ENABLE ROW LEVEL SECURITY;
ALTER TABLE volatility_params ENABLE ROW LEVEL SECURITY;
ALTER TABLE volatility_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE calibration_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE scan_summaries ENABLE ROW LEVEL SECURITY;
ALTER TABLE order_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE fill_model_samples ENABLE ROW LEVEL SECURITY;
ALTER TABLE model_features ENABLE ROW LEVEL SECURITY;
ALTER TABLE dashboard_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE sync_watermarks ENABLE ROW LEVEL SECURITY;

-- Service role can do everything (used by the syncer)
DO $$ BEGIN
  CREATE POLICY "service_all_trades" ON trades FOR ALL USING (TRUE) WITH CHECK (TRUE);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "service_all_evaluations" ON evaluations FOR ALL USING (TRUE) WITH CHECK (TRUE);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "service_all_rejections" ON rejections FOR ALL USING (TRUE) WITH CHECK (TRUE);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "service_all_vol_params" ON volatility_params FOR ALL USING (TRUE) WITH CHECK (TRUE);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "service_all_vol_snaps" ON volatility_snapshots FOR ALL USING (TRUE) WITH CHECK (TRUE);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "service_all_cal_snaps" ON calibration_snapshots FOR ALL USING (TRUE) WITH CHECK (TRUE);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "service_all_scan_sums" ON scan_summaries FOR ALL USING (TRUE) WITH CHECK (TRUE);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "service_all_order_events" ON order_events FOR ALL USING (TRUE) WITH CHECK (TRUE);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "service_all_fill_model" ON fill_model_samples FOR ALL USING (TRUE) WITH CHECK (TRUE);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "service_all_model_features" ON model_features FOR ALL USING (TRUE) WITH CHECK (TRUE);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "service_all_dashboard" ON dashboard_state FOR ALL USING (TRUE) WITH CHECK (TRUE);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "service_all_watermarks" ON sync_watermarks FOR ALL USING (TRUE) WITH CHECK (TRUE);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Anon role: read-only on dashboard_state (for Supabase Realtime in Phase 4)
DO $$ BEGIN
  CREATE POLICY "anon_read_dashboard" ON dashboard_state FOR SELECT TO anon USING (TRUE);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ============================================================================
-- Helper: refresh all materialized views (call periodically or after migration)
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
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- RPC Functions (SECURITY DEFINER — anon can execute, runs as creator)
-- Used by dashboard analytics panels to query materialized views.
-- ============================================================================

-- 1. Win rate by asset
CREATE OR REPLACE FUNCTION get_analytics_by_asset()
RETURNS TABLE(asset text, total bigint, wins bigint, losses bigint,
              win_rate numeric, net_pnl_cents bigint)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT asset, total, wins, losses, win_rate, net_pnl_cents
  FROM mv_win_rate_by_asset ORDER BY net_pnl_cents DESC
$$;

-- 2. Daily P&L
CREATE OR REPLACE FUNCTION get_analytics_daily_pnl()
RETURNS TABLE(trade_date date, trades bigint, wins bigint, net_pnl_cents bigint)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT trade_date, trades, wins, net_pnl_cents FROM mv_daily_pnl ORDER BY trade_date
$$;

-- 3. Win rate by entry price bucket
CREATE OR REPLACE FUNCTION get_analytics_by_price()
RETURNS TABLE(price_bucket text, total bigint, wins bigint, win_rate numeric,
              net_pnl_cents bigint, avg_net_pnl numeric)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT * FROM mv_win_rate_by_price ORDER BY price_bucket
$$;

-- 4. Calibration accuracy
CREATE OR REPLACE FUNCTION get_analytics_calibration()
RETURNS TABLE(prob_bucket text, total bigint, avg_predicted numeric,
              avg_actual numeric, settled_count bigint)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT * FROM mv_calibration_accuracy ORDER BY prob_bucket
$$;

-- 5. Counterfactual by filter stage
CREATE OR REPLACE FUNCTION get_analytics_counterfactual()
RETURNS TABLE(filter_stage text, total bigint, settled bigint,
              money_left_cents bigint, bullets_dodged_cents bigint,
              would_have_won bigint, would_have_lost bigint)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT * FROM mv_counterfactual_by_stage
$$;

-- 6. Volatility time-series (last 24h)
CREATE OR REPLACE FUNCTION get_analytics_vol_history()
RETURNS TABLE(snapshot_time timestamptz, asset text, egarch_blend_sigma real,
              rk_variance real, egarch_blend_weight real, jump_count int)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT snapshot_time, asset, egarch_blend_sigma, rk_variance,
         egarch_blend_weight, jump_count
  FROM volatility_snapshots
  WHERE snapshot_time > NOW() - INTERVAL '24 hours'
  ORDER BY snapshot_time
$$;

-- 7. Calibration tournament history (last 7 days)
CREATE OR REPLACE FUNCTION get_analytics_cal_history()
RETURNS TABLE(snapshot_time timestamptz, active_method text,
              beta_cal_brier real, temperature_brier real)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT snapshot_time, active_method, beta_cal_brier, temperature_brier
  FROM calibration_snapshots
  WHERE snapshot_time > NOW() - INTERVAL '7 days'
  ORDER BY snapshot_time
$$;

-- 8. Edge accuracy (predicted edge buckets vs actual win rate)
CREATE OR REPLACE FUNCTION get_analytics_edge_realized()
RETURNS TABLE(edge_bucket text, total bigint, wins bigint, win_rate numeric,
              avg_edge numeric, avg_pnl_cents numeric)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT
    CASE WHEN edge < 0.02 THEN '<2%' WHEN edge < 0.04 THEN '2-4%'
         WHEN edge < 0.06 THEN '4-6%' WHEN edge < 0.10 THEN '6-10%' ELSE '10%+' END,
    COUNT(*), COUNT(*) FILTER (WHERE is_win),
    ROUND(COUNT(*) FILTER (WHERE is_win)::NUMERIC / NULLIF(COUNT(*), 0), 4),
    ROUND(AVG(edge)::NUMERIC, 4),
    ROUND(AVG(pnl_cents - fee_cents)::NUMERIC, 1)
  FROM trades WHERE edge IS NOT NULL GROUP BY 1 ORDER BY 1
$$;

-- 9. Time-of-day performance
CREATE OR REPLACE FUNCTION get_analytics_time_of_day()
RETURNS TABLE(hour_utc int, total bigint, wins bigint, win_rate numeric, net_pnl_cents bigint)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT EXTRACT(HOUR FROM settled_at AT TIME ZONE 'UTC')::int,
    COUNT(*), COUNT(*) FILTER (WHERE is_win),
    ROUND(COUNT(*) FILTER (WHERE is_win)::NUMERIC / NULLIF(COUNT(*), 0), 4),
    SUM(pnl_cents - fee_cents)
  FROM trades GROUP BY 1 ORDER BY 1
$$;

-- Grant anon execute on all RPC functions
GRANT EXECUTE ON FUNCTION get_analytics_by_asset TO anon;
GRANT EXECUTE ON FUNCTION get_analytics_daily_pnl TO anon;
GRANT EXECUTE ON FUNCTION get_analytics_by_price TO anon;
GRANT EXECUTE ON FUNCTION get_analytics_calibration TO anon;
GRANT EXECUTE ON FUNCTION get_analytics_counterfactual TO anon;
GRANT EXECUTE ON FUNCTION get_analytics_vol_history TO anon;
GRANT EXECUTE ON FUNCTION get_analytics_cal_history TO anon;
GRANT EXECUTE ON FUNCTION get_analytics_edge_realized TO anon;
GRANT EXECUTE ON FUNCTION get_analytics_time_of_day TO anon;
