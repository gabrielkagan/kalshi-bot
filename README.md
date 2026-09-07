# Kalshi Crypto Trading Bot

Automated trading platform for Kalshi prediction markets. The core engine trades 15-minute cryptocurrency contracts (BTC, ETH, SOL, XRP, HYPE, DOGE, BNB — all seven live post P2.4 promotion 2026-05-19; HYPE/DOGE T4 was P2.3 2026-05-14; BNB T4 was P2.4 2026-05-19) live, with several adjacent strategies layered on top: decided contracts, late-window momentum, weekend/overnight discounts, and a near-expiry low-price entry. Adjacent products (S&P 500 intraday, daily weather temperature across 19 US cities, and live sports outcomes across 28 leagues) run in observation or 1-contract verification mode while their CalEngines train.

## How It Works

```
Coinbase (1s prices) ──┐
Kraken ────────────────┤                                          ┌─ Maker post_only (no fee)
Bybit ─────────────────┼──→ Volatility ──→ Probability ──→ Edge ──┤  ↓ if unfilled or near-close
Binance (geo-blocked)──┤     Engine          Engine      Filter   └─ Cancel-replace IOC taker
Deribit DVOL ──────────┤
CoinGlass funding ─────┘
```

Every second the bot scans all active 15-minute windows across the seven live crypto assets. Edge thresholds are price-dependent and per-asset. The global edge floor is V-shaped: it relaxes from 0.25% (80--90c) to a low of 0.20% at 91--92c, then climbs back up to 0.5% (93--94c), 0.75% (95--96c), and 1.0% at 97c+. The 91--92c trough reflects empirical tightness in that price band; very-high prices need the larger edge to absorb fee drag and time risk. On top of that, each asset has its own minimum entry price (BTC 88c+, SOL 86c+, XRP 92c+, HYPE 90c+, DOGE 85c+, BNB 90c+) and ETH operates with a main tier at 90c+ plus a sub-80c live tier capped at 50 contracts (the 80--89c band is blocked due to negative historical PnL).

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
- **Deribit DVOL integration** --- when IV diverges from RV materially, the engine shifts toward implied vol via inverse-variance weighting (BTC and ETH only --- the only assets with a public IV index). Non-BTC/ETH assets carry no implied vol and rely on the realized-kernel/EGARCH estimate alone; the former beta-scaled BTC-DVOL proxy was removed in 2026-06 after it was found to systematically understate alt volatility
- **Adaptive jump detection** --- per-asset percentile thresholds (replaced the fixed 3-sigma rule) with EWMA variance tracking and tiered response scaling

### Probability Model

Converts the volatility estimate into a settlement probability:

1. Compute z-score: distance from current price to strike, normalized by estimated vol
2. Map through a per-asset Normal Inverse Gaussian (NIG) CDF --- captures heavy tails and asymmetry; falls back to Student-t(df=4) if NIG isn't available
3. Data-driven calibration via per-product CalEngines. The 15M engine currently runs in **passthrough mode** (raw probability has lower Brier than the BLR fit, so the BLR layer is bypassed); per-city weather, per-sport-group, and SPX-D engines run their full Platt → Beta → BLR pipeline
4. Dynamic probability cap: bypassed when learned calibration is active (uses 0.999 safety ceiling); cap schedule applies during startup before training
5. Market-price blending: per-asset 15M weights — BTC 10%, ETH 20%, SOL 80%, XRP 90% (cal_mlp v1.1 4×6 sim-PnL sweep, P2.1.d 2026-05-13); HYPE 80%, DOGE 60% (B.1 Brier sweep on shadow data, P2.3 live promotion 2026-05-14); BNB 20% (B.1-equivalent Brier sweep on shadow data n=721, P2.4 live promotion 2026-05-19 — argmin matches ETH pattern, raw model beats market by ~10% Brier). Weather/SPX use product-specific weights. Canonical lockstep: `MARKET_BLEND_W_BY_ASSET = {BNB:0.20,BTC:0.10,DOGE:0.60,ETH:0.20,HYPE:0.80,SOL:0.80,XRP:0.90}` (doc-drift contract).

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
| Coinbase | WebSocket | BTC, ETH, SOL, XRP, HYPE, DOGE, BNB spot prices | 1s snapshots (300-sample buffer) |
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
| Markets evaluated | 622,470 |
| Observation period | 2026-02-22 to 2026-09-07 |
| Filter pass rate | 1.8\% (11,267 of 622,470) |
| Top rejection reason | Insufficient Edge (163,479) |
| Settled trades | 5,683 (5,285 W / 396 L / 2 BE) |
| Win rate | 93.0\% |

*Last updated: 2026-09-07T00:21:13Z*

## Live vs Observation

The bot runs multiple product engines in parallel, with different live/observation states:

- **15M crypto (BTC, ETH, SOL, XRP, HYPE, DOGE, BNB)** --- LIVE (XRP gated to 92c+, ETH to 90c+ main path with a separate 75--79c capped sub-tier; HYPE 90c+, DOGE 85c+ post P2.3 promotion 2026-05-14; BNB 90c+ post P2.4 promotion 2026-05-19)
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
python3 -m bot
```

Set `OBSERVATION_MODE = True` in `bot/constants.py` to log everything but place no orders.

## Deployment

Runs as a systemd service (`kalshi-bot`) on a DigitalOcean droplet. Pushing to `main` auto-deploys via GitHub Actions:

1. SSH into VPS as `botuser`
2. `git pull origin main`
3. Syntax-check runtime hotspots via `make ast-check` (scans `bot/constants.py` + `bot/main_loop.py` + `bot/scanner/__init__.py`)
4. `sudo systemctl restart kalshi-bot`

## Kalshi API Notes

- **Auth**: RSA-PSS signature --- the signing path must include the `/trade-api/v2` prefix
- **Orderbook**: returns separate YES and NO orderbooks. Market NBBO provides `yes_ask`, `yes_bid`, `no_ask`, `no_bid`. YES + NO prices do **not** always sum to 100
- **Order type**: all orders are limit orders (no market orders as of Feb 2026)
- **Settlements**: bot uses the settlements API for outcome detection, never z-score heuristics or balance deltas
- **Fee formula**: maker = **free**; taker = `ceil(0.07 * C * P * (1-P))` --- ceil on the total, not per contract

## Project Structure

```
bot/main_loop.py               -- MainLoop class body (Bit 9.3, 2026-05-10; core orchestration)
bot/scanner/__init__.py        -- OpportunityScanner class body (Bit 8.1, 2026-05-10)
bot/executor.py                -- OrderExecutor class body (Bit 9.1, 2026-05-10)
bot/state.py                   -- StateManager class body (Bit 7.1, 2026-05-10)
bot/settlement.py              -- SettlementTracker + discover_active_windows (Bit 9.2)
bot/constants.py               -- ~120 module-level constants (Bit 3.1)
bot/runtime_config.py          -- PEP 562 dual-probe of bot.constants → bot.config (Bit 9.3-iii.c, retargeted in Bit 12.1)
bot/boot.py                    -- boot-time bindings + cal_mlp warmup (Bit 9.3-iii.a)
bot/config.py                  -- centralized SIZING_TIERS / DRAWDOWN_* / EGARCH bounds / MAX_RISK_PER_TRADE (Bit 12.1, 2026-05-12 — relocated from repo root)
bot/models.py                  -- EGARCH / Mincer-Zarnowitz / PositionSizer / fee math  (Sprint 10.5b relocation, 2026-05-11)
bot/ai/analyst.py              -- AI analyst (news sentiment, loss analysis, Telegram alerts)  (Sprint 10.3 relocation, 2026-05-12)
bot/ai/auditor.py              -- hourly deterministic health checks → Telegram alerts  (Sprint 10.3 relocation, 2026-05-12)
bot/ai/researcher.py           -- 3×/day performance reports → Telegram  (Sprint 10.3 relocation, 2026-05-12)
market_config.py               -- centralized MarketTypeConfig (validates against bot.constants at startup; Bit 9.3-iii.c deleted bot/_impl.py)
bot/shadows/fifteenm_shadow.py -- 15M shadow engine (recalibrated EGARCH + LightGBM research)  (Sprint 10.2 relocation, 2026-05-11)
bot/engines/spx_engine.py      -- S&P 500 intraday engine (EGARCH + VIX, observation mode)  (Sprint 10.1b relocation, 2026-05-11)
bot/engines/weather_engine.py  -- weather temperature engine (NWP ensemble, NO-side live + observation)  (Sprint 10.1c relocation, 2026-05-11)
bot/engines/sports_engine.py   -- sports comeback engine (Bayesian LR, observation mode)  (Sprint 10.1d relocation, 2026-05-11)
bot/engines/sports_data.py     -- sports LR tables and league configuration  (Sprint 10.1a relocation, 2026-05-11)
bot/infra/capital_allocator.py -- capital allocation across product types  (Sprint 10.5a relocation, 2026-05-11)
bot/infra/circuit_breaker.py   -- per-asset trading halt logic  (Sprint 10.5a relocation, 2026-05-11)
bot/snapshots/dashboard_snapshot.py            -- builds dashboard state snapshots for Supabase  (Sprint 10.4 relocation, 2026-05-12)
bot/snapshots/bot_state_snapshot.py            -- bot microstate forward-capture helper  (Sprint 10.4 relocation, 2026-05-12)
bot/snapshots/market_observations_snapshotter.py -- NBBO continuous snapshotter  (Sprint 10.4 relocation, 2026-05-12)
bot/snapshots/supabase_sync.py                 -- pushes snapshots to Supabase Realtime every 10s  (Sprint 10.4 relocation, 2026-05-12)
ops/watchdog.py                -- process health monitoring (Sprint 14-A Bit X.5 relocation, 2026-05-17)
ops/kalshi-bot.service         -- systemd unit, source of truth (installed via ops/install.sh)
start.sh                       -- wrapper invoked by ops/kalshi-bot.service (venv + .env + `python -m bot`)
collector/                     -- Data Corpus collector (top-level SIBLING to bot/, D1.1 SHIPPED 2026-05-16, ticket 86b9ypn49)
collector/__main__.py          -- entrypoint shim, mirrors bot/__main__.py sacred-boundary rule (`python -m collector`)
collector/{main_loop,ws_connection,rest_snapshot,writer,uploader,subscription_manager}.py
                               -- scaffolded at D1.1; ws_connection.py rewired to consume kalshi_wire at D1.1.5 (auth.py DELETED, auth flows through kalshi_wire.auth); **D1.2 SHIPPED 2026-05-16 (ticket 86b9ypn66)**: writer.py + uploader.py + main_loop.py body + ws_connection.py BronzeArchiver.run() body wired (WS → JSONL.zst → S3 via rclone copyto). **D1.3 SHIPPED 2026-05-16 (ticket 86b9ypn72)**: subscription_manager.py body (tier-aware per-conn ticker assignment + subscribe-frame batching) + BronzeArchiver.on_session_start dispatch + sid→channel mapping from subscribe-acks + main_loop multi-conn fan-out (channel-aware writers_by_channel dispatch); first-bronze-flow lands here. **D1.4 SHIPPED 2026-05-16 (ticket 86b9ypn8r)**: rest_snapshot.py body (Kalshi REST /markets?status=open paginated fetch via kalshi_wire.auth.make_rest_headers + RestSnapshotRefresher hourly poll + BronzeArchiver.update_subscriptions / request_reconnect + main_loop._replan_for_archivers); hourly REST refresh is now the default ticker source. **D1.5 SHIPPED 2026-05-16 (ticket 86b9ypna4, REQUIRES-APPROVAL)**: ops/kalshi-collector.service systemd unit + ops/install.sh extended to multi-unit installer + collector-start.sh body sources dedicated /home/botuser/.env.collector (isolation strengthened beyond D1.1 stub's shared bot .env). Bronze day-zero = first-chunk-in-S3 timestamp after `systemctl start kalshi-collector`
kalshi_wire/                   -- shared Kalshi WS transport library (top-level SIBLING to bot/ and collector/, D1.1.5 SHIPPED 2026-05-16, ticket 86b9zdhz2; ZERO bot.* / collector.* imports; consumed by both)
kalshi_wire/{auth,ws_client}.py
                               -- auth.py: RSA-PSS-SHA256 sign + REST/WS header helpers; ws_client.py: WSClient (asyncio thread + connect/reconnect + silence watchdog + send queue) + Frame dataclass + build_envelope() (D0.3 §2 6-field bronze envelope)
collector-start.sh             -- wrapper invoked by ops/kalshi-collector.service (D1.5 SHIPPED; parallel to start.sh; sources /home/botuser/.env.collector; `python -m collector`)
ops/kalshi-collector.service   -- D1.5 systemd unit (SHIPPED 2026-05-16, ticket 86b9ypna4; Nice=10, MemoryHigh=400M, MemoryMax=512M, dedicated EnvironmentFile=/home/botuser/.env.collector — CPUAffinity retired 2026-05-19 ticket 86ba12rv6; MemoryHigh added 2026-05-20 ticket 86ba12rf0)
requirements.txt               -- Python dependencies
.env.example                   -- credential template
.github/workflows/deploy.yml   -- auto-deploy on push to main
.github/workflows/whitepaper.yml -- auto-generate README stats + whitepaper PDFs
```

The `collector/`, `kalshi_wire/`, and `coinbase_wire/` packages have ZERO `bot.*` imports — structural bot-isolation contract enforced by the `collector-no-bot` + `kalshi_wire-no-bot` + `kalshi_wire-no-collector` + `coinbase_wire-no-bot` + `coinbase_wire-no-collector` `.importlinter` forbidden contracts (10 total). Off-switch in either direction (`systemctl stop kalshi-{bot,collector}`) leaves the other unaffected. The 2026-05-16 §5 AMENDMENT to `kb/decisions/data-corpus-architecture.md` adopted the "two sides of the same coin" shared-transport shape (`kalshi_wire/`, mirrored by `coinbase_wire/` at D2.1.5) after external-advisor feedback that capture + replay must share the same wire-level transport so bronze is reusable; post P1-B-brutalist Phase B1 (2026-05-20) the collector opts into `WSClient(parse_on_demand=True)` and skips the per-frame `json.loads` for throughput at high-ticker scale, but `Frame.raw` remains byte-identical across bot and collector (pinned by `tests/equivalence/test_kalshi_wire_differential.py`).

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

When `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` are set, `bot/snapshots/supabase_sync.py` pushes a state snapshot every 10 seconds to the `dashboard_state` table: balance, active positions, recent trades, win/loss record, current volatility readings, order flow signals, and session stats. The dashboard is a static HTML page hosted on GitHub Pages, reading from Supabase Realtime.
