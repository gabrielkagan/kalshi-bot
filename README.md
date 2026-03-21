# Kalshi Crypto Trading Bot

Automated trading platform for Kalshi prediction markets. Core engine trades 15-minute cryptocurrency contracts (BTC, ETH, SOL, XRP) using microstructure-aware volatility models. Expanding into S&P 500 intraday, daily weather temperature (19 US cities), and live sports outcomes (28 leagues) --- all in shadow mode collecting calibration data.

## How It Works

```
Coinbase (1s prices) ──┐
Kraken ────────────────┤                                          ┌─ Maker order (post_only)
Bybit ─────────────────┼──→ Volatility ──→ Probability ──→ Edge ──┤
Deribit DVOL ──────────┤     Engine          Engine      Filter   └─ Taker escalation (amend/IOC)
CoinGlass funding ─────┘
```

Every second, the bot scans all active 15-minute windows across all four assets and executes when the fee-adjusted edge exceeds a price-dependent minimum (0.25% at 80c up to 2.0% at 97c+).

## Architecture

### Volatility Engine

The bot doesn't use a single volatility number --- it blends three Realized Kernel estimators (Barndorff-Nielsen 2008, Parzen flat-top kernel) with data-adaptive bandwidth selection:

| Estimator | Base Weight | Purpose |
|-----------|-------------|---------|
| 1-min realized kernel | 50% | Current microstructure |
| 5-min bipower variation | 30% | Jump-robust medium-term vol |
| 15-min realized kernel | 20% | Window-level baseline |

On top of this:

- **Adaptive RK bandwidth (H\*)** --- bandwidth auto-tunes from the noise-to-signal ratio, producing tighter estimates in calm periods and wider smoothing during noisy periods
- **Mincer-Zarnowitz R2-weighted EGARCH blending** --- an EGARCH(1,1) model with Student-t innovations runs live, blending with RK vol weighted by MZ regression R2 (typically 0.42--0.61)
- **Deribit DVOL integration** --- when IV diverges from RV by >50%, the engine shifts toward implied vol using inverse-variance weighting. For SOL/XRP (no direct DVOL), it scales BTC DVOL by a rolling cross-asset beta (60-return lookback, clamped 0.5--3.0)
- **Adaptive jump detection** --- percentile-based per-asset thresholds (replaced fixed 3-sigma); EWMA variance tracking with tiered response scaling by severity

### Probability Model

Converts the volatility estimate into a settlement probability:

1. Compute z-score: distance from current price to strike, normalized by estimated vol
2. Map through per-asset Normal Inverse Gaussian (NIG) CDF --- captures both heavy tails and asymmetry unique to each crypto; falls back to Student-t(df=4) if NIG unavailable
3. Data-driven calibration via CalibrationEngine --- progresses from fixed logistic -> Platt Scaling -> Beta Calibration -> BLR as data accumulates
4. Dynamic probability cap: bypassed when learned calibration is active (uses 0.999 safety ceiling); cap schedule only applies during startup before training
5. Market-price blending: 60% model / 40% market-implied probability

Safety rails refuse to trade if: the model says >90% but the market is below 75c, or |z-score| > 25.

### Cross-Exchange Intelligence

Three WebSocket feeds (Kraken, Bybit, Binance) run concurrently via `CrossExchangeFeed` to detect directional signals before they show up on Kalshi. Binance is geo-blocked (HTTP 451) on the production VPS but the feed reconnects silently; Kraken and Bybit provide the primary cross-exchange signal.

- **Lead/lag consensus** --- if 3+ exchanges move >0.3% in the same direction, the probability gets a +2pp boost
- **Single-exchange lead** --- a >0.2% move on one exchange adds +1pp
- **Funding rate signal** --- extreme funding (>0.05%/8h via CoinGlass) reduces probability by up to 1.5pp as a contrarian dampener

Total cross-exchange adjustment is capped at +/-3pp.

### Execution Strategy

The bot enters as a maker by default and escalates to taker based on time pressure. Per-asset overrides exist (SOL always enters as taker). A diagnostic strategy engine classifies each opportunity (WAIT, MAKER_PATIENT, MAKER_AGGRESSIVE, TAKER_NOW) for logging, but the actual execution path is:

1. Place maker order with `post_only=True` ($0 maker fee)
2. Monitor for fills via Kalshi WebSocket (zero API cost, REST fallback)
3. Poll queue position every ~5s for escalation timing
4. If unfilled after wait period, escalate to taker:
   - STC >= 180s: 15s wait (BTC: 7s override)
   - STC 120-180s: 7s wait
   - STC 60-120s: 5s wait
   - Attempt `amend_order()` to convert to taker price in-place
   - Fallback: cancel + IOC (`time_in_force="immediate_or_cancel"`) taker order
