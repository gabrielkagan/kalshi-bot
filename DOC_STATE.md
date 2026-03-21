# DOC_STATE.md — Full Documentation State Extract
Generated: 2026-03-20

---

## PART A: Current Documentation (full content)

### A1: README

```markdown
# Kalshi Crypto Trading Bot

Automated trading platform for Kalshi prediction markets. Core engine trades 15-minute cryptocurrency contracts (BTC, ETH, SOL, XRP) using microstructure-aware volatility models. Expanding into S&P 500 intraday, daily weather temperature (19 US cities), and live sports outcomes (28 leagues) --- all in shadow mode collecting calibration data.

## How It Works

```
Coinbase (1s prices) ──┐
Kraken ────────────────┤                                          ┌─ Maker order (post_only)
Bybit ─────────────────┼──→ Volatility ──→ Probability ──→ Edge ──┤
Deribit DVOL ──────────┤     Engine          Engine      Filter   └─ Taker escalation (amend/IOC)
CoinGlass funding ─────┘
```

Every second, the bot scans all active 15-minute windows across all four assets and executes when the fee-adjusted edge exceeds a price-dependent minimum (0.25% at 86c up to 2.0% at 97c+).

## Architecture

### Volatility Engine

The bot doesn't use a single volatility number --- it blends three Realized Kernel estimators (Barndorff-Nielsen 2008, Parzen flat-top kernel) with data-adaptive bandwidth selection:

| Estimator | Base Weight | Purpose |
|-----------|-------------|---------|
| 1-min realized kernel | 50% | Current microstructure |
| 5-min bipower variation | 30% | Jump-robust medium-term vol |
| 15-min realized kernel | 20% | Window-level baseline |

On top of this:

- **Adaptive RK bandwidth (H\*)** --- bandwidth auto-tunes from the noise-to-signal ratio, producing tighter estimates in calm periods and wider smoothing during noisy periods
- **Mincer-Zarnowitz R2-weighted EGARCH blending** --- an EGARCH(1,1) model with Student-t innovations runs live, blending with RK vol weighted by MZ regression R2 (typically 0.42--0.61)
- **Deribit DVOL integration** --- when IV diverges from RV by >50%, the engine shifts toward implied vol using inverse-variance weighting. For SOL/XRP (no direct DVOL), it scales BTC DVOL by a rolling cross-asset beta (60-return lookback, clamped 0.5--3.0)
- **Adaptive jump detection** --- percentile-based per-asset thresholds (replaced fixed 3-sigma); EWMA variance tracking with tiered response scaling by severity

### Probability Model

Converts the volatility estimate into a settlement probability:

1. Compute z-score: distance from current price to strike, normalized by estimated vol
2. Map through per-asset Normal Inverse Gaussian (NIG) CDF --- captures both heavy tails and asymmetry unique to each crypto; falls back to Student-t(df=4) if NIG unavailable
3. Data-driven calibration via CalibrationEngine --- progresses from fixed logistic -> Platt Scaling -> Beta Calibration -> BLR as data accumulates
4. Dynamic probability cap: bypassed when learned calibration is active (uses 0.999 safety ceiling); cap schedule only applies during startup before training
5. Market-price blending: 60% model / 40% market-implied probability

Safety rails refuse to trade if: the model says >90% but the market is below 75c, or |z-score| > 25.

### Cross-Exchange Intelligence

Three WebSocket feeds (Kraken, Bybit, Binance) run concurrently via `CrossExchangeFeed` to detect directional signals before they show up on Kalshi. Binance is geo-blocked (HTTP 451) on the production VPS but the feed reconnects silently; Kraken and Bybit provide the primary cross-exchange signal.

- **Lead/lag consensus** --- if 3+ exchanges move >0.3% in the same direction, the probability gets a +2pp boost
- **Single-exchange lead** --- a >0.2% move on one exchange adds +1pp
- **Funding rate signal** --- extreme funding (>0.05%/8h via CoinGlass) reduces probability by up to 1.5pp as a contrarian dampener

Total cross-exchange adjustment is capped at +/-3pp.

### Execution Strategy

The bot always enters as a maker and escalates to taker based on time pressure. A diagnostic strategy engine classifies each opportunity (WAIT, MAKER_PATIENT, MAKER_AGGRESSIVE, TAKER_NOW) for logging, but the actual execution path is:

1. Place maker order with `post_only=True` (guarantees 75% cheaper maker fees)
2. Monitor for fills via Kalshi WebSocket (zero API cost, REST fallback)
3. Poll queue position every ~5s for escalation timing
4. If unfilled after wait period (15s/10s/5s depending on time remaining):
   - Attempt `amend_order()` to convert to taker price in-place
   - Fallback: cancel + IOC (`time_in_force="immediate_or_cancel"`) taker order
5. Three-tier post_only rejection handler: normal -> degraded -> taker IOC after 3+ rejections
6. Direct taker: when seconds-to-close < 180s, skip maker and submit IOC taker directly

### Position Sizing

Edge-tiered sizing with drawdown scaling:

| Fee-Adjusted Edge | Risk Fraction |
|-------------------|---------------|
| >= 4% | 25% of bankroll |
| >= 2.5% | 20% of bankroll |
| >= 1.8% | 15% of bankroll |
| >= 1.2% | 10% of bankroll |
| >= 0.9% | 7% of bankroll |
| >= 0.7% | 5% of bankroll |
| >= 0.5% | 3% of bankroll |
| >= 0.25% | 2% of bankroll |

- Safety ceiling: max 25% of bankroll at risk per trade
- At 85% of rolling 7-day peak balance: halve position sizes
- At 75% of rolling 7-day peak balance: quarter position sizes
- At 65% of rolling 7-day peak balance: halt trading entirely
- Can trade multiple assets per 15-minute window

### State & Persistence

SQLite (WAL mode) stores positions, pending orders, settled trades, GARCH parameters, and rejected/evaluated opportunities. On startup the bot reconciles local state against the Kalshi API --- API always wins.

## Data Sources

| Source | Transport | Data | Frequency |
|--------|-----------|------|-----------|
| Coinbase | WebSocket | BTC, ETH, SOL, XRP spot prices | 1s snapshots (300-sample buffer) |
| Kraken | WebSocket | Spot prices for lead/lag detection | Real-time |
| Bybit | WebSocket | Spot prices for lead/lag detection | Real-time |
| Binance | WebSocket | Spot prices (geo-blocked on VPS) | Real-time (when reachable) |
| Deribit | REST | DVOL implied volatility index | Every 60s (120s cache) |
| CoinGlass | REST | Funding rates | Every 10min (100 calls/day budget) |
| Kalshi | REST + WebSocket | Markets, orderbooks, positions, settlements, fills | 1s scan loop + real-time WS fills/orderbook |

## Live Stats

<!-- Auto-updated by GitHub Actions from VPS state.db -->

| Metric | Value |
|--------|-------|
| Markets evaluated | 12,165 |
| Observation period | 2026-02-22 to 2026-03-06 |
| Filter pass rate | 1.3% (158 of 12,165) |
| Top rejection reason | Price Out Of Range (6,201) |
| Settled trades | 258 |
| Win rate | 88.8% |
| Observation P&L | 10,686 cents |

*Last updated: 2026-03-06T23:11:09Z*

## Setup

### Prerequisites

- Python 3
- Kalshi API key + RSA private key (.pem)

### Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Dependencies: `requests`, `websockets`, `cryptography`, `scipy`, `numpy`

### Configure

```bash
cp .env.example .env
```

Required:
- `KALSHI_API_KEY` --- your Kalshi API key ID
- `KALSHI_PRIVATE_KEY_PATH` --- path to your RSA private key PEM file

Optional:
- `KALSHI_ENV=production` --- trade on live exchange (defaults to demo)
- `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` --- enable real-time dashboard (pushes state every 10s via Supabase)

### Run

```bash
source .env
python3 bot.py
```

The bot runs the full pipeline (price feeds, volatility, probability, edge detection) and places live orders. Set `OBSERVATION_MODE = True` in `bot.py` to run in observation-only mode (logs everything but places no orders).

## Deployment

Runs as a systemd service (`kalshi-bot`) on a DigitalOcean droplet. Pushing to `main` auto-deploys via GitHub Actions:

1. SSH into VPS as `botuser`
2. `git pull origin main`
3. Syntax-check `bot.py` (`python3 -c "import ast; ast.parse(..."`)
4. `sudo systemctl restart kalshi-bot`

## Kalshi API Notes

- **Auth**: RSA-PSS signature --- the signing path must include the `/trade-api/v2` prefix
- **Orderbook**: Returns separate YES and NO orderbooks. Market NBBO provides `yes_ask`, `yes_bid`, `no_ask`, `no_bid`. YES + NO prices do NOT always sum to 100.
- **Order type**: All orders are limit orders (no market orders as of Feb 2026)
- **Settlements**: Bot uses the settlements API for outcome detection, never z-score heuristics or balance deltas
- **Fee formula**: taker = `ceil(0.07 * C * P * (1-P))`, maker = `ceil(0.0175 * C * P * (1-P))` --- ceil on total, not per contract

## Project Structure

```
bot.py                         -- core bot logic (~10,400 lines, never rename)
analyst.py                     -- AI analyst (news sentiment, loss analysis, Telegram alerts)
market_config.py               -- centralized MarketTypeConfig (validates against bot.py at startup)
fifteenm_shadow.py             -- 15M shadow engine (recalibrated EGARCH + LightGBM research)
spx_engine.py                  -- S&P 500 intraday engine (EGARCH + VIX, shadow mode)
weather_engine.py              -- weather temperature engine (NWP ensemble, shadow mode)
sports_engine.py               -- sports comeback engine (Bayesian LR, shadow mode)
sports_data.py                 -- sports LR tables and league configuration
capital_allocator.py           -- capital allocation across product types
dashboard_snapshot.py          -- builds dashboard state snapshots for Supabase
supabase_sync.py               -- pushes snapshots to Supabase Realtime every 10s
watchdog.py                    -- process health monitoring
start.sh                       -- systemd entrypoint (venv + .env + bot.py)
requirements.txt               -- Python dependencies
.env.example                   -- credential template
.github/workflows/deploy.yml   -- auto-deploy on push to main
```

### Journals (gitignored)

The bot writes JSONL journals for every stage of its decision-making pipeline:

| Journal | Contents |
|---------|----------|
| `scan` | Every 1-second scan cycle with prices and vol estimates |
| `opportunity` | Evaluated opportunities with full model output |
| `trade` | Executed trades with price, count, cost, fee, z-score, edge, strategy |
| `order` | Order lifecycle (placed, filled, cancelled) |
| `rejection` | Opportunities that were filtered out, with reasons |
| `settlement` | Contract outcomes and P&L |
| `execution` | Execution quality metrics |
| `performance` | Daily summary aggregations |
| `fill_model` | Maker order lifecycle data for ML fill prediction |

### Dashboard (Supabase)

When `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` are set, `supabase_sync.py` pushes a state snapshot every 10 seconds to the `dashboard_state` table: balance, active positions, recent trades, win/loss record, current volatility readings, order flow signals, and session stats. The dashboard is a static HTML page hosted on GitHub Pages, reading from Supabase Realtime.
```

