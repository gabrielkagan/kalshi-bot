# Kalshi Crypto Trading Bot

Automated trading bot for Kalshi's 15-minute cryptocurrency prediction markets (BTC, ETH, SOL, XRP).

## How It Works

The bot continuously monitors crypto prices and Kalshi orderbooks to find mispriced contracts. When the model's estimated probability diverges enough from the market price, it places a trade.

**Pipeline:** Real-time price feeds → volatility estimation → probability model → edge detection → order execution

### Data Sources

- **Coinbase WebSocket** — primary price feed (1-second snapshots)
- **Binance, Kraken, Bybit** — cross-exchange order flow for directional confirmation
- **Deribit DVOL** — implied volatility for BTC/ETH
- **CoinGlass** — funding rates and derivatives sentiment

### Probability Engine

- Blended realized volatility across 1-min, 5-min, and 15-min windows
- Student-t distribution (df=4) for fat-tailed crypto returns
- IV-RV regime detection — shifts toward implied vol when it diverges from realized
- Jump detection — multiplies vol estimate during elevated volatility regimes

### Risk Management

- Quarter-Kelly position sizing with drawdown scaling (halves at -10%, quarters at -20%)
- One asset per 15-minute window (trades whichever has highest edge)
- Entry prices constrained to 85–97¢ range
- Minimum 5pp edge required to enter

### Order Execution

- Maker-first strategy with adaptive escalation as expiry approaches
- Taker fallback if maker order doesn't fill within timeout
- All orders are limit orders (Kalshi has no market orders)

## Setup

### Prerequisites

- Python 3
- Kalshi API key + RSA private key

### Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Configure

Copy the example env file and fill in your credentials:

```bash
cp .env.example .env
```

Required variables:
- `KALSHI_API_KEY` — your Kalshi API key ID
- `KALSHI_PRIVATE_KEY_PATH` — path to your RSA private key PEM file

Set `KALSHI_ENV=production` to trade on the live exchange (defaults to demo).

### Run

```bash
source .env
python3 bot.py
```

The bot starts in **observation mode** by default — it evaluates and logs everything but places no orders. Set `OBSERVATION_MODE = False` in `bot.py` to enable live trading.

## Deployment

Runs as a systemd service on a DigitalOcean droplet. Pushing to `main` triggers auto-deploy via GitHub Actions:

1. SSHes into VPS
2. Pulls latest code
3. Syntax-checks `bot.py`
4. Restarts the `kalshi-bot` service

## Project Structure

```
bot.py          — all bot logic (sacred — never rename)
start.sh        — systemd startup script
requirements.txt
.env.example
.github/workflows/deploy.yml
```

### Journals (gitignored)

The bot writes JSONL journals for every stage of its decision-making: scans, opportunities, trades, orders, rejections, settlements, executions, and performance summaries.
