# Kalshi Crypto Trading Bot

Cryptocurrency prediction market trading bot for the Kalshi platform. Trades above/below 15-minute window markets on BTC, ETH, SOL, and XRP.

## Critical Rules

- **bot.py is sacred** — never rename it. systemd calls `start.sh` which calls `bot.py`
- **Never commit `.env` or `*.jsonl` files** — both are gitignored
- **Always syntax-check before committing:** `python3 -c "import ast; ast.parse(open('bot.py').read())"`
- **Pushing to main auto-deploys** — GitHub Actions SSHes into the VPS and restarts the service
- **User prefers data-driven analysis over speculative changes** — do not suggest config changes without backing data

## Project Structure

- `bot.py` — Main bot entry point (~8300 lines, all bot logic lives here)
- `firebase_push.py` — Pushes live dashboard snapshots to Firebase
- `start.sh` — Startup script (activates venv, sources .env, runs bot)
- `.github/workflows/deploy.yml` — Auto-deploy to VPS on push to main

## Current Bot State

- **OBSERVATION_MODE = False** (line 30) — LIVE TRADING with real money
- **Balance:** ~$58
- **Counterfactual performance (24h sim):** 82 trades, 75W/7L, 91.5% WR, +$42.27

## Key Config Values (bot.py)

| Config | Value | Line | Notes |
|--------|-------|------|-------|
| OBSERVATION_MODE | False | 30 | LIVE trading |
| MIN_ENTRY_PRICE | 86 | 38 | Cents; 86-88c bucket is 100% WR in sim |
| MAX_ENTRY_PRICE | 99 | 39 | Cents |
| MIN_EDGE_PCT | 1.0 | 332 | 1.0 percentage point minimum edge (data: 1.0-1.5% bucket 97.4% WR) |
| MAX_SECONDS_BEFORE_CLOSE | 240 | 42 | Start scanning 4 min before window close |
| ONE_ASSET_PER_WINDOW | False | 43 | Can trade multiple assets per window |
| SIZING_TIERS | [(0.05,0.75),(0.03,0.35),(0.015,0.20),(0.01,0.10)] | 339 | Edge-based tiered sizing |

## Shadow Mode Features

Features that compute and log but do NOT affect live probability/trading:

| Feature | Constant | Status |
|---------|----------|--------|
| EGARCH core vol | EGARCH_SHADOW_MODE = True | Logging, not affecting blended_rv |
| EGARCH blend | EGARCH_BLEND_SHADOW_MODE = True | Closest to promotion (R² 0.37-0.46) |
| HAR model | HAR_SHADOW_MODE = True | NOT ready — models rejected (negative coefficients) |
| Kalshi Order Flow | KALSHI_OFT_SHADOW_MODE = True | New, collecting data, has diagnostic logging |

Promoted features (shadow off, driving live behavior):
- JUMP_ADAPTIVE (JUMP_ADAPTIVE_SHADOW_MODE = False)
- RK_ADAPTIVE (RK_ADAPTIVE_SHADOW_MODE = False)

## Order Execution Engine

| Feature | Status | Notes |
|---------|--------|-------|
| `post_only=True` on maker orders | Active | Guarantees maker fees (4x cheaper) |
| `time_in_force="ioc"` on taker orders | Active | Auto-cancel unfilled, 0.3s wait |
| `amend_order()` for escalation | Active | Amend-first, cancel-replace fallback |
| `get_queue_position()` polling | Active | Every ~5s, queue-aware escalation |
| KalshiFeed WebSocket | Active | fill + orderbook_delta channels |
| WS fill detection | Active | Zero API cost, REST fallback |
| `fill_model_journal.jsonl` | Active | ML training data for fill prediction |
| Dynamic maker offset | Deferred | Needs fill model data (2+ weeks) |
| Continuous urgency function | Deferred | Needs fill model data |

## Tech Stack

- **Language:** Python 3
- **Environment:** virtualenv (`venv/`)
- **Deployment:** DigitalOcean droplet (45.55.181.30), Ubuntu 24.04, runs as `botuser`
- **Service:** systemd unit `kalshi-bot`
- **Dashboard:** Firebase Realtime Database (pushed by firebase_push.py)

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
- **API tier:** Advanced (30 reads/sec, 30 writes/sec)
- **Market series:** KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M

## Fee Formula

- **Taker:** `ceil(0.07 * C * P * (1-P))` — ceil on TOTAL, not per contract
- **Maker:** `ceil(0.0175 * C * P * (1-P))` — ceil on TOTAL, not per contract

## Pipeline Funnel

How the scanner filters opportunities (typical distribution):
1. **low_probability** (~75%) — calibrated prob too low
2. **price_out_of_range** (~12%) — best ask outside [86, 99]c
3. **insufficient_edge** (~11%) — net edge after fees < 1%
4. **candidate** (~0.4%) — passed all filters, would be traded
5. **observation_trade** — best candidate selected per scan tick (logged, not executed in obs mode)

## Data Storage

- `state.db` — SQLite with settled_trades, rejected_opportunities, evaluated_opportunities
- `opportunity_journal.jsonl` — filter stage tracking for every market evaluation
- `scan_journal.jsonl` — per-tick scan summaries (grows fast, ~330MB/day)
- `rejection_journal.jsonl` — settlement outcomes for rejected opportunities
- `fill_model_journal.jsonl` — maker order lifecycle data for ML fill prediction

## Trading Rules

- **Assets:** BTC, ETH, SOL, XRP — can trade multiple per 15-minute window
- **Entry prices:** 86–99c (never below 86c)
- **Minimum edge:** 1% (after fees)
- **Position sizing:** Tiered by edge — 75% risk at 5%+ edge, 35% at 3%+, 20% at 1.5%+, 10% at 1%+
