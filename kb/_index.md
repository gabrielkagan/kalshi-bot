# Kalshi Bot Knowledge Base

Last updated: 2026-05-08 | Articles: 56 active + 6 archived (curated) | Status: Reconciled

## Usage
Read this index first before answering any deep question about the bot. Identify relevant articles, read those, then respond. After significant sessions, update articles and this index.

## Curation policy

`_index.md` is a **curated** subset, not an auto-generated listing. Per
`kb/CLAUDE.md`, `kb/` is **local-only by convention** — the 56 entries
here are pre-rule-legacy files that remain tracked in git as agent-guide
infrastructure. New session notes (`kb/decisions/`, `kb/findings/`,
`kb/failures/` since the convention took effect) are local-only and
intentionally not indexed; reach them via filesystem search or the
`kb/decisions/session-resume-*.md` chain.

A regression test (`tests/integration/test_kb_index_links_resolve.py`) asserts every
`[[…]]` reference here resolves to a file on disk. Adding a new entry
requires the file to exist; the policy decision (commit-vs-local) is the
maintainer's call.

---

## Concepts (26)
- [[concepts/dashboard-architecture.md]] - Dashboard architecture: Supabase Realtime, 216 snap keys, 91 renderers, 10 analytics RPCs, 10K-line HTML.
- [[concepts/supabase-schema-parity.md]] - Startup validator: OpenAPI-based column diff, logs ALTER suggestions, kills silent-400 drift class.
- [[concepts/agent-audit-verification.md]] - Protocol for verifying agent-swarm findings before acting — 5/5 Chesterton deletes were wrong after verification.
- [[concepts/dashboard-snapshot-single-scan.md]] - Win/loss + risk compute: 11 SELECTs → 1 single-scan + in-memory bucketing (2026-04-18).
- [[concepts/public-dashboard-schema.md]] - Public /performance/ page: strict 15-field whitelist, separate Supabase row, sanitized by construction (2026-04-18).
- [[concepts/dc-strategy.md]] - Decided Contract: T1/T1B/T2 tiers, z-score thresholds, risk caps, fill rates.
- [[concepts/sol-dynamics.md]] - SOL-specific: edge floor, PnL dominance, tiered DC risk, sizing concerns.
- [[concepts/execution-layer.md]] - Execution: maker-first escalation, SOL override, per-asset locks, order lifecycle.
- [[concepts/per-asset-rules.md]] - Per-asset config: BTC 15%, ETH 20%, SOL 15%, XRP 15% risk caps and floors.
- [[concepts/weather-system.md]] - Weather ensemble model: GFS+ECMWF, 19 cities, bracket/tail markets, ensemble cache persistence.
- [[concepts/stacking-infrastructure.md]] - Multi-strategy stacking: composite PK, strategy_to_group(), caps.
- [[concepts/position-reconciliation.md]] - Startup reconciliation: API-always-wins sync, multi-strategy aware.
- [[concepts/fifteenm-shadow-variants.md]] - 15M shadows: A1 RecalibratedEGARCH, A2 LightGBM, A3 gating, A4 LateWindow.
- [[concepts/fee-optimization.md]] - Fee structure: $0 maker, quadratic taker, fill rate tradeoffs by asset.
- [[concepts/sports-engine.md]] - Sports comeback: ESPN feed, Bayesian posterior, 8 sport groups, SPRT testing.
- [[concepts/spx-engine.md]] - SPX hourly: dual vol model (EGARCH+HAR-RV), Finnhub WS, observation-only.
- [[concepts/cal-engine-registry.md]] - CalEngine registry: per-product/city/sport engines, settlement routing.
- [[concepts/market-config-system.md]] - MarketTypeConfig dataclass, startup validation, crash loop origin.
- [[concepts/guard-aware-protocol.md]] - Mandatory 5-step protocol for changes touching balance, sizing, HWM, or guards.
- [[concepts/drawdown-scaler.md]] - Drawdown scaler: 7-day rolling HWM, tiered reduction, warmup, spike guards.
- [[concepts/edge-thresholds.md]] - Edge thresholds: price-dependent schedule, SOL override, probability parameters.
- [[concepts/balance-tracking.md]] - Balance tracking: cached accessor, invalidation on fill, sanity cap, stale balance.
- [[concepts/data-analysis-rules.md]] - Mandatory 10-point checklist for DB analysis: schema-first, no compounding errors.
- [[concepts/dc-execution-mechanics.md]] - DC execution: taker IOC, retry queue (11 attempts), price widening, 22% fill rate.
- [[concepts/addon-strategies.md]] - Addon strategies: confirmation addon (live) and dip addon (killed, 55.2% WR).
- [[concepts/shadow-expansion-variants.md]] - Shadow expansion: 6 DC variants, low-price sim, overnight LP, promotion criteria.

## Strategies (5)
- [[strategies/terminal-momentum.md]] - TM: 96/98/99c (95c/97c killed Apr 9), 61-300s STC, margin x STC sizing, stacking.
- [[strategies/lpne.md]] - LPNE: BTC 80-87c near-expiry (STC<=120s), intercepts at price floor. 97.6% WR on 42 obs, 50ct fixed.
- [[strategies/bracket-no.md]] - Weather bracket NO: buy NO when YES 88-96c, 91.7% NO settlement rate.
- [[strategies/overnight-discount.md]] - Overnight/weekend edge discount: 0.6x multiplier, overnight 89c+ / weekend 90c+ live gates.
- [[strategies/hourly-markets.md]] - Hourly sub-60c: BTC+ETH only, fixed 25ct, T=1.45, taker-only.

