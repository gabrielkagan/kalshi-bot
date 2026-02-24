---
title: "Kalshi Crypto Trading Bot — Investor Whitepaper"
author: "Gabriel Kagan"
date: "February 2026"
---

# Executive Summary

This document describes an automated trading system for **Kalshi**, the first CFTC-regulated prediction market exchange in the United States. The bot trades short-duration cryptocurrency contracts — binary options that settle every 15 minutes — using a systematic, model-driven approach designed to capture small but frequent mispricings.

The system monitors real-time prices across seven data sources, estimates outcome probabilities using institutional-grade statistical models, and executes trades only when it identifies a clear edge over the market price. Every aspect of the strategy — from market selection to position sizing to execution — is designed around capital preservation and disciplined risk management.

**Key facts:**

- {{TOTAL_EVALUATED}} markets evaluated during the observation period ({{OBSERVATION_PERIOD}})
- {{TOTAL_SETTLED}} markets settled with verified outcomes
- {{WIN_RATE}} observed win rate across all settled positions
- Fully automated, always-on operation with complete audit trail

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

The system maintains real-time connections to seven data sources simultaneously:

- **Four spot exchanges** (Coinbase, Binance, Kraken, Bybit) — providing live cryptocurrency prices with sub-second updates
- **Deribit** — the leading crypto derivatives exchange, providing implied volatility data that reflects the market's forward-looking risk expectations
- **CoinGlass** — providing funding rate data from perpetual futures markets, indicating leverage buildup and crowded positioning
- **Kalshi** — the prediction market itself, providing current contract prices and orderbook depth

This multi-source approach means the bot sees price movements developing across global markets before they are reflected in Kalshi contract prices.

## 2. Estimate

For every active market, the bot estimates the probability that the underlying asset will stay above the contract's threshold for the remainder of the 15-minute window. This estimate incorporates:

- Current spot price relative to the threshold
- Recent realized volatility (how much the price has been moving)
- Options-implied volatility (what the derivatives market expects)
- Cross-exchange price signals (whether other exchanges are leading a move)
- Funding rate regime (whether leveraged positions suggest mean-reversion risk)

The probability model uses fat-tailed statistical distributions that better capture the reality of crypto markets, where sudden large moves occur more frequently than simple models predict.

## 3. Filter

Most markets are not worth trading. The system applies a rigorous multi-stage filter that rejects markets for any of the following reasons:

- The model's estimated probability is too low (the contract is unlikely to pay out)
- The Kalshi price is too high (not enough profit potential) or too low (too much uncertainty)
- The edge after fees is insufficient
- Another asset in the same window offers a better opportunity
- The model and the market disagree by a suspicious margin (suggesting the model may be missing information)

Of {{TOTAL_EVALUATED}} markets evaluated during the observation period, the vast majority are correctly identified as unprofitable and filtered out — the system is highly selective.

## 4. Size

For the small number of markets that pass all filters, the bot determines the appropriate position size using a conservative mathematical framework (a fractional Kelly criterion). Position sizes are:

- **Small by design** — never risking more than a carefully calculated fraction of the bankroll
- **Automatically reduced during drawdowns** — if the account balance drops, position sizes shrink proportionally
- **Hard-capped** — regardless of model confidence, no single trade can exceed the maximum position limit

## 5. Execute

The bot uses a fee-minimizing execution strategy:

- **Maker-first**: It initially places limit orders with `post_only` guarantees, earning 75% lower fees than aggressive orders
- **Real-time fill detection**: Kalshi WebSocket provides instant fill notifications at zero API cost, with REST polling as a backup
- **Smart escalation**: If the limit order hasn't filled, the bot first tries to amend the order in-place (faster than canceling and re-placing), then falls back to immediate-or-cancel taker orders
- **Price re-validation**: Before every execution step, the bot re-checks current market conditions to confirm the trade still makes sense

---

# Edge Sources

The bot's expected profitability comes from four structural advantages:

## Speed and Breadth of Information

While a typical Kalshi trader might check the Bitcoin price on one website, this system simultaneously processes real-time data from seven professional-grade feeds. It detects cross-exchange price movements — where a large buy on Binance precedes a move on Coinbase — and incorporates these signals before the Kalshi market adjusts.

## Volatility Sophistication

The probability of a crypto asset staying above a given price depends critically on how much the price is expected to move. The bot uses academic-grade volatility estimation techniques (Realized Kernel estimators from Barndorff-Nielsen 2008, HAR-RV multi-horizon blending) that are standard in institutional finance but rare among retail prediction market participants. This produces more accurate probability estimates, especially during volatile periods.

## Derivatives Intelligence

The bot integrates implied volatility from Deribit (the leading crypto options exchange) and funding rates from CoinGlass (perpetual futures data). These signals capture the broader crypto market's risk expectations and positioning, providing context that pure spot-price analysis misses entirely.

## Fee Optimization

Kalshi charges different fees for different order types. The bot's maker-first execution strategy captures the 75% fee discount available to limit orders whenever possible, directly improving the profit margin on every trade. This seemingly small advantage compounds significantly over hundreds of trades.

---

# Risk Management

Capital preservation is the system's primary objective. Every design decision prioritizes survival over aggression.

## Conservative Position Sizing

The bot uses a **quarter-Kelly** approach — it bets only 25% of the mathematically optimal amount for long-run growth. While this sacrifices some theoretical upside, it dramatically reduces the probability of large drawdowns. In practical terms, this means:

- Individual trades risk a small, precisely calculated fraction of the bankroll
- Even a string of losses has a limited impact on total capital
- The sizing formula is derived from decades of financial mathematics research

