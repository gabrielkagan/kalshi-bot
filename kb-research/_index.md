# Research Knowledge Base Index

Last updated: 2026-04-07 | Articles: 20

## Purpose
Complete research findings, analysis outputs, and external knowledge compiled from past Claude chat sessions. These are reference materials that informed bot decisions. Cross-link to kb/decisions/ for what was actually decided.

---

## Bot Research
- [[bot/ml-probability-improvements.md]] - ML/AI evaluation: LightGBM A2, HAR-RV, regime detection (BOCPD/HMM), RL feasibility, calibration deep dive (BLR collapse, temperature scaling, CalEngine progression). What was implemented vs what remains actionable.
- [[bot/market-expansion-analysis.md]] - SPX, weather, Nasdaq, FX market ranking with data. Open-Meteo API details, forecast accuracy tables, implementation roadmap, and what actually happened vs plan.
- [[bot/profitability-acceleration.md]] - 7-question deep dive on 604 trades. MAKER_PATIENT kill, DC overlay optimization, SOL maker adverse selection, fee math, weekend/overnight discount graduation, NBBO fill rate doubling.
- [[bot/sports-comeback-model.md]] - Bayesian LR comeback model complete architecture. Academic foundation (Choi & Hui, Croxson & Reade). Model comparison matrix. Basketball 69% WR / SPRT converged. 1-point deficit 91.7% WR finding. Filter configurations. Calibration methodology. Kill list (tennis, hockey, soccer). Swisstony analysis.
- [[bot/weather-nwp-analysis.md]] - NWP ensemble analysis on 581 observations. NO ALPHA verdict (+32pp overconfidence). NO-side 73.1% WR but pricing unviable. Bias correction approaches. Temperature scaling analysis. EMOS calibration research.
- [[bot/autoresearch-applicability.md]] - Karpathy autoresearch mapped to bot. Fast backtesting harness as blocking prerequisite. Overfitting mitigations. Regime sensitivity framework. Weather-specific application design.
- [[bot/dota-draft-arbitrage.md]] - Draft-phase win prediction design. 12-table Supabase schema. Steam/STRATZ APIs. Implementation plan. Not started — pending backtest validation.
- [[bot/btc-loss-investigation-apr4.md]] - BTC $75 loss deep dive: 41 passthrough trades, p=0.39, entry price caps counterproductive, escalation pattern (73% rate), 30ct cap recommendation.
- [[bot/backtesting-harness.md]] - Backtester build: two rounds of broken results (phantom signals, balance re-sizing), validated filter mode, SOL confirmed profitable (+$71).
- [[bot/ppo-research-questions.md]] — 15 PPO research questions: Q1-Q10 answered (spot data), Q11-Q13 answered (orderbook data). Buffer predicts outcomes, bids available 99%+, early exit feasible. n=2 losses too small for activation.
- [[bot/buffer-rescue-analysis.md]] — Buffer-gated trade rescue: 33 rejected trades at 100% WR with fat buffers. BUT all NBBO-sourced, Wilson CI overlaps breakeven. Verdict: wait 2 weeks for statistical power. See also stc-extended-zone decision.
- [[bot/settlement-price-divergence.md]] — Kalshi settles on CFB RTI (multi-exchange, 60s avg), we use Coinbase only. Plan: log expiration_value, compute 60s trailing avg, measure divergence before building anything.
- [[bot/price-drift-analysis.md]] — Price drift is NET PROFITABLE ($1.25/trade on down-drift). Don't fix. Sub-floor fills = +$108.
- [[bot/same-ticker-reentry-analysis.md]] — Same-ticker re-entry: DEBUNKED. Initial 78/78 was cherry-picked DC subset. Full data: 92.9% WR (100 losses), worse than 94% base rate (p=0.46). Real finding: bot is 97.6% blind to post-entry prices (occupied timeslot at line 6484). Post-entry monitoring is the actionable item.
- [[bot/goldmine-hunt-apr5.md]] — 12-agent comprehensive alpha hunt. Most opportunities collapsed under verification (relaxed edge=-$103 at Kelly, DC z-1.5=base rate, post-loss=$23 total). Real survivors: fill rate improvement ($967 CF), hourly STC tightening, intraday vol seasonality, weather NO pipeline fix, cross-asset confirmation, market making.
- [[bot/stc-sizing-research.md]] - STC sizing vulnerability: SOL sub-86c far-from-expiry losses (-$289), universal STC overexposure at 7m+ (-$254). Two fixes: SOL time gate + Kelly STC scaler. Combined +$353 (+73%) PnL improvement. Edge doesn't predict winning (r=-0.04).
- [[bot/overnight-miscalibration-analysis.md]] - Overnight (04-11 UTC) model underconfidence: 7pp gap at 91-92c, BLR disabled is root cause, verified PnL +$259/19d but p=0.15 not significant. Golden hour framing debunked. Shadowing recommended.
- [[bot/per-asset-calengine.md]] - Per-asset 15M CalEngine. SOL 3.5pp overconfident, shared engine can't fix. Deployed shadow Apr 6. STC-aware Platt researched: +11.8% Brier for SOL out-of-sample but walk-forward PnL unreliable (3/5 folds). Standard Platt/Beta Cal training in shadow while STC-Platt deferred to Phase 2.

## Infrastructure Research
- [[infrastructure/firebase-supabase-migration.md]] - Complete 7-agent migration plan with test criteria at every stage. Dual-write safety architecture. Supabase config decisions. Completed Mar 6.
- [[infrastructure/claude-code-automation.md]] - Opus as autonomous agent. API vs CLI interaction models. Three-tier approval system. Scheduling architecture. Current vs proposed automation layers.

## Documents
- [[documents/whitepaper-system.md]] - Investor/technical whitepapers, 16-slide deck (full slide breakdown), rebuild playbook (11 sections). Dynamic updating system design. Complete list of accuracy drift issues found in March audit.

## Work (Felix Pago)
- [[work/fraud-vendor-analysis.md]] - Complete vendor evaluation: Alloy, Sardine, Oscilar, Taktile, Camunda, DataVisor, Feedzai, Socure. Where each fails. Camunda vs Oscilar/Taktile with practical examples. Five-vendor architecture with full data flow. Strategic insight on feedback loops.
