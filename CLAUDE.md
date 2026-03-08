# Kalshi Crypto Trading Bot

Cryptocurrency prediction market trading bot for the Kalshi platform. Trades above/below 15-minute window markets on BTC, ETH, SOL, and XRP. Also scans hourly markets (KXBTCD, KXETHD, KXSOLD, KXXRPD) in observation mode.

## Interaction Rules

- **Answer first, plan later** — when asked to investigate something (a loss, an alert, performance data, an anomaly), give direct analysis with numbers FIRST. Do not explore code, write plans, or enter plan mode. Answer the question, then offer next steps.
- **Don't re-plan finalized plans** — when continuing from a prior session with an existing plan, start implementing immediately. Do not re-audit, re-plan, or rewrite plans that were already approved.
- **Don't deploy without explicit confirmation** — always present the change summary and wait for user approval before `git push`. Never auto-deploy.

## Common Workflows

These are the standard procedures for recurring tasks. Follow these steps without asking for permission to start.

### Investigate a loss or anomaly
1. Query state.db for the specific trade(s) — get entry price, settlement, PnL, fees, STC, asset, product_type
2. Check what the model predicted (raw_prob, calibrated_prob, blended_prob) from evaluated_opportunities
3. Check if settlement was correct (verify against actual price data)
4. Check if the loss was a config issue, model issue, or just variance
5. Present findings with actual numbers FIRST, then offer next steps

### Performance analysis
1. Identify current config regime (check git log for last major config change)
2. Filter settled_trades to current regime only
3. Use actual Kelly sizing in any PnL calculations — never flat 1-contract
4. Report: n trades, W/L, win rate, total PnL, PnL per trade, Brier score if applicable
5. Break down by asset, by STC zone, by price bucket if relevant

### Add a new shadow strategy
1. Add a shadow flag constant (e.g., NEW_FEATURE_SHADOW = True)
2. Wire into scan() — run shadow logic, log to evaluated_opportunities with appropriate filter_stage
3. Add DB columns if needed (remember: update INSERT + signature + SQL in same commit)
4. Add dashboard metric to dashboard_snapshot.py
5. Do NOT make it live — shadow only until explicitly told to promote

### Deploy a change
1. Make the code change
2. Run `python3 -c "import ast; ast.parse(open('bot.py').read())"` — syntax check
3. Grep for any affected call sites if signatures changed
4. Grep for any affected constants in market_config.py
5. Present a change summary — wait for user approval
6. `git add`, `git commit`, `git push` (triggers auto-deploy)
7. Verify deployment: confirm VPS pulled latest commit hash
8. Post-deploy: verify expected DB entries are being created

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
- **When wiring any engine to CalEngine pipeline**: all three must ship in the SAME commit: (1) engine's INSERT includes `raw_prob`, (2) settlement code routes to the correct CalEngine, (3) audit script checks for CalEngine observations. Shipping these across separate commits creates silent data gaps where audits check for data the code isn't producing yet. (Learned: sports raw_prob was added to audit before the INSERT was fixed → 134 rows with NULL raw_prob, Mar 4 2026)
- **After ANY change to `discover_active_windows()` or `product_type` assignment**: grep every `window.get("product_type")` comparison in `scan()` and verify each condition still matches actual values. The STC shadow gate, observation gate, and all product_type-based branching must be checked. (Learned: STC shadow gate checked `is None` but 15M windows had `product_type='15m'` — gate was silently dead code, 98c954d Mar 1 2026)
- **Post-deploy data validation**: After deploy, don't just check "bot is running, no errors". Verify **expected DB entries are being created** — e.g., stc_shadow entries when STC is 300-600s, weather_observation entries when weather is enabled. Missing expected rows = silent logic bug.
- **Any new `sqlite3.connect()` call MUST include `PRAGMA busy_timeout=10000`** — multiple threads (bot, firebase, sports) share state.db. Missing timeout = "database is locked" errors under contention. (Learned: sports_engine.py missing busy_timeout caused ~2000 errors/8hr, Mar 2 2026)
- **Performance analysis must filter to current config regime** — losses under old configs (old sizing, old calibration, pre-maker-only) are not relevant to current optimization decisions. Always identify when major config changes happened and filter accordingly.
- **After ANY bug fix**: do root cause analysis, explain why it happened, and add a regression test to prevent recurrence. Never just fix and move on.
- **All sim PnL and counterfactual analysis MUST use actual Kelly sizing** — never use 1-contract flat sizing. Position size comes from the Kelly formula with the bot's actual risk parameters. Flat sizing produces misleading PnL numbers.
- **Never present analysis without checking actual data first** — no assumptions about column values, schema, enum strings, or data shape. Always run `PRAGMA table_info()` and `SELECT DISTINCT` before building queries. (Learned: wrong column values, wrong regime detection, wrong filter_stage assumptions all caused bad analysis.)

