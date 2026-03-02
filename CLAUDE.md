# Kalshi Crypto Trading Bot

Cryptocurrency prediction market trading bot for the Kalshi platform. Trades above/below 15-minute window markets on BTC, ETH, SOL, and XRP. Also scans hourly markets (KXBTCD, KXETHD, KXSOLD, KXXRPD) in observation mode.

## Critical Rules

- **bot.py is sacred** — never rename it. systemd calls `start.sh` which calls `bot.py`
- **Never commit `.env` or `*.jsonl` files** — both are gitignored
- **Always syntax-check before committing:** `python3 -c "import ast; ast.parse(open('bot.py').read())"`
- **Pushing to main auto-deploys** — GitHub Actions SSHes into the VPS and restarts the service
- **Always verify deployment** — confirm VPS pulled the latest commit hash. Not done until verified.
- **Data-driven changes only** — do not suggest config changes without backing data
- **Investigate before explaining** — when the user reports a loss or anomaly, look at actual data first. Do not dismiss or speculate.
- **After ANY function signature change**: grep ALL call sites and verify every caller passes the new parameter. `ast.parse` won't catch unbound names.
- **After ANY constant change in bot.py**: grep the constant name across ALL files (especially `market_config.py`) — `MarketTypeConfig` mirrors bot.py constants and `validate_market_configs()` asserts they match at startup. A mismatch = crash loop on VPS.
- **Never add keys to `_shadow_diag`** without also adding them to `insert_rejection()` + `insert_evaluated_opportunity()` signatures + SQL.
- **After ANY change to `discover_active_windows()` or `product_type` assignment**: grep every `window.get("product_type")` comparison in `scan()` and verify each condition still matches actual values. The STC shadow gate, observation gate, and all product_type-based branching must be checked. (Learned: STC shadow gate checked `is None` but 15M windows had `product_type='15m'` — gate was silently dead code, 98c954d Mar 1 2026)
- **Post-deploy data validation**: After deploy, don't just check "bot is running, no errors". Verify **expected DB entries are being created** — e.g., stc_shadow entries when STC is 300-600s, weather_observation entries when weather is enabled. Missing expected rows = silent logic bug.
- **Any new `sqlite3.connect()` call MUST include `PRAGMA busy_timeout=10000`** — multiple threads (bot, firebase, sports) share state.db. Missing timeout = "database is locked" errors under contention. (Learned: sports_engine.py missing busy_timeout caused ~2000 errors/8hr, Mar 2 2026)
- **Performance analysis must filter to current config regime** — losses under old configs (old sizing, old calibration, pre-maker-only) are not relevant to current optimization decisions. Always identify when major config changes happened and filter accordingly.

## Project Structure

- `bot.py` — Main bot (~10400 lines, all trading logic)
- `analyst.py` — AI analyst system (news sentiment, loss analysis, Telegram alerts)
- `weather_engine.py` — Weather ensemble fetcher + probability model (Open-Meteo GFS/ECMWF)
- `firebase_push.py` — Pushes live dashboard snapshots to Firebase
- `start.sh` — Startup script (activates venv, sources .env, runs bot)
- `.github/workflows/deploy.yml` — Auto-deploy to VPS on push to main

## Current State (Mar 2, 2026)

- **OBSERVATION_MODE = False** — LIVE TRADING with real money
- **15M performance:** 169 trades, 149W/20L (88.2%)
- **Hourly:** Reverted to observation mode (HOURLY_OBSERVATION_ONLY = True) — 66.7% WR was unprofitable, calibration under investigation
- **Weather:** Observation mode (WEATHER_OBSERVATION_ONLY = True) — NWP ensemble model (GFS+ECMWF, 82 members), 5 cities, collecting data. wx_market_type tracked in DB for post-hoc analysis.
- **CalibrationEngine:** Hourly data excluded from training (was contaminating 15M model — 35.5% of training data)

## Key Config Values (bot.py)

| Config | Value | Notes |
|--------|-------|-------|
| OBSERVATION_MODE | False | LIVE trading |
| MIN_ENTRY_PRICE | 87 | Cents; two losses at 86c |
| MAX_ENTRY_PRICE | 99 | Cents |
| MIN_EDGE_BY_PRICE | 0.7%-4.0% | Price-dependent: 87c→0.7%, 89c→0.9%, 91c→1.2%, 93c→1.8%, 95c→2.5%, 97c→4.0% |
| MARKET_BLEND_W | 0.40 | 60% model, 40% market (data: model underconfident 0.8-2.1pp at 90%+) |
| MAX_RISK_PER_TRADE | 0.25 | Max 25% bankroll per trade |
| MAX_SECONDS_BEFORE_CLOSE | 900 | 15 min before close (500-900s shadow, 0-500s live) |
| STC_SHADOW_THRESHOLD | 500 | 15M trades above this STC are shadow-only |
| XRP_MAX_RISK_PER_TRADE | 0.12 | XRP RK vol underestimates → cap exposure |
| MAKER_ONLY_THRESHOLD | 0.0 | Taker allowed at all STC (was 90.0, removed: taker 14W/0L 100% WR) |
| HOURLY_OBSERVATION_ONLY | True | Reverted — calibration too overconfident for hourly |
| HOURLY_MARKET_BLEND_W | 0.40 | Optimal Brier per 134K simulation |
| HOURLY_MIN_ENTRY_PRICE | 50 | Lowered from 70 for data collection |
| HOURLY_MAX_RISK_PER_TRADE | 0.15 | 60% of 15M's 0.25 |
| HOURLY_TEMPERATURE_T | 1.45 | Softens overconfident probs: 95%→88.4% |
| HOURLY_KELLY_FRACTION | 0.25 | Quarter-Kelly sizing for hourly |
| HOURLY_CALIBRATION_ENABLED | False | Engine disabled — passthrough + T=1.45 (engine was hurting: Brier 0.12→0.20) |
| HOURLY_MIN_STC_ENTRY | 300 | Min 5 min STC — EGARCH degrades below this |
| HOURLY_MAX_STC_ENTRY | 1800 | Max 30 min STC — expanded for observation data collection |
| HOURLY_EXCLUDED_ASSETS | set() | Empty — collecting all asset data in observation mode |
| HOURLY_MAX_POSITIONS_PER_WINDOW | 2 | ENB ~1.3 — limit correlated exposure |
| HOURLY_MAX_WINDOW_RISK | 0.15 | Max aggregate risk per hourly window |
| WEATHER_OBSERVATION_ONLY | True | Observation-only — collecting ensemble data |
| WEATHER_MIN_ENTRY_PRICE | 10 | Cents — low floor for data collection |
| WEATHER_MAX_ENTRY_PRICE | 99 | Cents |
| WEATHER_MARKET_BLEND_W | 0.20 | 80% model, 20% market (ensemble is primary signal) |
| WEATHER_MIN_EDGE_PCT | 0.003 | 0.3% — lower than crypto for illiquid weather markets |
| WEATHER_MAX_RISK_PER_TRADE | 0.10 | Conservative sizing |
| WEATHER_KELLY_FRACTION | 0.25 | Quarter-Kelly |
| WEATHER_MIN_SECONDS_BEFORE_CLOSE | 3600 | At least 1 hour before settlement |
| WEATHER_MAX_SECONDS_BEFORE_CLOSE | 86400 | Weather settles daily — always eligible |

