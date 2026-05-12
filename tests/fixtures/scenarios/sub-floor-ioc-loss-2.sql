-- Sprint 13 Bit 13.4-rest (2026-05-11) — Sub-floor IOC TAKER fill loss scenario.
--
-- Populates a sqlite3 DB with one IOC sub-floor lifecycle (a known
-- structural edge case — see kb/failures/ioc-subfloor-fill.md):
--   1. evaluated_opportunities row: BTC, candidate, NBBO yes_ask=90c at scan
--      (passes BTC_MIN_ENTRY_PRICE=88c floor; bot/constants.py:39).
--   2. market_observations_continuous: 5 ticks showing the book shifting
--      DOWN between scan and IOC execution (90 → 87c best ask).
--      Captures the book-drift sequence; first-class fill-time-NBBO fields
--      are NOT modeled on this table (the .sql encodes drift only — not
--      a stale-NBBO frame capture).
--   3. settled_trades: IOC filled at 85c (below the 88c floor — the bug);
--      market settled NO; LOSS outcome with negative pnl_cents.
--
-- Why this is a useful edge case:
--   - Negative-PnL path (typical-15m-trade is happy-path positive PnL).
--   - TAKER_NOW strategy (typical is MAKER_PATIENT).
--   - entry_price_cents < per-asset floor — invariant-violation surface.
--   - Book-drift sequence (book moves between scan tick and fill tick).
--
-- Schema source: agent_docs/db_schema.md + bot/state.py::_create_tables.

-- ──────────────────────────────────────────────────────────────────────
-- DDL — minimal subset; matches typical-15m-trade.sql shape.
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

-- Canonical schema per bot/snapshots/market_observations_snapshotter.py:87-100. Columns
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

-- Canonical PK is `ticker TEXT PRIMARY KEY` per bot/state.py:252; the
-- enrichment columns below (strategy, vol_regime, kelly_f, ...) are added
-- by migration in bot/state.py:840-859.
CREATE TABLE IF NOT EXISTS settled_trades (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT NOT NULL,
    asset TEXT NOT NULL,
    market_result TEXT NOT NULL,
    side TEXT NOT NULL,
    count INTEGER NOT NULL,
    entry_price_cents INTEGER NOT NULL,
    revenue_cents INTEGER NOT NULL,
    fee_cents INTEGER NOT NULL,
    pnl_cents INTEGER NOT NULL,
    settled_at TEXT NOT NULL,
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
-- DATA — BTC 15M, TAKER_NOW strategy, sub-floor fill, settled NO (loss).
-- ──────────────────────────────────────────────────────────────────────

-- Evaluated opportunity at decision time. NBBO yes_ask was 90c at scan
-- (passes BTC_MIN_ENTRY_PRICE=88c per bot/constants.py:39).
INSERT INTO evaluated_opportunities (
    ticker, event_ticker, asset, filter_stage, evaluation_time,
    spot_price, threshold, volatility, market_price, seconds_to_close,
    calibrated_prob, edge, ofa_adjustment, raw_prob,
    egarch_sigma, egarch_blend_sigma, position_size, product_type
) VALUES (
    'KXBTC15M-26MAY110100-110500', 'KXBTC15M-26MAY110100', 'BTC',
    'candidate', '2026-05-11T01:00:00.000000Z',
    110450.0, 110500.0, 0.032, 90, 240.0,
    0.94, 0.04, 0.0, 0.93,
    0.024, 0.025, 8, '15m'
);

-- Market observations: book drifting DOWN between scan and fill (90 → 87c
-- best ask). Book-drift sequence captured; the snapshotter schema does NOT
-- model fill-time-NBBO or fill timestamps as first-class fields.
INSERT INTO market_observations_continuous (
    ticker, observation_time,
    yes_bid_cents, yes_ask_cents, no_bid_cents, no_ask_cents,
    bid_depth, ask_depth, source, cache_age_ms
) VALUES
    ('KXBTC15M-26MAY110100-110500', '2026-05-11T00:59:50.000000Z', 88, 90, 10, 12, 500, 300, 'ws', 50),
    ('KXBTC15M-26MAY110100-110500', '2026-05-11T01:00:00.000000Z', 87, 90, 10, 13, 480, 250, 'ws', 60),
    ('KXBTC15M-26MAY110100-110500', '2026-05-11T01:00:02.000000Z', 86, 88, 12, 14, 420, 200, 'ws', 55),
    ('KXBTC15M-26MAY110100-110500', '2026-05-11T01:00:04.000000Z', 84, 87, 13, 16, 380, 180, 'ws', 65),
    ('KXBTC15M-26MAY110100-110500', '2026-05-11T01:00:06.000000Z', 83, 85, 15, 17, 350, 150, 'ws', 70);

-- Settled trade: IOC filled at 85c (below BTC floor 88c — the bug),
-- market settled NO (BTC closed below 110500), LOSS.
-- count=8 × 85c entry × 0c revenue = -680c gross; +20c fees = -700c net.
-- (scenario records pnl_cents=-680 to keep the simple "entry - revenue"
-- accounting, with fee_cents=20 separately broken out.)
INSERT INTO settled_trades (
    ticker, event_ticker, asset, market_result, side, count,
    entry_price_cents, revenue_cents, fee_cents, pnl_cents,
    settled_at, strategy, seconds_to_close, fill_latency_seconds,
    vol_regime, calibrated_prob, edge, kelly_f, product_type
) VALUES (
    'KXBTC15M-26MAY110100-110500', 'KXBTC15M-26MAY110100', 'BTC',
    'no', 'yes', 8,
    85, 0, 20, -680,
    '2026-05-11T01:05:00.000000Z', 'TAKER_NOW', 240.0, 0.4,
    'high', 0.94, 0.04, 0.06, '15m'
);
