---
title: "Kalshi Trading Platform"
subtitle: "Investor Whitepaper"
author: "Gabriel Kagan"
date: "May 2026"
titlepage: true
titlepage-color: "0D1B2A"
titlepage-text-color: "FFFFFF"
titlepage-rule-color: "E8A838"
titlepage-rule-height: 4
toc: true
toc-own-page: true
colorlinks: true
linkcolor: "investorlink"
urlcolor: "investorlink"
toccolor: "investorlink"
header-left: "\\footnotesize Kalshi Trading Platform"
header-right: "\\footnotesize Investor Whitepaper"
footer-left: "\\footnotesize Gabriel Kagan"
footer-center: ""
footer-right: "\\footnotesize \\thepage"
mainfont: "TeX Gyre Termes"
sansfont: "TeX Gyre Heros"
monofont: "DejaVu Sans Mono"
fontsize: "11pt"
linestretch: 1.15
geometry: "margin=1.1in"
header-includes:
  - |
    ```{=latex}
    \usepackage{etoolbox}
    \usepackage{xcolor}
    \definecolor{investorlink}{HTML}{1B4F72}
    \AtBeginEnvironment{Highlighting}{\footnotesize}
    \AtBeginEnvironment{verbatim}{\footnotesize}
    \setcounter{tocdepth}{2}
    ```
---

# Executive Summary

This is a quantitative trading platform that operates on **Kalshi**, the only U.S. exchange currently designated by the Commodity Futures Trading Commission (CFTC) for event contracts.[^kalshi-dcm] The system has been trading real capital since **February 22, 2026**.

