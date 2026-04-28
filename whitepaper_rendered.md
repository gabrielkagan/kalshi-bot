---
title: "Kalshi Crypto Trading Bot"
subtitle: "Technical Whitepaper"
author: "Gabriel Kagan"
date: "April 2026"
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

This system is an automated trading platform for **Kalshi**, a CFTC-regulated prediction market exchange. The 15-minute cryptocurrency engine is the primary live business, augmented by several adjacent live strategies: decided contracts (high-conviction overlay), late-window momentum, weekend and overnight discount entries, low-price near-expiry, and weather temperature NO-side. Adjacent product engines (S&P 500 intraday, hourly crypto, sports comebacks) collect calibration data in observation mode, with hourly currently kill-switched off after a March incident.

The bot monitors real-time data from multiple sources per vertical, estimates outcome probabilities using domain-specific models (EGARCH volatility for crypto/SPX, NWP ensemble forecasts for weather, Bayesian comeback likelihood for sports), and trades when it identifies a statistical edge over the market price.

## Market Opportunity

Kalshi lists 15-minute crypto contracts around the clock. Each window produces fresh contracts for four assets at multiple strike prices, creating hundreds of tradeable markets per day. Because these are short-duration, binary-outcome instruments, mispricing tends to be small but frequent — an ideal environment for systematic, model-driven trading.

Beyond the live 15M engine, the platform spans four adjacent verticals: S&P 500 intraday (observation, briefly live Mar 17 then reverted on Polygon 403), daily weather temperature across 19 US cities (NO-side LIVE since Apr 11 in 1-contract verification mode; YES-side observation), live sports outcomes across 28 leagues (observation; basketball alpha detected), and hourly crypto (kill-switched Apr 18 after correlated multi-strike losses). Each uses domain-specific models while sharing common edge detection, sizing, and execution infrastructure.

## Strategy in Plain English

1. **Observe** — Continuously stream spot prices from Coinbase and Kraken. Fetch implied volatility from Deribit. Monitor Kalshi's own orderbook via WebSocket.
2. **Estimate** — For every active market, compute the probability that the asset stays above its threshold using EGARCH-conditioned volatility with fat-tailed NIG distributions fitted per asset.
3. **Filter** — Reject markets that are too uncertain, too expensive, or offer insufficient edge after fees.
4. **Size** — Use edge-tiered position sizing with automatic drawdown scaling.
5. **Execute** — Maker-first by default to minimize fees, with three-tier post_only rejection handling, time-aware taker escalation, and direct taker below 180 seconds. SOL bypasses maker entirely (taker-first) and decided contracts route direct taker regardless of STC.
6. **Settle** — Track outcomes via the Kalshi settlements API and log performance for continuous evaluation.

## Key Differentiators

| Differentiator | Description |
|---|---|
| **Multi-exchange intelligence** | Aggregates spot prices from Coinbase and Kraken plus derivatives signals from Deribit, detecting cross-exchange lead-lag patterns before they appear in Kalshi prices |
| **EGARCH-conditioned volatility** | Realized Kernel estimation (Barndorff-Nielsen 2008) with data-adaptive bandwidth, MZ R²-weighted blending, and EGARCH(1,1) conditional volatility — all promoted to live trading |
| **Per-asset NIG distributions** | Normal Inverse Gaussian CDF replaces the generic Student-t, capturing both heavy tails and asymmetry specific to each cryptocurrency |
| **Adaptive execution** | Three-tier post_only rejection handler, maker-first by default with per-asset overrides (SOL taker-first, decided contracts direct taker), time-aware escalation, and direct taker below 180s (data: 7.7% maker fill rate at low STC — direct taker strictly better) |
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

When Deribit implied volatility (DVOL) diverges materially from realized volatility, the system blends in the implied estimate using inverse-variance weighting. This respects the market's forward-looking information during regime changes while anchoring to observed data.

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
- **Model-market discrepancy**: If $p_{cal} > 90\%$ (`DISCREPANCY_PROB`) but the market price is $< 75$¢ (`DISCREPANCY_PRICE`), the market is refused — the model may be missing material information the market has
- **EGARCH/RV divergence clamp**: If the EGARCH-to-realized variance ratio falls outside `[1/3, 3]`, EGARCH is rejected and the engine falls back to RK-only volatility

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

