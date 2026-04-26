# bot.py Layout

Approximate line ranges. May drift — `grep -n 'class \|^def ' bot.py` to verify before quoting line numbers.

| Lines | Component | Notes |
|---|---|---|
| 1–600 | Imports, constants, config | Trading params, API config, vol engine, calibration, sizing, execution |
| 600–620 | Utility functions | FP/dollar string helpers |
| 620–815 | `evaluate_execution_strategy()` | Diagnostic only, does NOT control execution |
| 820–1085 | `KalshiClient` | API wrapper, order management, orderbook fetching |
| 1089–1150 | `Logger` | JSONL trade/event logging |
| 1152–1185 | `TelegramNotifier` | Telegram alerts |
| 1188–2480 | `StateManager` | DB init (`_create_tables` at 1209), positions, settlement |
| 2483–2635 | `CoinbaseFeed` | WebSocket price feed, OHLCV snapshots |
| 2645–2970 | `KalshiFeed` | WebSocket orderbook stream, fill detection |
| 2977–3050 | `DeribitDVOLFetcher` | Implied volatility index |
| 3058–3370 | `CrossExchangeFeed` | Kraken, Binance, Bybit order flow |
| 3376–3450 | `CoinGlassFetcher` | Derivative funding rates |
| 3453–3730 | `OrderFlowEngine` + `KalshiOrderFlowTracker` | Kalshi OB flow signals |
| 3736–4640 | `VolatilityEngine` | RK, GARCH, RV estimation, TV RK weights |
| 4643–4805 | `ProbabilityEngine` | Z-score, NIG CDF, market blend |
| 4809–5670 | `CalibrationEngine` | Beta/Platt/isotonic, `_CAL_REGISTRY`, shadow pipeline |
| 5674–9844 | `OpportunityScanner` | `scan()` at 5904, market discovery, filter pipeline, shadow signals |
| 9848–12220 | `OrderExecutor` | `execute()` at 9945, maker→taker escalation, fill detection |
| 12223–12980 | `SettlementTracker` | Settlement detection, CalEngine routing, PnL computation |
| 12983–13065 | `discover_active_windows()` | Market discovery from Kalshi API |
| 13068–14030 | `MainLoop` | `run()` at 13902, init, WS subscription, periodic tasks |
| 14030–14043 | Entry point | |

## Project Structure

- `bot.py` — Main bot (all trading logic, ~23.5K lines)
- `analyst.py` — AI analyst (news sentiment, loss analysis, Telegram alerts)
- `spx_engine.py` — SPX hourly engine (Polygon.io, EGARCH, RK, VIX)
- `weather_engine.py` — Weather ensemble fetcher + probability model (Open-Meteo GFS/ECMWF)
- `sports_engine.py` — Sports comeback engine (ESPN live data, Bayesian posterior)
- `market_config.py` — Centralized MarketTypeConfig (asserts against bot.py at startup)
- `dashboard_snapshot.py` — Dashboard state snapshots (Supabase syncer)
- `fifteenm_shadow.py` — 15M shadow strategies (A1/A2/A3/A4)
- `hourly_alt_shadow.py` — Hourly alt shadow strategies (HAR-RV, market-making sim)
- `auditor.py` — Hourly cron health checks → Telegram
- `researcher.py` — 3x daily performance reports → Telegram
- `start.sh` — Startup script (venv + .env + bot.py)
- `.github/workflows/deploy.yml` — Auto-deploy to VPS on push to main