### A2: Whitepapers

#### whitepaper.md (Technical Whitepaper)

```markdown
---
title: "Kalshi Crypto Trading Bot — Technical Whitepaper"
author: "Gabriel Kagan"
date: "March 2026"
---

# Part 1: Executive Summary

## What It Does

This system is an automated trading platform for **Kalshi**, a CFTC-regulated prediction market exchange. It began with **15-minute cryptocurrency price threshold contracts** and has expanded to cover **five distinct market verticals**: crypto (15M + hourly), S&P 500 intraday, daily weather temperature, and live sports outcomes — each with domain-specific models running in shadow or observation mode alongside the live crypto engine.

The bot monitors real-time data from multiple sources per vertical, estimates outcome probabilities using domain-specific models (EGARCH volatility for crypto/SPX, NWP ensemble forecasts for weather, Bayesian comeback likelihood for sports), and trades when it identifies a statistical edge over the market price.

## Market Opportunity

Kalshi lists 15-minute crypto contracts around the clock. Each window produces fresh contracts for four assets at multiple strike prices, creating hundreds of tradeable markets per day. Because these are short-duration, binary-outcome instruments, mispricing tends to be small but frequent — an ideal environment for systematic, model-driven trading.

Beyond crypto, the platform monitors four additional verticals in shadow/observation mode: S&P 500 intraday markets (EGARCH + VIX integration), daily weather temperature markets across 19 US cities (82-member NWP ensemble), live sports outcomes across 28 leagues including tennis (Bayesian comeback model), and hourly crypto markets (collecting calibration data). Each vertical uses domain-specific models while sharing the common edge detection, sizing, and execution infrastructure.

## Strategy in Plain English

1. **Observe** — Continuously stream spot prices from Coinbase and Kraken. Fetch implied volatility from Deribit. Monitor Kalshi's own orderbook via WebSocket.
2. **Estimate** — For every active market, compute the probability that the asset stays above its threshold using EGARCH-conditioned volatility with fat-tailed NIG distributions fitted per asset.
3. **Filter** — Reject markets that are too uncertain, too expensive, or offer insufficient edge after fees.
4. **Size** — Use edge-tiered position sizing with automatic drawdown scaling.
5. **Execute** — Place maker (limit) orders first to minimize fees, with three-tier post_only rejection handling, time-aware taker escalation, and direct taker execution below 180 seconds.
6. **Settle** — Track outcomes via the Kalshi settlements API and log performance for continuous evaluation.

## Key Differentiators

| Differentiator | Description |
|---|---|
| **Multi-exchange intelligence** | Aggregates spot prices from Coinbase and Kraken plus derivatives signals from Deribit, detecting cross-exchange lead-lag patterns before they appear in Kalshi prices |
| **EGARCH-conditioned volatility** | Realized Kernel estimation (Barndorff-Nielsen 2008) with data-adaptive bandwidth, MZ R²-weighted blending, and EGARCH(1,1) conditional volatility — all promoted to live trading |
| **Per-asset NIG distributions** | Normal Inverse Gaussian CDF replaces the generic Student-t, capturing both heavy tails and asymmetry specific to each cryptocurrency |
| **Adaptive execution** | Three-tier post_only rejection handler, maker-first with time-aware escalation, and direct taker below 180s (data: 7.7% maker fill rate at low STC — direct taker strictly better) |
| **Data-driven risk controls** | Edge-tiered sizing (25% max), drawdown scaling, z-score sanity checks, learned calibration, and model-market discrepancy detection |
| **Multi-vertical expansion** | Five market verticals sharing common risk infrastructure, each with domain-specific probability models |

---

# Part 2: System Architecture

## Data Pipeline

```
  Coinbase WS ──┐
  Kraken WS ────┤──→ VolatilityEngine ──→ ProbabilityEngine ──→ OpportunityScanner
                │         ↑                      ↑                     │
                │         │                      │                     ▼
  Deribit API ──┘         │         CalibrationEngine           PositionSizer
                          │                                          │
                     EGARCHEstimator                                  ▼
                                                                OrderExecutor
  Kalshi API + WS ◄──────────────────────────────────────────────────┘
       │                                                          │
       ▼                                                          ▼
  SettlementTracker ──→ StateManager (SQLite) ◄──── Logger (JSONL journals)
                                                          │
                                                     AnalystEngine ──→ Telegram
