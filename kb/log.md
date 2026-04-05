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

## [2026-04-05] feat | SOL sub-86c time gate + universal STC sizing scaler
Two pipeline fixes from stc-sizing-research. Layer 1: SOL_LOW_ENTRY_STC_GATE blocks SOL ≤85c at STC≥300s (data: 78.3% WR -$289, near-expiry 100% WR preserved). Inserted after XRP shadow gate, follows same pattern. Layer 2: STC_SIZING_SCALER_ENABLED scales contracts by 300/STC for all 15M at STC>300s (data: 5-7m 90.8%, 7m+ net negative). Applied in main pipeline, overnight discount, and weekend discount for consistency. kelly_f stays pure — only contracts scaled. Combined +$353 (+73%) projected PnL improvement.

## [2026-04-05] research | Overnight miscalibration deep dive — golden hour debunked
Deep analysis of "golden hour" (UTC 3,5,6,11) shadow data. Findings: (1) Golden hour framing is wrong — it's the entire 04-11 UTC window, not 4 specific hours. (2) Model underconfident by 7pp at 91-92c overnight (89% cal_prob vs 96.2% actual WR). (3) Root cause: BLR calibrator disabled Mar 28 — the one mechanism that would fix this. (4) Verified PnL is modest (+$259/19 weekdays = $13.60/day at 50ct). (5) NOT statistically significant: Wilson CI lower bound 90.5% below 92% breakeven, z-test p=0.152. (6) Overnight discount is currently zero-incremental (all 36 live fires also had candidate entries). (7) STC<=400 is strongest sub-filter (97.3% WR, 4L on 149 trades). Recommendation: keep shadowing, investigate overnight-specific BLR. Filed as kb-research/bot/overnight-miscalibration-analysis.md. Cross-linked to decisions/blr-removal.md.

## [2026-04-05] feat | TM 98c/99c scaled to 100 contracts
Terminal Momentum per-price sizing: TM_CONTRACTS_BY_PRICE = {98: 100, 99: 100}, default 50 for 95-97c. Execution-time drift guard in _execute_tm_taker re-derives count from fresh_ask when price moves between scan and execution — prevents oversized fills on lower tiers. Data: 98c 33/33 WR, 99c 62/62 WR. Price drift analysis showed 44% of TM trades execute at different price than scanned (mostly upward toward 99c near expiry). Updated terminal-momentum.md.

## [2026-04-05] fix | Doc drift — test count 999, hourly observation
CLAUDE.md and whitepaper.md test count updated 872→999. CLAUDE.md hourly observation description fixed (was hardcoded True, now matches code: `not HOURLY_LIVE_ENABLED`).

## [2026-04-04] decision | OpenClaw + Gemma evaluation — deferred
Evaluated OpenClaw (open-source AI agent, 247K stars) + Google Gemma 3 for bot operations automation. Conclusion: security concerns outweigh benefits for a live trading system. OpenClaw's ClawHub had 2,419 malicious skills purged, 21,639 exposed instances (Kaspersky). SSH access from OpenClaw to trading VPS = full access to Kalshi API keys. VPS can't run Gemma (1 vCPU/2GB RAM). Gemma 4B is much weaker than Claude for complex trade analysis. Recommended alternative: simple Python Telegram bot (200-300 lines) using existing scripts, zero new attack surface. News monitoring is the one genuine gap OpenClaw fills. Decision: build Python Telegram bot first, revisit OpenClaw only for news regime detection on an isolated server.