## 3.6 Weather Temperature Engine (NO-side Live, YES-side Observation)

The weather engine trades daily high temperature prediction markets across **19 US cities** using numerical weather prediction (NWP) ensemble forecasts.

> **Status: SPLIT.** NO-side is **LIVE** since 2026-04-11 in 1-contract verification mode (entry zone 36–40¢, STC ≥ 16h, with a 36¢ floor added Apr 20). YES-side remains observation-only — research verdict is "no alpha" on YES: the per-city Gaussian fit materially overestimates YES probability vs. actual outcomes. Per-city CalEngines train on every settlement, with bias correction tracking forecast-vs-actual error per city. The 1-contract NO sizing reflects the verification-mode goal: collect outcome data on the bot's own NO entries (rather than counterfactuals) before any size promotion.

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
| Status | NO-side LIVE (1-contract verification, since Apr 11), YES-side observation |
| NO-side entry price range | 36–40¢ (36¢ floor since Apr 20) |
| YES-side entry price range | 10–99¢ (logging only) |
| NO-side STC requirement | ≥ 16h before close |
| Market blend | 80/20 (model/market) — ensemble is primary signal |
| NO-side fixed size | 1 contract per signal |
| YES-side max risk per trade | 10% (only used by counterfactual sim) |
| Kelly fraction (YES sim) | 0.25 (quarter-Kelly) |
| Poll interval | 15 minutes (weather changes slowly) |
| Cities tracked | 19 |

## 3.7 Sports Comeback Engine (Observation Mode)

The sports engine monitors live games across 28 leagues for Bayesian comeback signals — identifying situations where a pregame favorite is trailing but statistically likely to recover.

> **Research status:** ALPHA DETECTED (per-sport breakdown). Basketball is the clear alpha source — best-robust-filter is the NBA strong-config (pregame ≥60%, price ≤70c, time remaining >85%). Tennis is a drag (negative PnL). Per-sport-group CalEngines are learning in shadow. Overall SPRT has not converged — see `kb/decisions/sports-promotion-criteria.md` for current promotion gates and most-recent counts (refreshed by `researcher.py` 3× daily).

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

| Tier | Z-Score Threshold | Price Range | Sizing | Status |
|---|---|---|---|---|
| T1 | z ≤ -5.0 | 93¢+ | 20% bankroll fixed | LIVE |
| T1B | z ≤ -4.0 | 95¢+ | 20% bankroll fixed | LIVE |
| T2 | z ≤ -3.0 | 93–96¢ | 20% bankroll fixed | LIVE |
| T2-Z25 | z ≤ -2.5 | 93–96¢ | 10% bankroll fixed | LIVE (cut from 20% Apr 21 after a 14d -$95 / 17-trade run) |
| T2-Z2 | z ≤ -2.0 | 93–96¢ | 20% bankroll fixed | SHADOW (re-promotion rejected Apr 22; -$313 / 47-trade history) |

**SOL DC overrides**: SOL DC at ≥97c sized at 5% (vs. default 20%); 95–96c sized at 10%. Below 95¢, the default tier risk applies.