```

## Component Overview

| Component | Role |
|---|---|
| **KalshiClient** | API communication with RSA-PSS authentication and per-second rate limiting (30 reads/sec, 30 writes/sec on Advanced tier) |
| **KalshiFeed** | WebSocket connection for real-time fills and orderbook delta streaming |
| **Logger** | Structured JSONL logging across multiple journals with fill deduplication |
| **StateManager** | SQLite-backed persistent state (WAL mode for crash resilience, `busy_timeout=10000`); tracks positions, orders, fills, settlements, and order lifecycle |
| **CoinbaseFeed** | Real-time WebSocket feed for BTC, ETH, SOL, XRP with 300-point price buffer (5 minutes at 1-second intervals); EGARCH uses a separate 10,800-point return buffer (15 hours at 5-second intervals) |
| **DeribitDVOLFetcher** | Daemon thread fetching implied volatility (DVOL) index for BTC and ETH every 60 seconds |
| **CrossExchangeFeed** | WebSocket feeds from Kraken, Bybit, and Binance for cross-exchange lead-lag detection (Binance geo-blocked on VPS) |
| **KalshiOrderFlowTracker** | Shadow-mode Kalshi-native orderbook imbalance, depth velocity, and spread convergence signals |
| **VolatilityEngine** | Realized Kernel volatility with adaptive bandwidth (H*), MZ R²-weighted blending, EGARCH(1,1) Student-t conditioning, time-varying RK weights, adaptive jump detection, and DVOL integration |
| **EGARCHEstimator** | EGARCH(1,1) with Student-t innovations (df 3.2–3.8), MLE-fitted on 10,800 samples (15 hours), refitted hourly — live (promoted from shadow) |
| **MZTracker** | Mincer-Zarnowitz R² regression for dynamic EGARCH blend weight estimation with EMA smoothing |
| **ProbabilityEngine** | Win probability via NIG CDF (per-asset fitted) with data-driven calibration, dynamic caps (bypassed when learned calibration active), and market-price blending |
| **CalibrationEngine** | Learns calibration from settlement outcomes: Fixed Logistic → Platt Scaling → Beta Calibration → Bayesian Linear Regression (auto-promotes as data accumulates) |
| **PositionSizer** | Edge-tiered position sizing (8 tiers) with drawdown-based scaling |
| **OpportunityScanner** | Multi-stage filter pipeline evaluating all markets across active 15-minute windows, hourly observation windows, SPX windows, weather markets, and sports markets |
| **OrderExecutor** | Three-tier post_only handler, maker-first limit orders with adaptive taker escalation, direct taker below 180s, WebSocket fill detection, amend-first conversion, and full order lifecycle tracking |
| **SettlementTracker** | Incremental settlement polling (30-second intervals) using the Kalshi settlements API |
| **SPXEngine** | S&P 500 intraday engine: Polygon/Finnhub price feeds, EGARCH with VIX integration, intraday seasonal deseasonalization, per-window position limits (shadow mode) |
| **WeatherEngine** | Weather temperature engine: Open-Meteo NWP ensemble (82 members per run), Gaussian probability model with per-city bias correction, 19 US cities (shadow mode) |
| **SportsEngine** | Sports comeback engine: ESPN live scores across 28 leagues (incl. ATP/WTA tennis), Bayesian LR model with conservative scaling, 30-second polling (shadow mode) |
| **AnalystEngine** | AI-powered trade analyst: Claude API for loss root-cause analysis and pattern detection, Telegram alerts for high-confidence findings |
| **TelegramNotifier** | Optional Telegram alerts for trades, settlements, and errors |
| **MainLoop** | Continuous 1-second observation loop coordinating all components |

## Data Sources

| Source | Data | Transport | Frequency |
|---|---|---|---|
| Coinbase | Spot prices (BTC, ETH, SOL, XRP) | WebSocket | Real-time (1s snapshots, 300-point buffer = 5min) |
| Kraken | Spot prices (cross-exchange) | WebSocket | Real-time |
| Deribit | Implied volatility (DVOL) for BTC/ETH | REST API | 60 seconds |
| Polygon.io | SPX spot price | REST API | 1s polling (NYSE RTH) |
| Finnhub | SPX/SPY fallback | REST API | 1s polling |
| CBOE | VIX implied volatility | REST API | 60s polling |
| Open-Meteo | NWP ensemble forecasts (GFS + ECMWF) | REST API | 15 minutes |
| ESPN | Live sports scores, clock, period | REST API | 30 seconds |
| Kalshi | Markets, orderbooks, balance, fills, settlements | REST API + WebSocket | On-demand + real-time |
| Claude API | AI analyst for loss analysis and pattern detection | REST API | Per-settlement |

---

# Part 3: Technical Deep-Dive

## 3.1 Volatility Engine

The volatility engine produces a per-asset, per-5-second realized volatility estimate that feeds the probability model. It combines multiple techniques with data-adaptive weighting and EGARCH forward-looking conditioning.

### Realized Kernel (Barndorff-Nielsen 2008)

Standard sample variance of high-frequency returns is biased by market microstructure noise (bid-ask bounce, discrete tick sizes). The Realized Kernel estimator corrects this using a kernel-weighted autocovariance function with a Parzen flat-top kernel, producing noise-robust volatility estimates from 5-second log returns.

**Adaptive bandwidth (H*)**: Rather than using a fixed bandwidth H=1, the system estimates the optimal bandwidth from the data using the noise-to-signal ratio:

$$H^* = c \times \left(\frac{\hat{\omega}^2}{\text{IV}}\right)^{2/5} \times n^{3/5}$$

where $\hat{\omega}^2$ is the estimated microstructure noise variance and IV is the integrated variance. This produces tighter estimates during calm periods and wider smoothing during noisy periods.

### Time-Varying RK Weights

Multiple RK estimators at different scales are blended using time-varying weights that adapt to current market conditions, rather than fixed proportions.

### Mincer-Zarnowitz R²-Weighted Blending

Rather than fixed weights, the system dynamically weights estimators based on their forecasting quality. A Mincer-Zarnowitz regression compares each estimator's forecast against realized outcomes:

$$RV_{t+1} = \alpha + \beta \times \hat{\sigma}_t + \varepsilon_t$$

The R² from this regression measures forecast quality. Weights are smoothed using an EMA (λ=0.97) to prevent whipsawing:

$$w_t = \lambda \times w_{t-1} + (1-\lambda) \times w_{raw}$$

Below an R² threshold of 0.10, the system reverts to equal-weight blending as a fallback.

### EGARCH(1,1) with Student-t Innovations

The EGARCH model captures volatility clustering and leverage effects:

$$\log(\sigma_t^2) = \omega + \alpha \left(|z_{t-1}| - E[|z|]\right) + \gamma z_{t-1} + \beta \log(\sigma_{t-1}^2)$$

Fitted with Student-t innovations (df typically 3.2–3.8 for crypto) via maximum likelihood on 10,800 samples (15 hours at 5-second intervals), refitted every 2 hours. The EGARCH forecast is blended with the RK estimate using MZ R²-weighted blending — the system dynamically determines how much weight to give the conditional model vs. the realized estimate based on forecast quality.

**Status: Live** — promoted from shadow mode after extensive validation. EGARCH core vol, EGARCH blend, and MZ R²-weighted blending all drive live trading decisions.

### Adaptive Jump Detection

Jumps — sudden, large price moves — invalidate smooth volatility assumptions. The system uses an adaptive threshold rather than the fixed 3σ approach:

- **EWMA variance tracking**: Tracks running variance of 15-second subsampled returns (λ=0.94)
- **Percentile-based threshold**: Jump trigger set at the asset's own volatility distribution percentile, adapting to current regime
- **Tiered response**: Jump multiplier and cooldown duration scale with jump severity
- **Health monitoring**: Logs EWMA σ, percentile, threshold, and total jump count per asset

### DVOL Integration

When Deribit implied volatility (DVOL) exceeds realized volatility by more than 50%, the system blends in the implied estimate using inverse-variance weighting. This respects the market's forward-looking information during regime changes while anchoring to observed data.

### Cross-Asset Beta

For assets without direct DVOL data (SOL, XRP), the system estimates a cross-asset beta against BTC using a 60-return lookback window, clamped to [0.5, 3.0], allowing derivative signals to propagate across correlated assets.

## 3.2 Probability Model

Given the blended volatility, the probability engine estimates the likelihood that an asset stays above its threshold for the remaining window duration.

### Step 1: Z-Score Computation

$$z = \frac{\text{threshold} - \text{spot}}{\text{spot} \times \sigma_{blended} \times \sqrt{T / 5}}$$

where $T$ is seconds remaining and $\sigma_{blended}$ is per-5-second scale. The denominator represents the expected magnitude of price movement over the remaining window.

### Step 2: NIG CDF (Per-Asset Fitted)

The model uses the Normal Inverse Gaussian distribution with per-asset fitted parameters:

$$p_{raw} = 1 - F_{NIG}(z; a, b, \mu, \delta)$$

where $a$ controls tail heaviness, $b$ captures asymmetry (skew), $\mu$ is location, and $\delta$ is scale. NIG parameters are fitted via maximum likelihood on 7 days of 60-second returns (~10,000 samples per asset) and stored in `dist_config.json`.

**Why NIG over Student-t?** NIG provides two key improvements:
- **Asymmetry**: The $b$ parameter captures the empirical skew in crypto returns (e.g., BTC $b=-0.019$, slight left skew)
- **Better tail fit**: KS test p-values for NIG are dramatically higher (BTC: 0.11, ETH: 0.42) compared to Student-t (effectively 0), indicating NIG genuinely captures the return distribution

The system falls back to Student-t(df=4) if NIG parameters are unavailable.

### Step 3: Data-Driven Calibration

Raw probabilities are calibrated using a CalibrationEngine that learns from settlement outcomes. The engine automatically promotes to better methods as data accumulates:

```
Fixed Logistic (β=0.85) ──→ Platt Scaling ──→ Beta Calibration ──→ Bayesian LR
     0 samples              200+ samples        350+ samples         50+ samples
   (default fallback)     (2-param logistic)   (3-param, flexible)  (with uncertainty)
```

| Method | Min Samples | Description |
|---|---|---|
| Fixed logistic (β=0.85) | 0 | Default fallback — compresses extreme probabilities |
| Platt Scaling | 200 | 2-parameter logistic (A, B) fitted to outcomes |
| Beta Calibration | 350 | 3-parameter (a, b, c) — more flexible than Platt |
| Bayesian Linear Regression | 50 | Bayesian approach with uncertainty estimation |

Each promotion undergoes validation checks to prevent degradation. **15-minute data only**: Hourly settlement data is excluded from calibration training (was contaminating the 15M model at 35.5% of training data).

### Step 4: Dynamic Probability Cap

A time-dependent cap adjusts confidence based on time remaining:

| Time to Expiry | Cap |
|---|---|
| > 10 minutes | 93% |
| 5–10 minutes | 95% |
| 2–5 minutes | 97% |
| 1–2 minutes | 98.5% |
| < 1 minute | 99.5% |

> **Important**: When a learned calibration method is active (Platt, Beta, or BLR trained on settlement data), the dynamic cap schedule is **bypassed entirely**. A numerical safety ceiling of 99.9% is used instead.

### Step 5: Market-Price Blending

The calibrated probability is blended with the market-implied probability:

$$p_{final} = 0.60 \times p_{cal} + 0.40 \times p_{market}$$

### Sanity Checks

- **Z-score limit**: If $|z| > 25$, the market is refused
- **Model-market discrepancy**: If $p_{cal} > 90\%$ but market price $< 75$¢, the market is refused

## 3.3 Edge Detection

### Fee Formula

$$\text{taker fee} = \left\lceil 0.07 \times C \times P \times (1-P) \right\rceil \text{ cents}$$
$$\text{maker fee} = \$0 \text{ (Kalshi charges no fee on maker fills)}$$

SPX ("finance" category) uses half the crypto fee multiplier: 0.035 taker.

### Fee-Adjusted Edge

$$\text{edge} = p_{final} - \frac{\text{best\_ask}}{100} - \frac{\text{taker\_fee}}{C \times 100}$$

### Price-Dependent Minimum Edge

| Entry Price | Min Edge |
|---|---|
| 97¢+ | 2.0% |
| 95–96¢ | 1.25% |
| 93–94¢ | 0.9% |
| 91–92¢ | 0.35% |
| 89–90¢ | 0.25% |
| 80–88¢ | 0.25% |

## 3.4–3.10

[Sections 3.4 through 3.10 cover Kalshi Order Flow (shadow), SPX Engine (shadow), Weather Engine (shadow), Sports Engine (shadow), Execution Strategy, Position Sizing, and AI Analyst — all fully documented in the source whitepaper.md]

# Part 4: Risk Management

## Position Sizing Controls

- **Edge-tiered sizing**: 8 tiers from 25% at 4%+ edge down to 2% at 0.25% edge
- **Drawdown scaling**: Half at 85%, quarter at 75%, halt at 65%
- **Hard limits**: Max 25% bankroll per trade

## Market Selection Controls

- **Price range**: 80–99¢ (per-asset overrides: BTC 89c, ETH 80c, XRP 92c, SOL 80c)
- **Price-dependent edge threshold**: 0.25% at 80c up to 2.0% at 97c+
- **Scanner uses taker fees** (worst-case)

## Model Sanity Controls

- **Z-score limit**: |z| > 25 → refuse
- **Model-market discrepancy**: >90% model but <75¢ market → refuse
- **Dynamic probability cap**: bypassed when learned calibration active (99.9% ceiling)
- **Market-price blending**: 60/40

## Execution Controls

- Three-tier post_only handler
- Maker-first with post_only
- Direct taker below 180s
- WebSocket fill detection
- Amend-first escalation
- Price re-validation before taker
- UUID persistence for crash recovery

## Per-Window Correlation Controls

- Max 2 positions per window (hourly/SPX)
- Max 15% risk per window
- Quarter-Kelly for non-15M verticals

# Part 5: Performance

| Metric | Value |
|---|---|
| Status | Live trading since February 22, 2026 |
| Settled trades | {{TOTAL_SETTLED}} |
| Win rate | {{WIN_RATE}} |

# Part 6: Infrastructure

| Component | Detail |
|---|---|
| Host | DigitalOcean droplet (Ubuntu 24.04), 1 vCPU / 2GB RAM / 48GB disk |
| Runtime | Python 3, virtualenv |
| Process manager | systemd (`kalshi-bot` service) |
| Auto-deploy | Push to main → GitHub Action → SSH → pull → syntax-check → restart |

[Full shadow mode feature table, data persistence details, and journal rotation documented in source]

*Last updated: {{GENERATED_AT}}*
```

