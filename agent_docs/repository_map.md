# Repository Map (auto-generated)

**DO NOT EDIT MANUALLY.** Regenerate with `make refresh-map` (invokes `scripts/refresh_repo_map.py`).

Generated: 2026-05-11T22:45:17Z (HEAD: `ba46d77`).

## Summary
- Modules: 54 .py files under `bot/`
- Total LOC: 42,390
- Top-level classes: 58
- Public top-level functions: 41 (underscore-prefixed private functions excluded from this map)

Public-surface snapshot is in `tests/contracts/public_api.json` (Pillar 1); this map is the navigation-aid complement.

## Module tree

### `bot/`

  - `__init__.py` (41 LOC)
  - `__main__.py` (58 LOC)
  - `_thread_env.py` (36 LOC)
  - `boot.py` (166 LOC)
  - `constants.py` (1804 LOC)
  - `db_writer_registry.py` (168 LOC)
    - functions: recent_writes, register_write, snapshot_active, tracked_write, unregister_write
  - `executor.py` (5395 LOC)
    - classes: OrderExecutor
  - `kalshi_client.py` (388 LOC)
    - classes: KalshiClient
  - `logger.py` (90 LOC)
    - classes: Logger
  - `main_loop.py` (2208 LOC)
    - classes: MainLoop
  - `models.py` (1274 LOC)
    - classes: EGARCHEstimator, MincerZarnowitzTracker, PositionSizer
    - functions: calculate_fee, calculate_maker_fee, calculate_taker_fee, compute_tv_rk_weights, strategy_to_group
  - `notifier.py` (87 LOC)
    - classes: TelegramNotifier
  - `order_flow.py` (384 LOC)
    - classes: KalshiOrderFlowTracker, OrderFlowEngine
  - `orphan_db_watchdog.py` (245 LOC)
    - functions: detect_orphan_db_holders
  - `runtime_config.py` (52 LOC)
  - `settlement.py` (1275 LOC)
    - classes: SettlementTracker
    - functions: discover_active_windows
  - `state.py` (2774 LOC)
    - classes: StateManager
  - `clients/`
    - `__init__.py` (0 LOC)
  - `engines/`
    - `__init__.py` (66 LOC)
    - `calibration.py` (1209 LOC)
      - classes: CalibrationEngine
    - `probability.py` (316 LOC)
      - classes: ProbabilityEngine
    - `sports_data.py` (477 LOC)
      - classes: LeagueConfig, SportGroupConfig
      - functions: classify_deficit_binary, classify_deficit_tennis, classify_deficit_three_way, classify_strength, classify_time_remaining, get_sport_group_config (+ 1 more)
    - `sports_engine.py` (2281 LOC)
      - classes: BayesianComebackModel, ComebackSignal, ESPNLiveFeed, GameState, KalshiGameMarkets, KalshiSportsDiscovery, PlattCalibrator, SportsEngine
    - `spx_engine.py` (1162 LOC)
      - classes: IntradaySeasonalFilter, MarketHoursGuard, SPXEGARCHEstimator, SPXEngine, SPXPriceFeed, SPXVolatilityEngine
    - `volatility.py` (1056 LOC)
      - classes: VolatilityEngine
    - `weather_engine.py` (1030 LOC)
      - classes: WeatherEngine, WeatherEnsembleFetcher, WeatherProbabilityModel, WeatherWindowDiscovery
  - `feeds/`
    - `__init__.py` (33 LOC)
    - `coinbase.py` (368 LOC)
      - classes: CoinbaseFeed
    - `cross_exchange.py` (377 LOC)
      - classes: CrossExchangeFeed
    - `kalshi.py` (1840 LOC)
      - classes: KalshiFeed
    - `orderbook_schema.py` (28 LOC)
      - classes: OrderbookSchemaError
  - `fetchers/`
    - `__init__.py` (24 LOC)
    - `coinglass.py` (107 LOC)
      - classes: CoinGlassFetcher
    - `deribit.py` (110 LOC)
      - classes: DeribitDVOLFetcher
  - `helpers/`
    - `__init__.py` (18 LOC)
    - `breakers.py` (185 LOC)
    - `cell_blocks.py` (206 LOC)
      - functions: should_block_high_price_stc_band, should_block_high_price_stc_candidate, should_block_sol_bleed_v2_candidate, should_block_sol_taker_lowprice_bleed_candidate, should_block_tm98_highprice_bleed_candidate, should_exclude_weather_no_ticker
    - `derived_features.py` (61 LOC)
      - functions: compute_derived_features
    - `orderbook.py` (72 LOC)
      - functions: best_yes_ask_cents, convert_orderbook_fp
    - `raw_api_journal.py` (47 LOC)
      - functions: append_raw_api_journal
    - `sizing.py` (29 LOC)
      - functions: buffer_sizing_multiplier, get_min_edge
    - `strategy.py` (190 LOC)
      - functions: evaluate_execution_strategy
    - `strings.py` (24 LOC)
      - functions: cents_to_dollars_str, dollars_str_to_cents, fp_str_to_int, int_to_fp_str
    - `time_features.py` (60 LOC)
      - functions: compute_time_regime_features
    - `tm_sweep.py` (142 LOC)
      - functions: tm_compute_contracts, tm_sweep_counterfactual_pnl, tm_sweep_extract_depths
    - `validators.py` (67 LOC)
  - `infra/`
    - `__init__.py` (28 LOC)
    - `capital_allocator.py` (326 LOC)
      - classes: CapitalAllocator, CorrelationRiskManager
    - `circuit_breaker.py` (412 LOC)
      - classes: CircuitBreaker, CircuitBreakerOpen, CircuitBreakerRegistry, State
  - `scanner/`
    - `__init__.py` (9472 LOC)
      - classes: OpportunityScanner
  - `shadows/`
    - `__init__.py` (21 LOC)
    - `fifteenm_shadow.py` (1650 LOC)
      - classes: EGARCHGatingApproach, FifteenMShadowEngine, LateWindowApproach, LightGBMApproach, RecalibratedEGARCHApproach
    - `hourly_alt_shadow.py` (1344 LOC)
      - classes: HARRVShadow, HourlyAltShadowEngine, MarketMakingShadow
      - functions: calculate_shadow_fee
    - `spx_harrv_shadow.py` (1107 LOC)
      - classes: SPXHARRVModel, SPXHARRVShadowEngine