## Anti-Patterns — Do NOT Do These

- **Don't refactor bot.py into multiple files** — systemd/start.sh/deploy pipeline all depend on the single-file structure. Engines (spx_engine.py, weather_engine.py, analyst.py) are the exception because they run as separate threads/processes.
- **Don't add async** — the bot is synchronous by design, threading is used only for WS feeds and engine threads
- **Don't suggest switching from SQLite** — single-writer is fine for our throughput, latency matters, and the DB is local to the VPS
- **Don't suggest switching from JSONL journals** — they're append-only, zero-overhead, and rotated daily via cron
- **Don't create test files without being asked** — focus on the change, add regression tests only when specified in Critical Rules
- **Don't refactor code "for readability" during a bugfix** — fix the bug, nothing else
- **Don't change Kelly fraction, blend weights, or edge thresholds without data justification** — these are tuned from backtests and live data

## Project Structure

- `bot.py` — Main bot (~13600 lines, all trading logic)
- `analyst.py` — AI analyst system (news sentiment, loss analysis, Telegram alerts)
- `spx_engine.py` — SPX hourly market engine (Polygon.io price feed, EGARCH, RK, VIX integration)
- `weather_engine.py` — Weather ensemble fetcher + probability model (Open-Meteo GFS/ECMWF)
- `market_config.py` — Centralized MarketTypeConfig for all product types (validates against bot.py at startup)
- `dashboard_snapshot.py` — Builds dashboard state snapshots (used by Supabase syncer)
- `start.sh` — Startup script (activates venv, sources .env, runs bot)
- `fifteenm_shadow.py` — 15M shadow strategies (A1 RecalibratedEGARCH, A2 LightGBM, A3 EGARCH gating)
- `hourly_alt_shadow.py` — Hourly alternative shadow strategies (HAR-RV, market-making sim)
- `sports_engine.py` — Sports comeback market engine (ESPN live data, Bayesian posterior)
- `auditor.py` — Deterministic health checks, runs hourly via cron, Telegram alerts
- `auditor_state.db` — Alert deduplication state (gitignored via *.db pattern)
- `researcher.py` — 3x daily performance reports to Telegram, runs via cron (7:30am/12:30pm/7:30pm ET)
- `researcher_state.db` — Report history and period tracking (gitignored via *.db pattern)
- `.github/workflows/deploy.yml` — Auto-deploy to VPS on push to main

## bot.py Layout (approximate line ranges)

- **Lines 1–600:** Imports, constants, config (trading params, API config, volatility engine, calibration, sizing, execution)
- **Lines 600–700:** Utility functions (fee calculation, TV RK weights, dollar/cent conversions)
- **Lines 700–890:** `evaluate_execution_strategy()` — diagnostic only, does not control execution
- **Lines 894–1162:** `KalshiClient` — API wrapper, order management, orderbook fetching
- **Lines 1163–1225:** `Logger` — JSONL trade/event logging
- **Lines 1226–1261:** `TelegramNotifier` — Telegram alerts
- **Lines 1262–2375:** `StateManager` — DB init (`_create_tables` at 1283), position tracking, settlement processing
- **Lines 2376–2537:** `CoinbaseFeed` — WebSocket price feed, OHLCV snapshots
- **Lines 2538–2869:** `KalshiFeed` — WebSocket orderbook stream, fill detection
- **Lines 2870–2950:** `DeribitDVOLFetcher` — Implied volatility index
- **Lines 2951–3268:** `CrossExchangeFeed` — Kraken, Binance, Gemini order flow
- **Lines 3269–3345:** `CoinGlassFetcher` — Derivative funding rates
- **Lines 3346–3628:** `OrderFlowEngine` + `KalshiOrderFlowTracker` — Kalshi OB flow signals
- **Lines 3629–4552:** `VolatilityEngine` — Realized Kernel (RK), GARCH, RV estimation, TV RK weights
- **Lines 4553–5084:** `EGARCHEstimator` — EGARCH(1,1) with Student-t, MLE fitting
- **Lines 5105–5278:** `MincerZarnowitzTracker` — R² tracking, EGARCH weight optimization
- **Lines 5279–5485:** `ProbabilityEngine` — Z-score, normal CDF, market blend
- **Lines 5486–6350:** `CalibrationEngine` — Beta/Platt/isotonic, `_CAL_REGISTRY`, shadow pipeline
- **Lines 6351–6490:** `PositionSizer` — Kelly sizing, risk parity, drawdown caps
- **Lines 6491–9896:** `OpportunityScanner` — `scan()` at 6713, market discovery, filter pipeline, shadow signals
- **Lines 9897–12021:** `OrderExecutor` — `execute()` at 9992, maker→taker escalation, fill detection
- **Lines 12022–12633:** `SettlementTracker` — Settlement detection, CalEngine routing, PnL computation
- **Lines 12634–12718:** `discover_active_windows()` — Market discovery from Kalshi API
- **Lines 12719–13620:** `MainLoop` — `run()` at 13482, init, WS subscription, periodic tasks
- **Lines 13621–13623:** `main()` — Entry point