#### whitepaper_investor.md (Investor Whitepaper)

```markdown
---
title: "Kalshi Crypto Trading Bot"
subtitle: "Investor Whitepaper"
author: "Gabriel Kagan"
date: "March 2026"
titlepage: true
titlepage-color: "0D1B2A"
titlepage-text-color: "FFFFFF"
titlepage-rule-color: "E8A838"
titlepage-rule-height: 4
toc: true
toc-own-page: true
[... LaTeX header configuration ...]
---

# Executive Summary

This document describes an automated trading platform for **Kalshi**, the first CFTC-regulated prediction market exchange in the United States. The system trades short-duration cryptocurrency contracts — binary options that settle every 15 minutes — and is expanding into four additional market verticals: S&P 500 intraday, daily weather temperature, hourly crypto, and live sports outcomes.

[Full investor whitepaper content — covers: The Opportunity, Strategy Overview (5-step process), Edge Sources (speed, volatility sophistication, distribution fitting, fee optimization), Risk Management (conservative sizing, automatic de-risking, multiple safety checks, hard price boundaries), Performance, Market Expansion Pipeline (hourly, SPX, weather, sports), Infrastructure and Reliability, Technical Appendix]

*Last updated: {{GENERATED_AT}}*
```

#### Other doc files found:

- `whitepaper_rendered.md` — rendered version with stats filled in
- `whitepaper_investor_rendered.md` — rendered version with stats filled in
- `whitepaper.pdf` — compiled PDF
- `whitepaper_investor.pdf` — compiled PDF
- `scripts/build_whitepaper.py` — PDF generation script
- `scripts/generate_whitepaper_stats.py` — stats extraction from VPS DB
- `templates/technical_template.latex` — LaTeX template for PDF rendering

### A3: CLAUDE.md

The full CLAUDE.md contents are included in the system context above. It is 368 lines covering:
- Interaction rules (answer first, don't re-plan, don't deploy without confirmation)
- Common workflows (investigate loss, performance analysis, add shadow strategy, deploy)
- Skill routing guide (18 skills across operations, performance, deep research, specialized)
- Critical rules (17 rules with learned-from-incidents annotations)
- Anti-patterns (7 things never to do)
- Project structure with bot.py line ranges
- Current state (Mar 8, 2026 snapshot — some values now stale)
- Key config values table (50+ configs)
- Calibration pipeline (6-step)
- Hourly three-layer optimization
- Shadow mode features table
- Order execution details
- Tech stack
- Kalshi API notes
- Fee formula
- Data storage
- Full DB schema reference (settled_trades, evaluated_opportunities, rejected_opportunities, other tables)

---

## PART B: Current Codebase Facts

### B1: Project Structure

```
.
├── .claude/                    # Claude Code config
│   ├── skills/                 # 18+ skill definitions
│   └── settings.json
├── .github/
│   └── workflows/
│       └── deploy.yml          # Auto-deploy on push to main
├── analysis/
│   └── regime_analysis.py
├── research/
│   ├── hourly_alt_shadow_implementation.md
│   ├── hourly_calibration_brief.md
│   └── hourly_eth_sol_xrp_brief.md
├── scripts/
│   ├── 15m_alpha_research.py
│   ├── 15m_live_audit.py
│   ├── alpha_audit.py
│   ├── audit_alerts.py
│   ├── audit_cron.py
│   ├── audit_runner.sh
│   ├── build_lr_tables.py
│   ├── build_whitepaper.py
│   ├── calibrate_dist.py
│   ├── check_docs_freshness.py
│   ├── data_health_monitor.py
│   ├── extract_config.py
│   ├── generate_docs.py
│   ├── generate_whitepaper_stats.py
│   ├── hourly_alpha_research.py
│   ├── hourly_shadow_audit.py
│   ├── maker_opportunity_cost.py
│   ├── no_side_status.py
│   ├── pre_deploy_check.sh
│   ├── quiet_market_monitor.py
│   ├── setup_audit_cron.sh
│   ├── setup_full_audit_timer.sh
│   ├── shadow_eval.py
│   ├── sports_alpha_research.py
│   ├── sports_analysis.py
│   ├── sports_shadow_audit.py
│   ├── spx_alpha_research.py
│   ├── spx_shadow_audit.py
│   ├── vps_mcp_server.py
│   ├── VPS_SETUP.md
│   ├── weather_alpha_research.py
│   ├── weather_shadow_audit.py
│   └── weekend_discount_audit.py
├── templates/
│   └── technical_template.latex
├── tests/
│   ├── test_calibration_engine.py
│   ├── test_call_sites.py
│   ├── test_config_consistency.py
│   ├── test_contracts.py
│   ├── test_db_signatures.py
│   ├── test_decided_contract.py
│   ├── test_execution.py
│   ├── test_fee_calc.py
│   ├── test_invariants.py
│   ├── test_probability_engine.py
│   ├── test_regression.py
│   ├── test_scan_pipeline.py
│   ├── test_spx_price_feed.py
│   ├── test_vol_engine.py
│   └── test_weather_no_side.py
├── bot.py                      # Main bot (~13,600 lines)
├── analyst.py                  # AI analyst (Claude API)
├── auditor.py                  # Hourly health checks via cron
├── capital_allocator.py        # Cross-product capital allocation
├── config.py                   # Shared constants
├── conftest.py                 # Pytest fixtures
├── dashboard_snapshot.py       # Dashboard state builder
├── fifteenm_shadow.py          # 15M shadow engine (A1-A4)
├── hourly_alt_shadow.py        # Hourly alt strategies
├── market_config.py            # MarketTypeConfig registry
├── models.py                   # Pure-math model classes
├── researcher.py               # 3x daily Telegram reports
├── sports_data.py              # Sports LR tables
├── sports_engine.py            # Sports comeback engine
├── spx_engine.py               # SPX hourly engine
├── spx_harrv_shadow.py         # SPX HAR-RV shadow
├── supabase_sync.py            # Supabase syncer thread
├── watchdog.py                 # Process health monitoring
├── weather_engine.py           # Weather ensemble engine
├── start.sh                    # systemd entrypoint
├── CLAUDE.md
├── README.md
├── POSTMORTEMS.md
├── TESTING_STRATEGY.md
├── whitepaper.md
├── whitepaper_investor.md
└── requirements.txt
```

**Total Python LOC:** 1,261,023

**Total tests:** 691 tests collected

### B2: Live Trading Config

#### Global 15M Config (from bot.py lines 1-600)

| Config | Value | Source |
|--------|-------|--------|
| OBSERVATION_MODE | False | LIVE TRADING |
| MIN_ENTRY_PRICE | 80 (global floor) | bot.py:46 |
| BTC_MIN_ENTRY_PRICE | 89 | bot.py:48 |
| ETH_MIN_ENTRY_PRICE | 80 | bot.py:49 |
| XRP_MIN_ENTRY_PRICE | 92 | bot.py:50 |
| SOL MIN_ENTRY_PRICE | 80 (uses global) | bot.py:46 |
| MAX_ENTRY_PRICE | 99 | bot.py:47 |
| XRP_MAX_RISK_PER_TRADE | 0.12 (12%) | bot.py:51 |
| XRP_15M_SHADOW | False (promoted to live) | bot.py:52 |
| MAX_SECONDS_BEFORE_CLOSE | 900 (15 min) | bot.py:55 |
| STC_SHADOW_THRESHOLD | 600 (600-900s shadow-only) | bot.py:56 |
| Temperature scaling | 1.0 (none for 15M) | config.py BETA_SLOPE=0.85 |
| MARKET_BLEND_W | 0.40 (60% model, 40% market) | bot.py:359 |
| ENDGAME_BLEND_PRICE | 96 (no blend at 96c+) | bot.py:360 |
| MAX_RISK_PER_TRADE | 0.25 (25%) | config.py:147 |
| Kelly fraction | 1.0 (full Kelly for 15M) | market_config.py:77 |

#### Per-Asset Overrides

| Asset | Min Price | Max Risk | Special |
|-------|-----------|----------|---------|
| BTC | 89c | 25% | 7s escalation wait (vs 15s default) |
| ETH | 80c | 25% | — |
| SOL | 80c | 25% | Taker-first (bypass maker entirely) |
| XRP | 92c | 12% | Promoted from shadow at 92c+ |

#### Edge Thresholds (MIN_EDGE_BY_PRICE)

| Price Range | Min Edge |
|-------------|----------|
| 97-99c | 2.0% |
| 95-96c | 1.25% |
| 93-94c | 0.9% |
| 91-92c | 0.35% |
| 89-90c | 0.25% |
| 80-88c | 0.25% |

#### Maker Bid Window & Taker Escalation

| Config | Value |
|--------|-------|
| MAKER_PRICE_OFFSET | 1c below fair value |
| MAKER_POLL_INTERVAL | 2.0s |
| MAKER_TIMEOUT_SECONDS | 30.0s |
| DIRECT_TAKER_THRESHOLD | 180s (below this → skip maker, go IOC) |
| MAKER_ONLY_THRESHOLD | 0.0 (taker allowed at all STC) |
| SOL_TAKER_FIRST | True (SOL bypasses maker entirely) |
| BTC_ESCALATION_WAIT_OVERRIDE | 7.0s |
| ESCALATION_WAIT_LONG | 15.0s (STC >= 180s) |
| ESCALATION_WAIT_MEDIUM | 7.0s (120-180s) |
| ESCALATION_WAIT_SHORT | 5.0s (60-120s) |
| EARLY_ESCALATION_MIN_MOVE | 5c (ask must move 5c+ to trigger early) |
| POST_ONLY_MAX_SAME_PRICE | 2 (tier 1→2 after 2 rejections) |
| POST_ONLY_DEGRADED_EXTRA_OFFSET | 1c |
| POST_ONLY_REJECTION_EXPIRY | 30.0s |
| ESCALATION_MAX_ENTRY | 99c |

