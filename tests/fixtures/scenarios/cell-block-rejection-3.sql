-- Sprint 13 Bit 13.4-rest (2026-05-11) — Cell-block rejection + shadow scenario.
--
-- Populates a sqlite3 DB with one rejected opportunity (cell-block path)
-- AND its shadow-strategy sibling row:
--   1. evaluated_opportunities row: SOL, filter_stage='SOL_BLEED_V2_88_93C_2_5MIN',
--      rejection_reason populated (NOT a 'candidate').
--   2. rejected_opportunities row: same opportunity in the rejection table.
--   3. market_observations_continuous: 3 ticks of pre-decision book state.
--   4. fifteenm_shadow_signals row: A2 shadow approach captured the same
--      signal (since live path was blocked by cell-block).
--   5. NO settled_trades row — that's the point: the rejection prevented
--      a live position from being taken.
--
-- Why this is a useful edge case:
--   - Rejection-path schema (typical-15m + sub-floor-ioc cover the
--     candidate→fill→settled path; this covers candidate→rejected→shadow).
--   - Cell-block filter_stage string literal exercise — load-bearing per
--     bot/CLAUDE.md, regression-sensitive (SOL_BLEED_V2 ship May 10).
--     Constant: SOL_BLEED_V2_BLOCK_FILTER_STAGE = "SOL_BLEED_V2_88_93C_2_5MIN"
--     (bot/constants.py:229; emit site bot/scanner/__init__.py:6580).
--   - Shadow-table coverage (fifteenm_shadow_signals is a sibling table
--     consumed by /shadow + /variant-status skills).
--   - No-settled-trades arm — the absence is part of the contract.
--
-- Schema source: agent_docs/db_schema.md + bot/state.py::_create_tables
--                + bot/shadows/fifteenm_shadow.py::_create_tables.

-- ──────────────────────────────────────────────────────────────────────
-- DDL — adds rejected_opportunities + fifteenm_shadow_signals on top of
-- the typical scenario shape.
-- ──────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS evaluated_opportunities (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    asset TEXT NOT NULL,
    filter_stage TEXT NOT NULL,
    rejection_reason TEXT,
    evaluation_time TEXT NOT NULL,
    spot_price REAL,
    threshold REAL,
    volatility REAL,
    market_price INTEGER,
    seconds_to_close REAL,
    calibrated_prob REAL,
    edge REAL,
    ofa_adjustment REAL,
    raw_prob REAL,
    egarch_sigma REAL,
    egarch_blend_sigma REAL,
    position_size REAL,
    product_type TEXT
);

-- Canonical PK is `ticker TEXT PRIMARY KEY` per bot/state.py:308.
CREATE TABLE IF NOT EXISTS rejected_opportunities (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT NOT NULL,
    asset TEXT NOT NULL,
    rejection_reason TEXT NOT NULL,
    rejection_time TEXT NOT NULL,
    z_score REAL,
    spot_price REAL,
    threshold REAL,
    volatility REAL,
    market_price INTEGER,
    seconds_to_close REAL,
    calibrated_prob REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    product_type TEXT
);

-- Canonical schema per market_observations_snapshotter.py:87-100. Columns
-- are `yes_bid_cents`/`yes_ask_cents`/...; the table has NO `event_ticker`,
-- `last_price`, or `spot_price` columns (snapshotter writes NBBO+depth only).
CREATE TABLE IF NOT EXISTS market_observations_continuous (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    observation_time TEXT NOT NULL,
    yes_bid_cents INTEGER,
    yes_ask_cents INTEGER,
    no_bid_cents INTEGER,
    no_ask_cents INTEGER,
    bid_depth INTEGER,
    ask_depth INTEGER,
    source TEXT NOT NULL,
    cache_age_ms INTEGER
);

-- Canonical schema per bot/shadows/fifteenm_shadow.py:943-994. Per-approach
-- columns are PREFIXED (a1_*, a2_*); there is NO scalar `approach` /
-- `signal_time` / `would_size` / `counterfactual_pnl` / `calibrated_prob`
-- column on this table.
CREATE TABLE IF NOT EXISTS fifteenm_shadow_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    asset TEXT NOT NULL,
    evaluation_time TEXT NOT NULL,
    -- Market state
    spot_price REAL,
    threshold REAL,
    seconds_to_close REAL,
    market_price INTEGER,
    best_bid INTEGER,
    best_ask INTEGER,
    -- Live baseline
    live_prob REAL,
    live_edge REAL,
    live_fee_edge REAL,
    -- Approach 1: Recalibrated EGARCH
    a1_raw_prob REAL,
    a1_temperature REAL,
    a1_temp_prob REAL,
    a1_blend_w REAL,
    a1_final_prob REAL,
    a1_edge REAL,
    a1_fee_edge REAL,
    a1_edge_band_blocked INTEGER,
    a1_debiased_prob REAL,
    a1_kelly_f REAL,
    a1_contracts INTEGER,
    a1_gates_passed INTEGER,
    a1_gate_failures TEXT,
    -- Approach 2: LightGBM
    a2_raw_prob REAL,
    a2_calibrated_prob REAL,
    a2_edge REAL,
    a2_fee_edge REAL,
    a2_kelly_f REAL,
    a2_contracts INTEGER,
    a2_model_version TEXT,
    a2_gates_passed INTEGER,
    a2_gate_failures TEXT,
    -- Market-only baseline
    market_only_prob REAL,
    -- Settlement
    status TEXT NOT NULL DEFAULT 'pending',
    market_result TEXT,
    live_pnl_cents INTEGER,
    a1_pnl_cents INTEGER,
    a2_pnl_cents INTEGER,
    market_only_pnl_cents INTEGER,
    settled_time TEXT
);