5. Three-tier post_only rejection handler: normal -> degraded -> taker IOC after 3+ rejections
6. Direct taker: when seconds-to-close < 180s, skip maker and submit IOC taker directly
7. **SOL taker-first**: SOL bypasses maker entirely (`SOL_TAKER_FIRST=True`), goes direct IOC at all STC
8. **Decided contract overlay**: T1 (z <= -5, 93c+), T1B (z <= -4, 95c+), and T2 (z <= -3, 93-96c) route to direct taker for near-certain settlements. Fixed 12.5% bankroll risk per signal. T1 and T1B are live; T2 remains shadow-only.

### Position Sizing

Full Kelly sizing with edge-tiered risk fractions and drawdown scaling:

| Fee-Adjusted Edge | Risk Fraction |
|-------------------|---------------|
| >= 4% | 25% of bankroll |
| >= 2.5% | 20% of bankroll |
| >= 1.8% | 15% of bankroll |
| >= 1.2% | 10% of bankroll |
| >= 0.9% | 7% of bankroll |
| >= 0.7% | 5% of bankroll |
| >= 0.5% | 3% of bankroll |
| >= 0.25% | 2% of bankroll |

- 15M uses full Kelly (kelly_fraction=1.0); hourly/weather use quarter-Kelly (0.25); SPX uses eighth-Kelly (0.125)
- Safety ceiling: max 25% of bankroll at risk per trade
- Low-STC sizing cap: halve position below 100s STC
- At 85% of peak balance: halve position sizes
- At 75% of peak balance: quarter position sizes
- At 65% of peak balance: halt trading entirely
- Price improvement addon (live) + dip addon (shadow) for existing positions
- Can trade multiple assets per 15-minute window

### Live Trading Configuration

| Asset | Min Entry Price | Notes |
|-------|----------------|-------|
| BTC | 89c | 86-88c below taker breakeven |
| ETH | 80c | Price shadow data: 80-85c 89.7% WR |
| SOL | 80c (global floor) | Taker-first execution |
| XRP | 92c | PnL negative below 90c; 12% max risk cap |

- **STC window**: full scan 0-900s, live trading 0-600s, shadow observation 600-900s
- **Price-dependent edge thresholds**: 80-90c → 0.25%, 91-92c → 0.35%, 93-94c → 0.9%, 95-96c → 1.25%, 97-99c → 2.0%

### State & Persistence

SQLite (WAL mode) stores positions, pending orders, settled trades, GARCH parameters, and rejected/evaluated opportunities. On startup the bot reconciles local state against the Kalshi API --- API always wins.

## Data Sources

| Source | Transport | Data | Frequency |
|--------|-----------|------|-----------|
| Coinbase | WebSocket | BTC, ETH, SOL, XRP spot prices | 1s snapshots (300-sample buffer) |
| Kraken | WebSocket | Spot prices for lead/lag detection | Real-time |
| Bybit | WebSocket | Spot prices for lead/lag detection | Real-time |
| Binance | WebSocket | Spot prices (geo-blocked on VPS) | Real-time (when reachable) |
| Deribit | REST | DVOL implied volatility index | Every 60s (120s cache) |
| CoinGlass | REST | Funding rates | Every 10min (100 calls/day budget) |
| Kalshi | REST + WebSocket | Markets, orderbooks, positions, settlements, fills | 1s scan loop + real-time WS fills/orderbook |

## Live Stats

<!-- Auto-updated by scripts/generate_whitepaper_stats.py from VPS state.db -->

Stats are generated by `scripts/generate_whitepaper_stats.py` from the production database and rendered via `README.template.md`. The observation period began 2026-02-22 and is ongoing.

## Shadow / Observation Pipelines

19+ shadow strategies across 5 verticals, all collecting data for potential promotion:

| Vertical | Status | Key Shadows |
|----------|--------|-------------|
| 15M crypto | **LIVE** (BTC/ETH/SOL/XRP) | STC 600-900s, price 70-85c, decided contract (dc_shadow_t1b_93c, dc_shadow_t2_z25, dc_shadow_t2_90c, dc_shadow_t2_90c_xrp, dc_shadow_t2_z2, dc_shadow_no_side), relaxed edge, weekend discount (LIVE on Sat/Sun 89c+ STC<=600s; sub-89c/STC>600s shadow), overnight LP, dip addon, XRP low-price, shadow engine A1-A4 |
| Hourly crypto | Observation | 11+ config variants (C through M), alt shadows (HAR-RV, market-making sim) |
| SPX hourly | Observation | Reverted from brief live stint (Polygon 403 broke vol engine) |
| Weather | Observation | 19-city NWP ensemble (GFS+ECMWF, 82 members), NO-side pipeline wired |
| Sports | Observation | 28 leagues, basketball best group (69.2% WR), SPRT still collecting |

