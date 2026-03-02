---
title: "Kalshi Crypto Trading Bot — Investor Whitepaper"
author: "Gabriel Kagan"
date: "February 2026"
---

# Executive Summary

This document describes an automated trading platform for **Kalshi**, the first CFTC-regulated prediction market exchange in the United States. The system trades short-duration cryptocurrency contracts — binary options that settle every 15 minutes — and is expanding into three additional market verticals: S&P 500 intraday, daily weather temperature, and live sports outcomes.

The platform monitors real-time data from multiple sources per vertical, estimates outcome probabilities using domain-specific models, and executes trades only when it identifies a clear edge over the market price. Every aspect of the strategy — from market selection to position sizing to execution — is designed around disciplined risk management and profit maximization.

**Live trading results (as of 2026-03-02T02:05:10Z):**

- 186 settled trades with a 89.2% win rate (166W / 20L)
- Live trading with real capital since February 22, 2026
- Fully automated, always-on operation with complete audit trail
- Three additional market verticals in shadow mode: S&P 500 intraday, weather temperature (5 cities), and live sports (26 leagues)
- Each vertical uses domain-specific models while sharing the common risk and execution infrastructure

---

# The Opportunity

## Why Prediction Markets?

Prediction markets allow participants to trade contracts on the outcomes of real-world events. Unlike traditional financial markets, where pricing depends on complex fundamental analysis, prediction market contracts resolve to a simple binary outcome: the event either happens or it doesn't. This clarity creates a well-defined mathematical edge for quantitative approaches.

## Why Kalshi?

Kalshi is the only CFTC-regulated prediction market exchange in the US, providing the legal and structural protections of a regulated financial venue. Its crypto contracts offer several attractive properties for systematic trading:

- **High frequency**: New contracts launch every 15 minutes, 24/7, across four major cryptocurrencies (BTC, ETH, SOL, XRP)
- **Short duration**: Each contract settles within 15 minutes, meaning capital is never locked up for long
- **Binary outcomes**: Contracts pay exactly $1 if the asset stays above a threshold, $0 otherwise — no partial outcomes or complex payoffs
- **Hundreds of opportunities daily**: The combination of four assets, multiple strike prices, and 15-minute windows creates a deep pool of tradeable markets every day

## The Systematic Edge

Retail traders on Kalshi typically check one price source and make intuitive judgments about whether a crypto asset will stay above a given level. This approach is slow, inconsistent, and unable to process the volume of markets available.

A systematic approach has structural advantages: it can process every market, every window, with consistent discipline — never skipping a promising trade due to fatigue, and never chasing a bad one due to emotion.

---

# Strategy Overview

The bot follows a disciplined five-step process for every 15-minute trading window:

## 1. Observe

The system maintains real-time connections to multiple data sources simultaneously:

- **Two spot exchanges** (Coinbase, Kraken) — providing live cryptocurrency prices with sub-second updates
- **Deribit** — the leading crypto derivatives exchange, providing implied volatility data that reflects the market's forward-looking risk expectations
- **Kalshi** — the prediction market itself, providing current contract prices, orderbook depth, and real-time fill notifications via WebSocket

This multi-source approach means the bot sees price movements developing across global markets before they are reflected in Kalshi contract prices.

## 2. Estimate

For every active market, the bot estimates the probability that the underlying asset will stay above the contract's threshold for the remainder of the 15-minute window. This estimate incorporates:

- Current spot price relative to the threshold
- EGARCH-conditioned volatility (how much the price is expected to move, accounting for clustering and leverage effects)
- Options-implied volatility (what the derivatives market expects)
- Cross-exchange price signals (whether other exchanges are leading a move)
- Market-price blending (60% model / 40% market blend to prevent overconfidence)

The probability model uses **Normal Inverse Gaussian (NIG) distributions** fitted specifically to each cryptocurrency's return characteristics. Unlike generic models, NIG captures both the heavy tails (large moves are more common than a bell curve predicts) and the asymmetry (upward and downward moves have different frequencies) unique to each asset.

## 3. Filter

Most markets are not worth trading. The system applies a rigorous multi-stage filter that rejects markets for any of the following reasons:

- The model's estimated probability is too low (the contract is unlikely to pay out)
- The Kalshi price is too high (not enough profit potential) or too low (too much uncertainty)
- The edge after fees is insufficient — must exceed a price-dependent minimum (0.7% at 87¢ up to 4.0% at 97¢+) after taker fees (evaluated at worst-case rates)
- The model and the market disagree by a suspicious margin (suggesting the model may be missing information)
- Statistical inputs appear unreliable (extreme z-scores indicating potential data issues)

The vast majority of markets are correctly identified as unprofitable and filtered out — the system is highly selective.

## 4. Size

