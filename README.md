# Kalshi Crypto Trading Bot

Automated trading bot for Kalshi's cryptocurrency prediction markets. Monitors real-time price feeds, estimates settlement probabilities using microstructure-aware volatility models with EGARCH conditioning, and trades BTC, ETH, SOL, and XRP contracts when it finds sufficient edge.

## How It Works

```
Coinbase (5s prices) ──┐
Kraken ────────────────┤                                          ┌─ Maker order (post_only)
                       ├──→ Volatility ──→ Probability ──→ Edge ──┤
Kalshi WS (orderbook) ─┤     Engine          Engine      Filter   └─ Taker escalation (amend/IOC)
                       │
Deribit DVOL ──────────┘
```

Every few seconds, the bot scans all active market windows across four assets and executes when the fee-adjusted edge exceeds 0.9 percentage points.

## Live Performance

| Metric | Value |
|--------|-------|
| Status | **LIVE TRADING** |
| Balance | ~$217 |
| Settled trades | 107 |
| Win rate | 93.5% (100W / 7L) |
| Live since | February 22, 2026 |

## Markets

### 15-Minute Markets (Live Trading)
Binary contracts settling every 15 minutes — pays $1 if a crypto asset closes above a threshold, $0 otherwise. Series: KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M.

### Hourly Markets (Observation Mode)
75 strikes per event, settling every hour. Currently collecting calibration data only — no live trading. Series: KXBTCD, KXETHD, KXSOLD, KXXRPD.

## Architecture

### Volatility Engine

Blends multiple Realized Kernel estimators (Barndorff-Nielsen 2008, Parzen flat-top kernel) with data-adaptive bandwidth selection, then conditions on EGARCH for forward-looking estimates:

- **Adaptive RK bandwidth (H\*)** — auto-tunes from noise-to-signal ratio
- **EGARCH(1,1) with Student-t innovations** — captures volatility clustering and leverage effects; MLE-fitted on 10,800 samples (15 hours), refitted hourly
- **EGARCH blend** — Mincer-Zarnowitz R²-weighted blending dynamically weights EGARCH vs RK based on forecast quality
- **Time-varying RK weights** — TV blend of multi-scale RK estimators
- **Adaptive jump detection** — percentile-based per-asset thresholds with EWMA variance tracking and tiered severity response
- **Adaptive RK bandwidth** — data-driven H* selection per asset
- **Deribit DVOL integration** — when IV diverges from RV by >50%, blends in implied vol. Cross-asset beta for SOL/XRP (no direct DVOL)

### Probability Model

1. Z-score: distance from spot to threshold, normalized by estimated vol
2. Per-asset Normal Inverse Gaussian (NIG) CDF — captures heavy tails and asymmetry; falls back to Student-t(df=4)
3. Data-driven calibration via CalibrationEngine — currently Beta Calibration with 1,900+ observations
4. Dynamic probability cap: 93% at >10min, relaxing to 99.5% at <1min
5. Market-price blending: 50/50 blend with market-implied probability

### Execution Engine

Always enters as maker, escalates to taker based on time pressure:

1. Place maker order with `post_only=True` (75% cheaper fees)
2. Monitor fills via Kalshi WebSocket (zero API cost, REST fallback)
3. Poll queue position every ~5s for escalation timing
4. If unfilled after wait period (15s/10s/5s depending on time):
   - Attempt `amend_order()` to convert to taker in-place
   - Fallback: cancel + IOC taker order
5. **Maker-only below 90s** — no taker execution when <90s to close (data: taker <90s cost -$85)
6. Three-tier post_only rejection handler: normal → degraded → taker IOC

### Position Sizing

Edge-tiered sizing with drawdown scaling:

| Fee-Adjusted Edge | Risk Fraction |
|-------------------|---------------|
| ≥ 4% | 25% of bankroll |
| ≥ 2% | 20% of bankroll |
| ≥ 1.5% | 15% of bankroll |
| ≥ 1% | 10% of bankroll |
| ≥ 0.9% | 7% of bankroll |

