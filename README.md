# Kalshi Crypto Trading Bot

Automated trading bot for Kalshi's 15-minute cryptocurrency prediction markets. Monitors real-time price feeds across four exchanges, estimates settlement probabilities using microstructure-aware volatility models, and trades BTC, ETH, SOL, and XRP contracts when it finds sufficient edge.

## How It Works

```
Coinbase (1s prices) ──┐
Binance ───────────────┤                                          ┌─ Maker order (patient/degraded)
Kraken ────────────────┼──→ Volatility ──→ Probability ──→ Edge ──┤
Bybit ─────────────────┤     Engine          Engine      Filter   ├─ Taker escalation (IOC)
Deribit DVOL ──────────┤                                          └─ Three-tier post_only handler
CoinGlass funding ─────┘
```

Every second, the bot scans all active 15-minute windows across all four assets and executes when the fee-adjusted edge exceeds 1.5 percentage points.

## Architecture

### Volatility Engine

The bot blends multiple volatility estimators using Mincer-Zarnowitz R²-weighted EMA blending:

| Estimator | Purpose |
|-----------|---------|
| 1-min realized kernel (adaptive H*) | Current microstructure (Barndorff-Nielsen 2008, Parzen flat-top kernel, data-driven bandwidth) |
| 5-min bipower variation | Jump-robust medium-term vol |
| 15-min realized kernel | Window-level baseline |
| EGARCH(1,1) Student-t | Conditional volatility (shadow mode — logging, not yet driving trades) |

Blend weights are determined dynamically by Mincer-Zarnowitz R² regression quality, smoothed with an EMA (λ=0.97). This replaces the earlier fixed 50/30/20 weighting with data-adaptive weights per asset.

On top of this:

- **Deribit DVOL integration** — when IV diverges from RV by >50%, the engine shifts toward implied vol using inverse-variance weighting. For SOL/XRP (no direct DVOL), it scales BTC DVOL by a rolling cross-asset beta (60-return lookback, clamped 0.5–3.0).
- **Adaptive jump detection** — uses EWMA variance tracking on 15-second subsampled returns. Jump threshold adapts to each asset's current volatility regime (percentile-based), replacing the fixed 3σ multiplier. Includes a cooldown and tier system for sustained jump episodes.
- **Adaptive RK bandwidth** — the Realized Kernel bandwidth parameter H* is chosen data-adaptively using the noise-to-signal ratio, replacing the fixed H=1 default.

### Probability Model

Converts the volatility estimate into a settlement probability:

1. Compute z-score: distance from current price to strike, normalized by estimated vol
2. Map through **Normal Inverse Gaussian (NIG) CDF** — per-asset fitted parameters capturing both heavy tails and asymmetry in crypto returns. NIG dramatically outperforms Student-t (KS p-values: BTC 0.11, ETH 0.42 vs. ~0 for Student-t). Falls back to Student-t(df=4) if NIG config unavailable.
3. **Data-driven calibration** — CalibrationEngine replaces fixed β=0.85 Platt scaling with learned calibration from settlement outcomes. Supports Platt Scaling (200+ samples), Beta Calibration (500+ samples), and Isotonic Regression (1000+ samples). Falls back to fixed logistic when insufficient data.
4. Dynamic probability cap — time-dependent ceiling (93% at >10min, up to 99.5% at <1min)

Safety rails refuse to trade if: the model says >90% but the market is below 75¢, or |z-score| > 12.0.

### Cross-Exchange Intelligence

Four WebSocket feeds run concurrently to detect directional signals before they show up on Kalshi:

- **Lead/lag consensus** — if 3+ exchanges move >0.3% in the same direction, the probability gets a +2pp boost
- **Single-exchange lead** — a >0.2% move on one exchange adds +1pp
- **Funding rate signal** — extreme funding (>0.05%/8h via CoinGlass) reduces probability by up to 1.5pp as a contrarian dampener
- **Kalshi order flow** (shadow mode) — orderbook imbalance, depth velocity, and spread convergence signals from Kalshi's own book

Total cross-exchange adjustment is capped at ±3pp.

### Execution Strategy

Three-tier post_only rejection handling with adaptive maker-to-taker escalation:

| Tier | When | Action |
|------|------|--------|
| **Tier 1: Normal maker** | 0–1 post_only rejections | `post_only=True` limit order, 1–2¢ below fair value |
| **Tier 2: Degraded maker** | 2 rejections (locked spread) | Same as Tier 1 but 1¢ worse price |
| **Tier 3: Taker escalation** | 3+ rejections | IOC taker order with edge re-verification at taker fee rates |

For orders that are placed but sit unfilled, the existing time-based escalation handles conversion:

| Urgency | Time to Close | Maker Wait |
|---------|--------------|------------|
| Low | 60–300s | 15s |
| Medium | 30–60s | 10s |
| High | <30s | 5s |

Escalation uses `amend_order()` to convert in-place, falling back to cancel + IOC taker. Queue position polled every ~5s for timing. Fill detection via Kalshi WebSocket (zero API cost, REST fallback).

### Position Sizing

Quarter-Kelly with drawdown scaling:

```
f = 0.25 × (b×p − q) / b

where b = (100 − price) / price, p = calibrated prob, q = 1 − p
```

- Risk-based sizing: up to 75% of bankroll at 5%+ edge, 35% at 3%+, 20% at 1.5%+
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
| Kalshi | REST + WebSocket | Markets, orderbooks, positions, settlements, fills, orderbook deltas | 1s scan loop + real-time WS fills/orderbook |

## Live Stats

<!-- Auto-updated by GitHub Actions from VPS state.db -->

| Metric | Value |
|--------|-------|
| Markets evaluated | 0 |
| Observation period | N/A |
| Filter pass rate | 0% (0 of 0) |
| Top rejection reason | N/A |
| Settled trades | 0 |
| Win rate | N/A |
| Observation P&L | 0 cents |

*Last updated: N/A*

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
- **API tier**: Advanced (30 reads/sec, 30 writes/sec)

## Project Structure

```
bot.py                         — all bot logic (~9,200 lines, never rename)
firebase_push.py               — pushes live dashboard snapshots to Firebase
start.sh                       — systemd entrypoint (venv + .env + bot.py)
requirements.txt               — Python dependencies
dist_config.json               — per-asset NIG distribution parameters (fitted)
.env.example                   — credential template
.github/workflows/deploy.yml   — auto-deploy on push to main
scripts/                       — whitepaper stats generation and rendering
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

When `FIREBASE_DB_URL` is set, the bot pushes a state snapshot every 10 seconds: balance, active positions, recent trades, win/loss record, current volatility readings, order flow signals, execution engine stats, and session counters.
