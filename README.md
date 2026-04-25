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

Every second, the bot scans all active 15-minute windows across all four assets and executes when the fee-adjusted edge exceeds a price-dependent minimum (0.25% at 86c up to 1.0% at 97c+).

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

The bot always enters as a maker and escalates to taker based on time pressure. A diagnostic strategy engine classifies each opportunity (WAIT, MAKER_PATIENT, MAKER_AGGRESSIVE, TAKER_NOW) for logging, but the actual execution path is:

1. Place maker order with `post_only=True` (guarantees 75% cheaper maker fees)
2. Monitor for fills via Kalshi WebSocket (zero API cost, REST fallback)
3. Poll queue position every ~5s for escalation timing
4. If unfilled after wait period (15s/10s/5s depending on time remaining):
   - Attempt `amend_order()` to convert to taker price in-place
   - Fallback: cancel + IOC (`time_in_force="immediate_or_cancel"`) taker order
5. Three-tier post_only rejection handler: normal -> degraded -> taker IOC after 3+ rejections
6. Direct taker: when seconds-to-close < 180s, skip maker and submit IOC taker directly

### Position Sizing

Edge-tiered sizing with drawdown scaling:

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

- Safety ceiling: max 25% of bankroll at risk per trade
- At 85% of rolling 7-day peak balance: halve position sizes
- At 75% of rolling 7-day peak balance: quarter position sizes
- At 65% of rolling 7-day peak balance: halt trading entirely
- Can trade multiple assets per 15-minute window

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

<!-- Auto-updated by GitHub Actions from VPS state.db -->

| Metric | Value |
|--------|-------|
| Markets evaluated | 151,358 |
| Observation period | 2026-02-22 to 2026-04-25 |
| Filter pass rate | 3.7\% (5,623 of 151,358) |
| Top rejection reason | Insufficient Edge (47,395) |
| Settled trades | 2,855 |
| Win rate | 93.2\% |
| Observation P&L | 86,618 cents |

*Last updated: 2026-04-25T18:34:37Z*

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

Dependencies: `requests`, `websockets`, `cryptography`, `scipy`, `numpy`

### Configure

```bash
cp .env.example .env
```

Required:
- `KALSHI_API_KEY` --- your Kalshi API key ID
- `KALSHI_PRIVATE_KEY_PATH` --- path to your RSA private key PEM file

Optional:
- `KALSHI_ENV=production` --- trade on live exchange (defaults to demo)
- `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` --- enable real-time dashboard (pushes state every 10s via Supabase)

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
- **Fee formula**: taker = `ceil(0.07 * C * P * (1-P))`, maker = `ceil(0.0175 * C * P * (1-P))` --- ceil on total, not per contract

## Project Structure

```
bot.py                         -- core bot logic (~14,700 lines, never rename)
analyst.py                     -- AI analyst (news sentiment, loss analysis, Telegram alerts)
market_config.py               -- centralized MarketTypeConfig (validates against bot.py at startup)
fifteenm_shadow.py             -- 15M shadow engine (recalibrated EGARCH + LightGBM research)
spx_engine.py                  -- S&P 500 intraday engine (EGARCH + VIX, shadow mode)
weather_engine.py              -- weather temperature engine (NWP ensemble, shadow mode)
sports_engine.py               -- sports comeback engine (Bayesian LR, shadow mode)
sports_data.py                 -- sports LR tables and league configuration
capital_allocator.py           -- capital allocation across product types
dashboard_snapshot.py          -- builds dashboard state snapshots for Supabase
supabase_sync.py               -- pushes snapshots to Supabase Realtime every 10s
watchdog.py                    -- process health monitoring
start.sh                       -- systemd entrypoint (venv + .env + bot.py)
requirements.txt               -- Python dependencies
.env.example                   -- credential template
.github/workflows/deploy.yml   -- auto-deploy on push to main
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

When `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` are set, `supabase_sync.py` pushes a state snapshot every 10 seconds to the `dashboard_state` table: balance, active positions, recent trades, win/loss record, current volatility readings, order flow signals, and session stats. The dashboard is a static HTML page hosted on GitHub Pages, reading from Supabase Realtime.