- Safety ceiling: max 25% of bankroll at risk per trade
- At 90% of starting balance: halve position sizes
- At 80% of starting balance: quarter position sizes
- Entry prices: 87–99¢ only (data: losses at 86¢ prompted the raise)

### Shadow Mode Features

Features computing and logging but not affecting live trading:

| Feature | Status | Purpose |
|---------|--------|---------|
| Kalshi Order Flow | Shadow | Orderbook imbalance, depth velocity, spread convergence signals |
| Sigmoid QLIKE | Shadow | Alternative EGARCH weight via QLIKE improvement ratio |
| Shadow Cal Pipeline | Shadow | No-blend calibration monitoring (was promoted, caused overconfidence) |
| Dip Addon | Shadow | Buy more when ask dips ≥3¢ below entry after fill |
| Hourly Observation | Observation | Collecting calibration data for hourly markets (75 strikes/event) |

Promoted features (driving live behavior):
- EGARCH core vol + EGARCH blend + MZ R²-weighted blending
- Adaptive jump detection + Adaptive RK bandwidth
- Time-varying RK weights
- Temperature calibration (Brier tournament)

## Data Sources

| Source | Transport | Data | Status |
|--------|-----------|------|--------|
| Coinbase | WebSocket | BTC, ETH, SOL, XRP spot (5s buffer, 10,800 points = 15hr) | Active |
| Kraken | WebSocket | Spot prices for cross-exchange signals | Active |
| Deribit | REST | DVOL implied volatility (BTC/ETH) | Active |
| Kalshi | REST + WebSocket | Markets, orderbooks, positions, settlements, fills | Active |
| Binance | WebSocket | Spot prices | Geo-blocked (HTTP 451) |

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
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` — trade/settlement alerts

### Run

```bash
source .env
python3 bot.py
```

Set `OBSERVATION_MODE = True` in `bot.py` to run in observation-only mode.

## Deployment

Runs as a systemd service (`kalshi-bot`) on a DigitalOcean droplet (Ubuntu 24.04). Pushing to `main` auto-deploys via GitHub Actions:

1. SSH into VPS as `botuser`
2. `git pull origin main`
3. Syntax-check `bot.py`
4. `sudo systemctl restart kalshi-bot`

## Project Structure

```
bot.py                         — all bot logic (~9,800 lines, never rename)
firebase_push.py               — pushes live dashboard snapshots to Firebase
start.sh                       — systemd entrypoint (venv + .env + bot.py)
requirements.txt               — Python dependencies
.env.example                   — credential template
.github/workflows/deploy.yml   — auto-deploy on push to main
```

### Journals (gitignored)

| Journal | Contents |
|---------|----------|
| `opportunity_journal.jsonl` | Filter stage tracking for every market evaluation |
| `scan_journal.jsonl` | Per-tick scan summaries (~330MB/day) |
| `rejection_journal.jsonl` | Settlement outcomes for rejected opportunities |
| `fill_model_journal.jsonl` | Maker order lifecycle data for ML fill prediction |

### Firebase Dashboard

When `FIREBASE_DB_URL` is set, pushes a state snapshot every 10 seconds: balance, positions, trades, volatility, orderbooks, execution engine health, calibration diagnostics, EGARCH/NIG parameters, order flow signals, hourly observation stats, and 30+ other dashboard sections.

## Kalshi API Notes

- **Auth**: RSA-PSS signature — signing path includes `/trade-api/v2` prefix
- **Orderbook**: Returns only bids — best YES ask = `100 - highest_NO_bid`
- **Order type**: All orders are limit orders (no market orders as of Feb 2026)
- **Settlements**: Uses settlements API for outcome detection, never heuristics
- **Fee formula**: taker = `ceil(0.07 × C × P × (1−P))`, maker = `ceil(0.0175 × C × P × (1−P))` — ceil on total, not per contract
- **API tier**: Advanced (30 reads/sec, 30 writes/sec)