T1B was added based on research showing near-perfect win rate in the -5 < z ≤ -4 zone at 95¢+. T2-Z25 and T2-Z2 extend coverage into shallower z-score zones at 93–96¢ — T2-Z25 promoted live, T2-Z2 returned to shadow after underperforming. All live tiers share a per-window cap of 35% bankroll risk. These are incremental — they add on top of the regular trading pipeline, capturing near-certain outcomes that the standard edge filter might not size aggressively enough.

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
| 15M crypto (main) | 1.0 (full Kelly) | Primary system, most data, well-calibrated |
| Hourly crypto | Fixed 25 contracts (bypasses Kelly) | Currently kill-switched off. `HOURLY_FIXED_CONTRACTS=25` for YES, `HOURLY_DC_CONTRACTS=25` for the DC overlay. `HOURLY_KELLY_FRACTION=0.25` exists but is unused under the fixed-sizing path |
| SPX hourly | 0.125 (eighth-Kelly) | Ultra-conservative; observation only |
| Weather (YES sim) | 0.25 (quarter-Kelly) | YES sim only; NO-side trades 1 contract fixed |
| Decided contracts (T1/T1B/T2) | Fixed 20% risk | Not Kelly-derived — high-conviction near-certain outcomes |
| Decided contracts (T2-Z25) | Fixed 10% risk | Cut from 20% after Apr 21 underperformance |
| SOL DC (≥97¢ / 95–96¢) | Fixed 5% / 10% | SOL-specific overrides reflect tighter edges |
| Weekend / overnight discount | Kelly with 7% floor | Bypasses Kelly when computed size is zero |
| LPNE | 50 contracts fixed | Near-expiry BTC 80–87¢; only with model conviction at the strike |

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

Sizing is scaled against a **rolling 7-day cash high-water mark** (`HWM_LOOKBACK_SECONDS = 7 × 86400`); the cash-only HWM avoids inflating against unrealized DC position value.

| Balance vs. Rolling 7-day HWM | Sizing Adjustment |
|---|---|
| ≥ 85% | Full sizing |
| 75–85% | Half sizing |
| 65–75% | Quarter sizing |
| < 65% | Halt trading |

This creates a geometric de-risking curve that preserves capital during losing streaks. The 7-day window prevents a stale HWM from compressing sizing for weeks after a withdrawal or one-off drawdown.

### Loss-Burst Cooldown

Per-asset 2-hour lockout after any 15M loss. Triggered Apr 11 after data showed loss-clustering on the same asset within 30–120 minutes. Live efficacy is being tracked since deploy; the original deploy-time sim showed positive 30-day counterfactual but that figure is not a forward indicator.

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
- **Drawdown scaling**: Size halved below 85% of the rolling 7-day cash high-water mark, quartered below 75%, halted below 65%
- **Hard limits**: Maximum risk per trade capped at 25% of bankroll

## Market Selection Controls

- **Multi-asset capable**: Can trade multiple assets per 15-minute window
- **Price range guardrails**: Global floor 75–99¢ with per-asset overrides — BTC 88¢ (with LPNE intercepting 80–87¢ near-expiry), ETH 90¢ main tier (plus a 75–79¢ live sub-tier capped at 50 contracts; the 80–89¢ band is rejected by the floor due to negative historical PnL), SOL 86¢, XRP 92¢. Below these floors, win rates are insufficient after fees; above 99¢ offers insufficient reward
- **Price-dependent edge threshold**: Fee-adjusted edge must exceed a price-dependent minimum (0.25% at 80¢ up to 1.0% at 97¢+) after taker fees (worst-case)
- **Scanner uses taker fees**: Every candidate is profitable even if forced to taker execution

## Model Sanity Controls

- **Z-score limit**: Refuse markets where $|z| > 25$ (validated against settlement data: 82 tradeable z-score rejections above 12 were all winners, leading to the raise from 12 → 25)
- **Model-market discrepancy**: If $p_{cal} > 90\%$ but the market price is < 75¢, the market is refused (`DISCREPANCY_PROB`/`DISCREPANCY_PRICE`)
- **EGARCH/RV divergence clamp**: If the EGARCH-to-RV variance ratio falls outside `[1/3, 3]`, EGARCH is rejected and the engine falls back to RK-only volatility
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

All numbers below are auto-regenerated from `state.db` on every push. See `kb/decisions/doc-rewrite-2026-04-26.md` for the methodology behind the live/observation split.

