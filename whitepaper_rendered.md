---
title: "Kalshi Crypto Trading Bot"
subtitle: "Technical Whitepaper"
author: "Gabriel Kagan"
date: "March 2026"
titlepage: true
titlepage-color: "0F1B33"
titlepage-text-color: "FFFFFF"
titlepage-rule-color: "D4883E"
titlepage-rule-height: 4
toc: true
toc-own-page: true
numbersections: true
colorlinks: true
linkcolor: "navylink"
urlcolor: "bluelink"
toccolor: "navylink"
header-left: "\\footnotesize Kalshi Trading Bot"
header-right: "\\footnotesize Technical Whitepaper"
footer-left: "\\footnotesize Gabriel Kagan"
footer-center: ""
footer-right: "\\footnotesize \\thepage"
mainfont: "DejaVu Sans"
monofont: "DejaVu Sans Mono"
fontsize: "11pt"
geometry: "margin=1in"
header-includes:
  - |
    ```{=latex}
    \usepackage{tcolorbox}
    \tcbuselibrary{breakable}
    \usepackage{xcolor}
    \usepackage{colortbl}

    \definecolor{navylink}{HTML}{2C4270}
    \definecolor{bluelink}{HTML}{2C5AA0}
    \definecolor{navyprimary}{HTML}{1B2A4A}
    \definecolor{navydark}{HTML}{0F1B33}
    \definecolor{navylight}{HTML}{2C4270}
    \definecolor{accentwarm}{HTML}{D4883E}
    \definecolor{codebg}{HTML}{F5F6FA}
    \definecolor{codeborder}{HTML}{D1D5E0}
    \definecolor{calloutbg}{HTML}{FFF8F0}
    \definecolor{calloutborder}{HTML}{D4883E}

    % Styled code blocks
    \newenvironment{Shaded}{%
      \begin{tcolorbox}[
        breakable,
        colback=codebg,
        colframe=codeborder,
        boxrule=0.5pt,
        arc=3pt,
        left=10pt, right=10pt, top=8pt, bottom=8pt,
        fontupper=\small\ttfamily,
      ]
    }{%
      \end{tcolorbox}
    }

    % Blockquotes as callout boxes
    \newtcolorbox{quotecallout}{
      breakable,
      colback=calloutbg,
      colframe=calloutborder,
      leftrule=3pt, rightrule=0pt, toprule=0pt, bottomrule=0pt,
      arc=0pt, outer arc=0pt,
      left=12pt, right=12pt, top=10pt, bottom=10pt,
      fontupper=\small,
    }
    \renewenvironment{quote}{\begin{quotecallout}}{\end{quotecallout}}
    ```
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

> **Important**: When a learned calibration method is active (Platt, Beta, or BLR trained on settlement data), the dynamic cap schedule is **bypassed entirely**. A numerical safety ceiling of 99.9% is used instead. The rationale is that learned calibration already accounts for the overconfidence the cap was designed to prevent. The cap schedule only applies during startup before sufficient training data accumulates.

As expiry approaches and less can go wrong, the cap relaxes to allow higher-confidence trades in the endgame.

### Step 5: Market-Price Blending

The calibrated probability is blended with the market-implied probability:

$$p_{final} = 0.60 \times p_{cal} + 0.40 \times p_{market}$$

This 60/40 blend (60% model, 40% market) was validated against a no-blend alternative: the no-blend system was +1.86 percentage points overconfident (Brier score 0.0946 vs 0.0422), and would have generated 16 trades that were net -$53.54. The 40% market weight was subsequently tuned from 50% after data showed the model was underconfident by 0.8–2.1pp at 90%+ probabilities. The no-blend system now monitors in shadow mode.

### Sanity Checks

- **Z-score limit**: If $|z| > 25$, the market is refused (all historical losses have z-scores below this threshold; the previous limit of 12 was blocking only winners)
- **Model-market discrepancy**: If $p_{cal} > 90\%$ but market price $< 75$¢, the market is refused (the model may be missing information the market has)

## 3.3 Edge Detection

A trade requires positive expected value after accounting for fees.

### Fee Formula

Kalshi charges fees using a variance-based formula:

$$\text{taker fee} = \left\lceil \text{fee\_mult} \times C \times P \times (100 - P) / 100 \right\rceil \text{ cents}$$
$$\text{maker fee} = \$0$$

where $C$ is the number of contracts and $P$ is the trade price in cents. The ceiling is applied to the total, not per contract. Default `fee_mult` is 0.07 for crypto; SPX ("finance" category) uses 0.035 (half rate). Maker fills incur no fee on any product.

### Fee-Adjusted Edge

The scanner evaluates edge using taker fees (worst-case), so any candidate that passes the filter is profitable even if maker order is rejected:

$$\text{edge} = p_{final} - \frac{\text{best\_ask}}{100} - \frac{\text{taker\_fee}}{C \times 100}$$

