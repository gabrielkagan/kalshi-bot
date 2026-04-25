# Kalshi Crypto Trading Bot

Cryptocurrency prediction market trading bot for the Kalshi platform. Trades above/below 15-minute window markets on BTC, ETH, SOL, and XRP. Also scans hourly markets (KXBTCD, KXETHD, KXSOLD, KXXRPD) in observation mode.

## Interaction Rules

- **Answer first, plan later** — when asked to investigate something (a loss, an alert, performance data, an anomaly), give direct analysis with numbers FIRST. Do not explore code, write plans, or enter plan mode. Answer the question, then offer next steps.
- **Don't re-plan finalized plans** — when continuing from a prior session with an existing plan, start implementing immediately. Do not re-audit, re-plan, or rewrite plans that were already approved.
- **Don't deploy without explicit confirmation** — always present the change summary and wait for user approval before `git push`. Never auto-deploy.

### Knowledge Base
The `kb/` directory contains structured articles on bot history, design decisions, failure post-mortems, and strategy documentation.

**On session start, run:** `find kb/ kb-research/ -name '*.md' -type f | head -100` — this ensures you always know what KB articles exist before doing anything.

**On any deep question about why something works the way it does, what went wrong before, or what decisions were made:** Read `kb/_index.md` first, then read relevant articles before responding.

**After any significant session** (bug fix, strategy change, new insight, loss analysis): Update relevant kb/ articles and the index. Follow `kb/_meta/MAINTENANCE.md` for conventions.

### Research Knowledge Base
The `kb-research/` directory contains compiled findings from past research sessions — ML evaluations, market expansion analysis, sports model derivations, vendor comparisons, etc. Check `kb-research/_index.md` when investigating topics that may have been researched previously. See `kb/_meta/MAINTENANCE.md` for conventions on maintaining both KBs.

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

## Skill Routing Guide

When the user's request is ambiguous, use these rules to pick the right skill.

### Operations
| Skill | Use when... |
|---|---|
| `/status` | Quick pulse check — "how's it going?", "anything happening?", "is data flowing?" |
| `/investigate` | Emergency — anomaly, unexpected trade, dashboard alert, suspected bug |
| `/deploy` | Push to main + full deploy verification (syntax, constants, VPS, DB entries) |
| `/data-health` | Instrumentation quality — NULL rates, data gaps, shadow coverage |

### Performance & Shadow Analysis
| Skill | Use when... |
|---|---|
| `/audit` | Run one system's audit script with regime-filtered numbers and Wilson CIs |
| `/alpha-audit` | Cross-system funnel analysis — rejections, counterfactual PnL, shadow readiness |
| `/shadow` | Bird's-eye summary across ALL 5 shadow/observation systems |
| `/variant-status` | Focused shadow variant comparison (A1/A2/A3, hourly alts) with Kelly-sized PnL |

### Deep Research (single-system)
| Skill | Use when... |
|---|---|
| `/15m-alpha` | 13-section 15M deep dive (regime, price tiers, STC, calibration, losses) |
| `/hourly-alpha` | 12-section hourly research (600+ config grid search, robustness) |
| `/spx-alpha` | 15-section SPX research (EGARCH blend, VIX regimes, readiness checklist) |
| `/weather-alpha` | 18-section weather research (ensemble quality, HRRR, bias correction) |
| `/sports-alpha` | 16-section sports research (SPRT test, deficit analysis, CLV) |

### Specialized
| Skill | Use when... |
|---|---|
| `/maker-cost` | Maker vs taker opportunity cost — fill rates, unfilled cost |
| `/no-side` | NO-side shadow data — volume, pricing verification, settlements |
| `/weekend-discount` | Weekend/overnight edge discount — live performance and shadow tails |
| `/research-package` | Compile self-contained data package for external researcher |

### Knowledge Base
| Skill | Use when... |
|---|---|
| `/kb-lint` | Health check — stale articles, dead code refs, broken links, drift, missing coverage |
| `/kb-ingest` | Post-session capture — turn conversation findings into KB articles |
| `/kb-evolve` | Structural maintenance — merge overlaps, archive dead weight, fix contradictions, rebalance |

### Decision Rules for Ambiguous Pairs

