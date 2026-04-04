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
