# Kalshi Crypto Trading Bot

Automated trading platform for Kalshi prediction markets. The core engine trades 15-minute cryptocurrency contracts (BTC, ETH, SOL, XRP) live, with several adjacent strategies layered on top: decided contracts, late-window momentum, weekend/overnight discounts, and a near-expiry low-price entry. Adjacent products (S&P 500 intraday, daily weather temperature across 19 US cities, and live sports outcomes across 28 leagues) run in observation or 1-contract verification mode while their CalEngines train.

## How It Works

```
Coinbase (1s prices) ──┐
Kraken ────────────────┤                                          ┌─ Maker post_only (no fee)
Bybit ─────────────────┼──→ Volatility ──→ Probability ──→ Edge ──┤  ↓ if unfilled or near-close
Binance (geo-blocked)──┤     Engine          Engine      Filter   └─ Cancel-replace IOC taker
Deribit DVOL ──────────┤
CoinGlass funding ─────┘
```

Every second the bot scans all active 15-minute windows across the four crypto assets. Edge thresholds are price-dependent and per-asset. The global edge floor is V-shaped: it relaxes from 0.25% (80--90c) to a low of 0.20% at 91--92c, then climbs back up to 0.5% (93--94c), 0.75% (95--96c), and 1.0% at 97c+. The 91--92c trough reflects empirical tightness in that price band; very-high prices need the larger edge to absorb fee drag and time risk. On top of that, each asset has its own minimum entry price (BTC 88c+, SOL 86c+, XRP 92c+) and ETH operates with a main tier at 90c+ plus a sub-80c live tier capped at 50 contracts (the 80--89c band is blocked due to negative historical PnL).

## Architecture

### Volatility Engine

The engine blends three Realized Kernel estimators (Barndorff-Nielsen 2008, Parzen flat-top kernel) with data-adaptive bandwidth selection. Baseline weights are 50% / 30% / 20% on 1-min / 5-min / 15-min, but in production the weights are **time-varying** as a function of seconds-to-close --- shorter horizons get more weight as the window approaches expiry.

| Estimator | Baseline weight | Purpose |
|-----------|----------------|---------|
| 1-min realized kernel | 50% | Current microstructure |
| 5-min bipower variation | 30% | Jump-robust medium-term vol |
| 15-min realized kernel | 20% | Window-level baseline |

On top of that:

- **Adaptive RK bandwidth (H\*)** --- bandwidth auto-tunes from the noise-to-signal ratio, producing tighter estimates in calm periods and wider smoothing during noisy periods
- **Mincer-Zarnowitz R²-weighted EGARCH blending** --- an EGARCH(1,1) model with Student-t innovations runs live, blending with RK vol weighted by the MZ regression R². EGARCH/RV ratios outside `[1/3, 3]` are rejected
- **Deribit DVOL integration** --- when IV diverges from RV materially, the engine shifts toward implied vol via inverse-variance weighting. SOL and XRP have no direct DVOL feed, so BTC DVOL is scaled by a rolling cross-asset beta
- **Adaptive jump detection** --- per-asset percentile thresholds (replaced the fixed 3-sigma rule) with EWMA variance tracking and tiered response scaling

### Probability Model

Converts the volatility estimate into a settlement probability:

1. Compute z-score: distance from current price to strike, normalized by estimated vol
2. Map through a per-asset Normal Inverse Gaussian (NIG) CDF --- captures heavy tails and asymmetry; falls back to Student-t(df=4) if NIG isn't available
3. Data-driven calibration via per-product CalEngines. The 15M engine currently runs in **passthrough mode** (raw probability has lower Brier than the BLR fit, so the BLR layer is bypassed); per-city weather, per-sport-group, and SPX-D engines run their full Platt → Beta → BLR pipeline
4. Dynamic probability cap: bypassed when learned calibration is active (uses 0.999 safety ceiling); cap schedule applies during startup before training
5. Market-price blending: 60% model / 40% market-implied probability for 15M; weather/SPX use product-specific weights

Hard safety rails: refuse to trade if `|z-score| > 25` or if the EGARCH/RV ratio falls outside `[1/3, 3]`.

### Cross-Exchange Intelligence

Three WebSocket feeds (Kraken, Bybit, Binance) run concurrently via `CrossExchangeFeed` to detect directional signals before they show up on Kalshi. Binance is geo-blocked (HTTP 451) on the production VPS but the feed reconnects silently; Kraken and Bybit provide the primary cross-exchange signal.