| Metric | Value |
|---|---|
| **Status** | Live trading since February 22, 2026 |
| **Settled trades** | 3,048 (2,833W / 213L / 2 breakeven) |
| **Win rate** | 92.9\% |
| **Live P&L (cumulative)** | $707.05 |
| **Live P&L (Kelly-comparable headline; excludes 1-contract weather + kill-switched hourly)** | $705.94 |
| **Assets** | BTC (88¢+, LPNE 80–87¢), ETH (90¢+ main, 75–79¢ capped sub-tier), SOL (86¢+, taker-first), XRP (92¢+) |

### Performance by Strategy Group

| Strategy group | n | W / L | PnL ($) | Mean entry (¢) |
|---|---|---|---|---|
| 15M main (Kelly-sized) | 1,347 | 1,220 W / 127 L | 639.33 | — |
| Decided contracts | 219 | 211 W / 8 L | -91.38 | — |
| Weekend discount | 124 | 116 W / 8 L | 102.79 | — |
| Overnight discount | 63 | 61 W / 2 L | 60.74 | — |
| LPNE (BTC 80–87¢ near-expiry) | 2 | 2 W / 0 L | 20.00 | — |
| Weather NO (1-contract verification) | 76 | 30 W / 46 L | 1.84 | — |
| Hourly NO (pre-kill-switch) | 14 | 6 W / 8 L | -0.73 | — |

### Calibration

The bot exposes two Brier scores:

- **Brier (all live candidates)** — measures the **model's** calibration on every opportunity that passed the live-candidate filter, whether or not it filled: 0.0443 overall, 0.0315 on 15M, 0.3908 on weather (side-aware: NO-side rows use $1-p_{raw}$ as the model's probability of the bot's bet winning).
- **Brier (filled trades only)** — measures the **bot's paid-decision** calibration via JOIN(settled_trades, latest matching evaluated_opportunities row), deduplicated on stacked tickers and timestamp ties: 0.0550 overall (2,868 samples), 0.0452 on 15M.

A small number of settled trades (3,048 total, of which N lack a matching EO row — see `settled_without_matching_eo` in the auto-generated stats) are excluded from filled-Brier; their model prediction was not preserved in evaluated_opportunities.

### Regime Slices

Two regime cutoffs are pinned to actual deploy commit timestamps:

| Slice | Live PnL ($) | Settled | W / L | Brier (model) |
|---|---|---|---|---|
| Since 2026-04-11T20:43Z (loss-burst cooldown + weather NO live) | 649.56 | 1,226 | 1,131 W / — L | 0.0483 |
| Since 2026-04-23T23:46Z (WS schema fix `0ddcaf8`) | -104.06 | 278 | 248 W / — L | 0.0324 |

The post-Apr-23 slice is the cleanest "current regime" view: WS orderbook depth is now decoded correctly, loss-burst cooldown is shipped, weather NO has been live for 12 days, and XRP has been live at 92¢+ for ~5 days.

### Shadow / Hypothetical PnL

Counterfactual PnL for shadow-only strategies (would-have entered at relaxed gates), summed across all evaluated_opportunities with `counterfactual_pnl IS NOT NULL`: $-146,352.75 across 146,658 signals. These are simulated under the assumption of no fill impact, so they overstate what live promotion would actually capture; treat them as upper bounds when evaluating shadow→live promotions.

## Markets

### Crypto 15-Minute (Live Trading)

Binary contracts settling every 15 minutes. Series: KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M. STC window: scan 0–900s, live 0–600s, shadow observation 600–900s. Several live overlays add incremental volume on top of the main 15M scan:

- **Decided contract overlay** — T1, T1B, T2, T2-Z25 live; T2-Z2 returned to shadow Apr 22 after underperformance (-$313 / 47 trades). Six T1/T2 expansion shadows (T1A, T1B-EXP, T2A, T2B, T3, T3A) collect data for potential future tiers
- **Terminal Momentum (TM)** — trades the final 1–5 minutes at 96/98/99¢ (95 and 97 removed Apr 9 after −$980/2wk on 347 trades). Sizing is `TM_BASE_CONTRACTS=100` × margin × STC multiplier with caps (min 25, max 500); 96¢ is blocked when sourced from NBBO
- **Low-Price Near-Expiry (LPNE)** — intercepts BTC at 80–87¢ in the final 10–120 seconds, 50 contracts fixed, only with model conviction at the strike
- **Weekend / Overnight discount** — relaxed-edge entries during low-liquidity windows. Weekend (Sat/Sun) at 90¢+ STC≤600s; overnight (weekday 04–11 UTC) at 89¢+ STC≤600s; both with no-DC-overlap guards. Sub-floor and STC>600s remain shadow
- **Loss-burst cooldown** — per-asset 2h lockout after any 15M loss (shipped Apr 11 on positive deploy-time sim; live efficacy still accumulating)
- **Universal STC sizing scaler** — contracts ×= 300/STC for any strategy when STC > 300s
- **SOL sub-86¢ gate** — blocks SOL entries at ≤85¢ when STC ≥ 300s (preserves the 86¢ floor at long horizons while allowing late-window flexibility)

### Crypto Hourly (Disabled)

75 strikes per event, settling every hour. **Disabled since 2026-04-18** — `HOURLY_LIVE_ENABLED` and `HOURLY_NO_SIDE_LIVE` env vars (default `0`) must both be flipped to `1` on the VPS to re-enable hourly window discovery and entry. The hourly DC overlay (`HOURLY_DC_ENABLED`, default `1`) remains available but does not fire while hourly windows aren't being scanned. The kill followed two regime issues: (1) Feb 27–28 brief live stint reverted after a -$97 overnight loss from calibration overconfidence and correlated multi-strike exposure; (2) the NO-side BTC 40–54¢ tier reached 53.9% WR (n=1,113, p=0.005) post-correction but was disabled when broader hourly economics turned negative. Hourly CalEngine remains disabled (+44pp overconfident historically); uses T=1.45 temperature scaling when re-enabled. BTC is the only historically viable hourly asset — ETH/SOL structurally unprofitable after fees, XRP fundamentally broken. Series: KXBTCD, KXETHD, KXSOLD, KXXRPD.

### S&P 500 Intraday (Observation Mode)

15-minute binary contracts on the S&P 500 during NYSE regular trading hours. Series: KXINXU. Uses equity-adapted EGARCH with VIX integration and intraday seasonal adjustment. Was briefly promoted to live Mar 17, reverted same day due to Polygon.io 403 errors breaking the primary price feed. Now observation-only with Finnhub as primary fallback. Per-window limits: max 2 positions, 15% risk cap. Eighth-Kelly sizing (0.125), 90¢+ floor, no market blend (CalEngine only).

### Weather Temperature (NO-side Live, YES-side Observation)

Daily high temperature markets across 19 US cities. Bracket and threshold contracts settling based on the observed daily high. Probability from 82-member NWP ensemble (GFS + ECMWF). NO-side has been LIVE since 2026-04-11 in 1-contract verification mode (entry zone 36–40¢, 36¢ floor since Apr 20, STC ≥ 16h before settlement); YES-side remains observation-only (research verdict: Gaussian fit materially overestimates YES probability vs. actual outcomes — which is why NO at 36–40¢ is the profitable side). Per-city CalEngines train on every settlement; bias correction tracks per-city forecast-vs-actual error.

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

Pytest suite spanning the volatility engine, probability model, calibration pipeline, executor, fee calculation, config / market_config consistency, DB signature drift guards, scan pipeline, ghost fill detection, decided contracts, weather NO-side, weekend discount, low-price shadow, NBBO fallback gates, DC routing priority, INSERT↔schema parity, `_shadow_diag` tri-contract guards, product_type enum contract, post-deploy DB-row verification, the whitepaper-stats generator (live/observation split, side-aware Brier, JOIN dedup), and regression tests for every prior incident captured in `kb/failures/`. Real-DB integration patterns (`tmp_path` fixtures, no mocks) are required for any test that touches sizing or settlement.

## Shadow Mode Features

The system supports shadow mode for experimental features — they compute and log but do not affect live trading decisions:

| Feature | Status | Purpose |
|---|---|---|
| S&P 500 Intraday | Observation | EGARCH + VIX vol model for SPX 15M contracts (KXINXU) — reverted from brief Mar 17 live stint |
| Weather YES-side | Observation | YES sim is logged but not entered; Gaussian fit overestimates YES probability vs. actual outcomes (NO-side IS live, see §3.6) |
| Sports Comeback | Observation | Bayesian LR comeback model across 28 leagues — basketball alpha detected (69.2% WR / Fisher p=0.035) |
| Hourly Crypto | DISABLED | Both `HOURLY_LIVE_ENABLED` and `HOURLY_NO_SIDE_LIVE` env-gated off since Apr 18; no signals logged |
| 15M Shadow Engine | Shadow | A1 RecalibratedEGARCH, A2 LightGBM, A3 EGARCH gating, A4 LateWindow |
| Decided Contract expansions | Shadow | T1A, T1B-EXP, T2A, T2B, T3, T3A — six tier expansions (lower price floors / shallower z) |
| T2-Z2 | Shadow | Re-promotion rejected Apr 22 after -$313/47-trade live history |
| Weekend / Overnight discount sub-floor | Shadow | Below-floor and STC>600s remain shadow even though main bands are live |
| Kalshi Order Flow | Shadow | Orderbook imbalance, depth velocity, spread convergence signals |
| Sigmoid QLIKE | Shadow | Alternative EGARCH weight via QLIKE improvement ratio |
| Dip Addon | Shadow | Buy more when ask dips ≥3¢ below entry after fill (50% addon size, 35% total risk cap) |
| SPX HAR-RV | Shadow | HAR-RV shadow strategy for SPX (parallel comparison to EGARCH) |
| Position price monitor | Shadow | Logs live YES bid/ask for held 15M positions via WS for exit-signal research |

Promoted features (driving live behavior):

- **EGARCH core vol** — EGARCH(1,1) with Student-t innovations, MLE-fitted on 10,800 samples, refitted every 2 hours
- **EGARCH blend** — MZ R²-weighted blending of EGARCH vs RK
- **Time-varying RK weights** — adaptive multi-scale RK blending (1m / 5m / 15m, weights vary with seconds-to-close)
- **Adaptive jump detection** — percentile-based thresholds per asset (replaced fixed 3-sigma)
- **Adaptive RK bandwidth** — data-driven H* selection
- **Decided contracts (T1, T1B, T2, T2-Z25)** — live overlay; T2-Z25 sized at 10%, others at 20%; SOL DC has separate 5% / 10% tiers at ≥97¢ / 95–96¢
- **SOL taker-first** — SOL bypasses maker, direct IOC at all STC (data: SOL maker fills suffered adverse selection)
- **XRP 15M live** — promoted from shadow at 92¢+ floor with 15% per-trade risk cap (data: 41W/2L = 95.3% WR at ≥92¢)
- **ETH 75–79¢ sub-tier** — live with a 50-contract position cap (the 80–89¢ band remains rejected by the floor)
- **Weekend discount** — live Sat/Sun at 90¢+, STC≤600s, no-DC-overlap guard; sub-floor and STC>600s remain shadow
- **Overnight discount** — live weekday 04–11 UTC at 89¢+, STC≤600s, no-DC-overlap guard
- **Low-Price Near-Expiry (LPNE)** — live on BTC at 80–87¢ with STC 10–120s, 50 contracts fixed, model-conviction gated
- **Loss-burst cooldown** — per-asset 2h lockout after any 15M loss
- **Weather NO-side** — live in 1-contract verification mode (NO 36–40¢, STC≥16h)
- **Price improvement addon** — adds to winning positions on price improvement (50% addon size, 35% total risk cap)
- **STC sizing scaler** — universal contracts ×= 300/STC for any strategy at STC > 300s
- **Low-STC sizing cap** — 50% of computed size when STC < 100s

---

*Last updated: 2026-04-28T13:08:45Z*