- **Status vs Audit:** Quick 30-second answer → `/status`. Statistically rigorous numbers with CIs → `/audit`.
- **Audit vs Alpha-Audit:** One system's raw numbers → `/audit`. Cross-system funnel tracing, "where are we leaving money?" → `/alpha-audit`.
- **Shadow vs Variant-Status:** All 5 systems at a glance → `/shadow`. Deep dive on specific variants with promotion timeline → `/variant-status`.
- **Alpha-Audit vs *-Alpha:** Pipeline-wide opportunity analysis → `/alpha-audit`. Single-system deep research with grid search and config recommendations → use the system-specific `-alpha` skill.
- **Status vs Variant-Status:** "How's it going?" → `/status`. "How are A1 and A2 looking?" with PnL numbers → `/variant-status`.

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
- **Any new `sqlite3.connect()` call MUST include `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=10000`** — multiple threads (bot, supabase_sync, sports, analyst) share state.db. Missing WAL or timeout = "database is locked" errors under contention. (Learned: sports_engine.py missing busy_timeout caused ~2000 errors/8hr, Mar 2 2026. analyst.py missing busy_timeout contributed to contention, Mar 9 2026.)
- **Never commit inside a loop — always batch** — per-row `conn.commit()` in a loop multiplies the contention window with concurrent readers (supabase_sync runs 165 queries/10s). Accumulate writes, commit once at the end. (Learned: `_poll_evaluated_opportunities()` doing 91 individual commits caused "database is locked" burst + CPU spike, Mar 9 2026. See POSTMORTEMS.md PM-001.)
- **Performance analysis must filter to current config regime** — losses under old configs (old sizing, old calibration, pre-maker-only) are not relevant to current optimization decisions. Always identify when major config changes happened and filter accordingly.
- **After ANY bug fix**: do root cause analysis, explain why it happened, and add a regression test to prevent recurrence. Never just fix and move on.
- **All sim PnL and counterfactual analysis MUST use actual Kelly sizing** — never use 1-contract flat sizing. Position size comes from the Kelly formula with the bot's actual risk parameters. Flat sizing produces misleading PnL numbers.
- **Never present analysis without checking actual data first** — no assumptions about column values, schema, enum strings, or data shape. Always run `PRAGMA table_info()` and `SELECT DISTINCT` before building queries. (Learned: wrong column values, wrong regime detection, wrong filter_stage assumptions all caused bad analysis.)
- **Dashboard changes require KB + docs consistency** — `dashboard_snapshot.py` and `dashboard/index.html` (in gabekagan/gh-pages) are governed by the Option A→B plan in `kb/decisions/dashboard-overhaul-plan.md`. Before adding a new snapshot key, check: is there already a v2 section it belongs in? Is the frontend renderer wired? Does it need a Supabase column + RLS check? Every dashboard change that adds or removes a key must update both sides in the same commit and the KB article if it's structural.
- **Never use `PRAGMA wal_checkpoint(TRUNCATE)` — use PASSIVE** — TRUNCATE requires an exclusive lock that blocks all readers/writers. With supabase_sync running 192 SELECTs every 30s, TRUNCATE creates a deadlock triangle: checkpoint waits for reader to finish → reader holds shared lock → settlement writer waits for checkpoint's exclusive lock. PASSIVE checkpoints whatever pages it can without blocking. (Learned: 11,258 "database is locked" errors in 12h, Mar 16 2026. Root cause: TRUNCATE + supabase_sync reader + 228-row settlement batch.)
- **Keep DB write batches small (≤50 rows per commit)** — large batches hold the write lock long enough to conflict with concurrent readers and checkpoints. Settlement Phase 2 now chunks into batches of 50. (Learned: 228-row batch from weather expansion held lock long enough to deadlock, Mar 16 2026.)
- **Update docs with code changes** — if you change a config value, threshold, or shadow strategy status, update the corresponding claim in README.md, whitepaper.md, whitepaper_investor.md, and/or CLAUDE.md in THE SAME COMMIT. Run `python3 scripts/doc_drift_check.py` before committing to verify. (Learned: 3+ full manual doc rewrites caused by accumulated drift, Mar 2026.)

## Anti-Patterns — Do NOT Do These

- **Don't refactor bot.py into multiple files** — systemd/start.sh/deploy pipeline all depend on the single-file structure. Engines (spx_engine.py, weather_engine.py, analyst.py) are the exception because they run as separate threads/processes.
- **Don't add async** — the bot is synchronous by design, threading is used only for WS feeds and engine threads
- **Don't suggest switching from SQLite** — single-writer is fine for our throughput, latency matters, and the DB is local to the VPS
- **Don't suggest switching from JSONL journals** — they're append-only, zero-overhead, and rotated daily via cron
- **Don't create test files without being asked** — focus on the change, add regression tests only when specified in Critical Rules
- **Don't refactor code "for readability" during a bugfix** — fix the bug, nothing else
- **Don't change Kelly fraction, blend weights, or edge thresholds without data justification** — these are tuned from backtests and live data