A trade must satisfy:

$$\text{edge} \geq \text{get\_min\_edge(price)}$$

The minimum edge is price-dependent, reflecting the higher risk of expensive contracts:

| Entry Price | Min Edge |
|---|---|
| 97¢+ | 1.0% |
| 95–96¢ | 0.75% |
| 93–94¢ | 0.5% |
| 91–92¢ | 0.20% |
| 89–90¢ | 0.25% |
| 80–88¢ | 0.25% |

A flat fallback of 0.25% (MIN\_EDGE\_PCT) applies if the price-dependent schedule is unavailable.

## 3.4 Kalshi Order Flow (Shadow Mode)

The KalshiOrderFlowTracker monitors Kalshi's own orderbook for predictive signals:

- **Imbalance**: Ratio of bid vs. ask depth — strong imbalance (>0.8 or <0.2) suggests directional pressure
- **Depth velocity**: Rate of change in total depth — draining liquidity may predict a move
- **Spread convergence**: Narrowing spread + trending depth suggests informed trading
- **Adjustments**: ±1 to 1.5pp based on signal strength, with confidence levels based on snapshot count

Currently logging only — signals are computed but do not affect trading decisions.

## 3.5 S&P 500 Intraday Engine (Observation Mode)

The SPX engine trades S&P 500 15-minute prediction markets (KXINXU series) using the same EGARCH framework adapted for equity microstructure. The engine was briefly promoted to live trading on March 17, 2026 but was reverted the same day after Polygon.io returned 403 errors, breaking the primary price feed. It now runs in observation mode with Finnhub as the primary feed. A HAR-RV shadow strategy (`spx_harrv_shadow.py`) runs in parallel for comparison.

### Data Sources

| Source | Data | Frequency |
|---|---|---|
| Polygon.io | SPX spot price (currently returning 403) | 1s polling during RTH |
| Finnhub | SPX primary fallback + SPY→SPX conversion (10.03×) | 1s polling |
| CBOE VIX | Implied volatility index | 60s polling |

### Intraday Seasonal Filter

SPX volatility follows a well-documented U-shaped intraday pattern (high at open/close, low midday). The engine deseasonalizes returns using 13 half-hour buckets (09:30–16:00 ET) with EWMA-calibrated seasonal factors, preventing systematic bias from time-of-day effects.

### EGARCH Adaptation for Equities

The EGARCH(1,1) model is re-parameterized for equity-specific dynamics:

- **Leverage bounds**: $\gamma \in (-0.30, -0.05)$ — approximately 4× stronger than crypto, reflecting the well-documented equity leverage effect (down moves increase volatility more than up moves)
- **VIX integration**: When VIX-implied vol diverges >30% from realized, the engine shifts 30% weight toward VIX. On startup, EGARCH is seeded from VIX to avoid a cold-start period
- **Market hours guard**: NYSE RTH 9:30–16:00 ET with DST awareness and holiday calendar. Engine skips the first 10 minutes post-open (auction noise)

### CalEngine (SPX-D Variant)

The SPX engine has a dedicated CalibrationEngine instance (`_CAL_REGISTRY["spx_hourly"]`) that learns from SPX settlement data independently of crypto calibration. Currently in observation mode — accumulating data for future promotion.

### Configuration

| Config | Value |
|---|---|
| Status | Observation only (reverted from brief live stint Mar 17 — Polygon 403) |
| Entry price range | 90–99¢ |
| STC window | 300–1800s |
| Market blend | 0/100 (no blend — CalEngine calibration only) |
| Max risk per trade | 10% |
| Kelly fraction | 0.125 (eighth-Kelly) |
| Fee multiplier (taker) | 0.035 (half crypto) |
| Fee multiplier (maker) | 0.0 ($0 — maker fills are free) |
| Bankroll fraction | 15% (SPX sizes off 15% of total balance — crypto unaffected) |
| Max positions per window | 2 |
| Max risk per window | 15% |

## 3.6 Weather Temperature Engine (Observation Mode)

The weather engine trades daily high temperature prediction markets across **19 US cities** using numerical weather prediction (NWP) ensemble forecasts.

> **Research verdict: NO ALPHA.** As of March 2026, YES-side WR is 29.8% with +32pp overconfidence (model predicts much higher than actual outcomes). Brier score 0.3498. The NO-side shows 73.1% WR but pricing doesn't generate sufficient edge. Short-STC windows (1–8h before settlement) show marginal promise (n=34) but sample size is insufficient to draw conclusions. The NO-side execution pipeline is wired but kill-switched off (`WEATHER_NO_SIDE_LIVE = False`). Per-city CalEngines are learning in shadow to improve calibration.

### Cities and Series