#### Endgame / Addon Params

| Config | Value |
|--------|-------|
| ADDON_ENABLED | True |
| ADDON_MIN_PRICE_IMPROVEMENT | 3c |
| ADDON_MIN_SECONDS_SINCE_FILL | 10.0s |
| ADDON_MIN_STC_REMAINING | 45.0s |
| ADDON_SIZE_FRACTION | 0.50 |
| ADDON_MAX_PER_POSITION | 1 |
| ADDON_MAX_ENTRY_PRICE | 98c |
| DIP_ADDON_ENABLED | True |
| DIP_ADDON_SHADOW_MODE | True (log only) |
| DIP_ADDON_MIN_DROP_CENTS | 3c |
| DIP_ADDON_SIZE_FRACTION | 0.50 |
| DIP_ADDON_MAX_TOTAL_RISK | 0.35 |

#### Drawdown Tiers

| Balance vs Starting | Action |
|---------------------|--------|
| >= 85% | Full sizing |
| 75-85% | Half sizing |
| 65-75% | Quarter sizing |
| < 65% | Halt trading |

#### Low-STC Sizing Cap

| Config | Value |
|--------|-------|
| LOW_STC_SIZING_CAP | 0.50 (halve position) |
| LOW_STC_SIZING_CAP_THRESHOLD | 100s |

### B3: All Shadow Strategies

#### 1. STC Shadow (15M)
- **filter_stage**: `stc_shadow`, `stc_shadow_no_xrp`, `stc_shadow_xrp`
- **Assets**: BTC, ETH, SOL, XRP
- **Timeframe**: 15M, STC 600-900s
- **Key params**: STC_SHADOW_THRESHOLD=600, MAX_SECONDS_BEFORE_CLOSE=900
- **Promotion criteria**: Not explicitly defined
- **Status**: Actively logging

#### 2. Price Shadow (15M)
- **filter_stage**: `price_shadow`, `price_shadow_no_xrp`, `price_shadow_xrp`
- **Assets**: BTC, ETH, SOL, XRP
- **Timeframe**: 15M, price 70-85c
- **Key params**: PRICE_SHADOW_FLOOR=70
- **Status**: Actively logging

#### 3. Decided Contract Shadow (15M)
- **filter_stage**: `decided_contract_t1`, `decided_contract_t2`
- **Assets**: BTC, ETH, SOL, XRP
- **Timeframe**: 15M, STC ≤ 300s
- **Key params**: T1: z ≤ -5, any price 93+c. T2: z ≤ -3, price 93-96c
- **Promotion**: T1 and T2 both LIVE as incremental overlay
- **Status**: LIVE (DECIDED_T1_ENABLED=True, DECIDED_T2_ENABLED=True)

#### 4. Relaxed Edge Shadow (15M)
- **filter_stage**: `relaxed_edge_shadow`
- **Key params**: 50% of normal edge threshold, price 88-93c
- **Status**: Actively logging

#### 5. Weekend Edge Discount Shadow
- **filter_stage**: `weekend_discount_shadow`
- **Key params**: WEEKEND_EDGE_DISCOUNT=0.60 (60% of normal thresholds on Sat/Sun)
- **Promotion criteria**: 4-6 weekends, WR≥85%, no asset below 75%
- **Status**: Actively logging

#### 6. Overnight Edge Discount Shadow
- **filter_stage**: `overnight_discount_shadow`
- **Key params**: OVERNIGHT_EDGE_DISCOUNT=0.60, hours 04-11 UTC
- **Status**: Actively logging

#### 7. Overnight Low-Price Shadow
- **filter_stage**: `overnight_lp_shadow`
- **Key params**: Price 50-85c, cal_prob≥0.82, edge≥10%, STC 120-600s, hours 00-12 UTC
- **Promotion criteria**: ≥80 settled, ≥85% WR, positive PnL, ≥10 sessions
- **Status**: Actively logging

#### 8. XRP Shadow (15M)
- **filter_stage**: `xrp_shadow`
- **Note**: XRP_15M_SHADOW=False — XRP promoted to live at 92c+. Shadow remains for 88-91c range.

#### 9. Kalshi Order Flow (OFT)
- **Constant**: KALSHI_OFT_SHADOW_MODE=True
- **Signals**: Imbalance, depth velocity, spread convergence
- **Status**: Logging only, does not affect prob_adjustment

#### 10. Sigmoid QLIKE Mapping
- **Constant**: MZ_SIGMOID_SHADOW_MODE=True
- **Purpose**: Alternative EGARCH blend weight via QLIKE improvement ratio
- **Status**: Logging only

#### 11. Shadow Cal Pipeline (no-blend)
- **Constant**: SHADOW_CAL_PIPELINE=True
- **Purpose**: No-blend calibration monitoring (reverted: +1.86pp overconfident)
- **Status**: Monitoring in shadow

#### 12. 15M Shadow Engine (fifteenm_shadow.py) — A1/A2/A3/A4
- **A1 RecalibratedEGARCH**: Per-asset T + blend + edge bands. T: BTC=1.15, ETH=1.25, SOL=1.05, XRP=1.30
- **A2 LightGBM**: Binary classifier, needs 200 settled rows, retrained daily
- **A3 EGARCH Gating**: Predicts loss probability, needs 100 rows
- **A4 LateWindow**: 55-74c asks in final 5 min, taker-only
- **SHADOW_MIN_ENTRY_PRICE**: 70 (lower than live 80-92c)
- **Status**: All actively logging to fifteenm_shadow_signals

#### 13. Hourly Observation
- **filter_stage**: `hourly_observation`
- **Config**: HOURLY_OBSERVATION_ONLY=True, T=1.45, blend=0.40, price 50-99c
- **Shadow configs C-M**: 11 shadow variants exploring different T, blend, asset, STC combinations
- **Config A**: Excluded XRP, max edge 0.7%
- **Config B**: BTC only, 70-89c
- **Status**: Actively logging

#### 14. SPX Hourly Observation
- **filter_stage**: `spx_hourly_observation`
- **Config**: SPX_HOURLY_OBSERVATION_ONLY=True (reverted from live — Polygon 403)
- **Was briefly live Mar 17**: SPX-D CalEngine, 90c+ floor, eighth-Kelly
- **Shadow calibration variants**: T/blend exploration
- **Status**: Observation only

#### 15. Weather Observation
- **filter_stage**: `weather_observation`
- **Config**: WEATHER_OBSERVATION_ONLY=True, 19 cities, NWP ensemble, blend 0.20
- **Shadow variants**: capped30, short_stc, capped30_short_stc
- **Weather NO-side**: WEATHER_NO_SIDE_LIVE=False (kill switch off)
- **Status**: Observation only

#### 16. Sports Observation
- **filter_stage**: sports shadow signals in sports_shadow_log
- **Config**: SPORTS_OBSERVATION_ONLY=True, 28 leagues, Bayesian LR
- **Variants**: NBA-only, strong-config filter, tennis exclusion, Platt calibration
- **Status**: Observation only, hardcoded never-live

#### 17. Hourly Alt Shadow (hourly_alt_shadow.py)
- **Strategies**: HAR-RV, market-making simulation
- **Status**: Actively logging to hourly_alt_shadow_signals

#### 18. SPX HAR-RV Shadow (spx_harrv_shadow.py)
- **Purpose**: HAR-RV comparison for SPX
- **Status**: Actively logging

#### 19. Dip Addon Shadow
- **Constant**: DIP_ADDON_SHADOW_MODE=True
- **Purpose**: Buy more when ask drops 3c+ below entry after fill
- **Status**: Logging only

### B4: Model Architecture

#### EGARCH(1,1) with Student-t Innovations

**Specification:**
$$\log(\sigma_t^2) = \omega + \alpha(|z_{t-1}| - E[|z|]) + \gamma z_{t-1} + \beta \log(\sigma_{t-1}^2)$$

**Innovation distribution:** Student-t with estimated df (bounds 3.0-30.0, default 5.0)

**Refit frequency:**
- BTC, ETH: every 7200s (2 hours)
- SOL, XRP: every 3600s (1 hour) — faster regime changes

**Parameter bounds:**
| Param | Bounds |
|-------|--------|
| ω (omega) | (-5.0, 0.0) |
| α (alpha) | (0.01, 0.5) |
| γ (gamma) | (-0.3, 0.3) — per-asset overrides below |
| β (beta) | (0.80, 0.999) |
| df | (3.0, 30.0) |

**Per-asset gamma constraints:**
- BTC: (0.0, 0.0) — gamma insignificant, fixed at zero
- ETH: (-0.3, 0.3) — significant
- SOL: (-0.3, 0.3) — significant
- XRP: (0.0, 0.0) — gamma insignificant, fixed at zero

**EGARCH blend weight bounds (from MZ R²):**
- BTC: (0.15, 0.45) — high persistence
- ETH: (0.10, 0.35)
- SOL: (0.05, 0.20) — low persistence
- XRP: (0.05, 0.25)

**Buffer:** 10,800 returns (15 hours at 5-second intervals)
**MLE:** L-BFGS-B optimizer, max 200 iterations, exponential weighting λ=0.99984

#### Realized Kernel

**Windows:**
- 1-min RK: 12 returns (60s / 5s)
- 5-min bipower: 60 returns (300s / 5s)
- 15-min RK: 180 returns (900s / 5s)

**Default blend weights:** (0.50, 0.30, 0.20) for (1min, 5min, 15min)

**Time-varying weights (promoted):**
| STC | w1 (1min) | w5 (5min) | w15 (15min) |
|-----|-----------|-----------|-------------|
| ≤30s | 0.80 | 0.15 | 0.05 |
| 60s | 0.65 | 0.20 | 0.15 |
| 120s | 0.50 | 0.25 | 0.20 |
| 240s+ | 0.35 | 0.30 | 0.35 |