## Project Structure

- `bot.py` — Main bot (~21482 lines, all trading logic)
- `analyst.py` — AI analyst system (news sentiment, loss analysis, Telegram alerts)
- `spx_engine.py` — SPX hourly market engine (Polygon.io price feed, EGARCH, RK, VIX integration)
- `weather_engine.py` — Weather ensemble fetcher + probability model (Open-Meteo GFS/ECMWF)
- `market_config.py` — Centralized MarketTypeConfig for all product types (validates against bot.py at startup)
- `dashboard_snapshot.py` — Builds dashboard state snapshots (used by Supabase syncer)
- `start.sh` — Startup script (activates venv, sources .env, runs bot)
- `fifteenm_shadow.py` — 15M shadow strategies (A1 RecalibratedEGARCH, A2 LightGBM, A3 EGARCH gating, A4 LateWindow)
- `hourly_alt_shadow.py` — Hourly alternative shadow strategies (HAR-RV, market-making sim)
- `sports_engine.py` — Sports comeback market engine (ESPN live data, Bayesian posterior)
- `auditor.py` — Deterministic health checks, runs hourly via cron, Telegram alerts
- `auditor_state.db` — Alert deduplication state (gitignored via *.db pattern)
- `researcher.py` — 3x daily performance reports to Telegram, runs via cron (7:30am/12:30pm/7:30pm ET)
- `researcher_state.db` — Report history and period tracking (gitignored via *.db pattern)
- `.github/workflows/deploy.yml` — Auto-deploy to VPS on push to main

## bot.py Layout (approximate line ranges)

- **Lines 1–600:** Imports, constants, config (trading params, API config, volatility engine, calibration, sizing, execution)
- **Lines 600–620:** Utility functions (FP/dollar string helpers)
- **Lines 620–815:** `evaluate_execution_strategy()` — diagnostic only, does not control execution
- **Lines 820–1085:** `KalshiClient` — API wrapper, order management, orderbook fetching
- **Lines 1089–1150:** `Logger` — JSONL trade/event logging
- **Lines 1152–1185:** `TelegramNotifier` — Telegram alerts
- **Lines 1188–2480:** `StateManager` — DB init (`_create_tables` at 1209), position tracking, settlement processing
- **Lines 2483–2635:** `CoinbaseFeed` — WebSocket price feed, OHLCV snapshots
- **Lines 2645–2970:** `KalshiFeed` — WebSocket orderbook stream, fill detection
- **Lines 2977–3050:** `DeribitDVOLFetcher` — Implied volatility index
- **Lines 3058–3370:** `CrossExchangeFeed` — Kraken, Binance, Bybit order flow
- **Lines 3376–3450:** `CoinGlassFetcher` — Derivative funding rates
- **Lines 3453–3730:** `OrderFlowEngine` + `KalshiOrderFlowTracker` — Kalshi OB flow signals
- **Lines 3736–4640:** `VolatilityEngine` — Realized Kernel (RK), GARCH, RV estimation, TV RK weights
- **Lines 4643–4805:** `ProbabilityEngine` — Z-score, NIG CDF, market blend
- **Lines 4809–5670:** `CalibrationEngine` — Beta/Platt/isotonic, `_CAL_REGISTRY`, shadow pipeline
- **Lines 5674–9844:** `OpportunityScanner` — `scan()` at 5904, market discovery, filter pipeline, shadow signals
- **Lines 9848–12220:** `OrderExecutor` — `execute()` at 9945, maker→taker escalation, fill detection
- **Lines 12223–12980:** `SettlementTracker` — Settlement detection, CalEngine routing, PnL computation
- **Lines 12983–13065:** `discover_active_windows()` — Market discovery from Kalshi API
- **Lines 13068–14030:** `MainLoop` — `run()` at 13902, init, WS subscription, periodic tasks
- **Lines 14030–14043:** Entry point

## Current State (Mar 20, 2026)