| City | Series Ticker |
|---|---|
| New York | KXHIGHNY |
| Chicago | KXHIGHCHI |
| Miami | KXHIGHMIA |
| Denver | KXHIGHDEN |
| Los Angeles | KXHIGHLAX |
| Austin | KXHIGHAUS |
| Atlanta | KXHIGHTATL |
| San Francisco | KXHIGHTSFO |
| Dallas | KXHIGHTDAL |
| Phoenix | KXHIGHTPHX |
| Philadelphia | KXHIGHPHIL |
| Minneapolis | KXHIGHTMIN |
| Seattle | KXHIGHTSEA |
| Houston | KXHIGHTHOU |
| Boston | KXHIGHTBOS |
| Las Vegas | KXHIGHTLV |
| Oklahoma City | KXHIGHTOKC |
| Washington DC | KXHIGHTDC |
| New Orleans | KXHIGHTNOLA |

Each city has Kalshi bracket and threshold markets settling daily based on the observed high temperature.

### Ensemble Probability Model

The engine queries the Open-Meteo API for two independent NWP ensemble systems:

| Model | Members | Resolution | Provider |
|---|---|---|---|
| GFS Seamless | 31 | ~13 km | NOAA |
| ECMWF IFS 0.25° | 51 | ~25 km | ECMWF |
| HRRR (deterministic) | 1 | ~3 km | NOAA |

The 82 ensemble members (31 GFS + 51 ECMWF) provide a distribution of possible temperature outcomes. The engine fits a Gaussian $(\mu, \sigma)$ to the combined ensemble and computes:

- **Bracket markets**: $P(\text{lower} < T < \text{upper})$ via CDF difference
- **Threshold markets**: $P(T > \text{threshold})$ or $P(T < \text{threshold})$ via tail probability

### Bias Correction

An EWMA bias tracker ($\lambda = 0.90$, 7-day half-life) maintains per-city forecast error history. After each day's actual temperature is observed, the engine updates $\text{bias}_\text{city} = \lambda \times \text{bias}_\text{prev} + (1-\lambda) \times (\text{actual} - \text{forecast})$ and shifts the ensemble mean accordingly.

### Configuration

| Config | Value |
|---|---|
| Status | Shadow (observation only) |
| Entry price range | 10–99¢ |
| Settle window | Daily (min 1hr before close) |
| Market blend | 80/20 (model/market) — ensemble is primary signal |
| Max risk per trade | 10% |
| Kelly fraction | 0.25 (quarter-Kelly) |
| Poll interval | 15 minutes (weather changes slowly) |
| Max cities per day | 19 (all enabled for data collection) |

## 3.7 Sports Comeback Engine (Observation Mode)

The sports engine monitors live games across 28 leagues for Bayesian comeback signals — identifying situations where a pregame favorite is trailing but statistically likely to recover.

> **Research status (March 2026):** ALPHA DETECTED (4/6 checks pass). Basketball is the clear alpha source (69.2% WR, n=39, Fisher p=0.035 vs other sports). Tennis is a drag (52.2% WR, -$1.64 PnL). NBA strong-config (pregame ≥60%, price ≤70c, time remaining >85%) is the best robust filter. Per-sport-group CalEngines are learning in shadow. Overall SPRT has not converged — needs approximately 2–3 more weeks of data collection before a promotion decision can be made.

### Supported Leagues

**Binary outcome (home/away)**: NBA, NHL, MLB, NCAAB, NCAAF, NFL, WNBA, UFC, ATP Tennis, WTA Tennis, plus esports (CS:GO, LoL, Valorant — Kalshi price monitoring only)

**Three-way outcome (home/draw/away)**: EPL, Bundesliga, La Liga, Serie A, UCL, Ligue 1, MLS, Liga MX, Europa League, Conference League, Super Lig, Eredivisie, World Cup, FIFA Friendlies, AFC Asian Cup

### Data Sources

| Source | Data | Coverage |
|---|---|---|
| ESPN API | Live scores, clock, period, red cards | Leagues with live scoreboards |
| Kalshi API | Game-level market prices | 28 series (2 markets/binary game, 3/three-way) |

### Bayesian Comeback Model

The model computes posterior comeback probability using a lookup-table of empirically calibrated likelihood ratios:

$$P(\text{comeback} \mid \text{data}) = \frac{LR \times P(\text{prior})}{LR \times P(\text{prior}) + (1 - P(\text{prior}))}$$

**Likelihood ratio table keys**: $(d, t, s)$ where:
- $d$ = deficit bucket (binary: small/medium/large/blowout; three-way: 1-goal/2-goal/3+)
- $t$ = time remaining bucket (>75%, 50–75%, 25–50%, <25%)
- $s$ = pregame strength bucket (strong favorite ≥75%, moderate 65–75%, slight 55–65%)

