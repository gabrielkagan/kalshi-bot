# Kalshi Crypto Trading Bot

Automated trading bot for Kalshi's 15-minute cryptocurrency prediction markets. Monitors real-time price feeds across four exchanges, estimates settlement probabilities using microstructure-aware volatility models, and trades BTC, ETH, SOL, and XRP contracts when it finds sufficient edge.

## How It Works

```
Coinbase (1s prices) ──┐
Binance ───────────────┤                                          ┌─ Maker order (post_only)
Kraken ────────────────┼──→ Volatility ──→ Probability ──→ Edge ──┤
Bybit ─────────────────┤     Engine          Engine      Filter   └─ Taker escalation (amend/IOC)
Deribit DVOL ──────────┤
CoinGlass funding ─────┘
```

Every second, the bot scans all active 15-minute windows across all four assets and executes when the fee-adjusted edge exceeds 1 percentage point.

## Architecture

### Volatility Engine

The bot doesn't use a single volatility number — it blends three Realized Kernel estimators (Barndorff-Nielsen 2008, Parzen flat-top kernel) with data-adaptive bandwidth selection:

| Estimator | Base Weight | Purpose |
|-----------|-------------|---------|
| 1-min realized kernel | 50% | Current microstructure |
| 5-min bipower variation | 30% | Jump-robust medium-term vol |
| 15-min realized kernel | 20% | Window-level baseline |

On top of this:

- **Adaptive RK bandwidth (H\*)** — bandwidth auto-tunes from the noise-to-signal ratio, producing tighter estimates in calm periods and wider smoothing during noisy periods
- **Mincer-Zarnowitz R²-weighted EGARCH blending** — an EGARCH(1,1) model with Student-t innovations runs in shadow mode (R² typically 0.42–0.61); MZ regression scores forecast quality, EMA-smoothed weights ready for promotion
- **Deribit DVOL integration** — when IV diverges from RV by >50%, the engine shifts toward implied vol using inverse-variance weighting. For SOL/XRP (no direct DVOL), it scales BTC DVOL by a rolling cross-asset beta (60-return lookback, clamped 0.5–3.0)
- **Adaptive jump detection** — percentile-based per-asset thresholds (replaced fixed 3σ); EWMA variance tracking with tiered response scaling by severity

### Probability Model

Converts the volatility estimate into a settlement probability:

1. Compute z-score: distance from current price to strike, normalized by estimated vol
2. Map through per-asset Normal Inverse Gaussian (NIG) CDF — captures both heavy tails and asymmetry unique to each crypto; falls back to Student-t(df=4) if NIG unavailable
3. Data-driven calibration via CalibrationEngine — progresses from fixed logistic (β=0.85) → Platt Scaling → Beta Calibration → BLR as data accumulates (currently Beta Cal with 1170+ observations)
4. Dynamic probability cap: 93% at >10min, relaxing to 99.5% at <1min remaining
5. Market-price blending: 50/50 blend with market-implied probability below 96¢

Safety rails refuse to trade if: the model says >90% but the market is below 75¢, or |z-score| > 12.0.

### Cross-Exchange Intelligence

Four WebSocket feeds run concurrently to detect directional signals before they show up on Kalshi:

- **Lead/lag consensus** — if 3+ exchanges move >0.3% in the same direction, the probability gets a +2pp boost
- **Single-exchange lead** — a >0.2% move on one exchange adds +1pp
- **Funding rate signal** — extreme funding (>0.05%/8h via CoinGlass) reduces probability by up to 1.5pp as a contrarian dampener

Total cross-exchange adjustment is capped at ±3pp.

### Execution Strategy

The bot always enters as a maker and escalates to taker based on time pressure. A diagnostic strategy engine classifies each opportunity (WAIT, MAKER_PATIENT, MAKER_AGGRESSIVE, TAKER_NOW) for logging, but the actual execution path is:

1. Place maker order with `post_only=True` (guarantees 75% cheaper maker fees)
2. Monitor for fills via Kalshi WebSocket (zero API cost, REST fallback)
3. Poll queue position every ~5s for escalation timing
4. If unfilled after wait period (15s/10s/5s depending on time remaining):
   - Attempt `amend_order()` to convert to taker price in-place
   - Fallback: cancel + IOC (`time_in_force="immediate_or_cancel"`) taker order