## Current State (Mar 8, 2026)

- **OBSERVATION_MODE = False** — LIVE TRADING with real money
- **15M performance:** 237 trades, 218W/19L (92.0%)
- **XRP_15M_SHADOW = True** — XRP 15M candidates shadow-only, not traded live
- **Hourly:** Observation mode (HOURLY_OBSERVATION_ONLY = True) — calibration disabled (HOURLY_CALIBRATION_ENABLED = False), T=1.45 softening
- **SPX Hourly:** Observation mode (SPX_HOURLY_OBSERVATION_ONLY = True) — EGARCH+RK blend, VIX integration
- **Weather:** Observation mode (WEATHER_OBSERVATION_ONLY = True) — NWP ensemble model (GFS+ECMWF, 82 members), 5 cities
- **Sports:** Observation mode (SPORTS_OBSERVATION_ONLY = True) — hardcoded, never live without explicit promotion
- **15M Shadow:** A1 (RecalibratedEGARCH), A2 (LightGBM), A3 (EGARCH gating) — all shadow-only in fifteenm_shadow.py
- **CalibrationEngine:** Hourly data excluded from 15M training; hourly CalEngine disabled

## Key Config Values (bot.py)

| Config | Value | Notes |
|--------|-------|-------|
| OBSERVATION_MODE | False | LIVE trading |
| MIN_ENTRY_PRICE | 86 | Cents (data: 86c counterfactual 93.8% WR, 30W/2L n=32) |
| MAX_ENTRY_PRICE | 99 | Cents |
| MIN_EDGE_PCT | 0.25 | Flat fallback for execution paths (was 0.7) |
| MIN_EDGE_BY_PRICE | 0.25%-2.0% | Price-dependent (halved Mar 3): 86c→0.25%, 89c→0.25%, 91c→0.35%, 93c→0.9%, 95c→1.25%, 97c→2.0% |
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
| SPX_HOURLY_OBSERVATION_ONLY | True | Shadow-only — collecting data, no live trades |
| SPX_HOURLY_MIN_ENTRY_PRICE | 70 | Cents — lower than crypto for data collection |
| SPX_HOURLY_MAX_ENTRY_PRICE | 99 | Cents |
| SPX_HOURLY_MARKET_BLEND_W | 0.40 | 60% model, 40% market |
| SPX_HOURLY_TEMPERATURE_T | 1.0 | No temperature correction yet — need data |
| SPX_HOURLY_KELLY_FRACTION | 0.25 | Quarter-Kelly |
| SPX_HOURLY_MAX_RISK_PER_TRADE | 0.15 | Conservative sizing |
| SPX_HOURLY_FEE_MULTIPLIER_TAKER | 0.035 | Finance category — half of crypto's 0.07 |
| SPX_HOURLY_FEE_MULTIPLIER_MAKER | 0.0175 | Same as crypto maker |
| SPX_HOURLY_MAX_POSITIONS_PER_WINDOW | 2 | Prevent correlated multi-strike blowups |
| SPX_HOURLY_MAX_WINDOW_RISK | 0.15 | Max aggregate risk per SPX window |
| WEATHER_OBSERVATION_ONLY | True | Observation-only — collecting ensemble data |
| WEATHER_MIN_ENTRY_PRICE | 10 | Cents — low floor for data collection |
| WEATHER_MAX_ENTRY_PRICE | 99 | Cents |
| WEATHER_MARKET_BLEND_W | 0.20 | 80% model, 20% market (ensemble is primary signal) |
| WEATHER_MIN_EDGE_PCT | 0.001 | 0.1% — very low for max signal collection (observation-only) |
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
6. Fee-adjusted edge check: price-dependent minimum (0.25% at 86-90c up to 2.0% at 97c+)

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
- **Dashboard:** Supabase Realtime (dashboard_state table)
- **Analyst:** Claude API via `analyst.py` — Telegram alerts (high confidence only)