**Conservative scaling**: LR values are compressed 80% toward neutral ($LR_\text{scaled} = 1.0 + (LR_\text{raw} - 1.0) \times 0.2$) to prevent overconfident signals. This aggressive compression was chosen because the LR tables are based on general historical comeback rates that may not transfer to Kalshi-specific market dynamics.

**Model-market safety cap**: Signals are rejected when the model posterior exceeds the market price by more than 80 percentage points, allowing wide-gap data collection for model calibration. This threshold will be tightened once sufficient calibration data accumulates.

### Tennis Support

Tennis (ATP and WTA) uses a specialized parsing pipeline due to structural differences from team sports:

- **ESPN structure**: Tennis events are organized as tournament → groupings → competitions, with an extra nesting level compared to team sports
- **Gender filtering**: ESPN returns all genders at a tournament endpoint; the engine filters to "Men's Singles" for ATP and "Women's Singles" for WTA to prevent cross-gender contamination
- **Player codes**: ESPN has no 3-letter abbreviations for tennis players. Codes are derived from player last names (first 3 alpha characters of concatenated surname parts): "Jannik Sinner" → "SIN", "Alex de Minaur" → "DEM", "Christopher O'Connell" → "OCO"
- **Scoring**: Sets won (count of `linescores` entries with `winner=True`) rather than cumulative points
- **Time remaining**: Estimated from sets completed + games played in current set (no clock in tennis)
- **Deficit classifier**: 1 set down = "small" (common comeback), 2+ sets down = "large" (rare, best-of-5 only)

### Entry Criteria

Current entry criteria are relaxed for maximum data collection in observation mode:

| Criteria | Binary | Three-way |
|---|---|---|
| Min pregame favorite prob | 50% | 50% |
| Max Kalshi favorite price | 95¢ | 95¢ |
| Min time remaining | 10% | 10% |
| Max deficit bucket | Blowout | 3 goals |
| Signal dedup | One signal per game | One signal per game |

These thresholds are intentionally permissive to capture wide-gap and late-game data for model calibration. They will be tightened before any promotion to live trading.

### Game Lifecycle

1. **Pregame capture**: Record Kalshi prices for each team before game starts
2. **Live evaluation**: Poll ESPN scores every 30 seconds, compute LR on each score change
3. **Signal dedup**: One signal per game to prevent correlated exposure
4. **Settlement**: After game ends, record final outcome, closing prices, and simulated P&L

## 3.8 Execution Strategy

The executor uses a maker-first approach with three-tier post_only rejection handling, time-aware escalation, and direct taker execution for late-window entries.

### Direct Taker Threshold

When less than **180 seconds** remain before settlement, the system skips the maker order entirely and submits a direct IOC (immediate-or-cancel) taker order.

> **Data justification**: Maker fill rate was only 7.7% (1/13 candidates) at 0–60s STC. Twelve missed candidates were all winners (~$49 net missed profit). Threshold raised from 60s → 75s → 180s. Edge and liquidity checks still apply.

### Three-Tier Post-Only Handler

When a `post_only=True` maker order is rejected (the order would cross the spread rather than rest on the book), the system escalates through three tiers:

| Tier | Trigger | Action |
|---|---|---|
| Tier 1: Normal maker | 0–1 rejections | Standard maker order, 1–2¢ below fair value |
| Tier 2: Degraded maker | 2 rejections | Same offset + 1¢ additional discount. If price drops below asset floor (80–92¢), skipped. |
| Tier 3: Taker IOC | 3+ rejections | Edge re-verified with actual taker fees → IOC order if still profitable |

Rejection counts expire after 30 seconds and are per-ticker (unique per market window).

### Time-Based Escalation

For orders that are successfully placed but sit unfilled:

| Urgency | Time to Close | Maker Wait |
|---|---|---|
| Low | ≥180s | 15s |
| Medium | 120–180s | 7s (86% of fills happen within 7s) |
| High | 60–120s | 5s |

### Per-Asset Execution Overrides

- **SOL taker-first** (`SOL_TAKER_FIRST=True`): SOL bypasses maker entirely and submits direct IOC taker at all STC values. SOL's thin Kalshi orderbooks made maker fills unreliable.
- **BTC escalation wait override**: BTC uses a 7s maker wait at STC ≥180s (vs. the default 15s), reflecting BTC's higher liquidity and faster fill times.

### Decided Contract Overlay

A separate overlay identifies near-certain settlements and routes them to direct taker execution:

| Tier | Z-Score Threshold | Price Range | Sizing |
|---|---|---|---|
| T1 | z ≤ -5.0 | 93¢+ | Fixed 20% bankroll |
| T1B | z ≤ -4.0 | 95¢+ | Fixed 20% bankroll |
| T2 | z ≤ -3.0 | 93–96¢ | Fixed 20% bankroll |
| T2-Z25 | z ≤ -2.5 | 93–96¢ | Fixed 20% bankroll |
| T2-Z2 | z ≤ -2.0 | 93–96¢ | Fixed 20% bankroll |