## Automatic De-Risking

If the account balance drops below certain thresholds relative to its starting value, position sizes are automatically reduced:

- At **90% of starting balance**, sizes are cut in half
- At **80% of starting balance**, sizes are cut to one-quarter

This creates a geometric de-risking curve: the more the account loses, the less it risks, making recovery from drawdowns more manageable.

## Diversification Rule

The bot trades only **one asset per 15-minute window** — whichever offers the highest edge. This prevents correlated exposure (e.g., being long both BTC and ETH when a broad crypto selloff hits) and concentrates capital on the best available opportunity.

## Multiple Safety Checks

Before any trade is placed, the system verifies:

- The model's probability estimate passes a sanity check against the market price
- The estimated edge exceeds the minimum threshold after accounting for all fees
- The contract price falls within acceptable bounds (not too cheap, not too expensive)
- No extreme statistical indicators suggest unreliable model inputs
- The position size respects all hard limits and drawdown adjustments

If any single check fails, the trade is refused — no exceptions. The system is designed to say "no" far more often than "yes."

## Hard Price Boundaries

The bot only trades contracts priced between 86 and 99 cents. Below 86 cents, historical data shows poor win rates and excessive uncertainty. Above 99 cents, the potential profit is too small to justify the risk. This guardrail eliminates an entire class of low-quality trades.

---

# Performance and Observations

*This section contains live statistics from the bot's observation database, updated on each whitepaper build.*

## Market Evaluation Summary

During the observation period ({{OBSERVATION_PERIOD}}), the bot evaluated **{{TOTAL_EVALUATED}}** individual markets across {{ASSETS_TRACKED}}. The vast majority were correctly identified as unprofitable and filtered out at various stages:

| Stage | Outcome | Count | Share |
|---|---|---|---|
| Initial screening | Probability too low to be interesting | {{FILTER_LOW_PROB}} | {{FILTER_LOW_PROB_PCT}} |
| Orderbook check | No orderbook available | {{FILTER_NO_OB}} | {{FILTER_NO_OB_PCT}} |
| Price discovery | No executable ask price | {{FILTER_NO_ASK}} | {{FILTER_NO_ASK_PCT}} |
| Price validation | Price outside acceptable range | {{FILTER_PRICE_OOR}} | {{FILTER_PRICE_OOR_PCT}} |
| Edge calculation | Edge too small after fees | {{FILTER_INSUFF_EDGE}} | {{FILTER_INSUFF_EDGE_PCT}} |
| Position sizing | Calculated size rounded to zero | {{FILTER_ZERO_SIZE}} | {{FILTER_ZERO_SIZE_PCT}} |
| Strategy rules | Another asset chosen for this window | {{FILTER_STRATEGY_WAIT}} | {{FILTER_STRATEGY_WAIT_PCT}} |
| **Passed all filters** | **Trade candidate** | **{{FILTER_CANDIDATE}}** | **{{FILTER_CANDIDATE_PCT}}** |

This extreme selectivity is by design — the system only trades when every condition aligns.

## Settled Outcomes

| Metric | Value |
|---|---|
| Total settled trades | {{TOTAL_SETTLED}} |
| Observed win rate | {{WIN_RATE}} |
| Observation P&L (cents) | {{OBSERVATION_PNL}} |

## Win Rate by Entry Price

| Entry Price | Trades | Wins | Win Rate |
|---|---|---|---|
| 80–84 cents | {{WR_80_N}} | {{WR_80_W}} | {{WR_80_R}} |
| 85–89 cents | {{WR_85_N}} | {{WR_85_W}} | {{WR_85_R}} |
| 90–94 cents | {{WR_90_N}} | {{WR_90_W}} | {{WR_90_R}} |
| 95–99 cents | {{WR_95_N}} | {{WR_95_W}} | {{WR_95_R}} |

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
- **Eight JSONL journal files** — Append-only logs covering scans, opportunities, rejections, trades, settlements, orders, execution events, and performance summaries
- **Firebase dashboard** — Real-time web interface showing current positions, market evaluations, and system status

This comprehensive logging enables full after-the-fact analysis of any trade or decision.

## Crash Recovery

Order identifiers are written to the database before API submission. If the bot crashes mid-order, it can reconcile its state on restart without placing duplicate orders or losing track of open positions.

---

# Technical Appendix

For readers interested in the mathematical foundations, the full technical whitepaper provides detailed formulas and derivations. Brief summaries of the key models:

**Volatility Model** — Uses the Realized Kernel estimator (Barndorff-Nielsen 2008) to produce noise-robust volatility from high-frequency returns, blended across 1/5/15-minute horizons using the HAR-RV framework. Includes jump detection (bipower variation) and options-implied volatility integration from Deribit.

**Probability Model** — Computes win probability using a Student-t distribution (4 degrees of freedom) applied to the z-score of price distance to threshold, with logistic calibration for model uncertainty and a dynamic time-dependent probability cap.

**Position Sizing** — Quarter-Kelly criterion with drawdown-based scaling. The Kelly fraction maximizes long-run geometric growth rate; using one-quarter of this fraction sacrifices approximately 6% of theoretical growth in exchange for dramatically reduced variance.

**Execution Model** — Maker-first limit orders (`post_only`) with WebSocket fill detection and smart escalation. Maker orders save 75% on fees. If unfilled, the system attempts in-place order amendment before falling back to immediate-or-cancel taker orders. Queue position monitoring enables optimal escalation timing.

---

*Generated: {{GENERATED_AT}}*