## Setup

### Prerequisites

- Python 3
- Kalshi API key + RSA private key (.pem)

### Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Dependencies: `requests`, `websocket-client`, `websockets`, `cryptography`, `scipy`, `numpy`, `lightgbm`, `scikit-learn`

### Configure

```bash
cp .env.example .env
```

Required:
- `KALSHI_API_KEY` --- your Kalshi API key ID
- `KALSHI_PRIVATE_KEY_PATH` --- path to your RSA private key PEM file

Optional:
- `KALSHI_ENV=production` --- trade on live exchange (defaults to demo)
- `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` --- enable real-time dashboard (pushes state every 30s via Supabase)

### Run

```bash
source .env
python3 bot.py
```

The bot runs the full pipeline (price feeds, volatility, probability, edge detection) and places live orders. Set `OBSERVATION_MODE = True` in `bot.py` to run in observation-only mode (logs everything but places no orders).

## Deployment

Runs as a systemd service (`kalshi-bot`) on a DigitalOcean droplet. Pushing to `main` auto-deploys via GitHub Actions:

1. SSH into VPS as `botuser`
2. `git pull origin main`
3. Syntax-check `bot.py` (`python3 -c "import ast; ast.parse(..."`)
4. `sudo systemctl restart kalshi-bot`

## Kalshi API Notes

- **Auth**: RSA-PSS signature --- the signing path must include the `/trade-api/v2` prefix
- **Orderbook**: Returns separate YES and NO orderbooks. Market NBBO provides `yes_ask`, `yes_bid`, `no_ask`, `no_bid`. YES + NO prices do NOT always sum to 100.
- **Order type**: All orders are limit orders (no market orders as of Feb 2026)
- **Settlements**: Bot uses the settlements API for outcome detection, never z-score heuristics or balance deltas
- **Fee formula**: taker = `ceil(fee_mult_taker * C * P * (100-P) / 100)`, maker = $0. Default `fee_mult_taker` = 0.07 (crypto), 0.035 (SPX finance category). Ceil on total, not per contract.

## Project Structure

```
bot.py                         -- core bot logic (~14,600 lines, never rename)
models.py                     -- pure-math model classes (EGARCHEstimator, fee calc, TV RK weights)
config.py                     -- shared constants (sizing tiers, drawdown thresholds, EGARCH params)
market_config.py               -- centralized MarketTypeConfig (validates against bot.py at startup)
analyst.py                     -- AI analyst (news sentiment, loss analysis, Telegram alerts)
fifteenm_shadow.py             -- 15M shadow engine (A1 RecalibratedEGARCH, A2 LightGBM, A3 EGARCH gating, A4 LateWindow)
hourly_alt_shadow.py           -- hourly alt shadow strategies (HAR-RV, market-making sim)
spx_engine.py                  -- S&P 500 intraday engine (EGARCH + VIX, observation mode)
spx_harrv_shadow.py            -- SPX HAR-RV shadow strategy
weather_engine.py              -- weather temperature engine (NWP ensemble, 19 cities, observation mode)
sports_engine.py               -- sports comeback engine (Bayesian LR, observation mode)
sports_data.py                 -- sports LR tables and league configuration
capital_allocator.py           -- capital allocation across product types
dashboard_snapshot.py          -- builds dashboard state snapshots for Supabase
supabase_sync.py               -- pushes snapshots to Supabase Realtime every 30s
watchdog.py                    -- process health monitoring
auditor.py                     -- deterministic health checks, runs hourly via cron, Telegram alerts
researcher.py                  -- 3x daily performance reports to Telegram (7:30am/12:30pm/7:30pm ET)
conftest.py                    -- pytest fixtures
start.sh                       -- systemd entrypoint (venv + .env + bot.py)
requirements.txt               -- Python dependencies
.env.example                   -- credential template
.github/workflows/deploy.yml   -- auto-deploy on push to main
POSTMORTEMS.md                 -- incident post-mortems
TESTING_STRATEGY.md            -- test architecture documentation
```

### Scripts (scripts/)