T1B was added based on research showing 40/40 = 100% win rate in the -5 < z ≤ -4 zone at 95¢+. T2-Z25 and T2-Z2 extend coverage into shallower z-score zones at 93–96¢. All five tiers are enabled by default (env var toggles) with a per-window cap of 35% bankroll risk. These are incremental — they add on top of the regular trading pipeline, capturing near-certain outcomes that the standard edge filter might not size aggressively enough.

Six expansion shadow variants are also collecting data for potential future tiers:

| Shadow Variant | Z-Score | Price Range | Status |
|---|---|---|---|
| T1A | z ≤ -5.0 | 90–92¢ | Shadow — lower price floor for T1 |
| T1B-EXP | z ≤ -4.0 | 93–94¢ | Shadow — T1B at lower prices |
| T2A | z ≤ -3.0 | 90–92¢ | Shadow — lower price floor for T2 |
| T2B | z ≤ -3.0 | 97¢+ | Shadow — T2 at higher prices |
| T3 | z ≤ -2.5 | 95¢+ | Shadow — shallower z-score |
| T3A | z ≤ -2.5 | 93–94¢ | Shadow — T3 at lower prices |

### Maker-to-Taker Conversion

1. Place maker order with `post_only=True` ($0 maker fee)
2. Monitor for fills via Kalshi WebSocket (zero API cost) with REST polling fallback
3. Poll queue position every ~5s for queue-aware escalation timing
4. If timeout reached without fill:
   - Attempt `amend_order()` to convert to taker price in-place (avoids cancel+replace race)
   - If amend fails, fall back to cancel + IOC (`time_in_force="immediate_or_cancel"`) taker order
   - Re-validate price still in [80¢, 99¢] (per-asset floors apply) before taker submission

> **Taker execution data**: Taker trades show 14W/0L (100% win rate) across all STC zones. The previous maker-only threshold of 90s was removed after this data demonstrated taker execution is profitable at all time horizons.

### Addon System

After an initial fill, the bot can add to the position under specific conditions:

- **Price improvement addon** (live): When the ask price improves by ≥1¢ from entry while remaining above the asset's floor, submit a taker addon at up to 50% of original size. Max 1 addon per position, total risk capped at 35%.
- **Dip addon** (shadow — `DIP_ADDON_SHADOW_MODE=True`): When the ask drops ≥3¢ below entry, log the signal for analysis. Same sizing parameters as price improvement but currently logging only, not executing.

### Partial Fill Handling

Orders may partially fill (e.g., 3 of 13 contracts). The execution engine tracks `filled_so_far` cumulatively and keeps the order active until fully filled or escalated. REST fill detection uses a `_seen_fill_ids` set to prevent double-counting across consecutive polls and against WebSocket fills. Escalation to taker subtracts partial fills from the IOC count to prevent position doubling.

### UUID Persistence

Each order gets a `client_order_id` (UUID4) written to SQLite before API submission. This ensures crash recovery — if the bot restarts mid-order, it can reconcile using the persisted UUID.

### Order Lifecycle Tracking

Every evaluated opportunity that reaches candidate status gets full order lifecycle tracking:

- **`order_id`**: Kalshi's assigned order identifier
- **`order_submitted_at`**: Timestamp of API submission
- **`order_outcome`**: Final disposition — `filled`, `unfilled`, `canceled`, or `partial_fill`

This enables post-hoc analysis of execution quality, fill rates by STC zone, and maker vs. taker performance comparison.

## 3.9 Position Sizing

### Edge-Tiered Sizing

Position sizing is tiered by fee-adjusted edge, with higher-conviction trades receiving larger allocations:

| Fee-Adjusted Edge | Risk Fraction |
|---|---|
| ≥ 4% | 25% of bankroll |
| ≥ 2.5% | 20% of bankroll |
| ≥ 1.8% | 15% of bankroll |
| ≥ 1.2% | 10% of bankroll |
| ≥ 0.9% | 7% of bankroll |
| ≥ 0.7% | 5% of bankroll |
| ≥ 0.5% | 3% of bankroll |
| ≥ 0.25% | 2% of bankroll |

Safety ceiling: max 25% of bankroll at risk per trade.

### Per-Vertical Kelly Fractions

| Vertical | Kelly Fraction | Rationale |
|---|---|---|
| 15M crypto | 1.0 (full Kelly) | Primary system, most data, well-calibrated |
| Hourly crypto | 0.25 (quarter-Kelly) | Observation mode — conservative |
| SPX hourly | 0.125 (eighth-Kelly) | Ultra-conservative for new vertical |
| Weather | 0.25 (quarter-Kelly) | Observation mode |
| Decided contracts | Fixed 20% risk | Not Kelly-derived — fixed sizing for near-certain outcomes |