5. Three-tier post_only rejection handler: normal → degraded → taker IOC after 3+ rejections

### Position Sizing

Edge-tiered sizing with drawdown scaling:

| Fee-Adjusted Edge | Risk Fraction |
|-------------------|---------------|
| ≥ 4% (~5%+ gross) | 50% of bankroll |
| ≥ 2% (~3%+ gross) | 35% of bankroll |
| ≥ 1.5% (~2.5%+ gross) | 20% of bankroll |
| ≥ 1% | 10% of bankroll |

- Safety ceiling: max 50% of bankroll at risk per trade
- At 90% of starting balance: halve position sizes
- At 80% of starting balance: quarter position sizes
- Can trade multiple assets per 15-minute window

### State & Persistence

SQLite (WAL mode) stores positions, pending orders, settled trades, GARCH parameters, and rejected/evaluated opportunities. On startup the bot reconciles local state against the Kalshi API — API always wins.

## Data Sources

| Source | Transport | Data | Frequency |
|--------|-----------|------|-----------|
| Coinbase | WebSocket | BTC, ETH, SOL, XRP spot prices | 1s snapshots (300-sample buffer) |
| Binance | WebSocket | Spot prices for lead/lag detection | Real-time |
| Kraken | WebSocket | Spot prices for lead/lag detection | Real-time |
| Bybit | WebSocket | Spot prices for lead/lag detection | Real-time |
| Deribit | REST | DVOL implied volatility index | Every 60s (120s cache) |
| CoinGlass | REST | Funding rates | Every 10min (100 calls/day budget) |
| Kalshi | REST + WebSocket | Markets, orderbooks, positions, settlements, fills | 1s scan loop + real-time WS fills/orderbook |

## Live Stats

<!-- Auto-updated by GitHub Actions from VPS state.db -->

| Metric | Value |
|--------|-------|
| Markets evaluated | {{TOTAL_EVALUATED}} |
| Observation period | {{OBSERVATION_PERIOD}} |
| Filter pass rate | {{FILTER_CANDIDATE_PCT}} ({{FILTER_CANDIDATE}} of {{TOTAL_EVALUATED}}) |
| Top rejection reason | {{TOP_REJECTION}} |
| Settled trades | {{TOTAL_SETTLED}} |
| Win rate | {{WIN_RATE}} |
| Observation P&L | {{OBSERVATION_PNL}} cents |

*Last updated: {{GENERATED_AT}}*

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
- `KALSHI_API_KEY` — your Kalshi API key ID
- `KALSHI_PRIVATE_KEY_PATH` — path to your RSA private key PEM file

Optional:
- `KALSHI_ENV=production` — trade on live exchange (defaults to demo)
- `FIREBASE_DB_URL` — enable real-time dashboard (pushes state every 10s)

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

- **Auth**: RSA-PSS signature — the signing path must include the `/trade-api/v2` prefix
- **Orderbook quirk**: Returns only bids — best YES ask = `100 - highest_NO_bid`
- **Order type**: All orders are limit orders (no market orders as of Feb 2026)
- **Settlements**: Bot uses the settlements API for outcome detection, never z-score heuristics or balance deltas
- **Fee formula**: taker = `ceil(0.07 × C × P × (1−P))`, maker = `ceil(0.0175 × C × P × (1−P))` — ceil on total, not per contract

## Project Structure

```
bot.py                         — all bot logic (~9,200 lines, never rename)
firebase_push.py               — pushes live dashboard snapshots to Firebase
start.sh                       — systemd entrypoint (venv + .env + bot.py)
requirements.txt               — Python dependencies
.env.example                   — credential template
.github/workflows/deploy.yml   — auto-deploy on push to main
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

### Firebase Dashboard (optional)

When `FIREBASE_DB_URL` is set, the bot pushes a state snapshot every 10 seconds: balance, active positions, recent trades, win/loss record, current volatility readings, order flow signals, and session stats. In observation mode it also tracks simulated P&L.
