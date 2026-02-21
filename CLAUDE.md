# Kalshi Crypto Trading Bot

Cryptocurrency prediction market trading bot for the Kalshi platform.

## Critical Rules

- **bot.py is sacred** — never rename it. systemd calls `start.sh` which calls `bot.py`
- **Never commit `.env` or `*.jsonl` files** — both are gitignored
- **Always syntax-check before committing:** `python3 -c "import ast; ast.parse(open('bot.py').read())"`
- **Pushing to main auto-deploys** — GitHub Actions SSHes into the VPS and restarts the service

## Project Structure

- `bot.py` — Main bot entry point (all bot logic lives here)
- `start.sh` — Startup script (activates venv, sources .env, runs bot)
- `.github/workflows/deploy.yml` — Auto-deploy to VPS on push to main

## Tech Stack

- **Language:** Python 3
- **Environment:** virtualenv (`venv/`)
- **Deployment:** DigitalOcean droplet (45.55.181.30), Ubuntu 24.04, runs as `botuser`
- **Service:** systemd unit `kalshi-bot`

## Deployment

Pushing to `main` triggers auto-deploy:
1. SSH into VPS as `botuser`
2. `git pull origin main` in `~/kalshi-bot-repo`
3. Syntax check on `bot.py`
4. `sudo systemctl restart kalshi-bot`

## Kalshi API Notes

- **Auth:** RSA-PSS signature must include `/trade-api/v2` prefix in the path
- **Orderbook:** Returns only bids — best YES ask = `100 - highest_NO_bid`
- **Order type:** All orders are limit orders (no market orders as of Feb 2026)
- **Outcome detection:** Use Kalshi settlements API, never z-score heuristics or balance deltas

## Fee Formula

- **Taker:** `ceil(0.07 × C × P × (1−P))` — ceil on TOTAL, not per contract
- **Maker:** `ceil(0.0175 × C × P × (1−P))` — ceil on TOTAL, not per contract

## Trading Rules

- **Assets:** BTC, ETH, SOL, XRP — trade only ONE per 15-minute window (whichever has highest edge)
- **Entry prices:** 85–92¢ target (never below 80¢, endgame at 97–99¢ with tiny positions)
- **Position size:** 2–5 contracts per trade at current bankroll (~$200)