### Per-Asset Risk Caps

| Asset / Vertical | Max Risk Per Trade |
|---|---|
| 15M (BTC) | 15% |
| 15M (ETH) | 20% |
| 15M (SOL) | 15% (raised from 12% — 43.9% of trades were capped) |
| 15M (XRP) | 15% |
| Hourly | 15% |
| SPX | 10% |
| Weather | 10% |

### Low-STC Sizing Cap

Below 100 seconds before settlement (`LOW_STC_SIZING_CAP_THRESHOLD`), position sizes are halved (`LOW_STC_SIZING_CAP=0.50`). Last-second price reversals at low STC have disproportionate impact — halving the position limits downside on these trades.

### Drawdown Scaling

| Balance vs. Starting | Sizing Adjustment |
|---|---|
| ≥ 85% | Full sizing |
| 75–85% | Half sizing |
| 65–75% | Quarter sizing |
| < 65% | Halt trading |

This creates a geometric de-risking curve that preserves capital during losing streaks.

## 3.10 AI Analyst System

The analyst engine (`analyst.py`) uses the Claude API to provide automated post-trade analysis:

- **Loss root-cause analysis**: After every losing trade, the analyst examines market conditions, volatility regime, entry timing, and model state to identify the cause
- **Pattern detection**: Identifies recurring loss patterns across assets, time windows, and market conditions
- **Telegram alerts**: High-confidence findings are pushed to Telegram for real-time operator awareness
- **Non-interfering**: The analyst runs asynchronously and never affects trading decisions — it is purely diagnostic

---

# Part 4: Risk Management

## Position Sizing Controls

- **Edge-tiered sizing**: Position size scales with conviction — 25% max at 4%+ edge, down to 2% at 0.25% edge
- **Drawdown scaling**: Size halved below 85% of rolling 7-day peak balance, quartered below 75%, trading halted below 65%
- **Hard limits**: Maximum risk per trade capped at 25% of bankroll

## Market Selection Controls

- **Multi-asset capable**: Can trade multiple assets per 15-minute window
- **Price range guardrails**: Only trade contracts priced 75–99¢ (global floor), with per-asset overrides: BTC 88¢ (LPNE: 80–87¢ near-expiry), ETH 90¢, SOL 80¢ (gate blocks ≤85¢ at STC≥300s), XRP 92¢. Below these floors, win rates are insufficient after fees; above 99¢ offers insufficient reward
- **Price-dependent edge threshold**: Fee-adjusted edge must exceed a price-dependent minimum (0.25% at 80¢ up to 1.0% at 97¢+) after taker fees (worst-case)
- **Scanner uses taker fees**: Every candidate is profitable even if forced to taker execution

## Model Sanity Controls

- **Z-score limit**: Refuse markets where $|z| > 25$ (validated against settlement data: 82 tradeable z-score rejections above 12 were all winners, leading to the raise from 12 → 25)
- **Model-market discrepancy**: If the model estimates >90% probability but the market prices below 75¢, refuse (the model may be missing material information)
- **Dynamic probability cap**: Time-dependent ceiling (93–99.5%) prevents overconfidence during startup; bypassed (99.9% ceiling) once learned calibration is active
- **Data-driven calibration**: CalibrationEngine learns from settlement outcomes, replacing fixed assumptions with empirical mappings
- **Market-price blending**: 60/40 blend (60% model, 40% market) anchors estimates and prevents systematic overconfidence

## Execution Controls

- **Three-tier post_only handler**: Escalates from normal maker → degraded maker → taker IOC after repeated rejections, with edge re-verification at each tier
- **Maker-first with `post_only`**: $0 maker fee (taker fee only on escalation), rejected if it would cross the spread
- **Direct taker below 180 seconds**: Below 180s STC, maker orders are skipped entirely (7.7% fill rate at low STC) — direct IOC taker submitted with full edge/liquidity validation
- **WebSocket fill detection**: Zero-cost fill monitoring via Kalshi WebSocket, with REST polling fallback
- **Amend-first escalation**: Uses `amend_order()` API to convert maker→taker in-place, avoiding cancel+replace race conditions
- **IOC taker orders**: Taker escalation uses `time_in_force="immediate_or_cancel"` to prevent stale resting orders
- **Partial fill tracking**: Escalation subtracts filled contracts to prevent position doubling
- **Price re-validation**: After maker timeout, the system re-fetches the orderbook and re-validates the price range before submitting a taker order
- **UUID persistence**: Order IDs written to disk before API submission, enabling crash recovery without duplicate orders
- **Rejection expiry**: Post_only rejection counts expire after 30 seconds, preventing stale state from affecting future windows

## Per-Window Correlation Controls (Hourly/SPX)