```
15m_alpha_research.py          -- 13-section 15M deep dive (regime, price tiers, STC, calibration)
15m_live_audit.py              -- 15M live audit with regime filtering and Wilson CIs
alpha_audit.py                 -- cross-system funnel analysis, counterfactual PnL
audit_alerts.py                -- audit alert delivery
audit_cron.py                  -- automated hourly audit runner
audit_runner.sh                -- shell wrapper for audit cron
build_lr_tables.py             -- build sports likelihood ratio tables
build_whitepaper.py            -- render whitepaper from template
calibrate_dist.py              -- fit NIG distribution parameters per asset
check_docs_freshness.py        -- documentation staleness checker
data_health_monitor.py         -- NULL rates, data gaps, shadow coverage
extract_config.py              -- extract config for external analysis
generate_docs.py               -- generate documentation
generate_whitepaper_stats.py   -- auto-update README stats from VPS DB
hourly_alpha_research.py       -- 12-section hourly research (600+ config grid search)
hourly_shadow_audit.py         -- hourly shadow audit
maker_opportunity_cost.py      -- maker vs taker fill rate analysis
no_side_status.py              -- NO-side shadow data report
pre_deploy_check.sh            -- pre-deploy syntax and constant validation
quiet_market_monitor.py        -- weekend/overnight discount monitor
setup_audit_cron.sh            -- install audit cron job
setup_full_audit_timer.sh      -- install full audit systemd timer
shadow_eval.py                 -- shadow strategy evaluator
sports_alpha_research.py       -- 16-section sports research (SPRT, deficit, CLV)
sports_analysis.py             -- sports data analysis utilities
sports_shadow_audit.py         -- sports shadow audit
spx_alpha_research.py          -- 15-section SPX research (EGARCH blend, VIX regimes)
spx_shadow_audit.py            -- SPX shadow audit
vps_mcp_server.py              -- MCP server for VPS remote access
weather_alpha_research.py      -- 18-section weather research (ensemble quality, HRRR)
weather_shadow_audit.py        -- weather shadow audit
weekend_discount_audit.py      -- weekend/overnight edge discount audit
```

### Tests (774 tests)

```
test_adaptive_jump.py          -- adaptive jump detection tests
test_adaptive_rk.py            -- adaptive RK bandwidth tests
test_addon.py                  -- price improvement addon tests
test_buffer_persistence.py     -- price buffer persistence tests
test_dashboard_contract.py     -- dashboard snapshot ↔ JS contract tests
test_egarch.py                 -- EGARCH estimator tests
test_ghost_fill.py             -- ghost fill detection tests
test_har_iv.py                 -- HAR-IV model tests
test_har.py                    -- HAR model tests
tests/test_calibration_engine.py
tests/test_call_sites.py       -- function call site validation
tests/test_config_consistency.py -- bot.py ↔ market_config.py consistency
tests/test_contracts.py
tests/test_db_signatures.py    -- DB insert signature validation
tests/test_decided_contract.py -- decided contract overlay tests
tests/test_execution.py        -- order execution tests
tests/test_fee_calc.py         -- fee calculation tests
tests/test_invariants.py       -- system invariant assertions
tests/test_probability_engine.py
tests/test_regression.py       -- regression tests for past bugs
tests/test_scan_pipeline.py    -- scan pipeline integration tests
tests/test_spx_price_feed.py   -- SPX price feed tests
tests/test_vol_engine.py       -- volatility engine tests
tests/test_weather_no_side.py  -- weather NO-side pipeline tests
tests/test_weekend_discount.py -- weekend edge discount tests (24 tests)
```

### Other Directories

```
analysis/                      -- one-off analysis scripts (regime_analysis.py)
research/                      -- research briefs (hourly calibration, ETH/SOL/XRP, hourly alt shadow)
templates/                     -- LaTeX templates for whitepaper rendering
.claude/skills/                -- Claude Code skill definitions for automated workflows
```

### Journals (gitignored)

The bot writes JSONL journals for every stage of its decision-making pipeline:

| Journal | Contents |
|---------|----------|
| `scan` | Every 1-second scan cycle with prices and vol estimates |
| `opportunity` | Evaluated opportunities with full model output |
| `trade` | Executed trades with price, count, cost, fee, z-score, edge, strategy |
| `order` | Order lifecycle (placed, filled, cancelled) |
| `rejection` | Opportunities that were filtered out, with reasons |
| `settlement` | Contract outcomes and P&L |
| `execution` | Execution quality metrics |
| `performance` | Daily summary aggregations |
| `fill_model` | Maker order lifecycle data for ML fill prediction |

### Dashboard (Supabase)

When `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` are set, `supabase_sync.py` pushes a state snapshot every 30 seconds to the `dashboard_state` table: balance, active positions, recent trades, win/loss record, current volatility readings, order flow signals, and session stats. The dashboard is a static HTML page hosted on GitHub Pages, reading from Supabase Realtime.