- **Lead/lag consensus** --- if 3+ exchanges move >0.3% in the same direction, probability gets a +2pp adjustment (or -2pp if they oppose)
- **Single-exchange lead** --- a >0.2% move on one exchange adds ±1pp
- **Funding rate signal** --- extreme funding (≥0.05%/8h via CoinGlass) reduces probability by up to 1.5pp as a contrarian dampener; elevated funding (≥0.03%/8h) is -0.5pp

Total cross-exchange adjustment is capped at ±3pp.

### Execution Strategy

Maker-first by default, but with several asset- and product-specific overrides. Taker fills are allowed at any seconds-to-close (`MAKER_ONLY_THRESHOLD = 0`). The decision tree:

1. **Default 15M (BTC, ETH, XRP)** --- place maker `post_only=True` (no maker fee), poll for fills via Kalshi WebSocket. If unfilled after the per-tier wait (15s for ≥180s STC, 7s for 120--180s, 5s for 60--120s), `amend_order()` to taker price; fall back to cancel + IOC taker if amend rejects
2. **SOL** --- `SOL_TAKER_FIRST = True`. Skip maker entirely, go direct IOC at all STC (data: SOL maker fills suffered adverse selection; taker-first net positive)
3. **Direct taker zone** --- when STC < 180s, all assets skip maker and submit IOC directly
4. **Decided contracts (T1, T1B, T2, T2-Z25)** --- route direct taker regardless of STC; structural high-conviction signals
5. **Three-tier post_only rejection handler** --- normal → degraded → taker IOC after 3+ rejections

Fee schedule: maker = **free**. Taker = `ceil(0.07 * C * P * (1-P))` --- ceil on total, not per contract.

### Position Sizing

Edge-tiered Kelly sizing as the baseline, with several overrides:

| Fee-adjusted edge | Risk fraction |
|-------------------|---------------|
| ≥ 4% | 25% of bankroll |
| ≥ 2.5% | 20% of bankroll |
| ≥ 1.8% | 15% of bankroll |
| ≥ 1.2% | 10% of bankroll |
| ≥ 0.9% | 7% of bankroll |
| ≥ 0.7% | 5% of bankroll |
| ≥ 0.5% | 3% of bankroll |
| ≥ 0.25% | 2% of bankroll |

Per-asset hard caps (lower than the global 25% ceiling):

- BTC: 15%, ETH: 20%, SOL: 15%, XRP: 15%

Per-strategy fixed sizing (overrides Kelly):

- **Decided contracts T1 / T1B / T2** --- 20% of bankroll, fixed; 35% per-window cap
- **Decided contract T2-Z25** --- 10% (cut from 20% Apr 21 after a 14d / 17-trade losing run)
- **SOL decided-contract overrides** --- SOL DC at ≥97c sized at 5%, 95--96c at 10% (below the default 20%)
- **Weather NO-side** --- 1 contract per signal
- **Low-price near-expiry (LPNE, BTC 80--87c)** --- 50 contracts fixed, only with model conviction at the entry strike
- **Overnight LP variant** --- 10% max per trade (vs 25% live ceiling)
- **Universal STC sizing scaler** --- contracts ×= 300/STC for any strategy when STC > 300s
- **Low-STC sizing cap** --- 50% of computed size when STC < 100s

Drawdown scaling (driven by a rolling 7-day high-water mark, cash balance only):

- At 85% of HWM: halve position sizes
- At 75% of HWM: quarter sizes
- At 65% of HWM: halt trading entirely

Loss-burst cooldown: per-asset 2-hour lockout after any 15M loss.

### State & Persistence

SQLite (WAL mode) stores positions, pending orders, settled trades, GARCH parameters, evaluated and rejected opportunities, plus per-product calibration observations. On startup the bot reconciles local state against the Kalshi API --- API always wins.

## Data Sources

| Source | Transport | Data | Frequency |
|--------|-----------|------|-----------|
| Coinbase | WebSocket | BTC, ETH, SOL, XRP spot prices | 1s snapshots (300-sample buffer) |
| Kraken | WebSocket | Spot prices for lead/lag detection | Real-time |
| Bybit | WebSocket | Spot prices for lead/lag detection | Real-time |
| Binance | WebSocket | Spot prices (geo-blocked on VPS) | Real-time when reachable |
| Deribit | REST | DVOL implied volatility index | Every 60s (120s cache) |
| CoinGlass | REST | Funding rates | Every 10min (100 calls/day budget) |
| Kalshi | REST + WebSocket | Markets, orderbooks, positions, settlements, fills | 1s scan loop + real-time WS |