- **Max positions per window**: 2 (limits correlated multi-strike exposure)
- **Max risk per window**: 15% of bankroll (prevents simultaneous multi-asset blowups)
- **Fractional Kelly sizing**: Non-15M verticals use reduced Kelly fractions — quarter-Kelly (0.25) for hourly/weather, eighth-Kelly (0.125) for SPX. 15M uses full Kelly (1.0)
- **SPX bankroll isolation**: SPX sizes off 15% of total balance, preventing SPX losses from affecting crypto sizing

---

# Part 5: Performance

## Live Trading Results

| Metric | Value |
|---|---|
| **Status** | Live trading since February 22, 2026 |
| **Settled trades** | 2,701 |
| **Win rate** | 93.4\% (2,523W / 178L) |
| **Assets** | BTC (88¢+, LPNE 80¢+), ETH (90¢+), SOL (80¢+, taker-first, sub-86¢ gate), XRP (92¢+) |

## Markets

### Crypto 15-Minute (Live Trading)

Binary contracts settling every 15 minutes. Series: KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M. STC window: scan 0–900s, live 0–600s, shadow observation 600–900s. Decided contract overlay (T1/T1B/T2/T2-Z25/T2-Z2) adds incremental trades on near-certain outcomes, with 6 expansion shadows collecting data for future tiers. Terminal Momentum (TM) trades 95–99¢ contracts in the final 1–5 minutes (50–100 contracts fixed). Low-Price Near-Expiry (LPNE) intercepts BTC at 80–87¢ in the final 10–120 seconds (50 contracts fixed). STC sizing scaler reduces position size proportionally to time remaining (contracts × 300/STC for STC > 300s). SOL sub-86¢ time gate blocks entries at ≤85¢ with STC ≥ 300s.

### Crypto Hourly (Live — Kill Switch Gated)

75 strikes per event, settling every hour. Currently collecting calibration data only — no live trading. Was briefly promoted to live trading (Feb 27–28) but reverted after -$97 overnight disaster from calibration overconfidence and correlated multi-strike exposure. Hourly CalEngine disabled (+44pp overconfident); uses T=1.45 temperature scaling instead. BTC is the only viable hourly asset — ETH/SOL structurally unprofitable after fees, XRP fundamentally broken. Series: KXBTCD, KXETHD, KXSOLD, KXXRPD.

### S&P 500 Intraday (Observation Mode)

15-minute binary contracts on the S&P 500 during NYSE regular trading hours. Series: KXINXU. Uses equity-adapted EGARCH with VIX integration and intraday seasonal adjustment. Was briefly promoted to live Mar 17, reverted same day due to Polygon.io 403 errors breaking the primary price feed. Now observation-only with Finnhub as primary fallback. Per-window limits: max 2 positions, 15% risk cap. Eighth-Kelly sizing (0.125), 90¢+ floor, no market blend (CalEngine only).

### Weather Temperature (Observation Mode — NO ALPHA)

Daily high temperature markets across 19 US cities. Bracket and threshold contracts settling based on the observed daily high. Probability from 82-member NWP ensemble (GFS + ECMWF). Research verdict: YES-side 29.8% WR with +32pp overconfidence, NO-side 73.1% WR but pricing doesn't generate sufficient edge. NO-side execution pipeline wired but kill-switched off.

### Sports Outcomes (Observation Mode — ALPHA DETECTED)

Live game outcome markets across 28 leagues including NBA, NHL, MLB, NFL, EPL, ATP/WTA Tennis, and more. Bayesian comeback model identifies edge when pregame favorites trail in-game. Binary and three-way (soccer draw) market types. Basketball is the clear alpha source (69.2% WR, Fisher p=0.035). Tennis is a drag (52.2% WR, negative PnL). SPRT hasn't converged — needs more data before promotion decision.

---

# Part 6: Infrastructure

## Deployment

| Component | Detail |
|---|---|
| **Host** | DigitalOcean droplet (Ubuntu 24.04), 1 vCPU / 2GB RAM / 48GB disk |
| **Runtime** | Python 3, virtualenv |
| **Process manager** | systemd (`kalshi-bot` service) — auto-restarts on crash |
| **Startup** | `start.sh` activates venv, sources `.env`, launches `bot.py` |
| **Auto-deploy** | Push to `main` → GitHub Action → SSH → pull → syntax-check → restart |

## Data Persistence

### SQLite (state.db)

The primary state store uses SQLite in WAL (Write-Ahead Logging) mode for crash resilience. All connections use `busy_timeout=10000` to handle concurrent access from multiple threads (bot main loop, Supabase sync, sports engine).

| Table | Purpose |
|---|---|
| `positions` | Active positions (ticker, asset, side, count, avg price) |
| `pending_orders` | Orders awaiting fill (with UUID client_order_id) |
| `settled_trades` | Completed trades with P&L, product_type, and execution metadata |
| `rejected_opportunities` | Markets rejected with reason, model state, and product_type |
| `evaluated_opportunities` | Every market evaluation with filter stage, order lifecycle tracking (order_id, order_submitted_at, order_outcome), and available_balance_cents |