**Adaptive bandwidth:** H* = c*(ω²/IV)^(2/5) * n^(3/5), c*=3.5134 (Parzen flat-top)

#### NIG Distribution (per-asset fitted parameters from dist_config.json)

| Asset | a | b | loc | scale | KS p-value |
|-------|---|---|-----|-------|------------|
| BTC | 0.3198 | -0.0191 | 0.0331 | 0.5539 | 0.112 |
| ETH | 0.3057 | -0.0074 | 0.0131 | 0.5420 | 0.424 |
| SOL | 0.4443 | -0.0085 | 0.0126 | 0.6587 | ~0 |
| XRP | 0.4759 | 0.0051 | -0.0073 | 0.6790 | 8.3e-6 |

Fitted on 10,061 60-second returns (7 days). Falls back to Student-t(df=4) if NIG unavailable.

#### LightGBM (15M Shadow A2)

- **Features**: blended_rv, z_score, market_price, STC, spread, etc. (from evaluated_opportunities)
- **Training**: Binary classifier on settled evaluated_opportunities
- **Min training rows**: 200 (LGBM_MIN_TRAINING_ROWS)
- **Retrain interval**: daily (86400s)
- **Calibration**: Isotonic regression post-hoc
- **Status**: Shadow only (A2 in fifteenm_shadow.py)

#### Calibration

**15M CalibrationEngine:**
- State file: `calibration_state.json`
- Progression: Fixed Logistic (β=0.85) → Platt (200 samples) → Beta (350) → BLR (50)
- Retrain interval: 3600s
- Brier window: 500 outcomes
- 15M data only (hourly excluded)

**Hourly CalibrationEngine:**
- HOURLY_CALIBRATION_ENABLED = False (disabled: +44pp overconfident)
- Temperature: T=1.45 (softens: 95%→88.4%)
- State file: `hourly_calibration_state.json`

**SPX CalEngine:**
- SPX-D: post_temp variant, blend_w=0.0 (no market blend)
- Was briefly live, reverted

**Weather CalEngines:**
- Per-city engines (19 cities)
- WEATHER_CAL_ENGINE_ENABLED = True (learning in shadow)

**Sports CalEngines:**
- Per-sport-group engines (basketball, hockey, tennis, soccer, baseball)

### B5: Infrastructure

#### VPS Specs
- **Provider**: DigitalOcean
- **Droplet**: 1 vCPU / 2GB RAM / 48GB disk (resized from 1GB/25GB on Mar 3 2026)
- **Droplet ID**: 551718648, region: nyc3
- **OS**: Ubuntu 24.04
- **IP**: 45.55.181.30
- **User**: botuser

#### systemd Service
- **Service name**: `kalshi-bot`
- **Entrypoint**: `start.sh` → activates venv → sources .env → runs `bot.py`
- **Auto-restart**: systemd restarts on crash
- **Bot also exits on 6 consecutive tick errors** for systemd recovery (commit 76ab6ed)

#### CI/CD Pipeline (deploy.yml)
```yaml
name: Deploy to VPS
on:
  push:
    branches: [main]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Deploy to VPS
        uses: appleboy/ssh-action@master
        with:
          host: ${{ secrets.VPS_HOST }}
          username: botuser
          key: ${{ secrets.VPS_SSH_KEY }}
          script: |
            cd ~/kalshi-bot-repo
            git pull origin main
            python3 -c "import ast; ast.parse(open('bot.py').read()); print('SYNTAX OK')" 2>/dev/null && \
            sudo systemctl restart kalshi-bot || echo "No bot.py yet, skipping restart"
```

#### SQLite Config
- **Journal mode**: WAL (Write-Ahead Logging)
- **busy_timeout**: 10000ms (bot.py), 30000ms (supabase_sync.py), 5000ms (analyst.py), 10000ms (sports_engine.py)
- **check_same_thread**: False for cross-thread files (fifteenm_shadow.py, supabase_sync.py)
- **Checkpoint**: PASSIVE only (never TRUNCATE — learned from 11,258 errors)
- **Batch size**: ≤50 rows per commit (learned from 228-row deadlock)

#### Supabase
- **Project**: srbdajecmkjxinmcozxl.supabase.co
- **Table**: `dashboard_state` (JSONB, UPSERT)
- **Sync interval**: 30s (dashboard), 30s (incremental), 900s (snapshots)
- **Storage guardrails**: 400MB warning, 450MB stop
- **Auth**: Service key in VPS .env (`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`)

#### Dashboard
- **Host**: GitHub Pages (gabekagan repo, gh-pages branch)
- **Source**: `/private/tmp/gabekagan-dashboard/dashboard/index.html`
- **Data**: Reads from Supabase Realtime `dashboard_state` table
- **No Firebase** (removed Mar 6 2026)

#### Telegram Alert Config
- **Bot**: TelegramNotifier class in bot.py (lines 1226-1261)
- **Alerts**: trades, settlements, errors, high-confidence analyst findings
- **Env vars**: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

#### auditor.py
- **Runs**: Hourly via cron
- **Checks**: Performance, schema drift, data freshness, execution health
- **Expected column counts**: settled_trades=22, evaluated_opportunities=79, rejected_opportunities=28, positions=23, pending_orders=12
- **Alert dedup**: 24-hour window (DEDUP_HOURS=24)
- **Max messages per run**: 5 (overflow batched into summary)
- **State DB**: auditor_state.db (gitignored)

#### researcher.py
- **Runs**: Via cron every 30 minutes, auto-detects report time in ET
- **Schedule**: 7:30am (morning briefing), 12:30pm (midday update), 7:30pm (evening wrap)
- **Tolerance**: ±5 minutes from target time
- **Reports**: Compiled from state.db and auditor findings, sent to Telegram
- **State DB**: researcher_state.db (gitignored)

#### shadow_eval.py (scripts/)
- **Purpose**: Evaluates all shadow strategies, recommends promotion decisions
- **Metrics**: Win rate, PnL, Wilson CI, statistical confidence
- **Known shadow stages**: decided_contract_t1/t2, relaxed_edge_shadow, weekend/overnight_discount_shadow, overnight_lp_shadow, stc_shadow, xrp_shadow, price_shadow (+ _no_xrp, _xrp variants)
- **Usage**: `python3 scripts/shadow_eval.py --db state.db --days 14`

#### Journal Rotation
- **Schedule**: Daily 4AM UTC cron
- **Pattern**: copytruncate (bot uses open/close per write)
- **Archives**: `journal_archives/`, gzip compressed, 30-day retention

### B6: Fee Structure

**From code (models.py lines 65-89):**

```python
def calculate_fee(count, price_cents, is_taker, fee_mult_taker=0.07, fee_mult_maker=0.0):
    if not is_taker:
        return 0  # Maker fee = $0
    return math.ceil(fee_mult_taker * count * price_cents * (100 - price_cents) / 100)
```

| Type | Formula | Multiplier |
|------|---------|------------|
| **Taker (crypto)** | `ceil(0.07 × C × P × (100-P) / 100)` | 0.07 |
| **Maker (all)** | $0 | 0.0 |
| **Taker (SPX finance)** | `ceil(0.035 × C × P × (100-P) / 100)` | 0.035 (half crypto) |

**How fees enter edge calculation (bot.py):**
- Scanner evaluates edge using **taker fees** (worst-case): `edge = p_final - best_ask/100 - taker_fee/(C×100)`
- Any candidate that passes is profitable even if forced to taker execution
- Maker fills get $0 fees — pure profit improvement over the worst-case estimate
- SPX uses `SPX_HOURLY_FEE_MULTIPLIER_TAKER = 0.035`, `SPX_HOURLY_FEE_MULTIPLIER_MAKER = 0.0`

### B7: Risk Management

#### Layer 1: Position Sizing (8 tiers)

| Fee-Adjusted Edge | Risk Fraction |
|-------------------|---------------|
| ≥ 4.0% | 25% |
| ≥ 2.5% | 20% |
| ≥ 1.8% | 15% |
| ≥ 1.2% | 10% |
| ≥ 0.9% | 7% |
| ≥ 0.7% | 5% |
| ≥ 0.5% | 3% |
| ≥ 0.25% | 2% |

#### Layer 2: Hard Caps

| Cap | Value |
|-----|-------|
| MAX_RISK_PER_TRADE | 25% (global) |
| XRP_MAX_RISK_PER_TRADE | 12% |
| HOURLY_MAX_RISK_PER_TRADE | 15% |
| SPX_HOURLY_MAX_RISK_PER_TRADE | 10% |
| WEATHER_MAX_RISK_PER_TRADE | 10% |
| OVERNIGHT_LP_MAX_RISK_PER_TRADE | 10% |
| DECIDED_CONTRACT_RISK | 12.5% (fixed) |
| DECIDED_CONTRACT_MAX_WINDOW_RISK | 25% |
| SPX_HOURLY_BANKROLL_FRACTION | 15% (sizes off 15% of total balance) |

#### Layer 3: Drawdown Scaling

| Balance vs Starting | Sizing |
|---------------------|--------|
| ≥ 85% | Full |
| 75-85% | Half |
| 65-75% | Quarter |
| < 65% | HALT |

#### Layer 4: Low-STC Sizing Cap

- Below 100s STC: position halved (LOW_STC_SIZING_CAP=0.50)

#### Layer 5: Per-Window Correlation Controls (Hourly/SPX)

| Control | Hourly | SPX |
|---------|--------|-----|
| Max positions/window | 2 | 2 |
| Max risk/window | 15% | 15% |
| Kelly fraction | 0.25 (quarter) | 0.125 (eighth) |

#### Layer 6: Price Range Guards

| Product | Min Price | Max Price |
|---------|-----------|-----------|
| 15M (global) | 80c | 99c |
| 15M BTC | 89c | 99c |
| 15M ETH | 80c | 99c |
| 15M SOL | 80c | 99c |
| 15M XRP | 92c | 99c |
| Hourly | 50c | 99c |
| SPX | 90c | 99c |
| Weather | 10c | 99c |

#### Layer 7: Model Sanity