- **OBSERVATION_MODE = False** — LIVE TRADING with real money
- **15M live assets:** BTC (88c+), ETH (75c+, 50-contract cap sub-80c), SOL (86c+, taker-first), XRP (92c+)
- **XRP_15M_SHADOW = False** — XRP promoted to live at 92c+ (data: 41W/2L, 95.3% WR)
- **SOL_TAKER_FIRST = True** — SOL bypasses maker entirely, direct IOC at all STC
- **Decided contracts LIVE:** T1 (z≤-5), T1B (z≤-4, 95c+), T2 (z≤-3, 93-96c), T2-Z25 (z≤-2.5, 93-96c) — **T2-Z2 SHADOWED** (commit 97a365f Apr 1, -$313 on 47 trades, no structural edge in z∈[-2.5,-1.75]). All live tiers use 20% fixed sizing. See `kb/concepts/dc-strategy.md` for canonical reference.
- **Overnight discount LIVE:** OVERNIGHT_DISCOUNT_LIVE=True on weekday 04-11 UTC — 89c+, STC<=600s, no DC overlap; sub-89c and STC>600s remain shadow
- **Loss burst cooldown LIVE (Apr 11):** LOSS_COOLDOWN_ENABLED=True, per-asset 2h lockout after any 15M loss. Data: 53/82 losses (30d) in 15 bursts, counterfactual +$441/30d. First-ship is per-asset (conservative); escalate to global if cross-asset correlation persists.
- **Weather NO-side unblocked (Apr 11):** Live NO candidate block was nested inside broken model-edge gate → 0 trades Apr 4-11. Unnested; should fire on NO≤40c, STC≥16h, assumed-prob 0.70. See kb/failures/weather-no-candidate-never-fires.md
- **Hourly FULLY DISABLED (Apr 18):** Both `HOURLY_LIVE_ENABLED=0` and `HOURLY_NO_SIDE_LIVE=0` on VPS. YES-side was turned off earlier; NO-side verification killed Apr 18 by user. Pre-kill NO-side data: BTC NO 40-54c had 53.9% WR (n=1,113, z=2.61, p=0.005), time-split stable. Kill switches remain in place — re-enable by flipping env vars and restarting service.
- **STC window:** scan 0-900s, core live 0-300s, extended live 300-600s (per-asset higher floors), shadow 600-900s
- **STC extended zone:** 300-600s live with higher floors — BTC 93c+, ETH 90c+, SOL 95c+, XRP 92c+ (data: 98.8% WR, n=83)
- **SOL sub-86c gate:** SOL_LOW_ENTRY_STC_GATE=True — blocks SOL ≤85c at STC≥300s (data: 78.3% WR -$289; near-expiry 100% WR preserved)
- **STC sizing scaler:** STC_SIZING_SCALER_ENABLED=True — contracts *= 300/STC for 15M at STC>300s. Applied in main pipeline + overnight + weekend discounts. kelly_f stays pure.
- **LPNE:** LPNE_ENABLED=True — BTC 80-87c near-expiry (STC 10-120s), intercepts at price floor. 50ct fixed. Prob gate: final_prob >= price/100. BTC NBBO gate lowered to 80c. BTC_MIN_ENTRY_PRICE unchanged at 88c.
- **Position price monitor:** POSITION_PRICE_MONITOR_ENABLED=True — logs yes_ask/bid for held 15M positions via WS (zero API cost). Change-only dedup. New `position_price_observations` table.
- **Hourly:** DISABLED as of Apr 18 (both kill switches 0). Config still defined — sub-60c BTC+ETH only, taker-only IOC, fixed 25-contract sizing, 10% bankroll fraction, max 5% edge cap, STC 600-1800s. Calibration disabled (passthrough+T=1.45). SOL/XRP excluded (XRP 42.9% WR = toxic). To re-enable: set `HOURLY_LIVE_ENABLED=1` (YES) and/or `HOURLY_NO_SIDE_LIVE=1` (NO) in VPS .env + restart.
- **SPX Hourly:** Observation mode (SPX_HOURLY_OBSERVATION_ONLY = True) — was briefly live Mar 17, reverted due to Polygon 403 breaking vol engine. SPX-D CalEngine, 90c+ floor, eighth-Kelly, no market blend
- **Weather:** Observation mode (WEATHER_OBSERVATION_ONLY = True) — NWP ensemble model (GFS+ECMWF, 82 members), 19 cities. WEATHER_NO_SIDE_LIVE = True (NO 36-40c, STC ≥ 16h, 1-contract). 36c floor added Apr 20 (data: sub-36c cohort 4/33 = 12.1% WR, Wilson UB 27.7% < 40% breakeven)
- **Sports:** Observation mode (SPORTS_OBSERVATION_ONLY = True) — hardcoded, never live without explicit promotion. Basketball best group (69.2% WR, n=39), SPRT still CONTINUE_COLLECTING. FIXED Apr 12: 31-day data outage from MLB code mismatch (CHW→CWS, ARI→AZ, OAK→ATH) + LA→LAK broke NHL + hockey un-excluded for playoff data collection
- **15M Shadow:** A1 (RecalibratedEGARCH), A2 (LightGBM), A3 (EGARCH gating), A4 (LateWindow 55-74c) — all shadow-only in fifteenm_shadow.py
- **CalibrationEngine:** Hourly data excluded from 15M training; hourly CalEngine disabled. Per-city weather CalEngines and per-sport-group CalEngines learning in shadow
- **Weekend discount LIVE:** WEEKEND_DISCOUNT_LIVE=True on Sat/Sun — 90c+, STC<=600s, no DC overlap; sub-90c and STC>600s remain shadow
- **Tests:** 3094 tests across 15+ test files