### JSONL Journals

Append-only journal files provide a complete audit trail:

| Journal | Contents |
|---|---|
| `opportunity_journal.jsonl` | Filter stage tracking for every market evaluation |
| `scan_journal.jsonl` | Per-tick scan summaries (~330MB/day) |
| `rejection_journal.jsonl` | Settlement outcomes for rejected opportunities |
| `fill_model_journal.jsonl` | Maker order lifecycle data for ML fill prediction |

**Journal rotation**: A daily cron job (4 AM UTC) runs `rotate_journals.sh` using a copytruncate pattern — journals are compressed to `journal_archives/` with gzip and 30-day retention. The bot uses open/close per write, so rotation is safe without process interruption.

## Supabase Real-Time Dashboard

`supabase_sync.py` pushes a state snapshot every 30 seconds to the `dashboard_state` table in Supabase. A static HTML dashboard hosted on GitHub Pages reads from Supabase Realtime, showing:

- Current positions and P&L
- Active market evaluations
- Volatility regime indicators and EGARCH/NIG parameters
- Orderbook visibility for active windows
- Execution engine statistics (amend success rate, WS fill ratio, post_only rejection counts, taker escalation counts)
- Calibration diagnostics and CalEngine Registry (per-engine observations, method, Brier)
- Hourly observation stats
- SPX shadow data
- Weather observation data
- Sports shadow signals

## Automated Monitoring

| Component | Schedule | Purpose |
|---|---|---|
| `auditor.py` | Hourly (cron) | Deterministic health checks — data freshness, schema integrity, settlement gaps. Telegram alerts on anomalies. |
| `researcher.py` | 3× daily (7:30am, 12:30pm, 7:30pm ET) | Performance reports to Telegram — regime-filtered stats, per-asset breakdown, shadow summaries. |
| `watchdog.py` | Continuous | Process health monitoring |

## Test Suite

1553 tests across 25+ test files covering volatility engine, probability model, calibration engine, execution, fee calculation, config consistency, DB signatures, scan pipeline, ghost fill detection, decided contracts, weather NO-side, weekend discount, low-price shadow, NBBO fallback gates, DC routing priority, and regression tests for past bugs.

## Shadow Mode Features

The system supports shadow mode for experimental features — they compute and log but do not affect live trading decisions:

| Feature | Status | Purpose |
|---|---|---|
| S&P 500 Intraday | Observation | EGARCH + VIX vol model for SPX 15M contracts (KXINXU) — reverted from brief live |
| Weather Temperature | Observation | 82-member NWP ensemble for daily high temperature markets (19 cities) — verdict: NO ALPHA |
| Sports Comeback | Observation | Bayesian LR comeback model across 28 leagues — basketball showing promise (69.2% WR) |
| Hourly Crypto | Observation | Collecting calibration data for hourly markets (75 strikes/event) — CalEngine disabled |
| 15M Shadow Engine | Shadow | A1 RecalibratedEGARCH, A2 LightGBM, A3 EGARCH gating, A4 LateWindow (55-74¢) |
| Kalshi Order Flow | Shadow | Orderbook imbalance, depth velocity, spread convergence signals |
| Sigmoid QLIKE | Shadow | Alternative EGARCH weight via QLIKE improvement ratio |
| Shadow Cal Pipeline | Shadow | No-blend calibration monitoring (was promoted, caused +1.86pp overconfidence) |
| Dip Addon | Shadow | Buy more when ask dips ≥3¢ below entry after fill (50% addon size, 35% total risk cap) |
| SPX HAR-RV | Shadow | HAR-RV shadow strategy for SPX (parallel comparison to EGARCH) |

Promoted features (driving live behavior):
- **EGARCH core vol** — EGARCH(1,1) with Student-t innovations
- **EGARCH blend** — MZ R²-weighted blending of EGARCH vs RK
- **Time-varying RK weights** — adaptive multi-scale RK blending
- **Adaptive jump detection** — percentile-based thresholds per asset
- **Adaptive RK bandwidth** — data-driven H* selection
- **Decided contracts (T1/T1B/T2)** — live overlay for z ≤ -5 (93¢+), z ≤ -4 (95¢+), and z ≤ -3 (93-96¢); 6 expansion shadows collecting data
- **SOL taker-first** — SOL bypasses maker, direct IOC at all STC
- **XRP live** — promoted from shadow at 92¢+ floor with 12% risk cap
- **Price improvement addon** — adds to winning positions on price improvement
- **Weekend edge discount** — live on Sat/Sun (89¢+, STC≤600s, no DC overlap); sub-89¢ and STC>600s remain shadow

---

*Last updated: 2026-04-23T02:34:11Z*