[^kalshi-dcm]: Kalshi was granted Designated Contract Market (DCM) status by the CFTC in November 2020 and publicly launched in 2021. Source: [CFTC public records](https://kalshi.com/market-integrity/regulation); [Britannica](https://www.britannica.com/money/Kalshi-Inc).

What's unusual about this project — and the reason it merits an investor whitepaper rather than a paragraph in a pitch deck — is that it has accidentally become **two assets**:

1. **A live trading business** running on Kalshi's 15-minute crypto contracts (BTC, ETH, SOL, XRP, HYPE, DOGE, BNB — all seven live post P2.4 promotion 2026-05-19; HYPE/DOGE walked T1 shadow 2026-05-10 → T4 live 2026-05-14, BNB walked T1 shadow 2026-05-17 → T4 live 2026-05-19), augmented by six conditional overlays (decided contracts, terminal momentum, low-price near-expiry, weekend discount, overnight discount, loss-burst cooldown) and a multi-vertical research pipeline (SPX intraday, weather, sports — all currently observation-only).
2. **A proprietary data archive** — the "Data Corpus." As of **May 17, 2026** the system began capturing every WebSocket frame Kalshi emits, byte-exact, into a permanent immutable archive in cloud object storage. This is a different kind of asset: it has no edge to degrade, it appreciates without effort, and it cannot be retroactively created. Bloomberg's terminal business generates roughly **$10 billion per year** primarily from this kind of asset[^bloomberg-rev]; Renaissance Technologies' multi-decade tick archive is the most-cited single contributor to their durable edge.[^rentec] The corpus is small today and will be small a year from now in absolute terms. But the day it stops being optional research infrastructure and becomes a balance-sheet asset is determined entirely by how early we started capturing — and we have started.

[^bloomberg-rev]: Bloomberg L.P. revenue is privately held; Terminal revenue estimated at ~$10B/yr (~85% of LP revenue) per public market commentary. Source: [The Terminalist](https://theterminalist.substack.com/p/bloombergs-7-powers-and-why-the-terminal); [Wikipedia](https://en.wikipedia.org/wiki/Bloomberg_Terminal).

[^rentec]: Renaissance Technologies' tick archive reportedly extends to the 1960s with petabyte-scale ingest. Source: [Zuckerman, *The Man Who Solved the Market* (2019)](https://www.danielscrivner.com/renaissance-technologies-business-breakdown/).

The third structural fact is that the entire platform is **operated by one person with the assistance of AI agents** — an arrangement that was not feasible eighteen months ago and is becoming dramatically more feasible every quarter. The published benchmark data (SWE-bench Verified, May 2026) shows that the *same* underlying LLM placed in different agentic scaffolds varies in coding performance by **fifteen-plus percentage points**.[^swebench] In other words: the engineering discipline imposed on the agents — paved roads, test-driven development, adversarial review, architecture fitness functions — is itself the moat.

[^swebench]: SWE-bench Verified leaderboard, May 2026. Source: [SWE-bench](https://www.swebench.com/); [MarkTechPost AI Agent Benchmark](https://www.marktechpost.com/2026/05/15/best-ai-agents-for-software-development-ranked-a-benchmark-driven-look-at-the-current-field/); [MorphLLM 14 Best AI Coding Agents 2026](https://www.morphllm.com/best-ai-coding-agents-2026).

> **Live trading snapshot (auto-updated, last refresh 2026-09-06T23:06:47Z):**
>
> - **5,683** settled trades since 2026-02-22 — 5,285W / 396L / 2 BE; win rate 93.0\%
> - Seven live 15-minute crypto assets (BTC, ETH, SOL, XRP, HYPE, DOGE, BNB)
> - Six conditional overlays: decided contracts (z-score-driven near-certain outcomes), terminal momentum, low-price near-expiry, weekend discount, overnight discount, loss-burst cooldown
> - 19 weather cities, 28 sports leagues, and S&P 500 intraday markets in observation mode (calibration data accumulating; no capital at risk)
> - Hourly crypto disabled since Apr 18 after a correlated multi-strike loss event; re-enable path preserved behind two environment variables

This document is structured around three theses. **Section 1** describes the bot — what trades today, why those particular assets, and where the edge comes from. **Section 2** describes the Data Corpus — what it is, why it might matter more than the bot, and what the realistic upside scenarios look like. **Section 3** describes the agentic-engineering operating model — why one person plus AI agents can credibly run this without quality collapse, and why the discipline embedded in the code is itself defensible. **Sections 4–6** cover risk management, capital ask context, and fair-witness disclosures.

---

# 1. The Bot

## 1.1 The market

Kalshi is the only CFTC-regulated Designated Contract Market in the United States that lists short-duration event contracts. Its scale matured rapidly through 2025: estimated 2025 trading volume of roughly **$23.8 billion** (a >1,000% year-on-year increase), with a record monthly volume of **$6.38 billion in December 2025** and a single-day record of **$381.7 million on December 21, 2025**.[^kalshi-vol] The exchange was valued at approximately **$11 billion** in its December 2025 funding round.[^kalshi-val] Susquehanna International Group became Kalshi's first dedicated institutional market maker in April 2024[^kalshi-sig]; Jump Trading took equity-for-liquidity stakes in both Kalshi and Polymarket in February 2026[^kalshi-jump]; Tradeweb followed with a minority investment shortly after.[^kalshi-tradeweb]

[^kalshi-vol]: 2025 volume figures vary by source; the figures here use [Cointribune](https://www.cointribune.com/en/kalshi-overtakes-polymarket-as-weekly-trading-volume-hits-a-record-2-3-billion/) and [ainvest Q4 2025 review](https://www.ainvest.com/news/kalshi-q4-2025-surge-redefining-trading-volume-benchmarks-predictive-markets-2512/). A higher commonly-cited number ($238B) appears inconsistent across sources and is not used here.

[^kalshi-val]: [Bitget](https://www.bitget.com/news/detail/12560605095875).

[^kalshi-sig]: [Kalshi blog](https://kalshi.com/blog/article/kalshi-kit-liquidity-sig-market-makers); [BusinessWire](https://www.businesswire.com/news/home/20240403664852/en/Kalshi-Onboards-Its-First-Dedicated-Institutional-Market-Maker).

[^kalshi-jump]: [Bloomberg](https://www.bloomberg.com/news/articles/2026-02-09/jump-trading-poised-to-gain-stakes-in-kalshi-and-polymarket).

[^kalshi-tradeweb]: [Finance Magnates](https://www.financemagnates.com/institutional-forex/tradeweb-backs-kalshi-after-jump-trading-to-push-prediction-markets-to-institutions/).

Kalshi launched short-duration crypto contracts (5-minute and 15-minute windows on BTC, ETH, and other major assets) in **December 2025**.[^kalshi-crypto] Combined daily volume across 5-min and 15-min crypto contracts on Kalshi and Polymarket runs at roughly **$70 million per day**.[^kalshi-crypto-vol] Settlement uses CF Benchmarks' regulated index (60-second TWAP), removing single-source manipulation risk.[^cfbenchmarks]

[^kalshi-crypto]: [Good Money Guide](https://goodmoneyguide.com/usa/kalshi-takes-on-crypto-options-trading-with-launch-of-15-minute-crypto-prediction-markets/).

[^kalshi-crypto-vol]: [Benzinga](https://www.benzinga.com/crypto/cryptocurrency/26/03/51250409/5-minute-bitcoin-bets-hit-70m-daily-volume-as-traders-lean-into-ai). Short-term contracts are roughly 50% of Kalshi's crypto volume.

[^cfbenchmarks]: [CF Benchmarks](https://www.cfbenchmarks.com/blog/kalshi-leads-surging-crypto-event-contract-market-powered-by-cf-benchmarks).

The microstructure properties that make these contracts attractive for systematic trading are: (a) binary fixed-payout structure (pays exactly $1 if the threshold condition is met, $0 otherwise — the cleanest case for Kelly-optimal sizing in the academic literature[^kelly-pm]); (b) short duration, capping capital lock-up and per-position downside; (c) extremely high market frequency (hundreds of fresh contracts per day across active assets); (d) a maker/taker fee structure that rewards patience (Kalshi's taker fee follows `ceil(0.07 × C × P × (100−P) / 100)` cents where C is contract count and P is price in cents — typically ~1¢ per contract at the 90¢ entries where the bot operates; the bot's maker-first execution captures the fee asymmetry on every fill that posts); and (e) settlement against a regulated public index, removing the manipulation surface that has historically afflicted offshore prediction markets.[^whelan-kalshi]

[^kelly-pm]: Noonan, A. & Smith, P. (2024). "Application of the Kelly Criterion to Prediction Markets." [arXiv:2412.14144](https://arxiv.org/html/2412.14144v1). Binary fixed-payout markets are the cleanest case for Kelly because the payoff is exactly $1 and there is no continuous-rebalancing pathology.

[^whelan-kalshi]: Whelan, K. (2025). "Makers and Takers: The Economics of the Kalshi Prediction Market." [karlwhelan.com](https://www.karlwhelan.com/Papers/Kalshi.pdf). Whelan's paper is the most-rigorous public analysis of Kalshi-specific microstructure and is read carefully throughout this document.

## 1.2 What the bot actually does

For every active 15-minute contract — across seven live-trading cryptocurrencies (BTC, ETH, SOL, XRP, HYPE, DOGE, BNB — BNB T4-promoted 2026-05-19 via P2.4 sibling to P2.3 HYPE/DOGE), multiple strike prices per asset, every fifteen minutes, 24/7 — the bot performs the following sequence:

1. **Observe**. Real-time spot-price feeds from Coinbase and Kraken via WebSocket; cross-exchange feeds for lead-lag detection from Bybit (Binance is geo-blocked from the production VPS); implied volatility from Deribit (DVOL index for BTC and ETH every 60 seconds); orderbook state from Kalshi via WebSocket (real-time fills, orderbook deltas, market lifecycle events).
2. **Estimate**. Per-asset volatility computed from a Realized Kernel estimator with adaptive bandwidth (Barndorff-Nielsen, Hansen, Lunde, Shephard 2008[^bnhls]), conditioned by an EGARCH(1,1) model (Nelson 1991[^nelson-egarch]) fit by maximum likelihood with Student-t innovations, blended dynamically using forecast-quality-weighted Bates-Granger combination weights (Bates & Granger 1969[^bates-granger]). Per-asset Normal Inverse Gaussian (NIG) distribution (Barndorff-Nielsen 1997[^bn-nig]) fit by MLE on seven days of returns produces a raw probability that the asset stays above the contract threshold. The KS-test fit improvement over Student-t is large (BTC NIG p-value ≈ 0.11 vs. Student-t effectively 0; ETH ≈ 0.42 vs. effectively 0).
3. **Calibrate**. The raw probability passes through a multi-layer calibration engine described in detail in §1.4. The 15M flow currently runs the inner CalibrationEngine in passthrough mode (raw probability beats the Beta/BLR fits on measured Brier), and the per-asset market blend (§1.4 Layer 3) is the load-bearing calibration step. Per-product CalEngines (weather, sports, SPX) run their full Beta/BLR pipelines independently. Beta calibration follows Kull, Silva Filho & Flach 2017[^kull-beta].
4. **Filter**. Reject the candidate if the after-fee expected value is below a price-dependent threshold (V-shaped: lowest at 91–92¢, rising at both ends), or if model and market disagree by an implausible margin (e.g., model says 92% but market trades at 70¢, suggesting the model is missing material information).
5. **Size**. Edge-tiered fractional-Kelly allocation across eight discrete tiers (25% of bankroll at 4%+ edge, declining to 2% at the 0.25% minimum), wrapped in an automatic drawdown-scaling rule (half-size at 85% of seven-day cash high-water mark, quarter-size at 75%, 10% floor below 65% — not a full halt; the explicit operator kill-switch is the true halt mechanism). Edge tiering is operationally chosen for parameter-uncertainty robustness; the drawdown scaling implements the Grossman-Zhou (1993) drawdown-constrained-Kelly result.[^grossman-zhou]
6. **Execute**. Maker-first with `post_only` guarantees (the bot operates on a maker fee = $0 assumption per its current configuration; Kalshi's published Feb-2026 fee schedule documents a non-zero maker rate of approximately one-fourth of taker, which may apply to the bot's products — see the §6 fair-witness note on fee verification). If the maker leg is rejected (locked spread): degrade-maker, then taker-IOC with edge re-verification at the worse price. Below 180 seconds to settlement, skip maker entirely (empirical fill rates at low STC are very low — 7.7% measured at 0–60s STC and 0% measured at 75–180s STC; direct taker is strictly better). SOL bypasses maker at all STC levels (thin orderbook). BTC uses a shorter escalation wait. Fill detection is via Kalshi WebSocket (zero API cost) with REST polling as backup.
7. **Settle and learn**. Settlement is polled every 30 seconds. Each settled outcome is written to a per-product CalEngine (15M, weather-per-city, sports-per-group, SPX) and used to retrain the calibration layers on the next scheduled tick.

[^bnhls]: Barndorff-Nielsen, O. E., Hansen, P. R., Lunde, A., & Shephard, N. (2008). "Designing Realized Kernels to Measure the ex post Variation of Equity Prices in the Presence of Noise." *Econometrica* 76(6): 1481–1536. [DOI 10.3982/ECTA6495](https://onlinelibrary.wiley.com/doi/abs/10.3982/ECTA6495). The 2008 paper introduces the Parzen-kernel realized-variance estimator with optimal bandwidth `H* ∝ n^(3/5)` calibrated from the noise-to-signal ratio. (The implementation paper BNHLS 2009, *Econometrics Journal*, is the practitioner companion.)

[^nelson-egarch]: Nelson, D. B. (1991). "Conditional Heteroskedasticity in Asset Returns: A New Approach." *Econometrica* 59(2): 347–370. The original specification used GED innovations; the Student-t variant is a common practitioner extension.

[^bates-granger]: Bates, J. M. & Granger, C. W. J. (1969). "The Combination of Forecasts." *Operational Research Quarterly* 20(4): 451–468. The R²-weighted blending used in our volatility engine is an industrial variant of the inverse-MSE Bates-Granger weighting, not a Mincer-Zarnowitz construct (the MZ regression evaluates a forecast, it does not combine forecasts).

[^bn-nig]: Barndorff-Nielsen, O. E. (1997). "Normal Inverse Gaussian Distributions and Stochastic Volatility Modelling." *Scandinavian Journal of Statistics* 24(1): 1–13.

[^kull-beta]: Kull, M., Silva Filho, T. & Flach, P. (2017). "Beta calibration: a well-founded and easily implemented improvement on logistic calibration for binary classifiers." *AISTATS 2017*, PMLR 54:623–631.

[^grossman-zhou]: Grossman, S. J. & Zhou, Z. (1993). "Optimal Investment Strategies for Controlling Drawdowns." *Mathematical Finance* 3(3): 241–276. The result that drawdown-constrained log-optimal allocation scales with distance from the high-water mark is the canonical basis for our HWM-driven sizing.

The seven-step loop runs every second across every active contract. Most ticks reject (the bot is highly selective — the vast majority of markets do not pass the after-fee edge threshold), but the loop never sleeps.

## 1.3 What's live, what's shadow, what's killed

This section is more interesting than it looks. The single most important quality indicator for a multi-strategy trading system is **the willingness to turn things off when they stop working**. The current state, as of May 2026:

**Live (real capital at risk):**

- **15-minute crypto, seven assets.** BTC (min entry 88¢), ETH (90¢ main tier, 75–79¢ sub-tier capped at 50 contracts), SOL (86¢, taker-first due to thin orderbooks), XRP (92¢), HYPE (90¢), DOGE (85¢), BNB (90¢). HYPE and DOGE promoted to live trading on 2026-05-14 via the P2.3 expansion sweep (B.1 Brier sweep on T1 shadow data accumulated 2026-05-10 through 2026-05-14). BNB promoted to live trading on 2026-05-19 via the P2.4 sibling sweep (B.1-equivalent Brier sweep on T1 shadow data accumulated 2026-05-17 through 2026-05-19, n=721 settled; argmin matches ETH pattern at w=0.20 — BNB's raw model is well-calibrated, opposite of HYPE/DOGE which needed heavy market blend).
- **Six conditional overlays.** Decided contracts (four live tiers identifying near-certain outcomes via extreme z-scores), terminal momentum (96/98/99¢ trades in the final 1–5 minutes), low-price near-expiry (BTC 80–87¢ in the final 10–120 seconds), weekend discount (Sat/Sun 90¢+ at STC ≤ 600s), overnight discount (weekday 04–11 UTC 89¢+ at STC ≤ 600s), loss-burst cooldown (per-asset 2-hour lockout after any 15M loss; +$441/30d counterfactual at last measurement).
- **P4.1 band-calibrated sizing.** Promoted 2026-05-17. Kelly sizing on 15M trades now receives a band-stratified calibrated probability (42-cell hierarchical-shrunk empirical lookup) rather than the raw model probability — this only changes Kelly magnitudes, not trade selection. Soak through 2026-05-31.

**Observation (capital not at risk, calibration data accumulating):**

- **S&P 500 intraday** via Polygon.io and Finnhub price feeds, with VIX-adapted EGARCH and intraday-seasonal deseasonalization. Briefly live March 17, 2026 and reverted same-day after Polygon returned a 403 error, breaking the primary feed. Shadow throughout, with a HAR-RV competitor running in parallel.
- **Sports comeback signals** across 28 leagues (NBA, NHL, MLB, NFL, EPL, ATP/WTA tennis, soccer worldwide, UFC). Bayesian comeback model with conservative likelihood-ratio compression. Basketball alpha detected (69.2% WR n=39 at last reading; sequential probability ratio test running, has not yet converged on stop-or-continue).
- **Hourly crypto NO-side** at 40–54¢ price tier in single-contract verification mode. BTC NO at this tier had 53.9% WR (n=1,113, p=0.005) pre-kill.
- **Shadow strategies in 15M:** A1 (RecalibratedEGARCH), A2 (LightGBM), A3 (EGARCH gating), A4 (late-window 55–74¢).

**Killed (turned off, not deferred — explicit decision to halt with postmortem):**

- **Hourly crypto, full kill 2026-04-18.** Correlated multi-strike losses concentrated in the same hour-window across BTC/ETH/SOL/XRP. Re-enable path preserved (two env vars on the VPS), but the kill is the default state.
- **Weather NO-side, kill 2026-05-16.** Live from 2026-04-11 through kill in 1-contract verification mode at 39–40¢ NO. Settlement-rate review showed lifetime n=167 with 38.3% WR (Wilson 95% CI [23.6%, 47.0%]) against an assumed 70% prior — the near-ATM zone is the market-maker zone, with no available edge. The far-ITM NO band (4–12¢) showed 91.7% WR (n=157) in shadow and is where the next iteration of weather research is anchored.

The kill discipline matters. A system that ships every shadow strategy to live without an exit ramp accumulates dead-weight quickly. The architectural pattern — shadow → observation → live, with explicit pre-committed rollback rules — is the load-bearing quality control on every new strategy.

## 1.4 The calibration engine, properly described

The phrase "we calibrate model probabilities against outcomes" is in every quant trading whitepaper. What we actually run is materially more elaborate than that phrase implies, and previous versions of this document under-described it. Concretely, every 15-minute model probability passes through **five distinct layers** before it reaches a Kelly sizer:

**Layer 0 — Raw statistical probability.** NIG-CDF on a z-score that incorporates spot price, the blended volatility from §1.2 step 2, threshold, and time-remaining. Per-asset NIG parameters refitted from seven rolling days of 60-second returns.

**Layer 1 — Per-product CalEngine.** Each market type (15M crypto, weather-per-city, sports-per-group, SPX-D, hourly) maintains its own CalibrationEngine instance learning independently from settlement outcomes. The engine selects from a hierarchy that auto-promotes as data accumulates: a fixed logistic fallback (β=0.85, used when there is no training data); Platt scaling (2-parameter logistic; Platt 1999[^platt-1999]) once 200+ samples are available; Beta calibration (3-parameter; Kull/Silva Filho/Flach 2017[^kull-beta]) once 350+ samples are available; and a Bayesian linear regression variant for low-N regimes with credible-interval reporting. The progressive method-promotion-with-sample-size pattern is a practical industrial choice — there is no single canonical paper proposing it, but it sits on top of the Niculescu-Mizil & Caruana (2005) finding that isotonic and Beta-style methods require ~1000+ samples to beat Platt.[^nm-caruana] The 15M engine currently runs in **passthrough mode** — measured Brier of the raw probability is lower than the BLR fit, so we bypass the BLR layer until/unless that flips. Per-city weather, per-sport-group, and SPX-D engines run their full pipelines.

[^platt-1999]: Platt, J. C. (1999). "Probabilistic Outputs for Support Vector Machines and Comparisons to Regularized Likelihood Methods." *Advances in Large Margin Classifiers* (MIT Press), pp. 61–74.

[^nm-caruana]: Niculescu-Mizil, A. & Caruana, R. (2005). "Predicting Good Probabilities with Supervised Learning." *ICML 2005*.

**Layer 2 — cal_mlp v1.1 (per-asset MLP residual calibrator + Mondrian conformal).** This is the layer that makes the calibration pipeline distinctly non-textbook. Each of the four legacy 15M assets (BTC/ETH/SOL/XRP) has its own multi-layer perceptron trained as a residual calibrator over an eight-feature canonical set (`market_price`, `prob_breakeven_gap`, `seconds_to_close`, `time_decayed_proximity`, plus four asset-and-time encodings), wrapped in a **Mondrian conformal predictor** (Vovk 2012[^vovk-conformal]) for distribution-free, group-conditional coverage guarantees. The "Mondrian" partitions are `(price_tier, stc_bucket, vol_regime, side)` so that the coverage guarantee holds conditionally on the regime, not just on average. Bundles are version-pinned by a `cfg_fp` (eight-byte feature-set fingerprint) — the current production cfg_fp is `345978797274721f`. Sigma features are winsorized at ±25 to prevent terminal-STC blowups (a real prior incident: `spot_distance_to_strike_sigma` blows up to ±3,000+ as the time denominator goes to zero; without clipping, z-scoring across the column inflated standard deviation 100× and collapsed the real signal). **HYPE and DOGE bypass Layer 2 entirely** — they were promoted to live trading on 2026-05-14 via a Brier-sweep raw_prob direct-promote path (the cal_mlp training arc was retired for the HYPE/DOGE cohort). A four-feature `replay_v1` recipe exists in the training tooling for the historical-replay backfill used to score those assets pre-promotion, but it is not wired into serving.

[^vovk-conformal]: Vovk, V. (2012). "Conditional validity of inductive conformal predictors." *Proceedings of ACML 2012*, PMLR 25. The Mondrian variant is in [Shafer & Vovk (2008)](https://jmlr.csail.mit.edu/papers/volume9/shafer08a/shafer08a.pdf) and the canonical book [Vovk/Gammerman/Shafer (2005), *Algorithmic Learning in a Random World*](https://alrw.net/).

**Layer 3 — Per-asset market blend.** The calibrated probability is blended with the market-implied probability `best_ask/100` using per-asset weights. The current production weights are:

| Asset | Weight on market | Weight on model |
|---|---|---|
| BTC | 10% | 90% |
| ETH | 20% | 80% |
| SOL | 80% | 20% |
| XRP | 90% | 10% |
| HYPE | 80% | 20% |
| DOGE | 60% | 40% |

These weights replaced a legacy "60/40" default in May 2026 (P2.1.d / P2.3) after a 4×6 sim-PnL sweep across discrete blend ratios. The per-asset variation reflects the different per-asset Brier improvements from the cal_mlp v1.1 layer (BTC −13%, ETH −11%, SOL −3%, XRP −6%) — assets where the model materially outperforms market-implied get a low market weight; assets where the model is roughly market-equivalent (SOL, XRP) get a high market weight, defaulting to market as the more reliable signal.

**Layer 4 — Band-calibrated sizing (P4.1).** Promoted 2026-05-17. The Kelly sizer no longer receives the blended probability directly — instead it receives a probability looked up from a **42-cell hierarchical-shrunk empirical table** keyed on `(asset × price_band)`, with seven price bands (70–79 / 80–85 / 86–89 / 90–93 / 94–96 / 97–98 / 99). Cells with thin sample counts shrink toward the per-band aggregate prior with shrinkage parameter k=30; lookback is 30 days for the volatile 70–93¢ regime and 60 days for the thin-cell 94–100¢ regime. Trade selection gates and post-CalEngine probabilities are unchanged — only the magnitude of Kelly sizes shifts. Soak runs through 2026-05-31 with per-cell rollback rule (any cell drifts ≥5pp from baseline → revert that cell).

The total system has four substantial calibration steps (Layers 1–4) applied serially on top of the raw statistical probability (Layer 0), each with its own promotion gate, each with its own test pin in CI. No single layer dominates; together they implement what the calibration literature would call a **stacked / cascaded calibration architecture** with conformal coverage guarantees at the inner layer and empirical-table shrinkage at the outer.

## 1.5 Where the edge comes from — honestly

There are five plausible edge sources for a Kalshi systematic crypto trader. Most "edges" claimed in pitch decks turn out to be **one** of these wearing a clever name. Stated honestly:

1. **Speed and breadth of information processing.** The bot watches every market on every active contract, every second, with no fatigue and no anchoring. A retail trader physically can't compete on this axis — they will look at one contract at a time and skip 99% of the universe. This is durable across changing market conditions, but it is the *least* defensible edge because it can be replicated by any competent engineering team within months.
2. **Volatility sophistication.** Realized-kernel + EGARCH + NIG-fit is institutional-grade quant infrastructure that is currently rare among Kalshi participants. Susquehanna and Jump have arrived; eventually their crypto market-makers will eat most of this. We probably have months, not years, of differentiation here.
3. **Calibration sophistication.** The five-layer pipeline in §1.4 is more elaborate than what most participants are doing. Calibration matters disproportionately for Kelly-sized betting — the academic literature is explicit that for sizing decisions, calibration optimality dominates accuracy optimality.[^calibration-betting] This is durable for as long as the cal_mlp + conformal + band-calibrated machinery stays current with the data. It is also the layer that requires the most ongoing effort.
4. **Execution-cost optimization.** Maker-first with adaptive escalation, decided-contract direct-taker routing, and the empirical low-STC fill-rate collapse (under 10% at 0–60s STC, measured 0% at 75–180s STC — the data behind the 180s direct-taker threshold). Whelan (2025)[^whelan-kalshi] documents that on Kalshi, the maker-vs-taker fee gap is the single largest driver of net returns for sophisticated participants. We are aligned with this finding, and the optimization is mostly mechanical once you've measured it. Other systematic participants will converge on the same answer.
5. **Conditional overlays exploiting structural patterns** — weekend/overnight low-liquidity windows, terminal momentum in the final minutes, decided contracts at extreme z-scores. These exploit specific anomalies that the broader market hasn't priced. They are the most fragile of the five — each is one large-trader's serious effort from disappearing — but they're also among the most profitable per-trade today.

[^calibration-betting]: Wunderlich, F. & Memmert, D. (2023). "Machine learning for sports betting: should forecasting models be optimised for accuracy or calibration?" [arXiv:2303.06021](https://arxiv.org/pdf/2303.06021) — calibration-optimised models yielded +34.7% ROI vs −35.2% for accuracy-optimised in a head-to-head test. The general principle: log-loss aligns with Kelly; raw accuracy doesn't.

The realistic case is that this stack of edges, collectively, decays. **The bot is not the moat.** It is a high-quality, currently-profitable consumer of a more durable asset, which is the topic of Section 2.

---

# 2. The Data Corpus

## 2.1 The reframe

In the technical literature on quantitative trading, the implicit hierarchy is *strategy → data → infrastructure*. Strategy is the asset, data is an input, infrastructure is a cost center.

This is the wrong hierarchy for a small operator in 2026.

In a market where alpha-extraction techniques can be Googled, where institutional market makers can replicate any well-described strategy within months, and where the cost of compute to *try* every published technique has collapsed, the durable asset is **the data that no one else has and that cannot be retroactively created**. Two specific framings support this:

**Bloomberg.** A privately-held data company. Roughly $10 billion per year in revenue from the Terminal, which is — at its core — a curated, normalized, historically-deep proprietary data feed.[^bloomberg-rev] The analytics are not the moat. The cleaned tick history going back decades is the moat. Hamilton Helmer's *7 Powers* framework explicitly identifies Bloomberg's data corpus as a "cornered resource."[^7powers]

[^7powers]: Helmer, H. (2016). *7 Powers: The Foundations of Business Strategy*. Cornered Resource is the relevant category for data assets. Coverage in [The Terminalist](https://theterminalist.substack.com/p/bloombergs-7-powers-and-why-the-terminal).

**Renaissance Technologies.** Per Zuckerman's *The Man Who Solved the Market*[^rentec] (the only book-length public account), Renaissance's most-defensible asset is a multi-decade tick archive going back to the 1960s. The strategy stack on top of it has changed several times across the firm's history; the data has accumulated continuously. The standard insider quote: "We can lose the strategies and rebuild. We can't lose the data."

The same logic applies to a Kalshi-focused operator, with one important difference: Kalshi is **new**. Kalshi launched its crypto contracts in December 2025. Every day a small operator captures from that launch onward is a day that no later-arriving competitor — however well-capitalized — can purchase, license, or reproduce. Once the day has passed, the WebSocket frames are gone from Kalshi's transient infrastructure and exist nowhere else.

## 2.2 What we're actually capturing

The Data Corpus is a **medallion-architecture** data lake (bronze / silver / gold) hosted in S3, built using Databricks' canonical pattern[^medallion] but adapted for byte-fidelity event capture. Bronze went live on **2026-05-17** (collector service started at 09:57:59 UTC; first chunk uploaded ~09:52 UTC).

[^medallion]: [Databricks Medallion Architecture](https://www.databricks.com/blog/what-is-medallion-architecture); [Microsoft Learn](https://learn.microsoft.com/en-us/azure/databricks/lakehouse/medallion). Bronze = raw immutable source of truth; Silver = cleaned, typed, validated; Gold = business-ready aggregates.

**Bronze**: byte-exact JSON-line capture of every WebSocket frame, compressed with Zstandard and uploaded to `s3://kalshi-bot-archive/bronze/`. Bronze day-zero (first non-empty chunk in S3) was approximately 09:52 UTC on 2026-05-17; the collector service is currently running continuously since an operator-initiated restart at 09:57:59 UTC the same day. Each bronze line carries a six-field envelope: `_wire_recv_ts` (microsecond UTC, captured pre-parse), `_source` (currently `kalshi_ws`), `_conn` (which of the parallel WebSocket connections — A through F), `_channel` (`orderbook_delta`, `trade`, `market_lifecycle_v2`), `_collector_seq` (monotone per-collector sequence number from boot), and `_raw` (the full unparsed Kalshi wire payload as a string). The envelope is **engineered to be irreversible** — the raw payload is preserved verbatim, so any future analysis can verify exactly what Kalshi sent, not what we thought Kalshi sent. Partitioning is Hive-style date-leading: `bronze/<source>/<channel>/year=YYYY/month=MM/day=DD/hour=HH/conn=<X>/<chunk>.jsonl.zst`. Rotation cadence is 5 minutes or 100 MB whichever first. The upload protocol — write to tmp, fsync, atomic rename to outbox, rclone copy with `--checksum --immutable`, verify byte-exact match, delete local — implements the canonical event-sourcing pattern[^event-sourcing] and never deletes a local file unless S3 has confirmed receipt of the byte-exact match.

[^event-sourcing]: [Event Sourcing pattern (Azure Architecture Center)](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing); [AWS Prescriptive Guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/event-sourcing.html).

**Silver**: Cleaned, typed Parquet derivatives produced by an off-VPS DuckDB + dbt pipeline. Schema-versioned at both the path level (`silver/v1/...` → `silver/v2/...`) and the row level (`_silver_schema_version` column). The pipeline is planned but not yet built (the bronze layer is the load-bearing piece; silver can be regenerated from bronze at any future date, so its construction does not need to race the data).

**Gold**: Joined, labeled, feature-ready tables for direct consumption by trading models. Path-versioned. Also planned but not yet built.

**Storage cost**: roughly $20–50 per month in projected steady state, dominated by S3 storage at the DEEP_ARCHIVE tier (bronze transitions to DEEP_ARCHIVE at 30 days; never expires). This is approximately the cost of one nice dinner per month for an asset that grows continuously and that no external party can buy.

**Bot-collector isolation**: this is a structural rather than documentary property. The collector runs in a separate Python process, in its own systemd unit, at `Nice=10` priority (bot runs at `Nice=0`, so the bot preempts when both compete for CPU), with a soft `MemoryHigh=400M` cgroup throttle plus a hard `MemoryMax=512M` kill, using a separate API key, with zero imports from the bot codebase (enforced by `import-linter` contracts in CI). A collector crash cannot affect bot trading; a bot crash cannot affect data capture. The off-switch is `systemctl stop kalshi-collector`; the inverse is `systemctl stop kalshi-bot`. Each is independent. A shared `kalshi_wire/` package houses RSA-PSS authentication and the WebSocket transport (connect, reconnect, silence-watchdog, frame ingress); this is the "two sides of the same coin" architectural amendment of 2026-05-16 that prevents bot-collector drift in how Kalshi frames are received at the wire. The two consumers still share byte-identical `Frame.raw` (pinned by a differential test); the collector additionally opts into a parse-skip mode post P1-B-brutalist (2026-05-20) for throughput at high-ticker scale.

## 2.3 The corpus landscape

The public landscape for Kalshi historical data is materially thinner than it is for Polymarket. The full survey, as of May 2026:

- **Kalshi's own API** exposes historical *trades* and *orders* via REST endpoints (the historical-data tier on `docs.kalshi.com/getting_started/historical_data`).[^kalshi-hist-api] These are derived summary tables — neither byte-exact WebSocket frames nor the full orderbook delta stream.
- **Predexon** (`docs.predexon.com/api-reference/kalshi/orderbooks`) offers historical orderbook **snapshots** for Kalshi markets, **starting January 7, 2026**.[^predexon] Snapshots, not raw WebSocket payloads. Pre-January-7-2026 data is not available at any public source.
- **Lychee Data** (`lycheedata.com/guides/kalshi-historical-data`) provides CSV/Excel/JSON download of Kalshi historical data for backtesting.[^lychee] Derived format, not raw frames.
- **The Polymarket archivists** — pmxt.dev, jon-becker's GitHub research dataset, and similar — do not cover Kalshi at all.

[^kalshi-hist-api]: [Kalshi Historical Data API documentation](https://docs.kalshi.com/getting_started/historical_data); [Get Historical Trades](https://docs.kalshi.com/api-reference/historical/get-historical-trades); [Get Historical Orders](https://docs.kalshi.com/api-reference/historical/get-historical-orders).

[^predexon]: [Predexon Orderbook History API](https://docs.predexon.com/api-reference/kalshi/orderbooks).

[^lychee]: [Lychee Data — Kalshi Historical Data Guide](https://lycheedata.com/guides/kalshi-historical-data).

This is the differentiation that matters. **No publicly-listed source offers byte-exact WebSocket-frame archive of Kalshi.** Predexon's snapshots are the nearest comparable product; they begin January 7, 2026, are snapshot-derived (not raw frames), and do not capture the cancel-replace event sequence between snapshots. Lychee is a re-distributor of REST-pull data. Kalshi's own API exposes only the historical-tier derived tables.

The defensible claim is precise: **byte-exact WebSocket-frame capture of Kalshi, with the unparsed wire payload preserved verbatim, with day-zero coverage of every product Kalshi launches going forward (the 15-minute crypto contracts launched only in December 2025 — pre-collector capture of these contracts at byte fidelity does not exist anywhere outside Kalshi's internal systems).** Every day post-2026-05-17 that the collector is running adds a day of data that has no public substitute and cannot be retroactively reconstructed.

The question "what is the byte-exact WebSocket frame fidelity good for, vs. snapshots?" has empirical answers that snapshot-based capture cannot provide:

1. **Microstructure replay** — backtests that need to replay exactly what a participant would have seen at a given microsecond can only be run from the wire payload. Snapshot-based capture loses cancel-replace pairs, latency variations, and the actual event sequence.
2. **Model training on raw inputs** — every published machine-learning approach to limit-orderbook dynamics (Cont/Stoikov/Talreja 2010[^cont-stoikov]; Gould & Bonart 2016[^gould-bonart]; deep-learning approaches in 2024–25[^lob-dl]) requires per-message granularity. Minute-bar data is unusable.
3. **Forensic reconstruction** — if Kalshi disputes a settlement, the wire payload is the only ground truth. Snapshots taken at intervals miss the event we'd need to reconstruct.
4. **Schema-drift survival** — Kalshi has already revved at least one channel (`market_lifecycle_v2`). Bronze captures the raw payload, so future schema interpretations remain possible. Snapshot capture freezes the interpretation at the moment of capture.

[^cont-stoikov]: Cont, R., Stoikov, S. & Talreja, R. (2010). "A Stochastic Model for Order Book Dynamics." *Operations Research* 58(3): 549–563. [PDF](http://www.columbia.edu/~ww2040/orderbook.pdf).

[^gould-bonart]: Gould, M. & Bonart, J. (2016). "Queue Imbalance as a One-Tick-Ahead Price Predictor in a Limit Order Book." *Market Microstructure and Liquidity* 2(2). [arXiv 1512.03492](https://arxiv.org/pdf/1512.03492).

[^lob-dl]: e.g., [Deep Limit Order Book Forecasting (arXiv 2403.09267)](https://arxiv.org/html/2403.09267v1); [Order Book Filtration (arXiv 2507.22712)](https://arxiv.org/html/2507.22712v1).

## 2.4 Realistic upside scenarios for the corpus

The corpus's realized value depends on what one does with it. Three buckets of scenarios, in roughly increasing speculation:

**Scenario A — Internal research substrate.** The corpus is the input to the bot's own ongoing strategy research. Every new shadow strategy backtests against the full historical record. Every cal_mlp version trains on a richer feature set. The model retraining cycle in §1.4 becomes substantially better-grounded as the corpus deepens. This is the **conservative case** — no external monetization, just a continuously-improving internal asset. The cost is the storage bill. There is no execution risk on this scenario; it happens automatically as bronze accumulates.

**Scenario B — Research partnerships.** Academic researchers and small-team quants who want to study Kalshi microstructure currently have no commercial source for byte-fidelity data. Wolfers, Whelan, and other named prediction-market researchers in the broader literature have no comparable dataset to work with. A formal research-data-sharing arrangement (with publication credit, possibly small honoraria) is a low-effort scenario that increases the corpus's external footprint without disturbing trading. **Polymarket's $112M acquisition of QCEX (April 2025)**[^qcex] — its mechanism for entering the regulated U.S. event-contracts market — and Nasdaq's earlier acquisition of Quandl (December 2018, price undisclosed)[^quandl-nasdaq] are precedents for "regulated-market entity acquiring an adjacent data or licensing asset." These are speculative as direct comps but the category exists.

[^qcex]: QCEX was acquired by Polymarket (Polymarket's mechanism for re-entering the U.S. market under a CFTC-licensed venue), April 2025. [Sports Illustrated overview](https://www.si.com/betting/prediction-market/prediction-markets-101/the-difference-between-kalshi-vs-polymarket-what-us-traders-actually-need-to-know-in-2026).

[^quandl-nasdaq]: [Nasdaq press release](https://ir.nasdaq.com/news-releases/news-release-details/nasdaq-acquires-quandl-advance-use-alternative-data).

**Scenario C — Productized data feed.** The corpus, surfaced through a clean API or research-friendly Parquet distribution, becomes its own revenue line. The closest comp is the alternative-data industry, projected at ~$79B by 2029.[^altdata] This requires significant build (API, billing, documentation, compliance review, customer support) and is well outside the current operating posture. It is a real option, not a plan.

[^altdata]: [Arizton Alternative Data Market Report](https://www.arizton.com/market-reports/alternative-data-market/market-size).

The honest framing is **Scenario A happens by default, Scenario B is opportunistic, Scenario C is a far-future option**. The capital ask in this whitepaper is not predicated on B or C materializing.

## 2.5 What makes the corpus durable

The structural properties that protect the corpus from being out-competed:

- **Capture timing**. Already started. Cannot be retroactively replicated. Every day the corpus exists adds a day of irreplicable history.
- **Capture fidelity**. Byte-exact wire payload, not derived snapshots. The wire payload is the bedrock; everything else is downstream of it.
- **Immutability discipline**. Bronze cannot be mutated by design. The "capture promiscuously, filter at read" event-sourcing pattern means new analyses can be derived from old data without losing the option to derive future-unknown analyses.
- **Schema-versioned silver/gold**. The two derivative tiers can rev without disturbing bronze. We can change our mind about what the data means at any time and the original payload remains queryable.
- **Off-VPS compute discipline**. The bot's VPS does not run silver/gold ETL — that runs on commodity Mac/cloud hardware. The bronze layer is isolated from any analytical workload.
- **Isolation contract** (per §2.2). The corpus's continued capture is structurally independent of the bot's continued trading.

The corpus does not need the bot to succeed. The bot's edge can decay completely and the corpus continues accumulating. That asymmetry is the strategic point of Section 2.

---

# 3. The Operating Model: One Person + Many Agents

## 3.1 What is actually different about 2026

Sam Altman has been publicly discussing the "one-person billion-dollar company" thesis since 2024[^altman-1p]; the recent variation is that "we're dangerously close." Forrester has explicitly called this thesis "magical."[^forrester-counter] Both can be partially right. The empirically verifiable fact is more useful than either claim: as of May 2026, the *same* large language model placed in different agentic scaffolds shows **performance differentials of 15+ percentage points** on standardized coding benchmarks (SWE-bench Verified).[^swebench] In plain English: the model is roughly fixed; the discipline around it is everything.

[^altman-1p]: [Fello AI summary of Altman remarks](https://felloai.com/2025/09/sam-altman-other-ai-leaders-the-next-1b-startup-will-be-a-one-person-company/).

[^forrester-counter]: [Forrester counterpoint blog](https://www.forrester.com/blogs/beware-the-magical-two-person-1-billion-ai-driven-startup/).

This project operationalizes that fact. The development methodology is not "one founder uses Claude as a productivity tool." It is "one founder *designs the scaffold* and then operates within it." The scaffold is the asset.

## 3.2 The five pillars

The agentic-engineering discipline is structured around five named "Pillars" that govern how every change to the codebase is made. Pillar 1 is the foundational CI/test scaffolding (described in Parts VIII and IX of the technical whitepaper) — pytest tiers, GitHub Actions deploy pipeline, basic linting. Pillars 2–5 are the discipline that makes agentic engineering load-bearing rather than ornamental. They are reinforced by Claude Code hooks — automatic interventions that block disallowed actions before they happen — and by import-linter[^importlinter] contracts that act as architecture fitness functions in the sense of Ford, Parsons & Kua's *Building Evolutionary Architectures*.[^evolutionary-arch]

[^importlinter]: [import-linter on GitHub](https://github.com/seddonym/import-linter).

[^evolutionary-arch]: Ford, N., Parsons, R. & Kua, P. *Building Evolutionary Architectures: Automated Software Governance* (O'Reilly, 2nd ed.). [Thoughtworks](https://www.thoughtworks.com/en-us/insights/books/building-evolutionaryarchitectures-second-edition).

**Pillar 2 — Paved Roads (Hooks + Skills).** Following the Netflix paved-road tradition[^netflix-paved] and Spotify's golden-path framing[^spotify-golden], we make the desired path the easiest path. Concretely: Claude Code hooks fire on tool invocations to block bad-shape commits (e.g., a hook prevents `git push --force` to main without explicit override); skills package well-tested workflows (e.g., `/deploy`, `/investigate`, `/audit`) that the agent prefers to ad-hoc shell commands; CLAUDE.md files at every directory load context automatically so that the agent never needs to discover conventions by trial and error.

[^netflix-paved]: [Netflix Paved Roads](https://seifrajhi.github.io/blog/paved-roads-netflix-developers/).

[^spotify-golden]: [Platform Engineering: Golden Paths That Actually Go Somewhere](https://platformengineering.org/blog/how-to-pave-golden-paths-that-actually-go-somewhere).

**Pillar 3 — Equivalence Snapshots.** Engine outputs are pinned against a 1,000-row reference corpus. If a change to the volatility engine, the probability engine, or the calibration pipeline alters output on the reference corpus, the test fails. Snapshots are **never auto-regenerated** by agents — regen is a human-with-diff-review operation. This catches the entire class of "agent confidently made a refactor that silently shifted model behavior" failures.

**Pillar 4 — TDD-First.** Modeled on the Claude Code community's TDD discipline (which has been articulated in multiple write-ups since late 2025[^tdd-claude]) and the Anthropic engineering culture more broadly, the rule is: before any non-trivial change, an agent must write the failing regression test, confirm it fails on `main`, then make the smallest change that turns the test green. The `/test-writer` skill scaffolds this. The TDD discipline is what makes AI-generated code reviewable — the test asserts the change's *meaning*, and the human reviewer reads the test, not the implementation.

[^tdd-claude]: [InfoQ: Inside Claude Code Creator's Workflow](https://www.infoq.com/news/2026/01/claude-code-creator-workflow/); [alexop.dev: Forcing Claude Code to TDD](https://alexop.dev/posts/custom-tdd-workflow-claude-code-vue/); [The New Stack: Claude Code and the Art of TDD](https://thenewstack.io/claude-code-and-the-art-of-test-driven-development/); [Pragmatic Engineer: How Claude Code is Built](https://newsletter.pragmaticengineer.com/p/how-claude-code-is-built).

**Pillar 5 — Tiered Test Suite + mutmut + testmon.** Tests are stratified into tiers (contracts → integration → unit → equivalence → regression), `testmon` runs only the tests affected by current changes for fast feedback, and `mutmut` mutation-tests run out-of-band against the engine modules to catch the class of "test passes but doesn't actually constrain the code." The current test count is **~6,506 tests across ~270 test files** at this writing.

**Adversarial review.** The most distinctive pattern. Risky changes (anything affecting the bot's main loop, the calibration pipeline, the execution layer, or the data corpus) are reviewed by a separate AI agent prompted to find every fault — pretending the change is being submitted by a hostile contributor. The change ships only when the adversarial reviewer returns **two consecutive rounds with zero CRITICAL and zero MAJOR findings**. This is a production application of the Constitutional AI[^constitutional-ai] and AI Safety via Debate[^debate] patterns from the AI safety literature: one agent generates, another critiques against an explicit rubric. In practice, R1 typically catches 3–8 issues, R2 catches 0–3 follow-up issues, and most "Bits" (the unit of shippable change) clear in 2–4 rounds. Particularly elaborate Bits with multi-surface lockstep concerns have cleared in 7+ rounds; the canonical discipline anticipates this in its sacred-rule language ("some Bits need 8").

[^constitutional-ai]: Bai, Y. et al. (2022). "Constitutional AI: Harmlessness from AI Feedback." [arXiv:2212.08073](https://arxiv.org/abs/2212.08073).

[^debate]: Irving, G., Christiano, P. & Amodei, D. (2018). "AI Safety via Debate." [arXiv:1805.00899](https://arxiv.org/abs/1805.00899).

## 3.3 The verification gap

The binding constraint on agentic engineering — and the reason the discipline above is load-bearing rather than ornamental — is what Andrej Karpathy and Jason Wei have called the **verification gap** (sometimes "asymmetry of verification").[^karpathy-vg][^wei-verification] LLMs can generate plausible code faster than a human can verify it; the gap between "agent claims a change is done" and "we have evidence the change is correct" is the dominant source of agentic engineering failure modes.

[^karpathy-vg]: [Karpathy on X (status 1930305209747812559)](https://x.com/karpathy/status/1930305209747812559); ["Software 3.0" on Latent Space](https://www.latent.space/p/s3).

[^wei-verification]: Wei, J. "Asymmetry of Verification and Verifier's Law." [jasonwei.net](https://www.jasonwei.net/blog/asymmetry-of-verification-and-verifiers-law).

The five-pillar architecture is, at its core, an industrial response to the verification gap. Tests close it from the correctness side. Equivalence snapshots close it from the regression side. Import-linter contracts close it from the architecture side. Adversarial review closes it from the "did the agent miss something subtle" side. None individually is sufficient; collectively, they make verification roughly comparable in speed to generation, which is the condition under which agentic engineering scales.

## 3.4 What this enables in practice

The pace of shipped change is the empirically observable output. As of the date of this whitepaper, the project shipped a partial list of substantial structural changes in May 2026 alone:

- The Data Corpus bronze layer (3 months of design work, 6 sub-Bits across two weeks, day-zero capture achieved 2026-05-17).
- Per-asset market blend rollout (4×6 sim-PnL sweep, atomic 3-commit deploy).
- HYPE and DOGE promoted to live trading via a structured Brier sweep across shadow data.
- The P4.1 band-calibrated sizing layer (42-cell empirical table with hierarchical shrinkage).
- The weekend-discount Kelly-sign followup chain (6 PRs closing an ambiguous-proxy class bug).
- Three CI performance fixes that materially shrank the deploy cycle.
- The collector health-monitor cron job + Telegram alerting.

The traditional team-size estimate for this rate of structurally significant change, at this level of test discipline, is somewhere between five and fifteen engineers depending on how productive one assumes the team to be. Currently: one person plus agents.

## 3.5 What this doesn't enable

It is important to be honest about what the operating model does not solve.

- **The model can't run while the operator sleeps.** The bot does — but new development requires the operator. There is no "agent works overnight on a hard problem" yet; coding tasks are interactive with frequent human checkpoints, particularly on high-risk changes.
- **Adversarial review catches narrowly, not broadly.** It will find the specific bug class it is prompted to look for; it will miss the bug class no one thought to ask about. This is a real failure mode and is why postmortems with named lessons (currently 100+ "L" lessons in the project's knowledge base) are part of the development cycle.
- **The model is not infinite leverage.** Engineering time-to-completion has dropped, but the operator's attention budget is the binding constraint. Adding more AI agents past a certain point does not produce more output; it produces more output the human has to verify.
- **Some tasks remain human-only**: capital deployment decisions, strategic kills (e.g., the May 16 weather NO-side decision), incident calls during live trading anomalies. Operational and judgment work do not delegate well to agents.

## 3.6 Why this is durable

The operating model is itself a moat — but a different kind of moat than the bot or the corpus. The bot's edge decays through competitive pressure; the corpus appreciates without effort; the operating model **compounds through investment**. Every new hook, every new skill, every named lesson encoded in CLAUDE.md, every additional fitness function in CI is a permanent reduction in the cost of future change. Three years from now, the discipline embedded in the development environment is *itself* a substantial fraction of the project's total intellectual property.

The phrase that captures it: the bot is the proof-of-concept, the corpus is the asset, and the agentic-engineering discipline is the velocity at which the next bot, and the one after that, can be built.

---

# 4. Risk Management

This section covers the risk controls that prevent any single bad day from materially impairing the platform. The controls are layered: any single failure has to defeat multiple independent guards before it affects capital.

## 4.1 Position sizing — what's actually in production

Per-trade sizing is **edge-tiered fractional Kelly with parameter-uncertainty discounting.** Eight tiers map fee-adjusted edge to risk-fraction-of-bankroll:

| Fee-adjusted edge | Risk fraction | Rationale |
|---|---|---|
| ≥ 4.0% | 25% | High-conviction; large edge survives most plausible parameter errors |
| ≥ 2.5% | 20% | Confident |
| ≥ 1.8% | 15% | Strong |
| ≥ 1.2% | 10% | Standard |
| ≥ 0.9% | 7% | Moderate |
| ≥ 0.7% | 5% | Thin |
| ≥ 0.5% | 3% | Marginal |
| ≥ 0.25% | 2% | Minimum acceptable |

This is **operationally** a fractional-Kelly approximation — it is not an academic construct. Full Kelly is a poor production choice for two reasons that the literature is explicit about: (a) the optimal Kelly fraction is extremely sensitive to overestimation of `p` (Thorp's well-known result that betting past optimum is monotonically destructive of growth)[^thorp-frac]; (b) the realized drawdown distribution under full Kelly is much heavier-tailed than is psychologically tenable. Practitioner consensus, including the MacLean/Thorp/Ziemba (2011) handbook[^mtz], is fractional Kelly at 0.25× to 0.5× of theoretical optimum.

[^thorp-frac]: Thorp, E. O. (2006). "The Kelly Criterion in Blackjack, Sports Betting, and the Stock Market." *Handbook of Asset and Liability Management* Vol. 1, pp. 385–428. [PDF mirror](https://gwern.net/doc/statistics/decision/2006-thorp.pdf).

[^mtz]: MacLean, L. C., Thorp, E. O., & Ziemba, W. T. (eds.) (2011). *The Kelly Capital Growth Investment Criterion: Theory and Practice.* World Scientific. [Publisher page](https://www.worldscientific.com/worldscibooks/10.1142/7598).

## 4.2 Drawdown-scaled sizing — implementing Grossman-Zhou

Drawdown control is **structurally enforced**, not discretionary:

| Balance vs. 7-day rolling cash HWM | Sizing multiplier |
|---|---|
| ≥ 85% | 1.0× (full) |
| 75–85% | 0.5× (half) |
| 65–75% | 0.25× (quarter) |
| < 65% | 0.10× (floor — never zero) |

This is a discrete approximation of the Grossman-Zhou (1993) result that drawdown-constrained log-optimal allocation scales with distance from the high-water mark.[^grossman-zhou] The HWM mechanism itself is borrowed from the hedge-fund incentive-fee literature (Goetzmann, Ingersoll & Ross 2003).[^girs] The bottom step is a 10% floor on size — explicitly not a complete halt. The reasoning is that the feedback signal from continued small-size trading is more valuable than a hard freeze when distinguishing a recoverable streak from a regime break. A true halt remains available via operator kill-switch (`OBSERVATION_MODE=True` or `systemctl stop kalshi-bot`).

[^girs]: Goetzmann, W. N., Ingersoll, J. E., & Ross, S. A. (2003). "High-Water Marks and Hedge Fund Management Contracts." *Journal of Finance* 58(4): 1685–1718.

Per-asset caps are tighter than the global Kelly cap: BTC 15%, ETH 20%, SOL 15%, XRP 15%, HYPE 10%, DOGE 10%, BNB 10%. SPX uses eighth-Kelly (0.125); the weather YES-side simulation used quarter-Kelly (0.25); weather NO was 1-contract verification-mode prior to the May 16 kill.

## 4.3 Edge-detection guards

Before any trade is placed, five independent checks must pass:

1. The model's estimated probability is high enough to justify the contract price.
2. The fee-adjusted edge exceeds the price-dependent minimum (0.25% at 80–90¢, dipping to 0.20% at 91–92¢, climbing to 1.0% at 97¢+) — V-shaped because near-100¢ entries have asymmetric loss distributions.
3. The contract price is within hard per-asset bounds (BTC 88¢+, ETH 90¢+ main / 75–79¢ capped sub-tier, SOL 86¢+, XRP 92¢+, HYPE 90¢+, DOGE 85¢+, BNB 90¢+).
4. The z-score is not extreme (|z| > 25 rejects — preserves valid high-conviction trades while blocking obvious data-corruption inputs).
5. Model-market discrepancy guard — if the model says ≥90% but the market is below 75¢, the model is suspected of missing material information and the trade is refused.

Each check is independent; **any one rejection refuses the trade.** The system errs heavily toward saying no.

## 4.4 Execution-side guards

Crash recovery is structural: every order's `order_id` is written to SQLite *before* the API request is submitted. If the bot crashes between submission and confirmation, the next restart reconciles state from Kalshi's API without duplicating or losing the order.

The maker-first execution stack has three rejection-handling tiers and a hard direct-taker fallback below 180 seconds. Whelan (2025)[^whelan-kalshi] is explicit that on Kalshi the maker-vs-taker gap is the largest single driver of net returns for sophisticated participants — our execution stack is aligned with this finding.

Order escalation prefers in-place amendment (`amend_order`) over cancel-then-replace, which is materially faster (amend preserves the queue position; cancel-replace loses it).

## 4.5 Operational guards

A separate **collector health-monitor** cron (shipped 2026-05-17 as `scripts/ops/collector_health_monitor.py`) emits Telegram alerts for disk pressure, WebSocket reconnection storms, and service-down events. The bot itself emits Telegram alerts for losses, large fills, and any error condition. An AI analyst (`bot/ai/analyst.py`) examines every losing trade and emits high-confidence root-cause findings via Telegram.

The SQLite database runs in WAL mode with `busy_timeout=30000` (30-second wait on lock contention) and per-row retry-on-busy logic. Journal archives rotate every 4 h to hour-stamped archives (`ops/rotate_journals.sh`, tracked in git since 2026-09-05) and upload to S3 30 min after each rotation tick with the same `--checksum --immutable` rclone discipline used by the data collector.

The VPS runs systemd with `Restart=always` on the bot and `Restart=on-failure RestartSec=10s` on the collector. If either service crashes, it restarts within seconds.

## 4.6 What's not covered by these guards

Stated honestly: a regulator-driven Kalshi outage, a CFTC enforcement action against the venue, a sustained spot-feed outage across multiple exchanges simultaneously, or a Kalshi API protocol change that breaks the bot's WebSocket parser — these are not directly mitigated. The kill-switch (`OBSERVATION_MODE=True` or stopping the systemd unit) is fast, but realized losses in the minutes between event and kill are not zero.

---

# 5. Capital Ask Context

The motivation for raising capital is not "we have figured everything out and need scale to deploy it." It is "we have built a defensible system that produces measurable cash flow, and additional capital is the binding constraint on three specific use cases."

**Use case 1 — Increased per-trade size while preserving the same risk profile.** The bot's edge-tiered sizing percentages are bankroll-fractions. Doubling the bankroll doubles the dollar size of every trade without changing the risk-of-ruin properties; tripling triples; and so on, until per-trade size starts hitting Kalshi's orderbook depth limits (which empirically begin to bind at five-figure-per-trade sizes on thinner asset/strike combinations). The math here is unsexy and well-defined: larger bankroll, same fractional Kelly, same drawdown-scaled multiplier, more absolute dollars per trade.

**Use case 2 — Off-VPS compute for silver/gold ETL and model retraining.** The bot VPS is a 2-vCPU / 2-GB-RAM DigitalOcean droplet; the operator has been explicit that the VPS is not the right place for batch ML or large data processing. Several pieces of work (silver/gold ETL, cal_mlp v2/v3 training, multi-asset feature engineering, weather research reboot) are constrained by available compute outside the bot VPS. A modest cloud-compute budget (or a research-grade Mac Studio) directly accelerates the next-generation model work.

**Use case 3 — Selective hire(s).** The operating model in §3 scales much further than is initially obvious, but it has a ceiling. A single founder's attention budget is the binding constraint, not LLM API costs. Selective addition of (a) a part-time quant researcher for model R&D, (b) an operations contractor for the weeks-during-vacation continuity gap, or (c) eventually a second engineer once the codebase warrants it, are the realistic uses of capital for sustained scale.

**What capital is not for**: subsidizing strategies that haven't validated, hiring before the operating model proves it can absorb headcount without quality regression, marketing the data corpus before bronze has months of accumulation, or attempting to compete with SIG/Jump/Tradeweb on raw liquidity provision (the wrong fight).

The minimum-viable-effective bankroll for the current bot configuration is roughly 5× the present operating account. Materially larger bankrolls run into per-trade depth limits and require either tighter per-trade size capping or expanding the asset universe (a research project, not a deployment of capital alone).

---

# 6. Fair-Witness Disclosures

What follows is what I would want to read if I were considering writing a check.

**Edge decay is real and modeled.** The systematic edges in §1.5 are not guaranteed to persist. Susquehanna and Jump Trading entering Kalshi in 2024–2026 is the obvious near-term competitive pressure. The strategic response is the corpus + agentic-engineering velocity — not the assumption that current edges hold.

**The corpus is not yet monetized.** Section 2 describes the corpus's structural properties; it does not promise revenue from external parties. The conservative case is internal research substrate, with zero external monetization. Any business model on top of the corpus is optionality, not committed plan.

**The Whelan paper is a real counterpoint and is referenced honestly.** Whelan (2025) argues that on Kalshi, edge accrues primarily to market makers via the maker/taker fee asymmetry, not to forecast-quality traders.[^whelan-kalshi] (Kalshi does not pay maker rebates in the Nasdaq sense; the "advantage" is paying a substantially lower fee on resting fills, not receiving a payment.) Our position: we are not market makers, but we use maker-first execution to capture the fee asymmetry on the taker side. The system's profitability after fees is the empirical test of whether forecast-quality plus execution-cost-optimization can be net positive in this market structure. The audit trail to date suggests yes, with the honest caveat that the test continues every day.

**Maker-fee schedule needs operator verification.** The bot's current configuration treats maker fee as $0; Kalshi's public fee schedule effective February 2026 documents a maker fee of approximately `ceil(0.0175 × C × P × (100−P) / 100)` cents (one-fourth of the taker rate, same dimensional form). If maker fees apply to the bot's 15M crypto contracts, the bot's edge calculation understates total trading cost by approximately 0.4¢ per maker fill at typical 90¢ entries. This is a tracked followup; the actual fee paid is observable on every fill, so the discrepancy (if it exists) would surface quickly in any operator audit.

**The win rate ≠ edge.** Reported win rates (high — see §0 metric box) are partly a function of trading mostly at high prices (≥85¢ entries) where the bot's pricing model expects to win frequently. A 95% win rate at 92¢ entries does *not* imply 95% return on capital. The correct arithmetic per contract is: expected payout = `0.95 × $1 + 0.05 × $0 = $0.95`, cost = `$0.92`, expected profit = `$0.03`, gross ROI per bet ≈ `$0.03 / $0.92 ≈ +3.3%` (before fees; fees and slippage shave further). The relevant metric is **fee-adjusted edge per trade × number of trades**, which is what the sizing math uses, not the headline win rate. We disclose win rate because it's the standard headline number, not because it is the most informative number. Worth noting in the opposite direction: at 97¢+ entries with the same 95% WR, expected payout = `$0.95`, cost = `$0.97`, expected profit = `−$0.02`, gross ROI ≈ `−2.1%` — which is exactly why the bot's price-dependent minimum-edge schedule requires 1.0% edge at 97¢+ entries (§4.3 / technical paper §4.2).

**Past results are over a short live-trading window.** February 22, 2026 to date is several months. Out-of-sample performance over longer windows, multiple macro regimes, and adverse selection during liquidity crises is not yet observed. The shadow→observation→live promotion pipeline is the structural mitigation, but it is not a guarantee.

**The one-person operation is a single point of failure.** If the operator is incapacitated, there are no successor humans currently trained on the system. The kill-switches are operational, but ongoing iteration would stop. This is mitigated by the documentation discipline (CLAUDE.md, agent_docs/, kb/) which makes the system substantially more recoverable by an external engineer than typical bespoke trading code — but it's not zero risk.

**The VPS is one machine.** Trading runs on one DigitalOcean droplet at 45.55.181.30. There is no automatic failover to a secondary instance. Service downtime during a regional outage would halt trading. This is acceptable given the system's scale; it would not be acceptable at materially larger size.

**The data corpus is not unique only by luck of timing.** Section 2.3 surveys the public landscape: no byte-exact WebSocket-frame archive of Kalshi exists outside this project. Predexon offers snapshot-derived orderbook history starting January 7, 2026; Kalshi's own API exposes historical-tier derived trades and orders. The differentiation — raw wire frames, immutable, day-zero coverage of new products — is durable as long as we keep capturing. The risk is structural: a future Kalshi product change that throttles WebSocket capture, or a competitor explicitly setting out to build the same archive starting today, would compress the differentiation window. We have a head start; we do not have a permanent exclusion.

**The model science is not original.** All the volatility, calibration, and Kelly-sizing techniques described in this document are public, named, and cited. What is original is the *integration* — combining institutional-grade quant infrastructure with a prediction-market exchange and a one-person agentic-engineering operating model. Originality of components is not the bet; originality of the system is.

---

# 7. Closing

The one-sentence version: this is a quantitatively sophisticated, currently-profitable, structurally disciplined trading operation on the only CFTC-regulated event-contract exchange in the U.S., which has accidentally also become an irreplicable data archive, operated by one person with AI-agent leverage that wasn't feasible a year ago and is becoming more feasible every quarter. The trading edge will compress under competitive pressure. The data corpus will appreciate without effort. The agentic-engineering operating model is itself a moat — and the moat is structural, not personal.

If the technical layers behind these claims matter to you, the companion technical whitepaper covers every system in depth — including the testing infrastructure, the equivalence-snapshot discipline, the silver/gold ETL roadmap, the cal_mlp v2/v3 retraining plan, the SPX re-promotion path, and the planned auto-research layer that runs the model-development loop without operator gating.

If you'd prefer a non-technical overview that you could hand to a friend or family member, there is also a layperson whitepaper.

— Gabriel Kagan
*Last updated: 2026-09-06T23:06:47Z*