## Failures (14)
- [[failures/dashboard-drift.md]] - Dashboard messy, stale, partial: 412-trade sync lag, spx_harrv 400s, 20 DEAD keys, 8 GHOST panels, 3 duplicate families. Plan A→B chosen.
- [[failures/sync-watermark-clock-drift.md]] - settled_at watermark + 13.6s clock drift → 412-trade gap over 5 days. Fixed via rowid watermark.
- [[failures/hwm-bugs.md]] - Five HWM/drawdown scaler variants. Recurring bug family (Mar 25-30).
- [[failures/blr-calibrator.md]] - Broken BLR outputting ~95% constant. Discovery March 25.
- [[failures/polygon-403.md]] - Polygon.io 403 errors: zero SPX evaluations, Finnhub fallback.
- [[failures/evaluations-sync.md]] - 28-day Supabase sync failure: 32 missing columns, FK, NaN values.
- [[failures/regime-cap-discovery.md]] - Capital allocator permanently GREEN, $400 cap throttling all trades.
- [[failures/loss-clustering.md]] - Mar 31 triple-loss + Apr 11 30d burst analysis (53/82 losses in bursts) -> 2h per-asset cooldown shipped.
- [[failures/database-contention.md]] - Five SQLite contention incidents (Mar 2-16): busy_timeout, batch commits.
- [[failures/ioc-subfloor-fill.md]] - IOC sub-floor fills via phantom top-of-book. Recurring (~4 severe/50d). Updated Apr 15.
- [[failures/supabase-sync-silent-failure.md]] - SELECT * sent ~30 unknown columns to Supabase -> weeks of eval/rejection data lost silently.
- [[failures/pnl-reporting-bugs.md]] - Fee overcounting ($120), revenue inflation ($20), stacking double-revenue ($13). Fixed Apr 6.
- [[failures/settlement-watermark-race.md]] - Watermark skips failed settlements -> stuck positions, inflated PnL, stale orders. Fixed Apr 7.
- [[failures/ppo-monitor-bugs.md]] - Three PPO bugs: STC timezone, deprecated API fields, WS stale threshold. 0% orderbook data. Fixed Apr 7.

## Decisions (11)
- [[decisions/dashboard-overhaul-plan.md]] - Dashboard overhaul: Option A (surgical, 1wk) → Option B (v2 contract + parallel HTML, 2-3wk). Option C (SPA) deferred. Six-agent swarm audit.
- [[decisions/blr-removal.md]] - Disabled BLR calibrator via feature flag (Mar 29, 2026).
- [[decisions/sol-edge-floor.md]] - SOL_MIN_EDGE=1.0%: <1.0% = 82% WR vs >=1.0% = 94.2% WR.
- [[decisions/xrp-promotion.md]] - XRP promoted to live at 92c+ floor (41W/2L, 95.3% WR).
- [[decisions/regime-cap-removal.md]] - Removed $400 regime cap, replaced with per-asset caps (Apr 2).
- [[decisions/stacking-enabled.md]] - Enabled multi-strategy stacking with composite PK (Apr 1-2).
- [[decisions/sol-taker-first.md]] - SOL taker-first + adverse selection analysis (merged). 44.7% fill rate, $101/wk missed.
- [[decisions/t2-z2-shadowed.md]] - T2-Z2 shadowed + root cause analysis (merged). -$313 net on 47 trades, effectively killed.
- [[decisions/hourly-promotion.md]] - Hourly promoted to live: sub-60c BTC+ETH, 66.3% WR on 1,474 tickers.
- [[decisions/config-models-extraction.md]] - Extracted config.py and models.py from bot.py (Mar 21).
- [[decisions/stc-extended-zone.md]] - 300-600s re-enabled with per-asset higher floors (BTC 93c, ETH 90c, SOL 95c, XRP 92c). 98.8% WR on n=83.
- D3.0 silver layer foundations (ticket 86b9zxc6t, plan doc local-only per kb/CLAUDE.md). First Bit of Phase 2 (Silver). Off-VPS DuckDB+dbt ETL on operator Mac via launchd nightly. 5 Tier-1 sources normalized to Parquet under `silver/v1/<source>/utc_date=YYYY-MM-DD/data.parquet`. D3.0 originally shipped direct-DuckDB ETL; D3.0-fu1 (SHIPPED 2026-05-18, ticket 86ba0a2k8) layered the dbt project on top — 5 models with `materialized='external'` + parameterized `location` driven by `var('target_date')` (R6-M2 materialization fork resolved to path (c) at D3.0-fu1 sandbox). See `silver/README.md` + `agent_docs/bot_layout.md` "Silver ETL" section.

## Conceptually archived (6)
Superseded or merged decisions kept in their active dirs for historical
reference — not actively maintained. Files remain in `kb/decisions/` and
`kb/failures/` (no `kb/_archive/` dir exists).
- [[decisions/tm-97c-promotion.md]] — Decision reversed Apr 9 (97c removed from TM_PRICE_SET, negative EV).
- [[failures/t2-z2-losses.md]] — Merged into decisions/t2-z2-shadowed.md.
- [[failures/sol-maker-adverse-selection.md]] — Merged into decisions/sol-taker-first.md.
- [[failures/dedup-tuple-crash.md]] — Resolved Mar 7, trivial fix, guarded by regression test.
- [[failures/shadow-callsite-variable.md]] — Resolved Mar 7, generic Python lesson.
- [[decisions/openclaw-deferred.md]] — Deferred indefinitely, no active relevance.