For the small number of markets that pass all filters, the bot determines the appropriate position size using an edge-tiered framework. Position sizes are:

- **Proportional to conviction** — higher-edge trades receive larger allocations (up to 25% of bankroll at 4%+ edge), lower-edge trades receive minimal sizing (7% at 0.9% edge)
- **Automatically reduced during drawdowns** — if the account balance drops below 85% of starting value, sizes halve; below 75%, they quarter; below 65%, trading halts entirely
- **Hard-capped** — no single trade can exceed 25% of bankroll regardless of model confidence

## 5. Execute

The bot uses a fee-minimizing execution strategy with intelligent escalation:

- **Maker-first**: It initially places limit orders with `post_only` guarantees, earning 75% lower fees than aggressive orders
- **Three-tier rejection handling**: If a maker order is rejected (locked spread), the system tries a degraded maker (worse price), then escalates to an aggressive taker order — but only after re-verifying the trade is still profitable at the higher fee rate
- **Maker-only below 90 seconds**: No taker execution when less than 90 seconds remain before settlement. Data showed taker trades in this window cost -$85 in net losses. Maker orders can still fill passively.
- **Real-time fill detection**: Kalshi WebSocket provides instant fill notifications at zero API cost, with REST polling as a backup
- **Smart escalation**: If a limit order hasn't filled and sufficient time remains, the bot first tries to amend the order in-place (faster than canceling and re-placing), then falls back to immediate-or-cancel taker orders
- **Price re-validation**: Before every execution step, the bot re-checks current market conditions to confirm the trade still makes sense

---

# Edge Sources

The bot's expected profitability comes from four structural advantages:

## Speed and Breadth of Information

While a typical Kalshi trader might check the Bitcoin price on one website, this system simultaneously processes real-time data from multiple professional-grade feeds. It detects cross-exchange price movements — where a large buy on one exchange precedes a move on another — and incorporates these signals before the Kalshi market adjusts.

## Volatility Sophistication

The probability of a crypto asset staying above a given price depends critically on how much the price is expected to move. The bot uses academic-grade volatility estimation techniques (Realized Kernel estimators from Barndorff-Nielsen 2008, data-adaptive bandwidth selection, EGARCH conditional volatility, Mincer-Zarnowitz R²-weighted blending) that are standard in institutional finance but rare among retail prediction market participants. This produces more accurate probability estimates, especially during volatile periods.

## Distribution Fitting

Most quantitative models assume returns follow a simple bell curve (Gaussian) or a generic fat-tailed distribution. This system fits **Normal Inverse Gaussian distributions** to each cryptocurrency individually, capturing the specific tail behavior and asymmetry of BTC, ETH, SOL, and XRP. This produces measurably better probability estimates — statistical tests confirm NIG fits the actual data far better than generic alternatives.

## Fee Optimization

Kalshi charges different fees for different order types. The bot's maker-first execution strategy with three-tier escalation captures the 75% fee discount available to limit orders whenever possible, directly improving the profit margin on every trade. When forced to pay taker fees (locked spreads), the system re-verifies profitability before proceeding. This seemingly small advantage compounds significantly over hundreds of trades.

---

# Risk Management

Capital preservation is a core component of profit maximization. The system manages risk through position sizing, execution controls, and model safety checks.

## Conservative Position Sizing

The bot uses an **edge-tiered** approach — higher-conviction trades (greater model edge over the market) receive larger allocations, while lower-edge trades get minimal sizing:

| Fee-Adjusted Edge | Risk Fraction |
|---|---|
| ≥ 4% | 25% of bankroll |
| ≥ 2.5% | 20% of bankroll |
| ≥ 1.8% | 15% of bankroll |
| ≥ 1.2% | 10% of bankroll |
| ≥ 0.9% | 7% of bankroll |
| ≥ 0.7% | 5% of bankroll |

Individual trades risk a precisely calculated fraction of the bankroll, proportional to estimated edge. Even a string of losses has a limited impact on total capital. The sizing tiers were calibrated against actual trade performance data — previous higher tiers (50/35/20) were reduced after loss analysis.

## Automatic De-Risking

If the account balance drops below certain thresholds relative to its starting value, position sizes are automatically reduced:

- At **85% of starting balance**, sizes are cut in half
- At **75% of starting balance**, sizes are cut to one-quarter
- At **65% of starting balance**, trading halts entirely

This creates a geometric de-risking curve: the more the account loses, the less it risks, making recovery from drawdowns more manageable.

## Multi-Asset Trading

The bot can trade multiple assets per 15-minute window, concentrating capital on the best available opportunities while maintaining independent risk assessment for each position.

## Multiple Safety Checks

Before any trade is placed, the system verifies:

- The model's probability estimate passes a sanity check against the market price
- The estimated edge exceeds the price-dependent minimum (0.7%–4.0%) after accounting for all fees (at worst-case taker rates)
- The contract price falls within acceptable bounds (87–99¢)
- No extreme statistical indicators suggest unreliable model inputs
- The position size respects all hard limits and drawdown adjustments

If any single check fails, the trade is refused — no exceptions. The system is designed to say "no" far more often than "yes."

## Hard Price Boundaries

The bot only trades contracts priced between 87 and 99 cents. Below 87 cents, historical data shows poor win rates (two losses at 86¢ prompted the raise). Above 99 cents, the potential profit is too small to justify the risk. This guardrail eliminates an entire class of low-quality trades.

## Maker-Only Late Window

No taker (aggressive) orders are placed when less than 90 seconds remain before settlement. This data-driven threshold was introduced after analysis showed taker trades in the final 90 seconds produced -$85 in net losses. Maker (passive) orders can still fill during this period.

---

# Performance

## Live Trading Results

| Metric | Value |
|---|---|
| Status | Live trading since February 22, 2026 |
| Settled trades | 186 |
| Win rate | 89.2% (166W / 20L) |
| Assets | BTC, ETH, SOL, XRP |
| Entry prices | 87–99¢ |

## Market Expansion Pipeline

The platform is actively expanding beyond 15-minute crypto into four additional verticals. Each runs in shadow/observation mode — computing probabilities, logging signals, and tracking outcomes — to validate the model before enabling live trading with real capital.

### Crypto Hourly Markets

Hourly cryptocurrency markets (KXBTCD, KXETHD, KXSOLD, KXXRPD) with 75 strikes per event. The system evaluates every strike, computes probabilities, and tracks settlement outcomes. Currently collecting calibration data — analysis showed the 15-minute calibration model doesn't transfer well to hourly timescales, so a dedicated hourly calibration is being developed.

### S&P 500 Intraday Markets

15-minute binary contracts on the S&P 500 index during NYSE regular trading hours (9:30 AM–4:00 PM ET). The SPX engine uses the same EGARCH volatility framework as crypto, adapted for equity-specific dynamics:

- **Stronger leverage effect**: Down moves in equities increase volatility approximately 4× more than in crypto, requiring different EGARCH parameterization
- **VIX integration**: The CBOE Volatility Index provides a forward-looking volatility signal not available for crypto — the engine blends it with realized estimates when the two diverge significantly
- **Intraday seasonality**: SPX volatility follows a well-documented U-shaped pattern (high at open/close, low midday). The engine deseasonalizes returns to prevent systematic bias

This vertical leverages the same infrastructure (edge detection, position sizing, execution) while accessing a much larger and more liquid underlying market.

### Weather Temperature Markets

Daily high temperature markets across five major US cities (New York, Chicago, Miami, Denver, Los Angeles). This vertical is fundamentally different from financial markets — it uses **weather forecast ensembles** rather than price-based models:

- **82 independent forecasts**: 31 from NOAA's GFS model and 51 from ECMWF (the European weather model), each representing a plausible temperature scenario
- **Probabilistic framework**: The spread across 82 forecasts directly maps to outcome probability — if 60 of 82 models predict the temperature will exceed a threshold, that's roughly a 73% probability
- **Bias correction**: A per-city learning system tracks forecast errors over time and adjusts predictions accordingly

Weather markets are structurally attractive because they have longer settlement windows (daily), publicly available data, and probability estimates that are independent of financial market dynamics — providing natural portfolio diversification.

### Live Sports Outcomes

Game outcome markets across 26 leagues including NBA, NHL, MLB, NFL, EPL, and other major soccer leagues. The sports engine identifies a specific high-value pattern: **pregame favorites trailing in-game**.

When a team that was heavily favored before the game falls behind, the market often overreacts — pricing the favorite far below its historical comeback probability. The engine uses a Bayesian model calibrated on historical comeback data to identify when the market discount is excessive:

- **26 leagues monitored**: Both binary outcome (US sports, UFC) and three-way outcome (soccer with draw possibility)
- **Conservative entry**: Only signals when a strong pregame favorite (65%+ pre-game probability) is available at a significant discount (38¢ or below)
- **One signal per game**: Prevents correlated exposure from multiple entries in the same game
- **Safety cap**: Rejects signals where the model disagrees with the market by more than 30 percentage points — if the model thinks 90% but the market says 20%, the market is probably right

### Expansion Philosophy

Each new vertical follows the same disciplined pipeline:

1. **Build domain-specific model** — using the best available data source for each market type
2. **Shadow mode** — run alongside live trading, logging all signals without executing
3. **Calibration** — track model accuracy against actual outcomes over weeks/months
4. **Validation** — only promote to live trading when data confirms the model has genuine edge
5. **Conservative sizing** — new verticals start with lower risk limits (10–15% vs 25% for proven crypto)

