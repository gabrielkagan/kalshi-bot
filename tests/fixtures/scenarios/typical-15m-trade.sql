-- Sprint 13 Bit 13.4 (2026-05-11) — Typical 15M trade lifecycle scenario.
--
-- Populates a sqlite3 DB with one complete trade lifecycle:
--   1. evaluated_opportunities row (filter_stage='candidate', sized)
--   2. market_observations_continuous baseline rows (5 ticks pre-decision)
--   3. settled_trades row (positive PnL outcome)
--
-- Intent: tests that need a "happy path" 15M sample row set without
-- spinning up the full bot runtime. Load via:
--     conn.executescript(open("tests/fixtures/scenarios/typical-15m-trade.sql").read())
--
-- Synthetic values throughout — no production data.
-- Schema source: agent_docs/db_schema.md.

-- ──────────────────────────────────────────────────────────────────────
-- DDL — minimal subset needed for the typical-15m-trade lifecycle.
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

CREATE TABLE IF NOT EXISTS market_observations_continuous (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    observation_time TEXT NOT NULL,
    yes_bid INTEGER,
    yes_ask INTEGER,
    no_bid INTEGER,
    no_ask INTEGER,
    last_price INTEGER,
    spot_price REAL
);

CREATE TABLE IF NOT EXISTS settled_trades (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    asset TEXT NOT NULL,
    market_result TEXT,
    side TEXT,
    count INTEGER,
    entry_price_cents INTEGER,
    revenue_cents INTEGER,
    fee_cents INTEGER,
    pnl_cents INTEGER,
    settled_at TEXT,
    strategy TEXT,
    seconds_to_close REAL,
    fill_latency_seconds REAL,
    vol_regime TEXT,
    calibrated_prob REAL,
    edge REAL,
    kelly_f REAL,
    product_type TEXT
);

-- ──────────────────────────────────────────────────────────────────────
-- DATA — one 15M trade lifecycle: BTC, MAKER_PATIENT strategy.
-- ──────────────────────────────────────────────────────────────────────

-- Evaluated_opportunity at decision time. Sized; passed all filters.
-- Synthetic ticker / event_ticker (KX prefix conventional).
INSERT INTO evaluated_opportunities (
    ticker, event_ticker, asset, filter_stage, evaluation_time,
    spot_price, threshold, volatility, market_price, seconds_to_close,
    calibrated_prob, edge, ofa_adjustment, raw_prob,
    egarch_sigma, egarch_blend_sigma, position_size, product_type
) VALUES (
    'KXBTC15M-26MAY110000-110500', 'KXBTC15M-26MAY110000', 'BTC',
    'candidate', '2026-05-11T00:00:00.000000Z',
    110000.0, 110500.0, 0.025, 35, 600.0,
    0.62, 0.27, 0.0, 0.60,
    0.018, 0.019, 12.0, '15m'
);

-- Market observations leading to the decision (5 ticks, 10s apart).
INSERT INTO market_observations_continuous (
    ticker, event_ticker, observation_time,
    yes_bid, yes_ask, no_bid, no_ask, last_price, spot_price
) VALUES
    ('KXBTC15M-26MAY110000-110500', 'KXBTC15M-26MAY110000', '2026-05-10T23:59:10.000000Z', 32, 36, 64, 68, 34, 109980.0),
    ('KXBTC15M-26MAY110000-110500', 'KXBTC15M-26MAY110000', '2026-05-10T23:59:20.000000Z', 33, 36, 64, 67, 35, 109995.0),
    ('KXBTC15M-26MAY110000-110500', 'KXBTC15M-26MAY110000', '2026-05-10T23:59:30.000000Z', 33, 35, 65, 67, 34, 110005.0),
    ('KXBTC15M-26MAY110000-110500', 'KXBTC15M-26MAY110000', '2026-05-10T23:59:40.000000Z', 34, 36, 64, 66, 35, 110010.0),
    ('KXBTC15M-26MAY110000-110500', 'KXBTC15M-26MAY110000', '2026-05-10T23:59:50.000000Z', 34, 35, 65, 66, 35, 110002.0);

-- Settled trade outcome. YES side, market YES (above strike), positive PnL.
-- Count=12 contracts × 65c profit = 780c gross; minus 35c fees = 745c net.
INSERT INTO settled_trades (
    ticker, event_ticker, asset, market_result, side, count,
    entry_price_cents, revenue_cents, fee_cents, pnl_cents,
    settled_at, strategy, seconds_to_close, fill_latency_seconds,
    vol_regime, calibrated_prob, edge, kelly_f, product_type
) VALUES (
    'KXBTC15M-26MAY110000-110500', 'KXBTC15M-26MAY110000', 'BTC',
    'yes', 'yes', 12,
    35, 1200, 35, 780,
    '2026-05-11T00:15:00.000000Z', 'MAKER_PATIENT', 600.0, 2.5,
    'normal', 0.62, 0.27, 0.10, '15m'
);
