# KB Change Log

Append-only chronological record of KB operations. Each entry starts with
`## [YYYY-MM-DD] type | description` for grep-parseable filtering.

Usage: `grep "^## \[" kb/log.md | tail -10` for recent entries.

---

## [2026-04-03] init | Knowledge base created with 10 seed articles
Initial seed from user: 6 concepts, 3 failures, 0 decisions. Covering calibration, Kelly sizing, DC strategy, SOL dynamics, shadow framework, market microstructure, HWM bugs, BLR calibrator, T2-Z2 losses.

## [2026-04-03] expand | 23 new articles across all categories
Major expansion: EGARCH model, execution layer, infrastructure, Supabase, dashboard, monitoring, price feeds, per-asset rules, weather system, stacking infrastructure, 4 strategies (TM, bracket NO, overnight discount, hourly), 4 failures (Polygon 403, evaluations sync, SOL adverse selection, regime cap), 5 decisions (BLR removal, SOL edge floor, XRP promotion, regime cap removal, stacking enabled). Total: 32 articles.

## [2026-04-03] expand | 8 more articles closing identified gaps
Sports engine, SPX engine, CalEngine registry, 15M shadow variants, loss clustering failure, fee optimization, position reconciliation, SOL taker-first decision. Total: 40 articles.

## [2026-04-03] expand | 18 more articles from deep audit
Drawdown scaler, scan loop, probability pipeline, edge thresholds, balance tracking, database contention, IOC sub-floor fill, dedup tuple crash, shadow callsite variable, T2-Z2 shadowed, hourly promotion, TM 97c promotion, market config system, journal system, guard-aware protocol, test suite, deploy pipeline, dedup system. Total: 58 articles.

## [2026-04-03] expand | 8 more articles (execution + methodology)
DC execution mechanics, addon strategies, shadow expansion variants, watchdog, config/models extraction decision, data analysis rules, hourly alt shadows, Kalshi API. Total: 66 articles.

## [2026-04-03] trim | Deleted 19 textbook concept articles
Removed: kelly-sizing, market-microstructure, calibration, shadow-framework, supabase-data-layer, dashboard, infrastructure, journal-system, monitoring-alerts, price-feeds, scan-loop, probability-pipeline, egarch-model, dedup-system, watchdog, test-suite, deploy-pipeline, kalshi-api, hourly-alt-shadows. Folded orphaned specifics (shadow promotion criteria → shadow-expansion-variants, probability parameters → edge-thresholds). Total: 47 articles.

## [2026-04-03] meta | Added YAML frontmatter to all 59 articles
Status, updated, tags on every article. Failures include severity. Decisions include date. Created Dataview dashboard at kb/_meta/dashboard.md.

## [2026-04-04] integrate | Research KB (kb-research/) integrated
11 research articles from past Claude sessions added as kb-research/. Cross-linked 8 kb/ articles to research backstory. Added research KB conventions to MAINTENANCE.md and CLAUDE.md. Weather NWP verdict merged into weather-system.md.

## [2026-04-04] ingest | BTC loss investigation filed
kb-research/bot/btc-loss-investigation-apr4.md — 41 passthrough trades, p=0.39, entry price caps counterproductive. 30ct cap recommendation.

## [2026-04-04] ingest | Backtesting harness research filed
kb-research/bot/backtesting-harness.md — Two rounds of broken results documented. Validated filter mode: SOL confirmed profitable (+$71). Methodological lessons on balance-dependent re-sizing.

## [2026-04-04] update | per-asset-rules.md corrected
SOL section updated with backtester-validated data. Risk cap noted as 12%. Cap 30ct tradeoff documented.