## Live Stats

<!-- Auto-updated by GitHub Actions from VPS state.db -->

| Metric | Value |
|--------|-------|
| Markets evaluated | 174,086 |
| Observation period | 2026-02-22 to 2026-05-04 |
| Filter pass rate | 3.9\% (6,800 of 174,086) |
| Top rejection reason | Insufficient Edge (53,524) |
| Settled trades | 3,417 (3,168 W / 247 L / 2 BE) |
| Win rate | 92.7\% |

*Last updated: 2026-05-04T14:16:44Z*

## Live vs Observation

The bot runs multiple product engines in parallel, with different live/observation states:

- **15M crypto (BTC, ETH, SOL, XRP)** --- LIVE (XRP gated to 92c+, ETH to 90c+ main path with a separate 75--79c capped sub-tier)
- **Decided contracts (T1, T1B, T2, T2-Z25)** --- LIVE on top of 15M
- **Late-window momentum (terminal_momentum at 96/98/99c)** --- LIVE
- **Weekend / overnight discount entries** --- LIVE in restricted price/STC zones
- **Low-price near-expiry (LPNE, BTC 80--87c)** --- LIVE
- **Weather NO-side (19 cities)** --- LIVE at 1 contract per signal
- **Hourly crypto** --- DISABLED (kill-switched April 18)
- **SPX intraday** --- observation only (CalEngine training)
- **Sports outcomes (28 leagues)** --- observation only
- **15M shadow variants (recalibrated EGARCH, LightGBM, late-window, etc.)** --- shadow only; see whitepaper for detail

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
- `HOURLY_LIVE_ENABLED=1` / `HOURLY_NO_SIDE_LIVE=1` --- hourly is disabled by default; both env vars must be set on the VPS to re-enable

### Run

```bash
source .env
python3 bot.py
```

Set `OBSERVATION_MODE = True` in `bot.py` to log everything but place no orders.

## Deployment

Runs as a systemd service (`kalshi-bot`) on a DigitalOcean droplet. Pushing to `main` auto-deploys via GitHub Actions:

1. SSH into VPS as `botuser`
2. `git pull origin main`
3. Syntax-check `bot.py` (`python3 -c "import ast; ast.parse(...)"`)
4. `sudo systemctl restart kalshi-bot`

## Kalshi API Notes

- **Auth**: RSA-PSS signature --- the signing path must include the `/trade-api/v2` prefix
- **Orderbook**: returns separate YES and NO orderbooks. Market NBBO provides `yes_ask`, `yes_bid`, `no_ask`, `no_bid`. YES + NO prices do **not** always sum to 100
- **Order type**: all orders are limit orders (no market orders as of Feb 2026)
- **Settlements**: bot uses the settlements API for outcome detection, never z-score heuristics or balance deltas
- **Fee formula**: maker = **free**; taker = `ceil(0.07 * C * P * (1-P))` --- ceil on the total, not per contract

## Project Structure

```
bot.py                         -- core bot logic (~27,713 lines, never rename)
config.py                      -- centralized SIZING_TIERS / DRAWDOWN_* / MIN_EDGE_BY_PRICE
models.py                      -- EGARCH / Mincer-Zarnowitz / PositionSizer / fee math
analyst.py                     -- AI analyst (news sentiment, loss analysis, Telegram alerts)
market_config.py               -- centralized MarketTypeConfig (validates against bot.py at startup)
fifteenm_shadow.py             -- 15M shadow engine (recalibrated EGARCH + LightGBM research)
spx_engine.py                  -- S&P 500 intraday engine (EGARCH + VIX, observation mode)
weather_engine.py              -- weather temperature engine (NWP ensemble, NO-side live + observation)
sports_engine.py               -- sports comeback engine (Bayesian LR, observation mode)
sports_data.py                 -- sports LR tables and league configuration
capital_allocator.py           -- capital allocation across product types
circuit_breaker.py             -- per-asset trading halt logic
dashboard_snapshot.py          -- builds dashboard state snapshots for Supabase
supabase_sync.py               -- pushes snapshots to Supabase Realtime every 10s
watchdog.py                    -- process health monitoring
start.sh                       -- systemd entrypoint (venv + .env + bot.py)
requirements.txt               -- Python dependencies
.env.example                   -- credential template
.github/workflows/deploy.yml   -- auto-deploy on push to main
.github/workflows/whitepaper.yml -- auto-generate README stats + whitepaper PDFs
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