## Key Config Values (bot.py)

| Config | Value | Notes |
|--------|-------|-------|
| OBSERVATION_MODE | False | LIVE trading |
| MIN_ENTRY_PRICE | 75 | Cents (global floor — lowered from 80 for ETH 75-79c) |
| BTC_MIN_ENTRY_PRICE | 88 | Cents (data: 88c = 96.2% WR on n=53 shadow, 96.3% on n=27 recent) |
| ETH_MIN_ENTRY_PRICE | 90 | Cents (raised from 85 — data: ETH 85-89c 86.2% WR on 65 trades, -$23.76 PnL; 90c+ is 95.2% WR) |
| SOL_MIN_ENTRY_PRICE | 86 | Cents (raised from 80: SOL@85c 68.2% WR -$496 on 22 trades vs 86c 94.4% WR +$375 on 36 trades) |
| ETH_SUB80_POSITION_CAP | 50 | Max contracts for ETH 75-79c (half-Kelly clamp [20,50]) |
| XRP_MIN_ENTRY_PRICE | 92 | Cents (data: PnL negative at every floor <90c, PF=1.68 at ≥92c) |
| MAX_ENTRY_PRICE | 99 | Cents |
| MIN_EDGE_PCT | 0.25 | Flat fallback for execution paths (was 0.7) |
| MIN_EDGE_BY_PRICE | 0.20%-1.0% | Price-dependent: 80-88c→0.25%, 89-90c→0.25%, 91-92c→0.20%, 93-94c→0.50%, 95-96c→0.75%, 97-99c→1.0% |
| MARKET_BLEND_W | 0.40 | 60% model, 40% market (data: model underconfident 0.8-2.1pp at 90%+) |
| MAX_RISK_PER_TRADE | 0.25 | Max 25% bankroll per trade |
| MAX_SECONDS_BEFORE_CLOSE | 900 | 15 min before close (600-900s shadow, 0-600s live) |
| STC_SHADOW_THRESHOLD | 600 | 15M trades above this STC are shadow-only |
| STC_EXTENDED_LIVE_FLOOR | 300 | 300-600s zone: per-asset higher floors apply |
| STC_EXTENDED_BTC_MIN_PRICE | 93 | BTC floor for 300-600s (data: 93c+ = 98.1% WR, n=52) |
| STC_EXTENDED_ETH_MIN_PRICE | 90 | ETH floor for 300-600s (same as main floor) |
| STC_EXTENDED_SOL_MIN_PRICE | 95 | SOL floor for 300-600s (data: 95c+ = 100% WR, n=14) |
| STC_EXTENDED_XRP_MIN_PRICE | 92 | XRP floor for 300-600s (same as main floor) |
| SOL_LOW_ENTRY_STC_GATE | True | Block SOL ≤85c at STC≥300s (data: 78.3% WR -$289; <300s is 100% WR +$228) |
| STC_SIZING_SCALER_KNEE | 300 | Seconds — start scaling contracts by 300/STC above this (data: 5m+ WR drops) |
| STC_SIZING_SCALER_ENABLED | True | Universal STC scaler: contracts *= 300/STC for 15M at STC>300s |
| LOSS_COOLDOWN_ENABLED | True | Per-asset 2h 15M lockout after any loss (data: 53/82 losses in bursts, +$441/30d counterfactual) |
| LOSS_COOLDOWN_SECONDS | 7200 | 2-hour cooldown window — first-ship conservative; may escalate to global later |
| LPNE_ENABLED | True (env var) | BTC 80-87c near-expiry overlay (STC 10-120s, prob >= price/100) |
| LPNE_FIXED_CONTRACTS | 50 | Fixed sizing for LPNE (LOW_STC_SIZING_CAP halves most to 25) |
| LPNE_MAX_CONCURRENT | 2 | Max simultaneous LPNE positions |
| POSITION_PRICE_MONITOR_ENABLED | True | Post-entry price logging via WS (change-only dedup) |
| XRP_MAX_RISK_PER_TRADE | 0.15 | XRP: 15% per-trade (was 12% — regime cap removal gives full balance) |
| BTC_MAX_RISK_PER_TRADE | 0.15 | BTC: 15% per-trade (was 12% — regime cap removal gives full balance) |
| SOL_MIN_EDGE | 0.010 | SOL-specific edge floor (data: >=1.0% = 94.2% WR on 258 trades; <1.0% drops to 82%) |
| SOL_TAKER_FIRST | True | SOL bypasses maker entirely, direct IOC at all STC |
| SOL_RESCUE_CONTRACT_CAP | 25 | SOL rescue sizing clamp (Apr 19: 5 losses avg 56ct × 89c → capped to ~$22/loss from $45-72) |
| IOC_TICKER_COOLDOWN | 15 | Seconds cooldown per ticker after IOC attempt (was 60) |
| IOC_RETRY_OFFSET | 1 | Cents above ask for taker-first IOC + retry offset |
| MAX_CONCURRENT_TAKER_PER_ASSET | 3 | Safety cap on simultaneous taker positions per asset |
| DIP_ADDON_ENABLED | False | Killed — 55.2% WR, no edge |
| DECIDED_T1_ENABLED | True | Decided contract overlay: z≤-5, any price (env var) |
| DECIDED_CONTRACT_Z_T1B | -4.0 | T1B z-score threshold (between T1's -5 and T2's -3) |
| DECIDED_CONTRACT_T1B_MIN_PRICE | 95 | T1B minimum price in cents |
| DECIDED_T1B_ENABLED | True | Decided contract overlay: z≤-4, 95c+ (env var) |
| DECIDED_T2_ENABLED | True | Decided contract overlay: z≤-3, 93-96c (env var) |
| DECIDED_T2_Z25_ENABLED | True | Decided contract overlay: z≤-2.5, 93-96c (env var, data: 7/7 WR) |
| DECIDED_T2_Z2_ENABLED | False (default "0") | **SHADOWED** Apr 1 — z∈[-2.5,-1.75], -$313 on 47 trades, last fire Mar 31 |
| DECIDED_CONTRACT_Z_T2_Z2 | -1.75 | T2-Z2 expanded from -2.0 pre-shadow (data: 114/115 = 99.1% WR in -2.0 to -1.75) |
| DECIDED_CONTRACT_RISK | 0.20 | Fixed 20% bankroll per signal for T1/T1B/T2 (was 12.5% pre-3d03e20) |
| DECIDED_CONTRACT_T2_Z25_RISK | 0.10 | T2-Z25 fixed sizing (cut from 0.20 Apr 21 — 14d bleed -$95, Apr 21 XRP -$132) |
| DECIDED_CONTRACT_T2_Z2_RISK | 0.20 | T2-Z2 fixed sizing (moot — shadowed) |
| DECIDED_CONTRACT_MAX_WINDOW_RISK | 0.35 | Hard cap across all DC signals in one window (was 0.25 pre-3d03e20) |
| SOL_DC_RISK_TIERS | [(97, 0.05), (95, 0.10)] | SOL-specific price-tiered DC risk: ≥97c→5%, 95-96c→10%, <95c→20% |
| OVERNIGHT_DISCOUNT_LIVE | True | Promote overnight discount to live trading (kill switch) |
| OVERNIGHT_DISCOUNT_MIN_PRICE | 89 | Cents — 89c+ floor for live overnight discount trades |
| OVERNIGHT_DISCOUNT_MAX_STC | 600 | STC gate for live overnight discount trades |
| HOURLY_LIVE_ENABLED | env var (0 on VPS as of Apr 18) | Kill switch — must be "1" on VPS to trade (default "0" = observation) |
| HOURLY_OBSERVATION_ONLY | not HOURLY_LIVE_ENABLED | Derived from kill switch |
| HOURLY_MAX_ENTRY_PRICE | 59 | Sub-60c only — edge lives at low prices, 70-79c death zone |
| HOURLY_BANKROLL_FRACTION | 0.10 | Hourly sizes off 10% of balance (like SPX's 0.15) |
| HOURLY_FIXED_CONTRACTS | 25 | Fixed sizing — bypass Kelly entirely (raised from 10) |
| HOURLY_MAX_EDGE | 0.05 | Reject >5% edge (10%+ zone has 24.2% WR — edge inversion) |
| HOURLY_NO_SIDE_LIVE | env var (0 on VPS as of Apr 18) | Kill switch — must be "1" on VPS to trade NO-side (default "0") |
| HOURLY_NO_MIN_PRICE | 40 | Minimum NO entry price (cents) |
| HOURLY_NO_MAX_PRICE | 54 | Maximum NO entry price (cents) |
| HOURLY_NO_FIXED_CONTRACTS | 1 | Flat 1-contract — verification mode |
| HOURLY_NO_KILL_THRESHOLD | -2000 | Auto-disable if cumulative hourly NO PnL < -$20 |
| HOURLY_TAKER_ONLY | True | IOC only — no maker orders, no per-asset lock contention with 15M |
| HOURLY_MARKET_BLEND_W | 0.40 | Optimal Brier per 134K simulation |
| HOURLY_MIN_ENTRY_PRICE | 50 | Floor for data collection |
| HOURLY_MAX_RISK_PER_TRADE | 0.15 | 60% of 15M's 0.25 |
| HOURLY_TEMPERATURE_T | 1.45 | Softens overconfident probs: 95%→88.4% |
| HOURLY_KELLY_FRACTION | 0.25 | Quarter-Kelly (unused — fixed sizing active) |
| HOURLY_CALIBRATION_ENABLED | False | Engine disabled — passthrough + T=1.45 |
| HOURLY_MIN_STC_ENTRY | 600 | 10 min minimum (5-10m zone 56.5% WR — too thin) |
| HOURLY_MAX_STC_ENTRY | 1800 | 30 min maximum (25-30m sweet spot at 69.4% WR) |
| HOURLY_EXCLUDED_ASSETS | {SOL, XRP} | BTC+ETH only — XRP 42.9% WR (toxic), SOL marginal |
| HOURLY_MAX_POSITIONS_PER_WINDOW | 2 | Limit correlated exposure |
| HOURLY_MAX_WINDOW_RISK | 0.15 | Max aggregate risk per hourly window |
| SPX_HOURLY_OBSERVATION_ONLY | True | Reverted — Polygon 403 broke vol engine (was briefly live Mar 17) |
| SPX_HOURLY_MIN_ENTRY_PRICE | 90 | Cents (SPX-C: 90.9% WR at 90c+) |
| SPX_HOURLY_MAX_ENTRY_PRICE | 99 | Cents |
| SPX_HOURLY_MARKET_BLEND_W | 0.00 | No blend — CalEngine calibration only (SPX-D) |
| SPX_HOURLY_TEMPERATURE_T | 1.0 | No temperature correction yet — need data |
| SPX_HOURLY_KELLY_FRACTION | 0.125 | Eighth-Kelly: ultra-conservative |
| SPX_HOURLY_MAX_RISK_PER_TRADE | 0.10 | Conservative (down from 0.15) |
| SPX_HOURLY_FEE_MULTIPLIER_TAKER | 0.035 | Finance category — half of crypto's 0.07 |
| SPX_HOURLY_FEE_MULTIPLIER_MAKER | 0.0 | Kalshi charges $0 on maker fills |
| SPX_HOURLY_BANKROLL_FRACTION | 0.15 | SPX sizes off 15% of total balance — crypto unaffected |
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
| WEATHER_NO_SIDE_LIVE | True | LIVE — NO 36-40c, STC ≥ 16h, 1-contract |
| WEATHER_NO_MIN_PRICE | 36 | Floor added Apr 20 — sub-36c cohort 4/33 = 12.1% WR backtest |
| WEEKEND_DISCOUNT_LIVE | True | Weekend edge discount promoted to live (Sat/Sun only) |
| WEEKEND_DISCOUNT_MIN_PRICE | 90 | Cents — 90c+ floor for live weekend discount trades (raised from 89 to match ETH floor) |
| WEEKEND_DISCOUNT_MAX_STC | 600 | STC gate for live weekend discount trades |
| WEEKEND_EDGE_DISCOUNT | 0.60 | 40% edge reduction applied on weekends (unchanged) |

## Calibration Pipeline

1. Raw statistical probability (from volatility model)
2. Beta calibration (CalibrationEngine — trained on 15M data only, hourly excluded)
3. **Hourly temperature scaling** (Layer 1): T=1.45 softens overconfident probs (95%→88.4%). Applied before OFA/dynamic cap. 15M unaffected.
4. Dynamic cap: **bypassed** when learned calibration is active (`is_learned_method_active()` → uses 0.999 safety ceiling instead of the cap schedule). Cap schedule only applies during startup before training.
5. Market blend: 40% weight toward market price (60% model)
6. Fee-adjusted edge check: price-dependent minimum (0.25% at 80-90c up to 1.0% at 97c+)

## Hourly Three-Layer Optimization

Researcher-recommended filters to fix hourly overconfidence, timing, and correlation issues. All run under `HOURLY_OBSERVATION_ONLY = not HOURLY_LIVE_ENABLED` — observation gate is the shadow mechanism. Filters placed LATE in pipeline so all upstream data is still logged for counterfactual analysis.

| Layer | Filter Stage | Purpose |
|-------|-------------|---------|
| 1 | Temperature scaling (T=1.45) | Softens 15M calibration that doesn't transfer to hourly |
| 2 | STC timing (120-3600s) | Expanded for observation data collection (2 min to 60 min) |
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
| Dip addon (DIP_ADDON_ENABLED=False) | **Killed** — 55.2% WR (29 settled), no edge |
| 15M shadow A4 (LateWindow 55-74c) | Shadow — low-price late-window approach |
| JUMP_ADAPTIVE, RK_ADAPTIVE | **Promoted** — driving live |
| EGARCH core + blend | **Promoted** — driving live |
| TV RK weights | **Promoted** — driving live |
| Decided contracts (T1+T1B+T2+T2-Z25) | **Promoted live** — T1 z≤-5 (any price ≥93c), T1B z≤-4 (95c+), T2 z≤-3 (93-96c), T2-Z25 z≤-2.5 (93-96c). All @ 20% fixed risk. T2-Z2 shadowed. See kb/concepts/dc-strategy.md |
| DC shadow: dc_shadow_t1b_93c | Shadow — T1B at 93c+ floor variant |
| DC shadow: dc_shadow_t2_90c | Shadow — T2 at 90c+ floor variant |
| DC shadow: dc_shadow_t2_90c_xrp | Shadow — T2 at 90c+ XRP-only variant |
| DC shadow: dc_shadow_no_side | Shadow — NO-side decided contract variant |
| SOL taker-first | **Promoted** — SOL bypasses maker, direct IOC |
| XRP live (was shadow) | **Promoted** — XRP live at 92c+ floor |
| Weekend edge discount | **Promoted** — live on Sat/Sun (90c+, STC<=600s, no DC overlap); sub-90c/STC>600s shadow |
| Overnight edge discount | **Promoted** — live on weekday 04-11 UTC (89c+, STC<=600s, no DC overlap); sub-89c/STC>600s shadow |
| Hourly NO-side (40-54c) | Shadow/Verification — 1-contract flat, model-filtered NO at 40-54c, env var kill switch |
| Low-price shadow (70-79c) | Shadow — dual-sizing sim (full Kelly vs capped LP_KELLY=0.25, LP_MAX_RISK=0.10) with correlation tracking |

## Order Execution

- **Default: maker-first** (post_only=True), escalates to taker if unfilled
- **SOL exception: taker-first** — SOL_TAKER_FIRST=True bypasses maker, direct IOC at all STC
- **Decided contract overlay**: T1 (z≤-5, any price), T1B (z≤-4, 95c+), and T2 (z≤-3, 93-96c) route to direct taker for near-certain settlements
- **Taker allowed at all STC** — MAKER_ONLY_THRESHOLD=0
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
- **Series (weather):** 19 cities — KXHIGHNY, KXHIGHCHI, KXHIGHMIA, KXHIGHDEN, KXHIGHLAX, KXHIGHAUS, KXHIGHTATL, KXHIGHTSFO, KXHIGHTDAL, KXHIGHTPHX, KXHIGHPHIL, KXHIGHTMIN, KXHIGHTSEA, KXHIGHTHOU, KXHIGHTBOS, KXHIGHTLV, KXHIGHTOKC, KXHIGHTDC, KXHIGHTNOLA

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