This approach ensures each vertical is validated on real market data before real capital is deployed.

---

# Infrastructure and Reliability

## Always-On Operation

The bot runs on a dedicated cloud server (DigitalOcean, Ubuntu 24.04) managed by systemd, the standard Linux process manager. If the bot crashes for any reason, systemd automatically restarts it within seconds. The system has been designed for unattended 24/7 operation.

## Continuous Deployment

Code changes pushed to the main branch automatically deploy to the production server via GitHub Actions:

1. The CI pipeline SSHes into the server
2. Pulls the latest code
3. Runs a syntax check to catch errors before they reach production
4. Restarts the bot service

This pipeline ensures rapid iteration while maintaining a safety net against broken deployments.

## Complete Audit Trail

Every decision the bot makes is logged:

- **SQLite database** — Stores all positions, orders, fills, settlements, and every market evaluation with its filter stage outcome
- **JSONL journal files** — Append-only logs covering scans, opportunities, rejections, trades, settlements, orders, execution events, and maker order fill model training data
- **Firebase dashboard** — Real-time web interface showing current positions, market evaluations, volatility, orderbooks, execution engine health, calibration diagnostics, and EGARCH/NIG parameters

This comprehensive logging enables full after-the-fact analysis of any trade or decision.

## Crash Recovery

Order identifiers are written to the database before API submission. If the bot crashes mid-order, it can reconcile its state on restart without placing duplicate orders or losing track of open positions.

---

# Technical Appendix

For readers interested in the mathematical foundations, the full technical whitepaper provides detailed formulas and derivations. Brief summaries of the key models:

**Volatility Model** — Uses the Realized Kernel estimator (Barndorff-Nielsen 2008) with data-adaptive bandwidth selection to produce noise-robust volatility from high-frequency returns. Multiple estimators are blended using Mincer-Zarnowitz R²-weighted EMA blending, replacing fixed weights with data-driven quality scores. Includes EGARCH(1,1) with Student-t innovations for conditional volatility (promoted to live trading), time-varying RK weights, adaptive jump detection (percentile-based thresholds per asset), and options-implied volatility integration from Deribit.

**Probability Model** — Computes win probability using the Normal Inverse Gaussian (NIG) distribution with per-asset fitted parameters (a, b, μ, δ), capturing both heavy tails and asymmetry. NIG dramatically outperforms Student-t on statistical fit tests. Calibration is data-driven: as settlement outcomes accumulate, the CalibrationEngine progresses from fixed logistic scaling to Platt Scaling to Beta Calibration (currently active with 1,900+ observations). A dynamic time-dependent probability cap relaxes as expiry approaches (93% at 10min+ → 99.5% at <1min). Final probability blends 60/40 (60% model, 40% market) to prevent overconfidence.

**Position Sizing** — Edge-tiered sizing with drawdown-based scaling. Higher fee-adjusted edge trades get larger allocations (25% at 4%+, 20% at 2.5%+, 15% at 1.8%+, 10% at 1.2%+, 7% at 0.9%+, 5% at 0.7%+), with automatic de-risking during drawdowns (half at 85%, quarter at 75%, halt at 65%). Max risk per trade: 25%.

**Execution Model** — Maker-first with three-tier post_only rejection handler: normal maker → degraded maker (1¢ worse) → taker IOC (with edge re-verification). Maker orders use `post_only=True` to guarantee 75% fee savings. Fill detection via Kalshi WebSocket (zero API cost). Unfilled orders escalate via in-place amendment (`amend_order()`) before falling back to cancel + IOC (`time_in_force="immediate_or_cancel"`). Queue position monitoring every ~5s enables optimal escalation timing. No taker execution below 90 seconds to close.

**SPX Engine** — Adapts the crypto EGARCH framework for S&P 500 equities: stronger leverage effect bounds (4× crypto), VIX-implied volatility integration when realized and implied diverge >30%, intraday seasonal deseasonalization (13 half-hour buckets), and NYSE market hours guard with holiday calendar.

**Weather Engine** — Gaussian probability model over 82-member NWP ensemble (31 GFS + 51 ECMWF). Per-city EWMA bias correction with 7-day half-life. Supports bracket, threshold, and tail probability market types.

**Sports Engine** — Bayesian comeback model using empirically calibrated likelihood ratios keyed on (deficit bucket, time remaining, pregame strength). Conservative LR scaling (50% compression toward neutral). Model-market disagreement cap at 30pp. One signal per game dedup. 26 leagues with both binary and three-way (soccer draw) outcome types.

---

*Last updated: 2026-03-02T02:05:10Z*