## [2026-04-04] fix | Audit script bugs fixed across all 5 systems
Major fixes: (1) All shadow scripts used maker fees ($0) instead of taker fees — hourly, SPX, weather, sports PnL was inflated. SPX dropped from $124K→$14K sim PnL. (2) 15M edge tiers were stale (4/6 wrong). (3) SPX price buckets were crypto-style (70-99c) missing sub-80c data. (4) Sports MAX(fav_won) dedup inflated WR (58.1%→55.5%). (5) Regime detection 30→180 day window. Also fixed doc drift: BTC floor 89→88c, edge thresholds in whitepapers/DOC_STATE/skills, HOURLY_FIXED_CONTRACTS 10→25, BTC/XRP risk caps 12%→15%, WEATHER_NO_SIDE_LIVE=True, bot.py line count, test count.

## [2026-04-04] fix | Dashboard pipeline deep audit — 22 bugs fixed
Supabase sync: evaluations/rejections SELECT * sent ~30 unknown columns → permanent 400 failures (data lost since columns were added). Fixed with explicit column lists. Trades sync now incremental (was full-table re-send every 30s). dashboard_snapshot.py: calibration_gap dead (wrong column name `created_at`→`evaluation_time`), loss_clustering cache key mismatch (missing 5/6 cycles), 42 silent `except: debug` → `warning`, cleanup_expired_resting_orders removed from sync thread, balance_history_4h always set. Dashboard HTML: NO-side wins showed as losses (side-blind logic), ROI used HWM not initial_deposit, status strip WR ignored scope toggle, added Realtime disconnect handling. Contract test: 8 missing required snap keys added, path fixed.