## Kalshi API

- **Auth:** RSA-PSS signature with `/trade-api/v2` prefix
- **Orderbook:** Returns separate YES and NO orderbooks. Market NBBO provides `yes_ask`, `yes_bid`, `no_ask`, `no_bid`. YES + NO prices do NOT always sum to 100.
- **All orders are limit orders** (no market orders)
- **API tier:** Advanced (30 reads/sec, 30 writes/sec)
- **Series (15M):** KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M
- **Series (hourly):** KXBTCD, KXETHD, KXSOLD, KXXRPD
- **Series (weather):** KXHIGHNY, KXHIGHCHI, KXHIGHMIA, KXHIGHDEN, KXHIGHLAX

## Fee Formula

- **Taker:** `ceil(0.07 * C * P * (1-P))` — ~1% of edge at typical prices
- **Maker:** $0 — Kalshi charges no fee on maker fills

## Data Storage

- `state.db` — SQLite: settled_trades, rejected_opportunities, evaluated_opportunities
- `opportunity_journal.jsonl` — filter stage tracking
- `scan_journal.jsonl` — per-tick scan summaries (~330MB/day)
- `fill_model_journal.jsonl` — maker order lifecycle for ML fill prediction

## DB Schema Reference

### settled_trades
| Column | Type | Notes |
|--------|------|-------|
| ticker | TEXT PK | Market ticker |
| event_ticker | TEXT | Event-level ticker |
| asset | TEXT | BTC, ETH, SOL, XRP |
| market_result | TEXT | Settlement result |
| side | TEXT | yes/no |
| count | INTEGER | Contracts |
| entry_price_cents | INTEGER | Entry price in cents |
| revenue_cents | INTEGER | Settlement revenue |
| fee_cents | INTEGER | Fees paid |
| pnl_cents | INTEGER | Net PnL in cents |
| settled_at | TEXT | Settlement timestamp |
| product_type | TEXT | 15m, hourly, spx_hourly, weather, sports |

### evaluated_opportunities
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| ticker | TEXT | Market ticker |
| event_ticker | TEXT | Event-level ticker |
| asset | TEXT | Asset symbol |
| filter_stage | TEXT | candidate, observation_trade, shadow, edge_too_low, etc. |
| rejection_reason | TEXT | Why rejected (if applicable) |
| evaluation_time | TEXT | When evaluated |
| spot_price | REAL | Underlying price |
| threshold | REAL | Strike threshold |
| volatility | REAL | Vol estimate used |
| market_price | INTEGER | Market price in cents |
| seconds_to_close | REAL | STC at evaluation |
| calibrated_prob | REAL | Final calibrated probability |
| edge | REAL | Edge percentage |
| ofa_adjustment | REAL | Order flow adjustment |
| status | TEXT | open/settled |
| market_result | TEXT | Settlement result (backfilled) |
| counterfactual_pnl | REAL | Simulated PnL |
| product_type | TEXT | 15m, hourly, spx_hourly, weather, sports |

### rejected_opportunities
| Column | Type | Notes |
|--------|------|-------|
| ticker | TEXT PK | Market ticker |
| event_ticker | TEXT | Event-level ticker |
| asset | TEXT | Asset symbol |
| rejection_reason | TEXT | Full descriptive string (NOT short labels) |
| rejection_time | TEXT | When rejected |
| z_score | REAL | Z-score at rejection |
| spot_price | REAL | Underlying price |
| threshold | REAL | Strike threshold |
| volatility | REAL | Vol estimate |
| market_price | INTEGER | Market price in cents |
| seconds_to_close | REAL | STC at rejection |
| calibrated_prob | REAL | Calibrated probability |
| status | TEXT | open/settled |
| product_type | TEXT | 15m, hourly, spx_hourly, weather, sports |

### Other Tables
- **positions** — Open position tracking (ticker PK, asset, side, count, avg_price_cents, status)
- **pending_orders** — In-flight order tracking (order_id PK, ticker, side, action, count, price_cents, status)
- **garch_params** — Persisted GARCH parameters per asset
- **egarch_params** — Persisted EGARCH(1,1) parameters per asset
- **sports_shadow_log** — Sports comeback shadow signals (game_id, sport, league, teams, comeback_prob, edge, market_result, pnl_cents)
- **fifteenm_shadow_signals** — 15M shadow A1/A2/A3 signals (in fifteenm_shadow.py)
- **hourly_alt_shadow_signals** — Hourly alternative shadow signals (in hourly_alt_shadow.py)