-- ──────────────────────────────────────────────────────────────────────
-- DATA — SOL 15M, 88c × 240s × TAKER_NOW; bleed-cell-block fires;
-- A2 shadow captures the would-be signal; nothing settles.
-- ──────────────────────────────────────────────────────────────────────

-- Evaluated opportunity, but filter_stage is the cell-block, not 'candidate'.
-- filter_stage literal matches bot/constants.py:229
-- SOL_BLEED_V2_BLOCK_FILTER_STAGE = "SOL_BLEED_V2_88_93C_2_5MIN".
INSERT INTO evaluated_opportunities (
    ticker, event_ticker, asset, filter_stage, rejection_reason,
    evaluation_time, spot_price, threshold, volatility, market_price,
    seconds_to_close, calibrated_prob, edge, ofa_adjustment, raw_prob,
    egarch_sigma, egarch_blend_sigma, position_size, product_type
) VALUES (
    'KXSOL15M-26MAY110200-148000', 'KXSOL15M-26MAY110200', 'SOL',
    'SOL_BLEED_V2_88_93C_2_5MIN', 'SOL × 88-93c × 121-300s × TAKER_NOW historically net-negative',
    '2026-05-11T02:00:00.000000Z', 148.05, 148.00, 0.041, 88,
    240.0, 0.91, 0.03, 0.0, 0.90,
    0.030, 0.031, 0, '15m'
);

-- Rejection-table mirror row (some downstream readers use this table).
-- status='pending' matches canonical default (bot/state.py:320).
INSERT INTO rejected_opportunities (
    ticker, event_ticker, asset, rejection_reason, rejection_time,
    z_score, spot_price, threshold, volatility, market_price,
    seconds_to_close, calibrated_prob, status, product_type
) VALUES (
    'KXSOL15M-26MAY110200-148000', 'KXSOL15M-26MAY110200', 'SOL',
    'SOL_BLEED_V2 cell-block: SOL × 88-93c × 121-300s × TAKER_NOW',
    '2026-05-11T02:00:00.000000Z',
    0.12, 148.05, 148.00, 0.041, 88,
    240.0, 0.91, 'pending', '15m'
);

-- Pre-decision market state (3 ticks, stable book at 87-88c).
INSERT INTO market_observations_continuous (
    ticker, observation_time,
    yes_bid_cents, yes_ask_cents, no_bid_cents, no_ask_cents,
    bid_depth, ask_depth, source, cache_age_ms
) VALUES
    ('KXSOL15M-26MAY110200-148000', '2026-05-11T01:59:30.000000Z', 86, 88, 12, 14, 200, 180, 'ws', 80),
    ('KXSOL15M-26MAY110200-148000', '2026-05-11T01:59:45.000000Z', 87, 88, 12, 13, 220, 160, 'ws', 90),
    ('KXSOL15M-26MAY110200-148000', '2026-05-11T01:59:58.000000Z', 87, 89, 11, 13, 240, 150, 'ws', 75);

-- Shadow-strategy A2 captures the would-be signal (live path was blocked
-- by cell-block, so this is counterfactual-only). Per-approach columns
-- are PREFIXED on this table — there is no scalar `approach` column.
-- Hypothetical settle: market closed YES (above 148.00), so the A2
-- counterfactual would have made money — a2_pnl_cents integer cents.
-- Settled-state row (status='settled', market_result='yes') so the
-- a2_pnl_cents column is meaningful.
INSERT INTO fifteenm_shadow_signals (
    ticker, event_ticker, asset, evaluation_time,
    spot_price, threshold, seconds_to_close, market_price,
    best_bid, best_ask,
    live_prob, live_edge, live_fee_edge,
    a2_raw_prob, a2_calibrated_prob, a2_edge, a2_fee_edge,
    a2_kelly_f, a2_contracts, a2_model_version,
    a2_gates_passed, a2_gate_failures,
    status, market_result, a2_pnl_cents, settled_time
) VALUES (
    'KXSOL15M-26MAY110200-148000', 'KXSOL15M-26MAY110200', 'SOL',
    '2026-05-11T02:00:00.000000Z',
    148.05, 148.00, 240.0, 88,
    87, 89,
    0.91, 0.03, 0.025,
    0.90, 0.91, 0.03, 0.025,
    0.04, 4, 'lgbm-v1.0.0',
    1, NULL,
    'settled', 'yes', 36, '2026-05-11T02:04:00.000000Z'
);
