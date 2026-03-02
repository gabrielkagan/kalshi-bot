---
title: "Kalshi Crypto Trading Bot — Investor Whitepaper"
author: "Gabriel Kagan"
date: "February 2026"
---

# Executive Summary

This document describes an automated trading system for **Kalshi**, the first CFTC-regulated prediction market exchange in the United States. The bot trades short-duration cryptocurrency contracts — binary options that settle every 15 minutes — using a systematic, model-driven approach designed to capture small but frequent mispricings.

The system monitors real-time prices across multiple exchanges, estimates outcome probabilities using EGARCH-conditioned volatility models with per-asset distribution fitting, and executes trades only when it identifies a clear edge over the market price. Every aspect of the strategy — from market selection to position sizing to execution — is designed around disciplined risk management and profit maximization.

**Live trading results (as of 2026-03-02T01:47:10Z):**

- 186 settled trades with a 89.2% win rate (166W / 20L)
- Live trading with real capital since February 22, 2026
- Fully automated, always-on operation with complete audit trail
- Also collecting calibration data on hourly markets (75 strikes/event) for future expansion

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

## Hourly Market Expansion

The bot is also monitoring hourly cryptocurrency markets (KXBTCD, KXETHD, KXSOLD, KXXRPD) in observation mode. These markets have 75 strikes per event and settle every hour. The system is collecting calibration data — evaluating every strike, computing probabilities, and tracking settlement outcomes — to validate the model before enabling live trading.

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

---

*Last updated: 2026-03-02T01:47:10Z*