- Z-score limit: |z| > 25 → refuse
- Model-market discrepancy: >90% model but <75c market → refuse
- Dynamic probability cap: 93-99.5% (bypassed when learned cal active → 99.9%)

#### Layer 8: Overnight Vol Circuit Breaker

- OVERNIGHT_LP_VOL_SPIKE_MULT = 2.0 (skip if trailing vol > 2x overnight median)

#### Layer 9: Auto-restart

- Exit after 6 consecutive tick errors for systemd recovery

### B8: Recent Git History

```
8a93353 fix: raise ask_confirmed threshold 2c→5c — reduce adverse selection while collecting more data
b0289d3 feat: route decided contracts to direct taker — fix $151/2wk fill rate leak
d6af1f3 fix: raise SPX stale price threshold to 180s for Finnhub free tier
17f141c fix: thin cumulative P&L series to ~100 points to reduce Supabase egress
000fd0e fix: datetime.utcnow() → datetime.datetime.utcnow() in PlattCalibrator
6421f9b fix: add rollback() after failed DB writes to prevent stuck RESERVED locks
467be42 feat: Finnhub WebSocket feed for SPX — tick-level data for EGARCH
21739e9 revert: SPX back to observation-only — Polygon 403 breaks vol engine
a012f7b fix: prevent Finnhub low-freq returns from collapsing SPX EGARCH sigma
16f8163 diag: add probability result logging for SPX markets
39cd7cb diag: add detailed SPX eval path tracing
2a556dd diag: make SPX STC filter logging repeat (not one-shot)
5a810da diag: add STC filter logging for SPX windows
b29b513 diag: add one-shot SPX spot/vol failure logging to scan()
93620c4 feat: promote SPX hourly to live — SPX-D CalEngine, 90c+ floor, 15% bankroll, eighth-Kelly
c0a71cb fix: WAL checkpoint TRUNCATE→PASSIVE + DB health watchdog + batch size limit
63f6714 feat: hourly NO-side dashboard tracking — XRP and SOL
4c794df feat: SPX shadow calibration variants — T/blend exploration for promotion path
be6cc57 feat: lower ETH floor to 80c, SOL floor to 80c
22fe073 refine: sports comeback — NBA only, split core vs wide variants + dashboard section
8ee743c feat: add hourly shadow configs J-M — calibration parameter exploration
1c4071b feat: add weather NO-side live dashboard section
af3d6ff feat: wire weather NO-side execution pipeline — ready for March 20 launch
ada3a7e fix: split settlement API calls from DB writes to eliminate lock contention
92092a5 fix: enforce per-asset MIN_ENTRY_PRICE in executor (maker + escalation)
32deb5f feat: per-asset MIN_ENTRY_PRICE — BTC→89c, XRP ungated at 92c + shadow low-price variants
27468bc feat: expand 15M MAX_STC from 500s to 600s
c6e7b6a fix: defer _seen.add until after DB write to prevent silent shadow data loss
79dccfb feat: add weather NO-side shadow variant
0bced65 feat: add hourly shadow variants for T=2.0 and no-blend calibration
e3f1d3a feat: promote decided_contract t1+t2 as incremental overlay strategy
bb6b37c refine: weather shadow — price cap ≤30c variant + short STC variant + cautious bias correction
c887e83 chore: remove CG_LOOP debug log from hourly shadow configs
51f12fc debug: change CG_LOOP log to INFO level for visibility
b747629 debug: add CG_LOOP diagnostic log for hourly shadow configs
853ee66 feat: add hourly shadow Configs C–G from alpha research
61adc2b fix: cut Supabase egress — return=minimal on all UPSERTs + dashboard interval 10s→30s
6d86916 fix: SPX price feed circuit breaker — Polygon 403 backoff + Finnhub rate limit handling
cf84cdd feat: remove z-score gate — price+edge filters are sufficient
6028086 feat: add A4 late-window shadow variant (55-74c, STC≤300s, taker-only)
dd4848e feat: per-asset MIN_ENTRY_PRICE — BTC/ETH 88c, SOL keeps 86c
e08ab54 feat: add hourly Config B shadow variant (BTC 70-89c wl2)
a1267c9 test: add regression test for NO-side win counting (audit bug)
1ced6bf fix: correct NO-side win/loss counting in audit snapshot script
301cf21 test: add VolatilityEngine and scan pipeline tests (146 new)
4307d5d test: add execution path tests (57 new) — maker/taker escalation, IOC, ghost fills, post-only rejection
9d27d03 fix: resolve pre-existing test failures for clean full suite
8eb23c9 test: add fee calc, calibration engine, and probability engine tests (89 new)
efcb563 test: update tests for extraction, fix temp_t false positive, skip dead HAR tests
8fe815e refactor: extract constants to config.py and classes to models.py
6a2a732 feat: sports shadow — Platt calibration, strong-config filter, tennis exclusion
e8ac263 feat: hourly Config A shadow — no_XRP + edge ≤ 0.7% dual-insert
ec34925 perf: reduce supabase sync queries 81→~47 per cycle (PM-001 follow-up)
9ba270a harden: PM-001 regression tests, WAL/busy_timeout coverage, volume monitor
38754ef fix: batch settlement polling to eliminate "database is locked" contention
48ca19b feat: overnight LP shadow — 50-85c YES contracts during 00-12 UTC
4977173 feat: improve final 7 skills — all 17 now have error handling, WHY, examples
c4bee49 feat: improve batch 2 skills — error handling, WHY explanations, db-sync refs
acd26ed feat: improve top 5 skills — error handling, WHY explanations, db-sync reference
0069786 feat: skill routing guide + YAML frontmatter + skill organization
3fe7b44 feat: SOL Path C shadow + ETH filter shadow — counterfactual execution tracking
dbfec02 fix: auditor — suppress expected alerts, raise journal threshold
baf6ed8 fix: shadow WR calculation — exclude pnl=0 (no-trade) rows from denominator
1290fcb fix: escape underscores for Telegram Markdown + calibration gap formatting
e2a904a fix: auditor/researcher — schema counts, Markdown fallback, hourly dupes
e957641 feat: add auditor.py (hourly health checks) and researcher.py (3x daily reports)
d750b8b feat: comprehensive NO-side data collection — lower floors, wire SPX + sports
61477ed feat: NBA-only shadow signals + 60c price floor + LR≥1.2 gate
8059f4e fix: add exponential backoff for Open-Meteo 429 rate limits
a420b5c feat: weather pipeline fixes — std correction, NO-side data bug, focus filter
7a13a3b fix: MM fill simulation + XRP exclusion + hourly shadow audit coverage
e51f84d feat: STC sizing cap (live) + decided contract & relaxed edge shadows
c9426b5 fix: seed balance cache from DB on startup to avoid NULL gap after restart
8d149da feat: fix stale balance on non-candidate rows + unified shadow view
e3cc703 feat: overnight edge discount shadow (0.6x thresholds 04-11 UTC weekdays)
8044f59 fix: SOL taker-first path now increments session counters for dashboard
67b8dd3 feat: SOL taker-first, BTC 7s escalation, fix taker_ask_at_submit wipe
93ae378 feat: weekend edge discount shadow (0.6x thresholds Sat/Sun)
23a2109 feat: lower shadow min entry price from 86c to 70c for data collection
98f8dd7 fix: guard Kelly sizer against zero-payout prices (b=0 at 99c)
a7408ec feat: V2 calibration variant tracking for hourly shadow pipeline
43de091 fix: settled_time → settled_at in new monitoring scripts
595afd3 feat: add skills, monitoring scripts, MCP server, and CLAUDE.md rules
3fd8cc5 fix: HAR-RV shadow PnL uses actual Kelly sizing, not fake 1-contract fallback
08ec8ab feat: shadow taker tracker + maker opportunity cost script
fc02ee2 fix: Sharpe ratio annualization 252→365 (crypto trades 24/7)
d588d99 test: add comprehensive pytest infrastructure and CI workflow
36f8fe7 fix: read actual NO ask from market NBBO instead of deriving from YES prices
5c670a3 fix: upgrade 11 HIGH-risk silent except blocks to logging.warning
a7b8701 fix: maker fee = $0 — match Kalshi actual billing (verified against API)
62bde79 fix: use actual orderbook for NO-side pricing across all engines
dffdb05 fix: shadow engine NameError + move eval before price filter
6b017bb diag: upgrade fifteenm_shadow error logging from debug to warning
131b6f0 feat: add NO_SIDE_MIN_ENTRY_PRICE=70 and full NO-side shadow taxonomy
4f56b52 feat: add Approach 3 EGARCH gating model to 15M shadow pipeline
0412cde test: add regression test for _eval_opp_seen tuple destructuring (abd47c8)
abd47c8 fix: _eval_opp_seen cleanup crashes on NO-side 3-tuples
562b2f8 feat: add NO-side shadow evaluation across all markets and shadow variants
39c8451 feat: add BTC to hourly alt shadow strategies for HAR-RV vs EGARCH comparison
f4a4242 fix: compute 1-contract counterfactual PnL for ungated HAR-RV signals
035e5eb fix: remove tcolorbox skins dep + fix deprecated --highlight-style
f1194b6 docs: accuracy audit + visual redesign + dynamic templating overhaul
13e4f29 fix: dashboard counterfactual shows both combined + no-XRP shadow rows
e0c8c6d Auto-generate whitepaper PDFs and README stats [skip ci]
ae02019 fix: weather bias persistence + HRRR reliability + dashboard shadow merge
3f3c73b fix: rename shadow stage labels for clarity + merge in dashboard
f37ed76 fix: reduce false-positive health alerts for IOC/direct taker unfills
b20e4ca fix: fifteenm_shadow product_type filter + widen LightGBM training data
969cdf9 feat: shadow approach monitoring in audit scripts + cross-thread SQLite regression tests
53c953b fix: fifteenm_shadow SQLite cross-thread error — check_same_thread=False
bf6e89e fix: raise fifteenm_shadow DB insert error to WARNING for debugging
76aa512 Auto-generate whitepaper PDFs and README stats [skip ci]
c352976 feat: remove Firebase — Supabase is sole dashboard data source
ee5ae42 fix: pass Supabase syncer's own db_conn to _build_snapshot
98035b5 feat: auto-reconcile VPS/Supabase trade data drift
46cd3cc feat: 15M shadow audit + dashboard contract coverage
9ee5139 feat: 15M shadow engine — two alternative approaches for all 4 assets
ce2f69e fix: false ghost fill on IOC orders — check fill_count before registering position
f6ebd72 fix: edge_integrity query used nonexistent 'side' column on evaluated_opportunities
5c14f7e fix: correct SQL column names for evaluated_opportunities queries
a3d11c6 feat: dashboard accuracy fixes + 10 new analytics panels
bb11e64 fix: sports settlement skipping partially-settled games
0d30bda Auto-generate whitepaper PDFs and README stats [skip ci]
08d946b fix: increase Open-Meteo inter-call delay to 2s
321507a Auto-generate whitepaper PDFs and README stats [skip ci]
b9ab204 fix: add inter-call rate limiting in weather fetch_ensemble + self-test
9643514 Auto-generate whitepaper PDFs and README stats [skip ci]
f7ff544 feat: fix HRRR silent failure + position sizing in all audit scripts
56eff07 fix: ghost fill detection — prevent untracked position accumulation
313b7bc feat: SPX HAR-RV shadow engine + bot.py integration + dashboard panel
ece8138 feat: add market-only baseline Brier comparison to SPX + hourly audits
3186eb5 feat: add alt shadow strategy sections to hourly audit + alpha scripts
ffaae67 fix: hourly_alt_shadow SQLite cross-thread error
c41bde6 feat: hourly alt shadow strategies (MM + HAR-RV) for ETH/SOL/XRP
9ef3b00 feat: disable overconfident hourly CalEngine + shadow variant tracking
9b81ed3 fix: _submit_taker return value bug + git-diff regime detection for all audits
05ba9d6 feat: stale game settlement sweep + comprehensive sports audit rewrite
9263866 feat: weather instrumentation (wx_hrrr_temp, wx_corrected_mean) + audit rewrite
1bdf4a4 feat: SPX VIX instrumentation + comprehensive audit script rewrite
dffbb8c fix: correct breakeven WR values + per-tier edge analysis in audit
72fda9d fix: dashboard accuracy audit — Sharpe methodology, product_type filters, streak, breakeven WR
d7fb31d fix: hourly_applied_temp_t NULL when CalEngine active
f147b81 feat: shadow variant price_shadow_no_xrp for price 70-85c tracking
ae3a65f feat: shadow variant stc_shadow_no_xrp for STC 500-900s tracking
ffca9c6 fix: settled_at → settled_time column name in sports eval settlement
e92615f feat: add strategy computation to insufficient_edge INSERT
8aa6684 feat: enrich price_shadow INSERT with sizing, strategy, expected_value
fa3f5a0 feat: fix SPX + sports instrumentation gaps
9181937 fix: sports data gaps — market_price NULL, signaled_games restore, enriched INSERT
7da25ff feat: hourly shadow T×W grid — 6 new columns + blend_50 bug fix
9f1a9f2 feat: add price shadow analysis section to 15M audit
b82c467 feat: price shadow — collect edge data for 70-85c markets
053ba5c feat: headless audit automation — runner, alerts, post-deploy CI, timer
3eebab2 feat: regression test suite (62 tests) + pre-deploy check script
0554dbe feat: re-enable hourly CalEngine (observation-only, zero risk)
fbd26ec fix: nested window function in mv_daily_risk — split into 3 CTEs
3e9e3e0 feat: risk metrics framework — sync fix + Supabase migration 003
d982527 fix: dashboard data truth audit — 8 accuracy fixes
34251fc fix: update sports audit dedup recommendation
de9f338 fix: ESPN short team code matching + sport_group backfill
4b9b943 fix: correct 9 bugs in audit scripts
b35373f feat: CalEngine observation tracking across firebase, dashboard, and audit scripts
24d13d8 feat: wire sports CalEngine settlement pipeline
18680c6 fix: instantiate hourly/spx CalEngines even when disabled for data collection
0179aee feat: per-subtype CalibrationEngine registry for weather cities + sports groups
061985b feat: per-market CalibrationEngine registry + revert hourly cal
4b96f8f fix: restore hourly temperature shadow instrumentation + audit script accuracy
ad4abd6 fix: Firebase balance $0 glitch + CLAUDE.md interaction rules + auto syntax hook
357f33b fix: elevate silent failure logging + guard max() empty sequence
ae177a9 halve MIN_EDGE_BY_PRICE schedule: grid search 7 extra winners in 4 days
864008a add calibration grid search to 15M and hourly audit scripts
a68bd17 fix: handle None lr_scale in sport group calibration recs
c02dc1e feat: add sport_group awareness to sports audit scripts
d66b4ca Auto-generate whitepaper PDFs and README stats [skip ci]
8f860c8 feat: per-sport calibration architecture for sports comeback engine
f118206 Auto-generate whitepaper PDFs and README stats [skip ci]
165c513 fix: sports engine data gaps — eval_opp dedup, tennis game deficit, NHL logging
19b9c21 enable hourly CalibrationEngine: 5057 obs, replaces passthrough + T=1.45
97e2be8 raise DIRECT_TAKER_THRESHOLD 75→180s: 0% maker fill rate, 9 missed/day
2877245 Auto-generate whitepaper PDFs and README stats [skip ci]
397dac1 docs: comprehensive whitepaper update + drift-prevention hardening
f6643af fix: tennis player code matching
2ffb7db feat: add tennis (ATP + WTA) to sports comeback engine
7ab53f6 fix: soccer clock parsing, cross-series ticker matching, discovery logging
30bf266 audit: per-sport breakdown in sports shadow audit
5bca205 fix: sports engine side-matching fallback + signal dedup + diagnostics
55113d0 audit: make hourly R3/R4/R5 recommendations dynamic based on DB coverage
be20e11 fix: per-window position limit was dead code in observation mode
1f3d013 dashboard: add product_type to settled_trades + filter metrics to 15M
6e49edf audit skill: WAL checkpoint before SCP + /audit all option
9f8afc1 weather: expand to 19 cities + fix instrumentation gaps
234a149 fix: strategy_wait blocking observation signals + sports market_price NULL
d55d92b 15M: direct taker threshold 60→75s + order tracking + balance logging
a602571 15M: lower MIN_ENTRY_PRICE 87→86, edge threshold 0.7→0.5% at 86-88c
e29336a SPX: fix egarch_blend_sigma data gap + add temperature shadow columns
35ece38 fix: strategy_wait blocking weather/hourly/SPX signals + archive API backfill
e5f2550 hourly shadow: accelerate data collection
1393734 15M fill rate optimization: XRP shadow, relaxed edge thresholds, fill microstructure
76ab6ed auto-restart: exit after 6 consecutive tick errors for systemd recovery
2af4076 add incident detection: consecutive tick error escalation + recovery alerts
64f38df fix: move closing_price lookup outside DB write loop to prevent 10s+ lock
190bf27 sports: maximize data collection
aaf1f2f hourly shadow: add T=1.0 and T=2.5 temperature columns
017a2f0 fix 12 silent failures
93b99e3 audit cron: pre-compute shadow/audit metrics every 30min
a89c927 hourly shadow audit: merge supplemental queries
4a7ab9e weather shadow: audit rewrite, observed temp instrumentation
2bad00f SPX shadow: per-window limits, egarch_blend_var fix, MZ R² tracking
2d43606 sports shadow: raise price ceiling 38→80c, LR scale 0.5→0.2
acc4db1 disable hourly CalibrationEngine + lower HOURLY_MIN_ENTRY_PRICE 70→50
ebf8078 remove 90s taker floor + fix escalation_type tracking
cb0e11d enhance 15m_live_audit.py: 6 new analysis features
f920c17 docs: add weather config values and engine to CLAUDE.md
3f44c77 weather shadow R1-R5
ca12a6b docs: sync CLAUDE.md + analyst.py config values
e297e3d expand 15M scan window to 900s
f00213e promote: extend 15M live trading window from 300s to 500s STC
3eccb77 fix: cap XRP 15M position size to 12% max risk per trade
47d6b8f fix: use raw passthrough instead of fixed_beta for non-cal_eligible types
a078cce fix: gate 15M Platt calibration behind cal_eligible for non-15M product types
d8eb75e feat: dedicated hourly CalibrationEngine (isolated from 15M)
0753e8a SPX shadow: add health alerting + NBBO pre-filter
5ad1c92 Add DB busy_timeout startup assertion
2d5575c docs: add sqlite busy_timeout rule to CLAUDE.md
7dbd821 fix: add busy_timeout to sports_engine DB
f4987b7 Auto-generate whitepaper PDFs and README stats [skip ci]
2f86175 docs: add SPX, weather, and sports engine sections to whitepapers
ba761e8 Auto-generate whitepaper PDFs and README stats [skip ci]
75d7a29 fix: pass VPS DB path to stats script in CI
ce3db9c Auto-generate whitepaper PDFs and README stats [skip ci]
cb17308 fix: remove workflow_run trigger from whitepaper CI
05a9fcf Auto-generate whitepaper PDFs and README stats [skip ci]
69d435a Consolidate doc workflows
0a943aa docs: auto-update rendered docs with latest stats
35a7a1c Fix docs CI: add contents:write permission
0b9c1f4 Fix: unignore rendered whitepapers
385df8f Auto-generate whitepaper PDFs and README stats [skip ci]
3043096 Docs sync: fix stale values, add automated doc generation pipeline
fe0dd5a Auto-generate whitepaper PDFs and README stats [skip ci]
350284d Fix sports event-game matching: require BOTH team codes
ac8e70e Fix 5 sports engine infrastructure gaps
4a95d02 Fix 19 silent failure modes from comprehensive code audit
f0de9bf Add 15M live trading audit script with 7-section analysis
```

---

*End of DOC_STATE.md*