## [2026-04-04] feat | Dashboard UX overhaul — 3 waves
Wave 1 (UX): Active Positions/Orders moved above analytics (first thing after header). Shadow tier collapsed by default, restructured into Live Sub-Strategies → Promotion Pipeline → Research (collapsed). Duplicate panels hidden (Balance card, Total PnL, Cross-Exchange). Time-since-last-trade color escalation (green/amber/red). Skip DOM updates for collapsed tiers. Wave 2 (Design): Gold accent system (--gold-0: #C5A855) for Jewish design continuity (techelet+gold palette). Frank Ruhl Libre Hebrew font via Google Fonts. Gold section divider accents alongside Gedolim quotes. Trade outcome strip (last 40 trades as win/loss dots). WCAG AA contrast fix. Wave 3 (Analytics): Rolling 7d/30d performance card, calendar PnL heatmap (35-day color grid), Sortino+Calmar ratios in Risk Metrics, drawdown shading on equity curve. Mobile: inline grid overrides fixed, touch targets 44px, noise overlay disabled on mobile GPUs.

## [2026-04-05] research | Price drift analysis — drift is NET PROFITABLE, don't fix
68.8% of trades have fill price != scan price. Initially recommended tightening STC gate to 92c — WRONG. PnL analysis shows DOWN-drift fills are the MOST profitable group ($1.25/trade, 47.5% of total PnL). 135 sub-floor fills are net +$108 (12.4% of PnL). The 6 STC gate violations are -$11 net (negligible). Tightening gates would COST money because at 93.5% WR, every blocked trade is 14:1 winners-to-losers. Recommendation: do nothing — drift is a feature, not a bug. Filed as kb-research/bot/price-drift-analysis.md.

## [2026-04-06] fix | 5 P&L reporting bugs — fee overcounting, revenue inflation, chart scope
Fee overcounting (~$120): record_settlement recomputed full taker fee on mixed maker/taker positions. Fix: accumulated_fee_cents per-fill tracking. Revenue inflation (34 trades, $20.48): lost addon positions caused full API revenue on surviving row. Fix: revenue_cents = count*100, revenue_override for stacked. Chart scope: Lifetime P&L used 15M-only data by default. Fix: always use all_products_cumulative_pnl. Trade count: showed thinned points (100) not actual (1300+). Fix: use win+loss counts. Added actual_pnl_cents from Kalshi balance for correct total display. Also fixed LPNE UnboundLocalError (final_prob used before assignment) and auditor schema drift alert.

## [2026-04-06] fix | CalEngine training data filtered to candidates only
Root cause: load_training_data_from_db() had no filter_stage restriction. 90% of training data was rejected trades (insufficient_edge at 50% prob, no_side_shadow at 17%). Only 10% was actual candidates. This caused Platt A=0.15 (compress to 80%) instead of correct A=1.0 (near-identity). Fix: added accepted_stages to CalEngine — filters both startup load AND ongoing add_observation(). 15M engines now train on candidates only. SOL Brier improved 7.9% out-of-sample.

## [2026-04-06] feat | Per-asset 15M CalEngines deployed (shadow)
4 per-asset CalEngines (BTC, ETH, SOL, XRP) deployed in shadow mode. Follow existing weather/sports subtype pattern. Each trains independently on per-asset data. Initial Brier: BTC 0.109 (beta_cal), ETH 0.110 (beta_cal), SOL 0.170 (platt), XRP 0.116 (platt). SOL Beta Cal had degenerate params — correctly rejected by regression guard. Dual-feed: per-asset AND global engine get observations. Backfilled 1,744 NULL product_type rows in evaluated_opportunities. Zero impact on live trading (cal_engine_enabled=False).

## [2026-04-05] fix | PPO v2 table creation — executescript silently failed
The `conn.executescript()` with DROP+CREATE+INDEX in one block silently failed on VPS, leaving the table non-existent. Split into separate `conn.execute()` calls with IF NOT EXISTS guards. This is the 5th PPO fix in one day — lesson: test table creation on VPS, not just locally.

## [2026-04-05] feat | PPO v2 confirmed working — 95+ rows on live trades
First successful PPO data collection: 5 open positions (BTC 11ct@93c, ETH 47ct@91c, SOL 157ct@86c, XRP 88ct@92c, XRP 44ct@95c), all showing healthy buffers (0.13-0.37%). Spot price backbone working, threshold cached from DB, STC parsed from event_ticker. 156 obs on XRP, 78 on SOL. Data accumulating for research questions in ppo-research-questions.md. Next: dashboard position health panel.

## [2026-04-05] feat | PPO v2 — spot-price primary, complete rewrite
Complete rewrite of position price monitor. v1 had 3 bugs (wrong class name, _active_windows lookup, logging.debug swallowing errors) and fundamentally relied on Kalshi quotes which are empty near settlement. v2 uses CoinbaseFeed spot price as backbone (always available), Kalshi quotes as supplementary. Logs spot_price, threshold, spot_buffer_pct, STC (parsed from event_ticker, not _active_windows), plus Kalshi ask/bid when available. Every tick, never skips. This answers: post-entry price trajectory, entry timing quality, loss anatomy (exact moment spot crosses threshold).

## [2026-04-05] fix | Position price monitor was silently broken
PPO `_ppo_is_15m` check iterated `self._active_windows` looking for the held ticker's market entry. Failed because `_active_windows` may not contain the ticker after the occupied timeslot check filters it out → 0 observations ever collected despite trades occurring. Fix: simple ticker prefix check (`"15M" in ticker`). Lesson: "0 rows, waiting for conditions" after a qualifying event IS a bug — don't accept it as verification. (See memory/feedback_verify_new_features.md)

## [2026-04-05] fix | SOL risk cap 12%→15% + DC per-asset cap enforcement
SOL_MAX_RISK_PER_TRADE raised from 0.12 to 0.15 (data: 43.9% of trades capped, +$26 PnL). Also fixed bug: DC sizing path bypassed all per-asset caps (used DECIDED_CONTRACT_RISK=20% directly). Added per-asset clamping after DC position computation for SOL/BTC/XRP. This REDUCES DC SOL exposure from 20% to 15% while increasing main pipeline SOL from 12% to 15% — net more consistent risk across paths.

## [2026-04-05] feat | Post-entry position price monitor
New `position_price_observations` table logs yes_ask/yes_bid for held 15M positions. WS orderbook cache primary (zero API cost), REST fallback if stale. Change-only dedup: only logs when price moves. Every tick (~1s) polling. Purpose: fill the 97.6% post-entry data blind spot discovered during re-entry analysis. Data enables: price trajectory analysis, entry timing evaluation, future position management strategies.

## [2026-04-05] research | Same-ticker re-entry — DEBUNKED after stress test
Initial analysis showed 78/78=100% WR on re-entry at 3c+ drop. Stress test revealed: (1) claim was from narrow DC-overlay subset, not full data. Full dataset: 1,304/1,404=92.9% with 100 losses. (2) Re-entry is WORSE than base rate: 90.0% vs 94.0% at same price/STC. Fisher p=0.46. (3) 86% have zero ask depth. (4) 21% of drops continue falling avg 29c more. (5) z-scores average -1.0 (real risk, not noise). REAL finding: bot is 97.6% blind to post-entry prices due to occupied timeslot check at line 6484. Post-entry monitoring is the actionable item. Filed as kb-research/bot/same-ticker-reentry-analysis.md (status: debunked).

## [2026-04-05] research | 12-agent goldmine hunt — comprehensive alpha search
Massive parallel research: counterfactual PnL by rejection stage, hourly optimization, DC expansion, NO-side trading, execution efficiency, vol regime sizing, cross-asset correlation, web research (prediction markets + vol models), 15M shadow readiness, time-of-day patterns, fee optimization. Most "opportunities" collapsed under rigorous verification: relaxed edge = -$103 at Kelly (only 29% incremental), DC z≤-1.5 = base rate (z-stat 0.87, one good week), post-loss = $23 total (same-window correlation), maker adverse selection = real but price-dependent and small. Real survivors: fill rate improvement ($967 CF on 450 unfilled), hourly STC tightening (600-1200s losing), intraday vol seasonality (structural model fix), weather NO pipeline (94.5% but zero fills), cross-asset confirmation (BTC 95c+ confirms others at 90%), market making ($1K/day liquidity incentives). Filed as kb-research/bot/goldmine-hunt-apr5.md.

## [2026-04-05] feat | LPNE strategy — BTC 80-87c near-expiry overlay
Low-Price Near-Expiry (LPNE): intercepts BTC at 80-87c when STC<=120s at the price floor check. Data: 97.6% WR on 42 signals (p=0.031), near-expiry advantage p=0.006. Fixed 50-contract sizing (LOW_STC_SIZING_CAP halves most to 25). Probability gate: final_prob >= price/100 (model must believe break-even). Separate _execute_lpne_taker executor. NBBO gate lowered from 86c to 80c for BTC. BTC_MIN_ENTRY_PRICE unchanged at 88c — LPNE intercepts before floor rejection.

## [2026-04-05] feat | SOL sub-86c time gate + universal STC sizing scaler
Two pipeline fixes from stc-sizing-research. Layer 1: SOL_LOW_ENTRY_STC_GATE blocks SOL ≤85c at STC≥300s (data: 78.3% WR -$289, near-expiry 100% WR preserved). Inserted after XRP shadow gate, follows same pattern. Layer 2: STC_SIZING_SCALER_ENABLED scales contracts by 300/STC for all 15M at STC>300s (data: 5-7m 90.8%, 7m+ net negative). Applied in main pipeline, overnight discount, and weekend discount for consistency. kelly_f stays pure — only contracts scaled. Combined +$353 (+73%) projected PnL improvement.

## [2026-04-05] research | Overnight miscalibration deep dive — golden hour debunked
Deep analysis of "golden hour" (UTC 3,5,6,11) shadow data. Findings: (1) Golden hour framing is wrong — it's the entire 04-11 UTC window, not 4 specific hours. (2) Model underconfident by 7pp at 91-92c overnight (89% cal_prob vs 96.2% actual WR). (3) Root cause: BLR calibrator disabled Mar 28 — the one mechanism that would fix this. (4) Verified PnL is modest (+$259/19 weekdays = $13.60/day at 50ct). (5) NOT statistically significant: Wilson CI lower bound 90.5% below 92% breakeven, z-test p=0.152. (6) Overnight discount is currently zero-incremental (all 36 live fires also had candidate entries). (7) STC<=400 is strongest sub-filter (97.3% WR, 4L on 149 trades). Recommendation: keep shadowing, investigate overnight-specific BLR. Filed as kb-research/bot/overnight-miscalibration-analysis.md. Cross-linked to decisions/blr-removal.md.

## [2026-04-05] feat | TM 98c/99c scaled to 100 contracts
Terminal Momentum per-price sizing: TM_CONTRACTS_BY_PRICE = {98: 100, 99: 100}, default 50 for 95-97c. Execution-time drift guard in _execute_tm_taker re-derives count from fresh_ask when price moves between scan and execution — prevents oversized fills on lower tiers. Data: 98c 33/33 WR, 99c 62/62 WR. Price drift analysis showed 44% of TM trades execute at different price than scanned (mostly upward toward 99c near expiry). Updated terminal-momentum.md.

## [2026-04-05] fix | Doc drift — test count 999, hourly observation
CLAUDE.md and whitepaper.md test count updated 872→999. CLAUDE.md hourly observation description fixed (was hardcoded True, now matches code: `not HOURLY_LIVE_ENABLED`).

## [2026-04-10] feat | Add yes_bid_cents column for buy-low-sell-higher analysis
New column `yes_bid_cents INTEGER` added to `evaluated_opportunities` via migration. Populated by main 15M scanner via `OrderExecutor._best_yes_bid(ob_data)`. Cached in `StateManager._scan_bid_cache` so all 50+ `insert_evaluated_opportunity` call sites pick it up automatically without threading. Enables: (1) buy-low-sell-higher strategy validation, (2) early-exit price quality check, (3) spread/staleness analysis. Analysis script: `scripts/buy_low_analysis.py`. New KB article: [[kb-research/bot/buy-low-investigation.md]] documenting the dual YES/NO logging discovery and the proper YES-side filter (raw_prob > 0.5 + filter_stage NOT LIKE '%no_side%').

## [2026-04-09] fix | Kill TM 95c/97c, escalation edge recheck, shadow exit signals
Three changes deployed: (1) TM_PRICE_SET {95,96,97,98,99}→{96,98,99} — 95c/97c had 94.5% WR vs 95-97% breakeven, -$980/2wk. (2) Taker escalation edge recheck — aborts cancel-replace IOC if fee-adjusted edge goes negative at new ask price. 10 MAKER_PATIENT drift losses cost $704/2wk. (3) Shadow exit signal table (`exit_signal_shadow`) — fires on buffer < -0.10% or >50% negative in 30-obs window. Data collection for future early-exit system. Updated: terminal-momentum.md, execution-layer.md, ppo-research-questions.md.

## [2026-04-04] decision | OpenClaw + Gemma evaluation — deferred
Evaluated OpenClaw (open-source AI agent, 247K stars) + Google Gemma 3 for bot operations automation. Conclusion: security concerns outweigh benefits for a live trading system. OpenClaw's ClawHub had 2,419 malicious skills purged, 21,639 exposed instances (Kaspersky). SSH access from OpenClaw to trading VPS = full access to Kalshi API keys. VPS can't run Gemma (1 vCPU/2GB RAM). Gemma 4B is much weaker than Claude for complex trade analysis. Recommended alternative: simple Python Telegram bot (200-300 lines) using existing scripts, zero new attack surface. News monitoring is the one genuine gap OpenClaw fills. Decision: build Python Telegram bot first, revisit OpenClaw only for news regime detection on an isolated server.
