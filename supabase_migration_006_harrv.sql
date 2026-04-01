-- Migration 006: Create spx_harrv_shadow_signals table for HAR-RV shadow sync
-- Run in Supabase SQL Editor

CREATE TABLE IF NOT EXISTS spx_harrv_shadow_signals (
    id BIGINT PRIMARY KEY,
    ticker TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    asset TEXT NOT NULL DEFAULT 'SPX',
    evaluation_time TEXT NOT NULL,
    spot_price DOUBLE PRECISION,
    threshold DOUBLE PRECISION,
    seconds_to_close DOUBLE PRECISION,
    market_price INTEGER,
    best_bid INTEGER,
    best_ask INTEGER,

    -- HAR-RV fields
    rv_1h DOUBLE PRECISION,
    rv_1d DOUBLE PRECISION,
    rv_1w DOUBLE PRECISION,
    n_returns_1h INTEGER,
    n_returns_1d INTEGER,
    n_returns_1w INTEGER,
    rv_1d_imputed INTEGER DEFAULT 0,
    rv_1w_imputed INTEGER DEFAULT 0,
    rv_forecast DOUBLE PRECISION,
    sigma_forecast DOUBLE PRECISION,
    har_method TEXT,
    n_ols_obs INTEGER,

    -- Probability chain
    raw_prob DOUBLE PRECISION,
    scaled_prob DOUBLE PRECISION,
    temperature DOUBLE PRECISION,
    final_prob DOUBLE PRECISION,
    market_blend_w DOUBLE PRECISION,
    mkt_only_prob DOUBLE PRECISION,

    -- Edge
    edge DOUBLE PRECISION,
    fee_adjusted_edge DOUBLE PRECISION,
    est_fee_cents INTEGER,

    -- Gates
    gates_passed INTEGER DEFAULT 0,
    gate_failures TEXT,

    -- Sizing
    kelly_f DOUBLE PRECISION,
    shadow_contracts INTEGER,
    bankroll_cents INTEGER,

    -- EGARCH baseline (counterfactual)
    egarch_prob DOUBLE PRECISION,
    egarch_edge DOUBLE PRECISION,

    -- Settlement
    status TEXT NOT NULL DEFAULT 'pending',
    market_result TEXT,
    shadow_pnl_cents INTEGER,
    settled_time TEXT,

    -- NO-side columns
    no_price INTEGER,
    no_prob DOUBLE PRECISION,
    no_edge DOUBLE PRECISION,
    no_fee_edge DOUBLE PRECISION,
    no_kelly_f DOUBLE PRECISION,
    no_contracts INTEGER,
    no_gates_passed INTEGER,
    no_gate_failures TEXT,
    no_pnl_cents INTEGER
);

-- Index for watermark queries
CREATE INDEX IF NOT EXISTS idx_harrv_id ON spx_harrv_shadow_signals(id);
CREATE INDEX IF NOT EXISTS idx_harrv_status ON spx_harrv_shadow_signals(status);
