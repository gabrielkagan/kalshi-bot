"""kalshi-bot package — runtime body in extracted submodules.

Post-Bit-9.3-iii.b (2026-05-11) the `_BotProxy` ModuleType subclass that
re-routed `bot.X` attribute access to `bot._impl.X` has been retired.
Reach public names directly from their canonical submodules:

| Name | Canonical submodule |
|---|---|
| MainLoop | `bot.main_loop` |
| OpportunityScanner | `bot.scanner` |
| OrderExecutor | `bot.executor` |
| SettlementTracker, discover_active_windows | `bot.settlement` |
| OrderFlowEngine, KalshiOrderFlowTracker | `bot.order_flow` |
| detect_orphan_db_holders, _alert_orphan_db_holder, _ORPHAN_DB_WATCHDOG_PATTERNS | `bot.orphan_db_watchdog` |
| StateManager | `bot.state` |
| TelegramNotifier, _TELEGRAM | `bot.notifier` |
| Logger | `bot.logger` |
| KalshiClient | `bot.kalshi_client` |
| VolatilityEngine, ProbabilityEngine, CalibrationEngine | `bot.engines` |
| DeribitDVOLFetcher, CoinGlassFetcher | `bot.fetchers` |
| CoinbaseFeed, CrossExchangeFeed, KalshiFeed, OrderbookSchemaError | `bot.feeds` |
| EGARCHEstimator, MincerZarnowitzTracker, PositionSizer, calculate_fee, calculate_taker_fee, calculate_maker_fee, compute_tv_rk_weights, strategy_to_group | `bot.models` |
| _HPSB_MISSING_BLEEDERS, _HPSB_VALIDATOR_UNAVAILABLE_REASON, _BLEED_BLOCK_MISSING_BLEEDERS, compute_for_15m_main_path | `bot.boot` |
| ~120 module-level constants (OBSERVATION_MODE, MIN_ENTRY_PRICE, …, _CROSS_EXCHANGE_FEEDS_ACTIVE) | `bot.constants` |
| ~50 helpers (cell-block / sizing / feature / validators / breakers) | `bot.helpers` |
| _BREAKER_REGISTRY | `bot.infra.circuit_breaker.REGISTRY` |
| recent_writes, snapshot_active, tracked_write | `bot.db_writer_registry` |

Bit 9.3-iii.c (shipped 2026-05-11, ticket 86b9w1jdz) DELETED `bot/_impl.py` —
the residual shim is GONE. Production reaches names via canonical submodules:
- `bot/__main__.py` uses `from bot.main_loop import MainLoop` directly (Bit 9.3-ii)
- `bot/state.py` uses `from bot.boot import compute_for_15m_main_path` (Bit 9.3-iii.a)
- The proxy fall-through that previously made `bot.X → bot._impl.X` resolve was retired (Bit 9.3-iii.b)
- `dashboard_snapshot.py` + `supabase_sync.py` read runtime config via
  `import bot.runtime_config as _bot_mod` (PEP 562 dual-probe of bot.constants → config;
  Bit 9.3-iii.c — replaces bot._impl as the getattr target).

The body of this `__init__.py` is intentionally empty — Python's default
package import semantics auto-create `sys.modules['bot']` as a plain
`types.ModuleType` on first `import bot`, which is exactly what we want.
"""
