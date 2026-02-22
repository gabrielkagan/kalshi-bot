# Kalshi Crypto Trading Bot

Automated trading bot for Kalshi's 15-minute cryptocurrency prediction markets. Monitors real-time price feeds across four exchanges, estimates settlement probabilities using microstructure-aware volatility models, and trades BTC, ETH, SOL, and XRP contracts when it finds sufficient edge.

## How It Works

```
Coinbase (1s prices) ──┐
Binance ───────────────┤                                          ┌─ Maker order (1¢ below fair)
Kraken ────────────────┼──→ Volatility ──→ Probability ──→ Edge ──┤
Bybit ─────────────────┤     Engine          Engine      Filter   ├─ Taker escalation
Deribit DVOL ──────────┤                                          └─ Panic capture (99¢)
CoinGlass funding ─────┘
```

Every second, the bot scans all active 15-minute windows, picks the single best opportunity across all four assets, and executes if the edge exceeds 5 percentage points.

## Architecture

### Volatility Engine

The bot doesn't use a single volatility number — it blends three estimators with a HAR-RV weighting scheme:

| Estimator | Weight | Purpose |
|-----------|--------|---------|
| 1-min realized kernel | 50% | Current microstructure (Barndorff-Nielsen 2008, Parzen flat-top kernel) |
| 5-min bipower variation | 30% | Jump-robust medium-term vol |
| 15-min realized kernel | 20% | Window-level baseline |

On top of this:

- **Deribit DVOL integration** — when IV diverges from RV by >50%, the engine shifts toward implied vol using inverse-variance weighting. For SOL/XRP (no direct DVOL), it scales BTC DVOL by a rolling cross-asset beta (60-return lookback, clamped 0.5–3.0).
- **Jump detection** — when any single return exceeds 3σ, the vol estimate doubles for 60 seconds.

### Probability Model

Converts the volatility estimate into a settlement probability:

1. Compute z-score: distance from current price to strike, normalized by estimated vol
2. Map through Student-t CDF (df=4) — heavier tails than Gaussian, better for crypto
3. Logistic calibration (β=0.85) — compresses extreme probabilities toward center
4. Hard cap at 93% — the model never claims >93% confidence

Safety rails refuse to trade if: the model says >90% but the market is below 75¢, or |z-score| > 8.0.

### Cross-Exchange Intelligence

Four WebSocket feeds run concurrently to detect directional signals before they show up on Kalshi:

- **Lead/lag consensus** — if 3+ exchanges move >0.3% in the same direction, the probability gets a +2pp boost
- **Single-exchange lead** — a >0.2% move on one exchange adds +1pp
- **Funding rate signal** — extreme funding (>0.05%/8h via CoinGlass) reduces probability by up to 1.5pp as a contrarian dampener

Total cross-exchange adjustment is capped at ±3pp.

### Execution Strategy

The bot has five decision modes, selected by a composite score (45% certainty, 25% orderbook depth, 30% urgency):

| Mode | When | Action |
|------|------|--------|
| `WAIT` | Low score | Do nothing |
| `MAKER_PATIENT` | Moderate edge, time remaining | Post 1¢ below fair value, wait 15s |
| `MAKER_AGGRESSIVE` | Good edge, some time | Post at fair value, wait 10s |
| `TAKER_NOW` | High edge or running low on time | Lift the ask immediately |
| `PANIC_CAPTURE` | Near-certain outcome + dry book | Bid 99¢ |

Maker orders escalate to taker if unfilled within the adaptive timeout window (15s → 10s → 5s as expiry approaches).

### Position Sizing

Quarter-Kelly with drawdown scaling:

```
f = 0.25 × (b×p − q) / b

where b = (100 − price) / price, p = calibrated prob, q = 1 − p
```

- Max 5 contracts per trade, max 3% of bankroll at risk
- At 90% of starting balance: halve position sizes
- At 80% of starting balance: quarter position sizes
- Only one asset per 15-minute window (whichever has highest edge)

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
| Kalshi | REST | Markets, orderbooks, positions, settlements | 1s scan loop, 30s market refresh |

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

The bot starts in **observation mode** by default — it runs the full pipeline (price feeds, volatility, probability, edge detection) and logs everything, but places no orders. Set `OBSERVATION_MODE = False` in `bot.py` to enable live trading.

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
bot.py                         — all bot logic (~4,500 lines, never rename)
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

### Firebase Dashboard (optional)

When `FIREBASE_DB_URL` is set, the bot pushes a state snapshot every 10 seconds: balance, active positions, recent trades, win/loss record, current volatility readings, order flow signals, and session stats. In observation mode it also tracks simulated P&L.
