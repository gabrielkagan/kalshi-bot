# Kalshi Bot Knowledge Base

Last updated: 2026-04-07 | Articles: 53 | Status: Trimmed

## Usage
Read this index first before answering any deep question about the bot. Identify relevant articles, read those, then respond. After significant sessions, update articles and this index.

---

## Concepts (21)
- [[concepts/dc-strategy.md]] - Decided Contract: T1/T1B/T2 tiers, z-score thresholds, risk caps, fill rates.
- [[concepts/sol-dynamics.md]] - SOL-specific: edge floor, PnL dominance, tiered DC risk, sizing concerns.
- [[concepts/execution-layer.md]] - Execution: maker-first escalation, SOL override, per-asset locks, order lifecycle.
- [[concepts/per-asset-rules.md]] - Per-asset config: BTC 15%, ETH 20%, SOL 15%, XRP 15% risk caps and floors.
- [[concepts/weather-system.md]] - Weather ensemble model: GFS+ECMWF, 19 cities, bracket/tail markets.
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
- [[strategies/terminal-momentum.md]] - TM: 95-99c, 61-300s STC, margin×STC sizing, price-level stacking. 276 trades, 98.6% WR.
- [[strategies/lpne.md]] - LPNE: BTC 80-87c near-expiry (STC<=120s), intercepts at price floor. 97.6% WR on 42 obs, 50ct fixed.
- [[strategies/bracket-no.md]] - Weather bracket NO: buy NO when YES 88-96c, 91.7% NO settlement rate.
- [[strategies/overnight-discount.md]] - Overnight/weekend edge discount: 0.6x multiplier, 89c+ live gates.
- [[strategies/hourly-markets.md]] - Hourly sub-60c: BTC+ETH only, fixed 25ct, T=1.45, taker-only.

## Failures (15)
- [[failures/hwm-bugs.md]] - Five HWM/drawdown scaler variants. Recurring bug family (Mar 25-30).
- [[failures/blr-calibrator.md]] - Broken BLR outputting ~95% constant. Discovery March 25.
- [[failures/t2-z2-losses.md]] - Two T2_Z2 losses with known root causes. Led to shadow decision.
- [[failures/polygon-403.md]] - Polygon.io 403 errors: zero SPX evaluations, Finnhub fallback.
- [[failures/evaluations-sync.md]] - 28-day Supabase sync failure: 32 missing columns, FK, NaN values.
- [[failures/sol-maker-adverse-selection.md]] - SOL MAKER_PATIENT: 88.1% WR below breakeven, adverse selection.
- [[failures/regime-cap-discovery.md]] - Capital allocator permanently GREEN, $400 cap throttling all trades.
- [[failures/loss-clustering.md]] - Mar 31 triple-loss window ($352): BTC+XRP+SOL in same window, worst ever.
- [[failures/database-contention.md]] - Five SQLite contention incidents (Mar 2-16): busy_timeout, batch commits.
- [[failures/ioc-subfloor-fill.md]] - IOC fills below asset MIN_ENTRY_PRICE via stale NBBO. Known unfixed.
- [[failures/dedup-tuple-crash.md]] - Mixed 2/3-tuple sizes in _eval_opp_seen crashed scan loop (Mar 7).
- [[failures/shadow-callsite-variable.md]] - NameError swallowed by logging.debug made shadow engine dead code (Mar 7).
- [[failures/supabase-sync-silent-failure.md]] - SELECT * sent ~30 unknown columns to Supabase → weeks of eval/rejection data lost silently.
- [[failures/pnl-reporting-bugs.md]] - Fee overcounting ($120), revenue inflation ($20), stacking double-revenue ($13). Fixed Apr 6.
- [[failures/settlement-watermark-race.md]] - Watermark skips failed settlements → stuck positions, inflated PnL, stale orders. Fixed Apr 7.
- [[failures/ppo-monitor-bugs.md]] - Three PPO bugs: STC timezone, deprecated API fields, WS stale threshold. 0% orderbook data. Fixed Apr 7.

## Decisions (11)
- [[decisions/blr-removal.md]] - Disabled BLR calibrator via feature flag (Mar 29, 2026).
- [[decisions/sol-edge-floor.md]] - SOL_MIN_EDGE=1.0%: <1.0% = 82% WR vs ≥1.0% = 94.2% WR.
- [[decisions/xrp-promotion.md]] - XRP promoted to live at 92c+ floor (41W/2L, 95.3% WR).
- [[decisions/regime-cap-removal.md]] - Removed $400 regime cap, replaced with per-asset caps (Apr 2).
- [[decisions/stacking-enabled.md]] - Enabled multi-strategy stacking with composite PK (Apr 1-2).
- [[decisions/sol-taker-first.md]] - SOL bypasses maker: 44.7% fill rate, $101/wk missed, IOC direct.
- [[decisions/t2-z2-shadowed.md]] - T2-Z2 shadowed after two catastrophic losses totaling -$552 (Mar 31).
- [[decisions/hourly-promotion.md]] - Hourly promoted to live: sub-60c BTC+ETH, 66.3% WR on 1,474 tickers.
- [[decisions/tm-97c-promotion.md]] - Added 97c to TM price set: 98.2% WR on 55 obs above breakeven.
- [[decisions/config-models-extraction.md]] - Extracted config.py and models.py from bot.py (Mar 21).
- [[decisions/openclaw-deferred.md]] - OpenClaw+Gemma evaluated and deferred: security risk, VPS can't run Gemma, Python Telegram bot preferred.
- [[decisions/stc-extended-zone.md]] - 300-600s re-enabled with per-asset higher floors (BTC 93c, ETH 90c, SOL 95c, XRP 92c). 98.8% WR on n=83.