## Calibration Pipeline

1. Raw statistical probability (from volatility model)
2. Beta calibration (CalibrationEngine — trained on 15M data only, hourly excluded)
3. **Hourly temperature scaling** (Layer 1): T=1.45 softens overconfident probs (95%→88.4%). Applied before OFA/dynamic cap. 15M unaffected.
4. Dynamic cap: **bypassed** when learned calibration is active (`is_learned_method_active()` → uses 0.999 safety ceiling instead of the cap schedule). Cap schedule only applies during startup before training.
5. Market blend: 40% weight toward market price (60% model)
6. Fee-adjusted edge check: price-dependent minimum (0.7% at 87c up to 4.0% at 97c+)

## Hourly Three-Layer Optimization

Researcher-recommended filters to fix hourly overconfidence, timing, and correlation issues. All run under `HOURLY_OBSERVATION_ONLY = True` — observation gate is the shadow mechanism. Filters placed LATE in pipeline so all upstream data is still logged for counterfactual analysis.

| Layer | Filter Stage | Purpose |
|-------|-------------|---------|
| 1 | Temperature scaling (T=1.45) | Softens 15M calibration that doesn't transfer to hourly |
| 2 | STC timing (300-1800s) | EGARCH degrades outside; expanded to 1800s for observation data |
| 3a | Asset exclusion (disabled) | Disabled in observation mode — collecting all asset data |
| 3b | Per-window position limit (2) | ENB ~1.3 independent bets per window |
| 3c | Per-window risk cap (15%) | Prevents correlated multi-asset blowups |
| 3d | Quarter-Kelly sizing | 44% of growth rate, ~3% halving probability |

## Shadow Mode Features

| Feature | Status |
|---------|--------|
| Kalshi Order Flow (OFT) | Shadow — collecting data |
| Sigmoid QLIKE mapping | Shadow — alternative EGARCH weight |
| Cal pipeline (no-blend) | Shadow — monitoring after revert |
| JUMP_ADAPTIVE, RK_ADAPTIVE | **Promoted** — driving live |
| EGARCH core + blend | **Promoted** — driving live |
| TV RK weights | **Promoted** — driving live |

## Order Execution

- **Always enters as maker** (post_only=True), escalates to taker if unfilled
- **Taker allowed at all STC** — MAKER_ONLY_THRESHOLD=0 (data: taker 14W/0L, 100% WR across all STC zones)
- **Escalation**: maker → poll queue → cancel-replace IOC taker
- **WS fill detection** with REST fallback
- **Candidate logging**: Both observation_trade (obs mode) and candidate (live mode) logged to evaluated_opportunities DB

## Tech Stack

- **Language:** Python 3, virtualenv
- **Deployment:** DigitalOcean droplet (45.55.181.30), Ubuntu 24.04, `botuser`, systemd `kalshi-bot`
- **Dashboard:** Firebase Realtime Database
- **Analyst:** Claude API via `analyst.py` — Telegram alerts (high confidence only)

## Kalshi API

- **Auth:** RSA-PSS signature with `/trade-api/v2` prefix
- **Orderbook:** Returns only bids — best YES ask = `100 - highest_NO_bid`
- **All orders are limit orders** (no market orders)
- **API tier:** Advanced (30 reads/sec, 30 writes/sec)
- **Series (15M):** KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M
- **Series (hourly):** KXBTCD, KXETHD, KXSOLD, KXXRPD
- **Series (weather):** KXHIGHNY, KXHIGHCHI, KXHIGHMIA, KXHIGHDEN, KXHIGHLAX

## Fee Formula

- **Taker:** `ceil(0.07 * C * P * (1-P))` — ~1% of edge at typical prices
- **Maker:** `ceil(0.0175 * C * P * (1-P))` — 4x cheaper

## Data Storage

- `state.db` — SQLite: settled_trades, rejected_opportunities, evaluated_opportunities
- `opportunity_journal.jsonl` — filter stage tracking
- `scan_journal.jsonl` — per-tick scan summaries (~330MB/day)
- `fill_model_journal.jsonl` — maker order lifecycle for ML fill prediction
