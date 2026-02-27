# Kalshi Crypto Trading Bot

Cryptocurrency prediction market trading bot for the Kalshi platform. Trades above/below 15-minute window markets on BTC, ETH, SOL, and XRP. Also collects observation data on hourly above/below markets (KXBTCD, KXETHD, KXSOLD, KXXRPD).

## Critical Rules

- **bot.py is sacred** — never rename it. systemd calls `start.sh` which calls `bot.py`
- **Never commit `.env` or `*.jsonl` files** — both are gitignored
- **Always syntax-check before committing:** `python3 -c "import ast; ast.parse(open('bot.py').read())"`
- **Pushing to main auto-deploys** — GitHub Actions SSHes into the VPS and restarts the service
- **User prefers data-driven analysis over speculative changes** — do not suggest config changes without backing data

## Project Structure

- `bot.py` — Main bot entry point (~8600 lines, all bot logic lives here)
- `firebase_push.py` — Pushes live dashboard snapshots to Firebase
- `start.sh` — Startup script (activates venv, sources .env, runs bot)
- `.github/workflows/deploy.yml` — Auto-deploy to VPS on push to main

## Current Bot State

- **OBSERVATION_MODE = False** (line 30) — LIVE TRADING with real money
- **Balance:** ~$54
- **Live performance:** 96 settled trades, 89W/7L (92.7% WR)

## Key Config Values (bot.py)

| Config | Value | Line | Notes |
|--------|-------|------|-------|
| OBSERVATION_MODE | False | 31 | LIVE trading |
| MIN_ENTRY_PRICE | 87 | 39 | Cents; two losses at 86c, raised to 87c |
| MAX_ENTRY_PRICE | 99 | 40 | Cents |
| MIN_EDGE_PCT | 0.9 | 331 | 0.9 percentage point minimum edge |
| MARKET_BLEND_W | 0.50 | 273 | 50% market blend (reverted: no-blend was +1.86pp overconfident) |
| MAX_RISK_PER_TRADE | 0.25 | 41 | Max 25% bankroll per trade (was 50%; reduced after loss analysis) |
| MAX_SECONDS_BEFORE_CLOSE | 270 | 43 | Start scanning 4.5 min before close (data: 240-270s 8W/0L) |
| ONE_ASSET_PER_WINDOW | False | 44 | Can trade multiple assets per window |
| SIZING_TIERS | [(0.04,0.25),(0.02,0.20),(0.015,0.15),(0.01,0.10),(0.009,0.07)] | 339 | Fee-adjusted edge tiered sizing (reduced: was 50/35/20) |
| DRAWDOWN_HALF_THRESHOLD | 0.90 | 346 | Halve size below 90% of starting balance |
| DRAWDOWN_QUARTER_THRESHOLD | 0.80 | 347 | Quarter size below 80% |
| MAKER_ONLY_THRESHOLD | 90.0 | 358 | No taker execution below 90s to close (maker only) |
| HOURLY_OBSERVATION_ENABLED | True | 46 | Master switch for hourly data collection |
| HOURLY_OBSERVATION_ONLY | True | 47 | True = log only; False = live trading |
| HOURLY_MARKET_BLEND_W | 0.70 | 53 | Higher blend — calibration untested at hourly |
| HOURLY_MAX_SECONDS_BEFORE_CLOSE | 900 | 51 | 15 min before close |

## Shadow Mode Features

Features that compute and log but do NOT affect live probability/trading:

| Feature | Constant | Status |
|---------|----------|--------|
| Kalshi Order Flow | KALSHI_OFT_SHADOW_MODE = True | Collecting data, has diagnostic logging |
| Sigmoid QLIKE mapping | MZ_SIGMOID_SHADOW_MODE = True | Alternative EGARCH weight via QLIKE ratio |
| Cal pipeline (no-blend) | SHADOW_CAL_PIPELINE = True | Reverted: no-blend system monitors in shadow (was promoted, caused +1.86pp overconfidence) |

Promoted features (shadow off, driving live behavior):
- JUMP_ADAPTIVE (JUMP_ADAPTIVE_SHADOW_MODE = False)
- RK_ADAPTIVE (RK_ADAPTIVE_SHADOW_MODE = False)
- EGARCH core vol (EGARCH_SHADOW_MODE = False)
- EGARCH blend (EGARCH_BLEND_SHADOW_MODE = False)
- TV RK weights (RK_TV_SHADOW_MODE = False)
- Temperature calibration competes in hourly Brier tournament (with 50% market blend applied)

## Order Execution Engine

| Feature | Status | Notes |
|---------|--------|-------|
| `post_only=True` on maker orders | Active | Guarantees maker fees (4x cheaper) |
| `time_in_force="immediate_or_cancel"` on taker orders | Active | Auto-cancel unfilled |
| Direct taker for <60s | Blocked (<90s) | Blocked by maker-only threshold; would skip maker, IOC immediately |
| Cancel-replace escalation | Blocked (<90s) | Blocked by maker-only threshold; cancel maker + IOC taker |
| Maker-only below 90s | Active | No taker execution below 90s to close (data: taker <90s cost -$85) |
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
- **Market series (15M):** KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M
- **Market series (hourly):** KXBTCD, KXETHD, KXSOLD, KXXRPD (observation mode)

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
- **Entry prices:** 87–99c (never below 87c)
- **Minimum edge:** 0.9% (after fees)
- **Position sizing:** Tiered by edge — 25% risk at 4%+ edge, 20% at 2%+, 15% at 1.5%+, 10% at 1%+, 7% at 0.9%+
